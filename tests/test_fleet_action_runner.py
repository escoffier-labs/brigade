"""Fake executables prove argv, durable dispatch fences and transient job behavior."""

import json
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.test_fleet_actions import setup_action


requires_posix = pytest.mark.skipif(
    os.name != "posix", reason="Broker execution requires POSIX private files, process groups, and systemd"
)


def fake_executable(tmp_path, payload, *, name="fake-tool"):
    log = tmp_path / (name + ".log")
    executable = tmp_path / name
    executable.write_text(
        "#!" + sys.executable + "\nimport json,sys,os,stat\nfrom pathlib import Path\n"
        "args=sys.argv[1:]\nrecord={'args':args}\n"
        "for flag in ('--prompt-file','--message-file','--body-file'):\n"
        " if flag in args:\n"
        "  p=Path(args[args.index(flag)+1]);record['prompt']=p.read_text(encoding='utf-8');record['mode']=stat.S_IMODE(p.stat().st_mode)\n"
        "with open(" + repr(str(log)) + ",'a') as stream: stream.write(json.dumps(record)+'\\n')\n"
        "print(" + repr(json.dumps(payload)) + ")\n"
    )
    executable.chmod(0o700)
    return executable, log


def manifest(tmp_path, executable, target, *, binding=None):
    path = tmp_path / "manifest.json"
    public = {key: value for key, value in target.items() if key not in {"binding_digest", "node_id"}}
    path.write_text(
        json.dumps(
            {
                "schema": "brigade.fleet_action_manifest.v1",
                "journal_dir": str(tmp_path / "journal"),
                "executables": {
                    "openclaw": str(executable),
                    "t3": str(executable),
                    "brigade": str(executable),
                    "systemd-run": str(executable),
                    "systemctl": str(executable),
                },
                "targets": [{"public": public, "binding": binding or {"agent_id": "fixture-agent"}}],
            }
        )
    )
    path.chmod(0o600)
    return path


def broker_fixture(tmp_path, monkeypatch, *, payload=None):
    from brigade import fleet_action_runner as runner, fleet_client_actions as client, fleet_hub_actions as actions
    from brigade.fleet_action_http import handle_api

    conn, _, grant, request = setup_action(tmp_path)
    exe, log = fake_executable(tmp_path, payload or {"status": "ok", "runId": "fixture-turn"})
    target = json.loads(conn.execute("SELECT document FROM action_targets").fetchone()[0])
    path = manifest(tmp_path, exe, target)
    monkeypatch.setattr(client, "request", lambda body, **kwargs: handle_api(conn, (False, "node-fixture"), body))
    runner.sync_targets(path)
    action = actions.submit(conn, grant, request)
    return conn, path, log, action


@requires_posix
@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("t3", None),
        ("openclaw", False),
        ("brigade", 0),
        ("systemd-run", []),
        ("systemctl", {}),
        ("t3", ""),
        ("openclaw", "bad\x00path"),
        ("brigade", "bad\ud800path"),
    ],
)
def test_invalid_configured_executable_fails_before_transport(tmp_path, monkeypatch, key, value):
    from brigade import fleet_action_auth as auth, fleet_action_runner as runner, fleet_client_actions as client

    executable, log = fake_executable(tmp_path, {"status": "ok"})
    path = manifest(tmp_path, executable, {"target_id": "fixture-target", "family": "t3"})
    data = json.loads(path.read_text())
    data["targets"] = []
    data["executables"][key] = value
    path.write_text(json.dumps(data))
    monkeypatch.setattr(runner.shutil, "which", lambda _: str(executable))
    monkeypatch.setattr(client, "request", lambda *_args, **_kwargs: pytest.fail("invalid manifest reached Hub"))

    with pytest.raises(auth.ActionError, match="invalid-executable"):
        runner.sync_targets(path)

    assert not log.exists()
    assert not (tmp_path / "journal").exists()


@requires_posix
@pytest.mark.parametrize(
    "suffix",
    [
        "x\x00y",
        pytest.param(
            "\ud800",
            marks=pytest.mark.skipif(
                os.name != "posix", reason="filesystem encoding accepts this surrogate on Windows"
            ),
        ),
    ],
)
def test_invalid_journal_path_fails_before_target_sync(tmp_path, monkeypatch, suffix):
    from brigade import fleet_action_auth as auth, fleet_action_runner as runner, fleet_client_actions as client

    executable, _ = fake_executable(tmp_path, {"status": "ok"})
    path = manifest(tmp_path, executable, {"target_id": "fixture-target", "family": "t3"})
    data = json.loads(path.read_text())
    data["journal_dir"] = str(tmp_path / suffix)
    data["targets"] = []
    path.write_text(json.dumps(data))
    monkeypatch.setattr(client, "request", lambda *_args, **_kwargs: pytest.fail("invalid manifest reached Hub"))

    with pytest.raises(auth.ActionError, match="invalid-journal-path"):
        runner.sync_targets(path)

    assert not (tmp_path / "journal").exists()


@requires_posix
def test_manifest_accepts_filesystem_encodable_unicode_journal_path(tmp_path):
    from brigade import fleet_action_runner as runner

    executable, _ = fake_executable(tmp_path, {"status": "ok"})
    path = manifest(tmp_path, executable, {"target_id": "fixture-target", "family": "t3"})
    journal_dir = str(tmp_path / "journal-雪")
    data = json.loads(path.read_text())
    data["journal_dir"] = journal_dir
    data["targets"] = []
    path.write_text(json.dumps(data))

    assert runner.load_manifest(path)["journal_dir"] == journal_dir


@pytest.mark.skipif(os.name != "posix", reason="surrogateescape paths are POSIX filesystem paths")
def test_manifest_accepts_filesystem_surrogateescape_journal_path(tmp_path):
    from brigade import fleet_action_runner as runner

    executable, _ = fake_executable(tmp_path, {"status": "ok"})
    path = manifest(tmp_path, executable, {"target_id": "fixture-target", "family": "t3"})
    journal_dir = str(tmp_path / "journal-\udcff")
    os.fsencode(journal_dir)
    data = json.loads(path.read_text())
    data["journal_dir"] = journal_dir
    data["targets"] = []
    path.write_text(json.dumps(data))

    assert runner.load_manifest(path)["journal_dir"] == journal_dir


@requires_posix
def test_manifest_uses_path_default_only_for_omitted_executable(tmp_path, monkeypatch):
    from brigade import fleet_action_runner as runner

    executable, _ = fake_executable(tmp_path, {"status": "ok"})
    path = manifest(tmp_path, executable, {"target_id": "fixture-target", "family": "t3"})
    data = json.loads(path.read_text())
    data["targets"] = []
    configured = "path-\udcff"
    data["executables"] = {"t3": str(executable), "brigade": configured}
    path.write_text(json.dumps(data))
    monkeypatch.setattr(runner.shutil, "which", lambda name: f"/ambient/{name}")

    loaded = runner.load_manifest(path)

    assert loaded["executables"]["t3"] == str(executable)
    assert loaded["executables"]["brigade"] == configured
    assert loaded["executables"]["openclaw"] == "/ambient/openclaw"
    assert loaded["executables"]["systemctl"] == "/ambient/systemctl"

    monkeypatch.setattr(runner.shutil, "which", lambda _: None)
    loaded = runner.load_manifest(path)
    assert loaded["executables"]["systemctl"] == "systemctl"


