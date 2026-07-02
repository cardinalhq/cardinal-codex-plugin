---
name: cardinal-disconnect
description: Disconnect Codex from Cardinal by revoking keys and removing managed Codex MCP and hook config.
---

# Cardinal Disconnect

Use this skill when the user asks to disconnect Cardinal, remove Cardinal telemetry hooks or MCP tools from Codex, or revoke Cardinal keys.

Run:

```bash
python3 scripts/cardinal-disconnect
```

If the state file is missing and the user still wants cleanup, rerun with `--force`.

After success, tell the user to restart Codex so it reloads MCP and hook configuration.
