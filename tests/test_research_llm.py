# tests/test_research_llm.py
from __future__ import annotations

import pytest

from brigade.research import llm
from brigade.research.llm import PhaseBackend, ResearchSeatError, resolve_lane
from brigade.roster import Agent, Roster
from brigade.run_seat import SeatResult


class FakeInvoker:
    def __init__(self, result: SeatResult) -> None:
        self.result = result
        # PhaseBackend reads requested_model from the invoker roster.
        self.roster = Roster(
            orchestrator="chef",
            agents={
                "gemini": Agent(
                    name="gemini",
                    cli="oracle",
                    role="researcher",
                    model="gemini-3.1-pro",
                ),
            },
        )

    def invoke(self, **_kwargs: object) -> SeatResult:
        return self.result


def test_phase_backend_raises_typed_worker_failure() -> None:
    invoker = FakeInvoker(
        SeatResult(
            seat="gemini",
            attempt_id="research-1:0001",
            text="",
            ok=False,
            failure_phase="transport",
            failure_kind="browser-auth",
            detail="browser profile is not authenticated",
            requested_model="gemini-3.1-pro",
            observed_model="unverified",
            attempts=(),
        )
    )
    backend = PhaseBackend(invoker=invoker, seat="gemini", phase="research.synthesize")

    with pytest.raises(ResearchSeatError) as caught:
        backend.complete([{"role": "user", "content": "write"}])

    assert caught.value.failure_kind == "browser-auth"
    assert caught.value.seat == "gemini"


class FakeAgent:
    def __init__(
        self, name, cli=None, endpoint=None, model=None, role="researcher", headers=None, timeout_seconds=None
    ):
        self.name, self.cli, self.endpoint, self.model, self.role, self.headers = (
            name,
            cli,
            endpoint,
            model,
            role,
            headers,
        )
        self.timeout_seconds = timeout_seconds


class FakeRoster:
    def __init__(self, agents):
        self._a = agents

    def find_role(self, role):
        return next((a for a in self._a if a.role == role), None)


def test_resolve_http_backend(monkeypatch):
    r = FakeRoster([FakeAgent("api", endpoint="http://x/v1", model="m", role="researcher")])
    captured = {}

    def fake_post(url, payload, headers, timeout):
        captured["url"] = url
        return {"choices": [{"message": {"content": "http-answer"}}]}

    monkeypatch.setattr(llm, "_http_post_json", fake_post)
    backend = llm.resolve_backend(r)
    assert backend.complete([{"role": "user", "content": "hi"}]) == "http-answer"
    assert captured["url"].endswith("/chat/completions")


def test_no_researcher_raises():
    with pytest.raises(llm.NoResearcherError):
        llm.resolve_backend(FakeRoster([]))


def test_resolve_lane_uses_profile_candidates_in_order() -> None:
    roster = Roster(
        orchestrator="chef",
        agents={
            "chef": Agent(name="chef", cli="codex", role="orchestrator"),
            "gemini_browser": Agent(
                name="gemini_browser",
                cli="oracle",
                role="researcher",
                capabilities=("research.synthesize",),
            ),
            "luna": Agent(
                name="luna",
                cli="codex",
                role="researcher",
                capabilities=("research.synthesize", "research.review"),
            ),
        },
    )

    resolved = resolve_lane(
        roster,
        phase="research.synthesize",
        candidates=("gemini_browser", "luna"),
    )

    assert resolved.primary == "gemini_browser"
    assert resolved.fallbacks == ("luna",)
    assert resolved.resolution == "profile"


def test_resolve_lane_capability_ranks_installed_oracle_first(monkeypatch) -> None:
    monkeypatch.setattr(llm.shutil, "which", lambda name: "/usr/bin/oracle" if name == "oracle" else None)
    roster = Roster(
        orchestrator="chef",
        agents={
            "chef": Agent(name="chef", cli="codex", role="orchestrator"),
            "luna": Agent(
                name="luna",
                cli="codex",
                role="researcher",
                capabilities=("research.synthesize",),
                read_only_capable=True,
            ),
            "gemini_browser": Agent(
                name="gemini_browser",
                cli="oracle",
                role="researcher",
                capabilities=("research.synthesize",),
                read_only_capable=True,
            ),
        },
    )

    resolved = resolve_lane(roster, phase="research.synthesize", candidates=())

    assert resolved.primary == "gemini_browser"
    assert resolved.fallbacks == ("luna",)
    assert resolved.resolution == "capability"


def test_resolve_lane_compatibility_via_research_general() -> None:
    roster = Roster(
        orchestrator="chef",
        agents={
            "chef": Agent(name="chef", cli="codex", role="orchestrator"),
            "legacy": Agent(name="legacy", cli="codex", role="researcher"),
        },
    )

    resolved = resolve_lane(roster, phase="research.plan", candidates=())

    assert resolved.primary == "legacy"
    assert resolved.fallbacks == ()
    assert resolved.resolution == "compatibility"


def test_resolve_lane_prefers_explicit_phase_capability_over_compatibility() -> None:
    roster = Roster(
        orchestrator="chef",
        agents={
            "chef": Agent(name="chef", cli="codex", role="orchestrator"),
            "legacy": Agent(name="legacy", cli="codex", role="researcher"),
            "specialist": Agent(
                name="specialist",
                cli="codex",
                role="researcher",
                capabilities=("research.plan",),
            ),
        },
    )

    resolved = resolve_lane(roster, phase="research.plan", candidates=())

    assert resolved.primary == "specialist"
    assert resolved.fallbacks == ("legacy",)
    assert resolved.resolution == "capability"


def test_resolve_backend_error_describes_http_requirement_not_stale_cli_hint() -> None:
    roster = FakeRoster([FakeAgent("legacy", cli="codex", role="researcher")])

    with pytest.raises(llm.NoResearcherError) as caught:
        llm.resolve_backend(roster)

    message = str(caught.value)
    assert "endpoint" in message.lower()
    assert "model" in message.lower()
    assert "either cli or" not in message.lower()


def test_resolve_lane_resolution_follows_selected_primary_only() -> None:
    """Profile-ordered candidates label resolution from the primary alone."""
    roster = Roster(
        orchestrator="chef",
        agents={
            "chef": Agent(name="chef", cli="codex", role="orchestrator"),
            "legacy": Agent(name="legacy", cli="codex", role="researcher"),
            "specialist": Agent(
                name="specialist",
                cli="codex",
                role="researcher",
                capabilities=("research.plan",),
            ),
        },
    )

    resolved = resolve_lane(
        roster,
        phase="research.plan",
        candidates=("legacy", "specialist"),
    )

    assert resolved.primary == "legacy"
    assert resolved.fallbacks == ("specialist",)
    assert resolved.resolution == "profile"
