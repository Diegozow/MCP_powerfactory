# -*- coding: utf-8 -*-
"""PowerFactory model inspection, validation and calculation operations."""

import hashlib
import json
import math
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


ATTRIBUTE_PATTERN = r"(?:[A-Za-z][A-Za-z0-9_]{0,63}|[mcs]:[A-Za-z][A-Za-z0-9_]*(?::[A-Za-z][A-Za-z0-9_]*){0,2})"
MAX_ATTRIBUTES = 100


def load_profiles(path):
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != 2:
        raise ValueError("pf_profiles.json must have schema_version=2.")
    if not isinstance(data.get("coverage"), str) or not data["coverage"]:
        raise ValueError("Missing profile coverage description.")
    calculations = data.get("calculations")
    expected = {"power_flow": ("ComLdf", "IsLdfValid", "element_attributes"),
                "short_circuit": ("ComShc", "IsShcValid", "element_attributes"),
                "rms": ("ComSim", "IsRmsValid", "ElmRes")}
    if not isinstance(calculations, dict) or set(calculations) != set(expected):
        raise ValueError("Profiles must describe power_flow, short_circuit and rms.")
    for kind, values in expected.items():
        profile = calculations[kind]
        if not isinstance(profile, dict) or tuple(profile.get(key) for key in (
                "command_class", "validity_method", "result_source")) != values:
            raise ValueError("Invalid native calculation metadata for " + kind)

    def valid_names(names, results):
        return (isinstance(names, list) and len(names) <= MAX_ATTRIBUTES
                and all(isinstance(name, str) and len(name) <= 64 and re.fullmatch(ATTRIBUTE_PATTERN, name)
                        and name.startswith(("m:", "c:", "s:")) == results for name in names)
                and len(set(names)) == len(names))

    for group, prefix in (("elements", "(?:Elm|Rel)"), ("types", "Typ")):
        profiles = data.get(group)
        if not isinstance(profiles, dict) or (group == "elements" and not profiles):
            raise ValueError("Missing class profiles: " + group)
        for class_name, profile in profiles.items():
            if not isinstance(class_name, str) or re.fullmatch(prefix + r"[A-Za-z0-9_]+", class_name) is None or not isinstance(profile, dict):
                raise ValueError("Invalid class profile in " + group)
            parameters = profile.get("parameters")
            if (not isinstance(profile.get("label"), str) or not isinstance(parameters, dict)
                    or any(not valid_names(parameters.get(key), False) for key in ("basic", "extended"))
                    or not set(parameters["basic"]) <= set(parameters["extended"])):
                raise ValueError("Invalid parameter profiles for " + class_name)
            results = profile.get("results")
            if not isinstance(results, dict) or set(results) - set(calculations):
                raise ValueError("Invalid calculation result profiles for " + class_name)
            for kind, selection in results.items():
                if (not isinstance(selection, dict)
                        or any(not valid_names(selection.get(key), True) for key in ("attributes", "defaults"))
                        or not set(selection["defaults"]) <= set(selection["attributes"])):
                    raise ValueError("Invalid " + kind + " result names for " + class_name)
    return data


PROFILE_DATA = load_profiles(Path(__file__).with_name("pf_profiles.json"))
ELEMENT_PROFILES = PROFILE_DATA["elements"]
TYPE_PROFILES = PROFILE_DATA["types"]
CALCULATION_PROFILES = PROFILE_DATA["calculations"]
ELEMENT_CLASSES = tuple(ELEMENT_PROFILES)
ATTRIBUTE_RESPONSE_BUDGET = 524288
DEFAULT_ATTRIBUTES = {name: tuple(profile["parameters"]["basic"]) for name, profile in ELEMENT_PROFILES.items()}
BASE_ATTRIBUTES = ("loc_name", "outserv")
EQUIPMENT_TYPE_CLASSES = {
    "ElmLne": ("TypLne", "TypTow", "TypGeo", "TypCabsys", "ElmTow", "ElmCabsys"),
    "ElmLnesec": ("TypLne", "TypTow", "TypGeo", "TypCabsys"),
    "ElmTr2": ("TypTr2",),
    "ElmTr3": ("TypTr3",),
    "ElmSym": ("TypSym",),
    "ElmAsm": ("TypAsm", "TypAsmo"),
    "ElmLod": ("TypLod",),
    "ElmRelay": ("TypRelay",),
    "RelToc": ("TypToc",),
    "RelIoc": ("TypIoc",),
    "ElmTow": ("TypTow", "TypGeo"),
    "ElmCabsys": ("TypCabsys", "TypCab"),
}
TYPE_DEFAULT_ATTRIBUTES = {name: tuple(profile["parameters"]["basic"]) for name, profile in TYPE_PROFILES.items()}
CALCULATION_CLASSES = {"power_flow": "ComLdf", "short_circuit": "ComShc"}
CALCULATION_TOOLS = {"run_power_flow": "power_flow", "run_short_circuit": "short_circuit"}
CALCULATION_SETTINGS = {
    "ComLdf": ["iopt_net", "iopt_at", "iopt_asht", "iopt_lim", "iopt_tem", "iopt_pq", "iopt_fls"],
    "ComShc": ["iopt_mde", "iopt_shc", "iopt_allbus", "shcobj", "Rf", "Xf", "iopt_cur", "tk"],
}
REQUEST_ID_PATTERN = r"[A-Za-z0-9][A-Za-z0-9_-]{7,79}"
SNAPSHOT_ID_PATTERN = r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}"
VOLTAGE_QUANTITIES = {"positive_sequence": "m:u1", "phase_a": "m:u:A", "phase_b": "m:u:B", "phase_c": "m:u:C", "scalar": "m:u"}
SHORT_CIRCUIT_FAULT_TYPES = ("3psc", "2psc", "spgf", "2pgf")
SHORT_CIRCUIT_FAULT_SETTINGS = ("iopt_mde", "iopt_shc", "iopt_allbus", "shcobj", "Rf", "Xf", "iopt_cur",
                                "iopt_cnf", "iEnhancedZf", "i_p2psc", "i_pspgf", "i_p2pgf", "Tk", "Ta")
OPERATING_SETPOINTS = {"ElmLod": ("plini", "qlini"), "ElmSym": ("pgini", "qgini"), "ElmGenstat": ("pgini", "qgini")}
SETPOINT_UNITS = {"plini": ("W", "kW", "MW"), "pgini": ("W", "kW", "MW"),
                  "qlini": ("var", "kvar", "Mvar"), "qgini": ("var", "kvar", "Mvar")}
SETPOINT_CONTROLS = ("mode_inp", "av_mode", "ip_ctrl", "c_pstac", "c_psecc", "c_pmod", "scale0", "i_scale", "ngnum")
IMPLEMENTATION_VERSION = "0.20.0"
SUPPORTED_TOOLS = (
    "get_active_session", "get_active_scenario", "list_operation_scenarios", "browse_powerfactory_tree", "activate_study_case",
    "list_network_variations", "activate_network_variation", "deactivate_network_variation",
    "list_model_elements", "get_line_loading", "capture_result_snapshot", "get_result_snapshot",
    "get_element_attributes", "get_equipment_type_attributes", "get_model_inventory", "list_element_attributes",
    "get_elements_attributes", "list_calculation_commands", "run_power_flow", "run_short_circuit",
    "get_calculation_status", "activate_operation_scenario", "get_bus_voltage",
    "get_transformer_loading", "export_results", "get_powerfactory_capabilities",
    "set_study_time", "get_power_balance", "get_bus_voltage_sequences", "get_transformer_control_status",
    "list_expansion_stages",
    "get_element_connections",
    "get_short_circuit_contributions",
    "run_short_circuit_at_bus",
    "set_operating_setpoints",
    "get_protection_chain",
    "get_graphic_selection", "mark_in_graphics",
    "get_feeder_inventory", "export_graphic",
    "save_operation_scenario", "create_operation_scenario",
)
SOURCE_FILES = ("pf_agent.py", "pf_model.py", "pf_connection.py", "pf_profiles.json")
CONNECTION_SIDES = {"ElmLne": ("bus1", "bus2"), "ElmTr2": ("bushv", "buslv"),
                    "ElmTr3": ("bushv", "busmv", "buslv"), "ElmCoup": ("bus1", "bus2"),
                    "ElmSym": ("bus1",), "ElmXnet": ("bus1",),
                    "ElmGenstat": ("bus1",), "ElmAsm": ("bus1",)}
LOADED_SOURCES = {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                  if Path(__file__).with_name(name).exists() else None for name in SOURCE_FILES}


def result_candidates(kind, element_class, defaults=True):
    key = "defaults" if defaults else "attributes"
    profile = (ELEMENT_PROFILES.get(element_class) or TYPE_PROFILES.get(element_class) or {}).get("results", {}).get(kind, {})
    return list(profile.get(key, [])), "pf_profiles.json:" + element_class + ".results." + kind + "." + key


class RequestError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code = code
        self.status = status


def describe(obj):
    if obj is None:
        return None
    return {
        "name": str(obj.loc_name),
        "class": str(obj.GetClassName()),
        "full_name": str(obj.GetFullName()),
    }


def session_info(app):
    info = {
        "project": describe(app.GetActiveProject()),
        "study_case": describe(app.GetActiveStudyCase()),
    }
    if info["project"] is None or info["study_case"] is None:
        raise RequestError("NO_ACTIVE_SESSION", "Activate a project and study case.", 409)
    return info


def active_context(app, info=None):
    info = session_info(app) if info is None else info

    def read_context():
        values, errors = {}, {}
        for name, method in (("scenario", "GetActiveScenario"), ("active_variations", "GetActiveNetworkVariations"),
                             ("recording_stage", "GetRecordingStage"), ("study_time", "GetStudyTimeObject")):
            try:
                obj = getattr(app, method)()
                if name == "active_variations":
                    if not isinstance(obj, (list, tuple)) or any(item is None for item in obj):
                        raise ValueError("Expected an active-variation list.")
                    value = sorted((describe(item) for item in obj), key=lambda item: item["full_name"])
                elif name == "study_time":
                    if obj is None:
                        raise ValueError("No study-time object is available.")
                    value = {"object": describe(obj), "cDate": read_attribute(obj, "cDate"), "cTime": read_attribute(obj, "cTime"),
                             "utc_seconds": {**read_attribute(app.GetActiveStudyCase(), "iStudyTime"), "source": "IntCase.iStudyTime"},
                             "representation": "cDate/cTime are raw PowerFactory display fields; utc_seconds is the study case's native epoch seconds. No display timezone is inferred."}
                    if any(value[name]["status"] != "ok" or value[name].get("metadata_errors") for name in ("cDate", "cTime", "utc_seconds")):
                        errors[name] = {"code": "STUDY_TIME_INCOMPLETE", "message": "Check date/time status and metadata."}
                else:
                    value = describe(obj)
                values[name] = value
            except Exception as exc:
                values[name] = None
                errors[name] = {"code": "CONTEXT_READ_FAILED", "message": (type(exc).__name__ + ": " + str(exc))[:320]}
        return {**values, "context_complete": not errors, "context_errors": errors}

    context = read_context()
    if session_info(app) != info or read_context() != context:
        raise RequestError("SESSION_CHANGED", "The active configuration changed during the context read; query again.", 409)
    return {**info, **context}


def powerfactory_capabilities(app):
    methods = ("GetActiveScenario", "GetActiveNetworkVariations", "GetRecordingStage", "GetStudyTimeObject",
               "GetInstallationDirectory", "GetInterfaceVersion", "GetCalcRelevantObjects", "IsLdfValid", "IsShcValid", "IsRmsValid",
               "GetDesktop", "GetCurrentDiagram", "GetDiagramSelection", "MarkInGraphics")
    data = {"implementation_version": IMPLEMENTATION_VERSION, "supported_tools": list(SUPPORTED_TOOLS),
            "supported_element_classes": list(ELEMENT_CLASSES),
            "loaded_sources": LOADED_SOURCES, "python_version": sys.version.split()[0],
            "api_methods": {name: callable(getattr(app, name, None)) for name in methods},
            "license_status": "unknown", "license_note": "Method availability does not establish a calculation license.",
            "snapshot_limit": 16, "calculation_record_limit": 32,
            "profile_catalog": {"file": "pf_profiles.json", "schema_version": PROFILE_DATA["schema_version"],
                                "calculations": CALCULATION_PROFILES, "executable_calculations": list(CALCULATION_CLASSES)}}
    for key, method in (("installation_directory", "GetInstallationDirectory"), ("interface_version", "GetInterfaceVersion")):
        try:
            data[key] = {"status": "ok", "value": json_attribute_value(getattr(app, method)())}
        except Exception as exc:
            data[key] = {"status": "unknown", "error": (type(exc).__name__ + ": " + str(exc))[:320]}
    try:
        import ctypes
        installation = data["installation_directory"]
        if installation["status"] != "ok":
            raise ValueError("Installation directory unavailable.")
        executable = Path(installation["value"]) / "PowerFactory.exe"
        version = ctypes.windll.version
        filename = ctypes.c_wchar_p(str(executable))
        size = version.GetFileVersionInfoSizeW(filename, None)
        if not size:
            raise OSError("Executable version information unavailable.")
        buffer = ctypes.create_string_buffer(size)
        pointer, length = ctypes.c_void_p(), ctypes.c_uint()
        if not version.GetFileVersionInfoW(filename, 0, size, buffer) or not version.VerQueryValueW(buffer, ctypes.c_wchar_p("\\"), ctypes.byref(pointer), ctypes.byref(length)):
            raise OSError("Executable version query failed.")
        words = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32 * 13)).contents
        data["powerfactory_product_version"] = {"status": "ok", "value": ".".join(map(str, (words[4] >> 16, words[4] & 65535, words[5] >> 16, words[5] & 65535))),
                                              "source": str(executable) + " Windows product-version resource"}
    except Exception as exc:
        data["powerfactory_product_version"] = {"status": "unknown", "error": (type(exc).__name__ + ": " + str(exc))[:320]}
    return data


def validate_path(full_name):
    if (not isinstance(full_name, str) or not 1 <= len(full_name) <= 2048
            or not full_name.startswith("\\") or any(ord(char) < 32 for char in full_name)):
        raise RequestError("INVALID_ARGUMENTS", "Use an exact listed full_name (maximum 2048 characters).")
    element_class = full_name.rsplit(".", 1)[-1]
    if element_class not in ELEMENT_CLASSES:
        raise RequestError("INVALID_ARGUMENTS", "The path must identify a supported model-element class, not a library type.")
    return {"full_name": full_name, "element_class": element_class}


def validate_names(names):
    if names is None:
        return None
    if (not isinstance(names, list) or not 1 <= len(names) <= MAX_ATTRIBUTES
            or any(not isinstance(name, str) or len(name) > 64 or re.fullmatch(ATTRIBUTE_PATTERN, name) is None for name in names)):
        raise RequestError("INVALID_ARGUMENTS", "Request 1-100 direct parameter names or m:/c:/s: result names, maximum 64 characters each. Reference chains and expressions are excluded.")
    if len(set(names)) != len(names):
        raise RequestError("INVALID_ARGUMENTS", "Attribute names must be unique.")
    return names


def validate_full_path(path, object_class):
    if (not isinstance(path, str) or not 1 <= len(path) <= 2048 or not path.startswith("\\")
            or any(ord(char) < 32 for char in path) or path.rsplit(".", 1)[-1] != object_class):
        raise RequestError("INVALID_ARGUMENTS", "Use an exact full_name ending in ." + object_class + ".")
    return path


def validate_request_id(value):
    if not isinstance(value, str) or re.fullmatch(REQUEST_ID_PATTERN, value) is None:
        raise RequestError("INVALID_ARGUMENTS", "request_id must contain 8-80 ASCII letters, digits, underscores or hyphens.")
    return value


def validate_page(arguments, max_limit=100):
    offset, limit = arguments.get("offset", 0), arguments.get("limit", max_limit)
    needle = arguments.get("name_contains", "")
    if type(offset) is not int or not 0 <= offset <= 1000000:
        raise RequestError("INVALID_ARGUMENTS", "offset must be an integer from 0 to 1000000.")
    if type(limit) is not int or not 1 <= limit <= max_limit:
        raise RequestError("INVALID_ARGUMENTS", "limit must be an integer from 1 to " + str(max_limit) + ".")
    if not isinstance(needle, str) or len(needle) > 120:
        raise RequestError("INVALID_ARGUMENTS", "name_contains must be a string of at most 120 characters.")
    return {"offset": offset, "limit": limit, "name_contains": needle}


