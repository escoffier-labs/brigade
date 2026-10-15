"""Cross-boundary qualification of journal acceptance, replay and projection."""

from copy import deepcopy
from contextlib import contextmanager, nullcontext
import errno
import json
import os
import select
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from brigade import (
    aboyeur,
    dirfd,
    run_checkpoint,
    run_control_journal,
    run_dirfd,
    run_events,
    run_journal,
    run_lifecycle,
    run_projector,
    runguard,
)
from brigade.work_cmd import nt_dirfd

RUN_ID = "20260727-153045-a1b2c3d4"
RECORDED_AT = "2026-07-27T15:30:45.123456Z"


@pytest.fixture
def recording_nt_lock(monkeypatch):
    """Record real NtCreateFile arguments and model Win32 sharing, not locking."""
    opened, requests, converted, closed = {}, [], [], []

    def share_classes(access):
        return (1 if access & 1 else 0) | (2 if access & 6 else 0) | (4 if access & 0x10000 else 0)

    def admit(name, access, share):
        requests.append((name, access, share))
        print(f"{name}: access={access:#010x}, share={share:#x}")
        for prior_access, prior_share in opened.values():
            if share_classes(access) & ~prior_share or share_classes(prior_access) & ~share:
                raise PermissionError(errno.EACCES, "bidirectional Windows share conflict")
        opened[name] = (access, share)

    def create(handle_ref, access, obj_ref, iosb, size, attributes, share, disposition, options, ea, ea_size):
        assert obj_ref._obj.RootDirectory == 99
        assert disposition == 3  # FILE_OPEN_IF, never truncate the sibling.
        assert options == 0x00200060  # non-directory, synchronous, open reparse point itself.
        assert attributes == 0x80
        admit("bound", access, share)
        handle_ref._obj.value = 123
        return 0

    def get_info(handle, info_ref):
        info_ref._obj.dwFileAttributes = 0
        return True

    def close(handle):
        closed.append(handle)
        opened.pop("bound", None)

    api = SimpleNamespace(
        OBJECT_ATTRIBUTES=nt_dirfd._OBJECT_ATTRIBUTES,
        IO_STATUS_BLOCK=nt_dirfd._IO_STATUS_BLOCK,
        BY_HANDLE_FILE_INFORMATION=nt_dirfd._BY_HANDLE_FILE_INFORMATION,
        make_unicode=nt_dirfd._make_unicode,
        NtCreateFile=create,
        GetFileInformationByHandle=get_info,
        CloseHandle=close,
    )
    monkeypatch.setattr(nt_dirfd, "_require_api", lambda: api)
    monkeypatch.setattr(nt_dirfd, "_handle_from_fd", lambda fd: 99)
    real_convert = nt_dirfd._handle_to_fd
    monkeypatch.setattr(nt_dirfd, "_handle_to_fd", lambda api, handle, flags: converted.append(flags) or 42)
    monkeypatch.setattr(dirfd, "posix_available", lambda: False)
    monkeypatch.setattr(dirfd, "nt_available", lambda: True)
    monkeypatch.setattr(os, "O_BINARY", 0x8000, raising=False)

    @contextmanager
    def crt_peer():
        # CRT O_RDWR requests read/write and shares read/write, without delete.
        admit("crt", 3, 3)
        try:
            yield
        finally:
            opened.pop("crt")

    return SimpleNamespace(
        api=api, peer=crt_peer, requests=requests, converted=converted, closed=closed, real_convert=real_convert
    )


@pytest.mark.parametrize("first", ["bound", "unbound"])
def test_nt_journal_lock_shares_with_crt_peer_in_both_open_orders(tmp_path, monkeypatch, recording_nt_lock, first):
    """Pin the native open-time failure separately from byte-lock exclusion."""
    recording = recording_nt_lock
    path = tmp_path / "events" / "lifecycle.jsonl"
    bound = SimpleNamespace(
        dir_fd=lambda *components: 7,
        open_file=lambda components, name, flags, mode: dirfd.open_child_file(7, name, flags, mode),
    )
    monkeypatch.setattr(run_journal, "_bound_target", lambda path: (bound, ("events",), path.name))
    monkeypatch.setattr(run_journal, "_chmod_fd_or_path", lambda *args: None)
    monkeypatch.setattr(run_journal, "fcntl", None)
    monkeypatch.setattr(run_journal, "msvcrt", SimpleNamespace(LK_LOCK=1, LK_UNLCK=0, locking=lambda *args: None))
    monkeypatch.setattr(os, "lseek", lambda *args: 0)
    monkeypatch.setattr(os, "close", lambda fd: recording.api.CloseHandle(123))
    first_window = run_journal._append_critical_section(path) if first == "bound" else recording.peer()
    second_window = recording.peer() if first == "bound" else run_journal._append_critical_section(path)
    with first_window, second_window:
        pass
    request = next(request for request in recording.requests if request[0] == "bound")
    assert request == ("bound", 0x00100083, 7)  # READ_DATA|WRITE_DATA|READ_ATTRIBUTES|SYNCHRONIZE, no DELETE.
    assert recording.converted == [os.O_RDWR | os.O_BINARY]
    assert recording.closed == [123]


@pytest.mark.parametrize("failure", ["reparse", "directory", "conversion"])
def test_nt_lock_open_rejects_invalid_handles_and_closes_once(monkeypatch, recording_nt_lock, failure):
    recording = recording_nt_lock
    if failure == "conversion":

        def fail_convert(handle, flags):
            raise OSError("descriptor conversion failed")

        monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(open_osfhandle=fail_convert))
        monkeypatch.setattr(nt_dirfd, "_handle_to_fd", recording.real_convert)
    else:

        def get_info(handle, info_ref):
            info_ref._obj.dwFileAttributes = 0x400 if failure == "reparse" else 0x10
            return True

        monkeypatch.setattr(recording.api, "GetFileInformationByHandle", get_info)
    with pytest.raises(OSError, match="reparse point|is a directory|descriptor conversion failed"):
        dirfd.open_child_lock_file(7, "lifecycle.jsonl.lock")
    assert recording.closed == [123]


def test_bound_journal_lock_refuses_unavailable_dirfd_without_path_fallback(tmp_path, monkeypatch):
    path = tmp_path / "events" / "lifecycle.jsonl.lock"
    bound = SimpleNamespace(dir_fd=lambda *components: 7)
    monkeypatch.setattr(run_journal, "_bound_target", lambda path: (bound, ("events",), path.name))
    monkeypatch.setattr(dirfd, "posix_available", lambda: False)
    monkeypatch.setattr(dirfd, "nt_available", lambda: False)

    def forbidden_path_open(*args, **kwargs):
        raise AssertionError("bound sibling lock fell back to a pathname")

    monkeypatch.setattr(os, "open", forbidden_path_open)
    with pytest.raises(OSError, match="descriptor-relative sibling lock operations are unavailable"):
        run_journal._open_journal_lock(path)


def _append(path, *, event_type="run.created", payload=None, key="create", previous=0):
    return run_journal.append_event(
        path,
        run_id=RUN_ID,
        event_type=event_type,
        payload={"status": "started"} if payload is None else payload,
        idempotency_key=key,
        expected_previous_sequence=previous,
        recorded_at=RECORDED_AT,
    )


def _cancel_payload():
    return {
        "transport_capability": "interrupt",
        "transport_result": "interrupted",
        "active_remaining": 0,
        "active_seats": [],
        "outcomes": [{"seat": "coder", "transport_capability": "interrupt", "transport_result": "interrupted"}],
    }


@pytest.mark.parametrize(
    "failure",
    ["lock", "nesting", TypeError, RuntimeError, OSError],
    ids=["lock", "nesting", "private-copy-error", "private-copy-runtime-error", "private-copy-os-error"],
)
def test_payload_copy_failure_is_typed_and_preserves_journal(tmp_path, failure):
    path = tmp_path / "events" / "lifecycle.jsonl"
    _append(path)
    before = path.read_bytes()
    private_marker = "PRIVATE_COPY_FAILURE_MARKER"

    class Uncopyable:
        def __deepcopy__(self, memo):
            raise failure(private_marker + "x" * 600)

    if failure == "lock":
        value = threading.Lock()
    elif failure == "nesting":
        value = []
        for _ in range(sys.getrecursionlimit()):
            value = [value]
    else:
        value = Uncopyable()

    with pytest.raises(run_events.CanonicalizationError) as excinfo:
        _append(path, payload={"status": value}, key="invalid-copy", previous=1)
    assert private_marker not in str(excinfo.value)
    assert len(str(excinfo.value)) <= run_events.MAX_DIAGNOSTIC_LEN
    assert path.read_bytes() == before
    assert len(run_journal.read_journal_bounded(path).events) == 1


@pytest.mark.parametrize("error_type", [KeyboardInterrupt, SystemExit])
def test_payload_copy_preserves_base_exception(tmp_path, error_type):
    path = tmp_path / "events" / "lifecycle.jsonl"
    _append(path)
    before = path.read_bytes()

    class InterruptedCopy:
        def __deepcopy__(self, memo):
            raise error_type("copy interrupted")

    with pytest.raises(error_type, match="copy interrupted"):
        _append(path, payload={"status": InterruptedCopy()}, key="interrupted-copy", previous=1)
    assert path.read_bytes() == before


def test_caller_nested_mutation_does_not_change_returned_accepted_event(tmp_path):
    path = tmp_path / "events" / "lifecycle.jsonl"
    payload = _cancel_payload()
    event = _append(path, event_type="run_budget.cancelled", payload=payload)
    accepted = deepcopy(event.to_dict())
    before = path.read_bytes()

    payload["outcomes"][0]["seat"] = "reviewer"
    payload["active_seats"].append("reviewer")

    assert event.to_dict() == accepted
    assert run_events.validate_event(event.to_dict()) == []
    assert path.read_bytes() == before
    assert run_journal.read_journal_bounded(path).events[0].to_dict() == accepted


