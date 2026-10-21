"""Windows scheduled runbooks must pin Python, imports, and target paths."""

import base64
import csv
import dataclasses
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import pytest

from brigade import care_cmd


@pytest.mark.parametrize("query_fails", [False, True])
def test_scheduler_diagnostics_never_discloses_query_output(tmp_path, monkeypatch, query_fails):
    private = r"C:\hostedtoolcache\windows\Python\3.12.10\x64"
    sid = "S-1-5-21-111111111-222222222-333333333-1001"
    keys = []
    entropy = secrets.token_bytes

    def fresh_key(size):
        key = entropy(size)
        keys.append(key)
        return key

    def query(*args, **kwargs):
        invocation = json.dumps([args, kwargs])
        for key in keys:
            assert key.hex() not in invocation
            assert base64.b64encode(key).decode() not in invocation
        payload = json.dumps(
            {
                "task": {"last_result": 2147942405},
                "identity": sid,
                "identity_again": sid,
                "path": private,
                "path_again": private,
                "common_digest": hashlib.sha256(sid.encode()).hexdigest(),
                "apparently_prehashed_sha256": hashlib.sha256(sid.encode()).hexdigest(),
            }
        )
        if query_fails:
            raise subprocess.TimeoutExpired(private, 12, output=payload + "\n" + sid, stderr=private)
        return subprocess.CompletedProcess(args[0], 0, payload, private)

    monkeypatch.setattr(secrets, "token_bytes", fresh_key)
    monkeypatch.setattr(subprocess, "run", query)
    reports = [
        _scheduler_diagnostics(
            system32=tmp_path,
            task_name="BrigadeCare-diagnostic-fixture",
            workspace=tmp_path,
            executable=sys.executable,
            arguments="-I -c private-bootstrap",
            working_directory=tmp_path,
            timeline=[],
        )
        for _ in range(2)
    ]
    for report in reports:
        rendered = json.dumps(report)
        assert json.dumps(private)[1:-1] not in rendered
        assert sid not in rendered
        assert report["identity"] == report["identity_again"]
        assert report["path"] == report["path_again"]
        assert report["task"]["last_result"] == 2147942405
        assert report["query_stderr_sha256"] == report["path"]["redacted_sha256"]
        if query_fails:
            assert report["error_type"] == "TimeoutExpired"
    assert reports[0]["identity"] != reports[1]["identity"], "identifiers must be unlinkable between reports"
    assert reports[0]["path"] != reports[1]["path"]
    assert reports[0]["query_stdout_sha256"] != reports[1]["query_stdout_sha256"]
    assert reports[0]["query_stderr_sha256"] != reports[1]["query_stderr_sha256"]
    assert len(keys) == 2
    assert all(len(key) == 32 for key in keys)
    rendered = json.dumps(reports)
    for value in (private, sid):
        assert hashlib.sha256(value.encode()).hexdigest() not in rendered
    for key in keys:
        assert key.hex() not in rendered
        assert base64.b64encode(key).decode() not in rendered


