"""Local revision-5 trust evidence, with one atomic POSIX snapshot.

Callers supply an already authenticated hub id. Only explicit initialization
creates state; no operation repairs unsafe or corrupt state. Anchor selection is
explicit CAS per hub. Membership/revocation streams use global (kind, full
SHA256 public-key hash) marks. Other statement kinds are deliberately refused.

Snapshots retain canonical complete signed objects and all anchor transitions.
Membership under a replaced anchor is historical evidence only. Floors expose
unsatisfied facts without erasing history or reconciling replacement policy.
Signed cumulative baselines are distinct from source-attributed effective
revocation barriers, which unsigned authenticated Hub allegations can tighten.
Principal names are attributed claims, not ownership. Peer use consults the
(hub, kid) barrier without principal filtering. Explicit initialize_peers extends
v1 evidence to v2, adding peers and immutable signed peer_records in this same
atomic file. Reads never extend v1. Local use holds the shared directory lock
through bounded local crypto; eligibility grants no message authority, send
permit or current-policy proof. Detached views can stale after lock release.

Binding conflicts are ordinary evidence, bounded by the 8192 barriers. The
separate conflicts table holds up to 4096 authenticated signer/floor conflicts,
including contradictions of retained accepted history: up to 12,288 evidence
rows combined. Ordinary bytes include binding groups and all other metadata;
only authenticated conflicts can use the headroom between 56 and 64 MiB.
That difference is protected headroom, not a separate conflict-byte cap.
Both tables are required in this unpublished v1 schema; incompatible snapshots
are refused without conversion or reset.

There is no arbitrary save API, pruning, migration, network, or private key
storage. Same-user rollback/deletion is outside this filesystem boundary. A
post-replace directory-fsync failure has an indeterminate durability outcome;
identical retries reload under lock and flush before reporting success.
A final directory-close diagnostic may follow a fully flushed generation.
Reload state before retrying a mutation after a lost return.
"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import time
from typing import Any, Iterator, Literal

from . import dirfd
from . import fleet_mesh_crypto as crypto
from . import fleet_mesh_statements as statements

_MAX_BYTES = 64 * 1024 * 1024
_ORDINARY_BYTES = 56 * 1024 * 1024
_MAX_OBJECTS = 4096
_MAX_CONFLICTS = 4096
_MAX_ENTRIES = 8192
# Bound shared-custody directory enumeration independently of snapshot bytes.
# Every name counts; read at most this cap plus one before any cleanup or write.
_MAX_DIRECTORY_ENTRIES = 65_536
_MAX_STRING = 2 * 1024 * 1024
_MAX_SEQUENCE = 2**53 - 1
_LOCK_SECONDS = 10.0
_NAME = "trust-state.json"
_TABLES = {
    "anchors",
    "hubs",
    "objects",
    "marks",
    "floors",
    "baselines",
    "hub_revocations",
    "barriers",
    "binding_conflicts",
    "conflicts",
}
_PEER_TABLES = {"peers", "peer_records"}
# Admission only: legacy evidence retains its existing hard ceilings.
_PEER_BYTES = 8 * 1024 * 1024
_PEER_HEADROOM = 8 * 1024 * 1024
_MAX_PEER_CHANGES = 8
_KINDS = {"membership", "revocation"}
_HEX = re.compile(r"[0-9a-f]{64}", re.ASCII)
_PRINCIPAL = re.compile(r"(?:[A-Za-z0-9][A-Za-z0-9._-]{0,127}|agent:[a-z][a-z0-9._-]{0,62})", re.ASCII)
_TIME = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{3}(?:[0-9]{3})?)?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])",
    re.ASCII,
)


@dataclass(frozen=True)
class TrustSnapshot:
    """Detached evidence data plus unsatisfied floors; neither grants peer use.

    ``data`` is the exact persisted v1 or explicitly extended v2 schema. Its dicts and lists are
    mutable caller-owned copies. ``unsatisfied_floors`` contains detached floor
    objects that have no accepted same-hub/signer object at or above the floor.
    """

    data: dict[str, Any]
    unsatisfied_floors: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class ImportResult:
    """Fixed import disposition; conflict storage failure preserves diagnosis.

    ``status`` is accepted, replay, or signed_statement_conflict. A conflict
    never advances accepted heads for a signer/floor conflict. A retained,
    previously accepted object can report signed_statement_conflict when a
    trusted floor contradicts it; accepted history and heads remain unchanged.
    Active-anchor signature and scope authentication precede classification.
    Accepted attribution disagreements remain accepted: ``evidence_recorded``
    confirms their binding evidence was flushed (also on replay). For signer/
    floor conflicts it confirms conflict evidence was flushed; ``storage_error``
    reports fixed evidence-write failures. Accepted-transaction failures raise.
    """

    status: str
    digest: str
    evidence_recorded: bool = False
    storage_error: str | None = None


def _require(condition: object, code: str = "mesh_trust_invalid") -> None:
    if not condition:
        raise crypto.MeshError(code)


def _platform() -> None:
    _require(os.name == "posix" and dirfd.posix_available(), "mesh_trust_platform_unsupported")


def _kind(kind: str) -> None:
    _require(isinstance(kind, str) and kind in _KINDS, "statement_kind_unsupported")


def _hex(value: Any) -> None:
    _require(isinstance(value, str) and _HEX.fullmatch(value))


def _tag(value: Any, prefix: str) -> None:
    _require(isinstance(value, str) and value.startswith(prefix))
    crypto.decode_binary(value[len(prefix) :], 16)


def _seq(value: Any) -> None:
    _require(type(value) is int and 0 <= value <= _MAX_SEQUENCE)


def _instant(value: Any) -> int:
    """UTC microseconds on an unbounded day axis, including year-edge offsets."""
    _require(isinstance(value, str) and _TIME.fullmatch(value))
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        offset = parsed.utcoffset()
        assert offset is not None
        local = ((parsed.toordinal() * 24 + parsed.hour) * 60 + parsed.minute) * 60 + parsed.second
        return (local - int(offset.total_seconds())) * 1000000 + parsed.microsecond
    except (ValueError, OverflowError):
        raise crypto.MeshError("mesh_trust_invalid") from None


def _private(descriptor: int, *, directory: bool = False) -> None:
    info = os.fstat(descriptor)
    _require(
        (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
        and info.st_uid == os.geteuid()
        and stat.S_IMODE(info.st_mode) == (0o700 if directory else 0o600)
        and (directory or info.st_nlink == 1),
        "mesh_trust_unsafe",
    )


@contextmanager
def _directory(home: Path, *, create: bool) -> Iterator[int]:
    _platform()  # Before Path conversion and every filesystem operation.
    home = Path(home)
    _require(".." not in home.parts, "mesh_trust_unsafe")
    home = home.absolute()
    _require(home != Path(home.anchor), "mesh_trust_unsafe")
    descriptor = None
    child: int | None
    completed = False
    try:
        descriptor = dirfd.open_directory_nofollow(Path(home.anchor))
        components = (*home.parts[1:], "fleet", "mesh-keys")
        for index, component in enumerate(components):
            private = index >= len(home.parts) - 2
            try:
                child = dirfd.open_child_directory(descriptor, component)
            except FileNotFoundError:
                _require(create and private, "mesh_trust_missing")
                try:
                    dirfd.mkdir_child(descriptor, component, 0o700)
                except FileExistsError:
                    pass
                child = dirfd.open_child_directory(descriptor, component)
            try:
                if private:
                    _private(child, directory=True)
                    if create:
                        # An earlier mkdir may have succeeded before its parent
                        # flush failed. Existing children need this barrier too.
                        try:
                            dirfd.fsync_directory(descriptor)
                        except OSError:
                            raise crypto.MeshError("mesh_trust_storage_failure") from None
            except BaseException:
                unadopted, child = child, None
                try:
                    os.close(unadopted)
                except BaseException:
                    pass  # Preserve validation/cancellation; never retry an ambiguous close.
                raise
            previous, descriptor = descriptor, child
            os.close(previous)
        yield descriptor
        completed = True
    except OSError as error:
        # Include initial opens and errors raised through yield. Storage and
        # descriptor exhaustion are distinct from path/permission violations.
        if error.errno in {errno.ENOSPC, errno.EDQUOT, errno.EIO, errno.EROFS, errno.EMFILE, errno.ENFILE}:
            raise crypto.MeshError("mesh_trust_storage_failure") from None
        raise crypto.MeshError("mesh_trust_unsafe") from None
    finally:
        closing, descriptor = descriptor, None
        if closing is not None:
            try:
                os.close(closing)
            except BaseException as error:
                if completed:
                    if isinstance(error, OSError):
                        code = (
                            "mesh_trust_storage_failure"
                            if error.errno
                            in {errno.ENOSPC, errno.EDQUOT, errno.EIO, errno.EROFS, errno.EMFILE, errno.ENFILE}
                            else "mesh_trust_unsafe"
                        )
                        raise crypto.MeshError(code) from None
                    raise
                # A primary exception already owns the diagnosis, including cancellation.


@contextmanager
def _locked(descriptor: int) -> Iterator[None]:
    # The same anchored mesh-keys directory inode used by custody, no lock file.
    import fcntl

    deadline = time.monotonic() + _LOCK_SECONDS
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            _require(time.monotonic() < deadline, "mesh_trust_busy")
            time.sleep(0.01)
    completed = False
    try:
        yield
        completed = True
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except BaseException:
            if completed:
                raise
            # Preserve the active primary; release is attempted only once.


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def _integer(value: str) -> int:
    _require(len(value) <= 16)
    number = int(value)
    _seq(number)
    return number


def _invalid_number(value: str) -> Any:
    raise crypto.MeshError("mesh_trust_invalid")


def _raw_bound(raw: bytes) -> None:
    _require(len(raw) <= _MAX_BYTES and raw.isascii() and b"\0" not in raw)
    depth = span = 0
    quoted = escaped = False
    for char in raw:
        if quoted:
            span += 1
            _require(span <= 2 * _MAX_STRING + 1024)
            if escaped:
                escaped = False
            elif char == 92:
                escaped = True
            elif char == 34:
                quoted = False
        elif char == 34:
            quoted, span = True, 0
        elif char in (91, 123):
            depth += 1
            _require(depth <= 8)
        elif char in (93, 125):
            depth -= 1
            _require(depth >= 0)
    _require(not quoted and depth == 0)


def _tree(value: Any, depth: int = 0) -> None:
    if type(value) is str:
        _require(value.isascii() and len(value) <= _MAX_STRING)
    elif type(value) is int:
        _seq(value)
    elif type(value) is dict:
        _require(depth < 8 and len(value) <= max(_MAX_ENTRIES, len(_TABLES) + 2))
        for key, item in value.items():
            _require(isinstance(key, str) and key.isascii() and len(key) <= 512)
            _tree(item, depth + 1)
    elif type(value) is list:
        _require(depth < 8 and len(value) <= _MAX_ENTRIES)
        for item in value:
            _tree(item, depth + 1)
    else:
        _require(False)


def _fields(value: Any, names: set[str]) -> None:
    _require(type(value) is dict and value.keys() == names)


def _stream(kind: str, signer: str) -> str:
    return kind + ":" + signer


def _scope(kind: str, signer: str, hub: str) -> str:
    return _stream(kind, signer) + ":" + hub


def _barrier_key(hub: str, kid: str) -> str:
    return hub + ":" + kid


def _hub_claim_key(hub: str, kid: str, principal: str) -> str:
    return _barrier_key(hub, kid) + ":" + principal


def _object(
    raw: bytes, *, kind: str, hub: str, signer: str, public_key: bytes
) -> tuple[str, dict[str, str], dict[str, Any]]:
    value = statements.verify_statement(raw, expected_kind=kind, public_key=public_key)
    _require(value["hub"] == hub, "mesh_trust_scope_mismatch")
    canonical = crypto.canonical(value)
    digest = hashlib.sha256(canonical).hexdigest()
    return digest, {"kind": kind, "signer": signer, "hub": hub, "canonical": canonical.decode("ascii")}, value


def _check_object(state: dict[str, Any], digest: str, obj: Any) -> dict[str, Any]:
    _hex(digest)
    _fields(obj, {"kind", "signer", "hub", "canonical"})
    _kind(obj["kind"])
    _hex(obj["signer"])
    _tag(obj["hub"], "h_")
    _require(isinstance(obj["canonical"], str))
    _require(obj["hub"] in state["hubs"] and obj["signer"] in state["hubs"][obj["hub"]])
    anchor = state["anchors"][obj["signer"]]
    actual, checked, value = _object(
        obj["canonical"].encode("ascii"),
        kind=obj["kind"],
        hub=obj["hub"],
        signer=obj["signer"],
        public_key=crypto.decode_binary(anchor["public_key"], 32),
    )
    _require(actual == digest and checked == obj)
    return value


def _revocation(value: Any) -> None:
    _fields(value, {"hub", "principal", "kid", "cutoff"})
    _tag(value["hub"], "h_")
    _tag(value["kid"], "k1_")
    _require(isinstance(value["principal"], str) and _PRINCIPAL.fullmatch(value["principal"]))
    _instant(value["cutoff"])


def _derive(state: dict[str, Any], values: dict[str, dict[str, Any]]) -> tuple[dict, dict, dict]:
    """Reconstruct marks, signed baselines and irreversible attributed barriers."""
    marks: dict[str, Any] = {}
    baselines: dict[str, str] = {}
    barriers: dict[str, Any] = {}
    streams: dict[str, list[tuple[int, str]]] = {}

    def add(hub: str, principal: str, kid: str, cutoff: str, source: str) -> None:
        name = _barrier_key(hub, kid)
        barrier = barriers.setdefault(
            name, {"hub": hub, "principals": set(), "kid": kid, "cutoff": cutoff, "sources": []}
        )
        barrier["principals"].add(principal)
        if _instant(cutoff) < _instant(barrier["cutoff"]):
            barrier["cutoff"] = cutoff
        barrier["sources"].append({"principal": principal, "source": source, "cutoff": cutoff})
        _require(len(barriers) <= _MAX_ENTRIES and len(barrier["sources"]) <= _MAX_ENTRIES, "mesh_trust_capacity")

    for digest, obj in sorted(state["objects"].items()):
        value = values[digest]
        stream = _stream(obj["kind"], obj["signer"])
        mark = {"seq": value["seq"], "digest": digest}
        if stream not in marks or marks[stream]["seq"] < value["seq"]:
            marks[stream] = mark
        else:
            _require(marks[stream]["seq"] != value["seq"])
        if obj["kind"] == "revocation":
            scope = _scope(obj["kind"], obj["signer"], obj["hub"])
            streams.setdefault(scope, []).append((value["seq"], digest))
            for entry in value["revocations"]:
                add(obj["hub"], entry["principal"], entry["kid"], entry["compromised_from"], digest)
    # A global stream cannot contain any two distinct accepted objects at one seq.
    seen: set[tuple[str, int]] = set()
    for digest, obj in state["objects"].items():
        pair = (_stream(obj["kind"], obj["signer"]), values[digest]["seq"])
        _require(pair not in seen)
        seen.add(pair)
    for scope, history in streams.items():
        previous: dict[str, Any] = {}
        for _, digest in sorted(history):
            current = {entry["kid"]: entry for entry in values[digest]["revocations"]}
            for kid, entry in previous.items():
                _require(
                    kid in current
                    and current[kid]["principal"] == entry["principal"]
                    and _instant(current[kid]["compromised_from"]) <= _instant(entry["compromised_from"]),
                    "mesh_trust_revocation_not_cumulative",
                )
            previous = current
        baselines[scope] = max(history)[1]
    for name, entry in sorted(state["hub_revocations"].items()):
        _revocation(entry)
        _require(name == _hub_claim_key(entry["hub"], entry["kid"], entry["principal"]))
        add(entry["hub"], entry["principal"], entry["kid"], entry["cutoff"], "hub")
    for barrier in barriers.values():
        barrier["principals"] = sorted(barrier["principals"])
        barrier["sources"].sort(key=lambda item: (item["principal"], item["source"], item["cutoff"]))
    return marks, baselines, barriers


def _floor(value: Any) -> None:
    _fields(value, {"kind", "signer", "hub", "seq", "digest"})
    _kind(value["kind"])
    _hex(value["signer"])
    _tag(value["hub"], "h_")
    _seq(value["seq"])
    _hex(value["digest"])


def _conflict_id(evidence: dict[str, Any]) -> str:
    return hashlib.sha256(crypto.canonical(evidence)).hexdigest()


def _binding_conflicts(barriers: dict[str, Any]) -> dict[str, Any]:
    """One stable group per disputed hub/kid, with linear source attribution.

    Group identity hashes reason/hub/kid only. Claims grow with retained signed
    history and unsigned principal minima, never with pairwise source products.
    """
    result = {}
    for barrier in barriers.values():
        if len(barrier["principals"]) > 1:
            identity = {"reason": "kid_binding_conflict", "hub": barrier["hub"], "kid": barrier["kid"]}
            result[_conflict_id(identity)] = dict(identity, claims=deepcopy(barrier["sources"]))
    return result


def _update_binding_conflicts(state: dict[str, Any]) -> bool:
    bindings = _binding_conflicts(state["barriers"])
    changed = state["binding_conflicts"] != bindings
    state["binding_conflicts"] = bindings
    return changed


def _binding_evidence_for_digest(state: dict[str, Any], digest: str) -> bool:
    return any(
        any(claim["source"] == digest for claim in evidence["claims"])
        for evidence in state["binding_conflicts"].values()
    )


def _validate(state: Any) -> dict[str, dict[str, Any]]:
    _tree(state)
    _require(type(state) is dict and type(state.get("v")) is int and state["v"] in {1, 2})
    tables = _TABLES | (_PEER_TABLES if state["v"] == 2 else set())
    _fields(state, tables | {"v", "generation"})
    _seq(state["generation"])
    _require(state["generation"] >= 1)
    for name in tables:
        _require(type(state[name]) is dict and len(state[name]) <= _MAX_ENTRIES)
    _require(len(state["objects"]) <= _MAX_OBJECTS and len(state["conflicts"]) <= _MAX_CONFLICTS)
    _require(len(crypto.canonical(dict(state, conflicts={}))) <= _ORDINARY_BYTES)
    for signer, anchor in state["anchors"].items():
        _hex(signer)
        _fields(anchor, {"public_key", "fingerprint"})
        public = crypto.decode_binary(anchor["public_key"], 32)
        _require(hashlib.sha256(public).hexdigest() == signer)
        statements.match_public_key_fingerprint(public, anchor["fingerprint"])
    referenced = set()
    for hub, history in state["hubs"].items():
        _tag(hub, "h_")
        _require(type(history) is list and 1 <= len(history) <= _MAX_OBJECTS)
        for index, signer in enumerate(history):
            _require(signer in state["anchors"] and (index == 0 or history[index - 1] != signer))
            referenced.add(signer)
    _require(referenced == state["anchors"].keys())
    values = {digest: _check_object(state, digest, obj) for digest, obj in state["objects"].items()}
    marks, baselines, barriers = _derive(state, values)
    _require(state["marks"] == marks and state["baselines"] == baselines and state["barriers"] == barriers)
    for name, floor in state["floors"].items():
        _floor(floor)
        _require(name == _scope(floor["kind"], floor["signer"], floor["hub"]))
    # Full equality proves schema, stable identities, complete coverage and
    # every claim's principal/source/cutoff against authenticated derivation.
    _require(state["binding_conflicts"] == _binding_conflicts(barriers))
    for identity, evidence in state["conflicts"].items():
        _hex(identity)
        _require(type(evidence) is dict)
        reason = evidence.get("reason")
        _require(identity == _conflict_id(evidence))
        if reason == "signer_equivocation":
            _fields(evidence, {"reason", "rejected", "digest", "accepted_digest"})
        else:
            _require(reason == "trusted_floor_contradiction")
            _fields(evidence, {"reason", "rejected", "digest", "floor"})
        obj = evidence["rejected"]
        value = _check_object(state, evidence["digest"], obj)
        if reason == "signer_equivocation":
            accepted = state["objects"][evidence["accepted_digest"]]
            _require(
                accepted["kind"] == obj["kind"]
                and accepted["signer"] == obj["signer"]
                and values[evidence["accepted_digest"]]["seq"] == value["seq"]
                and evidence["digest"] != evidence["accepted_digest"],
            )
        else:
            floor = evidence["floor"]
            _floor(floor)
            _require(
                (floor["kind"], floor["signer"], floor["hub"], floor["seq"])
                == (obj["kind"], obj["signer"], obj["hub"], value["seq"])
                and floor["digest"] != evidence["digest"],
            )
            current = state["floors"][_scope(floor["kind"], floor["signer"], floor["hub"])]
            _require(current["seq"] > floor["seq"] or current == floor)
    if state["v"] == 2:
        _validate_peers(state, values)
    return values


def _read(descriptor: int) -> dict[str, Any]:
    source: int | None
    try:
        source = dirfd.open_child_file(descriptor, _NAME, dirfd.file_flags(os.O_RDONLY | os.O_NONBLOCK))
    except FileNotFoundError:
        raise crypto.MeshError("mesh_trust_missing") from None
    completed = False
    try:
        _private(source)
        _require(os.fstat(source).st_size <= _MAX_BYTES)
        with os.fdopen(source, "rb", closefd=False) as stream:
            raw = stream.read(_MAX_BYTES + 1)
        _raw_bound(raw)
        try:
            state = json.loads(
                raw,
                object_pairs_hook=_pairs,
                parse_int=_integer,
                parse_float=_invalid_number,
                parse_constant=_invalid_number,
            )
            _validate(state)
            _require(crypto.canonical(state) == raw)
            completed = True
            return state
        except crypto.MeshExtraMissing:
            raise
        except crypto.MeshError as error:
            if error.code == "mesh_backend_unsupported":
                raise
            raise crypto.MeshError("mesh_trust_invalid") from None
        except (ValueError, TypeError, KeyError, RecursionError, UnicodeError):
            raise crypto.MeshError("mesh_trust_invalid") from None
    finally:
        closing, source = source, None
        try:
            os.close(closing)
        except BaseException:
            if completed:
                raise
            # Keep validation/backend/cancellation priority; never retry close.


def _unsatisfied_floors(state: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    scopes = {(floor["kind"], floor["signer"], floor["hub"]) for floor in state["floors"].values()}
    heads: dict[tuple[str, str, str], tuple[int, set[str]]] = {}
    for digest, obj in state["objects"].items():
        scope = (obj["kind"], obj["signer"], obj["hub"])
        if scope not in scopes:
            continue
        seq = json.loads(obj["canonical"])["seq"]
        head = heads.get(scope)
        if head is None or seq > head[0]:
            heads[scope] = (seq, {digest})
        elif seq == head[0]:
            head[1].add(digest)
    # A higher sequence suffices regardless of digest. At the maximum sequence,
    # retain all tied digests so the exact-match predicate is order-independent.
    unsatisfied = []
    for floor in state["floors"].values():
        head = heads.get((floor["kind"], floor["signer"], floor["hub"]))
        if head is None or head[0] < floor["seq"] or (head[0] == floor["seq"] and floor["digest"] not in head[1]):
            unsatisfied.append(floor)
    return tuple(unsatisfied)


def _snapshot(state: dict[str, Any]) -> TrustSnapshot:
    return TrustSnapshot(deepcopy(state), deepcopy(_unsatisfied_floors(state)))


def _flush(descriptor: int) -> None:
    try:
        dirfd.fsync_directory(descriptor)
    except OSError:
        raise crypto.MeshError("mesh_trust_durability_indeterminate") from None


def _reap_trust_temporaries(descriptor: int) -> None:
    """Remove safe crash leftovers under the directory lock, without opening them."""
    names = dirfd.child_names(descriptor, _MAX_DIRECTORY_ENTRIES + 1)
    _require(len(names) <= _MAX_DIRECTORY_ENTRIES, "mesh_trust_capacity")
    removed = False
    try:
        for name in names:
            if not re.fullmatch(r"\.trust-[0-9a-f]{32}\.tmp", name, re.ASCII):
                continue
            try:
                info = dirfd.stat_child(descriptor, name)
                if (
                    stat.S_ISREG(info.st_mode)
                    and info.st_uid == os.geteuid()
                    and stat.S_IMODE(info.st_mode) == 0o600
                    and info.st_nlink == 1
                ):
                    dirfd.unlink_child(descriptor, name)
                    removed = True
            except FileNotFoundError:
                continue
    finally:
        if removed:
            dirfd.fsync_directory(descriptor)


def _save(descriptor: int, state: dict[str, Any]) -> None:
    _require(state["generation"] < _MAX_SEQUENCE, "mesh_trust_capacity")
    state["generation"] += 1
    raw = crypto.canonical(state)
    _require(len(raw) <= _MAX_BYTES, "mesh_trust_capacity")
    ordinary = crypto.canonical(dict(state, conflicts={}))
    _require(len(ordinary) <= _ORDINARY_BYTES, "mesh_trust_capacity")
    _require(len(state["objects"]) <= _MAX_OBJECTS and len(state["conflicts"]) <= _MAX_CONFLICTS, "mesh_trust_capacity")
    _require(
        all(len(state[name]) <= _MAX_ENTRIES for name in _TABLES | (_PEER_TABLES if state["v"] == 2 else set())),
        "mesh_trust_capacity",
    )
    # Public mutators construct only this bounded schema. Validate before publish
    # as well as on load; no partial marks or derived barriers can be committed.
    _validate(state)
    try:
        _reap_trust_temporaries(descriptor)
    except OSError:
        raise crypto.MeshError("mesh_trust_storage_failure") from None
    temporary = ".trust-" + secrets.token_hex(16) + ".tmp"
    destination = None
    created = False
    completed = False
    try:
        destination = dirfd.open_child_file(
            descriptor, temporary, dirfd.file_flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        )
        created = True
        _private(destination)
        stream = os.fdopen(destination, "wb", closefd=False)
        stream_completed = False
        try:
            stream.write(raw)
            stream.flush()
            os.fsync(destination)
            stream_completed = True
        finally:
            try:
                stream.close()
            except BaseException:
                if stream_completed:
                    raise
                # A close-time reflush must preserve the active writer failure.
        # close may consume the fd even when it raises. Relinquish ownership
        # before attempting it, and never publish after an ambiguous close.
        closing, destination = destination, None
        os.close(closing)
        dirfd.replace_children(descriptor, temporary, _NAME)
        completed = True
    except OSError:
        raise crypto.MeshError("mesh_trust_storage_failure") from None
    finally:
        if destination is not None:
            closing, destination = destination, None
            try:
                os.close(closing)
            except BaseException:
                pass  # Preserve the primary failure; an ambiguous close is not retried.
        if created:
            try:
                dirfd.unlink_child(descriptor, temporary)
            except FileNotFoundError:
                pass  # Expected after replace, limited to this attempt's temporary.
            except OSError:
                if completed:
                    raise crypto.MeshError("mesh_trust_storage_failure") from None
            except BaseException:
                if completed:
                    raise
                # Both cleanup attempts preserve an active writer error/cancellation.
    _flush(descriptor)


def initialize_trust(*, brigade_home: Path) -> TrustSnapshot:
    """Explicitly create a missing store, or validate/flush an existing store."""
    with _directory(brigade_home, create=True) as descriptor, _locked(descriptor):
        try:
            state = _read(descriptor)
        except crypto.MeshError as error:
            if error.code != "mesh_trust_missing":
                raise
            state = {"v": 1, "generation": 0, **{name: {} for name in _TABLES}}
            _save(descriptor, state)
        else:
            _flush(descriptor)
        return _snapshot(state)


def load_trust(*, brigade_home: Path) -> TrustSnapshot:
    """Read only; never initialize, flush, repair, adopt or reap files."""
    with _directory(brigade_home, create=False) as descriptor, _locked(descriptor):
        return _snapshot(_read(descriptor))


def pin_manifest_anchor(
    *,
    brigade_home: Path,
    hub: str,
    public_key_file: bytes,
    expected_fingerprint: str,
    expected_current_signer: str | None,
) -> TrustSnapshot:
    """Pin/replace an explicit key file with independent full fingerprint/CAS.

    Expected None means no active anchor. Same-anchor retries may carry the
    immediate predecessor CAS. Reactivation retains its original marks.
    """
    _platform()
    _tag(hub, "h_")
    _require(isinstance(public_key_file, bytes) and len(public_key_file) == 43, "mesh_trust_key_file_invalid")
    try:
        public = crypto.decode_binary(public_key_file.decode("ascii"), 32)
    except (ValueError, UnicodeError):
        raise crypto.MeshError("mesh_trust_key_file_invalid") from None
    statements.match_public_key_fingerprint(public, expected_fingerprint)
    signer = hashlib.sha256(public).hexdigest()
    if expected_current_signer is not None:
        _hex(expected_current_signer)
    with _directory(brigade_home, create=False) as descriptor, _locked(descriptor):
        state = _read(descriptor)
        history = state["hubs"].get(hub, [])
        current = history[-1] if history else None
        # A retry can carry the pre-commit CAS after a directory-fsync failure.
        same = current == signer
        _require(
            current == expected_current_signer
            or (
                same
                and (
                    (len(history) == 1 and expected_current_signer is None)
                    or (len(history) > 1 and history[-2] == expected_current_signer)
                )
            ),
            "mesh_trust_anchor_cas_mismatch",
        )
        if same:
            _flush(descriptor)
        else:
            _require(len(history) < _MAX_OBJECTS, "mesh_trust_capacity")
            state["anchors"][signer] = {"public_key": crypto.encode_binary(public), "fingerprint": expected_fingerprint}
            state["hubs"][hub] = [*history, signer]
            _save(descriptor, state)
        return _snapshot(state)


def require_floor(*, brigade_home: Path, hub: str, kind: str, signer: str, seq: int, digest: str) -> TrustSnapshot:
    """Atomically install/tighten a floor and evidence contradicting retained history.

    A matching historical tuple with a different digest is a trusted-floor
    contradiction even if a higher head already satisfies the new floor. The
    accepted object stays retained, without labeling the signer an equivocator.
    """
    _platform()
    value = {"kind": kind, "signer": signer, "hub": hub, "seq": seq, "digest": digest}
    _floor(value)
    name = _scope(kind, signer, hub)
    with _directory(brigade_home, create=False) as descriptor, _locked(descriptor):
        state = _read(descriptor)
        previous = state["floors"].get(name)
        changed = previous != value
        _require(not changed or previous is None or seq > previous["seq"], "mesh_trust_floor_not_tightened")
        state["floors"][name] = value
        for accepted_digest, obj in state["objects"].items():
            if (
                (obj["kind"], obj["signer"], obj["hub"]) == (kind, signer, hub)
                and json.loads(obj["canonical"])["seq"] == seq
                and accepted_digest != digest
            ):
                evidence = {
                    "reason": "trusted_floor_contradiction",
                    "rejected": deepcopy(obj),
                    "digest": accepted_digest,
                    "floor": deepcopy(value),
                }
                identity = _conflict_id(evidence)
                if identity not in state["conflicts"]:
                    state["conflicts"][identity] = evidence
                    changed = True
        if changed:
            _save(descriptor, state)
        else:
            _flush(descriptor)
        return _snapshot(state)


def _record_conflict(descriptor: int, state: dict[str, Any], evidence: dict[str, Any]) -> ImportResult:
    identity = _conflict_id(evidence)
    try:
        if identity in state["conflicts"]:
            _flush(descriptor)
        else:
            state["conflicts"][identity] = evidence
            _save(descriptor, state)
    except crypto.MeshError as error:
        return ImportResult("signed_statement_conflict", evidence["digest"], False, error.code)
    return ImportResult("signed_statement_conflict", evidence["digest"], True)


def import_statement(*, brigade_home: Path, hub: str, kind: str, raw: bytes) -> ImportResult:
    """Authenticate active-anchor signature/scope before any disposition.

    A retained accepted object contradicted by a trusted floor reports
    signed_statement_conflict, including after heads or floors advance.
    Accepted history and heads remain unchanged; evidence_recorded is true
    only after the conflict evidence is flushed.
    """
    _platform()
    _kind(kind)
    _tag(hub, "h_")
    with _directory(brigade_home, create=False) as descriptor, _locked(descriptor):
        state = _read(descriptor)
        _require(hub in state["hubs"], "mesh_trust_anchor_missing")
        signer = state["hubs"][hub][-1]
        public = crypto.decode_binary(state["anchors"][signer]["public_key"], 32)
        digest, obj, value = _object(raw, kind=kind, hub=hub, signer=signer, public_key=public)
        # Rejected evidence remains idempotent after heads/floors advance. The
        # active anchor, signature and caller-supplied scope still check first.
        for evidence in state["conflicts"].values():
            if (
                evidence["reason"] in {"signer_equivocation", "trusted_floor_contradiction"}
                and evidence["digest"] == digest
                and evidence["rejected"] == obj
            ):
                return _record_conflict(descriptor, state, evidence)
        stream = _stream(kind, signer)
        mark = state["marks"].get(stream)
        if mark is not None and value["seq"] == mark["seq"] and digest != mark["digest"]:
            return _record_conflict(
                descriptor,
                state,
                {
                    "reason": "signer_equivocation",
                    "rejected": obj,
                    "digest": digest,
                    "accepted_digest": mark["digest"],
                },
            )
        floor = state["floors"].get(_scope(kind, signer, hub))
        if floor is not None:
            if value["seq"] == floor["seq"] and digest != floor["digest"]:
                return _record_conflict(
                    descriptor,
                    state,
                    {
                        "reason": "trusted_floor_contradiction",
                        "rejected": obj,
                        "digest": digest,
                        "floor": deepcopy(floor),
                    },
                )
            _require(value["seq"] >= floor["seq"], "signed_statement_rollback")
        if mark is not None:
            _require(value["seq"] >= mark["seq"], "signed_statement_rollback")
            if value["seq"] == mark["seq"]:
                _flush(descriptor)
                return ImportResult("replay", digest, _binding_evidence_for_digest(state, digest))
        state["objects"][digest] = obj
        values = {d: json.loads(o["canonical"]) for d, o in state["objects"].items()}
        state["marks"], state["baselines"], state["barriers"] = _derive(state, values)
        binding_evidence = _update_binding_conflicts(state)
        _save(descriptor, state)
        return ImportResult("accepted", digest, binding_evidence)


def record_hub_revocation(*, brigade_home: Path, hub: str, principal: str, kid: str, cutoff: str) -> TrustSnapshot:
    """Record a typed allegation from an authenticated Hub caller; earliest wins.

    This is not a signed baseline and never changes a signed statement mark.
    Same-principal later/equivalent cutoffs are durable no-ops. A different
    principal adds attributed denial evidence even when its cutoff is later.
    """
    _platform()
    entry = {"hub": hub, "principal": principal, "kid": kid, "cutoff": cutoff}
    _revocation(entry)
    name = _hub_claim_key(hub, kid, principal)
    with _directory(brigade_home, create=False) as descriptor, _locked(descriptor):
        state = _read(descriptor)
        previous = state["hub_revocations"].get(name)
        if previous is not None and _instant(previous["cutoff"]) <= _instant(cutoff):
            _flush(descriptor)
        else:
            state["hub_revocations"][name] = entry
            values = {d: json.loads(o["canonical"]) for d, o in state["objects"].items()}
            state["marks"], state["baselines"], state["barriers"] = _derive(state, values)
            _update_binding_conflicts(state)
            _save(descriptor, state)
        return _snapshot(state)


@dataclass(frozen=True)
class ContinuityRefusal:
    """Detached proof-dependency refusal, never a descendant revocation."""

    reason: Literal["continuity_ancestor_revoked"]
    ancestor_ref: str
    ancestor_digest: str
    barrier: dict[str, Any]


@dataclass(frozen=True)
class PeerUse:
    """Detached eligibility evidence, valid only inside peer_for_use's lock.

    No message authentication, authority, send permit or freshness is implied.
    Revoked sender records are returned for inspection, never receipt-qualified.
    """

    eligible: bool
    error: str | None
    key_status: str
    provenance: str | None
    record: bytes | None
    validity: dict[str, Any]
    barrier: dict[str, Any] | None
    membership: dict[str, Any] | None
    changed: tuple[dict[str, Any], ...]
    floors: tuple[dict[str, Any], ...]
    conflicts: tuple[dict[str, Any], ...]
    hub: str
    principal: str
    kid: str | None
    trust_generation: int
    identity_generation: int
    identity_digest: str
    identity_provenance: str
    continuity_refusal: ContinuityRefusal | None = None
    policy_freshness: Literal["unproven"] = "unproven"


@dataclass(frozen=True)
class PeerSnapshot:
    """Caller-owned inspection copies; signed canonical bytes are authoritative."""

    data: dict[str, Any]
    hub: str
    principal: str | None
    head: bytes | None
    head_digest: str | None
    history: tuple[dict[str, Any], ...]
    provenance: str | None
    use: PeerUse | None
    trust_generation: int
    identity_generation: int
    identity_digest: str
    identity_provenance: str
    policy_freshness: Literal["unproven"] = "unproven"


@dataclass(frozen=True)
class PeerAcceptanceResult:
    """Fixed disposition; authenticated changed survives evidence-write failure."""

    disposition: Literal["accepted", "replay", "changed", "refused"]
    error: str | None
    peer: PeerSnapshot
    evidence_recorded: bool = False
    storage_error: str | None = None


def _peer_name(*parts: str) -> str:
    return hashlib.sha256(crypto.canonical(list(parts))).hexdigest()


def _peer_parameters(principal: str, require_verified: bool) -> None:
    _platform()
    _require(isinstance(principal, str) and _PRINCIPAL.fullmatch(principal), "mesh_peer_arguments_invalid")
    _require(type(require_verified) is bool, "mesh_peer_arguments_invalid")


def _peer_record(raw: bytes) -> tuple[str, str, dict[str, Any]]:
    """Bound raw JSON before allocation; never authenticate an untrusted dict."""
    try:
        statements._raw_bound(raw, crypto.MAX_ENVELOPE_BYTES)
        value = json.loads(
            raw,
            object_pairs_hook=statements._pairs,
            parse_int=statements._integer,
            parse_float=statements._invalid_number,
            parse_constant=statements._invalid_number,
        )
        statements._json_types(value)
        _require(type(value) is dict, "mesh_peer_record_invalid")
        canonical = crypto.canonical(value)
        _require(len(canonical) <= crypto.MAX_ENVELOPE_BYTES, "mesh_peer_record_invalid")
        _tag(value.get("hub"), "h_")  # The frozen crypto core also supports mesh; peers do not.
        crypto.verify_key_record(value)
        return hashlib.sha256(canonical).hexdigest(), canonical.decode("ascii"), value
    except crypto.MeshExtraMissing:
        raise
    except crypto.MeshError as error:
        if error.code == "mesh_backend_unsupported":
            raise
        raise crypto.MeshError("mesh_peer_record_invalid") from None
    except (ValueError, TypeError, KeyError, RecursionError, UnicodeError):
        raise crypto.MeshError("mesh_peer_record_invalid") from None


@contextmanager
def _peer_directory(home: Path) -> Iterator[int]:
    opened = False
    try:
        with _directory(home, create=False) as descriptor:
            opened = True
            with _locked(descriptor):
                yield descriptor
    except crypto.MeshError as error:
        if not opened and error.code == "mesh_trust_missing":
            raise crypto.MeshError("hub_identity_unverified") from None
        raise


def _peer_identity(descriptor: int, origin: str) -> dict[str, Any]:
    # Deliberate narrow private coupling, reciprocal to identity's trust lock
    # adapter. Local import avoids the cycle. Public load_identity would acquire
    # a second descriptor lock and deadlock. Identity/custody are read-only here.
    from . import fleet_mesh_hub_identity as identity

    snapshot = identity._snapshot(identity._read(descriptor), origin)
    value = snapshot.identity
    assert value is not None and snapshot.provenance is not None
    signer = hashlib.sha256(crypto.decode_binary(value["hub_sig_pub"], 32)).hexdigest()
    _require(
        not any(f["hub"] == value["hub_id"] and f["signer"] == signer for f in snapshot.unsatisfied_floors),
        "hub_identity_floor_unsatisfied",
    )
    return {
        "hub": value["hub_id"],
        "digest": snapshot.data["origins"][origin]["current_digest"],
        "provenance": snapshot.provenance,
        "generation": snapshot.data["generation"],
        "floors": list(snapshot.data["floors"].values()),
        "conflicts": [
            c for c in snapshot.data["conflicts"].values() if json.loads(c["canonical"])["hub_id"] == value["hub_id"]
        ],
    }


def _membership(state: dict[str, Any], hub: str) -> tuple[dict[str, Any] | None, str | None]:
    history = state["hubs"].get(hub, [])
    signer = history[-1] if history else None
    pending = [f for f in _unsatisfied_floors(state) if f["kind"] == "membership" and f["hub"] == hub]
    if any(f["signer"] != signer for f in pending):
        return None, "mesh_peer_membership_reconciliation_unsupported"
    if pending:
        return None, None
    selected = None
    for digest, obj in state["objects"].items():
        if (obj["kind"], obj["signer"], obj["hub"]) == ("membership", signer, hub):
            value = json.loads(obj["canonical"])
            if selected is None or selected["seq"] < value["seq"]:
                selected = dict(value, digest=digest, signer=signer)
    return selected, None


def _validate_peers(state: dict[str, Any], values: dict[str, dict[str, Any]]) -> None:
    """Reconstruct tuple bindings, provenance, ordered history and change proofs.

    Evidence survives new floors, replaced anchors and later revocations. Those
    affect today's use, never erase the original authenticated acceptance.
    """
    records = state["peer_records"]
    parsed = {}
    for name, entry in records.items():
        _fields(
            entry,
            {
                "hub",
                "principal",
                "kid",
                "canonical",
                "digest",
                "accepted_at",
                "observation",
                "confirmation",
                "predecessor",
                "method",
            },
        )
        _require(isinstance(entry["canonical"], str))
        digest, canonical, value = _peer_record(entry["canonical"].encode("ascii"))
        _require((entry["hub"], entry["principal"], entry["kid"]) == (value["hub"], value["principal"], value["kid"]))
        _require(
            name == _peer_name(entry["hub"], entry["principal"], entry["kid"])
            and entry["digest"] == digest
            and entry["canonical"] == canonical
        )
        _instant(entry["accepted_at"])
        _require(entry["method"] in {"first", "manual", "continuity"})
        _require(type(entry["observation"]) is dict and type(entry["confirmation"]) is dict)
        if entry["observation"]:
            evidence = entry["observation"]
            _fields(evidence, {"digest", "signer", "at"})
            obj = state["objects"][evidence["digest"]]
            _require((obj["kind"], obj["hub"], obj["signer"]) == ("membership", entry["hub"], evidence["signer"]))
            _require(entry["principal"] in values[evidence["digest"]]["members"])
            _instant(evidence["at"])
        if entry["confirmation"]:
            evidence = entry["confirmation"]
            _fields(evidence, {"fingerprint", "at", "head"})
            statements.match_public_key_fingerprint(crypto.decode_binary(value["sig_pub"], 32), evidence["fingerprint"])
            _instant(evidence["at"])
            _require(isinstance(evidence["head"], str))
        parsed[name] = value
    referenced = set()
    for name, peer in state["peers"].items():
        _fields(peer, {"hub", "principal", "history", "changes", "acceptance"})
        _require(name == _peer_name(peer["hub"], peer["principal"]))
        history = peer["history"]
        _require(type(history) is list and 1 <= len(history) <= _MAX_ENTRIES and len(set(history)) == len(history))
        previous = ""
        indices = {}
        for index, reference in enumerate(history):
            entry, value = records[reference], parsed[reference]
            _require(
                (entry["hub"], entry["principal"]) == (peer["hub"], peer["principal"]) and reference not in referenced
            )
            _require(entry["predecessor"] == previous)
            confirmation = entry["confirmation"]
            if previous:
                old = records[previous]
                _require(_instant(value["created_at"]) > _instant(parsed[previous]["created_at"]))
                _require(entry["method"] in {"manual", "continuity"})
                if entry["method"] == "continuity":
                    crypto.verify_key_record(value, previous_record=parsed[previous])
                else:
                    _require(bool(confirmation))
                allowed_cas = {old["digest"], entry["digest"]}
            else:
                _require(entry["method"] == "first")
                allowed_cas = {"", entry["digest"]}
            if confirmation:
                _require(confirmation["head"] in allowed_cas)
            indices[entry["digest"]] = index
            referenced.add(reference)
            previous = reference
        acceptance = peer["acceptance"]
        _require(type(acceptance) is dict)
        if acceptance:
            _fields(acceptance, {"digest", "head", "at", "fingerprint"})
            entry = records[history[-1]]
            _require(acceptance["digest"] == entry["digest"] and bool(entry["confirmation"]))
            predecessor = records[history[-2]]["digest"] if len(history) > 1 else ""
            _require(acceptance["head"] in {entry["digest"], predecessor})
            _require(acceptance["fingerprint"] == entry["confirmation"]["fingerprint"])
            _instant(acceptance["at"])
        _require(type(peer["changes"]) is dict and len(peer["changes"]) <= _MAX_ENTRIES)
        for identity, change in peer["changes"].items():
            _fields(change, {"canonical", "digest", "head", "resolved_by"})
            _require(isinstance(change["canonical"], str))
            digest, canonical, candidate = _peer_record(change["canonical"].encode("ascii"))
            _require(candidate["hub"] == peer["hub"] and candidate["principal"] == peer["principal"])
            _require(change["digest"] == digest and change["canonical"] == canonical and change["head"] in indices)
            _require(identity == _peer_name(change["head"], digest))
            if change["resolved_by"]:
                _require(change["resolved_by"] in indices and indices[change["resolved_by"]] >= indices[change["head"]])
                _require(bool(records[history[indices[change["resolved_by"]]]]["confirmation"]))
            else:
                _require(change["resolved_by"] == "")
    _require(referenced == records.keys())


def _provenance(state: dict[str, Any], reference: str) -> str:
    # Iterative traversal avoids recursion at the 8192 retained-record bound.
    while reference:
        entry = state["peer_records"][reference]
        if entry["confirmation"]:
            return "verified"
        if entry["observation"]:
            return "observed"
        if entry["method"] != "continuity":
            break
        reference = entry["predecessor"]
    return "unverified"


def _continuity_refusal(state: dict[str, Any], reference: str) -> ContinuityRefusal | None:
    # Eight limits a supplied batch, not accumulated retained ancestry. Only
    # independent confirmation ends this dependency, never membership observation.
    for _ in range(_MAX_ENTRIES):
        entry = state["peer_records"][reference]
        barrier = state["barriers"].get(_barrier_key(entry["hub"], entry["kid"]))
        if barrier:
            return ContinuityRefusal("continuity_ancestor_revoked", reference, entry["digest"], deepcopy(barrier))
        if entry["confirmation"] or entry["method"] != "continuity":
            return None
        reference = entry["predecessor"]
    raise crypto.MeshError("mesh_trust_invalid")


def _peer_use(
    state: dict[str, Any],
    identity: dict[str, Any],
    principal: str,
    require_verified: bool,
    *,
    purpose: str = "recipient",
    kid: str | None = None,
    instant: str | None = None,
) -> PeerUse:
    hub = identity["hub"]
    peer = state["peers"].get(_peer_name(hub, principal))
    history = peer["history"] if peer else []
    reference = history[-1] if history and purpose == "recipient" else _peer_name(hub, principal, kid or "")
    entry = state["peer_records"].get(reference)
    barrier = state["barriers"].get(_barrier_key(hub, entry["kid"] if entry else kid or ""))
    dependency = _continuity_refusal(state, reference) if entry and not barrier else None
    membership, membership_error = _membership(state, hub)
    changes = tuple(peer["changes"].values()) if peer else ()
    provenance = _provenance(state, reference) if entry else None
    validity: dict[str, Any] = {}
    interval_ok = True
    if entry:
        index = history.index(reference)
        value = json.loads(entry["canonical"])
        retirement = (
            json.loads(state["peer_records"][history[index + 1]]["canonical"])["created_at"]
            if index + 1 < len(history)
            else None
        )
        validity = {
            "start": value["created_at"],
            "retirement": retirement,
            "cutoff": barrier["cutoff"] if barrier else None,
        }
        if instant is not None:
            timestamp = _instant(instant)
            interval_ok = timestamp >= _instant(value["created_at"]) and (
                retirement is None or timestamp < _instant(retirement) + 300_000_000
            )
    error = None
    status = provenance or "missing"
    if barrier:
        status, error = "revoked", purpose + "_key_revoked"
    elif dependency is not None or (peer and any(not change["resolved_by"] for change in changes)):
        status, error = "changed", purpose + "_key_changed"
    elif entry is None:
        status, error = "missing", purpose + "_key_missing"
    elif provenance == "observed" and membership_error:
        error = membership_error
    elif provenance == "observed" and membership is None:
        status, error = "unverified", purpose + "_key_unverified"
    elif provenance == "observed" and membership is not None and principal not in membership["members"]:
        status, error = "observed_not_member", purpose + "_key_not_member"
    elif provenance == "unverified" or (require_verified and provenance != "verified"):
        error = purpose + "_key_unverified"
    elif not interval_ok:
        error = "mesh_peer_outside_interval"
    return PeerUse(
        error is None,
        error,
        status,
        provenance,
        entry["canonical"].encode("ascii") if entry else None,
        deepcopy(validity),
        deepcopy(barrier),
        deepcopy(membership),
        deepcopy(changes),
        tuple(
            dict(f, source=source)
            for source, floors in (("trust", state["floors"].values()), ("identity", identity["floors"]))
            for f in floors
            if f["hub"] == hub
        ),
        tuple(
            deepcopy([c for c in state["conflicts"].values() if c["rejected"]["hub"] == hub] + identity["conflicts"])
        ),
        hub,
        principal,
        entry["kid"] if entry else kid,
        state["generation"],
        identity["generation"],
        identity["digest"],
        identity["provenance"],
        continuity_refusal=dependency,
    )


def _peer_snapshot(
    state: dict[str, Any], identity: dict[str, Any], principal: str | None = None, require_verified: bool = False
) -> PeerSnapshot:
    peer = state["peers"].get(_peer_name(identity["hub"], principal)) if principal is not None else None
    history = tuple(deepcopy(state["peer_records"][name]) for name in peer["history"]) if peer else ()
    head = history[-1] if history else None
    use = _peer_use(state, identity, principal, require_verified) if principal is not None else None
    return PeerSnapshot(
        deepcopy(state),
        identity["hub"],
        principal,
        head["canonical"].encode("ascii") if head else None,
        head["digest"] if head else None,
        history,
        use.provenance if use else None,
        use,
        state["generation"],
        identity["generation"],
        identity["digest"],
        identity["provenance"],
    )


def _peer_state(descriptor: int, origin: str) -> tuple[dict[str, Any], dict[str, Any]]:
    identity = _peer_identity(descriptor, origin)
    state = _read(descriptor)
    _require(state["v"] == 2, "mesh_peers_missing")
    return state, identity


def _admit_peers(state: dict[str, Any]) -> None:
    """Preflight a real peer commit, including next-generation serialization.

    Reads and legacy evidence admissions retain the original 56/64 MiB limits.
    Headroom is finite evidence allowance, not unlimited revocation availability.
    """
    prospective = dict(state, generation=state["generation"] + 1)
    _require(len(crypto.canonical({name: state[name] for name in _PEER_TABLES})) <= _PEER_BYTES, "mesh_trust_capacity")
    _require(
        len(crypto.canonical(dict(prospective, conflicts={}))) <= _ORDINARY_BYTES - _PEER_HEADROOM,
        "mesh_trust_capacity",
    )
    _require(len(crypto.canonical(prospective)) <= _MAX_BYTES - _PEER_HEADROOM, "mesh_trust_capacity")


def initialize_peers(*, brigade_home: Path, origin: str) -> PeerSnapshot:
    """Explicitly extend a valid v1 store against an accepted current identity."""
    _platform()
    origin = statements.normalize_origin(origin)
    with _peer_directory(brigade_home) as descriptor:
        identity = _peer_identity(descriptor, origin)
        state = _read(descriptor)
        if state["v"] == 1:
            state.update(v=2, peers={}, peer_records={})
            _admit_peers(state)
            _save(descriptor, state)
        else:
            _flush(descriptor)
        result = _peer_snapshot(state, identity)
    return result


def load_peer(*, brigade_home: Path, origin: str, principal: str, require_verified: bool) -> PeerSnapshot:
    """Read-only inspection. Re-enter peer_for_use for actual local crypto."""
    _peer_parameters(principal, require_verified)
    origin = statements.normalize_origin(origin)
    with _peer_directory(brigade_home) as descriptor:
        state, identity = _peer_state(descriptor, origin)
        return _peer_snapshot(state, identity, principal, require_verified)


@contextmanager
def peer_for_use(
    *,
    brigade_home: Path,
    origin: str,
    principal: str,
    purpose: Literal["recipient", "sender"],
    require_verified: bool,
    hub: str | None = None,
    kid: str | None = None,
    signed_created_at: str | None = None,
) -> Iterator[PeerUse]:
    """Hold the shared inode lock during bounded LOCAL crypto only.

    No Hub I/O, nested store calls, await or user interaction in the scope.
    Returned eligibility can stale on exit and is not a message/send authority.
    """
    _peer_parameters(principal, require_verified)
    _require(purpose in {"recipient", "sender"}, "mesh_peer_arguments_invalid")
    if purpose == "recipient":
        _require(hub is None and kid is None and signed_created_at is None, "mesh_peer_arguments_invalid")
    else:
        _tag(hub, "h_")
        _tag(kid, "k1_")
        _instant(signed_created_at)
    origin = statements.normalize_origin(origin)
    body_error: BaseException | None = None
    try:
        with _peer_directory(brigade_home) as descriptor:
            state, identity = _peer_state(descriptor, origin)
            _require(hub is None or hub == identity["hub"], "mesh_peer_scope_mismatch")
            result = _peer_use(
                state, identity, principal, require_verified, purpose=purpose, kid=kid, instant=signed_created_at
            )
            try:
                yield result
            except BaseException as error:
                body_error = error
                raise
    except BaseException as error:
        if body_error is None or error is body_error:
            raise
    # Restore after a legacy translation, outside its active handler, so Python
    # preserves the caller's cause, context and suppression intent as well.
    if body_error is not None:
        raise body_error


def record_for(
    *,
    brigade_home: Path,
    origin: str,
    hub: str,
    principal: str,
    kid: str,
    signed_created_at: str,
    require_verified: bool,
) -> PeerUse:
    """Detached sender-history inspection, without receipt-qualified authority."""
    with peer_for_use(
        brigade_home=brigade_home,
        origin=origin,
        principal=principal,
        purpose="sender",
        require_verified=require_verified,
        hub=hub,
        kid=kid,
        signed_created_at=signed_created_at,
    ) as result:
        return result


def _peer_insert(
    state: dict[str, Any],
    peer: dict[str, Any],
    parsed: tuple[str, str, dict[str, Any]],
    *,
    method: str,
    observation: dict[str, Any],
    confirmation: dict[str, Any],
    now: str,
) -> None:
    digest, canonical, value = parsed
    name = _peer_name(value["hub"], value["principal"], value["kid"])
    _require(name not in state["peer_records"], "mesh_peer_history_transition_unsupported")
    state["peer_records"][name] = {
        "hub": value["hub"],
        "principal": value["principal"],
        "kid": value["kid"],
        "canonical": canonical,
        "digest": digest,
        "accepted_at": now,
        "observation": observation,
        "confirmation": confirmation,
        "predecessor": peer["history"][-1] if peer["history"] else "",
        "method": method,
    }
    peer["history"].append(name)


def _peer_new(state: dict[str, Any], hub: str, principal: str) -> dict[str, Any]:
    return state["peers"].setdefault(
        _peer_name(hub, principal), {"hub": hub, "principal": principal, "history": [], "changes": {}, "acceptance": {}}
    )


def _peer_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _peer_admission_clock() -> int:
    """Actual local wall time on _instant's microsecond axis, not metadata time."""
    return _instant("1970-01-01T00:00:00Z") + time.time_ns() // 1000


