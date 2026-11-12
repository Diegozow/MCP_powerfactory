# -*- coding: utf-8 -*-
"""External stdio MCP wrapper. The CLI launches this; never run it in ComPython."""

import argparse
import csv
import hashlib
import io
import json
import re
from pathlib import Path
from typing import Annotated, Any, Literal

import anyio
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from pf_bridge import BridgeError, PFBridge
from pf_connection import default_connection_path
from pf_model import PROFILE_DATA, IMPLEMENTATION_VERSION


_profiles = PROFILE_DATA
ElementClass = Literal.__getitem__(tuple(_profiles["elements"]))
Profile = Literal["basic", "extended"]
Calculation = Literal.__getitem__(("all", *_profiles["calculations"]))
FullName = Annotated[str, Field(min_length=1, max_length=2048)]
AttributeName = Annotated[str, Field(pattern=r"^(?:[A-Za-z][A-Za-z0-9_]{0,63}|[mcs]:[A-Za-z][A-Za-z0-9_]*(?::[A-Za-z][A-Za-z0-9_]*){0,2})$", max_length=64)]
AttributeList = Annotated[list[AttributeName], Field(min_length=1, max_length=100)]
CalculationRequestId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{7,79}$")]
ResultPaths = Annotated[list[FullName], Field(max_length=8)]
SnapshotId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")]


def write_result_export(data, file_name):
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\.(csv|json)", file_name) is None:
        raise ToolError("INVALID_ARGUMENTS: Use a simple filename ending in .csv or .json.")
    rows = []
    metadata = {key: value for key, value in data.items() if key not in ("elements", "full_names", "result_attributes")}
    for item in data["elements"]:
        for name in data["result_attributes"]:
            attribute = item.get("attributes", {}).get(name, {})
            rows.append({"element_full_name": item["full_name"], "attribute": name,
                         "value_json": json.dumps(attribute.get("value"), ensure_ascii=False, allow_nan=False),
                         "unit": attribute.get("unit") or "", "status": attribute.get("status", "error"),
                         "error_json": json.dumps(attribute.get("error") or item.get("error"), ensure_ascii=False),
                         "metadata_errors_json": json.dumps(attribute.get("metadata_errors", {}), ensure_ascii=False),
                         "context_json": json.dumps(metadata, ensure_ascii=False, allow_nan=False)})
    if file_name.endswith(".json"):
        content = json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
    else:
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
        content = stream.getvalue()
    encoded = content.encode("utf-8")
    if len(encoded) > 8388608:
        raise ToolError("EXPORT_TOO_LARGE: Select fewer results; the artifact limit is 8 MiB.")
    root = Path(__file__).resolve().parent
    directory = (root / "exports").resolve()
    path = (directory / file_name).resolve()
    if not directory.is_relative_to(root) or path.parent != directory:
        raise ToolError("EXPORT_OUT_OF_SCOPE: Exports must stay inside this MCP workspace's exports folder.")
    created = False
    try:
        directory.mkdir(exist_ok=True)
        with path.open("x", encoding="utf-8", newline="") as output:
            created = True
            output.write(content)
    except FileExistsError as exc:
        raise ToolError("EXPORT_EXISTS: This filename already exists; choose a new name.") from exc
    except OSError as exc:
        if created:
            path.unlink(missing_ok=True)
        raise ToolError("EXPORT_WRITE_FAILED: " + str(exc)) from exc
    return {"file_path": str(path), "format": path.suffix[1:], "byte_count": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest(), "row_count": len(rows),
            "export_complete": data["export_complete"], "context": data["context"],
            "source": data["source"], "calculation": data["calculation"],
            "successful_attribute_count": data["successful_attribute_count"],
            "failed_attribute_count": data["failed_attribute_count"],
            "note": "Check completeness, per-result status and validity in the artifact. No calculation was run and no snapshot slot was used."}


class PowerFactoryServer(MCPServer):
    async def list_tools(self):
        listed = await super().list_tools()
        for tool in listed:
            tool.input_schema["additionalProperties"] = False
        return listed

    async def call_tool(self, name, arguments, context=None):
        # The SDK's default argument model ignores extras. Reject them before
        # coercion so an unsupported calculation setting cannot disappear.
        for tool in await self.list_tools():
            if tool.name == name:
                unexpected = set(arguments) - set(tool.input_schema.get("properties", {}))
                if unexpected:
                    raise ToolError("INVALID_ARGUMENTS: Unsupported fields: " + ", ".join(sorted(unexpected)))
                break
        return await super().call_tool(name, arguments, context)


