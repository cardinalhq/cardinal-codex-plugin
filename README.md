# Cardinal Codex plugin

The Codex CLI twin of the [Cardinal](https://cardinalhq.io) plugin. Connect Codex to Cardinal in a single browser-approved consent:

- **Telemetry** — agent sessions stream to the Cardinal Outcomes Dashboard (workflow classification, cost per satisfied outcome, anti-pattern detection, shared plan candidates). Codex's native `[otel]` exporter carries the session/turn stream; the plugin's `cardinal.*` enrichment hooks add per-turn git/usage/plan events on top.
- **MCP** — the unified `cardinal` MCP server appears in your Codex session, exposing whichever observability and integration tools your org has configured (lakerunner, common, github, jira, kube, …).

Both are minted by maestro's device-code flow and committed to your local config atomically. Use `--telemetry-only` if you want the Outcomes Dashboard side but no Cardinal tools in your Codex palette.

## Install

The marketplace lives in this repo at `.agents/plugins/marketplace.json` and points at `./plugins/cardinal`.

```bash
codex plugin marketplace add cardinalhq/cardinal-codex-plugin
codex plugin add cardinal@cardinalhq-codex-plugin
```

(Add a local path instead of the repo slug if you're developing against a checkout.)

## Connect

```
/cardinal:connect
```

Or run `bin/cardinal-connect` directly. The plugin prints a `https://app.cardinalhq.io/connect?code=ABCD-EFGH` URL — open it in your browser, log in (if you're not already), pick the org to connect, and click **Approve**. The plugin's poller picks up your consent within a few seconds and writes:

| File | What gets written |
|---|---|
| `~/.codex/config.toml` | `[otel]` exporter block (telemetry side) + `[mcp_servers.cardinal]` block (MCP side) |
| `~/.codex/cardinal.json` | Full ingest key + non-secret state and key ids, read by the hooks and by `/cardinal:status` / `/cardinal:disconnect` (written `0600`) |
| `~/.codex/hooks.json` | The plugin's `cardinal.*` enrichment hook entries, merged in (unrelated hooks preserved) |

Then **start a new Codex thread/session** — `config.toml` and `hooks.json` are read when a thread starts, so the wiring comes online on the next thread, not the current one.

Run `/cardinal:status` from the new session to verify both sides.

### Variants

```
/cardinal:connect --telemetry-only      # [otel] + hooks only; skip the MCP block
/cardinal:connect --rotate              # Mint fresh keys, overwrite existing config
/cardinal:connect --host https://...    # Point at dogfood / customer in-VPC install
/cardinal:connect --no-tool-details     # Privacy-conscious opt-out (see warning below)
/cardinal:connect --dry-run             # Run the device-code flow, print what would be written
```

## What it does

### Telemetry side

The plugin owns an `[otel]` block in `~/.codex/config.toml` so Codex's native exporter streams its session/turn telemetry to Cardinal:

```toml
[otel]
environment = "<deployment_env>"
exporter = { otlp-http = { endpoint = "<your region's intake host>", protocol = "binary", headers = { "x-cardinalhq-api-key" = "<your key>" } } }
```

On top of that, the plugin's hooks (merged into `~/.codex/hooks.json`) POST the `cardinal.*` enrichment events — `cardinal.git_state`, `cardinal.turn_usage`, `cardinal.turn_tool`, `cardinal.subagent_usage`, `cardinal.plan_state`, `cardinal.plan_usage` — directly to the ingest endpoint using the full key in `~/.codex/cardinal.json`. The Outcomes Dashboard consumes the Codex and Claude Code runtimes uniformly.

Any other keys you have in `config.toml` are left alone. `/cardinal:disconnect` removes only the blocks above and leaves the rest.

### MCP side

`cardinal-connect` writes a `[mcp_servers.cardinal]` block into `~/.codex/config.toml` pointing at your org's durable aggregator URL with the minted MCP key:

```
mcp_url = https://<host>/api/orgs/<org-uuid>/mcp
```

The URL points at the **aggregator** — a single durable endpoint that exposes whatever tools your org has integrations for. As your admin enables more integrations on the Cardinal side, the same URL surfaces more tools on the next `tools/list`. **You don't need to re-run `/cardinal:connect` to "see" new tools.**

## Privacy

Tool-details capture is **on by default**. Without it, the Outcomes Dashboard can't derive `repo` or `service` per session — every event would show as `repo=unknown`, `service=unknown`. Bash command lines and file paths may contain PII; if your org's privacy policy forbids capturing those, pass `--no-tool-details` to `/cardinal:connect`.

Full prompt text is **never** captured by this plugin. Codex's `[otel]` exporter has a `log_user_prompt` switch (off by default); the plugin leaves it off. If you want it, edit `~/.codex/config.toml` by hand after running connect.

## Commands

| Command | What it does |
|---|---|
| `/cardinal:connect` | Runs the device-code flow and wires up both telemetry and MCP. Use `--telemetry-only` to skip the MCP side, `--rotate` to overwrite an existing config. |
| `/cardinal:status` | Show the configured mode, host, org, both endpoints, key prefixes, connection age, and a reachability probe against each enabled side. |
| `/cardinal:disconnect` | Best-effort revoke the MCP key server-side (via `/api/maestro-keys/<id>/revoke`), strip the plugin-owned `[otel]` / `[mcp_servers.cardinal]` blocks from `~/.codex/config.toml` and the enrichment hooks from `~/.codex/hooks.json`, and delete `~/.codex/cardinal.json`. The ingest-key revoke endpoint isn't shipped yet; the script points at the admin UI. Use `--keep-telemetry` to disconnect only the MCP side. |

## Requirements

- **Codex CLI** with plugin + hooks support (e.g. `>= 0.141.0`).
- **Python 3.9+** on PATH (used by the plugin's `bin/` executables and `hooks/` scripts).
- A **Cardinal account** — sign up at <https://cardinalhq.io>. The MCP side is empty until your org has at least one integration configured on the Cardinal side; the built-in `common-mcp` tools are available either way.

## A note on plan-tier telemetry

The `cardinal.plan_state` / `cardinal.plan_usage` events are emitted with the same shape as the Claude Code runtime, but **runtime-only**: Codex runs on OpenAI/ChatGPT, so `plan_type` / `rate_limit_tier` are reported as `unknown` rather than fabricating OpenAI plan data. The dashboard schema stays uniform across runtimes.

## License

Apache 2.0. See [LICENSE](./LICENSE).
