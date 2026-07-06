# Claude-plugin parity — spec & plan of action

Status: **in progress** · Target plugin version: **0.4.0** · Source of truth:
`cardinal-claude-plugin` v0.11.x (`~/workspace/cardinal-claude-plugin`)

## Goal

Bring the Codex plugin to feature equivalence with the Claude Code plugin so a
Codex session produces the same Cardinal telemetry contract, the same
initiative classification, and the same spend-limits behaviour as a Claude
Code session — modulo fields that genuinely do not exist on the Codex side.

## Verified facts (2026-07-06)

These were checked before writing this plan; they gate feasibility.

1. **Attribute naming is compatible.** Lakerunner's agent-sessions processor
   reads underscore keys (`cardinal_initiative_name`,
   `cardinal_head_sha`, … — `lakerunner/internal/agentsessions/processor.go`).
   The Claude plugin emits dotted keys (`cardinal.initiative.name`) which the
   ingest pipeline normalizes to underscores; this plugin emits the
   underscore form directly. Both land on the same columns. No change needed.
2. **Codex CLI supports the hook surface we need.** codex-cli 0.142.5
   recognizes `SessionStart`, `SubagentStart`/`SubagentStop`, `PreToolUse`,
   `PostToolUse`, `PreCompact`, `Stop`, `UserPromptSubmit`, and the
   Claude-compatible hook output protocol (`hookSpecificOutput`,
   `additionalContext`, `systemMessage`, `decision`, `permissionDecision`) —
   verified against the binary. So both the SessionStart context injection
   and a blocking spend-limits gate are portable.
3. **The connect script already persists the `limits` block** from the
   device-flow bundle into `~/.codex/cardinal.json` (parity with the Claude
   connect script). Only the hook-side consumption is missing.

## Gap inventory (Claude v0.11.x → Codex v0.3.0)

### Missing features

| # | Feature | Claude implementation | Codex status |
|---|---------|----------------------|--------------|
| A | Spend-limits gate | `limits-gate.py` (sync UserPromptSubmit: block / warn / notify with band hysteresis + override file), async verdict refresh in `git-state.py`, budget standing at SessionStart. Shared logic in `_limits_common.py`. | Absent |
| B | Initiative-convention prompt | `initiative-convention.py` SessionStart `additionalContext` (branch-naming convention + budget standing) | Absent — SessionStart not even registered |

### Divergences inside shared events

| # | Divergence | Claude behaviour | Codex v0.3.0 behaviour | Resolution |
|---|-----------|------------------|------------------------|------------|
| 1 | Long-turn event loss | Emits `truncated=true`, never loses records it saw | Breaks at `MAX_EVENTS_PER_STOP=512` but records `last_line: len(lines)` → unprocessed tail skipped forever | Record the actual resume offset; the next Stop continues from it (strictly better than a truncated flag — no flag needed) |
| 2 | Worktree branch noise | `_strip_worktree_noise` (`worktree-fix-1018-foo` → `foo`) | Missing → pollutes initiative rollup | Port verbatim |
| 3 | Command detection | Raw `/cmd` **and** `<command-name>` tag forms | Raw form only | Port tag regex |
| 4 | `cardinal_remote_url` | Emitted on git_state | Missing | Add |
| 5 | `ts` attribute | On turn_usage / turn_tool / plan_state / plan_usage | Missing | Add (epoch ns int) |
| 6 | `turn_seq` semantics | Model-call index **within the current user turn** (resets each turn) | Session-cumulative counter | Reset on `user_message` boundary, same place `tool_seq` resets |
| 7 | `target` extraction | Allowlisted file-path inputs on Read/Edit/Write/NotebookEdit | `TARGET_KEYS` table defined but dead; only `apply_patch` extracts a target | Wire `TARGET_KEYS` as fallback in tool-call normalization |
| 8 | plan_state / plan_usage cadence | plan_state once per SessionStart; plan_usage at SessionStart + 10-min throttle on Stop | Both emitted on **every** `token_count` transcript event | Emit plan_state once per session (and again on value change); throttle plan_usage to 10 min per session, first snapshot unthrottled |
| 9 | Plan stamping | `plan_type` + `rate_limit_tier` stamped onto git_state, turn_usage, turn_tool, subagent_usage via `_plan_cache.stamp_attrs()` | Not stamped | Cache last-seen `rate_limits` plan fields in `~/.codex/cardinal/telemetry/plan.json`; stamp the same two keys onto the same events |

