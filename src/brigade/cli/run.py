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
    p_run.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Allow running when --cwd has uncommitted git changes.",
    )
    p_run.add_argument(
        "--worktree",
        action="store_true",
        help="Run agents in a detached git worktree and write changes.patch to the run artifacts.",
    )
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
    from .. import runguard
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
    if args.worktree and args.no_artifacts:
        print(
            "error: --worktree cannot be used with --no-artifacts; "
            "the worktree is removed after the run and changes.patch is its only output.",
            file=sys.stderr,
        )
        return 2
    if args.worktree and not runguard.is_git_worktree(run_cwd):
        print(f"error: --worktree requires a git worktree: {run_cwd}", file=sys.stderr)
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
    if args.read_only:
        advisory = _read_only_advisory(loaded_roster, effective_sandbox)
        if advisory:
            print("warning: --read-only is best-effort for some agents in this run:", file=sys.stderr)
            for line in advisory:
                print(f"  - {line}", file=sys.stderr)
            print(
                "  brigade cannot guarantee these agents leave the tree untouched; review the run output.",
                file=sys.stderr,
            )
            if output_dir is not None:
                from .. import localio

                localio.write_json(
                    output_dir / "read-only-enforcement.json",
                    {"read_only": True, "sandbox": effective_sandbox, "best_effort_agents": advisory},
                )
    # The dirty guard protects write runs from mixing agent edits with uncommitted
    # work. Dry, read-only, and worktree runs never edit the tree, so reviewing
    # uncommitted changes stays possible without --allow-dirty.
    write_run = not args.dry_run and not args.read_only and effective_sandbox != "read-only"
    try:
        if write_run and not args.worktree and not args.allow_dirty and runguard.is_git_worktree(run_cwd):
            runguard.require_clean_worktree(run_cwd)
    except runguard.RunGuardError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    worktree_cwd = None
    effective_cwd = run_cwd
    keep_worktree = False
    try:
        with runguard.run_lock(run_cwd):
            if args.worktree:
                worktree_cwd = _worktree_checkout_path(runguard.git_root(run_cwd), output_dir)
                effective_cwd = runguard.create_detached_worktree(run_cwd, worktree_cwd)
                print(f"worktree: {effective_cwd}", file=sys.stderr)
            rc = aboyeur_mod.run(
                args.task,
                loaded_roster,
                dry_run=args.dry_run,
                show_plan=args.show_plan,
                verbose=args.verbose,
                cwd=effective_cwd,
                output_dir=output_dir,
                handoff_inbox=handoff_inbox,
                read_only=args.read_only,
                sandbox=effective_sandbox,
            )
            if args.worktree and output_dir is not None:
                summary = runguard.collect_changes_patch(effective_cwd, output_dir / "changes.patch")
                if summary.changed and not runguard.verify_changes_patch(effective_cwd, summary.path):
                    # A corrupt patch must never be the run's silent primary
                    # deliverable; keep the worktree as the recoverable copy.
                    keep_worktree = True
                    print(
                        f"error: changes.patch failed validation ({summary.path}); "
                        f"worktree kept for recovery: {effective_cwd}",
                        file=sys.stderr,
                    )
                    rc = max(rc, 2)
                elif summary.changed:
                    print(
                        f"changes: {summary.path} ({summary.tracked_count + summary.untracked_count} file(s))",
                        file=sys.stderr,
                    )
                else:
                    print(f"changes: none ({summary.path})", file=sys.stderr)
    except runguard.RunGuardError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        if worktree_cwd is not None and not keep_worktree:
            runguard.remove_worktree(run_cwd, worktree_cwd)
    if output_dir is not None:
        print(f"artifacts: {output_dir}", file=sys.stderr)
        if args.inspect:
            from .. import runs_cmd

            runs_cmd.show(output_dir)
    return rc


def _worktree_checkout_path(repo_root: Path, output_dir: Path) -> Path:
    run_id = output_dir.expanduser().resolve().name
    return Path.home() / ".cache" / "brigade" / "worktrees" / f"{repo_root.name}-{run_id}"


def _read_only_advisory(roster, effective_sandbox) -> list[str]:
    """Lines describing which worker agents do not hard-enforce read-only.

    A writable --sandbox override downgrades even natively-sandboxed CLIs to
    prompt-only, so the advisory reflects the sandbox actually in effect.
    """
    from .. import agents as agents_mod
    from .. import roster as roster_mod

    sandbox_overrides_native = effective_sandbox in ("workspace-write", "danger-full-access")
    lines: list[str] = []
    # The orchestrator runs too (it plans), so include it alongside the workers.
    orchestrator = roster.agents.get(roster.orchestrator)
    agents_to_check = [orchestrator, *roster_mod.workers(roster)] if orchestrator else roster_mod.workers(roster)
    for agent in agents_to_check:
        cli = agent.cli or ""
        enforcement = agents_mod.read_only_enforcement(cli)
        if sandbox_overrides_native and enforcement == "hard":
            enforcement = "soft"
        if enforcement == "hard":
            continue
        how = "prompt-only (the model may ignore it)" if enforcement == "soft" else "not applied for this CLI"
        lines.append(f"{agent.name} ({cli or 'unknown'}): read-only is {how}")
    return lines