def _scheduler_diagnostics(*, system32, task_name, workspace, executable, arguments, working_directory, timeline):
    """Read-only, bounded probes. Never expose raw command output or identifiers."""

    def literal(value):
        return "'" + str(value).replace("'", "''") + "'"

    script = (
        "\n".join(
            f"${name}={literal(value)}"
            for name, value in {
                "taskName": task_name,
                "workspace": workspace,
                "expectedExe": executable,
                "expectedArgs": arguments,
                "expectedCwd": working_directory,
                "package": Path(care_cmd.__file__).parent,
            }.items()
        )
        + r"""
$ErrorActionPreference='Stop'
$out=@{}
# Both entropy and the HMAC key stay inside this collector process.
$key=New-Object byte[] 32
$rng=[Security.Cryptography.RandomNumberGenerator]::Create()
try {$rng.GetBytes($key)} finally {$rng.Dispose()}
$hasher=[Security.Cryptography.HMACSHA256]::new($key)
[Array]::Clear($key,0,$key.Length)
function Hash($s) {
    ([BitConverter]::ToString($hasher.ComputeHash([Text.Encoding]::UTF8.GetBytes([string]$s)))).Replace('-','').ToLower()
}
function Probe($name, [scriptblock]$block) {
    try { $out[$name]=& $block }
    catch { $out[$name]=@{error_type=$_.Exception.GetType().FullName;hresult=$_.Exception.HResult} }
    @{$name=$out[$name]} | ConvertTo-Json -Depth 10 -Compress
}
$engines=@()
Probe 'task' {
    $service=New-Object -ComObject Schedule.Service
    $service.Connect()
    $task=$service.GetFolder('\').GetTask($taskName)
    $definition=$task.Definition
    $action=$definition.Actions.Item(1)
    $principalSid=$null
    try {
        $account=New-Object Security.Principal.NTAccount($definition.Principal.UserId)
        $principalSid=$account.Translate([Security.Principal.SecurityIdentifier]).Value
    } catch {}
    $instances=@($task.GetInstances(0) | ForEach-Object {
        $script:engines+= $_.EnginePID
        @{pid=$_.EnginePID;state=$_.State;instance_sha256=(Hash $_.InstanceGuid)}
    })
    @{
        last_result=$task.LastTaskResult;state=$task.State
        last_run_utc=$task.LastRunTime.ToUniversalTime().ToString('o')
        next_run_utc=$task.NextRunTime.ToUniversalTime().ToString('o')
        missed_runs=$task.NumberOfMissedRuns;instances=$instances
        principal_sha256=(Hash $definition.Principal.UserId)
        principal_sid_sha256=$(if ($principalSid) {Hash $principalSid} else {$null})
        logon_type=$definition.Principal.LogonType;run_level=$definition.Principal.RunLevel
        executable_sha256=(Hash $action.Path);arguments_sha256=(Hash $action.Arguments)
        cwd_sha256=(Hash $action.WorkingDirectory)
        executable_matches_registered=($action.Path -ceq $expectedExe)
        arguments_match_registered=($action.Arguments -ceq $expectedArgs)
        cwd_matches_registered=($action.WorkingDirectory -ceq $expectedCwd)
        isolated=($action.Arguments -match '^-I\s+-c\s')
        enabled=$definition.Settings.Enabled;allow_demand_start=$definition.Settings.AllowDemandStart
        start_when_available=$definition.Settings.StartWhenAvailable
        disallow_battery=$definition.Settings.DisallowStartIfOnBatteries
        stop_on_battery=$definition.Settings.StopIfGoingOnBatteries
        multiple_instances=$definition.Settings.MultipleInstances
    }
}
Probe 'token' {
    $id=[Security.Principal.WindowsIdentity]::GetCurrent()
    $principal=New-Object Security.Principal.WindowsPrincipal($id)
    $groups=@($id.Groups | ForEach-Object { Hash $_.Value })
    $integrity=@(& "$env:SystemRoot\System32\whoami.exe" /groups /fo csv /nh |
        ConvertFrom-Csv -Header Name,Type,Sid,Attributes | Where-Object {$_.Sid -like 'S-1-16-*'} |
        ForEach-Object {[int]($_.Sid.Split('-')[-1])})
    @{
        sid_sha256=(Hash $id.User.Value);owner_sha256=(Hash $id.Owner.Value)
        groups_sha256=$groups;integrity_rid=$integrity
        session_id=[Diagnostics.Process]::GetCurrentProcess().SessionId
        elevated_admin=$principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
        authenticated=$id.IsAuthenticated;system=$id.IsSystem;user_interactive=[Environment]::UserInteractive
    }
}
Probe 'access' {
    # These are effective probes for THIS token, not an impersonation of the
    # scheduler token. Retain DACLs for comparison to its principal and groups.
    $paths=@(@{role='workspace';path=$workspace},@{role='interpreter';path=$expectedExe},
        @{role='package';path=$package},@{role='initializer';path=(Join-Path $package '__init__.py')})
    foreach($source in @(@{role='workspace';path=$workspace},
        @{role='interpreter';path=$expectedExe},@{role='package';path=$package})) {
        $parent=[IO.Directory]::GetParent($source.path)
        for ($i=0;$null -ne $parent -and $i -lt 12;$i++) {
            $paths+=@{role=($source.role+"-parent-$i");path=$parent.FullName};$parent=$parent.Parent
        }
    }
    @($paths | ForEach-Object {
        $row=@{role=$_.role;path_sha256=(Hash $_.path);access_token='current-test-token'}
        try {
            $item=Get-Item -LiteralPath $_.path -Force
            $acl=Get-Acl -LiteralPath $_.path
            $row.owner_sha256=Hash $acl.Owner
            $row.rules=@($acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier]) |
                Select-Object -First 32 | ForEach-Object {
                    @{sid_sha256=(Hash $_.IdentityReference.Value);rights=[int]$_.FileSystemRights
                        type=[int]$_.AccessControlType;inherited=$_.IsInherited
                        inheritance=[int]$_.InheritanceFlags;propagation=[int]$_.PropagationFlags}
                })
            if ($item.PSIsContainer) {
                $reader=[IO.Directory]::EnumerateFileSystemEntries($_.path).GetEnumerator()
                try { $null=$reader.MoveNext();$row.directory_read=$true }
                finally { if ($reader -is [IDisposable]) {$reader.Dispose()} }
            } else {
                $reader=[IO.File]::Open($_.path,'Open','Read','ReadWrite')
                $reader.Dispose();$row.file_read=$true
            }
        } catch {$row.error_type=$_.Exception.GetType().FullName;$row.hresult=$_.Exception.HResult}
        $row
    })
}
Probe 'events' {
    @(Get-WinEvent -FilterHashtable @{LogName='Microsoft-Windows-TaskScheduler/Operational';
        StartTime=(Get-Date).AddMinutes(-3)} -MaxEvents 128 | ForEach-Object {
        $xml=[xml]$_.ToXml()
        $data=@{};foreach($d in $xml.Event.EventData.Data) {$data[[string]$d.Name]=[string]$d.'#text'}
        if ($data['TaskName'] -eq ('\'+$taskName)) {
            $row=@{event_id=$_.Id;record_id=$_.RecordId;utc=$_.TimeCreated.ToUniversalTime().ToString('o')}
            foreach($key in @('ResultCode','ProcessID','EnginePID','InstanceId','ActionName')) {
                if ($data.ContainsKey($key)) {
                    $number=0L
                    if ([long]::TryParse($data[$key],[ref]$number)) {$row[$key]=$number}
                    else {$row[$key+'_sha256']=Hash $data[$key]}
                }
            }
            $row
        }
    })
}
Probe 'processes' {
    # Command lines, environments and owners are deliberately never queried.
    $all=@(Get-CimInstance -Query 'SELECT ProcessId,ParentProcessId,Name,SessionId,ExecutablePath,CreationDate FROM Win32_Process')
    $wanted=@([int]$PID)+@($engines)
    $descendants=$wanted
    for($i=0;$i -lt 3;$i++) {
        $next=@($all | Where-Object {$_.ParentProcessId -in $descendants} | Select-Object -First 32 |
            ForEach-Object {[int]$_.ProcessId})
        $wanted+=$next;$descendants=$next
    }
    $ancestor=[int]$PID
    for($i=0;$i -lt 8;$i++) {
        $p=$all | Where-Object {$_.ProcessId -eq $ancestor} | Select-Object -First 1
        if ($null -eq $p -or $p.ParentProcessId -eq 0) {break}
        $ancestor=[int]$p.ParentProcessId;$wanted+=$ancestor
    }
    @($all | Where-Object {$_.ProcessId -in $wanted} | Select-Object -First 48 | ForEach-Object {
        $image=[string]$_.Name
        if ($image -notin @('python.exe','powershell.exe','pwsh.exe','cmd.exe','taskhostw.exe',
            'taskeng.exe','schtasks.exe','conhost.exe','OpenConsole.exe','sshd.exe',
            'wsmprovhost.exe','Runner.Worker.exe','Runner.Listener.exe','services.exe')) {$image='other'}
        @{pid=$_.ProcessId;parent_pid=$_.ParentProcessId;session_id=$_.SessionId;image_kind=$image
            image_sha256=(Hash $_.Name);executable_sha256=(Hash $_.ExecutablePath)
            is_probe=($_.ProcessId -eq $PID);is_task_engine=($_.ProcessId -in $engines)
            created_utc=$_.CreationDate.ToUniversalTime().ToString('o')}
    })
}
$hasher.Dispose()
"""
    )

    # A separate fresh key also protects partial/error output and unknown
    # strings in the Python boundary. Neither key is an input or output.
    private_key = secrets.token_bytes(32)

    def private_digest(value):
        if isinstance(value, str):
            value = value.encode()
        return hmac.new(private_key, value or b"", hashlib.sha256).hexdigest()

    def sanitize(value, key=""):
        if isinstance(value, dict):
            return {k: sanitize(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [sanitize(v, key) for v in value]
        if isinstance(value, str):
            if key.endswith("_sha256") and re.fullmatch(r"[0-9a-f]{64}", value):
                return private_digest(value)
            if key.endswith("utc") and re.fullmatch(r"[0-9T:.+Z-]+", value):
                return value
            if key == "role" and re.fullmatch(
                r"(?:workspace|interpreter|package)(?:-parent-[0-9]+)?|initializer", value
            ):
                return value
            if key == "image_kind" and value in {
                "python.exe",
                "powershell.exe",
                "pwsh.exe",
                "cmd.exe",
                "taskhostw.exe",
                "taskeng.exe",
                "schtasks.exe",
                "conhost.exe",
                "OpenConsole.exe",
                "sshd.exe",
                "wsmprovhost.exe",
                "Runner.Worker.exe",
                "Runner.Listener.exe",
                "services.exe",
                "other",
            }:
                return value
            if key == "access_token" and value == "current-test-token":
                return value
            if key == "error_type" and re.fullmatch(r"System\.[A-Za-z.]+", value):
                return value
            return {"redacted_sha256": private_digest(value)}
        return value

    report = {"timeline": timeline, "collector_pid": os.getpid()}

    def collect(output):
        if isinstance(output, bytes):
            output = output.decode("utf-8", "replace")
        for line in (output or "").splitlines():
            try:
                section = json.loads(line)
            except ValueError:
                continue
            if isinstance(section, dict):
                report.update(sanitize(section))

    try:
        query = subprocess.run(
            [
                str(system32 / "WindowsPowerShell/v1.0/powershell.exe"),
                "-NoProfile",
                "-NonInteractive",
                "-EncodedCommand",
                base64.b64encode(script.encode("utf-16le")).decode("ascii"),
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        report["query_exit"] = query.returncode
        collect(query.stdout)
        report["query_stdout_sha256"] = private_digest(query.stdout)
        # Failed queries can echo arguments, accounts or paths. Hash only.
        report["query_stderr_sha256"] = private_digest(query.stderr)
    except subprocess.TimeoutExpired as exc:
        collect(exc.stdout)
        report["query_stdout_sha256"] = private_digest(exc.stdout)
        report["query_stderr_sha256"] = private_digest(exc.stderr)
        report["error_type"] = type(exc).__name__
    except Exception as exc:
        report["error_type"] = type(exc).__name__
    return report


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
        timeline = [{"phase": "run-request", "utc": datetime.now(timezone.utc).isoformat()}]
        subprocess.run(
            ["schtasks", "/Run", "/TN", care_cmd._schtasks_task_name(entry)], check=True, capture_output=True
        )
        timeline.append({"phase": "run-returned", "utc": datetime.now(timezone.utc).isoformat()})
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            receipts = list((workspace / ".brigade/runbooks/runs").glob("*/receipt.json"))
            timeline.append(
                {"phase": "receipt-count", "utc": datetime.now(timezone.utc).isoformat(), "count": len(receipts)}
            )
            if len(receipts) >= 2:
                break
            time.sleep(0.1)
        if len(receipts) != 2:
            timeline.append(
                {"phase": "poll-ended", "utc": datetime.now(timezone.utc).isoformat(), "count": len(receipts)}
            )
            # --tb=line omits captured sections in the hosted driver. Emit only
            # sanitized diagnostics outside capture, before deleting the task.
            try:
                report = _scheduler_diagnostics(
                    system32=system32,
                    task_name=care_cmd._schtasks_task_name(entry),
                    workspace=workspace,
                    executable=executable,
                    arguments=arguments,
                    working_directory=root.findtext(".//t:Exec/t:WorkingDirectory", namespaces=ns),
                    timeline=timeline,
                )
            except Exception as exc:
                report = {"error_type": type(exc).__name__}
            try:
                with capsys.disabled():
                    print("SCHEDULER_DIAGNOSTICS " + json.dumps(report), file=sys.stderr, flush=True)
            except Exception:
                # Even a broken diagnostic output stream must not replace the
                # original scheduler assertion below.
                pass
        assert len(receipts) == 2, "native scheduler failed to run its stored action"
        assert not marker.exists()
        assert all(json.loads(path.read_text())["status"] == "completed" for path in receipts)
    finally:
        subprocess.run(["schtasks", "/Delete", "/TN", care_cmd._schtasks_task_name(entry), "/F"], capture_output=True)
