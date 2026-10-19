#!/usr/bin/env python3
"""Emit Cardinal agent-session telemetry from Codex hooks.

Codex does not expose Claude Code's native OTel emitter, so this hook reads
Codex hook payloads and local session JSONL transcripts, normalizes them into
the existing Cardinal/Lakerunner event contract, and POSTs OTLP/HTTP logs.
Failures are best-effort and silent: telemetry must not break the agent loop.

Shared algorithms and the OTLP contract live in cardinal_core (vendored next
to this file by build/vendor.py). What stays here is Codex-specific:
transcript scraping with resume-line semantics, tool-name normalization,
token-event assembly from token_count records, and payload spellings.
"""

from __future__ import annotations

import time

# Before any other import: the prompt and Stop deadlines are measured from here.
START = time.monotonic()

import argparse  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import shlex  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _codex_decisions  # noqa: E402
import _plugin_version  # noqa: E402
from cardinal_core import bashclass, decisions, initiative, limits, otlp, pricing  # noqa: E402
from cardinal_core import session as core_session  # noqa: E402
from cardinal_core.paths import AgentPaths, read_json  # noqa: E402


PLUGIN_VERSION = _plugin_version.plugin_version()
HOOK_TIMEOUT_SEC = 2.0
MAX_EVENTS_PER_STOP = 512
# UserPromptSubmit is synchronous in Codex and its stdout is discarded if
# the hook times out (hooks.json gives it 5s), so the prompt path never
# waits on `gh`: it reads the PR cache and refreshes it in a detached child.
PR_REFRESH_TIMEOUT_SEC = 10.0
# PostToolUse decision recording does only local work synchronously (argv
# parse, gate, validation, id, ledger write, reply; PR from cache). D18
# clusters and the OTLP send run in ONE detached child fed by a 0600 spool
# file, so a stalled ingest or DNS can never hold the tool result or push
# the hook past its timeout (which would discard the reply).
REFRESH_PR_EVENT = "_RefreshPr"
BACKGROUND_EVENT = "_Background"
# Tests only: run the spooled job in-process for deterministic assertions.
BACKGROUND_INLINE_ENV = "CARDINAL_CODEX_BACKGROUND_INLINE"
# The launcher execs this file from the connected plugin root (or newest
# cache), so the sibling scripts/ dir is the install the agent should call.
DECISION_CLI = Path(__file__).resolve().parent.parent / "scripts" / "cardinal-decision"

# P5 capture affordance (subagent-telemetry-enrichment field 4, step 1):
# Codex's SubagentStop payload shape has never been observed in the wild,
# so an env-gated raw-payload dump is how we finally capture one. Off by
# default; writes nothing unless CARDINAL_CODEX_DEBUG_PAYLOADS=1.
DEBUG_PAYLOADS_ENV = "CARDINAL_CODEX_DEBUG_PAYLOADS"

# UserPromptSubmit has a 5 s timeout and Codex discards a timed-out hook's
# stdout (the limits gate's verdict with it). Every synchronous network call
# on the prompt path (the owner-input capture) ends by START +
# PROMPT_DEADLINE_S, the hook's one JSON object is written then, and the
# git_state telemetry after it is bounded by START + PROMPT_EXIT_S.
PROMPT_DEADLINE_S = 3.5
PROMPT_EXIT_S = 4.5
# Stop (10 s): the turn end's synchronous check and owner-input flush end by
# START + STOP_DEADLINE_S; the transcript telemetry follows.
STOP_DEADLINE_S = 5.0
# PostToolUse StoryboardAuthoring (10 s): edit lookups end by START + this.
EDIT_DEADLINE_S = 4.0
# The prompt's storyboard re-look runs in a detached child (never on the
# prompt's clock); what it finds is staged for the next boundary.
RELOOK_EVENT = "_Relook"
RELOOK_BUDGET_S = 4.0

TARGET_KEYS = {
    "Read": "file_path",
    "Edit": "file_path",
    "Write": "file_path",
    "NotebookEdit": "notebook_path",
}

EXIT_CODE_RE = re.compile(r"Process exited with code (-?\d+)")
CUSTOM_EXEC_CALL_RE = re.compile(r"tools\.([A-Za-z_][A-Za-z0-9_]*)\s*\(")


def codex_paths() -> AgentPaths:
    return AgentPaths(home=Path.home() / ".codex")


def silent_exit() -> None:
    sys.exit(0)


def hard_exit() -> None:
    """Flush and exit at once: a request thread abandoned at its deadline
    (owner-input capture, an edit lookup, the turn end's check) must not
    hold the hook open past Codex's timeout, which would discard stdout."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass
    os._exit(0)


def session_id_from_payload(payload: dict[str, Any]) -> str | None:
    for key in ("session_id", "sessionId", "sessionID"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    for key in ("CODEX_SESSION_ID", "OPENAI_CODEX_SESSION_ID"):
        value = os.environ.get(key)
        if value:
            return value
    return None


def transcript_path_from_payload(payload: dict[str, Any], session_id: str) -> Path | None:
    for key in ("transcript_path", "transcriptPath", "session_path", "sessionPath"):
        value = payload.get(key)
        if isinstance(value, str) and value.endswith(".jsonl"):
            path = Path(value).expanduser()
            if path.exists():
                return path

    sessions_dir = codex_paths().home / "sessions"
    if not sessions_dir.exists():
        return None
    matches = list(sessions_dir.rglob(f"*{session_id}.jsonl"))
    if not matches:
        return None
    matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return matches[0]


def emit_records(records: list[dict[str, Any]], timeout: float = HOOK_TIMEOUT_SEC) -> None:
    if not records:
        return
    paths = codex_paths()
    conn = otlp.connection_from_paths(paths)
    if conn is None:
        return
    state = paths.read_state()
    resource = otlp.resource_attrs(
        service_name="codex",
        agent_runtime="codex",
        deployment_environment=state.get("deployment_environment"),
        user_email=state.get("user_email"),
        org=state.get("org_slug") or state.get("org_id"),
        plugin_version=PLUGIN_VERSION,
    )
    otlp.emit_records(
        records,
        conn,
        resource,
        scope_name="cardinal-codex-plugin",
        scope_version=PLUGIN_VERSION,
        timeout=timeout,
    )


def dump_debug_payload(event: str, payload: dict[str, Any]) -> None:
    """Env-gated raw hook-payload dump for shape capture. A no-op unless
    CARDINAL_CODEX_DEBUG_PAYLOADS=1; best-effort like everything else."""
    if os.environ.get(DEBUG_PAYLOADS_ENV) != "1":
        return
    try:
        debug_dir = codex_paths().debug_dir
        debug_dir.mkdir(parents=True, exist_ok=True)
        path = debug_dir / f"{event}-{time.time_ns()}.json"
        path.write_text(json.dumps(payload, indent=2, default=str) + "\n")
    except (OSError, TypeError, ValueError):
        pass


def build_decision_context(cli: str, session_id: str, entries: list[dict[str, Any]]) -> str:
    """Decision-capture instructions + session ledger (Claude parity:
    adapters/claude/hooks/decision-prompt.py), with Codex's invocation."""
    command = f"python3 {shlex.quote(cli)} record --session {shlex.quote(session_id)}"
    return (
        "Cardinal decision capture is on for this session. When you make a choice that "
        "constrains later work (picking between approaches, settling an open question, or "
        "the user deciding something), record it right away with one shell command:\n"
        f"{command} --choice '<the option chosen, 2-7 words>' "
        "--question '<what had to be settled>' --why '<one sentence>' "
        "[--alt '<rejected option>']... [--by user] [--anchor <path>[::Symbol]]... "
        "[--follows|--refines|--supersedes <id>]\n"
        "Wrap every value in single quotes so the shell doesn't expand $ or backticks; "
        "if the hook sees different text than the command printed, it refuses to record.\n"
        "Record choices, not progress, findings, or tool calls. Use --by user when the user "
        "made the call. Anchor the files or symbols the decision governs. Link a decision to "
        "an earlier one when it builds on, narrows, or replaces it.\n"
        "Decisions so far this session:\n"
        f"{decisions.render_ledger(entries)}"
    )


