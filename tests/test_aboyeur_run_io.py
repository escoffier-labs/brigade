"""Descriptor-bound run.json reads in aboyeur run-I/O stay on the held inode."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brigade import aboyeur, dirfd, run_checkpoint, run_dirfd, run_lifecycle, runguard


_AUTHORITY_REQUEST_FIELD = "run_journal_authority_requested"


def _legacy(status: str = "started", **extra: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": "brigade.run.v1",
        "schema_version": 1,
        "status": status,
        "task": "legacy",
    }
    payload.update(extra)
    return payload


def _incomplete_authority(status: str = "started") -> dict[str, object]:
    return _legacy(status, **{_AUTHORITY_REQUEST_FIELD: True, "projector_version": 1})


def _workspace_run(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / "20260730-104300-deadbeef"
    run_dir.mkdir(parents=True)
    return workspace, run_dir


def _write_run_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _outside_canary(tmp_path: Path, payload: dict[str, object], name: str = "outside") -> tuple[Path, Path, bytes]:
    outside = tmp_path / name
    outside.mkdir()
    outside_json = outside / "run.json"
    _write_run_json(outside_json, payload)
    return outside, outside_json, outside_json.read_bytes()


def _swap_aside(run_dir: Path, outside: Path) -> Path:
    moved = run_dir.with_name(f"{run_dir.name}.moved")
    run_dir.rename(moved)
    run_dir.symlink_to(outside, target_is_directory=True)
    return moved


def _assert_outside_untouched(outside_json: Path, before: bytes) -> None:
    assert outside_json.read_bytes() == before


@pytest.mark.skipif(not dirfd.available(), reason="descriptor-relative primitives required")
def test_bound_authority_state_swap_cannot_block_with_outside_bytes(tmp_path: Path) -> None:
    """Held legacy bytes win; attacker projection metadata must not raise."""
    _, run_dir = _workspace_run(tmp_path)
    _write_run_json(run_dir / "run.json", _legacy())
    outside, outside_json, before = _outside_canary(tmp_path, _incomplete_authority())

    with run_dirfd.bound_run_dir(run_dir):
        _swap_aside(run_dir, outside)
        assert aboyeur._resolve_authority_state(run_dir) == "legacy"

    _assert_outside_untouched(outside_json, before)


@pytest.mark.skipif(not dirfd.available(), reason="descriptor-relative primitives required")
def test_bound_authority_state_swap_cannot_authorize_with_outside_bytes(tmp_path: Path) -> None:
    """Held incomplete authority still fails closed after a same-UID symlink swap."""
    _, run_dir = _workspace_run(tmp_path)
    _write_run_json(run_dir / "run.json", _incomplete_authority())
    outside, outside_json, before = _outside_canary(tmp_path, _legacy())

    with run_dirfd.bound_run_dir(run_dir):
        _swap_aside(run_dir, outside)
        with pytest.raises(run_lifecycle.LifecycleJournalError, match="missing projection metadata"):
            aboyeur._resolve_authority_state(run_dir)

    _assert_outside_untouched(outside_json, before)


def test_no_binding_authority_state_still_follows_swapped_pathname(tmp_path: Path) -> None:
    """Unbound callers keep today's pathname follow of a swapped run directory."""
    _, run_dir = _workspace_run(tmp_path)
    _write_run_json(run_dir / "run.json", _legacy())
    outside, _, _ = _outside_canary(tmp_path, _incomplete_authority())
    _swap_aside(run_dir, outside)

    with pytest.raises(run_lifecycle.LifecycleJournalError, match="missing projection metadata"):
        aboyeur._resolve_authority_state(run_dir)


@pytest.mark.skipif(not dirfd.available(), reason="descriptor-relative primitives required")
def test_bound_authority_state_malformed_held_run_json_is_legacy(tmp_path: Path) -> None:
    _, run_dir = _workspace_run(tmp_path)
    (run_dir / "run.json").write_bytes(b"{not valid json")
    outside, outside_json, before = _outside_canary(tmp_path, _incomplete_authority())

    with run_dirfd.bound_run_dir(run_dir):
        _swap_aside(run_dir, outside)
        assert aboyeur._resolve_authority_state(run_dir) == "legacy"

    _assert_outside_untouched(outside_json, before)


def test_no_binding_malformed_run_json_is_legacy(tmp_path: Path) -> None:
    _, run_dir = _workspace_run(tmp_path)
    (run_dir / "run.json").write_bytes(b"{not valid json")
    assert aboyeur._resolve_authority_state(run_dir) == "legacy"


