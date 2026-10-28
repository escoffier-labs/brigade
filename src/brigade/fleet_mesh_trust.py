"""Local revision-5 trust evidence, with one atomic POSIX snapshot.

Callers supply an already authenticated hub id. Only explicit initialization
creates state; no operation repairs unsafe or corrupt state. Anchor selection is
explicit CAS per hub. Membership/revocation streams use global (kind, full
SHA256 public-key hash) marks. Other statement kinds are deliberately refused.

Snapshots retain canonical complete signed objects and all anchor transitions.
Membership under a replaced anchor is historical evidence only. This module
makes no peer authorization or current-membership-use decision. Floors expose
unsatisfied facts without erasing history or reconciling replacement policy.
Signed cumulative baselines are distinct from source-attributed effective
revocation barriers, which unsigned authenticated Hub allegations can tighten.
Principal names are attributed claims, not ownership. Future peer use must
consult the (hub, kid) barrier without filtering by principal. No use API exists
here. The unpublished v1 representation retains unsigned minima per principal.

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
from datetime import datetime
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import time
from typing import Any, Iterator

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

    ``data`` is the exact version-1 persisted schema. Its dicts and lists are
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
    _fields(state, _TABLES | {"v", "generation"})
    _require(type(state["v"]) is int and state["v"] == 1)
    _seq(state["generation"])
    _require(state["generation"] >= 1)
    for name in _TABLES:
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


def _snapshot(state: dict[str, Any]) -> TrustSnapshot:
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
            unsatisfied.append(deepcopy(floor))
    return TrustSnapshot(deepcopy(state), tuple(unsatisfied))


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
    _require(all(len(state[name]) <= _MAX_ENTRIES for name in _TABLES), "mesh_trust_capacity")
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
