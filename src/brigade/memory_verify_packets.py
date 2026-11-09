"""The verify pass's review packets and staged submissions, under `.brigade/memory/verify/packets/`.

Every file here is written by one procedure: an owner-only (0600) temporary
file created with `O_CREAT | O_EXCL | O_NOFOLLOW` inside the descriptor-opened
packet directory, written and fsynced, then renamed over its final name with
`os.replace(..., dir_fd=...)`, and the directory fsynced. A crash leaves at
most a temporary file, never a partial file under a final name. The next run
removes leftover temporary files it owns.

Before a submission is sent, its stage record is published as
`staged-<ledger digest>.json`: the exact ops, the manifest bytes and the
packet bytes. The manifest's `packet_sha256` is checked against the packet
whenever a stage is read, and the request id ledger holds the digest of the
full ops, so a stage edited after it was written is never sent. A stage is
deleted `STAGE_DAYS` after it was created when its submission still has no
result. Once the proposal is open, `finalize` publishes the
packet as `<proposal id>.json` and removes the stage. A packet already under
that name is never replaced: when its bytes differ, finalize reports
`verify_packet_mismatch` and leaves it for review. A stage whose submission
has no result yet stays, so the next run replays the identical request
through the request id ledger.

An open proposal's packet is `<proposal id>.json`. When a run sees the
proposal is no longer open, the packet becomes `<proposal id>.t<ms>.json`, and
it is deleted 14 days after that time. The directory never grows past 64 MiB:
the oldest terminal packets go first, and a new packet that still does not fit
is refused.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import json
import os
import re
import secrets
import stat
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import memory_hub_client as hubc
from . import memory_proposal_io as io
from . import memory_verify_select as pick
from . import memory_verify_state as vstate

PACKETS = f"{vstate.ROOT}/packets"
PACKET_CAP = 64 * 1024 * 1024
PACKET_DAYS = 14
# A stage whose submission has no result is deleted this long after it was created.
STAGE_DAYS = 7
# The hub refuses to bind a verifier manifest left unbound for 24 hours (UNBOUND_MS in
# engines/hub/src/memory/manifest_binding.rs). A replay stops an hour short of that.
HUB_UNBOUND_MS = 24 * 3600 * 1000
MANIFEST_WINDOW_MS = HUB_UNBOUND_MS - 3600 * 1000
DAY_MS = pick.DAY_MS
STAGE_SCHEMA = "brigade.memory-verify-stage.v1"
WORKSPACE_ID = "memory-verify"
MISMATCH = "verify_packet_mismatch"
OPEN_PACKET = re.compile(r"([0-9A-HJKMNP-TV-Z]{26})\.json")
DONE_PACKET = re.compile(r"([0-9A-HJKMNP-TV-Z]{26})\.t([0-9]{1,16})\.json")
STAGED = re.compile(r"staged-([0-9a-f]{64})\.json")
TEMPORARY = re.compile(r"\.packet-[0-9a-f]{32}\.tmp")
STAGE_KEYS = frozenset(
    {
        "schema",
        "digest",
        "scope",
        "card_id",
        "evidence_id",
        "stance",
        "manifest",
        "packet",
        "ops",
        "created_ms",
        "attached_ms",
    }
)


@contextlib.contextmanager
def directory(target: Path) -> Iterator[int]:
    """The packet directory, created owner-only (0700) and opened without following links."""
    with io.directory(target / PACKETS, create=True, private=True) as fd:
        yield fd


def listing(fd: int) -> list[tuple[str, int]]:
    """(name, size) of every regular file in the packet directory."""
    found = []
    for name in os.listdir(fd):
        metadata = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if stat.S_ISREG(metadata.st_mode):
            found.append((name, metadata.st_size))
    return found


def write_all(handle: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(handle, view) :]


def atomic_write(fd: int, name: str, data: bytes) -> None:
    """Publish `data` as `name` in the directory `fd`, all or nothing."""
    temporary = f".packet-{secrets.token_hex(16)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    handle = os.open(temporary, flags, 0o600, dir_fd=fd)
    try:
        try:
            os.fchmod(handle, 0o600)
            write_all(handle, data)
            os.fsync(handle)
        finally:
            os.close(handle)
        os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary, dir_fd=fd)
        raise
    os.fsync(fd)


def read(fd: int, name: str) -> bytes | None:
    """An owner-only regular file's bytes, or None when absent. Anything else raises OSError."""
    try:
        handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    except FileNotFoundError:
        return None
    try:
        metadata = os.fstat(handle)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o077
            or metadata.st_size > PACKET_CAP
        ):
            raise OSError("unsafe packet file")
        parts = []
        while chunk := os.read(handle, 1 << 20):
            parts.append(chunk)
            if sum(len(part) for part in parts) > PACKET_CAP:
                raise OSError("packet file grew past its bound")
        return b"".join(parts)
    finally:
        os.close(handle)


