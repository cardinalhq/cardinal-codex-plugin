"""Shared OTLP config source for the cardinal Codex hooks.

Codex (unlike Claude Code) does not inject OTel env into hook subprocesses and
has no settings.json `env` block, so `cardinal-connect` writes a single state
file — ~/.codex/cardinal.json — that every hook reads directly to learn where
to POST telemetry and which resource attributes to stamp.

This module is the one place that knows the state-file shape. Hooks call
`otlp_target()` and `session_id()` instead of inlining any settings parsing.

Everything here is best-effort: any failure yields an empty/None result so the
caller silent-exits. Telemetry must never block or crash a turn.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

STATE_PATH = Path.home() / ".codex" / "cardinal.json"

# Defaults stamped onto the OTLP resource when the state file's
# resource_attributes string omits them. Codex runtime, always.
_DEFAULT_RESOURCE = {
    "service.name": "codex",
    "agent.runtime": "codex",
}


def load_state() -> dict:
    """Read ~/.codex/cardinal.json. Returns {} on any error (not connected,
    malformed, unreadable). Never raises."""
    try:
        raw = STATE_PATH.read_text(encoding="utf-8")
    except OSError:
        return {}
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _parse_kv(raw: str) -> dict[str, str]:
    """Parse a comma-separated `k=v,k=v` string (OTEL_RESOURCE_ATTRIBUTES
    style) into a dict. Blank/garbage parts are skipped."""
    out: dict[str, str] = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        k = k.strip()
        if k:
            out[k] = v.strip()
    return out


def otlp_target(
    state: dict | None = None,
) -> tuple[str | None, dict[str, str], dict[str, str]]:
    """Return (endpoint, headers, resource_attrs) for an OTLP/HTTP POST.

    - endpoint: the ingest base URL (caller appends /v1/logs). None when the
      plugin is not connected — caller should silent-exit.
    - headers: the auth header map, e.g. {"x-cardinalhq-api-key": "<key>"}.
    - resource_attrs: parsed resource_attributes with service.name /
      agent.runtime defaulted to "codex".
    """
    if state is None:
        state = load_state()

    endpoint = state.get("ingest_endpoint") or None

    headers: dict[str, str] = {}
    header_name = state.get("ingest_api_header") or "x-cardinalhq-api-key"
    api_key = state.get("ingest_api_key")
    if api_key:
        headers[header_name] = api_key

    resource_attrs = _parse_kv(state.get("resource_attributes", ""))
    for k, v in _DEFAULT_RESOURCE.items():
        resource_attrs.setdefault(k, v)

    # An endpoint with no key is unusable — treat as not-connected so the
    # caller doesn't POST unauthenticated.
    if not api_key:
        endpoint = None

    return endpoint, headers, resource_attrs


def session_id(payload: dict) -> str | None:
    """Canonical session id for a hook invocation: stdin `session_id` first
    (Codex sets it, same field name as Claude Code), then a CODEX_SESSION_ID
    env fallback. None when neither is present → caller silent-exits."""
    return payload.get("session_id") or os.environ.get("CODEX_SESSION_ID") or None
