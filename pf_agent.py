# -*- coding: utf-8 -*-
"""Step 8: calculation-specific result candidates and availability-aware sample reads.

Standard library only. All PowerFactory API calls stay on the launching
script thread. Optional Qt event processing keeps the GUI active between requests.
"""

import ctypes
import hmac
import importlib
import json
import os
import secrets
import socket
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# Reject nested launches before reloading modules used by the active agent.
if getattr(sys.modules.get("powerfactory"), "_pf_mcp_agent_running", False):
    raise RuntimeError("The MCP agent is already active. Stop it before launching another agent.")

# ComPython can retain imported modules between script launches.
importlib.reload(importlib.import_module("pf_connection"))
importlib.reload(importlib.import_module("pf_model"))
importlib.reload(importlib.import_module("pf_protection_studies"))
from pf_connection import default_connection_path, prepare_private_directory
from pf_model import (
    CALCULATION_TOOLS, RequestError, execute_read, list_calculation_commands,
    run_calculation, session_info, validate_arguments, calculation_outcome,
    capture_result_snapshot,
    browse_powerfactory_tree, activate_study_case,
    list_network_variations, change_network_variation,
    activate_operation_scenario, set_study_time, powerfactory_capabilities, IMPLEMENTATION_VERSION,
    export_graphic, write_operation_scenario, mark_in_graphics, run_short_circuit_at_bus, set_operating_setpoints,
)
from pf_protection_studies import (
    inspect_protection_study, run_short_circuit_fault, run_protection_sweep, get_protection_sweep_status,
    read_protection_sweep_results,
)


HOST = "127.0.0.1"
PORT = 8767
RUN_SECONDS = 28800
FIRST_REQUEST_TIMEOUT_SECONDS = 1800
IDLE_TIMEOUT_SECONDS = 1800
SOCKET_TIMEOUT_SECONDS = 3
REQUEST_READ_TIMEOUT_SECONDS = 5
VERSION = IMPLEMENTATION_VERSION
PROTOCOL = "pf-mcp-bridge-v1"


class GuiEventPump:
    """Use only PowerFactory's already-loaded Qt5 ABI, on the script thread."""

    def __init__(self, app, requested=False):
        self.app = app
        self.requested = requested
        self.enabled = False
        self.thread_id = threading.get_ident()
        self.iterations = 0
        self.echo_on_called = False
        self.error = None
        self.restore_error = None
        self.previous_gui_updates = None
        if not requested:
            return
        try:
            if sys.platform != "win32" or ctypes.sizeof(ctypes.c_void_p) != 8:
                raise RuntimeError("GUI event processing requires 64-bit Windows PowerFactory Qt5.")
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            get_module = kernel.GetModuleHandleW
            get_module.argtypes = [ctypes.c_wchar_p]
            get_module.restype = ctypes.c_void_p
            path = str(Path(app.GetInstallationDirectory()) / "Qt5Core_pf.dll")
            handle = get_module(path)
            if not handle:
                raise RuntimeError("PowerFactory's Qt5Core_pf.dll is not loaded in this process.")
            self.qt = ctypes.CDLL(path, handle=handle)
            self.process = getattr(self.qt, "?processEvents@QCoreApplication@pf@@SAXV?$QFlags@W4ProcessEventsFlag@QEventLoop@pf@@@2@H@Z")
            self.process.argtypes = [ctypes.c_int, ctypes.c_int]
            self.process.restype = None
            self.send_posted = getattr(self.qt, "?sendPostedEvents@QCoreApplication@pf@@SAXPEAVQObject@2@H@Z")
            self.send_posted.argtypes = [ctypes.c_void_p, ctypes.c_int]
            self.send_posted.restype = None
            previous = app.SetGuiUpdateEnabled(1)
            if type(previous) is not int or previous not in (0, 1):
                raise RuntimeError("SetGuiUpdateEnabled did not return its previous 0/1 state.")
            self.previous_gui_updates = previous
            app.EchoOn()  # ComPython can leave input deactivated independently of GUI redraws.
            self.echo_on_called = True
            self.enabled = True
        except Exception as exc:
            self.error = (type(exc).__name__ + ": " + str(exc))[:320]

    def process_events(self):
        if not self.enabled:
            return
        if threading.get_ident() != self.thread_id:
            raise RuntimeError("Process GUI events only on the original ComPython thread.")
        try:
            self.process(0, 5)  # AllEvents; callbacks/modal dialogs can exceed this time slice.
            self.send_posted(None, 52)  # DeferredDelete is not drained by a local processEvents loop.
            self.iterations += 1
        except Exception as exc:
            self.enabled = False
            self.error = (type(exc).__name__ + ": " + str(exc))[:320]

    def close(self):
        if self.previous_gui_updates is not None and threading.get_ident() != self.thread_id:
            raise RuntimeError("Restore GUI updates only on the original ComPython thread.")
        self.enabled = False
        if self.previous_gui_updates is not None:
            try:
                self.app.SetGuiUpdateEnabled(self.previous_gui_updates)
            except Exception as exc:
                self.restore_error = (type(exc).__name__ + ": " + str(exc))[:320]
            self.previous_gui_updates = None

    def status(self):
        return {"requested": self.requested, "event_pump_enabled": self.enabled,
                "backend": "PowerFactory loaded Qt5" if self.enabled else None,
                "processed_iterations": self.iterations, "thread_id": self.thread_id,
                "echo_on_called": self.echo_on_called,
                "between_requests_only": True, "error": self.error, "restore_error": self.restore_error}


