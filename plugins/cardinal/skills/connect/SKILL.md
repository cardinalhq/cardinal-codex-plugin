---
name: cardinal-connect
description: Connect Codex to Cardinal — runs the device-code flow to enable telemetry to the Outcomes Dashboard AND the unified Cardinal MCP tools, in one consent.
disable-model-invocation: false
---

# /cardinal:connect

Wires Codex up to a Cardinal workspace. **Enables both sides at
once by default**:

- **Telemetry** — Codex's native OpenTelemetry exporter streams to
  Cardinal's Outcomes Dashboard, and the plugin's `cardinal.*`
  enrichment hooks add per-turn events on top. Configured via the
  `[otel]` block in `~/.codex/config.toml`; the hooks read
  `~/.codex/cardinal.json` directly.
- **MCP** — the unified `cardinal` MCP server appears in this Codex
  session, exposing whichever tools the org has integrations
  configured for. `cardinal-connect` writes `[mcp_servers.cardinal]`
  into `~/.codex/config.toml`.

Both are minted in one browser-approved consent via the maestro
device-code flow. The MCP URL is a single durable endpoint per org
(`https://<host>/api/orgs/<uuid>/mcp`) whose aggregator fans out to
whatever integrations are configured — adding / removing integrations
on the Cardinal side never requires re-running this command.

## How you (the model) should run this

**You MUST run `cardinal-connect` in the background.** The script
blocks for up to 10 minutes waiting for the user to approve in their
browser; the Bash tool's stdout is buffered until the call returns, so
if you don't background it the user never sees the verification URL.

Invoke via the Bash tool with `run_in_background: true`:

```
cardinal-connect
```

Then surface the URL via the pending side-channel file:

1. After kicking off the background bash call, poll
   `~/.codex/cardinal-pending.json` — the script writes it within
   1–2 seconds of starting. Read up to 5 times with 1-second gaps.
2. Parse the JSON. Shape:
   ```json
   {
     "verification_uri": "https://app.cardinalhq.io/connect?code=ABCD-EFGH",
     "user_code": "ABCD-EFGH",
     "expires_in": 600,
     "written_at": "2026-06-05T05:40:59Z",
     "plugin_version": "0.1.0"
   }
   ```
3. **Show `verification_uri` to the user prominently** — wrap it in
   a code fence (a real markdown link is fine too) and say something
   like "Open this in your browser, log in if needed, pick your org,
   and click Approve. I'm watching for it." Do NOT block on it
   yourself; the background bash call is doing that.
4. Wait for the background bash call to complete. Codex will
   notify you when it finishes; until then you can answer side
   questions, but don't run another long-blocking command in the
   same conversation.
5. When the background call returns, read its final stdout for the
   success summary or the error. Surface it to the user verbatim.

The pending file is deleted automatically when `cardinal-connect`
exits — success, denied, expired, or error.

### What the script actually does

1. POST to `/api/auth/device/code` to start the flow.
2. Writes the verification URL to `~/.codex/cardinal-pending.json`
   (this is what step 1 above reads).
3. Polls `/api/auth/device/token` until approval lands (or the user
   denies / the 10-minute TTL expires).
4. Writes on success (preserving any unrelated keys, atomic, `0600`
   for the state file):
   - **`~/.codex/config.toml`** — an `[otel]` exporter block (telemetry
     side) + a `[mcp_servers.cardinal]` block (MCP side).
   - **`~/.codex/cardinal.json`** — full ingest key, MCP key, and
     connection metadata for `/cardinal:status`, `/cardinal:disconnect`,
     and the hooks.
   - **`~/.codex/hooks.json`** — merges the plugin's `cardinal.*`
     enrichment hook entries; unrelated hooks are preserved.
5. Probes both endpoints to confirm the keys actually authenticate.
6. Deletes `~/.codex/cardinal-pending.json` on exit.

## Flags

- `--telemetry-only` — request only the ingest scope. The
  `[mcp_servers.cardinal]` block is NOT written; only the `[otel]`
  block and the enrichment hooks land.
- `--rotate` — proceed even when state shows we're already connected.
  Mints fresh keys; the previous ones stay alive until their TTL or
  until `/cardinal:disconnect` revokes them.
- `--host <url>` — Cardinal host (default `https://app.cardinalhq.io`).
- `--no-tool-details` — opt out of OTel tool-details capture.
- `--deployment-env <name>` — override the derived
  `deployment.environment` label.
- `--dry-run` — run the device-code flow, print what would be written.

## How the MCP side actually wires up (for the curious)

`cardinal-connect` writes a `[mcp_servers.cardinal]` block into
`~/.codex/config.toml` pointing at the org's durable aggregator URL
(`https://<host>/api/orgs/<uuid>/mcp`), authenticated with the minted
MCP key in Codex's masked `http_headers` field. Codex loads MCP servers
from `config.toml` when a thread starts, so the `cardinal` server comes
online on the next new thread — no per-tool re-registration. As your
admin enables more integrations on the Cardinal side, the same URL
surfaces more tools on the next `tools/list`; you don't need to re-run
`/cardinal:connect`.

## A note about `--no-tool-details`

Tool-details capture is **on by default** because without it the
Outcomes Dashboard can't derive `repo` or `service` from per-step
events — every session shows as `repo=unknown` and `service=unknown`.
Bash command lines and file paths may contain PII some orgs' privacy
policies forbid. If the user's org has such a policy, suggest
`--no-tool-details`.

## After success

Tell the user:

1. `~/.codex/config.toml` ( `[otel]` + `[mcp_servers.cardinal]` ),
   `~/.codex/cardinal.json`, and `~/.codex/hooks.json` have been
   updated.
2. **Start a new Codex thread/session** to pick up the plugin. Codex
   reads `config.toml` and `hooks.json` when a thread starts, so the
   new wiring comes online on the next thread, not the current one.
3. Run `/cardinal:status` from the new session to verify both sides.

## Errors

Surface the script's stderr verbatim and don't claim success. Common
cases:

- `Cardinal is already connected as ...` — exit 2 from the
  already-connected guard. Re-run with `--rotate` to overwrite.
- `Consent request expired before approval` — the 10-minute TTL
  elapsed; re-run.
- `Request was denied in the browser` — the user clicked Deny.
- `config.toml is not valid TOML` — the script refuses to write into
  an unparseable file. Tell the user to fix or back up the file.
- `ingest reachability failed` / `MCP reachability failed` — the
  newly-minted keys don't authenticate at the endpoint. Usually means a
  maestro misconfig (org has no active lakerunner integration for the
  ingest side, gateway not running for the MCP side).
