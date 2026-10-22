"""Round-two regressions for recording isolation and retention boundaries."""

from __future__ import annotations

import hashlib
import json
import os
import time

import pytest

from brigade import agents, proc, run_journal, run_shadow, runguard
from brigade import run_cassette_store as store
from brigade.roster import Agent, Roster
from brigade.run_seat import SeatInvoker
from tests.test_run_cassette import _documents, _record
from tests.test_run_cassette import auto_recording as auto_recording


def test_publication_works_without_posix_link_primitive(tmp_path, monkeypatch):
    def unavailable(*args, **kwargs):
        raise OSError("hard-link operations are unavailable on Windows")

    monkeypatch.setattr(store.dirfd, "link_child", unavailable)
    run = tmp_path / "run"
    data = store.encode({"privacy_class": "private", "result": "answer"})
    digest = hashlib.sha256(data).hexdigest()
    with store.directory(run, create=True) as parent:
        assert store.publish(parent, data) == digest
        assert store.publish(parent, data) == digest
        assert store.read(parent, f"{digest}.json") == data
        target = run / "events" / "cassettes" / f"{digest}.json"
        target.write_bytes(b"collision")
        with pytest.raises(store.CassetteError, match="collision"):
            store.publish(parent, data)
        assert target.read_bytes() == b"collision"
    assert not list(target.parent.glob(".cassette-*.tmp"))


@pytest.mark.parametrize("secret", ["text", "ext"])
def test_secret_colliding_with_structural_key_stops_before_body_publication(tmp_path, monkeypatch, secret, capsys):
    monkeypatch.setenv("CASSETTE_TEST_TOKEN", secret)
    run = tmp_path / ".brigade" / "runs" / "current"
    agent = Agent(name="worker", cli="codex", role="write", env={"API_KEY_REF": "CASSETTE_TEST_TOKEN"})
    assert _record(run, agent=agent) is None
    assert (run / "events" / "cassettes" / ".stopped").exists()
    assert all(d["privacy_class"] == "public-metadata" and d["status"] == "stopped" for d in _documents(run))
    assert capsys.readouterr().err == "cassette recording stopped: storage, classification, or byte cap\n"


@pytest.mark.parametrize("leftover", ["temp", "invalid-json", "hardlink"])
def test_retention_skips_crash_debris_and_keeps_recording(tmp_path, leftover):
    old = tmp_path / ".brigade" / "runs" / "old"
    _record(old)
    (old / "run.json").write_text(json.dumps({"run_id": old.name}))
    debris = old / "events" / "cassettes" / (".cassette-abcd.tmp" if leftover == "temp" else "unsafe.json")
    if leftover == "hardlink":
        outside = tmp_path / "outside"
        outside.write_text(' {"privacy_class":"private"}')
        os.link(outside, debris)
    else:
        debris.write_bytes(b"partial json")
    debris.chmod(0o600)
    modified = time.time() - 40 * 86400
    os.utime(old, (modified, modified))
    current = old.parent / "current"
    assert _record(current) is not None
    assert _record(current, task="next worker") is not None
    assert not (current / "events" / "cassettes" / ".stopped").exists()
    # Inspect only valid content-addressed bodies, leaving debris untouched.
    bodies = [json.loads(p.read_bytes()) for p in debris.parent.glob("[0-9a-f]" * 64 + ".json")]
    assert not any(d["privacy_class"] == "private" for d in bodies)
    assert debris.exists()


def test_raw_appserver_stream_can_exceed_cassette_entry_cap(tmp_path):
    run = tmp_path / ".brigade" / "runs" / "current"
    events = run / "events"
    events.mkdir(parents=True)
    row = {"method": "item/completed", "params": {"item": {"id": "i", "type": "agentMessage", "text": "x" * 300_000}}}
    raw = ((json.dumps(row) + "\n") * 18).encode()
    assert len(raw) > store.MAX_ENTRY_BYTES
    (events / "worker.jsonl").write_bytes(raw)
    assert _record(run, result=agents.AgentResult(text="answer", ok=True, transport="codex-app-server")) is not None
    metadata = next(d for d in _documents(run) if d["privacy_class"] == "scrubbed")
    assert metadata["notification_projection"]["event_count"] == 18
    assert metadata["notification_projection"]["source_digest"] == hashlib.sha256(raw).hexdigest()
    assert _record(run, task="later worker") is not None
    assert not (events / "cassettes" / ".stopped").exists()


def test_request_key_uses_configured_transport_before_dispatch(tmp_path, monkeypatch):
    observed = ["codex-app-server", "exec", None, "codex-app-server"]
    monkeypatch.setattr(
        agents, "run_agent", lambda *a, **kw: agents.AgentResult(text="answer", ok=True, transport=observed.pop(0))
    )
    keys = []
    transports = []
    for i, transport in enumerate(("app-server", "app-server", "app-server", "exec")):
        run = tmp_path / ".brigade" / "runs" / f"run-{i}"
        invoker = SeatInvoker(
            roster=Roster(
                orchestrator="chef",
                agents={"worker": Agent(name="worker", cli="codex", role="write")},
                codex_transport=transport,
            ),
            cwd=tmp_path,
            run_id=run.name,
            process_registry=proc.ProcessRegistry(),
            output_dir=run,
        )
        assert invoker.invoke(seat="worker", phase="research.plan", prompt="task", timeout=10).ok
        manifest = next(d for d in _documents(run) if d["privacy_class"] == "public-metadata")
        keys.append(manifest["key"])
        metadata = next(d for d in _documents(run) if d["privacy_class"] == "scrubbed")
        transports.append(metadata.get("observed_transport"))
    assert keys[0] == keys[1] == keys[2]
    assert keys[0] != keys[3]
    assert transports == ["codex-app-server", "exec", "direct", "codex-app-server"]


