# tests/test_research_cmd.py
from pathlib import Path

from brigade import research_cmd
from brigade.research import registry


class StubLlm:
    def complete(self, messages, **kw):
        p = messages[0]["content"]
        if "research plan" in p.lower():
            return '{"sub_questions":["q"],"key_topics":["t"],"success_criteria":"c"}'
        if "search queries" in p.lower():
            return '["light plants"]'
        if "comprehensive enough" in p.lower():
            return "YES done"
        if "UNTRUSTED DATA" in p:
            return '{"summary":"plants use light","evidence":"x"}'
        return "## Report\nPlants use light."


def test_run_local_only_writes_artifacts(tmp_path: Path, monkeypatch):
    (tmp_path / "a.md").write_text("photosynthesis converts light to energy in plants")
    monkeypatch.setattr(research_cmd, "_resolve_backend", lambda target: StubLlm())
    rid = research_cmd.run(target=tmp_path, question="how do plants make energy?",
                           sources=[str(tmp_path / "*.md")], web=False,
                           overrides={"max_rounds": 2, "min_rounds": 1, "max_time": 30},
                           run_id="20260602-120000-x")
    rec = registry.show_run(tmp_path, rid)
    assert rec["status"] == "done"
    d = registry.run_dir(tmp_path, rid)
    assert (d / "report.html").exists() and (d / "report.md").exists() and (d / "handoff.md").exists()
    assert "Plants use light" in (d / "report.md").read_text()


def test_web_flag_without_playwright_records_blocker(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(research_cmd, "_resolve_backend", lambda target: StubLlm())
    from brigade.research.sources import web as webmod
    monkeypatch.setattr(webmod, "_import_playwright", lambda: None)
    rid = research_cmd.run(target=tmp_path, question="q", sources=[], web=True,
                           overrides={"max_rounds": 1, "min_rounds": 1, "max_time": 20},
                           run_id="20260602-120001-x")
    rec = registry.show_run(tmp_path, rid)
    assert rec["status"] in ("done", "error")
    assert any("playwright" in b.lower() for b in rec.get("blockers", []))
