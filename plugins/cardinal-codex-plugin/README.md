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
| `cardinal-decision` | Turns opt-in decision capture on/off and shows its status (see Decision capture). |
| `cardinal-optimize-toolkit` | Mines the caller's last 30 days of session telemetry via the `cardinal` MCP server's `outcomes__*` tools for capability-fit recommendations, and on explicit confirmation writes the accepted artifact to `.codex/agents/<name>.toml`. Explicit-invocation only (W4.T4.2; see `docs/specs/toolkit-hive-mind.md` §4). |

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
- `tool_result` plus `cardinal.turn_tool` from legacy function call/output
  and current custom tool call/output transcript events (privacy-safe
  `bash_class` enum, raw qualified MCP names on turn_tool). Single-operation
  `functions.exec` programs are unwrapped to Bash/Edit/MCP; compound programs
  retain only their nested tool names, never the raw JavaScript source.
- `cardinal.git_state` on `UserPromptSubmit`, including initiative
  classification from the branch name and slash-command detection. A
  research-classified context shell is also emitted outside Git and for
  repositories without a first commit. When the branch has a PR the record
  also carries `cardinal_pr_number` and `cardinal_pr_url`; otherwise both
  keys are absent. Codex runs `UserPromptSubmit` synchronously and
  discards its stdout on timeout (5s in `hooks.json`), which would also
  drop the spend-gate warning and decision context. So this path never runs
  `gh`: it reads the PR cache that `cardinal_core.decisions.resolve_pr`
  maintains (`~/.codex/cardinal/decisions/cache/prs.json`, 10 min hit /
  2 min miss). On a miss or stale entry it spawns a detached refresh child
  (own session, devnull stdio) and uses the stale value, if any. The first
  prompt on a new branch therefore lacks the PR keys; later prompts carry
  them. Protected branches never run `gh`.
- `cardinal.decision`, emitted by the `PostToolUse` handler (see Decision
  capture below).
- `cardinal.turn_usage` per model call; `cardinal.plan_state` once per
  session and `cardinal.plan_usage` throttled to one snapshot per 10
  minutes, from Codex rate-limit blocks.
- `cardinal.subagent_usage` when Codex hook payloads include subagent
  token totals.

SessionStart injects the initiative branch-naming convention plus the
session's spend-budget standing; the per-prompt spend-limits gate reads the
locally cached verdict (file I/O only) and fails open.

State lives under `~/.codex/cardinal/` (telemetry progress cursors, plan
stamp, limits verdicts, decision setting + ledgers); `cardinal-disconnect`
removes it.

## Decision capture

Same contract as the Claude adapter (`docs/specs/decision-telemetry.md`),
off by default. `CARDINAL_DECISIONS=1/0` in the environment overrides
on/off (Codex has no settings env block).

### Why the hook emits, not the CLI

Codex runs the agent's shell commands under the `workspace-write` sandbox
by default. Network is off, and the writable roots are cwd, `/tmp` and
`$TMPDIR`, not `~/.codex`: see `SandboxPolicy::WorkspaceWrite` and
`get_writable_roots_with_cwd` in `codex-rs/protocol/src/protocol.rs`. Hooks
are spawned by Codex outside the sandbox (plain `Command::new`,
`codex-rs/hooks/src/engine/command_runner.rs`). So the flow is:

1. **Prompt.** `UserPromptSubmit` injects the instructions and session
   ledger as `hookSpecificOutput.additionalContext`, merged into the spend
   gate's single JSON object; a block verdict is emitted unchanged. The
   command given is `python3 <plugin root>/scripts/cardinal-decision record
   --session <id> ...`, from the install the launcher actually executed.
   Injection only happens when the managed `PostToolUse` hook is
   registered; otherwise the flow could never fire.
2. **Agent (sandboxed).** `record` validates, prints exactly one
   `cardinal-decision-record:v1 <json>` line (keys `v, session, cwd, id,
   choice, question, why, alt, by, anchor, follows, refines, supersedes`;
   the same wire format as the Cursor adapter) and exits 0. It never reads
   or writes `~/.codex`, never runs `gh`, and makes no network call.
