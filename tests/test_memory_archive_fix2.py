"""Second review regressions, including real kernel crash recovery."""

from datetime import date, timedelta

import pytest

from brigade import memory_archive as archive, memory_card_reads as reads, memory_care_retire as retire
from brigade import memory_care_journal as journal, memory_care_tend as tend, memory_cmd
from tests.test_memory_archive_review import move, configure_hub
from tests.test_memory_archive import setup_card
from tests.test_memory_archive_phases import interrupt_second_plan


@pytest.mark.parametrize(
    "header",
    [
        "tags:\n  - value",
        "tags:\n  - nested:\n      - value",
        "description: >\n  folded\n  value",
        "title: [draft] notes",
    ],
)
def test_valid_headers_archive(tmp_path, header):
    setup_card(tmp_path, f"---\n{header}\n---\nbody".encode())
    assert move(tmp_path).kind == "archive"


@pytest.mark.parametrize(
    "header", ['"archive": never', "'archive': never", "Archive: never", "archive: never\narchive: auto"]
)
def test_all_pin_spellings_refused(tmp_path, header):
    setup_card(tmp_path, f"---\n{header}\n---\nbody".encode())
    with pytest.raises(archive.ArchiveRefused, match="never"):
        move(tmp_path)


def test_stale_revival_superseded(tmp_path, monkeypatch):
    from brigade import memory_care_hub as hub

    setup_card(tmp_path)
    fake = configure_hub(tmp_path, monkeypatch)
    fake.unavailable = True
    d = move(tmp_path)
    undo = archive.restore(tmp_path, d)
    fake.unavailable = False
    move(tmp_path)
    fake.calls.clear()
    hub.retry_pending(tmp_path)
    assert not any(a == "put" for a, _ in fake.calls)
    assert journal.load(tmp_path, undo.decision_id).hub["status"] == "superseded"


def test_real_kernel_drift_does_not_block_index(tmp_path, monkeypatch):
    path = setup_card(tmp_path)
    (tmp_path / "memory/cards/second.md").write_bytes(b"second")
    (tmp_path / "MEMORY.md").write_text("- [Old](memory/cards/old.md)\n- [Second](memory/cards/second.md)\n")
    execute = journal.kernel.execute

    def crash(plan, **kwargs):
        if plan.operation_id.endswith("-finish"):
            kwargs["inject"] = journal.kernel.FailureInjector(crash_after=1)
        return execute(plan, **kwargs)

    monkeypatch.setattr(journal.kernel, "execute", crash)
    with pytest.raises(archive.ArchiveRefused):
        move(tmp_path)
    path.write_bytes(b"recreated")
    monkeypatch.setattr(journal.kernel, "execute", execute)
    result = tend.run(tmp_path, stages=[])
    assert result["recovery"][0]["phase"] == "abandoned"
    assert "--force" not in str(result)
    assert not journal.kernel.unfinished_operations(tmp_path)
    move(tmp_path, "memory/cards/second.md")
    assert path.read_bytes() == b"recreated"


def test_read_only_between_phases_abandons(tmp_path, monkeypatch):
    path = setup_card(tmp_path)
    execute = interrupt_second_plan(monkeypatch)
    with pytest.raises(RuntimeError):
        move(tmp_path)
    monkeypatch.setattr(journal.kernel, "execute", execute)
    (tmp_path / ".brigade/memory-care.toml").write_text('read_only_paths = ["memory/cards/**"]\n')
    assert tend.run(tmp_path, stages=[])["recovery"][0]["phase"] == "abandoned"
    assert tend.run(tmp_path, stages=[])["recovery"] == []
    assert path.exists()


def test_abandoned_undo_never_revives(tmp_path, monkeypatch):
    path = setup_card(tmp_path)
    fake = configure_hub(tmp_path, monkeypatch)
    d = move(tmp_path)
    finish = journal.finish_prepared

    def drift(target, decision_id):
        path.write_bytes(b"edited during undo")
        return finish(target, decision_id)

    monkeypatch.setattr(journal, "finish_prepared", drift)
    undo = archive.restore(tmp_path, d)
    assert journal._payload(tmp_path, undo.decision_id)["phase"] == "abandoned"
    assert not any(a == "put" for a, _ in fake.calls)


