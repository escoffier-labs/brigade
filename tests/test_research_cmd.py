# tests/test_research_cmd.py
from pathlib import Path
import json
import sys

from brigade import research_cmd
from brigade import work_cmd
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
    rid = research_cmd.run(
        target=tmp_path,
        question="how do plants make energy?",
        sources=[str(tmp_path / "*.md")],
        web=False,
        overrides={"max_rounds": 2, "min_rounds": 1, "max_time": 30},
        run_id="20260602-120000-x",
    )
    rec = registry.show_run(tmp_path, rid)
    assert rec["status"] == "done"
    d = registry.run_dir(tmp_path, rid)
    assert (d / "report.html").exists() and (d / "report.md").exists() and (d / "handoff.md").exists()
    assert "Plants use light" in (d / "report.md").read_text()


def test_web_flag_without_playwright_records_blocker(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(research_cmd, "_resolve_backend", lambda target: StubLlm())
    from brigade.research.sources import web as webmod

    monkeypatch.setattr(webmod, "_import_playwright", lambda: None)
    rid = research_cmd.run(
        target=tmp_path,
        question="q",
        sources=[],
        web=True,
        overrides={"max_rounds": 1, "min_rounds": 1, "max_time": 20},
        run_id="20260602-120001-x",
    )
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


def test_sources_payload_reports_pageforge_status(tmp_path: Path):
    (tmp_path / ".brigade").mkdir()
    (tmp_path / ".brigade" / "research.toml").write_text(
        "[search]\n"
        'research_search_provider = "pageforge"\n'
        f'pageforge_command = ["{sys.executable}", "-c", "print(\'ok\')"]\n'
        'pageforge_db_path = "/tmp/pageforge.db"\n'
    )

    payload = research_cmd.sources_payload(target=tmp_path)
    route = next(route for route in payload["routes"] if route["id"] == "pageforge")
    assert route["status"] == "ok"
    assert route["type"] == "web"
    assert route["trust"] == "web"
    assert "local cache" in route["detail"]


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
    (tmp_path / ".brigade" / "research.toml").write_text('[[source]]\nid = "antigravity"\ntype = "antigravity"\n')

    payload = research_cmd.sources_payload(target=tmp_path)
    route = next(route for route in payload["routes"] if route["id"] == "antigravity")
    assert route["status"] == "fail"
    assert "agy" in route["detail"]


def _finished_research_run(tmp_path: Path, monkeypatch, run_id: str = "20260602-120010-export") -> str:
    (tmp_path / "a.md").write_text("photosynthesis converts light to energy in plants")
    monkeypatch.setattr(research_cmd, "_resolve_backend", lambda target: StubLlm())
    return research_cmd.run(
        target=tmp_path,
        question="how do plants make energy?",
        sources=[str(tmp_path / "*.md")],
        web=False,
        overrides={"max_rounds": 1, "min_rounds": 1, "max_time": 30},
        run_id=run_id,
    )


def test_export_handoff_to_codex_inbox_records_linted_receipt(tmp_path: Path, monkeypatch):
    rid = _finished_research_run(tmp_path, monkeypatch)
    inbox = tmp_path / ".codex" / "memory-handoffs"
    inbox.mkdir(parents=True)

    payload = research_cmd.export_handoff(target=tmp_path, run_id=rid, inbox="codex")

    assert payload["status"] == "exported"
    out_path = Path(payload["path"])
    assert out_path.exists()
    text = out_path.read_text()
    assert f"- research_run_id: {rid}" in text
    assert "research_source_fingerprint" in text
    assert payload["lint"]["valid"] is True
    rec = registry.show_run(tmp_path, rid)
    assert rec["handoff_exports"][0]["path"] == str(out_path)
    status = research_cmd.handoff_status_payload(target=tmp_path)
    assert status["issue_count"] == 0
    assert status["runs"][0]["status"] == "exported"


def test_export_handoff_to_custom_inbox_records_custom_destination(tmp_path: Path, monkeypatch):
    rid = _finished_research_run(tmp_path, monkeypatch, run_id="20260602-120011-custom")
    inbox = tmp_path / "handoffs" / "custom"
    inbox.mkdir(parents=True)

    payload = research_cmd.export_handoff(target=tmp_path, run_id=rid, handoff_inbox=inbox)

    assert payload["status"] == "exported"
    assert payload["inbox"] == "custom"
    assert Path(payload["path"]).parent == inbox


def test_export_handoff_missing_inbox_blocks_without_writing(tmp_path: Path, monkeypatch):
    rid = _finished_research_run(tmp_path, monkeypatch, run_id="20260602-120012-missing")

    payload = research_cmd.export_handoff(target=tmp_path, run_id=rid, inbox="claude")

    assert payload["status"] == "blocked"
    assert any("handoff inbox missing" in blocker for blocker in payload["blockers"])
    assert not (tmp_path / ".claude").exists()
    rec = registry.show_run(tmp_path, rid)
    assert rec.get("handoff_exports") is None


def test_handoff_status_reports_stale_export_when_run_artifact_changes(tmp_path: Path, monkeypatch):
    rid = _finished_research_run(tmp_path, monkeypatch, run_id="20260602-120013-stale")
    inbox = tmp_path / ".opencode" / "memory-handoffs"
    inbox.mkdir(parents=True)
    research_cmd.export_handoff(target=tmp_path, run_id=rid, inbox="opencode")
    handoff_artifact = registry.run_dir(tmp_path, rid) / "handoff.md"
    handoff_artifact.write_text(handoff_artifact.read_text() + "\n\nAdditional finding.\n")

    status = research_cmd.handoff_status_payload(target=tmp_path)

    assert status["issue_count"] == 1
    assert status["top_issue"]["status"] == "stale-export"
    assert status["top_issue"]["suggested_next_command"] == f"brigade research export-handoff {rid} --inbox codex"


def test_research_handoffs_doctor_reports_missing_export(tmp_path: Path, monkeypatch, capsys):
    rid = _finished_research_run(tmp_path, monkeypatch, run_id="20260602-120014-doctor")

    assert research_cmd.cli_handoffs_doctor(target=tmp_path) == 1
    out = capsys.readouterr().out
    assert "research handoffs doctor:" in out
    assert f"[warn] {rid}: missing-export" in out
    assert f"brigade research export-handoff {rid} --inbox codex" in out


def test_research_handoffs_import_issues_creates_fingerprinted_work_import(tmp_path: Path, monkeypatch, capsys):
    rid = _finished_research_run(tmp_path, monkeypatch, run_id="20260602-120015-import")

    assert research_cmd.cli_handoffs_import_issues(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["candidate_count"] == 1
    assert payload["imported"] == 1
    item = work_cmd._pending_imports(tmp_path)[0]
    metadata = item["metadata"]
    assert item["source"] == "research-handoff"
    assert item["kind"] == "research"
    assert metadata["research_run_id"] == rid
    assert metadata["source_item_key"] == f"research-handoff:{rid}:missing-export"
    assert metadata["source_fingerprint"]
    assert item["acceptance"]

    assert research_cmd.cli_handoffs_import_issues(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["imported"] == 0
    assert payload["skipped"] == 1


def test_research_handoffs_import_respects_dismissed_until_changed(tmp_path: Path, monkeypatch, capsys):
    rid = _finished_research_run(tmp_path, monkeypatch, run_id="20260602-120016-dismiss")
    research_cmd.cli_handoffs_import_issues(target=tmp_path, json_output=True)
    capsys.readouterr()
    item = work_cmd._pending_imports(tmp_path)[0]
    assert work_cmd.import_dismiss(target=tmp_path, import_id=item["id"], reason="not durable") == 0
    capsys.readouterr()

    assert research_cmd.cli_handoffs_import_issues(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["imported"] == 0
    assert payload["dismissed"] == 1

    handoff_artifact = registry.run_dir(tmp_path, rid) / "handoff.md"
    handoff_artifact.write_text(handoff_artifact.read_text() + "\n\nChanged source.\n")
    assert research_cmd.cli_handoffs_import_issues(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["imported"] == 1
    imports = work_cmd._read_imports(tmp_path)
    assert len(imports) == 2
    assert imports[-1]["metadata"]["source_fingerprint"] != item["metadata"]["source_fingerprint"]


def test_sources_payload_typeless_adapter_does_not_misalign_routes(tmp_path: Path):
    """A malformed (typeless) adapter must not shift command checks onto the wrong source."""
    (tmp_path / ".brigade").mkdir()
    (tmp_path / ".brigade" / "research.toml").write_text(
        "[[source]]\n"
        'id = "broken"\n'
        'command = ["/nonexistent/never-here"]\n'
        "[[source]]\n"
        'id = "research-cli"\n'
        'type = "cli"\n'
        f'command = ["{sys.executable}", "-c", "print(\'ok\')", "{{query}}"]\n'
    )

    payload = research_cmd.sources_payload(target=tmp_path)
    route = next(route for route in payload["routes"] if route["id"] == "research-cli")
    # Misalignment would pair research-cli's safe entry with the broken raw
    # adapter's missing executable and report fail.
    assert route["status"] == "ok"
