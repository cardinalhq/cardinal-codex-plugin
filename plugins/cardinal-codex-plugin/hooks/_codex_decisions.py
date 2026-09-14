"""Codex-side decision capture: the sandbox handoff between the agent-run
CLI and the (unsandboxed) telemetry hook.

Why a handoff: Codex runs agent shell commands in the `workspace-write`
sandbox by default — network off, writable roots limited to cwd, /tmp and
$TMPDIR (openai/codex codex-rs/protocol/src/protocol.rs
SandboxPolicy::WorkspaceWrite / get_writable_roots_with_cwd) — so the CLI
can neither write ~/.codex/cardinal nor POST OTLP. Hooks are spawned by
Codex itself, outside the sandbox (codex-rs/hooks/src/engine/command_runner.rs
build_command).

`cardinal-decision record` only validates and prints one marker line, the
same wire format the Cursor adapter uses:

    cardinal-decision-record:v1 {"v":1,"session":...,"cwd":...,"id":...,
      "choice":...,"question":...,"why":...,"alt":[...],"by":...,
      "anchor":[...],"follows":[...],"refines":[...],"supersedes":[...]}

The PostToolUse handler (tool_name "Bash", tool_input.command,
tool_response = command output; codex-rs/core/src/tools/handlers/
unified_exec.rs post_unified_exec_tool_use_payload) is the ONLY emitter.
The decision is built from the real invocation's argv — parsed shell-aware
from tool_input.command with the same argparse spec the CLI uses — and the
marker is only confirmation that the CLI ran and validated it: a marker is
accepted only when it equals the argv-derived one. A spoofed marker
(`echo`, `cat` of a log, ...) that differs is ignored; one that matches is
harmless. Local work (validate, id, ledger) is synchronous; clusters and
the OTLP send run in one detached child. `record --emit` (a human in their
own terminal) records and emits directly and prints no marker.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import time
from pathlib import Path
from typing import Any, Callable, Optional

from cardinal_core import decisions, otlp
from cardinal_core.initiative import canonical_repo, git
from cardinal_core.paths import AgentPaths, read_json

MARKER = "cardinal-decision-record:v1 "
MARKER_VERSION = 1
SPEC_KEYS = (
    "session", "cwd", "id", "choice", "question", "why", "alt", "by", "anchor",
    "follows", "refines", "supersedes",
)
LIST_KEYS = ("alt", "anchor", "follows", "refines", "supersedes")
# Marker fields the argv fully determines (cwd comes from the CLI process;
# session only when --session was passed).
CONFIRM_KEYS = ("v", "id", "choice", "question", "why", "alt", "by", "anchor",
                "follows", "refines", "supersedes")
MAX_ANCHOR_SPEC = 500
EMIT_TIMEOUT_SEC = 3.0
SCOPE_NAME = "cardinal-codex-plugin"
CLI_NAME = "cardinal-decision"

CAPTURE_OFF_REPORT = (
    "Cardinal decision capture is off, so this decision was not recorded. "
    "Only the user can turn it on (`cardinal-decision on` in their own terminal)."
)


def codex_paths() -> AgentPaths:
    return AgentPaths(home=Path.home() / ".codex")


# --- shared argv spec ----------------------------------------------------------


def add_record_arguments(record: argparse.ArgumentParser) -> None:
    """`cardinal-decision record` flags — shared by the CLI and the hook's
    argv parser so both derive the same decision from the same command."""
    record.add_argument("--session", help="session id (supplied by the prompt hook; the hook uses "
                                          "the Codex session it observes)")
    record.add_argument("--choice", required=True, help="the option chosen, in a few words")
    record.add_argument("--question", help="the question this decision settles")
    record.add_argument("--why", help="one sentence on why this option won")
    record.add_argument("--alt", action="append", default=[], metavar="OPTION",
                        help="an option that was considered and rejected (repeatable)")
    record.add_argument("--by", choices=decisions.DECIDED_BY, default="agent",
                        help="who made the call (default: agent)")
    record.add_argument("--anchor", action="append", default=[], metavar="ANCHOR",
                        help="file, dir/, file::Symbol, or <kind>:<identifier>[@path] the decision governs (repeatable)")
    record.add_argument("--follows", action="append", default=[], metavar="ID",
                        help="an earlier decision this one only makes sense because of")
    record.add_argument("--refines", action="append", default=[], metavar="ID",
                        help="an earlier decision this one narrows")
    record.add_argument("--supersedes", action="append", default=[], metavar="ID",
                        help="an earlier decision this one replaces")
    record.add_argument("--id", help="decision id; reuse an existing id to revise that decision")
    record.add_argument("--emit", action="store_true",
                        help="record and emit directly (your own terminal only; not inside Codex)")


class _ArgvError(Exception):
    pass


class _RaisingParser(argparse.ArgumentParser):
    def error(self, message: str):  # type: ignore[override]
        raise _ArgvError(message)

    def exit(self, status: int = 0, message: Optional[str] = None):  # type: ignore[override]
        raise _ArgvError(message or "exited")


def parse_record_argv(argv: list[str]) -> argparse.Namespace:
    parser = _RaisingParser(prog=f"{CLI_NAME} record", add_help=False)
    add_record_arguments(parser)
    return parser.parse_args(argv)


def spec_from_args(args: argparse.Namespace, cwd: str, session: Optional[str]) -> dict[str, Any]:
    return {
        "session": session, "cwd": cwd, "id": args.id, "choice": args.choice,
        "question": args.question, "why": args.why, "alt": list(args.alt), "by": args.by,
        "anchor": list(args.anchor), "follows": list(args.follows),
        "refines": list(args.refines), "supersedes": list(args.supersedes),
    }


# --- marker ------------------------------------------------------------------


def normalize_spec(raw: Any) -> Optional[dict[str, Any]]:
    """Coerce a spec/marker dict to the spec shape; None when unusable."""
    if not isinstance(raw, dict) or raw.get("v", MARKER_VERSION) != MARKER_VERSION:
        return None
    spec: dict[str, Any] = {}
    for key in SPEC_KEYS:
        value = raw.get(key)
        if key in LIST_KEYS:
            spec[key] = [str(v) for v in value if isinstance(v, str)] if isinstance(value, list) else []
        else:
            spec[key] = value if isinstance(value, str) and value else None
    if not spec["choice"] or not spec["cwd"]:
        return None
    spec["by"] = spec["by"] or "agent"
    return spec


def marker_body(spec: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    """Cursor-compatible marker JSON from a validated decision: cleaned
    field values, `id` only when the agent chose one."""

    def ids(relation: str) -> list[str]:
        return [link["to"] for link in decision["links"] if link["relation"] == relation]

    return {
        "v": MARKER_VERSION,
        "session": spec.get("session"),
        "cwd": spec["cwd"],
        "id": decision["id"] if spec.get("id") else None,
        "choice": decision["choice"],
        "question": decision["question"],
        "why": decision["rationale"],
        "alt": decision["alternatives"],
        "by": decision["decided_by"],
        "anchor": spec["anchor"],
        "follows": ids("follows_from"),
        "refines": ids("refines"),
        "supersedes": ids("supersedes"),
    }


def marker_line(spec: dict[str, Any], decision: dict[str, Any]) -> str:
    return MARKER + json.dumps(marker_body(spec, decision), separators=(",", ":"), ensure_ascii=True)


def extract_markers(text: str) -> list[dict[str, Any]]:
    """Every JSON object on a line starting with the marker, in order."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith(MARKER):
            continue
        try:
            body = json.loads(line[len(MARKER):])
        except ValueError:
            continue
        if isinstance(body, dict):
            out.append(body)
    return out


