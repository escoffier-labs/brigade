"""Tests for brigade.run_checkpoint: checkpoint event type, validation, crash-safe publish.

Issue #568 slice 5, Task 1 (RED-first). Standard library only.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from brigade import run_checkpoint, run_events, run_journal, run_lifecycle, runguard
from brigade import localio
from tests.support import PRIVATE_DIRECTORY_MODE, PRIVATE_FILE_MODE, assert_private_mode

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
    os.chmod(final, PRIVATE_FILE_MODE)
    return final


# -- Constants ---------------------------------------------------------------


def test_checkpoint_constants_have_approved_values():
    assert run_checkpoint.CHECKPOINT_EVENT_TYPE == "run.snapshot.checkpointed"
    assert run_checkpoint.CHECKPOINT_MEDIA_TYPE == "application/vnd.brigade.run+json"
    assert run_checkpoint.CHECKPOINT_PRIVACY_CLASS == "private"
    assert run_checkpoint.CHECKPOINT_DIR_NAME == "recovery-checkpoints"
    assert run_checkpoint.MAX_CHECKPOINT_BYTES == 16 * 1024 * 1024
    assert run_checkpoint.MAX_JOURNAL_BYTES == 8 * 1024 * 1024
    assert run_checkpoint.MAX_JOURNAL_EVENTS == 2048


def test_checkpoint_dir_and_path_helpers():
    run_dir = Path("/tmp/runs/abc")
    assert run_checkpoint.checkpoint_dir(run_dir) == run_dir / "events" / "recovery-checkpoints"
    sha = "a" * 64
    assert run_checkpoint.checkpoint_path(run_dir, sha) == (run_dir / "events" / "recovery-checkpoints" / f"{sha}.json")


# -- Event type registration -------------------------------------------------


def test_checkpoint_event_type_registered_with_closed_payload_keys():
    assert "run.snapshot.checkpointed" in run_events.EVENT_TYPES
    assert run_events.EVENT_TYPES["run.snapshot.checkpointed"] == frozenset(
        {
            "path",
            "sha256",
            "media_type",
            "byte_size",
            "privacy_class",
            "paired_event_type",
            "body_kind",
            "pairing_key",
        }
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


def test_validate_checkpoint_accepts_registered_status_neutral_approval_type(tmp_path):
    run_dir = _run_dir(tmp_path)
    run_json_bytes = _writer_bytes({"status": "running"})
    _place_checkpoint_file(run_dir, run_json_bytes)
    payload = _checkpoint_payload(run_json_bytes, paired_event_type="approval.requested")

    assert run_checkpoint.validate_checkpoint(run_dir, payload) == run_json_bytes


def test_validate_checkpoint_rejects_unregistered_approval_type(tmp_path):
    run_dir = _run_dir(tmp_path)
    run_json_bytes = _writer_bytes({"status": "running"})
    _place_checkpoint_file(run_dir, run_json_bytes)
    payload = _checkpoint_payload(run_json_bytes, paired_event_type="approval.forged")

    with pytest.raises(run_checkpoint.CheckpointError, match="not in registry") as excinfo:
        run_checkpoint.validate_checkpoint(run_dir, payload)

    assert excinfo.value.category == "paired-event-type"


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
    os.chmod(real, PRIVATE_FILE_MODE)
    extra = cp_dir / "link.json"
    os.link(real, extra)
    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.validate_checkpoint(run_dir, payload)
    assert excinfo.value.category == "link-count"
    extra.unlink()

    # size mismatch
    wrong_bytes = _writer_bytes({"status": "started", "extra": "diff"})
    real.write_bytes(wrong_bytes)
    os.chmod(real, PRIVATE_FILE_MODE)
    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.validate_checkpoint(run_dir, payload)
    assert excinfo.value.category == "size-mismatch"

    # digest mismatch (right size, wrong bytes)
    same_size = b"x" * len(run_json_bytes)
    real.write_bytes(same_size)
    os.chmod(real, PRIVATE_FILE_MODE)
    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.validate_checkpoint(run_dir, payload)
    assert excinfo.value.category == "digest-mismatch"

    # writer-canonical-byte mismatch: a file with a valid digest (matches its
    # own bytes) but whose bytes are not the aboyeur writer canonical encoding
    # of the JSON object they parse to. The digest check passes (file bytes
    # hash to the declared sha256), then the writer-byte equality check fails.
    real.write_bytes(run_json_bytes)
    os.chmod(real, PRIVATE_FILE_MODE)
    non_writer = b'{"status":"planning"}\n'  # compact, not writer canonical
    non_writer_sha = hashlib.sha256(non_writer).hexdigest()
    non_writer_path = cp_dir / f"{non_writer_sha}.json"
    non_writer_path.write_bytes(non_writer)
    os.chmod(non_writer_path, PRIVATE_FILE_MODE)
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
    assert_private_mode(final, PRIVATE_FILE_MODE)
    assert_private_mode(final.parent, PRIVATE_DIRECTORY_MODE)
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
    os.chmod(final, PRIVATE_FILE_MODE)
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
    os.chmod(real, PRIVATE_FILE_MODE)
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


# -- Finding 1: post-unlink directory fsync ---------------------------------


def test_publish_checkpoint_file_fsyncs_directory_after_temp_unlink(tmp_path, monkeypatch):
    run_dir = _run_dir(tmp_path)
    run_json_bytes = _writer_bytes({"status": "started", "task": "demo"})
    cp_dir = run_dir / "events" / run_checkpoint.CHECKPOINT_DIR_NAME

    real_open = os.open
    real_fsync = os.fsync
    real_unlink = os.unlink
    call_log: list[tuple[str, bool]] = []

    def tracking_unlink(path):
        call_log.append(("unlink", False))
        return real_unlink(path)

    def tracking_open(path, flags, mode=0o666, *, dir_fd=None):
        return real_open(path, flags, mode, dir_fd=dir_fd) if dir_fd is not None else real_open(path, flags, mode)

    def tracking_fsync(fd):
        # Prove the descriptor passed to fsync refers to the checkpoint
        # directory itself (not merely that any fsync occurred later), while
        # the directory descriptor is still open.
        call_log.append(("fsync", _refers_to_cp_dir(fd, cp_dir)))
        return real_fsync(fd)

    monkeypatch.setattr(os, "unlink", tracking_unlink)
    monkeypatch.setattr(os, "fsync", tracking_fsync)
    monkeypatch.setattr(os, "open", tracking_open)

    run_checkpoint.publish_checkpoint_file(run_dir, run_json_bytes)

    # A directory fsync on the checkpoint directory must occur after the
    # legitimate temp unlink, so look for an unlink followed by a later
    # fsync whose descriptor refers to the checkpoint directory.
    unlink_indices = [i for i, (kind, _) in enumerate(call_log) if kind == "unlink"]
    assert unlink_indices, "temp was never unlinked"
    first_unlink = unlink_indices[0]
    dir_fsyncs_after = [
        i for i, (kind, refers_cp) in enumerate(call_log) if kind == "fsync" and refers_cp and i > first_unlink
    ]
    assert dir_fsyncs_after, "no checkpoint-directory fsync after temp unlink"


# -- Finding 2: failure paths must not leak temps ----------------------------


def _list_temps(cp_dir: Path) -> list[Path]:
    if not cp_dir.is_dir():
        return []
    return [p for p in cp_dir.iterdir() if p.name.startswith(".checkpoint.") and p.name.endswith(".tmp")]


def test_publish_checkpoint_file_removes_temp_when_chmod_fails(tmp_path, monkeypatch):
    run_dir = _run_dir(tmp_path)
    run_json_bytes = _writer_bytes({"status": "started"})
    cp_dir = run_dir / "events" / run_checkpoint.CHECKPOINT_DIR_NAME

    def fail_chmod_fd_or_path(fd, path, mode):
        raise OSError(errno.EPERM, "chmod denied")

    monkeypatch.setattr(run_journal, "_chmod_fd_or_path", fail_chmod_fd_or_path)

    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.publish_checkpoint_file(run_dir, run_json_bytes)
    assert excinfo.value.category == "chmod"
    assert _list_temps(cp_dir) == []


@pytest.mark.parametrize("has_fchmod", [True, False])
def test_publish_checkpoint_file_removes_temp_when_chmod_fails_under_both_backends(tmp_path, monkeypatch, has_fchmod):
    run_dir = _run_dir(tmp_path)
    run_json_bytes = _writer_bytes({"status": "started"})
    cp_dir = run_dir / "events" / run_checkpoint.CHECKPOINT_DIR_NAME

    monkeypatch.setattr(run_journal, "_HAS_FCHMOD", has_fchmod)

    def fail_chmod_fd_or_path(fd, path, mode):
        raise OSError(errno.EPERM, "chmod denied")

    monkeypatch.setattr(run_journal, "_chmod_fd_or_path", fail_chmod_fd_or_path)

    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.publish_checkpoint_file(run_dir, run_json_bytes)
    assert excinfo.value.category == "chmod"
    assert _list_temps(cp_dir) == []


def test_publish_checkpoint_file_removes_temp_when_write_raises(tmp_path, monkeypatch):
    run_dir = _run_dir(tmp_path)
    run_json_bytes = _writer_bytes({"status": "started"})
    cp_dir = run_dir / "events" / run_checkpoint.CHECKPOINT_DIR_NAME

    def fail_write(fd, data):
        raise OSError(errno.EIO, "write denied")

    monkeypatch.setattr(os, "write", fail_write)

    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.publish_checkpoint_file(run_dir, run_json_bytes)
    assert excinfo.value.category == "io"
    assert _list_temps(cp_dir) == []


def test_publish_checkpoint_file_removes_temp_when_fsync_fails(tmp_path, monkeypatch):
    run_dir = _run_dir(tmp_path)
    run_json_bytes = _writer_bytes({"status": "started"})
    cp_dir = run_dir / "events" / run_checkpoint.CHECKPOINT_DIR_NAME

    def fail_fsync(fd):
        raise OSError(errno.EIO, "fsync denied")

    monkeypatch.setattr(os, "fsync", fail_fsync)

    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.publish_checkpoint_file(run_dir, run_json_bytes)
    assert excinfo.value.category == "io"
    assert _list_temps(cp_dir) == []


def test_publish_checkpoint_file_removes_temp_when_link_raises_nonexist(tmp_path, monkeypatch):
    run_dir = _run_dir(tmp_path)
    run_json_bytes = _writer_bytes({"status": "started"})
    cp_dir = run_dir / "events" / run_checkpoint.CHECKPOINT_DIR_NAME

    def fail_link(src, dst):
        raise OSError(errno.EACCES, "link denied")

    monkeypatch.setattr(os, "link", fail_link)

    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.publish_checkpoint_file(run_dir, run_json_bytes)
    assert excinfo.value.category == "link"
    assert _list_temps(cp_dir) == []


def test_publish_checkpoint_file_removes_temp_when_collision_verify_raises(tmp_path):
    run_dir = _run_dir(tmp_path)
    run_json_bytes = _writer_bytes({"status": "started"})
    cp_dir = run_dir / "events" / run_checkpoint.CHECKPOINT_DIR_NAME
    sha = hashlib.sha256(run_json_bytes).hexdigest()
    # Pre-place a mismatched collision so _verify_collision raises collision-mismatch.
    final = cp_dir / f"{sha}.json"
    cp_dir.mkdir(parents=True, exist_ok=True)
    final.write_bytes(_writer_bytes({"status": "different"}))
    os.chmod(final, PRIVATE_FILE_MODE)

    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.publish_checkpoint_file(run_dir, run_json_bytes)
    assert excinfo.value.category == "collision-mismatch"
    assert _list_temps(cp_dir) == []


def test_publish_checkpoint_file_removes_temp_when_collision_open_fails_raw(tmp_path, monkeypatch):
    run_dir = _run_dir(tmp_path)
    run_json_bytes = _writer_bytes({"status": "started"})
    cp_dir = run_dir / "events" / run_checkpoint.CHECKPOINT_DIR_NAME
    sha = hashlib.sha256(run_json_bytes).hexdigest()
    # Pre-place a matching collision so os.link raises FileExistsError and the
    # collision-race path invokes _verify_collision.
    final = cp_dir / f"{sha}.json"
    cp_dir.mkdir(parents=True, exist_ok=True)
    final.write_bytes(run_json_bytes)
    os.chmod(final, PRIVATE_FILE_MODE)

    real_open = os.open

    def fail_collision_open(path, flags, mode=0o666, *, dir_fd=None):
        if Path(path) == final:
            raise OSError(errno.EACCES, "collision open denied")
        return real_open(path, flags, mode, dir_fd=dir_fd) if dir_fd is not None else real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", fail_collision_open)

    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.publish_checkpoint_file(run_dir, run_json_bytes)
    assert excinfo.value.category == "collision-unsafe"
    assert len(str(excinfo.value)) <= run_events.MAX_DIAGNOSTIC_LEN
    assert _list_temps(cp_dir) == []


def test_publish_checkpoint_file_removes_temp_when_dir_fsync_fails(tmp_path, monkeypatch):
    run_dir = _run_dir(tmp_path)
    run_json_bytes = _writer_bytes({"status": "started"})
    cp_dir = run_dir / "events" / run_checkpoint.CHECKPOINT_DIR_NAME

    real_fsync = os.fsync
    call_count = {"n": 0}

    def fail_dir_fsync(fd):
        call_count["n"] += 1
        # Let the temp-file fsync succeed; fail the directory fsync.
        # Distinguish by attempting fstat: directories and files both are fstat-able,
        # so we fail on every fsync call after the first.
        if call_count["n"] > 1:
            raise OSError(errno.EIO, "dir fsync denied")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_dir_fsync)

    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.publish_checkpoint_file(run_dir, run_json_bytes)
    assert excinfo.value.category == "dir-fsync"
    assert _list_temps(cp_dir) == []


def test_publish_checkpoint_file_cleanup_failure_is_bounded_when_no_primary(tmp_path, monkeypatch):
    run_dir = _run_dir(tmp_path)
    run_json_bytes = _writer_bytes({"status": "started"})

    def fail_unlink(path):
        raise OSError(errno.EACCES, "unlink denied")

    monkeypatch.setattr(os, "unlink", fail_unlink)

    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.publish_checkpoint_file(run_dir, run_json_bytes)
    assert excinfo.value.category == "unlink"
    assert len(str(excinfo.value)) <= run_events.MAX_DIAGNOSTIC_LEN


def test_publish_checkpoint_file_preserves_primary_when_cleanup_also_fails(tmp_path, monkeypatch):
    run_dir = _run_dir(tmp_path)
    run_json_bytes = _writer_bytes({"status": "started"})

    real_fsync = os.fsync
    fsync_count = {"n": 0}

    def fail_dir_fsync(fd):
        fsync_count["n"] += 1
        if fsync_count["n"] > 1:
            raise OSError(errno.EIO, "dir fsync denied")
        return real_fsync(fd)

    def fail_unlink(path):
        raise OSError(errno.EACCES, "unlink denied")

    monkeypatch.setattr(os, "fsync", fail_dir_fsync)
    monkeypatch.setattr(os, "unlink", fail_unlink)

    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.publish_checkpoint_file(run_dir, run_json_bytes)
    # Primary is the dir-fsync failure, not the cleanup unlink failure.
    assert excinfo.value.category == "dir-fsync"


# -- Finding 3: no unconditional fchmod -------------------------------------


def test_publish_checkpoint_file_succeeds_without_fchmod(tmp_path, monkeypatch):
    monkeypatch.setattr(run_journal, "_HAS_FCHMOD", False)
    run_dir = _run_dir(tmp_path)
    run_json_bytes = _writer_bytes({"status": "started"})

    final = run_checkpoint.publish_checkpoint_file(run_dir, run_json_bytes)

    assert final.is_file()
    assert_private_mode(final, PRIVATE_FILE_MODE)
    assert final.read_bytes() == run_json_bytes


def test_publish_checkpoint_file_does_not_call_fchmod_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(run_journal, "_HAS_FCHMOD", False)
    run_dir = _run_dir(tmp_path)
    run_json_bytes = _writer_bytes({"status": "started"})

    # If the code path still called os.fchmod it would raise AttributeError on
    # hosts without it; we simulate by removing the attribute entirely. Let
    # monkeypatch manage state without reading the attribute first so the test
    # is portable on hosts where os.fchmod is initially absent.
    monkeypatch.delattr(os, "fchmod", raising=False)
    run_checkpoint.publish_checkpoint_file(run_dir, run_json_bytes)


# -- Finding 4: non-string payload keys + raw OSError translation ------------


def test_validate_checkpoint_rejects_non_string_payload_key_before_sort(tmp_path):
    run_dir = _run_dir(tmp_path)
    good_bytes = _writer_bytes({"status": "started"})
    good_payload = _checkpoint_payload(good_bytes)
    bad = dict(good_payload)
    bad["unexpected"] = "string extra"  # type: ignore[dict-item]
    bad[1] = "non-string key"  # type: ignore[dict-item]

    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.validate_checkpoint(run_dir, bad)
    assert excinfo.value.category == "payload-keys"
    assert len(str(excinfo.value)) <= run_events.MAX_DIAGNOSTIC_LEN


def test_publish_checkpoint_file_translates_raw_oserror_on_mkdir(tmp_path, monkeypatch):
    run_dir = _run_dir(tmp_path)
    run_json_bytes = _writer_bytes({"status": "started"})

    def fail_mkdir(path, *, parents=False, exist_ok=False, mode=0o777):
        raise OSError(errno.EACCES, "mkdir denied")

    monkeypatch.setattr(Path, "mkdir", fail_mkdir)

    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.publish_checkpoint_file(run_dir, run_json_bytes)
    assert excinfo.value.category == "mkdir"
    assert len(str(excinfo.value)) <= run_events.MAX_DIAGNOSTIC_LEN


def test_publish_checkpoint_file_translates_raw_oserror_on_mkstemp(tmp_path, monkeypatch):
    run_dir = _run_dir(tmp_path)
    run_json_bytes = _writer_bytes({"status": "started"})

    def fail_mkstemp(*args, **kwargs):
        raise OSError(errno.EACCES, "mkstemp denied")

    monkeypatch.setattr(tempfile, "mkstemp", fail_mkstemp)

    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.publish_checkpoint_file(run_dir, run_json_bytes)
    assert excinfo.value.category == "temp-create"
    assert len(str(excinfo.value)) <= run_events.MAX_DIAGNOSTIC_LEN


# -- Finding 5: bounded reads, reject growth after fstat --------------------


def test_validate_checkpoint_rejects_growth_after_fstat(tmp_path, monkeypatch):
    run_dir = _run_dir(tmp_path)
    run_json_bytes = _writer_bytes({"status": "started"})
    final = _place_checkpoint_file(run_dir, run_json_bytes)
    payload = _checkpoint_payload(run_json_bytes)

    real_fstat = os.fstat
    extra = b"\n" * 64

    def extending_fstat(fd):
        st = real_fstat(fd)
        # Extend the same inode through a separate handle AFTER fstat returns
        # the declared size, simulating concurrent growth on the open inode.
        with open(final, "ab") as handle:
            handle.write(extra)
        return st

    monkeypatch.setattr(os, "fstat", extending_fstat)

    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.validate_checkpoint(run_dir, payload)
    assert excinfo.value.category == "size-mismatch"
    assert len(str(excinfo.value)) <= run_events.MAX_DIAGNOSTIC_LEN


def test_validate_checkpoint_rejects_growth_after_fstat_via_collision(tmp_path, monkeypatch):
    run_dir = _run_dir(tmp_path)
    run_json_bytes = _writer_bytes({"status": "started"})
    final = _place_checkpoint_file(run_dir, run_json_bytes)
    sha = hashlib.sha256(run_json_bytes).hexdigest()

    real_fstat = os.fstat
    extra = b"\n" * 64

    def extending_fstat(fd):
        st = real_fstat(fd)
        with open(final, "ab") as handle:
            handle.write(extra)
        return st

    monkeypatch.setattr(os, "fstat", extending_fstat)

    # Drive _verify_collision directly: a matching-collision path that must
    # also bound its reads.
    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint._verify_collision(final, run_json_bytes, sha)
    assert excinfo.value.category == "size-mismatch"


# -- Re-review Finding 1: centralized cleanup fsyncs on every path -----------


def _refers_to_cp_dir(fd, cp_dir: Path) -> bool:
    try:
        st = os.fstat(fd)
        cp_st = cp_dir.stat()
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and st.st_ino == cp_st.st_ino and st.st_dev == cp_st.st_dev


def test_publish_checkpoint_file_fsyncs_directory_after_collision_unlink(tmp_path, monkeypatch):
    run_dir = _run_dir(tmp_path)
    run_json_bytes = _writer_bytes({"status": "started"})
    cp_dir = run_dir / "events" / run_checkpoint.CHECKPOINT_DIR_NAME
    _place_checkpoint_file(run_dir, run_json_bytes)

    real_open = os.open
    real_fsync = os.fsync
    real_unlink = os.unlink
    call_log: list[tuple[str, bool]] = []

    def tracking_unlink(path):
        call_log.append(("unlink", False))
        return real_unlink(path)

    def tracking_open(path, flags, mode=0o666, *, dir_fd=None):
        return real_open(path, flags, mode, dir_fd=dir_fd) if dir_fd is not None else real_open(path, flags, mode)

    def tracking_fsync(fd):
        call_log.append(("fsync", _refers_to_cp_dir(fd, cp_dir)))
        return real_fsync(fd)

    monkeypatch.setattr(os, "unlink", tracking_unlink)
    monkeypatch.setattr(os, "open", tracking_open)
    monkeypatch.setattr(os, "fsync", tracking_fsync)

    run_checkpoint.publish_checkpoint_file(run_dir, run_json_bytes)

    unlink_indices = [i for i, (kind, _) in enumerate(call_log) if kind == "unlink"]
    assert unlink_indices, "temp was never unlinked on collision path"
    first_unlink = unlink_indices[0]
    dir_fsyncs_after = [
        i for i, (kind, refers_cp) in enumerate(call_log) if kind == "fsync" and refers_cp and i > first_unlink
    ]
    assert dir_fsyncs_after, "no checkpoint-directory fsync after collision temp unlink"


def test_publish_checkpoint_file_fsyncs_directory_after_error_path_cleanup(tmp_path, monkeypatch):
    run_dir = _run_dir(tmp_path)
    run_json_bytes = _writer_bytes({"status": "started"})
    cp_dir = run_dir / "events" / run_checkpoint.CHECKPOINT_DIR_NAME

    real_open = os.open
    real_fsync = os.fsync
    real_unlink = os.unlink
    call_log: list[tuple[str, bool]] = []

    def fail_link(src, dst):
        raise OSError(errno.EACCES, "link denied")

    def tracking_unlink(path):
        call_log.append(("unlink", False))
        return real_unlink(path)

    def tracking_open(path, flags, mode=0o666, *, dir_fd=None):
        return real_open(path, flags, mode, dir_fd=dir_fd) if dir_fd is not None else real_open(path, flags, mode)

    def tracking_fsync(fd):
        call_log.append(("fsync", _refers_to_cp_dir(fd, cp_dir)))
        return real_fsync(fd)

    monkeypatch.setattr(os, "link", fail_link)
    monkeypatch.setattr(os, "unlink", tracking_unlink)
    monkeypatch.setattr(os, "open", tracking_open)
    monkeypatch.setattr(os, "fsync", tracking_fsync)

    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.publish_checkpoint_file(run_dir, run_json_bytes)
    assert excinfo.value.category == "link"

    unlink_indices = [i for i, (kind, _) in enumerate(call_log) if kind == "unlink"]
    assert unlink_indices, "temp was never cleaned up on the error path"
    first_unlink = unlink_indices[0]
    dir_fsyncs_after = [
        i for i, (kind, refers_cp) in enumerate(call_log) if kind == "fsync" and refers_cp and i > first_unlink
    ]
    assert dir_fsyncs_after, "no checkpoint-directory fsync after error-path cleanup unlink"


def test_publish_checkpoint_file_cleanup_dir_fsync_failure_is_bounded_when_no_primary(tmp_path, monkeypatch):
    run_dir = _run_dir(tmp_path)
    run_json_bytes = _writer_bytes({"status": "started"})

    real_fsync = os.fsync
    fsync_count = {"n": 0}

    def fail_dir_fsync(fd):
        fsync_count["n"] += 1
        # temp-file fsync (#1) and post-publish dir fsync (#2) succeed; the
        # post-cleanup dir fsync (#3) is the only failure.
        if fsync_count["n"] >= 3:
            raise OSError(errno.EIO, "cleanup dir fsync denied")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_dir_fsync)

    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.publish_checkpoint_file(run_dir, run_json_bytes)
    assert excinfo.value.category == "dir-fsync"
    assert len(str(excinfo.value)) <= run_events.MAX_DIAGNOSTIC_LEN


# -- Re-review Finding 2: fd always closed, close error precedence ----------


def test_publish_checkpoint_file_closes_fd_when_chmod_fails(tmp_path, monkeypatch):
    run_dir = _run_dir(tmp_path)
    run_json_bytes = _writer_bytes({"status": "started"})

    real_mkstemp = tempfile.mkstemp
    real_close = os.close
    opened_fds: list[int] = []
    closed_fds: list[int] = []

    def tracking_mkstemp(*args, **kwargs):
        fd, name = real_mkstemp(*args, **kwargs)
        opened_fds.append(fd)
        return fd, name

    def fail_chmod_fd_or_path(fd, path, mode):
        raise OSError(errno.EPERM, "chmod denied")

    def tracking_close(fd):
        closed_fds.append(fd)
        return real_close(fd)

    monkeypatch.setattr(tempfile, "mkstemp", tracking_mkstemp)
    monkeypatch.setattr(run_journal, "_chmod_fd_or_path", fail_chmod_fd_or_path)
    monkeypatch.setattr(os, "close", tracking_close)

    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.publish_checkpoint_file(run_dir, run_json_bytes)
    assert excinfo.value.category == "chmod"
    assert opened_fds, "mkstemp fd was never opened"
    mkstemp_fd = opened_fds[0]
    assert mkstemp_fd in closed_fds, "mkstemp fd was leaked when chmod failed"
    with pytest.raises(OSError) as fd_exc:
        os.fstat(mkstemp_fd)
    assert fd_exc.value.errno == errno.EBADF


def test_publish_checkpoint_file_chmod_failure_preserved_when_close_also_fails(tmp_path, monkeypatch):
    run_dir = _run_dir(tmp_path)
    run_json_bytes = _writer_bytes({"status": "started"})

    real_mkstemp = tempfile.mkstemp
    real_close = os.close
    opened_fds: list[int] = []

    def tracking_mkstemp(*args, **kwargs):
        fd, name = real_mkstemp(*args, **kwargs)
        opened_fds.append(fd)
        return fd, name

    def fail_chmod_fd_or_path(fd, path, mode):
        raise OSError(errno.EPERM, "chmod denied")

    def fail_close(fd):
        if opened_fds and fd == opened_fds[0]:
            real_close(fd)
            raise OSError(errno.EBADF, "close denied")
        real_close(fd)

    monkeypatch.setattr(tempfile, "mkstemp", tracking_mkstemp)
    monkeypatch.setattr(run_journal, "_chmod_fd_or_path", fail_chmod_fd_or_path)
    monkeypatch.setattr(os, "close", fail_close)

    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.publish_checkpoint_file(run_dir, run_json_bytes)
    assert excinfo.value.category == "chmod"


def test_publish_checkpoint_file_close_failure_is_bounded_when_no_primary(tmp_path, monkeypatch):
    run_dir = _run_dir(tmp_path)
    run_json_bytes = _writer_bytes({"status": "started"})

    real_mkstemp = tempfile.mkstemp
    real_close = os.close
    opened_fds: list[int] = []

    def tracking_mkstemp(*args, **kwargs):
        fd, name = real_mkstemp(*args, **kwargs)
        opened_fds.append(fd)
        return fd, name

    def fail_close(fd):
        if opened_fds and fd == opened_fds[0]:
            real_close(fd)
            raise OSError(errno.EBADF, "close denied")
        real_close(fd)

    monkeypatch.setattr(tempfile, "mkstemp", tracking_mkstemp)
    monkeypatch.setattr(os, "close", fail_close)

    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.publish_checkpoint_file(run_dir, run_json_bytes)
    assert excinfo.value.category == "close"
    assert len(str(excinfo.value)) <= run_events.MAX_DIAGNOSTIC_LEN


# -- Re-review Finding 3: raw OSError translation in validate/verify --------


def _tracking_open_factory(monkeypatch):
    real_open = os.open
    opened: list[int] = []

    def tracking_open(path, flags, mode=0o666, *, dir_fd=None):
        fd = real_open(path, flags, mode, dir_fd=dir_fd) if dir_fd is not None else real_open(path, flags, mode)
        opened.append(fd)
        return fd

    monkeypatch.setattr(os, "open", tracking_open)
    return opened


def _fail_last_close_factory(opened: list[int], monkeypatch):
    real_close = os.close

    def fail_close(fd):
        if opened and fd == opened[-1]:
            real_close(fd)
            raise OSError(errno.EBADF, "close denied")
        real_close(fd)

    monkeypatch.setattr(os, "close", fail_close)
    return fail_close


def test_validate_checkpoint_translates_raw_oserror_on_fstat(tmp_path, monkeypatch):
    run_dir = _run_dir(tmp_path)
    run_json_bytes = _writer_bytes({"status": "started"})
    _place_checkpoint_file(run_dir, run_json_bytes)
    payload = _checkpoint_payload(run_json_bytes)

    monkeypatch.setattr(os, "fstat", lambda fd: (_ for _ in ()).throw(OSError(errno.EIO, "fstat denied")))

    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.validate_checkpoint(run_dir, payload)
    assert excinfo.value.category == "fstat"
    assert len(str(excinfo.value)) <= run_events.MAX_DIAGNOSTIC_LEN


def test_validate_checkpoint_translates_raw_oserror_on_open_missing(tmp_path):
    run_dir = _run_dir(tmp_path)
    run_json_bytes = _writer_bytes({"status": "started"})
    payload = _checkpoint_payload(run_json_bytes)
    # Do NOT place the checkpoint file: _open_nofollow raises raw FileNotFoundError.

    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.validate_checkpoint(run_dir, payload)
    assert excinfo.value.category == "open-fd"
    assert len(str(excinfo.value)) <= run_events.MAX_DIAGNOSTIC_LEN


def test_validate_checkpoint_translates_raw_oserror_on_open_denied(tmp_path, monkeypatch):
    run_dir = _run_dir(tmp_path)
    run_json_bytes = _writer_bytes({"status": "started"})
    _place_checkpoint_file(run_dir, run_json_bytes)
    payload = _checkpoint_payload(run_json_bytes)
    final_path = run_checkpoint.checkpoint_path(run_dir, hashlib.sha256(run_json_bytes).hexdigest())

    real_open = os.open

    def fail_open(path, flags, mode=0o666, *, dir_fd=None):
        if Path(path) == final_path:
            raise OSError(errno.EACCES, "open denied")
        return real_open(path, flags, mode, dir_fd=dir_fd) if dir_fd is not None else real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", fail_open)

    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.validate_checkpoint(run_dir, payload)
    assert excinfo.value.category == "open-fd"
    assert len(str(excinfo.value)) <= run_events.MAX_DIAGNOSTIC_LEN


def test_validate_checkpoint_translates_raw_oserror_on_read(tmp_path, monkeypatch):
    run_dir = _run_dir(tmp_path)
    run_json_bytes = _writer_bytes({"status": "started"})
    _place_checkpoint_file(run_dir, run_json_bytes)
    payload = _checkpoint_payload(run_json_bytes)

    monkeypatch.setattr(os, "read", lambda fd, n: (_ for _ in ()).throw(OSError(errno.EIO, "read denied")))

    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.validate_checkpoint(run_dir, payload)
    assert excinfo.value.category == "read"
    assert len(str(excinfo.value)) <= run_events.MAX_DIAGNOSTIC_LEN


def test_validate_checkpoint_translates_raw_oserror_on_close(tmp_path, monkeypatch):
    run_dir = _run_dir(tmp_path)
    run_json_bytes = _writer_bytes({"status": "started"})
    _place_checkpoint_file(run_dir, run_json_bytes)
    payload = _checkpoint_payload(run_json_bytes)

    opened = _tracking_open_factory(monkeypatch)
    monkeypatch.setattr(os, "close", _fail_last_close_factory(opened, monkeypatch))

    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.validate_checkpoint(run_dir, payload)
    assert excinfo.value.category == "close"
    assert len(str(excinfo.value)) <= run_events.MAX_DIAGNOSTIC_LEN


def test_validate_checkpoint_preserves_primary_when_close_also_fails(tmp_path, monkeypatch):
    run_dir = _run_dir(tmp_path)
    run_json_bytes = _writer_bytes({"status": "started"})
    sha = hashlib.sha256(run_json_bytes).hexdigest()
    cp_dir = run_dir / "events" / run_checkpoint.CHECKPOINT_DIR_NAME
    cp_dir.mkdir(parents=True)
    real = cp_dir / f"{sha}.json"
    real.write_bytes(_writer_bytes({"status": "started", "extra": "diff"}))
    os.chmod(real, PRIVATE_FILE_MODE)
    payload = _checkpoint_payload(run_json_bytes)

    opened = _tracking_open_factory(monkeypatch)
    monkeypatch.setattr(os, "close", _fail_last_close_factory(opened, monkeypatch))

    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.validate_checkpoint(run_dir, payload)
    assert excinfo.value.category == "size-mismatch"


def test_verify_collision_translates_raw_oserror_on_fstat(tmp_path, monkeypatch):
    run_dir = _run_dir(tmp_path)
    run_json_bytes = _writer_bytes({"status": "started"})
    final = _place_checkpoint_file(run_dir, run_json_bytes)
    sha = hashlib.sha256(run_json_bytes).hexdigest()

    monkeypatch.setattr(os, "fstat", lambda fd: (_ for _ in ()).throw(OSError(errno.EIO, "fstat denied")))

    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint._verify_collision(final, run_json_bytes, sha)
    assert excinfo.value.category == "fstat"
    assert len(str(excinfo.value)) <= run_events.MAX_DIAGNOSTIC_LEN


def test_verify_collision_translates_raw_oserror_on_read(tmp_path, monkeypatch):
    run_dir = _run_dir(tmp_path)
    run_json_bytes = _writer_bytes({"status": "started"})
    final = _place_checkpoint_file(run_dir, run_json_bytes)
    sha = hashlib.sha256(run_json_bytes).hexdigest()

    monkeypatch.setattr(os, "read", lambda fd, n: (_ for _ in ()).throw(OSError(errno.EIO, "read denied")))

    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint._verify_collision(final, run_json_bytes, sha)
    assert excinfo.value.category == "read"
    assert len(str(excinfo.value)) <= run_events.MAX_DIAGNOSTIC_LEN


def test_verify_collision_translates_raw_oserror_on_close(tmp_path, monkeypatch):
    run_dir = _run_dir(tmp_path)
    run_json_bytes = _writer_bytes({"status": "started"})
    final = _place_checkpoint_file(run_dir, run_json_bytes)
    sha = hashlib.sha256(run_json_bytes).hexdigest()

    opened = _tracking_open_factory(monkeypatch)
    monkeypatch.setattr(os, "close", _fail_last_close_factory(opened, monkeypatch))

    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint._verify_collision(final, run_json_bytes, sha)
    assert excinfo.value.category == "close"
    assert len(str(excinfo.value)) <= run_events.MAX_DIAGNOSTIC_LEN


def test_verify_collision_translates_raw_oserror_on_open(tmp_path, monkeypatch):
    run_dir = _run_dir(tmp_path)
    run_json_bytes = _writer_bytes({"status": "started"})
    final = _place_checkpoint_file(run_dir, run_json_bytes)
    sha = hashlib.sha256(run_json_bytes).hexdigest()

    real_open = os.open

    def fail_open(path, flags, mode=0o666, *, dir_fd=None):
        if Path(path) == final:
            raise OSError(errno.EACCES, "open denied")
        return real_open(path, flags, mode, dir_fd=dir_fd) if dir_fd is not None else real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", fail_open)

    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint._verify_collision(final, run_json_bytes, sha)
    assert excinfo.value.category == "collision-unsafe"
    assert len(str(excinfo.value)) <= run_events.MAX_DIAGNOSTIC_LEN


def test_verify_collision_preserves_primary_when_close_also_fails(tmp_path, monkeypatch):
    run_dir = _run_dir(tmp_path)
    run_json_bytes = _writer_bytes({"status": "started"})
    sha = hashlib.sha256(run_json_bytes).hexdigest()
    cp_dir = run_dir / "events" / run_checkpoint.CHECKPOINT_DIR_NAME
    cp_dir.mkdir(parents=True)
    final = cp_dir / f"{sha}.json"
    final.write_bytes(_writer_bytes({"status": "different"}))
    os.chmod(final, PRIVATE_FILE_MODE)

    opened = _tracking_open_factory(monkeypatch)
    monkeypatch.setattr(os, "close", _fail_last_close_factory(opened, monkeypatch))

    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint._verify_collision(final, run_json_bytes, sha)
    assert excinfo.value.category == "collision-mismatch"


# -- Finding: _fsync_directory no-follow hardening --------------------------


def test_fsync_directory_refuses_symlink(tmp_path):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    symlink = tmp_path / "link"
    symlink.symlink_to(real_dir)

    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint._fsync_directory(symlink)
    assert excinfo.value.category == "dir-fsync"
    assert len(str(excinfo.value)) <= run_events.MAX_DIAGNOSTIC_LEN


def test_fsync_directory_refuses_non_directory(tmp_path):
    file_path = tmp_path / "file"
    file_path.write_bytes(b"x")

    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint._fsync_directory(file_path)
    assert excinfo.value.category == "dir-fsync"
    assert len(str(excinfo.value)) <= run_events.MAX_DIAGNOSTIC_LEN


def test_fsync_directory_translates_raw_oserror_on_open(tmp_path, monkeypatch):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    real_open = os.open

    def fail_open(path, flags, mode=0o666, *, dir_fd=None):
        if path == real_dir:
            raise OSError(errno.EACCES, "open denied")
        return real_open(path, flags, mode, dir_fd=dir_fd) if dir_fd is not None else real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", fail_open)

    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint._fsync_directory(real_dir)
    assert excinfo.value.category == "dir-fsync"
    assert len(str(excinfo.value)) <= run_events.MAX_DIAGNOSTIC_LEN


def test_fsync_directory_translates_raw_oserror_on_fstat(tmp_path, monkeypatch):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    monkeypatch.setattr(os, "fstat", lambda fd: (_ for _ in ()).throw(OSError(errno.EIO, "fstat denied")))

    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint._fsync_directory(real_dir)
    assert excinfo.value.category == "dir-fsync"
    assert len(str(excinfo.value)) <= run_events.MAX_DIAGNOSTIC_LEN


def test_fsync_directory_translates_raw_oserror_on_close(tmp_path, monkeypatch):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    opened = _tracking_open_factory(monkeypatch)
    monkeypatch.setattr(os, "close", _fail_last_close_factory(opened, monkeypatch))

    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint._fsync_directory(real_dir)
    assert excinfo.value.category == "dir-fsync"
    assert len(str(excinfo.value)) <= run_events.MAX_DIAGNOSTIC_LEN


def test_fsync_directory_preserves_primary_when_close_also_fails(tmp_path, monkeypatch):
    real_dir = tmp_path / "real"
    real_dir.mkdir()

    def fail_fsync(fd):
        raise OSError(errno.EIO, "fsync denied")

    opened = _tracking_open_factory(monkeypatch)
    monkeypatch.setattr(os, "fsync", fail_fsync)
    monkeypatch.setattr(os, "close", _fail_last_close_factory(opened, monkeypatch))

    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint._fsync_directory(real_dir)
    assert excinfo.value.category == "dir-fsync"


# -- Issue #568 slice 5 Task 1: RecursionError translation at both boundaries --


def _place_deep_checkpoint(run_dir: Path, deep_json: bytes) -> tuple[Path, dict]:
    sha = hashlib.sha256(deep_json).hexdigest()
    cp_dir = run_dir / "events" / run_checkpoint.CHECKPOINT_DIR_NAME
    cp_dir.mkdir(parents=True, exist_ok=True)
    final = cp_dir / f"{sha}.json"
    final.write_bytes(deep_json)
    os.chmod(final, PRIVATE_FILE_MODE)
    return final, _checkpoint_payload(deep_json)


def test_validate_checkpoint_translates_recursion_error_on_json_loads(tmp_path):
    """RecursionError from json.loads on very deep valid JSON -> json-object."""
    run_dir = _run_dir(tmp_path)
    # Depth that exhausts the JSON decoder's recursion capacity: a deeply
    # nested but otherwise valid JSON array. Byte size is well under
    # MAX_CHECKPOINT_BYTES; the digest and UTF-8 checks pass, then
    # json.loads raises RecursionError (not a ValueError subclass).
    depth = 20000
    deep_json = b"[" * depth + b"]" * depth
    _final, payload = _place_deep_checkpoint(run_dir, deep_json)

    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.validate_checkpoint(run_dir, payload)
    err = excinfo.value
    assert err.category == "json-object", f"expected json-object, got {err.category}"
    assert isinstance(err, run_checkpoint.CheckpointError)
    diagnostic = str(err)
    assert diagnostic == err.diagnostic
    assert len(diagnostic) <= run_events.MAX_DIAGNOSTIC_LEN


def test_validate_checkpoint_translates_recursion_error_on_writer_canonical_dumps(tmp_path, monkeypatch):
    """RecursionError from canonical json.dumps re-encoding -> writer-bytes."""
    run_dir = _run_dir(tmp_path)
    canonical = _writer_bytes({"status": "running"})
    _place_checkpoint_file(run_dir, canonical)
    payload = _checkpoint_payload(canonical)

    def fail_writer_canonical_bytes(_obj):
        raise RecursionError("canonical writer nesting too deep")

    monkeypatch.setattr(run_checkpoint, "_writer_canonical_bytes", fail_writer_canonical_bytes)

    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.validate_checkpoint(run_dir, payload)
    err = excinfo.value
    assert err.category == "writer-bytes", f"expected writer-bytes, got {err.category}"
    assert isinstance(err, run_checkpoint.CheckpointError)
    diagnostic = str(err)
    assert diagnostic == err.diagnostic
    assert len(diagnostic) <= run_events.MAX_DIAGNOSTIC_LEN


# -- Issue #568 slice 5 Task 2: run_checkpoint.write_checkpoint coordinator ----


_REQUEST_FIELD = "lifecycle_journal_requested"


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace


def _journal_path(run_dir: Path) -> Path:
    return run_dir / "events" / "lifecycle.jsonl"


def _checkpoint_dir(run_dir: Path) -> Path:
    return run_dir / "events" / run_checkpoint.CHECKPOINT_DIR_NAME


def _events(run_dir: Path) -> list[run_journal.RunEvent]:
    return run_journal.read_journal(_journal_path(run_dir)).events


def _checkpoint_events(run_dir: Path) -> list[run_journal.RunEvent]:
    return [e for e in _events(run_dir) if e.event_type == run_checkpoint.CHECKPOINT_EVENT_TYPE]


def _bootstrap_request(run_dir: Path, status: str = "started") -> None:
    """Pre-lock bootstrap: write run.json with the durable request, no journal."""
    localio.write_json(
        run_dir / "run.json",
        {"schema": "brigade.run.v1", "status": status, _REQUEST_FIELD: True},
    )


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("BRIGADE_LIFECYCLE_JOURNAL", "1")
    yield
    monkeypatch.delenv("BRIGADE_LIFECYCLE_JOURNAL", raising=False)


def test_write_checkpoint_append_then_replay(enabled, tmp_path):
    workspace = _workspace(tmp_path)
    run_dir = _run_dir(tmp_path)
    _bootstrap_request(run_dir)
    with runguard.run_lock(workspace, run_dir=run_dir):
        run_lifecycle.prepare_lifecycle_journal(run_dir, workspace=workspace)

    run_json_bytes = _writer_bytes({"schema": "brigade.run.v1", "status": "planning"})
    with runguard.run_lock(workspace, run_dir=run_dir):
        run_checkpoint.write_checkpoint(
            run_dir, run_json_bytes, workspace=workspace, paired_event_type="run.planning.started"
        )
    events_after_first = _events(run_dir)
    files_after_first = sorted(_checkpoint_dir(run_dir).iterdir())
    journal_after_first = _journal_path(run_dir).read_bytes()

    # Replay: same bytes + same pairing derive the same idempotency key, so
    # no new file and no new event.
    with runguard.run_lock(workspace, run_dir=run_dir):
        run_checkpoint.write_checkpoint(
            run_dir, run_json_bytes, workspace=workspace, paired_event_type="run.planning.started"
        )

    assert _events(run_dir) == events_after_first
    assert sorted(_checkpoint_dir(run_dir).iterdir()) == files_after_first
    assert _journal_path(run_dir).read_bytes() == journal_after_first


def test_write_checkpoint_same_bytes_different_pairing_distinct_events(enabled, tmp_path):
    workspace = _workspace(tmp_path)
    run_dir = _run_dir(tmp_path)
    _bootstrap_request(run_dir)
    with runguard.run_lock(workspace, run_dir=run_dir):
        run_lifecycle.prepare_lifecycle_journal(run_dir, workspace=workspace)

    run_json_bytes = _writer_bytes({"schema": "brigade.run.v1", "status": "planning"})
    with runguard.run_lock(workspace, run_dir=run_dir):
        first = run_checkpoint.write_checkpoint(
            run_dir, run_json_bytes, workspace=workspace, paired_event_type="run.planning.started"
        )
    with runguard.run_lock(workspace, run_dir=run_dir):
        second = run_checkpoint.write_checkpoint(
            run_dir, run_json_bytes, workspace=workspace, paired_event_type="run.dispatch.requested"
        )

    checkpoints = _checkpoint_events(run_dir)
    assert len(checkpoints) == 2
    assert [e.sequence for e in checkpoints] == [1, 2]
    assert first.event_id == checkpoints[0].event_id
    assert second.event_id == checkpoints[1].event_id
    assert first.idempotency_key != second.idempotency_key
    assert first.idempotency_key.endswith(":run.planning.started")
    assert second.idempotency_key.endswith(":run.dispatch.requested")
    # Same bytes -> same content-addressed file (collision-noop), one file.
    sha = hashlib.sha256(run_json_bytes).hexdigest()
    assert sorted(p.name for p in _checkpoint_dir(run_dir).iterdir()) == [f"{sha}.json"]


def test_write_checkpoint_noop_when_journal_not_active(enabled, tmp_path):
    workspace = _workspace(tmp_path)
    run_dir = _run_dir(tmp_path)
    _bootstrap_request(run_dir)  # pre-lock bootstrap: request only, no journal

    run_json_bytes = _writer_bytes({"schema": "brigade.run.v1", "status": "planning"})
    # No lock held: prepare is a no-op, the journal stays absent, and
    # write_checkpoint no-ops (no file, no event).
    result = run_checkpoint.write_checkpoint(
        run_dir, run_json_bytes, workspace=workspace, paired_event_type="run.planning.started"
    )
    assert result is None
    assert not (run_dir / "events").exists()


# -- Issue #568 slice 5 Task 5: latest_checkpoint_event + recover_from_checkpoint --


def _activated_journal_with_checkpoint(
    workspace: Path,
    run_dir: Path,
    run_json_obj: dict,
    *,
    paired_event_type: str | None = "run.planning.started",
) -> run_journal.RunEvent:
    """Activate the journal under lock and append one checkpoint event.

    The journal ends in the checkpoint event (the recoverable state: the
    crash happened after the checkpoint publish+append but before the
    paired status append). Returns the appended checkpoint RunEvent.
    """
    _bootstrap_request(run_dir)
    with runguard.run_lock(workspace, run_dir=run_dir):
        run_lifecycle.prepare_lifecycle_journal(run_dir, workspace=workspace)
        run_json_bytes = _writer_bytes(run_json_obj)
        event = run_checkpoint.write_checkpoint(
            run_dir, run_json_bytes, workspace=workspace, paired_event_type=paired_event_type
        )
    assert event is not None
    return event


def test_latest_checkpoint_event_returns_none_for_empty_list():
    assert run_checkpoint.latest_checkpoint_event([]) is None


def test_latest_checkpoint_event_returns_none_when_no_checkpoints(tmp_path):
    workspace = _workspace(tmp_path)
    run_dir = _run_dir(tmp_path)
    _bootstrap_request(run_dir)
    with runguard.run_lock(workspace, run_dir=run_dir):
        run_lifecycle.prepare_lifecycle_journal(run_dir, workspace=workspace)
        run_journal.append_event(
            _journal_path(run_dir),
            run_id=RUN_ID,
            event_type="run.created",
            payload={"status": "started"},
            idempotency_key="create-1",
            expected_previous_sequence=0,
            recorded_at="2026-07-27T15:30:45.123456Z",
        )
    events = _events(run_dir)
    assert all(e.event_type != run_checkpoint.CHECKPOINT_EVENT_TYPE for e in events)
    assert run_checkpoint.latest_checkpoint_event(events) is None


def test_latest_checkpoint_event_selects_highest_sequence(tmp_path):
    workspace = _workspace(tmp_path)
    run_dir = _run_dir(tmp_path)
    first_bytes = _writer_bytes({"schema": "brigade.run.v1", "status": "started"})
    second_bytes = _writer_bytes({"schema": "brigade.run.v1", "status": "planning"})
    _bootstrap_request(run_dir)
    with runguard.run_lock(workspace, run_dir=run_dir):
        run_lifecycle.prepare_lifecycle_journal(run_dir, workspace=workspace)
        run_checkpoint.write_checkpoint(run_dir, first_bytes, workspace=workspace, paired_event_type="run.created")
        second = run_checkpoint.write_checkpoint(
            run_dir, second_bytes, workspace=workspace, paired_event_type="run.planning.started"
        )
    events = _events(run_dir)
    latest = run_checkpoint.latest_checkpoint_event(events)
    assert latest is not None
    assert latest.sequence == second.sequence
    assert latest.event_id == second.event_id


def test_recover_from_checkpoint_restores_missing_run_json_from_latest(tmp_path):
    workspace = _workspace(tmp_path)
    run_dir = _run_dir(tmp_path)
    run_json_obj = {"schema": "brigade.run.v1", "status": "planning", "task": "demo"}
    _activated_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    # No run.json present (the crash lost it): drop the bootstrap request file.
    (run_dir / "run.json").unlink()

    repaired = run_checkpoint.recover_from_checkpoint(run_dir, None)

    assert repaired == run_json_obj
    restored_bytes = (run_dir / "run.json").read_bytes()
    assert restored_bytes == _writer_bytes(run_json_obj)


def test_recover_from_checkpoint_preserves_corrupt_run_json_and_replaces(tmp_path):
    workspace = _workspace(tmp_path)
    run_dir = _run_dir(tmp_path)
    run_json_obj = {"schema": "brigade.run.v1", "status": "planning", "task": "demo"}
    _activated_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    (run_dir / "run.json").write_text("not json")

    repaired = run_checkpoint.recover_from_checkpoint(run_dir, None)

    assert repaired == run_json_obj
    assert (run_dir / "run.json").read_bytes() == _writer_bytes(run_json_obj)
    preserved = list(run_dir.glob("run.json.corrupt-*"))
    assert len(preserved) == 1
    assert preserved[0].read_text() == "not json"


def test_recover_from_checkpoint_leaves_parseable_run_json_untouched(tmp_path):
    workspace = _workspace(tmp_path)
    run_dir = _run_dir(tmp_path)
    run_json_obj = {"schema": "brigade.run.v1", "status": "planning", "task": "demo"}
    _activated_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    parseable = {"schema": "brigade.run.v1", "status": "planning", "task": "different"}
    _writer_bytes(parseable)
    (run_dir / "run.json").write_text(json.dumps(parseable, indent=2, sort_keys=True) + "\n")
    before = (run_dir / "run.json").read_bytes()

    repaired = run_checkpoint.recover_from_checkpoint(run_dir, parseable)

    assert repaired == run_json_obj
    assert (run_dir / "run.json").read_bytes() == before
    assert not list(run_dir.glob("run.json.corrupt-*"))


def test_recover_from_checkpoint_quarantines_partial_tail_then_verifies(tmp_path):
    workspace = _workspace(tmp_path)
    run_dir = _run_dir(tmp_path)
    run_json_obj = {"schema": "brigade.run.v1", "status": "planning", "task": "demo"}
    _activated_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    (run_dir / "run.json").unlink()
    journal = _journal_path(run_dir)
    partial = b'{"schema":"brigade.run_event.v1","event_type":"run.plan'
    journal.write_bytes(journal.read_bytes() + partial)
    complete_bytes = journal.read_bytes()[: -len(partial)]

    repaired = run_checkpoint.recover_from_checkpoint(run_dir, None)

    assert repaired == run_json_obj
    assert journal.read_bytes() == complete_bytes
    quarantine_files = [p for p in (run_dir / "events" / "quarantine").iterdir() if p.suffix == ".bin"]
    assert quarantine_files, "partial tail was not quarantined"
    assert quarantine_files[0].read_bytes() == partial


def test_recover_from_checkpoint_fails_on_invalid_latest_checkpoint(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    run_dir = _run_dir(tmp_path)
    run_json_obj = {"schema": "brigade.run.v1", "status": "planning", "task": "demo"}
    _activated_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    (run_dir / "run.json").unlink()
    # Corrupt the checkpoint file so validate_checkpoint fails (digest mismatch).
    sha = hashlib.sha256(_writer_bytes(run_json_obj)).hexdigest()
    cp_file = run_checkpoint.checkpoint_path(run_dir, sha)
    cp_file.write_bytes(b"x" * len(_writer_bytes(run_json_obj)))
    os.chmod(cp_file, PRIVATE_FILE_MODE)

    with pytest.raises(run_checkpoint.CheckpointError):
        run_checkpoint.recover_from_checkpoint(run_dir, None)
    assert not (run_dir / "run.json").exists()


def test_recover_from_checkpoint_accepts_covered_paired_status_event(tmp_path):
    """A checkpoint at N plus its matching paired status event at N+1 is covered.

    The checkpoint's ``paired_event_type`` names the mapped lifecycle event
    type for the checkpointed status, and the N+1 event's derived status
    equals the status in the validated checkpoint bytes. Per the slice-5
    coverage semantics this is a covered tail (crash after the paired
    status append but before the run.json replace), so recovery restores
    the checkpoint bytes without mutation beyond the restore.
    """
    workspace = _workspace(tmp_path)
    run_dir = _run_dir(tmp_path)
    run_json_obj = {"schema": "brigade.run.v1", "status": "planning", "task": "demo"}
    _bootstrap_request(run_dir)
    with runguard.run_lock(workspace, run_dir=run_dir):
        run_lifecycle.prepare_lifecycle_journal(run_dir, workspace=workspace)
        run_checkpoint.write_checkpoint(
            run_dir,
            _writer_bytes(run_json_obj),
            workspace=workspace,
            paired_event_type="run.planning.started",
        )
        run_journal.append_event(
            _journal_path(run_dir),
            run_id=RUN_ID,
            event_type="run.planning.started",
            payload={"detail": "planning"},
            idempotency_key="plan-start-1",
            expected_previous_sequence=1,
            recorded_at="2026-07-27T15:30:46.000000Z",
        )
    (run_dir / "run.json").unlink()

    repaired = run_checkpoint.recover_from_checkpoint(run_dir, None)

    assert repaired == run_json_obj
    assert (run_dir / "run.json").read_bytes() == _writer_bytes(run_json_obj)


def _journal_with_checkpoint_and_trailing(
    workspace: Path,
    run_dir: Path,
    run_json_obj: dict,
    *,
    paired_event_type: str | None,
    pairing_key: str | None = None,
    trailing_events: list[tuple[str, dict, str, str]],
) -> run_journal.RunEvent:
    """Activate the journal, write one checkpoint, then append trailing events.

    ``trailing_events`` is a list of ``(event_type, payload, idempotency_key,
    recorded_at)`` tuples appended after the checkpoint, each linking to the
    prior event. Returns the appended checkpoint RunEvent.
    """
    _bootstrap_request(run_dir)
    with runguard.run_lock(workspace, run_dir=run_dir):
        run_lifecycle.prepare_lifecycle_journal(run_dir, workspace=workspace)
        run_json_bytes = _writer_bytes(run_json_obj)
        checkpoint = run_checkpoint.write_checkpoint(
            run_dir,
            run_json_bytes,
            workspace=workspace,
            paired_event_type=paired_event_type,
            pairing_key=pairing_key,
        )
        assert checkpoint is not None
        prev_seq = checkpoint.sequence
        for event_type, payload, key, recorded_at in trailing_events:
            appended = run_journal.append_event(
                _journal_path(run_dir),
                run_id=RUN_ID,
                event_type=event_type,
                payload=payload,
                idempotency_key=key,
                expected_previous_sequence=prev_seq,
                recorded_at=recorded_at,
            )
            prev_seq = appended.sequence
    return checkpoint


def test_recover_from_checkpoint_accepts_trailing_redaction_anchor(tmp_path):
    workspace = _workspace(tmp_path)
    run_dir = _run_dir(tmp_path)
    run_json_obj = {"schema": "brigade.run.v1", "status": "dispatching", "task": "demo"}
    _journal_with_checkpoint_and_trailing(
        workspace,
        run_dir,
        run_json_obj,
        paired_event_type=None,
        trailing_events=[
            (
                "run.redaction.recorded",
                {
                    "operation_id": "redact-0123456789abcdef",
                    "affected_first_sequence": 1,
                    "affected_last_sequence": 1,
                    "reason_class": "credential-exposure",
                    "record_sha256": "a" * 64,
                },
                "redaction-recorded-test",
                "2026-07-27T15:30:46.000000Z",
            )
        ],
    )
    (run_dir / "run.json").unlink()

    assert run_checkpoint.recover_from_checkpoint(run_dir, None) == run_json_obj


@pytest.mark.parametrize(
    ("payload", "pairing_seat", "pairing_attempt"),
    [
        ({"seat": "other", "attempt": 1, "detail": "completed"}, "coder", 1),
        ({"seat": "coder", "attempt": 2, "detail": "completed"}, "coder", 1),
        ({"seat": "coder", "detail": "completed"}, "coder", 1),
    ],
    ids=("wrong-seat", "wrong-attempt", "missing-attempt"),
)
def test_recover_from_checkpoint_rejects_mismatched_dispatch_pairing_identity(
    tmp_path, payload, pairing_seat, pairing_attempt
):
    workspace = _workspace(tmp_path)
    run_dir = _run_dir(tmp_path)
    run_json_obj = {"schema": "brigade.run.v1", "status": "dispatching", "task": "demo"}
    _journal_with_checkpoint_and_trailing(
        workspace,
        run_dir,
        run_json_obj,
        paired_event_type="run.dispatch.completed",
        pairing_key=run_checkpoint.dispatch_pairing_key("run.dispatch.completed", pairing_seat, pairing_attempt),
        trailing_events=[
            ("run.dispatch.completed", payload, "dispatch-completed-1", "2026-07-27T15:30:46.000000Z"),
        ],
    )
    (run_dir / "run.json").unlink()

    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.recover_from_checkpoint(run_dir, None)

    assert excinfo.value.category == "pairing"
    assert not (run_dir / "run.json").exists()


@pytest.mark.parametrize(
    "event_type",
    [
        "run.dispatch.requested",
        "run.dispatch.observed",
        "run.dispatch.completed",
        "run.dispatch.failed",
    ],
)
def test_recover_rejects_dispatch_pairing_checkpoint_at_tail_as_incomplete(tmp_path, event_type):
    workspace = _workspace(tmp_path)
    run_dir = _run_dir(tmp_path)
    run_json_obj = {"schema": "brigade.run.v1", "status": "dispatching", "task": "demo"}
    _journal_with_checkpoint_and_trailing(
        workspace,
        run_dir,
        run_json_obj,
        paired_event_type=event_type,
        pairing_key=run_checkpoint.dispatch_pairing_key(event_type, "coder", 1),
        trailing_events=[],
    )
    (run_dir / "run.json").unlink()

    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.recover_from_checkpoint(run_dir, None)

    assert excinfo.value.category == "incomplete-pair"
    assert len(str(excinfo.value)) <= run_events.MAX_DIAGNOSTIC_LEN
    assert not (run_dir / "run.json").exists()


def test_recover_from_checkpoint_fails_on_wrong_paired_event_type(tmp_path):
    """N+1 event_type differs from the checkpoint's paired_event_type -> uncovered."""
    workspace = _workspace(tmp_path)
    run_dir = _run_dir(tmp_path)
    run_json_obj = {"schema": "brigade.run.v1", "status": "planning", "task": "demo"}
    _journal_with_checkpoint_and_trailing(
        workspace,
        run_dir,
        run_json_obj,
        paired_event_type="run.planning.started",
        trailing_events=[
            ("run.dispatch.requested", {"detail": "dispatching"}, "dispatch-1", "2026-07-27T15:30:46.000000Z"),
        ],
    )
    (run_dir / "run.json").unlink()

    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.recover_from_checkpoint(run_dir, None)
    assert excinfo.value.category == "uncovered-tail"
    assert not (run_dir / "run.json").exists()