def _admit_peer_times(records: list[tuple[str, str, dict[str, Any]]]) -> None:
    """New admissions only; stored history and completed retries ignore clocks.

    One local cutoff covers the entire authenticated batch under the inode lock.
    This bounds distant future heads, without relaxing immutable intervals.
    """
    cutoff = _peer_admission_clock() + 300_000_000
    _require(all(_instant(record[2]["created_at"]) <= cutoff for record in records), "mesh_peer_created_in_future")


def _peer_refused(
    state: dict[str, Any], identity: dict[str, Any], principal: str, code: str, require_verified: bool
) -> PeerAcceptanceResult:
    return PeerAcceptanceResult("refused", code, _peer_snapshot(state, identity, principal, require_verified))


def accept_peer(
    *,
    brigade_home: Path,
    origin: str,
    principal: str,
    raw: bytes,
    expected_fingerprint: str,
    expected_head_digest: str | None,
    require_verified: bool,
) -> PeerAcceptanceResult:
    """Independent full fingerprint plus explicit head-digest CAS, no trust inputs.

    Identical completed retries retain pre-transition CAS and timestamps. A
    later revocation changes the returned current view, never revives that kid.
    """
    _peer_parameters(principal, require_verified)
    origin = statements.normalize_origin(origin)
    parsed = _peer_record(raw)
    digest, _, value = parsed
    statements.match_public_key_fingerprint(crypto.decode_binary(value["sig_pub"], 32), expected_fingerprint)
    if expected_head_digest is not None:
        _hex(expected_head_digest)
    with _peer_directory(brigade_home) as descriptor:
        state, identity = _peer_state(descriptor, origin)
        _require((value["hub"], value["principal"]) == (identity["hub"], principal), "mesh_peer_scope_mismatch")
        peer = state["peers"].get(_peer_name(identity["hub"], principal))
        head = state["peer_records"][peer["history"][-1]] if peer else None
        current = head["digest"] if head else None
        confirmation = head["confirmation"] if head else {}
        acceptance = peer["acceptance"] if peer else {}
        retry = current == digest and acceptance and acceptance["head"] == (expected_head_digest or "")
        unresolved = peer and any(not change["resolved_by"] for change in peer["changes"].values())
        _require(current == expected_head_digest or retry, "mesh_peer_head_cas_mismatch")
        name = _peer_name(value["hub"], principal, value["kid"])
        retained = state["peer_records"].get(name)
        if retained is not None and (
            head is None or retained["digest"] != head["digest"] or retained["digest"] != digest
        ):
            result = _peer_refused(
                state, identity, principal, "mesh_peer_history_transition_unsupported", require_verified
            )
        elif retry or (current == digest and confirmation and not unresolved):
            _flush(descriptor)
            result = PeerAcceptanceResult(
                "replay", None, _peer_snapshot(state, identity, principal, require_verified), True
            )
        elif _barrier_key(value["hub"], value["kid"]) in state["barriers"]:
            result = _peer_refused(state, identity, principal, "recipient_key_revoked", require_verified)
        elif (
            head
            and current != digest
            and _instant(value["created_at"]) <= _instant(json.loads(head["canonical"])["created_at"])
        ):
            result = _peer_refused(
                state, identity, principal, "mesh_peer_history_transition_unsupported", require_verified
            )
        else:
            if current != digest or not confirmation:
                _admit_peer_times([parsed])
            peer = _peer_new(state, identity["hub"], principal)
            now = _peer_now()
            evidence = {"fingerprint": expected_fingerprint, "at": now, "head": expected_head_digest or ""}
            if current == digest:
                assert head is not None
                if not head["confirmation"]:
                    head["confirmation"] = evidence
            else:
                _peer_insert(
                    state,
                    peer,
                    parsed,
                    method="manual" if head else "first",
                    observation={},
                    confirmation=evidence,
                    now=now,
                )
            peer["acceptance"] = dict(evidence, digest=digest)
            for change in peer["changes"].values():
                if not change["resolved_by"]:
                    change["resolved_by"] = digest
            _admit_peers(state)
            _save(descriptor, state)
            result = PeerAcceptanceResult(
                "accepted", None, _peer_snapshot(state, identity, principal, require_verified), True
            )
    return result


