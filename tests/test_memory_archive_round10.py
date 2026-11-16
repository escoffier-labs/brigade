"""Scoped references and recoverable lifecycle failures."""

from datetime import date
import json
import hashlib
from pathlib import Path
import subprocess
import sys

import pytest

from brigade import memory_care_hub as hub, memory_care_journal as journal
from brigade import memory_care_tend as tend, memory_lifecycle as life
from tests.memory_read_fixtures import automatic_archive_test_mode as automatic_archive_test_mode
from tests.memory_read_fixtures import signed_memory_authority as signed_memory_authority
from tests.memory_read_fixtures import windows_scheduler as windows_scheduler
from tests.memory_read_fixtures import coverage
from tests.test_memory_lifecycle import TODAY, card


def covered_card(root):
    path = card(root)
    coverage(root, date(2026, 1, 1), TODAY)
    return path


@pytest.mark.parametrize("name", ["AGENTS.md", "CLAUDE.md", "AGENTS.override.md"])
@pytest.mark.parametrize("directory", ["memory", "memory/cards/scoped"])
def test_scoped_memory_instructions_veto_archive(tmp_path, name, directory):
    path = covered_card(tmp_path)
    ref = tmp_path / directory / name
    ref.parent.mkdir(parents=True, exist_ok=True)
    ref.write_text("[old](/memory/cards/old.md)\n")
    result = life.run(tmp_path, today=TODAY)
    assert path.exists(), result
    assert {"path": "memory/cards/old.md", "reason": "referenced"} in result["skipped"]


@pytest.mark.parametrize("operation", ["retire", "annotation"])
@pytest.mark.parametrize("error", [OSError, TimeoutError, journal.JournalConflict])
def test_post_commit_failure_returns_transition_and_pending_decision(tmp_path, monkeypatch, operation, error):
    path = covered_card(tmp_path)
    update = journal.update_hub

    def failed(*args, **kwargs):
        raise error("annotation unavailable")

    if operation == "retire":
        monkeypatch.setattr(hub, "retire", failed)
    else:
        monkeypatch.setattr(journal, "update_hub", failed)
    result = life.run(tmp_path, today=TODAY)
    assert not path.exists()
    transition = result["transitions"][-1]
    assert transition["to_state"] == "archived", result
    decision = journal.load(tmp_path, transition["decision_id"])
    assert decision.hub["status"] == "pending"
    monkeypatch.setattr(journal, "update_hub", update)
    assert hub.retry_pending(tmp_path)["pending"] == 0


@pytest.mark.parametrize("source", ["policy", "outcome", "evidence", "state", "missing-state-key", "subprocess"])
def test_failed_lifecycle_keeps_later_stages_and_saves_run(tmp_path, monkeypatch, source):
    covered_card(tmp_path)
    if source == "policy":
        (tmp_path / ".brigade/memory-care.toml").write_text("lifecycle = 1\n")
    elif source in {"outcome", "evidence"}:
        from brigade import memory_evidence_log

        rel = "memory/outcome/records.jsonl" if source == "outcome" else memory_evidence_log.LOG_PATH
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{broken\n")
    elif source in {"state", "missing-state-key"}:
        digest = hashlib.sha256(b"memory/cards/old.md").hexdigest()
        path = tmp_path / f".brigade/memory/lifecycle/{digest}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{broken" if source == "state" else "{}")
    else:

        def failed(*args, **kwargs):
            raise subprocess.TimeoutExpired("schtasks", 15)

        monkeypatch.setattr(life, "_references", failed)
    result = tend.run(tmp_path, today=TODAY, stages=["lifecycle", "inbox", "instructions"])
    assert result["failed_stages"] == ["lifecycle"]
    assert result["stages"]["lifecycle"]["error"]
    assert {"inbox", "instructions"} <= result["stages"].keys()
    assert json.loads((tmp_path / tend.STATE_REL).read_text()) == result


@pytest.mark.parametrize(
    "error",
    [subprocess.TimeoutExpired("schtasks", 15), subprocess.CalledProcessError(1, "schtasks"), OSError("unreadable")],
)
def test_windows_schedule_failure_is_fail_closed(tmp_path, monkeypatch, error):
    run = subprocess.run

    def failed(args, *a, **kw):
        if args[0] == "schtasks":
            raise error
        return run(args, *a, **kw)

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "run", failed)
    with pytest.raises(ValueError, match="installed care schedules are unreadable"):
        life._references(tmp_path, {})


def test_crlf_reference_fixture_survives_windows_translation(tmp_path, monkeypatch):
    from tests.test_memory_archive_round6 import test_multiline_and_workspace_absolute_reference_destinations

    write = Path.write_text

    def windows_write(path, data, *args, **kwargs):
        kwargs.setdefault("newline", "\r\n")
        return write(path, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", windows_write)
    test_multiline_and_workspace_absolute_reference_destinations(tmp_path, "[dep]:\r\n  notes.md")


def test_default_tend_records_shared_lifecycle_signal_failures(tmp_path):
    covered_card(tmp_path)
    (tmp_path / ".brigade/memory-care.toml").write_text("lifecycle = 1\n")
    result = tend.run(tmp_path, today=TODAY)
    assert result["failed_stages"] == ["lifecycle", "retire"]
    assert {"inbox", "instructions"} <= result["stages"].keys()
    assert json.loads((tmp_path / tend.STATE_REL).read_text()) == result


def test_gitignored_memory_instructions_still_veto_archive(tmp_path):
    path = covered_card(tmp_path)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("memory/\n")
    (tmp_path / "memory/AGENTS.md").write_text("[old](cards/old.md)\n")
    result = life.run(tmp_path, today=TODAY)
    assert path.exists(), result
