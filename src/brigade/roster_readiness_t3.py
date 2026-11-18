"""Parent-reported T3 route support plus Brigade governance for ``route == "t3"``.

T3 Code keeps no admission ledger and no child concurrency limit, so Brigade is
the admission and capacity authority for governed T3. T3 route support is
different: only the parent session can call its own ``orchestrator_capabilities``
tool. Brigade never calls T3, never mints a T3 session and never calls MCP. The
parent passes a bounded envelope on stdout or in a file, and this module parses
it strictly and reports evidence for the exact selected targets.

Nothing here reserves capacity, takes the native capacity lock or the run lock,
or writes workspace or Hub state.
"""

from __future__ import annotations

import json
import math
import sys
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

from . import native_t3_admission, native_t3_capacity, roster as roster_mod
from .native_t3_contract import INHERIT_MODE, INTERACTION_MODES, RUNTIME_MODES, NativeTarget
from .native_t3_dispatch import delegate_target_arguments
from .native_t3_enrollment import EnrollmentError, target_for_seat
from .roster_readiness import OPERATION_TTL_SECONDS, Observation, ReadinessRequest, operation_scope

SCHEMA = "brigade.t3_capabilities_report.v1"
T3_CONTRACT_REVISION = "c52df24d28"
SOURCE_PARENT_REPORTED = "parent-reported"
SOURCE_BRIGADE_GOVERNANCE = "brigade-governance"

CALLER_CONTEXT_UNAVAILABLE = "t3-caller-context-unavailable"
CAPABILITIES_MALFORMED = "t3-capabilities-malformed"
CAPABILITIES_STALE = "t3-capabilities-stale"
CAPABILITIES_FUTURE = "t3-capabilities-future"
CAPABILITIES_ROUTE_MISMATCH = "t3-capabilities-route-mismatch"
QUALIFIER_MISSING = "t3-qualifier-missing"

MAX_BYTES = 262_144
MAX_DEPTH = 12
MAX_NODES = 50_000
MAX_PROVIDERS = 64
MAX_MODELS = 256
MAX_OPTIONS = 64
MAX_VALUES = 256
MAX_STRING = 1024
MAX_COUNT = 1_000_000

_ENVELOPE_KEYS = frozenset({"schema", "origin_environment", "origin_project", "reported_at", "capabilities"})
_CAPABILITY_KEYS = frozenset(
    {
        "parentThreadId",
        "inheritedProviderInstanceId",
        "inheritedModel",
        "inheritedOptions",
        "runtimeMode",
        "interactionMode",
        "providers",
        "features",
    }
)
_PROVIDER_KEYS = frozenset(
    {
        "providerInstanceId",
        "driverKind",
        "displayName",
        "models",
        "omittedModels",
        "runtimeModes",
        "canRunChildTask",
        "canRunCrossProviderChildTask",
        "constraints",
    }
)
_MODEL_KEYS = frozenset({"id", "label", "options"})
_OPTION_KEYS = frozenset({"id", "values", "default"})
_SELECTION_KEYS = frozenset({"id", "value"})
_FEATURE_KEYS = frozenset(
    {
        "appOwnedSubagents",
        "asyncPolling",
        "cancellation",
        "batchThreadCreation",
        "threadManagement",
        "incrementalThreadRead",
        "scheduledTasks",
        "maxBatchThreads",
    }
)
_FEATURE_BOOLS = _FEATURE_KEYS - {"maxBatchThreads"}
_CAPABILITY_REQUIRED = _CAPABILITY_KEYS - {"inheritedOptions"}
_PROVIDER_REQUIRED = _PROVIDER_KEYS - {"omittedModels", "runtimeModes"}
_MODEL_REQUIRED = frozenset({"id"})
_OPTION_REQUIRED = frozenset({"id", "values"})

_TARGET_ERRORS = frozenset(
    {"seat-unbound", "invalid-target", "target-mode-invalid", "target-remote-unsupported", "target-isolation"}
)
_SOURCE_REVISION_ERRORS = frozenset({"source-unavailable", "source-dirty"})


class _Malformed(Exception):
    """A bounded input violation with no retained content."""


_UNSET = object()


