"""Session renewal uses only saved presence and a matching live process."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, replace

import pytest

from brigade import fleet_client, fleet_session_heartbeat as heartbeat
from brigade import fleet_session_heartbeat_state as state
from brigade import fleet_session_presence as presence
from brigade.claude_hooks import anchored_io
from brigade.fleet_session_process import ProcessIdentity, ProcessTable


def _remember(snapshot):
    return state.publish_tracked(snapshot, lambda: True, successful=bool)


def _invalidate(snapshot):
    return state.publish_tracked(snapshot, lambda: True, successful=bool, ended=True)


class FakeProcesses(ProcessTable):
    def __init__(self):
        self.rows = {42: ProcessIdentity(42, 1, "123", "fake-boot", "claude", ("claude",))}

    def read(self, pid):
        return self.rows.get(pid)

    def owner(self, harness):
        return self.rows[42].proof()


@pytest.fixture
def setup(tmp_path, monkeypatch):
    from brigade.config import Config, write_config
    from brigade.selection import Selection

    write_config(tmp_path, Config(version=1, selection=Selection(depth="repo", harnesses=("claude", "codex"))))
    monkeypatch.setattr(heartbeat, "_hub_url_configured", lambda: True)
    monkeypatch.setattr(heartbeat, "_require_node_auth", lambda: None)
    processes = FakeProcesses()
    monkeypatch.setattr(state, "ProcessTable", lambda: processes)
    snapshot = presence.SessionSnapshot(
        harness="claude",
        session_id="session-example",
        repo_identity="github.com/example/project",
        identity_scope="fleet",
        repo_label="project",
        checkout_path=str(tmp_path),
        branch="feature/example",
        dirty_paths=("src/example.py",),
        dirty_truncated=False,
    )
    _remember(snapshot)
    sent = []

    def send(value):
        sent.append(value)
        return fleet_client.SessionWriteResult(ok=True)

    def observe(**kwargs):
        return heartbeat.observe([tmp_path], processes=processes, publisher=send, **kwargs)

    return tmp_path, snapshot, processes, sent, observe


def test_live_session_reuses_exact_snapshot_even_if_checkout_changes(setup):
    target, snapshot, _, sent, observe = setup
    (target / "new-file").write_text("later work")
    report = observe(publish=True)
    assert report["published"] == report["live"] == 1
    assert report["ok"]
    assert sent == [snapshot]
    assert asdict(sent[0]) == asdict(snapshot)
    assert "session-example" not in json.dumps(report)
    assert str(target) not in json.dumps(report)


@pytest.mark.parametrize(
    "reason", ["dead", "reused", "reboot", "missing_pid", "opted_out", "unwired", "ended", "wrong_binary"]
)
def test_unproven_or_disabled_sessions_are_skipped(setup, reason):
    target, snapshot, processes, sent, observe = setup
    name = state.record_name(snapshot.harness, snapshot.session_id)
    record = anchored_io.read_json(target, name)
    if reason == "dead":
        processes.rows.clear()
    elif reason == "reused":
        processes.rows[42] = replace(processes.rows[42], start="456")
    elif reason == "reboot":
        processes.rows[42] = replace(processes.rows[42], boot="another-boot")
    elif reason == "missing_pid":
        record["process"].pop("pid")
        anchored_io.write_json(target, name, record)
    elif reason == "opted_out":
        record["enabled"] = False
        anchored_io.write_json(target, name, record)
    elif reason == "unwired":
        (target / ".brigade/config.json").unlink()
    elif reason == "ended":
        _invalidate(snapshot)
    else:
        processes.rows[42] = replace(processes.rows[42], executable="unrelated")
    report = observe(publish=True)
    assert report["published"] == 0
    assert sent == []


def test_inspect_never_publishes(setup):
    _, _, _, sent, observe = setup
    assert observe()["live"] == 1
    assert sent == []


def test_cap_bounds_attempts_and_observations(setup, monkeypatch):
    _, snapshot, _, sent, observe = setup
    for index in range(5):
        _remember(replace(snapshot, session_id=f"example-{index}"))
    monkeypatch.setattr(heartbeat, "PUBLISH_LIMIT", 2)
    report = observe(publish=True)
    assert len(sent) == report["published"] == 2
    assert report["truncated"] and not report["ok"]
    sent.clear()
    monkeypatch.setattr(heartbeat, "SESSION_LIMIT", 1)
    report = observe(publish=True)
    assert report["observed"] == len(sent) == 1
    assert report["truncated"]


def test_budget_reserves_entire_network_call(setup, monkeypatch):
    _, _, _, sent, observe = setup
    # Enough to read local metadata, too little for a two-second publication.
    monkeypatch.setattr(heartbeat, "OBSERVATION_BUDGET_SECONDS", 1)
    report = observe(publish=True)
    assert sent == []
    assert report["error"] == "observation_timeout"
    assert report["truncated"]


def test_unreachable_hub_stops_at_first_failure_without_leaking_error(setup):
    target, snapshot, processes, _, _ = setup
    _remember(replace(snapshot, session_id="another-example"))
    calls = []

    def failed(value):
        calls.append(value)
        return fleet_client.SessionWriteResult(False, "private transport detail")

    report = heartbeat.observe([target], publish=True, processes=processes, publisher=failed)
    assert len(calls) == 1
    assert report["error"] == "publication_failed"
    assert "private" not in json.dumps(report)


def test_codex_interactive_session_is_renewed_but_app_server_is_not(setup):
    _, snapshot, processes, sent, observe = setup
    _invalidate(snapshot)
    codex = replace(snapshot, harness="codex")
    processes.rows[42] = replace(processes.rows[42], executable="codex", arguments=("codex",))
    _remember(codex)
    assert observe(publish=True)["published"] == 1
    assert sent == [codex]
    sent.clear()
    processes.rows[42] = replace(processes.rows[42], arguments=("codex", "app-server"))
    assert observe(publish=True)["published"] == 0
    assert sent == []


def test_hook_tracks_only_successful_publication_and_end_invalidates(setup, monkeypatch):
    target, snapshot, _, sent, observe = setup
    monkeypatch.setattr(presence, "_hub_url_configured", lambda: True)
    monkeypatch.setattr(presence, "build_snapshot", lambda *a, **k: snapshot)
    _invalidate(snapshot)
    monkeypatch.setattr(fleet_client, "upsert_session", lambda s: False)
    assert presence._publish_presence("PostToolUse", target, snapshot.session_id) == "unpublished"
    assert observe(publish=True)["published"] == 0
    monkeypatch.setattr(fleet_client, "upsert_session", lambda s: True)
    assert presence._publish_presence("PostToolUse", target, snapshot.session_id) is None
    assert observe(publish=True)["published"] == 1
    monkeypatch.setattr(fleet_client, "end_session", lambda s: False)
    assert presence._publish_presence("SessionEnd", target, snapshot.session_id) == "end_unpublished"
    sent.clear()
    assert observe(publish=True)["published"] == 0
    assert sent == []


def test_codex_brief_tracks_the_payload_it_published(setup, monkeypatch):
    from brigade.work_cmd.session.briefing import _interactive_presence_for_brief

    target, snapshot, processes, _, observe = setup
    _invalidate(snapshot)
    processes.rows[42] = replace(processes.rows[42], executable="codex", arguments=("codex",))
    codex = replace(snapshot, harness="codex")
    monkeypatch.setenv("CODEX_THREAD_ID", codex.session_id)
    monkeypatch.setattr(presence, "_hub_url_configured", lambda: True)
    monkeypatch.setattr(presence, "build_snapshot", lambda *a, **k: codex)
    monkeypatch.setattr(fleet_client, "publish_session", lambda s: fleet_client.SessionWriteResult(True))
    monkeypatch.setattr(fleet_client, "fetch_sessions", lambda: [])
    assert _interactive_presence_for_brief(target)["interactive_sessions"]["published"]
    assert observe(publish=True)["published"] == 1


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink fixture, platform fallback is tested separately")
def test_symlink_record_and_oversized_record_are_not_followed(setup):
    target, snapshot, _, sent, observe = setup
    name = state.record_name(snapshot.harness, snapshot.session_id)
    path = target / ".brigade/work/claude-hooks/sessions" / name
    path.write_bytes(b"x" * (state.MAX_RECORD_BYTES + 1))
    assert observe(publish=True)["published"] == 0
    path.unlink()
    outside = target / "outside"
    outside.write_text("private")
    path.symlink_to(outside)
    assert observe(publish=True)["published"] == 0
    assert outside.read_text() == "private"
    assert sent == []


def test_main_fixed_json_and_auth_failure(monkeypatch, capsys):
    def denied():
        raise heartbeat.ObservationError("node_auth_required")

    monkeypatch.setattr(heartbeat, "_require_node_auth", denied)
    assert heartbeat.main(["--target", "/example/workspace", "--publish"]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "node_auth_required"


@pytest.mark.skipif(os.name != "posix", reason="Linux procfs fixture requires POSIX ownership and symlinks")
def test_process_reader_proves_start_and_rejects_zombies(tmp_path, monkeypatch):
    monkeypatch.setattr("brigade.fleet_session_process.sys.platform", "linux")
    root = tmp_path / "proc"
    process = root / "42"
    process.mkdir(parents=True)
    boot = root / "sys/kernel/random"
    boot.mkdir(parents=True)
    (boot / "boot_id").write_text("fake-boot")
    fields = ["S", "1"] + ["0"] * 17 + ["123"]
    (process / "stat").write_text("42 (name with ) spaces) " + " ".join(fields))
    (process / "cmdline").write_bytes(b"claude\0")
    (process / "exe").symlink_to("/example/bin/claude")
    table = ProcessTable(root)
    proof = {"pid": 42, "start": "123", "boot": "fake-boot"}
    assert table.alive(proof, "claude")
    assert not table.alive({**proof, "start": "456"}, "claude")
    (process / "stat").write_text("42 (claude) " + " ".join(["Z", *fields[1:]]))
    assert not table.alive(proof, "claude")


def test_ancestor_discovery_skips_hook_workers_and_fails_closed(monkeypatch):
    table = FakeProcesses()
    worker = ProcessIdentity(80, 42, "999", "fake-boot", "python", ("python",))
    table.rows[80] = worker
    monkeypatch.setattr(os, "getpid", lambda: 80)
    assert ProcessTable.owner(table, "claude") == table.rows[42].proof()
    table.rows[42] = replace(table.rows[42], executable="service")
    assert ProcessTable.owner(table, "claude") is None


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_unsupported_platform_never_proves_liveness(monkeypatch, platform):
    monkeypatch.setattr("brigade.fleet_session_process.sys.platform", platform)
    assert ProcessTable().read(42) is None
    assert ProcessTable().owner("claude") is None


def test_publish_disables_old_record_before_sending_new_payload(setup):
    target, snapshot, _, _, observe = setup
    newer = replace(snapshot, branch="new-branch", dirty_paths=("src/new.py",))
    name = state.record_name(snapshot.harness, snapshot.session_id)

    def send():
        assert anchored_io.read_json(target, name)["enabled"] is False
        return True

    assert state.publish_tracked(newer, send, successful=bool)
    assert anchored_io.read_json(target, name)["snapshot"]["branch"] == "new-branch"
    assert observe(publish=True)["published"] == 1


def test_failed_new_publication_disables_previous_snapshot(setup):
    _, snapshot, _, sent, observe = setup
    newer = replace(snapshot, branch="new-branch")
    assert not state.publish_tracked(newer, lambda: False, successful=bool)
    assert observe(publish=True)["published"] == 0
    assert sent == []


def test_session_end_serializes_after_inflight_heartbeat(setup):
    import threading

    target, snapshot, processes, _, observe = setup
    publishing = threading.Event()
    release = threading.Event()
    ending = threading.Event()
    order = []
    results = []

    def send(value):
        publishing.set()
        assert release.wait(3)
        order.append("heartbeat")
        return fleet_client.SessionWriteResult(True)

    def end():
        ending.set()
        state.publish_tracked(snapshot, lambda: order.append("end") or True, successful=bool, ended=True)

    worker = threading.Thread(
        target=lambda: results.append(heartbeat.observe([target], publish=True, processes=processes, publisher=send))
    )
    worker.start()
    assert publishing.wait(3)
    closer = threading.Thread(target=end)
    closer.start()
    assert ending.wait(3)
    release.set()
    worker.join(3)
    closer.join(3)
    assert not worker.is_alive() and not closer.is_alive()
    assert results[0]["published"] == 1
    assert order == ["heartbeat", "end"]
    assert observe(publish=True)["published"] == 0


def test_codex_noninteractive_child_does_not_bind_outer_interactive_session(monkeypatch):
    table = FakeProcesses()
    table.rows[42] = replace(table.rows[42], executable="codex", arguments=("codex",))
    table.rows[80] = ProcessIdentity(80, 42, "456", "fake-boot", "codex", ("codex", "exec"))
    monkeypatch.setattr(os, "getpid", lambda: 80)
    assert ProcessTable.owner(table, "codex") is None
