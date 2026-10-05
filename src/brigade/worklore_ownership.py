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
from typing import Any, Mapping

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
    "checkpoint": "ownership-checkpoint",
    "handoff": "ownership-handoff-offered",
    "release": "ownership-released",
}
_FIELDS = {
    "offer": {"action", "target_node", "exclusions", "authorization_ref", "attempt_budget"},
    "withdraw": {"action"},
    "accept": {"action", "generation"},
    "checkpoint": {
        "action",
        "generation",
        "repo_identity",
        "write_scope",
        "source_revision",
        "next_action",
        "evidence_refs",
    },
    "handoff": {"action", "generation", "target_node"},
    "release": {"action", "generation"},
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
        "last_seq": None,
        "updated_at": None,
        "liveness": "unknown",
        "conflict_check": "not-performed",
    }


def _current(conn: sqlite3.Connection, work_id: str) -> dict[str, Any]:
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
    if action in {"offer", "withdraw"}:
        if not (is_admin or is_operator):
            raise store.WorkloreForbidden("operator token required", code="forbidden")
        return
    if is_admin:
        raise store.WorkloreForbidden("node holder authority required", code="holder-mismatch")
    if generation != authority["generation"]:
        raise store.WorkloreConflict("ownership generation does not match", code="stale-generation")
    expected_node = authority["offered_to"] if action == "accept" else authority["owner_node"]
    if actor_id != expected_node:
        raise store.WorkloreForbidden("ownership holder does not match", code="holder-mismatch")
    if action != "accept" and not hmac.compare_digest(str(authority["holder_hash"] or ""), str(digest or "")):
        raise store.WorkloreForbidden("ownership holder does not match", code="holder-mismatch")


def _transition(
    snapshot: dict[str, Any], body: Mapping[str, Any], *, actor_id: str, actor_type: str, digest: str | None
) -> dict[str, Any]:
    action = body["action"]
    allowed = {
        "offer": {"unowned"},
        "withdraw": {"offered"},
        "accept": {"offered", "handoff-pending"},
        "checkpoint": {"owned"},
        "handoff": {"owned"},
        "release": {"owned", "handoff-pending"},
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
    elif action == "checkpoint":
        result.update({key: value for key, value in body.items() if key not in {"action", "generation"}})
    else:
        result = _initial()
        result["generation"] = snapshot["generation"]
    result["revision"] = snapshot["revision"] + 1
    return result


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
    digest = (supplied_digest or nonce_hash(holder_nonce)) if action not in {"offer", "withdraw"} else None
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
        item = store.get_item(conn, work_id)
        if not is_admin:
            _enrolled(conn, actor_id)
        if action in {"offer", "withdraw"} and not (is_admin or is_operator):
            raise store.WorkloreForbidden("operator token required", code="forbidden")
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
        if item["status"] in TERMINAL_STATUSES and action not in {"release", "withdraw"}:
            raise store.WorkloreConflict("terminal work item refuses ownership progress", code="ownership-conflict")
        if "target_node" in body:
            _enrolled(conn, body["target_node"])
        count = conn.execute(
            f"SELECT COUNT(*) FROM work_events WHERE work_id=? AND {_EVENT_PREDICATE}", (work_id, *EVENT_TYPES.values())
        ).fetchone()[0]
        maximum = (
            OFFER_MAX_PRIOR_EVENTS
            if action in {"offer", "handoff"}
            else ITEM_MAX_OWNERSHIP_TOTAL - 1
            if action in {"release", "withdraw"}
            else ITEM_MAX_OWNERSHIP_EVENTS - 1
        )
        if count > maximum:
            raise store.WorkloreConflict(
                "ownership event capacity exhausted; successor item required", code="ownership-capacity-exhausted"
            )
        if action == "accept":
            accepted = conn.execute(
                "SELECT detail_json FROM work_events WHERE work_id=? AND event_type='ownership-accepted' ORDER BY seq LIMIT ?",
                (work_id, ITEM_MAX_OWNERSHIP_TOTAL),
            ).fetchall()
            if any(
                hmac.compare_digest(json.loads(row[0])["ownership"]["holder_hash"], digest or "") for row in accepted
            ):
                raise store.WorkloreConflict(
                    "new acceptance requires a fresh holder nonce; ownership unchanged", code="ownership-conflict"
                )
        result = _transition(
            current, body, actor_id=actor_id, actor_type="operator" if action == "offer" else actor_type, digest=digest
        )
        result["last_seq"] = conn.execute(f"SELECT {store._EVENT_SEQ_SQL}").fetchone()[0]
        result["updated_at"] = store._utc_now()
        authority = {key: current[key] for key in ("generation", "owner_node", "offered_to", "holder_hash")}
        detail = {"action": action, "fingerprint": fingerprint, "authority": authority, "ownership": result}
        serialized = json.dumps(detail)
        if len(serialized.encode("utf-8")) > OWNERSHIP_EVENT_MAX_BYTES:
            raise _invalid("ownership event exceeds serialized byte bound; ownership unchanged")
        placeholders = ",".join(["?"] * len(store._EVENT_COLUMNS) + [store._EVENT_SEQ_SQL])
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
