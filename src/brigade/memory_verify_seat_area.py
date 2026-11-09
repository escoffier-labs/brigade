"""Private work areas for verify seat calls.

A call has two owner-only directories with the same random `call-<hex>` name:

- a files directory under `.brigade/memory/verify/work/` in the verify
  folder, for the prompt and reply files, and
- a run directory under a system temporary root, holding the seat's empty
  working directory (`cwd/`) and its TMPDIR (`tmp/`).

The run directory sits outside the verify folder because a CLI that loads
instruction files from the parents of its working directory would otherwise
inherit the workspace's CLAUDE.md or AGENTS.md. The temporary root is
accepted only when no directory from `/` down to it holds a CLAUDE.md,
CLAUDE.local.md, AGENTS.md or `.claude` entry, and the per-target directory
under it is created, owner-checked and walked through held descriptors. With
no such root the call is refused with `verify_seat_not_isolated`.

Every file operation goes through a descriptor held by this module (the
`brigade.dirfd` facade). The only path strings handed out are the child's
working directory and TMPDIR, which `check_identity()` compares with the held
descriptors before and after each launch.

Both directories are removed when the area closes, after an error in the
caller's block too. A removal that fails is added as a note to the primary
exception, or raised as `verify_workdir_cleanup` when there is none. Leftovers
from a killed run are swept in batches the next time an area opens, and an
area is refused with `verify_workdir_stale` while one cannot be removed.
Callers hold the verify state lock, and the temporary directory is per user
and per verify folder, so no other verify process has a live call there.
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import stat
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import dirfd
from . import memory_proposal_io as io
from . import memory_verify_state as vstate

WORK_ROOT = f"{vstate.ROOT}/work"
CALL_DIR = re.compile(r"call-[0-9a-f]{32}")
# Names a seat CLI may load as instructions from any parent of its working directory.
INSTRUCTION_NAMES = ("CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", ".claude")
SWEEP_BATCH = 64
SWEEP_SCAN = 4096
SWEEP_MAX = 256
CLEANUP_NOTE = "verify_workdir_cleanup_failed: a verify call directory could not be removed"


def _temp_roots() -> list[str]:
    return list(dict.fromkeys([tempfile.gettempdir(), "/tmp", "/var/tmp"]))


# Injectable for tests: candidate temporary roots, in order of preference.
TEMP_ROOTS: Callable[[], list[str]] = _temp_roots


class Refused(ValueError):
    """A fixed code and reason, never formatted with paths or file text."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code


@dataclass
class Area:
    files_fd: int
    run_fd: int
    cwd_fd: int
    cwd: Path
    tmp: Path
    calls: int = 0

    def check_identity(self) -> bool:
        """True while the cwd path still names the held directory and that directory is empty."""
        try:
            live = os.lstat(self.cwd)
        except OSError:
            return False
        held = os.fstat(self.cwd_fd)
        if not stat.S_ISDIR(live.st_mode) or (live.st_dev, live.st_ino) != (held.st_dev, held.st_ino):
            return False
        return not dirfd.child_names(self.cwd_fd, 1)


def available() -> bool:
    """The area needs the POSIX descriptor facade, as the verify state file does."""
    return dirfd.posix_available() and dirfd.tree_removal_available()


def _clean(fd: int) -> bool:
    for name in INSTRUCTION_NAMES:
        try:
            dirfd.stat_child(fd, name)
        except FileNotFoundError:
            continue
        return False
    return True


def _private_child(parent: int, name: str) -> int:
    """Create or open an owner-only child directory and check its owner on the held descriptor."""
    with contextlib.suppress(FileExistsError):
        dirfd.mkdir_child(parent, name)
    fd = dirfd.open_child_directory(parent, name, writable=False)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise OSError("verify directory has another owner")
        os.fchmod(fd, 0o700)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _open_clean_root(candidate: str) -> int | None:
    """A held descriptor for `candidate` when no directory from / down to it holds instruction files."""
    real = os.path.realpath(candidate)
    if not os.path.isabs(real):
        return None
    fd = dirfd.open_directory_nofollow(Path("/"), writable=False)
    try:
        for part in PurePosixPath(real).parts[1:]:
            if not _clean(fd):
                return None
            child = dirfd.open_child_directory(fd, part, writable=False)
            os.close(fd)
            fd = child
        if not _clean(fd):
            return None
        held, fd = fd, -1
        return held
    except OSError:
        return None
    finally:
        if fd >= 0:
            os.close(fd)


