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
| `cardinal-canvas` | Draws a storyboard scene's Canvas and critiques the PNGs the preview hook renders locally (shared `common/canvas` core). |
| `cardinal-optimize-toolkit` | Mines the caller's last 30 days of session telemetry via the `cardinal` MCP server's `outcomes__*` tools for capability-fit recommendations, and on explicit confirmation writes the accepted artifact to `.codex/agents/<name>.toml`. Explicit-invocation only (W4.T4.2; see `docs/specs/toolkit-hive-mind.md` §4). |

## Layout

- `hooks/cardinal-codex-telemetry.py` — the single hook for every event
  (SessionStart / UserPromptSubmit / Stop / SubagentStart / SubagentStop /
  PreToolUse / PostToolUse; `--event` names the handler). Codex-specific
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
  `cardinal-disconnect`, `cardinal-checks` and `cardinal-behavior` (see
  Checks preview), `cardinal-wake` (see Owner-woken sessions).
- `checks/skill/` — the Checks preview's opt-in skill template (installed
  into `skills/checks/` by `cardinal-checks on`, never shipped there).
- `agents/brainstorm.toml` — the `cardinal-brainstorm` custom agent;
  `cardinal-connect` installs it in `~/.codex/agents/` (see Storyboard
  authoring).
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

Automatic session storyboards carry the checkout's repository, branch, HEAD,
relative directory, and client labels. SessionStart uses the hook payload's
working directory and cached PR information; it never sends an absolute path
or runs a network PR lookup. The first repository snapshot is retained for
bootstrap retries and background refresh, so a later directory change cannot
retag the storyboard. Resuming an older session can fill missing repo tags on
its own draft after Maestro supports session context. Existing tags and
published content are preserved. Repositories observed later are added as
separate provenance labels, so a session can span several repositories. `CARDINAL_STORYBOARD_CONTEXT=0` disables this
collection and clears the saved snapshot. Older Maestro servers still create
the session normally, without repo tags, until upgraded.

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

Every connected Codex session creates or reuses one private Investigation and
Storyboard at SessionStart. The session guidance (`cardinal_core.investigation_text`,
the text every runtime gets) gives the labeled links: the Storyboard, a private
draft until an act is published, and the Investigation timeline, which shows the
live record before any publish and is the link to share. It tells Codex to show
them in its first progress update, set the investigation question, and checkpoint
material findings with captured or witnessed evidence as the work proceeds. When
the server advertises projection, its projector updates that draft
automatically; the worker does not create another Storyboard. Publication and
sharing remain explicit. No transcript is supplied.

The guidance is sent at SessionStart for every source (`startup`, `resume`,
`clear`, `fork`, and `compact`: Codex fires SessionStart again after a
compaction, before the next prompt). A prompt sends it again only when it
changed (a bootstrap the poller completed after SessionStart, refreshed
capabilities, owner input turned on or off); otherwise prompts carry no
guidance.

`scripts/cardinal-storyboard investigation link|question|checkpoint|events|ack
--session SID` uses the shared Investigation transport, evidence promotion,
validation and idempotency. `checkpoint --help` lists the event types and fields;
checkpoint input is a JSON array on stdin. The session id is passed explicitly.
A session joined to another author's Investigation cannot checkpoint or frame it.
A shell holding `CARDINAL_INVESTIGATION_TOKEN` cannot use this author CLI.
These commands reach Cardinal over the network: in Codex's `workspace-write`
sandbox (network off) each one needs the user's approval. The MCP tools
`storyboard__read_investigation_events` and `storyboard__post_investigation_event`
read and post without that approval when no local evidence needs promoting.

A detached, per-session poller retries transient bootstrap failures, refreshes
capabilities and fetches advisory events. Polling pauses while idle and exits
with its Codex ancestor. Advisories keep their untrusted authority and are
delivered as model context, never as owner input:

