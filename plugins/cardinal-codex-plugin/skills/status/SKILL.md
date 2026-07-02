---
name: cardinal-status
description: Check the Cardinal Codex connection and probe configured telemetry and MCP endpoints.
---

# Cardinal Status

Use this skill when the user asks whether Codex is connected to Cardinal, whether Cardinal telemetry or MCP is configured, or to verify Cardinal setup.

Run:

```bash
python3 scripts/cardinal-status
```

Surface the script output clearly. If it says Codex needs a restart, tell the user that MCP and hook config are loaded at process start.
