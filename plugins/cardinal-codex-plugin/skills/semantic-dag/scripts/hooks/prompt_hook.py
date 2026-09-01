#!/usr/bin/env python3
"""Keep Semantic DAG watch mode active across Codex prompt turns."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

STATE_DIR = Path(os.path.expanduser(os.environ.get("SEMANTIC_DAG_STATE_DIR", "~/.cardinal/state/semantic-dag")))
EMITTER = Path(__file__).parents[1] / "emit.py"
SKILL = Path(__file__).parents[2] / "SKILL.md"
SAFE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
DISABLE_RE = re.compile(r"^\s*(?:[$/])?semantic[- ]?dag\s+(?:off|stop|disable)\s*$", re.IGNORECASE)
WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'/-]*")
GENERIC_LABEL_RE = re.compile(r"\b(?:phase|stage|step|part|task)\s*[-:#]?\s*\d+\b", re.IGNORECASE)


def safe_id(value: str) -> str:
    return value if SAFE_RE.fullmatch(value) else "c-" + hashlib.sha1(value.encode()).hexdigest()[:16]


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def short_topic(prompt: str) -> str:
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9'_-]*", prompt)
    return " ".join(words[:6]) or "Continue watched task"


def goal_label(prompt: str) -> str:
    words = WORD_RE.findall(prompt)
    label = " ".join(words[:6])
    if len(WORD_RE.findall(label)) < 2:
        label = "Handle user request"
    if GENERIC_LABEL_RE.search(label):
        label = "Handle user request"
    trimmed = WORD_RE.findall(label)
    if len(trimmed) > 7:
        label = " ".join(trimmed[:7])
    return label


def current_turn(thread: str) -> int:
    dag = read_json(STATE_DIR / "threads" / safe_id(thread) / "dag.json")
    value = dag.get("turn", 1)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 1


def run_emit(thread: str, *arguments: str) -> None:
    environment = os.environ.copy()
    environment["SEMANTIC_DAG_NO_SERVER"] = "1"
    environment["SEMANTIC_DAG_NO_OPEN"] = "1"
    subprocess.run(
        [sys.executable, str(EMITTER), *arguments, "--thread", thread],
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=2,
        check=False,
    )


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return
    if not isinstance(payload, dict):
        return
    session = next((payload.get(key) for key in ("session_id", "sessionId", "sessionID") if payload.get(key)), None)
    if not isinstance(session, str):
        return
    binding = read_json(STATE_DIR / "bindings" / f"{safe_id(session)}.json")
    thread = binding.get("thread")
    if not isinstance(thread, str):
        return
    dag = read_json(STATE_DIR / "threads" / safe_id(thread) / "dag.json")
    if not dag.get("watch_mode"):
        return
    prompt = str(payload.get("prompt") or payload.get("user_prompt") or "").strip()
    if DISABLE_RE.fullmatch(prompt):
        run_emit(thread, "watch", "off")
        print(json.dumps({"systemMessage": "Semantic DAG watch mode disabled for this task."}))
        return
    run_emit(thread, "reset", short_topic(prompt))
    turn_n = current_turn(thread)
    goal_id = f"turn-{turn_n}-goal"
    run_emit(thread, "add", goal_id, "GOAL", goal_label(prompt), "--root")
    run_emit(thread, "activate", goal_id)
    context = (
        "Persistent Semantic DAG watch mode is active for this Codex task. "
        "The prompt hook already repainted the existing viewer; do not run `begin` or open a separate DAG thread. "
        f"Use the emitter at {EMITTER}. The hook already created and activated this turn's GOAL; add only distinct durable child nodes with "
        "`add <id> <TYPE> <label>` and `activate <id>`; valid types are GOAL, QUESTION, HYPOTHESIS, "
        "DECISION, WORK, EVIDENCE, and OUTCOME, and labels must be concrete 2–7 word phrases. "
        "Use stable IDs and connect nodes with decomposes_into, raises, tested_by, supported_by, "
        "refuted_by, resolved_by, based_on, leads_to, depends_on, produces, implements, validates, or supersedes. "
        "Keep commands, narration, and glossary concepts as `tool`, `note`, or `concept` metadata, not nodes. "
        "On every substantive turn that introduces domain language, attach 1–3 important non-obvious terms with `concept` so the Glossary is populated without filler. "
        "Native Codex progress commentary is mirrored onto the active node automatically; use `note` only for graph narration that was not sent to the user. "
        "Immediately before the final response, run `finish` with a factual one-line outcome. "
        f"Consult the full skill at {SKILL} only when subagent provenance or another edge case needs more detail. "
        "Watch mode remains active on later prompts until the user submits `semantic-dag off`."
    )
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        }
    }))


if __name__ == "__main__":
    main()
