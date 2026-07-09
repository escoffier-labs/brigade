"""Dispatch helpers for ``brigade memory status|lint|compact|init-git``."""

from __future__ import annotations

import os
from pathlib import Path

from .memory_doctor.paths import PathConfigError, resolve_paths


def _resolve_commit(commit: bool, no_commit: bool) -> bool:
    if no_commit:
        return False
    if commit:
        return True
    return os.environ.get("MEMORY_DOCTOR_COMMIT", "").strip() in ("1", "true", "yes")


def _paths(
    *,
    memory_dir: str | None,
    handoffs_dir: str | None,
    max_lines: int | None,
    max_bytes: int | None,
    target: Path | None,
    require_handoffs: bool = True,
) -> tuple[object | None, int]:
    """Resolve memory/handoffs paths. When --target is set and dirs are not,
    prefer the Brigade workspace layout under that target.
    """
    md = memory_dir
    hd = handoffs_dir
    if target is not None:
        target = target.expanduser().resolve()
        if md is None:
            candidate = target / "memory"
            if candidate.is_dir():
                md = str(candidate)
            elif (target / "memory" / "cards").is_dir():
                # Some layouts keep MEMORY.md at memory/ and cards under cards/.
                md = str(target / "memory")
        if hd is None:
            for candidate in (
                target / ".claude" / "memory-handoffs",
                target / ".codex" / "memory-handoffs",
                target / ".brigade" / "memory-handoffs",
            ):
                if candidate.is_dir():
                    hd = str(candidate)
                    break
    try:
        return (
            resolve_paths(
                memory_dir=md,
                handoffs_dir=hd,
                max_lines=max_lines,
                max_bytes=max_bytes,
                require_handoffs=require_handoffs,
            ),
            0,
        )
    except PathConfigError as exc:
        print(f"brigade memory: {exc}", file=__import__("sys").stderr)
        return None, 2


def status(
    *,
    memory_dir: str | None = None,
    handoffs_dir: str | None = None,
    max_lines: int | None = None,
    max_bytes: int | None = None,
    target: Path | None = None,
    json_output: bool = False,
) -> int:
    from .memory_doctor.status import run

    cfg, code = _paths(
        memory_dir=memory_dir,
        handoffs_dir=handoffs_dir,
        max_lines=max_lines,
        max_bytes=max_bytes,
        target=target,
        require_handoffs=False,
    )
    if cfg is None:
        return code
    return run(cfg, as_json=json_output)


def lint(
    *,
    memory_dir: str | None = None,
    handoffs_dir: str | None = None,
    max_lines: int | None = None,
    max_bytes: int | None = None,
    target: Path | None = None,
) -> int:
    from .memory_doctor.lint import run

    cfg, code = _paths(
        memory_dir=memory_dir,
        handoffs_dir=handoffs_dir,
        max_lines=max_lines,
        max_bytes=max_bytes,
        target=target,
        require_handoffs=False,
    )
    if cfg is None:
        return code
    return run(cfg)


def compact(
    *,
    memory_dir: str | None = None,
    handoffs_dir: str | None = None,
    max_lines: int | None = None,
    max_bytes: int | None = None,
    target: Path | None = None,
    apply: bool = False,
    commit: bool = False,
    no_commit: bool = False,
    commit_author: str | None = None,
) -> int:
    from .memory_doctor.compact import run

    cfg, code = _paths(
        memory_dir=memory_dir,
        handoffs_dir=handoffs_dir,
        max_lines=max_lines,
        max_bytes=max_bytes,
        target=target,
        require_handoffs=False,
    )
    if cfg is None:
        return code
    author = commit_author or os.environ.get("MEMORY_DOCTOR_COMMIT_AUTHOR")
    return run(cfg, apply=apply, commit=_resolve_commit(commit, no_commit), commit_author=author)


def init_git(
    *,
    memory_dir: str | None = None,
    handoffs_dir: str | None = None,
    max_lines: int | None = None,
    max_bytes: int | None = None,
    target: Path | None = None,
) -> int:
    from .memory_doctor.init_git import run

    cfg, code = _paths(
        memory_dir=memory_dir,
        handoffs_dir=handoffs_dir,
        max_lines=max_lines,
        max_bytes=max_bytes,
        target=target,
        require_handoffs=False,
    )
    if cfg is None:
        return code
    return run(cfg)
