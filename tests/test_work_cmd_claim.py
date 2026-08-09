"""Atomic claim CAS and fail-closed ready filters for the work inbox (#738)."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from brigade import work_cmd
from brigade.work_cmd import claiming as claim_mod

from tests.work_cmd_test_helpers import _init_git_repo


def _add(target: Path, text: str, **kwargs):
    task, created = work_cmd._add_task(target, text, **kwargs)
    assert created
    return task


def test_healthy_claim_transitions_and_excludes_from_ready(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd.helpers,
        "_now",
        lambda: datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc),
    )
    task = _add(tmp_path, "Claimable work")

    assert (
        work_cmd.claim(
            target=tmp_path,
            task_id=task["id"],
            actor="lane-a",
            claim_id="claim-fixed-1",
            json_output=True,
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["created"] is True
    assert payload["status"] == "in_progress"
    assert payload["assignee"] == "lane-a"
    assert payload["claim"]["claim_id"] == "claim-fixed-1"

    ledger = work_cmd._read_task_ledger(tmp_path)
    stored = next(item for item in ledger["tasks"] if item["id"] == task["id"])
    assert stored["status"] == "in_progress"
    assert stored["assignee"] == "lane-a"
    assert stored["claim"]["actor"] == "lane-a"

    assert work_cmd.ready(target=tmp_path, json_output=True) == 0
    ready_payload = json.loads(capsys.readouterr().out)
    assert ready_payload["ready_count"] == 0
    assert task["id"] not in {item["id"] for item in ready_payload["ready"]}


def test_claim_blocked_task_fails_closed(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    clock = {"n": 0}

    def _now():
        clock["n"] += 1
        return datetime(2026, 8, 9, 12, 0, clock["n"], tzinfo=timezone.utc)

    monkeypatch.setattr(work_cmd.helpers, "_now", _now)
    blocker = _add(tmp_path, "Blocker")
    blocked = _add(tmp_path, "Blocked work")
    assert (
        work_cmd.task_edge_add(
            target=tmp_path,
            edge_type="blocks",
            source=blocker["id"],
            target_id=blocked["id"],
        )
        == 0
    )
    capsys.readouterr()

    rc = work_cmd.claim(
        target=tmp_path,
        task_id=blocked["id"],
        actor="lane-a",
        claim_id="claim-blocked",
        json_output=True,
    )
    assert rc == claim_mod.EXIT_GUARD_MISMATCH
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason"] == claim_mod.REASON_NOT_READY
    assert payload["exit_code"] == 13

    ledger = work_cmd._read_task_ledger(tmp_path)
    stored = next(item for item in ledger["tasks"] if item["id"] == blocked["id"])
    assert stored["status"] == "pending"
    assert "claim" not in stored


def test_claim_wrong_status_fails_closed(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd.helpers,
        "_now",
        lambda: datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc),
    )
    task = _add(tmp_path, "Deferred work")
    ledger = work_cmd._read_task_ledger(tmp_path)
    for item in ledger["tasks"]:
        if item.get("id") == task["id"]:
            item["status"] = "deferred"
    work_cmd._write_task_ledger(tmp_path, ledger)

    rc = work_cmd.claim(
        target=tmp_path,
        task_id=task["id"],
        actor="lane-a",
        json_output=True,
    )
    assert rc == 13
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason"] == claim_mod.REASON_NOT_READY


def test_concurrent_claim_rejection_two_processes(tmp_path, monkeypatch):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd.helpers,
        "_now",
        lambda: datetime(2026, 8, 9, 15, 0, 0, tzinfo=timezone.utc),
    )
    task = _add(tmp_path, "Race target")
    barrier = threading.Barrier(2)
    results: list[tuple[str, int, str, str]] = []
    lock = threading.Lock()

    def worker(actor: str, claim_id: str) -> None:
        barrier.wait(timeout=10)
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "brigade",
                "work",
                "claim",
                task["id"],
                "--actor",
                actor,
                "--claim-id",
                claim_id,
                "--target",
                str(tmp_path),
                "--json",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        with lock:
            results.append((actor, proc.returncode, proc.stdout, proc.stderr))

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(worker, "lane-a", "claim-a"),
            executor.submit(worker, "lane-b", "claim-b"),
        ]
        for future in futures:
            future.result(timeout=30)

    assert len(results) == 2
    codes = sorted(code for _actor, code, _out, _err in results)
    assert codes == [0, 13]

    winners = [item for item in results if item[1] == 0]
    losers = [item for item in results if item[1] == 13]
    assert len(winners) == 1
    assert len(losers) == 1
    winner_payload = json.loads(winners[0][2])
    loser_payload = json.loads(losers[0][2])
    assert winner_payload["created"] is True
    assert loser_payload["reason"] == claim_mod.REASON_ALREADY_CLAIMED
    assert loser_payload["holder"] == winners[0][0]
    assert "release" not in losers[0][2].casefold()
    assert "steal" not in losers[0][2].casefold()
    assert "release" not in losers[0][3].casefold()

    ledger = work_cmd._read_task_ledger(tmp_path)
    stored = next(item for item in ledger["tasks"] if item["id"] == task["id"])
    assert stored["status"] == "in_progress"
    assert stored["assignee"] == winners[0][0]


def test_same_actor_different_claim_id_rejects(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd.helpers,
        "_now",
        lambda: datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc),
    )
    task = _add(tmp_path, "Same actor race")
    assert work_cmd.claim(target=tmp_path, task_id=task["id"], actor="lane-a", claim_id="c1", json_output=True) == 0
    capsys.readouterr()
    rc = work_cmd.claim(target=tmp_path, task_id=task["id"], actor="lane-a", claim_id="c2", json_output=True)
    assert rc == 13
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason"] == claim_mod.REASON_ALREADY_CLAIMED
    assert payload["holder"] == "lane-a"


def test_idempotent_retry_same_claim_id(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd.helpers,
        "_now",
        lambda: datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc),
    )
    task = _add(tmp_path, "Retry safe")
    assert (
        work_cmd.claim(target=tmp_path, task_id=task["id"], actor="lane-a", claim_id="same-id", json_output=True) == 0
    )
    first = json.loads(capsys.readouterr().out)
    assert first["created"] is True
    assert (
        work_cmd.claim(target=tmp_path, task_id=task["id"], actor="lane-a", claim_id="same-id", json_output=True) == 0
    )
    second = json.loads(capsys.readouterr().out)
    assert second["created"] is False
    assert second["idempotent"] is True


def test_if_actor_and_if_status_guards(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd.helpers,
        "_now",
        lambda: datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc),
    )
    task = _add(tmp_path, "Guarded")
    assert work_cmd.claim(target=tmp_path, task_id=task["id"], actor="lane-a", claim_id="g1", json_output=True) == 0
    capsys.readouterr()

    rc = work_cmd.release(
        target=tmp_path,
        task=[task["id"]],
        if_actor="other-lane",
        json_output=True,
    )
    assert rc == 13
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason"] == claim_mod.REASON_GUARD_MISMATCH

    rc = work_cmd.release(
        target=tmp_path,
        task=[task["id"]],
        if_status="pending",
        json_output=True,
    )
    assert rc == 13
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason"] == claim_mod.REASON_GUARD_MISMATCH

    assert (
        work_cmd.release(
            target=tmp_path,
            task=[task["id"]],
            if_actor="lane-a",
            if_status="in_progress",
            json_output=True,
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["released_count"] == 1


def test_release_empty_filter_fail_closed(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd.helpers,
        "_now",
        lambda: datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc),
    )
    task = _add(tmp_path, "Hold this")
    assert work_cmd.claim(target=tmp_path, task_id=task["id"], actor="lane-a", claim_id="hold", json_output=True) == 0
    capsys.readouterr()

    rc = work_cmd.release(target=tmp_path, actor=[""], json_output=True)
    assert rc == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason"] == claim_mod.REASON_EMPTY_FILTER

    rc = work_cmd.release(target=tmp_path, actor=["   "], json_output=True)
    assert rc == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason"] == claim_mod.REASON_EMPTY_FILTER

    ledger = work_cmd._read_task_ledger(tmp_path)
    stored = next(item for item in ledger["tasks"] if item["id"] == task["id"])
    assert stored["status"] == "in_progress"


def test_release_compares_claim_id_against_newer_claim(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    clock = {"n": 0}

    def _now():
        clock["n"] += 1
        return datetime(2026, 8, 9, 12, 0, clock["n"], tzinfo=timezone.utc)

    monkeypatch.setattr(work_cmd.helpers, "_now", _now)
    task = _add(tmp_path, "Reaper target")
    assert work_cmd.claim(target=tmp_path, task_id=task["id"], actor="lane-a", claim_id="old", json_output=True) == 0
    capsys.readouterr()
    assert (
        work_cmd.reassign(
            target=tmp_path,
            task_id=task["id"],
            to_actor="lane-a",
            claim_id="old",
            new_claim_id="new",
            json_output=True,
        )
        == 0
    )
    capsys.readouterr()

    ledger = work_cmd._read_task_ledger(tmp_path)
    stored = next(item for item in ledger["tasks"] if item["id"] == task["id"])
    with pytest.raises(claim_mod.ClaimError) as excinfo:
        claim_mod.apply_release_to_task(ledger, stored, claim_id="old")
    assert excinfo.value.reason == claim_mod.REASON_CLAIM_ID_MISMATCH


def test_claim_next_picks_highest_priority_ready(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    clock = {"n": 0}

    def _now():
        clock["n"] += 1
        return datetime(2026, 8, 9, 12, 0, clock["n"], tzinfo=timezone.utc)

    monkeypatch.setattr(work_cmd.helpers, "_now", _now)
    low = _add(tmp_path, "Low priority", priority="low")
    urgent = _add(tmp_path, "Urgent priority", priority="urgent")
    _add(tmp_path, "Normal priority", priority="normal")

    assert (
        work_cmd.claim(
            target=tmp_path,
            claim_next=True,
            actor="lane-a",
            claim_id="next-1",
            json_output=True,
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["task_id"] == urgent["id"]
    assert payload["task_id"] != low["id"]


def test_text_exit_13_names_holder_without_release_command(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd.helpers,
        "_now",
        lambda: datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc),
    )
    task = _add(tmp_path, "Conflict copy")
    assert work_cmd.claim(target=tmp_path, task_id=task["id"], actor="lane-a", claim_id="c1") == 0
    capsys.readouterr()
    rc = work_cmd.claim(target=tmp_path, task_id=task["id"], actor="lane-b", claim_id="c2")
    assert rc == 13
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "lane-a" in combined
    assert "release" not in combined.casefold()
    assert "steal" not in combined.casefold()


def test_stale_claims_listed_by_age(tmp_path, monkeypatch):
    _init_git_repo(tmp_path)
    now = datetime(2026, 8, 9, 18, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(work_cmd.helpers, "_now", lambda: now)
    task = _add(tmp_path, "Old claim")
    assert work_cmd.claim(target=tmp_path, task_id=task["id"], actor="lane-a", claim_id="stale-1") == 0
    ledger = work_cmd._read_task_ledger(tmp_path)
    for item in ledger["tasks"]:
        if item.get("id") == task["id"]:
            item["claim"]["claimed_at"] = (now - timedelta(hours=30)).isoformat()
    work_cmd._write_task_ledger(tmp_path, ledger)

    payload = work_cmd._stale_claim_payload(tmp_path, stale_after_hours=24)
    assert payload["stale_count"] == 1
    assert payload["stale"][0]["claim_id"] == "stale-1"
