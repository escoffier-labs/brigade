"""brigade run command group."""

from __future__ import annotations

import argparse
import math
import sys
import time
from pathlib import Path
from subprocess import DEVNULL, STDOUT, Popen


_DETACH_START_TIMEOUT_SECONDS = 30.0
_DETACH_POLL_INTERVAL_SECONDS = 0.05
_DEFAULT_RUN_LOCK_WAIT_SECONDS = 600.0


def _non_negative_seconds(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number of seconds") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("must be a finite non-negative number of seconds")
    return parsed


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
        "--worker",
        default=None,
        help="Dispatch the full task directly to one worker seat, skipping planning and synthesis.",
    )
    p_run.add_argument(
        "--detach",
        action="store_true",
        help="Start the run in a detached child process and return after run metadata is written.",
    )
    p_run.add_argument(
        "--wait",
        nargs="?",
        const=_DEFAULT_RUN_LOCK_WAIT_SECONDS,
        default=0.0,
        type=_non_negative_seconds,
        metavar="SECONDS",
        help=(
            "Wait up to SECONDS for an active run lock instead of failing immediately "
            f"(default with no value: {_DEFAULT_RUN_LOCK_WAIT_SECONDS:g}s)."
        ),
    )
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
        "--no-code-graph",
        action="store_true",
        help="Do not attach GraphTrail code graph context to brigade run prompts.",
    )
    p_run.add_argument(
        "--no-evidence",
        action="store_true",
        help="Do not attach MiseLedger run evidence context to brigade run prompts.",
    )
    p_run.add_argument(
        "--no-route",
        action="store_true",
        help="Do not compute the deterministic route brief or check plan coverage against it.",
    )
    p_run.add_argument(
        "--approve-ship",
        action="store_true",
        help="Release a ship stage the route would otherwise hold for approval.",
    )
    p_run.add_argument(
        "--route-template",
        default=None,
        help="Task template hint for route derivation (e.g. vertical-slice, bugfix, docs).",
    )
    p_run.add_argument(
        "--route-signal",
        action="append",
        default=[],
        dest="route_signals",
        metavar="+SIG|-SIG",
        help="Force-add (+auth-surface) or suppress (~ship-requested) a derived route signal. Repeatable.",
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
        "--codex-transport",
        choices=["exec", "app-server"],
        default=None,
        help="Transport for codex workers. Defaults to the roster's codex_transport (exec).",
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
    from ..route_catalog import validate_overrides

    run_cwd = args.cwd.expanduser().resolve()
    if not run_cwd.is_dir():
        print(f"error: --cwd is not a directory: {run_cwd}", file=sys.stderr)
        return 2
    if args.route_signals:
        # Fail before the run machinery spins up, not deep inside route_brief.
        try:
            validate_overrides(args.route_signals)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    if args.detach and args.dry_run:
        print("error: --detach cannot be used with --dry-run", file=sys.stderr)
        return 2
    if args.detach and args.no_artifacts:
        print("error: --detach cannot be used with --no-artifacts", file=sys.stderr)
        return 2
    if args.detach and args.inspect:
        print("error: --detach cannot be used with --inspect", file=sys.stderr)
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
    try:
        roster_path = roster_mod.resolve_roster_path(run_cwd, args.roster)
    except FileNotFoundError as exc:
        print(f"error: {exc}. Create .brigade/roster.toml or pass --roster.", file=sys.stderr)
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
    if args.worker is not None:
        worker_error = _direct_worker_error(args.worker, loaded_roster, roster_mod)
        if worker_error is not None:
            print(f"error: {worker_error}", file=sys.stderr)
            return 2
    output_dir = None
    if not args.no_artifacts:
        output_dir = args.output_dir or aboyeur_mod.make_run_dir(run_cwd / ".brigade" / "runs")
    handoff_inbox = None
    if args.handoff:
        handoff_inbox = args.handoff_inbox or (run_cwd / ".claude" / "memory-handoffs")
    effective_sandbox = args.sandbox if args.sandbox is not None else loaded_roster.sandbox
    if args.read_only:
        advisory = _read_only_advisory(loaded_roster, effective_sandbox, worker=args.worker)
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

    if args.detach:
        assert output_dir is not None
        return _dispatch_detached(args, run_cwd=run_cwd, roster_path=roster_path, output_dir=output_dir)

    worktree_cwd = None
    effective_cwd = run_cwd
    keep_worktree = False
    try:
        with runguard.run_lock(run_cwd, wait_seconds=args.wait):
            if args.worktree:
                worktree_cwd = _worktree_checkout_path(runguard.git_root(run_cwd), output_dir)
                effective_cwd = runguard.create_detached_worktree(run_cwd, worktree_cwd)
                print(f"worktree: {effective_cwd}", file=sys.stderr)
            run_kwargs = {
                "dry_run": args.dry_run,
                "show_plan": args.show_plan,
                "verbose": args.verbose,
                "cwd": effective_cwd,
                "output_dir": output_dir,
                "handoff_inbox": handoff_inbox,
                "read_only": args.read_only,
                "sandbox": effective_sandbox,
            }
            if args.worktree and any(agent.transport == "acpx" for agent in loaded_roster.agents.values()):
                run_kwargs["authorized_writable_worktree"] = True
            if args.worker is not None:
                run_kwargs["worker"] = args.worker
            if args.codex_transport is not None:
                run_kwargs["codex_transport"] = args.codex_transport
            if args.no_code_graph:
                run_kwargs["code_graph_enabled"] = False
            if args.no_evidence:
                run_kwargs["evidence_enabled"] = False
            if args.no_route:
                run_kwargs["route_enabled"] = False
            if args.approve_ship:
                run_kwargs["route_approvals"] = ("ship-approved",)
            if args.route_template is not None:
                run_kwargs["route_template"] = args.route_template
            if args.route_signals:
                run_kwargs["route_overrides"] = tuple(args.route_signals)
            rc = aboyeur_mod.run(args.task, loaded_roster, **run_kwargs)
            if args.worktree and output_dir is not None:
                # Until the patch is proven good, the worktree is the only
                # recoverable copy of the agents' edits; a collection failure
                # must not let cleanup destroy it.
                keep_worktree = True
                summary = runguard.collect_changes_patch(effective_cwd, output_dir / "changes.patch")
                if summary.path.is_file():
                    aboyeur_mod.set_artifact_patch_ref(output_dir, "changes.patch")
                if summary.changed and not runguard.verify_changes_patch(effective_cwd, summary.path):
                    # A corrupt patch must never be the run's silent primary
                    # deliverable; keep the worktree as the recoverable copy.
                    print(
                        f"error: changes.patch failed validation ({summary.path}); "
                        f"worktree kept for recovery: {effective_cwd}",
                        file=sys.stderr,
                    )
                    rc = max(rc, 2)
                elif summary.changed:
                    keep_worktree = False
                    print(
                        f"changes: {summary.path} ({summary.tracked_count + summary.untracked_count} file(s))",
                        file=sys.stderr,
                    )
                else:
                    keep_worktree = False
                    print(f"changes: none ({summary.path})", file=sys.stderr)
    except runguard.RunGuardError as exc:
        print(f"error: {exc}", file=sys.stderr)
        if worktree_cwd is not None and keep_worktree:
            print(f"worktree kept for recovery: {worktree_cwd}", file=sys.stderr)
        return 2
    finally:
        if worktree_cwd is not None and not keep_worktree:
            runguard.remove_worktree(run_cwd, worktree_cwd)
    if output_dir is not None:
        print(f"artifacts: {output_dir}", file=sys.stderr)
        _print_suspected_noop_warning(output_dir)
        if args.inspect:
            from .. import runs_cmd

            runs_cmd.show(output_dir)
    return rc


def _dispatch_detached(args, *, run_cwd: Path, roster_path: Path, output_dir: Path) -> int:
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = output_dir / "detached.log"
    argv = _detached_child_argv(args, run_cwd=run_cwd, roster_path=roster_path, output_dir=output_dir)
    try:
        with log_path.open("a", encoding="utf-8") as log:
            proc = Popen(
                argv,
                cwd=run_cwd,
                stdin=DEVNULL,
                stdout=log,
                stderr=STDOUT,
                start_new_session=True,
            )
    except OSError as exc:
        print(f"error: failed to start detached run: {exc}", file=sys.stderr)
        print(f"log: {log_path}", file=sys.stderr)
        return 2

    exit_code = _poll_detached_start(proc, output_dir)
    if exit_code is not None:
        print(f"error: detached child exited before run metadata was written: exit {exit_code}", file=sys.stderr)
        print(f"artifacts: {output_dir}", file=sys.stderr)
        print(f"log: {log_path}", file=sys.stderr)
        return 2

    if not (output_dir / "run.json").is_file():
        print(
            "warning: detached child has not written run metadata yet; check the log for startup progress.",
            file=sys.stderr,
        )
    print(f"run: {output_dir.name}")
    print(f"detached: pid {proc.pid}", file=sys.stderr)
    print(f"artifacts: {output_dir}", file=sys.stderr)
    print(f"log: {log_path}", file=sys.stderr)
    return 0


def _detached_child_argv(args, *, run_cwd: Path, roster_path: Path, output_dir: Path) -> list[str]:
    argv = [
        sys.executable,
        "-m",
        "brigade",
        "run",
        args.task,
        "--roster",
        str(roster_path.expanduser().resolve()),
        "--cwd",
        str(run_cwd),
        "--output-dir",
        str(output_dir),
    ]
    if args.allow_dirty:
        argv.append("--allow-dirty")
    if args.worktree:
        argv.append("--worktree")
    if args.show_plan:
        argv.append("--show-plan")
    if args.verbose:
        argv.append("--verbose")
    if args.read_only:
        argv.append("--read-only")
    if args.worker is not None:
        argv.extend(["--worker", args.worker])
    if args.wait > 0:
        argv.extend(["--wait", f"{args.wait:g}"])
    if args.no_code_graph:
        argv.append("--no-code-graph")
    if args.no_evidence:
        argv.append("--no-evidence")
    if args.sandbox is not None:
        argv.extend(["--sandbox", args.sandbox])
    if args.codex_transport is not None:
        argv.extend(["--codex-transport", args.codex_transport])
    if args.handoff:
        argv.append("--handoff")
    if args.handoff_inbox is not None:
        argv.extend(["--handoff-inbox", str(args.handoff_inbox.expanduser().resolve())])
    return argv


def _direct_worker_error(worker: str, loaded_roster, roster_mod) -> str | None:
    agent = loaded_roster.agents.get(worker)
    if agent is None:
        return f"unknown worker: {worker}"
    if worker == loaded_roster.orchestrator:
        return f"--worker cannot target orchestrator seat: {worker}"
    if agent.cli is None:
        return f"worker has no CLI adapter: {worker}"
    if not roster_mod.is_cli_allowed(agent.cli, loaded_roster):
        return f"{agent.cli} is not allowed by limits.allow_models"
    return None


def _poll_detached_start(proc: Popen, output_dir: Path) -> int | None:
    run_json = output_dir / "run.json"
    deadline = time.monotonic() + _DETACH_START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if run_json.is_file():
            return None
        exit_code = proc.poll()
        if exit_code is not None:
            return exit_code
        time.sleep(_DETACH_POLL_INTERVAL_SECONDS)
    return None


def _worktree_checkout_path(repo_root: Path, output_dir: Path) -> Path:
    run_id = output_dir.expanduser().resolve().name
    return Path.home() / ".cache" / "brigade" / "worktrees" / f"{repo_root.name}-{run_id}"


def _print_suspected_noop_warning(output_dir: Path) -> None:
    try:
        import json

        payload = json.loads((output_dir / "run.json").read_text())
    except (OSError, json.JSONDecodeError):
        return
    if isinstance(payload, dict) and payload.get("suspected_noop") is True:
        print(
            "warning: suspected no-op run; ok workers produced no non-.brigade file changes.",
            file=sys.stderr,
        )


def _read_only_advisory(roster, effective_sandbox, worker: str | None = None) -> list[str]:
    """Lines describing which worker agents do not hard-enforce read-only.

    A writable --sandbox override downgrades even natively-sandboxed CLIs to
    prompt-only, so the advisory reflects the sandbox actually in effect.
    """
    from .. import agents as agents_mod
    from .. import roster as roster_mod

    sandbox_overrides_native = effective_sandbox in ("workspace-write", "danger-full-access")
    lines: list[str] = []
    if worker is not None:
        selected = roster.agents.get(worker)
        agents_to_check = [selected] if selected is not None else []
    else:
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