- **At a tool boundary** (PostToolUse `ToolEvidence`): what was staged, then the
  local inbox, then one owed challenge or directive re-surfaced; no network
  wait. A subagent's tool call (its payload names `agent_id`; `session_id` is the
  parent's) gets nothing, and the items wait for the main thread.
- **At the turn's end** (Stop, `investigation_agent.turn_end`): the inbox; else,
  when the poller has not read the events lately, one synchronous check; else
  an open directive or the owner-decision reminders. What is found blocks the
  stop (Codex wraps the reason as a `<hook_prompt>` item and UserPromptSubmit
  does not fire for it, so advisory text never becomes owner input). Then the
  owner-input outbox is flushed. All of it ends 5 s after the hook starts
  (Stop's timeout is 10 s).
- **At the next prompt**: what was staged (for example a storyboard re-look's
  block) goes into `additionalContext`.

`CARDINAL_STORYBOARD_SESSION_START=0` disables automatic bootstrap/guidance;
`CARDINAL_INVESTIGATION_POLLER=0` disables polling.

### Owner input

When the server enables it for the session (`capabilities.owner_input`, the
session authors its Investigation), the UserPromptSubmit hook records what
you typed into the Investigation as owner input (`cardinal_core.owner_input.capture`,
the gate every runtime shares): credentials scrubbed, at most 32 KiB, readable
only by you and grantees you authorize.

- **Disclosure first.** The first prompt that would be recorded is not: Codex
  shows you a Cardinal `systemMessage` saying prompts are recorded to the
  Investigation and how to turn it off, and recording starts with the next
  prompt. Turning it off and on again shows the disclosure again first.
- **Kill switch.** `CARDINAL_OWNER_INPUT=0` (or `false` / `off` / `no`) in the
  environment, or `{"codex": "off"}` in `~/.cardinal/owner-input.json` (for the
  Codex app, which does not see your shell's environment). Set, nothing is
  recorded and the queued outbox is deleted at the next turn end.
- **What is recorded.** Exactly the prompt text Codex's UserPromptSubmit hook
  receives, which a live capture (codex-cli 0.160.1 and 0.162.1) showed is the
  composer text: an `@file` mention is the path text, not the file's contents;
  `$skill` mentions and a `/review` with custom instructions are sent as typed.
  Never recorded: a subagent's prompt (payload `agent_id`), the prompts Codex
  generates for `/review`'s presets and `/init`, slash commands that submit no
  prompt (`/compact`). The same prompt typed twice is recorded twice.
- **Bounded.** Recording finishes within 3.5 s of the hook's start (the hook's
  timeout is 5 s and Codex discards a timed-out hook's output, the spend-limits
  verdict with it); a prompt that cannot be posted in time is queued (0600) and
  posted at the turn end or the next prompt. A spend-limits block wins: a
  blocked prompt is not recorded.

### Storyboard authoring

- **Preview render** (PostToolUse `StoryboardPreview`, matcher
  `^mcp__cardinal__storyboard__preview$`, timeout 150 s): renders every scene of
  the preview result in your local Chrome/Chromium
  (`skills/storyboard/scripts/render_preview.py --runtime codex`) and hands the
  agent the PNG paths under `~/.codex/cardinal/storyboards/<id>/r<rev>/`, then the
  link-preview cover and mock. No Chromium: said once per session.
- **Hero upload and edit lookup** (PostToolUse `StoryboardAuthoring`, matcher
  `^(mcp__cardinal__storyboard__publish|apply_patch)$`, timeout 10 s): after a
  publish, the local render of the act's cover scene is uploaded for link
  previews (`CARDINAL_STORYBOARD_HERO=0` turns it off); after an `apply_patch`,
  the storyboards about the edited files (or the PR that last changed them) are
  added as data, one lookup per new directory, at most six per session, within
  4 s. Codex's PreToolUse output schema has `additionalContext`, but whether
  Codex shows it to the model is not verified, so the lookup runs after the
  edit.
- **Evidence token**: Cardinal's `storyboard__create` / `add_act` / `preview`
  results' evidence token is kept per session (`~/.cardinal/evidence/<session>/`),
  so `cardinal-evidence promote` uploads cited evidence without the org key.
- **Storyboard re-look**: a prompt whose branch or HEAD moved since the
  session's last look starts a detached look (never on the prompt's clock);
  its block is staged for the next tool or prompt boundary. A **SubagentStart**
  hook gives a subagent the block the session's last look stored (no git, no
  network).