@requires_posix
def test_execute_protected_prompt_and_replay_cannot_repeat_turn(tmp_path, monkeypatch):
    from brigade import fleet_action_runner as runner, fleet_hub_actions as actions

    conn, path, log, action = broker_fixture(tmp_path, monkeypatch)
    claimed = actions.claim(conn, "node-fixture")
    result = runner.execute(path, action["action_id"], claimed["generation"])
    assert result["status"] == "finished" and result["verification"] == "pending"
    record = json.loads(log.read_text().splitlines()[-1])
    assert record["mode"] == 0o600
    assert record["args"][:3] == ["agent", "--agent", "fixture-agent"]
    assert "--deliver" not in record["args"] and "--local" not in record["args"]
    assert "雪 <script>bad()</script>" in record["prompt"] and "Attached Hub evidence" in record["prompt"]
    runner.execute(path, action["action_id"], claimed["generation"])
    assert len(log.read_text().splitlines()) == 1
    assert stat.S_IMODE((tmp_path / "journal").stat().st_mode) == 0o700
    conn.close()


@requires_posix
@pytest.mark.parametrize("failure", ["create", "write", "flush", "file-fsync", "directory-fsync", "intent-write"])
def test_local_preparation_failure_is_recoverable_before_hub_start(tmp_path, monkeypatch, failure):
    from brigade import fleet_action_runner as runner, fleet_hub_actions as actions

    conn, path, log, action = broker_fixture(tmp_path, monkeypatch)
    claimed = actions.claim(conn, "node-fixture")
    action_id, generation = action["action_id"], claimed["generation"]
    prompt = tmp_path / "journal" / (action_id + ".prompt")
    journal = prompt.with_suffix(".jsonl")
    normal_open, normal_fdopen = runner.fleet_client._open_private, runner.os.fdopen
    normal_fsync, normal_write = runner.os.fsync, runner._write
    prompt_fds = set()

    def fail():
        raise OSError("fixture local preparation failure")

    def open_private(file, flags, **kwargs):
        if file == prompt and flags & os.O_WRONLY:
            if failure == "create":
                fail()
            fd = normal_open(file, flags, **kwargs)
            prompt_fds.add(fd)
            return fd
        return normal_open(file, flags, **kwargs)

    class PromptStream:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def write(self, text):
            if failure == "write":
                self.stream.write(text[:4])
                fail()
            return self.stream.write(text)

        def flush(self):
            if failure == "flush":
                fail()
            return self.stream.flush()

        def fileno(self):
            return self.stream.fileno()

    def fdopen(fd, *args, **kwargs):
        stream = normal_fdopen(fd, *args, **kwargs)
        if fd in prompt_fds:
            prompt_fds.remove(fd)
            return PromptStream(stream)
        return stream

    def fsync(fd):
        if prompt.exists():
            info = os.fstat(fd)
            if (failure == "file-fsync" and info.st_ino == prompt.stat().st_ino) or (
                failure == "directory-fsync" and stat.S_ISDIR(info.st_mode)
            ):
                fail()
        return normal_fsync(fd)

    def write(root, name, record, directory):
        if failure == "intent-write" and record.get("phase") == "starting":
            fail()
        return normal_write(root, name, record, directory)

    with monkeypatch.context() as patch:
        patch.setattr(runner.fleet_client, "_open_private", open_private)
        patch.setattr(runner.os, "fdopen", fdopen)
        patch.setattr(runner.os, "fsync", fsync)
        patch.setattr(runner, "_write", write)
        with pytest.raises(OSError, match="fixture local preparation failure"):
            runner.execute(path, action_id, generation)

    untouched = actions.show(conn, action_id)
    assert untouched["started_at"] is None
    assert untouched["status"] == "dispatching"
    assert not any(event["event_id"] == "started" for event in untouched["events"])
    assert not log.exists()
    assert not prompt.exists()
    assert not journal.exists() or json.loads(journal.read_text()).get("phase") not in {"starting", "started"}
    recovered = runner.execute(path, action_id, generation)
    assert recovered["status"] == "finished" and recovered["destination_ref"] == "fixture-turn"
    assert runner.execute(path, action_id, generation)["destination_ref"] == "fixture-turn"
    assert len(log.read_text().splitlines()) == 1
    conn.close()


@requires_posix
def test_missing_local_executable_is_recoverable_before_hub_start(tmp_path, monkeypatch):
    from brigade import fleet_action_auth as auth, fleet_action_runner as runner, fleet_hub_actions as actions

    conn, path, log, action = broker_fixture(tmp_path, monkeypatch)
    generation = actions.claim(conn, "node-fixture")["generation"]
    executable = Path(json.loads(path.read_text())["executables"]["openclaw"])
    unavailable = executable.with_suffix(".unavailable")
    executable.rename(unavailable)
    with pytest.raises(auth.ActionError, match="transient-executor-unavailable") as error:
        runner.execute(path, action["action_id"], generation)
    assert error.value.status == 503
    current = actions.show(conn, action["action_id"])
    assert current["started_at"] is None and current["status"] == "dispatching"
    assert not any(event["event_id"] == "started" for event in current["events"])
    assert not log.exists()
    assert not list((tmp_path / "journal").glob("*.prompt"))
    unavailable.rename(executable)
    assert runner.execute(path, action["action_id"], generation)["status"] == "finished"
    assert runner.execute(path, action["action_id"], generation)["destination_ref"] == "fixture-turn"
    assert len(log.read_text().splitlines()) == 1
    conn.close()


@requires_posix
@pytest.mark.parametrize("failure", [FileNotFoundError, PermissionError])
def test_prompt_cleanup_preserves_durable_receipt_and_report(tmp_path, monkeypatch, failure):
    from brigade import fleet_action_runner as runner, fleet_hub_actions as actions

    conn, path, log, action = broker_fixture(tmp_path, monkeypatch)
    claimed = actions.claim(conn, "node-fixture")
    action_id, generation = action["action_id"], claimed["generation"]
    prompt = tmp_path / "journal" / (action_id + ".prompt")
    journal = prompt.with_suffix(".jsonl")
    normal_unlink = runner.os.unlink
    cleanup_receipts = []

    def unlink(file, *, dir_fd=None):
        if file == prompt.name:
            record = json.loads(journal.read_text())
            assert record["phase"] == "result"
            assert record["receipt"]["destination_ref"] == "fixture-turn"
            cleanup_receipts.append(record["receipt"])
            if failure is FileNotFoundError:
                normal_unlink(file, dir_fd=dir_fd)
            raise failure("private prompt cleanup detail")
        return normal_unlink(file, dir_fd=dir_fd)

    monkeypatch.setattr(runner.os, "unlink", unlink)
    # The wrapper still forwards dir_fd, including for the platform capability check.
    monkeypatch.setattr(runner.os, "supports_dir_fd", runner.os.supports_dir_fd | {unlink})
    result = runner.execute(path, action_id, generation)
    assert result["status"] == "finished" and result["destination_ref"] == "fixture-turn"
    assert result["verification"] == "pending"
    saved = json.loads(journal.read_text())
    assert saved["phase"] == "reported" and saved["receipt"] == cleanup_receipts[0]
    event_id = saved["receipt"]["event_id"]
    assert sum(event["event_id"] == event_id for event in result["events"]) == 1
    replay = runner.execute(path, action_id, generation)
    assert replay["destination_ref"] == "fixture-turn"
    assert sum(event["event_id"] == event_id for event in replay["events"]) == 1
    assert json.loads(journal.read_text())["receipt"] == saved["receipt"]
    assert len(cleanup_receipts) == 1 and len(log.read_text().splitlines()) == 1
    assert "private prompt cleanup detail" not in json.dumps(result) + journal.read_text()
    if failure is PermissionError:
        assert stat.S_IMODE(prompt.stat().st_mode) == 0o600
        assert stat.S_IMODE(prompt.parent.stat().st_mode) == 0o700
    else:
        assert not prompt.exists()
    conn.close()


