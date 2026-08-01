"""brigade runs command group."""

from __future__ import annotations

import argparse
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
        "resume", help="Re-attach interrupted app-server workers from a run and re-synthesize."
    )
    p_runs_resume.add_argument("run_dir", type=Path, help="Path to a Brigade run artifact directory.")
    p_runs.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from .. import runs_cmd

    if args.runs_command == "list":
        return runs_cmd.list_runs(cwd=args.cwd, runs_dir=args.runs_dir, limit=args.limit)
    if args.runs_command == "latest":
        return runs_cmd.show_latest(cwd=args.cwd, runs_dir=args.runs_dir)
    if args.runs_command == "show":
        return runs_cmd.show(args.run_dir)
    if args.runs_command == "watch":
        return runs_cmd.watch(
            args.run,
            cwd=args.cwd,
            runs_dir=args.runs_dir,
            json_output=args.json,
            interval=args.interval,
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
        return runs_cmd.resume(args.run_dir)
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
