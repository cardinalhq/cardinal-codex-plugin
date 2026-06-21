#!/usr/bin/env python3
"""cardinal plan_state hook — SessionStart.

Emits one OTLP log event with event_name='cardinal.plan_state' per
SessionStart so the lakerunner processor can LWW the subscription tier +
billing mode onto the agent_sessions row.

Runtime-only on Codex (codex-port.md §8): Codex runs on OpenAI/ChatGPT,
not Anthropic, so there is NO keychain token and NO api.anthropic.com
fetch. The _plan_cache shim returns plan_type / rate_limit_tier =
"unknown" with empty usage, so the dashboard schema stays uniform across
runtimes without fabricating OpenAI plan data. The event *shape* is
identical to the Claude runtime; only the values differ.

Contract:
  - Input on stdin: SessionStart hook JSON {session_id, transcript_path,
    ...}.
  - Config: ~/.codex/cardinal.json via _codex_state.
  - Behaviour: best-effort, exit 0 silently on any failure.

See docs/specs/plan-state-telemetry.md for the contract.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _codex_state  # noqa: E402
import _plan_cache  # noqa: E402


HOOK_TIMEOUT_SEC = 2.0
SCOPE_VERSION = "0.1.0"


def _silent_exit() -> None:
    sys.exit(0)


def _kv(key: str, value) -> dict:
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    return {"key": key, "value": {"stringValue": str(value)}}


def _build_plan_state_attrs(blob: dict, session_id: str, ts_ns: int) -> list[dict]:
    attrs = [
        _kv("event_name", "cardinal.plan_state"),
        _kv("session_id", session_id),
        _kv("ts", ts_ns),
    ]
    # Profile-derived fields — each emitted only when present. On Codex,
    # plan_type / rate_limit_tier arrive as "unknown" from the shim; the
    # organization/billing fields are absent.
    for key in (
        "plan_type",
        "rate_limit_tier",
        "organization_type",
        "billing_type",
        "billing_mode",
    ):
        v = blob.get(key)
        if isinstance(v, str) and v:
            attrs.append(_kv(key, v))
    has_extra = blob.get("has_extra_usage_enabled")
    if isinstance(has_extra, bool):
        attrs.append(_kv("has_extra_usage_enabled", has_extra))
    return attrs


def _build_plan_usage_attrs(blob: dict, session_id: str, ts_ns: int) -> list[dict] | None:
    usage = blob.get("usage")
    if not isinstance(usage, dict) or not usage:
        return None
    attrs = [
        _kv("event_name", "cardinal.plan_usage"),
        _kv("session_id", session_id),
        _kv("ts", ts_ns),
    ]
    any_field = False
    for window in ("five_hour", "seven_day", "seven_day_sonnet", "seven_day_opus"):
        bucket = usage.get(window)
        if not isinstance(bucket, dict):
            continue
        util = bucket.get("utilization")
        if isinstance(util, (int, float)):
            attrs.append(_kv(f"{window}_utilization", float(util)))
            any_field = True
        resets = bucket.get("resets_at")
        if isinstance(resets, str) and resets:
            attrs.append(_kv(f"{window}_resets_at", resets))
            any_field = True
    return attrs if any_field else None


def main() -> None:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        _silent_exit()

    session_id = _codex_state.session_id(payload)
    if not session_id:
        _silent_exit()

    endpoint, otlp_headers, resource_attrs = _codex_state.otlp_target()
    if not endpoint:
        _silent_exit()

    blob = _plan_cache.refresh_plan_state()
    if not isinstance(blob, dict):
        _silent_exit()

    now_ns = time.time_ns()
    state_attrs = _build_plan_state_attrs(blob, session_id, now_ns)
    usage_attrs = _build_plan_usage_attrs(blob, session_id, now_ns)

    # Always emit plan_state if we have any of the projected fields.
    # Emit plan_usage too if we have usage data — the SessionStart fetch
    # anchors the first snapshot of the session for the Δ math. (On Codex
    # usage is empty, so only plan_state is emitted.)
    log_records = []
    if len(state_attrs) > 3:  # more than event_name/session_id/ts
        log_records.append({
            "timeUnixNano": str(now_ns),
            "observedTimeUnixNano": str(now_ns),
            "severityNumber": 9,
            "severityText": "INFO",
            "body": {"stringValue": "cardinal.plan_state"},
            "attributes": state_attrs,
        })
    if usage_attrs is not None:
        # Offset by 1 ns so the two records share the batch but have
        # distinct chq_tsns server-side (same per-record-uniqueness reason
        # as turn-usage.py's loop offset).
        log_records.append({
            "timeUnixNano": str(now_ns + 1),
            "observedTimeUnixNano": str(now_ns + 1),
            "severityNumber": 9,
            "severityText": "INFO",
            "body": {"stringValue": "cardinal.plan_usage"},
            "attributes": usage_attrs,
        })

    if not log_records:
        _silent_exit()

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
                            "version": SCOPE_VERSION,
                        },
                        "logRecords": log_records,
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