@requires_posix
@pytest.mark.parametrize("response", ["denied", "lost-before", "lost-after"])
def test_start_response_failure_cleans_prepared_prompt_without_resending(tmp_path, monkeypatch, response):
    from brigade import (
        fleet_action_auth as auth,
        fleet_action_runner as runner,
        fleet_client_actions as client,
        fleet_hub_actions as actions,
    )

    conn, path, log, action = broker_fixture(tmp_path, monkeypatch)
    claimed = actions.claim(conn, "node-fixture")
    action_id, generation = action["action_id"], claimed["generation"]
    prompt = tmp_path / "journal" / (action_id + ".prompt")
    journal = prompt.with_suffix(".jsonl")
    normal = client.request
    prepared = []
    if response == "denied":
        auth.revoke_operator(conn, action["grant_id"])

    def start(body, **kwargs):
        if body["op"] == "start":
            prepared.append(prompt.exists())
            if response == "denied":
                return normal(body, **kwargs)
            if response == "lost-after":
                normal(body, **kwargs)
            raise OSError("fixture lost start response")
        return normal(body, **kwargs)

    monkeypatch.setattr(client, "request", start)
    error, message = (auth.ActionError, "scope-revoked") if response == "denied" else (OSError, "lost start")
    with pytest.raises(error, match=message):
        runner.execute(path, action_id, generation)
    assert prepared == [True]
    assert not prompt.exists() and not log.exists()
    saved = json.loads(journal.read_text())
    assert saved["phase"] == "starting" and "receipt" not in saved
    current = actions.show(conn, action_id)
    assert (current["started_at"] is not None) == (response == "lost-after")
    replay = runner.execute(path, action_id, generation)
    assert prepared == [True] and not log.exists()
    if response == "lost-after":
        assert replay["status"] == "unknown"
        assert replay["destination_ref"] is None
        assert json.loads(journal.read_text())["receipt"]["status"] == "unknown"
    else:
        assert replay["started_at"] is None
        assert json.loads(journal.read_text())["phase"] == "awaiting-reclaim"
    conn.close()


@requires_posix
def test_dispatch_needs_no_poststart_preparation_journal_write(tmp_path, monkeypatch):
    from brigade import fleet_action_runner as runner, fleet_hub_actions as actions

    conn, path, log, action = broker_fixture(tmp_path, monkeypatch)
    generation = actions.claim(conn, "node-fixture")["generation"]
    normal = runner._write

    def write(root, name, record, directory):
        if actions.show(conn, action["action_id"])["started_at"] is not None and not log.exists():
            raise OSError("fixture post-start preparation write")
        return normal(root, name, record, directory)

    monkeypatch.setattr(runner, "_write", write)
    result = runner.execute(path, action["action_id"], generation)
    assert result["status"] == "finished" and result["destination_ref"] == "fixture-turn"
    runner.execute(path, action["action_id"], generation)
    assert len(log.read_text().splitlines()) == 1
    conn.close()


@requires_posix
def test_ambiguous_acceptance_is_unknown_and_never_redispatched(tmp_path, monkeypatch):
    from brigade import fleet_action_runner as runner, fleet_hub_actions as actions

    conn, path, log, action = broker_fixture(tmp_path, monkeypatch, payload={"unexpected": "no handle"})
    claimed = actions.claim(conn, "node-fixture")
    assert runner.execute(path, action["action_id"], claimed["generation"])["status"] == "unknown"

    def forbidden(*args, **kwargs):
        pytest.fail("handle-free unknown must not dispatch or recover")

    monkeypatch.setattr(runner.adapters, "dispatch", forbidden)
    monkeypatch.setattr(runner.adapters, "recover", forbidden)
    runner.execute(path, action["action_id"], claimed["generation"])
    assert len(log.read_text().splitlines()) == 1
    conn.close()


@requires_posix
def test_revoke_before_start_never_invokes_destination(tmp_path, monkeypatch):
    from brigade import fleet_action_auth as auth, fleet_action_runner as runner, fleet_hub_actions as actions

    conn, path, log, action = broker_fixture(tmp_path, monkeypatch)
    claimed = actions.claim(conn, "node-fixture")
    auth.revoke_operator(conn, action["grant_id"])
    with pytest.raises(auth.ActionError, match="scope-revoked"):
        runner.execute(path, action["action_id"], claimed["generation"])
    assert not log.exists()
    conn.close()


@requires_posix
def test_tick_schedules_one_unique_transient_job_and_no_inline_turn(tmp_path, monkeypatch):
    from brigade import fleet_action_runner as runner

    conn, path, log, action = broker_fixture(tmp_path, monkeypatch)
    first = runner.tick(path, budget_seconds=15, max_actions=1)
    runner.tick(path, budget_seconds=15, max_actions=1)
    records = [json.loads(line)["args"] for line in log.read_text().splitlines()]
    jobs = [args for args in records if any(arg.startswith("--unit=brigade-action-") for arg in args)]
    assert first["scheduled"] == 1 and len(jobs) == 1
    assert "--user" in jobs[0] and "execute" in jobs[0] and action["action_id"] in jobs[0]
    assert not any(args[:1] == ["agent"] for args in records)
    conn.close()


@requires_posix
@pytest.mark.parametrize(
    ("started_at", "scheduled"),
    [
        (1_699_913_601, 1),
        (1_699_913_600, 0),
        (1_699_913_599, 0),
        ("malformed", 0),
        (float("inf"), 0),
        (float("-inf"), 0),
        ("nan", 0),
        (True, 0),
    ],
)
@pytest.mark.parametrize("receipt_status", ["accepted", "running", "needs-input", "unknown"])
def test_reconcile_parks_started_action_at_fixed_recovery_lifetime(
    tmp_path, monkeypatch, started_at, scheduled, receipt_status
):
    from brigade import fleet_action_runner as runner, fleet_hub_actions as actions

    conn, path, log, action = broker_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(runner.time, "time", lambda: 1_700_000_000)
    claimed = actions.claim(conn, "node-fixture")
    actions.start(conn, "node-fixture", action["action_id"], claimed["generation"])
    with conn:
        conn.execute(
            "UPDATE agent_actions SET status='running', started_at=? WHERE action_id=?",
            (
                started_at,
                action["action_id"],
            ),
        )
    root = path.parent / "journal"
    with runner._journal_lock(root, action["action_id"]) as directory:
        runner._write(
            root,
            action["action_id"],
            {
                "generation": claimed["generation"],
                "phase": "reported",
                "receipt": {"status": receipt_status, "destination_ref": "fixture-ref"},
            },
            directory,
        )
    result = runner.tick(path, budget_seconds=15, max_actions=1)
    assert result["scheduled"] == scheduled
    with runner._journal_lock(root, action["action_id"]) as directory:
        record = runner._read(root, action["action_id"], directory)
    if scheduled:
        assert record["phase"] == "reported"
    else:
        assert record["phase"] == "parked"
    assert (
        conn.execute("SELECT status FROM agent_actions WHERE action_id=?", (action["action_id"],)).fetchone()[0]
        == "running"
    )
    jobs = [
        json.loads(line)["args"]
        for line in log.read_text().splitlines()
        if any(arg.startswith("--unit=brigade-action-") for arg in json.loads(line)["args"])
    ]
    assert len(jobs) == scheduled
    assert runner.tick(path, budget_seconds=15, max_actions=1)["scheduled"] == 0
    jobs = [
        json.loads(line)["args"]
        for line in log.read_text().splitlines()
        if any(arg.startswith("--unit=brigade-action-") for arg in json.loads(line)["args"])
    ]
    assert len(jobs) == scheduled
    if not scheduled:
        with runner._journal_lock(root, action["action_id"]) as directory:
            assert runner._read(root, action["action_id"], directory)["phase"] == "parked"
    conn.close()


