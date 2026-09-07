"""Roster doctor must bound hanging health probes (issue #1481).

A probe subprocess that never exits (a wedged agent CLI behind a .ps1 shim
or an npm wrapper on Windows) must not stall ``brigade roster doctor`` and
must not be orphaned: every probe runs through ``brigade.proc`` under an
explicit timeout, the timeout surfaces as a distinct ``timeout`` health
value, and the whole probe process tree is terminated.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from brigade import model_inventory, roster_cmd


def _hanging_command(pid_file: Path) -> list[str]:
    # A small Python child that sleeps, plus a grandchild that sleeps longer:
    # killing only the direct child would orphan the grandchild, which is the
    # original Windows-fleet failure. argv[0] identifies the probe child so
    # the test can assert the exact process is gone after doctor returns.
    code = (
        "import subprocess, sys, time; "
        "grandchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)']); "
        f"open({str(pid_file)!r}, 'w').write(str(grandchild.pid)); "
        "time.sleep(120)"
    )
    return [sys.executable, "-c", code]


def _write_hanging_cursor_roster(tmp_target: Path, pid_file: Path) -> None:
    command = _hanging_command(pid_file)
    command_toml = "[" + ", ".join(json.dumps(part) for part in command) + "]"
    path = tmp_target / ".brigade" / "roster.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        'orchestrator = "chef"\n'
        "[agents.chef]\n"
        'cli = "cursor"\n'
        'model = "composer-2.5"\n'
        'role = "plan"\n'
        f"command = {command_toml}\n"
    )


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def test_roster_doctor_bounds_hanging_probe_and_reaps_its_tree(monkeypatch, tmp_target, tmp_path, capsys):
    # Keep the developer machine's real user roster out of the probe path.
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "empty-home")
    pid_file = tmp_path / "grandchild.pid"
    _write_hanging_cursor_roster(tmp_target, pid_file)
    # The suite pins proc.which to a bare host; resolve only the injected
    # hanging probe command so detection sees a runnable executable.
    from brigade import agents

    monkeypatch.setattr(agents.proc, "which", lambda cmd: cmd if cmd == sys.executable else None)

    spawned: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen

    def spy_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        spawned.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", spy_popen)

    started = time.monotonic()
    rc = roster_cmd.doctor(tmp_target)
    elapsed = time.monotonic() - started
    out = capsys.readouterr().out

    # The hung inventory probe is advisory: doctor lists every seat and exits
    # fine, well within the 60-second acceptance bound for a hanging agent CLI,
    # reporting the distinct timeout state rather than blocking or raising.
    assert rc == 0
    assert elapsed < 60
    assert "agent: chef model inventory" in out
    assert "model inventory  timeout:" in out

    probe_children = [process for process in spawned if process.args and list(process.args)[0] == sys.executable]
    assert probe_children, "expected the hanging probe command to be spawned"
    for process in probe_children:
        assert process.poll() is not None, "hanging probe child must be reaped"
        if os.name == "posix":
            assert not _pid_alive(process.pid), "hanging probe child must not survive"
    if os.name == "posix":
        # The grandchild outlives a direct-child-only kill: the probe tree
        # kill must reap it too, or it lingers like the fleet orphans.
        grandchild_pid = int(pid_file.read_text().strip())
        assert not _pid_alive(grandchild_pid), "probe grandchild must not be orphaned"


def test_hung_grok_inventory_surfaces_distinct_timeout_state(monkeypatch):
    from brigade import agents

    monkeypatch.setattr(
        model_inventory.proc,
        "run",
        lambda argv, **kwargs: agents.proc.Result(124, "", "timeout after 15.0s"),
    )

    result = model_inventory.ModelInventoryInspector().inspect("grok", "grok-4.5")

    assert result is not None
    assert result.state == "timeout"
    assert "timed out" in result.detail
