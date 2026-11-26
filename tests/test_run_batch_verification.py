"""Coordinator-owned verification through the guarded batch CLI."""

import json
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from brigade import agents, cli, localio, run_batch_verify_tree, run_export_projection, run_tasks
from brigade.run_batch import SCHEMA
from brigade.work_cmd import verification
from tests.test_run_batch_execute import _commit, task
from tests.test_run_batch_worktrees import site as site


@pytest.fixture
def setup(site, monkeypatch):
    # Use the short checkout name and isolated home exercised by the native
    # Windows worktree tests. The pytest case name must not become a Git slug.
    calls = []

    def model(agent, prompt, **kwargs):
        calls.append(prompt)
        return agents.AgentResult(text="done", ok=True)

    monkeypatch.setattr(agents, "run_agent", model)
    return site.repo, calls


def _run_batch(setup, capsys, *, isolation="worktree", verify=None):
    root, _ = setup
    path = root.parent / "home" / "batch.json"
    spec = {
        "schema": SCHEMA,
        "request_id": "req",
        "tasks": [{**task("a", read_only=isolation == "shared"), "isolation": isolation}],
    }
    if verify is not None:
        spec["verify"] = verify
    path.write_text(json.dumps(spec), encoding="utf-8")
    rc = cli.main(["run", "batch", str(path), "--cwd", str(root), "--json"])
    captured = capsys.readouterr()
    assert captured.out, captured.err
    result = json.loads(captured.out)
    assert rc == 0, f"{captured.err}\n{result}"
    run_dir = root / ".brigade" / "runs" / result["run_id"]
    payload = json.loads((run_dir / result["tasks"]["a"]["result_ref"]).read_text())
    return result, run_dir, payload


