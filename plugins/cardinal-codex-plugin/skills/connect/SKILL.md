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

Besides the ingest and MCP keys, the script requests a control-plane token (`maestro:act`) with `dashboards:write`, `alerts:write` and `telemetry:query`, so skills like migrate-from-grafana can write dashboards and alert rules and query telemetry as the user — bounded by their org role; `telemetry:query` is read-only in every org they belong to. Mention that when you show the approval URL. The token is stored 0600 in `cardinal-secrets.json` and `cardinal-disconnect` revokes it. Pass `--minimal-scopes` for users who don't want it. A Cardinal server that doesn't offer those scopes yet makes the script connect without them and say so. Users connected before this get the token with `--rotate`.

The script prints an approval URL. Show that URL to the user and wait for the script to finish. On success, tell the user to restart Codex so it reloads `~/.codex/config.toml` and `~/.codex/hooks.json`, review/trust the new Cardinal hooks when Codex prompts, then suggest `cardinal-status`.

Do not claim Codex native OpenTelemetry was enabled. The plugin emits Cardinal-compatible telemetry from Codex hooks and local Codex transcripts.
