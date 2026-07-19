"""brigade run command group."""

from __future__ import annotations

import argparse
import math
import sys
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from subprocess import DEVNULL, STDOUT, Popen
from typing import Callable, Iterator


_DETACH_START_TIMEOUT_SECONDS = 30.0
_DETACH_POLL_INTERVAL_SECONDS = 0.05
_DEFAULT_RUN_LOCK_WAIT_SECONDS = 600.0


@contextmanager
def _terminalize_escaped_run(
    output_dir: Path | None,
    *,
    seat: str | None,
    should_terminalize: Callable[[], bool] | None = None,
) -> Iterator[None]:
    from .. import aboyeur as aboyeur_mod
    from .. import runguard

    try:
        yield
    except runguard.RetainRunLockError:
        raise
    except KeyboardInterrupt:
        if (
            (should_terminalize is None or should_terminalize())
            and output_dir is not None
            and (output_dir / "run.json").is_file()
        ):
            aboyeur_mod.record_run_termination(
                output_dir,
                status="canceled",
                failure_phase=None,
                failure_kind="keyboard-interrupt",
                detail="run canceled by user",
                seat=seat,
            )
        raise
    except TimeoutError as exc:
        detail = " ".join(str(exc).split()) or "run timed out"
        if (
            (should_terminalize is None or should_terminalize())
            and output_dir is not None
            and (output_dir / "run.json").is_file()
        ):
            aboyeur_mod.record_run_termination(
                output_dir,
                status="timeout",
                failure_phase=None,
                failure_kind="timeout",
                detail=detail,
                seat=seat,
            )
        raise
    except Exception as exc:
        detail = f"{type(exc).__name__}: {' '.join(str(exc).split()) or 'unexpected run failure'}"
        if (
            (should_terminalize is None or should_terminalize())
            and output_dir is not None
            and (output_dir / "run.json").is_file()
        ):
            aboyeur_mod.record_run_termination(
                output_dir,
                status="failed",
                failure_phase=None,
                failure_kind="unexpected-error",
                detail=detail,
                seat=seat,
            )
        raise


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
    p_run.add_argument(
        "--resolved-roster-source",
        choices=("explicit", "workspace", "user"),
        default=None,
        help=argparse.SUPPRESS,
    )
    p_run.add_argument(
        "--resolved-roster-shadowed",
        action="append",
        default=[],
        type=Path,
        help=argparse.SUPPRESS,
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
        roster_resolution = roster_mod.resolve_roster(run_cwd, args.roster)
    except FileNotFoundError as exc:
        print(f"error: {exc}. Create .brigade/roster.toml or pass --roster.", file=sys.stderr)
        return 2
    if args.resolved_roster_source is not None:
        roster_resolution = roster_mod.RosterResolution(
            path=roster_resolution.path,
            source=args.resolved_roster_source,
            shadowed=tuple(path.expanduser().resolve() for path in args.resolved_roster_shadowed),
        )
    roster_path = roster_resolution.path
    try:
        loaded_roster = roster_mod.load_roster(roster_path, resolution=roster_resolution)
    except FileNotFoundError:
        print(
            f"error: roster not found: {roster_path}. Create .brigade/roster.toml or pass --roster.",
            file=sys.stderr,
        )
        return 2
    except ValueError as exc:
        print(f"error: invalid roster at {roster_path}: {exc}", file=sys.stderr)
        return 2
    print(f"roster: {roster_resolution.path} ({roster_resolution.source})", file=sys.stderr)
    for shadowed in roster_resolution.shadowed:
        print(
            f"warning: workspace roster {roster_resolution.path} shadows user roster {shadowed}; "
            "pass --roster PATH to choose either file explicitly.",
            file=sys.stderr,
        )
    if args.worker is not None:
        worker_error = _direct_worker_error(args.worker, loaded_roster, roster_mod)
        if worker_error is not None:
            print(f"error: {worker_error}", file=sys.stderr)
            return 2
    handoff_inbox = None
    if args.handoff:
        handoff_inbox = args.handoff_inbox or (run_cwd / ".claude" / "memory-handoffs")
    effective_sandbox = args.sandbox if args.sandbox is not None else loaded_roster.sandbox
    advisory: list[str] = []
    output_warnings: list[str] = []
    if args.read_only:
        advisory = _read_only_advisory(loaded_roster, effective_sandbox, worker=args.worker)
        output_warnings = _read_only_output_warnings(loaded_roster, worker=args.worker)
        if advisory:
            print("warning: --read-only is best-effort for some agents in this run:", file=sys.stderr)
            for line in advisory:
                print(f"  - {line}", file=sys.stderr)
            print(
                "  brigade cannot guarantee these agents leave the tree untouched; review the run output.",
                file=sys.stderr,
            )
        if output_warnings:
            print("warning: direct Cursor read-only output is model-limited:", file=sys.stderr)
            for line in output_warnings:
                print(f"  - {line}", file=sys.stderr)
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

    output_dir = None
    if not args.no_artifacts:
        output_dir = args.output_dir or aboyeur_mod.make_run_dir(run_cwd / ".brigade" / "runs")

    if args.detach:
        assert output_dir is not None
        return _dispatch_detached(
            args,
            run_cwd=run_cwd,
            roster_resolution=roster_resolution,
            roster=loaded_roster,
            output_dir=output_dir,
        )

    worktree_cwd = None
    effective_cwd = run_cwd
    keep_worktree = False
    lifecycle_seat = args.worker or loaded_roster.orchestrator
    try:
        with ExitStack() as lifecycle:
            lifecycle.enter_context(_terminalize_escaped_run(output_dir, seat=lifecycle_seat))
            lifecycle.enter_context(aboyeur_mod.terminal_sigterm_handler(output_dir, seat=lifecycle_seat))
            if output_dir is not None:
                aboyeur_mod.record_run_start(
                    output_dir,
                    task=args.task,
                    cwd=run_cwd,
                    roster=loaded_roster,
                    read_only=args.read_only,
                    worker=args.worker,
                    dry_run=args.dry_run,
                    lock_workspace=run_cwd,
                    codex_transport=args.codex_transport or loaded_roster.codex_transport,
                )
            if output_dir is not None and (advisory or output_warnings):
                from .. import localio

                localio.write_json(
                    output_dir / "read-only-enforcement.json",
                    {
                        "read_only": True,
                        "sandbox": effective_sandbox,
                        "best_effort_agents": advisory,
                        "output_warnings": output_warnings,
                    },
                )
            lifecycle.enter_context(runguard.run_lock(run_cwd, run_dir=output_dir, wait_seconds=args.wait))
            if args.worktree:
                worktree_cwd = _worktree_checkout_path(runguard.git_root(run_cwd), output_dir)
                effective_cwd = runguard.create_detached_worktree(run_cwd, worktree_cwd)
                keep_worktree = True
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
            if args.worktree:
                run_kwargs["lock_workspace"] = run_cwd
                run_kwargs["defer_artifact_collection"] = True
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
            try:
                rc = aboyeur_mod.run(args.task, loaded_roster, **run_kwargs)
            except runguard.RetainRunLockError:
                raise
            except KeyboardInterrupt:
                if output_dir is not None:
                    aboyeur_mod.record_run_termination(
                        output_dir,
                        status="canceled",
                        failure_phase=None,
                        failure_kind="keyboard-interrupt",
                        detail="run canceled by user",
                        seat=lifecycle_seat,
                    )
                raise
            except TimeoutError as exc:
                detail = " ".join(str(exc).split()) or "run timed out"
                if output_dir is not None:
                    aboyeur_mod.record_run_termination(
                        output_dir,
                        status="timeout",
                        failure_phase=None,
                        failure_kind="timeout",
                        detail=detail,
                        seat=lifecycle_seat,
                    )
                raise
            except Exception as exc:
                detail = f"{type(exc).__name__}: {' '.join(str(exc).split()) or 'unexpected run failure'}"
                if output_dir is not None:
                    aboyeur_mod.record_run_termination(
                        output_dir,
                        status="failed",
                        failure_phase=None,
                        failure_kind="unexpected-error",
                        detail=detail,
                        seat=lifecycle_seat,
                    )
                raise
            if args.worktree and output_dir is not None:
                # Until the patch is proven good, the worktree is the only
                # recoverable copy of the agents' edits; a collection failure
                # must not let cleanup destroy it.
                patch_path = output_dir / "changes.patch"
                try:
                    summary = runguard.collect_changes_patch(effective_cwd, patch_path)
                except (runguard.RunGuardError, OSError) as exc:
                    detail = (
                        str(exc) if isinstance(exc, runguard.RunGuardError) else f"failed to write changes.patch: {exc}"
                    )
                    patch_ref = "changes.patch" if patch_path.is_file() else None
                    aboyeur_mod.record_artifact_collection(
                        output_dir,
                        status="failed",
                        patch_ref=patch_ref,
                        worktree=effective_cwd,
                        failure_phase="artifact-collection",
                        failure_kind="collection-error",
                        detail=detail,
                    )
                    if isinstance(exc, runguard.RunGuardError):
                        raise
                    raise runguard.RunGuardError(detail) from exc
                if summary.changed and not runguard.verify_changes_patch(effective_cwd, summary.path):
                    # A corrupt patch must never be the run's silent primary
                    # deliverable; keep the worktree as the recoverable copy.
                    aboyeur_mod.record_artifact_collection(
                        output_dir,
                        status="failed",
                        patch_ref="changes.patch",
                        changed=True,
                        tracked_count=summary.tracked_count,
                        untracked_count=summary.untracked_count,
                        worktree=effective_cwd,
                        failure_phase="artifact-validation",
                        failure_kind="invalid-patch",
                        detail="changes.patch failed validation",
                    )
                    print(
                        f"error: changes.patch failed validation ({summary.path}); "
                        f"worktree kept for recovery: {effective_cwd}",
                        file=sys.stderr,
                    )
                    rc = max(rc, 2)
                else:
                    if summary.path.is_file():
                        try:
                            aboyeur_mod.set_artifact_patch_ref(output_dir, "changes.patch")
                        except runguard.RunGuardError as exc:
                            aboyeur_mod.record_artifact_collection(
                                output_dir,
                                status="failed",
                                patch_ref="changes.patch",
                                changed=summary.changed,
                                tracked_count=summary.tracked_count,
                                untracked_count=summary.untracked_count,
                                worktree=effective_cwd,
                                failure_phase="artifact-collection",
                                failure_kind="receipt-update-error",
                                detail=str(exc),
                            )
                            raise
                    aboyeur_mod.record_artifact_collection(
                        output_dir,
                        status="ok",
                        patch_ref="changes.patch",
                        changed=summary.changed,
                        tracked_count=summary.tracked_count,
                        untracked_count=summary.untracked_count,
                    )
                    keep_worktree = False
                    if summary.changed:
                        print(
                            f"changes: {summary.path} ({summary.tracked_count + summary.untracked_count} file(s))",
                            file=sys.stderr,
                        )
                    else:
                        print(f"changes: none ({summary.path})", file=sys.stderr)
    except KeyboardInterrupt:
        print("error: run canceled by user", file=sys.stderr)
        if worktree_cwd is not None and keep_worktree:
            print(f"worktree kept for recovery: {worktree_cwd}", file=sys.stderr)
        return 130
    except runguard.RunGuardError as exc:
        print(f"error: {exc}", file=sys.stderr)
        if worktree_cwd is not None and keep_worktree:
            print(f"worktree kept for recovery: {worktree_cwd}", file=sys.stderr)
        return 2
    except TimeoutError as exc:
        detail = " ".join(str(exc).split()) or "worker dispatch timed out"
        print(f"error: run timed out: {detail}", file=sys.stderr)
        if worktree_cwd is not None and keep_worktree:
            print(f"worktree kept for recovery: {worktree_cwd}", file=sys.stderr)
        return 2
    except Exception as exc:
        detail = " ".join(str(exc).split()) or "unexpected run failure"
        print(f"error: unexpected run failure: {type(exc).__name__}: {detail}", file=sys.stderr)
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


def _dispatch_detached(args, *, run_cwd: Path, roster_resolution, roster, output_dir: Path) -> int:
    from .. import aboyeur as aboyeur_mod
    from .. import proc as proc_mod

    output_dir = output_dir.expanduser().resolve()
    lifecycle_seat = args.worker or roster.orchestrator
    startup_registry = proc_mod.ProcessRegistry()
    child = None
    child_handoff = False

    def cancel_startup_child() -> None:
        if child is not None and not child_handoff:
            startup_registry.cancel()

    try:
        with ExitStack() as lifecycle:
            lifecycle.enter_context(
                _terminalize_escaped_run(
                    output_dir,
                    seat=lifecycle_seat,
                    should_terminalize=lambda: not child_handoff,
                )
            )
            lifecycle.enter_context(
                aboyeur_mod.terminal_sigterm_handler(
                    output_dir,
                    seat=lifecycle_seat,
                    before_record=cancel_startup_child,
                    should_record=lambda: not child_handoff,
                )
            )
            aboyeur_mod.record_run_start(
                output_dir,
                task=args.task,
                cwd=run_cwd,
                roster=roster,
                read_only=args.read_only,
                worker=args.worker,
            )
            initial_receipt = (output_dir / "run.json").read_bytes()
            log_path = output_dir / "detached.log"
            argv = _detached_child_argv(
                args,
                run_cwd=run_cwd,
                roster_resolution=roster_resolution,
                output_dir=output_dir,
            )
            try:
                with log_path.open("a", encoding="utf-8") as log:
                    child = Popen(
                        argv,
                        cwd=run_cwd,
                        stdin=DEVNULL,
                        stdout=log,
                        stderr=STDOUT,
                        start_new_session=True,
                    )
                    startup_registry.register(child)
            except OSError as exc:
                if child is not None:
                    startup_registry.terminate(child)
                detail = f"failed to start detached run: {exc}"
                aboyeur_mod.record_run_termination(
                    output_dir,
                    status="failed",
                    failure_phase="startup",
                    failure_kind="spawn-error",
                    detail=detail,
                    seat=lifecycle_seat,
                )
                print(f"error: {detail}", file=sys.stderr)
                print(f"log: {log_path}", file=sys.stderr)
                return 2
            except BaseException:
                if child is not None:
                    startup_registry.terminate(child)
                raise

            try:
                exit_code, metadata_taken_over = _poll_detached_start(
                    child,
                    output_dir,
                    initial_receipt=initial_receipt,
                )
            except BaseException:
                cancel_startup_child()
                raise
            if metadata_taken_over:
                child_handoff = True
                startup_registry.unregister(child)
            elif exit_code is None:
                cancel_startup_child()
                detail = f"detached child did not write run metadata within {_DETACH_START_TIMEOUT_SECONDS:g} seconds"
                aboyeur_mod.record_run_termination(
                    output_dir,
                    status="timeout",
                    failure_phase="startup",
                    failure_kind="timeout",
                    detail=detail,
                    seat=lifecycle_seat,
                )
                print(f"error: {detail}", file=sys.stderr)
                print(f"artifacts: {output_dir}", file=sys.stderr)
                print(f"log: {log_path}", file=sys.stderr)
                return 2
            else:
                startup_registry.unregister(child)
            if exit_code is not None:
                detail = f"detached child exited before run metadata was written: exit {exit_code}"
                aboyeur_mod.record_run_termination(
                    output_dir,
                    status="failed",
                    failure_phase="startup",
                    failure_kind="early-exit",
                    detail=detail,
                    seat=lifecycle_seat,
                )
                print(f"error: {detail}", file=sys.stderr)
                print(f"artifacts: {output_dir}", file=sys.stderr)
                print(f"log: {log_path}", file=sys.stderr)
                return 2

            if not (output_dir / "run.json").is_file():
                print(
                    "warning: detached child has not written run metadata yet; check the log for startup progress.",
                    file=sys.stderr,
                )
            print(f"run: {output_dir.name}")
            print(f"detached: pid {child.pid}", file=sys.stderr)
            print(f"artifacts: {output_dir}", file=sys.stderr)
            print(f"log: {log_path}", file=sys.stderr)
            return 0
    except KeyboardInterrupt:
        print("error: run canceled by user", file=sys.stderr)
        return 130
    except Exception as exc:
        detail = " ".join(str(exc).split()) or "unexpected run failure"
        print(f"error: unexpected run failure: {type(exc).__name__}: {detail}", file=sys.stderr)
        return 2


def _detached_child_argv(args, *, run_cwd: Path, roster_resolution, output_dir: Path) -> list[str]:
    argv = [
        sys.executable,
        "-m",
        "brigade",
        "run",
        args.task,
        "--roster",
        str(roster_resolution.path),
        "--resolved-roster-source",
        roster_resolution.source,
        "--cwd",
        str(run_cwd),
        "--output-dir",
        str(output_dir),
    ]
    for shadowed in roster_resolution.shadowed:
        argv.extend(["--resolved-roster-shadowed", str(shadowed)])
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


def _poll_detached_start(proc: Popen, output_dir: Path, *, initial_receipt: bytes) -> tuple[int | None, bool]:
    run_json = output_dir / "run.json"
    deadline = time.monotonic() + _DETACH_START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            if run_json.read_bytes() != initial_receipt:
                return None, True
        except OSError:
            pass
        exit_code = proc.poll()
        if exit_code is not None:
            return exit_code, False
        time.sleep(_DETACH_POLL_INTERVAL_SECONDS)
    return None, False


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

    Enforcement follows each adapter's actual read-only versus sandbox
    precedence rather than treating every hard adapter like Codex.
    """
    from .. import agents as agents_mod
    from .. import roster as roster_mod

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
        enforcement = agents_mod.read_only_enforcement(
            cli,
            sandbox=effective_sandbox,
            transport=agent.transport,
        )
        if enforcement == "hard":
            continue
        how = "prompt-only (the model may ignore it)" if enforcement == "soft" else "not applied for this CLI"
        lines.append(f"{agent.name} ({cli or 'unknown'}): read-only is {how}")
    return lines


def _read_only_output_warnings(roster, worker: str | None = None) -> list[str]:
    """Warnings for direct adapters whose read-only mode can lose assistant text."""
    from .. import agents as agents_mod
    from .. import roster as roster_mod

    if worker is not None:
        selected = roster.agents.get(worker)
        agents_to_check = [selected] if selected is not None else []
    else:
        orchestrator = roster.agents.get(roster.orchestrator)
        agents_to_check = [orchestrator, *roster_mod.workers(roster)] if orchestrator else roster_mod.workers(roster)
    lines: list[str] = []
    for agent in agents_to_check:
        if agent.cli != "cursor" or agent.transport != "direct":
            continue
        limitation = agents_mod.direct_cursor_read_only_limitation(agent.model)
        if limitation is not None:
            lines.append(f"{agent.name} (cursor/{agent.model}): {limitation}")
    return lines
