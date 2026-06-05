# tests/test_research_engine.py
from brigade.research.engine import DeepResearcher
from brigade.research.types import Caps, Finding

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
        return [{"source": "/n/a.md", "title": "a.md",
                 "text": "photosynthesis text", "trust": "local"}]

class StubBrowser:
    trust = "browser"
    def search(self, query, limit):
        return [{"url": "https://example.test/page", "title": "Example"}]
    def fetch(self, url):
        return {"success": True, "content": "browser page about photosynthesis", "title": "Example"}

def test_local_only_run_produces_report_and_findings():
    eng = DeepResearcher(llm=StubLlm(), local_index=StubIndex(), web=None,
                         caps=Caps.build(max_rounds=2, min_rounds=1, max_time=30))
    result = eng.research("how do plants make energy?")
    assert "Plants convert light" in result.report
    assert any(f.trust == "local" for f in result.findings)
    assert result.stats["rounds"] >= 1

def test_cancel_stops_early():
    eng = DeepResearcher(llm=StubLlm(), local_index=StubIndex(), web=None,
                         caps=Caps.build(max_rounds=5, min_rounds=1, max_time=30))
    eng.cancel()
    result = eng.research("q")
    assert result.stats["rounds"] == 0

def test_checkpoint_callback_invoked():
    seen = []
    eng = DeepResearcher(llm=StubLlm(), local_index=StubIndex(), web=None,
                         caps=Caps.build(max_rounds=2, min_rounds=1, max_time=30),
                         on_checkpoint=lambda cp: seen.append(cp))
    eng.research("q")
    assert seen and "round" in seen[-1]

def test_browser_provider_trust_is_preserved():
    eng = DeepResearcher(llm=StubLlm(), local_index=None, web=StubBrowser(),
                         caps=Caps.build(max_rounds=1, min_rounds=1, max_time=30))
    result = eng.research("how do plants make energy?")
    assert any(f.trust == "browser" for f in result.findings)