- **canvas skill** (`skills/canvas/`, the shared `common/canvas` core):
  drawing a scene and critiquing the rendered PNGs.
- **`cardinal-brainstorm` agent**: `cardinal-connect` installs
  `agents/brainstorm.toml` as `~/.codex/agents/cardinal-brainstorm.toml` (Codex
  discovers custom agents there; a plugin cannot ship one) and
  `cardinal-disconnect` removes it. A file at that path without the plugin's
  first-line marker is yours and is left alone.

SessionStart also tells the user, once per machine, how to connect when Cardinal
is not connected, and warns once per session when an MCP key is set without an
MCP URL.

### Upgrading: re-trust the hooks

Upgrade the plugin, run `scripts/cardinal-connect --repair-hooks`, and start
Codex. The repair needs no new key: it refreshes the launchers and hooks, and
adds `[mcp_servers.cardinal-behavior]` to the managed block of a connection
made before the Checks preview (`cardinal-checks on` / `status` print this
command while it is missing). Codex lists the new and modified Cardinal hooks for review, and runs none of
them until you trust them in the TUI. Every hook entry whose command or matcher
changed must be trusted again (`codex exec` runs only trusted hooks unless
`--dangerously-bypass-hook-trust` is given). Start a new session to pick them up.
SessionStart has an eight-second budget for existing context plus the bounded
bootstrap request. Older servers without bootstrap retain discovery behavior;
servers without projection are described honestly as not automatically updating.

### Platform limits

What Codex can and can't do next to the Claude, Cursor and Gemini CLI plugins,
feature by feature, with a citation for every limit: [`docs/parity.md`](../../docs/parity.md).

- `/prompts:<name>` custom prompts do not exist in codex-cli 0.160.1 / 0.162.1
  (the TUI answers "Unrecognized command"; `codex exec` passes the text
  through literally), so there is no expanded custom-prompt shape to handle.
- `/review` turns fire UserPromptSubmit with Codex's generated prompt but no
  Stop; their advisories wait for the next tool or turn boundary.
- An advisory that arrives after a turn's last tool call is delivered at that
  turn's Stop (a block), or at the next tool boundary.

## Checks preview (opt-in)

Checks are reusable, evidence-backed decisions over telemetry (a contract the
user approves, approved teaching examples, per-trace results with evidence),
built on the `cardinal-behavior` MCP server. The preview is **off by
default**; turn it on or off in your own terminal:

```bash
python3 scripts/cardinal-checks on       # install the checks skill; start a new Codex session
python3 scripts/cardinal-checks off      # remove it
python3 scripts/cardinal-checks status   # the setting, the installed skill, accepted checks
```

- **`cardinal-behavior` MCP server.** `cardinal-connect` registers it
  as `[mcp_servers.cardinal-behavior]` inside the managed block of `~/.codex/config.toml` (through the stable launcher `~/.codex/cardinal/cardinal-behavior.py`, like the hooks, so a plugin upgrade does not dangle it; an unmanaged table of that name is left alone), launched as `scripts/cardinal-behavior --runtime codex`;
  `cardinal-disconnect` removes it (only the managed one: a table of that
  name outside the markers is yours and is kept). A connection made before
  the Checks preview has none: `python3 scripts/cardinal-connect --repair-hooks`
  adds it without a new key, and `cardinal-checks on` / `status` print that
  command while it is missing. It is a local stdio server that Codex
  starts outside its sandbox, so it can write the Checks ledger and read
  range runs that a sandboxed agent cannot. It reads this runtime's
  connection (`~/.codex/cardinal.json` `mcp_url` + `cardinal-secrets.json`
  `mcp_api_key`); `CARDINAL_MCP_URL` with `CARDINAL_MCP_API_KEY` in its
  environment overrides them (a URL without its key does not). No key is
  written into the MCP registration. Its draft ledger and `enabled.json` are
  private (0600 files in 0700 directories).
