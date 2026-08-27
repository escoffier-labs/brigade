"""brigade run command group."""

from __future__ import annotations

import _thread
import argparse
import logging
import math
import sys
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from subprocess import DEVNULL, STDOUT, Popen
from typing import Any, Callable, Iterator, Mapping


_DETACH_START_TIMEOUT_SECONDS = 30.0
_DETACH_POLL_INTERVAL_SECONDS = 0.05
_UNBOUNDED_RUN_LOCK_WAIT = math.inf
# Same logger the fleet client uses for its own claim fallbacks, so the
# --no-fleet-claim line lands next to them.
_FLEET_LOG = logging.getLogger("brigade.fleet")


@contextmanager
def _terminalize_escaped_run(
    output_dir: Path | None,
    *,
    seat: str | None,
    should_terminalize: Callable[[], bool] | None = None,
    interrupt_failure: Callable[[], tuple[str, str, str, str] | None] | None = None,
) -> Iterator[None]:
    """Terminalize a run that escaped its normal result path.

    ``interrupt_failure`` (#1157 round 2) classifies a KeyboardInterrupt on
    the main thread: when it returns a ``(status, failure_phase,
    failure_kind, detail)`` tuple (a fleet heartbeat abort), the receipt
    carries that kind instead of "keyboard-interrupt"."""

    from .. import aboyeur as aboyeur_mod
    from .. import runguard

    try:
        yield
    except runguard.RetainRunLockError:
        raise
    except KeyboardInterrupt:
        failure = interrupt_failure() if interrupt_failure is not None else None
        if (
            (should_terminalize is None or should_terminalize())
            and output_dir is not None
            and (output_dir / "run.json").is_file()
        ):
            if failure is not None:
                status, phase, kind, detail = failure
                aboyeur_mod.record_run_termination(
                    output_dir,
                    status=status,
                    failure_phase=phase,
                    failure_kind=kind,
                    detail=detail,
                    seat=seat,
                )
            else:
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
                # #711: unexpected-error is infrastructure-neutral at kind level; phase is omitted
                # because record_run_termination derives a status-based phase that may fall outside
                # RUN_INFRASTRUCTURE_FAILURE_PHASES. Kind-level membership in
                # RUN_UNAMBIGUOUS_INFRASTRUCTURE_FAILURE_KINDS keeps outcome capture neutral here
                # and in run_failure_is_infrastructure_at_read_time regardless of stamped phase.
                failure_phase=None,
                failure_kind="unexpected-error",
                detail=detail,
                seat=seat,
            )
        raise