def test_returned_nested_dictionary_cannot_mutate_event_or_replay(tmp_path):
    path = tmp_path / "events" / "lifecycle.jsonl"
    event = _append(path, event_type="run_budget.cancelled", payload=_cancel_payload())
    accepted = deepcopy(event.to_dict())
    exported = event.to_dict()
    exported["payload"]["outcomes"][0]["seat"] = "reviewer"

    assert event.to_dict() == accepted
    replay = _append(path, event_type="run_budget.cancelled", payload=_cancel_payload())
    assert replay.to_dict() == accepted
    assert (
        run_projector.project_run_snapshot({"status": "started"}, [event], journal_present=True).last_event_digest
        == event.event_digest
    )


def test_caller_mutation_during_write_cannot_change_accepted_return(tmp_path, monkeypatch):
    path = tmp_path / "events" / "lifecycle.jsonl"
    payload = _cancel_payload()
    original = deepcopy(payload)
    real_write = os.write

    def mutate_caller_after_write(fd, data):
        written = real_write(fd, data)
        payload["outcomes"][0]["seat"] = "reviewer"
        return written

    monkeypatch.setattr(os, "write", mutate_caller_after_write)
    event = _append(path, event_type="run_budget.cancelled", payload=payload)
    assert event.payload == original
    assert run_events.validate_event(event.to_dict()) == []
    assert run_journal.read_journal_bounded(path).events[0].to_dict() == event.to_dict()


@pytest.mark.parametrize("source", ["append", "read", "lookup", "replay"])
def test_mutable_returned_payload_cannot_rewrite_persisted_history(tmp_path, source):
    path = tmp_path / "events" / "lifecycle.jsonl"
    event = _append(path, event_type="run_budget.cancelled", payload=_cancel_payload())
    accepted = deepcopy(event.to_dict())
    before = path.read_bytes()
    if source == "read":
        event = run_journal.read_journal_bounded(path).events[0]
    elif source == "lookup":
        event = run_journal.lookup_idempotent_event(
            path, event_type="run_budget.cancelled", payload=_cancel_payload(), idempotency_key="create"
        )
    elif source == "replay":
        event = _append(path, event_type="run_budget.cancelled", payload=_cancel_payload())
    assert event is not None
    event.payload["outcomes"][0]["seat"] = "reviewer"

    assert path.read_bytes() == before
    assert run_journal.read_journal_bounded(path).events[0].to_dict() == accepted
    with pytest.raises(run_projector.EventChainError):
        run_projector.project_run_snapshot({"status": "started"}, [event], journal_present=True)


_SAME_KEY_CHILD = r"""
import json
import sys
from pathlib import Path
from brigade import run_journal

path = Path(sys.argv[1])
detail = sys.argv[2]
report = run_journal.read_journal_bounded(path)
assert not report.chain_errors and report.partial_tail is None
tail = report.events[-1].sequence if report.events else 0
print(tail, flush=True)
assert sys.stdin.readline().strip() == "go"
try:
    event = run_journal.append_event(
        path, run_id=sys.argv[3], event_type="run.planning.started",
        payload={"detail": detail}, idempotency_key="shared-request",
        expected_previous_sequence=tail, recorded_at=sys.argv[4],
    )
    print(json.dumps({"event": event.to_dict()}), flush=True)
except run_journal.IdempotencyConflict as exc:
    print(json.dumps({"conflict": exc.existing_event_id}), flush=True)
"""


@pytest.mark.skipif(run_journal.fcntl is None, reason="cross-process locking requires POSIX flock")
@pytest.mark.parametrize("conflicting", [False, True])
def test_same_key_process_race_has_one_record_and_consistent_result(tmp_path, conflicting):
    path = tmp_path / "events" / "lifecycle.jsonl"
    run_journal.ensure_journal(path)
    children = []
    try:
        for detail in ("alpha", "beta" if conflicting else "alpha"):
            children.append(
                subprocess.Popen(
                    [sys.executable, "-c", _SAME_KEY_CHILD, str(path), detail, RUN_ID, RECORDED_AT],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
        # Both processes have read the identical head before either may append.
        for child in children:
            assert select.select([child.stdout], [], [], 15)[0], "child did not reach head barrier"
            assert child.stdout.readline().strip() == "0"
        for child in children:
            child.stdin.write("go\n")
            child.stdin.flush()
        results = []
        for child in children:
            stdout, stderr = child.communicate(timeout=15)
            assert child.returncode == 0, stderr
            results.append(json.loads(stdout))
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)

    report = run_journal.read_journal_bounded(path)
    assert report.chain_errors == []
    assert report.partial_tail is None
    assert len(report.events) == 1
    accepted = report.events[0]
    assert accepted.sequence == 1 and accepted.previous_digest is None
    assert all(result["event"] == accepted.to_dict() for result in results if "event" in result)
    if conflicting:
        assert sum("conflict" in result for result in results) == 1
        assert next(result["conflict"] for result in results if "conflict" in result) == accepted.event_id
    else:
        assert results == [{"event": accepted.to_dict()}] * 2
    before = path.read_bytes()
    replay = _append(path, event_type=accepted.event_type, payload=accepted.payload, key="shared-request", previous=0)
    assert replay.to_dict() == accepted.to_dict()
    assert path.read_bytes() == before


@pytest.mark.parametrize("bound", [False, True], ids=["pathname", "bound-directory"])
def test_crlf_recovery_preserves_complete_bytes_and_quarantines_exact_suffix(tmp_path, monkeypatch, bound):
    path = tmp_path / "events" / "lifecycle.jsonl"
    first = _append(path)
    complete = path.read_bytes().rstrip(b"\r\n") + b"\r\n"
    partial = b'{"incomplete":\r'
    path.write_bytes(complete + partial)

    binding = run_dirfd.bound_run_dir(tmp_path) if bound else nullcontext()
    with binding, monkeypatch.context() as patch:
        if os.name == "posix":
            # Model CRT translation on real descriptors, not on recovery itself.
            # The fixture fits in one journal read, so CRLF cannot straddle chunks.
            binary_flag = 1 << 30
            real_open, real_read, real_write, real_close = os.open, os.read, os.write, os.close
            text_fds = set()

            def crt_open(opened_path, flags, mode=0o777, *, dir_fd=None):
                fd = real_open(opened_path, flags & ~binary_flag, mode, dir_fd=dir_fd)
                text_fds.discard(fd)
                if not flags & binary_flag:
                    text_fds.add(fd)
                return fd

            def crt_read(fd, size):
                data = real_read(fd, size)
                return data.replace(b"\r\n", b"\n") if fd in text_fds else data

            def crt_write(fd, data):
                physical = data.replace(b"\n", b"\r\n") if fd in text_fds else data
                written = real_write(fd, physical)
                return len(data) if written == len(physical) else written

            def crt_close(fd):
                try:
                    return real_close(fd)
                finally:
                    text_fds.discard(fd)

            patch.setattr(os, "O_BINARY", binary_flag, raising=False)
            patch.setattr(os, "open", crt_open)
            patch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {crt_open})
            patch.setattr(os, "read", crt_read)
            patch.setattr(os, "write", crt_write)
            patch.setattr(os, "close", crt_close)

        report = run_journal.read_journal_bounded(path)
        assert report.events == [first] and report.chain_errors == []
        assert report.partial_tail == partial
        recovery = run_journal.recover_partial_tail(path, tmp_path / "quarantine")
        assert recovery.partial_bytes == partial
        assert recovery.quarantine_path.read_bytes() == partial
        assert path.read_bytes() == complete

        second = _append(
            path, event_type="run.planning.started", payload={"detail": "planning"}, key="plan", previous=1
        )
        appended = path.read_bytes()[len(complete) :]
        assert appended.endswith(b"\n") and b"\r" not in appended
        assert path.read_bytes() == complete + appended
        report = run_journal.read_journal_bounded(path)
        assert report.events == [first, second] and report.chain_errors == [] and report.partial_tail is None


def test_nt_bound_recovery_then_append_preserves_accepted_bytes(tmp_path, monkeypatch):
    """Use real NT calls on Windows, translating only the OS boundary on POSIX."""
    path = tmp_path / "events" / "lifecycle.jsonl"
    first = _append(path)
    complete = path.read_bytes()
    partial = b'{"incomplete":'
    path.write_bytes(complete + partial)

    if sys.platform != "win32":
        import fcntl
        import stat

        monkeypatch.setattr(nt_dirfd, "_require_api", lambda: SimpleNamespace(CloseHandle=os.close))

        def create(api, parent, name, *, access, disposition, options, attributes):
            nt_dirfd.validate_component(name)
            flags = dirfd.file_flags(os.O_RDONLY)
            if access & 1 and access & (2 | 4):
                flags |= os.O_RDWR
            elif access & (2 | 4):
                flags |= os.O_WRONLY
            if disposition == 2:  # FILE_CREATE
                flags |= os.O_CREAT | os.O_EXCL
            elif disposition == 3:  # FILE_OPEN_IF
                flags |= os.O_CREAT
            else:
                assert disposition == 1  # FILE_OPEN
            return os.open(name, flags, 0o600, dir_fd=parent)

        def reject_reparse(api, handle, *, expected_directory):
            assert not expected_directory
            assert stat.S_ISREG(os.fstat(handle).st_mode)

        def convert(api, handle, flags):
            current = fcntl.fcntl(handle, fcntl.F_GETFL)
            fcntl.fcntl(handle, fcntl.F_SETFL, current | (flags & os.O_APPEND))
            return handle

        monkeypatch.setattr(nt_dirfd, "_nt_create", create)
        monkeypatch.setattr(nt_dirfd, "_reject_reparse", reject_reparse)
        monkeypatch.setattr(nt_dirfd, "_handle_to_fd", convert)
        monkeypatch.setattr(dirfd, "open_child_file", nt_dirfd.open_file)

    with run_dirfd.bound_run_dir(tmp_path) as bound:
        assert bound is not None
        recovery = run_journal.recover_partial_tail(path, tmp_path / "quarantine")
        assert recovery.partial_bytes == partial
        assert recovery.quarantine_path.read_bytes() == partial
        assert path.read_bytes() == complete
        second = _append(
            path, event_type="run.planning.started", payload={"detail": "planning"}, key="plan", previous=1
        )
        assert path.read_bytes().startswith(complete)
        report = run_journal.read_journal_bounded(path)
        assert report.events == [first, second] and report.chain_errors == [] and report.partial_tail is None


