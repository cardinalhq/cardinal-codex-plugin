#!/usr/bin/env python3
"""Emit Cardinal agent-session telemetry from Codex hooks.

Codex does not expose Claude Code's native OTel emitter, so this hook reads
Codex hook payloads and local session JSONL transcripts, normalizes them into
the existing Cardinal/Lakerunner event contract, and POSTs OTLP/HTTP logs.
Failures are best-effort and silent: telemetry must not break the agent loop.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PLUGIN_VERSION = "0.4.1"
HOOK_TIMEOUT_SEC = 2.0
MAX_EVENTS_PER_STOP = 512

# plan_usage cadence (mirrors the Claude plugin's 10-min Stop throttle):
# the first snapshot of a session is unthrottled; later ones emit at most
# every 10 minutes so heavy users produce ~7 usage events/day, not one
# per token_count transcript event.
PLAN_USAGE_TTL_SEC = 10 * 60

CODEX_DIR = Path.home() / ".codex"
STATE_PATH = CODEX_DIR / "cardinal.json"
SECRETS_PATH = CODEX_DIR / "cardinal-secrets.json"
TELEMETRY_DIR = CODEX_DIR / "cardinal" / "telemetry"
# Last-seen plan facts (plan_type + rate_limit_tier), global across
# sessions — the Codex analogue of the Claude plugin's plan cache. Written
# by the Stop handler when a rate_limits block is seen; read by every
# handler to stamp the two keys onto emitted records (parity with
# _plan_cache.stamp_attrs()).
PLAN_STAMP_PATH = TELEMETRY_DIR / "plan.json"

TARGET_KEYS = {
    "Read": "file_path",
    "Edit": "file_path",
    "Write": "file_path",
    "NotebookEdit": "notebook_path",
}

REMOTE_URL_RE = re.compile(r"(?:git@|https?://)([^:/]+)[:/]([^/]+)/(.+?)(?:\.git)?/?$")
EXIT_CODE_RE = re.compile(r"Process exited with code (-?\d+)")
SESSION_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")

PROTECTED_BRANCHES = frozenset({"main", "master", "develop", "trunk"})

# Noise words that appear between `worktree-` and the real name in
# EnterWorktree-style branches. Kept in lockstep with the Claude plugin's
# git-state.py (_strip_worktree_noise) and conductor's
# normalizeInitiativeName.
WORKTREE_NOISE = frozenset({
    "fix", "feat", "bug", "bugfix", "issue", "issues", "pr",
})
NUMERIC_SEGMENT_RE = re.compile(r"^\d+$")
PREFIX_TO_TYPE = {
    "feat": "feature",
    "feature": "feature",
    "perf": "feature",
    "fix": "bugfix",
    "bugfix": "bugfix",
    "refactor": "refactor",
    "cleanup": "refactor",
    "infra": "infra",
    "chore": "infra",
    "test": "infra",
    "tests": "infra",
    "ci": "infra",
    "build": "infra",
    "deps": "infra",
    "docs": "infra",
    "doc": "infra",
    "research": "research",
    "spike": "research",
}

# USD per 1M tokens, per OpenAI's public pricing. Codex does not emit a
# cost — Claude Code does, so upstream (lakerunner) reads cost_usd off the
# api_request attributes verbatim. Without a plugin-side computation every
# codex session lands at $0 and disappears from the Outcomes Dashboard's
# spend-headed views. Keep this table in sync with OpenAI's pricing page;
# lookup is exact-match first, then longest-prefix (so dated SKUs like
# `gpt-5-codex-2026-03-01` still price correctly).
MODEL_PRICING_USD_PER_M: dict[str, dict[str, float]] = {
    "gpt-5":         {"input": 1.25, "cached_input": 0.125, "output": 10.00},
    "gpt-5-codex":   {"input": 1.25, "cached_input": 0.125, "output": 10.00},
    "gpt-5-mini":    {"input": 0.25, "cached_input": 0.025, "output":  2.00},
    "gpt-5-nano":    {"input": 0.05, "cached_input": 0.005, "output":  0.40},
    "o3":            {"input": 2.00, "cached_input": 0.500, "output":  8.00},
    "o3-mini":       {"input": 1.10, "cached_input": 0.550, "output":  4.40},
    "o4-mini":       {"input": 1.10, "cached_input": 0.275, "output":  4.40},
}


def price_for_model(model: str | None) -> dict[str, float] | None:
    if not model:
        return None
    if model in MODEL_PRICING_USD_PER_M:
        return MODEL_PRICING_USD_PER_M[model]
    # Longest-prefix fallback for dated / suffixed SKUs.
    match = ""
    for key in MODEL_PRICING_USD_PER_M:
        if model.startswith(key) and len(key) > len(match):
            match = key
    return MODEL_PRICING_USD_PER_M.get(match) if match else None


def compute_cost_usd(model: str | None, usage: dict[str, Any]) -> float | None:
    """Return the USD cost for one Codex api_request or None if the model
    isn't priced. Follows OpenAI billing semantics: `input_tokens` is the
    total input count and `cached_input_tokens` is a subset that bills at
    the cached rate. Returning None (vs 0.0) skips the attribute so
    unpriced models don't accumulate misleading zero rows in lakerunner."""
    price = price_for_model(model)
    if price is None:
        return None
    input_total = int(usage.get("input_tokens") or 0)
    cached = int(usage.get("cached_input_tokens") or 0)
    output = int(usage.get("output_tokens") or 0)
    non_cached_input = max(0, input_total - cached)
    cost = (
        non_cached_input * price["input"]
        + cached          * price["cached_input"]
        + output          * price["output"]
    ) / 1_000_000.0
    return round(cost, 6)


