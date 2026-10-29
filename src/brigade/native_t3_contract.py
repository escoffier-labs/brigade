"""Pure supplied-data declarations and immutable native T3 prelaunch intent.

No supplied projection is authenticated here. A matched declaration grants no
readiness, capacity, admission, isolation eligibility or permission to launch.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Literal

from brigade import fleet_model_admission, roster_readiness, run_task_journal, run_tasks

_REFUSALS = fleet_model_admission._POLICY_DENIALS | fleet_model_admission._PROOF_DENIALS
_AUTHORITY_KINDS = frozenset(
    {"policy", "decision", "delegation", "admission", "reservation", "launch-proof", "budget-dispatch"}
)
_OPERATION_UNKNOWNS = ("route-support-unknown", "isolation-unknown", "admission-unknown", "capacity-unknown")
_REASONS = _REFUSALS | frozenset(
    {
        *_OPERATION_UNKNOWNS,
        "discovery-missing",
        "binding-missing",
        "discovery-stale",
        "binding-stale",
        "discovery-context-mismatch",
        "binding-mismatch",
        "provider-disabled",
        "runtime-undeclared",
        "model-undeclared",
        "option-undeclared",
        "option-value-undeclared",
    }
)


def _require(valid: bool) -> None:
    if not valid:
        raise ValueError("invalid native T3 contract")


def _reference(value: object) -> None:
    _require(type(value) is str and run_task_journal.provider_reference(value))


def _time(value: object) -> float:
    _require(type(value) in (int, float))
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        raise ValueError("invalid native T3 contract") from None
    _require(math.isfinite(result))
    return result


def _sequence(value: object) -> tuple:
    _require(type(value) in (tuple, list))
    return tuple(value)  # type: ignore[arg-type]


def _option_pair(value: object) -> tuple[str, object]:
    pair = _sequence(value)
    _require(len(pair) == 2)
    key, item = pair
    _require(type(key) is str and run_task_journal.token(key))
    return key, item


def _option_value(value: object) -> None:
    _require(type(value) in (str, bool))
    if type(value) is str:
        _reference(value)


def _interval(observed_at: object, expires_at: object) -> tuple[float, float]:
    observed, expires = _time(observed_at), _time(expires_at)
    _require(observed < expires)
    return observed, expires


@dataclass(frozen=True)
class NativeTarget:
    origin_environment: str
    origin_thread: str
    target_environment: str
    target_project: str
    provider_instance: str
    driver: str
    launch_model: str
    options: tuple[tuple[str, str | bool], ...]
    runtime_mode: str
    interaction_mode: str
    isolation: str
    source_revision: str

    def __post_init__(self) -> None:
        for value in (
            self.origin_environment,
            self.origin_thread,
            self.target_environment,
            self.target_project,
            self.provider_instance,
            self.driver,
            self.launch_model,
            self.runtime_mode,
            self.interaction_mode,
            self.isolation,
        ):
            _reference(value)
        _require(self.interaction_mode in {"default", "plan"} and self.isolation in {"shared", "worktree"})
        _require(type(self.source_revision) is str and re.fullmatch(r"[0-9a-f]{40}", self.source_revision) is not None)
        pairs = []
        for pair in _sequence(self.options):
            key, selected_value = _option_pair(pair)
            _option_value(selected_value)
            pairs.append((key, selected_value))
        _require(len({key for key, _ in pairs}) == len(pairs))
        object.__setattr__(self, "options", tuple(sorted(pairs)))


@dataclass(frozen=True)
class WorkBinding:
    authority_ref: str
    work_id: str
    canonical_repository: str
    source_ref: str
    owner_node: str
    ownership_revision: int
    generation: int

    def __post_init__(self) -> None:
        for value in (self.authority_ref, self.work_id, self.canonical_repository, self.source_ref, self.owner_node):
            _reference(value)
        _require(type(self.ownership_revision) is int and self.ownership_revision >= 0)
        _require(type(self.generation) is int and self.generation > 0)


@dataclass(frozen=True)
class ModelDeclaration:
    model: str
    options: tuple[tuple[str, tuple[str | bool, ...]], ...]

    def __post_init__(self) -> None:
        _reference(self.model)
        pairs = []
        for pair in _sequence(self.options):
            key, supplied = _option_pair(pair)
            values = _sequence(supplied)
            _require(bool(values))
            for value in values:
                _option_value(value)
            # str and bool are distinct; canonical ordering does not coerce either.
            allowed = tuple(sorted(set(values), key=lambda value: (type(value).__name__, value)))
            pairs.append((key, allowed))
        _require(len({key for key, _ in pairs}) == len(pairs))
        object.__setattr__(self, "options", tuple(sorted(pairs)))


@dataclass(frozen=True)
class DiscoveryEvidence:
    environment: str
    project: str
    provider_instance: str
    driver: str
    observed_at: float
    expires_at: float
    child_enabled: bool
    runtime_modes: tuple[str, ...]
    models: tuple[ModelDeclaration, ...]

    def __post_init__(self) -> None:
        for value in (self.environment, self.project, self.provider_instance, self.driver):
            _reference(value)
        observed, expires = _interval(self.observed_at, self.expires_at)
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "expires_at", expires)
        _require(type(self.child_enabled) is bool)
        modes = _sequence(self.runtime_modes)
        for mode in modes:
            _reference(mode)
        models = _sequence(self.models)
        _require(all(type(model) is ModelDeclaration for model in models))
        _require(len({model.model for model in models}) == len(models))
        object.__setattr__(self, "runtime_modes", tuple(sorted(set(modes))))
        object.__setattr__(self, "models", tuple(sorted(models, key=lambda model: model.model)))


@dataclass(frozen=True)
class BindingEvidence:
    """A supplied binding projection, with no authentication or target authority."""

    consumer: str
    seat: str
    canonical_model: str
    launch_model: str
    roster_digest: str
    policy_digest: str
    roster_revision: int
    policy_version: int
    observed_at: float
    expires_at: float
    refusal: str | None = None

    def __post_init__(self) -> None:
        for value in (
            self.consumer,
            self.seat,
            self.canonical_model,
            self.launch_model,
            self.roster_digest,
            self.policy_digest,
        ):
            _reference(value)
        _require(type(self.roster_revision) is int and self.roster_revision >= 0)
        _require(type(self.policy_version) is int and self.policy_version >= 0)
        _require(self.refusal is None or (type(self.refusal) is str and self.refusal in _REFUSALS))
        observed, expires = _interval(self.observed_at, self.expires_at)
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "expires_at", expires)


@dataclass(frozen=True)
class Qualification:
    status: Literal["declared", "unknown", "refused"]
    reasons: tuple[str, ...]
    observed_at: float | None
    expires_at: float | None

    def __post_init__(self) -> None:
        _require(type(self.status) is str and self.status in {"declared", "unknown", "refused"})
        reasons = _sequence(self.reasons)
        _require(all(type(reason) is str and reason in _REASONS for reason in reasons))
        object.__setattr__(self, "reasons", reasons)
        _require((self.observed_at is None) == (self.expires_at is None))
        if self.observed_at is not None:
            observed, expires = _interval(self.observed_at, self.expires_at)
            object.__setattr__(self, "observed_at", observed)
            object.__setattr__(self, "expires_at", expires)


def _fresh(evidence: DiscoveryEvidence | BindingEvidence, now: float) -> bool:
    return 0 <= now - evidence.observed_at <= roster_readiness.OPERATION_TTL_SECONDS and now < evidence.expires_at


def _discovery_refusal(target: NativeTarget, evidence: DiscoveryEvidence) -> str | None:
    if (evidence.environment, evidence.project, evidence.provider_instance, evidence.driver) != (
        target.target_environment,
        target.target_project,
        target.provider_instance,
        target.driver,
    ):
        return "discovery-context-mismatch"
    if not evidence.child_enabled:
        return "provider-disabled"
    if target.runtime_mode not in evidence.runtime_modes:
        return "runtime-undeclared"
    model = next((model for model in evidence.models if model.model == target.launch_model), None)
    if model is None:
        return "model-undeclared"
    declarations = dict(model.options)
    for key, value in target.options:
        if key not in declarations:
            return "option-undeclared"
        if value not in declarations[key]:
            return "option-value-undeclared"
    return None


def qualify_target(
    target: NativeTarget,
    *,
    consumer: str,
    seat: str,
    discovery: DiscoveryEvidence | None,
    binding: BindingEvidence | None,
    now: float,
) -> Qualification:
    """Match supplied declarations only; all operation authority stays unknown."""
    _require(type(target) is NativeTarget)
    _reference(consumer)
    _reference(seat)
    _require(discovery is None or type(discovery) is DiscoveryEvidence)
    _require(binding is None or type(binding) is BindingEvidence)
    current = _time(now)
    evidence = [item for item in (discovery, binding) if item is not None]
    observed = min((item.observed_at for item in evidence), default=None)
    expires = min((item.expires_at for item in evidence), default=None)
    unknown, refused = [], []
    if discovery is None:
        unknown.append("discovery-missing")
    elif not _fresh(discovery, current):
        unknown.append("discovery-stale")
    else:
        reason = _discovery_refusal(target, discovery)
        if reason is not None:
            refused.append(reason)
    if binding is None:
        unknown.append("binding-missing")
    elif not _fresh(binding, current):
        unknown.append("binding-stale")
    elif (binding.consumer, binding.seat, binding.launch_model) != (consumer, seat, target.launch_model):
        refused.append("binding-mismatch")
    elif binding.refusal is not None:
        refused.append(binding.refusal)
    if refused:
        return Qualification("refused", tuple(refused), observed, expires)
    if unknown:
        return Qualification("unknown", tuple(unknown), observed, expires)
    return Qualification("declared", _OPERATION_UNKNOWNS, observed, expires)


@dataclass(frozen=True)
class AuthorityReference:
    """Correlation only; retaining a reference never validates its authority."""

    kind: str
    reference: str
    expires_at: float | None

    def __post_init__(self) -> None:
        _require(type(self.kind) is str and self.kind in _AUTHORITY_KINDS)
        _reference(self.reference)
        if self.expires_at is not None:
            object.__setattr__(self, "expires_at", _time(self.expires_at))


@dataclass(frozen=True)
class NativeIntent:
    work: WorkBinding
    handle: run_tasks.TaskHandle
    dispatch_attempt: int
    target: NativeTarget
    payload_context_digest: str
    batch_binding_ref: str
    native_client_request_key: str
    authority_refs: tuple[AuthorityReference, ...]

    def __post_init__(self) -> None:
        _require(type(self.work) is WorkBinding and type(self.target) is NativeTarget)
        _require(type(self.handle) is run_tasks.TaskHandle and type(self.handle.spec) is run_tasks.TaskSpec)
        handle = self.handle
        _require(run_task_journal.token(handle.run_id) and run_task_journal.token(handle.request_id))
        _require(run_task_journal.task_id(handle.spec.task_id) and run_task_journal.token(handle.spec.seat))
        _require(run_task_journal.digest(handle.spec.payload_digest) and run_task_journal.digest(handle.attempt_id))
        _require(type(self.dispatch_attempt) is int and self.dispatch_attempt > 0)
        _require(run_task_journal.digest(self.payload_context_digest))
        _reference(self.batch_binding_ref)
        _reference(self.native_client_request_key)
        refs = _sequence(self.authority_refs)
        _require(all(type(ref) is AuthorityReference for ref in refs))
        _require(len({ref.kind for ref in refs}) == len(refs))
        object.__setattr__(self, "authority_refs", tuple(sorted(refs, key=lambda ref: ref.kind)))


def compare_intents(original: NativeIntent, candidate: NativeIntent) -> Literal["identical", "conflict"]:
    """Ordinary equality across the validated prelaunch values, without side effects."""
    _require(type(original) is NativeIntent and type(candidate) is NativeIntent)
    return "identical" if original == candidate else "conflict"


def qualification_diagnostic(result: Qualification) -> dict[str, object]:
    """Return only validated fixed tokens and original source time bounds."""
    _require(type(result) is Qualification)
    return {
        "status": result.status,
        "reasons": list(result.reasons),
        "observed_at": result.observed_at,
        "expires_at": result.expires_at,
    }
