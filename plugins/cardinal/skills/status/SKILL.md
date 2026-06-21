---
name: cardinal-status
description: Verify the Cardinal plugin's wiring on this Codex install (telemetry and/or MCP).
disable-model-invocation: false
---

# /cardinal:status

Reports both sides of the plugin's wiring depending on the recorded
mode:

- `telemetry-and-mcp` (default after `/cardinal:connect`) — both sides.
- `telemetry-only` (after `/cardinal:connect --telemetry-only`) —
  `[otel]` block + enrichment hooks only.
- `mcp-only` (rare) — `[mcp_servers.cardinal]` only.

For each enabled side it shows the configured endpoint, key prefix, and
key age, and probes the endpoint for reachability.

## How you (the model) should run this

Invoke via the Bash tool:

```
cardinal-status
```

The script reads `~/.codex/cardinal.json` (the state file) and reports:

- Mode, user email, org, host, plugin version, connection age.
- **Telemetry side** (when enabled): the ingest endpoint, key prefix,
  whether tool-details capture is on, that the `[otel]` block is present
  in `~/.codex/config.toml` and the enrichment hooks are merged into
  `~/.codex/hooks.json`, and a reachability probe.
- **MCP side** (when enabled): the MCP URL, key prefix, that
  `[mcp_servers.cardinal]` is present in `~/.codex/config.toml`, and a
  reachability probe.

If `~/.codex/cardinal.json` doesn't exist, surfaces "not connected"
and suggests `/cardinal:connect`. If state says connected but the
matching `config.toml` block is absent or a probe returns 401/403,
surfaces a clear repair hint (`/cardinal:connect --rotate`).
