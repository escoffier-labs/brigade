"""Orca 1.4.268 binding of the governed native child core."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping

from . import roster as roster_mod
from .native_child_host import NativeChildError, NativeChildHost, TargetUnresolved

ORCA_VERSION = "1.4.268"
UNSUPPORTED = "orca-unsupported"
STRUCTURAL_GAPS = frozenset(
    {
        "version-unsupported",
        "driver-unsupported",
        "worktree-unsupported",
        "model-required",
        "model-unsupported",
        "effort-unsupported",
    }
)

# Orca's worker-start help and agent-session option catalogs at v1.4.268.
# Provider-specific model/effort availability remains an Orca runtime check.
_STANDARD_EFFORT = frozenset({"low", "medium", "high"})
_EXTENDED_EFFORT = _STANDARD_EFFORT | {"xhigh", "max"}
AGENT_EFFORTS = {
    "claude": _EXTENDED_EFFORT,
    "codex": _EXTENDED_EFFORT | {"minimal", "ultra"},
    "opencode": frozenset(),
    "antigravity": _STANDARD_EFFORT,
    "pi": _EXTENDED_EFFORT | {"off", "minimal"},
    "muse": _EXTENDED_EFFORT | {"minimal", "ultra"},
}
NEW_WORKTREES = frozenset({"new-child", "new-top-level"})
# Orca's stable worktree identity names its execution host. An id/path selector
# can resolve to an SSH checkout even when the Orca server itself is local.
_LOCAL_WORKTREE = re.compile(r"identity:wt2:local:[A-Za-z0-9][A-Za-z0-9._%+-]{0,255}", re.ASCII)
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,255}", re.ASCII)
_REVISION = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", re.ASCII)
_FIELDS = frozenset({"agent", "worktree", "orca_version", "model", "effort", "max_live"})


@dataclass(frozen=True)
class NativeTarget:
    agent: str
    worktree: str
    orca_version: str
    source_revision: str
    launch_model: str | None = None
    effort: str | None = None


def structural_gap(target: NativeTarget) -> str | None:
    if target.orca_version != ORCA_VERSION:
        return "version-unsupported"
    if target.agent not in AGENT_EFFORTS:
        return "driver-unsupported"
    creates_worktree = target.worktree in NEW_WORKTREES
    if not creates_worktree and _LOCAL_WORKTREE.fullmatch(target.worktree) is None:
        return "worktree-unsupported"
    if target.effort is not None and target.launch_model is None:
        return "model-required"
    if target.effort is not None and target.effort not in AGENT_EFFORTS[target.agent]:
        return "effort-unsupported"
    if target.launch_model is not None:
        if _MODEL.fullmatch(target.launch_model) is None:
            return "model-unsupported"
        if target.agent == "pi":
            provider, _, model = target.launch_model.partition("/")
            if not provider or not model:
                return "model-unsupported"
        if target.agent == "opencode" and creates_worktree:
            return "worktree-unsupported"
    return None


def _declarations(roster: roster_mod.Roster) -> Mapping[str, Mapping[str, object]]:
    return roster.orca_targets


def _driver_adapter(target: NativeTarget) -> str | None:
    return target.agent if target.agent in AGENT_EFFORTS else None


def _required(binding: Mapping[str, object], key: str) -> str:
    value = binding.get(key)
    if not isinstance(value, str) or not value or len(value) > 521 or value != value.strip():
        raise TargetUnresolved(missing=False)
    return value


def _resolve_target(roster: roster_mod.Roster, seat: str, source_revision: str) -> NativeTarget:
    binding = roster.orca_targets.get(seat)
    if binding is None:
        raise TargetUnresolved(missing=True)
    if binding.keys() - _FIELDS or not _REVISION.fullmatch(source_revision):
        raise TargetUnresolved(missing=False)
    return NativeTarget(
        agent=_required(binding, "agent"),
        worktree=_required(binding, "worktree"),
        orca_version=_required(binding, "orca_version"),
        source_revision=source_revision,
        launch_model=_required(binding, "model") if "model" in binding else None,
        effort=_required(binding, "effort") if "effort" in binding else None,
    )


def target_for_seat(roster: roster_mod.Roster, seat: str, *, source_revision: str) -> NativeTarget:
    """Resolve the declaration at enrollment without granting launch authority."""
    try:
        target = _resolve_target(roster, seat, source_revision)
    except TargetUnresolved as exc:
        if exc.missing:
            raise NativeChildError("seat-unbound", "the seat has no declared Orca target") from None
        raise NativeChildError("invalid-target", "the Orca target declaration is invalid") from None
    require_supported_target(target)
    return target


def require_supported_target(target: NativeTarget) -> None:
    if not _REVISION.fullmatch(target.source_revision):
        raise NativeChildError("invalid-target", "the Orca source revision is invalid")
    if (gap := structural_gap(target)) is not None:
        raise NativeChildError(UNSUPPORTED, f"the Orca target is unsupported ({gap})")


HOST = NativeChildHost(
    run_kind="native-orca",
    run_prefix="orca-",
    run_noun="governed native Orca run",
    roster_digest_domain="brigade.native-orca.roster-digest.v1",
    idempotency_domain="brigade.native-orca.idempotency.v1",
    seat_digest_domain="brigade.native-orca.seat.v1",
    ack_schema="brigade.orca_ack.v1",
    finalize_schema="brigade.orca_finalize.v1",
    status_schema="brigade.orca_status.v1",
    declarations_digest_key="orca_targets",
    unsupported_code=UNSUPPORTED,
    cancel_event="task.native-cancel-requested",
    structural_gaps=STRUCTURAL_GAPS,
    declarations=_declarations,
    structural_gap=structural_gap,
    driver_adapter=_driver_adapter,
    resolve_target=_resolve_target,
)
