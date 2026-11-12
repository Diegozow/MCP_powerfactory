# -*- coding: utf-8 -*-
"""Standard-library HTTP client used by the external MCP wrapper and stop command."""

import json
import threading
from http.client import HTTPException
from pathlib import Path
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from pf_connection import default_connection_path


PROTOCOL = "pf-mcp-bridge-v1"


class BridgeError(Exception):
    def __init__(self, code, message):
        super().__init__(code + ": " + message)
        self.code = code


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def load_connection(path):
    try:
        info = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BridgeError("CONNECTION_NOT_FOUND", "No connection file exists. Launch pf_agent.py through ComPython, or verify --connection. No request was sent.") from exc
    except OSError as exc:
        raise BridgeError("CONNECTION_UNREADABLE", "The connection file could not be read. Verify its path and access permissions. No request was sent.") from exc
    except (UnicodeError, ValueError) as exc:
        raise BridgeError("INVALID_CONNECTION", "The connection file is not valid UTF-8 JSON. Use the file created by pf_agent.py. No request was sent.") from exc
    if not isinstance(info, dict) or info.get("protocol") != PROTOCOL:
        raise BridgeError("WRONG_CONNECTION", "Use the current pf_mcp_connection.json created by pf_agent.py.")
    for key in ("base_url", "token", "agent_instance_id"):
        if not isinstance(info.get(key), str) or not info[key]:
            raise BridgeError("INVALID_CONNECTION", "Missing connection setting: " + key)
    try:
        url = urlsplit(info["base_url"])
        if (url.scheme != "http" or url.hostname != "127.0.0.1"
                or url.username is not None or url.password is not None
                or url.path not in ("", "/") or url.query or url.fragment
                or url.port is None or not 1 <= url.port <= 65535):
            raise ValueError("Invalid local address.")
    except ValueError as exc:
        raise BridgeError("INVALID_CONNECTION", "Expected http://127.0.0.1:<port>.") from exc
    if type(info.get("powerfactory_process_id")) is not int:
        raise BridgeError("INVALID_CONNECTION", "Missing PowerFactory process ID.")
    return info


