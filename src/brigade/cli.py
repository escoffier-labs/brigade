"""brigade command-line entrypoint."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .dogfood_cmd import DEFAULT_TIMEOUT_SECONDS
from .prompt import prompt_for_selection  # imported here so tests can monkeypatch cli.prompt_for_selection


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="brigade",
        description="Brigade: run your agent brigade. Operator-system CLI for agent workspaces.",
    )
    parser.add_argument(
        "--version", action="version", version=f"brigade {__version__}"
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

    # status
    p_status = sub.add_parser("status", help="Show which stations are present and healthy.")
    p_status.add_argument("--target", "-t", type=Path, default=Path("."))

    # add
    p_add = sub.add_parser("add", help="Install and wire a station's managed tools.")
    p_add.add_argument("station", help="Station to add tools for (e.g. memory, guard, tokens).")
    p_add.add_argument("--target", "-t", type=Path, default=Path("."))

    # dogfood
    p_dogfood = sub.add_parser("dogfood", help="Run a safe Codex-only Brigade dogfood review.")
    p_dogfood.add_argument(
        "dogfood_args",
        nargs="*",
        help="Dogfood task, or `init` to write local dogfood defaults.",
    )
    p_dogfood.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_dogfood.add_argument("--output-dir", type=Path, default=None, help="Directory for run artifacts.")
    p_dogfood.add_argument(
        "--handoff-inbox",
        type=Path,
        default=None,
        help="Memory Handoff inbox. Defaults to .claude/memory-handoffs under --target.",
    )
    p_dogfood.add_argument("--force", action="store_true", help="Overwrite an existing dogfood config during init.")
    p_dogfood.add_argument("--no-handoff", action="store_true", help="Do not write a Memory Handoff.")
    p_dogfood.add_argument("--no-inspect", action="store_true", help="Do not print the artifact summary afterward.")
    p_dogfood.add_argument(
        "--native-read-only-sandbox",
        action="store_true",
        help="Use Codex's native read-only sandbox instead of dogfood's default trusted-workspace danger-full-access setting.",
    )
    p_dogfood.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS, help="Per-agent timeout.")

    # run
    p_run = sub.add_parser("run", help="Run a bounded cross-model orchestration task.")
    p_run.add_argument("task", help="Task for the aboyeur to plan, dispatch, and synthesize.")
    p_run.add_argument(
        "--roster",
        type=Path,
        default=None,
        help="Path to roster.toml. Defaults to .brigade/roster.toml under the current directory.",
    )
    p_run.add_argument("--dry-run", action="store_true", help="Print the plan without dispatching workers.")
    p_run.add_argument("--show-plan", action="store_true", help="Print parsed assignments before dispatch.")
    p_run.add_argument("--verbose", action="store_true", help="Print plan, worker status, and synthesis status.")
    p_run.add_argument(
        "--read-only",
        action="store_true",
        help="Tell agents to inspect and recommend only, without modifying files or external state.",
    )
    p_run.add_argument(
        "--inspect",
        action="store_true",
        help="Print a readable artifact summary after the run completes.",
    )
    p_run.add_argument(
        "--cwd",
        type=Path,
        default=Path("."),
        help="Working directory for agent CLI calls and default run artifacts.",
    )
    p_run.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory for run artifacts. Defaults to .brigade/runs/<id> under --cwd.",
    )
    p_run.add_argument("--no-artifacts", action="store_true", help="Do not write run artifacts.")
    p_run.add_argument(
        "--handoff",
        action="store_true",
        help="Write a Memory Handoff for a successful non-dry run.",
    )
    p_run.add_argument(
        "--handoff-inbox",
        type=Path,
        default=None,
        help="Memory Handoff inbox. Defaults to .claude/memory-handoffs under --cwd.",
    )

    # roster
    p_roster = sub.add_parser("roster", help="Create and check aboyeur rosters.")
    roster_sub = p_roster.add_subparsers(dest="roster_command", metavar="<roster-command>")
    roster_sub.required = True
    p_roster_init = roster_sub.add_parser("init", help="Write a starter .brigade/roster.toml.")
    p_roster_init.add_argument("--target", "-t", type=Path, default=Path("."))
    p_roster_init.add_argument("--force", action="store_true", help="Overwrite an existing roster.")
    p_roster_init.add_argument(
        "--ollama-model",
        default="llama3.3",
        help="Default local researcher model for the starter roster.",
    )
    p_roster_init.add_argument("--max-workers", type=int, default=4)
    p_roster_doctor = roster_sub.add_parser("doctor", help="Validate roster syntax and installed CLIs.")
    p_roster_doctor.add_argument("--target", "-t", type=Path, default=Path("."))
    p_roster_doctor.add_argument(
        "--roster",
        type=Path,
        default=None,
        help="Path to roster.toml. Defaults to .brigade/roster.toml under --target.",
    )

    # runs
    p_runs = sub.add_parser("runs", help="Inspect Brigade run artifacts.")
    runs_sub = p_runs.add_subparsers(dest="runs_command", metavar="<runs-command>")
    runs_sub.required = True
    p_runs_list = runs_sub.add_parser("list", help="List recent Brigade run directories.")
    p_runs_list.add_argument(
        "--cwd",
        type=Path,
        default=Path("."),
        help="Workspace whose default .brigade/runs directory should be listed.",
    )
    p_runs_list.add_argument(
        "--runs-dir",
        type=Path,
        default=None,
        help="Explicit runs directory. Defaults to .brigade/runs under --cwd.",
    )
    p_runs_list.add_argument("--limit", type=int, default=10, help="Maximum number of runs to show.")
    p_runs_latest = runs_sub.add_parser("latest", help="Show the most recent Brigade run.")
    p_runs_latest.add_argument(
        "--cwd",
        type=Path,
        default=Path("."),
        help="Workspace whose default .brigade/runs directory should be inspected.",
    )
    p_runs_latest.add_argument(
        "--runs-dir",
        type=Path,
        default=None,
        help="Explicit runs directory. Defaults to .brigade/runs under --cwd.",
    )
    p_runs_show = runs_sub.add_parser("show", help="Show a readable summary of one run directory.")
    p_runs_show.add_argument("run_dir", type=Path, help="Path to a Brigade run artifact directory.")

    # scrub
    p_scrub = sub.add_parser("scrub", help="Run content-guard against a target.")
    p_scrub.add_argument("--target", "-t", type=Path, default=Path("."))
    p_scrub.add_argument(
        "--policy",
        default="public-repo",
        help="Policy file name (looks under .brigade/policies, then content-guard/policies) or path.",
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
    if cmd == "status":
        from . import status as status_mod

        return status_mod.run(target=args.target)
    if cmd == "add":
        from . import add as add_mod

        return add_mod.run(target=args.target, station=args.station)
    if cmd == "dogfood":
        from . import dogfood_cmd

        dogfood_args = list(args.dogfood_args)
        if dogfood_args and dogfood_args[0] == "init":
            if len(dogfood_args) > 1:
                print("error: dogfood init does not accept a task argument", file=sys.stderr)
                return 2
            return dogfood_cmd.init(
                target=args.target,
                artifacts_dir=args.output_dir,
                handoff_inbox=args.handoff_inbox,
                force=args.force,
                handoff=not args.no_handoff,
                inspect=not args.no_inspect,
                native_read_only_sandbox=args.native_read_only_sandbox,
                timeout_seconds=args.timeout_seconds,
            )
        if dogfood_args and dogfood_args[0] == "status":
            if len(dogfood_args) > 1:
                print("error: dogfood status does not accept a task argument", file=sys.stderr)
                return 2
            return dogfood_cmd.status(target=args.target)
        if dogfood_args and dogfood_args[0] == "latest":
            if len(dogfood_args) > 1:
                print("error: dogfood latest does not accept a task argument", file=sys.stderr)
                return 2
            return dogfood_cmd.latest(target=args.target)
        if dogfood_args and dogfood_args[0] == "next":
            if len(dogfood_args) > 1:
                print("error: dogfood next does not accept a task argument", file=sys.stderr)
                return 2
            return dogfood_cmd.next_step(target=args.target)
        task = " ".join(dogfood_args) if dogfood_args else None
        return dogfood_cmd.run(
            task,
            target=args.target,
            output_dir=args.output_dir,
            handoff=not args.no_handoff,
            handoff_inbox=args.handoff_inbox,
            inspect=not args.no_inspect,
            native_read_only_sandbox=args.native_read_only_sandbox,
            timeout_seconds=args.timeout_seconds,
        )
    if cmd == "run":
        from . import aboyeur as aboyeur_mod
        from . import roster as roster_mod

        run_cwd = args.cwd.expanduser().resolve()
        if not run_cwd.is_dir():
            print(f"error: --cwd is not a directory: {run_cwd}", file=sys.stderr)
            return 2
        if args.handoff and args.dry_run:
            print("error: --handoff cannot be used with --dry-run", file=sys.stderr)
            return 2
        if args.inspect and args.no_artifacts:
            print("error: --inspect cannot be used with --no-artifacts", file=sys.stderr)
            return 2
        roster_path = args.roster or (run_cwd / ".brigade" / "roster.toml")
        try:
            loaded_roster = roster_mod.load_roster(roster_path)
        except FileNotFoundError:
            print(
                f"error: roster not found: {roster_path}. Create .brigade/roster.toml or pass --roster.",
                file=sys.stderr,
            )
            return 2
        except ValueError as exc:
            print(f"error: invalid roster: {exc}", file=sys.stderr)
            return 2
        output_dir = None
        if not args.no_artifacts:
            output_dir = args.output_dir or aboyeur_mod.make_run_dir(run_cwd / ".brigade" / "runs")
        handoff_inbox = None
        if args.handoff:
            handoff_inbox = args.handoff_inbox or (run_cwd / ".claude" / "memory-handoffs")
        rc = aboyeur_mod.run(
            args.task,
            loaded_roster,
            dry_run=args.dry_run,
            show_plan=args.show_plan,
            verbose=args.verbose,
            cwd=run_cwd,
            output_dir=output_dir,
            handoff_inbox=handoff_inbox,
            read_only=args.read_only,
        )
        if output_dir is not None:
            print(f"artifacts: {output_dir}", file=sys.stderr)
            if args.inspect:
                from . import runs_cmd

                runs_cmd.show(output_dir)
        return rc
    if cmd == "roster":
        from . import roster_cmd

        if args.roster_command == "init":
            return roster_cmd.init(
                target=args.target,
                force=args.force,
                ollama_model=args.ollama_model,
                max_workers=args.max_workers,
            )
        if args.roster_command == "doctor":
            return roster_cmd.doctor(target=args.target, roster_path=args.roster)
        parser.error(f"unknown roster command: {args.roster_command}")
        return 2
    if cmd == "runs":
        from . import runs_cmd

        if args.runs_command == "list":
            return runs_cmd.list_runs(cwd=args.cwd, runs_dir=args.runs_dir, limit=args.limit)
        if args.runs_command == "latest":
            return runs_cmd.show_latest(cwd=args.cwd, runs_dir=args.runs_dir)
        if args.runs_command == "show":
            return runs_cmd.show(args.run_dir)
        parser.error(f"unknown runs command: {args.runs_command}")
        return 2
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
            print("error: no .brigade/config.json in target. Run `brigade init` first.", file=sys.stderr)
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


def main_deprecated(argv=None) -> int:
    print(
        "warning: the 'solo-mise' command is deprecated; use 'brigade' instead.",
        file=sys.stderr,
    )
    return main(argv)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
