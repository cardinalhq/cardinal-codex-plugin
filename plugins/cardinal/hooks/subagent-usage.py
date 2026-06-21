#!/usr/bin/env python3
"""cardinal subagent_usage hook — PostToolUse on spawn_agent.

Emits one OTLP/HTTP log event with event_name='cardinal.subagent_usage'
per completed subagent spawn, carrying the spawn's token spend so the
lakerunner agent-sessions processor can fold it into
agents_used[type].subtok (conductor
docs/specs/agent-outcomes-toolkit-metering.md §7).

Why a hook at all: the host reports all subagent activity inline under the
parent session_id with no per-request marker, so server-side attribution
cannot isolate a spawn's spend (background spawns interleave with the main
loop). When the harness writes the subagent's own transcript JSONL with
per-request usage records, this hook sums them:

    total_tokens = Σ (input + cache_creation + output)   per request

which matches the "worked tokens" definition the server-side turn
attribution uses, so subtok and tok read in the same unit.

Codex mapping (codex-port.md §5, §8):
  - PostToolUse matcher is `spawn_agent` (Claude matched Agent|Task).
  - Codex's spawn_agent tool_response token-accounting fields DIFFER from
    Claude's. We map best-effort:
        agent id      ← tool_response.agent_thread_id (Codex) | agentId (Claude)
        total_tokens  ← tool_response.tokens_used (Codex) when no
                        per-request subagent transcript is available.
    These field names are NOT empirically confirmed against a Codex
    binary — see TODOs below. Anything unmapped is omitted; never crash.

Contract:
  - Input on stdin: PostToolUse hook JSON {session_id, transcript_path,
    cwd, tool_name, tool_input, tool_response, ...}.
  - Config: ~/.codex/cardinal.json via _codex_state.
  - Behaviour: best-effort, exit 0 silently on any failure. If neither a
    subagent transcript nor a tool_response token count is available, the
    event is emitted WITHOUT total_tokens — the processor then skips
    subtok entirely rather than recording a wrong number.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _codex_state  # noqa: E402
import _plan_cache  # noqa: E402


HOOK_TIMEOUT_SEC = 2.0

# PostToolUse matcher for Codex's subagent tool. hooks.json already filters
# to this; the in-process check is belt-and-braces for direct invocation.
_SUBAGENT_TOOLS = ("spawn_agent",)


def _silent_exit() -> None:
    sys.exit(0)


def _kv(key: str, value: str) -> dict:
    return {"key": key, "value": {"stringValue": str(value)}}


def _sum_transcript_usage(path: Path) -> tuple[int, int, int] | None:
    """Sum per-request usage records from a subagent transcript JSONL.

    Returns (worked_tokens, cache_read_tokens, request_count) or None
    when the file is missing/unreadable/contains no usage records.
    worked = input + cache_creation + output, matching the server-side
    turn-attribution definition so subtok and tok share a unit.
    """
    try:
        worked = 0
        cache_read = 0
        n = 0
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                usage = (rec.get("message") or {}).get("usage")
                if not isinstance(usage, dict):
                    continue
                n += 1
                worked += (
                    int(usage.get("input_tokens") or 0)
                    + int(usage.get("cache_creation_input_tokens") or 0)
                    + int(usage.get("output_tokens") or 0)
                )
                cache_read += int(usage.get("cache_read_input_tokens") or 0)
        if n == 0:
            return None
        return worked, cache_read, n
    except OSError:
        return None


def main() -> None:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        _silent_exit()

    if payload.get("tool_name") not in _SUBAGENT_TOOLS:
        # hooks.json matcher already filters; belt-and-braces for direct
        # invocation.
        _silent_exit()

    session_id = _codex_state.session_id(payload)
    if not session_id:
        _silent_exit()

    endpoint, otlp_headers, resource_attrs = _codex_state.otlp_target()
    if not endpoint:
        _silent_exit()

    tool_response = payload.get("tool_response")
    if not isinstance(tool_response, dict):
        tool_response = {}
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}

    # Type sourcing mirrors lakerunner's toolkitKey defaulting chain so
    # the subtok lands on the same agents_used key the tool_result's n
    # landed on.
    # TODO(codex): confirm Codex's spawn_agent surfaces an agent-type label
    # in tool_response (Claude used `agentType`); tool_input.subagent_type is
    # the more likely Codex carrier. Defaulting chain is harmless if absent.
    subagent_type = (
        tool_response.get("agentType")
        or tool_input.get("subagent_type")
        or "general-purpose"
    )
    # Agent id: Codex spawn_agent → agent_thread_id; Claude Agent|Task →
    # agentId. Best-effort, first present wins.
    # TODO(codex): verify the exact key name (`agent_thread_id`) against a
    # real Codex spawn_agent tool_response.
    agent_id = (
        tool_response.get("agent_thread_id")
        or tool_response.get("agentId")
    )

    # Exact cumulative spend from the subagent's own transcript, when the
    # harness writes one at <transcript_dir>/<session_id>/subagents/
    # agent-<id>.jsonl (Claude layout). Codex's subagent-transcript layout
    # is unverified; if the file isn't there, we fall back to the
    # tool_response token count below.
    # TODO(codex): confirm whether Codex writes a per-spawn subagent
    # transcript and at what path. If it does, this exact-sum path is
    # preferred over tokens_used.
    totals = None
    transcript_path = payload.get("transcript_path") or ""
    if agent_id and transcript_path.endswith(".jsonl"):
        sub = Path(transcript_path[: -len(".jsonl")]) / "subagents" / f"agent-{agent_id}.jsonl"
        totals = _sum_transcript_usage(sub)

    attributes = [
        _kv("event_name", "cardinal.subagent_usage"),
        _kv("session_id", session_id),
        _kv("subagent_type", subagent_type),
        *([_kv("agent_id", agent_id)] if agent_id else []),
    ]
    if totals is not None:
        worked, cache_read, request_count = totals
        attributes += [
            _kv("total_tokens", worked),
            _kv("subagent_cache_read_tokens", cache_read),
            _kv("subagent_request_count", request_count),
        ]
    else:
        # No per-request subagent transcript — fall back to Codex's
        # tool_response token count. This is a single cumulative figure,
        # not a per-request sum, but it's the best total_tokens available.
        # TODO(codex): confirm `tokens_used` is the cumulative spend (not
        # a final-context footprint). If it's actually a footprint, move it
        # to final_context_tokens below and drop it as total_tokens.
        tokens_used = tool_response.get("tokens_used")
        if isinstance(tokens_used, (int, float)):
            attributes.append(_kv("total_tokens", int(tokens_used)))

    # Footprint fields from the harness result — informational; the
    # processor's subtok reads ONLY total_tokens (cumulative spend). These
    # Claude field names are kept as best-effort; absent on Codex → omitted.
    # TODO(codex): map Codex equivalents of totalTokens / totalToolUseCount /
    # totalDurationMs if/when their tool_response field names are known.
    for src, dst in (
        ("totalTokens", "final_context_tokens"),
        ("totalToolUseCount", "subagent_tool_use_count"),
        ("totalDurationMs", "subagent_duration_ms"),
    ):
        v = tool_response.get(src)
        if isinstance(v, (int, float)):
            attributes.append(_kv(dst, int(v)))
    attributes.extend(_plan_cache.stamp_attrs())

    now_ns = time.time_ns()
    body = {
        "resourceLogs": [
            {
                "resource": {
                    "attributes": [_kv(k, v) for k, v in resource_attrs.items()],
                },
                "scopeLogs": [
                    {
                        "scope": {
                            "name": "cardinal-codex-plugin",
                            "version": "0.1.0",
                        },
                        "logRecords": [
                            {
                                "timeUnixNano": str(now_ns),
                                "observedTimeUnixNano": str(now_ns),
                                "severityNumber": 9,
                                "severityText": "INFO",
                                "body": {"stringValue": "cardinal.subagent_usage"},
                                "attributes": attributes,
                            }
                        ],
                    }
                ],
            }
        ]
    }

    url = endpoint.rstrip("/") + "/v1/logs"
    headers = {"Content-Type": "application/json"}
    headers.update(otlp_headers)
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=HOOK_TIMEOUT_SEC):
            pass
    except (urllib.error.URLError, OSError, TimeoutError):
        pass

    _silent_exit()


if __name__ == "__main__":
    main()
