"""Third review crash, identity and replay regressions."""

import pytest

from brigade import memory_archive as archive, memory_care_journal as journal, memory_care_tend as tend
from brigade import memory_care_hub as hub, memory_hub_client as hubc, memory_hub_store as store
from tests.test_memory_archive import setup_card
from tests.test_memory_archive_review import move, configure_hub
from tests.test_memory_archive_phases import interrupt_second_plan


@pytest.mark.parametrize("interrupted", [False, True])
def test_index_identity_survives_empty_rebase(tmp_path, monkeypatch, interrupted):
    path = setup_card(tmp_path)
    original = path.read_bytes()
    fake = configure_hub(tmp_path, monkeypatch)
    index = tmp_path / "MEMORY.md"
    if interrupted:
        execute = interrupt_second_plan(monkeypatch)
        with pytest.raises(RuntimeError):
            move(tmp_path)
        index.write_bytes(b"New unrelated index text\n")
        monkeypatch.setattr(journal.kernel, "execute", execute)
        tend.run(tmp_path, stages=["hub"])
        d = journal.entries(tmp_path)[0]
    else:
        d = move(tmp_path)
        index.write_bytes(b"New unrelated index text\n")
    tend.undo(tmp_path, d.decision_id)
    assert b"New unrelated index text\n" in index.read_bytes()
    if interrupted:
        assert index.read_bytes() == b"New unrelated index text\n"
    assert path.read_bytes() == original
    assert all(fields["text"] == original.decode() for action, fields in fake.calls if action == "put")


@pytest.mark.parametrize("changed", ["scope", "hub"])
def test_replay_destination_change_needs_review(tmp_path, monkeypatch, changed):
    setup_card(tmp_path)
    fake = configure_hub(tmp_path, monkeypatch)
    fake.unavailable = True
    d = move(tmp_path)
    fake.unavailable = False
    if changed == "scope":
        monkeypatch.setattr(hubc, "load_settings", lambda _: hubc.HubSettings(mode="sync", scope="other"))
    else:
        fake.identity = "other-hub"
    with store.locked(tmp_path):
        store.save_published(
            tmp_path,
            {
                fake.identity: {
                    "other" if changed == "scope" else "fleet": {"old": {"revision": 9, "log_epoch": "epoch"}}
                }
            },
        )
    fake.calls.clear()
    for _ in range(2):
        result = tend.run(tmp_path, stages=["hub"])
        assert result["stages"]["hub"]["needs_review"] == 1
        assert changed in str(result["stages"]["hub"]["review_items"])
    assert fake.calls == []
    assert journal.load(tmp_path, d.decision_id).hub["status"] == "needs_review"


@pytest.mark.parametrize("retire_pending", [False, True])
@pytest.mark.parametrize("saved_request", [False, True])
@pytest.mark.parametrize("replacement", [None, b"---\ncard_id: another\n---\nOther\n"])
def test_revival_requires_current_identity(tmp_path, monkeypatch, replacement, saved_request, retire_pending):
    path = setup_card(tmp_path)
    fake = configure_hub(tmp_path, monkeypatch)
    fake.unavailable = retire_pending
    d = move(tmp_path)
    fake.unavailable = True
    undo = archive.restore(tmp_path, d)
    if not saved_request:
        journal.update_hub(tmp_path, undo.decision_id, {k: v for k, v in undo.hub.items() if k != "request"})
    path.unlink() if replacement is None else path.write_bytes(replacement)
    fake.unavailable = retire_pending
    fake.calls.clear()
    hub.retry_pending(tmp_path)
    assert not any(action == "put" for action, _ in fake.calls)
    saved = journal.load(tmp_path, undo.decision_id).hub
    assert saved["status"] == "superseded"
    assert saved["reason"]


def crash_finish(monkeypatch, *, count=None):
    execute = journal.kernel.execute
    persist = journal.kernel._persist_receipt

    def interrupted(plan, **kwargs):
        if plan.operation_id.endswith("-finish"):
            if count is not None:
                kwargs["inject"] = journal.kernel.FailureInjector(crash_after=count)
            else:

                def stop(*args, **kw):
                    raise RuntimeError("before receipt")

                monkeypatch.setattr(journal.kernel, "_persist_receipt", stop)
        try:
            return execute(plan, **kwargs)
        finally:
            monkeypatch.setattr(journal.kernel, "_persist_receipt", persist)

    monkeypatch.setattr(journal.kernel, "execute", interrupted)
    return execute


