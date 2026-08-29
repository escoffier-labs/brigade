"""Descriptor-bound run.json reads in aboyeur run-I/O stay on the held inode."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brigade import (
    aboyeur,
    dirfd,
    run_checkpoint,
    run_dirfd,
    run_journal,
    run_lifecycle,
    run_shadow,
    runguard,
)


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


def _journal_payload(status: str, workspace: Path) -> dict[str, object]:
    return _legacy(status, lifecycle_journal_requested=True, lock_workspace=str(workspace))


def _seed_one_pair_journal_ahead(tmp_path: Path) -> tuple[Path, Path, dict[str, object]]:
    """Activate a journal, then append one unpaired checkpoint/status pair.

    The held shadow artifact lags the tail by exactly one structurally
    covered pair, which is the only shape ``_genuine_journal_ahead`` accepts.
    """
    workspace, run_dir = _workspace_run(tmp_path)
    _write_run_json(run_dir / "run.json", _journal_payload("started", workspace))
    with runguard.run_lock(workspace, run_dir=run_dir):
        aboyeur._write_json(run_dir / "run.json", _journal_payload("started", workspace))
        snapshot = (run_dir / "run.json").read_bytes()
        run_checkpoint.write_checkpoint(
            run_dir,
            snapshot,
            workspace=workspace,
            paired_event_type="run.planning.started",
        )
        journal = run_lifecycle._journal_path(run_dir)
        tail = run_journal.read_journal(journal).events[-1].sequence
        run_journal.append_event(
            journal,
            run_id=run_dir.name,
            event_type="run.planning.started",
            payload={"detail": "ok"},
            idempotency_key="test-uncompared:planning",
            expected_previous_sequence=tail,
        )
    artifact = json.loads(run_shadow.shadow_artifact_path(run_dir).read_text())
    return workspace, run_dir, artifact


def _write_shadow_artifact(run_dir: Path, artifact: dict[str, object]) -> None:
    path = run_shadow.shadow_artifact_path(run_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n")


def _clean_match_artifact(run_id: str) -> dict[str, object]:
    artifact = run_shadow._empty_artifact(run_id)
    artifact.update(
        {
            "comparisons": 1,
            "matches": 1,
            "last_outcome": run_shadow.OUTCOME_MATCH,
            "last_compared_sequence": 1,
            "last_compared_event_digest": "a" * 64,
            "last_error_category": None,
        }
    )
    return artifact


@pytest.mark.skipif(not dirfd.available(), reason="descriptor-relative primitives required")
def test_bound_genuine_journal_ahead_swap_cannot_authorize_with_outside_shadow(tmp_path: Path) -> None:
    """Held forged cursor stays untrusted after a same-UID rename/symlink swap."""
    _, run_dir, genuine = _seed_one_pair_journal_ahead(tmp_path)
    assert aboyeur._genuine_journal_ahead(run_dir) is True

    outside = tmp_path / "outside-shadow"
    _write_shadow_artifact(outside, genuine)
    outside_artifact = run_shadow.shadow_artifact_path(outside)
    before = outside_artifact.read_bytes()

    held = run_shadow.shadow_artifact_path(run_dir)
    forged = json.loads(held.read_text())
    forged["last_compared_event_digest"] = "f" * 64
    _write_shadow_artifact(run_dir, forged)
    assert aboyeur._genuine_journal_ahead(run_dir) is False

    with run_dirfd.bound_run_dir(run_dir):
        _swap_aside(run_dir, outside)
        assert aboyeur._genuine_journal_ahead(run_dir) is False

    assert outside_artifact.read_bytes() == before


@pytest.mark.skipif(not dirfd.available(), reason="descriptor-relative primitives required")
def test_bound_prior_clean_match_swap_cannot_authorize_with_outside_shadow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Outside clean-match bytes must not accept a bound catch-up."""
    _, run_dir = _workspace_run(tmp_path)
    held_unclean = _clean_match_artifact(run_dir.name)
    held_unclean["last_outcome"] = run_shadow.OUTCOME_MISMATCH
    held_unclean["mismatches"] = 1
    held_unclean["matches"] = 0
    _write_shadow_artifact(run_dir, held_unclean)

    outside = tmp_path / "outside-clean-match"
    _write_shadow_artifact(outside, _clean_match_artifact(run_dir.name))
    outside_artifact = run_shadow.shadow_artifact_path(outside)
    before = outside_artifact.read_bytes()

    calls = {"n": 0}

    def readiness(_run_dir: Path) -> run_shadow.ReadinessReport:
        calls["n"] += 1
        if calls["n"] == 1:
            return run_shadow.ReadinessReport(ready=False, reasons=(run_shadow.REASON_JOURNAL_AHEAD,))
        return run_shadow.ReadinessReport(ready=True, reasons=())

    monkeypatch.setattr(run_shadow, "check_projection_readiness", readiness)
    monkeypatch.setattr("brigade.aboyeur.run_io._genuine_journal_ahead", lambda _run_dir: True)
    monkeypatch.setattr(run_shadow, "record_shadow_comparison", lambda *_args, **_kwargs: None)

    with run_dirfd.bound_run_dir(run_dir):
        _swap_aside(run_dir, outside)
        with pytest.raises(run_lifecycle.LifecycleJournalError, match="not a clean match"):
            aboyeur._authoritative_prior_decision(run_dir, {"status": "started"})

    assert outside_artifact.read_bytes() == before
