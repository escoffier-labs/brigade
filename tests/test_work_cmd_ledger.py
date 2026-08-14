import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from brigade import cli
from brigade import dogfood_cmd
from brigade import work_cmd

from tests.work_cmd_test_helpers import (
    _write_json,
    _init_git_repo,
    _plan_task_id,
    _make_research_run,
    _accepted_plan_task_id,
    _assert_no_install_dirs,
)


def test_work_task_ledger_add_list_show_and_done(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    times = iter(
        [
            datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 12, 30, 0, tzinfo=timezone.utc),
        ]
    )
    monkeypatch.setattr(work_cmd.helpers, "_now", lambda: next(times))

    assert work_cmd.task_add(target=tmp_path, text="Build task ledger") == 0
    out = capsys.readouterr().out
    assert "task:" in out
    task_id = out.split("task: ", 1)[1].splitlines()[0]

    assert work_cmd.tasks(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "work tasks:" in out
    assert task_id in out
    assert "[pending] ready [task normal acceptance=0] Build task ledger" in out

    assert work_cmd.task_show(target=tmp_path, task_id=task_id[:12]) == 0
    out = capsys.readouterr().out
    assert f"task: {task_id}" in out
    assert "status: pending" in out
    assert "type: task" in out
    assert "priority: normal" in out
    assert "acceptance: 0" in out
    assert "text: Build task ledger" in out

    assert work_cmd.task_done(target=tmp_path, task_id=task_id[:12]) == 0
    assert "status: done" in capsys.readouterr().out
    assert work_cmd.tasks(target=tmp_path) == 0
    assert "tasks: none" in capsys.readouterr().out
    assert work_cmd.tasks(target=tmp_path, all_tasks=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["tasks"][0]["status"] == "done"
    assert payload["tasks"][0]["completed_at"] == "2026-05-26T12:30:00+00:00"


def test_work_task_add_from_next_deduplicates_pending_task(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    dogfood_cmd.init(target=tmp_path)
    run_dir = tmp_path / ".brigade" / "runs" / "latest"
    run_dir.mkdir(parents=True)
    _write_json(run_dir / "run.json", {"started_at": "2026-05-26T12:10:00Z", "status": "ok", "task": "review"})
    (run_dir / "final.txt").write_text("Done.\n\nNext step: Build from extracted next.\n")
    monkeypatch.setattr(
        work_cmd.helpers,
        "_now",
        lambda: datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
    )

    assert work_cmd.task_add(target=tmp_path, from_next=True) == 0
    out = capsys.readouterr().out
    assert "Build from extracted next." in out
    assert "created: True" in out
    first_id = out.split("task: ", 1)[1].splitlines()[0]
    assert work_cmd.task_add(target=tmp_path, from_next=True) == 0
    out = capsys.readouterr().out
    assert f"task: {first_id}" in out
    assert "created: False" in out
    assert work_cmd.next(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["next_source"] == "task_ledger"
    assert payload["next"] == "Build from extracted next."
    assert payload["task_id"]


def test_work_task_add_stores_metadata_acceptance_and_plan(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd.helpers,
        "_now",
        lambda: datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
    )

    assert (
        work_cmd.task_add(
            target=tmp_path,
            text="Build issue loop",
            task_type="feature",
            priority="high",
            acceptance=["Adds metadata", "Shows criteria in the plan"],
        )
        == 0
    )
    out = capsys.readouterr().out
    task_id = out.split("task: ", 1)[1].splitlines()[0]
    assert "type: feature" in out
    assert "priority: high" in out
    assert "acceptance: 2" in out

    assert work_cmd.task_plan(target=tmp_path, task_id=task_id[:12]) == 0
    out = capsys.readouterr().out
    assert "task: " in out
    assert "type: feature" in out
    assert "priority: high" in out
    assert "  - Adds metadata" in out
    assert "  - Shows criteria in the plan" in out
    assert "suggested_command: brigade work run" in out

    assert work_cmd.task_plan(target=tmp_path, task_id=task_id[:12], json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["type"] == "feature"
    assert payload["priority"] == "high"
    assert payload["acceptance_count"] == 2
    assert payload["acceptance_missing"] is False


def test_work_task_add_template_preserves_explicit_acceptance_and_plan(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd.helpers,
        "_now",
        lambda: datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
    )

    assert (
        work_cmd.task_add(
            target=tmp_path,
            text="Fix login redirect",
            task_type="bug",
            priority="high",
            template="bugfix",
            acceptance=["The login redirect works in the browser smoke test."],
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "template: bugfix" in out
    task_id = out.split("task: ", 1)[1].splitlines()[0]
    ledger = json.loads((tmp_path / ".brigade" / "work" / "tasks.json").read_text())
    task = ledger["tasks"][0]
    assert task["type"] == "bug"
    assert task["priority"] == "high"
    assert task["template"] == "bugfix"
    assert "The bug is reproduced by a focused failing test or equivalent fixture." in task["acceptance"]
    assert "The login redirect works in the browser smoke test." in task["acceptance"]

    assert work_cmd.task_plan(target=tmp_path, task_id=task_id[:12]) == 0
    out = capsys.readouterr().out
    assert "template: bugfix" in out
    assert "guidance:" in out
    assert "Reproduce the failing behavior first." in out
    assert "The login redirect works in the browser smoke test." in out


def test_task_plan_write_creates_both_artifacts_with_full_schema(tmp_path, capsys):
    _init_git_repo(tmp_path)
    task_id = _plan_task_id(tmp_path, capsys)

    assert (
        work_cmd.task_plan(
            target=tmp_path,
            task_id=task_id[:12],
            write=True,
            assumptions=["API is stable"],
            risks=["rate limits"],
            sources=["issue #42"],
            next_command="brigade work run",
            title="Custom plan title",
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "wrote plan:" in out
    assert "status: draft" in out

    json_path, md_path = work_cmd._plan_paths(tmp_path, task_id)
    assert json_path.is_file()
    assert md_path.is_file()
    assert json_path.name == f"{task_id}.json"
    assert md_path.name == f"{task_id}.plan.md"

    receipt = json.loads(json_path.read_text())
    expected_keys = {
        "task_id",
        "kind",
        "title",
        "status",
        "created_at",
        "updated_at",
        "source_context",
        "assumptions",
        "acceptance",
        "risks",
        "steps",
        "decisions",
        "next_command",
        "receipt_paths",
        "research_runs",
    }
    assert set(receipt.keys()) == expected_keys
    assert receipt["task_id"] == task_id
    assert receipt["kind"] == "plan"
    assert receipt["steps"] == []
    assert receipt["decisions"] == []
    assert receipt["title"] == "Custom plan title"
    assert receipt["status"] == "draft"
    assert receipt["created_at"] == receipt["updated_at"]
    assert receipt["assumptions"] == ["API is stable"]
    assert receipt["risks"] == ["rate limits"]
    assert receipt["source_context"] == ["issue #42"]
    assert receipt["acceptance"] == ["Plan is written", "Plan is reviewed"]
    assert receipt["next_command"] == "brigade work run"
    paths = receipt["receipt_paths"]
    assert ".brigade/work/tasks.json" in paths
    assert f".brigade/work/plans/{task_id}.json" in paths
    assert f".brigade/work/plans/{task_id}.plan.md" in paths


def test_task_plan_write_defaults_title_to_task_text(tmp_path, capsys):
    _init_git_repo(tmp_path)
    task_id = _plan_task_id(tmp_path, capsys, text="Implement the loop")

    assert work_cmd.task_plan(target=tmp_path, task_id=task_id[:12], write=True) == 0
    capsys.readouterr()
    json_path, _ = work_cmd._plan_paths(tmp_path, task_id)
    receipt = json.loads(json_path.read_text())
    assert receipt["title"] == "Implement the loop"
    assert receipt["next_command"] == "brigade work run"


def test_task_plan_write_second_appends_dedupes_preserves_created(tmp_path, capsys):
    _init_git_repo(tmp_path)
    task_id = _plan_task_id(tmp_path, capsys)

    assert (
        work_cmd.task_plan(
            target=tmp_path,
            task_id=task_id[:12],
            write=True,
            assumptions=["API is stable"],
            risks=["rate limits"],
            sources=["issue #42"],
        )
        == 0
    )
    capsys.readouterr()
    json_path, _ = work_cmd._plan_paths(tmp_path, task_id)
    first = json.loads(json_path.read_text())
    first_created = first["created_at"]

    assert (
        work_cmd.task_plan(
            target=tmp_path,
            task_id=task_id[:12],
            write=True,
            assumptions=["API is stable", "DB is migrated"],
            risks=["rate limits"],
            sources=["issue #99"],
            accept=True,
        )
        == 0
    )
    capsys.readouterr()
    second = json.loads(json_path.read_text())
    assert second["created_at"] == first_created
    assert second["updated_at"] >= first_created
    assert second["assumptions"] == ["API is stable", "DB is migrated"]
    assert second["risks"] == ["rate limits"]
    assert second["source_context"] == ["issue #42", "issue #99"]
    assert second["status"] == "accepted"


def test_task_plan_write_rerenders_acceptance_from_task(tmp_path, capsys):
    _init_git_repo(tmp_path)
    task_id = _plan_task_id(tmp_path, capsys, acceptance=["Only criterion"])
    assert work_cmd.task_plan(target=tmp_path, task_id=task_id[:12], write=True) == 0
    capsys.readouterr()
    json_path, _ = work_cmd._plan_paths(tmp_path, task_id)
    receipt = json.loads(json_path.read_text())
    assert receipt["acceptance"] == ["Only criterion"]


def test_plan_md_has_title_sections_items_and_none_recorded(tmp_path, capsys):
    _init_git_repo(tmp_path)
    task_id = _plan_task_id(tmp_path, capsys)
    assert (
        work_cmd.task_plan(
            target=tmp_path,
            task_id=task_id[:12],
            write=True,
            assumptions=["API is stable"],
            sources=["issue #42"],
            title="Readable Plan",
        )
        == 0
    )
    capsys.readouterr()
    _, md_path = work_cmd._plan_paths(tmp_path, task_id)
    md = md_path.read_text()
    assert "# Plan: Readable Plan" in md
    assert "## Source context" in md
    assert "## Assumptions" in md
    assert "## Acceptance criteria" in md
    assert "## Risks" in md
    assert "## Steps" in md
    assert "## Decision checkpoints" in md
    assert "## Next safe command" in md
    assert "## Receipts" in md
    assert "- issue #42" in md
    assert "- API is stable" in md
    assert "- Plan is written" in md
    assert "`brigade work run`" in md
    # Risks / decisions empty -> none recorded marker present
    assert "_none recorded_" in md


def test_task_plan_read_view_shows_artifact_line_and_json(tmp_path, capsys):
    _init_git_repo(tmp_path)
    task_id = _plan_task_id(tmp_path, capsys)
    assert work_cmd.task_plan(target=tmp_path, task_id=task_id[:12], write=True) == 0
    capsys.readouterr()

    assert work_cmd.task_plan(target=tmp_path, task_id=task_id[:12]) == 0
    out = capsys.readouterr().out
    assert "  - Plan is written" in out
    assert "suggested_command: brigade work run" in out
    assert "plan_artifact: draft" in out
    assert f".brigade/work/plans/{task_id}.plan.md" in out
    assert "meta_artifact: none" in out

    assert work_cmd.task_plan(target=tmp_path, task_id=task_id[:12], json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["plan_artifact"] is not None
    assert payload["plan_artifact"]["status"] == "draft"
    assert payload["plan_artifact"]["path"] == f".brigade/work/plans/{task_id}.plan.md"
    assert payload["plan_artifact"]["updated_at"]
    assert payload["meta_artifact"] is None


def test_task_plan_read_view_without_artifact_is_null_and_unchanged(tmp_path, capsys):
    _init_git_repo(tmp_path)
    task_id = _plan_task_id(tmp_path, capsys)

    assert work_cmd.task_plan(target=tmp_path, task_id=task_id[:12]) == 0
    out = capsys.readouterr().out
    assert "  - Plan is written" in out
    assert "  - Plan is reviewed" in out
    assert "suggested_command: brigade work run" in out
    assert "plan_artifact: none" in out
    assert "meta_artifact: none" in out

    assert work_cmd.task_plan(target=tmp_path, task_id=task_id[:12], json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["plan_artifact"] is None
    assert payload["meta_artifact"] is None
    assert payload["acceptance"] == ["Plan is written", "Plan is reviewed"]


def test_task_plan_write_unknown_task_exits_1(tmp_path, capsys):
    _init_git_repo(tmp_path)
    assert work_cmd.task_plan(target=tmp_path, task_id="nope", write=True) == 1
    err = capsys.readouterr().err
    assert "task not found" in err


def test_significant_pending_without_plan_lists_accepted_task(tmp_path, capsys):
    _init_git_repo(tmp_path)
    task_id = _plan_task_id(tmp_path, capsys)

    missing = work_cmd._significant_pending_without_plan(tmp_path)
    assert [task["id"] for task in missing] == [task_id]


def test_significant_pending_without_plan_drops_task_after_plan_written(tmp_path, capsys):
    _init_git_repo(tmp_path)
    task_id = _plan_task_id(tmp_path, capsys)
    assert work_cmd.task_plan(target=tmp_path, task_id=task_id[:12], write=True) == 0
    capsys.readouterr()

    missing = work_cmd._significant_pending_without_plan(tmp_path)
    assert missing == []


def test_significant_pending_without_plan_ignores_insignificant_task(tmp_path, capsys):
    _init_git_repo(tmp_path)
    assert work_cmd.task_add(target=tmp_path, text="Trivial chore") == 0
    capsys.readouterr()

    missing = work_cmd._significant_pending_without_plan(tmp_path)
    assert missing == []
    payload = work_cmd._plan_coverage_payload(tmp_path)
    assert payload == {"pending_total": 1, "significant_without_plan": 0, "task_ids": []}


def test_task_plan_write_from_research_records_entry_and_quarantined_source(tmp_path, capsys):
    _init_git_repo(tmp_path)
    task_id = _plan_task_id(tmp_path, capsys)
    _make_research_run(tmp_path, run_id="r1", question="what is the loop")

    assert (
        work_cmd.task_plan(
            target=tmp_path,
            task_id=task_id[:12],
            write=True,
            from_research="r1",
        )
        == 0
    )
    capsys.readouterr()
    json_path, _ = work_cmd._plan_paths(tmp_path, task_id)
    receipt = json.loads(json_path.read_text())
    runs = receipt["research_runs"]
    assert len(runs) == 1
    assert runs[0]["run_id"] == "r1"
    assert runs[0]["question"] == "what is the loop"
    assert runs[0]["report_path"].endswith("report.md")
    assert any("research:r1 (untrusted-web)" in line for line in receipt["source_context"])


def test_task_plan_write_from_research_renders_quarantined_section(tmp_path, capsys):
    _init_git_repo(tmp_path)
    task_id = _plan_task_id(tmp_path, capsys)
    _make_research_run(tmp_path, run_id="r1", question="what is the loop")

    assert (
        work_cmd.task_plan(
            target=tmp_path,
            task_id=task_id[:12],
            write=True,
            from_research="r1",
        )
        == 0
    )
    capsys.readouterr()
    _, md_path = work_cmd._plan_paths(tmp_path, task_id)
    md = md_path.read_text()
    assert "## Research evidence (quarantined)" in md
    assert md.index("## Research evidence (quarantined)") < md.index("## Receipts")
    assert "untrusted source material" in md
    assert "r1" in md
    assert "what is the loop" in md


def test_task_plan_write_from_research_unknown_returns_1_and_writes_nothing(tmp_path, capsys):
    _init_git_repo(tmp_path)
    task_id = _plan_task_id(tmp_path, capsys)

    assert (
        work_cmd.task_plan(
            target=tmp_path,
            task_id=task_id[:12],
            write=True,
            from_research="nope",
        )
        == 1
    )
    err = capsys.readouterr().err
    assert "research run not found: nope" in err
    json_path, _ = work_cmd._plan_paths(tmp_path, task_id)
    assert not json_path.is_file()


def test_task_plan_write_without_research_has_empty_runs_and_no_section(tmp_path, capsys):
    _init_git_repo(tmp_path)
    task_id = _plan_task_id(tmp_path, capsys)

    assert work_cmd.task_plan(target=tmp_path, task_id=task_id[:12], write=True) == 0
    capsys.readouterr()
    json_path, md_path = work_cmd._plan_paths(tmp_path, task_id)
    receipt = json.loads(json_path.read_text())
    assert receipt["research_runs"] == []
    assert "## Research evidence (quarantined)" not in md_path.read_text()


def test_task_plan_write_from_research_dedupes_same_run(tmp_path, capsys):
    _init_git_repo(tmp_path)
    task_id = _plan_task_id(tmp_path, capsys)
    _make_research_run(tmp_path, run_id="r1", question="what is the loop")

    assert work_cmd.task_plan(target=tmp_path, task_id=task_id[:12], write=True, from_research="r1") == 0
    assert work_cmd.task_plan(target=tmp_path, task_id=task_id[:12], write=True, from_research="r1") == 0
    capsys.readouterr()
    json_path, _ = work_cmd._plan_paths(tmp_path, task_id)
    receipt = json.loads(json_path.read_text())
    assert [r["run_id"] for r in receipt["research_runs"]] == ["r1"]


def doctor_roster():
    from brigade.roster import Agent, Roster

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


def seed_completed_standard_research_run(target):
    from brigade import aboyeur, localio
    from brigade.research import registry
    from brigade.research.types import CitationAudit, CitationRecord

    run_id = "completed-research"
    registry.create_standard_run(
        target,
        run_id=run_id,
        question="What is the loop?",
        profile="grounded",
        caps={"max_time": 300, "max_dispatches": 8},
        roster=doctor_roster(),
    )
    audit_ref = registry.write_citation_audit(
        target,
        run_id,
        CitationAudit(
            accepted=True,
            citations=(
                CitationRecord(
                    token="[source:src-1111111111111111]",
                    source_ids=("src-1111111111111111",),
                    status="accepted",
                ),
            ),
            unresolved=(),
        ),
    )
    localio.write_text_atomic(registry.standard_run_dir(target, run_id) / "report.md", "# Report\n")
    # Production completion shape: plain names in artifacts, digest refs in artifact_refs.
    registry.update_research(
        target,
        run_id,
        review_result="accepted",
        artifacts={
            "report_md": registry.REPORT_MD_ARTIFACT,
            "citation_audit": registry.CITATION_AUDIT_ARTIFACT,
        },
        artifact_refs={"report_md": {"path": "report.md", "digest": "unused"}, "citation_audit": audit_ref},
    )
    aboyeur.update_run_receipt(
        registry.standard_run_dir(target, run_id),
        status="completed",
        artifacts={"report_md": registry.REPORT_MD_ARTIFACT},
    )
    return run_id


def test_plan_artifact_accepts_completed_standard_research_run(tmp_path: Path, capsys) -> None:
    _init_git_repo(tmp_path)
    task_id = _plan_task_id(tmp_path, capsys)
    run_id = seed_completed_standard_research_run(tmp_path)

    code = work_cmd.task_plan(
        target=tmp_path,
        task_id=task_id[:12],
        write=True,
        from_research=run_id,
    )
    capsys.readouterr()
    json_path, _ = work_cmd._plan_paths(tmp_path, task_id)
    artifact = json.loads(json_path.read_text(encoding="utf-8"))["research_runs"][0]

    assert code == 0
    assert artifact["kind"] == "research"
    assert artifact["trust"] == "mixed-provenance"
    assert artifact["citation_audit"] == "accepted"


def test_plan_artifact_prefers_artifact_refs_over_plain_artifact_names(tmp_path: Path, capsys) -> None:
    from brigade.research import registry

    _init_git_repo(tmp_path)
    task_id = _plan_task_id(tmp_path, capsys)
    run_id = seed_completed_standard_research_run(tmp_path)
    rec = registry.show_run(tmp_path, run_id)
    assert rec is not None
    assert rec["artifacts"]["citation_audit"] == registry.CITATION_AUDIT_ARTIFACT
    assert isinstance(rec["artifact_refs"]["citation_audit"], dict)
    assert "digest" in rec["artifact_refs"]["citation_audit"]

    code = work_cmd.task_plan(
        target=tmp_path,
        task_id=task_id[:12],
        write=True,
        from_research=run_id,
    )
    assert code == 0
    capsys.readouterr()


def test_plan_artifact_rejects_active_standard_research_run(tmp_path, capsys) -> None:
    from brigade.research import registry

    _init_git_repo(tmp_path)
    task_id = _plan_task_id(tmp_path, capsys)
    registry.create_standard_run(
        tmp_path,
        run_id="active-research",
        question="Still running?",
        profile="grounded",
        caps={"max_time": 300, "max_dispatches": 8},
        roster=doctor_roster(),
    )

    code = work_cmd.task_plan(
        target=tmp_path,
        task_id=task_id[:12],
        write=True,
        from_research="active-research",
    )
    assert code == 1
    assert "not completed" in capsys.readouterr().err


def test_plan_artifact_rejects_standard_done_status(tmp_path, capsys) -> None:
    from brigade import aboyeur
    from brigade.research import registry

    _init_git_repo(tmp_path)
    task_id = _plan_task_id(tmp_path, capsys)
    run_id = seed_completed_standard_research_run(tmp_path)
    aboyeur.update_run_receipt(registry.standard_run_dir(tmp_path, run_id), status="done")
    registry.update_research(tmp_path, run_id, status="done")

    code = work_cmd.task_plan(
        target=tmp_path,
        task_id=task_id[:12],
        write=True,
        from_research=run_id,
    )
    err = capsys.readouterr().err
    assert code == 1
    assert "not completed" in err


def test_plan_artifact_rejects_tampered_direct_citation_audit(tmp_path, capsys) -> None:
    from brigade import localio
    from brigade.research import registry

    _init_git_repo(tmp_path)
    task_id = _plan_task_id(tmp_path, capsys)
    run_id = seed_completed_standard_research_run(tmp_path)
    audit_path = registry.standard_run_dir(tmp_path, run_id) / registry.CITATION_AUDIT_ARTIFACT
    # Tamper the on-disk audit so the stored digest no longer verifies, while
    # leaving an "accepted" payload that a naive direct-file fallback would trust.
    localio.write_json(
        audit_path,
        {
            "schema": "brigade.research.citation-audit.v1",
            "schema_version": 1,
            "accepted": True,
            "citations": [],
            "unresolved": [],
            "tampered": True,
        },
    )

    code = work_cmd.task_plan(
        target=tmp_path,
        task_id=task_id[:12],
        write=True,
        from_research=run_id,
    )
    err = capsys.readouterr().err
    assert code == 1
    assert "citation audit" in err


def test_plan_artifact_rejects_missing_verified_audit_ref(tmp_path, capsys) -> None:
    from brigade import aboyeur, localio
    from brigade.research import registry

    _init_git_repo(tmp_path)
    task_id = _plan_task_id(tmp_path, capsys)
    run_id = "no-audit-ref"
    registry.create_standard_run(
        tmp_path,
        run_id=run_id,
        question="Missing ref?",
        profile="grounded",
        caps={"max_time": 300, "max_dispatches": 8},
        roster=doctor_roster(),
    )
    localio.write_text_atomic(registry.standard_run_dir(tmp_path, run_id) / "report.md", "# Report\n")
    localio.write_json(
        registry.standard_run_dir(tmp_path, run_id) / registry.CITATION_AUDIT_ARTIFACT,
        {
            "schema": "brigade.research.citation-audit.v1",
            "schema_version": 1,
            "accepted": True,
            "citations": [],
            "unresolved": [],
        },
    )
    registry.update_research(
        tmp_path,
        run_id,
        review_result="accepted",
        artifacts={"report_md": "report.md"},
    )
    aboyeur.update_run_receipt(
        registry.standard_run_dir(tmp_path, run_id),
        status="completed",
        artifacts={"report_md": "report.md"},
    )

    code = work_cmd.task_plan(
        target=tmp_path,
        task_id=task_id[:12],
        write=True,
        from_research=run_id,
    )
    err = capsys.readouterr().err
    assert code == 1
    assert "citation audit" in err
    assert "research run" in err.lower()
    json_path, _ = work_cmd._plan_paths(tmp_path, task_id)
    assert not json_path.is_file()


def test_plan_promote_accepted_writes_draft_proposal(tmp_path, capsys):
    _init_git_repo(tmp_path)
    task_id = _accepted_plan_task_id(tmp_path, capsys)

    assert work_cmd.plan_promote(target=tmp_path, task_id=task_id[:12], as_kind="rule") == 0
    out = capsys.readouterr().out
    assert "wrote draft proposal:" in out
    assert "move it into place yourself" in out

    proposal = work_cmd._proposal_path(tmp_path, task_id, "rule")
    assert proposal.name == f"{task_id}-rule.md"
    assert proposal.is_file()
    text = proposal.read_text()
    assert text.startswith("# Draft rule:")
    assert "DRAFT proposal generated from an accepted plan" in text
    assert "## Acceptance checklist" in text
    assert "- [ ] Plan is written" in text
    assert "- [ ] Plan is reviewed" in text

    _assert_no_install_dirs(tmp_path, task_id)


def test_plan_promote_json_output(tmp_path, capsys):
    _init_git_repo(tmp_path)
    task_id = _accepted_plan_task_id(tmp_path, capsys)

    assert work_cmd.plan_promote(target=tmp_path, task_id=task_id[:12], as_kind="template", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["task_id"] == task_id
    assert payload["as"] == "template"
    assert payload["path"] == f".brigade/work/plan-proposals/{task_id}-template.md"


def test_plan_promote_draft_plan_refused_and_writes_nothing(tmp_path, capsys):
    _init_git_repo(tmp_path)
    task_id = _plan_task_id(tmp_path, capsys)
    assert work_cmd.task_plan(target=tmp_path, task_id=task_id[:12], write=True) == 0
    capsys.readouterr()

    assert work_cmd.plan_promote(target=tmp_path, task_id=task_id[:12], as_kind="rule") == 1
    err = capsys.readouterr().err
    assert "plan not accepted" in err
    assert not work_cmd._proposal_path(tmp_path, task_id, "rule").exists()
    assert not work_cmd._plan_proposals_dir(tmp_path).exists()


def test_plan_promote_missing_plan_returns_1(tmp_path, capsys):
    _init_git_repo(tmp_path)

    assert work_cmd.plan_promote(target=tmp_path, task_id="nope", as_kind="rule") == 1
    err = capsys.readouterr().err
    assert "no plan artifact for task: nope" in err
    assert not work_cmd._plan_proposals_dir(tmp_path).exists()


def test_plan_promote_invalid_as_kind_exits_2(tmp_path, capsys):
    _init_git_repo(tmp_path)
    task_id = _accepted_plan_task_id(tmp_path, capsys)

    assert work_cmd.plan_promote(target=tmp_path, task_id=task_id[:12], as_kind="bogus") == 2
    err = capsys.readouterr().err
    assert "as" in err.lower()
    assert not work_cmd._plan_proposals_dir(tmp_path).exists()


def test_plan_promote_is_idempotent(tmp_path, capsys):
    _init_git_repo(tmp_path)
    task_id = _accepted_plan_task_id(tmp_path, capsys)

    assert work_cmd.plan_promote(target=tmp_path, task_id=task_id[:12], as_kind="skill") == 0
    assert work_cmd.plan_promote(target=tmp_path, task_id=task_id[:12], as_kind="skill") == 0
    capsys.readouterr()
    proposals = work_cmd._plan_proposals_dir(tmp_path)
    files = sorted(proposals.glob("*.md"))
    assert len(files) == 1
    assert files[0].name == f"{task_id}-skill.md"


def test_plan_proposals_lists_and_empty(tmp_path, capsys):
    _init_git_repo(tmp_path)
    # empty case
    assert work_cmd.plan_proposals(target=tmp_path) == 0
    assert "no plan proposals" in capsys.readouterr().out
    assert work_cmd.plan_proposals(target=tmp_path, json_output=True) == 0
    assert json.loads(capsys.readouterr().out) == []

    task_id = _accepted_plan_task_id(tmp_path, capsys)
    assert work_cmd.plan_promote(target=tmp_path, task_id=task_id[:12], as_kind="rule") == 0
    capsys.readouterr()

    assert work_cmd.plan_proposals(target=tmp_path, json_output=True) == 0
    entries = json.loads(capsys.readouterr().out)
    assert len(entries) == 1
    assert entries[0]["task_id"] == task_id
    assert entries[0]["as"] == "rule"
    assert entries[0]["path"] == f".brigade/work/plan-proposals/{task_id}-rule.md"

    assert work_cmd.plan_proposals(target=tmp_path) == 0
    text_out = capsys.readouterr().out
    assert task_id in text_out
    assert "rule" in text_out


def test_meta_plan_write_creates_meta_artifacts_with_kind(tmp_path, capsys):
    _init_git_repo(tmp_path)
    task_id = _plan_task_id(tmp_path, capsys)

    assert (
        work_cmd.task_plan(
            target=tmp_path,
            task_id=task_id[:12],
            write=True,
            kind="meta",
            title="Research synthesis",
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "wrote plan:" in out

    json_path, md_path = work_cmd._plan_paths(tmp_path, task_id, "meta")
    assert json_path.name == f"{task_id}.meta.json"
    assert md_path.name == f"{task_id}.meta.plan.md"
    assert json_path.is_file()
    assert md_path.is_file()

    receipt = json.loads(json_path.read_text())
    assert receipt["kind"] == "meta"
    assert receipt["task_id"] == task_id
    paths = receipt["receipt_paths"]
    assert f".brigade/work/plans/{task_id}.meta.json" in paths
    assert f".brigade/work/plans/{task_id}.meta.plan.md" in paths

    # The plain plan artifact must not exist from a meta write.
    plan_json, _ = work_cmd._plan_paths(tmp_path, task_id, "plan")
    assert not plan_json.is_file()


def test_meta_plan_md_has_banner_and_meta_title(tmp_path, capsys):
    _init_git_repo(tmp_path)
    task_id = _plan_task_id(tmp_path, capsys)
    assert (
        work_cmd.task_plan(
            target=tmp_path,
            task_id=task_id[:12],
            write=True,
            kind="meta",
            title="Deep work",
        )
        == 0
    )
    capsys.readouterr()
    _, md_path = work_cmd._plan_paths(tmp_path, task_id, "meta")
    md = md_path.read_text()
    assert md.startswith("# Meta-plan: Deep work")
    assert "Do NOT jump to the deliverable" in md
    assert f"brigade work task plan {task_id} --write" in md
    assert "## Steps" in md


def test_plan_steps_append_and_render(tmp_path, capsys):
    _init_git_repo(tmp_path)
    task_id = _plan_task_id(tmp_path, capsys)
    assert (
        work_cmd.task_plan(
            target=tmp_path,
            task_id=task_id[:12],
            write=True,
            kind="meta",
            steps=["Gather sources"],
        )
        == 0
    )
    capsys.readouterr()
    assert (
        work_cmd.task_plan(
            target=tmp_path,
            task_id=task_id[:12],
            write=True,
            kind="meta",
            steps=["Gather sources", "Outline the plan"],
        )
        == 0
    )
    capsys.readouterr()
    json_path, md_path = work_cmd._plan_paths(tmp_path, task_id, "meta")
    receipt = json.loads(json_path.read_text())
    assert receipt["steps"] == ["Gather sources", "Outline the plan"]
    md = md_path.read_text()
    assert "## Steps" in md
    assert "- Gather sources" in md
    assert "- Outline the plan" in md


def test_plan_and_meta_coexist_independently(tmp_path, capsys):
    _init_git_repo(tmp_path)
    task_id = _plan_task_id(tmp_path, capsys)
    assert work_cmd.task_plan(target=tmp_path, task_id=task_id[:12], write=True) == 0
    capsys.readouterr()
    assert work_cmd.task_plan(target=tmp_path, task_id=task_id[:12], write=True, kind="meta", steps=["meta step"]) == 0
    capsys.readouterr()

    plan_json, _ = work_cmd._plan_paths(tmp_path, task_id, "plan")
    meta_json, _ = work_cmd._plan_paths(tmp_path, task_id, "meta")
    assert plan_json.is_file()
    assert meta_json.is_file()
    plan_receipt = json.loads(plan_json.read_text())
    meta_receipt = json.loads(meta_json.read_text())
    assert plan_receipt["kind"] == "plan"
    assert plan_receipt["steps"] == []
    assert meta_receipt["kind"] == "meta"
    assert meta_receipt["steps"] == ["meta step"]


def test_task_plan_read_view_shows_both_artifacts(tmp_path, capsys):
    _init_git_repo(tmp_path)
    task_id = _plan_task_id(tmp_path, capsys)
    assert work_cmd.task_plan(target=tmp_path, task_id=task_id[:12], write=True) == 0
    capsys.readouterr()
    assert work_cmd.task_plan(target=tmp_path, task_id=task_id[:12], write=True, kind="meta") == 0
    capsys.readouterr()

    assert work_cmd.task_plan(target=tmp_path, task_id=task_id[:12]) == 0
    out = capsys.readouterr().out
    assert "plan_artifact: draft" in out
    assert "meta_artifact: draft" in out
    assert f".brigade/work/plans/{task_id}.meta.plan.md" in out

    assert work_cmd.task_plan(target=tmp_path, task_id=task_id[:12], json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["plan_artifact"]["status"] == "draft"
    assert payload["meta_artifact"]["status"] == "draft"
    assert payload["meta_artifact"]["path"] == f".brigade/work/plans/{task_id}.meta.plan.md"


def test_meta_plan_via_cli_flags(tmp_path, capsys):
    _init_git_repo(tmp_path)
    task_id = _plan_task_id(tmp_path, capsys)
    assert (
        cli.main(
            [
                "work",
                "task",
                "plan",
                task_id[:12],
                "--target",
                str(tmp_path),
                "--write",
                "--meta",
                "--step",
                "First step",
                "--step",
                "Second step",
            ]
        )
        == 0
    )
    capsys.readouterr()
    json_path, md_path = work_cmd._plan_paths(tmp_path, task_id, "meta")
    receipt = json.loads(json_path.read_text())
    assert receipt["kind"] == "meta"
    assert receipt["steps"] == ["First step", "Second step"]
    md = md_path.read_text()
    assert md.startswith("# Meta-plan:")
    assert "- First step" in md


def test_plan_decision_checkpoint_declare_resolve_and_gate(tmp_path, capsys):
    _init_git_repo(tmp_path)
    task_id = _plan_task_id(tmp_path, capsys)

    assert (
        work_cmd.task_plan(
            target=tmp_path,
            task_id=task_id[:12],
            write=True,
            decision="auth-approach",
            decision_prompt="Which auth approach?",
            decision_options=["oauth", "api-key"],
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "decision_checkpoints_pending: 1" in out
    json_path, md_path = work_cmd._plan_paths(tmp_path, task_id)
    receipt = json.loads(json_path.read_text())
    assert len(receipt["decisions"]) == 1
    decision = receipt["decisions"][0]
    assert decision["id"] == "auth-approach"
    assert decision["prompt"] == "Which auth approach?"
    assert decision["options"] == ["oauth", "api-key"]
    assert decision["status"] == "pending"
    assert decision["selected"] is None
    assert decision["rationale"] is None
    assert decision["evidence_ref"] is None
    md = md_path.read_text()
    assert "## Decision checkpoints" in md
    assert "**auth-approach** [pending]" in md

    assert (
        work_cmd.task_plan(
            target=tmp_path,
            task_id=task_id[:12],
            write=True,
            accept=True,
        )
        == 2
    )
    err = capsys.readouterr().err
    assert "unresolved plan decision checkpoint" in err
    assert "auth-approach" in err

    assert work_cmd.task_claim(target=tmp_path, task_id=task_id[:12], actor="ops") == 2
    err = capsys.readouterr().err
    assert "unresolved plan decision checkpoint" in err

    assert work_cmd.task_done(target=tmp_path, task_id=task_id[:12]) == 2
    err = capsys.readouterr().err
    assert "unresolved plan decision checkpoint" in err

    assert (
        work_cmd.task_plan(
            target=tmp_path,
            task_id=task_id[:12],
            write=True,
            resolve_decision="auth-approach",
            selected="oauth",
            rationale="Matches existing SSO providers",
            evidence_ref=".brigade/work/verify-runs/demo/receipt.json",
        )
        == 0
    )
    capsys.readouterr()
    receipt = json.loads(json_path.read_text())
    decision = receipt["decisions"][0]
    assert decision["status"] == "resolved"
    assert decision["selected"] == "oauth"
    assert decision["rationale"] == "Matches existing SSO providers"
    assert decision["evidence_ref"] == ".brigade/work/verify-runs/demo/receipt.json"
    assert decision["resolved_at"]
    md = md_path.read_text()
    assert "**auth-approach** [resolved]" in md
    assert "selected: oauth" in md

    assert (
        work_cmd.task_plan(
            target=tmp_path,
            task_id=task_id[:12],
            write=True,
            accept=True,
        )
        == 0
    )
    capsys.readouterr()
    receipt = json.loads(json_path.read_text())
    assert receipt["status"] == "accepted"

    assert work_cmd.task_claim(target=tmp_path, task_id=task_id[:12], actor="ops") == 0
    out = capsys.readouterr().out
    assert "status: in_progress" in out

    assert work_cmd.task_plan(target=tmp_path, task_id=task_id[:12], json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["unresolved_decision_count"] == 0
    assert payload["decisions"][0]["status"] == "resolved"


def test_plan_decision_resolve_requires_all_fields_and_valid_option(tmp_path, capsys):
    _init_git_repo(tmp_path)
    task_id = _plan_task_id(tmp_path, capsys)
    assert (
        work_cmd.task_plan(
            target=tmp_path,
            task_id=task_id[:12],
            write=True,
            decision="route",
            decision_prompt="Pick a route",
            decision_options=["direct", "proxy"],
        )
        == 0
    )
    capsys.readouterr()

    assert (
        work_cmd.task_plan(
            target=tmp_path,
            task_id=task_id[:12],
            write=True,
            resolve_decision="route",
            selected="direct",
            rationale="faster",
        )
        == 2
    )
    assert "--evidence-ref" in capsys.readouterr().err

    assert (
        work_cmd.task_plan(
            target=tmp_path,
            task_id=task_id[:12],
            write=True,
            resolve_decision="route",
            selected="sidecar",
            rationale="not listed",
            evidence_ref="notes/decision.md",
        )
        == 2
    )
    assert "not in decision options" in capsys.readouterr().err

    assert (
        work_cmd.task_plan(
            target=tmp_path,
            task_id=task_id[:12],
            write=True,
            decision="brand-new",
        )
        == 2
    )
    assert "decision prompt is required" in capsys.readouterr().err


def test_work_claim_blocks_on_unresolved_plan_decision(tmp_path, capsys):
    _init_git_repo(tmp_path)
    task_id = _plan_task_id(tmp_path, capsys)
    assert (
        work_cmd.task_plan(
            target=tmp_path,
            task_id=task_id[:12],
            write=True,
            decision="store",
            decision_prompt="Which store?",
            decision_options=["sqlite", "postgres"],
        )
        == 0
    )
    capsys.readouterr()

    assert work_cmd.claim(target=tmp_path, task_id=task_id[:12], actor="ops") == 2
    err = capsys.readouterr().err
    assert "unresolved plan decision checkpoint" in err

    assert (
        work_cmd.task_plan(
            target=tmp_path,
            task_id=task_id[:12],
            write=True,
            resolve_decision="store",
            selected="sqlite",
            rationale="local-first",
            evidence_ref=".brigade/work/plans/evidence.md",
        )
        == 0
    )
    capsys.readouterr()
    assert work_cmd.claim(target=tmp_path, task_id=task_id[:12], actor="ops") == 0
    out = capsys.readouterr().out
    assert "claim_id:" in out


def test_plan_decision_absent_on_legacy_receipt_remains_valid(tmp_path, capsys):
    _init_git_repo(tmp_path)
    task_id = _plan_task_id(tmp_path, capsys)
    assert work_cmd.task_plan(target=tmp_path, task_id=task_id[:12], write=True) == 0
    capsys.readouterr()
    json_path, _ = work_cmd._plan_paths(tmp_path, task_id)
    receipt = json.loads(json_path.read_text())
    receipt.pop("decisions", None)
    json_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")

    assert work_cmd.task_plan(target=tmp_path, task_id=task_id[:12], write=True, accept=True) == 0
    capsys.readouterr()
    assert work_cmd.task_claim(target=tmp_path, task_id=task_id[:12], actor="ops") == 0
    out = capsys.readouterr().out
    assert "status: in_progress" in out


def test_plan_decision_malformed_non_list_fails_closed_on_accept_claim_done(tmp_path, capsys):
    _init_git_repo(tmp_path)
    task_id = _plan_task_id(tmp_path, capsys)
    assert work_cmd.task_plan(target=tmp_path, task_id=task_id[:12], write=True) == 0
    capsys.readouterr()
    json_path, _ = work_cmd._plan_paths(tmp_path, task_id)
    receipt = json.loads(json_path.read_text())
    receipt["decisions"] = {"id": "auth-approach"}
    json_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")

    assert work_cmd.task_plan(target=tmp_path, task_id=task_id[:12], write=True, accept=True) == 2
    err = capsys.readouterr().err
    assert "decisions must be a list" in err

    assert work_cmd.task_claim(target=tmp_path, task_id=task_id[:12], actor="ops") == 2
    err = capsys.readouterr().err
    assert "decisions must be a list" in err

    assert work_cmd.claim(target=tmp_path, task_id=task_id[:12], actor="ops") == 2
    err = capsys.readouterr().err
    assert "decisions must be a list" in err

    assert work_cmd.task_done(target=tmp_path, task_id=task_id[:12]) == 2
    err = capsys.readouterr().err
    assert "decisions must be a list" in err


def test_plan_decision_malformed_field_types_fail_closed_on_accept_claim_done(tmp_path, capsys):
    _init_git_repo(tmp_path)
    task_id = _plan_task_id(tmp_path, capsys)
    assert work_cmd.task_plan(target=tmp_path, task_id=task_id[:12], write=True) == 0
    capsys.readouterr()
    json_path, _ = work_cmd._plan_paths(tmp_path, task_id)
    receipt = json.loads(json_path.read_text())

    # Non-list options must fail closed and must not satisfy resolved gate via typed fields.
    receipt["decisions"] = [
        {
            "id": "auth-approach",
            "prompt": "Which auth?",
            "options": {"oauth": True},
            "selected": "oauth",
            "rationale": "Matches SSO",
            "evidence_ref": "miseledger:bundle/demo",
        }
    ]
    json_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")

    assert work_cmd.task_plan(target=tmp_path, task_id=task_id[:12], write=True, accept=True) == 2
    err = capsys.readouterr().err
    assert "field 'options' must be a non-empty list of non-empty strings" in err

    assert work_cmd.task_claim(target=tmp_path, task_id=task_id[:12], actor="ops") == 2
    assert "field 'options' must be a non-empty list of non-empty strings" in capsys.readouterr().err

    assert work_cmd.claim(target=tmp_path, task_id=task_id[:12], actor="ops") == 2
    assert "field 'options' must be a non-empty list of non-empty strings" in capsys.readouterr().err

    assert work_cmd.task_done(target=tmp_path, task_id=task_id[:12]) == 2
    assert "field 'options' must be a non-empty list of non-empty strings" in capsys.readouterr().err

    # Representative corruption on other recognized fields (non-string selected).
    receipt["decisions"] = [
        {
            "id": "auth-approach",
            "prompt": "Which auth?",
            "options": ["oauth", "api-key"],
            "selected": 1,
            "rationale": "Matches SSO",
            "evidence_ref": "miseledger:bundle/demo",
        }
    ]
    json_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")

    assert work_cmd.task_plan(target=tmp_path, task_id=task_id[:12], write=True, accept=True) == 2
    assert "field 'selected' must be a string or null" in capsys.readouterr().err


def _assert_plan_decision_options_gate_fails_closed(tmp_path, capsys, *, task_id, options_value):
    json_path, _ = work_cmd._plan_paths(tmp_path, task_id)
    receipt = json.loads(json_path.read_text())
    receipt["decisions"] = [
        {
            "id": "auth-approach",
            "prompt": "Which auth?",
            "options": options_value,
            "selected": "oauth",
            "rationale": "Matches SSO",
            "evidence_ref": "miseledger:bundle/demo",
        }
    ]
    json_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")

    assert work_cmd.task_plan(target=tmp_path, task_id=task_id[:12], write=True, accept=True) == 2
    err = capsys.readouterr().err
    assert "field 'options' must be a non-empty list of non-empty strings" in err

    assert work_cmd.task_claim(target=tmp_path, task_id=task_id[:12], actor="ops") == 2
    assert "field 'options' must be a non-empty list of non-empty strings" in capsys.readouterr().err

    assert work_cmd.claim(target=tmp_path, task_id=task_id[:12], actor="ops") == 2
    assert "field 'options' must be a non-empty list of non-empty strings" in capsys.readouterr().err

    assert work_cmd.task_done(target=tmp_path, task_id=task_id[:12]) == 2
    assert "field 'options' must be a non-empty list of non-empty strings" in capsys.readouterr().err


@pytest.mark.parametrize("options_value", [None, []])
def test_plan_decision_null_or_empty_options_fail_closed_on_accept_claim_done(tmp_path, capsys, options_value):
    _init_git_repo(tmp_path)
    task_id = _plan_task_id(tmp_path, capsys)
    assert work_cmd.task_plan(target=tmp_path, task_id=task_id[:12], write=True) == 0
    capsys.readouterr()
    _assert_plan_decision_options_gate_fails_closed(
        tmp_path,
        capsys,
        task_id=task_id,
        options_value=options_value,
    )


def test_plan_decision_missing_options_key_fail_closed_on_accept_claim_done(tmp_path, capsys):
    _init_git_repo(tmp_path)
    task_id = _plan_task_id(tmp_path, capsys)
    assert work_cmd.task_plan(target=tmp_path, task_id=task_id[:12], write=True) == 0
    capsys.readouterr()
    json_path, _ = work_cmd._plan_paths(tmp_path, task_id)
    receipt = json.loads(json_path.read_text())
    receipt["decisions"] = [
        {
            "id": "auth-approach",
            "prompt": "Which auth?",
            "selected": "oauth",
            "rationale": "Matches SSO",
            "evidence_ref": "miseledger:bundle/demo",
        }
    ]
    json_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")

    assert work_cmd.task_plan(target=tmp_path, task_id=task_id[:12], write=True, accept=True) == 2
    err = capsys.readouterr().err
    assert "field 'options' must be a non-empty list of non-empty strings" in err

    assert work_cmd.task_claim(target=tmp_path, task_id=task_id[:12], actor="ops") == 2
    assert "field 'options' must be a non-empty list of non-empty strings" in capsys.readouterr().err

    assert work_cmd.claim(target=tmp_path, task_id=task_id[:12], actor="ops") == 2
    assert "field 'options' must be a non-empty list of non-empty strings" in capsys.readouterr().err

    assert work_cmd.task_done(target=tmp_path, task_id=task_id[:12]) == 2
    assert "field 'options' must be a non-empty list of non-empty strings" in capsys.readouterr().err


@pytest.mark.parametrize("legacy_mode", ["absent", "null"])
def test_plan_decision_absent_or_null_decisions_field_remains_valid_legacy(tmp_path, capsys, legacy_mode):
    _init_git_repo(tmp_path)
    task_id = _plan_task_id(tmp_path, capsys)
    assert work_cmd.task_plan(target=tmp_path, task_id=task_id[:12], write=True) == 0
    capsys.readouterr()
    json_path, _ = work_cmd._plan_paths(tmp_path, task_id)
    receipt = json.loads(json_path.read_text())
    if legacy_mode == "absent":
        receipt.pop("decisions", None)
    else:
        receipt["decisions"] = None
    json_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")

    assert work_cmd.task_plan(target=tmp_path, task_id=task_id[:12], write=True, accept=True) == 0
    capsys.readouterr()
    assert work_cmd.task_claim(target=tmp_path, task_id=task_id[:12], actor="ops") == 0
    out = capsys.readouterr().out
    assert "status: in_progress" in out


def test_plan_decision_malformed_entry_fails_closed_and_keeps_opaque_evidence_ref(tmp_path, capsys):
    _init_git_repo(tmp_path)
    task_id = _plan_task_id(tmp_path, capsys)
    assert (
        work_cmd.task_plan(
            target=tmp_path,
            task_id=task_id[:12],
            write=True,
            decision="store",
            decision_prompt="Which store?",
            decision_options=["sqlite", "postgres"],
        )
        == 0
    )
    capsys.readouterr()
    json_path, _ = work_cmd._plan_paths(tmp_path, task_id)
    receipt = json.loads(json_path.read_text())
    receipt["decisions"] = [{"prompt": "missing id", "options": ["a"]}]
    json_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")

    assert work_cmd.task_plan(target=tmp_path, task_id=task_id[:12], write=True, accept=True) == 2
    err = capsys.readouterr().err
    assert "invalid or missing id" in err
    assert work_cmd.task_claim(target=tmp_path, task_id=task_id[:12], actor="ops") == 2
    assert "invalid or missing id" in capsys.readouterr().err
    assert work_cmd.task_done(target=tmp_path, task_id=task_id[:12]) == 2
    assert "invalid or missing id" in capsys.readouterr().err

    # Restore a valid pending checkpoint, then resolve with an opaque external id.
    receipt["decisions"] = [
        {
            "id": "store",
            "prompt": "Which store?",
            "options": ["sqlite", "postgres"],
            "selected": None,
            "rationale": None,
            "evidence_ref": None,
            "status": "pending",
            "created_at": "2026-08-10T00:00:00+00:00",
            "resolved_at": None,
        }
    ]
    json_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    opaque_ref = "miseledger:bundle/demo-evidence-id"
    assert (
        work_cmd.task_plan(
            target=tmp_path,
            task_id=task_id[:12],
            write=True,
            resolve_decision="store",
            selected="sqlite",
            rationale="local-first",
            evidence_ref=opaque_ref,
        )
        == 0
    )
    capsys.readouterr()
    saved = json.loads(json_path.read_text())
    assert saved["decisions"][0]["evidence_ref"] == opaque_ref
    assert not (tmp_path / opaque_ref).exists()
    assert work_cmd.task_plan(target=tmp_path, task_id=task_id[:12], write=True, accept=True) == 0
    capsys.readouterr()


def test_extract_issue_acceptance_from_sections_and_checkboxes():
    body = """
## Context
- This is background, not acceptance.

## Acceptance Criteria
- CLI imports the first criterion.
1. Numbered criteria are supported.

## Notes
- This should not be imported.
- [ ] Checkboxes are imported wherever they appear.

Testing:
* Focused tests pass.
"""

    assert work_cmd._extract_issue_acceptance(body) == [
        "CLI imports the first criterion.",
        "Numbered criteria are supported.",
        "Checkboxes are imported wherever they appear.",
        "Focused tests pass.",
    ]


def test_extract_issue_acceptance_returns_empty_for_missing_body():
    assert work_cmd._extract_issue_acceptance(None) == []
    assert work_cmd._extract_issue_acceptance("") == []


def test_work_task_add_from_issue_preserves_github_metadata(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd.helpers,
        "_now",
        lambda: datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(work_cmd.helpers.shutil, "which", lambda name: "/usr/bin/gh" if name == "gh" else None)

    def fake_run(args, **kwargs):
        assert args[:3] == ["gh", "issue", "view"]
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=json.dumps(
                {
                    "url": "https://github.com/acme/widgets/issues/42",
                    "number": 42,
                    "title": "Import issue backed task",
                    "labels": [{"name": "bug"}, {"name": "tdd"}],
                    "state": "OPEN",
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(work_cmd.helpers.subprocess, "run", fake_run)

    assert work_cmd.task_add(target=tmp_path, from_issue="42", template="red-green-refactor") == 0
    out = capsys.readouterr().out
    assert "issue: https://github.com/acme/widgets/issues/42" in out
    ledger = json.loads((tmp_path / ".brigade" / "work" / "tasks.json").read_text())
    task = ledger["tasks"][0]
    assert task["text"] == "Import issue backed task"
    assert task["source"] == "github_issue"
    assert task["metadata"]["github_issue"] == {
        "url": "https://github.com/acme/widgets/issues/42",
        "number": 42,
        "title": "Import issue backed task",
        "labels": ["bug", "tdd"],
        "state": "OPEN",
        "source": "gh",
        "ref": "42",
    }


def test_work_task_add_from_issue_fails_without_partial_task(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(work_cmd.helpers.shutil, "which", lambda name: None)

    assert work_cmd.task_add(target=tmp_path, from_issue="42") == 1
    assert "gh CLI is not available" in capsys.readouterr().err
    assert not (tmp_path / ".brigade" / "work" / "tasks.json").exists()


def test_work_task_add_from_issue_rejects_malformed_gh_output_without_partial_task(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(work_cmd.helpers.shutil, "which", lambda name: "/usr/bin/gh" if name == "gh" else None)

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout="{bad json", stderr="")

    monkeypatch.setattr(work_cmd.helpers.subprocess, "run", fake_run)

    assert work_cmd.task_add(target=tmp_path, from_issue="42") == 1
    assert "returned invalid JSON" in capsys.readouterr().err
    assert not (tmp_path / ".brigade" / "work" / "tasks.json").exists()


def test_work_brief_includes_pending_tasks(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd.helpers,
        "_now",
        lambda: datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
    )
    assert work_cmd.task_add(target=tmp_path, text="Build queued task") == 0
    capsys.readouterr()

    assert work_cmd.brief(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["next_source"] == "task_ledger"
    assert payload["next"] == "Build queued task"
    assert payload["pending_tasks"][0]["text"] == "Build queued task"
    assert payload["suggested_command"] == "brigade work run"
    assert payload["next_task"]["acceptance_missing"] is True
    assert payload["next_task"]["acceptance_count"] == 0


def test_work_brief_reports_next_task_acceptance(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd.helpers,
        "_now",
        lambda: datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
    )
    assert (
        work_cmd.task_add(
            target=tmp_path,
            text="Build accepted task",
            task_type="workflow",
            priority="urgent",
            acceptance=["Brief reports acceptance"],
        )
        == 0
    )
    capsys.readouterr()

    assert work_cmd.brief(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "next_type: workflow" in out
    assert "next_priority: urgent" in out
    assert "next_acceptance: 1" in out
    assert "[workflow urgent acceptance=1] Build accepted task" in out


def test_work_brief_surfaces_issue_backed_next_task_context(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd.helpers,
        "_now",
        lambda: datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(work_cmd.helpers.shutil, "which", lambda name: "/usr/bin/gh" if name == "gh" else None)

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=json.dumps(
                {
                    "url": "https://github.com/acme/widgets/issues/7",
                    "number": 7,
                    "title": "Surface issue context",
                    "labels": [{"name": "docs"}],
                    "state": "OPEN",
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(work_cmd.helpers.subprocess, "run", fake_run)
    assert work_cmd.task_add(target=tmp_path, from_issue="7") == 0
    capsys.readouterr()

    assert work_cmd.brief(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["next_issue"]["url"] == "https://github.com/acme/widgets/issues/7"
    assert payload["next_issue"]["labels"] == ["docs"]

    assert work_cmd.brief(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "issue: https://github.com/acme/widgets/issues/7" in out
    assert "issue_state: OPEN" in out
    assert "issue_labels: docs" in out


def test_work_tasks_cli(tmp_path, monkeypatch):
    seen = []

    def fake_tasks(**kwargs):
        seen.append(("tasks", kwargs))
        return 0

    def fake_task_add(**kwargs):
        seen.append(("add", kwargs))
        return 0

    def fake_task_show(**kwargs):
        seen.append(("show", kwargs))
        return 0

    def fake_task_plan(**kwargs):
        seen.append(("plan", kwargs))
        return 0

    def fake_task_done(**kwargs):
        seen.append(("done", kwargs))
        return 0

    monkeypatch.setattr(work_cmd, "tasks", fake_tasks)
    monkeypatch.setattr(work_cmd, "task_add", fake_task_add)
    monkeypatch.setattr(work_cmd, "task_show", fake_task_show)
    monkeypatch.setattr(work_cmd, "task_plan", fake_task_plan)
    monkeypatch.setattr(work_cmd, "task_done", fake_task_done)

    assert cli.main(["work", "tasks", "--target", str(tmp_path), "--all", "--json"]) == 0
    assert (
        cli.main(
            [
                "work",
                "task",
                "add",
                "build",
                "queue",
                "--target",
                str(tmp_path),
                "--type",
                "feature",
                "--priority",
                "high",
                "--acceptance",
                "passes",
                "--template",
                "vertical-slice",
            ]
        )
        == 0
    )
    assert cli.main(["work", "task", "add", "--target", str(tmp_path), "--from-next"]) == 0
    assert cli.main(["work", "task", "show", "abc123", "--target", str(tmp_path)]) == 0
    assert cli.main(["work", "task", "plan", "abc123", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["work", "task", "done", "abc123", "--target", str(tmp_path)]) == 0
    assert seen == [
        ("tasks", {"target": tmp_path, "all_tasks": True, "json_output": True}),
        (
            "add",
            {
                "target": tmp_path,
                "text": "build queue",
                "from_next": False,
                "from_issue": None,
                "task_type": "feature",
                "priority": "high",
                "acceptance": ["passes"],
                "template": "vertical-slice",
                "deps": [],
                "symbols": None,
                "files": None,
                "seat_class": None,
                "spend_by": None,
                "graph": None,
                "dry_run": False,
                "json_output": False,
            },
        ),
        (
            "add",
            {
                "target": tmp_path,
                "text": None,
                "from_next": True,
                "from_issue": None,
                "task_type": "task",
                "priority": "normal",
                "acceptance": [],
                "template": None,
                "deps": [],
                "symbols": None,
                "files": None,
                "seat_class": None,
                "spend_by": None,
                "graph": None,
                "dry_run": False,
                "json_output": False,
            },
        ),
        ("show", {"target": tmp_path, "task_id": "abc123", "json_output": False}),
        (
            "plan",
            {
                "target": tmp_path,
                "task_id": "abc123",
                "json_output": True,
                "write": False,
                "title": None,
                "assumptions": [],
                "risks": [],
                "sources": [],
                "next_command": None,
                "accept": False,
                "kind": "plan",
                "steps": [],
                "from_research": None,
                "decision": None,
                "decision_prompt": None,
                "decision_options": [],
                "resolve_decision": None,
                "selected": None,
                "rationale": None,
                "evidence_ref": None,
            },
        ),
        ("done", {"target": tmp_path, "task_id": "abc123", "force": False, "json_output": False}),
    ]
