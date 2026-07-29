"""Tests for brigade.run_checkpoint: checkpoint event type, validation, crash-safe publish.

Issue #568 slice 5, Task 1 (RED-first). Standard library only.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import tempfile
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
    # legitimate temp unlink. The outer finally also unlinks (missing_ok
    # no-op) as a safety net, so look for any unlink followed by a later
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
    os.chmod(final, 0o600)

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
    os.chmod(final, 0o600)

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
    assert stat.S_IMODE(final.stat().st_mode) == 0o600
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
    os.chmod(real, 0o600)
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
    os.chmod(final, 0o600)

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
    os.chmod(final, 0o600)
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


def test_validate_checkpoint_translates_recursion_error_on_writer_canonical_dumps(tmp_path):
    """RecursionError from canonical json.dumps re-encoding -> writer-bytes."""
    run_dir = _run_dir(tmp_path)
    # A deeply nested JSON *object* (top-level dict): the C JSON decoder
    # handles this depth (json.loads succeeds and returns a dict, so the
    # isinstance(obj, dict) gate passes), but the recursive canonical
    # re-encoder (json.dumps with indent=2, sort_keys=True) cannot, raising
    # RecursionError from _writer_canonical_bytes.
    depth = 3000
    deep_json = (b'{"a":' * depth) + b"1" + (b"}" * depth)
    _final, payload = _place_deep_checkpoint(run_dir, deep_json)

    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.validate_checkpoint(run_dir, payload)
    err = excinfo.value
    assert err.category == "writer-bytes", f"expected writer-bytes, got {err.category}"
    assert isinstance(err, run_checkpoint.CheckpointError)
    diagnostic = str(err)
    assert diagnostic == err.diagnostic
    assert len(diagnostic) <= run_events.MAX_DIAGNOSTIC_LEN
