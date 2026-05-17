"""solo-mise command-line entrypoint."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .prompt import prompt_for_selection  # imported here so tests can monkeypatch cli.prompt_for_selection


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="solo-mise",
        description="Solomon's Mise en Place: installable agent kitchen starter kit.",
    )
    parser.add_argument(
        "--version", action="version", version=f"solo-mise {__version__}"
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

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
    p_init.add_argument("--dry-run", action="store_true", help="Show what would happen.")
    p_init.add_argument(
        "--depth",
        choices=["repo", "workspace"],
        default=None,
        help="Install depth: 'repo' (minimal) or 'workspace' (full home). "
             "Omit for an interactive prompt.",
    )
    p_init.add_argument(
        "--harnesses",
        default=None,
        help="Comma-separated harness ids: claude, codex, openclaw, hermes. "
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

    # doctor
    p_doctor = sub.add_parser("doctor", help="Verify a target workspace.")
    p_doctor.add_argument("--target", "-t", type=Path, default=Path("."))
    p_doctor.add_argument(
        "--harness",
        choices=["generic", "openclaw", "hermes"],
        default="generic",
    )

    # scrub
    p_scrub = sub.add_parser("scrub", help="Run content-guard against a target.")
    p_scrub.add_argument("--target", "-t", type=Path, default=Path("."))
    p_scrub.add_argument(
        "--policy",
        default="public-repo",
        help="Policy file name (looks under .solo-mise/policies, then content-guard/policies) or path.",
    )
    p_scrub.add_argument("--dry-run", action="store_true")

    # handoff-template
    p_ht = sub.add_parser("handoff-template", help="Print the handoff TEMPLATE.md.")
    p_ht.add_argument(
        "--target",
        "-t",
        type=Path,
        default=None,
        help="Prefer the target's installed TEMPLATE.md when present.",
    )

    # ingest
    p_ing = sub.add_parser("ingest", help="Process .claude/memory-handoffs/*.md into canonical memory.")
    p_ing.add_argument("--target", "-t", type=Path, default=Path("."))
    p_ing.add_argument("--dry-run", action="store_true")
    p_ing.add_argument(
        "--promote-cards",
        action="store_true",
        help="Auto-promote create-card / update-card handoffs (default off; opt-in).",
    )
    p_ing.add_argument(
        "--route-documents",
        action="store_true",
        help="Auto-route no-card handoffs to TOOLS.md/USER.md/rules/.learnings (default off; opt-in).",
    )

    # openclaw-fragments
    p_ocf = sub.add_parser("openclaw-fragments", help="Write OpenClaw config fragments for manual review.")
    p_ocf.add_argument("--out", "-o", type=Path, required=True, help="Output directory.")

    # hermes-fragments
    p_hf = sub.add_parser("hermes-fragments", help="Write Hermes adapter fragments (experimental).")
    p_hf.add_argument("--out", "-o", type=Path, required=True, help="Output directory.")

    # reconfigure
    p_recon = sub.add_parser("reconfigure", help="Adjust an existing install to a new Selection.")
    p_recon.add_argument("--target", "-t", type=Path, default=Path("."))
    p_recon.add_argument("--depth", choices=["repo", "workspace"], default=None)
    p_recon.add_argument("--harnesses", default=None)
    p_recon.add_argument("--owner", default=None)
    p_recon.add_argument("--include", dest="includes", action="append", default=[])
    p_recon.add_argument("--prune", action="store_true",
                         help="Remove files for harnesses no longer selected.")

    return parser


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    cmd = args.command

    if cmd == "init":
        # New v0.3.0 path: --depth/--harnesses build a Selection directly.
        if getattr(args, "depth", None) is not None or getattr(args, "harnesses", None) is not None:
            from .selection import Selection, KNOWN_HARNESSES, resolve_owner
            from .install import install_selection

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
            )

        # No selection flags: interactive prompt.
        from .prompt import NonInteractiveError
        from .install import install_selection
        try:
            sel = prompt_for_selection()
        except NonInteractiveError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        return install_selection(
            target=args.target,
            selection=sel,
            force=getattr(args, "force", False),
            dry_run=getattr(args, "dry_run", False),
            allow_home=getattr(args, "allow_home", False),
        )
    if cmd == "doctor":
        from . import doctor as doctor_mod

        return doctor_mod.run(target=args.target, harness=args.harness)
    if cmd == "scrub":
        from . import scrub as scrub_mod

        return scrub_mod.run(target=args.target, policy=args.policy, dry_run=args.dry_run)
    if cmd == "handoff-template":
        from . import handoff as handoff_mod

        return handoff_mod.run(target=args.target)
    if cmd == "ingest":
        from . import ingest as ingest_mod

        return ingest_mod.run(
            target=args.target,
            dry_run=args.dry_run,
            promote_cards=args.promote_cards,
            route_documents=args.route_documents,
        )
    if cmd == "openclaw-fragments":
        from . import fragments as frag_mod

        return frag_mod.write_fragments(args.out, harness="openclaw")
    if cmd == "hermes-fragments":
        from . import fragments as frag_mod

        return frag_mod.write_fragments(args.out, harness="hermes")
    if cmd == "reconfigure":
        from .config import load_config
        from .reconfigure import reconfigure as _reconfigure
        from .selection import Selection, KNOWN_HARNESSES, resolve_owner

        existing = load_config(args.target)
        if existing is None:
            print("error: no .solo-mise/config.json in target. Run `solo-mise init` first.", file=sys.stderr)
            return 2

        depth = args.depth or existing.selection.depth
        if args.harnesses is None:
            harnesses = list(existing.selection.harnesses)
        elif args.harnesses == "none":
            harnesses = []
        else:
            harnesses = [h.strip() for h in args.harnesses.split(",") if h.strip()]
        for h in harnesses:
            if h not in KNOWN_HARNESSES:
                print(f"error: unknown harness {h!r}", file=sys.stderr)
                return 2
        owner = resolve_owner(harnesses, override=args.owner)
        includes = list(args.includes) if args.includes else list(existing.selection.includes)
        new_sel = Selection(depth=depth, harnesses=harnesses, owner=owner, includes=includes)
        return _reconfigure(args.target, new_selection=new_sel, prune=args.prune)

    parser.error(f"unknown command: {cmd}")
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
