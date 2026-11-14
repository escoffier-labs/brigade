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
        "--profile",
        default=None,
        help="Named bundled template profile (e.g. repo-claude). Mutually exclusive with --depth and --harnesses.",
    )
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
        help="Optional add-on ('publisher', 'repo-extras'). May be repeated.",
    )
    p_init.add_argument(
        "--full",
        action="store_true",
        help="Repo depth: also install the full kit (rules/, hooks/pre-push, INSTALL_FOR_AGENTS.md). "
        "Workspace depth always installs it.",
    )
    mode_group = p_init.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--yolo",
        action="store_true",
        help="Keep shared context (brief, recall, Fleet Hub, Memory Handoffs) without verification "
        "enforcement. Switch back with `brigade work mode set engineer`.",
    )
    mode_group.add_argument(
        "--engineer",
        action="store_true",
        help="Engineer mode (the default for new installs): route checks through `brigade work verify run` "
        "and keep the Stop receipt gate. Switches an existing yolo install back.",
    )
    p_init.set_defaults(func=dispatch)


def _requested_work_mode(args: argparse.Namespace) -> str | None:
    """``yolo`` or ``engineer`` when a mode flag was passed; None keeps the recorded mode."""
    if getattr(args, "yolo", False):
        return "yolo"
    if getattr(args, "engineer", False):
        return "engineer"
    return None


def _recorded_selection(target: Path):
    from ..config import load_config

    try:
        cfg = load_config(target.expanduser().resolve())
    except (ValueError, AttributeError, TypeError):
        return None
    return cfg.selection if cfg is not None else None


def dispatch(args) -> int:
    profile = getattr(args, "profile", None)
    depth_arg = getattr(args, "depth", None)
    harnesses_arg = getattr(args, "harnesses", None)
    if profile is not None:
        if depth_arg is not None or harnesses_arg is not None:
            print(
                "error: --profile cannot be combined with --depth or --harnesses",
                file=sys.stderr,
            )
            return 2
        from ..template_profiles import UnknownTemplateProfile, resolve_profile
        from ..install import install_selection
        from ..selection import Selection, resolve_owner

        try:
            sel = resolve_profile(profile)
        except UnknownTemplateProfile as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        except ValueError as exc:
            print(f"error: template profile snapshot invalid: {exc}", file=sys.stderr)
            return 2
        includes: list[str] = []
        for include in list(sel.includes) + list(args.includes):
            if include not in includes:
                includes.append(include)
        if getattr(args, "full", False) and sel.depth == "repo" and "repo-extras" not in includes:
            includes.append("repo-extras")
        if args.owner is not None:
            try:
                owner = resolve_owner(sel.harnesses, override=args.owner)
            except ValueError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
        else:
            owner = sel.owner
        sel = Selection(
            depth=sel.depth,
            harnesses=list(sel.harnesses),
            owner=owner,
            includes=includes,
            surfaces=list(sel.surfaces),
        )
        return install_selection(
            target=args.target,
            selection=sel,
            force=getattr(args, "force", False),
            dry_run=getattr(args, "dry_run", False),
            allow_home=getattr(args, "allow_home", False),
            use_git_exclude=getattr(args, "git_exclude", False),
            update_gitignore=getattr(args, "update_gitignore", True),
            wire_skills=getattr(args, "wire_skills", True),
            work_mode=_requested_work_mode(args),
        )

    # New v0.3.0 path: --depth/--harnesses build a Selection directly.
    if depth_arg is not None or harnesses_arg is not None:
        from ..selection import Selection, KNOWN_HARNESSES, resolve_owner
        from ..install import install_selection

        # A re-run keeps the recorded selection for any flag it omits (#1810).
        recorded = _recorded_selection(args.target)
        depth = args.depth or (recorded.depth if recorded else "repo")
        if args.harnesses is None or args.harnesses == "":
            harnesses = list(recorded.harnesses) if recorded else ["claude"]
        elif args.harnesses == "none":
            harnesses = []
        else:
            harnesses = [h.strip() for h in args.harnesses.split(",") if h.strip()]
        for h in harnesses:
            if h not in KNOWN_HARNESSES:
                print(f"error: unknown harness {h!r} (valid: {KNOWN_HARNESSES})", file=sys.stderr)
                return 2
        if recorded and args.owner is None and harnesses == recorded.harnesses:
            owner = recorded.owner
        else:
            try:
                owner = resolve_owner(harnesses, override=args.owner)
            except ValueError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
        includes = list(recorded.includes) if recorded else []
        for include in args.includes:
            if include not in includes:
                includes.append(include)
        if getattr(args, "full", False) and depth == "repo" and "repo-extras" not in includes:
            includes.append("repo-extras")
        sel = Selection(depth=depth, harnesses=harnesses, owner=owner, includes=includes)
        return install_selection(
            target=args.target,
            selection=sel,
            force=getattr(args, "force", False),
            dry_run=getattr(args, "dry_run", False),
            allow_home=getattr(args, "allow_home", False),
            use_git_exclude=getattr(args, "git_exclude", False),
            update_gitignore=getattr(args, "update_gitignore", True),
            wire_skills=getattr(args, "wire_skills", True),
            work_mode=_requested_work_mode(args),
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
        work_mode=_requested_work_mode(args),
    )