class _V:
    """Tiny validator for one decoded JSON value."""

    def __init__(self, value: object) -> None:
        self.value = value

    def obj(self, keys: frozenset[str]) -> Mapping[str, Any]:
        if not isinstance(self.value, dict):
            raise _Malformed()
        if frozenset(self.value) != keys:
            raise _Malformed()
        return self.value

    def obj_fields(self, required: frozenset[str], allowed: frozenset[str]) -> Mapping[str, Any]:
        if not isinstance(self.value, dict):
            raise _Malformed()
        present = frozenset(self.value)
        if not required <= present or not present <= allowed:
            raise _Malformed()
        return self.value

    def text(self, *, maximum: int = MAX_STRING) -> str:
        value = self.value
        if type(value) is not str or not value or len(value) > maximum:
            raise _Malformed()
        return value

    def optional_text(self) -> str | None:
        if self.value is None:
            return None
        return self.text()

    def boolean(self) -> bool:
        if type(self.value) is not bool:
            raise _Malformed()
        return self.value

    def count(self, *, maximum: int = MAX_COUNT) -> int:
        value = self.value
        if type(value) is not int or value < 0 or value > maximum:
            raise _Malformed()
        return value

    def number(self) -> float:
        value = self.value
        if type(value) not in (int, float):
            raise _Malformed()
        result = float(value)  # type: ignore[arg-type]
        if not math.isfinite(result) or result < 0 or result > float(MAX_COUNT):
            raise _Malformed()
        return result

    def sequence(self, *, maximum: int) -> list[Any]:
        value = self.value
        if type(value) is not list or len(value) > maximum:
            raise _Malformed()
        return value


def _option_value(value: object) -> str | bool:
    if type(value) not in (str, bool):
        raise _Malformed()
    if type(value) is str and (not value or len(value) > MAX_STRING):
        raise _Malformed()
    return value  # type: ignore[return-value]


def _value_key(value: str | bool) -> tuple[str, str | bool]:
    # Python treats ``True == 1``. Allowed values are strings or booleans, so
    # the type name keeps boolean and string literals distinct.
    return type(value).__name__, value


def _scan_tree(value: object) -> None:
    """Bound total nodes, nesting depth and every string, without recursion."""
    stack: list[tuple[object, int]] = [(value, 1)]
    nodes = 0
    while stack:
        node, depth = stack.pop()
        nodes += 1
        if nodes > MAX_NODES or depth > MAX_DEPTH:
            raise _Malformed()
        if isinstance(node, dict):
            for key, item in node.items():
                if type(key) is not str or len(key) > MAX_STRING:
                    raise _Malformed()
                stack.append((item, depth + 1))
        elif isinstance(node, list):
            for item in node:
                stack.append((item, depth + 1))
        elif isinstance(node, str):
            if len(node) > MAX_STRING:
                raise _Malformed()
        elif node is None or type(node) in (bool, int, float):
            continue
        else:
            raise _Malformed()


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: set[str] = set()
    for key, _ in pairs:
        if key in seen:
            raise _Malformed()
        seen.add(key)
    return dict(pairs)


def _constant(_: str) -> Any:
    raise _Malformed()


@dataclass(frozen=True)
class OptionDescriptor:
    id: str
    values: tuple[str | bool, ...]


@dataclass(frozen=True)
class ModelCapability:
    id: str
    # ``None`` means T3 omitted descriptors, so T3 skips option validation.
    options: tuple[OptionDescriptor, ...] | None


@dataclass(frozen=True)
class ProviderCapability:
    provider_instance: str
    driver: str
    models: tuple[ModelCapability, ...]
    omitted: int
    runtime_modes: tuple[str, ...] | None
    can_run_child: bool
    can_run_cross: bool
    constraints_empty: bool


@dataclass(frozen=True)
class T3Capabilities:
    """A bounded, normalized parent report. Raw input values are never retained."""

    state: str
    reported_at: float
    received_at: float
    origin_environment: str = ""
    origin_project: str = ""
    parent_thread: str = ""
    inherited_provider: str | None = None
    runtime_mode: str = ""
    interaction_mode: str = ""
    providers: tuple[ProviderCapability, ...] = ()
    app_owned_subagents: bool = False
    cancellation: bool = False
    projection: Mapping[str, Any] = field(default_factory=dict)


