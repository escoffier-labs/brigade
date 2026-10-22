"""Campaign CLI: offline preview and inspect, plus Hub-backed create, list and show (#1128).

Preview and inspect stay offline. Only create, campaigns and show reach the Hub, and
none of them launches anything."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .. import fleet_campaign as campaign
from .. import fleet_campaign_authority as authority
from ..repos_cmd.constants import RepoEntry
from ..repos_cmd.sweeps import _select_sweep_entries


def register(fleet_sub: argparse._SubParsersAction) -> None:
    group = fleet_sub.add_parser("campaign", help="Campaign membership: offline preview, Hub create and show.")
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
    inspector = commands.add_parser("inspect", help="Read-only exact local action/import/task lineage diagnostic.")
    inspector.add_argument("--target", type=Path, default=Path.cwd())
    inspector.add_argument("--campaign-id", required=True, help="Explicit opaque campaign ID.")
    inspector.add_argument(
        "--references", type=Path, required=True, help="Bounded JSON list of exact action references."
    )
    inspector.add_argument("--repos", help="Comma-separated exact enabled repos.toml IDs; default all enabled.")
    inspector.add_argument("--json", action="store_true", help="Emit JSON (also the default).")
    inspector.set_defaults(func=_inspect)
    _register_hub_commands(commands, fleet_sub)


def _inspect(args: argparse.Namespace) -> int:
    try:
        campaign.opaque_key(args.campaign_id)
        ids = None
        if args.repos is not None:
            ids = args.repos.split(",")
            if len(ids) > campaign.MAX_MEMBERS:
                raise campaign.PreviewError("membership_refused")
            ids = [campaign.opaque_key(value) for value in ids]
        target = args.target.expanduser().resolve()
        entries, errors, loaded = _select_sweep_entries(target, repo_ids=ids)
        selected = {entry.repo_id for entry in entries}
        if not loaded or errors or not selected or (ids is not None and set(ids) != selected):
            raise campaign.PreviewError("repository_selection_refused")
        payload = authority.inspect(
            target=target,
            campaign_id=args.campaign_id,
            entries=entries,
            references=authority.parse_references(_read_input(args.references)),
        )
    except campaign.PreviewError as exc:
        print(json.dumps({"kind": authority.INSPECT_KIND, "read_only": True, "error": str(exc)}))
        return 2
    except (OSError, ValueError, TypeError, IndexError, RecursionError, RuntimeError):
        print(json.dumps({"kind": authority.INSPECT_KIND, "read_only": True, "error": "input_refused"}))
        return 2
    print(json.dumps(payload, sort_keys=True, indent=2))
    return 0


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
    except (OSError, ValueError, TypeError, IndexError, RecursionError, RuntimeError):
        # Existing config parser errors may contain paths or private text.
        print(json.dumps({"kind": campaign.PREVIEW_KIND, "read_only": True, "error": "input_refused"}))
        return 2
    print(json.dumps(payload, sort_keys=True, indent=2))
    return 0


def _register_hub_commands(commands: argparse._SubParsersAction, fleet_sub: argparse._SubParsersAction) -> None:
    create = commands.add_parser(
        "create",
        help="Create a campaign and one member item per repos.toml repository on the Hub; re-running adds only the missing.",
        description=(
            "Create a campaign on the Fleet Hub: one campaign item and one member item per selected repository, "
            "keyed by canonical repository identity. Re-running adds only missing members. Launches nothing."
        ),
    )
    create.add_argument("--target", type=Path, default=Path.cwd(), help="Workspace containing .brigade/repos.toml.")
    create.add_argument("--campaign-id", required=True, help="Explicit opaque campaign key.")
    create.add_argument("--title", required=True, help="Campaign title.")
    create.add_argument(
        "--acceptance", action="append", required=True, metavar="TEXT", help="Acceptance item; repeat for more."
    )
    create.add_argument("--description-file", type=Path, help="File holding the shared spec description.")
    create.add_argument(
        "--overrides", type=Path, help="Bounded JSON object mapping repos.toml repo ID to override text."
    )
    create.add_argument("--offer-file", type=Path, help="Bounded JSON campaign authorization offer.")
    create.add_argument("--repos", help="Comma-separated exact repos.toml IDs; default all enabled IDs.")
    create.add_argument("--priority", help="Campaign priority (low, normal, high, urgent).")
    create.add_argument("--dry-run", action="store_true", help="Print the request without writing to the Hub.")
    create.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    create.set_defaults(func=_create)
    show = commands.add_parser("show", help="Show one campaign with a row per member repository.")
    show.add_argument("campaign_key", help="Campaign key.")
    show.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    show.set_defaults(func=_show)
    listing = fleet_sub.add_parser("campaigns", help="List campaigns on the Hub with rollup counts.")
    listing.add_argument("--limit", type=int, default=None, help="Campaigns to show (1-50, default 20).")
    listing.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    listing.set_defaults(func=_list)


def _text(value: object) -> str:
    from .. import worklore_client

    return worklore_client._safe_terminal_text(value) if value is not None else "-"


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list_of(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _count(value: object) -> str:
    """Hub counts print only as real ints; a missing or non-int count prints ``-``."""
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return "-"


def _refuse(args: argparse.Namespace, code: str) -> int:
    if getattr(args, "json", False):
        print(json.dumps({"error": code}), file=sys.stderr)
    else:
        print(f"error: {code}", file=sys.stderr)
    return 2


def _hub_failure(args: argparse.Namespace, exc: Exception) -> int:
    from .. import worklore_client

    error = _text(exc) or "fleet hub work failed"
    code = worklore_client._error_code(getattr(exc, "code", None))
    if getattr(args, "json", False):
        print(json.dumps({"error": error, **({"code": code} if code else {})}, sort_keys=True), file=sys.stderr)
    else:
        print(f"error: {error}" + (f" (code: {code})" if code else ""), file=sys.stderr)
    return 1


def _read_text(path: Path, limit: int) -> str:
    try:
        with path.open("rb") as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise campaign.PreviewError("input_refused")
        return raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        raise campaign.PreviewError("input_refused") from None


def _identity(entry: RepoEntry) -> str:
    """Canonical ``owner/repo`` for a selected entry; the worktree suffix is never part of it."""
    from .. import fleet_claim_target
    from ..worklore_campaigns import canonical_identity

    try:
        # The entry must be a checkout root (main or linked worktree): a subdirectory or a
        # missing path would otherwise resolve to whatever repository encloses it.
        layout = fleet_claim_target._git_layout(entry.path) if entry.path.is_dir() else None
        if layout is None or layout[0].resolve() != entry.path.resolve():
            raise campaign.PreviewError("repository_identity_unresolved")
        key = fleet_claim_target.git_claim_key(entry.path)
    except (fleet_claim_target.ClaimTargetError, OSError):
        key = None
    identity = canonical_identity(key.split("@", 1)[0]) if key else None
    if identity is None:
        raise campaign.PreviewError("repository_identity_unresolved")
    return identity


def _members(args: argparse.Namespace) -> list[dict[str, str]]:
    ids = None
    if args.repos is not None:
        ids = args.repos.split(",")
        if len(ids) > campaign.MAX_MEMBERS:
            raise campaign.PreviewError("membership_refused")
        ids = [campaign.opaque_key(value) for value in ids]
    entries, errors, loaded = _select_sweep_entries(args.target.expanduser().resolve(), repo_ids=ids)
    selected = {entry.repo_id for entry in entries}
    if not loaded or errors or not selected or (ids is not None and set(ids) != selected):
        raise campaign.PreviewError("repository_selection_refused")
    if len(selected) > campaign.MAX_MEMBERS:
        raise campaign.PreviewError("membership_refused")
    overrides: dict[str, object] = {}
    if args.overrides:
        raw = _read_input(args.overrides)
        if not isinstance(raw, dict) or any(not isinstance(v, str) for v in raw.values()):
            raise campaign.PreviewError("overrides_refused")
        if set(raw) - selected:
            raise campaign.PreviewError("override_for_unknown_repository")
        overrides = raw
    members: dict[str, dict[str, str]] = {}
    for entry in sorted(entries, key=lambda e: e.repo_id):
        identity = _identity(entry)
        if identity in members:
            raise campaign.PreviewError("repository_identity_collision")
        member = {"repo": identity}
        if overrides.get(entry.repo_id):
            member["override"] = str(overrides[entry.repo_id])
        members[identity] = member
    return list(members.values())


def _request_body(args: argparse.Namespace) -> dict[str, Any]:
    key = campaign.opaque_key(args.campaign_id)
    body: dict[str, Any] = {
        "campaign_key": key,
        "title": args.title,
        "acceptance": list(args.acceptance),
        "members": _members(args),
    }
    if args.description_file:
        from ..worklore_validate import DESCRIPTION_MAX

        body["description"] = _read_text(args.description_file, DESCRIPTION_MAX * 4)
    if args.priority:
        body["priority"] = args.priority
    if args.offer_file:
        from ..worklore_campaigns import parse_offer
        from ..worklore_validate import WorkloreValidationError

        try:
            body["offer"] = parse_offer(_read_input(args.offer_file))
        except WorkloreValidationError:
            raise campaign.PreviewError("offer_refused") from None
    return body


def _hub_marks(body: dict[str, Any]) -> dict[str, Any]:
    """One Hub read to mark which members already exist; a failure is reported, never raised."""
    from .. import worklore_client
    from ..worklore_campaigns import parse_offer, spec_fingerprint
    from ..worklore_validate import WorkloreValidationError

    if not worklore_client.load_fleet_settings().get("hub_url"):
        return {"checked": False, "reason": "no fleet hub configured"}
    try:
        shown = worklore_client.get_campaign(str(body["campaign_key"]))
    except worklore_client.WorkloreClientError as exc:
        if exc.code == "not-found":
            return {
                "checked": True,
                "campaign_exists": False,
                "existing": [],
                "spec_changed": False,
                "offer_changed": False,
            }
        return {"checked": False, "error": _text(exc), **({"code": exc.code} if exc.code else {})}
    except worklore_client.FleetClientError as exc:
        return {"checked": False, "error": _text(exc)}
    stored = shown.get("campaign") if isinstance(shown, dict) else None
    members = shown.get("members") if isinstance(shown, dict) else None
    if not isinstance(stored, dict) or not isinstance(members, list):
        return {"checked": False, "error": "unexpected hub response"}
    offer_changed = False
    if "offer" in body:
        if "offer" not in stored:
            return {"checked": False, "error": "unexpected hub response"}
        if stored["offer"] is not None:
            try:
                stored_offer = parse_offer(stored["offer"])
            except WorkloreValidationError:
                return {"checked": False, "error": "unexpected hub response"}
            offer_changed = stored_offer != body["offer"]
    wanted = spec_fingerprint(str(body.get("description", "")), list(body["acceptance"]))
    return {
        "checked": True,
        "campaign_exists": True,
        "existing": sorted(str(m.get("repo")) for m in members if isinstance(m, dict)),
        "spec_changed": stored.get("spec_fingerprint") != wanted,
        "offer_changed": offer_changed,
    }


def _create_acknowledged(result: object, body: dict[str, Any]) -> bool:
    """Require the Hub's create acknowledgement before reporting a successful write."""
    if not isinstance(result, dict) or not isinstance(result.get("created"), bool):
        return False
    stored = result.get("campaign")
    if not isinstance(stored, dict) or stored.get("campaign_key") != body["campaign_key"]:
        return False
    if not isinstance(stored.get("work_id"), str) or not stored["work_id"]:
        return False
    for field in ("added", "existing", "override_conflicts"):
        repos = result.get(field)
        if not isinstance(repos, list) or any(not isinstance(repo, str) or not repo for repo in repos):
            return False
        if len(set(repos)) != len(repos):
            return False
    added, existing, conflicts = (set(result[field]) for field in ("added", "existing", "override_conflicts"))
    requested = {member["repo"] for member in body["members"]}
    return not (added & existing) and (added | existing) == requested and conflicts <= existing


