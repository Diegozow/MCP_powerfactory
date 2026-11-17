# -*- coding: utf-8 -*-
"""Guarded exact faults and durable, synchronous protection-study chunks."""

import csv
import hashlib
import json
import math
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pf_model as model


EXPORT_ROOT = Path(__file__).resolve().parent / 'exports' / 'protection_sweeps'
FAULT_INPUTS = ('target_full_name', 'position_percent', 'reference_end', 'fault_type', 'phases',
                'resistance_ohm', 'reactance_ohm', 'method', 'prefault', 'voltage_factor')
COMMAND_FIELDS = tuple(dict.fromkeys((*model.SHORT_CIRCUIT_FAULT_SETTINGS,
    'iopt_dfr', 'ppro', 'pabs', 'ildfinit', 'cfac_full', 'iopt_preload', 'iopt_prot',
    'iopt_calc', 'iopt_cdef', 'cfac', 'cfac_min', 'cfacMax', 'cfacMin', 'iec_pub',
    'iopt_tap', 'iopt_brc', 'iopt_asc', 'iopt_peak', 'iopt_peak_full', 'iopt_saveRes',
    'iIgnLoad', 'iIgnLneCap', 'iIgnTrfMag', 'iIgnShnt', 'iIksForProt', 'iiterMethod', 'maxiter')))
REQUIRED_FIELDS = (*model.SHORT_CIRCUIT_FAULT_SETTINGS, 'ildfinit', 'cfac_full')
INSPECTION_FIELDS = {
    'SetPath': ('loc_name', 'icolor'),
    'ComShcsweep': ('loc_name', 'path', 'res', 'rels', 'cmds', 'iopt_itr', 'valusage',
                    'eps', 'ceps', 'epsrel', 'cepsrel', 'minLength'),
    'ComProtgraphic': ('loc_name', 'path', 'devices', 'sweepDevices', 'reference', 'iopt_action', 'createVars'),
    'VisPlottz': ('loc_name', 'pPath', 'pRef', 'pRefB', 'p_res', 'fObjs', 'iopt_mod', 'iopt_unit', 'outserv'),
}


def fail(code, message, status=409):
    raise model.RequestError(code, message, status)


def exact(path, classes=None):
    cls = path.rsplit('.', 1)[-1] if isinstance(path, str) else ''
    model.validate_full_path(path, cls)
    if (not cls or classes is not None and cls not in classes
            or any(part in ('', '.', '..') for part in path.split('\\')[1:]) or any(c in path for c in '*?')):
        fail('INVALID_ARGUMENTS', 'Use an exact listed path of the requested class, without wildcards or relative segments.', 400)
    return path


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode('utf-8')).hexdigest()


def number(value, low, high, label):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        fail('INVALID_ARGUMENTS', label + ' must be a finite number in the documented range.', 400)
    return value


def identifier(value):
    if not isinstance(value, str) or re.fullmatch(model.SNAPSHOT_ID_PATTERN, value) is None:
        fail('INVALID_ARGUMENTS', 'Use a simple 1-80 character ID containing letters, digits, underscores or hyphens.', 400)
    return value


def validate_fault(value):
    if not isinstance(value, dict) or set(value) - set(FAULT_INPUTS):
        fail('INVALID_ARGUMENTS', 'Use only the documented exact-fault fields.', 400)
    path = exact(value.get('target_full_name'), ('ElmTerm', 'ElmLne'))
    line = path.endswith('.ElmLne')
    position, end = value.get('position_percent'), value.get('reference_end')
    if line:
        number(position, 0, 100, 'position_percent')
        if end not in ('bus1', 'bus2'):
            fail('INVALID_ARGUMENTS', 'Line faults require reference_end=bus1/bus2.', 400)
    elif position is not None or end is not None:
        fail('INVALID_ARGUMENTS', 'Bus faults do not accept a line position or reference end.', 400)
    fault_type, phases = value.get('fault_type'), value.get('phases')
    choices = {'3psc': ('abc',), '2psc': ('ab', 'bc', 'ca'), 'spgf': ('a', 'b', 'c'), '2pgf': ('ab', 'bc', 'ca')}
    if not isinstance(fault_type, str) or fault_type not in choices or phases not in choices[fault_type]:
        fail('INVALID_ARGUMENTS', 'Specify consistent fault_type and phases: abc, ab/bc/ca, or a/b/c.', 400)
    for name in ('resistance_ohm', 'reactance_ohm'):
        number(value.get(name), 0, 1e6, name)
    if value.get('method') not in ('iec_60909', 'complete'):
        fail('INVALID_ARGUMENTS', 'method must be iec_60909 or complete.', 400)
    prefault, factor = value.get('prefault', 'configured'), value.get('voltage_factor')
    if prefault not in ('configured', 'load_flow', 'nominal_voltage'):
        fail('INVALID_ARGUMENTS', 'prefault must be configured, load_flow or nominal_voltage.', 400)
    if prefault != 'configured' and value['method'] != 'complete':
        fail('INVALID_ARGUMENTS', 'Explicit pre-fault initialisation is supported only for the Complete method.', 400)
    if prefault == 'nominal_voltage':
        number(factor, 0.01, 2, 'voltage_factor')
    elif factor is not None:
        fail('INVALID_ARGUMENTS', 'voltage_factor requires nominal_voltage initialisation.', 400)
    return {**value, 'position_percent': position, 'reference_end': end, 'prefault': prefault, 'voltage_factor': factor}


