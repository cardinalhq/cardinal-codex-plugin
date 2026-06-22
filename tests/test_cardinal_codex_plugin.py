"""Pytest suite for the cardinal Codex plugin — the final verification gate
for the port from cardinal-claude-plugin.

Covers (per docs/specs/codex-port.md, the authoritative porting contract):

  1. Pure functions ported verbatim from the Claude suite (logic unchanged):
     git-state _resolve_initiative / _detect_command / _canonical_repo, and
     cardinal-connect.derive_deployment_env.
  2. hooks/_codex_state.py: load_state / otlp_target / session_id.
  3. Hook behaviour via SourceFileLoader, HOME redirected to tmp_path:
     not-connected hooks silent-exit with no POST; connected hooks POST the
     right OTLP event with service.name=codex / agent.runtime=codex.
  4. connect/disconnect config.toml + cardinal.json round-trip; the shipped
     hooks/hooks.json template Codex auto-registers
     (formalised from /tmp/cardinal_codex_harness.py).

Pytest + stdlib only. No network: urllib is monkeypatched for every POST.
Each test isolates HOME via tmp_path so nothing touches the real ~/.codex.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
PLUGIN = REPO / "plugins" / "cardinal"
HOOKS_DIR = PLUGIN / "hooks"
BIN = PLUGIN / "bin"


# ---------------------------------------------------------------------------
# Module loaders
# ---------------------------------------------------------------------------

def _load_module(name: str, path: Path):
    """Load a .py hook module (or a hyphenated bin script) by path.

    Hooks add their own dir to sys.path at import time (`sys.path.insert(0,
    dirname)`), so sibling imports like `import _codex_state` resolve. We also
    ensure HOOKS_DIR is importable for any module that imports a sibling.
    """
    if str(HOOKS_DIR) not in sys.path:
        sys.path.insert(0, str(HOOKS_DIR))
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture
def git_state():
    return _load_module("codex_git_state", HOOKS_DIR / "git-state.py")


@pytest.fixture
def codex_state():
    return _load_module("codex_state_mod", HOOKS_DIR / "_codex_state.py")


@pytest.fixture
def connect():
    return _load_module("cardinal_connect", BIN / "cardinal-connect")


@pytest.fixture
def disconnect():
    return _load_module("cardinal_disconnect", BIN / "cardinal-disconnect")


# ---------------------------------------------------------------------------
# 1a. git-state _resolve_initiative  (ported verbatim from the Claude suite)
# ---------------------------------------------------------------------------

class TestResolveInitiative:
    def test_protected_branches_yield_research_with_no_name(self, git_state):
        for branch in ["main", "master", "develop", "trunk"]:
            name, itype = git_state._resolve_initiative(branch)
            assert name is None, branch
            assert itype == "research", branch

    def test_no_branch_yields_research_with_no_name(self, git_state):
        for sentinel in [None, "", "HEAD"]:
            name, itype = git_state._resolve_initiative(sentinel)
            assert name is None
            assert itype == "research"

    def test_recognized_prefixes_map_to_canonical_types(self, git_state):
        cases = [
            ("feat/outcomes-observability", "outcomes-observability", "feature"),
            ("feature/outcomes-observability", "outcomes-observability", "feature"),
            ("fix/login-crash", "login-crash", "bugfix"),
            ("bugfix/login-crash", "login-crash", "bugfix"),
            ("refactor/auth-token", "auth-token", "refactor"),
            ("infra/k8s-bump", "k8s-bump", "infra"),
            ("chore/k8s-bump", "k8s-bump", "infra"),
            ("research/data-pipeline-spike", "data-pipeline-spike", "research"),
            ("spike/data-pipeline-spike", "data-pipeline-spike", "research"),
            # conventional-but-uncanonical prefixes → closest enum member
            ("perf/logs-raw-wide-window-latency", "logs-raw-wide-window-latency", "feature"),
            ("cleanup/dead-flags", "dead-flags", "refactor"),
            ("test/flaky-suite-quarantine", "flaky-suite-quarantine", "infra"),
            ("tests/flaky-suite-quarantine", "flaky-suite-quarantine", "infra"),
            ("ci/release-pipeline", "release-pipeline", "infra"),
            ("build/esbuild-migration", "esbuild-migration", "infra"),
            ("deps/react-19-bump", "react-19-bump", "infra"),
            ("docs/install-guide", "install-guide", "infra"),
            ("doc/install-guide", "install-guide", "infra"),
        ]
        for branch, want_name, want_type in cases:
            name, itype = git_state._resolve_initiative(branch)
            assert name == want_name, branch
            assert itype == want_type, branch

    def test_prefix_match_is_case_insensitive(self, git_state):
        name, itype = git_state._resolve_initiative("Feat/foo-bar")
        assert name == "foo-bar"
        assert itype == "feature"

    def test_multi_segment_tail_kept_intact(self, git_state):
        name, itype = git_state._resolve_initiative("feat/multi-segment-name-keeps-going")
        assert name == "multi-segment-name-keeps-going"
        assert itype == "feature"

    def test_unrecognized_prefix_falls_back_to_feature(self, git_state):
        name, itype = git_state._resolve_initiative("rjha/some-thing")
        assert name == "rjha/some-thing"
        assert itype == "feature"

    def test_unprefixed_branch_falls_back_to_feature(self, git_state):
        name, itype = git_state._resolve_initiative("my-personal-work")
        assert name == "my-personal-work"
        assert itype == "feature"

    def test_worktree_noise_is_stripped_from_the_name(self, git_state):
        cases = [
            ("worktree-fix-1018-github-app-repo-picker", "github-app-repo-picker"),
            ("worktree-investigate-log-query-step", "investigate-log-query-step"),
            ("worktree-issue-862-split-auth-context", "split-auth-context"),
        ]
        for branch, want_name in cases:
            name, itype = git_state._resolve_initiative(branch)
            assert name == want_name, branch
            assert itype == "feature"

    def test_worktree_strip_applies_after_prefix_strip(self, git_state):
        name, itype = git_state._resolve_initiative(
            "fix/worktree-fix-1018-github-app-repo-picker"
        )
        assert name == "github-app-repo-picker"
        assert itype == "bugfix"

    def test_worktree_branch_with_no_real_segments_kept_verbatim(self, git_state):
        name, itype = git_state._resolve_initiative("worktree-fix-1018")
        assert name == "worktree-fix-1018"
        assert itype == "feature"

    def test_worktree_strip_is_idempotent(self, git_state):
        for clean in ["github-app-repo-picker", "test-in-pod", "fix-1018-something"]:
            name, _ = git_state._resolve_initiative(clean)
            assert name == clean
        once = git_state._strip_worktree_noise(
            "worktree-fix-1018-github-app-repo-picker"
        )
        assert git_state._strip_worktree_noise(once) == once

    def test_recognized_prefix_with_empty_tail_falls_back(self, git_state):
        name, itype = git_state._resolve_initiative("feat/")
        assert name == "feat/"
        assert itype == "feature"

    def test_type_is_always_from_closed_enum(self, git_state):
        for branch in [
            None, "", "HEAD", "main", "feat/x", "fix/x", "refactor/x",
            "infra/x", "chore/x", "research/x", "spike/x", "perf/x",
            "cleanup/x", "test/x", "ci/x", "build/x", "deps/x", "docs/x",
            "worktree-fix-1018-thing", "weird-branch", "user/scratchpad",
        ]:
            _name, itype = git_state._resolve_initiative(branch)
            assert itype in git_state._INITIATIVE_TYPES, branch

    def test_resolution_is_a_pure_function(self, git_state):
        for branch in ["feat/auth", "main", "perf/hot-path",
                       "worktree-fix-1018-github-app-repo-picker"]:
            a = git_state._resolve_initiative(branch)
            b = git_state._resolve_initiative(branch)
            assert a == b


# ---------------------------------------------------------------------------
# 1b. git-state _detect_command
# ---------------------------------------------------------------------------

class TestDetectCommand:
    def test_table_cases(self, git_state):
        cases = [
            ("/code-review", "code-review"),
            ("/code-review --fix high", "code-review"),
            ("  /verify", "verify"),
            ("/model claude-fable-5", "model"),
            ("/commit-commands:commit-push-pr now", "commit-commands:commit-push-pr"),
            ("<command-name>/deep-research</command-name> args follow", "deep-research"),
            ("<command-name>loop</command-name>", "loop"),
            ("fix the /etc/hosts parser", None),
            ("please run /code-review for me", None),
            ("plain prompt", None),
            ("", None),
            (None, None),
            ("/", None),
            ("/ leading space", None),
        ]
        for prompt, expected in cases:
            assert git_state._detect_command(prompt) == expected, prompt

    def test_args_never_leak(self, git_state):
        assert git_state._detect_command(
            "/deep-research acme corp acquisition plans"
        ) == "deep-research"


# ---------------------------------------------------------------------------
# 1c. git-state _canonical_repo
# ---------------------------------------------------------------------------

class TestCanonicalRepo:
    def test_ssh_github_form(self, git_state):
        assert git_state._canonical_repo(
            "git@github.com:cardinalhq/cardinal-codex-plugin.git"
        ) == "cardinalhq/cardinal-codex-plugin"

    def test_https_form_with_and_without_git_suffix(self, git_state):
        assert git_state._canonical_repo(
            "https://github.com/org/name.git"
        ) == "org/name"
        assert git_state._canonical_repo(
            "https://github.com/org/name"
        ) == "org/name"

    def test_host_agnostic(self, git_state):
        assert git_state._canonical_repo(
            "git@gitlab.example.com:team/proj.git"
        ) == "team/proj"

    def test_unparseable_is_none(self, git_state):
        assert git_state._canonical_repo("not a url") is None


# ---------------------------------------------------------------------------
# 1d. cardinal-connect.derive_deployment_env
# ---------------------------------------------------------------------------

class TestDeriveDeploymentEnv:
    def test_table(self, connect):
        cases = [
            ("https://app.cardinalhq.io", "prod"),
            ("https://dogfood.cardinalhq.io", "dogfood"),
            ("https://something.dogfood.example/x", "dogfood"),
            ("https://intake.cardinalhq.io", "cardinal"),
            ("https://acme.customer.example.com", "customer"),
        ]
        for host, want in cases:
            assert connect.derive_deployment_env(host) == want, host


# ---------------------------------------------------------------------------
# 2. _codex_state.py
# ---------------------------------------------------------------------------

@pytest.fixture
def home(tmp_path, monkeypatch):
    """Isolated HOME so nothing touches the real ~/.codex. Returns the
    tmp HOME; ~/.codex is NOT created (tests that need it create it)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_SESSION_ID", raising=False)
    return tmp_path


