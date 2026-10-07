"""Coordinated process qualification, with native Windows or POSIX byte-lock emulation.

Termination observes visible writes and OS lock release, never power-loss durability.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from brigade import run_events, run_journal, run_projector

BACKEND = "native-windows" if os.name == "nt" else "posix-msvcrt-emulation"

# Written only into the short, isolated temporary root. No inherited provider
# environment, pipe polling, platform-specific kill calls, or production seam.
_CHILD = r"""
import errno
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from brigade import fleet_client, run_journal

options = json.loads(sys.argv[1])
root = Path.cwd()
journal = root / "events" / "lifecycle.jsonl"
name = options["name"]
mode = options.get("mode", "append")

# Reporting is outside this storage qualification. Do not discover host fleet
# identities, credentials, or repository metadata from a fixture's ancestors.
fleet_client.report_journal_event = lambda *args, **kwargs: False
if os.name == "posix":
    import fcntl

    # Explicit emulation of msvcrt using real OS byte-region locks. This does
    # not qualify NT sharing or Windows handles. Native Windows keeps msvcrt.
    def locking(fd, operation, length):
        flags = fcntl.LOCK_UN if operation == 0 else fcntl.LOCK_EX
        if operation == 2:
            flags |= fcntl.LOCK_NB
        fcntl.lockf(fd, flags, length, os.lseek(fd, 0, os.SEEK_CUR), os.SEEK_SET)

    run_journal.msvcrt = SimpleNamespace(LK_LOCK=1, LK_UNLCK=0, LK_NBLCK=2, locking=locking)
run_journal.fcntl = None
assert run_journal.msvcrt is not None

def mark(suffix, value):
    temporary = root / (name + "." + suffix + ".tmp")
    temporary.write_text(json.dumps(value), encoding="utf-8")
    temporary.replace(root / (name + "." + suffix))

def wait_for(filename):
    deadline = time.monotonic() + 30
    while not (root / filename).exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("child barrier: " + filename)
        time.sleep(0.01)

if options.get("pause_tail") or options.get("contender"):
    real_read_tail = run_journal._read_tail_state
    def tracked_read_tail(path):
        if options.get("contender"):
            mark("tail-entered", "tail-read-entered")
            assert (root / options["tail_release"]).exists(), "contender read tail before writer release"
            assert (root / (name + ".lock-acquired")).exists(), "contender read tail before lock acquisition"
        state = real_read_tail(path)
        sequence, digest, index, partial, journal_bytes = state
        mark("tail", {
            "sequence": sequence, "digest": digest, "keys": sorted(index),
            "partial": partial.hex() if partial is not None else None, "bytes": journal_bytes,
        })
        if options.get("pause_tail"):
            # Pause after the real transaction read, not an external head read.
            wait_for(options["tail_release"])
        return state
    run_journal._read_tail_state = tracked_read_tail

if options.get("contender"):
    # Retain the real native msvcrt callable (or explicit POSIX emulation).
    real_locking = run_journal.msvcrt.locking
    def tracked_locking(fd, operation, length):
        acquiring = operation == run_journal.msvcrt.LK_LOCK
        if acquiring:
            mark("lock-attempt", "LK_LOCK")
        result = real_locking(fd, operation, length)
        if acquiring:
            mark("lock-acquired", "lock-acquired")
        return result
    run_journal.msvcrt.locking = tracked_locking

identity = journal.stat()
def is_journal(fd):
    info = os.fstat(fd)
    return (info.st_dev, info.st_ino) == (identity.st_dev, identity.st_ino)