def test_undo_of_undo_refused(tmp_path):
    setup_card(tmp_path)
    undo = archive.restore(tmp_path, move(tmp_path))
    with pytest.raises(ValueError, match="undo an undo"):
        tend.undo(tmp_path, undo.decision_id)


def test_restorable_abandoned_is_listed(tmp_path, monkeypatch):
    path = setup_card(tmp_path)
    execute = interrupt_second_plan(monkeypatch)
    with pytest.raises(RuntimeError):
        move(tmp_path)
    path.unlink()
    monkeypatch.setattr(journal.kernel, "execute", execute)
    recovered = tend.run(tmp_path, stages=[])["recovery"][0]["decision_id"]
    assert recovered in [d.decision_id for d in journal.entries(tmp_path)]


def test_dot_root_refused(tmp_path):
    (tmp_path / "AGENTS.md").write_text("instructions")
    with pytest.raises(ValueError, match="card_roots"):
        memory_cmd._iter_cards(tmp_path, memory_cmd.MemoryCareConfig(card_roots=(".",)))


def test_cap_counts_success_and_plan_shares_pin_check(tmp_path):
    for name, extra in [("a", "archive: never # pinned\n"), ("b", ""), ("c", "")]:
        p = tmp_path / f"memory/cards/{name}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"---\nexpires: 2000-01-01\n{extra}---\nended")
    reads.record(tmp_path, [], today=date(2000, 1, 1))
    cfg = memory_cmd.MemoryCareConfig()
    tc = tend.TendConfig(max_archives_per_run=1)
    assert retire.plan(tmp_path, cfg, tc, today=date.today())["candidate_count"] == 2
    dest = tmp_path / "memory/archive/cards/b.md"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"occupied")
    result = retire.apply(tmp_path, cfg, tc, today=date.today())
    assert result["archived"] == 1
    assert not (tmp_path / "memory/cards/c.md").exists()


def test_hub_days_use_validated_client_response(tmp_path, monkeypatch, capsys):
    from brigade import memory_hub_client as hubc

    class Client:
        def call(self, action, **kwargs):
            if action == "status":
                return {"credential": "node"}
            return {
                "scope": "fleet",
                "next_after": None,
                "read_counts_since_day": 1,
                "rows": [
                    {"card_id": "old", "reads_7d": 1, "reads_30d": 1, "total_reads": 1, "last_read_day": 20000},
                    {"card_id": "overflow", "reads_7d": 1, "reads_30d": 1, "total_reads": 1, "last_read_day": 10**30},
                ],
            }

    monkeypatch.setattr(hubc, "load_settings", lambda _: hubc.HubSettings(mode="sync", scope="fleet"))
    monkeypatch.setattr(hubc, "client_for", lambda _: Client())
    assert reads.hub_last_reads(tmp_path) == {"old": date(1970, 1, 1) + timedelta(days=20000)}
    assert "skipped" in capsys.readouterr().err


def test_windows_crlf_manifest_pin(tmp_path):
    path = setup_card(tmp_path, b"---\r\narchive: never\r\n---\r\nkeep")
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text('{"card":"memory/cards/old.md","reason":"ended"}\n')
    result = tend.archive_manifest(tmp_path, manifest)
    assert result["archived"] == 0
    assert "archive: never" in result["refused"][0]["reason"]
    assert path.exists()


@pytest.mark.parametrize("reply", [None, [], {}, {"rows": None}, {"rows": [None]}])
def test_malformed_hub_shapes_reported(tmp_path, monkeypatch, capsys, reply):
    from brigade import memory_hub_client as hubc

    class Client:
        def call(self, action, **kwargs):
            return {"credential": "node"} if action == "status" else reply

    monkeypatch.setattr(hubc, "load_settings", lambda _: hubc.HubSettings(mode="sync", scope="fleet"))
    monkeypatch.setattr(hubc, "client_for", lambda _: Client())
    tend.run(tmp_path)
    tend.after_command(tmp_path)
    assert "skipped hub read stats" in capsys.readouterr().err


def test_indented_malformed_fence_cannot_hide_pin(tmp_path):
    setup_card(tmp_path, b"  ---\narchive: never\n---\nkeep")
    with pytest.raises(archive.ArchiveRefused):
        move(tmp_path)