@pytest.fixture
def configured_repo(setup):
    root, _ = setup
    (root / ".gitignore").write_text(".brigade/\n.venv/\nbatch.json\nhome/\n", encoding="utf-8")
    (root / "verify").mkdir()
    (root / "verify" / "required-tests.json").write_text(
        json.dumps(
            {
                "schema": "brigade.verify_required_config.v1",
                "planner_files": ["planner.py"],
                "planner_argv": ["{python}", "{planner_dir}/planner.py", "{output}"],
                "mode_key": "mode",
                "selected_key": "selected",
                "run_argv": [sys.executable, "{tests}"],
            }
        ),
        encoding="utf-8",
    )
    (root / "planner.py").write_text(
        "import json, pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text(json.dumps({'mode': 'selective', 'selected': ['selected.py']}))\n",
        encoding="utf-8",
    )
    (root / "selected.py").write_text(
        "import os\nassert 'BRIGADE_ATTEMPT_ID' not in os.environ\nprint('coordinator check ran')\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    _commit(root, "base")


def _receipt(run_dir):
    (path,) = run_dir.glob("batch-verification/*/receipt.json")
    return json.loads(path.read_text())


def _assert_binding(run_dir, payload, verification, *, status):
    receipt = _receipt(run_dir)
    (state,) = run_tasks.read_tasks(run_dir)
    digest = localio.canonical_json_digest(receipt, exclude_keys={"digests"})
    assert receipt["status"] == status
    assert receipt["producer_attempt_id"] == state.handle.attempt_id
    assert receipt["producer_run_id"] == run_dir.name
    assert verification == {
        "attempt_id": state.handle.attempt_id,
        "receipt_sha256": digest,
        "status": status,
        **({"trust": "candidate-authored-tests"} if status == "completed" else {}),
    }
    assert receipt["tree_fingerprint"] == receipt["tree_fingerprint_after"]
    assert receipt["tree_fingerprint"]
    assert "selected.py" not in json.dumps(payload)
    return receipt


def test_isolated_task_records_coordinator_verification(setup, linux_sandbox, configured_repo, capsys, monkeypatch):
    monkeypatch.setenv("BRIGADE_ATTEMPT_ID", "unrelated-caller-value")
    _, run_dir, payload = _run_batch(setup, capsys, verify=True)
    receipt = _assert_binding(run_dir, payload, payload["verification"], status="completed")
    assert receipt["commands"][0]["stdout_summary"] == "coordinator check ran"


def test_isolated_task_ignores_planted_receipt(setup, linux_sandbox, configured_repo, capsys, monkeypatch):
    fake_digest = None

    def model(agent, prompt, **kwargs):
        nonlocal fake_digest
        checkpoint = Path(kwargs["cwd"])
        run_dir = next((setup[0] / ".brigade" / "runs").glob("batch-*"))
        (state,) = run_tasks.read_tasks(run_dir)
        fake = {
            "producer_attempt_id": state.handle.attempt_id,
            "status": "failed",
            "target": str(checkpoint),
            "tree_fingerprint": localio.tree_fingerprint(checkpoint),
        }
        fake_digest = localio.canonical_json_digest(fake)
        fake["digests"] = {"receipt_sha256": fake_digest}
        localio.write_json(checkpoint / ".brigade/work/verify-runs/planted/receipt.json", fake)
        return agents.AgentResult(text="done", ok=True)

    monkeypatch.setattr(agents, "run_agent", model)
    _, run_dir, payload = _run_batch(setup, capsys, verify=True)
    assert fake_digest is not None
    assert fake_digest != payload["verification"]["receipt_sha256"]
    assert fake_digest not in json.dumps(_receipt(run_dir))
    _assert_binding(run_dir, payload, payload["verification"], status="completed")


def test_shared_task_records_only_skip_reason(setup, capsys):
    _, run_dir, payload = _run_batch(setup, capsys, isolation="shared", verify=True)
    assert payload["verification_skipped"] == "shared-checkout"
    assert "verification" not in payload
    assert not (run_dir / "batch-verification").exists()


def test_cloud_result_skips_verification(setup, configured_repo, capsys, monkeypatch):
    def model(agent, prompt, **kwargs):
        return agents.AgentResult(text="done", ok=True, cloud_environment={"status": "test"})

    monkeypatch.setattr(agents, "run_agent", model)
    _, run_dir, payload = _run_batch(setup, capsys, verify=True)
    assert payload["verification_skipped"] == "remote-or-cloud"
    assert "verification" not in payload
    assert not (run_dir / "batch-verification").exists()


def test_failed_verifier_records_failure_and_batch_finishes(setup, linux_sandbox, configured_repo, capsys, monkeypatch):
    checkout = None

    def model(agent, prompt, **kwargs):
        nonlocal checkout
        checkout = Path(kwargs["cwd"])
        (checkout / "selected.py").write_text("raise SystemExit(7)\n", encoding="utf-8")
        return agents.AgentResult(text="done", ok=True)

    monkeypatch.setattr(agents, "run_agent", model)
    result, run_dir, payload = _run_batch(setup, capsys, verify=True)
    assert result["tasks"]["a"]["status"] == "completed"
    receipt = _assert_binding(run_dir, payload, payload["verification"], status="failed")
    assert receipt["commands"][0]["exit_code"] == 7
    candidate_tree = subprocess.check_output(["git", "-C", checkout, "rev-parse", "HEAD^{tree}"], text=True).strip()
    assert receipt["tree_fingerprint"] == candidate_tree
    assert not Path(receipt["target"]).exists()


def test_verifier_that_changes_worktree_cannot_report_success(
    setup, linux_sandbox, configured_repo, capsys, monkeypatch
):
    def model(agent, prompt, **kwargs):
        (Path(kwargs["cwd"]) / "selected.py").write_text(
            "from pathlib import Path\nPath('verifier-write.txt').write_text('changed')\n",
            encoding="utf-8",
        )
        return agents.AgentResult(text="done", ok=True)

    monkeypatch.setattr(agents, "run_agent", model)
    _, run_dir, payload = _run_batch(setup, capsys, verify=True)
    receipt = _receipt(run_dir)
    assert receipt["commands"][0]["exit_code"] == 0
    assert receipt["tree_fingerprint"] != receipt["tree_fingerprint_after"]
    assert payload["verification"]["status"] == "failed"


def test_legacy_spec_replays_with_explicit_false_without_verification(setup, configured_repo, capsys):
    first = _run_batch(setup, capsys)
    assert _run_batch(setup, capsys, verify=False) == first
    _, run_dir, payload = first
    assert "verification" not in payload
    assert "verification_skipped" not in payload
    assert not (run_dir / "batch-verification").exists()
    assert len(setup[1]) == 1


def test_existing_export_projection_omits_local_verification(setup, linux_sandbox, configured_repo, capsys, tmp_path):
    result, run_dir, _ = _run_batch(setup, capsys, verify=True)
    result_ref = result["tasks"]["a"]["result_ref"]
    staged = tmp_path / "staged"
    staged.mkdir()
    shutil.copyfile(run_dir / result_ref, staged / result_ref)
    shutil.copytree(run_dir / "batch-verification", staged / "batch-verification")
    assert run_export_projection.filter_export_files(staged)
    assert not any(path.is_file() for path in (staged / "batch-verification").rglob("*"))
    run_export_projection.project_payload_tree(staged, workspace=setup[0], run_dir=run_dir, key=b"test-key")
    projected = json.loads((staged / result_ref).read_text())
    assert "verification" not in projected
    assert "coordinator check ran" not in json.dumps(projected)


def test_unconfigured_planner_skips_verification(setup, capsys):
    root, _ = setup
    (root / ".git" / "info" / "exclude").write_text(".brigade/\nbatch.json\nhome/\n")
    _commit(root, "base", allow_empty=True)
    _, run_dir, payload = _run_batch(setup, capsys, verify=True)
    assert payload["verification_skipped"] == "planner-unconfigured"
    assert "verification" not in payload
    assert not (run_dir / "batch-verification").exists()


@pytest.fixture
def script_runner(setup, configured_repo):
    root, _ = setup
    runner = root / "scripts" / "verify-focused"
    runner.parent.mkdir()
    runner.write_text('#!/bin/sh\nexec "${PY:-.venv/bin}/python" "$@"\n', encoding="utf-8")
    runner.chmod(0o755)
    config = root / "verify" / "required-tests.json"
    payload = json.loads(config.read_text())
    payload["run_argv"] = ["./scripts/verify-focused", "{tests}"]
    config.write_text(json.dumps(payload))
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    _commit(root, "use repository runner")


def test_ignored_worker_interpreter_does_not_run(setup, linux_sandbox, script_runner, capsys, monkeypatch):
    def model(agent, prompt, **kwargs):
        checkout = Path(kwargs["cwd"])
        interpreter = checkout / ".venv/bin/python"
        interpreter.parent.mkdir(parents=True)
        interpreter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        interpreter.chmod(0o755)
        (checkout / "selected.py").write_text("raise SystemExit(7)\n", encoding="utf-8")
        return agents.AgentResult(text="done", ok=True)

    monkeypatch.setattr(agents, "run_agent", model)
    _, run_dir, payload = _run_batch(setup, capsys, verify=True)
    assert payload["verification"]["status"] == "failed"
    assert _receipt(run_dir)["commands"][0]["exit_code"] == 7


@pytest.mark.parametrize(
    "change",
    [
        "script",
        pytest.param("symlink", marks=pytest.mark.skipif(sys.platform == "win32", reason="requires POSIX symlinks")),
        "conftest",
        "force-added-interpreter",
    ],
)
def test_verification_infrastructure_change_is_skipped(setup, script_runner, capsys, monkeypatch, change):
    def model(agent, prompt, **kwargs):
        checkout = Path(kwargs["cwd"])
        runner = checkout / "scripts/verify-focused"
        if change == "script":
            runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        elif change == "symlink":
            runner.unlink()
            runner.symlink_to("../selected.py")
        elif change == "conftest":
            (checkout / "conftest.py").write_text("raise RuntimeError('unexpected execution')\n", encoding="utf-8")
        else:
            interpreter = checkout / ".venv/bin/python"
            interpreter.parent.mkdir(parents=True)
            interpreter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(checkout), "add", "-f", ".venv/bin/python"], check=True)
        return agents.AgentResult(text="done", ok=True)

    monkeypatch.setattr(agents, "run_agent", model)
    _, run_dir, payload = _run_batch(setup, capsys, verify=True)
    assert payload["verification_skipped"] == "verify-infra-modified"
    assert "verification" not in payload
    assert not (run_dir / "batch-verification").exists()


