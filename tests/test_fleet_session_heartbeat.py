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


def test_disabled_history_does_not_starve_first_live_renewal(setup, monkeypatch):
    target, snapshot, _, sent, observe = setup
    history = [replace(snapshot, session_id=f"ended-{index}") for index in range(200)]
    for ended in history:
        _invalidate(ended)
    names = [state.record_name(s.harness, s.session_id) for s in [*history, snapshot]]
    monkeypatch.setattr(heartbeat.dirfd, "child_names", lambda fd, limit: names[:limit])
    report = observe(publish=True)
    assert report["observed"] == report["live"] == report["published"] == 1
    assert report["ok"] and not report["truncated"]
    assert sent == [snapshot]
    with anchored_io.session_directory(target) as anchor:
        assert all(not (target / ".brigade/work/claude-hooks/sessions" / name).exists() for name in names[:-1])
        assert state.read_record(anchor, names[-1])["enabled"] is True
    assert str(target) not in json.dumps(report) and snapshot.session_id not in json.dumps(report)
    assert all(isinstance(value, (bool, int)) for key, value in report.items() if key != "mode")


def test_expired_unowned_records_are_pruned_but_live_and_recent_records_survive(setup):
    target, snapshot, processes, sent, observe = setup
    old = replace(snapshot, session_id="old-dead")
    recent = replace(snapshot, session_id="recent-dead")
    disabled_live = replace(snapshot, session_id="disabled-live")
    for saved in (old, recent, disabled_live):
        _remember(saved)
        name = state.record_name(saved.harness, saved.session_id)
        record = anchored_io.read_json(target, name)
        if saved == disabled_live:
            record["enabled"] = False
        else:
            record["process"]["start"] = "456"
        anchored_io.write_json(target, name, record)
    directory = target / ".brigade/work/claude-hooks/sessions"
    for saved in (snapshot, old, disabled_live):
        os.utime(directory / state.record_name(saved.harness, saved.session_id), (1, 1))
    report = observe(publish=True)
    assert report["live"] == 1 and sent == [snapshot]
    assert not (directory / state.record_name(old.harness, old.session_id)).exists()
    for saved in (snapshot, recent, disabled_live):
        assert (directory / state.record_name(saved.harness, saved.session_id)).exists()
    assert processes.alive(processes.rows[42].proof(), "claude")


def test_newer_enabled_session_precedes_old_enabled_history(setup, monkeypatch):
    target, snapshot, _, sent, observe = setup
    older = []
    for index in range(3):
        saved = replace(snapshot, session_id=f"old-{index}")
        _remember(saved)
        name = state.record_name(saved.harness, saved.session_id)
        record = anchored_io.read_json(target, name)
        record["process"]["start"] = "456"
        anchored_io.write_json(target, name, record)
        os.utime(target / ".brigade/work/claude-hooks/sessions" / name, (1, 1))
        older.append(name)
    name = state.record_name(snapshot.harness, snapshot.session_id)
    monkeypatch.setattr(heartbeat.dirfd, "child_names", lambda fd, limit: [*older, name][:limit])
    monkeypatch.setattr(heartbeat, "SESSION_LIMIT", 1)
    report = observe(publish=True)
    assert report["observed"] == report["published"] == 1
    assert report["truncated"] and sent == [snapshot]


def test_expired_record_waits_for_ttl_and_obsolete_grace(setup, monkeypatch):
    target, snapshot, _, _, observe = setup
    saved = replace(snapshot, session_id="expiry-boundary", ttl_seconds=120)
    _remember(saved)
    name = state.record_name(saved.harness, saved.session_id)
    record = anchored_io.read_json(target, name)
    record["process"]["start"] = "456"
    anchored_io.write_json(target, name, record)
    path = target / ".brigade/work/claude-hooks/sessions" / name
    now = 10000
    monkeypatch.setattr(heartbeat.time, "time", lambda: now)
    expiry = now - saved.ttl_seconds - state.OBSOLETE_GRACE_SECONDS
    os.utime(path, (expiry + 1, expiry + 1))
    assert observe()["live"] == 1 and path.exists()
    os.utime(path, (expiry - 1, expiry - 1))
    assert observe()["live"] == 1 and not path.exists()