def _peer_changed(
    descriptor: int,
    state: dict[str, Any],
    identity: dict[str, Any],
    principal: str,
    parsed: tuple[str, str, dict[str, Any]],
    require_verified: bool,
    supplied: list[tuple[str, str, dict[str, Any]]],
) -> PeerAcceptanceResult:
    before = deepcopy(state)
    digest, canonical, _ = parsed
    peer = state["peers"][_peer_name(identity["hub"], principal)]
    head = state["peer_records"][peer["history"][-1]]["digest"]
    name = _peer_name(head, digest)
    if name not in peer["changes"]:
        _admit_peer_times([parsed, *supplied])
    try:
        if name in peer["changes"]:
            _flush(descriptor)
        else:
            _require(len(peer["changes"]) < _MAX_PEER_CHANGES, "mesh_trust_capacity")
            peer["changes"][name] = {"canonical": canonical, "digest": digest, "head": head, "resolved_by": ""}
            _admit_peers(state)
            _save(descriptor, state)
    except crypto.MeshExtraMissing:
        raise
    except crypto.MeshError as error:
        if error.code == "mesh_backend_unsupported":
            raise
        return PeerAcceptanceResult(
            "changed",
            "recipient_key_changed",
            _peer_snapshot(before, identity, principal, require_verified),
            False,
            error.code,
        )
    return PeerAcceptanceResult(
        "changed", "recipient_key_changed", _peer_snapshot(state, identity, principal, require_verified), True
    )