def _malformed(received_at: float) -> T3Capabilities:
    return T3Capabilities("malformed", 0.0, received_at, projection={"state": "malformed"})


def _read_source(source: str | Path) -> bytes:
    if str(source) == "-":
        data = sys.stdin.buffer.read(MAX_BYTES + 1)
    else:
        path = Path(source)
        if not path.is_file():
            raise OSError("not a regular file")
        if path.stat().st_size > MAX_BYTES:
            raise _Malformed()
        with path.open("rb") as handle:
            data = handle.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise _Malformed()
    return data


def _parse_timestamp(value: object) -> float:
    text = _V(value).text(maximum=64)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise _Malformed() from None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise _Malformed()
    return parsed.timestamp()


def _parse_option(raw: object) -> OptionDescriptor:
    option = _V(raw).obj_fields(_OPTION_REQUIRED, _OPTION_KEYS)
    option_id = _V(option["id"]).text()
    values = _V(option["values"]).sequence(maximum=MAX_VALUES)
    # T3's capabilities result always carries a non-empty values list when the
    # descriptor is present. A boolean descriptor reports [false, true].
    if not values:
        raise _Malformed()
    normalized = tuple(_option_value(value) for value in values)
    seen: set[tuple[str, str | bool]] = set()
    for value in normalized:
        key = _value_key(value)
        if key in seen:
            raise _Malformed()
        seen.add(key)
    if "default" in option:
        _option_value(option["default"])
    return OptionDescriptor(option_id, normalized)


def _parse_model(raw: object) -> ModelCapability:
    model = _V(raw).obj_fields(_MODEL_REQUIRED, _MODEL_KEYS)
    model_id = _V(model["id"]).text()
    if "label" in model:
        _V(model["label"]).text()
    options: tuple[OptionDescriptor, ...] | None = None
    if "options" in model:
        raw_options = _V(model["options"]).sequence(maximum=MAX_OPTIONS)
        parsed = tuple(_parse_option(option) for option in raw_options)
        seen: set[str] = set()
        for option in parsed:
            if option.id in seen:
                raise _Malformed()
            seen.add(option.id)
        options = parsed
    return ModelCapability(model_id, options)


def _parse_provider(raw: object) -> ProviderCapability:
    provider = _V(raw).obj_fields(_PROVIDER_REQUIRED, _PROVIDER_KEYS)
    provider_instance = _V(provider["providerInstanceId"]).text()
    driver = _V(provider["driverKind"]).text()
    if "displayName" in provider:
        _V(provider["displayName"]).optional_text()
    raw_models = _V(provider["models"]).sequence(maximum=MAX_MODELS)
    models = tuple(_parse_model(model) for model in raw_models)
    seen_models: set[str] = set()
    for model in models:
        if model.id in seen_models:
            raise _Malformed()
        seen_models.add(model.id)
    omitted = _V(provider["omittedModels"]).count() if "omittedModels" in provider else 0
    runtime_modes: tuple[str, ...] | None = None
    if "runtimeModes" in provider:
        raw_modes = _V(provider["runtimeModes"]).sequence(maximum=MAX_VALUES)
        modes = tuple(_V(mode).text() for mode in raw_modes)
        if len(set(modes)) != len(modes) or any(mode not in RUNTIME_MODES for mode in modes):
            raise _Malformed()
        runtime_modes = modes
    can_run_child = _V(provider["canRunChildTask"]).boolean()
    can_run_cross = _V(provider["canRunCrossProviderChildTask"]).boolean()
    raw_constraints = _V(provider["constraints"]).sequence(maximum=MAX_VALUES)
    for constraint in raw_constraints:
        _V(constraint).text()
    return ProviderCapability(
        provider_instance,
        driver,
        models,
        omitted,
        runtime_modes,
        can_run_child,
        can_run_cross,
        not raw_constraints,
    )


def _parse_features(raw: object) -> tuple[bool, bool]:
    features = _V(raw).obj(_FEATURE_KEYS)
    values = {name: _V(features[name]).boolean() for name in _FEATURE_BOOLS}
    _V(features["maxBatchThreads"]).number()
    return values["appOwnedSubagents"], values["cancellation"]


