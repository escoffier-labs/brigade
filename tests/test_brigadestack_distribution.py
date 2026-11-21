"""Distribution contracts: brigadestack is the real package, brigade-cli the same code under the old name."""

import hashlib
import importlib.util
import json
import os
import select
import shlex
import shutil
import signal
import subprocess
import sys
import time
import zipfile
from email.parser import Parser
from pathlib import Path

import pytest

from tests._posix import requires_process_groups
from tests.test_publish_workflow import _load_workflow_yaml

import tomllib

ROOT = Path(__file__).resolve().parents[1]
NPM_COMPAT = ROOT / "distribution/escoffier-labs-brigade/npm"
NPM_REAL = ROOT / "npm/feasibility"
BRIGADE_CLI_README = ROOT / "distribution/brigade-cli/pypi/README.md"
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
    assert jobs["publish-compat"]["env"]["COMPAT_DIR"] == "distribution/escoffier-labs-brigade/npm"
    # A rerun reconciles integrity and provenance in the publisher instead of a bare publish, and the
    # job no longer aborts when the version is already live.
    assert "node npm/feasibility/publish-compat.cjs" in steps
    assert "npm publish" not in steps
    assert "bump ${process.env.COMPAT_DIR}" not in steps
    assert "npm/feasibility/runtime.json" in steps and "npm/feasibility/package.json" in steps
    publisher = (ROOT / "npm/feasibility/publish-compat.cjs").read_text()
    assert "--provenance" in publisher and "Immutable registry integrity mismatch" in publisher


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(f"{name}_under_test", ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


BRIGADESTACK_SCRIPTS = {
    "brigade": "brigade.cli:process_main",
    "brigadestack": "brigade.cli:process_main",
    "brigade-hook": "brigade.claude_hooks.entry:main",
}
BRIGADE_CLI_SCRIPTS = {
    "brigade": "brigade.cli:process_main",
    "brigade-cli": "brigade.cli:process_main",
    "brigade-hook": "brigade.claude_hooks.entry:main",
}


def test_root_package_is_brigadestack_with_the_brigadestack_and_brigade_scripts():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["name"] == "brigadestack"
    assert project["dependencies"] == []
    assert project["scripts"] == BRIGADESTACK_SCRIPTS
    # The stamped brigade-cli build owns this script.
    assert "brigade-cli" not in project["scripts"]


def test_thin_brigade_cli_package_is_gone():
    # A package that depends on brigadestack breaks `pipx upgrade brigade-cli` from 0.31.0: pip writes
    # the new brigade/ files, then uninstalls the old brigade-cli, whose RECORD deletes them (#2816).
    assert not (ROOT / "distribution/brigade-cli/pypi/pyproject.toml").exists()


def test_stamped_pyproject_changes_only_the_name_and_scripts():
    stamp = _load_script("stamp_brigade_cli")
    root_text = (ROOT / "pyproject.toml").read_text()
    root = tomllib.loads(root_text)
    stamped = tomllib.loads(stamp.stamp_pyproject(root_text))
    assert stamped["project"]["name"] == "brigade-cli"
    assert stamped["project"]["scripts"] == BRIGADE_CLI_SCRIPTS
    # Same version, dependencies, extras, metadata and tool configuration, including package discovery.
    for document in (root, stamped):
        document["project"].pop("name")
        document["project"].pop("scripts")
    assert stamped == root


def test_stamp_refuses_a_pyproject_it_does_not_recognise():
    stamp = _load_script("stamp_brigade_cli")
    with pytest.raises(ValueError, match="name"):
        stamp.stamp_pyproject('[project]\nname = "other"\n[project.scripts]\nx = "y:z"\n[tool]\n')
    with pytest.raises(ValueError, match="scripts"):
        stamp.stamp_pyproject('[project]\nname = "brigadestack"\n[tool]\n')


def test_stamp_never_edits_the_checkout_and_requires_an_empty_output(tmp_path):
    stamp = _load_script("stamp_brigade_cli")
    before = (ROOT / "pyproject.toml").read_bytes()
    out = tmp_path / "copy"
    stamp.stamp(ROOT, out)
    assert (ROOT / "pyproject.toml").read_bytes() == before
    assert 'name = "brigade-cli"' in (out / "pyproject.toml").read_text()
    assert (out / "README.md").read_text() == BRIGADE_CLI_README.read_text()
    assert (out / "src/brigade/cli/__init__.py").is_file()
    assert not list((out / "src").rglob("__pycache__"))
    with pytest.raises(ValueError, match="not empty"):
        stamp.stamp(ROOT, out)


def test_brigade_cli_readme_points_to_brigadestack_and_does_not_claim_the_registry_markers():
    readme = BRIGADE_CLI_README.read_text()
    assert "old PyPI name" in readme
    assert "pipx install brigadestack" in readme
    assert "depends on" not in readme
    # The MCP registry reads these from the README PyPI shows for `brigadestack`.
    assert "mcp-name" not in readme
    assert "mcp-name: io.github.escoffier-labs/brigade-memory" in (ROOT / "README.md").read_text()


@pytest.fixture(scope="module")
def built_wheels(tmp_path_factory):
    """Build brigadestack from a copy of the tree and brigade-cli from the stamped copy of it."""
    stamp = _load_script("stamp_brigade_cli")
    work = tmp_path_factory.mktemp("wheels")
    real = work / "brigadestack"
    real.mkdir()
    for name in (*stamp.COPIED_FILES, "README.md"):
        shutil.copy2(ROOT / name, real / name)
    shutil.copytree(ROOT / "src", real / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"))
    old = work / "brigade-cli"
    stamp.stamp(ROOT, old)
    out = work / "dist"
    wheels = {}
    for name, source in (("brigadestack", real), ("brigade-cli", old)):
        result = subprocess.run(
            [sys.executable, "-m", "pip", "wheel", "--no-deps", "--quiet", "--wheel-dir", str(out), str(source)],
            capture_output=True,
            text=True,
            timeout=600,
        )
        if result.returncode != 0 and any(
            marker in result.stderr for marker in ("Could not find a version", "Connection", "resolution")
        ):
            pytest.skip(f"cannot fetch the build backend offline: {result.stderr[-300:]}")
        assert result.returncode == 0, result.stdout + result.stderr
        wheels[name] = next(out.glob(f"{name.replace('-', '_')}-*.whl"))
    return wheels


def _read_wheel(path: Path):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        dist_info = next(name.split("/")[0] for name in names if name.endswith(".dist-info/METADATA"))
        metadata = Parser().parsestr(archive.read(f"{dist_info}/METADATA").decode())
        entry_points = archive.read(f"{dist_info}/entry_points.txt").decode()
        files = {
            name: hashlib.sha256(archive.read(name)).hexdigest()
            for name in names
            if name.startswith("brigade/") and not name.endswith("/")
        }
    scripts = dict(
        line.split(" = ", 1) for line in entry_points.split("[console_scripts]")[1].strip().splitlines() if line.strip()
    )
    return metadata, scripts, files


def test_the_two_wheels_carry_the_same_code_version_and_dependencies(built_wheels):
    real_metadata, real_scripts, real_files = _read_wheel(built_wheels["brigadestack"])
    old_metadata, old_scripts, old_files = _read_wheel(built_wheels["brigade-cli"])
    root = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]

    assert real_metadata["Name"] == "brigadestack"
    assert old_metadata["Name"] == "brigade-cli"
    assert real_metadata["Version"] == old_metadata["Version"] == root["version"]
    assert real_metadata["Requires-Python"] == old_metadata["Requires-Python"] == root["requires-python"]
    # The full copy depends on nothing from the real package: that was the pipx upgrade breakage.
    assert "brigadestack" not in " ".join(old_metadata.get_all("Requires-Dist") or [])
    assert old_metadata.get_all("Requires-Dist") == real_metadata.get_all("Requires-Dist")
    assert old_metadata.get_all("Provides-Extra") == real_metadata.get_all("Provides-Extra")
    assert set(old_metadata.get_all("Provides-Extra")) >= {"connector", "grokbot", "research"}
    # Identical brigade/ file set and contents, including the packaged templates and manifest.
    assert real_files and old_files == real_files
    assert "brigade/cli/__init__.py" in real_files
    assert real_scripts == BRIGADESTACK_SCRIPTS
    assert old_scripts == BRIGADE_CLI_SCRIPTS
    # Only the long description differs, and it names the old-name package.
    assert "old PyPI name" in old_metadata.get_payload()
    assert "old PyPI name" not in real_metadata.get_payload()


def test_previous_stable_picks_the_newest_older_non_yanked_stable_release():
    gate = _load_script("check_cli_upgrade")
    live = [{"yanked": False}]
    releases = {
        "0.30.0": live,
        "0.31.0": live,
        "0.31.1": [{"yanked": True}],
        "0.32.0": live,
        "0.32.0.dev20261120": live,
        "0.33.0rc1": live,
        "0.33.0": live,
        "0.29.0": [],
    }
    assert gate.previous_stable(releases, "0.32.0") == "0.31.0"
    assert gate.previous_stable(releases, "0.33.0") == "0.32.0"
    assert gate.previous_stable(releases, "0.100.0") == "0.33.0"
    with pytest.raises(ValueError, match="no stable"):
        gate.previous_stable(releases, "0.30.0")


def test_upgrade_gate_fails_closed_when_it_cannot_run(tmp_path, monkeypatch, capsys):
    gate = _load_script("check_cli_upgrade")
    assert gate.check_upgrade(tmp_path, "1.0.0", "0.9.0")[0].startswith("expected exactly one brigade_cli-1.0.0 wheel")

    def unreachable(_package):
        raise OSError("offline")

    monkeypatch.setattr(gate, "fetch_releases", unreachable)
    monkeypatch.setattr(sys, "argv", ["check_cli_upgrade.py", "--dist", str(tmp_path), "--version", "1.0.0"])
    assert gate.main() == 1
    assert "cannot resolve the release to upgrade from" in capsys.readouterr().err


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


def test_npm_compat_package_is_tied_to_the_npm_promotion_files_not_pyproject():
    # npm publishes after RELEASE.md section 5b already bumped pyproject.toml, and the npm pin in
    # runtime.json moves later in the section 8 promotion PR. The compatibility package must move
    # with that promotion: version, exact brigadestack pin, runtime.json and the launcher manifest.
    manifest = json.loads((NPM_COMPAT / "package.json").read_text())
    runtime = json.loads((NPM_REAL / "runtime.json").read_text())
    launcher = json.loads((NPM_REAL / "package.json").read_text())
    assert manifest["name"] == "@escoffier-labs/brigade"
    assert manifest["version"] == runtime["version"] == runtime["payloadVersion"] == launcher["version"]
    assert manifest["dependencies"] == {"brigadestack": launcher["version"]}


def test_npm_compat_package_is_not_version_synced_with_pyproject():
    sync = (ROOT / "scripts/version_sync.py").read_text()
    assert "escoffier-labs-brigade" not in sync.split("PLUGIN_LOCATIONS = [")[1].split("# ----")[0]


def test_npm_compat_package_declares_its_own_bin_and_payload():
    manifest = json.loads((NPM_COMPAT / "package.json").read_text())
    # npm links only the installed package's own bins, so `npm update -g` needs this one.
    assert manifest["bin"] == {"brigade": "launcher.cjs"}
    assert manifest["files"] == ["launcher.cjs", "README.md", "LICENSE"]
    assert manifest["engines"]["node"] == ">=22"
    assert manifest["repository"]["directory"] == "distribution/escoffier-labs-brigade/npm"


def test_npm_compat_launcher_resolves_brigadestack_not_itself():
    launcher = (NPM_COMPAT / "launcher.cjs").read_text()
    # Node's resolver would also consult NODE_PATH and global folders, so the lookup is manual.
    assert "require.resolve" not in launcher
    assert "path.join(dir, 'node_modules', 'brigadestack')" in launcher
    assert "@escoffier-labs/brigade/launcher.cjs" not in launcher


def test_npm_launcher_contracts():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for compatibility launcher contract tests")
    result = subprocess.run(
        [node, "--test", str(NPM_COMPAT / "launcher.test.cjs")],
        capture_output=True,
        text=True,
        timeout=180,
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
    (dependency / "package.json").write_text(json.dumps({"name": "brigadestack", "version": "1.0.0"}))
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
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "@escoffier-labs/brigade", "version": "1.0.0", "dependencies": {"brigadestack": "1.0.0"}})
    )
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


@pytest.mark.parametrize(
    "page",
    [
        "README.md",
        "docs/update-channels.md",
        "npm/feasibility/README.md",
        "distribution/escoffier-labs-brigade/npm/README.md",
    ],
)
def test_install_pages_explain_the_global_brigade_bin_conflict_with_both_commands(page):
    # npm refuses the second global install because both packages link `brigade`.
    text = (ROOT / page).read_text()
    assert "EEXIST" in text or "refuses the second" in text or "npm refuses" in text
    assert "npm uninstall --global @escoffier-labs/brigade" in text
    assert "npm install --global brigadestack" in text
