"""Fenced ownership metadata in immutable Worklore events, without schema changes.

HTTP supplies the authenticated bearer principal. Ownership controls this Hub
protocol only; physical writers, locks, and verification remain external.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import sqlite3
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any, Mapping

from . import worklore_scope as scope
from . import worklore_store as store
from .worklore_validate import WorkloreValidationError, safe_text

ITEM_MAX_OWNERSHIP_EVENTS = 200
ITEM_MAX_OWNERSHIP_TOTAL = 201
OWNERSHIP_EVENT_MAX_BYTES = 65536
OFFER_MAX_PRIOR_EVENTS = 197
ACTION_MAX = 128
IDEMPOTENCY_KEY_MAX = 128
NODE_ID_MAX = 128
REPO_IDENTITY_MAX = 255
SCOPE_MAX_ITEMS = 32
SCOPE_PATH_MAX = 256
EXCLUSIONS_MAX_ITEMS = 32
EXCLUSION_MAX = 256
REFERENCE_MAX = 128
BUDGET_CAP_MAX = 100000
RESUME_CONDITION_MAX = 512
EVIDENCE_MAX_ITEMS = 20
SOURCE_REVISION_LENGTH = 40
COUNTER_MAX = 9999999999
NONCE_BYTES = 32
NONCE_CHARS = 43
NEXT_ACTION_KINDS = frozenset({"implement", "verify", "await-review", "await-checks", "await-merge", "blocked"})
EVIDENCE_KINDS = frozenset({"receipt", "github-pr", "github-check-run", "worklore-event"})
TERMINAL_STATUSES = frozenset({"completed", "canceled", "archived"})
EVENT_TYPES = {
    "offer": "ownership-offered",
    "withdraw": "ownership-offer-withdrawn",
    "accept": "ownership-accepted",
    "take": "ownership-taken",
    "checkpoint": "ownership-checkpoint",
    "report": "ownership-reported",
    "handoff": "ownership-handoff-offered",
    "release": "ownership-released",
    "recover": "ownership-recovered",
}
EXTERNAL_EVENT_TYPES = {
    "admit": "external-ownership-admitted",
    "register": "external-ownership-registered",
    "renew": "external-ownership-renewed",
    "report": "external-ownership-reported",
    "release": "external-ownership-released",
    "expire": "external-ownership-expired",
    "withdraw": "external-ownership-withdrawn",
}
ALL_OWNERSHIP_EVENT_TYPES = (*EVENT_TYPES.values(), *EXTERNAL_EVENT_TYPES.values())
_OWNERSHIP_BINDS = tuple(EVENT_TYPES.values())
_EXTERNAL_NAMESPACE_PREDICATE = """CASE typeof(event_type)
 WHEN 'text' THEN substr(event_type,1,19) COLLATE BINARY = 'external-ownership-'
 WHEN 'blob' THEN substr(event_type,1,19) = X'65787465726e616c2d6f776e6572736869702d'
 ELSE 0
