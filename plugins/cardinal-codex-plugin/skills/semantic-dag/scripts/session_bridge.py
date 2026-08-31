#!/usr/bin/env python3
"""Attach Codex session file events to the active Semantic DAG node.

Codex Desktop writes completed execution items to its session JSONL.  This
bridge consumes the structured ``CommandExecution.parsed_cmd`` and
``FileChange.changes`` fields directly, so file attribution does not depend on
model instructions or lifecycle-hook coverage.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import time
import urllib.parse
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
EMITTER = SCRIPT_DIR / "emit.py"


def _load_runtime():
    spec = importlib.util.spec_from_file_location("semantic_dag_codex_emitter", EMITTER)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load Semantic DAG emitter at {EMITTER}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from cardinal_core.file_events import file_events_from_hook
    from cardinal_core.semantic_dag import configure, emit

    configure(module.CONFIG)
    return module.CONFIG, emit, file_events_from_hook


CONFIG, emit, file_events_from_hook = _load_runtime()


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value))
    temporary.replace(path)


def _safe_id(value: str) -> str:
    import hashlib
    import re

    return value if re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", value) else (
        "c-" + hashlib.sha1(value.encode()).hexdigest()[:16]
    )


def _state_dir() -> Path:
    return CONFIG.state_dir


def _binding(session: str) -> dict:
    return _read_json(_state_dir() / "bindings" / f"{_safe_id(session)}.json")


def _checkpoint_path(session: str) -> Path:
    return _state_dir() / "bridges" / f"{_safe_id(session)}.json"


def _session_log(session: str) -> Path | None:
    override = os.environ.get("CODEX_SESSION_LOG")
    if override:
        path = Path(os.path.expanduser(override))
        return path if path.is_file() else None
    root = Path(
        os.path.expanduser(os.environ.get("CODEX_SESSIONS_DIR", "~/.codex/sessions"))
    )
    if not root.is_dir():
        return None
    suffix = f"-{session}.jsonl"
    candidates = [path for path in root.rglob("*.jsonl") if path.name.endswith(suffix)]
    return max(candidates, key=lambda path: path.stat().st_mtime, default=None)


def _cwd(value: object) -> str:
    raw = str(value or os.getcwd())
    if raw.startswith("file://"):
        parsed = urllib.parse.urlparse(raw)
        return urllib.parse.unquote(parsed.path)
    return raw


def _hook_events(tool_name: str, tool_input: object, cwd: str) -> list[tuple[str, str]]:
    return file_events_from_hook(
        {
            "hook_event_name": "PostToolUse",
            "tool_name": tool_name,
            "tool_input": tool_input if isinstance(tool_input, dict) else {},
            "cwd": cwd,
        }
    )


def file_events_from_session_record(record: object) -> list[tuple[str, str]]:
    """Extract exact file metadata from one completed Codex session record."""
    if not isinstance(record, dict) or record.get("type") != "event_msg":
        return []
    payload = record.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "item_completed":
        return []
    item = payload.get("item")
    if not isinstance(item, dict):
        return []

    item_type = item.get("type")
    cwd = _cwd(item.get("cwd"))
    events: list[tuple[str, str]] = []

    if item_type == "CommandExecution" and item.get("status") == "completed":
        parsed_commands = item.get("parsed_cmd")
        if isinstance(parsed_commands, list):
            for parsed in parsed_commands:
                if not isinstance(parsed, dict) or parsed.get("type") != "read":
                    continue
                events.extend(_hook_events("read_file", {"path": parsed.get("path")}, cwd))
    elif item_type == "FileChange":
        changes = item.get("changes")
        if isinstance(changes, dict):
            for path, change in changes.items():
                events.extend(
                    file_events_from_hook(
                        {
                            "hook_event_name": "FileChanged",
                            "file_path": path,
                            "cwd": cwd,
                        }
                    )
                )
                if isinstance(change, dict) and change.get("move_path"):
                    events.extend(
                        file_events_from_hook(
                            {
                                "hook_event_name": "FileChanged",
                                "file_path": change["move_path"],
                                "cwd": cwd,
                            }
                        )
                    )
    elif item_type == "McpToolCall" and item.get("status") == "completed":
        events.extend(_hook_events(str(item.get("tool") or ""), item.get("arguments"), cwd))

    unique: list[tuple[str, str]] = []
    for event in events:
        if event not in unique:
            unique.append(event)
    return unique


def _display_path(path: str, dag: dict) -> str:
    candidate = Path(path)
    project = Path(str(dag.get("cwd") or os.getcwd()))
    try:
        return str(candidate.relative_to(project.resolve()))
    except (OSError, ValueError):
        return str(candidate)


def _record_time(record: dict) -> float | None:
    payload = record.get("payload")
    if isinstance(payload, dict):
        completed = payload.get("completed_at_ms")
        if isinstance(completed, (int, float)):
            return float(completed) / 1000.0
    timestamp = record.get("timestamp")
    if isinstance(timestamp, str):
        try:
            return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return None


def _active_at(thread: str, agent: str, completed_at: float | None, dag: dict) -> str | None:
    if completed_at is None:
        current = (dag.get("active_by_agent") or {}).get(agent)
        if not current and agent == "root":
            current = dag.get("active")
        return current if isinstance(current, str) and current else None

    active_by_agent: dict[str, str] = {}
    events_path = _state_dir() / "threads" / _safe_id(thread) / "events.jsonl"
    try:
        lines = events_path.read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        event_time = event.get("ts")
        if not isinstance(event_time, (int, float)) or float(event_time) > completed_at:
            continue
        event_type = event.get("type")
        owner = str(event.get("agent") or "root")
        node = event.get("id")
        if event_type in ("start", "reset", "finish"):
            active_by_agent.clear()
        elif event_type == "agent_finish":
            active_by_agent.pop(owner, None)
        elif event_type == "activate" and isinstance(node, str):
            active_by_agent[owner] = node
        elif event_type == "status" and isinstance(node, str):
            if event.get("status") == "active":
                active_by_agent[owner] = node
            elif active_by_agent.get(owner) == node:
                active_by_agent.pop(owner, None)
        elif event_type in ("done", "error") and active_by_agent.get(owner) == node:
            active_by_agent.pop(owner, None)
    return active_by_agent.get(agent)


def _emit_record(session: str, record: dict) -> None:
    file_events = file_events_from_session_record(record)
    if not file_events:
        return
    binding = _binding(session)
    thread = str(binding.get("thread") or session)
    agent = str(binding.get("agent") or "root")
    dag_path = _state_dir() / "threads" / _safe_id(thread) / "dag.json"
    dag = _read_json(dag_path)
    active = _active_at(thread, agent, _record_time(record), dag)
    if active is None:
        return
    for kind, path in file_events:
        emit(
            thread,
            {
                "type": "file",
                "id": active,
                "agent": agent,
                "kind": kind,
                "path": _display_path(path, dag),
                "source": "codex-session",
            },
        )


def _watch_enabled(session: str) -> bool:
    binding = _binding(session)
    thread = str(binding.get("thread") or session)
    dag = _read_json(_state_dir() / "threads" / _safe_id(thread) / "dag.json")
    return bool(dag.get("watch_mode"))


def _process_available(stream, session: str) -> int:
    while True:
        offset = stream.tell()
        line = stream.readline()
        if not line:
            return offset
        if not line.endswith("\n"):
            stream.seek(offset)
            return offset
        try:
            record = json.loads(line)
        except ValueError:
            continue
        _emit_record(session, record)


@contextmanager
def _bridge_lock(session: str):
    path = _checkpoint_path(session).with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a")
    locked = True
    try:
        try:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            locked = False
        except (ImportError, OSError):
            pass
        yield locked
    finally:
        stream.close()


def run_bridge(session: str, once: bool = False, from_start: bool = False) -> int:
    with _bridge_lock(session) as locked:
        if not locked:
            return 0
        transcript = _session_log(session)
        if transcript is None:
            return 0
        checkpoint_path = _checkpoint_path(session)
        checkpoint = _read_json(checkpoint_path)
        same_transcript = checkpoint.get("transcript") == str(transcript)
        if from_start:
            offset = 0
        elif same_transcript and isinstance(checkpoint.get("offset"), int):
            offset = min(checkpoint["offset"], transcript.stat().st_size)
        else:
            offset = transcript.stat().st_size

        def checkpoint_offset() -> None:
            _write_json(
                checkpoint_path,
                {
                    "pid": os.getpid(),
                    "session": session,
                    "transcript": str(transcript),
                    "offset": offset,
                    "updated": time.time(),
                },
            )

        checkpoint_offset()
        with transcript.open(encoding="utf-8") as stream:
            stream.seek(offset)
            while True:
                next_offset = _process_available(stream, session)
                if next_offset != offset:
                    offset = next_offset
                    checkpoint_offset()
                if once or not _watch_enabled(session):
                    return 0
                time.sleep(0.25)


def _pid_running(value: object) -> bool:
    if not isinstance(value, int) or value <= 0:
        return False
    try:
        os.kill(value, 0)
        return True
    except OSError:
        return False


def start_bridge(session: str) -> int:
    checkpoint = _read_json(_checkpoint_path(session))
    if _pid_running(checkpoint.get("pid")):
        return 0
    environment = os.environ.copy()
    environment["SEMANTIC_DAG_STATE_DIR"] = str(_state_dir())
    subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "run", "--session", session],
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=sys.platform != "win32",
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("start", "run"):
        child = subparsers.add_parser(command)
        child.add_argument("--session", required=True)
        if command == "run":
            child.add_argument("--once", action="store_true")
            child.add_argument("--from-start", action="store_true")
    arguments = parser.parse_args()
    if arguments.command == "start":
        return start_bridge(arguments.session)
    return run_bridge(arguments.session, arguments.once, arguments.from_start)


if __name__ == "__main__":
    raise SystemExit(main())
