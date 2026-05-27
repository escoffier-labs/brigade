from __future__ import annotations

import json
import os

from brigade import cli
from brigade import handoff_cmd
from brigade import work_cmd


CARD_HANDOFF = """# Memory Handoff

## Type
learning

## Title
Handoff lint cards

## Summary
Card handoffs should be promotable.

## Recommended memory action
create-card

## Target card
handoff-lint-cards.md

## Suggested card content
---
topic: handoff-lint-cards
category: foundation
tags: [memory, handoff]
---

# Handoff lint cards

Card handoffs only include card fields.
"""


NO_CARD_HANDOFF = """# Memory Handoff

## Type
learning

## Title
Handoff lint documents

## Summary
Document handoffs should be routable.

## Recommended memory action
no-card

## Target document
.learnings/LEARNINGS.md

## Suggested document content
### Handoff lint documents

Document handoffs only include document fields.
"""


def test_handoff_lint_accepts_card_handoff_without_document_sections(tmp_path, capsys):
    path = tmp_path / "note.md"
    path.write_text(CARD_HANDOFF)

    assert handoff_cmd.lint(target=tmp_path, paths=[path]) == 0

    out = capsys.readouterr().out
    assert "[ok]" in out
    assert "(create-card)" in out


def test_handoff_lint_allows_level_three_card_headings_without_warning(tmp_path, capsys):
    path = tmp_path / "note.md"
    path.write_text(CARD_HANDOFF + "\n### Details\n\nMore durable context.\n")

    assert handoff_cmd.lint(target=tmp_path, paths=[path]) == 0

    out = capsys.readouterr().out
    assert "warning:" not in out


def test_handoff_lint_rejects_card_handoff_with_empty_document_sections(tmp_path, capsys):
    path = tmp_path / "note.md"
    path.write_text(CARD_HANDOFF + "\n## Target document\n\n## Suggested document content\n")

    assert handoff_cmd.lint(target=tmp_path, paths=[path]) == 1

    out = capsys.readouterr().out
    assert "[fail]" in out
    assert "card handoffs must omit the Target document section entirely" in out
    assert "card handoffs must omit the Suggested document content section entirely" in out


def test_handoff_lint_accepts_no_card_handoff_without_card_sections(tmp_path, capsys):
    path = tmp_path / "note.md"
    path.write_text(NO_CARD_HANDOFF)

    assert handoff_cmd.lint(target=tmp_path, paths=[path]) == 0

    out = capsys.readouterr().out
    assert "[ok]" in out
    assert "(no-card)" in out


def test_handoff_lint_rejects_no_card_handoff_with_card_sections(tmp_path, capsys):
    path = tmp_path / "note.md"
    path.write_text(NO_CARD_HANDOFF + "\n## Target card\nextra.md\n\n## Suggested card content\n---\n")

    assert handoff_cmd.lint(target=tmp_path, paths=[path]) == 1

    out = capsys.readouterr().out
    assert "[fail]" in out
    assert "no-card handoffs must omit the Target card section entirely" in out
    assert "no-card handoffs must omit the Suggested card content section entirely" in out


def test_handoff_lint_defaults_to_pending_inboxes(tmp_path, capsys):
    inbox = tmp_path / ".codex" / "memory-handoffs"
    inbox.mkdir(parents=True)
    (inbox / "TEMPLATE.md").write_text("# template\n")
    (inbox / "2026-05-27-note.md").write_text(CARD_HANDOFF)

    assert handoff_cmd.lint(target=tmp_path) == 0

    out = capsys.readouterr().out
    assert "files: 1" in out
    assert "2026-05-27-note.md" in out
    assert "TEMPLATE.md" not in out


def test_handoff_doctor_warns_for_pending_handoff_without_source_config(tmp_path, capsys):
    inbox = tmp_path / ".claude" / "memory-handoffs"
    inbox.mkdir(parents=True)
    (inbox / "TEMPLATE.md").write_text("# Template\n")
    (inbox / "2026-05-27-note.md").write_text("# Memory Handoff\n")

    assert handoff_cmd.doctor(target=tmp_path) == 0

    out = capsys.readouterr().out
    assert "handoff doctor:" in out
    assert "[warn] handoff_sources: not configured" in out
    assert ".claude/memory-handoffs" in out
    assert "pending=1" in out
    assert "watched=no" in out


