"""Explicit offline custody; supplied scope grants no endpoint or principal trust."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

_NOTE = "offline local custody; no publication or trust acceptance"
_RECOVERY = (
    "Do not retry rotation automatically. Inspect the saved kid with read-only show for the same scope, "
    "then explicitly use init to flush existing custody. If custody is missing, stop and investigate; "
    "do not initialize or overwrite it as recovery."
)
_CODES = frozenset(
    {
        "mesh_keys_identity_invalid",
        "mesh_keys_missing",
        "mesh_keys_invalid",
        "mesh_keys_unsafe",
        "mesh_keys_busy",
        "mesh_keys_io_error",
        "mesh_keys_platform_unsupported",
        "mesh_keys_clock_rollback",
        "mesh_extra_missing",
        "mesh_backend_unsupported",
        "mesh_keys_failed",
        "mesh_keys_interrupted",
        "mesh_keys_output_failed",
    }
)
_FIELDS = (
    "v",
    "principal",
    "hub",
    "kid",
    "sig_pub",
    "enc_pub",
    "suites",
    "created_at",
    "transport",
    "prev_kid",
    "self_sig",
    "prev_sig",
)


def register(fleet_sub: argparse._SubParsersAction) -> None:
    parser = fleet_sub.add_parser(
        "keys",
        help="Manage offline local private-key custody.",
        description="Offline local custody only. Supply the intended principal and independently obtained hub id. "
        "These inputs do not authenticate an endpoint, enroll a principal, or grant trust.",
    )
    sub = parser.add_subparsers(dest="keys_command", metavar="<keys-command>", required=True)
    for verb, help_text in (
        ("init", "Create custody or flush an existing generation without rotation."),
        ("show", "Read saved public keys without creating or repairing custody."),
        ("rotate", "Rotate locally once, retaining history and retry material."),
    ):
        command = sub.add_parser(verb, help=help_text, description=help_text)
        command.add_argument("--principal", required=True, help="Explicit intended offline principal.")
        command.add_argument("--hub-id", required=True, help="Explicit independently obtained canonical h_ id.")
        output = command.add_mutually_exclusive_group()
        output.add_argument("--json", action="store_true", help="Print the public local-custody summary.")
        if verb == "show":
            output.add_argument("--record", action="store_true", help="Print the active complete public record.")
        command.set_defaults(func=_dispatch)


def _silence_failed_stream(stream) -> None:
    # Buffered output can fail again during interpreter shutdown, overriding
    # the handled failure with exit 120. Discard only an already failed FD.
    try:
        descriptor = stream.fileno()
        with open(os.devnull, "wb") as sink:
            os.dup2(sink.fileno(), descriptor)
    except (Exception, KeyboardInterrupt):
        pass


def _failure(args: argparse.Namespace, code: str) -> int:
    # Both streams may already be broken after a committed rotation. Reporting
    # must never re-enter custody or let an exception reach the global logger.
    try:
        if args.json:
            print(json.dumps({"error": code, "status": None, "code": code}, sort_keys=True))
            sys.stdout.flush()
    except (Exception, KeyboardInterrupt):
        _silence_failed_stream(sys.stdout)
    try:
        recovery = f" {_RECOVERY}" if args.keys_command == "rotate" else ""
        print(f"error: {code}.{recovery}", file=sys.stderr)
        sys.stderr.flush()
    except (Exception, KeyboardInterrupt):
        _silence_failed_stream(sys.stderr)
    return 1


def _dispatch(args: argparse.Namespace) -> int:
    # Validate before reading the home or calling any custody API. No argparse
    # converters: their diagnostics repeat invalid, potentially secret input.
    from .. import fleet_mesh_crypto as crypto

    try:
        if not re.fullmatch(
            r"(?:[A-Za-z0-9][A-Za-z0-9._-]{0,127}|agent:[a-z][a-z0-9._-]{0,62})", args.principal, re.ASCII
        ) or not args.hub_id.startswith("h_"):
            return _failure(args, "mesh_keys_identity_invalid")
        try:
            crypto.decode_binary(args.hub_id[2:], 16)
        except ValueError:
            return _failure(args, "mesh_keys_identity_invalid")

        from .. import fleet_client, fleet_mesh_keys

        home = fleet_client.brigade_home()
        if args.keys_command == "rotate":
            state = fleet_mesh_keys.rotate_local_keys(brigade_home=home, principal=args.principal, hub_id=args.hub_id)
        else:
            state = fleet_mesh_keys.load_local_keys(
                brigade_home=home,
                principal=args.principal,
                hub_id=args.hub_id,
                create=args.keys_command == "init",
            )
        # Explicit public schema. Never serialize the custody dataclass, its
        # private history, or unknown future record fields.
        record = {key: state.active.record[key] for key in _FIELDS if key in state.active.record}
        fingerprint = crypto.fingerprint(crypto.decode_binary(record["sig_pub"], 32))
        summary = {key: record[key] for key in ("principal", "hub", "kid")}
        summary.update(fingerprint=fingerprint, scope="local-custody")
        if getattr(args, "record", False):
            text = crypto.canonical(record).decode("ascii")
        elif args.json:
            text = json.dumps(summary, sort_keys=True)
        else:
            text = (
                f"principal: {summary['principal']}\nhub: {summary['hub']}\nkid: {summary['kid']}\n"
                f"fingerprint: {fingerprint}\n{_NOTE}"
            )
    except crypto.MeshError as error:
        return _failure(args, error.code if error.code in _CODES else "mesh_keys_failed")
    except KeyboardInterrupt:
        return _failure(args, "mesh_keys_interrupted")
    except Exception:
        return _failure(args, "mesh_keys_failed")
    try:
        print(text)
        sys.stdout.flush()
    except (Exception, KeyboardInterrupt):
        _silence_failed_stream(sys.stdout)
        return _failure(args, "mesh_keys_output_failed")
    return 0