def _non_empty_task(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("must not be empty or whitespace")
    return value


def _non_negative_seconds(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number of seconds") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("must be a finite non-negative number of seconds")
    return parsed


def _run_lock_wait_arg(value: str) -> float:
    if value.lower() in ("inf", "infinity", "unbounded"):
        return _UNBOUNDED_RUN_LOCK_WAIT
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number of seconds or 'inf'") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("must be a finite non-negative number of seconds or 'inf'")
    return parsed


def _resolved_run_lock_wait_seconds(args, run_cwd: Path) -> float:
    from .. import config as config_mod

    if args.wait is not None:
        return args.wait
    return config_mod.resolve_run_lock_wait_seconds(run_cwd)


def register(sub: argparse._SubParsersAction) -> None:
    # run
    p_run = sub.add_parser("run", help="Run a bounded cross-model orchestration task.")
    p_run.add_argument(
        "task",
        type=_non_empty_task,
        help="Task for the aboyeur to plan, dispatch, and synthesize.",
    )
    p_run.add_argument(
        "--roster",
        type=Path,
        default=None,
        help="Path to roster.toml. Defaults to .brigade/roster.toml under the current directory.",
    )
    p_run.add_argument(
        "--resolved-roster-source",
        choices=("explicit", "workspace", "worktree-parent", "user"),
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
        const=_UNBOUNDED_RUN_LOCK_WAIT,
        default=None,
        type=_run_lock_wait_arg,
        metavar="SECONDS",
        help=(
            "Wait for an active run lock in arrival order instead of failing immediately. "
            "A bare --wait waits until the lock is available with no fixed ceiling; "
            "pass SECONDS to bound the wait. Use --wait=0 to fail fast even when "
            ".brigade/config.json sets run_lock_wait_seconds."
        ),
    )
    p_run.add_argument(
        "--no-fleet-claim",
        action="store_true",
        help=(
            "Skip the fleet hub repo claim and rely on the local run lock alone (logged once). "
            "Escape hatch when a claim left by a crashed run blocks this repo; "
            "see also `brigade fleet claims --release`."
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
    p_run.add_argument(
        "--run-budget",
        type=Path,
        default=None,
        help="Path to a brigade.run_budget.v1 JSON declaration persisted with this run.",
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
    p_run.add_argument(
        "--keep-going",
        action="store_true",
        help="Do not skip later stages when an earlier stage worker fails (pre-2026-07 behavior).",
    )
    p_run.add_argument(
        "--scheduler",
        choices=("waves", "dag"),
        default=None,
        help=(
            "Worker scheduling: fixed integer-stage waves or router-DAG ready queue. "
            "Defaults to limits.scheduler from the roster, then waves."
        ),
    )
    p_run.set_defaults(func=dispatch)


def _resolved_scheduler(args, roster) -> str:
    """Scheduler precedence: --scheduler flag, then roster limits.scheduler, then waves."""
    return args.scheduler or roster.scheduler or "waves"


def dispatch(args) -> int:
    from .. import aboyeur as aboyeur_mod
    from .. import fleet_client
    from .. import run_budget
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
    if args.run_budget is not None and args.no_artifacts:
        print("error: --run-budget requires run artifacts so the declaration can be persisted", file=sys.stderr)
        return 2
    run_budget_payload = None
    if args.run_budget is not None:
        try:
            run_budget_payload = run_budget.load_declaration_file(args.run_budget)
        except run_budget.BudgetCompatibilityError as exc:
            print(f"error: invalid run budget declaration: {exc}", file=sys.stderr)
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
    from .. import run_preference

    preference = run_preference.refresh_cache()
    loaded_roster = run_preference.apply_to_roster(loaded_roster, preference)
    if args.worker is None:
        args.worker = run_preference.resolve_worker(
            preference,
            loaded_roster,
            worker=None,
            task=args.task,
        )
    args.task = run_preference.apply_to_task(args.task, preference, worker=args.worker)
    preference_bits = [f"{key}={value}" for key, value in preference.payload().items() if key != "notes"]
    if preference_bits:
        print(f"run preference: {' '.join(preference_bits)} (cached)", file=sys.stderr)
    print(f"roster: {roster_resolution.path} ({roster_resolution.source})", file=sys.stderr)
    # A worktree-parent roster shadowing the user roster is the designed
    # outcome for worktrees of a wired repo, so only a workspace roster warns.
    if roster_resolution.source == "workspace":
        for shadowed in roster_resolution.shadowed:
            print(
                f"warning: workspace roster {roster_resolution.path} shadows user roster {shadowed}; "
                "pass --roster PATH to choose either file explicitly.",
                file=sys.stderr,
            )
    if args.worker is not None:
        worker_error = _direct_worker_error(
            args.worker,
            loaded_roster,
            roster_mod,
            read_only=args.read_only,
        )
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
    # --allow-dirty in a dirty primary checkout would let ground truth attribute
    # pre-existing work to the worker; require a linked worktree (or --worktree)
    # so attribution stays clean. A clean tree, or a linked worktree the user
    # created themselves, may still run dirty.
    if (
        args.allow_dirty
        and write_run
        and not args.worktree
        and runguard.is_git_worktree(run_cwd)
        and runguard.is_primary_checkout(run_cwd)
    ):
        try:
            runguard.require_clean_worktree(run_cwd)
        except runguard.DirtyWorktreeError:
            print(
                "error: --allow-dirty is not allowed in a primary checkout with uncommitted "
                "changes; brigade would attribute pre-existing changes to the worker. "
                "Commit or stash, use a linked git worktree, or pass --worktree to run in an "
                "isolated checkout.",
                file=sys.stderr,
            )
            return 2
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
            run_budget_payload=run_budget_payload,
        )

    worktree_cwd = None
    effective_cwd = run_cwd
    keep_worktree = False
    lifecycle_seat = args.worker or loaded_roster.orchestrator
    # Set when the fleet heartbeat reports a mid-run credential refusal
    # (#1161): the outer KeyboardInterrupt handler turns the resulting
    # interrupt into an accurate credential-failure message instead of
    # "run canceled by user".
    fleet_credential_failure: list[str] = []
    # Set when the fleet heartbeat reports lost ownership (#1157): without
    # this the interrupt would be remembered as "canceled by user".
    fleet_claim_loss: list[str] = []

    def _fleet_interrupt_failure() -> tuple[str, str, str, str] | None:
        """Classify a fleet-caused interrupt from the main thread (#1157
        round 2): returns ``(status, failure_phase, failure_kind, detail)``
        for the terminal receipt, or None for a genuine user Ctrl-C. The
        markers are appended by the heartbeat-thread callbacks before they
        let the abort fire, so a failed receipt write can never downgrade
        the abort to "keyboard-interrupt"."""
        if fleet_claim_loss:
            detail = fleet_claim_loss[0]
            return (
                "failed",
                "dispatch",
                "fleet-claim-lost",
                f"fleet claim on {claim_target} was lost ({detail}); run canceled",
            )
        if fleet_credential_failure:
            detail = fleet_credential_failure[0]
            return (
                "failed",
                "dispatch",
                "fleet-credentials-rejected",
                f"fleet hub rejected this node's credentials ({detail}); run canceled",
            )
        return None

    def abort_on_fleet_claim_lost(reason: str | None) -> None:
        """Heartbeat-thread callback (#1157): another owner now holds the
        claim this run was granted. Only the cheap classification marker is
        recorded here; the terminal receipt itself is written by the
        main-thread handler (#1157 round 2), where a write failure surfaces
        loudly instead of silently degrading to a user-cancel record.
        The marker lands before the fallible stderr print (#1157 round 3):
        a failed write must not stop the abort from being classified."""
        detail = reason or "held by another node"
        fleet_claim_loss.append(detail)
        print(
            f"error: fleet claim on {claim_target!r} was lost ({detail}); canceling the active run",
            file=sys.stderr,
        )

    def abort_on_fleet_credential_failure(detail: str | None) -> None:
        """Heartbeat-thread callback (#1161): the hub rejected this machine's
        token while dispatch is active.

        The marker is appended before any diagnostic and the interrupt fires
        from ``finally`` (#1157 round 3): a stderr write failure must leave
        the abort and its classification intact, never swallow the callback
        into a logged warning while the run continues. The main-thread
        handler writes the terminal receipt from the marker (#1157 round 2)."""
        fleet_credential_failure.append(detail or "auth refused")
        try:
            print(
                f"error: fleet hub rejected this node's credentials ({detail or 'auth refused'}); "
                "canceling the active run; enroll this machine with 'brigade fleet nodes add <node_id>' "
                "and set [fleet] node_token_file",
                file=sys.stderr,
            )
        finally:
            _thread.interrupt_main()

    try:
        with ExitStack() as lifecycle:
            lifecycle.enter_context(
                _terminalize_escaped_run(output_dir, seat=lifecycle_seat, interrupt_failure=_fleet_interrupt_failure)
            )
            lifecycle.enter_context(aboyeur_mod.terminal_sigterm_handler(output_dir, seat=lifecycle_seat))
            if output_dir is not None:
                start_kwargs: dict[str, Any] = {
                    "task": args.task,
                    "cwd": run_cwd,
                    "roster": loaded_roster,
                    "read_only": args.read_only,
                    "worker": args.worker,
                    "dry_run": args.dry_run,
                    "lock_workspace": run_cwd,
                    "codex_transport": args.codex_transport or loaded_roster.codex_transport,
                    "scheduler": _resolved_scheduler(args, loaded_roster),
                }
                if run_budget_payload is not None:
                    start_kwargs["run_budget_payload"] = run_budget_payload
                aboyeur_mod.record_run_start(
                    output_dir,
                    **start_kwargs,
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
            # The local lease's verdict on the previous run at this lock
            # (issue #1141): each dead owner it reconciled on the way in.
            reconciled_dead_owners: list[dict[str, object] | None] = []
            lock_path = lifecycle.enter_context(
                runguard.run_lock(
                    run_cwd,
                    run_dir=output_dir,
                    wait_seconds=_resolved_run_lock_wait_seconds(args, run_cwd),
                    on_reconcile=reconciled_dead_owners.append,
                )
            )
            # Hub-arbitrated cross-machine claim (issue #1125), taken after
            # the local lease so only the run that owns run.lock ever holds
            # (or releases) the hub claim — a queued sibling can neither run
            # unclaimed nor delete the winner's claim. No hub, no node
            # identity, or an unreachable hub falls back to the local
            # run.lock taken above. The claim row records this run's lock
            # lease; when the lease reconcile just found the previous run at
            # this lock dead, the claim that run took under that exact lease
            # (same node, same target, a token that died with it) is
            # superseded instead of locking this node out for the residual
            # TTL — a claim under any other lease is never touched.
            claim_target = fleet_client.resolve_claim_target(run_cwd)
            if args.no_fleet_claim:
                _FLEET_LOG.warning(
                    "fleet claim skipped for %s (--no-fleet-claim); relying on the local run lock alone",
                    claim_target,
                )
            else:
                dead_owner = next(
                    (owner for owner in reversed(reconciled_dead_owners) if owner and owner.get("owner_token")),
                    None,
                )
                lifecycle.enter_context(
                    fleet_client.repo_claim(
                        claim_target,
                        base_path=run_cwd,
                        conductor=lifecycle_seat,
                        lock_owner=runguard.read_lock_owner(lock_path),
                        supersede_dead_owner=dead_owner,
                        on_credential_failure=abort_on_fleet_credential_failure,
                        on_claim_lost=abort_on_fleet_claim_lost,
                    )
                )
            if args.worktree:
                worktree_cwd = _worktree_checkout_path(runguard.git_root(run_cwd), output_dir)
                effective_cwd = runguard.create_detached_worktree(run_cwd, worktree_cwd)
                keep_worktree = True
                print(f"worktree: {effective_cwd}", file=sys.stderr)
            run_kwargs: dict[str, Any] = {
                "dry_run": args.dry_run,
                "show_plan": args.show_plan,
                "verbose": args.verbose,
                "cwd": effective_cwd,
                "output_dir": output_dir,
                "handoff_inbox": handoff_inbox,
                "read_only": args.read_only,
                "sandbox": effective_sandbox,
            }
            if run_budget_payload is not None:
                run_kwargs["run_budget_payload"] = run_budget_payload
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
            run_kwargs["fail_fast"] = not args.keep_going
            run_kwargs["scheduler"] = _resolved_scheduler(args, loaded_roster)
            try:
                rc = aboyeur_mod.run(args.task, loaded_roster, **run_kwargs)
            except runguard.RetainRunLockError:
                raise
            except KeyboardInterrupt:
                failure = _fleet_interrupt_failure()
                if output_dir is not None:
                    if failure is not None:
                        # The interrupt came from a fleet heartbeat abort
                        # (#1157/#1161); record the real kind from the main
                        # thread so a heartbeat-side receipt failure can
                        # never downgrade it to a user cancel (#1157 round 2).
                        status, phase, kind, detail = failure
                        aboyeur_mod.record_run_termination(
                            output_dir,
                            status=status,
                            failure_phase=phase,
                            failure_kind=kind,
                            detail=detail,
                            seat=lifecycle_seat,
                        )
                    else:
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
            finally:
                if output_dir is not None and (output_dir / "run.json").is_file():
                    from ..route_receipts import write_route_decision

                    write_route_decision(output_dir, loaded_roster, target=run_cwd)
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
                    keep_worktree = rc != 0 and summary.changed
                    if summary.changed:
                        print(
                            f"changes: {summary.path} ({summary.tracked_count + summary.untracked_count} file(s))",
                            file=sys.stderr,
                        )
                    else:
                        print(f"changes: none ({summary.path})", file=sys.stderr)
                    if keep_worktree:
                        print(f"worktree kept for recovery: {worktree_cwd}", file=sys.stderr)
                    elif rc == 0:
                        for pruned in _prune_retained_worktrees(runguard.git_root(run_cwd), effective_cwd):
                            print(f"worktree pruned: {pruned}", file=sys.stderr)
    except KeyboardInterrupt:
        if fleet_credential_failure:
            # The interrupt came from the fleet heartbeat's credential-failure
            # callback (#1161), which already printed and recorded the reason.
            print("error: run canceled: fleet hub rejected this node's credentials", file=sys.stderr)
            rc = 2
        elif fleet_claim_loss:
            # The interrupt came from the fleet heartbeat's claim-lost
            # callback (#1157), which already printed and recorded the reason.
            print("error: run canceled: fleet claim lost to another owner", file=sys.stderr)
            rc = 2
        else:
            print("error: run canceled by user", file=sys.stderr)
            rc = 130
        if worktree_cwd is not None and keep_worktree:
            print(f"worktree kept for recovery: {worktree_cwd}", file=sys.stderr)
        return rc
    except runguard.RunGuardError as exc:
        print(f"error: {exc}", file=sys.stderr)
        if worktree_cwd is not None and keep_worktree:
            print(f"worktree kept for recovery: {worktree_cwd}", file=sys.stderr)
        return 2
    except fleet_client.FleetClaimHeldError as exc:
        print(f"error: {exc}", file=sys.stderr)
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


def _dispatch_detached(
    args, *, run_cwd: Path, roster_resolution, roster, output_dir: Path, run_budget_payload: Mapping[str, Any] | None
) -> int:
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
            start_kwargs: dict[str, Any] = {
                "task": args.task,
                "cwd": run_cwd,
                "roster": roster,
                "read_only": args.read_only,
                "worker": args.worker,
                "scheduler": _resolved_scheduler(args, roster),
            }
            if run_budget_payload is not None:
                start_kwargs["run_budget_payload"] = run_budget_payload
            aboyeur_mod.record_run_start(
                output_dir,
                **start_kwargs,
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
    if args.wait is not None:
        if math.isinf(args.wait):
            argv.append("--wait")
        else:
            argv.extend(["--wait", f"{args.wait:g}"])
    if args.no_code_graph:
        argv.append("--no-code-graph")
    if args.no_evidence:
        argv.append("--no-evidence")
    if getattr(args, "no_fleet_claim", False):
        argv.append("--no-fleet-claim")
    if args.sandbox is not None:
        argv.extend(["--sandbox", args.sandbox])
    if args.codex_transport is not None:
        argv.extend(["--codex-transport", args.codex_transport])
    if args.handoff:
        argv.append("--handoff")
    if args.handoff_inbox is not None:
        argv.extend(["--handoff-inbox", str(args.handoff_inbox.expanduser().resolve())])
    if args.run_budget is not None:
        argv.extend(["--run-budget", str(args.run_budget.expanduser().resolve())])
    return argv


def _direct_worker_error(worker: str, loaded_roster, roster_mod, *, read_only: bool = False) -> str | None:
    agent = loaded_roster.agents.get(worker)
    if agent is None:
        return f"unknown worker: {worker}"
    if worker == loaded_roster.orchestrator:
        return f"--worker cannot target orchestrator seat: {worker}"
    if read_only:
        capability_error = roster_mod.read_only_capability_error(agent)
        if capability_error is not None:
            return capability_error
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


def _prune_retained_worktrees(repo_root: Path, current_worktree: Path) -> list[Path]:
    from .. import proc
    from .. import runguard

    repo_root = repo_root.expanduser().resolve()
    current_worktree = current_worktree.expanduser().resolve()
    cache_parent = current_worktree.parent
    prefix = f"{repo_root.name}-"

    result = proc.run(["git", "worktree", "list", "--porcelain"], cwd=repo_root)
    if result.code != 0:
        return []

    candidates: list[Path] = []
    for line in result.stdout.splitlines():
        if not line.startswith("worktree "):
            continue
        path = Path(line.removeprefix("worktree ").strip()).expanduser().resolve()
        if path == current_worktree:
            continue
        if path.parent != cache_parent:
            continue
        if not path.name.startswith(prefix):
            continue
        candidates.append(path)

    pruned: list[Path] = []
    for path in candidates:
        runguard.remove_worktree(repo_root, path)
        pruned.append(path)
    return pruned


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
        # #703: name the scoring consequence here so the negative capture is not a surprise.
        print(
            "warning: suspected no-op run; ok workers produced no non-.brigade file changes. "
            "outcome capture scores this run negative (see docs/outcome-scoring.md).",
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
