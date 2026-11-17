"""Native T3 enrollment custody: durable references, never a launch."""

from __future__ import annotations

import json
import stat
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from brigade import cli, native_t3_enrollment as enrollment, proc, run_journal, run_tasks
from brigade.native_t3_contract import NativeIntentTemplate

TASK_TEXT = "retained-input-marker"
ATTEMPT = "a" * 64


def _git(repo: Path, *args: str):
    result = proc.run(["git", *args], cwd=repo)
    assert result.code == 0, result.stderr
    return result


def _roster_text(*, targets: tuple[str, ...] = ("coder",), isolation: str = "worktree") -> str:
    lines = ['orchestrator = "coder"', ""]
    for agent, role in (("coder", "coder"), ("reviewer", "reviewer")):
        lines += [f"[agents.{agent}]", 'cli = "codex"', f'role = "{role}"', ""]
    for name in targets:
        lines += [
            f"[t3_targets.{name}]",
            'origin_environment = "env-origin"',
            'origin_thread = "thread-parent"',
            'target_environment = "env-target"',
            'target_project = "project-test"',
            'provider_instance = "codex-test"',
            'driver = "codex"',
            'model = "launch-model"',
            f'isolation = "{isolation}"',
            "",
            f"[t3_targets.{name}.options]",
            'reasoningEffort = "high"',
            "",
        ]
    return "\n".join(lines)


def _workspace(tmp_path: Path, *, targets: tuple[str, ...] = ("coder",), isolation: str = "worktree") -> Path:
    repo = tmp_path / "workspace"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test User")
    (repo / "tracked.txt").write_text("base\n")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "initial")
    brigade_dir = repo / ".brigade"
    brigade_dir.mkdir()
    (brigade_dir / "roster.toml").write_text(_roster_text(targets=targets, isolation=isolation), encoding="utf-8")
    return repo


def _batch(
    tmp_path: Path,
    *,
    request_id: str = "request-1",
    seat: str = "coder",
    ids: tuple[str, ...] = ("one", "two"),
    text: str = TASK_TEXT,
) -> Path:
    payload = {
        "schema": "brigade.run_batch.v1",
        "request_id": request_id,
        "tasks": [
            {"task_id": task_id, "text": f"{text} {task_id}", "seat": seat, "isolation": "worktree"} for task_id in ids
        ],
    }
    path = tmp_path / f"batch-{request_id}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _run_dir(workspace: Path, run_id: str) -> Path:
    return workspace / ".brigade" / "runs" / run_id


def _open(batch: Path, workspace: Path) -> int:
    return cli.main(["t3", "open", str(batch), "--target", str(workspace)])


def test_open_enrolls_two_tasks_on_one_seat(tmp_path, capsys):
    workspace = _workspace(tmp_path)
    assert _open(_batch(tmp_path), workspace) == 0
    out = capsys.readouterr().out
    assert TASK_TEXT not in out
    report = json.loads(out)
    assert report["schema"] == enrollment.SCHEMA
    assert [entry["task_id"] for entry in report["tasks"]] == ["one", "two"]
    for entry in report["tasks"]:
        assert set(entry) == {"task_id", "seat", "state", "target"}
        assert entry["seat"] == "coder" and entry["state"] == enrollment.ENROLLED_STATE
        assert entry["target"] == {"provider_instance": "codex-test", "model": "launch-model"}

    run_dir = _run_dir(workspace, report["run_id"])
    results = run_tasks.read_tasks(run_dir)
    assert [result.handle.spec.task_id for result in results] == ["one", "two"]
    assert all(result.status == "queued" and result.enrollment is not None for result in results)
    assets = {result.enrollment.asset_path for result in results}
    assert len(assets) == 2
    for name in assets:
        asset = run_dir / name
        assert stat.S_IMODE(asset.stat().st_mode) == 0o600
        assert TASK_TEXT.encode("utf-8") in asset.read_bytes()

    journal_path = run_dir / "events" / "lifecycle.jsonl"
    assert TASK_TEXT not in journal_path.read_text(encoding="utf-8")
    events = run_journal.read_journal_bounded(journal_path).events
    enrolled = [event for event in events if event.event_type == "task.native-enrolled"]
    assert len(enrolled) == 2
    assert all(
        set(event.payload) == {"descriptor_version", "task_id", "asset_path", "asset_digest", "template_digest"}
        for event in enrolled
    )
    assert json.loads((run_dir / "run.json").read_text(encoding="utf-8"))["status"] == (
        enrollment.AWAITING_NATIVE_PARENT
    )


@pytest.mark.parametrize(
    ("roster_kwargs", "seat", "dirty", "code"),
    [
        ({"targets": ()}, "coder", False, "seat-unbound"),
        ({"isolation": "shared"}, "coder", False, "target-isolation"),
        ({"targets": ("coder",)}, "reviewer", False, "seat-unbound"),
        ({}, "coder", True, "source-dirty"),
    ],
)
def test_refusals_are_bounded_and_write_nothing(tmp_path, capsys, roster_kwargs, seat, dirty, code):
    workspace = _workspace(tmp_path, **roster_kwargs)
    if dirty:
        (workspace / "tracked.txt").write_text("changed\n", encoding="utf-8")
    assert _open(_batch(tmp_path, seat=seat, ids=("one",)), workspace) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"[{code}]" in captured.err and TASK_TEXT not in captured.err
    assert not (workspace / ".brigade" / "runs").exists()
    assert not (workspace / ".brigade" / "run.lock").exists()