def _seed_state(home_dir: Path, **overrides) -> dict:
    """Write a realistic ~/.codex/cardinal.json connected state file."""
    codex = home_dir / ".codex"
    codex.mkdir(parents=True, exist_ok=True)
    state = {
        "schema_version": 1,
        "runtime": "codex",
        "ingest_endpoint": "https://otelhttp.intake.cardinalhq.io/",
        "ingest_api_header": "x-cardinalhq-api-key",
        "ingest_api_key": "ik_SECRET123",
        "resource_attributes": (
            "service.name=codex,agent.runtime=codex,"
            "deployment.environment=prod,user.email=dev@acme.com"
        ),
    }
    state.update(overrides)
    (codex / "cardinal.json").write_text(json.dumps(state))
    return state


def _fresh_codex_state(home_dir: Path):
    """Re-import _codex_state with STATE_PATH bound to the tmp HOME.

    The module captures `Path.home() / ".codex" / "cardinal.json"` at import
    time, so we rebind STATE_PATH after loading for the active HOME.
    """
    mod = _load_module("codex_state_fresh", HOOKS_DIR / "_codex_state.py")
    mod.STATE_PATH = home_dir / ".codex" / "cardinal.json"
    return mod


class TestCodexStateLoad:
    def test_absent_file_returns_empty(self, home):
        mod = _fresh_codex_state(home)
        assert mod.load_state() == {}

    def test_malformed_file_returns_empty(self, home):
        codex = home / ".codex"
        codex.mkdir(parents=True)
        (codex / "cardinal.json").write_text("{not json")
        mod = _fresh_codex_state(home)
        assert mod.load_state() == {}

    def test_empty_file_returns_empty(self, home):
        codex = home / ".codex"
        codex.mkdir(parents=True)
        (codex / "cardinal.json").write_text("   ")
        mod = _fresh_codex_state(home)
        assert mod.load_state() == {}

    def test_non_object_json_returns_empty(self, home):
        codex = home / ".codex"
        codex.mkdir(parents=True)
        (codex / "cardinal.json").write_text("[1, 2, 3]")
        mod = _fresh_codex_state(home)
        assert mod.load_state() == {}


