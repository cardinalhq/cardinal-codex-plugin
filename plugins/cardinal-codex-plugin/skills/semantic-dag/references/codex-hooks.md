# Codex lifecycle hooks

## Persistent later-turn repaint

Skills are selected per prompt, so a standalone `$semantic-dag` invocation
does not by itself guarantee that the next unrelated question reloads the
skill. The included `UserPromptSubmit` bridge closes that gap. `begin` records
watch mode against the native task binding; later prompts repaint the existing
thread and receive a compact continuation protocol. The full skill is named
only as an edge-case reference, avoiding a repeated full-file prompt cost on
ordinary turns. The hook is silent for tasks where watch mode was never
enabled.

The Cardinal connect flow installs this bridge through its stable launcher,
without overwriting unrelated hooks. For a manual development checkout, merge
this matcher group into `UserPromptSubmit` and replace `<plugin-root>`:

```json
{
  "matcher": "",
  "hooks": [
    {
      "type": "command",
      "command": "/usr/bin/python3 <plugin-root>/skills/semantic-dag/scripts/hooks/prompt_hook.py",
      "timeout": 3,
      "statusMessage": "Repainting Semantic DAG"
    }
  ]
}
```

`finish` does not disable watch mode. The exact prompt `semantic-dag off` or
the CLI command `emit.py watch off` disables it for that task.

## Native activity bridge

Every semantic emission ensures that a quiet session-event bridge is running
for the native Codex task, so later-turn resets recover automatically after a
process or application restart. The bridge tails Codex's completed local
session records and mirrors native progress commentary onto the agent's active
node. A native `sub_agent_activity(kind="started")` record registers each new
agent immediately, records its launch hierarchy and owning active node, binds
the child thread to the same DAG, and starts a child tailer. The binding carries
the launch timestamp: forked parent history is ignored while output from a
child that finishes before its tailer starts is still consumed. If a registered
subagent has no active node, its first commentary creates
and activates a provisional `WORK` node linked to the delegated parent. The
viewer therefore shows useful subagent activity without depending on the model
to emit narration commands. Native message IDs and a short text/time window
suppress duplicates when both structured record forms—or an explicit `note`—
carry the same update. A native subagent final response also completes its open
nodes and supplies the agent summary unless an explicit `finish` already did so.

The same bridge attaches
`CommandExecution.parsed_cmd` reads plus exact `FileChange.changes` updates to
the node that was active when each record completed. It checkpoints its byte
offset under the Semantic DAG state directory, deduplicates materialized paths,
and never adds content to the model prompt.

The personal lifecycle hooks remain a compatibility fallback for Codex
runtimes that emit `PreToolUse` and `PostToolUse`. `PreToolUse` paints the most
recent tool as a badge; `PostToolUse` uses the same conservative classifier for
successful direct file tools and common shell commands. File attribution must
not rely on model-generated `file` commands.

The Cardinal connect flow also installs this quiet bridge. For a manual
development checkout, inspect `~/.codex/hooks.json` and merge this entry
without overwriting unrelated hooks:

```json
{
  "description": "Personal Codex lifecycle hooks.",
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "/usr/bin/python3 <plugin-root>/skills/semantic-dag/scripts/hooks/tool_hook.py",
            "timeout": 3,
            "async": true
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "/usr/bin/python3 <plugin-root>/skills/semantic-dag/scripts/hooks/tool_hook.py",
            "timeout": 3,
            "async": true
          }
        ]
      }
    ]
  }
}
```

If the file already has a `PreToolUse` or `PostToolUse` list, append the
corresponding matcher group. Codex will ask the user to trust newly discovered
personal hooks. Do not bypass hook trust.

Both paths use the native session binding to find the right DAG, so concurrent
Codex tasks in the same working directory remain isolated.