def test_handoff_doctor_accepts_configured_claude_and_codex_inboxes(tmp_path, capsys):
    for rel in (".claude/memory-handoffs", ".codex/memory-handoffs"):
        inbox = tmp_path / rel
        inbox.mkdir(parents=True)
        (inbox / "2026-05-27-note.md").write_text(CARD_HANDOFF)
    config = tmp_path / ".brigade" / "handoff-sources.json"
    config.parent.mkdir()
    config.write_text(
        json.dumps(
            {
                "canonical_owner": "openclaw",
                "sources": [
                    {
                        "root": ".",
                        "inboxes": [
                            ".claude/memory-handoffs",
                            ".codex/memory-handoffs",
                        ],
                    }
                ],
            }
        )
    )

    assert handoff_cmd.doctor(target=tmp_path) == 0

    out = capsys.readouterr().out
    assert "[ok] handoff_sources:" in out
    assert "pending=1" in out
    assert "watched=yes" in out
    assert "handoff_warning" not in out


def test_handoff_doctor_warns_for_pending_lint_error(tmp_path, capsys):
    inbox = tmp_path / ".claude" / "memory-handoffs"
    inbox.mkdir(parents=True)
    (inbox / "bad.md").write_text(CARD_HANDOFF + "\n## Target document\n\n## Suggested document content\n")

    assert handoff_cmd.doctor(target=tmp_path) == 0

    out = capsys.readouterr().out
    assert "[warn] handoff_lint:" in out
    assert "card handoffs must omit the Target document section entirely" in out


def test_handoff_issues_include_pending_lint_errors(tmp_path, capsys):
    inbox = tmp_path / ".claude" / "memory-handoffs"
    inbox.mkdir(parents=True)
    (inbox / "bad.md").write_text(CARD_HANDOFF + "\n## Target document\n\n## Suggested document content\n")

    assert handoff_cmd.issues(target=tmp_path, categories=["lint"], json_output=True) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["by_category"] == {"lint": 1}
    issue = payload["issues"][0]
    assert "Fix pending handoff lint error in bad.md" in issue["text"]
    assert "Delete Target document and Suggested document content sections entirely" in issue["repair"]


def test_handoff_doctor_fails_invalid_source_config(tmp_path, capsys):
    config = tmp_path / ".brigade" / "handoff-sources.json"
    config.parent.mkdir()
    config.write_text("{broken")

    assert handoff_cmd.doctor(target=tmp_path) == 1

    out = capsys.readouterr().out
    assert "[fail] handoff_sources:" in out
    assert "invalid JSON" in out


def test_handoff_doctor_json_output(tmp_path, capsys):
    inbox = tmp_path / ".codex" / "memory-handoffs"
    inbox.mkdir(parents=True)
    (inbox / "2026-05-27-note.md").write_text("# Memory Handoff\n")

    assert handoff_cmd.doctor(target=tmp_path, json_output=True) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["sources_loaded"] is False
    assert payload["inboxes"][1]["pending"] == 1
    assert payload["ingestor"]["configured"] is False


def test_handoff_doctor_warns_for_ingestor_warning_log(tmp_path, capsys):
    log = tmp_path / ".brigade" / "handoff-ingest" / "latest.log"
    log.parent.mkdir(parents=True)
    log.write_text(
        "\n".join(
            [
                "=== handoff-dir: .claude/memory-handoffs ===",
                "SKIP bad-note.md: no recognizable markdown sections found",
                "Warnings: 1",
                "NO_REPLY",
                "",
            ]
        )
    )
    config = tmp_path / ".brigade" / "handoff-sources.json"
    config.write_text(
        json.dumps(
            {
                "sources": [{"root": ".", "inboxes": [".claude/memory-handoffs"]}],
                "ingestor": {"last_run_log": ".brigade/handoff-ingest/latest.log"},
            }
        )
    )

    assert handoff_cmd.doctor(target=tmp_path) == 0

    out = capsys.readouterr().out
    assert "[warn] handoff_ingestor:" in out
    assert "SKIP bad-note.md" in out
    assert "hidden behind NO_REPLY" in out


def test_handoff_doctor_warns_for_stale_ingestor_log(tmp_path, capsys):
    log = tmp_path / "latest.log"
    log.write_text("Processed 0 handoff(s)\n")
    os.utime(log, (1, 1))
    config = tmp_path / ".brigade" / "handoff-sources.json"
    config.parent.mkdir()
    config.write_text(
        json.dumps(
            {
                "sources": [{"root": ".", "inboxes": [".claude/memory-handoffs"]}],
                "ingestor": {"last_run_log": "latest.log", "stale_after_minutes": 1},
            }
        )
    )

    assert handoff_cmd.doctor(target=tmp_path) == 0

    out = capsys.readouterr().out
    assert "[warn] handoff_ingestor:" in out
    assert "handoff ingestor log is stale" in out


