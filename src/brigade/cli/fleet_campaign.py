"""Read-only offline campaign preview CLI; no fleet client or dispatch calls."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .. import fleet_campaign as campaign
from ..repos_cmd.sweeps import _select_sweep_entries


def register(fleet_sub: argparse._SubParsersAction) -> None:
    group = fleet_sub.add_parser("campaign", help="Read-only offline campaign membership planning.")
    commands = group.add_subparsers(dest="fleet_campaign_command", required=True)
    parser = commands.add_parser(
        "preview",
        help="Read-only offline preview as JSON; never dispatches or reports campaign completion.",
        description=(
            "Read-only offline campaign preview. Emits JSON without dispatch, claim, enqueue, Hub access or writes. "
            "All task states and launch safety remain unknown. Supplied observations are unverified evidence."
        ),
    )
    parser.add_argument("--target", type=Path, default=Path.cwd(), help="Workspace containing .brigade/repos.toml.")
    parser.add_argument(
        "--campaign-id", required=True, help="Explicit opaque campaign key, never a name/latest lookup."
    )
    parser.add_argument(
        "--bindings", type=Path, required=True, help="Bounded JSON list of exact task/action references."
    )
    parser.add_argument("--repos", help="Comma-separated exact repos.toml IDs; default all enabled IDs, no named sets.")
    parser.add_argument("--prior", type=Path, help="Explicit prior preview JSON; only this envelope is compared.")
    parser.add_argument("--observations", type=Path, help="Bounded typed supplied/unverified observation fixture JSON.")
    parser.add_argument("--json", action="store_true", help="Emit JSON (also the default).")
    parser.set_defaults(func=_dispatch)


def _read_input(path: Path) -> object:
    try:
        with path.open("rb") as stream:
            raw = stream.read(campaign.MAX_INPUT_BYTES + 1)
        if len(raw) > campaign.MAX_INPUT_BYTES:
            raise campaign.PreviewError("input_refused")
        return json.loads(raw)
    except (OSError, ValueError, RecursionError):
        raise campaign.PreviewError("input_refused") from None


def _dispatch(args: argparse.Namespace) -> int:
    try:
        campaign.opaque_key(args.campaign_id)
        ids = None
        if args.repos is not None:
            ids = args.repos.split(",")
            if len(ids) > campaign.MAX_MEMBERS:
                raise campaign.PreviewError("membership_refused")
            ids = [campaign.opaque_key(value) for value in ids]
        entries, errors, loaded = _select_sweep_entries(args.target.expanduser().resolve(), repo_ids=ids)
        selected = [entry.repo_id for entry in entries]
        if not loaded or errors or not selected or (ids is not None and set(ids) != set(selected)):
            raise campaign.PreviewError("repository_selection_refused")
        prior = _read_input(args.prior) if args.prior else None
        if args.prior and not isinstance(prior, dict):
            raise campaign.PreviewError("prior_preview_refused")
        payload = campaign.preview(
            campaign_id=args.campaign_id,
            repo_ids=selected,
            bindings=campaign.parse_bindings(_read_input(args.bindings)),
            observations=campaign.parse_observations(_read_input(args.observations)) if args.observations else (),
            prior=prior,
        )
    except campaign.PreviewError as exc:
        print(json.dumps({"kind": campaign.PREVIEW_KIND, "read_only": True, "error": str(exc)}))
        return 2
    except (OSError, ValueError, TypeError, IndexError, RecursionError):
        # Existing config parser errors may contain paths or private text.
        print(json.dumps({"kind": campaign.PREVIEW_KIND, "read_only": True, "error": "input_refused"}))
        return 2
    print(json.dumps(payload, sort_keys=True, indent=2))
    return 0