END"""
_OWNERSHIP_PREDICATE = (
    "(event_type IN (" + ",".join("?" for _ in _OWNERSHIP_BINDS) + ") OR (" + _EXTERNAL_NAMESPACE_PREDICATE + "))"
)
_EXTERNAL_HISTORY_SQL = (
    "SELECT 1 FROM work_events WHERE work_id=? AND typeof(event_type)='text' "
    "AND event_type COLLATE BINARY >= 'external-ownership-' "
    "AND event_type COLLATE BINARY < 'external-ownership.' "
    "UNION ALL SELECT 1 FROM work_events WHERE work_id=? AND typeof(event_type)='blob' "
    "AND event_type >= X'65787465726e616c2d6f776e6572736869702d' "
    "AND event_type < X'65787465726e616c2d6f776e6572736869702e' LIMIT 1"
)
_EXTERNAL_UNSUPPORTED = "unsupported external ownership version; ownership unchanged"
_FIELDS = {
    "offer": {"action", "target_node", "exclusions", "authorization_ref", "attempt_budget"},
    "withdraw": {"action"},
    "accept": {"action", "generation"},
    "take": {"action", "generation"},
    "checkpoint": {
        "action",
        "generation",
        "repo_identity",
        "write_scope",
        "source_revision",
        "next_action",
        "evidence_refs",
    },
    "report": {"action", "generation", "report"},
    "handoff": {"action", "generation", "target_node"},
    "release": {"action", "generation"},
    "recover": {"action", "generation", "holder_quiesced", "evidence_ref"},
}
_NODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")
# A slash within a relative reference is permitted. Absolute references at a
# text boundary (including quotes, parentheses, and assignments) are not.
_ABSOLUTE_PATH_RE = re.compile(r"(?<![\w./-])(?:/|[A-Za-z]:[\\/]|\\\\)")
_EVENT_PREDICATE = "event_type IN (" + ",".join("?" for _ in EVENT_TYPES) + ")"


def _invalid(
    message: str = "ownership field is out of bounds", *, code: str = "field-bound"
) -> WorkloreValidationError:
    return WorkloreValidationError(message, code=code)


def _object(raw: object, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise _invalid("ownership field must be an object")
    if any(key not in allowed for key in raw):
        # Never reflect an untrusted field name (which could itself be a secret).
        raise _invalid("unknown ownership field", code="unknown-field")
    if set(raw) != allowed:
        raise _invalid("required ownership field is missing")
    return dict(raw)


def _integer(raw: object, *, minimum: int = 0, maximum: int = COUNTER_MAX) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or not minimum <= raw <= maximum:
        raise _invalid("ownership integer is out of bounds")
    return raw


def _text(raw: object, field: str, maximum: int, *, minimum: int = 1) -> str:
    text = safe_text(raw, field, max_len=maximum, min_len=minimum)
    if _ABSOLUTE_PATH_RE.search(text):
        raise _invalid("ownership metadata must not contain absolute paths", code="private-data")
    if "\u2028" in text or "\u2029" in text:
        raise _invalid("ownership text must be a single line")
    if minimum and not text.strip():
        raise _invalid("ownership text must not be blank")
    return text


def _reference(raw: object) -> str:
    text = _text(raw, "reference", REFERENCE_MAX)
    if not store.EVIDENCE_REF_RE.fullmatch(text):
        raise _invalid("ownership reference is invalid")
    return text


def _node(raw: object) -> str:
    text = _text(raw, "node", NODE_ID_MAX)
    if text in {"admin", "unknown"} or not _NODE_RE.fullmatch(text):
        raise _invalid("ownership node is invalid")
    return text


def _array(raw: object, maximum: int) -> list[Any]:
    if not isinstance(raw, list) or len(raw) > maximum:
        raise _invalid("ownership array is out of bounds")
    return raw


def _source_revision(raw: object) -> str:
    text = _text(raw, "source_revision", SOURCE_REVISION_LENGTH)
    if not _REVISION_RE.fullmatch(text):
        raise _invalid("source_revision must be a lowercase 40-character hex revision")
    return text


def _path(raw: object, field: str = "write_scope", maximum: int = SCOPE_PATH_MAX) -> str:
    text = _text(raw, field, maximum)
    if any(char in text for char in "\\:*?[]") or any(part in {"", ".", ".."} for part in text.split("/")):
        raise _invalid("ownership path must be canonical and relative")
    return text


def _parse(raw: object) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise _invalid("ownership body must be an object")
    action = _text(raw.get("action"), "action", ACTION_MAX)
    if action not in _FIELDS:
        raise _invalid("ownership action is invalid")
    body = _object(raw, _FIELDS[action])
    if "generation" in body:
        body["generation"] = _integer(body["generation"])
    if "target_node" in body:
        body["target_node"] = _node(body["target_node"])
    if action == "offer":
        body["exclusions"] = [
            _text(value, "exclusions", EXCLUSION_MAX) for value in _array(body["exclusions"], EXCLUSIONS_MAX_ITEMS)
        ]
        body["authorization_ref"] = _reference(body["authorization_ref"])
        budget = _object(body["attempt_budget"], {"cap", "source_ref"})
        body["attempt_budget"] = {
            "cap": _integer(budget["cap"], minimum=1, maximum=BUDGET_CAP_MAX),
            "source_ref": _reference(budget["source_ref"]),
        }
    if action == "recover":
        if body["holder_quiesced"] is not True:
            raise _invalid("recovery requires explicit holder quiescence acknowledgement")
        body["evidence_ref"] = _reference(body["evidence_ref"])
    if action == "report":
        from .fleet_dot import DotReportError, validate_ownership_report

        try:
            body["report"] = validate_ownership_report(body["report"])
        except DotReportError:
            raise _invalid("invalid bounded ownership report") from None
    if action == "checkpoint":
        body["repo_identity"] = _path(body["repo_identity"], "repo_identity", REPO_IDENTITY_MAX)
        if not _REPO_RE.fullmatch(body["repo_identity"]):
            raise _invalid("repository identity must be a canonical relative identifier")
        body["write_scope"] = [_path(value) for value in _array(body["write_scope"], SCOPE_MAX_ITEMS)]
        if len(set(body["write_scope"])) != len(body["write_scope"]):
            raise _invalid("write_scope must contain distinct paths")
        body["source_revision"] = _source_revision(body["source_revision"])
        next_action = _object(body["next_action"], {"kind", "resume_condition"})
        kind = _text(next_action["kind"], "next_action.kind", ACTION_MAX)
        if kind not in NEXT_ACTION_KINDS:
            raise _invalid("next_action kind is invalid")
        body["next_action"] = {
            "kind": kind,
            "resume_condition": _text(
                next_action["resume_condition"], "resume_condition", RESUME_CONDITION_MAX, minimum=0
            ),
        }
        evidence = []
        for value in _array(body["evidence_refs"], EVIDENCE_MAX_ITEMS):
            record = _object(value, {"kind", "ref", "source_revision"})
            kind = _text(record["kind"], "evidence.kind", ACTION_MAX)
            if kind not in EVIDENCE_KINDS:
                raise _invalid("evidence kind is invalid")
            evidence.append(
                {
                    "kind": kind,
                    "ref": _reference(record["ref"]),
                    "source_revision": _source_revision(record["source_revision"]),
                }
            )
        body["evidence_refs"] = evidence
    return body


def nonce_hash(raw: object) -> str:
    """Validate the header-only capability and return only its SHA256 digest."""
    if not isinstance(raw, str) or not _NONCE_RE.fullmatch(raw):
        raise _invalid("holder header must contain a canonical 32-byte base64url nonce")
    try:
        decoded = base64.b64decode(raw + "=", altchars=b"-_", validate=True)
    except (ValueError, binascii.Error):
        raise _invalid("holder header is invalid") from None
    if len(decoded) != NONCE_BYTES or base64.urlsafe_b64encode(decoded).decode().rstrip("=") != raw:
        raise _invalid("holder header is invalid")
    return hashlib.sha256(decoded).hexdigest()


def _reject_nonce_echo(raw: object, key: object, nonce: str) -> None:
    """Reject the presented capability and its standard base64/hex spellings."""
    decoded = base64.urlsafe_b64decode(nonce + "=")
    standard = base64.b64encode(decoded).decode()
    secrets = {nonce, standard, standard.rstrip("="), decoded.hex(), decoded.hex().upper()}
    pending = [raw, key]
    while pending:
        value = pending.pop()
        if isinstance(value, str) and any(secret in value for secret in secrets):
            raise _invalid("ownership metadata must not contain holder secret", code="private-data")
        if isinstance(value, Mapping):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)


def _initial() -> dict[str, Any]:
    return {
        "state": "unowned",
        "generation": 0,
        "revision": 0,
        "owner_node": None,
        "offered_to": None,
        "holder_hash": None,
        "granted_by": None,
        "authorization_ref": None,
        "exclusions": [],
        "attempt_budget": None,
        "repo_identity": None,
        "write_scope": [],
        "source_revision": None,
        "next_action": None,
        "evidence_refs": [],
        "last_report": None,
        "last_seq": None,
        "updated_at": None,
        "liveness": "unknown",
        "conflict_check": "not-performed",
    }


def _current(conn: sqlite3.Connection, work_id: str) -> dict[str, Any]:
    if conn.execute(_EXTERNAL_HISTORY_SQL, (work_id, work_id)).fetchone() is not None:
        raise store.WorkloreConflict(_EXTERNAL_UNSUPPORTED, code="ownership-conflict")
    row = conn.execute(
        f"SELECT detail_json FROM work_events WHERE work_id=? AND {_EVENT_PREDICATE} ORDER BY seq DESC LIMIT 1",
        (work_id, *EVENT_TYPES.values()),
    ).fetchone()
    return _initial() if row is None else json.loads(row[0])["ownership"]


def _public(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in snapshot.items() if key != "holder_hash"}


def get_ownership(conn: sqlite3.Connection, work_id: str) -> dict[str, Any]:
    """Read an ownership projection only for an existing Worklore item."""
    store.get_item(conn, work_id)
    return _public(_current(conn, work_id))


def _enrolled(conn: sqlite3.Connection, node: str) -> None:
    if conn.execute("SELECT 1 FROM nodes WHERE node_id=? AND revoked_at IS NULL", (node,)).fetchone() is None:
        raise store.WorkloreForbidden("ownership requires an enrolled nonrevoked node", code="holder-mismatch")


def _authorize(
    action: str,
    authority: Mapping[str, Any],
    *,
    actor_id: str,
    is_admin: bool,
    is_operator: bool,
    generation: int | None,
    digest: str | None,
) -> None:
    if action in {"offer", "withdraw", "recover"}:
        if not (is_admin or is_operator):
            raise store.WorkloreForbidden("operator token required", code="forbidden")
        if action == "recover" and generation != authority["generation"]:
            raise store.WorkloreConflict("ownership generation does not match", code="stale-generation")
        return
    if is_admin:
        raise store.WorkloreForbidden("node holder authority required", code="holder-mismatch")
    if generation != authority["generation"]:
        raise store.WorkloreConflict("ownership generation does not match", code="stale-generation")
    if action == "take":
        return  # Enrollment and campaign authorization run under the same write fence.
    expected_node = authority["offered_to"] if action == "accept" else authority["owner_node"]
    if actor_id != expected_node:
        raise store.WorkloreForbidden("ownership holder does not match", code="holder-mismatch")
    if action != "accept" and not hmac.compare_digest(str(authority["holder_hash"] or ""), str(digest or "")):
        raise store.WorkloreForbidden("ownership holder does not match", code="holder-mismatch")


def expired(snapshot: Mapping[str, Any], now: str) -> bool:
    """Only a valid explicit campaign deadline expires. Legacy holders stay reserved."""
    deadline = snapshot.get("expires_at")
    if not isinstance(deadline, str):
        return False
    try:
        stamp, current = datetime.fromisoformat(deadline), datetime.fromisoformat(now)
        return stamp.tzinfo is not None and current.tzinfo is not None and stamp <= current
    except ValueError:
        return False


def require_campaign_write(
    conn: sqlite3.Connection,
    work_id: str,
    item: Mapping[str, Any],
    *,
    actor_id: str,
    is_admin: bool,
    is_operator: bool,
    generation: object,
    holder_nonce: object,
) -> dict[str, Any] | None:
    """Fence new side effects under the caller's existing write transaction.

    Trusted operator observations are explicit authority, never inferred from an
    actor label. Node writes must present the current campaign holder capability.
    The returned binding can be persisted on an execution, without its secret.
    """
    from . import worklore_campaigns as campaigns

    member = campaigns.member_campaign(conn, work_id)
    if member is None or is_admin or is_operator:
        return None
    _enrolled(conn, actor_id)
    grant = campaigns._eligible_campaign(conn, member)
    current = _current(conn, work_id)
    if generation is None or holder_nonce is None:
        raise store.WorkloreForbidden("campaign holder capability required", code="holder-mismatch")
    digest = nonce_hash(holder_nonce)
    _authorize(
        "checkpoint",
        current,
        actor_id=actor_id,
        is_admin=False,
        is_operator=False,
        generation=_integer(generation),
        digest=digest,
    )
    if current["state"] != "owned":
        raise store.WorkloreConflict("campaign ownership is not active", code="ownership-conflict")
    if expired(current, store._utc_now()):
        raise store.WorkloreConflict("campaign ownership lease expired", code="stale-generation")
    offer = grant["offer"]
    if (
        current.get("campaign_offer_event") != grant["event_id"]
        or actor_id not in offer["allowed_nodes"]
        or member["repo"] in offer["exclusions"]
    ):
        raise store.WorkloreForbidden("campaign grant does not authorize holder", code="campaign-not-authorized")
    if item["status"] in TERMINAL_STATUSES or int(item["attempt_count"]) >= min(
        store.ATTEMPT_LIMIT, offer["attempt_budget"]["cap"]
    ):
        raise store.WorkloreConflict("member is not eligible for campaign progress", code="campaign-ineligible")
    return {"generation": current["generation"], "holder_hash": digest, "campaign_offer_event": grant["event_id"]}


def _transition(
    snapshot: dict[str, Any], body: Mapping[str, Any], *, actor_id: str, actor_type: str, digest: str | None
) -> dict[str, Any]:
    action = body["action"]
    allowed = {
        "offer": {"unowned"},
        "withdraw": {"offered"},
        "accept": {"offered", "handoff-pending"},
        "checkpoint": {"owned"},
        "report": {"owned"},
        "handoff": {"owned"},
        "release": {"owned", "handoff-pending"},
        "recover": {"owned", "handoff-pending"},
    }
    if snapshot["state"] not in allowed[action]:
        raise store.WorkloreConflict("ownership state does not permit action", code="ownership-conflict")
    result = dict(snapshot)
    if action == "offer":
        result.update(
            state="offered",
            offered_to=body["target_node"],
            granted_by={"actor_id": actor_id, "actor_type": actor_type},
            exclusions=body["exclusions"],
            authorization_ref=body["authorization_ref"],
            attempt_budget=body["attempt_budget"],
        )
    elif action == "accept":
        result.update(
            state="owned",
            owner_node=actor_id,
            offered_to=None,
            holder_hash=digest,
            generation=snapshot["generation"] + 1,
        )
    elif action == "handoff":
        result.update(state="handoff-pending", offered_to=body["target_node"])
    elif action == "report":
        incoming = body["report"]
        result["last_report"] = dict(incoming) | {"reporter_node": actor_id}
    elif action == "checkpoint":
        result.update({key: value for key, value in body.items() if key not in {"action", "generation"}})
        result["conflict_check"] = "path-admission"
    else:
        result = _initial()
        result["generation"] = snapshot["generation"] + (1 if action == "recover" else 0)
    result["revision"] = snapshot["revision"] + 1
    return result


def _validate_report_history(
    conn: sqlite3.Connection, work_id: str, incoming: Mapping[str, Any], *, reporter_node: str
) -> None:
    """Fence each reporter/session against its latest bounded immutable report.

    The caller holds the ownership write transaction. Checking only last_report
    would let an interleaved report for another session hide stale provenance.
    """
    rows = conn.execute(
        "SELECT detail_json FROM work_events WHERE work_id=? AND event_type='ownership-reported' "
        "AND node_id=? ORDER BY seq DESC LIMIT ?",
        (work_id, reporter_node, ITEM_MAX_OWNERSHIP_TOTAL),
    )
    for row in rows:
        prior = json.loads(row[0])["ownership"]["last_report"]
        if prior["session_id"] != incoming["session_id"]:
            continue
        linkage = (
            "version",
            "provider",
            "agent_label",
            "parent_session_id",
            "repo_identity",
            "source",
            "source_scope",
            "coverage",
        )
        if (
            incoming["sequence"] <= prior["sequence"]
            or incoming["observed_at"] < prior["observed_at"]
            or any(prior.get(k) != incoming.get(k) for k in linkage)
        ):
            raise store.WorkloreConflict("report observation conflicts; ownership unchanged", code="ownership-conflict")
        return


def ownership_action(
    conn: sqlite3.Connection,
    work_id: str,
    raw: object,
    *,
    expected_revision: int,
    idempotency_key: object,
    actor_id: str,
    actor_type: str,
    is_admin: bool = False,
    is_operator: bool = False,
    holder_nonce: object = None,
) -> dict[str, Any]:
    """Serialize validation, historical replay authority, CAS, and event insertion."""
    supplied_digest = nonce_hash(holder_nonce) if holder_nonce is not None else None
    if isinstance(holder_nonce, str):
        _reject_nonce_echo(raw, idempotency_key, holder_nonce)
    expected_revision = _integer(expected_revision)
    if idempotency_key is None or idempotency_key == "":
        raise _invalid("Idempotency-Key is required", code="idempotency-key-required")
    key = _text(idempotency_key, "idempotency_key", IDEMPOTENCY_KEY_MAX)
    body = _parse(raw)
    action = body["action"]
    digest = (supplied_digest or nonce_hash(holder_nonce)) if action not in {"offer", "withdraw", "recover"} else None
    actor_id = "admin" if is_admin else _node(actor_id)
    actor_type = "admin" if is_admin else "node"
    # Include principal type, not merely a node/key pair that could collide with admin.
    event_id = "own-" + hashlib.sha256(f"{actor_type}\0{actor_id}\0{key}".encode()).hexdigest()[:32]
    fingerprint = hashlib.sha256(
        json.dumps(
            {"body": body, "expected_revision": expected_revision, "holder_hash": digest},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()

    def write() -> dict[str, Any]:
        from . import worklore_campaigns as campaigns

        item = store.get_item(conn, work_id)
        if not is_admin:
            _enrolled(conn, actor_id)
        if action in {"offer", "withdraw", "recover"} and not (is_admin or is_operator):
            raise store.WorkloreForbidden("operator token required", code="forbidden")
        if action == "take":
            if is_admin:
                raise store.WorkloreForbidden("node holder authority required", code="holder-mismatch")
        member = campaigns.member_campaign(conn, work_id)
        if action == "recover" and member is None:
            raise store.WorkloreForbidden("recovery requires campaign membership", code="campaign-not-authorized")
        if member is not None and action in {"offer", "accept", "handoff"}:
            raise store.WorkloreForbidden(
                "campaign members require policy-bounded take", code="campaign-not-authorized"
            )
        # Permanent domain fence also precedes historical legacy replay. Pure
        # legacy items retain their original replay ordering and validation.
        if conn.execute(_EXTERNAL_HISTORY_SQL, (work_id, work_id)).fetchone() is not None:
            raise store.WorkloreConflict(_EXTERNAL_UNSUPPORTED, code="ownership-conflict")
        replay = conn.execute(
            "SELECT actor_type, actor_id, detail_json FROM work_events WHERE work_id=? AND event_id=?",
            (work_id, event_id),
        ).fetchone()
        if replay is not None:
            detail = json.loads(replay[2])
            if replay[0] != actor_type or replay[1] != actor_id or detail["action"] != action:
                raise store.WorkloreConflict(
                    "idempotency key conflicts with recorded action", code="idempotency-conflict"
                )
            # Historical target/holder authority is checked independently of fingerprint.
            _authorize(
                action,
                detail["authority"],
                actor_id=actor_id,
                is_admin=is_admin,
                is_operator=is_operator,
                generation=body.get("generation"),
                digest=digest,
            )
            if action == "accept" and not hmac.compare_digest(detail["ownership"]["holder_hash"], digest or ""):
                raise store.WorkloreForbidden("ownership holder does not match", code="holder-mismatch")
            if not hmac.compare_digest(detail["fingerprint"], fingerprint):
                raise store.WorkloreConflict(
                    "idempotency key conflicts with recorded request", code="idempotency-conflict"
                )
            return _public(detail["ownership"])
        if action == "take":
            # Legacy keys remain item-scoped. Later reuse there cannot invalidate
            # an already recorded take, but a new take cannot bind a used key.
            reused = conn.execute(
                "SELECT 1 FROM work_events WHERE event_id=? AND work_id!=? LIMIT 1", (event_id, work_id)
            ).fetchone()
            if reused is not None:
                raise store.WorkloreConflict("take key is bound to another work item", code="idempotency-conflict")
        current = _current(conn, work_id)
        if current["revision"] != expected_revision:
            raise store.WorkloreConflict("ownership revision does not match", code="version-conflict")
        _authorize(
            action,
            current,
            actor_id=actor_id,
            is_admin=is_admin,
            is_operator=is_operator,
            generation=body.get("generation"),
            digest=digest,
        )
        now = store._utc_now()
        campaign_authority = None
        if action == "take":
            campaign_authority = campaigns.take_authority(conn, work_id, item, current, node=actor_id, now=now)
        elif member is not None and action not in {"release", "withdraw", "recover"}:
            if expired(current, now):
                raise store.WorkloreConflict("campaign ownership lease expired", code="stale-generation")
            campaigns._eligible_campaign(conn, member)
            if current.get("campaign_offer_event") is None:
                raise store.WorkloreForbidden("campaign ownership has no authorization", code="campaign-not-authorized")
            if action == "checkpoint" and body["repo_identity"] != member["repo"]:
                raise store.WorkloreForbidden(
                    "checkpoint repository differs from member", code="campaign-not-authorized"
                )
        if item["status"] in TERMINAL_STATUSES and action not in {"release", "withdraw", "recover"}:
            raise store.WorkloreConflict("terminal work item refuses ownership progress", code="ownership-conflict")
        if "target_node" in body:
            _enrolled(conn, body["target_node"])
        count = conn.execute(
            f"SELECT COUNT(*) FROM work_events WHERE work_id=? AND {_EVENT_PREDICATE}", (work_id, *EVENT_TYPES.values())
        ).fetchone()[0]
        maximum = (
            OFFER_MAX_PRIOR_EVENTS
            if action in {"offer", "handoff", "take"}
            else ITEM_MAX_OWNERSHIP_TOTAL - 1
            if action in {"release", "withdraw", "recover"}
            else ITEM_MAX_OWNERSHIP_EVENTS - 1
        )
        if count > maximum:
            raise store.WorkloreConflict(
                "ownership event capacity exhausted; successor item required", code="ownership-capacity-exhausted"
            )
        if action in {"accept", "take"}:
            accepted = conn.execute(
                "SELECT detail_json FROM work_events WHERE work_id=? AND event_type IN ('ownership-accepted','ownership-taken') ORDER BY seq LIMIT ?",
                (work_id, ITEM_MAX_OWNERSHIP_TOTAL),
            ).fetchall()
            if any(
                hmac.compare_digest(json.loads(row[0])["ownership"]["holder_hash"], digest or "") for row in accepted
            ):
                raise store.WorkloreConflict(
                    "new acceptance requires a fresh holder nonce; ownership unchanged", code="ownership-conflict"
                )
        if action == "report":
            _validate_report_history(conn, work_id, body["report"], reporter_node=actor_id)
        if campaign_authority is not None:
            # A reclaim drops old path/report declarations and fences the old capability.
            result = _initial()
            result.update(campaign_authority)
            result.update(
                state="owned",
                owner_node=actor_id,
                holder_hash=digest,
                generation=current["generation"] + 1,
                revision=current["revision"] + 1,
            )
        else:
            result = _transition(
                current,
                body,
                actor_id=actor_id,
                actor_type="operator" if action == "offer" else actor_type,
                digest=digest,
            )
        result["last_seq"] = conn.execute(f"SELECT {store._EVENT_SEQ_SQL}").fetchone()[0]
        result["updated_at"] = now
        authority = {key: current[key] for key in ("generation", "owner_node", "offered_to", "holder_hash")}
        detail = {"action": action, "fingerprint": fingerprint, "authority": authority, "ownership": result}
        if action == "recover":
            detail["recovery"] = {
                "holder_quiesced": True,
                "evidence_ref": body["evidence_ref"],
                "verification": "operator-reported-unverified",
            }
        serialized = json.dumps(detail)
        if len(serialized.encode("utf-8")) > OWNERSHIP_EVENT_MAX_BYTES:
            raise _invalid("ownership event exceeds serialized byte bound; ownership unchanged")
        placeholders = ",".join(["?"] * len(store._EVENT_COLUMNS) + [store._EVENT_SEQ_SQL])
        if action == "checkpoint":
            scope.admit_checkpoint(conn, work_id, body, current_holder_digest=result["holder_hash"])
        conn.execute(
            f"INSERT INTO work_events ({','.join(store._EVENT_INSERT_COLUMNS)}) VALUES ({placeholders})",
            (
                work_id,
                event_id,
                EVENT_TYPES[action],
                None,
                None,
                actor_type,
                actor_id,
                None if is_admin else actor_id,
                None,
                serialized,
                result["updated_at"],
                result["updated_at"],
            ),
        )
        return _public(result)

    return store._in_transaction(conn, write)


def _external_event(row: Any) -> dict[str, Any]:
    """Fully decode one bounded typed external row; unknown history never resets."""
    from . import worklore_external as external

    try:
        work_id, event_type, node_id, raw, actor_type, actor_id, seq = row
        if event_type not in EXTERNAL_EVENT_TYPES.values() or node_id is not None:
            raise ValueError()
        if len(raw if isinstance(raw, bytes) else raw.encode()) > OWNERSHIP_EVENT_MAX_BYTES:
            raise ValueError()
        detail = _object(json.loads(raw), {"external_ownership_version", "action", "fingerprint", "ownership"})
        _integer(detail["external_ownership_version"], minimum=1, maximum=1)
        action = detail["action"]
        if not isinstance(action, str) or EXTERNAL_EVENT_TYPES.get(action) != event_type:
            raise ValueError()
        if not isinstance(detail["fingerprint"], str) or not scope._DIGEST_RE.fullmatch(detail["fingerprint"]):
            raise ValueError()
        snapshot = _object(
            detail["ownership"],
            {
                "state",
                "generation",
                "revision",
                "owner_node",
                "owner_principal",
                "admission",
                "binding",
                "holder_hash",
                "heartbeat",
                "expires_at",
                "last_report",
                "last_seq",
                "updated_at",
            },
        )
        admission = external.parse_admission(snapshot["admission"])
        if admission.work_id != _reference(work_id) or snapshot["generation"] != admission.generation:
            raise ValueError()
        _integer(snapshot["generation"], minimum=1)
        _integer(snapshot["revision"], minimum=1)
        if snapshot["owner_node"] is not None or snapshot["owner_principal"] != asdict(admission.principal):
            raise ValueError()
        state = {"admit": "admitted", "withdraw": "withdrawn", "release": "released", "expire": "expired"}.get(
            action, "active"
        )
        if snapshot["state"] != state:
            raise ValueError()
        expected_actor = "admin" if action in {"admit", "withdraw"} else "hub" if action == "expire" else "agent"
        if actor_type != expected_actor or actor_id != (
            admission.principal.id if expected_actor == "agent" else expected_actor
        ):
            raise ValueError()
        _integer(snapshot["last_seq"], minimum=1)
        if snapshot["last_seq"] != seq:
            raise ValueError()
        updated = datetime.fromisoformat(snapshot["updated_at"])
        if updated.tzinfo is None or updated.utcoffset() is None:
            raise ValueError()
        if state in {"admitted", "withdrawn"}:
            if any(
                snapshot[key] is not None
                for key in ("binding", "holder_hash", "heartbeat", "expires_at", "last_report")
            ):
                raise ValueError()
        else:
            binding = external.parse_registration(snapshot["binding"])
            external.validate_binding(admission, binding)
            if not isinstance(snapshot["holder_hash"], str) or not scope._DIGEST_RE.fullmatch(snapshot["holder_hash"]):
                raise ValueError()
            heartbeat = datetime.fromisoformat(snapshot["heartbeat"])
            deadline = datetime.fromisoformat(snapshot["expires_at"])
            external.LeaseSnapshot(
                admission,
                binding,
                snapshot["revision"],
                "released" if state != "active" else "active",
                heartbeat,
                deadline,
            )
            if updated < heartbeat or (state == "expired" and updated < deadline):
                raise ValueError()
            if snapshot["last_report"] is not None:
                external.parse_report(snapshot["last_report"], repo_identity=admission.repo_identity)
        return detail
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError, OverflowError, WorkloreValidationError):
        raise store.WorkloreConflict(_EXTERNAL_UNSUPPORTED, code="ownership-conflict") from None


def _external_current(conn: sqlite3.Connection, work_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT work_id,event_type,node_id,substr(CAST(detail_json AS BLOB),1,?),actor_type,actor_id,seq "
        "FROM work_events WHERE work_id=? AND " + _OWNERSHIP_PREDICATE + " ORDER BY seq DESC LIMIT 1",
        (OWNERSHIP_EVENT_MAX_BYTES + 1, work_id, *_OWNERSHIP_BINDS),
    ).fetchone()
    external_history = conn.execute(_EXTERNAL_HISTORY_SQL, (work_id, work_id)).fetchone() is not None
    if not external_history:
        return None
    if row is None:
        raise store.WorkloreConflict(_EXTERNAL_UNSUPPORTED, code="ownership-conflict")
    return _external_event(row)["ownership"]


def _external_authority(conn: sqlite3.Connection, admission: Any, observed: Any) -> None:
    """Read the supported current principal and compare private auth under the fence."""
    from . import worklore_external as external

    if observed is not None and (
        not isinstance(observed, external.AuthorityObservation) or observed.principal != admission.principal
    ):
        raise store.WorkloreForbidden("external ownership authority refused", code="principal-forbidden")
    row = conn.execute(
        "SELECT role,revoked_at,enrollment_id,authorization_enrollment_epoch,token_hash "
        "FROM fleet_message_principals WHERE principal_id=?",
        (admission.principal.id,),
    ).fetchone()
    code = None
    if row is None or row[0] != "agent":
        code = "principal-forbidden"
    elif row[1] is not None:
        code = "principal-revoked"
    elif row[2] is None or row[2] != admission.enrollment_id or row[3] != admission.authorization_enrollment_epoch:
        code = "enrollment-mismatch"
    elif observed is not None:
        if not isinstance(observed, external.AuthorityObservation):
            code = "principal-forbidden"
        else:
            try:
                current = external.AuthorityObservation(
                    observed.principal,
                    row[2],
                    row[3],
                    external.CredentialBinding(row[4]),
                    observed.authenticated_credential_generation,
                    None,
                    None,
                    "active",
                )
                code = external._authority_refusal(admission, current)
                # The server-authenticated enrollment observation must still
                # match; current DB values cannot repair stale authentication.
                code = code or external._authority_refusal(admission, observed)
            except external.ModelError:
                code = "stale-credential"
    if code:
        raise store.WorkloreForbidden("external ownership authority refused", code=code)


def _external_capacity(conn: sqlite3.Connection, work_id: str, state: str, planned: int = 1) -> None:
    count = conn.execute(
        "SELECT COUNT(*) FROM work_events WHERE work_id=? AND " + _OWNERSHIP_PREDICATE,
        (work_id, *_OWNERSHIP_BINDS),
    ).fetchone()[0]
    maximum = 198 if state == "admitted" else 199 if state == "active" else ITEM_MAX_OWNERSHIP_TOTAL
    if count + planned > maximum:
        raise store.WorkloreConflict("external ownership event capacity exhausted", code="ownership-capacity-exhausted")


def _external_public(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    public_fields = (
        "state",
        "generation",
        "revision",
        "owner_node",
        "owner_principal",
        "binding",
        "heartbeat",
        "expires_at",
        "last_report",
        "last_seq",
        "updated_at",
    )
    return {key: snapshot[key] for key in public_fields} | {
        "claim_ref": {"work_id": snapshot["admission"]["work_id"], "generation": snapshot["generation"]},
        "physical_writer_stopped": False,
        "evidence_class": "reported-unverified",
        "reservation_retained": snapshot["state"] == "active",
    }


def _external_insert(
    conn: sqlite3.Connection,
    work_id: str,
    snapshot: dict[str, Any],
    action: str,
    event_id: str,
    fingerprint: str,
    *,
    now: str,
) -> dict[str, Any]:
    snapshot = dict(snapshot) | {
        "last_seq": conn.execute(f"SELECT {store._EVENT_SEQ_SQL}").fetchone()[0],
        "updated_at": now,
    }
    actor_type = "admin" if action in {"admit", "withdraw"} else "hub" if action == "expire" else "agent"
    actor_id = snapshot["owner_principal"]["id"] if actor_type == "agent" else actor_type
    detail = {"external_ownership_version": 1, "action": action, "fingerprint": fingerprint, "ownership": snapshot}
    serialized = json.dumps(detail)
    # Validate the same complete typed envelope that future retaining scans read.
    validated = _external_event(
        (work_id, EXTERNAL_EVENT_TYPES[action], None, serialized, actor_type, actor_id, snapshot["last_seq"])
    )
    placeholders = ",".join(["?"] * len(store._EVENT_COLUMNS) + [store._EVENT_SEQ_SQL])
    conn.execute(
        f"INSERT INTO work_events ({','.join(store._EVENT_INSERT_COLUMNS)}) VALUES ({placeholders})",
        (
            work_id,
            event_id,
            EXTERNAL_EVENT_TYPES[action],
            None,
            None,
            actor_type,
            actor_id,
            None,
            None,
            serialized,
            now,
            now,
        ),
    )
    return validated["ownership"]


def _external_admission(conn: sqlite3.Connection, work_id: str, admission: Any) -> dict[str, Any]:
    current = _current(conn, work_id)
    if (
        current["state"] != "unowned"
        or conn.execute(
            "SELECT 1 FROM work_campaign_members WHERE work_id=? UNION ALL "
            "SELECT 1 FROM work_campaigns WHERE work_id=? LIMIT 1",
            (work_id, work_id),
        ).fetchone()
        is not None
    ):
        raise store.WorkloreConflict("external admission requires unowned noncampaign work", code="ownership-conflict")
    if admission.work_id != work_id or admission.generation != _integer(current["generation"]) + 1:
        raise store.WorkloreConflict("external admission generation does not match", code="stale-generation")
    _external_authority(conn, admission, None)
    return {
        "state": "admitted",
        "generation": admission.generation,
        "revision": _integer(current["revision"]) + 1,
        "owner_node": None,
        "owner_principal": asdict(admission.principal),
        "admission": asdict(admission),
        "binding": None,
        "holder_hash": None,
        "heartbeat": None,
        "expires_at": None,
        "last_report": None,
        "last_seq": None,
        "updated_at": None,
    }


def _external_replay(
    conn: sqlite3.Connection,
    work_id: str,
    event_id: str,
    fingerprint: str,
    action: str,
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT work_id,event_type,node_id,substr(CAST(detail_json AS BLOB),1,?),actor_type,actor_id,seq "
        "FROM work_events WHERE work_id=? AND event_id=?",
        (OWNERSHIP_EVENT_MAX_BYTES + 1, work_id, event_id),
    ).fetchone()
    if row is None:
        return None
    detail = _external_event(row)
    if detail["action"] != action or not hmac.compare_digest(detail["fingerprint"], fingerprint):
        raise store.WorkloreConflict("external idempotency request conflicts", code="idempotency-conflict")
    return detail["ownership"]


def _expire_external_overlaps(conn: sqlite3.Connection, work_id: str, binding: Any, now: str) -> None:
    eligible = []
    for index, row in enumerate(scope._reservation_rows(conn, work_id)):
        if index == scope.MAX_SCOPE_SNAPSHOTS:
            raise scope.ScopeAdmissionError("path-scope-capacity-exhausted")
        reservation = scope._reservation(row, repo_identity=binding.repo_identity)
        snapshot = reservation.get("external")
        if snapshot is None or snapshot["state"] != "active" or reservation["repo"].casefold() != binding.repo_identity:
            continue
        if not any(
            a == b or a.startswith(b + "/") or b.startswith(a + "/")
            for a in binding.write_scope
            for b in reservation["paths"]
        ):
            continue
        if datetime.fromisoformat(snapshot["expires_at"]) > datetime.fromisoformat(now):
            continue
        eligible.append((row[0], snapshot))
        if len(eligible) > 32:
            raise scope.ScopeAdmissionError("path-scope-capacity-exhausted")
    # Check every affected item's own exit slot before inserting any ending.
    for identifier, snapshot in eligible:
        _external_capacity(conn, identifier, "expired")
        if snapshot["revision"] == COUNTER_MAX or datetime.fromisoformat(now) < datetime.fromisoformat(
            snapshot["heartbeat"]
        ):
            raise store.WorkloreConflict("external expiry revision or clock refused", code="ownership-conflict")
    for identifier, snapshot in eligible:
        ended = dict(snapshot) | {"state": "expired", "revision": snapshot["revision"] + 1}
        _external_insert(
            conn,
            identifier,
            ended,
            "expire",
            store._mint_id("ext-own-"),
            hashlib.sha256(
                f"expire\0{identifier}\0{snapshot['generation']}\0{snapshot['revision']}".encode()
            ).hexdigest(),
            now=now,
        )


def external_ownership_action(
    conn: sqlite3.Connection,
    work_id: str,
    raw: object,
    *,
    expected_revision: int,
    idempotency_key: object,
    authority: Any = None,
    operator_authorized: bool = False,
    holder_nonce: object = None,
) -> dict[str, Any]:
    """Dormant transaction entry. Only tests call it; no route authenticates it.

    Authority observations and the explicit operator flag belong to a future
    trusted server caller, never request-body fields. No live records are allowed
    before the separately held safe-read and authenticated-route prerequisites.
    """
    from . import worklore_external as external

    expected_revision = _integer(expected_revision)
    key = _text(idempotency_key, "idempotency_key", IDEMPOTENCY_KEY_MAX)
    if isinstance(holder_nonce, str):
        nonce_hash(holder_nonce)
        _reject_nonce_echo(raw, key, holder_nonce)
    try:
        if not isinstance(raw, Mapping):
            raise _invalid()
        action = raw.get("action")
        fields = {
            "admit": {"action", "admission"},
            "withdraw": {"action", "generation"},
            "register": {"action", "generation", "registration", "lease_seconds"},
            "renew": {"action", "generation", "lease_seconds"},
            "release": {"action", "generation"},
            "report": {"action", "generation", "report"},
        }
        if not isinstance(action, str) or action not in fields:
            raise _invalid()
        body = _object(raw, fields[action])
        parsed_admission = external.parse_admission(body["admission"]) if action == "admit" else None
        parsed_binding = external.parse_registration(body["registration"]) if action == "register" else None
        if "generation" in body:
            _integer(body["generation"], minimum=1)
        if "lease_seconds" in body:
            _integer(body["lease_seconds"], minimum=60, maximum=3600)
    except external.ModelError as error:
        raise _invalid("external ownership input refused", code=error.code) from None
    operator = action in {"admit", "withdraw"}
    if operator:
        if operator_authorized is not True or authority is not None:
            raise store.WorkloreForbidden("explicit external operator authority required", code="forbidden")
        digest = None
    else:
        if not isinstance(authority, external.AuthorityObservation) or operator_authorized is not False:
            raise store.WorkloreForbidden("authenticated agent context required", code="principal-forbidden")
        digest = nonce_hash(holder_nonce)

    def write() -> dict[str, Any]:
        item = store.get_item(conn, work_id)
        current = _external_current(conn, work_id)
        admission = (
            parsed_admission
            if action == "admit"
            else external.parse_admission(current["admission"])
            if current
            else None
        )
        if admission is None:
            raise store.WorkloreConflict("external admission is required", code="ownership-conflict")
        if action == "admit":
            _external_authority(conn, admission, None)
        elif action != "withdraw":
            _external_authority(conn, admission, authority)
            if action not in admission.actions:
                raise store.WorkloreForbidden("external action is not admitted", code="action-forbidden")
        if action != "admit":
            assert current is not None  # Established by the admission check above.
            if body["generation"] != current["generation"]:
                raise store.WorkloreConflict("external generation does not match", code="stale-generation")
            if (
                action != "register"
                and not operator
                and not hmac.compare_digest(current["holder_hash"] or "", digest or "")
            ):
                raise store.WorkloreForbidden("external holder does not match", code="holder-mismatch")
        principal = {"kind": "admin", "id": "admin"} if operator else asdict(authority.principal)
        event_id = (
            "ext-own-"
            + hashlib.sha256(
                json.dumps(
                    [principal, admission.enrollment_id, work_id, key], sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest()[:32]
        )
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "principal": principal,
                    "enrollment_id": admission.enrollment_id,
                    "work_id": work_id,
                    "request_key": key,
                    "admission": asdict(admission),
                    "binding": asdict(parsed_binding)
                    if parsed_binding is not None
                    else current["binding"]
                    if current is not None and not operator
                    else None,
                    "body": body,
                    "expected_revision": expected_revision,
                    "holder_hash": digest,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        historical = _external_replay(conn, work_id, event_id, fingerprint, action)
        if historical is not None:
            if historical["generation"] != admission.generation or (
                current is not None and historical["generation"] != current["generation"]
            ):
                raise store.WorkloreConflict("external replay generation differs", code="stale-generation")
            if action == "register" and not hmac.compare_digest(historical["holder_hash"] or "", digest or ""):
                raise store.WorkloreForbidden("external holder does not match", code="holder-mismatch")
            return _external_public(historical)
        now = store._utc_now()
        if item["status"] in TERMINAL_STATUSES and action not in {"release", "withdraw"}:
            raise store.WorkloreConflict("terminal work item refuses external progress", code="ownership-conflict")
        if action == "admit":
            if current is not None:
                raise store.WorkloreConflict("external work item cannot be reused", code="ownership-conflict")
            legacy = _current(conn, work_id)
            if legacy["revision"] != expected_revision:
                raise store.WorkloreConflict("ownership revision does not match", code="version-conflict")
            result = _external_admission(conn, work_id, admission)
        else:
            assert current is not None
            if current["revision"] != expected_revision:
                raise store.WorkloreConflict("ownership revision does not match", code="version-conflict")
            if current["revision"] == COUNTER_MAX:
                raise store.WorkloreConflict("external revision exhausted", code="ownership-conflict")
            expected_state = "admitted" if action in {"register", "withdraw"} else "active"
            if current["state"] != expected_state:
                raise store.WorkloreConflict("external state does not permit action", code="ownership-conflict")
            result = dict(current) | {"revision": current["revision"] + 1}
            if action in {"register", "renew"}:
                if body["lease_seconds"] > admission.max_lease_seconds:
                    raise store.WorkloreConflict("external duration exceeds admission", code="ownership-conflict")
                try:
                    result["expires_at"] = (
                        datetime.fromisoformat(now) + timedelta(seconds=body["lease_seconds"])
                    ).isoformat()
                except OverflowError:
                    raise _invalid("external deadline out of bounds") from None
            if action == "register":
                assert parsed_binding is not None
                external.validate_binding(admission, parsed_binding)
                prior = conn.execute(
                    "SELECT substr(CAST(detail_json AS BLOB),1,?) FROM work_events WHERE work_id=? "
                    "AND event_type IN ('ownership-accepted','ownership-taken','external-ownership-registered') LIMIT ?",
                    (OWNERSHIP_EVENT_MAX_BYTES + 1, work_id, ITEM_MAX_OWNERSHIP_TOTAL),
                )
                for row in prior:
                    try:
                        old = json.loads(row[0])["ownership"]["holder_hash"]
                        if not isinstance(old, str) or not scope._DIGEST_RE.fullmatch(old):
                            raise ValueError()
                    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
                        raise store.WorkloreConflict(
                            "prior holder history unavailable", code="ownership-conflict"
                        ) from None
                    if hmac.compare_digest(old, digest or ""):
                        raise store.WorkloreConflict(
                            "external registration needs fresh holder nonce", code="ownership-conflict"
                        )
                result.update(state="active", binding=asdict(parsed_binding), holder_hash=digest, heartbeat=now)
            elif action == "withdraw":
                result["state"] = "withdrawn"
            else:
                clock = datetime.fromisoformat(now)
                if clock < datetime.fromisoformat(current["heartbeat"]) or clock >= datetime.fromisoformat(
                    current["expires_at"]
                ):
                    raise store.WorkloreConflict("external lease clock refused", code="ownership-conflict")
                if action == "renew":
                    result["heartbeat"] = now
                elif action == "release":
                    result["state"] = "released"
                else:
                    result["last_report"] = external.parse_report(body["report"], repo_identity=admission.repo_identity)
        _external_capacity(conn, work_id, result["state"])
        if action == "register":
            assert parsed_binding is not None and digest is not None
            _expire_external_overlaps(conn, work_id, parsed_binding, now)
            scope.admit_checkpoint(
                conn,
                work_id,
                {"repo_identity": parsed_binding.repo_identity, "write_scope": list(parsed_binding.write_scope)},
                current_holder_digest=digest,
            )
        result = _external_insert(conn, work_id, result, action, event_id, fingerprint, now=now)
        return _external_public(result)

    try:
        return store._in_transaction(conn, write)
    except external.ModelError as error:
        raise _invalid("external ownership input refused", code=error.code) from None