@requires_posix
@pytest.mark.parametrize("delivery", ["none", "acknowledged"])
def test_inbox_unknown_noack_is_bounded_and_ack_fastpath_stays_parked(tmp_path, monkeypatch, delivery):
    from brigade import fleet_action_runner as runner, fleet_hub_actions as actions
    from tests.test_fleet_action_r2_core import inbox_action

    conn, path, _, action = inbox_action(tmp_path, monkeypatch)
    claimed = actions.claim(conn, "node-fixture")
    actions.start(conn, "node-fixture", action["action_id"], claimed["generation"])
    with conn:
        conn.execute(
            "UPDATE agent_actions SET status='unknown', started_at=? WHERE action_id=?",
            (1_699_913_600, action["action_id"]),
        )
    monkeypatch.setattr(runner.time, "time", lambda: 1_700_000_000)
    root = path.parent / "journal"
    receipt = (
        {"status": "unknown"}
        if delivery == "none"
        else {"status": "accepted", "delivery": "acknowledged", "destination_ref": "77"}
    )
    with runner._journal_lock(root, action["action_id"]) as directory:
        runner._write(
            root,
            action["action_id"],
            {
                "generation": claimed["generation"],
                "phase": "reported",
                "dispatch_attempted": True,
                "receipt": receipt,
            },
            directory,
        )
    result = runner.tick(path, budget_seconds=15, max_actions=1)
    assert result["scheduled"] == 0
    with runner._journal_lock(root, action["action_id"]) as directory:
        parked = runner._read(root, action["action_id"], directory)
    assert parked["phase"] == "parked"
    assert parked["receipt"] == receipt
    assert runner.tick(path, budget_seconds=15, max_actions=1)["scheduled"] == 0
    assert (
        conn.execute("SELECT status FROM agent_actions WHERE action_id=?", (action["action_id"],)).fetchone()[0]
        == "unknown"
    )
    conn.close()


@requires_posix
@pytest.mark.parametrize("interrupted", [False, True])
def test_handle_free_inbox_recovery_resumes_durable_pages(tmp_path, monkeypatch, interrupted):
    from brigade import fleet_action_runner as runner, fleet_client_messages as messages, fleet_hub_actions as actions
    from tests.test_fleet_action_r2_core import inbox_action

    conn, path, _, action = inbox_action(tmp_path, monkeypatch)
    claim = actions.claim(conn, "node-fixture")
    sends, cursors = [], []
    failed = False

    def invoke(executable, argv, **kwargs):
        nonlocal failed
        assert kwargs == {"timeout": 15}
        if argv[:3] == ["-m", "brigade.fleet_action_adapters", "--inbox-send"]:
            sends.append(argv)
            return {"error": "fixture lost send response"}
        assert argv[:4] == ["fleet", "msg", "sent", "--after"] and argv[-1] == "--json"
        after = int(argv[4])
        cursors.append(after)
        if interrupted and after == 200 and not failed:
            failed = True
            raise OSError("fixture interrupted pagination")
        page = [{"id": i, "client_id": "older-message"} for i in range(after + 1, min(after + 100, 1100) + 1)]
        if after == 1100:
            page = [{"id": 1101, "client_id": action["action_id"], "acked_at": "now"}]
        return {"messages": page, "next_cursor": page[-1]["id"] if page else after}

    monkeypatch.setattr(runner.adapters, "invoke", invoke)
    assert runner.execute(path, action["action_id"], claim["generation"])["status"] == "unknown"
    root = path.parent / "journal"
    with runner._journal_lock(root, action["action_id"]) as directory:
        attempted = runner._read(root, action["action_id"], directory)
    assert attempted["dispatch_attempted"] is True
    assert "destination_ref" not in attempted["receipt"]
    monkeypatch.setattr(runner.adapters, "dispatch", lambda *a: pytest.fail("recovery dispatched again"))
    for name in ("send", "flush", "_spool_path", "_drain_locked"):
        monkeypatch.setattr(messages, name, lambda *a, **kw: pytest.fail("recovery touched message spool"))

    assert runner.execute(path, action["action_id"], claim["generation"])["status"] == "unknown"
    expected = [0, 100, 200] if interrupted else list(range(0, 1000, 100))
    assert cursors == expected
    # Reload the durable record, as a fresh executor does, rather than carrying an in-memory cursor.
    with runner._journal_lock(root, action["action_id"]) as directory:
        saved = runner._read(root, action["action_id"], directory)
    assert saved["inbox_lookup_after"] == (200 if interrupted else 1000)
    assert saved["phase"] == "reported" and saved["receipt"]["status"] == "unknown"
    cursors.clear()
    result = runner.execute(path, action["action_id"], claim["generation"])
    assert cursors == list(range(200 if interrupted else 1000, 1101, 100))
    assert len(cursors) <= 10 and len(sends) == 1
    assert result["status"] == "accepted" and result["destination_ref"] == "1101"
    assert result["delivery"] == "acknowledged"
    conn.close()


@requires_posix
@pytest.mark.parametrize(
    "page",
    [
        {"messages": [], "next_cursor": 100},
        {"messages": {"id": 1}, "next_cursor": 100},
        *[
            {"messages": [{"id": 1, "client_id": "older-message"}], "next_cursor": cursor}
            for cursor in (None, True, -1, 0, "100", 1 << 63)
        ],
    ],
)
def test_handle_free_inbox_recovery_stops_on_invalid_page_cursor(tmp_path, monkeypatch, page):
    from brigade import fleet_action_runner as runner, fleet_hub_actions as actions
    from tests.test_fleet_action_r2_core import inbox_action

    conn, path, _, action = inbox_action(tmp_path, monkeypatch)
    claim = actions.claim(conn, "node-fixture")
    monkeypatch.setattr(runner.adapters, "invoke", lambda *a, **kw: {"error": "fixture lost send response"})
    assert runner.execute(path, action["action_id"], claim["generation"])["status"] == "unknown"
    cursors = []

    def invoke(executable, argv, **kwargs):
        assert argv[:4] == ["fleet", "msg", "sent", "--after"]
        cursors.append(int(argv[4]))
        return page

    monkeypatch.setattr(runner.adapters, "invoke", invoke)
    assert runner.execute(path, action["action_id"], claim["generation"])["status"] == "unknown"
    assert cursors == [0]
    root = path.parent / "journal"
    with runner._journal_lock(root, action["action_id"]) as directory:
        saved = runner._read(root, action["action_id"], directory)
    assert saved.get("inbox_lookup_after", 0) == 0
    assert "destination_ref" not in saved["receipt"] and "delivery" not in saved["receipt"]
    conn.close()


