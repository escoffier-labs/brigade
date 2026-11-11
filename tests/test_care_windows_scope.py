"""Windows replacement retains one validated reflect scope."""

import subprocess
import sys

import pytest

from brigade import care_windows


@pytest.mark.parametrize("scope", ["fleet", "repo:example/app", "collection:example"])
def test_windows_reinstall_preserves_existing_scope(scope):
    previous = subprocess.list2cmdline(
        [
            "-I",
            "-c",
            'print("--scope decoy")',
            "memory",
            "reflect",
            "run",
            "--target",
            r"C:\example workspace",
            "--scope",
            scope,
        ]
    )
    desired = subprocess.list2cmdline(
        ["-I", "-c", 'print("new")', "memory", "reflect", "run", "--target", r"C:\example workspace"]
    )
    result = care_windows.preserve_scope_arguments(desired, previous)
    assert care_windows.split_windows_argv(result)[-2:] == ["--scope", scope]
    assert care_windows.split_windows_argv(result)[:-2] == care_windows.split_windows_argv(desired)


@pytest.mark.parametrize("tail", [["--scope", "bad"], ["--scope"], ["--scope", "fleet", "--scope", "repo:example"]])
def test_invalid_existing_windows_scope_refuses_replacement(tail):
    previous = subprocess.list2cmdline(["memory", "reflect", "run", *tail])
    with pytest.raises(ValueError):
        care_windows.preserve_scope_arguments("memory reflect run", previous)


def test_windows_argument_parser_handles_native_quoting():
    expected = [
        "-I",
        "-c",
        'print("quote")',
        "memory",
        "reflect",
        "run",
        "--target",
        "C:\\example path\\",
        "--scope",
        "repo:example",
    ]
    assert care_windows.split_windows_argv(subprocess.list2cmdline(expected)) == expected


@pytest.mark.skipif(sys.platform != "win32", reason="requires native Windows Task Scheduler")
def test_native_reflect_reinstall_keeps_scope(tmp_path, monkeypatch):
    import dataclasses
    import secrets
    import xml.etree.ElementTree as ET

    from brigade import care_cmd
    from tests._home import set_home

    set_home(monkeypatch, tmp_path / "home")
    workspace = tmp_path / "reflect folder"
    workspace.mkdir()
    entry = care_cmd.MEMORY_REFLECT_ENTRY
    name = "BrigadeCare-test-reflect-" + secrets.token_hex(8)
    try:
        for selected in (dataclasses.replace(entry, reflect_scope="fleet"), entry):
            command = care_windows.create_command(
                selected,
                command=care_windows.task_run(selected, workspace=workspace),
                name=name,
                schedule="/SC DAILY /ST 06:15",
                account='/RU "%USERNAME%" /IT',
            )
            result = subprocess.run(
                care_windows.split_windows_argv(command), capture_output=True, text=True, timeout=30
            )
            assert result.returncode == 0, result.stderr
            xml = subprocess.check_output(
                [care_windows.SCHTASKS_EXE, "/Query", "/TN", name, "/XML"], text=True, timeout=15
            )
            arguments = ET.fromstring(xml).findtext(".//{*}Exec/{*}Arguments")
            assert care_windows.split_windows_argv(arguments)[-2:] == ["--scope", "fleet"]
    finally:
        subprocess.run([care_windows.SCHTASKS_EXE, "/Delete", "/TN", name, "/F"], capture_output=True, timeout=15)


def test_isolated_scope_helper_roundtrips_quoted_action_arguments(tmp_path):
    import base64
    import json

    from brigade.isolated_exec import module_argv, trusted_cwd

    desired = subprocess.list2cmdline(
        ["-I", "-c", 'print("quoted")', "memory", "reflect", "run", "--target", str(tmp_path / "space folder")]
    )
    previous = desired + " --scope collection:example"
    payload = base64.b64encode(json.dumps([desired, previous]).encode()).decode()
    result = subprocess.run(
        module_argv("brigade.care_windows"),
        input=payload,
        cwd=trusted_cwd(),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == desired + " --scope collection:example"


def test_reflect_replacement_command_fits_cmd_limit(tmp_path):
    from brigade import care_cmd

    command = care_cmd._schtasks_create_command(care_cmd.MEMORY_REFLECT_ENTRY, workspace=tmp_path / "reflect folder")
    assert len(command) <= 8191


@pytest.mark.parametrize("entry_id", ["daily-care", "scoped-memory-reflect"])
def test_printed_windows_plan_matches_acceptance_grammar(tmp_path, monkeypatch, entry_id):
    """Mirror Assert-EncodedCarePlan in scripts/windows-native-acceptance.ps1."""
    import base64
    import dataclasses
    import re

    from brigade import care_cmd
    from brigade.isolated_exec import trusted_cwd
    from tests._home import set_home

    set_home(monkeypatch, tmp_path / "home")
    if entry_id == "daily-care":
        entry = next(item for item in care_cmd.CARE_ENTRIES if item.entry_id == "daily-care")
    else:
        entry = dataclasses.replace(care_cmd.MEMORY_REFLECT_ENTRY, reflect_scope="repo:example")
    command = care_cmd._schtasks_create_command(entry, workspace=tmp_path / "care folder")
    prefix = care_windows.POWERSHELL_EXE + " -NoProfile -NonInteractive -EncodedCommand "
    assert command.startswith(prefix)
    script = base64.b64decode(command.removeprefix(prefix)).decode("utf-16le")

    def literal(value):
        return "'" + value.replace("'", "''") + "'"

    match = re.search(r"\$action\.Arguments=(?P<value>'(?:[^']|'')*');", script)
    assert match, "care action arguments missing"
    raw = match.group("value")
    name = care_cmd._schtasks_task_name(entry)
    schedule = care_cmd._schtasks_schedule_flags(entry.schedule)
    expected = (
        "$ErrorActionPreference='Stop'; $s=New-Object -ComObject Schedule.Service; $s.Connect(); "
        "$f=$s.GetFolder('\\'); "
        "$old=$null; $xml=$null; "
        f"try {{ $old=$f.GetTask('{name}') }} "
        "catch { $err=$_.Exception; "
        "while ($null -ne $err.InnerException) { $err=$err.InnerException }; "
        "if ($err.HResult -ne -2147024894) { throw } }; "
        "if ($null -ne $old) { $xml=$old.Xml }; "
        f"try {{ & 'C:\\Windows\\System32\\schtasks.exe' /Create /TN '{name}' "
        f"/TR 'C:\\Windows\\System32\\cmd.exe /d /c exit 1' {schedule} /RU $env:USERNAME /IT /F; "
        "if ($LASTEXITCODE -ne 0) { throw 'cannot create care task' }; "
        f"$d=$f.GetTask('{name}').Definition; "
        "$action=$d.Actions.Item(1); "
        f"$action.Path={literal(sys.executable)}; $action.Arguments={raw}; "
        f"$action.WorkingDirectory={literal(str(trusted_cwd()))}; "
        f"$f.RegisterTaskDefinition('{name}',$d,6,$null,$null,3,$null) | Out-Null"
        "; } catch { if ($null -ne $xml) { "
        f"$f.RegisterTask('{name}',$xml,6,$null,$null,3,$null) | Out-Null"
        f" }} else {{ $f.DeleteTask('{name}',0) }}; throw }}"
    )
    assert script == expected
    arguments = care_windows.split_windows_argv(raw[1:-1].replace("''", "'"))
    if entry.reflect_scope is not None:
        assert arguments[-2:] == ["--scope", "repo:example"]