3. **PostToolUse (unsandboxed), the only emitter.** It acts on
   `tool_name` `Bash` calls whose `tool_input.command` itself runs
   `cardinal-decision record`.
   - **Argv first.** Invocations are found with shell-aware tokenization:
     backslash continuations are joined, and the call may follow `;`, `&&`
     or `|`, env assignments, a path, or `python3 [options]` including
     `-X utf8`. Each decision is built from that invocation's real argv,
     using the CLI's own argparse spec.
   - **Marker as confirmation only.** The marker in `tool_response` merely
     confirms the CLI ran and validated. It is accepted only when its JSON
     equals the argv-derived decision, and each marker confirms at most one
     invocation. A differing spoofed marker (`echo`, `cat`, `grep`, before
     or after the real call) is ignored; a matching one is harmless.
   - **Synchronous (local only):** argv parse, on/off gate, validation,
     final id against the ledger, git facts, PR from cache (detached
     refresh on a miss), ledger write, reply.
   - **Background:** D18 clusters and the OTLP send run in one detached
     child (own session, /dev/null stdio) fed by a 0600 spool file under
     `~/.codex/cardinal/spool/`, the same pattern as the Gemini adapter. A
     stalled ingest or DNS never holds the tool result or the hook
     timeout.
   - **Always replies once invoked.** The result goes to the agent as
     PostToolUse `hookSpecificOutput.additionalContext`. If nothing could be
     recorded, the reply says NOT recorded and why:
     - capture off, or no session id;
     - unparsable or invalid arguments;
     - no matching marker (the command failed, or its output was truncated
       or altered);
     - an unsupported form such as `bash -lc "..."` whose output carries a
       marker;
     - a ledger write error.
   - **Session:** it uses the payload's `session_id` only, never the
     marker's.

   Every other shell call returns immediately and prints nothing.

`record --emit` is for a human in their own terminal: it records and emits
directly and prints no marker, so PostToolUse never emits it again, even
under `danger-full-access`. `on`/`off` write `~/.codex/cardinal`, which the
sandbox blocks. They report the exact terminal command on failure, and the
`cardinal-decision` skill runs only `status` and tells the user to toggle
from their own terminal.

**Upgrading, and restart Codex:** installs connected before this change
have no `PostToolUse` entry. Run `cardinal-connect --repair-hooks`, then
**restart Codex**. Codex builds its hook set when a session starts
(`Hooks::new` in `codex-rs/core/src/session/session.rs`). It rebuilds it
(`Session::refresh_hooks`, `core/src/session/mod.rs`) only at the end of
its own config-reload path, which runs on a config change made through
Codex, not on an external `hooks.json` edit. Without a restart, the prompt
hook (which reads `hooks.json`) could show recording instructions while no
PostToolUse emitter is active. `cardinal-decision status` reports whether
the hook is registered.

### Host-surface evidence (openai/codex `a4354e2d`, https://learn.chatgpt.com/docs/hooks)

- **UserPromptSubmit context.** `codex-rs/hooks/src/events/user_prompt_submit.rs`
  `parse_completed` appends `additionalContext` to the model context. The
  output schema (`hooks/schema/generated/user-prompt-submit.command.output.schema.json`)
  is a single object. Stdout is discarded on timeout
  (`command_runner.rs` `run_command`, the timeout arm).
- **PostToolUse payload.** `codex-rs/core/src/tools/handlers/unified_exec.rs`
  `post_unified_exec_tool_use_payload` builds it with
  `tool_name: HookToolName::bash()` ("Bash"). `unified_exec_tests.rs`
  asserts `tool_input: {"command": "echo three"}` and
  `tool_response: "three"`. `PostToolUseRequest`
  (`hooks/src/events/post_tool_use.rs`) adds `session_id`, `turn_id`,
  `cwd`, `tool_use_id`, `permission_mode`.
- **When PostToolUse runs.** `core/src/tools/registry.rs` runs it after
  successful dispatch, and the docs say Bash also fires on non-zero exit.
  Code-mode `exec` callbacks dispatch through
  `ToolRouter::dispatch_tool_call_with_code_mode_result` into the same
  registry path (`core/src/tools/router.rs`).
- **Matchers.** An alphanumeric matcher is an exact match
  (`hooks/src/events/common.rs` `matches_matcher`).
- **Background children.** Detached helpers survive a completed hook; the
  process group is killed only on timeout or error (`command_runner.rs`
  `ProcessTreeGuard`).

