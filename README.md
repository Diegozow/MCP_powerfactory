# PowerFactory MCP

Connect an MCP client to an existing DIgSILENT PowerFactory session. Inspect models, read native values, run configured power-flow/short-circuit calculations, and export results. RMS execution and time-series reading are not implemented.

## Requirements

- Windows and licensed PowerFactory 2024 with Python/ComPython configured.
- Python 3.12 and an MCP client supporting stdio.

## Setup

1. Install dependencies in an external environment, outside PowerFactory:

   ```powershell
   py -3.12 -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

2. In PowerFactory, activate a project and study case. Configure a ComPython script to run `pf_agent.py`, add this repository to its Python module search path, and execute it. Keep all Python modules and `pf_profiles.json` together.

3. Configure your MCP client, replacing the example paths:

   ```json
   {
     "mcpServers": {
       "powerfactory": {
         "command": "C:/path/to/powerfactory-mcp/.venv/Scripts/python.exe",
         "args": ["C:/path/to/powerfactory-mcp/pf_mcp_server.py"]
       }
     }
   }
   ```

4. Check the connection with `get_powerfactory_capabilities`, then `get_active_session`.

## Usage

`pf_profiles.json` defines parameters and calculation-specific results. Set `include_results=true` to add defaults from valid calculations. Explicit result reads may contain stale values.

For protection relays, use `list_model_elements(element_class="ElmRelay")`, then pass a returned relay `full_name` to `get_element_attributes` or `get_equipment_type_attributes`. The type read follows the relay's assigned `typ_id` to `TypRelay`. Select `profile="extended"` for relay CT/VT parameters and broader type metadata; relay-definition and block references remain object metadata. Batch reads support `include_equipment_types=true`. Set `include_results=true` to include `c:yout` (tripping time) where available and short-circuit results are valid.

Protection blocks `RelToc` (time overcurrent) and `RelIoc` (instantaneous overcurrent, capital I) use the same listing, discovery, single/batch attribute and equipment-type tools. Their assigned types are `TypToc` and `TypIoc`. Basic element profiles include pickup, direction and time settings; extended profiles add reset/blocking settings and metadata. Basic type profiles include `loc_name`, `sfiec`, `sfansi`, `iunit`, `atype`, `Ts`, `Kr` and `Tr`; extended profiles add ranges, model options and references. With `include_results=true`, valid short-circuit defaults include block tripping time `s:yout` and available scalar/phase `s:Iabs` and `c:Ipabs` current variables. Scalar and A/B/C variants are checked individually and missing variants are reported as exclusions. Complete field lists are in `pf_profiles.json`.

Use `get_protection_chain(full_name=...)` with an exact listed `ElmRelay` or browsed `StaCubic` path in the active project. It reads nested protection blocks, native `pelm` references to CT/VT/measurement blocks, explicit `RelLogdip`/`RelLogic.pSwitch` references, available settings and one assigned-type hop. Nodes retain original values, units and attribute errors. Follow `next_offset` for all nodes and their outgoing relationships; check `read_complete`, `errors` and `excluded_attributes`. Containment is separate from measurement/trip references and does not prove which breaker operates. Other reference targets remain metadata. The tool performs no calculation, logic evaluation or model write.

Use `get_graphic_selection()` to read the top network diagram's exact page/diagram identities and native selection. Where `inspection.available=true`, pass `inspection.full_name` to the existing model inspection tools. Graphic symbols follow their native `pDataObj` reference; annotations, unsupported classes and non-calculation-relevant objects remain explicit. Empty selection and missing diagram are distinct states. With GUI event processing enabled, selections can change between requests; otherwise, select before launching the agent.

Use `mark_in_graphics(full_names=[...], expected_diagram_full_name=..., expected_study_case_full_name=..., expected_agent_instance_id=...)` only for requested highlighting. Copy the diagram identity from `get_graphic_selection` and case/agent identities from the session. It prechecks 1–8 exact supported model elements against symbols in the active diagram, skipping absent/uncertain symbols. The native method searches open diagrams and can activate another one; inspect the returned before/after state. Native completion does not establish visual highlighting, and highlighting is distinct from diagram selection. This changes UI state only; no symbols, electrical objects, settings, calculations or saves are requested.

Use `get_short_circuit_contributions(bus_full_name=...)` with an exact listed `ElmTerm` path to read existing bus Ikss/angle and currents at directly connected element ends. It returns original units, endpoint/winding identities, switch/service states, current fault settings and native validity. Lines, two/three-winding transformers, couplers and directly connected machines/static generators/external grids are supported; other classes and missing results remain explicit. Follow `next_offset` and check `read_complete`/`coverage_complete`. Current settings do not prove the settings used for stored results; all-bus/multiple-fault results must not be treated as contributions to one fault. The tool performs no calculation, current summation or tracing to remote sources.

Use `run_short_circuit_at_bus(command_full_name=..., bus_full_name=..., fault_type=..., expected_study_case_full_name=..., expected_agent_instance_id=..., request_id=...)` to preview a fault at one exact bus. Review the preview, then supply its `confirmation_digest` with the same arguments for an authorized run. It supports in-service three-phase AC buses and existing IEC 60909 or Complete commands with multiple faults and enhanced impedance disabled; `fault_type` is `3psc`, `2psc`, `spgf` or `2pgf`. Only target/type settings are temporarily changed; method, impedance and phase choices remain configured. Results default to the fault bus and are sampled before settings restoration. Check `execution_success`, `outcome`, `restoration` and `validity_after_restoration` separately. Restoring settings does not restore prior results. Confirmed repeats replay the stored attempt; after a timeout, query `get_calculation_status` with the same ID and inspect settings before further work. Native field names were checked against the installed PowerFactory 2024 schema; Complete-method execution and replay have been verified live; IEC 60909 and restoration after changed settings remain pending.

Use `set_operating_setpoints` to preview and change one exact `ElmLod` (`plini`/`qlini`), `ElmSym` or `ElmGenstat`/BESS (`pgini`/`qgini`). Provide one or two `setpoints`, each with `attribute`, `value`, `expected_value` and the exact reported native `unit` (`W`/`kW`/`MW` or `var`/`kvar`/`Mvar`), plus expected case, agent, active scenario and recording-stage paths; pass explicit `null` for absent scenario/stage. Review the preview's values, controller/scaling metadata and recording destination, then supply its `confirmation_digest` for an authorized write. Native input mode must be `PQ` or `DEF`; characteristic-driven inputs are excluded. DEF retains the native default selection and is not assumed to mean PQ. The preview records the verified mode; check `input_mode_after` and `input_mode_unchanged` after a write. Active scenarios require one non-excluded grid subset; type-based, missing or ambiguous subsets are unsupported. Expected prior values require exact equality; readback allows numerical roundoff of 1e-12 relative/absolute in the reported unit. Stored P/Q readback does not prove achieved power. Check `success`, `parameters_after`, `errors` and `context_after`. Partial writes remain applied; restoration needs a new explicit request using current values. Calculations and explicit saving are separate operations; native auto-save can persist edits. After a timeout, inspect context and exact values before further work. T39 passed 200 local tests, including DEF/PQ stored-input changes and explicit restoration. The earlier 12 live read-only checks covered the original PQ-only guard. Both DEF and PQ passed 44 native HTTP/agent checks for a synchronous-generator active-power change and explicit restoration in the base grid. Independent final MCP reads confirmed all ten sampled parameters restored, including the original DEF/controller modes. Native load/static-generator writes, reactive-power changes and scenario/stage recording remain unverified.

Normal agent startup enables GUI event processing between requests on supported 64-bit Windows PowerFactory Qt5 installations. Selection, zoom and pan remain available while the agent waits; calculations, request processing and modal dialogs remain synchronous. Check `get_active_session().session.gui` for its status. Unsupported bindings fall back to blocking mode. A ComPython wrapper can call `pf_agent.main(gui_events=False)` to select blocking mode explicitly.

Stop the agent from the repository folder:

```powershell
.\.venv\Scripts\python.exe -c "from pf_bridge import PFBridge; PFBridge().stop()"
```

Restart the agent and MCP server after updates. A timeout does not cancel native work; check its status before retrying.

At startup, the agent validates its retained application reference and replaces it if PowerFactory reports that it was deleted. If the replacement is also deleted, close and reopen PowerFactory, then run `pf_agent.py` through ComPython again. Nested agent launches are rejected; stop the existing agent before launching another one.

## Security

The bridge uses authenticated HTTP on `127.0.0.1`. Credentials are stored in `%LOCALAPPDATA%\PowerFactoryMCP`, restricted to the current Windows user and SYSTEM. Never publish credentials, model data or exports, and do not expose the listener through a proxy or tunnel.

Independent integration; not affiliated with DIgSILENT.
