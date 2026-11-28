"""Content-bound local origin stamps, independent of agent-authored Markdown.

The signing process and receipt key are trusted. This is a MAC boundary against
text-only forgery, not an isolation boundary against an agent with shell access.
"""

from __future__ import annotations

import hashlib
from itertools import islice
import hmac
import json
import os
import secrets
import stat
import sys
import re
from contextlib import ExitStack
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


class OriginClaimRefused(OSError):
    """Automatic authority could not be consumed before a canonical write."""


class LinkedHandoffError(OSError):
    """A handoff cannot be trusted through a linked publication."""


class HandoffTooLargeError(OSError):
    """A regular handoff needs review outside the automatic inspection limit."""

    def __init__(self, size: int):
        super().__init__("handoff exceeds the automatic inspection limit")
        self.size = size


def read_handoff(path: Path, *, max_bytes: int, private_key: bool = False) -> bytes:
    """Read one stable, single-link regular file through a no-follow descriptor."""
    parent = dirfd.open_directory_chain(path.absolute().parent)
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
        info = dirfd.stat_child(parent, path.name, allow_reparse=True)
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise LinkedHandoffError("linked handoff")
        fd = dirfd.open_child_file(parent, path.name, flags, share_write=False)
        with os.fdopen(fd, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise OSError("handoff is not a regular file")
            if before.st_nlink != 1 or getattr(before, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise LinkedHandoffError("linked handoff")
            if before.st_size > max_bytes:
                raise HandoffTooLargeError(before.st_size)
            if (
                private_key
                and sys.platform != "win32"
                and (not hasattr(os, "getuid") or before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) & 0o077)
            ):
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
    if sys.platform == "win32":
        return receipt_signing.workspace_key(target)
    try:
        path = target.expanduser().resolve() / ".brigade" / receipt_signing.KEY_NAME
        data = read_handoff(path, max_bytes=256, private_key=True)
        if not dirfd.posix_available() or not hasattr(os, "getuid"):
            return None
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


def _temporary_name(marker: str) -> str:
    return f".claim-{marker}.tmp"


def _temporary_directory(parent: int) -> int:
    # A separate namespace keeps permanent markers out of the maintenance budget.
    try:
        dirfd.mkdir_child(parent, "temporaries")
        dirfd.fsync_directory(parent)
    except FileExistsError:
        pass
    return dirfd.open_child_directory(parent, "temporaries")


def _sweep_claim_temporaries(parent: int) -> None:
    temporary_parent = _temporary_directory(parent)
    try:
        scan = os.open(".", dirfd.directory_flags(), dir_fd=temporary_parent)
        try:
            with os.scandir(scan) as entries:
                for entry in islice(entries, 64):
                    name = entry.name
                    if not name.startswith(".claim-") or not name.endswith(".tmp"):
                        continue
                    try:
                        info = dirfd.stat_child(temporary_parent, name)
                        if not stat.S_ISREG(info.st_mode):
                            continue
                        if info.st_nlink == 2:
                            marker = dirfd.stat_child(parent, name[7:-4])
                            if (marker.st_dev, marker.st_ino) != (info.st_dev, info.st_ino):
                                continue
                        elif info.st_nlink != 1:
                            continue
                        # Under the claim lock, every valid temporary is crash debris.
                        # Removing each visited entry makes successive batches progress.
                        dirfd.unlink_child(temporary_parent, name)
                    except FileNotFoundError:
                        pass
        finally:
            os.close(scan)
        dirfd.fsync_directory(temporary_parent)
    finally:
        os.close(temporary_parent)


def _resume_claim(parent: int, name: str, data: bytes) -> bool:
    fd = dirfd.open_child_file(parent, name, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink not in (1, 2) or os.read(fd, 257) != data:
            return False
        if info.st_nlink == 2:
            temporary_parent = _temporary_directory(parent)
            try:
                temporary = _temporary_name(name)
                sibling = dirfd.stat_child(temporary_parent, temporary)
                if (sibling.st_dev, sibling.st_ino) != (info.st_dev, info.st_ino):
                    return False
                dirfd.unlink_child(temporary_parent, temporary)
                dirfd.fsync_directory(temporary_parent)
            finally:
                os.close(temporary_parent)
        if os.fstat(fd).st_nlink != 1:
            return False
        os.fsync(fd)
        return True
    finally:
        os.close(fd)


def _sweep_windows_claim_temporaries(parent: int) -> None:
    from .work_cmd import nt_dirfd

    # Filter on the held directory so permanent markers do not spend the sweep budget.
    for name in nt_dirfd.child_names(parent, 64, pattern=".claim-*.tmp"):
        if re.fullmatch(r"\.claim-[a-f0-9]{32}\.tmp", name) is None:
            continue
        try:
            info = dirfd.stat_child(parent, name)
            if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                dirfd.unlink_child(parent, name)
        except (FileNotFoundError, nt_dirfd.ReparsePointRefusedError):
            pass
    dirfd.fsync_directory(parent)


def _publish_windows_claim(parent: int, name: str, data: bytes) -> bool:
    temporary = _temporary_name(secrets.token_hex(16))
    fd = dirfd.open_child_file(parent, temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            dirfd.replace_children(parent, temporary, name, replace=False)
        except FileExistsError:
            return False
        return True
    finally:
        try:
            dirfd.unlink_child(parent, temporary)
        except FileNotFoundError:
            pass


def _windows_claim(consumed: Path, decision_id: str, *, create: bool) -> bool:
    """Serialize cleanup and publish a complete marker with a no-replace rename."""
    import msvcrt

    try:
        data = decision_id.encode("ascii")
        with ExitStack() as held:
            state = dirfd.open_directory_chain(consumed.parent.parent, writable=create)
            held.callback(os.close, state)
            if create:
                try:
                    dirfd.mkdir_child(state, consumed.parent.name)
                except FileExistsError:
                    pass
            parent = dirfd.open_child_directory(state, consumed.parent.name)
            held.callback(os.close, parent)
            lock = dirfd.open_child_lock_file(parent, ".claims.lock")
            held.callback(os.close, lock)
            # Closing the descriptor releases the byte-range lock on every exit.
            msvcrt.locking(lock, msvcrt.LK_LOCK, 1)  # type: ignore[attr-defined]
            _sweep_windows_claim_temporaries(parent)
            if create and _publish_windows_claim(parent, consumed.name, data):
                dirfd.fsync_directory(parent)
                dirfd.fsync_directory(state)
                return True
            if not decision_id:
                return False
            fd = dirfd.open_child_file(parent, consumed.name, os.O_RDONLY, share_write=False)
            held.callback(os.close, fd)
            info = os.fstat(fd)
            return stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and os.read(fd, 257) == data
    except (OSError, ValueError):
        return False


def owns_review_claim(target: Path, mac: str, decision_id: str) -> bool:
    """Recovery must already own a durable marker, never mint one from journal metadata."""
    if not isinstance(mac, str) or re.fullmatch(r"[0-9a-f]{64}", mac) is None or not decision_id:
        return False
    if sys.platform == "win32":
        consumed = target.resolve() / ".brigade/handoffs/consumed-stamps" / mac
        return _windows_claim(consumed, decision_id, create=False)
    if not dirfd.posix_available():
        return False
    import fcntl

    try:
        parent = dirfd.open_directory_chain(target.resolve() / ".brigade/handoffs/consumed-stamps")
        try:
            fcntl.flock(parent, fcntl.LOCK_EX)
            _sweep_claim_temporaries(parent)
            owned = _resume_claim(parent, mac, decision_id.encode("ascii"))
            os.fsync(parent)
            return owned
        finally:
            os.close(parent)
    except (OSError, ValueError):
        return False


def _claim(consumed: Path, *, decision_id: str = "") -> bool:
    """Consume before canonical writes. Only the same journal decision can resume."""
    if sys.platform == "win32":
        return _windows_claim(consumed, decision_id, create=True)
    if not dirfd.posix_available():
        return False
    import fcntl

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
                # Keep the orphan batch bounded while serializing it with publication,
                # so a live temporary cannot be mistaken for crash debris.
                fcntl.flock(parent, fcntl.LOCK_EX)
                _sweep_claim_temporaries(parent)
                data = decision_id.encode("ascii")
                try:
                    dirfd.stat_child(parent, consumed.name)
                except FileNotFoundError:
                    pass
                else:
                    owned = bool(decision_id) and _resume_claim(parent, consumed.name, data)
                    os.fsync(parent)
                    os.fsync(state)
                    return owned
                temporary_parent = _temporary_directory(parent)
                temporary = _temporary_name(consumed.name)
                try:
                    fd = dirfd.open_child_file(temporary_parent, temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                except BaseException:
                    os.close(temporary_parent)
                    raise
                try:
                    try:
                        data = decision_id.encode("ascii")
                        if os.write(fd, data) != len(data):
                            raise OSError("short origin claim write")
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                    try:
                        os.link(
                            temporary,
                            consumed.name,
                            src_dir_fd=temporary_parent,
                            dst_dir_fd=parent,
                            follow_symlinks=False,
                        )
                    except FileExistsError:
                        if not decision_id:
                            return False
                        if not _resume_claim(parent, consumed.name, data):
                            return False
                finally:
                    try:
                        dirfd.unlink_child(temporary_parent, temporary)
                        os.fsync(temporary_parent)
                    finally:
                        os.close(temporary_parent)
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
class UnstampedStamp(VerifiedStamp):
    """A draft written without a stamp, claimed under the same lock as a stamp."""

    def claim(self) -> bool:
        # Origins/ may be absent: no stamp was ever published for this draft.
        try:
            state = dirfd.open_directory_chain(self.consumed.parent.parent, create=True)
        except (OSError, ValueError):
            return False
        os.close(state)
        if not _claim(self.consumed):
            return False
        # A writer may publish its stamp between the check and this claim. That
        # stamp is not ours to spend, so the draft keeps its review instead.
        try:
            self.sidecar.lstat()
        except FileNotFoundError:
            return True
        except OSError:
            pass
        try:
            self.consumed.unlink()
        except OSError:
            pass
        return False


@dataclass(frozen=True)
class VerifiedOrigin(Origin):
    stamp: VerifiedStamp
    # No signed record of the writer survived, only its location in the target's own inbox.
    unstamped: bool = False


def selection_writer_inbox_rels(target: Path) -> tuple[str, ...]:
    """Writer inbox paths relative to `target` for its harness selection.

    Falls back to the pre-v0.3.0 `.claude/memory-handoffs` path when no config
    exists. Raises where the config cannot be loaded; callers decide the default.
    """
    from .config import load_config
    from .selection import WRITER_INBOXES

    config = load_config(target)
    if config is None:
        return (".claude/memory-handoffs",)
    return tuple(WRITER_INBOXES[name] for name in sorted(config.selection.harnesses) if name in WRITER_INBOXES)


def own_inbox_paths(target: Path) -> frozenset[Path]:
    """The memory owner's own per-harness writer inboxes, never an externally declared root."""
    try:
        relative = selection_writer_inbox_rels(target)
    except (OSError, ValueError):
        return frozenset()
    return frozenset(path for path in ((target / rel).resolve() for rel in relative) if path.is_dir())


UNSTAMPED_CLAIM_SCHEMA = "brigade.unstamped-origin.v1"


def unstamped_origin(
    target: Path, handoff: Path, snapshot: bytes, *, own_inboxes: frozenset[Path]
) -> VerifiedOrigin | None:
    """Read a stamp-less draft in the owner's own inbox as an unknown origin.

    Any sidecar present at read time, including one that fails verification,
    keeps its review. Only a draft with no sidecar reaches the content policy:
    either the writer never published one, or it was deliberately deleted, which
    takes shell access and so falls outside the threat model. The claim is keyed
    by path and content, so a re-arrival of the same bytes at the same name
    resumes that one claim, while different bytes at the same name get a claim of
    their own.
    """
    if handoff.absolute().parent not in own_inboxes:
        return None
    sidecar = stamp_path(target, handoff)
    try:
        sidecar.lstat()
        return None
    except FileNotFoundError:
        pass
    except OSError:
        return None
    digest = hashlib.sha256(
        (UNSTAMPED_CLAIM_SCHEMA + "\n" + sidecar.stem + "\n" + hashlib.sha256(snapshot).hexdigest()).encode("ascii")
    ).hexdigest()
    return VerifiedOrigin(
        "unknown",
        None,
        "unknown",
        "unknown",
        stamp=UnstampedStamp(sidecar, sidecar.parent.parent / "consumed-stamps" / digest),
        unstamped=True,
    )


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


def publish_unverified_stamp(target: Path, handoff: Path) -> None:
    """Publish a MAC-less sidecar so a draft that cannot be signed waits for review.

    Writers publish this before the draft, so no reader ever lists a draft whose
    origin is still unpublished. A real stamp replaces it atomically; if signing
    then fails, the placeholder keeps the draft in review instead of unstamped.
    Deleting the placeholder afterwards is a deliberate shell action, so that
    draft reads as unstamped and the content policy decides it.
    """
    sidecar = stamp_path(target, handoff)
    sidecar.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = {"schema": SCHEMA, "unstamped": True, "handoff_name": handoff.name, "path_digest": sidecar.stem}
    handoff_publish.publish_handoff(sidecar.parent, sidecar.name, json.dumps(payload) + "\n", replace=True)


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