@pytest.mark.parametrize("started_at", [True, "bad", float("nan"), float("inf"), float("-inf"), 10**1000])
def test_recovery_age_malformed_values_fail_closed(started_at):
    from brigade import fleet_action_runner as runner

    assert runner._recovery_expired(started_at, now=1_700_000_000) is True


@requires_posix
def test_reconcile_closes_terminal_hub_action_without_reclassifying_it(tmp_path, monkeypatch):
    from brigade import fleet_action_runner as runner, fleet_hub_actions as actions

    conn, path, _, action = broker_fixture(tmp_path, monkeypatch)
    claimed = actions.claim(conn, "node-fixture")
    actions.start(conn, "node-fixture", action["action_id"], claimed["generation"])
    with conn:
        conn.execute(
            "UPDATE agent_actions SET status='finished', started_at=? WHERE action_id=?",
            (1_699_913_600, action["action_id"]),
        )
    monkeypatch.setattr(runner.time, "time", lambda: 1_700_000_000)
    root = path.parent / "journal"
    with runner._journal_lock(root, action["action_id"]) as directory:
        runner._write(
            root,
            action["action_id"],
            {
                "generation": claimed["generation"],
                "phase": "reported",
                "receipt": {"status": "finished", "destination_ref": "fixture-ref"},
            },
            directory,
        )
    runner.tick(path, budget_seconds=15, max_actions=1)
    with runner._journal_lock(root, action["action_id"]) as directory:
        assert runner._read(root, action["action_id"], directory)["phase"] == "closed"
    assert actions.show(conn, action["action_id"])["status"] == "finished"
    conn.close()


@requires_posix
def test_cutoff_preserves_result_receipt_for_explicit_report_replay(tmp_path, monkeypatch):
    from brigade import fleet_action_runner as runner, fleet_client_actions as client
    from tests.test_fleet_action_r2_core import inbox_action

    conn, path, log, action = inbox_action(tmp_path, monkeypatch)
    from brigade import fleet_hub_actions as actions

    claimed = actions.claim(conn, "node-fixture")
    normal_invoke = runner.adapters.invoke
    provider_calls = []

    def ambiguous_send(executable, argv, **kwargs):
        if argv[:3] == ["-m", "brigade.fleet_action_adapters", "--inbox-send"]:
            provider_calls.append("send")
            return {"error": "ambiguous"}
        provider_calls.append("lookup")
        return normal_invoke(executable, argv, **kwargs)

    monkeypatch.setattr(runner.adapters, "invoke", ambiguous_send)
    normal = client.request
    lost_after_apply = False

    def lose_applied_report(body, **kwargs):
        nonlocal lost_after_apply
        response = normal(body, **kwargs)
        if body["op"] == "report" and not lost_after_apply:
            lost_after_apply = True
            raise OSError("fixture lost applied reply")
        return response

    monkeypatch.setattr(client, "request", lose_applied_report)
    with pytest.raises(OSError, match="lost applied reply"):
        runner.execute(path, action["action_id"], claimed["generation"])
    root = path.parent / "journal"
    with runner._journal_lock(root, action["action_id"]) as directory:
        record = runner._read(root, action["action_id"], directory)
        receipt = record["receipt"].copy()
        assert record["phase"] == "result"
    with conn:
        conn.execute(
            "UPDATE agent_actions SET started_at=? WHERE action_id=?",
            (1_699_913_600, action["action_id"]),
        )
    monkeypatch.setattr(runner.time, "time", lambda: 1_700_000_000)
    assert client.request({"op": "show", "action_id": action["action_id"]})["status"] == "unknown"
    tick = runner.tick(path, budget_seconds=15, max_actions=1)
    assert tick["scheduled"] == 0
    with runner._journal_lock(root, action["action_id"]) as directory:
        parked = runner._read(root, action["action_id"], directory)
    assert parked["recovery_parked"] is True
    assert parked["phase"] == "result" and parked["receipt"] == receipt
    assert runner.tick(path, budget_seconds=15, max_actions=1)["scheduled"] == 0
    monkeypatch.setattr(runner.adapters, "recover", lambda *_args: pytest.fail("parked replay queried provider"))
    assert provider_calls == ["send"]
    reports = []

    def count_reports(body, **kwargs):
        if body["op"] == "report":
            reports.append(body["receipt"].copy())
        return normal(body, **kwargs)

    monkeypatch.setattr(client, "request", count_reports)
    assert runner.execute(path, action["action_id"], claimed["generation"])["status"] == "unknown"
    assert len(reports) == 1
    assert reports[0]["event_id"] == receipt["event_id"]
    assert reports[0] == receipt
    assert provider_calls == ["send"]
    assert runner.tick(path, budget_seconds=15, max_actions=1)["scheduled"] == 0
    conn.close()


@requires_posix
def test_delayed_execution_after_cutoff_does_not_query_provider(tmp_path, monkeypatch):
    from brigade import fleet_action_runner as runner, fleet_hub_actions as actions

    conn, path, _, action = broker_fixture(tmp_path, monkeypatch)
    claimed = actions.claim(conn, "node-fixture")
    actions.start(conn, "node-fixture", action["action_id"], claimed["generation"])
    with conn:
        conn.execute(
            "UPDATE agent_actions SET started_at=? WHERE action_id=?",
            (1_699_913_600, action["action_id"]),
        )
    root = path.parent / "journal"
    with runner._journal_lock(root, action["action_id"]) as directory:
        runner._write(
            root,
            action["action_id"],
            {
                "generation": claimed["generation"],
                "phase": "reported",
                "receipt": {"status": "running", "destination_ref": "fixture-ref"},
            },
            directory,
        )
    monkeypatch.setattr(runner.time, "time", lambda: 1_700_000_000)
    calls = []
    monkeypatch.setattr(
        runner.adapters,
        "recover",
        lambda *_args: calls.append("recover") or {"status": "running", "destination_ref": "fixture-ref"},
    )
    assert runner.execute(path, action["action_id"], claimed["generation"])["status"] == "dispatching"
    assert calls == []
    with runner._journal_lock(root, action["action_id"]) as directory:
        assert runner._read(root, action["action_id"], directory)["phase"] == "parked"
    conn.close()


@requires_posix
def test_delayed_inbox_execution_without_handle_does_not_lookup(tmp_path, monkeypatch):
    from brigade import fleet_action_runner as runner, fleet_hub_actions as actions
    from tests.test_fleet_action_r2_core import inbox_action

    conn, path, _, action = inbox_action(tmp_path, monkeypatch)
    claimed = actions.claim(conn, "node-fixture")
    actions.start(conn, "node-fixture", action["action_id"], claimed["generation"])
    with conn:
        conn.execute(
            "UPDATE agent_actions SET started_at=? WHERE action_id=?",
            (1_699_913_600, action["action_id"]),
        )
    root = path.parent / "journal"
    with runner._journal_lock(root, action["action_id"]) as directory:
        runner._write(
            root,
            action["action_id"],
            {
                "generation": claimed["generation"],
                "phase": "reported",
                "dispatch_attempted": True,
                "receipt": {"status": "unknown"},
            },
            directory,
        )
    monkeypatch.setattr(runner.time, "time", lambda: 1_700_000_000)
    monkeypatch.setattr(runner.adapters, "invoke", lambda *_args, **_kwargs: pytest.fail("expired inbox lookup"))
    assert runner.execute(path, action["action_id"], claimed["generation"])["status"] == "dispatching"
    with runner._journal_lock(root, action["action_id"]) as directory:
        parked = runner._read(root, action["action_id"], directory)
    assert parked["phase"] == "parked" and parked["receipt"] == {"status": "unknown"}
    conn.close()


