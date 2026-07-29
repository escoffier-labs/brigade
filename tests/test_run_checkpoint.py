"""Tests for brigade.run_checkpoint: checkpoint event type, validation, crash-safe publish.

Issue #568 slice 5, Task 1 (RED-first). Standard library only.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from brigade import run_checkpoint, run_events, run_journal

RUN_ID = "20260727-153045-a1b2c3d4"


def _run_dir(tmp_path: Path) -> Path:
    run_dir = tmp_path / "runs" / RUN_ID
    run_dir.mkdir(parents=True)
    return run_dir


def _writer_bytes(obj: dict) -> bytes:
    """Replicate aboyeur._write_json canonical encoding."""
    return (json.dumps(obj, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _checkpoint_payload(run_json_bytes: bytes, *, paired_event_type: str | None = None) -> dict:
    sha = hashlib.sha256(run_json_bytes).hexdigest()
    return {
        "path": f"events/recovery-checkpoints/{sha}.json",
        "sha256": sha,
        "media_type": run_checkpoint.CHECKPOINT_MEDIA_TYPE,
        "byte_size": len(run_json_bytes),
        "privacy_class": run_checkpoint.CHECKPOINT_PRIVACY_CLASS,
        "paired_event_type": paired_event_type,
    }


def _place_checkpoint_file(run_dir: Path, run_json_bytes: bytes) -> Path:
    cp_dir = run_dir / "events" / run_checkpoint.CHECKPOINT_DIR_NAME
    cp_dir.mkdir(parents=True, exist_ok=True)
    sha = hashlib.sha256(run_json_bytes).hexdigest()
    final = cp_dir / f"{sha}.json"
    final.write_bytes(run_json_bytes)
    os.chmod(final, 0o600)
    return final


# -- Constants ---------------------------------------------------------------


def test_checkpoint_constants_have_approved_values():
    assert run_checkpoint.CHECKPOINT_EVENT_TYPE == "run.snapshot.checkpointed"
    assert run_checkpoint.CHECKPOINT_MEDIA_TYPE == "application/vnd.brigade.run+json"
    assert run_checkpoint.CHECKPOINT_PRIVACY_CLASS == "private"
    assert run_checkpoint.CHECKPOINT_DIR_NAME == "recovery-checkpoints"
    assert run_checkpoint.MAX_CHECKPOINT_BYTES == 16 * 1024 * 1024
    assert run_checkpoint.MAX_JOURNAL_BYTES == 8 * 1024 * 1024
    assert run_checkpoint.MAX_JOURNAL_EVENTS == 512


def test_checkpoint_dir_and_path_helpers():
    run_dir = Path("/tmp/runs/abc")
    assert run_checkpoint.checkpoint_dir(run_dir) == run_dir / "events" / "recovery-checkpoints"
    sha = "a" * 64
    assert run_checkpoint.checkpoint_path(run_dir, sha) == (run_dir / "events" / "recovery-checkpoints" / f"{sha}.json")


# -- Event type registration -------------------------------------------------


def test_checkpoint_event_type_registered_with_closed_payload_keys():
    assert "run.snapshot.checkpointed" in run_events.EVENT_TYPES
    assert run_events.EVENT_TYPES["run.snapshot.checkpointed"] == frozenset(
        {"path", "sha256", "media_type", "byte_size", "privacy_class", "paired_event_type"}
    )


# -- Payload validation ------------------------------------------------------


def test_validate_checkpoint_accepts_well_formed_payload(tmp_path):
    run_dir = _run_dir(tmp_path)
    run_json_bytes = _writer_bytes({"status": "started", "task": "demo"})
    _place_checkpoint_file(run_dir, run_json_bytes)
    payload = _checkpoint_payload(run_json_bytes)

    result = run_checkpoint.validate_checkpoint(run_dir, payload)

    assert result == run_json_bytes


def test_validate_checkpoint_rejects_malformed_payloads_without_opening_files(tmp_path, monkeypatch):
    run_dir = _run_dir(tmp_path)
    good_bytes = _writer_bytes({"status": "started"})
    good_payload = _checkpoint_payload(good_bytes)

    def _fail_open(*args, **kwargs):
        raise AssertionError("validate_checkpoint must not open a file before payload validation")

    monkeypatch.setattr(run_journal, "_open_nofollow", _fail_open)

    cases = []

    def add(label, mutate, category):
        cases.append((label, mutate, category))

    extra = dict(good_payload)
    extra["unexpected"] = "x"
    add("extra key", lambda p: {**p, "unexpected": "x"}, "payload-keys")
    add("missing key", lambda p: {k: v for k, v in p.items() if k != "media_type"}, "payload-keys")
    add("wrong media_type", lambda p: {**p, "media_type": "application/json"}, "media-type")
    add("wrong privacy_class", lambda p: {**p, "privacy_class": "public"}, "privacy-class")
    add("uppercase sha256", lambda p: {**p, "sha256": p["sha256"].upper()}, "sha256")
    add("short sha256", lambda p: {**p, "sha256": p["sha256"][:32]}, "sha256")
    add("bool byte_size", lambda p: {**p, "byte_size": True}, "byte-size")
    add("negative byte_size", lambda p: {**p, "byte_size": -1}, "byte-size")
    add("oversize byte_size", lambda p: {**p, "byte_size": run_checkpoint.MAX_CHECKPOINT_BYTES + 1}, "byte-size")
    add("absolute path", lambda p: {**p, "path": "/events/recovery-checkpoints/x.json"}, "path")
    add("backslash path", lambda p: {**p, "path": "events\\recovery-checkpoints\\x.json"}, "path")
    add("dotdot path", lambda p: {**p, "path": "events/../recovery-checkpoints/x.json"}, "path")
    add("stem mismatch", lambda p: {**p, "path": "events/recovery-checkpoints/deadbeef.json"}, "path")
    add(
        "invalid paired_event_type",
        lambda p: {**p, "paired_event_type": "run.not.a.real.event"},
        "paired-event-type",
    )

    for label, mutate, category in cases:
        bad = mutate(good_payload)
        with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
            run_checkpoint.validate_checkpoint(run_dir, bad)
        err = excinfo.value
        assert err.category == category, f"{label}: expected {category}, got {err.category}"
        assert len(str(err)) <= run_events.MAX_DIAGNOSTIC_LEN, f"{label}: diagnostic not bounded"


def test_validate_checkpoint_accepts_null_paired_event_type(tmp_path):
    run_dir = _run_dir(tmp_path)
    run_json_bytes = _writer_bytes({"status": "started"})
    _place_checkpoint_file(run_dir, run_json_bytes)
    payload = _checkpoint_payload(run_json_bytes, paired_event_type=None)
    assert run_checkpoint.validate_checkpoint(run_dir, payload) == run_json_bytes


def test_validate_checkpoint_accepts_mapped_paired_event_type(tmp_path):
    run_dir = _run_dir(tmp_path)
    run_json_bytes = _writer_bytes({"status": "started"})
    _place_checkpoint_file(run_dir, run_json_bytes)
    payload = _checkpoint_payload(run_json_bytes, paired_event_type="run.created")
    assert run_checkpoint.validate_checkpoint(run_dir, payload) == run_json_bytes


# -- Open-fd hardening -------------------------------------------------------


def test_validate_checkpoint_open_fd_hardening(tmp_path):
    run_dir = _run_dir(tmp_path)
    run_json_bytes = _writer_bytes({"status": "started"})
    sha = hashlib.sha256(run_json_bytes).hexdigest()
    cp_dir = run_dir / "events" / run_checkpoint.CHECKPOINT_DIR_NAME
    cp_dir.mkdir(parents=True)

    # symlink final component -> refused (bounded, no follow)
    outside = tmp_path / "outside"
    outside.mkdir()
    symlink_target = cp_dir / f"{sha}.json"
    symlink_target.symlink_to(outside / "real.json")
    payload = _checkpoint_payload(run_json_bytes)
    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.validate_checkpoint(run_dir, payload)
    assert excinfo.value.category in {"open-fd", "not-regular", "link-count"}

    symlink_target.unlink()

    # non-regular inode (directory at the final path)
    nonreg = cp_dir / f"{sha}.json"
    nonreg.mkdir()
    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.validate_checkpoint(run_dir, payload)
    assert excinfo.value.category == "not-regular"
    nonreg.rmdir()

    # link count above one (hard link the real file)
    real = cp_dir / f"{sha}.json"
    real.write_bytes(run_json_bytes)
    os.chmod(real, 0o600)
    extra = cp_dir / "link.json"
    os.link(real, extra)
    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.validate_checkpoint(run_dir, payload)
    assert excinfo.value.category == "link-count"
    extra.unlink()

    # size mismatch
    wrong_bytes = _writer_bytes({"status": "started", "extra": "diff"})
    real.write_bytes(wrong_bytes)
    os.chmod(real, 0o600)
    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.validate_checkpoint(run_dir, payload)
    assert excinfo.value.category == "size-mismatch"

    # digest mismatch (right size, wrong bytes)
    same_size = b"x" * len(run_json_bytes)
    real.write_bytes(same_size)
    os.chmod(real, 0o600)
    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.validate_checkpoint(run_dir, payload)
    assert excinfo.value.category == "digest-mismatch"

    # writer-canonical-byte mismatch: a file with a valid digest (matches its
    # own bytes) but whose bytes are not the aboyeur writer canonical encoding
    # of the JSON object they parse to. The digest check passes (file bytes
    # hash to the declared sha256), then the writer-byte equality check fails.
    real.write_bytes(run_json_bytes)
    os.chmod(real, 0o600)
    non_writer = b'{"status":"planning"}\n'  # compact, not writer canonical
    non_writer_sha = hashlib.sha256(non_writer).hexdigest()
    non_writer_path = cp_dir / f"{non_writer_sha}.json"
    non_writer_path.write_bytes(non_writer)
    os.chmod(non_writer_path, 0o600)
    bad_payload = _checkpoint_payload(non_writer)
    bad_payload["sha256"] = non_writer_sha
    bad_payload["path"] = f"events/recovery-checkpoints/{non_writer_sha}.json"
    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.validate_checkpoint(run_dir, bad_payload)
    assert excinfo.value.category == "writer-bytes"


# -- Crash-safe publish ------------------------------------------------------


def test_publish_checkpoint_file_writes_private_file_with_matching_digest(tmp_path):
    run_dir = _run_dir(tmp_path)
    run_json_bytes = _writer_bytes({"status": "started", "task": "demo"})

    final = run_checkpoint.publish_checkpoint_file(run_dir, run_json_bytes)

    assert final.is_file()
    assert stat.S_IMODE(final.stat().st_mode) == 0o600
    assert stat.S_IMODE(final.parent.stat().st_mode) == 0o700
    assert final == run_checkpoint.checkpoint_path(run_dir, hashlib.sha256(run_json_bytes).hexdigest())
    assert final.read_bytes() == run_json_bytes
    assert hashlib.sha256(final.read_bytes()).hexdigest() == hashlib.sha256(run_json_bytes).hexdigest()


def test_publish_checkpoint_file_matching_collision_is_noop(tmp_path):
    run_dir = _run_dir(tmp_path)
    run_json_bytes = _writer_bytes({"status": "started"})
    pre = _place_checkpoint_file(run_dir, run_json_bytes)
    pre_mtime = pre.stat().st_mtime_ns
    pre_bytes = pre.read_bytes()

    final = run_checkpoint.publish_checkpoint_file(run_dir, run_json_bytes)

    assert final == pre
    assert final.read_bytes() == pre_bytes
    assert final.stat().st_mtime_ns == pre_mtime


def test_publish_checkpoint_file_mismatched_collision_fails_closed(tmp_path):
    run_dir = _run_dir(tmp_path)
    run_json_bytes = _writer_bytes({"status": "started"})
    final = _place_checkpoint_file(run_dir, run_json_bytes)
    # Corrupt the existing file in place (same path, different bytes).
    final.write_bytes(_writer_bytes({"status": "failed"}))
    os.chmod(final, 0o600)
    before = final.read_bytes()

    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.publish_checkpoint_file(run_dir, run_json_bytes)
    assert excinfo.value.category == "collision-mismatch"
    assert final.read_bytes() == before


def test_publish_checkpoint_file_unsafe_collision_fails_closed(tmp_path):
    run_dir = _run_dir(tmp_path)
    run_json_bytes = _writer_bytes({"status": "started"})
    sha = hashlib.sha256(run_json_bytes).hexdigest()
    cp_dir = run_dir / "events" / run_checkpoint.CHECKPOINT_DIR_NAME
    cp_dir.mkdir(parents=True)

    # symlink collision
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "real.json"
    target.write_bytes(run_json_bytes)
    symlink = cp_dir / f"{sha}.json"
    symlink.symlink_to(target)
    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.publish_checkpoint_file(run_dir, run_json_bytes)
    assert excinfo.value.category == "collision-unsafe"
    assert symlink.is_symlink()
    symlink.unlink()

    # non-regular inode collision (directory)
    nonreg = cp_dir / f"{sha}.json"
    nonreg.mkdir()
    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.publish_checkpoint_file(run_dir, run_json_bytes)
    assert excinfo.value.category == "collision-unsafe"
    nonreg.rmdir()

    # link count above one collision
    real = cp_dir / f"{sha}.json"
    real.write_bytes(run_json_bytes)
    os.chmod(real, 0o600)
    extra = cp_dir / "extra.json"
    os.link(real, extra)
    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.publish_checkpoint_file(run_dir, run_json_bytes)
    assert excinfo.value.category == "collision-unsafe"
    extra.unlink()


def test_publish_checkpoint_file_refuses_oversize_bytes(tmp_path):
    run_dir = _run_dir(tmp_path)
    oversize = b"x" * (run_checkpoint.MAX_CHECKPOINT_BYTES + 1)
    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.publish_checkpoint_file(run_dir, oversize)
    assert excinfo.value.category == "byte-size"
