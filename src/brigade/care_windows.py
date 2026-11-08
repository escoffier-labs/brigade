"""Windows scheduler actions without shell parsing of runbook arguments."""

from __future__ import annotations

import base64
import os
import json
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from .care_launchers import entry_argv
from .isolated_exec import module_argv, trusted_cwd

if TYPE_CHECKING:
    from .care_cmd import CareEntry

CMD_EXE = r"C:\Windows\System32\cmd.exe"
POWERSHELL_EXE = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
SCHTASKS_EXE = r"C:\Windows\System32\schtasks.exe"


def split_windows_argv(command: str) -> list[str]:
    """Decode the native quoting emitted by subprocess.list2cmdline."""
    result = []
    index = 0
    while index < len(command):
        while index < len(command) and command[index] in " \t":
            index += 1
        if index == len(command):
            break
        word = []
        quoted = False
        while index < len(command):
            slashes = 0
            while index < len(command) and command[index] == "\\":
                slashes += 1
                index += 1
            if index < len(command) and command[index] == '"':
                word.append("\\" * (slashes // 2))
                if slashes % 2:
                    word.append('"')
                elif quoted and index + 1 < len(command) and command[index + 1] == '"':
                    word.append('"')
                    index += 1
                else:
                    quoted = not quoted
                index += 1
            else:
                word.append("\\" * slashes)
                if index == len(command) or (command[index] in " \t" and not quoted):
                    break
                word.append(command[index])
                index += 1
        if quoted:
            raise ValueError("invalid existing task quoting")
        result.append("".join(word))
    return result


def preserve_scope_arguments(desired: str, existing: str) -> str:
    from .memory_hub_client import valid_scope

    argv = split_windows_argv(existing)
    if "--scope" not in argv:
        return desired
    if argv.count("--scope") != 1:
        raise ValueError("ambiguous existing reflect scope")
    index = argv.index("--scope")
    if index + 1 == len(argv) or not valid_scope(argv[index + 1]):
        raise ValueError("invalid existing reflect scope")
    return desired + " " + subprocess.list2cmdline(["--scope", argv[index + 1]])


def task_run(entry: CareEntry, *, workspace: Path) -> str:
    if entry.kind in ("runbook", "reflect"):
        return subprocess.list2cmdline(entry_argv(entry, workspace))
    workspace_text = os.fspath(workspace).replace("/", "\\")
    cd_target = f'\\"{workspace_text}\\"' if " " in workspace_text or "\t" in workspace_text else workspace_text
    assert entry.shell is not None
    return f"{CMD_EXE} /c cd /d {cd_target} && {entry.shell}"


def create_command(entry: CareEntry, *, command: str, name: str, schedule: str, account: str) -> str:
    if entry.kind not in ("runbook", "reflect"):
        return f'schtasks /Create /TN "{name}" /TR "{command}" {schedule} {account} /F'

    # schtasks /TR is limited to 261 characters and passes through cmd quoting.
    # Keep its schedule/account translation, then replace the inert action via
    # COM's separate executable, arguments, and working-directory fields.
    executable = sys.executable
    arguments = command.removeprefix(subprocess.list2cmdline([executable])).lstrip()

    def literal(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    account = account.replace('"%USERNAME%"', "$env:USERNAME")
    prepare = ""
    if entry.kind == "reflect" and entry.reflect_scope is None:
        helper = module_argv("brigade.care_windows")
        invocation = "& " + " ".join(literal(arg) for arg in helper)
        prepare = (
            "$existing=$null; "
            f"try {{ $existing=$folder.GetTask({literal(name)}) }} "
            "catch { $failure=$_.Exception; "
            "while ($null -ne $failure.InnerException) { $failure=$failure.InnerException }; "
            "if ($failure.HResult -ne -2147024894) { throw } }; "
            "if ($null -ne $existing) { "
            "$actions=$existing.Definition.Actions; "
            "if ($actions.Count -ne 1 -or $actions.Item(1).Type -ne 0) { throw 'invalid existing reflect action' }; "
            "$payload=ConvertTo-Json -InputObject @($newArguments,($actions.Item(1).Arguments)) -Compress; "
            "$payload64=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($payload)); "
            f"$result=$payload64 | {invocation}; "
            "if ($LASTEXITCODE -ne 0) { throw 'cannot preserve existing reflect scope' }; "
            "$newArguments=($result | ConvertFrom-Json) }; "
        )
    script = (
        "$ErrorActionPreference='Stop'; "
        "$service=New-Object -ComObject Schedule.Service; $service.Connect(); "
        "$folder=$service.GetFolder('\\'); "
        f"$newArguments={literal(arguments)}; " + prepare + f"& {literal(SCHTASKS_EXE)} /Create /TN {literal(name)} "
        f"/TR {literal(CMD_EXE + ' /d /c exit 1')} {schedule} {account} /F; "
        "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }; "
        f"$definition=$folder.GetTask({literal(name)}).Definition; "
        "$action=$definition.Actions.Item(1); "
        f"$action.Path={literal(executable)}; $action.Arguments=$newArguments; "
        f"$action.WorkingDirectory={literal(str(trusted_cwd()))}; "
        f"$folder.RegisterTaskDefinition({literal(name)},$definition,6,$null,$null,3,$null) | Out-Null"
    )
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    # A bare absolute executable path parses in both cmd and PowerShell 5.1.
    return f"{POWERSHELL_EXE} -NoProfile -NonInteractive -EncodedCommand {encoded}"


if __name__ == "__main__":
    try:
        payload = sys.stdin.read(1024 * 1024 + 1)
        if len(sys.argv) != 1 or len(payload) > 1024 * 1024:
            raise ValueError("invalid task argument envelope")
        values = json.loads(base64.b64decode(payload.strip(), validate=True).decode("utf-8"))
        if not isinstance(values, list) or len(values) != 2 or not all(isinstance(value, str) for value in values):
            raise ValueError("invalid task argument envelope")
        print(json.dumps(preserve_scope_arguments(*values)))
    except ValueError:
        print("cannot preserve existing reflect scope", file=sys.stderr)
        raise SystemExit(2) from None