def silent_exit() -> None:
    sys.exit(0)


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def safe_session(session_id: str) -> str:
    return SESSION_SAFE_RE.sub("_", session_id)[:128]


def progress_path(session_id: str) -> Path:
    return TELEMETRY_DIR / f"{safe_session(session_id)}.json"


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)


def kv(key: str, value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    return {"key": key, "value": {"stringValue": str(value)}}


def parse_ts_ns(raw: Any, fallback_ns: int) -> int:
    if isinstance(raw, str) and raw:
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1_000_000_000)
        except ValueError:
            return fallback_ns
    return fallback_ns


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

    sessions_dir = CODEX_DIR / "sessions"
    if not sessions_dir.exists():
        return None
    matches = list(sessions_dir.rglob(f"*{session_id}.jsonl"))
    if not matches:
        return None
    matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return matches[0]


def load_connection() -> tuple[dict[str, Any], dict[str, Any]] | None:
    state = read_json(STATE_PATH)
    secrets = read_json(SECRETS_PATH)
    endpoint = state.get("ingest_endpoint")
    api_key = secrets.get("ingest_api_key")
    if not endpoint or not api_key:
        return None
    return state, secrets


def resource_attrs(state: dict[str, Any]) -> dict[str, str]:
    return {
        "service.name": "codex",
        "agent.runtime": "codex",
        "deployment.environment": str(state.get("deployment_environment") or "unknown"),
        "user.email": str(state.get("user_email") or "unknown"),
        "cardinal.org": str(state.get("org_slug") or state.get("org_id") or "unknown"),
        "cardinal.plugin_version": PLUGIN_VERSION,
    }