def decision_context(paths: AgentPaths, session_id: str) -> str | None:
    """Context to inject when decision capture is on, else None. File
    reads only — safe on the synchronous prompt path."""
    if not decisions.is_enabled(paths.runtime_dir, os.environ.get(decisions.ENABLE_ENV)):
        return None
    # The PostToolUse handler is the only emitter. Installs connected
    # before it existed lack that hook; asking them to record would be a
    # flow that never fires (cardinal-decision status says how to repair).
    if not _codex_decisions.post_tool_use_registered(paths):
        return None
    entries = decisions.read_ledger(paths.runtime_dir, session_id)
    return build_decision_context(str(DECISION_CLI), session_id, entries)


def merge_prompt_output(gate_out: dict[str, Any] | None, context: str | None,
                        system_message: str | None = None) -> dict[str, Any] | None:
    """Codex parses a hook's stdout as ONE JSON object, so the spend gate,
    the decision / investigation context and the owner-input disclosure
    (a user-visible systemMessage) share it. A block verdict wins outright."""
    out = dict(gate_out or {})
    if out.get("decision") == "block":
        return out
    if context:
        specific = dict(out.get("hookSpecificOutput") or {"hookEventName": "UserPromptSubmit"})
        existing = specific.get("additionalContext")
        specific["additionalContext"] = f"{existing}\n\n{context}" if existing else context
        out["hookSpecificOutput"] = specific
    if system_message:
        existing = out.get("systemMessage")
        out["systemMessage"] = f"{existing}\n\n{system_message}" if existing else system_message
    return out or None


def cached_pr(
    paths: AgentPaths, repo: str | None, branch: str | None, now: float | None = None,
) -> tuple[int | None, str | None, bool]:
    """(number, url, fresh) from the cache decisions.resolve_pr maintains,
    without running `gh`. fresh=True also when there is nothing to
    resolve (no repo/branch, detached HEAD, protected branch)."""
    if not repo or not branch or branch == "HEAD" or branch in initiative.PROTECTED_BRANCHES:
        return None, None, True
    cache = read_json(decisions.cache_dir(paths.runtime_dir) / "prs.json")
    entry = cache.get(f"{repo}#{branch}")
    if not isinstance(entry, dict):
        return None, None, False
    number = entry.get("number")
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        number = None
    url = entry.get("url") if number and isinstance(entry.get("url"), str) else None
    ttl = decisions.PR_CACHE_TTL_SEC if number else decisions.PR_NEGATIVE_TTL_SEC
    try:
        age = (time.time() if now is None else now) - float(entry.get("at") or 0)
    except (TypeError, ValueError):
        age = float("inf")
    return number, url, age < ttl


def spawn_pr_refresh(cwd: str, repo: str, branch: str) -> None:
    """Detached `gh` refresh. Own session + devnull stdio: Codex waits for
    the hook's stdout to close and only kills the process group on
    timeout (codex-rs/hooks/src/engine/command_runner.rs run_command)."""
    subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--event", REFRESH_PR_EVENT,
         "--refresh", json.dumps({"cwd": cwd, "repo": repo, "branch": branch})],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        close_fds=True, start_new_session=True,
    )


def handle_refresh_pr(raw: str | None) -> None:
    spec = json.loads(raw or "{}")
    if not isinstance(spec, dict):
        return
    decisions.resolve_pr(
        str(spec.get("cwd") or os.getcwd()), spec.get("repo"), spec.get("branch"),
        decisions.cache_dir(codex_paths().runtime_dir), timeout=PR_REFRESH_TIMEOUT_SEC,
    )


def cached_pr_lookup(paths: AgentPaths):
    """PR lookup for the bounded decision path: cache only, detached
    refresh on a miss/stale entry (same as the prompt path)."""

    def lookup(cwd: str, repo: str | None, branch: str | None) -> tuple[int | None, str | None]:
        number, url, fresh = cached_pr(paths, repo, branch)
        if not fresh:
            try:
                spawn_pr_refresh(cwd, repo, branch)
            except Exception:
                pass
        return number, url

    return lookup


def spawn_background(job: dict[str, Any]) -> bool:
    """Spool `job` (0600, O_EXCL) and run it in a detached child with
    /dev/null stdio and its own session (Codex waits for the hook's stdout
    to close and kills the process group only on timeout). Same pattern as
    the Gemini adapter. False when the job could not be queued."""
    try:
        spool_dir = codex_paths().runtime_dir / "spool"
        spool_dir.mkdir(parents=True, exist_ok=True)
        path = spool_dir / f"{job.get('kind')}-{os.getpid()}-{time.time_ns()}.json"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(job, fh, default=str)
    except (OSError, TypeError, ValueError):
        return False
    if os.environ.get(BACKGROUND_INLINE_ENV) == "1":
        run_background_job(path)
        return True
    try:
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--event", BACKGROUND_EVENT,
             "--background", str(path)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True,
        )
    except OSError:
        try:
            path.unlink()
        except OSError:
            pass
        return False
    return True


def run_background_job(path: Path) -> None:
    try:
        job = json.loads(path.read_text())
    except (OSError, ValueError):
        return
    finally:
        try:
            path.unlink()
        except OSError:
            pass
    if isinstance(job, dict) and job.get("kind") == "decisions":
        entries = job.get("entries")
        if isinstance(entries, list) and entries:
            _codex_decisions.emit_entries(codex_paths(), entries, PLUGIN_VERSION)


def record_decisions(payload: dict[str, Any], specs: list[dict[str, Any]],
                     reasons: list[str]) -> list[str]:
    """Synchronous local half; returns the lines reported to the agent."""
    paths = codex_paths()
    if not decisions.is_enabled(paths.runtime_dir, os.environ.get(decisions.ENABLE_ENV)):
        return [_codex_decisions.CAPTURE_OFF_REPORT]
    reports = list(reasons)
    if not specs:
        return reports
    # The session Codex observed — never the marker's self-reported one.
    session_id = session_id_from_payload(payload)
    if not session_id:
        return reports + ["Cardinal did NOT record the decision: the hook payload had no session id."]
    payload_cwd = str(payload.get("cwd") or os.getcwd())
    lookup = cached_pr_lookup(paths)
    connected = otlp.connection_from_paths(paths) is not None
    entries: list[dict[str, Any]] = []
    recorded: list[str] = []
    for spec in specs:
        if not os.path.isdir(spec["cwd"]):
            spec = dict(spec, cwd=payload_cwd)
        try:
            entry = _codex_decisions.record_local(paths, spec, session_id, lookup)
        except decisions.DecisionError as err:
            reports.append(f"Cardinal did NOT record decision {spec['choice']!r}: {err}")
            continue
        except Exception as err:
            reports.append(f"Cardinal did NOT record decision {spec['choice']!r} "
                           f"({type(err).__name__}: {err}).")
            continue
        entries.append(entry)
        recorded.append(_codex_decisions.report_line(entry, connected))
    if entries and connected and not spawn_background({"kind": "decisions", "entries": entries}):
        recorded.append("Warning: the decisions above were saved locally but Cardinal could not "
                        "queue sending them.")
    return recorded + reports


def handle_post_tool_use(payload: dict[str, Any]) -> None:
    """Hook-side decision recording — the only cardinal.decision emitter.
    Silent for every tool call that doesn't run `cardinal-decision record`;
    once one does, always replies (recorded, or NOT recorded and why)."""
    try:
        specs, reasons = _codex_decisions.plan_post_tool_use(payload)
    except Exception as err:
        specs, reasons = [], [f"Cardinal did NOT record the decision ({type(err).__name__}: {err})."]
    if not specs and not reasons:
        return
    try:
        reports = record_decisions(payload, specs, reasons)
    except Exception as err:  # never fail silently once an invocation was seen
        reports = reasons + [f"Cardinal did NOT record the decision ({type(err).__name__}: {err})."]
    if not reports:
        return
    # PostToolUse accepts hookSpecificOutput.additionalContext
    # (codex-rs/hooks/schema/generated/post-tool-use.command.output.schema.json).
    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": "\n\n".join(reports),
        }
    }))
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Evidence capture (PostToolUse, every tool) — the Codex side of the generic
# evidence pipeline (cardinal_core.evidence_capture.capture_call).
# cardinal-connect registers a second PostToolUse group, matcher ".*", that
# runs this file with `--event ToolEvidence`; the "Bash" group above stays
# the decision emitter. Codex names MCP tools mcp__<server>__<tool>
# (normalize_tool_name); Cardinal's own `cardinal` server is skipped, its
# gateway already mints witnessed receipts.
# ---------------------------------------------------------------------------