class TestCodexStateOtlpTarget:
    def test_no_api_key_yields_none_endpoint(self, home):
        mod = _fresh_codex_state(home)
        # Even with an endpoint present, no api_key → not-connected.
        state = {"ingest_endpoint": "https://x/", "resource_attributes": ""}
        endpoint, headers, attrs = mod.otlp_target(state)
        assert endpoint is None
        # Defaults still applied to resource_attrs.
        assert attrs["service.name"] == "codex"
        assert attrs["agent.runtime"] == "codex"

    def test_not_connected_yields_none(self, home):
        mod = _fresh_codex_state(home)
        endpoint, headers, attrs = mod.otlp_target()
        assert endpoint is None
        assert headers == {}

    def test_connected_returns_endpoint_headers_and_attrs(self, home):
        _seed_state(home)
        mod = _fresh_codex_state(home)
        endpoint, headers, attrs = mod.otlp_target()
        assert endpoint == "https://otelhttp.intake.cardinalhq.io/"
        assert headers == {"x-cardinalhq-api-key": "ik_SECRET123"}
        assert attrs["service.name"] == "codex"
        assert attrs["agent.runtime"] == "codex"
        assert attrs["deployment.environment"] == "prod"
        assert attrs["user.email"] == "dev@acme.com"

    def test_resource_defaults_applied_when_string_omits_them(self, home):
        _seed_state(home, resource_attributes="deployment.environment=prod")
        mod = _fresh_codex_state(home)
        _endpoint, _headers, attrs = mod.otlp_target()
        # service.name / agent.runtime defaulted to codex when absent.
        assert attrs["service.name"] == "codex"
        assert attrs["agent.runtime"] == "codex"

    def test_custom_header_name_honored(self, home):
        _seed_state(home, ingest_api_header="x-custom-key", ingest_api_key="k1")
        mod = _fresh_codex_state(home)
        _endpoint, headers, _attrs = mod.otlp_target()
        assert headers == {"x-custom-key": "k1"}


