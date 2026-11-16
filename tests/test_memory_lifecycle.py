"""Unused-card lifecycle, protection, search and rollback acceptance."""

from tests.memory_read_fixtures import automatic_archive_test_mode as automatic_archive_test_mode
from tests.memory_read_fixtures import windows_scheduler as windows_scheduler

from datetime import date, datetime, timezone
import json
import os
import shutil

import pytest

from tests.memory_read_fixtures import signed_memory_authority as signed_memory_authority

from brigade import memory_card_reads as reads, memory_cmd, memory_lifecycle as life
from brigade import memory_care_journal as journal, memory_care_tend as tend
from tests.memory_read_fixtures import coverage, signing_key
from brigade.memory_retrieval_eval import DEFAULT_FIXTURE_ROOT, run_eval

TODAY = date(2026, 11, 15)


def card(root, name="old", meta="trust: agent-unreviewed\n", body="retrievalwidget"):
    path = root / f"memory/cards/{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(f"---\ncard_id: {name}\n{meta}---\n{body}\n".encode())
    stamp = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
    os.utime(path, (stamp, stamp))
    signing_key(root)
    reads.record(root, [], today=date(2026, 1, 1))
    return path


def observed_run(root, *, today, **kwargs):
    ledger = reads.load(root)
    coverage(root, ledger.tracking_since, today)
    return life.run(root, today=today, **kwargs)


@pytest.mark.parametrize("crlf", [False, True])
def test_unused_archive_search_and_one_command_restore(tmp_path, crlf):
    path = card(tmp_path)
    if crlf:
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
        stamp = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
        os.utime(path, (stamp, stamp))
    original = path.read_bytes()
    payload = observed_run(tmp_path, today=TODAY)
    assert [r["to_state"] for r in payload["transitions"]] == ["stale", "archived"]
    assert not path.exists()
    assert (tmp_path / "memory/archive/cards/old.md").read_bytes() == original
    assert not memory_cmd.search_cards_payload(tmp_path, "retrievalwidget")["matches"]
    assert memory_cmd.search_cards_payload(tmp_path, "retrievalwidget", include_archived=True)["matches"]
    for row in payload["transitions"]:
        assert journal._payload(tmp_path, row["decision_id"])["actor"] == "policy"
        assert row["policy_digest"]
    tend.undo(tmp_path, payload["transitions"][-1]["decision_id"])
    assert path.read_bytes() == original
    assert memory_cmd.search_cards_payload(tmp_path, "retrievalwidget")["matches"]
    assert not observed_run(tmp_path, today=TODAY)["transitions"][-1:][0]["to_state"] == "archived"


@pytest.mark.parametrize(
    "meta",
    [
        "trust: human\n",
        "trust: agent-unreviewed\npinned: true\n",
        "trust: agent-unreviewed\narchive: never\n",
        "",
        "trust: agent-unreviewed\nauthor_type: human\n",
    ],
)
def test_human_pinned_and_unknown_cards_stay(tmp_path, meta):
    path = card(tmp_path, meta=meta)
    assert not observed_run(tmp_path, today=TODAY)["transitions"]
    assert path.exists()


@pytest.mark.parametrize(
    "reference",
    [
        "memory/cards/active.md",
        ".brigade/memory-care/runbooks/example.json",
        ".brigade/care/schedule.json",
        "docs/runbooks/operate.md",
        "memory/pinned.md",
    ],
)
def test_references_protect(tmp_path, reference):
    path = card(tmp_path)
    ref = tmp_path / reference
    ref.parent.mkdir(parents=True, exist_ok=True)
    ref.write_text("[Old](memory/cards/old.md)")
    assert not observed_run(tmp_path, today=TODAY)["transitions"]
    assert path.exists()


@pytest.mark.parametrize("source", ["read", "interactive"])
def test_observed_use_protects(tmp_path, source):
    card(tmp_path)
    if source == "read":
        reads.record(tmp_path, ["old"], today=TODAY)
    elif source == "interactive":
        path = tmp_path / ".brigade/work/claude-hooks/recall/session.json"
        path.parent.mkdir(parents=True)
        days = {"old": [TODAY.isoformat()]}
        auth = reads.authenticate(tmp_path, path.relative_to(tmp_path).as_posix(), days)
        path.write_text(
            json.dumps({"v": 1, "opened": ["old"], "interactive_open_days": days, "interactive_open_auth": auth})
        )
    assert not observed_run(tmp_path, today=TODAY)["transitions"]


