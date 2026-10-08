"""Repo claim key derivation for hub-arbitrated claims (#1639).

The key must name the repository, never the directory it happens to sit in.
A workspace that carries its own ``.brigade/node.toml`` keeps its directory
name. The per-user machine identity at ``~/.brigade/node.toml`` is not a
repo workspace, so it is never a key: every repo under that home would
otherwise share one claim, and the same repo on two machines would get two
keys whenever the home names differ.

Otherwise the key comes from the git repository: the normalized ``origin``
remote as ``owner/repo`` (host dropped, so it matches the ``owner/repo#N``
issue-scoped claims), else the git toplevel name. A linked worktree adds
``@<worktree name>`` so parallel workers on one repo are not serialized by
a single claim, while the main checkout keeps the bare ``owner/repo`` key
that every machine agrees on.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def _git_layout(cwd: Path) -> tuple[Path, bool] | None:
    """``(toplevel, is_linked_worktree)`` for the repo containing ``cwd``."""
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
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    lines = completed.stdout.splitlines()
    if completed.returncode != 0 or len(lines) != 3:
        return None
    toplevel, git_dir, common_dir = (Path(line) for line in lines)
    try:
        linked = (cwd / git_dir).resolve() != (cwd / common_dir).resolve()
    except OSError:
        linked = False
    return toplevel, linked


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
    except (subprocess.SubprocessError, OSError):
        return toplevel.name or None
    if identity.scope != "fleet":
        return toplevel.name or None
    key = identity.value.split("/", 1)[-1]
    return f"{key}@{toplevel.name}" if linked and toplevel.name else key


def resolve(base_path: Path | None) -> str:
    """Stable cross-machine claim key for the repo at ``base_path``."""
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
