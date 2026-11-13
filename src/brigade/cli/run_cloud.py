"""brigade run cloud — cloud dispatch registry commands (#890)."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

_PROMPT_FILE_MAX_BYTES = 64 * 1024
_LAUNCH_LABEL_MAX = 120


def add_cloud_subcommands(parser: argparse.ArgumentParser) -> None:
    """Attach the cloud dispatch subcommands to `run cloud`."""
    cloud_sub = parser.add_subparsers(dest="run_cloud_command", metavar="<cloud-command>")
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

    p_launch = cloud_sub.add_parser(
        "launch",
        help=(
            "Launch a Codex Cloud or Cursor Cloud session from a private prompt file. "
            "Claude cloud sessions are launched by the conductor through routines, never here."
        ),
    )
    p_launch.add_argument("--target", type=Path, default=Path("."), help="Workspace with .brigade/cloud state.")
    p_launch.add_argument("--provider", required=True, choices=("codex-cloud", "cursor-cloud", "claude-cloud"))
    p_launch.add_argument("--repo", required=True, help="GitHub owner/repo or https://github.com/owner/repo URL.")
    p_launch.add_argument("--label", required=True, help="Registry label. Prompt text is never stored.")
    p_launch.add_argument(
        "--prompt-file",
        type=Path,
        required=True,
        help="Bounded private prompt file. Prompt text is never an argv value.",
    )
    p_launch.add_argument(
        "--auto-create-pr",
        action="store_true",
        help="Opt in to provider autoCreatePR. Default is off. Rejected for Codex Cloud.",
    )
    p_launch.add_argument("--json", action="store_true", help="Emit the public launch JSON contract.")

    p_land = cloud_sub.add_parser(
        "land",
        help="Apply a ready Codex Cloud task in isolation and open a PR for review.",
        description=(
            "Apply one pinned task attempt for review. Target PR CI must give proposal code no secrets, "
            "write token or trusted caches. Staging-fork mode is tracked in #2512."
        ),
    )
    p_land.add_argument("--target", type=Path, default=Path("."))
    p_land.add_argument("--provider", required=True, choices=("codex-cloud",))
    p_land.add_argument("--task-id", required=True)
    p_land.add_argument("--issue", required=True, type=int)
    p_land.add_argument("--closes", action="store_true", help="Use Closes #N instead of Refs #N in the PR body.")
    p_land.add_argument("--json", action="store_true")

    p_register = cloud_sub.add_parser("register", help="Register a dispatched cloud task.")
    p_register.add_argument("--target", type=Path, default=Path("."))
    p_register.add_argument(
        "--provider", required=True, choices=("codex-cloud", "cursor-cloud", "grokbot-cloud", "claude-cloud")
    )
    p_register.add_argument("--task-id", default=None)
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
        "--provider", required=True, choices=("codex-cloud", "cursor-cloud", "grokbot-cloud", "claude-cloud")
    )
    p_adopt.add_argument("--task-id", default=None)
    p_adopt.add_argument("--branch", default=None)
    p_adopt.add_argument("--label", default=None)
    p_adopt.add_argument("--prompt-hash", default=None)
    p_adopt.add_argument("--session-id", default=None)
    p_adopt.add_argument("--json", action="store_true")
    for command_parser in (p_register, p_adopt):
        command_parser.add_argument("--repo", default=None, help="Explicit Claude GitHub owner/repo binding.")
        command_parser.add_argument("--commit", default=None, help="Claude immutable full Git commit SHA.")
        command_parser.add_argument("--pr", default=None, help="Claude PR number or canonical GitHub PR URL.")
        command_parser.add_argument(
            "--routine-id", default=None, help="Claude routine ID (trig_...) that launched the session."
        )
        command_parser.add_argument(
            "--issue", default=None, help="Claude issue number or issue URL in the bound repository."
        )
        command_parser.add_argument(
            "--session-url", default=None, help="Claude session URL when it differs from the session ID form."
        )
        command_parser.add_argument(
            "--observed-status",
            default=None,
            help="Run status word the conductor read from the routine run. Requires --routine-id.",
        )

    p_sweep = cloud_sub.add_parser(
        "sweep",
        help="Receipted orphan/ready sweep REPORT only; nothing is deleted.",
    )
    p_sweep.add_argument("--target", type=Path, default=Path("."))
    p_sweep.add_argument("--json", action="store_true")

    p_compact = cloud_sub.add_parser(
        "compact",
        help="Explicit registry maintenance. Never runs as a side effect of status.",
    )
    p_compact.add_argument("--target", type=Path, default=Path("."))
    p_compact.add_argument("--json", action="store_true")
    p_compact.add_argument(
        "--keep-terminal",
        type=int,
        default=None,
        help="Newest landed/terminal rows to keep (default 50).",
    )
    p_compact.add_argument(
        "--max-age-hours",
        type=int,
        default=None,
        help="Drop landed/terminal rows older than this many hours (default 168).",
    )

    p_setup = cloud_sub.add_parser("setup", help="Write local Codex Cloud environment configuration (no live task).")
    p_setup.add_argument("--target", type=Path, default=Path("."))
    p_setup.add_argument("--provider", required=True, choices=("codex-cloud",))
    setup_src = p_setup.add_mutually_exclusive_group(required=True)
    setup_src.add_argument("--env-var", help="Name of the environment variable holding the Codex Cloud env id.")
    setup_src.add_argument("--env-id", help="Store a Codex Cloud env id locally. Prefer --env-var.")

    p_doctor = cloud_sub.add_parser("doctor", help="Check one cloud provider without submitting a task.")
    p_doctor.add_argument("--target", type=Path, default=Path("."))
    p_doctor.add_argument(
        "--provider", required=True, choices=("codex-cloud", "cursor-cloud", "grokbot-cloud", "claude-cloud")
    )
    p_doctor.add_argument("--json", action="store_true")
    p_doctor.add_argument(
        "--selector",
        default="configured",
        help="Codex Cloud environment selector to validate (Codex Cloud only).",
    )

    p_canary = cloud_sub.add_parser("canary", help="Bounded cloud inventory check. Never exec or apply.")
    p_canary.add_argument("--target", type=Path, default=Path("."))
    p_canary.add_argument(
        "--provider", required=True, choices=("codex-cloud", "cursor-cloud", "grokbot-cloud", "claude-cloud")
    )
    p_canary.add_argument("--json", action="store_true")
    p_canary.add_argument(
        "--selector",
        default="configured",
        help="Codex Cloud environment selector to validate (Codex Cloud only).",
    )

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
    p_feed.add_argument(
        "--apply",
        action="store_true",
        help="Enqueue after validation. Default is validate only. A private wake webhook is notified after each new job.",
    )

    p_scout_feed = grokbot_sub.add_parser(
        "scout-feed", help="Select one approved GitHub issue for Grok Bot Repository Scout."
    )
    add_target(p_scout_feed)
    p_scout_feed.add_argument("--policy", type=Path, required=True, help="Path to the approved private scout policy.")
    p_scout_feed.add_argument(
        "--apply",
        action="store_true",
        help="Enqueue after selection. Default is preview only. A private wake webhook is notified after a new job.",
    )

    grokbot_sub.add_parser("build-feed", help="Removed job feed.")

    p_reconcile = grokbot_sub.add_parser(
        "reconcile-reports",
        help="Preview or draft canonical-owner Memory Handoffs from completed scout reports.",
    )
    add_target(p_reconcile)
    p_reconcile.add_argument("--owner", type=Path, required=True, help="Owner workspace that receives handoff drafts.")
    p_reconcile.add_argument(
        "--inbox",
        default="review",
        help="Owner-relative review or writer inbox. Defaults to the owner's review inbox.",
    )
    p_reconcile.add_argument("--limit", type=int, default=1, help="Maximum new drafts (1-50). Defaults to 1.")
    p_reconcile.add_argument("--apply", action="store_true", help="Write drafts and markers. Default is preview only.")

    p_findings = grokbot_sub.add_parser(
        "reconcile-findings",
        help="Preview or draft canonical-owner Memory Handoffs from a private findings manifest.",
    )
    add_target(p_findings)
    p_findings.add_argument("--owner", type=Path, required=True, help="Owner workspace that receives handoff drafts.")
    p_findings.add_argument("--manifest", type=Path, required=True, help="Path to the private findings manifest.")
    p_findings.add_argument("--limit", type=int, default=1, help="Maximum new drafts (1-50). Defaults to 1.")
    p_findings.add_argument("--apply", action="store_true", help="Write drafts and markers. Default is preview only.")

    p_convert = grokbot_sub.add_parser(
        "convert-findings",
        help="Convert live normalized findings into a private generic manifest.",
    )
    add_target(p_convert)
    p_convert.add_argument("--input", type=Path, required=True, help="Path to the live normalized findings file.")
    p_convert.add_argument("--output", type=Path, required=True, help="Path to write the generic findings manifest.")

    p_relay = grokbot_sub.add_parser(
        "relay-findings",
        help="Preview or relay private findings to Fleet Hub and owner-review drafts.",
    )
    add_target(p_relay)
    p_relay.add_argument("--owner", type=Path, required=True, help="Owner workspace that receives handoff drafts.")
    p_relay.add_argument("--manifest", type=Path, required=True, help="Path to the private findings manifest.")
    p_relay.add_argument("--limit", type=int, default=1, help="Maximum new drafts (1-50). Defaults to 1.")
    p_relay.add_argument(
        "--apply", action="store_true", help="Write drafts and report events. Default is preview only."
    )

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
    serve_identity = p_grokbot_serve.add_mutually_exclusive_group(required=True)
    serve_identity.add_argument("--instance", choices=("operator", "repository-scout", "implementation-worker"))
    serve_identity.add_argument(
        "--pack",
        choices=(
            "operations-relay",
            "dot",
            "duet",
        ),
    )
    p_grokbot_serve.add_argument(
        "--bind", default=None, help="Listener host:port. Defaults to the role or pack loopback."
    )
    p_grokbot_serve.add_argument("--allow-host", action="append", default=[], help="Explicit allowed Host value.")
    p_grokbot_serve.add_argument("--allow-origin", action="append", default=[], help="Explicit allowed Origin value.")
    secret_group = p_grokbot_serve.add_mutually_exclusive_group(required=False)
    secret_group.add_argument("--bearer-file", type=Path, help="Protected file containing the listener bearer.")
    secret_group.add_argument("--bearer-env", help="Environment variable name containing the listener bearer.")

    def add_client_id(command: argparse.ArgumentParser) -> None:
        command.add_argument(
            "--client-id",
            default=None,
            help="Optional lowercase deployment identifier (1-64 characters); must match the hub credential node ID.",
        )

    add_client_id(p_grokbot_serve)

    def add_instance(command: argparse.ArgumentParser) -> None:
        command.add_argument("--target", type=Path, default=Path("."))
        command.add_argument(
            "--instance", required=True, choices=("operator", "repository-scout", "implementation-worker")
        )
        command.add_argument("--json", action="store_true")
        add_client_id(command)

    p_setup = grokbot_sub.add_parser("setup", help="Write non-secret role-scoped listener configuration.")
    add_target(p_setup)
    add_client_id(p_setup)
    p_setup.add_argument("--instance", required=True, choices=("operator", "repository-scout", "implementation-worker"))
    p_setup.add_argument("--bind", default="127.0.0.1:8766", help="Listener host:port. Defaults to loopback.")
    p_setup.add_argument("--allow-host", action="append", default=[], help="Explicit allowed Host value.")
    p_setup.add_argument("--allow-origin", action="append", default=[], help="Explicit allowed Origin value.")
    setup_secret = p_setup.add_mutually_exclusive_group(required=True)
    setup_secret.add_argument("--bearer-file", type=Path, help="Path of a protected bearer file (reference only).")
    setup_secret.add_argument("--bearer-env", help="Name of the environment variable holding the bearer.")
    p_setup.add_argument(
        "--hub-token-file",
        type=Path,
        default=None,
        help="Protected absolute Fleet Hub node-token file for this worker role (reference only).",
    )

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

    from ..dot import legacy_registry as grokbot_packs

    pack_ids = tuple(pack["id"] for pack in grokbot_packs.packaged_manifests())
    p_pack = grokbot_sub.add_parser("pack", help="Inspect and manage first-party Grok Bot connector packs.")
    pack_sub = p_pack.add_subparsers(dest="grokbot_pack_command", metavar="<pack-command>")
    pack_sub.required = True

    def add_pack_id(command: argparse.ArgumentParser) -> None:
        command.add_argument("--target", type=Path, default=Path("."))
        command.add_argument("--id", required=True, choices=pack_ids, dest="pack_id")
        command.add_argument("--json", action="store_true")

    p_pack_list = pack_sub.add_parser("list", help="List packaged first-party connector packs.")
    add_target(p_pack_list)

    p_pack_show = pack_sub.add_parser("show", help="Show one packaged connector pack.")
    add_pack_id(p_pack_show)

    p_pack_setup = pack_sub.add_parser("setup", help="Preview or apply non-secret pack instance configuration.")
    add_pack_id(p_pack_setup)
    p_pack_setup.add_argument("--bind", default=None, help="Listener host:port. Defaults to the pack loopback port.")
    p_pack_setup.add_argument("--allow-host", action="append", default=[], help="Explicit allowed Host value.")
    p_pack_setup.add_argument("--allow-origin", action="append", default=[], help="Explicit allowed Origin value.")
    pack_secret = p_pack_setup.add_mutually_exclusive_group(required=False)
    pack_secret.add_argument("--bearer-file", type=Path, help="Path of a protected bearer file (reference only).")
    pack_secret.add_argument("--bearer-env", help="Name of the environment variable holding the bearer.")
    p_pack_setup.add_argument(
        "--runtime-path",
        type=Path,
        default=None,
        help="Absolute runtime JSON path. Required for dot and duet.",
    )
    p_pack_setup.add_argument(
        "--owner",
        type=Path,
        default=None,
        dest="owner_workspace",
        help="Absolute owner workspace. Required for operations-relay.",
    )
    p_pack_setup.add_argument("--apply", action="store_true", help="Write local config. Default is preview only.")

    p_pack_doctor = pack_sub.add_parser("doctor", help="Sanitized pack diagnostics.")
    add_pack_id(p_pack_doctor)
    p_pack_doctor.add_argument(
        "--service-result",
        action="store_true",
        help="Inspect the Brigade-owned service unit Result. Does not start or mutate systemd.",
    )

    p_pack_canary = pack_sub.add_parser("canary", help="Bounded non-mutating pack authentication and inventory check.")
    add_pack_id(p_pack_canary)

    p_pack_install = pack_sub.add_parser("install-service", help="Render or install a pack-scoped systemd unit.")
    add_pack_id(p_pack_install)
    p_pack_install.add_argument("--out", type=Path, default=None, help="Directory to render the unit into.")
    p_pack_install.add_argument("--force", action="store_true", help="Allow overwriting this pack's own unit file.")

    p_pack_update = pack_sub.add_parser("update", help="Preview or apply a compatible pack version update.")
    add_pack_id(p_pack_update)
    p_pack_update.add_argument("--apply", action="store_true", help="Write local config. Default is preview only.")

    p_pack_remove = pack_sub.add_parser("remove", help="Preview or apply removal of owned pack config and unit files.")
    add_pack_id(p_pack_remove)
    p_pack_remove.add_argument(
        "--out", type=Path, default=None, help="Owned unit directory previously written by install-service."
    )
    p_pack_remove.add_argument("--apply", action="store_true", help="Delete owned files. Default is preview only.")

    p_pack_relay_setup = pack_sub.add_parser(
        "relay-setup",
        help="Preview or apply first-party finding-relay owner configuration.",
    )
    add_target(p_pack_relay_setup)
    p_pack_relay_setup.add_argument("--owner", type=Path, required=True, help="Absolute owner workspace.")
    p_pack_relay_setup.add_argument("--apply", action="store_true", help="Write local config. Default is preview only.")

    p_pack_relay_doctor = pack_sub.add_parser("relay-doctor", help="Sanitized first-party finding-relay diagnostics.")
    add_target(p_pack_relay_doctor)

    p_pack_relay = pack_sub.add_parser(
        "relay",
        help="Preview or relay first-party steward findings to owner review and Fleet Hub.",
    )
    add_target(p_pack_relay)
    p_pack_relay.add_argument("--limit", type=int, default=1, help="Maximum new drafts (1-50). Defaults to 1.")
    p_pack_relay.add_argument(
        "--apply", action="store_true", help="Write drafts and report events. Default is preview only."
    )

    p_pack_install_relay = pack_sub.add_parser(
        "install-relay-service",
        help="Render or install the first-party findings-relay systemd units.",
    )
    add_target(p_pack_install_relay)
    p_pack_install_relay.add_argument("--out", type=Path, default=None, help="Directory to render the units into.")
    p_pack_install_relay.add_argument("--force", action="store_true", help="Allow overwriting these relay unit files.")

    parser.set_defaults(func=dispatch)


def dispatch(args) -> int:
    command = getattr(args, "run_cloud_command", None)
    target = Path(args.target).expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2

    if command == "grokbot":
        return _dispatch_grokbot(args, target)
    if command in {"setup", "doctor", "canary"}:
        return _dispatch_cloud_ops(args, target)

    from .. import cloud_tracker

    if command == "land":
        from .. import codex_cloud_land

        try:
            result = (
                codex_cloud_land.land(target, args.task_id, args.issue, closes=True)
                if args.closes
                else codex_cloud_land.land(target, args.task_id, args.issue)
            )
        except codex_cloud_land.LandError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(result) if args.json else result["pr_url"])
        return 0

    if command == "launch":
        return _dispatch_launch(args, target)

    if command == "register":
        if not args.task_id and not (args.provider == "claude-cloud" and args.repo and args.session_id):
            print("error: register requires --task-id (or Claude --session-id and --repo)", file=sys.stderr)
            return 2
        try:
            entry = None
            if args.provider == "codex-cloud":
                from .. import codex_cloud

                codex_cloud.require_hub_lease_storage()
                entry = next(
                    (
                        row
                        for row in cloud_tracker.load_registry(target)["entries"]
                        if row.get("provider") == "codex-cloud" and row.get("task_id") == args.task_id
                    ),
                    None,
                )
            if entry is None:
                entry = cloud_tracker.register(
                    target,
                    provider=args.provider,
                    task_id=args.task_id,
                    label=args.label,
                    prompt_hash=args.prompt_hash,
                    session_id=args.session_id,
                    repo=args.repo,
                    commit=args.commit,
                    pr=args.pr,
                    routine_id=args.routine_id,
                    issue=args.issue,
                    session_url=args.session_url,
                    observed_status=args.observed_status,
                    branch=args.branch,
                    expected_artifact=(
                        {"kind": "branch", "pattern": args.branch or "codex/*"}
                        if args.artifact_kind == "branch"
                        else {"kind": "diff"}
                    ),
                )
            if args.provider == "codex-cloud":
                from .. import codex_cloud

                codex_cloud.acquire_registered_task_lease(
                    target,
                    task_id=str(entry["task_id"]),
                    label=str(entry["label"]),
                    prompt_hash=entry.get("prompt_hash"),
                    repo=args.repo or codex_cloud._repo_from_cwd(target),
                )
                entry = {key: value for key, value in entry.items() if key != "lease_holder"}
        except (ValueError, OSError, RuntimeError) as exc:
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
                repo=args.repo,
                commit=args.commit,
                pr=args.pr,
                routine_id=args.routine_id,
                issue=args.issue,
                session_url=args.session_url,
                observed_status=args.observed_status,
            )
        except (ValueError, OSError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(entry, indent=2, sort_keys=True))
        else:
            print(f"adopted {entry['id']} source={entry['source']}")
        return 0

    if command == "sync":
        from .. import fleet_client

        observations, github = cloud_tracker.observe_provider_details(target)
        provider_tasks = cloud_tracker._legacy_provider_tasks(observations)  # noqa: SLF001 - compatibility projection
        cursor_wired = observations["cursor-cloud"].configured
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
            provider_observations=observations,
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
        for row in payload.get("prs", []):
            print(f"  pr: task={row.get('task_id')} {row['pr_url']}")
        for row in payload.get("pending_landings", []):
            print(
                f"  landing: task={row.get('task_id')} state={row.get('landing_state')} commit={row.get('landing_commit')}"
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
        observations, github = cloud_tracker.observe_provider_details(target)
        provider_tasks = cloud_tracker._legacy_provider_tasks(observations)  # noqa: SLF001 - compatibility projection
        cursor_wired = observations["cursor-cloud"].configured
        payload = cloud_tracker.status_payload(
            target,
            provider_tasks=provider_tasks,
            github=github,
            cursor_wired=cursor_wired,
            provider_observations=observations,
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
            url = row.get("pr_url") or row.get("url")
            extra = f" url={url}" if url else ""
            if row.get("landing_state"):
                extra += f" landing={row['landing_state']} commit={row.get('landing_commit')}"
            print(
                f"  - [{row.get('classification')}] {row.get('label')} "
                f"provider={row.get('provider')} task={row.get('task_id')} branch={row.get('branch')}{extra}"
            )
        return 0

    if command == "sweep":
        observations, github = cloud_tracker.observe_provider_details(target)
        provider_tasks = cloud_tracker._legacy_provider_tasks(observations)  # noqa: SLF001 - compatibility projection
        cursor_wired = observations["cursor-cloud"].configured
        status = cloud_tracker.status_payload(
            target,
            provider_tasks=provider_tasks,
            github=github,
            cursor_wired=cursor_wired,
            provider_observations=observations,
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

    if command == "compact":
        observations, github = cloud_tracker.observe_provider_details(target)
        provider_tasks = cloud_tracker._legacy_provider_tasks(observations)  # noqa: SLF001 - compatibility projection
        cursor_wired = observations["cursor-cloud"].configured
        try:
            report = cloud_tracker.compact_registry(
                target,
                keep_terminal=args.keep_terminal,
                max_age_hours=args.max_age_hours,
                provider_tasks=provider_tasks,
                github=github,
                cursor_wired=cursor_wired,
            )
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
            return 0
        print(f"cloud compact: {report['maintenance_id']} dropped={report['counts']['dropped']}")
        print(
            "policy: keep_terminal="
            f"{report['policy']['keep_terminal']} max_age_hours={report['policy']['max_age_hours']}"
        )
        return 0

    print(f"error: unknown cloud command: {command}", file=sys.stderr)
    return 2


def _dispatch_launch(args, target: Path) -> int:
    """Launch a hosted task from a private prompt file and register it."""
    from .. import cloud_tracker, cursor_cloud

    provider = args.provider
    if provider == "claude-cloud":
        # Refused before the label or the prompt file is read: nothing private is touched.
        from .. import claude_cloud_routine

        return _emit_launch_payload(
            {
                "ok": False,
                "provider": provider,
                "reason": claude_cloud_routine.LAUNCH_REFUSAL_REASON,
                "detail": claude_cloud_routine.LAUNCH_REFUSAL_DETAIL,
                "runbook": claude_cloud_routine.RUNBOOK,
            },
            as_json=args.json,
            exit_code=2,
        )
    try:
        label = _canonicalize_launch_label(args.label)
    except ValueError:
        return _emit_launch_payload(
            {"ok": False, "provider": provider, "label": args.label, "reason": "bad-label"},
            as_json=args.json,
            exit_code=2,
        )
    if provider == "codex-cloud" and args.auto_create_pr:
        return _emit_launch_payload(
            {"ok": False, "provider": provider, "label": label, "reason": "unsupported-flag"},
            as_json=args.json,
            exit_code=2,
        )
    if provider == "codex-cloud":
        from .. import codex_cloud

        try:
            codex_cloud.require_hub_lease_storage()
        except OSError:
            return _emit_launch_payload(
                {"ok": False, "provider": provider, "label": label, "reason": "secure-owner-store-unavailable"},
                as_json=args.json,
                exit_code=2,
            )
    try:
        prompt = _read_prompt_file(Path(args.prompt_file))
    except ValueError:
        return _emit_launch_payload(
            {"ok": False, "provider": provider, "label": label, "reason": "bad-prompt-file"},
            as_json=args.json,
            exit_code=2,
        )
    prompt_hash = cloud_tracker.prompt_hash(prompt)
    if provider == "codex-cloud":
        from .. import codex_cloud

        try:
            codex_result = codex_cloud.launch_task(prompt, target=target, repo=args.repo, label=label)
        except (ValueError, OSError) as exc:
            return _emit_launch_payload(
                {"ok": False, "provider": provider, "label": label, "reason": str(exc)},
                as_json=args.json,
                exit_code=2,
            )
        payload = _public_launch_payload(provider=provider, label=label, prompt_hash=prompt_hash, result=codex_result)
        return _emit_launch_payload(payload, as_json=args.json, exit_code=0 if codex_result["ok"] else 1)
    api_key = _resolve_launch_key(provider)
    if not api_key:
        return _emit_launch_payload(
            {
                "ok": False,
                "provider": provider,
                "label": label,
                "prompt_hash": prompt_hash,
                "reason": "missing-key",
            },
            as_json=args.json,
            exit_code=2,
        )

    launched = cursor_cloud.launch_agent(
        api_key,
        repo=args.repo,
        prompt=prompt,
        auto_create_pr=bool(args.auto_create_pr),
        register_target=target,
        label=label,
    )
    payload = _public_launch_payload(provider=provider, label=label, prompt_hash=prompt_hash, result=launched)
    return _emit_launch_payload(payload, as_json=args.json, exit_code=0 if launched.ok else 1)


def _resolve_launch_key(provider: str) -> str | None:
    """Return the provider key from existing environment resolution, never argv."""
    from .. import cloud_tracker

    if provider == "cursor-cloud":
        return cloud_tracker._cursor_api_key()
    return None


def _canonicalize_launch_label(label: object) -> str:
    """Strip and validate a launch label once before any provider or hub mutation."""
    if not isinstance(label, str):
        raise ValueError("bad-label")
    canonical = label.strip()
    if not 1 <= len(canonical) <= _LAUNCH_LABEL_MAX:
        raise ValueError("bad-label")
    if any(ord(ch) < 32 or ch == "\x7f" for ch in canonical):
        raise ValueError("bad-label")
    return canonical


def _read_prompt_file(path: Path) -> str:
    """Read a bounded regular prompt file through one O_NOFOLLOW descriptor."""
    resolved = path.expanduser()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:
        raise ValueError("bad-prompt-file")
    flags |= nofollow
    descriptor: int | None = None
    try:
        descriptor = os.open(os.fspath(resolved), flags)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("bad-prompt-file")
        owner_uid = getattr(os, "getuid", None)
        if owner_uid is not None and info.st_uid != owner_uid():
            raise ValueError("bad-prompt-file")
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise ValueError("bad-prompt-file")
        if info.st_size < 1 or info.st_size > _PROMPT_FILE_MAX_BYTES:
            raise ValueError("bad-prompt-file")
        chunks: list[bytes] = []
        remaining = _PROMPT_FILE_MAX_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if not 1 <= len(data) <= _PROMPT_FILE_MAX_BYTES:
            raise ValueError("bad-prompt-file")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("bad-prompt-file") from exc
        if not text.strip():
            raise ValueError("bad-prompt-file")
        return text
    except ValueError:
        raise
    except OSError as exc:
        raise ValueError("bad-prompt-file") from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError as exc:
                raise ValueError("bad-prompt-file") from exc


def _public_launch_payload(*, provider: str, label: str, prompt_hash: str, result: Any) -> dict[str, Any]:
    """Build the public launch contract. Holder and prompt text never appear."""
    payload: dict[str, Any] = {
        "ok": bool(result.get("ok")) if provider == "codex-cloud" else bool(result.ok),
        "provider": provider,
        "label": label,
        "prompt_hash": prompt_hash,
        "reason": result.get("reason") if provider == "codex-cloud" else result.reason,
    }
    if provider == "cursor-cloud":
        payload["task_id"] = result.agent_id
        payload["run_id"] = result.run_id
    else:
        payload["task_id"] = result.get("task_id")
    return payload


def _emit_launch_payload(payload: dict[str, Any], *, as_json: bool, exit_code: int) -> int:
    """Print a holder-free result. Prompt text is never echoed."""
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return exit_code
    if payload.get("ok"):
        extra = f" run={payload['run_id']}" if payload.get("run_id") else ""
        print(f"launched {payload.get('provider')} task={payload.get('task_id')}{extra}")
        return exit_code
    extra = ""
    if payload.get("provider") == "codex-cloud":
        from .. import codex_cloud

        task_id = payload.get("task_id")
        if isinstance(task_id, str) and codex_cloud.parse_task_id(task_id) == task_id:
            extra = f" task={task_id}"
    print(f"error: {payload.get('reason', 'launch-failed')}{extra}", file=sys.stderr)
    if isinstance(payload.get("detail"), str):
        print(payload["detail"], file=sys.stderr)
    return exit_code


def _dispatch_cloud_ops(args, target: Path) -> int:
    """Route setup, doctor, and canary to their bounded provider operation."""
    from .. import cloud_tracker, codex_cloud

    command = args.run_cloud_command
    if command == "setup":
        if getattr(args, "provider", None) != "codex-cloud":
            print("error: setup supports --provider codex-cloud only", file=sys.stderr)
            return 2
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
    if command == "doctor" and args.provider == "codex-cloud":
        result = codex_cloud.doctor(target, selector=args.selector)
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            print(f"codex-cloud doctor: {'ok' if result['ok'] else 'fail'}")
            print(f"environment_configured: {'yes' if result['environment_configured'] else 'no'}")
            print(f"selector: {result.get('selector', args.selector)}")
        return 0 if result["ok"] else 1
    if command == "canary" and args.provider == "codex-cloud":
        result = codex_cloud.canary(target, selector=args.selector)
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            print(f"codex-cloud canary: {'ok' if result['ok'] else 'fail'}")
            print(f"environment_configured: {'yes' if result['environment_configured'] else 'no'}")
            print(f"selector: {result.get('selector', args.selector)}")
            if not result["ok"]:
                print(f"reason: {result.get('reason', 'unknown')}")
            print(f"task_count: {result['task_count']}")
            print("apply: no")
        return 0 if result["ok"] else 1
    if command not in {"doctor", "canary"}:
        print(f"error: unknown cloud command: {command}", file=sys.stderr)
        return 2

    observation = cloud_tracker.observe_provider(args.provider, target)
    provider_result: dict[str, Any] = {
        "ok": observation.configured and observation.reachable and observation.reason is None,
        "provider": args.provider,
        "configured": observation.configured,
        "reachable": observation.reachable,
        "reason": observation.reason,
    }
    if args.provider == "claude-cloud":
        from .. import claude_cloud_routine

        # Tracking is a local registry contract with no provider wire to probe,
        # so doctor passes offline. Canary still fails: no inventory exists.
        provider_result["ok"] = command == "doctor"
        provider_result["launch"] = "conductor-routine"
        provider_result["detail"] = claude_cloud_routine.SOURCE_DETAIL
        provider_result["runbook"] = claude_cloud_routine.RUNBOOK
    if command == "canary":
        tasks = observation.tasks
        if args.provider == "grokbot-cloud" and provider_result["ok"]:
            from .. import fleet_client_grokbot

            decision = fleet_client_grokbot.list_jobs(include_all=False)
            if not decision.granted or decision.jobs is None:
                health_reason = _grokbot_health_reason(decision.reason)
                provider_result.update(
                    {
                        "ok": False,
                        "reachable": health_reason
                        not in {
                            "unconfigured",
                            "actor-not-enrolled",
                            "auth-failure",
                            "transport-failure",
                        },
                        "reason": health_reason,
                    }
                )
                tasks = {}
            else:
                tasks = {
                    str(job.get("job_id")): {
                        "job_id": str(job.get("job_id")),
                        "state": str(job.get("state")),
                        "role": job.get("role"),
                    }
                    for job in decision.jobs[:20]
                    if isinstance(job, dict)
                    and isinstance(job.get("job_id"), str)
                    and cloud_tracker.is_active_state(job.get("state"))
                }
        provider_result["tasks"] = list(tasks.values())[:20]
        provider_result["task_count"] = len(provider_result["tasks"])
        provider_result["apply"] = False
    if args.json:
        print(json.dumps(provider_result, indent=2, sort_keys=True))
    else:
        print(f"{args.provider} {command}: {'ok' if provider_result['ok'] else 'fail'}")
        print(f"configured: {'yes' if provider_result['configured'] else 'no'}")
        print(f"reachable: {'yes' if provider_result['reachable'] else 'no'}")
        if provider_result["reason"]:
            print(f"reason: {provider_result['reason']}")
        if command == "canary":
            print(f"task_count: {provider_result['task_count']}")
            print("apply: no")
    return 0 if provider_result["ok"] else 1


def _grokbot_health_reason(reason: str) -> str:
    if reason == "no-hub":
        return "unconfigured"
    if reason == "no-identity":
        return "actor-not-enrolled"
    if reason == "hub-unavailable":
        return "transport-failure"
    if reason == "auth-failed":
        return "auth-failure"
    return reason


def _grokbot_bearer_valid(args, pack_id: str | None) -> bool:
    has_bearer = args.bearer_file is not None or args.bearer_env is not None
    if pack_id in ("dot", "duet"):
        if has_bearer:
            print(f"error: the {pack_id} pack takes no bearer; it verifies Cloudflare Access tokens", file=sys.stderr)
            return False
    elif not has_bearer:
        print("error: --bearer-file or --bearer-env is required", file=sys.stderr)
        return False
    return True


def _dispatch_grokbot(args, target: Path) -> int:
    from ..dot import transport, legacy_registry, service_render
    from .removed_commands import removed

    if args.grokbot_command == "pack":
        return _dispatch_grokbot_pack(args, target)
    if args.grokbot_command != "serve" or args.pack not in ("dot", "duet"):
        return removed("run cloud grokbot " + args.grokbot_command)
    if not _grokbot_bearer_valid(args, args.pack):
        return 2
    try:
        if args.client_id is not None:
            raise transport.ConfigurationError("invalid")
        from .. import dot

        dot_config, dot_tools, dot_runtime = dot.build_listener_from_target(
            target,
            bind=args.bind,
            # Omitted flags keep the allowlists stored by `pack setup`.
            allowed_hosts=args.allow_host or None,
            allowed_origins=args.allow_origin or None,
            # Dot keeps its original call; other profiles name themselves.
            **({} if args.pack == "dot" else {"pack_id": args.pack}),
        )
        dot.run_listener(dot_config, dot_tools, dot_runtime)
    except transport.OptionalDependencyError:
        print("error: Grok Bot listener requires pip install brigade-cli[grokbot]", file=sys.stderr)
        return 2
    except (transport.ConfigurationError, legacy_registry.PackError, service_render.ServiceRenderError):
        print("error: Grok Bot listener configuration is invalid", file=sys.stderr)
        return 2
    except Exception as exc:
        from ..dot import DotError

        if isinstance(exc, DotError):
            print("error: Grok Bot listener configuration is invalid", file=sys.stderr)
            return 2
        raise
    return 0


_DOCTOR_NON_FAILING_STATUSES = frozenset({"ok", "skipped"})


def _doctor_exit_code(checks: list[dict[str, str]]) -> int:
    """Fail only on statuses outside the explicit non-failing allowlist."""
    return 0 if all(check["status"] in _DOCTOR_NON_FAILING_STATUSES for check in checks) else 1


def _dispatch_grokbot_pack(args, target: Path) -> int:
    """Preview-first connector-pack lifecycle without printing secret values."""
    from ..dot import transport as grokbot_mcp, service_render as grokbot_ops, legacy_registry as grokbot_packs

    command = args.grokbot_pack_command
    if command in {"relay-setup", "relay-doctor", "relay", "install-relay-service"}:
        from .removed_commands import removed

        return removed("run cloud grokbot pack " + command)
    try:
        if command == "list":
            result = {"packs": grokbot_packs.list_packs()}
        elif command == "show":
            result = grokbot_packs.show_pack(args.pack_id)
        elif command == "setup":
            if not _grokbot_bearer_valid(args, args.pack_id):
                return 2
            setup_kwargs = {
                "bind": args.bind,
                "allowed_hosts": args.allow_host,
                "allowed_origins": args.allow_origin,
                "bearer_env": args.bearer_env,
                "bearer_file": args.bearer_file,
                "runtime_path": getattr(args, "runtime_path", None),
                "owner_workspace": getattr(args, "owner_workspace", None),
            }
            result = (
                grokbot_packs.apply_setup(target, args.pack_id, **setup_kwargs)
                if args.apply
                else grokbot_packs.preview_setup(target, args.pack_id, **setup_kwargs)
            )
        elif command == "doctor":
            checks = grokbot_packs.doctor(target, args.pack_id, service_result=getattr(args, "service_result", False))
            if args.json:
                print(json.dumps({"checks": checks}, indent=2, sort_keys=True))
            else:
                for check in checks:
                    print(f"{check['check']}: {check['status']}")
            return _doctor_exit_code(checks)
        elif command == "canary":
            result = grokbot_packs.canary(target, args.pack_id)
            if args.json:
                print(json.dumps(result, indent=2, sort_keys=True))
            else:
                print(f"canary: {'ok' if result['ok'] else 'fail'}")
                if not result["ok"]:
                    print(f"reason: {result.get('reason', 'unknown')}")
            return 0 if result["ok"] else 1
        elif command == "install-service":
            if args.out is None:
                sys.stdout.write(grokbot_packs.render_install_service(target, args.pack_id))
                return 0
            result = grokbot_packs.apply_install_service(target, args.pack_id, out_dir=Path(args.out), force=args.force)
        elif command == "update":
            result = (
                grokbot_packs.apply_update(target, args.pack_id)
                if args.apply
                else grokbot_packs.preview_update(target, args.pack_id)
            )
        elif command == "remove":
            result = (
                grokbot_packs.apply_remove(target, args.pack_id, unit_dir=args.out)
                if args.apply
                else grokbot_packs.preview_remove(target, args.pack_id, unit_dir=args.out)
            )
        else:
            print(f"error: unknown Grok Bot pack command: {command}", file=sys.stderr)
            return 2
    except grokbot_packs.PackError as exc:
        print(f"error: {exc.reason}", file=sys.stderr)
        return 2
    except grokbot_mcp.ConfigurationError:
        print("error: Grok Bot configuration is invalid", file=sys.stderr)
        return 2
    except grokbot_ops.ServiceRenderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError):
        print("error: Grok Bot pack operation failed", file=sys.stderr)
        return 2

    if args.json or command in {"list", "show"}:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    print(f"grokbot pack {command}: apply={result.get('apply', False)} pack={result.get('pack_id', args.pack_id)}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    from . import main as cli_main

    raise SystemExit(cli_main(["run", "cloud", *sys.argv[1:]]))
