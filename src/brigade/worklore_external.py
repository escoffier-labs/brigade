"""Inactive, pure external-lease candidate over explicitly supplied observations.

Parsing establishes neither authentication nor currentness. No lookup, registry,
event history, acquisition, scope conflict scan or runtime registration lives
here. Future integration must derive authority on the server and revalidate it
under the existing write fence. Proposed snapshots are never live claims.
"""

from __future__ import annotations

import hmac
import json
import re
from dataclasses import dataclass, field, fields, replace
from datetime import datetime, timedelta
from typing import Any, Callable, Literal, TypeVar

from . import worklore_ownership as ownership
from .worklore_validate import WorkloreValidationError

T = TypeVar("T")
_AGENT_RE = re.compile(r"agent:[a-z][a-z0-9._-]{0,62}")
_ACTIONS = frozenset({"register", "read", "renew", "release", "report"})
_EPOCH_RE = re.compile(r"[A-Za-z0-9_-]{16,128}")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")


class ModelError(ValueError):
    """Fixed, nonreflecting refusal; codes describe model checks only."""

    def __init__(self, code: str = "field-bound") -> None:
        super().__init__("external model input refused")
        self.code = code


def _checked(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    # Reuse canonical Worklore value rules without leaking parser internals.
    try:
        return fn(*args, **kwargs)
    except WorkloreValidationError as error:
        raise ModelError(error.code) from None


def _integer(value: object, *, minimum: int = 0, maximum: int = ownership.COUNTER_MAX) -> int:
    return _checked(ownership._integer, value, minimum=minimum, maximum=maximum)


def _reference(value: object) -> str:
    return _checked(ownership._reference, value)


def _repo(value: object) -> str:
    text = _checked(ownership._path, value, "repo_identity", ownership.REPO_IDENTITY_MAX)
    if not ownership._REPO_RE.fullmatch(text):
        raise ModelError()
    return text.casefold()


def _paths(value: object) -> tuple[str, ...]:
    raw = _checked(ownership._array, value, ownership.SCOPE_MAX_ITEMS)
    paths = tuple(_checked(ownership._path, path) for path in raw)
    if not paths or len(set(paths)) != len(paths):
        raise ModelError()
    return tuple(sorted(paths))


def _clock(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ModelError()
    return value


def _epoch(value: object) -> str:
    if not isinstance(value, str) or not _EPOCH_RE.fullmatch(value):
        raise ModelError()
    return value


@dataclass(frozen=True, eq=False)
class CredentialBinding:
    """Private observation of a credential digest, never a public epoch."""

    _value: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self._value, str) or not _DIGEST_RE.fullmatch(self._value):
            raise ModelError()

    def __eq__(self, other: object) -> bool:
        return isinstance(other, CredentialBinding) and hmac.compare_digest(self._value, other._value)


@dataclass(frozen=True)
class PrincipalRef:
    """Typed canonical identity, independent of enrollment and credential epochs."""

    kind: Literal["agent", "node"]
    id: str

    def __post_init__(self) -> None:
        if self.kind == "agent":
            text = _checked(ownership._text, self.id, "principal", 69)
            if not _AGENT_RE.fullmatch(text):
                raise ModelError()
        elif self.kind == "node":
            _checked(ownership._node, self.id)
        else:
            raise ModelError()


@dataclass(frozen=True)
class Admission:
    """An observed one-binding offer, not proof of an operator's permission."""

    protocol_version: int
    admission_ref: str
    work_id: str
    generation: int
    principal: PrincipalRef
    enrollment_id: str
    authorization_enrollment_epoch: str
    repo_identity: str
    allowed_paths: tuple[str, ...]
    max_lease_seconds: int
    actions: tuple[str, ...]
    authorization_ref: str
    approval_ref: str | None
    campaign_member: bool

    def __post_init__(self) -> None:
        _integer(self.protocol_version, minimum=1, maximum=1)
        for value in (self.admission_ref, self.work_id, self.enrollment_id, self.authorization_ref):
            _reference(value)
        if self.approval_ref is not None:
            _reference(self.approval_ref)
        _integer(self.generation)
        _epoch(self.authorization_enrollment_epoch)
        # Candidate bounds only. These constants activate no lease policy.
        _integer(self.max_lease_seconds, minimum=60, maximum=3600)
        if not isinstance(self.principal, PrincipalRef) or self.principal.kind != "agent":
            raise ModelError("principal-forbidden")
        if type(self.campaign_member) is not bool:
            raise ModelError()
        if self.campaign_member:
            raise ModelError("campaign-ineligible")
        if _repo(self.repo_identity) != self.repo_identity:
            raise ModelError()
        if type(self.allowed_paths) is not tuple or _paths(list(self.allowed_paths)) != self.allowed_paths:
            raise ModelError()
        if (
            type(self.actions) is not tuple
            or not 1 <= len(self.actions) <= len(_ACTIONS)
            or any(not isinstance(action, str) or action not in _ACTIONS for action in self.actions)
            or len(set(self.actions)) != len(self.actions)
            or "register" not in self.actions
        ):
            raise ModelError()


@dataclass(frozen=True)
class Registration:
    """Bounded reported metadata; task/thread identity is not authenticated."""

    protocol_version: int
    admission_ref: str
    work_id: str
    generation: int
    provider: str
    task_id: str
    thread_id: str
    repo_identity: str
    write_scope: tuple[str, ...]
    source_revision: str

    def __post_init__(self) -> None:
        _integer(self.protocol_version, minimum=1, maximum=1)
        _integer(self.generation)
        for value in (self.admission_ref, self.work_id):
            _reference(value)
        for value in (self.provider, self.task_id, self.thread_id):
            _checked(ownership._text, value, "external_binding", 128)
        if _repo(self.repo_identity) != self.repo_identity:
            raise ModelError()
        if type(self.write_scope) is not tuple or _paths(list(self.write_scope)) != self.write_scope:
            raise ModelError()
        _checked(ownership._source_revision, self.source_revision)


def parse_admission(raw: object) -> Admission:
    """Parse an exact v1 offer shape without establishing its authority."""
    body = _checked(ownership._object, raw, {field.name for field in fields(Admission)})
    principal = _checked(ownership._object, body["principal"], {"kind", "id"})
    body["principal"] = PrincipalRef(**principal)
    body["repo_identity"] = _repo(body["repo_identity"])
    body["allowed_paths"] = _paths(body["allowed_paths"])
    body["actions"] = tuple(_checked(ownership._array, body["actions"], len(_ACTIONS)))
    return Admission(**body)


def parse_registration(raw: object) -> Registration:
    """Parse metadata only; caller/admin fields are forbidden."""
    body = _checked(ownership._object, raw, {field.name for field in fields(Registration)})
    body["repo_identity"] = _repo(body["repo_identity"])
    body["write_scope"] = _paths(body["write_scope"])
    return Registration(**body)


def scope_within_admission(admission: Admission, registration: Registration) -> bool:
    """Require every requested path to equal or descend from an allowed root."""
    return admission.repo_identity == registration.repo_identity and all(
        any(path == root or path.startswith(root + "/") for root in admission.allowed_paths)
        for path in registration.write_scope
    )


def validate_binding(
    admission: Admission, registration: Registration, *, pinned: Registration | None = None
) -> Registration:
    """Return the first immutable binding; a changed head does not rewrite it.

    Later head/report observations need separate history in held runtime work.
    No offer is consumed or ownership acquired by this check.
    """
    if registration.generation != admission.generation:
        raise ModelError("stale-generation")
    if (
        registration.admission_ref != admission.admission_ref
        or registration.work_id != admission.work_id
        or registration.repo_identity != admission.repo_identity
    ):
        raise ModelError("admission-mismatch")
    if not scope_within_admission(admission, registration):
        raise ModelError("scope-forbidden")
    if pinned is not None:
        validate_binding(admission, pinned)
        if replace(registration, source_revision=pinned.source_revision) != pinned:
            raise ModelError("binding-conflict")
        return pinned
    return registration


@dataclass(frozen=True)
class AuthorityObservation:
    """Explicit supplied observations, never body-derived authentication.

    Private credential bindings compare within their own domain. Unrelated
    grant/key observations establish no Worklore permission.
    """

    principal: PrincipalRef
    enrollment_id: str
    authorization_enrollment_epoch: str
    current_credential_generation: CredentialBinding
    authenticated_credential_generation: CredentialBinding
    messaging_grant_epoch: str | None
    mesh_key_epoch: int | None
    activity: Literal["active", "inactive", "revoked"]

    def __post_init__(self) -> None:
        if not isinstance(self.principal, PrincipalRef) or self.activity not in ("active", "inactive", "revoked"):
            raise ModelError()
        _reference(self.enrollment_id)
        _epoch(self.authorization_enrollment_epoch)
        if not isinstance(self.current_credential_generation, CredentialBinding) or not isinstance(
            self.authenticated_credential_generation, CredentialBinding
        ):
            raise ModelError()
        if self.messaging_grant_epoch is not None:
            _epoch(self.messaging_grant_epoch)
        if self.mesh_key_epoch is not None:
            _integer(self.mesh_key_epoch)


def parse_report(raw: object, *, repo_identity: str) -> dict[str, Any]:
    """Validate bounded reported metadata, without resolving refs or assessing truth."""
    body = _checked(ownership._object, raw, {"version", "branch", "head_commit", "issues", "pull_requests"})
    _integer(body["version"], minimum=1, maximum=1)
    body["branch"] = _checked(ownership._text, body["branch"], "branch", 128)
    body["head_commit"] = _checked(ownership._source_revision, body["head_commit"])
    for name in ("issues", "pull_requests"):
        refs = []
        for raw_ref in _checked(ownership._array, body[name], 20):
            ref = _checked(ownership._object, raw_ref, {"repo_identity", "number"})
            repo = _repo(ref["repo_identity"])
            if repo != repo_identity:
                raise ModelError("admission-mismatch")
            refs.append({"repo_identity": repo, "number": _integer(ref["number"], minimum=1)})
        body[name] = refs
    if len(body["issues"]) + len(body["pull_requests"]) > 20 or len(json.dumps(body).encode()) > 8192:
        raise ModelError()
    return body


@dataclass(frozen=True)
class LeaseSnapshot:
    """A proposed immutable v1 lease snapshot, not a live reservation."""

    admission: Admission
    binding: Registration
    revision: int
    state: Literal["active", "released"]
    heartbeat: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.admission, Admission) or not isinstance(self.binding, Registration):
            raise ModelError()
        validate_binding(self.admission, self.binding)
        _integer(self.revision)
        if self.state not in ("active", "released"):
            raise ModelError()
        duration = (_clock(self.expires_at) - _clock(self.heartbeat)).total_seconds()
        if not 0 <= duration <= self.admission.max_lease_seconds:
            raise ModelError()