Unverified or limits:
- Whether Codex exports a session id to shell commands. The CLI falls back
  to `CODEX_SESSION_ID` / `OPENAI_CODEX_SESSION_ID`, but the prompt always
  passes `--session`.
- Codex may truncate very large command output. A record chained with a
  noisy command could lose its marker.
- Worst-case `UserPromptSubmit` time is now git + git_state POST (2s) +
  limits refresh (2s), under the 5s timeout but not by much.

## Evidence capture (storyboards)

`cardinal-connect` registers a second `PostToolUse` group (matcher `.*`,
`--event ToolEvidence`) next to the decision emitter. It records every tool
call Codex reports to `PostToolUse` in the local evidence spool shared with
the other Cardinal adapters, `~/.cardinal/evidence/<session>/ev_<id>.json`,
through the shared generic pipeline (`cardinal_core.evidence_capture`): a
call that touches something sensitive (a `.env` or key file, `printenv`,
`gh auth token`, a credentialed URL or header) is kept only as a *withheld*
stub; everything else is scrubbed, capped at 256 KiB and removed after 14
days. The id comes back as `hookSpecificOutput.additionalContext`
(`[evidence:ev_…]`). Nothing leaves the machine: `scripts/cardinal-evidence
promote --storyboard sb_… ev_…` uploads only what a storyboard cites, with
the connection's MCP key, and needs the network (approve it outside the
sandbox). Run `cardinal-connect --repair-hooks` and restart Codex to pick up
the new group.

Verified: `PostToolUse` fires for `Bash` with `tool_response` = the output
string (see Host-surface evidence); a 2026-10-03 capture (codex-cli
0.142.5, see Storyboard associations) also saw it fire for `apply_patch` and
MCP tools. Not verified: that `.*` matches every tool name (`matches_matcher` treats a non-alphanumeric matcher as a regex;
the pre-removal Semantic DAG hooks used `.*`). Tool calls that do not reach
`PostToolUse` are not captured; cite them with `storyboard__record_evidence`
(the reported tier). `CARDINAL_CODEX_DEBUG_PAYLOADS=1` dumps real payloads.

## Automatic Investigation Storyboards

Every connected Codex session now creates or reuses one private Investigation
and Storyboard at SessionStart. The hook tells Codex to show its link in the
first progress update, set the investigation question, and checkpoint material
findings with captured or witnessed evidence as the work proceeds. When the
server advertises projection, its projector updates that draft automatically;
the worker does not create another Storyboard or author scenes. Publication and
sharing remain explicit. No transcript or owner-input payload is supplied.

`scripts/cardinal-storyboard investigation link|question|checkpoint|events|ack
--session SID` uses the shared Investigation transport, evidence promotion,
validation and idempotency. `checkpoint --help` lists the event types and fields;
checkpoint input is a JSON array on stdin. The session id is passed explicitly.
A session joined to another author's Investigation cannot checkpoint or frame it.
A shell holding `CARDINAL_INVESTIGATION_TOKEN` cannot use this author CLI.

A detached, per-session poller retries transient bootstrap failures, refreshes
capabilities and fetches advisory events. PostToolUse delivers its local inbox
without network waits. Polling pauses while idle and exits with its Codex
ancestor. Advisories retain their untrusted authority. Owner-input capture is
not wired in the Codex adapter. `CARDINAL_STORYBOARD_SESSION_START=0` disables
automatic bootstrap/guidance; `CARDINAL_INVESTIGATION_POLLER=0` disables polling.

Upgrade the plugin, run `scripts/cardinal-connect --repair-hooks`, and review and
trust the updated Cardinal hooks in Codex. Start a new session to pick them up.
SessionStart has an eight-second budget for existing context plus the bounded
bootstrap request. Older servers without bootstrap retain discovery behavior;
servers without projection are described honestly as not automatically updating.

## Storyboard associations

Where a storyboard is written from (repo, branch, PR, HEAD, the files this
session edited) is recorded on each act as its `context`, and storyboards
that may relate to the checkout are shown at session start. Shared logic:
`cardinal_core.storyboard_agent`.