def test_policy_windows_off_switch_dry_run_and_rollback(tmp_path):
    path = card(tmp_path)
    cfg = tmp_path / ".brigade/memory-care.toml"
    cfg.write_text("[lifecycle]\nstale_after_days = 300\narchive_after_days = 400\n")
    rows = observed_run(tmp_path, today=TODAY, dry_run=True)["transitions"]
    assert [r["to_state"] for r in rows] == ["stale"]
    assert not (tmp_path / journal.JOURNAL_REL).exists()
    rows = observed_run(tmp_path, today=TODAY)["transitions"]
    assert path.exists()
    tend.undo(tmp_path, rows[0]["decision_id"])
    cfg.write_text("[lifecycle]\nenabled = false\n")
    assert not observed_run(tmp_path, today=TODAY)["transitions"]


def test_new_history_and_new_card_are_protected(tmp_path):
    path = card(tmp_path)
    os.utime(path, None)
    assert not observed_run(tmp_path, today=TODAY)["transitions"]


def test_blast_cap(tmp_path):
    card(tmp_path)
    (tmp_path / ".brigade/memory-care.toml").write_text("[lifecycle]\nmax_transitions_per_run = 1\n")
    assert [r["to_state"] for r in observed_run(tmp_path, today=TODAY)["transitions"]] == ["stale"]
    assert [r["to_state"] for r in observed_run(tmp_path, today=TODAY)["transitions"]] == ["archived"]


def test_retrieval_fixture_unchanged_with_archived_decoy(tmp_path):
    baseline = run_eval(adapters=["current"])["adapters"]["current"]
    shutil.copytree(DEFAULT_FIXTURE_ROOT, tmp_path, dirs_exist_ok=True)
    archive = tmp_path / "memory/archive/cards"
    archive.mkdir(parents=True)
    (archive / "decoy.md").write_text("---\ntitle: Restic backup schedule\n---\nrestic snapshot prune backup schedule")
    assert run_eval(fixture_root=tmp_path, adapters=["current"])["adapters"]["current"] == baseline


def test_stale_reactivated_by_use(tmp_path):
    card(tmp_path)
    rows = observed_run(tmp_path, today=date(2026, 2, 1))["transitions"]
    assert [r["to_state"] for r in rows] == ["stale"]
    reads.record(tmp_path, ["old"], today=date(2026, 2, 2))
    rows = observed_run(tmp_path, today=date(2026, 2, 2))["transitions"]
    assert [r["to_state"] for r in rows] == ["active"]


def test_unavailable_hub_protects(tmp_path, monkeypatch):
    path = card(tmp_path)
    monkeypatch.setattr(reads, "hub_last_reads", lambda *a, **k: None)
    assert observed_run(tmp_path, today=TODAY)["blocked"]
    assert path.exists()


@pytest.mark.parametrize("days,state", [(29, None), (30, "stale"), (89, "stale"), (90, "archived")])
def test_window_boundaries(tmp_path, days, state):
    from datetime import timedelta

    card(tmp_path)
    rows = observed_run(tmp_path, today=date(2026, 1, 1) + timedelta(days=days))["transitions"]
    assert (rows[-1]["to_state"] if rows else None) == state


def test_invalid_search_data_does_not_count_as_use(tmp_path):
    path = card(tmp_path)
    log = tmp_path / ".brigade/memory/search-log.jsonl"
    log.write_text('{"card_ids": ["old"]}\n')
    assert observed_run(tmp_path, today=TODAY)["transitions"][-1]["to_state"] == "archived"
    assert not path.exists()


def test_expired_human_or_referenced_cards_are_protected(tmp_path):
    from brigade import memory_care_retire

    path = card(tmp_path, meta="trust: human\nexpires: 2026-01-01\n")
    assert not memory_care_retire.plan(tmp_path, memory_cmd.MemoryCareConfig(), tend.TendConfig(), today=TODAY)[
        "candidates"
    ]
    assert path.exists()