def test_handoff_issues_groups_ingestor_log_warnings(tmp_path, capsys):
    log = tmp_path / ".brigade" / "handoff-ingest" / "latest.log"
    log.parent.mkdir(parents=True)
    log.write_text(
        "\n".join(
            [
                "SKIP /repo/.claude/memory-handoffs/bad.md: no recognizable markdown sections found",
                "PROMOTE-SKIP card.md: target card does not exist for update",
                "ROUTE-SKIP doc.md: action is not no-card",
                "Warnings: 3",
                "NO_REPLY",
                "",
            ]
        )
    )
    config = tmp_path / ".brigade" / "handoff-sources.json"
    config.write_text(
        json.dumps(
            {
                "sources": [{"root": ".", "inboxes": [".claude/memory-handoffs"]}],
                "ingestor": {"last_run_log": ".brigade/handoff-ingest/latest.log"},
            }
        )
    )

    assert handoff_cmd.issues(target=tmp_path) == 0

    out = capsys.readouterr().out
    assert "handoff issues:" in out
    assert "- skip: 1" in out
    assert "- promote-skip: 1" in out
    assert "- route-skip: 1" in out
    assert "- warning-summary: 1" in out
    assert "- hidden-warning: 1" in out
    assert "Rewrite the handoff with the standard markdown sections" in out
    assert "Use Recommended memory action no-card" in out


def test_handoff_issues_json_reports_repair_guidance(tmp_path, capsys):
    log = tmp_path / "latest.log"
    log.write_text("PROMOTE-SKIP note.md: target card does not exist for update\n")
    config = tmp_path / ".brigade" / "handoff-sources.json"
    config.parent.mkdir()
    config.write_text(
        json.dumps(
            {
                "sources": [{"root": ".", "inboxes": [".claude/memory-handoffs"]}],
                "ingestor": {"last_run_log": "latest.log"},
            }
        )
    )

    assert handoff_cmd.issues(target=tmp_path, json_output=True) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 1
    assert payload["by_category"] == {"promote-skip": 1}
    assert payload["issues"][0]["kind"] == "task"
    assert "create-card" in payload["issues"][0]["repair"]


def test_handoff_import_issues_appends_work_imports(tmp_path, capsys):
    log = tmp_path / "latest.log"
    log.write_text("ROUTE-SKIP note.md: action is not no-card\n")
    config = tmp_path / ".brigade" / "handoff-sources.json"
    config.parent.mkdir()
    config.write_text(
        json.dumps(
            {
                "sources": [{"root": ".", "inboxes": [".claude/memory-handoffs"]}],
                "ingestor": {"last_run_log": "latest.log"},
            }
        )
    )

    assert handoff_cmd.import_issues(target=tmp_path) == 0

    out = capsys.readouterr().out
    assert "handoff issue imports:" in out
    assert "imported: 1" in out
    stored = (tmp_path / ".brigade" / "work" / "imports" / "inbox.jsonl").read_text()
    item = json.loads(stored.splitlines()[0])
    assert item["source"] == "handoff-ingest"
    assert item["kind"] == "task"
    assert item["metadata"]["handoff_issue_category"] == "route-skip"
    assert "no-card" in item["metadata"]["repair"]


def test_handoff_import_issues_dedupes_existing_pending_imports(tmp_path, capsys):
    log = tmp_path / "latest.log"
    log.write_text("SKIP bad.md: no recognizable markdown sections found\n")
    config = tmp_path / ".brigade" / "handoff-sources.json"
    config.parent.mkdir()
    config.write_text(
        json.dumps(
            {
                "sources": [{"root": ".", "inboxes": [".claude/memory-handoffs"]}],
                "ingestor": {"last_run_log": "latest.log"},
            }
        )
    )

    assert handoff_cmd.import_issues(target=tmp_path) == 0
    capsys.readouterr()
    assert handoff_cmd.import_issues(target=tmp_path) == 0

    out = capsys.readouterr().out
    assert "imported: 0" in out
    assert "skipped_duplicates: 1" in out


