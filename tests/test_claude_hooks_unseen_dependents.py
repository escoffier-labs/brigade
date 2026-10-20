"""Non-blocking dependency observations at the real Claude hook boundary."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brigade import graphtrail_delta
from brigade.claude_hooks import package, runtime
from brigade.install import install_selection
from brigade.selection import Selection


@pytest.fixture
def target(tmp_path: Path, monkeypatch) -> Path:
    target = tmp_path / "repo"
    assert install_selection(target, Selection(depth="repo", harnesses=["claude"], owner="claude", includes=[])) == 0
    (target / "api.py").write_text("def serve(): pass\n")
    (target / "caller.py").write_text("from api import serve\nserve()\n")
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
    db.touch()
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
