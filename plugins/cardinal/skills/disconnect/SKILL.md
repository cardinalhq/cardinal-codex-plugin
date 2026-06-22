---
name: cardinal-disconnect
description: Disconnect this Codex install from Cardinal — revoke the MCP key, strip the plugin's config blocks and hooks, delete local state.
disable-model-invocation: false
---

# /cardinal:disconnect

Reverses what `/cardinal:connect` did:

1. Best-effort POST to `/api/maestro-keys/<mcp_key_id>/revoke`. The
   plugin reads the plaintext MCP key from `~/.codex/cardinal.json` and
   authenticates as the key itself (R11 §1 "self" path).
2. Strips the plugin-owned blocks from `~/.codex/config.toml` — the
   `[otel]` exporter block and `[mcp_servers.cardinal]` (with a backup
   before mutating). Unrelated config keys stay. The enrichment hooks
   are NOT touched: Codex auto-registers them and drops that
   registration when the plugin is removed (`codex plugin remove`).
3. Deletes `~/.codex/cardinal.json`.

The ingest key is not revoked server-side — the maestro endpoint
hasn't shipped yet. The script prints a pointer to the admin UI.

## How you (the model) should run this

Codex does not put the plugin's `bin/` on `$PATH`, so invoke the script
by its installed absolute path. Resolve it with the Bash tool (picks the
highest installed version):

```
"$(ls -d "${CODEX_HOME:-$HOME/.codex}"/plugins/cache/*/cardinal/*/bin 2>/dev/null | sort -V | tail -1)/cardinal-disconnect"
```

### Flags

- `--force` — proceed even if `~/.codex/cardinal.json` is missing.
- `--keep-telemetry` — only remove the MCP side. Keeps the `[otel]`
  block in place. Useful for going from `telemetry-and-mcp` back to
  `telemetry-only` without re-running connect. (The enrichment hooks
  stay auto-registered regardless.)

## After success

Tell the user:

1. The MCP key has been revoked server-side (if the revoke call
   succeeded — the script reports either way).
2. The ingest key is still active server-side; revoke it via
   `https://<host>/settings/api-keys` for a clean disconnect.
3. Start a new Codex thread/session so it picks up the `config.toml`
   change. Without `[mcp_servers.cardinal]` the `cardinal` server won't
   load, and without the `[otel]` block no telemetry is emitted —
   effectively off on the next thread. (The enrichment hooks stay
   registered until the plugin itself is removed, but they no-op once
   `~/.codex/cardinal.json` is gone.)