def validate_arguments(tool, arguments):
    if not isinstance(tool, str):
        raise RequestError("INVALID_ARGUMENTS", "tool must be a string.")
    if not isinstance(arguments, dict):
        raise RequestError("INVALID_ARGUMENTS", "arguments must be a JSON object.")
    if tool == "set_operating_setpoints":
        required = {"full_name", "setpoints", "expected_study_case_full_name", "expected_agent_instance_id",
                    "expected_active_scenario_full_name", "expected_recording_stage_full_name"}
        if not required <= set(arguments) or set(arguments) - required - {"confirmation_digest"}:
            raise RequestError("INVALID_ARGUMENTS", "Provide the exact element, expected context and one or two P/Q setpoints; omit confirmation_digest for a preview.")
        path = validate_path(arguments["full_name"])
        if path["element_class"] not in OPERATING_SETPOINTS:
            raise RequestError("INVALID_ARGUMENTS", "Setpoints support only ElmLod, ElmSym and ElmGenstat.")
        case = validate_full_path(arguments["expected_study_case_full_name"], "IntCase")
        for key, cls in (("expected_active_scenario_full_name", "IntScenario"), ("expected_recording_stage_full_name", "IntSstage")):
            if arguments[key] is not None:
                validate_full_path(arguments[key], cls)
        for value in (path["full_name"], case, arguments["expected_active_scenario_full_name"], arguments["expected_recording_stage_full_name"]):
            if value is not None and any(part in ("", ".", "..") for part in value.split("\\")[1:]):
                raise RequestError("INVALID_ARGUMENTS", "Use exact returned paths without relative or empty segments.")
        instance = arguments["expected_agent_instance_id"]
        if not isinstance(instance, str) or re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", instance) is None:
            raise RequestError("INVALID_ARGUMENTS", "Copy expected_agent_instance_id from the active session.")
        changes = arguments["setpoints"]
        if not isinstance(changes, list) or not 1 <= len(changes) <= 2:
            raise RequestError("INVALID_ARGUMENTS", "Provide one or two setpoints for one exact element.")
        seen = set()
        for change in changes:
            if not isinstance(change, dict) or set(change) != {"attribute", "value", "expected_value", "unit"}:
                raise RequestError("INVALID_ARGUMENTS", "Each setpoint requires only attribute, value, expected_value and unit.")
            name = change["attribute"]
            if not isinstance(name, str) or name not in OPERATING_SETPOINTS[path["element_class"]] or name in seen:
                raise RequestError("INVALID_ARGUMENTS", "Use unique class-appropriate P/Q attributes only.")
            seen.add(name)
            if change["unit"] not in SETPOINT_UNITS[name]:
                raise RequestError("INVALID_ARGUMENTS", "Use an explicit active/reactive power unit appropriate to the attribute.")
            if any(type(change[key]) not in (int, float) or abs(change[key]) > sys.float_info.max or not math.isfinite(change[key]) for key in ("value", "expected_value")):
                raise RequestError("INVALID_ARGUMENTS", "Setpoint values must be finite numbers, excluding booleans and numeric strings.")
        digest = arguments.get("confirmation_digest")
        if digest is not None and (not isinstance(digest, str) or re.fullmatch(r"[a-f0-9]{64}", digest) is None):
            raise RequestError("INVALID_ARGUMENTS", "Copy the reviewed preview's exact confirmation_digest.")
        return {**arguments, "setpoints": sorted(changes, key=lambda item: item["attribute"]), "confirmation_digest": digest}
    result_options = {}
    if tool in ("get_element_attributes", "get_elements_attributes", "list_element_attributes"):
        include_results = arguments.get("include_results", False)
        calculation = arguments.get("calculation", "all")
        if (type(include_results) is not bool or not isinstance(calculation, str)
                or calculation not in ("all", *CALCULATION_PROFILES)):
            raise RequestError("INVALID_ARGUMENTS", "Use boolean include_results and calculation all/power_flow/short_circuit/rms.")
        if calculation != "all" and not include_results:
            raise RequestError("INVALID_ARGUMENTS", "Set include_results=true when selecting a calculation.")
        result_options = {"include_results": include_results, "calculation": calculation}
    if tool in ("save_operation_scenario", "create_operation_scenario"):
        required = {"expected_study_case_full_name", "expected_agent_instance_id", "expected_active_scenario_full_name"}
        if tool == "create_operation_scenario":
            required |= {"folder_full_name", "scenario_name"}
        if not required <= set(arguments) or set(arguments) - required - {"confirmation_digest"}:
            raise RequestError("INVALID_ARGUMENTS", "Provide the exact expected context and destination; omit confirmation_digest for a preview.")
        case = validate_full_path(arguments["expected_study_case_full_name"], "IntCase")
        scenario = arguments["expected_active_scenario_full_name"]
        if scenario is not None:
            validate_full_path(scenario, "IntScenario")
        elif tool == "save_operation_scenario":
            raise RequestError("INVALID_ARGUMENTS", "Saving requires an exact active IntScenario path.")
        instance = arguments["expected_agent_instance_id"]
        if not isinstance(instance, str) or re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", instance) is None:
            raise RequestError("INVALID_ARGUMENTS", "Copy expected_agent_instance_id from get_active_session.")
        digest = arguments.get("confirmation_digest")
        if digest is not None and (not isinstance(digest, str) or re.fullmatch(r"[a-f0-9]{64}", digest) is None):
            raise RequestError("INVALID_ARGUMENTS", "Copy the preview's exact confirmation_digest after approval.")
        paths = [case, scenario]
        if tool == "create_operation_scenario":
            folder = arguments["folder_full_name"]
            folder_class = folder.rsplit(".", 1)[-1] if isinstance(folder, str) else ""
            if folder_class not in ("IntFolder", "IntPrjfolder"):
                raise RequestError("INVALID_ARGUMENTS", "Select an existing operation-scenario root or IntFolder.")
            validate_full_path(folder, folder_class)
            paths.append(folder)
            name = arguments["scenario_name"]
            if (not isinstance(name, str) or not 1 <= len(name) <= 120 or name != name.strip() or name in (".", "..")
                    or any(ord(char) < 32 or char in "\\/*?" for char in name)):
                raise RequestError("INVALID_ARGUMENTS", "Use a new scenario name of 1-120 characters without path separators, wildcards or surrounding spaces.")
        if any(part in ("", ".", "..") for path in paths if path is not None for part in path.split("\\")[1:]):
            raise RequestError("INVALID_ARGUMENTS", "Use exact context/destination paths without relative or empty segments.")
        return {**arguments, "confirmation_digest": digest}
    if tool == "get_feeder_inventory":
        if set(arguments) - {"feeder_full_name", "include_nested", "object_class", "name_contains", "offset", "limit"}:
            raise RequestError("INVALID_ARGUMENTS", "Unsupported feeder-inventory argument.")
        path = arguments.get("feeder_full_name")
        if path is not None:
            validate_full_path(path, "ElmFeeder")
            if any(part in ("", ".", "..") for part in path.split("\\")[1:]):
                raise RequestError("INVALID_ARGUMENTS", "Use an exact feeder path without relative or empty segments.")
        nested, object_class = arguments.get("include_nested", False), arguments.get("object_class")
        if type(nested) is not bool or (object_class is not None and
                (not isinstance(object_class, str) or re.fullmatch(r"Elm[A-Za-z0-9_]{1,61}", object_class) is None)):
            raise RequestError("INVALID_ARGUMENTS", "Use boolean include_nested and an exact Elm class without wildcards.")
        if path is None and (nested or object_class is not None):
            raise RequestError("INVALID_ARGUMENTS", "Select a feeder before applying member-class or nested-feeder options.")
        return {**validate_page({"limit": 20, **arguments}, 50), "feeder_full_name": path,
                "include_nested": nested, "object_class": object_class}
    if tool == "get_graphic_selection":
        if set(arguments) - {"offset", "limit"}:
            raise RequestError("INVALID_ARGUMENTS", "Use only offset and limit for the current diagram selection.")
        return validate_page(arguments, 50)
    if tool == "mark_in_graphics":
        required = {"full_names", "expected_diagram_full_name", "expected_study_case_full_name", "expected_agent_instance_id"}
        if set(arguments) != required:
            raise RequestError("INVALID_ARGUMENTS", "Provide exact element paths, expected diagram, study case and agent ID.")
        paths = arguments["full_names"]
        if (not isinstance(paths, list) or not 1 <= len(paths) <= 8
                or any(not isinstance(path, str) for path in paths) or len(set(paths)) != len(paths)):
            raise RequestError("INVALID_ARGUMENTS", "Use 1-8 unique supported model-element paths.")
        paths = [validate_path(path)["full_name"] for path in paths]
        diagram = validate_full_path(arguments["expected_diagram_full_name"], "IntGrfnet")
        case = validate_full_path(arguments["expected_study_case_full_name"], "IntCase")
        if any(part in ("", ".", "..") for path in [*paths, diagram, case] for part in path.split("\\")[1:]) or any(char in path for path in [*paths, diagram, case] for char in "*?"):
            raise RequestError("INVALID_ARGUMENTS", "Use exact paths without wildcards, relative or empty segments.")
        instance = arguments["expected_agent_instance_id"]
        if not isinstance(instance, str) or re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", instance) is None:
            raise RequestError("INVALID_ARGUMENTS", "Copy the current expected_agent_instance_id exactly.")
        return {**arguments, "full_names": paths}
    if tool == "export_graphic":
        if set(arguments) - {"page_full_name", "command_full_name", "file_name"} or not {"page_full_name", "file_name"} <= set(arguments):
            raise RequestError("INVALID_ARGUMENTS", "Provide an exact graphic page and a new PNG, SVG or PDF filename.")
        page = arguments["page_full_name"]
        page_class = page.rsplit(".", 1)[-1] if isinstance(page, str) else ""
        if page_class not in ("SetDeskpage", "GrpPage"):
            raise RequestError("INVALID_ARGUMENTS", "Select an existing SetDeskpage diagram tab or GrpPage plot.")
        validate_full_path(page, page_class)
        command = arguments.get("command_full_name")
        if command is not None:
            validate_full_path(command, "ComWr")
        if any(part in ("", ".", "..") for path in (page, command) if path is not None for part in path.split("\\")[1:]):
            raise RequestError("INVALID_ARGUMENTS", "Use exact page/command paths without relative or empty segments.")
        filename = arguments["file_name"]
        if not isinstance(filename, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\.(png|svg|pdf)", filename) is None:
            raise RequestError("INVALID_ARGUMENTS", "Use a simple filename ending in .png, .svg or .pdf.")
        return {"page_full_name": page, "command_full_name": command, "file_name": filename}
    if tool == "set_study_time":
        if set(arguments) != {"timestamp_utc", "expected_study_time_seconds", "expected_study_case_full_name", "expected_agent_instance_id"}:
            raise RequestError("INVALID_ARGUMENTS", "Provide a zoned timestamp, expected prior epoch seconds, study case and agent ID.")
        stamp = arguments["timestamp_utc"]
        if not isinstance(stamp, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})", stamp) is None:
            raise RequestError("INVALID_ARGUMENTS", "timestamp_utc must include whole seconds and an explicit offset, e.g. 2026-11-02T12:00:00Z.")
        try:
            timestamp = datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(timezone.utc)
            seconds = int((timestamp - datetime(1970, 1, 1, tzinfo=timezone.utc)).total_seconds())
        except (ValueError, OverflowError) as exc:
            raise RequestError("INVALID_ARGUMENTS", "timestamp_utc is not a valid date/time.") from exc
        previous = arguments["expected_study_time_seconds"]
        if not 0 <= seconds <= 4294967295 or type(previous) is not int or not 0 <= previous <= 4294967295:
            raise RequestError("INVALID_ARGUMENTS", "Target and expected epoch seconds must be within 0–4294967295; fractional seconds are unsupported.")
        case_path = validate_full_path(arguments["expected_study_case_full_name"], "IntCase")
        instance = arguments["expected_agent_instance_id"]
        if not isinstance(instance, str) or re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", instance) is None:
            raise RequestError("INVALID_ARGUMENTS", "Copy the current expected_agent_instance_id exactly.")
        return {**arguments, "timestamp_utc": timestamp.isoformat(), "utc_seconds": seconds, "expected_study_case_full_name": case_path}
    if tool == "get_power_balance":
        if set(arguments) - {"network_full_name"}:
            raise RequestError("INVALID_ARGUMENTS", "Unsupported power-balance argument.")
        network = arguments.get("network_full_name")
        if network is not None:
            validate_full_path(network, "ElmNet")
        return {"network_full_name": network}
    if tool == "get_transformer_control_status":
        if set(arguments) != {"full_name"}:
            raise RequestError("INVALID_ARGUMENTS", "Provide one exact transformer full_name.")
        path = validate_path(arguments["full_name"])
        if path["element_class"] not in ("ElmTr2", "ElmTr3"):
            raise RequestError("INVALID_ARGUMENTS", "Select an ElmTr2 or ElmTr3 transformer.")
        return path
    if tool == "get_element_connections":
        if set(arguments) != {"full_name"}:
            raise RequestError("INVALID_ARGUMENTS", "Provide one exact line, transformer or switch full_name.")
        path = validate_path(arguments["full_name"])
        if path["element_class"] not in ("ElmLne", "ElmTr2", "ElmTr3", "ElmCoup"):
            raise RequestError("INVALID_ARGUMENTS", "Connections support ElmLne, ElmTr2, ElmTr3 and ElmCoup.")
        if any(part in ("", ".", "..") for part in path["full_name"].split("\\")[1:]):
            raise RequestError("INVALID_ARGUMENTS", "Use an exact listed path without relative or empty segments.")
        return path
    if tool == "get_protection_chain":
        if "full_name" not in arguments or set(arguments) - {"full_name", "offset", "limit"}:
            raise RequestError("INVALID_ARGUMENTS", "Provide one exact relay or cubicle full_name and optional offset/limit.")
        path = arguments["full_name"]
        cls = path.rsplit(".", 1)[-1] if isinstance(path, str) else ""
        if cls not in ("ElmRelay", "StaCubic"):
            raise RequestError("INVALID_ARGUMENTS", "Select an ElmRelay or StaCubic.")
        validate_full_path(path, cls)
        if any(part in ("", ".", "..") for part in path.split("\\")[1:]) or any(char in path for char in "*?"):
            raise RequestError("INVALID_ARGUMENTS", "Use an exact path without wildcards, relative or empty segments.")
        return {"full_name": path, "element_class": cls, **validate_page(arguments, 50)}
    if tool == "get_short_circuit_contributions":
        if "bus_full_name" not in arguments or set(arguments) - {"bus_full_name", "offset", "limit"}:
            raise RequestError("INVALID_ARGUMENTS", "Provide one exact bus_full_name and optional offset/limit.")
        path = validate_full_path(arguments["bus_full_name"], "ElmTerm")
        if any(part in ("", ".", "..") for part in path.split("\\")[1:]):
            raise RequestError("INVALID_ARGUMENTS", "Use an exact listed bus path without relative or empty segments.")
        return {"bus_full_name": path, **validate_page(arguments, 50)}
    if tool == "activate_operation_scenario":
        allowed = {"scenario_full_name", "expected_study_case_full_name", "expected_agent_instance_id",
                   "expected_active_scenario_full_name", "discard_unsaved_changes"}
        if set(arguments) - allowed or not allowed - {"discard_unsaved_changes"} <= set(arguments):
            raise RequestError("INVALID_ARGUMENTS", "Provide exact target/expected scenario paths, expected study case and agent ID.")
        paths = {}
        for name, object_class in (("scenario_full_name", "IntScenario"), ("expected_study_case_full_name", "IntCase"),
                                   ("expected_active_scenario_full_name", "IntScenario")):
            value = arguments[name]
            if value is None and name == "expected_active_scenario_full_name":
                paths[name] = None
                continue
            paths[name] = validate_full_path(value, object_class)
            if any(part in ("", ".", "..") for part in value.split("\\")[1:]):
                raise RequestError("INVALID_ARGUMENTS", "Use exact paths without relative or empty segments.")
        expected_instance = arguments["expected_agent_instance_id"]
        if not isinstance(expected_instance, str) or re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", expected_instance) is None:
            raise RequestError("INVALID_ARGUMENTS", "Copy the current expected_agent_instance_id exactly.")
        discard = arguments.get("discard_unsaved_changes", False)
        if type(discard) is not bool:
            raise RequestError("INVALID_ARGUMENTS", "discard_unsaved_changes must be a boolean.")
        return {**paths, "expected_agent_instance_id": expected_instance, "discard_unsaved_changes": discard}
    if tool == "export_results":
        if set(arguments) - {"full_names", "result_attributes", "calculation", "calculation_request_id"}:
            raise RequestError("INVALID_ARGUMENTS", "Unsupported result-export selection argument.")
        return validate_arguments("capture_result_snapshot", {"snapshot_id": "export_read", **arguments})
    if tool == "list_operation_scenarios":
        if set(arguments) - {"name_contains", "offset", "limit"}:
            raise RequestError("INVALID_ARGUMENTS", "Unsupported operation-scenario listing argument.")
        return validate_page({"limit": 20, **arguments}, 50)
    if tool == "list_network_variations":
        if set(arguments) - {"name_contains", "offset", "limit"}:
            raise RequestError("INVALID_ARGUMENTS", "Unsupported network-variation listing argument.")
        return validate_page({"limit": 20, **arguments}, 50)
    if tool == "list_expansion_stages":
        if set(arguments) - {"variation_full_name", "name_contains", "offset", "limit"} or "variation_full_name" not in arguments:
            raise RequestError("INVALID_ARGUMENTS", "Provide one exact variation path and optional name/pagination filters.")
        path = validate_full_path(arguments["variation_full_name"], "IntScheme")
        if any(part in ("", ".", "..") for part in path.split("\\")[1:]):
            raise RequestError("INVALID_ARGUMENTS", "Use an exact listed variation path without relative or empty segments.")
        return {**validate_page({"limit": 20, **arguments}, 50), "variation_full_name": path}
    if tool in ("activate_network_variation", "deactivate_network_variation"):
        if set(arguments) != {"variation_full_name", "expected_study_case_full_name", "expected_agent_instance_id"}:
            raise RequestError("INVALID_ARGUMENTS", "Provide the exact variation path, expected study-case path and current agent ID.")
        path = validate_full_path(arguments["variation_full_name"], "IntScheme")
        case_path = validate_full_path(arguments["expected_study_case_full_name"], "IntCase")
        if any(part in ("", ".", "..") for value in (path, case_path) for part in value.split("\\")[1:]):
            raise RequestError("INVALID_ARGUMENTS", "Use exact returned paths without relative or empty segments.")
        expected_instance = arguments["expected_agent_instance_id"]
        if not isinstance(expected_instance, str) or re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", expected_instance) is None:
            raise RequestError("INVALID_ARGUMENTS", "Copy expected_agent_instance_id exactly from the current session response.")
        return {"variation_full_name": path, "expected_study_case_full_name": case_path,
                "expected_agent_instance_id": expected_instance}
    if tool == "activate_study_case":
        if set(arguments) != {"study_case_full_name", "expected_project_full_name", "expected_agent_instance_id"}:
            raise RequestError("INVALID_ARGUMENTS", "Provide the exact study-case path, expected project path and current agent ID.")
        case_path = validate_full_path(arguments["study_case_full_name"], "IntCase")
        project_path = validate_full_path(arguments["expected_project_full_name"], "IntPrj")
        if any(part in ("", ".", "..") for path in (case_path, project_path) for part in path.split("\\")[1:]):
            raise RequestError("INVALID_ARGUMENTS", "Use exact returned paths without relative or empty segments.")
        expected_instance = arguments["expected_agent_instance_id"]
        if not isinstance(expected_instance, str) or re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", expected_instance) is None:
            raise RequestError("INVALID_ARGUMENTS", "Copy expected_agent_instance_id exactly from the current session response.")
        return {"study_case_full_name": case_path, "expected_project_full_name": project_path,
                "expected_agent_instance_id": expected_instance}
    if tool == "browse_powerfactory_tree":
        if set(arguments) - {"full_name", "direction", "object_class", "name_contains", "offset", "limit"}:
            raise RequestError("INVALID_ARGUMENTS", "Unsupported tree-navigation argument.")
        full_name = arguments.get("full_name")
        if full_name is not None and (not isinstance(full_name, str) or not 1 <= len(full_name) <= 2048
                or not full_name.startswith("\\") or any(ord(char) < 32 for char in full_name)
                or any(part in ("", ".", "..") for part in full_name.split("\\")[1:])):
            raise RequestError("INVALID_ARGUMENTS", "Use an exact returned full_name, without relative path segments or trailing separators.")
        direction = arguments.get("direction", "children")
        if direction not in ("children", "parent"):
            raise RequestError("INVALID_ARGUMENTS", "direction must be children or parent.")
        object_class = arguments.get("object_class")
        if object_class is not None and (not isinstance(object_class, str) or re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", object_class) is None):
            raise RequestError("INVALID_ARGUMENTS", "object_class must be an exact class name such as IntPrj or IntCase, without wildcards.")
        return {**validate_page({"limit": 20, **arguments}, 50), "full_name": full_name,
                "direction": direction, "object_class": object_class}
    if tool in ("capture_result_snapshot", "get_result_snapshot"):
        allowed = ({"snapshot_id", "full_names", "result_attributes", "calculation", "calculation_request_id"}
                   if tool == "capture_result_snapshot" else {"snapshot_id"})
        if set(arguments) - allowed:
            raise RequestError("INVALID_ARGUMENTS", "Unsupported snapshot argument.")
        if not isinstance(arguments.get("snapshot_id"), str) or re.fullmatch(SNAPSHOT_ID_PATTERN, arguments["snapshot_id"]) is None:
            raise RequestError("INVALID_ARGUMENTS", "snapshot_id must contain 1-80 ASCII letters, digits, underscores or hyphens.")
        if tool == "get_result_snapshot":
            return {"snapshot_id": arguments["snapshot_id"]}
        paths = arguments.get("full_names")
        if (not isinstance(paths, list) or not 1 <= len(paths) <= 8 or any(not isinstance(path, str) for path in paths)
                or len(set(paths)) != len(paths)):
            raise RequestError("INVALID_ARGUMENTS", "full_names must contain 1-8 unique exact element paths.")
        names = validate_names(arguments.get("result_attributes"))
        if names is None or any(not name.startswith(("m:", "c:", "s:")) for name in names):
            raise RequestError("INVALID_ARGUMENTS", "Provide explicit direct m:/c:/s: result_attributes.")
        calculation = arguments.get("calculation", "power_flow")
        if not isinstance(calculation, str) or calculation not in CALCULATION_CLASSES:
            raise RequestError("INVALID_ARGUMENTS", "calculation must be power_flow or short_circuit.")
        request_id = arguments.get("calculation_request_id")
        if request_id is not None:
            validate_request_id(request_id)
        return {"snapshot_id": arguments["snapshot_id"], "full_names": [validate_path(path)["full_name"] for path in paths],
                "result_attributes": names, "calculation": calculation, "calculation_request_id": request_id}
    if tool in ("get_line_loading", "get_transformer_loading", "get_bus_voltage", "get_bus_voltage_sequences"):
        voltage = tool == "get_bus_voltage"
        if set(arguments) - ({"network_full_name", "name_contains", "offset", "limit"} | ({"quantity"} if voltage else set())):
            raise RequestError("INVALID_ARGUMENTS", "Unsupported native-result argument.")
        selection = {}
        if voltage:
            quantity = arguments.get("quantity", "positive_sequence")
            if not isinstance(quantity, str) or quantity not in VOLTAGE_QUANTITIES:
                raise RequestError("INVALID_ARGUMENTS", "Select positive_sequence, phase_a, phase_b, phase_c or scalar.")
            selection["quantity"] = quantity
        network = arguments.get("network_full_name")
        if network is not None:
            validate_full_path(network, "ElmNet")
        return {**validate_page({"limit": 20, **arguments}, 50), **selection, "network_full_name": network}
    if tool == "list_calculation_commands":
        if set(arguments) - {"calculation", "offset", "limit", "settings_attributes"}:
            raise RequestError("INVALID_ARGUMENTS", "Unsupported calculation-list argument.")
        calculation = arguments.get("calculation", "all")
        if calculation not in ("all", *CALCULATION_CLASSES):
            raise RequestError("INVALID_ARGUMENTS", "Use all, power_flow or short_circuit.")
        return {**validate_page(arguments, 20), "calculation": calculation,
                "settings_attributes": validate_names(arguments.get("settings_attributes"))}
    if tool == "get_calculation_status":
        if set(arguments) != {"request_id"}:
            raise RequestError("INVALID_ARGUMENTS", "Provide only request_id.")
        return {"request_id": validate_request_id(arguments["request_id"])}
    if tool == "run_short_circuit_at_bus":
        extra = {"bus_full_name", "fault_type", "confirmation_digest"}
        base = validate_arguments("run_short_circuit", {key: value for key, value in arguments.items() if key not in extra})
        bus = validate_full_path(arguments.get("bus_full_name"), "ElmTerm")
        if arguments.get("result_full_names") is None:
            base["result_full_names"] = [bus]
        fault_type = arguments.get("fault_type")
        if not isinstance(fault_type, str) or fault_type not in SHORT_CIRCUIT_FAULT_TYPES:
            raise RequestError("INVALID_ARGUMENTS", "fault_type must be 3psc, 2psc, spgf or 2pgf.")
        digest = arguments.get("confirmation_digest")
        if digest is not None and (not isinstance(digest, str) or re.fullmatch(r"[a-f0-9]{64}", digest) is None):
            raise RequestError("INVALID_ARGUMENTS", "Copy the preview's exact confirmation_digest for an authorized execution.")
        for path in (bus, base["command_full_name"], base["expected_study_case_full_name"], *base["result_full_names"]):
            if any(part in ("", ".", "..") for part in path.split("\\")[1:]):
                raise RequestError("INVALID_ARGUMENTS", "Use exact returned paths without relative or empty segments.")
        return {**base, "bus_full_name": bus, "fault_type": fault_type, "confirmation_digest": digest}
    if tool in CALCULATION_TOOLS:
        if set(arguments) - {"command_full_name", "expected_study_case_full_name", "expected_agent_instance_id", "request_id", "result_full_names", "result_attributes"}:
            raise RequestError("INVALID_ARGUMENTS", "Unsupported calculation-execution argument; configure options in the GUI.")
        kind = CALCULATION_TOOLS[tool]
        command_path = validate_full_path(arguments.get("command_full_name"), CALCULATION_CLASSES[kind])
        study_case = validate_full_path(arguments.get("expected_study_case_full_name"), "IntCase")
        expected_instance = arguments.get("expected_agent_instance_id")
        if not isinstance(expected_instance, str) or re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", expected_instance) is None:
            raise RequestError("INVALID_ARGUMENTS", "Copy expected_agent_instance_id exactly from the current session response.")
        paths = arguments.get("result_full_names", [])
        paths = [] if paths is None else paths
        if (not isinstance(paths, list) or len(paths) > 8 or any(not isinstance(path, str) for path in paths)
                or len(set(paths)) != len(paths)):
            raise RequestError("INVALID_ARGUMENTS", "result_full_names must be a list of at most eight unique element paths.")
        names = validate_names(arguments.get("result_attributes"))
        if names and any(not name.startswith(("m:", "c:", "s:")) for name in names):
            raise RequestError("INVALID_ARGUMENTS", "result_attributes must contain direct m:/c:/s: names.")
        return {"command_full_name": command_path, "expected_study_case_full_name": study_case,
                "expected_agent_instance_id": expected_instance,
                "request_id": validate_request_id(arguments.get("request_id")),
                "result_full_names": [validate_path(path)["full_name"] for path in paths], "result_attributes": names}
    if tool in ("get_active_session", "get_active_scenario", "get_powerfactory_capabilities"):
        if arguments:
            raise RequestError("INVALID_ARGUMENTS", tool + " takes no arguments.")
        return {}
    if tool == "get_model_inventory":
        if set(arguments) - {"element_classes", "sample_limit"}:
            raise RequestError("INVALID_ARGUMENTS", "Unsupported inventory argument.")
        classes = arguments.get("element_classes")
        classes = list(ELEMENT_CLASSES) if classes is None else classes
        sample_limit = arguments.get("sample_limit", 1)
        if (not isinstance(classes, list) or not 1 <= len(classes) <= len(ELEMENT_CLASSES)
                or any(not isinstance(name, str) or name not in ELEMENT_CLASSES for name in classes)
                or len(set(classes)) != len(classes) or type(sample_limit) is not int or not 0 <= sample_limit <= 3):
            raise RequestError("INVALID_ARGUMENTS", "Use unique supported classes and sample_limit 0-3.")
        return {"element_classes": classes, "sample_limit": sample_limit}
    if tool in ("get_element_attributes", "get_equipment_type_attributes", "get_elements_attributes"):
        allowed = {"full_names", "attributes", "profile", "include_equipment_types"} if tool == "get_elements_attributes" else {"full_name", "attributes", "profile"}
        if tool != "get_equipment_type_attributes":
            allowed |= {"include_results", "calculation"}
        if set(arguments) - allowed:
            raise RequestError("INVALID_ARGUMENTS", "Unsupported attribute-reading argument.")
        profile = arguments.get("profile", "extended" if tool == "get_elements_attributes" else "basic")
        if profile not in ("basic", "extended"):
            raise RequestError("INVALID_ARGUMENTS", "profile must be basic or extended.")
        data = {"attributes": validate_names(arguments.get("attributes")), "profile": profile, **result_options}
        if data["attributes"] is not None and result_options.get("include_results"):
            raise RequestError("INVALID_ARGUMENTS", "Use explicit attributes or automatic result defaults, not both.")
        if tool == "get_elements_attributes":
            paths = arguments.get("full_names")
            if (not isinstance(paths, list) or not 1 <= len(paths) <= 8
                    or any(not isinstance(name, str) for name in paths) or len(set(paths)) != len(paths)):
                raise RequestError("INVALID_ARGUMENTS", "Use 1-8 unique exact element full_names.")
            include_types = arguments.get("include_equipment_types", False)
            if type(include_types) is not bool:
                raise RequestError("INVALID_ARGUMENTS", "include_equipment_types must be a boolean.")
            return {**data, "elements": [validate_path(path) for path in paths], "include_equipment_types": include_types}
        return {**validate_path(arguments.get("full_name")), **data}
    if tool == "list_element_attributes":
        if set(arguments) - {"full_name", "target", "candidate_attributes", "include_results", "calculation", "include_unavailable", "name_contains", "offset", "limit"}:
            raise RequestError("INVALID_ARGUMENTS", "Unsupported attribute-catalog argument.")
        target = arguments.get("target", "element")
        include_results, include_unavailable = arguments.get("include_results", False), arguments.get("include_unavailable", False)
        if target not in ("element", "equipment_type") or type(include_results) is not bool or type(include_unavailable) is not bool:
            raise RequestError("INVALID_ARGUMENTS", "Use target element/equipment_type and boolean inclusion options.")
        return {**validate_path(arguments.get("full_name")), **validate_page(arguments), "target": target,
                **result_options, "include_unavailable": include_unavailable,
                "candidate_attributes": validate_names(arguments.get("candidate_attributes"))}
    if tool != "list_model_elements":
        raise RequestError("UNKNOWN_TOOL", "Select a supported PowerFactory tool.")
    if set(arguments) - {"element_class", "name_contains", "offset", "limit", "network_full_name", "in_service", "nominal_voltage_min_kv", "nominal_voltage_max_kv"}:
        raise RequestError("INVALID_ARGUMENTS", "Unsupported element-listing argument.")
    element_class = arguments.get("element_class", "ElmTerm")
    name_contains = arguments.get("name_contains", "")
    offset = arguments.get("offset", 0)
    limit = arguments.get("limit", 20)
    if element_class not in ELEMENT_CLASSES:
        raise RequestError("INVALID_ARGUMENTS", "Select one of: " + ", ".join(ELEMENT_CLASSES))
    if not isinstance(name_contains, str) or len(name_contains) > 120:
        raise RequestError("INVALID_ARGUMENTS", "name_contains must be a string of at most 120 characters.")
    if type(offset) is not int or not 0 <= offset <= 1000000:
        raise RequestError("INVALID_ARGUMENTS", "offset must be an integer from 0 to 1000000.")
    if type(limit) is not int or not 1 <= limit <= 50:
        raise RequestError("INVALID_ARGUMENTS", "limit must be an integer from 1 to 50.")
    network = arguments.get("network_full_name")
    if network is not None:
        validate_full_path(network, "ElmNet")
    in_service = arguments.get("in_service")
    if in_service is not None and type(in_service) is not bool:
        raise RequestError("INVALID_ARGUMENTS", "in_service must be a boolean or null.")
    minimum, maximum = arguments.get("nominal_voltage_min_kv"), arguments.get("nominal_voltage_max_kv")
    for bound in (minimum, maximum):
        if bound is not None and (type(bound) not in (int, float) or not math.isfinite(bound) or bound < 0):
            raise RequestError("INVALID_ARGUMENTS", "Nominal-voltage bounds must be finite non-negative numbers in kV.")
    if minimum is not None or maximum is not None:
        if element_class != "ElmTerm":
            raise RequestError("INVALID_ARGUMENTS", "Nominal-voltage filtering is supported only for ElmTerm.")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise RequestError("INVALID_ARGUMENTS", "nominal_voltage_min_kv must not exceed nominal_voltage_max_kv.")
    return {"element_class": element_class, "name_contains": name_contains,
            "offset": offset, "limit": limit, "network_full_name": network, "in_service": in_service,
            "nominal_voltage_min_kv": minimum, "nominal_voltage_max_kv": maximum}


def json_attribute_value(value, depth=0, budget=None):
    """Bound each value; never expose a live PowerFactory object or stringify it."""
    if budget is None:
        budget = [500]
    budget[0] -= 1
    if budget[0] < 0 or depth > 5:
        raise ValueError("Attribute exceeds 500 values or five levels of nesting.")
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Attribute contains a non-finite number.")
        return value
    if isinstance(value, str):
        if len(value) > 4096:
            raise ValueError("Attribute string exceeds 4096 characters.")
        return value
    if isinstance(value, (list, tuple)):
        if len(value) > 100:
            raise ValueError("Attribute array exceeds 100 entries.")
        return [json_attribute_value(item, depth + 1, budget) for item in value]
    if callable(getattr(value, "GetClassName", None)) and callable(getattr(value, "GetFullName", None)):
        reference = describe(value)
        if any(len(text) > 4096 for text in reference.values()):
            raise ValueError("Object reference metadata exceeds 4096 characters.")
        return {"kind": "object_reference", **reference}
    raise ValueError("Unsupported attribute value type: " + type(value).__name__)


def read_attribute(obj, name):
    try:
        raw = obj.GetAttribute(name)
    except Exception as exc:
        return {"status": "error", "error": {
            "code": "ATTRIBUTE_UNAVAILABLE",
            "message": (type(exc).__name__ + ": " + str(exc))[:320],
        }}
    try:
        value = json_attribute_value(raw)
        if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 32768:
            raise ValueError("Serialized attribute value exceeds 32 KiB.")
    except Exception as exc:
        return {"status": "error", "error": {
            "code": "ATTRIBUTE_VALUE_UNSUPPORTED",
            "message": (type(exc).__name__ + ": " + str(exc))[:320],
        }}
    result = {"status": "ok", "value": value, "unit": None, "description": None}
    for field, method in (("unit", "GetAttributeUnit"), ("description", "GetAttributeDescription")):
        try:
            metadata = getattr(obj, method)(name)
            if not isinstance(metadata, str) or len(metadata) > 512:
                raise ValueError("Expected metadata text of at most 512 characters.")
            result[field] = metadata
        except Exception as exc:
            # A metadata failure must not discard a successfully read value.
            result.setdefault("metadata_errors", {})[field] = (type(exc).__name__ + ": " + str(exc))[:320]
    unit = result["unit"]
    if unit is None:
        result["unit_kind"] = "unavailable"
    elif not unit.strip():
        result["unit_kind"] = "blank"
    elif re.fullmatch(r"(?:Elm|Typ|Sta|Int)[A-Za-z0-9_]+(?:,(?:Elm|Typ|Sta|Int)[A-Za-z0-9_]+)*", unit.strip()):
        result["unit_kind"] = "reference_class_metadata"
    elif "," in unit and "/" not in unit:
        result["unit_kind"] = "choice_metadata"
    else:
        result["unit_kind"] = "unit_text"
    result["attribute_kind"] = "existing_result" if name.startswith(("m:", "c:", "s:")) else "parameter"
    return result


def resolve_element(app, arguments, candidates_cache=None):
    # Never pass a user-supplied path/name to PowerFactory's wildcard matcher.
    element_class = arguments["element_class"]
    if candidates_cache is not None:
        if element_class not in candidates_cache:
            index = {}
            for obj in app.GetCalcRelevantObjects("*." + element_class) or []:
                if obj is not None:
                    index.setdefault(str(obj.GetFullName()), []).append(obj)
            candidates_cache[element_class] = index
        matches = candidates_cache[element_class].get(arguments["full_name"], [])
    else:
        candidates = app.GetCalcRelevantObjects("*." + element_class)
        matches = [obj for obj in candidates or [] if obj is not None
                   and str(obj.GetFullName()) == arguments["full_name"]]
    if not matches:
        raise RequestError("ELEMENT_NOT_FOUND", "The exact path is not calculation-relevant in the current study case. List elements again.", 404)
    if len(matches) != 1:
        raise RequestError("AMBIGUOUS_ELEMENT", "More than one element has this full path; no attributes were read.", 409)
    obj = matches[0]
    element = describe(obj)
    if element["class"] != arguments["element_class"]:
        raise RequestError("ELEMENT_CLASS_MISMATCH", "The resolved element class does not match its path.", 409)
    return obj


def attribute_data(obj, names, response_budget=ATTRIBUTE_RESPONSE_BUDGET):
    started = time.perf_counter()
    attributes, bytes_used = {}, 0
    for name in names:
        record = read_attribute(obj, name)
        size = len(json.dumps({name: record}, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        if bytes_used + size > response_budget:
            record = {"status": "error", "error": {"code": "ATTRIBUTE_RESPONSE_BUDGET_EXCEEDED",
                      "message": "Value/metadata would exceed this read's response budget. Request fewer attributes or a single element."}}
            size = len(json.dumps({name: record}).encode("utf-8"))
        attributes[name] = record
        bytes_used += size
    successful = sum(item["status"] == "ok" for item in attributes.values())
    return {
        "requested_attributes": names,
        "attributes": attributes, "successful_count": successful,
        "failed_count": len(attributes) - successful,
        "all_attributes_read": successful == len(attributes),
        "units_source": "current PowerFactory attribute units",
        "null_unit_count": sum(item.get("status") == "ok" and item.get("unit") is None for item in attributes.values()),
        "metadata_error_count": sum(len(item.get("metadata_errors", {})) for item in attributes.values()),
        "attribute_read_ms": round((time.perf_counter() - started) * 1000, 2),
        "response_budget_bytes": response_budget,
        "results_note": "No calculation was run; readable result attributes do not establish result freshness.",
    }


def availability(obj, name):
    try:
        result = obj.HasAttribute(name)
        if result not in (0, 1):
            raise ValueError("HasAttribute did not return 0 or 1.")
        return {"name": name, "available": bool(result)}
    except Exception as exc:
        return {"name": name, "available": None, "availability_error": (type(exc).__name__ + ": " + str(exc))[:320]}


def select_calculation_results(obj, element_class, kind, custom):
    if custom is not None:
        # Explicit names keep strict reads and all errors, including unavailable fields.
        return custom, {"attribute_selection": "custom", "calculation": kind,
                        "result_profile_source": "request", "candidate_attributes": custom,
                        "availability_checked": False, "excluded_attributes": [], "availability_errors": []}
    candidates, source = result_candidates(kind, element_class)
    checks = [availability(obj, name) for name in candidates]
    selection = {"attribute_selection": "calculation_defaults", "calculation": kind,
                 "result_profile_source": source, "default_profile_available": bool(candidates),
                 "candidate_attributes": candidates, "availability_checked": True,
                 "excluded_attributes": [item["name"] for item in checks if item["available"] is False],
                 "availability_errors": [item for item in checks if item["available"] is None],
                 "coverage_complete": False}
    # Failed checks stay explicit; try the read rather than silently exclude an unknown.
    return [item["name"] for item in checks if item["available"] is not False], selection


def select_attributes(obj, object_class, arguments):
    custom = arguments["attributes"]
    if custom is not None:
        return custom, {"attribute_selection": "custom", "excluded_attributes": [], "availability_errors": []}
    profile = (ELEMENT_PROFILES if object_class in ELEMENT_PROFILES else TYPE_PROFILES).get(object_class)
    names = list(profile["parameters"][arguments["profile"]]) if profile else ["loc_name"]
    selection = {"attribute_selection": "defaults", "profile": arguments["profile"], "excluded_attributes": [], "availability_errors": [],
                 "coverage_complete": False, "coverage_note": PROFILE_DATA["coverage"]}
    if arguments["profile"] == "extended":
        checks = [availability(obj, name) for name in names]
        selection["excluded_attributes"] = [item["name"] for item in checks if item["available"] is False]
        selection["availability_errors"] = [item for item in checks if item["available"] is None]
        names = [item["name"] for item in checks if item["available"] is not False]
    return names, selection


def select_current_results(app, obj, object_class, calculation):
    if calculation != "all" and CALCULATION_PROFILES[calculation]["result_source"] != "element_attributes":
        raise RequestError("RESULT_SOURCE_UNSUPPORTED", "RMS time-series reads require recorded ElmRes channels; this element reader does not read result files.")
    kinds = list(CALCULATION_PROFILES) if calculation == "all" else [calculation]
    names, states, selections, skipped = [], {}, {}, []
    for kind in kinds:
        if CALCULATION_PROFILES[kind]["result_source"] != "element_attributes":
            skipped.append({"calculation": kind, "reason": "REQUIRES_RESULT_FILE"})
            continue
        if not result_candidates(kind, object_class)[0]:
            skipped.append({"calculation": kind, "reason": "NO_DEFAULT_RESULT_PROFILE"})
            continue
        states[kind] = calculation_validity(app, kind)
        if states[kind]["valid"] is not True:
            skipped.append({"calculation": kind, "reason": "RESULTS_INVALID" if states[kind]["valid"] is False else "VALIDITY_UNKNOWN"})
            continue
        selected, selections[kind] = select_calculation_results(obj, object_class, kind, None)
        names.extend(selected)
    return list(dict.fromkeys(names)), {"calculation": calculation, "calculation_states": states,
                                       "selections": selections, "skipped_calculations": skipped}


def active_network_graphic(app, info):
    """Resolve the shown tab and its native diagram reference without opening pages."""
    desktop = app.GetDesktop()
    page = desktop.GetActivePage() if desktop is not None else None
    diagram = app.GetCurrentDiagram()
    if not page or not diagram:
        raise RequestError("NO_ACTIVE_DIAGRAM", "Show an existing network diagram before using graphics tools.", 409)
    page_info, diagram_info = describe(page), describe(diagram)
    if page_info["class"] != "SetDeskpage" or diagram_info["class"] != "IntGrfnet":
        raise RequestError("UNSUPPORTED_GRAPHIC_PAGE", "The active page must be a network diagram, not a plot.", 409)
    if (not page_info["full_name"].startswith(info["study_case"]["full_name"] + "\\")
            or not diagram_info["full_name"].startswith(info["project"]["full_name"] + "\\")):
        raise RequestError("GRAPHIC_OUT_OF_SCOPE", "The shown tab/diagram must belong to the active study case/project.", 409)
    if describe(page.GetAttribute("pGrph")) != diagram_info:
        raise RequestError("GRAPHIC_CONTEXT_MISMATCH", "The shown tab and current diagram do not refer to the same graphic.", 409)
    return page_info, diagram_info, diagram


def get_graphic_selection(app, info, arguments):
    context = active_context(app, info)
    page, diagram, _ = active_network_graphic(app, info)
    selected = app.GetDiagramSelection()
    if not isinstance(selected, (list, tuple)) or any(obj is None for obj in selected):
        raise RequestError("GRAPHIC_SELECTION_UNAVAILABLE", "GetDiagramSelection did not return a native object list.", 409)
    identities = [describe(obj) for obj in selected]
    offset, limit = arguments["offset"], arguments["limit"]
    entries, cache = [], {}
    for index, obj in enumerate(selected[offset:offset + limit], offset):
        identity = identities[index]
        model, mapping_error = None, None
        source = "GetDiagramSelection"
        try:
            if identity["class"] == "IntGrf":
                source = "IntGrf.pDataObj"
                model = obj.GetAttribute("pDataObj")
            elif identity["class"].startswith(("Elm", "Rel", "Sta")):
                model = obj
            model_info = describe(model)
            if model_info is None:
                inspection = {"available": False, "reason": "NO_MODEL_REFERENCE"}
            elif not model_info["full_name"].startswith(info["project"]["full_name"] + "\\"):
                inspection = {"available": False, "reason": "OUTSIDE_ACTIVE_PROJECT"}
            elif model_info["class"] not in ELEMENT_CLASSES:
                inspection = {"available": False, "reason": "UNSUPPORTED_INSPECTION_CLASS"}
            else:
                try:
                    resolve_element(app, {"full_name": model_info["full_name"], "element_class": model_info["class"]}, cache)
                    inspection = {"available": True, "full_name": model_info["full_name"]}
                except RequestError as exc:
                    inspection = {"available": False, "reason": exc.code}
        except Exception as exc:
            model_info = None
            mapping_error = (type(exc).__name__ + ": " + str(exc))[:320]
            inspection = {"available": None, "reason": "SELECTION_MAPPING_FAILED"}
        entries.append({"index": index, "selected_object": identity, "model_element": model_info,
                        "mapping_source": source, "mapping_error": mapping_error, "inspection": inspection})
    after_page, after_diagram, _ = active_network_graphic(app, info)
    if (active_context(app) != context or page != after_page or diagram != after_diagram
            or [describe(obj) for obj in app.GetDiagramSelection()] != identities):
        raise RequestError("GRAPHIC_SELECTION_CHANGED", "The active context, diagram or selection changed during the read; query again.", 409)
    response = {**info, "context": context, "page": page, "diagram": diagram, "selection": entries,
                "selected_count": len(selected), "returned_count": len(entries), "offset": offset, "limit": limit,
                "next_offset": offset + len(entries) if offset + len(entries) < len(selected) else None,
                "read_complete": all(item["mapping_error"] is None for item in entries),
                "source": "Application.GetDiagramSelection",
                "note": "This is the native selection in the top diagram, not the browser or script-launch selection. Use inspection.full_name with existing attribute/type/connection tools only when inspection.available is true. Annotations, unsupported classes and non-calculation-relevant objects remain explicit. No selection change, page opening, model edit or calculation. The current synchronous agent keeps the GUI busy; the selection may need to be made before launching it."}
    if len(json.dumps(response, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 786432:
        raise RequestError("GRAPHIC_RESPONSE_TOO_LARGE", "Selection metadata exceeds the response budget; request a smaller page.", 409)
    return response


def mark_in_graphics(app, instance_id, info, arguments):
    if arguments["expected_agent_instance_id"] != instance_id:
        raise RequestError("INSTANCE_MISMATCH", "The agent changed; read the active session again.", 409)
    if arguments["expected_study_case_full_name"] != info["study_case"]["full_name"]:
        raise RequestError("STUDY_CASE_MISMATCH", "The study case changed; read the active session again.", 409)
    context = active_context(app, info)
    if not context["context_complete"]:
        raise RequestError("CONTEXT_INCOMPLETE", "Resolve the active-context errors before highlighting.", 409)
    page, diagram, graphic = active_network_graphic(app, info)
    if diagram["full_name"] != arguments["expected_diagram_full_name"]:
        raise RequestError("GRAPHIC_CONTEXT_MISMATCH", "The active diagram changed; read its selection again.", 409)
    cache = {}
    targets = [resolve_element(app, validate_path(path), cache) for path in arguments["full_names"]]
    symbols, errors, error_count = {}, [], 0
    for obj in graphic.GetContents("*.IntGrf", 1) or []:
        try:
            symbol = describe(obj)
            if not symbol["full_name"].startswith(diagram["full_name"] + "\\"):
                raise ValueError("A graphic object was outside the active diagram.")
            model = obj.GetAttribute("pDataObj")
            if model is not None:
                symbols.setdefault(str(model.GetFullName()), []).append(symbol)
        except Exception as exc:
            error_count += 1
            if len(errors) < 50:
                errors.append({"operation": "IntGrf.pDataObj", "error": (type(exc).__name__ + ": " + str(exc))[:320]})
    rows = [{"element": describe(obj), "graphics": symbols.get(str(obj.GetFullName()), []),
             "presence": "present" if str(obj.GetFullName()) in symbols else "unknown" if errors else "absent"}
            for obj in targets]
    eligible = [obj for obj, row in zip(targets, rows) if row["presence"] == "present"]
    if len(json.dumps({"context": context, "elements": rows}, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 262144:
        raise RequestError("GRAPHIC_RESPONSE_TOO_LARGE", "Graphic/context metadata exceeds the preparation budget; select fewer elements. Nothing was marked.", 409)
    check_page, check_diagram, _ = active_network_graphic(app, info)
    if active_context(app) != context or check_page != page or check_diagram != diagram:
        raise RequestError("GRAPHIC_CONTEXT_MISMATCH", "Context or diagram changed during preparation; nothing was marked.", 409)
    completed, error = False, None
    if eligible:
        try:
            app.MarkInGraphics(eligible, 1)
            completed = True
        except Exception as exc:
            error = {"code": "GRAPHIC_MARK_OUTCOME_UNKNOWN", "message": (type(exc).__name__ + ": " + str(exc))[:320]}
    after, observation_error = None, None
    try:
        after = get_graphic_selection(app, session_info(app), {"offset": 0, "limit": 50})
    except Exception as exc:
        observation_error = (type(exc).__name__ + ": " + str(exc))[:320]
    response = {**info, "context_before": context, "page_before": page, "diagram_before": diagram,
            "elements": rows, "eligible_count": len(eligible), "mark_attempted": bool(eligible),
            "native_call_completed": completed, "error": error, "graphic_read_errors": errors,
            "graphic_read_error_count": error_count, "graphic_read_errors_truncated": error_count > len(errors),
            "selection_after": after, "observation_error": observation_error,
            "active_context_unchanged": after["context"] == context if after is not None else None,
            "active_diagram_unchanged": after["diagram"] == diagram and after["page"] == page if after is not None else None,
            "highlight_verified": None, "effect": "UI highlighting and possible activation of an already open diagram",
            "note": "Only explicitly requested elements with symbols in the initial active diagram are passed to MarkInGraphics(objects, 1). Absent/unknown elements are skipped; no symbols or electrical objects are created. The native method searches open diagrams and returns no highlight-verification result; native_call_completed is not visual confirmation. selection_after reports actual native selection, which is distinct from highlighting. Inspect before retrying after an exception/timeout. No settings, calculation or save is requested."}
    if len(json.dumps(response, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 786432:
        response["selection_after"] = None
        response["observation_error"] = "Selection metadata omitted to preserve the operation outcome within the response budget; call get_graphic_selection separately."
    return response


def export_graphic(app, info, arguments):
    context = active_context(app, info)
    if not context["context_complete"]:
        raise RequestError("CONTEXT_INCOMPLETE", "Read get_active_session and resolve its context errors before exporting.", 409)
    case = app.GetActiveStudyCase()
    boards = [obj for obj in case.GetContents("*.SetDesktop", 1) or [] if obj is not None]
    pages = [(board, page) for board in boards for cls in ("SetDeskpage", "GrpPage")
             for page in board.GetContents("*." + cls, 1) or []
             if page is not None and str(page.GetFullName()) == arguments["page_full_name"]]
    if len(pages) != 1:
        raise RequestError("GRAPHIC_PAGE_NOT_FOUND" if not pages else "AMBIGUOUS_GRAPHIC_PAGE",
                           "Select an exact existing SetDeskpage or GrpPage under this study case's Graphics Board.", 404 if not pages else 409)
    root = Path(__file__).resolve().parent
    directory = (root / "exports").resolve()
    path = (directory / arguments["file_name"]).resolve()
    if not directory.is_relative_to(root) or path.parent != directory:
        raise RequestError("EXPORT_OUT_OF_SCOPE", "Graphics must stay inside this MCP workspace's exports folder.", 403)
    if path.exists():
        raise RequestError("EXPORT_EXISTS", "This filename already exists; choose a new name.", 409)
    commands = [obj for obj in case.GetContents("*.ComWr", 1) or [] if obj is not None
                and (arguments["command_full_name"] is None or str(obj.GetFullName()) == arguments["command_full_name"])]
    command_created = not commands and arguments["command_full_name"] is None
    if command_created:
        command = app.GetFromStudyCase("ComWr")
        if command is None or str(command.GetClassName()) != "ComWr" or not str(command.GetFullName()).startswith(info["study_case"]["full_name"] + "\\"):
            raise RequestError("GRAPHIC_EXPORT_COMMAND_UNAVAILABLE", "PowerFactory did not create its standard ComWr in the active study case.", 409)
        commands = [command]
    if len(commands) != 1:
        raise RequestError("GRAPHIC_EXPORT_COMMAND_NOT_FOUND" if not commands else "AMBIGUOUS_GRAPHIC_EXPORT_COMMAND",
                           "Select an exact existing ComWr command in this study case; if several exist, supply its command_full_name.", 404 if not commands else 409)
    board, page = pages[0]
    command = commands[0]
    if not callable(getattr(command, "ExportGraphicTab", None)):
        raise RequestError("GRAPHIC_EXPORT_UNAVAILABLE", "This ComWr does not expose ExportGraphicTab.", 409)
    page_info, command_info = describe(page), describe(command)
    previous_page = board.GetActivePage()
    previous_page_info = describe(previous_page) if previous_page else None
    settings = {name: command.GetAttribute(name) for name in ("iopt_rd", "iopt_savas", "f")}
    try:
        directory.mkdir(exist_ok=True)
        with path.open("xb"):
            pass
    except FileExistsError as exc:
        raise RequestError("EXPORT_EXISTS", "This filename already exists; choose a new name.", 409) from exc
    except OSError as exc:
        raise RequestError("EXPORT_WRITE_FAILED", str(exc), 409) from exc
    code, error, restoration_errors = None, None, {}
    try:
        code = command.ExportGraphicTab(page, str(path))
        if type(code) is not int or code != 0:
            error = {"code": "GRAPHIC_EXPORT_FAILED", "message": "ExportGraphicTab did not return integer zero."}
    except Exception as exc:
        error = {"code": "GRAPHIC_EXPORT_FAILED", "message": (type(exc).__name__ + ": " + str(exc))[:320]}
    finally:
        for name, value in settings.items():
            try:
                if command.GetAttribute(name) != value:
                    command.SetAttribute(name, value)
                if command.GetAttribute(name) != value:
                    raise ValueError("Original export setting was not restored.")
            except Exception as exc:
                restoration_errors[name] = (type(exc).__name__ + ": " + str(exc))[:320]
        try:
            current_page = board.GetActivePage()
            if (describe(current_page) if current_page else None) != previous_page_info:
                if previous_page is None or previous_page == 0:
                    raise ValueError("Export changed an initially inactive desktop; no previous page can be shown.")
                board.Show(previous_page)
            current_page = board.GetActivePage()
            if (describe(current_page) if current_page else None) != previous_page_info:
                raise ValueError("Original active page was not restored.")
        except Exception as exc:
            restoration_errors["active_page"] = (type(exc).__name__ + ": " + str(exc))[:320]
    after = active_context(app, info)
    size, digest, valid_file = 0, None, False
    try:
        size = path.stat().st_size
        with path.open("rb") as output:
            header = output.read(4096)
            suffix = path.suffix
            valid_file = size > 0 and (header.startswith(b"\x89PNG\r\n\x1a\n") if suffix == ".png" else
                                      header.startswith(b"%PDF-") if suffix == ".pdf" else b"<svg" in header)
            if valid_file:
                output.seek(0)
                checksum = hashlib.sha256()
                for block in iter(lambda: output.read(65536), b""):
                    checksum.update(block)
                digest = checksum.hexdigest()
        if not valid_file and error is None:
            error = {"code": "GRAPHIC_FILE_INVALID", "message": "The requested graphic is missing, empty or has a different format."}
    except OSError as exc:
        error = error or {"code": "GRAPHIC_FILE_UNREADABLE", "message": str(exc)[:320]}
    if error is not None:
        path.unlink(missing_ok=True)
        valid_file = False
    complete = valid_file and error is None and not restoration_errors and after == context
    return {**info, "context": context, "context_after": after, "context_unchanged": after == context,
            "page": page_info, "command": command_info, "command_created": command_created,
            "active_page_before": previous_page_info,
            "return_code": code if type(code) is int else None,
            "status": "complete" if complete else "partial" if valid_file else "failed", "export_complete": bool(complete),
            "file_path": str(path) if valid_file else None, "format": path.suffix[1:],
            "byte_count": size if valid_file else 0, "sha256": digest if valid_file else None,
            "settings_restored": not any(name in restoration_errors for name in settings),
            "active_page_restored": "active_page" not in restoration_errors, "restoration_errors": restoration_errors, "error": error,
            "note": "Exports the exact existing graphic tab through ComWr.ExportGraphicTab. If no ComWr exists and none was explicitly selected, PowerFactory's standard export command is created once and retained; command_created reports this setup, which may remain after a failed export. Format/path/save-dialog settings and the displayed page are checked and restored if changed. No page or calculation is created/run and no database save is requested. The graphic is not a complete result dataset. A timeout can leave an artifact or command; inspect exports and the study case before retrying."}


def get_feeder_inventory(app, info, arguments):
    context = active_context(app, info)
    folder = app.GetProjectFolder("netdat", 0)
    if folder is None:
        raise RequestError("NETWORK_DATA_UNAVAILABLE", "The active project has no Network Data folder.", 409)
    definitions = [obj for obj in folder.GetContents("*.ElmFeeder", 1) or [] if obj is not None]
    feeder_path = arguments["feeder_full_name"]
    feeder, before, after, class_counts, member_count = None, None, None, None, None
    mode, status = "definitions", "complete"
    entries = [describe(obj) for obj in definitions] if feeder_path is None else []
    if feeder_path is not None:
        matches = [obj for obj in definitions if str(obj.GetFullName()) == feeder_path]
        if len(matches) != 1:
            raise RequestError("FEEDER_NOT_FOUND" if not matches else "AMBIGUOUS_FEEDER",
                               "Select one exact ElmFeeder path from this active project's feeder definitions.", 404 if not matches else 409)
        feeder, mode, entries = describe(matches[0]), "members", []
        before = calculation_validity(app, "power_flow")
        status = "invalid_results" if before["valid"] is False else "validity_unknown"
        if before["valid"] is True:
            members = matches[0].GetAll(int(arguments["include_nested"]))
            if not isinstance(members, (list, tuple)) or any(obj is None for obj in members):
                raise RequestError("FEEDER_MEMBERSHIP_UNAVAILABLE", "ElmFeeder.GetAll did not return a native object list.", 409)
            entries = [describe(obj) for obj in members]
            if len({item["full_name"] for item in entries}) != len(entries):
                raise RequestError("FEEDER_MEMBERSHIP_UNAVAILABLE", "ElmFeeder.GetAll returned duplicate identities.", 409)
            member_count, class_counts, status = len(entries), {}, "complete"
            for item in entries:
                class_counts[item["class"]] = class_counts.get(item["class"], 0) + 1
        after = calculation_validity(app, "power_flow")
        if after["valid"] is not True:
            entries, member_count, class_counts = [], None, None
            status = "invalid_results" if after["valid"] is False else "validity_unknown"
    if active_context(app, info) != context:
        raise RequestError("SESSION_CHANGED", "The active configuration changed during the feeder read; query again.", 409)
    if status == "complete" and not context["context_complete"]:
        status = "context_incomplete"
    selected = [item for item in entries if arguments["name_contains"].casefold() in item["name"].casefold()
                and (arguments["object_class"] is None or item["class"] == arguments["object_class"])]
    selected.sort(key=lambda item: (item["name"].casefold(), item["full_name"]))
    offset, limit = arguments["offset"], arguments["limit"]
    page = selected[offset:offset + limit]
    following = offset + len(page)
    return {**info, "context": context, "mode": mode, "definition_folder": describe(folder),
            "defined_feeder_count": len(definitions), "feeder": feeder,
            "include_nested": arguments["include_nested"], "object_class": arguments["object_class"],
            "membership_source": "ElmFeeder.GetAll(iNested)" if feeder is not None else None,
            "member_count": member_count, "class_counts": dict(sorted(class_counts.items())) if class_counts is not None else None,
            "status": status, "inventory_complete": status == "complete",
            "validity_before": before, "validity_after": after,
            "matched_count": len(selected) if mode == "definitions" or member_count is not None else None,
            "returned_count": len(page), "offset": offset, "limit": limit,
            "next_offset": following if following < len(selected) else None,
            "feeders": page if mode == "definitions" else [], "members": page if mode == "members" else [],
            "note": "Definitions are native ElmFeeder objects under the active project's Network Data. Membership uses existing valid load-flow results; invalid/unknown validity leaves counts unknown, not zero. Counts cover all native members before class/name filtering and pagination. Nested feeders are included only when requested. No membership is inferred from folder names and no topology trace, calculation or model write is performed."}


def get_element_connections(app, info, arguments, obj=None):
    obj = resolve_element(app, arguments) if obj is None else obj
    context = active_context(app, info)
    sides = CONNECTION_SIDES[arguments["element_class"]]

    def switch_state(switch):
        data = {"switch": describe(switch), "on_off": read_attribute(switch, "on_off"), "closed": None, "error": None}
        try:
            code = switch.IsClosed()
            if code not in (0, 1):
                raise ValueError("IsClosed did not return 0 or 1.")
            data["closed"] = bool(code)
        except Exception as exc:
            data["error"] = {"code": "SWITCH_STATE_UNAVAILABLE", "message": (type(exc).__name__ + ": " + str(exc))[:320]}
        return data

    outserv = read_attribute(obj, "outserv")
    in_service = (outserv["value"] == 0 if outserv["status"] == "ok" and outserv.get("value") in (0, 1) else None)
    own_switch = switch_state(obj) if arguments["element_class"] == "ElmCoup" else None
    ends = []
    for index, attribute in enumerate(sides):
        end = {"bus_index": index, "connection_attribute": attribute, "cubicle": None,
               "terminal": None, "terminal_considering_switches": None, "busbar": None,
               "physically_connected": None, "connected_considering_switches": None,
               "cubicle_switches": None, "errors": {}}
        cubicle = None
        try:
            cubicle = obj.GetCubicle(index)
            end["cubicle"] = describe(cubicle)
            if cubicle is not None and end["cubicle"]["class"] != "StaCubic":
                raise ValueError("GetCubicle did not return a StaCubic.")
            end["cubicle_switches"] = [] if cubicle is None else None
        except Exception as exc:
            cubicle = None
            end["errors"]["cubicle"] = (type(exc).__name__ + ": " + str(exc))[:320]
        for consider_switches, field, flag in ((0, "terminal", "physically_connected"),
                                                (1, "terminal_considering_switches", "connected_considering_switches")):
            try:
                terminal = obj.GetNode(index, consider_switches)
                end[field] = describe(terminal)
                end[flag] = terminal is not None
            except Exception as exc:
                end["errors"][field] = (type(exc).__name__ + ": " + str(exc))[:320]
        if cubicle is not None:
            try:
                end["busbar"] = describe(cubicle.GetAttribute("cBusBar"))
            except Exception as exc:
                end["errors"]["busbar"] = (type(exc).__name__ + ": " + str(exc))[:320]
            try:
                switches = cubicle.GetContents("*.StaSwitch", 0) or []
                if any(switch is None or str(switch.GetClassName()) != "StaSwitch" for switch in switches):
                    raise ValueError("The cubicle did not return a StaSwitch list.")
                switches.sort(key=lambda switch: str(switch.GetFullName()))
                end["cubicle_switches"] = [switch_state(switch) for switch in switches]
            except Exception as exc:
                end["errors"]["cubicle_switches"] = (type(exc).__name__ + ": " + str(exc))[:320]
        ends.append(end)
    if active_context(app, info) != context:
        raise RequestError("SESSION_CHANGED", "The active context changed while reading electrical connections; query again.", 409)
    switches = ([own_switch] if own_switch is not None else []) + [switch for end in ends for switch in end["cubicle_switches"] or []]
    complete = (context["context_complete"] and in_service is not None and not outserv.get("metadata_errors")
                and not any(end["errors"] for end in ends)
                and all(switch["closed"] is not None and switch["on_off"]["status"] == "ok"
                        and not switch["on_off"].get("metadata_errors") for switch in switches))
    return {**info, "context": context, "element": describe(obj), "outserv": outserv, "in_service": in_service,
            "switch_state": own_switch, "connections": ends, "endpoint_count": len(ends),
            "status": "complete" if complete else "partial", "read_complete": bool(complete),
            "status_sources": {"cubicle": "GetCubicle(bus_index)", "terminal": "GetNode(bus_index, 0)",
                               "terminal_considering_switches": "GetNode(bus_index, 1)", "busbar": "StaCubic.cBusBar",
                               "switch_closed": "ElmCoup/StaSwitch.IsClosed", "in_service": "element.outserv == 0"},
            "note": "Direct terminals/cubicles, resolved busbars, switch-aware node reads, switch positions and the element's own service flag are separate facts. Null read states with errors are unknown; a successful GetNode returning None means no node was found. An empty cubicle-switch list does not cover every series switch in a detailed substation. This is endpoint inspection, not a path/energisation or power-flow assessment. No switch, service state or model data is changed and no calculation is run."}


def get_short_circuit_contributions(app, info, arguments):
    bus_path = arguments["bus_full_name"]
    bus = resolve_element(app, {"full_name": bus_path, "element_class": "ElmTerm"})
    context = active_context(app, info)
    before = calculation_validity(app, "short_circuit")
    bus_data = attribute_data(bus, ["uknom", "outserv", "m:Ikss", "m:phii"], 8192)
    commands = list_calculation_commands(app, info, {"calculation": "short_circuit", "offset": 0,
                                                   "limit": 20, "settings_attributes": None})
    connected = {str(obj.GetFullName()): obj for obj in bus.GetConnectedElements(0, 0, 0) or [] if obj is not None}
    entries, unsupported = [], []
    for path, obj in sorted(connected.items()):
        identity = describe(obj)
        if identity["class"] not in CONNECTION_SIDES:
            unsupported.append(identity)
        else:
            entries.append((identity, obj))
    offset, limit = arguments["offset"], arguments["limit"]
    rows = []
    for identity, obj in entries[offset:offset + limit]:
        row = {"element": identity, "bus_ends": [], "read_complete": False}
        try:
            connection = get_element_connections(app, info, {"full_name": identity["full_name"],
                                                  "element_class": identity["class"]}, obj=obj)
            row.update({key: connection[key] for key in ("outserv", "in_service", "switch_state", "connections")})
            for end in connection["connections"]:
                if end["terminal"] is not None and end["terminal"]["full_name"] == bus_path:
                    side = end["connection_attribute"]
                    row["bus_ends"].append({"bus_index": end["bus_index"], "connection_attribute": side,
                                            **attribute_data(obj, ["m:Ikss:" + side, "m:phii:" + side], 8192)})
            row["read_complete"] = bool(connection["read_complete"] and row["bus_ends"]
                                        and all(end["all_attributes_read"] and not end["metadata_error_count"]
                                                for end in row["bus_ends"]))
            if not row["bus_ends"]:
                row["error"] = {"code": "NO_DIRECT_TERMINAL_MATCH",
                                "message": "No physical endpoint matched the selected bus; no current end was inferred."}
        except RequestError as exc:
            if exc.code == "SESSION_CHANGED":
                raise
            row["error"] = {"code": exc.code, "message": str(exc)}
        except Exception as exc:
            row["error"] = {"code": "CONNECTION_READ_FAILED", "message": (type(exc).__name__ + ": " + str(exc))[:320]}
        rows.append(row)
    if active_context(app, info) != context:
        raise RequestError("SESSION_CHANGED", "The active context changed during the short-circuit contribution read.", 409)
    after = calculation_validity(app, "short_circuit")
    following = offset + len(rows)
    settings_complete = (not commands["class_query_errors"] and commands["next_offset"] is None
                         and all(item["settings"]["all_attributes_read"] and not item["settings"]["metadata_error_count"]
                                 and not item["settings"]["availability_errors"] for item in commands["commands"]))
    return {**info, "context": context, "bus": describe(bus), "bus_data": bus_data,
            "validity_before": before, "validity_after": after, "short_circuit_commands": commands,
            "connection_source": "ElmTerm.GetConnectedElements(0, 0, 0)",
            "supported_connection_classes": list(CONNECTION_SIDES), "unsupported_elements": unsupported,
            "connected_element_count": len(connected), "matched_count": len(entries), "returned_count": len(rows),
            "offset": offset, "limit": limit, "next_offset": following if following < len(entries) else None,
            "contributions": rows,
            "coverage_complete": context["context_complete"] and not unsupported and offset == 0 and following >= len(entries)
                                 and all(row["bus_ends"] for row in rows),
            "read_complete": bool(context["context_complete"] and bus_data["all_attributes_read"]
                                  and not bus_data["metadata_error_count"] and settings_complete
                                  and all(row["read_complete"] for row in rows)),
            "results_note": "Existing scalar Ikss and phase-angle values with native units/sign conventions; readable values are retained even when validity is false or unknown. Connections include open switches and out-of-service elements. Only exact physical terminal matches select current ends; resolved busbars are reported separately. Current command settings do not prove the settings used for stored results or that the selected bus was faulted. All-bus/multiple-fault results must not be interpreted as contributions to one fault. No phase conversion, current reversal, sum, percentage, source/path tracing, model change or calculation is performed."}


def get_element_attributes(app, info, arguments, obj=None, response_budget=ATTRIBUTE_RESPONSE_BUDGET):
    obj = resolve_element(app, arguments) if obj is None else obj
    names, selection = select_attributes(obj, arguments["element_class"], arguments)
    if arguments.get("include_results", False):
        results, selection["result_selection"] = select_current_results(
            app, obj, arguments["element_class"], arguments.get("calculation", "all"))
        names = list(dict.fromkeys(names + results))
    data = attribute_data(obj, names, response_budget)
    if "result_selection" in selection:
        selection["result_selection"]["calculation_states_after"] = {
            kind: calculation_validity(app, kind) for kind in selection["result_selection"]["calculation_states"]}
    return {
        **info, "scope": "calculation-relevant elements in the active study case",
        "element": describe(obj), **selection, **data,
    }


def resolve_equipment_type(source, element_class):
    try:
        equipment_type = source.GetAttribute("typ_id")
    except Exception as exc:
        raise RequestError("TYPE_REFERENCE_UNAVAILABLE", "Could not read the source element's typ_id: " + (type(exc).__name__ + ": " + str(exc))[:320], 409) from exc
    if equipment_type is None:
        raise RequestError("NO_EQUIPMENT_TYPE", "This source element has no assigned typ_id.", 409)
    if not callable(getattr(equipment_type, "GetClassName", None)) or not callable(getattr(equipment_type, "GetFullName", None)):
        raise RequestError("INVALID_TYPE_REFERENCE", "typ_id did not return a PowerFactory object reference.", 409)
    type_info = describe(equipment_type)
    type_class = type_info["class"]
    expected = EQUIPMENT_TYPE_CLASSES.get(element_class)
    if (expected is not None and type_class not in expected
            or expected is None and re.fullmatch(r"Typ[A-Za-z0-9_]+", type_class) is None):
        raise RequestError("TYPE_CLASS_MISMATCH", "The assigned type class does not match the source element class.", 409)
    return equipment_type, type_info


def get_equipment_type_attributes(app, info, arguments, source=None, response_budget=ATTRIBUTE_RESPONSE_BUDGET):
    source = resolve_element(app, arguments) if source is None else source
    equipment_type, type_info = resolve_equipment_type(source, arguments["element_class"])
    type_class = type_info["class"]
    names, selection = select_attributes(equipment_type, type_class, arguments)
    has_profile = len((TYPE_PROFILES.get(type_class) or ELEMENT_PROFILES.get(type_class) or {}).get("parameters", {}).get(arguments["profile"], [])) > 1
    data = {
        **info, "scope": "equipment type referenced by a calculation-relevant element",
        "source_element": describe(source), "reference_attribute": "typ_id",
        "equipment_type": type_info, "reference_kind": "coupling_element" if type_class in ("ElmTow", "ElmCabsys") else "equipment_type",
        **selection,
        "default_profile_available": has_profile,
        **attribute_data(equipment_type, names, response_budget),
    }
    if not has_profile and arguments["attributes"] is None:
        data["profile_note"] = "Only loc_name is read by default for " + type_class + "; request direct attribute names to inspect additional type parameters."
    return data


def list_element_attributes(app, info, arguments):
    source = resolve_element(app, arguments)
    obj = source
    if arguments["target"] == "equipment_type":
        obj, _ = resolve_equipment_type(source, arguments["element_class"])
    identity = describe(obj)
    profile = (ELEMENT_PROFILES if identity["class"] in ELEMENT_PROFILES else TYPE_PROFILES).get(identity["class"], {})
    results, result_kinds, states = [], {}, {}
    if arguments["include_results"]:
        calculation = arguments.get("calculation", "all")
        kinds = list(CALCULATION_PROFILES) if calculation == "all" else [calculation]
        for kind in kinds:
            candidates, _ = result_candidates(kind, identity["class"], defaults=False)
            if candidates or calculation != "all":
                states[kind] = calculation_validity(app, kind)
            for name in candidates:
                results.append(name)
                result_kinds.setdefault(name, []).append(kind)
    names = list(dict.fromkeys(profile.get("parameters", {}).get("extended", ["loc_name"]) + results +
                (arguments["candidate_attributes"] or [])))
    checks = [availability(obj, name) for name in names]
    for item in checks:
        if not item["name"].startswith(("m:", "c:", "s:")):
            continue
        item["attribute_exists"] = item["available"]
        item["requires_calculations"] = result_kinds.get(item["name"], [])
        valid = [states[kind]["valid"] for kind in item["requires_calculations"]]
        sources = {kind: CALCULATION_PROFILES[kind]["result_source"] for kind in item["requires_calculations"]}
        item["result_sources"] = sources
        if item["attribute_exists"] is True:
            item["available"] = (True if any(states[kind]["valid"] is True and source == "element_attributes"
                                             for kind, source in sources.items())
                                 else False if valid and all(value is False for value in valid) else None)
    selected = [item for item in checks if (arguments["include_unavailable"] or item["available"] is not False)
                and arguments["name_contains"].casefold() in item["name"].casefold()]
    selected.sort(key=lambda item: item["name"])
    offset, limit = arguments["offset"], arguments["limit"]
    page = selected[offset:offset + limit]
    for item in page:
        item["attribute_kind"] = "existing_result" if item["name"].startswith(("m:", "c:", "s:")) else "parameter"
        if item["available"] is True:
            for field, method in (("unit", "GetAttributeUnit"), ("description", "GetAttributeDescription")):
                try:
                    value = getattr(obj, method)(item["name"])
                    if not isinstance(value, str) or len(value) > 512:
                        raise ValueError("Expected metadata text of at most 512 characters.")
                    item[field] = value
                except Exception as exc:
                    item[field] = None
                    item.setdefault("metadata_errors", {})[field] = (type(exc).__name__ + ": " + str(exc))[:320]
    following = offset + len(page)
    return {**info, "source_element": describe(source), "target": arguments["target"], "object": identity,
            "attributes": page, "candidate_count": len(checks), "available_count": sum(item["available"] is True for item in checks),
            "unavailable_count": sum(item["available"] is False for item in checks),
            "availability_error_count": sum(item["available"] is None for item in checks),
            "matched_count": len(selected), "returned_count": len(page), "offset": offset, "limit": limit,
            "next_offset": following if following < len(selected) else None,
            "name_source": "class_profiles_plus_calculation_results_plus_custom_names", "coverage_complete": False,
            "coverage_note": PROFILE_DATA["coverage"], "values_read": False,
            "calculation_states": states,
            "reference_value_read": arguments["target"] == "equipment_type",
            "results_note": "Result candidates require HasAttribute and native calculation validity. Unknown/uncatalogued or ElmRes-channel availability stays unknown. No result values were read and no calculation was run; metadata does not prove result readability or freshness."}


def get_model_inventory(app, info, arguments):
    records = []
    for element_class in arguments["element_classes"]:
        started = time.perf_counter()
        try:
            objects = [obj for obj in app.GetCalcRelevantObjects("*." + element_class) or [] if obj is not None]
            records.append({"element_class": element_class, "label": ELEMENT_PROFILES[element_class]["label"],
                            "status": "ok", "count": len(objects), "samples": [describe(obj) for obj in objects[:arguments["sample_limit"]]],
                            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2)})
        except Exception as exc:
            records.append({"element_class": element_class, "label": ELEMENT_PROFILES[element_class]["label"],
                            "status": "error", "count": None, "samples": [],
                            "error": {"code": "CLASS_QUERY_FAILED", "message": (type(exc).__name__ + ": " + str(exc))[:320]}})
    return {**info, "scope": "calculation-relevant elements in the active study case", "classes": records,
            "queried_class_count": len(records), "successful_class_count": sum(item["status"] == "ok" for item in records),
            "failed_class_count": sum(item["status"] == "error" for item in records),
            "empty_classes": [item["element_class"] for item in records if item["count"] == 0],
            "total_elements": sum(item["count"] or 0 for item in records),
            "sample_order": "PowerFactory query order; use list_model_elements for sorted pages",
            "supported_element_classes": list(ELEMENT_CLASSES)}


def get_elements_attributes(app, info, arguments):
    records, candidates_cache, bytes_used = [], {}, 0
    # Fresh request-local cache: reduce class queries without retaining live PF objects across calls.
    for path in arguments["elements"]:
        started = time.perf_counter()
        selected = {**path, "attributes": arguments["attributes"], "profile": arguments["profile"],
                    "include_results": arguments.get("include_results", False), "calculation": arguments.get("calculation", "all")}
        item = {"full_name": path["full_name"]}
        try:
            obj = resolve_element(app, selected, candidates_cache)
            budget = 32768 if arguments["include_equipment_types"] else 65536
            item.update({"status": "ok", "element_data": get_element_attributes(app, info, selected, obj, budget)})
            if arguments["include_equipment_types"]:
                check = availability(obj, "typ_id")
                if check["available"] is False:
                    item["equipment_type_read"] = {"status": "skipped", "reason": "no_typ_id_attribute"}
                else:
                    try:
                        typed = get_equipment_type_attributes(app, info, {**selected, "attributes": None, "include_results": False}, obj, budget)
                        item["equipment_type_read"] = {"status": "ok", "data": typed}
                    except RequestError as exc:
                        item["equipment_type_read"] = {"status": "skipped" if exc.code == "NO_EQUIPMENT_TYPE" else "error",
                                                       "reason": exc.code, "message": str(exc)}
        except RequestError as exc:
            item.update({"status": "error", "error": {"code": exc.code, "message": str(exc)}})
        except Exception as exc:
            item.update({"status": "error", "error": {"code": "POWERFACTORY_API_ERROR", "message": (type(exc).__name__ + ": " + str(exc))[:320]}})
        item["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
        size = len(json.dumps(item, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        if bytes_used + size > 786432:
            item = {"full_name": path["full_name"], "status": "error", "error": {
                "code": "ELEMENT_RESPONSE_BUDGET_EXCEEDED", "message": "This element would exceed the batch response budget. Read it separately."}}
            size = len(json.dumps(item).encode("utf-8"))
        bytes_used += size
        records.append(item)
    return {**info, "elements": records, "requested_count": len(records),
            "successful_count": sum(item["status"] == "ok" for item in records),
            "failed_count": sum(item["status"] == "error" for item in records),
            "all_elements_resolved": all(item["status"] == "ok" for item in records),
            "resolved_class_queries": len(candidates_cache), "profile": arguments["profile"]}


def calculation_objects(app, info, command_class):
    case = app.GetActiveStudyCase()
    if case is None or str(case.GetFullName()) != info["study_case"]["full_name"]:
        raise RequestError("SESSION_CHANGED", "The active study case changed; list commands again.", 409)
    # GetFromStudyCase can create an object; inspect only existing case contents.
    objects = case.GetContents("*." + command_class, 1) or []
    prefix = info["study_case"]["full_name"] + "\\"
    result = []
    for obj in objects:
        if obj is not None and str(obj.GetClassName()) == command_class:
            identity = describe(obj)
            if identity["full_name"].startswith(prefix):
                result.append((obj, identity))
    return result


def calculation_settings(obj, command_class, custom=None):
    checks = [availability(obj, name) for name in (custom or CALCULATION_SETTINGS[command_class])]
    names = [item["name"] for item in checks if item["available"] is not False]
    return {**attribute_data(obj, names, 32768),
            "excluded_attributes": [item["name"] for item in checks if item["available"] is False],
            "availability_errors": [item for item in checks if item["available"] is None],
            "coverage_complete": False, "coverage_note": "Selected settings only; other GUI options can affect execution."}


def list_calculation_commands(app, info, arguments):
    kinds = list(CALCULATION_CLASSES) if arguments["calculation"] == "all" else [arguments["calculation"]]
    commands, errors = [], []
    for kind in kinds:
        try:
            commands.extend((kind, obj, identity) for obj, identity in calculation_objects(app, info, CALCULATION_CLASSES[kind]))
        except Exception as exc:
            errors.append({"calculation": kind, "error": (type(exc).__name__ + ": " + str(exc))[:320]})
    commands.sort(key=lambda entry: (entry[0], entry[2]["full_name"]))
    offset, limit = arguments["offset"], arguments["limit"]
    page = [{"calculation": kind, "command": identity,
             "settings": calculation_settings(obj, identity["class"], arguments["settings_attributes"])}
            for kind, obj, identity in commands[offset:offset + limit]]
    return {**info, "commands": page, "matched_count": len(commands), "returned_count": len(page),
            "offset": offset, "limit": limit,
            "next_offset": offset + len(page) if offset + len(page) < len(commands) else None,
            "class_query_errors": errors, "scope": "Existing ComLdf/ComShc objects contained in the active study case."}


def calculation_validity(app, kind):
    method = CALCULATION_PROFILES[kind]["validity_method"]
    try:
        value = getattr(app, method)()
        if value not in (0, 1):
            raise ValueError("Expected 0 or 1.")
        return {"method": method, "valid": bool(value)}
    except Exception as exc:
        return {"method": method, "valid": None, "error": (type(exc).__name__ + ": " + str(exc))[:320]}


def calculation_outcome(response):
    code = response.get("return_code")
    execution = ("succeeded" if code == 0 else "failed") if type(code) is int else response.get("status", "indeterminate")
    validity = response.get("validity_after", {}).get("valid")
    results = response.get("results", [])
    requested = response.get("result_requested_count", len(results))
    successful = sum(item.get("data", {}).get("successful_count", 0) for item in results)
    missing = (len(results) != requested or any(item.get("status") != "ok" for item in results)
               or any(response.get(name, 0) for name in ("result_error_count", "result_attribute_error_count",
                                                        "result_availability_error_count", "result_excluded_attribute_count")))
    if not requested:
        quality = "not_requested"
    elif not response.get("results_read"):
        quality = "not_read" if response.get("status") != "succeeded" else "unavailable"
    elif missing:
        quality = "partial"
    elif response.get("result_metadata_error_count", 0):
        quality = "metadata_incomplete"
    else:
        quality = "complete"
    return {"execution": execution, "validity": "valid" if validity is True else "invalid" if validity is False else "unknown",
            "result_quality": quality, "requested_element_count": requested, "successful_attribute_count": successful,
            **({"settings_restoration": response["restoration"]["status"]} if "restoration" in response else {}),
            "note": "Separate execution, validity and read coverage; this is not independent engineering validation."}


def run_calculation(app, instance_id, calculation_records, info, tool, arguments):
    kind = CALCULATION_TOOLS[tool]
    request_id = arguments["request_id"]
    if arguments["expected_agent_instance_id"] != instance_id:
        raise RequestError("INSTANCE_MISMATCH", "The agent restarted; this calculation request belongs to an earlier session. Nothing was executed.", 409)
    if info["study_case"]["full_name"] != arguments["expected_study_case_full_name"]:
        raise RequestError("SESSION_CHANGED", "The active study case differs from the expected exact path; nothing was executed.", 409)
    signature = json.dumps({"tool": tool, "arguments": arguments}, sort_keys=True, ensure_ascii=False)
    if request_id in calculation_records:
        prior = calculation_records[request_id]
        if prior["signature"] != signature:
            raise RequestError("REQUEST_ID_CONFLICT", "This request_id already identifies different calculation arguments.", 409)
        return {**prior["response"], "replayed": True}
    if len(calculation_records) >= 32:
        raise RequestError("CALCULATION_HISTORY_FULL", "This agent has recorded 32 calculation attempts; start a new work session.", 409)
    matches = [(obj, identity) for obj, identity in calculation_objects(app, info, CALCULATION_CLASSES[kind])
               if identity["full_name"] == arguments["command_full_name"]]
    if not matches:
        raise RequestError("COMMAND_NOT_FOUND", "The exact command is not an existing object in the active study case. List commands again.", 404)
    if len(matches) != 1:
        raise RequestError("AMBIGUOUS_COMMAND", "More than one existing command has this path; nothing was executed.", 409)
    command, identity = matches[0]
    # Resolve every requested result element before allowing any calculation.
    cache = {}
    paths = [validate_path(path) for path in arguments["result_full_names"]]
    resolved = [resolve_element(app, path, cache) for path in paths]
    settings = calculation_settings(command, identity["class"])
    context_before = active_context(app, info)
    if session_info(app) != info:
        raise RequestError("SESSION_CHANGED", "The active session changed during preparation; nothing was executed.", 409)
    response = {**info, "request_id": request_id, "calculation": kind, "command": identity,
                "context_before": context_before,
                "status": "running", "success": False, "replayed": False,
                "started_at_utc": datetime.now(timezone.utc).isoformat(), "return_code": None,
                "settings_before": settings, "results": [], "results_read": False, "result_requested_count": len(paths),
                "effects": "Executes the existing command with its GUI settings. Results and automatic-control state may change. No explicit parameter writes, object creation, activation or database saves are performed.",
                "result_scope_note": "Result paths select reads, not fault locations. The existing ComShc settings define the fault method, type, impedance and locations."}
    calculation_records[request_id] = {"signature": signature, "response": response}
    started = time.perf_counter()
    try:
        code = command.Execute()
        response["return_code"] = code if type(code) is int else None
        response["execute_ms"] = round((time.perf_counter() - started) * 1000, 2)
        if type(code) is not int:
            response.update(status="indeterminate", error={"code": "UNEXPECTED_EXECUTE_RETURN", "message": "Execute returned " + type(code).__name__ + "; execution was attempted. Do not assume success or retry with a new ID."})
        elif code != 0:
            response.update(status="failed", error={"code": "CALCULATION_FAILED", "message": "Execute returned a nonzero code. Inspect PowerFactory's Output Window; stale results were not read."})
        else:
            after = session_info(app)
            response["context_after"] = active_context(app, after)
            if after != info or response["context_after"] != context_before:
                response.update(status="indeterminate", error={"code": "SESSION_CHANGED_DURING_EXECUTION", "message": "The calculation returned zero but the active session changed; no result values were read."})
            else:
                validity = calculation_validity(app, kind)
                response["validity_after"] = validity
                if validity["valid"] is False:
                    response.update(status="failed", error={"code": "RESULTS_INVALID", "message": "Execute returned zero, but PowerFactory reports invalid results; no stale values were read."})
                else:
                    response.update(status="succeeded", success=True)
                    for path, obj in zip(paths, resolved):
                        try:
                            names, selection = select_calculation_results(obj, path["element_class"], kind, arguments["result_attributes"])
                            data = get_element_attributes(app, info, {**path, "attributes": names, "profile": "basic"}, obj=obj, response_budget=49152)
                            data.update(selection)
                            data["results_note"] = "Read immediately after this command returned zero; validity and calculation scope must be checked. Availability alone does not establish per-element result applicability."
                            sample = {"element": data["element"], "status": "ok" if names else "skipped", "data": data}
                            if not names:
                                sample["reason"] = "NO_AVAILABLE_RESULT_ATTRIBUTES" if selection["candidate_attributes"] else "NO_DEFAULT_RESULT_PROFILE"
                            response["results"].append(sample)
                        except Exception as exc:
                            response["results"].append({"element": describe(obj), "status": "error", "error": {"code": "RESULT_READ_FAILED", "message": (type(exc).__name__ + ": " + str(exc))[:320]}})
                    response["results_read"] = any(item.get("data", {}).get("successful_count", 0) > 0 for item in response["results"])
    except Exception as exc:
        # Preserve an attempted calculation even when Execute raises. A retry
        # with this ID returns the record instead of executing for a second time.
        response.update(status="indeterminate", success=False, error={"code": "CALCULATION_EXECUTION_EXCEPTION", "message": (type(exc).__name__ + ": " + str(exc))[:320]})
    response["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
    if "context_after" not in response:
        try:
            response["context_after"] = active_context(app)
        except Exception as exc:
            response["context_after"] = None
            response["context_error"] = (type(exc).__name__ + ": " + str(exc))[:320]
    response.setdefault("execute_ms", round((time.perf_counter() - started) * 1000, 2))
    response["result_error_count"] = sum(item.get("status") == "error" or item.get("data", {}).get("failed_count", 0) > 0
                                         or bool(item.get("data", {}).get("availability_errors")) for item in response["results"])
    response["result_attribute_error_count"] = sum(item.get("data", {}).get("failed_count", 0) for item in response["results"])
    response["result_availability_error_count"] = sum(len(item.get("data", {}).get("availability_errors", [])) for item in response["results"])
    response["result_metadata_error_count"] = sum(item.get("data", {}).get("metadata_error_count", 0) for item in response["results"])
    response["result_excluded_attribute_count"] = sum(len(item.get("data", {}).get("excluded_attributes", [])) for item in response["results"])
    response["result_skipped_count"] = sum(item["status"] == "skipped" for item in response["results"])
    response["outcome"] = calculation_outcome(response)
    return response


def run_short_circuit_at_bus(app, instance_id, calculation_records, info, arguments):
    if arguments["expected_agent_instance_id"] != instance_id:
        raise RequestError("INSTANCE_MISMATCH", "The agent changed; no fault settings were written or calculation executed.", 409)
    if arguments["expected_study_case_full_name"] != info["study_case"]["full_name"]:
        raise RequestError("SESSION_CHANGED", "The active study case changed; nothing was written or executed.", 409)
    request_id = arguments["request_id"]
    signature = json.dumps({"tool": "run_short_circuit_at_bus", "arguments": arguments}, sort_keys=True, ensure_ascii=False)
    if request_id in calculation_records:
        prior = calculation_records[request_id]
        if prior["signature"] != signature:
            raise RequestError("REQUEST_ID_CONFLICT", "This request_id already identifies different calculation arguments.", 409)
        return {**prior["response"], "replayed": True}
    matches = [(obj, identity) for obj, identity in calculation_objects(app, info, "ComShc")
               if identity["full_name"] == arguments["command_full_name"]]
    if len(matches) != 1:
        raise RequestError("COMMAND_NOT_FOUND", "Select one exact existing ComShc in the active study case.", 404)
    command, identity = matches[0]
    bus = resolve_element(app, {"full_name": arguments["bus_full_name"], "element_class": "ElmTerm"})
    if not str(bus.GetFullName()).startswith(info["project"]["full_name"] + "\\"):
        raise RequestError("FAULT_BUS_OUT_OF_SCOPE", "Select a calculation-relevant bus inside the active project.", 409)
    bus_state = attribute_data(bus, ["outserv", "systype", "nphase", "uknom"])["attributes"]
    if any(bus_state[name].get("status") != "ok" or bus_state[name].get("value") != value
           for name, value in (("outserv", 0), ("systype", 0), ("nphase", 3))):
        raise RequestError("UNSUPPORTED_FAULT_BUS", "This workflow requires an in-service, three-phase AC ElmTerm with readable native state.", 409)
    settings = attribute_data(command, SHORT_CIRCUIT_FAULT_SETTINGS)
    fields = settings["attributes"]
    if not settings["all_attributes_read"]:
        raise RequestError("FAULT_SETTINGS_UNAVAILABLE", "The required PowerFactory 2024 fault settings could not all be read; nothing was written.", 409)
    values = {name: entry["value"] for name, entry in fields.items()}
    if (type(values["iopt_mde"]) is not int or values["iopt_mde"] not in (1, 3)
            or type(values["iopt_cnf"]) is not int or values["iopt_cnf"] != 0
            or type(values["iEnhancedZf"]) is not int or values["iEnhancedZf"] != 0):
        raise RequestError("UNSUPPORTED_FAULT_MODE", "Use an existing IEC 60909 or Complete command with multiple faults and enhanced fault impedance disabled. This tool preserves method and impedance settings.", 409)
    context = active_context(app, info)
    if not context["context_complete"]:
        raise RequestError("CONTEXT_INCOMPLETE", "Resolve active-session context errors before configuring a fault.", 409)
    requested = {"iopt_allbus": 0, "shcobj": json_attribute_value(bus), "iopt_shc": arguments["fault_type"]}
    changes = [{"attribute": name, "before": fields[name], "after": value} for name, value in requested.items()]
    preview = {"agent_instance_id": instance_id, "context": context, "command": identity,
               "bus": describe(bus), "bus_state": bus_state, "changes": changes,
               "settings_before": fields, "settings_coverage_complete": False,
               "result_full_names": arguments["result_full_names"],
               "result_attributes": arguments["result_attributes"], "request_id": request_id,
               "scope": "Temporarily set only iopt_allbus, shcobj and iopt_shc; execute once; read results before restoring those settings. Other command options remain configured.",
               "restoration_note": "Restoring command settings does not restore prior calculation results or automatic-control state. Stored samples describe this execution; later native validity can change."}
    digest = hashlib.sha256(json.dumps(preview, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()
    if active_context(app, info) != context:
        raise RequestError("SESSION_CHANGED", "The active context changed during preparation; nothing was written or executed.", 409)
    if arguments["confirmation_digest"] is None:
        return {**info, "status": "preview", "preview": preview, "confirmation_digest": digest,
                "write_attempted": False, "execution_attempted": False, "success": None}
    if arguments["confirmation_digest"] != digest:
        raise RequestError("FAULT_PREVIEW_CHANGED", "The reviewed target, settings or context changed. Request a new preview; nothing was written or executed.", 409)
    if len(calculation_records) >= 32:
        raise RequestError("CALCULATION_HISTORY_FULL", "This agent has recorded 32 calculation attempts; no fault settings were written.", 409)
    # Resolve sample paths and preserve native references before the first setter.
    for path in arguments["result_full_names"]:
        resolve_element(app, validate_path(path))
    original = {name: command.GetAttribute(name) for name in requested}
    if any(json_attribute_value(original[name]) != values[name] for name in original):
        raise RequestError("FAULT_PREVIEW_CHANGED", "Fault settings changed before assignment; nothing was written or executed.", 409)
    response = {**info, "request_id": request_id, "calculation": "short_circuit", "command": identity,
                "context_before": context, "status": "configuring", "success": False, "replayed": False,
                "started_at_utc": datetime.now(timezone.utc).isoformat(), "return_code": None,
                "write_attempted": False, "execution_attempted": False, "results": [], "results_read": False,
                "result_requested_count": len(arguments["result_full_names"]),
                "fault_configuration": {"preview": preview, "confirmation_digest": digest, "verified": False}}
    calculation_records[request_id] = {"signature": signature, "response": response}
    attempted = []
    try:
        for name, value in requested.items():
            if values[name] != value:
                attempted.append(name)  # A setter can write and then raise.
                response["write_attempted"] = True
                command.SetAttribute(name, bus if name == "shcobj" else value)
        configured = attribute_data(command, SHORT_CIRCUIT_FAULT_SETTINGS)
        expected = {**values, **requested}
        if (not configured["all_attributes_read"]
                or any(configured["attributes"][name]["value"] != value for name, value in expected.items())
                or active_context(app, info) != context):
            raise ValueError("Fault settings or context did not match the confirmed configuration; Execute was not called.")
        response["fault_configuration"].update(verified=True, settings_at_execution=configured["attributes"])
        response["status"] = "running"
        base = {key: value for key, value in arguments.items() if key not in ("bus_full_name", "fault_type", "confirmation_digest")}
        execution_records = {}
        try:
            run_calculation(app, instance_id, execution_records, info, "run_short_circuit", base)
        finally:
            response["execution_attempted"] = request_id in execution_records
            if request_id in execution_records:
                response.update(execution_records[request_id]["response"])
    except Exception as exc:
        response.update(status="indeterminate" if response["execution_attempted"] else "not_executed", success=False,
                        error={"code": "FAULT_WORKFLOW_FAILED", "message": (type(exc).__name__ + ": " + str(exc))[:320]})
    finally:
        execution_success = response["success"]
        restoration_errors = []
        for name in reversed(attempted):
            try:
                command.SetAttribute(name, original[name])
            except Exception as exc:
                restoration_errors.append({"attribute": name, "error": (type(exc).__name__ + ": " + str(exc))[:320]})
        restored = attribute_data(command, SHORT_CIRCUIT_FAULT_SETTINGS)
        settings_restored = (restored["all_attributes_read"]
                             and all(restored["attributes"][name]["value"] == value for name, value in values.items()))
        response["restoration"] = {"status": "restored" if settings_restored else "failed", "success": settings_restored,
                                   "settings_after": restored["attributes"], "errors": restoration_errors,
                                   "prior_results_restored": False}
        response["execution_success"] = execution_success
        response["success"] = execution_success and settings_restored
        response["effects"] = preview["scope"] + " " + preview["restoration_note"]
        response["result_scope_note"] = "Fault target/type are recorded in fault_configuration. Result paths select sample reads only."
        response["validity_after_restoration"] = calculation_validity(app, "short_circuit")
        response["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
        response["outcome"] = calculation_outcome(response)
    return response


def list_model_elements(app, info, arguments):
    network = arguments["network_full_name"]
    if network is not None:
        resolve_element(app, {"full_name": network, "element_class": "ElmNet"})
    objects = app.GetCalcRelevantObjects("*." + arguments["element_class"])
    needle = arguments["name_contains"].casefold()
    minimum, maximum = arguments["nominal_voltage_min_kv"], arguments["nominal_voltage_max_kv"]
    in_service = arguments["in_service"]
    items, errors, error_count = [], [], 0
    for obj in objects or []:
        if obj is not None and needle in str(obj.loc_name).casefold():
            identity = describe(obj)
            if network is not None and identity["full_name"] != network and not identity["full_name"].startswith(network + "\\"):
                continue
            try:
                if in_service is not None:
                    attribute = "outserv"
                    outserv = obj.GetAttribute(attribute)
                    if outserv not in (0, 1):
                        raise ValueError("outserv must be 0 or 1.")
                    if (outserv == 0) != in_service:
                        continue
                if minimum is not None or maximum is not None:
                    attribute = "uknom"
                    value = obj.GetAttribute(attribute)
                    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                        raise ValueError("uknom must be a finite non-negative number.")
                    unit = obj.GetAttributeUnit(attribute).strip()
                    if unit not in ("V", "kV", "MV"):
                        raise ValueError("uknom requires a reported V, kV or MV unit.")
                    voltage_kv = value * {"V": 0.001, "kV": 1, "MV": 1000}[unit]
                    if minimum is not None and voltage_kv < minimum or maximum is not None and voltage_kv > maximum:
                        continue
            except Exception as exc:
                error_count += 1
                if len(errors) < 50:
                    errors.append({"element": identity, "attribute": attribute,
                                   "error": {"code": "FILTER_READ_FAILED", "message": (type(exc).__name__ + ": " + str(exc))[:320]}})
                continue
            items.append(identity)
    items.sort(key=lambda item: (item["name"].casefold(), item["full_name"]))
    offset, limit = arguments["offset"], arguments["limit"]
    page = items[offset:offset + limit]
    following = offset + len(page)
    result = {
        **info,
        "scope": "calculation-relevant elements in the active study case",
        "element_class": arguments["element_class"],
        "name_contains": arguments["name_contains"],
        "matched_count": len(items), "returned_count": len(page),
        "offset": offset, "limit": limit,
        "next_offset": following if following < len(items) else None,
        "elements": page,
    }
    if any(value is not None for value in (network, in_service, minimum, maximum)):
        result.update(filters={"network_full_name": network, "in_service": in_service,
                               "nominal_voltage_min_kv": minimum, "nominal_voltage_max_kv": maximum},
                      filter_error_count=error_count, filter_errors=errors,
                      filter_errors_truncated=error_count > len(errors), filtering_complete=error_count == 0)
    return result


def setpoint_recording_destination(app, info, obj, context, names):
    relevant = {name: app.IsScenarioAttribute(str(obj.GetClassName()), name) for name in names}
    if any(type(value) is not int or value != 1 for value in relevant.values()):
        raise RequestError("SETPOINT_NOT_OPERATIONAL", "The native scenario configuration does not classify every selected attribute as operational.", 409)
    grid = obj
    for _ in range(64):
        if grid is None or str(grid.GetClassName()) == "ElmNet":
            break
        grid = grid.GetParent()
    if grid is None or str(grid.GetClassName()) != "ElmNet" or not str(grid.GetFullName()).startswith(info["project"]["full_name"] + "\\"):
        raise RequestError("SETPOINT_GRID_UNAVAILABLE", "The element's exact grid in the active project could not be established.", 409)
    destination = {"grid": describe(grid), "scenario_attributes": relevant,
                   "persistence_note": "No explicit Save is called. Native auto-save settings can persist edits; restoring values later does not undo save history."}
    scenario = app.GetActiveScenario()
    if describe(scenario) != context["scenario"]:
        raise RequestError("SESSION_CHANGED", "The active scenario changed during recording-destination inspection.", 409)
    if scenario is not None:
        subsets = [subset for subset in scenario.GetContents("*.IntSubset", 1) or [] if subset is not None
                   and str(subset.GetFullName()).startswith(str(scenario.GetFullName()) + "\\")
                   and describe(subset.GetAttribute("pGrid")) == describe(grid)]
        excluded = subsets[0].GetAttribute("iExcluded") if len(subsets) == 1 else None
        if type(excluded) is not int or excluded != 0:
            raise RequestError("SCENARIO_RECORDING_UNVERIFIED", "Require one non-excluded grid subset in the active scenario. Excluded, missing, ambiguous and type-based subsets are unsupported.", 409)
        return {**destination, "kind": "active_scenario", "object": describe(scenario), "subset": describe(subsets[0])}
    if context["recording_stage"] is not None:
        return {**destination, "kind": "recording_stage", "object": context["recording_stage"]}
    if context["active_variations"]:
        raise RequestError("SETPOINT_RECORDING_UNVERIFIED", "Active variations have no verified recording stage; nothing was written.", 409)
    return {**destination, "kind": "base_grid", "object": describe(grid)}


def setpoint_readback_matches(entry, value, unit):
    return (entry.get("status") == "ok" and type(entry.get("value")) in (int, float)
            and math.isclose(entry["value"], value, rel_tol=1e-12, abs_tol=1e-12)
            and (entry.get("unit") or "").strip() == unit.strip())


def set_operating_setpoints(app, instance_id, info, arguments):
    if arguments["expected_agent_instance_id"] != instance_id:
        raise RequestError("INSTANCE_MISMATCH", "The agent changed; no setpoint was written.", 409)
    if arguments["expected_study_case_full_name"] != info["study_case"]["full_name"]:
        raise RequestError("SESSION_CHANGED", "The active study case changed; no setpoint was written.", 409)
    context = active_context(app, info)
    if not context["context_complete"]:
        raise RequestError("CONTEXT_INCOMPLETE", "Resolve active-session context errors before setpoint changes.", 409)
    for key, expected in (("scenario", "expected_active_scenario_full_name"), ("recording_stage", "expected_recording_stage_full_name")):
        actual = (context[key] or {}).get("full_name")
        if actual != arguments[expected] or actual is not None and not actual.startswith(info["project"]["full_name"] + "\\"):
            raise RequestError("SETPOINT_CONTEXT_CHANGED", "The expected scenario or recording stage differs from the active context.", 409)
    path = validate_path(arguments["full_name"])
    obj = resolve_element(app, path)
    if not path["full_name"].startswith(info["project"]["full_name"] + "\\"):
        raise RequestError("SETPOINT_OUT_OF_SCOPE", "Select an exact calculation-relevant element in the active project.", 409)
    names = OPERATING_SETPOINTS[path["element_class"]]
    before = attribute_data(obj, names)["attributes"]
    if any(entry.get("status") != "ok" or type(entry.get("value")) not in (int, float) for entry in before.values()):
        raise RequestError("SETPOINT_UNAVAILABLE", "Both native P/Q inputs must be readable scalar numbers before a write.", 409)
    changes = arguments["setpoints"]
    for change in changes:
        current = before[change["attribute"]]
        if current.get("status") != "ok" or type(current.get("value")) not in (int, float):
            raise RequestError("SETPOINT_UNAVAILABLE", "Each selected native setpoint must be a readable scalar number.", 409)
        if current.get("unit") is None or current["unit"].strip() != change["unit"]:
            raise RequestError("SETPOINT_UNIT_MISMATCH", "Use exactly the physical unit reported for this attribute. No unit conversion is performed.", 409)
        if current["value"] != change["expected_value"]:
            raise RequestError("SETPOINT_CHANGED", "A native setpoint differs from its expected value; no setpoint was written.", 409)
    controls = control_attribute_data(obj, SETPOINT_CONTROLS)
    input_mode = controls["attributes"].get("mode_inp", {})
    if input_mode.get("status") != "ok" or input_mode.get("value") not in ("PQ", "DEF"):
        raise RequestError("UNSUPPORTED_SETPOINT_MODE", "This workflow requires native PQ or DEF input mode; input/controller modes are not changed.", 409)
    mode = input_mode["value"]
    characteristics = [describe(ref) for ref in obj.GetContents("*.ChaRef", 0) or []
                       if ref is not None and str(ref.loc_name) in {change["attribute"] for change in changes}]
    if characteristics:
        raise RequestError("SETPOINT_CHARACTERISTIC_ASSIGNED", "A selected input has a parameter characteristic; characteristic-driven inputs are unsupported.", 409)
    destination = setpoint_recording_destination(app, info, obj, context, [change["attribute"] for change in changes])
    preview = {"agent_instance_id": instance_id, "context": context, "element": describe(obj),
               "setpoints": changes, "parameters_before": before, "recording_destination": destination,
               "controls": {key: value for key, value in controls.items() if key != "attribute_read_ms"},
               "input_mode_verification": {"mode": mode,
                   "note": "DEF retains PowerFactory's native default input selection; it is not assumed to mean PQ. Verification covers stored P/Q inputs, not controller-achieved power."},
               "readback_tolerance": {"relative": 1e-12, "absolute_in_reported_unit": 1e-12},
               "scope": "Write only the selected stored P/Q inputs. Controllers, scaling and subsequent calculations can alter achieved P/Q. No calculation, activation, explicit save or automatic rollback is requested. Multiple writes are not atomic."}
    digest = hashlib.sha256(json.dumps(preview, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()
    if active_context(app, info) != context:
        raise RequestError("SESSION_CHANGED", "The active context changed during the preview; no setpoint was written.", 409)
    if arguments["confirmation_digest"] is None:
        return {**info, "status": "preview", "preview": preview, "confirmation_digest": digest,
                "write_attempted": False, "success": None}
    if arguments["confirmation_digest"] != digest:
        raise RequestError("SETPOINT_PREVIEW_CHANGED", "The reviewed values, units, controller metadata or context changed; request a new preview. Nothing was written.", 409)
    attempted, errors = [], []
    validity_before = {kind: calculation_validity(app, kind) for kind in ("power_flow", "short_circuit")}
    try:
        for change in changes:
            name = change["attribute"]
            current = read_attribute(obj, name)
            if (active_context(app, info) != context or obj.GetAttribute("mode_inp") != mode or current.get("status") != "ok"
                    or current.get("value") != change["expected_value"] or (current.get("unit") or "").strip() != change["unit"]):
                raise ValueError("Context, input mode, native value or unit changed before assignment; remaining setters were skipped.")
            if change["value"] != change["expected_value"]:
                attempted.append(name)  # A setter can write and then raise.
                obj.SetAttribute(name, change["value"])
                observed = read_attribute(obj, name)
                if not setpoint_readback_matches(observed, change["value"], change["unit"]):
                    raise ValueError("Native setpoint readback did not match; remaining setters were skipped.")
    except Exception as exc:
        errors.append({"code": "SETPOINT_WRITE_FAILED", "message": (type(exc).__name__ + ": " + str(exc))[:320]})
    after = attribute_data(obj, names)["attributes"]
    mode_after = read_attribute(obj, "mode_inp")
    mode_unchanged = mode_after.get("status") == "ok" and mode_after.get("value") == mode
    try:
        context_after = active_context(app)
    except Exception as exc:
        context_after = None
        errors.append({"code": "SETPOINT_CONTEXT_UNAVAILABLE", "message": (type(exc).__name__ + ": " + str(exc))[:320]})
    targets = {change["attribute"]: change["value"] for change in changes}
    verified = all(setpoint_readback_matches(after[name], targets.get(name, before[name]["value"]), before[name].get("unit") or "") for name in names)
    if not verified:
        errors.append({"code": "SETPOINT_READBACK_MISMATCH", "message": "Final native P/Q values or units differ from the requested/unchanged values; inspect parameters_after."})
    if context_after is not None and context_after != context:
        errors.append({"code": "SETPOINT_CONTEXT_CHANGED", "message": "The active context differs after the operation; inspect context_after."})
    if not mode_unchanged:
        errors.append({"code": "SETPOINT_MODE_CHANGED", "message": "The native input mode changed or could not be read after the operation; no mode restoration was attempted."})
    success = not errors and verified and mode_unchanged and context_after == context
    return {**info, "element": describe(obj), "status": "succeeded" if success else "partial_or_unverified",
            "success": success, "write_attempted": bool(attempted), "attempted_attributes": attempted,
            "preview": preview, "confirmation_digest": digest, "parameters_after": after,
            "readback_verified": verified, "input_mode_after": mode_after, "input_mode_unchanged": mode_unchanged,
            "context_after": context_after, "errors": errors,
            "validity_before": validity_before,
            "validity_after": {kind: calculation_validity(app, kind) for kind in validity_before},
            "rollback_attempted": False, "note": "Inspect all native readbacks after any failure. Changes already made remain applied. Restore through a new explicit request using current expected values; native auto-save may have persisted changes."}


def set_study_time(app, instance_id, info, arguments):
    if arguments["expected_agent_instance_id"] != instance_id:
        raise RequestError("INSTANCE_MISMATCH", "The agent restarted; study time was not changed.", 409)
    if arguments["expected_study_case_full_name"] != info["study_case"]["full_name"]:
        raise RequestError("SESSION_CHANGED", "The active study case changed; study time was not changed.", 409)
    before = active_context(app, info)
    prior = (before.get("study_time") or {}).get("utc_seconds", {})
    if prior.get("status") != "ok" or type(prior.get("value")) is not int:
        raise RequestError("STUDY_TIME_UNAVAILABLE", "The current native study time could not be verified; nothing was changed.", 409)
    if prior["value"] != arguments["expected_study_time_seconds"]:
        raise RequestError("STUDY_TIME_CHANGED", "The current study time differs from the expected epoch seconds; nothing was changed.", 409)
    obj = app.GetStudyTimeObject()
    if describe(obj) != before["study_time"]["object"] or not str(obj.GetFullName()).startswith(info["study_case"]["full_name"] + "\\"):
        raise RequestError("STUDY_TIME_SCOPE_MISMATCH", "The study-time object is not the expected object in the active case.", 409)
    validity_before = calculation_validity(app, "power_flow")
    attempted = prior["value"] != arguments["utc_seconds"]
    error = None
    if attempted:
        try:
            result = obj.SetTimeUTC(arguments["utc_seconds"])
            if result is not None:
                error = {"code": "UNEXPECTED_STUDY_TIME_RETURN", "message": "SetTimeUTC did not return its documented None value; inspect the resulting time."}
        except Exception as exc:
            error = {"code": "STUDY_TIME_CHANGE_EXCEPTION", "message": (type(exc).__name__ + ": " + str(exc))[:320]}
    try:
        after = active_context(app)
        actual = (after.get("study_time") or {}).get("utc_seconds", {})
    except Exception as exc:
        raise RequestError("STUDY_TIME_OUTCOME_UNKNOWN", "Could not read context after the request; read get_active_session before another attempt: " + str(exc)[:320], 409) from exc
    verified = (after["project"] == info["project"] and after["study_case"] == info["study_case"]
                and actual.get("status") == "ok" and actual.get("value") == arguments["utc_seconds"])
    if not verified and error is None:
        error = {"code": "STUDY_TIME_CHANGE_NOT_VERIFIED", "message": "The requested native time was not observed in the expected project/case."}
    return {"project": after["project"], "study_case": after["study_case"],
            "requested_timestamp_utc": arguments["timestamp_utc"], "requested_utc_seconds": arguments["utc_seconds"],
            "change_attempted": attempted, "already_in_requested_state": not attempted,
            "success": verified and error is None, "error": error, "context_before": before, "context_after": after,
            "validity_before": validity_before, "validity_after": calculation_validity(app, "power_flow"),
            "effects": "Changes study time and can change applicable stages, characteristics and outages or invalidate results. No calculation, database save, automatic retry or rollback is performed."}


def get_power_balance(app, info, arguments):
    network = arguments["network_full_name"]
    obj = (resolve_element(app, {"full_name": network, "element_class": "ElmNet"})
           if network is not None else app.GetSummaryGrid())
    if obj is None:
        raise RequestError("NO_ACTIVE_GRID", "No summary grid is available for this study case.", 409)
    identity = describe(obj)
    if identity["class"] != "ElmNet" or not identity["full_name"].startswith(info["project"]["full_name"] + "\\"):
        raise RequestError("NETWORK_SCOPE_MISMATCH", "The selected grid is outside the active project.", 409)
    context = active_context(app, info)
    before = calculation_validity(app, "power_flow")
    prefixes = ("Gen", "Net", "Load", "Mot", "Loss", "Inter", "Import", "Export")
    names = ["c:" + prefix + suffix for prefix in prefixes for suffix in ("P", "Q")] + ["c:CompP", "c:CompQ", "c:ComprQ"]
    data = attribute_data(obj, names)
    if active_context(app, info) != context:
        raise RequestError("SESSION_CHANGED", "The active context changed during the power-balance read.", 409)
    after = calculation_validity(app, "power_flow")
    return {**info, "context": context, "network": identity, "network_full_name": network,
            "scope": "Native summary grid of all active grids" if network is None else "Native totals for the selected ElmNet",
            **data, "validity_before": before, "validity_after": after,
            "coverage_note": "Native PowerFactory attributes and reported units only. Summary-grid totals are read once. No unit conversion, residual calculation or engineering assessment is performed."}


def control_attribute_data(obj, names):
    checks = [availability(obj, name) for name in names]
    return {**attribute_data(obj, [item["name"] for item in checks if item["available"] is not False]),
            "excluded_attributes": [item["name"] for item in checks if item["available"] is False],
            "availability_errors": [item for item in checks if item["available"] is None],
            "coverage_note": "Focused candidate fields only; absence is not a false/zero control state."}


def get_transformer_control_status(app, info, arguments):
    obj = resolve_element(app, arguments)
    context = active_context(app, info)
    before = calculation_validity(app, "power_flow")
    two_winding = arguments["element_class"] == "ElmTr2"
    tap_names = ["nntap", "nntap2"] if two_winding else ["n3tap_h", "n3tap_m", "n3tap_l"]
    names = ["outserv", "typ_id", *tap_names, "ntrcn", "ntrcn2", "i_cont", "imldc", "t2ldc", "t2ldc2",
             "iMeasLoc", "i_rem", "p_rem", "p_cub", "uset_mode", "usetp", "usp_low", "usp_up",
             "usetp2", "usp_low2", "usp_up2", "c_ptapc", "c_pstac"]
    instance = control_attribute_data(obj, names)
    equipment_type, type_error = None, None
    try:
        typ, identity = resolve_equipment_type(obj, arguments["element_class"])
        type_names = (["tap_side", "oltc", "nntap0", "ntpmn", "ntpmx", "dutap"] if two_winding else
                      [prefix + side for side in ("h", "m", "l") for prefix in ("n3tp0_", "n3tmn_", "n3tmx_", "du3tp_")])
        equipment_type = {"object": identity, **control_attribute_data(typ, type_names)}
    except RequestError as exc:
        type_error = {"code": exc.code, "message": str(exc)}
    controllers = []
    for name, expected_class in (("c_ptapc", "ElmTapctrl"), ("c_pstac", "ElmStactrl")):
        record = instance["attributes"].get(name, {})
        reference = record.get("value")
        if record.get("status") == "ok" and isinstance(reference, dict) and reference.get("class") == expected_class:
            controller = obj.GetAttribute(name)
            if describe(controller)["full_name"] != reference["full_name"]:
                raise RequestError("CONTROLLER_CHANGED", "A transformer control reference changed during inspection.", 409)
            controllers.append({"reference_attribute": name, "object": describe(controller),
                                **control_attribute_data(controller, ["loc_name", "outserv", "i_ctrl", "usetp", "usp_low", "usp_up", "p_cub", "rembar"])})
    solved = control_attribute_data(obj, ["c:" + name for name in tap_names]) if before["valid"] is True else None
    commands = list_calculation_commands(app, info, {"calculation": "power_flow", "offset": 0, "limit": 20,
                                                   "settings_attributes": ["iopt_net", "iopt_at"]})
    if active_context(app, info) != context:
        raise RequestError("SESSION_CHANGED", "The active context changed during transformer inspection.", 409)
    after = calculation_validity(app, "power_flow")
    if after["valid"] is not True:
        solved = None
    return {**info, "context": context, "transformer": describe(obj), "instance": instance,
            "equipment_type": equipment_type, "type_error": type_error, "controllers": controllers,
            "solved_tap_results": solved, "validity_before": before, "validity_after": after,
            "load_flow_commands": commands, "control_authority": "not_inferred",
            "note": "Configured taps, solved tap results, type limits, controller references and command settings are separate facts. ntrcn is automatic tap changing; i_auto is the autotransformer design flag. Type limits may differ from operational limits. An enabled flag does not prove controller authority or achieved regulation. Controller reads follow only the two named references once. No tap, control setting or calculation was changed."}


def get_line_loading(app, info, arguments):
    return read_result_data(app, info, arguments, ("ElmLne",), "lines", ["outserv", "c:loading"])


def get_transformer_loading(app, info, arguments):
    return read_result_data(app, info, arguments, ("ElmTr2", "ElmTr3"), "transformers", ["outserv", "c:loading"])


def get_bus_voltage(app, info, arguments):
    return read_result_data(app, info, arguments, ("ElmTerm",), "buses", ["outserv", VOLTAGE_QUANTITIES[arguments["quantity"]]])


def get_bus_voltage_sequences(app, info, arguments):
    return read_result_data(app, info, arguments, ("ElmTerm",), "buses", ["outserv", "nphase", "systype", "m:u1", "m:u2"])


def read_result_data(app, info, arguments, element_classes, result_key, names):
    network = arguments["network_full_name"]
    if network is not None:
        resolve_element(app, {"full_name": network, "element_class": "ElmNet"})
    context = active_context(app, info)
    before = calculation_validity(app, "power_flow")
    needle = arguments["name_contains"].casefold()
    entries = []
    for element_class in element_classes:
        for obj in app.GetCalcRelevantObjects("*." + element_class) or []:
            if obj is None:
                continue
            identity = describe(obj)
            if needle not in identity["name"].casefold():
                continue
            if network is not None and not identity["full_name"].startswith(network + "\\"):
                continue
            entries.append((identity, obj))
    entries.sort(key=lambda item: item[0]["full_name"])
    offset, limit = arguments["offset"], arguments["limit"]
    rows = []
    for identity, obj in entries[offset:offset + limit]:
        row = {"element": identity, **attribute_data(obj, names, response_budget=8192)}
        if element_classes == ("ElmTerm",):
            try:
                row["energized"] = {"method": "IsEnergized", "value": json_attribute_value(obj.IsEnergized())}
            except Exception as exc:
                row["energized"] = {"method": "IsEnergized", "error": (type(exc).__name__ + ": " + str(exc))[:320]}
        rows.append(row)
    if session_info(app) != info or active_context(app, info) != context:
        raise RequestError("SESSION_CHANGED", "The active session changed during the result read.", 409)
    after = calculation_validity(app, "power_flow")
    following = offset + len(rows)
    return {**info, "context": context, "network_full_name": network, "name_contains": arguments["name_contains"],
            "result_attributes": names, **({"quantity": arguments["quantity"]} if "quantity" in arguments else {}),
            "validity_before": before, "validity_after": after,
            "matched_count": len(entries), "returned_count": len(rows), "offset": offset, "limit": limit,
            "next_offset": following if following < len(entries) else None, result_key: rows,
            "results_note": "Existing native values and units only, including out-of-service elements. Native validity flags are returned without suppressing readable values. No calculation, threshold comparison, ratio, ranking or engineering assessment is performed."}


def capture_result_snapshot(app, info, arguments, instance_id, record=None):
    names, paths = arguments["result_attributes"], arguments["full_names"]
    if record is not None:
        if record["calculation"] != arguments["calculation"]:
            raise RequestError("SNAPSHOT_CALCULATION_MISMATCH", "The stored calculation kind differs from the requested kind.", 409)
        info = {key: record[key] for key in ("project", "study_case")}
        context = record.get("context_after")
        before = after = record.get("validity_after", {"valid": None})
        stored = {item["element"]["full_name"]: item for item in record["results"] if "data" in item}
    else:
        context = active_context(app, info)
        before = calculation_validity(app, arguments["calculation"])
    elements, cache = [], {}
    for path in paths:
        if record is not None:
            data = stored.get(path, {}).get("data", {})
            if not all(name in data.get("attributes", {}) for name in names):
                raise RequestError("SNAPSHOT_RESULT_NOT_RECORDED", "The stored attempt does not contain every requested result for " + path, 409)
            elements.append({"full_name": path, "status": "ok", "element": stored[path]["element"],
                             "attributes": {name: data["attributes"][name] for name in names}})
            continue
        item = {"full_name": path, "attributes": {}}
        try:
            if before["valid"] is False:
                raise RequestError("RESULTS_INVALID", "Results are invalid; no values were captured.", 409)
            selected = {**validate_path(path), "attributes": names, "profile": "basic"}
            obj = resolve_element(app, selected, cache)
            data = get_element_attributes(app, info, selected, obj=obj, response_budget=49152)
            item.update(status="ok", element=data["element"], attributes=data["attributes"])
        except RequestError as exc:
            item.update(status="error", error={"code": exc.code, "message": str(exc)})
        except Exception as exc:
            item.update(status="error", error={"code": "SNAPSHOT_READ_FAILED", "message": (type(exc).__name__ + ": " + str(exc))[:320]})
        elements.append(item)
    if record is None:
        if session_info(app) != info or active_context(app, info) != context:
            raise RequestError("SESSION_CHANGED", "The active session changed during capture; no snapshot was stored.", 409)
        after = calculation_validity(app, arguments["calculation"])
        if after["valid"] is False:
            for item in elements:
                item.update(status="error", attributes={}, error={"code": "RESULTS_INVALID", "message": "Values discarded because results became invalid."})
    successful = sum(value.get("status") == "ok" for item in elements for value in item["attributes"].values())
    failed = len(paths) * len(names) - successful
    metadata_errors = sum(len(value.get("metadata_errors", {})) for item in elements for value in item["attributes"].values())
    return {**info, "context": context, "snapshot_id": arguments["snapshot_id"], "agent_instance_id": instance_id,
            "captured_at_utc": datetime.now(timezone.utc).isoformat(), "calculation": arguments["calculation"],
            "source": "stored_calculation_record" if record is not None else "live_result_read",
            "calculation_request_id": arguments["calculation_request_id"],
            "data_recorded_at_utc": record.get("completed_at_utc") if record is not None else None,
            "source_outcome": calculation_outcome(record) if record is not None else None,
            "full_names": paths, "result_attributes": names, "elements": elements,
            "validity_before": before, "validity_after": after,
            "successful_attribute_count": successful, "failed_attribute_count": failed, "metadata_error_count": metadata_errors,
            "snapshot_complete": (not failed and not metadata_errors and context is not None and context.get("context_complete") is True
                                  and before["valid"] is True and after["valid"] is True
                                  and (record is None or record.get("success") is True)),
            "note": "Immutable session-only plain data. Validity is historical; no independent freshness or model-version guarantee. No calculation was run by capture."}


def network_variation_state(app, info):
    folder = app.GetProjectFolder("scheme", 0)
    folder_info = describe(folder)
    if folder_info is not None and not folder_info["full_name"].startswith(info["project"]["full_name"] + "\\"):
        raise RequestError("VARIATION_SCOPE_MISMATCH", "The variations folder is outside the active project.", 409)
    entries = []
    objects = (folder.GetContents("*.IntScheme", 1) or []) if folder is not None else []
    for obj in objects:
        identity = describe(obj)
        if identity is None or identity["class"] != "IntScheme" or not identity["full_name"].startswith(folder_info["full_name"] + "\\"):
            raise RequestError("VARIATION_SCOPE_MISMATCH", "A returned object is outside the network-variations folder or is not an IntScheme.", 409)
        entries.append((obj, identity))
    paths = {identity["full_name"] for obj, identity in entries}
    if len(paths) != len(entries):
        raise RequestError("AMBIGUOUS_VARIATION", "More than one network variation has the same exact path.", 409)
    active_objects = app.GetActiveNetworkVariations()
    if not isinstance(active_objects, (list, tuple)) or any(obj is None for obj in active_objects):
        raise RequestError("VARIATION_STATUS_UNAVAILABLE", "PowerFactory did not return an active-variation list; no inactive states were inferred.", 409)
    active = {str(obj.GetFullName()) for obj in active_objects}
    if not active.issubset(paths):
        raise RequestError("VARIATION_STATUS_INCOMPLETE", "An active variation is missing from the visible network-variations folder; no complete status listing is available.", 409)
    if session_info(app) != info:
        raise RequestError("SESSION_CHANGED", "The active context changed while reading network variations.", 409)
    entries.sort(key=lambda item: (item[1]["name"].casefold(), item[1]["full_name"]))
    return folder_info, entries, active


def list_network_variations(app, info, arguments):
    folder, entries, active = network_variation_state(app, info)
    rows = [{**identity, "active": identity["full_name"] in active} for obj, identity in entries
            if arguments["name_contains"].casefold() in identity["name"].casefold()]
    offset, limit = arguments["offset"], arguments["limit"]
    page = rows[offset:offset + limit]
    following = offset + len(page)
    return {**info, "folder": folder, "total_count": len(entries), "active_count": len(active),
            "inactive_count": len(entries) - len(active), "matched_count": len(rows),
            "name_contains": arguments["name_contains"], "offset": offset, "limit": limit,
            "returned_count": len(page), "next_offset": following if following < len(rows) else None,
            "variations": page, "status_source": "GetActiveNetworkVariations",
            "note": "Includes network variations in nested folders of the active project. Active means the variation is enabled in the current study case; expansion-stage applicability also depends on study time. No activation or calculation is performed."}


def list_expansion_stages(app, info, arguments):
    _, variations, enabled = network_variation_state(app, info)
    matches = [(obj, identity) for obj, identity in variations if identity["full_name"] == arguments["variation_full_name"]]
    if not matches:
        raise RequestError("VARIATION_NOT_FOUND", "Select an exact network variation from list_network_variations in the active project.", 404)
    variation, identity = matches[0]
    context = active_context(app, info)
    if context["active_variations"] is None or {item["full_name"] for item in context["active_variations"]} != enabled:
        raise RequestError("SESSION_CHANGED", "The active variation set could not be verified consistently; query again.", 409)

    def read_state():
        state, errors = {}, {}
        try:
            folder = app.GetProjectFolder("netdat", 0)
            if folder is None or not str(folder.GetFullName()).startswith(info["project"]["full_name"] + "\\"):
                raise ValueError("The active project's Network Data folder is unavailable.")
            objects = app.GetActiveStages(folder)
            if not isinstance(objects, (list, tuple)) or any(obj is None or str(obj.GetClassName()) != "IntSstage" for obj in objects):
                raise ValueError("GetActiveStages did not return a stage list.")
            state["active_paths"] = {str(obj.GetFullName()) for obj in objects}
        except Exception as exc:
            state["active_paths"] = None
            errors["active_stages"] = (type(exc).__name__ + ": " + str(exc))[:320]
        try:
            scheduler = describe(variation.GetActiveScheduler())
            if scheduler is not None and (scheduler["class"] != "IntSscheduler" or not scheduler["full_name"].startswith(identity["full_name"] + "\\")):
                raise ValueError("The returned scheduler is outside the selected variation.")
            state["scheduler"] = scheduler
        except Exception as exc:
            state["scheduler"] = None
            errors["active_scheduler"] = (type(exc).__name__ + ": " + str(exc))[:320]
        try:
            study_time = context["study_time"]
            local_time = datetime.fromtimestamp(study_time["utc_seconds"]["value"], timezone.utc).astimezone()
            if local_time.strftime("%Y%m%d") != study_time["cDate"]["value"] or local_time.strftime("%H%M%S") != study_time["cTime"]["value"]:
                raise ValueError("The host's local timezone does not match PowerFactory's study-time display; display times remain unknown.")
            state["display_time_verified"] = True
        except Exception as exc:
            state["display_time_verified"] = False
            errors["display_time"] = (type(exc).__name__ + ": " + str(exc))[:320]
        return {**state, "errors": errors}

    state = read_state()
    rows = []
    recording_known = "recording_stage" not in context["context_errors"]
    for obj in variation.GetContents("*.IntSstage", 1) or []:
        stage = describe(obj)
        if stage is None or stage["class"] != "IntSstage" or not stage["full_name"].startswith(identity["full_name"] + "\\"):
            raise RequestError("STAGE_SCOPE_MISMATCH", "A returned stage is outside the selected variation.", 409)
        if describe(obj.GetVariation()) != identity:
            raise RequestError("STAGE_SCOPE_MISMATCH", "A returned stage belongs to a different variation.", 409)
        errors = {}
        activation = read_attribute(obj, "tAcTime")
        timestamp = display_timestamp = None
        try:
            if activation["status"] != "ok" or type(activation.get("value")) is not int:
                raise ValueError("IntSstage.tAcTime did not return integer epoch seconds.")
            utc_time = datetime.fromtimestamp(activation["value"], timezone.utc)
            timestamp = utc_time.isoformat()
            if activation.get("metadata_errors"):
                errors["activation_metadata"] = activation["metadata_errors"]
        except (ValueError, OverflowError, OSError) as exc:
            errors["activation_time"] = str(exc)[:320]
        if timestamp is not None and state["display_time_verified"]:
            try:
                display_timestamp = utc_time.astimezone().isoformat()
            except (ValueError, OverflowError, OSError) as exc:
                errors["display_time"] = str(exc)[:320]
        excluded = None
        try:
            value = obj.IsExcluded()
            if value not in (0, 1):
                raise ValueError("IsExcluded did not return 0 or 1.")
            excluded = bool(value)
        except Exception as exc:
            errors["excluded_from_activation"] = (type(exc).__name__ + ": " + str(exc))[:320]
        effective_time_known = state["scheduler"] is None and "active_scheduler" not in state["errors"]
        if not effective_time_known:
            errors["effective_activation_time"] = "A scheduler may override stage times; its per-stage effective time is not resolved by this reader."
        rows.append({**stage, "activation_time": activation, "activation_time_utc": timestamp,
                     "activation_time_display": display_timestamp,
                     "effective_activation_time_utc": timestamp if effective_time_known else None,
                     "effective_activation_time_display": display_timestamp if effective_time_known else None,
                     "active": stage["full_name"] in state["active_paths"] if state["active_paths"] is not None else None,
                     "excluded_from_activation": excluded,
                     "recording": stage == context["recording_stage"] if recording_known else None, "errors": errors})
    paths = {row["full_name"] for row in rows}
    if len(paths) != len(rows):
        raise RequestError("AMBIGUOUS_STAGE", "More than one stage has the same exact path.", 409)
    if state["active_paths"] is not None and any(path.startswith(identity["full_name"] + "\\") and path not in paths for path in state["active_paths"]):
        raise RequestError("STAGE_STATUS_INCOMPLETE", "An active stage in the selected variation is missing from the listing.", 409)
    if active_context(app, info) != context or read_state() != state:
        raise RequestError("SESSION_CHANGED", "The active context, stage set or scheduler changed during the listing; query again.", 409)
    rows.sort(key=lambda item: (item["name"].casefold(), item["full_name"]))
    filtered = [row for row in rows if arguments["name_contains"].casefold() in row["name"].casefold()]
    offset, limit = arguments["offset"], arguments["limit"]
    page = filtered[offset:offset + limit]
    following = offset + len(page)
    complete = context["context_complete"] and not state["errors"] and not any(row["errors"] for row in rows)
    return {**info, "context": context, "variation": identity, "variation_active": identity["full_name"] in enabled,
            "active_scheduler": state["scheduler"], "status_errors": state["errors"],
            "display_time_verified": state["display_time_verified"],
            "display_time_source": "PowerFactory host's local timezone, checked against native study-time cDate/cTime; each display timestamp includes its UTC offset.",
            "status": "complete" if complete else "partial", "stage_status_complete": complete,
            "total_count": len(rows), "matched_count": len(filtered),
            "active_count": sum(row["active"] is True for row in rows), "inactive_count": sum(row["active"] is False for row in rows),
            "unknown_active_count": sum(row["active"] is None for row in rows),
            "excluded_count": sum(row["excluded_from_activation"] is True for row in rows),
            "unknown_excluded_count": sum(row["excluded_from_activation"] is None for row in rows),
            "recording_count": sum(row["recording"] is True for row in rows), "unknown_recording_count": sum(row["recording"] is None for row in rows),
            "name_contains": arguments["name_contains"], "offset": offset, "limit": limit,
            "returned_count": len(page), "next_offset": following if following < len(filtered) else None, "stages": page,
            "status_sources": {"active": "Application.GetActiveStages(Network Data)", "excluded_from_activation": "IntSstage.IsExcluded", "recording": "Application.GetRecordingStage", "activation_time": "IntSstage.tAcTime"},
            "note": "Prefer activation_time_display for PowerFactory-facing time reports; label activation_time_utc explicitly as UTC. Native active, exclusion and recording states are separate; none is inferred solely from a date comparison. IsExcluded includes variation validity and scheduler rules. tAcTime is the native stage time; an active scheduler may override it. Counts cover all stages before filtering. No stage/variation activation, time change, calculation or save is performed."}


def change_network_variation(app, instance_id, info, tool, arguments):
    if arguments["expected_agent_instance_id"] != instance_id:
        raise RequestError("INSTANCE_MISMATCH", "The agent restarted; refresh the session before changing a variation. Nothing was changed.", 409)
    if arguments["expected_study_case_full_name"] != info["study_case"]["full_name"]:
        raise RequestError("SESSION_CHANGED", "The active study case differs from the expected path. Nothing was changed.", 409)
    folder, entries, before = network_variation_state(app, info)
    path = arguments["variation_full_name"]
    if folder is None or not path.startswith(folder["full_name"] + "\\"):
        raise RequestError("VARIATION_OUT_OF_SCOPE", "Select a network variation inside the active project's variations folder.", 409)
    matches = [(obj, identity) for obj, identity in entries if identity["full_name"] == path]
    if not matches:
        raise RequestError("VARIATION_NOT_FOUND", "No network variation has this exact path. List variations again.", 404)
    obj, identity = matches[0]
    requested_active = tool == "activate_network_variation"
    previous_active = path in before
    attempted = previous_active != requested_active
    code, error = None, None
    if attempted:
        try:
            code = obj.Activate() if requested_active else obj.Deactivate()
            if type(code) is not int or code != 0:
                error = {"code": "VARIATION_CHANGE_FAILED" if type(code) is int else "UNEXPECTED_VARIATION_RETURN",
                         "message": "The variation operation did not return integer zero; inspect the resulting active variations and PowerFactory Output Window."}
        except Exception as exc:
            error = {"code": "VARIATION_CHANGE_EXCEPTION", "message": (type(exc).__name__ + ": " + str(exc))[:320]}
    try:
        _, after_entries, after = network_variation_state(app, info)
    except Exception as exc:
        raise RequestError("VARIATION_OUTCOME_UNKNOWN", "Could not verify variation state after the request. Check list_network_variations before another attempt: " + (type(exc).__name__ + ": " + str(exc))[:320], 409) from exc
    if path not in {item["full_name"] for item_obj, item in after_entries}:
        raise RequestError("VARIATION_OUTCOME_UNKNOWN", "The target is missing after the request. List variations before another attempt.", 409)
    actual_active = path in after
    if actual_active != requested_active and error is None:
        error = {"code": "VARIATION_CHANGE_NOT_VERIFIED", "message": "The requested active state was not observed. Inspect the resulting active variations before another attempt."}
    return {**info, "variation": identity, "requested_active": requested_active,
            "previous_active": previous_active, "active": actual_active, "change_attempted": attempted,
            "already_in_requested_state": not attempted, "return_code": code if type(code) is int else None,
            "success": actual_active == requested_active and error is None, "error": error,
            "active_variations_before": [item for item_obj, item in entries if item["full_name"] in before],
            "active_variations_after": [item for item_obj, item in after_entries if item["full_name"] in after],
            "effects": "Changes variation activation in the current study case and may invalidate results. Study time is unchanged; applicable stages are managed by PowerFactory. No calculation, stage editing, consolidation or database save is requested. Stored results remain historical."}


def scenario_data_summary(scenario):
    if scenario is None:
        return {"status": "no_active_scenario", "stored_object_count": None, "class_counts": None,
                "samples": [], "unsaved_change_count": None, "change_list_complete": False}
    try:
        objects = scenario.GetObjects()
        if not isinstance(objects, (list, tuple)) or any(obj is None for obj in objects):
            raise ValueError("GetObjects did not return an object list.")
        counts, chosen = {}, {}
        for obj in objects:
            cls, path = str(obj.GetClassName()), str(obj.GetFullName())
            counts[cls] = counts.get(cls, 0) + 1
            if cls in ELEMENT_PROFILES and (cls not in chosen or path < chosen[cls][0]) and obj.IsCalcRelevant() == 1:
                chosen[cls] = (path, obj)
        samples, errors = [], []
        for cls in sorted(chosen)[:8]:
            obj = chosen[cls][1]
            for name in ("outserv", "on_off", "plini", "qlini", "pgini", "qgini", "nntap"):
                if name not in ELEMENT_PROFILES[cls]["parameters"]["basic"]:
                    continue
                try:
                    saved = scenario.GetOperationValue(obj, name, 0)
                    if not isinstance(saved, (list, tuple)) or len(saved) != 2:
                        raise ValueError("GetOperationValue did not return [error, value].")
                    if type(saved[0]) is not int or saved[0] != 0:
                        continue
                    stored, current = json_attribute_value(saved[1]), json_attribute_value(obj.GetAttribute(name))
                    sample = {"element": describe(obj), "attribute": name, "stored_value": stored,
                              "current_value": current}
                    sample["current_unit"] = str(obj.GetAttributeUnit(name)).strip()
                    if name in ("plini", "pgini", "qlini", "qgini"):
                        sample["stored_unit"] = "MW" if name in ("plini", "pgini") else "Mvar"
                    samples.append(sample)
                except Exception as exc:
                    errors.append({"element_full_name": str(obj.GetFullName()), "attribute": name,
                                   "error": (type(exc).__name__ + ": " + str(exc))[:320]})
        return {"status": "partial" if errors else "complete", "stored_object_count": len(objects),
                "class_counts": dict(sorted(counts.items())), "samples": samples, "sample_errors": errors,
                "unsaved_change_count": None, "change_list_complete": False,
                "note": "Stored-object counts describe saved data, not unsaved edits. Samples return native stored/current operational values on one calculation-relevant object per class, at most eight classes. GetOperationValue power values use base MW/Mvar; current values retain reported units. No differences or converted values are returned. Unsupported/non-stored candidates are skipped. The complete unsaved-change list is unavailable."}
    except Exception as exc:
        return {"status": "unavailable", "stored_object_count": None, "class_counts": None, "samples": [],
                "unsaved_change_count": None, "change_list_complete": False,
                "error": (type(exc).__name__ + ": " + str(exc))[:320]}


def write_operation_scenario(app, instance_id, info, tool, arguments):
    if arguments["expected_agent_instance_id"] != instance_id:
        raise RequestError("INSTANCE_MISMATCH", "The agent restarted; no scenario was saved or created.", 409)
    if arguments["expected_study_case_full_name"] != info["study_case"]["full_name"]:
        raise RequestError("SESSION_CHANGED", "The active study case changed; no scenario was saved or created.", 409)
    before = active_context(app, info)
    if not before["context_complete"]:
        raise RequestError("CONTEXT_INCOMPLETE", "Resolve get_active_session context errors before scenario persistence.", 409)
    source = app.GetActiveScenario()
    source_info = describe(source)
    if (source_info["full_name"] if source_info else None) != arguments["expected_active_scenario_full_name"]:
        raise RequestError("SCENARIO_CHANGED", "The active scenario differs from the expected path; nothing was saved or created.", 409)
    root = app.GetProjectFolder("scen", 0)
    if root is None or not str(root.GetFullName()).startswith(info["project"]["full_name"] + "\\"):
        raise RequestError("SCENARIO_FOLDER_UNAVAILABLE", "Select an existing operation-scenarios folder in the active project.", 409)
    if source_info is not None and (source_info["class"] != "IntScenario" or not source_info["full_name"].startswith(str(root.GetFullName()) + "\\")):
        raise RequestError("SCENARIO_OUT_OF_SCOPE", "The active scenario is outside this project's operation-scenarios folder.", 409)
    creating = tool == "create_operation_scenario"
    folder = None
    if creating:
        folders = [root] + [obj for obj in root.GetContents("*.IntFolder", 1) or [] if obj is not None]
        matches = [obj for obj in folders if str(obj.GetFullName()) == arguments["folder_full_name"]]
        if len(matches) != 1:
            raise RequestError("SCENARIO_DESTINATION_NOT_FOUND", "Select one exact existing folder under this project's operation scenarios.", 404)
        folder = matches[0]
        if any(str(obj.loc_name).casefold() == arguments["scenario_name"].casefold()
               for obj in folder.GetContents("*.IntScenario", 0) or [] if obj is not None):
            raise RequestError("SCENARIO_NAME_EXISTS", "A scenario with this name already exists in the destination; no overwrite is allowed.", 409)
        destination = {"folder": describe(folder), "name": arguments["scenario_name"],
                       "full_name": str(folder.GetFullName()) + "\\" + arguments["scenario_name"] + ".IntScenario"}
    else:
        if source is None:
            raise RequestError("NO_ACTIVE_SCENARIO", "Activate the exact scenario to save before requesting persistence.", 409)
        destination = source_info
    summary = scenario_data_summary(source)
    ready = summary["status"] in ("complete", "no_active_scenario")
    preview = {"operation": tool, "agent_instance_id": instance_id, "context": before, "destination": destination,
               "source_saved_data": summary, "ready_to_confirm": ready,
               "scope": "Native persistence of current operational attributes for active network elements; includes more than the sampled values. This is not a full-project save.",
               "confirmation_scope": "Destination, active context and shown saved-data/sample values; not an atomic snapshot or full operational-state lock."}
    digest = hashlib.sha256(json.dumps(preview, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()
    if active_context(app, info) != before:
        raise RequestError("SESSION_CHANGED", "The active context changed during the preview; nothing was saved or created.", 409)
    confirmation = arguments["confirmation_digest"]
    if confirmation is None:
        return {**info, "status": "preview", "preview": preview, "confirmation_digest": digest if ready else None,
                "write_attempted": False, "success": None,
                "note": "Review the exact destination and native stored/current sample values before approval. Copy confirmation_digest only for an explicitly authorized write; the tool recomputes the preview and rejects stale confirmations."}
    if not ready or confirmation != digest:
        raise RequestError("SCENARIO_PREVIEW_CHANGED", "The preview is incomplete or differs from the confirmed digest; request and review a new preview. Nothing was saved or created.", 409)
    target, code, move_code, error = None if creating else source, None, None, None
    try:
        if creating:
            staging_name = "MCP_" + uuid.uuid4().hex
            target = app.SaveAsScenario(staging_name, 0)
            identity = describe(target)
            if (identity is None or identity["class"] != "IntScenario" or identity["name"] != staging_name
                    or not identity["full_name"].startswith(str(root.GetFullName()) + "\\")):
                raise ValueError("SaveAsScenario did not return the new scenario in the active project.")
            if describe(app.GetActiveScenario()) != source_info:
                raise ValueError("SaveAsScenario unexpectedly changed the active scenario; placement was not attempted.")
            if str(target.GetParent().GetFullName()) != str(folder.GetFullName()):
                move_code = folder.Move(target)
                if type(move_code) is not int or move_code != 0:
                    raise ValueError("Moving the new capture to the selected folder failed; inspect its actual path.")
            target.SetAttribute("loc_name", arguments["scenario_name"])
            if describe(target) != {"name": arguments["scenario_name"], "class": "IntScenario", "full_name": destination["full_name"]}:
                raise ValueError("The created scenario's final name/path does not match the requested destination.")
        else:
            code = source.Save()
            if type(code) is not int or code != 0:
                error = {"code": "SCENARIO_SAVE_NOT_CONFIRMED", "message": "Save did not return integer zero. Native code 1 can mean unmodified, inactive or failed; it is not proof of a successful save."}
    except Exception as exc:
        error = {"code": "SCENARIO_PERSISTENCE_FAILED", "message": (type(exc).__name__ + ": " + str(exc))[:320]}
    try:
        after = active_context(app)
    except Exception as exc:
        after = None
        error = error or {"code": "SCENARIO_OUTCOME_UNKNOWN", "message": (type(exc).__name__ + ": " + str(exc))[:320]}
    stored = scenario_data_summary(target)
    source_unchanged = scenario_data_summary(source) == summary if creating else None
    samples_match = True
    for sample in stored["samples"]:
        current, saved = sample["current_value"], sample["stored_value"]
        if sample["attribute"] in ("plini", "pgini", "qlini", "qgini"):
            factor = {"W": 1e-6, "kW": 1e-3, "MW": 1.0, "var": 1e-6, "kvar": 1e-3, "Mvar": 1.0}.get(sample["current_unit"])
            samples_match = samples_match and (factor is not None and type(current) in (int, float)
                                               and type(saved) in (int, float)
                                               and math.isclose(saved, current * factor, rel_tol=1e-9, abs_tol=1e-9))
        else:
            samples_match = samples_match and saved == current
    if error is None and (after != before or stored["status"] != "complete"
                          or not samples_match or creating and not source_unchanged):
        error = {"code": "SCENARIO_PERSISTENCE_NOT_VERIFIED", "message": "Check resulting context, stored-data samples and source summary; persistence was attempted and is not automatically undone."}
    success = error is None
    return {**info, "status": "succeeded" if success else "not_saved_or_unchanged" if not creating and code == 1 else "unverified",
            "success": success, "write_attempted": True, "preview": preview, "confirmation_digest": digest,
            "scenario": describe(target), "return_code": code if type(code) is int else None,
            "move_return_code": move_code if type(move_code) is int else None,
            "saved_data": stored, "source_summary_unchanged": source_unchanged,
            "context_after": after, "context_unchanged": after == before, "error": error,
            "note": "Only the explicitly confirmed scenario is saved or a new capture is created. Creation keeps the source active, stages under a unique MCP_ name, then moves/renames the new object. A failure/timeout may leave that capture at its reported or staging path; inspect scenarios before retrying. No automatic overwrite, activation, rollback, calculation or full-project save."}


def list_operation_scenarios(app, info, arguments):
    active = describe(app.GetActiveScenario())
    folder = app.GetProjectFolder("scen", 0)
    folder_info = describe(folder)
    if folder_info is not None and not folder_info["full_name"].startswith(info["project"]["full_name"] + "\\"):
        raise RequestError("SCENARIO_SCOPE_MISMATCH", "The operation-scenarios folder is outside the active project.", 409)
    entries = []
    for obj in (folder.GetContents("*.IntScenario", 1) or []) if folder is not None else []:
        identity = describe(obj)
        if identity is None or identity["class"] != "IntScenario" or not identity["full_name"].startswith(folder_info["full_name"] + "\\"):
            raise RequestError("SCENARIO_SCOPE_MISMATCH", "A returned object is outside the operation-scenarios folder or is not an IntScenario.", 409)
        entries.append(identity)
    paths = {item["full_name"] for item in entries}
    if len(paths) != len(entries):
        raise RequestError("AMBIGUOUS_SCENARIO", "More than one operation scenario has the same exact path.", 409)
    if active is not None and (active["class"] != "IntScenario" or active["full_name"] not in paths):
        raise RequestError("SCENARIO_STATUS_INCOMPLETE", "The active scenario is missing from the visible operation-scenarios folder; no complete status listing is available.", 409)
    if session_info(app) != info or describe(app.GetActiveScenario()) != active:
        raise RequestError("SESSION_CHANGED", "The active context or scenario changed while listing operation scenarios.", 409)
    entries.sort(key=lambda item: (item["name"].casefold(), item["full_name"]))
    rows = [{**item, "folder_full_name": item["full_name"].rsplit("\\", 1)[0], "active": item == active}
            for item in entries if arguments["name_contains"].casefold() in item["name"].casefold()]
    offset, limit = arguments["offset"], arguments["limit"]
    page = rows[offset:offset + limit]
    following = offset + len(page)
    return {**info, "folder": folder_info, "active_scenario": active,
            "total_count": len(entries), "active_count": int(active is not None),
            "inactive_count": len(entries) - int(active is not None), "matched_count": len(rows),
            "name_contains": arguments["name_contains"], "offset": offset, "limit": limit,
            "returned_count": len(page), "next_offset": following if following < len(rows) else None,
            "scenarios": page, "status_source": "GetActiveScenario",
            "note": "Includes operation scenarios in nested folders of the active project. Counts cover all scenarios before filtering. Use exact full_name paths for activation, not row numbers. No activation, save or calculation is performed."}


def activate_operation_scenario(app, instance_id, info, arguments):
    if arguments["expected_agent_instance_id"] != instance_id:
        raise RequestError("INSTANCE_MISMATCH", "The agent restarted; nothing was activated.", 409)
    if arguments["expected_study_case_full_name"] != info["study_case"]["full_name"]:
        raise RequestError("SESSION_CHANGED", "The active study case changed; nothing was activated.", 409)
    current = app.GetActiveScenario()
    previous = describe(current)
    if (previous["full_name"] if previous else None) != arguments["expected_active_scenario_full_name"]:
        raise RequestError("SCENARIO_CHANGED", "The active scenario differs from the expected path; nothing was activated.", 409)
    folder = app.GetProjectFolder("scen", 0)
    path = arguments["scenario_full_name"]
    if folder is None or not str(folder.GetFullName()).startswith(info["project"]["full_name"] + "\\") or not path.startswith(str(folder.GetFullName()) + "\\"):
        raise RequestError("SCENARIO_OUT_OF_SCOPE", "Select an operation scenario inside the active project's scenario folder.", 409)
    matches = [obj for obj in folder.GetContents("*.IntScenario", 1) or []
               if obj is not None and str(obj.GetFullName()) == path and str(obj.GetClassName()) == "IntScenario"]
    if not matches:
        raise RequestError("SCENARIO_NOT_FOUND", "The exact scenario path was not found; browse the scenario folders again.", 404)
    if len(matches) != 1:
        raise RequestError("AMBIGUOUS_SCENARIO", "More than one scenario has this exact path; nothing was activated.", 409)
    target = describe(matches[0])
    already_active = previous == target
    if not already_active and current is not None and not arguments["discard_unsaved_changes"]:
        raise RequestError("SCENARIO_DISCARD_PERMISSION_REQUIRED", "Switching deactivates the current scenario. PowerFactory 2024 has no documented dirty-state query here; explicitly allow discarding unsaved scenario changes, or handle them in the GUI first. Nothing was changed.", 409)
    before = active_context(app, info)
    if before["scenario"] != previous or "scenario" in before["context_errors"]:
        raise RequestError("SCENARIO_CHANGED", "Could not verify the expected active scenario; nothing was changed.", 409)
    code, deactivation_code, error = None, None, None
    deactivation_attempted = activation_attempted = False
    if not already_active:
        try:
            if current is not None:
                deactivation_attempted = True
                deactivation_code = current.Deactivate(0)
                if type(deactivation_code) is not int or deactivation_code != 0 or app.GetActiveScenario() is not None:
                    raise RuntimeError("Scenario deactivation did not return integer zero and an empty active scenario. Target activation was not attempted.")
            if session_info(app) != info:
                raise RuntimeError("Active project/study case changed before target activation.")
            activation_attempted = True
            code = matches[0].Activate()
            if type(code) is not int or code != 0:
                error = {"code": "SCENARIO_ACTIVATION_FAILED", "message": "Activate did not return integer zero; inspect the resulting scenario."}
        except Exception as exc:
            error = {"code": "SCENARIO_ACTIVATION_EXCEPTION", "message": (type(exc).__name__ + ": " + str(exc))[:320]}
    try:
        after = active_context(app)
    except Exception as exc:
        raise RequestError("SCENARIO_OUTCOME_UNKNOWN", "Could not read context after the request. Check get_active_session before another attempt: " + (type(exc).__name__ + ": " + str(exc))[:320], 409) from exc
    verified = after["project"] == info["project"] and after["study_case"] == info["study_case"] and after["scenario"] == target
    if not verified and error is None:
        error = {"code": "SCENARIO_ACTIVATION_NOT_VERIFIED", "message": "The target scenario was not observed in the expected case/project."}
    return {"project": after["project"], "study_case": after["study_case"], "scenario": after["scenario"],
            "previous_scenario": previous, "requested_scenario": target, "already_active": already_active,
            "deactivation_attempted": deactivation_attempted, "activation_attempted": activation_attempted,
            "deactivation_return_code": deactivation_code if type(deactivation_code) is int else None,
            "return_code": code if type(code) is int else None, "success": verified and error is None, "error": error,
            "context_before": before, "context_after": after,
            "effects": "An explicitly permitted switch discards unsaved changes in the previous scenario. No save or calculation is requested. Results may become invalid; there is no automatic rollback."}


def activate_study_case(app, instance_id, arguments):
    if arguments["expected_agent_instance_id"] != instance_id:
        raise RequestError("INSTANCE_MISMATCH", "The agent restarted; refresh the session before activating a study case. Nothing was activated.", 409)
    project = app.GetActiveProject()
    info = {"project": describe(project), "study_case": describe(app.GetActiveStudyCase())}
    if project is None:
        raise RequestError("NO_ACTIVE_PROJECT", "Activate a project in PowerFactory first.", 409)
    if info["project"]["full_name"] != arguments["expected_project_full_name"]:
        raise RequestError("SESSION_CHANGED", "The active project differs from the expected exact path. Nothing was activated.", 409)
    path = arguments["study_case_full_name"]
    if not path.startswith(info["project"]["full_name"] + "\\"):
        raise RequestError("STUDY_CASE_OUT_OF_SCOPE", "Select a study case inside the active project; this tool does not activate other projects.", 409)
    matches = [obj for obj in project.GetContents("*.IntCase", 1) or []
               if obj is not None and str(obj.GetFullName()) == path and str(obj.GetClassName()) == "IntCase"]
    if not matches:
        raise RequestError("STUDY_CASE_NOT_FOUND", "No study case has this exact path in the active project. Browse the study-case folders again.", 404)
    if len(matches) != 1:
        raise RequestError("AMBIGUOUS_STUDY_CASE", "More than one study case has this exact path. Nothing was activated.", 409)
    target = describe(matches[0])
    before = {"project": describe(app.GetActiveProject()), "study_case": describe(app.GetActiveStudyCase())}
    if before != info:
        raise RequestError("SESSION_CHANGED", "The active context changed during preparation. Nothing was activated.", 409)
    already_active = info["study_case"] == target
    code, error = None, None
    if not already_active:
        try:
            code = matches[0].Activate()
            if type(code) is not int or code != 0:
                error = {"code": "ACTIVATION_FAILED" if type(code) is int else "UNEXPECTED_ACTIVATE_RETURN",
                         "message": "Activate did not return integer zero; inspect the resulting active context and PowerFactory Output Window."}
        except Exception as exc:
            error = {"code": "ACTIVATION_EXCEPTION", "message": (type(exc).__name__ + ": " + str(exc))[:320]}
    try:
        after = {"project": describe(app.GetActiveProject()), "study_case": describe(app.GetActiveStudyCase())}
    except Exception as exc:
        raise RequestError("ACTIVATION_OUTCOME_UNKNOWN", "Could not read the active context after activation. Check get_active_session before another attempt: " + (type(exc).__name__ + ": " + str(exc))[:320], 409) from exc
    verified = after["project"] == info["project"] and after["study_case"] == target
    if not verified and error is None:
        error = {"code": "ACTIVATION_NOT_VERIFIED", "message": "The requested study case was not observed in the expected active project. Check the resulting context before another attempt."}
    return {**after, "previous_study_case": info["study_case"], "requested_study_case": target,
            "already_active": already_active, "activation_attempted": not already_active,
            "return_code": code if type(code) is int else None, "success": verified and error is None,
            "error": error,
            "effects": "Activates the existing study case and its configured context. Results may become invalid; no calculation or database save is requested. Stored snapshots and calculation records remain historical."}


def browse_powerfactory_tree(app, arguments):
    info = {"project": describe(app.GetActiveProject()), "study_case": describe(app.GetActiveStudyCase())}
    root = app.GetCurrentUser()
    if root is None:
        raise RequestError("NO_CURRENT_USER", "No current PowerFactory user is available.", 409)
    root_info = describe(root)
    root_path = root_info["full_name"]
    target = arguments["full_name"] or root_path
    if target != root_path and not target.startswith(root_path + "\\"):
        raise RequestError("TREE_OUT_OF_SCOPE", "Navigation is limited to the current user's tree: " + root_path, 403)
    nodes = [root]
    expected = root_path
    if target != root_path:
        for part in target[len(root_path) + 1:].split("\\"):
            expected += "\\" + part
            try:
                matches = [obj for obj in nodes[-1].GetContents("*", 0) or []
                           if obj is not None and str(obj.GetFullName()) == expected]
            except Exception as exc:
                raise RequestError("TREE_READ_FAILED", "Could not resolve tree contents: " + (type(exc).__name__ + ": " + str(exc))[:320], 409) from exc
            if not matches:
                raise RequestError("TREE_NODE_NOT_FOUND", "No visible direct child has the exact path " + expected, 404)
            if len(matches) != 1:
                raise RequestError("AMBIGUOUS_TREE_NODE", "More than one child has the exact path " + expected, 409)
            nodes.append(matches[0])
    moved_up = arguments["direction"] == "parent" and len(nodes) > 1
    if moved_up:
        nodes.pop()
    breadcrumbs = [describe(obj) for obj in nodes]
    current = breadcrumbs[-1]
    try:
        children = [obj for obj in nodes[-1].GetContents("*", 0) or [] if obj is not None]
    except Exception as exc:
        raise RequestError("TREE_READ_FAILED", "Could not list this object's children: " + (type(exc).__name__ + ": " + str(exc))[:320], 409) from exc
    entries, errors, class_counts, error_count = [], [], {}, 0
    prefix = current["full_name"] + "\\"
    for index, obj in enumerate(children):
        try:
            identity = describe(obj)
            if any(len(value) > 4096 for value in identity.values()):
                raise ValueError("Child metadata exceeds 4096 characters per field.")
            if not identity["full_name"].startswith(prefix) or "\\" in identity["full_name"][len(prefix):]:
                raise ValueError("The API returned an object outside the requested direct containment scope.")
        except Exception as exc:
            error_count += 1
            if len(errors) < 50:
                errors.append({"child_index": index, "error": {"code": "TREE_METADATA_FAILED", "message": (type(exc).__name__ + ": " + str(exc))[:320]}})
            continue
        class_counts[identity["class"]] = class_counts.get(identity["class"], 0) + 1
        if (arguments["object_class"] is None or identity["class"] == arguments["object_class"]) and arguments["name_contains"].casefold() in identity["name"].casefold():
            entries.append(identity)
    entries.sort(key=lambda item: (item["name"].casefold(), item["full_name"]))
    offset, limit = arguments["offset"], arguments["limit"]
    page = entries[offset:offset + limit]
    following = offset + len(page)
    after = {"project": describe(app.GetActiveProject()), "study_case": describe(app.GetActiveStudyCase())}
    if after != info or describe(app.GetCurrentUser()) != root_info:
        raise RequestError("SESSION_CHANGED", "The active context or user changed during navigation; browse again.", 409)
    response = {**info, "scope": "Visible containment tree of the current PowerFactory user, including inactive projects.",
                "root": root_info, "current": current, "parent": breadcrumbs[-2] if len(nodes) > 1 else None,
                "breadcrumbs": breadcrumbs, "at_root": len(nodes) == 1, "moved_up": moved_up,
                "browsed_project": next((item for item in breadcrumbs if item["class"] == "IntPrj"), None),
                "active_context_unchanged": True, "direction": arguments["direction"],
                "object_class": arguments["object_class"], "name_contains": arguments["name_contains"],
                "total_child_count": len(children), "class_counts": dict(sorted(class_counts.items())),
                "matched_count": len(entries), "returned_count": len(page), "offset": offset, "limit": limit,
                "next_offset": following if following < len(entries) else None, "children": page,
                "listing_complete": error_count == 0, "metadata_error_count": error_count,
                "child_errors": errors, "child_errors_truncated": error_count > len(errors),
                "note": "Stateless metadata navigation. Counts cover direct children only; descend into subfolders for nested objects. No activation, calculation, parameter read/write or reference following is performed. Active project/study_case fields may be null and describe the active context, not the browsed project."}
    if len(json.dumps(response, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 786432:
        raise RequestError("TREE_RESPONSE_TOO_LARGE", "Tree metadata exceeds the response budget; request a smaller page.", 409)
    return response


def get_protection_chain(app, info, arguments):
    """Read a bounded protection graph; containment is distinct from signal wiring."""
    context = active_context(app, info)
    project_path = info["project"]["full_name"]
    path = arguments["full_name"]
    if not path.startswith(project_path + "\\"):
        raise RequestError("PROTECTION_OUT_OF_SCOPE", "Select a relay or cubicle inside the active project.", 403)
    if arguments["element_class"] == "ElmRelay":
        root = resolve_element(app, arguments)
    else:
        root, current = app.GetActiveProject(), project_path
        for part in path[len(project_path) + 1:].split("\\"):
            current += "\\" + part
            matches = [obj for obj in root.GetContents("*", 0) or []
                       if obj is not None and str(obj.GetFullName()) == current]
            if len(matches) != 1:
                raise RequestError("CUBICLE_NOT_FOUND", "The exact cubicle path is missing or ambiguous; browse again.", 404)
            root = matches[0]
    if str(root.GetClassName()) != arguments["element_class"]:
        raise RequestError("ELEMENT_CLASS_MISMATCH", "The resolved protection root does not match its path.", 409)

    fields = {
        "ElmRelay": ("outserv", "actset", "ctprim", "ctsec", "vtprim", "vtsec", "pelm"),
        "StaCt": ("outserv", "ptapset", "stapset", "pbranch", "iorient"),
        "StaVt": ("outserv", "ptapset", "stapset", "pbusbar"),
        "StaCubic": ("obj_id", "obj_bus"), "StaSwitch": ("on_off", "iUse"),
        "ElmCoup": ("outserv", "on_off"),
        "RelLogdip": ("outserv", "pSwitch", "sLogic", "sTripsig", "iout"),
        "RelLogic": ("outserv", "pSwitch"), "RelMultiset": ("outserv", "pelm"),
    }
    reference_fields = {"ElmRelay": "pelm", "RelMultiset": "pelm",
                        "RelLogdip": "pSwitch", "RelLogic": "pSwitch"}

    def supported(cls):
        return cls in fields or cls.startswith("Rel")

    pending, discovered, nodes, relationships, errors = [root], set(), [], [], []

    def add(obj):
        full_name = str(obj.GetFullName())
        if full_name in discovered:
            return False
        if len(discovered) >= 500:
            raise RequestError("PROTECTION_CHAIN_TOO_LARGE", "More than 500 protection objects; select a narrower relay.", 409)
        discovered.add(full_name)
        return True

    def failure(obj, operation, exc):
        errors.append({"object": describe(obj), "operation": operation,
                       "error": (type(exc).__name__ + ": " + str(exc))[:320]})

    add(root)
    for obj in pending:
        identity = describe(obj)
        cls = identity["class"]
        names = list(dict.fromkeys(("typ_id", *fields.get(cls, DEFAULT_ATTRIBUTES.get(cls, ("outserv",))))))
        attributes, excluded = {}, []
        for name in names:
            try:
                if not obj.HasAttribute(name):
                    excluded.append(name)
                    continue
                attributes[name] = read_attribute(obj, name)
            except Exception as exc:
                failure(obj, "HasAttribute:" + name, exc)
        node = {"object": identity, "attributes": attributes, "excluded_attributes": excluded,
                "equipment_type": None}
        assigned = attributes.get("typ_id", {})
        if assigned.get("status") == "ok" and assigned.get("value") is not None:
            try:
                typ = obj.GetAttribute("typ_id")
                if json_attribute_value(typ) != assigned["value"]:
                    raise ValueError("Assigned type changed during the read.")
                type_class = str(typ.GetClassName())
                type_names = TYPE_DEFAULT_ATTRIBUTES.get(type_class, {
                    "TypCt": ("loc_name", "primtaps", "sectaps"),
                    "TypVt": ("loc_name", "primtaps", "Uinom"),
                }.get(type_class, ("loc_name",)))
                node["equipment_type"] = {"object": describe(typ), **attribute_data(typ, list(type_names))}
            except Exception as exc:
                failure(obj, "typ_id", exc)
        nodes.append(node)
        ref_name = reference_fields.get(cls)
        if ref_name and attributes.get(ref_name, {}).get("status") == "ok":
            try:
                raw = obj.GetAttribute(ref_name)
                if json_attribute_value(raw) != attributes[ref_name]["value"]:
                    raise ValueError("Protection references changed during the read.")
                for index, target in enumerate(raw if isinstance(raw, (list, tuple)) else [raw]):
                    target_info = describe(target)
                    expandable = (target_info is not None and supported(target_info["class"])
                                  and target_info["full_name"].startswith(project_path + "\\"))
                    relationships.append({"source": identity, "target": target_info, "kind": "reference",
                                          "attribute": ref_name, "index": index,
                                          "expansion": "supported" if expandable else "unassigned" if target is None else "metadata_only"})
                    if expandable and add(target):
                        pending.append(target)
            except RequestError:
                raise
            except Exception as exc:
                failure(obj, ref_name, exc)
        if cls == "StaCubic" or cls == "ElmRelay" or cls.startswith("Rel"):
            try:
                for child in obj.GetContents("*", 0) or []:
                    child_info = describe(child)
                    if child_info is None or not supported(child_info["class"]):
                        continue
                    suffix = child_info["full_name"][len(identity["full_name"]) + 1:]
                    if not child_info["full_name"].startswith(identity["full_name"] + "\\") or "\\" in suffix:
                        raise ValueError("GetContents returned an object outside direct containment.")
                    relationships.append({"source": identity, "target": child_info, "kind": "contains",
                                          "source_api": "GetContents(*, 0)"})
                    if add(child):
                        pending.append(child)
            except RequestError:
                raise
            except Exception as exc:
                failure(obj, "GetContents", exc)

    if active_context(app) != context:
        raise RequestError("SESSION_CHANGED", "The active configuration changed during the protection read; query again.", 409)
    nodes.sort(key=lambda item: item["object"]["full_name"])
    offset, limit = arguments["offset"], arguments["limit"]
    page = nodes[offset:offset + limit]
    page_paths = {item["object"]["full_name"] for item in page}
    failed = any(record["status"] != "ok" or record.get("metadata_errors")
                 for node in nodes for record in node["attributes"].values())
    failed_types = any(node["equipment_type"] and (not node["equipment_type"]["all_attributes_read"]
                       or node["equipment_type"]["metadata_error_count"]) for node in nodes)
    response = {**info, "context": context, "root": describe(root), "nodes": page,
                "relationships": [item for item in relationships if item["source"]["full_name"] in page_paths],
                "matched_count": len(nodes), "returned_count": len(page), "offset": offset, "limit": limit,
                "next_offset": offset + len(page) if offset + len(page) < len(nodes) else None,
                "relationship_count": len(relationships), "errors": errors,
                "read_complete": not errors and not failed and not failed_types,
                "scope": "Protection containment plus ElmRelay/RelMultiset.pelm and RelLogdip/RelLogic.pSwitch references; one assigned-type hop.",
                "note": "Follow next_offset for all nodes and their outgoing relationships. Contains does not mean measurement or trip wiring. Reference array indices are native zero-based slots; null is unassigned. Other reference targets remain metadata_only. Excluded attributes are unavailable on this object. No signal-logic evaluation, inferred breaker association, calculation, settings change or save is performed."}
    if len(json.dumps(response, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 786432:
        raise RequestError("PROTECTION_RESPONSE_TOO_LARGE", "Protection data exceeds the response budget; request a smaller page or narrower relay.", 409)
    return response


def execute_read(app, tool, arguments):
    info = session_info(app)  # Each request reads the live session.
    if tool == "get_graphic_selection":
        return get_graphic_selection(app, info, arguments)
    if tool == "get_feeder_inventory":
        return get_feeder_inventory(app, info, arguments)
    if tool == "get_element_connections":
        return get_element_connections(app, info, arguments)
    if tool == "get_short_circuit_contributions":
        return get_short_circuit_contributions(app, info, arguments)
    if tool == "get_protection_chain":
        return get_protection_chain(app, info, arguments)
    if tool == "list_expansion_stages":
        return list_expansion_stages(app, info, arguments)
    if tool == "get_power_balance":
        return get_power_balance(app, info, arguments)
    if tool == "get_transformer_control_status":
        return get_transformer_control_status(app, info, arguments)
    if tool == "get_bus_voltage_sequences":
        return get_bus_voltage_sequences(app, info, arguments)
    if tool == "get_active_session":
        return {**active_context(app, info), "python_version": sys.version.split()[0]}
    if tool == "get_active_scenario":
        scenario = describe(app.GetActiveScenario())
        if session_info(app) != info or describe(app.GetActiveScenario()) != scenario:
            raise RequestError("SESSION_CHANGED", "The active context or operation scenario changed during the read; query again.", 409)
        return {**info, "scenario": scenario, "has_active_scenario": scenario is not None}
    if tool == "list_operation_scenarios":
        return list_operation_scenarios(app, info, arguments)
    if tool == "get_element_attributes":
        return get_element_attributes(app, info, arguments)
    if tool == "get_equipment_type_attributes":
        return get_equipment_type_attributes(app, info, arguments)
    if tool == "list_element_attributes":
        return list_element_attributes(app, info, arguments)
    if tool == "get_model_inventory":
        return get_model_inventory(app, info, arguments)
    if tool == "get_elements_attributes":
        return get_elements_attributes(app, info, arguments)
    if tool == "get_line_loading":
        return get_line_loading(app, info, arguments)
    if tool == "get_transformer_loading":
        return get_transformer_loading(app, info, arguments)
    if tool == "get_bus_voltage":
        return get_bus_voltage(app, info, arguments)
    return list_model_elements(app, info, arguments)


