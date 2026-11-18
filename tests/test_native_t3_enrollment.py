"""Native T3 enrollment custody: durable references, never a launch."""

from __future__ import annotations

import ctypes
import json
import os
import stat
import struct
import subprocess
from contextlib import contextmanager
from ctypes import wintypes
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from brigade import (
    cli,
    native_t3_enrollment as enrollment,
    proc,
    run_batch,
    run_batch_execute,
    run_journal,
    run_lane_state,
    run_reap,
    run_resume,
    run_tasks,
    runs_status,
    runguard,
)
from brigade.native_t3_contract import NativeIntentTemplate
from brigade.work_cmd import nt_dirfd, nt_private

TASK_TEXT = "retained-input-marker"
ATTEMPT = "a" * 64
FULL_CONTROL = 0x001F01FF
DACL_PRESENT = 0x0004
DACL_PROTECTED = 0x1000
POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX mode-bit custody assertions")
WINDOWS_ONLY = pytest.mark.skipif(os.name != "nt", reason="Windows owner-only DACL custody")


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
    repo.mkdir(parents=True)
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
    isolation: str = "worktree",
    synthesis: dict | None = None,
) -> Path:
    payload = {
        "schema": "brigade.run_batch.v1",
        "request_id": request_id,
        "tasks": [
            {"task_id": task_id, "text": f"{text} {task_id}", "seat": seat, "isolation": isolation} for task_id in ids
        ],
        "synthesis": synthesis,
    }
    path = tmp_path / f"batch-{request_id}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _asset(run_dir: Path, task_id: str) -> Path:
    result = next(item for item in run_tasks.read_tasks(run_dir) if item.handle.spec.task_id == task_id)
    return run_dir / result.enrollment.asset_path


def _run_dir(workspace: Path, run_id: str) -> Path:
    return workspace / ".brigade" / "runs" / run_id


def _open(batch: Path, workspace: Path) -> int:
    return cli.main(["t3", "open", str(batch), "--target", str(workspace)])


def _stored_security(handle: int):
    """Read owner, control flags and raw ACEs with test-side bindings."""
    api = nt_private._security_api()
    descriptor, owner, dacl = (ctypes.c_void_p() for _ in range(3))
    try:
        error = api.GetSecurityInfo(
            handle, 1, 5, ctypes.byref(owner), None, ctypes.byref(dacl), None, ctypes.byref(descriptor)
        )
        assert error == 0, f"fixture GetSecurityInfo failed: {error}"
        assert descriptor.value
        control, revision = wintypes.WORD(), wintypes.DWORD()
        assert api.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision))
        aces = None
        if dacl.value:
            assert api.IsValidAcl(dacl)
            _, _, size, count, _ = struct.unpack("<BBHHH", ctypes.string_at(dacl, 8))
            data = ctypes.string_at(dacl, size)
            aces, offset = [], 8
            for _ in range(count):
                kind, flags, length, mask = struct.unpack_from("<BBHI", data, offset)
                assert length >= 16 and offset + length <= size
                aces.append((kind, flags, mask, data[offset + 8 : offset + length]))
                offset += length
        return nt_private._copy_sid(api, owner), control.value, aces
    finally:
        if descriptor.value:
            api.LocalFree(descriptor)


def _assert_owner_only(handle: int) -> None:
    """Assert a protected DACL with exactly one unflagged owner allow ACE."""
    owner = nt_private._effective_owner(nt_private._security_api())
    stored_owner, control, aces = _stored_security(handle)
    assert stored_owner == owner
    assert control & DACL_PRESENT and control & DACL_PROTECTED
    assert aces == [(0, 0, FULL_CONTROL, owner)]


@contextmanager
def _native_descriptor(sddl: str):
    """Build an unsafe test fixture without adding setters to production."""
    api = nt_private._security_api()
    value = ctypes.c_void_p()
    try:
        if not api.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(value), None):
            raise nt_dirfd._win_error()
        yield value.value
    finally:
        if value.value:
            api.LocalFree(value)


def _set_fixture_security(path: Path, sddl: str, *, information: int) -> None:
    """Install an exact DACL on a temporary test object only."""
    api = ctypes.WinDLL("advapi32", use_last_error=True)
    api.GetSecurityDescriptorOwner.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL),
    ]
    api.GetSecurityDescriptorOwner.restype = wintypes.BOOL
    api.GetSecurityDescriptorDacl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL),
    ]
    api.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    api.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    api.SetNamedSecurityInfoW.restype = wintypes.DWORD
    with _native_descriptor(sddl) as descriptor:
        owner, dacl = ctypes.c_void_p(), ctypes.c_void_p()
        defaulted, present = wintypes.BOOL(), wintypes.BOOL()
        if information & 1:
            assert api.GetSecurityDescriptorOwner(descriptor, ctypes.byref(owner), ctypes.byref(defaulted))
        if information & 4:
            assert api.GetSecurityDescriptorDacl(
                descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)
            )
            assert present.value
        error = api.SetNamedSecurityInfoW(str(path), 1, information, owner, None, dacl, None)
        assert error == 0, f"fixture SetNamedSecurityInfoW failed: {error}"