def _unlink(fd: int, name: str) -> None:
    with contextlib.suppress(FileNotFoundError):
        os.unlink(name, dir_fd=fd)


# --- Stages --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Stage:
    """One submission exactly as it is sent: the ops, the manifest it cites and the packet it binds."""

    digest: str
    scope: str
    card_id: str
    evidence_id: str
    stance: str
    manifest: bytes = field(repr=False)
    packet: bytes = field(repr=False)
    ops: tuple[dict[str, Any], ...] = field(repr=False)
    created_ms: int = 0
    # When the hub first held this manifest, or None before any attach succeeded.
    attached_ms: int | None = None

    @property
    def name(self) -> str:
        return f"staged-{self.digest}.json"

    @property
    def ops_sha256(self) -> str:
        """The digest of every submitted op, the manifest's `append_evidence` op included.

        The ledger keeps it beside the request id, and a stage whose ops no
        longer hash to it is never sent.
        """
        return ops_sha256(self.ops)

    def expired(self, now: int) -> bool:
        return now - self.created_ms >= STAGE_DAYS * DAY_MS

    def manifest_stale(self, now: int) -> bool:
        """The attached manifest is too old for the hub to bind it to a proposal."""
        return self.attached_ms is not None and now - self.attached_ms >= MANIFEST_WINDOW_MS

    def record(self) -> bytes:
        return io.json_bytes(
            {
                "schema": STAGE_SCHEMA,
                "digest": self.digest,
                "scope": self.scope,
                "card_id": self.card_id,
                "evidence_id": self.evidence_id,
                "stance": self.stance,
                "manifest": base64.b64encode(self.manifest).decode("ascii"),
                "packet": base64.b64encode(self.packet).decode("ascii"),
                "ops": list(self.ops),
                "created_ms": self.created_ms,
                "attached_ms": self.attached_ms,
            }
        )


def ops_sha256(ops: Any) -> str:
    return io.sha256(io.json_bytes(list(ops)))


def parse(data: bytes, digest: str, submission_digest: Any) -> Stage | None:
    """A stage record, or None when any part of it fails its checks.

    The record must name this digest, its correction ops must hash to it, its
    last op must cite exactly the manifest by its content address, every op
    must name the one card, and the manifest must bind the packet bytes.
    """
    try:
        value = json.loads(data)
        if not isinstance(value, dict) or set(value) != STAGE_KEYS or value["schema"] != STAGE_SCHEMA:
            return None
        manifest = base64.b64decode(value["manifest"], validate=True)
        packet = base64.b64decode(value["packet"], validate=True)
        scope, card_id, evidence_id, ops = value["scope"], value["card_id"], value["evidence_id"], value["ops"]
        bound = json.loads(manifest)
    except (ValueError, TypeError, KeyError, binascii.Error, RecursionError):
        return None
    if (
        value["digest"] != digest
        or not hubc.valid_scope(scope)
        or not hubc.valid_card_id(card_id)
        or value["stance"] not in ("support", "contrary")
        or not isinstance(evidence_id, str)
        or not evidence_id.endswith(f"/{WORKSPACE_ID}/{io.sha256(manifest)}")
        or not isinstance(bound, dict)
        or bound.get("packet_sha256") != io.sha256(packet)
        or bound.get("card", {}).get("scope") != scope
        or bound.get("card", {}).get("card_id") != card_id
        or not isinstance(ops, list)
        or len(ops) < 2
        or not all(isinstance(op, dict) and op.get("scope") == scope and op.get("card_id") == card_id for op in ops)
        or ops[-1].get("type") != "append_evidence"
        or ops[-1].get("evidence_ids") != [evidence_id]
        or submission_digest(ops[:-1]) != digest
        or type(value["created_ms"]) is not int
        or value["created_ms"] < 0
        or not (value["attached_ms"] is None or (type(value["attached_ms"]) is int and value["attached_ms"] >= 0))
    ):
        return None
    return Stage(
        digest,
        scope,
        card_id,
        evidence_id,
        value["stance"],
        manifest,
        packet,
        tuple(ops),
        value["created_ms"],
        value["attached_ms"],
    )


