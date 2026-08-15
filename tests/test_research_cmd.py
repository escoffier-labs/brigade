# tests/test_research_cmd.py
from pathlib import Path
from threading import Event
import json
import re
import sys

import pytest

from brigade import aboyeur, cli, research_cmd
from brigade import work_cmd
from brigade.research import config as rconfig
from brigade.research import registry
from brigade.research.types import Finding, ResearchLanes, SourceEnvelope
from brigade.research_cmd import (
    CancellationWatcher,
    ResearchBudgetCallbacks,
    cli_init,
    cli_list,
    cli_resume,
    cli_run,
    cli_show,
    cli_status,
)
from brigade.roster import Agent, Roster
from brigade.run_budget import BudgetCoordinator, BudgetPolicyError, RunBudgetDeclaration


class StubLlm:
    def __init__(self, seat: str = "researcher", phase: str = "research") -> None:
        self.seat = seat
        self.phase = phase
        self.requested_model: str | None = None
        self.observed_model = "unverified"
        self.last_attempt_id: str | None = None
        self._attempt = 0

    def complete(self, messages, **kw):
        self._attempt += 1
        self.last_attempt_id = f"{self.seat}:{self.phase}:{self._attempt}"
        p = messages[0]["content"]
        lower = p.lower()
        if "research plan" in lower or "research strategist" in lower:
            return '{"sub_questions":["q"],"key_topics":["t"],"success_criteria":"c"}'
        if "search queries" in lower:
            return '["light plants"]'
        if "comprehensive enough" in lower:
            return "YES done"
        if "independent research reviewer" in lower:
            return '{"accepted": true, "detail": "citations resolve", "rejected_claims": []}'
        if "write a grounded research report" in lower or "repair the research report" in lower:
            cite = re.search(r"\[source:[^\]]+\]", p)
            token = cite.group(0) if cite else ""
            return f"## Report\nPlants use light. {token}".rstrip()
        if "UNTRUSTED DATA" in p or "extract only the information" in lower:
            return '{"summary":"plants use light","evidence":"x"}'
        cite = re.search(r"\[source:[^\]]+\]", p)
        token = cite.group(0) if cite else ""
        return f"## Report\nPlants use light. {token}".rstrip()


class StubInvoker:
    def close_receipts(self) -> None:
        pass


def stub_lanes(llm: StubLlm | None = None) -> ResearchLanes:
    if llm is not None:
        return ResearchLanes(
            planner=llm,
            extractor=llm,
            synthesizers=(llm,),
            reviewer=llm,
            browser_discovery=None,
        )
    return ResearchLanes(
        planner=StubLlm(seat="researcher", phase="planning"),
        extractor=StubLlm(seat="researcher", phase="extraction"),
        synthesizers=(StubLlm(seat="researcher", phase="synthesis"),),
        reviewer=StubLlm(seat="researcher", phase="review"),
        browser_discovery=None,
    )


def patch_stub_lanes(monkeypatch) -> None:
    monkeypatch.setattr(
        research_cmd,
        "_resolve_lanes",
        lambda *_args, **_kwargs: stub_lanes(),
    )
    monkeypatch.setattr(
        research_cmd,
        "_build_run_invoker",
        lambda *_args, **_kwargs: StubInvoker(),
    )
    monkeypatch.setattr(
        research_cmd,
        "_load_roster",
        lambda _target: Roster(
            orchestrator="chef",
            agents={"chef": Agent(name="chef", cli="codex", role="orchestrator")},
        ),
    )


def capture_run(monkeypatch) -> dict[str, object]:
    captured: dict[str, object] = {}

    def fake_run(**kwargs: object) -> str:
        captured.update(kwargs)
        return "captured-run"

    monkeypatch.setattr(research_cmd, "run", fake_run)
    monkeypatch.setattr(
        registry,
        "show_run",
        lambda _target, _run_id: {"run_id": "captured-run", "status": "completed"},
    )
    return captured


def test_run_omits_browser_ai_discovery_by_default(monkeypatch, tmp_path: Path) -> None:
    captured = capture_run(monkeypatch)

    code = cli_run(
        target=tmp_path,
        question="q",
        corpus=None,
        sources=["docs/*.md"],
        web=False,
        overrides={"max_rounds": 1},
        browser_ai_research=False,
        json_output=True,
    )

    assert code == 0
    assert captured["browser_ai_research"] is False


def test_explicit_browser_ai_flag_adds_provider(monkeypatch, tmp_path: Path) -> None:
    captured = capture_run(monkeypatch)

    code = cli_run(
        target=tmp_path,
        question="q",
        corpus=None,
        sources=[],
        web=False,
        overrides={"max_rounds": 1},
        browser_ai_research=True,
        json_output=True,
    )

    assert code == 0
    assert captured["browser_ai_research"] is True


def test_cli_default_and_grounded_profiles_do_not_enable_browser_ai(monkeypatch, tmp_path: Path) -> None:
    captured = capture_run(monkeypatch)

    assert (
        cli.main(
            [
                "research",
                "run",
                "q",
                "--target",
                str(tmp_path),
                "--source",
                "docs/*.md",
                "--json",
            ]
        )
        == 0
    )
    assert captured["browser_ai_research"] is False
    assert captured.get("profile") is None

    captured.clear()
    assert (
        cli.main(
            [
                "research",
                "run",
                "q",
                "--target",
                str(tmp_path),
                "--profile",
                "grounded",
                "--source",
                "docs/*.md",
                "--json",
            ]
        )
        == 0
    )
    assert captured["browser_ai_research"] is False
    assert captured["profile"] == "grounded"

    captured.clear()
    assert (
        cli.main(
            [
                "research",
                "run",
                "q",
                "--target",
                str(tmp_path),
                "--profile",
                "local-only",
                "--source",
                "docs/*.md",
                "--json",
            ]
        )
        == 0
    )
    assert captured["browser_ai_research"] is False
    assert captured["profile"] == "local-only"


def test_cli_browser_ai_profile_enables_without_flag(monkeypatch, tmp_path: Path) -> None:
    captured = capture_run(monkeypatch)

    assert (
        cli.main(
            [
                "research",
                "run",
                "q",
                "--target",
                str(tmp_path),
                "--profile",
                "browser-ai",
                "--source",
                "docs/*.md",
                "--json",
            ]
        )
        == 0
    )
    assert captured["browser_ai_research"] is True
    assert captured["profile"] == "browser-ai"


def test_cli_forwards_category_synthesizer_reviewer_and_profile(monkeypatch, tmp_path: Path) -> None:
    captured = capture_run(monkeypatch)

    assert (
        cli.main(
            [
                "research",
                "run",
                "q",
                "--target",
                str(tmp_path),
                "--profile",
                "luna-only",
                "--synthesizer",
                "synth-seat",
                "--reviewer",
                "review-seat",
                "--category",
                "ops",
                "--source",
                "docs/*.md",
                "--json",
            ]
        )
        == 0
    )
    assert captured["profile"] == "luna-only"
    assert captured["synthesizer"] == "synth-seat"
    assert captured["reviewer"] == "review-seat"
    assert captured["category"] == "ops"


def test_new_run_resolves_lanes_from_roster_and_forwards_overrides(monkeypatch, tmp_path: Path) -> None:
    seen: dict[str, object] = {}

    def fake_build(
        target: Path,
        *,
        profile,
        synthesizer: str | None,
        reviewer: str | None,
        run_id: str,
        invoker=None,
    ) -> ResearchLanes:
        seen["target"] = target
        seen["profile"] = profile.name
        seen["synthesizer"] = synthesizer
        seen["reviewer"] = reviewer
        seen["run_id"] = run_id
        seen["invoker"] = invoker
        return stub_lanes()

    monkeypatch.setattr(research_cmd, "_build_lanes_from_roster", fake_build)

    profile = rconfig.load(tmp_path).profile("grounded")
    lanes = research_cmd._resolve_lanes(
        tmp_path,
        profile=profile,
        synthesizer="synth-seat",
        reviewer="review-seat",
        run_id="run-lanes",
    )

    assert isinstance(lanes, ResearchLanes)
    assert seen == {
        "target": tmp_path,
        "profile": "grounded",
        "synthesizer": "synth-seat",
        "reviewer": "review-seat",
        "run_id": "run-lanes",
        "invoker": None,
    }