def test_git_identity_ignores_inherited_git_environment(setup, linux_sandbox, configured_repo, capsys, monkeypatch):
    run = verification._run_verify_commands

    def run_with_ambient_env(*args, **kwargs):
        with monkeypatch.context() as env:
            env.setenv("GIT_DIR", str(setup[0] / "unrelated-git-dir"))
            env.setenv("GIT_WORK_TREE", str(setup[0]))
            env.setenv("PY", "missing-worker-tools")
            return run(*args, **kwargs)

    monkeypatch.setattr(verification, "_run_verify_commands", run_with_ambient_env)
    _, run_dir, payload = _run_batch(setup, capsys, verify=True)
    receipt = _assert_binding(run_dir, payload, payload["verification"], status="completed")
    assert receipt["commands"][0]["stdout_summary"] == "coordinator check ran"
    assert not Path(receipt["target"]).exists()


def test_trusted_repository_runner_completes_and_cleans_snapshot(setup, linux_sandbox, script_runner, capsys):
    _, run_dir, payload = _run_batch(setup, capsys, verify=True)
    receipt = _assert_binding(run_dir, payload, payload["verification"], status="completed")
    assert receipt["commands"][0]["stdout_summary"] == "coordinator check ran"
    assert not Path(receipt["target"]).parent.exists()


