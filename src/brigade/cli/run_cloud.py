"""brigade run cloud — cloud dispatch registry commands (#890)."""

from __future__ import annotations

import argparse
from contextlib import nullcontext
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any, ContextManager, NamedTuple

_PROMPT_FILE_MAX_BYTES = 64 * 1024
_LAUNCH_LABEL_MAX = 120


_RUN_CLOUD_ALIAS_NOTICE = "note: `brigade run-cloud` is deprecated; use `brigade run cloud`."


def add_cloud_subcommands(parser: argparse.ArgumentParser) -> None:
    """Attach the cloud dispatch subcommands to `run cloud` or the `run-cloud` alias."""
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
        help="Launch a Cursor Cloud or Jules session from a private prompt file.",
    )
    p_launch.add_argument("--target", type=Path, default=Path("."), help="Workspace with .brigade/cloud state.")
    p_launch.add_argument("--provider", required=True, choices=("cursor-cloud", "jules"))
    p_launch.add_argument("--repo", required=True, help="GitHub owner/repo or https://github.com/owner/repo URL.")
    p_launch.add_argument("--label", required=True, help="Registry label. Prompt text is never stored.")
    p_launch.add_argument(
        "--prompt-file",
        type=Path,
        required=True,
        help="Bounded private prompt file. Prompt text is never an argv value.",
    )
    p_launch.add_argument(
        "--starting-branch",
        default=None,
        help="Jules starting branch. Rejected for Cursor Cloud.",
    )
    p_launch.add_argument("--title", default=None, help="Jules session title. Rejected for Cursor Cloud.")
    p_launch.add_argument(
        "--auto-create-pr",
        action="store_true",
        help="Opt in to provider autoCreatePR. Default is off.",
    )
    p_launch.add_argument("--json", action="store_true", help="Emit the public launch JSON contract.")

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
        "--provider", required=True, choices=("codex-cloud", "cursor-cloud", "grokbot-cloud", "claude-cloud", "jules")
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
        "--provider", required=True, choices=("codex-cloud", "cursor-cloud", "grokbot-cloud", "claude-cloud", "jules")
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
    p_feed.add_argument("--apply", action="store_true", help="Enqueue after validation. Default is validate only.")

    p_scout_feed = grokbot_sub.add_parser(
        "scout-feed", help="Select one approved GitHub issue for Grok Bot Repository Scout."
    )
    add_target(p_scout_feed)
    p_scout_feed.add_argument("--policy", type=Path, required=True, help="Path to the approved private scout policy.")
    p_scout_feed.add_argument("--apply", action="store_true", help="Enqueue after selection. Default is preview only.")

    from . import run_cloud_build_feed

    run_cloud_build_feed.register(grokbot_sub, add_target)

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
            "cerebro-memory",
            "fleet-steward",
            "backup-steward",
            "n8n-operator",
            "obsidian-operator",
            "operations-relay",
            "wazuh-triage",
            "n8n-operator",
        ),
    )
    p_grokbot_serve.add_argument(
        "--bind", default=None, help="Listener host:port. Defaults to the role or pack loopback."
    )
    p_grokbot_serve.add_argument("--allow-host", action="append", default=[], help="Explicit allowed Host value.")
    p_grokbot_serve.add_argument("--allow-origin", action="append", default=[], help="Explicit allowed Origin value.")
    secret_group = p_grokbot_serve.add_mutually_exclusive_group(required=True)
    secret_group.add_argument("--bearer-file", type=Path, help="Protected file containing the listener bearer.")
    secret_group.add_argument("--bearer-env", help="Environment variable name containing the listener bearer.")
    p_grokbot_serve.add_argument(
        "--upstream-url",
        default=None,
        help="Loopback HTTPS Local REST URL. Used by obsidian-operator.",
    )
    serve_upstream_key = p_grokbot_serve.add_mutually_exclusive_group()
    serve_upstream_key.add_argument(
        "--upstream-key-file",
        type=Path,
        help="Protected file containing the Obsidian upstream key (reference only).",
    )
    serve_upstream_key.add_argument(
        "--upstream-key-env",
        help="Environment variable name containing the Obsidian upstream key.",
    )

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

    from .. import grokbot_packs

    pack_ids = tuple(pack["id"] for pack in grokbot_packs.list_packs())
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
    pack_secret = p_pack_setup.add_mutually_exclusive_group(required=True)
    pack_secret.add_argument("--bearer-file", type=Path, help="Path of a protected bearer file (reference only).")
    pack_secret.add_argument("--bearer-env", help="Name of the environment variable holding the bearer.")
    p_pack_setup.add_argument(
        "--cli-executable",
        type=Path,
        default=None,
        help="Absolute Cerebro CLI executable. Required for cerebro-memory.",
    )
    p_pack_setup.add_argument(
        "--workdir",
        type=Path,
        default=None,
        help="Absolute Cerebro work directory. Required for cerebro-memory.",
    )
    p_pack_setup.add_argument(
        "--runtime-path",
        type=Path,
        default=None,
        help="Absolute runtime JSON path. Required for fleet-steward, backup-steward, obsidian-operator, wazuh-triage, and n8n-operator.",
    )
    p_pack_setup.add_argument(
        "--ledger-path",
        type=Path,
        default=None,
        help="Absolute steward ledger path. Required for fleet-steward, backup-steward, and wazuh-triage.",
    )
    p_pack_setup.add_argument(
        "--action-state-path",
        type=Path,
        default=None,
        help="Absolute action-state directory. Required for fleet-steward, backup-steward, obsidian-operator, wazuh-triage, and n8n-operator.",
    )
    p_pack_setup.add_argument(
        "--approval-dir",
        type=Path,
        default=None,
        help="Absolute approval directory. Required for fleet-steward, backup-steward, obsidian-operator, wazuh-triage, and n8n-operator.",
    )
    p_pack_setup.add_argument(
        "--staging-dir",
        type=Path,
        default=None,
        help="Absolute Obsidian staging directory. Required for obsidian-operator.",
    )
    p_pack_setup.add_argument(
        "--excalidraw-bin",
        type=Path,
        default=None,
        help="Absolute Excalidraw helper executable. Required for obsidian-operator.",
    )
    p_pack_setup.add_argument(
        "--upstream-url",
        default=None,
        help="Loopback HTTPS Local REST URL. Required for obsidian-operator.",
    )
    pack_upstream_key = p_pack_setup.add_mutually_exclusive_group()
    pack_upstream_key.add_argument(
        "--upstream-key-file",
        type=Path,
        help="Path of a protected upstream-key file (reference only). Required for obsidian-operator.",
    )
    pack_upstream_key.add_argument(
        "--upstream-key-env",
        help="Name of the environment variable holding the upstream key. Required for obsidian-operator.",
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


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "run-cloud",
        help="Deprecated alias for `brigade run cloud`.",
    )
    add_cloud_subcommands(p)