def test_recover_from_checkpoint_fails_on_paired_derived_status_mismatch(tmp_path):
    """N+1 event_type matches but its derived status differs from the checkpoint -> uncovered."""
    workspace = _workspace(tmp_path)
    run_dir = _run_dir(tmp_path)
    # Checkpoint status "failed" pairs with run.failed; an N+1 run.failed
    # event whose payload status is "timeout" derives "timeout" != "failed".
    run_json_obj = {"schema": "brigade.run.v1", "status": "failed", "task": "demo"}
    _journal_with_checkpoint_and_trailing(
        workspace,
        run_dir,
        run_json_obj,
        paired_event_type="run.failed",
        trailing_events=[
            ("run.failed", {"status": "timeout"}, "fail-1", "2026-07-27T15:30:46.000000Z"),
        ],
    )
    (run_dir / "run.json").unlink()

    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.recover_from_checkpoint(run_dir, None)
    assert excinfo.value.category == "uncovered-tail"
    assert not (run_dir / "run.json").exists()


def test_recover_from_checkpoint_fails_on_null_pairing_with_following_event(tmp_path):
    """A null paired_event_type covers only checkpoint-at-tail; a following event is uncovered."""
    workspace = _workspace(tmp_path)
    run_dir = _run_dir(tmp_path)
    # Same-status refresh: paired_event_type is null. Any following event is uncovered.
    run_json_obj = {"schema": "brigade.run.v1", "status": "started", "task": "demo"}
    _journal_with_checkpoint_and_trailing(
        workspace,
        run_dir,
        run_json_obj,
        paired_event_type=None,
        trailing_events=[
            ("run.created", {"status": "started"}, "create-1", "2026-07-27T15:30:46.000000Z"),
        ],
    )
    (run_dir / "run.json").unlink()

    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.recover_from_checkpoint(run_dir, None)
    assert excinfo.value.category == "uncovered-tail"
    assert not (run_dir / "run.json").exists()