def _strings(node: Any) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [s for v in node.values() for s in _strings(v)]
    if isinstance(node, list):
        return [s for v in node for s in _strings(v)]
    return []


# --- invocation discovery --------------------------------------------------------

_SHELL_OPERATORS = frozenset({";", "&&", "||", "|", "|&", "&", "(", ")", ";;", "{", "}"})
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_PYTHON_RE = re.compile(r"^python(\d+(\.\d+)*)?$")
_PYTHON_VALUE_OPTIONS = frozenset({"-X", "-W", "--check-hash-based-pycs"})
_PYTHON_NOT_SCRIPT = frozenset({"-c", "-m", "-"})
_COMMAND_PREFIXES = frozenset({"exec", "command", "env", "nohup", "time"})
_REDIRECT_RE = re.compile(r"^[<>&]+$")
_CONTINUATION_RE = re.compile(r"\\\r?\n")
_MENTION_RE = re.compile(r"cardinal-decision['\"]?\s+record\b")


def find_record_invocations(command: str) -> list[list[str]]:
    """argv (the tokens after `record`) of every `cardinal-decision record`
    the shell command runs in command position — directly, via a path, after
    env assignments, or through `python3 [options]`. Backslash-continued
    lines are joined first. Forms that hide the call inside a string (e.g.
    `bash -lc "..."`) are not parsed."""
    out: list[list[str]] = []
    for line in _CONTINUATION_RE.sub(" ", command).splitlines():
        try:
            lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
            # Split only on whitespace and shell punctuation; otherwise
            # `svc/a.py::Name`, `config:X@path` etc. break into pieces.
            lexer.whitespace_split = True
            tokens = list(lexer)
        except ValueError:
            continue
        at_command = True
        i = 0
        while i < len(tokens):
            token = tokens[i]
            if token in _SHELL_OPERATORS:
                at_command = True
                i += 1
                continue
            if not at_command:
                i += 1
                continue
            if _ASSIGNMENT_RE.match(token) or token in _COMMAND_PREFIXES:
                i += 1
                continue
            at_command = False
            j = i
            if _PYTHON_RE.match(os.path.basename(token)):
                j += 1
                while j < len(tokens) and tokens[j].startswith("-") and tokens[j] not in _SHELL_OPERATORS:
                    if tokens[j] in _PYTHON_NOT_SCRIPT:
                        break
                    j += 2 if tokens[j] in _PYTHON_VALUE_OPTIONS else 1
            if (j + 1 < len(tokens) and os.path.basename(tokens[j]) == CLI_NAME
                    and tokens[j + 1] == "record"):
                k = j + 2
                argv: list[str] = []
                while k < len(tokens) and tokens[k] not in _SHELL_OPERATORS:
                    if _REDIRECT_RE.match(tokens[k]):
                        if argv and argv[-1].isdigit():
                            argv.pop()  # the fd of `2>&1`
                        k += 2  # the redirect and its target
                        continue
                    argv.append(tokens[k])
                    k += 1
                out.append(argv)
                i = k
                continue
            i += 1
    return out