@dataclass(frozen=True)
class LeaseRequest:
    action: Literal["renew", "release"]
    request_key: str
    generation: int
    expected_revision: int
    lease_seconds: int

    def __post_init__(self) -> None:
        if self.action not in ("renew", "release"):
            raise ModelError()
        _checked(ownership._text, self.request_key, "request_key", ownership.IDEMPOTENCY_KEY_MAX)
        _integer(self.generation)
        _integer(self.expected_revision)
        _integer(self.lease_seconds, maximum=3600)
        if (self.action == "release" and self.lease_seconds != 0) or (
            self.action == "renew" and self.lease_seconds < 60
        ):
            raise ModelError()


@dataclass(frozen=True)
class HistoricalResult:
    """Caller-supplied immutable historical observation, not an event store."""

    request: LeaseRequest
    snapshot: LeaseSnapshot

    def __post_init__(self) -> None:
        if not isinstance(self.request, LeaseRequest) or not isinstance(self.snapshot, LeaseSnapshot):
            raise ModelError()
        if (
            self.snapshot.revision != self.request.expected_revision + 1
            or self.snapshot.admission.generation != self.request.generation
            or self.snapshot.state != ("released" if self.request.action == "release" else "active")
        ):
            raise ModelError()
        if self.request.action == "renew" and (
            self.snapshot.expires_at - self.snapshot.heartbeat != timedelta(seconds=self.request.lease_seconds)
        ):
            raise ModelError()


