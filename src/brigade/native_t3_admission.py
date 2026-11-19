"""Seat admission for one governed native T3 claim.

Brigade is the admission authority for governed T3, because T3 Code keeps no
admission ledger. This module re-applies the predicates the brigade route
already uses, against the current roster and, when this node is enrolled, the
live Hub policy. It is pure over its inputs so ``brigade roster ready
--route t3`` can reuse it later.

A retained target, a matched declaration or a readable policy grant nothing on
their own. Only a granted claim consumes the predicates, and only under the
capacity and run locks held by ``native_t3_dispatch.claim``.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from . import agents as agents_mod
from . import fleet_model_roster, roster as roster_mod
from . import native_t3_enrollment as enrollment
from . import native_t3_structural as structural
from .native_t3_contract import NativeTarget

ADMITTED = "admitted"
DENIED = "admission-denied"
UNAVAILABLE = "admission-unavailable"

SEAT_UNKNOWN = "seat-unknown"
SEAT_ORCHESTRATOR = "seat-orchestrator"
TARGET_MISSING = "target-missing"
TARGET_INVALID = "target-invalid"
TARGET_CHANGED = "target-changed"
# The three structural tokens are shared with the closed classifier, so a
# claim can report exactly what `t3 open` refuses.
TARGET_MODE_INVALID = structural.TARGET_MODE_INVALID
TARGET_REMOTE_UNSUPPORTED = structural.TARGET_REMOTE_UNSUPPORTED
DRIVER_UNSUPPORTED = structural.DRIVER_UNSUPPORTED
STRUCTURAL_REASONS = structural.STRUCTURAL_GAPS
ADAPTER_NOT_ALLOWED = "adapter-not-allowed"
DRIVER_MISMATCH = "driver-mismatch"
MODEL_RETIRED = "model-retired"
POLICY_UNAVAILABLE = "policy-unavailable"
POLICY_CONSUMER_MISSING = "policy-consumer-missing"
POLICY_DENIED = "policy-denied"

_ROSTER_DOMAIN = "brigade.native-t3.roster-digest.v1"


@dataclass(frozen=True)
class HubAuthority:
    """One reading of the live Hub policy, or why it is not usable.

    ``state`` is ``unenrolled`` only when no Hub URL is configured and no
    activation was observed, ``live`` for a readable policy document, and
    ``unavailable`` or ``consumer-missing`` when a required authority is
    absent. A last-known-good roster is never substituted for live policy, so
    every non-live state refuses a claim.
    """

    state: str
    document: Mapping[str, Any] | None = None
    resolution: Mapping[str, Any] | None = None
    digest: str | None = None


@dataclass(frozen=True)
class Admission:
    """A pure admission verdict carrying digests-only evidence."""

    code: str
    reason: str
    roster_digest: str
    policy_digest: str | None

    @property
    def admitted(self) -> bool:
        return self.code == ADMITTED


def _digest(material: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        material, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False, default=str
    )
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def roster_digest(roster: roster_mod.Roster) -> str:
    """Digest the admission-relevant roster declaration, never its text."""
    return _digest(
        {
            "domain": _ROSTER_DOMAIN,
            "orchestrator": roster.orchestrator,
            "max_workers": roster.max_workers,
            "allow_models": list(roster.allow_models),
            "seats": {name: {"cli": agent.cli, "model": agent.model} for name, agent in sorted(roster.agents.items())},
            "t3_targets": {name: declaration for name, declaration in sorted(roster.t3_targets.items())},
        }
    )


def _verdict(code: str, reason: str, roster: roster_mod.Roster, hub: HubAuthority) -> Admission:
    return Admission(code=code, reason=reason, roster_digest=roster_digest(roster), policy_digest=hub.digest)


def _target_refusal(roster: roster_mod.Roster, seat: str, retained: NativeTarget) -> tuple[str, str] | None:
    """Resolve the current declaration, classify it, then require it to equal the retained target."""
    try:
        declared = enrollment._declared_target(roster, seat, source_revision=retained.source_revision)
    except enrollment.EnrollmentError as exc:
        reason = TARGET_MISSING if exc.code == "seat-unbound" else TARGET_INVALID
        return DENIED, reason
    gap = structural.structural_gap(declared)
    if gap is not None:
        # A declaration edited after open into a target T3 cannot run is a
        # structural gap, not a plain target change.
        return DENIED, gap
    if declared != retained:
        # NativeTarget equality covers every field, including canonical options.
        return DENIED, TARGET_CHANGED
    return None


def _model_refusal(roster: roster_mod.Roster, seat: str, retained: NativeTarget) -> str | None:
    agent = roster.agents[seat]
    adapter = structural.DRIVER_ADAPTERS.get(retained.driver)
    if adapter is None:
        return DRIVER_UNSUPPORTED
    if (
        agent.cli is None
        or not roster_mod.is_cli_allowed(agent.cli, roster)
        or not roster_mod.is_cli_allowed(adapter, roster)
    ):
        return ADAPTER_NOT_ALLOWED
    driver_provider = agents_mod.model_policy_provider(adapter)
    seat_provider = agents_mod.model_policy_provider(agent.cli)
    if seat_provider != driver_provider:
        # The launch provider comes from the target driver. A seat bound to a
        # different provider family must not claim with this target.
        return DRIVER_MISMATCH
    # Normalize the launch model through the adapter that will run it, so a
    # case variant cannot escape the retired-model families. Keep the seat
    # model check as well.
    models = (
        agents_mod.model_policy_model(adapter, retained.launch_model),
        agents_mod.model_policy_model(agent.cli, agent.model),
    )
    return MODEL_RETIRED if any(fleet_model_roster.retired_reason(driver_provider, model) for model in models) else None


def evaluate(
    roster: roster_mod.Roster,
    seat: str,
    retained: NativeTarget,
    *,
    hub: HubAuthority,
) -> Admission:
    """Decide whether one seat may claim, without side effects."""
    if seat not in roster.agents:
        return _verdict(DENIED, SEAT_UNKNOWN, roster, hub)
    if seat == roster.orchestrator:
        return _verdict(DENIED, SEAT_ORCHESTRATOR, roster, hub)
    retained_gap = structural.structural_gap(retained)
    if retained_gap is not None:
        # A retained target from before the native mode contract, or one whose
        # driver or locality T3 cannot run, is never rewritten. It refuses
        # until the operator re-enrolls, and the reason is the structural token.
        return _verdict(DENIED, retained_gap, roster, hub)
    refusal = _target_refusal(roster, seat, retained)
    if refusal is not None:
        return _verdict(*refusal, roster, hub)
    model_refusal = _model_refusal(roster, seat, retained)
    if model_refusal is not None:
        return _verdict(DENIED, model_refusal, roster, hub)
    if hub.state == "unenrolled":
        return _verdict(ADMITTED, "local-roster", roster, hub)
    if hub.state == "consumer-missing":
        return _verdict(DENIED, POLICY_CONSUMER_MISSING, roster, hub)
    # Only live policy authorizes a claim. Any other state, including a
    # last-known-good reading, refuses as unavailable.
    if hub.state != "live" or hub.document is None or hub.resolution is None:
        return _verdict(UNAVAILABLE, POLICY_UNAVAILABLE, roster, hub)
    from . import fleet_policy

    try:
        verdict = fleet_policy.admissible_seat(hub.document, seat, hub.resolution)
    except (fleet_policy.FleetPolicyError, ValueError, KeyError, TypeError):
        return _verdict(UNAVAILABLE, POLICY_UNAVAILABLE, roster, hub)
    if not verdict.get("admissible"):
        return _verdict(DENIED, POLICY_DENIED, roster, hub)
    return _verdict(ADMITTED, "hub-policy", roster, hub)


def load_roster(workspace: Path) -> roster_mod.Roster:
    """Load the roster that currently applies to the workspace."""
    return enrollment._load_roster(workspace, None)


def read_hub_authority(workspace: Path) -> HubAuthority:
    """Read live Hub policy authority, or a fixed reason it is not usable.

    A configured Hub URL is enough to require live policy, even when no
    enrollment high-water file exists. A missing or corrupt high-water file
    collapses to no observed activation, so it can never unenroll a node that
    points at a Hub. Only no Hub URL and no observed activation is
    ``unenrolled``. The cached roster is deliberately never consulted, so a
    last-known-good reading cannot authorize a governed claim.
    """
    from . import (
        fleet_client,
        fleet_client_policy,
        fleet_model_admission,
        fleet_policy,
        fleet_session_bootstrap,
        fleet_session_presence,
    )

    try:
        settings = fleet_client.load_fleet_settings(strict=True)
    except (fleet_client.FleetClientError, OSError, ValueError):
        return HubAuthority(state="unavailable")
    hub_url = settings.get("hub_url", "").strip()
    try:
        activated = fleet_model_admission.enrollment_activation_observed(
            lock_timeout=fleet_client.CLOUD_TIMEOUT_SECONDS
        )
    except (fleet_client.FleetClientError, OSError, RuntimeError, ValueError):
        return HubAuthority(state="unavailable")
    if not hub_url and not activated:
        return HubAuthority(state="unenrolled")
    try:
        payload = fleet_client_policy.show_policy()
        raw = payload.get("document") if isinstance(payload, Mapping) else None
        if not isinstance(raw, Mapping):
            raise ValueError("fleet policy response is missing document")
        parsed = fleet_policy.parse_document(raw)
        if fleet_session_bootstrap.CONSUMER_BRIGADE_RUN not in parsed["consumers"]:
            return HubAuthority(state="consumer-missing")
        repository = fleet_session_presence.repository_identity(workspace).value
        resolution = fleet_policy.resolve_policy(
            raw, fleet_session_bootstrap.CONSUMER_BRIGADE_RUN, repository, _parsed=parsed
        )
        digest = fleet_policy.document_digest(parsed)
    except (
        fleet_policy.FleetPolicyError,
        fleet_client.FleetClientError,
        subprocess.SubprocessError,
        OSError,
        RuntimeError,
        ValueError,
        KeyError,
        TypeError,
    ):
        return HubAuthority(state="unavailable")
    return HubAuthority(state="live", document=parsed, resolution=resolution, digest=digest)