def test_inflight_hook_cannot_be_pruned(setup):
    target, snapshot, _, _, observe = setup
    newer = replace(snapshot, branch="newer-branch")
    name = state.record_name(snapshot.harness, snapshot.session_id)

    def send():
        assert anchored_io.read_json(target, name)["enabled"] is False
        assert observe()["published"] == 0
        assert anchored_io.read_json(target, name)["snapshot"]["branch"] == newer.branch
        return True

    assert state.publish_tracked(newer, send, successful=bool)
    assert observe(publish=True)["published"] == 1


def test_pruning_rereads_record_under_publication_lock(setup, monkeypatch):
    from contextlib import contextmanager

    target, snapshot, _, sent, observe = setup
    _invalidate(snapshot)
    newer = replace(snapshot, branch="newer-branch")
    original = anchored_io.SessionDirectory.held_lock
    raced = []

    @contextmanager
    def racing_lock(anchor, name, *, deadline_seconds):
        if not raced:
            raced.append(True)
            _remember(newer)
        with original(anchor, name, deadline_seconds=deadline_seconds):
            yield

    monkeypatch.setattr(anchored_io.SessionDirectory, "held_lock", racing_lock)
    assert observe(publish=True)["published"] == 1
    assert sent == [newer]
    assert anchored_io.read_json(target, state.record_name(snapshot.harness, snapshot.session_id))["enabled"]


def test_prune_holds_same_lock_as_hook_publication(setup, monkeypatch):
    import threading

    target, snapshot, _, _, observe = setup
    _invalidate(snapshot)
    original = heartbeat.dirfd.unlink_child
    pruned = []

    def unlink(fd, name):
        if name.endswith(state.SUFFIX):
            blocked = []

            def hook():
                try:
                    _remember(snapshot)
                except OSError:
                    blocked.append(True)

            worker = threading.Thread(target=hook)
            worker.start()
            worker.join(3)
            assert not worker.is_alive() and blocked == [True]
            pruned.append(name)
        return original(fd, name)

    monkeypatch.setattr(heartbeat.dirfd, "unlink_child", unlink)
    assert observe()["observed"] == 0
    assert pruned == [state.record_name(snapshot.harness, snapshot.session_id)]
    _remember(snapshot)
    assert observe(publish=True)["published"] == 1


def test_disabled_reads_remain_bounded_by_directory_cap_and_deadline(setup, monkeypatch):
    target, snapshot, _, _, observe = setup
    _invalidate(snapshot)
    name = state.record_name(snapshot.harness, snapshot.session_id)
    monkeypatch.setattr(heartbeat, "DIRECTORY_LIMIT", 2)
    monkeypatch.setattr(heartbeat.dirfd, "child_names", lambda fd, limit: [name] * limit)
    original = anchored_io.SessionDirectory.read_bytes
    reads = []

    def read(anchor, name, max_bytes=None):
        reads.append(max_bytes)
        return original(anchor, name, max_bytes)

    monkeypatch.setattr(anchored_io.SessionDirectory, "read_bytes", read)
    assert observe()["truncated"]
    assert len(reads) <= 2 and all(limit <= state.MAX_RECORD_BYTES + 1 for limit in reads)
    _invalidate(snapshot)
    clock = [100.0]
    monkeypatch.setattr(heartbeat.time, "monotonic", lambda: clock[0])

    def slow_read(anchor, name, max_bytes=None):
        clock[0] += heartbeat.OBSERVATION_BUDGET_SECONDS
        return original(anchor, name, max_bytes)

    monkeypatch.setattr(anchored_io.SessionDirectory, "read_bytes", slow_read)
    report = observe()
    assert report["error"] == "observation_timeout" and report["truncated"]
    assert (target / ".brigade/work/claude-hooks/sessions" / name).exists()


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