def emit_records(records: list[dict[str, Any]]) -> None:
    if not records:
        return
    conn = load_connection()
    if not conn:
        return
    state, secrets = conn
    endpoint = str(state.get("ingest_endpoint")).rstrip("/")
    api_header = str(secrets.get("ingest_api_header") or "x-cardinalhq-api-key")
    api_key = str(secrets.get("ingest_api_key"))

    body = {
        "resourceLogs": [
            {
                "resource": {
                    "attributes": [kv(k, v) for k, v in resource_attrs(state).items()],
                },
                "scopeLogs": [
                    {
                        "scope": {
                            "name": "cardinal-codex-plugin",
                            "version": PLUGIN_VERSION,
                        },
                        "logRecords": records,
                    }
                ],
            }
        ]
    }
    req = urllib.request.Request(
        endpoint + "/v1/logs",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "content-type": "application/json",
            api_header: api_key,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=HOOK_TIMEOUT_SEC):
            pass
    except (urllib.error.URLError, OSError, TimeoutError):
        pass


def log_record(event_name: str, attrs: dict[str, Any], ts_ns: int) -> dict[str, Any]:
    all_attrs = {"event_name": event_name, **attrs}
    return {
        "timeUnixNano": str(ts_ns),
        "observedTimeUnixNano": str(ts_ns),
        "severityNumber": 9,
        "severityText": "INFO",
        "body": {"stringValue": event_name},
        "attributes": [kv(k, v) for k, v in all_attrs.items() if v is not None and v != ""],
    }


