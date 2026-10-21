"""Non-blocking dependency observations at the real Claude hook boundary."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import threading
from contextlib import closing
from pathlib import Path

import pytest

from brigade import graphtrail_delta
from brigade.claude_hooks import dependencies, package, runtime
from brigade.install import install_selection
from brigade.selection import Selection


@pytest.fixture
def inherited_cache(tmp_path: Path, monkeypatch) -> Path:
    cache = tmp_path / "unrelated-cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    return cache


@pytest.fixture
def target(tmp_path: Path, monkeypatch, inherited_cache) -> Path:
    target = tmp_path / "repo"
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    assert install_selection(target, Selection(depth="repo", harnesses=["claude"], owner="claude", includes=[])) == 0
    (target / "api.py").write_text("def serve(): pass\n")
    (target / "caller.py").write_text("from api import serve\nserve()\n")
    (target / "tests").mkdir()
    (target / "tests/test_api.py").write_text("from api import serve\ndef test_api(): assert serve()\n")
    monkeypatch.setattr(runtime.heartbeat, "on_event", lambda *args: None)
    monkeypatch.setattr(runtime.presence, "emit_presence", lambda *args: None)
    monkeypatch.setattr(runtime, "_run_recall", lambda *args: "")
    monkeypatch.setattr(runtime, "_run_brief", lambda *args: "work brief")
    return target


def event(target: Path, name: str, **fields):
    return runtime.handle_payload(name, {"session_id": "session-one", "cwd": str(target), **fields})


def graph(target: Path, monkeypatch, **overrides):
    db = target / ".graphtrail" / "graphtrail.db"
    db.parent.mkdir()
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("CREATE TABLE files(path TEXT, content_hash TEXT)")
        conn.executemany(
            "INSERT INTO files VALUES (?, ?)",
            [
                (path.relative_to(target).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest())
                for path in target.rglob("*")
                if path.suffix in {".py", ".astro"}
            ],
        )
    monkeypatch.setattr(graphtrail_delta, "_graphtrail_bin", lambda: "graphtrail")
    report = {
        "changed_files": ["api.py"],
        "missing_files": [],
        "affected_tests": [{"file_path": "tests/test_api.py", "min_hops": 1}],
        "impacted_files": [{"file_path": "caller.py", "min_hops": 1}],
        "truncated": False,
        **overrides,
    }
    calls = []

    def run(binary, db_path, command, *extra, **kwargs):
        calls.append((command, extra))
        return {
            "returncode": 0,
            "timed_out": False,
            "stdout": json.dumps(report) if command == "affected" else "synced",
        }

    monkeypatch.setattr(graphtrail_delta, "_run_graphtrail", run)
    return calls


@pytest.mark.parametrize("operation", ["fixture", "matching", "stale", "missing_file"])
def test_graph_connections_close_after_use(target, monkeypatch, operation):
    connections = []
    connect = sqlite3.connect

    def tracked_connect(*args, **kwargs):
        conn = connect(*args, **kwargs)
        connections.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, "connect", tracked_connect)
    graph(target, monkeypatch)
    if operation != "fixture":
        connections.clear()
        if operation == "stale":
            (target / "caller.py").write_text("changed\n")
        elif operation == "missing_file":
            (target / "caller.py").unlink()
        db = target / ".graphtrail" / "graphtrail.db"
        if operation == "missing_file":
            with pytest.raises(FileNotFoundError):
                dependencies._content_matches_index(target, db, threading.Event())
        else:
            assert dependencies._content_matches_index(target, db, threading.Event()) is (operation == "matching")
    assert len(connections) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")


@pytest.mark.parametrize("seen", ["unread", "read", "brief"])
@pytest.mark.parametrize("tool,key", [("Edit", "file_path"), ("Write", "file_path"), ("NotebookEdit", "notebook_path")])
def test_explicit_write_records_unseen_callers_without_denying(target, monkeypatch, seen, tool, key):
    calls = graph(target, monkeypatch)
    if seen == "brief":
        monkeypatch.setattr(
            runtime,
            "_run_brief",
            lambda *args: (
                "## Code graph context (GraphTrail, read-only)\n_schema v1: 1 entry points - 1 edges_\n- caller.py\n- tests/test_api.py\n"
            ),
        )
    event(target, "SessionStart")
    if seen == "read":
        for path in ("caller.py", "tests/test_api.py"):
            event(target, "PostToolUse", tool_name="Read", tool_input={"file_path": str(target / path)})
    result = event(target, "PreToolUse", tool_name=tool, tool_input={key: str(target / "api.py")})
    assert result is None or "permissionDecision" not in result.get("hookSpecificOutput", {})
    state = runtime.read_session_state(target, "session-one")
    check = state["dependency_checks"][-1]
    assert check["file_path"] == "api.py"
    assert check["unseen_dependents"] == (["caller.py", "tests/test_api.py"] if seen == "unread" else [])
    assert calls == [("sync", ()), ("affected", ("api.py",))]
    # A subsequent event must preserve the observation rather than normalize it away.
    event(target, "PostToolUse", tool_name=tool, tool_input={key: str(target / "api.py")})
    assert runtime.read_session_state(target, "session-one")["dependency_checks"] == state["dependency_checks"]


def test_managed_hooks_deliver_reads():
    for groups in (package.managed_groups(), package.managed_user_groups(Path("hook.sh"))):
        assert any("Read" in group["matcher"].split("|") for group in groups["PostToolUse"])


def test_stalled_content_read_fails_open_before_hook_timeout(target, monkeypatch):
    calls = graph(target, monkeypatch)
    event(target, "SessionStart")
    monkeypatch.setattr(dependencies, "CONTENT_TIMEOUT_SECONDS", 0.02, raising=False)
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    original_open = Path.open
    results = []

    def stalled_open(path, *args, **kwargs):
        if path == target / "caller.py":
            entered.set()
            assert release.wait(5)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", stalled_open)

    def write():
        try:
            results.append(event(target, "PreToolUse", tool_name="Edit", tool_input={"file_path": "api.py"}))
        finally:
            finished.set()

    worker = threading.Thread(target=write)
    worker.start()
    try:
        assert entered.wait(2)
        assert finished.wait(1), "a stalled content read consumed the hook worker budget"
        check = runtime.read_session_state(target, "session-one")["dependency_checks"][-1]
        assert check["unseen_dependents"] is None
        assert check["reason"] == "content_check_incomplete"
        assert results == [None]
        assert calls == [("sync", ())]
    finally:
        release.set()
        worker.join(2)


@pytest.mark.parametrize(
    "failure,reason",
    [
        ("no_index", "no_index"),
        ("no_binary", "graph_unavailable"),
        ("sync_failed", "sync_failed"),
        ("sync_timeout", "sync_timed_out"),
        ("query_timeout", "affected_timed_out"),
        ("truncated", "incomplete_graph_report"),
        ("missing_file", "file_outside_coverage"),
        ("malformed", "graph_error"),
    ],
)
def test_unusable_graph_records_null_and_reason_without_denying(target, monkeypatch, failure, reason):
    graph(
        target,
        monkeypatch,
        truncated=failure == "truncated",
        missing_files=["api.py"] if failure == "missing_file" else [],
    )
    original = graphtrail_delta._run_graphtrail
    if failure == "no_index":
        (target / ".graphtrail" / "graphtrail.db").unlink()
    elif failure == "no_binary":
        monkeypatch.setattr(graphtrail_delta, "_graphtrail_bin", lambda: None)
    elif failure in {"sync_failed", "sync_timeout", "query_timeout", "malformed"}:

        def run(binary, db_path, command, *extra, **kwargs):
            if command == "sync" and failure in {"sync_failed", "sync_timeout"}:
                return {"returncode": 1, "timed_out": failure == "sync_timeout"}
            if command == "affected" and failure == "query_timeout":
                return {"returncode": 124, "timed_out": True}
            if command == "affected" and failure == "malformed":
                return {"returncode": 0, "stdout": "broken JSON"}
            return original(binary, db_path, command, *extra, **kwargs)

        monkeypatch.setattr(graphtrail_delta, "_run_graphtrail", run)
    result = event(target, "PreToolUse", tool_name="Edit", tool_input={"file_path": "api.py"})
    assert result is None or "permissionDecision" not in result.get("hookSpecificOutput", {})
    check = runtime.read_session_state(target, "session-one")["dependency_checks"][-1]
    assert check["unseen_dependents"] is None
    assert check["reason"] == reason


def test_read_paths_are_repo_relative_and_do_not_cross_task_boundaries(target, monkeypatch):
    graph(target, monkeypatch)
    subdir = target / "subdir"
    subdir.mkdir()
    runtime.handle_payload(
        "PostToolUse",
        {
            "session_id": "session-one",
            "cwd": str(subdir),
            "tool_name": "Read",
            "tool_input": {"file_path": "../caller.py"},
        },
    )
    state = runtime.read_session_state(target, "session-one")
    assert state["read_files"] == ["caller.py"]
    event(target, "PostToolUse", tool_name="Read", tool_input={"file_path": str(target.parent / "outside.py")})
    assert runtime.read_session_state(target, "session-one")["read_files"] == ["caller.py"]
    event(target, "SessionStart", source="clear")
    event(target, "PreToolUse", tool_name="Edit", tool_input={"file_path": "api.py"})
    assert runtime.read_session_state(target, "session-one")["dependency_checks"][-1]["unseen_dependents"] == [
        "caller.py",
        "tests/test_api.py",
    ]


def test_elided_graph_brief_does_not_mark_files_seen(target, monkeypatch):
    graph(target, monkeypatch)
    monkeypatch.setenv("BRIGADE_HOOK_MAX_CHARS", "200")
    monkeypatch.setattr(
        runtime,
        "_run_brief",
        lambda *args: (
            "## Code graph context (GraphTrail, read-only)\n_schema v1: 1 entry points - 1 edges_\n"
            + "x" * 9000
            + "\n- caller.py\n"
        ),
    )
    result = event(target, "SessionStart")
    assert "caller.py" not in result["hookSpecificOutput"]["additionalContext"]
    event(target, "PreToolUse", tool_name="Edit", tool_input={"file_path": "api.py"})
    assert (
        "caller.py" in runtime.read_session_state(target, "session-one")["dependency_checks"][-1]["unseen_dependents"]
    )


def test_dependency_check_forces_refresh_when_stat_metadata_could_be_unchanged(target, monkeypatch):
    graph(target, monkeypatch)
    caller = target / "caller.py"
    caller.write_text(caller.read_text().replace("serve()", "other()"))
    db = target / ".graphtrail/graphtrail.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE files SET content_hash = ? WHERE path = 'caller.py'",
            (hashlib.sha256(caller.read_bytes()).hexdigest(),),
        )
    before = caller.stat()
    caller.write_text(caller.read_text().replace("other()", "serve()"))
    os.utime(caller, ns=(before.st_atime_ns, before.st_mtime_ns))
    original = graphtrail_delta._run_graphtrail
    forced = False

    def run(binary, db_path, command, *extra, **kwargs):
        nonlocal forced
        if command == "sync":
            forced = "--force" in extra
            if forced:
                with sqlite3.connect(db) as conn:
                    conn.execute(
                        "UPDATE files SET content_hash = ? WHERE path = 'caller.py'",
                        (hashlib.sha256(caller.read_bytes()).hexdigest(),),
                    )
        result = original(binary, db_path, command, *extra, **kwargs)
        # The engine may keep the old call graph if a same-size edit preserves mtime.
        if command == "affected" and not forced:
            report = json.loads(result["stdout"])
            report.update(impacted_files=[], affected_tests=[])
            result["stdout"] = json.dumps(report)
        return result

    monkeypatch.setattr(graphtrail_delta, "_run_graphtrail", run)
    event(target, "PreToolUse", tool_name="Edit", tool_input={"file_path": "api.py"})
    assert runtime.read_session_state(target, "session-one")["dependency_checks"][-1]["unseen_dependents"] == [
        "caller.py",
        "tests/test_api.py",
    ]


@pytest.mark.parametrize("caller_path", ["caller.py", "src/pages/index.astro"])
def test_native_graph_sections_mark_only_injected_paths_seen(target, monkeypatch, caller_path):
    caller = target / caller_path
    if caller.suffix == ".astro":
        caller.parent.mkdir(parents=True, exist_ok=True)
        caller.write_text("---\nimport { serve } from '../../api';\nserve();\n---\n")
    graph(target, monkeypatch, impacted_files=[{"file_path": caller_path, "min_hops": 1}])
    monkeypatch.setattr(
        runtime,
        "_run_brief",
        lambda *args: (
            "## Code graph context (GraphTrail, read-only)\n"
            "Task: inspect outside.py\n"
            "_schema v1 - 1 entry points - 0 callers - 0 callees - 1 related files_\n"
            f"## Entry points\n- caller - {caller_path}:2\n"
            "## Related files\n- tests/test_api.py\n"
            "## Next task\n- outside.py\n"
        ),
    )
    event(target, "SessionStart")
    assert runtime.read_session_state(target, "session-one")["brief_files"] == [caller_path, "tests/test_api.py"]
    event(target, "PreToolUse", tool_name="Edit", tool_input={"file_path": "api.py"})
    assert runtime.read_session_state(target, "session-one")["dependency_checks"][-1]["unseen_dependents"] == []


@pytest.mark.parametrize("source", ["resume", "compact", "prompt"])
def test_late_brief_cannot_mark_files_seen_in_new_task(target, monkeypatch, source):
    graph(target, monkeypatch)
    event(target, "SessionStart", source="startup")
    if source == "resume":
        state = runtime.read_session_state(target, "session-one")
        state["briefed"] = False
        runtime.write_session_state(target, "session-one", state)
    elif source == "prompt":
        event(target, "PreCompact", trigger="auto")
    interleaved = False

    def brief(*args):
        nonlocal interleaved
        if interleaved:
            return "new task brief"
        interleaved = True
        event(target, "SessionStart", source="clear")
        return (
            "## Code graph context (GraphTrail, read-only)\n"
            "_schema v1: 1 entry points - 1 edges_\n- caller.py\n- tests/test_api.py\n"
        )

    monkeypatch.setattr(runtime, "_run_brief", brief)
    result = event(target, "UserPromptSubmit" if source == "prompt" else "SessionStart", source=source)
    assert "caller.py" in result["hookSpecificOutput"]["additionalContext"]
    assert not runtime.read_session_state(target, "session-one").get("brief_files")
    result = event(target, "PreToolUse", tool_name="Edit", tool_input={"file_path": "api.py"})
    check = runtime.read_session_state(target, "session-one")["dependency_checks"][-1]
    assert check["unseen_dependents"] == ["caller.py", "tests/test_api.py"]
    assert "caller.py" in result["hookSpecificOutput"]["additionalContext"]


@pytest.mark.parametrize("interleaving", ["read", "write", "new_task"])
def test_graph_query_cannot_overwrite_observations_from_overlapping_hooks(target, monkeypatch, interleaving):
    graph(target, monkeypatch)
    event(target, "SessionStart", source="startup")
    original = graphtrail_delta._run_graphtrail
    interleaved = False

    def run(binary, db_path, command, *extra, **kwargs):
        nonlocal interleaved
        if command == "affected" and not interleaved:
            interleaved = True
            if interleaving == "read":
                event(target, "PostToolUse", tool_name="Read", tool_input={"file_path": "caller.py"})
            elif interleaving == "write":
                event(target, "PreToolUse", tool_name="Edit", tool_input={"file_path": "caller.py"})
            else:
                event(target, "SessionStart", source="clear")
        return original(binary, db_path, command, *extra, **kwargs)

    monkeypatch.setattr(graphtrail_delta, "_run_graphtrail", run)
    event(target, "PreToolUse", tool_name="Edit", tool_input={"file_path": "api.py"})
    state = runtime.read_session_state(target, "session-one")
    if interleaving == "read":
        assert state["read_files"] == ["caller.py"]
    elif interleaving == "write":
        assert {check["file_path"] for check in state["dependency_checks"]} == {"api.py", "caller.py"}
    else:
        assert state["task_epoch"] == runtime._current_task_epoch("session-one")
        assert not state.get("dependency_checks")


def test_read_completed_during_graph_query_is_excluded_from_record_and_warning(target, monkeypatch):
    graph(target, monkeypatch)
    original = graphtrail_delta._run_graphtrail

    def run(binary, db_path, command, *extra, **kwargs):
        if command == "affected":
            event(target, "PostToolUse", tool_name="Read", tool_input={"file_path": "caller.py"})
        return original(binary, db_path, command, *extra, **kwargs)

    monkeypatch.setattr(graphtrail_delta, "_run_graphtrail", run)
    result = event(target, "PreToolUse", tool_name="Edit", tool_input={"file_path": "api.py"})
    check = runtime.read_session_state(target, "session-one")["dependency_checks"][-1]
    assert check["unseen_dependents"] == ["tests/test_api.py"]
    assert "caller.py" not in result["hookSpecificOutput"]["additionalContext"]
    assert "tests/test_api.py" in result["hookSpecificOutput"]["additionalContext"]
    event(target, "PostToolUse", tool_name="Read", tool_input={"file_path": "tests/test_api.py"})
    assert runtime.read_session_state(target, "session-one")["dependency_checks"][-1] == check


@pytest.mark.parametrize("earlier_write", [False, True])
def test_read_cannot_erase_or_rewind_concurrent_write_evidence(target, monkeypatch, earlier_write):
    event(target, "SessionStart", source="startup")
    if earlier_write:
        monkeypatch.setattr(runtime.localio, "utc_now_iso", lambda: "2025-01-01T00:00:00+00:00")
        event(target, "PostToolUse", tool_name="Edit", tool_input={"file_path": "api.py"})
    original = dependencies.record_read
    written = {}
    fields = ("write_observed", "last_write_at", "last_attributed_write_at", "last_verification_write_at")

    def read_after_write(repo, state, payload):
        monkeypatch.setattr(runtime.localio, "utc_now_iso", lambda: "2025-01-02T00:00:00+00:00")
        (target / "api.py").write_text("def serve(): return 1\n")
        event(target, "PostToolUse", tool_name="Edit", tool_input={"file_path": "api.py"})
        saved = runtime.read_session_state(target, "session-one")
        written.update({key: saved[key] for key in fields})
        return original(repo, state, payload)

    monkeypatch.setattr(dependencies, "record_read", read_after_write)
    event(target, "PostToolUse", tool_name="Read", tool_input={"file_path": "caller.py"})
    state = runtime.read_session_state(target, "session-one")
    assert {key: state.get(key) for key in fields} == written
    assert state["read_files"] == ["caller.py"]
    assert event(target, "Stop")["decision"] == "block"


@pytest.mark.parametrize("observer", ["read", "brief", "write_check"])
@pytest.mark.parametrize("transition", ["start", "finish"])
def test_observer_preserves_pending_bash_lifecycle(target, monkeypatch, observer, transition):
    graph(target, monkeypatch)
    event(target, "SessionStart", source="startup")
    bash = {"tool_name": "Bash", "tool_input": {"command": "custom-generator"}}
    if transition == "finish":
        event(target, "PreToolUse", **bash)
    expected_pending = {}

    def overlap():
        if transition == "start":
            event(target, "PreToolUse", **bash)
        else:
            (target / "api.py").write_text("def serve(): return 1\n")
            event(target, "PostToolUse", **bash)
        current = runtime.read_session_state(target, "session-one")
        expected_pending.update({key: value for key, value in current.items() if key.startswith("pending_bash_")})

    if observer == "read":
        original = dependencies.record_read

        def read(*args):
            overlap()
            return original(*args)

        monkeypatch.setattr(dependencies, "record_read", read)
        event(target, "PostToolUse", tool_name="Read", tool_input={"file_path": "caller.py"})
    elif observer == "brief":

        def brief(*args):
            overlap()
            return "_schema v1: 1 entry points - 1 edges_\n- caller.py\n"

        monkeypatch.setattr(runtime, "_run_brief", brief)
        event(target, "SessionStart", source="compact")
    else:
        original_query = dependencies._dependents

        def dependents(*args):
            overlap()
            return original_query(*args)

        monkeypatch.setattr(dependencies, "_dependents", dependents)
        event(target, "PreToolUse", tool_name="Edit", tool_input={"file_path": "api.py"})
    state = runtime.read_session_state(target, "session-one")
    assert {key: value for key, value in state.items() if key.startswith("pending_bash_")} == expected_pending
    if transition == "start":
        (target / "api.py").write_text("def serve(): return 1\n")
        event(target, "PostToolUse", **bash)
    assert runtime.read_session_state(target, "session-one")["write_observed"] is True
    assert event(target, "Stop")["decision"] == "block"


@pytest.mark.parametrize("key", ["read_files", "brief_files", "dependency_checks"])
@pytest.mark.parametrize("malformed", [None, 17, "not-a-list", [None, 17, {}]])
def test_hook_repairs_malformed_observation_collections(target, key, malformed):
    event(target, "SessionStart", source="startup")
    state = runtime.read_session_state(target, "session-one")
    state[key] = malformed
    runtime.write_session_state(target, "session-one", state)
    event(target, "PostToolUse", tool_name="Read", tool_input={"file_path": "caller.py"})
    state = runtime.read_session_state(target, "session-one")
    assert state["read_files"] == ["caller.py"]
    if key != "read_files":
        assert state.get(key, []) == []


@pytest.mark.parametrize("persisted", [None, 17, "not-a-list", [None, 17, {}], ["caller.py", None, {}], ["caller.py"]])
def test_compaction_restore_validates_and_retains_brief_observations(target, monkeypatch, persisted):
    event(target, "SessionStart", source="startup")
    event(target, "PreCompact", trigger="auto")
    state = runtime.read_session_state(target, "session-one")
    state["brief_files"] = persisted
    runtime.write_session_state(target, "session-one", state)
    monkeypatch.setattr(
        runtime,
        "_run_brief",
        lambda *args: "_schema v1: 1 entry points - 1 edges_\n- tests/test_api.py\n",
    )
    result = event(target, "UserPromptSubmit")
    assert "tests/test_api.py" in result["hookSpecificOutput"]["additionalContext"]
    expected = ["tests/test_api.py"]
    if isinstance(persisted, list) and "caller.py" in persisted:
        expected.insert(0, "caller.py")
    assert runtime.read_session_state(target, "session-one")["brief_files"] == expected


@pytest.mark.parametrize("interleaving", ["none", "reread", "parallel_read", "stale_write"])
def test_seen_file_cap_retains_recent_reads_for_dependency_checks(target, monkeypatch, interleaving):
    monkeypatch.setattr(dependencies, "MAX_SEEN_FILES", 2)
    for name in ("a.py", "m.py", "z.py", "zz.py"):
        (target / name).write_text("def caller(): pass\n")
    graph(target, monkeypatch, impacted_files=[{"file_path": "z.py"}], affected_tests=[])
    event(target, "SessionStart", source="startup")
    for name in ("a.py", "m.py"):
        event(target, "PostToolUse", tool_name="Read", tool_input={"file_path": name})
    if interleaving == "reread":
        event(target, "PostToolUse", tool_name="Read", tool_input={"file_path": "a.py"})
    elif interleaving == "parallel_read":
        original = dependencies.record_read
        interleaved = False

        def read(*args):
            nonlocal interleaved
            if not interleaved:
                interleaved = True
                event(target, "PostToolUse", tool_name="Read", tool_input={"file_path": "zz.py"})
            return original(*args)

        monkeypatch.setattr(dependencies, "record_read", read)
    if interleaving == "stale_write":
        original_write = runtime._write_session_state_preserving_latch
        interleaved = False

        def write(repo, session_id, state, **kwargs):
            nonlocal interleaved
            if state.get("write_observed") and not interleaved:
                interleaved = True
                event(target, "PostToolUse", tool_name="Read", tool_input={"file_path": "z.py"})
            return original_write(repo, session_id, state, **kwargs)

        monkeypatch.setattr(runtime, "_write_session_state_preserving_latch", write)
        event(target, "PostToolUse", tool_name="Edit", tool_input={"file_path": "api.py"})
    else:
        event(target, "PostToolUse", tool_name="Read", tool_input={"file_path": "z.py"})
    expected = {"z.py", {"reread": "a.py", "parallel_read": "zz.py"}.get(interleaving, "m.py")}
    assert set(runtime.read_session_state(target, "session-one")["read_files"]) == expected
    result = event(target, "PreToolUse", tool_name="Edit", tool_input={"file_path": "api.py"})
    assert runtime.read_session_state(target, "session-one")["dependency_checks"][-1]["unseen_dependents"] == []
    assert result is None


def test_fresh_incremental_graph_does_not_require_full_rebuild(target, monkeypatch):
    graph(target, monkeypatch)
    original = graphtrail_delta._run_graphtrail

    def run(binary, db_path, command, *extra, **kwargs):
        if command == "sync" and "--force" in extra:
            return {"returncode": 124, "timed_out": True}
        return original(binary, db_path, command, *extra, **kwargs)

    monkeypatch.setattr(graphtrail_delta, "_run_graphtrail", run)
    event(target, "PreToolUse", tool_name="Edit", tool_input={"file_path": "api.py"})
    assert runtime.read_session_state(target, "session-one")["dependency_checks"][-1]["unseen_dependents"] == [
        "caller.py",
        "tests/test_api.py",
    ]


@pytest.mark.skipif(sys.platform != "linux", reason="XDG cache roots apply on Linux")
def test_task_epochs_do_not_touch_inherited_cache(target, inherited_cache):
    event(target, "SessionStart", source="startup")
    assert not inherited_cache.exists()
