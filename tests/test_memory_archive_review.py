"""Review regressions for archive safety and publication."""

import json
from datetime import date

import pytest

from brigade import memory_archive as archive, memory_care_journal as journal, memory_care_tend as tend, memory_cmd
from brigade import (
    memory_care_hub as hub,
    memory_hub_client as hubc,
    memory_hub_store as store,
    memory_hub_sync as sync,
)
from tests.test_memory_archive import setup_card
from tests.test_memory_archive_phases import interrupt_second_plan
from tests.test_memory_care_hub import FakeHub


def move(target, rel="memory/cards/old.md", config=None):
    return archive.archive_card(target, rel, reason="ended", config=config or memory_cmd.MemoryCareConfig())


@pytest.mark.parametrize(
    "rel",
    [
        "memory/./cards/old.md",
        "memory//cards/old.md",
        "AGENTS.md",
        "memory/archive/a.md",
        ".brigade/memory-care/journal/example/decision.json",
    ],
)
def test_only_canonical_enumerated_cards_can_move(tmp_path, rel):
    setup_card(tmp_path)
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"protected")
    with pytest.raises(archive.ArchiveRefused):
        move(tmp_path, rel)
    assert path.read_bytes() == b"protected"


@pytest.mark.parametrize(
    "prefix,value,end",
    [
        (b"---\r\n", b"never", b"\r\n---\r\n"),
        (b"\xef\xbb\xbf---\n", b"never", b"\n---\n"),
        (b"---\n", b"never # pinned", b"\n---\n"),
        (b"---\n", b"never", b"\n"),
        (b"---\n", b"[never", b"\n---\n"),
    ],
)
def test_pinned_or_malformed_frontmatter_never_moves(tmp_path, prefix, value, end):
    path = setup_card(tmp_path, prefix + b"archive: " + value + end)
    before = path.read_bytes()
    with pytest.raises(archive.ArchiveRefused):
        move(tmp_path)
    assert path.read_bytes() == before


@pytest.mark.parametrize("rel", ["memory/cards/old.md", "memory/./cards/old.md", "memory//cards/old.md"])
def test_read_only_spelling_never_moves(tmp_path, rel):
    path = setup_card(tmp_path)
    before = path.read_bytes()
    with pytest.raises(archive.ArchiveRefused):
        move(tmp_path, rel, memory_cmd.MemoryCareConfig(read_only_paths=("memory/cards/**",)))
    assert path.read_bytes() == before


def test_modes_and_exact_index_round_trip(tmp_path):
    path = setup_card(tmp_path)
    index = tmp_path / "MEMORY.md"
    before = b"# Index\n\n- [Old](memory/cards/old.md)\n\n## Other\n"
    index.write_bytes(before)
    path.chmod(0o640)
    index.chmod(0o644)
    card_mode = path.stat().st_mode & 0o777
    index_mode = index.stat().st_mode & 0o777
    d = move(tmp_path)
    assert index.stat().st_mode & 0o777 == index_mode
    archive.restore(tmp_path, d)
    assert path.stat().st_mode & 0o777 == card_mode
    assert index.read_bytes() == before


def test_mixed_link_line_is_preserved_and_reported(tmp_path):
    setup_card(tmp_path)
    index = tmp_path / "MEMORY.md"
    before = b"See [Old](memory/cards/old.md) and [Other](memory/cards/other.md) for setup\n"
    index.write_bytes(before)
    d = move(tmp_path)
    assert index.read_bytes() == before
    assert journal._payload(tmp_path, d.decision_id)["index_warnings"]


def test_undo_drift_abandons_and_care_continues(tmp_path, monkeypatch):
    path = setup_card(tmp_path)
    d = move(tmp_path)
    execute = interrupt_second_plan(monkeypatch)
    with pytest.raises(RuntimeError):
        archive.restore(tmp_path, d)
    path.write_bytes(b"edited")
    monkeypatch.setattr(journal.kernel, "execute", execute)
    result = tend.run(tmp_path, stages=[])
    assert result["recovery"][0]["phase"] == "abandoned"
    assert path.read_bytes() == b"edited"
    assert (tmp_path / "memory/archive/cards/old.md").exists()
    assert tend.run(tmp_path, stages=[])["recovery"] == []


