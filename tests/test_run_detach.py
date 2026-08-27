import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from brigade import aboyeur, cli, proc, run_checkpoint, run_journal
from brigade import roster as roster_mod
from brigade import runguard
from brigade.cli import run as run_cli


def _pid_is_running(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().split()[2]
    except (FileNotFoundError, IndexError, OSError):
        return False
    return state != "Z"


def _expected_detach_mode() -> str:
    if os.name == "nt":
        return proc.DETACH_MODE_WINDOWS_JOB_BREAKAWAY
    return proc.DETACH_MODE_POSIX_SESSION


def _write_roster(target: Path) -> None:
    config_dir = target / ".brigade"
    config_dir.mkdir()
    (config_dir / "roster.toml").write_text(
        """
orchestrator = "chef"

[agents.chef]
cli = "codex"
role = "plan"

[agents.coder]
cli = "codex"
role = "code"
"""
    )


def test_run_detach_rejects_fixed_incompatibilities(tmp_path, capsys):
    _write_roster(tmp_path)

    for flag in ("--dry-run", "--no-artifacts", "--inspect"):
        rc = cli.main(["run", "x", "--cwd", str(tmp_path), "--detach", flag])
        assert rc == 2
        assert f"--detach cannot be used with {flag}" in capsys.readouterr().err


def test_run_detach_spawns_child_with_output_dir_and_log(tmp_path, monkeypatch, capsys):
    _write_roster(tmp_path)
    calls = []

    class FakeProcess:
        pid = 4321

        def __init__(self, argv, **kwargs):
            calls.append((argv, kwargs))
            output_dir = Path(argv[argv.index("--output-dir") + 1])
            assert output_dir.is_dir()
            assert "--detach" not in argv
            assert argv.count("--output-dir") == 1
            assert kwargs["cwd"] == tmp_path
            kwargs["stdout"].write("child started\n")
            kwargs["stdout"].flush()
            (output_dir / "run.json").write_text(json.dumps({"status": "started"}) + "\n")

        def poll(self):
            return None

    monkeypatch.setattr(run_cli, "Popen", FakeProcess)

    rc = cli.main(["run", "do work", "--cwd", str(tmp_path), "--detach", "--worker", "coder"])

    captured = capsys.readouterr()
    assert rc == 0
    assert len(calls) == 1
    run_dirs = list((tmp_path / ".brigade" / "runs").iterdir())
    assert len(run_dirs) == 1
    run_dir = run_dirs[0]
    log_lines = (run_dir / "detached.log").read_text().splitlines()
    assert "child started" in log_lines
    assert f"brigade detach: {_expected_detach_mode()}" in log_lines
    assert f"run: {run_dir.name}" in captured.out
    assert "detached: pid 4321" in captured.err
    assert f"artifacts: {run_dir}" in captured.err
    assert f"log: {run_dir / 'detached.log'}" in captured.err


def test_run_detach_reports_early_child_exit(tmp_path, monkeypatch, capsys):
    _write_roster(tmp_path)

    class FakeProcess:
        pid = 4321

        def __init__(self, argv, **kwargs):
            kwargs["stdout"].write("boom\n")
            kwargs["stdout"].flush()

        def poll(self):
            return 7

    monkeypatch.setattr(run_cli, "Popen", FakeProcess)

    rc = cli.main(["run", "do work", "--cwd", str(tmp_path), "--detach", "--worker", "coder"])

    captured = capsys.readouterr()
    run_dir = next((tmp_path / ".brigade" / "runs").iterdir())
    assert rc == 2
    assert "detached child exited before run metadata was written: exit 7" in captured.err
    assert f"log: {run_dir / 'detached.log'}" in captured.err
    log_lines = (run_dir / "detached.log").read_text().splitlines()
    assert "boom" in log_lines
    assert f"brigade detach: {_expected_detach_mode()}" in log_lines
    run_meta = json.loads((run_dir / "run.json").read_text())
    assert run_meta["status"] == "failed"
    assert run_meta["finished_at"].endswith("Z")
    assert run_meta["failure"] == {
        "phase": "startup",
        "kind": "early-exit",
        "detail": "detached child exited before run metadata was written: exit 7",
        "seat": "coder",
    }


def test_run_detach_records_spawn_failure_receipt(tmp_path, monkeypatch, capsys):
    _write_roster(tmp_path)

    def failed_spawn(*args, **kwargs):  # noqa: ARG001
        raise OSError("spawn denied")

    monkeypatch.setattr(run_cli, "Popen", failed_spawn)

    assert cli.main(["run", "do work", "--cwd", str(tmp_path), "--detach", "--worker", "coder"]) == 2

    run_dir = next((tmp_path / ".brigade" / "runs").iterdir())
    run_meta = json.loads((run_dir / "run.json").read_text())
    assert "failed to start detached run: spawn denied" in capsys.readouterr().err
    assert run_meta["status"] == "failed"
    assert run_meta["finished_at"].endswith("Z")
    assert run_meta["failure"] == {
        "phase": "startup",
        "kind": "spawn-error",
        "detail": "failed to start detached run: spawn denied",
        "seat": "coder",
    }


def test_run_detach_terminalizes_roster_snapshot_write_failure(tmp_path, monkeypatch):
    _write_roster(tmp_path)
    real_write_json = aboyeur._write_json

    def fail_roster_write(path, payload):
        if path.name == "roster.json":
            raise OSError("roster snapshot denied")
        return real_write_json(path, payload)

    monkeypatch.setattr(aboyeur, "_write_json", fail_roster_write)

    assert cli.main(["run", "do work", "--cwd", str(tmp_path), "--detach", "--worker", "coder"]) == 2

    run_dir = next((tmp_path / ".brigade" / "runs").iterdir())
    receipt = json.loads((run_dir / "run.json").read_text())
    assert receipt["schema"] == "brigade.run.v1"
    assert receipt["status"] == "failed"
    assert receipt["failure"] == {
        "phase": "startup",
        "kind": "unexpected-error",
        "detail": "OSError: roster snapshot denied",
        "seat": "coder",
    }


def test_run_detach_terminalizes_sigterm_during_roster_snapshot(tmp_path, monkeypatch):
    _write_roster(tmp_path)
    real_write_json = aboyeur._write_json

    def terminate_during_roster_write(path, payload):
        if path.name == "roster.json":
            signal.raise_signal(signal.SIGTERM)
        return real_write_json(path, payload)

    monkeypatch.setattr(aboyeur, "_write_json", terminate_during_roster_write)

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["run", "do work", "--cwd", str(tmp_path), "--detach", "--worker", "coder"])

    assert exc_info.value.code == 128 + signal.SIGTERM
    run_dir = next((tmp_path / ".brigade" / "runs").iterdir())
    receipt = json.loads((run_dir / "run.json").read_text())
    assert receipt["schema"] == "brigade.run.v1"
    assert receipt["status"] == "canceled"
    assert receipt["failure"] == {
        "phase": "startup",
        "kind": "signal",
        "detail": "run terminated by SIGTERM",
        "seat": "coder",
    }


