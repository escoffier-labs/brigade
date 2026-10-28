"""Offline, non-migrating endpoint identity evidence in one POSIX generation.

The unpublished v1 file is hub-identity-state.json in fleet/mesh-keys. Objects
map complete signed SHA256 digests to canonical signed text. Each origin keeps
its first/current digest, original acceptance time, latest observation time and
independently asserted confirmation times. The first digest/time is the sole
lineage entry: neither operator confirmation nor this store can migrate it.
Marks are global (hub-identity, full SHA256 signer), not per origin. Historical
origins remain valid storage but a globally stale selected origin is refused.

Limits fail closed without eviction: 256 origins, 1024 objects, 1024 floors,
1024 authenticated conflicts, 12 MiB ordinary/16 MiB total canonical bytes,
65536 directory names and a ten-second directory-inode lock wait. No load
repairs, initializes or flushes. Identical mutation retries flush without
changing generation, provenance or acceptance times. A lost return can follow a
complete generation; reload before retrying. Same-user rollback is outside this
boundary. No snapshot grants policy freshness or peer/message authorization.

Private coupling is deliberate: fleet_mesh_trust._directory, _locked and
_private supply the reviewed physical-path/owner-only/descriptor lifecycle and
shared custody directory lock; _instant validates timestamps. Their fixed
mesh_trust diagnostics are translated at this module's boundary. Atomic file
publication follows that reviewed lifecycle, without any trust/custody writes.
There are no runtime consumers, migration, keys, CLI, network or arbitrary save.
"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
from typing import Any, Iterator, Literal

from . import dirfd
from . import fleet_mesh_crypto as crypto
from . import fleet_mesh_statements as statements
from . import fleet_mesh_trust as trust

_NAME = "hub-identity-state.json"
_KIND = "hub-identity"
_MAX_BYTES = 16 * 1024 * 1024
_ORDINARY_BYTES = 12 * 1024 * 1024
_MAX_ORIGINS = 256
_MAX_OBJECTS = 1024
_MAX_FLOORS = 1024
_MAX_CONFLICTS = 1024
_MAX_DIRECTORY_ENTRIES = 65_536
_MAX_SEQUENCE = 2**53 - 1
_MAX_STRING = 32_768
_TABLES = {"origins", "objects", "marks", "floors", "conflicts"}
_HEX = re.compile(r"[0-9a-f]{64}", re.ASCII)


@dataclass(frozen=True)
class IdentitySnapshot:
    """Detached evidence; origin/identity/provenance are absent for floor writes.

    ``data`` is the complete persisted generation, including retained historical
    origins. Only ``identity`` is a selected, globally current origin's signed
    object. Unsatisfied floors are facts, not a combined policy-use verdict.
    First-entry lineage is ``origins[origin].first_digest/accepted_at``; later
    confirmations never rewrite that original observation.
    """

    data: dict[str, Any]
    origin: str | None
    identity: dict[str, Any] | None
    provenance: Literal["observed", "verified"] | None
    unsatisfied_floors: tuple[dict[str, Any], ...]
    policy_freshness: Literal["unproven"] = "unproven"


@dataclass(frozen=True)
class IdentityImportResult:
    """accepted/replay/signed_statement_conflict, with fixed evidence diagnosis.

    A conflict never changes an accepted head. Evidence-write failure still
    returns the authenticated refusal, with evidence_recorded false and a fixed
    storage_error. Cancellation and missing/unsupported crypto propagate.
    """

    status: str
    digest: str
    evidence_recorded: bool = False
    storage_error: str | None = None


def _require(condition: object, code: str = "mesh_identity_invalid") -> None:
    if not condition:
        raise crypto.MeshError(code)


def _platform() -> None:
    _require(os.name == "posix" and dirfd.posix_available(), "mesh_identity_platform_unsupported")


def _translate(error: crypto.MeshError) -> crypto.MeshError:
    if error.code == "mesh_trust_missing":
        return crypto.MeshError("hub_identity_unverified")
    if error.code.startswith("mesh_trust_"):
        return crypto.MeshError(error.code.replace("mesh_trust_", "mesh_identity_", 1))
    return error


@contextmanager
def _transaction(home: Path, *, create: bool = False) -> Iterator[int]:
    _platform()
    try:
        with trust._directory(home, create=create) as descriptor, trust._locked(descriptor):
            yield descriptor
    except crypto.MeshError as error:
        mapped = _translate(error)
        if mapped is error:
            raise
        raise mapped from None


def _fields(value: Any, names: set[str]) -> None:
    _require(type(value) is dict and value.keys() == names)


def _hex(value: Any) -> None:
    _require(isinstance(value, str) and _HEX.fullmatch(value))


def _seq(value: Any) -> None:
    _require(type(value) is int and 0 <= value <= _MAX_SEQUENCE)


def _hub(value: Any) -> None:
    _require(isinstance(value, str) and value.startswith("h_"))
    crypto.decode_binary(value[2:], 16)


def _floor(value: Any) -> None:
    _fields(value, {"kind", "signer", "hub", "seq", "digest"})
    _require(value["kind"] == _KIND, "statement_kind_unsupported")
    _hex(value["signer"])
    _hub(value["hub"])
    _seq(value["seq"])
    _hex(value["digest"])


def _stream(signer: str) -> str:
    return _KIND + ":" + signer


def _scope(signer: str, hub: str) -> str:
    return _stream(signer) + ":" + hub


def _evidence_id(value: dict[str, Any]) -> str:
    return hashlib.sha256(crypto.canonical(value)).hexdigest()


def _object(raw: bytes) -> tuple[str, str, dict[str, Any], str]:
    value = statements.parse_statement(raw, expected_kind=_KIND)
    public = crypto.decode_binary(value["hub_sig_pub"], 32)
    value = statements.verify_statement(raw, expected_kind=_KIND, public_key=public)
    canonical = crypto.canonical(value)
    return hashlib.sha256(canonical).hexdigest(), canonical.decode("ascii"), value, hashlib.sha256(public).hexdigest()


def _stored_object(digest: str, canonical: Any) -> tuple[dict[str, Any], str]:
    _hex(digest)
    _require(isinstance(canonical, str))
    actual, checked, value, signer = _object(canonical.encode("ascii"))
    _require(actual == digest and checked == canonical)
    return value, signer


def _tree(value: Any, depth: int = 0) -> None:
    if type(value) is str:
        _require(value.isascii() and len(value) <= _MAX_STRING)
    elif type(value) is int:
        _seq(value)
    elif type(value) is dict:
        _require(depth < 6 and len(value) <= max(_MAX_OBJECTS, _MAX_ORIGINS, _MAX_FLOORS, _MAX_CONFLICTS, 16))
        for key, item in value.items():
            _require(type(key) is str and key.isascii() and len(key) <= 2048)
            _tree(item, depth + 1)
    else:
        _require(False)


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
    raise crypto.MeshError("mesh_identity_invalid")


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
            _require(depth <= 6)
        elif char in (93, 125):
            depth -= 1
            _require(depth >= 0)
    _require(not quoted and depth == 0)


def _floor_evidence(digest: str, canonical: str, floor: dict[str, Any]) -> dict[str, Any]:
    return {"reason": "trusted_floor_contradiction", "digest": digest, "canonical": canonical, "floor": deepcopy(floor)}


def _validate(state: Any) -> dict[str, tuple[dict[str, Any], str]]:
    _tree(state)
    _fields(state, _TABLES | {"v", "generation"})
    _require(type(state["v"]) is int and state["v"] == 1)
    _seq(state["generation"])
    _require(state["generation"] >= 1)
    for table, cap in (
        ("origins", _MAX_ORIGINS),
        ("objects", _MAX_OBJECTS),
        ("marks", _MAX_OBJECTS),
        ("floors", _MAX_FLOORS),
        ("conflicts", _MAX_CONFLICTS),
    ):
        _require(type(state[table]) is dict and len(state[table]) <= cap)
    _require(len(crypto.canonical(dict(state, conflicts={}))) <= _ORDINARY_BYTES)
    values = {digest: _stored_object(digest, canonical) for digest, canonical in state["objects"].items()}
    marks: dict[str, Any] = {}
    histories: dict[str, list[tuple[int, str]]] = {}
    seen: set[tuple[str, int]] = set()
    for digest, (value, signer) in values.items():
        stream = _stream(signer)
        pair = (signer, value["seq"])
        _require(pair not in seen)
        seen.add(pair)
        if stream not in marks or marks[stream]["seq"] < value["seq"]:
            marks[stream] = {"seq": value["seq"], "digest": digest}
        histories.setdefault(value["endpoint"], []).append((value["seq"], digest))
    _require(state["marks"] == marks and state["origins"].keys() == histories.keys())
    for origin, entry in state["origins"].items():
        _require(statements.normalize_origin(origin) == origin)
        _fields(entry, {"first_digest", "current_digest", "accepted_at", "updated_at", "confirmations"})
        history = sorted(histories[origin])
        _require(entry["first_digest"] == history[0][1] and entry["current_digest"] == history[-1][1])
        first, first_signer = values[entry["first_digest"]]
        for _, digest in history:
            value, signer = values[digest]
            _require(
                signer == first_signer
                and value["hub_id"] == first["hub_id"]
                and value["hub_sig_pub"] == first["hub_sig_pub"]
            )
        trust._instant(entry["accepted_at"])
        trust._instant(entry["updated_at"])
        confirmations = entry["confirmations"]
        _require(type(confirmations) is dict and confirmations.keys() <= {"operator", "invite"})
        for instant in confirmations.values():
            trust._instant(instant)
    for name, floor in state["floors"].items():
        _floor(floor)
        _require(name == _scope(floor["signer"], floor["hub"]))
    for identity, evidence in state["conflicts"].items():
        _hex(identity)
        _require(type(evidence) is dict and identity == _evidence_id(evidence))
        reason = evidence.get("reason")
        if reason == "signer_equivocation":
            _fields(evidence, {"reason", "digest", "canonical", "accepted_digest"})
        else:
            _require(reason == "trusted_floor_contradiction")
            _fields(evidence, {"reason", "digest", "canonical", "floor"})
        rejected, signer = _stored_object(evidence["digest"], evidence["canonical"])
        if reason == "signer_equivocation":
            accepted, accepted_signer = values[evidence["accepted_digest"]]
            _require(
                signer == accepted_signer
                and rejected["seq"] == accepted["seq"]
                and evidence["digest"] != evidence["accepted_digest"]
            )
        else:
            floor = evidence["floor"]
            _floor(floor)
            _require(
                (floor["signer"], floor["hub"], floor["seq"]) == (signer, rejected["hub_id"], rejected["seq"])
                and floor["digest"] != evidence["digest"]
            )
            current = state["floors"][_scope(signer, rejected["hub_id"])]
            _require(current["seq"] > floor["seq"] or current == floor)
    # A later floor cannot erase contradiction of already retained history.
    for digest, (value, signer) in values.items():
        floor = state["floors"].get(_scope(signer, value["hub_id"]))
        if floor and floor["seq"] == value["seq"] and floor["digest"] != digest:
            evidence = _floor_evidence(digest, state["objects"][digest], floor)
            _require(state["conflicts"].get(_evidence_id(evidence)) == evidence)
    return values


def _read(descriptor: int) -> dict[str, Any]:
    source: int | None
    try:
        source = dirfd.open_child_file(descriptor, _NAME, dirfd.file_flags(os.O_RDONLY | os.O_NONBLOCK))
    except FileNotFoundError:
        raise crypto.MeshError("hub_identity_unverified") from None
    completed = False
    try:
        trust._private(source)
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
            raise crypto.MeshError("mesh_identity_invalid") from None
        except (ValueError, TypeError, KeyError, RecursionError, UnicodeError):
            raise crypto.MeshError("mesh_identity_invalid") from None
    finally:
        closing, source = source, None
        try:
            os.close(closing)
        except BaseException:
            if completed:
                raise


def _flush(descriptor: int) -> None:
    try:
        dirfd.fsync_directory(descriptor)
    except OSError:
        raise crypto.MeshError("mesh_identity_durability_indeterminate") from None


def _reap_temporaries(descriptor: int) -> None:
    names = dirfd.child_names(descriptor, _MAX_DIRECTORY_ENTRIES + 1)
    _require(len(names) <= _MAX_DIRECTORY_ENTRIES, "mesh_identity_capacity")
    removed = False
    completed = False
    try:
        for name in names:
            if not re.fullmatch(r"\.hub-identity-[0-9a-f]{32}\.tmp", name, re.ASCII):
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
        completed = True
    finally:
        if removed:
            try:
                dirfd.fsync_directory(descriptor)
            except BaseException:
                if completed:
                    raise


def _save(descriptor: int, state: dict[str, Any]) -> None:
    _require(state["generation"] < _MAX_SEQUENCE, "mesh_identity_capacity")
    state["generation"] += 1
    raw = crypto.canonical(state)
    _require(
        len(raw) <= _MAX_BYTES and len(crypto.canonical(dict(state, conflicts={}))) <= _ORDINARY_BYTES,
        "mesh_identity_capacity",
    )
    for table, cap in (
        ("origins", _MAX_ORIGINS),
        ("objects", _MAX_OBJECTS),
        ("marks", _MAX_OBJECTS),
        ("floors", _MAX_FLOORS),
        ("conflicts", _MAX_CONFLICTS),
    ):
        _require(len(state[table]) <= cap, "mesh_identity_capacity")
    _validate(state)
    temporary = ".hub-identity-" + secrets.token_hex(16) + ".tmp"
    destination = None
    created = False
    completed = False
    try:
        _reap_temporaries(descriptor)
        destination = dirfd.open_child_file(
            descriptor, temporary, dirfd.file_flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        )
        created = True
        trust._private(destination)
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
        # Ownership ends before the one close attempt, including consumed-close
        # errors/cancellation. Never publish a file whose close was ambiguous.
        closing, destination = destination, None
        os.close(closing)
        dirfd.replace_children(descriptor, temporary, _NAME)
        completed = True
    except OSError:
        raise crypto.MeshError("mesh_identity_storage_failure") from None
    finally:
        if destination is not None:
            closing, destination = destination, None
            try:
                os.close(closing)
            except BaseException:
                pass  # Keep the primary writer failure, including cancellation.
        if created:
            try:
                dirfd.unlink_child(descriptor, temporary)
            except FileNotFoundError:
                pass  # Only this attempt's temporary; it may already be gone.
            except OSError:
                if completed:
                    raise crypto.MeshError("mesh_identity_storage_failure") from None
            except BaseException:
                if completed:
                    raise
                # A primary writer error/cancellation already owns the diagnosis.
    _flush(descriptor)


def _selected(state: dict[str, Any], origin: str) -> tuple[dict[str, Any], dict[str, Any]]:
    _require(origin in state["origins"], "hub_identity_unverified")
    entry = state["origins"][origin]
    value = json.loads(state["objects"][entry["current_digest"]])
    signer = hashlib.sha256(crypto.decode_binary(value["hub_sig_pub"], 32)).hexdigest()
    _require(
        state["marks"][_stream(signer)] == {"seq": value["seq"], "digest": entry["current_digest"]},
        "signed_statement_rollback",
    )
    return entry, value


def _snapshot(state: dict[str, Any], origin: str | None = None) -> IdentitySnapshot:
    identity = None
    provenance: Literal["observed", "verified"] | None = None
    if origin is not None:
        entry, identity = _selected(state, origin)
        provenance = "verified" if entry["confirmations"] else "observed"
    heads: dict[str, tuple[int, str]] = {}
    for digest, canonical in state["objects"].items():
        value = json.loads(canonical)
        signer = hashlib.sha256(crypto.decode_binary(value["hub_sig_pub"], 32)).hexdigest()
        scope = _scope(signer, value["hub_id"])
        if scope not in heads or value["seq"] > heads[scope][0]:
            heads[scope] = (value["seq"], digest)
    unsatisfied = []
    for scope, floor in state["floors"].items():
        head = heads.get(scope)
        if head is None or head[0] < floor["seq"] or (head[0] == floor["seq"] and head[1] != floor["digest"]):
            unsatisfied.append(deepcopy(floor))
    return IdentitySnapshot(deepcopy(state), origin, deepcopy(identity), provenance, tuple(unsatisfied))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def initialize_identity_store(*, brigade_home: Path) -> None:
    """Explicit durable initialization or validation/flush, without adoption."""
    with _transaction(brigade_home, create=True) as descriptor:
        try:
            _read(descriptor)
        except crypto.MeshError as error:
            if error.code != "hub_identity_unverified":
                raise
            _save(descriptor, {"v": 1, "generation": 0, **{name: {} for name in _TABLES}})
        else:
            _flush(descriptor)


def load_identity(*, brigade_home: Path, origin: str) -> IdentitySnapshot:
    """Read-only selected evidence; refuse a selected origin below global mark."""
    _platform()
    origin = statements.normalize_origin(origin)
    with _transaction(brigade_home) as descriptor:
        return _snapshot(_read(descriptor), origin)


def _record_conflict(descriptor: int, state: dict[str, Any], evidence: dict[str, Any]) -> IdentityImportResult:
    identity = _evidence_id(evidence)
    try:
        if identity in state["conflicts"]:
            _flush(descriptor)
        else:
            state["conflicts"][identity] = evidence
            _save(descriptor, state)
    except crypto.MeshExtraMissing:
        raise
    except crypto.MeshError as error:
        if error.code == "mesh_backend_unsupported":
            raise
        return IdentityImportResult("signed_statement_conflict", evidence["digest"], False, _translate(error).code)
    return IdentityImportResult("signed_statement_conflict", evidence["digest"], True)


def observe_identity(*, brigade_home: Path, origin: str, raw: bytes) -> IdentityImportResult:
    """Authenticate exact endpoint before writes; accept only unchanged id/key.

    Bare self-signed bootstrap is observed, not independently verified. An
    independently configured new origin under a known signer needs a strictly
    higher global sequence. Authenticated conflicts persist without admission.
    """
    _platform()
    origin = statements.normalize_origin(origin)
    digest, canonical, value, signer = _object(raw)
    _require(value["endpoint"] == origin, "hub_endpoint_mismatch")
    result: IdentityImportResult | None = None
    try:
        with _transaction(brigade_home) as descriptor:
            state = _read(descriptor)
            entry = state["origins"].get(origin)
            if entry is not None:
                first = json.loads(state["objects"][entry["first_digest"]])
                _require(
                    (value["hub_id"], value["hub_sig_pub"]) == (first["hub_id"], first["hub_sig_pub"]),
                    "hub_identity_changed",
                )
                statements.verify_statement(
                    raw, expected_kind=_KIND, public_key=crypto.decode_binary(first["hub_sig_pub"], 32)
                )
            evidence = None
            for previous in state["conflicts"].values():
                if previous["digest"] == digest and previous["canonical"] == canonical:
                    evidence = previous
                    break
            if evidence is None:
                for accepted_digest, accepted_raw in state["objects"].items():
                    accepted = json.loads(accepted_raw)
                    if (
                        accepted["hub_sig_pub"] == value["hub_sig_pub"]
                        and accepted["seq"] == value["seq"]
                        and accepted_digest != digest
                    ):
                        evidence = {
                            "reason": "signer_equivocation",
                            "digest": digest,
                            "canonical": canonical,
                            "accepted_digest": accepted_digest,
                        }
                        break
            floor = state["floors"].get(_scope(signer, value["hub_id"]))
            if evidence is None and floor and value["seq"] == floor["seq"] and digest != floor["digest"]:
                evidence = _floor_evidence(digest, canonical, floor)
            if evidence is not None:
                result = _record_conflict(descriptor, state, evidence)
            else:
                if floor:
                    _require(value["seq"] >= floor["seq"], "signed_statement_rollback")
                mark = state["marks"].get(_stream(signer))
                if mark is not None:
                    _require(value["seq"] >= mark["seq"], "signed_statement_rollback")
                if mark is not None and value["seq"] == mark["seq"]:
                    _require(mark["digest"] == digest and entry is not None, "signed_statement_rollback")
                    _flush(descriptor)
                    result = IdentityImportResult("replay", digest)
                else:
                    state["objects"][digest] = canonical
                    state["marks"][_stream(signer)] = {"seq": value["seq"], "digest": digest}
                    now = _now()
                    if entry is None:
                        entry = {"first_digest": digest, "accepted_at": now, "confirmations": {}}
                        state["origins"][origin] = entry
                    entry.update(current_digest=digest, updated_at=now)
                    _save(descriptor, state)
                    result = IdentityImportResult("accepted", digest)
    except crypto.MeshError as error:
        if result is None or result.status != "signed_statement_conflict" or error.code == "mesh_backend_unsupported":
            raise
        # A directory-close/unlock failure after conflict classification must
        # not hide the refusal or claim successfully flushed evidence.
        return IdentityImportResult(result.status, digest, False, result.storage_error or error.code)
    assert result is not None
    return result


def confirm_identity(
    *, brigade_home: Path, origin: str, expected_hub_id: str, source: Literal["operator", "invite"]
) -> IdentitySnapshot:
    """Caller-asserted independent confirmation of an existing unchanged id.

    Invite parsing/authentication belongs to the caller. No response field can
    call this implicitly. This never replaces identity or proves current policy.
    """
    _platform()
    origin = statements.normalize_origin(origin)
    _hub(expected_hub_id)
    _require(source in ("operator", "invite"), "mesh_identity_confirmation_invalid")
    with _transaction(brigade_home) as descriptor:
        state = _read(descriptor)
        _require(origin in state["origins"], "hub_identity_unverified")
        entry = state["origins"][origin]
        value = json.loads(state["objects"][entry["current_digest"]])
        _require(value["hub_id"] == expected_hub_id, "hub_identity_changed")
        _selected(state, origin)
        if source in entry["confirmations"]:
            _flush(descriptor)
        else:
            entry["confirmations"][source] = _now()
            _save(descriptor, state)
        return _snapshot(state, origin)


def require_identity_floor(
    *, brigade_home: Path, kind: str, signer: str, hub: str, seq: int, digest: str
) -> IdentitySnapshot:
    """Install an independent monotone floor plus same-generation contradictions."""
    _platform()
    value = {"kind": kind, "signer": signer, "hub": hub, "seq": seq, "digest": digest}
    _floor(value)
    name = _scope(signer, hub)
    with _transaction(brigade_home) as descriptor:
        state = _read(descriptor)
        previous = state["floors"].get(name)
        changed = previous != value
        _require(not changed or previous is None or seq > previous["seq"], "mesh_identity_floor_not_tightened")
        state["floors"][name] = value
        for accepted_digest, canonical in state["objects"].items():
            accepted = json.loads(canonical)
            accepted_signer = hashlib.sha256(crypto.decode_binary(accepted["hub_sig_pub"], 32)).hexdigest()
            if (accepted_signer, accepted["hub_id"], accepted["seq"]) == (
                signer,
                hub,
                seq,
            ) and accepted_digest != digest:
                evidence = _floor_evidence(accepted_digest, canonical, value)
                identity = _evidence_id(evidence)
                if identity not in state["conflicts"]:
                    state["conflicts"][identity] = evidence
                    changed = True
        if changed:
            _save(descriptor, state)
        else:
            _flush(descriptor)
        return _snapshot(state)