CARDINAL_MCP_SERVERS = ("cardinal",)
EVIDENCE_CLI = Path(__file__).resolve().parent.parent / "scripts" / "cardinal-evidence"
EVIDENCE_EVENT = "ToolEvidence"


def codex_version() -> str | None:
    """The Codex CLI version, when the environment names it (no process is
    spawned in a hook)."""
    for key in ("CODEX_VERSION", "CODEX_CLI_VERSION"):
        v = os.environ.get(key)
        if isinstance(v, str) and v:
            return v
    return None


def evidence_call(payload: dict[str, Any]):
    """A ToolCall for any PostToolUse payload, or None without a tool
    name. Codex's documented shape (unified_exec PostToolUseRequest):
    {session_id, turn_id, cwd, tool_name, tool_input, tool_response,
    tool_use_id}; tool_response is the command output string for Bash."""
    from cardinal_core import evidence, evidence_capture as cap

    raw_name = payload.get("tool_name") or payload.get("toolName")
    if not isinstance(raw_name, str) or not raw_name:
        return None
    source, tool = cap.classify_mcp_name(raw_name, "codex", CARDINAL_MCP_SERVERS)
    tuid = payload.get("tool_use_id") or payload.get("toolUseId") or payload.get("call_id")
    cwd = payload.get("cwd")
    response = payload.get("tool_response")
    if response is None:
        response = payload.get("toolResponse")
    error = payload.get("error")
    return cap.ToolCall(
        runtime="codex",
        tool_name=raw_name,
        source=source,
        tool=tool,
        tool_input=payload.get("tool_input") if "tool_input" in payload else payload.get("toolInput"),
        response=response,
        error=error if isinstance(error, str) and error.strip() else None,
        session_id=session_id_from_payload(payload),
        tool_use_id=tuid if isinstance(tuid, str) else None,
        cwd=cwd if isinstance(cwd, str) else None,
        client=evidence.client_string("codex", codex_version()),
    )


def handle_tool_evidence(payload: dict[str, Any]) -> None:
    """Record one tool call (any tool) in the local evidence spool and hand
    the agent its `[evidence:ev_...]` id as PostToolUse
    hookSpecificOutput.additionalContext. Local file work only."""
    dump_debug_payload("PostToolUse", payload)
    failure = None
    try:
        from cardinal_core import evidence_capture as cap

        call = evidence_call(payload)
        if call is None:
            return
        from cardinal_core import investigation_failures
        failure = investigation_failures.observe(Path.home(), call.session_id, call.tool_input,
                                                  call.response, call.error, runtime=call.runtime, tool_name=call.tool_name)
        # Never silent: a call the pipeline cannot finish in time is kept as a
        # withheld stub (capture_call_guarded).
        got = cap.capture_call_guarded(call, Path.home(), promote_cmd=shlex.quote(str(EVIDENCE_CLI)))
    except BaseException:
        got = None
    record_patch_edits(payload)
    observe_storyboard_token(payload)
    lines = [line for line in (failure, got.line if got is not None else None) if line]
    try:
        from cardinal_core import investigation_agent
        wiring = storyboard_wiring()
        sid = session_id_from_payload(payload)
        investigation_agent.spawn_poller(wiring, sid, str(INVESTIGATION_POLLER))
        # A subagent's tool call (Codex names it with agent_id; session_id is
        # the parent's) gets nothing: advisories and staged items wait for
        # the main thread.
        investigation_agent.deliver(wiring, sid, lines.append, is_subagent=bool(payload.get("agent_id")))
    except Exception:
        pass
    if not lines:
        return
    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": "\n\n".join(lines),
        }
    }))
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Storyboard associations (cardinal_core.storyboard_agent): the session id +
# discovery block at SessionStart, the files apply_patch edited (recorded by
# the ToolEvidence PostToolUse run above: no new hook process), and context
# stamping on Cardinal's storyboard tools (PreToolUse, `--event
# StoryboardContext`). Payload shapes captured from codex-cli 0.142.5
# (tests/test_codex_storyboard.py fixtures).
# ---------------------------------------------------------------------------

STORYBOARD_CLI = Path(__file__).resolve().parent.parent / "scripts" / "cardinal-storyboard"
INVESTIGATION_POLLER = Path(__file__).resolve().parent / "investigation-poller.py"
# The plugin root the Checks preview template (checks/skill/) ships in.
CHECKS_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
STORYBOARD_CONTEXT_EVENT = "StoryboardContext"
# The four tools stamped_input fills (link's about refs are the model's);
# the same set as cardinal-connect's PreToolUse matcher.
STORYBOARD_TOOL_RE = re.compile(r"^mcp__cardinal__storyboard__(create|add_act|publish|find)$")
# hooks.json's handler for the stamping hook (cardinal-connect
# managed_hook_command("StoryboardContext")).
STORYBOARD_CONTEXT_HOOK_NEEDLE = f"--event {STORYBOARD_CONTEXT_EVENT} # cardinal-codex-plugin"
# Codex applies a PreToolUse updatedInput only together with
# permissionDecision "allow" ("PreToolUse hook returned updatedInput without
# permissionDecision:allow"), and whether that "allow" also skips an approval
# prompt Codex would otherwise show is not verified. So the hook stamps only
# where no prompt exists (bypassPermissions), or everywhere when the user sets
# CARDINAL_STORYBOARD_CONTEXT=always; elsewhere `cardinal-storyboard context`
# (named at SessionStart) is how the context gets there.
STAMP_PERMISSION_MODES = ("bypassPermissions",)
STAMP_ALWAYS_VALUES = ("always", "allow")


def storyboard_wiring():
    from cardinal_core import storyboard_agent

    return storyboard_agent.Wiring("codex", codex_paths(), PLUGIN_VERSION, cli=str(STORYBOARD_CLI))


def stamping_allowed(payload: dict[str, Any]) -> bool:
    value = os.environ.get("CARDINAL_STORYBOARD_CONTEXT")
    if isinstance(value, str) and value.strip().lower() in STAMP_ALWAYS_VALUES:
        return True
    return payload.get("permission_mode") in STAMP_PERMISSION_MODES


def stamping_registered() -> bool:
    """Whether ~/.codex/hooks.json has the PreToolUse StoryboardContext
    handler. The stable launcher runs the newest cached plugin's hook, so
    after an upgrade this code runs before `cardinal-connect --repair-hooks`
    has added the handler."""
    from cardinal_core import storyboard_agent

    return storyboard_agent.hooks_file_registers(Path.home() / ".codex" / "hooks.json", "PreToolUse",
                                                 STORYBOARD_CONTEXT_HOOK_NEEDLE)


def auto_context(payload: dict[str, Any], wiring) -> bool:
    """Whether the PreToolUse hook will fill an absent session_id / context
    in this session: allowed in this permission mode, not turned off with
    CARDINAL_STORYBOARD_CONTEXT=0, and actually registered. Chosen from the
    SessionStart permission_mode; a mid-session /approvals change is not
    seen (the CLI fallback is named in both wordings)."""
    return stamping_allowed(payload) and not wiring.context_disabled() and stamping_registered()


def record_patch_edits(payload: dict[str, Any]) -> None:
    """The files a successful apply_patch named, into storyboard_files
    (`context.paths`). Local only; never raises."""
    try:
        name = payload.get("tool_name") or payload.get("toolName")
        if name not in ("apply_patch", "functions.apply_patch"):
            return
        from cardinal_core import storyboard_agent

        response = payload.get("tool_response")
        if response is None:
            response = payload.get("toolResponse")
        if not storyboard_agent.apply_patch_succeeded(response):
            return
        tool_input = payload.get("tool_input") if "tool_input" in payload else payload.get("toolInput")
        cwd = payload.get("cwd")
        storyboard_agent.record_edits(storyboard_wiring(), session_id_from_payload(payload),
                                      storyboard_agent.apply_patch_paths(tool_input),
                                      cwd if isinstance(cwd, str) else None)
    except Exception:
        pass


def tool_name_of(payload: dict[str, Any]) -> Any:
    return payload.get("tool_name") or payload.get("toolName")


