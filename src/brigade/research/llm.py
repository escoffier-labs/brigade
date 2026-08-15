# src/brigade/research/llm.py
from __future__ import annotations

import json
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional
from urllib import request as _req

from brigade.roster import Agent, Roster
from brigade.run_seat import SeatInvoker, SeatResult


class NoResearcherError(RuntimeError):
    pass


class ResearchSeatError(RuntimeError):
    def __init__(self, result: SeatResult) -> None:
        super().__init__(result.detail or result.failure_kind or "research seat failed")
        self.seat = result.seat
        self.failure_phase = result.failure_phase or "dispatch"
        self.failure_kind = result.failure_kind or "worker-failed"
        self.result = result


@dataclass(frozen=True)
class ResolvedLane:
    phase: str
    primary: str
    fallbacks: tuple[str, ...]
    resolution: Literal["profile", "capability", "compatibility"]


class PhaseBackend:
    def __init__(self, *, invoker: SeatInvoker, seat: str, phase: str) -> None:
        self.invoker = invoker
        self.seat = seat
        self.phase = phase
        self.requested_model = invoker.roster.agents[seat].model
        self.observed_model = "unverified"
        self.last_attempt_id: str | None = None

    def complete(
        self,
        messages,
        *,
        max_tokens=2048,
        temperature=0.3,
        timeout=60,
        deadline_ceiling: bool = False,
    ) -> str:
        del max_tokens, temperature
        result = self.invoker.invoke(
            seat=self.seat,
            phase=self.phase,
            prompt=_messages_to_prompt(messages),
            timeout=timeout,
            deadline_ceiling=deadline_ceiling,
        )
        if not result.ok:
            raise ResearchSeatError(result)
        self.observed_model = result.observed_model
        self.last_attempt_id = result.attempt_id
        return result.text


def resolve_lane(
    roster: Roster,
    *,
    phase: str,
    candidates: Sequence[str] = (),
) -> ResolvedLane:
    """Resolve a research phase to a primary seat and ordered fallbacks.

    Profile candidates are tried in order and must support the phase capability.
    With no candidates, use ``Roster.find_capability`` in roster order, except
    synthesis ranks an installed, read-only-capable ``cli == "oracle"`` seat
    first. Authentication is not checked at admission.
    """
    if candidates:
        selected = tuple(name for name in candidates if name in roster.agents and roster.agents[name].supports(phase))
        if not selected:
            raise NoResearcherError(f"no profile candidate supports {phase}")
        return ResolvedLane(
            phase=phase,
            primary=selected[0],
            fallbacks=selected[1:],
            resolution="profile",
        )

    agents = roster.find_capability(phase)
    if not agents:
        raise NoResearcherError(f"no seat supports {phase}")
    agents = _rank_phase_agents(agents, phase)
    primary = agents[0]
    resolution: Literal["capability", "compatibility"] = (
        "capability" if phase in primary.capabilities else "compatibility"
    )
    names = tuple(agent.name for agent in agents)
    return ResolvedLane(phase=phase, primary=names[0], fallbacks=names[1:], resolution=resolution)


def _rank_phase_agents(agents: tuple[Agent, ...], phase: str) -> tuple[Agent, ...]:
    explicit: list[Agent] = []
    compatibility: list[Agent] = []
    for agent in agents:
        if phase in agent.capabilities:
            explicit.append(agent)
        else:
            compatibility.append(agent)
    if phase == "research.synthesize":
        return _rank_synthesis_agents(tuple(explicit)) + _rank_synthesis_agents(tuple(compatibility))
    return tuple(explicit + compatibility)


def _rank_synthesis_agents(agents: tuple[Agent, ...]) -> tuple[Agent, ...]:
    preferred: list[Agent] = []
    rest: list[Agent] = []
    for agent in agents:
        if agent.cli == "oracle" and agent.read_only_capable and shutil.which("oracle"):
            preferred.append(agent)
        else:
            rest.append(agent)
    return tuple(preferred + rest)


def _http_post_json(url: str, payload: Dict[str, Any], headers: Dict[str, str], timeout: int) -> Dict[str, Any]:
    data = json.dumps(payload).encode()
    req = _req.Request(url, data=data, method="POST", headers={"Content-Type": "application/json", **headers})
    with _req.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _messages_to_prompt(messages: List[Dict[str, str]]) -> str:
    return "\n\n".join(m.get("content", "") for m in messages)


class HttpBackend:
    def __init__(self, endpoint: str, model: str, headers: Optional[Dict[str, str]] = None) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.headers = headers or {}

    def complete(
        self, messages, *, max_tokens=2048, temperature=0.3, timeout=60, deadline_ceiling: bool = False
    ) -> str:
        del deadline_ceiling
        url = self.endpoint + "/chat/completions"
        payload = {"model": self.model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature}
        resp = _http_post_json(url, payload, self.headers, timeout)
        return resp["choices"][0]["message"]["content"]


def resolve_backend(roster: Any):
    agent = roster.find_role("researcher")
    if agent is None:
        raise NoResearcherError("roster has no agent with role 'researcher'")
    if getattr(agent, "endpoint", None) and getattr(agent, "model", None):
        return HttpBackend(agent.endpoint, agent.model, getattr(agent, "headers", None))
    raise NoResearcherError("researcher agent requires endpoint and model for HTTP backend")