def test_unavailable_candidate_tree_has_closed_skip_reason(setup, configured_repo, capsys, monkeypatch):
    def unavailable(*args, **kwargs):
        raise OSError("fixture Git failure")

    monkeypatch.setattr(run_batch_verify_tree, "fingerprint", unavailable)
    _, run_dir, payload = _run_batch(setup, capsys, verify=True)
    assert payload["verification_skipped"] == "tree-unavailable"
    assert "verification" not in payload
    assert not (run_dir / "batch-verification").exists()


def test_sandbox_unavailable_skips_batch_verification(setup, configured_repo, capsys, monkeypatch):
    from brigade import run_batch_verify_sandbox

    # Exercise the real platform fallback on Windows.
    if sys.platform == "linux":
        monkeypatch.setattr(run_batch_verify_sandbox, "bwrap", lambda: None)
    materializations = []
    materialize = run_batch_verify_tree.materialize

    def record_materialization(*args, **kwargs):
        materializations.append(True)
        return materialize(*args, **kwargs)

    monkeypatch.setattr(run_batch_verify_tree, "materialize", record_materialization)
    result, run_dir, payload = _run_batch(setup, capsys, verify=True)
    assert result["tasks"]["a"]["status"] == "completed"
    assert payload["verification_skipped"] == "sandbox-unavailable"
    assert materializations == []
    assert not (run_dir / "batch-verification").exists()


@pytest.mark.parametrize("destination", ["/somewhere", "../outside"])
@pytest.mark.skipif(sys.platform == "win32", reason="requires POSIX symlinks")
def test_candidate_symlink_escape_skips_batch_verification(
    setup, configured_repo, linux_sandbox, capsys, monkeypatch, destination
):
    def model(agent, prompt, **kwargs):
        (Path(kwargs["cwd"]) / "outside.py").symlink_to(destination)
        return agents.AgentResult(text="done", ok=True)

    monkeypatch.setattr(agents, "run_agent", model)
    _, run_dir, payload = _run_batch(setup, capsys, verify=True)
    assert payload["verification_skipped"] == "candidate-symlink-escape"
    assert not (run_dir / "batch-verification").exists()


