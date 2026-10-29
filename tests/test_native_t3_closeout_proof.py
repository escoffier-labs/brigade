"""Fixture-only proof of reviewed native results in existing work-session storage.

These tests exercise retention and restart readback, not native settlement,
journal acknowledgement, redaction, byte-limit enforcement or memory promotion.
"""

import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from brigade import work_cmd
from brigade.native_t3_observations import NativeTaskReference, classify_native_snapshot

from tests.work_cmd_test_helpers import _init_git_repo


@pytest.fixture
def session(tmp_path, monkeypatch):
    # Do not inherit the operator's harness identity or collect live snapshots.
    for name in ("CODEX_THREAD_ID", "CLAUDE_SESSION_ID", "T3_THREAD_ID", "T3_ENVIRONMENT_ID"):
        monkeypatch.delenv(name, raising=False)
    target = tmp_path / "workspace"
    target.mkdir()
    _init_git_repo(target)
    monkeypatch.setattr(work_cmd.helpers, "_session_snapshot", lambda target: {"git": {}, "dogfood": {}})
    assert work_cmd.start(target=target, title="Synthetic native result") == 0
    root = target / ".brigade" / "work"
    session_dir = root / (root / "current").read_text(encoding="utf-8").strip()
    return target, session_dir


def _fresh_read(session_dir):
    """Only ordinary files cross this process boundary, never parent objects."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json, pathlib, sys\n"
            "root = pathlib.Path(sys.argv[1])\n"
            "print(json.dumps({\n"
            " 'session': json.loads((root / 'session.json').read_text(encoding='utf-8')),\n"
            " 'markdown': (root / 'notes.md').read_text(encoding='utf-8')\n"
            "}))\n",
            str(session_dir),
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return json.loads(result.stdout)


def test_reviewed_native_result_is_readable_in_fresh_process(session):
    target, session_dir = session
    accepted = NativeTaskReference(
        "example-origin", "example-request", "example-task", "example-child", "example-thread"
    )
    supplied = {
        "taskId": "example-task",
        "remoteChild": {"environmentId": "example-child", "threadId": "example-thread", "label": "Example"},
        "status": "completed",
        "workState": "result_available",
        "hasPendingChildRuns": False,
        "hasPendingQuestion": False,
        "pendingApproval": None,
        "waitTimedOut": False,
        "latestTerminalStatus": "completed",
        "latestTerminalRunId": "example-terminal-run",
        "latestTerminalResultContextTransferId": "example-transfer",
        "resultContextTransferId": "example-transfer",
    }
    observed = classify_native_snapshot(
        accepted, supplied, observed_origin_environment_id="example-origin", containment="quiescent"
    )
    assert observed.family == "terminal-candidate"
    report = target / "report.txt"
    report.write_text("Synthetic report: no outstanding defects.\n", encoding="utf-8")
    report_digest = hashlib.sha256(report.read_bytes()).hexdigest()
    reviewed = "Reviewed summary: synthetic report retained, no outstanding defects."
    record = "\n".join(
        [
            f"Origin environment: {accepted.origin_environment_id}",
            f"Request key: {accepted.native_client_request_key}",
            f"Native task: {accepted.task_id}",
            f"Child environment: {accepted.remote_environment_id}",
            f"Child thread: {accepted.remote_thread_id}",
            f"Terminal run: {observed.terminal_run_id}",
            f"Result transfer: {observed.result_transfer_id}",
            f"Native status: {observed.status}",
            reviewed,
            f"Report: {report}",
            f"Report SHA-256: {report_digest}",
            "Consumer: example-parent",
            "Consumption: operator consumption only, no journal acknowledgement",
            f"Retained record: {session_dir / 'session.json'} notes[0]",
            "Obligations: none held by child",
            "Settlement: unresolved, no native operation performed",
        ]
    )
    assert work_cmd.note(target=target, text=record) == 0

    saved = _fresh_read(session_dir)
    assert saved["session"]["notes"][0]["text"] == record
    for fact in (
        "Origin environment: example-origin",
        "Request key: example-request",
        "Native task: example-task",
        "Child environment: example-child",
        "Child thread: example-thread",
        "Terminal run: example-terminal-run",
        "Result transfer: example-transfer",
        reviewed,
        f"Report SHA-256: {hashlib.sha256(report.read_bytes()).hexdigest()}",
        "Consumption: operator consumption only, no journal acknowledgement",
    ):
        assert fact in saved["session"]["notes"][0]["text"]
        assert fact in saved["markdown"]
    assert not (target / ".brigade" / "runs").exists()


def test_source_and_retained_summary_have_distinct_digests(session):
    target, session_dir = session
    source = "Synthetic source: café result complete. Extra unreviewed discussion omitted.\n"
    summary_input = "  \nReviewed summary: café result complete.\n  "
    normalized = summary_input.strip()
    source_digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    summary_digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    assert source_digest != summary_digest
    assert (
        work_cmd.note(
            target=target,
            text=f"Source representation SHA-256: {source_digest}\nReviewed summary SHA-256: {summary_digest}",
        )
        == 0
    )
    assert work_cmd.note(target=target, text=summary_input) == 0

    saved = _fresh_read(session_dir)
    retained = saved["session"]["notes"][1]["text"]
    assert retained == "Reviewed summary: café result complete."
    labels = saved["session"]["notes"][0]["text"]
    assert f"Source representation SHA-256: {hashlib.sha256(source.encode('utf-8')).hexdigest()}" in labels
    assert f"Reviewed summary SHA-256: {hashlib.sha256(retained.encode('utf-8')).hexdigest()}" in labels
    assert normalized in saved["markdown"]
    assert source not in saved["markdown"]


def test_ended_session_handoff_leads_to_retained_native_result(session, tmp_path):
    target, session_dir = session
    record = (
        "Result reference: example-task/example-terminal-run/example-transfer\n"
        "Reviewed summary: synthetic failure retained for recovery.\n"
        "Consumption: operator consumption only, no journal acknowledgement\n"
        "Obligations: recovery unresolved, no recipient acceptance recorded\n"
        "Settlement: unresolved, no native operation performed"
    )
    assert work_cmd.note(target=target, text=record) == 0
    reference = f"Retained native result: {session_dir / 'session.json'} notes[0], also {session_dir / 'notes.md'}"
    inbox = tmp_path / "handoffs"
    assert work_cmd.end(target=target, note=reference, handoff=True, handoff_inbox=inbox) == 0
    handoffs = list(inbox.glob("*.md"))
    assert len(handoffs) == 1

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json, pathlib, sys\n"
            "handoff = pathlib.Path(sys.argv[1]).read_text(encoding='utf-8')\n"
            "line = next(line for line in handoff.splitlines() if line.startswith('- session.json: `'))\n"
            "path = pathlib.Path(line.split('`')[1])\n"
            "print(json.dumps({'handoff': handoff,\n"
            " 'session': json.loads(path.read_text(encoding='utf-8')),\n"
            " 'markdown': (path.parent / 'notes.md').read_text(encoding='utf-8')}))\n",
            str(handoffs[0]),
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    saved = json.loads(result.stdout)
    assert reference in saved["handoff"]
    assert saved["session"]["status"] == "ended"
    assert saved["session"]["notes"][0]["text"] == record
    assert record in saved["markdown"]
    assert Path(saved["session"]["handoff"]) == handoffs[0]
    assert not (target / ".brigade" / "work" / "current").exists()
    assert not (target / ".brigade" / "runs").exists()
    assert not (target / "memory").exists()