def test_recover_from_checkpoint_fails_on_two_events_after_checkpoint(tmp_path):
    """Two events after the latest checkpoint (even if the first matches) -> uncovered."""
    workspace = _workspace(tmp_path)
    run_dir = _run_dir(tmp_path)
    run_json_obj = {"schema": "brigade.run.v1", "status": "planning", "task": "demo"}
    _journal_with_checkpoint_and_trailing(
        workspace,
        run_dir,
        run_json_obj,
        paired_event_type="run.planning.started",
        trailing_events=[
            ("run.planning.started", {"detail": "planning"}, "plan-start-1", "2026-07-27T15:30:46.000000Z"),
            ("run.dispatch.requested", {"detail": "dispatching"}, "dispatch-1", "2026-07-27T15:30:47.000000Z"),
        ],
    )
    (run_dir / "run.json").unlink()

    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.recover_from_checkpoint(run_dir, None)
    assert excinfo.value.category == "uncovered-tail"
    assert not (run_dir / "run.json").exists()


def test_recover_from_checkpoint_fails_on_invalid_coverage_with_parseable_run_json(tmp_path):
    """An uncovered tail fails closed even when run.json is parseable; it is left untouched.

    The coverage check runs for every activated-journal run regardless of
    whether run.json parses. A parseable run.json with an uncovered tail
    must fail closed with no mutation to run.json (validation-before-
    mutation: coverage validation completes before the restore).
    """
    workspace = _workspace(tmp_path)
    run_dir = _run_dir(tmp_path)
    run_json_obj = {"schema": "brigade.run.v1", "status": "planning", "task": "demo"}
    _journal_with_checkpoint_and_trailing(
        workspace,
        run_dir,
        run_json_obj,
        paired_event_type="run.planning.started",
        trailing_events=[
            ("run.dispatch.requested", {"detail": "dispatching"}, "dispatch-1", "2026-07-27T15:30:46.000000Z"),
        ],
    )
    parseable = {"schema": "brigade.run.v1", "status": "planning", "task": "different"}
    (run_dir / "run.json").write_text(json.dumps(parseable, indent=2, sort_keys=True) + "\n")
    before = (run_dir / "run.json").read_bytes()

    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.recover_from_checkpoint(run_dir, parseable)
    assert excinfo.value.category == "uncovered-tail"
    assert (run_dir / "run.json").read_bytes() == before
    assert not list(run_dir.glob("run.json.corrupt-*"))


