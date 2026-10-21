"""Windows scheduled runbooks must pin Python, imports, and target paths."""

import dataclasses
import csv
import json
import os
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from brigade import care_cmd


def test_schtasks_runbook_pins_interpreter_and_absolute_paths(tmp_path):
    workspace = tmp_path / "workspace with spaces & symbols %literal% ! ^ (test)"
    workspace.mkdir()
    entry = care_cmd.CARE_ENTRIES[0]
    command = care_cmd._schtasks_task_run(entry, workspace=workspace)
    assert "-I" in command, "scheduled runbook still resolves a workspace brigade executable"
    assert sys.executable in command
    assert str(workspace / entry.runbook_rel) in command
    assert str(workspace) in command
    assert "--target ." not in command
    assert "cmd.exe /c" not in command


@pytest.mark.skipif(sys.platform != "win32", reason="requires native Windows Task Scheduler")
@pytest.mark.parametrize("shell", ["cmd", "powershell"])
def test_native_schtasks_roundtrip_runs_isolated_approved_runbook(tmp_path, monkeypatch, capsys, shell):
    workspace = tmp_path / "workspace spaces & literal%pct% ! ^ (test)"
    workspace.mkdir()
    # Elevated pytest temp ACLs can exclude the scheduler's filtered user
    # token. Grant only that user Modify access to this disposable workspace.
    system32 = Path(os.environ["SystemRoot"]) / "System32"
    identity = subprocess.check_output([str(system32 / "whoami.exe"), "/user", "/fo", "csv", "/nh"], text=True)
    sid = next(csv.reader(identity.splitlines()))[1]
    subprocess.run(
        [str(system32 / "icacls.exe"), str(workspace), "/grant", f"*{sid}:(OI)(CI)M"],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    marker = tmp_path / "shadow-executed"
    payload = f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
    (workspace / "brigade.py").write_text(payload)
    for suffix in ("cmd", "bat"):
        (workspace / f"brigade.{suffix}").write_text(f'@echo shadow>"{marker}"\r\n@exit /b 0\r\n')
    shutil.copyfile(Path(os.environ["SystemRoot"]) / "System32/where.exe", workspace / "brigade.exe")
    monkeypatch.setenv("PYTHONPATH", str(workspace))
    entry = dataclasses.replace(care_cmd.CARE_ENTRIES[0], entry_id="isolation-" + tmp_path.name[-24:])
    approved = workspace / entry.runbook_rel
    approved.parent.mkdir(parents=True)
    approved.write_text(
        json.dumps(
            {
                "id": "windows-isolated-care",
                "allowed_commands": ["echo"],
                "steps": [{"id": "probe", "run": "echo scheduler-target"}],
            }
        )
    )
    assert care_cmd._windows_install(workspace=workspace, entries=(entry,), json_output=False, dry_run=True) == 3
    printed = capsys.readouterr().out
    command = next(line for line in printed.splitlines() if "-EncodedCommand " in line)
    batch = tmp_path / "create-care.cmd"
    batch.write_text(command + "\r\n", encoding="utf-8")
    create_argv = (
        [str(batch)]
        if shell == "cmd"
        else [
            str(system32 / "WindowsPowerShell/v1.0/powershell.exe"),
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            command,
        ]
    )
    create = subprocess.run(create_argv, capture_output=True, text=True, timeout=30)
    try:
        assert create.returncode == 0, create.stdout + create.stderr
        query = subprocess.run(
            ["schtasks", "/Query", "/TN", care_cmd._schtasks_task_name(entry), "/XML"],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
        root = ET.fromstring(query.stdout)
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        executable = root.findtext(".//t:Exec/t:Command", namespaces=ns)
        arguments = root.findtext(".//t:Exec/t:Arguments", namespaces=ns)
        assert Path(executable).resolve() == Path(sys.executable).resolve()
        assert "-I" in arguments
        # Execute exactly the registered action with hostile ambient state.
        action = subprocess.list2cmdline([executable]) + " " + arguments
        result = subprocess.run(action, cwd=workspace, capture_output=True, text=True, timeout=30)
        assert not marker.exists(), "scheduled action executed a workspace shadow"
        assert result.returncode == 0, result.stdout + result.stderr
        receipts = list((workspace / ".brigade/runbooks/runs").glob("*/receipt.json"))
        assert len(receipts) == 1
        receipt = json.loads(receipts[0].read_text())
        assert receipt["status"] == "completed"
        assert receipt["target"] == str(workspace)
        assert receipt["runbook_path"] == str(approved)
        subprocess.run(
            ["schtasks", "/Run", "/TN", care_cmd._schtasks_task_name(entry)], check=True, capture_output=True
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            receipts = list((workspace / ".brigade/runbooks/runs").glob("*/receipt.json"))
            if len(receipts) >= 2:
                break
            time.sleep(0.1)
        assert len(receipts) == 2, "native scheduler failed to run its stored action"
        assert not marker.exists()
        assert all(json.loads(path.read_text())["status"] == "completed" for path in receipts)
    finally:
        subprocess.run(["schtasks", "/Delete", "/TN", care_cmd._schtasks_task_name(entry), "/F"], capture_output=True)
