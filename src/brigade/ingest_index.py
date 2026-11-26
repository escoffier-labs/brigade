"""Reuse stable Git path listings during a handoff ingest run."""

from __future__ import annotations

import os
import stat
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

_IndexStamp = tuple[int, int, int, int, int, int]
_Listing = dict[str, set[str]]
_listings: ContextVar[dict[Path, tuple[_IndexStamp, _Listing | None]] | None] = ContextVar(
    "ingest_index_listings", default=None
)


@contextmanager
def cache_listings() -> Iterator[None]:
    token = _listings.set({})
    try:
        yield
    finally:
        _listings.reset(token)


def _stamp(index: Path) -> _IndexStamp | None:
    try:
        info = index.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or not info.st_ino:
        return None
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _git(command: list[str], env: dict[str, str], *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run([*command, *args], capture_output=True, env=env, timeout=30, check=False)


def _group_paths(output: bytes) -> _Listing:
    parents: _Listing = {}
    for path in output.decode("utf-8", "surrogateescape").split("\0"):
        if path:
            entry = Path(path)
            # Keep case variants distinct even where Path equality folds case.
            parents.setdefault(str(entry.parent), set()).add(entry.name)
    return parents


def _full_listing(command: list[str], env: dict[str, str]) -> _Listing:
    result = _git(command, env, "ls-files", "-z")
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip().splitlines()
        raise OSError(detail[0] if detail else f"git exited {result.returncode}")
    return _group_paths(result.stdout)


def _cacheable_listing(command: list[str], env: dict[str, str]) -> _Listing | None:
    # A split index depends on a separate shared file. A sparse index can
    # depend on tree objects. Keep asking Git for a fresh expanded listing
    # in both cases, and on Git versions without these options.
    shared = _git(command, env, "rev-parse", "--shared-index-path")
    if shared.returncode != 0 or shared.stdout:
        return None
    result = _git(command, env, "ls-files", "--sparse", "-z")
    if result.returncode != 0 or b"/\0" in result.stdout:
        return None
    return _group_paths(result.stdout)


def paths(command: list[str], env: dict[str, str]) -> Iterator[tuple[str, set[str]]]:
    cache = _listings.get()
    if cache is None:
        yield from _full_listing(command, env).items()
        return

    # Resolve the marker at every check. Pin that Git directory for a cache
    # fill so a redirected gitfile cannot pair another index with this stamp.
    result = _git(command, env, "rev-parse", "--absolute-git-dir")
    gitdir = Path(os.fsdecode(result.stdout.removesuffix(b"\n")))
    index = gitdir / "index"  # The caller removes Git location overrides.
    stamp = _stamp(index) if result.returncode == 0 and gitdir.is_absolute() else None
    if stamp is None:
        yield from _full_listing(command, env).items()
        return

    previous = cache.get(index)
    if previous is not None and previous[0] == stamp:
        listing = previous[1]
    else:
        pinned = command.copy()
        pinned[pinned.index("--git-dir") + 1] = gitdir.as_posix()
        listing = _cacheable_listing(pinned, env)

    if listing is None:
        yield from _full_listing(command, env).items()
    else:
        yield from listing.items()
    # The consumer rechecks each indexed parent's identity during iteration.
    # Refuse the check if Git replaced the index while we read or used it.
    if _stamp(index) != stamp:
        raise OSError("git index changed during handoff tracking check")
    cache[index] = (stamp, listing)