def test_recover_from_checkpoint_fails_on_chain_break(tmp_path):
    workspace = _workspace(tmp_path)
    run_dir = _run_dir(tmp_path)
    run_json_obj = {"schema": "brigade.run.v1", "status": "planning", "task": "demo"}
    _activated_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    # Rewrite the journal with a sequence gap by hand (sequence 1 then 3).
    journal = _journal_path(run_dir)
    events = _events(run_dir)
    assert len(events) == 1
    env = events[0].to_dict()
    env["sequence"] = 3
    env["previous_digest"] = events[0].previous_digest
    # Recompute event_id and event_digest for the tampered envelope.
    env["event_digest"] = run_events.compute_event_digest(env)
    env["event_id"] = run_events.make_event_id(
        run_id=env["run_id"], sequence=env["sequence"], event_digest=env["event_digest"]
    )
    journal.write_bytes(run_events.canonical_bytes(env) + b"\n")

    with pytest.raises(run_checkpoint.CheckpointError):
        run_checkpoint.recover_from_checkpoint(run_dir, None)


def test_recover_from_checkpoint_fails_on_unknown_event(tmp_path):
    workspace = _workspace(tmp_path)
    run_dir = _run_dir(tmp_path)
    run_json_obj = {"schema": "brigade.run.v1", "status": "planning", "task": "demo"}
    _activated_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    journal = _journal_path(run_dir)
    events = _events(run_dir)
    env = events[0].to_dict()
    env["event_type"] = "run.not.a.real.event"
    env["payload"] = {}
    env["event_digest"] = run_events.compute_event_digest(env)
    env["event_id"] = run_events.make_event_id(
        run_id=env["run_id"], sequence=env["sequence"], event_digest=env["event_digest"]
    )
    journal.write_bytes(run_events.canonical_bytes(env) + b"\n")

    with pytest.raises(run_checkpoint.CheckpointError):
        run_checkpoint.recover_from_checkpoint(run_dir, None)


