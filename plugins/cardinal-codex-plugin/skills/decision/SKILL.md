---
name: cardinal-decision
description: Show Cardinal decision-capture status for Codex and tell the user how to turn it on or off.
---

# Cardinal Decision Capture

Use this skill when the user asks to turn Cardinal decision capture on or off, or asks whether it is on.

Run:

```bash
python3 scripts/cardinal-decision status
```

Surface the output. Do not run `on` or `off` yourself: they write `~/.codex/cardinal`, which the Codex sandbox blocks. Instead give the user the exact terminal command from the status output (`python3 <path>/cardinal-decision on` or `off`) to run in their own terminal. If the status says the recording hook is not registered, tell the user to run `cardinal-connect --repair-hooks` and restart Codex.

When capture is on, the Cardinal telemetry hook adds instructions to each prompt with the exact `record` command (including `--session`) to run whenever a material decision is made. Follow those instructions; never add `--emit` inside Codex.