### Accepted asymmetries (non-goals)

- **OAuth plan cache** (`_plan_cache.py` profile fetch: `organization_type`,
  `billing_type`, `billing_mode`, `has_extra_usage_enabled`,
  `seven_day_sonnet`/`seven_day_opus` windows) — Anthropic-subscription
  concepts with no Codex equivalent. Codex plan facts come from the
  `rate_limits` blocks in transcript `token_count` events. Documented in
  README; unchanged.
- **`cost_usd`** — Codex-only computation (OpenAI pricing table); Claude Code
  emits cost natively. Keep as-is.
- **Native OTel events** (`user_prompt`, `api_error`, …) that Claude Code's
  built-in exporter emits and this plugin does not synthesize — out of scope,
  as already documented in the README's Telemetry Scope section.
- **Subagent exact token summing** — the Claude plugin sums the subagent's
  own transcript. Codex's SubagentStop payload shape is not yet observed in
  the wild; `handle_subagent_stop` stays best-effort (emits only when the
  payload carries token totals). Follow-up: capture a real SubagentStop
  payload and revisit. Tracked as **P5 (deferred)**.

## Plan of action

- **P1 — telemetry hook parity fixes** (divergences 1–9) in
  `plugins/cardinal-codex-plugin/hooks/cardinal-codex-telemetry.py`. Pure-local changes, no new hook
  registrations.
- **P2 — SessionStart**: new `--event SessionStart` handler emitting
  `additionalContext` = initiative-convention prompt (Codex wording) +
  budget standing when limits are configured; register `SessionStart` in
  `cardinal-connect`'s `write_hooks_config()`. Existing installs pick it up
  on the next `cardinal-connect` run (README note).
- **P3 — spend-limits gate**: port `_limits_common.py` with Codex paths
  (state `~/.codex/cardinal.json`, key from `~/.codex/cardinal-secrets.json`,
  verdict/ack/override files under `~/.codex/cardinal/limits/`). The
  UserPromptSubmit handler becomes: sync gate (file read only → block /
  additionalContext + systemMessage with band hysteresis) → git_state OTLP
  POST → TTL-driven verdict refresh. SessionStart does one forced fetch
  (1.5 s, fail open) for standing. Fail open everywhere.
- **P4 — tests + release**: extend `tests/test_cardinal_plugin.py`
  (worktree stripping cases mirroring the Claude repo's fixtures, cursor
  resume, plan throttle, gate block/warn/hysteresis, SessionStart context);
  bump `PLUGIN_VERSION` → 0.4.0; README updates.
- **P5 (deferred) — subagent fidelity** pending a captured SubagentStop
  payload.

### Keeping the repos in lockstep

The shared pure logic (initiative resolution incl. worktree stripping,
command detection, repo canonicalization) is duplicated by design (two repos,
no shared package). The guard is test parity: the Codex test suite pins the
same branch→initiative and command-detection fixtures as
`cardinal-claude-plugin/tests/test_cardinal_plugin.py`. When one repo changes
the contract, its fixture diff is the prompt to mirror the other.

## Live verification checklist (post-implementation)

Verified 2026-07-06 against a real Codex session
(`019f3819-d79b-7ae1-9e2c-67f67831b2c0`, codex-cli 0.142.5) with events
confirmed in the prod `agent_sessions` / `agent_session_events` tables:

- [x] SessionStart entry in `~/.codex/hooks.json` (connect covered by tests;
      live install registered in the managed format).
- [x] SessionStart handler emits the convention prompt + real budget standing
      (`session: $0.00 of $100.00 — set by you`) and warm-writes the verdict
      file via a live maestro fetch. **Caveat:** `codex exec` does not fire
      SessionStart at all (interactive sessions expected to; unconfirmed) —
      exec-mode sessions get the convention only via downstream branch
      classification, which still works.
- [x] Worktree stripping in prod: branch
      `feat/worktree-fix-123-codex-parity-verify` → `initiative_name
      codex-parity-verify`, type `feature`; `plan_type=team` /
      `rate_limit_tier=codex` stamped on the session row.
- [x] Throttling in prod: 1 `plan_state` + 1 `plan_usage` across a
      multi-turn session (2 `api_request`/`turn_usage`, 3 `git_state`).
- [x] Block verdict → `hook: UserPromptSubmit Blocked`, turn never reached
      the model; override file downgraded the block and the turn completed.