def test_recover_from_checkpoint_fails_on_envelope_run_id_mismatch(tmp_path):
    workspace = _workspace(tmp_path)
    run_dir = _run_dir(tmp_path)
    run_json_obj = {"schema": "brigade.run.v1", "status": "planning", "task": "demo"}
    _activated_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    journal = _journal_path(run_dir)
    events = _events(run_dir)
    env = events[0].to_dict()
    env["run_id"] = "a-different-run-id"
    env["event_digest"] = run_events.compute_event_digest(env)
    env["event_id"] = run_events.make_event_id(
        run_id=env["run_id"], sequence=env["sequence"], event_digest=env["event_digest"]
    )
    journal.write_bytes(run_events.canonical_bytes(env) + b"\n")

    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.recover_from_checkpoint(run_dir, None)
    assert excinfo.value.category == "run-id-mismatch"


def test_recover_from_checkpoint_fails_on_corrupt_rename_oserror(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    run_dir = _run_dir(tmp_path)
    run_json_obj = {"schema": "brigade.run.v1", "status": "planning", "task": "demo"}
    _activated_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    (run_dir / "run.json").write_text("not json")

    real_rename = Path.rename

    def fail_rename_on_corrupt(self, target):
        if str(target).startswith(str(run_dir / "run.json.corrupt-")):
            raise OSError(errno.EACCES, "rename denied")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", fail_rename_on_corrupt)

    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.recover_from_checkpoint(run_dir, None)
    assert excinfo.value.category == "preserve-corrupt"
    # The corrupt original is still in place (rename failed).
    assert (run_dir / "run.json").read_text() == "not json"


# -- Issue #568 slice 5 Task 5 sendback: bounded journal-read normalization --


def test_recover_from_checkpoint_normalizes_oversized_journal_to_checkpoint_error(tmp_path, monkeypatch):
    """An oversized journal surfaces a bounded CheckpointError, not a raw RunJournalError.

    ``read_journal_bounded`` raises ``RunJournalError`` when the journal exceeds
    ``MAX_JOURNAL_BYTES``; ``recover_from_checkpoint`` must normalize that to a
    bounded categorized ``CheckpointError`` as its public contract promises.
    """
    workspace = _workspace(tmp_path)
    run_dir = _run_dir(tmp_path)
    run_json_obj = {"schema": "brigade.run.v1", "status": "planning", "task": "demo"}
    _activated_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    (run_dir / "run.json").unlink()
    # Force the bound check to fire on the existing (small) journal.
    monkeypatch.setattr(run_checkpoint, "MAX_JOURNAL_BYTES", 8)

    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.recover_from_checkpoint(run_dir, None)
    assert excinfo.value.category == "journal-read"
    assert not (run_dir / "run.json").exists()


def test_recover_from_checkpoint_normalizes_raw_oserror_on_initial_journal_read(tmp_path, monkeypatch):
    """A raw ``OSError`` from the initial bounded journal read is normalized.

    ``read_journal_bounded`` can raise a raw ``OSError`` (e.g. permission denied
    on ``_open_nofollow``); the public contract promises a bounded
    ``CheckpointError``.
    """
    workspace = _workspace(tmp_path)
    run_dir = _run_dir(tmp_path)
    run_json_obj = {"schema": "brigade.run.v1", "status": "planning", "task": "demo"}
    _activated_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    (run_dir / "run.json").unlink()

    def raise_oserror(_journal_path):
        raise OSError(errno.EACCES, "permission denied")

    monkeypatch.setattr(run_journal, "read_journal_bounded", raise_oserror)

    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.recover_from_checkpoint(run_dir, None)
    assert excinfo.value.category == "journal-read"
    assert isinstance(excinfo.value.__cause__, OSError)


def test_recover_from_checkpoint_normalizes_partial_tail_quarantine_raw_oserror(tmp_path, monkeypatch):
    """A raw ``OSError`` from ``recover_partial_tail`` is normalized.

    The quarantine step can raise a raw ``OSError`` (e.g. ``mkdir`` or temp
    write failure); the contract promises a bounded ``CheckpointError``.
    """
    workspace = _workspace(tmp_path)
    run_dir = _run_dir(tmp_path)
    run_json_obj = {"schema": "brigade.run.v1", "status": "planning", "task": "demo"}
    _activated_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    (run_dir / "run.json").unlink()
    journal = _journal_path(run_dir)
    partial = b'{"schema":"brigade.run_event.v1","event_type":"run.plan'
    journal.write_bytes(journal.read_bytes() + partial)

    def raise_oserror(_journal_path, _quarantine_dir):
        raise OSError(errno.EACCES, "quarantine mkdir denied")

    monkeypatch.setattr(run_journal, "recover_partial_tail", raise_oserror)

    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.recover_from_checkpoint(run_dir, None)
    assert excinfo.value.category == "partial-tail"
    assert isinstance(excinfo.value.__cause__, OSError)


def test_recover_from_checkpoint_normalizes_reread_oserror_after_quarantine(tmp_path, monkeypatch):
    """A raw ``OSError`` on the post-quarantine reread is normalized.

    The reread path (after a successful quarantine) can raise a raw ``OSError``;
    the contract promises a bounded ``CheckpointError``.
    """
    workspace = _workspace(tmp_path)
    run_dir = _run_dir(tmp_path)
    run_json_obj = {"schema": "brigade.run.v1", "status": "planning", "task": "demo"}
    _activated_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    (run_dir / "run.json").unlink()
    journal = _journal_path(run_dir)
    partial = b'{"schema":"brigade.run_event.v1","event_type":"run.plan'
    journal.write_bytes(journal.read_bytes() + partial)

    real_read = run_journal.read_journal_bounded
    call_count = {"n": 0}

    def fail_second_read(journal_path):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise OSError(errno.EIO, "reread io error")
        return real_read(journal_path)

    monkeypatch.setattr(run_journal, "read_journal_bounded", fail_second_read)

    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.recover_from_checkpoint(run_dir, None)
    assert excinfo.value.category == "journal-read"
    assert isinstance(excinfo.value.__cause__, OSError)


def test_recover_from_checkpoint_normalizes_restore_write_raw_oserror(tmp_path, monkeypatch):
    """A raw ``OSError`` from the restoration atomic write is normalized.

    ``localio.write_text_atomic`` can raise a raw ``OSError`` (mkstemp, fsync,
    or ``os.replace`` failure); the contract promises a bounded
    ``CheckpointError`` so a raw filesystem error never leaks out of
    ``recover_from_checkpoint``.
    """
    from brigade import localio

    workspace = _workspace(tmp_path)
    run_dir = _run_dir(tmp_path)
    run_json_obj = {"schema": "brigade.run.v1", "status": "planning", "task": "demo"}
    _activated_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    (run_dir / "run.json").unlink()

    def raise_oserror(_path, _data):
        raise OSError(errno.EACCES, "restore write denied")

    monkeypatch.setattr(localio, "write_text_atomic", raise_oserror)

    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.recover_from_checkpoint(run_dir, None)
    assert excinfo.value.category == "restore-write"
    assert isinstance(excinfo.value.__cause__, OSError)
    assert not (run_dir / "run.json").exists()


# -- Issue #568 slice 5 Task 5 second sendback: run.json input edges + path resolve --


def test_recover_from_checkpoint_preserves_non_utf8_run_json_and_replaces(tmp_path):
    workspace = _workspace(tmp_path)
    run_dir = _run_dir(tmp_path)
    run_json_obj = {"schema": "brigade.run.v1", "status": "planning", "task": "demo"}
    _activated_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    corrupt_bytes = b"\xff\xfe not utf-8"
    (run_dir / "run.json").write_bytes(corrupt_bytes)

    repaired = run_checkpoint.recover_from_checkpoint(run_dir, None)

    assert repaired == run_json_obj
    assert (run_dir / "run.json").read_bytes() == _writer_bytes(run_json_obj)
    preserved = list(run_dir.glob("run.json.corrupt-*"))
    assert len(preserved) == 1
    assert preserved[0].read_bytes() == corrupt_bytes


def test_recover_from_checkpoint_preserves_recursion_error_run_json_and_replaces(tmp_path):
    workspace = _workspace(tmp_path)
    run_dir = _run_dir(tmp_path)
    run_json_obj = {"schema": "brigade.run.v1", "status": "planning", "task": "demo"}
    _activated_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    depth = 20000
    deep_json = ("[" * depth) + ("]" * depth)
    (run_dir / "run.json").write_text(deep_json)

    repaired = run_checkpoint.recover_from_checkpoint(run_dir, None)

    assert repaired == run_json_obj
    assert (run_dir / "run.json").read_bytes() == _writer_bytes(run_json_obj)
    preserved = list(run_dir.glob("run.json.corrupt-*"))
    assert len(preserved) == 1
    assert preserved[0].read_text() == deep_json


def test_recover_from_checkpoint_normalizes_path_resolve_oserror(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    run_dir = _run_dir(tmp_path)
    run_json_obj = {"schema": "brigade.run.v1", "status": "planning", "task": "demo"}
    _activated_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    (run_dir / "run.json").unlink()

    real_resolve = Path.resolve

    def fail_resolve(self):
        if self == run_dir:
            raise OSError(errno.EACCES, "resolve denied")
        return real_resolve(self)

    monkeypatch.setattr(Path, "resolve", fail_resolve)

    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.recover_from_checkpoint(run_dir, None)
    assert excinfo.value.category == "path-resolve"
    assert isinstance(excinfo.value.__cause__, OSError)
    assert not (run_dir / "run.json").exists()


def test_recover_from_checkpoint_normalizes_path_resolve_runtime_error(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    run_dir = _run_dir(tmp_path)
    run_json_obj = {"schema": "brigade.run.v1", "status": "planning", "task": "demo"}
    _activated_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    (run_dir / "run.json").unlink()

    real_resolve = Path.resolve

    def fail_resolve(self):
        if self == run_dir:
            raise RuntimeError("symlink cycle")
        return real_resolve(self)

    monkeypatch.setattr(Path, "resolve", fail_resolve)

    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.recover_from_checkpoint(run_dir, None)
    assert excinfo.value.category == "path-resolve"
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert not (run_dir / "run.json").exists()


# -- Issue #568 slice 6 Task 3: stripped checkpoint base and body_kind ----------


_DURABLE_REQUEST_FIELDS = ("lifecycle_journal_requested", "run_journal_authority_requested")
_JOURNAL_METADATA_FIELDS = (
    "projector_version",
    "journal_present",
    "journal_last_sequence",
    "journal_last_event_digest",
)


def _base_stripped_payload(run_json_bytes: bytes, *, paired_event_type: str | None = None) -> dict:
    payload = _checkpoint_payload(run_json_bytes, paired_event_type=paired_event_type)
    payload["body_kind"] = "base-stripped"
    return payload


# -- _JOURNAL_METADATA_FIELDS constant ----------------------------------------


def test_journal_metadata_fields_constant_has_approved_values():
    assert run_checkpoint._JOURNAL_METADATA_FIELDS == frozenset(_JOURNAL_METADATA_FIELDS)


# -- _strip_journal_metadata_from_base helper --------------------------------


def test_strip_journal_metadata_removes_exactly_the_four_metadata_fields():
    base = {
        "schema": "brigade.run.v1",
        "status": "planning",
        "task": "demo",
        "lifecycle_journal_requested": True,
        "run_journal_authority_requested": True,
        "projector_version": 3,
        "journal_present": True,
        "journal_last_sequence": 7,
        "journal_last_event_digest": "a" * 64,
    }
    base_bytes = _writer_bytes(base)

    stripped_bytes = run_checkpoint._strip_journal_metadata_from_base(base_bytes)

    stripped = json.loads(stripped_bytes)
    for field in _JOURNAL_METADATA_FIELDS:
        assert field not in stripped
    # status and other fields retained
    assert stripped["status"] == "planning"
    assert stripped["task"] == "demo"
    assert stripped["schema"] == "brigade.run.v1"
    assert stripped["lifecycle_journal_requested"] is True
    assert stripped["run_journal_authority_requested"] is True
    assert len(stripped) == len(base) - 4


def test_strip_journal_metadata_canonicalizes_to_writer_form():
    # Non-canonical input bytes (compact, no trailing newline) must come back
    # in the aboyeur writer canonical encoding.
    base = {
        "schema": "brigade.run.v1",
        "status": "planning",
        "projector_version": 3,
        "journal_present": True,
        "journal_last_sequence": 1,
        "journal_last_event_digest": "b" * 64,
    }
    raw = (json.dumps(base, sort_keys=True) + "\n").encode("utf-8")  # compact, no indent
    assert raw != _writer_bytes(base)

    stripped_bytes = run_checkpoint._strip_journal_metadata_from_base(raw)

    expected_obj = {k: v for k, v in base.items() if k not in _JOURNAL_METADATA_FIELDS}
    assert stripped_bytes == _writer_bytes(expected_obj)


def test_strip_journal_metadata_is_idempotent():
    base = {
        "schema": "brigade.run.v1",
        "status": "planning",
        "task": "demo",
        "lifecycle_journal_requested": True,
        "run_journal_authority_requested": True,
        "projector_version": 3,
        "journal_present": True,
        "journal_last_sequence": 7,
        "journal_last_event_digest": "a" * 64,
    }
    base_bytes = _writer_bytes(base)

    once = run_checkpoint._strip_journal_metadata_from_base(base_bytes)
    twice = run_checkpoint._strip_journal_metadata_from_base(once)

    assert twice == once


def test_strip_journal_metadata_bounds_non_utf8_as_utf8():
    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint._strip_journal_metadata_from_base(b"\xff\xfe not utf-8")
    assert excinfo.value.category == "utf8"
    assert len(str(excinfo.value)) <= run_events.MAX_DIAGNOSTIC_LEN


def test_strip_journal_metadata_bounds_non_json_as_json_object():
    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint._strip_journal_metadata_from_base(b"not json at all")
    assert excinfo.value.category == "json-object"
    assert len(str(excinfo.value)) <= run_events.MAX_DIAGNOSTIC_LEN


def test_strip_journal_metadata_bounds_non_object_as_json_object():
    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint._strip_journal_metadata_from_base(b"[1, 2, 3]\n")
    assert excinfo.value.category == "json-object"
    assert len(str(excinfo.value)) <= run_events.MAX_DIAGNOSTIC_LEN


# -- payload validation: body_kind -------------------------------------------


def test_validate_checkpoint_missing_body_kind_validates_as_legacy_full(tmp_path):
    run_dir = _run_dir(tmp_path)
    run_json_bytes = _writer_bytes({"status": "started"})
    _place_checkpoint_file(run_dir, run_json_bytes)
    payload = _checkpoint_payload(run_json_bytes)  # no body_kind key
    assert run_checkpoint.validate_checkpoint(run_dir, payload) == run_json_bytes


def test_validate_checkpoint_unknown_body_kind_rejects_with_body_kind_category(tmp_path):
    run_dir = _run_dir(tmp_path)
    run_json_bytes = _writer_bytes({"status": "started"})
    _place_checkpoint_file(run_dir, run_json_bytes)
    payload = _checkpoint_payload(run_json_bytes)
    payload["body_kind"] = "legacy-full"
    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.validate_checkpoint(run_dir, payload)
    assert excinfo.value.category == "body-kind"
    assert len(str(excinfo.value)) <= run_events.MAX_DIAGNOSTIC_LEN


def test_validate_checkpoint_explicit_null_body_kind_rejects_with_body_kind_category(tmp_path):
    """An explicitly present body_kind with null must fail category body-kind.

    Missing body_kind is legacy-full, but any *present* value must equal
    base-stripped; an explicit null is a present value that is not
    base-stripped, so it fails closed with category body-kind (not silently
    treated as legacy-full).
    """
    run_dir = _run_dir(tmp_path)
    run_json_bytes = _writer_bytes({"status": "started"})
    _place_checkpoint_file(run_dir, run_json_bytes)
    payload = _checkpoint_payload(run_json_bytes)
    payload["body_kind"] = None  # explicitly present null
    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.validate_checkpoint(run_dir, payload)
    assert excinfo.value.category == "body-kind"
    assert len(str(excinfo.value)) <= run_events.MAX_DIAGNOSTIC_LEN


def test_validate_checkpoint_accepts_base_stripped_body_kind(tmp_path):
    run_dir = _run_dir(tmp_path)
    base = {
        "schema": "brigade.run.v1",
        "status": "planning",
        "lifecycle_journal_requested": True,
        "run_journal_authority_requested": True,
    }
    stripped_bytes = _writer_bytes(base)
    _place_checkpoint_file(run_dir, stripped_bytes)
    payload = _base_stripped_payload(stripped_bytes)
    assert run_checkpoint.validate_checkpoint(run_dir, payload) == stripped_bytes


def test_validate_checkpoint_base_stripped_requires_both_durable_request_fields(tmp_path):
    run_dir = _run_dir(tmp_path)
    # missing run_journal_authority_requested
    base = {
        "schema": "brigade.run.v1",
        "status": "planning",
        "lifecycle_journal_requested": True,
    }
    stripped_bytes = _writer_bytes(base)
    _place_checkpoint_file(run_dir, stripped_bytes)
    payload = _base_stripped_payload(stripped_bytes)
    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.validate_checkpoint(run_dir, payload)
    assert excinfo.value.category == "base-stripped-requests"
    assert len(str(excinfo.value)) <= run_events.MAX_DIAGNOSTIC_LEN


def test_validate_checkpoint_base_stripped_excludes_journal_metadata_fields(tmp_path):
    run_dir = _run_dir(tmp_path)
    # contains a metadata field that should have been stripped
    base = {
        "schema": "brigade.run.v1",
        "status": "planning",
        "lifecycle_journal_requested": True,
        "run_journal_authority_requested": True,
        "projector_version": 3,
    }
    stripped_bytes = _writer_bytes(base)
    _place_checkpoint_file(run_dir, stripped_bytes)
    payload = _base_stripped_payload(stripped_bytes)
    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.validate_checkpoint(run_dir, payload)
    assert excinfo.value.category == "base-stripped-requests"
    assert len(str(excinfo.value)) <= run_events.MAX_DIAGNOSTIC_LEN


def test_validate_checkpoint_base_stripped_rejects_false_lifecycle_journal_requested(tmp_path):
    """A present-but-False durable request field fails closed.

    The base-stripped durable-request rule requires the field to be True,
    not merely present; a False value fails category base-stripped-requests.
    """
    run_dir = _run_dir(tmp_path)
    base = {
        "schema": "brigade.run.v1",
        "status": "planning",
        "lifecycle_journal_requested": False,
        "run_journal_authority_requested": True,
    }
    stripped_bytes = _writer_bytes(base)
    _place_checkpoint_file(run_dir, stripped_bytes)
    payload = _base_stripped_payload(stripped_bytes)
    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.validate_checkpoint(run_dir, payload)
    assert excinfo.value.category == "base-stripped-requests"
    assert len(str(excinfo.value)) <= run_events.MAX_DIAGNOSTIC_LEN


def test_validate_checkpoint_base_stripped_rejects_false_run_journal_authority_requested(tmp_path):
    run_dir = _run_dir(tmp_path)
    base = {
        "schema": "brigade.run.v1",
        "status": "planning",
        "lifecycle_journal_requested": True,
        "run_journal_authority_requested": False,
    }
    stripped_bytes = _writer_bytes(base)
    _place_checkpoint_file(run_dir, stripped_bytes)
    payload = _base_stripped_payload(stripped_bytes)
    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.validate_checkpoint(run_dir, payload)
    assert excinfo.value.category == "base-stripped-requests"
    assert len(str(excinfo.value)) <= run_events.MAX_DIAGNOSTIC_LEN


def test_validate_checkpoint_base_stripped_rejects_wrong_type_lifecycle_journal_requested(tmp_path):
    """A wrong-typed durable request field (e.g. string) fails closed.

    The base-stripped durable-request rule requires the field to be True
    (bool), not merely present; a string "true" fails category
    base-stripped-requests.
    """
    run_dir = _run_dir(tmp_path)
    base = {
        "schema": "brigade.run.v1",
        "status": "planning",
        "lifecycle_journal_requested": "true",
        "run_journal_authority_requested": True,
    }
    stripped_bytes = _writer_bytes(base)
    _place_checkpoint_file(run_dir, stripped_bytes)
    payload = _base_stripped_payload(stripped_bytes)
    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.validate_checkpoint(run_dir, payload)
    assert excinfo.value.category == "base-stripped-requests"
    assert len(str(excinfo.value)) <= run_events.MAX_DIAGNOSTIC_LEN


def test_validate_checkpoint_base_stripped_rejects_wrong_type_run_journal_authority_requested(tmp_path):
    run_dir = _run_dir(tmp_path)
    base = {
        "schema": "brigade.run.v1",
        "status": "planning",
        "lifecycle_journal_requested": True,
        "run_journal_authority_requested": "true",
    }
    stripped_bytes = _writer_bytes(base)
    _place_checkpoint_file(run_dir, stripped_bytes)
    payload = _base_stripped_payload(stripped_bytes)
    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.validate_checkpoint(run_dir, payload)
    assert excinfo.value.category == "base-stripped-requests"
    assert len(str(excinfo.value)) <= run_events.MAX_DIAGNOSTIC_LEN


# -- idempotency keys --------------------------------------------------------


def test_checkpoint_idempotency_key_legacy_remains_checkpoint_sha_paired():
    sha = "a" * 64
    # paired present
    key = run_checkpoint._checkpoint_idempotency_key(sha, paired_event_type="run.created")
    assert key == f"checkpoint:{sha}:run.created"
    # paired absent -> "none"
    key_none = run_checkpoint._checkpoint_idempotency_key(sha, paired_event_type=None)
    assert key_none == f"checkpoint:{sha}:none"


def test_checkpoint_idempotency_key_base_stripped_is_checkpoint_base_stripped_sha_paired():
    sha = "a" * 64
    key = run_checkpoint._checkpoint_idempotency_key(sha, paired_event_type="run.created", body_kind="base-stripped")
    assert key == f"checkpoint:base-stripped:{sha}:run.created"
    # paired absent -> "none"
    key_none = run_checkpoint._checkpoint_idempotency_key(sha, paired_event_type=None, body_kind="base-stripped")
    assert key_none == f"checkpoint:base-stripped:{sha}:none"


def test_checkpoint_idempotency_key_base_stripped_is_bounded():
    sha = "a" * 64
    long_paired = "x" * 500
    key = run_checkpoint._checkpoint_idempotency_key(sha, paired_event_type=long_paired, body_kind="base-stripped")
    assert len(key) <= run_events.MAX_IDEMPOTENCY_KEY_LEN
    assert key.startswith(f"checkpoint:base-stripped:{sha}:")


# -- write_checkpoint with body_kind ------------------------------------------


def test_write_checkpoint_base_stripped_stores_stripped_body_and_payload_body_kind(enabled, tmp_path):
    workspace = _workspace(tmp_path)
    run_dir = _run_dir(tmp_path)
    _bootstrap_request(run_dir)
    with runguard.run_lock(workspace, run_dir=run_dir):
        run_lifecycle.prepare_lifecycle_journal(run_dir, workspace=workspace)

    base = {
        "schema": "brigade.run.v1",
        "status": "planning",
        "task": "demo",
        "lifecycle_journal_requested": True,
        "run_journal_authority_requested": True,
        "projector_version": 3,
        "journal_present": True,
        "journal_last_sequence": 7,
        "journal_last_event_digest": "c" * 64,
    }
    base_bytes = _writer_bytes(base)
    expected_stripped = _writer_bytes({k: v for k, v in base.items() if k not in _JOURNAL_METADATA_FIELDS})
    expected_sha = hashlib.sha256(expected_stripped).hexdigest()

    with runguard.run_lock(workspace, run_dir=run_dir):
        event = run_checkpoint.write_checkpoint(
            run_dir,
            base_bytes,
            workspace=workspace,
            paired_event_type="run.planning.started",
            body_kind="base-stripped",
        )

    assert event is not None
    # payload carries body_kind
    assert event.payload["body_kind"] == "base-stripped"
    # SHA is over the stripped bytes
    assert event.payload["sha256"] == expected_sha
    assert event.payload["byte_size"] == len(expected_stripped)
    assert event.payload["path"] == f"events/recovery-checkpoints/{expected_sha}.json"
    # idempotency key is the base-stripped form
    assert event.idempotency_key == f"checkpoint:base-stripped:{expected_sha}:run.planning.started"
    # the stored checkpoint file holds the stripped bytes
    stored = run_checkpoint.checkpoint_path(run_dir, expected_sha)
    assert stored.read_bytes() == expected_stripped
    # validate_checkpoint round-trips
    assert run_checkpoint.validate_checkpoint(run_dir, event.payload) == expected_stripped


def test_write_checkpoint_default_legacy_full_omits_body_kind(enabled, tmp_path):
    workspace = _workspace(tmp_path)
    run_dir = _run_dir(tmp_path)
    _bootstrap_request(run_dir)
    with runguard.run_lock(workspace, run_dir=run_dir):
        run_lifecycle.prepare_lifecycle_journal(run_dir, workspace=workspace)

    run_json_bytes = _writer_bytes({"schema": "brigade.run.v1", "status": "planning"})
    sha = hashlib.sha256(run_json_bytes).hexdigest()

    with runguard.run_lock(workspace, run_dir=run_dir):
        event = run_checkpoint.write_checkpoint(
            run_dir,
            run_json_bytes,
            workspace=workspace,
            paired_event_type="run.planning.started",
        )

    assert event is not None
    assert "body_kind" not in event.payload
    assert event.payload["sha256"] == sha
    assert event.idempotency_key == f"checkpoint:{sha}:run.planning.started"


def test_write_checkpoint_base_stripped_rejects_missing_request_field(enabled, tmp_path):
    workspace = _workspace(tmp_path)
    run_dir = _run_dir(tmp_path)
    # bootstrap writes only lifecycle_journal_requested; authority missing
    _bootstrap_request(run_dir)
    with runguard.run_lock(workspace, run_dir=run_dir):
        run_lifecycle.prepare_lifecycle_journal(run_dir, workspace=workspace)

    base = {
        "schema": "brigade.run.v1",
        "status": "planning",
        "lifecycle_journal_requested": True,
        # run_journal_authority_requested absent
        "projector_version": 3,
    }
    base_bytes = _writer_bytes(base)

    with runguard.run_lock(workspace, run_dir=run_dir), pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.write_checkpoint(
            run_dir,
            base_bytes,
            workspace=workspace,
            paired_event_type="run.planning.started",
            body_kind="base-stripped",
        )
    assert excinfo.value.category == "base-stripped-requests"
    assert len(str(excinfo.value)) <= run_events.MAX_DIAGNOSTIC_LEN


def test_write_checkpoint_base_stripped_rejects_false_request_field(enabled, tmp_path):
    """Write path: a present-but-False durable request field fails closed.

    The write-path base-stripped rule requires the field to be True, not
    merely present; a False value fails category base-stripped-requests
    before stripping, SHA, payload, publish, or append.
    """
    workspace = _workspace(tmp_path)
    run_dir = _run_dir(tmp_path)
    _bootstrap_request(run_dir)
    with runguard.run_lock(workspace, run_dir=run_dir):
        run_lifecycle.prepare_lifecycle_journal(run_dir, workspace=workspace)

    base = {
        "schema": "brigade.run.v1",
        "status": "planning",
        "lifecycle_journal_requested": True,
        "run_journal_authority_requested": False,
    }
    base_bytes = _writer_bytes(base)

    with runguard.run_lock(workspace, run_dir=run_dir), pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.write_checkpoint(
            run_dir,
            base_bytes,
            workspace=workspace,
            paired_event_type="run.planning.started",
            body_kind="base-stripped",
        )
    assert excinfo.value.category == "base-stripped-requests"
    assert len(str(excinfo.value)) <= run_events.MAX_DIAGNOSTIC_LEN
    # No checkpoint file published, no checkpoint event appended.
    assert not _checkpoint_dir(run_dir).exists()
    assert _checkpoint_events(run_dir) == []


def test_write_checkpoint_base_stripped_rejects_wrong_type_request_field(enabled, tmp_path):
    """Write path: a wrong-typed durable request field fails closed.

    A string "true" is not the bool True, so the write-path base-stripped
    rule rejects it with category base-stripped-requests before stripping,
    SHA, payload, publish, or append.
    """
    workspace = _workspace(tmp_path)
    run_dir = _run_dir(tmp_path)
    _bootstrap_request(run_dir)
    with runguard.run_lock(workspace, run_dir=run_dir):
        run_lifecycle.prepare_lifecycle_journal(run_dir, workspace=workspace)

    base = {
        "schema": "brigade.run.v1",
        "status": "planning",
        "lifecycle_journal_requested": "true",
        "run_journal_authority_requested": True,
    }
    base_bytes = _writer_bytes(base)

    with runguard.run_lock(workspace, run_dir=run_dir), pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.write_checkpoint(
            run_dir,
            base_bytes,
            workspace=workspace,
            paired_event_type="run.planning.started",
            body_kind="base-stripped",
        )
    assert excinfo.value.category == "base-stripped-requests"
    assert len(str(excinfo.value)) <= run_events.MAX_DIAGNOSTIC_LEN
    assert not _checkpoint_dir(run_dir).exists()
    assert _checkpoint_events(run_dir) == []


def test_write_checkpoint_base_stripped_rejects_non_house_canonical_bytes(enabled, tmp_path):
    """Write path: non-house-canonical incoming bytes fail category writer-bytes.

    A base-stripped write must reject incoming bytes that are valid JSON but
    not the aboyeur writer canonical encoding, with category writer-bytes,
    BEFORE stripping, SHA, payload, publish, or append. The standalone
    stripping helper may still canonicalize valid noncanonical JSON, but the
    write path must not. No checkpoint file or journal append on this failure.
    """
    workspace = _workspace(tmp_path)
    run_dir = _run_dir(tmp_path)
    _bootstrap_request(run_dir)
    with runguard.run_lock(workspace, run_dir=run_dir):
        run_lifecycle.prepare_lifecycle_journal(run_dir, workspace=workspace)

    base = {
        "schema": "brigade.run.v1",
        "status": "planning",
        "lifecycle_journal_requested": True,
        "run_journal_authority_requested": True,
    }
    # Compact encoding (no indent) is valid JSON but not writer canonical.
    non_canonical = (json.dumps(base, sort_keys=True) + "\n").encode("utf-8")
    assert non_canonical != _writer_bytes(base)

    journal_before = _journal_path(run_dir).read_bytes()

    with runguard.run_lock(workspace, run_dir=run_dir), pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.write_checkpoint(
            run_dir,
            non_canonical,
            workspace=workspace,
            paired_event_type="run.planning.started",
            body_kind="base-stripped",
        )
    assert excinfo.value.category == "writer-bytes"
    assert len(str(excinfo.value)) <= run_events.MAX_DIAGNOSTIC_LEN
    # No checkpoint file published.
    assert not _checkpoint_dir(run_dir).exists()
    # No journal append: bytes unchanged and no checkpoint events.
    assert _journal_path(run_dir).read_bytes() == journal_before
    assert _checkpoint_events(run_dir) == []


def test_write_checkpoint_base_stripped_strips_metadata_fields_present(enabled, tmp_path):
    """A base carrying the four journal metadata fields is stripped, not rejected."""
    workspace = _workspace(tmp_path)
    run_dir = _run_dir(tmp_path)
    _bootstrap_request(run_dir)
    with runguard.run_lock(workspace, run_dir=run_dir):
        run_lifecycle.prepare_lifecycle_journal(run_dir, workspace=workspace)

    base = {
        "schema": "brigade.run.v1",
        "status": "planning",
        "lifecycle_journal_requested": True,
        "run_journal_authority_requested": True,
        "projector_version": 3,
        "journal_present": True,
        "journal_last_sequence": 7,
        "journal_last_event_digest": "d" * 64,
    }
    base_bytes = _writer_bytes(base)
    expected_stripped = _writer_bytes({k: v for k, v in base.items() if k not in _JOURNAL_METADATA_FIELDS})
    expected_sha = hashlib.sha256(expected_stripped).hexdigest()

    with runguard.run_lock(workspace, run_dir=run_dir):
        event = run_checkpoint.write_checkpoint(
            run_dir,
            base_bytes,
            workspace=workspace,
            paired_event_type="run.planning.started",
            body_kind="base-stripped",
        )

    assert event is not None
    assert event.payload["sha256"] == expected_sha
    assert run_checkpoint.checkpoint_path(run_dir, expected_sha).read_bytes() == expected_stripped


def _bootstrap_authority_request(run_dir: Path, status: str = "planning") -> None:
    """Pre-lock bootstrap: write run.json with BOTH durable request fields, no journal.

    This is the enrolled-but-inactive state: the run has durably requested both
    the lifecycle journal and the journal authority, but the events directory
    and lifecycle.jsonl do not yet exist (prepare_lifecycle_journal has not
    been called). Used by the preactivation regression tests below.
    """
    localio.write_json(
        run_dir / "run.json",
        {
            "schema": "brigade.run.v1",
            "status": status,
            "lifecycle_journal_requested": True,
            "run_journal_authority_requested": True,
        },
    )


def test_write_checkpoint_base_stripped_preactivation_rejects_non_house_canonical_bytes(enabled, tmp_path):
    """Validation before activation: rejected candidate bytes must not create the journal.

    An enrolled-but-inactive run (both durable request fields True, no events
    directory) holds the real workspace run lock. A base-stripped write with
    non-house-canonical incoming bytes must fail closed with category
    writer-bytes BEFORE prepare_lifecycle_journal activates the journal, so
    the events directory, lifecycle.jsonl, and checkpoint files all remain
    absent. Mutating the journal on a rejected candidate violates the
    no-mutation precondition contract.
    """
    workspace = _workspace(tmp_path)
    run_dir = _run_dir(tmp_path)
    _bootstrap_authority_request(run_dir)
    # No prior prepare_lifecycle_journal: the run is enrolled but inactive.

    base = {
        "schema": "brigade.run.v1",
        "status": "planning",
        "lifecycle_journal_requested": True,
        "run_journal_authority_requested": True,
    }
    # Compact encoding (no indent) is valid JSON but not writer canonical.
    non_canonical = (json.dumps(base, sort_keys=True) + "\n").encode("utf-8")
    assert non_canonical != _writer_bytes(base)

    with runguard.run_lock(workspace, run_dir=run_dir), pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.write_checkpoint(
            run_dir,
            non_canonical,
            workspace=workspace,
            paired_event_type="run.planning.started",
            body_kind="base-stripped",
        )
    assert excinfo.value.category == "writer-bytes"
    assert len(str(excinfo.value)) <= run_events.MAX_DIAGNOSTIC_LEN
    # No mutation: events directory, lifecycle journal, and checkpoint files absent.
    assert not (run_dir / "events").exists()
    assert not _journal_path(run_dir).exists()
    assert not _checkpoint_dir(run_dir).exists()


def test_write_checkpoint_base_stripped_preactivation_rejects_false_request_field(enabled, tmp_path):
    """Validation before activation: a False durable request field must not create the journal.

    Enrolled-but-inactive run under the lock; the candidate base carries a
    present-but-False durable request field. The durable-request rule fails
    closed with category base-stripped-requests BEFORE
    prepare_lifecycle_journal, so no events directory, journal, or checkpoint
    files are created.
    """
    workspace = _workspace(tmp_path)
    run_dir = _run_dir(tmp_path)
    _bootstrap_authority_request(run_dir)

    base = {
        "schema": "brigade.run.v1",
        "status": "planning",
        "lifecycle_journal_requested": True,
        "run_journal_authority_requested": False,
    }
    base_bytes = _writer_bytes(base)

    with runguard.run_lock(workspace, run_dir=run_dir), pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.write_checkpoint(
            run_dir,
            base_bytes,
            workspace=workspace,
            paired_event_type="run.planning.started",
            body_kind="base-stripped",
        )
    assert excinfo.value.category == "base-stripped-requests"
    assert len(str(excinfo.value)) <= run_events.MAX_DIAGNOSTIC_LEN
    assert not (run_dir / "events").exists()
    assert not _journal_path(run_dir).exists()
    assert not _checkpoint_dir(run_dir).exists()


def test_write_checkpoint_base_stripped_preactivation_rejects_wrong_type_request_field(enabled, tmp_path):
    """Validation before activation: a wrong-typed durable request field must not create the journal.

    Enrolled-but-inactive run under the lock; the candidate base carries a
    string durable request field. The durable-request rule fails closed with
    category base-stripped-requests BEFORE prepare_lifecycle_journal, so no
    events directory, journal, or checkpoint files are created.
    """
    workspace = _workspace(tmp_path)
    run_dir = _run_dir(tmp_path)
    _bootstrap_authority_request(run_dir)

    base = {
        "schema": "brigade.run.v1",
        "status": "planning",
        "lifecycle_journal_requested": "true",
        "run_journal_authority_requested": True,
    }
    base_bytes = _writer_bytes(base)

    with runguard.run_lock(workspace, run_dir=run_dir), pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.write_checkpoint(
            run_dir,
            base_bytes,
            workspace=workspace,
            paired_event_type="run.planning.started",
            body_kind="base-stripped",
        )
    assert excinfo.value.category == "base-stripped-requests"
    assert len(str(excinfo.value)) <= run_events.MAX_DIAGNOSTIC_LEN
    assert not (run_dir / "events").exists()
    assert not _journal_path(run_dir).exists()
    assert not _checkpoint_dir(run_dir).exists()


def test_write_checkpoint_invalid_body_kind_preactivation_must_not_create_journal(enabled, tmp_path):
    """Validation before activation: an unknown body_kind must not create the journal.

    Enrolled-but-inactive run under the lock; an unknown body_kind value
    fails closed with category body-kind BEFORE prepare_lifecycle_journal
    activates the journal, so no events directory, journal, or checkpoint
    files are created. Pins the full validation surface ahead of activation.
    """
    workspace = _workspace(tmp_path)
    run_dir = _run_dir(tmp_path)
    _bootstrap_authority_request(run_dir)

    base_bytes = _writer_bytes(
        {
            "schema": "brigade.run.v1",
            "status": "planning",
            "lifecycle_journal_requested": True,
            "run_journal_authority_requested": True,
        }
    )

    with runguard.run_lock(workspace, run_dir=run_dir), pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.write_checkpoint(
            run_dir,
            base_bytes,
            workspace=workspace,
            paired_event_type="run.planning.started",
            body_kind="legacy-full",
        )
    assert excinfo.value.category == "body-kind"
    assert len(str(excinfo.value)) <= run_events.MAX_DIAGNOSTIC_LEN
    assert not (run_dir / "events").exists()
    assert not _journal_path(run_dir).exists()
    assert not _checkpoint_dir(run_dir).exists()


# -- Issue #568 slice 6: authority-aware base-stripped recovery ----------------


def _activated_authority_journal_with_checkpoint(
    workspace: Path,
    run_dir: Path,
    base_obj: dict,
    *,
    paired_event_type: str | None = "run.planning.started",
) -> run_journal.RunEvent:
    """Activate the journal and append one base-stripped checkpoint event.

    The run.json bootstrap carries only the lifecycle request; the authority
    request signal lives in the checkpointed base (both durable request
    fields True). The journal ends in the checkpoint event (the recoverable
    state). Returns the appended checkpoint RunEvent.
    """
    _bootstrap_request(run_dir)
    with runguard.run_lock(workspace, run_dir=run_dir):
        run_lifecycle.prepare_lifecycle_journal(run_dir, workspace=workspace)
        event = run_checkpoint.write_checkpoint(
            run_dir,
            _writer_bytes(base_obj),
            workspace=workspace,
            paired_event_type=paired_event_type,
            body_kind="base-stripped",
        )
    assert event is not None
    return event


def _authority_base(workspace: Path, **overrides) -> dict:
    base = {
        "schema": "brigade.run.v1",
        "status": "planning",
        "task": "demo",
        "cwd": str(workspace),
        "orchestrator": "chef",
        "lifecycle_journal_requested": True,
        "run_journal_authority_requested": True,
    }
    base.update(overrides)
    return base


def test_recover_from_checkpoint_base_stripped_authority_projects_snapshot(tmp_path):
    """Base-stripped + authority request restores the projected snapshot.

    The run.json bootstrap carries only the lifecycle request, so the
    authority request comes from the validated checkpoint base (the
    checkpoint request), not from run.json. With run.json missing, recovery
    parses the canonical stripped base and projects it over the verified
    journal events: preserved base fields survive, and the four journal
    metadata fields are the CURRENT projector values (a verbatim restore of
    the stripped body would have none of them).
    """
    from brigade import run_projector

    workspace = _workspace(tmp_path)
    run_dir = _run_dir(tmp_path)
    base = _authority_base(
        workspace,
        # Stale journal metadata the live base carried; stripped at write and
        # recomputed by the projector at recovery.
        projector_version=1,
        journal_present=False,
        journal_last_sequence=0,
        journal_last_event_digest=None,
    )
    _activated_authority_journal_with_checkpoint(workspace, run_dir, base)
    (run_dir / "run.json").unlink()

    repaired = run_checkpoint.recover_from_checkpoint(run_dir, None)

    events = _events(run_dir)
    stripped_base = {k: v for k, v in base.items() if k not in _JOURNAL_METADATA_FIELDS}
    expected = run_projector.project_run_snapshot(stripped_base, events, journal_present=True)
    assert repaired == expected.snapshot
    # Preserved base fields survive the projection.
    assert repaired["schema"] == "brigade.run.v1"
    assert repaired["task"] == "demo"
    assert repaired["cwd"] == str(workspace)
    assert repaired["orchestrator"] == "chef"
    assert repaired["lifecycle_journal_requested"] is True
    assert repaired["run_journal_authority_requested"] is True
    # Current projector metadata/status, not the stale base values.
    assert repaired["status"] == "planning"
    assert repaired["projector_version"] == run_projector.PROJECTOR_VERSION
    assert repaired["journal_present"] is True
    assert repaired["journal_last_sequence"] == events[-1].sequence
    assert repaired["journal_last_event_digest"] == events[-1].event_digest
    # The restored run.json is exactly the projector's canonical bytes.
    assert (run_dir / "run.json").read_bytes() == expected.to_bytes()


def test_recover_from_checkpoint_base_stripped_preserves_corrupt_run_json(tmp_path):
    """Corrupt run.json is preserved by rename, then replaced by the projection."""
    from brigade import run_projector

    workspace = _workspace(tmp_path)
    run_dir = _run_dir(tmp_path)
    base = _authority_base(workspace)
    _activated_authority_journal_with_checkpoint(workspace, run_dir, base)
    (run_dir / "run.json").write_text("not json")

    repaired = run_checkpoint.recover_from_checkpoint(run_dir, None)

    preserved = list(run_dir.glob("run.json.corrupt-*"))
    assert len(preserved) == 1
    assert preserved[0].read_text() == "not json"
    events = _events(run_dir)
    expected = run_projector.project_run_snapshot(base, events, journal_present=True)
    assert repaired == expected.snapshot
    assert (run_dir / "run.json").read_bytes() == expected.to_bytes()


def test_recover_from_checkpoint_base_stripped_leaves_parseable_run_json_untouched(tmp_path):
    """A parseable run.json is never overwritten; the projected mapping is returned."""
    workspace = _workspace(tmp_path)
    run_dir = _run_dir(tmp_path)
    base = _authority_base(workspace)
    _activated_authority_journal_with_checkpoint(workspace, run_dir, base)
    parseable = {"schema": "brigade.run.v1", "status": "planning", "task": "different"}
    (run_dir / "run.json").write_text(json.dumps(parseable, indent=2, sort_keys=True) + "\n")
    before = (run_dir / "run.json").read_bytes()

    repaired = run_checkpoint.recover_from_checkpoint(run_dir, parseable)

    # The returned receipt is the projected snapshot (journal metadata present),
    # but the parseable run.json on disk is untouched.
    assert repaired["journal_present"] is True
    assert repaired["task"] == "demo"
    assert (run_dir / "run.json").read_bytes() == before
    assert not list(run_dir.glob("run.json.corrupt-*"))


def test_recover_from_checkpoint_legacy_full_restores_checkpoint_bytes_verbatim(tmp_path):
    """A checkpoint without body_kind restores the exact stored bytes.

    The legacy-full body carries journal metadata fields whose values are
    stale relative to the journal; recovery must NOT re-derive them. The
    restore stays byte-identical to the validated checkpoint bytes.
    """
    workspace = _workspace(tmp_path)
    run_dir = _run_dir(tmp_path)
    run_json_obj = {
        "schema": "brigade.run.v1",
        "status": "planning",
        "task": "demo",
        "projector_version": 1,
        "journal_present": False,
        "journal_last_sequence": 99,
        "journal_last_event_digest": "d" * 64,
    }
    event = _activated_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    (run_dir / "run.json").unlink()
    checkpoint_bytes = run_checkpoint.checkpoint_path(run_dir, event.payload["sha256"]).read_bytes()

    repaired = run_checkpoint.recover_from_checkpoint(run_dir, None)

    assert repaired == run_json_obj
    assert (run_dir / "run.json").read_bytes() == checkpoint_bytes
    assert (run_dir / "run.json").read_bytes() == _writer_bytes(run_json_obj)


def test_recover_from_checkpoint_base_stripped_projection_failure_is_bounded(tmp_path, monkeypatch):
    """A projector failure surfaces as a bounded CheckpointError, no fallback.

    run_projector.ProjectionError is wrapped as a CheckpointError with
    category ``projection``, the fixed bounded diagnostic, and the original
    error preserved as the cause. Recovery never falls back to the earlier
    valid legacy-full checkpoint or to a verbatim restore of the stripped
    bytes: nothing is restored.
    """
    from brigade import run_projector

    workspace = _workspace(tmp_path)
    run_dir = _run_dir(tmp_path)
    # An earlier, valid legacy-full checkpoint exists; recovery must not fall
    # back to it when the latest base-stripped checkpoint fails to project.
    _bootstrap_request(run_dir)
    with runguard.run_lock(workspace, run_dir=run_dir):
        run_lifecycle.prepare_lifecycle_journal(run_dir, workspace=workspace)
        run_checkpoint.write_checkpoint(
            run_dir,
            _writer_bytes({"schema": "brigade.run.v1", "status": "started"}),
            workspace=workspace,
            paired_event_type="run.created",
        )
        event = run_checkpoint.write_checkpoint(
            run_dir,
            _writer_bytes(_authority_base(workspace)),
            workspace=workspace,
            paired_event_type="run.planning.started",
            body_kind="base-stripped",
        )
    assert event is not None
    (run_dir / "run.json").unlink()

    def boom(base_snapshot, events, *, journal_present):
        raise run_projector.ProjectionError("raw projector detail " + "x" * 500)

    monkeypatch.setattr(run_projector, "project_run_snapshot", boom)

    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.recover_from_checkpoint(run_dir, None)
    assert excinfo.value.category == "projection"
    assert excinfo.value.diagnostic == "projection failed"
    assert len(excinfo.value.diagnostic) <= run_events.MAX_DIAGNOSTIC_LEN
    assert isinstance(excinfo.value.__cause__, run_projector.ProjectionError)
    # No restore, no fallback to the earlier legacy-full checkpoint.
    assert not (run_dir / "run.json").exists()


# -- Issue #568 slice 7 assignment 6: localio.write_text_atomic SIGKILL crash window --


_CHILD_WRITE_ATOMIC_CRASH_SCRIPT = """
import sys
from pathlib import Path

from brigade import localio

target = Path(sys.argv[1])
ready = Path(sys.argv[2])
new_text = Path(sys.argv[3]).read_text(encoding="utf-8")

ready.write_text("ready", encoding="utf-8")
while True:
    localio.write_text_atomic(target, new_text)
"""


def _write_text_atomic_temp_paths(run_dir: Path, target_name: str = "run.json") -> list[Path]:
    prefix = f".{target_name}."
    return sorted(
        path
        for path in run_dir.iterdir()
        if path.is_file() and path.name.startswith(prefix) and path.name.endswith(".tmp")
    )


@pytest.mark.skipif(os.name != "posix", reason="POSIX SIGKILL crash window requires os.kill")
def test_write_text_atomic_sigkill_during_temp_window_preserves_one_valid_payload(tmp_path):
    """A real SIGKILL during the temp-file window leaves one valid run.json.

    Exercises ``localio.write_text_atomic`` (write -> fsync -> replace) in a child
    subprocess without monkeypatching ``os.write``, ``os.fsync``, ``os.replace``,
    ``tempfile``, or ``localio``. A successful attempt must leave the real
    ``.run.json.*.tmp`` sibling after the killed child is reaped. That proves
    ``SIGKILL`` landed before ``os.replace`` or exception cleanup could remove
    the temp file. Fresh-directory retries keep the scheduling stress bounded.
    """
    old_obj = {
        "schema": "brigade.run.v1",
        "status": "planning",
        "task": "crash-window",
        "run_id": RUN_ID,
    }
    old_bytes = _writer_bytes(old_obj)

    new_obj = {
        "schema": "brigade.run.v1",
        "status": "running",
        "task": "crash-window",
        "run_id": RUN_ID,
        "padding": "x" * (2 * 1024 * 1024),
    }
    new_bytes = _writer_bytes(new_obj)
    new_text_path = tmp_path / "new_payload.json"
    new_text_path.write_bytes(new_bytes)

    repo_root = Path(__file__).resolve().parents[1]
    child_env = os.environ.copy()
    child_env["PYTHONPATH"] = str(repo_root / "src")

    successful_leftovers: list[str] = []
    attempt_diagnostics: list[str] = []
    attempt_run_dirs: list[Path] = []
    for attempt in range(1, 6):
        run_dir = _run_dir(tmp_path / f"attempt-{attempt}")
        attempt_run_dirs.append(run_dir)
        target = run_dir / "run.json"
        target.write_bytes(old_bytes)
        ready_path = run_dir / "child.ready"
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _CHILD_WRITE_ATOMIC_CRASH_SCRIPT,
                str(target),
                str(ready_path),
                str(new_text_path),
            ],
            env=child_env,
        )
        try:
            ready_deadline = time.monotonic() + 2
            while not ready_path.is_file():
                if proc.poll() is not None:
                    pytest.fail(f"child exited before signaling ready: returncode={proc.returncode}")
                if time.monotonic() >= ready_deadline:
                    pytest.fail("timed out waiting for child ready signal")
                time.sleep(0.001)

            temp_observed = False
            kill_deadline = time.monotonic() + 2
            while time.monotonic() < kill_deadline:
                if proc.poll() is not None:
                    break
                if _write_text_atomic_temp_paths(run_dir):
                    temp_observed = True
                    os.kill(proc.pid, signal.SIGKILL)
                    break
                time.sleep(0.001)
            if not temp_observed and proc.poll() is None:
                os.kill(proc.pid, signal.SIGKILL)

            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
                pytest.fail("child did not exit after SIGKILL")

            assert proc.returncode == -signal.SIGKILL, (
                f"expected SIGKILL exit (-{signal.SIGKILL}), got {proc.returncode}"
            )

            assert target.is_file(), "authoritative run.json disappeared after crash"
            actual = target.read_bytes()
            assert actual in (old_bytes, new_bytes), (
                "run.json bytes are neither the complete old payload nor the complete new payload"
            )
            parsed = json.loads(actual.decode("utf-8"))
            assert parsed["schema"] == "brigade.run.v1"
            assert parsed["task"] == "crash-window"
            assert parsed["run_id"] == RUN_ID
            assert parsed["status"] in ("planning", "running")

            leftovers = _write_text_atomic_temp_paths(run_dir)
            attempt_diagnostics.append(
                f"attempt={attempt} observed={temp_observed} leftovers={[path.name for path in leftovers]!r}"
            )
            if leftovers:
                successful_leftovers = [path.name for path in leftovers]
                break
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=2)
            for leftover in _write_text_atomic_temp_paths(run_dir):
                leftover.unlink()

    assert successful_leftovers, (
        f"SIGKILL never left a crash-window temp file after child reaping; attempts={attempt_diagnostics!r}"
    )
    for run_dir in attempt_run_dirs:
        assert _write_text_atomic_temp_paths(run_dir) == []