class PFBridge:
    def __init__(self, connection_path=None, timeout=10):
        self.connection_path = Path(default_connection_path() if connection_path is None else connection_path).resolve()
        self.timeout = timeout
        self.lock = threading.Lock()

    def request(self, endpoint, fields, expected_instance_id=None, timeout=None):
        # Reload for every request so a new ComPython session needs no wrapper restart.
        with self.lock:
            connection = load_connection(self.connection_path)
            if expected_instance_id is not None and connection["agent_instance_id"] != expected_instance_id:
                raise BridgeError("INSTANCE_MISMATCH", "The work session changed; no stop was sent.")
            body = {"agent_instance_id": connection["agent_instance_id"], **fields}
            request = Request(
                connection["base_url"].rstrip("/") + endpoint,
                data=json.dumps(body, ensure_ascii=False).encode("utf-8"), method="POST",
                headers={"Authorization": "Bearer " + connection["token"],
                         "Content-Type": "application/json"},
            )
            opener = build_opener(ProxyHandler({}), NoRedirects())
            try:
                try:
                    response = opener.open(request, timeout=self.timeout if timeout is None else timeout)
                except HTTPError as exc:
                    response = exc
                with response:
                    status, raw = response.status, response.read(1048577)
            except (URLError, OSError, HTTPException) as exc:
                if fields.get("tool") in ("save_operation_scenario", "create_operation_scenario") and fields.get("arguments", {}).get("confirmation_digest") is not None:
                    raise BridgeError("SCENARIO_PERSISTENCE_OUTCOME_UNKNOWN", "No scenario-persistence response was received. A timeout does not cancel Save/SaveAsScenario or placement. Inspect the active scenario, saved data and scenario listings, including new MCP_ staging names, before retrying. Do not automatically retry or restart.") from exc
                if fields.get("tool") == "set_study_time":
                    raise BridgeError("STUDY_TIME_OUTCOME_UNKNOWN", "No study-time response was received. A timeout does not cancel the time change or restore the prior time. Read get_active_session before another attempt; do not automatically retry or restart.") from exc
                if fields.get("tool") in ("run_power_flow", "run_short_circuit"):
                    request_id = fields.get("arguments", {}).get("request_id", "unknown")
                    raise BridgeError("CALCULATION_OUTCOME_UNKNOWN", "No calculation response was received for request_id=" + str(request_id) + ". A timeout/disconnection does not cancel Execute. Query get_calculation_status with that ID in the same agent session; do not automatically rerun with a new ID or after a restart.") from exc
                if fields.get("tool") == "mark_in_graphics":
                    raise BridgeError("GRAPHIC_MARK_OUTCOME_UNKNOWN", "No highlighting response was received. A timeout does not cancel native graphics work. Inspect the active diagram/highlighting and read get_graphic_selection before retrying; do not automatically restart the agent.") from exc
                if fields.get("tool") == "export_graphic":
                    raise BridgeError("GRAPHIC_EXPORT_OUTCOME_UNKNOWN", "No graphic-export response was received. A timeout does not cancel export or restoration. Inspect the output file and PowerFactory page/export settings before retrying; do not automatically restart the agent.") from exc
                if fields.get("tool") == "activate_study_case":
                    raise BridgeError("ACTIVATION_OUTCOME_UNKNOWN", "No activation response was received. A timeout/disconnection does not cancel Activate. Check get_active_session before another attempt; do not automatically retry or restart the agent.") from exc
                if fields.get("tool") == "activate_operation_scenario":
                    raise BridgeError("SCENARIO_OUTCOME_UNKNOWN", "No scenario response was received. A timeout does not cancel deactivation/activation or restore discarded changes. Read get_active_session before another attempt; do not automatically retry or restart.") from exc
                if fields.get("tool") in ("activate_network_variation", "deactivate_network_variation"):
                    raise BridgeError("VARIATION_OUTCOME_UNKNOWN", "No variation response was received. A timeout/disconnection does not cancel PowerFactory work. Check list_network_variations before another attempt; do not automatically retry or restart the agent.") from exc
                reason = getattr(exc, "reason", exc)
                expired_hint = ""
                try:
                    expiry = datetime.fromisoformat(connection.get("expires_at_utc", ""))
                    if expiry.tzinfo is not None and expiry <= datetime.now(timezone.utc):
                        expired_hint = " The connection file's maximum expiry has passed; it may be stale. This metadata alone does not establish the live agent state."
                except (TypeError, ValueError):
                    pass
                if isinstance(reason, TimeoutError):
                    raise BridgeError("AGENT_TIMEOUT", "The agent did not respond in time and may be busy. A timeout does not cancel PowerFactory work; do not automatically restart." + expired_hint) from exc
                if isinstance(reason, ConnectionRefusedError) or getattr(reason, "winerror", None) == 10061:
                    raise BridgeError("AGENT_UNREACHABLE", "No listener accepted the connection at the recorded local address. Verify the agent and connection file; do not infer a prior calculation outcome." + expired_hint) from exc
                raise BridgeError("AGENT_DISCONNECTED", "The agent connection failed or disconnected. This does not establish that PowerFactory stopped or its work was cancelled." + expired_hint) from exc
            if len(raw) > 1048576:
                raise BridgeError("INVALID_RESPONSE", "Response exceeds 1 MiB.")
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeError, ValueError) as exc:
                raise BridgeError("INVALID_RESPONSE", "Agent returned invalid JSON.") from exc
            if not isinstance(payload, dict):
                raise BridgeError("INVALID_RESPONSE", "Agent response must be an object.")
            if payload.get("agent_instance_id") != connection["agent_instance_id"]:
                raise BridgeError("INSTANCE_MISMATCH", "Response belongs to another agent session.")
            if status != 200 or payload.get("ok") is not True:
                error = payload.get("error")
                if not isinstance(error, dict):
                    error = {}
                # Never expose a token accidentally included in an error message.
                message = str(error.get("message", "Agent request failed.")).replace(connection["token"], "[redacted]")
                raise BridgeError(str(error.get("code", "HTTP_" + str(status))), message)
            if endpoint == "/call":
                execution = payload.get("execution")
                if not isinstance(execution, dict):
                    execution = {}
                if (payload.get("powerfactory_process_id") != connection["powerfactory_process_id"]
                        or payload.get("tool") != fields.get("tool")
                        or not isinstance(payload.get("data"), dict)
                        or execution.get("source") != "live_powerfactory_session"
                        or execution.get("read_only") is not (fields.get("tool") not in ("run_power_flow", "run_short_circuit", "capture_result_snapshot", "activate_study_case", "activate_network_variation", "deactivate_network_variation", "activate_operation_scenario", "set_study_time", "export_graphic", "mark_in_graphics") and (fields.get("tool") not in ("save_operation_scenario", "create_operation_scenario") or fields.get("arguments", {}).get("confirmation_digest") is None))
                        or execution.get("api_calls_on_script_thread") is not True
                        or type(execution.get("script_thread_id")) is not int
                        or execution.get("api_call_thread_id") != execution.get("script_thread_id")):
                    raise BridgeError("INVALID_RESPONSE", "Session, tool, or execution-thread check failed.")
            elif payload.get("stop_requested") is not True:
                raise BridgeError("INVALID_RESPONSE", "Stop request was not accepted.")
            return payload

    def call(self, tool, arguments, timeout=None):
        return self.request("/call", {"tool": tool, "arguments": arguments}, timeout=timeout)

    def stop(self, expected_instance_id=None):
        return self.request("/stop", {}, expected_instance_id)