@POSIX_ONLY
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
    assert stat.S_IMODE(run_dir.stat().st_mode) == 0o700
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
@POSIX_ONLY
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


@pytest.mark.parametrize(
    ("batch_kwargs", "code"),
    [
        ({"isolation": "shared", "ids": ("one",)}, "isolation-required"),
        ({"synthesis": {"text": "combine", "seat": "coder"}}, "synthesis-unsupported"),
    ],
)
@POSIX_ONLY
def test_batch_shape_refusals_write_nothing(tmp_path, capsys, batch_kwargs, code):
    workspace = _workspace(tmp_path)
    assert _open(_batch(tmp_path, **batch_kwargs), workspace) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and f"[{code}]" in captured.err
    assert not (workspace / ".brigade" / "runs").exists()


@POSIX_ONLY
def test_asset_is_owner_only_append_only_and_digest_checked(tmp_path):
    workspace = _workspace(tmp_path)
    report = enrollment.enroll(batch_path=_batch(tmp_path), target=workspace)
    run_dir = _run_dir(workspace, report["run_id"])
    asset = _asset(run_dir, "one")
    original = asset.read_bytes()
    assert stat.S_IMODE(asset.stat().st_mode) == 0o600

    retained = enrollment.read_delegation_input(run_dir, task_id="one")
    assert retained.text == f"{TASK_TEXT} one"
    assert retained.target.provider_instance == "codex-test"

    with pytest.raises(enrollment.EnrollmentError) as refused:
        enrollment.write_delegation_asset(run_dir, asset.name, b"{}")
    assert refused.value.code == "asset-conflict"
    assert asset.read_bytes() == original

    asset.write_bytes(original.replace(TASK_TEXT.encode("utf-8"), b"tampered-marker!!!"))
    with pytest.raises(enrollment.EnrollmentError) as tampered:
        enrollment.read_delegation_input(run_dir, task_id="one")
    assert tampered.value.code == "asset-digest"


@POSIX_ONLY
def test_custody_refuses_a_planted_symlink(tmp_path):
    workspace = _workspace(tmp_path)
    batch = _batch(tmp_path)
    report = enrollment.enroll(batch_path=batch, target=workspace)
    run_dir = _run_dir(workspace, report["run_id"])
    journal_path = run_dir / "events" / "lifecycle.jsonl"
    asset = _asset(run_dir, "one")
    outside = tmp_path / "outside.json"
    outside.write_bytes(asset.read_bytes())
    asset.unlink()
    try:
        os.symlink(outside, asset)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable on this platform")
    with pytest.raises(enrollment.EnrollmentError) as symlinked:
        enrollment.read_delegation_input(run_dir, task_id="one")
    assert symlinked.value.code == "asset-custody"
    before = journal_path.read_bytes()
    with pytest.raises(enrollment.EnrollmentError) as replay:
        enrollment.enroll(batch_path=batch, target=workspace)
    assert replay.value.code == "asset-custody"
    assert journal_path.read_bytes() == before


@POSIX_ONLY
def test_custody_refuses_a_hardlinked_asset(tmp_path):
    workspace = _workspace(tmp_path)
    batch = _batch(tmp_path)
    report = enrollment.enroll(batch_path=batch, target=workspace)
    run_dir = _run_dir(workspace, report["run_id"])
    asset = _asset(run_dir, "one")
    try:
        os.link(asset, tmp_path / "linked.json")
    except (OSError, NotImplementedError):
        pytest.skip("hardlink creation is unavailable on this platform")
    with pytest.raises(enrollment.EnrollmentError) as linked:
        enrollment.read_delegation_input(run_dir, task_id="one")
    assert linked.value.code == "asset-custody"
    with pytest.raises(enrollment.EnrollmentError) as replay:
        enrollment.enroll(batch_path=batch, target=workspace)
    assert replay.value.code == "asset-custody"


