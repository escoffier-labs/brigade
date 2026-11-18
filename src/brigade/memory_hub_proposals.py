"""Operator inspection and explicit decisions for reviewed hub proposals."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from . import memory_hub_client as hubc
from . import memory_hub_sync as sync
from .memory_reflect import _terminal_text


def register(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser(
        "proposals",
        help="Review hub proposals. Distinct from memory hub proposals (local sync conflicts).",
        description="Review hub proposals. memory hub proposals manages local sync conflicts separately.",
    )
    commands = parser.add_subparsers(dest="hub_proposal_command", required=True)
    for name in ("list", "show", "decide"):
        command = commands.add_parser(name, help=f"{name.capitalize()} reviewed hub proposals.")
        command.add_argument("--target", "-t", type=Path, default=Path("."))
        command.add_argument("--json", action="store_true")
        command.add_argument("--hub-url", help="Explicit memory hub URL. Requires --token-file.")
        command.add_argument("--token-file", type=Path, help="Absolute path to a private memory credential file.")
        if name == "list":
            command.add_argument(
                "--all",
                action="store_true",
                help="Include states other than pending. Listing requires list_proposals on *.",
            )
            command.add_argument("--after", help="Opaque next_after cursor returned by the hub.")
            command.add_argument("--limit", type=int, default=100, help="Page size from 1 to 100.")
        else:
            command.add_argument("proposal_id")
        if name == "decide":
            decision = command.add_mutually_exclusive_group(required=True)
            decision.add_argument("--accept", dest="decision", action="store_const", const="accept")
            decision.add_argument("--reject", dest="decision", action="store_const", const="reject")
            command.add_argument(
                "--expected-policy-digest",
                help=(
                    "Current hub policy digest. Copy the policy_digest printed by "
                    "'brigade memory proposals show <id>', for example sha256:4c31..., and pass it verbatim. "
                    "Accepts sha256:<64 hex>, bare <64 hex>, or none when the policy is absent. "
                    "Required for accept and after a policy change."
                ),
            )


def _id(value: str | None) -> bool:
    return isinstance(value, str) and hubc.event_id_ms(value) is not None


def _policy_digest(value: str) -> str | None:
    """Normalize an operator digest to the hub's ``sha256:<hex>`` form.

    ``none`` means the hub currently has no policy and is sent as JSON null.
    Both a prefixed and a bare 64-character lowercase digest are accepted; the
    hub compares the digest by exact equality, so hex values are always sent
    with the prefix that ``proposals show`` prints.
    """
    if value == "none":
        return None
    hex_value = value[len("sha256:") :] if value.startswith("sha256:") else value
    if not re.fullmatch(r"[0-9a-f]{64}", hex_value):
        raise ValueError(
            "--expected-policy-digest must be sha256:<64 lowercase hex>, <64 lowercase hex>, or none. "
            "Copy the policy_digest printed by 'brigade memory proposals show <id>' and pass it verbatim"
        )
    return f"sha256:{hex_value}"


def _run(args: argparse.Namespace) -> dict[str, Any]:
    if bool(args.hub_url) != (args.token_file is not None):
        raise ValueError("set both --hub-url and --token-file, or neither")
    command = args.hub_proposal_command
    if command == "list":
        if not 1 <= args.limit <= 100 or (args.after is not None and not re.fullmatch(r"p1\.[0-9a-f]{64}", args.after)):
            raise ValueError("list needs --limit from 1 to 100 and an opaque --after cursor from list")
    elif not _id(args.proposal_id):
        raise ValueError("proposal_id must be a time-ordered id")
    expected_digest: str | None = None
    if command == "decide" and args.decision == "accept" and args.expected_policy_digest is None:
        raise ValueError(
            "--accept requires --expected-policy-digest. "
            f"Copy the policy_digest printed by 'brigade memory proposals show {args.proposal_id} "
            "--target <reflect-folder>', for example sha256:4c31..., and pass it verbatim, "
            "or none if show reports null"
        )
    if command == "decide" and args.expected_policy_digest is not None:
        expected_digest = _policy_digest(args.expected_policy_digest)
    pair = (args.hub_url, args.token_file) if args.hub_url else None
    try:
        settings = hubc.load_settings(args.target, credential_pair=pair)
        if not settings.enabled or settings.source == hubc.SOURCE_FLEET:
            raise ValueError("operator proposals require enabled hub memory and explicit memory credentials")
        client = hubc.client_for(settings)
        status = sync.read_status(client, settings)
    except hubc.HubConfigError:
        raise ValueError("invalid hub memory configuration or credential file") from None
    if command == "list":
        fields: dict[str, Any] = {"all": args.all, "limit": args.limit}
        if args.after is not None:
            fields["after"] = args.after
        return client.call("proposal_list", **fields)
    if command == "show":
        # Only the hub may return a diff. Never reconstruct one from replica or log data.
        return client.call("proposal_show", proposal_id=args.proposal_id)
    if not _id(status.get("log_epoch")):
        raise ValueError("hub status names no valid log epoch")
    fields = {
        "proposal_id": args.proposal_id,
        "decision": args.decision,
        "base_epoch": status["log_epoch"],
        "client_event_id": hubc.event_id(),
    }
    if args.expected_policy_digest is not None:
        fields["expected_policy_digest"] = expected_digest
    # No automatic retry with a new policy digest. The operator must review that policy.
    result = client.call("decide", **fields)
    return {key: result[key] for key in ("proposal_id", "state", "reason") if key in result}


def dispatch(args: argparse.Namespace) -> int:
    code = 0
    try:
        payload = _run(args)
    except hubc.HubRefusal as exc:
        safe = exc.code if re.fullmatch(r"[a-z][a-z0-9_]{0,127}", exc.code) else "hub_refused"
        payload = {"status": "refused", "error": safe}
        if safe == "memory_policy_digest_mismatch":
            digest = exc.body.get("policy_digest")
            if "policy_digest" in exc.body and (
                digest is None or isinstance(digest, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
            ):
                payload["policy_digest"] = digest
            payload["reason"] = "review the current policy and supply --expected-policy-digest explicitly"
        code = 2
    except hubc.HubUnavailable:
        payload = {"status": "failed", "error": "hub_unavailable"}
        code = 1
    except (OSError, ValueError) as exc:
        payload = {"status": "refused", "error": str(exc) if isinstance(exc, ValueError) else "local state unavailable"}
        code = 2
    if args.json:
        print(json.dumps(payload, sort_keys=True, indent=2))
    elif args.hub_proposal_command == "show" and code == 0:
        metadata = {key: value for key, value in payload.items() if key != "diff"}
        print(_terminal_text(json.dumps(metadata, sort_keys=True, indent=2)))
        diff = payload.get("diff")
        print(_terminal_text(diff) if isinstance(diff, str) else "diff: redacted by hub review gate")
    else:
        print(_terminal_text(json.dumps(payload, sort_keys=True, indent=2)))
        if args.hub_proposal_command == "list" and code == 0 and not payload.get("proposals"):
            print(
                "note: an empty list also means this credential lacks list_proposals on *. "
                "Use proposals show <id> with a known id."
            )
    return code
