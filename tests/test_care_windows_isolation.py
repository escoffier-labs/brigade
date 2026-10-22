"""Windows scheduled runbooks must pin Python, imports, and target paths."""

import base64
import csv
from contextlib import contextmanager
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


@pytest.mark.parametrize("observer_fails", [False, True])
def test_failed_scheduler_assertion_survives_diagnostic_replay(observer_fails):
    receipts = ["original receipt"]
    failure = AssertionError("original scheduler failure")

    def observe():
        receipts.append("replay receipt")
        if observer_fails:
            raise subprocess.TimeoutExpired("diagnostic", 15)

    with pytest.raises(AssertionError) as caught:
        with _preserve_scheduler_failure(observe):
            raise failure
    assert caught.value is failure
    assert len(receipts) == 2, "the replay ran but must never rescue the original assertion"
    with _preserve_scheduler_failure(lambda: pytest.fail("success must not trigger replay")):
        pass


def test_scheduler_replay_refuses_nonfixture_runbooks(tmp_path):
    approved = tmp_path / "runbook.json"
    approved.write_text(json.dumps({"steps": [{"run": "echo unapproved"}]}))
    with pytest.raises(ValueError, match="synthetic echo"):
        _scheduler_replay(
            system32=tmp_path,
            task_name="unused",
            workspace=tmp_path,
            approved=approved,
            executable=sys.executable,
            arguments="",
            working_directory=tmp_path,
        )


@pytest.mark.parametrize("query_times_out", [False, True])
def test_scheduler_replay_keeps_partial_output_private_and_reserves_cleanup(tmp_path, monkeypatch, query_times_out):
    from brigade.care_launchers import runbook_argv
    from brigade.isolated_exec import trusted_cwd

    approved = tmp_path / "synthetic.json"
    approved.write_text(
        json.dumps(
            {
                "id": "windows-isolated-care",
                "allowed_commands": ["echo"],
                "steps": [{"id": "probe", "run": "echo scheduler-target"}],
            }
        )
    )
    arguments = (
        subprocess.list2cmdline(runbook_argv(approved.name, tmp_path))
        .removeprefix(subprocess.list2cmdline([sys.executable]))
        .lstrip()
    )
    private = "S-1-5-21-111111111-222222222-333333333-1001"
    keys, calls = [], []
    entropy = secrets.token_bytes

    def key(size):
        value = entropy(size)
        keys.append(value)
        return value

    def query(argv, **kwargs):
        calls.append(kwargs["timeout"])
        if query_times_out:
            raise subprocess.TimeoutExpired(private, kwargs["timeout"], output=private, stderr=private)
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps(
                {
                    "unknown": private,
                    "again": private,
                    "apparently_prehashed": hashlib.sha256(private.encode()).hexdigest(),
                    **{
                        key: True
                        for key in (
                            "cleanup_confirmed",
                            "registered",
                            "ownership_verified_before_stop",
                            "ownership_verified_before_delete",
                            "wrapper_identity_verified",
                            "wrapper_signaled",
                            "child_identity_verified",
                            "child_signaled",
                            "all_instances_gone",
                            "task_absent_after_delete",
                        )
                    },
                }
            ),
            private,
        )

    monkeypatch.setattr(secrets, "token_bytes", key)
    monkeypatch.setattr(subprocess, "run", query)
    reports = [
        _scheduler_replay(
            system32=tmp_path,
            task_name="synthetic",
            workspace=tmp_path,
            approved=approved,
            executable=sys.executable,
            arguments=arguments,
            working_directory=trusted_cwd(),
        )
        for _ in range(2)
    ]
    assert len(calls) == 2
    assert all(0 < value <= 15 for value in calls)
    if query_times_out:
        assert all(report["controller_timeout"] and not report["cleanup_confirmed"] for report in reports)
        assert len(list(tmp_path.glob("BrigadeCare-diagnostic-*.ps1"))) == 2
    else:
        assert all(report["cleanup_confirmed"] for report in reports)
        assert not list(tmp_path.glob("BrigadeCare-diagnostic-*"))
        assert reports[0]["unknown"] == reports[0]["again"]
        assert reports[0]["unknown"] != reports[1]["unknown"]
    rendered = json.dumps(reports)
    assert private not in rendered
    assert hashlib.sha256(private.encode()).hexdigest() not in rendered
    for value in keys:
        assert value.hex() not in rendered
        assert base64.b64encode(value).decode() not in rendered


def _scheduled_extras_config(monkeypatch, workspace):
    from brigade import care_launchers
    from brigade.isolated_exec import module_argv, trusted_cwd

    config = (workspace / "private-config").resolve()
    with monkeypatch.context() as parent:
        parent.setenv("XDG_CONFIG_HOME", str(config))
        enabled = subprocess.run(
            module_argv("brigade", "extras", "on"), cwd=trusted_cwd(), capture_output=True, text=True, timeout=30
        )
    assert enabled.returncode == 0, "fixture extras opt-in failed"
    marker = config / "brigade/extras"
    assert marker.is_file()

    def configured_argv(*args, **kwargs):
        argv = module_argv(*args, **kwargs)
        # Scheduler does not inherit pytest's BRIGADE_EXTRAS=1. Locate only
        # this fixture's persistent opt-in, before the unchanged isolated
        # bootstrap. This is explicit test configuration, not product opt-in.
        argv[3] = f"import os;os.environ['XDG_CONFIG_HOME']={str(config)!r};" + argv[3]
        return argv

    monkeypatch.setattr(care_launchers, "module_argv", configured_argv)
    return marker