def git(args: list[str], cwd: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def canonical_repo(remote_url: str | None) -> str | None:
    if not remote_url:
        return None
    m = REMOTE_URL_RE.match(remote_url.strip())
    if not m:
        return None
    name = re.sub(r"\.git$", "", m.group(3))
    return f"{m.group(2)}/{name}" if m.group(2) and name else None


def strip_worktree_noise(name: str) -> str:
    """worktree-fix-1018-github-app-repo-picker → github-app-repo-picker.
    Conservative: non-worktree names pass through verbatim; if nothing
    real remains after the head, keep the original."""
    if not name.startswith("worktree-"):
        return name
    segs = name.split("-")
    i = 1
    while i < len(segs) and (
        segs[i] in WORKTREE_NOISE or NUMERIC_SEGMENT_RE.match(segs[i])
    ):
        i += 1
    if i < len(segs):
        return "-".join(segs[i:])
    return name


def resolve_initiative(branch: str | None) -> tuple[str | None, str]:
    if not branch or branch == "HEAD":
        return None, "research"
    if branch in PROTECTED_BRANCHES:
        return None, "research"
    if "/" in branch:
        prefix, _, rest = branch.partition("/")
        mapped = PREFIX_TO_TYPE.get(prefix.lower())
        if mapped and rest:
            return strip_worktree_noise(rest), mapped
    return strip_worktree_noise(branch), "feature"


COMMAND_RE = re.compile(r"^\s*/([A-Za-z0-9][\w:-]*)")
COMMAND_TAG_RE = re.compile(r"<command-name>\s*/?([\w:-]+)\s*</command-name>")


def detect_command(prompt: Any) -> str | None:
    """'/code-review --fix' → 'code-review'. Accepts the raw typed form
    (anchored at start) and the expanded <command-name> tag form, matching
    the Claude plugin's git-state.py."""
    if not isinstance(prompt, str):
        return None
    m = COMMAND_RE.match(prompt)
    if m:
        return m.group(1)
    m = COMMAND_TAG_RE.search(prompt)
    if m:
        return m.group(1)
    return None


def read_plan_stamp() -> dict[str, Any]:
    """{plan_type, rate_limit_tier} from the last-seen rate_limits block,
    or {} — callers merge it into event attrs (missing keys are skipped
    by log_record's None/empty filter)."""
    blob = read_json(PLAN_STAMP_PATH)
    out: dict[str, Any] = {}
    for key in ("plan_type", "rate_limit_tier"):
        v = blob.get(key)
        if isinstance(v, str) and v:
            out[key] = v
    return out


def _limits():
    """Lazy import of the sibling limits module — best-effort, None when
    unavailable so every limits path degrades to a no-op."""
    try:
        import _limits_common as lc
        return lc
    except ImportError:
        return None


def limits_gate_output(session_id: str) -> dict[str, Any] | None:
    """Port of the Claude plugin's limits-gate.py (sync half). File I/O
    only — never touches the network. Returns the hook-output JSON to
    print, or None (fail open).

    Severity → channel mapping (the server decides severity; we route it):
      decision=allow, band>0 → additionalContext only (model economizes).
      decision=warn          → additionalContext + systemMessage.
      decision=block         → {"decision": "block", reason}; an override
                               file downgrades it to warn-tier surfacing.
    Warn/notify obey band hysteresis (only speak when the band RISES);
    a block is enforced every turn while in force.
    """
    lc = _limits()
    if lc is None:
        return None
    verdict = lc.read_verdict(session_id)
    if not verdict:
        return None

    decision = verdict.get("decision")
    try:
        band = int(verdict.get("band") or 0)
    except (TypeError, ValueError):
        band = 0
    fetched_at = verdict.get("fetched_at")
    age = (
        time.time() - fetched_at
        if isinstance(fetched_at, (int, float))
        else float("inf")
    )

    if decision == "block" and age <= lc.BLOCK_MAX_AGE_SEC:
        if not lc.override_path(session_id).exists():
            reason = (
                verdict.get("block_reason")
                or verdict.get("user_message")
                or "A Cardinal spend limit for this work has been reached."
            )
            return {"decision": "block", "reason": reason}
        decision = "warn"  # overridden: keep the human-visible standing

    if band <= 0 or age > lc.WARN_MAX_AGE_SEC:
        return None

    ack = lc._read_json_file(lc.ack_path(session_id))
    try:
        last_band = int(ack.get("band") or 0)
    except (TypeError, ValueError):
        last_band = 0
    if band <= last_band:
        return None

    out: dict[str, Any] = {}
    agent_context = verdict.get("agent_context")
    if isinstance(agent_context, str) and agent_context:
        out["hookSpecificOutput"] = {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": agent_context,
        }
    user_message = verdict.get("user_message")
    if decision == "warn" and isinstance(user_message, str) and user_message:
        out["systemMessage"] = user_message
    if not out:
        return None
    lc.atomic_write_json(
        lc.ack_path(session_id), {"band": band, "surfaced_at": time.time()}
    )
    return out


def handle_user_prompt_submit(payload: dict[str, Any]) -> None:
    session_id = session_id_from_payload(payload)
    if not session_id:
        return
    cwd = str(payload.get("cwd") or os.getcwd())

    # Sync gate FIRST — its stdout is the hook's verdict channel and must
    # not wait on any network call below.
    try:
        gate_out = limits_gate_output(session_id)
        if gate_out:
            sys.stdout.write(json.dumps(gate_out))
            sys.stdout.flush()
    except Exception:
        pass

    branch = None
    repo = None
    head_sha = git(["rev-parse", "HEAD"], cwd)
    if head_sha:
        branch = git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
        remote_url = git(["remote", "get-url", "origin"], cwd)
        repo = canonical_repo(remote_url)
        initiative_name, initiative_type = resolve_initiative(branch)
        attrs: dict[str, Any] = {
            "session_id": session_id,
            "cardinal_cwd": cwd,
            "cardinal_head_sha": head_sha,
            "cardinal_branch": branch,
            "cardinal_repo": repo,
            "cardinal_remote_url": remote_url,
            "cardinal_initiative_name": initiative_name,
            "cardinal_initiative_type": initiative_type,
            "cardinal_command": detect_command(payload.get("prompt") or payload.get("message")),
            **read_plan_stamp(),
        }
        emit_records([log_record("cardinal.git_state", attrs, time.time_ns())])

    # Spend-limits verdict refresh — the async half of the gate. Runs
    # AFTER the OTLP post and stays best-effort: limits must never cost
    # telemetry. Refetches from maestro when the server-assigned TTL has
    # lapsed and rewrites the verdict file the sync gate reads next turn.
    try:
        lc = _limits()
        if lc is not None:
            lc.maybe_refresh_verdict(session_id=session_id, repo=repo, branch=branch)
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
    cost_usd = compute_cost_usd(model, usage)
    if cost_usd is not None:
        base["cost_usd"] = cost_usd
    records.append(log_record("api_request", base, ts_ns))
    records.append(log_record("cardinal.turn_usage", {
        **base,
        "ts": ts_ns,
        "turn_seq": state["turn_seq"],
        **plan_stamp,
    }, ts_ns + 1))

    if not isinstance(rate_limits, dict):
        return

    # plan_state: once per session, re-emitted only when the values change
    # (Claude parity: one SessionStart emit; LWW downstream).
    plan_sig = f"{plan_type or ''}|{limit_id or ''}"
    if plan_sig != "|" and plan_sig != state.get("plan_state_sig"):
        records.append(log_record("cardinal.plan_state", {
            "session_id": session_id,
            "agent_runtime": "codex",
            "ts": ts_ns,
            "plan_type": plan_type,
            "rate_limit_tier": limit_id,
        }, ts_ns + 2))
        state["plan_state_sig"] = plan_sig

    # plan_usage: first snapshot of the session unthrottled (anchors the
    # Δ math), then at most every PLAN_USAGE_TTL_SEC of wall time.
    last_emit = state.get("plan_usage_emitted_at")
    now_s = time.time()
    if isinstance(last_emit, (int, float)) and now_s - last_emit < PLAN_USAGE_TTL_SEC:
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
        records.append(log_record("cardinal.plan_usage", plan_usage, ts_ns + 3))
        state["plan_usage_emitted_at"] = now_s


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
        "turn_seq": state["turn_seq"],
        "tool_seq": state["tool_seq"],
        "tool_name": tool_name,
        "target": target,
        **plan_stamp,
    }
    records.append(log_record("cardinal.turn_tool", attrs, ts_ns))
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
    records.append(log_record("tool_result", attrs, ts_ns))


