import json

import pytest

from brigade import memory_archive as archive, memory_care_journal as journal, memory_care_tend as tend, memory_cmd


def card(target):
    path = target / "cards.md"
    path.write_bytes(b"---\ncard_id: old\n---\nOriginal\r\n")
    (target / "MEMORY.md").write_text("- [Old](cards.md)\n")
    return path


def interrupt_second_plan(monkeypatch):
    execute = journal.kernel.execute
    calls = []

    def interrupted(plan, **kwargs):
        calls.append(plan)
        if len(calls) == 2:
            raise RuntimeError("between phases")
        return execute(plan, **kwargs)

    monkeypatch.setattr(journal.kernel, "execute", interrupted)
    return execute


def prepared(target):
    paths = list((target / journal.JOURNAL_REL).glob("*/decision.json"))
    assert len(paths) == 1
    payload = json.loads(paths[0].read_text())
    assert payload["phase"] == "prepared"
    return payload


@pytest.mark.parametrize("entry", ["tend", "manifest", "undo"])
@pytest.mark.parametrize("changed", [False, True])
def test_archive_crash_resolves_before_next_command(tmp_path, monkeypatch, entry, changed):
    path = card(tmp_path)
    original = path.read_bytes()
    execute = interrupt_second_plan(monkeypatch)
    with pytest.raises(RuntimeError, match="between phases"):
        archive.archive_card(tmp_path, "cards.md", reason="ended", config=memory_cmd.MemoryCareConfig())
    p = prepared(tmp_path)
    assert path.read_bytes() == original
    assert (tmp_path / "memory/archive/cards.md").read_bytes() == original
    assert journal.before_bytes(tmp_path, journal.load(tmp_path, p["decision_id"]), "cards.md") == original
    cards = memory_cmd._iter_cards(tmp_path, memory_cmd.MemoryCareConfig(card_roots=(".", "memory")))
    assert cards.count(path) == 1
    assert tmp_path / "memory/archive/cards.md" not in cards
    assert "- [Old]" in (tmp_path / "MEMORY.md").read_text()
    monkeypatch.setattr(journal.kernel, "execute", execute)
    if changed:
        path.write_bytes(b"Changed")
    manifest = tmp_path / "empty.jsonl"
    manifest.write_text("")
    if entry == "tend":
        result = tend.run(tmp_path, stages=[])
    elif entry == "manifest":
        result = tend.archive_manifest(tmp_path, manifest)
    else:
        if changed:
            with pytest.raises(archive.ArchiveRefused, match="abandoned"):
                tend.undo(tmp_path, p["decision_id"])
            result = None
        else:
            tend.undo(tmp_path, p["decision_id"])
            result = None
    state = journal._payload(tmp_path, p["decision_id"])
    if changed:
        assert path.read_bytes() == b"Changed"
        assert not (tmp_path / "memory/archive/cards.md").exists()
        assert state["phase"] == "abandoned"
        if result is not None:
            assert result["recovery"][0]["phase"] == "abandoned"
    elif entry == "undo":
        assert path.read_bytes() == original
        assert state["phase"] == "undone"
    else:
        assert not path.exists()
        assert state["phase"] == "committed"


def test_undo_crash_keeps_both_copies_and_finishes(tmp_path, monkeypatch):
    path = card(tmp_path)
    original = path.read_bytes()
    d = archive.archive_card(tmp_path, "cards.md", reason="ended", config=memory_cmd.MemoryCareConfig())
    execute = interrupt_second_plan(monkeypatch)
    with pytest.raises(RuntimeError, match="between phases"):
        tend.undo(tmp_path, d.decision_id)
    assert path.read_bytes() == original
    assert (tmp_path / "memory/archive/cards.md").read_bytes() == original
    assert "- [Old]" in (tmp_path / "MEMORY.md").read_text()
    monkeypatch.setattr(journal.kernel, "execute", execute)
    tend.run(tmp_path, stages=[])
    assert path.read_bytes() == original
    assert not (tmp_path / "memory/archive/cards.md").exists()
    assert journal._payload(tmp_path, d.decision_id)["phase"] == "undone"