def validate_arguments(tool, arguments):
    if tool == 'read_protection_sweep_results':
        if set(arguments) - {'source_full_name', 'columns', 'axis_kind', 'offset', 'limit', 'file_name'}:
            fail('INVALID_ARGUMENTS', 'Unsupported recorded-result argument.', 400)
        source = exact(arguments.get('source_full_name'), ('ElmRes', 'ComShcsweep', 'VisPlottz'))
        columns = arguments.get('columns')
        if columns is not None:
            if not isinstance(columns, list) or not 1 <= len(columns) <= 8:
                fail('INVALID_ARGUMENTS', 'Select 1-8 explicit recorded columns or omit columns to discover metadata.', 400)
            seen = set()
            for column in columns:
                if (not isinstance(column, dict) or set(column) != {'index', 'value_kind'}
                        or type(column['index']) is not int or not 0 <= column['index'] <= 1000000
                        or column['index'] in seen or column['value_kind'] not in ('number', 'object')):
                    fail('INVALID_ARGUMENTS', 'Use unique column indices and explicit number/object value_kind.', 400)
                seen.add(column['index'])
        axis = arguments.get('axis_kind', 'number')
        filename = arguments.get('file_name')
        if axis not in ('number', 'object') or columns is None and axis != 'number':
            fail('INVALID_ARGUMENTS', 'Select number/object axis_kind only when reading rows.', 400)
        if filename is not None and (not isinstance(filename, str)
                or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}\.(json|csv)', filename) is None or columns is None):
            fail('INVALID_ARGUMENTS', 'Export selected row pages to a new simple .json/.csv basename.', 400)
        return {**model.validate_page({**arguments, 'limit': arguments.get('limit', 20)}, 100),
                'source_full_name': source, 'columns': columns, 'axis_kind': axis, 'file_name': filename}
    if tool == 'inspect_protection_study':
        if set(arguments) - {'full_name', 'object_class', 'offset', 'limit'}:
            fail('INVALID_ARGUMENTS', 'Use only full_name, object_class, offset and limit.', 400)
        cls, path = arguments.get('object_class', 'SetPath'), arguments.get('full_name')
        if not isinstance(cls, str) or cls not in INSPECTION_FIELDS:
            fail('INVALID_ARGUMENTS', 'Select SetPath, ComShcsweep, ComProtgraphic or VisPlottz.', 400)
        if path is not None:
            exact(path, (cls,))
        return {**model.validate_page(arguments, 50), 'full_name': path, 'object_class': cls}
    if tool == 'get_protection_sweep_status':
        if set(arguments) != {'sweep_id', 'request_id'}:
            fail('INVALID_ARGUMENTS', 'Provide the sweep_id and chunk request_id.', 400)
        if arguments['sweep_id'] == 'recorded':
            fail('INVALID_ARGUMENTS', 'The recorded export namespace is reserved.', 400)
        return {'sweep_id': identifier(arguments['sweep_id']), 'request_id': model.validate_request_id(arguments['request_id'])}
    common = {'command_full_name', 'expected_study_case_full_name', 'expected_agent_instance_id', 'request_id',
              'confirmation_digest', 'channels'}
    extra = set(FAULT_INPUTS) if tool == 'run_short_circuit_fault' else {
        'sweep_id', 'path_full_name', 'steps', 'discard_unsaved_scenario_changes'}
    if set(arguments) - common - extra:
        fail('INVALID_ARGUMENTS', 'Unsupported protection-study argument.', 400)
    base = model.validate_arguments('run_short_circuit', {key: arguments.get(key) for key in
        ('command_full_name', 'expected_study_case_full_name', 'expected_agent_instance_id', 'request_id')})
    exact(base['command_full_name'], ('ComShc',))
    exact(base['expected_study_case_full_name'], ('IntCase',))
    confirmation = arguments.get('confirmation_digest')
    if confirmation is not None and (not isinstance(confirmation, str) or re.fullmatch(r'[a-f0-9]{64}', confirmation) is None):
        fail('INVALID_ARGUMENTS', 'Copy the reviewed preview confirmation_digest.', 400)
    channels = arguments.get('channels', [])
    if not isinstance(channels, list) or len(channels) > 8:
        fail('INVALID_ARGUMENTS', 'Provide at most eight explicit scalar result channels.', 400)
    seen = set()
    for channel in channels:
        if not isinstance(channel, dict) or set(channel) != {'full_name', 'attributes'}:
            fail('INVALID_ARGUMENTS', 'Each channel requires only full_name and attributes.', 400)
        path = exact(channel['full_name'])
        cls = path.rsplit('.', 1)[-1]
        if cls not in model.ELEMENT_CLASSES and re.fullmatch(r'Rel[A-Za-z0-9_]+', cls) is None:
            fail('INVALID_ARGUMENTS', 'Channels support model elements and native Rel* blocks, not definitions.', 400)
        names = model.validate_names(channel['attributes'])
        if not names or len(names) > 32 or any(not name.startswith(('m:', 'c:', 's:')) for name in names) or path in seen:
            fail('INVALID_ARGUMENTS', 'Use unique object paths and 1-32 direct m:/c:/s: variables per channel.', 400)
        seen.add(path)
    base = {key: value for key, value in base.items() if key not in ('result_full_names', 'result_attributes')}
    base.update(channels=channels, confirmation_digest=confirmation)
    if tool == 'run_short_circuit_fault':
        return {**base, **validate_fault({key: arguments[key] for key in FAULT_INPUTS if key in arguments})}
    steps = arguments.get('steps')
    if arguments.get('sweep_id') == 'recorded':
        fail('INVALID_ARGUMENTS', 'The recorded export namespace is reserved.', 400)
    discard = arguments.get('discard_unsaved_scenario_changes', False)
    if not channels or not isinstance(steps, list) or not 1 <= len(steps) <= 8 or type(discard) is not bool:
        fail('INVALID_ARGUMENTS', 'A sweep chunk requires 1-8 steps, result channels and a boolean discard flag.', 400)
    normalized, seen = [], set()
    for step in steps:
        if not isinstance(step, dict) or set(step) - set(FAULT_INPUTS) - {'step_id', 'scenario_full_name'}:
            fail('INVALID_ARGUMENTS', 'Use explicit fault fields, step_id and scenario_full_name for each step.', 400)
        step_id = identifier(step.get('step_id'))
        if step_id in seen or 'scenario_full_name' not in step:
            fail('INVALID_ARGUMENTS', 'Each step needs a unique step_id and an explicit scenario_full_name (or null).', 400)
        seen.add(step_id)
        scenario = step['scenario_full_name']
        if scenario is not None:
            exact(scenario, ('IntScenario',))
        normalized.append({'step_id': step_id, 'scenario_full_name': scenario,
                           **validate_fault({key: step[key] for key in FAULT_INPUTS if key in step})})
    return {**base, 'sweep_id': identifier(arguments.get('sweep_id')),
            'path_full_name': exact(arguments.get('path_full_name'), ('SetPath',)),
            'steps': normalized, 'discard_unsaved_scenario_changes': discard}


def stored_object(app, info, path):
    """Exact containment only; no wildcard user input, activation or object creation."""
    root, current = app.GetActiveProject(), info['project']['full_name']
    if not path.startswith(current + '\\'):
        fail('STUDY_OBJECT_OUT_OF_SCOPE', 'Select an exact object inside the active project.', 403)
    for part in path[len(current) + 1:].split('\\'):
        current += '\\' + part
        matches = [obj for obj in root.GetContents('*', 0) or [] if obj is not None and str(obj.GetFullName()) == current]
        if len(matches) != 1:
            fail('STUDY_OBJECT_NOT_FOUND', 'The exact stored object is missing or ambiguous; browse again.', 404)
        root = matches[0]
    if str(root.GetClassName()) != path.rsplit('.', 1)[-1]:
        fail('ELEMENT_CLASS_MISMATCH', 'Resolved object class does not match its exact path.')
    return root


def bounded(response):
    if len(json.dumps(response, ensure_ascii=False, allow_nan=False).encode('utf-8')) > 786432:
        fail('PROTECTION_STUDY_RESPONSE_TOO_LARGE', 'Request fewer channels, variables or steps, or a smaller inspection page.')
    return response


