"""brigade run cloud — cloud dispatch registry commands (#890)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "run-cloud",
        help="Cloud dispatch registry (invoked as: brigade run cloud ...).",
    )
    cloud_sub = p.add_subparsers(dest="run_cloud_command", metavar="<cloud-command>")
    cloud_sub.required = True

    p_status = cloud_sub.add_parser("status", help="Classify registered cloud tasks against providers and GitHub.")
    p_status.add_argument("--target", type=Path, default=Path("."), help="Workspace with .brigade/cloud state.")
    p_status.add_argument("--json", action="store_true", help="Emit the status JSON contract.")
    p_status.add_argument(
        "--stale-ready-hours",
        type=int,
        default=None,
        help="Override and persist the stale-READY threshold in hours.",
    )

    p_register = cloud_sub.add_parser("register", help="Register a dispatched cloud task.")
    p_register.add_argument("--target", type=Path, default=Path("."))
    p_register.add_argument("--provider", required=True, choices=("codex-cloud", "cursor-cloud", "grokbot-cloud"))
    p_register.add_argument("--task-id", required=True)
    p_register.add_argument("--label", required=True)
    p_register.add_argument("--prompt-hash", default=None, help="sha256:... of the prompt; never store prompt text.")
    p_register.add_argument("--session-id", default=None)
    p_register.add_argument("--branch", default=None)
    p_register.add_argument(
        "--artifact-kind",
        choices=("diff", "branch"),
        default="diff",
        help="Expected artifact kind.",
    )
    p_register.add_argument("--json", action="store_true")

    p_adopt = cloud_sub.add_parser("adopt", help="Back-register an already-running task or orphaned branch.")
    p_adopt.add_argument("--target", type=Path, default=Path("."))
    p_adopt.add_argument("--provider", required=True, choices=("codex-cloud", "cursor-cloud", "grokbot-cloud"))
    p_adopt.add_argument("--task-id", default=None)
    p_adopt.add_argument("--branch", default=None)
    p_adopt.add_argument("--label", default=None)
    p_adopt.add_argument("--prompt-hash", default=None)
    p_adopt.add_argument("--session-id", default=None)
    p_adopt.add_argument("--json", action="store_true")

    p_sweep = cloud_sub.add_parser(
        "sweep",
        help="Receipted orphan/ready sweep REPORT only; nothing is deleted.",
    )
    p_sweep.add_argument("--target", type=Path, default=Path("."))
    p_sweep.add_argument("--json", action="store_true")

    p_grokbot = cloud_sub.add_parser("grokbot", help="Manage the private local Grok Bot job queue.")
    grokbot_sub = p_grokbot.add_subparsers(dest="grokbot_command", metavar="<grokbot-command>")
    grokbot_sub.required = True

    def add_target(command: argparse.ArgumentParser) -> None:
        command.add_argument("--target", type=Path, default=Path("."))
        command.add_argument("--json", action="store_true")

    p_enqueue = grokbot_sub.add_parser("enqueue", help="Queue a Grok Bot envelope from a JSON file.")
    add_target(p_enqueue)
    p_enqueue.add_argument("--spec", type=Path, required=True, help="Path to the private task envelope JSON file.")
    p_enqueue.add_argument("--idempotency-key", required=True)

    for name, help_text in (("claim", "Claim a queued job."), ("renew", "Renew a current job lease.")):
        command = grokbot_sub.add_parser(name, help=help_text)
        add_target(command)
        command.add_argument("--job-id", required=True)
        command.add_argument("--bot-id", required=True)
        command.add_argument("--lease-id", required=True)
        command.add_argument("--lease-seconds", required=True, type=int)

    for name, help_text in (
        ("start", "Mark a claimed job running."),
        ("fail", "Mark a live job failed."),
        ("ack-cancel", "Acknowledge a requested cancellation."),
    ):
        command = grokbot_sub.add_parser(name, help=help_text)
        add_target(command)
        command.add_argument("--job-id", required=True)
        command.add_argument("--bot-id", required=True)
        command.add_argument("--lease-id", required=True)

    p_complete = grokbot_sub.add_parser("complete", help="Complete a running job with artifact references.")
    add_target(p_complete)
    p_complete.add_argument("--job-id", required=True)
    p_complete.add_argument("--bot-id", required=True)
    p_complete.add_argument("--lease-id", required=True)
    p_complete.add_argument("--artifact", type=Path, required=True, help="Path to the completion artifact JSON file.")

    for name, help_text in (
        ("cancel", "Cancel a queued job or request cancellation."),
        ("expire", "Expire a deadline or lease-expired job."),
    ):
        command = grokbot_sub.add_parser(name, help=help_text)
        add_target(command)
        command.add_argument("--job-id", required=True)

    p_grokbot_status = grokbot_sub.add_parser("status", help="Show safe Grok Bot job projections.")
    add_target(p_grokbot_status)
    p_grokbot_status.add_argument("--job-id", default=None)

    p.set_defaults(func=dispatch)


def dispatch(args) -> int:
    command = getattr(args, "run_cloud_command", None)
    target = Path(args.target).expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2

    if command == "grokbot":
        return _dispatch_grokbot(args, target)

    from .. import cloud_tracker

    if command == "register":
        try:
            entry = cloud_tracker.register(
                target,
                provider=args.provider,
                task_id=args.task_id,
                label=args.label,
                prompt_hash=args.prompt_hash,
                session_id=args.session_id,
                branch=args.branch,
                expected_artifact=(
                    {"kind": "branch", "pattern": args.branch or "codex/*"}
                    if args.artifact_kind == "branch"
                    else {"kind": "diff"}
                ),
            )
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(entry, indent=2, sort_keys=True))
        else:
            print(f"registered {entry['id']} provider={entry['provider']} task_id={entry['task_id']}")
        return 0

    if command == "adopt":
        try:
            entry = cloud_tracker.adopt(
                target,
                provider=args.provider,
                task_id=args.task_id,
                branch=args.branch,
                label=args.label,
                prompt_hash=args.prompt_hash,
                session_id=args.session_id,
            )
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(entry, indent=2, sort_keys=True))
        else:
            print(f"adopted {entry['id']} source={entry['source']}")
        return 0

    if command == "status":
        if args.stale_ready_hours is not None:
            try:
                cloud_tracker.set_stale_ready_hours(target, args.stale_ready_hours)
            except ValueError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
        provider_tasks, github, cursor_wired = cloud_tracker.observe_providers(target)
        payload = cloud_tracker.status_payload(
            target,
            provider_tasks=provider_tasks,
            github=github,
            cursor_wired=cursor_wired,
        )
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0
        print(f"cloud status: {payload['target']}")
        print(f"stale_ready_hours: {payload['stale_ready_hours']}")
        for name, source in payload["sources"].items():
            wired = "wired" if source.get("wired") else "unwired"
            print(f"source {name}: {wired} ({source.get('authority')})")
        counts = payload.get("counts") or {}
        print("counts: " + ", ".join(f"{k}={counts.get(k, 0)}" for k in cloud_tracker.CLASSIFICATIONS))
        for row in payload.get("entries", []):
            print(
                f"  - [{row.get('classification')}] {row.get('label')} "
                f"provider={row.get('provider')} task={row.get('task_id')} branch={row.get('branch')}"
            )
        return 0

    if command == "sweep":
        provider_tasks, github, cursor_wired = cloud_tracker.observe_providers(target)
        status = cloud_tracker.status_payload(
            target,
            provider_tasks=provider_tasks,
            github=github,
            cursor_wired=cursor_wired,
        )
        report = cloud_tracker.sweep(target, status=status)
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
            return 0
        print(f"cloud sweep: {report['sweep_id']} action={report['action']}")
        print(f"recoverable: {len(report['recoverable'])}")
        print(f"deletable: {len(report['deletable'])}")
        print(report["note"])
        return 0

    print(f"error: unknown cloud command: {command}", file=sys.stderr)
    return 2


def _dispatch_grokbot(args, target: Path) -> int:
    """Dispatch local queue operations without loading envelopes into CLI arguments."""
    from .. import grokbot_jobs

    command = args.grokbot_command
    try:
        if command == "enqueue":
            result = grokbot_jobs.enqueue(target, _read_json_object(args.spec, "--spec"), args.idempotency_key)
        elif command == "claim":
            result = grokbot_jobs.claim(target, args.job_id, args.bot_id, args.lease_id, args.lease_seconds)
        elif command == "renew":
            result = grokbot_jobs.renew(target, args.job_id, args.bot_id, args.lease_id, args.lease_seconds)
        elif command == "start":
            result = grokbot_jobs.transition(target, args.job_id, args.bot_id, args.lease_id, "running")
        elif command == "complete":
            result = grokbot_jobs.transition(
                target,
                args.job_id,
                args.bot_id,
                args.lease_id,
                "completed",
                artifact=_read_json_object(args.artifact, "--artifact"),
            )
        elif command == "fail":
            result = grokbot_jobs.transition(target, args.job_id, args.bot_id, args.lease_id, "failed")
        elif command == "cancel":
            result = grokbot_jobs.cancel(target, args.job_id)
        elif command == "ack-cancel":
            result = grokbot_jobs.acknowledge_cancel(target, args.job_id, args.bot_id, args.lease_id)
        elif command == "expire":
            result = grokbot_jobs.expire(target, args.job_id)
        elif command == "status":
            result = grokbot_jobs.status(target, args.job_id)
        else:  # argparse makes this unreachable.
            print(f"error: unknown Grok Bot command: {command}", file=sys.stderr)
            return 2
    except grokbot_jobs.GrokbotJobError as exc:
        print(f"error: {exc.reason}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    _print_grokbot_result(result)
    return 0


def _read_json_object(path: Path, option: str) -> dict:
    """Load a command payload only from a regular JSON file."""
    if not path.is_file():
        raise ValueError(f"{option} is not a file: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON object in {option}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"invalid JSON object in {option}")
    return payload


def _print_grokbot_result(result: dict) -> None:
    """Render safe queue projections without exposing task envelope content."""
    if "jobs" in result:
        jobs = result["jobs"]
        print(f"grokbot jobs: {len(jobs)}")
        for job in jobs:
            print(f"job {job['job_id']} state={job['state']}")
        return
    print(f"job {result['job_id']} state={result['state']}")