class TestCodexStateSessionId:
    def test_prefers_payload(self, home, monkeypatch):
        mod = _fresh_codex_state(home)
        monkeypatch.setenv("CODEX_SESSION_ID", "env-sess")
        assert mod.session_id({"session_id": "payload-sess"}) == "payload-sess"

    def test_falls_back_to_env(self, home, monkeypatch):
        mod = _fresh_codex_state(home)
        monkeypatch.setenv("CODEX_SESSION_ID", "env-sess")
        assert mod.session_id({}) == "env-sess"

    def test_none_when_neither(self, home):
        mod = _fresh_codex_state(home)
        assert mod.session_id({}) is None


# ---------------------------------------------------------------------------
# 3. Hook behaviour — run each hook's main() with stdin/stdout redirected
# ---------------------------------------------------------------------------

class _PostCapture:
    """Stand-in for urllib.request.urlopen that records the POSTed request
    instead of hitting the network. Raises if a test expects no POST."""

    def __init__(self):
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)

        class _Resp:
            status = 200

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

            def read(self_inner):
                return b""

        return _Resp()

    @property
    def bodies(self):
        out = []
        for req in self.requests:
            data = req.data
            if isinstance(data, (bytes, bytearray)):
                data = data.decode("utf-8")
            out.append(json.loads(data))
        return out


def _raise_if_called(*a, **k):
    raise AssertionError("urlopen must not be called when not connected")


