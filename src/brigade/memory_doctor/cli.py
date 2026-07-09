"""CLI for the embedded brigade memory verbs.

Used by ``brigade memory …`` and ``python -m brigade.memory_doctor``.
The standalone ``memory-doctor`` package is retired; this is the home path.
"""

from __future__ import annotations

import argparse
import os
import sys

from . import __version__
from .paths import PathConfigError, resolve_paths


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--memory-dir", default=None, help="Memory dir (cards + MEMORY.md).")
    p.add_argument("--handoffs-dir", default=None, help="Handoffs dir.")
    p.add_argument("--max-lines", type=int, default=None, help="MEMORY.md line threshold (default 180)")
    p.add_argument("--max-bytes", type=int, default=None, help="MEMORY.md byte threshold (default 24000)")


def _add_commit_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--commit", action="store_true", help="Stage + commit after --apply (off by default).")
    p.add_argument("--no-commit", action="store_true", help="Suppress committing even if MEMORY_DOCTOR_COMMIT=1.")
    p.add_argument(
        "--commit-author",
        default=None,
        help='Override author for this commit ("Name <email>"). Default: git config user.name/user.email.',
    )


def build_parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="brigade memory",
        description="Maintenance verbs for the Claude Code / OpenClaw file-based memory system.",
    )
    root.add_argument("--version", action="version", version=f"brigade memory (embedded) {__version__}")
    sub = root.add_subparsers(dest="verb", required=True)

    p_status = sub.add_parser("status", help="Print a read-only summary")
    _add_common(p_status)
    p_status.add_argument("--json", action="store_true", help="Emit JSON instead of human text")

    p_lint = sub.add_parser("lint", help="Scan for dead [[wiki-links]]; exit 1 if any")
    _add_common(p_lint)

    p_compact = sub.add_parser("compact", help="Flatten multi-line MEMORY.md entries into topic files")
    _add_common(p_compact)
    _add_commit_flags(p_compact)
    p_compact.add_argument("--apply", action="store_true", help="Actually write changes (default: dry-run)")

    p_init = sub.add_parser("init-git", help="Initialize the memory dir as a git repo with one initial commit")
    _add_common(p_init)

    return root


def _resolve_commit_flag(args: argparse.Namespace) -> bool:
    if getattr(args, "no_commit", False):
        return False
    if getattr(args, "commit", False):
        return True
    return os.environ.get("MEMORY_DOCTOR_COMMIT", "").strip() in ("1", "true", "yes")


def _resolve_commit_author(args: argparse.Namespace) -> str | None:
    return getattr(args, "commit_author", None) or os.environ.get("MEMORY_DOCTOR_COMMIT_AUTHOR") or None


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        cfg = resolve_paths(
            memory_dir=args.memory_dir,
            handoffs_dir=args.handoffs_dir,
            max_lines=args.max_lines,
            max_bytes=args.max_bytes,
        )
    except PathConfigError as exc:
        print(f"brigade memory: {exc}", file=sys.stderr)
        return 2

    if args.verb == "status":
        from .status import run as run_status

        return run_status(cfg, as_json=args.json)
    if args.verb == "lint":
        from .lint import run as run_lint

        return run_lint(cfg)
    if args.verb == "compact":
        from .compact import run as run_compact

        return run_compact(
            cfg,
            apply=args.apply,
            commit=_resolve_commit_flag(args),
            commit_author=_resolve_commit_author(args),
        )
    if args.verb == "init-git":
        from .init_git import run as run_init_git

        return run_init_git(cfg)
    parser.error(f"unknown verb: {args.verb}")
    return 2