def build_server(connection_path, timeout=10, calculation_timeout=120):
    bridge = PFBridge(connection_path, timeout)
    server = PowerFactoryServer(
        "powerfactory", version=IMPLEMENTATION_VERSION, log_level="WARNING",
        instructions=(
            "Model inspection and explicit calculations in the user's existing PowerFactory session. Return native PowerFactory values with factual context, units and read/operation metadata; leave engineering analysis to the client. "
            "Call get_active_session before inspecting model elements. "
            "Use get_active_scenario to read the active operation scenario's exact identity; scenario=null means none is active. "
            "Use list_operation_scenarios for all scenarios in the active project, including nested folders, exact paths and active markers. "
            "get_active_session also reports scenario, active variations, raw study time, recording stage and context read errors. "
            "Study time includes utc_seconds from IntCase.iStudyTime; do not infer a display timezone from cDate/cTime. "
            "Use set_study_time only for an explicitly requested zoned timestamp, with expected prior epoch seconds, case and agent ID. "
            "get_power_balance reads native summary-grid or selected-grid attributes without unit conversion or residual calculations. "
            "get_bus_voltage_sequences returns native m:u1/m:u2 and terminal phase/system fields without calculating a ratio. "
            "get_transformer_control_status separates configured taps, solved taps, type limits, controller references and current command settings. "
            "Use get_powerfactory_capabilities to compare the running agent's tool/source manifest with this server; licensing can remain unknown. "
            "Use activate_operation_scenario only with an explicit target and permission to discard prior unsaved scenario changes when switching. "
            "Use save_operation_scenario/create_operation_scenario without confirmation_digest for a read-only preview; show its destination and native stored/current samples before an authorized confirmed write. Saving persists all native operational data, not only sampled values. Creation keeps the source active and never overwrites an existing destination. "
            "get_bus_voltage reads native m:u1 by default, with phase/scalar choices and original reported units. "
            "get_transformer_loading covers ElmTr2/ElmTr3 existing native c:loading values without thresholds. No calculation runs. "
            "export_results writes selected results to a new CSV/JSON in this workspace's exports folder; inspect export_complete. "
            "Use browse_powerfactory_tree for stateless metadata navigation under the current user, including inactive projects. "
            "It needs no active project/study case and never activates objects; copy exact returned paths to descend or use direction=parent. "
            "Use activate_study_case only when the user requests a switch, with an exact browsed IntCase path inside the active project and the current project path/agent ID. "
            "Check success and the resulting study_case; after an activation timeout, read get_active_session before another attempt. "
            "Use list_network_variations to read active/inactive states in the current study case, including nested variation folders. "
            "Use list_expansion_stages with an exact listed variation path for native stage times, active/excluded/recording states and scheduler information. Unknown states are not false. "
            "For stage times, prefer activation_time_display with its explicit offset; label activation_time_utc as UTC. Check display_time_verified. "
            "Activate/deactivate a network variation only when requested, using its exact listed IntScheme path and the expected case/agent ID. "
            "Check success, active, and the before/after active-variation lists. After a timeout, list variations before another attempt. "
            "The user must launch pf_agent.py through ComPython for live calls. "
            "Element listings are calculation-relevant elements for the active study case. "
            "Use an element's exact full_name to read its parameter attributes. "
            "Use get_element_connections for fixed endpoint/cubicle/busbar and switch-state reads on lines, transformers and ElmCoup; physical wiring, switch position and service state are separate. "
            "Use get_short_circuit_contributions for native bus Ikss/angle and directly connected element-end currents, connection states, current fault settings and validity; no calculation or source tracing is performed. "
            "Use get_protection_chain for one exact ElmRelay or StaCubic: native protection containment, pelm measurement/block references, pSwitch references and assigned types. Contains is not proof of trip wiring; follow next_offset and check read_complete/errors. "
            "Use get_graphic_selection for the current network-diagram selection and exact inspectable model paths. Use mark_in_graphics only when highlighting is requested, with the expected diagram/case/agent; it changes UI state, skips absent/unknown symbols and can activate another already open diagram. Highlighting is distinct from selection; inspect observed after-state and do not treat native_call_completed as visual verification. "
            "Use get_feeder_inventory without a path to discover native feeder definitions, then select an exact feeder for membership; valid load-flow results are required and no calculation runs. "
            "Use export_graphic only for an explicit existing SetDeskpage/GrpPage under the active case's Graphics Board and a new PNG/SVG/PDF basename in exports; it creates a standard ComWr once if missing. Check command_created, export_complete and restoration_errors. "
            "Check each attribute's status and unit; an ok call may contain attribute errors. "
            "Use get_equipment_type_attributes with the SOURCE element full_name to read its live typ_id. "
            "Transformer instance parameters and equipment-type ratings are separate reads. "
            "Raw unit text can be blank or reference-class metadata; do not present it as a physical unit. "
            "Only one explicit typ_id hop is available; other references remain metadata. "
            "Use get_model_inventory to find populated classes, then list_element_attributes for available candidate names. "
            "The catalog is a checked candidate profile, not exhaustive enumeration. "
            "Use profile=extended to read available parameters from the broader class profile. "
            "pf_profiles.json separates parameters and per-calculation result candidates/defaults. "
            "Use list_element_attributes with include_results=true and calculation to inspect state-dependent candidates. "
            "get_element_attributes/get_elements_attributes can include valid calculation defaults with include_results=true; check result_selection and its before/after validity. RMS time-series reading/execution is not implemented. "
            "Use get_elements_attributes for up to eight elements per call; check each item's and attribute's status. "
            "Existing m:/c:/s: result values can be requested explicitly; no calculation or freshness guarantee is provided. "
            "Use get_line_loading to read all selected native c:loading values without thresholds or loading ranking. "
            "Native-result readers retain readable values with native validity flags, original units and per-attribute errors; paginate by exact path. "
            "get_active_session includes expiry estimates and session-only storage counts; it never exposes connection credentials. "
            "Use capture_result_snapshot for explicit result selections and get_result_snapshot with one snapshot_id for stored native data. "
            "Snapshots are immutable, limited to 16 per agent session and lost at restart. Capture changes only agent memory. "
            "A calculation_request_id snapshot uses only results already stored in that attempt, never current values labeled as historical. "
            "Run a calculation only when requested by the user. First list_calculation_commands and inspect its current raw settings. "
            "run_power_flow and run_short_circuit execute only an existing ComLdf/ComShc with its GUI settings; no option overrides or object creation. "
            "Copy the exact command and study-case paths and expected_agent_instance_id from the session envelope; provide a unique request_id known before execution. "
            "The same ID and arguments replay the stored record within this agent session instead of executing twice. "
            "A timeout does not cancel Execute: query get_calculation_status, do not automatically rerun or restart the agent. "
            "Calculation commands update results and can change automatic-control state. Check success, return_code, validity_after and per-result errors. "
            "outcome separates execution, validity and result_quality. A zero Execute return is not proof of valid or complete results. "
            "Result element paths select reads, not short-circuit fault locations. Those locations and the method/type remain the GUI command settings."
        ),
    )
    annotations = ToolAnnotations(
        read_only_hint=True, destructive_hint=False,
        idempotent_hint=True, open_world_hint=False,
    )
    calculation_annotations = ToolAnnotations(
        read_only_hint=False, destructive_hint=False,
        idempotent_hint=False, open_world_hint=False,
    )

    def call(tool, arguments):
        try:
            long_call = tool in ("run_power_flow", "run_short_circuit", "get_calculation_status", "get_line_loading", "capture_result_snapshot", "browse_powerfactory_tree", "activate_study_case", "list_operation_scenarios", "list_network_variations", "list_expansion_stages", "activate_network_variation", "deactivate_network_variation", "activate_operation_scenario", "get_bus_voltage", "get_transformer_loading", "export_results", "set_study_time", "get_power_balance", "get_bus_voltage_sequences", "get_transformer_control_status", "get_element_connections", "get_short_circuit_contributions", "get_protection_chain", "get_graphic_selection", "mark_in_graphics", "get_feeder_inventory", "export_graphic", "save_operation_scenario", "create_operation_scenario")
            return bridge.call(tool, arguments, timeout=calculation_timeout if long_call else None)
        except BridgeError as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(title="Get active PowerFactory session", annotations=annotations)
    def get_active_session() -> dict[str, Any]:
        """Read active model context, Python version, and credential-free session details.

        session includes start/expiry times, remaining lifetime/idle estimates,
        configured time limits, calculation record count and snapshot IDs/count.
        This call refreshes the idle window. Status waits behind synchronous
        PowerFactory operations; a timeout is not proof that the agent stopped.
        Also returns scenario, active_variations, recording_stage and raw study
        date/time with metadata. Check context_complete/context_errors; an unknown
        field is not an empty active state. No timezone conversion is inferred.
        """
        return call("get_active_session", {})

    @server.tool(title="Inspect PowerFactory server and agent capabilities", annotations=annotations)
    async def get_powerfactory_capabilities() -> dict[str, Any]:
        """Compare this server's real tool catalog with the loaded agent's manifest.

        Includes implementation/Python/PowerFactory product/interface versions,
        native API presence, supported element classes and loaded-source hashes.
        Method presence is not proof of licensing; license status may be unknown.
        Works without an active project/case. An unreachable/older agent is an
        explicit diagnostic result with server metadata, not a compatible state.
        """
        catalog = await server.list_tools()
        names = sorted(item.name for item in catalog)
        schemas = {item.name: item.input_schema for item in catalog}
        source_names = ("pf_agent.py", "pf_model.py", "pf_connection.py", "pf_profiles.json")
        sources = {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                   if Path(__file__).with_name(name).exists() else None for name in source_names}
        report = {"server": {"implementation_version": IMPLEMENTATION_VERSION, "tool_names": names,
                             "supported_element_classes": list(_profiles["elements"]),
                             "input_schema_sha256": hashlib.sha256(json.dumps(schemas, sort_keys=True).encode("utf-8")).hexdigest(),
                             "expected_agent_sources": sources}}
        try:
            response = await anyio.to_thread.run_sync(call, "get_powerfactory_capabilities", {})
        except ToolError as exc:
            return {**report, "agent": None, "compatibility": {"status": "agent_unavailable_or_outdated", "error": str(exc)}}
        agent = response["data"]
        matches = {"tool_names_match": names == sorted(agent.get("supported_tools", [])),
                   "element_classes_match": sorted(_profiles["elements"]) == sorted(agent.get("supported_element_classes", [])),
                   "sources_match": sources == agent.get("loaded_sources"),
                   "implementation_version_match": IMPLEMENTATION_VERSION == agent.get("implementation_version")}
        return {**report, "agent": agent, "agent_instance_id": response["agent_instance_id"],
                "powerfactory_process_id": response["powerfactory_process_id"], "execution": response["execution"],
                "compatibility": {**matches, "status": "compatible" if all(matches.values()) else "mismatch"}}

    @server.tool(title="Get the active PowerFactory operation scenario", annotations=annotations)
    def get_active_scenario() -> dict[str, Any]:
        """Read the active operation scenario directly from GetActiveScenario.

        Takes no arguments. Returns the active project/study case and scenario
        metadata (name, class, exact full_name). scenario=null and
        has_active_scenario=false mean no operation scenario is active.
        Requires an active project and study case. API errors remain errors;
        they are never interpreted as no active scenario. The context and
        scenario are checked again before returning. No activation, parameter
        changes or calculation is performed.
        """
        return call("get_active_scenario", {})

    @server.tool(title="Preview or save the active operation scenario", annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False,
    ))
    def save_operation_scenario(
        expected_active_scenario_full_name: FullName,
        expected_study_case_full_name: FullName,
        expected_agent_instance_id: Annotated[str, Field(pattern=r"^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$")],
        confirmation_digest: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")] | None = None,
    ) -> dict[str, Any]:
        """Omit confirmation_digest for a read-only preview of saving the active scenario.

        Copy the exact active scenario, case and agent from get_active_session.
        Preview identifies the destination, stored-object counts and sampled
        saved/current operational values. It is not a complete unsaved-change
        list. Save persists current native operational attributes for all active
        network elements, potentially more edits than a single discussed value.

        Show the preview before an authorized write; copy its confirmation_digest
        to confirm. Context/destination/shown samples are recomputed and checked.
        This guard is not a full model-state lock. Saves only the active scenario
        through IntScenario.Save; never activates, creates, discards or performs
        a full-project save/calculation. Native return 1 is ambiguous between
        unmodified/inactive/error and is not reported as successful persistence.
        Check success, error, saved_data and context_unchanged. No automatic retry
        or rollback; after a timeout inspect saved state before another attempt.
        """
        return call("save_operation_scenario", {
            "expected_active_scenario_full_name": expected_active_scenario_full_name,
            "expected_study_case_full_name": expected_study_case_full_name,
            "expected_agent_instance_id": expected_agent_instance_id, "confirmation_digest": confirmation_digest,
        })

    @server.tool(title="Preview or create an inactive operation scenario", annotations=calculation_annotations)
    def create_operation_scenario(
        folder_full_name: FullName,
        scenario_name: Annotated[str, Field(min_length=1, max_length=120)],
        expected_active_scenario_full_name: FullName | None,
        expected_study_case_full_name: FullName,
        expected_agent_instance_id: Annotated[str, Field(pattern=r"^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$")],
        confirmation_digest: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")] | None = None,
    ) -> dict[str, Any]:
        """Omit confirmation_digest for a read-only preview of a new scenario capture.

        Select an existing root/IntFolder under the active project's operation
        scenarios and a new simple name; duplicate names in that folder are
        rejected case-insensitively. Copy expected context from get_active_session;
        use null for the expected scenario only when none is active.

        Show the exact destination and native stored/current values before
        approval, then return the preview's confirmation_digest for creation.
        Native SaveAsScenario captures current operational data with activation
        disabled. The new object is staged under a unique MCP_ name, moved into
        the selected existing folder and renamed. Existing scenarios/folders are
        not overwritten or created; the source remains active. Context/sample
        checks do not cover every operational attribute or a full model revision.

        Check success, scenario, saved_data and source_summary_unchanged. On
        failure/timeout a new capture may remain at its actual/staging path;
        inspect list_operation_scenarios before retrying. No automatic deletion,
        rollback, activation, calculation or full-project save.
        """
        return call("create_operation_scenario", {
            "folder_full_name": folder_full_name, "scenario_name": scenario_name,
            "expected_active_scenario_full_name": expected_active_scenario_full_name,
            "expected_study_case_full_name": expected_study_case_full_name,
            "expected_agent_instance_id": expected_agent_instance_id, "confirmation_digest": confirmation_digest,
        })

    @server.tool(title="Set the active study case's study time", annotations=calculation_annotations)
    def set_study_time(
        timestamp_utc: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})$", max_length=25)],
        expected_study_time_seconds: Annotated[int, Field(ge=0, le=4294967295, strict=True)],
        expected_study_case_full_name: FullName,
        expected_agent_instance_id: Annotated[str, Field(pattern=r"^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$")],
    ) -> dict[str, Any]:
        """Set an explicitly requested timestamp using SetTime.SetTimeUTC once.

        Include Z or an explicit UTC offset and whole seconds. Copy expected
        prior seconds from get_active_session.data.study_time.utc_seconds.value,
        plus the current case path and agent ID. Supports epoch seconds from
        0 through 4294967295; PowerFactory/project constraints can reject a time.
        Display cDate/cTime are not assumed to be UTC or the computer's timezone.

        Returns verified native epoch readback, before/after context, success,
        errors and result-validity flags. An already matching time skips the
        setter. Time changes can affect stages, characteristics and outages and
        invalidate results. No calculation, save, retry or rollback is performed.
        After a timeout, read get_active_session before another attempt.
        """
        return call("set_study_time", {
            "timestamp_utc": timestamp_utc, "expected_study_time_seconds": expected_study_time_seconds,
            "expected_study_case_full_name": expected_study_case_full_name,
            "expected_agent_instance_id": expected_agent_instance_id,
        })

    @server.tool(title="List operation scenarios and the active scenario", annotations=annotations)
    def list_operation_scenarios(
        name_contains: Annotated[str, Field(max_length=120)] = "",
        offset: Annotated[int, Field(ge=0, le=1000000, strict=True)] = 0,
        limit: Annotated[int, Field(ge=1, le=50, strict=True)] = 20,
    ) -> dict[str, Any]:
        """List every operation scenario under the active project's scenario folder.

        Includes nested folders. Each row has name, class, exact full_name,
        folder_full_name and an active boolean checked against GetActiveScenario.
        active_scenario identifies the current scenario even when filtered out;
        null means none is active. Requires an active project and study case.
        Read errors remain errors; they are never interpreted as inactive.

        name_contains is a case-insensitive literal name filter. Rows sort by
        name/path and paginate after filtering; follow next_offset. total_count,
        active_count and inactive_count cover the whole folder before filtering.
        Use full_name for activation, not a row number. This read performs no
        activation, deactivation, save or calculation.
        """
        return call("list_operation_scenarios", {
            "name_contains": name_contains, "offset": offset, "limit": limit,
        })

    @server.tool(title="Browse the PowerFactory object tree", annotations=annotations)
    def browse_powerfactory_tree(
        full_name: FullName | None = None,
        direction: Literal["children", "parent"] = "children",
        object_class: Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")] | None = None,
        name_contains: Annotated[str, Field(max_length=120)] = "",
        offset: Annotated[int, Field(ge=0, le=1000000, strict=True)] = 0,
        limit: Annotated[int, Field(ge=1, le=50, strict=True)] = 20,
    ) -> dict[str, Any]:
        """Browse the current user's visible PowerFactory hierarchy without activation.

        Omit full_name to start at the current user. To go down or jump, copy an
        exact full_name from a returned child, parent, breadcrumb or session.
        direction=children lists that object's immediate contents;
        direction=parent moves up one containment level and lists its siblings.
        At the user root, parent stays at the root. No shared current-directory
        state is stored. This browses PowerFactory objects, not Windows folders.

        Includes other visible/inactive projects under this user, project
        folders, study cases, equipment libraries and model objects. It never
        activates a project/case, follows parameter references, executes a
        calculation or changes model data. No active project/case is required;
        project/study_case report the active context (possibly null), while
        current and browsed_project identify what is being browsed.

        Filter by exact object_class (e.g. IntPrj or IntCase, no wildcard) and a
        case-insensitive literal name substring. Counts/class_counts cover
        direct children only; nested study cases require browsing their folders.
        Results sort by name/path and paginate after filtering. Check
        listing_complete and metadata_error_count; child_errors contains at
        most 50 details. Visibility/access errors are explicit. Paths outside
        the current user's tree and relative path syntax are rejected.
        """
        return call("browse_powerfactory_tree", {
            "full_name": full_name, "direction": direction, "object_class": object_class,
            "name_contains": name_contains, "offset": offset, "limit": limit,
        })

    @server.tool(title="Activate a PowerFactory study case", annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False,
    ))
    def activate_study_case(
        study_case_full_name: FullName,
        expected_project_full_name: FullName,
        expected_agent_instance_id: Annotated[str, Field(pattern=r"^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$")],
    ) -> dict[str, Any]:
        """Activate one exact existing IntCase inside the active project, when requested.

        Copy study_case_full_name from browse_powerfactory_tree. Copy the active
        project path and agent ID from the current session or tree response.
        A project must be active; an active study case is optional. Other projects are
        rejected. Names and list numbers alone are not accepted.

        Returns previous/requested/resulting study cases, success, already_active,
        activation_attempted, return_code and any error. An already active target
        skips Activate. Success requires the expected project and target case
        to be observed after Activate returns integer zero.

        Activation applies the case's configured context and may invalidate
        results. No calculation or database save is requested. Stored snapshots
        and calculation records remain historical. A timeout does not cancel
        activation: check get_active_session before another attempt. There is
        no automatic retry, rollback, project activation or record clearing.
        """
        return call("activate_study_case", {
            "study_case_full_name": study_case_full_name,
            "expected_project_full_name": expected_project_full_name,
            "expected_agent_instance_id": expected_agent_instance_id,
        })

    @server.tool(title="Activate an operation scenario", annotations=calculation_annotations)
    def activate_operation_scenario(
        scenario_full_name: FullName,
        expected_study_case_full_name: FullName,
        expected_agent_instance_id: Annotated[str, Field(pattern=r"^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$")],
        expected_active_scenario_full_name: FullName | None,
        discard_unsaved_changes: Annotated[bool, Field(strict=True)] = False,
    ) -> dict[str, Any]:
        """Activate an exact IntScenario in the active project's scenario folder.

        Call only for an explicitly requested scenario switch. Copy the target
        path from tree browsing and expected case/agent/current scenario from
        get_active_session. Use null for the expected scenario only when none
        is active. An already active target skips all mutation.

        Switching from an active scenario requires discard_unsaved_changes=true
        with user authorization: this version cannot reliably query whether the
        scenario is dirty. It explicitly deactivates the old scenario with
        Deactivate(0), discarding its unsaved changes, then calls Activate once.
        It never silently saves. Return codes and resulting context are verified.
        Check success, scenario, both attempt flags, return codes and error.

        Failure can leave no scenario active. There is no automatic rollback.
        Results may become invalid. No calculation is run. On timeout, read
        get_active_session before another attempt; activation is not cancelled.
        """
        return call("activate_operation_scenario", {
            "scenario_full_name": scenario_full_name,
            "expected_study_case_full_name": expected_study_case_full_name,
            "expected_agent_instance_id": expected_agent_instance_id,
            "expected_active_scenario_full_name": expected_active_scenario_full_name,
            "discard_unsaved_changes": discard_unsaved_changes,
        })

    @server.tool(title="List network variations and their active states", annotations=annotations)
    def list_network_variations(
        name_contains: Annotated[str, Field(max_length=120)] = "",
        offset: Annotated[int, Field(ge=0, le=1000000, strict=True)] = 0,
        limit: Annotated[int, Field(ge=1, le=50, strict=True)] = 20,
    ) -> dict[str, Any]:
        """Read network variations and active/inactive states in the current study case.

        Includes nested folders in the active project's network-variations folder.
        Each row has name, class, exact full_name and active boolean, checked
        against GetActiveNetworkVariations. A failed state read raises an error;
        it is never interpreted as inactive. Requires an active project/case.
        Activation state does not establish which timed expansion stages apply.

        name_contains is a case-insensitive literal name filter. Rows sort by
        name/path and paginate after filtering; follow next_offset. total_count,
        active_count and inactive_count cover the whole folder before filtering.
        This tool performs no activation, deactivation or calculation.
        """
        return call("list_network_variations", {
            "name_contains": name_contains, "offset": offset, "limit": limit,
        })

    @server.tool(title="List expansion stages and their current native states", annotations=annotations)
    def list_expansion_stages(
        variation_full_name: FullName,
        name_contains: Annotated[str, Field(max_length=120)] = "",
        offset: Annotated[int, Field(ge=0, le=1000000, strict=True)] = 0,
        limit: Annotated[int, Field(ge=1, le=50, strict=True)] = 20,
    ) -> dict[str, Any]:
        """Read stages inside one exact IntScheme from list_network_variations.

        Requires an active project/case. Lists stages recursively without
        activating the variation, including when it is inactive. Returns native
        tAcTime, activation_time_display with an explicit offset, UTC rendering,
        GetActiveStages membership, IsExcluded and the
        recording marker separately, alongside study-time context and scheduler.
        Active/excluded/recording states are not derived from dates.

        Prefer activation_time_display for times shown to the PowerFactory user;
        label activation_time_utc explicitly as UTC. Local rendering uses the
        PowerFactory host timezone after matching its native study-time display.
        Check display_time_verified; mismatches leave display fields null with
        errors. Effective display times follow the same scheduler rules as UTC.

        Unknown reads stay null with errors. tAcTime may be overridden by an active
        scheduler; effective scheduler times remain unknown in that case. Check
        stage_status_complete, status_errors and per-stage errors. Counts cover
        the whole variation before filtering. Name filtering is a literal,
        case-insensitive substring; rows sort by name/path and paginate after
        filtering. No activation, time change, calculation or save is performed.
        """
        return call("list_expansion_stages", {
            "variation_full_name": variation_full_name, "name_contains": name_contains,
            "offset": offset, "limit": limit,
        })

    @server.tool(title="Activate a network variation", annotations=calculation_annotations)
    def activate_network_variation(
        variation_full_name: FullName,
        expected_study_case_full_name: FullName,
        expected_agent_instance_id: Annotated[str, Field(pattern=r"^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$")],
    ) -> dict[str, Any]:
        """Activate one exact listed network variation when explicitly requested.

        Copy the IntScheme path from list_network_variations and the expected
        study-case path/agent ID from the current session. Only the active
        project's network variations are supported. Already active targets skip
        Activate. Success requires integer zero plus verified active state.
        Returns previous/requested/actual state, change_attempted, return_code,
        success, error and all active variations before/after the request.

        PowerFactory applies applicable expansion stages at the existing study
        time and may adjust other active variations. Results may become invalid.
        No calculation, time change, stage editing, consolidation or database save
        is requested. On timeout, check list_network_variations before another
        attempt; do not automatically retry or restart. No automatic rollback.
        """
        return call("activate_network_variation", {
            "variation_full_name": variation_full_name,
            "expected_study_case_full_name": expected_study_case_full_name,
            "expected_agent_instance_id": expected_agent_instance_id,
        })

    @server.tool(title="Deactivate a network variation", annotations=calculation_annotations)
    def deactivate_network_variation(
        variation_full_name: FullName,
        expected_study_case_full_name: FullName,
        expected_agent_instance_id: Annotated[str, Field(pattern=r"^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$")],
    ) -> dict[str, Any]:
        """Deactivate one exact listed network variation when explicitly requested.

        Copy the IntScheme path from list_network_variations and the expected
        study-case path/agent ID from the current session. Only the active
        project's network variations are supported. Already inactive targets skip
        Deactivate. Success requires integer zero plus verified inactive state.
        Returns previous/requested/actual state, change_attempted, return_code,
        success, error and all active variations before/after the request.

        Removing the variation changes the model represented by the study case
        and may invalidate results. No calculation, time change, stage editing,
        consolidation or database save is requested. On timeout, check
        list_network_variations before another attempt; do not automatically
        retry or restart. No automatic rollback.
        """
        return call("deactivate_network_variation", {
            "variation_full_name": variation_full_name,
            "expected_study_case_full_name": expected_study_case_full_name,
            "expected_agent_instance_id": expected_agent_instance_id,
        })

    @server.tool(title="List PowerFactory model elements", annotations=annotations)
    def list_model_elements(
        element_class: ElementClass = "ElmTerm",
        name_contains: Annotated[str, Field(max_length=120)] = "",
        offset: Annotated[int, Field(ge=0, le=1000000, strict=True)] = 0,
        limit: Annotated[int, Field(ge=1, le=50, strict=True)] = 20,
        network_full_name: FullName | None = None,
        in_service: Annotated[bool, Field(strict=True)] | None = None,
        nominal_voltage_min_kv: Annotated[float, Field(ge=0, allow_inf_nan=False, strict=True)] | None = None,
        nominal_voltage_max_kv: Annotated[float, Field(ge=0, allow_inf_nan=False, strict=True)] | None = None,
    ) -> dict[str, Any]:
        """List names/classes/full paths of calculation-relevant elements in the active case.

        Supports 24 classes including terminals, lines, transformers, loads,
        synchronous/asynchronous machines (ElmSym/ElmAsm), shunts (ElmShnt), grids (ElmNet/ElmXnet),
        line/cable couplings (ElmTow/ElmCabsys), mutual data (ElmMut), switches,
        neutral earthing elements, voltage regulators and protection relays (ElmRelay).
        Protection blocks RelToc (time overcurrent) and RelIoc (instantaneous
        overcurrent) are included, even when nested inside relay objects.
        name_contains is a case-insensitive literal
        substring of the name. Results are sorted by name/full path. Use the
        returned next_offset for the next page; None means no further page.

        Optional filters combine before pagination. Copy network_full_name from
        an ElmNet listing; scope includes that exact network and its descendants,
        not electrically connected elements outside it. in_service checks only
        the element's own outserv flag, not energisation or connectivity.
        Nominal-voltage bounds are inclusive, in kV, and supported only for
        ElmTerm. uknom is converted using its reported V/kV/MV unit.
        Filter read failures are excluded from matches and reported through
        filter_error_count and up to 50 filter_errors; filtering_complete=false
        means matched_count is incomplete. No calculation or model edit occurs.
        """
        return call("list_model_elements", {
            "element_class": element_class, "name_contains": name_contains,
            "offset": offset, "limit": limit,
            "network_full_name": network_full_name, "in_service": in_service,
            "nominal_voltage_min_kv": nominal_voltage_min_kv,
            "nominal_voltage_max_kv": nominal_voltage_max_kv,
        })

    @server.tool(title="Read native PowerFactory line loading", annotations=annotations)
    def get_line_loading(
        network_full_name: FullName | None = None,
        name_contains: Annotated[str, Field(max_length=120)] = "",
        offset: Annotated[int, Field(ge=0, le=1000000, strict=True)] = 0,
        limit: Annotated[int, Field(ge=1, le=50, strict=True)] = 20,
    ) -> dict[str, Any]:
        """Read native outserv/c:loading records on all selected ElmLne objects.

        Returns original values, units, metadata/errors and native validity flags,
        including out-of-service elements. Readable values remain visible when
        validity is false or unknown. No calculation, threshold comparison or
        loading ranking is performed. Network/name filters precede pagination
        by exact full path. Follow next_offset for every row.
        """
        return call("get_line_loading", {"network_full_name": network_full_name,
            "name_contains": name_contains, "offset": offset, "limit": limit})

    @server.tool(title="Inspect native feeder definitions and membership", annotations=annotations)
    def get_feeder_inventory(
        feeder_full_name: FullName | None = None,
        include_nested: Annotated[bool, Field(strict=True)] = False,
        object_class: Annotated[str, Field(pattern=r"^Elm[A-Za-z0-9_]{1,61}$")] | None = None,
        name_contains: Annotated[str, Field(max_length=120)] = "",
        offset: Annotated[int, Field(ge=0, le=1000000, strict=True)] = 0,
        limit: Annotated[int, Field(ge=1, le=50, strict=True)] = 20,
    ) -> dict[str, Any]:
        """Omit feeder_full_name to list native ElmFeeder definitions in Network Data.

        Select an exact returned feeder path to read native GetAll membership.
        Membership requires existing valid load-flow results before and after;
        unknown/invalid validity produces unknown counts, never an empty-feeder
        claim. No load flow is run. include_nested explicitly includes nested
        feeders and defaults to false. object_class filters members by exact
        Elm class; it and include_nested require a selected feeder.

        class_counts/member_count cover all native members before filtering.
        Name filtering is a literal case-insensitive substring; results sort by
        name/path and paginate after filtering. Definitions, folder contents and
        a topology trace are different scopes. No membership is inferred from
        names or modeled as a path trace. Check status/inventory_complete and
        validity fields. No activation, model edit or database save is requested.
        """
        return call("get_feeder_inventory", {
            "feeder_full_name": feeder_full_name, "include_nested": include_nested,
            "object_class": object_class, "name_contains": name_contains, "offset": offset, "limit": limit,
        })

    @server.tool(title="Read the active diagram selection and model identities", annotations=annotations)
    def get_graphic_selection(
        offset: Annotated[int, Field(ge=0, le=1000000, strict=True)] = 0,
        limit: Annotated[int, Field(ge=1, le=50, strict=True)] = 50,
    ) -> dict[str, Any]:
        """Read the top network diagram's native selection without changing the UI.

        Returns exact SetDeskpage and IntGrfnet identities and selected objects.
        IntGrf selections follow only their native pDataObj link; annotations,
        unsupported classes and non-calculation-relevant model objects remain
        explicit. Pass inspection.full_name to existing attribute/type/connection
        tools only when inspection.available is true. Copy diagram.full_name
        and the session's study case/agent identity for a later highlight request.
        Follow next_offset and check read_complete and per-object mapping errors.
        A missing diagram is an explicit error, distinct from an empty selection.
        No page opening, selection change, model edit or calculation. The current
        synchronous agent keeps the GUI busy; select before launching if needed.
        """
        return call("get_graphic_selection", {"offset": offset, "limit": limit})

    @server.tool(title="Highlight exact model elements in open diagrams", annotations=calculation_annotations)
    def mark_in_graphics(
        full_names: Annotated[list[FullName], Field(min_length=1, max_length=8)],
        expected_diagram_full_name: FullName,
        expected_study_case_full_name: FullName,
        expected_agent_instance_id: Annotated[str, Field(pattern=r"^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$")],
    ) -> dict[str, Any]:
        """Highlight 1-8 exact supported model elements only when requested.

        Copy diagram.full_name from get_graphic_selection and case/agent identity
        from the active session. Preflights every exact calculation-relevant
        target and its symbols in the active network diagram. Only present
        targets reach Application.MarkInGraphics(objects, 1); absent or unknown
        symbols are reported/skipped. No symbol or electrical object is created.

        The native method searches open diagrams and may activate one. Returns
        the observed selection/context afterward; selection and highlighting
        are separate facts. Native completion is not visual verification and
        highlight_verified remains unknown. Check errors, mark_attempted,
        native_call_completed and the before/after diagram/context. After a
        timeout inspect the UI and read selection before retrying. No settings
        write, calculation or save is requested. This tool changes UI state.
        """
        return call("mark_in_graphics", {"full_names": full_names,
                    "expected_diagram_full_name": expected_diagram_full_name,
                    "expected_study_case_full_name": expected_study_case_full_name,
                    "expected_agent_instance_id": expected_agent_instance_id})

    @server.tool(title="Export an existing PowerFactory diagram or plot", annotations=calculation_annotations)
    def export_graphic(
        page_full_name: FullName,
        file_name: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}\.(png|svg|pdf)$")],
        command_full_name: FullName | None = None,
    ) -> dict[str, Any]:
        """Export an explicitly selected existing graphic tab to a new workspace file.

        Copy the exact SetDeskpage diagram-tab or GrpPage plot path by browsing
        the active study case's Graphics Board. Uses ComWr.ExportGraphicTab on
        a ComWr command in that case. Supply command_full_name if several exist.
        If none exists and no exact command was requested, creates PowerFactory's
        standard ComWr once and retains it; command_created reports this setup.
        Missing pages or an explicitly requested missing command are errors.
        file_name is a simple PNG/SVG/PDF basename under this workspace's exports
        folder. Existing files are rejected. The native method receives the
        explicit page and file path; no Show or Execute is used to select/export.

        Checks the format signature, bytes/hash, context, original command
        format/path/save-dialog settings and displayed page. These are restored if the native
        API changed them. Check export_complete, error and restoration_errors;
        a graphic is not a complete result dataset or proof of fresh results.
        No calculation or database save. A timeout can leave an artifact; inspect
        exports and the study case before retrying, because export/restoration
        is not automatically canceled. First-use command setup can remain even
        when export fails.
        """
        return call("export_graphic", {"page_full_name": page_full_name, "file_name": file_name,
                                       "command_full_name": command_full_name})

    @server.tool(title="Read electrical endpoints and switch states", annotations=annotations)
    def get_element_connections(full_name: FullName) -> dict[str, Any]:
        """Inspect one exact calculation-relevant ElmLne/ElmTr2/ElmTr3/ElmCoup.

        Returns two line/switch ends, two transformer windings or three windings
        for ElmTr3. Each end includes GetCubicle, the direct GetNode(index, 0)
        terminal, GetNode(index, 1) with switch states considered, and PowerFactory's
        resolved cubicle cBusBar. Lists direct StaSwitch children of each cubicle
        with native IsClosed and raw on_off, plus the source ElmCoup state.

        Physical wiring, switch-aware endpoint availability, switch closed/open
        and the source element's own outserv/in_service flag are separate facts.
        A complete read may include disconnected ends. Null states with errors
        are unknown, never assumed disconnected/open/out of service. Cubicle
        switch lists do not enumerate every series ElmCoup in detailed substations.
        Check status, read_complete and per-end/per-switch errors. No path tracing,
        energisation assessment, switching, parameter changes or calculation.
        """
        return call("get_element_connections", {"full_name": full_name})

    @server.tool(title="Read native protection relationships and assigned types", annotations=annotations)
    def get_protection_chain(
        full_name: FullName,
        offset: Annotated[int, Field(ge=0, le=1000000, strict=True)] = 0,
        limit: Annotated[int, Field(ge=1, le=50, strict=True)] = 50,
    ) -> dict[str, Any]:
        """Inspect one exact ElmRelay or StaCubic in the active project, read-only.

        Copy a relay path from list_model_elements or a cubicle path from the
        tree/connection tools. Enumerates nested relays and Rel* blocks, native
        pelm references to CT/VT/measurement blocks, explicit pSwitch references
        from RelLogdip/RelLogic, available settings and one assigned-type hop.
        Preserves native values, units, null references and zero-based array slots.
        Contains edges describe physical/model containment, never inferred trip
        wiring. Other referenced classes/projects remain metadata only.

        Pagination selects nodes and their outgoing relationships; targets can
        appear on another page. Follow next_offset. The traversal has a fixed
        500-object limit; select a narrower relay if exceeded. read_complete
        covers the supported read scope, not the completeness of protection
        logic. Inspect errors, per-attribute status and excluded_attributes.
        No logic evaluation, calculation, creation, settings write or save.
        """
        return call("get_protection_chain", {"full_name": full_name, "offset": offset, "limit": limit})

    @server.tool(title="Read native short-circuit contributions at a bus", annotations=annotations)
    def get_short_circuit_contributions(
        bus_full_name: FullName,
        offset: Annotated[int, Field(ge=0, le=1000000, strict=True)] = 0,
        limit: Annotated[int, Field(ge=1, le=50, strict=True)] = 50,
    ) -> dict[str, Any]:
        """Read existing bus Ikss/angle and currents at directly connected element ends.

        Copy an exact ElmTerm full_name from list_model_elements. Reads native
        m:Ikss/m:phii at the bus and m:Ikss:<side>/m:phii:<side> at matching
        physical terminals, with original units, descriptions and errors.
        Supports lines, two/three-winding transformers, couplers, synchronous/
        asynchronous machines, static generators and external grids. Other
        connected classes are listed as unsupported; follow next_offset.

        Returns physical endpoints, switch/service states, before/after native
        short-circuit validity and existing ComShc settings. Open/out-of-service
        elements and readable invalid/unknown results remain visible. Current
        settings do not establish which settings produced the stored results or
        prove that this bus was faulted. All-bus/multiple-fault results are not
        contributions to a single fault. Scalar fields retain native signs/angles;
        no phase conversion, phasor sum, source tracing or calculation is done.
        Check per-end status, read_complete, coverage_complete and validity.
        """
        return call("get_short_circuit_contributions", {"bus_full_name": bus_full_name,
                                                       "offset": offset, "limit": limit})

    @server.tool(title="Read native network power balance", annotations=annotations)
    def get_power_balance(network_full_name: FullName | None = None) -> dict[str, Any]:
        """Read native grid P/Q attributes with their original values and units.

        Omit network_full_name for GetSummaryGrid across active grids, or supply
        one exact ElmNet path. Reads generation, external-grid infeed, loads,
        motors, compensation, losses and boundary interchange. Returns native
        validity flags and per-attribute errors. Readable values are retained
        even when validity is false or unknown. No unit conversion, residual,
        aggregation, limit check or calculation is performed.
        """
        return call("get_power_balance", {"network_full_name": network_full_name})

    @server.tool(title="Inspect transformer taps and control settings", annotations=annotations)
    def get_transformer_control_status(full_name: FullName) -> dict[str, Any]:
        """Read one exact calculation-relevant ElmTr2/ElmTr3 and its tap controls.

        Separates configured tap positions, available valid solved taps, type
        limits, voltage targets and current ComLdf automatic-tap settings.
        ntrcn means automatic tap changing; i_auto means autotransformer design.
        Follows only typ_id and the named c_ptapc/c_pstac controller references;
        controller attributes are focused candidates, not exhaustive settings.
        Missing fields/references remain explicit, never inferred false/zero.

        No tap or control setting is changed and no calculation runs. Type tap
        limits can differ from operational limits. An enabled flag or controller
        reference does not prove active control authority or achieved regulation.
        Check per-attribute status/units, exclusions, type_error, context and
        validity. Configured taps are not substituted for unavailable solved taps.
        """
        return call("get_transformer_control_status", {"full_name": full_name})

    @server.tool(title="Read native bus voltage sequences", annotations=annotations)
    def get_bus_voltage_sequences(
        network_full_name: FullName | None = None,
        name_contains: Annotated[str, Field(max_length=120)] = "",
        offset: Annotated[int, Field(ge=0, le=1000000, strict=True)] = 0,
        limit: Annotated[int, Field(ge=1, le=50, strict=True)] = 20,
    ) -> dict[str, Any]:
        """Read native m:u1/m:u2, outserv, nphase and systype on selected ElmTerm objects.

        Returns original units, errors, native IsEnergized values and load-flow
        validity flags without hiding readable values. Includes every selected
        terminal regardless of service, supply or phase technology. No sequence
        ratio, threshold comparison, ranking or calculation is performed.
        Network/name filters precede pagination by exact full path.
        """
        return call("get_bus_voltage_sequences", {"network_full_name": network_full_name,
            "name_contains": name_contains, "offset": offset, "limit": limit})

    @server.tool(title="Read native bus voltage values", annotations=annotations)
    def get_bus_voltage(
        quantity: Literal["positive_sequence", "phase_a", "phase_b", "phase_c", "scalar"] = "positive_sequence",
        network_full_name: FullName | None = None,
        name_contains: Annotated[str, Field(max_length=120)] = "",
        offset: Annotated[int, Field(ge=0, le=1000000, strict=True)] = 0,
        limit: Annotated[int, Field(ge=1, le=50, strict=True)] = 20,
    ) -> dict[str, Any]:
        """Read native voltage and outserv records on all selected ElmTerm objects.

        quantity selects m:u1 (positive_sequence), m:u:A/B/C (phase_a/b/c) or
        m:u (scalar). Returns original values and units, metadata/errors, native
        IsEnergized values and validity flags, including out-of-service terminals.
        Readable values remain visible when validity is false or unknown.
        No unit conversion, limit check, deviation or ranking is performed.
        Network/name filters precede pagination by exact full path.
        """
        return call("get_bus_voltage", {"quantity": quantity, "network_full_name": network_full_name,
            "name_contains": name_contains, "offset": offset, "limit": limit})

    @server.tool(title="Read native transformer loading", annotations=annotations)
    def get_transformer_loading(
        network_full_name: FullName | None = None,
        name_contains: Annotated[str, Field(max_length=120)] = "",
        offset: Annotated[int, Field(ge=0, le=1000000, strict=True)] = 0,
        limit: Annotated[int, Field(ge=1, le=50, strict=True)] = 20,
    ) -> dict[str, Any]:
        """Read native outserv/c:loading records on selected ElmTr2/ElmTr3 objects.

        Returns original values, units, metadata/errors and native validity flags,
        including out-of-service transformers. Readable values remain visible
        when validity is false or unknown. No loading threshold, ranking,
        inferred winding quantities or calculation is performed. Network/name
        filters precede pagination by exact full path.
        """
        return call("get_transformer_loading", {"network_full_name": network_full_name,
            "name_contains": name_contains, "offset": offset, "limit": limit})

    @server.tool(title="Export selected PowerFactory results to CSV or JSON", annotations=calculation_annotations)
    def export_results(
        file_name: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}\.(csv|json)$")],
        full_names: Annotated[list[FullName], Field(min_length=1, max_length=8)],
        result_attributes: AttributeList,
        calculation: Literal["power_flow", "short_circuit"] = "power_flow",
        calculation_request_id: CalculationRequestId | None = None,
    ) -> dict[str, Any]:
        """Write explicit selected result attributes to a new workspace artifact.

        file_name is a simple .csv/.json basename under this MCP server's exports
        folder; paths and overwrites are rejected. Select 1-8 unique exact model
        element paths and 1-100 unique direct m:/c:/s: attributes. The artifact
        limit is 8 MiB. This first version exports selected attributes, not ElmRes
        time series or a whole-network table. No native export command is edited.

        Reads use snapshot capture's units/status/context/validity checks but
        store no snapshot. An optional calculation_request_id uses only values
        already stored in that attempt; missing selections are errors. Otherwise
        values are read live. No calculation runs. Invalid/unavailable data is
        exported as explicit errors, not zeros. Check export_complete and the
        artifact's statuses. CSV includes value_json, unit, status, errors and
        context_json; JSON preserves the complete structured selection.

        Writes occur on the MCP server host. The returned PF execution metadata
        remains read-only; external_execution reports the separate file write.
        """
        response = call("export_results", {
            "full_names": full_names, "result_attributes": result_attributes,
            "calculation": calculation, "calculation_request_id": calculation_request_id,
        })
        artifact = write_result_export(response["data"], file_name)
        return {**response, "data": artifact,
                "external_execution": {"source": "MCP server filesystem", "read_only": False, "effect": "new_file"}}

    @server.tool(title="Capture a PowerFactory result snapshot", annotations=calculation_annotations)
    def capture_result_snapshot(
        snapshot_id: SnapshotId,
        full_names: Annotated[list[FullName], Field(min_length=1, max_length=8)],
        result_attributes: AttributeList,
        calculation: Literal["power_flow", "short_circuit"] = "power_flow",
        calculation_request_id: CalculationRequestId | None = None,
    ) -> dict[str, Any]:
        """Capture immutable plain result data in this agent's memory; no calculation.

        Select 1-8 unique exact listed element paths and explicit m:/c:/s: names.
        A new snapshot_id is required; existing IDs cannot be overwritten.
        At most 16 snapshots of 512 KiB each persist until the agent stops.
        This tool changes only agent memory, not PowerFactory parameters.

        By default, read existing results with before/after validity checks for
        the selected calculation kind. Invalid results prevent reads or discard
        captured values; unknown validity makes values provisional. Inspect
        snapshot_complete, per-item/attribute errors and metadata errors.
        Supplying calculation_request_id copies ONLY the selected results that
        were stored in that historical attempt, with its context/validity. It
        never attaches that ID to a new live read. Missing recorded selections
        are errors. Snapshot context is not a complete model revision.
        """
        return call("capture_result_snapshot", {
            "snapshot_id": snapshot_id, "full_names": full_names, "result_attributes": result_attributes,
            "calculation": calculation, "calculation_request_id": calculation_request_id,
        })

    @server.tool(title="Retrieve a stored PowerFactory result snapshot", annotations=annotations)
    def get_result_snapshot(snapshot_id: SnapshotId) -> dict[str, Any]:
        """Return one immutable native-data snapshot from this agent session.

        Returns stored values, original units, context, capture time and historical
        validity/read metadata. No new element/result reads, delta, comparison or
        unit conversion is performed. Snapshots disappear when the ComPython
        agent stops. Unknown IDs return SNAPSHOT_NOT_FOUND.
        """
        return call("get_result_snapshot", {"snapshot_id": snapshot_id})

    @server.tool(title="Read PowerFactory element attributes", annotations=annotations)
    def get_element_attributes(
        full_name: FullName,
        attributes: AttributeList | None = None,
        profile: Profile = "basic",
        include_results: Annotated[bool, Field(strict=True)] = False,
        calculation: Calculation = "all",
    ) -> dict[str, Any]:
        """Read parameters and optional native results of one calculation-relevant element.

        Copy full_name exactly from list_model_elements; names alone are not unique.
        Omit attributes for defaults: ElmTerm reads loc_name, uknom, outserv;
        ElmLne reads loc_name, dline, outserv, typ_id. ElmTr2 adds parallel count,
        tap/control/rating factor, and HV/LV cubicle references; ElmTr3 adds
        parallel count, HV/MV/LV tap positions and cubicle references. ElmRelay
        reads loc_name, outserv, typ_id and actset. RelToc/RelIoc add pickup,
        direction and time settings (Tpset/Tset); RelToc also reads pcharac.
        Other classes have compact class-specific defaults. profile=extended checks
        the broader parameter candidates with HasAttribute, reads the available
        ones, and reports excluded names and availability-check errors.
        With attributes omitted, include_results=true adds calculation defaults
        only when the native validity flag is true. calculation selects all,
        power_flow or short_circuit. Invalid/unknown state and absent profiles are
        explicit skips in result_selection; inspect its before/after state.
        For ElmRelay, valid short-circuit defaults include c:yout (tripping time)
        where available. RelToc/RelIoc use s:yout and available scalar/phase
        s:Iabs and c:Ipabs current results; missing variants are explicit exclusions.
        rms requires an ElmRes time-series reader and is rejected here.
        Custom lists contain 1-100 unique direct names. Existing m:/c:/s: results
        may be read explicitly; they can be unavailable or stale. Reference
        chains, indexed expressions, arbitrary methods and calculations are excluded.

        Each attribute has status=ok with value, unit and description, or
        status=error with a reason. Units come from current PowerFactory settings;
        null means metadata could not be read. Object references such as typ_id
        become name/class/full_name metadata with kind=object_reference.
        No calculation is run and no parameter is changed.
        """
        return call("get_element_attributes", {"full_name": full_name, "attributes": attributes, "profile": profile,
            "include_results": include_results, "calculation": calculation})

    @server.tool(title="Read PowerFactory equipment type attributes", annotations=annotations)
    def get_equipment_type_attributes(
        full_name: FullName,
        attributes: AttributeList | None = None,
        profile: Profile = "basic",
    ) -> dict[str, Any]:
        """Read the type currently assigned to a listed model element through typ_id.

        full_name is the SOURCE element path copied from
        list_model_elements, not the equipment-type library path. The source is
        resolved again in the current case, then exactly one typ_id hop is read.

        Omit attributes for defaults: TypLne returns rated voltage/current,
        positive/zero-sequence R, X, C and phase/neutral counts. TypTr2 returns
        ratings, voltages, impedance-test/loss data, winding connections/clock
        number and tap-range parameters. TypTr3 returns HV/MV/LV ratings and
        voltages, three impedance-test/loss entries and winding connections/clock
        numbers. TypRelay returns the name, manufacturer, relay-definition
        reference and category; extended adds version, function flags and block
        references. Definition/block references remain metadata without recursion.
        RelToc and RelIoc sources resolve strictly to TypToc and TypIoc. Their
        basic type profiles read IEC/ANSI symbols, current unit, model identifier,
        Ts, Kr and Tr; extended adds ranges, reset/blocking options and references.
        Other allowed line types (TypTow/TypGeo/TypCabsys) default to
        loc_name only with basic profiles. Extended profiles also cover machine,
        load, relay, tower and cable-system type candidates, checked with HasAttribute.
        A listed element without typ_id or an assigned type returns an explicit
        error. Line assignments may reference ElmTow/ElmCabsys coupling objects;
        these return reference_kind=coupling_element and are read one hop only.

        Custom attributes are 1-100 unique direct names. Each has status,
        value, raw unit metadata and description, or an explicit attribute error.
        Units are not converted; blank/reference metadata is not a physical unit.
        No automatic recursion, type creation, calculations, or parameter edits.
        """
        return call("get_equipment_type_attributes", {"full_name": full_name, "attributes": attributes, "profile": profile})

    @server.tool(title="Inventory PowerFactory model classes", annotations=annotations)
    def get_model_inventory(
        element_classes: Annotated[list[ElementClass], Field(min_length=1, max_length=len(_profiles["elements"]))] | None = None,
        sample_limit: Annotated[int, Field(ge=0, le=3, strict=True)] = 1,
    ) -> dict[str, Any]:
        """Read class counts and exact-path samples in the live calculation-relevant model.

        Omit element_classes to query all 24 supported classes. Empty classes and
        individual class-query errors are reported explicitly. sample_limit=0
        returns counts only. This does not activate grids or search inactive projects.
        """
        return call("get_model_inventory", {"element_classes": element_classes, "sample_limit": sample_limit})

    @server.tool(title="Discover available candidate attributes", annotations=annotations)
    def list_element_attributes(
        full_name: FullName,
        target: Literal["element", "equipment_type"] = "element",
        candidate_attributes: AttributeList | None = None,
        include_results: Annotated[bool, Field(strict=True)] = False,
        calculation: Calculation = "all",
        include_unavailable: Annotated[bool, Field(strict=True)] = False,
        name_contains: Annotated[str, Field(max_length=120)] = "",
        offset: Annotated[int, Field(ge=0, le=1000000, strict=True)] = 0,
        limit: Annotated[int, Field(ge=1, le=100, strict=True)] = 100,
    ) -> dict[str, Any]:
        """Check candidate names with HasAttribute and return paginated availability/metadata.

        Pass the SOURCE element full_name for either target. The equipment_type
        target follows its live typ_id once. No cataloged attribute values are read;
        the source typ_id reference is read to resolve the equipment-type target.
        Built-in extended profiles plus optional candidate_attributes supply names;
        coverage_complete=false because this is not exhaustive API enumeration.
        include_results adds calculation-specific candidates from pf_profiles.json.
        calculation filters all/power_flow/short_circuit/rms. Native validity gates
        result availability separately from attribute_exists. include_unavailable
        exposes results whose calculation is invalid and names missing on this object.
        Unknown availability and metadata failures are explicit. Follow next_offset.
        """
        return call("list_element_attributes", {
            "full_name": full_name, "target": target, "candidate_attributes": candidate_attributes,
            "include_results": include_results, "calculation": calculation, "include_unavailable": include_unavailable,
            "name_contains": name_contains, "offset": offset, "limit": limit,
        })

    @server.tool(title="Read several PowerFactory elements", annotations=annotations)
    def get_elements_attributes(
        full_names: Annotated[list[FullName], Field(min_length=1, max_length=8)],
        attributes: AttributeList | None = None,
        profile: Profile = "extended",
        include_equipment_types: Annotated[bool, Field(strict=True)] = False,
        include_results: Annotated[bool, Field(strict=True)] = False,
        calculation: Calculation = "all",
    ) -> dict[str, Any]:
        """Read 1-8 exact listed element paths in one request, with independent item statuses.

        Omit attributes for each element's selected profile. Custom attributes apply
        to all elements in the batch. With attributes omitted, include_results=true
        adds valid power_flow/short_circuit defaults per class; calculation filters
        them. Inspect each result_selection and native validity. RMS time-series
        reads require ElmRes and are not implemented. With include_equipment_types, each assigned
        type is read using its own profile; missing/unassigned references are
        explicit skips. Per-attribute, per-item and aggregate response budgets give
        explicit errors rather than silent truncation. Large values can be read
        separately. Class resolution is cached only within this request, all calls
        stay on the ComPython thread, and no calculation or model edit is performed.
        """
        return call("get_elements_attributes", {
            "full_names": full_names, "attributes": attributes, "profile": profile,
            "include_equipment_types": include_equipment_types,
            "include_results": include_results, "calculation": calculation,
        })

    @server.tool(title="List existing PowerFactory calculation commands", annotations=annotations)
    def list_calculation_commands(
        calculation: Literal["all", "power_flow", "short_circuit"] = "all",
        offset: Annotated[int, Field(ge=0, le=1000000, strict=True)] = 0,
        limit: Annotated[int, Field(ge=1, le=20, strict=True)] = 20,
        settings_attributes: AttributeList | None = None,
    ) -> dict[str, Any]:
        """Inspect existing ComLdf/ComShc objects in the active study case, without executing.

        Returns exact command and study-case paths and selected raw command settings.
        Settings include available mode/control or fault-method/type/location fields;
        this is not a complete GUI-option catalog. Supply direct settings_attributes
        to inspect other known names. Follow next_offset. No command is created:
        if empty, configure/save the calculation manually in PowerFactory first.
        """
        return call("list_calculation_commands", {"calculation": calculation,
                    "offset": offset, "limit": limit, "settings_attributes": settings_attributes})

    @server.tool(title="Execute an existing PowerFactory load flow", annotations=calculation_annotations)
    def run_power_flow(
        command_full_name: FullName,
        expected_study_case_full_name: FullName,
        expected_agent_instance_id: Annotated[str, Field(pattern=r"^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$")],
        request_id: CalculationRequestId,
        result_full_names: ResultPaths | None = None,
        result_attributes: AttributeList | None = None,
    ) -> dict[str, Any]:
        """Execute the exact existing ComLdf once, with its current GUI settings.

        Call only when the user requests a calculation. Copy both paths from
        list_calculation_commands; the active case must match exactly. Copy the
        expected_agent_instance_id from the session envelope; a restart rejects
        the old attempt. Supply a
        request_id before execution; identical repeats replay the stored attempt
        within this agent session. No settings are assigned, commands created,
        scenarios activated or database saves performed. PowerFactory calculation
        results and automatic controls may change. A failed/invalid calculation
        returns success=false and no post-calculation result values.
        outcome separately reports execution, validity and result_quality,
        including not_requested, not_read, unavailable and partial reads.

        Optional result_full_names selects up to eight exact model element paths,
        resolved before Execute. Defaults use the calculation-specific JSON
        candidates, falling back to each class's existing JSON results list.
        Unavailable defaults are excluded and listed; unknown availability and
        read/metadata failures remain explicit. Explicit
        result_attributes contains direct m:/c:/s: names only, applying to
        all samples. Inspect each result/attribute separately. Validity is checked
        when available; no independent engineering validation is claimed.
        On timeout, query get_calculation_status with this ID; do not retry with
        a new ID or automatically restart, because Execute is not cancelled.
        """
        return call("run_power_flow", {"command_full_name": command_full_name,
            "expected_study_case_full_name": expected_study_case_full_name, "request_id": request_id,
            "expected_agent_instance_id": expected_agent_instance_id,
            "result_full_names": result_full_names, "result_attributes": result_attributes})

    @server.tool(title="Execute an existing PowerFactory short circuit", annotations=calculation_annotations)
    def run_short_circuit(
        command_full_name: FullName,
        expected_study_case_full_name: FullName,
        expected_agent_instance_id: Annotated[str, Field(pattern=r"^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$")],
        request_id: CalculationRequestId,
        result_full_names: ResultPaths | None = None,
        result_attributes: AttributeList | None = None,
    ) -> dict[str, Any]:
        """Execute the exact existing ComShc once, with its current GUI fault settings.

        Inspect list_calculation_commands first and run only when requested.
        The exact active study-case path, expected_agent_instance_id from the
        session envelope, and request_id are required. Method,
        fault type/location, impedance and other options remain those configured
        in PowerFactory; this tool does not set a new fault location/type or run
        a fault sweep. result_full_names selects reads, not fault targets.

        On return code zero and no false validity flag, defaults use short_circuit
        class defaults from pf_profiles.json for the requested calculation.
        Without that file, built-in defaults cover only ElmTerm buses:
        m:Ikss, m:Skss, m:ip and m:Ith. Other classes, including ElmLne lines,
        require a profile in that file or explicit result_attributes.
        Unavailable defaults are listed
        as excluded; failed availability checks, read errors and metadata failures
        remain explicit. Empty or wholly unavailable profiles report a skipped
        sample. Explicit result_attributes is strict and is never filtered.
        Eight samples and 100
        direct result names are supported. Raw units/errors remain explicit.
        Nonzero return, invalid results or Execute exception prevent result reads.
        outcome separates execution, validity and result_quality; inspect all
        three rather than treating success alone as complete result coverage.
        A timeout does not cancel execution. Query get_calculation_status using
        the same ID; do not automatically rerun with a new ID or after a restart.
        """
        return call("run_short_circuit", {"command_full_name": command_full_name,
            "expected_study_case_full_name": expected_study_case_full_name, "request_id": request_id,
            "expected_agent_instance_id": expected_agent_instance_id,
            "result_full_names": result_full_names, "result_attributes": result_attributes})

    @server.tool(title="Read a stored PowerFactory calculation attempt", annotations=annotations)
    def get_calculation_status(request_id: CalculationRequestId) -> dict[str, Any]:
        """Read the stored attempt/results for this ID in the current ComPython session.

        This never executes a calculation. Calls wait behind an ongoing synchronous
        PF operation. Records persist only until the agent stops, with at most 32
        attempts per session. A stored result is historical, not a freshness check
        after subsequent calculations or model edits. Missing records after a
        restart do not establish that a previous calculation was never executed.
        record.outcome uses the stored evidence; it does not recheck historical
        validity against the current model.
        """
        return call("get_calculation_status", {"request_id": request_id})

    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connection", type=Path,
                        default=default_connection_path())
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument("--calculation-timeout", type=float, default=120)
    args = parser.parse_args()
    if not 0 < args.timeout <= 30:
        parser.error("timeout must be greater than 0 and at most 30 seconds.")
    if not 0 < args.calculation_timeout <= 600:
        parser.error("calculation-timeout must be greater than 0 and at most 600 seconds.")
    # Tool discovery works even before PowerFactory's agent is started.
    # stdout is exclusively the MCP protocol; diagnostics go to stderr.
    build_server(args.connection, args.timeout, args.calculation_timeout).run(transport="stdio")


if __name__ == "__main__":
    main()