def test_finished_archive_drift_remains_visible_and_undoable(tmp_path, monkeypatch):
    path = setup_card(tmp_path)
    original = path.read_bytes()
    fake = configure_hub(tmp_path, monkeypatch)
    # BaseException simulates process death without the kernel's Exception cleanup.
    persist = journal.kernel._persist_receipt

    class Killed(BaseException):
        pass

    def stop(op_dir, *args, **kwargs):
        if op_dir.name.endswith("-finish"):
            raise Killed()
        return persist(op_dir, *args, **kwargs)

    monkeypatch.setattr(journal.kernel, "_persist_receipt", stop)
    with pytest.raises(Killed):
        move(tmp_path)
    monkeypatch.setattr(journal.kernel, "_persist_receipt", persist)
    (tmp_path / "MEMORY.md").write_bytes(b"Edited index\n")
    result = tend.run(tmp_path, stages=["hub"])
    assert result["recovery"][0]["phase"] == "abandoned"
    d = journal.entries(tmp_path)[0]
    assert any(a == "retire" for a, _ in fake.calls)
    tend.undo(tmp_path, d.decision_id)
    assert path.read_bytes() == original
    assert b"Edited index" in (tmp_path / "MEMORY.md").read_bytes()


@pytest.mark.parametrize("recreated", [False, True])
def test_annotate_precedes_supersede_and_retry_keeps_archive(tmp_path, monkeypatch, recreated):
    path = setup_card(tmp_path)
    original = path.read_bytes()
    execute = crash_finish(monkeypatch, count=2)
    with pytest.raises(archive.ArchiveRefused):
        move(tmp_path)
    monkeypatch.setattr(journal.kernel, "execute", execute)
    path.write_bytes(b"recreated") if recreated else path.unlink()
    (tmp_path / "MEMORY.md").write_bytes(b"Edited index\n")
    supersede = journal.kernel.supersede

    def stop(operation_id, **kwargs):
        decision_id = operation_id.removeprefix("care-").removesuffix("-finish")
        assert journal._payload(tmp_path, decision_id)["phase"] == "abandoned"
        raise RuntimeError("between annotation and supersede")

    monkeypatch.setattr(journal.kernel, "supersede", stop)
    with pytest.raises(RuntimeError):
        tend.run(tmp_path, stages=[])
    monkeypatch.setattr(journal.kernel, "supersede", supersede)
    tend.run(tmp_path, stages=[])
    assert (tmp_path / "memory/archive/cards/old.md").read_bytes() == original
    assert tend.run(tmp_path, stages=[])["recovery"] == []


def test_partial_recovery_reports_destinations_and_repairs_index(tmp_path, monkeypatch):
    path = setup_card(tmp_path)
    index = tmp_path / "MEMORY.md"
    before = index.read_bytes()
    execute = crash_finish(monkeypatch, count=2)
    with pytest.raises(archive.ArchiveRefused):
        move(tmp_path)
    monkeypatch.setattr(journal.kernel, "execute", execute)
    path.write_bytes(b"recreated")
    result = tend.run(tmp_path, stages=[])
    assert "MEMORY.md" in result["recovery"][0]["committed_destinations"]
    assert index.read_bytes() == before


