"""Cross-repo campaign membership on the Fleet Hub (#1128 slice 1).

A campaign is a Worklore item of kind ``fleet``; each member is a Worklore item of kind
``repo``. These two tables only record which items belong together, so there is no second
task store. ``(campaign_key, repo)`` is the idempotency key: a repeated create adds only
the members that are missing. Nothing here launches, claims or completes anything.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence

from . import worklore_ownership as ownership
from . import worklore_store as store
from .fleet_campaign import MAX_MEMBERS, PreviewError, opaque_key
from .worklore_store import WorkloreConflict, WorkloreNotFound
from .worklore_validate import (
    DESCRIPTION_MAX,
    TITLE_MAX,
    WorkloreValidationError,
    assert_ready,
    parse_create,
    safe_text,
)

LIST_DEFAULT = 20
LIST_MAX = 50
REPO_MAX = 255
REQUEST_FIELDS = frozenset({"campaign_key", "title", "description", "acceptance", "priority", "members", "offer"})
MEMBER_FIELDS = frozenset({"repo", "override"})
TERMINAL_BUCKETS = ("completed", "canceled", "archived")
BUCKETS = ("unowned", "in_flight", "awaiting_gate", "attempt_limited", "blocked", *TERMINAL_BUCKETS)
_HELD_STATES = frozenset({"owned", "handoff-pending"})

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS work_campaigns (
        campaign_key TEXT PRIMARY KEY,
        work_id TEXT NOT NULL UNIQUE REFERENCES work_items(work_id) ON DELETE RESTRICT,
        created_at TEXT NOT NULL,
        created_by TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS work_campaign_members (
        campaign_key TEXT NOT NULL REFERENCES work_campaigns(campaign_key) ON DELETE RESTRICT,
        repo TEXT NOT NULL,
        work_id TEXT NOT NULL UNIQUE REFERENCES work_items(work_id) ON DELETE RESTRICT,
        override TEXT NOT NULL DEFAULT '',
        spec_fingerprint TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (campaign_key, repo)
    )
    """,
    "CREATE INDEX IF NOT EXISTS work_campaigns_created ON work_campaigns (created_at, campaign_key)",
)


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the campaign tables. Additive only; safe to run on every start."""
    for statement in _SCHEMA_STATEMENTS:
        conn.execute(statement)


def _field_bound(message: str) -> WorkloreValidationError:
    return WorkloreValidationError(message, code="field-bound")


def _digest(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def spec_fingerprint(description: str, acceptance: Sequence[str]) -> str:
    """Fingerprint of the shared spec. The title is a label and is not part of it."""
    return _digest({"description": description, "acceptance": list(acceptance)})


def member_fingerprint(spec: str, override: str) -> str:
    """Fingerprint of the combined spec a member was created from."""
    return _digest({"spec": spec, "override": override})


def _campaign_key(value: object) -> str:
    try:
        key = opaque_key(value)
    except PreviewError:
        raise _field_bound("campaign_key must be an opaque key") from None
    return safe_text(key, "campaign_key", max_len=128)


def canonical_identity(value: str) -> str | None:
    """Lowercased ``owner/repo`` (or ``host/path``) identity, github.com host dropped, or None when it is not canonical."""
    text = value.lower().removeprefix("github.com/")
    if len(text) > REPO_MAX or "/" not in text or not ownership._REPO_RE.fullmatch(text):
        return None
    if any(segment in {".", ".."} for segment in text.split("/")):
        return None
    return text


def valid_key(value: str) -> bool:
    try:
        opaque_key(value)
    except PreviewError:
        return False
    return True


def _repo(value: object, index: int) -> str:
    field = f"members[{index}].repo"
    identity = canonical_identity(safe_text(value, field, max_len=REPO_MAX))
    if identity is None:
        raise _field_bound(f"{field} must be a canonical repository identity")
    return identity


def _member_item(title: str, repo: str, override: str, acceptance: Sequence[str]) -> dict[str, Any]:
    label = f"{title} [{repo}]"
    return parse_create(
        {
            "title": label[:TITLE_MAX],
            "kind": "repo",
            "description": override,
            "acceptance": list(acceptance),
            "burn_eligible": 0,
            "execution_mode": "agent",
        }
    )


def parse_request(raw: object) -> dict[str, Any]:
    """Validate a create body into the campaign item fields and its member list."""
    if not isinstance(raw, Mapping):
        raise _field_bound("create body must be an object")
    unknown = [name for name in raw if name not in REQUEST_FIELDS]
    if unknown:
        raise WorkloreValidationError(f"unknown field: {unknown[0]}", code="unknown-field")
    if "campaign_key" not in raw or "title" not in raw or "members" not in raw:
        raise _field_bound("campaign_key, title and members are required")
    key = _campaign_key(raw["campaign_key"])
    body: dict[str, Any] = {
        "title": raw["title"],
        "kind": "fleet",
        "description": raw.get("description", ""),
        "acceptance": raw.get("acceptance", []),
        "burn_eligible": 0,
        "execution_mode": "manual",
    }
    if "priority" in raw:
        body["priority"] = raw["priority"]
    parsed = parse_create(body)
    assert_ready(parsed)
    members_raw = raw["members"]
    if not isinstance(members_raw, list) or not 1 <= len(members_raw) <= MAX_MEMBERS:
        raise _field_bound(f"members must be an array of 1 to {MAX_MEMBERS} entries")
    members: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, entry in enumerate(members_raw):
        if not isinstance(entry, Mapping):
            raise _field_bound(f"members[{index}] must be an object")
        extra = [name for name in entry if name not in MEMBER_FIELDS]
        if extra:
            raise WorkloreValidationError(f"unknown field: {extra[0]}", code="unknown-field")
        repo = _repo(entry.get("repo"), index)
        if repo in seen:
            raise _field_bound(f"members[{index}].repo is a duplicate")
        seen.add(repo)
        override = safe_text(
            entry.get("override", ""), f"members[{index}].override", max_len=DESCRIPTION_MAX, min_len=0
        )
        members.append({"repo": repo, "override": override})
    return {
        "campaign_key": key,
        "item": parsed,
        "members": members,
        "offer": parse_offer(raw["offer"]) if "offer" in raw else None,
    }


def create_campaign(
    conn: sqlite3.Connection,
    raw: object,
    *,
    actor_id: str,
    actor_type: str = "operator",
    node_id: str | None = None,
) -> dict[str, Any]:
    """Create a campaign and its missing members in one transaction; a re-create adds the rest."""
    request = parse_request(raw)
    key = request["campaign_key"]
    parsed = request["item"]
    requested_spec = spec_fingerprint(parsed["description"], parsed["acceptance"])

    def _create_item(fields: Mapping[str, Any], now: str) -> str:
        item = store._item_from_create(fields, work_id=store._mint_id("wl-"), now=now)
        store._insert_item(conn, item)
        store._append_event(
            conn,
            work_id=item["work_id"],
            event_type="created",
            from_status=None,
            to_status=item["status"],
            actor_type=actor_type,
            actor_id=actor_id,
            now=now,
            node_id=node_id,
        )
        return str(item["work_id"])

    def _write() -> dict[str, Any]:
        now = store._utc_now()
        row = conn.execute("SELECT work_id FROM work_campaigns WHERE campaign_key = ?", (key,)).fetchone()
        created = row is None
        stored = {
            str(repo): str(override)
            for repo, override in conn.execute(
                "SELECT repo, override FROM work_campaign_members WHERE campaign_key = ?", (key,)
            )
        }
        if len(stored.keys() | {member["repo"] for member in request["members"]}) > MAX_MEMBERS:
            raise _field_bound(f"campaign must contain at most {MAX_MEMBERS} members in total")
        title = parsed["title"]
        if row is None:
            campaign_work_id = _create_item(parsed, now)
            conn.execute(
                "INSERT INTO work_campaigns (campaign_key, work_id, created_at, created_by) VALUES (?, ?, ?, ?)",
                (key, campaign_work_id, now, actor_id),
            )
            stored_spec = requested_spec
        else:
            campaign_work_id = str(row[0])
            current = store._loaded_item(conn, campaign_work_id)
            title = current["title"]
            stored_spec = spec_fingerprint(current["description"], current["acceptance"])
            if stored_spec != requested_spec:
                raise WorkloreConflict(
                    "campaign spec differs from the stored campaign; patch the campaign item to change the spec",
                    code="campaign-spec-changed",
                )
        requested_offer = request["offer"]
        current_offer = campaign_offer(conn, campaign_work_id)
        if requested_offer is not None:
            if current_offer is not None and current_offer["offer"] != requested_offer:
                raise WorkloreConflict("campaign offer is immutable", code="campaign-offer-conflict")
            if current_offer is None:
                status = store._loaded_item(conn, campaign_work_id)["status"]
                store._append_event(
                    conn,
                    work_id=campaign_work_id,
                    event_type="campaign-offered",
                    from_status=status,
                    to_status=status,
                    actor_type=actor_type,
                    actor_id=actor_id,
                    node_id=node_id,
                    now=now,
                    detail={"offer": requested_offer},
                )
        added: list[str] = []
        existing: list[str] = []
        conflicts: list[str] = []
        for member in request["members"]:
            repo = member["repo"]
            if repo in stored:
                existing.append(repo)
                if stored[repo] != member["override"]:
                    conflicts.append(repo)
                continue
            member_work_id = _create_item(_member_item(title, repo, member["override"], parsed["acceptance"]), now)
            conn.execute(
                "INSERT INTO work_campaign_members "
                "(campaign_key, repo, work_id, override, spec_fingerprint, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    key,
                    repo,
                    member_work_id,
                    member["override"],
                    member_fingerprint(stored_spec, member["override"]),
                    now,
                ),
            )
            added.append(repo)
        if added:
            status = "captured" if created else store._loaded_item(conn, campaign_work_id)["status"]
            store._append_event(
                conn,
                work_id=campaign_work_id,
                event_type="campaign-members-added",
                from_status=status,
                to_status=status,
                actor_type=actor_type,
                actor_id=actor_id,
                now=now,
                node_id=node_id,
                detail={"campaign_key": key, "added": added},
            )
        return {
            "campaign": _campaign(conn, key),
            "created": created,
            "added": added,
            "existing": existing,
            "override_conflicts": conflicts,
        }

    return store._in_transaction(conn, _write)


def member_bucket(item: Mapping[str, Any], held: Mapping[str, Any] | None) -> str:
    """Rollup bucket for one member item and its ownership projection."""
    status = str(item["status"])
    if status in TERMINAL_BUCKETS:
        return status
    if status == "blocked":
        return "blocked"
    if status == "verifying":
        return "awaiting_gate"
    if status in {"claimed", "running"} or (held is not None and held.get("state") in _HELD_STATES):
        return "in_flight"
    if int(item["attempt_count"]) >= store.ATTEMPT_LIMIT:
        return "attempt_limited"
    return "unowned"


def _holder(conn: sqlite3.Connection, work_id: str) -> dict[str, Any]:
    return ownership._public(ownership._current(conn, work_id))


def _latest_link(conn: sqlite3.Connection, work_id: str, link_type: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT display_ref, url, external_state FROM work_links WHERE work_id = ? AND link_type = ? "
        "ORDER BY external_updated_at IS NULL, external_updated_at DESC, synced_at DESC, link_id DESC LIMIT 1",
        (work_id, link_type),
    ).fetchone()
    if row is None:
        return None
    return {"display_ref": row[0], "url": row[1], "external_state": row[2]}


def _campaign_row(conn: sqlite3.Connection, key: str) -> Any:
    row = conn.execute(
        "SELECT campaign_key, work_id, created_at, created_by FROM work_campaigns WHERE campaign_key = ?", (key,)
    ).fetchone()
    if row is None:
        raise WorkloreNotFound("campaign not found")
    return row


def _member_rows(conn: sqlite3.Connection, key: str) -> list[Any]:
    return conn.execute(
        "SELECT repo, work_id, override, spec_fingerprint FROM work_campaign_members "
        "WHERE campaign_key = ? ORDER BY repo ASC",
        (key,),
    ).fetchall()


def _members(conn: sqlite3.Connection, key: str, spec: str) -> list[dict[str, Any]]:
    rows = []
    for repo, work_id, override, stored in _member_rows(conn, key):
        item = store._loaded_item(conn, str(work_id))
        held = _holder(conn, str(work_id))
        rows.append(
            {
                "repo": str(repo),
                "work_id": str(work_id),
                "status": item["status"],
                "bucket": member_bucket(item, held),
                "attempt_count": item["attempt_count"],
                "drifted": str(stored) != member_fingerprint(spec, str(override)),
                "owner_node": held.get("owner_node"),
                "ownership_state": held.get("state"),
                "brigade": _latest_link(conn, str(work_id), "brigade"),
                "fleet_run": _latest_link(conn, str(work_id), "fleet-run"),
                "github": _latest_link(conn, str(work_id), "github"),
            }
        )
    return rows


def summarize(members: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Rollup counts for member rows, plus whether every member is terminal."""
    counts = {bucket: 0 for bucket in BUCKETS}
    for member in members:
        counts[str(member["bucket"])] += 1
    done = bool(members) and all(counts[bucket] == 0 for bucket in BUCKETS if bucket not in TERMINAL_BUCKETS)
    return {
        "total": len(members),
        **counts,
        "drifted": sum(1 for member in members if member["drifted"]),
        "done": done,
    }


