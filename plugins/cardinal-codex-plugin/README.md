# Cardinal Codex plugin

Connect Codex to Cardinal telemetry and the unified MCP endpoint in one browser-approved consent.

This is a Codex-native port of the command surface from the Claude Code plugin:

| Skill | What it does |
| --- | --- |
| `cardinal-connect` | Runs Cardinal's device-code flow, mints ingest and MCP keys, writes managed Codex MCP config, and installs Cardinal telemetry hooks. |
| `cardinal-status` | Shows the recorded Cardinal workspace and probes the configured ingest and MCP endpoints. |
| `cardinal-disconnect` | Best-effort revokes Cardinal keys, removes managed Codex config/hooks, and deletes local state. |

## Telemetry Scope

Codex does not expose Claude Code's native OpenTelemetry emitter. This plugin emits Cardinal-compatible telemetry from Codex hooks and local Codex transcript JSONL instead. It sends the same Lakerunner event contract used by the Claude plugin where Codex exposes equivalent data:

- `api_request` token usage from Codex `token_count` transcript events.
- `tool_result` plus `cardinal.turn_tool` from Codex function call/output transcript events.
- `cardinal.git_state` from the active Git checkout on `UserPromptSubmit`.
- `cardinal.turn_usage`, `cardinal.plan_state`, and `cardinal.plan_usage` from Codex token and rate-limit snapshots.
- `cardinal.subagent_usage` when Codex hook payloads include subagent token totals.

Claude subscription-specific plan fields that do not exist in Codex are left empty; Codex plan/rate-limit fields are mapped onto the existing plan usage columns where possible.

## Install Locally

This repository is a local Codex plugin directory. Install it through your Codex plugin marketplace or local plugin workflow, then ask Codex to use one of the bundled Cardinal skills.

The plugin contains a disabled `.mcp.json` template for discoverability. The actual live MCP entry is written by `cardinal-connect` into `~/.codex/config.toml`. Telemetry hooks are written into `~/.codex/hooks.json` with absolute paths to this plugin's hook script.

## Connect

Ask Codex:

```text
Use cardinal-connect
```

The skill runs `scripts/cardinal-connect`, prints a Cardinal approval URL, waits for approval, and writes:

| File | What gets written |
| --- | --- |
| `~/.codex/config.toml` | A managed `[mcp_servers.cardinal]` entry with the Cardinal MCP URL and API-key header. |
| `~/.codex/hooks.json` | Managed Cardinal hook entries for `UserPromptSubmit`, `Stop`, and `SubagentStop`. |
| `~/.codex/cardinal.json` | Non-secret state: org/user metadata, endpoint URLs, key ids, key prefixes, and config locations. |
| `~/.codex/cardinal-secrets.json` | Local plaintext ingest/MCP keys needed by hooks and status probes; written mode `0600`. |

Restart Codex after connecting so it reloads MCP and hook config. Review and trust the Cardinal hooks if Codex prompts.

## Scripts

You can also run the scripts directly from this repository:

```bash
python3 scripts/cardinal-connect
python3 scripts/cardinal-status
python3 scripts/cardinal-disconnect
```

Options:

```bash
python3 scripts/cardinal-connect --host https://app.cardinalhq.io
python3 scripts/cardinal-connect --rotate
python3 scripts/cardinal-connect --telemetry-only
python3 scripts/cardinal-connect --dry-run
python3 scripts/cardinal-disconnect --force
```

## Requirements

- Codex with MCP server config support.
- Python 3.11+.
- A Cardinal account.

## License

Apache 2.0. See [LICENSE](./LICENSE).
