"""Content-bound local origin stamps, independent of agent-authored Markdown.

The signing process and receipt key are trusted. This is a MAC boundary against
text-only forgery, not an isolation boundary against an agent with shell access.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from . import handoff_publish, receipt_signing
from .bounded_read import read_regular_file

SCHEMA = "brigade.handoff-origin.v1"
SESSION_KINDS = frozenset({"interactive", "worker", "cron", "subagent", "unknown"})
_LABEL = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}\Z")


@dataclass(frozen=True)
class Origin:
    session_kind: str
    untrusted_tools: bool | None
    harness: str
    host: str

    @property
    def allows_automatic_filing(self) -> bool:
        return self.session_kind == "interactive" and self.untrusted_tools is False


def stamp_path(target: Path, handoff: Path) -> Path:
    """Tooling state outside the writer inbox, keyed by the full handoff path."""
    identity = hashlib.sha256(str(handoff.expanduser().resolve()).encode("utf-8")).hexdigest()
    return target.expanduser().resolve() / ".brigade" / "handoffs" / "origins" / f"{identity}.json"


def _key(target: Path) -> bytes | None:
    # Reuse the receipt key location and format, but bound reads even if a key
    # path is replaced by a FIFO or an oversized file. Never create a key here.
    data = read_regular_file(receipt_signing.key_path(target), max_bytes=256, require_stable=True).data
    if data is None:
        return None
    try:
        key = bytes.fromhex(data.decode("ascii").strip())
    except (ValueError, UnicodeError):
        return None
    return key if len(key) == 32 else None


def _valid(origin: Origin) -> bool:
    return (
        isinstance(origin.session_kind, str)
        and origin.session_kind in SESSION_KINDS
        and (origin.untrusted_tools is None or type(origin.untrusted_tools) is bool)
        and all(isinstance(label, str) and _LABEL.fullmatch(label) for label in (origin.harness, origin.host))
    )


def _publication(handoff: Path) -> list[int]:
    info = handoff.stat()
    return [info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size]


def _mac(payload: dict, key: bytes) -> str:
    # Domain separation prevents a receipt MAC from being reused as an origin.
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hmac.new(key, (SCHEMA + "\n" + encoded).encode("ascii"), hashlib.sha256).hexdigest()


def _stamp(target: Path, handoff: Path, snapshot: bytes, claims: dict) -> bool:
    key = _key(target)
    if key is None:
        return False
    payload = {
        "schema": SCHEMA,
        "handoff_name": handoff.name,
        "path_digest": stamp_path(target, handoff).stem,
        "publication": _publication(handoff),
        "sha256": hashlib.sha256(snapshot).hexdigest(),
        **claims,
        "key_id": receipt_signing.key_id(key),
    }
    sidecar = stamp_path(target, handoff)
    sidecar.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    handoff_publish.publish_handoff(
        sidecar.parent, sidecar.name, json.dumps({**payload, "mac": _mac(payload, key)}) + "\n", replace=True
    )
    return True


def _verify(target: Path, handoff: Path, snapshot: bytes, *, bind_publication: bool = True) -> dict | None:
    try:
        key = _key(target)
        if key is None:
            return None
        raw = read_regular_file(stamp_path(target, handoff), max_bytes=16384, require_stable=True).data
        if raw is None:
            return None
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return None
        mac = payload.pop("mac", None)
        if not isinstance(mac, str) or not re.fullmatch(r"[0-9a-f]{64}", mac):
            return None
        if not hmac.compare_digest(mac, _mac(payload, key)):
            return None
        if (
            payload.get("schema") != SCHEMA
            or payload.get("handoff_name") != handoff.name
            or payload.get("path_digest") != stamp_path(target, handoff).stem
            or (bind_publication and payload.get("publication") != _publication(handoff))
            or payload.get("sha256") != hashlib.sha256(snapshot).hexdigest()
            or payload.get("key_id") != receipt_signing.key_id(key)
        ):
            return None
        return payload
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return None


def stamp_handoff(target: Path, handoff: Path, snapshot: bytes, origin: Origin) -> bool:
    """For trusted tooling only. Publish first and never create a signing key."""
    if not _valid(origin):
        raise ValueError("invalid handoff origin")
    return _stamp(target, handoff, snapshot, {"origin": asdict(origin)})


def verify_handoff(target: Path, handoff: Path, snapshot: bytes) -> Origin | None:
    """Verify exactly the bytes ingest parsed. Every unverifiable case is unknown."""
    payload = _verify(target, handoff, snapshot)
    try:
        origin = Origin(**payload["origin"]) if payload else None
        return origin if origin is not None and _valid(origin) else None
    except (ValueError, TypeError, KeyError):
        return None


@dataclass(frozen=True)
class ReviewDraft:
    code: str
    promote_cards: bool
    route_documents: bool


def _valid_review(review: ReviewDraft) -> bool:
    return (
        isinstance(review.code, str)
        and re.fullmatch(r"[a-z0-9-]{1,128}", review.code) is not None
        and type(review.promote_cards) is bool
        and type(review.route_documents) is bool
    )


def _review_location(target: Path, draft: Path) -> bool:
    # Writer inboxes, including malformed/, can never supply retry authority.
    return draft.absolute().parent == target.resolve() / "memory" / "handoff-inbox"


def stamp_review_draft(target: Path, draft: Path, snapshot: bytes, review: ReviewDraft) -> bool:
    """Ingest's routing decision and flags, bound to this publication's bytes."""
    if not _valid_review(review) or not _review_location(target, draft):
        raise ValueError("invalid review draft origin")
    return _stamp(target, draft, snapshot, {"review": asdict(review)})


def verify_review_draft(target: Path, draft: Path, snapshot: bytes) -> ReviewDraft | None:
    if not _review_location(target, draft):
        return None
    # Journal recovery may republish an identical draft at the same path. Its
    # authenticated bytes and flags remain authoritative across that restoration.
    # Writer handoffs still require the original publication identity.
    payload = _verify(target, draft, snapshot, bind_publication=False)
    try:
        review = ReviewDraft(**payload["review"]) if payload else None
        return review if review is not None and _valid_review(review) else None
    except (ValueError, TypeError, KeyError):
        return None


def remove_stamp(target: Path, handoff: Path) -> None:
    """Discard authority only after the handoff is archived or resolved."""
    stamp_path(target, handoff).unlink(missing_ok=True)


def draft_origin() -> Origin:
    """Until a harness observer supplies provenance, draft trust is unknown."""
    import os
    import socket

    from .agent_identity import detect_harness

    harness = detect_harness()[0]
    host = socket.gethostname()
    return Origin(
        "worker" if os.environ.get("BRIGADE_RUN_ID") else "unknown",
        None,
        harness if _LABEL.fullmatch(harness) else "unknown",
        host if _LABEL.fullmatch(host) else "unknown",
    )