def _campaign(conn: sqlite3.Connection, key: str, *, members: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    row = _campaign_row(conn, key)
    item = store._loaded_item(conn, str(row[1]))
    spec = spec_fingerprint(item["description"], item["acceptance"])
    return {
        "campaign_key": str(row[0]),
        "work_id": str(row[1]),
        "title": item["title"],
        "description": item["description"],
        "acceptance": item["acceptance"],
        "priority": item["priority"],
        "status": item["status"],
        "spec_fingerprint": spec,
        "created_at": str(row[2]),
        "created_by": str(row[3]),
        "offer": (campaign_offer(conn, str(row[1])) or {}).get("offer"),
        "summary": summarize(members if members is not None else _members(conn, key, spec)),
    }


def _limit(value: object) -> int:
    if isinstance(value, str) and value.isdigit() and len(value) <= 10:
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= LIST_MAX:
        raise _field_bound(f"limit must be an integer from 1 to {LIST_MAX}")
    return value


def list_campaigns(conn: sqlite3.Connection, *, limit: object = LIST_DEFAULT) -> dict[str, Any]:
    """Newest campaigns first, each with its rollup counts."""
    bound = _limit(limit)
    store._pragma_foreign_keys(conn)
    keys = [
        str(row[0])
        for row in conn.execute(
            "SELECT campaign_key FROM work_campaigns ORDER BY created_at DESC, campaign_key DESC LIMIT ?", (bound,)
        )
    ]
    return {"campaigns": [_campaign(conn, key) for key in keys]}


def show_campaign(conn: sqlite3.Connection, campaign_key: str) -> dict[str, Any]:
    """The campaign projection plus one row per member; an unknown key is not found."""
    store._pragma_foreign_keys(conn)
    row = _campaign_row(conn, campaign_key)
    item = store._loaded_item(conn, str(row[1]))
    members = _members(conn, campaign_key, spec_fingerprint(item["description"], item["acceptance"]))
    return {"campaign": _campaign(conn, campaign_key, members=members), "members": members}


def parse_offer(raw: object) -> dict[str, Any]:
    """Validate a closed campaign authorization; no offer means no self-assignment."""
    if not isinstance(raw, Mapping):
        raise _field_bound("campaign offer must be an object")
    required = {"allowed_nodes", "per_node_concurrency", "attempt_budget", "authorization_ref", "exclusions"}
    if set(raw) - (required | {"lease_seconds"}):
        raise WorkloreValidationError("unknown campaign offer field", code="unknown-field")
    if not required <= set(raw):
        raise _field_bound("campaign offer requires all authorization bounds")
    nodes = [ownership._node(node) for node in ownership._array(raw["allowed_nodes"], 32)]
    if not nodes or len(set(nodes)) != len(nodes):
        raise _field_bound("campaign offer requires distinct allowed nodes")
    budget = ownership._object(raw["attempt_budget"], {"cap", "source_ref"})
    exclusions = [_repo(value, index) for index, value in enumerate(ownership._array(raw["exclusions"], 32))]
    if len(set(exclusions)) != len(exclusions):
        raise _field_bound("campaign exclusions must be distinct repository identities")
    return {
        "allowed_nodes": sorted(nodes),
        "per_node_concurrency": ownership._integer(raw["per_node_concurrency"], minimum=1, maximum=32),
        "attempt_budget": {
            "cap": ownership._integer(budget["cap"], minimum=1, maximum=ownership.BUDGET_CAP_MAX),
            "source_ref": ownership._reference(budget["source_ref"]),
        },
        "authorization_ref": ownership._reference(raw["authorization_ref"]),
        "exclusions": sorted(exclusions),
        "lease_seconds": ownership._integer(raw.get("lease_seconds", 300), minimum=1, maximum=3600),
    }


def campaign_offer(conn: sqlite3.Connection, work_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT event_id, actor_id, actor_type, detail_json FROM work_events "
        "WHERE work_id=? AND event_type='campaign-offered' ORDER BY seq DESC LIMIT 1",
        (work_id,),
    ).fetchone()
    if row is None:
        return None
    return {"event_id": row[0], "actor_id": row[1], "actor_type": row[2], "offer": json.loads(row[3])["offer"]}


def member_campaign(conn: sqlite3.Connection, work_id: str) -> dict[str, str] | None:
    """Indexed backlink for old and new members, derived from canonical membership."""
    row = conn.execute(
        "SELECT m.campaign_key, c.work_id, m.repo FROM work_campaign_members m "
        "JOIN work_campaigns c ON c.campaign_key=m.campaign_key WHERE m.work_id=?",
        (work_id,),
    ).fetchone()
    return None if row is None else {"campaign_key": row[0], "work_id": row[1], "repo": row[2]}


def _eligible_campaign(conn: sqlite3.Connection, member: Mapping[str, str]) -> dict[str, Any]:
    campaign = store._loaded_item(conn, member["work_id"])
    if campaign["status"] in {*TERMINAL_BUCKETS, "blocked", "deferred"} or campaign.get("blocker"):
        raise WorkloreConflict("campaign is not eligible for assignment", code="campaign-ineligible")
    grant = campaign_offer(conn, member["work_id"])
    if grant is None:
        raise store.WorkloreForbidden("campaign has no authorization offer", code="campaign-not-authorized")
    return grant


def take_authority(
    conn: sqlite3.Connection, work_id: str, item: Mapping[str, Any], current: Mapping[str, Any], *, node: str, now: str
) -> dict[str, Any]:
    """Check eligibility and per-node occupancy while ownership holds BEGIN IMMEDIATE."""
    member = member_campaign(conn, work_id)
    if member is None:
        raise store.WorkloreForbidden("take requires campaign membership", code="campaign-not-authorized")
    grant = _eligible_campaign(conn, member)
    offer = grant["offer"]
    if node not in offer["allowed_nodes"]:
        raise store.WorkloreForbidden("node is outside campaign authorization", code="campaign-not-authorized")
    if member["repo"] in offer["exclusions"]:
        raise WorkloreConflict("repository is excluded from campaign assignment", code="campaign-ineligible")
    reclaim = (
        current["state"] in _HELD_STATES
        and current.get("campaign_offer_event") == grant["event_id"]
        and ownership.expired(current, now)
    )
    allowed_statuses = {"captured", "ready", "claimed", "running"} if reclaim else {"captured", "ready"}
    if (
        item["status"] not in allowed_statuses
        or item.get("blocker")
        or store._review_after_future(item.get("review_after"))
        or int(item["attempt_count"]) >= min(store.ATTEMPT_LIMIT, offer["attempt_budget"]["cap"])
    ):
        raise WorkloreConflict("member is not eligible for assignment", code="campaign-ineligible")
    spec = store._loaded_item(conn, member["work_id"])
    override, fingerprint = conn.execute(
        "SELECT override, spec_fingerprint FROM work_campaign_members WHERE work_id=?", (work_id,)
    ).fetchone()
    expected = member_fingerprint(spec_fingerprint(spec["description"], spec["acceptance"]), override)
    if fingerprint != expected or item["description"] != override or item["acceptance"] != spec["acceptance"]:
        raise WorkloreConflict("member specification has drifted", code="campaign-ineligible")
    if current["state"] != "unowned" and not ownership.expired(current, now):
        raise WorkloreConflict("member ownership is already reserved", code="ownership-conflict")
    active = 0
    # Membership can grow through re-create, so do not truncate the occupancy set.
    for (identifier,) in conn.execute(
        "SELECT work_id FROM work_campaign_members WHERE campaign_key=?", (member["campaign_key"],)
    ):
        held = ownership._current(conn, identifier)
        if held["state"] in _HELD_STATES and held["owner_node"] == node and not ownership.expired(held, now):
            active += 1
    if active >= offer["per_node_concurrency"]:
        raise WorkloreConflict("campaign per-node concurrency limit reached", code="campaign-concurrency-limit")
    return {
        "granted_by": {"actor_id": grant["actor_id"], "actor_type": grant["actor_type"]},
        "authorization_ref": offer["authorization_ref"],
        "attempt_budget": offer["attempt_budget"],
        "exclusions": offer["exclusions"],
        "campaign_key": member["campaign_key"],
        "campaign_repo": member["repo"],
        "campaign_offer_event": grant["event_id"],
        "expires_at": (datetime.fromisoformat(now) + timedelta(seconds=offer["lease_seconds"])).isoformat(),
    }
