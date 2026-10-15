---
name: cardinal-supervise
description: Supervise another agent session through its Cardinal Investigation — watch everything it does (its checkpoints and its captured tool activity), validate every finding, question every hypothesis, challenge what is wrong or unreported, and review its artifacts when it is done, posting verdicts the owner sees on the Storyboard under a Supervisor tag. Use only when the user asks you to supervise, watch, review or check another session (by session id, inv_ id or Storyboard link); not for reviewing your own work.
---

# supervise — watch another session, check it independently

Follow [CORE.md](CORE.md), co-located in this directory, for the whole workflow: set up,
the review loop, verdicts, rulings, the artifact review and the bounds. This file adds what
is specific to Codex.

## Commands and state

- Run the commands from this installed plugin (not similarly named files in the working
  repository): `python3 -I <this-skill-directory>/../../scripts/cardinal-storyboard …` and
  `python3 -I <this-skill-directory>/../../scripts/cardinal-evidence …`, each on its own.
- These commands reach the network, and the grant setup writes a private state directory.
  Under workspace-write with network off they need the user's approval; ask once, for the
  supervision run, and say why.
- `<dir>` (CORE.md) is a private directory outside the repository, by default
  `~/.cardinal/supervise/<inv>/` (`mkdir -p -m 700`). Never put the grant file in the
  repository.

## Waking

Codex has no background listener. Each pass, block on the watch until there is something
to review:

```
python3 -I <this-skill-directory>/../../scripts/cardinal-storyboard investigation events <inv> --credential-file <dir>/grant.json --cursor-file <dir>/cursor.json --follow --timeout 600 --max 1 --session <your_sid>
```

Starting from your cursor file, the watch signals only events you have not committed, so
re-arming it never replays the history; it never commits anything itself.

It returns as soon as new events arrive (one line each, your own posts excluded) or after
ten quiet minutes. Then run one pass of CORE.md's review loop through your cursor file,
sweeping the worker's new activity even when the watch timed out, and start the watch
again. Never write sleep-and-reread loops.

## Owner decisions

When the owner types `decide: …`, `approve <id>` or `reject <id>`, Cardinal's prompt hook
shows them the outcome and gives you the same line. Confirm it in one line; run nothing to
send or approve it. No Cardinal line means nothing was sent; say so.

## Fresh-context passes (step backs)

Run each pass (CORE.md, Step backs) as a separate read-only Codex on the copy `budget pass`
wrote:

```
codex exec --ignore-user-config --ignore-rules --disable hooks --disable apps --disable plugins --sandbox read-only --ephemeral -C <code checkout> - < <the copy>
```

Keep every flag: the sandbox covers only shell commands; your config's MCP servers, apps and
hooks would give the pass write tools and Cardinal context. Its final message is the answer.
It needs network: ask once, with the Cardinal commands. Run the two passes one after the
other. If one fails, tell the owner the step back needs a Claude supervisor; for a plan, answer
the estimator copy yourself.

## Doing your own checks

Reproduce claims read-only: read files, `git log` / `git diff`, Cardinal query tools
(their witnessed `rcpt_…` receipts can be cited). Run builds or tests only in your own
worktree, never in the worker's checkout, and only the targeted test.