def execute_tool(server, tool, arguments):
    if tool == "get_protection_sweep_status":
        return get_protection_sweep_status(arguments)
    if tool == "get_powerfactory_capabilities":
        return powerfactory_capabilities(server.app)
    if tool == "browse_powerfactory_tree":
        return browse_powerfactory_tree(server.app, arguments)
    if tool == "activate_study_case":
        return activate_study_case(server.app, server.instance_id, arguments)
    info = session_info(server.app)
    if tool == "inspect_protection_study":
        return inspect_protection_study(server.app, info, arguments)
    if tool == "read_protection_sweep_results":
        return read_protection_sweep_results(server.app, info, arguments)
    if tool == "run_short_circuit_fault":
        return run_short_circuit_fault(server.app, server.instance_id, server.calculation_records, info, arguments)
    if tool == "run_protection_sweep":
        return run_protection_sweep(server.app, server.instance_id, info, arguments)
    if tool == "set_operating_setpoints":
        return set_operating_setpoints(server.app, server.instance_id, info, arguments)
    if tool in ("save_operation_scenario", "create_operation_scenario"):
        return write_operation_scenario(server.app, server.instance_id, info, tool, arguments)
    if tool == "export_graphic":
        return export_graphic(server.app, info, arguments)
    if tool == "mark_in_graphics":
        return mark_in_graphics(server.app, server.instance_id, info, arguments)
    if tool == "set_study_time":
        return set_study_time(server.app, server.instance_id, info, arguments)
    if tool == "activate_operation_scenario":
        return activate_operation_scenario(server.app, server.instance_id, info, arguments)
    if tool == "get_active_session":
        return {**execute_read(server.app, tool, arguments), "session": server.session_status()}
    if tool == "list_network_variations":
        return list_network_variations(server.app, info, arguments)
    if tool in ("activate_network_variation", "deactivate_network_variation"):
        return change_network_variation(server.app, server.instance_id, info, tool, arguments)
    if tool in ("capture_result_snapshot", "export_results"):
        snapshot_id = arguments["snapshot_id"]
        if tool == "capture_result_snapshot" and snapshot_id in server.snapshots:
            raise RequestError("SNAPSHOT_ID_CONFLICT", "This snapshot ID already exists; use a new ID.", 409)
        if tool == "capture_result_snapshot" and len(server.snapshots) >= 16:
            raise RequestError("SNAPSHOT_STORAGE_FULL", "This session holds 16 snapshots; start a new session for more.", 409)
        request_id = arguments["calculation_request_id"]
        record = server.calculation_records.get(request_id) if request_id is not None else None
        if request_id is not None and record is None:
            raise RequestError("CALCULATION_RECORD_NOT_FOUND", "The requested calculation record does not exist in this agent session.", 404)
        data = capture_result_snapshot(server.app, info, arguments, server.instance_id,
                                       record["response"] if record is not None else None)
        if tool == "export_results":
            data.pop("snapshot_id")
            data["export_complete"] = data.pop("snapshot_complete")
            data["stored_in_session"] = False
            data["note"] = "Plain selected result data for external export. No snapshot was stored and no calculation was run. Check validity, context, coverage and export_complete."
        encoded = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(encoded) > 524288:
            raise RequestError("EXPORT_TOO_LARGE" if tool == "export_results" else "SNAPSHOT_TOO_LARGE", "Selected result data exceeds 512 KiB; select fewer results.", 409)
        if tool == "export_results":
            return data
        server.snapshots[snapshot_id] = json.loads(encoded)
        return server.snapshots[snapshot_id]
    if tool == "get_result_snapshot":
        snapshot_id = arguments["snapshot_id"]
        if snapshot_id not in server.snapshots:
            raise RequestError("SNAPSHOT_NOT_FOUND", "No snapshot with ID " + snapshot_id + " exists in this session.", 404)
        return server.snapshots[snapshot_id]
    if tool == "list_calculation_commands":
        return list_calculation_commands(server.app, info, arguments)
    if tool == "run_short_circuit_at_bus":
        return run_short_circuit_at_bus(server.app, server.instance_id, server.calculation_records, info, arguments)
    if tool in CALCULATION_TOOLS:
        return run_calculation(server.app, server.instance_id, server.calculation_records, info, tool, arguments)
    if tool == "get_calculation_status":
        record = server.calculation_records.get(arguments["request_id"])
        if record is None:
            raise RequestError("CALCULATION_RECORD_NOT_FOUND", "No execution record with this ID exists in this agent session; no calculation was run by this query.", 404)
        return {**info, "request_id": arguments["request_id"],
                "record": {**record["response"], "outcome": calculation_outcome(record["response"])},
                "historical_record": True, "note": "This is the stored attempt, not a freshness check. Records are lost when ComPython stops; do not automatically retry after an agent restart."}
    return execute_read(server.app, tool, arguments)


