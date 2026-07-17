---
name: cardinal-optimize-toolkit
description: Mine this engineer's own session telemetry for capability-fit recommendations (extract a new sub-agent, pin/downgrade a model tier, adopt or swap an existing capability, consolidate duplicates) and, on confirmation, write the accepted artifact into the working tree. Only run this when the user explicitly asks to optimize their toolkit, review capability-fit recommendations, or names this skill directly — do not trigger it opportunistically off a loose topical match.
---

# Cardinal Optimize Toolkit

Use this skill when the user explicitly asks Codex to optimize their
toolkit, mine their own session history for capability-fit
recommendations, or review whether a recurring inline pattern should
become a reusable sub-agent. It is **not** a skill to reach for
opportunistically just because the conversation is adjacent to agents
or tooling — see **What not to do** below for why.

A **per-user, past-informed, future-effective** optimizer — not a
session-local one. It looks at your own last 30 days of sessions,
picks the top few recurring inline-work patterns worth acting on,
authors the artifact itself grounded in your actual toolkit, and — only
on your explicit confirmation — writes the accepted artifact to this
working tree. **Anything written takes effect next session**, not this
one: Codex loads custom agent definitions (`.codex/agents/*.toml`) at
startup, the same as `~/.codex/config.toml` and `~/.codex/hooks.json` —
there is no live-registration channel, so accepting a candidate here
will not change what happens for the rest of this conversation.

This burns your own session tokens to run. The server (conductor's
maestro, via the `cardinal` MCP server) provides evidence — clusters,
model mix, toolkit adoption, tier pricing, priced-savings math. **You
(Codex) do the authorship** from first principles: pick the
recommendation kind by reasoning about the evidence, then write the
`.codex/agents/<name>.toml` artifact grounded in this user's real
capabilities, real tool signatures, real cluster labels. There is no
server-side artifact template — the artifact is composed here, per
invocation, and passed back to the ledger verbatim on `mark`.

## Before you start

This skill orchestrates six `outcomes__*` tools served by the
`cardinal` MCP server (see the `cardinal-connect` skill — same server,
same consent). They are documented in conductor's
`docs/specs/optimize-toolkit-mcp-tools.md`:

1. `outcomes__my_turn_pattern`
2. `outcomes__my_toolkit_adoption`
3. `outcomes__cluster_spawns`
4. `outcomes__org_offered_tiers`
5. `outcomes__estimate_savings`
6. `outcomes__mark`

`outcomes__my_recent_spawns` also exists on the same server — it's a
raw spawn history for ad-hoc debugging, not part of this flow. Reach
for it only if the user explicitly asks "what did I spawn recently?".

**Check your available tools before doing anything else.** If none of
the `outcomes__*` tools appear in your tool list (they'd show under
the `cardinal` MCP server, alongside whatever other integrations this
org has configured), say so plainly and stop:

> "The optimize-toolkit tools aren't wired into this org's Cardinal MCP
> server yet — that's a known rollout gap, not a problem with your
> connection. Nothing to do here yet."