@pytest.mark.parametrize("phase", [1, 2])
def test_kernel_crash_reproduction_preserves_original_until_prepared(tmp_path, monkeypatch, phase):
    path = card(tmp_path)
    original = path.read_bytes()
    execute = journal.kernel.execute
    calls = 0

    def interrupted(plan, **kwargs):
        nonlocal calls
        calls += 1
        if calls == phase:
            return execute(plan, **kwargs, inject=journal.kernel.FailureInjector(crash_after=1))
        return execute(plan, **kwargs)

    monkeypatch.setattr(journal.kernel, "execute", interrupted)
    with pytest.raises(archive.ArchiveRefused, match="recovery-required"):
        archive.archive_card(tmp_path, "cards.md", reason="ended", config=memory_cmd.MemoryCareConfig())
    if phase == 1:
        assert path.read_bytes() == original
    else:
        assert (tmp_path / "memory/archive/cards.md").read_bytes() == original
    monkeypatch.setattr(journal.kernel, "execute", execute)
    tend.run(tmp_path, stages=[])
    if phase == 1:
        assert path.read_bytes() == original
    else:
        assert not path.exists()
        assert (tmp_path / "memory/archive/cards.md").read_bytes() == original


def test_hub_waits_for_local_phases_and_offline_recovery_stays_pending(tmp_path, monkeypatch):
    path = card(tmp_path)
    calls = []
    offline = True

    def send(target, *, action, **kwargs):
        if action == "retire":
            assert not path.exists()
            assert journal._payload(target, journal.entries(target)[0].decision_id)["phase"] == "committed"
        else:
            assert path.read_bytes().endswith(b"Original\r\n")
        calls.append(action)
        return {"status": "pending" if offline else "done", "action": action, "path": "cards.md"}

    monkeypatch.setattr(archive.hub, "_send", send)
    execute = interrupt_second_plan(monkeypatch)
    with pytest.raises(RuntimeError):
        archive.archive_card(tmp_path, "cards.md", reason="ended", config=memory_cmd.MemoryCareConfig())
    assert calls == []
    monkeypatch.setattr(journal.kernel, "execute", execute)
    tend.run(tmp_path, stages=["hub"])
    d = journal.entries(tmp_path)[0]
    assert d.hub["status"] == "pending"
    offline = False
    tend.run(tmp_path, stages=["hub"])
    assert journal.load(tmp_path, d.decision_id).hub["status"] == "done"
    calls.clear()
    execute = interrupt_second_plan(monkeypatch)
    with pytest.raises(RuntimeError):
        tend.undo(tmp_path, d.decision_id)
    assert calls == []
    assert path.exists()
    monkeypatch.setattr(journal.kernel, "execute", execute)
    offline = True
    tend.run(tmp_path, stages=["hub"])
    undo = journal.load(tmp_path, journal.load(tmp_path, d.decision_id).undone_by)
    assert undo.hub["status"] == "pending"
    assert calls == ["put"]
    offline = False
    tend.run(tmp_path, stages=["hub"])
    assert journal.load(tmp_path, undo.decision_id).hub["status"] == "done"


def test_prepared_archive_preserves_intervening_index_edits(tmp_path, monkeypatch):
    path = card(tmp_path)
    execute = interrupt_second_plan(monkeypatch)
    with pytest.raises(RuntimeError):
        archive.archive_card(tmp_path, "cards.md", reason="ended", config=memory_cmd.MemoryCareConfig())
    p = prepared(tmp_path)
    (tmp_path / "MEMORY.md").write_text("New header\n- [Old](cards.md)\nNew footer\n")
    monkeypatch.setattr(journal.kernel, "execute", execute)
    tend.run(tmp_path, stages=[])
    assert not path.exists()
    assert (tmp_path / "MEMORY.md").read_text() == "New header\nNew footer\n"
    tend.undo(tmp_path, p["decision_id"])
    assert (tmp_path / "MEMORY.md").read_text() == "New header\n- [Old](cards.md)\nNew footer\n"


def test_edit_between_phases_abandons_without_retiring(tmp_path, monkeypatch):
    path = card(tmp_path)
    execute = journal.kernel.execute
    calls = []

    def edit_after_prepare(plan, **kwargs):
        result = execute(plan, **kwargs)
        if plan.operation_id.endswith("-prepare"):
            path.write_bytes(b"Changed concurrently")
        return result

    monkeypatch.setattr(journal.kernel, "execute", edit_after_prepare)
    monkeypatch.setattr(archive.hub, "retire", lambda *a, **k: calls.append("retire"))
    with pytest.raises(archive.ArchiveRefused, match="abandoned"):
        archive.archive_card(tmp_path, "cards.md", reason="ended", config=memory_cmd.MemoryCareConfig())
    assert calls == []
    assert path.read_bytes() == b"Changed concurrently"
    assert not (tmp_path / "memory/archive/cards.md").exists()