def _create(args: argparse.Namespace) -> int:
    from .. import worklore_client

    try:
        body = _request_body(args)
    except campaign.PreviewError as exc:
        return _refuse(args, str(exc))
    except (OSError, ValueError, TypeError, IndexError, RecursionError, RuntimeError):
        # Existing config parser errors may contain paths or private text.
        return _refuse(args, "input_refused")
    if args.dry_run:
        hub = _hub_marks(body)
        existing = set(hub.get("existing", []))
        members = [{"repo": m["repo"], "exists": m["repo"] in existing} for m in body["members"]]
        if args.json:
            print(
                json.dumps({"dry_run": True, "request": body, "hub": hub, "members": members}, sort_keys=True, indent=2)
            )
            return 0
        print(f"dry run: campaign {_text(body['campaign_key'])}, {len(members)} member(s), nothing written")
        if not hub.get("checked"):
            print(f"hub not checked: {_text(hub.get('error') or hub.get('reason'))}")
        else:
            if hub.get("spec_changed"):
                print("warning: spec differs from the stored campaign; create would be refused (campaign-spec-changed)")
            if hub.get("offer_changed"):
                print(
                    "warning: offer differs from the stored campaign; create would be refused (campaign-offer-conflict)"
                )
        for member in members:
            print(f"  {_text(member['repo'])}  {'existing' if member['exists'] else 'new'}")
        print(json.dumps(body, sort_keys=True, indent=2))
        return 0
    try:
        result = worklore_client.create_campaign(body)
    except worklore_client.FleetClientError as exc:
        return _hub_failure(args, exc)
    if not _create_acknowledged(result, body):
        return _hub_failure(
            args,
            worklore_client.WorkloreClientError(
                "unexpected hub response: create may have been applied. Retry with the same campaign key and spec."
            ),
        )
    if args.json:
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0
    added, kept = _list_of(result.get("added")), _list_of(result.get("existing"))
    conflicts = _list_of(result.get("override_conflicts"))
    verb = "created" if result.get("created") else "updated"
    print(f"{verb} campaign {_text(body['campaign_key'])}: {len(added)} added, {len(kept)} existing")
    for label, repos in (("added", added), ("existing", kept), ("override conflict (kept stored)", conflicts)):
        for repo in repos:
            print(f"  {label}: {_text(repo)}")
    return 0


