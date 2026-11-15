"""Content-bound local origin stamps, independent of agent-authored Markdown.

The signing process and receipt key are trusted. This is a MAC boundary against
text-only forgery, not an isolation boundary against an agent with shell access.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import stat
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from . import dirfd, handoff_publish, receipt_signing

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


def read_handoff(path: Path, *, max_bytes: int, private_key: bool = False) -> bytes:
    """Read one stable, single-link regular file through a no-follow descriptor."""
    parent = dirfd.open_directory_chain(path.absolute().parent)
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = dirfd.open_child_file(parent, path.name, flags, share_write=False)
        with os.fdopen(fd, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > max_bytes:
                raise OSError("not a bounded single-link regular file")
            if private_key and (before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) & 0o077):
                raise OSError("origin key is not private to the current user")
            data = handle.read(max_bytes + 1)
            after = os.fstat(handle.fileno())
            now = dirfd.stat_child(parent, path.name)
            if (
                any(
                    getattr(before, field) != getattr(after, field)
                    for field in (
                        "st_dev",
                        "st_ino",
                        "st_mode",
                        "st_nlink",
                        "st_uid",
                        "st_gid",
                        "st_size",
                        "st_mtime_ns",
                        "st_ctime_ns",
                    )
                )
                or (now.st_dev, now.st_ino) != (before.st_dev, before.st_ino)
                or not stat.S_ISREG(now.st_mode)
                or len(data) != before.st_size
                or len(data) > max_bytes
            ):
                raise OSError("file changed during origin read")
            return data
    finally:
        os.close(parent)


def _key(target: Path) -> bytes | None:
    # Origin authority never honors the receipt-only environment override.
    # Windows cannot prove POSIX ownership and private mode, so fail closed.
    if not dirfd.posix_available() or not hasattr(os, "getuid"):
        return None
    try:
        path = target.expanduser().resolve() / ".brigade" / receipt_signing.KEY_NAME
        data = read_handoff(path, max_bytes=256, private_key=True)
        key = bytes.fromhex(data.decode("ascii").strip())
    except (OSError, ValueError, UnicodeError):
        return None
    return key if len(key) == 32 else None


@dataclass(frozen=True)
class VerifiedStamp:
    sidecar: Path
    consumed: Path

    def claim(self) -> bool:
        return _claim(self.consumed)


def _claim(consumed: Path, *, decision_id: str = "") -> bool:
    """Consume before canonical writes. Only the same journal decision can resume."""
    try:
        # origins/ already exists. Flush its sibling's creation as well as the
        # marker, so a power loss cannot lose a successful first claim.
        state = dirfd.open_directory_chain(consumed.parent.parent)
        try:
            try:
                dirfd.mkdir_child(state, consumed.parent.name)
            except FileExistsError:
                pass
            parent = dirfd.open_child_directory(state, consumed.parent.name)
            try:
                created = True
                try:
                    fd = dirfd.open_child_file(parent, consumed.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                except FileExistsError:
                    if not decision_id:
                        return False
                    created = False
                    fd = dirfd.open_child_file(parent, consumed.name, os.O_RDONLY)
                try:
                    data = decision_id.encode("ascii")
                    if created:
                        if os.write(fd, data) != len(data):
                            raise OSError("short origin claim write")
                    else:
                        info = os.fstat(fd)
                        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or os.read(fd, 257) != data:
                            return False
                    # Re-flush an existing claim too. The earlier attempt may
                    # have failed its durability check before canonical writes.
                    os.fsync(fd)
                finally:
                    os.close(fd)
                os.fsync(parent)
                os.fsync(state)
            finally:
                os.close(parent)
        finally:
            os.close(state)
        return True
    except (OSError, ValueError):
        return False


def claim_review_stamp(target: Path, mac: str, decision_id: str) -> bool:
    """Journal recovery can resume its own claim, never another consumption."""
    if not isinstance(mac, str) or re.fullmatch(r"[0-9a-f]{64}", mac) is None:
        return False
    consumed = target.resolve() / ".brigade" / "handoffs" / "consumed-stamps" / mac
    return _claim(consumed, decision_id=decision_id)


@dataclass(frozen=True)
class VerifiedOrigin(Origin):
    stamp: VerifiedStamp


def _valid(origin: Origin) -> bool:
    return (
        isinstance(origin.session_kind, str)
        and origin.session_kind in SESSION_KINDS
        and (origin.untrusted_tools is None or type(origin.untrusted_tools) is bool)
        and all(isinstance(label, str) and _LABEL.fullmatch(label) for label in (origin.harness, origin.host))
    )


def _publication(handoff: Path) -> list[int]:
    info = handoff.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise OSError("linked handoff publication")
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
        "nonce": secrets.token_hex(16),
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


def _verify(
    target: Path, handoff: Path, snapshot: bytes, *, bind_publication: bool = True
) -> tuple[dict, VerifiedStamp] | None:
    try:
        key = _key(target)
        if key is None:
            return None
        sidecar = stamp_path(target, handoff)
        raw = read_handoff(sidecar, max_bytes=16384)
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
            or payload.get("path_digest") != sidecar.stem
            or (bind_publication and payload.get("publication") != _publication(handoff))
            or payload.get("sha256") != hashlib.sha256(snapshot).hexdigest()
            or payload.get("key_id") != receipt_signing.key_id(key)
        ):
            return None
        # The MAC identifies the authenticated record, independent of JSON layout.
        consumed = sidecar.parent.parent / "consumed-stamps" / mac
        try:
            consumed.lstat()
        except FileNotFoundError:
            return payload, VerifiedStamp(sidecar, consumed)
        return None
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return None


def stamp_handoff(target: Path, handoff: Path, snapshot: bytes, origin: Origin) -> bool:
    """For trusted tooling only. Publish first and never create a signing key."""
    if not _valid(origin):
        raise ValueError("invalid handoff origin")
    return _stamp(target, handoff, snapshot, {"origin": asdict(origin)})


def verify_handoff(target: Path, handoff: Path, snapshot: bytes) -> VerifiedOrigin | None:
    """Verify exactly the bytes ingest parsed. Every unverifiable case is unknown."""
    verified = _verify(target, handoff, snapshot)
    try:
        if verified is None:
            return None
        payload, stamp = verified
        origin = Origin(**payload["origin"])
        return VerifiedOrigin(**asdict(origin), stamp=stamp) if _valid(origin) else None
    except (ValueError, TypeError, KeyError):
        return None


@dataclass(frozen=True)
class ReviewDraft:
    code: str
    promote_cards: bool
    route_documents: bool


@dataclass(frozen=True)
class VerifiedReviewDraft(ReviewDraft):
    stamp: VerifiedStamp


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


def verify_review_draft(target: Path, draft: Path, snapshot: bytes) -> VerifiedReviewDraft | None:
    if not _review_location(target, draft):
        return None
    # Journal recovery may republish an identical draft at the same path. Its
    # authenticated bytes and flags remain authoritative until the stamp is claimed.
    # Writer handoffs still require the original publication identity.
    verified = _verify(target, draft, snapshot, bind_publication=False)
    try:
        if verified is None:
            return None
        payload, stamp = verified
        review = ReviewDraft(**payload["review"])
        return VerifiedReviewDraft(**asdict(review), stamp=stamp) if _valid_review(review) else None
    except (ValueError, TypeError, KeyError):
        return None


def prune_missing_review_stamps(target: Path) -> int:
    """Revoke review authority when a drain observes a manually removed draft."""
    target = target.resolve()
    root = target / ".brigade/handoffs/origins"
    removed = 0
    for sidecar in root.glob("*.json"):
        try:
            payload = json.loads(read_handoff(sidecar, max_bytes=16384))
            if not isinstance(payload, dict) or not isinstance(payload.get("review"), dict):
                continue
            name = payload.get("handoff_name")
            if not isinstance(name, str) or Path(name).name != name:
                continue
            draft = target / "memory/handoff-inbox" / name
            if stamp_path(target, draft) != sidecar:
                continue
            try:
                draft.lstat()
            except FileNotFoundError:
                remove_stamp(sidecar)
                removed += 1
        except (OSError, ValueError, TypeError, RecursionError):
            continue
    return removed


def remove_stamp(sidecar: Path) -> None:
    """Remove the exact path captured before archive, never resolve the handoff again."""
    sidecar.unlink(missing_ok=True)


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
