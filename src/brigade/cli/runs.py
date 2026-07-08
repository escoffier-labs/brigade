"""brigade runs command group."""

from __future__ import annotations

import argparse
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
    if args.runs_command == "steer":
        return _control_request(
            args.run,
            cwd=args.cwd,
            runs_dir=args.runs_dir,
            payload={"op": "steer", "worker": args.worker, "text": " ".join(args.text)},
        )
    if args.runs_command == "interrupt":
        payload = {"op": "interrupt"}
        if args.worker is not None:
            payload["worker"] = args.worker
        return _control_request(args.run, cwd=args.cwd, runs_dir=args.runs_dir, payload=payload)
    if args.runs_command == "resume":
        from .. import run_resume

        return run_resume.resume(args.run_dir)
    args._brigade_parser.error(f"unknown runs command: {args.runs_command}")
    return 2


def _control_request(run: str, *, cwd: Path, runs_dir: Path | None, payload: dict[str, object]) -> int:
    import sys

    from .. import run_control, runs_cmd

    run_dir, error = runs_cmd._resolve_run_dir(run, cwd=cwd, runs_dir=runs_dir)
    if error is not None:
        print(error, file=sys.stderr)
        return 2
    assert run_dir is not None
    try:
        socket_path = run_control.control_socket_from_run(run_dir)
        response = run_control.send_request(socket_path, payload)
    except run_control.ControlError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return run_control.print_control_response(response, op=str(payload["op"]))