def mentions_record(command: str) -> bool:
    return bool(_MENTION_RE.search(command))


# --- planning a PostToolUse payload -------------------------------------------------


def plan_post_tool_use(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """(specs to record, not-recorded reasons) for one PostToolUse payload.
    Both empty → the tool call has nothing to do with decision capture.

    Each parsed invocation must be confirmed by a marker equal to the
    decision its argv produces; each marker confirms at most one
    invocation. Specs are built from argv, never from marker contents."""
    if payload.get("tool_name") != "Bash":
        return [], []
    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str):
        return [], []
    markers = [m for text in _strings(payload.get("tool_response")) for m in extract_markers(text)]
    invocations = find_record_invocations(command)
    if not invocations:
        if markers and mentions_record(command):
            return [], ["Cardinal did NOT record the decision: this command form isn't supported "
                        "(run cardinal-decision record directly, not inside `bash -c` or a string)."]
        return [], []

    specs: list[dict[str, Any]] = []
    reasons: list[str] = []
    unused = list(markers)
    for argv in invocations:
        try:
            args = parse_record_argv(argv)
        except _ArgvError as err:
            reasons.append(f"Cardinal did NOT record a decision: couldn't parse its arguments ({err}).")
            continue
        if args.emit:
            continue  # the CLI recorded and emitted itself; no marker expected
        match = None
        for marker in unused:
            cwd = marker.get("cwd")
            if not isinstance(cwd, str) or not cwd:
                continue
            spec = spec_from_args(args, cwd, args.session or marker.get("session"))
            try:
                expected = marker_body(spec, preview(spec))
            except decisions.DecisionError:
                break
            if all(marker.get(k) == expected[k] for k in CONFIRM_KEYS) and (
                args.session is None or marker.get("session") == args.session
            ):
                match = (marker, spec)
                break
        if match is None:
            try:
                preview(spec_from_args(args, str(payload.get("cwd") or os.getcwd()), args.session))
                why = ("no matching confirmation from the CLI in the command output "
                       "(the command failed, its output was truncated, or it was altered)")
            except decisions.DecisionError as err:
                why = str(err)
            reasons.append(f"Cardinal did NOT record decision {args.choice!r}: {why}.")
            continue
        unused.remove(match[0])
        specs.append(match[1])
    return specs, reasons


