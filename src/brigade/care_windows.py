"""Windows scheduler actions without shell parsing of runbook arguments."""

from __future__ import annotations

import base64
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from .care_launchers import runbook_argv
from .isolated_exec import trusted_cwd

if TYPE_CHECKING:
    from .care_cmd import CareEntry

CMD_EXE = r"C:\Windows\System32\cmd.exe"
POWERSHELL_EXE = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
SCHTASKS_EXE = r"C:\Windows\System32\schtasks.exe"


def task_run(entry: CareEntry, *, workspace: Path) -> str:
    if entry.kind == "runbook":
        assert entry.runbook_rel is not None
        return subprocess.list2cmdline(runbook_argv(entry.runbook_rel, workspace))
    workspace_text = os.fspath(workspace).replace("/", "\\")
    cd_target = f'\\"{workspace_text}\\"' if " " in workspace_text or "\t" in workspace_text else workspace_text
    assert entry.shell is not None
    return f"{CMD_EXE} /c cd /d {cd_target} && {entry.shell}"


def create_command(entry: CareEntry, *, command: str, name: str, schedule: str, account: str) -> str:
    if entry.kind != "runbook":
        return f'schtasks /Create /TN "{name}" /TR "{command}" {schedule} {account} /F'

    # schtasks /TR is limited to 261 characters and passes through cmd quoting.
    # Keep its schedule/account translation, then replace the inert action via
    # COM's separate executable, arguments, and working-directory fields.
    executable = sys.executable
    arguments = command.removeprefix(subprocess.list2cmdline([executable])).lstrip()

    def literal(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    account = account.replace('"%USERNAME%"', "$env:USERNAME")
    script = (
        "$ErrorActionPreference='Stop'; "
        f"& {literal(SCHTASKS_EXE)} /Create /TN {literal(name)} "
        f"/TR {literal(CMD_EXE + ' /d /c exit 1')} {schedule} {account} /F; "
        "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }; "
        "$service=New-Object -ComObject Schedule.Service; $service.Connect(); "
        "$folder=$service.GetFolder('\\'); "
        f"$definition=$folder.GetTask({literal(name)}).Definition; "
        "$action=$definition.Actions.Item(1); "
        f"$action.Path={literal(executable)}; $action.Arguments={literal(arguments)}; "
        f"$action.WorkingDirectory={literal(str(trusted_cwd()))}; "
        f"$folder.RegisterTaskDefinition({literal(name)},$definition,6,$null,$null,3,$null) | Out-Null"
    )
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    # A bare absolute executable path parses in both cmd and PowerShell 5.1.
    return f"{POWERSHELL_EXE} -NoProfile -NonInteractive -EncodedCommand {encoded}"