def _run_hook(hook_file: str, home: Path, payload: dict, monkeypatch,
              urlopen, extra_env=None, git_fake=None):
    """Load a hook module fresh, rebind its _codex_state.STATE_PATH to the
    tmp HOME, feed it `payload` on stdin, capture stdout, and run main().
    Returns (exit_code, stdout_text).

    `git_fake`: optional dict mapping a git-arg-tuple to its stdout, used to
    stub git-state.py's `_git` so the test never shells out to a real repo
    (the contract forbids running git in the suite).
    """
    import io

    mod_name = f"hook_{hook_file.replace('-', '_').replace('.py', '')}_{id(home)}"
    mod = _load_module(mod_name, HOOKS_DIR / hook_file)
    # Rebind the shared state module's STATE_PATH for this HOME. All hooks
    # import the same `_codex_state` object, so patch it on the one the hook
    # holds a reference to.
    mod._codex_state.STATE_PATH = home / ".codex" / "cardinal.json"

    if git_fake is not None and hasattr(mod, "_git"):
        def _fake_git(args, cwd, _table=git_fake):
            return _table.get(tuple(args))
        monkeypatch.setattr(mod, "_git", _fake_git)

    monkeypatch.setattr(mod.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    if extra_env:
        for k, v in extra_env.items():
            monkeypatch.setenv(k, v)

    code = 0
    try:
        mod.main()
    except SystemExit as e:
        code = e.code or 0
    return code, out.getvalue()


def _resource_attrs(body: dict) -> dict:
    attrs = body["resourceLogs"][0]["resource"]["attributes"]
    return {a["key"]: a["value"].get("stringValue") for a in attrs}


def _log_records(body: dict) -> list:
    return body["resourceLogs"][0]["scopeLogs"][0]["logRecords"]


def _event_names(body: dict) -> set:
    names = set()
    for rec in _log_records(body):
        for a in rec["attributes"]:
            if a["key"] == "event_name":
                names.add(a["value"].get("stringValue"))
    return names


# --- Not connected: every hook silent-exits 0 with no POST -----------------

class TestHooksNotConnected:
    @pytest.mark.parametrize("hook_file,payload", [
        ("git-state.py", {"session_id": "s1", "cwd": ".", "prompt": "hi"}),
        ("turn-usage.py", {"session_id": "s1", "transcript_path": "/tmp/x.jsonl"}),
        ("subagent-usage.py", {
            "session_id": "s1", "tool_name": "spawn_agent",
            "tool_response": {"tokens_used": 10},
        }),
        ("plan-state.py", {"session_id": "s1"}),
        ("plan-usage.py", {"session_id": "s1"}),
    ])
    def test_silent_exit_no_post(self, hook_file, payload, home, monkeypatch):
        # No ~/.codex/cardinal.json → otlp_target returns endpoint=None.
        code, out = _run_hook(hook_file, home, payload, monkeypatch,
                              urlopen=_raise_if_called)
        assert code == 0
        assert out == ""


# --- Connected: hooks POST the right OTLP event with codex resource attrs ---

class TestHooksConnected:
    def test_git_state_posts_git_state_event(self, home, monkeypatch, tmp_path):
        _seed_state(home)
        cap = _PostCapture()
        # Stub `_git` rather than shelling out — the porting contract forbids
        # running git in the suite. Supply canned head/branch/remote so the
        # hook proceeds to the POST.
        git_fake = {
            ("rev-parse", "HEAD"): "abc123def",
            ("rev-parse", "--abbrev-ref", "HEAD"): "feat/outcomes-observability",
            ("remote", "get-url", "origin"):
                "git@github.com:cardinalhq/cardinal-codex-plugin.git",
        }
        payload = {"session_id": "s1", "cwd": "/work",
                   "hook_event_name": "UserPromptSubmit", "prompt": "/verify now"}
        code, _out = _run_hook("git-state.py", home, payload, monkeypatch,
                              urlopen=cap, git_fake=git_fake)
        assert code == 0
        assert len(cap.requests) == 1
        req = cap.requests[0]
        assert req.full_url.endswith("/v1/logs")
        assert req.headers.get("X-cardinalhq-api-key") == "ik_SECRET123"
        body = cap.bodies[0]
        assert "cardinal.git_state" in _event_names(body)
        attrs = _resource_attrs(body)
        assert attrs["service.name"] == "codex"
        assert attrs["agent.runtime"] == "codex"
        # scope name is the codex plugin (codex-port.md §4)
        scope = body["resourceLogs"][0]["scopeLogs"][0]["scope"]
        assert scope["name"] == "cardinal-codex-plugin"
        # the command name was detected and stamped (never the args)
        flat = {
            a["key"]: a["value"].get("stringValue")
            for rec in _log_records(body) for a in rec["attributes"]
        }
        assert flat["cardinal.command"] == "verify"
        assert flat["cardinal.repo"] == "cardinalhq/cardinal-codex-plugin"
        assert flat["cardinal.branch"] == "feat/outcomes-observability"
        assert flat["cardinal.initiative.name"] == "outcomes-observability"
        assert flat["cardinal.initiative.type"] == "feature"

    def test_turn_usage_posts_turn_usage_event(self, home, monkeypatch, tmp_path):
        _seed_state(home)
        # Build a minimal transcript with one assistant usage record.
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text("\n".join([
            json.dumps({"message": {"role": "user", "content": "do it"}}),
            json.dumps({"message": {
                "role": "assistant",
                "model": "gpt-5.5",
                "usage": {"input_tokens": 100, "output_tokens": 20,
                          "cache_read_input_tokens": 5},
                "content": [
                    {"type": "tool_use", "name": "Read",
                     "input": {"file_path": "/repo/a.py"}},
                ],
            }}),
        ]) + "\n")
        cap = _PostCapture()
        payload = {"session_id": "s1", "transcript_path": str(transcript),
                   "hook_event_name": "Stop"}
        code, _out = _run_hook("turn-usage.py", home, payload, monkeypatch,
                              urlopen=cap)
        assert code == 0
        assert len(cap.requests) == 1
        body = cap.bodies[0]
        names = _event_names(body)
        assert "cardinal.turn_usage" in names
        assert "cardinal.turn_tool" in names
        attrs = _resource_attrs(body)
        assert attrs["service.name"] == "codex"
        assert attrs["agent.runtime"] == "codex"

    def test_subagent_usage_posts_event_with_codex_mapping(self, home, monkeypatch):
        _seed_state(home)
        cap = _PostCapture()
        payload = {
            "session_id": "s1",
            "tool_name": "spawn_agent",
            "tool_input": {"subagent_type": "code-explorer"},
            "tool_response": {"agent_thread_id": "th-9", "tokens_used": 4321},
            "transcript_path": "/tmp/none.jsonl",
        }
        code, _out = _run_hook("subagent-usage.py", home, payload, monkeypatch,
                              urlopen=cap)
        assert code == 0
        assert len(cap.requests) == 1
        body = cap.bodies[0]
        assert "cardinal.subagent_usage" in _event_names(body)
        flat = {
            a["key"]: a["value"]["stringValue"]
            for rec in _log_records(body) for a in rec["attributes"]
        }
        # Codex spawn_agent mapping (codex-port.md §5/§8):
        assert flat["agent_id"] == "th-9"          # ← agent_thread_id
        assert flat["total_tokens"] == "4321"      # ← tokens_used fallback
        assert flat["subagent_type"] == "code-explorer"
        attrs = _resource_attrs(body)
        assert attrs["agent.runtime"] == "codex"

    def test_subagent_usage_skips_non_spawn_agent(self, home, monkeypatch):
        _seed_state(home)
        payload = {"session_id": "s1", "tool_name": "Read",
                   "tool_response": {"tokens_used": 1}}
        code, out = _run_hook("subagent-usage.py", home, payload, monkeypatch,
                              urlopen=_raise_if_called)
        assert code == 0
        assert out == ""


# --- plan-state / plan-usage: runtime-only, "unknown", never Anthropic ------

class TestPlanHooksRuntimeOnly:
    def test_plan_state_emits_unknown_and_no_anthropic(self, home, monkeypatch):
        _seed_state(home)
        cap = _PostCapture()
        payload = {"session_id": "s1", "hook_event_name": "SessionStart"}
        code, _out = _run_hook("plan-state.py", home, payload, monkeypatch,
                              urlopen=cap)
        assert code == 0
        assert len(cap.requests) == 1
        # Never call api.anthropic.com.
        for req in cap.requests:
            assert "anthropic.com" not in req.full_url
        body = cap.bodies[0]
        assert "cardinal.plan_state" in _event_names(body)
        flat = {
            a["key"]: a["value"].get("stringValue")
            for rec in _log_records(body) for a in rec["attributes"]
        }
        assert flat["plan_type"] == "unknown"
        assert flat["rate_limit_tier"] == "unknown"
        attrs = _resource_attrs(body)
        assert attrs["agent.runtime"] == "codex"

    def test_plan_usage_is_noop_on_codex(self, home, monkeypatch):
        # The _plan_cache shim's read() returns None, so plan-usage silent-
        # exits before any POST (no Anthropic usage source in the Codex
        # runtime — codex-port.md §8).
        _seed_state(home)
        payload = {"session_id": "s1", "hook_event_name": "Stop"}
        code, out = _run_hook("plan-usage.py", home, payload, monkeypatch,
                              urlopen=_raise_if_called)
        assert code == 0
        assert out == ""


# ---------------------------------------------------------------------------
# 4. connect / disconnect round-trip (formalised from the manual harness)
# ---------------------------------------------------------------------------

BUNDLE = {
    "org": {"id": "org-uuid", "slug": "acme"},
    "user": {"email": "dev@acme.com"},
    "ingest": {
        "endpoint": "https://otelhttp.intake.cardinalhq.io/",
        "api_header": "x-cardinalhq-api-key",
        "api_key": "ik_SECRET123",
        "key_id": "ik1",
    },
    "mcp": {
        "url": "https://app.cardinalhq.io/api/orgs/org-uuid/mcp",
        "api_header": "x-cardinalhq-api-key",
        "api_key": "mk_SECRET456",
        "key_id": "mk1",
        "key_prefix": "mk_SECR",
        "created_at": "2026-06-21T00:00:00Z",
    },
    "limits": {
        "status_url": "https://app.cardinalhq.io/api/limits/status",
        "enabled": True,
    },
}


@pytest.fixture
def seeded_codex(home):
    """A tmp ~/.codex pre-seeded with unrelated config.toml keys and a
    supacode-style hooks.json entry, to test preservation across connect/
    disconnect."""
    codex = home / ".codex"
    codex.mkdir(parents=True, exist_ok=True)
    (codex / "config.toml").write_text(
        'model = "gpt-5.5"\n\n'
        '[projects."/x"]\ntrust_level = "trusted"\n\n'
        '[mcp_servers.other]\ncommand = "foo"\n'
    )
    (codex / "hooks.json").write_text(json.dumps({"hooks": {"SessionStart": [
        {"hooks": [{"type": "command", "command": "echo supacode", "timeout": 5}]}
    ]}}, indent=2))
    return codex


def _rebind_paths(mod, codex: Path):
    """Rebind a connect/disconnect module's module-level path constants to
    the tmp ~/.codex (they were captured from the real Path.home() at import)."""
    mod.CODEX_DIR = codex
    mod.CONFIG_PATH = codex / "config.toml"
    mod.STATE_PATH = codex / "cardinal.json"
    if hasattr(mod, "PENDING_PATH"):
        mod.PENDING_PATH = codex / "cardinal-pending.json"


def _all_hook_commands(hooks_json: Path) -> list:
    data = json.loads(hooks_json.read_text())
    return [
        h["command"]
        for grp in data["hooks"].values()
        for blk in grp
        for h in blk["hooks"]
    ]


class TestConnectWritesState:
    def test_state_schema_full_key_and_0600(self, seeded_codex, connect):
        _rebind_paths(connect, seeded_codex)
        dep = connect.derive_deployment_env("https://app.cardinalhq.io")
        assert dep == "prod"
        connect.write_state(BUNDLE, "https://app.cardinalhq.io", dep, True, False)

        state = json.loads((seeded_codex / "cardinal.json").read_text())
        assert state["runtime"] == "codex"
        assert state["mode"] == "telemetry-and-mcp"
        # FULL ingest key is stored (not a prefix) — codex-port.md §3.
        assert state["ingest_api_key"] == "ik_SECRET123"
        assert "service.name=codex" in state["resource_attributes"]
        assert "agent.runtime=codex" in state["resource_attributes"]
        assert "deployment.environment=prod" in state["resource_attributes"]
        assert state["limits"]["status_url"].endswith("/status")
        assert state["mcp_api_header"] == "x-cardinalhq-api-key"
        assert state["mcp_api_key"] == "mk_SECRET456"
        assert state["mcp_key_id"] == "mk1"
        # 0600
        mode = stat.S_IMODE(os.stat(seeded_codex / "cardinal.json").st_mode)
        assert oct(mode) == oct(0o600)


class TestConnectWritesConfigToml:
    def test_merge_preserves_unrelated_and_adds_otel_mcp(self, seeded_codex, connect):
        _rebind_paths(connect, seeded_codex)
        dep = connect.derive_deployment_env("https://app.cardinalhq.io")
        otel = connect._emit_otel_block(
            BUNDLE["ingest"]["endpoint"], dep,
            BUNDLE["ingest"]["api_header"], BUNDLE["ingest"]["api_key"],
        )
        mcp = connect._emit_mcp_block(
            BUNDLE["mcp"]["url"],
            BUNDLE["mcp"]["api_header"],
            BUNDLE["mcp"]["api_key"],
        )
        connect.write_config_toml({"otel": otel, "mcp_servers.cardinal": mcp})

        cfg = (seeded_codex / "config.toml").read_text()
        # unrelated content preserved verbatim
        assert 'model = "gpt-5.5"' in cfg
        assert '[projects."/x"]' in cfg
        assert "[mcp_servers.other]" in cfg
        # cardinal tables present
        assert "[otel]" in cfg
        assert "otlp-http" in cfg
        assert "[mcp_servers.cardinal]" in cfg
        assert "ik_SECRET123" in cfg  # otel headers carry the key
        assert "http_headers" in cfg
        assert "mk_SECRET456" in cfg
        # result must still parse as TOML
        import tomllib
        parsed = tomllib.loads(cfg)
        assert parsed["model"] == "gpt-5.5"
        assert "otel" in parsed
        assert parsed["mcp_servers"]["cardinal"]["url"] == BUNDLE["mcp"]["url"]
        assert parsed["mcp_servers"]["cardinal"]["http_headers"] == {
            "x-cardinalhq-api-key": "mk_SECRET456",
        }
        assert "bearer_token_env_var" not in parsed["mcp_servers"]["cardinal"]
        assert parsed["mcp_servers"]["other"]["command"] == "foo"
        mode = stat.S_IMODE(os.stat(seeded_codex / "config.toml").st_mode)
        assert oct(mode) == oct(0o600)


class TestPluginHooksTemplate:
    """Codex auto-registers the shipped hooks/hooks.json (codex-port.md §5);
    connect no longer merges hooks. The template's commands must be runnable
    as-is: relative to the plugin root (`./hooks/<script>.py`), pointing at
    scripts that exist and are executable. A bare name would fail with 127."""

    def test_commands_are_relative_to_plugin_root_and_exist(self):
        cmds = _all_hook_commands(HOOKS_DIR / "hooks.json")
        assert cmds, "template must declare hook commands"
        for c in cmds:
            assert c.startswith("./hooks/"), f"hook command not plugin-root-relative: {c}"
            script = PLUGIN / c[len("./"):]
            assert script.exists(), f"hook script missing: {c}"
            assert os.access(script, os.X_OK), f"hook script not executable: {c}"

    def test_connect_does_not_register_hooks(self, seeded_codex, connect):
        """connect must leave ~/.codex/hooks.json untouched — Codex owns
        registration, and a merge would double-fire every event."""
        _rebind_paths(connect, seeded_codex)
        before = (seeded_codex / "hooks.json").read_text()
        assert not hasattr(connect, "merge_hooks")
        # connect exposes no global-hooks path constant anymore
        assert not hasattr(connect, "HOOKS_PATH")
        assert (seeded_codex / "hooks.json").read_text() == before

    def test_spawn_agent_matcher_present(self):
        data = json.loads((HOOKS_DIR / "hooks.json").read_text())
        post = data["hooks"]["PostToolUse"]
        spawn_groups = [g for g in post if g.get("matcher") == "spawn_agent"]
        assert spawn_groups, "subagent hook must carry matcher=spawn_agent"
        assert any(
            "subagent-usage.py" in h["command"]
            for g in spawn_groups for h in g["hooks"]
        )


class TestConnectReachability:
    def test_ingest_401_retry_budget_is_60_seconds(self, connect, monkeypatch):
        sleeps = []

        monkeypatch.setattr(
            connect,
            "_ingest_probe_once",
            lambda _endpoint, _key: (False, "HTTP 401 — key invalid"),
        )
        monkeypatch.setattr(connect.time, "sleep", sleeps.append)

        ok, msg = connect.verify_ingest_reachable("https://otel.example", "ik_test")

        assert ok is False
        assert sleeps == list(connect._INGEST_PROBE_RETRY_SLEEPS)
        assert sum(sleeps) == 60.0
        assert "after 7 attempts over ~60s" in msg


class TestDisconnectRoundTrip:
    def test_removes_cardinal_tables_preserves_rest_and_leaves_hooks(
        self, seeded_codex, connect, disconnect,
    ):
        # First connect-side writes.
        _rebind_paths(connect, seeded_codex)
        dep = connect.derive_deployment_env("https://app.cardinalhq.io")
        otel = connect._emit_otel_block(
            BUNDLE["ingest"]["endpoint"], dep,
            BUNDLE["ingest"]["api_header"], BUNDLE["ingest"]["api_key"],
        )
        mcp = connect._emit_mcp_block(
            BUNDLE["mcp"]["url"],
            BUNDLE["mcp"]["api_header"],
            BUNDLE["mcp"]["api_key"],
        )
        connect.write_config_toml({"otel": otel, "mcp_servers.cardinal": mcp})

        hooks_before = (seeded_codex / "hooks.json").read_text()

        # Now disconnect.
        _rebind_paths(disconnect, seeded_codex)
        disconnect.remove_config_tables([disconnect.OTEL_TABLE, disconnect.MCP_TABLE])

        cfg = (seeded_codex / "config.toml").read_text()
        assert "[otel]" not in cfg
        assert "[mcp_servers.cardinal]" not in cfg
        # unrelated config survives
        assert 'model = "gpt-5.5"' in cfg
        assert '[projects."/x"]' in cfg
        assert "[mcp_servers.other]" in cfg

        # disconnect must not touch ~/.codex/hooks.json — Codex owns hook
        # registration; there are no cardinal entries there to strip.
        assert not hasattr(disconnect, "strip_cardinal_hooks")
        assert (seeded_codex / "hooks.json").read_text() == hooks_before
