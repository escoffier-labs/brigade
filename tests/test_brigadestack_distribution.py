"""Bridge package contracts, independent of the development CLI version."""

import json
import os
import select
import shlex
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

from tests._posix import requires_process_groups

from brigade import toml_compat

BRIDGE = Path(__file__).resolve().parents[1] / "distribution/brigadestack"


def test_python_bridge_adds_only_an_alias_for_the_exact_stable_dependency():
    manifest = toml_compat.loads((BRIDGE / "pypi/pyproject.toml").read_text())
    project = manifest["project"]
    assert (project["name"], project["version"]) == ("brigadestack", "0.30.0")
    assert project["requires-python"] == ">=3.10"
    assert project["dependencies"] == ["brigade-cli==0.30.0"]
    assert project["scripts"] == {"brigadestack": "brigade.cli:process_main"}
    # No discovery of a second brigade package or alternate implementation.
    assert manifest["tool"]["setuptools"]["packages"] == []
    assert manifest["tool"]["setuptools"]["py-modules"] == []


def test_npm_bridge_keeps_existing_global_brigade_bin_and_pins_stable():
    manifest = json.loads((BRIDGE / "npm/package.json").read_text())
    assert (manifest["name"], manifest["version"]) == ("brigadestack", "0.30.0")
    assert manifest["dependencies"] == {"@escoffier-labs/brigade": "0.30.0"}
    assert manifest["bin"] == {"brigadestack": "launcher.cjs"}
    assert manifest["engines"]["node"] == "^22.15.0 || >=23.11.0"


def test_npm_launcher_contracts():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for bridge launcher contract tests")
    result = subprocess.run(
        [node, "--test", str(BRIDGE / "npm/launcher.test.cjs")],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@requires_process_groups
@pytest.mark.skipif(
    os.name != "posix" or shutil.which("bash") is None,
    reason="Requires a POSIX controlling terminal and bash job control",
)
@pytest.mark.parametrize("entrypoint", ["direct", "bridge"])
def test_npm_launcher_preserves_controlling_terminal_and_job_control(tmp_path, entrypoint):
    """A stopped foreground job must stop its workload, then resume and cancel."""
    import pty

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for bridge terminal tests")
    dependency = tmp_path / "node_modules/@escoffier-labs/brigade"
    dependency.mkdir(parents=True)
    ready = tmp_path / "ready.json"
    heartbeat = tmp_path / "heartbeat"
    (dependency / "launcher.cjs").write_text(
        "const fs = require('node:fs');\n"
        "let tty;\n"
        "try { fs.closeSync(fs.openSync('/dev/tty', 'r+')); tty = 'open'; }\n"
        "catch (error) { tty = error.code; }\n"
        f"fs.writeFileSync({json.dumps(str(ready))}, JSON.stringify({{\n"
        "  pid: process.pid, tty, stdinTTY: process.stdin.isTTY }));\n"
        f"setInterval(() => fs.appendFileSync({json.dumps(str(heartbeat))}, '.'), 25);\n"
    )
    launcher = tmp_path / "launcher.cjs"
    shutil.copyfile(BRIDGE / "npm/launcher.cjs", launcher)
    command = dependency / "launcher.cjs" if entrypoint == "direct" else launcher
    prompt = b"BRIDGE_TEST_PROMPT> "
    shell_pid, terminal = pty.fork()
    if shell_pid == 0:
        os.execve(
            shutil.which("bash"),
            ["bash", "--noprofile", "--norc", "-i"],
            {**os.environ, "PS1": prompt.decode(), "PROMPT_COMMAND": ""},
        )

    workload_pid = None
    foreground_group = None
    transcript = bytearray()

    def read_until(marker):
        output = bytearray()
        deadline = time.monotonic() + 5
        while marker not in output:
            remaining = deadline - time.monotonic()
            assert remaining > 0, bytes(transcript)
            if select.select([terminal], [], [], remaining)[0]:
                chunk = os.read(terminal, 65536)
                assert chunk, bytes(transcript)
                output.extend(chunk)
                transcript.extend(chunk)
        return bytes(output)

    def ticks():
        return heartbeat.stat().st_size if heartbeat.exists() else 0

    def wait_for_ticks(after):
        deadline = time.monotonic() + 5
        while ticks() <= after:
            assert time.monotonic() < deadline, bytes(transcript)
            time.sleep(0.01)

    try:
        read_until(prompt)
        os.write(terminal, (shlex.join([node, str(command)]) + "\n").encode())
        wait_for_ticks(0)
        metadata = json.loads(ready.read_text())
        workload_pid = metadata["pid"]
        foreground_group = os.tcgetpgrp(terminal)
        workload_group = os.getpgid(workload_pid)
        os.write(terminal, b"\x1a")  # Terminal-generated SIGTSTP, as with Ctrl-Z.
        stopped_output = read_until(prompt)
        stopped_ticks = ticks()
        time.sleep(0.3)  # Observe twelve workload intervals while the shell owns the tty.
        ticks_while_stopped = ticks() - stopped_ticks

        os.write(terminal, b"fg\n")
        wait_for_ticks(ticks())
        resumed_group = os.tcgetpgrp(terminal)
        os.write(terminal, b"\x03")  # Cancel the resumed foreground job with Ctrl-C.
        read_until(prompt)
        cancelled_ticks = ticks()
        time.sleep(0.1)

        assert b"Stopped" in stopped_output, stopped_output
        assert {
            "tty": metadata["tty"],
            "stdinTTY": metadata["stdinTTY"],
            "workload_in_foreground_group": workload_group == foreground_group,
            "ticks_while_stopped": ticks_while_stopped,
        } == {
            "tty": "open",
            "stdinTTY": True,
            "workload_in_foreground_group": True,
            "ticks_while_stopped": 0,
        }
        assert resumed_group == foreground_group
        assert ticks() == cancelled_ticks, "Ctrl-C left the actual workload running"
        with pytest.raises(ProcessLookupError):
            os.kill(workload_pid, 0)
    finally:
        # Also clean up the detached workload on the known-bad launcher.
        for pid in [workload_pid, -foreground_group if foreground_group else None]:
            if pid is not None:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        os.kill(shell_pid, signal.SIGKILL)
        os.waitpid(shell_pid, 0)
        os.close(terminal)