# -- Issue #636: checkpoint export privacy -----------------------------------


def test_strip_checkpoint_bodies_for_export_replaces_private_body(tmp_path):
    run_dir = _run_dir(tmp_path)
    private_task = "SECRET_TASK_PROMPT_do_not_export"
    private_error = "SECRET_ERROR_TRACE_do_not_export"
    body = _writer_bytes(
        {
            "schema": "brigade.run.v1",
            "status": "failed",
            "task": private_task,
            "error": private_error,
        }
    )
    placed = _place_checkpoint_file(run_dir, body)
    assert private_task in placed.read_text(encoding="utf-8")

    export_copy = tmp_path / "export-copy"
    shutil.copytree(run_dir, export_copy)

    refs = run_checkpoint.strip_checkpoint_bodies_for_export(export_copy)
    assert len(refs) == 1
    assert run_checkpoint.is_checkpoint_artifact_reference(refs[0])
    assert refs[0]["privacy_class"] == "private"
    assert refs[0]["sha256"] == hashlib.sha256(body).hexdigest()
    assert refs[0]["byte_size"] == len(body)

    exported = (export_copy / "events" / "recovery-checkpoints" / placed.name).read_text(encoding="utf-8")
    assert private_task not in exported
    assert private_error not in exported
    assert '"task"' not in exported
    assert json.loads(exported) == refs[0]

    # Local recovery source is unchanged.
    assert placed.read_bytes() == body
    run_checkpoint.assert_export_tree_has_no_checkpoint_bodies(export_copy)