- **The skill** (`cardinal-checks`, from `checks/skill/`: this plugin's
  SKILL.md over the shared `common/checks/CORE.md`) is installed into
  this plugin's `skills/checks/` (`plugin.json` `"skills": "./skills/"`) by `on` and removed by `off` (and by `cardinal-disconnect`). The SessionStart hook
  reconciles that copy with the setting and, while the preview is on and
  Cardinal is connected, adds the routing rule (answer with the query tools
  first; a check only for per-trace judgments). Off: no text at all.
- **Labels and reuse** go through MCP tools (`set_check_example`,
  `approve_check_example`, `remove_check_example`, `get_check_draft`,
  `lock_check_examples`, `list_accepted_checks`), never shell commands. The
  ledger is `~/.codex/cardinal/checks/drafts/`; disconnect removes it.
- **Range runs** (`execute_behavior_range`) run in a detached worker; the
  agent follows one by polling `get_behavior_range` (each call waits up to
  30 s for new events, and a completed run's summary comes back in it).
  Claude Code listens with its Monitor tool instead; Codex has no
  equivalent, so it polls.

**Limit: a change takes effect at the next session; no skill reload or
deny.** Claude Code's gate returns `reloadSkills` and denies its Skill tool
while the preview is off. A Codex SessionStart hook can only add context
(`hookSpecificOutput.additionalContext`; see the host-surface evidence above,
https://learn.chatgpt.com/docs/hooks), and Codex has no skill tool call to
deny: it lists the plugin's skills when the session starts and the model
opens a SKILL.md itself. So `on` / `off` apply to sessions started
afterwards; a running session keeps the skill list it started with.

## Owner-woken sessions

A stopped Codex session normally sees nothing until you type into it again.
With wake on, an **owner decision** you post on its Investigation page,
addressed to that session, resumes it: Codex reads the investigation events
and acts on your decision, inside the sandbox the session always had. The
design and its proofs are in
[`docs/specs/resume-and-wake.md`](../../docs/specs/resume-and-wake.md) §5–§6.

Turn it on or off in your own terminal (macOS; a per-user launchd agent):

```bash
python3 scripts/cardinal-wake enable            # notifications only
python3 scripts/cardinal-wake enable --launch   # also resume eligible Codex CLI sessions
python3 scripts/cardinal-wake status            # the setting, recent wakes, which sessions could launch
python3 scripts/cardinal-wake run --dry-run     # what the next pass would do; launches nothing
python3 scripts/cardinal-wake disable           # remove the launchd agent
```

`enable` copies this plugin's `cardinal_core` and a small runner into
`~/.cardinal/wake/lib/` and points `~/Library/LaunchAgents/io.cardinalhq.wake.plist`
there (every enable copies them again, so a plugin upgrade never leaves the
agent pointing at a removed version). It runs `cardinal-wake run --once`
every 45 s, with a Python that resolves outside your home directory, your
repositories, temp dirs and shims (this one, or the real interpreter behind
the `/usr/bin/python3` shim), in isolated mode without site (`-I -S`). `enable --launch` needs `codex` to be the native (Mach-O)
binary it runs: a Homebrew cask or a standalone install is used as is; an
npm install's `codex.js` is resolved to the native binary it would spawn
(`vendor/<triple>/codex/codex` of `@openai/codex`), which is launched
directly, never through node; any other script is refused. It also refuses
a `codex` that resolves inside a repository under your home, an approved
root, a temp dir or a version-manager shim (`~/.asdf`, `~/.local/share/mise`,
`~/.nodenv`, `~/.volta`, `~/.nvm` and the like, which pick the real binary
from files in the working directory). The daemon's PATH, and every woken
run's, is the system directories plus those binaries' directories, never
your shell's PATH. Each binary's path, size, mtime, inode, ctime and SHA-256
are checked again before every launch; if one changed, the decision is a
notification until you run `enable` again.

**What wakes a session.** Only a decision you post on the Investigation page
from your signed-in Cardinal web session, addressed to that session, with
"Resume this session if it has stopped" checked; without it the decision
is a notification only. Each step of a wake also shows on the
Investigation timeline (a `session.wake` event, with a short fixed reason
and never your decision's text). A
broadcast decision, a cue, a question, a challenge or a ruling never wakes
anything, and neither does anything an agent posts: Cardinal refuses an
owner decision from the plugin's API key, the only Cardinal credential an
agent holds (proof C-1). The waker reads the events from Cardinal itself,
never from the local inbox. A decision that is already acknowledged, or
that the session cannot see (`owner_decision` off, not the Investigation's
author), wakes nothing. Each decision causes at most one run, ever: a
never-pruned high-water mark per investigation and session refuses a seq it
has handled. A decision older than 7 days gets one notification and never
launches. A session gets at most 3 wakes a day, and the machine at most 10.

**Codex unattended launch is built but shipped off.** Its lockdown and
sandbox gates passed, but one gate (that a second decision can't start a
concurrent run) depends on detecting the processes a woken run leaves
behind, and two cases of that could not be verified live yet. So every
Codex wake is a notification for now, exactly like Cursor and Gemini CLI.
Turning it on is a reviewed code change (set `CODEX_LAUNCH_PROVEN = True`),
not a setting. The rest of this section describes what a Codex wake will do
once it is on.

**Which sessions can launch.** A Codex CLI session (`codex` or `codex exec`)
that meets all of these:
- every turn ran `-s workspace-write` or `-s read-only` with network off,
  and could write only inside its working directory (its recorded
  `writable_roots` and file-system policy are checked). Its **last** turn
  must be `workspace-write`: a read-only session is notify-only, because the
  survivor check needs a writable workspace to recognize the run's own
  processes;
- its working directory, taken from its rollout and resolved through
  symlinks, is a git work tree strictly inside your home directory, and
  neither in nor above a protected directory (`~/.cardinal`, `~/.codex`,
  `~/Library`, `~/.ssh`, `~/.config` and the like);
- there's no Codex project config (below);
- your Codex config (`~/.codex/config.toml`, `/etc/codex/*.toml`) doesn't
  widen workspace-write: no `sandbox_workspace_write`, `permissions`,
  `writable_roots` or `network_access`.

It is resumed exactly as

```
codex exec -s <its own sandbox> -c approval_policy="never" -c 'web_search="disabled"' \
  -c sandbox_workspace_write.network_access=false -c 'sandbox_workspace_write.writable_roots=[]' \
  -c sandbox_workspace_write.exclude_slash_tmp=true -c sandbox_workspace_write.exclude_tmpdir_env_var=false \
  -c 'mcp_servers.cardinal.tools.storyboard__read_investigation_events.approval_mode="approve"' \
  -c 'mcp_servers.cardinal.enabled_tools=["storyboard__read_investigation_events"]' \
  -c mcp_servers.<each other server>.enabled=false \
  -c 'plugins."<each non-Cardinal plugin>".enabled=false' \
  --disable apps --disable plugins --disable remote_plugin --disable image_generation \
  --disable computer_use --disable browser_use --disable browser_use_external --disable in_app_browser \
  -c notify=[] \
  --disable multi_agent --disable tool_suggest --disable skill_mcp_dependency_install --disable hooks \
  --disable daemon_auto_start \
  resume <session id> "Cardinal resumed this session for owner decision #<seq> in Investigation <inv_id>. Read it with the storyboard__read_investigation_events tool (investigation_id <inv_id>, types [\"owner.decided\"], after <seq - 1>) and act on it; read no other events. When done, write your answer to $TMPDIR/cardinal-wake-ack.json as {\"ack_of\": <seq>, \"disposition\": \"accepted\", \"declined\" or \"noted\", \"note\": \"what you did or why\"}. The note may not be passed on; put no secrets in it."
```

in that working directory, never one from the Investigation binding (which
the agent can write), with only `HOME`, `USER`, `LOGNAME`, `LANG`, `LC_*`,
`SHELL`, `TERM=dumb`, the vetted `PATH`, `OPENAI_API_KEY` (if you use API-key
auth) and a private `TMPDIR` in its environment: no `NODE_OPTIONS`,
`DYLD_*`, `*_BASE_URL`, proxy or CA variable reaches it. The `TMPDIR` is a
new 0700 directory under `~/.cardinal/wake/tmp/`, removed when the wake
settles; the shared `/tmp` is not writable.
- **Never passed:** `--profile`, `--dangerously-*`, `--ignore-*` or
  `--skip-git-repo-check`.
- **MCP and built-in tools:** Codex runs MCP servers outside its sandbox,
  and exposes tools your config doesn't list: ChatGPT app connectors, web
  search, image generation, the browser and computer-use tools, and plugin
  installs.
  - The woken run keeps only Cardinal's investigation read, pre-approved,
    since with approvals off an unapproved MCP call is refused. (The MCP
    post takes only cues, questions and challenges, never an
    acknowledgment, so it is off too.)
  - Every other MCP server and every plugin except Cardinal's are turned
    off, and so are the features behind those built-in tools.
  - What's left: the shell and `apply_patch` inside the sandbox, goals,
    MCP resource reads, `view_image`, and sub-agents, which inherit the
    same sandbox and tools.
  - Before each launch the waker checks these exact flags with
    `codex … features list` (no session). If this Codex doesn't know a
    feature or rejects a setting, or leaves on a feature the lockdown was
    not tested with (a new default in another Codex version), the decision
    is a notification: it never launches with a partial lockdown. Codex
    accepts an unknown `-c` key silently, so the sandbox settings are also
    read back from the woken run's turns (below).
  - Then `codex … mcp list --json` with the same flags must show no
    enabled MCP server but Cardinal's (it also sees cloud-managed and MDM
    config layers the waker doesn't parse). If it lists another, or can't
    run, the decision is a notification.
  - Servers declared in a way the waker can't turn off on the command line
    make the session notify-only.
- **Hooks are off.** Codex runs hooks outside its sandbox, in the
  workspace, so a hook (yours, or one resolving into a checkout) would be
  workspace code running unsandboxed. The woken run has none: no evidence
  capture, telemetry or owner-input capture, no investigation poller and no
  Stop reminder during it.
- **The prompt:** it names the decision's seq and investigation, the tool
  that reads it, and where to write the answer; the decision text is never
  in it, and the prompt itself is never recorded as owner input. The agent
  reads the decision with `storyboard__read_investigation_events`, owner
  decisions only: someone else's advisory never steers an unattended run.
- **notify is overridden.** A `notify` program in your Codex config runs
  outside the sandbox at every turn end (proven live, even with hooks
  off), so every woken run gets `-c notify=[]` as its last setting. Your
  own config is left as it is.
- **The acknowledgment is relayed.** With no hooks and no network, the
  woken agent can't acknowledge by itself. It writes
  `{"ack_of": <seq>, "disposition": "accepted" | "declined" | "noted",
  "note": "..."}` to `$TMPDIR/cardinal-wake-ack.json` (the wake's private
  directory). After the run has exited, and only then, the waker reads it
  (a regular file, never a symlink, at most 8 KiB, exactly those keys,
  `ack_of` the decision it woke the session for) and posts that one
  acknowledgment as the session, with this plugin's key, the same way
  `cardinal-storyboard investigation ack` does. The content is the
  agent's; the waker only carries it out. Anything else in the file, a
  different seq, or a run that failed its checks: nothing is posted, and
  the run is reported as ended without an acknowledgment. Only the
  disposition is relayed: the note is dropped, unless you run
  `cardinal-wake enable --launch --relay-notes`, and then it is scrubbed of
  credential shapes first. It is never shown in a notification.
- **The run ends when its last process does.** When codex exits, the waker
  stops anything the run left behind. It finds those by the sandbox itself:
  everything the run starts stays in Codex's sandbox (it can't escape it),
  and that sandbox lets it write the wake's private temp directory but not
  the parent `~/.cardinal/wake/tmp`. So the waker asks macOS
  (`sandbox_check`) which of your processes are sandboxed, allowed to write
  that temp directory, **and** denied on the parent — clearing a variable,
  `setsid`, `cd`, or re-exec can't change any of those answers. Those get
  SIGTERM, then SIGKILL. Your own editor and apps are not it: an editor
  isn't sandboxed, and the macOS agents that are sandboxed with broad
  write access under your home are allowed on the parent too, so the parent
  check rules them out. Until none is left the wake stays in progress, so no
  second run starts for the session and its temp dir is kept; if some
  process can't be cleared or checked within two hours, the run is marked
  failed, its temp dir left in place, and Codex launch is turned off. Right
  before every launch, under the session's lock, everything above is checked
  again; the same sandbox test also marks a session live if a process of an
  earlier wake is still running.

**Notify-only** (a macOS notification naming the runtime, the session, the
Investigation and the state, never the decision's text):
- Claude Code sessions (see the Claude plugin's README; use
  `claude --remote-control` to reach a running one), Cursor and Gemini CLI
  sessions (a Gemini CLI launch behind an allow-list proxy is built and off
  until its live proofs pass; see the Gemini plugin's README), and sessions
  whose runtime is not recognized;
- Codex desktop app sessions (its process outlives its threads), subagent
  threads, and any originator the waker does not recognize;
- a repository that carries its own Codex config: a `.codex/` with any
  `*.toml` or a `hooks.json` (or a `.codex` that is a symlink or
  unreadable) anywhere from the session's working directory up to the
  repository root. A project `config.toml` can declare MCP servers, and a
  resumed `codex exec` starts them outside the sandbox (proof C-8). A
  sandboxed agent cannot create `.codex`; a cloned repository or another
  tool can. Checked before every launch;
- any session that ever ran `danger-full-access` or with network access:
  that agent could have changed the waker's own files;
- a session whose window is still open (its process, a recent rollout
  write or tool call), which sees the decision itself. One gap: a Codex TUI
  started without the session id on its command line (`codex`, then
  picking the session), idle for more than 2 minutes, looks stopped. Close
  that window before you post a decision for it;
- everything, until you `enable --launch`.

**After a woken run.** Success means the session acknowledged your decision
(it shows on the Investigation timeline; for a Codex run the waker
relayed it, above). The report includes its disposition (accepted,
declined, noted). Only an acknowledgment from your
own principal naming the session counts, never a grantee's. An agent
holding your key could still post one, so this reports what the session
said. Starting a process is never reported as success.
- A run that ends without an acknowledgment is reported and never retried:
  post another decision if you want another run.
- A run still going after 2 hours is reported, left running, and judged
  when it exits.
- Then the waker checks the rollout. Every turn of the woken run must show
  the pinned sandbox with network off, no extra writable roots, the shared
  `/tmp` excluded, and writes only inside its working directory and its
  private `TMPDIR`, and every tool call must be on the allow-list (no web search,
  image generation, app connector or other MCP tool). If not (or with no
  turn recorded), it writes `~/.cardinal/wake/disabled/codex`
  with the reason, notifies you, and launches no Codex session again until
  you run `python3 scripts/cardinal-wake reenable codex`. It never retries
  with other flags.
- If the waker itself stops in the middle of a launch, the next pass finds
  the run by its command line and judges it normally.

**What a wake allows.** Processing your decision inside the session's own
sandbox: in `workspace-write`, no network and no writes outside the
workspace; with approvals off, anything that needs approval fails instead of
asking. A woken agent that needs a deploy, a revert, a merge or anything
else consequential does the rest of the work, then stops and asks; that
needs a later decision or your own turn. In `workspace-write` the
network-bound CLIs (`cardinal-storyboard investigation ack`,
`cardinal-evidence promote`) fail. The Cardinal read works, because Codex
runs MCP servers outside the sandbox, and that is exactly why every other
MCP tool is turned off for the run. A woken agent that needs you says so in
its acknowledgment's note.

**Trust boundary.** The waker uses this plugin's Cardinal key, so it sees
only your investigations, and it only considers sessions that ran on this
machine. Whoever holds your signed-in Cardinal web session can wake this
machine's eligible sessions, the way a self-hosted CI runner trusts its
account; the sandbox and the caps bound what such a wake can do. An agent
driving a browser signed in to Cardinal could post a decision; a sandboxed
Codex session has no network and cannot. An unsandboxed agent on this
machine already has your full user power, including over the waker itself,
which is why a session that ever ran unsandboxed is never launched.

Local state, all 0600 in 0700 directories:
- `~/.cardinal/wake/config.json`: `enabled`, `launch`, `codex_path`,
  `python`, `path_env`, the binaries' fingerprints, `enabled_at`.
- One ledger per session in `~/.cardinal/wake/ledger/`: entries keyed by
  investigation and seq, each transition (`DECISION_SEEN`, `CLAIMED`,
  `RESUMING`, `TIMED_OUT`, `ACKNOWLEDGED`, `FAILED`, `NOTIFIED`), and the
  high-water marks. It holds seqs and states only, never decision text.
- Each run's output in `~/.cardinal/wake/logs/`: the newest 50 are kept,
  and a finished one over 10 MB keeps its last 1 MB.

The
implementation is `cardinal_core.session_wake`, shared with the Claude Code
plugin. Not yet: a `session.wake` event on the Investigation timeline (the
notification and ledger are the report for now), and Linux or Windows.

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

## Repository associations across a session

Repository provenance is shared across Claude Code, Codex, Cursor and Gemini.
Starting in a parent workspace does not prevent later repository attachment:
explicit tool working directories, file arguments and successful edits add
repository labels locally. The background poller submits them to Cardinal.
The original checkout context remains unchanged; later repos are additive
`written_from` provenance, never automatically asserted as the storyboard's
subject. Labels contain canonical repository names, never local paths.

To backfill an existing session from its saved edited-file ledger, run:

```sh
cardinal-storyboard investigation repos --session SESSION_ID
```

Supply `--cwd /path/to/checkout` (repeatable) to attach additional checkouts.
This command uses the session's existing investigation and storyboard. It does
not publish or share anything. Automatic collection respects
`CARDINAL_STORYBOARD_CONTEXT=0`, and only an author's own session may sync.
At most 32 repositories are retained. Shell command text is not parsed: use
explicit tool working directories or the CLI for shell-only checkout changes.
Historical repair requires a retained local edit ledger or explicit checkouts.

Multiple labels and browser grouping require Cardinal's
`repository_provenance` capability (the companion Conductor migration/server
change). Older servers continue to accept the existing context request, and
the CLI reports when full multi-repo support is unavailable. Every adapter's
next patch release includes this same shared implementation; the parity matrix
and packaged-artifact tests guard against adapter drift.