@requires_posix
@pytest.mark.parametrize("kind", ["execution-ref", "inbox-no-handle"])
def test_persisted_recovery_park_survives_clock_rollback(tmp_path, monkeypatch, kind):
    from brigade import fleet_action_runner as runner, fleet_hub_actions as actions

    if kind == "execution-ref":
        conn, path, _, action = broker_fixture(tmp_path, monkeypatch)
        receipt = {"status": "running", "destination_ref": "fixture-ref"}
        hub_status = "running"
    else:
        from tests.test_fleet_action_r2_core import inbox_action

        conn, path, _, action = inbox_action(tmp_path, monkeypatch)
        receipt = {"status": "unknown"}
        hub_status = "unknown"
    claimed = actions.claim(conn, "node-fixture")
    actions.start(conn, "node-fixture", action["action_id"], claimed["generation"])
    with conn:
        conn.execute(
            "UPDATE agent_actions SET status=?, started_at=? WHERE action_id=?",
            (hub_status, 1_699_913_600, action["action_id"]),
        )
    root = path.parent / "journal"
    with runner._journal_lock(root, action["action_id"]) as directory:
        runner._write(
            root,
            action["action_id"],
            {
                "generation": claimed["generation"],
                "phase": "reported",
                "dispatch_attempted": kind == "inbox-no-handle",
                "receipt": receipt,
            },
            directory,
        )
    now = [1_700_000_000]
    monkeypatch.setattr(runner.time, "time", lambda: now[0])
    assert runner.tick(path, budget_seconds=15, max_actions=1)["scheduled"] == 0
    with runner._journal_lock(root, action["action_id"]) as directory:
        parked = runner._read(root, action["action_id"], directory)
    assert parked["phase"] == "parked"

    now[0] = 1_699_999_999
    provider_calls = []
    monkeypatch.setattr(
        runner.adapters,
        "recover",
        lambda *_args: provider_calls.append("recover") or receipt,
    )
    monkeypatch.setattr(
        runner,
        "_inbox_lookup",
        lambda *_args: provider_calls.append("inbox-lookup") or receipt,
    )
    assert runner.execute(path, action["action_id"], claimed["generation"])["status"] == hub_status
    assert provider_calls == []
    with runner._journal_lock(root, action["action_id"]) as directory:
        assert runner._read(root, action["action_id"], directory)["phase"] == "parked"
    assert actions.show(conn, action["action_id"])["status"] == hub_status
    conn.close()


@requires_posix
@pytest.mark.parametrize("terminal", ["finished", "failed"])
def test_terminal_hub_state_replays_exact_pending_result_after_cutoff(tmp_path, monkeypatch, terminal):
    from brigade import fleet_action_runner as runner, fleet_client_actions as client, fleet_hub_actions as actions

    conn, path, _, action = broker_fixture(tmp_path, monkeypatch)
    claimed = actions.claim(conn, "node-fixture")
    actions.start(conn, "node-fixture", action["action_id"], claimed["generation"])
    receipt = {"event_id": "result_fixture", "status": terminal, "destination_ref": "fixture-ref"}
    actions.report(conn, "node-fixture", action["action_id"], claimed["generation"], receipt)
    with conn:
        conn.execute(
            "UPDATE agent_actions SET started_at=? WHERE action_id=?",
            (1_699_913_600, action["action_id"]),
        )
    root = path.parent / "journal"
    with runner._journal_lock(root, action["action_id"]) as directory:
        runner._write(
            root,
            action["action_id"],
            {"generation": claimed["generation"], "phase": "result", "receipt": receipt},
            directory,
        )
    monkeypatch.setattr(runner.time, "time", lambda: 1_700_000_000)
    monkeypatch.setattr(runner.adapters, "recover", lambda *_args: pytest.fail("terminal replay queried provider"))
    monkeypatch.setattr(runner.adapters, "dispatch", lambda *_args: pytest.fail("terminal replay redispatched"))
    reports = []
    normal = client.request

    def count_report(body, **kwargs):
        if body["op"] == "report":
            reports.append(body["receipt"].copy())
        return normal(body, **kwargs)

    monkeypatch.setattr(client, "request", count_report)
    assert runner.tick(path, budget_seconds=15, max_actions=1)["scheduled"] == 0
    with runner._journal_lock(root, action["action_id"]) as directory:
        parked = runner._read(root, action["action_id"], directory)
    assert parked["phase"] == "result" and parked["recovery_parked"] is True
    assert runner.execute(path, action["action_id"], claimed["generation"])["status"] == terminal
    assert reports == [receipt]
    conn.close()


@requires_posix
def test_executor_unavailable_removes_target_instead_of_shell_fallback(tmp_path, monkeypatch):
    from brigade import fleet_action_runner as runner

    conn, path, _, _ = broker_fixture(tmp_path, monkeypatch)
    value = json.loads(path.read_text())
    value["executables"]["systemd-run"] = str(tmp_path / "absent")
    path.write_text(json.dumps(value))
    assert runner.tick(path)["available"] is False
    assert conn.execute("SELECT count(*) FROM action_targets").fetchone()[0] == 0
    conn.close()


@pytest.mark.parametrize(
    "family,payload,expected",
    [
        (
            "t3",
            {
                "schema": "t3-fleet.result.v1",
                "request_id": "fixture-host-fixture-repo-12345678-1234-1234-1234-123456789abc",
                "state": "running",
            },
            "running",
        ),
        ("grokbot", {"message": {"id": 41, "client_id": "aa_fixture"}}, "accepted"),
        ("dot", {"message": {"id": 42, "client_id": "aa_fixture", "acked_at": "now"}}, "accepted"),
    ],
)
@requires_posix
def test_supported_transport_contracts(tmp_path, monkeypatch, family, payload, expected):
    from brigade import fleet_action_adapters as adapters

    executable, log = fake_executable(tmp_path, payload)
    normal = adapters.invoke

    def invoke(tool, argv, **kw):
        if family != "t3":
            assert tool == Path(sys.executable)
            assert argv[:3] == ["-m", "brigade.fleet_action_adapters", "--inbox-send"]
        return normal(executable, argv, **kw)

    monkeypatch.setattr(adapters, "invoke", invoke)
    prompt = tmp_path / "prompt"
    prompt.write_text("hostile $(touch nowhere) `echo bad` 雪", encoding="utf-8")
    prompt.chmod(0o600)
    target = {"family": family, "principal": "agent:fixture", "capability": "inbox" if family != "t3" else "execution"}
    binding = {
        "host": "fixture-host",
        "repository": "fixture-repo",
        "source_revision": "a" * 40,
        "override_reason": "explicit operator selection",
    }
    action = {"action_id": "aa_fixture", "request": {"target": target, "model_id": "fixture-seat"}}
    receipt = adapters.dispatch(action, binding, executable, prompt)
    assert receipt["status"] == expected
    assert "verification" not in receipt
    record = json.loads(log.read_text())
    args = record["args"]
    assert "$(touch nowhere)" not in " ".join(args)
    if family == "t3":
        assert args[args.index("--seat") + 1] == "fixture-seat"
        assert args[args.index("--source-revision") + 1] == "a" * 40
        assert receipt["destination_ref"] == "fixture-host-fixture-repo-12345678-1234-1234-1234-123456789abc"
    else:
        assert args[args.index("--client-id") + 1] == "aa_fixture"
        assert args[args.index("--to") + 1] == "agent:fixture"
        assert receipt["delivery"] in {"delivered", "acknowledged"}


