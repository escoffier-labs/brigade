# tests/test_research_engine.py
from brigade.research.engine import DeepResearcher
from brigade.research.types import Caps


class StubLlm:
    """Deterministic responses keyed by a marker in the prompt."""

    def complete(self, messages, **kw):
        p = messages[0]["content"]
        if "research strategist" in p or "research plan" in p.lower():
            return '{"sub_questions": ["q1"], "key_topics": ["t"], "success_criteria": "c"}'
        if "search queries" in p.lower():
            return '["light energy plants"]'
        if "comprehensive enough" in p.lower():
            return "YES - covered."
        if "UNTRUSTED DATA" in p:
            return '{"summary": "plants convert light", "evidence": "chloroplast"}'
        return "## Report\nPlants convert light to energy [/n/a.md]."


class StubIndex:
    num_chunks = 1

    def search(self, query, limit=5):
        return [{"source": "/n/a.md", "title": "a.md", "text": "photosynthesis text", "trust": "local"}]


class StubBrowser:
    trust = "browser"

    def search(self, query, limit):
        return [{"url": "https://example.test/page", "title": "Example"}]

    def fetch(self, url):
        return {"success": True, "content": "browser page about photosynthesis", "title": "Example"}


def test_local_only_run_produces_report_and_findings():
    eng = DeepResearcher(
        llm=StubLlm(), local_index=StubIndex(), web=None, caps=Caps.build(max_rounds=2, min_rounds=1, max_time=30)
    )
    result = eng.research("how do plants make energy?")
    assert "Plants convert light" in result.report
    assert any(f.trust == "local" for f in result.findings)
    assert result.stats["rounds"] >= 1


def test_cancel_stops_early():
    eng = DeepResearcher(
        llm=StubLlm(), local_index=StubIndex(), web=None, caps=Caps.build(max_rounds=5, min_rounds=1, max_time=30)
    )
    eng.cancel()
    result = eng.research("q")
    assert result.stats["rounds"] == 0


def test_checkpoint_callback_invoked():
    seen = []
    eng = DeepResearcher(
        llm=StubLlm(),
        local_index=StubIndex(),
        web=None,
        caps=Caps.build(max_rounds=2, min_rounds=1, max_time=30),
        on_checkpoint=lambda cp: seen.append(cp),
    )
    eng.research("q")
    assert seen and "round" in seen[-1]


def test_browser_provider_trust_is_preserved():
    eng = DeepResearcher(
        llm=StubLlm(), local_index=None, web=StubBrowser(), caps=Caps.build(max_rounds=1, min_rounds=1, max_time=30)
    )
    result = eng.research("how do plants make energy?")
    assert any(f.trust == "browser" for f in result.findings)


def test_phase_backend_forwards_engine_timeout_and_seat_invoker_applies_roster_floor(
    monkeypatch, tmp_path
):
    """Engine-requested timeout reaches SeatInvoker; roster floor lifts it.

    engine._plan() still asks for timeout=30. A browser-driven seat declaring
    timeout_seconds=300 must see the floored value on the dispatched agent.
    """
    from pathlib import Path

    from brigade.proc import ProcessRegistry
    from brigade.research.llm import PhaseBackend
    from brigade.roster import Agent, Roster
    from brigade.run_seat import SeatInvoker
    from brigade.run_transport import WorkerResult

    captured: dict[str, object] = {}

    def fake_dispatch(assignments, roster, **kwargs):
        del assignments, kwargs
        captured["timeout_seconds"] = roster.agents["luna"].timeout_seconds
        return [
            WorkerResult(
                worker="luna",
                task="research.plan",
                text='{"sub_questions": ["q1"]}',
                ok=True,
                detail="",
            )
        ]

    monkeypatch.setattr("brigade.run_seat.run_transport.dispatch", fake_dispatch)
    roster = Roster(
        orchestrator="chef",
        agents={
            "chef": Agent(name="chef", cli="codex", role="orchestrator"),
            "luna": Agent(
                name="luna",
                cli="codex",
                role="researcher",
                model="gpt-5.6-luna",
                timeout_seconds=300.0,
                capabilities=("research.plan",),
            ),
        },
    )
    invoker = SeatInvoker(
        roster=roster,
        cwd=tmp_path,
        run_id="research-timeout",
        process_registry=ProcessRegistry(),
        output_dir=Path(tmp_path) / ".brigade" / "runs" / "research-timeout",
    )
    backend = PhaseBackend(invoker=invoker, seat="luna", phase="research.plan")

    text = backend.complete([{"role": "user", "content": "plan"}], timeout=30)

    assert text == '{"sub_questions": ["q1"]}'
    # 30 is the engine planning literal; without the SeatInvoker floor this stays 30.
    assert captured["timeout_seconds"] == 300.0
