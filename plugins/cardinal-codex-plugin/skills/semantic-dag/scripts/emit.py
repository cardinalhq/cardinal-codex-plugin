#!/usr/bin/env python3
"""Codex entrypoint for the shared Cardinal Semantic DAG emitter."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[3]
REPO_ROOT = Path(__file__).resolve().parents[5]
for candidate in (REPO_ROOT / "core", PLUGIN_ROOT / "hooks"):
    if (candidate / "cardinal_core" / "semantic_dag.py").is_file():
        sys.path.insert(0, str(candidate))
        break

from cardinal_core.semantic_dag import RuntimeConfig, main  # noqa: E402


CONFIG = RuntimeConfig(
    runtime="codex",
    default_state_dir="~/.cardinal/state/semantic-dag",
    default_port=8766,
    viewer_dir=(
        REPO_ROOT / "common" / "semantic-dag" / "viewer"
        if (REPO_ROOT / "common" / "semantic-dag" / "viewer").is_dir()
        else Path(__file__).resolve().parent / "viewer"
    ),
    plugin_root=PLUGIN_ROOT,
    emit_path=Path(__file__).resolve(),
    native_thread_env=(
        "CODEX_THREAD_ID",
        "CODEX_SESSION_ID",
        "OPENAI_CODEX_SESSION_ID",
    ),
)


def _bridge_running(session: str) -> bool:
    safe_session = session if re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", session) else (
        "c-" + hashlib.sha1(session.encode()).hexdigest()[:16]
    )
    try:
        checkpoint = json.loads(
            (CONFIG.state_dir / "bridges" / f"{safe_session}.json").read_text()
        )
        pid = checkpoint.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            return False
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def _ensure_session_bridge(arguments: list[str]) -> None:
    if not arguments:
        return
    if os.environ.get("SEMANTIC_DAG_NO_SESSION_BRIDGE"):
        return
    if arguments[0] in ("finish", "watch-default") or (
        arguments[0] == "watch" and any(value.lower() == "off" for value in arguments[1:])
    ):
        return
    session = next(
        (os.environ.get(name) for name in CONFIG.native_thread_env if os.environ.get(name)),
        None,
    )
    if not session or _bridge_running(session):
        return
    environment = os.environ.copy()
    environment["SEMANTIC_DAG_STATE_DIR"] = str(CONFIG.state_dir)
    try:
        subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve().parent / "session_bridge.py"),
                "start",
                "--session",
                session,
            ],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


if __name__ == "__main__":
    arguments = sys.argv[1:]
    result = main(CONFIG)
    if result == 0:
        _ensure_session_bridge(arguments)
    raise SystemExit(result)