def path_data(path):
    values = {}
    for key, method, args in [('members', 'GetAll', ()), ('forward_branches', 'GetBranches', (0,)),
                              ('reverse_branches', 'GetBranches', (1,)),
                              ('forward_devices', 'AllProtectionDevices', (0,)),
                              ('reverse_devices', 'AllProtectionDevices', (1,))]:
        objects = getattr(path, method)(*args)
        if not isinstance(objects, (list, tuple)) or len(objects) > 500 or any(obj is None for obj in objects):
            fail('UNSUPPORTED_PROTECTION_PATH', 'Native path membership must be a readable list of at most 500 objects.')
        values[key] = [model.describe(obj) for obj in objects]
    return values


def inspect_protection_study(app, info, arguments):
    context = model.active_context(app, info)
    cls, path = arguments['object_class'], arguments['full_name']
    if path is None:
        root = app.GetActiveProject() if cls == 'SetPath' else app.GetActiveStudyCase()
        prefix = str(root.GetFullName()) + '\\'
        objects = [model.describe(obj) for obj in root.GetContents('*.' + cls, 1) or []
                   if obj is not None and str(obj.GetClassName()) == cls and str(obj.GetFullName()).startswith(prefix)]
        objects.sort(key=lambda obj: obj['full_name'])
        offset, limit = arguments['offset'], arguments['limit']
        response = {**info, 'context': context, 'object_class': cls, 'objects': objects[offset:offset + limit],
                    'matched_count': len(objects), 'next_offset': offset + limit if offset + limit < len(objects) else None}
    else:
        obj = stored_object(app, info, path)
        if cls != 'SetPath' and not path.startswith(info['study_case']['full_name'] + '\\'):
            fail('STUDY_OBJECT_OUT_OF_SCOPE', 'Select an existing command or plot inside the active study case.', 403)
        checks = [model.availability(obj, name) for name in INSPECTION_FIELDS[cls]]
        data = model.attribute_data(obj, [item['name'] for item in checks if item['available'] is not False])
        response = {**info, 'context': context, 'object': model.describe(obj), **data,
                    'unavailable_attributes': [item['name'] for item in checks if item['available'] is False],
                    'availability_errors': [item for item in checks if item['available'] is None]}
        if cls == 'SetPath':
            data = path_data(obj)
            offset, limit = arguments['offset'], arguments['limit']
            response.update({key: [{'index': index, 'object': value} for index, value in enumerate(rows[offset:offset + limit], offset)]
                             for key, rows in data.items()})
            response['counts'] = {key: len(rows) for key, rows in data.items()}
            response['next_offset'] = offset + limit if any(offset + limit < len(rows) for rows in data.values()) else None
            response['order_note'] = 'GetBranches(0/1) supplies native forward/reverse branch order. GetAll membership order is not asserted to be an electrical traversal.'
    if model.active_context(app) != context:
        fail('SESSION_CHANGED', 'Active context changed during inspection; read again.')
    return bounded({**response, 'license_status': 'unknown', 'execution_supported': False,
                    'note': 'Read existing definitions only; no path/plot creation or command Execute. Inspect ComShcsweep and ComProtgraphic settings here; bounded execution uses explicit ComShc steps through run_protection_sweep. Use read_protection_sweep_results for an existing assigned ElmRes file.'})


def check_session(instance_id, info, arguments):
    if arguments['expected_agent_instance_id'] != instance_id:
        fail('INSTANCE_MISMATCH', 'The agent changed; refresh the session before any operation.')
    if arguments['expected_study_case_full_name'] != info['study_case']['full_name']:
        fail('SESSION_CHANGED', 'The active study case changed; no settings were written.')


def settings(command):
    checks = [model.availability(command, name) for name in COMMAND_FIELDS]
    if any(item['available'] is None or item['available'] is False and item['name'] in REQUIRED_FIELDS for item in checks):
        fail('FAULT_SETTINGS_UNAVAILABLE', 'Required native fault settings or their availability could not be read.')
    data = model.attribute_data(command, [item['name'] for item in checks if item['available']])
    if not data['all_attributes_read'] or data['metadata_error_count']:
        fail('FAULT_SETTINGS_UNAVAILABLE', 'Native fault settings or metadata are incomplete; nothing was written.')
    return data['attributes']


