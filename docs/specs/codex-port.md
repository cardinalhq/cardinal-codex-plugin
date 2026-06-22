# Cardinal Codex plugin — porting contract

This repo is the Codex CLI twin of `cardinal-claude-plugin`. It delivers the
same two surfaces — **telemetry** to the Cardinal Outcomes Dashboard and the
unified **`cardinal` MCP** server — using Codex's native plugin/hook/MCP/OTel
systems instead of Claude Code's.

The reference implementation lives at
`../cardinal-claude-plugin/plugins/cardinal/`. Port file-by-file applying the
rules below. Behaviour, event names, and the maestro device-code flow stay
**identical**; only the host-integration layer changes.

---

## 1. What stays the same (copy nearly verbatim)

- The **maestro device-code flow** (`start_device_code`, `poll_device_token`,
  reachability probes, the `_INGEST_PROBE_RETRY_SLEEPS` 401 backoff). The
  server bundle shape (`org`, `user`, `ingest`, `mcp`, `limits`) is unchanged.
- All **OTLP log event schemas** (`cardinal.git_state`, `cardinal.turn_usage`,
  `cardinal.turn_tool`, `cardinal.subagent_usage`, `cardinal.plan_state`,
  `cardinal.plan_usage`) — same `event_name`, same attributes, same
  `/v1/logs` POST. The Outcomes Dashboard consumes both runtimes uniformly.
- The **initiative branch-naming** logic in `git-state.py`
  (`_resolve_initiative`, `_strip_worktree_noise`, `_PREFIX_TO_TYPE`). Verbatim.
- The **spend-limits** verdict cache/gate split (`_limits_common.py`,
  `limits-gate.py`, the async refresh in `git-state.py`).

## 2. Codex hook input is Claude-compatible

Empirically confirmed from the Codex 0.141.0 binary: hook stdin JSON uses the
**same field names** as Claude Code — `session_id`, `cwd`, `hook_event_name`,
`prompt`, `tool_name`, `tool_input`, `tool_response`. So `payload.get(...)`
parsing in every hook is unchanged. Only the *env-var fallbacks* differ:

| Claude env fallback | Codex replacement |
|---|---|
| `CLAUDE_CODE_SESSION_ID`, `CLAUDE_SESSION_ID` | drop (stdin `session_id` is canonical); keep a generic `CODEX_SESSION_ID` fallback only if present |
| `CLAUDE_PROJECT_DIR` | `CODEX_PROJECT_DIR` if set, else `cwd` from stdin / `os.getcwd()` |

## 3. Config source: `~/.codex/cardinal.json`, not settings.json

Codex does **not** inject env into hook subprocesses, and there is no
`settings.json` `env` block. `cardinal-connect` writes a single state file the
hooks read directly. **Replace every `_load_otel_settings()` /
`~/.claude/settings.json` read** with a read of `~/.codex/cardinal.json` via the
shared helper `hooks/_codex_state.py` (see §6).

### `~/.codex/cardinal.json` schema (written by connect, read by hooks)

```json
{
  "schema_version": 1,
  "host": "https://app.cardinalhq.io",
  "mode": "telemetry-and-mcp",
  "runtime": "codex",
  "org_id": "...", "org_slug": "...", "user_email": "...",
  "deployment_environment": "prod",
  "plugin_version": "0.1.0",
  "written_at": "<iso8601>",
  "telemetry": { "enabled": true, "tool_details": true },

  "ingest_endpoint": "https://otelhttp.intake.../",
  "ingest_api_header": "x-cardinalhq-api-key",
  "ingest_api_key": "<FULL KEY — hooks POST with this>",
  "ingest_key_id": "...", "ingest_key_prefix": "abcd1234",

  "resource_attributes": "service.name=codex,agent.runtime=codex,deployment.environment=prod,user.email=...,cardinal.org=...,cardinal.plugin_version=0.1.0",

  "mcp_url": "https://<host>/api/orgs/<uuid>/mcp",
  "mcp_key_id": "...", "mcp_key_prefix": "...", "mcp_created_at": "...",

  "limits": { "status_url": "https://.../status", "enabled": true }
}
```

The file holds the **full ingest key** (unlike the Claude state file, which kept
only a prefix because the key lived in settings.json env). Write it `0600`.

## 4. Runtime attribution

Everywhere the Claude code emits `service.name=claude-code` /
`agent.runtime=claude-code`, emit **`service.name=codex` / `agent.runtime=codex`**.
The OTLP scope name stays `cardinal-claude-plugin`? No — use
`cardinal-codex-plugin`. Keep the scope `version` in sync with `plugin.json`.

## 5. Hook registration: merge into `~/.codex/hooks.json`

The Codex plugin-manifest validator **rejects** a `hooks` field, so hooks are
not declared in `plugin.json`. Instead, `cardinal-connect` **merges** the
plugin's hook entries into the global `~/.codex/hooks.json` (the same mechanism
supacode uses), tagging each managed entry, and `cardinal-disconnect` removes
them. Source of truth is `plugins/cardinal/hooks/hooks.json` (template);
connect rewrites the `command` paths to absolute paths to the installed hook
scripts and merges. Format (PascalCase event keys, Claude-compatible):

