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


PLUGIN_VERSION = "0.3.0"
HOOK_TIMEOUT_SEC = 2.0
MAX_EVENTS_PER_STOP = 512

CODEX_DIR = Path.home() / ".codex"
STATE_PATH = CODEX_DIR / "cardinal.json"
SECRETS_PATH = CODEX_DIR / "cardinal-secrets.json"
TELEMETRY_DIR = CODEX_DIR / "cardinal" / "telemetry"

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


def resolve_initiative(branch: str | None) -> tuple[str | None, str]:
    if not branch or branch == "HEAD":
        return None, "research"
    if branch in PROTECTED_BRANCHES:
        return None, "research"
    if "/" in branch:
        prefix, _, rest = branch.partition("/")
        mapped = PREFIX_TO_TYPE.get(prefix.lower())
        if mapped and rest:
            return rest, mapped
    return branch, "feature"


def detect_command(prompt: Any) -> str | None:
    if not isinstance(prompt, str):
        return None
    m = re.match(r"^\s*/([A-Za-z0-9][\w:-]*)", prompt)
    return m.group(1) if m else None


def handle_user_prompt_submit(payload: dict[str, Any]) -> None:
    session_id = session_id_from_payload(payload)
    if not session_id:
        return
    cwd = str(payload.get("cwd") or os.getcwd())
    head_sha = git(["rev-parse", "HEAD"], cwd)
    if not head_sha:
        return
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
        "cardinal_initiative_name": initiative_name,
        "cardinal_initiative_type": initiative_type,
        "cardinal_command": detect_command(payload.get("prompt") or payload.get("message")),
    }
    emit_records([log_record("cardinal.git_state", attrs, time.time_ns())])


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
    turn_seq: int,
    ts_ns: int,
) -> None:
    info = payload.get("info")
    if not isinstance(info, dict):
        return
    usage = info.get("last_token_usage")
    if not isinstance(usage, dict):
        usage = info.get("total_token_usage")
    if not isinstance(usage, dict):
        return

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
        "turn_seq": turn_seq,
    }, ts_ns + 1))

    rate_limits = payload.get("rate_limits")
    if isinstance(rate_limits, dict):
        plan_type = rate_limits.get("plan_type")
        records.append(log_record("cardinal.plan_state", {
            "session_id": session_id,
            "agent_runtime": "codex",
            "plan_type": plan_type,
            "rate_limit_tier": rate_limits.get("limit_id"),
        }, ts_ns + 2))
        plan_usage = {
            "session_id": session_id,
            "agent_runtime": "codex",
            "plan_type": plan_type,
            "rate_limit_tier": rate_limits.get("limit_id"),
        }
        primary = rate_limits.get("primary")
        if isinstance(primary, dict):
            plan_usage["five_hour_utilization"] = primary.get("used_percent")
            plan_usage["five_hour_resets_at"] = primary.get("resets_at")
        secondary = rate_limits.get("secondary")
        if isinstance(secondary, dict):
            plan_usage["seven_day_utilization"] = secondary.get("used_percent")
            plan_usage["seven_day_resets_at"] = secondary.get("resets_at")
        records.append(log_record("cardinal.plan_usage", plan_usage, ts_ns + 3))


def append_tool_call_event(
    records: list[dict[str, Any]],
    session_id: str,
    call: dict[str, Any],
    turn_seq: int,
    tool_seq: int,
    ts_ns: int,
) -> tuple[dict[str, Any], int]:
    raw_name = str(call.get("name") or "")
    args = parse_args_json(call.get("arguments"))
    tool_name, params, target = normalize_tool_name(raw_name, args)
    attrs: dict[str, Any] = {
        "session_id": session_id,
        "turn_seq": turn_seq,
        "tool_seq": tool_seq,
        "tool_name": tool_name,
        "target": target,
    }
    records.append(log_record("cardinal.turn_tool", attrs, ts_ns))
    return {
        "tool_name": tool_name,
        "tool_parameters": params,
        "tool_input": args,
        "target": target,
    }, tool_seq + 1


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
    turn_seq = int(progress.get("turn_seq") or 0)
    tool_seq = int(progress.get("tool_seq") or 0)
    pending = progress.get("pending_calls") if isinstance(progress.get("pending_calls"), dict) else {}

    try:
        lines = transcript_path.read_text().splitlines()
    except (OSError, UnicodeDecodeError):
        return
    if last_line > len(lines):
        last_line = 0
        turn_seq = 0
        tool_seq = 0
        pending = {}

    records: list[dict[str, Any]] = []
    meta: dict[str, Any] = {}
    now_ns = time.time_ns()
    for offset, line in enumerate(lines[last_line:], start=last_line):
        if len(records) >= MAX_EVENTS_PER_STOP:
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
            tool_seq = 0
            continue
        if rtype == "event_msg" and body.get("type") == "token_count":
            append_token_events(records, session_id, body, meta, turn_seq, ts_ns)
            turn_seq += 1
            tool_seq = 0
            continue
        if rtype != "response_item":
            continue

        item_type = body.get("type")
        if item_type == "function_call":
            call_id = body.get("call_id")
            normalized, tool_seq = append_tool_call_event(
                records, session_id, body, turn_seq, tool_seq, ts_ns
            )
            if isinstance(call_id, str) and call_id:
                pending[call_id] = normalized
            continue
        if item_type == "function_call_output":
            call_id = body.get("call_id")
            if isinstance(call_id, str) and call_id in pending:
                append_tool_result_event(records, session_id, pending.pop(call_id), body.get("output"), ts_ns)

    emit_records(records)
    atomic_write_json(progress_path(session_id), {
        "last_line": len(lines),
        "turn_seq": turn_seq,
        "tool_seq": tool_seq,
        "pending_calls": pending,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "transcript_path": str(transcript_path),
    })


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
        elif args.event == "Stop":
            handle_stop(payload)
        elif args.event == "SubagentStop":
            handle_subagent_stop(payload)
    except Exception:
        pass
    silent_exit()


if __name__ == "__main__":
    main()
