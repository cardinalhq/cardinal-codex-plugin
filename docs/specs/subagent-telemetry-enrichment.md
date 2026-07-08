# Subagent telemetry enrichment (latent-subagent mining, Codex side)

Codex counterpart of cardinal-claude-plugin
`docs/specs/subagent-telemetry-enrichment.md`: add the targeted fields
the latent-subagent harvester (conductor
`docs/specs/latent-subagent-harvester.md`) needs, adapted to what the
Codex runtime actually exposes. The goal is **one query shape across
runtimes** — a harvester query grouped on
`(tool_name, bash_class, user_turn_seq, model)` must work identically
over Claude and Codex sessions.

Everything lands in the single hook script
(`plugins/cardinal-codex-plugin/hooks/cardinal-codex-telemetry.py`),
same contract as today: best-effort, silent-exit, fail-open,
cursor-resumed transcript processing.

## What Codex already has that Claude needed fixing

- **No long-turn data loss.** The Stop handler's `last_line` cursor
  with resume-offset (`MAX_EVENTS_PER_STOP=512`, unprocessed tail
  picked up next firing) already achieves what the Claude spec's
  "chunked emission" field buys. No change needed.
- **Per-call cost.** `compute_cost_usd` already prices each
  `api_request`; `turn_usage` carries `model` and links to
  `turn_tool` via `turn_seq`. No change needed.
- **Bash command text in-process.** `normalize_tool_name` already
  receives the full `exec_command` cmd string, so the verb classifier
  is a lookup away (field 3).

## Field 1 — MCP qualified names on `cardinal.turn_tool`

Today `normalize_tool_name` maps `mcp__<server>__<tool>` to the
generic `tool_name="mcp_tool"` and the server/tool names survive only
on `tool_result`. The Claude plugin emits the raw qualified name as
`turn_tool.tool_name` — MCP names are the strongest clustering signal
the harvester has (they narrate intent without content), and the
observability archetype was unsizable without them.

Change: for MCP calls, `cardinal.turn_tool` carries the **raw
qualified name** as `tool_name` (parity with Claude `turn_tool`) plus
`mcp_server_name` / `mcp_tool_name` as separate attributes.
`tool_result` keeps the normalized `mcp_tool` form — that is what
lakerunner's `mcp_servers_used` aggregation reads; do not disturb it.

## Field 2 — session-monotonic `user_turn_seq`

`turn_seq` resets at each `user_message` boundary (Claude parity) and
`tool_seq` resets per model call — correct, but nothing totally orders
a session's tool stream across turns; ordering leans on wall-clock
`ts`. Add `user_turn_seq` (int): the ordinal of the current user turn
within the session, incremented on each `event_msg`/`user_message`
record, **persisted in the per-session progress file** alongside
`turn_seq`/`tool_seq` (the cursor design makes this incremental —
cheaper than the Claude side's per-Stop transcript walk, same
semantics). Stamp it on every `cardinal.turn_usage` and
`cardinal.turn_tool` record. `(user_turn_seq, turn_seq, tool_seq)`
then totally orders the stream, same triple as Claude.

Reset rule: when the progress cursor resets (`last_line >
len(lines)` — truncated/rotated transcript), `user_turn_seq` resets
with the other counters.

## Field 3 — privacy-safe `bash_class` on Bash `turn_tool` records

Same closed enum, same classifier rules, and same emit boundary as the
Claude spec (see that spec's field 4 for the full table):
`git-read | git-write | test | build | pkg | file-read | file-write |
network | other`, plus `bash_multi=true` for compound commands
spanning classes; most-write-risky class wins; classification input
is the command word per shell segment; **the enum is what lands on
`cardinal.turn_tool` — never the command text on that event.**

Codex note on the boundary: this plugin's `tool_result` event already
carries the full `exec_command` args in `tool_input` (an existing,
documented divergence from the Claude plugin, consumed by lakerunner
as-is). This spec neither widens nor narrows that — it only ensures
`cardinal.turn_tool`, the event the harvester clusters on, carries
the same coarse enum on both runtimes so one query works everywhere.

Implementation: `normalize_tool_name` already isolates the command
string for `exec_command`; add the classifier (static command-word →
class dict, shared fixtures with the Claude repo per the
lockstep-testing convention in `claude-parity.md`) and attach
`bash_class`/`bash_multi` in `append_tool_call_event`.

## Field 4 — subagent components + model (gated on P5)

The Claude keystone field — per-spawn token component split,
`subagent_model`, tool histogram — is **blocked on the P5 deferral**
in `claude-parity.md`: Codex's `SubagentStop` payload shape has never
been observed in the wild, and `handle_subagent_stop` emits only when
the payload happens to carry token totals.

Staged approach:

1. **Capture first.** Add a debug affordance (env-gated
   `CARDINAL_CODEX_DEBUG_PAYLOADS=1` dump of the raw `SubagentStop`
   payload to `~/.codex/cardinal/telemetry/debug/`) and run a real
   Codex multi-agent session to capture the shape. This is the P5
   follow-up, now with a deadline: it gates harvester coverage of
   Codex subagents.
2. **Then mirror.** Once the payload (or a subagent transcript path
   convention) is known, emit the same attribute set as the Claude
   spec's field 1: `subagent_input_tokens`, `subagent_output_tokens`,
   `subagent_cache_creation_tokens`, `subagent_model`,
   `subagent_model_count`, `subagent_tool_counts` (32-name cap +
   truncation flag), preserving `total_tokens` = component sum.
3. **Until then, degrade honestly.** Sessions without the enriched
   subagent event simply don't contribute to Codex subagent analytics
   — the harvester's no-thin-signal rule excludes them; nothing is
   guessed.

## Assumed-agent catalog (open question, do not block on it)

The Claude plugin ships a `brainstorm` catalog agent with a `model:`
pin. Codex's agent-definition surface (whether custom subagents can
be defined and model-pinned by a plugin) is unverified. Investigate
alongside P5; if no equivalent exists, the fallback is a `/brainstorm`
custom prompt that self-labels via `cardinal_command` — labeling
without delegation. Record the finding in `claude-parity.md`'s
asymmetries table either way. Nothing in fields 1–3 depends on this.

## Testing

Extend `tests/test_cardinal_plugin.py`:

- MCP call → `turn_tool.tool_name` is the raw qualified name +
  server/tool attrs; `tool_result` unchanged (`mcp_tool`).
- `user_turn_seq`: multi-turn synthetic transcript → increments on
  `user_message` only; survives cursor resume across two Stop
  firings; resets with the cursor on transcript truncation.
- `bash_class`: **shared fixtures with the Claude repo** (lockstep
  convention) — same command → same class in both test suites;
  compound command → most-write-risky + `bash_multi`; assert the
  emitted `turn_tool` attributes never contain the command string.
- SubagentStop debug dump: env-gated, off by default, writes nothing
  when unset.

## Rollout & measurement

- Ships as v0.5.0; bump `PLUGIN_VERSION`.
- Cross-runtime validation gate: after both plugins ship, one
  harvester query over `agent_session_events` grouped by
  `(agent_runtime, tool_name, bash_class)` must return comparable
  archetype pools for Claude and Codex sessions — that query
  succeeding is the definition of done for parity.
- P5 capture (field 4 step 1) has no user-visible effect and can ship
  in the same release.