@dataclass(frozen=True)
class LeaseDecision:
    """A proposed result or fixed refusal, never a real authorization/acquisition."""

    code: str
    snapshot: LeaseSnapshot | None = None
    replayed: bool = False


def _authority_refusal(admission: Admission, observed: AuthorityObservation) -> str | None:
    if observed.principal != admission.principal:
        return "principal-forbidden"
    if observed.activity == "revoked":
        return "principal-revoked"
    if observed.activity != "active":
        return "principal-inactive"
    if (
        observed.enrollment_id != admission.enrollment_id
        or observed.authorization_enrollment_epoch != admission.authorization_enrollment_epoch
    ):
        return "enrollment-mismatch"
    if observed.authenticated_credential_generation != observed.current_credential_generation:
        return "stale-credential"
    return None


def decide_lease(
    current: LeaseSnapshot,
    observed: AuthorityObservation,
    request: LeaseRequest,
    *,
    holder_matches: bool,
    now: datetime,
    historical: HistoricalResult | None = None,
) -> LeaseDecision:
    """Evaluate supplied authority, holder comparison, CAS and lease clocks.

    Replay checks the historical owner/enrollment, not a successor's authority.
    A replay never applies its result to ``current`` or refreshes its deadline.
    ``holder_matches`` refers to the historical holder for replay and the
    current holder otherwise. Integration must authenticate that selected
    record and holder comparison; these explicit inputs are never a body
    boolean authorization route.
    """
    now = _clock(now)
    authority = historical.snapshot.admission if historical is not None else current.admission
    refusal = _authority_refusal(authority, observed)
    if refusal:
        return LeaseDecision(refusal)
    if holder_matches is not True:
        return LeaseDecision("holder-mismatch")
    if request.action not in authority.actions:
        return LeaseDecision("action-forbidden")
    if historical is not None:
        if (
            historical.request != request
            or historical.snapshot.admission.work_id != current.admission.work_id
            or historical.snapshot.admission.repo_identity != current.admission.repo_identity
        ):
            return LeaseDecision("idempotency-conflict")
        return LeaseDecision("historical-replay", historical.snapshot, True)
    if request.generation != current.admission.generation:
        return LeaseDecision("stale-generation")
    if current.state != "active":
        return LeaseDecision("lease-not-active")
    if request.expected_revision != current.revision:
        return LeaseDecision("version-conflict")
    if now < current.heartbeat:
        return LeaseDecision("clock-before-heartbeat")
    if now >= current.expires_at:
        return LeaseDecision("lease-expired")
    if request.lease_seconds > current.admission.max_lease_seconds:
        return LeaseDecision("lease-duration-forbidden")
    if current.revision == ownership.COUNTER_MAX:
        return LeaseDecision("revision-exhausted")
    try:
        deadline = now + timedelta(seconds=request.lease_seconds) if request.action == "renew" else current.expires_at
    except OverflowError:
        return LeaseDecision("clock-out-of-bounds")
    return LeaseDecision(
        "proposed",
        replace(
            current,
            revision=current.revision + 1,
            heartbeat=now,
            expires_at=deadline,
            state="released" if request.action == "release" else "active",
        ),
    )


