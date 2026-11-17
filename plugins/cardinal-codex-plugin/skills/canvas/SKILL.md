---
name: cardinal-canvas
description: Draw and preview the Canvas of a Cardinal Investigation Storyboard scene — free-form HTML/CSS/SVG/d3/Plot in Cardinal's sandboxed frame, with the five cv operations (cv.data, cv.mark, cv.embed, cv.reveal, cv.highlight) and the in-frame prefabs (timeline, trace-waterfall, world-graph, compare, diff) — then read the PNGs the plugin renders locally with Chromium after every storyboard__preview, and critique every reveal step. Use whenever you write or revise a storyboard surface (storyboard__define_surface), pick a prefab for a scene, style or animate a scene, or need to see what a storyboard scene actually looks like before storyboard__publish. Pairs with the cardinal-storyboard skill, which owns the argument; this one owns the visual.
---

# canvas — draw the scene, then look at it

Follow [CORE.md](CORE.md), co-located in this directory, for the Canvas workflow: fetch the
`canvas` guide first, the exemplars, and the preview-then-critique loop. This file adds what
is specific to Codex.

## The hook renders for you

After `storyboard__preview` returns, the Cardinal plugin's PostToolUse hook (the
`StoryboardPreview` entry `cardinal-connect` registers, up to 150 s) passes the result to
the storyboard skill's renderer, which renders every scene and reveal step in your local
Chrome/Chromium. The hook then reports in your context the PNG directory
(`~/.codex/cardinal/storyboards/<storyboard_id>/r<revision>/`), each scene's files
(`<scene>-<step>.png`, step 0 first) and any per-scene render error, then the cover and
the link-preview mock when the preview carried `card`. You do not build the renderer's
input. Open every PNG it names (view the image file), as CORE.md says.

The hook runs only when `~/.codex/hooks.json` has that entry and Codex trusts it: after a
plugin upgrade, `scripts/cardinal-connect --repair-hooks` adds it, and the TUI asks you to
review and trust the changed hooks.

## Rendering by hand (fallback)

Render yourself when no hook output names the PNGs (the entry is missing or untrusted, the
hook timed out or left scenes unrendered), for the dark theme, or to re-render a subset.
Use `render_preview.py` in the installed **cardinal-storyboard** skill's `scripts/`
directory, next to this skill's directory, not a similarly named file in the working
repository. Save the JSON `storyboard__preview` returned to a local file, unchanged, and
run:

```sh
python3 -I <this-skill-directory>/../storyboard/scripts/render_preview.py --runtime codex --from-json <preview.json> [--scene <id>]… [--theme dark]
```

`--runtime codex` uses this Codex's Cardinal connection and writes under
`~/.codex/cardinal/storyboards/`. Keep the `-I`: without it Python imports modules from the
working directory first. The renderer fetches each scene's page from Cardinal, so inside a
sandbox without network access the command needs the user's approval; give it a long
timeout (it stops itself at 9 minutes). If Chrome/Chromium is unavailable, say the preview
could not be verified.