def tool_input_of(payload: dict[str, Any]) -> Any:
    return payload.get("tool_input") if "tool_input" in payload else payload.get("toolInput")


def tool_response_of(payload: dict[str, Any]) -> Any:
    response = payload.get("tool_response")
    return payload.get("toolResponse") if response is None else response


def post_tool_context(text: str) -> None:
    # PostToolUse hookSpecificOutput.additionalContext (codex-rs/hooks/schema/
    # generated/post-tool-use.command.output.schema.json).
    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": text},
    }))
    sys.stdout.flush()


def observe_storyboard_token(payload: dict[str, Any]) -> None:
    """After Cardinal's storyboard__create / add_act / preview: keep the
    result's evidence token for this session (storyboard_authoring.
    observe_cardinal_result: `cardinal-evidence promote` then uploads cited
    evidence without the org key); after a successful storyboard__checkpoint:
    hold this session's evidence while space allows. Every other tool
    returns before the import. Local only; never raises."""
    try:
        name = tool_name_of(payload)
        if not (isinstance(name, str) and name.startswith("mcp__cardinal__storyboard__")):
            return
        from cardinal_core import storyboard_authoring as sa

        sa.observe_cardinal_result(codex_paths(), "codex", name, tool_response_of(payload),
                                   session_id=session_id_from_payload(payload))
    except Exception:
        pass


# Storyboard authoring after Cardinal's tools (cardinal_core.storyboard_authoring,
# Claude's storyboard-preview.py / storyboard-hero.py / storyboard-edit-lookup.py),
# each in its own PostToolUse group so the slow work never shares the
# ToolEvidence group's 3 s: StoryboardPreview (matcher storyboard__preview,
# 150 s: the local Chromium render) and StoryboardAuthoring (publish's hero
# upload, apply_patch's edit lookup; 10 s).
STORYBOARD_PREVIEW_EVENT = "StoryboardPreview"
STORYBOARD_AUTHORING_EVENT = "StoryboardAuthoring"
APPLY_PATCH_TOOLS = ("apply_patch", "functions.apply_patch")


def handle_storyboard_preview(payload: dict[str, Any]) -> None:
    """After storyboard__preview: render every scene locally and hand the
    agent the PNG paths to Read (storyboard_authoring.on_preview, bounded by
    its own budget from START)."""
    from cardinal_core import storyboard_authoring as sa

    name = tool_name_of(payload)
    if not sa.is_cardinal_tool("codex", name, "storyboard__preview"):
        return
    tool_input = tool_input_of(payload)
    ctx = sa.on_preview(codex_paths(), "codex", tool_response_of(payload),
                        session_id=session_id_from_payload(payload),
                        tool_input=tool_input if isinstance(tool_input, dict) else None,
                        environ=os.environ, started=START)
    if ctx:
        post_tool_context(ctx)


def edit_lookups(payload: dict[str, Any], wiring) -> list[str]:
    """The storyboards about the files an apply_patch named, one lookup per
    new directory (storyboard_authoring.edit_lookup dedupes and caps), all
    ending by START + EDIT_DEADLINE_S."""
    from cardinal_core import storyboard_agent, storyboard_authoring as sa

    sid = session_id_from_payload(payload)
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
    deadline = START + EDIT_DEADLINE_S
    blocks: list[str] = []
    for path in storyboard_agent.apply_patch_paths(tool_input_of(payload)):
        if time.monotonic() >= deadline:
            break
        block = sa.edit_lookup(wiring.paths, "codex", sid, path, cwd, connection=wiring.connection,
                               client=wiring.client, environ=wiring.environ, deadline=deadline)
        if block and block not in blocks:
            blocks.append(block)
    return blocks


def handle_storyboard_authoring(payload: dict[str, Any]) -> None:
    """storyboard__publish: upload the hero render (storyboard_authoring.
    on_publish); apply_patch: the storyboards about the edited files
    (edit_lookups). As PostToolUse context."""
    from cardinal_core import storyboard_authoring as sa

    name = tool_name_of(payload)
    wiring = storyboard_wiring()
    lines: list[str] = []
    if sa.is_cardinal_tool("codex", name, "storyboard__publish"):
        message = sa.on_publish(wiring.paths, "codex", tool_response_of(payload), connection=wiring.connection,
                                client=wiring.client, environ=wiring.environ)
        if message:
            lines.append(message)
    elif name in APPLY_PATCH_TOOLS:
        lines.extend(edit_lookups(payload, wiring))
    if lines:
        post_tool_context("\n\n".join(lines))


def handle_storyboard_context(payload: dict[str, Any]) -> None:
    """PreToolUse on mcp__cardinal__storyboard__*: fill an absent
    session_id / context (storyboard_agent.stamped_input). Prints nothing
    (the call runs unchanged) when nothing was added, when stamping is not
    allowed in this permission mode, or on any failure."""
    dump_debug_payload("PreToolUse", payload)
    m = STORYBOARD_TOOL_RE.match(str(payload.get("tool_name") or ""))
    if not m or not stamping_allowed(payload):
        return
    from cardinal_core import storyboard_agent

    out = storyboard_agent.stamped_input(storyboard_wiring(), m.group(1), payload.get("tool_input"),
                                         session_id_from_payload(payload), payload.get("cwd"))
    if out is None:
        return
    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": out,
        }
    }))
    sys.stdout.flush()


SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def is_codex_machine_prompt(prompt: Any) -> bool:
    """A prompt Codex submitted for a slash command (owner_input.MACHINE_PROMPTS["codex"])."""
    from cardinal_core import owner_input

    return owner_input.is_machine_prompt("codex", prompt)


def guidance_marker(session_id: str) -> Path:
    return codex_paths().runtime_dir / "investigation-guidance" / f"{session_id}.sha256"


def note_guidance(session_id: str, text: str) -> bool:
    """Record that the model has `text` (the session guidance) in context.
    True when it differs from what was recorded for this session, i.e. it
    must be sent now. Never raises (cannot tell: True, send it)."""
    if not SESSION_ID_RE.match(session_id or ""):
        return True
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    path = guidance_marker(session_id)
    try:
        if path.read_text().strip() == digest:
            return False
    except (OSError, UnicodeDecodeError):
        pass
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(digest + "\n")
    except OSError:
        pass
    return True


def prompt_guidance(wiring, session_id: str) -> str | None:
    """The session guidance (investigation_agent.describe) when the model
    does not have this text yet: SessionStart sends it at startup, resume,
    clear, fork and after compaction (Codex fires SessionStart with source
    "compact" before the next prompt), and records it (note_guidance). A
    prompt sends it only when it changed since (the poller completed a
    pending bootstrap, the capabilities or the owner-input state changed),
    not on every prompt. Local only."""
    from cardinal_core import investigation_agent, investigation_events as ie

    if not investigation_agent.enabled(wiring) or not ie.valid_session(session_id):
        return None
    binding = ie.read_binding(Path.home(), session_id)
    if not (binding and binding.get("org") == wiring.connection().get("org")
            and binding.get("bootstrap", {}).get("status") == "ok"):
        return None
    text = investigation_agent.describe(wiring, session_id, binding)
    return text if text and note_guidance(session_id, text) else None


def prompt_investigation(payload: dict[str, Any], session_id: str) -> tuple[list[str], str | None]:
    """The prompt's investigation work: ([model-context texts], the
    user-visible message or None).

    Owner input (owner_input.capture, runtime "codex"): what the owner typed
    is recorded to the session's Investigation, network bounded by START +
    PROMPT_DEADLINE_S. Never a subagent's prompt (agent_id), never a prompt
    Codex generated for /review or /init (owner_input.MACHINE_PROMPTS). The first
    prompt that would be recorded is not: the disclosure is returned as the
    user-visible message instead (not yet recorded as shown: the caller
    marks it after flushing it to stdout), and capture starts with the next
    prompt.
    Then (main thread only) the session guidance when the model lacks it,
    and what was staged for this boundary (investigation_agent.prompt)."""
    from cardinal_core import investigation_agent, owner_input

    wiring = storyboard_wiring()
    message = None
    prompt = payload.get("prompt")
    if not is_codex_machine_prompt(prompt):
        cwd = payload.get("cwd")
        got = owner_input.capture(Path.home(), session_id, prompt, runtime="codex", conn=wiring.connection(),
                                  client=wiring.client, environ=wiring.environ, agent_id=payload.get("agent_id"),
                                  deadline=START + PROMPT_DEADLINE_S, cwd=cwd if isinstance(cwd, str) else None,
                                  mark_disclosure=False)
        message = got.disclosure
    if payload.get("agent_id"):
        return [], message
    parts: list[str] = []
    guidance = prompt_guidance(wiring, session_id)
    if guidance:
        parts.append(guidance)
    parts.extend(investigation_agent.prompt(wiring, session_id).agent_context)
    return parts, message