def _peer_chain(
    state: dict[str, Any], peer: dict[str, Any], parsed: tuple[str, str, dict[str, Any]], chain: tuple[bytes, ...]
) -> list[tuple[str, str, dict[str, Any]]] | None:
    if len(chain) > 8 or _continuity_refusal(state, peer["history"][-1]) is not None:
        return None
    head = state["peer_records"][peer["history"][-1]]
    previous = json.loads(head["canonical"])
    links = []
    seen = set(peer["history"])
    try:
        for raw in chain or (parsed[1].encode("ascii"),):
            link = _peer_record(raw)
            value = link[2]
            reference = _peer_name(value["hub"], value["principal"], value["kid"])
            if (
                reference in seen
                or _barrier_key(previous["hub"], previous["kid"]) in state["barriers"]
                or _barrier_key(value["hub"], value["kid"]) in state["barriers"]
            ):
                return None
            crypto.verify_key_record(value, previous_record=previous)
            seen.add(reference)
            links.append(link)
            previous = value
        if links[-1][0] != parsed[0]:
            return None
    except crypto.MeshExtraMissing:
        raise
    except crypto.MeshError as error:
        if error.code == "mesh_backend_unsupported":
            raise
        return None
    return links


def observe_peer(
    *,
    brigade_home: Path,
    origin: str,
    principal: str,
    raw: bytes,
    chain: tuple[bytes, ...] = (),
    require_verified: bool,
) -> PeerAcceptanceResult:
    """Authenticate supplied bytes; chain contains successors only, at most eight.

    Nine authenticated successors diagnose changed. Longer tuples refuse before
    crypto. Exact completed retries reconcile retained continuity history.
    """
    _peer_parameters(principal, require_verified)
    _require(type(chain) is tuple, "mesh_peer_arguments_invalid")
    _require(len(chain) <= 9, "mesh_peer_chain_invalid")
    _require(all(isinstance(link, bytes) for link in chain), "mesh_peer_arguments_invalid")
    origin = statements.normalize_origin(origin)
    parsed = _peer_record(raw)
    digest, _, value = parsed
    # Authenticate every supplied link's self-signature before classifying a
    # continuity failure. Invalid input must not manufacture changed evidence.
    checked_chain = [_peer_record(link) for link in chain]
    result = None
    try:
        with _peer_directory(brigade_home) as descriptor:
            state, identity = _peer_state(descriptor, origin)
            _require((value["hub"], value["principal"]) == (identity["hub"], principal), "mesh_peer_scope_mismatch")
            _require(all(link[2]["hub"] == identity["hub"] for link in checked_chain), "mesh_peer_scope_mismatch")
            peer = state["peers"].get(_peer_name(identity["hub"], principal))
            head = state["peer_records"][peer["history"][-1]] if peer else None
            retained = state["peer_records"].get(_peer_name(value["hub"], principal, value["kid"]))
            if retained is not None and (
                head is None or retained["digest"] != head["digest"] or retained["digest"] != digest
            ):
                result = _peer_refused(
                    state, identity, principal, "mesh_peer_history_transition_unsupported", require_verified
                )
            elif head and head["digest"] != digest:
                assert peer is not None
                _require(not checked_chain or checked_chain[0][0] != head["digest"], "mesh_peer_chain_invalid")
                links = _peer_chain(state, peer, parsed, chain)
                if links is None:
                    result = _peer_changed(
                        descriptor, state, identity, principal, parsed, require_verified, checked_chain
                    )
                else:
                    _admit_peer_times(links)
                    for link in links:
                        _peer_insert(
                            state, peer, link, method="continuity", observation={}, confirmation={}, now=_peer_now()
                        )
                    peer["acceptance"] = {}
                    _admit_peers(state)
                    _save(descriptor, state)
                    result = PeerAcceptanceResult(
                        "accepted", None, _peer_snapshot(state, identity, principal, require_verified), True
                    )
            else:
                # A completed multi-link mutation is an exact suffix of saved
                # continuity history. Reflush that generation after a lost return.
                history = peer["history"] if peer else []
                suffix = history[-len(chain) :] if chain else []
                _require(
                    not chain
                    or (
                        1 <= len(chain) <= 8
                        and len(chain) < len(history)
                        and [state["peer_records"][ref]["digest"] for ref in suffix]
                        == [link[0] for link in checked_chain]
                        and all(state["peer_records"][ref]["method"] == "continuity" for ref in suffix)
                    ),
                    "mesh_peer_chain_invalid",
                )
                membership, membership_error = _membership(state, identity["hub"])
                observation = {}
                if not require_verified and not membership_error and membership and principal in membership["members"]:
                    observation = {"digest": membership["digest"], "signer": membership["signer"], "at": _peer_now()}
                if head is None:
                    _admit_peer_times([parsed])
                    peer = _peer_new(state, identity["hub"], principal)
                    _peer_insert(
                        state, peer, parsed, method="first", observation=observation, confirmation={}, now=_peer_now()
                    )
                    _admit_peers(state)
                    _save(descriptor, state)
                    disposition: Literal["accepted", "replay"] = "accepted"
                elif peer is not None and _provenance(state, peer["history"][-1]) == "unverified" and observation:
                    head["observation"] = observation
                    _admit_peers(state)
                    _save(descriptor, state)
                    disposition = "accepted"
                else:
                    _flush(descriptor)
                    disposition = "replay"
                result = PeerAcceptanceResult(
                    disposition, None, _peer_snapshot(state, identity, principal, require_verified), True
                )
    except crypto.MeshError as error:
        if (
            result is None
            or result.disposition != "changed"
            or error.code in {"mesh_backend_unsupported", "mesh_extra_missing"}
        ):
            raise
        return PeerAcceptanceResult(
            "changed", "recipient_key_changed", result.peer, False, result.storage_error or error.code
        )
    assert result is not None
    return result