def test_run_detach_terminalizes_keyboard_interrupt_during_startup_poll(tmp_path, monkeypatch):
    _write_roster(tmp_path)

    class FakeProcess:
        pid = 4321

        def poll(self):
            return 0

    monkeypatch.setattr(run_cli, "Popen", lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr(
        run_cli,
        "_poll_detached_start",
        lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    assert cli.main(["run", "do work", "--cwd", str(tmp_path), "--detach", "--worker", "coder"]) == 130

    run_dir = next((tmp_path / ".brigade" / "runs").iterdir())
    receipt = json.loads((run_dir / "run.json").read_text())
    assert receipt["schema"] == "brigade.run.v1"
    assert receipt["status"] == "canceled"
    assert receipt["failure"] == {
        "phase": "startup",
        "kind": "keyboard-interrupt",
        "detail": "run canceled by user",
        "seat": "coder",
    }


def test_run_detach_kills_child_when_interrupted_during_registry_registration(tmp_path, monkeypatch):
    _write_roster(tmp_path)
    events = []

    class FakeProcess:
        pid = 4321

        def poll(self):
            return None

    class InterruptingRegistry:
        def register(self, child):
            events.append(("register", child.pid))
            raise KeyboardInterrupt

        def terminate(self, child):
            events.append(("terminate", child.pid))

        def cancel(self):
            events.append(("cancel", None))

        def unregister(self, child):
            events.append(("unregister", child.pid))

    monkeypatch.setattr(run_cli, "Popen", lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr(proc, "ProcessRegistry", InterruptingRegistry)

    assert cli.main(["run", "do work", "--cwd", str(tmp_path), "--detach", "--worker", "coder"]) == 130
    assert events[:2] == [("register", 4321), ("terminate", 4321)]


@pytest.mark.parametrize("escape", ["keyboard", "sigterm"])
def test_run_detach_parent_stops_terminalizing_receipt_at_child_takeover(tmp_path, monkeypatch, escape):
    _write_roster(tmp_path)
    events = []

    class FakeProcess:
        pid = 4321

        def poll(self):
            return None

    class InterruptingRegistry:
        def register(self, child):
            events.append(("register", child.pid))

        def terminate(self, child):
            events.append(("terminate", child.pid))

        def cancel(self):
            events.append(("cancel", None))

        def unregister(self, child):
            events.append(("unregister", child.pid))
            if escape == "sigterm":
                signal.raise_signal(signal.SIGTERM)
            raise KeyboardInterrupt

    def take_over(child, output_dir, *, initial_receipt):  # noqa: ARG001
        (output_dir / "run.json").write_text(json.dumps({"status": "dispatching", "active_seats": ["coder"]}) + "\n")
        return None, True

    monkeypatch.setattr(run_cli, "Popen", lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr(proc, "ProcessRegistry", InterruptingRegistry)
    monkeypatch.setattr(run_cli, "_poll_detached_start", take_over)

    if escape == "sigterm":
        with pytest.raises(SystemExit) as exc_info:
            cli.main(["run", "do work", "--cwd", str(tmp_path), "--detach", "--worker", "coder"])
        assert exc_info.value.code == 128 + signal.SIGTERM
    else:
        assert cli.main(["run", "do work", "--cwd", str(tmp_path), "--detach", "--worker", "coder"]) == 130

    run_dir = next((tmp_path / ".brigade" / "runs").iterdir())
    receipt = json.loads((run_dir / "run.json").read_text())
    assert receipt == {"status": "dispatching", "active_seats": ["coder"]}
    assert events == [("register", 4321), ("unregister", 4321)]


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
@pytest.mark.parametrize(
    ("escape", "expected_code", "expected_status", "expected_kind"),
    [
        ("keyboard", 130, "canceled", "keyboard-interrupt"),
        ("sigterm", 128 + signal.SIGTERM, "canceled", "signal"),
        ("error", 2, "failed", "unexpected-error"),
    ],
)
def test_run_detach_startup_escape_kills_child_group_before_terminal_receipt(
    tmp_path, monkeypatch, escape, expected_code, expected_status, expected_kind
):
    _write_roster(tmp_path)
    child_pid_path = tmp_path / "detached-child.pid"
    descendant_pid_path = tmp_path / "detached-descendant.pid"
    output_dir = tmp_path / "run"
    overwrite_code = (
        "import time; from pathlib import Path; "
        "time.sleep(0.4); "
        f'Path({str(output_dir / "run.json")!r}).write_text(\'{{"status": "overwritten"}}\\n\')'
    )
    child_code = (
        "import os,subprocess,sys,time; from pathlib import Path; "
        f"child=subprocess.Popen([sys.executable, '-c', {overwrite_code!r}]); "
        f"Path({str(child_pid_path)!r}).write_text(str(os.getpid())); "
        f"Path({str(descendant_pid_path)!r}).write_text(str(child.pid)); "
        "time.sleep(60)"
    )
    spawned = []
    real_popen = subprocess.Popen

    def tracked_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        spawned.append(process)
        return process

    def interrupt_poll(*args, **kwargs):  # noqa: ARG001
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if child_pid_path.is_file() and descendant_pid_path.is_file():
                break
            time.sleep(0.01)
        else:
            pytest.fail("detached child did not start")
        if escape == "keyboard":
            raise KeyboardInterrupt
        if escape == "sigterm":
            signal.raise_signal(signal.SIGTERM)
        raise RuntimeError("startup poll exploded")

    monkeypatch.setattr(run_cli, "Popen", tracked_popen)
    monkeypatch.setattr(
        run_cli,
        "_detached_child_argv",
        lambda *args, **kwargs: [sys.executable, "-c", child_code],
    )
    monkeypatch.setattr(run_cli, "_poll_detached_start", interrupt_poll)

    try:
        if escape == "sigterm":
            with pytest.raises(SystemExit) as exc_info:
                cli.main(["run", "do work", "--cwd", str(tmp_path), "--output-dir", str(output_dir), "--detach"])
            assert exc_info.value.code == expected_code
        else:
            assert (
                cli.main(["run", "do work", "--cwd", str(tmp_path), "--output-dir", str(output_dir), "--detach"])
                == expected_code
            )

        time.sleep(0.6)
        receipt = json.loads((output_dir / "run.json").read_text())
        assert receipt["status"] == expected_status
        assert receipt["failure"]["kind"] == expected_kind
        assert not _pid_is_running(int(child_pid_path.read_text()))
        assert not _pid_is_running(int(descendant_pid_path.read_text()))
    finally:
        for process in spawned:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)


def test_run_detach_terminalizes_startup_timeout_and_kills_child(tmp_path, monkeypatch):
    _write_roster(tmp_path)

    class FakeProcess:
        pid = 4321

        def __init__(self, argv, **kwargs):
            pass

        def poll(self):
            return None

    monkeypatch.setattr(run_cli, "Popen", FakeProcess)
    monkeypatch.setattr(run_cli, "_DETACH_START_TIMEOUT_SECONDS", 0.0)

    assert cli.main(["run", "do work", "--cwd", str(tmp_path), "--detach"]) == 2

    run_dir = next((tmp_path / ".brigade" / "runs").iterdir())
    run_meta = json.loads((run_dir / "run.json").read_text())
    roster = json.loads((run_dir / "roster.json").read_text())
    assert run_meta["status"] == "timeout"
    assert run_meta["failure"] == {
        "phase": "startup",
        "kind": "timeout",
        "detail": "detached child did not write run metadata within 0 seconds",
        "seat": "chef",
    }
    assert run_meta["finished_at"].endswith("Z")
    assert roster["orchestrator"] == "chef"


def test_run_detach_child_records_artifact_identity_in_lock(tmp_path, monkeypatch):
    _write_roster(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    _write_roster(home)
    workspace_roster = tmp_path / ".brigade" / "roster.toml"
    user_roster = home / ".brigade" / "roster.toml"
    seen = {}

    def fake_run(*args, output_dir=None, **kwargs):
        resolution = args[1].resolution
        seen["roster_path"] = str(resolution.path)
        seen["roster_source"] = resolution.source
        seen["roster_shadowed"] = [str(path) for path in resolution.shadowed]
        owner_path = tmp_path / ".brigade" / "run.lock" / "owner.json"
        seen.update(json.loads(owner_path.read_text()))
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "run.json").write_text(json.dumps({"status": "ok"}) + "\n")
        return 0

    class FakeProcess:
        pid = 4321

        def __init__(self, argv, **kwargs):
            assert cli.main(argv[3:]) == 0

        def poll(self):
            return None

    monkeypatch.setattr(aboyeur, "run", fake_run)
    monkeypatch.setattr(run_cli, "Popen", FakeProcess)
    monkeypatch.setattr(Path, "home", lambda: home)

    assert cli.main(["run", "do work", "--cwd", str(tmp_path), "--detach"]) == 0

    run_dir = next((tmp_path / ".brigade" / "runs").iterdir())
    assert seen["run_dir"] == str(run_dir.resolve())
    assert isinstance(seen["owner_token"], str) and seen["owner_token"]
    assert seen["roster_path"] == str(workspace_roster.resolve())
    assert seen["roster_source"] == "workspace"
    assert seen["roster_shadowed"] == [str(user_roster.resolve())]
    assert not (tmp_path / ".brigade" / "run.lock").exists()


def test_run_detach_child_argv_preserves_worker(tmp_path):
    args = argparse.Namespace(
        task="do work",
        allow_dirty=False,
        worktree=False,
        show_plan=False,
        verbose=False,
        read_only=False,
        no_code_graph=False,
        no_evidence=False,
        sandbox=None,
        codex_transport=None,
        handoff=False,
        handoff_inbox=None,
        worker="coder",
        model=None,
        wait=2.5,
        keep_going=False,
        scheduler="waves",
        run_budget=None,
    )
    roster_resolution = roster_mod.RosterResolution(
        path=(tmp_path / "roster.toml").resolve(),
        source="workspace",
        shadowed=((tmp_path / "user-roster.toml").resolve(),),
    )
    output_dir = tmp_path / "run"

    argv = run_cli._detached_child_argv(
        args,
        run_cwd=tmp_path,
        roster_resolution=roster_resolution,
        output_dir=output_dir,
    )

    assert "--worker" in argv
    assert argv[argv.index("--worker") + 1] == "coder"
    assert argv[argv.index("--wait") + 1] == "2.5"
    assert argv[argv.index("--resolved-roster-source") + 1] == "workspace"
    assert argv[argv.index("--resolved-roster-shadowed") + 1] == str(roster_resolution.shadowed[0])
    assert "--run-budget" not in argv


def test_run_detach_child_argv_forwards_run_budget(tmp_path):
    budget_path = tmp_path / "budget.json"
    budget_path.write_text("{}")
    args = argparse.Namespace(
        task="do work",
        allow_dirty=False,
        worktree=False,
        show_plan=False,
        verbose=False,
        read_only=False,
        no_code_graph=False,
        no_evidence=False,
        sandbox=None,
        codex_transport=None,
        handoff=False,
        handoff_inbox=None,
        worker="coder",
        model=None,
        wait=2.5,
        keep_going=False,
        scheduler="waves",
        run_budget=budget_path,
    )
    roster_resolution = roster_mod.RosterResolution(
        path=(tmp_path / "roster.toml").resolve(),
        source="workspace",
        shadowed=((tmp_path / "user-roster.toml").resolve(),),
    )
    output_dir = tmp_path / "run"

    argv = run_cli._detached_child_argv(
        args,
        run_cwd=tmp_path,
        roster_resolution=roster_resolution,
        output_dir=output_dir,
    )

    assert argv[argv.index("--run-budget") + 1] == str(budget_path.resolve())


def _minimal_roster() -> roster_mod.Roster:
    return roster_mod.Roster(
        orchestrator="chef",
        agents={"chef": roster_mod.Agent("chef", "codex", "plan")},
    )


def _journal_path(run_dir: Path) -> Path:
    return run_dir / "events" / "lifecycle.jsonl"


@pytest.fixture
def lifecycle_enabled(monkeypatch):
    monkeypatch.setenv("BRIGADE_LIFECYCLE_JOURNAL", "1")
    yield
    monkeypatch.delenv("BRIGADE_LIFECYCLE_JOURNAL", raising=False)


def test_detach_lifecycle_parent_child_pre_lock_writes_request_not_journal(lifecycle_enabled, tmp_path):
    """Detached parent/child split: request before lock, journal only after lock."""
    repo = tmp_path / "workspace"
    repo.mkdir()
    _write_roster(repo)
    roster = _minimal_roster()
    run_dir = repo / ".brigade" / "runs" / "20260728-170000-detach01"
    run_dir.mkdir(parents=True)

    # Detached parent pre-lock bootstrap.
    aboyeur.record_run_start(
        run_dir,
        task="do work",
        cwd=repo,
        roster=roster,
        read_only=False,
        lock_workspace=repo,
    )
    parent_meta = json.loads((run_dir / "run.json").read_text())
    assert parent_meta["lifecycle_journal_requested"] is True
    assert not (run_dir / "events").exists()

    # Detached child pre-lock bootstrap before runguard.run_lock.
    aboyeur.record_run_start(
        run_dir,
        task="do work",
        cwd=repo,
        roster=roster,
        read_only=False,
        lock_workspace=repo,
    )
    child_pre_meta = json.loads((run_dir / "run.json").read_text())
    assert child_pre_meta["lifecycle_journal_requested"] is True
    assert not (run_dir / "events").exists()

    # Child in-lock start activates journaling, checkpoints, then appends run.created.
    with runguard.run_lock(repo, run_dir=run_dir):
        aboyeur.record_run_start(
            run_dir,
            task="do work",
            cwd=repo,
            roster=roster,
            read_only=False,
            lock_workspace=repo,
        )

    assert _journal_path(run_dir).is_file()
    journal_events = run_journal.read_journal(_journal_path(run_dir)).events
    assert [event.sequence for event in journal_events] == [1, 2]
    assert [event.event_type for event in journal_events] == [
        run_checkpoint.CHECKPOINT_EVENT_TYPE,
        "run.created",
    ]


def test_detach_lifecycle_pre_lock_sigterm_writes_terminal_without_journal(lifecycle_enabled, tmp_path, monkeypatch):
    _write_roster(tmp_path)
    real_write_json = aboyeur._write_json

    def terminate_during_roster_write(path, payload):
        if path.name == "roster.json":
            signal.raise_signal(signal.SIGTERM)
        return real_write_json(path, payload)

    monkeypatch.setattr(aboyeur, "_write_json", terminate_during_roster_write)

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["run", "do work", "--cwd", str(tmp_path), "--detach", "--worker", "coder"])

    assert exc_info.value.code == 128 + signal.SIGTERM
    run_dir = next((tmp_path / ".brigade" / "runs").iterdir())
    receipt = json.loads((run_dir / "run.json").read_text())
    assert receipt["schema"] == "brigade.run.v1"
    assert receipt["status"] == "canceled"
    assert receipt["lifecycle_journal_requested"] is True
    assert receipt["failure"] == {
        "phase": "startup",
        "kind": "signal",
        "detail": "run terminated by SIGTERM",
        "seat": "coder",
    }
    assert not (run_dir / "events").exists()


def test_detached_launch_kwargs_do_not_rely_on_start_new_session_on_windows(monkeypatch):
    """Windows silently drops start_new_session, so detach must use creation flags."""

    monkeypatch.setattr(os, "name", "nt")
    kwargs = proc.detached_launch_kwargs()

    assert "start_new_session" not in kwargs
    flags = kwargs["creationflags"]
    assert flags & proc._WINDOWS_DETACHED_PROCESS
    assert flags & proc._WINDOWS_NEW_PROCESS_GROUP
    assert flags & proc._WINDOWS_CREATE_BREAKAWAY_FROM_JOB

    without_breakaway = proc.detached_launch_kwargs(job_breakaway=False)["creationflags"]
    assert not without_breakaway & proc._WINDOWS_CREATE_BREAKAWAY_FROM_JOB
    assert without_breakaway & proc._WINDOWS_DETACHED_PROCESS


def test_detached_launch_kwargs_keep_posix_new_session(monkeypatch):
    monkeypatch.setattr(os, "name", "posix")

    assert proc.detached_launch_kwargs() == {"start_new_session": True}
    assert proc.detached_launch_kwargs(job_breakaway=False) == {"start_new_session": True}


def test_spawn_detached_retries_without_breakaway_when_the_job_forbids_it(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    attempts = []

    class FakeProcess:
        pid = 99

    def fake_popen(argv, **kwargs):
        attempts.append(kwargs["creationflags"])
        if kwargs["creationflags"] & proc._WINDOWS_CREATE_BREAKAWAY_FROM_JOB:
            error = OSError(5, "Access is denied")
            error.winerror = proc._WINDOWS_BREAKAWAY_DENIED_WINERROR
            raise error
        return FakeProcess()

    process, mode = proc.spawn_detached(["child"], popen=fake_popen)

    assert isinstance(process, FakeProcess)
    assert mode == proc.DETACH_MODE_WINDOWS_NO_BREAKAWAY
    assert len(attempts) == 2
    assert attempts[1] & proc._WINDOWS_DETACHED_PROCESS


def test_spawn_detached_propagates_non_breakaway_spawn_failures(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")

    def fake_popen(argv, **kwargs):
        error = OSError(2, "The system cannot find the file specified")
        error.winerror = 2
        raise error

    with pytest.raises(OSError) as excinfo:
        proc.spawn_detached(["child"], popen=fake_popen)

    assert excinfo.value.winerror == 2


_SURVIVOR_CHILD = """
import json
import sys
import time
from pathlib import Path

from brigade import aboyeur

output_dir = Path(sys.argv[1])
(output_dir / "run.json").write_text(json.dumps({"status": "started"}) + "\\n")
(output_dir / "child-alive").write_text("1")
deadline = time.monotonic() + 30
while time.monotonic() < deadline:
    if not (output_dir / "parent-alive").exists():
        break
    time.sleep(0.05)
aboyeur.record_run_termination(
    output_dir,
    status="failed",
    failure_phase=None,
    failure_kind="session-teardown",
    detail="parent session ended",
)
"""

_SURVIVOR_PARENT = """
import sys
import time
from pathlib import Path

from brigade import proc

output_dir = Path(sys.argv[1])
child_script = sys.argv[2]
child, mode = proc.spawn_detached([sys.executable, child_script, str(output_dir)])
(output_dir / "detach-mode").write_text(mode)
(output_dir / "child.pid").write_text(str(child.pid))
time.sleep(300)
"""


@pytest.mark.skipif(os.name != "posix", reason="POSIX session teardown semantics")
def test_spawn_detached_child_survives_parent_session_exit(tmp_path):
    """The detached child must reach a terminal receipt after its parent's session dies.

    Killing the parent's whole process group is what an SSH disconnect does.
    A child launched without its own session dies with that signal; a detached
    one keeps running and finishes writing run.json.
    """

    output_dir = tmp_path / "run"
    output_dir.mkdir()
    (output_dir / "parent-alive").write_text("1")
    child_script = tmp_path / "survivor_child.py"
    child_script.write_text(_SURVIVOR_CHILD)
    parent_script = tmp_path / "survivor_parent.py"
    parent_script.write_text(_SURVIVOR_PARENT)

    parent = subprocess.Popen(
        [sys.executable, str(parent_script), str(output_dir), str(child_script)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not (output_dir / "child-alive").exists():
            if parent.poll() is not None:
                raise AssertionError(f"parent exited early: {parent.stderr.read().decode()}")
            time.sleep(0.05)
        assert (output_dir / "child-alive").exists(), "detached child never started"
        child_pid = int((output_dir / "child.pid").read_text())
        assert (output_dir / "detach-mode").read_text() == proc.DETACH_MODE_POSIX_SESSION
        assert os.getpgid(child_pid) != os.getpgid(parent.pid)

        # Tear down the parent's session the way an SSH disconnect does.
        os.killpg(os.getpgid(parent.pid), signal.SIGKILL)
        parent.wait(timeout=30)
        (output_dir / "parent-alive").unlink()

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            payload = json.loads((output_dir / "run.json").read_text())
            if payload.get("finished_at"):
                break
            time.sleep(0.05)
        else:
            raise AssertionError("detached child never reached a terminal run receipt")

        assert payload["status"] == "failed"
        assert payload["failure"]["kind"] == "session-teardown"

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and _pid_is_running(child_pid):
            time.sleep(0.05)
        assert not _pid_is_running(child_pid), "detached child never exited on its own"
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=30)
        if parent.stderr is not None:
            parent.stderr.close()