def test_scheduled_extras_configuration_does_not_escape_its_scope(tmp_path, monkeypatch):
    from brigade import care_launchers
    from brigade.isolated_exec import trusted_cwd

    monkeypatch.delenv("BRIGADE_EXTRAS", raising=False)
    empty_config = tmp_path / "unconfigured"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(empty_config))
    with monkeypatch.context() as scoped:
        _scheduled_extras_config(scoped, tmp_path / "scheduled-fixture")
        enabled = subprocess.run(
            care_launchers.module_argv("brigade", "runbook", "--help"),
            cwd=trusted_cwd(),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert enabled.returncode == 0 and "usage: brigade runbook" in enabled.stdout
    disabled = subprocess.run(
        care_launchers.module_argv("brigade", "runbook", "--help"),
        cwd=trusted_cwd(),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert disabled.returncode == 2 and "extras surface, which is disabled" in disabled.stderr
    assert os.environ["XDG_CONFIG_HOME"] == str(empty_config)
    assert "BRIGADE_EXTRAS" not in os.environ and not (empty_config / "brigade/extras").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="requires native Windows Task Scheduler")
@pytest.mark.parametrize("extras_on", [True, False], ids=["configured", "unconfigured"])
def test_native_scheduler_diagnostic_replay(monkeypatch, extras_on):
    from brigade.care_launchers import runbook_argv
    from brigade.isolated_exec import trusted_cwd

    # Inherit access from the disposable source checkout instead of changing
    # any ACL. The replay observation must not repair the state it measures.
    system32 = Path(os.environ["SystemRoot"]) / "System32"
    with _owned_replay_workspace(Path(__file__).resolve().parents[1]) as workspace:
        marker = _scheduled_extras_config(monkeypatch, workspace)
        if not extras_on:
            marker.unlink()
        entry = dataclasses.replace(care_cmd.CARE_ENTRIES[0], entry_id="replay-" + secrets.token_hex(8))
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
        # This is an independent disposable echo registration, not an operator task.
        from brigade.care_windows import create_command

        command = create_command(
            entry,
            command=care_cmd._schtasks_task_run(entry, workspace=workspace),
            name=care_cmd._schtasks_task_name(entry),
            schedule="/SC DAILY /ST 06:15",
            account='/RU "%USERNAME%" /IT',
        )
        powershell = str(system32 / "WindowsPowerShell/v1.0/powershell.exe")
        created = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            timeout=30,
        )
        try:
            assert created.returncode == 0
            before = subprocess.check_output(
                [str(system32 / "schtasks.exe"), "/Query", "/TN", care_cmd._schtasks_task_name(entry), "/XML"],
                text=True,
            )
            action = subprocess.list2cmdline(runbook_argv(entry.runbook_rel, workspace))
            arguments = action.removeprefix(subprocess.list2cmdline([sys.executable])).lstrip()
            report = _scheduler_replay(
                system32=system32,
                task_name=care_cmd._schtasks_task_name(entry),
                workspace=workspace,
                approved=approved,
                executable=sys.executable,
                arguments=arguments,
                working_directory=trusted_cwd(),
            )
            assert report["cleanup_confirmed"], report
            assert report["wrapper_signaled"] and report["child_signaled"] and report["all_instances_gone"], report
            assert report["task_absent_after_delete"], report
            assert report["elapsed_seconds"] <= 15, report
            assert report["wrapper_reached"] and report["process_started"], report
            assert "actual_exit" in report, report
            assert report["principal_matches"] and report["settings_match"], report
            assert report["principal_sid_matches"], report
            assert all(row["readable"] for row in report["read_probes"]), report
            if extras_on:
                assert report["actual_exit"] == 0, report
                assert not any(report["stderr_flags"].values()), report
                assert len(list((workspace / ".brigade/runbooks/runs").glob("*/receipt.json"))) == 1
            else:
                assert report["actual_exit"] == 2, report
                assert report["stderr_flags"]["extras_disabled"], report
                assert not list((workspace / ".brigade/runbooks/runs").glob("*/receipt.json"))
            after = subprocess.check_output(
                [str(system32 / "schtasks.exe"), "/Query", "/TN", care_cmd._schtasks_task_name(entry), "/XML"],
                text=True,
            )
            assert before == after, "diagnostics must not mutate the original registration"
            assert not list(workspace.glob("BrigadeCare-diagnostic-*"))
        finally:
            subprocess.run(
                [str(system32 / "schtasks.exe"), "/Delete", "/TN", care_cmd._schtasks_task_name(entry), "/F"],
                capture_output=True,
                timeout=15,
            )


@pytest.mark.skipif(sys.platform != "win32", reason="requires native Windows Task Scheduler")
def test_native_replay_absent_task_cannot_confirm_cleanup():
    from brigade.care_launchers import runbook_argv
    from brigade.isolated_exec import trusted_cwd

    with _owned_replay_workspace(Path(__file__).resolve().parents[1]) as workspace:
        approved = workspace / "echo.json"
        approved.write_text(
            json.dumps(
                {
                    "id": "windows-isolated-care",
                    "allowed_commands": ["echo"],
                    "steps": [{"id": "probe", "run": "echo scheduler-target"}],
                }
            )
        )
        action = subprocess.list2cmdline(runbook_argv(approved.name, workspace))
        arguments = action.removeprefix(subprocess.list2cmdline([sys.executable])).lstrip()
        report = _scheduler_replay(
            system32=Path(os.environ["SystemRoot"]) / "System32",
            task_name="BrigadeCare-absent-" + secrets.token_hex(16),
            workspace=workspace,
            approved=approved,
            executable=sys.executable,
            arguments=arguments,
            working_directory=trusted_cwd(),
        )
        assert report["cleanup_confirmed"] is False, "missing task is not process-termination proof"
        assert not report["registered"] and not report["stop_requested"] and not report["delete_requested"]
        # This source never existed, so registration/Run were never reached.
        # Explicitly remove only this test's inert scripts after that proof.
        for path in workspace.glob("BrigadeCare-diagnostic-*.ps1"):
            path.unlink()


@pytest.mark.skipif(sys.platform != "win32", reason="requires native Windows PowerShell 5.1")
def test_native_replay_capture_drains_both_streams_and_uses_private_keys(tmp_path):
    system32 = Path(os.environ["SystemRoot"]) / "System32"
    reports = []
    private = "S-1-5-21-111111111-222222222-333333333-1001"
    for index in range(2):
        output = tmp_path / f"result-{index}.json"
        script_path = tmp_path / f"capture-{index}.ps1"
        # Fixed synthetic child, no runbook or operator side effects. Both pipes
        # exceed OS buffer capacity; an implementation that drains serially hangs.
        code = (
            "import sys;sys.stdout.write('x'*131072);sys.stderr.write("
            + repr(private)
            + "+'x'*131072+'error: runbook policy failed');sys.exit(2)"
        )
        script_path.write_text(
            _replay_wrapper(sys.executable, subprocess.list2cmdline(["-I", "-c", code]), tmp_path, output),
            encoding="utf-8-sig",
        )
        try:
            result = subprocess.run(
                [
                    str(system32 / "WindowsPowerShell/v1.0/powershell.exe"),
                    "-NoProfile",
                    "-NonInteractive",
                    "-File",
                    str(script_path),
                ],
                capture_output=True,
                timeout=12,
            )
            assert result.returncode == 0
            report = json.loads(output.read_text(encoding="utf-8-sig"))
            assert report["actual_exit"] == 2, report
            assert report["stderr_flags"]["policy_failed"], report
            assert report["stdout_chars"] == 131072 and report["stderr_chars"] > 131072
            assert report["process_started"] and report["wrapper_reached"]
            rendered = json.dumps(report)
            assert private not in rendered and hashlib.sha256(private.encode()).hexdigest() not in rendered
            reports.append(report)
        finally:
            script_path.unlink(missing_ok=True)
            output.unlink(missing_ok=True)
    assert reports[0]["stderr_digest"] != reports[1]["stderr_digest"]