def _parse_payload(payload: object, received_at: float) -> T3Capabilities:
    envelope = _V(payload).obj(_ENVELOPE_KEYS)
    if _V(envelope["schema"]).text() != SCHEMA:
        raise _Malformed()
    origin_environment = _V(envelope["origin_environment"]).text()
    origin_project = _V(envelope["origin_project"]).text()
    reported_at = _parse_timestamp(envelope["reported_at"])
    capabilities = _V(envelope["capabilities"]).obj_fields(_CAPABILITY_REQUIRED, _CAPABILITY_KEYS)
    parent_thread = _V(capabilities["parentThreadId"]).optional_text() or ""
    inherited_provider = _V(capabilities["inheritedProviderInstanceId"]).optional_text()
    _V(capabilities["inheritedModel"]).optional_text()
    if "inheritedOptions" in capabilities:
        selections = _V(capabilities["inheritedOptions"]).sequence(maximum=MAX_OPTIONS)
        seen: set[str] = set()
        for raw in selections:
            selection = _V(raw).obj(_SELECTION_KEYS)
            selection_id = _V(selection["id"]).text()
            _option_value(selection["value"])
            if selection_id in seen:
                raise _Malformed()
            seen.add(selection_id)
    runtime_mode = _V(capabilities["runtimeMode"]).text()
    interaction_mode = _V(capabilities["interactionMode"]).text()
    if runtime_mode not in RUNTIME_MODES or interaction_mode not in INTERACTION_MODES:
        raise _Malformed()
    raw_providers = _V(capabilities["providers"]).sequence(maximum=MAX_PROVIDERS)
    providers = tuple(_parse_provider(provider) for provider in raw_providers)
    seen_providers: set[str] = set()
    for provider in providers:
        if provider.provider_instance in seen_providers:
            raise _Malformed()
        seen_providers.add(provider.provider_instance)
    app_owned, cancellation = _parse_features(capabilities["features"])
    return T3Capabilities(
        "ok",
        reported_at,
        received_at,
        origin_environment=origin_environment,
        origin_project=origin_project,
        parent_thread=parent_thread,
        inherited_provider=inherited_provider,
        runtime_mode=runtime_mode,
        interaction_mode=interaction_mode,
        providers=providers,
        app_owned_subagents=app_owned,
        cancellation=cancellation,
        projection={"state": "ok"},
    )


def load_capabilities(source: str | Path, *, now: float | None = None) -> T3Capabilities:
    """Parse one parent-reported envelope with strict, closed bounds."""
    received_at = time.time() if now is None else now
    try:
        data = _read_source(source)
        payload = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
        _scan_tree(payload)
        return _parse_payload(payload, received_at)
    except (OSError, UnicodeDecodeError, ValueError, RecursionError, _Malformed):
        return _malformed(received_at)


@dataclass(frozen=True)
class SeatTarget:
    seat: str
    target: NativeTarget | None
    error: str | None


def resolve_seat_targets(
    roster: roster_mod.Roster, workers: tuple[str, ...], workspace: Path
) -> tuple[SeatTarget, ...]:
    """Resolve every selected seat through the enrollment mapping, without writes."""
    from .fleet_t3_transport import SourceRevisionError, clean_source_revision

    try:
        revision = clean_source_revision(workspace, ignore_prefixes=(".brigade/",))
    except SourceRevisionError as exc:
        code = exc.code if exc.code in _SOURCE_REVISION_ERRORS else "source-unavailable"
        return tuple(SeatTarget(seat, None, code) for seat in workers)
    except (OSError, ValueError):
        return tuple(SeatTarget(seat, None, "source-unavailable") for seat in workers)
    resolved: list[SeatTarget] = []
    for seat in workers:
        try:
            target = target_for_seat(roster, seat, source_revision=revision)
        except EnrollmentError as exc:
            code = exc.code if exc.code in _TARGET_ERRORS else "invalid-target"
            resolved.append(SeatTarget(seat, None, code))
            continue
        resolved.append(SeatTarget(seat, target, None))
    return tuple(resolved)