def handle_stop(payload: dict[str, Any]) -> None:
    session_id = session_id_from_payload(payload)
    if not session_id:
        return
    transcript_path = transcript_path_from_payload(payload, session_id)
    if not transcript_path:
        return

    progress = read_json(progress_path(session_id))
    last_line = int(progress.get("last_line") or 0)
    pending = progress.get("pending_calls") if isinstance(progress.get("pending_calls"), dict) else {}
    state: dict[str, Any] = {
        "turn_seq": int(progress.get("turn_seq") or 0),
        "tool_seq": int(progress.get("tool_seq") or 0),
        "plan_state_sig": progress.get("plan_state_sig"),
        "plan_usage_emitted_at": progress.get("plan_usage_emitted_at"),
        "plan_stamp": progress.get("plan_stamp") if isinstance(progress.get("plan_stamp"), dict) else read_plan_stamp(),
    }

    try:
        lines = transcript_path.read_text().splitlines()
    except (OSError, UnicodeDecodeError):
        return
    if last_line > len(lines):
        last_line = 0
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
        ts_ns = parse_ts_ns(rec.get("timestamp"), now_ns + offset)
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
            # Turn boundary: both counters restart (Claude parity —
            # turn_seq is the model-call index WITHIN the current turn).
            state["turn_seq"] = 0
            state["tool_seq"] = 0
            continue
        if rtype == "event_msg" and body.get("type") == "token_count":
            append_token_events(records, session_id, body, meta, state, ts_ns)
            state["turn_seq"] += 1
            state["tool_seq"] = 0
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
        atomic_write_json(PLAN_STAMP_PATH, {
            **plan_stamp,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
    atomic_write_json(progress_path(session_id), {
        "last_line": resume_line,
        "turn_seq": state["turn_seq"],
        "tool_seq": state["tool_seq"],
        "plan_state_sig": state.get("plan_state_sig"),
        "plan_usage_emitted_at": state.get("plan_usage_emitted_at"),
        "plan_stamp": plan_stamp if isinstance(plan_stamp, dict) else None,
        "pending_calls": pending,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "transcript_path": str(transcript_path),
    })


# The initiative-convention prompt (Claude plugin parity: initiative-
# convention.py). Codex reads it once per session via SessionStart
# additionalContext and acts on it when branches come up. Worded to steer
# branch creation, not to demand renames of existing branches.
CONVENTION_PROMPT = (
    "You are running inside a Cardinal-instrumented Codex session. "
    "Cardinal attributes agent spend to 'initiatives' — "
    "one branch = one initiative. When you create a new branch for "
    "work in this session, follow the convention:\n\n"
    "  <type-prefix>/<kebab-name>\n\n"
    "  type-prefix  ∈ {feat, fix, refactor, infra, chore, research, spike}\n"
    "  kebab-name   = lowercase, 1–4 dash-separated segments\n\n"
    "Examples:\n"
    "  feat/outcomes-observability    → name 'outcomes-observability', type 'feature'\n"
    "  fix/login-crash                → name 'login-crash',            type 'bugfix'\n"
    "  refactor/auth-token-rotation   → name 'auth-token-rotation',    type 'refactor'\n"
    "  research/data-pipeline-spike   → name 'data-pipeline-spike',    type 'research'\n\n"
    "Prefix aliases: 'feature' = 'feat', 'bugfix' = 'fix', 'chore' = "
    "'infra', 'spike' = 'research'. Other conventional prefixes are "
    "also recognized: 'perf' → feature; 'cleanup' → refactor; 'test', "
    "'tests', 'ci', 'build', 'deps', 'docs', 'doc' → infra. Sessions "
    "on main/master/develop/"
    "trunk are treated as research/scoping work — when intent "
    "crystallises into a deliverable, cut a typed branch using this "
    "convention. Off-convention branches get a stable name but "
    "default to type 'feature', so the convention is the way to "
    "ensure correct classification."
)


def _is_git_repo(cwd: str) -> bool:
    return git(["rev-parse", "--is-inside-work-tree"], cwd) == "true"


def _budget_standing(session_id: str | None, cwd: str) -> str | None:
    """One synchronous limits fetch at session start (short timeout, fail
    open) so the budget is part of the session's standing context from
    turn one. Also warm-writes the verdict file the per-turn sync gate
    reads. No-op when the backend doesn't advertise the limits protocol."""
    if not session_id:
        return None
    lc = _limits()
    if lc is None or not lc.limits_config():
        return None
    repo, branch = lc.git_facts(cwd)
    verdict = lc.maybe_refresh_verdict(
        session_id=session_id, repo=repo, branch=branch, force=True, timeout=1.5
    )
    if not verdict:
        return None
    lines = lc.standing_lines(verdict)
    if not lines:
        return None
    parts = ["Cardinal spend budgets apply to this session:"]
    parts.extend(lines)
    # Server-authored copy rides through verbatim — when a threshold is
    # already crossed at session start, lead with the server's message.
    user_message = verdict.get("user_message")
    if isinstance(user_message, str) and user_message:
        parts.append(user_message)
    parts.append(
        "Work economically as budgets tighten; budget standing updates "
        "arrive automatically as the session proceeds."
    )
    return "\n".join(parts)


def handle_session_start(payload: dict[str, Any]) -> None:
    cwd = str(payload.get("cwd") or os.getcwd())
    if not _is_git_repo(cwd):
        # Outside a git repo there's no branch to advise on; suppress the
        # prompt to avoid wasted context.
        return
    context = CONVENTION_PROMPT
    try:
        standing = _budget_standing(session_id_from_payload(payload), cwd)
        if standing:
            context = f"{CONVENTION_PROMPT}\n\n{standing}"
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


def handle_subagent_stop(payload: dict[str, Any]) -> None:
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
        "total_tokens": total_tokens,
        **read_plan_stamp(),
    }
    emit_records([log_record("cardinal.subagent_usage", attrs, time.time_ns())])


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