def test_bound_recovery_directory_handles_without_posix_flag(tmp_path, monkeypatch):
    """Recovery owns its directory fd, while the binding keeps its cached fd."""
    path = tmp_path / "events" / "lifecycle.jsonl"
    first = _append(path)
    complete = path.read_bytes()
    partial = b'{"incomplete":'
    quarantine = tmp_path / "quarantine"
    quarantine.mkdir(mode=0o700)

    if sys.platform != "win32":
        import stat

        # Model the missing POSIX flag and NT's file-only open contract.
        monkeypatch.setattr(run_journal, "_O_DIRECTORY", 1 << 29)
        monkeypatch.setattr(run_journal, "_HAS_O_DIRECTORY", False)
        real_open_file = dirfd.open_child_file

        def file_only_open(parent, name, flags, mode=0o600):
            fd = real_open_file(parent, name, flags, mode)
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                os.close(fd)
                raise IsADirectoryError("path component is a directory")
            return fd

        monkeypatch.setattr(dirfd, "open_child_file", file_only_open)

    with run_dirfd.bound_run_dir(tmp_path) as bound:
        assert bound is not None
        parent_fd = bound.dir_fd()
        bound.dir_fd("events")
        cached_fd = bound.dir_fd("quarantine")
        held_identity = os.fstat(cached_fd)
        opened = []
        real_open_directory = dirfd.open_child_directory

        def track_directory(parent, name):
            fd = real_open_directory(parent, name)
            opened.append(fd)
            return fd

        monkeypatch.setattr(dirfd, "open_child_directory", track_directory)
        # Repeat after callers close their temporary descriptors. The held
        # parent and cached quarantine handle must remain usable both times.
        for _ in range(2):
            path.write_bytes(complete + partial)
            recovery = run_journal.recover_partial_tail(path, quarantine)
            assert recovery.partial_bytes == partial
            assert recovery.quarantine_path.read_bytes() == partial
            assert path.read_bytes() == complete
            os.fstat(parent_fd)
            current = os.fstat(cached_fd)
            assert (current.st_dev, current.st_ino) == (held_identity.st_dev, held_identity.st_ino)
            assert opened
            for fd in opened:
                with pytest.raises(OSError):
                    os.fstat(fd)
            opened.clear()

        second = _append(
            path, event_type="run.planning.started", payload={"detail": "planning"}, key="plan", previous=1
        )
        assert path.read_bytes().startswith(complete)
        report = run_journal.read_journal_bounded(path)
        assert report.events == [first, second] and report.chain_errors == [] and report.partial_tail is None


