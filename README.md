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

PowerFactory's GUI stays busy while the agent runs. Stop it from the repository folder:

```powershell
.\.venv\Scripts\python.exe -c "from pf_bridge import PFBridge; PFBridge().stop()"
```

Restart the agent and MCP server after updates. A timeout does not cancel native work; check its status before retrying.

## Security

The bridge uses authenticated HTTP on `127.0.0.1`. Credentials are stored in `%LOCALAPPDATA%\PowerFactoryMCP`, restricted to the current Windows user and SYSTEM. Never publish credentials, model data or exports, and do not expose the listener through a proxy or tunnel.

Independent integration; not affiliated with DIgSILENT.