def lease_projection(snapshot: object, *, now: datetime) -> dict[str, object]:
    """Bounded public allowlist; expiry never releases scope or proves cessation.

    Legacy, campaign, unknown-version and malformed raw reservations remain
    unassessed. Only validated explicit v1 snapshots receive an expiry verdict.
    Cleanup, exit capacity and replacement atomicity belong to held H1b work.
    """
    now = _clock(now)
    result: dict[str, object] = {
        "status": "unassessed",
        "reservation_retained": True,
        "physical_writer_stopped": False,
        "evidence_class": "supplied-model-only",
    }
    if not isinstance(snapshot, LeaseSnapshot):
        return result
    result.update(
        status="released" if snapshot.state == "released" else "elapsed" if now >= snapshot.expires_at else "active",
        protocol_version=1,
        work_id=snapshot.admission.work_id,
        generation=snapshot.admission.generation,
        revision=snapshot.revision,
        owner_kind=snapshot.admission.principal.kind,
        owner_id=snapshot.admission.principal.id,
        provider=snapshot.binding.provider,
        task_id=snapshot.binding.task_id,
        thread_id=snapshot.binding.thread_id,
        repo_identity=snapshot.binding.repo_identity,
        write_scope=snapshot.binding.write_scope,
        source_revision=snapshot.binding.source_revision,
        heartbeat=snapshot.heartbeat.isoformat(),
        expires_at=snapshot.expires_at.isoformat(),
    )
    return result
