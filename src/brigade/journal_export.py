"""Export a complete, validated local lifecycle journal or a time-filtered subset."""

from __future__ import annotations

import os
import re
import stat
import sys
import tempfile
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path

from . import dirfd, run_checkpoint, run_dirfd, run_journal
from .run_events import canonical_bytes


def _cutoff(since: str | None) -> datetime | None:
    if since is None:
        return None
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", since):
            return datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        parsed = datetime.fromisoformat(since.replace("Z", "+00:00"))
        if parsed.tzinfo is not None and parsed.utcoffset() is not None:
            return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        pass
    raise ValueError("--since requires a valid YYYY-MM-DD date or timezone-aware ISO timestamp")


def _source_child_directory(parent: int, component: str) -> int:
    """Allow POSIX ancestor names without relaxing shared contained-name rules."""
    if os.name == "posix" and (":" in component or "\\" in component):
        if "/" in component or "\x00" in component:
            raise OSError("invalid source directory name")
        if not dirfd.posix_available():
            raise dirfd.unavailable("directory operations")
        return os.open(component, dirfd.directory_flags(), dir_fd=parent)
    return dirfd.open_child_directory(parent, component, writable=False)


def _source_metadata(info: os.stat_result) -> tuple[int, ...]:
    return info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _named_source_metadata(bound: run_dirfd.BoundRunDir) -> tuple[int, ...]:
    """Observe the currently named events directory from the held run root."""
    original = os.fstat(bound.dir_fd("events"))
    descriptor = dirfd.open_child_directory(bound.dir_fd(), "events", writable=False)
    try:
        current = os.fstat(descriptor)
        if (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino):
            raise OSError("journal events directory changed during export")
        return _source_metadata(dirfd.stat_child(descriptor, "lifecycle.jsonl"))
    finally:
        os.close(descriptor)


def _check_source(bound: run_dirfd.BoundRunDir, descriptor: int, metadata: tuple[int, ...]) -> None:
    if _source_metadata(os.fstat(descriptor)) != metadata or _named_source_metadata(bound) != metadata:
        raise OSError("journal source changed during export")


def _snapshot_report(bound: run_dirfd.BoundRunDir) -> run_journal.JournalReport:
    """Parse a private bounded copy of one held, unchanged source file."""
    before = bound.stat(("events",), "lifecycle.jsonl")
    if not stat.S_ISREG(before.st_mode):
        raise OSError("journal source must be a regular file without symlinks")
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    descriptor = bound.open_file(("events",), "lifecycle.jsonl", flags)
    # Unbuffered binary mode also prevents CRT newline translation on Windows.
    with os.fdopen(descriptor, "rb", buffering=0) as source:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise OSError("journal source must be a regular file without symlinks")
        if opened.st_size > run_checkpoint.MAX_JOURNAL_BYTES:
            raise ValueError("journal bound exceeded: above MAX_JOURNAL_BYTES")
        metadata = _source_metadata(opened)
        if metadata != _source_metadata(before):
            raise OSError("journal source changed before snapshot")
        with tempfile.TemporaryDirectory(prefix="brigade-journal-export-") as temporary:
            with tempfile.NamedTemporaryFile(dir=temporary, mode="wb", delete=False) as snapshot:
                remaining = opened.st_size
                while remaining:
                    chunk = source.read(min(65536, remaining))
                    if not chunk:
                        raise OSError("journal source changed during snapshot")
                    snapshot.write(chunk)
                    remaining -= len(chunk)
                if source.read(1):
                    raise OSError("journal source grew during snapshot")
            _check_source(bound, descriptor, metadata)
            # Validation uses only the private copy. Release the original so
            # writers can replace it on Windows while that copy is parsed.
            source.close()
            report = run_journal.read_journal_bounded(Path(snapshot.name))
            if _named_source_metadata(bound) != metadata:
                raise OSError("journal source changed during export")
            if opened.st_size > 0 and not report.events and not report.chain_errors and report.partial_tail is None:
                raise ValueError("nonempty journal validation snapshot disappeared or changed during read")
            return report


def _validated_events(run_dir: Path, run_id: str) -> list[run_journal.RunEvent]:
    """Hold every ancestor no-follow, then bind only the same verified run inode."""
    with ExitStack() as stack:
        descriptor = dirfd.open_directory_nofollow(Path(run_dir.anchor), writable=False)
        stack.callback(os.close, descriptor)
        descriptors = [descriptor]
        components: list[str] = []
        for component in run_dir.parts[1:]:
            if component == "..":
                # Every preceding component was opened no-follow, even if it
                # is later cancelled. Never skip a symlink in link/../target.
                if components:
                    components.pop()
                    descriptors.pop()
                continue
            descriptor = _source_child_directory(descriptors[-1], component)
            stack.callback(os.close, descriptor)
            descriptors.append(descriptor)
            components.append(component)
        run_dir = Path(run_dir.anchor).joinpath(*components)
        held = os.fstat(descriptors[-1])
        # The shared binding opens the named run directory. Compare it against
        # the held traversal before any journal read, so ancestor replacement
        # cannot authorize a different source through pathname re-resolution.
        with run_dirfd.bound_run_dir(run_dir) as bound:
            if bound.identity != (held.st_dev, held.st_ino):
                raise OSError("run directory changed during source traversal")
            report = _snapshot_report(bound)
            if not bound.still_bound():
                raise OSError("run directory changed during export")
            if report.chain_errors:
                raise ValueError(f"journal integrity failed: {report.chain_errors[0]}")
            if report.partial_tail is not None:
                raise ValueError("journal has a partial tail")
            if any(event.run_id != run_id for event in report.events):
                raise ValueError("journal event run id does not match requested run id")
            return report.events


def _expand_path(path: Path) -> Path:
    try:
        return path.expanduser()
    except RuntimeError as exc:
        raise ValueError(str(exc)) from exc


def export_journal(*, target: Path, run_id: str, out: str, since: str | None, format: str = "jsonl") -> int:
    """Validate the full original journal before creating any export output."""
    try:
        try:
            dirfd.validate_component(run_id)
        except OSError as exc:
            raise ValueError("run id must be an exact bare directory name") from exc
        if run_id == "latest":
            raise ValueError("run id must be exact, not 'latest'")
        if format != "jsonl":
            raise ValueError("journal export format must be jsonl")
        cutoff = _cutoff(since)
        # absolute() retains symlink components for the no-follow walk.
        run_dir = _expand_path(Path(target)).absolute() / ".brigade" / "runs" / run_id
        events = _validated_events(run_dir, run_id)
        data = b"".join(
            canonical_bytes(event.to_dict()) + b"\n"
            for event in events
            if cutoff is None or datetime.fromisoformat(event.recorded_at.replace("Z", "+00:00")) >= cutoff
        )
        if out == "-":
            # Binary stdout preserves UTF-8 and LF framing on Windows too.
            stream = getattr(sys.stdout, "buffer", None)
            if stream is None:
                sys.stdout.write(data.decode("utf-8"))
            else:
                stream.write(data)
            sys.stdout.flush()
        else:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            descriptor = dirfd.open_file_nofollow(_expand_path(Path(out)), flags, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
    except (OSError, ValueError, run_journal.RunJournalError) as exc:
        print(f"error: journal export: {exc}", file=sys.stderr)
        return 1
    return 0
