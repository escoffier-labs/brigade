# src/brigade/research/llm.py
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from urllib import request as _req

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


class PhaseBackend:
    def __init__(self, *, invoker: SeatInvoker, seat: str, phase: str) -> None:
        self.invoker = invoker
        self.seat = seat
        self.phase = phase
        self.requested_model = invoker.roster.agents[seat].model
        self.observed_model = "unverified"
        self.last_attempt_id: str | None = None

    def complete(self, messages, *, max_tokens=2048, temperature=0.3, timeout=60) -> str:
        del max_tokens, temperature
        result = self.invoker.invoke(
            seat=self.seat,
            phase=self.phase,
            prompt=_messages_to_prompt(messages),
            timeout=timeout,
        )
        if not result.ok:
            raise ResearchSeatError(result)
        self.observed_model = result.observed_model
        self.last_attempt_id = result.attempt_id
        return result.text


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

    def complete(self, messages, *, max_tokens=2048, temperature=0.3, timeout=60) -> str:
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
    raise NoResearcherError("researcher agent needs either cli or endpoint+model")
