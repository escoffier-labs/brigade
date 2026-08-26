"""brigade run cloud — cloud dispatch registry commands (#890)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


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

    p_sync = cloud_sub.add_parser(
        "sync", help="Reconcile cloud observations with hub leases (read-only status is not mutating)."
    )
    p_sync.add_argument("--target", type=Path, default=Path("."))
    p_sync.add_argument("--json", action="store_true", help="Emit the sync JSON contract.")

    p_register = cloud_sub.add_parser("register", help="Register a dispatched cloud task.")
    p_register.add_argument("--target", type=Path, default=Path("."))
    p_register.add_argument(
        "--provider", required=True, choices=("codex-cloud", "cursor-cloud", "grokbot-cloud", "claude-cloud", "jules")
    )
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
    p_adopt.add_argument(
        "--provider", required=True, choices=("codex-cloud", "cursor-cloud", "grokbot-cloud", "claude-cloud", "jules")
    )
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

    p_setup = cloud_sub.add_parser("setup", help="Write local Codex Cloud environment configuration (no live task).")
    p_setup.add_argument("--target", type=Path, default=Path("."))
    p_setup.add_argument(
        "--provider", required=True, choices=("codex-cloud", "cursor-cloud", "grokbot-cloud", "claude-cloud", "jules")
    )
    setup_src = p_setup.add_mutually_exclusive_group(required=True)
    setup_src.add_argument("--env-var", help="Name of the environment variable holding the Codex Cloud env id.")
    setup_src.add_argument("--env-id", help="Store a Codex Cloud env id locally. Prefer --env-var.")

    p_doctor = cloud_sub.add_parser("doctor", help="Check Codex Cloud seat configuration without submitting a task.")
    p_doctor.add_argument("--target", type=Path, default=Path("."))
    p_doctor.add_argument(
        "--provider", required=True, choices=("codex-cloud", "cursor-cloud", "grokbot-cloud", "claude-cloud", "jules")
    )
    p_doctor.add_argument("--json", action="store_true")

    p_canary = cloud_sub.add_parser("canary", help="Bounded Codex Cloud inventory check. Never exec or apply.")
    p_canary.add_argument("--target", type=Path, default=Path("."))
    p_canary.add_argument(
        "--provider", required=True, choices=("codex-cloud", "cursor-cloud", "grokbot-cloud", "claude-cloud", "jules")
    )
    p_canary.add_argument("--json", action="store_true")

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

    p_feed = grokbot_sub.add_parser("feed", help="Validate or enqueue an approved Grok Bot feed manifest.")
    add_target(p_feed)
    p_feed.add_argument("--manifest", type=Path, required=True, help="Path to the approved private feed manifest.")
    p_feed.add_argument("--limit", type=int, default=1, help="Maximum newly created jobs (1-10). Defaults to 1.")
    p_feed.add_argument("--apply", action="store_true", help="Enqueue after validation. Default is validate only.")

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

    p_grokbot_serve = grokbot_sub.add_parser("serve", help="Run the role-scoped Grok Bot MCP listener.")
    p_grokbot_serve.add_argument("--target", type=Path, default=Path("."))
    p_grokbot_serve.add_argument(
        "--instance", required=True, choices=("operator", "repository-scout", "implementation-worker")
    )
    p_grokbot_serve.add_argument("--bind", default="127.0.0.1:8766", help="Listener host:port. Defaults to loopback.")
    p_grokbot_serve.add_argument("--allow-host", action="append", default=[], help="Explicit allowed Host value.")
    p_grokbot_serve.add_argument("--allow-origin", action="append", default=[], help="Explicit allowed Origin value.")
    secret_group = p_grokbot_serve.add_mutually_exclusive_group(required=True)
    secret_group.add_argument("--bearer-file", type=Path, help="Protected file containing the listener bearer.")
    secret_group.add_argument("--bearer-env", help="Environment variable name containing the listener bearer.")

    def add_instance(command: argparse.ArgumentParser) -> None:
        command.add_argument("--target", type=Path, default=Path("."))
        command.add_argument(
            "--instance", required=True, choices=("operator", "repository-scout", "implementation-worker")
        )
        command.add_argument("--json", action="store_true")

    p_setup = grokbot_sub.add_parser("setup", help="Write non-secret role-scoped listener configuration.")
    add_target(p_setup)
    p_setup.add_argument("--instance", required=True, choices=("operator", "repository-scout", "implementation-worker"))
    p_setup.add_argument("--bind", default="127.0.0.1:8766", help="Listener host:port. Defaults to loopback.")
    p_setup.add_argument("--allow-host", action="append", default=[], help="Explicit allowed Host value.")
    p_setup.add_argument("--allow-origin", action="append", default=[], help="Explicit allowed Origin value.")
    setup_secret = p_setup.add_mutually_exclusive_group(required=True)
    setup_secret.add_argument("--bearer-file", type=Path, help="Path of a protected bearer file (reference only).")
    setup_secret.add_argument("--bearer-env", help="Name of the environment variable holding the bearer.")

    p_doctor = grokbot_sub.add_parser(
        "doctor", help="Sanitized dependency/config/permission/queue/endpoint diagnostics."
    )
    add_instance(p_doctor)

    p_canary = grokbot_sub.add_parser("canary", help="Bounded non-mutating authentication and inventory check.")
    add_instance(p_canary)

    p_install = grokbot_sub.add_parser("install-service", help="Render or install a role-scoped systemd unit.")
    add_instance(p_install)
    p_install.add_argument("--out", type=Path, default=None, help="Directory to render the unit into.")
    p_install.add_argument("--force", action="store_true", help="Allow overwriting this role's own unit file.")

    p.set_defaults(func=dispatch)


def dispatch(args) -> int:
    command = getattr(args, "run_cloud_command", None)
    target = Path(args.target).expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2

    if command == "grokbot":
        return _dispatch_grokbot(args, target)
    if command in {"setup", "doctor", "canary"}:
        return _dispatch_codex_cloud_ops(args, target)

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

    if command == "sync":
        from .. import fleet_client

        provider_tasks, github, cursor_wired = cloud_tracker.observe_providers(target)
        hub_leases: list[dict[str, Any]] = []
        try:
            snapshot = fleet_client.fetch_cloud()
            leases = snapshot.get("leases")
            if isinstance(leases, list):
                hub_leases = leases
        except fleet_client.FleetClientError:
            # No hub configured or unreachable: keep sync purely observational.
            pass
        payload = cloud_tracker.sync_payload(
            target,
            provider_tasks=provider_tasks,
            github=github,
            cursor_wired=cursor_wired,
            hub_leases=hub_leases,
        )
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0
        print(f"cloud sync: {payload['target']}")
        print(f"action: {payload['action']}")
        print(
            f"active: {payload['counts']['active']} released: {payload['counts']['released']} needs_you: {payload['counts']['needs_you']}"
        )
        for row in payload.get("needs_you", []):
            print(f"  needs-you: {row.get('detail')}")
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


def _dispatch_codex_cloud_ops(args, target: Path) -> int:
    """Setup, doctor, and inventory canary for a Codex Cloud run seat."""
    from .. import codex_cloud

    if getattr(args, "provider", None) != "codex-cloud":
        print("error: setup, doctor, and canary currently support --provider codex-cloud", file=sys.stderr)
        return 2
    command = args.run_cloud_command
    if command == "setup":
        try:
            if args.env_var:
                codex_cloud.save_environment_config(target, environment_id_env=args.env_var)
            else:
                codex_cloud.save_environment_config(target, environment_id=args.env_id)
        except codex_cloud.CodexCloudConfigError:
            print("error: invalid Codex Cloud setup", file=sys.stderr)
            return 2
        print("codex-cloud config saved")
        return 0
    if command == "doctor":
        result = codex_cloud.doctor(target)
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            print(f"codex-cloud doctor: {'ok' if result['ok'] else 'fail'}")
            print(f"environment_configured: {'yes' if result['environment_configured'] else 'no'}")
        return 0 if result["ok"] else 1
    if command == "canary":
        result = codex_cloud.canary(target)
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            print(f"codex-cloud canary: {'ok' if result['ok'] else 'fail'}")
            print(f"environment_configured: {'yes' if result['environment_configured'] else 'no'}")
            print(f"task_count: {result['task_count']}")
            print("apply: no")
        return 0 if result["ok"] else 1
    print(f"error: unknown cloud command: {command}", file=sys.stderr)
    return 2


def _dispatch_grokbot(args, target: Path) -> int:
    """Dispatch local queue operations without loading envelopes into CLI arguments."""
    from .. import grokbot_jobs, grokbot_mcp

    command = args.grokbot_command
    if command in {"setup", "doctor", "canary", "install-service"}:
        return _dispatch_grokbot_ops(args, target)
    if command == "feed":
        return _dispatch_grokbot_feed(args, target)
    if command == "serve":
        from .. import grokbot_mcp

        try:
            config = grokbot_mcp.build_listener_config(
                target=target,
                instance=args.instance,
                bind=args.bind,
                allowed_hosts=args.allow_host,
                allowed_origins=args.allow_origin,
                bearer_file=args.bearer_file,
                bearer_env=args.bearer_env,
            )
            grokbot_mcp.run_listener(config)
        except grokbot_mcp.OptionalDependencyError:
            print("error: Grok Bot listener requires pip install brigade-cli[grokbot]", file=sys.stderr)
            return 2
        except grokbot_mcp.ConfigurationError:
            print("error: Grok Bot listener configuration is invalid", file=sys.stderr)
            return 2
        return 0
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


def _dispatch_grokbot_feed(args, target: Path) -> int:
    """Validate or apply an approved feed without printing private task context."""
    from .. import grokbot_feed

    try:
        if args.apply:
            result = grokbot_feed.apply(target, args.manifest, limit=args.limit)
        else:
            result = grokbot_feed.preflight(target, args.manifest, limit=args.limit)
    except grokbot_feed.FeedError as exc:
        if exc.reason == "queue-error" and exc.index is not None:
            print(f"error: queue-error index={exc.index}", file=sys.stderr)
        else:
            print(f"error: {exc.reason}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    _print_feed_result(result)
    return 0


def _print_feed_result(result: dict) -> None:
    """Render feed counts and safe job handles only."""
    parts = [f"valid={result['valid']}", f"known={result['known']}", f"limit={result['limit']}"]
    if "created" in result:
        parts.insert(2, f"created={result['created']}")
        parts.insert(3, f"skipped={result['skipped']}")
    print("grokbot feed: " + " ".join(parts))
    for job in result.get("jobs", []):
        print(f"job {job['job_id']} state={job['state']}")


def _dispatch_grokbot_ops(args, target: Path) -> int:
    """Setup, doctor, canary, and install-service for the Grok Bot listener."""
    from .. import grokbot_mcp, grokbot_ops

    instance = args.instance
    command = args.grokbot_command
    try:
        if command == "setup":
            grokbot_ops.save_config(
                target,
                instance=instance,
                bind=args.bind,
                allowed_hosts=args.allow_host,
                allowed_origins=args.allow_origin,
                bearer_env=args.bearer_env,
                bearer_file=args.bearer_file,
            )
            print(f"grokbot config saved: role={instance}")
            return 0

        if command == "doctor":
            checks = grokbot_ops.doctor(target, instance)
            failed = False
            for check in checks:
                print(f"{check['check']}: {check['status']}")
                failed = failed or check["status"] != "ok"
            return 1 if failed else 0

        if command == "canary":
            result = grokbot_ops.canary(target, instance)
            if args.json:
                print(json.dumps(result, indent=2, sort_keys=True))
            else:
                print(f"canary: {'ok' if result['ok'] else 'fail'}")
                if not result["ok"]:
                    print(f"reason: {result.get('reason', 'unknown')}")
            return 0 if result["ok"] else 1

        if command == "install-service":
            config = grokbot_ops.load_config(target, instance)
            if args.out is None:
                sys.stdout.write(grokbot_ops.render_unit(config, python=sys.executable, exec_root=target))
                return 0
            path = grokbot_ops.write_unit(config, Path(args.out), exec_root=target, force=args.force)
            print(f"unit written: {path.name}")
            return 0
    except grokbot_mcp.ConfigurationError:
        print("error: Grok Bot configuration is invalid", file=sys.stderr)
        return 2
    except grokbot_ops.ServiceRenderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print("error: Grok Bot operation failed", file=sys.stderr)
        del exc
        return 2
    return 2


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
