"""Bounded reader regressions using the writer's serialized records."""

import json
from dataclasses import replace

from brigade import agent_identity, observed_checks as oc
from tests._home import set_home


def seed(tmp_path, monkeypatch):
    set_home(monkeypatch, tmp_path / "home")
    check = oc.build_check(
        target=tmp_path,
        command="pytest -q",
        status="ok",
        identity=agent_identity.AgentIdentity("claude-code", "hook-event", "claude-test", "hook-event"),
        session_fingerprint="session",
        fingerprint_tree=False,
    )
    assert oc.append_check(tmp_path, check)
    return check, oc.digest_key_path(tmp_path).with_name(oc.CHECKS_FILE)


def test_oversized_tail_skips_partial_line_and_keeps_newest(tmp_path, monkeypatch):
    check, path = seed(tmp_path, monkeypatch)
    line = (json.dumps(check.to_dict(), ensure_ascii=True, allow_nan=False) + "\n").encode()
    path.write_bytes(line * 3000)
    monkeypatch.setattr(oc, "MAX_FILE_BYTES", len(line) * 12 + 9)
    checks, skipped = oc.read_checks(tmp_path)
    assert checks == [check] * 12
    assert skipped == 1


def test_validation_runs_once_per_record(tmp_path, monkeypatch):
    check, path = seed(tmp_path, monkeypatch)
    original = oc.ObservedCheck.from_dict
    calls = []

    def validate(data):
        calls.append(data)
        return original(data)

    monkeypatch.setattr(oc.ObservedCheck, "from_dict", validate)
    assert oc.read_checks(tmp_path) == ([check], 0)
    assert len(calls) == 1


def test_snapshot_reads_two_generations_newest_first_without_writes(tmp_path, monkeypatch):
    check, path = seed(tmp_path, monkeypatch)
    old = replace(check, status="failed")
    path.with_name(path.name + ".1").write_text(json.dumps(old.to_dict()) + "\n")
    path.with_name(path.name + ".2").write_text("ignored\n")
    path.with_name(path.name + ".lock").unlink()

    def no_lock(*args, **kwargs):
        raise AssertionError("read-only snapshot must not create or acquire a writable lock")

    monkeypatch.setattr(oc, "held_file_lock", no_lock)
    assert oc.read_checks(tmp_path, snapshot=True) == ([check, old], 0)
    assert not path.with_name(path.name + ".lock").exists()


def test_record_cap_uses_active_then_old_generation(tmp_path, monkeypatch):
    check, path = seed(tmp_path, monkeypatch)
    line = json.dumps(check.to_dict()) + "\n"
    path.write_text(line * 3)
    path.with_name(path.name + ".1").write_text(line * 3)
    monkeypatch.setattr(oc, "MAX_RECORDS", 4)
    checks, skipped = oc.read_checks(tmp_path)
    assert checks == [check] * 4 and skipped == 0
