"""Plan-state SHIM for the Codex runtime.

The Claude plugin's `_plan_cache` fetched Anthropic subscription/usage data
(macOS keychain OAuth token → api.anthropic.com). Codex runs on OpenAI/ChatGPT,
not Anthropic, so there is no Anthropic credential to source and no equivalent
plan endpoint to call (codex-port.md §8). Rather than fabricate OpenAI plan
data, this shim returns "unknown" / empty so the dashboard schema stays uniform
across runtimes.

This module preserves the public surface the other hooks import so they keep
working without any Anthropic fetch:

  - stamp_attrs()        → []                (git-state / turn-usage / subagent-usage)
  - refresh_plan_state() → {plan_type/rate_limit_tier: "unknown", ...}  (plan-state)
  - read()               → None              (plan-usage throttle check)
  - refresh_usage_only() → None              (plan-usage)

Everything is best-effort and never raises — telemetry must never block a turn.
"""

from __future__ import annotations


def stamp_attrs() -> list:
    """No plan stamps in the Codex runtime. Downstream hooks treat an empty
    list as the no-op case (same as Claude when plan-state hadn't run)."""
    return []


def refresh_plan_state(force_profile: bool = False) -> dict:
    """Runtime-only plan state for Codex. No Anthropic/OpenAI fetch — emit the
    same blob *shape* plan-state.py projects, with plan_type / rate_limit_tier
    pinned to "unknown" so the dashboard schema is uniform across runtimes."""
    return {
        "plan_type": "unknown",
        "rate_limit_tier": "unknown",
        "usage": {},
    }


def read() -> None:
    """No cache file in the Codex runtime. plan-usage.py treats None as
    "plan-state hasn't populated a cache" and silent-exits."""
    return None


def refresh_usage_only() -> None:
    """No usage source in the Codex runtime."""
    return None