class AgentServer(HTTPServer):
    allow_reuse_address = False

    def __init__(self, app, port=PORT, run_seconds=RUN_SECONDS,
                 first_timeout=FIRST_REQUEST_TIMEOUT_SECONDS, idle_timeout=IDLE_TIMEOUT_SECONDS, gui_pump=None):
        if min(run_seconds, first_timeout, idle_timeout) <= 0:
            raise ValueError("All session time limits must be positive.")
        self.app = app
        self.gui_pump = gui_pump
        self.owner_thread_id = threading.get_ident()
        self.instance_id = str(uuid.uuid4())
        self.token = secrets.token_urlsafe(32)
        self.started_at = datetime.now(timezone.utc)
        self.expires_at = self.started_at + timedelta(seconds=run_seconds)
        now = time.monotonic()
        self.deadline = now + run_seconds
        self.wait_deadline = min(self.deadline, now + first_timeout)
        self.idle_timeout = idle_timeout
        self.first_timeout = first_timeout
        self.work_started = False
        self.stop_requested = False
        self.tool_requests = 0
        self.calculation_records = {}
        self.snapshots = {}
        super().__init__((HOST, port), AgentHandler)
        self.timeout = 0.25

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def connection_info(self):
        return {
            "schema_version": 1, "protocol": PROTOCOL, "agent_version": VERSION,
            "base_url": "http://" + HOST + ":" + str(self.server_address[1]),
            "token": self.token, "agent_instance_id": self.instance_id,
            "powerfactory_process_id": os.getpid(),
            "started_at_utc": self.started_at.isoformat(),
            "expires_at_utc": self.expires_at.isoformat(),
            "first_request_timeout_seconds": self.first_timeout,
            "idle_timeout_seconds": self.idle_timeout,
        }

    def session_status(self):
        remaining = max(0, self.deadline - time.monotonic())
        idle_remaining = min(remaining, self.idle_timeout)
        return {"agent_instance_id": self.instance_id, "started_at_utc": self.started_at.isoformat(),
                "maximum_expires_at_utc": self.expires_at.isoformat(),
                "idle_expires_at_utc": (datetime.now(timezone.utc) + timedelta(seconds=idle_remaining)).isoformat(),
                "remaining_lifetime_seconds": round(remaining, 1), "remaining_idle_seconds": round(idle_remaining, 1),
                "maximum_lifetime_seconds": int((self.expires_at - self.started_at).total_seconds()),
                "first_request_timeout_seconds": self.first_timeout, "idle_timeout_seconds": self.idle_timeout,
                "calculation_record_count": len(self.calculation_records), "snapshot_count": len(self.snapshots),
                "snapshot_ids": sorted(self.snapshots), "snapshot_limit": 16,
                "gui": self.gui_pump.status() if self.gui_pump is not None else {"requested": False, "event_pump_enabled": False},
                "note": "This call refreshes the idle window. Times are estimates at response creation. Operations are synchronous; status calls wait behind PowerFactory work. GUI event processing, when enabled, runs only between requests; modal dialogs can delay requests. Snapshots and calculation records last only for this agent session. Protection-sweep steps use durable files and are excluded from calculation_record_count; inspect get_protection_sweep_status."}

    def serve_until_stopped(self):
        if threading.get_ident() != self.owner_thread_id:
            raise RuntimeError("Serve from the thread that created the PowerFactory agent.")
        while not self.stop_requested and time.monotonic() < min(self.deadline, self.wait_deadline):
            if self.gui_pump is not None:
                self.gui_pump.process_events()
                self.timeout = 0.02 if self.gui_pump.enabled else 0.25
            if self.stop_requested or time.monotonic() >= min(self.deadline, self.wait_deadline):
                break
            self.handle_request()
        if self.stop_requested:
            return "stop_request"
        if time.monotonic() >= self.deadline:
            return "lifetime_expired"
        return "idle_timeout" if self.work_started else "first_request_timeout"

    def completed_work(self):
        self.work_started = True
        self.wait_deadline = min(self.deadline, time.monotonic() + self.idle_timeout)

    def handle_error(self, request, client_address):
        # Expected socket disconnects must not kill the work session.
        pass


class AgentHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "PFStep7Agent/" + VERSION
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(SOCKET_TIMEOUT_SECONDS)
        self.read_deadline = time.monotonic() + REQUEST_READ_TIMEOUT_SECONDS
        # This thread only closes a stalled socket; it never calls PowerFactory.
        self.read_timer = threading.Timer(REQUEST_READ_TIMEOUT_SECONDS, self.expire_read)
        self.read_timer.daemon = True
        self.read_timer.start()

    def expire_read(self):
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def finish(self):
        self.read_timer.cancel()
        super().finish()

    def log_message(self, format, *args):
        pass

    def reply(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass
        self.close_connection = True

    def error_reply(self, status, code, message):
        self.reply(status, {
            "ok": False, "agent_instance_id": self.server.instance_id,
            "error": {"code": code, "message": message},
        })

    def authorize(self):
        host = "127.0.0.1:" + str(self.server.server_address[1])
        if self.headers.get_all("Host", []) != [host]:
            self.error_reply(403, "INVALID_HOST", "Use the exact numeric loopback address and port.")
            return False
        # Only the native bridge is supported; no browser Origin is accepted.
        if self.headers.get_all("Origin", []):
            self.error_reply(403, "BROWSER_ORIGIN_NOT_ALLOWED", "Browser-origin requests are not supported.")
            return False
        authorization = self.headers.get_all("Authorization", [])
        actual = authorization[0].encode("utf-8") if len(authorization) == 1 else b""
        expected = ("Bearer " + self.server.token).encode("utf-8")
        if not hmac.compare_digest(actual, expected):
            self.error_reply(401, "UNAUTHORIZED", "Valid agent token required.")
            return False
        if time.monotonic() >= min(self.server.deadline, self.server.wait_deadline):
            self.error_reply(410, "SESSION_EXPIRED", "Execute pf_agent.py through ComPython again.")
            return False
        return True

    def read_body(self):
        if self.headers.get("Transfer-Encoding"):
            raise RequestError("INVALID_BODY", "Use Content-Length, not chunked encoding.")
        lengths = self.headers.get_all("Content-Length", [])
        try:
            if len(lengths) != 1:
                raise ValueError("Provide one Content-Length header.")
            length = int(lengths[0])
            if not 0 < length <= 65536:
                raise ValueError("Body must be 1 to 65536 bytes.")
            if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
                raise ValueError("Use Content-Type: application/json.")
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if time.monotonic() >= self.read_deadline:
                raise RequestError("REQUEST_READ_TIMEOUT", "The request was not received in time.", 408)
            if not isinstance(body, dict):
                raise ValueError("Expected a JSON object.")
            # Native work has its own lifetime; a network deadline cannot cancel it.
            self.read_timer.cancel()
            return body
        except (ValueError, UnicodeError, OSError) as exc:
            raise RequestError("INVALID_BODY", str(exc)) from exc

    def do_GET(self):
        if self.authorize():
            self.error_reply(405, "METHOD_NOT_ALLOWED", "Use POST /call or POST /stop.")

    def do_POST(self):
        if not self.authorize():
            return
        if self.path not in ("/call", "/stop"):
            self.error_reply(404, "NOT_FOUND", "Use /call or /stop.")
            return
        try:
            body = self.read_body()
            if body.get("agent_instance_id") != self.server.instance_id:
                raise RequestError("INSTANCE_MISMATCH", "Use the current connection file.", 409)
            if self.path == "/stop":
                if set(body) != {"agent_instance_id"}:
                    raise RequestError("INVALID_BODY", "Stop takes only agent_instance_id.")
                self.server.stop_requested = True
                self.reply(200, {"ok": True, "agent_instance_id": self.server.instance_id,
                                 "stop_requested": True})
                return
            if set(body) != {"agent_instance_id", "tool", "arguments"}:
                raise RequestError("INVALID_BODY", "Call takes agent_instance_id, tool, and arguments.")
            tool = body["tool"]
            arguments = validate_arguments(tool, body["arguments"])
            api_thread_id = threading.get_ident()
            if api_thread_id != self.server.owner_thread_id:
                raise RequestError("WRONG_THREAD", "PowerFactory calls must use the script thread.", 500)
            self.server.tool_requests += 1
            started = time.perf_counter()
            try:
                data = execute_tool(self.server, tool, arguments)
            finally:
                # Valid attempted work resets idle time, including an API error.
                self.server.completed_work()
            self.reply(200, {
                "ok": True, "agent_instance_id": self.server.instance_id,
                "powerfactory_process_id": os.getpid(), "tool": tool,
                "data": data,
                "execution": {
                    "source": "live_powerfactory_session", "read_only": tool not in CALCULATION_TOOLS and tool not in ("capture_result_snapshot", "activate_study_case", "activate_network_variation", "deactivate_network_variation", "activate_operation_scenario", "set_study_time", "export_graphic", "mark_in_graphics") and (tool not in ("save_operation_scenario", "create_operation_scenario", "run_short_circuit_at_bus", "set_operating_setpoints", "run_short_circuit_fault", "run_protection_sweep") or arguments["confirmation_digest"] is None) and (tool != "read_protection_sweep_results" or arguments["file_name"] is None),
                    "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                    "script_thread_id": self.server.owner_thread_id,
                    "api_call_thread_id": api_thread_id,
                    "api_calls_on_script_thread": True,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            })
        except RequestError as exc:
            self.error_reply(exc.status, exc.code, str(exc))
        except Exception as exc:
            self.error_reply(500, "POWERFACTORY_API_ERROR", type(exc).__name__ + ": " + str(exc))


def write_connection(path, info):
    path = Path(path).resolve()
    prepare_private_directory(path.parent)
    temporary = path.with_name(path.name + "." + info["agent_instance_id"] + ".tmp")
    try:
        descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(info, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(str(temporary), str(path))
    finally:
        if temporary.exists():
            temporary.unlink()


def remove_own_connection(path, instance_id):
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(info, dict) and info.get("agent_instance_id") == instance_id:
            path.unlink()
    except (OSError, ValueError):
        pass


def run_agent(app, connection_path, port=PORT, run_seconds=RUN_SECONDS,
              first_timeout=FIRST_REQUEST_TIMEOUT_SECONDS, idle_timeout=IDLE_TIMEOUT_SECONDS, gui_events=False):
    if app is None or app.GetCurrentScript() is None:
        raise RuntimeError("Launch pf_agent.py through ComPython in the existing PowerFactory GUI.")
    if app.GetCurrentUser() is None:
        raise RuntimeError("Log in to PowerFactory before launching the agent.")
    connection_path = Path(connection_path).resolve()
    gui = GuiEventPump(app, requested=gui_events)
    try:
        with AgentServer(app, port, run_seconds, first_timeout, idle_timeout, gui_pump=gui) as server:
            try:
                write_connection(connection_path, server.connection_info())
                app.PrintPlain("[PF MCP] Listening at " + server.connection_info()["base_url"])
                if gui.enabled:
                    app.PrintPlain("[PF MCP] GUI events enabled between requests. Calculations and dialogs remain synchronous.")
                else:
                    app.PrintPlain("[PF MCP] GUI event processing disabled; original blocking mode. " + (gui.error or ""))
                app.PrintPlain("[PF MCP] First request: " + str(first_timeout) + " s; idle: "
                               + str(idle_timeout) + " s; maximum: " + str(run_seconds) + " s.")
                app.PrintPlain("[PF MCP] Stop from the project folder: python -c \"from pf_bridge import PFBridge; PFBridge().stop()\"")
                reason = server.serve_until_stopped()
            finally:
                remove_own_connection(connection_path, server.instance_id)
            summary = {"stop_reason": reason, "tool_requests": server.tool_requests}
    finally:
        gui.close()
    summary["gui"] = gui.status()
    app.PrintPlain("[PF MCP] Finished: " + reason + "; tool requests: " + str(summary["tool_requests"]))
    return summary


def main(gui_events=True):
    pf = importlib.import_module("powerfactory")
    if getattr(pf, "_pf_mcp_agent_running", False):
        raise RuntimeError("The MCP agent is already active. Stop it before launching another agent.")
    pf._pf_mcp_agent_running = True
    try:
        # ComPython retains this module between launches; keep one native application owner.
        app = getattr(pf, "_pf_mcp_application", None)
        if app is not None:
            try:
                app.GetCurrentScript()
            except RuntimeError as exc:
                if str(exc) != "'powerfactory.Application' already deleted":
                    raise
                # A retained Python wrapper can outlive its native application.
                pf._pf_mcp_application = None
                app = None
        if app is None:
            app = pf.GetApplication()
            if app is not None:
                try:
                    app.GetCurrentScript()
                except RuntimeError as exc:
                    if str(exc) != "'powerfactory.Application' already deleted":
                        raise
                    raise RuntimeError(
                        "PowerFactory returned a deleted application handle. Restart PowerFactory, "
                        "then launch pf_agent.py through ComPython again."
                    ) from exc
                pf._pf_mcp_application = app
        return run_agent(app, default_connection_path(), gui_events=gui_events)
    finally:
        pf._pf_mcp_agent_running = False


if __name__ == "__main__":
    main()
