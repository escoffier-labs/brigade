from __future__ import annotations

import json
import os
import time

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


PROMOTED_IMPORT_HANDOFF = """# Memory Handoff

## Type
decision

## Title
Reviewed scanner import

## Summary
Reviewed scanner import `import-one`.

## Evidence
- import: import-one
- source_fingerprint: fp-one
- scanner_id: chat-surfaces
- scanner_run_id: run-one
- sweep_id: sweep-one

## Recommended memory action
no-card

## Target document
.learnings/LEARNINGS.md

## Suggested document content
### Reviewed scanner import

- source: chat-memory-sweep
- kind: decision
- import: import-one
- summary: Durable scanner decision.
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


def test_handoff_list_and_show_report_draft_metadata(tmp_path, capsys):
    codex_inbox = tmp_path / ".codex" / "memory-handoffs"
    codex_inbox.mkdir(parents=True)
    path = codex_inbox / "2026-05-28-reviewed.md"
    path.write_text(PROMOTED_IMPORT_HANDOFF)

    assert handoff_cmd.list_drafts(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["counts"]["reviewed"] == 1
    draft = payload["drafts"][0]
    assert draft["id"] == "2026-05-28-reviewed"
    assert draft["lint"]["valid"] is True
    assert draft["action"] == "no-card"
    assert draft["target_document"] == ".learnings/LEARNINGS.md"
    assert draft["source_import_id"] == "import-one"
    assert draft["source_fingerprint"] == "fp-one"
    assert draft["scanner_provenance"]["scanner_id"] == "chat-surfaces"

    assert handoff_cmd.show_draft(target=tmp_path, draft_id="2026-05-28-reviewed") == 0
    out = capsys.readouterr().out
    assert "handoff: 2026-05-28-reviewed" in out
    assert "target_document: .learnings/LEARNINGS.md" in out
    assert "source_import_id: import-one" in out


def test_handoff_runs_list_show_and_draft_ingestion_status(tmp_path, capsys):
    codex_inbox = tmp_path / ".codex" / "memory-handoffs"
    codex_inbox.mkdir(parents=True)
    draft_path = codex_inbox / "2026-05-28-reviewed.md"
    draft_path.write_text(PROMOTED_IMPORT_HANDOFF)
    runs_root = tmp_path / ".brigade" / "handoffs" / "ingest-runs"
    runs_root.mkdir(parents=True)
    receipt = {
        "run_id": "run-one",
        "started_at": "2026-05-28T10:00:00+00:00",
        "completed_at": "2026-05-28T10:01:00+00:00",
        "source_root": str(tmp_path),
        "inbox_paths": [str(codex_inbox)],
        "processed_handoff_paths": [str(draft_path)],
        "promoted_card_targets": [],
        "routed_document_targets": [
            {"handoff_path": str(draft_path), "target": ".learnings/LEARNINGS.md"}
        ],
        "skipped_handoff_paths": [],
        "failed_handoff_paths": [],
        "warning_count": 0,
        "safe_summary": "processed=1",
        "log_path": str(tmp_path / ".brigade" / "handoff-ingest" / "latest.log"),
    }
    (runs_root / "run-one.json").write_text(json.dumps(receipt))

    assert handoff_cmd.runs(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 1
    assert payload["runs"][0]["run_id"] == "run-one"
    assert payload["runs"][0]["outcome_counts"] == {"ingested": 1}

    assert handoff_cmd.run_show(target=tmp_path, run_id="run-one", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["run"]["routed_document_targets"][0]["target"] == ".learnings/LEARNINGS.md"

    assert handoff_cmd.list_drafts(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["latest_ingest_run"]["run_id"] == "run-one"
    assert payload["drafts"][0]["ingestion_status"] == "ingested"
    assert payload["drafts"][0]["ingest_run_id"] == "run-one"

    assert handoff_cmd.show_draft(target=tmp_path, draft_id="2026-05-28-reviewed") == 0
    out = capsys.readouterr().out
    assert "ingestion_status: ingested" in out
    assert "ingest_run_id: run-one" in out


def test_handoff_reconcile_parses_ingestor_log_into_receipt(tmp_path, capsys):
    inbox = tmp_path / ".codex" / "memory-handoffs"
    inbox.mkdir(parents=True)
    draft_path = inbox / "reviewed.md"
    draft_path.write_text(NO_CARD_HANDOFF)
    skipped_path = inbox / "skipped.md"
    skipped_path.write_text("# Memory Handoff\n")
    log = tmp_path / ".brigade" / "handoff-ingest" / "latest.log"
    log.parent.mkdir(parents=True)
    log.write_text(
        "\n".join(
            [
                f"ROUTED {draft_path} -> .learnings/LEARNINGS.md",
                f"SKIP {skipped_path}: no recognizable markdown sections found",
                "Warnings: 1",
                "",
            ]
        )
    )
    config = tmp_path / ".brigade" / "handoff-sources.json"
    config.write_text(
        json.dumps(
            {
                "sources": [{"root": ".", "inboxes": [".codex/memory-handoffs"]}],
                "ingestor": {"last_run_log": ".brigade/handoff-ingest/latest.log"},
            }
        )
    )

    assert handoff_cmd.reconcile(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    receipt_path = tmp_path / payload["receipt_path"]
    assert receipt_path.exists()
    assert payload["run"]["processed_handoff_paths"] == [str(draft_path)]
    assert payload["run"]["skipped_handoff_paths"] == [str(skipped_path)]
    assert payload["run"]["warning_count"] == 1

    assert handoff_cmd.show_draft(target=tmp_path, draft_id="reviewed", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["draft"]["ingestion_status"] == "ingested"


def test_handoff_archive_preserves_reason_and_adds_ingest_outcome(tmp_path, capsys):
    inbox = tmp_path / ".codex" / "memory-handoffs"
    inbox.mkdir(parents=True)
    draft_path = inbox / "valid-one.md"
    draft_path.write_text(NO_CARD_HANDOFF)
    runs_root = tmp_path / ".brigade" / "handoffs" / "ingest-runs"
    runs_root.mkdir(parents=True)
    (runs_root / "run-archive.json").write_text(
        json.dumps(
            {
                "run_id": "run-archive",
                "started_at": "2026-05-28T10:00:00+00:00",
                "completed_at": "2026-05-28T10:01:00+00:00",
                "source_root": str(tmp_path),
                "inbox_paths": [str(inbox)],
                "processed_handoff_paths": [str(draft_path)],
                "promoted_card_targets": [],
                "routed_document_targets": [],
                "skipped_handoff_paths": [],
                "failed_handoff_paths": [],
                "warning_count": 0,
                "safe_summary": "processed=1",
                "log_path": "latest.log",
            }
        )
    )

    assert handoff_cmd.archive_draft(target=tmp_path, draft_id="valid-one", reason="manual review", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    record = payload["records"][0]
    assert record["review_reason"] == "manual review"
    assert record["ingestion_status"] == "ingested"
    assert record["ingest_run_id"] == "run-archive"


def test_handoff_doctor_warns_for_stale_unreconciled_draft(tmp_path, capsys):
    inbox = tmp_path / ".codex" / "memory-handoffs"
    inbox.mkdir(parents=True)
    stale = inbox / "stale.md"
    stale.write_text(NO_CARD_HANDOFF)
    old = time.time() - (handoff_cmd.HANDOFF_DRAFT_STALE_HOURS + 2) * 3600
    os.utime(stale, (old, old))

    assert handoff_cmd.doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "[warn] handoff_draft_unreconciled:" in out


def test_handoff_list_discovers_claude_codex_and_configured_inboxes(tmp_path, capsys):
    for rel, name in (
        (".claude/memory-handoffs", "claude.md"),
        (".codex/memory-handoffs", "codex.md"),
        ("custom/handoffs", "custom.md"),
    ):
        inbox = tmp_path / rel
        inbox.mkdir(parents=True)
        (inbox / name).write_text(NO_CARD_HANDOFF)
    sources = tmp_path / ".brigade" / "handoff-sources.json"
    sources.parent.mkdir(parents=True)
    sources.write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "root": ".",
                        "inboxes": [
                            ".claude/memory-handoffs",
                            ".codex/memory-handoffs",
                            "custom/handoffs",
                        ],
                    }
                ]
            }
        )
        + "\n"
    )

    assert handoff_cmd.list_drafts(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert {draft["path"].rsplit("/", 1)[-1] for draft in payload["drafts"]} == {"claude.md", "codex.md", "custom.md"}
    assert all(draft["watched"] is True for draft in payload["drafts"])


def test_handoff_archive_single_and_all_reviewed_write_records(tmp_path, capsys):
    inbox = tmp_path / ".codex" / "memory-handoffs"
    inbox.mkdir(parents=True)
    (inbox / "valid-one.md").write_text(NO_CARD_HANDOFF)
    (inbox / "valid-two.md").write_text(PROMOTED_IMPORT_HANDOFF)
    (inbox / "invalid.md").write_text("# Memory Handoff\n")

    assert handoff_cmd.archive_draft(target=tmp_path, draft_id="valid-one", reason="reviewed", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["archived"] == 1
    assert not (inbox / "valid-one.md").exists()
    assert payload["records"][0]["status"] == "archived"
    assert payload["records"][0]["review_reason"] == "reviewed"

    assert handoff_cmd.archive_draft(target=tmp_path, all_reviewed=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["archived"] == 1
    assert not (inbox / "valid-two.md").exists()
    assert (inbox / "invalid.md").exists()
    records = [
        json.loads(line)
        for line in (tmp_path / ".brigade" / "handoffs" / "archive.jsonl").read_text().splitlines()
    ]
    assert [record["id"] for record in records] == ["valid-one", "valid-two"]


def test_handoff_doctor_reports_draft_queue_warnings(tmp_path, capsys):
    inbox = tmp_path / ".codex" / "memory-handoffs"
    inbox.mkdir(parents=True)
    stale = inbox / "stale.md"
    stale.write_text(PROMOTED_IMPORT_HANDOFF)
    invalid = inbox / "invalid.md"
    invalid.write_text("# Memory Handoff\n")
    old = time.time() - (handoff_cmd.HANDOFF_DRAFT_STALE_HOURS + 2) * 3600
    os.utime(stale, (old, old))

    assert handoff_cmd.doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "[warn] handoff_draft_stale:" in out
    assert "[warn] handoff_draft_invalid:" in out
    assert "[warn] handoff_draft_missing_source_import:" in out
    assert "[warn] handoff_draft_uncovered_inbox:" in out


def test_handoff_doctor_reports_changed_source_fingerprint(tmp_path, capsys):
    inbox = tmp_path / ".codex" / "memory-handoffs"
    inbox.mkdir(parents=True)
    (inbox / "promoted.md").write_text(PROMOTED_IMPORT_HANDOFF)
    work_cmd._write_imports(
        tmp_path,
        [
            {
                "id": "import-one",
                "kind": "decision",
                "source": "chat-memory-sweep",
                "text": "Durable scanner decision.",
                "status": "promoted",
                "metadata": {"source_item_key": "chat:one", "source_fingerprint": "fp-two"},
            }
        ],
    )

    assert handoff_cmd.doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "[warn] handoff_draft_changed_source_fingerprint:" in out


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


def test_handoff_source_coverage_imports_are_fingerprinted_and_respect_dismissal(tmp_path, capsys):
    inbox = tmp_path / ".claude" / "memory-handoffs"
    inbox.mkdir(parents=True)
    (inbox / "one.md").write_text(NO_CARD_HANDOFF)
    memory_index = tmp_path / "MEMORY.md"
    memory_index.write_text("# Memory\n")

    assert handoff_cmd.import_issues(target=tmp_path, categories=["untracked-inbox"], json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["imported"] == 1
    item = payload["imports"][0]
    assert item["metadata"]["handoff_issue_category"] == "untracked-inbox"
    assert item["metadata"]["source_item_key"] == "handoff-ingest:untracked-inbox:.claude/memory-handoffs"
    first_fingerprint = item["metadata"]["source_fingerprint"]

    assert work_cmd.import_dismiss(target=tmp_path, import_id=item["id"], reason="covered elsewhere") == 0
    capsys.readouterr()
    assert handoff_cmd.import_issues(target=tmp_path, categories=["untracked-inbox"], json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["imported"] == 0
    assert payload["skipped_dismissed"] == 1

    (inbox / "two.md").write_text(NO_CARD_HANDOFF)
    assert handoff_cmd.import_issues(target=tmp_path, categories=["untracked-inbox"], json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["imported"] == 1
    assert payload["imports"][0]["metadata"]["source_fingerprint"] != first_fingerprint
    assert memory_index.read_text() == "# Memory\n"


def test_handoff_source_config_drift_reports_missing_configured_inbox(tmp_path, capsys):
    config = tmp_path / ".brigade" / "handoff-sources.json"
    config.parent.mkdir()
    config.write_text(
        json.dumps(
            {
                "sources": [{"root": ".", "inboxes": [".missing/memory-handoffs"]}],
            }
        )
    )

    assert handoff_cmd.doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "[warn] handoff_source_coverage:" in out
    assert "configured inbox missing" in out

    assert handoff_cmd.import_issues(target=tmp_path, categories=["source-inbox-missing"], dry_run=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["by_category"] == {"source-inbox-missing": 1}
    item = payload["imports"][0]
    assert item["metadata"]["source_item_key"] == "handoff-ingest:source-inbox:.missing/memory-handoffs"
    assert item["metadata"]["source_fingerprint"]


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


def test_handoff_issues_parse_hardened_ingestor_warning_states(tmp_path, capsys):
    log = tmp_path / "latest.log"
    log.write_text(
        "\n".join(
            [
                "skipped bad.md: malformed handoff sections",
                "FAILED failed.md: write failed",
                "MALFORMED malformed.md: missing required section",
                "source unreachable safe-source-label",
                "no_updates",
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

    assert handoff_cmd.issues(target=tmp_path, json_output=True) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["by_category"]["skip"] == 1
    assert payload["by_category"]["failed"] == 1
    assert payload["by_category"]["malformed"] == 1
    assert payload["by_category"]["source-unreachable"] == 1
    assert payload["by_category"]["no-reply"] == 1
    repairs = {issue["category"]: issue["repair"] for issue in payload["issues"]}
    assert "failed handoff" in repairs["failed"]
    assert "malformed handoff" in repairs["malformed"]
    assert "no-reply output cannot hide" in repairs["no-reply"]

    assert handoff_cmd.import_issues(target=tmp_path, dry_run=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    imported_categories = {item["metadata"]["handoff_issue_category"] for item in payload["imports"]}
    assert {"skip", "failed", "malformed", "source-unreachable", "no-reply"} <= imported_categories


def test_handoff_reconcile_records_hardened_warning_events(tmp_path, capsys):
    log = tmp_path / "latest.log"
    log.write_text(
        "\n".join(
            [
                "SKIPPED skipped.md: malformed handoff sections",
                "FAILED failed.md: write failed",
                "MALFORMED malformed.md: missing required section",
                "cannot reach safe-source-label",
                "NO_REPLY",
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

    assert handoff_cmd.reconcile(target=tmp_path, json_output=True) == 0

    payload = json.loads(capsys.readouterr().out)
    run = payload["run"]
    assert run["skipped_handoff_paths"] == ["skipped.md"]
    assert run["failed_handoff_paths"] == ["failed.md"]
    assert run["malformed_handoff_paths"] == ["malformed.md"]
    assert run["unreachable_sources"] == ["cannot reach safe-source-label"]
    assert run["no_reply"] is True
    categories = {event["category"] for event in run["warning_events"]}
    assert {"skip", "failed", "malformed", "source-unreachable", "no-reply"} <= categories
    assert run["warning_count"] >= 5


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


def test_handoff_draft_review_cli(tmp_path, monkeypatch):
    seen = []

    def fake_list_drafts(**kwargs):
        seen.append(("list", kwargs))
        return 0

    def fake_show_draft(**kwargs):
        seen.append(("show", kwargs))
        return 0

    def fake_archive_draft(**kwargs):
        seen.append(("archive", kwargs))
        return 0

    def fake_runs(**kwargs):
        seen.append(("runs", kwargs))
        return 0

    def fake_run_show(**kwargs):
        seen.append(("run-show", kwargs))
        return 0

    def fake_reconcile(**kwargs):
        seen.append(("reconcile", kwargs))
        return 0

    monkeypatch.setattr(handoff_cmd, "list_drafts", fake_list_drafts)
    monkeypatch.setattr(handoff_cmd, "show_draft", fake_show_draft)
    monkeypatch.setattr(handoff_cmd, "archive_draft", fake_archive_draft)
    monkeypatch.setattr(handoff_cmd, "runs", fake_runs)
    monkeypatch.setattr(handoff_cmd, "run_show", fake_run_show)
    monkeypatch.setattr(handoff_cmd, "reconcile", fake_reconcile)

    assert cli.main(["handoff", "list", "--target", str(tmp_path), "--json", "--limit", "3"]) == 0
    assert cli.main(["handoff", "show", "draft-one", "--target", str(tmp_path), "--json"]) == 0
    assert (
        cli.main(
            [
                "handoff",
                "archive",
                "--target",
                str(tmp_path),
                "--all-reviewed",
                "--reason",
                "done",
                "--json",
            ]
        )
        == 0
    )
    assert cli.main(["handoff", "runs", "--target", str(tmp_path), "--json", "--limit", "2"]) == 0
    assert cli.main(["handoff", "run-show", "run-one", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["handoff", "reconcile", "--target", str(tmp_path), "--json"]) == 0
    assert seen == [
        ("list", {"target": tmp_path, "sources": None, "json_output": True, "limit": 3}),
        ("show", {"target": tmp_path, "draft_id": "draft-one", "sources": None, "json_output": True}),
        (
            "archive",
            {
                "target": tmp_path,
                "draft_id": None,
                "all_reviewed": True,
                "reason": "done",
                "sources": None,
                "json_output": True,
            },
        ),
        ("runs", {"target": tmp_path, "json_output": True, "limit": 2}),
        ("run-show", {"target": tmp_path, "run_id": "run-one", "json_output": True}),
        ("reconcile", {"target": tmp_path, "sources": None, "json_output": True}),
    ]


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


# --- Handoff backlog detection (pending AND stale) -------------------------

BACKLOG_STALE_SECONDS = 3 * 24 * 60 * 60  # mirror handoff_cmd.BACKLOG_STALE_SECONDS


def _write_handoff(tmp_path, name: str, *, age_seconds: float | None = None) -> None:
    inbox = tmp_path / ".claude" / "memory-handoffs"
    inbox.mkdir(parents=True, exist_ok=True)
    path = inbox / name
    path.write_text("# Memory Handoff\n")
    if age_seconds is not None:
        ts = time.time() - age_seconds
        os.utime(path, (ts, ts))


def test_inspect_tracks_oldest_pending_age(tmp_path):
    _write_handoff(tmp_path, "old.md", age_seconds=BACKLOG_STALE_SECONDS + 3600)
    _write_handoff(tmp_path, "new.md", age_seconds=10)

    health = handoff_cmd.inspect(tmp_path)
    claude_inbox = next(i for i in health.inboxes if i.inbox == ".claude/memory-handoffs")

    assert claude_inbox.pending == 2
    assert claude_inbox.oldest_pending_age_seconds is not None
    # oldest is the stale one
    assert claude_inbox.oldest_pending_age_seconds >= BACKLOG_STALE_SECONDS


def test_doctor_checks_warn_on_stale_backlog(tmp_path):
    _write_handoff(tmp_path, "stuck.md", age_seconds=BACKLOG_STALE_SECONDS + 3600)

    checks = handoff_cmd.doctor_checks(tmp_path)
    backlog = [c for c in checks if c[1] == "handoff_backlog"]

    assert backlog, "expected a handoff_backlog check"
    status, _name, detail = backlog[0]
    assert status == handoff_cmd.WARN
    assert "1" in detail  # one pending


def test_doctor_checks_no_backlog_warn_for_fresh_pending(tmp_path):
    _write_handoff(tmp_path, "fresh.md", age_seconds=30)

    checks = handoff_cmd.doctor_checks(tmp_path)
    backlog_warns = [
        c for c in checks if c[1] == "handoff_backlog" and c[0] == handoff_cmd.WARN
    ]
    assert not backlog_warns, "fresh pending handoff should not trip the stale backlog warning"


def test_handoff_writer_inboxes_include_opencode():
    from brigade import handoff_cmd
    assert ".opencode/memory-handoffs" in handoff_cmd.WRITER_INBOXES


def test_handoff_sources_example_lists_opencode():
    import json
    from brigade.templates import template_root
    data = json.loads((template_root() / "handoff" / "handoff-sources.example.json").read_text())
    assert ".opencode/memory-handoffs" in data["sources"][0]["inboxes"]