def test_cli_registers_actions_without_permitting_shell_input():
    from brigade.cli import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["fleet", "actions", "tick", "--manifest", "fixture.json"])
    assert args.func.__module__ == "brigade.cli.fleet_actions"
    assert args.budget_seconds == 15 and args.max_actions == 1
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "fleet",
                "actions",
                "execute",
                "aa_fixture",
                "--generation",
                "1",
                "--manifest",
                "fixture.json",
                "--shell",
                "echo bad",
            ]
        )


@pytest.mark.parametrize("family", ["dot", "grokbot"])
@pytest.mark.parametrize("found", [False, True])
@requires_posix
def test_inbox_recovery_without_reference_only_reads_stable_id(tmp_path, family, found):
    from brigade import fleet_action_adapters as adapters

    executable, log = fake_executable(
        tmp_path,
        {"messages": [{"id": 77, "client_id": "aa_fixture", "acked_at": "now"}] if found else []},
    )
    action = {"action_id": "aa_fixture", "request": {"target": {"family": family}}}
    receipt = adapters.recover(action, executable, None)
    # Recovery cannot flush unrelated queued messages or invent pending delivery.
    calls = [json.loads(line)["args"] for line in log.read_text().splitlines()]
    assert all("send" not in args and "flush" not in args for args in calls)
    assert calls == [["fleet", "msg", "sent", "--after", "0", "--json"]]
    assert receipt == (
        {"status": "accepted", "delivery": "acknowledged", "destination_ref": "77"} if found else {"status": "unknown"}
    )


@requires_posix
def test_lost_report_response_replays_saved_receipt_without_another_turn(tmp_path, monkeypatch):
    from brigade import fleet_action_runner as runner, fleet_client_actions as client, fleet_hub_actions as actions

    conn, path, log, action = broker_fixture(tmp_path, monkeypatch)
    claimed = actions.claim(conn, "node-fixture")
    normal = client.request
    lost = False

    def lose_response(body, **kwargs):
        nonlocal lost
        if body["op"] == "report" and not lost:
            lost = True
            raise OSError("fixture lost reply")
        return normal(body, **kwargs)

    monkeypatch.setattr(client, "request", lose_response)
    with pytest.raises(OSError, match="lost reply"):
        runner.execute(path, action["action_id"], claimed["generation"])
    assert runner.execute(path, action["action_id"], claimed["generation"])["status"] == "finished"
    assert len(log.read_text().splitlines()) == 1
    assert conn.execute("SELECT count(*) FROM action_events WHERE event_id LIKE 'result_%'").fetchone()[0] == 1
    conn.close()


@pytest.mark.parametrize("family", ["dot", "grokbot"])
@requires_posix
def test_initial_inbox_send_leaves_unrelated_credential_spool_untouched(tmp_path, monkeypatch, capsys, family):
    from brigade import fleet_action_adapters as adapters, fleet_client, fleet_client_messages as messages
    from brigade import fleet_client_message_credentials as credentials

    monkeypatch.setattr(
        credentials, "message_settings", lambda *a, **kw: ("https://hub.example.test", "x" * 32, "fixture")
    )
    monkeypatch.setattr(fleet_client, "spool_path", lambda node: tmp_path / "spool" / "node.jsonl")
    authority = credentials.MessageAuthority.capture()
    spool = messages._spool_path(authority.node_id, authority.token, hub_url=authority.hub)
    spool.parent.mkdir(mode=0o700)
    old = {
        "action": "send",
        "to": "agent:other",
        "subject": "Unrelated old subject",
        "body": "old body",
        "ref": "old",
        "thread": "old",
        "priority": "normal",
        "client_id": "old-client",
        "enqueued_at": datetime.now(timezone.utc).isoformat(),
    }
    original = (json.dumps(old) + "\n").encode()
    spool.write_bytes(original)
    spool.chmod(0o600)
    seen = []

    def request(hub, token, body, **kw):
        seen.append(body.copy())
        return 200, {"message": {"id": 42, "client_id": body["client_id"]}}

    monkeypatch.setattr(messages, "_request", request)

    def invoke(executable, argv, **kw):
        if argv[:3] == ["fleet", "msg", "send"]:
            # Exercise the actual old route, including its credential-wide drain.
            return messages.send(
                recipient=argv[argv.index("--to") + 1],
                subject=argv[argv.index("--subject") + 1],
                body=prompt.read_text(encoding="utf-8"),
                reference="aa_fixture",
                priority="normal",
                thread="aa_fixture",
                client_id="aa_fixture",
            )
        assert executable == Path(sys.executable)
        assert argv[:3] == ["-m", "brigade.fleet_action_adapters", "--inbox-send"]
        assert kw == {"timeout": 15}
        assert adapters._inbox_main(argv[2:]) == 0
        return json.loads(capsys.readouterr().out)

    monkeypatch.setattr(adapters, "invoke", invoke)
    prompt = tmp_path / "prompt"
    prompt.write_text("hostile $(touch nowhere) `echo bad` 雪", encoding="utf-8")
    action = {"action_id": "aa_fixture", "request": {"target": {"family": family, "principal": "agent:fixture"}}}
    receipt = adapters.dispatch(action, {}, tmp_path / "unused-brigade", prompt)
    assert receipt == {"status": "accepted", "delivery": "delivered", "destination_ref": "42"}
    assert seen == [
        {
            "action": "send",
            "to": "agent:fixture",
            "subject": "Repair alert aa_fixture",
            "body": "hostile $(touch nowhere) `echo bad` 雪",
            "ref": "aa_fixture",
            "thread": "aa_fixture",
            "client_id": "aa_fixture",
            "priority": "normal",
        }
    ]
    assert spool.read_bytes() == original


@pytest.fixture(params=["authority", "legacy"])
def inbox_online_api(request, monkeypatch):
    from brigade import fleet_client_message_credentials as credentials, fleet_client_messages as messages

    settings = ("https://hub.example.test", "x" * 32, "fixture")
    monkeypatch.setattr(credentials, "message_settings", lambda *a, **kw: settings)
    if request.param == "legacy":
        # Exact older co-versioned private API: settings selection is inside _online.
        def online(body, *, token_file=None, timeout=None):
            assert token_file is None
            hub, token, _ = credentials.message_settings(token_file)
            status, payload = messages._request(hub, token, body, timeout=timeout)
            if status != 200:
                raise RuntimeError("private upstream error")
            return payload

        monkeypatch.setattr(messages, "_online", online)
    return request.param


def inbox_args(prompt):
    return ["--inbox-send", "--to", "agent:fixture", "--client-id", "aa_fixture", "--body-file", str(prompt)]