```json
{ "hooks": {
  "SessionStart":     [ { "hooks": [ {"type":"command","command":"<abs>/initiative-convention.py","timeout":5},
                                     {"type":"command","command":"<abs>/plan-state.py","timeout":10} ] } ],
  "UserPromptSubmit": [ { "hooks": [ {"type":"command","command":"<abs>/git-state.py","timeout":10},
                                     {"type":"command","command":"<abs>/limits-gate.py","timeout":5} ] } ],
  "PostToolUse":      [ { "matcher":"spawn_agent", "hooks": [ {"type":"command","command":"<abs>/subagent-usage.py","timeout":10} ] } ],
  "Stop":             [ { "hooks": [ {"type":"command","command":"<abs>/turn-usage.py","timeout":10},
                                     {"type":"command","command":"<abs>/plan-usage.py","timeout":10} ] } ]
} }
```

Merge rules: preserve unrelated (e.g. supacode) entries; tag cardinal entries
so disconnect can strip exactly them (append `# cardinal-managed-hook` is not
possible in JSON — instead track managed commands by their absolute path prefix
= the plugin root, and remove any hook whose `command` starts with that root).

> **PostToolUse matcher** — Claude matched `Agent|Task`; Codex's subagent tool
> is `spawn_agent`. The token-accounting fields in Codex's `tool_response`
> differ from Claude's; `subagent-usage.py` must map Codex's spawn-agent result
> (`tokens_used` / `agent_thread_id`) — see §8. Best-effort; mark unmapped
> fields TODO and never crash.

## 6. New shared module: `hooks/_codex_state.py`

Single source of OTLP config for all hooks. Exposes:

- `load_state() -> dict` — read `~/.codex/cardinal.json`; `{}` on any error.
- `otlp_target() -> tuple[str|None, dict[str,str], dict[str,str]]` — returns
  `(endpoint, headers, resource_attrs)` where headers already include
  `{ingest_api_header: ingest_api_key}` and resource_attrs is the parsed
  `resource_attributes` string with `service.name`/`agent.runtime` defaulted to
  `codex`. `endpoint` is `None` when not connected → hook silent-exits.
- `session_id(payload) -> str|None` — `payload.get("session_id")` then
  `os.environ.get("CODEX_SESSION_ID")`.

Hooks call these instead of inlining settings.json parsing. The Claude
`_load_otel_settings()` and `_parse_otlp_headers`/`_parse_resource_attrs`
helpers collapse into this module.

## 7. config.toml writes (cardinal-connect)

`cardinal-connect` writes to `~/.codex/config.toml` (TOML, preserve unrelated
keys — parse with `tomllib` for read, re-serialize minimally or use a targeted
text merge; do NOT clobber the user's existing config):

- **MCP** — write `[mcp_servers.cardinal]` directly with Codex's
  `http_headers` map. Codex 0.141.x accepts and masks `http_headers` in
  `codex mcp get`, while `codex mcp add` only exposes bearer-token env vars
  for HTTP servers. Cardinal's aggregator is probed with
  `X-CardinalHQ-API-Key`, so the emitted table is:

  ```toml
  [mcp_servers.cardinal]
  url = "<mcp_url>"
  http_headers = { "x-cardinalhq-api-key" = "<mcp_key>" }
  ```
- **OTel** — write an `[otel]` block so Codex's native exporter streams its own
  session/turn telemetry to Cardinal:

  ```toml
  [otel]
  environment = "<deployment_env>"
  exporter = { otlp-http = { endpoint = "<ingest_endpoint>", protocol = "binary", headers = { "x-cardinalhq-api-key" = "<key>" } } }
  ```

  **VERIFY** the exact enum serialization against `codex` (write it, run
  `codex doctor` / start a session, confirm "No OTEL exporter enabled" does not
  appear). Adjust to the form Codex actually parses.

Connect owns/removes these on disconnect; keep an `OWNED_*` allowlist analogous
to the Claude `OWNED_ENV_KEYS` so `--telemetry-only` / disconnect are clean.

Drop the entire `~/.claude.json` v0.2→v0.3 legacy cleanup — no Codex history.

## 8. plan-state / plan-usage: runtime-only

Codex runs on OpenAI/ChatGPT, not Anthropic. **Do not** port `_plan_cache.py`'s
Anthropic keychain / `api.anthropic.com` fetch. Instead emit the same
`cardinal.plan_state` / `cardinal.plan_usage` event **shape** with
`agent.runtime=codex` and `plan_type` / `rate_limit_tier` = `"unknown"`, so the
dashboard schema stays uniform across runtimes without fabricating OpenAI plan
data. `git-state.py`'s `_plan_cache.stamp_attrs()` call becomes a no-op/`[]`
(or a tiny `_plan_cache` shim that returns `unknown`). Keep it from crashing.

## 9. Skills (slash commands)

Port `skills/{connect,status,disconnect}/SKILL.md` retargeted to Codex: invoke
`bin/cardinal-*`, reference `~/.codex/config.toml` and `~/.codex/cardinal.json`,
tell the user to start a **new thread** (not "quit Claude Code") to pick up the
plugin. Keep the background-run + `cardinal-pending.json` URL-surfacing pattern.

## 10. Distribution

Repo-local marketplace at `.agents/plugins/marketplace.json` →
`./plugins/cardinal`. Install: `codex plugin marketplace add <path-or-repo>`
then `codex plugin add cardinal@<marketplace>`. README documents this.