@pytest.fixture
def linux_sandbox():
    from brigade import run_batch_verify_sandbox

    if run_batch_verify_sandbox.bwrap() is None:
        pytest.skip("Linux bubblewrap namespaces are unavailable")


def test_sandbox_hides_installer_metadata_and_runs_fake_venv(linux_sandbox, tmp_path, monkeypatch):
    from brigade import run_batch_verify_sandbox

    binary = run_batch_verify_sandbox.bwrap()
    assert binary is not None
    scratch = tmp_path / "runs" / "task" / "scratch"
    checkout = scratch / "candidate"
    (checkout / ".git").mkdir(parents=True)
    env = run_batch_verify_tree.command_environment(scratch, run_batch_verify_tree.git_environment())
    # Bubblewrap gives /tmp its own mount. Put the fake tool prefix outside it.
    base = Path(__file__).resolve().parents[1] / ".brigade"
    with tempfile.TemporaryDirectory(prefix="toolchain-fixture-", dir=base) as directory:
        prefix = Path(directory)
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(prefix)], check=True)
        python = prefix / "bin" / "python"
        packages = prefix / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
        info = packages / "sandbox_fixture-1.0.dist-info"
        info.mkdir()
        (packages / "sandbox_fixture.py").write_text("value = 42\n", encoding="utf-8")
        metadata = [
            prefix / "pipx_metadata.json",
            packages / "pipx_metadata.json",
            info / "direct_url.json",
            prefix / "prefix-only.txt",
        ]
        for path in metadata:
            path.write_text("private fixture value\n", encoding="utf-8")
        unrelated = tmp_path / "unrelated"
        unrelated.mkdir()
        (unrelated / "private.txt").write_text("private fixture value\n", encoding="utf-8")
        (prefix / "Scripts").symlink_to(unrelated, target_is_directory=True)
        metadata.append(prefix / "Scripts" / "private.txt")
        monkeypatch.setattr(sys, "prefix", str(prefix))
        monkeypatch.setattr(sys, "exec_prefix", str(prefix))

        sandbox = run_batch_verify_sandbox.prepare(binary, checkout, env)
        assert sandbox is not None
        # lib64 can be a second mount of the same site-packages on Linux.
        if (prefix / "lib64").is_dir():
            metadata.append(prefix / "lib64" / metadata[2].relative_to(prefix / "lib"))
        probe = (
            "import json, sys, sandbox_fixture\n"
            "from pathlib import Path\n"
            "assert sandbox_fixture.value == 42\n"
            "for name in json.loads(sys.argv[1]):\n"
            "    try:\n"
            "        Path(name).read_bytes()\n"
            "    except (FileNotFoundError, PermissionError):\n"
            "        continue\n"
            "    raise AssertionError('installer metadata is readable')\n"
        )
        command = sandbox.command([str(python), "-P", "-s", "-c", probe, json.dumps([str(p) for p in metadata])])
        result = subprocess.run(command, capture_output=True, text=True, timeout=20, check=False)
        assert result.returncode == 0, result.stderr


def test_sandbox_hides_coordinator_files_and_network(setup, configured_repo, linux_sandbox, capsys, monkeypatch):
    root, _ = setup
    home = root.parent / "home"
    secret = home / "private-fixture.txt"
    secret.write_text("fixture", encoding="utf-8")
    coordinator_config = Path.home() / ".config"

    def model(agent, prompt, **kwargs):
        run = next((root / ".brigade" / "runs").glob("batch-*"))
        (Path(kwargs["cwd"]) / "selected.py").write_text(
            "import os, pathlib, socket\n"
            f"assert not pathlib.Path({str(secret)!r}).exists()\n"
            f"assert not pathlib.Path({str(run)!r}).exists()\n"
            f"assert not pathlib.Path({str(home)!r}).exists()\n"
            f"assert not pathlib.Path({str(coordinator_config)!r}).exists()\n"
            "assert pathlib.Path.home().is_dir()\n"
            "assert 'BRIGADE_HOME' not in os.environ\n"
            "assert socket.if_nameindex() == [(1, 'lo')]\n"
            "with socket.socket() as peer:\n"
            "    peer.settimeout(0.5)\n"
            "    try:\n"
            "        peer.connect(('192.0.2.1', 80))\n"
            "    except OSError:\n"
            "        pass\n"
            "    else:\n"
            "        raise AssertionError('unexpected network route')\n",
            encoding="utf-8",
        )
        return agents.AgentResult(text="done", ok=True)

    monkeypatch.setattr(agents, "run_agent", model)
    _, run_dir, payload = _run_batch(setup, capsys, verify=True)
    _assert_binding(run_dir, payload, payload["verification"], status="completed")


