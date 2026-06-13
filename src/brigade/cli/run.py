"""brigade run command group."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
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
        "--sandbox",
        choices=["read-only", "workspace-write", "danger-full-access"],
        default=None,
        help=(
            "Native sandbox mode for codex agents. Combine with --read-only to keep prompt-level "
            "read-only rules while overriding the native sandbox."
        ),
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
    p_run.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from .. import aboyeur as aboyeur_mod
    from .. import roster as roster_mod

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
    cwd_roster_path = run_cwd / ".brigade" / "roster.toml"
    if args.roster is not None:
        roster_path = args.roster.expanduser()
    elif cwd_roster_path.exists():
        roster_path = cwd_roster_path
    else:
        home_roster_path = Path.home() / ".brigade" / "roster.toml"
        if home_roster_path.exists():
            roster_path = home_roster_path
        else:
            print(
                f"error: roster not found: checked {cwd_roster_path} and {home_roster_path}. "
                "Create .brigade/roster.toml or pass --roster.",
                file=sys.stderr,
            )
            return 2
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
    effective_sandbox = args.sandbox if args.sandbox is not None else loaded_roster.sandbox
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
        sandbox=effective_sandbox,
    )
    if output_dir is not None:
        print(f"artifacts: {output_dir}", file=sys.stderr)
        if args.inspect:
            from .. import runs_cmd

            runs_cmd.show(output_dir)
    return rc
