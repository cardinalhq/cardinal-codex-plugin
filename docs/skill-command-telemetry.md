# Skill / command usage telemetry — Codex plugin

**Status**: draft (Codex port)
**Reference**: the Claude twin's
`docs/skill-command-telemetry.md` (the original analysis) and
`docs/specs/codex-port.md` §2 (hook-input compatibility).
This doc covers only what this plugin does for skill/command attribution
on Codex.

---

## Why the plugin barely needs to change

Almost everything the Skills-distribution feature needs is already on
the wire, emitted by Codex's **native** OTel exporter (which
`cardinal-connect` configures via the `[otel]` block in
`~/.codex/config.toml`). Every tool execution lands as a tool-result
event whose tags identify the thing invoked — model-invoked skills, MCP
tools (`mcp_server_name` / `mcp_tool_name`), and subagent spawns
(Codex's subagent tool is `spawn_agent`, vs Claude's `Agent`/`Task`).

Subagents need no plugin assist: their internal tool calls emit
tool-result events under the **parent** `session_id`, so skills/MCP/tools
used inside a subagent are already counted.

All of these carry `session_id` / `user_email` / `event_sequence` as
indexed dimensions, so they join to `agent_sessions` rows for free.
**No plugin work is needed for any of that.**

The one genuine gap: **skills the user invokes by typing a slash
command** (`/code-review`, `/verify`, …). Codex expands those directly
into context — no `Skill` tool call happens, so no tool-result event
fires. Without a plugin assist, user-invoked skills are invisible and
the adoption numbers undercount exactly the invocations that show
deliberate human intent.

The fix is small because the plugin already has a hook in the right
place: `git-state.py` runs on every `UserPromptSubmit` and already POSTs
one `cardinal.git_state` event per turn. The hook's stdin payload
includes the raw `prompt` (Codex uses the same field names as Claude
Code — see `docs/specs/codex-port.md` §2) — for slash-command turns,
that is the command text. We parse the command name out and stamp it on
the event we already send.

## The change — `hooks/git-state.py`

### Detection

After parsing the hook payload, inspect `payload["prompt"]`:

```python
_COMMAND_RE = re.compile(r"^\s*/([A-Za-z0-9][\w:-]*)")

def _detect_command(prompt: str | None) -> str | None:
    """'/code-review --fix' → 'code-review'; non-command prompts → None."""
    if not prompt:
        return None
    m = _COMMAND_RE.match(prompt)
    return m.group(1) if m else None
```

Rules:

- **Name only, never arguments.** Slash-command args are free text
  (`/deep-research <anything>`) and can carry sensitive content. The
  attribute is the command name, full stop.
- Namespaced commands (`plugin:command`) pass through verbatim — the
  `:` is part of the name and downstream grouping wants it.
- A prompt that merely *mentions* a slash command mid-sentence does
  not match (anchored at start).
- Built-in CLI commands match too. That is fine — filtering builtins
  from *skills* is a downstream concern (lakerunner knows the skill
  vocabulary; the plugin does not, and must not hardcode a denylist
  that rots).

### Emission

One optional attribute on the existing `cardinal.git_state` log record,
same emission style as `cardinal.initiative.name`:

```
log.attributes:
  ...
  cardinal.command = "code-review"        (only when the turn is a slash command)
```

Stored form in lakerunner (dots → underscores): `cardinal_command`.

Why piggyback rather than a second event: the command is a per-turn
fact, `cardinal.git_state` is the per-turn event, and one POST per turn
keeps the hook's latency budget and failure surface unchanged.

### Open validation item

The exact `prompt` shape Codex hands to `UserPromptSubmit` for
slash-command turns needs one empirical check: it is expected to be the
raw typed text (`"/code-review --fix"`), but if it arrives pre-expanded
(`<command-name>/code-review</command-name>…`), the regex must also
accept that form:

```python
_COMMAND_TAG_RE = re.compile(r"<command-name>\s*/?([\w:-]+)\s*</command-name>")
```

Cheap to validate: log the payload from a live hook once, confirm the
shape, then delete the logging. Ship whichever branch matches reality;
keeping both costs four lines.

### Non-functional invariants

- Best-effort: any failure → `exit 0`, never block the prompt.
- No new POST, no new timeout, no new dependency.
- Tests: extend the plugin test suite with `_detect_command` table
  cases (plain command, args, namespaced, mid-sentence mention, empty,
  tag-wrapped) and one end-to-end payload assertion that
  `cardinal.command` appears when expected and is absent otherwise.

## Out of scope (fast-follow candidate)

**Installed-skills inventory** — the /outcomes "unused skills" feature
("you shipped 9 skills, 4 have never fired") needs to know what is
*installed*, not just what *fired*. That would be a new `SessionStart`-
hook event (`cardinal.skills_installed`, names only) enumerating skills
visible to the session. Deferred: new event type, new payload contract.
