"""Repo claim key derivation for hub-arbitrated claims (#1639).

The key must name the repository, never the directory it happens to sit in.
A workspace that carries its own ``.brigade/node.toml`` keeps its directory
name. The per-user machine identity at ``~/.brigade/node.toml`` is not a
repo workspace, so it is never a key: every repo under that home would
otherwise share one claim, and the same repo on two machines would get two
keys whenever the home names differ.

Otherwise the key comes from the git repository: the normalized ``origin``
remote, else the git toplevel name. A github.com remote becomes
``owner/repo`` so it matches the ``owner/repo#N`` issue-scoped claims. Any
other host keeps its host (``gitlab.com/group/subgroup/repo``) so equal
paths on different hosts never collide. A linked worktree adds
``@<worktree name>`` so parallel workers on one repo are not serialized by
a single claim, while the main checkout keeps the bare repo key that every
machine agrees on.

A directory that is not a git repo falls back to its own name. A transient
git failure (timeout, I/O error, an unreadable repo) is not "not a repo":
it raises ``ClaimTargetError`` instead of quietly producing a different key
that would address the wrong claim mid-lease.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


class ClaimTargetError(RuntimeError):
    """The claim key could not be determined right now. Never a fallback key."""


def _git_layout(cwd: Path) -> tuple[Path, bool] | None:
    """``(toplevel, is_linked_worktree)``, or ``None`` when not in a git repo."""
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--show-toplevel", "--git-dir", "--git-common-dir"],
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
    except FileNotFoundError:
        return None  # no git binary: nothing to derive a repo key from, consistently
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise ClaimTargetError(f"cannot determine the claim target for {cwd}: git failed ({exc})") from exc
    if completed.returncode != 0:
        if "not a git repository" in completed.stderr:
            return None
        detail = completed.stderr.strip() or f"exit {completed.returncode}"
        raise ClaimTargetError(f"cannot determine the claim target for {cwd}: git failed ({detail})")
    lines = completed.stdout.splitlines()
    if len(lines) != 3:
        raise ClaimTargetError(f"cannot determine the claim target for {cwd}: unexpected git output")
    toplevel, git_dir, common_dir = (Path(line) for line in lines)
    try:
        linked = (cwd / git_dir).resolve() != (cwd / common_dir).resolve()
    except OSError:
        linked = False
    return toplevel, linked


def _remote_key(identity: str) -> str:
    """``host/path`` identity to a claim key: only github.com drops its host."""
    host, _, path = identity.partition("/")
    return path if host == "github.com" and path else identity


def git_claim_key(start: Path) -> str | None:
    """Repo-derived claim key for ``start``, or ``None`` outside a git repo."""
    from .fleet_session_presence import repository_identity

    cwd = start if start.is_dir() else start.parent
    layout = _git_layout(cwd)
    if layout is None:
        return None
    toplevel, linked = layout
    try:
        identity = repository_identity(toplevel)
    except (subprocess.SubprocessError, OSError) as exc:
        raise ClaimTargetError(f"cannot determine the claim target for {cwd}: git failed ({exc})") from exc
    if identity.scope != "fleet":
        return toplevel.name or None
    key = _remote_key(identity.value)
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