@pytest.mark.parametrize("failure", ["outage", "wrong-client", "queued"])
@requires_posix
def test_inbox_helper_unknown_never_queues_or_retries(tmp_path, monkeypatch, capsys, inbox_online_api, failure):
    from brigade import fleet_action_adapters as adapters, fleet_client, fleet_client_messages as messages

    attempts = []

    def request(hub, token, body, **kw):
        attempts.append(body)
        if failure == "outage":
            raise OSError("private credential and prompt details")
        return 200, (
            {"queued": True, "client_id": "aa_fixture"}
            if failure == "queued"
            else {"message": {"id": 42, "client_id": "wrong-client"}}
        )

    def forbidden(*args, **kwargs):
        pytest.fail("online helper must not access any spool")

    monkeypatch.setattr(messages, "_request", request)
    for name in ("send", "_send_ordered", "_drain_locked", "_queue_locked", "flush", "_spool_path"):
        monkeypatch.setattr(messages, name, forbidden)
    monkeypatch.setattr(fleet_client, "_read_spool", forbidden)
    prompt = tmp_path / "body"
    prompt.write_text("data only")
    assert adapters._inbox_main(inbox_args(prompt)) == 1
    assert json.loads(capsys.readouterr().out) == {"error": "transport-acceptance-unknown"}
    assert len(attempts) == 1
    assert sorted(path.name for path in tmp_path.iterdir()) == ["body"]


@pytest.mark.parametrize(
    "malformed",
    [
        "flags",
        "client",
        "recipient",
        *[
            pytest.param(case, marks=requires_posix)
            for case in ("oversize", "utf8", "empty", "missing", "fifo", "symlink")
        ],
    ],
)
def test_inbox_helper_rejects_malformed_input_before_transport(tmp_path, monkeypatch, capsys, malformed):
    from brigade import (
        fleet_action_adapters as adapters,
        fleet_client_messages as messages,
        fleet_hub_messages as limits,
    )

    prompt = tmp_path / "body"
    prompt.write_text("data only")
    args = inbox_args(prompt)
    if malformed == "flags":
        args += ["--arbitrary", "private-data"]
    elif malformed == "client":
        args[4] = "invalid client id"
    elif malformed == "recipient":
        args[2] = " "
    elif malformed == "oversize":
        prompt.write_bytes(b"x" * (limits.BODY_LIMIT + 1))
    elif malformed == "utf8":
        prompt.write_bytes(b"\xff")
    elif malformed == "empty":
        prompt.write_text(" ")
    elif malformed == "missing":
        prompt.unlink()
    elif malformed == "fifo":
        prompt.unlink()
        os.mkfifo(prompt)
    elif malformed == "symlink":
        prompt.rename(tmp_path / "other")
        prompt.symlink_to(tmp_path / "other")

    def forbidden(*args, **kwargs):
        pytest.fail("malformed data must fail before credential capture or transport")

    monkeypatch.setattr(messages.MessageAuthority, "capture", forbidden)
    monkeypatch.setattr(messages, "_online", forbidden)
    assert adapters._inbox_main(args) == 1
    assert json.loads(capsys.readouterr().out) == {"error": "transport-acceptance-unknown"}


@pytest.mark.parametrize(
    "hub,allowed",
    [("http://remote.example.test", False), ("https://hub.example.test", True), ("http://127.0.0.1", True)],
)
@requires_posix
def test_inbox_helper_retains_transport_endpoint_guard(tmp_path, monkeypatch, capsys, inbox_online_api, hub, allowed):
    import io
    from brigade import fleet_action_adapters as adapters, fleet_client, fleet_client_message_credentials as credentials

    monkeypatch.setattr(credentials, "message_settings", lambda *a, **kw: (hub, "x" * 32, "fixture"))
    opened = []

    def hub_open(request, **kw):
        opened.append(request.full_url)
        response = io.BytesIO(b'{"message":{"id":42,"client_id":"aa_fixture"}}')
        response.status = 200
        return response

    monkeypatch.setattr(fleet_client, "_hub_open", hub_open)
    prompt = tmp_path / "body"
    prompt.write_text("data only")
    assert adapters._inbox_main(inbox_args(prompt)) == (0 if allowed else 1)
    output = json.loads(capsys.readouterr().out)
    assert opened == ([hub + "/messages"] if allowed else [])
    assert output == (
        {"message": {"id": 42, "client_id": "aa_fixture"}} if allowed else {"error": "transport-acceptance-unknown"}
    )


@requires_posix
def test_inbox_helper_validates_captured_authority_before_transport(tmp_path, monkeypatch, capsys):
    from brigade import fleet_action_adapters as adapters, fleet_client_message_credentials as credentials
    from brigade import fleet_client_messages as messages

    calls = []

    def validate(self):
        calls.append("validate")
        raise credentials.MessageAuthorityError("private rotated credential")

    monkeypatch.setattr(
        credentials, "message_settings", lambda *a, **kw: ("https://hub.example.test", "x" * 32, "fixture")
    )
    monkeypatch.setattr(credentials.MessageAuthority, "validate", validate)
    monkeypatch.setattr(messages, "_request", lambda *a, **kw: pytest.fail("invalid authority reached transport"))
    prompt = tmp_path / "body"
    prompt.write_text("data only")
    assert adapters._inbox_main(inbox_args(prompt)) == 1
    assert calls == ["validate"]
    assert json.loads(capsys.readouterr().out) == {"error": "transport-acceptance-unknown"}


@requires_posix
def test_inbox_helper_unknown_private_api_fails_closed(tmp_path, monkeypatch, capsys):
    from brigade import fleet_action_adapters as adapters, fleet_client_messages as messages

    def unsupported(body, **kwargs):
        pytest.fail("unknown private contract must never be called")

    monkeypatch.setattr(messages, "_online", unsupported)
    prompt = tmp_path / "body"
    prompt.write_text("data only")
    assert adapters._inbox_main(inbox_args(prompt)) == 1
    assert json.loads(capsys.readouterr().out) == {"error": "transport-acceptance-unknown"}


def test_native_transport_refuses_non_posix_before_process_launch(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from brigade import fleet_action_adapters as adapters
    from brigade.fleet_action_auth import ActionError

    monkeypatch.setattr(adapters, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(adapters.subprocess, "Popen", lambda *a, **kw: pytest.fail("unsupported transport launched"))
    with pytest.raises(ActionError, match="transport-platform-unsupported"):
        adapters.invoke(tmp_path / "unused", [])


def test_broker_journal_refuses_non_posix_before_file_access(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from brigade import fleet_action_runner as runner
    from brigade.fleet_action_auth import ActionError

    monkeypatch.setattr(runner, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(
        runner.fleet_client, "_ensure_private_dir", lambda *a: pytest.fail("unsupported journal opened")
    )
    with pytest.raises(ActionError, match="transient-executor-unavailable"):
        with runner._journal_lock(tmp_path / "journal", "fixture"):
            pytest.fail("unsupported journal acquired")
    assert not (tmp_path / "journal").exists()


def test_inbox_helper_without_secure_file_flags_refuses_before_transport(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    from brigade import fleet_action_adapters as adapters

    prompt = tmp_path / "body"
    prompt.write_text("data only", encoding="utf-8")
    monkeypatch.setattr(adapters, "os", SimpleNamespace(O_RDONLY=0))
    monkeypatch.setattr(adapters, "_inbox_online", lambda *a: pytest.fail("unsupported file access reached transport"))
    assert adapters._inbox_main(inbox_args(prompt)) == 1
    assert json.loads(capsys.readouterr().out) == {"error": "transport-acceptance-unknown"}