def _provider(report: T3Capabilities, provider_instance: str) -> ProviderCapability | None:
    return next((item for item in report.providers if item.provider_instance == provider_instance), None)


def _model(provider: ProviderCapability, model_id: str) -> ModelCapability | None:
    return next((item for item in provider.models if item.id == model_id), None)


def _normalized_provider(report: T3Capabilities, target: NativeTarget) -> dict[str, Any]:
    """The selected-target capability projection bound into operation scope."""
    provider = _provider(report, target.provider_instance)
    if provider is None:
        return {"selected": False}
    model = _model(provider, target.launch_model)
    return {
        "selected": True,
        "driver": provider.driver,
        "can_run_child": provider.can_run_child,
        "can_run_cross": provider.can_run_cross,
        "constraints_empty": provider.constraints_empty,
        "omitted": provider.omitted,
        "runtime_modes": None if provider.runtime_modes is None else list(provider.runtime_modes),
        "model": None
        if model is None
        else {
            "options": None
            if model.options is None
            else [
                {"id": option.id, "values": [[type(value).__name__, value] for value in option.values]}
                for option in model.options
            ]
        },
    }


def scope_projection(
    roster: roster_mod.Roster,
    request: ReadinessRequest,
    workspace: Path,
    *,
    report: T3Capabilities | None,
    targets: tuple[SeatTarget, ...] | None = None,
    hub_digest: str | None | object = _UNSET,
) -> dict[str, Any]:
    """Bind every readiness-relevant T3 input, including the launch arguments."""
    resolved = targets if targets is not None else resolve_seat_targets(roster, request.workers, workspace)
    if hub_digest is _UNSET:
        authority = native_t3_admission.read_hub_authority(workspace)
        hub_digest = authority.digest if authority.state == "live" else None
    seats: dict[str, Any] = {}
    for item in resolved:
        declaration = roster.t3_targets.get(item.seat) or {}
        seats[item.seat] = {
            "target": None if item.target is None else _target_projection(item.target),
            "target_error": item.error,
            "max_live": declaration.get("max_live"),
            "launch": None if item.target is None else delegate_target_arguments(item.target),
            "capability": None if report is None or item.target is None else _normalized_provider(report, item.target),
        }
    return {
        "contract": T3_CONTRACT_REVISION,
        "source": SOURCE_PARENT_REPORTED,
        "roster_digest": native_t3_admission.roster_digest(roster),
        "global_limit": roster.max_workers,
        "hub_policy_digest": hub_digest,
        "seats": seats,
        # Local receipt time is reported but never bound into scope, so the same
        # envelope keeps one scope across invocations.
        "received_at": None if report is None else report.received_at,
        "envelope": None
        if report is None
        else {
            "state": report.state,
            "origin_environment": report.origin_environment,
            "origin_project": report.origin_project,
            "parent_thread": report.parent_thread,
            "reported_at": report.reported_at,
            "projection": dict(report.projection),
        },
    }


def _target_projection(target: NativeTarget) -> dict[str, Any]:
    return {
        "origin_environment": target.origin_environment,
        "origin_thread": target.origin_thread,
        "target_environment": target.target_environment,
        "target_project": target.target_project,
        "provider_instance": target.provider_instance,
        "driver": target.driver,
        "launch_model": target.launch_model,
        "options": [[key, value] for key, value in target.options],
        "runtime_mode": target.runtime_mode,
        "interaction_mode": target.interaction_mode,
        "isolation": target.isolation,
        "source_revision": target.source_revision,
    }


def _freshness(report: T3Capabilities, now: float) -> str | None:
    if report.reported_at > report.received_at or now < report.reported_at:
        return CAPABILITIES_FUTURE
    age = now - report.reported_at
    if not math.isfinite(age) or age > OPERATION_TTL_SECONDS:
        return CAPABILITIES_STALE
    return None


