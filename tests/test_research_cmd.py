# tests/test_research_cmd.py
from pathlib import Path
import sys

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


def test_run_persists_research_manifest(tmp_path: Path, monkeypatch):
    (tmp_path / ".brigade").mkdir()
    (tmp_path / "notes.md").write_text("plants use light")
    (tmp_path / ".brigade" / "research.toml").write_text(
        "[[corpus]]\n"
        'name = "plants"\n'
        'paths = ["notes.md"]\n\n'
        "[[source]]\n"
        'id = "local-cli"\n'
        'type = "cli"\n'
        f'command = ["{sys.executable}", "-c", "print(\'cli plants\')"]\n'
    )
    monkeypatch.setattr(research_cmd, "_resolve_backend", lambda target: StubLlm())

    rid = research_cmd.run(
        target=tmp_path,
        question="how do plants make energy?",
        corpus="plants",
        sources=[str(tmp_path / "*.md")],
        web=False,
        overrides={"max_rounds": 1, "min_rounds": 1, "max_time": 30},
        run_id="20260602-120002-x",
    )

    rec = registry.show_run(tmp_path, rid)
    manifest = rec["manifest"]
    assert manifest["corpus"] == "plants"
    assert manifest["sources"] == [str(tmp_path / "*.md")]
    assert manifest["web_enabled"] is False
    assert manifest["cli_sources"] == [{"id": "local-cli", "type": "cli"}]
    assert manifest["source_adapters"][0]["command"] == Path(sys.executable).name


def test_resume_reuses_manifest_sources_web_provider_and_caps(tmp_path: Path, monkeypatch):
    registry.create_run(
        tmp_path,
        question="q",
        run_id="run-one",
        caps={"max_rounds": 4, "min_rounds": 1, "max_time": 70},
        manifest={
            "sources": ["docs/*.md"],
            "corpus": "docs",
            "web_enabled": True,
            "provider": "searxng",
        },
    )
    seen = {}

    def fake_run(**kwargs):
        seen.update(kwargs)
        return kwargs["run_id"]

    monkeypatch.setattr(research_cmd, "run", fake_run)

    assert research_cmd.resume(target=tmp_path, run_id="run-one", overrides={"max_rounds": 2}) == "run-one"
    assert seen["sources"] == ["docs/*.md"]
    assert seen["corpus"] == "docs"
    assert seen["web"] is True
    assert seen["provider"] == "searxng"
    assert seen["overrides"]["max_rounds"] == 2
    assert seen["overrides"]["max_time"] == 70
    assert seen["overrides"]["_resume"] is True


def test_run_with_cli_source_adapter_records_cli_finding(tmp_path: Path, monkeypatch):
    (tmp_path / ".brigade").mkdir()
    (tmp_path / ".brigade" / "research.toml").write_text(
        "[[source]]\n"
        'id = "research-cli"\n'
        'type = "cli"\n'
        f'command = ["{sys.executable}", "-c", "print(\'photosynthesis cli source says plants use light\')"]\n'
    )
    monkeypatch.setattr(research_cmd, "_resolve_backend", lambda target: StubLlm())

    rid = research_cmd.run(
        target=tmp_path,
        question="how do plants make energy?",
        sources=[],
        web=False,
        overrides={"max_rounds": 1, "min_rounds": 1, "max_time": 30},
        run_id="20260602-120003-x",
    )

    report = (registry.run_dir(tmp_path, rid) / "report.html").read_text()
    assert "Sources - Configured CLI" in report
    assert "cli://research-cli/" in report


def test_sources_payload_reports_configured_cli_source(tmp_path: Path):
    (tmp_path / ".brigade").mkdir()
    (tmp_path / ".brigade" / "research.toml").write_text(
        "[[source]]\n"
        'id = "research-cli"\n'
        'type = "cli"\n'
        f'command = ["{sys.executable}", "-c", "print(\'ok\')", "{{query}}"]\n'
    )

    payload = research_cmd.sources_payload(target=tmp_path)
    route = next(route for route in payload["routes"] if route["id"] == "research-cli")
    assert route["status"] == "ok"
    assert route["type"] == "cli"
    assert route["accepts_query"] is True


def test_antigravity_source_adapter_is_cli_lane(tmp_path: Path):
    (tmp_path / ".brigade").mkdir()
    (tmp_path / ".brigade" / "research.toml").write_text(
        "[[source]]\n"
        'id = "antigravity"\n'
        'type = "antigravity"\n'
        f'command = ["{sys.executable}", "-c", "print(\'agy research\')", "{{query}}"]\n'
    )

    payload = research_cmd.sources_payload(target=tmp_path)
    route = next(route for route in payload["routes"] if route["id"] == "antigravity")
    assert route["status"] == "ok"
    assert route["type"] == "antigravity"
    assert route["trust"] == "cli"


def test_antigravity_source_without_command_reports_actionable_failure(tmp_path: Path):
    (tmp_path / ".brigade").mkdir()
    (tmp_path / ".brigade" / "research.toml").write_text(
        "[[source]]\n"
        'id = "antigravity"\n'
        'type = "antigravity"\n'
    )

    payload = research_cmd.sources_payload(target=tmp_path)
    route = next(route for route in payload["routes"] if route["id"] == "antigravity")
    assert route["status"] == "fail"
    assert "agy" in route["detail"]
