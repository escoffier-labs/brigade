"""solo-mise command-line entrypoint."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__


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
    p_init = sub.add_parser("init", help="Materialize a profile into a target directory.")
    p_init.add_argument("--target", "-t", type=Path, default=Path("."), help="Where to install.")
    p_init.add_argument(
        "--profile",
        "-p",
        default="repo",
        choices=["repo", "workspace", "openclaw", "hermes", "generic", "publisher"],
        help="Profile to install (default: repo).",
    )
    p_init.add_argument(
        "--harness",
        help="Override the memory-owner label rendered into bootstrap files. "
        "Defaults to the profile's memory_owner_default.",
    )
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

    return parser


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    cmd = args.command

    if cmd == "init":
        from . import init as init_mod

        return init_mod.run(
            target=args.target,
            profile_id=args.profile,
            force=args.force,
            dry_run=args.dry_run,
            harness=args.harness,
            allow_home=args.allow_home,
            update_gitignore=args.update_gitignore,
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

    parser.error(f"unknown command: {cmd}")
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