def test_handoff_import_issues_filters_by_category(tmp_path, capsys):
    log = tmp_path / "latest.log"
    log.write_text(
        "\n".join(
            [
                "SKIP bad.md: no recognizable markdown sections found",
                "ROUTE-SKIP note.md: action is not no-card",
                "",
            ]
        )
    )
    config = tmp_path / ".brigade" / "handoff-sources.json"
    config.parent.mkdir()
    config.write_text(
        json.dumps(
            {
                "sources": [{"root": ".", "inboxes": [".claude/memory-handoffs"]}],
                "ingestor": {"last_run_log": "latest.log"},
            }
        )
    )

    assert handoff_cmd.import_issues(target=tmp_path, categories=["route-skip"]) == 0

    out = capsys.readouterr().out
    assert "issues: 1" in out
    assert "imported: 1" in out
    item = json.loads((tmp_path / ".brigade" / "work" / "imports" / "inbox.jsonl").read_text().splitlines()[0])
    assert item["metadata"]["handoff_issue_category"] == "route-skip"


def test_handoff_sync_issues_imports_new_and_closes_stale_local_work(tmp_path, capsys):
    log = tmp_path / "latest.log"
    log.write_text("SKIP current.md: no recognizable markdown sections found\n")
    config = tmp_path / ".brigade" / "handoff-sources.json"
    config.parent.mkdir()
    config.write_text(
        json.dumps(
            {
                "sources": [{"root": ".", "inboxes": [".claude/memory-handoffs"]}],
                "ingestor": {"last_run_log": "latest.log"},
            }
        )
    )
    stale_metadata = {
        "handoff_issue_id": "handoff-route-skip-old",
        "handoff_issue_category": "route-skip",
    }
    stale_import = work_cmd._make_import(
        "Fix old route skip",
        kind="task",
        source="handoff-ingest",
        metadata=stale_metadata,
    )
    work_cmd._write_imports(tmp_path, [stale_import])
    stale_task, _ = work_cmd._add_task(
        tmp_path,
        "Fix old route skip",
        source="import:handoff-ingest",
        metadata=stale_metadata,
    )

    assert handoff_cmd.sync_issues(target=tmp_path, json_output=True) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["issues"] == 1
    assert payload["imported"] == 1
    assert payload["stale_imports_closed"] == 1
    assert payload["stale_tasks_closed"] == 1
    imports = work_cmd._read_imports(tmp_path)
    assert imports[0]["status"] == "dismissed"
    assert imports[0]["dismiss_reason"] == "resolved or absent from latest handoff issue scan"
    assert imports[1]["status"] == "pending"
    assert imports[1]["metadata"]["handoff_issue_category"] == "skip"
    task, _ = work_cmd._find_task(tmp_path, stale_task["id"])
    assert task is not None
    assert task["status"] == "done"
    assert task["completion_reason"] == "resolved or absent from latest handoff issue scan"


def test_handoff_sync_issues_does_not_reimport_dismissed_known_issue(tmp_path, capsys):
    log = tmp_path / "latest.log"
    log.write_text("SKIP current.md: no recognizable markdown sections found\n")
    config = tmp_path / ".brigade" / "handoff-sources.json"
    config.parent.mkdir()
    config.write_text(
        json.dumps(
            {
                "sources": [{"root": ".", "inboxes": [".claude/memory-handoffs"]}],
                "ingestor": {"last_run_log": "latest.log"},
            }
        )
    )
    issue = handoff_cmd.collect_issues(tmp_path)[0]
    dismissed = work_cmd._make_import(
        issue.text,
        kind=issue.kind,
        source="handoff-ingest",
        metadata=issue.as_import_record()["metadata"],
    )
    dismissed["status"] = "dismissed"
    work_cmd._write_imports(tmp_path, [dismissed])

    assert handoff_cmd.sync_issues(target=tmp_path, json_output=True) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["issues"] == 1
    assert payload["known_issues"] == 1
    assert payload["new_issues"] == 0
    assert payload["imported"] == 0
    assert len(work_cmd._read_imports(tmp_path)) == 1


