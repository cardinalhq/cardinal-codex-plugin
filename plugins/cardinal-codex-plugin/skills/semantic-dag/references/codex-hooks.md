# Codex lifecycle hooks

## Persistent later-turn repaint

Skills are selected per prompt, so a standalone `$semantic-dag` invocation
does not by itself guarantee that the next unrelated question reloads the
skill. The included `UserPromptSubmit` bridge closes that gap. `begin` records
watch mode against the native task binding; later prompts repaint the existing
thread and receive context to use the skill again. The hook is silent for
tasks where watch mode was never enabled.

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

## Optional tool attribution

The viewer does not require hooks. This optional `PreToolUse` hook automatically paints the most recent tool as a badge on the currently active semantic node.

The Cardinal connect flow also installs this quiet bridge. For a manual
development checkout, inspect `~/.codex/hooks.json` and merge this entry
without overwriting unrelated hooks:

```json
{
  "description": "Personal Codex lifecycle hooks.",
  "hooks": {
    "PreToolUse": [
      {
        "matcher": ".*",
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

If the file already has a `PreToolUse` list, append the matcher group. Codex will ask the user to trust newly discovered personal hooks. Do not bypass hook trust.

The hook uses the `session_id` in Codex's event payload to find the right DAG, so concurrent Codex tasks in the same working directory remain isolated.