def test_cli_archive_flag(tmp_path, capsys):
    from brigade.cli import main

    card(tmp_path)
    observed_run(tmp_path, today=TODAY)
    assert main(["memory", "search", "retrievalwidget", "--target", str(tmp_path), "--include-archived", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["matches"][0]["path"].startswith("memory/archive/")


def test_changed_card_refused_before_archive(tmp_path, monkeypatch):
    from brigade import memory_archive

    path = card(tmp_path)
    real = memory_archive.archive_card

    def changed(*args, **kwargs):
        path.write_text("---\ntrust: human\n---\nNew human text\n")
        return real(*args, **kwargs)

    monkeypatch.setattr(memory_archive, "archive_card", changed)
    payload = observed_run(tmp_path, today=TODAY)
    assert any("changed since" in row["reason"] for row in payload["skipped"])
    assert [row["to_state"] for row in payload["transitions"]] == ["stale"]
    assert "New human text" in path.read_text()
    assert not (tmp_path / "memory/archive/cards/old.md").exists()


def test_occupied_archive_skips_only_that_card(tmp_path):
    first = card(tmp_path, name="occupiedwidget")
    second = card(tmp_path, name="freewidget")
    held = tmp_path / "memory/archive/cards/occupiedwidget.md"
    held.parent.mkdir(parents=True, exist_ok=True)
    held.write_bytes(b"earlier archive\n")
    for _ in range(2):
        payload = observed_run(tmp_path, today=TODAY)
        assert any(
            row["path"] == "memory/cards/occupiedwidget.md" and "occupied" in row["reason"]
            for row in payload["skipped"]
        )
    assert first.exists()
    assert held.read_bytes() == b"earlier archive\n"
    assert not second.exists()
    assert (tmp_path / "memory/archive/cards/freewidget.md").exists()


def test_daily_schedule_installs_lifecycle(tmp_path):
    from brigade import care_cmd

    care_cmd._ensure_memory_care_runbooks(tmp_path)
    path = tmp_path / ".brigade/memory-care/runbooks/daily-care-pass.json"
    assert any("tend --stage lifecycle" in step["run"] for step in json.loads(path.read_text())["steps"])


def test_evidence_retraction_does_not_block_care(tmp_path):
    card(tmp_path)
    log = tmp_path / ".brigade/memory/reflect/evidence-log.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text(json.dumps({"retract_sha256": "a" * 64}) + "\n")
    assert observed_run(tmp_path, today=TODAY)["transitions"][-1]["to_state"] == "archived"


def test_unread_workspace_starts_observation_window(tmp_path):
    card(tmp_path)
    (tmp_path / reads.READS_REL).unlink()
    assert not life.run(tmp_path, today=date(2026, 1, 1))["transitions"]
    assert reads.load(tmp_path).tracking_since == date(2026, 1, 1)
    assert observed_run(tmp_path, today=date(2026, 4, 1))["transitions"][-1]["to_state"] == "archived"


def test_reference_scan_does_not_block_concurrent_reader(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    card(tmp_path)
    (tmp_path / "AGENTS.md").write_text("Unrelated instructions")
    coverage(tmp_path, date(2026, 1, 1), TODAY)
    original = life._mentioned
    observed = []

    def slow_scan(*args, **kwargs):
        if not observed:
            # Wait for a real reader while scanning. If the ledger is locked,
            # this lasts longer than the reader's five-second lock deadline.
            with ThreadPoolExecutor(max_workers=1) as pool:
                observed.append(pool.submit(reads.record, tmp_path, ["old"], today=TODAY).result(timeout=10))
        return original(*args, **kwargs)

    monkeypatch.setattr(life, "_mentioned", slow_scan)
    result = life.run(tmp_path, today=TODAY)
    assert observed == [True]
    assert reads.covered(reads.load(tmp_path), today=TODAY, days=90)
    assert not result["transitions"]


def test_source_read_during_reference_scan_protects_dependency(tmp_path, monkeypatch):
    path = card(tmp_path)
    card(tmp_path, name="source", body="[dependency](old.md)")
    coverage(tmp_path, date(2026, 1, 1), TODAY)
    original = life._mentioned

    def scanned(*args, **kwargs):
        assert reads.record(tmp_path, ["source"], today=TODAY)
        return original(*args, **kwargs)

    monkeypatch.setattr(life, "_mentioned", scanned)
    result = life.run(tmp_path, today=TODAY)
    assert path.exists()
    assert {"path": "memory/cards/old.md", "reason": "referenced"} in result["skipped"]
    assert not result["transitions"]


def test_unused_cycle_parses_once_and_does_not_protect(tmp_path, monkeypatch):
    first = card(tmp_path, name="cyclefirst", body="[Second](cyclesecond.md)")
    second = card(tmp_path, name="cyclesecond", body="[First](cyclefirst.md)")
    keys, targets = [], []
    original_keys, original_targets = life._keys, life._resolved_targets

    def counted_keys(text, rel):
        keys.append(rel)
        return original_keys(text, rel)

    def counted_targets(text, referring):
        targets.append(referring)
        return original_targets(text, referring)

    monkeypatch.setattr(life, "_keys", counted_keys)
    monkeypatch.setattr(life, "_resolved_targets", counted_targets)
    result = observed_run(tmp_path, today=TODAY)
    assert sorted(keys) == ["memory/cards/cyclefirst.md", "memory/cards/cyclesecond.md"]
    assert len(targets) == len(set(targets))
    assert not first.exists(), result
    assert not second.exists(), result


def test_unused_pinned_source_still_protects_dependency(tmp_path):
    path = card(tmp_path)
    source = card(tmp_path, name="pinned", body="memory/cards/old.md")
    source.rename(tmp_path / "memory/pinned.md")
    result = observed_run(tmp_path, today=TODAY)
    assert path.exists()
    assert {"path": "memory/cards/old.md", "reason": "referenced"} in result["skipped"]


@pytest.mark.parametrize("kind,reason,actor", [("archive", "unused-card", "policy"), ("fix", "repair", "operator")])
def test_scheduled_care_cannot_finish_workspace_prepared_archive(tmp_path, monkeypatch, kind, reason, actor):
    from brigade import memory_care_journal as journal, memory_care_tend as tend

    path = card(tmp_path)
    original = path.read_bytes()
    saved = []

    def interrupted(target, decision_id):
        saved.append(decision_id)
        raise RuntimeError("interrupted prepare")

    with monkeypatch.context() as patch:
        patch.setattr(journal, "finish_prepared", interrupted)
        with pytest.raises(RuntimeError, match="interrupted prepare"):
            journal.commit_phased(
                tmp_path,
                kind=kind,
                reason=reason,
                card_id="old",
                changes=[journal.Change("memory/cards/old.md", original, None)],
                prepared_paths=[],
                hub={},
                metadata={"actor": actor},
            )
    decision_id = saved[0]
    before = journal._payload(tmp_path, decision_id)
    monkeypatch.setattr(life, "AUTOMATIC_ARCHIVE_ENABLED", False)
    result = tend.run(tmp_path, stages=["lifecycle"], today=TODAY)
    assert path.read_bytes() == original
    assert journal._payload(tmp_path, decision_id) == before
    assert result["recovery"] == [
        {
            "decision_id": decision_id,
            "phase": "operator_required",
            "reason": "removal or move outside automatic recovery allowlist",
        }
    ]
    journal.resolve_prepared(tmp_path)
    assert not path.exists()


@pytest.mark.parametrize("operation", ["payload", "update"])
def test_committed_archive_with_unreadable_recovery_reports_id(tmp_path, monkeypatch, operation):
    from brigade import memory_archive, memory_care_journal as journal, memory_care_tend as tend

    path = card(tmp_path)
    coverage(tmp_path, date(2026, 1, 1), TODAY)
    commit = journal.commit_phased
    calls = []

    def committed(*args, **kwargs):
        decision = commit(*args, **kwargs)
        if kwargs["kind"] == "archive":
            calls.append(decision.decision_id)

            def failed(*args, **kwargs):
                raise OSError("stored decision unavailable")

            monkeypatch.setattr(journal, "load", failed)
            monkeypatch.setattr(journal, "_payload" if operation == "payload" else "update_hub", failed)
        return decision

    monkeypatch.setattr(journal, "commit_phased", committed)
    care = tend.run(tmp_path, stages=["lifecycle"], today=TODAY)
    assert care["failed_stages"] == ["lifecycle"]
    result = care["stages"]["lifecycle"]
    assert len(calls) == 1
    assert not path.exists()
    assert result["error"] is True
    assert result["recovery_unverified"] == calls
    assert all(row["to_state"] != "archived" for row in result["transitions"])
    assert (tmp_path / memory_archive.archive_rel("memory/cards/old.md")).exists()


@pytest.mark.parametrize("source", ["runbooks/job.md", "memory/scoped/AGENTS.md", "scheduler:schtasks"])
@pytest.mark.parametrize("prefix", ["C:\\repo\\", "C:/repo/", "\\\\server\\share\\"])
@pytest.mark.parametrize(
    "name,protects",
    [("memory/cards/old.md", True), ("othermemory/cards/old.md", False), ("memory/cards/old.md.extra", False)],
)
def test_native_windows_absolute_references(tmp_path, monkeypatch, source, prefix, name, protects):
    path = card(tmp_path)
    coverage(tmp_path, date(2026, 1, 1), TODAY)
    reference = prefix + (name.replace("/", "\\") if "\\" in prefix else name)
    monkeypatch.setattr(life, "_references", lambda *args: {source: reference})
    result = life.run(tmp_path, today=TODAY)
    assert path.exists() == protects, result


@pytest.mark.parametrize("kind", ["lifecycle", "fix", "instructions", "undo"])
def test_prepared_non_archive_recovers_with_archive_gate_off(tmp_path, monkeypatch, kind):
    from brigade import memory_care_journal as journal, memory_care_tend as tend

    rel = ".brigade/memory/lifecycle/example.json"
    saved = []

    def interrupted(target, decision_id):
        saved.append(decision_id)
        raise RuntimeError("interrupted prepare")

    with monkeypatch.context() as patch:
        patch.setattr(journal, "finish_prepared", interrupted)
        with pytest.raises(RuntimeError, match="interrupted prepare"):
            journal.commit_phased(
                tmp_path,
                kind=kind,
                reason="test recovery",
                card_id=None,
                changes=[journal.Change(rel, None, b'{"state":"stale"}\n')],
                prepared_paths=[],
                hub={},
            )
    monkeypatch.setattr(life, "AUTOMATIC_ARCHIVE_ENABLED", False)
    result = tend.run(tmp_path, stages=["lifecycle"], today=TODAY)
    assert (tmp_path / rel).read_bytes() == b'{"state":"stale"}\n'
    # A claimed undo kind without an undoes link has only replacement effects.
    phase = "committed"
    assert result["recovery"] == [{"decision_id": saved[0], "phase": phase, "reason": ""}]
    assert journal._payload(tmp_path, saved[0])["phase"] == phase


def test_custom_root_config_cannot_authorize_prepared_move(tmp_path, monkeypatch):
    path = tmp_path / "custom/cards/old.md"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"card\n")
    config = tmp_path / ".brigade/memory-care.toml"
    config.parent.mkdir(parents=True)
    config.write_text('card_roots = ["custom/cards"]\n')
    saved = []

    def interrupted(target, decision_id):
        saved.append(decision_id)
        raise RuntimeError("interrupted prepare")

    with monkeypatch.context() as patch:
        patch.setattr(journal, "finish_prepared", interrupted)
        with pytest.raises(RuntimeError, match="interrupted prepare"):
            journal.commit_phased(
                tmp_path,
                kind="fix",
                reason="repair",
                card_id="old",
                hub={},
                changes=[
                    journal.Change("custom/cards/old.md", b"card\n", None),
                    journal.Change("custom/elsewhere/old.md", None, b"card\n"),
                ],
                prepared_paths=[],
                metadata={"actor": "operator"},
            )
    config.write_text('card_roots = ["memory/cards"]\n')
    monkeypatch.setattr(life, "AUTOMATIC_ARCHIVE_ENABLED", False)
    result = tend.run(tmp_path, stages=[])
    assert path.exists()
    assert journal._payload(tmp_path, saved[0])["phase"] == "prepared"
    assert result["recovery"][0]["decision_id"] == saved[0]
    assert result["recovery"][0]["phase"] == "operator_required"


@pytest.mark.parametrize("command", ["manifest", "undo"])
def test_unrelated_commands_do_not_finish_prepared_archive(tmp_path, monkeypatch, command):
    path = card(tmp_path)
    other = journal.commit_phased(
        tmp_path,
        kind="fix",
        reason="repair",
        card_id=None,
        hub={},
        changes=[journal.Change("other.md", None, b"other")],
        prepared_paths=[],
    )
    saved = []

    def interrupted(target, decision_id):
        saved.append(decision_id)
        raise RuntimeError("interrupted prepare")

    with monkeypatch.context() as patch:
        patch.setattr(journal, "finish_prepared", interrupted)
        with pytest.raises(RuntimeError, match="interrupted prepare"):
            journal.commit_phased(
                tmp_path,
                kind="archive",
                reason="operator",
                card_id="old",
                hub={},
                changes=[journal.Change("memory/cards/old.md", path.read_bytes(), None)],
                prepared_paths=[],
            )
    if command == "manifest":
        manifest = tmp_path / "empty.jsonl"
        manifest.write_text("")
        tend.archive_manifest(tmp_path, manifest)
    else:
        tend.undo(tmp_path, other.decision_id)
    assert path.exists()
    assert journal._payload(tmp_path, saved[0])["phase"] == "prepared"


@pytest.mark.parametrize(
    "reference",
    [
        r"C:\Repo\MEMORY\CARDS\OLD.MD",
        r"C:\Repo\MEMORY\CARDS\OLD.MD#section",
        r"C:\Repo\MEMORY\CARDS\OLD.MD?raw=1#section",
        r"\\Server\Share\memory\cards\old.md#section",
        r"\\Server\Share\MEMORY\CARDS\OLD.MD?raw=1#section",
    ],
)
def test_windows_case_and_fragment_references(reference):
    assert life._mentioned(reference, "memory/cards/old.md", "old")


def test_windows_schedule_timeout_blocks_without_failing_tend(tmp_path, monkeypatch, capsys):
    import argparse
    import subprocess
    import sys

    path = card(tmp_path)
    coverage(tmp_path, date(2026, 1, 1), TODAY)
    original = subprocess.run

    def timeout(command, *args, **kwargs):
        if command[0] == "schtasks":
            raise subprocess.TimeoutExpired(command, 15)
        return original(command, *args, **kwargs)

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "run", timeout)
    monkeypatch.setattr(reads, "record", lambda *args, **kwargs: True)
    monkeypatch.setattr(life.receipt_signing, "workspace_key", lambda target: bytes.fromhex("12" * 32))
    args = argparse.Namespace(
        memory_care_command="tend", target=tmp_path, stage=["lifecycle", "retire"], dry_run=False, json=True
    )
    monkeypatch.setattr(memory_cmd, "_today", lambda: TODAY)
    assert tend.dispatch(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["stages"]["lifecycle"]["blocked"] == "installed care schedules are unreadable"
    assert result["stages"]["lifecycle"]["transitions"] == []
    assert not result.get("failed_stages")
    assert path.exists()


@pytest.mark.parametrize("state", ["null", "[]"])
def test_nonobject_state_keeps_later_stages_and_latest(tmp_path, monkeypatch, state):
    path = card(tmp_path)
    coverage(tmp_path, date(2026, 1, 1), TODAY)
    state_path = tmp_path / (
        ".brigade/memory/lifecycle/" + life.hashlib.sha256(b"memory/cards/old.md").hexdigest() + ".json"
    )
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(state)
    later = []
    monkeypatch.setattr(tend.memory_care_retire, "apply", lambda *a, **kw: later.append(True) or {})
    result = tend.run(tmp_path, stages=["lifecycle", "retire"], today=TODAY)
    assert result["failed_stages"] == ["lifecycle"]
    assert later == [True]
    assert json.loads((tmp_path / tend.STATE_REL).read_text()) == result
    assert path.exists()


def test_card_lock_timeout_preserves_prior_transitions(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from brigade import inbox_lock

    card(tmp_path, name="a")
    card(tmp_path, name="b")
    card(tmp_path, name="c")
    coverage(tmp_path, date(2026, 1, 1), TODAY)
    original = inbox_lock.held_file_lock
    calls = []

    @contextmanager
    def lock(path, **kwargs):
        if kwargs.get("deadline_seconds") == 5:
            calls.append(path)
            if len(calls) == 2:
                raise inbox_lock.InboxLockTimeout("busy")
        with original(path, **kwargs):
            yield

    monkeypatch.setattr(reads, "record", lambda *args, **kwargs: True)
    monkeypatch.setattr(life, "_references", lambda *args: {})
    monkeypatch.setattr(inbox_lock, "held_file_lock", lock)
    monkeypatch.setattr(life, "AUTOMATIC_ARCHIVE_ENABLED", False)
    result = tend.run(tmp_path, stages=["lifecycle"], today=TODAY)
    stage = result["stages"]["lifecycle"]
    assert [row["card_path"] for row in stage["transitions"]] == ["memory/cards/a.md", "memory/cards/c.md"]
    assert all(row["decision_id"] for row in stage["transitions"])
    assert {"path": "memory/cards/b.md", "reason": "local read ledger busy"} in stage["skipped"]
    assert not result.get("failed_stages")


def test_quoted_windows_path_with_spaces():
    assert life._mentioned(r'"C:\Example Workspace\ws\memory\cards\old.md#section"', "memory/cards/old.md", "old")


@pytest.mark.parametrize("state", ["null", "[]", "{broken"])
def test_round14_bad_state_skips_only_card(tmp_path, monkeypatch, state):
    monkeypatch.setattr(reads, "record", lambda *args, **kwargs: True)
    monkeypatch.setattr(life, "_references", lambda *args: {})
    for name in ("a", "b", "c"):
        card(tmp_path, name=name)
    coverage(tmp_path, date(2026, 1, 1), TODAY)
    state_path = tmp_path / (
        ".brigade/memory/lifecycle/" + life.hashlib.sha256(b"memory/cards/b.md").hexdigest() + ".json"
    )
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(state)
    monkeypatch.setattr(life, "AUTOMATIC_ARCHIVE_ENABLED", False)
    result = tend.run(tmp_path, stages=["lifecycle"], today=TODAY)
    stage = result["stages"]["lifecycle"]
    assert [r["card_path"] for r in stage["transitions"]] == ["memory/cards/a.md", "memory/cards/c.md"]
    assert all(r["decision_id"] for r in stage["transitions"])
    assert {"path": "memory/cards/b.md", "reason": "invalid lifecycle state"} in stage["skipped"]
    assert json.loads((tmp_path / tend.STATE_REL).read_text()) == result


def test_round14_overlap_keeps_later_stages(tmp_path, monkeypatch):
    monkeypatch.setattr(reads, "record", lambda *args, **kwargs: True)
    monkeypatch.setattr(life, "_references", lambda *args: {})
    card(tmp_path)
    coverage(tmp_path, date(2026, 1, 1), TODAY)
    saved = []
    execute = journal.kernel.execute

    def crash(plan, *, target, **kwargs):
        saved.append(plan.operation_id)
        execute(plan, target=target, inject=journal.kernel.FailureInjector(crash_after=0))
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(journal.kernel, "execute", crash)
        with pytest.raises(KeyboardInterrupt):
            life.run(tmp_path, today=TODAY)
    stages = ["lifecycle", "retire", "inbox", "instructions"]
    result = tend.run(tmp_path, stages=stages, today=TODAY)
    assert set(result["stages"]) == set(stages)
    skipped = result["stages"]["lifecycle"]["skipped"]
    assert any(saved[0] in r["reason"] for r in skipped), result
    decision_id = saved[0].removeprefix("care-").removesuffix("-prepare")
    assert any(r["decision_id"] == decision_id for r in result["recovery"])
    assert f"brigade memory care recover {decision_id} --target ." in result["operator_recovery"]
    assert json.loads((tmp_path / tend.STATE_REL).read_text()) == result
