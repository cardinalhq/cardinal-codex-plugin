---
name: cardinal-connect
description: Connect Codex to Cardinal by running the device-code flow and configuring telemetry hooks plus the unified Cardinal MCP server.
---

# Cardinal Connect

Use this skill when the user asks to connect Codex to Cardinal, enable Cardinal telemetry, enable Cardinal MCP tools, rotate a Cardinal connection, or run Cardinal setup.

Run the repository script:

```bash
python3 scripts/cardinal-connect
```

If the user asks for a non-production Cardinal host, pass `--host <url>`. If the script reports that Cardinal is already connected, ask whether to rotate or rerun with `--rotate` when the user has already asked to overwrite.

The script prints an approval URL. Show that URL to the user and wait for the script to finish. On success, tell the user to restart Codex so it reloads `~/.codex/config.toml` and `~/.codex/hooks.json`, review/trust the new Cardinal hooks when Codex prompts, then suggest `cardinal-status`.

Do not claim Codex native OpenTelemetry was enabled. The plugin emits Cardinal-compatible telemetry from Codex hooks and local Codex transcripts.