def _list(args: argparse.Namespace) -> int:
    from .. import worklore_client

    try:
        payload = worklore_client.list_campaigns(limit=args.limit)
    except worklore_client.FleetClientError as exc:
        return _hub_failure(args, exc)
    if args.json:
        print(json.dumps(payload, sort_keys=True, indent=2))
        return 0
    rows: list[tuple[str, ...]] = [
        (
            "KEY",
            "TOTAL",
            "UNOWNED",
            "IN FLIGHT",
            "AWAITING GATE",
            "ATTEMPT LIMITED",
            "BLOCKED",
            "COMPLETED",
            "CANCELED",
            "ARCHIVED",
            "DRIFTED",
            "TITLE",
        )
    ]
    for item in _list_of(payload.get("campaigns")):
        item = _dict(item)
        summary = _dict(item.get("summary"))
        rows.append(
            (
                _text(item.get("campaign_key")),
                *(
                    _count(summary.get(name))
                    for name in (
                        "total",
                        "unowned",
                        "in_flight",
                        "awaiting_gate",
                        "attempt_limited",
                        "blocked",
                        "completed",
                        "canceled",
                        "archived",
                        "drifted",
                    )
                ),
                _text(item.get("title")),
            )
        )
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]) - 1)]
    for row in rows:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row[:-1])) + "  " + row[-1])
    return 0