@POSIX_ONLY
def test_custody_refuses_a_widened_mode(tmp_path):
    workspace = _workspace(tmp_path)
    batch = _batch(tmp_path)
    report = enrollment.enroll(batch_path=batch, target=workspace)
    run_dir = _run_dir(workspace, report["run_id"])
    os.chmod(_asset(run_dir, "one"), 0o644)
    with pytest.raises(enrollment.EnrollmentError) as widened:
        enrollment.read_delegation_input(run_dir, task_id="one")
    assert widened.value.code == "asset-custody"
    with pytest.raises(enrollment.EnrollmentError) as replay:
        enrollment.enroll(batch_path=batch, target=workspace)
    assert replay.value.code == "asset-custody"


@POSIX_ONLY
def test_enrolled_run_reports_non_terminal_and_is_not_reaped(tmp_path, capsys):
    workspace = _workspace(tmp_path)
    report = enrollment.enroll(batch_path=_batch(tmp_path), target=workspace)
    assert cli.main(["runs", "show", report["run_id"], "--cwd", str(workspace)]) == 0
    shown = capsys.readouterr().out
    assert "terminal: no" in shown
    assert "terminal: yes" not in shown

    lanes = run_lane_state.collect_lanes(workspace, now=datetime.now(timezone.utc))
    lane = next(item for item in lanes if item.run_dir is not None and item.run_dir.name == report["run_id"])
    assert run_lane_state.classify(lane, datetime.now(timezone.utc), run_lane_state.Thresholds()).state == "waiting"

    run_dir = _run_dir(workspace, report["run_id"])
    run_json = run_dir / "run.json"
    before = run_json.read_bytes()
    clock = datetime.now(timezone.utc)
    meta = json.loads(before)
    # The reaper's selection rule and its reapable set both exclude this state,
    # so it can never be terminalized locally. The full reap transaction is run
    # where the existing reaper suite is verified (POSIX).
    assert enrollment.AWAITING_NATIVE_PARENT not in run_reap._REAPABLE_STATUSES
    assert run_reap._revalidate_metadata(meta, older_than=timedelta(seconds=1), now=clock) == "externally-controlled"
    assert run_reap.reap(cwd=workspace, runs_dir=None, older_than="1s", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reaped"] == []
    assert payload["skipped"] == [{"run_id": report["run_id"], "reason": "externally-controlled"}]
    assert run_json.read_bytes() == before


@POSIX_ONLY
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


@POSIX_ONLY
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


@POSIX_ONLY
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


@POSIX_ONLY
def test_native_enrolled_task_refuses_generic_launch(tmp_path):
    workspace = _workspace(tmp_path)
    report = enrollment.enroll(batch_path=_batch(tmp_path, ids=("one",)), target=workspace)
    run_dir = _run_dir(workspace, report["run_id"])
    with runguard.run_lock(workspace, run_dir=run_dir):
        with pytest.raises(run_tasks.TaskJournalError):
            run_tasks.prepare_attempt(run_dir, workspace=workspace, task_id="one")
        handle = run_tasks.prepare_attempt(run_dir, workspace=workspace, task_id="one", native_governed=True)
        version = next(item.version for item in run_tasks.read_tasks(run_dir) if item.handle.spec.task_id == "one")
        with pytest.raises(run_tasks.TaskJournalError):
            run_tasks.claim_task_launch(
                run_dir,
                workspace=workspace,
                handle=handle,
                expected_version=version,
                seat="coder",
                dispatch_attempt=1,
            )
        decision = run_tasks.claim_task_launch(
            run_dir,
            workspace=workspace,
            handle=handle,
            expected_version=version,
            seat="coder",
            dispatch_attempt=1,
            native_governed=True,
        )
        assert decision.outcome == "granted"


@POSIX_ONLY
def test_generic_launch_paths_refuse_an_enrolled_run(tmp_path, capsys):
    workspace = _workspace(tmp_path)
    report = enrollment.enroll(batch_path=_batch(tmp_path, ids=("one",)), target=workspace)
    run_dir = _run_dir(workspace, report["run_id"])
    meta = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert runs_status.refuse_launch(meta)
    assert "cannot be launched" in capsys.readouterr().err
    with pytest.raises(run_batch.BatchError) as refused:
        run_batch_execute._refuse_externally_controlled(run_dir)
    assert refused.value.code == "native-enrolled-run"
    assert run_resume.resume(run_dir) == 2
    assert "cannot be resumed" in capsys.readouterr().err


@WINDOWS_ONLY
def test_windows_open_creates_owner_only_run_dir_and_assets(tmp_path, capsys):
    workspace = _workspace(tmp_path)
    assert _open(_batch(tmp_path), workspace) == 0
    out = capsys.readouterr().out
    assert TASK_TEXT not in out
    report = json.loads(out)
    run_dir = _run_dir(workspace, report["run_id"])
    parent = nt_dirfd.open_root_directory(run_dir.parent)
    try:
        descriptor = nt_private.open_child_directory(parent, run_dir.name)
        try:
            _assert_owner_only(nt_dirfd._handle_from_fd(descriptor))
        finally:
            os.close(descriptor)
    finally:
        os.close(parent)
    results = run_tasks.read_tasks(run_dir)
    assert [item.handle.spec.task_id for item in results] == ["one", "two"]
    parent = nt_dirfd.open_root_directory(run_dir)
    try:
        for result in results:
            assert result.enrollment is not None
            descriptor = nt_private.open_file(parent, result.enrollment.asset_path, os.O_RDONLY)
            try:
                _assert_owner_only(nt_dirfd._handle_from_fd(descriptor))
            finally:
                os.close(descriptor)
    finally:
        os.close(parent)
    assert enrollment.read_delegation_input(run_dir, task_id="one").text == f"{TASK_TEXT} one"


@WINDOWS_ONLY
def test_windows_readback_refuses_a_widened_asset_dacl(tmp_path):
    workspace = _workspace(tmp_path)
    batch = _batch(tmp_path)
    report = enrollment.enroll(batch_path=batch, target=workspace)
    run_dir = _run_dir(workspace, report["run_id"])
    owner = nt_private._sid_string(nt_private._effective_owner(nt_private._security_api()))
    _set_fixture_security(_asset(run_dir, "one"), f"D:P(A;;FA;;;{owner})(A;;FR;;;WD)", information=4 | 0x80000000)
    with pytest.raises(enrollment.EnrollmentError) as refused:
        enrollment.read_delegation_input(run_dir, task_id="one")
    assert refused.value.code == "asset-custody"
    with pytest.raises(enrollment.EnrollmentError) as replay:
        enrollment.enroll(batch_path=batch, target=workspace)
    assert replay.value.code == "asset-custody"


@WINDOWS_ONLY
def test_windows_refuses_a_pre_existing_unverified_run_dir(tmp_path):
    workspace = _workspace(tmp_path)
    batch = _batch(tmp_path)
    plan = enrollment.plan_enrollment(workspace=workspace, batch_path=batch)
    plan.run_dir.mkdir(parents=True)
    owner = nt_private._sid_string(nt_private._effective_owner(nt_private._security_api()))
    _set_fixture_security(plan.run_dir, f"D:(A;;FA;;;{owner})", information=4 | 0x20000000)
    with pytest.raises(enrollment.EnrollmentError) as refused:
        enrollment.enroll(batch_path=batch, target=workspace)
    assert refused.value.code == "run-dir"
    assert plan.run_dir.is_dir()


@WINDOWS_ONLY
def test_windows_refuses_a_reparse_point_and_a_hardlinked_asset(tmp_path):
    workspace = _workspace(tmp_path)
    batch = _batch(tmp_path)
    plan = enrollment.plan_enrollment(workspace=workspace, batch_path=batch)
    plan.run_dir.parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(plan.run_dir), str(outside)], capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
    try:
        with pytest.raises(enrollment.EnrollmentError) as refused:
            enrollment.enroll(batch_path=batch, target=workspace)
        assert refused.value.code == "run-dir"
    finally:
        plan.run_dir.rmdir()

    report = enrollment.enroll(batch_path=batch, target=workspace)
    run_dir = _run_dir(workspace, report["run_id"])
    os.link(_asset(run_dir, "one"), tmp_path / "linked.json")
    with pytest.raises(enrollment.EnrollmentError) as linked:
        enrollment.read_delegation_input(run_dir, task_id="one")
    assert linked.value.code == "asset-custody"
    with pytest.raises(enrollment.EnrollmentError) as replay:
        enrollment.enroll(batch_path=batch, target=workspace)
    assert replay.value.code == "asset-custody"


@pytest.mark.parametrize("failure", [OSError("advapi32 missing"), AttributeError("symbol missing")])
def test_windows_gate_refuses_when_security_api_cannot_bind(monkeypatch, failure):
    import types

    from brigade import dirfd
    from brigade.work_cmd import nt_private

    def unavailable():
        raise failure

    monkeypatch.setattr(enrollment, "os", types.SimpleNamespace(name="nt"))
    monkeypatch.setattr(dirfd, "nt_available", lambda: True)
    monkeypatch.setattr(nt_private, "_security_api", unavailable)

    assert enrollment._nt_private() is None
    with pytest.raises(enrollment.EnrollmentError) as excinfo:
        enrollment._require_supported_platform()
    assert excinfo.value.code == "unsupported-platform"
