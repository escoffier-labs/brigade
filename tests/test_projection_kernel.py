"""Projection transaction kernel (issue #910)."""

from __future__ import annotations

import errno
import hashlib
import json
import os
from pathlib import Path

import pytest

from brigade.projection import kernel as projection
from brigade.projection.fixture import mixed_workspace


def _digest(data: bytes | None) -> str:
    if data is None:
        return projection.ABSENT
    return hashlib.sha256(data).hexdigest()


def _snapshot(root: Path) -> dict[str, tuple[str, int | None]]:
    records: dict[str, tuple[str, int | None]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_dir() or ".brigade" in path.parts:
            continue
        rel = path.relative_to(root).as_posix()
        records[rel] = (_digest(path.read_bytes()), path.stat().st_mode & 0o777)
    return records


def test_mixed_plan_commits_create_replace_and_remove(tmp_path: Path) -> None:
    workspace, plan = mixed_workspace(tmp_path)
    receipt = projection.execute(plan, target=workspace)
    assert receipt.terminal_state == "committed"
    assert (workspace / "a-create.txt").read_bytes() == b"created\n"
    assert (workspace / "b-replace.txt").read_bytes() == b"new\n"
    assert not (workspace / "c-remove.txt").exists()
    op_dir = projection.operation_dir(workspace, plan.operation_id)
    assert not (op_dir / "before").exists()
    assert (op_dir / "receipt.json").is_file()
    payload = json.loads((op_dir / "receipt.json").read_text())
    assert payload["terminal_state"] == "committed"
    assert payload["recovery_command"] is None


@pytest.mark.parametrize(
    "boundary",
    [
        "commit:0:before",
        "commit:0:after",
        "commit:1:before",
        "commit:1:after",
        "commit:2:before",
        "commit:2:after",
    ],
)
def test_injected_failure_at_each_mutation_boundary_restores(tmp_path: Path, boundary: str) -> None:
    workspace, plan = mixed_workspace(tmp_path)
    before = _snapshot(workspace)
    receipt = projection.execute(
        plan,
        target=workspace,
        inject=projection.FailureInjector(boundary=boundary),
    )
    assert receipt.terminal_state == "restored"
    assert _snapshot(workspace) == before
    assert receipt.recovery_command is None


def test_restore_failure_returns_recovery_required_and_recover_is_idempotent(tmp_path: Path) -> None:
    workspace, plan = mixed_workspace(tmp_path)
    before = _snapshot(workspace)
    receipt = projection.execute(
        plan,
        target=workspace,
        inject=projection.FailureInjector(boundary="commit:1:after", restore_boundary="restore:0:before"),
    )
    assert receipt.terminal_state == "recovery-required"
    assert receipt.recovery_command == f"brigade projection recover {plan.operation_id}"
    first = projection.recover(plan.operation_id, target=workspace)
    assert first.terminal_state == "restored"
    assert _snapshot(workspace) == before
    second = projection.recover(plan.operation_id, target=workspace)
    assert second.terminal_state == "restored"
    assert _snapshot(workspace) == before


def test_manual_recovery_failure_keeps_recovery_material(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, plan = mixed_workspace(tmp_path)
    projection.execute(
        plan,
        target=workspace,
        inject=projection.FailureInjector(boundary="commit:1:after", restore_boundary="restore:0:before"),
    )

    def boom(*_args, **_kwargs):
        raise OSError("restore failed")

    monkeypatch.setattr(projection, "_publish_bytes_via_parent", boom)
    receipt = projection.recover(plan.operation_id, target=workspace)
    assert receipt.terminal_state == "recovery-required"
    assert receipt.recovery_command == f"brigade projection recover {plan.operation_id}"
    assert (projection.operation_dir(workspace, plan.operation_id) / "before").is_dir()


def test_destination_drift_between_plan_and_prepare_fails_closed(tmp_path: Path) -> None:
    workspace, plan = mixed_workspace(tmp_path)
    before = _snapshot(workspace)
    (workspace / "b-replace.txt").write_bytes(b"drifted\n")
    with pytest.raises(projection.DriftError):
        projection.execute(plan, target=workspace)
    assert (workspace / "b-replace.txt").read_bytes() == b"drifted\n"
    assert not (workspace / "a-create.txt").exists()
    assert (workspace / "c-remove.txt").exists()
    assert not projection.operation_dir(workspace, plan.operation_id).exists()
    assert _snapshot(workspace)["c-remove.txt"] == before["c-remove.txt"]


def test_crash_state_restart_detects_and_recovers(tmp_path: Path) -> None:
    workspace, plan = mixed_workspace(tmp_path)
    before = _snapshot(workspace)
    projection.write_crash_fixture(plan, target=workspace, committed_count=1)
    unfinished = projection.unfinished_operations(workspace)
    assert len(unfinished) == 1
    assert unfinished[0].operation_id == plan.operation_id
    assert unfinished[0].status in {"committing", "recovery-required"}
    receipt = projection.recover(plan.operation_id, target=workspace)
    assert receipt.terminal_state == "restored"
    assert _snapshot(workspace) == before


def test_plan_rejects_symlink_duplicate_and_parent_child(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = workspace / "file.txt"
    target.write_bytes(b"ok\n")
    link = workspace / "link.txt"
    link.symlink_to(target)
    nested = workspace / "dir"
    nested.mkdir()
    (nested / "child.txt").write_bytes(b"child\n")

    with pytest.raises(projection.PlanError, match="symlink"):
        projection.build_plan(
            operation_id="op-symlink",
            projector="fixture",
            source_fingerprint="src",
            mutations=[
                projection.mutation(
                    destination=link,
                    mutation="replace",
                    expected_before=_digest(b"ok\n"),
                    desired_after=_digest(b"new\n"),
                    staged_bytes=b"new\n",
                )
            ],
            target=workspace,
        )

    outside = tmp_path / "outside"
    outside.mkdir()
    directory_link = workspace / "directory-link"
    directory_link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(projection.PlanError, match="symlink"):
        projection.build_plan(
            operation_id="op-parent-symlink",
            projector="fixture",
            source_fingerprint="src",
            mutations=[
                projection.mutation(
                    destination=directory_link / "outside.txt",
                    mutation="create",
                    expected_before=projection.ABSENT,
                    desired_after=_digest(b"new\\n"),
                    staged_bytes=b"new\\n",
                )
            ],
            target=workspace,
        )

    with pytest.raises(projection.PlanError, match="duplicate"):
        projection.build_plan(
            operation_id="op-dup",
            projector="fixture",
            source_fingerprint="src",
            mutations=[
                projection.mutation(
                    destination=target,
                    mutation="replace",
                    expected_before=_digest(b"ok\n"),
                    desired_after=_digest(b"a\n"),
                    staged_bytes=b"a\n",
                ),
                projection.mutation(
                    destination=target,
                    mutation="replace",
                    expected_before=_digest(b"ok\n"),
                    desired_after=_digest(b"b\n"),
                    staged_bytes=b"b\n",
                ),
            ],
            target=workspace,
        )

    with pytest.raises(projection.PlanError, match="parent-child"):
        projection.build_plan(
            operation_id="op-nested",
            projector="fixture",
            source_fingerprint="src",
            mutations=[
                projection.mutation(
                    destination=nested,
                    mutation="replace",
                    expected_before=_digest(b"unused"),
                    desired_after=_digest(b"x"),
                    staged_bytes=b"x",
                ),
                projection.mutation(
                    destination=nested / "child.txt",
                    mutation="replace",
                    expected_before=_digest(b"child\n"),
                    desired_after=_digest(b"y\n"),
                    staged_bytes=b"y\n",
                ),
            ],
            target=workspace,
        )


def test_permission_and_disk_write_failures_restore(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, plan = mixed_workspace(tmp_path)
    before = _snapshot(workspace)

    def boom_permission(*_args, **_kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(projection, "_publish_bytes_via_parent", boom_permission)
    receipt = projection.execute(plan, target=workspace)
    assert receipt.terminal_state == "restored"
    assert _snapshot(workspace) == before

    def boom_disk(*_args, **_kwargs):
        raise OSError(errno.ENOSPC, "no space")

    monkeypatch.setattr(projection, "_publish_bytes_via_parent", boom_disk)
    receipt = projection.execute(plan, target=workspace)
    assert receipt.terminal_state == "restored"
    assert _snapshot(workspace) == before


def test_unfinished_recovery_blocks_only_overlapping_destinations(tmp_path: Path) -> None:
    workspace, blocked_plan = mixed_workspace(tmp_path / "shared")
    projection.execute(
        blocked_plan,
        target=workspace,
        inject=projection.FailureInjector(boundary="commit:1:after", restore_boundary="restore:0:before"),
    )
    overlapping = projection.build_plan(
        operation_id="op-overlap",
        projector="fixture",
        source_fingerprint="src-2",
        mutations=[
            projection.mutation(
                destination=workspace / "b-replace.txt",
                mutation="replace",
                expected_before=_digest(b"old\n"),
                desired_after=_digest(b"other\n"),
                staged_bytes=b"other\n",
            )
        ],
        target=workspace,
    )
    with pytest.raises(projection.OverlapBlockedError):
        projection.execute(overlapping, target=workspace)

    other = workspace / "other.txt"
    other.write_bytes(b"keep\n")
    disjoint = projection.build_plan(
        operation_id="op-disjoint",
        projector="fixture",
        source_fingerprint="src-3",
        mutations=[
            projection.mutation(
                destination=other,
                mutation="replace",
                expected_before=_digest(b"keep\n"),
                desired_after=_digest(b"changed\n"),
                staged_bytes=b"changed\n",
            )
        ],
        target=workspace,
    )
    receipt = projection.execute(disjoint, target=workspace)
    assert receipt.terminal_state == "committed"
    assert other.read_bytes() == b"changed\n"


def test_receipt_redacts_paths_secrets_and_omits_contents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home" / "operator"
    home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    workspace = home / "proj"
    workspace.mkdir()
    secret = b"Authorization: Bearer tok_live_secret_value\n"
    dest = workspace / "owned.txt"
    dest.write_bytes(b"before\n")
    plan = projection.build_plan(
        operation_id="op-private",
        projector="fixture",
        source_fingerprint="src",
        mutations=[
            projection.mutation(
                destination=dest,
                display_path=str(dest),
                mutation="replace",
                expected_before=_digest(b"before\n"),
                desired_after=_digest(secret),
                staged_bytes=secret,
            )
        ],
        target=workspace,
    )
    receipt = projection.execute(plan, target=workspace)
    payload = receipt.to_dict()
    rendered = json.dumps(payload)
    assert str(home) not in rendered
    assert "tok_live_secret_value" not in rendered
    assert "Authorization" not in rendered
    assert secret.decode() not in rendered
    assert payload["destinations"][0]["display_path"] == "owned.txt"
    assert payload["schema_version"] == projection.SCHEMA_VERSION
    assert payload["operation_id"] == "op-private"
    assert payload["projector"] == "fixture"


def test_recover_refuses_post_failure_destination_drift_without_force(tmp_path: Path) -> None:
    workspace, plan = mixed_workspace(tmp_path)
    projection.execute(
        plan,
        target=workspace,
        inject=projection.FailureInjector(boundary="commit:1:after", restore_boundary="restore:0:before"),
    )
    (workspace / "a-create.txt").write_bytes(b"user-edited\n")
    with pytest.raises(projection.DestinationChangedError):
        projection.recover(plan.operation_id, target=workspace)
    forced = projection.recover(plan.operation_id, target=workspace, force=True)
    assert forced.terminal_state == "restored"
    assert not (workspace / "a-create.txt").exists()


def test_dependency_decision_is_native_stdlib() -> None:
    assert projection.DEPENDENCY_DECISION["implementation"] == "native"
    assert projection.DEPENDENCY_DECISION["runtime_package"] is None


def _swap_parent_for_outside(parent: Path, original: Path, outside: Path) -> None:
    parent.rename(original)
    parent.symlink_to(outside, target_is_directory=True)


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(os, "O_NOFOLLOW"),
    reason="parent-swap probe requires POSIX O_NOFOLLOW",
)
def test_parent_swap_between_plan_and_commit_does_not_escape(tmp_path: Path) -> None:
    """Mutation probe for #1013: swap a checked parent for a symlink before commit.

    Unfixed pathname writes publish the new file outside the vault. The held-parent
    descriptor path must refuse the commit and leave the outside tree untouched.
    """
    workspace = tmp_path / "vault"
    parent = workspace / "Cards"
    parent.mkdir(parents=True)
    dest = parent / "note.md"
    payload = b"escaped-via-parent-swap\n"
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / "note.md"
    original = tmp_path / "Cards.original"

    plan = projection.build_plan(
        operation_id="op-parent-swap",
        projector="probe",
        source_fingerprint="probe-src",
        mutations=[
            projection.mutation(
                destination=dest,
                mutation="create",
                expected_before=projection.ABSENT,
                desired_after=_digest(payload),
                staged_bytes=payload,
            )
        ],
        target=workspace,
    )
    _swap_parent_for_outside(parent, original, outside)

    with pytest.raises(projection.PlanError, match="symlink"):
        projection.execute(plan, target=workspace)

    assert not outside_file.exists()
    assert list(outside.iterdir()) == []
    assert not dest.exists()
    assert not (original / "note.md").exists()


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(os, "O_NOFOLLOW"),
    reason="parent-swap probe requires POSIX O_NOFOLLOW",
)
def test_parent_swap_after_parent_is_held_does_not_escape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A parent swapped after the descriptor is held cannot redirect the write."""
    workspace = tmp_path / "vault"
    parent = workspace / "Cards"
    parent.mkdir(parents=True)
    dest = parent / "note.md"
    payload = b"escaped-via-held-parent-swap\n"
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / "note.md"
    original = tmp_path / "Cards.original"
    plan = projection.build_plan(
        operation_id="op-held-parent-swap",
        projector="probe",
        source_fingerprint="probe-src",
        mutations=[
            projection.mutation(
                destination=dest,
                mutation="create",
                expected_before=projection.ABSENT,
                desired_after=_digest(payload),
                staged_bytes=payload,
            )
        ],
        target=workspace,
    )
    real_publish = projection._publish_bytes_via_parent
    swapped = {"done": False}

    def swap_then_publish(parent_fd: int, name: str, data: bytes) -> None:
        if not swapped["done"]:
            swapped["done"] = True
            if parent.exists() and not parent.is_symlink():
                _swap_parent_for_outside(parent, original, outside)
        return real_publish(parent_fd, name, data)

    monkeypatch.setattr(projection, "_publish_bytes_via_parent", swap_then_publish)
    receipt = projection.execute(plan, target=workspace)
    assert swapped["done"] is True
    assert receipt.terminal_state == "committed"
    assert not outside_file.exists()
    assert list(outside.iterdir()) == []
    assert (original / "note.md").read_bytes() == payload
    assert not dest.exists()


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(os, "O_NOFOLLOW"),
    reason="parent-swap probe requires POSIX O_NOFOLLOW",
)
def test_parent_swap_after_hold_on_writer_callback_does_not_escape(tmp_path: Path) -> None:
    """Mutation probe: a writer= callback must not pathname-commit after the hold.

    Stays red if `_apply_writer` is restored to `spec.writer(spec.destination, ...)`.
    """
    workspace = tmp_path / "vault"
    parent = workspace / "Cards"
    parent.mkdir(parents=True)
    dest = parent / "note.md"
    payload = b"escaped-via-parent-swap\n"
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / "note.md"
    original = tmp_path / "Cards.original"

    def swapping_writer(path: Path, data: bytes) -> None:
        if parent.exists() and not parent.is_symlink():
            _swap_parent_for_outside(parent, original, outside)
        path.write_bytes(data)

    plan = projection.build_plan(
        operation_id="op-writer-parent-swap",
        projector="probe",
        source_fingerprint="probe-src",
        mutations=[
            projection.mutation(
                destination=dest,
                mutation="create",
                expected_before=projection.ABSENT,
                desired_after=_digest(payload),
                staged_bytes=payload,
                writer=swapping_writer,
            )
        ],
        target=workspace,
    )
    receipt = projection.execute(plan, target=workspace)
    assert receipt.terminal_state != "committed"
    assert not outside_file.exists()
    assert list(outside.iterdir()) == []


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(os, "O_NOFOLLOW"),
    reason="parent-swap probe requires POSIX O_NOFOLLOW",
)
def test_parent_swap_after_hold_on_managed_block_writer_does_not_escape(tmp_path: Path) -> None:
    """Production marked-block writer plus an after-hold parent swap must not escape."""
    from brigade import managed_block

    workspace = tmp_path / "vault"
    parent = workspace / ".claude"
    parent.mkdir(parents=True)
    dest = parent / "CLAUDE.md"
    payload = b"escaped-via-parent-swap\n"
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / "CLAUDE.md"
    original = tmp_path / "claude.original"

    def production_writer(path: Path, data: bytes) -> None:
        if parent.exists() and not parent.is_symlink():
            _swap_parent_for_outside(parent, original, outside)
        outcome = managed_block.write_text_nofollow_atomic(path, data.decode("utf-8"))
        if outcome.status not in {managed_block.WRITE_WRITTEN, managed_block.WRITE_NOOP}:
            raise OSError(outcome.detail or "instruction write refused")

    plan = projection.build_plan(
        operation_id="op-managed-block-writer-swap",
        projector="probe",
        source_fingerprint="probe-src",
        mutations=[
            projection.mutation(
                destination=dest,
                mutation="create",
                expected_before=projection.ABSENT,
                desired_after=_digest(payload),
                staged_bytes=payload,
                writer=production_writer,
            )
        ],
        target=workspace,
    )
    receipt = projection.execute(plan, target=workspace)
    assert receipt.terminal_state != "committed"
    assert not outside_file.exists()
    assert list(outside.iterdir()) == []


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(os, "O_NOFOLLOW"),
    reason="parent-swap probe requires POSIX O_NOFOLLOW",
)
def test_parent_swap_after_hold_on_remover_callback_does_not_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "vault"
    parent = workspace / "Cards"
    parent.mkdir(parents=True)
    dest = parent / "note.md"
    dest.write_bytes(b"keep-inside\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / "note.md"
    outside_file.write_bytes(b"outside-victim\n")
    original = tmp_path / "Cards.original"

    def swapping_remover(path: Path) -> None:
        if parent.exists() and not parent.is_symlink():
            _swap_parent_for_outside(parent, original, outside)
        path.unlink()

    plan = projection.build_plan(
        operation_id="op-remover-parent-swap",
        projector="probe",
        source_fingerprint="probe-src",
        mutations=[
            projection.mutation(
                destination=dest,
                mutation="remove",
                expected_before=_digest(b"keep-inside\n"),
                desired_after=projection.ABSENT,
                remover=swapping_remover,
            )
        ],
        target=workspace,
    )
    receipt = projection.execute(plan, target=workspace)
    assert receipt.terminal_state != "committed"
    assert outside_file.read_bytes() == b"outside-victim\n"


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(os, "O_NOFOLLOW"),
    reason="parent-swap probe requires POSIX O_NOFOLLOW",
)
def test_create_under_missing_nested_parent_stays_inside_tree(tmp_path: Path) -> None:
    workspace = tmp_path / "vault"
    workspace.mkdir()
    dest = workspace / "Cards" / "nested" / "note.md"
    payload = b"nested-create\n"
    plan = projection.build_plan(
        operation_id="op-nested-create",
        projector="probe",
        source_fingerprint="probe-src",
        mutations=[
            projection.mutation(
                destination=dest,
                mutation="create",
                expected_before=projection.ABSENT,
                desired_after=_digest(payload),
                staged_bytes=payload,
            )
        ],
        target=workspace,
    )
    receipt = projection.execute(plan, target=workspace)
    assert receipt.terminal_state == "committed"
    assert dest.read_bytes() == payload
    assert not dest.is_symlink()
    assert dest.parent.is_dir()
    assert not dest.parent.is_symlink()


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(os, "O_NOFOLLOW"),
    reason="parent-swap probe requires POSIX O_NOFOLLOW",
)
def test_sibling_creates_under_shared_missing_parent_commit(tmp_path: Path) -> None:
    workspace = tmp_path / "vault"
    workspace.mkdir()
    first = workspace / "Brigade Memory" / "Cards" / "Alpha.md"
    second = workspace / "Brigade Memory" / "Cards" / "Beta.md"
    plan = projection.build_plan(
        operation_id="op-shared-parent",
        projector="probe",
        source_fingerprint="probe-src",
        mutations=[
            projection.mutation(
                destination=first,
                mutation="create",
                expected_before=projection.ABSENT,
                desired_after=_digest(b"alpha\n"),
                staged_bytes=b"alpha\n",
            ),
            projection.mutation(
                destination=second,
                mutation="create",
                expected_before=projection.ABSENT,
                desired_after=_digest(b"beta\n"),
                staged_bytes=b"beta\n",
            ),
        ],
        target=workspace,
    )
    receipt = projection.execute(plan, target=workspace)
    assert receipt.terminal_state == "committed"
    assert first.read_bytes() == b"alpha\n"
    assert second.read_bytes() == b"beta\n"