def test_missing_original_abandonment_remains_restorable(tmp_path, monkeypatch):
    path = setup_card(tmp_path)
    before = path.read_bytes()
    execute = interrupt_second_plan(monkeypatch)
    with pytest.raises(RuntimeError):
        move(tmp_path)
    decision_id = next((tmp_path / journal.JOURNAL_REL).iterdir()).name
    path.unlink()
    monkeypatch.setattr(journal.kernel, "execute", execute)
    tend.run(tmp_path, stages=[])
    assert (tmp_path / "memory/archive/cards/old.md").read_bytes() == before
    tend.undo(tmp_path, decision_id)
    assert path.read_bytes() == before


def test_projection_error_is_per_row_and_ids_printed(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace

    setup_card(tmp_path)
    for name in ("bad", "later"):
        (tmp_path / f"memory/cards/{name}.md").write_bytes(b"card")
    real = archive.archive_card

    def fail(target, rel, **kwargs):
        if "bad" in rel:
            raise journal.kernel.ProjectionError("injected kernel error")
        return real(target, rel, **kwargs)

    monkeypatch.setattr(archive, "archive_card", fail)
    manifest = tmp_path / "rows.jsonl"
    manifest.write_text(
        "\n".join(json.dumps({"card": f"memory/cards/{n}.md", "reason": "ended"}) for n in ("old", "bad", "later"))
    )
    code = tend.dispatch(
        SimpleNamespace(memory_care_command="archive", target=tmp_path, json=True, from_file=manifest, limit=None)
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert len(payload["decision_ids"]) == 2
    assert "injected kernel error" in payload["refused"][0]["reason"]


def configure_hub(target, monkeypatch, card_id="old", path="memory/cards/old.md"):
    fake = FakeHub()
    monkeypatch.setattr(hubc, "load_settings", lambda _: hubc.HubSettings(mode="sync", scope="fleet"))
    monkeypatch.setattr(hubc, "client_for", lambda _: fake)
    with store.locked(target):
        store.save_published(
            target,
            {fake.identity: {"fleet": {card_id: {"revision": 1, "log_epoch": "epoch", "path": path}}}},
        )
    return fake


@pytest.mark.parametrize("visibility", ["private", "secret"])
@pytest.mark.parametrize("offline", [False, True])
def test_withheld_never_reaches_hub(tmp_path, monkeypatch, visibility, offline):
    path = setup_card(tmp_path, f"---\ncard_id: old\nvisibility: {visibility}\n---\nsecret\n".encode())
    fake = configure_hub(tmp_path, monkeypatch)
    fake.unavailable = offline
    d = move(tmp_path)
    undo = archive.restore(tmp_path, d)
    fake.unavailable = False
    counts = hub.retry_pending(tmp_path)
    assert path.exists()
    assert not any(a == "put" for a, _ in fake.calls)
    assert journal.load(tmp_path, undo.decision_id).hub["status"] == "skipped"
    assert journal.load(tmp_path, undo.decision_id).hub["reason"].startswith("visibility-")
    assert counts["skipped"] or undo.hub["status"] == "skipped"


def test_pending_put_replays_recorded_bytes(tmp_path, monkeypatch):
    path = setup_card(tmp_path)
    original = path.read_bytes().decode("utf-8")
    fake = configure_hub(tmp_path, monkeypatch)
    d = move(tmp_path)
    fake.unavailable = True
    undo = archive.restore(tmp_path, d)
    path.write_text("new public text")
    fake.unavailable = False
    hub.retry_pending(tmp_path)
    fields = [f for a, f in fake.calls if a == "put"][-1]
    assert fields["text"] == original
    assert fields["client_event_id"] == undo.hub["request"]["client_event_id"]
    assert store.published(tmp_path)[fake.identity]["fleet"]["old"]["content_hash"] == hubc.content_hash(original)


def test_retry_checks_current_visibility(tmp_path, monkeypatch):
    path = setup_card(tmp_path)
    fake = configure_hub(tmp_path, monkeypatch)
    d = move(tmp_path)
    fake.unavailable = True
    undo = archive.restore(tmp_path, d)
    path.write_text("---\nvisibility: private\n---\nsecret")
    fake.unavailable = False
    hub.retry_pending(tmp_path)
    assert not any(a == "put" for a, _ in fake.calls)
    assert journal.load(tmp_path, undo.decision_id).hub["status"] == "skipped"


def test_legacy_publication_identity_retire_revive_and_reads(tmp_path, monkeypatch):
    from brigade import memory_care_retire as retire, memory_card_reads as reads
    from brigade.memory_care_tend import TendConfig

    path = tmp_path / "memory/cards/Old Note.md"
    path.parent.mkdir(parents=True)
    text = "---\nexpires: 2000-01-01\n---\nlegacy\n"
    path.write_text(text)
    identity = sync.publication_identity(text, "memory/cards/Old Note.md")[0]
    fake = configure_hub(tmp_path, monkeypatch, identity, "memory/cards/Old Note.md")
    monkeypatch.setattr(reads, "load", lambda _: reads.ReadLedger(date(2000, 1, 1), {}))
    monkeypatch.setattr(reads, "hub_last_reads", lambda _: {identity: date.today()})
    assert (
        retire.plan(tmp_path, memory_cmd.MemoryCareConfig(), TendConfig(), today=date.today())["candidate_count"] == 0
    )
    d = move(tmp_path, "memory/cards/Old Note.md")
    assert d.hub["status"] == "done"
    archive.restore(tmp_path, d)
    assert [f["card_id"] for a, f in fake.calls if a in ("put", "retire")] == [identity, identity]


def test_concurrent_care_reports_busy(tmp_path):
    import subprocess
    import sys
    from brigade.inbox_lock import held_file_lock

    setup_card(tmp_path)
    lock = tmp_path / ".brigade/memory-care/care.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with held_file_lock(lock, deadline_seconds=1):
        result = subprocess.run(
            [sys.executable, "-m", "brigade", "memory", "care", "tend", "--target", str(tmp_path), "--json"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    assert result.returncode != 0
    assert "busy" in result.stderr
    assert "Traceback" not in result.stderr
    assert (tmp_path / "memory/cards/old.md").exists()


@pytest.mark.parametrize("action", ["archive", "undo"])
@pytest.mark.parametrize("phase", [1, 2])
@pytest.mark.parametrize("step", range(1, 7))
def test_single_injected_failure_always_preserves_card_bytes(tmp_path, monkeypatch, action, phase, step):
    path = setup_card(tmp_path)
    original = path.read_bytes()
    d = move(tmp_path) if action == "undo" else None
    execute = journal.kernel.execute
    calls = 0

    def injected(plan, **kwargs):
        nonlocal calls
        calls += 1
        if calls == phase:
            kwargs["inject"] = journal.kernel.FailureInjector(crash_after=step)
        return execute(plan, **kwargs)

    monkeypatch.setattr(journal.kernel, "execute", injected)
    try:
        archive.restore(tmp_path, d) if d else move(tmp_path)
    except archive.ArchiveRefused:
        pass
    copies = [p.read_bytes() for p in (path, tmp_path / "memory/archive/cards/old.md") if p.exists()]
    assert original in copies


def test_kernel_recovery_drift_abandons_without_blocking_care(tmp_path, monkeypatch):
    from tests.test_memory_archive_fix2 import test_real_kernel_drift_does_not_block_index

    test_real_kernel_drift_does_not_block_index(tmp_path, monkeypatch)
    assert tend.run(tmp_path, stages=[])["recovery"] == []
    assert (tmp_path / "memory/cards/old.md").exists()
    assert not (tmp_path / "memory/archive/cards/old.md").exists()
    decision_file = next((tmp_path / journal.JOURNAL_REL).glob("*/decision.json"))
    d = journal.load(tmp_path, decision_file.parent.name)
    assert journal.before_bytes(tmp_path, d, "memory/cards/old.md") is not None
