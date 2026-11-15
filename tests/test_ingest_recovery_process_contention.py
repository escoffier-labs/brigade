"""Cross-process coverage for ingest and memory care lock contention."""

from __future__ import annotations

import contextlib
import multiprocessing
import time
from pathlib import Path

from brigade import ingest, instruction_overflow, memory_care_pending
from brigade import memory_care_journal as journal
from brigade import memory_inbox_drain as drain
from tests._home import set_home
from tests._instruction_fixtures import field_workspace, handoff_text, write_handoff
from tests.test_ingest_overflow_card_trust import CARD, NOTE, TODAY, _drafts, _ingest


def _contend(workspace, role, barrier, sender):
    """Hold opposite first locks, then attempt each other's lock together."""
    from brigade import inbox_lock

    workspace = Path(workspace)
    real = inbox_lock.held_file_lock
    first = "document.lock" if role == "ingest" else "care.lock"
    second = "care.lock" if role == "ingest" else "document.lock"
    acquired, attempted, timed_out = [], [], []

    @contextlib.contextmanager
    def synchronized(path, *args, **kwargs):
        name = Path(path).name
        initial = name == first and not acquired
        crossing = name == second and not attempted
        if crossing:
            attempted.append(name)
            barrier.wait(timeout=3)
        try:
            with real(path, *args, **kwargs) as held:
                if initial:
                    acquired.append(name)
                    barrier.wait(timeout=3)
                yield held
        except inbox_lock.InboxLockTimeout:
            timed_out.append(name)
            raise

    inbox_lock.held_file_lock = synchronized
    try:
        if role == "ingest":
            stats = ingest.IngestStats()
            code = ingest.ingest_into(
                source=workspace, owner=workspace, stats=stats, promote_cards=True, route_documents=True
            )
            result = {"status": "returned", "code": code, "actions": stats.actions, "failed": stats.failed}
        else:
            result = {"status": "returned", "recovery": journal.resolve_prepared(workspace)}
    except journal.JournalConflict as exc:
        result = {"status": "busy", "reason": str(exc)}
    except OSError as exc:
        if str(exc) != "document publication busy" or not isinstance(exc.__cause__, inbox_lock.InboxLockTimeout):
            raise
        result = {"status": "busy", "reason": str(exc)}
    except BaseException as exc:
        result = {"status": "unexpected", "type": type(exc).__name__, "reason": str(exc)}
    finally:
        inbox_lock.held_file_lock = real
    sender.send({"role": role, "acquired": acquired, "attempted": attempted, "timed_out": timed_out, **result})
    sender.close()


def test_ingest_and_recovery_contend_without_deadlock_or_lost_draft(tmp_path, monkeypatch):
    started = time.monotonic()
    deadline = started + 8
    ws, home, _ = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    documents = {name: (ws / name).read_bytes() for name in ("USER.md", "AGENTS.md")}

    def interrupt(*_args, **_kwargs):
        raise OSError("interrupted publication")

    with monkeypatch.context() as patch:
        patch.setattr(ingest, "_append_document_nofollow", interrupt)
        assert _ingest(ws) == 0
    recovery_draft = _drafts(ws)[0]
    recovery_bytes = recovery_draft.read_bytes()
    assert not (ws / CARD).exists()
    assert any(row["append"] for row in memory_care_pending.load(ws).values())

    contender = "Prefer UTC timestamps in reports."
    source = write_handoff(ws, "2026-11-14-contender.md", handoff_text("USER.md", contender, type_="preference"))
    source_bytes = source.read_bytes()
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    processes, receivers = [], []
    try:
        for role in ("ingest", "recovery"):
            receiver, sender = context.Pipe(duplex=False)
            process = context.Process(target=_contend, args=(str(ws), role, barrier, sender))
            process.start()
            sender.close()
            processes.append(process)
            receivers.append(receiver)
        for process in processes:
            process.join(timeout=max(0, deadline - time.monotonic()))
            assert not process.is_alive(), "ingest/recovery deadlocked"
            assert process.exitcode == 0
        results = []
        for receiver in receivers:
            assert receiver.poll(0.1), "worker returned no result"
            results.append(receiver.recv())
    finally:
        for process in processes:
            if process.is_alive():
                process.kill()
                process.join(timeout=0.5)
            process.close()
        for receiver in receivers:
            receiver.close()

    ingested, recovered = results
    assert ingested["acquired"] == ["document.lock"] and ingested["attempted"] == ["care.lock"], results
    assert recovered["acquired"] == ["care.lock"] and recovered["attempted"] == ["document.lock"], results
    assert any(result["timed_out"] for result in results), results
    assert ingested["status"] == "returned" and ingested["code"] == 0 and ingested["failed"] == 0, results
    assert recovered["status"] == "returned" or (
        recovered["status"] == "busy" and recovered["reason"] in {"memory care busy", "document publication busy"}
    ), results

    card = ws / CARD
    card_text = card.read_text() if card.exists() else ""
    assert card_text.count(NOTE) <= 1 and card_text.count(contender) <= 1
    if card.exists():
        assert instruction_overflow.card_trust_problem(card_text) is None
        assert card.stat().st_size <= 12000
    assert recovery_draft.read_bytes() == recovery_bytes
    if contender not in card_text:
        held = [path.read_text() for path in _drafts(ws) if source_bytes in path.read_bytes()]
        assert source.exists() or len(held) == 1
        if held:
            assert "reason: document-destination-unwritable" in held[0]
            assert any("document-destination-unwritable" in action for action in ingested["actions"])
    if source.exists():
        assert source.read_bytes() == source_bytes
    assert {name: (ws / name).read_bytes() for name in documents} == documents

    # Once contention ends, normal recovery and drain deliver both blocks exactly once.
    journal.resolve_prepared(ws)
    if source.exists():
        assert ingest.run(target=ws, promote_cards=True, route_documents=True) == 0
    result = drain.drain(ws, today=TODAY, deadline_days=14)
    assert not result["skipped"], result
    card_text = card.read_text()
    assert card_text.count(NOTE) == card_text.count(contender) == 1
    assert sorted(block.strip() for block in card_text.split("\n---\n", 1)[1].strip().split("\n\n")) == sorted(
        [NOTE, contender]
    )
    assert instruction_overflow.card_trust_problem(card_text) is None
    assert not _drafts(ws)
    assert {name: (ws / name).read_bytes() for name in documents} == documents
    assert time.monotonic() - started < 10, "keep this contention test in the fast normal suite"