def prepare_fault(app, instance_id, info, arguments, allow_inactive=False):
    check_session(instance_id, info, arguments)
    context = model.active_context(app, info)
    if not context['context_complete']:
        fail('CONTEXT_INCOMPLETE', 'Resolve native context errors before configuring a fault.')
    matches = [obj for obj, ident in model.calculation_objects(app, info, 'ComShc')
               if ident['full_name'] == arguments['command_full_name']]
    if len(matches) != 1:
        fail('COMMAND_NOT_FOUND', 'Select one exact existing ComShc from the active case.', 404)
    command, target = matches[0], stored_object(app, info, arguments['target_full_name'])
    before = settings(command)
    if before['iopt_cnf']['value'] != 0 or before['iEnhancedZf']['value'] != 0:
        fail('UNSUPPORTED_FAULT_MODE', 'Multiple/internal/intercircuit faults and enhanced impedance require separate verified workflows.')
    for name in ('Rf', 'Xf'):
        if before[name]['unit'].strip() not in ('Ohm', 'ohm', '\u03a9'):
            fail('FAULT_UNIT_MISMATCH', 'Rf/Xf must report native ohms; no unit conversion is performed.')
    requested = {'iopt_allbus': 0, 'shcobj': target, 'iopt_shc': arguments['fault_type'],
                 'iopt_mde': 1 if arguments['method'] == 'iec_60909' else 3,
                 'Rf': arguments['resistance_ohm'], 'Xf': arguments['reactance_ohm']}
    phase_fields = {'2psc': 'i_p2psc', 'spgf': 'i_pspgf', '2pgf': 'i_p2pgf'}
    if arguments['fault_type'] in phase_fields:
        requested[phase_fields[arguments['fault_type']]] = (('a', 'b', 'c') if arguments['fault_type'] == 'spgf'
                                                         else ('ab', 'bc', 'ca')).index(arguments['phases'])
    if arguments['prefault'] != 'configured':
        requested['ildfinit'] = int(arguments['prefault'] == 'load_flow')
        if arguments['prefault'] == 'nominal_voltage':
            if before['cfac_full']['unit'].strip() not in ('', 'p.u.', 'pu'):
                fail('FAULT_UNIT_MISMATCH', 'The nominal-voltage factor must use native dimensionless/per-unit metadata.')
            requested['cfac_full'] = arguments['voltage_factor']
    state = model.attribute_data(target, ['outserv'])['attributes']['outserv']
    if state.get('status') != 'ok' or not allow_inactive and state.get('value') != 0:
        fail('UNSUPPORTED_FAULT_TARGET', 'The exact target must have native outserv=0.')
    option, option_before, terminals, reference = None, None, [], None
    if arguments['target_full_name'].endswith('.ElmLne'):
        for side in ('bus1', 'bus2'):
            cubicle = target.GetAttribute(side)
            terminal = cubicle.GetAttribute('cterm') if cubicle is not None else None
            if terminal is None or str(terminal.GetClassName()) != 'ElmTerm':
                fail('UNSUPPORTED_FAULT_TARGET', 'Both line ends must have explicit native cubicle/terminal references.')
            terminals.append(terminal)
        for name in ('iopt_dfr', 'ppro', 'pabs'):
            if name not in before:
                fail('FAULT_SETTINGS_UNAVAILABLE', 'Native line position/reference settings are unavailable.')
        if before['ppro']['unit'].strip() != '%':
            fail('FAULT_UNIT_MISMATCH', 'Native relative line position must report percent.')
        length = model.read_attribute(target, 'dline')
        if length.get('status') != 'ok' or type(length['value']) not in (int, float) or length['value'] <= 0:
            fail('UNSUPPORTED_FAULT_TARGET', 'The selected line must have a readable positive native length.')
        options = [obj for obj, _ in model.calculation_objects(app, info, 'OptComshc')]
        if len(options) != 1:
            fail('LINE_FAULT_OPTIONS_UNAVAILABLE', 'Configure exactly one existing OptComshc in this study case; none is created.')
        option = options[0]
        option_before = model.read_attribute(option, 'iopt_opt')
        if option_before.get('status') != 'ok' or option_before.get('value') not in ('pro', 'abs'):
            fail('LINE_FAULT_OPTIONS_UNAVAILABLE', 'The native OptComshc position mode is unavailable or unsupported.')
        requested.update(ppro=arguments['position_percent'], iopt_dfr=0 if arguments['reference_end'] == 'bus1' else 1)
        reference = model.describe(terminals[0 if arguments['reference_end'] == 'bus1' else 1])
    else:
        terminals = [target]
    for terminal in terminals:
        fields = model.attribute_data(terminal, ['outserv', 'systype', 'nphase'])['attributes']
        if any(fields[name].get('status') != 'ok' or fields[name].get('value') != value
               for name, value in (('outserv', 0), ('systype', 0), ('nphase', 3))):
            if not (allow_inactive and all(fields[name].get('status') == 'ok' and fields[name].get('value') == value
                                          for name, value in (('systype', 0), ('nphase', 3)))):
                fail('UNSUPPORTED_FAULT_TARGET', 'This workflow requires explicit in-service, three-phase AC terminals.')
    resolved = [(stored_object(app, info, channel['full_name']), channel['attributes']) for channel in arguments['channels']]
    preview = {'context': context, 'agent_instance_id': instance_id, 'command': model.describe(command),
               'fault': {**{key: arguments[key] for key in FAULT_INPUTS}, 'reference_terminal': reference,
                         'terminal_connections': [model.describe(obj) for obj in terminals]},
               'settings_before': before,
               'requested_settings': {key: model.json_attribute_value(value) for key, value in requested.items()},
               'line_options': {'object': model.describe(option), 'before': option_before, 'after': 'pro'} if option else None,
               'channels': arguments['channels'], 'request_id': arguments['request_id'],
               'coverage_note': 'Selected PowerFactory 2024 command options only, not a full model hash. Bus terminals are exact physical locations, not inferred internal transformer/CT-zone faults. Restoring settings does not restore previous results or automatic controls.'}
    if model.active_context(app) != context:
        fail('SESSION_CHANGED', 'Active context changed during fault preparation.')
    return command, target, option, requested, resolved, preview


def messages(app):
    try:
        values = app.GetOutputWindow().GetContent()
        if not isinstance(values, (list, tuple)) or any(not isinstance(value, str) for value in values):
            raise ValueError('Expected native output-window text records.')
        return list(values), None
    except Exception as exc:
        return None, (type(exc).__name__ + ': ' + str(exc))[:320]


