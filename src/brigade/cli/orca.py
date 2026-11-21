"""Caller-mediated governed Orca delegation."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any


def _target(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--target", "-t", type=Path, default=Path("."), help="Workspace that owns the run.")


def _run(parser: argparse.ArgumentParser, *, task: bool = True) -> None:
    _target(parser)
    parser.add_argument("run_id", help="Brigade run identity from `orca open`.")
    if task:
        parser.add_argument("task_id", help="Brigade task identity from `orca open`.")


def _receipt(parser: argparse.ArgumentParser, *, required: bool = False) -> None:
    parser.add_argument("--native-json", required=required, help="Orca JSON file or `-` for standard input.")
    parser.add_argument("--exit-code", type=int, choices=range(256), metavar="CODE", help="Orca CLI exit code.")


def register(sub: argparse._SubParsersAction) -> None:
    orca = sub.add_parser("orca", help="Enroll and govern local workers for an Orca coordinator.")
    commands = orca.add_subparsers(dest="orca_command", metavar="<orca-command>", required=True)

    opened = commands.add_parser("open", help="Enroll a validated batch. The coordinator runs Orca.")
    _target(opened)
    opened.add_argument("batch_file", type=Path, help="Versioned brigade.run_batch.v1 JSON file.")
    opened.add_argument("--roster", type=Path, help="Optional roster TOML path.")

    claim = commands.add_parser(
        "claim",
        help="Reserve one launch and print replay-safe worker-start argv.",
        description="Run the returned argv from the Orca coordinator for this workspace, Run, and existing Task.",
    )
    _run(claim)
    claim.add_argument("--orca-run", required=True, help="Existing Orca Run for this workspace's coordinator.")
    claim.add_argument("--orca-task", required=True, help="Existing Orca Task in that Run with the enrolled brief.")

    accepted = commands.add_parser(
        "accepted",
        help="Record a full worker-start receipt or a definite refusal for the claim.",
        description=(
            "If status shows a start refusal without a result ID after an interrupted write, "
            "re-run accepted with the matching refusal and its exit code."
        ),
    )
    _run(accepted)
    _receipt(accepted, required=True)

    hold = commands.add_parser("hold-resolve", help="Assert a recovered launch or abandon it while retaining capacity.")
    _run(hold)
    _receipt(hold)
    hold.add_argument("--abandon", action="store_true")
    hold.add_argument("--show-json", help="Full worker-show JSON for recovering an outcome_unknown start.")

    observe = commands.add_parser(
        "observe",
        help="Record worker-show evidence and read completion mail with inbox --full.",
        description=(
            "Record full worker-show JSON. Read completion mail with orca-ide orchestration inbox --full. "
            "A missing worker_done leaves completion pending with result-transfer-missing. "
            "After release, supply a fresh worker-show to update capacity for a result already retained."
        ),
    )
    _run(observe)
    observe.add_argument("--native-json", required=True, help="worker-show --json --full file, or `-`.")
    observe.add_argument("--worker-done-json", help="Orca inbox --full --json output file, or `-`.")
    observe.add_argument("--transcript-json", help="Orca worker-read --source transcript --json output file, or `-`.")

    cancel = commands.add_parser("cancel", help="Request worker-stop or record its receipt and full worker-show JSON.")
    _run(cancel)
    _receipt(cancel)
    cancel.add_argument("--show-json", help="Correlated worker-show --json --full output file, or `-`.")

    ack = commands.add_parser("ack", help="Acknowledge one exact terminal result.")
    _run(ack)
    ack.add_argument("--result-id", required=True, help="Terminal result identity from accepted, observe, or cancel.")

    finalize = commands.add_parser("finalize", help="Close the run after claimed workers have settled and released.")
    _run(finalize, task=False)
    status = commands.add_parser("status", help="Show redacted task state and workspace-wide capacity.")
    _run(status, task=False)
    status.add_argument("--json", action="store_true", help="Print the machine-readable report.")
    orca.set_defaults(func=dispatch)


def _dispatch(args: Any) -> dict[str, Any]:
    from .. import native_orca_enrollment, native_orca_lifecycle, native_orca_verbs
    from ..native_child_cli import read_native
    from ..native_child_journal import refuse

    def read(path: str | None) -> object:
        return None if path is None else read_native(path)

    inputs = ("native_json", "show_json", "worker_done_json", "transcript_json")
    if sum(getattr(args, field, None) == "-" for field in inputs) > 1:
        raise refuse("native-invalid", "only one JSON input can read from standard input")
    workspace = args.target
    verb = args.orca_command
    if verb == "open":
        return native_orca_enrollment.enroll(workspace=workspace, batch_path=args.batch_file, roster_path=args.roster)
    run = {"workspace": workspace, "run_id": args.run_id}
    if verb == "finalize":
        return native_orca_lifecycle.finalize(**run)
    if verb == "status":
        return native_orca_lifecycle.status(**run)
    task = {**run, "task_id": args.task_id}
    if verb == "claim":
        return native_orca_verbs.claim(**task, orca_run=args.orca_run, orca_task=args.orca_task)
    if verb == "ack":
        return native_orca_lifecycle.ack(**task, result_id=args.result_id)
    if verb == "observe":
        return native_orca_lifecycle.observe(
            **task,
            payload=read(args.native_json),
            worker_done=read(args.worker_done_json),
            transcript=read(args.transcript_json),
        )
    reporting = args.native_json is not None
    show = getattr(args, "show_json", None)
    if (args.exit_code is not None) != reporting or (show is not None and not reporting):
        raise refuse("native-invalid", "supply the native JSON file and Orca exit code together")
    payload = read(args.native_json)
    exit_code = args.exit_code if reporting else 0
    if verb == "accepted":
        return native_orca_verbs.accepted(**task, payload=payload, exit_code=exit_code)
    if verb == "hold-resolve":
        if args.abandon == reporting:
            raise refuse("native-invalid", "supply exactly one of --abandon or the native start receipt")
        return native_orca_verbs.hold_resolve(
            **task, payload=payload, abandon=args.abandon, exit_code=exit_code, show=read(show)
        )
    if verb == "cancel":
        return native_orca_lifecycle.cancel(
            **task, payload=payload, reporting=reporting, exit_code=exit_code, show=read(show)
        )
    raise AssertionError("unregistered Orca verb")


def _describe(report: dict[str, Any]) -> None:
    capacity = report["capacity"]
    live = capacity["live"] if capacity["live"] is not None else "unknown"
    limit = capacity["max_workers"] if capacity["max_workers"] is not None else "unknown"
    print(f"{report['run_id']} {report['status']} (workspace workers {live}/{limit})")
    for task in report["tasks"]:
        reason = task["capacity_reason"]
        refusal = task.get("start_refusal")
        disposition = f" ({refusal})" if refusal is not None else ""
        capacity_note = f" (holds capacity: {reason})" if reason is not None else ""
        print(f"  {task['task_id']} {task['state']}{disposition}{capacity_note}")


def dispatch(args: Any) -> int:
    from ..native_child_cli import emit
    from ..native_orca_host import UNSUPPORTED

    if args.orca_command == "status" and not args.json:
        return emit(lambda: _dispatch(args), output=_describe)
    return emit(
        lambda: _dispatch(args), recovery={UNSUPPORTED: "Submit unsupported work separately with brigade run batch."}
    )