Do not attempt a substitute analysis (no reading transcripts, no
grepping local logs, no falling back to "let me look at your repo
instead"). MCP unreachable or empty candidates → one line, stop, no
retries.

## How you (Codex) should run this

Budget: usually 5–7 tool calls total, ≤10 in the worst case. You'll
also read a handful of files from the working tree to ground kind
picking and authorship — that's fine, keep it targeted.

### 1. Open — situational-awareness bundle (2 calls)

Call `outcomes__my_turn_pattern` and `outcomes__my_toolkit_adoption`,
both with `window: "30d"`. From the two responses, compose a short
(≤6 line) opening paragraph before mentioning any candidate:

- State the evidence window explicitly ("based on your last 30 days of
  sessions") — never let the opening imply "this session."
- Name the caller's top 1–2 model-mix rows by cost from
  `my_turn_pattern.models`.
- Name the toolkit-adoption headline the first candidate will target,
  e.g. "42 exec/apply-patch spawns / 480k tokens on a reasoning-tier
  model in the last 30 days" (from `my_toolkit_adoption.agents` or
  `.skills`).
- **No usable evidence, stop before any ratio math.** If
  `my_toolkit_adoption.coverage.sessions_scanned == 0`, or
  `my_turn_pattern.turns_total` is 0, there is nothing to compute a
  coverage ratio from — say "no optimization candidates right now,
  not enough session history yet" and stop.
- **Coverage caveat.** Use `my_toolkit_adoption.coverage.sessions_with_tier_attribution
  / coverage.sessions_scanned` as the enrichment-coverage proxy. When
  that ratio is under 0.5, prepend a one-line caveat: "evidence from
  N/M of your sessions in the last 30 days — coverage will climb as
  you keep working on a current plugin version." Also check
  `my_turn_pattern.coverage.plugin_versions_seen` — if it shows a
  stale version for recent turns, mention the plugin-version-drift
  possibility and suggest a Codex restart.

### 2. Discover (1 call)

Call `outcomes__cluster_spawns` with `window: "30d"` (defaults:
`min_jaccard: 0.4`, `min_cluster_size: 3` — leave as-is unless the
conversation gives you a reason to tune them). Rank clusters by
`total_tokens` (cost isn't per-cluster available; use tokens as the
size proxy) and recurrence; drop anything with `recurrence < 3` even
if it slipped through, and prefer clusters with a higher
`with_description_share` in `coverage` (clustering is weaker without
descriptions).

Take the top **K = 3** clusters forward. If fewer than 3 clear
clusters exist, present fewer — do not pad with weak candidates to
hit the number.

### 3. Ground yourself in the user's real toolkit (0–2 file reads per candidate)

Before picking a kind for a cluster, look at what the user actually
has. `my_toolkit_adoption` lists capabilities by name and usage, but
"a name in a usage map" is not the same as "a file on disk that will
still be there next session." For each of the top-K clusters:

- If `my_toolkit_adoption` shows an agent/skill whose name looks like
  it might cover this cluster's `representative_label` +
  `tool_signature`, read the matching file under `.codex/agents/` (or
  `~/.codex/agents/` for user-scoped agents) to confirm the fit before
  recommending `adopt`. Bad `adopt` recommendations happen when the
  name matches but the actual scope doesn't.
- If you're considering `extract` (mint new), grep under
  `.codex/agents/` for the cluster's dominant tools in the
  `tool_signature` (as they'd appear in `developer_instructions` or
  `mcp_servers`) — a similar-shape agent may already exist under a
  name your `my_toolkit_adoption` scan didn't surface.
- If you're considering `pin`/`downgrade`, locate the existing agent
  TOML file so you can propose a **minimal edit** (change one `model`
  field) rather than overwriting the whole file.

Keep this bounded — one or two focused reads per candidate, not a
full-repo sweep. If the file isn't obviously there, that's
information ("adopt target's name matched but the file isn't under
`.codex/agents/` — treat as evidence to soften the adopt pitch, or
reframe as `gap`").

### 4. Pick the kind, from first principles

Call `outcomes__org_offered_tiers` once — this tells you the org's
actual `cheap` and `reasoning` model tiers. **Never suggest a model
the org isn't offered**; if a tier is `null`, that door is closed for
this org.

For each of the top-K clusters, reason about kind from the evidence
in front of you. There is no server-side kind gate — you pick, you
justify. The five buildable kinds and one signal-only kind:

- **`adopt`** — the cluster's `tool_signature` and label overlap an
  existing capability you saw in step 3. Recommend the user reach
  for the existing thing consistently, no new file. Softer signal:
  the user's own `my_toolkit_adoption` shows the target with
  meaningful usage already (they know it exists; the miss is
  inconsistency, not awareness).
- **`swap`** — cluster overlaps an existing capability, but that
  existing capability is the wrong shape or wrong model for this
  work. Recommend replacing it. Harder to justify than `adopt`;
  state the reason ("existing agent is pinned to a reasoning tier
  but the tool_signature is mechanical — swap it for a cheap-tier
  variant").
- **`pin`** — cluster runs on a mix of tiers with the org's cheap
  tier already carrying meaningful share (say, ≥30% of the cluster's
  `subagent_model` occurrences) and no evidence the reasoning tier
  is load-bearing. Recommend pinning the existing capability to the
  cheap tier. Requires you to have located an existing file to
  edit.
- **`downgrade`** — cluster runs predominantly on the reasoning tier
  (say, ≥50% share) but the `tool_signature` looks mechanical
  (`jaccard_within` ≥ 0.6, small tool set, no reasoning-heavy tools
  like WebSearch/deep-analysis). Recommend re-tiering the existing
  capability down. Same file-locate requirement as `pin`.
- **`extract`** — a recurring inline pattern with no existing named
  capability to point at. Mint a new sub-agent. This is the one
  kind where you author a full new `.codex/agents/<name>.toml` file
  from scratch. Requires you to name the capability and derive its
  `developer_instructions` from the cluster's `tool_signature` and
  representative work.
- **`consolidate`** — two clusters look like the same underlying job
  under two different labels (or two existing capabilities do). No
  new file; recommend merging. Present conversationally, don't
  auto-locate the files.
- **`gap`** (signal only, no artifact) — cluster is real recurring
  work, but you cannot pick a kind honestly (no adopt target,
  extract would be premature, `tool_signature` too diffuse to
  justify a minted agent). Say so plainly, no artifact, no
  confirmation question, no `estimate_savings` call for this
  candidate.

Thresholds above (30% cheap share, 50% reasoning share, ≥0.6
jaccard_within) are rules of thumb, not gates. Adjust when the
cluster's specifics clearly warrant.

### 5. Estimate savings (1 call per non-`gap` candidate)

For each candidate that isn't `gap`, call
`outcomes__estimate_savings` with the cluster's `total_tokens`,
per-component tokens if you can derive them from members' fields,
`current_model` (the dominant `subagent_model` across cluster
members), and `target_tier` (`"cheap"` for `pin`/`downgrade`/
`extract`; `"cheap"` also for `adopt`/`swap` if the pointed-to
capability runs cheaper). Read
`estimate.assumptions.placeholder_output_ratio` /
`placeholder_cache_ratio` and `estimate.estimate` on every response —
see **Placeholder savings, honestly** below before you say a dollar
figure out loud.

### 6. Author + Present (no tool call; you write the artifact)

For each candidate, author the artifact yourself from first
principles before asking for confirmation. The user will see the
full artifact, not a description of one. Codex's custom-agent format
is TOML — `.codex/agents/<name>.toml` — with fields `name`,
`description`, `developer_instructions`, and optionally `model`,
`model_reasoning_effort`, `sandbox_mode`, `mcp_servers`. Per kind:

**`extract` — new file at `.codex/agents/<name>.toml`.** You author:

- `name`: kebab-case, `^[a-z][a-z0-9-]*$`, derived from the
  cluster's `representative_label`. Short enough to type. If a
  same-name file already exists in `.codex/agents/`, disambiguate
  rather than overwrite.
- `description`: one sentence stating what work this handles and
  when Codex should delegate to it. Ground the sentence in the
  cluster's actual observed work — reference the tool_signature's
  dominant tools and the label's phrasing. Not a template.
- `developer_instructions`: a short (≤10 line) system prompt
  describing the role, grounded in this cluster's specifics — what
  request shape the subagent handles, what to report back, when to
  escalate. Wrap the value as a TOML literal triple-quoted string
  (`'''…'''`); fall back to a basic triple-quoted string
  (`"""…"""`) with `\`/`"` escaping if the body itself contains
  `'''`. If the body contains both `'''` and `"""`, stop and
  surface the collision to the user — don't silently mangle. Never
  mix flavors inside one field.
- `model`: the `target_model_id` from `org_offered_tiers` for the
  tier you picked in step 5.
- Leave `model_reasoning_effort`, `sandbox_mode`, `mcp_servers`,
  `nickname_candidates` **unset** unless the user asks for a value.
  Codex has no per-tool allowlist field equivalent to Claude's
  `tools:` — if tool restrictions matter, express them inline in
  `developer_instructions`, and say so at present time: "codex's
  custom-agent format doesn't have a per-tool allowlist; if
  restrictions matter I've noted them in the instructions body."

**`pin`/`downgrade` — edit an existing file at `.codex/agents/<name>.toml`.**
You already located the file in step 3. The meaningful change is
one TOML field: `model = "<target_model_id>"`. Author the dry-run
as a one-field edit, not a full-file replacement. If you couldn't
locate the existing file, say so and reframe as a conversational
nudge ("your `<capability>` agent shows heavy reasoning-tier usage;
consider pinning it to cheap — I couldn't find its file to edit;
where should this go?").

**`adopt` — usually no file.** Author a plain-language
recommendation: "stop spawning this inline pattern; your existing
`<capability>` already handles it — I saw N sessions where it would
have applied." No confirmation-to-write question; the actionable
part is the user's future behavior, not a diff. If the user asks
for a durable reminder, offer to write a short note somewhere
they'd see it — only on explicit ask.

**`swap` — edit or replace an existing file.** Author the dry-run
as the specific edit you'd propose: which file, which field(s)
change, what the new content is. Do not mint a brand-new file
under `swap` without the user explicitly asking for one.

**`consolidate` — no automated file work.** Present the
two-candidate overlap and ask the user which capability should
absorb the other. Do not attempt auto-locate or auto-merge.

**`gap` — no artifact.** State the pattern, name it as a signal.
Call `mark` with `status: "presented"` and `proposed_kind: "gap"`
for the ledger's sake, but skip the write/confirmation loop.

**Present** the authored artifact (or the plain-language
recommendation) with:

- The evidence summary in plain language.
- The `matching_sessions` slice from the cluster if present,
  referenced inline ("this would have covered your session on Jul 6").
- The savings figure, honestly caveated per placeholder rules.
- A plain confirmation question ("write this to
  `.codex/agents/<name>.toml`? yes/no"). One candidate at a time —
  don't stack.

### 7. Write (only on explicit confirmation)

**Render-target doc citation.** The `.codex/agents/<name>.toml`
render target follows the Codex CLI custom-agents contract
documented at https://developers.openai.com/codex/subagents (pinned
as of 2026-07-16). If Codex CLI changes this contract (path, file
extension, discovery mechanism, or field shape), this skill's
render target needs updating in lock-step; re-pin the citation date
when that happens.

**Validate the target-file basename before anything else.** Names
are used as path segments — reject if the name (a) contains `/` or
`\`, (b) contains `..`, or (c) doesn't match kebab-case
`^[a-z][a-z0-9-]*$`. On rejection, surface the value verbatim to
the user with the specific reason and stop.

Only write after an explicit "yes"-shaped answer in the
conversation. No write on silence, hedging, or topic change. After
writing, tell the user this **takes effect next session** (Codex
loads `.codex/agents/*.toml` at startup; there is no
live-registration channel — same restart requirement as the
`cardinal-connect`/`cardinal-disconnect` skills already ask for
when they touch `~/.codex/config.toml` or `~/.codex/hooks.json`) —
never imply the current conversation just changed. Consent,
revert, and distribution are git: the artifact lands in the
working tree like any other change, reviewed in the diff, reverted
with `git checkout --`, shared via the repo.

### 8. Mark (1 call per candidate you presented)

**Exactly one `mark` call per candidate you showed, carrying its
terminal status and — when there is one — the artifact body you
authored.** Not one call per state transition. Pick from:

- `status: "accepted"` — confirmed and written.
- `status: "dismissed"` — **explicit refusal only** ("no," "don't
  want this"). Ask one short follow-up ("what didn't fit?") and
  forward the answer verbatim as `reason` (cap ~200 chars).
- `status: "presented"` — shown, no decision either way ("not
  now," topic change, session ends without an answer). **Never
  auto-dismiss on non-confirmation.**

Use `action: { kind: "cluster", cluster_id, proposed_kind }`. When
the candidate is `extract`/`pin`/`downgrade`/`swap`/`consolidate`
and you authored an artifact, pass `agent_spec_md` (your authored
TOML body, verbatim — the field name is `agent_spec_md` regardless
of adapter format) and `est_savings_low_usd` /
`est_savings_high_usd` (from `estimate_savings`) so the ledger row
isn't lossy. For `adopt` with no file written, and for `gap`, omit
those fields.

Mark is best-effort: if the call fails, don't error the
conversation over it — the artifact write (or its absence) is the
real outcome; the ledger is measurement, not the source of truth.

## Failure handling for non-`mark` tools

`mark` is the one tool that follows the silent-log rule above —
every other tool is on the hard-stop rule. If any of
`my_turn_pattern`, `my_toolkit_adoption`, `cluster_spawns`,
`org_offered_tiers`, or `estimate_savings` returns `503`
(lakerunner-not-configured), `400` (invalid body), or an empty
result set where the flow depends on at least one row, **surface
the error verbatim to the user, stop the flow, do not retry**. An
empty `cluster_spawns` result means "no clusters cleared the
recurrence floor — nothing to pitch," not "try again with looser
thresholds."

## Placeholder savings, honestly

Every `outcomes__estimate_savings` response carries fields that
exist specifically so you don't overstate a number:

- `estimate: "no_cohort_catalog_only"` means there's no cohort of
  other engineers/orgs to compare against yet — the figure is
  **catalog pricing math only**, not validated against how the
  tier actually performs for this kind of work. Say this out
  loud: "this is a catalog-only estimate — treat it as a ceiling,
  not a promise." Do not drop the caveat just because the number
  is attractive.
- `assumptions.placeholder_output_ratio` /
  `placeholder_cache_ratio` set to `true` mean the estimate fell
  back to typical ratios. Say "estimated within a wide band"
  rather than quoting a bare point figure.
- When `current_cost_usd` is `null` (current model unpriced),
  don't imply a before/after delta — state the projected cost
  alone.

## What not to do

- Don't spawn a sub-agent to do independent investigation — you
  already have the evidence you need from the six tools plus a
  couple of targeted local file reads.
- Don't invent a cohort comparison when a tool response says
  there isn't one.
- Don't write anything without an explicit "yes" in this
  conversation.
- Don't auto-invoke yourself opportunistically. Codex has no
  `disable-model-invocation` frontmatter gate the way the claude
  adapter does — there is no hard mechanism here, only this prose
  rule and a narrowly-scoped `description` field. Treat it as
  load-bearing anyway: only run this flow when the user
  explicitly asks to optimize their toolkit, review capability-
  fit recommendations, or names `cardinal-optimize-toolkit`
  directly.
- Don't paper over a bad pick with a plausibly-worded artifact.
  Authoring from first principles means the artifact should read
  as specific to the cluster's actual pattern. If you find
  yourself writing template-shaped prose, that's a signal the
  evidence isn't strong enough to recommend — reclassify as
  `gap` and say so.
- Don't exceed the ~10-call budget on `outcomes__*` tools; if
  you're reaching for more, stop and say the pipeline needs more
  than a thin skill can responsibly do here.