def test_no_binding_oversize_run_json_stays_legacy(tmp_path: Path) -> None:
    _, run_dir = _workspace_run(tmp_path)
    (run_dir / "run.json").write_bytes(b"x" * (run_dirfd.MAX_READ_BYTES + 1))
    assert aboyeur._resolve_authority_state(run_dir) == "legacy"


@pytest.mark.skipif(not dirfd.available(), reason="descriptor-relative primitives required")
def test_bound_oversize_held_run_json_fails_closed(tmp_path: Path) -> None:
    _, run_dir = _workspace_run(tmp_path)
    (run_dir / "run.json").write_bytes(b"x" * (run_dirfd.MAX_READ_BYTES + 1))

    with run_dirfd.bound_run_dir(run_dir):
        with pytest.raises(OSError, match="bound read exceeds byte limit"):
            aboyeur._resolve_authority_state(run_dir)


@pytest.mark.skipif(not dirfd.available(), reason="descriptor-relative primitives required")
def test_bound_oversize_outside_cannot_block_authority_state(tmp_path: Path) -> None:
    _, run_dir = _workspace_run(tmp_path)
    _write_run_json(run_dir / "run.json", _legacy(**{_AUTHORITY_REQUEST_FIELD: True}))
    outside = tmp_path / "outside-oversize"
    outside.mkdir()
    outside_json = outside / "run.json"
    outside_json.write_bytes(b"x" * (run_dirfd.MAX_READ_BYTES + 1))
    before = outside_json.read_bytes()

    with run_dirfd.bound_run_dir(run_dir):
        _swap_aside(run_dir, outside)
        assert aboyeur._resolve_authority_state(run_dir) == "authority-requested"

    assert outside_json.read_bytes() == before


@pytest.mark.skipif(not dirfd.available(), reason="descriptor-relative primitives required")
def test_bound_prior_snapshot_swap_cannot_authorize_research_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, run_dir = _workspace_run(tmp_path)
    _write_run_json(run_dir / "run.json", _legacy("started"))
    outside, outside_json, before = _outside_canary(
        tmp_path,
        _legacy("completed", kind="research", marker="outside-canary"),
    )
    paired: list[str | None] = []
    real_write = run_checkpoint.write_checkpoint

    def capture(*args: object, **kwargs: object) -> object:
        paired.append(kwargs.get("paired_event_type"))  # type: ignore[arg-type]
        return real_write(*args, **kwargs)

    monkeypatch.setattr(run_checkpoint, "write_checkpoint", capture)
    payload = _legacy("running", kind="research")

    with runguard.run_lock(workspace, run_dir=run_dir):
        with run_dirfd.bound_run_dir(run_dir):
            moved = _swap_aside(run_dir, outside)
            aboyeur._write_json(run_dir / "run.json", payload)

    _assert_outside_untouched(outside_json, before)
    assert json.loads(before)["marker"] == "outside-canary"
    assert json.loads((moved / "run.json").read_text())["status"] == "running"
    assert paired == [None]


def test_no_binding_prior_snapshot_still_follows_swapped_pathname(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, run_dir = _workspace_run(tmp_path)
    _write_run_json(run_dir / "run.json", _legacy("started"))
    outside, _, _ = _outside_canary(tmp_path, _legacy("completed", kind="research"))
    _swap_aside(run_dir, outside)
    paired: list[str | None] = []
    real_write = run_checkpoint.write_checkpoint

    def capture(*args: object, **kwargs: object) -> object:
        paired.append(kwargs.get("paired_event_type"))  # type: ignore[arg-type]
        return real_write(*args, **kwargs)

    monkeypatch.setattr(run_checkpoint, "write_checkpoint", capture)

    with runguard.run_lock(workspace, run_dir=run_dir):
        aboyeur._write_json(run_dir / "run.json", _legacy("running", kind="research"))

    assert paired == ["run.recovery.started"]


@pytest.mark.skipif(not dirfd.available(), reason="descriptor-relative primitives required")
def test_bound_authority_write_swap_cannot_block_with_outside_bytes(tmp_path: Path) -> None:
    workspace, run_dir = _workspace_run(tmp_path)
    _write_run_json(run_dir / "run.json", _legacy("started"))
    outside, outside_json, before = _outside_canary(tmp_path, _incomplete_authority())
    payload = _legacy("planning", **{_AUTHORITY_REQUEST_FIELD: True})

    with runguard.run_lock(workspace, run_dir=run_dir):
        with run_dirfd.bound_run_dir(run_dir):
            moved = _swap_aside(run_dir, outside)
            aboyeur._write_json(run_dir / "run.json", payload)

    _assert_outside_untouched(outside_json, before)
    written = json.loads((moved / "run.json").read_text())
    assert written["status"] == "planning"
    assert written[_AUTHORITY_REQUEST_FIELD] is True
