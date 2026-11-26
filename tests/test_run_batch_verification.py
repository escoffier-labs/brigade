"""Coordinator-owned verification through the guarded batch CLI."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from brigade import agents, cli, localio, run_batch_verify_tree, run_export_projection, run_tasks
from brigade.run_batch import SCHEMA
from brigade.work_cmd import verification
from tests.test_run_batch_execute import _commit, task
from tests.test_run_batch_execute import setup as setup


def _run_batch(setup, capsys, *, isolation="worktree", verify=None):
    root, _ = setup
    path = root / "batch.json"
    spec = {"schema": SCHEMA, "request_id": "req", "tasks": [{**task("a"), "isolation": isolation}]}
    if verify is not None:
        spec["verify"] = verify
    path.write_text(json.dumps(spec), encoding="utf-8")
    rc = cli.main(["run", "batch", str(path), "--cwd", str(root), "--json"])
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    result = json.loads(captured.out)
    run_dir = root / ".brigade" / "runs" / result["run_id"]
    payload = json.loads((run_dir / result["tasks"]["a"]["result_ref"]).read_text())
    return result, run_dir, payload


@pytest.fixture
def configured_repo(setup):
    root, _ = setup
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
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
    }
    assert receipt["tree_fingerprint"] == receipt["tree_fingerprint_after"]
    assert receipt["tree_fingerprint"]
    assert "selected.py" not in json.dumps(payload)
    return receipt


def test_isolated_task_records_coordinator_verification(setup, configured_repo, capsys, monkeypatch):
    monkeypatch.setenv("BRIGADE_ATTEMPT_ID", "unrelated-caller-value")
    _, run_dir, payload = _run_batch(setup, capsys, verify=True)
    receipt = _assert_binding(run_dir, payload, payload["verification"], status="completed")
    assert receipt["commands"][0]["stdout_summary"] == "coordinator check ran"


def test_isolated_task_ignores_planted_receipt(setup, configured_repo, capsys, monkeypatch):
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


def test_failed_verifier_records_failure_and_batch_finishes(setup, configured_repo, capsys, monkeypatch):
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


def test_verifier_that_changes_worktree_cannot_report_success(setup, configured_repo, capsys, monkeypatch):
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


def test_existing_export_projection_omits_local_verification(setup, configured_repo, capsys, tmp_path):
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
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
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


def test_ignored_worker_interpreter_does_not_run(setup, script_runner, capsys, monkeypatch):
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


@pytest.mark.parametrize("change", ["script", "symlink", "conftest", "force-added-interpreter"])
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


def test_git_identity_ignores_inherited_git_environment(setup, configured_repo, capsys, monkeypatch):
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


def test_trusted_repository_runner_completes_and_cleans_snapshot(setup, script_runner, capsys):
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