def test_strip_checkpoint_bodies_for_export_removes_crashed_temp(tmp_path):
    """#646/#654: a crashed ``.checkpoint.*.tmp`` holding private bytes must be
    stripped on export and the assert must not pass while one remains."""
    run_dir = _run_dir(tmp_path)
    private_task = "SECRET_TASK_PROMPT_do_not_export"
    body = _writer_bytes({"schema": "brigade.run.v1", "status": "planning", "task": private_task})
    placed = _place_checkpoint_file(run_dir, body)
    cp_dir = placed.parent
    # A crash mid-write leaves an arbitrary, not-necessarily-JSON prefix.
    crashed = cp_dir / ".checkpoint.abc123.tmp"
    crashed.write_bytes(b'{"schema": "brigade.run.v1", "task": "SECRET_CRASHED_' + b"\xff\xfe")
    os.chmod(crashed, PRIVATE_FILE_MODE)

    export_copy = tmp_path / "export-copy"
    shutil.copytree(run_dir, export_copy)
    export_temp = export_copy / "events" / run_checkpoint.CHECKPOINT_DIR_NAME / crashed.name

    # Before stripping, the temp alone is a refusal.
    with pytest.raises(run_checkpoint.CheckpointError, match="privacy_class=private") as exc_info:
        run_checkpoint.assert_export_tree_has_no_checkpoint_bodies(export_copy)
    assert exc_info.value.category == "export-privacy"

    refs = run_checkpoint.strip_checkpoint_bodies_for_export(export_copy)
    assert len(refs) == 1
    assert not export_temp.exists()
    exported_tree = b"".join(p.read_bytes() for p in export_copy.rglob("*") if p.is_file())
    assert b"SECRET_CRASHED_" not in exported_tree
    assert private_task.encode() not in exported_tree
    run_checkpoint.assert_export_tree_has_no_checkpoint_bodies(export_copy)

    # Local recovery source keeps its temp and body untouched.
    assert crashed.exists()
    assert placed.read_bytes() == body


