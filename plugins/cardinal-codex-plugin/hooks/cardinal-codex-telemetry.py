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

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _plugin_version  # noqa: E402
from cardinal_core import bashclass, initiative, limits, otlp, pricing  # noqa: E402
from cardinal_core import session as core_session  # noqa: E402
from cardinal_core.paths import AgentPaths  # noqa: E402


PLUGIN_VERSION = _plugin_version.plugin_version()
HOOK_TIMEOUT_SEC = 2.0
MAX_EVENTS_PER_STOP = 512

# P5 capture affordance (subagent-telemetry-enrichment field 4, step 1):
# Codex's SubagentStop payload shape has never been observed in the wild,
# so an env-gated raw-payload dump is how we finally capture one. Off by
# default; writes nothing unless CARDINAL_CODEX_DEBUG_PAYLOADS=1.
DEBUG_PAYLOADS_ENV = "CARDINAL_CODEX_DEBUG_PAYLOADS"

TARGET_KEYS = {
    "Read": "file_path",
    "Edit": "file_path",
    "Write": "file_path",
    "NotebookEdit": "notebook_path",
}

EXIT_CODE_RE = re.compile(r"Process exited with code (-?\d+)")


def codex_paths() -> AgentPaths:
    return AgentPaths(home=Path.home() / ".codex")


def silent_exit() -> None:
    sys.exit(0)


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


def emit_records(records: list[dict[str, Any]]) -> None:
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
        timeout=HOOK_TIMEOUT_SEC,
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


def handle_user_prompt_submit(payload: dict[str, Any]) -> None:
    session_id = session_id_from_payload(payload)
    if not session_id:
        return
    cwd = str(payload.get("cwd") or os.getcwd())
    paths = codex_paths()

    # Sync gate FIRST — its stdout is the hook's verdict channel and must
    # not wait on any network call below.
    try:
        gate_out = limits.gate_output(
            paths, session_id, hook_event_name="UserPromptSubmit"
        )
        if gate_out:
            sys.stdout.write(json.dumps(gate_out))
            sys.stdout.flush()
    except Exception:
        pass

    branch = None
    repo = None
    head_sha = initiative.git(["rev-parse", "HEAD"], cwd)
    if head_sha:
        branch = initiative.git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
        remote_url = initiative.git(["remote", "get-url", "origin"], cwd)
        repo = initiative.canonical_repo(remote_url)
        initiative_name, initiative_type = initiative.resolve_initiative(branch)
        attrs: dict[str, Any] = {
            "session_id": session_id,
            "cardinal_cwd": cwd,
            "cardinal_head_sha": head_sha,
            "cardinal_branch": branch,
            "cardinal_repo": repo,
            "cardinal_remote_url": remote_url,
            "cardinal_initiative_name": initiative_name,
            "cardinal_initiative_type": initiative_type,
            "cardinal_command": initiative.detect_command(
                payload.get("prompt") or payload.get("message")
            ),
            **core_session.read_plan_stamp(paths),
        }
        emit_records([otlp.log_record("cardinal.git_state", attrs, time.time_ns())])

    # Spend-limits verdict refresh — the async half of the gate. Runs
    # AFTER the OTLP post and stays best-effort: limits must never cost
    # telemetry. Refetches from maestro when the server-assigned TTL has
    # lapsed and rewrites the verdict file the sync gate reads next turn.
    try:
        limits.maybe_refresh_verdict(paths, session_id=session_id, repo=repo, branch=branch)
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
    raw_name = str(call.get("name") or "")
    args = parse_args_json(call.get("arguments"))
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
    session_id = session_id_from_payload(payload)
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
        if item_type == "function_call":
            call_id = body.get("call_id")
            normalized = append_tool_call_event(records, session_id, body, state, ts_ns)
            if isinstance(call_id, str) and call_id:
                pending[call_id] = normalized
            continue
        if item_type == "function_call_output":
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
    cwd = str(payload.get("cwd") or os.getcwd())
    if not initiative.is_git_repo(cwd):
        # Outside a git repo there's no branch to advise on; suppress the
        # prompt to avoid wasted context.
        return
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
    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
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
    args = parser.parse_args()

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
        elif args.event == "SubagentStop":
            handle_subagent_stop(payload)
    except Exception:
        pass
    silent_exit()


if __name__ == "__main__":
    main()
