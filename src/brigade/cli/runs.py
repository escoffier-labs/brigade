"""brigade runs command group."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
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
    p_runs_list.add_argument(
        "--json",
        action="store_true",
        help="Emit the versioned brigade.runs-list.v1 JSON contract.",
    )
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
    p_runs_latest.add_argument(
        "--json",
        action="store_true",
        help="Emit the versioned brigade.run-detail.v1 JSON contract.",
    )
    p_runs_show = runs_sub.add_parser("show", help="Show a readable summary of one run directory.")
    p_runs_show.add_argument("run", help="Run directory path, run id under --runs-dir, or 'latest'.")
    p_runs_show.add_argument(
        "--cwd",
        type=Path,
        default=Path("."),
        help="Workspace whose default .brigade/runs directory should be used for run ids.",
    )
    p_runs_show.add_argument(
        "--runs-dir",
        type=Path,
        default=None,
        help="Explicit runs directory for run ids. Defaults to .brigade/runs under --cwd.",
    )
    p_runs_show.add_argument(
        "--json",
        action="store_true",
        help="Emit the versioned brigade.run-detail.v1 JSON contract.",
    )
    p_runs_inspect = runs_sub.add_parser(
        "inspect",
        help="Show a run's parent-child lineage and terminal lifecycle state.",
    )
    p_runs_inspect.add_argument("run", help="Run directory path, run id under --runs-dir, or 'latest'.")
    p_runs_inspect.add_argument(
        "--cwd",
        type=Path,
        default=Path("."),
        help="Workspace whose default .brigade/runs directory should be used for run ids.",
    )
    p_runs_inspect.add_argument(
        "--runs-dir",
        type=Path,
        default=None,
        help="Explicit runs directory for run ids. Defaults to .brigade/runs under --cwd.",
    )
    p_runs_child = runs_sub.add_parser(
        "child",
        help="Create a durable child run from a checkpoint-covered parent lifecycle event.",
    )
    p_runs_child.add_argument("run", help="Parent run directory path, run id under --runs-dir, or 'latest'.")
    p_runs_child.add_argument("event_id", help="Parent lifecycle journal event id to branch from.")
    p_runs_child.add_argument(
        "--cwd",
        type=Path,
        default=Path("."),
        help="Workspace whose default .brigade/runs directory should be used for run ids.",
    )
    p_runs_child.add_argument(
        "--runs-dir",
        type=Path,
        default=None,
        help="Explicit runs directory for run ids. Defaults to .brigade/runs under --cwd.",
    )
    p_runs_diff = runs_sub.add_parser(
        "diff",
        help="Compare a durable child run against its recorded parent, or two sibling runs.",
    )
    p_runs_diff.add_argument("run", help="Child run directory path, run id under --runs-dir, or 'latest'.")
    p_runs_diff.add_argument(
        "other",
        nargs="?",
        default=None,
        help="Optional other run (recorded parent or sibling). Defaults to the run's recorded parent.",
    )
    p_runs_diff.add_argument(
        "--cwd",
        type=Path,
        default=Path("."),
        help="Workspace whose default .brigade/runs directory should be used for run ids.",
    )
    p_runs_diff.add_argument(
        "--runs-dir",
        type=Path,
        default=None,
        help="Explicit runs directory for run ids. Defaults to .brigade/runs under --cwd.",
    )
    p_runs_diff.add_argument(
        "--json",
        action="store_true",
        help="Emit the versioned brigade.run-diff.v1 JSON contract.",
    )
    p_runs_watch = runs_sub.add_parser("watch", help="Watch a Brigade run artifact directory until it finishes.")
    p_runs_watch.add_argument("run", help="Run directory path, run id under --runs-dir, or 'latest'.")
    p_runs_watch.add_argument(
        "--cwd",
        type=Path,
        default=Path("."),
        help="Workspace whose default .brigade/runs directory should be used for run ids.",
    )
    p_runs_watch.add_argument(
        "--runs-dir",
        type=Path,
        default=None,
        help="Explicit runs directory for run ids. Defaults to .brigade/runs under --cwd.",
    )
    p_runs_watch.add_argument("--json", action="store_true", help="Emit newline-delimited JSON records.")
    p_runs_watch.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="Polling interval in seconds.",
    )
    p_runs_serve = runs_sub.add_parser(
        "serve",
        help="Open a local read-only Run View in the browser.",
        description="Foreground read-only Run View on loopback. Stops on Ctrl+C; never a service.",
    )
    p_runs_serve.add_argument(
        "--cwd",
        type=Path,
        default=Path("."),
        help="Workspace whose default .brigade/runs directory should be inspected.",
    )
    p_runs_serve.add_argument(
        "--runs-dir",
        type=Path,
        default=None,
        help="Explicit runs directory. Defaults to .brigade/runs under --cwd.",
    )
    p_runs_serve.add_argument(
        "--port",
        type=int,
        default=0,
        help="Loopback port. Default 0 selects an available port.",
    )
    p_runs_serve.add_argument(
        "--no-open",
        action="store_true",
        help="Print the bound URL without opening a browser.",
    )
    p_runs_events = runs_sub.add_parser(
        "events",
        help="Read verified lifecycle journal events with durable cursors.",
    )
    p_runs_events.add_argument("run", help="Run directory path, run id under --runs-dir, or 'latest'.")
    p_runs_events.add_argument(
        "--cwd",
        type=Path,
        default=Path("."),
        help="Workspace whose default .brigade/runs directory should be used for run ids.",
    )
    p_runs_events.add_argument(
        "--runs-dir",
        type=Path,
        default=None,
        help="Explicit runs directory for run ids. Defaults to .brigade/runs under --cwd.",
    )
    p_runs_events.add_argument(
        "--after",
        default=None,
        help="Opaque cursor; emit only committed lifecycle events after this position.",
    )
    p_runs_events.add_argument(
        "--follow",
        action="store_true",
        help="Wait for appended lifecycle events and exit after a terminal event.",
    )
    p_runs_events.add_argument(
        "--json",
        action="store_true",
        default=True,
        help="Emit newline-delimited JSON records (default).",
    )
    p_runs_events.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="Polling interval in seconds when --follow is set.",
    )
    p_runs_steer = runs_sub.add_parser("steer", help="Send steering text to an active app-server worker turn.")
    p_runs_steer.add_argument("run", help="Run directory path, run id under --runs-dir, or 'latest'.")
    p_runs_steer.add_argument("worker", help="Worker name to steer.")
    p_runs_steer.add_argument("text", nargs="+", help="Text to append to the active worker turn.")
    p_runs_steer.add_argument(
        "--cwd",
        type=Path,
        default=Path("."),
        help="Workspace whose default .brigade/runs directory should be used for run ids.",
    )
    p_runs_steer.add_argument(
        "--runs-dir",
        type=Path,
        default=None,
        help="Explicit runs directory for run ids. Defaults to .brigade/runs under --cwd.",
    )
    p_runs_steer.add_argument(
        "--request-id",
        default=None,
        help="Caller request identity for idempotent live control (generated when omitted).",
    )
    p_runs_interrupt = runs_sub.add_parser("interrupt", help="Interrupt active app-server worker turns.")
    p_runs_interrupt.add_argument("run", help="Run directory path, run id under --runs-dir, or 'latest'.")
    p_runs_interrupt.add_argument("worker", nargs="?", default=None, help="Optional worker name to interrupt.")
    p_runs_interrupt.add_argument(
        "--cwd",
        type=Path,
        default=Path("."),
        help="Workspace whose default .brigade/runs directory should be used for run ids.",
    )
    p_runs_interrupt.add_argument(
        "--runs-dir",
        type=Path,
        default=None,
        help="Explicit runs directory for run ids. Defaults to .brigade/runs under --cwd.",
    )
    p_runs_interrupt.add_argument(
        "--request-id",
        default=None,
        help="Caller request identity for idempotent live control (generated when omitted).",
    )
    p_runs_recover = runs_sub.add_parser(
        "recover",
        help="Recover a nonterminal run whose recorded owner process has exited.",
    )
    p_runs_recover.add_argument("run", help="Run directory path, run id under --runs-dir, or 'latest'.")
    p_runs_recover.add_argument(
        "--cwd",
        type=Path,
        default=Path("."),
        help="Workspace whose default .brigade/runs directory should be used for run ids.",
    )
    p_runs_recover.add_argument(
        "--runs-dir",
        type=Path,
        default=None,
        help="Explicit runs directory for run ids. Defaults to .brigade/runs under --cwd.",
    )
    p_runs_redact = runs_sub.add_parser(
        "redact",
        help="Run the explicit operator procedure for lifecycle journal redaction.",
    )
    p_runs_redact.add_argument("run", help="Run directory path, run id under --runs-dir, or 'latest'.")
    p_runs_redact.add_argument(
        "--cwd",
        type=Path,
        default=Path("."),
        help="Workspace whose default .brigade/runs directory should be used for run ids.",
    )
    p_runs_redact.add_argument(
        "--runs-dir",
        type=Path,
        default=None,
        help="Explicit runs directory for run ids. Defaults to .brigade/runs under --cwd.",
    )
    p_runs_redact.add_argument("--from-sequence", dest="sequence_start", type=int, default=None)
    p_runs_redact.add_argument("--to-sequence", dest="sequence_end", type=int, default=None)
    p_runs_redact.add_argument(
        "--reason",
        default=None,
        help="Closed incident reason code; never include the private value.",
    )
    p_runs_redact.add_argument(
        "--cleanup-quarantine",
        dest="cleanup_operation",
        default=None,
        metavar="OPERATION_ID",
        help="Explicitly remove a previously verified quarantine.",
    )
    p_runs_redact.add_argument(
        "--operator-confirm",
        action="store_true",
        help="Confirm this operator-only incident procedure.",
    )
    p_runs_resume = runs_sub.add_parser(
        "resume",
        help="Resume a durable child from its branch-point, or re-attach interrupted app-server workers.",
    )
    p_runs_resume.add_argument(
        "run",
        help="Run directory path, run id under --runs-dir, or 'latest'.",
    )
    p_runs_resume.add_argument(
        "--cwd",
        type=Path,
        default=Path("."),
        help="Workspace whose default .brigade/runs directory should be used for run ids.",
    )
    p_runs_resume.add_argument(
        "--runs-dir",
        type=Path,
        default=None,
        help="Explicit runs directory for run ids. Defaults to .brigade/runs under --cwd.",
    )
    p_runs_audit = runs_sub.add_parser(
        "audit",
        help=(
            "Offline coordinator decision audit from recorded lifecycle evidence. "
            "Exit 0 on match, 1 on divergence or corrupt journal evidence, "
            "2 when the run is not auditable or the run id cannot be resolved."
        ),
    )
    p_runs_audit.add_argument("run", help="Run directory path, run id under --runs-dir, or 'latest'.")
    p_runs_audit.add_argument(
        "--cwd",
        type=Path,
        default=Path("."),
        help="Workspace whose default .brigade/runs directory should be used for run ids.",
    )
    p_runs_audit.add_argument(
        "--runs-dir",
        type=Path,
        default=None,
        help="Explicit runs directory for run ids. Defaults to .brigade/runs under --cwd.",
    )
    p_runs_audit.add_argument("--json", action="store_true", help="Emit the audit receipt as JSON.")
    p_runs_export = runs_sub.add_parser(
        "export",
        help="Export a run directory as a versioned brigade.work-run archive.",
    )
    p_runs_export.add_argument("run", help="Run directory path, run id under --runs-dir, or 'latest'.")
    p_runs_export.add_argument(
        "--cwd",
        type=Path,
        default=Path("."),
        help="Workspace whose default .brigade/runs directory should be used for run ids.",
    )
    p_runs_export.add_argument(
        "--runs-dir",
        type=Path,
        default=None,
        help="Explicit runs directory for run ids. Defaults to .brigade/runs under --cwd.",
    )
    p_runs_export.add_argument(
        "--output",
        "-o",
        type=Path,
        required=True,
        help="Destination archive directory (created; must not exist unless --force).",
    )
    p_runs_export.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing destination archive directory.",
    )
    p_runs_export.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    p_runs_import = runs_sub.add_parser(
        "import",
        help="Validate a brigade.work-run archive and copy its payload into the runs directory.",
    )
    p_runs_import.add_argument("archive", type=Path, help="Path to a brigade.work-run archive directory.")
    p_runs_import.add_argument(
        "--cwd",
        type=Path,
        default=Path("."),
        help="Workspace whose default .brigade/runs directory receives the import.",
    )
    p_runs_import.add_argument(
        "--runs-dir",
        type=Path,
        default=None,
        help="Explicit runs directory. Defaults to .brigade/runs under --cwd.",
    )
    p_runs_import.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing destination run directory with the same run id.",
    )
    p_runs_import.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    p_runs_reap = runs_sub.add_parser(
        "reap",
        help="Terminalize local runs whose recorded owner process is gone.",
    )
    p_runs_reap.add_argument(
        "--cwd",
        type=Path,
        default=Path("."),
        help="Workspace whose default .brigade/runs directory should be scanned.",
    )
    p_runs_reap.add_argument(
        "--runs-dir",
        type=Path,
        default=None,
        help="Explicit runs directory. Defaults to .brigade/runs under --cwd.",
    )
    p_runs_reap.add_argument(
        "--older-than",
        default="2h",
        help="Only consider nonterminal runs older than this duration (default 2h).",
    )
    p_runs_reap.add_argument(
        "--json",
        action="store_true",
        help="Emit the versioned brigade.runs-reap.v1 JSON contract.",
    )
    p_runs_prune_worktrees = runs_sub.add_parser(
        "prune-worktrees",
        help="List or remove Brigade-created worktrees that are safe to delete.",
    )
    p_runs_prune_worktrees.add_argument(
        "--target",
        type=Path,
        default=Path("."),
        help="Repository or workspace root to inspect.",
    )
    p_runs_prune_worktrees.add_argument(
        "--older-than",
        type=int,
        default=14,
        help="Only consider worktrees older than this many days (default 14).",
    )
    p_runs_prune_worktrees.add_argument(
        "--apply",
        action="store_true",
        help="Actually remove removable worktrees; default is a dry run.",
    )
    p_runs_prune_worktrees.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    p_runs_validate_archive = runs_sub.add_parser(
        "validate-archive",
        help="Validate a brigade.work-run archive manifest, digests, and export privacy rules.",
    )
    p_runs_validate_archive.add_argument(
        "archive",
        type=Path,
        help="Path to a brigade.work-run archive directory.",
    )
    p_runs_validate_archive.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    p_runs.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from .. import runs_cmd

    if args.runs_command == "list":
        return runs_cmd.list_runs(cwd=args.cwd, runs_dir=args.runs_dir, limit=args.limit, json_output=args.json)
    if args.runs_command == "latest":
        return runs_cmd.show_latest(cwd=args.cwd, runs_dir=args.runs_dir, json_output=args.json)
    if args.runs_command == "show":
        run_dir, error = runs_cmd._resolve_run_dir(args.run, cwd=args.cwd, runs_dir=args.runs_dir)
        if error is not None:
            print(error, file=sys.stderr)
            return 2
        assert run_dir is not None
        return runs_cmd.show(run_dir, json_output=args.json)
    if args.runs_command == "inspect":
        return runs_cmd.inspect(args.run, cwd=args.cwd, runs_dir=args.runs_dir)
    if args.runs_command == "child":
        return runs_cmd.child(args.run, args.event_id, cwd=args.cwd, runs_dir=args.runs_dir)
    if args.runs_command == "diff":
        from .. import runs_diff

        return runs_diff.diff(
            args.run,
            args.other,
            cwd=args.cwd,
            runs_dir=args.runs_dir,
            json_output=args.json,
        )
    if args.runs_command == "watch":
        return runs_cmd.watch(
            args.run,
            cwd=args.cwd,
            runs_dir=args.runs_dir,
            json_output=args.json,
            interval=args.interval,
        )
    if args.runs_command == "serve":
        return runs_cmd.serve(
            cwd=args.cwd,
            runs_dir=args.runs_dir,
            port=args.port,
            open_browser=not args.no_open,
        )
    if args.runs_command == "events":
        return runs_cmd.events(
            args.run,
            cwd=args.cwd,
            runs_dir=args.runs_dir,
            after=args.after,
            follow=args.follow,
            interval=args.interval,
        )
    if args.runs_command == "steer":
        return _control_request(
            args.run,
            cwd=args.cwd,
            runs_dir=args.runs_dir,
            payload={"op": "steer", "worker": args.worker, "text": " ".join(args.text)},
            request_id=args.request_id,
        )
    if args.runs_command == "interrupt":
        payload = {"op": "interrupt"}
        if args.worker is not None:
            payload["worker"] = args.worker
        return _control_request(
            args.run,
            cwd=args.cwd,
            runs_dir=args.runs_dir,
            payload=payload,
            request_id=args.request_id,
        )
    if args.runs_command == "recover":
        return runs_cmd.recover(args.run, cwd=args.cwd, runs_dir=args.runs_dir)
    if args.runs_command == "redact":
        return runs_cmd.redact(
            args.run,
            cwd=args.cwd,
            runs_dir=args.runs_dir,
            sequence_start=args.sequence_start,
            sequence_end=args.sequence_end,
            reason=args.reason,
            operator_confirmed=args.operator_confirm,
            cleanup_operation=args.cleanup_operation,
        )
    if args.runs_command == "resume":
        run_dir, error = runs_cmd._resolve_run_dir(args.run, cwd=args.cwd, runs_dir=args.runs_dir)
        if error is not None:
            print(error, file=sys.stderr)
            return 2
        assert run_dir is not None
        return runs_cmd.resume(run_dir)
    if args.runs_command == "audit":
        return runs_cmd.audit(
            args.run,
            cwd=args.cwd,
            runs_dir=args.runs_dir,
            json_output=args.json,
        )
    if args.runs_command == "export":
        from .. import work_run_archive

        return work_run_archive.export_cli(
            args.run,
            cwd=args.cwd,
            runs_dir=args.runs_dir,
            output=args.output,
            force=args.force,
            json_output=args.json,
        )
    if args.runs_command == "import":
        from .. import work_run_archive

        return work_run_archive.import_cli(
            args.archive,
            cwd=args.cwd,
            runs_dir=args.runs_dir,
            force=args.force,
            json_output=args.json,
        )
    if args.runs_command == "reap":
        from .. import run_reap

        return run_reap.reap(
            cwd=args.cwd,
            runs_dir=args.runs_dir,
            older_than=args.older_than,
            json_output=args.json,
        )
    if args.runs_command == "prune-worktrees":
        from .. import runs_cmd_worktrees as _worktrees_cmd

        return _worktrees_cmd.prune_worktrees(
            cwd=args.target,
            older_than_days=args.older_than,
            apply=args.apply,
            json_output=args.json,
        )
    if args.runs_command == "validate-archive":
        from .. import work_run_archive

        return work_run_archive.validate_cli(args.archive, json_output=args.json)
    args._brigade_parser.error(f"unknown runs command: {args.runs_command}")
    return 2


def _control_request(
    run: str,
    *,
    cwd: Path,
    runs_dir: Path | None,
    payload: Mapping[str, object],
    request_id: str | None = None,
) -> int:
    import sys

    from .. import run_control, run_control_journal, runs_cmd

    run_dir, error = runs_cmd._resolve_run_dir(run, cwd=cwd, runs_dir=runs_dir)
    if error is not None:
        print(error, file=sys.stderr)
        return 2
    assert run_dir is not None

    # Legacy runs without a lifecycle journal keep the pre-#604 transport path.
    # An explicit --request-id cannot be honored without a journal.
    if not run_control_journal.journal_present(run_dir):
        if request_id is not None:
            print("error: legacy-no-journal", file=sys.stderr)
            return 2
        try:
            transport = run_control.control_transport_from_run(run_dir)
            response = run_control.send_request_with_retry(run_dir, transport, dict(payload))
        except run_control.ControlError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        return run_control.print_control_response(response, op=str(payload["op"]))

    op = str(payload["op"])
    worker = payload.get("worker")
    text = payload.get("text")
    try:
        transport = run_control.control_transport_from_run(run_dir)

        def _send(transport_payload: dict[str, object]) -> dict[str, object]:
            return run_control.send_request_with_retry(run_dir, transport, transport_payload)

        result = run_control_journal.execute_control_request(
            run_dir,
            op=op,
            request_id=request_id,
            worker=str(worker) if isinstance(worker, str) else None,
            text=str(text) if isinstance(text, str) else None,
            send=_send,
        )
    except run_control_journal.ControlJournalError as exc:
        print(f"error: {exc.code}: {exc}", file=sys.stderr)
        return 2
    except run_control.ControlError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"request_id: {result.request_id}")
    if result.replayed:
        print("control: replay")
    return run_control.print_control_response(result.response, op=op)