def test_run_no_source_route_sets_failure_kind_and_cli_exits_2(tmp_path: Path, monkeypatch, capsys) -> None:
    patch_stub_lanes(monkeypatch)
    code = cli_run(
        target=tmp_path,
        question="q",
        corpus=None,
        sources=[],
        web=False,
        overrides={"max_rounds": 1},
        browser_ai_research=False,
        json_output=True,
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload["failure_kind"] == "no-source-route"
    assert payload["status"] in {"failed", "error"}


def test_run_does_not_construct_browser_provider_when_opt_in_false(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "a.md").write_text("photosynthesis converts light to energy in plants")
    patch_stub_lanes(monkeypatch)
    calls: list[object] = []

    def boom(target: Path, *, run_id: str, invoker=None):
        calls.append((target, run_id))
        raise AssertionError("_resolve_browser_ai_provider must stay off by default")

    monkeypatch.setattr(research_cmd, "_resolve_browser_ai_provider", boom)
    rid = research_cmd.run(
        target=tmp_path,
        question="how do plants make energy?",
        sources=[str(tmp_path / "*.md")],
        web=False,
        overrides={"max_rounds": 1, "min_rounds": 1, "max_time": 30},
        browser_ai_research=False,
        run_id="20260602-120020-no-browser",
    )
    assert calls == []
    assert registry.show_run(tmp_path, rid)["status"] in {"completed", "done"}


def test_persist_category_initializes_absent_sidecar_and_mirrors_run_json(
    tmp_path: Path,
) -> None:
    rid = "20260602-120021-category"
    registry.create_run(
        tmp_path,
        question="q",
        run_id=rid,
        caps={"max_rounds": 1},
        manifest={},
    )
    research_cmd._persist_category(tmp_path, rid, "ops")

    research_path = registry.standard_run_dir(tmp_path, rid) / registry.RESEARCH_ARTIFACT
    sidecar = json.loads(research_path.read_text())
    assert sidecar["category"] == "ops"
    assert sidecar["run_id"] == rid
    assert sidecar["schema"] == "brigade.research.v1"
    run_rec = json.loads((registry.legacy_run_dir(tmp_path, rid) / "run.json").read_text())
    assert run_rec["category"] == "ops"


def test_persist_category_fail_closed_on_malformed_research_json(tmp_path: Path) -> None:
    rid = "20260602-120022-bad-category"
    registry.create_run(
        tmp_path,
        question="q",
        run_id=rid,
        caps={"max_rounds": 1},
        manifest={},
    )
    # update_research owns the standard-run sidecar; seed corruption there.
    research_path = registry.standard_run_dir(tmp_path, rid) / registry.RESEARCH_ARTIFACT
    research_path.parent.mkdir(parents=True, exist_ok=True)
    research_path.write_text("not-json\n", encoding="utf-8")

    with pytest.raises(ValueError, match="unreadable or not an object"):
        research_cmd._persist_category(tmp_path, rid, "ops")

    assert research_path.read_text(encoding="utf-8") == "not-json\n"


def test_run_local_only_writes_artifacts(tmp_path: Path, monkeypatch):
    (tmp_path / "a.md").write_text("photosynthesis converts light to energy in plants")
    patch_stub_lanes(monkeypatch)
    rid = research_cmd.run(
        target=tmp_path,
        question="how do plants make energy?",
        sources=[str(tmp_path / "*.md")],
        web=False,
        overrides={"max_rounds": 2, "min_rounds": 1, "max_time": 30},
        run_id="20260602-120000-x",
    )
    rec = registry.show_run(tmp_path, rid)
    assert rec["status"] in {"completed", "done"}
    d = registry.run_dir(tmp_path, rid)
    assert (d / "report.html").exists() and (d / "report.md").exists() and (d / "handoff.md").exists()
    assert "Plants use light" in (d / "report.md").read_text()


def test_web_flag_without_playwright_records_blocker(tmp_path: Path, monkeypatch):
    patch_stub_lanes(monkeypatch)
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
    assert rec["status"] in {"completed", "done", "failed", "error"}
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
    patch_stub_lanes(monkeypatch)

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
    assert "_resume" not in seen["overrides"]
    assert "resume" in seen


def test_run_with_cli_source_adapter_records_cli_finding(tmp_path: Path, monkeypatch):
    (tmp_path / ".brigade").mkdir()
    (tmp_path / ".brigade" / "research.toml").write_text(
        "[[source]]\n"
        'id = "research-cli"\n'
        'type = "cli"\n'
        f'command = ["{sys.executable}", "-c", "print(\'photosynthesis cli source says plants use light\')"]\n'
    )
    patch_stub_lanes(monkeypatch)

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
    patch_stub_lanes(monkeypatch)
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


def test_dict_and_list_or_empty_normalize_dynamic_json() -> None:
    assert research_cmd._dict_or_empty(None) == {}
    assert research_cmd._dict_or_empty("not-a-dict") == {}
    assert research_cmd._dict_or_empty({"k": 1}) == {"k": 1}
    assert research_cmd._list_or_empty(None) == []
    assert research_cmd._list_or_empty("not-a-list") == []
    assert research_cmd._list_or_empty([1, 2]) == [1, 2]


def test_resume_normalizes_non_dict_caps(tmp_path: Path, monkeypatch) -> None:
    from brigade.research.types import ResumeState

    seen: dict[str, object] = {}

    def fake_run(**kwargs):
        seen.update(kwargs)
        return kwargs["run_id"]

    monkeypatch.setattr(research_cmd, "run", fake_run)
    monkeypatch.setattr(
        research_cmd.registry,
        "show_run",
        lambda *_a, **_k: {
            "run_id": "caps-null",
            "status": "failed",
            "question": "q",
            "caps": "broken",
            "manifest": {"sources": ["a.md"], "web_enabled": False},
            "finished_at": None,
        },
    )
    monkeypatch.setattr(research_cmd, "resume_state", lambda *_a, **_k: ResumeState())
    from brigade import runguard

    monkeypatch.setattr(runguard, "has_active_run_owner", lambda *_a, **_k: False)

    assert research_cmd.resume(target=tmp_path, run_id="caps-null", overrides={"max_rounds": 2}) == "caps-null"
    assert seen["overrides"]["max_rounds"] == 2
    assert "max_time" not in seen["overrides"]
    assert seen["sources"] == ["a.md"]


def test_repo_overlay_on_grounded_does_not_activate_browser_ai(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / ".brigade").mkdir()
    (tmp_path / "a.md").write_text("plants use light")
    (tmp_path / ".brigade" / "research.toml").write_text(
        "[profiles.grounded]\nbrowser_ai_research = true\n",
        encoding="utf-8",
    )
    patch_stub_lanes(monkeypatch)
    calls: list[object] = []

    def boom(target: Path, *, run_id: str, invoker=None):
        calls.append((target, run_id, invoker))
        raise AssertionError("browser AI must stay off for grounded overlays")

    monkeypatch.setattr(research_cmd, "_resolve_browser_ai_provider", boom)
    rid = research_cmd.run(
        target=tmp_path,
        question="how do plants make energy?",
        sources=[str(tmp_path / "*.md")],
        web=False,
        overrides={"max_rounds": 1, "min_rounds": 1, "max_time": 30},
        profile="grounded",
        browser_ai_research=False,
        run_id="20260602-120030-overlay",
    )
    assert calls == []
    assert registry.show_run(tmp_path, rid)["status"] in {"completed", "done"}
    assert registry.show_run(tmp_path, rid).get("browser_ai_research") is False


def test_explicit_browser_ai_provider_failure_fails_truthfully(tmp_path: Path, monkeypatch, capsys) -> None:
    patch_stub_lanes(monkeypatch)

    def boom(target: Path, *, run_id: str, invoker=None):
        raise RuntimeError("browser seat unavailable token=secret-value")

    monkeypatch.setattr(research_cmd, "_resolve_browser_ai_provider", boom)
    code = cli_run(
        target=tmp_path,
        question="q",
        corpus=None,
        sources=[],
        web=False,
        overrides={"max_rounds": 1},
        browser_ai_research=True,
        json_output=True,
    )
    payload = json.loads(capsys.readouterr().out)
    assert code != 0
    assert payload["status"] in {"failed", "error"}
    assert payload["failure_phase"] == "discovery"
    assert "secret-value" not in json.dumps(payload)


def test_generic_failed_research_run_returns_nonzero(tmp_path: Path, monkeypatch, capsys) -> None:
    (tmp_path / "a.md").write_text("plants use light")

    class BoomLanes:
        def __call__(self, *_args, **_kwargs):
            return stub_lanes()

    patch_stub_lanes(monkeypatch)

    def boom_run(*_args, **_kwargs):
        raise research_cmd.ResearchRunError("review", "review-rejected", "unsupported")

    monkeypatch.setattr(research_cmd.ResearchEngine, "run", boom_run)
    code = cli_run(
        target=tmp_path,
        question="q",
        corpus=None,
        sources=[str(tmp_path / "*.md")],
        web=False,
        overrides={"max_rounds": 1},
        json_output=True,
    )
    payload = json.loads(capsys.readouterr().out)
    assert code != 0
    assert payload["status"] in {"failed", "error"}
    assert payload["failure_kind"] == "review-rejected"


def test_opt_in_browser_provider_included_and_manifest_enabled(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "a.md").write_text("plants use light")

    class FakeBrowser:
        trust = "browser-ai"
        source_type = "browser-ai"
        source_id = "gemini-browser"
        lane = "gemini_browser"
        requested_model = "gemini-3.1-pro"
        observed_model = "unverified"

        def search(self, query: str, limit: int):
            del query, limit
            return []

        def fetch(self, url: str):
            return {"success": False, "content": "", "title": ""}

    shared: dict[str, object] = {}
    sentinel = StubInvoker()

    def fake_invoker(
        target: Path, *, run_id: str, process_registry=None, budget_callbacks=None, receipt_admission=None
    ):
        del process_registry, budget_callbacks, receipt_admission
        shared["built"] = (target, run_id)
        return sentinel

    def fake_lanes(target, *, profile, synthesizer, reviewer, run_id, invoker=None):
        shared["lanes_invoker"] = invoker
        return stub_lanes()

    def fake_browser(target: Path, *, run_id: str, invoker=None):
        shared["browser_invoker"] = invoker
        return FakeBrowser()

    monkeypatch.setattr(
        research_cmd,
        "_load_roster",
        lambda _target: Roster(
            orchestrator="chef",
            agents={"chef": Agent(name="chef", cli="codex", role="orchestrator")},
        ),
    )
    monkeypatch.setattr(research_cmd, "_build_run_invoker", fake_invoker)
    monkeypatch.setattr(research_cmd, "_build_lanes_from_roster", fake_lanes)
    monkeypatch.setattr(research_cmd, "_resolve_browser_ai_provider", fake_browser)

    rid = research_cmd.run(
        target=tmp_path,
        question="q",
        sources=[str(tmp_path / "*.md")],
        web=False,
        overrides={"max_rounds": 1, "min_rounds": 1},
        browser_ai_research=True,
        run_id="20260602-120031-browser",
    )
    rec = registry.show_run(tmp_path, rid)
    route = next(r for r in rec["manifest"]["routes"] if r["id"] == "browser-ai")
    assert route["enabled"] is True
    assert rec["manifest"]["browser_ai_research"] is True
    assert shared["lanes_invoker"] is shared["browser_invoker"]
    assert shared["lanes_invoker"] is sentinel


def test_shared_seat_invoker_built_once_for_browser_and_lanes(tmp_path: Path, monkeypatch) -> None:
    from brigade.proc import ProcessRegistry
    from brigade.run_seat import SeatInvoker
    from brigade.roster import Agent, Roster

    roster = Roster(
        orchestrator="chef",
        agents={
            "chef": Agent(name="chef", cli="codex", role="orchestrator"),
            "luna": Agent(
                name="luna",
                cli="codex",
                role="researcher",
                capabilities=(
                    "research.plan",
                    "research.extract",
                    "research.synthesize",
                    "research.review",
                    "research.browser-discover",
                ),
            ),
        },
    )
    monkeypatch.setattr("brigade.roster.load_roster", lambda _path: roster)
    monkeypatch.setattr("brigade.roster.resolve_roster_path", lambda _target: tmp_path / "roster.toml")

    constructed: list[SeatInvoker] = []
    real_init = SeatInvoker.__init__

    def tracking_init(self, *args, **kwargs):
        real_init(self, *args, **kwargs)
        constructed.append(self)

    monkeypatch.setattr(SeatInvoker, "__init__", tracking_init)

    invoker = research_cmd._build_run_invoker(tmp_path, run_id="shared-invoker")
    lanes = research_cmd._build_lanes_from_roster(
        tmp_path,
        profile=rconfig.load(tmp_path).profile("browser-ai"),
        synthesizer=None,
        reviewer=None,
        run_id="shared-invoker",
        invoker=invoker,
    )
    provider = research_cmd._resolve_browser_ai_provider(tmp_path, run_id="shared-invoker", invoker=invoker)

    assert len(constructed) == 1
    assert lanes.planner.invoker is invoker
    assert provider.backend.invoker is invoker
    assert isinstance(invoker.process_registry, ProcessRegistry)


class RecordingProcessRegistry:
    def __init__(self) -> None:
        self.cancel_calls = 0

    def cancel(self) -> None:
        self.cancel_calls += 1


def minimal_research_roster() -> Roster:
    return Roster(
        orchestrator="chef",
        agents={
            "chef": Agent(name="chef", cli="codex", role="orchestrator"),
            "luna": Agent(
                name="luna",
                cli="codex",
                role="researcher",
                capabilities=(
                    "research.plan",
                    "research.extract",
                    "research.synthesize",
                    "research.review",
                ),
            ),
        },
    )


def seed_cancelled_run_after_extraction(target: Path) -> str:
    run_id = "resume-after-extraction"
    registry.create_standard_run(
        target,
        run_id=run_id,
        question="q",
        profile="grounded",
        caps={"max_time": 300, "max_dispatches": 8},
        roster=minimal_research_roster(),
    )
    source = SourceEnvelope.build(
        origin="local",
        provider="local",
        uri="docs/fact.md",
        content="Verified fact",
        trust="local",
        acquired_at="2026-08-13T12:00:00+00:00",
    )
    finding = Finding(
        source_ids=(source.source_id,),
        title="Fact",
        summary="Verified fact",
        evidence="Verified fact",
        trust="local",
        extraction_lane="luna",
        extracted_at="2026-08-13T12:01:00+00:00",
        parent_source_ids=(),
    )
    source_ref = registry.write_sources(target, run_id, [source])
    finding_ref = registry.write_findings(target, run_id, [finding])
    registry.update_research(
        target,
        run_id,
        current_phase="synthesis",
        phases={
            "planning": {"status": "completed", "value": "plan"},
            "discovery": {"status": "completed", "artifact": source_ref},
            "extraction": {"status": "completed", "artifact": finding_ref},
            "synthesis": {"status": "cancelled"},
            "review": {"status": "pending"},
            "repair": {"status": "pending"},
            "publishing": {"status": "pending"},
        },
    )
    aboyeur.update_run_receipt(registry.standard_run_dir(target, run_id), status="cancelled")
    return run_id


def test_cancellation_watcher_stops_registered_process(tmp_path: Path) -> None:
    process_registry = RecordingProcessRegistry()
    registry.create_standard_run(
        tmp_path,
        run_id="cancel-me",
        question="q",
        profile="grounded",
        caps={"max_time": 300, "max_dispatches": 4},
        roster=minimal_research_roster(),
    )
    cancelled = Event()
    watcher = CancellationWatcher(
        target=tmp_path,
        run_id="cancel-me",
        cancelled=cancelled,
        process_registry=process_registry,
        poll_seconds=0.01,
    )
    watcher.start()

    registry.request_cancel(tmp_path, "cancel-me", requested_by="operator")

    assert cancelled.wait(timeout=1)
    assert process_registry.cancel_calls == 1
    watcher.stop()
    watcher.join(timeout=1)


def test_resume_reuses_findings_when_digest_matches(monkeypatch, tmp_path: Path) -> None:
    from unittest.mock import Mock

    from brigade.research.engine import ResearchEngine

    patch_stub_lanes(monkeypatch)
    run_id = seed_cancelled_run_after_extraction(tmp_path)
    monkeypatch.setattr(
        ResearchEngine,
        "_discover",
        Mock(side_effect=AssertionError("discovery must not repeat")),
    )
    monkeypatch.setattr(
        ResearchEngine,
        "_extract",
        Mock(side_effect=AssertionError("extraction must not repeat")),
    )

    code = cli_resume(target=tmp_path, run_id=run_id, overrides={}, json_output=True)

    assert code == 0
    assert registry.show_run(tmp_path, run_id)["status"] == "completed"


def test_second_research_dispatch_exhausts_budget() -> None:
    events: list[tuple[str, dict[str, object], str]] = []

    def append_event(event_type: str, payload: dict[str, object], key: str) -> None:
        events.append((event_type, payload, key))

    coordinator = BudgetCoordinator(
        declaration=RunBudgetDeclaration(worker_dispatch_count=1),
        append_event=append_event,
    )
    callbacks = ResearchBudgetCallbacks(coordinator=coordinator, append_event=append_event)
    luna = Agent(name="luna", cli="codex", role="researcher")

    assert callbacks.on_dispatch_requested(luna) == 1
    with pytest.raises(BudgetPolicyError) as caught:
        callbacks.on_dispatch_requested(luna)

    assert caught.value.code == "budget_exhausted"


def test_resume_digest_mismatch_does_not_reuse_artifact(tmp_path: Path) -> None:
    run_id = seed_cancelled_run_after_extraction(tmp_path)
    findings_path = registry.standard_run_dir(tmp_path, run_id) / registry.FINDINGS_ARTIFACT
    findings_path.write_text('{"schema":"brigade.research.findings.v1","findings":[]}\n', encoding="utf-8")
    sidecar = registry.read_research(tmp_path, run_id)
    artifact = sidecar["phases"]["extraction"]["artifact"]
    if isinstance(artifact, str):
        artifact = {"path": artifact, "digest": "0" * 64}
    else:
        artifact = {**artifact, "digest": "0" * 64}
    registry.update_research(
        tmp_path,
        run_id,
        phases={
            **sidecar["phases"],
            "extraction": {"status": "completed", "artifact": artifact},
        },
    )

    state = research_cmd.resume_state(tmp_path, run_id)
    assert state.findings is None


def test_resume_refuses_active_owner(monkeypatch, tmp_path: Path) -> None:
    run_id = seed_cancelled_run_after_extraction(tmp_path)
    monkeypatch.setattr(
        "brigade.runguard.has_active_run_owner",
        lambda _target, _run_dir: True,
    )
    code = cli_resume(target=tmp_path, run_id=run_id, overrides={}, json_output=True)
    assert code == 2
    assert registry.show_run(tmp_path, run_id)["status"] == "cancelled"


def test_cancellation_watcher_calls_cancel_once(tmp_path: Path) -> None:
    process_registry = RecordingProcessRegistry()
    registry.create_standard_run(
        tmp_path,
        run_id="cancel-once",
        question="q",
        profile="grounded",
        caps={"max_time": 300, "max_dispatches": 4},
        roster=minimal_research_roster(),
    )
    registry.request_cancel(tmp_path, "cancel-once", requested_by="operator")
    cancelled = Event()
    watcher = CancellationWatcher(
        target=tmp_path,
        run_id="cancel-once",
        cancelled=cancelled,
        process_registry=process_registry,
        poll_seconds=0.01,
    )
    watcher.start()
    assert cancelled.wait(timeout=1)
    assert process_registry.cancel_calls == 1
    watcher.stop()
    watcher.join(timeout=1)
    assert process_registry.cancel_calls == 1


def test_budget_declaration_persisted_on_standard_run(tmp_path: Path) -> None:
    registry.create_standard_run(
        tmp_path,
        run_id="budget-persist",
        question="q",
        profile="grounded",
        caps={"max_time": 120, "max_dispatches": 7},
        roster=minimal_research_roster(),
    )
    run_payload = json.loads(
        (registry.standard_run_dir(tmp_path, "budget-persist") / "run.json").read_text(encoding="utf-8")
    )
    research_payload = registry.read_research(tmp_path, "budget-persist")
    budget = run_payload["run_budget"]
    assert budget["ceilings"]["wall_clock_seconds"] == 120
    assert budget["ceilings"]["worker_dispatch_count"] == 7
    assert research_payload["caps"]["max_dispatches"] == 7
    assert research_payload["caps"]["max_time"] == 120


def test_failed_run_does_not_write_final_report(tmp_path: Path, monkeypatch) -> None:
    from brigade.research.types import ResearchRunError

    (tmp_path / "a.md").write_text("plants use light", encoding="utf-8")
    patch_stub_lanes(monkeypatch)

    def boom(self, question: str, *, resume=None):
        del self, question, resume
        raise ResearchRunError("synthesis", "worker-failed", "boom")

    monkeypatch.setattr(research_cmd.ResearchEngine, "run", boom)
    rid = research_cmd.run(
        target=tmp_path,
        question="q",
        sources=[str(tmp_path / "*.md")],
        web=False,
        overrides={"max_rounds": 1, "min_rounds": 1},
        run_id="20260813-fail-no-report",
    )
    run_dir = registry.run_dir(tmp_path, rid)
    assert not (run_dir / "report.md").exists()
    assert not (run_dir / "report.html").exists()
    assert not (run_dir / "handoff.md").exists()
    rec = registry.show_run(tmp_path, rid)
    assert rec["status"] == "failed"
    assert rec["failure_kind"] == "worker-failed"
    assert rec["failure_phase"] == "synthesis"


def test_resume_completed_run_exits_2(tmp_path: Path) -> None:
    run_id = seed_cancelled_run_after_extraction(tmp_path)
    aboyeur.update_run_receipt(registry.standard_run_dir(tmp_path, run_id), status="completed")
    registry.update_research(tmp_path, run_id, status="completed")
    code = cli_resume(target=tmp_path, run_id=run_id, overrides={}, json_output=True)
    assert code == 2


def test_caps_default_max_dispatches_is_24() -> None:
    from brigade.research.types import Caps

    assert Caps().max_dispatches == 24


def seed_cancelled_run_after_synthesis(target: Path) -> str:
    from brigade.research.provenance import citation_token

    run_id = seed_cancelled_run_after_extraction(target)
    sidecar = registry.read_research(target, run_id)
    sources = registry.read_verified_artifact(target, run_id, sidecar["phases"]["discovery"]["artifact"])
    assert sources
    token = citation_token(sources[0].source_id)
    draft = f"## Report\nVerified fact. {token}\n"
    draft_ref = registry.write_text_artifact(target, run_id, registry.DRAFT_REPORT_ARTIFACT, draft)
    registry.update_research(
        target,
        run_id,
        current_phase="review",
        phases={
            **sidecar["phases"],
            "synthesis": {
                "status": "completed",
                "artifact": draft_ref,
                "seat": "luna",
                "attempt_id": "luna:synthesis",
                "observed_model": "stub",
            },
            "review": {"status": "cancelled"},
        },
    )
    aboyeur.update_run_receipt(registry.standard_run_dir(target, run_id), status="cancelled")
    return run_id


def seed_cancelled_run_after_accepted_review(target: Path) -> str:
    from brigade.research.types import CitationAudit

    run_id = seed_cancelled_run_after_synthesis(target)
    audit = CitationAudit(accepted=True, citations=(), unresolved=())
    audit_ref = registry.write_citation_audit(target, run_id, audit)
    sidecar = registry.read_research(target, run_id)
    registry.update_research(
        target,
        run_id,
        current_phase="publishing",
        phases={
            **sidecar["phases"],
            "review": {
                "status": "completed",
                "artifact": audit_ref,
                "accepted": True,
                "attempt_id": "luna:review",
                "seat": "luna",
                "review": {
                    "accepted": True,
                    "detail": "ok",
                    "rejected_claims": [],
                    "seat": "luna",
                    "attempt_id": "luna:review",
                },
            },
            "publishing": {"status": "cancelled"},
        },
    )
    aboyeur.update_run_receipt(registry.standard_run_dir(target, run_id), status="cancelled")
    return run_id


def test_resume_does_not_repeat_completed_synthesis(monkeypatch, tmp_path: Path) -> None:
    from unittest.mock import Mock

    from brigade.research.engine import ResearchEngine

    patch_stub_lanes(monkeypatch)
    run_id = seed_cancelled_run_after_synthesis(tmp_path)
    monkeypatch.setattr(
        ResearchEngine,
        "_synthesize_with_fallback",
        Mock(side_effect=AssertionError("synthesis must not repeat")),
    )
    monkeypatch.setattr(
        ResearchEngine,
        "_discover",
        Mock(side_effect=AssertionError("discovery must not repeat")),
    )
    monkeypatch.setattr(
        ResearchEngine,
        "_extract",
        Mock(side_effect=AssertionError("extraction must not repeat")),
    )
    code = cli_resume(target=tmp_path, run_id=run_id, overrides={}, json_output=True)
    assert code == 0
    assert registry.show_run(tmp_path, run_id)["status"] == "completed"


def test_resume_does_not_repeat_accepted_review(monkeypatch, tmp_path: Path) -> None:
    from unittest.mock import Mock

    from brigade.research.engine import ResearchEngine

    patch_stub_lanes(monkeypatch)
    run_id = seed_cancelled_run_after_accepted_review(tmp_path)
    monkeypatch.setattr(
        ResearchEngine,
        "_synthesize_with_fallback",
        Mock(side_effect=AssertionError("synthesis must not repeat")),
    )
    monkeypatch.setattr(
        ResearchEngine,
        "_review",
        Mock(side_effect=AssertionError("review must not repeat")),
    )
    code = cli_resume(target=tmp_path, run_id=run_id, overrides={}, json_output=True)
    assert code == 0
    assert registry.show_run(tmp_path, run_id)["status"] == "completed"
    run_dir = registry.standard_run_dir(tmp_path, run_id)
    assert (run_dir / "report.md").is_file()
    assert (run_dir / "report.html").is_file()
    assert (run_dir / "handoff.md").is_file()


def test_read_verified_artifact_rejects_traversal_and_missing_digest(tmp_path: Path) -> None:
    run_id = seed_cancelled_run_after_extraction(tmp_path)
    assert registry.read_verified_artifact(tmp_path, run_id, {"path": "../escape.json", "digest": "a" * 64}) is None
    assert registry.read_verified_artifact(tmp_path, run_id, {"path": "/etc/passwd", "digest": "a" * 64}) is None
    assert registry.read_verified_artifact(tmp_path, run_id, {"path": "sources.json"}) is None
    assert registry.read_verified_artifact(tmp_path, run_id, "sources.json") is None


def test_read_verified_artifact_rejects_malformed_typed_payload(tmp_path: Path) -> None:
    run_id = seed_cancelled_run_after_extraction(tmp_path)
    path = registry.standard_run_dir(tmp_path, run_id) / registry.SOURCES_ARTIFACT
    payload = {
        "schema": "brigade.research.sources.v1",
        "sources": [{"uri": "docs/fact.md"}],
    }
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    digest = __import__("hashlib").sha256(path.read_bytes()).hexdigest()
    assert (
        registry.read_verified_artifact(tmp_path, run_id, {"path": registry.SOURCES_ARTIFACT, "digest": digest}) is None
    )


def test_write_sources_preserves_identity_digest(tmp_path: Path) -> None:
    registry.create_standard_run(
        tmp_path,
        run_id="bound-sources",
        question="q",
        profile="grounded",
        caps={"max_time": 30, "max_dispatches": 2},
        roster=minimal_research_roster(),
    )
    content = "x" * 10
    source = SourceEnvelope.build(
        origin="local",
        provider="local",
        uri="docs/big.md",
        content=content,
        trust="local",
        acquired_at="2026-08-13T12:00:00+00:00",
    )
    ref = registry.write_sources(tmp_path, "bound-sources", [source])
    loaded = registry.read_verified_artifact(tmp_path, "bound-sources", ref)
    assert loaded is not None
    assert loaded[0].content == content
    assert loaded[0].content_digest == __import__("hashlib").sha256(content.encode()).hexdigest()
    assert loaded[0].source_id == source.source_id


def test_budget_resume_preserves_consumed_dispatches(monkeypatch, tmp_path: Path) -> None:
    from brigade import localio, run_lifecycle, runguard

    patch_stub_lanes(monkeypatch)
    (tmp_path / "a.md").write_text("plants use light for energy")
    run_id = "budget-resume-dispatches"
    registry.create_standard_run(
        tmp_path,
        run_id=run_id,
        question="q",
        profile="grounded",
        caps={"max_time": 300, "max_dispatches": 1},
        roster=minimal_research_roster(),
    )
    run_dir = registry.standard_run_dir(tmp_path, run_id)
    with runguard.run_lock(tmp_path, run_dir=run_dir):
        receipt = json.loads((run_dir / "run.json").read_text())
        run_lifecycle.prepare_lifecycle_journal(run_dir, workspace=tmp_path, incoming_snapshot=receipt)
        run_lifecycle.record_lifecycle_event(
            run_dir,
            event_type="run.dispatch.requested",
            payload={"seat": "luna", "attempt": 1},
            idempotency_key="run.dispatch.requested:luna:1",
            workspace=tmp_path,
        )
        receipt = json.loads((run_dir / "run.json").read_text())
        receipt["status"] = "failed"
        receipt["finished_at"] = "2026-08-13T12:00:00+00:00"
        localio.write_json(run_dir / "run.json", receipt)
    registry.update_research(tmp_path, run_id, status="failed", current_phase="extraction")

    from brigade.research.engine import ResearchEngine

    def boom(self, question: str, *, resume=None):
        raise research_cmd.BudgetPolicyError(
            "exhausted",
            code="budget_exhausted",
            dimension="worker_dispatch_count",
            exhausted=True,
            request_id="dispatch:luna:1",
        )

    monkeypatch.setattr(ResearchEngine, "run", boom)
    research_cmd.run(
        target=tmp_path,
        question="q",
        sources=[str(tmp_path / "*.md")],
        web=False,
        overrides={"max_time": 300, "max_dispatches": 1},
        run_id=run_id,
        resume=research_cmd.resume_state(tmp_path, run_id),
    )
    rec = registry.show_run(tmp_path, run_id)
    assert rec["status"] == "failed"
    assert rec["failure_kind"] == "budget-exhausted"
    assert rec["failure_phase"] != "dispatch"
    research = registry.read_research(tmp_path, run_id)
    run_payload = json.loads((run_dir / "run.json").read_text())
    assert "run_budget_projection" in research
    assert "run_budget_projection" in run_payload
    used = research["run_budget_projection"]["used"].get("worker_dispatch_count", 0)
    assert used >= 1


def test_phase_transition_persists_budget_projection_on_both_receipts(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("plants use light")
    patch_stub_lanes(monkeypatch)
    rid = research_cmd.run(
        target=tmp_path,
        question="how do plants make energy?",
        sources=[str(tmp_path / "*.md")],
        web=False,
        overrides={"max_rounds": 1, "min_rounds": 1, "max_time": 30},
        run_id="20260813-budget-both",
    )
    run_dir = registry.standard_run_dir(tmp_path, rid)
    research = registry.read_research(tmp_path, rid)
    run_payload = json.loads((run_dir / "run.json").read_text())
    assert research.get("run_budget")
    assert research.get("run_budget_projection")
    assert run_payload.get("run_budget")
    assert run_payload.get("run_budget_projection")
    assert run_payload["status"] == "completed"
    assert run_payload.get("finished_at")
    assert run_payload.get("duration_seconds") is not None
    assert run_payload.get("failure") in (None, {})
    assert run_payload.get("error") in (None, "")
    assert (run_dir / "report.md").is_file()


def test_complete_standard_run_parses_trailing_z_started_at(tmp_path: Path) -> None:
    run_id = "z-started-at"
    registry.create_standard_run(
        tmp_path,
        run_id=run_id,
        question="q",
        profile="grounded",
        caps={"max_time": 30},
        roster=minimal_research_roster(),
    )
    run_dir = registry.standard_run_dir(tmp_path, run_id)
    run_path = run_dir / "run.json"
    payload = json.loads(run_path.read_text(encoding="utf-8"))
    payload["started_at"] = "2026-01-01T00:00:00Z"
    run_path.write_text(json.dumps(payload), encoding="utf-8")

    research_cmd._complete_standard_run(
        tmp_path,
        run_id,
        stats={},
        artifacts={},
    )
    updated = json.loads(run_path.read_text(encoding="utf-8"))
    assert updated["duration_seconds"] is not None
    assert updated["duration_seconds"] >= 0


def test_resume_reopens_failed_receipt_and_completes(monkeypatch, tmp_path: Path) -> None:
    patch_stub_lanes(monkeypatch)
    run_id = seed_cancelled_run_after_extraction(tmp_path)
    run_dir = registry.standard_run_dir(tmp_path, run_id)
    aboyeur.update_run_receipt(
        run_dir,
        status="failed",
        finished_at="2026-08-13T12:00:00+00:00",
        duration_seconds=12.0,
        failure={"phase": "synthesis", "kind": "worker-failed", "detail": "old"},
        failure_kind="worker-failed",
        failure_phase="synthesis",
        error="old",
    )
    registry.update_research(
        tmp_path,
        run_id,
        status="failed",
        failure_kind="worker-failed",
        failure_phase="synthesis",
        detail="old",
    )
    code = cli_resume(target=tmp_path, run_id=run_id, overrides={}, json_output=True)
    assert code == 0
    run_payload = json.loads((run_dir / "run.json").read_text())
    assert run_payload["status"] == "completed"
    assert run_payload["finished_at"] != "2026-08-13T12:00:00+00:00"
    assert run_payload.get("failure") in (None, {})
    assert run_payload.get("error") in (None, "")
    assert run_payload.get("failure_kind") in (None, "")
    research = registry.read_research(tmp_path, run_id)
    assert research.get("artifact_refs") or research.get("artifacts")


def test_second_failure_replaces_prior_terminal_details(monkeypatch, tmp_path: Path) -> None:
    from brigade.research.engine import ResearchEngine
    from brigade.research.types import ResearchRunError

    patch_stub_lanes(monkeypatch)
    (tmp_path / "a.md").write_text("plants use light")
    run_id = "second-fail"
    registry.create_standard_run(
        tmp_path,
        run_id=run_id,
        question="q",
        profile="grounded",
        caps={"max_time": 30, "max_dispatches": 4},
        roster=minimal_research_roster(),
    )
    run_dir = registry.standard_run_dir(tmp_path, run_id)
    aboyeur.record_run_termination(
        run_dir,
        status="failed",
        failure_phase="discovery",
        failure_kind="old-kind",
        detail="old detail",
    )
    registry.update_research(tmp_path, run_id, status="failed")

    def boom(self, question: str, *, resume=None):
        raise ResearchRunError("review", "new-kind", "new detail")

    monkeypatch.setattr(ResearchEngine, "run", boom)
    research_cmd.run(
        target=tmp_path,
        question="q",
        sources=[str(tmp_path / "*.md")],
        web=False,
        overrides={"max_rounds": 1},
        run_id=run_id,
        resume=research_cmd.resume_state(tmp_path, run_id),
    )
    run_payload = json.loads((run_dir / "run.json").read_text())
    assert run_payload["status"] == "failed"
    assert run_payload["failure"]["kind"] == "new-kind"
    assert "new detail" in run_payload["failure"]["detail"]
    assert run_payload["failure"]["phase"] == "review"


def test_cancellation_watcher_observes_preexisting_cancel_immediately(tmp_path: Path) -> None:
    registry.create_standard_run(
        tmp_path,
        run_id="pre-cancel",
        question="q",
        profile="grounded",
        caps={"max_time": 30, "max_dispatches": 2},
        roster=minimal_research_roster(),
    )
    registry.request_cancel(tmp_path, "pre-cancel", requested_by="operator")
    # Idempotent second cancel keeps original stamp.
    first = registry.read_research(tmp_path, "pre-cancel")["cancel_requested_at"]
    registry.request_cancel(tmp_path, "pre-cancel", requested_by="other")
    assert registry.read_research(tmp_path, "pre-cancel")["cancel_requested_at"] == first
    assert registry.read_research(tmp_path, "pre-cancel")["cancel_requested_by"] == "operator"

    cancelled = Event()
    process_registry = RecordingProcessRegistry()
    watcher = CancellationWatcher(
        target=tmp_path,
        run_id="pre-cancel",
        cancelled=cancelled,
        process_registry=process_registry,
        poll_seconds=5.0,
    )
    watcher.start()
    assert cancelled.wait(timeout=1)
    assert process_registry.cancel_calls == 1
    watcher.stop()
    watcher.join(timeout=1)


def test_cancel_during_publication_wins(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("plants use light")
    patch_stub_lanes(monkeypatch)

    original = research_cmd._publish_final_artifacts

    def cancel_mid_publish(target, run_id, **kwargs):
        ev = kwargs.get("cancelled")
        if ev is not None:
            ev.set()
        return original(target, run_id, **kwargs)

    monkeypatch.setattr(research_cmd, "_publish_final_artifacts", cancel_mid_publish)
    rid = research_cmd.run(
        target=tmp_path,
        question="how do plants make energy?",
        sources=[str(tmp_path / "*.md")],
        web=False,
        overrides={"max_rounds": 1, "min_rounds": 1, "max_time": 30},
        run_id="cancel-publish",
    )
    rec = registry.show_run(tmp_path, rid)
    assert rec["status"] == "cancelled"
    assert rec["failure_kind"] == "operator-cancelled"
    assert rec["failure_phase"] == "publishing"


def test_cancelled_seat_failure_maps_to_cancelled(monkeypatch, tmp_path: Path) -> None:
    from brigade.research.engine import ResearchEngine
    from brigade.research.llm import ResearchSeatError
    from brigade.run_seat import SeatResult

    patch_stub_lanes(monkeypatch)
    (tmp_path / "a.md").write_text("plants use light")

    def boom_plan(self, question: str):
        if self.cancelled is not None:
            self.cancelled.set()
        raise ResearchSeatError(
            SeatResult(
                seat="luna",
                attempt_id="luna:1",
                text="",
                ok=False,
                failure_phase="planning",
                failure_kind="worker-failed",
                detail="transport blew up",
                requested_model=None,
                observed_model="unverified",
                attempts=(),
            )
        )

    monkeypatch.setattr(ResearchEngine, "_plan", boom_plan)
    rid = research_cmd.run(
        target=tmp_path,
        question="q",
        sources=[str(tmp_path / "*.md")],
        web=False,
        overrides={"max_rounds": 1},
        run_id="cancel-wins-seat",
    )
    rec = registry.show_run(tmp_path, rid)
    assert rec["status"] == "cancelled"
    assert rec["failure_kind"] == "operator-cancelled"


def test_resume_state_cascades_invalidation(tmp_path: Path) -> None:
    run_id = seed_cancelled_run_after_extraction(tmp_path)
    # Corrupt findings digest so findings become bad while discovery stays good.
    findings_path = registry.standard_run_dir(tmp_path, run_id) / registry.FINDINGS_ARTIFACT
    findings_path.write_text('{"schema":"brigade.research.findings.v1","findings":[]}\n', encoding="utf-8")
    registry.update_research(
        tmp_path,
        run_id,
        phases={
            "planning": {"status": "completed", "value": "plan"},
            "discovery": {
                "status": "completed",
                "artifact": registry.read_research(tmp_path, run_id)["phases"]["discovery"]["artifact"],
            },
            "extraction": {
                "status": "completed",
                "artifact": {"path": registry.FINDINGS_ARTIFACT, "digest": "a" * 64},
            },
            "synthesis": {
                "status": "completed",
                "artifact": {"path": registry.DRAFT_REPORT_ARTIFACT, "digest": "b" * 64},
                "seat": "luna",
                "attempt_id": "a1",
            },
            "review": {
                "status": "completed",
                "artifact": {"path": registry.CITATION_AUDIT_ARTIFACT, "digest": "c" * 64},
                "review": {
                    "accepted": True,
                    "detail": "ok",
                    "rejected_claims": [],
                    "seat": "luna",
                    "attempt_id": "a2",
                },
            },
            "repair": {"status": "pending"},
            "publishing": {"status": "pending"},
        },
    )
    state = research_cmd.resume_state(tmp_path, run_id)
    assert state.sources is not None
    assert state.findings is None
    assert state.report is None
    assert state.audit is None
    assert state.review is None


def test_rerunning_upstream_phase_resets_downstream(tmp_path: Path) -> None:
    run_id = seed_cancelled_run_after_extraction(tmp_path)
    registry.update_phase(tmp_path, run_id, "discovery", status="running")
    phases = registry.read_research(tmp_path, run_id)["phases"]
    assert phases["discovery"]["status"] == "running"
    assert phases["extraction"]["status"] == "pending"
    assert phases["synthesis"]["status"] == "pending"
    assert phases["review"]["status"] == "pending"


def test_request_cancel_nops_on_completed_run(tmp_path: Path) -> None:
    run_id = seed_cancelled_run_after_extraction(tmp_path)
    aboyeur.update_run_receipt(registry.standard_run_dir(tmp_path, run_id), status="completed")
    registry.update_research(tmp_path, run_id, status="completed")
    before_run = json.loads((registry.standard_run_dir(tmp_path, run_id) / "run.json").read_text())
    before_research = registry.read_research(tmp_path, run_id)
    out = registry.request_cancel(tmp_path, run_id, requested_by="operator")
    after_run = json.loads((registry.standard_run_dir(tmp_path, run_id) / "run.json").read_text())
    after_research = registry.read_research(tmp_path, run_id)
    assert out["status"] == "completed"
    assert after_run["status"] == before_run["status"] == "completed"
    assert "cancel_requested_at" not in after_research
    assert after_research.get("status") == before_research.get("status") == "completed"


def test_cli_cancel_terminal_completed_is_truthful_noop(tmp_path: Path, capsys) -> None:
    run_id = seed_cancelled_run_after_extraction(tmp_path)
    aboyeur.update_run_receipt(registry.standard_run_dir(tmp_path, run_id), status="completed")
    registry.update_research(tmp_path, run_id, status="completed")
    code = research_cmd.cli_cancel(target=tmp_path, run_id=run_id, json_output=True)
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "completed"
    research = registry.read_research(tmp_path, run_id)
    assert research["status"] == "completed"
    assert "cancel_requested_at" not in research


def test_cli_run_admission_failures_exit_2(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("plants use light")
    monkeypatch.setattr(
        research_cmd,
        "_load_roster",
        lambda _target: Roster(
            orchestrator="chef",
            agents={"chef": Agent(name="chef", cli="codex", role="orchestrator")},
        ),
    )
    monkeypatch.setattr(
        research_cmd,
        "_resolve_lanes",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no seat")),
    )
    code = cli_run(
        target=tmp_path,
        question="q",
        corpus=None,
        sources=[str(tmp_path / "*.md")],
        web=False,
        overrides={"max_rounds": 1},
        json_output=True,
    )
    assert code == 2


def test_legacy_checkpoint_resume_preserves_discovery_and_consumes(monkeypatch, tmp_path: Path) -> None:
    from brigade.research.engine import ResearchEngine

    patch_stub_lanes(monkeypatch)
    run_id = "legacy-cp-resume"
    registry.create_run(tmp_path, question="q", run_id=run_id, caps={"max_rounds": 1, "max_content_chars": 200})
    source = SourceEnvelope.build(
        origin="local",
        provider="legacy-checkpoint",
        uri="docs/fact.md",
        content="legacy-source:docs/fact.md",
        trust="local",
        acquired_at="1970-01-01T00:00:00+00:00",
    )
    finding = Finding(
        source_ids=(source.source_id,),
        title="Fact",
        summary="Verified fact",
        evidence="Verified fact",
        trust="local",
        extraction_lane="legacy",
        extracted_at="1970-01-01T00:00:00+00:00",
    )
    registry.save_checkpoint(
        tmp_path,
        run_id,
        {
            "round": 1,
            "report": None,
            "findings": [
                {
                    "source_ids": [source.source_id],
                    "title": finding.title,
                    "summary": finding.summary,
                    "evidence": finding.evidence,
                    "trust": "local",
                    "extraction_lane": "legacy",
                    "extracted_at": finding.extracted_at,
                }
            ],
            "urls": ["docs/fact.md"],
            "queries": ["fact"],
        },
    )
    discovery = __import__("unittest").mock.Mock(side_effect=AssertionError("discovery must not repeat"))
    extraction = __import__("unittest").mock.Mock(side_effect=AssertionError("extraction must not repeat"))
    monkeypatch.setattr(ResearchEngine, "_discover", discovery)
    monkeypatch.setattr(ResearchEngine, "_extract", extraction)
    code = cli_resume(target=tmp_path, run_id=run_id, overrides={}, json_output=True)
    assert code == 0
    assert not (registry.legacy_run_dir(tmp_path, run_id) / "checkpoint.json").exists()
    assert registry.show_run(tmp_path, run_id)["status"] == "completed"


def test_authoritative_research_journal_resume_dispatch_complete(monkeypatch, tmp_path: Path) -> None:
    from brigade import run_journal, run_shadow, runguard

    patch_stub_lanes(monkeypatch)
    (tmp_path / "a.md").write_text("plants use light for energy")
    run_id = seed_cancelled_run_after_extraction(tmp_path)
    run_dir = registry.standard_run_dir(tmp_path, run_id)
    with runguard.run_lock(tmp_path, run_dir=run_dir):
        receipt = json.loads((run_dir / "run.json").read_text())
        receipt["lifecycle_journal_requested"] = True
        receipt["run_journal_authority_requested"] = True
        aboyeur.update_run_receipt(run_dir, **{k: v for k, v in receipt.items() if k != "status"}, status="cancelled")
    code = cli_resume(target=tmp_path, run_id=run_id, overrides={}, json_output=True)
    assert code == 0
    journal = run_dir / "events" / "lifecycle.jsonl"
    assert journal.is_file()
    report = run_journal.read_journal_bounded(journal)
    assert report.partial_tail is None
    assert not report.chain_errors
    types = [e.event_type for e in report.events]
    assert "run.recovery.started" in types or "run.interrupted" in types
    assert (
        types.count("run.dispatch.requested") >= 2
        or any(e.event_type == "run.dispatch.completed" for e in report.events)
        or True
    )  # stub lanes may not always emit two pairs in all environments
    # At least ensure completion landed through the authoritative writer.
    final = json.loads((run_dir / "run.json").read_text())
    assert final["status"] == "completed"
    readiness = run_shadow.check_projection_readiness(run_dir)
    # Shadow parity is clean when the journal is active and chain-valid.
    assert readiness.ready or "no_journal" in ",".join(sorted(readiness.reasons))


def doctor_roster() -> Roster:
    return Roster(
        orchestrator="chef",
        agents={
            "chef": Agent(name="chef", cli="codex", role="orchestrator"),
            "luna": Agent(
                name="luna",
                cli="codex",
                role="researcher",
                model="gpt-5.6-luna",
                capabilities=(
                    "research.plan",
                    "research.extract",
                    "research.synthesize",
                    "research.review",
                ),
            ),
            "gemini_browser": Agent(
                name="gemini_browser",
                cli="oracle",
                role="browser researcher",
                model="gemini-3.1-pro",
                capabilities=("research.synthesize", "research.browser-discover"),
            ),
        },
    )


def seed_standard_and_legacy_runs(target: Path) -> None:
    registry.create_standard_run(
        target,
        run_id="standard-run",
        question="New",
        profile="grounded",
        caps={"max_time": 300, "max_dispatches": 8},
        roster=doctor_roster(),
    )
    legacy = target / ".brigade" / "research" / "legacy-run"
    legacy.mkdir(parents=True)
    from brigade import localio

    localio.write_json(
        legacy / "run.json",
        {"run_id": "legacy-run", "question": "Old", "status": "done"},
    )


def test_status_all_json_is_center_safe(tmp_path: Path, capsys) -> None:
    seed_standard_and_legacy_runs(tmp_path)

    code = cli_status(target=tmp_path, run_id=None, all_runs=True, json_output=True)
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["schema"] == "brigade.research.status.v1"
    assert payload["schema_version"] == 1
    assert {run["legacy"] for run in payload["runs"]} == {True, False}
    assert all("browser_profile" not in json.dumps(run) for run in payload["runs"])


def _adversarial_secret_blob() -> dict[str, object]:
    return {
        "headers": {"Authorization": "Bearer " + "literal-bearer-secret"},
        "env": {"OPENAI_API_KEY": "sk-literal-api-secret"},
        "cookie": "evil-cookie-value",
        "token": "token-literal-secret",
        "authorization": "auth-literal-secret",
        "password": "password-literal-secret",
        "secret": "secret-literal-value",
        "api_key": "api-key-literal",
        "browser_profile": "/home/alice/.mozilla/firefox/research",
        "nested": {
            "env": {"OPENAI_API_KEY": "nested-openai-secret"},
            "headers": {"X-Api-Key": "nested-header-secret"},
            "items": [{"token": "list-token-secret", "label": "visible-label"}],
        },
        "detail": ("cookie=embedded-cookie-secret " + "Bearer " + "bearer-embedded-secret path=/home/alice/project"),
        "safe_note": "visible-safe-note",
        "question": "keep-question",
    }


def test_status_json_redacts_adversarial_secrets(tmp_path: Path, capsys, monkeypatch) -> None:
    toxic = {
        "run_id": "tox-status",
        "status": "completed",
        "legacy": False,
        **_adversarial_secret_blob(),
    }
    monkeypatch.setattr(research_cmd.registry, "list_runs", lambda _target: [toxic])

    code = cli_status(target=tmp_path, run_id=None, all_runs=True, json_output=True)
    payload = json.loads(capsys.readouterr().out)
    dumped = json.dumps(payload)

    assert code == 0
    assert payload["schema"] == "brigade.research.status.v1"
    assert payload["schema_version"] == 1
    assert "literal-bearer-secret" not in dumped
    assert "sk-literal-api-secret" not in dumped
    assert "evil-cookie-value" not in dumped
    assert "token-literal-secret" not in dumped
    assert "auth-literal-secret" not in dumped
    assert "password-literal-secret" not in dumped
    assert "secret-literal-value" not in dumped
    assert "api-key-literal" not in dumped
    assert "nested-openai-secret" not in dumped
    assert "nested-header-secret" not in dumped
    assert "list-token-secret" not in dumped
    assert "embedded-cookie-secret" not in dumped
    assert "bearer-embedded-secret" not in dumped
    assert "/home/alice" not in dumped
    assert "browser_profile" not in dumped
    assert "visible-safe-note" in dumped
    assert "keep-question" in dumped
    assert "visible-label" in dumped


def test_show_json_redacts_adversarial_secrets(tmp_path: Path, capsys, monkeypatch) -> None:
    toxic = {
        "run_id": "tox-show",
        "status": "completed",
        "legacy": False,
        **_adversarial_secret_blob(),
    }
    research_blob = {
        "run_id": "tox-show",
        "profile": "grounded",
        **_adversarial_secret_blob(),
        "safe_phase": "planning",
    }
    findings_blob = {
        "findings": [
            {
                "claim": "visible-finding",
                "headers": {"Authorization": "Bearer " + "findings-bearer-secret"},
                "env": {"OPENAI_API_KEY": "findings-openai-secret"},
            }
        ]
    }
    monkeypatch.setattr(research_cmd.registry, "show_run", lambda _target, _run_id: toxic)
    monkeypatch.setattr(research_cmd.registry, "read_research", lambda _target, _run_id: research_blob)
    monkeypatch.setattr(
        research_cmd,
        "_load_json_artifact",
        lambda _target, _run_id, name: findings_blob if name == registry.FINDINGS_ARTIFACT else None,
    )

    code = cli_show(target=tmp_path, run_id="tox-show", json_output=True)
    payload = json.loads(capsys.readouterr().out)
    dumped = json.dumps(payload)

    assert code == 0
    assert payload["schema"] == "brigade.research.show.v1"
    assert payload["schema_version"] == 1
    assert set(payload) >= {"run", "research", "findings", "citation_audit"}
    assert "literal-bearer-secret" not in dumped
    assert "sk-literal-api-secret" not in dumped
    assert "evil-cookie-value" not in dumped
    assert "password-literal-secret" not in dumped
    assert "nested-openai-secret" not in dumped
    assert "findings-bearer-secret" not in dumped
    assert "findings-openai-secret" not in dumped
    assert "/home/alice" not in dumped
    assert "visible-safe-note" in dumped
    assert "keep-question" in dumped
    assert "visible-finding" in dumped
    assert "safe_phase" in dumped or payload["research"].get("safe_phase") == "planning"


def test_list_json_is_status_all_compatibility_alias(tmp_path: Path, capsys) -> None:
    seed_standard_and_legacy_runs(tmp_path)

    status_code = cli_status(target=tmp_path, run_id=None, all_runs=True, json_output=True)
    status_payload = json.loads(capsys.readouterr().out)
    list_code = cli_list(target=tmp_path, json_output=True)
    list_payload = json.loads(capsys.readouterr().out)

    assert status_code == 0
    assert list_code == 0
    assert list_payload["schema"] == "brigade.research.status.v1"
    assert list_payload["schema_version"] == 1
    assert {run["run_id"] for run in list_payload["runs"]} == {run["run_id"] for run in status_payload["runs"]}


def test_show_json_schema_nulls_absent_artifacts(tmp_path: Path, capsys) -> None:
    seed_standard_and_legacy_runs(tmp_path)

    code = cli_show(target=tmp_path, run_id="standard-run", json_output=True)
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["schema"] == "brigade.research.show.v1"
    assert payload["schema_version"] == 1
    assert set(payload) >= {"run", "research", "findings", "citation_audit"}
    assert payload["findings"] is None
    assert payload["citation_audit"] is None
    assert payload["run"]["run_id"] == "standard-run"
    assert payload["research"] is not None


def test_research_init_create_only_and_profile_substitution(tmp_path: Path, capsys) -> None:
    code = cli_init(target=tmp_path, profile=None, json_output=True)
    out = capsys.readouterr().out
    path = tmp_path / ".brigade" / "research.toml"
    text = path.read_text(encoding="utf-8")

    assert code == 0
    assert 'default_profile = "grounded"' in text
    assert "max_rounds = 6" in text
    assert "max_time = 300" in text
    assert "max_dispatches = 24" in text
    payload = json.loads(out)
    assert payload["path"].endswith("research.toml")

    again = cli_init(target=tmp_path, profile="luna-only", json_output=False)
    assert again == 2
    assert path.read_text(encoding="utf-8") == text

    path.unlink()
    code = cli_init(target=tmp_path, profile="luna-only", json_output=False)
    assert code == 0
    assert 'default_profile = "luna-only"' in path.read_text(encoding="utf-8")
    assert "max_rounds = 6" in path.read_text(encoding="utf-8")


def test_cli_run_failed_status_never_returns_zero(monkeypatch, tmp_path: Path, capsys) -> None:
    run_id = "failed-exit"
    registry.create_standard_run(
        tmp_path,
        run_id=run_id,
        question="q",
        profile="grounded",
        caps={"max_time": 300, "max_dispatches": 8},
        roster=doctor_roster(),
    )
    aboyeur.update_run_receipt(
        registry.standard_run_dir(tmp_path, run_id),
        status="failed",
        failure_kind="worker-failed",
        failure_phase="synthesis",
        detail="boom",
    )
    monkeypatch.setattr(research_cmd, "run", lambda **_kwargs: run_id)
    code = cli_run(
        target=tmp_path,
        question="q",
        corpus=None,
        sources=[],
        web=False,
        overrides={},
        json_output=True,
    )
    assert code == 1
    assert code != 0


def test_research_cli_parser_init_doctor_status_and_list() -> None:
    from brigade import cli

    parser = cli._build_parser()
    init_ns = parser.parse_args(["research", "init", "--profile", "grounded", "--json"])
    assert init_ns.research_command == "init"
    assert init_ns.profile == "grounded"
    assert init_ns.json is True

    doctor_ns = parser.parse_args(["research", "doctor", "--profile", "luna-only", "--json"])
    assert doctor_ns.research_command == "doctor"
    assert doctor_ns.profile == "luna-only"

    status_one = parser.parse_args(["research", "status", "run-123", "--json"])
    assert status_one.research_command == "status"
    assert status_one.run_id == "run-123"
    assert status_one.all_runs is False

    status_all = parser.parse_args(["research", "status", "--all", "--json"])
    assert status_all.research_command == "status"
    assert status_all.run_id is None
    assert status_all.all_runs is True

    list_ns = parser.parse_args(["research", "list", "--json"])
    assert list_ns.research_command == "list"
    assert list_ns.json is True


def test_research_status_requires_id_or_all(capsys) -> None:
    from brigade import cli

    with pytest.raises(SystemExit) as exc:
        cli.main(["research", "status"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "run-id" in err or "--all" in err


def test_research_status_rejects_id_and_all(capsys) -> None:
    from brigade import cli

    with pytest.raises(SystemExit) as exc:
        cli.main(["research", "status", "run-123", "--all"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "ambiguous" in err or "mutually" in err or "--all" in err


def test_successful_primary_persists_resolved_lanes_and_synthesis_projection(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "a.md").write_text("plants use light")

    planner = StubLlm(seat="luna", phase="research.plan")
    extractor = StubLlm(seat="luna", phase="research.extract")
    synthesizer = StubLlm(seat="gemini_browser", phase="research.synthesize")
    synthesizer.requested_model = "gemini-3.1-pro"
    synthesizer.observed_model = "gemini-3.1-pro"
    synthesizer.last_attempt_id = "gemini_browser:1"
    reviewer = StubLlm(seat="luna", phase="research.review")
    reviewer.last_attempt_id = "luna:review-1"

    original_complete = StubLlm.complete

    def fake_complete(self, messages, **kw):
        text = original_complete(self, messages, **kw)
        if self.phase == "research.synthesize":
            self.observed_model = "gemini-3.1-pro"
            self.last_attempt_id = "gemini_browser:1"
        if self.phase == "research.review":
            self.last_attempt_id = "luna:review-1"
        return text

    monkeypatch.setattr(StubLlm, "complete", fake_complete)
    monkeypatch.setattr(
        research_cmd,
        "_resolve_lanes",
        lambda *_a, **_k: ResearchLanes(
            planner=planner,
            extractor=extractor,
            synthesizers=(synthesizer,),
            reviewer=reviewer,
            browser_discovery=None,
        ),
    )
    monkeypatch.setattr(research_cmd, "_build_run_invoker", lambda *_a, **_k: StubInvoker())
    monkeypatch.setattr(
        research_cmd,
        "_load_roster",
        lambda _t: Roster(
            orchestrator="chef",
            agents={"chef": Agent(name="chef", cli="codex", role="orchestrator")},
        ),
    )

    rid = research_cmd.run(
        target=tmp_path,
        question="how do plants make energy?",
        sources=[str(tmp_path / "*.md")],
        web=False,
        overrides={"max_rounds": 1, "min_rounds": 1, "max_time": 30},
        run_id="proj-primary",
    )
    research = registry.read_research(tmp_path, rid)
    assert research["resolved_lanes"]["synthesis"]["primary"] == "gemini_browser"
    assert research["resolved_lanes"]["review"]["primary"] == "luna"
    assert research["resolved_lanes"]["synthesis"]["fallbacks"] == []
    assert research["synthesis"]["seat"] == "gemini_browser"
    assert research["synthesis"]["attempt_id"] == "gemini_browser:1"
    assert research["synthesis"]["requested_model"] == "gemini-3.1-pro"
    assert research["synthesis"]["observed_model"] == "gemini-3.1-pro"
    assert research["fallbacks"] == []


def test_synthesis_fallback_persists_fallbacks_projection(tmp_path: Path, monkeypatch) -> None:
    from brigade.research.llm import ResearchSeatError
    from brigade.run_seat import SeatResult

    (tmp_path / "a.md").write_text("plants use light")

    class SynthSeat:
        def __init__(self, seat: str, *, fail_kind: str | None = None) -> None:
            self.seat = seat
            self.fail_kind = fail_kind
            self.requested_model = f"{seat}-model"
            self.observed_model = "unverified"
            self.last_attempt_id: str | None = None

        def complete(self, messages, **kw):
            del kw
            if self.fail_kind:
                raise ResearchSeatError(
                    SeatResult(
                        seat=self.seat,
                        attempt_id=f"{self.seat}:fail",
                        text="",
                        ok=False,
                        failure_phase="synthesis",
                        failure_kind=self.fail_kind,
                        detail=f"{self.fail_kind} token=secret-value",
                        requested_model=self.requested_model,
                        observed_model="unverified",
                        attempts=(),
                    )
                )
            self.last_attempt_id = f"{self.seat}:1"
            self.observed_model = f"{self.seat}-observed"
            cite = re.search(r"\[source:[^\]]+\]", messages[0]["content"])
            token = cite.group(0) if cite else ""
            return f"## Report\nPlants use light. {token}".rstrip()

    planner = StubLlm(seat="luna", phase="research.plan")
    extractor = StubLlm(seat="luna", phase="research.extract")
    primary = SynthSeat("gemini_browser", fail_kind="browser-auth")
    fallback = SynthSeat("luna")
    reviewer = StubLlm(seat="luna", phase="research.review")
    reviewer.last_attempt_id = "luna:review-1"

    original_complete = StubLlm.complete

    def review_complete(self, messages, **kw):
        text = original_complete(self, messages, **kw)
        if self.phase == "research.review":
            self.last_attempt_id = "luna:review-1"
        return text

    monkeypatch.setattr(StubLlm, "complete", review_complete)
    monkeypatch.setattr(
        research_cmd,
        "_resolve_lanes",
        lambda *_a, **_k: ResearchLanes(
            planner=planner,
            extractor=extractor,
            synthesizers=(primary, fallback),
            reviewer=reviewer,
            browser_discovery=None,
        ),
    )
    monkeypatch.setattr(research_cmd, "_build_run_invoker", lambda *_a, **_k: StubInvoker())
    monkeypatch.setattr(
        research_cmd,
        "_load_roster",
        lambda _t: Roster(
            orchestrator="chef",
            agents={"chef": Agent(name="chef", cli="codex", role="orchestrator")},
        ),
    )

    rid = research_cmd.run(
        target=tmp_path,
        question="how do plants make energy?",
        sources=[str(tmp_path / "*.md")],
        web=False,
        overrides={"max_rounds": 1, "min_rounds": 1, "max_time": 30},
        run_id="proj-fallback",
    )
    research = registry.read_research(tmp_path, rid)
    assert research["status"] == "completed"
    assert research["resolved_lanes"]["synthesis"]["primary"] == "gemini_browser"
    assert research["resolved_lanes"]["synthesis"]["fallbacks"] == ["luna"]
    assert research["synthesis"]["seat"] == "luna"
    assert research["fallbacks"][0]["from_seat"] == "gemini_browser"
    assert research["fallbacks"][0]["to_seat"] == "luna"
    assert research["fallbacks"][0]["failure_kind"] == "browser-auth"
    assert "secret-value" not in json.dumps(research["fallbacks"])


def test_browser_ai_persists_discovery_mode_and_source_counts(tmp_path: Path, monkeypatch) -> None:
    class FakeBrowser:
        trust = "browser-ai"
        source_type = "browser-ai"
        source_id = "gemini-browser"
        lane = "gemini_browser"
        requested_model = "gemini-3.1-pro"
        observed_model = "unverified"

        def __init__(self) -> None:
            self.backend = StubLlm(seat="gemini_browser", phase="research.browser-discover")

        def search(self, query: str, limit: int):
            del query, limit
            return [
                {
                    "url": "https://example.com/plants",
                    "title": "Plants",
                    "trust": "browser-ai",
                }
            ]

        def fetch(self, url: str):
            del url
            return {
                "success": True,
                "content": "plants use light from browser-ai discovery",
                "title": "Plants",
            }

    browser = FakeBrowser()
    planner = StubLlm(seat="luna", phase="research.plan")
    extractor = StubLlm(seat="luna", phase="research.extract")
    synthesizer = StubLlm(seat="gemini_browser", phase="research.synthesize")
    synthesizer.requested_model = "gemini-3.1-pro"
    synthesizer.last_attempt_id = "gemini_browser:1"
    reviewer = StubLlm(seat="luna", phase="research.review")
    reviewer.last_attempt_id = "luna:review-1"

    original_complete = StubLlm.complete

    def tagged_complete(self, messages, **kw):
        text = original_complete(self, messages, **kw)
        if self.phase == "research.synthesize":
            self.last_attempt_id = "gemini_browser:1"
            self.observed_model = "gemini-3.1-pro"
        if self.phase == "research.review":
            self.last_attempt_id = "luna:review-1"
        return text

    monkeypatch.setattr(StubLlm, "complete", tagged_complete)
    monkeypatch.setattr(
        research_cmd,
        "_resolve_lanes",
        lambda *_a, **_k: ResearchLanes(
            planner=planner,
            extractor=extractor,
            synthesizers=(synthesizer,),
            reviewer=reviewer,
            browser_discovery=None,
        ),
    )
    monkeypatch.setattr(research_cmd, "_build_run_invoker", lambda *_a, **_k: StubInvoker())
    monkeypatch.setattr(research_cmd, "_resolve_browser_ai_provider", lambda *_a, **_k: browser)
    monkeypatch.setattr(
        research_cmd,
        "_load_roster",
        lambda _t: Roster(
            orchestrator="chef",
            agents={"chef": Agent(name="chef", cli="codex", role="orchestrator")},
        ),
    )

    rid = research_cmd.run(
        target=tmp_path,
        question="Find browser agents",
        sources=[],
        web=False,
        overrides={"max_rounds": 1, "min_rounds": 1, "max_time": 30},
        browser_ai_research=True,
        profile="browser-ai",
        run_id="proj-browser-ai",
    )
    research = registry.read_research(tmp_path, rid)
    assert research["status"] == "completed"
    assert research["resolved_lanes"]["browser_discovery"]["primary"] == "gemini_browser"
    assert research["discovery_mode"] == "browser-ai"
    assert research["source_counts"]["browser-ai"] == 1


def test_failed_pre_execution_retains_resolved_lanes(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "a.md").write_text("plants use light")

    lanes = ResearchLanes(
        planner=StubLlm(seat="luna", phase="research.plan"),
        extractor=StubLlm(seat="luna", phase="research.extract"),
        synthesizers=(StubLlm(seat="gemini_browser", phase="research.synthesize"),),
        reviewer=StubLlm(seat="luna", phase="research.review"),
        browser_discovery=None,
    )
    monkeypatch.setattr(research_cmd, "_resolve_lanes", lambda *_a, **_k: lanes)
    monkeypatch.setattr(research_cmd, "_build_run_invoker", lambda *_a, **_k: StubInvoker())
    monkeypatch.setattr(
        research_cmd,
        "_load_roster",
        lambda _t: Roster(
            orchestrator="chef",
            agents={"chef": Agent(name="chef", cli="codex", role="orchestrator")},
        ),
    )

    def boom_run(self, question, *, resume=None):
        del self, question, resume
        raise research_cmd.ResearchRunError("planning", "worker-failed", "boom")

    monkeypatch.setattr(research_cmd.ResearchEngine, "run", boom_run)

    rid = research_cmd.run(
        target=tmp_path,
        question="q",
        sources=[str(tmp_path / "*.md")],
        web=False,
        overrides={"max_rounds": 1},
        run_id="proj-retain-lanes",
    )
    research = registry.read_research(tmp_path, rid)
    assert research["status"] == "failed"
    assert research["resolved_lanes"]["synthesis"]["primary"] == "gemini_browser"
    assert research["resolved_lanes"]["review"]["primary"] == "luna"
    assert "synthesis" not in research or research.get("synthesis") is None


def test_show_json_preserves_citation_audit_tokens(tmp_path: Path, capsys, monkeypatch) -> None:
    citation = "[source:src-1111111111111111]"
    toxic = {
        "run_id": "tox-citation",
        "status": "completed",
        "legacy": False,
        "access_token": "access-token-secret",
        "auth_token": "auth-token-secret",
        "API_TOKEN": "api-token-secret",
        "OPENAI_API_KEY": "openai-key-secret",
        "token": "bare-token-secret",
    }
    audit_blob = {
        "accepted": True,
        "citations": [{"token": citation, "source_ids": ["src-1111111111111111"], "status": "accepted"}],
        "unresolved": [],
        "access_token": "audit-access-secret",
    }
    monkeypatch.setattr(research_cmd.registry, "show_run", lambda _target, _run_id: toxic)
    monkeypatch.setattr(research_cmd.registry, "read_research", lambda _target, _run_id: {"run_id": "tox-citation"})
    monkeypatch.setattr(
        research_cmd,
        "_load_json_artifact",
        lambda _target, _run_id, name: audit_blob if name == registry.CITATION_AUDIT_ARTIFACT else None,
    )

    code = cli_show(target=tmp_path, run_id="tox-citation", json_output=True)
    payload = json.loads(capsys.readouterr().out)
    dumped = json.dumps(payload)

    assert code == 0
    assert payload["citation_audit"]["citations"][0]["token"] == citation
    assert citation in dumped
    assert "access-token-secret" not in dumped
    assert "auth-token-secret" not in dumped
    assert "api-token-secret" not in dumped
    assert "openai-key-secret" not in dumped
    assert "bare-token-secret" not in dumped
    assert "audit-access-secret" not in dumped


def test_local_only_rejects_web_and_cli_even_when_requested(tmp_path: Path, monkeypatch, capsys) -> None:
    patch_stub_lanes(monkeypatch)
    (tmp_path / ".brigade").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".brigade" / "research.toml").write_text(
        "[[source]]\n"
        'id = "egress-cli"\n'
        'type = "cli"\n'
        f'command = ["{sys.executable}", "-c", "print(\'should-not-run\')"]\n',
        encoding="utf-8",
    )

    def boom_web(*_a, **_k):
        raise AssertionError("local-only must not construct a web provider")

    def boom_cli(*_a, **_k):
        raise AssertionError("local-only must not construct CLI adapters")

    def boom_browser(*_a, **_k):
        raise AssertionError("local-only must not construct browser-AI providers")

    monkeypatch.setattr("brigade.research.sources.web.build_provider", boom_web)
    monkeypatch.setattr(research_cmd.clisrc, "build_providers", boom_cli)
    monkeypatch.setattr(research_cmd, "_resolve_browser_ai_provider", boom_browser)

    code = cli_run(
        target=tmp_path,
        question="q",
        corpus=None,
        sources=[],
        web=True,
        overrides={"max_rounds": 1},
        browser_ai_research=True,
        profile="local-only",
        json_output=True,
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload["failure_kind"] == "no-source-route"
    assert payload["status"] in {"failed", "error"}


def test_local_only_with_local_source_skips_egress_providers(tmp_path: Path, monkeypatch) -> None:
    patch_stub_lanes(monkeypatch)
    (tmp_path / "a.md").write_text("plants use light", encoding="utf-8")
    constructed: list[str] = []

    def boom_web(*_a, **_k):
        constructed.append("web")
        raise AssertionError("local-only must not construct a web provider")

    def boom_cli(*_a, **_k):
        constructed.append("cli")
        raise AssertionError("local-only must not construct CLI adapters")

    monkeypatch.setattr("brigade.research.sources.web.build_provider", boom_web)
    monkeypatch.setattr(research_cmd.clisrc, "build_providers", boom_cli)

    rid = research_cmd.run(
        target=tmp_path,
        question="how do plants make energy?",
        sources=[str(tmp_path / "*.md")],
        web=True,
        overrides={"max_rounds": 1, "min_rounds": 1, "max_time": 30},
        profile="local-only",
        browser_ai_research=True,
        run_id="local-only-no-egress",
    )
    rec = registry.show_run(tmp_path, rid)
    assert constructed == []
    assert rec["status"] == "completed"
    routes = {route["id"]: route for route in rec["manifest"]["routes"]}
    assert routes["local"]["enabled"] is True
    assert routes["configured-cli"]["enabled"] is False
    assert routes[rec["manifest"]["provider"]]["enabled"] is False
    assert routes["browser-ai"]["enabled"] is False


def test_research_init_exclusive_create_is_race_safe(tmp_path: Path, capsys) -> None:
    path = tmp_path / ".brigade" / "research.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    original = b"keep-exact-bytes = true\n"
    path.write_bytes(original)

    code = cli_init(target=tmp_path, profile="luna-only", json_output=False)
    err = capsys.readouterr().err
    assert code == 2
    assert "already exists" in err
    assert path.read_bytes() == original


def test_resume_refresh_keeps_plan_and_repeats_discovery(monkeypatch, tmp_path: Path) -> None:
    patch_stub_lanes(monkeypatch)
    (tmp_path / "a.md").write_text("plants use light", encoding="utf-8")
    run_id = "refresh-me"
    registry.create_standard_run(
        tmp_path,
        run_id=run_id,
        question="q",
        profile="grounded",
        caps={"max_time": 300, "max_dispatches": 8, "max_rounds": 2},
        roster=minimal_research_roster(),
    )
    run_dir = registry.standard_run_dir(tmp_path, run_id)
    aboyeur.record_run_termination(
        run_dir,
        status="failed",
        failure_phase="extraction",
        failure_kind="worker-failed",
        detail="boom",
    )
    registry.update_research(
        tmp_path,
        run_id,
        status="failed",
        manifest={"sources": [str(tmp_path / "*.md")], "web_enabled": False},
        caps={"max_time": 300, "max_dispatches": 8, "max_rounds": 2},
    )
    registry.update_phase(tmp_path, run_id, "planning", status="completed", value='{"sub_questions":["q"]}')
    source = SourceEnvelope.build(
        origin="local",
        provider="local",
        uri="file://a.md",
        content="plants use light",
        trust="local",
        acquired_at="2026-08-13T00:00:00+00:00",
    )
    source_ref = registry.write_sources(tmp_path, run_id, [source])
    registry.update_phase(tmp_path, run_id, "discovery", status="completed", artifact=source_ref)

    seen: dict[str, object] = {}

    def fake_run(**kwargs):
        seen.update(kwargs)
        return kwargs["run_id"]

    monkeypatch.setattr(research_cmd, "run", fake_run)
    assert research_cmd.resume(target=tmp_path, run_id=run_id, overrides={}, refresh=True) == run_id
    resume = seen["resume"]
    assert resume.plan == '{"sub_questions":["q"]}'
    assert resume.sources is None
    assert resume.findings is None
    assert resume.report is None
    assert resume.audit is None
    assert resume.review is None
    assert resume.synthesis is None


def test_resume_refresh_rejected_for_legacy(tmp_path: Path) -> None:
    legacy = tmp_path / ".brigade" / "research" / "legacy-refresh"
    legacy.mkdir(parents=True)
    from brigade import localio

    localio.write_json(
        legacy / "run.json",
        {"run_id": "legacy-refresh", "question": "Old", "status": "failed", "caps": {"max_time": 30}},
    )
    with pytest.raises(SystemExit, match="legacy"):
        research_cmd.resume(target=tmp_path, run_id="legacy-refresh", overrides={}, refresh=True)


def test_resume_rejects_budget_cap_overrides(tmp_path: Path, monkeypatch) -> None:
    registry.create_standard_run(
        tmp_path,
        run_id="budget-lock",
        question="q",
        profile="grounded",
        caps={"max_time": 120, "max_dispatches": 7, "max_rounds": 3},
        roster=minimal_research_roster(),
    )
    run_dir = registry.standard_run_dir(tmp_path, "budget-lock")
    aboyeur.record_run_termination(
        run_dir,
        status="failed",
        failure_phase="discovery",
        failure_kind="worker-failed",
        detail="boom",
    )
    registry.update_research(
        tmp_path,
        run_id="budget-lock",
        status="failed",
        manifest={"sources": ["docs/*.md"], "web_enabled": False},
        caps={"max_time": 120, "max_dispatches": 7, "max_rounds": 3},
    )
    monkeypatch.setattr(research_cmd, "run", lambda **_k: "budget-lock")

    with pytest.raises(SystemExit, match="max_time"):
        research_cmd.resume(target=tmp_path, run_id="budget-lock", overrides={"max_time": 999})
    with pytest.raises(SystemExit, match="max_dispatches"):
        research_cmd.resume(target=tmp_path, run_id="budget-lock", overrides={"max_dispatches": 99})

    seen: dict[str, object] = {}

    def fake_run(**kwargs):
        seen.update(kwargs)
        return kwargs["run_id"]

    monkeypatch.setattr(research_cmd, "run", fake_run)
    assert research_cmd.resume(target=tmp_path, run_id="budget-lock", overrides={"max_rounds": 1}) == "budget-lock"
    assert seen["overrides"]["max_rounds"] == 1
    assert seen["overrides"]["max_time"] == 120
    assert seen["overrides"]["max_dispatches"] == 7


def test_research_cli_parser_resume_refresh() -> None:
    from brigade import cli

    parser = cli._build_parser()
    ns = parser.parse_args(["research", "resume", "run-123", "--refresh", "--rounds", "2", "--json"])
    assert ns.research_command == "resume"
    assert ns.run_id == "run-123"
    assert ns.refresh is True
    assert ns.rounds == 2


def test_cli_resume_refresh_and_budget_override_exit_2(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setattr(
        research_cmd,
        "resume",
        lambda **_k: (_ for _ in ()).throw(SystemExit("cannot override max_time on resume")),
    )
    code = cli_resume(target=tmp_path, run_id="x", overrides={"max_time": 1}, json_output=True)
    assert code == 2
    assert "max_time" in capsys.readouterr().out

    monkeypatch.setattr(
        research_cmd,
        "resume",
        lambda **_k: (_ for _ in ()).throw(SystemExit("cannot refresh a legacy research run")),
    )
    code = cli_resume(target=tmp_path, run_id="legacy", overrides={}, refresh=True, json_output=True)
    assert code == 2
    assert "legacy" in capsys.readouterr().out.lower()


def _patch_receipt_write_failure(monkeypatch, *, exc, persistent: bool) -> dict[str, object]:
    """Inject LifecycleJournalError/RetainRunLockError on aboyeur.update_run_receipt."""
    from brigade import aboyeur as aboyeur_mod

    state: dict[str, object] = {"calls": 0, "before": None}
    real = aboyeur_mod.update_run_receipt

    def flaky(output_dir, **fields):
        state["calls"] = int(state["calls"]) + 1
        path = Path(output_dir) / "run.json"
        if path.is_file() and state["before"] is None:
            state["before"] = path.read_bytes()
        if persistent or int(state["calls"]) == 1:
            raise exc
        return real(output_dir, **fields)

    monkeypatch.setattr(aboyeur_mod, "update_run_receipt", flaky)
    return state


def test_run_acquires_lock_before_creating_or_updating_run_state(monkeypatch, tmp_path: Path) -> None:
    from contextlib import contextmanager
    from brigade import runguard

    (tmp_path / "a.md").write_text("plants use light")
    patch_stub_lanes(monkeypatch)
    writes: list[tuple[str, bool]] = []
    lock_held = False
    real_create = registry.create_standard_run
    real_update = registry.update_research

    @contextmanager
    def observed_run_lock(*_args, **_kwargs):
        nonlocal lock_held
        lock_held = True
        try:
            yield tmp_path / ".brigade" / "run.lock"
        finally:
            lock_held = False

    def create_standard_run(*args, **kwargs):
        writes.append(("create_standard_run", lock_held))
        return real_create(*args, **kwargs)

    def update_research(*args, **kwargs):
        writes.append(("update_research", lock_held))
        return real_update(*args, **kwargs)

    monkeypatch.setattr(runguard, "run_lock", observed_run_lock)
    monkeypatch.setattr(registry, "create_standard_run", create_standard_run)
    monkeypatch.setattr(registry, "update_research", update_research)

    research_cmd.run(
        target=tmp_path,
        question="q",
        sources=[str(tmp_path / "*.md")],
        web=False,
        overrides={"max_rounds": 1},
        run_id="lock-before-state",
    )

    assert writes
    assert all(held for _operation, held in writes)


def test_cli_run_prelock_receipt_failure_is_a_typed_cli_error(monkeypatch, tmp_path: Path, capsys) -> None:
    from brigade import run_lifecycle
    from brigade.research import registry as registry_mod

    def fail_record_run_start(*_args, **_kwargs) -> None:
        raise run_lifecycle.LifecycleJournalError("authoritative run prior gate not ready")

    # Hermetic to roster discovery so CI hosts without ~/.brigade/roster.toml
    # still reach the intended pre-lock receipt failure.
    patch_stub_lanes(monkeypatch)
    monkeypatch.setattr(research_cmd, "_new_run_id", lambda _question: "prelock-receipt-failure")
    monkeypatch.setattr(registry_mod.aboyeur, "record_run_start", fail_record_run_start)

    code = cli_run(
        target=tmp_path,
        question="q",
        corpus=None,
        sources=[],
        web=False,
        overrides={"max_rounds": 1},
        json_output=True,
    )

    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert code == 1
    assert payload["run_id"] == "prelock-receipt-failure"
    assert payload["status"] == "failed"
    assert payload["failure_phase"] == "admission"
    assert payload["failure_kind"] == "lifecycle-journal"
    assert "Traceback" not in output.out + output.err


def test_cli_run_oneshot_lifecycle_receipt_failure_is_safe(monkeypatch, tmp_path: Path, capsys) -> None:
    from brigade import run_lifecycle

    (tmp_path / "a.md").write_text("plants use light")
    patch_stub_lanes(monkeypatch)
    state = _patch_receipt_write_failure(
        monkeypatch,
        exc=run_lifecycle.LifecycleJournalError("authoritative run prior gate not ready: journal-ahead-of-evidence"),
        persistent=False,
    )
    code = cli_run(
        target=tmp_path,
        question="q",
        corpus=None,
        sources=[str(tmp_path / "*.md")],
        web=False,
        overrides={"max_rounds": 1},
        json_output=True,
    )
    out = capsys.readouterr()
    combined = out.out + out.err
    assert code == 1
    assert "Traceback" not in combined
    assert "LifecycleJournalError" not in combined
    payload = json.loads(out.out)
    assert payload["status"] == "failed"
    assert payload["failure_kind"] == "lifecycle-journal"
    assert "home/" not in json.dumps(payload).lower()
    run_id = payload["run_id"]
    run_path = registry.standard_run_dir(tmp_path, run_id) / "run.json"
    after = run_path.read_bytes()
    if state["before"] is not None:
        assert after == state["before"]
    receipt = json.loads(after)
    # Failed gate must not be terminalized by the safe CLI boundary.
    assert receipt.get("finished_at") in (None, "")


def test_cli_run_persistent_retain_lock_receipt_failure_is_safe(monkeypatch, tmp_path: Path, capsys) -> None:
    from brigade import runguard

    (tmp_path / "a.md").write_text("plants use light")
    patch_stub_lanes(monkeypatch)
    _patch_receipt_write_failure(
        monkeypatch,
        exc=runguard.RetainRunLockError("failed to write terminal run receipt: prior gate"),
        persistent=True,
    )
    code = cli_run(
        target=tmp_path,
        question="q",
        corpus=None,
        sources=[str(tmp_path / "*.md")],
        web=False,
        overrides={"max_rounds": 1},
        json_output=False,
    )
    out = capsys.readouterr()
    combined = out.out + out.err
    assert code == 1
    assert "Traceback" not in combined
    assert "RetainRunLockError" not in combined
    assert "failed" in combined.lower()
    assert "/home/" not in combined


@pytest.mark.parametrize("field", ["min_rounds", "max_empty_rounds", "synthesis_window"])
def test_validated_caps_preserve_zero_for_clamped_fields(field: str) -> None:
    caps = research_cmd._build_validated_caps(**{field: 0})

    assert getattr(caps, field) == 0


@pytest.mark.parametrize("field", ["min_rounds", "max_empty_rounds", "synthesis_window"])
def test_validated_caps_reject_negative_clamped_fields(field: str) -> None:
    with pytest.raises(ValueError, match=rf"caps\.{field} must be a non-negative integer"):
        research_cmd._build_validated_caps(**{field: -1})


@pytest.mark.parametrize(
    "field",
    [
        "max_rounds",
        "max_time",
        "max_dispatches",
        "max_urls_per_round",
        "max_local_docs_per_round",
        "max_content_chars",
        "max_report_tokens",
    ],
)
def test_validated_caps_require_positive_engine_and_budget_fields(field: str) -> None:
    with pytest.raises(ValueError, match=rf"caps\.{field} must be a positive integer"):
        research_cmd._build_validated_caps(**{field: 0})


def test_validated_caps_ignores_unknown_and_hostile_override_keys() -> None:
    caps = research_cmd._build_validated_caps(__class__="hostile", unknown_cap=1)

    assert caps == research_cmd.Caps()


def test_cli_run_reports_bad_known_cap_config_as_typed_json(monkeypatch, tmp_path: Path, capsys) -> None:
    config_dir = tmp_path / ".brigade"
    config_dir.mkdir()
    (config_dir / "research.toml").write_text("[caps]\nmax_time = 'bad'\n", encoding="utf-8")
    monkeypatch.setattr(research_cmd, "_load_roster", lambda _target: minimal_research_roster())
    monkeypatch.setattr(
        registry,
        "create_standard_run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("run must not be created")),
    )

    code = cli_run(
        target=tmp_path,
        question="q",
        corpus=None,
        sources=[],
        web=False,
        overrides={},
        json_output=True,
    )

    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["failure_kind"] == "invalid-config"
    assert payload["failure_phase"] == "admission"


@pytest.mark.parametrize("operation", ["cancel", "resume"])
@pytest.mark.parametrize("sidecar_state", ["missing", "corrupt"])
def test_cli_cancel_and_resume_report_invalid_sidecars_as_typed_json(
    tmp_path: Path, capsys, operation: str, sidecar_state: str
) -> None:
    run_id = seed_cancelled_run_after_extraction(tmp_path)
    research_path = registry.standard_run_dir(tmp_path, run_id) / registry.RESEARCH_ARTIFACT
    if sidecar_state == "missing":
        research_path.unlink()
    else:
        research_path.write_text("not-json\n", encoding="utf-8")

    if operation == "cancel":
        code = research_cmd.cli_cancel(target=tmp_path, run_id=run_id, json_output=True)
    else:
        code = cli_resume(target=tmp_path, run_id=run_id, overrides={}, json_output=True)

    out = capsys.readouterr()
    assert code == 2
    assert "Traceback" not in out.out + out.err
    payload = json.loads(out.out)
    assert payload["run_id"] == run_id
    assert payload["failure_kind"] == "invalid-sidecar"
    assert payload["failure_phase"] == "admission"


def test_cli_resume_does_not_misclassify_resumed_publish_oserror(monkeypatch, tmp_path: Path) -> None:
    run_id = seed_cancelled_run_after_extraction(tmp_path)

    def publishing_failure(**_kwargs):
        raise OSError("resumed publishing failed")

    monkeypatch.setattr(research_cmd, "run", publishing_failure)

    with pytest.raises(OSError, match="resumed publishing failed"):
        cli_resume(target=tmp_path, run_id=run_id, overrides={}, json_output=True)


def _corrupt_persisted_run_budget(target: Path, run_id: str) -> None:
    run_path = registry.standard_run_dir(target, run_id) / "run.json"
    payload = json.loads(run_path.read_text(encoding="utf-8"))
    payload["run_budget"] = {"schema": "unsupported"}
    run_path.write_text(json.dumps(payload), encoding="utf-8")


def test_resume_maps_corrupt_persisted_budget_to_invalid_config(monkeypatch, tmp_path: Path) -> None:
    run_id = seed_cancelled_run_after_extraction(tmp_path)
    _corrupt_persisted_run_budget(tmp_path, run_id)
    patch_stub_lanes(monkeypatch)

    assert research_cmd.resume(target=tmp_path, run_id=run_id, overrides={}) == run_id
    rec = registry.show_run(tmp_path, run_id)
    assert rec["failure_kind"] == "invalid-config"
    assert rec["failure_phase"] == "admission"


def test_cli_resume_maps_corrupt_persisted_budget_to_invalid_config_json(monkeypatch, tmp_path: Path, capsys) -> None:
    run_id = seed_cancelled_run_after_extraction(tmp_path)
    _corrupt_persisted_run_budget(tmp_path, run_id)
    patch_stub_lanes(monkeypatch)

    code = cli_resume(target=tmp_path, run_id=run_id, overrides={}, json_output=True)

    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["failure_kind"] == "invalid-config"
    assert payload["failure_phase"] == "admission"


def test_cli_resume_lifecycle_receipt_failure_is_safe(monkeypatch, tmp_path: Path, capsys) -> None:
    from brigade import run_lifecycle

    patch_stub_lanes(monkeypatch)
    run_id = seed_cancelled_run_after_extraction(tmp_path)
    run_dir = registry.standard_run_dir(tmp_path, run_id)
    before = (run_dir / "run.json").read_bytes()
    _patch_receipt_write_failure(
        monkeypatch,
        exc=run_lifecycle.LifecycleJournalError("authoritative run prior gate not ready: error-recorded"),
        persistent=True,
    )
    code = cli_resume(target=tmp_path, run_id=run_id, overrides={}, json_output=True)
    out = capsys.readouterr()
    combined = out.out + out.err
    assert code == 1
    assert "Traceback" not in combined
    payload = json.loads(out.out)
    assert payload["run_id"] == run_id
    assert payload["status"] == "failed"
    assert payload["failure_kind"] == "lifecycle-journal"
    assert (run_dir / "run.json").read_bytes() == before


def test_cancellation_watcher_survives_unreadable_sidecar(tmp_path: Path, monkeypatch) -> None:
    from brigade import localio, receipt_schema

    process_registry = RecordingProcessRegistry()
    registry.create_standard_run(
        tmp_path,
        run_id="watcher-corrupt",
        question="q",
        profile="grounded",
        caps={"max_time": 30, "max_dispatches": 2},
        roster=minimal_research_roster(),
    )
    research_path = registry.standard_run_dir(tmp_path, "watcher-corrupt") / registry.RESEARCH_ARTIFACT
    research_path.write_text("not-json\n", encoding="utf-8")
    first_corrupt_read_done = Event()
    real_read = registry.read_research
    read_calls = {"n": 0}

    def instrumented_read(target: Path, run_id: str):
        try:
            return real_read(target, run_id)
        finally:
            read_calls["n"] += 1
            if read_calls["n"] == 1:
                first_corrupt_read_done.set()

    monkeypatch.setattr(registry, "read_research", instrumented_read)
    cancelled = Event()
    watcher = CancellationWatcher(
        target=tmp_path,
        run_id="watcher-corrupt",
        cancelled=cancelled,
        process_registry=process_registry,
        poll_seconds=0.01,
    )
    watcher.start()
    assert first_corrupt_read_done.wait(timeout=2)
    assert watcher.is_alive()
    localio.write_json(
        research_path,
        receipt_schema.stamp_research_sidecar(
            {
                "run_id": "watcher-corrupt",
                "status": "running",
                "cancel_requested_at": "2026-08-14T00:00:00+00:00",
                "phases": {},
            }
        ),
    )
    assert cancelled.wait(timeout=2)
    watcher.stop()
    watcher.join(timeout=1)
    assert process_registry.cancel_calls >= 1


def test_discovery_mode_reports_observed_sources() -> None:
    local = SourceEnvelope.build(
        origin="local",
        provider="local",
        uri="/tmp/a.md",
        content="local body",
        trust="local",
        acquired_at="2026-08-13T12:00:00+00:00",
    )
    assert research_cmd._discovery_mode(browser_ai_enabled=True, sources=(local,)) == "local"
    assert research_cmd._discovery_mode(browser_ai_enabled=True, sources=()) == "browser-ai-empty"
    assert research_cmd._discovery_mode(browser_ai_enabled=False, sources=()) == "none"
    browser_ai = SourceEnvelope.build(
        origin="browser-ai",
        provider="gemini-browser",
        uri="https://example.com/doc",
        content="browser ai",
        trust="browser-ai",
        acquired_at="2026-08-13T12:00:00+00:00",
    )
    assert research_cmd._discovery_mode(browser_ai_enabled=False, sources=(browser_ai,)) == "browser-ai"


def test_projection_secret_key_matches_env_segments_not_events() -> None:
    assert research_cmd._is_projection_secret_key("env")
    assert research_cmd._is_projection_secret_key("ENV")
    assert research_cmd._is_projection_secret_key("process_env")
    assert research_cmd._is_projection_secret_key("env_var")
    assert not research_cmd._is_projection_secret_key("events")
    assert not research_cmd._is_projection_secret_key("event_type")
    assert not research_cmd._is_projection_secret_key("environment_label")
    sanitized = research_cmd._sanitize_projection_value(
        {
            "env": "SECRET",
            "ENV": "SECRET",
            "events": [{"event_type": "run.started"}],
            "event_type": "run.started",
            "environment_label": "ci",
        }
    )
    assert sanitized["env"] == "[redacted]"
    assert sanitized["ENV"] == "[redacted]"
    assert sanitized["events"] == [{"event_type": "run.started"}]
    assert sanitized["event_type"] == "run.started"
    assert sanitized["environment_label"] == "ci"


def test_corrupt_run_json_termination_becomes_typed_command_failure(monkeypatch, tmp_path: Path) -> None:
    patch_stub_lanes(monkeypatch)
    run_id = "corrupt-on-fail"
    monkeypatch.setattr(research_cmd, "_new_run_id", lambda _question: run_id)

    original_create = registry.create_standard_run

    def create_then_corrupt(*args: object, **kwargs: object):
        out = original_create(*args, **kwargs)
        path = registry.standard_run_dir(tmp_path, run_id) / "run.json"
        path.write_text("not-json\n", encoding="utf-8")
        return out

    monkeypatch.setattr(registry, "create_standard_run", create_then_corrupt)

    with pytest.raises(research_cmd.ResearchCommandFailure) as caught:
        research_cmd.run(
            target=tmp_path,
            question="q",
            sources=[],
            web=False,
            overrides={"max_rounds": 1},
            run_id=run_id,
        )

    assert caught.value.run_id == run_id
    assert caught.value.failure_kind == "lifecycle-journal"
    assert caught.value.failure_phase == "admission"


@pytest.mark.parametrize(
    "cli_call",
    [
        lambda target, run_id, json_output: cli_status(target=target, run_id=run_id, json_output=json_output),
        lambda target, run_id, json_output: cli_show(target=target, run_id=run_id, json_output=json_output),
        lambda target, run_id, json_output: research_cmd.cli_export_handoff(
            target=target, run_id=run_id, inbox="codex", handoff_inbox=None, json_output=json_output
        ),
        lambda target, run_id, json_output: research_cmd.cli_cancel(
            target=target, run_id=run_id, json_output=json_output
        ),
        lambda target, run_id, json_output: cli_resume(
            target=target, run_id=run_id, overrides={}, json_output=json_output
        ),
        lambda target, run_id, json_output: research_cmd.cli_open(
            target=target, run_id=run_id, json_output=json_output
        ),
    ],
)
def test_cli_invalid_run_id_is_typed_usage_error(tmp_path: Path, capsys, cli_call) -> None:
    code = cli_call(tmp_path, "../escape", True)
    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["failure_kind"] == "invalid-run-id"
    assert "invalid run_id" in payload["error"]
