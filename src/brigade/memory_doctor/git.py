"""Git integration: pre-flight checks, commit driver, rollback.

All git interaction goes through subprocess.run(["git", ...]) with
capture_output=True and check=False. Callers branch on returncode and the
typed helpers in this module. No exceptions bubble up from subprocess.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path


def is_git_repo(memory_dir: Path) -> bool:
    """True if memory_dir is the toplevel of a git repo.

    Walking up to a parent repo is intentionally rejected: the memory dir
    must own its own .git/. We check by asking git for the toplevel and
    comparing it to the resolved memory_dir.
    """
    if not memory_dir.exists():
        return False
    result = subprocess.run(
        ["git", "-C", str(memory_dir), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return False
    toplevel = Path(result.stdout.strip()).resolve()
    return toplevel == memory_dir.resolve()


def working_tree_sane(memory_dir: Path) -> tuple[bool, str]:
    """Refuse commits during merge / rebase / cherry-pick / bisect.

    Returns (True, "") when safe to commit; (False, reason) otherwise.
    The reason string is human-readable and surfaces in the CLI error.
    """
    git_dir = memory_dir / ".git"
    if (git_dir / "MERGE_HEAD").exists():
        return False, "merge in progress"
    if (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists():
        return False, "rebase in progress"
    if (git_dir / "CHERRY_PICK_HEAD").exists():
        return False, "cherry-pick in progress"
    if (git_dir / "BISECT_LOG").exists():
        return False, "bisect in progress"
    return True, ""


def files_have_uncommitted_changes(memory_dir: Path, files: list[Path]) -> list[tuple[Path, str]]:
    """Return (file, status_word) pairs for files with uncommitted changes.

    Empty list = all clean. status_word is human-readable
    ('modified', 'untracked', 'staged') and surfaces directly in the CLI error.
    """
    if not files:
        return []
    rel = [str(f.resolve().relative_to(memory_dir.resolve())) for f in files]
    cmd = ["git", "-C", str(memory_dir), "status", "--porcelain", "--", *rel]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        # git status against a path inside an uninitialized repo would have
        # been caught upstream; treat unknown failure as "no dirty files
        # detected" rather than crash. The caller's is_git_repo() check is
        # the authoritative gate.
        return []

    dirty: list[tuple[Path, str]] = []
    files_by_rel = {str(f.resolve().relative_to(memory_dir.resolve())): f for f in files}
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        code = line[:2]
        path = line[3:].strip()
        # Handle quoted paths from git status (paths with spaces or special chars).
        if path.startswith('"') and path.endswith('"'):
            path = path[1:-1]
        if path not in files_by_rel:
            continue
        if code == "??":
            status = "untracked"
        elif code[0] != " " and code[1] != " ":
            status = "modified, staged"
        elif code[0] != " ":
            status = "staged"
        else:
            status = "modified, not staged"
        dirty.append((files_by_rel[path], status))
    return dirty


@dataclass
class CommitResult:
    """Outcome of a commit_run() invocation.

    On success: sha is set, error_kind is None.
    On failure: error_kind is one of {"add", "hook", "commit-other"};
    sha is None; error_message has the git stderr; staged_files lists
    what was already staged at the point of failure.
    """

    sha: str | None = None
    staged_files: list[Path] = field(default_factory=list)
    error_kind: str | None = None
    error_message: str | None = None


def commit_run(
    *,
    memory_dir: Path,
    files: list[Path],
    subject: str,
    body: str,
    author: str | None,
) -> CommitResult:
    """Stage `files` and create a commit with the given subject/body.

    Uses `git commit -- <files>` pathspec form so other staged content is
    not pulled into our commit. Author override via -c user.name/email.
    Never passes --no-verify; pre-commit hooks run normally.
    """
    if not files:
        return CommitResult()

    name: str | None
    email: str | None
    if author:
        try:
            name, email = _parse_author(author)
        except ValueError as e:
            return CommitResult(error_kind="author", error_message=str(e))
    else:
        name = email = None

    rel = [str(f.resolve().relative_to(memory_dir.resolve())) for f in files]

    add_result = subprocess.run(
        ["git", "-C", str(memory_dir), "add", "--", *rel],
        capture_output=True,
        text=True,
        check=False,
    )
    if add_result.returncode != 0:
        return CommitResult(
            error_kind="add",
            error_message=add_result.stderr.strip() or add_result.stdout.strip(),
        )

    cmd = ["git", "-C", str(memory_dir)]
    if name and email:
        cmd += ["-c", f"user.name={name}", "-c", f"user.email={email}"]
    cmd += ["commit", "--quiet", "-m", subject, "-m", body, "--", *rel]

    commit_result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if commit_result.returncode != 0:
        stderr = commit_result.stderr.lower()
        # Pre-commit hook failures vary by hook; git itself emits one of
        # these phrases when a hook exits non-zero. Any other failure is
        # bucketed as commit-other.
        hook_markers = ("pre-commit hook failed", "hook declined", "hook exited")
        if any(marker in stderr for marker in hook_markers):
            error_kind = "hook"
        else:
            error_kind = "commit-other"
        return CommitResult(
            staged_files=files,
            error_kind=error_kind,
            error_message=commit_result.stderr.strip() or commit_result.stdout.strip(),
        )

    sha_result = subprocess.run(
        ["git", "-C", str(memory_dir), "rev-parse", "--short=12", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    sha = sha_result.stdout.strip() if sha_result.returncode == 0 else None
    return CommitResult(sha=sha, staged_files=files)


def _parse_author(spec: str) -> tuple[str, str]:
    """Parse 'Name <email>' into (name, email). Raises ValueError on bad format."""
    if "<" not in spec or not spec.rstrip().endswith(">"):
        raise ValueError(f"author must be in 'Name <email>' format, got: {spec!r}")
    name_part, email_part = spec.split("<", 1)
    name = name_part.strip()
    email = email_part.rstrip(">").strip()
    if not name or not email:
        raise ValueError(f"author missing name or email: {spec!r}")
    return name, email


def validate_author_format(author: str | None) -> str | None:
    """Return an error message when author is not in 'Name <email>' format."""
    if not author:
        return None
    try:
        _parse_author(author)
    except ValueError as e:
        return str(e)
    return None


def rollback_files(memory_dir: Path, files: list[Path]) -> None:
    """Best-effort revert each file to its HEAD state.

    For previously-tracked files: restore from `git show HEAD:<path>`.
    For new (untracked) files: delete from disk.
    Missing files are a no-op. Never raises; rollback failures are logged
    to stderr and swallowed, because rollback is itself an error-path call
    and we don't want to mask the original failure.
    """
    import sys

    for f in files:
        if not f.exists():
            continue
        rel = str(f.resolve().relative_to(memory_dir.resolve()))
        show = subprocess.run(
            ["git", "-C", str(memory_dir), "show", f"HEAD:{rel}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if show.returncode == 0:
            try:
                f.write_text(show.stdout)
            except OSError as e:
                print(f"rollback: failed to restore {rel}: {e}", file=sys.stderr)
        else:
            # No HEAD version means the file was new; delete it.
            try:
                f.unlink()
            except OSError as e:
                print(f"rollback: failed to delete {rel}: {e}", file=sys.stderr)
