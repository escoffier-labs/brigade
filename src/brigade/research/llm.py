# src/brigade/research/llm.py
from __future__ import annotations
import json
from typing import Any, Dict, List, Optional
from urllib import request as _req


class NoResearcherError(RuntimeError):
    pass


def _run_cli(cli: str, prompt: str, timeout: int, model: Optional[str] = None) -> str:
    from brigade import agents  # same dispatch brigade run uses

    return agents.run_agent(cli, prompt, timeout=timeout, model=model)


def _http_post_json(url: str, payload: Dict[str, Any], headers: Dict[str, str], timeout: int) -> Dict[str, Any]:
    data = json.dumps(payload).encode()
    req = _req.Request(url, data=data, method="POST", headers={"Content-Type": "application/json", **headers})
    with _req.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _messages_to_prompt(messages: List[Dict[str, str]]) -> str:
    return "\n\n".join(m.get("content", "") for m in messages)


class CliBackend:
    def __init__(self, cli: str, model: Optional[str] = None) -> None:
        self.cli = cli
        self.model = model

    def complete(self, messages, *, max_tokens=2048, temperature=0.3, timeout=60) -> str:
        return _run_cli(self.cli, _messages_to_prompt(messages), timeout, model=self.model)


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
    if getattr(agent, "cli", None):
        return CliBackend(agent.cli, getattr(agent, "model", None))
    raise NoResearcherError("researcher agent needs either cli or endpoint+model")