def _route_support(item: SeatTarget, report: T3Capabilities | None, now: float) -> Observation:
    if report is None:
        return Observation(
            "unknown",
            now,
            "",
            detail="no parent-reported capabilities envelope was supplied",
            reason=CALLER_CONTEXT_UNAVAILABLE,
            seat=item.seat,
            source=SOURCE_PARENT_REPORTED,
        )
    if report.state != "ok":
        return Observation(
            "failed",
            now,
            "",
            detail="parent-reported capabilities envelope is malformed",
            reason=CAPABILITIES_MALFORMED,
            seat=item.seat,
            source=SOURCE_PARENT_REPORTED,
        )
    stale = _freshness(report, now)
    if stale is not None:
        return Observation(
            "unknown",
            now,
            "",
            detail="parent-reported capabilities are not fresh",
            reason=stale,
            seat=item.seat,
            source=SOURCE_PARENT_REPORTED,
        )
    target = item.target
    if target is None:
        return Observation(
            "unknown",
            now,
            "",
            detail="the selected seat has no resolvable native target",
            reason="target-unavailable",
            seat=item.seat,
            source=SOURCE_PARENT_REPORTED,
        )
    refusal = _route_refusal(target, report)
    if refusal is not None:
        status, reason = refusal
        return Observation(
            status,
            now,
            "",
            detail="parent-reported route support",
            reason=reason,
            seat=item.seat,
            source=SOURCE_PARENT_REPORTED,
        )
    tokens: list[str] = []
    provider = _provider(report, target.provider_instance)
    model = _model(provider, target.launch_model) if provider is not None else None
    if target.options and model is not None and model.options is None:
        tokens.append("options-unchecked-by-t3")
    if target.runtime_mode != INHERIT_MODE or target.interaction_mode != INHERIT_MODE:
        tokens.append("mode-ceiling-unchecked")
    return Observation(
        "passed",
        now,
        "",
        detail=" ".join(tokens),
        reason=tokens[0] if tokens else SOURCE_PARENT_REPORTED,
        seat=item.seat,
        source=SOURCE_PARENT_REPORTED,
    )


def _route_refusal(target: NativeTarget, report: T3Capabilities) -> tuple[str, str] | None:
    """Return the first fixed-token refusal for one selected seat, or ``None``."""
    if report.origin_environment != target.origin_environment or target.target_environment != target.origin_environment:
        return "unsupported", "t3-environment-mismatch"
    if report.origin_project != target.target_project:
        return "unsupported", "t3-project-mismatch"
    if report.parent_thread != target.origin_thread:
        return "unsupported", "t3-parent-thread-mismatch"
    provider = _provider(report, target.provider_instance)
    if provider is None:
        return "failed", "provider-missing"
    if provider.driver != target.driver:
        return "failed", "driver-mismatch"
    if not provider.can_run_child:
        return "failed", "child-task-disabled"
    if not provider.constraints_empty:
        return "failed", "provider-constrained"
    if target.provider_instance != report.inherited_provider and not provider.can_run_cross:
        return "failed", "cross-provider-disabled"
    model = _model(provider, target.launch_model)
    if model is None:
        if provider.omitted > 0:
            return "unknown", "model-not-observed"
        return "failed", "model-unavailable"
    if model.options is not None:
        descriptors = {option.id: option for option in model.options}
        for key, value in target.options:
            descriptor = descriptors.get(key)
            if descriptor is None:
                return "failed", "option-undeclared"
            if _value_key(value) not in {_value_key(allowed) for allowed in descriptor.values}:
                return "failed", "option-value-undeclared"
    if (
        target.runtime_mode != INHERIT_MODE
        and provider.runtime_modes is not None
        and target.runtime_mode not in provider.runtime_modes
    ):
        return "failed", "runtime-mode-undeclared"
    if not report.app_owned_subagents:
        return "failed", "app-owned-subagents-disabled"
    if not report.cancellation:
        return "failed", "cancellation-disabled"
    return None


def _admission(item: SeatTarget, roster: roster_mod.Roster, hub: Any, now: float) -> Observation:
    if item.target is None:
        return Observation(
            "failed",
            now,
            "",
            detail=item.error or "target-unavailable",
            reason="target-unavailable",
            seat=item.seat,
            source=SOURCE_BRIGADE_GOVERNANCE,
        )
    verdict = native_t3_admission.evaluate(roster, item.seat, item.target, hub=hub)
    if verdict.admitted:
        status = "passed"
    elif verdict.code == native_t3_admission.DENIED:
        status = "failed"
    else:
        status = "unknown"
    return Observation(
        status,
        now,
        "",
        detail="native-t3-admission",
        reason=verdict.reason,
        seat=item.seat,
        source=SOURCE_BRIGADE_GOVERNANCE,
    )