def test_short_write_recovery_then_retry_accepts_exactly_once(tmp_path, monkeypatch):
    path = tmp_path / "events" / "lifecycle.jsonl"
    first = _append(path)
    before = path.read_bytes()
    projection = run_projector.project_run_snapshot({"status": "started"}, [first], journal_present=True)
    real_write = os.write
    with monkeypatch.context() as patch:
        patch.setattr(os, "write", lambda fd, data: real_write(fd, data[: len(data) // 2]))
        with pytest.raises(run_journal.PartialWriteError):
            _append(path, event_type="run.planning.started", payload={"detail": "planning"}, key="plan", previous=1)

    interrupted = path.read_bytes()
    report = run_journal.read_journal_bounded(path)
    assert report.chain_errors == []
    assert report.events == [first]
    assert report.partial_tail == interrupted[len(before) :]
    assert (
        run_projector.project_run_snapshot({"status": "started"}, report.events, journal_present=True).to_bytes()
        == projection.to_bytes()
    )
    with pytest.raises(run_journal.PartialTailError):
        _append(path, event_type="run.planning.started", payload={"detail": "planning"}, key="plan", previous=1)
    assert path.read_bytes() == interrupted

    recovery = run_journal.recover_partial_tail(path, tmp_path / "quarantine")
    assert recovery.quarantine_path.read_bytes() == report.partial_tail
    assert path.read_bytes() == before
    second = _append(path, event_type="run.planning.started", payload={"detail": "planning"}, key="plan", previous=1)
    accepted = path.read_bytes()
    assert (
        _append(path, event_type="run.planning.started", payload={"detail": "planning"}, key="plan", previous=1)
        == second
    )
    assert path.read_bytes() == accepted
    assert second.sequence == 2 and second.previous_digest == first.event_digest
    rebuilt = run_projector.project_run_snapshot(
        projection.snapshot, run_journal.read_journal_bounded(path).events, journal_present=True
    )
    assert rebuilt.status == "planning" and rebuilt.last_sequence == 2


@pytest.mark.parametrize("failure", ["short_write", "file_fsync", "directory_fsync"])
def test_failed_quarantine_durability_preserves_partial_journal(tmp_path, monkeypatch, failure):
    path = tmp_path / "events" / "lifecycle.jsonl"
    first = _append(path)
    before = path.read_bytes() + b'{"incomplete":'
    path.write_bytes(before)
    quarantine = tmp_path / "quarantine"

    def fail_fsync(fd):
        raise OSError(errno.EIO, "injected quarantine fsync failure")

    def fail_directory_fsync(directory):
        raise run_journal.RunJournalError("injected quarantine directory fsync failure")

    real_write = os.write
    with monkeypatch.context() as patch:
        if failure == "short_write":
            patch.setattr(os, "write", lambda fd, data: real_write(fd, data[: len(data) // 2]))
            error = run_journal.PartialWriteError
        elif failure == "file_fsync":
            patch.setattr(os, "fsync", fail_fsync)
            error = OSError
        else:
            patch.setattr(run_journal, "_fsync_directory", fail_directory_fsync)
            error = run_journal.RunJournalError
        with pytest.raises(error):
            run_journal.recover_partial_tail(path, quarantine)
    assert path.read_bytes() == before
    report = run_journal.read_journal_bounded(path)
    assert report.events == [first] and report.partial_tail == b'{"incomplete":'
    recovery = run_journal.recover_partial_tail(path, quarantine)
    assert recovery.quarantine_path.read_bytes() == report.partial_tail
    assert path.read_bytes() == before[: -len(report.partial_tail)]


def test_derived_drift_rebuilds_but_invalid_authority_fails_closed(tmp_path):
    path = tmp_path / "events" / "lifecycle.jsonl"
    _append(path)
    _append(path, event_type="run.planning.started", payload={"detail": "planning"}, key="plan", previous=1)
    authoritative = path.read_bytes()
    report = run_journal.read_journal_bounded(path)
    base = {"status": "started", "failure": {"detail": None}}
    expected = run_projector.project_run_snapshot(base, report.events, journal_present=True)
    drifted = {
        **base,
        "status": "failed",
        "projector_version": -1,
        "journal_present": False,
        "journal_last_sequence": 999,
        "journal_last_event_digest": "0" * 64,
    }
    rebuilt = run_projector.project_run_snapshot(drifted, report.events, journal_present=True)
    assert rebuilt.to_bytes() == expected.to_bytes()
    assert path.read_bytes() == authoritative

    corrupted = authoritative.replace(b'"detail":"planning"', b'"detail":"tampered"')
    assert corrupted != authoritative
    path.write_bytes(corrupted)
    rejected = run_journal.read_journal_bounded(path)
    assert rejected.chain_errors
    assert [event.sequence for event in rejected.events] == [1]
    with pytest.raises(run_journal.ChainIntegrityError):
        _append(path, event_type="run.completed", payload={"status": "completed"}, key="complete", previous=2)
    with pytest.raises(run_journal.ChainIntegrityError):
        run_journal.lookup_idempotent_event(
            path, event_type="run.created", payload={"status": "started"}, idempotency_key="create"
        )
    with pytest.raises(run_projector.EventChainError):
        run_projector.project_run_snapshot(
            base, [json.loads(line) for line in corrupted.splitlines()], journal_present=True
        )
    assert path.read_bytes() == corrupted


def test_prewrite_error_leaves_journal_and_projection_unchanged(tmp_path, monkeypatch):
    path = tmp_path / "events" / "lifecycle.jsonl"
    first = _append(path)
    before = path.read_bytes()
    projection = run_projector.project_run_snapshot({"status": "started"}, [first], journal_present=True)

    def fail_write(fd, data):
        raise OSError(errno.EIO, "injected journal write failure")

    monkeypatch.setattr(os, "write", fail_write)
    with pytest.raises(OSError, match="injected journal write failure"):
        _append(path, event_type="run.planning.started", payload={"detail": "planning"}, key="plan", previous=1)
    assert path.read_bytes() == before
    report = run_journal.read_journal_bounded(path)
    assert report.events == [first] and report.chain_errors == [] and report.partial_tail is None
    assert (
        run_projector.project_run_snapshot({"status": "started"}, report.events, journal_present=True).to_bytes()
        == projection.to_bytes()
    )


def test_failed_fsync_leaves_snapshot_unchanged_but_complete_journal_line_visible(tmp_path, monkeypatch):
    """Qualification limit: a failed fsync is not proof that no bytes persisted."""
    repo = tmp_path / "repo"
    run_dir = repo / ".brigade" / "runs" / RUN_ID
    run_dir.mkdir(parents=True)
    snapshot_path = run_dir / "run.json"
    payload = {
        "schema": "brigade.run.v1",
        "status": "started",
        "lock_workspace": str(repo),
        "lifecycle_journal_requested": True,
    }
    with runguard.run_lock(repo, run_dir=run_dir):
        aboyeur._write_json(snapshot_path, payload)
    path = run_dir / "events" / "lifecycle.jsonl"
    before_snapshot = snapshot_path.read_bytes()
    before_journal = path.read_bytes()
    real_fsync = os.fsync
    journal_inode = path.stat()

    def fail_journal_fsync(fd):
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == (journal_inode.st_dev, journal_inode.st_ino):
            raise OSError(errno.EIO, "injected journal fsync failure")
        return real_fsync(fd)

    with runguard.run_lock(repo, run_dir=run_dir):
        with monkeypatch.context() as patch:
            patch.setattr(os, "fsync", fail_journal_fsync)
            with pytest.raises(run_lifecycle.LifecycleJournalError) as failure_info:
                aboyeur._write_json(snapshot_path, {**payload, "status": "planning"})

    assert getattr(failure_info.value, "outcome", None) == "indeterminate"
    assert snapshot_path.read_bytes() == before_snapshot
    # This is the confirmed gap in the stronger failed-append invariant.
    # No successful return occurred, yet the complete checkpoint line is
    # visible to readers. Its persistence across power loss is unknown.
    assert path.read_bytes() != before_journal
    report = run_journal.read_journal_bounded(path)
    assert report.chain_errors == [] and report.partial_tail is None
    assert len(report.events) == len(before_journal.splitlines()) + 1


@pytest.mark.parametrize("failure", ["file", "directory"])
def test_failed_append_reports_indeterminate_outcome_without_discarding_history(tmp_path, monkeypatch, failure):
    """The approved contract retains bytes without reporting definite rejection."""
    path = tmp_path / "events" / "lifecycle.jsonl"
    first = _append(path)
    base = {"status": "started"}
    before = path.read_bytes()

    def fail_sync(*args):
        raise OSError(errno.EIO, "injected journal sync failure")

    with monkeypatch.context() as patch:
        if failure == "file":
            patch.setattr(os, "fsync", fail_sync)
        else:
            patch.setattr(run_journal, "_fsync_directory", fail_sync)
        with pytest.raises((OSError, run_journal.RunJournalError)) as failure_info:
            _append(path, event_type="run.planning.started", payload={"detail": "planning"}, key="plan", previous=1)

    report = run_journal.read_journal_bounded(path)
    assert report.chain_errors == [] and report.partial_tail is None
    assert path.read_bytes() != before
    assert report.events[0] == first and len(report.events) == 2
    forensic = run_projector.project_run_snapshot(base, report.events, journal_present=True)
    assert forensic.status == "planning" and forensic.last_sequence == 2
    assert getattr(failure_info.value, "outcome", None) == "indeterminate"


@pytest.mark.parametrize("failure", ["file", "directory"])
def test_recovery_cannot_publish_projection_when_fresh_journal_sync_fails(tmp_path, monkeypatch, failure):
    """A recovered projection needs its own barrier, independent of old receipts."""
    repo = tmp_path / "repo"
    run_dir = repo / ".brigade" / "runs" / RUN_ID
    run_dir.mkdir(parents=True)
    snapshot_path = run_dir / "run.json"
    bootstrap = {"schema": "brigade.run.v1", "status": "started", "lifecycle_journal_requested": True}
    snapshot_path.write_bytes(run_projector.encode_snapshot_bytes(bootstrap))
    base = {**bootstrap, "status": "planning", "run_journal_authority_requested": True}
    with runguard.run_lock(repo, run_dir=run_dir):
        checkpoint = run_checkpoint.write_checkpoint(
            run_dir,
            run_projector.encode_snapshot_bytes(base),
            workspace=repo,
            paired_event_type="run.planning.started",
            body_kind="base-stripped",
        )
    assert checkpoint is not None
    path = run_dir / "events" / "lifecycle.jsonl"
    journal_inode = path.stat()
    real_fsync = os.fsync
    real_directory_sync = run_journal._fsync_directory

    def fail_file_sync(fd):
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == (journal_inode.st_dev, journal_inode.st_ino):
            raise OSError(errno.EIO, "injected journal recovery sync failure")
        return real_fsync(fd)

    def fail_directory_sync(directory):
        if directory == path.parent:
            raise run_journal.RunJournalError("injected journal recovery directory sync failure")
        return real_directory_sync(directory)

    with monkeypatch.context() as patch:
        if failure == "file":
            patch.setattr(os, "fsync", fail_file_sync)
        else:
            patch.setattr(run_journal, "_fsync_directory", fail_directory_sync)
        with pytest.raises((OSError, run_journal.RunJournalError)):
            _append(
                path,
                event_type="run.planning.started",
                payload={"detail": "planning"},
                key="plan",
                previous=checkpoint.sequence,
            )
        uncertain = path.read_bytes()
        snapshot_path.unlink()
        report = run_journal.read_journal_bounded(path)
        assert report.chain_errors == [] and report.partial_tail is None
        assert report.events[-1].event_type == "run.planning.started"
        with pytest.raises(run_checkpoint.CheckpointError) as failure_info:
            run_checkpoint.recover_from_checkpoint(run_dir, None)
        assert failure_info.value.category == "journal-sync-indeterminate"
        assert not snapshot_path.exists()
        assert path.read_bytes() == uncertain

    syncs = []
    directory_inode = path.parent.stat()

    def track_sync(fd):
        info = os.fstat(fd)
        identity = (info.st_dev, info.st_ino)
        if identity == (journal_inode.st_dev, journal_inode.st_ino):
            syncs.append("file")
        elif identity == (directory_inode.st_dev, directory_inode.st_ino):
            syncs.append("directory")
        return real_fsync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", track_sync)
        recovered = run_checkpoint.recover_from_checkpoint(run_dir, None)
    assert syncs == (["file", "directory"] if os.name == "posix" else ["file"])
    assert recovered["status"] == "planning"
    assert recovered["journal_last_sequence"] == report.events[-1].sequence
    assert recovered["journal_last_event_digest"] == report.events[-1].event_digest
    assert json.loads(snapshot_path.read_bytes()) == recovered
    assert path.read_bytes() == uncertain


@pytest.mark.parametrize("phase", ["claim", "terminal"])
def test_control_sync_failure_preserves_code_and_exposes_indeterminate_outcome(tmp_path, monkeypatch, phase):
    run_dir = tmp_path / RUN_ID
    path = run_dir / "events" / "lifecycle.jsonl"
    _append(path)
    calls = []
    real_fsync = os.fsync

    def fail_sync(fd):
        raise OSError(errno.EIO, "injected control sync failure")

    def send(payload):
        calls.append(payload)
        monkeypatch.setattr(os, "fsync", fail_sync)
        return {"ok": True}

    if phase == "claim":
        monkeypatch.setattr(os, "fsync", fail_sync)
    with pytest.raises(run_control_journal.ControlJournalError) as failure_info:
        run_control_journal.execute_control_request(run_dir, op="interrupt", request_id="sync-uncertain", send=send)
    assert failure_info.value.code == "control_append_failed"
    assert getattr(failure_info.value, "outcome", None) == "indeterminate"
    assert len(calls) == (0 if phase == "claim" else 1)
    before = path.read_bytes()
    report = run_journal.read_journal_bounded(path)
    assert report.chain_errors == [] and report.partial_tail is None
    assert report.events[-1].event_type == ("control.requested" if phase == "claim" else "control.observed")
    with pytest.raises(run_control_journal.ControlJournalError) as retry_info:
        run_control_journal.execute_control_request(run_dir, op="interrupt", request_id="sync-uncertain", send=send)
    assert retry_info.value.code == ("indeterminate" if phase == "claim" else "control_append_failed")
    if phase == "terminal":
        assert retry_info.value.outcome == "indeterminate"
        monkeypatch.setattr(os, "fsync", real_fsync)
        replay = run_control_journal.execute_control_request(
            run_dir, op="interrupt", request_id="sync-uncertain", send=send
        )
        assert replay.replayed is True
    assert len(calls) == (0 if phase == "claim" else 1)
    assert path.read_bytes() == before


@pytest.mark.parametrize("failure", ["sequence", "digest", "partial", "close"])
def test_recovery_barrier_refuses_unverified_head_or_close_failure(tmp_path, monkeypatch, failure):
    repo = tmp_path / "repo"
    run_dir = repo / ".brigade" / "runs" / RUN_ID
    run_dir.mkdir(parents=True)
    snapshot = run_dir / "run.json"
    base = {"schema": "brigade.run.v1", "status": "started", "lifecycle_journal_requested": True}
    snapshot.write_bytes(run_projector.encode_snapshot_bytes(base))
    with runguard.run_lock(repo, run_dir=run_dir):
        assert (
            run_checkpoint.write_checkpoint(
                run_dir, run_projector.encode_snapshot_bytes(base), workspace=repo, paired_event_type=None
            )
            is not None
        )
    snapshot.unlink()
    path = run_dir / "events" / "lifecycle.jsonl"
    before = path.read_bytes()
    real_tail = run_journal._read_tail_state
    real_close, real_fsync = os.close, os.fsync
    journal_identity = path.stat()

    def changed_tail(journal_path):
        sequence, digest, index, partial, size = real_tail(journal_path)
        if failure == "sequence":
            sequence += 1
        elif failure == "digest":
            digest = "0" * 64
        elif failure == "partial":
            partial = b"incomplete"
        return sequence, digest, index, partial, size

    def fail_close(fd):
        info = os.fstat(fd)
        real_close(fd)
        if (info.st_dev, info.st_ino) == (journal_identity.st_dev, journal_identity.st_ino):
            raise OSError(errno.EIO, "injected recovery close failure")

    with monkeypatch.context() as patch:
        if failure == "close":

            def at_sync(fd):
                patch.setattr(os, "close", fail_close)
                return real_fsync(fd)

            patch.setattr(os, "fsync", at_sync)
        else:
            patch.setattr(run_journal, "_read_tail_state", changed_tail)
        with pytest.raises(run_checkpoint.CheckpointError) as failure_info:
            run_checkpoint.recover_from_checkpoint(run_dir, None)
    assert failure_info.value.category == ("journal-sync-indeterminate" if failure == "close" else "journal-sync")
    assert not snapshot.exists()
    assert path.read_bytes() == before


# Written to a private fixture root and run with an explicit environment. The
# probe uses the platform's real lock primitive (flock or msvcrt byte zero) on a
# fresh sibling-lock descriptor; open or unexpected probe errors fail the child.
_ADMITTED_CONTROL_CHILD = r"""
import errno
import json
import os
import sys
import time
from pathlib import Path

from brigade import fleet_client, run_control_journal, run_journal

# Reporting is outside this storage qualification. Do not discover host fleet
# identities, credentials, or repository metadata from a fixture's ancestors.
fleet_client.report_journal_event = lambda *args, **kwargs: False
markers = Path(sys.argv[1])
run_dir = Path(sys.argv[2])
journal = run_control_journal._journal_path(run_dir)

def mark(name, value):
    temporary = markers / (name + ".tmp")
    temporary.write_text(json.dumps(value), encoding="utf-8")
    temporary.replace(markers / name)

def wait_for(name):
    deadline = time.monotonic() + 30
    while not (markers / name).exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("child barrier: " + name)
        time.sleep(0.01)

def sibling_lock_held():
    fd = run_journal._open_journal_lock(run_journal._journal_lock_path(journal))
    try:
        if run_journal.fcntl is not None:
            try:
                run_journal.fcntl.flock(fd, run_journal.fcntl.LOCK_EX | run_journal.fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                    raise
                return "flock-contended"
            run_journal.fcntl.flock(fd, run_journal.fcntl.LOCK_UN)
            return None
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            run_journal.msvcrt.locking(fd, run_journal.msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EDEADLK):
                raise
            return "byte-lock-contended"
        os.lseek(fd, 0, os.SEEK_SET)
        run_journal.msvcrt.locking(fd, run_journal.msvcrt.LK_UNLCK, 1)
        return None
    finally:
        os.close(fd)

# Probe mode: report the sibling lock state from a process that holds no lock.
if len(sys.argv) > 3 and sys.argv[3] == "probe":
    mark(sys.argv[4], sibling_lock_held())
    sys.exit(0)

workspace = run_control_journal._preflight_live_control(run_dir)
mark("admitted", {"workspace": None if workspace is None else str(workspace)})
wait_for("go")
contention = sibling_lock_held()
if contention is not None:
    mark("blocked", contention)
fingerprint = run_control_journal.control_fingerprint_payload(op="interrupt", request_id="admitted")
try:
    state, requested, _terminal = run_control_journal._claim_control_request(
        journal, run_dir=run_dir, workspace=workspace,
        run_id=run_dir.name, request_id="admitted", fingerprint=fingerprint,
    )
except run_control_journal.ControlJournalError as exc:
    mark("rejected", {"code": exc.code, "contention": contention})
else:
    mark("claimed", {"state": state, "sequence": requested.sequence, "contention": contention})
"""


def _await_marker(markers, child, *names):
    """Bounded file-barrier poll that fails fast if the child exits first."""
    deadline = time.monotonic() + 20
    while True:
        found = [name for name in names if (markers / name).exists()]
        if found:
            return found[0], json.loads((markers / found[0]).read_text(encoding="utf-8"))
        if child.poll() is not None:
            stdout, stderr = child.communicate(timeout=5)
            pytest.fail(f"child exited {child.returncode} before {names}\nstdout:\n{stdout}\nstderr:\n{stderr}")
        if time.monotonic() >= deadline:
            pytest.fail(f"parent barrier timed out: {names}")
        time.sleep(0.01)


def test_recovery_publishes_only_the_head_it_synchronized(tmp_path, monkeypatch):
    """A control claim admitted before recovery cannot append inside the publication window.

    The fixture snapshot has no ``lock_workspace``, so the real preflight admits
    the claim on its journal-only compatibility path. No owner process exists or
    dies here; this qualifies storage interleaving of an admitted control writer
    against recovery's synchronized head and snapshot publication.
    """
    repo = tmp_path / "repo"
    run_dir = repo / ".brigade" / "runs" / RUN_ID
    run_dir.mkdir(parents=True)
    snapshot = run_dir / "run.json"
    base = {"schema": "brigade.run.v1", "status": "started", "lifecycle_journal_requested": True}
    snapshot.write_bytes(run_projector.encode_snapshot_bytes(base))
    with runguard.run_lock(repo, run_dir=run_dir):
        checkpoint = run_checkpoint.write_checkpoint(
            run_dir,
            run_projector.encode_snapshot_bytes({**base, "run_journal_authority_requested": True}),
            workspace=repo,
            paired_event_type=None,
            body_kind="base-stripped",
        )
    assert checkpoint is not None
    path = run_dir / "events" / "lifecycle.jsonl"
    synchronized = run_journal.read_journal_bounded(path).events[-1]
    assert synchronized.sequence == checkpoint.sequence
    child_root = tmp_path / "child"
    markers = child_root / "markers"
    markers.mkdir(parents=True)
    (child_root / "child.py").write_text(_ADMITTED_CONTROL_CHILD, encoding="utf-8")
    environment = {
        "HOME": str(child_root),
        "USERPROFILE": str(child_root),
        "TMP": str(child_root),
        "TEMP": str(child_root),
        "TMPDIR": str(child_root),
        "PATH": os.defpath,
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONIOENCODING": "utf-8",
        "BRIGADE_HOME": str(child_root / "operator" / ".brigade"),
        "BRIGADE_USER_DIR": str(child_root / "operator" / ".brigade"),
        "BRIGADE_NO_UPDATE_CHECK": "1",
    }
    if os.name == "nt":
        for key in ("SYSTEMROOT", "WINDIR"):
            if key in os.environ:
                environment[key] = os.environ[key]
    published = {}
    real_restore = run_checkpoint._restore_run_json_from_checkpoint

    def restore_at_publication(*args, **kwargs):
        # Release the admitted writer at the publication point. Whichever of
        # "blocked" (sibling lock contended) or "claimed" (append landed) it
        # reports first is decided by the lock state, not by scheduling.
        markers.joinpath("go").touch()
        published["window"] = _await_marker(markers, child, "blocked", "claimed")
        tail = run_journal.read_journal_bounded(path).events[-1]
        published["head"] = (tail.sequence, tail.event_digest)
        return real_restore(*args, **kwargs)

    child = subprocess.Popen(
        [sys.executable, str(child_root / "child.py"), str(markers), str(run_dir)],
        cwd=child_root,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        assert _await_marker(markers, child, "admitted") == ("admitted", {"workspace": None})
        snapshot.unlink()
        monkeypatch.setattr(run_checkpoint, "_restore_run_json_from_checkpoint", restore_at_publication)
        with runguard.run_lock(repo, run_dir=run_dir):
            recovered = run_checkpoint.recover_from_checkpoint(run_dir, None)
        stdout, stderr = child.communicate(timeout=20)
        assert child.returncode == 0, f"child exited {child.returncode}\nstdout:\n{stdout}\nstderr:\n{stderr}"
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)

    contention = "flock-contended" if run_journal.fcntl is not None else "byte-lock-contended"
    assert published["head"] == (synchronized.sequence, synchronized.event_digest)
    assert published["window"] == ("blocked", contention)
    assert recovered["journal_last_sequence"] == synchronized.sequence
    assert recovered["journal_last_event_digest"] == synchronized.event_digest
    assert json.loads(snapshot.read_bytes()) == recovered
    claim = json.loads(markers.joinpath("claimed").read_text(encoding="utf-8"))
    assert claim == {"state": "claimed", "sequence": synchronized.sequence + 1, "contention": contention}
    events = run_journal.read_journal_bounded(path).events
    assert events[synchronized.sequence - 1].to_dict() == synchronized.to_dict()
    assert [event.event_type for event in events[synchronized.sequence :]] == ["control.requested"]
    assert events[-1].previous_digest == synchronized.event_digest


def test_caller_terminalization_holds_journal_until_terminal_receipt(tmp_path, monkeypatch, capsys):
    """An admitted control claim cannot append between publication and terminalization.

    Regression for the PR #1681 residual race through the real ``brigade runs
    recover`` caller. A control claim admitted by the real
    ``_preflight_live_control`` before recovery blocks on the journal lock during
    checkpoint publication. Recovery must keep that lock through runguard's
    terminal receipt write, and the claim must recheck liveness under the lock,
    so the writer is refused ``control_run_not_live`` and the terminal cursor
    names the journal's actual tail.

    At the terminal boundary a separate process probes the real sibling lock.
    If it is free, the writer is allowed to finish first so the defect shows
    deterministically instead of depending on scheduling.

    Exercised: the journal-only compatibility preflight (no ``lock_workspace``)
    and a stale run lock whose recorded owner pid does not exist. No owner
    process is started or killed, so owner death is not exercised.
    """
    from brigade import doctor, run_audit, runs_cmd

    repo = tmp_path / "repo"
    run_dir = repo / ".brigade" / "runs" / RUN_ID
    run_dir.mkdir(parents=True)
    snapshot = run_dir / "run.json"
    base = {"schema": "brigade.run.v1", "status": "started", "lifecycle_journal_requested": True}
    snapshot.write_bytes(run_projector.encode_snapshot_bytes(base))
    with runguard.run_lock(repo, run_dir=run_dir):
        checkpoint = run_checkpoint.write_checkpoint(
            run_dir,
            run_projector.encode_snapshot_bytes({**base, "run_journal_authority_requested": True}),
            workspace=repo,
            paired_event_type=None,
            body_kind="base-stripped",
        )
    assert checkpoint is not None
    path = run_dir / "events" / "lifecycle.jsonl"
    synchronized = run_journal.read_journal_bounded(path).events[-1]
    # A dead owner's lock for this run: the recorded pid is above any pid_max.
    lock = runguard.lock_path(repo)
    lock.mkdir(parents=True)
    dead_pid = 99999999
    (lock / "pid").write_text(f"{dead_pid}\n", encoding="utf-8")
    (lock / "owner.json").write_text(
        json.dumps(
            {
                "schema": "brigade.run_lock.v1",
                "owner_token": "dead-owner",
                "pid": dead_pid,
                "run_dir": str(run_dir.resolve()),
                "acquired_at": "2026-10-12T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    child_root = tmp_path / "child"
    markers = child_root / "markers"
    markers.mkdir(parents=True)
    child_script = child_root / "child.py"
    child_script.write_text(_ADMITTED_CONTROL_CHILD, encoding="utf-8")
    environment = {
        "HOME": str(child_root),
        "USERPROFILE": str(child_root),
        "TMP": str(child_root),
        "TEMP": str(child_root),
        "TMPDIR": str(child_root),
        "PATH": os.defpath,
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONIOENCODING": "utf-8",
        "BRIGADE_HOME": str(child_root / "operator" / ".brigade"),
        "BRIGADE_USER_DIR": str(child_root / "operator" / ".brigade"),
        "BRIGADE_NO_UPDATE_CHECK": "1",
    }
    if os.name == "nt":
        for key in ("SYSTEMROOT", "WINDIR"):
            if key in os.environ:
                environment[key] = os.environ[key]
    observed = {}
    real_restore = run_checkpoint._restore_run_json_from_checkpoint
    real_terminalize = runguard._recover_run_artifact

    def restore_at_publication(*args, **kwargs):
        markers.joinpath("go").touch()
        observed["window"] = _await_marker(markers, child, "blocked", "claimed")
        return real_restore(*args, **kwargs)

    def terminalize_at_boundary(*args, **kwargs):
        probe = subprocess.run(
            [sys.executable, str(child_script), str(markers), str(run_dir), "probe", "terminal-probe"],
            cwd=child_root,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=20,
        )
        assert probe.returncode == 0, f"probe stdout:\n{probe.stdout}\nstderr:\n{probe.stderr}"
        observed["terminal_lock"] = json.loads(markers.joinpath("terminal-probe").read_text(encoding="utf-8"))
        if observed["terminal_lock"] is None:
            # The journal lock was released before terminalization; let the
            # admitted writer finish so the defect is observed, not raced.
            observed["claim"] = _await_marker(markers, child, "claimed", "rejected")
        observed["pre_terminal_tail"] = run_journal.read_journal_bounded(path).events[-1].sequence
        return real_terminalize(*args, **kwargs)

    child = subprocess.Popen(
        [sys.executable, str(child_script), str(markers), str(run_dir)],
        cwd=child_root,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    stdout = stderr = ""
    try:
        assert _await_marker(markers, child, "admitted") == ("admitted", {"workspace": None})
        snapshot.unlink()
        monkeypatch.setattr(run_checkpoint, "_restore_run_json_from_checkpoint", restore_at_publication)
        monkeypatch.setattr(runguard, "_recover_run_artifact", terminalize_at_boundary)
        rc = runs_cmd.recover(run_dir, cwd=repo)
        stdout, stderr = child.communicate(timeout=20)
        observed["child_rc"] = child.returncode
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)
    observed["child_reaped"] = child.returncode is not None
    monkeypatch.undo()

    terminal = json.loads(snapshot.read_bytes())
    events = run_journal.read_journal_bounded(path).events
    tail = events[-1]
    outcome = next(
        (
            (name, json.loads(markers.joinpath(name).read_text(encoding="utf-8")))
            for name in ("claimed", "rejected")
            if markers.joinpath(name).exists()
        ),
        None,
    )
    doctor_verdict = doctor._recovery_checkpoint_run_verdict(repo, run_dir)
    audit = run_audit.audit_run(run_dir)
    try:
        run_checkpoint.recover_from_checkpoint(run_dir, terminal)
        forensic = "covered"
    except run_checkpoint.CheckpointError as exc:
        forensic = exc.category
    evidence = {
        "rc": rc,
        "window": observed.get("window"),
        "terminal_lock": observed.get("terminal_lock"),
        "pre_terminal_tail": observed.get("pre_terminal_tail"),
        "writer_outcome": outcome,
        "terminal_status": terminal.get("status"),
        "terminal_cursor": (terminal.get("journal_last_sequence"), terminal.get("journal_last_event_digest")),
        "synchronized": (synchronized.sequence, synchronized.event_digest),
        "journal_tail": (tail.sequence, tail.event_digest, tail.event_type),
        "control_requested_events": sum(event.event_type == "control.requested" for event in events),
        "doctor_verdict": doctor_verdict,
        "audit_result": audit.result,
        "forensic_recovery": forensic,
        "lock_cleared": not lock.exists(),
        "child_rc": observed.get("child_rc"),
        "child_reaped": observed["child_reaped"],
    }
    with capsys.disabled():
        print("RESIDUAL-RACE-EVIDENCE " + json.dumps(evidence, sort_keys=True, default=str))

    contention = "flock-contended" if run_journal.fcntl is not None else "byte-lock-contended"
    assert evidence["child_rc"] == 0, f"child stdout:\n{stdout}\nstderr:\n{stderr}"
    assert evidence["child_reaped"]
    assert evidence["rc"] == 0
    assert evidence["lock_cleared"]
    assert evidence["window"] == ("blocked", contention)
    # The recovery still holds the journal lock when the terminal receipt is written.
    assert evidence["terminal_lock"] == contention
    assert evidence["pre_terminal_tail"] == synchronized.sequence
    assert evidence["writer_outcome"] == ("rejected", {"code": "control_run_not_live", "contention": contention})
    assert evidence["control_requested_events"] == 0
    assert evidence["journal_tail"][:2] == evidence["synchronized"]
    assert evidence["terminal_status"] not in runguard._NONTERMINAL_RUN_STATUSES
    assert evidence["terminal_cursor"] == evidence["journal_tail"][:2]
    assert evidence["doctor_verdict"] == ("ok", "stale-lock-recovery receipt matches projection reconstruction")
    assert evidence["forensic_recovery"] == "covered"


def test_runs_recovery_completes_with_two_matching_stale_claims(tmp_path):
    """Sibling recovery callbacks retain the outer journal mutation's reentry permission."""
    from brigade import doctor

    repo = tmp_path / "repo"
    run_dir = repo / ".brigade" / "runs" / RUN_ID
    run_dir.mkdir(parents=True)
    snapshot = run_dir / "run.json"
    base = {"schema": "brigade.run.v1", "status": "started", "lifecycle_journal_requested": True}
    snapshot.write_bytes(run_projector.encode_snapshot_bytes(base))
    with runguard.run_lock(repo, run_dir=run_dir):
        checkpoint = run_checkpoint.write_checkpoint(
            run_dir,
            run_projector.encode_snapshot_bytes({**base, "run_journal_authority_requested": True}),
            workspace=repo,
            paired_event_type=None,
            body_kind="base-stripped",
        )
    assert checkpoint is not None
    lock = runguard.lock_path(repo)
    claims = [lock.with_name(f".{lock.name}.{name}.stale") for name in ("first", "second")]
    for index, claim in enumerate(claims):
        claim.mkdir()
        dead_pid = 99999999
        (claim / "pid").write_text(f"{dead_pid}\n", encoding="utf-8")
        (claim / "owner.json").write_text(
            json.dumps(
                {
                    "schema": "brigade.run_lock.v1",
                    "owner_token": f"dead-owner-{index}",
                    "pid": dead_pid,
                    "run_dir": str(run_dir.resolve()),
                    "acquired_at": "2026-10-12T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
    snapshot.unlink()
    child_root = tmp_path / "child"
    child_root.mkdir()
    environment = {
        "HOME": str(child_root),
        "USERPROFILE": str(child_root),
        "TMP": str(child_root),
        "TEMP": str(child_root),
        "TMPDIR": str(child_root),
        "PATH": os.defpath,
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONIOENCODING": "utf-8",
        "BRIGADE_HOME": str(child_root / "operator" / ".brigade"),
        "BRIGADE_USER_DIR": str(child_root / "operator" / ".brigade"),
        "BRIGADE_NO_UPDATE_CHECK": "1",
    }
    if os.name == "nt":
        for key in ("SYSTEMROOT", "WINDIR"):
            if key in os.environ:
                environment[key] = os.environ[key]
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; from pathlib import Path; from brigade import runs_cmd; "
            "sys.exit(runs_cmd.recover(Path(sys.argv[1]), cwd=Path(sys.argv[2])))",
            str(run_dir),
            str(repo),
        ],
        cwd=child_root,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        try:
            stdout, stderr = child.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            child.kill()
            stdout, stderr = child.communicate(timeout=5)
            pytest.fail(f"recovery timed out with two matching stale claims\nstdout:\n{stdout}\nstderr:\n{stderr}")
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)
    assert child.returncode == 0, f"recovery stdout:\n{stdout}\nstderr:\n{stderr}"
    assert not lock.exists()
    assert all(not claim.exists() for claim in claims)
    assert not list(lock.parent.glob(f".{lock.name}.*.stale"))
    terminal = json.loads(snapshot.read_bytes())
    assert terminal["status"] == "failed"
    assert terminal["failure_phase"] == "stale-lock-recovery"
    tail = run_journal.read_journal_bounded(run_dir / "events" / "lifecycle.jsonl").events[-1]
    assert (terminal["journal_last_sequence"], terminal["journal_last_event_digest"]) == (
        tail.sequence,
        tail.event_digest,
    )
    assert doctor._recovery_checkpoint_run_verdict(repo, run_dir) == (
        "ok",
        "stale-lock-recovery receipt matches projection reconstruction",
    )


@pytest.mark.parametrize("operation", ["append-sync", "append-close", "replay-close"])
def test_complete_append_or_replay_close_failure_keeps_indeterminate_outcome(tmp_path, monkeypatch, operation):
    path = tmp_path / "events" / "lifecycle.jsonl"
    original = _append(path)
    journal_inode = path.stat()
    real_close, real_fsync = os.close, os.fsync

    def is_journal(fd):
        info = os.fstat(fd)
        return (info.st_dev, info.st_ino) == (journal_inode.st_dev, journal_inode.st_ino)

    def fail_close(fd):
        journal = is_journal(fd)
        real_close(fd)
        if journal:
            raise OSError(errno.EIO, "injected journal close failure")

    def fail_sync(fd):
        if is_journal(fd):
            raise OSError(errno.EIO, "injected journal sync failure")
        return real_fsync(fd)

    with monkeypatch.context() as patch:
        # The tail reader closes a read-only journal descriptor first. Inject
        # only after that real read, at the append/replay sync boundary.
        def at_sync(fd):
            patch.setattr(os, "close", fail_close)
            return fail_sync(fd) if operation == "append-sync" else real_fsync(fd)

        patch.setattr(os, "fsync", at_sync)
        with pytest.raises((OSError, run_journal.RunJournalError)) as failure_info:
            if operation == "replay-close":
                _append(path)
            else:
                _append(path, event_type="run.planning.started", payload={"detail": "planning"}, key="plan", previous=1)
    assert getattr(failure_info.value, "outcome", None) == "indeterminate"
    assert (
        "durability barrier failed" if operation == "append-sync" else "close failed"
    ) in failure_info.value.diagnostic
    report = run_journal.read_journal_bounded(path)
    assert report.chain_errors == [] and report.partial_tail is None
    assert report.events[0] == original
    assert len(report.events) == (1 if operation == "replay-close" else 2)


@pytest.mark.parametrize("failure", ["file", "directory"])
def test_fsync_error_retry_requires_new_sync_without_second_append(tmp_path, monkeypatch, failure):
    """Visible bytes need a fresh acknowledgment barrier, without rollback."""
    path = tmp_path / "events" / "lifecycle.jsonl"
    _append(path)
    before = path.read_bytes()

    def fail_sync(*args):
        raise OSError(errno.EIO, "injected journal sync failure")

    with monkeypatch.context() as patch:
        if failure == "file":
            patch.setattr(os, "fsync", fail_sync)
            error = OSError
        else:
            patch.setattr(run_journal, "_fsync_directory", fail_sync)
            error = (OSError, run_journal.RunJournalError)
        with pytest.raises(error):
            _append(path, event_type="run.planning.started", payload={"detail": "planning"}, key="plan", previous=1)

        uncertain = path.read_bytes()
        assert uncertain != before
        report = run_journal.read_journal_bounded(path)
        assert report.chain_errors == [] and report.partial_tail is None
        assert len(report.events) == 2
        for _ in range(2):
            with pytest.raises(run_journal.RunJournalError):
                _append(path, event_type="run.planning.started", payload={"detail": "planning"}, key="plan", previous=1)
            assert path.read_bytes() == uncertain

    syncs = []
    real_fsync = os.fsync

    def track_sync(fd):
        info = os.fstat(fd)
        syncs.append("file" if info.st_ino == path.stat().st_ino else "directory")
        return real_fsync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", track_sync)
        replay = _append(
            path, event_type="run.planning.started", payload={"detail": "planning"}, key="plan", previous=1
        )
    assert syncs == (["file", "directory"] if os.name == "posix" else ["file"])
    assert replay.to_dict() == report.events[-1].to_dict()
    assert path.read_bytes() == uncertain


def test_creation_directory_sync_failure_cannot_be_bypassed_by_new_append(tmp_path, monkeypatch):
    path = tmp_path / "events" / "lifecycle.jsonl"

    def fail_directory(directory):
        raise run_journal.RunJournalError("injected creation directory sync failure")

    with monkeypatch.context() as patch:
        patch.setattr(run_journal, "_fsync_directory", fail_directory)
        with pytest.raises(run_journal.RunJournalError):
            _append(path)
        assert path.read_bytes() == b""
        with pytest.raises(run_journal.RunJournalError):
            _append(path)
        visible = run_journal.read_journal_bounded(path).events
        assert len(visible) == 1
    before = path.read_bytes()
    assert _append(path) == visible[0]
    assert path.read_bytes() == before


@pytest.mark.parametrize("api", ["generic", "status", "checkpoint"])
def test_owner_replay_sync_failure_is_bounded_and_recovery_keeps_original_event(tmp_path, monkeypatch, api):
    repo = tmp_path / "repo"
    run_dir = repo / ".brigade" / "runs" / RUN_ID
    run_dir.mkdir(parents=True)
    snapshot = {"status": "started", "lock_workspace": str(repo), "lifecycle_journal_requested": True}
    snapshot_path = run_dir / "run.json"
    snapshot_path.write_text(json.dumps(snapshot))
    path = run_dir / "events" / "lifecycle.jsonl"

    def record():
        if api == "generic":
            return run_lifecycle.record_lifecycle_event(
                run_dir,
                event_type="approval.requested",
                payload={"approval_id": "approval-1", "source": "daily", "contract_fingerprint": "contract-1"},
                idempotency_key="approval:requested:stable",
                workspace=repo,
            )
        if api == "status":
            return run_lifecycle.record_lifecycle_transition(
                run_dir,
                status="planning",
                workspace=repo,
                incoming_snapshot={"detail": "changed refresh detail"},
            )
        return run_checkpoint.write_checkpoint(
            run_dir,
            json.dumps(snapshot).encode(),
            workspace=repo,
            paired_event_type="run.planning.started",
        )

    with runguard.run_lock(repo, run_dir=run_dir):
        run_lifecycle.prepare_lifecycle_journal(run_dir, workspace=repo)
        original = record()
        assert original is not None
        if api == "status":
            snapshot_path.write_text(json.dumps({**snapshot, "status": "planning"}))
        before = path.read_bytes()
        journal_inode = path.stat()
        real_fsync = os.fsync

        def fail_journal_sync(fd):
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) == (journal_inode.st_dev, journal_inode.st_ino):
                raise OSError(errno.EIO, "injected owner replay fsync failure")
            return real_fsync(fd)

        with monkeypatch.context() as patch:
            patch.setattr(os, "fsync", fail_journal_sync)
            with pytest.raises(run_lifecycle.LifecycleJournalError):
                record()
        assert path.read_bytes() == before
        assert record().to_dict() == original.to_dict()
        assert path.read_bytes() == before


def test_read_only_lookup_and_journal_read_do_not_sync(tmp_path, monkeypatch):
    path = tmp_path / "events" / "lifecycle.jsonl"
    original = _append(path)
    before = path.read_bytes()

    def forbidden_sync(*args):
        raise AssertionError("read-only API attempted sync")

    monkeypatch.setattr(os, "fsync", forbidden_sync)
    monkeypatch.setattr(run_journal, "_fsync_directory", forbidden_sync)
    assert (
        run_journal.lookup_idempotent_event(
            path,
            event_type="run.created",
            payload={"status": "started"},
            idempotency_key="create",
        )
        == original
    )
    assert run_journal.read_journal_bounded(path).events == [original]
    assert path.read_bytes() == before


# Runs the real execute_control_request. The synthetic send callback marks
# "sending" after the real control.requested claim is durable, then holds on a
# file barrier until the parent has recovered and terminalized the run.
_DELAYED_OUTCOME_CHILD = r"""
import json
import sys
import time
from pathlib import Path

from brigade import fleet_client, run_control_journal, run_journal

fleet_client.report_journal_event = lambda *args, **kwargs: False
markers = Path(sys.argv[1])
run_dir = Path(sys.argv[2])
outcome = sys.argv[3]
journal = run_control_journal._journal_path(run_dir)

def mark(name, value):
    temporary = markers / (name + ".tmp")
    temporary.write_text(json.dumps(value), encoding="utf-8")
    temporary.replace(markers / name)

def wait_for(name):
    deadline = time.monotonic() + 30
    while not (markers / name).exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("child barrier: " + name)
        time.sleep(0.01)

sends = []

def send(payload):
    sends.append(dict(payload))
    tail = run_journal.read_journal_bounded(journal).events[-1]
    mark("sending", {"payload": payload, "tail": [tail.sequence, tail.event_digest, tail.event_type]})
    wait_for("release")
    if outcome == "observed":
        return {"ok": True, "op": payload["op"], "interrupted": 1}
    if outcome == "rejected":
        return {"ok": False, "error": "RAW-REJECT-DETAIL"}
    raise ConnectionError("RAW-LOST-ACK-DETAIL")

try:
    result = run_control_journal.execute_control_request(
        run_dir, op="interrupt", request_id="delayed", send=send,
    )
except run_control_journal.ControlJournalError as exc:
    mark("finished", {
        "error": exc.code,
        "type": type(exc).__name__,
        "outcome": getattr(exc, "outcome", None),
        "diagnostic": exc.diagnostic,
        "sends": len(sends),
    })
else:
    terminal = result.terminal_event
    mark("finished", {
        "ok": result.response.get("ok"),
        "error_class": result.response.get("error_class"),
        "terminal": None if terminal is None else [terminal.sequence, terminal.event_type],
        "requested": None if result.requested_event is None else result.requested_event.sequence,
        "sends": len(sends),
    })
"""


@pytest.mark.parametrize(
    ("outcome", "observation"),
    [("observed", "acknowledged"), ("rejected", "negative response"), ("failed", "unknown")],
)
def test_delayed_control_outcome_cannot_append_past_recovered_terminal_cursor(
    tmp_path, monkeypatch, capsys, outcome, observation
):
    """A durable, checkpoint-covered request whose send returns after recovery.

    PR #1681 review I1. ``control.requested`` is claimed by the real
    ``execute_control_request`` and covered by a real latest checkpoint before
    the real ``brigade runs recover`` terminalizes the run. The held send then
    returns an acknowledgment (``observed``), a negative response
    (``rejected``) or raises a lost-ack transport exception (``failed``), and
    the real ``_append_control_event`` runs.

    Healthy invariant: the delayed outcome never advances the journal past the
    recovered terminal cursor, the transport is sent once, and doctor stays ok.
    The refusal is a clean ``control_outcome_unrecorded`` error naming only the
    observed response class; the durable request stays indeterminate.

    Limits: the journal-only compatibility preflight (no ``lock_workspace``)
    admits the request; the stale run lock names a nonexistent pid, so owner
    death is not exercised; the transport is a synthetic held callback.
    """
    from brigade import doctor, runs_cmd

    repo = tmp_path / "repo"
    run_dir = repo / ".brigade" / "runs" / RUN_ID
    run_dir.mkdir(parents=True)
    snapshot = run_dir / "run.json"
    base = {"schema": "brigade.run.v1", "status": "started", "lifecycle_journal_requested": True}
    snapshot.write_bytes(run_projector.encode_snapshot_bytes(base))
    authority = {**base, "run_journal_authority_requested": True}
    with runguard.run_lock(repo, run_dir=run_dir):
        first = run_checkpoint.write_checkpoint(
            run_dir,
            run_projector.encode_snapshot_bytes(authority),
            workspace=repo,
            paired_event_type=None,
            body_kind="base-stripped",
        )
    assert first is not None
    path = run_dir / "events" / "lifecycle.jsonl"
    child_root = tmp_path / "child"
    markers = child_root / "markers"
    markers.mkdir(parents=True)
    child_script = child_root / "child.py"
    child_script.write_text(_DELAYED_OUTCOME_CHILD, encoding="utf-8")
    environment = {
        "HOME": str(child_root),
        "USERPROFILE": str(child_root),
        "TMP": str(child_root),
        "TEMP": str(child_root),
        "TMPDIR": str(child_root),
        "PATH": os.defpath,
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONIOENCODING": "utf-8",
        "BRIGADE_HOME": str(child_root / "operator" / ".brigade"),
        "BRIGADE_USER_DIR": str(child_root / "operator" / ".brigade"),
        "BRIGADE_NO_UPDATE_CHECK": "1",
    }
    lock = runguard.lock_path(repo)
    evidence = {"outcome": outcome}
    fingerprint = run_control_journal.control_fingerprint_payload(op="interrupt", request_id="delayed")
    child = subprocess.Popen(
        [sys.executable, str(child_script), str(markers), str(run_dir), outcome],
        cwd=child_root,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    stdout = stderr = ""
    try:
        evidence["sending"] = _await_marker(markers, child, "sending")[1]
        requested = run_journal.read_journal_bounded(path).events[-1]
        evidence["requested"] = (requested.sequence, requested.event_type, requested.payload.get("request_id"))
        # A real latest checkpoint after the durable request covers it. A new
        # projector-known field gives it a distinct idempotency key.
        with runguard.run_lock(repo, run_dir=run_dir):
            covering = run_checkpoint.write_checkpoint(
                run_dir,
                run_projector.encode_snapshot_bytes({**authority, "task": "control-pending"}),
                workspace=repo,
                paired_event_type=None,
                body_kind="base-stripped",
            )
        evidence["covering_checkpoint"] = None if covering is None else (covering.sequence, covering.event_type)
        covered_events = run_journal.read_journal_bounded(path).events
        latest = run_checkpoint.latest_checkpoint_event(covered_events)
        try:
            run_checkpoint._verify_coverage(
                covered_events,
                latest,
                run_checkpoint._parse_checkpoint_object(run_checkpoint.validate_checkpoint(run_dir, latest)),
            )
            evidence["coverage"] = "covered"
        except run_checkpoint.CheckpointError as exc:
            evidence["coverage"] = exc.category
        # Recorded only: before recovery doctor compares the fixture's live
        # run.json with the projection, which is not a coverage verdict.
        evidence["doctor_covered"] = doctor._recovery_checkpoint_run_verdict(repo, run_dir)
        evidence["pending_state"] = run_control_journal.inspect_control_request(
            run_dir, request_id="delayed", fingerprint=fingerprint
        )[0]
        # A dead owner's lock for this run: the recorded pid is above any pid_max.
        lock.mkdir(parents=True)
        dead_pid = 99999999
        (lock / "pid").write_text(f"{dead_pid}\n", encoding="utf-8")
        (lock / "owner.json").write_text(
            json.dumps(
                {
                    "schema": "brigade.run_lock.v1",
                    "owner_token": "dead-owner",
                    "pid": dead_pid,
                    "run_dir": str(run_dir.resolve()),
                    "acquired_at": "2026-10-12T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        snapshot.unlink()
        evidence["rc"] = runs_cmd.recover(run_dir, cwd=repo)
        recovered = json.loads(snapshot.read_bytes())
        tail = run_journal.read_journal_bounded(path).events[-1]
        evidence["recovered_status"] = recovered.get("status")
        evidence["recovered_cursor"] = (
            recovered.get("journal_last_sequence"),
            recovered.get("journal_last_event_digest"),
        )
        evidence["recovered_tail"] = (tail.sequence, tail.event_digest, tail.event_type)
        evidence["doctor_recovered"] = doctor._recovery_checkpoint_run_verdict(repo, run_dir)
        evidence["lock_cleared"] = not lock.exists()
        # Release the held send only after real terminalization returned.
        markers.joinpath("release").touch()
        evidence["finished"] = _await_marker(markers, child, "finished")[1]
        stdout, stderr = child.communicate(timeout=20)
        evidence["child_rc"] = child.returncode
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)
    evidence["child_reaped"] = child.returncode is not None

    final = json.loads(snapshot.read_bytes())
    events = run_journal.read_journal_bounded(path).events
    tail = events[-1]
    evidence["final_cursor"] = (final.get("journal_last_sequence"), final.get("journal_last_event_digest"))
    evidence["final_status"] = final.get("status")
    evidence["final_tail"] = (tail.sequence, tail.event_digest, tail.event_type)
    evidence["doctor_final"] = doctor._recovery_checkpoint_run_verdict(repo, run_dir)
    try:
        run_checkpoint.recover_from_checkpoint(run_dir, final)
        evidence["forensic_recovery"] = "covered"
    except run_checkpoint.CheckpointError as exc:
        evidence["forensic_recovery"] = exc.category
    controls = [event for event in events if event.event_type.startswith("control.")]
    evidence["control_events"] = [
        (event.sequence, event.event_type, event.payload.get("request_id")) for event in controls
    ]
    evidence["final_state"] = run_control_journal.inspect_control_request(
        run_dir, request_id="delayed", fingerprint=fingerprint
    )[0]
    resends = []
    try:
        run_control_journal.execute_control_request(
            run_dir, op="interrupt", request_id="delayed", send=lambda payload: resends.append(payload) or {"ok": True}
        )
        evidence["retry"] = "accepted"
    except run_control_journal.ControlJournalError as exc:
        evidence["retry"] = exc.code
    evidence["retry_sends"] = len(resends)
    with capsys.disabled():
        print("DELAYED-OUTCOME-EVIDENCE " + json.dumps(evidence, sort_keys=True, default=str))

    # Fixture preconditions: a durable request covered by a valid latest checkpoint.
    assert evidence["child_rc"] == 0, f"child stdout:\n{stdout}\nstderr:\n{stderr}"
    assert evidence["child_reaped"]
    assert evidence["requested"] == (first.sequence + 1, "control.requested", "delayed")
    assert evidence["sending"]["tail"][0] == first.sequence + 1
    assert evidence["covering_checkpoint"] == (first.sequence + 2, "run.snapshot.checkpointed")
    assert evidence["coverage"] == "covered"
    assert evidence["pending_state"] == "indeterminate"
    # Real recovery terminalized at the actual tail before the delayed outcome.
    assert evidence["rc"] == 0
    assert evidence["lock_cleared"]
    assert evidence["recovered_status"] not in runguard._NONTERMINAL_RUN_STATUSES
    assert evidence["recovered_cursor"] == evidence["recovered_tail"][:2]
    assert evidence["doctor_recovered"][0] == "ok", evidence["doctor_recovered"]
    assert evidence["finished"]["sends"] == 1
    assert evidence["retry_sends"] == 0
    # Healthy invariant (I1): the delayed outcome must not pass the terminal cursor.
    assert evidence["final_tail"][:2] == evidence["recovered_cursor"]
    assert evidence["final_cursor"] == evidence["final_tail"][:2]
    assert evidence["doctor_final"][0] == "ok", evidence["doctor_final"]
    assert evidence["forensic_recovery"] == "covered"
    # The refused outcome is known unappended, not known undelivered.
    finished = evidence["finished"]
    assert finished["error"] == "control_outcome_unrecorded"
    assert finished["type"] == "ControlJournalError"
    assert finished["outcome"] is None
    assert f"transport observation: {observation};" in finished["diagnostic"]
    assert "RAW-" not in finished["diagnostic"]
    assert evidence["control_events"] == [(first.sequence + 1, "control.requested", "delayed")]
    assert evidence["final_state"] == "indeterminate"
    assert evidence["retry"] == "control_run_not_live"


def test_terminal_run_keeps_existing_control_receipt_and_refuses_new_outcome(tmp_path, monkeypatch):
    """Idempotent receipts keep replay semantics after terminalization; new outcomes do not append."""
    run_dir = tmp_path / RUN_ID
    path = run_dir / "events" / "lifecycle.jsonl"
    _append(path)
    receipt = run_control_journal.execute_control_request(
        run_dir, op="interrupt", request_id="done", send=lambda payload: {"ok": True}
    ).terminal_event
    terminal = {"schema": "brigade.run.v1", "status": "failed", "finished_at": "2026-10-12T00:00:00+00:00"}
    (run_dir / "run.json").write_bytes(run_projector.encode_snapshot_bytes(terminal))
    before = path.read_bytes()
    syncs = []
    real_sync = run_journal._sync_existing_journal
    monkeypatch.setattr(
        run_journal, "_sync_existing_journal", lambda journal: syncs.append(journal) or real_sync(journal)
    )

    def append(**overrides):
        arguments = {
            "run_dir": run_dir,
            "workspace": None,
            "run_id": RUN_ID,
            "event_type": receipt.event_type,
            "payload": dict(receipt.payload),
            "idempotency_key": receipt.idempotency_key,
            **overrides,
        }
        return run_control_journal._append_control_event(path, **arguments)

    def refusal(**overrides):
        with pytest.raises(run_control_journal.ControlJournalError) as info:
            append(**overrides)
        return info.value

    assert append() == receipt
    assert len(syncs) == 1
    conflict = refusal(payload={**receipt.payload, "detail": "interrupted=9"})
    assert conflict.code == "control_fingerprint_conflict"

    def fail_sync(journal):
        raise run_journal.IndeterminateOutcomeError("injected replay sync failure")

    monkeypatch.setattr(run_journal, "_sync_existing_journal", fail_sync)
    uncertain = refusal()
    assert uncertain.code == "control_append_failed"
    assert uncertain.outcome == "indeterminate"
    monkeypatch.setattr(run_journal, "_sync_existing_journal", real_sync)
    late = refusal(
        event_type=run_control_journal.CONTROL_FAILED,
        payload={
            "op": "interrupt",
            "request_id": "late",
            "error_class": "transport_rejected",
            "detail_digest": "0" * 64,
        },
        idempotency_key=run_control_journal.failed_idempotency_key("late"),
    )
    assert late.code == "control_outcome_unrecorded"
    assert type(late) is run_control_journal.ControlJournalError
    assert "transport observation: negative response;" in late.diagnostic
    assert path.read_bytes() == before