# --- validation + local recording --------------------------------------------------


_GIT_FACTS: dict[str, tuple[Optional[str], Optional[str], Optional[str], Optional[str]]] = {}


def _git_facts(cwd: str) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """Memoized per process: one hook call recording several decisions in
    the same cwd runs git once."""
    if cwd not in _GIT_FACTS:
        _GIT_FACTS[cwd] = _git_facts_uncached(cwd)
    return _GIT_FACTS[cwd]


def _git_facts_uncached(cwd: str) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    repo_root = git(["rev-parse", "--show-toplevel"], cwd)
    if not repo_root:
        return None, None, None, None
    head_sha = git(["rev-parse", "HEAD"], cwd)
    branch = git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    repo = canonical_repo(git(["remote", "get-url", "origin"], cwd))
    return repo_root, head_sha, branch, repo


def build_from_spec(
    spec: dict[str, Any], ledger: list[dict[str, Any]], repo_root: Optional[str],
) -> dict[str, Any]:
    anchors = [decisions.parse_anchor(a, repo_root, spec["cwd"]) for a in spec["anchor"]]
    return decisions.build_decision(
        choice=spec["choice"],
        question=spec["question"],
        rationale=spec["why"],
        decided_by=spec["by"],
        alternatives=spec["alt"],
        follows_from=spec["follows"],
        refines=spec["refines"],
        supersedes=spec["supersedes"],
        anchors=anchors,
        decision_id=spec["id"],
        existing=ledger,
    )


def preview(spec: dict[str, Any]) -> dict[str, Any]:
    """Validate with no side effects and no ~/.codex access: anchors parsed
    without a repo root, no ledger — the hook re-validates against both."""
    spec = dict(spec, anchor=[a[:MAX_ANCHOR_SPEC] for a in spec["anchor"]][: decisions.MAX_ANCHORS])
    return build_from_spec(spec, [], None)


PrLookup = Callable[[str, Optional[str], Optional[str]], "tuple[Optional[int], Optional[str]]"]


def record_local(
    paths: AgentPaths, spec: dict[str, Any], session_id: str, pr_lookup: PrLookup,
) -> dict[str, Any]:
    """Synchronous, local-only half: git facts, validation against the
    ledger (final id), PR via `pr_lookup`, ledger write. Returns the entry
    the background emitter needs. Raises DecisionError / OSError."""
    cwd = spec["cwd"]
    repo_root, head_sha, branch, repo = _git_facts(cwd)
    ledger = decisions.read_ledger(paths.runtime_dir, session_id)
    decision = build_from_spec(spec, ledger, repo_root)
    pr_number, pr_url = pr_lookup(cwd, repo, branch)
    known = {entry["id"] for entry in ledger}
    unknown = [link["to"] for link in decision["links"] if link["to"] not in known]
    decisions.record_in_ledger(paths.runtime_dir, session_id, decision)
    return {
        "session_id": session_id, "decision": decision, "repo_root": repo_root,
        "head_sha": head_sha, "branch": branch, "repo": repo,
        "pr_number": pr_number, "pr_url": pr_url, "unknown_links": unknown,
        "ts_ns": time.time_ns(),
    }


def resource_attrs(paths: AgentPaths, plugin_version: str) -> dict[str, Any]:
    state = paths.read_state()
    return otlp.resource_attrs(
        service_name="codex",
        agent_runtime="codex",
        deployment_environment=state.get("deployment_environment"),
        user_email=state.get("user_email"),
        org=state.get("org_slug") or state.get("org_id"),
        plugin_version=plugin_version,
    )