def test_assert_export_tree_refuses_reference_shaped_crashed_temp(tmp_path):
    """A temp whose bytes happen to parse as an artifact reference is still a
    leak: the temp pattern itself is refused without reading the file."""
    run_dir = _run_dir(tmp_path)
    cp_dir = run_dir / "events" / run_checkpoint.CHECKPOINT_DIR_NAME
    cp_dir.mkdir(parents=True)
    reference = run_checkpoint.checkpoint_artifact_reference(sha256="a" * 64, byte_size=1)
    (cp_dir / ".checkpoint.deadbeef.tmp").write_text(json.dumps(reference), encoding="utf-8")

    with pytest.raises(run_checkpoint.CheckpointError, match="crashed checkpoint temp") as exc_info:
        run_checkpoint.assert_export_tree_has_no_checkpoint_bodies(run_dir)
    assert exc_info.value.category == "export-privacy"


def test_strip_checkpoint_bodies_for_export_refuses_symlinked_temp(tmp_path):
    run_dir = _run_dir(tmp_path)
    cp_dir = run_dir / "events" / run_checkpoint.CHECKPOINT_DIR_NAME
    cp_dir.mkdir(parents=True)
    target = tmp_path / "outside-secret"
    target.write_text("SECRET", encoding="utf-8")
    (cp_dir / ".checkpoint.link.tmp").symlink_to(target)

    with pytest.raises(run_checkpoint.CheckpointError, match="not a regular file") as exc_info:
        run_checkpoint.strip_checkpoint_bodies_for_export(run_dir)
    assert exc_info.value.category == "export-privacy"
    assert target.exists()


def test_assert_export_tree_refuses_checkpoint_body(tmp_path):
    run_dir = _run_dir(tmp_path)
    body = _writer_bytes({"schema": "brigade.run.v1", "status": "planning", "task": "keep-private"})
    _place_checkpoint_file(run_dir, body)

    with pytest.raises(run_checkpoint.CheckpointError, match="privacy_class=private") as exc_info:
        run_checkpoint.assert_export_tree_has_no_checkpoint_bodies(run_dir)
    assert exc_info.value.category == "export-privacy"


def test_refuse_checkpoint_body_export_names_privacy_class():
    with pytest.raises(run_checkpoint.CheckpointError, match="privacy_class=private") as exc_info:
        run_checkpoint.refuse_checkpoint_body_export()
    assert exc_info.value.category == "export-privacy"


def test_local_recovery_round_trips_after_export_strip_of_copy(tmp_path):
    """Export stripping a copy must not change local recovery semantics."""
    workspace = _workspace(tmp_path)
    run_dir = _run_dir(tmp_path)
    run_json_obj = {
        "schema": "brigade.run.v1",
        "status": "planning",
        "task": "SECRET_TASK_for_local_recovery_only",
    }
    _activated_journal_with_checkpoint(workspace, run_dir, run_json_obj)

    export_copy = tmp_path / "export-copy"
    shutil.copytree(run_dir, export_copy)
    run_checkpoint.strip_checkpoint_bodies_for_export(export_copy)
    run_checkpoint.assert_export_tree_has_no_checkpoint_bodies(export_copy)

    (run_dir / "run.json").unlink()
    repaired = run_checkpoint.recover_from_checkpoint(run_dir, None)
    assert repaired == run_json_obj
    assert (run_dir / "run.json").read_bytes() == _writer_bytes(run_json_obj)