def relay_owner_decision(payload: dict[str, Any], session_id: str) -> tuple[str | None, str | None]:
    """Relayed owner decisions (owner_relay.handle_prompt, runtime "codex"):
    when the owner's whole prompt is `decide: <text>` (their own words, to
    the registered worker), or approves (`approve dp_…`, or bare `approve`
    for the only pending proposal) or rejects a decision this supervisor
    session proposed, relay it to the worker with this machine's key
    (bounded by START + owner_relay.BUDGET) or drop it. (the outcome for the
    user (systemMessage), the terser one for the model (additionalContext)),
    or (None, None).
    Runs whether or not owner input is on; never for a subagent's or a
    Codex-generated prompt. Never raises."""
    try:
        from cardinal_core import owner_relay

        if not owner_relay.wanted(Path.home(), session_id, payload.get("prompt")):
            return None, None
        wiring = storyboard_wiring()
        got = owner_relay.handle_prompt(Path.home(), session_id, payload.get("prompt"), runtime="codex",
                                        conn=wiring.connection(), client=wiring.client,
                                        agent_id=payload.get("agent_id"),
                                        deadline=START + min(owner_relay.BUDGET, PROMPT_DEADLINE_S))
        return got.message, got.model_text()
    except Exception:
        return None, None


def mark_disclosure_shown(session_id: str) -> None:
    """The owner-input disclosure was flushed to Codex as a systemMessage:
    record it (owner_input.mark_disclosed), so capture starts with the next
    prompt. Never raises."""
    try:
        from cardinal_core import owner_input

        owner_input.mark_disclosed(Path.home(), session_id)
    except Exception:
        pass


def spawn_relook(session_id: str, cwd: str) -> None:
    """The storyboard re-look for this prompt (storyboard_authoring.
    relook_if_moved: a look only when the branch or HEAD moved) in a
    detached child, never on the prompt's clock; its block is staged for the
    session's next tool or prompt boundary (handle_relook). Only for a
    session bound to an Investigation (staging needs the binding)."""
    from cardinal_core import investigation_events as ie, storyboard_authoring as sa

    if sa.discovery_disabled(codex_paths(), "codex", os.environ) or not ie.valid_session(session_id):
        return
    if ie.read_binding(Path.home(), session_id) is None:
        return
    spec = json.dumps({"session_id": session_id, "cwd": cwd})
    if os.environ.get(BACKGROUND_INLINE_ENV) == "1":
        handle_relook(spec)
        return
    subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--event", RELOOK_EVENT, "--refresh", spec],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        close_fds=True, start_new_session=True,
    )


def handle_relook(raw: str | None) -> None:
    spec = json.loads(raw or "{}")
    if not isinstance(spec, dict):
        return
    from cardinal_core import investigation_events as ie, investigation_turn, storyboard_authoring as sa

    sid = spec.get("session_id")
    if not ie.valid_session(sid) or ie.read_binding(Path.home(), sid) is None:
        return
    wiring = storyboard_wiring()
    block = sa.relook_if_moved(wiring.paths, "codex", sid, spec.get("cwd"), connection=wiring.connection,
                               client=wiring.client, environ=wiring.environ,
                               deadline=time.monotonic() + RELOOK_BUDGET_S)
    if block:
        investigation_turn.stage(Path.home(), sid, block)


def handle_user_prompt_submit(payload: dict[str, Any]) -> None:
    dump_debug_payload("UserPromptSubmit", payload)
    session_id = session_id_from_payload(payload)
    if not session_id:
        return
    cwd = str(payload.get("cwd") or os.getcwd())
    paths = codex_paths()

    # Sync gate FIRST: its verdict has priority. A block wins outright (the
    # prompt never reaches the model: nothing is recorded or consumed), and
    # everything else is bounded so the one JSON object is written by START +
    # PROMPT_DEADLINE_S.
    gate_out = None
    try:
        gate_out = limits.gate_output(
            paths, session_id, hook_event_name="UserPromptSubmit"
        )
    except Exception:
        pass
    blocked = isinstance(gate_out, dict) and gate_out.get("decision") == "block"
    context = None
    message = None
    relayed = told = None
    if not blocked:
        # The owner's approval of a proposed owner decision first: it is
        # independent of owner-input capture, and its outcome is shown.
        relayed, told = relay_owner_decision(payload, session_id)
        try:
            context = decision_context(paths, session_id)
        except Exception:
            pass
        try:
            parts, message = prompt_investigation(payload, session_id)
            context = "\n\n".join(x for x in (context, *parts) if x) or None
        except Exception:
            pass
    try:
        prompt_out = merge_prompt_output(gate_out, "\n\n".join(x for x in (told, context) if x) or None,
                                         "\n\n".join(x for x in (relayed, message) if x) or None)
        if prompt_out:
            sys.stdout.write(json.dumps(prompt_out))
            sys.stdout.flush()
            if message and message in str(prompt_out.get("systemMessage") or ""):
                # R3: the disclosure counts as shown only once it reached
                # Codex; a failed write (raised above) leaves it unmarked,
                # so the next prompt discloses again instead of capturing.
                mark_disclosure_shown(session_id)
    except Exception:
        pass
    if not blocked and not payload.get("agent_id"):
        try:
            spawn_relook(session_id, cwd)
        except Exception:
            pass

    # Everything after the output is bounded by START + PROMPT_EXIT_S: each
    # git call gets what is left of it, and once it is spent the rest
    # (git_state, the verdict refresh) is skipped for this prompt.
    def left() -> float:
        return START + PROMPT_EXIT_S - time.monotonic()

    def git(args: list[str]) -> str | None:
        if left() <= 0:
            return None
        return initiative.git(args, cwd, timeout=max(0.05, left()))

    head_sha = git(["rev-parse", "HEAD"])
    branch = git(["rev-parse", "--abbrev-ref", "HEAD"])
    if not branch:
        # rev-parse HEAD fails in a freshly initialized repository. The
        # symbolic ref still gives us its branch, so these sessions can be
        # classified as research instead of silently losing all context.
        branch = git(["symbolic-ref", "--short", "HEAD"])
    remote_url = git(["remote", "get-url", "origin"])
    if left() <= 0:
        return  # the budget is spent: no git_state for this prompt
    repo = initiative.canonical_repo(remote_url)
    initiative_name, initiative_type = initiative.resolve_initiative(branch)
    pr_number = pr_url = None
    try:
        # Cache-only on the sync path; a miss or stale entry is refreshed
        # by a detached child, so the first prompt on a new branch may
        # lack the PR keys. Unresolved → both keys dropped by log_record.
        pr_number, pr_url, fresh = cached_pr(paths, repo, branch)
        if not fresh:
            spawn_pr_refresh(cwd, repo, branch)
    except Exception:
        pass
    attrs: dict[str, Any] = {
        "session_id": session_id,
        "cardinal_cwd": cwd,
        "cardinal_head_sha": head_sha,
        "cardinal_branch": branch,
        "cardinal_repo": repo,
        "cardinal_remote_url": remote_url,
        "cardinal_pr_number": pr_number,
        "cardinal_pr_url": pr_url,
        "cardinal_initiative_name": initiative_name,
        "cardinal_initiative_type": initiative_type,
        "cardinal_command": initiative.detect_command(
            payload.get("prompt") or payload.get("message")
        ),
        **core_session.read_plan_stamp(paths),
    }
    # Emit a context shell even outside Git. Without this record, Codex
    # sessions started from a workspace root reach Outcomes with spend but
    # no initiative evidence and are defaulted to an in-flight feature.
    if left() <= 0:
        return
    emit_records([otlp.log_record("cardinal.git_state", attrs, time.time_ns())],
                 timeout=max(0.05, min(HOOK_TIMEOUT_SEC, left())))

    # Spend-limits verdict refresh — the async half of the gate. Runs
    # AFTER the OTLP post and stays best-effort: limits must never cost
    # telemetry. Refetches from maestro when the server-assigned TTL has
    # lapsed and rewrites the verdict file the sync gate reads next turn.
    remaining = left()
    if remaining < 0.3:
        return  # the next prompt refreshes it
    try:
        limits.maybe_refresh_verdict(paths, session_id=session_id, repo=repo, branch=branch,
                                     timeout=min(limits.FETCH_TIMEOUT_SEC, remaining))
    except Exception:
        pass