@pytest.fixture
def worktrees(setup, monkeypatch):
    from brigade import proc
    from brigade.config import Config, write_config
    from brigade.selection import Selection

    target, snapshot, processes, sent, _ = setup
    entries = []

    def add(name, *, wired=True, sidecar=True, mtime=1, flags=()):
        path = target / name
        path.mkdir()
        if wired:
            write_config(path, Config(version=1, selection=Selection(depth="repo", harnesses=("codex",))))
        if sidecar:
            saved = replace(snapshot, checkout_path=str(path), session_id=name, harness="codex")
            _remember(saved)
            record = path / ".brigade/work/claude-hooks/sessions" / state.record_name("codex", name)
            # Whole seconds survive Windows FILETIME's 100 ns resolution.
            stamp_ns = mtime * 1_000_000_000
            os.utime(record, ns=(stamp_ns, stamp_ns))
            with anchored_io.session_directory(path) as anchor:
                with anchor.open_file(record.name, os.O_RDONLY) as fd:
                    assert os.fstat(fd).st_mtime_ns == stamp_ns
        entries.append("\0".join([f"worktree {path}", "HEAD abcdef", *flags]) + "\0\0")
        return path

    monkeypatch.setattr(heartbeat.proc, "run", lambda *a, **k: proc.Result(0, "".join(entries), ""))
    processes.rows[42] = replace(processes.rows[42], executable="codex", arguments=("codex",))
    return target, add, entries, processes, sent


