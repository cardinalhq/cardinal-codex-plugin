#!/usr/bin/env python3
"""Codex entrypoint for the shared Cardinal Semantic DAG emitter."""
from __future__ import annotations

import sys
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[3]
REPO_ROOT = Path(__file__).resolve().parents[5]
for candidate in (PLUGIN_ROOT / "hooks", REPO_ROOT / "core"):
    if (candidate / "cardinal_core" / "semantic_dag.py").is_file():
        sys.path.insert(0, str(candidate))
        break

from cardinal_core.semantic_dag import RuntimeConfig, main  # noqa: E402


CONFIG = RuntimeConfig(
    runtime="codex",
    default_state_dir="~/.codex/state/semantic-dag",
    default_port=8766,
    viewer_dir=Path(__file__).resolve().parent / "viewer",
    native_thread_env=(
        "CODEX_THREAD_ID",
        "CODEX_SESSION_ID",
        "OPENAI_CODEX_SESSION_ID",
    ),
)


if __name__ == "__main__":
    raise SystemExit(main(CONFIG))