def _isolation(
    roster: roster_mod.Roster, request: ReadinessRequest, workers: tuple[str, ...], now: float
) -> Observation:
    if request.isolation != "worktree":
        return Observation(
            "unsupported",
            now,
            "",
            detail="route t3 requires worktree isolation",
            reason="isolation-shared-unsupported",
            source=SOURCE_BRIGADE_GOVERNANCE,
        )
    declared = all((roster.t3_targets.get(seat) or {}).get("isolation") == "worktree" for seat in workers)
    if not declared:
        return Observation(
            "unsupported",
            now,
            "",
            detail="every selected seat must declare worktree isolation",
            reason="isolation-worktree-required",
            source=SOURCE_BRIGADE_GOVERNANCE,
        )
    return Observation(
        "passed",
        now,
        "",
        detail="worktree isolation is declared; creation was not attempted",
        reason="isolation-worktree-declared",
        source=SOURCE_BRIGADE_GOVERNANCE,
    )


def _capacity(roster: roster_mod.Roster, workers: tuple[str, ...], workspace: Path, now: float) -> Observation:
    occupancy = native_t3_capacity.governed_occupancy(workspace)
    if not occupancy.authoritative:
        return Observation(
            "unknown",
            now,
            "",
            detail="native-t3-capacity occupancy is not authoritative",
            reason="capacity-unknown",
            source=SOURCE_BRIGADE_GOVERNANCE,
        )
    free: int | None = None
    for seat in workers:
        decision = native_t3_capacity.assess(roster, seat, occupancy)
        if decision.code not in {
            native_t3_capacity.CAPACITY_AVAILABLE,
            native_t3_capacity.CAPACITY_DENIED,
        }:
            return Observation(
                "unknown",
                now,
                "",
                detail="native-t3-capacity is unavailable for a selected seat",
                reason="capacity-unknown",
                seat=seat,
                source=SOURCE_BRIGADE_GOVERNANCE,
            )
        seat_free = decision.free
        if seat_free is None:
            return Observation(
                "unknown",
                now,
                "",
                detail="native-t3-capacity has no free count for a selected seat",
                reason="capacity-unknown",
                seat=seat,
                source=SOURCE_BRIGADE_GOVERNANCE,
            )
        free = seat_free if free is None else min(free, seat_free)
    return Observation(
        "passed",
        now,
        "",
        detail="native-t3-capacity governed occupancy",
        capacity=free,
        source=SOURCE_BRIGADE_GOVERNANCE,
    )


def t3_route_readiness(
    roster: roster_mod.Roster,
    request: ReadinessRequest,
    *,
    workspace: Path,
    report: T3Capabilities | None,
    now: float | None = None,
) -> tuple[dict[str, Any], dict[str, tuple[Observation, ...]]]:
    """Produce T3 route support and Brigade governance observations, read-only.

    The returned projection is the operation scope material. It binds the
    envelope, every selected target, the effective roster digest, the live Hub
    policy digest when readable, and the exact claim launch arguments.
    """
    moment = time.time() if now is None else now
    workers = tuple(dict.fromkeys(request.workers))
    targets = resolve_seat_targets(roster, workers, workspace)
    hub = native_t3_admission.read_hub_authority(workspace)
    projection = scope_projection(
        roster,
        request,
        workspace,
        report=report,
        targets=targets,
        hub_digest=hub.digest if hub.state == "live" else None,
    )
    scope = operation_scope(roster, request, workspace, t3_context=projection)
    produced = {
        "route-support": tuple(_route_support(item, report, moment) for item in targets),
        "isolation": (_isolation(roster, request, workers, moment),),
        "admission": tuple(_admission(item, roster, hub, moment) for item in targets),
        "capacity": (_capacity(roster, workers, workspace, moment),),
    }
    observations = {
        category: tuple(replace(item, scope=scope) for item in items) for category, items in produced.items()
    }
    return projection, observations
