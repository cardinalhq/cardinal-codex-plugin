---
name: semantic-dag
description: Show the current Codex task as a live, animated semantic DAG in a local browser. Use when the user asks to watch Codex work, see live progress visually, or replace a scrolling work log with a task graph. Do not use for ordinary status summaries or static diagrams.
---

# Semantic DAG

Drive a live typed task graph at `http://127.0.0.1:8766/t/<thread>` while doing the user's work. The graph is a semantic memory of the task, not an execution trace.

The viewer is one Cardinal workspace shared with Claude at `~/.cardinal/state/semantic-dag`. Its left navigation lists every session and marks the originating runtime. Inside a session, the Agents view shows only the launch hierarchy, while the Workflow view renders one independent semantic DAG per agent. Never merge different agents' workflow nodes into one visual DAG, and never model agents themselves as semantic nodes.

The helper is `scripts/emit.py`, resolved relative to this file. Replace `<emit>` below with its absolute path.

## Turn boundary

Activation is opt-in at the plugin level. The installed prompt bridge maintains
watch mode for tasks that explicitly activated the skill. A user may also make
new tasks default-on in their own user space with `watch-default on`; that
setting is stored in `~/.cardinal/state/semantic-dag/config.json`, not in the
plugin. When the bridge says user-default watch mode is active, do not run
`begin` again.

When the user invokes `$semantic-dag` explicitly, run `begin` so watch mode is
bound to the native task.

For manual activation only, before substantive work run:

```bash
python3 <emit> begin "<2–6 word topic>"
```

`begin` uses the native Codex thread ID when available, repaints an existing thread, starts the viewer, and opens it whenever no viewer is connected. It also enables persistent watch mode for this task. The installed `UserPromptSubmit` bridge repaints active tasks and, when enabled by the user's `watch-default` setting, creates new watched tasks while supplying a compact version of the required emission protocol. It points back to this full skill only for edge cases so normal continuation turns do not repeatedly load this entire file.

At the end of every turn, including blocked or failed turns, run this immediately before the final response:

```bash
python3 <emit> finish "<factual one-line outcome>"
```

`finish` ends the current graph turn but intentionally leaves watch mode enabled. The user can submit `semantic-dag off`, or run `python3 <emit> watch off`, to disable later-turn repainting for this task.

## Semantic ontology

Every node has exactly one first-class `type`, independent of its `status`. Use only:

- `GOAL` — desired end state.
- `QUESTION` — unresolved question that affects the work.
- `HYPOTHESIS` — candidate explanation that can be confirmed or rejected.
- `DECISION` — meaningful choice that constrains future work.
- `WORK` — substantial investigation, implementation, analysis, or verification phase.
- `EVIDENCE` — durable observation that supports or refutes something.
- `OUTCOME` — meaningful result, resolution, or completed state.

Use only these typed relationships:

`decomposes_into`, `raises`, `tested_by`, `supported_by`, `refuted_by`, `resolved_by`, `based_on`, `leads_to`, `depends_on`, `produces`, `implements`, `validates`, `supersedes`.

Read a relationship left to right: `hypothesis refuted_by evidence`, `question resolved_by decision`, `work produces outcome`.

Statuses are lifecycle or disposition, never node types: `pending`, `active`, `paused`, `completed`, `confirmed`, `rejected`, `superseded`, `resolved`, or `error`. Thus `HYPOTHESIS(status=rejected)`, `DECISION(status=superseded)`, and `WORK(status=active)` remain distinct.

## Node-worthiness test

Before adding a node, ask:

> Would a future agent want to retrieve this item independently and understand how it relates to the rest of the work?

If not, keep it as metadata. Never create semantic nodes for individual tool calls, commands, files, glossary concepts, narration, or subagents. Attach commands, concepts, and narration with `tool`, `concept`, `note`, or agent provenance; the session bridge attaches file metadata automatically. Prefer semantic granularity over execution granularity: one `WORK` node may contain dozens of tool calls and many file events.

Reuse a stable ID when revisiting the same goal, question, hypothesis, decision, work phase, evidence, or outcome. `add` is an upsert: it updates the existing node's type, label, and supplied description while preserving its status and provenance. Do not create a near-duplicate just because the item was revisited.

## Emit the graph

```bash
python3 <emit> add <id> <TYPE> "<label>" [--parent <id> | --root] [--relation <relationship>] [--description "<terse live description>"]
python3 <emit> link <from-id> <relationship> <to-id>
python3 <emit> activate <id>
python3 <emit> status <id> <status> ["<reason>"]
python3 <emit> done <id>
python3 <emit> error <id> "<reason>"
python3 <emit> describe <id> "<updated live description>"
python3 <emit> note <id> "<one-line live narration>"
python3 <emit> tool <id> "<tool-name>" "<short summary>"
python3 <emit> concept <id> "<important term>" "<one-sentence definition>"
python3 <emit> define "<important term>" "<one-sentence definition>"
python3 <emit> undefine "<important term>"
python3 <emit> watch <on|off>
python3 <emit> watch-default <on|off>
```