def test_asset_is_owner_only_append_only_and_digest_checked(tmp_path):
    workspace = _workspace(tmp_path)
    report = enrollment.enroll(batch_path=_batch(tmp_path), target=workspace)
    run_dir = _run_dir(workspace, report["run_id"])
    result = run_tasks.read_tasks(run_dir)[0]
    asset = run_dir / result.enrollment.asset_path
    original = asset.read_bytes()
    assert stat.S_IMODE(asset.stat().st_mode) == 0o600

    retained = enrollment.read_delegation_input(run_dir, task_id="one")
    assert retained.text == f"{TASK_TEXT} one"
    assert retained.target.provider_instance == "codex-test"

    with pytest.raises(enrollment.EnrollmentError) as refused:
        enrollment.write_delegation_asset(run_dir, result.enrollment.asset_path, b"{}")
    assert refused.value.code == "asset-conflict"
    assert asset.read_bytes() == original

    asset.write_bytes(original.replace(TASK_TEXT.encode("utf-8"), b"tampered-marker!!!"))
    with pytest.raises(enrollment.EnrollmentError) as tampered:
        enrollment.read_delegation_input(run_dir, task_id="one")
    assert tampered.value.code == "asset-digest"


def test_second_open_is_idempotent_and_changed_input_conflicts(tmp_path):
    workspace = _workspace(tmp_path)
    batch = _batch(tmp_path)
    first = enrollment.enroll(batch_path=batch, target=workspace)
    journal_path = _run_dir(workspace, first["run_id"]) / "events" / "lifecycle.jsonl"
    before = journal_path.read_bytes()
    assert enrollment.enroll(batch_path=batch, target=workspace) == first
    assert journal_path.read_bytes() == before
    with pytest.raises(enrollment.EnrollmentError) as refused:
        enrollment.enroll(batch_path=_batch(tmp_path, text="different retained input"), target=workspace)
    assert refused.value.code == "request-conflict"
    assert journal_path.read_bytes() == before


def test_enrolled_run_is_not_resumable(tmp_path, capsys):
    workspace = _workspace(tmp_path)
    report = enrollment.enroll(batch_path=_batch(tmp_path), target=workspace)
    assert cli.main(["runs", "resume", report["run_id"], "--cwd", str(workspace)]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "cannot be resumed" in captured.err
    meta = json.loads((_run_dir(workspace, report["run_id"]) / "run.json").read_text(encoding="utf-8"))
    assert meta["status"] == enrollment.AWAITING_NATIVE_PARENT
    assert enrollment.awaiting_native_parent(meta)


def test_request_key_is_deterministic_and_domain_separated():
    first = enrollment.request_key("run-1", "task-1", ATTEMPT)
    assert first == enrollment.request_key("run-1", "task-1", ATTEMPT)
    assert first.startswith(enrollment.REQUEST_KEY_PREFIX)
    assert len(first) == len(enrollment.REQUEST_KEY_PREFIX) + 64
    variants = {
        first,
        enrollment.request_key("run-2", "task-1", ATTEMPT),
        enrollment.request_key("run-1", "task-2", ATTEMPT),
        enrollment.request_key("run-1", "task-1", "b" * 64),
    }
    assert len(variants) == 4
    # Length prefixes keep adjacent fields from colliding across a boundary.
    assert enrollment.request_key("ab", "c", ATTEMPT) != enrollment.request_key("a", "bc", ATTEMPT)
    with pytest.raises(ValueError):
        enrollment.request_key("run-1", "task-1", "not-a-digest")


def test_intent_template_is_immutable_and_binds_enrollment_values(tmp_path):
    workspace = _workspace(tmp_path)
    report = enrollment.enroll(batch_path=_batch(tmp_path, ids=("one",)), target=workspace)
    run_dir = _run_dir(workspace, report["run_id"])
    result = run_tasks.read_tasks(run_dir)[0]
    target = enrollment.read_delegation_input(run_dir, task_id="one").target
    template = NativeIntentTemplate(
        run_id=run_dir.name,
        task_id="one",
        seat="coder",
        target=target,
        payload_digest=result.handle.spec.payload_digest,
        source_revision=target.source_revision,
    )
    assert template.template_digest == result.enrollment.template_digest
    assert not hasattr(template, "attempt_id") and not hasattr(template, "dispatch_attempt")
    with pytest.raises(FrozenInstanceError):
        template.task_id = "two"
    assert replace(template, payload_digest="b" * 64).template_digest != template.template_digest
    with pytest.raises(ValueError):
        replace(template, source_revision="b" * 40)
