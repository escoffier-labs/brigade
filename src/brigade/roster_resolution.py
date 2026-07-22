"""Resolve roster seats against host capabilities and fallback chains."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from . import agents as agent_adapters
from . import acpx_adapter
from .roster import Agent, Roster

SeatStatus = Literal["resolved", "dropped"]


@dataclass(frozen=True)
class SeatResolutionEntry:
    requested: str
    selected: str | None
    status: SeatStatus
    reason: str
    fallback_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class SeatResolutionReport:
    entries: tuple[SeatResolutionEntry, ...]


class CapabilityLookup(Protocol):
    def check_requirements(self, agent: Agent) -> tuple[bool, str]:
        """Return whether the seat's declared requirements are satisfied and why."""


@dataclass(frozen=True)
class DefaultCapabilityLookup:
    """Probe installed CLIs and auth using the same preflights as roster doctor."""

    cli_detect: Callable[[str], bool] = agent_adapters.detect
    ollama_model_present: Callable[[str], tuple[bool, str]] = agent_adapters.ollama_model_present
    cursor_auth_status: Callable[[], acpx_adapter.CursorAuthStatus] = acpx_adapter.cursor_auth_status

    def check_requirements(self, agent: Agent) -> tuple[bool, str]:
        if agent.requires is None or (agent.requires.cli is None and agent.requires.auth is None):
            return True, "seat has no hard requirements"

        req = agent.requires
        if req.cli == "ollama":
            if agent.cli is None or not agent.cli.startswith("ollama:"):
                return False, "seat requires ollama but agents.{name}.cli is not ollama:<model>".replace(
                    "{name}", agent.name
                )
            model = agent.cli[len("ollama:") :]
            present, detail = self.ollama_model_present(model)
            if not present:
                return False, detail or f"ollama model {model!r} is not available locally"
            return True, f"ollama model {model!r} is pulled locally"

        if req.cli == "cursor":
            if not self.cli_detect("cursor"):
                return False, "cursor CLI is not installed"
            if req.auth == "logged-in":
                auth = self.cursor_auth_status()
                if auth.state != "authenticated":
                    return False, auth.detail or "cursor CLI is not authenticated"
                return True, "cursor CLI is installed and authenticated"
            return True, "cursor CLI is installed"

        if req.cli is not None:
            if not self.cli_detect(req.cli):
                return False, f"{req.cli} CLI is not installed"
            if req.auth == "logged-in":
                return False, f"no authentication probe is available for {req.cli} CLI"
            return True, f"{req.cli} CLI is installed"

        if req.auth is not None:
            return False, f"requires.auth={req.auth!r} needs requires.cli to probe authentication"

        return True, "requirements satisfied"


def resolve_seats(
    roster: Roster,
    *,
    seat_names: Sequence[str] | None = None,
    lookup: CapabilityLookup | None = None,
) -> SeatResolutionReport:
    """Resolve an ordered seat list to the first satisfiable seat per entry."""

    if seat_names is None:
        seat_names = [name for name in roster.agents if name != roster.orchestrator]
    capability = lookup or DefaultCapabilityLookup()
    return SeatResolutionReport(entries=tuple(_resolve_one(roster, name, capability) for name in seat_names))


def _resolve_one(roster: Roster, requested: str, lookup: CapabilityLookup) -> SeatResolutionEntry:
    agent = roster.agents.get(requested)
    if agent is None:
        return SeatResolutionEntry(
            requested=requested,
            selected=None,
            status="dropped",
            reason=f"seat {requested!r} is not defined in the roster",
            fallback_reasons=(),
        )

    if agent.requires is None or (agent.requires.cli is None and agent.requires.auth is None):
        return SeatResolutionEntry(
            requested=requested,
            selected=requested,
            status="resolved",
            reason="seat has no hard requirements",
            fallback_reasons=(),
        )

    satisfied, reason = lookup.check_requirements(agent)
    if satisfied:
        return SeatResolutionEntry(
            requested=requested,
            selected=requested,
            status="resolved",
            reason=reason,
            fallback_reasons=(),
        )

    fallback_reasons = [reason]
    for fallback_name in agent.fallback:
        fallback_agent = roster.agents.get(fallback_name)
        if fallback_agent is None:
            fallback_reasons.append(f"fallback {fallback_name!r} is not defined in the roster")
            continue
        fb_satisfied, fb_reason = lookup.check_requirements(fallback_agent)
        if fb_satisfied:
            return SeatResolutionEntry(
                requested=requested,
                selected=fallback_name,
                status="resolved",
                reason=fb_reason,
                fallback_reasons=tuple(fallback_reasons),
            )
        fallback_reasons.append(fb_reason)

    return SeatResolutionEntry(
        requested=requested,
        selected=None,
        status="dropped",
        reason=f"no satisfiable fallback for seat {requested!r}",
        fallback_reasons=tuple(fallback_reasons),
    )