def run_short_circuit_fault(app, instance_id, records, info, arguments):
    check_session(instance_id, info, arguments)
    request_id = arguments['request_id']
    signature = digest({'tool': 'run_short_circuit_fault', 'arguments': arguments})
    if request_id in records:
        if records[request_id]['signature'] != signature:
            fail('REQUEST_ID_CONFLICT', 'This request ID already identifies different arguments.')
        return {**records[request_id]['response'], 'replayed': True}
    command, target, option, requested, resolved, preview = prepare_fault(app, instance_id, info, arguments)
    confirmation = digest(preview)
    if arguments['confirmation_digest'] is None:
        return {**info, 'status': 'preview', 'preview': preview, 'confirmation_digest': confirmation,
                'write_attempted': False, 'execution_attempted': False, 'success': None}
    if arguments['confirmation_digest'] != confirmation:
        fail('FAULT_PREVIEW_CHANGED', 'The reviewed inputs, settings or active context changed; request another preview.')
    if len(records) >= 32:
        fail('CALCULATION_HISTORY_FULL', 'The session already holds 32 attempts; no settings were written.')
    before = {name: entry['value'] for name, entry in preview['settings_before'].items()}
    original = {name: command.GetAttribute(name) for name in requested}
    if any(model.json_attribute_value(value) != before[name] for name, value in original.items()):
        fail('FAULT_PREVIEW_CHANGED', 'Native settings changed before the first setter.')
    response = {**info, 'request_id': request_id, 'calculation': 'short_circuit', 'status': 'configuring',
                'success': False, 'replayed': False, 'write_attempted': False, 'execution_attempted': False,
                'fault_configuration': {'preview': preview, 'confirmation_digest': confirmation, 'verified': False},
                'samples': [], 'started_at_utc': datetime.now(timezone.utc).isoformat(), 'return_code': None}
    records[request_id] = {'signature': signature, 'response': response}
    attempted, option_attempted = [], False
    old_messages, message_error = messages(app)
    try:
        if option is not None and preview['line_options']['before']['value'] != 'pro':
            option_attempted = True
            response['write_attempted'] = True
            option.SetAttribute('iopt_opt', 'pro')
        for name, value in requested.items():
            if before[name] != model.json_attribute_value(value):
                attempted.append(name)
                response['write_attempted'] = True
                command.SetAttribute(name, value)
        configured = settings(command)
        expected = {**before, **preview['requested_settings']}
        if (any(configured[name]['value'] != value for name, value in expected.items())
                or option is not None and option.GetAttribute('iopt_opt') != 'pro'
                or model.active_context(app) != preview['context']):
            raise ValueError('Configured fields, line mode or active context failed readback; Execute was not called.')
        response['fault_configuration'].update(verified=True, settings_at_execution=configured)
        response['execution_attempted'] = True
        response['status'] = 'running'
        code = command.Execute()
        response['return_code'] = code if type(code) is int else None
        response['validity_after'] = model.calculation_validity(app, 'short_circuit')
        response['context_after_execution'] = model.active_context(app)
        execution_success = type(code) is int and code == 0 and response['validity_after']['valid'] is True
        if response['context_after_execution'] != preview['context']:
            raise ValueError('Active context changed during Execute; result provenance is not verified.')
        response.update(status='succeeded' if execution_success else 'failed' if type(code) is int else 'indeterminate',
                        execution_success=execution_success)
        if execution_success:
            for obj, names in resolved:
                ancestors, current = [], obj
                ancestry_error = None
                # Native containment only; do not infer trip wiring or primary/backup roles.
                try:
                    for _ in range(12):
                        get_parent = getattr(current, 'GetParent', None)
                        if not callable(get_parent):
                            break
                        current = get_parent()
                        if current is None:
                            break
                        identity = model.describe(current)
                        ancestors.append(identity)
                        if identity['class'] == 'IntPrj':
                            break
                except Exception as exc:
                    ancestry_error = (type(exc).__name__ + ': ' + str(exc))[:320]
                protection_metadata = {'outserv': model.read_attribute(obj, 'outserv'), 'assigned_type': None}
                try:
                    if obj.HasAttribute('typ_id'):
                        typ = obj.GetAttribute('typ_id')
                        if typ is not None:
                            fields = [name for name in ('sfiec', 'sfansi', 'izone', 'aunit', 'iphases', 'atype', 'ienabled')
                                      if typ.HasAttribute(name)]
                            protection_metadata['assigned_type'] = {'object': model.describe(typ),
                                'attributes': model.attribute_data(typ, fields, 4096)['attributes']}
                except Exception as exc:
                    protection_metadata['error'] = (type(exc).__name__ + ': ' + str(exc))[:320]
                response['samples'].append({'object': model.describe(obj), 'ancestors': ancestors,
                    'ancestry_complete': bool(ancestors and ancestors[-1]['class'] == 'IntPrj') and ancestry_error is None,
                    'ancestry_error': ancestry_error, 'protection_metadata': protection_metadata,
                    **model.attribute_data(obj, names, 32768),
                    'fault_provenance': {'status': 'captured_for_execution', 'calculation_request_id': request_id}})
            if model.active_context(app) != preview['context']:
                raise ValueError('Active context changed during result sampling; the read is not a verified execution sample.')
    except Exception as exc:
        response.update(status='indeterminate' if response['execution_attempted'] else 'not_executed', execution_success=False,
                        error={'code': 'EXACT_FAULT_WORKFLOW_FAILED', 'message': (type(exc).__name__ + ': ' + str(exc))[:320]})
    finally:
        new_messages, after_error = messages(app)
        appended = old_messages is not None and new_messages is not None and new_messages[:len(old_messages)] == old_messages
        delta = new_messages[len(old_messages):] if appended else []
        response['diagnostics'] = {'attribution': 'appended_output_window_slice' if appended else 'unknown',
            'messages': [text[:2048] for text in delta[-100:]], 'truncated': len(delta) > 100 or any(len(text) > 2048 for text in delta),
            'error': message_error or after_error, 'note': 'No output was cleared or logging changed; an appended slice may include native nested calculations.'}
        errors = []
        for name in reversed(attempted):
            try:
                command.SetAttribute(name, original[name])
            except Exception as exc:
                errors.append({'attribute': name, 'error': (type(exc).__name__ + ': ' + str(exc))[:320]})
        if option_attempted:
            try:
                option.SetAttribute('iopt_opt', preview['line_options']['before']['value'])
            except Exception as exc:
                errors.append({'attribute': 'OptComshc.iopt_opt', 'error': (type(exc).__name__ + ': ' + str(exc))[:320]})
        try:
            restored = settings(command)
            verified = all(restored[name]['value'] == value for name, value in before.items())
            if option is not None:
                verified = verified and option.GetAttribute('iopt_opt') == preview['line_options']['before']['value']
        except Exception as exc:
            restored, verified = None, False
            errors.append({'operation': 'readback', 'error': (type(exc).__name__ + ': ' + str(exc))[:320]})
        response['restoration'] = {'status': 'restored' if verified else 'failed', 'success': verified,
                                   'settings_after': restored, 'errors': errors, 'prior_results_restored': False}
        response['execution_success'] = response.get('execution_success', False)
        response['success'] = response['execution_success'] and verified
        response['result_requested_count'] = len(resolved)
        response['results_read'] = any(item['successful_count'] for item in response['samples'])
        response['result_attribute_error_count'] = sum(item['failed_count'] for item in response['samples'])
        response['result_metadata_error_count'] = sum(item['metadata_error_count'] for item in response['samples'])
        response['validity_after_restoration'] = model.calculation_validity(app, 'short_circuit')
        response['completed_at_utc'] = datetime.now(timezone.utc).isoformat()
        response['outcome'] = model.calculation_outcome(response)
    return bounded(response)


def namespace(sweep_id):
    root = EXPORT_ROOT.resolve()
    path = root / identifier(sweep_id)
    if path.resolve().parent != root:
        fail('SWEEP_STORAGE_OUT_OF_SCOPE', 'Sweep storage must remain inside the configured export root.', 403)
    return path


def read_checkpoint(path):
    if path.is_symlink() or path.stat().st_size > 4 * 1024 * 1024:
        fail('SWEEP_CHECKPOINT_UNREADABLE', 'Checkpoint is outside the supported storage bounds; inspect it manually.')
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(value, dict):
            raise ValueError('Expected a checkpoint object.')
        return value
    except (OSError, ValueError) as exc:
        fail('SWEEP_CHECKPOINT_UNREADABLE', 'Cannot verify the durable checkpoint; do not reexecute: ' + str(exc)[:240])


def write_checkpoint(path, data, exclusive=False):
    encoded = json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2)
    temporary = path if exclusive else path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    # fsync before publication; a started checkpoint is written before native mutations.
    with temporary.open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    if not exclusive:
        os.replace(temporary, path)


def step_summary(step_id, path, checkpoint, replayed=False):
    response = checkpoint.get('response', {})
    return {'step_id': step_id, 'state': checkpoint['state'], 'success': response.get('success', False),
            'replayed': replayed, 'json_file': str(path), 'csv_file': checkpoint.get('csv_file'),
            'calculation_request_id': response.get('request_id'),
            'execution_attempted': response.get('execution_attempted', False),
            'execution_status': response.get('status', 'unknown'), 'restoration': response.get('restoration', {}).get('status', 'unknown'),
            'sampled_attribute_count': sum(len(item['attributes']) for item in response.get('samples', [])),
            'unavailable_attribute_count': sum(item['failed_count'] for item in response.get('samples', []))}