- **SessionStart** (the existing hook): when Cardinal MCP is connected, the
  additional context names this session's id (for `storyboard__create`,
  `storyboard__add_act` and `storyboard__find` `session_id`) and
  `scripts/cardinal-storyboard context --bare --session-id <id>`, which
  prints the `context` object itself to pass (without `--bare` it prints
  `{"context": {…}}`; only the inner object is `context`), plus the storyboards that may relate to this
  branch, PR or commit (at most 3, 2 KB, framed as data; gh cache only, a
  2 s network deadline, `X-Cardinal-Client: codex/<plugin version>`). Off:
  `CARDINAL_STORYBOARD_DISCOVERY=0` (the storyboards) or
  `CARDINAL_STORYBOARD_SESSION_START=0` (all of it).
- **Edited files**: the `ToolEvidence` `PostToolUse` run records the files a
  successful `apply_patch` names (`*** Add File:`, `Update File:`,
  `Delete File:`, `Move to:`; relative to the call's cwd) for
  `context.paths`. Codex has no other edit tool; files changed by shell
  commands are not recorded.
- **Automatic context** (`PreToolUse`, matcher
  `^mcp__cardinal__storyboard__(create|add_act|publish|find)$`,
  `--event StoryboardContext`): fills an absent `session_id` and an absent
  or `{}` `context`; never changes what the model set, never writes
  `about`; `publish` only once the server has advertised associations.
  Codex applies `updatedInput` only together with
  `permissionDecision: "allow"`, and whether that also skips an approval
  prompt is not verified, so the hook stamps only in `bypassPermissions`
  mode, or in every mode with `CARDINAL_STORYBOARD_CONTEXT=always`.
  **`always` may skip Codex's approval prompt for storyboard writes,
  including `storyboard__publish`**: the hook's `"allow"` is sent on every
  stamped `create`, `add_act`, `publish` and `find` call, so set it only if
  you accept that. Elsewhere the agent passes `cardinal-storyboard context
  --bare` itself. The SessionStart text says the plugin fills context only
  when stamping applies in the session's permission mode, is not turned
  off, and `~/.codex/hooks.json` has the `StoryboardContext` handler (an
  upgraded plugin runs before `--repair-hooks` adds it); otherwise it names
  the CLI. The wording follows the permission mode at session start; a
  later `/approvals` change is not reflected (the CLI is named either way).
  `CARDINAL_STORYBOARD_CONTEXT=0` turns it off.
- Without `--session-id` the storyboard CLI tries `CODEX_SESSION_ID` /
  `OPENAI_CODEX_SESSION_ID`; no capture shows Codex setting either, so the
  SessionStart text always passes `--session-id`.

Evidence (codex-cli 0.142.5, captured 2026-10-03 with a scratch
`CODEX_HOME`): `apply_patch` arrives as `tool_input.command` (the patch) with
a `tool_response` string `Exit code: 0 … Success. Updated the following
files: A hello.txt`; MCP tools are `mcp__<server>__<tool>`; a `PreToolUse`
`updatedInput` with `permissionDecision: "allow"` reached the MCP server
rewritten. Fixtures: `tests/test_codex_storyboard.py`. Run
`cardinal-connect --repair-hooks` and restart Codex to register the
`PreToolUse` group.

## Tests

```bash
python3 build/vendor.py codex          # from the repo root, once
cd adapters/codex
python3 -m unittest tests.test_parity -v
```

`tests/test_decisions.py` covers the sandboxed CLI (no writes, no `gh`,
no POST), hook-side emission from a Codex-shaped PostToolUse payload, no
double emission, prompt injection and cache-only PR linkage with
background refresh. `tests/fixtures.py` puts a logging stub `gh` first on
PATH for every hook run, so no test reaches GitHub, and seeds the PR cache
for the golden scenario so it doesn't depend on subprocess timing.

Exception to the rule below: `user_prompt_submit.json` was re-captured
from this adapter for the intentional `cardinal_pr_number` /
`cardinal_pr_url` addition (the pre-migration plugin never emitted them).
The diff was only those two attributes; every other golden was unchanged.

To re-capture goldens (only if fixtures change — goldens must always come
from the shipped pre-migration plugin, never from this adapter):

```bash
python3 tests/capture_goldens.py --hook /path/to/cardinal-codex-plugin/plugins/cardinal-codex-plugin/hooks/cardinal-codex-telemetry.py
```