@pytest.mark.skipif(sys.platform != "win32", reason="requires native Windows PowerShell 5.1")
def test_native_replay_timeout_preserves_private_partial_report_and_stops_owned_child(tmp_path):
    import ctypes

    system32 = Path(os.environ["SystemRoot"]) / "System32"
    output = tmp_path / "partial.json"
    script = tmp_path / "partial.ps1"
    code = "import sys,time;sys.stderr.write('synthetic private output');sys.stderr.flush();time.sleep(30)"
    script.write_text(
        _replay_wrapper(sys.executable, subprocess.list2cmdline(["-I", "-c", code]), tmp_path, output),
        encoding="utf-8-sig",
    )
    process = subprocess.Popen(
        [
            str(system32 / "WindowsPowerShell/v1.0/powershell.exe"),
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(script),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = None
    try:
        deadline = time.monotonic() + 6
        report = {}
        while time.monotonic() < deadline:
            try:
                report = json.loads(output.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                pass
            if report.get("process_started"):
                break
            time.sleep(0.02)
        assert report.get("process_started"), report
        handle = kernel.OpenProcess(0x100000, False, report["child_pid"])
        assert handle, "the owned sleeping child must still be alive before wrapper termination"
        assert kernel.WaitForSingleObject(handle, 0) == 258
        # Kill only this Popen-owned wrapper. Its private job closes and must
        # terminate the exact child handle, not a process found by a recycled PID.
        process.kill()
        process.wait(timeout=2)
        assert kernel.WaitForSingleObject(handle, 1000) == 0
        rendered = json.dumps(report)
        assert "synthetic private output" not in rendered
        assert "actual_exit" not in report, "timeout must not manufacture a completed child result"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
        if handle:
            kernel.CloseHandle(handle)
        script.unlink(missing_ok=True)
        output.unlink(missing_ok=True)


def _native_ps(system32, script, timeout=5):
    return subprocess.run(
        [
            str(system32 / "WindowsPowerShell/v1.0/powershell.exe"),
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            base64.b64encode(script.encode("utf-16le")).decode("ascii"),
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


@pytest.mark.skipif(sys.platform != "win32", reason="requires native Windows Task Scheduler")
@pytest.mark.parametrize("mutation", ["none", "replacement", "absence"])
def test_native_registered_sleeping_replay_requires_termination_proof(monkeypatch, capsys, mutation):
    import ctypes
    from threading import Event, Thread
    from brigade.isolated_exec import trusted_cwd

    system32 = Path(os.environ["SystemRoot"]) / "System32"
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(ctypes.c_uint64)] * 4
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    with _owned_replay_workspace(Path(__file__).resolve().parents[1]) as workspace:
        nonce = secrets.token_hex(16)
        original = "BrigadeCare-sleep-source-" + nonce
        name = "BrigadeCare-diagnostic-" + nonce
        description = "replacement-" + nonce if mutation == "replacement" else nonce
        output = workspace / (name + ".json")
        # Fixed synthetic child, never an operator runbook. Both readers must
        # drain pipes before the live sleeping child is stopped through COM.
        code = "import sys,time;sys.stdout.write('x'*131072);sys.stdout.flush();sys.stderr.write('y'*131072);sys.stderr.flush();time.sleep(30)"
        arguments = subprocess.list2cmdline(["-I", "-c", code])
        declarations = "\n".join(
            f"${key}={_ps_literal(value)}"
            for key, value in dict(
                original=original,
                name=name,
                owner=nonce,
                description=description,
                exe=sys.executable,
                argv=arguments,
                cwd=trusted_cwd(),
            ).items()
        )
        create = (
            declarations
            + r"""
$ErrorActionPreference='Stop'
$s=New-Object -ComObject Schedule.Service;$s.Connect();$f=$s.GetFolder('\')
$d=$s.NewTask(0);$d.RegistrationInfo.Description=$owner
$d.Principal.UserId=[Security.Principal.WindowsIdentity]::GetCurrent().Name
$d.Principal.LogonType=3;$d.Principal.RunLevel=0
$d.Settings.Enabled=$true;$d.Settings.AllowDemandStart=$true
$a=$d.Actions.Create(0);$a.Path=$exe;$a.Arguments=$argv;$a.WorkingDirectory=$cwd
$null=$f.RegisterTaskDefinition($original,$d,2,$null,$null,3,$null)
"""
        )
        created = _native_ps(system32, create)
        assert created.returncode == 0
        handles, monitor_errors, mutation_results = {}, [], []
        cancel = Event()

        def monitor():
            try:
                end = time.monotonic() + 10
                while not cancel.is_set() and time.monotonic() < end:
                    try:
                        snapshot = json.loads(output.read_text(encoding="utf-8-sig"))
                    except (OSError, ValueError):
                        time.sleep(0.01)
                        continue
                    if not snapshot.get("process_started"):
                        time.sleep(0.01)
                        continue
                    for role in ("wrapper", "child"):
                        handle = kernel.OpenProcess(0x101001, False, snapshot[role + "_pid"])
                        if not handle:
                            raise RuntimeError("missing owned process handle")
                        created_at, exited, system, user = (ctypes.c_uint64() for _ in range(4))
                        if (
                            not kernel.GetProcessTimes(
                                handle,
                                ctypes.byref(created_at),
                                ctypes.byref(exited),
                                ctypes.byref(system),
                                ctypes.byref(user),
                            )
                            or created_at.value != snapshot[role + "_created_filetime"]
                        ):
                            kernel.CloseHandle(handle)
                            raise RuntimeError("owned process identity mismatch")
                        handles[role] = handle
                    if mutation != "none":
                        mutate = (
                            declarations
                            + r"""
$ErrorActionPreference='Stop'
$s=New-Object -ComObject Schedule.Service;$s.Connect();$f=$s.GetFolder('\');$t=$f.GetTask($name)
if ($t.Definition.RegistrationInfo.Description -cne $owner) {throw 'unexpected fixture ownership'}
"""
                        )
                        mutate += (
                            r"""
$d=$t.Definition;$d.RegistrationInfo.Description=$description
$null=$f.RegisterTaskDefinition($name,$d,6,$null,$null,3,$null)
"""
                            if mutation == "replacement"
                            else "$f.DeleteTask($name,0)\n"
                        )
                        mutation_results.append(_native_ps(system32, mutate).returncode)
                    return
            except Exception as exc:
                monitor_errors.append(type(exc).__name__)

        thread = Thread(target=monitor, daemon=True)
        thread.start()
        report = None
        try:
            # Only the fresh diagnostic name is deterministic for this fixture.
            # Crypto keys still use independent real cryptographic randomness.
            monkeypatch.setattr(secrets, "token_hex", lambda _: nonce)
            report = _registered_replay(
                system32=system32,
                task_name=original,
                workspace=workspace,
                executable=sys.executable,
                arguments=arguments,
                working_directory=trusted_cwd(),
            )
            thread.join(timeout=1)
            assert not thread.is_alive() and not monitor_errors
            assert set(handles) == {"wrapper", "child"}, "independent live handles must be held before cleanup"
            assert report["elapsed_seconds"] <= 15, report
            assert report["registered"] and report["wrapper_identity_verified"] and report["child_identity_verified"], (
                report
            )
            proof = {key: value for key, value in report.items() if isinstance(value, bool)}
            proof.update(
                case=mutation,
                elapsed_seconds=round(report["elapsed_seconds"], 3),
                independent_wrapper_signaled=kernel.WaitForSingleObject(handles["wrapper"], 0) == 0,
                independent_child_signaled=kernel.WaitForSingleObject(handles["child"], 0) == 0,
            )
            with capsys.disabled():
                print("REGISTERED_REPLAY_PROOF " + json.dumps(proof), file=sys.stderr, flush=True)
            if mutation == "none":
                assert report["cleanup_confirmed"] and report["task_absent_after_delete"], report
                assert report["stop_requested"] and report["delete_requested"], report
                assert report["termination_before_watchdog"], (
                    "COM Stop must prove termination before the private fallback"
                )
                assert report["all_instances_gone"] and report["wrapper_signaled"] and report["child_signaled"], report
                assert all(kernel.WaitForSingleObject(handle, 0) == 0 for handle in handles.values())
                assert not list(workspace.glob("BrigadeCare-diagnostic-*"))
            else:
                assert mutation_results == [0]
                assert (
                    not report["cleanup_confirmed"] and not report["stop_requested"] and not report["delete_requested"]
                ), report
                assert report["artifacts_retained"]
                assert output.exists() and (workspace / (name + ".ps1")).exists()
                if mutation == "replacement":
                    assert report["ownership_changed"], report
                else:
                    assert report["task_lookup_failed"], report
        finally:
            cancel.set()
            thread.join(timeout=2)
            # The FIXTURE owns these exact handles independently of task-name
            # lookup. Terminate only its verified original wrapper, never a
            # replacement's process or a process rediscovered by numeric PID.
            if "wrapper" in handles and kernel.WaitForSingleObject(handles["wrapper"], 0) == 258:
                kernel.TerminateProcess(handles["wrapper"], 1)
            signaled = len(handles) == 2 and all(
                kernel.WaitForSingleObject(handle, 1000) == 0 for handle in handles.values()
            )
            cleanup = (
                declarations
                + "\n$proven="
                + ("$true" if signaled else "$false")
                + r"""
$ErrorActionPreference='Stop'
$s=New-Object -ComObject Schedule.Service;$s.Connect();$f=$s.GetFolder('\')
if ($proven) {
    $t=$null
    try {$t=$f.GetTask($name)} catch {if ($_.Exception.HResult -ne -2147024894) {throw}}
    if ($null -ne $t) {
        if ($t.Definition.RegistrationInfo.Description -cne $description -or $t.GetInstances(0).Count -ne 0) {throw 'fixture cleanup refused'}
        $f.DeleteTask($name,0)
    }
}
$t=$f.GetTask($original)
if ($t.Definition.RegistrationInfo.Description -cne $owner -or $t.GetInstances(0).Count -ne 0) {throw 'source cleanup refused'}
$f.DeleteTask($original,0)
"""
            )
            cleaned = _native_ps(system32, cleanup)
            for handle in handles.values():
                kernel.CloseHandle(handle)
            if signaled and cleaned.returncode == 0:
                for path in workspace.glob("BrigadeCare-diagnostic-*"):
                    path.unlink()
            assert signaled and cleaned.returncode == 0, "owned fixture cleanup needs independent termination proof"


@contextmanager
def _owned_replay_workspace(parent):
    parent = parent.resolve()
    workspace = parent / ("synthetic replay spaces & percent% ! ^ (test)-" + secrets.token_hex(16))
    # Default mkdir inheritance, no chmod or explicit ACL. Collision fails
    # before writing anything; an existing directory is never adopted.
    workspace.mkdir()
    identity = workspace.lstat()
    try:
        yield workspace
    finally:
        current = workspace.lstat()
        if (
            (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino)
            or workspace.is_symlink()
            or getattr(current, "st_file_attributes", 0) & 0x400
            or workspace.resolve().parent != parent
        ):
            raise RuntimeError("owned replay fixture changed; refusing cleanup")
        # Unknown cleanup retains the evidence and never removes live-task
        # script/report files, including when the fixture assertion failed.
        if not list(workspace.glob("BrigadeCare-diagnostic-*")):
            shutil.rmtree(workspace)


def test_owned_replay_fixture_cleanup_never_adopts_existing_or_replaced_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(secrets, "token_hex", lambda _: "collision")
    collision = tmp_path / "synthetic replay spaces & percent% ! ^ (test)-collision"
    collision.mkdir()
    sentinel = collision / "preserve"
    sentinel.write_text("existing")
    with pytest.raises(FileExistsError):
        with _owned_replay_workspace(tmp_path):
            pytest.fail("must never adopt an existing directory")
    assert sentinel.read_text() == "existing"
    sentinel.unlink()
    collision.rmdir()
    moved = tmp_path / "moved-owned-fixture"
    with pytest.raises(RuntimeError, match="refusing cleanup"):
        with _owned_replay_workspace(tmp_path) as workspace:
            (workspace / "preserve").write_text("owned")
            workspace.rename(moved)
            workspace.mkdir()
            (workspace / "replacement").write_text("foreign")
    assert (moved / "preserve").read_text() == "owned"
    assert (workspace / "replacement").read_text() == "foreign"
    shutil.rmtree(moved)
    shutil.rmtree(workspace)
    with _owned_replay_workspace(tmp_path) as workspace:
        (workspace / "synthetic").write_text("fixture")
    assert not workspace.exists()


@contextmanager
def _preserve_scheduler_failure(observe):
    """Diagnostics cannot turn a failed original assertion into a pass."""
    try:
        yield
    except AssertionError:
        try:
            observe()
        except Exception:
            pass
        raise


def _ps_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def _replay_wrapper(executable, arguments, working_directory, output, *, approved=None, task_name=None, managed=False):
    # This script runs only as the random diagnostic task, never the original.
    # A private kill-on-close job contains the replay and all its descendants.
    return (
        "\n".join(
            f"${name}={_ps_literal(value)}"
            for name, value in dict(
                exe=executable,
                argv=arguments,
                cwd=working_directory,
                output=output,
                approved=approved or "",
                original=task_name or "",
            ).items()
        )
        + ("\n$managed=$true" if managed else "\n$managed=$false")
        + r"""
$ErrorActionPreference='Stop'
$r=@{wrapper_reached=$true;process_started=$false;wrapper_pid=$PID
    wrapper_created_filetime=[Diagnostics.Process]::GetCurrentProcess().StartTime.ToFileTimeUtc()}
$key=New-Object byte[] 32
$rng=[Security.Cryptography.RandomNumberGenerator]::Create()
try {$rng.GetBytes($key)} finally {$rng.Dispose()}
$h=[Security.Cryptography.HMACSHA256]::new($key)
[Array]::Clear($key,0,$key.Length)
function Digest($s) {
    ([BitConverter]::ToString($h.ComputeHash([Text.Encoding]::UTF8.GetBytes([string]$s)))).Replace('-','').ToLower()
}
function Save {
    [IO.File]::WriteAllText($output,($r | ConvertTo-Json -Depth 5 -Compress))
}
Save
try {
    $id=[Security.Principal.WindowsIdentity]::GetCurrent()
    $principal=New-Object Security.Principal.WindowsPrincipal($id)
    $r.elevated_admin=$principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    $r.session_id=[Diagnostics.Process]::GetCurrentProcess().SessionId
    $r.read_probes=@()
    foreach($probe in @(@{role='interpreter';path=$exe},
        @{role='initializer';path=[IO.Path]::Combine($cwd,'__init__.py')},
        @{role='runbook';path=$approved})) {
        if (-not $probe.path) {continue}
        $row=@{role=$probe.role}
        try {
            $reader=[IO.File]::Open($probe.path,'Open','Read','ReadWrite')
            $reader.Dispose();$row.readable=$true
        } catch {$row.readable=$false;$row.hresult=$_.Exception.HResult}
        $r.read_probes+= $row
    }
    if ($original) {
        try {
            $s=New-Object -ComObject Schedule.Service;$s.Connect()
            $account=New-Object Security.Principal.NTAccount($s.GetFolder('\').GetTask($original).Definition.Principal.UserId)
            $sid=$account.Translate([Security.Principal.SecurityIdentifier]).Value
            $r.principal_sid_matches=($sid -ceq $id.User.Value)
        } catch {$r.principal_query_error=$true}
    }
    Save
    Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Text;
using System.Threading.Tasks;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.ComponentModel;
public class ReplayCapture {
    [DllImport("kernel32.dll", SetLastError=true)] static extern IntPtr CreateJobObject(IntPtr a, string n);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool SetInformationJobObject(IntPtr h, int c, ref Limits l, uint n);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool AssignProcessToJobObject(IntPtr h, IntPtr p);
    [StructLayout(LayoutKind.Sequential)] struct Basic {
        public long a,b; public uint flags; public UIntPtr c,d; public uint e; public UIntPtr f; public uint g,h;
    }
    [StructLayout(LayoutKind.Sequential)] struct IO {public ulong a,b,c,d,e,f;}
    [StructLayout(LayoutKind.Sequential)] struct Limits {
        public Basic basic; public IO io; public UIntPtr a,b,c,d;
    }
    // The wrapper holds this private handle until process exit. Its closure
    // kills only this wrapper and the replay descendants in this nested job.
    static IntPtr job;
    static System.Threading.Timer watchdog;
    [DllImport("kernel32.dll")] static extern ulong GetTickCount64();
    public static ulong WatchdogDeadline;
    public static void Contain(bool managed) {
        job=CreateJobObject(IntPtr.Zero,null);
        if(job==IntPtr.Zero) throw new Win32Exception(Marshal.GetLastWin32Error());
        Limits l=new Limits(); l.basic.flags=0x2000;
        if(!SetInformationJobObject(job,9,ref l,(uint)Marshal.SizeOf(l)) ||
           !AssignProcessToJobObject(job,Process.GetCurrentProcess().Handle))
            throw new Win32Exception(Marshal.GetLastWin32Error());
        // This private wrapper can terminate only itself and its private job.
        // Bound its lifetime even if the controller loses its proof channel.
        if(managed) {
            WatchdogDeadline=GetTickCount64()+8000;
            watchdog=new System.Threading.Timer(_=>Process.GetCurrentProcess().Kill(),null,8000,-1);
        }
    }
    public class Stream {
        public long Count; public int Flags; public StringBuilder Text=new StringBuilder();
        static readonly string[] Patterns={
            "is part of the brigade extras surface, which is disabled.", "error: invalid runbook JSON:",
            "error: runbook policy failed", "error: consequential runbook is incomplete without verification_contract",
            "error: runbook pin check failed:", "usage: brigade", "error:", "PermissionError:",
            "FileNotFoundError:", "ModuleNotFoundError:", "ImportError:", "SyntaxError:"};
        public void Drain(TextReader reader) {
            char[] b=new char[1024]; int n; string tail="";
            while((n=reader.Read(b,0,b.Length))>0) {
                Count+=n;
                string scan=tail+new string(b,0,n);
                for(int i=0;i<Patterns.Length;i++) if(scan.Contains(Patterns[i])) Flags|=(1<<i);
                tail=scan.Substring(Math.Max(0,scan.Length-128));
                lock(Text) {int keep=Math.Min(n,4096-Text.Length); if(keep>0) Text.Append(b,0,keep);}
            }
        }
        public string Snapshot() {lock(Text) return Text.ToString();}
    }
    public Process Child;
    public Stream Out=new Stream(), Err=new Stream();
    public Task OutTask,ErrTask;
    public void Start(string exe,string args,string cwd) {
        ProcessStartInfo info=new ProcessStartInfo(exe,args);
        info.WorkingDirectory=cwd; info.UseShellExecute=false;
        info.RedirectStandardOutput=true; info.RedirectStandardError=true; info.CreateNoWindow=true;
        // No environment assignments: the scheduled token's environment stays intact.
        Child=Process.Start(info);
        OutTask=Task.Factory.StartNew(()=>Out.Drain(Child.StandardOutput));
        ErrTask=Task.Factory.StartNew(()=>Err.Drain(Child.StandardError));
    }
}
'@
    [ReplayCapture]::Contain($managed)
    $r.job_ready=$true;$r.watchdog_deadline_ticks=[long][ReplayCapture]::WatchdogDeadline
    $capture=New-Object ReplayCapture
    try {
        $capture.Start($exe,$argv,$cwd)
        $r.process_started=$true;$r.child_pid=$capture.Child.Id
        $null=$capture.Child.Handle
        $r.child_created_filetime=$capture.Child.StartTime.ToFileTimeUtc()
    } catch {
        $e=$_.Exception
        while ($null -ne $e.InnerException) {$e=$e.InnerException}
        $r.native_error=$(if ($e -is [ComponentModel.Win32Exception]) {$e.NativeErrorCode} else {$null})
        $r.launch_error=$true
        Save
        exit 0
    }
    Save
    # The controller stops this owned task at its execution deadline, closing
    # the private job even if stream reads or the child have not completed.
    $capture.Child.WaitForExit()
    $null=[Threading.Tasks.Task]::WaitAll(@($capture.OutTask,$capture.ErrTask),1000)
    $r.actual_exit=$capture.Child.ExitCode
    $r.stdout_chars=$capture.Out.Count;$r.stderr_chars=$capture.Err.Count
    $stdout=$capture.Out.Snapshot();$stderr=$capture.Err.Snapshot()
    $r.stdout_digest=Digest $stdout;$r.stderr_digest=Digest $stderr
    $flags=$capture.Err.Flags
    $r.stderr_flags=@{
        extras_disabled=([bool]($flags -band 1));invalid_runbook=([bool]($flags -band 2))
        policy_failed=([bool]($flags -band 4));contract_incomplete=([bool]($flags -band 8))
        pin_failed=([bool]($flags -band 16));argparse_error=(($flags -band 96) -eq 96)
        permission_error=([bool]($flags -band 128));missing_file=([bool]($flags -band 256))
        import_error=([bool]($flags -band 1536));syntax_error=([bool]($flags -band 2048))
    }
    Save
} catch {$r.wrapper_error=$true;Save}
finally {
    $h.Dispose()
    # Retain the real Process handle and wrapper until the controller has
    # opened identity-checked handles and COM Stop closes this private job.
    if ($managed -and $r.job_ready) {while ($true) {[Threading.Thread]::Sleep(40)}}
}
"""
    )


def _scheduler_replay(*, system32, task_name, workspace, approved, executable, arguments, working_directory):
    """One <=15s diagnostic replay, restricted to this test's synthetic echo."""
    deadline = time.monotonic() + 15
    expected = {
        "id": "windows-isolated-care",
        "allowed_commands": ["echo"],
        "steps": [{"id": "probe", "run": "echo scheduler-target"}],
    }
    if not approved.resolve().is_relative_to(workspace.resolve()) or json.loads(approved.read_text()) != expected:
        raise ValueError("diagnostic replay requires the synthetic echo fixture")
    from brigade.care_launchers import runbook_argv
    from brigade.isolated_exec import trusted_cwd

    original = subprocess.list2cmdline(runbook_argv(str(approved.relative_to(workspace)), workspace))
    expected_args = original.removeprefix(subprocess.list2cmdline([sys.executable])).lstrip()
    if executable != sys.executable or arguments != expected_args or Path(working_directory).resolve() != trusted_cwd():
        raise ValueError("diagnostic replay must preserve the exact synthetic action")
    return _registered_replay(
        system32=system32,
        task_name=task_name,
        workspace=workspace,
        executable=executable,
        arguments=arguments,
        working_directory=working_directory,
        approved=approved,
        deadline=deadline,
    )


def _registered_replay(
    *, system32, task_name, workspace, executable, arguments, working_directory, approved=None, deadline=None
):
    """Test-only controller: one ownership-checked stop/wait/delete path."""
    deadline = deadline if deadline is not None else time.monotonic() + 15
    owner = secrets.token_hex(16)
    name = "BrigadeCare-diagnostic-" + owner
    script_path = workspace / (name + ".ps1")
    output_path = workspace / (name + ".json")
    script_path.write_text(
        _replay_wrapper(
            executable, arguments, working_directory, output_path, approved=approved, task_name=task_name, managed=True
        ),
        encoding="utf-8-sig",
    )
    powershell = str(system32 / "WindowsPowerShell/v1.0/powershell.exe")
    declarations = "\n".join(
        f"${k}={_ps_literal(v)}"
        for k, v in dict(
            original=task_name,
            name=name,
            owner=owner,
            wrapper=script_path,
            ps=powershell,
            expectedExe=executable,
            expectedArgs=arguments,
            expectedCwd=working_directory,
            output=output_path,
        ).items()
    )
    # One controller retains process handles across Stop and owns the ONLY
    # deletion path. Missing/replaced tasks and missing proof fail closed.
    controller = (
        declarations
        + "\n$totalMs="
        + str(max(1, int((deadline - time.monotonic() - 0.3) * 1000)))
        + r"""
$ErrorActionPreference='Stop'
$clock=[Diagnostics.Stopwatch]::StartNew()
$r=@{registered=$false;cleanup_confirmed=$false;stop_requested=$false;delete_requested=$false}
$wh=[IntPtr]::Zero;$ch=[IntPtr]::Zero;$latest=$null
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class ReplayProof {
    [StructLayout(LayoutKind.Sequential)] struct FT {public uint low,high;}
    [DllImport("kernel32.dll",SetLastError=true)] static extern IntPtr OpenProcess(uint rights,bool inherit,int pid);
    [DllImport("kernel32.dll",SetLastError=true)] static extern bool GetProcessTimes(IntPtr h,out FT c,out FT e,out FT k,out FT u);
    [DllImport("kernel32.dll")] public static extern uint WaitForSingleObject(IntPtr h,uint ms);
    [DllImport("kernel32.dll")] public static extern bool CloseHandle(IntPtr h);
    [DllImport("kernel32.dll")] public static extern ulong GetTickCount64();
    public static IntPtr Open(int pid,long created) {
        IntPtr h=OpenProcess(0x101000,false,pid); if(h==IntPtr.Zero) return h;
        FT c,e,k,u;
        if(!GetProcessTimes(h,out c,out e,out k,out u) || (((long)c.high<<32)|c.low)!=created) {
            CloseHandle(h);return IntPtr.Zero;
        }
        return h;
    }
    public static bool Signaled(IntPtr h) {return h!=IntPtr.Zero && WaitForSingleObject(h,0)==0;}
}
'@
function OwnedTask {
    try {$current=$f.GetTask($name)}
    catch {$r.task_lookup_failed=$true;return $null}
    if ($current.Definition.RegistrationInfo.Description -cne $owner) {
        $r.ownership_changed=$true;return $null
    }
    return $current
}
function ReadProof {
    try {
        $script:latest=[IO.File]::ReadAllText($output) | ConvertFrom-Json
        if ($script:wh -eq [IntPtr]::Zero -and $latest.wrapper_pid -and $latest.wrapper_created_filetime) {
            $script:wh=[ReplayProof]::Open([int]$latest.wrapper_pid,[long]$latest.wrapper_created_filetime)
        }
        if ($script:ch -eq [IntPtr]::Zero -and $latest.child_pid -and $latest.child_created_filetime) {
            $script:ch=[ReplayProof]::Open([int]$latest.child_pid,[long]$latest.child_created_filetime)
        }
    } catch {}
    $r.wrapper_identity_verified=($script:wh -ne [IntPtr]::Zero)
    $r.child_identity_verified=($script:ch -ne [IntPtr]::Zero)
    $r.child_not_started=($null -ne $latest -and $latest.launch_error -eq $true -and $latest.process_started -eq $false)
}
function CleanupOwned {
    if (-not $r.registered) {return}
    ReadProof
    $current=OwnedTask
    if ($null -eq $current) {return}
    # Fresh marker check is adjacent to Stop. Never stop a replacement task.
    $r.ownership_verified_before_stop=$true
    $current.Stop(0);$r.stop_requested=$true
    while ($clock.ElapsedMilliseconds -lt ($totalMs-100)) {
        $current=OwnedTask
        if ($null -eq $current) {return}
        $r.all_instances_gone=($current.GetInstances(0).Count -eq 0)
        $r.wrapper_signaled=[ReplayProof]::Signaled($wh)
        $r.child_signaled=[ReplayProof]::Signaled($ch)
        if ($r.all_instances_gone -and $r.wrapper_signaled -and $r.child_signaled) {break}
        [Threading.Thread]::Sleep(20)
    }
    if (-not ($r.all_instances_gone -and $r.wrapper_signaled -and $r.child_signaled)) {
        $r.termination_unproven=$true;return
    }
    $r.termination_before_watchdog=([ReplayProof]::GetTickCount64() -lt [uint64]$latest.watchdog_deadline_ticks)
    # Re-read ownership AND instances immediately before the single deletion.
    $current=OwnedTask
    if ($null -eq $current -or $current.GetInstances(0).Count -ne 0) {return}
    $current=OwnedTask
    if ($null -eq $current) {return}
    $r.ownership_verified_before_delete=$true
    $f.DeleteTask($name,0);$r.delete_requested=$true
    try {$null=$f.GetTask($name);$r.task_absent_after_delete=$false}
    catch {$r.task_absent_after_delete=($_.Exception.HResult -eq -2147024894)}
    $r.cleanup_confirmed=($r.task_absent_after_delete -eq $true)
}
try {
    $s=New-Object -ComObject Schedule.Service;$s.Connect();$f=$s.GetFolder('\')
    $source=$f.GetTask($original).Definition
    $originalAction=$source.Actions.Item(1)
    if ($originalAction.Path -cne $expectedExe -or $originalAction.Arguments -cne $expectedArgs -or
        $originalAction.WorkingDirectory -cne $expectedCwd) {throw 'action changed'}
    $d=$s.NewTask(0);$d.XmlText=$source.XmlText
    $d.Triggers.Clear();$d.Actions.Clear();$d.RegistrationInfo.Description=$owner
    $a=$d.Actions.Create(0);$a.Path=$ps
    $a.Arguments='-NoProfile -NonInteractive -File "'+$wrapper+'"'
    $a.WorkingDirectory=$source.Actions.Item(1).WorkingDirectory
    $t=$f.RegisterTaskDefinition($name,$d,2,$null,$null,$source.Principal.LogonType,$null)
    $r.registered=$true
    $registeredDefinition=$t.Definition
    [xml]$beforeXml=$source.XmlText;[xml]$afterXml=$registeredDefinition.XmlText
    $r.principal_matches=($beforeXml.Task.Principals.OuterXml -ceq $afterXml.Task.Principals.OuterXml)
    $r.settings_match=($beforeXml.Task.Settings.OuterXml -ceq $afterXml.Task.Settings.OuterXml)
    if (-not $r.principal_matches -or -not $r.settings_match) {throw 'definition changed'}
    $instance=$t.Run($null)
    $runClock=[Diagnostics.Stopwatch]::StartNew()
    while ($runClock.ElapsedMilliseconds -lt 6000 -and $clock.ElapsedMilliseconds -lt ($totalMs-4000)) {
        ReadProof
        if ($wh -ne [IntPtr]::Zero -and (($ch -ne [IntPtr]::Zero -and $latest.PSObject.Properties['actual_exit']) -or $r.child_not_started)) {break}
        [Threading.Thread]::Sleep(20)
    }
    $r.last_result=$t.LastTaskResult;$r.state=$t.State
} catch {$r.controller_error=$true}
finally {
    try {CleanupOwned} catch {
        $r.cleanup_error=$true;$r.cleanup_hresult=$_.Exception.HResult
        $r.cleanup_error_line=$_.InvocationInfo.ScriptLineNumber
        $r.cleanup_method_error=($_.FullyQualifiedErrorId -like '*Method*')
        $r.cleanup_cast_error=($_.FullyQualifiedErrorId -like '*Cast*' -or $_.FullyQualifiedErrorId -like '*Convert*')
    }
    $r.wrapper_signaled=[ReplayProof]::Signaled($wh)
    $r.child_signaled=[ReplayProof]::Signaled($ch)
    if ($wh -ne [IntPtr]::Zero) {$null=[ReplayProof]::CloseHandle($wh)}
    if ($ch -ne [IntPtr]::Zero) {$null=[ReplayProof]::CloseHandle($ch)}
}
$r | ConvertTo-Json -Depth 5 -Compress
"""
    )
    # The parent owns the TOTAL budget, including a separate cleanup reserve.
    private_key = secrets.token_bytes(32)

    def private(value, field=""):
        if isinstance(value, dict):
            return {k: private(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [private(v, field) for v in value]
        if isinstance(value, str):
            if field == "role" and value in {"interpreter", "initializer", "runbook"}:
                return value
            return hmac.new(private_key, value.encode(), hashlib.sha256).hexdigest()
        return value

    report = {"wrapper_reached": False, "cleanup_confirmed": False}

    def query(script, limit):
        return subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-EncodedCommand",
                base64.b64encode(script.encode("utf-16le")).decode("ascii"),
            ],
            capture_output=True,
            text=True,
            timeout=max(0.01, limit),
            check=False,
        )

    try:
        result = query(controller, deadline - time.monotonic() - 0.1)
        for line in result.stdout.splitlines():
            try:
                report.update(private(json.loads(line)))
            except (ValueError, TypeError):
                pass
    except subprocess.TimeoutExpired:
        report["controller_timeout"] = True
    except Exception as exc:
        report["controller_error_type"] = type(exc).__name__
    if output_path.is_file():
        try:
            report.update(private(json.loads(output_path.read_text(encoding="utf-8-sig"))))
        except (OSError, ValueError):
            report["partial_report"] = True
    # A boolean alone cannot certify cleanup; require the complete proof.
    report["cleanup_confirmed"] = (
        report.get("cleanup_confirmed") is True
        and all(
            report.get(key) is True
            for key in (
                "registered",
                "ownership_verified_before_stop",
                "ownership_verified_before_delete",
                "wrapper_identity_verified",
                "wrapper_signaled",
                "all_instances_gone",
                "task_absent_after_delete",
            )
        )
        and report.get("child_identity_verified") is True
        and report.get("child_signaled") is True
    )
    if report["cleanup_confirmed"]:
        for path in (script_path, output_path):
            path.unlink(missing_ok=True)
    else:
        report["artifacts_retained"] = True

    report["elapsed_seconds"] = time.monotonic() - (deadline - 15)
    return report


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
        $probePath=$_.path
        $row=@{role=$_.role;path_sha256=(Hash $probePath);access_token='current-test-token'}
        $operation='Get-Item'
        try {
            $item=Get-Item -LiteralPath $probePath -Force
            $operation='Get-Acl'
            $acl=Get-Acl -LiteralPath $probePath
            $operation='ACL-rules'
            $row.owner_sha256=Hash $acl.Owner
            $row.rules=@($acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier]) |
                Select-Object -First 32 | ForEach-Object {
                    @{sid_sha256=(Hash $_.IdentityReference.Value);rights=[int]$_.FileSystemRights
                        type=[int]$_.AccessControlType;inherited=$_.IsInherited
                        inheritance=[int]$_.InheritanceFlags;propagation=[int]$_.PropagationFlags}
                })
            $operation='current-token-read'
            if ($item.PSIsContainer) {
                $reader=[IO.Directory]::EnumerateFileSystemEntries($probePath).GetEnumerator()
                try { $null=$reader.MoveNext();$row.directory_read=$true }
                finally { if ($reader -is [IDisposable]) {$reader.Dispose()} }
            } else {
                $reader=[IO.File]::Open($probePath,'Open','Read','ReadWrite')
                $reader.Dispose();$row.file_read=$true
            }
        } catch {
            $row.operation=$operation
            $row.error_type=$_.Exception.GetType().FullName;$row.hresult=$_.Exception.HResult
            $row.category=[string]$_.CategoryInfo.Category
            # Only known command and error identifiers may cross the boundary.
            $row.command='other';$row.fqid='other'
            foreach($known in @('Get-Item','Get-Acl')) {
                if ($_.Exception.CommandName -ceq $known) {$row.command=$known}
            }
            foreach($known in @('CommandNotFoundException','CouldNotAutoloadMatchingModule',
                'Modules_ModuleNotFound','PathNotFound,Microsoft.PowerShell.Commands.GetItemCommand')) {
                if ($_.FullyQualifiedErrorId -ceq $known) {$row.fqid=$known}
            }
        }
        $row
    })
}
# Command discovery can autoload: perform it only AFTER the failed access probes.
Probe 'commands' {
    @('Get-Item','Get-Acl','Get-CimInstance','Get-WinEvent') | ForEach-Object {
        $name=$_
        try {$null=Get-Command -Name $name -ErrorAction Stop;@{command=$name;available=$true}}
        catch {@{command=$name;available=$false;error_type=$_.Exception.GetType().FullName}}
    }
}
Probe 'module_roots' {
    $paths=@($env:PSModulePath.Split(';') | ForEach-Object {$_.TrimEnd('\')})
    $inbox=Join-Path $PSHOME 'Modules'
    @{
        native_inbox_present=($paths -contains $inbox)
        native_inbox_exists=[IO.Directory]::Exists($inbox)
        security_manifest_exists=[IO.File]::Exists((Join-Path $inbox 'Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1'))
        management_manifest_exists=[IO.File]::Exists((Join-Path $inbox 'Microsoft.PowerShell.Management\Microsoft.PowerShell.Management.psd1'))
        ps7_root_present=(@($paths | Where-Object {$_ -match '\\PowerShell\\7\\Modules$'}).Count -gt 0)
    }
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
            if key in {"operation", "command", "fqid", "category"} and value in {
                "Get-Item",
                "Get-Acl",
                "Get-CimInstance",
                "Get-WinEvent",
                "ACL-rules",
                "current-token-read",
                "CommandNotFoundException",
                "CouldNotAutoloadMatchingModule",
                "Modules_ModuleNotFound",
                "PathNotFound,Microsoft.PowerShell.Commands.GetItemCommand",
                "other",
                "ObjectNotFound",
                "PermissionDenied",
                "ResourceUnavailable",
                "InvalidOperation",
                "NotSpecified",
                "SecurityError",
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
    _scheduled_extras_config(monkeypatch, workspace)
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

        def observe_failure():
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
                report["replay"] = _scheduler_replay(
                    system32=system32,
                    task_name=care_cmd._schtasks_task_name(entry),
                    workspace=workspace,
                    approved=approved,
                    executable=executable,
                    arguments=arguments,
                    working_directory=root.findtext(".//t:Exec/t:WorkingDirectory", namespaces=ns),
                )
            except Exception as exc:
                report["replay"] = {"error_type": type(exc).__name__}
            try:
                with capsys.disabled():
                    print("SCHEDULER_DIAGNOSTICS " + json.dumps(report), file=sys.stderr, flush=True)
            except Exception:
                # Even a broken diagnostic output stream must not replace the
                # original scheduler assertion below.
                pass

        with _preserve_scheduler_failure(observe_failure):
            assert len(receipts) == 2, "native scheduler failed to run its stored action"
        assert not marker.exists()
        assert all(json.loads(path.read_text())["status"] == "completed" for path in receipts)
    finally:
        subprocess.run(["schtasks", "/Delete", "/TN", care_cmd._schtasks_task_name(entry), "/F"], capture_output=True)