real_write, real_fsync = os.write, os.fsync
writes = []
syncs = []
def tracked_write(fd, data):
    target = is_journal(fd)
    physical = data[:len(data) // 2] if target and mode == "kill-partial" else data
    written = real_write(fd, physical)
    assert written == len(physical), "unexpected OS short write"
    if target:
        writes.append(physical.hex())
        if mode == "kill-partial":
            mark("boundary", {"at": "partial-write", "line": data.hex(), "written": physical.hex()})
            wait_for(name + ".never-release")
    return written

def tracked_fsync(fd):
    target = is_journal(fd)
    if target and mode == "kill-complete":
        assert len(writes) == 1
        mark("boundary", {"at": "before-file-fsync", "line": writes[0], "written": writes[0]})
        wait_for(name + ".never-release")
    real_fsync(fd)
    syncs.append("file" if target else "directory")

os.write, os.fsync = tracked_write, tracked_fsync

def append(previous):
    return run_journal.append_event(
        journal,
        run_id="20260727-153045-a1b2c3d4",
        event_type=options.get("event_type", "run.planning.started"),
        payload=options.get("payload", {"detail": "alpha"}),
        idempotency_key=options.get("key", "plan"),
        expected_previous_sequence=previous,
        recorded_at=options.get("recorded_at", "2026-07-27T15:30:46.000000Z"),
    )

def require_held_lock():
    # A nonblocking OS probe makes the held-lock observation deterministic.
    # Open errors are not accepted as proof of byte-lock exclusion.
    fd = run_journal._open_journal_lock(journal.with_name(journal.name + ".lock"))
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            run_journal.msvcrt.locking(fd, run_journal.msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            assert exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK)
            mark("blocked", "byte-lock-held")
        else:
            run_journal.msvcrt.locking(fd, run_journal.msvcrt.LK_UNLCK, 1)
            raise AssertionError("writer did not hold its OS byte lock")
    finally:
        os.close(fd)

result = {}
if mode in ("replay-after-kill", "recover-after-kill"):
    require_held_lock()
    with run_journal.journal_mutation(journal):
        mark("entered", "lock-acquired")
        if mode == "recover-after-kill":
            before = journal.read_bytes()
            try:
                append(1)
            except run_journal.PartialTailError:
                assert journal.read_bytes() == before
                result["refused_bytes"] = before.hex()
            else:
                raise AssertionError("append accepted a partial tail")
            recovery = run_journal.recover_partial_tail(journal, root / "quarantine")
            assert recovery.quarantine_path is not None
            result.update(
                partial=recovery.partial_bytes.hex(),
                quarantine=recovery.quarantine_path.name,
                recovered_bytes=journal.read_bytes().hex(),
            )
        event = append(1)
        result["event"] = event.to_dict()
        if mode == "recover-after-kill":
            accepted = journal.read_bytes()
            result["replay"] = append(1).to_dict()
            assert journal.read_bytes() == accepted
else:
    report = run_journal.read_journal_bounded(journal)
    assert report.chain_errors == [] and report.partial_tail is None
    head = report.events[-1] if report.events else None
    previous = head.sequence if head else 0
    mark("head", {"sequence": previous, "digest": head.event_digest if head else None})
    if "gate" in options:
        wait_for(options["gate"])
    if options.get("contender"):
        require_held_lock()
    try:
        result["event"] = append(options.get("previous", previous)).to_dict()
    except run_journal.IdempotencyConflict as exc:
        result.update(
            error=type(exc).__name__, existing_event_id=exc.existing_event_id,
            request_digest=exc.request_digest, existing_request_digest=exc.existing_request_digest,
        )
    except run_journal.StaleSequenceError as exc:
        result["error"] = type(exc).__name__
result.update(writes=writes, syncs=syncs)
print(json.dumps(result), flush=True)
"""


@pytest.fixture
def process_case():
    # conftest's isolated HOME owns the short root, as in the existing Windows
    # journal process tests. All children and scripts are reaped before cleanup.
    with tempfile.TemporaryDirectory(prefix="jp-", dir=Path.home()) as directory:
        root = Path(directory)
        path = root / "events" / "lifecycle.jsonl"
        run_journal.ensure_journal(path)
        (root / "child.py").write_text(_CHILD, encoding="utf-8")
        children = []
        try:
            yield root, path, children
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
            for child in children:
                child.communicate(timeout=5)


def _spawn(case, name, **options):
    root, _path, children = case
    environment = {
        "HOME": str(root),
        "USERPROFILE": str(root),
        "TMP": str(root),
        "TEMP": str(root),
        "TMPDIR": str(root),
        "PATH": os.defpath,
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONIOENCODING": "utf-8",
        "BRIGADE_HOME": str(root / "operator" / ".brigade"),
        "BRIGADE_USER_DIR": str(root / "operator" / ".brigade"),
        "BRIGADE_NO_UPDATE_CHECK": "1",
    }
    if os.name == "nt":
        for key in ("SYSTEMROOT", "WINDIR"):
            if key in os.environ:
                environment[key] = os.environ[key]
    child = subprocess.Popen(
        [sys.executable, str(root / "child.py"), json.dumps({"name": name, **options})],
        cwd=root,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    children.append(child)
    return child


def _finish(child):
    stdout, stderr = child.communicate(timeout=20)
    assert child.returncode == 0, f"child exited {child.returncode}\nstdout:\n{stdout}\nstderr:\n{stderr}"
    return json.loads(stdout)


def _wait(case, *names):
    root, _path, children = case
    deadline = time.monotonic() + 20
    while not all((root / name).exists() for name in names):
        for child in children:
            if child.poll() not in (None, 0):
                stdout, stderr = child.communicate(timeout=5)
                pytest.fail(f"child exited {child.returncode}\nstdout:\n{stdout}\nstderr:\n{stderr}")
        if time.monotonic() >= deadline:
            pytest.fail(f"parent barrier timed out: {names}")
        time.sleep(0.01)


def _read(case, name):
    deadline = time.monotonic() + 1
    while True:
        try:
            return json.loads((case[0] / name).read_text(encoding="utf-8"))
        except PermissionError as exc:
            if os.name != "nt" or exc.errno != 13 or time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def _history(path, count):
    report = run_journal.read_journal_bounded(path)
    assert report.chain_errors == [] and report.partial_tail is None
    assert len(report.events) == count
    previous = None
    for sequence, event in enumerate(report.events, start=1):
        assert event.sequence == sequence and event.previous_digest == previous
        assert run_events.validate_event(event.to_dict()) == []
        previous = event.event_digest
    assert path.read_bytes() == b"".join(run_events.canonical_bytes(event.to_dict()) + b"\n" for event in report.events)
    return report.events


@pytest.mark.parametrize("conflicting", [False, True], ids=[f"identical-{BACKEND}", f"conflicting-{BACKEND}"])
def test_same_key_process_race_replays_or_conflicts_without_second_record(process_case, conflicting):
    root, path, _children = process_case
    children = [
        _spawn(
            process_case,
            "alpha",
            key="shared",
            payload={"detail": "alpha"},
            gate="alpha.go",
            pause_tail=True,
            tail_release="alpha.tail-release",
        ),
        _spawn(
            process_case,
            "beta",
            key="shared",
            payload={"detail": "beta" if conflicting else "alpha"},
            gate="beta.go",
            contender=True,
            tail_release="alpha.tail-release",
        ),
    ]
    _wait(process_case, "alpha.head", "beta.head")
    assert _read(process_case, "alpha.head") == _read(process_case, "beta.head") == {"sequence": 0, "digest": None}
    root.joinpath("alpha.go").touch()
    _wait(process_case, "alpha.tail")
    assert _read(process_case, "alpha.tail") == {
        "sequence": 0,
        "digest": None,
        "keys": [],
        "partial": None,
        "bytes": 0,
    }
    assert children[0].poll() is None and path.read_bytes() == b""
    # Only alpha can hold the lock here. Reading its tail before locking must
    # fail beta's OS exclusion probe regardless of subsequent scheduling.
    root.joinpath("beta.go").touch()
    _wait(process_case, "beta.blocked", "beta.lock-attempt")
    assert _read(process_case, "beta.blocked") == "byte-lock-held"
    assert _read(process_case, "beta.lock-attempt") == "LK_LOCK"
    assert all(child.poll() is None for child in children)
    assert not root.joinpath("beta.lock-acquired").exists()
    assert not root.joinpath("beta.tail-entered").exists()
    assert not root.joinpath("beta.tail").exists()
    assert path.read_bytes() == b""
    root.joinpath("alpha.tail-release").touch()
    results = [_finish(child) for child in children]
    accepted = _history(path, 1)[0]
    assert _read(process_case, "beta.lock-acquired") == "lock-acquired"
    assert _read(process_case, "beta.tail-entered") == "tail-read-entered"
    assert _read(process_case, "beta.tail") == {
        "sequence": 1,
        "digest": accepted.event_digest,
        "keys": ["shared"],
        "partial": None,
        "bytes": len(path.read_bytes()),
    }
    assert sum(len(result["writes"]) for result in results) == 1
    assert len(results[0]["writes"]) == 1 and results[1]["writes"] == []
    events = [result["event"] for result in results if "event" in result]
    assert events == [accepted.to_dict()] * (1 if conflicting else 2)
    conflicts = [result for result in results if "error" in result]
    if conflicting:
        assert len(conflicts) == 1
        conflict = conflicts[0]
        assert conflict["error"] == "IdempotencyConflict" and conflict["writes"] == []
        assert conflict["existing_event_id"] == accepted.event_id
        assert conflict["existing_request_digest"] == accepted.request_digest
        assert conflict["request_digest"] != accepted.request_digest
    else:
        assert conflicts == []
    before = path.read_bytes()
    replay = _finish(_spawn(process_case, "replay", key="shared", payload=accepted.payload, previous=0))
    assert replay["event"] == accepted.to_dict() and replay["writes"] == []
    assert path.read_bytes() == before


def test_distinct_key_stale_head_process_loser_cannot_mutate_history(process_case):
    root, path, _children = process_case
    winner = _spawn(process_case, "winner", key="winner", gate="winner.go")
    loser = _spawn(process_case, "loser", key="loser", payload={"detail": "loser"}, gate="loser.go")
    _wait(process_case, "winner.head", "loser.head")
    assert _read(process_case, "winner.head") == _read(process_case, "loser.head") == {"sequence": 0, "digest": None}
    # One-shot scheduling preserves the loser's independent stale read while
    # allowing an exact byte comparison across its rejected append.
    root.joinpath("winner.go").touch()
    result = _finish(winner)
    before = path.read_bytes()
    root.joinpath("loser.go").touch()
    rejected = _finish(loser)
    assert "error" not in result and len(result["writes"]) == 1
    assert rejected == {"error": "StaleSequenceError", "writes": [], "syncs": []}
    assert path.read_bytes() == before
    accepted = _history(path, 1)[0]
    assert accepted.to_dict() == result["event"] and accepted.idempotency_key == "winner"


@pytest.mark.parametrize("boundary", ["complete", "partial"], ids=[f"complete-{BACKEND}", f"partial-{BACKEND}"])
def test_killed_writer_releases_os_lock_for_replay_or_partial_recovery(process_case, boundary):
    root, path, _children = process_case
    seed = _finish(_spawn(process_case, "seed", key="create", event_type="run.created", payload={"status": "started"}))[
        "event"
    ]
    complete_prefix = path.read_bytes()
    base = {"status": "started", "task": "fake-task"}
    seed_projection = run_projector.project_run_snapshot(base, [seed], journal_present=True)
    writer = _spawn(process_case, "writer", mode=f"kill-{boundary}")
    _wait(process_case, "writer.boundary")
    assert writer.poll() is None
    boundary_info = _read(process_case, "writer.boundary")
    written = bytes.fromhex(boundary_info["written"])
    line = bytes.fromhex(boundary_info["line"])
    interrupted = path.read_bytes()
    assert interrupted == complete_prefix + written
    assert line.endswith(b"\n")
    if boundary == "complete":
        assert boundary_info["at"] == "before-file-fsync" and written == line
        visible = _history(path, 2)
    else:
        assert boundary_info["at"] == "partial-write" and written == line[: len(line) // 2]
        assert written and not written.endswith(b"\n")
        report = run_journal.read_journal_bounded(path)
        assert report.chain_errors == [] and report.partial_tail == written
        assert [event.to_dict() for event in report.events] == [seed]
        assert (
            run_projector.project_run_snapshot(base, report.events, journal_present=True).to_bytes()
            == seed_projection.to_bytes()
        )
    successor = _spawn(
        process_case,
        "successor",
        mode="replay-after-kill" if boundary == "complete" else "recover-after-kill",
        recorded_at="2026-07-27T15:31:00.000000Z",
    )
    _wait(process_case, "successor.blocked")
    assert _read(process_case, "successor.blocked") == "byte-lock-held"
    assert not root.joinpath("successor.entered").exists()
    assert path.read_bytes() == interrupted
    assert writer.poll() is None, "writer exited before deliberate termination"
    writer.kill()
    stdout, stderr = writer.communicate(timeout=5)
    diagnostics = f"writer exited {writer.returncode}\nstdout:\n{stdout}\nstderr:\n{stderr}"
    assert "child barrier:" not in stdout + stderr and "TimeoutError" not in stdout + stderr, diagnostics
    # Windows Popen.kill aliases terminate, which calls TerminateProcess(..., 1).
    # Evaluate SIGKILL only on POSIX, where Popen reports the negative signal.
    expected_returncode = 1 if os.name == "nt" else -signal.SIGKILL
    assert writer.returncode == expected_returncode, diagnostics
    result = _finish(successor)
    assert _read(process_case, "successor.entered") == "lock-acquired"
    if boundary == "complete":
        assert result["event"] == visible[-1].to_dict()
        assert result["writes"] == []
        assert result["syncs"] == (["file", "directory"] if os.name == "posix" else ["file"])
        assert path.read_bytes() == interrupted
    else:
        assert bytes.fromhex(result["refused_bytes"]) == interrupted
        assert bytes.fromhex(result["partial"]) == written
        assert (root / "quarantine" / result["quarantine"]).read_bytes() == written
        assert bytes.fromhex(result["recovered_bytes"]) == complete_prefix
        assert result["event"] == result["replay"]
        assert len(result["writes"]) == 1
        assert bytes.fromhex(result["writes"][0]) == run_events.canonical_bytes(result["event"]) + b"\n"
    accepted = _history(path, 2)
    assert accepted[0].to_dict() == seed
    assert accepted[1].to_dict() == result["event"] and accepted[1].idempotency_key == "plan"
    rebuilt = run_projector.project_run_snapshot(seed_projection.snapshot, accepted, journal_present=True)
    assert rebuilt.status == "planning" and rebuilt.last_sequence == 2
    assert rebuilt.last_event_digest == accepted[1].event_digest
    assert rebuilt.to_bytes() == run_projector.project_run_snapshot(base, accepted, journal_present=True).to_bytes()