def stage(target: Path, record: Stage) -> None:
    """Publish the stage before anything is attached or submitted."""
    with directory(target) as fd:
        atomic_write(fd, record.name, record.record())


def drop(target: Path, record: Stage) -> None:
    with directory(target) as fd:
        _unlink(fd, record.name)
        os.fsync(fd)


def staged_digests(target: Path) -> set[str]:
    """The ledger digests that have a stage file, read without creating the directory."""
    try:
        with io.directory(target / PACKETS) as fd:
            return {matched[1] for name, _size in listing(fd) if (matched := STAGED.fullmatch(name))}
    except FileNotFoundError:
        return set()


def staged(target: Path, submission_digest: Any) -> tuple[list[Stage], int]:
    """Every valid stage, and the number of invalid ones removed."""
    found, invalid = [], 0
    with directory(target) as fd:
        for name, _size in listing(fd):
            matched = STAGED.fullmatch(name)
            if not matched:
                continue
            data = read(fd, name)
            parsed = None if data is None else parse(data, matched[1], submission_digest)
            if parsed is None:
                _unlink(fd, name)
                invalid += 1
            else:
                found.append(parsed)
        os.fsync(fd)
    return found, invalid


def finalize(target: Path, record: Stage, proposal_id: str) -> str | None:
    """Publish the staged packet as `<proposal id>.json`, then remove the stage.

    A packet already under that name is kept as it is. Returns `MISMATCH`
    when its bytes differ from the staged packet, else None.
    """
    final = f"{proposal_id}.json"
    if not OPEN_PACKET.fullmatch(final):
        raise ValueError("a packet is named by a proposal id")
    with directory(target) as fd:
        existing = read(fd, final)
        if existing is None:
            atomic_write(fd, final, record.packet)
            result = None
        else:
            result = None if io.sha256(existing) == io.sha256(record.packet) else MISMATCH
        _unlink(fd, record.name)
        os.fsync(fd)
    return result


# --- Housekeeping --------------------------------------------------------------------------


def maintain(target: Path, open_ids: set[str], now: int) -> dict[str, int]:
    """Remove owned leftover temporary files, settle packets of closed proposals, expire old ones."""
    counts = {"packets_temporary_removed": 0, "packets_settled": 0, "packets_expired": 0}
    with directory(target) as fd:
        for name in os.listdir(fd):
            if not TEMPORARY.fullmatch(name):
                continue
            metadata = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISREG(metadata.st_mode) and metadata.st_uid == os.getuid():
                _unlink(fd, name)
                counts["packets_temporary_removed"] += 1
        for name, _size in listing(fd):
            opened = OPEN_PACKET.fullmatch(name)
            if opened and opened[1] not in open_ids:
                os.rename(name, f"{opened[1]}.t{now}.json", src_dir_fd=fd, dst_dir_fd=fd)
                counts["packets_settled"] += 1
                continue
            done = DONE_PACKET.fullmatch(name)
            if done and now - int(done[2]) >= PACKET_DAYS * DAY_MS:
                os.unlink(name, dir_fd=fd)
                counts["packets_expired"] += 1
        os.fsync(fd)
    return counts


def room(target: Path, size: int) -> bool:
    """Whether `size` more bytes fit under the cap, deleting the oldest terminal packets first."""
    with directory(target) as fd:
        entries = listing(fd)
        total = sum(entry[1] for entry in entries)
        terminal = sorted(
            (int(done[2]), name, entry_size)
            for name, entry_size in entries
            if (done := DONE_PACKET.fullmatch(name)) is not None
        )
        for _, name, entry_size in terminal:
            if total + size <= PACKET_CAP:
                break
            os.unlink(name, dir_fd=fd)
            total -= entry_size
        return total + size <= PACKET_CAP