def test_superseded_kernel_id_cannot_be_reused(tmp_path):
    kernel = journal.kernel
    path = tmp_path / "example"
    path.write_bytes(b"before")

    def plan():
        return kernel.build_plan(
            operation_id="example",
            projector="test",
            source_fingerprint="test",
            target=tmp_path,
            mutations=[
                kernel.mutation(
                    destination=path,
                    mutation="replace",
                    expected_before=kernel.content_digest(b"before"),
                    desired_after=kernel.content_digest(b"after"),
                    staged_bytes=b"after",
                )
            ],
        )

    kernel.execute(plan(), target=tmp_path, inject=kernel.FailureInjector(crash_after=0))
    kernel.supersede("example", target=tmp_path)
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    with pytest.raises(kernel.ProjectionError, match="superseded"):
        kernel.execute(plan(), target=tmp_path)
    assert before == {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


@pytest.mark.parametrize("action", ["archive", "undo"])
@pytest.mark.parametrize("phase", ["prepare", "finish"])
@pytest.mark.parametrize("side", ["before", "after", "crash"])
def test_every_mutation_recovers_to_accounted_state(tmp_path, monkeypatch, action, phase, side):
    """Enumerate the real plans, with one process death or failure per fixture."""
    kernel = journal.kernel
    execute = kernel.execute
    seen = []

    def observe(plan, **kwargs):
        if plan.operation_id.endswith("-" + phase):
            seen.append(len(plan.mutations))
        return execute(plan, **kwargs)

    baseline = tmp_path / "baseline"
    setup_card(baseline)
    d = move(baseline) if action == "undo" else None
    with monkeypatch.context() as patch:
        patch.setattr(kernel, "execute", observe)
        archive.restore(baseline, d) if d else move(baseline)
    assert len(seen) == 1
    for step in range(seen[0]):
        target = tmp_path / str(step)
        path = setup_card(target)
        original = path.read_bytes()
        sentinel = target / "unrelated.txt"
        sentinel.write_bytes(b"must survive")
        d = move(target) if action == "undo" else None
        initial = {p.relative_to(target).as_posix(): p.read_bytes() for p in target.rglob("*") if p.is_file()}
        injected = []

        class Killed(BaseException):
            pass

        def interrupt(plan, *, injected=injected, step=step, **kwargs):
            if plan.operation_id.endswith("-" + phase) and not injected:
                injected.append(True)
                kwargs["inject"] = (
                    kernel.FailureInjector(crash_after=step)
                    if side == "crash"
                    else kernel.FailureInjector(boundary=f"commit:{step}:{side}", error=Killed())
                )
            return execute(plan, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(kernel, "execute", interrupt)
            try:
                archive.restore(target, d) if d else move(target)
            except (archive.ArchiveRefused, Killed):
                pass
        assert injected
        recovered = tend.run(target, stages=[])
        copies = [p.read_bytes() for p in (path, target / "memory/archive/cards/old.md") if p.exists()]
        assert original in copies, (action, phase, side, step)
        final = {p.relative_to(target).as_posix(): p.read_bytes() for p in target.rglob("*") if p.is_file()}
        allowed = {"memory/cards/old.md", "memory/archive/cards/old.md", "MEMORY.md"}
        for rel in initial.keys() | final.keys():
            # Both the care journal and the real projection recovery journal are expected writes.
            if rel not in allowed and not rel.startswith((".brigade/memory-care/", ".brigade/projection/operations/")):
                assert initial.get(rel) == final.get(rel), rel
        assert tend.run(target, stages=[])["recovery"] == []
        assert not kernel.unfinished_operations(target)
        listed = {d.decision_id for d in journal.entries(target)}
        for decision_file in (target / journal.JOURNAL_REL).glob("*/decision.json"):
            p = journal._payload(target, decision_file.parent.name)
            assert (
                p["phase"] in ("committed", "undone")
                or (
                    p["phase"] == "abandoned"
                    and p["abandoned_reason"]
                    and (
                        p["decision_id"] in listed
                        or any(item["decision_id"] == p["decision_id"] for item in recovered["recovery"])
                    )
                )
                or (p.get("restorable") and p["decision_id"] in listed)
            )


def test_configured_index_is_never_an_archive_card(tmp_path):
    from brigade import memory_cmd

    path = setup_card(tmp_path)
    before = path.read_bytes()
    with pytest.raises(archive.ArchiveRefused, match="index"):
        move(tmp_path, config=memory_cmd.MemoryCareConfig(index_paths=("memory/cards/old.md",)))
    assert path.read_bytes() == before


@pytest.mark.parametrize("side", ["before", "after"])
def test_interrupted_index_repair_is_recovered(tmp_path, monkeypatch, side):
    path = setup_card(tmp_path)
    index = tmp_path / "MEMORY.md"
    original_index = index.read_bytes()
    execute = crash_finish(monkeypatch, count=2)
    with pytest.raises(archive.ArchiveRefused):
        move(tmp_path)
    monkeypatch.setattr(journal.kernel, "execute", execute)
    path.write_bytes(b"recreated")

    class Killed(BaseException):
        pass

    def interrupt(plan, **kwargs):
        if plan.operation_id.endswith("-repair"):
            kwargs["inject"] = journal.kernel.FailureInjector(boundary=f"commit:0:{side}", error=Killed())
        return execute(plan, **kwargs)

    monkeypatch.setattr(journal.kernel, "execute", interrupt)
    with pytest.raises(Killed):
        tend.run(tmp_path, stages=[])
    monkeypatch.setattr(journal.kernel, "execute", execute)
    tend.run(tmp_path, stages=[])
    assert index.read_bytes() == original_index
    assert not journal.kernel.unfinished_operations(tmp_path)
    assert tend.run(tmp_path, stages=[])["recovery"] == []


def test_pending_destination_survives_configuration_outage(tmp_path, monkeypatch):
    setup_card(tmp_path)
    fake = configure_hub(tmp_path, monkeypatch)
    fake.unavailable = True
    d = move(tmp_path)
    settings = hubc.load_settings

    def unavailable(_):
        raise hubc.HubConfigError("temporarily unavailable")

    monkeypatch.setattr(hubc, "load_settings", unavailable)
    hub.retry_pending(tmp_path)
    saved = journal.load(tmp_path, d.decision_id).hub
    assert saved["request"] == d.hub["request"]
    assert saved["hub"] == d.hub["hub"]
    monkeypatch.setattr(hubc, "load_settings", settings)
