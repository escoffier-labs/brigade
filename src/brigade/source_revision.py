"""Check that a source checkout can be reproduced from an immutable Git revision."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, Callable

from . import proc

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class SourceRevisionError(RuntimeError):
    """A checkout that cannot supply an immutable, reproducible source revision."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def clean_source_revision(
    cwd: Path,
    *,
    runner: Callable[..., Any] | None = None,
    process_registry: proc.ProcessRegistry | None = None,
    ignore_prefixes: tuple[str, ...] = (),
    timeout: float = 120.0,
) -> str:
    """Return a clean 40-character HEAD, or raise ``SourceRevisionError``.

    Dirty tracked content or relevant untracked content means the source
    context cannot be reproduced from a revision. HEAD is never silently
    substituted. Ignored paths (local credentials, build output) are excluded by
    ``git status --porcelain`` and are never implied to travel with the
    revision. Callers own their own refusal codes for the two cases:
    ``source-dirty`` for a non-clean checkout and ``source-unavailable`` for
    unreadable or non-revision state.
    """
    runner = runner or subprocess.run
    proc.check_cancellation(process_registry)
    try:
        head = runner(
            ["git", "-C", str(cwd), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        proc.check_cancellation(process_registry)
        raise SourceRevisionError("source-unavailable", f"source revision is unreadable: {exc}") from exc
    proc.check_cancellation(process_registry)
    if getattr(head, "returncode", 1) != 0:
        raise SourceRevisionError("source-unavailable", "source checkout has no resolvable HEAD revision")
    revision = _CONTROL_RE.sub(" ", str(getattr(head, "stdout", "") or "")).strip()[:64]
    if SHA_RE.match(revision) is None:
        raise SourceRevisionError("source-unavailable", "source HEAD is not an immutable 40-character revision")
    proc.check_cancellation(process_registry)
    try:
        status = runner(
            ["git", "-C", str(cwd), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        proc.check_cancellation(process_registry)
        raise SourceRevisionError("source-unavailable", f"source cleanliness is unreadable: {exc}") from exc
    proc.check_cancellation(process_registry)
    if getattr(status, "returncode", 1) != 0:
        raise SourceRevisionError("source-unavailable", "source checkout cleanliness is unverifiable")
    # Porcelain v1 lines are "XY path". Callers may exclude their own state
    # directories (for example .brigade/) the same way require_clean_worktree does.
    dirty = [
        line
        for line in str(getattr(status, "stdout", "") or "").splitlines()
        if line.strip() and not line[3:].startswith(ignore_prefixes)
    ]
    if dirty:
        raise SourceRevisionError(
            "source-dirty",
            "source checkout has uncommitted tracked or untracked content; the approved revision is not reproducible",
        )
    return revision
