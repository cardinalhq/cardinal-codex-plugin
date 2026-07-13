# Cardinal Codex adapter

Connect Codex to Cardinal telemetry and the unified MCP endpoint in one
browser-approved consent. Migrated from
[cardinalhq/cardinal-codex-plugin](https://github.com/cardinalhq/cardinal-codex-plugin)
(P1 of the agent-core extraction — see `../../docs/specs/agent-core.md`);
shared algorithms and the OTLP contract now come from `core/cardinal_core`.

| Skill | What it does |
| --- | --- |
| `cardinal-connect` | Runs Cardinal's device-code flow, mints ingest and MCP keys, writes managed Codex MCP config, and installs Cardinal telemetry hooks. |
| `cardinal-status` | Shows the recorded Cardinal workspace and probes the configured ingest and MCP endpoints. |
| `cardinal-disconnect` | Best-effort revokes Cardinal keys, removes managed Codex config/hooks, and deletes local state. |

## Layout

- `hooks/cardinal-codex-telemetry.py` — the single telemetry hook
  (SessionStart / UserPromptSubmit / Stop / SubagentStop). Codex-specific
  logic lives here: transcript scraping with resume-line semantics
  (MAX_EVENTS_PER_STOP=512), tool-name normalization
  (`exec_command`→Bash, `apply_patch`→Edit with patch-target extraction,
  `mcp__*` splitting), and token-event assembly from `token_count`
  transcript records. Everything shared — OTLP record building/emission,
  initiative resolution, bash classification, pricing, spend-limits
  delivery, session counters, plan stamp — is imported from
  `cardinal_core`.
- `hooks/cardinal_core/` — vendored copy of `core/cardinal_core`,
  created by `python3 build/vendor.py codex` at build time. Gitignored;
  run the vendor step after checkout before executing hooks or tests.
- `scripts/` — `cardinal-connect` (device flow + probes from
  `cardinal_core.deviceflow`; the parse-aware TOML managed-block writer
  and the Codex `hooks.json` writer stay adapter-side), `cardinal-status`,
  `cardinal-disconnect`.
- `tests/goldens/` — normalized OTLP/stdout fixtures captured from the
  pre-migration shipped plugin (v0.5.2) by `tests/capture_goldens.py`.
- `tests/test_parity.py` — asserts the migrated hook is byte-equal to the
  goldens, plus the behavioral suite ported from the source repo.

## Telemetry scope

Codex does not expose Claude Code's native OpenTelemetry emitter; the hook
reads Codex hook payloads and local session JSONL transcripts and emits the
Cardinal/Lakerunner event contract over OTLP/HTTP:

- `api_request` token usage (with plugin-computed `cost_usd`) from
  `token_count` transcript events.
- `tool_result` plus `cardinal.turn_tool` from function call/output
  transcript events (privacy-safe `bash_class` enum, raw qualified MCP
  names on turn_tool).
- `cardinal.git_state` on `UserPromptSubmit`, including initiative
  classification from the branch name and slash-command detection.
- `cardinal.turn_usage` per model call; `cardinal.plan_state` once per
  session and `cardinal.plan_usage` throttled to one snapshot per 10
  minutes, from Codex rate-limit blocks.
- `cardinal.subagent_usage` when Codex hook payloads include subagent
  token totals.

SessionStart injects the initiative branch-naming convention plus the
session's spend-budget standing; the per-prompt spend-limits gate reads the
locally cached verdict (file I/O only) and fails open.

State lives under `~/.codex/cardinal/` (telemetry progress cursors, plan
stamp, limits verdicts); `cardinal-disconnect` removes it.

## Tests

```bash
python3 build/vendor.py codex          # from the repo root, once
cd adapters/codex
python3 -m unittest tests.test_parity -v
```

To re-capture goldens (only if fixtures change — goldens must always come
from the shipped pre-migration plugin, never from this adapter):

```bash
python3 tests/capture_goldens.py --hook /path/to/cardinal-codex-plugin/plugins/cardinal-codex-plugin/hooks/cardinal-codex-telemetry.py
```
