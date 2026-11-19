"""Fail closed on missing use signals while permitting index-only archival."""

from datetime import date
import os
from pathlib import Path
import subprocess
import sys

import pytest

from brigade import memory_card_reads as reads, memory_cmd, memory_lifecycle as life
from brigade import memory_care_tend as tend
from tests.test_memory_lifecycle import card, observed_run, TODAY
from tests.memory_read_fixtures import coverage
from tests.memory_read_fixtures import signed_memory_authority as signed_memory_authority
from tests.memory_read_fixtures import automatic_archive_test_mode as automatic_archive_test_mode
from tests.memory_read_fixtures import windows_scheduler as windows_scheduler


@pytest.mark.skipif(os.name == "nt", reason="POSIX key permissions")
@pytest.mark.parametrize("mode", [0, 0o644])
def test_unusable_key_failure_survives_process(tmp_path, mode):
    path = card(tmp_path)
    coverage(tmp_path, date(2026, 1, 1), TODAY)
    key = tmp_path / ".brigade/receipt-signing-key"
    key.chmod(mode)
    code = """
import sys
from pathlib import Path
from brigade import memory_card_reads as reads
assert not reads.record(Path(sys.argv[1]), ['old'])
"""
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    try:
        subprocess.run([sys.executable, "-c", code, str(tmp_path)], env=env, check=True)
    finally:
        key.chmod(0o600)
    assert reads.load(tmp_path).recorder_error
    code = """
import sys
from datetime import date
from pathlib import Path
from brigade import memory_lifecycle as life
assert not life.run(Path(sys.argv[1]), today=date(2026, 11, 15))['transitions']
"""
    subprocess.run([sys.executable, "-c", code, str(tmp_path)], env=env, check=True)
    assert path.exists()


@pytest.mark.parametrize("name,destination", [("notes(v1)", r"notes\(v1\).md"), ("notes&more", "notes&amp;more.md")])
@pytest.mark.parametrize("inline", [False, True])
def test_commonmark_destination_protects_card(tmp_path, name, destination, inline):
    path = card(tmp_path, name=name)
    ref = tmp_path / "runbooks/guide.md"
    ref.parent.mkdir()
    destination = "../memory/cards/" + destination
    ref.write_text(f"[notes]({destination})\n" if inline else f"[notes][dep]\n\n[dep]:\n  {destination}\n")
    assert not observed_run(tmp_path, today=TODAY)["transitions"]
    assert path.exists()


@pytest.mark.parametrize("index", ["MEMORY.md", "runbooks/index.md"])
@pytest.mark.parametrize("protected", [False, True])
def test_index_only_archive_and_restore(tmp_path, index, protected):
    path = card(tmp_path)
    cfg = tmp_path / ".brigade/memory-care.toml"
    cfg.write_text(f'index_paths = ["{index}"]\n')
    idx = tmp_path / index
    idx.parent.mkdir(parents=True, exist_ok=True)
    original = "- [Old](memory/cards/old.md)\n"
    idx.write_text(original)
    if protected:
        (tmp_path / "TOOLS.md").write_text(original)
    result = observed_run(tmp_path, today=TODAY)
    if protected:
        assert not result["transitions"]
        assert path.exists()
    else:
        assert result["transitions"][-1]["to_state"] == "archived"
        assert not path.exists()
        assert idx.read_text() == ""
        tend.undo(tmp_path, result["transitions"][-1]["decision_id"])
        assert idx.read_text() == original
        assert path.exists()


@pytest.mark.parametrize("failure", [False, True])
def test_read_during_reference_scan_blocks_archive(tmp_path, monkeypatch, failure):
    path = card(tmp_path)
    real = life._references

    def references(*args):
        result = real(*args)
        if failure:
            reads._record_failure(tmp_path)
        else:
            assert reads.record(tmp_path, ["old"], today=TODAY)
        return result

    monkeypatch.setattr(life, "_references", references)
    assert not observed_run(tmp_path, today=TODAY)["transitions"]
    assert path.exists()


@pytest.mark.parametrize("excluded", [False, True])
def test_custom_root_archive_search_filters(tmp_path, excluded):
    from brigade import memory_archive

    path = card(tmp_path)
    dest = tmp_path / "notes/cards/old.md"
    dest.parent.mkdir(parents=True)
    path.rename(dest)
    config = memory_cmd.MemoryCareConfig(card_roots=("notes/cards",), include_paths=("notes/cards",))
    memory_archive.archive_card(tmp_path, "notes/cards/old.md", reason="test", config=config)
    if excluded:
        from dataclasses import replace

        config = replace(config, exclude_paths=("notes/cards",))
    matches = memory_cmd.search_cards_payload(tmp_path, "retrievalwidget", config=config, include_archived=True)[
        "matches"
    ]
    assert bool(matches) is not excluded


@pytest.mark.parametrize(
    "original_rel,expected",
    [("memory/cards/old.md", False), ("notes/cards/old.md", True)],
)
def test_archived_search_requires_card_root_with_empty_filters(tmp_path, original_rel, expected):
    from brigade import memory_archive

    path = card(tmp_path)
    if original_rel != "memory/cards/old.md":
        dest = tmp_path / original_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        path.rename(dest)
    archive_config = memory_cmd.MemoryCareConfig(card_roots=("memory/cards", "notes/cards"))
    memory_archive.archive_card(tmp_path, original_rel, reason="test", config=archive_config)
    scoped = memory_cmd.MemoryCareConfig(card_roots=("notes/cards",), include_paths=(), exclude_paths=())
    matches = memory_cmd.search_cards_payload(tmp_path, "retrievalwidget", config=scoped, include_archived=True)[
        "matches"
    ]
    assert bool(matches) is expected


def test_scan_failure_keeps_later_tend_stages_running(tmp_path, monkeypatch):
    from brigade import memory_care_retire, memory_inbox_drain

    card(tmp_path)

    def inaccessible(*args, **kwargs):
        raise PermissionError("reference unavailable")

    monkeypatch.setattr(life, "run", inaccessible)
    monkeypatch.setattr(memory_care_retire, "apply", inaccessible)
    monkeypatch.setattr(memory_inbox_drain, "drain", lambda *a, **kw: {"ran": True})
    result = tend.run(tmp_path, stages=["lifecycle", "retire", "inbox"], today=TODAY)
    assert result["failed_stages"] == ["lifecycle", "retire"]
    assert result["stages"]["inbox"]["ran"]
    assert (tmp_path / tend.STATE_REL).exists()


def test_index_only_expiry_candidate(tmp_path):
    from brigade import memory_care_retire

    card(tmp_path, meta="trust: agent-unreviewed\nexpires: 2026-01-01\n")
    coverage(tmp_path, date(2026, 1, 1), TODAY)
    (tmp_path / "MEMORY.md").write_text("- [Old](memory/cards/old.md)\n")
    result = memory_care_retire.plan(tmp_path, memory_cmd.MemoryCareConfig(), tend.TendConfig(), today=TODAY)
    assert result["candidates"][0]["file"] == "memory/cards/old.md"
    (tmp_path / "TOOLS.md").write_text("[Old](memory/cards/old.md)\n")
    assert not memory_care_retire.plan(tmp_path, memory_cmd.MemoryCareConfig(), tend.TendConfig(), today=TODAY)[
        "candidates"
    ]