def test_handoff_sync_issues_closes_covered_warning_summary(tmp_path, capsys):
    log = tmp_path / "latest.log"
    log.write_text(
        "\n".join(
            [
                "SKIP current.md: no recognizable markdown sections found",
                "Warnings: 1",
                "",
            ]
        )
    )
    config = tmp_path / ".brigade" / "handoff-sources.json"
    config.parent.mkdir()
    config.write_text(
        json.dumps(
            {
                "sources": [{"root": ".", "inboxes": [".claude/memory-handoffs"]}],
                "ingestor": {"last_run_log": "latest.log"},
            }
        )
    )
    issues = handoff_cmd.collect_issues(tmp_path)
    skip_issue = [issue for issue in issues if issue.category == "skip"][0]
    summary_issue = [issue for issue in issues if issue.category == "warning-summary"][0]
    dismissed_skip = work_cmd._make_import(
        skip_issue.text,
        kind=skip_issue.kind,
        source="handoff-ingest",
        metadata=skip_issue.as_import_record()["metadata"],
    )
    dismissed_skip["status"] = "dismissed"
    pending_summary = work_cmd._make_import(
        summary_issue.text,
        kind=summary_issue.kind,
        source="handoff-ingest",
        metadata=summary_issue.as_import_record()["metadata"],
    )
    work_cmd._write_imports(tmp_path, [dismissed_skip, pending_summary])

    assert handoff_cmd.sync_issues(target=tmp_path, json_output=True) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["covered_summary_issues"] == 1
    assert payload["imported"] == 0
    assert payload["stale_imports_closed"] == 1
    imports = work_cmd._read_imports(tmp_path)
    assert imports[1]["status"] == "dismissed"
    assert imports[1]["dismiss_reason"] == "covered by known concrete handoff issue lines"


def test_handoff_sync_issues_dry_run_does_not_write(tmp_path, capsys):
    log = tmp_path / "latest.log"
    log.write_text("SKIP current.md: no recognizable markdown sections found\n")
    config = tmp_path / ".brigade" / "handoff-sources.json"
    config.parent.mkdir()
    config.write_text(
        json.dumps(
            {
                "sources": [{"root": ".", "inboxes": [".claude/memory-handoffs"]}],
                "ingestor": {"last_run_log": "latest.log"},
            }
        )
    )
    stale_metadata = {
        "handoff_issue_id": "handoff-route-skip-old",
        "handoff_issue_category": "route-skip",
    }
    work_cmd._write_imports(
        tmp_path,
        [
            work_cmd._make_import(
                "Fix old route skip",
                kind="task",
                source="handoff-ingest",
                metadata=stale_metadata,
            )
        ],
    )

    assert handoff_cmd.sync_issues(target=tmp_path, dry_run=True, json_output=True) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["imported"] == 1
    assert payload["stale_imports_closed"] == 1
    imports = work_cmd._read_imports(tmp_path)
    assert len(imports) == 1
    assert imports[0]["status"] == "pending"


def test_handoff_doctor_cli(tmp_path, monkeypatch):
    seen = {}

    def fake_doctor(**kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(handoff_cmd, "doctor", fake_doctor)

    assert cli.main(["handoff", "doctor", "--target", str(tmp_path), "--json"]) == 0
    assert seen == {"target": tmp_path, "sources": None, "json_output": True}


def test_handoff_lint_cli(tmp_path, monkeypatch):
    seen = {}

    def fake_lint(**kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(handoff_cmd, "lint", fake_lint)

    path = tmp_path / "note.md"
    assert cli.main(["handoff", "lint", "--target", str(tmp_path), "--json", str(path)]) == 0
    assert seen == {"target": tmp_path, "paths": [path], "json_output": True}


def test_handoff_issues_cli(tmp_path, monkeypatch):
    seen = {}

    def fake_issues(**kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(handoff_cmd, "issues", fake_issues)

    assert (
        cli.main(
            [
                "handoff",
                "issues",
                "--target",
                str(tmp_path),
                "--json",
                "--limit",
                "7",
                "--category",
                "skip",
            ]
        )
        == 0
    )
    assert seen == {"target": tmp_path, "sources": None, "json_output": True, "limit": 7, "categories": ["skip"]}


def test_handoff_import_issues_cli(tmp_path, monkeypatch):
    seen = {}

    def fake_import_issues(**kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(handoff_cmd, "import_issues", fake_import_issues)

    assert (
        cli.main(
            [
                "handoff",
                "import-issues",
                "--target",
                str(tmp_path),
                "--dry-run",
                "--json",
                "--category",
                "route-skip",
            ]
        )
        == 0
    )
    assert seen == {"target": tmp_path, "sources": None, "dry_run": True, "json_output": True, "categories": ["route-skip"]}


def test_handoff_sync_issues_cli(tmp_path, monkeypatch):
    seen = {}

    def fake_sync_issues(**kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(handoff_cmd, "sync_issues", fake_sync_issues)

    assert (
        cli.main(
            [
                "handoff",
                "sync-issues",
                "--target",
                str(tmp_path),
                "--dry-run",
                "--json",
                "--category",
                "route-skip",
                "--no-close-stale",
            ]
        )
        == 0
    )
    assert seen == {
        "target": tmp_path,
        "sources": None,
        "dry_run": True,
        "json_output": True,
        "categories": ["route-skip"],
        "close_stale": False,
    }