@pytest.mark.parametrize("timestamp_resolution_ns", [1, 100], ids=["nanoseconds", "windows-filetime"])
def test_worktree_discovery_selects_only_wired_present_targets_newest_first(
    worktrees, monkeypatch, timestamp_resolution_ns
):
    real_utime = os.utime

    def quantized_utime(path, *, ns):
        real_utime(path, ns=tuple(value // timestamp_resolution_ns * timestamp_resolution_ns for value in ns))

    monkeypatch.setattr(os, "utime", quantized_utime)
    target, add, entries, processes, sent = worktrees
    oldest = add("oldest", mtime=10)
    newest = add("newest", mtime=30)
    middle = add("middle", mtime=20, flags=("locked maintenance",))
    add("unwired", wired=False, mtime=100)
    add("empty", sidecar=False)
    add("prunable", flags=("prunable missing metadata",))
    add("bare", flags=("bare",))
    entries.append(f"worktree {target / 'missing'}\0locked unavailable\0\0")
    report = heartbeat.observe(
        [],
        worktrees_of=[target],
        publish=True,
        processes=processes,
        publisher=lambda s: sent.append(s) or fleet_client.SessionWriteResult(True),
    )
    assert report["published"] == report["discovered_targets"] == 3
    assert [s.checkout_path for s in sent] == [str(newest), str(middle), str(oldest)]
    assert report["discovery_errors"] == 0
    assert str(target) not in json.dumps(report)


@pytest.mark.parametrize("newest_mtime", [20, 30], ids=["equal-mtimes", "newest-sidecar"])
def test_discovery_uses_newest_sidecar_and_stable_path_ties(worktrees, newest_mtime):
    import time

    target, add, entries, _, _ = worktrees
    first = add("first", mtime=10)
    second = add("second", mtime=20)
    # Git enumeration and directory mtimes both favor the other checkout.
    entries.reverse()
    first_sessions = first / ".brigade/work/claude-hooks/sessions"
    extra = first_sessions / ("newer" + state.SUFFIX)
    extra.write_text("{}")
    stamp_ns = newest_mtime * 1_000_000_000
    os.utime(extra, ns=(stamp_ns, stamp_ns))
    second_sessions = second / ".brigade/work/claude-hooks/sessions"
    for path, stamp in ((first, 100), (second, 200), (first_sessions, 100), (second_sessions, 200)):
        os.utime(path, ns=(stamp * 1_000_000_000, stamp * 1_000_000_000))

    discovered, errors, truncated = heartbeat._discover_worktrees(
        [target], time.monotonic() + heartbeat.OBSERVATION_BUDGET_SECONDS
    )

    assert discovered == [first, second]
    assert errors == 0 and not truncated


def test_discovery_combines_explicit_targets_and_deduplicates_before_cap(worktrees, monkeypatch):
    target, add, _, processes, sent = worktrees
    explicit = add("explicit", mtime=1)
    newest = add("newest", mtime=30)
    add("older", mtime=20)
    monkeypatch.setattr(heartbeat, "TARGET_LIMIT", 2)
    report = heartbeat.observe(
        [explicit, explicit],
        worktrees_of=[target, target],
        publish=True,
        processes=processes,
        publisher=lambda s: sent.append(s) or fleet_client.SessionWriteResult(True),
    )
    assert [s.checkout_path for s in sent] == [str(explicit), str(newest)]
    assert report["published"] == 2
    assert report["truncated"]


def test_worktree_entry_limit_is_shared_across_repositories(worktrees, monkeypatch):
    target, add, _, processes, sent = worktrees
    first = add("first", mtime=10)
    second = add("second", mtime=20)
    add("beyond-limit", mtime=100)
    monkeypatch.setattr(heartbeat, "WORKTREE_ENTRY_LIMIT", 2)
    report = heartbeat.observe(
        [],
        worktrees_of=[target, target / "first"],
        publish=True,
        processes=processes,
        publisher=lambda s: sent.append(s) or fleet_client.SessionWriteResult(True),
    )
    assert [s.checkout_path for s in sent] == [str(second), str(first)]
    assert report["truncated"]


@pytest.mark.parametrize("code", [1, 124])
def test_discovery_git_failure_is_counted_without_leaking_details(worktrees, monkeypatch, code):
    from brigade import proc

    target, add, _, processes, sent = worktrees
    add("ignored")
    monkeypatch.setattr(
        heartbeat.proc, "run", lambda *a, **k: proc.Result(code, f"worktree {target}\0\0", "private git failure")
    )
    report = heartbeat.observe([], worktrees_of=[target], processes=processes)
    assert report["discovery_errors"] == 1
    assert report["discovered_targets"] == report["observed"] == 0
    assert not report["ok"]
    assert sent == []
    assert "private" not in json.dumps(report) and str(target) not in json.dumps(report)


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink fixture")
@pytest.mark.parametrize("component", [".brigade", "work", "claude-hooks", "sessions", "record"])
def test_discovery_never_follows_sidecar_symlinks(worktrees, component):
    target, add, _, processes, _ = worktrees
    candidate = add("linked")
    path = candidate / ".brigade/work/claude-hooks/sessions" / state.record_name("codex", "linked")
    if component != "record":
        path = next(parent for parent in path.parents if parent.name == component)
    outside = target / "outside-state"
    path.rename(outside)
    path.symlink_to(outside, target_is_directory=component != "record")
    report = heartbeat.observe([], worktrees_of=[target], processes=processes)
    assert report["discovered_targets"] == report["observed"] == 0


def test_discovery_and_observation_share_one_deadline(worktrees, monkeypatch):
    from brigade import proc

    target, add, entries, processes, sent = worktrees
    add("session")
    clock = [100.0]
    monkeypatch.setattr(heartbeat.time, "monotonic", lambda: clock[0])

    def slow_git(*args, **kwargs):
        assert 0 < kwargs["timeout"] <= heartbeat.WORKTREE_TIMEOUT_SECONDS
        clock[0] += heartbeat.OBSERVATION_BUDGET_SECONDS
        return proc.Result(0, "".join(entries), "")

    monkeypatch.setattr(heartbeat.proc, "run", slow_git)
    report = heartbeat.observe([], worktrees_of=[target], processes=processes)
    assert report["error"] == "observation_timeout"
    assert report["truncated"] and report["observed"] == 0
    assert sent == []


def test_main_accepts_worktree_discovery_without_explicit_target(worktrees, monkeypatch, capsys):
    target, add, _, processes, _ = worktrees
    add("session")
    monkeypatch.setattr(heartbeat, "ProcessTable", lambda: processes)
    assert heartbeat.main(["--worktrees-of", str(target)]) == 0
    assert json.loads(capsys.readouterr().out)["live"] == 1


def test_discovery_reads_real_git_worktree_porcelain(setup):
    from brigade import proc
    from brigade.config import Config, write_config
    from brigade.selection import Selection

    target, snapshot, processes, _, _ = setup
    linked = target.parent / (target.name + " linked checkout")
    for command in (
        ["git", "init", "-q", str(target)],
        [
            "git",
            "-C",
            str(target),
            "-c",
            "user.name=Example",
            "-c",
            "user.email=example@example.com",
            "commit",
            "--allow-empty",
            "-qm",
            "initial",
        ],
        ["git", "-C", str(target), "worktree", "add", "--detach", str(linked), "HEAD"],
    ):
        result = proc.run(command, timeout=5)
        assert result.code == 0, result.stderr
    write_config(linked, Config(version=1, selection=Selection(depth="repo", harnesses=("claude",))))
    _remember(replace(snapshot, checkout_path=str(linked), session_id="linked-example"))
    report = heartbeat.observe([], worktrees_of=[target], processes=processes)
    assert report["discovered_targets"] == report["live"] == 2
    assert report["ok"]


@pytest.mark.parametrize("failure", ["overflow", "decode", "launch"])
def test_discovery_unusable_git_output_is_counted(worktrees, monkeypatch, failure):
    from brigade import proc

    target, add, entries, processes, _ = worktrees
    add("session")

    def failed(*args, **kwargs):
        if failure == "launch":
            raise OSError("private command failure")
        return proc.Result(
            0,
            "".join(entries),
            "",
            output_limit_exceeded=failure == "overflow",
            stdout_decode_error="invalid bytes" if failure == "decode" else None,
        )

    monkeypatch.setattr(heartbeat.proc, "run", failed)
    report = heartbeat.observe([], worktrees_of=[target], processes=processes)
    assert report["discovery_errors"] == 1
    assert report["discovered_targets"] == report["observed"] == 0
    assert not report["ok"]


@pytest.mark.skipif(os.name != "posix", reason="Linux procfs fixture requires POSIX symlinks")
@pytest.mark.parametrize("native_layout", [True, False])
def test_versioned_claude_owner_requires_verified_native_installation(tmp_path, monkeypatch, native_layout):
    from pathlib import Path

    monkeypatch.setattr("brigade.fleet_session_process.sys.platform", "linux")
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    root = tmp_path / "proc"
    process = root / "42"
    process.mkdir(parents=True)
    boot = root / "sys/kernel/random"
    boot.mkdir(parents=True)
    (boot / "boot_id").write_text("fake-boot")
    (process / "stat").write_text("42 (claude) " + " ".join(["S", "1", *(["0"] * 17), "123"]))
    (process / "cmdline").write_bytes(b"claude\0")
    installation = Path.home() / ".local/share/claude/versions" if native_layout else tmp_path / "other/versions"
    installation.mkdir(parents=True)
    binary = installation / "2.1.39"
    binary.write_bytes(b"example executable")
    binary.chmod(0o755)
    (process / "exe").symlink_to(binary)
    monkeypatch.setattr(os, "getpid", lambda: 42)
    table = ProcessTable(root)
    proof = {"pid": 42, "start": "123", "boot": "fake-boot"}
    assert table.alive(proof, "claude") is native_layout
    assert table.owner("claude") == (proof if native_layout else None)
    binary.chmod(0o777)
    assert not table.alive(proof, "claude")


def test_legacy_attribution_records_are_not_renewed(setup):
    target, snapshot, _, sent, observe = setup
    name = state.record_name(snapshot.harness, snapshot.session_id)
    record = anchored_io.read_json(target, name)
    record["version"] = 1
    anchored_io.write_json(target, name, record)
    assert observe(publish=True)["published"] == 0
    assert sent == []
    # The next correctly attributed hook replaces the legacy sidecar.
    _remember(snapshot)
    assert observe(publish=True)["published"] == 1


def test_secondary_legacy_checkout_does_not_survive_primary_root_refresh(setup):
    from brigade.config import Config, write_config
    from brigade.selection import Selection

    target, snapshot, processes, sent, _ = setup
    secondary = target / "other"
    write_config(secondary, Config(version=1, selection=Selection(depth="repo", harnesses=("claude",))))
    wrong = replace(snapshot, checkout_path=str(secondary), repo_identity="github.com/example/other")
    _remember(wrong)
    name = state.record_name(wrong.harness, wrong.session_id)
    record = anchored_io.read_json(secondary, name)
    record["version"] = 1
    anchored_io.write_json(secondary, name, record)
    report = heartbeat.observe(
        [secondary, target],
        processes=processes,
        publish=True,
        publisher=lambda s: sent.append(s) or fleet_client.SessionWriteResult(ok=True),
    )
    assert report["published"] == 1
    assert sent == [snapshot]
