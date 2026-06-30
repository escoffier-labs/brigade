"""brigade init command group."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
    # init
    p_init = sub.add_parser("init", help="Materialize a selection into a target directory.")
    p_init.add_argument("--target", "-t", type=Path, default=Path("."), help="Where to install.")
    p_init.add_argument("--force", action="store_true", help="Overwrite existing files.")
    p_init.add_argument(
        "--allow-home",
        action="store_true",
        help="Override the safety guard that refuses to install directly into $HOME.",
    )
    p_init.add_argument(
        "--no-gitignore",
        dest="update_gitignore",
        action="store_false",
        default=True,
        help="Do not create or update the target's .gitignore.",
    )
    p_init.add_argument(
        "--no-wire",
        dest="wire_skills",
        action="store_false",
        default=True,
        help="Do not install built-in work-loop skills into harness skill dirs "
        "(leaves Brigade installed but not wired into the agent's work loop).",
    )
    p_init.add_argument(
        "--git-exclude",
        action="store_true",
        help="Write Brigade ignores to .git/info/exclude (local-only) instead of the tracked .gitignore. "
        "Use this in a third-party clone you do not want to commit Brigade ignores into.",
    )
    p_init.add_argument("--dry-run", action="store_true", help="Show what would happen.")
    p_init.add_argument(
        "--depth",
        choices=["repo", "workspace"],
        default=None,
        help="Install depth: 'repo' (minimal) or 'workspace' (full home). Omit for an interactive prompt.",
    )
    p_init.add_argument(
        "--harnesses",
        default=None,
        help="Comma-separated harness ids: claude, codex, opencode, antigravity, pi, cursor, aider, goose, continue, copilot, qwen, kimi, adal, openhands, grok, amp, crush, openclaw, hermes. "
        "Pass 'none' for a generic install with no harness-specific files.",
    )
    p_init.add_argument(
        "--owner",
        default=None,
        help="Override the canonical memory owner. Must be 'this-repo' or one of --harnesses.",
    )
    p_init.add_argument(
        "--include",
        dest="includes",
        action="append",
        default=[],
        help="Optional add-on (currently: 'publisher'). May be repeated.",
    )
    p_init.set_defaults(func=dispatch)


def dispatch(args) -> int:
    # New v0.3.0 path: --depth/--harnesses build a Selection directly.
    if getattr(args, "depth", None) is not None or getattr(args, "harnesses", None) is not None:
        from ..selection import Selection, KNOWN_HARNESSES, resolve_owner
        from ..install import install_selection

        depth = args.depth or "repo"
        if args.harnesses is None or args.harnesses == "":
            harnesses = ["claude"]
        elif args.harnesses == "none":
            harnesses = []
        else:
            harnesses = [h.strip() for h in args.harnesses.split(",") if h.strip()]
        for h in harnesses:
            if h not in KNOWN_HARNESSES:
                print(f"error: unknown harness {h!r} (valid: {KNOWN_HARNESSES})", file=sys.stderr)
                return 2
        try:
            owner = resolve_owner(harnesses, override=args.owner)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        sel = Selection(depth=depth, harnesses=harnesses, owner=owner, includes=list(args.includes))
        return install_selection(
            target=args.target,
            selection=sel,
            force=getattr(args, "force", False),
            dry_run=getattr(args, "dry_run", False),
            allow_home=getattr(args, "allow_home", False),
            use_git_exclude=getattr(args, "git_exclude", False),
            update_gitignore=getattr(args, "update_gitignore", True),
            wire_skills=getattr(args, "wire_skills", True),
        )

    # No selection flags: interactive prompt.
    from ..prompt import NonInteractiveError
    from ..install import install_selection
    from brigade import cli as _cli_pkg

    try:
        sel = _cli_pkg.prompt_for_selection()
    except NonInteractiveError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return install_selection(
        target=args.target,
        selection=sel,
        force=getattr(args, "force", False),
        dry_run=getattr(args, "dry_run", False),
        allow_home=getattr(args, "allow_home", False),
        use_git_exclude=getattr(args, "git_exclude", False),
        update_gitignore=getattr(args, "update_gitignore", True),
        wire_skills=getattr(args, "wire_skills", True),
    )