def parse_args_json(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _first_js_argument(source: str, start: int) -> tuple[str, int]:
    """Return a call's first argument and the closing separator offset.

    Codex custom tools store functions.exec input as JavaScript. The wrapper
    emits JSON literals for ordinary nested tools, so a small string-aware
    scanner is sufficient and avoids adding a JavaScript parser dependency.
    """
    depth = 0
    quote: str | None = None
    escaped = False
    i = start
    while i < len(source):
        ch = source[i]
        if quote is not None:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            i += 1
            continue
        if ch in {'"', "'", "`"}:
            quote = ch
        elif ch in "[{(":
            depth += 1
        elif ch == ")":
            if depth == 0:
                return source[start:i].strip(), i
            depth -= 1
        elif ch in "]}":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            return source[start:i].strip(), i
        i += 1
    return source[start:].strip(), len(source)


def extract_custom_exec_calls(source: Any) -> list[tuple[str, dict[str, Any]]]:
    """Extract executed-looking ``tools.<name>(JSON)`` calls from exec JS.

    Strings and comments are skipped so examples embedded in source do not
    inflate tool counts. Unparseable arguments still retain the tool name.
    """
    if not isinstance(source, str) or not source:
        return []
    calls: list[tuple[str, dict[str, Any]]] = []
    i = 0
    quote: str | None = None
    escaped = False
    while i < len(source):
        ch = source[i]
        if quote is not None:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            i += 1
            continue
        if ch in {'"', "'", "`"}:
            quote = ch
            i += 1
            continue
        if source.startswith("//", i):
            newline = source.find("\n", i + 2)
            i = len(source) if newline < 0 else newline + 1
            continue
        if source.startswith("/*", i):
            end_comment = source.find("*/", i + 2)
            i = len(source) if end_comment < 0 else end_comment + 2
            continue
        match = CUSTOM_EXEC_CALL_RE.match(source, i)
        if match:
            raw_arg, end = _first_js_argument(source, match.end())
            try:
                decoded = json.loads(raw_arg)
            except (json.JSONDecodeError, TypeError):
                decoded = None
            if isinstance(decoded, dict):
                args = decoded
            elif isinstance(decoded, str):
                args = {"input": decoded}
            else:
                args = {}
            calls.append((match.group(1), args))
            i = end + 1
            continue
        i += 1
    return calls


def decode_tool_call(call: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Normalize legacy function calls and current Codex custom calls."""
    raw_name = str(call.get("name") or "")
    if call.get("type") != "custom_tool_call":
        return raw_name, parse_args_json(call.get("arguments"))

    if raw_name in {"exec", "functions.exec"}:
        nested = extract_custom_exec_calls(call.get("input"))
        if len(nested) == 1:
            return nested[0]
        # One custom_tool_call is one model tool use. For compound executor
        # programs, keep that cardinality and retain only nested tool names;
        # never send the raw JavaScript program as tool_input.
        return "exec", {"tools": [name for name, _ in nested]}
    return raw_name, parse_args_json(call.get("input"))


def normalize_tool_name(name: str, args: dict[str, Any]) -> tuple[str, dict[str, Any], str | None]:
    if name == "exec_command":
        cmd = str(args.get("cmd") or "")
        return "Bash", {"full_command": cmd, "bash_command": cmd.split(" ", 1)[0] if cmd else ""}, None
    if name in {"apply_patch", "functions.apply_patch"}:
        patch = str(args.get("patch") or args.get("input") or "")
        target = extract_patch_target(patch)
        return "Edit", {}, target
    if name.startswith("mcp__"):
        parts = name.split("__")
        server = parts[1] if len(parts) > 1 else ""
        tool = parts[2] if len(parts) > 2 else name
        return "mcp_tool", {"mcp_server_name": server, "mcp_tool_name": tool}, None
    return name, {}, None


def extract_patch_target(patch: str) -> str | None:
    for prefix in ("*** Update File: ", "*** Add File: ", "*** Delete File: "):
        for line in patch.splitlines():
            if line.startswith(prefix):
                return line[len(prefix):].strip()
    return None


def output_success(output: Any) -> str:
    if not isinstance(output, str):
        return "true"
    m = EXIT_CODE_RE.search(output)
    if not m:
        return "true"
    return "true" if m.group(1) == "0" else "false"


def usage_attrs(usage: dict[str, Any]) -> dict[str, Any]:
    return {
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "cache_read_tokens": usage.get("cached_input_tokens"),
        "cache_read_input_tokens": usage.get("cached_input_tokens"),
        "cache_creation_tokens": usage.get("cache_creation_tokens"),
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
    }


def append_token_events(
    records: list[dict[str, Any]],
    session_id: str,
    payload: dict[str, Any],
    meta: dict[str, Any],
    state: dict[str, Any],
    ts_ns: int,
) -> None:
    """One token_count transcript event → api_request + cardinal.turn_usage,
    plus throttled cardinal.plan_state / cardinal.plan_usage.

    `state` is the per-session mutable progress dict (persisted by the
    caller): turn_seq, plan_state_sig, plan_usage_emitted_at, plan_stamp.
    """
    info = payload.get("info")
    if not isinstance(info, dict):
        return
    usage = info.get("last_token_usage")
    if not isinstance(usage, dict):
        usage = info.get("total_token_usage")
    if not isinstance(usage, dict):
        return

    # Refresh the plan stamp from this event's rate_limits FIRST so the
    # usage records emitted below carry the freshest plan facts.
    rate_limits = payload.get("rate_limits")
    plan_type = None
    limit_id = None
    if isinstance(rate_limits, dict):
        plan_type = rate_limits.get("plan_type")
        limit_id = rate_limits.get("limit_id")
        stamp = {}
        if isinstance(plan_type, str) and plan_type:
            stamp["plan_type"] = plan_type
        if isinstance(limit_id, str) and limit_id:
            stamp["rate_limit_tier"] = limit_id
        if stamp:
            state["plan_stamp"] = stamp

    plan_stamp = state.get("plan_stamp") if isinstance(state.get("plan_stamp"), dict) else {}

    model = meta.get("model")
    base = {
        "session_id": session_id,
        "user_email": meta.get("user_email"),
        "agent_runtime": "codex",
        "model": model,
        **usage_attrs(usage),
    }
    cost_usd = pricing.compute_cost_usd(model, usage, pricing.OPENAI_PRICING_USD_PER_M)
    if cost_usd is not None:
        base["cost_usd"] = cost_usd
    records.append(otlp.log_record("api_request", base, ts_ns))
    records.append(otlp.log_record("cardinal.turn_usage", {
        **base,
        "ts": ts_ns,
        "user_turn_seq": state["user_turn_seq"],
        "turn_seq": state["turn_seq"],
        **plan_stamp,
    }, ts_ns + 1))

    if not isinstance(rate_limits, dict):
        return

    # plan_state: once per session, re-emitted only when the values change
    # (Claude parity: one SessionStart emit; LWW downstream).
    plan_sig = f"{plan_type or ''}|{limit_id or ''}"
    if plan_sig != "|" and plan_sig != state.get("plan_state_sig"):
        records.append(otlp.log_record("cardinal.plan_state", {
            "session_id": session_id,
            "agent_runtime": "codex",
            "ts": ts_ns,
            "plan_type": plan_type,
            "rate_limit_tier": limit_id,
        }, ts_ns + 2))
        state["plan_state_sig"] = plan_sig

    # plan_usage: first snapshot of the session unthrottled (anchors the
    # Δ math), then at most every PLAN_USAGE_TTL_SEC of wall time.
    if core_session.plan_usage_throttled(state):
        return
    plan_usage = {
        "session_id": session_id,
        "agent_runtime": "codex",
        "ts": ts_ns,
        "plan_type": plan_type,
        "rate_limit_tier": limit_id,
    }
    any_field = False
    primary = rate_limits.get("primary")
    if isinstance(primary, dict):
        plan_usage["five_hour_utilization"] = primary.get("used_percent")
        plan_usage["five_hour_resets_at"] = primary.get("resets_at")
        any_field = True
    secondary = rate_limits.get("secondary")
    if isinstance(secondary, dict):
        plan_usage["seven_day_utilization"] = secondary.get("used_percent")
        plan_usage["seven_day_resets_at"] = secondary.get("resets_at")
        any_field = True
    if any_field:
        records.append(otlp.log_record("cardinal.plan_usage", plan_usage, ts_ns + 3))
        state["plan_usage_emitted_at"] = time.time()


def append_tool_call_event(
    records: list[dict[str, Any]],
    session_id: str,
    call: dict[str, Any],
    state: dict[str, Any],
    ts_ns: int,
) -> dict[str, Any]:
    raw_name, args = decode_tool_call(call)
    tool_name, params, target = normalize_tool_name(raw_name, args)
    if target is None:
        # Allowlisted file-path inputs (Claude parity: TARGET_KEYS is the
        # privacy boundary — only path-shaped inputs become `target`).
        key = TARGET_KEYS.get(tool_name)
        if key:
            v = args.get(key)
            if isinstance(v, str) and v:
                target = v
    plan_stamp = state.get("plan_stamp") if isinstance(state.get("plan_stamp"), dict) else {}
    attrs: dict[str, Any] = {
        "session_id": session_id,
        "ts": ts_ns,
        "user_turn_seq": state["user_turn_seq"],
        "turn_seq": state["turn_seq"],
        "tool_seq": state["tool_seq"],
        "tool_name": tool_name,
        "target": target,
        **plan_stamp,
    }
    if tool_name == "mcp_tool":
        # turn_tool carries the raw qualified MCP name — the harvester's
        # strongest clustering signal — while tool_result keeps the
        # normalized `mcp_tool` form (lakerunner's mcp_servers_used
        # aggregation reads that; do not disturb it).
        attrs["tool_name"] = raw_name
        attrs["mcp_server_name"] = params.get("mcp_server_name")
        attrs["mcp_tool_name"] = params.get("mcp_tool_name")
    elif tool_name == "Bash":
        # Privacy boundary: only the closed enum lands on turn_tool; the
        # command text itself stays on tool_result's tool_input (the
        # existing, documented divergence from the Claude plugin).
        classified = bashclass.classify_bash_command(str(params.get("full_command") or ""))
        if classified is not None:
            bash_class, bash_multi = classified
            attrs["bash_class"] = bash_class
            if bash_multi:
                attrs["bash_multi"] = True
    records.append(otlp.log_record("cardinal.turn_tool", attrs, ts_ns))
    state["tool_seq"] += 1
    return {
        "tool_name": tool_name,
        "tool_parameters": params,
        "tool_input": args,
        "target": target,
    }


def append_tool_result_event(
    records: list[dict[str, Any]],
    session_id: str,
    pending: dict[str, Any],
    output: Any,
    ts_ns: int,
) -> None:
    tool_input = pending.get("tool_input") if isinstance(pending.get("tool_input"), dict) else {}
    params = pending.get("tool_parameters") if isinstance(pending.get("tool_parameters"), dict) else {}
    attrs: dict[str, Any] = {
        "session_id": session_id,
        "agent_runtime": "codex",
        "tool_name": pending.get("tool_name"),
        "success": output_success(output),
        "tool_parameters": json.dumps(params, separators=(",", ":")) if params else None,
        "tool_input": json.dumps(tool_input, separators=(",", ":")) if tool_input else None,
    }
    records.append(otlp.log_record("tool_result", attrs, ts_ns))


def handle_stop(payload: dict[str, Any]) -> None:
    dump_debug_payload("Stop", payload)
    session_id = session_id_from_payload(payload)
    try:
        # The turn's end (investigation_agent.turn_end): the inbox, else one
        # synchronous check when the poller has not read the events lately,
        # else an open directive or the owner-decision reminders; then the
        # owner-input outbox flush. All by START + STOP_DEADLINE_S. Codex may
        # block (investigation_turn.TURN_END_BLOCK): the reason re-enters the
        # model as a <hook_prompt>-wrapped item and UserPromptSubmit does not
        # fire for it, so advisory text never becomes owner input.
        from cardinal_core import investigation_agent
        cwd = payload.get("cwd")
        d = investigation_agent.turn_end(storyboard_wiring(), session_id,
                                         stop_hook_active=payload.get("stop_hook_active") is True,
                                         deadline=START + STOP_DEADLINE_S, cwd=cwd if isinstance(cwd, str) else None)
        if d.turn_end_block:
            sys.stdout.write(json.dumps({"decision": "block", "reason": d.turn_end_block}))
            sys.stdout.flush()
    except Exception:
        pass
    if not session_id:
        return
    transcript_path = transcript_path_from_payload(payload, session_id)
    if not transcript_path:
        return

    paths = codex_paths()
    state = core_session.load_progress(paths, session_id)
    last_line = int(state.get("last_line") or 0)
    pending = state.get("pending_calls") if isinstance(state.get("pending_calls"), dict) else {}

    try:
        lines = transcript_path.read_text().splitlines()
    except (OSError, UnicodeDecodeError):
        return
    if last_line > len(lines):
        # Truncated/rotated transcript: restart the cursor and counters.
        last_line = 0
        state["user_turn_seq"] = 0
        state["turn_seq"] = 0
        state["tool_seq"] = 0
        pending = {}

    records: list[dict[str, Any]] = []
    meta: dict[str, Any] = {}
    now_ns = time.time_ns()
    # Where the NEXT firing resumes. When the per-Stop event cap trips we
    # record the first unprocessed line — not len(lines) — so the tail is
    # picked up next Stop instead of being silently skipped forever.
    resume_line = len(lines)
    for offset, line in enumerate(lines[last_line:], start=last_line):
        if len(records) >= MAX_EVENTS_PER_STOP:
            resume_line = offset
            break
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts_ns = otlp.parse_ts_ns(rec.get("timestamp"), now_ns + offset)
        rtype = rec.get("type")
        body = rec.get("payload")
        if not isinstance(body, dict):
            continue

        if rtype == "session_meta":
            meta["cwd"] = body.get("cwd")
            continue
        if rtype == "turn_context":
            if body.get("model"):
                meta["model"] = body.get("model")
            if body.get("cwd"):
                meta["cwd"] = body.get("cwd")
            continue
        if rtype == "event_msg" and body.get("type") == "user_message":
            # Turn boundary: the per-turn counters restart (Claude parity —
            # turn_seq is the model-call index WITHIN the current turn)
            # while the session-monotonic user-turn ordinal advances, so
            # (user_turn_seq, turn_seq, tool_seq) totally orders the
            # session's tool stream across Stop firings.
            core_session.begin_user_turn(state)
            continue
        if rtype == "event_msg" and body.get("type") == "token_count":
            append_token_events(records, session_id, body, meta, state, ts_ns)
            core_session.end_model_call(state)
            continue
        if rtype != "response_item":
            continue

        item_type = body.get("type")
        if item_type in {"function_call", "custom_tool_call"}:
            call_id = body.get("call_id") or body.get("id")
            normalized = append_tool_call_event(records, session_id, body, state, ts_ns)
            if isinstance(call_id, str) and call_id:
                pending[call_id] = normalized
            continue
        if item_type in {"function_call_output", "custom_tool_call_output"}:
            call_id = body.get("call_id")
            if isinstance(call_id, str) and call_id in pending:
                append_tool_result_event(records, session_id, pending.pop(call_id), body.get("output"), ts_ns)

    emit_records(records)
    plan_stamp = state.get("plan_stamp")
    if isinstance(plan_stamp, dict) and plan_stamp:
        core_session.write_plan_stamp(paths, plan_stamp)
    core_session.save_progress(paths, session_id, {
        "last_line": resume_line,
        "user_turn_seq": state["user_turn_seq"],
        "turn_seq": state["turn_seq"],
        "tool_seq": state["tool_seq"],
        "plan_state_sig": state.get("plan_state_sig"),
        "plan_usage_emitted_at": state.get("plan_usage_emitted_at"),
        "plan_stamp": plan_stamp if isinstance(plan_stamp, dict) else None,
        "pending_calls": pending,
        "transcript_path": str(transcript_path),
    })


def handle_session_start(payload: dict[str, Any]) -> None:
    dump_debug_payload("SessionStart", payload)
    started = time.monotonic()
    cwd = str(payload.get("cwd") or os.getcwd())
    parts: list[str] = []
    # Outside a git repo there's no branch to advise on; the convention
    # prompt is suppressed to avoid wasted context.
    if initiative.is_git_repo(cwd):
        context = core_session.convention_prompt("Codex")
        try:
            standing = core_session.budget_standing(
                codex_paths(), session_id_from_payload(payload), cwd
            )
            if standing:
                context = f"{context}\n\n{standing}"
        except Exception:
            # Budget standing is additive — never let it cost the convention
            # prompt (or session start).
            pass
        parts.append(context)
    try:
        # An ephemeral thread (transcript_path null: no rollout is kept, e.g.
        # the Codex app's ambient next-message suggestions, a new one every
        # few minutes) gets no investigation, storyboard or discovery.
        if "transcript_path" in payload and not payload.get("transcript_path"):
            raise LookupError("ephemeral thread")
        # The session id line (any directory: an incident storyboard needs no
        # repo) + the storyboards that may relate to this checkout. Bounded
        # bounded alongside bootstrap within hooks.json's 8 s.
        from cardinal_core import storyboard_agent

        wiring = storyboard_wiring()
        from cardinal_core import investigation_agent
        sid = session_id_from_payload(payload)
        live = investigation_agent.start(wiring, sid, payload.get("source"), cwd=cwd)
        if live:
            # Every source (startup, resume, clear, fork, and compact: Codex
            # fires SessionStart again after a compaction) sends the guidance;
            # prompts send it again only when it changed (prompt_guidance).
            note_guidance(sid, live)
        investigation_agent.spawn_poller(wiring, sid, str(INVESTIGATION_POLLER))
        discovery = storyboard_agent.session_start_text(
            wiring, cwd, sid, include_session_line=not bool(live),
            auto_context=auto_context(payload, wiring), deadline=min(started + 6.5, time.monotonic() + 1.5))
        storyboard = "\n\n".join(text for text in (live, discovery) if text)
        if storyboard:
            parts.append(storyboard)
    except Exception:
        pass
    try:
        # Local only: an MCP key without a URL (once per session), and how to
        # connect on an unconnected machine (told once per machine).
        from cardinal_core import storyboard_authoring as sa

        source = payload.get("source")
        for line in (sa.stray_key_warning(codex_paths(), "codex", sid=session_id_from_payload(payload), source=source),
                     sa.connect_hint(codex_paths(), "codex", source=source)):
            if line:
                parts.append(line)
    except Exception:
        pass
    try:
        # Checks preview (opt-in, `scripts/cardinal-checks on|off`): make the
        # installed skill match the setting (it takes effect at the next
        # session: Codex has no skill reload or skill-tool deny) and, while on
        # and connected, the routing rule. Local only.
        from cardinal_core import checks_install
        from cardinal_core import storyboard_authoring as sa

        _, rule = checks_install.session_start(
            "codex", CHECKS_PLUGIN_ROOT, connected=sa.is_connected(codex_paths(), "codex"))
        if rule:
            parts.append(rule)
    except Exception:
        pass
    if not parts:
        return
    context = "\n\n".join(parts)
    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }))
    sys.stdout.flush()


def handle_subagent_start(payload: dict[str, Any]) -> None:
    """A subagent starts without the session's SessionStart context: give it
    the storyboard discovery block the session's last look stored
    (storyboard_authoring.relook_if_moved, event "SubagentStart": no git, no
    network). Codex's SubagentStart payload names the PARENT's session_id
    (and the child as agent_id; captured from codex-cli 0.160.1/0.162.1), so
    the parent's stored look is found. Nothing when none is stored."""
    dump_debug_payload("SubagentStart", payload)
    from cardinal_core import storyboard_authoring as sa

    paths = codex_paths()
    if sa.discovery_disabled(paths, "codex", os.environ):
        return
    sid = session_id_from_payload(payload)
    if not sid:
        return
    wiring = storyboard_wiring()
    block = sa.relook_if_moved(paths, "codex", sid, payload.get("cwd"), event="SubagentStart",
                               connection=wiring.connection, client=wiring.client, environ=wiring.environ)
    if not block:
        return
    # SubagentStart hookSpecificOutput.additionalContext
    # (subagent-start.command.output schema, codex-cli 0.160.1).
    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {"hookEventName": "SubagentStart", "additionalContext": block},
    }))
    sys.stdout.flush()


