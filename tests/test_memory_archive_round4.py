"""Offline coverage, fail-open reads, intent recovery and pruned scans."""

from tests.memory_read_fixtures import windows_scheduler as windows_scheduler

from datetime import date, timedelta
import json
import os

import pytest

from brigade import memory_card_reads as reads, memory_lifecycle as life
from tests.memory_read_fixtures import signing_key
from tests.memory_read_fixtures import signed_memory_authority as signed_memory_authority

START = date(2026, 1, 1)


def test_weekend_gap_preserves_observation_without_inventing_use(tmp_path):
    signing_key(tmp_path)
    assert reads.record(tmp_path, ["old"], today=START)
    assert reads.record(tmp_path, [], today=START + timedelta(days=3))
    ledger = reads.load(tmp_path)
    assert ledger.healthy_since == START
    assert ledger.last_read == {"old": START}
    payload = json.loads((tmp_path / reads.READS_REL).read_text())
    assert payload["coverage_days"] == [START.isoformat(), (START + timedelta(days=3)).isoformat()]


def test_forged_gap_fails_authentication(tmp_path):
    signing_key(tmp_path)
    reads.record(tmp_path, [], today=START)
    path = tmp_path / reads.READS_REL
    value = json.loads(path.read_text())
    value["heartbeat"] = (START + timedelta(days=3)).isoformat()
    value["coverage_days"].append(value["heartbeat"])
    path.write_text(json.dumps(value))
    assert reads.load(tmp_path).recorder_error
    reads.record(tmp_path, [], today=START + timedelta(days=3))
    assert reads.load(tmp_path).healthy_since == START + timedelta(days=3)


@pytest.mark.parametrize("gap", [5, 6])
def test_gap_allowance_boundary(tmp_path, gap):
    signing_key(tmp_path)
    reads.record(tmp_path, [], today=START)
    end = START + timedelta(days=gap)
    reads.record(tmp_path, [], today=end)
    assert reads.load(tmp_path).healthy_since == (START if gap == 5 else end)


def test_expired_intent_recovers_with_new_window(tmp_path):
    signing_key(tmp_path)
    reads.record(tmp_path, [], today=START)
    intent = f"{reads.COVERAGE_REL}/interrupted.json"
    reads._write_json(tmp_path, intent, {"state": "pending", "day": START.isoformat()})
    assert reads.load(tmp_path).recorder_error
    reads.record(tmp_path, [], today=START)
    assert reads.load(tmp_path).recorder_error
    end = START + timedelta(days=1)
    reads.record(tmp_path, [], today=end)
    assert not reads.load(tmp_path).recorder_error
    assert reads.load(tmp_path).healthy_since == end
    assert not (tmp_path / intent).exists()


def test_unwritable_intent_fails_open_and_resets_on_recovery(tmp_path, monkeypatch):
    signing_key(tmp_path)
    reads.record(tmp_path, [], today=START)
    with monkeypatch.context() as patch:

        def denied(*args, **kwargs):
            raise PermissionError("read-only state directory")

        patch.setattr(reads, "_write_json", denied)
        reads.record_content(tmp_path, "content", "memory/cards/old.md")
        assert reads.load(tmp_path).recorder_error
    end = START + timedelta(days=1)
    reads.record(tmp_path, [], today=end)
    assert reads.load(tmp_path).healthy_since == end


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory permissions")
def test_read_only_state_directory(tmp_path):
    signing_key(tmp_path)
    state = tmp_path / reads.COVERAGE_REL
    state.mkdir(parents=True)
    state.chmod(0o500)
    try:
        assert reads.record(tmp_path, ["old"]) is False
        reads.record_content(tmp_path, "content", "memory/cards/old.md")
        assert reads.load(tmp_path).recorder_error
    finally:
        state.chmod(0o700)


@pytest.mark.parametrize("directory", ["node_modules", ".venv", "build", "dist", "vendor"])
def test_reference_scan_prunes_before_descent(tmp_path, monkeypatch, directory):
    hidden = tmp_path / directory / "nested"
    hidden.mkdir(parents=True)
    (hidden / "AGENTS.md").write_text("memory/cards/old.md")
    (tmp_path / "AGENTS.md").write_text("instructions")
    walk = os.walk
    visited = []

    def observed(root, **kwargs):
        for entry in walk(root, **kwargs):
            visited.append(entry[0])
            yield entry

    monkeypatch.setattr(life.os, "walk", observed)
    refs = life._references(tmp_path, {})
    assert "AGENTS.md" in refs
    assert not any(directory in str(path) for path in visited)
    assert not any(directory in path for path in refs)


def test_reference_scan_prunes_gitignored_directory(tmp_path):
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("ignored-instructions/\n")
    hidden = tmp_path / "ignored-instructions"
    hidden.mkdir()
    (hidden / "AGENTS.md").write_text("memory/cards/old.md")
    assert "ignored-instructions/AGENTS.md" not in life._references(tmp_path, {})


@pytest.mark.parametrize("definition", ["[dependency]: old.md", '[dependency]: <old.md#details> "Title"'])
def test_reference_style_link_protects_old_card(tmp_path, definition):
    from tests.test_memory_lifecycle import card, coverage, TODAY

    old = card(tmp_path)
    active = card(tmp_path, name="active", body=f"[details][dependency]\n\n{definition}")
    reads.record_content(tmp_path, active.read_text(), "memory/cards/active.md")
    coverage(tmp_path, START, TODAY)
    result = life.run(tmp_path, today=TODAY)
    assert old.exists(), result
    assert {row["reason"] for row in result["skipped"] if row["path"] == "memory/cards/old.md"} == {"referenced"}