def get_protection_sweep_status(arguments):
    folder = namespace(arguments['sweep_id'])
    path = folder / ('request_' + arguments['request_id'] + '.json')
    if not path.is_file():
        if (folder / 'running.json').exists():
            return {'sweep_id': arguments['sweep_id'], 'request_id': arguments['request_id'], 'state': 'unknown',
                    'recovery_required': True, 'source': 'durable_protection_sweep_files',
                    'note': 'A running marker exists but this request checkpoint is missing. Inspect durable files and native state; do not automatically retry or clear the marker.'}
        fail('SWEEP_CHECKPOINT_NOT_FOUND', 'No durable chunk record exists for these IDs. Missing records do not prove that earlier native work did not run.', 404)
    checkpoint = read_checkpoint(path)
    return {**checkpoint, 'checkpoint_file': str(path), 'historical_record': True,
            'source': 'durable_protection_sweep_files',
            'recovery_required': checkpoint['state'] == 'running' or (folder / 'running.json').exists(),
            'note': 'Read-only durable evidence. A running checkpoint after disconnect/restart has unknown native outcome; it must not be automatically repeated or cleared.'}


def scenario_object(app, info, path):
    if path is None:
        return None
    folder = app.GetProjectFolder('scen', 0)
    if folder is None or not path.startswith(str(folder.GetFullName()) + '\\'):
        fail('SCENARIO_OUT_OF_SCOPE', 'Select exact scenarios inside the active project scenario folder.')
    obj = stored_object(app, info, path)
    if str(obj.GetClassName()) != 'IntScenario':
        fail('SCENARIO_NOT_FOUND', 'The selected scenario no longer exists.', 404)
    return obj


def switch_scenario(app, target):
    current = app.GetActiveScenario()
    if model.describe(current) == model.describe(target):
        return
    if current is not None:
        code = current.Deactivate(0)  # Explicitly discard; never save or infer a dirty state.
        if type(code) is not int or code != 0 or app.GetActiveScenario() is not None:
            raise RuntimeError('Scenario deactivation was not verified.')
    if target is not None:
        code = target.Activate()
        if type(code) is not int or code != 0:
            raise RuntimeError('Scenario activation failed.')
    if model.describe(app.GetActiveScenario()) != model.describe(target):
        raise RuntimeError('Requested scenario was not observed after activation.')


