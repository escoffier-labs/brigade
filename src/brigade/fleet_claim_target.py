"""Repo claim key derivation for hub-arbitrated claims (#1639).

The key must name the repository, never the directory it happens to sit in.
A workspace that carries its own ``.brigade/node.toml`` keeps its directory
name. The per-user machine identity at ``~/.brigade/node.toml`` is not a
repo workspace, so it is never a key: every repo under that home would
otherwise share one claim, and the same repo on two machines would get two
keys whenever the home names differ.

Otherwise the key comes from the git repository: the normalized ``origin``
remote (``insteadOf`` rewrites applied, never the checkout name while a
remote exists), else the git toplevel name when no ``origin`` is configured. A
github.com remote becomes ``owner/repo`` so it matches the ``owner/repo#N``
issue-scoped claims. Any other host keeps its host
(``gitlab.com/group/subgroup/repo``) so equal paths on different hosts never
collide. A linked worktree adds ``@<worktree name>`` so parallel workers on
one repo are not serialized by a single claim, while the main checkout keeps
the bare repo key that every machine agrees on.

A directory that is not a git repo falls back to its own name. Anything else
that stops git from answering (a timeout, an I/O error, an unreadable remote,
or no git binary inside a checkout) is not "not a repo": it raises
``ClaimTargetError`` instead of quietly producing a different key that would
address the wrong claim. Nothing here is cached, so a failure never outlives
the call that saw it.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path


class ClaimTargetError(RuntimeError):
    """The claim key could not be determined right now. Never a fallback key."""


def _fail(cwd: Path, detail: object) -> ClaimTargetError:
    return ClaimTargetError(f"cannot determine the claim target for {cwd}: git failed ({detail})")


def _ceiling_directories() -> set[Path]:
    """``GIT_CEILING_DIRECTORIES`` with git's semantics.

    Absolute entries only. Entries before the first empty entry have their
    symlinks resolved. An empty entry tells git the entries after it are
    already real paths, so they are used as written; it does not disable them.
    """
    ceilings: set[Path] = set()
    resolve = True
    for entry in os.environ.get("GIT_CEILING_DIRECTORIES", "").split(os.pathsep):
        if not entry:
            resolve = False
            continue
        path = Path(entry)
        if not path.is_absolute():
            continue
        if resolve:
            try:
                path = path.resolve()
            except OSError:
                pass
        ceilings.add(path)
    return ceilings


def _device(path: Path) -> int | None:
    try:
        return os.stat(path).st_dev
    except OSError:
        return None


def _inside_checkout(cwd: Path) -> bool:
    """Whether git would find a ``.git`` file or directory at or above ``cwd``.

    The walk stops where git's own discovery stops: it never ascends into a
    ``GIT_CEILING_DIRECTORIES`` entry (the starting directory is always
    examined) and, unless ``GIT_DISCOVERY_ACROSS_FILESYSTEM`` is set, never
    crosses onto another filesystem.
    """
    try:
        cwd = cwd.resolve()
    except OSError:
        pass
    ceilings = _ceiling_directories()
    across = os.environ.get("GIT_DISCOVERY_ACROSS_FILESYSTEM", "").lower() in {"1", "true", "yes", "on"}
    start_device = _device(cwd)
    for candidate in (cwd, *cwd.parents):
        if candidate != cwd:
            if candidate in ceilings:
                return False
            if not across and start_device is not None and _device(candidate) != start_device:
                return False
        if (candidate / ".git").exists():
            return True
    return False


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run git, turning every failure to run it into ``ClaimTargetError``."""
    try:
        return subprocess.run(
            ["git", *args],
            shell=False,
            timeout=5,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise _fail(cwd, exc) from exc


def _git_layout(cwd: Path) -> tuple[Path, bool] | None:
    """``(toplevel, is_linked_worktree)``, or ``None`` when not in a git repo."""
    try:
        completed = _git(cwd, "rev-parse", "--show-toplevel", "--git-dir", "--git-common-dir")
    except ClaimTargetError as exc:
        # No git binary: only a directory with no checkout above it is safe to
        # call "not a repo". Inside one, another machine would get the repo key.
        if isinstance(exc.__cause__, FileNotFoundError) and not _inside_checkout(cwd):
            return None
        raise
    if completed.returncode != 0:
        if "not a git repository" in completed.stderr:
            return None
        raise _fail(cwd, completed.stderr.strip() or f"exit {completed.returncode}")
    lines = completed.stdout.splitlines()
    if len(lines) != 3:
        raise _fail(cwd, "unexpected output")
    toplevel, git_dir, common_dir = (Path(line) for line in lines)
    try:
        linked = (cwd / git_dir).resolve() != (cwd / common_dir).resolve()
    except OSError:
        linked = False
    return toplevel, linked


def _origin_remote(toplevel: Path) -> str | None:
    """The ``origin`` URL with ``insteadOf`` rewrites applied, or ``None`` without an origin.

    ``git ls-remote --get-url origin`` works on every git since 1.7.5, applies
    the rewrites (so ``gh:acme/repo`` and the https URL it stands for are one
    key on every machine), and exits 0 even when there is no such remote, in
    which case it echoes the name back. Any nonzero exit raises, unlike
    ``fleet_session_presence``, which maps every failure to "no remote".
    """
    completed = _git(toplevel, "ls-remote", "--get-url", "origin")
    if completed.returncode != 0:
        raise _fail(toplevel, completed.stderr.strip() or f"exit {completed.returncode}")
    url = completed.stdout.strip()
    return None if url in ("", "origin") else url


def _normalized_remote(url: str, cwd: Path) -> str:
    """A ``host/path`` identity for a URL the strict parser refuses.

    Scheme, credentials and a trailing ``.git`` are dropped and the host is
    lowercased, so the same configured remote gives the same key on every
    machine. A remote that exists never falls back to the checkout name.
    """
    from .fleet_session_presence import validate_repo_identity

    value = re.sub(r"^[A-Za-z][A-Za-z0-9+.-]*://", "", url.strip())
    authority, separator, rest = value.partition("/")
    authority = authority.rsplit("@", 1)[-1].lower()
    path = rest.strip("/")
    if path.lower().endswith(".git"):
        path = path[:-4].rstrip("/")
    identity = f"{authority}/{path}" if separator else authority
    if validate_repo_identity(identity) is None or not identity.strip("/"):
        raise ClaimTargetError(f"cannot determine the claim target for {cwd}: the origin remote is not usable")
    return identity


def _remote_key(identity: str) -> str:
    """``host/path`` identity to a claim key: only github.com drops its host."""
    host, _, path = identity.partition("/")
    return path if host == "github.com" and path else identity


def git_claim_key(start: Path) -> str | None:
    """Repo-derived claim key for ``start``, or ``None`` outside a git repo."""
    from .fleet_session_presence import _parse_remote

    cwd = start if start.is_dir() else start.parent
    layout = _git_layout(cwd)
    if layout is None:
        return None
    toplevel, linked = layout
    remote = _origin_remote(toplevel)
    if remote is None:
        return toplevel.name or None
    key = _remote_key(_parse_remote(remote) or _normalized_remote(remote, cwd))
    return f"{key}@{toplevel.name}" if linked and toplevel.name else key


def resolve(base_path: Path | None) -> str:
    """Stable cross-machine claim key for the repo at ``base_path``.

    Raises ``ClaimTargetError`` when git fails transiently.
    """
    from . import fleet_client

    start = Path(base_path) if base_path is not None else Path.cwd()
    workspace = fleet_client.find_workspace_for_path(start)
    if workspace is not None:
        try:
            is_home = workspace.resolve() == fleet_client.home_identity_target().resolve()
        except OSError:
            is_home = False
        if not is_home:
            return workspace.name
    key = git_claim_key(start.expanduser())
    if key:
        return key
    try:
        resolved = start.expanduser().resolve()
    except OSError:
        resolved = start.expanduser()
    return resolved.name or "unknown"