def subagent_description_from_payload(payload: dict[str, Any]) -> str | None:
    """Best-effort extraction of the subagent's short task label.

    Task label only — this is the approved free-text boundary widening
    (parity with cardinal-claude-plugin v0.12.1's `subagent_description`),
    capped at 160 chars. Codex's SubagentStop payload shape is still
    unobserved (P5), so probe the plausible key spellings; the real key
    names are to be confirmed from the P5 debug captures.
    """
    candidates: list[Any] = [
        payload.get("description"),
        payload.get("task_description"),
        payload.get("taskDescription"),
        payload.get("label"),
    ]
    for input_key in ("tool_input", "toolInput"):
        tool_input = payload.get(input_key)
        if isinstance(tool_input, dict):
            candidates.append(tool_input.get("description"))
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()[:160]
    return None


def handle_subagent_stop(payload: dict[str, Any]) -> None:
    # Shape capture BEFORE any early return — the whole point is to see
    # payloads the emit path below can't handle yet (P5).
    dump_debug_payload("SubagentStop", payload)
    session_id = session_id_from_payload(payload)
    if not session_id:
        return
    total_tokens = (
        payload.get("total_tokens")
        or payload.get("totalTokens")
        or payload.get("tokens")
    )
    if total_tokens is None:
        return
    attrs = {
        "session_id": session_id,
        "agent_runtime": "codex",
        "subagent_type": payload.get("subagent_type") or payload.get("subagentType") or payload.get("matcher"),
        "agent_id": payload.get("agent_id") or payload.get("agentId"),
        "subagent_description": subagent_description_from_payload(payload),
        # Cross-adapter contract key; best-effort — Codex payloads carry
        # the child's model only on some harness versions.
        "model": payload.get("model") or payload.get("modelName") or payload.get("model_name"),
        "total_tokens": total_tokens,
        **core_session.read_plan_stamp(codex_paths()),
    }
    emit_records([otlp.log_record("cardinal.subagent_usage", attrs, time.time_ns())])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", required=True)
    parser.add_argument("--refresh", help=argparse.SUPPRESS)
    parser.add_argument("--background", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.event == BACKGROUND_EVENT:
        try:
            if args.background:
                run_background_job(Path(args.background))
        except Exception:
            pass
        silent_exit()

    if args.event == REFRESH_PR_EVENT:
        try:
            handle_refresh_pr(args.refresh)
        except Exception:
            pass
        silent_exit()

    if args.event == RELOOK_EVENT:
        try:
            handle_relook(args.refresh)
        except Exception:
            pass
        hard_exit()

    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    try:
        if args.event == "UserPromptSubmit":
            handle_user_prompt_submit(payload)
        elif args.event == "SessionStart":
            handle_session_start(payload)
        elif args.event == "Stop":
            handle_stop(payload)
        elif args.event == "SubagentStart":
            handle_subagent_start(payload)
        elif args.event == "SubagentStop":
            handle_subagent_stop(payload)
        elif args.event == "PostToolUse":
            handle_post_tool_use(payload)
        elif args.event == EVIDENCE_EVENT:
            handle_tool_evidence(payload)
        elif args.event == STORYBOARD_CONTEXT_EVENT:
            handle_storyboard_context(payload)
        elif args.event == STORYBOARD_PREVIEW_EVENT:
            handle_storyboard_preview(payload)
        elif args.event == STORYBOARD_AUTHORING_EVENT:
            handle_storyboard_authoring(payload)
    except Exception:
        pass
    hard_exit()


if __name__ == "__main__":
    main()