def test_retention_streams_large_outcome_ledger_and_preserves_pins(tmp_path):
    old = tmp_path / ".brigade" / "runs" / "old"
    _record(old)
    (old / "run.json").write_text(json.dumps({"run_id": old.name}))
    modified = time.time() - 40 * 86400
    os.utime(old, (modified, modified))
    ledger = tmp_path / "memory" / "outcome" / "records.jsonl"
    ledger.parent.mkdir(parents=True)
    with ledger.open("w") as handle:
        handle.write("\n")
        for _ in range(45):
            handle.write(json.dumps({"context": "x" * 200_000}) + "\n")
    assert ledger.stat().st_size > 8 * 1024 * 1024
    assert store.retain_private(tmp_path, old.parent, current=old.parent / "new") == 1
    _record(old)
    with ledger.open("a") as handle:
        handle.write(json.dumps({"evidence_ref": str(old / "run.json")}) + "\n")
    os.utime(old, (modified, modified))
    assert store.retain_private(tmp_path, old.parent, current=old.parent / "new") == 0
    assert any(d["privacy_class"] == "private" for d in _documents(old))


@pytest.mark.parametrize("reason", ["invalid-ledger", "too-many-runs"])
def test_retention_reports_skipped_pruning(tmp_path, monkeypatch, capsys, reason):
    root = tmp_path / ".brigade" / "runs"
    root.mkdir(parents=True)
    if reason == "invalid-ledger":
        ledger = tmp_path / "memory" / "outcome" / "records.jsonl"
        ledger.parent.mkdir(parents=True)
        ledger.write_text("42\n")
    else:
        monkeypatch.setattr(store.dirfd, "child_names", lambda parent, limit: [f"run-{i}" for i in range(10001)])
    assert store.retain_private(tmp_path, root, current=root / "current") == 0
    assert "cassette retention skipped:" in capsys.readouterr().err


@pytest.mark.parametrize("failure", [False, True])
def test_retention_runs_once_and_never_stops_recording(tmp_path, monkeypatch, capsys, failure):
    calls = []
    retain = store.retain_private

    def retention(*args, **kwargs):
        calls.append(1)
        if failure:
            raise OSError("sensitive-path-needle")
        return retain(*args, **kwargs)

    monkeypatch.setattr(store, "retain_private", retention)
    run = tmp_path / ".brigade" / "runs" / "current"
    assert _record(run) is not None
    assert _record(run, task="later worker") is not None
    assert calls == [1]
    assert not (run / "events" / "cassettes" / ".stopped").exists()
    error = capsys.readouterr().err
    assert "sensitive-path-needle" not in error
    assert ("cassette retention skipped:" in error) == failure


@pytest.mark.parametrize(
    "contents",
    [
        '{"run":',
        '{"version":1,"depth":"repo","harnesses":["codex"],"run":{"record":"Off"}}',
    ],
)
def test_unreadable_recording_policy_creates_no_cassette(tmp_path, contents):
    cfg = tmp_path / ".brigade" / "config.json"
    cfg.parent.mkdir(exist_ok=True)
    cfg.write_text(contents)
    run = cfg.parent / "runs" / "current"
    assert _record(run) is None
    assert not (run / "events").exists()


def test_unexpected_recorder_exception_cannot_lose_completed_seat_result(tmp_path, monkeypatch):
    monkeypatch.setattr(agents, "run_agent", lambda *a, **kw: agents.AgentResult(text="completed answer", ok=True))

    def broken_shadow(*args, **kwargs):
        raise KeyError("shadow diagnostic needle")

    monkeypatch.setattr(run_shadow, "record_shadow_comparison", broken_shadow)
    run = tmp_path / ".brigade" / "runs" / "current"
    run_journal.ensure_journal(run / "events" / "lifecycle.jsonl")
    (run / "run.json").write_text(json.dumps({"status": "dispatching", "lifecycle_journal_requested": True}))
    invoker = SeatInvoker(
        roster=Roster(orchestrator="chef", agents={"worker": Agent(name="worker", cli="codex", role="write")}),
        cwd=tmp_path,
        run_id=run.name,
        process_registry=proc.ProcessRegistry(),
        output_dir=run,
    )
    with runguard.run_lock(tmp_path, run_dir=run):
        result = invoker.invoke(seat="worker", phase="research.plan", prompt="task", timeout=10)
    assert result.ok and result.text == "completed answer"
    assert (run / "events" / "cassettes" / ".stopped").exists()


def test_deleted_run_is_not_recreated_by_publication(tmp_path):
    from brigade import run_cassette

    run = tmp_path / ".brigade" / "runs" / "gone"
    run.parent.mkdir(parents=True)
    result = agents.AgentResult(text="private-worker-answer", ok=True, transport="cli")
    ref = run_cassette.record_result(
        run_dir=run,
        workspace=tmp_path,
        agent=Agent(name="worker", cli="codex", role="write", model="test-model"),
        task="t",
        prompt="p",
        result=result,
        read_only=True,
        sandbox="read-only",
    )
    assert ref is None
    assert not run.exists()
