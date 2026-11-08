"""Memory Handoff inboxes across git worktrees.

A handoff written into a linked worktree's inbox dies with that worktree, and
an ingester that only watches the main checkout never sees it. These helpers
let ingest find sibling worktree inboxes (bounded by `git worktree list`, never
a directory walk), let worktree removal deliver pending handoffs to the main
checkout first, and tell hooks which inbox outlives the worktree.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .selection import WRITER_INBOXES

# Upper bound on linked worktrees considered per repository. A repo with more
# than this many live worktrees is unusual; the rest are skipped, not walked.
MAX_WORKTREES = 256
_GIT_TIMEOUT_SECONDS = 20
# Git state that would point `git -C <dir>` at another repository.
GIT_LOCATION_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_CEILING_DIRECTORIES",
)
CLAUDE_INBOX = WRITER_INBOXES["claude"]


def _git(cwd: Path, *args: str) -> str | None:
    env = {k: v for k, v in os.environ.items() if k not in GIT_LOCATION_ENV}
    env["LC_ALL"] = "C"
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def main_checkout(path: Path) -> Path | None:
    """Return the main working tree when `path` sits in a linked worktree.

    Returns None for a main checkout, a bare repository, a path outside git,
    or a main checkout that no longer exists on disk.
    """
    out = _git(path, "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir")
    if out is None:
        return None
    lines = [line for line in out.splitlines() if line.strip()]
    if len(lines) != 2:
        return None
    git_dir, common_dir = (Path(line).resolve() for line in lines)
    if git_dir == common_dir or common_dir.name != ".git":
        return None
    main = common_dir.parent
    return main if main.is_dir() else None


def linked_worktrees(root: Path, *, limit: int = MAX_WORKTREES) -> list[Path]:
    """Existing working trees of `root`'s repository other than `root` itself."""
    out = _git(root, "worktree", "list", "--porcelain")
    if out is None:
        return []
    try:
        own = root.resolve()
    except OSError:
        return []
    found: list[Path] = []
    for line in out.splitlines():
        if not line.startswith("worktree "):
            continue
        candidate = Path(line[len("worktree ") :])
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved == own or not resolved.is_dir():
            continue
        found.append(resolved)
        if len(found) >= limit:
            break
    return found


def handoff_inbox(target: Path) -> Path:
    """Inbox a session at `target` should write to.

    A linked worktree points at the main checkout's inbox, which survives the
    worktree's deletion and is the inbox an ingester already watches.
    """
    main = main_checkout(target)
    return (main if main is not None else target) / CLAUDE_INBOX


def handoff_inboxes(target: Path) -> tuple[Path, ...]:
    """Every inbox a handoff for `target` may land in (own first)."""
    own = target / CLAUDE_INBOX
    preferred = handoff_inbox(target)
    return (own,) if preferred == own else (own, preferred)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _tracked_names(inbox: Path) -> set[str] | None:
    out = _git(inbox, "ls-files", "-z", "--", ".")
    if out is None:
        # Outside a work tree nothing is tracked. Any other failure is unknown.
        return set() if _git(inbox, "rev-parse", "--is-inside-work-tree") is None else None
    return {name for name in out.split("\0") if name and "/" not in name}


def pending_handoffs(inbox: Path) -> list[Path]:
    """Untracked handoff files waiting in `inbox` (never git-tracked copies).

    Raises OSError when git tracking cannot be checked, so a caller about to
    delete the inbox keeps it instead of guessing.
    """
    if not inbox.is_dir():
        return []
    tracked = _tracked_names(inbox)
    if tracked is None:
        raise OSError(f"git tracking check failed for {inbox}")
    pending: list[Path] = []
    for path in sorted(inbox.iterdir()):
        if path.suffix != ".md" or path.name == "TEMPLATE.md" or path.name.startswith("."):
            continue
        if path.is_symlink() or not path.is_file() or path.name in tracked:
            continue
        pending.append(path)
    return pending


@dataclass(frozen=True)
class Rescue:
    source: Path
    state: str  # delivered | duplicate | failed
    dest: Path | None = None
    reason: str = ""


def _known_digests(dest_inbox: Path) -> set[str]:
    known: set[str] = set()
    for folder in (dest_inbox, dest_inbox / "processed"):
        if not folder.is_dir():
            continue
        for path in folder.iterdir():
            if path.suffix != ".md" or path.is_symlink() or not path.is_file():
                continue
            try:
                known.add(digest(path.read_bytes()))
            except OSError:
                continue
    return known


def rescue_pending_handoffs(worktree: Path, dest_root: Path) -> list[Rescue]:
    """Move pending handoffs from `worktree`'s inboxes into `dest_root`'s.

    Content already present in the destination inbox or its processed archive
    counts as delivered once (dedupe by sha256). A name clash with different
    content gets a digest suffix. The source is removed only after the copy is
    verified, so a failure leaves the original in place.
    """
    results: list[Rescue] = []
    if worktree.resolve() == dest_root.resolve():
        return results
    for rel in dict.fromkeys(WRITER_INBOXES.values()):
        try:
            pending = pending_handoffs(worktree / rel)
        except OSError as exc:
            results.append(Rescue(worktree / rel, "failed", reason=f"{type(exc).__name__}: {exc}"))
            continue
        if not pending:
            continue
        dest_inbox = dest_root / rel
        known = _known_digests(dest_inbox)
        for source in pending:
            try:
                raw = source.read_bytes()
                sha = digest(raw)
                if sha in known:
                    source.unlink()
                    results.append(Rescue(source, "duplicate", reason="content already in destination"))
                    continue
                dest_inbox.mkdir(parents=True, exist_ok=True)
                dest = dest_inbox / source.name
                if dest.exists():
                    dest = dest_inbox / f"{source.stem}-{sha[:12]}{source.suffix}"
                with dest.open("xb") as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                if digest(dest.read_bytes()) != sha:
                    raise OSError(f"copy verification failed for {dest}")
                source.unlink()
                known.add(sha)
                results.append(Rescue(source, "delivered", dest=dest))
            except OSError as exc:
                results.append(Rescue(source, "failed", reason=f"{type(exc).__name__}: {exc}"))
    return results


def is_handoff_path(target: Path, path: Path) -> bool:
    """True when resolved `path` sits in an inbox a session at `target` may use."""
    own = (target / CLAUDE_INBOX).resolve()
    if path.is_relative_to(own):
        return True
    preferred = handoff_inbox(target).resolve()
    return preferred != own and path.is_relative_to(preferred)


def stop_hint(target: Path) -> str:
    """Where the Stop hook tells a session to write its handoff."""
    preferred = handoff_inbox(target)
    if preferred == target / CLAUDE_INBOX:
        return f"`{CLAUDE_INBOX}/`"
    return f"`{preferred}/` (the main checkout, so it outlives this worktree)"