def _configure_python_package(root, package, *, pytest_runner=False):
    config = root / "verify/required-tests.json"
    data = json.loads(config.read_text())
    data["import_packages"] = [package]
    if pytest_runner:
        data["run_argv"] = ["pytest", "{tests}"]
    config.write_text(json.dumps(data))
    source = root / "src" / package
    source.mkdir(parents=True)
    (source / "__init__.py").write_text("VALUE = 'candidate'\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    _commit(root, "configure candidate imports")


@pytest.mark.parametrize("package", ["pytest", "sys"])
def test_candidate_import_probe_rejects_outside_package(
    setup, configured_repo, linux_sandbox, capsys, monkeypatch, package
):
    _configure_python_package(setup[0], package)

    def model(agent, prompt, **kwargs):
        # The coordinator interpreter also has an installed or built-in copy.
        (Path(kwargs["cwd"]) / "src" / package / "__init__.py").unlink()
        return agents.AgentResult(text="done", ok=True)

    monkeypatch.setattr(agents, "run_agent", model)
    _, run_dir, payload = _run_batch(setup, capsys, verify=True)
    assert payload["verification_skipped"] == "candidate-import-mismatch"
    assert not (run_dir / "batch-verification").exists()


def test_pytest_imports_candidate_src_layout(setup, configured_repo, linux_sandbox, capsys, monkeypatch):
    _configure_python_package(setup[0], "fixture_project", pytest_runner=True)

    def model(agent, prompt, **kwargs):
        (Path(kwargs["cwd"]) / "selected.py").write_text(
            "import fixture_project\n\ndef test_value():\n    assert fixture_project.VALUE == 'candidate'\n",
            encoding="utf-8",
        )
        return agents.AgentResult(text="done", ok=True)

    monkeypatch.setattr(agents, "run_agent", model)
    _, run_dir, payload = _run_batch(setup, capsys, verify=True)
    receipt = _assert_binding(run_dir, payload, payload["verification"], status="completed")
    assert not Path(receipt["target"]).exists()


def test_sandbox_timeout_kills_detached_child_and_cleans_checkout(
    setup, configured_repo, linux_sandbox, capsys, monkeypatch
):
    from brigade import run_batch_verification

    monkeypatch.setattr(run_batch_verification, "_VERIFY_TIMEOUT", 5)
    marker = "brigade-batch-timeout-fixture-" + secrets.token_hex(8)

    def model(agent, prompt, **kwargs):
        (Path(kwargs["cwd"]) / "selected.py").write_text(
            "import subprocess, sys, time\n"
            f"subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)', {marker!r}], "
            "start_new_session=True)\n"
            "print('detached child started', flush=True)\n"
            "time.sleep(30)\n",
            encoding="utf-8",
        )
        return agents.AgentResult(text="done", ok=True)

    monkeypatch.setattr(agents, "run_agent", model)
    _, run_dir, payload = _run_batch(setup, capsys, verify=True)
    receipt = _receipt(run_dir)
    assert payload["verification"]["status"] == "failed"
    assert receipt["commands"][0]["status"] == "timed_out"
    assert "detached child started" in receipt["commands"][0]["stdout_summary"]
    assert not Path(receipt["target"]).parent.exists()
    for cmdline in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            assert marker.encode() not in cmdline.read_bytes().split(b"\0")
        except OSError:
            continue
