"""Explicit signed Hub identity and public-key transport.

Publication requires a custody generation whose explicit offline create, rotate,
or durability reconciliation completed successfully. A read-only reload cannot
certify an earlier ambiguous local fsync. HTTP response loss can be reconciled;
local custody write loss must first use the existing offline workflow.

There is no network/local transaction. Observable drift refuses success, without
compensation. Statement imports can leave earlier durable imports on failure.
No local store operation runs inside an abandonable network worker.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
import http.client
import hashlib
import math
from pathlib import Path
import re
import time
from typing import Any, Literal
import urllib.error
import urllib.request

from . import fleet_client
from . import fleet_mesh_crypto as crypto
from . import fleet_mesh_hub_identity as identity
from . import fleet_mesh_keys as keys
from . import fleet_mesh_statements as statements
from . import fleet_mesh_trust as trust
from .fleet_client_message_credentials import MessageAuthority

Effect = Literal["none", "possible", "confirmed"]
Disposition = Literal["published", "already_active", "reconciled"]
_CODES = frozenset(
    {
        "mesh_transport_timeout",
        "mesh_transport_authority",
        "mesh_transport_endpoint",
        "mesh_transport_response",
        "mesh_transport_unavailable",
        "mesh_transport_identity",
        "mesh_transport_principal",
        "mesh_transport_anchor",
        "mesh_transport_drift",
        "mesh_transport_key",
        "mesh_transport_local",
        "mesh_transport_partial_import",
    }
)
_SMALL = 64 * 1024
_BUNDLE = 8 * 1024 * 1024
_ORDER = ("membership", "manifest", "revocation")


class MeshTransportError(fleet_client.FleetClientError):
    """Fixed diagnosis, with a conservative remote publication effect."""

    def __init__(self, code: str, *, remote_effect: Effect = "none") -> None:
        self.code = code if code in _CODES else "mesh_transport_local"
        self.remote_effect = remote_effect
        super().__init__(self.code)


@dataclass(frozen=True)
class PublishedKey:
    """Detached public record only; no authority or custody material."""

    principal: str
    hub_id: str
    kid: str
    record: dict[str, Any]
    disposition: Disposition


class _Budget:
    def __init__(self, timeout: float) -> None:
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise MeshTransportError("mesh_transport_timeout")
        self.end = time.monotonic() + timeout

    def remaining(self) -> float:
        remaining = self.end - time.monotonic()
        if remaining <= 0:
            raise MeshTransportError("mesh_transport_timeout")
        return remaining


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _constant(value: str) -> Any:
    raise ValueError


def _decode(raw: bytes) -> dict[str, Any]:
    try:
        # Bound nesting before the decoder allocates a recursive object tree.
        depth, quoted, escaped = 0, False, False
        for char in raw:
            if quoted:
                if escaped:
                    escaped = False
                elif char == 92:
                    escaped = True
                elif char == 34:
                    quoted = False
            elif char == 34:
                quoted = True
            elif char in (91, 123):
                depth += 1
                if depth > 16:
                    raise ValueError
            elif char in (93, 125):
                depth -= 1
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
        if not isinstance(value, dict):
            raise ValueError

        def finite(item: Any) -> None:
            if isinstance(item, float) and not math.isfinite(item):
                raise ValueError
            if isinstance(item, dict):
                for child in item.values():
                    finite(child)
            elif isinstance(item, list):
                for child in item:
                    finite(child)

        finite(value)
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise MeshTransportError("mesh_transport_response") from None


def _wire(*, origin: str, token: str | None, body: bytes | None, timeout: float, limit: int) -> tuple[int, bytes]:
    """Network only: immutable inputs, bounded streams, no stores or callbacks."""
    headers = {"Content-Type": "application/json"} if body is not None else {}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(
        origin + ("/mesh/identity" if body is None else "/messages"), data=body, headers=headers
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        response = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        if response.headers.get("Content-Encoding", "identity") != "identity":
            raise MeshTransportError("mesh_transport_response")
        raw = fleet_client._read_bounded_response(response, limit=limit if response.status < 400 else _SMALL)
        return response.status, raw


class _Operation:
    def __init__(self, home: Path, authority: MessageAuthority, timeout: float, *, authenticated: bool) -> None:
        self.effect: Effect = "none"
        self.budget = _Budget(timeout)
        self.home, self.authority = home, authority
        self.authenticated = authenticated
        self.local_keys: keys.LocalKeys | None = None
        self.principal: str | None = None
        self.trusted: trust.TrustSnapshot | None = None
        try:
            if not isinstance(authority, MessageAuthority) or not (
                authority.node_credential or authority.token_file is not None
            ):
                raise MeshTransportError("mesh_transport_authority")
            authority.validate()
            self.origin = statements.normalize_origin(authority.hub)
            fleet_client._require_encrypted_or_loopback_hub(self.origin)
        except MeshTransportError:
            raise
        except (ValueError, fleet_client.FleetClientError):
            raise MeshTransportError("mesh_transport_authority") from None
        crypto.probe_backend()
        self.selected = self.load_identity()
        if authenticated:
            if (
                self.selected.identity is None
                or self.selected.provenance != "verified"
                or self.selected.unsatisfied_floors
            ):
                raise MeshTransportError("mesh_transport_identity")
            self.trusted = trust.load_trust(brigade_home=home)
            if self.trusted.unsatisfied_floors:
                raise MeshTransportError("mesh_transport_anchor")

    def load_identity(self) -> identity.IdentitySnapshot:
        if self.authenticated:
            return identity.load_identity(brigade_home=self.home, origin=self.origin)
        # The public loader requires a previously selected origin. First
        # observation instead needs the existing store generation without
        # selection. Reuse its read-only locked snapshot primitives, never
        # treating hub_identity_unverified as proof of an initialized store.
        with identity._transaction(self.home) as descriptor:
            return identity._snapshot(identity._read(descriptor))

    @property
    def hub(self) -> str:
        assert self.selected.identity is not None
        return str(self.selected.identity["hub_id"])

    def check(self) -> None:
        try:
            self.authority.validate()
            if statements.normalize_origin(self.authority.hub) != self.origin:
                raise ValueError
            if self.load_identity() != self.selected:
                raise ValueError
            if self.trusted is not None and trust.load_trust(brigade_home=self.home) != self.trusted:
                raise ValueError
            if self.local_keys is not None:
                current = keys.load_local_keys(
                    brigade_home=self.home, principal=self.local_keys.principal, hub_id=self.hub, create=False
                )
                if current != self.local_keys:
                    raise ValueError
        except (ValueError, fleet_client.FleetClientError):
            raise MeshTransportError("mesh_transport_drift", remote_effect=self.effect) from None

    def request(
        self, body: dict[str, Any] | None, *, limit: int = _SMALL, publish: bool = False
    ) -> tuple[int, dict[str, Any], bytes]:
        self.check()
        try:
            remaining = self.budget.remaining()
        except MeshTransportError:
            raise MeshTransportError("mesh_transport_timeout", remote_effect=self.effect) from None
        # Only these immutable values are captured by the daemon worker.
        origin, token = self.origin, self.authority.token if body is not None else None
        encoded = crypto.canonical(body) if body is not None else None
        if publish:
            self.effect = "possible"
        try:
            status, raw = fleet_client._run_with_deadline(
                lambda: _wire(origin=origin, token=token, body=encoded, timeout=remaining, limit=limit),
                timeout=remaining,
            )
            if publish and status == 201:
                self.effect = "confirmed"
            self.check()
            self.budget.remaining()
            return status, _decode(raw), raw
        except MeshTransportError as error:
            raise MeshTransportError(error.code, remote_effect=self.effect) from None
        except TimeoutError:
            raise MeshTransportError("mesh_transport_timeout", remote_effect=self.effect) from None
        except (OSError, ValueError, http.client.HTTPException, fleet_client.FleetClientError):
            raise MeshTransportError("mesh_transport_unavailable", remote_effect=self.effect) from None

    def authenticate(self, expected: str) -> None:
        status, value, _ = self.request({"action": "identity"})
        if (
            status != 200
            or set(value) != {"principal"}
            or not isinstance(value["principal"], str)
            or re.fullmatch(r"(?:[A-Za-z0-9][A-Za-z0-9._-]{0,127}|agent:[a-z][a-z0-9._-]{0,62})", value["principal"])
            is None
            or value["principal"] == "unknown"
            or value["principal"] != expected
        ):
            raise MeshTransportError("mesh_transport_principal", remote_effect=self.effect)
        self.principal = value["principal"]

    def active_entry(self, value: dict[str, Any]) -> None:
        assert self.local_keys is not None
        record = self.local_keys.active.record
        expected = {
            "principal_id": self.principal,
            "kid": record["kid"],
            "record": record,
            "state": "active",
            "created_at": record["created_at"],
            "retired_at": None,
            "revoked_at": None,
            "compromised_from": None,
        }
        if set(value) != {"key"} or value["key"] != expected:
            raise MeshTransportError("mesh_transport_key", remote_effect=self.effect)
        try:
            crypto.verify_key_record(value["key"]["record"], principal=self.principal, hub=self.hub)
        except crypto.MeshError:
            raise MeshTransportError("mesh_transport_key", remote_effect=self.effect) from None


def _observation_transition(
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    origin: str,
    value: dict[str, Any],
    result: identity.IdentityImportResult,
) -> None:
    """Refuse unrelated concurrent identity writes after our own admission."""
    expected = deepcopy(before)
    if result.status == "accepted":
        digest = result.digest
        expected["objects"][digest] = crypto.canonical(value).decode("ascii")
        entry = after["origins"][origin]
        previous = before["origins"].get(origin)
        if previous is None:
            previous = {"first_digest": digest, "accepted_at": entry["updated_at"], "confirmations": {}}
        expected["origins"][origin] = dict(previous, current_digest=digest, updated_at=entry["updated_at"])
        signer = hashlib.sha256(crypto.decode_binary(value["hub_sig_pub"], 32)).hexdigest()
        expected["marks"]["hub-identity:" + signer] = {"seq": value["seq"], "digest": digest}
        expected["generation"] += 1
    elif result.status == "signed_statement_conflict":
        for key, evidence in after["conflicts"].items():
            if key not in before["conflicts"] and evidence["digest"] == result.digest:
                expected["conflicts"][key] = evidence
        if expected["conflicts"] != before["conflicts"]:
            expected["generation"] += 1
    if expected != after:
        raise MeshTransportError("mesh_transport_drift")


def observe_hub_identity(
    *, brigade_home: Path, authority: MessageAuthority, timeout: float = 12.0
) -> identity.IdentityImportResult:
    """Observe signed evidence in a preinitialized store; never confirm it."""
    op = _Operation(brigade_home, authority, timeout, authenticated=False)
    status, value, raw = op.request(None)
    if status != 200 or "identity" in value or "migration" in value:
        raise MeshTransportError("mesh_transport_response")
    before = op.selected.data
    result = identity.observe_identity(brigade_home=brigade_home, origin=op.origin, raw=raw)
    op.selected = op.load_identity()
    _observation_transition(before, op.selected.data, origin=op.origin, value=value, result=result)
    op.check()
    return result


def import_hub_statements(
    *, brigade_home: Path, authority: MessageAuthority, expected_principal: str, timeout: float = 12.0
) -> dict[str, trust.ImportResult]:
    """Prevalidate the complete bundle, then import in fixed order.

    Conflicts are returned honestly. Later failures preserve earlier imports;
    retries use the stores' existing idempotent replay behavior.
    """
    op = _Operation(brigade_home, authority, timeout, authenticated=True)
    assert op.trusted is not None
    data = op.trusted.data
    if op.hub not in data["hubs"]:
        raise MeshTransportError("mesh_transport_anchor")
    signer = data["hubs"][op.hub][-1]
    public = crypto.decode_binary(data["anchors"][signer]["public_key"], 32)
    op.authenticate(expected_principal)
    status, bundle, _ = op.request({"action": "manifests_get"}, limit=_BUNDLE)
    if (
        status != 200
        or set(bundle) != {"statements"}
        or not isinstance(bundle["statements"], dict)
        or not bundle["statements"].keys() <= set(_ORDER)
    ):
        raise MeshTransportError("mesh_transport_response")
    prepared = {}
    for kind in _ORDER:
        if kind in bundle["statements"]:
            raw = crypto.canonical(bundle["statements"][kind])
            value = statements.verify_statement(raw, expected_kind=kind, public_key=public)
            if value["hub"] != op.hub:
                raise MeshTransportError("mesh_transport_anchor")
            prepared[kind] = raw
    op.authenticate(expected_principal)
    results = {}
    for kind, raw in prepared.items():
        op.check()
        try:
            results[kind] = trust.import_statement(brigade_home=brigade_home, hub=op.hub, kind=kind, raw=raw)
        except (ValueError, OSError):
            raise MeshTransportError("mesh_transport_partial_import") from None
        # Each store owns admission under its own lock. Accept only the intended
        # import transition; unrelated state must not be silently rebased.
        updated = trust.load_trust(brigade_home=brigade_home)
        if (
            updated.data["hubs"] != data["hubs"]
            or updated.data["anchors"] != data["anchors"]
            or updated.data["floors"] != data["floors"]
        ):
            raise MeshTransportError("mesh_transport_drift")
        previous = op.trusted.data
        digest = hashlib.sha256(raw).hexdigest()
        old_objects, new_objects = previous["objects"], updated.data["objects"]
        derived = {"generation", "objects", "marks", "baselines", "barriers", "binding_conflicts", "conflicts"}
        if (
            updated.data.keys() != previous.keys()
            or any(updated.data[name] != previous[name] for name in previous.keys() - derived)
            or any(
                evidence.get("digest") != digest
                for name, evidence in updated.data["conflicts"].items()
                if name not in previous["conflicts"]
            )
            or not new_objects.keys() <= old_objects.keys() | {digest}
            or any(new_objects.get(key) != obj for key, obj in old_objects.items())
            or any(updated.data["conflicts"].get(key) != obj for key, obj in previous["conflicts"].items())
            or updated.data.get("hub_revocations") != previous.get("hub_revocations")
        ):
            raise MeshTransportError("mesh_transport_drift")
        op.trusted = updated
        op.check()
    op.authenticate(expected_principal)
    return results


def publish_active_key(
    *, brigade_home: Path, authority: MessageAuthority, expected_principal: str, timeout: float = 12.0
) -> PublishedKey:
    """Publish only previously durable custody, once; reconcile by exact readback."""
    op = _Operation(brigade_home, authority, timeout, authenticated=True)
    op.authenticate(expected_principal)
    op.local_keys = keys.load_local_keys(
        brigade_home=brigade_home, principal=expected_principal, hub_id=op.hub, create=False
    )
    record = op.local_keys.active.record
    assert op.trusted is not None
    if op.local_keys.active.retired_at is not None or op.hub + ":" + record["kid"] in op.trusted.data["barriers"]:
        raise MeshTransportError("mesh_transport_key")
    lookup = {"action": "keys_get", "kid": record["kid"]}
    status, value, _ = op.request(lookup)
    disposition: Disposition = "already_active"
    if status == 200:
        op.active_entry(value)
    elif status == 404 and value == {"error": "key not found"}:
        op.authenticate(expected_principal)
        try:
            status, value, _ = op.request({"action": "keys_publish", "record": record}, publish=True)
            if status == 201:
                op.active_entry(value)
                disposition = "published"
            elif status == 409:
                raise MeshTransportError("mesh_transport_key", remote_effect=op.effect)
            else:
                raise MeshTransportError("mesh_transport_response", remote_effect=op.effect)
        except MeshTransportError:
            # Never repeat publication, including when a timed-out worker can
            # still commit after a missing reconciliation read.
            op.check()
            status, value, _ = op.request(lookup)
            if status != 200:
                raise MeshTransportError("mesh_transport_key", remote_effect=op.effect) from None
            op.active_entry(value)
            op.effect = "confirmed"
            disposition = "reconciled"
    else:
        raise MeshTransportError("mesh_transport_response")
    op.authenticate(expected_principal)
    op.check()
    return PublishedKey(expected_principal, op.hub, record["kid"], deepcopy(record), disposition)