def write_step_csv(path, arguments, step, result):
    fault = result.get('fault_configuration', {}).get('preview', {}).get('fault', {})
    valid = result.get('validity_after', {}).get('valid')
    columns = ['sweep_id', 'chunk_request_id', 'step_id', 'calculation_request_id', 'project', 'study_case',
               'scenario', 'fault_element', 'position_percent', 'reference_end', 'reference_terminal',
               'fault_type', 'phases', 'Rf_ohm', 'Xf_ohm', 'method', 'validity', 'device', 'class',
               'relay', 'bay', 'substation', 'function_iec', 'function_ansi', 'zone', 'protection_metadata_json',
               'variable', 'native_value_json', 'unit', 'status', 'error_json']
    with path.open('x', encoding='utf-8', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(columns)
        for sample in result.get('samples', []):
            ancestors = sample['ancestors']
            parent = lambda cls: next((item['full_name'] for item in ancestors if item['class'] == cls), None)
            metadata = sample.get('protection_metadata', {})
            type_fields = (metadata.get('assigned_type') or {}).get('attributes', {})
            for name, record in sample['attributes'].items():
                row = [arguments['sweep_id'], arguments['request_id'], step['step_id'], result['request_id'],
                       result['project']['full_name'], result['study_case']['full_name'], step['scenario_full_name'],
                       step['target_full_name'], step['position_percent'], step['reference_end'],
                       (fault.get('reference_terminal') or {}).get('full_name'), step['fault_type'], step['phases'],
                       step['resistance_ohm'], step['reactance_ohm'], step['method'], valid,
                       sample['object']['full_name'], sample['object']['class'],
                       sample['object']['full_name'] if sample['object']['class'] == 'ElmRelay' else parent('ElmRelay'),
                       parent('StaCubic'), parent('ElmSubstat'),
                       *[type_fields.get(key, {}).get('value') for key in ('sfiec', 'sfansi', 'izone')],
                       json.dumps(metadata, ensure_ascii=False, allow_nan=False), name,
                       json.dumps(record['value'], ensure_ascii=False, allow_nan=False) if 'value' in record else '',
                       record.get('unit'), record['status'], json.dumps(record.get('error'), ensure_ascii=False)]
                # Spreadsheet formula protection; authoritative native data stays in JSON.
                writer.writerow(["'" + value if isinstance(value, str) and value.startswith(('=', '+', '-', '@')) else value for value in row])
        stream.flush()
        os.fsync(stream.fileno())


def run_protection_sweep(app, instance_id, info, arguments):
    check_session(instance_id, info, arguments)
    folder = namespace(arguments['sweep_id'])
    request_file = folder / ('request_' + arguments['request_id'] + '.json')
    semantic = {key: value for key, value in arguments.items() if key not in ('confirmation_digest', 'expected_agent_instance_id')}
    signature = digest(semantic)
    if request_file.exists():
        prior = read_checkpoint(request_file)
        if prior['signature'] != signature:
            fail('REQUEST_ID_CONFLICT', 'This durable chunk ID identifies different study inputs.')
        if prior['state'] == 'running':
            fail('SWEEP_OUTCOME_UNKNOWN', 'This chunk has an unfinished checkpoint. Inspect get_protection_sweep_status; do not automatically retry.')
        return {**prior['response'], 'replayed': True}
    if (folder / 'running.json').exists():
        fail('SWEEP_OUTCOME_UNKNOWN', 'Another or interrupted chunk owns this sweep. Inspect its checkpoint before further work.')
    context = model.active_context(app, info)
    original_scenario = app.GetActiveScenario()
    native_path = stored_object(app, info, arguments['path_full_name'])
    path = path_data(native_path)
    members = {obj['full_name'] for obj in path['members']}
    plans, scenarios = [], []
    for step in arguments['steps']:
        if step['target_full_name'] not in members:
            fail('FAULT_OUTSIDE_PROTECTION_PATH', 'Every explicit fault location must be a native member of the selected path.')
        scenario = scenario_object(app, info, step['scenario_full_name'])
        scenarios.append(scenario)
        step_id = 'sweep_' + digest([arguments['sweep_id'], step['step_id']])[:32]
        fault_args = {key: arguments[key] for key in ('command_full_name', 'expected_study_case_full_name',
                                                     'expected_agent_instance_id', 'channels')}
        fault_args.update({key: step[key] for key in FAULT_INPUTS}, request_id=step_id, confirmation_digest=None)
        prepared = prepare_fault(app, instance_id, info, fault_args,
                                 allow_inactive=step['scenario_full_name'] != (context['scenario'] or {}).get('full_name'))
        step_file = folder / ('step_' + step['step_id'] + '.json')
        prior_summary = None
        if step_file.exists():
            prior = read_checkpoint(step_file)
            expected_signature = digest({'step': step, 'command': arguments['command_full_name'],
                                         'path': arguments['path_full_name'], 'channels': arguments['channels']})
            if prior.get('signature') != expected_signature:
                fail('SWEEP_STEP_ID_CONFLICT', 'A durable step ID identifies different inputs; no work was started.')
            if prior.get('state') != 'finished':
                fail('SWEEP_OUTCOME_UNKNOWN', 'An unfinished step requires manual outcome inspection; no work was started.')
            prior_summary = step_summary(step['step_id'], step_file, prior, True)
        plans.append({'step': step, 'fault_arguments': fault_args, 'preview': prepared[-1], 'stored_step': prior_summary})
    needs_switch = any(model.describe(scenario) != context['scenario'] for scenario in scenarios)
    preview = {'context': context, 'path': model.describe(native_path), 'native_path': path,
               'steps': plans, 'channels': arguments['channels'], 'sweep_id': arguments['sweep_id'],
               'request_id': arguments['request_id'], 'discard_unsaved_scenario_changes': arguments['discard_unsaved_scenario_changes'],
               'scenario_switch_required': needs_switch,
               'scenario_note': 'Future scenario states are rechecked after activation. Switching discards unsaved scenario changes; native automatic-control state and old results cannot be restored by this workflow.',
               'storage_directory': str(folder), 'final_context': 'restore the entry scenario and verify the complete entry context',
               'execution': 'Sequential explicit ComShc faults only; no native unbounded sweep command, asynchronous jobs, recorded-file import or coordination verdict.'}
    confirmation = digest(preview)
    if model.active_context(app) != context or model.describe(app.GetActiveScenario()) != context['scenario']:
        fail('SESSION_CHANGED', 'The active scenario or context changed during sweep preparation; no work was started.')
    if arguments['confirmation_digest'] is None:
        return bounded({**info, 'status': 'preview', 'preview': preview, 'confirmation_digest': confirmation,
                        'write_attempted': False, 'execution_attempted': False, 'success': None})
    if confirmation != arguments['confirmation_digest']:
        fail('SWEEP_PREVIEW_CHANGED', 'The reviewed path, inputs, settings or context changed; obtain another preview.')
    if needs_switch and not arguments['discard_unsaved_scenario_changes']:
        fail('SCENARIO_DISCARD_PERMISSION_REQUIRED', 'Explicitly review and allow discarding unsaved scenario changes before switching.')
    folder.mkdir(parents=True, exist_ok=True)
    lock_file = folder / 'running.json'
    try:
        write_checkpoint(lock_file, {'request_id': arguments['request_id'], 'state': 'running'}, exclusive=True)
    except FileExistsError:
        fail('SWEEP_OUTCOME_UNKNOWN', 'This sweep already has a running or interrupted chunk; no native work was started.')
    response = {**info, 'sweep_id': arguments['sweep_id'], 'request_id': arguments['request_id'], 'steps': [],
                'success': False, 'status': 'running', 'replayed': False, 'confirmation_digest': confirmation,
                'context_before': context, 'executed_step_count': 0, 'checkpoint_file': str(request_file)}
    checkpoint = {'signature': signature, 'state': 'running', 'response': response}
    uncertain = False
    try:
        write_checkpoint(request_file, checkpoint, exclusive=True)
        for plan, scenario in zip(plans, scenarios):
            step = plan['step']
            step_file = folder / ('step_' + step['step_id'] + '.json')
            step_signature = digest({'step': step, 'command': arguments['command_full_name'],
                                     'path': arguments['path_full_name'], 'channels': arguments['channels']})
            if step_file.exists():
                prior = read_checkpoint(step_file)
                if prior['signature'] != step_signature:
                    fail('SWEEP_STEP_ID_CONFLICT', 'A durable step ID already identifies different fault/channel inputs.')
                if prior['state'] != 'finished':
                    fail('SWEEP_OUTCOME_UNKNOWN', 'A durable step has an uncertain native outcome; do not repeat it.')
                summary = step_summary(step['step_id'], step_file, prior, True)
            else:
                write_checkpoint(step_file, {'signature': step_signature, 'state': 'started', 'step': step,
                                             'entry_context': context}, exclusive=True)
                uncertain = True  # Also retain quarantine if a BaseException interrupts native work.
                switch_scenario(app, scenario)
                if path_data(native_path) != path:
                    raise RuntimeError('The native protection path changed after scenario selection.')
                prepared = prepare_fault(app, instance_id, info, plan['fault_arguments'])
                if prepared[-1]['settings_before'] != plan['preview']['settings_before']:
                    raise RuntimeError('Command settings changed after scenario selection; no fault was executed.')
                result = run_short_circuit_fault(app, instance_id, {}, info,
                    {**plan['fault_arguments'], 'confirmation_digest': digest(prepared[-1])})
                response['executed_step_count'] += int(result['execution_attempted'])
                csv_file = folder / ('step_' + step['step_id'] + '.csv')
                write_step_csv(csv_file, arguments, step, result)
                finished = {'signature': step_signature, 'state': 'finished', 'step': step,
                            'response': result, 'csv_file': str(csv_file)}
                write_checkpoint(step_file, finished)
                summary = step_summary(step['step_id'], step_file, finished)
            response['steps'].append(summary)
            uncertain = summary['execution_status'] == 'indeterminate' or summary['restoration'] != 'restored'
            write_checkpoint(request_file, checkpoint)
            if not summary['success']:
                break
    except Exception as exc:
        uncertain = True
        response['error'] = {'code': getattr(exc, 'code', 'SWEEP_STEP_FAILED'), 'message': (type(exc).__name__ + ': ' + str(exc))[:320]}
    finally:
        restoration_error = None
        try:
            if model.session_info(app) != info:
                raise RuntimeError('Active project/study case changed; scenario restoration was not attempted in that context.')
            switch_scenario(app, original_scenario)
            after = model.active_context(app)
            restored = after == context
        except Exception as exc:
            after, restored = None, False
            restoration_error = (type(exc).__name__ + ': ' + str(exc))[:320]
        response['context_restoration'] = {'success': restored, 'context_after': after, 'error': restoration_error,
                                           'prior_results_restored': False}
        uncertain = uncertain or not restored
        complete = len(response['steps']) == len(plans) and all(item['success'] for item in response['steps'])
        response.update(success=complete and not uncertain, status='completed' if complete and not uncertain else 'stopped',
                        recovery_required=uncertain, unstarted_step_ids=[p['step']['step_id'] for p in plans[len(response['steps']):]])
        checkpoint['state'] = response['status']
        try:
            write_checkpoint(request_file, checkpoint)
        except Exception as exc:
            uncertain = True
            response.update(success=False, recovery_required=True, persistence_error=(type(exc).__name__ + ': ' + str(exc))[:320])
        if not uncertain:
            lock_file.unlink()
    return bounded(response)


def recorded_metadata(result, index=None):
    args = () if index is None else (index,)
    metadata, errors = {}, {}
    for key, method in (('variable', 'GetVariable'), ('unit', 'GetUnit'), ('description', 'GetDescription')):
        try:
            value = getattr(result, method)(*args)
            if not isinstance(value, str) or len(value) > 512:
                raise ValueError('Expected native metadata text of at most 512 characters.')
            metadata[key] = value
        except Exception as exc:
            metadata[key] = None
            errors[key] = (type(exc).__name__ + ': ' + str(exc))[:320]
    try:
        metadata['object'] = model.describe(result.GetObject(*args))
    except Exception as exc:
        metadata['object'] = None
        errors['object'] = (type(exc).__name__ + ': ' + str(exc))[:320]
    return {**metadata, 'index': index, 'metadata_errors': errors}


def recorded_cell(result, row, column=None, kind='number'):
    args = (row,) if column is None else (row, column)
    try:
        native = getattr(result, 'GetObjectValue' if kind == 'object' else 'GetValue')(*args)
        if not isinstance(native, (list, tuple)) or len(native) != 2 or type(native[0]) is not int:
            raise ValueError('Expected native [error_code, value].')
        if native[0] != 0:
            return {'status': 'unavailable' if native[0] == 3 else 'error', 'native_code': native[0]}
        raw = native[1]
        if kind == 'number' and type(raw) not in (int, float):
            raise ValueError('Expected a native scalar numeric value.')
        if kind == 'object' and raw is not None and not callable(getattr(raw, 'GetFullName', None)):
            raise ValueError('Expected a native object reference or null.')
        record = {'status': 'ok', 'native_code': 0, 'value': model.json_attribute_value(raw)}
        if kind == 'object':
            # Keep the recorded index as well as its native decoded object identity.
            record['encoded_value'] = recorded_cell(result, row, column, 'number')
        return record
    except Exception as exc:
        return {'status': 'error', 'error': (type(exc).__name__ + ': ' + str(exc))[:320]}


def read_protection_sweep_results(app, info, arguments):
    """Bounded recorded-data access; source context/freshness is never inferred."""
    context = model.active_context(app, info)
    source = stored_object(app, info, arguments['source_full_name'])
    cls = str(source.GetClassName())
    result = source if cls == 'ElmRes' else source.GetAttribute('res' if cls == 'ComShcsweep' else 'p_res')
    if result is None or str(result.GetClassName()) != 'ElmRes':
        fail('RECORDED_RESULTS_UNAVAILABLE', 'The selected source has no assigned native ElmRes.', 404)
    identity = model.describe(result)
    if not identity['full_name'].startswith(info['project']['full_name'] + '\\'):
        fail('STUDY_OBJECT_OUT_OF_SCOPE', 'Recorded result access is limited to the active project.', 403)
    loaded = False
    release_error = None
    try:
        loaded = True
        result.Load()
        rows, count = result.GetNumberOfRows(), result.GetNumberOfColumns()
        if type(rows) is not int or rows < 0 or type(count) is not int or count < 0:
            fail('RECORDED_RESULTS_UNAVAILABLE', 'Native result dimensions are unavailable.')
        offset, limit, selection = arguments['offset'], arguments['limit'], arguments['columns']
        if selection is not None and any(column['index'] >= count for column in selection):
            fail('RECORDED_COLUMN_OUT_OF_RANGE', 'Select indices from the recorded column-metadata page.', 400)
        indices = range(offset, min(count, offset + limit)) if selection is None else [column['index'] for column in selection]
        columns = [recorded_metadata(result, index) for index in indices]
        axis = recorded_metadata(result)
        values = []
        if selection is not None:
            for row in range(offset, min(rows, offset + limit)):
                values.append({'index': row, 'axis': recorded_cell(result, row, kind=arguments['axis_kind']),
                    'values': {str(column['index']): recorded_cell(result, row, column['index'], column['value_kind'])
                               for column in selection}})
        if result.GetNumberOfRows() != rows or result.GetNumberOfColumns() != count:
            fail('RECORDED_RESULTS_CHANGED', 'Result dimensions changed while reading; no page was exported.')
    finally:
        if loaded:
            try:
                result.Release()
            except Exception as exc:
                release_error = (type(exc).__name__ + ': ' + str(exc))[:320]
    if model.active_context(app) != context:
        fail('SESSION_CHANGED', 'Active context changed during the recorded read; no page was exported.')
    total = count if selection is None else rows
    cells = [cell for row in values for cell in [row['axis'], *row['values'].values()]]
    response = bounded({**info, 'context': context, 'source': model.describe(source), 'result_file': identity,
        'row_count': rows, 'column_count': count, 'axis': axis, 'axis_kind': arguments['axis_kind'],
        'columns': columns, 'selection': selection, 'rows': values, 'offset': offset, 'limit': limit,
        'next_offset': offset + limit if offset + limit < total else None,
        'read_complete': not release_error and not axis['metadata_errors'] and all(not column['metadata_errors'] for column in columns)
                         and all(cell['status'] == 'ok' and cell.get('encoded_value', {}).get('status', 'ok') == 'ok' for cell in cells),
        'unavailable_cell_count': sum(cell['status'] == 'unavailable' for cell in cells),
        'error_cell_count': sum(cell['status'] == 'error' for cell in cells), 'release_error': release_error,
        'fault_provenance': {'status': 'unknown', 'calculation_request_id': None},
        'note': 'Existing recorded values only; Load/Release manages native read memory. No Execute, Flush, variable registration or model write. The default axis may be distance, index or time; no axis meaning, freshness or model revision is inferred. Numeric/object value kinds are explicit client selections. Sparse native code 3 is unavailable, never zero. Page boundaries are row indices, not interpolated or regularized samples.'})
    filename = arguments['file_name']
    if filename is not None:
        folder = namespace('recorded')
        folder.mkdir(parents=True, exist_ok=True)
        destination = folder / filename
        if destination.exists():
            fail('EXPORT_FILE_EXISTS', 'Choose a new recorded-page basename; existing artifacts are never overwritten.')
        if filename.endswith('.json'):
            write_checkpoint(destination, response, exclusive=True)
        else:
            with destination.open('x', encoding='utf-8', newline='') as stream:
                writer = csv.writer(stream)
                writer.writerow(['result_file', 'row', 'axis_json', 'column', 'source_object_json', 'variable_json', 'unit_json', 'value_kind', 'cell_json'])
                for row in values:
                    for column, choice in zip(columns, selection):
                        writer.writerow([identity['full_name'], row['index'], json.dumps(row['axis'], ensure_ascii=False),
                            column['index'], json.dumps(column['object'], ensure_ascii=False), json.dumps(column['variable'], ensure_ascii=False),
                            json.dumps(column['unit'], ensure_ascii=False), choice['value_kind'],
                            json.dumps(row['values'][str(column['index'])], ensure_ascii=False, allow_nan=False)])
                stream.flush()
                os.fsync(stream.fileno())
        response['export_file'] = str(destination)
    return response