def _show(args: argparse.Namespace) -> int:
    from .. import worklore_client

    try:
        payload = worklore_client.get_campaign(args.campaign_key)
    except worklore_client.FleetClientError as exc:
        return _hub_failure(args, exc)
    if args.json:
        print(json.dumps(payload, sort_keys=True, indent=2))
        return 0
    stored = _dict(payload.get("campaign"))
    summary = _dict(stored.get("summary"))
    print(
        f"{_text(stored.get('campaign_key'))}: {_text(stored.get('title'))}"
        f" | total {_count(summary.get('total'))}, unowned {_count(summary.get('unowned'))},"
        f" in flight {_count(summary.get('in_flight'))}, awaiting gate {_count(summary.get('awaiting_gate'))},"
        f" attempt limited {_count(summary.get('attempt_limited'))}, blocked {_count(summary.get('blocked'))},"
        f" completed {_count(summary.get('completed'))}, canceled {_count(summary.get('canceled'))},"
        f" archived {_count(summary.get('archived'))},"
        f" drifted {_count(summary.get('drifted'))}" + (" | done" if summary.get("done") is True else "")
    )
    for member in _list_of(payload.get("members")):
        member = _dict(member)
        brigade, github = _dict(member.get("brigade")), _dict(member.get("github"))
        fleet_run = _dict(member.get("fleet_run"))
        pr = _text(github.get("display_ref")) if github else "-"
        if github and github.get("external_state"):
            pr = f"{pr} ({_text(github['external_state'])})"
        print(
            f"  {_text(member.get('repo'))}  {_text(member.get('bucket'))}  {_text(member.get('status'))}"
            f"  attempts {_count(member.get('attempt_count'))}  owner {_text(member.get('owner_node'))}"
            f"  task {_text(brigade.get('display_ref')) if brigade else '-'}"
            f"  run {_text(fleet_run.get('display_ref')) if fleet_run else '-'}  pr {pr}"
            f"  {'drifted' if member.get('drifted') else 'in-spec'}"
        )
    return 0
