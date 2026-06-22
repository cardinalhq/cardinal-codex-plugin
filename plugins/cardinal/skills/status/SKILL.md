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

Codex does not put the plugin's `bin/` on `$PATH`, so invoke the script
by its installed absolute path. Resolve it with the Bash tool (picks the
highest installed version):

```
"$(ls -d "${CODEX_HOME:-$HOME/.codex}"/plugins/cache/*/cardinal/*/bin 2>/dev/null | sort -V | tail -1)/cardinal-status"
```

The script reads `~/.codex/cardinal.json` (the state file) and reports:

- Mode, user email, org, host, plugin version, connection age.
- **Telemetry side** (when enabled): the ingest endpoint, key prefix,
  whether tool-details capture is on, that the `[otel]` block is present
  in `~/.codex/config.toml`, that Codex has auto-registered the plugin's
  enrichment hooks (counted from the `[hooks.state]` entries in
  `~/.codex/config.toml`), and a reachability probe.
- **MCP side** (when enabled): the MCP URL, key prefix, that
  `[mcp_servers.cardinal]` is present in `~/.codex/config.toml`, that
  auth is available from `cardinal.json` or `http_headers`, and a
  reachability probe. Legacy env-var registrations are reported with a
  rotate hint.

If `~/.codex/cardinal.json` doesn't exist, surfaces "not connected"
and suggests `/cardinal:connect`. If state says connected but the
matching `config.toml` block is absent or a probe returns 401/403,
surfaces a clear repair hint (`/cardinal:connect --rotate`).