def _run_root(target: Path) -> tuple[int, str]:
    """The per-user, per-verify-folder run root under the first clean temporary root."""
    owner = io.sha256(os.path.realpath(target).encode())[:16]
    name = f"brigade-verify-{os.getuid()}-{owner}"
    for candidate in TEMP_ROOTS():
        root_fd = _open_clean_root(candidate)
        if root_fd is None:
            continue
        try:
            fd = _private_child(root_fd, name)
        except OSError:
            continue
        finally:
            os.close(root_fd)
        if _clean(fd):
            return fd, os.path.join(os.path.realpath(candidate), name)
        os.close(fd)
    raise Refused("verify_seat_not_isolated", "no temporary directory is free of project instruction files above it")


def sweep(parent: int) -> None:
    """Remove leftover call directories in batches. Refuse while one cannot be removed."""
    removed = 0
    while True:
        names = [name for name in dirfd.child_names(parent, SWEEP_SCAN) if CALL_DIR.fullmatch(name)]
        if not names:
            return
        for name in names[:SWEEP_BATCH]:
            if removed >= SWEEP_MAX:
                raise Refused("verify_workdir_stale", "too many leftover verify call directories to remove")
            try:
                dirfd.remove_tree_child(parent, name)
            except OSError:
                raise Refused("verify_workdir_stale", "a leftover verify call directory cannot be removed") from None
            removed += 1


def _remove_all(name: str, parents: list[int]) -> bool:
    """Try every removal, even after one fails. True when all succeeded."""
    ok = True
    for parent in parents:
        try:
            dirfd.remove_tree_child(parent, name)
        except FileNotFoundError:
            continue
        except OSError:
            ok = False
    return ok


@contextlib.contextmanager
def open_area(target: Path) -> Iterator[Area]:
    """A fresh call area, removed on every exit path."""
    if not available():
        raise Refused("verify_workdir_unavailable", "verify needs safe directory descriptors for its work area")
    name = f"call-{secrets.token_hex(16)}"
    with contextlib.ExitStack() as parents:
        try:
            work_fd = parents.enter_context(io.directory(target / WORK_ROOT, create=True, private=True))
            run_root_fd, run_root = _run_root(target)
            parents.callback(os.close, run_root_fd)
            sweep(work_fd)
            sweep(run_root_fd)
        except OSError:
            raise Refused("verify_workdir", "verify could not open its private work area") from None
        created: list[int] = []
        held: list[int] = []
        try:
            try:
                dirfd.mkdir_child(work_fd, name)
                created.append(work_fd)
                held.append(_private_child(work_fd, name))
                dirfd.mkdir_child(run_root_fd, name)
                created.append(run_root_fd)
                run_fd = _private_child(run_root_fd, name)
                held.append(run_fd)
                dirfd.mkdir_child(run_fd, "cwd")
                dirfd.mkdir_child(run_fd, "tmp")
                cwd_fd = dirfd.open_child_directory(run_fd, "cwd", writable=False)
                held.append(cwd_fd)
            except OSError:
                raise Refused("verify_workdir", "verify could not create its private work area") from None
            base = Path(run_root) / name
            yield Area(files_fd=held[0], run_fd=run_fd, cwd_fd=cwd_fd, cwd=base / "cwd", tmp=base / "tmp")
        except BaseException as exc:
            for fd in held:
                os.close(fd)
            held = []
            if not _remove_all(name, created):
                exc.add_note(CLEANUP_NOTE)
            raise
        for fd in held:
            os.close(fd)
        if not _remove_all(name, created):
            raise Refused("verify_workdir_cleanup", "verify could not remove its private work area")


def write(parent: int, name: str, data: bytes) -> None:
    """Create an owner-only file under a held directory, refusing links and existing files."""
    fd = dirfd.open_child_file(parent, name, dirfd.file_flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL), 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
    finally:
        os.close(fd)