def dispatch(args) -> int:
    if getattr(args, "command", None) == "run-cloud":
        print(_RUN_CLOUD_ALIAS_NOTICE, file=sys.stderr)
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

    if command == "launch":
        return _dispatch_launch(args, target)

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
            print(
                f"  - [{row.get('classification')}] {row.get('label')} "
                f"provider={row.get('provider')} task={row.get('task_id')} branch={row.get('branch')}"
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
    """Launch Cursor Cloud or Jules from a private prompt file, then register on bind."""
    from .. import cloud_tracker, cursor_cloud, jules_cloud

    provider = args.provider
    try:
        label = _canonicalize_launch_label(args.label)
    except ValueError:
        return _emit_launch_payload(
            {"ok": False, "provider": provider, "label": args.label, "reason": "bad-label"},
            as_json=args.json,
            exit_code=2,
        )
    if provider == "cursor-cloud" and (args.starting_branch is not None or args.title is not None):
        return _emit_launch_payload(
            {"ok": False, "provider": provider, "label": label, "reason": "unsupported-flag"},
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

    launched: Any
    if provider == "cursor-cloud":
        launched = cursor_cloud.launch_agent(
            api_key,
            repo=args.repo,
            prompt=prompt,
            auto_create_pr=bool(args.auto_create_pr),
            register_target=target,
            label=label,
        )
    else:
        launched = jules_cloud.launch_agent(
            api_key,
            repo=args.repo,
            prompt=prompt,
            title=args.title,
            starting_branch=args.starting_branch,
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
    if provider == "jules":
        return cloud_tracker._jules_api_key()
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
        "ok": bool(result.ok),
        "provider": provider,
        "label": label,
        "prompt_hash": prompt_hash,
        "reason": result.reason,
    }
    if provider == "cursor-cloud":
        payload["task_id"] = result.agent_id
        payload["run_id"] = result.run_id
    else:
        payload["task_id"] = result.session_id
        payload["session_id"] = result.session_id
        payload["source_name"] = result.source_name
        payload["starting_branch"] = result.starting_branch
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
    print(f"error: {payload.get('reason', 'launch-failed')}", file=sys.stderr)
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


def _dispatch_grokbot(args, target: Path) -> int:
    """Dispatch local queue operations without loading envelopes into CLI arguments."""
    from .. import grokbot_jobs, grokbot_mcp

    command = args.grokbot_command
    if command == "pack":
        return _dispatch_grokbot_pack(args, target)
    if command in {"setup", "doctor", "canary", "install-service"}:
        return _dispatch_grokbot_ops(args, target)
    if command == "feed":
        return _dispatch_grokbot_feed(args, target)
    if command == "scout-feed":
        return _dispatch_grokbot_scout_feed(args, target)
    if command == "build-feed":
        from . import run_cloud_build_feed

        return run_cloud_build_feed.dispatch(args, target)
    if command == "reconcile-reports":
        return _dispatch_grokbot_reconcile(args, target)
    if command == "reconcile-findings":
        return _dispatch_grokbot_findings(args, target)
    if command == "convert-findings":
        return _dispatch_grokbot_convert_findings(args)
    if command == "relay-findings":
        return _dispatch_grokbot_relay_findings(args, target)
    if command == "serve":
        from .. import grokbot_mcp

        try:
            if getattr(args, "pack", None):
                from .. import grokbot_cerebro

                if args.pack != "obsidian-operator" and (
                    args.upstream_url or args.upstream_key_file or args.upstream_key_env
                ):
                    print("error: Grok Bot listener configuration is invalid", file=sys.stderr)
                    return 2
                listener_kwargs = {
                    "bind": args.bind,
                    "allowed_hosts": args.allow_host,
                    "allowed_origins": args.allow_origin,
                    "bearer_file": args.bearer_file,
                    "bearer_env": args.bearer_env,
                }
                if args.pack == "fleet-steward":
                    from ..grokbot_fleet.lifecycle import (
                        build_listener_from_target as build_fleet_listener,
                        run_listener as run_fleet_listener,
                    )

                    fleet_config, fleet_tools = build_fleet_listener(target, **listener_kwargs)
                    run_fleet_listener(fleet_config, fleet_tools)
                elif args.pack == "backup-steward":
                    from ..grokbot_backup.lifecycle import (
                        build_listener_from_target as build_backup_listener,
                        run_listener as run_backup_listener,
                    )

                    backup_config, backup_tools = build_backup_listener(target, **listener_kwargs)
                    run_backup_listener(backup_config, backup_tools)
                elif args.pack == "obsidian-operator":
                    from ..grokbot_obsidian.lifecycle import (
                        build_listener_from_target as build_obsidian_listener,
                        run_listener as run_obsidian_listener,
                    )

                    listener_kwargs["upstream_url"] = args.upstream_url
                    listener_kwargs["upstream_key_file"] = args.upstream_key_file
                    listener_kwargs["upstream_key_env"] = args.upstream_key_env
                    obsidian_config, obsidian_tools = build_obsidian_listener(target, **listener_kwargs)
                    run_obsidian_listener(obsidian_config, obsidian_tools)
                elif args.pack == "wazuh-triage":
                    from ..grokbot_wazuh.lifecycle import (
                        build_listener_from_target as build_wazuh_listener,
                        run_listener as run_wazuh_listener,
                    )

                    wazuh_config, wazuh_tools = build_wazuh_listener(target, **listener_kwargs)
                    run_wazuh_listener(wazuh_config, wazuh_tools)
                elif args.pack == "n8n-operator":
                    from ..grokbot_n8n.lifecycle import (
                        build_listener_from_target as build_n8n_listener,
                        run_listener as run_n8n_listener,
                    )

                    n8n_config, n8n_tools = build_n8n_listener(target, **listener_kwargs)
                    run_n8n_listener(n8n_config, n8n_tools)
                elif args.pack == "operations-relay":
                    from .. import grokbot_operations_relay

                    operations_config, operations_tools = grokbot_operations_relay.build_listener_from_target(
                        target, **listener_kwargs
                    )
                    grokbot_operations_relay.run_listener(operations_config, operations_tools)
                else:
                    cerebro_config, cerebro_tools = grokbot_cerebro.build_listener_from_target(
                        target, **listener_kwargs
                    )
                    grokbot_cerebro.run_listener(cerebro_config, cerebro_tools)
            else:
                config = grokbot_mcp.build_listener_config(
                    target=target,
                    instance=args.instance,
                    bind=args.bind or "127.0.0.1:8766",
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
        except Exception as exc:
            from .. import (
                grokbot_backup,
                grokbot_cerebro,
                grokbot_fleet,
                grokbot_n8n,
                grokbot_obsidian,
                grokbot_operations_relay,
                grokbot_packs,
                grokbot_wazuh,
            )

            if isinstance(
                exc,
                (
                    grokbot_backup.BackupError,
                    grokbot_cerebro.CerebroError,
                    grokbot_fleet.FleetError,
                    grokbot_n8n.N8nError,
                    grokbot_obsidian.ObsidianError,
                    grokbot_operations_relay.OperationsRelayError,
                    grokbot_packs.PackError,
                    grokbot_wazuh.WazuhError,
                ),
            ):
                print("error: Grok Bot listener configuration is invalid", file=sys.stderr)
                return 2
            raise
        return 0
    try:
        token = grokbot_mcp.load_direct_queue_listener_token()
        identity: ContextManager[None] = nullcontext()
        if token is not None:
            from .. import fleet_client_grokbot

            identity = fleet_client_grokbot.listener_identity(token)
        with identity:
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
    from .. import grokbot_feed, grokbot_mcp

    try:
        if args.apply:
            with _feed_hub_identity(target).context:
                result = grokbot_feed.apply(target, args.manifest, limit=args.limit)
        else:
            result = grokbot_feed.preflight(target, args.manifest, limit=args.limit)
    except grokbot_mcp.ConfigurationError:
        print("error: Grok Bot feed configuration is invalid", file=sys.stderr)
        return 2
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


class _FeedIdentity(NamedTuple):
    """The bound feed context plus the actor kind it speaks as, when bound."""

    context: ContextManager[None]
    actor_kind: str | None


def _feed_hub_identity(target: Path) -> _FeedIdentity:
    """Bind the dedicated feed actor for Hub mutations, with no fallback."""
    from .. import fleet_client_grokbot, grokbot_jobs, grokbot_mcp

    if not grokbot_jobs.hub_authority(target):
        return _FeedIdentity(nullcontext(), None)
    token = grokbot_mcp.load_feed_hub_token()
    if token is None:
        raise grokbot_mcp.ConfigurationError("invalid")
    return _FeedIdentity(fleet_client_grokbot.listener_identity(token), "feed")


def _dispatch_grokbot_scout_feed(args, target: Path) -> int:
    """Preview or enqueue one approved scout without exposing policy content."""
    from .. import grokbot_mcp, grokbot_scout_feed

    actor_kind: str | None = None
    try:
        if args.apply:
            identity = _feed_hub_identity(target)
            actor_kind = identity.actor_kind
            with identity.context:
                result = grokbot_scout_feed.apply(target, args.policy)
        else:
            result = grokbot_scout_feed.preflight(target, args.policy)
    except grokbot_mcp.ConfigurationError:
        print("error: Grok Bot feed configuration is invalid", file=sys.stderr)
        return 2
    except grokbot_scout_feed.ScoutFeedError as exc:
        if exc.action is not None and exc.actor_kind is None:
            exc.actor_kind = actor_kind
        print(f"error: {exc.public_detail()}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    _print_scout_feed_result(result)
    return 0


def _print_scout_feed_result(result: dict) -> None:
    """Render the safe scout selection projection without policy or issue contents."""
    parts = [f"created={result['created']}", f"reason={result['reason']}"]
    if result["issue_number"] is not None:
        parts.append(f"issue={result['issue_number']}")
    print("grokbot scout-feed: " + " ".join(parts))
    handle = result.get("handle")
    if handle is not None:
        print(f"job {handle['job_id']} state={handle['state']}")


def _dispatch_grokbot_reconcile(args, target: Path) -> int:
    """Preview or draft canonical-owner handoffs without printing report text."""
    # Reconciliation reads completed report bytes and writes the owner inbox.
    # The control actor has only enqueue/whoami authority, so it cannot safely
    # stand in for this local canonical-owner path.
    from .. import grokbot_reconcile

    try:
        if args.apply:
            result = grokbot_reconcile.apply(target, args.owner, inbox=args.inbox, limit=args.limit)
        else:
            result = grokbot_reconcile.preview(target, args.owner, inbox=args.inbox, limit=args.limit)
    except grokbot_reconcile.ReconcileError as exc:
        print(f"error: {exc.reason}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    _print_reconcile_result(result)
    return 0


def _dispatch_grokbot_findings(args, target: Path) -> int:
    """Preview or draft canonical-owner handoffs without printing finding text."""
    from .. import grokbot_findings

    try:
        if args.apply:
            result = grokbot_findings.apply(target, args.owner, args.manifest, limit=args.limit)
        else:
            result = grokbot_findings.preview(target, args.owner, args.manifest, limit=args.limit)
    except grokbot_findings.FindingsError as exc:
        print(f"error: {exc.reason}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    _print_findings_result(result)
    return 0


def _dispatch_grokbot_relay_findings(args, target: Path) -> int:
    """Preview or relay findings without printing finding text or credentials."""
    from .. import grokbot_findings, grokbot_findings_relay

    try:
        payload = grokbot_findings.load_manifest(args.manifest)
        if args.apply:
            result = grokbot_findings_relay.relay_apply(payload["entries"], target, args.owner, limit=args.limit)
        else:
            result = grokbot_findings_relay.relay_preview(payload["entries"], target, args.owner, limit=args.limit)
    except grokbot_findings.FindingsError as exc:
        print(f"error: {exc.reason}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    _print_relay_findings_result(result)
    return 0


def _print_relay_findings_result(result: dict) -> None:
    """Render relay counts and irreversible relay IDs only."""
    parts = [
        f"eligible={result['eligible']}",
        f"known={result['known']}",
        f"created={result['created']}",
        f"limit={result['limit']}",
    ]
    if "skipped" in result:
        parts.insert(2, f"skipped={result['skipped']}")
    if "pending" in result:
        parts.append(f"pending={result['pending']}")
    if "reported" in result:
        parts.append(f"reported={result['reported']}")
    print("grokbot relay-findings: " + " ".join(parts))
    for relay_id in result.get("relays", []):
        print(f"relay {relay_id}")


def _dispatch_grokbot_convert_findings(args) -> int:
    """Convert live records into a generic manifest without printing finding text."""
    from .. import grokbot_findings

    try:
        result = grokbot_findings.convert_live_findings(args.input, args.output)
    except grokbot_findings.FindingsError as exc:
        print(f"error: {exc.reason}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    _print_convert_findings_result(result)
    return 0


def _print_convert_findings_result(result: dict) -> None:
    """Render conversion counts and safe identity handles only."""
    print(f"grokbot convert-findings: converted={result['converted']}")
    for finding in result.get("findings", []):
        print(f"finding {finding['producer']} {finding['finding_id']} revision={finding['revision']}")


def _print_findings_result(result: dict) -> None:
    """Render findings counts and safe identity handles only."""
    parts = [
        f"eligible={result['eligible']}",
        f"known={result['known']}",
        f"created={result['created']}",
        f"limit={result['limit']}",
    ]
    if "skipped" in result:
        parts.insert(2, f"skipped={result['skipped']}")
    print("grokbot reconcile-findings: " + " ".join(parts))
    for finding in result.get("findings", []):
        print(f"finding {finding['producer']} {finding['finding_id']} revision={finding['revision']}")


def _print_reconcile_result(result: dict) -> None:
    """Render reconcile counts and safe job handles only."""
    parts = [
        f"eligible={result['eligible']}",
        f"known={result['known']}",
        f"unavailable={result['unavailable']}",
        f"created={result['created']}",
        f"limit={result['limit']}",
    ]
    if "skipped" in result:
        parts.insert(3, f"skipped={result['skipped']}")
    print("grokbot reconcile-reports: " + " ".join(parts))
    for job in result.get("jobs", []):
        print(f"job {job['job_id']} state={job['state']}")


_DOCTOR_NON_FAILING_STATUSES = frozenset({"ok", "skipped"})


def _doctor_exit_code(checks: list[dict[str, str]]) -> int:
    """Fail only on statuses outside the explicit non-failing allowlist."""
    return 0 if all(check["status"] in _DOCTOR_NON_FAILING_STATUSES for check in checks) else 1


def _dispatch_grokbot_pack(args, target: Path) -> int:
    """Preview-first connector-pack lifecycle without printing secret values."""
    from .. import grokbot_mcp, grokbot_ops, grokbot_packs

    command = args.grokbot_pack_command
    if command in {"relay-setup", "relay-doctor", "relay", "install-relay-service"}:
        return _dispatch_grokbot_pack_relay(args, target)
    try:
        if command == "list":
            result = {"packs": grokbot_packs.list_packs()}
        elif command == "show":
            result = grokbot_packs.show_pack(args.pack_id)
        elif command == "setup":
            setup_kwargs = {
                "bind": args.bind,
                "allowed_hosts": args.allow_host,
                "allowed_origins": args.allow_origin,
                "bearer_env": args.bearer_env,
                "bearer_file": args.bearer_file,
                "cli_executable": getattr(args, "cli_executable", None),
                "workdir": getattr(args, "workdir", None),
                "runtime_path": getattr(args, "runtime_path", None),
                "ledger_path": getattr(args, "ledger_path", None),
                "action_state_path": getattr(args, "action_state_path", None),
                "approval_dir": getattr(args, "approval_dir", None),
                "staging_dir": getattr(args, "staging_dir", None),
                "excalidraw_bin": getattr(args, "excalidraw_bin", None),
                "upstream_url": getattr(args, "upstream_url", None),
                "upstream_key_env": getattr(args, "upstream_key_env", None),
                "upstream_key_file": getattr(args, "upstream_key_file", None),
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


def _dispatch_grokbot_pack_relay(args, target: Path) -> int:
    """Preview-first first-party finding relay without printing finding text."""
    from .. import grokbot_pack_relay, grokbot_packs

    command = args.grokbot_pack_command
    try:
        if command == "relay-setup":
            result = (
                grokbot_packs.apply_relay_setup(target, args.owner)
                if args.apply
                else grokbot_packs.preview_relay_setup(target, args.owner)
            )
        elif command == "relay-doctor":
            checks = grokbot_packs.relay_doctor(target)
            if args.json:
                print(json.dumps({"checks": checks}, indent=2, sort_keys=True))
            else:
                for check in checks:
                    print(f"{check['check']}: {check['status']}")
            return 1 if any(check["status"] != "ok" for check in checks) else 0
        elif command == "relay":
            result = (
                grokbot_packs.apply_relay(target, limit=args.limit)
                if args.apply
                else grokbot_packs.preview_relay(target, limit=args.limit)
            )
        elif command == "install-relay-service":
            if args.out is None:
                units = grokbot_packs.render_relay_units(target)
                for name in (grokbot_pack_relay.RELAY_SERVICE_UNIT, grokbot_pack_relay.RELAY_TIMER_UNIT):
                    sys.stdout.write(f"# {name}\n{units[name]}")
                    if not units[name].endswith("\n"):
                        sys.stdout.write("\n")
                return 0
            written = grokbot_packs.write_relay_units(target, Path(args.out), force=args.force)
            result = {"action": "install-relay-service", "apply": True, "units": [path.name for path in written]}
        else:
            print(f"error: unknown Grok Bot pack command: {command}", file=sys.stderr)
            return 2
    except grokbot_pack_relay.PackRelayError as exc:
        print(f"error: {exc.reason}", file=sys.stderr)
        return 2
    except (OSError, ValueError):
        print("error: Grok Bot pack relay operation failed", file=sys.stderr)
        return 2

    if args.json or command == "relay":
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    print(f"grokbot pack {command}: apply={result.get('apply', False)}")
    return 0


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
                hub_token_file=args.hub_token_file,
            )
            print(f"grokbot config saved: role={instance}")
            return 0

        if command == "doctor":
            checks = grokbot_ops.doctor(target, instance)
            for check in checks:
                print(f"{check['check']}: {check['status']}")
            return _doctor_exit_code(checks)

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


if __name__ == "__main__":  # pragma: no cover
    from . import main as cli_main

    raise SystemExit(cli_main(["run", "cloud", *sys.argv[1:]]))