def bounded_code_clusters(
    anchors: list[dict[str, str]], repo_root: Optional[str], head_sha: Optional[str],
    cache: Path, timeout: float,
) -> tuple[list[str], Optional[str]]:
    """decisions.code_clusters with a caller-set `git ls-tree` budget."""
    paths = [(a["path"], a.get("kind") == "directory") for a in anchors if a.get("path") is not None]
    if not paths or not repo_root or not head_sha:
        return [], None
    loaded = decisions.load_domains(repo_root, head_sha, cache, timeout=timeout)
    if loaded is None:
        return [], None
    domains, scheme = loaded
    ids: list[str] = []
    for path, is_dir in paths:
        for cluster_id in decisions.match_clusters(domains, path, is_dir):
            if cluster_id not in ids:
                ids.append(cluster_id)
    return ids[: decisions.MAX_CLUSTERS], scheme


def emit_entries(
    paths: AgentPaths, entries: list[dict[str, Any]], plugin_version: str,
    *, cluster_timeout: float = 5.0, emit_timeout: float = EMIT_TIMEOUT_SEC,
) -> list[list[str]]:
    """Network half (background child, or --emit): D18 clusters + one OTLP
    post for all entries. Returns each entry's clusters."""
    cache = decisions.cache_dir(paths.runtime_dir)
    records = []
    all_clusters = []
    for entry in entries:
        decision = entry["decision"]
        clusters, scheme = bounded_code_clusters(
            decision["anchors"], entry.get("repo_root"), entry.get("head_sha"), cache, cluster_timeout)
        all_clusters.append(clusters)
        attrs = decisions.decision_attributes(
            session_id=entry["session_id"], decision=decision,
            code_clusters=clusters, cluster_scheme=scheme,
            repo=entry.get("repo"), branch=entry.get("branch"), head_sha=entry.get("head_sha"),
            pr_number=entry.get("pr_number"), pr_url=entry.get("pr_url"),
        )
        records.append(otlp.log_record(decisions.DECISION_EVENT, attrs, int(entry.get("ts_ns") or time.time_ns())))
    connection = otlp.connection_from_paths(paths)
    if connection is not None and records:
        otlp.emit_records(
            records, connection, resource_attrs(paths, plugin_version),
            scope_name=SCOPE_NAME, scope_version=plugin_version, timeout=emit_timeout,
        )
    return all_clusters


def report_line(entry: dict[str, Any], connected: bool, clusters: Optional[list[str]] = None,
                prefix: str = "Cardinal recorded decision") -> str:
    decision = entry["decision"]
    tags = [f"PR #{entry['pr_number']}"] if entry.get("pr_number") else []
    if clusters:
        tags.append("clusters " + ", ".join(clusters))
    detail = f" ({'; '.join(tags)})" if tags else ""
    lines = [f"{prefix} {decision['id']}: {decision['choice']}{detail}"]
    if entry.get("unknown_links"):
        lines.append(
            f"Note: no earlier decision in this session has id {', '.join(entry['unknown_links'])}; "
            "the link was kept as given."
        )
    if not connected:
        lines.append("Cardinal telemetry isn't connected, so it was only saved locally.")
    return "\n".join(lines)


# --- hook registration -------------------------------------------------------


def post_tool_use_registered(paths: AgentPaths) -> bool:
    """True when ~/.codex/hooks.json has the managed PostToolUse handler
    that performs the hook-side emission."""
    groups = read_json(paths.home / "hooks.json").get("hooks", {})
    groups = groups.get("PostToolUse") if isinstance(groups, dict) else None
    if not isinstance(groups, list):
        return False
    for group in groups:
        handlers = group.get("hooks") if isinstance(group, dict) else None
        for handler in handlers if isinstance(handlers, list) else []:
            command = str(handler.get("command") or "") if isinstance(handler, dict) else ""
            if "cardinal-codex-plugin" in command and "PostToolUse" in command:
                return True
    return False
