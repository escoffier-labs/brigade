"""Distribution contracts: brigadestack is the real package, brigade-cli the compatibility one."""

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
from tests.test_publish_workflow import _load_workflow_yaml

import tomllib

ROOT = Path(__file__).resolve().parents[1]
NPM_COMPAT = ROOT / "distribution/escoffier-labs-brigade/npm"
NPM_REAL = ROOT / "npm/feasibility"
COMPAT = ROOT / "distribution/brigade-cli/pypi"
MAIN_DISPATCH = "github.repository_id == '1238165856' && github.ref == 'refs/heads/main' && github.event_name == 'workflow_dispatch'"


def test_retired_npm_bridge_workflow_is_gone_and_one_workflow_publishes_every_npm_package():
    # npm allows one trusted publisher per package, so brigadestack moves to npm-publish.yml,
    # the file the six existing names already trust. The bridge workflow no longer exists.
    assert not (ROOT / ".github/workflows/brigadestack-npm-publish.yml").exists()
    assert not (ROOT / "distribution/brigadestack/npm").exists()
    workflow = _load_workflow_yaml(ROOT / ".github/workflows/npm-publish.yml")
    jobs = workflow["jobs"]
    assert "publish-compat" in jobs
    oidc = {name for name, job in jobs.items() if "id-token" in job.get("permissions", {})}
    # Only the two registry-writing jobs may request an OIDC token, both inside the npm environment.
    assert oidc == {"publish", "publish-compat"}
    for name in oidc:
        assert jobs[name]["environment"] == "npm"
        assert jobs[name]["permissions"] == {"contents": "read", "id-token": "write"}
    # The compatibility package depends on exactly the brigadestack version this run published.
    assert set(jobs["publish-compat"]["needs"]) == {"build", "registry-acceptance"}
    assert jobs["registry-acceptance"]["needs"] == "publish"
    assert "publish-compat" in jobs["release-gate"]["needs"]
    steps = "\n".join(step.get("run", "") for step in jobs["publish-compat"]["steps"])
    assert 'test "$GITHUB_SHA" = "$EXPECTED_SHA"' in steps
    assert 'npm view "brigadestack@$VERSION" version' in steps
    assert (
        "--provenance" in steps
        and "distribution/escoffier-labs-brigade/npm" in jobs["publish-compat"]["env"]["COMPAT_DIR"]
    )


def test_root_package_is_brigadestack_with_the_brigadestack_and_brigade_scripts():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["name"] == "brigadestack"
    assert project["dependencies"] == []
    assert project["scripts"]["brigadestack"] == "brigade.cli:process_main"
    assert project["scripts"]["brigade"] == "brigade.cli:process_main"
    assert project["scripts"]["brigade-hook"] == "brigade.claude_hooks.entry:main"
    # The compatibility package owns this script now.
    assert "brigade-cli" not in project["scripts"]


def test_compat_package_depends_on_exactly_the_root_version_and_declares_its_own_scripts():
    root = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    manifest = tomllib.loads((COMPAT / "pyproject.toml").read_text())
    project = manifest["project"]
    assert project["name"] == "brigade-cli"
    assert project["version"] == root["version"]
    assert project["requires-python"] == root["requires-python"]
    assert project["dependencies"] == [f"brigadestack=={root['version']}"]
    # pipx exposes only the installed package's own scripts, so these must live here.
    assert project["scripts"] == {
        "brigade": "brigade.cli:process_main",
        "brigade-cli": "brigade.cli:process_main",
        "brigade-hook": "brigade.claude_hooks.entry:main",
    }
    # Every user-facing extra of the real package is forwarded at the same exact version,
    # so pins like `brigade-cli[connector]==X` keep installing the extra's dependencies.
    user_extras = set(root["optional-dependencies"]) - {"dev"}
    assert project["optional-dependencies"] == {
        extra: [f"brigadestack[{extra}]=={root['version']}"] for extra in sorted(user_extras)
    }
    # No discovery of a second brigade package or alternate implementation.
    assert manifest["tool"]["setuptools"]["packages"] == []
    assert manifest["tool"]["setuptools"]["py-modules"] == []


def test_compat_readme_points_to_brigadestack_and_does_not_claim_the_registry_markers():
    readme = (COMPAT / "README.md").read_text()
    assert "compatibility" in readme
    assert "pipx install brigadestack" in readme
    # The MCP registry reads these from the README PyPI shows for `brigadestack`.
    assert "mcp-name" not in readme
    assert "mcp-name: io.github.escoffier-labs/brigade-memory" in (ROOT / "README.md").read_text()


def test_mcp_registry_manifests_launch_the_brigadestack_package():
    for server in ("memory", "skills"):
        manifest = json.loads((ROOT / f"mcp-registry/{server}/server.json").read_text())
        assert [package["identifier"] for package in manifest["packages"]] == ["brigadestack"]


def test_retired_bridge_publish_workflow_is_gone():
    assert not (ROOT / ".github/workflows/brigadestack-publish.yml").exists()
    assert not (ROOT / "distribution/brigadestack/pypi").exists()


def test_npm_real_package_is_brigadestack_with_both_commands():
    manifest = json.loads((NPM_REAL / "package.json").read_text())
    assert manifest["name"] == "brigadestack"
    assert manifest["bin"] == {"brigade": "launcher.cjs", "brigadestack": "launcher.cjs"}
    assert "dependencies" not in manifest
    assert all(name.startswith("@escoffier-labs/brigade-uv-") for name in manifest["optionalDependencies"])


def test_npm_compat_package_depends_on_exactly_the_root_version_and_declares_its_own_bin():
    root = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    manifest = json.loads((NPM_COMPAT / "package.json").read_text())
    assert manifest["name"] == "@escoffier-labs/brigade"
    assert manifest["version"] == root["version"]
    assert manifest["dependencies"] == {"brigadestack": root["version"]}
    # npm links only the installed package's own bins, so `npm update -g` needs this one.
    assert manifest["bin"] == {"brigade": "launcher.cjs"}
    assert manifest["files"] == ["launcher.cjs", "README.md", "LICENSE"]
    assert manifest["engines"]["node"] == "^22.15.0 || >=23.11.0"
    assert manifest["repository"]["directory"] == "distribution/escoffier-labs-brigade/npm"


def test_npm_compat_launcher_resolves_brigadestack_not_itself():
    launcher = (NPM_COMPAT / "launcher.cjs").read_text()
    assert "require.resolve('brigadestack/launcher.cjs')" in launcher
    assert "@escoffier-labs/brigade/launcher.cjs" not in launcher


def test_npm_launcher_contracts():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for compatibility launcher contract tests")
    result = subprocess.run(
        [node, "--test", str(NPM_COMPAT / "launcher.test.cjs")],
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
@pytest.mark.parametrize("entrypoint", ["direct", "compat"])
def test_npm_launcher_preserves_controlling_terminal_and_job_control(tmp_path, entrypoint):
    """A stopped foreground job must stop its workload, then resume and cancel."""
    import pty

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for compatibility terminal tests")
    dependency = tmp_path / "node_modules/brigadestack"
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
    shutil.copyfile(NPM_COMPAT / "launcher.cjs", launcher)
    command = dependency / "launcher.cjs" if entrypoint == "direct" else launcher
    prompt = b"COMPAT_TEST_PROMPT> "
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
