---
name: cardinal-mechanize
description: Compile a completed Codex CLI session (a past investigation) into a candidate Sentinel DAG plus rationale — a reusable procedure that could later be executed against a similar problem. Use when the user asks to /mechanize, compile a session, or extract a reusable investigation procedure. Spike-quality; produces YAML + rationale, does not execute anything.
---

# mechanize (Codex CLI) — compile a Codex CLI session into a Sentinel DAG

**Spike-quality compiler.** Produces a candidate `sentinel.yaml` + `rationale.md` from a past investigation session. Does NOT execute the Sentinel; that's a separate executor. Does NOT ship — this is exploratory work, and the rationale is where the honesty lives.

This SKILL.md is the **Codex-CLI-specific** part of the mechanize skill: how to find the session, how to read Codex's session JSONL, and how to handle the pieces that differ from other agents (attachments, assistant text, cold-subagent mechanism). The shared compilation algorithm — Stages 2 through 7, the Sentinel example, the ratification checklist, the expression language, the capability registry, the rules — lives in `CORE.md`, co-located in this directory.

**You MUST read `CORE.md` in full after finishing the Codex-specific stages below.**

## How this skill is invoked

The user typed `/mechanize`, possibly with a session ID or path as argument.

**Argument parsing:**
- If the user provided an argument that looks like an absolute path ending in `.jsonl` → that's `SESSION_PATH`.
- If the user provided a session UUID → look under `~/.codex/sessions/**/*<uuid>.jsonl` (recursive glob) and pick the match.
- If the user provided nothing → **default to the current session**. Resolve as follows:
  1. Read `$CODEX_SESSION_ID` (or `$OPENAI_CODEX_SESSION_ID` as fallback) from the environment.
  2. Glob `~/.codex/sessions/**/*<session_id>.jsonl` and pick the newest by mtime.
  3. Tell the user which session you resolved (short ID + path) in one line before proceeding.
  If no session ID is set in the environment, THEN ask the user to paste a path or ID.

**Caveat when compiling the current session:** the tail of the JSONL contains the `/mechanize` invocation itself. Treat everything from the user's `/mechanize` `user_message` onward as INCIDENTAL. Segment on the last substantive investigation conclusion BEFORE the mechanize call.

**Output location default:** `./mechanize-out/<session-id-short>/` under the current working directory. If the CWD is not writable, fall back to `~/mechanize-out/<session-id-short>/`. Tell the user where you're writing.

## Then, before anything else — read the spec

Read `sentinels.md` §§ 8, 9, 10, 11, 12, 13, 14, 14a, 28, 28.1, 29, 32, 37, 47, 52 (co-located in this directory), and `FINDINGS.md` in full. The complete reading list with rationale is at the top of `CORE.md`. Do NOT skip this.

## Stage 1 — Read and segment (Codex-CLI-specific)

Read the JSONL. Each line is a JSON object with three top-level fields: `timestamp` (ISO 8601), `type`, and `payload` (nested object).

**Record types you need to consume:**

- `type: "session_meta"` — `payload.id` (session UUID), `payload.cwd`. Session boundary marker.
- `type: "turn_context"` — `payload.model`, `payload.cwd`. Model changes across the session live here.
- `type: "event_msg"` with `payload.type == "user_message"` — `payload.message` is the user turn text. This is where you find the **objective** (first substantive user message) and later user follow-ups.
- `type: "event_msg"` with `payload.type == "token_count"` — closes each model call within a turn. Not directly compilation-relevant, but useful to know it delimits model calls.
- `type: "response_item"` with `payload.type == "function_call"` — a tool call. Fields: `payload.name` (tool name), `payload.arguments` (**JSON-encoded string** — you MUST `json.loads` it), `payload.call_id`.
- `type: "response_item"` with `payload.type == "function_call_output"` — the corresponding tool result. Fields: `payload.call_id` (matches the call_id above), `payload.output` (string).

**Tool-call/result pairing** is by `call_id`.

**Assistant text messages (the conclusion) — CAVEAT:** this repo's Codex telemetry hook does not enumerate an assistant-text record type. Codex may or may not persist assistant prose to the transcript under a `response_item` type this SKILL doesn't yet name. Before extracting the conclusion:

1. Read the tail of the JSONL and enumerate distinct `(type, payload.type)` pairs.
2. If you find a `response_item` variant that carries assistant text (e.g. `payload.type == "message"` or `"assistant_message"` or `"agent_message"`), use it — extract the last substantive assistant text block(s) as the conclusion.
3. If you do NOT find an assistant-text record type, note this in the rationale under `Unresolved: assistant conclusion not persisted in Codex transcript for this session; conclusion inferred from the tool-call sequence and the user's final turn`. Proceed — but Stage 3's investigation-vs-task-execution test relies on the conclusion shape, so this is a real fidelity hit worth flagging.