`done` is a convenience alias for `status <id> completed`. Use `status` for semantic dispositions such as `confirmed`, `rejected`, `resolved`, or `superseded`.

Create the semantic node before doing its work and activate it so the viewer pulses it in real time. Every label must be a concrete 2–7 word domain phrase that remains meaningful without the drawer, such as `Explain intermittent token expiry`, `Choose bounded retry policy`, or `Verify retry isolation`. Never use positional placeholders such as `Stacking Phase 5` or `Next Step`.

An explicit `--parent` defaults to `decomposes_into`; an automatic chain defaults to `leads_to`. Supply `--relation` whenever another relationship is more accurate. Without `--parent` or `--root`, a new node chains to the most recent node owned by the same agent. Use `--root` for a genuinely independent top-level semantic item.

Add a terse `--description` so the drawer explains the item in real time. `activate` automatically seeds the node's first narration entry from that description when it has no notes. Use `describe` when its meaning materially changes, then add 1–3 further useful notes for evolving facts rather than one note per tool call.

Agent cards present a plain-language **Problem Statement** and **Solution**.
Write topics, delegated tasks, node descriptions, progress notes, and finish
summaries for a non-technical reader: say what needs to be accomplished and
what was found or changed, without exposing tool names or internal mechanics.

File attribution is automatic and out of band: the Codex session-event bridge records completed structured reads and file changes on the active node. Do not emit file metadata manually. Attach domain terms with `concept`, which creates a drawer tab and dictionary entry. Use `define` only for important turn-wide terms. Do not define ordinary verbs, commands, filenames, or obvious tool names.

## Glossary discipline

Populate the glossary on every substantive turn that introduces domain language. As soon as the first relevant node is active, attach 1–3 genuinely task-specific, non-obvious terms with `concept`; before `finish`, add any missing term that a future reader would need to understand the graph. Use a contextual one-sentence definition and keep the term attached to the node where it matters.

Keep this bounded: do not add more than three new terms in one turn unless the user asks for a richer glossary, update an existing case-insensitive term instead of creating a variant, and never add filler on a trivial turn with no specialized vocabulary.

## Subagent provenance

Subagents are not semantic nodes. Their work appears through ordinary typed semantic nodes carrying agent provenance.

Before delegating, activate the semantic node that owns the delegated work. The native Codex session bridge consumes the authoritative `sub_agent_activity(kind="started")` record, registers the agent display name and launch hierarchy, links it to that active owning node, binds the child thread to this DAG, and starts a child tailer. Do not require the subagent to run `begin` merely to appear in the graph.

On the subagent's first native Codex commentary, the child bridge creates and activates a provisional `WORK` node automatically, so its pane has a live task and narration even if the subagent emits no graph commands. Its native final response completes that node and the agent automatically. Explicit durable nodes still render only in that subagent's own Workflow DAG and are namespaced as `<agent-id>::<node-id>`. Each agent may have one active node, and multiple agents may pulse concurrently. The child binding supplies its agent identity, so the subagent uses normal typed `add`, status, metadata, and `finish` commands only when it needs richer semantics or a custom concise completion summary. Only root `finish` completes the graph.

## Communication and controls

Keep each commentary update to one sentence at meaningful transitions. The Codex session bridge mirrors native commentary onto the active node automatically and suppresses duplicates, so do not repeat ordinary commentary with `note`. Use `note` only for graph narration that was not sent to the user. The graph is the primary progress surface, but the final response remains self-contained. Do not narrate emission commands.

```bash
python3 <emit> url
python3 <emit> reset "<new topic>"
python3 <emit> topic "<new topic>"
```

Set `SEMANTIC_DAG_PORT` to change the port, `SEMANTIC_DAG_NO_OPEN=1` to suppress browser launch, or `SEMANTIC_DAG_NO_SERVER=1` for headless testing. Read [references/codex-hooks.md](references/codex-hooks.md) when installing the later-turn prompt bridge or lifecycle-hook fallback; obtain confirmation before changing global hooks outside an explicitly requested installation or repair.

`watch off` disables only the current task. Plugin installs are opt-in by
default. `watch-default on` enables automatic activation for new tasks in the
current user's state, while `watch-default off` disables that user preference.
The `SEMANTIC_DAG_WATCH_DEFAULT` environment variable can override the user
setting for one process.