**Notable tool-name shapes** (from the telemetry hook's normalizer):

- `exec_command` — arguments `{"cmd": "<shell string>"}`. This is Codex's shell-shaped tool.
- `apply_patch` or `functions.apply_patch` — arguments `{"patch": "*** Begin Patch\n..."}`. Patch text uses `*** Update File:`, `*** Add File:`, `*** Delete File:` headers.
- `mcp__<server>__<tool>` — MCP tool calls.

**Attachments:** not enumerated by the telemetry hook; not observed in the sampled transcripts this SKILL was written against. If you encounter unknown record types in the tail of the transcript that appear to carry image/file data, note them in the rationale under `Unresolved: Codex attachment vocabulary not documented in this SKILL` and apply Stage 4.5 conservatively (prefer Q3 `requires-manual-input`).

Produce a mental model of:
- **Objective**: the first `event_msg`/`user_message`'s `payload.message` (skip slash-command entries and empty prompts).
- **Tool calls**: ordered list of `response_item`/`function_call` records with their ordinal, normalized name, parsed input (`json.loads(payload.arguments)`), and paired `function_call_output` content.
- **Conclusion**: last substantive assistant text (if any assistant-text record type exists in this transcript — see caveat above).

## Stage 1.5 — Recognize spill-to-disk pairs (Codex-CLI-specific)

**Status: unknown.** The Cardinal Codex telemetry hook does not scan tool_result outputs for spill-to-disk markers, and no such marker text is documented in this repo. Codex may or may not truncate large `exec_command` / `function_call_output` results with a "saved to file" pointer.

**Procedure:**
1. Scan every `function_call_output`'s `payload.output` for patterns suggestive of truncation-with-spill (e.g. `Output has been saved to`, `Truncated. Full output at`, `See file:`). If none found, this stage is a no-op.
2. If a marker IS found, treat it identically to CORE.md's Stage 1.5 collapsing rule (documented in the Claude adapter's SKILL.md — the semantics are agent-agnostic even if the marker text differs). Note the discovered marker text pattern in `rationale.md` under `Unresolved: Codex spill-to-disk pattern observed but not documented`; a future SKILL revision can codify it.

Otherwise, apply CORE.md's Stage 2 INCIDENTAL rule normally to any calls that reference `~/.codex/` state directories for non-spill reasons.

## Stage 2 addendum — shell-shaped tool in Codex CLI

`exec_command` is Codex's shell-shaped tool. Apply CORE.md Stage 2's synthetic-capability-ID rule (`bash.<argv[0]>`) to every `exec_command` call — parse `payload.arguments.cmd` with a shell tokenizer and take `argv[0]`. Preserve the raw tool name `exec_command` and add the synthetic ID for capability binding.

For `apply_patch` / `functions.apply_patch`, synthesize capability ID `code.apply-patch` — this ID is NOT in CORE.md's Known-capability registry, so record `capability-registry-extension-needed: code.apply-patch` in `rationale.md` per CORE.md ratification rule R2. Parse the patch header (`*** Update File:` / `*** Add File:` / `*** Delete File:`) to record the target path(s) as node output shape.

For `mcp__<server>__<tool>` names, treat the `<server>` prefix as the vendor/toolkit hint and derive an abstract capability ID per CORE.md's "Known capability registry" — do NOT emit `mcp__<server>__<tool>` as the capability ID (that leaks vendor shape and fails ratification rule R2).

## Stage 4.5 addendum — attachment vocabulary in Codex CLI transcripts

Not documented in this repo. If Stage 1 turned up no attachment-shaped records, Stage 4.5 has no attachments to apply the chooser to — record `Attachment handling: no attachments in this session` in the rationale.

If Stage 1 did turn up unknown attachment-shaped records, apply CORE.md Stage 4.5's Q1–Q4 conservatively: default to Q3 `requires-manual-input` unless the operator's inference from the attachment clearly became a plain-typed downstream input (Q1). Do NOT decode attachment content and describe it as evidence (Q4 is absolute).

## Stage 5.5 addendum — cold-subagent mechanism in Codex CLI

Codex CLI does not have a canonical `Agent`-tool equivalent that this SKILL can invoke synchronously. If the user's Codex install has a subagent-spawning MCP tool configured, use it. Otherwise:

**Fallback: inline ratification.** Perform the ratification checklist yourself in a fresh reasoning pass — but flag the degradation loudly in `rationale.md`:

> `Unresolved: Stage 5.5 ran inline because Codex CLI in this configuration exposes no cold-subagent mechanism to this SKILL. The verdict below is weaker than a cold read would produce; a reviewer should treat R1–R6 as PASS-with-caveat rather than PASS.`

The inline pass still uses CORE.md Stage 5.5's checklist and verdict format verbatim.

## Now continue with CORE.md

At this point you should have:
- A resolved session file path.
- A segmented mental model of the session (objective, tool calls with parsed arguments, conclusion or a caveat).
- Any spill-to-disk pairs collapsed per Stage 1.5 (if the marker exists in Codex — usually a no-op).

Continue at **CORE.md Stage 2** and follow through Stage 7. When CORE.md references the Stage 4.5 chooser, apply it with the Codex attachment vocabulary caveats above. When CORE.md instructs you to spawn a Stage 5.5 cold subagent, use the mechanism above (or the inline fallback with the degradation flag).

Do NOT skip any of Stages 2 through 7. Do NOT hallucinate rules that aren't in CORE.md.

## Success criterion

See CORE.md's "Success criterion" section. A `sentinel.yaml` + `rationale.md` that a human reader can audit is the bar — nothing less. For Codex-CLI-compiled Sentinels, the rationale MUST additionally include any `Unresolved:` notes triggered above (assistant-text caveat, spill-marker caveat, attachment caveat, Stage 5.5 degradation caveat) so downstream reviewers know which claims are provisional.
