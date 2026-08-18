import json
import os
import re
import shlex
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from brigade import cli
from brigade import dogfood_cmd
from brigade import memory_cmd
from brigade import runbook_cmd
from brigade import work_cmd
from brigade.templates import template_root

MEMORY_CARE_RUNBOOK_NAMES = memory_cmd.MEMORY_CARE_RUNBOOK_NAMES

EXPECTED_RUNBOOK_STEP_FRAGMENTS = {
    "daily-care-pass.json": ("memory care scan", "memory care import-issues"),
    "ingest-sweep.json": ("handoff lint", "ingest"),
    "weekly-outcome-ratchet.json": ("outcome rank", "outcome reconcile"),
}

_MODEL_DISPATCH_KEYS = frozenset(
    {
        "model",
        "model_dispatch",
        "model_seat",
        "worker",
        "worker_dispatch",
        "worker_seat",
        "seat",
        "roster",
        "agent",
        "harness",
        "budget_gate",
        "dispatch",
    }
)

_SCHEDULER_KEYS = frozenset(
    {
        "scheduler",
        "schedule",
        "cron",
        "crontab",
        "timer",
        "systemd",
    }
)


def _memory_care_runbook_dir(target):
    return target / ".brigade" / "memory-care" / "runbooks"


def _load_memory_care_runbooks(target):
    runbook_dir = _memory_care_runbook_dir(target)
    return {name: json.loads((runbook_dir / name).read_text()) for name in MEMORY_CARE_RUNBOOK_NAMES}


def _bundled_memory_care_runbook_dir() -> Path:
    return template_root() / "memory-care" / "runbooks"


def _assert_mechanical_template_shape(payload: dict, *, expect_pins: bool) -> None:
    forbidden = _MODEL_DISPATCH_KEYS | _SCHEDULER_KEYS
    for key in forbidden:
        assert key not in payload
    assert payload.get("allowed_commands") == ["brigade"]
    steps = payload.get("steps")
    assert isinstance(steps, list) and steps
    for step in steps:
        assert isinstance(step, dict)
        for key in forbidden:
            assert key not in step
        tokens = shlex.split(step["run"])
        assert tokens and Path(tokens[0]).name == "brigade"
    if expect_pins:
        pins = payload.get("pins")
        assert isinstance(pins, list) and pins
        brigade_pins = [pin for pin in pins if pin.get("command") == "brigade"]
        assert brigade_pins
        for pin in brigade_pins:
            assert isinstance(pin.get("sha256"), str) and pin["sha256"].strip()
    else:
        assert "pins" not in payload


def _assert_mechanical_brigade_only_runbook(payload):
    _assert_mechanical_template_shape(payload, expect_pins=True)


def _write_card(path, frontmatter, body="Body.\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    for key, value in frontmatter.items():
        if isinstance(value, list):
            rendered = json.dumps(value)
        else:
            rendered = str(value)
        lines.append(f"{key}: {rendered}")
    lines.extend(["---", "", body])
    path.write_text("\n".join(lines))


def test_memory_care_init_status_and_doctor(tmp_path, capsys):
    assert memory_cmd.init(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "memory_care_config:" in out
    assert (tmp_path / ".brigade" / "memory-care.toml").is_file()
    assert ".brigade/memory-care.toml" in (tmp_path / ".gitignore").read_text()

    assert memory_cmd.status(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["config_path"].endswith(".brigade/memory-care.toml")
    assert any(check["name"] == "memory_care_scan" for check in payload["checks"])
    archive = payload["archive_candidates"]
    assert archive["read_only"] is True
    assert archive["would_archive"] is False
    assert archive["approval_required"] is True
    assert archive["candidate_count"] == 0
    assert archive["candidates"] == []

    assert memory_cmd.doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "memory care doctor:" in out
    assert "memory_care_config" in out


def test_memory_care_doctor_warns_on_missing_card_ids(tmp_path):
    cards = tmp_path / "memory" / "cards"
    _write_card(cards / "legacy.md", {"topic": "legacy", "confidence": "high", "evidence": ["README.md"]})
    assert memory_cmd.scan(target=tmp_path) == 0
    health = memory_cmd.health(tmp_path)
    check = next(item for item in health["checks"] if item["name"] == "memory_card_ids")
    assert check["status"] == "warn"
    assert health["valid"] is True


def test_memory_care_scan_detects_card_decay_issues(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(memory_cmd, "_today", lambda: date(2026, 5, 28))
    config = tmp_path / ".brigade" / "memory-care.toml"
    config.parent.mkdir()
    config.write_text(
        "\n".join(
            [
                'card_roots = ["memory/cards"]',
                'index_paths = ["MEMORY.md"]',
                "stale_after_days = 30",
                "expiry_warning_days = 0",
                'minimum_confidence = "medium"',
                "require_evidence = true",
                "include_paths = []",
                'exclude_paths = ["memory/cards/decay"]',
                'output_path = "memory/cards/decay"',
                'enabled_checks = ["stale", "expired", "undersourced", "contradictory", "missing-index-link", "orphaned-card", "oversized-card", "missing-frontmatter"]',
                "max_card_bytes = 320",
                "",
            ]
        )
    )
    cards = tmp_path / "memory" / "cards"
    _write_card(
        cards / "stale.md",
        {"topic": "stale", "last_reviewed": "2026-01-01", "confidence": "high", "evidence": ["TOOLS.md"]},
    )
    _write_card(
        cards / "expired.md",
        {
            "topic": "expired",
            "last_reviewed": "2026-05-20",
            "fresh_until": "2026-05-01",
            "confidence": "high",
            "evidence": ["README.md"],
        },
    )
    _write_card(
        cards / "undersourced.md", {"topic": "undersourced", "last_reviewed": "2026-05-20", "confidence": "low"}
    )
    _write_card(
        cards / "orphan.md",
        {"topic": "orphan", "last_reviewed": "2026-05-20", "confidence": "high", "evidence": ["README.md"]},
    )
    _write_card(
        cards / "dup-a.md",
        {"topic": "duplicate", "last_reviewed": "2026-05-20", "confidence": "high", "evidence": ["README.md"]},
    )
    _write_card(
        cards / "dup-b.md",
        {"topic": "duplicate", "last_reviewed": "2026-05-20", "confidence": "high", "evidence": ["README.md"]},
    )
    _write_card(
        cards / "big.md",
        {"topic": "big", "last_reviewed": "2026-05-20", "confidence": "high", "evidence": ["README.md"]},
        body="x" * 500,
    )
    (cards / "nofm.md").write_text("No frontmatter.\n")
    (tmp_path / "MEMORY.md").write_text(
        "\n".join(
            [
                "- [stale](memory/cards/stale.md)",
                "- [expired](memory/cards/expired.md)",
                "- [undersourced](memory/cards/undersourced.md)",
                "- [missing](memory/cards/missing.md)",
                "- [dup-a](memory/cards/dup-a.md)",
                "- [dup-b](memory/cards/dup-b.md)",
                "- [big](memory/cards/big.md)",
                "- [nofm](memory/cards/nofm.md)",
            ]
        )
    )

    assert memory_cmd.scan(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    issue_types = {issue["issue_type"] for issue in payload["issues"]}
    assert {
        "stale",
        "expired",
        "undersourced",
        "missing-index-link",
        "orphaned-card",
        "oversized-card",
        "missing-frontmatter",
        "contradictory",
    } <= issue_types
    assert payload["queue_path"].endswith("memory/cards/decay/refresh-queue.json")
    queue = json.loads((tmp_path / "memory" / "cards" / "decay" / "refresh-queue.json").read_text())
    first = queue["cards"][0]
    assert first["source_fingerprint"]
    assert first["safe_summary"]
    assert first["suggested_refresh_action"]


def test_memory_care_status_explains_freshness_metadata(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(memory_cmd, "_today", lambda: date(2026, 5, 28))
    cards = tmp_path / "memory" / "cards"
    _write_card(cards / "missing-meta.md", {"topic": "missing-meta", "confidence": "high", "evidence": ["README.md"]})
    _write_card(
        cards / "stale.md",
        {
            "topic": "stale",
            "last_reviewed": "2026-01-01",
            "fresh_until": "2026-12-01",
            "confidence": "high",
            "evidence": ["README.md"],
        },
    )
    _write_card(
        cards / "expired.md",
        {
            "topic": "expired",
            "last_reviewed": "2026-05-01",
            "fresh_until": "2026-05-01",
            "confidence": "high",
            "evidence": ["README.md"],
        },
    )
    _write_card(
        cards / "undersourced.md",
        {"topic": "undersourced", "last_reviewed": "2026-05-01", "fresh_until": "2026-12-01", "confidence": "low"},
    )
    (tmp_path / "MEMORY.md").write_text(
        "\n".join(
            [
                "- [missing-meta](memory/cards/missing-meta.md)",
                "- [stale](memory/cards/stale.md)",
                "- [expired](memory/cards/expired.md)",
                "- [undersourced](memory/cards/undersourced.md)",
            ]
        )
        + "\n"
    )

    assert memory_cmd.scan(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    issue_types = {issue["issue_type"] for issue in payload["issues"]}
    assert {"missing-reviewed", "missing-freshness", "stale", "expired", "undersourced"} <= issue_types
    assert payload["metadata"]["reviewed_dates"] == {"present": 3, "missing": 1, "stale": 1}
    assert payload["metadata"]["freshness_dates"] == {"present": 3, "missing": 1, "expired": 1}
    assert payload["metadata"]["evidence"] == {"present": 3, "missing": 1}
    queue = json.loads((tmp_path / ".brigade" / "memory-care" / "decay" / "refresh-queue.json").read_text())
    queued_types = {card["issue_type"] for card in queue["cards"]}
    assert "missing-reviewed" in queued_types
    assert "missing-freshness" in queued_types

    assert memory_cmd.status(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "reviewed_dates: present=3 missing=1 stale=1" in out
    assert "freshness_dates: present=3 missing=1 expired=1" in out
    assert "evidence_metadata: present=3 missing=1" in out
    assert "confidence_metadata: high=3, low=1" in out

    assert memory_cmd.status(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["metadata"]["freshness_dates"]["missing"] == 1
    assert payload["autofix_plan"]["plan_count"] == 2
    assert payload["autofix_plan"]["blocked_count"] == 2

    assert memory_cmd.import_issues(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    imported_types = {item["metadata"]["issue_type"] for item in payload["imports"]}
    assert "missing-reviewed" in imported_types
    assert "missing-freshness" in imported_types


def test_memory_care_health_exposes_scan_and_queue_counts(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_cmd, "_today", lambda: date(2026, 5, 28))
    cards = tmp_path / "memory" / "cards"
    _write_card(
        cards / "stale.md",
        {
            "topic": "stale",
            "last_reviewed": "2026-01-01",
            "fresh_until": "2026-12-01",
            "confidence": "high",
            "evidence": ["README.md"],
        },
    )
    (tmp_path / "MEMORY.md").write_text("- [stale](memory/cards/stale.md)\n")
    assert memory_cmd.scan(target=tmp_path) == 0
    health = memory_cmd.health(tmp_path)
    assert health["scan_issue_count"] >= 1
    assert health["queue_count"] >= 1
    assert health["issue_count"] == len([c for c in health["checks"] if c.get("status") != "ok"])


def test_memory_care_scan_flags_missing_evidence_refs(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(memory_cmd, "_today", lambda: date(2026, 5, 28))
    cache_home = tmp_path / "cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_home))
    receipt = tmp_path / ".brigade" / "work" / "verify-runs" / "verify-one" / "receipt.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text(json.dumps({"status": "completed"}))
    bundle = cache_home / "miseledger" / "evidence" / "bundle-ok.json"
    bundle.parent.mkdir(parents=True)
    bundle.write_text(json.dumps({"id": "bundle-ok"}))
    cards = tmp_path / "memory" / "cards"
    _write_card(
        cards / "evidence-backed.md",
        {
            "topic": "evidence-backed",
            "last_reviewed": "2026-05-01",
            "fresh_until": "2026-12-01",
            "confidence": "high",
            "evidence": [
                ".brigade/work/verify-runs/verify-one/receipt.json",
                "miseledger://evidence/bundle-ok",
                "miseledger://evidence/bundle-missing",
            ],
        },
    )
    (tmp_path / "MEMORY.md").write_text("- [evidence-backed](memory/cards/evidence-backed.md)\n")

    assert memory_cmd.scan(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)

    issue = next(issue for issue in payload["issues"] if issue["issue_type"] == "missing-evidence-ref")
    assert issue["severity"] == "high"
    assert issue["evidence_references"] == ["missing MiseLedger evidence bundle: miseledger://evidence/bundle-missing"]
    assert payload["metadata"]["evidence_refs"] == {"present": 3, "missing": 1}

    assert memory_cmd.status(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "evidence_refs: present=3 missing=1" in out


def test_memory_care_plan_fixes_reports_blockers_and_writes_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(memory_cmd, "_today", lambda: date(2026, 5, 28))
    cards = tmp_path / "memory" / "cards"
    reviewed_missing = cards / "reviewed-missing.md"
    freshness_missing = cards / "freshness-missing.md"
    _write_card(
        reviewed_missing,
        {"topic": "reviewed-missing", "fresh_until": "2026-12-01", "confidence": "high", "evidence": ["README.md"]},
    )
    _write_card(
        freshness_missing,
        {"topic": "freshness-missing", "last_reviewed": "2026-05-01", "confidence": "high", "evidence": ["README.md"]},
    )
    (tmp_path / "MEMORY.md").write_text(
        "- [reviewed-missing](memory/cards/reviewed-missing.md)\n"
        "- [freshness-missing](memory/cards/freshness-missing.md)\n"
    )
    before_reviewed = reviewed_missing.read_text()
    before_freshness = freshness_missing.read_text()

    assert memory_cmd.scan(target=tmp_path, json_output=True) == 0
    capsys.readouterr()
    assert memory_cmd.plan_fixes(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["would_write"] is False
    assert payload["plan_count"] == 2
    assert payload["blocked_count"] == 2
    by_type = {item["issue_type"]: item for item in payload["items"]}
    assert by_type["missing-reviewed"]["candidate_fields"] == {"last_reviewed": "2026-05-28"}
    assert by_type["missing-reviewed"]["blockers"] == ["requires-current-evidence-review"]
    assert by_type["missing-freshness"]["candidate_fields"] == {"fresh_until": "<operator-selected-date>"}
    assert by_type["missing-freshness"]["blockers"] == ["requires-operator-freshness-date"]
    assert reviewed_missing.read_text() == before_reviewed
    assert freshness_missing.read_text() == before_freshness

    assert memory_cmd.plan_fixes(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "memory care fix plan:" in out
    assert "would_write: false" in out
    assert "blocked: 2" in out


def test_memory_care_imports_autofix_plan_and_brief_visibility(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(memory_cmd, "_today", lambda: date(2026, 5, 28))
    monkeypatch.setattr(work_cmd.helpers, "_now", lambda: datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc))
    cards = tmp_path / "memory" / "cards"
    _write_card(
        cards / "missing-reviewed.md",
        {"topic": "missing-reviewed", "fresh_until": "2026-12-01", "confidence": "high", "evidence": ["README.md"]},
    )
    (tmp_path / "MEMORY.md").write_text("- [missing-reviewed](memory/cards/missing-reviewed.md)\n")

    assert memory_cmd.scan(target=tmp_path) == 0
    capsys.readouterr()
    assert memory_cmd.import_issues(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["created"] == 1
    metadata = payload["imports"][0]["metadata"]
    assert metadata["issue_type"] == "missing-reviewed"
    assert metadata["safe_autofix_plan"]["would_write"] is False
    assert metadata["safe_autofix_plan"]["blockers"] == ["requires-current-evidence-review"]

    assert work_cmd.brief(target=tmp_path, json_output=True) == 0
    brief = json.loads(capsys.readouterr().out)
    assert brief["memory_care"]["autofix_plan"]["plan_count"] == 1
    assert brief["memory_care"]["autofix_plan"]["suggested_next_command"] == "brigade memory care plan-fixes"
    assert work_cmd.brief(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "memory_care_fix_plan: planned=1 blocked=1 command=brigade memory care plan-fixes" in out


def test_memory_care_imports_dedupe_and_respect_dismissed(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(memory_cmd, "_today", lambda: date(2026, 5, 28))
    monkeypatch.setattr(work_cmd.helpers, "_now", lambda: datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc))
    cards = tmp_path / "memory" / "cards"
    _write_card(
        cards / "stale.md",
        {
            "topic": "stale",
            "last_reviewed": "2026-01-01",
            "fresh_until": "2026-12-01",
            "confidence": "high",
            "evidence": ["README.md"],
        },
    )
    (tmp_path / "MEMORY.md").write_text("- [stale](memory/cards/stale.md)\n")
    assert memory_cmd.scan(target=tmp_path) == 0
    capsys.readouterr()

    assert memory_cmd.import_issues(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["created"] == 1
    item = payload["imports"][0]
    assert item["source"] == "memory-care"
    assert item["metadata"]["issue_type"] == "stale"
    assert item["metadata"]["safe_summary"]
    assert item["metadata"]["source_fingerprint"]

    assert memory_cmd.import_issues(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["created"] == 0
    assert payload["skipped_duplicates"] == 1

    assert work_cmd.import_dismiss(target=tmp_path, import_id=item["id"], reason="not now") == 0
    capsys.readouterr()
    assert memory_cmd.import_issues(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["created"] == 0
    assert payload["skipped_dismissed"] == 1


def test_memory_care_promoted_task_reaches_work_run_acceptance(tmp_path, monkeypatch, capsys):
    artifacts_dir = tmp_path / ".brigade" / "runs"
    dogfood_cmd.init(target=tmp_path, artifacts_dir=artifacts_dir)
    monkeypatch.setattr(memory_cmd, "_today", lambda: date(2026, 5, 28))
    times = iter(
        [
            datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 28, 12, 1, tzinfo=timezone.utc),
            datetime(2026, 5, 28, 12, 2, tzinfo=timezone.utc),
            datetime(2026, 5, 28, 12, 3, tzinfo=timezone.utc),
            datetime(2026, 5, 28, 12, 4, tzinfo=timezone.utc),
            datetime(2026, 5, 28, 12, 5, tzinfo=timezone.utc),
        ]
    )
    monkeypatch.setattr(work_cmd.helpers, "_now", lambda: next(times))
    cards = tmp_path / "memory" / "cards"
    _write_card(
        cards / "stale.md",
        {
            "topic": "stale",
            "last_reviewed": "2026-01-01",
            "fresh_until": "2026-12-01",
            "confidence": "high",
            "evidence": ["README.md"],
        },
    )
    (tmp_path / "MEMORY.md").write_text("- [stale](memory/cards/stale.md)\n")
    assert memory_cmd.scan(target=tmp_path) == 0
    assert memory_cmd.import_issues(target=tmp_path) == 0
    assert work_cmd.import_promote(target=tmp_path, all_matching=True, source="memory-care", kind="task") == 0
    capsys.readouterr()
    seen = {}

    def fake_dogfood_run(task, **kwargs):
        seen["task"] = task
        run_dir = kwargs["output_dir"]
        run_dir.mkdir(parents=True)
        (run_dir / "run.json").write_text(
            json.dumps({"started_at": "2026-05-28T12:10:00Z", "status": "ok", "task": task})
        )
        (run_dir / "final.txt").write_text("Done.\n")
        return 0

    monkeypatch.setattr(dogfood_cmd, "run", fake_dogfood_run)

    assert work_cmd.run(None, target=tmp_path, output_dir=artifacts_dir / "new", handoff=False) == 0
    assert "Refresh memory card memory/cards/stale.md" in seen["task"]
    assert "Review `memory/cards/stale.md` against current source evidence." in seen["task"]


def test_memory_care_cli(tmp_path, monkeypatch):
    seen = []

    def fake_init(**kwargs):
        seen.append(("init", kwargs))
        return 0

    def fake_scan(**kwargs):
        seen.append(("scan", kwargs))
        return 0

    def fake_plan_fixes(**kwargs):
        seen.append(("plan-fixes", kwargs))
        return 0

    def fake_status(**kwargs):
        seen.append(("status", kwargs))
        return 0

    def fake_doctor(**kwargs):
        seen.append(("doctor", kwargs))
        return 0

    def fake_import(**kwargs):
        seen.append(("import", kwargs))
        return 0

    monkeypatch.setattr(memory_cmd, "init", fake_init)
    monkeypatch.setattr(memory_cmd, "scan", fake_scan)
    monkeypatch.setattr(memory_cmd, "plan_fixes", fake_plan_fixes)
    monkeypatch.setattr(memory_cmd, "status", fake_status)
    monkeypatch.setattr(memory_cmd, "doctor", fake_doctor)
    monkeypatch.setattr(memory_cmd, "import_issues", fake_import)
    assert (
        cli.main(
            [
                "memory",
                "care",
                "init",
                "--target",
                str(tmp_path),
                "--force",
                "--no-gitignore",
                "--with-runbooks",
            ]
        )
        == 0
    )
    assert cli.main(["memory", "care", "scan", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["memory", "care", "plan-fixes", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["memory", "care", "status", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["memory", "care", "doctor", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["memory", "care", "import-issues", "--target", str(tmp_path), "--dry-run", "--json"]) == 0
    assert seen == [
        ("init", {"target": tmp_path, "force": True, "update_gitignore": False, "with_runbooks": True}),
        ("scan", {"target": tmp_path, "json_output": True}),
        ("plan-fixes", {"target": tmp_path, "json_output": True}),
        ("status", {"target": tmp_path, "json_output": True}),
        ("doctor", {"target": tmp_path, "json_output": True}),
        ("import", {"target": tmp_path, "dry_run": True, "json_output": True}),
    ]


def test_memory_care_scan_tolerates_malformed_utf8_index(tmp_path, capsys):
    cards = tmp_path / "memory" / "cards"
    _write_card(
        cards / "ok.md",
        {"topic": "ok", "last_reviewed": "2026-05-20", "confidence": "high", "evidence": ["README.md"]},
    )
    (tmp_path / "MEMORY.md").write_bytes(b"# index\n\xff not utf-8\n")

    assert memory_cmd.scan(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["issue_count"] >= 0


def test_memory_care_scan_default_output_lives_under_brigade_state(tmp_path, capsys):
    cards = tmp_path / "memory" / "cards"
    cards.mkdir(parents=True)
    (cards / "old.md").write_text(
        "---\ntopic: old\ncategory: system\ntags: [a]\nreviewed: 2020-01-01\n---\n\nOld fact.\n"
    )
    (tmp_path / "MEMORY.md").write_text("- [old](memory/cards/old.md)\n")

    assert memory_cmd.scan(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert ".brigade/memory-care/decay" in payload["scan_path"]
    assert (tmp_path / ".brigade" / "memory-care" / "decay" / "scan-latest.json").is_file()
    assert not (tmp_path / "memory" / "cards" / "decay").exists()


def test_memory_care_readers_fall_back_to_legacy_decay_dir(tmp_path, capsys):
    legacy = tmp_path / "memory" / "cards" / "decay"
    legacy.mkdir(parents=True)
    legacy_queue = {
        "version": 1,
        "scan_date": "2026-06-01",
        "generated_at": "2026-06-01T00:00:00+00:00",
        "source": "memory-care",
        "cards": [],
    }
    (legacy / "refresh-queue.json").write_text(json.dumps(legacy_queue))
    (legacy / "scan-latest.json").write_text(
        json.dumps({"scan_date": "2026-06-01", "generated_at": "2026-06-01T00:00:00+00:00", "issues": []})
    )

    rc = memory_cmd.import_issues(target=tmp_path, dry_run=True, json_output=True)
    out = capsys.readouterr()
    assert rc == 0, out.err


def _git(tmp_path, *args, env=None):
    import os
    import subprocess

    base_env = {**os.environ, "GIT_AUTHOR_DATE": "2026-03-15T12:00:00", "GIT_COMMITTER_DATE": "2026-03-15T12:00:00"}
    if env:
        base_env.update(env)
    subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True, env=base_env)


def test_memory_care_backfill_dry_run_derives_dates_and_writes_nothing(tmp_path, capsys):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.invalid")
    _git(tmp_path, "config", "user.name", "t")
    cards = tmp_path / "memory" / "cards"
    _write_card(cards / "bare.md", {"topic": "bare", "confidence": "high", "evidence": ["README.md"]})
    _write_card(
        cards / "complete.md",
        {
            "topic": "complete",
            "id": "card-11111111-1111-4111-8111-111111111111",
            "last_reviewed": "2026-05-01",
            "fresh_until": "2026-12-01",
            "fingerprint": "already-set",
            "confidence": "high",
            "evidence": ["README.md"],
        },
    )
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "add cards")
    before = (cards / "bare.md").read_text()

    assert memory_cmd.backfill(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["apply"] is False
    assert payload["candidate_count"] == 1
    item = payload["candidates"][0]
    assert item["file"].endswith("bare.md")
    assert item["source"] == "git-history"
    assert item["last_reviewed"] == "2026-03-15"
    assert item["fresh_until"] == "2026-06-13"
    assert "fingerprint" in item["fields"]
    assert len(item["fingerprint"]) == 64
    assert (cards / "bare.md").read_text() == before
    receipt_path = tmp_path / ".brigade" / "memory-care" / "backfills" / "dry-run-mapping.json"
    assert receipt_path.is_file()
    receipt = json.loads(receipt_path.read_text())
    assert receipt["schema"] == "brigade.memory-identity-mapping.v1"
    assert receipt["dry_run"] is True
    assert receipt["missing_ids_validation"] == "warning"
    assert receipt["coverage"]["path_fallbacks"] >= 1
    dumped = json.dumps(receipt)
    assert "Body." not in dumped
    assert str(tmp_path) not in dumped
    assert all(item["path"].startswith("memory/cards/") for item in receipt["mappings"])
    assert payload["identity_receipt"]["mappings"] == receipt["mappings"]


def test_memory_care_backfill_apply_writes_metadata_receipt_and_is_idempotent(tmp_path, capsys):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.invalid")
    _git(tmp_path, "config", "user.name", "t")
    cards = tmp_path / "memory" / "cards"
    _write_card(cards / "bare.md", {"topic": "bare", "confidence": "high", "evidence": ["README.md"]})
    _write_card(
        cards / "complete.md",
        {
            "topic": "complete",
            "id": "card-11111111-1111-4111-8111-111111111111",
            "last_reviewed": "2026-05-01",
            "fresh_until": "2026-12-01",
            "fingerprint": "already-set",
            "confidence": "high",
            "evidence": ["README.md"],
        },
    )
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "add cards")
    complete_before = (cards / "complete.md").read_text()

    assert memory_cmd.backfill(target=tmp_path, apply=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["apply"] is True
    assert payload["written_count"] == 1
    text = (cards / "bare.md").read_text()
    assert "last_reviewed: 2026-03-15" in text
    assert "fresh_until: 2026-06-13" in text
    assert "fingerprint:" in text
    assert re.search(
        r"^id: card-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", text, re.MULTILINE
    )
    assert text.endswith("Body.\n")
    assert (cards / "complete.md").read_text() == complete_before
    receipts = list((tmp_path / ".brigade" / "memory-care" / "backfills").glob("*.json"))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text())
    assert receipt["written_count"] == 1
    assert receipt["identity_mapping"] == [{"file": "memory/cards/bare.md", "card_id": payload["candidates"][0]["id"]}]

    assert memory_cmd.backfill(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["candidate_count"] == 0


def test_memory_care_backfill_falls_back_to_mtime_outside_git(tmp_path, capsys):
    import os

    cards = tmp_path / "memory" / "cards"
    _write_card(cards / "bare.md", {"topic": "bare", "confidence": "high", "evidence": ["README.md"]})
    stamp = datetime(2026, 4, 2, 12, 0, tzinfo=timezone.utc).timestamp()
    os.utime(cards / "bare.md", (stamp, stamp))

    assert memory_cmd.backfill(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["candidate_count"] == 1
    item = payload["candidates"][0]
    assert item["source"] == "file-mtime"
    assert item["last_reviewed"] == "2026-04-02"


def test_memory_care_backfill_fingerprint_only_for_dated_cards(tmp_path, capsys):
    """Cards that already have dates still get a content fingerprint backfill."""
    cards = tmp_path / "memory" / "cards"
    _write_card(
        cards / "dated.md",
        {
            "topic": "dated",
            "last_reviewed": "2026-05-01",
            "fresh_until": "2026-12-01",
            "confidence": "high",
            "evidence": ["README.md"],
        },
        body="Durable fact about the widget cache.\n",
    )

    assert memory_cmd.backfill(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["candidate_count"] == 1
    item = payload["candidates"][0]
    assert item["fields"] == ["fingerprint", "id"]
    assert item["source"] == "content-hash"
    assert len(item["fingerprint"]) == 64

    assert memory_cmd.backfill(target=tmp_path, apply=True, json_output=True) == 0
    text = (cards / "dated.md").read_text()
    assert f"fingerprint: {item['fingerprint']}" in text
    assert re.search(
        r"^id: card-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", text, re.MULTILINE
    )
    assert "last_reviewed: 2026-05-01" in text


def test_memory_care_backfill_rejects_alias_collisions_without_writing(tmp_path, capsys):
    cards = tmp_path / "memory" / "cards"
    _write_card(cards / "one.md", {"topic": "shared", "confidence": "high", "evidence": ["README.md"]})
    _write_card(cards / "two.md", {"topic": "shared", "confidence": "high", "evidence": ["README.md"]})
    before = {path.name: path.read_text() for path in cards.glob("*.md")}

    assert memory_cmd.backfill(target=tmp_path, apply=True, json_output=True) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["identity_collisions"] == {"shared": ["memory/cards/one.md", "memory/cards/two.md"]}
    assert {path.name: path.read_text() for path in cards.glob("*.md")} == before
    receipt = payload["identity_receipt"]
    assert receipt["dry_run"] is True
    assert receipt["coverage"]["alias_collisions"] == 1
    assert str(tmp_path) not in json.dumps(receipt)


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform cannot create symlinks")
def test_memory_care_backfill_skips_symlinked_cards(tmp_path, capsys):
    victim = tmp_path / "outside" / "victim.md"
    victim.parent.mkdir(parents=True)
    victim.write_bytes(b"VICTIM_SECRET_BYTES\n")
    cards = tmp_path / "memory" / "cards"
    _write_card(cards / "real.md", {"topic": "real", "confidence": "high", "evidence": ["README.md"]})
    (cards / "link.md").symlink_to(victim)

    assert memory_cmd.backfill(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    files = {item["file"] for item in payload["candidates"]}
    assert any(path.endswith("real.md") for path in files)
    assert not any(path.endswith("link.md") for path in files)
    assert victim.read_bytes() == b"VICTIM_SECRET_BYTES\n"


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform cannot create symlinks")
def test_memory_care_backfill_apply_preserves_outside_file(tmp_path, capsys):
    victim = tmp_path / "outside" / "victim.md"
    victim.parent.mkdir(parents=True)
    victim_bytes = ('---\ntopic: victim\nconfidence: high\nevidence: ["README.md"]\n---\n\nVictim body.\n').encode(
        "utf-8"
    )
    victim.write_bytes(victim_bytes)
    cards = tmp_path / "memory" / "cards"
    cards.mkdir(parents=True, exist_ok=True)
    (cards / "link.md").symlink_to(victim)

    assert memory_cmd.backfill(target=tmp_path, json_output=True) == 0
    assert victim.read_bytes() == victim_bytes
    capsys.readouterr()

    assert memory_cmd.backfill(target=tmp_path, apply=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["written_count"] == 0
    assert victim.read_bytes() == victim_bytes


def test_memory_care_backfill_dry_run_mapping_predicts_apply(tmp_path, capsys):
    """Two dry runs and --apply must emit the same new_id (probe #984)."""
    cards = tmp_path / "memory" / "cards"
    _write_card(
        cards / "incomplete-legacy.md",
        {"title": "Incomplete Legacy", "topic": "legacy-topic-key", "confidence": "high", "evidence": ["README.md"]},
    )

    assert memory_cmd.backfill(target=tmp_path, json_output=True) == 0
    first = json.loads(capsys.readouterr().out)
    assert memory_cmd.backfill(target=tmp_path, json_output=True) == 0
    second = json.loads(capsys.readouterr().out)
    first_id = first["candidates"][0]["id"]
    assert first_id == second["candidates"][0]["id"]
    assert first["identity_receipt"]["mappings"] == second["identity_receipt"]["mappings"]
    assert first["identity_receipt"]["mappings"][0]["old_id"] == "legacy-topic-key"
    assert first["identity_receipt"]["mappings"][0]["new_id"] == first_id

    assert memory_cmd.backfill(target=tmp_path, apply=True, json_output=True) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["candidates"][0]["id"] == first_id
    written = (cards / "incomplete-legacy.md").read_text()
    assert f"id: {first_id}" in written
    assert written.count("id:") == 1


def test_memory_care_backfill_mints_id_on_complete_idless_card(tmp_path, capsys):
    """Complete care frontmatter without an ID must still be mintable (probe #984)."""
    cards = tmp_path / "memory" / "cards"
    _write_card(
        cards / "complete-legacy.md",
        {
            "title": "Complete Legacy",
            "topic": "complete-topic",
            "last_reviewed": "2026-01-01",
            "fresh_until": "2026-04-01",
            "fingerprint": "already-set",
            "confidence": "high",
            "evidence": ["README.md"],
        },
    )
    _write_card(
        cards / "incomplete-legacy.md",
        {"title": "Incomplete Legacy", "topic": "incomplete-topic", "confidence": "high", "evidence": ["README.md"]},
    )

    assert memory_cmd.backfill(target=tmp_path, apply=True, json_output=True) == 0
    applied = json.loads(capsys.readouterr().out)
    files = {item["file"].split("/")[-1] for item in applied["candidates"]}
    assert files == {"complete-legacy.md", "incomplete-legacy.md"}
    complete_item = next(item for item in applied["candidates"] if item["file"].endswith("complete-legacy.md"))
    assert complete_item["fields"] == ["id"]
    assert complete_item["old_id"] == "complete-topic"
    assert (cards / "complete-legacy.md").read_text().count("id:") == 1

    assert memory_cmd.backfill(target=tmp_path, json_output=True) == 0
    follow_up = json.loads(capsys.readouterr().out)
    assert follow_up["candidate_count"] == 0
    assert follow_up["identity_receipt"]["coverage"]["path_fallbacks"] == 0
    assert follow_up["identity_receipt"]["missing_ids_validation"] == "enabled"


def test_memory_care_backfill_never_replaces_valid_existing_id(tmp_path, capsys):
    """A valid explicit ID plus a missing care field must keep exactly one id: (probe #984)."""
    cards = tmp_path / "memory" / "cards"
    card_id = "card-77777777-7777-4777-8777-777777777777"
    _write_card(
        cards / "has-id.md",
        {
            "title": "Has A Valid Id",
            "id": card_id,
            "last_reviewed": "2026-01-01",
            "confidence": "high",
            "evidence": ["README.md"],
        },
    )

    assert memory_cmd.backfill(target=tmp_path, json_output=True) == 0
    planned = json.loads(capsys.readouterr().out)
    item = planned["candidates"][0]
    assert "id" not in item["fields"]
    assert "id" not in item

    assert memory_cmd.backfill(target=tmp_path, apply=True, json_output=True) == 0
    text = (cards / "has-id.md").read_text()
    assert text.count("id:") == 1
    assert f"id: {card_id}" in text
    assert "fresh_until:" in text
    assert "fingerprint:" in text


def test_memory_care_backfill_dry_run_skips_receipt_when_no_candidates(tmp_path, capsys):
    """A zero-candidate dry run must not create .brigade/ (probe #984)."""
    cards = tmp_path / "memory" / "cards"
    _write_card(
        cards / "complete.md",
        {
            "topic": "complete",
            "id": "card-11111111-1111-4111-8111-111111111111",
            "last_reviewed": "2026-05-01",
            "fresh_until": "2026-12-01",
            "fingerprint": "already-set",
            "confidence": "high",
            "evidence": ["README.md"],
        },
    )

    assert memory_cmd.backfill(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["candidate_count"] == 0
    assert payload["receipt_path"] is None
    assert not (tmp_path / ".brigade").exists()


def test_memory_care_backfill_counts_duplicate_ids_without_namespace(tmp_path, capsys):
    """duplicate_ids must fire without memory/NAMESPACE (probe #984)."""
    cards = tmp_path / "memory" / "cards"
    shared = "card-99999999-9999-4999-8999-999999999999"
    _write_card(cards / "one.md", {"id": shared, "topic": "one", "confidence": "high", "evidence": ["README.md"]})
    _write_card(cards / "two.md", {"id": shared, "topic": "two", "confidence": "high", "evidence": ["README.md"]})
    _write_card(
        cards / "other.md",
        {
            "id": "card-11111111-1111-4111-8111-111111111111",
            "topic": "other",
            "confidence": "high",
            "evidence": ["README.md"],
        },
    )
    assert not (tmp_path / "memory" / "NAMESPACE").exists()

    assert memory_cmd.backfill(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    coverage = payload["identity_receipt"]["coverage"]
    assert coverage["alias_collisions"] == 1
    assert coverage["duplicate_ids"] == 1


def test_memory_care_producer_artifacts_use_write_dir_not_read_fallback(tmp_path):
    legacy = tmp_path / "memory" / "cards" / "decay"
    legacy.mkdir(parents=True)
    (legacy / "scan-latest.json").write_text('{"scan_date": "2026-06-01", "counts": {}}')

    config = memory_cmd.MemoryCareConfig()
    artifacts = memory_cmd.memory_care_producer_artifacts(tmp_path, config)

    assert artifacts == [
        (tmp_path / ".brigade" / "memory-care" / "decay" / "scan-latest.json", "brigade memory-care"),
        (tmp_path / ".brigade" / "memory-care" / "decay" / "refresh-queue.json", "brigade memory-care"),
    ]


def test_memory_care_producer_artifacts_honor_custom_output_path(tmp_path):
    config = memory_cmd.MemoryCareConfig(output_path=".brigade/memory-care/custom-decay")
    artifacts = memory_cmd.memory_care_producer_artifacts(tmp_path, config)

    assert artifacts == [
        (tmp_path / ".brigade" / "memory-care" / "custom-decay" / "scan-latest.json", "brigade memory-care"),
        (tmp_path / ".brigade" / "memory-care" / "custom-decay" / "refresh-queue.json", "brigade memory-care"),
    ]


def _write_archive_candidate_care_config(tmp_path, *, stale_after_days=30):
    config = tmp_path / ".brigade" / "memory-care.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        "\n".join(
            [
                'card_roots = ["memory/cards"]',
                'index_paths = ["MEMORY.md"]',
                f"stale_after_days = {stale_after_days}",
                "expiry_warning_days = 0",
                'minimum_confidence = "medium"',
                "require_evidence = true",
                "include_paths = []",
                'exclude_paths = ["memory/cards/decay"]',
                'output_path = ".brigade/memory-care/decay"',
                "",
            ]
        )
    )
    return config


def _snapshot_tree(root):
    snapshot = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            snapshot[str(path.relative_to(root))] = path.read_bytes()
    return snapshot


def test_memory_care_status_reports_archive_candidates_from_scan_payload_without_rescan(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(memory_cmd, "_today", lambda: date(2026, 5, 28))
    _write_archive_candidate_care_config(tmp_path, stale_after_days=30)
    cards = tmp_path / "memory" / "cards"
    receipt = tmp_path / ".brigade" / "work" / "verify-runs" / "verify-archive" / "receipt.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text(json.dumps({"status": "completed"}))
    _write_card(
        cards / "past-threshold.md",
        {
            "topic": "past-threshold",
            "last_reviewed": "2026-03-28",
            "fresh_until": "2026-12-01",
            "confidence": "high",
            "evidence": [".brigade/work/verify-runs/verify-archive/receipt.json"],
        },
    )
    (tmp_path / "MEMORY.md").write_text("- [past-threshold](memory/cards/past-threshold.md)\n")

    assert memory_cmd.scan(target=tmp_path, json_output=True) == 0
    capsys.readouterr()

    def _fail_iter_cards(*_args, **_kwargs):
        raise AssertionError("status must reuse scan-latest.json without rescanning cards")

    monkeypatch.setattr(memory_cmd, "_iter_cards", _fail_iter_cards)

    assert memory_cmd.status(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    archive = payload["archive_candidates"]
    assert archive["schema_version"] == 1
    assert archive["read_only"] is True
    assert archive["approval_required"] is True
    assert archive["would_archive"] is False
    assert archive["scan_date"] == "2026-05-28"
    assert archive["threshold"] == {
        "ttl_days": 30,
        "candidate_after_days": 60,
        "comparison": "age_days > candidate_after_days",
    }
    assert isinstance(archive["candidate_count"], int)
    assert isinstance(archive["unassessable_count"], int)
    assert isinstance(archive["candidates"], list)
    assert archive["candidate_count"] == 1
    assert archive["candidate_count"] == len(archive["candidates"])
    candidate = archive["candidates"][0]
    assert isinstance(candidate["card_id"], str)
    assert isinstance(candidate["file"], str)
    assert isinstance(candidate["age_days"], int)
    assert isinstance(candidate["last_reviewed"], str)
    assert candidate["fresh_until"] is None or isinstance(candidate["fresh_until"], str)
    assert isinstance(candidate["evidence_pointers"], list)
    assert all(isinstance(item, str) for item in candidate["evidence_pointers"])
    assert isinstance(candidate["reason"], str)
    assert isinstance(candidate["approval_required"], bool)
    assert candidate["card_id"] == "past-threshold"
    assert candidate["file"] == "memory/cards/past-threshold.md"
    assert candidate["age_days"] == 61
    assert candidate["last_reviewed"] == "2026-03-28"
    assert candidate["fresh_until"] == "2026-12-01"
    assert candidate["reason"] == "review_age_exceeds_2x_ttl"
    assert candidate["approval_required"] is True


def test_memory_care_status_archive_candidate_threshold_excludes_exactly_two_ttl(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(memory_cmd, "_today", lambda: date(2026, 5, 28))
    _write_archive_candidate_care_config(tmp_path, stale_after_days=30)
    cards = tmp_path / "memory" / "cards"
    _write_card(
        cards / "just-under.md",
        {
            "topic": "just-under",
            "last_reviewed": "2026-03-30",
            "fresh_until": "2026-12-01",
            "confidence": "high",
            "evidence": ["README.md"],
        },
    )
    _write_card(
        cards / "exactly-two-ttl.md",
        {
            "topic": "exactly-two-ttl",
            "last_reviewed": "2026-03-29",
            "fresh_until": "2026-12-01",
            "confidence": "high",
            "evidence": ["README.md"],
        },
    )
    _write_card(
        cards / "just-over.md",
        {
            "topic": "just-over",
            "last_reviewed": "2026-03-28",
            "fresh_until": "2026-12-01",
            "confidence": "high",
            "evidence": ["README.md"],
        },
    )
    _write_card(
        cards / "missing-reviewed.md",
        {
            "topic": "missing-reviewed",
            "fresh_until": "2026-12-01",
            "confidence": "high",
            "evidence": ["README.md"],
        },
    )
    _write_card(
        cards / "invalid-reviewed.md",
        {
            "topic": "invalid-reviewed",
            "last_reviewed": "not-a-date",
            "fresh_until": "2026-12-01",
            "confidence": "high",
            "evidence": ["README.md"],
        },
    )
    _write_card(
        cards / "future-reviewed.md",
        {
            "topic": "future-reviewed",
            "last_reviewed": "2026-06-01",
            "fresh_until": "2026-12-01",
            "confidence": "high",
            "evidence": ["README.md"],
        },
    )
    (tmp_path / "MEMORY.md").write_text(
        "\n".join(
            [
                "- [just-under](memory/cards/just-under.md)",
                "- [exactly-two-ttl](memory/cards/exactly-two-ttl.md)",
                "- [just-over](memory/cards/just-over.md)",
                "- [missing-reviewed](memory/cards/missing-reviewed.md)",
                "- [invalid-reviewed](memory/cards/invalid-reviewed.md)",
                "- [future-reviewed](memory/cards/future-reviewed.md)",
            ]
        )
        + "\n"
    )

    assert memory_cmd.scan(target=tmp_path, json_output=True) == 0
    capsys.readouterr()
    assert memory_cmd.status(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    archive = payload["archive_candidates"]
    assert archive["threshold"]["ttl_days"] == 30
    assert archive["threshold"]["candidate_after_days"] == 60
    assert archive["threshold"]["comparison"] == "age_days > candidate_after_days"
    candidate_ids = {item["card_id"] for item in archive["candidates"]}
    assert candidate_ids == {"just-over"}
    assert archive["candidate_count"] == 1
    assert archive["candidates"][0]["age_days"] == 61
    assert "just-under" not in candidate_ids
    assert "exactly-two-ttl" not in candidate_ids
    assert "missing-reviewed" not in candidate_ids
    assert "invalid-reviewed" not in candidate_ids
    assert "future-reviewed" not in candidate_ids
    assert archive["unassessable_count"] == 3


def test_memory_care_status_archive_candidates_include_age_reviewed_and_evidence_pointers(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(memory_cmd, "_today", lambda: date(2026, 5, 28))
    cache_home = tmp_path / "cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_home))
    _write_archive_candidate_care_config(tmp_path, stale_after_days=30)
    receipt = tmp_path / ".brigade" / "work" / "verify-runs" / "verify-one" / "receipt.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text(json.dumps({"status": "completed"}))
    bundle = cache_home / "miseledger" / "evidence" / "bundle-ok.json"
    bundle.parent.mkdir(parents=True)
    bundle.write_text(json.dumps({"id": "bundle-ok"}))
    cards = tmp_path / "memory" / "cards"
    evidence = [
        ".brigade/work/verify-runs/verify-one/receipt.json",
        "miseledger://evidence/bundle-ok",
        "README.md",
    ]
    _write_card(
        cards / "aged.md",
        {
            "topic": "aged",
            "last_reviewed": "2026-03-28",
            "fresh_until": "2026-11-15",
            "confidence": "high",
            "evidence": evidence,
        },
    )
    (tmp_path / "MEMORY.md").write_text("- [aged](memory/cards/aged.md)\n")

    assert memory_cmd.scan(target=tmp_path, json_output=True) == 0
    scan_payload = json.loads(capsys.readouterr().out)
    scan_card = next(card for card in scan_payload["cards"] if card["card_id"] == "aged")
    assert scan_card["evidence_pointers"] == evidence

    assert memory_cmd.status(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    candidate = payload["archive_candidates"]["candidates"][0]
    assert candidate["card_id"] == "aged"
    assert candidate["file"] == "memory/cards/aged.md"
    assert candidate["age_days"] == 61
    assert candidate["last_reviewed"] == "2026-03-28"
    assert candidate["fresh_until"] == "2026-11-15"
    assert candidate["evidence_pointers"] == evidence
    assert candidate["reason"] == "review_age_exceeds_2x_ttl"
    assert candidate["approval_required"] is True


def test_memory_care_status_archive_candidates_are_read_only(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(memory_cmd, "_today", lambda: date(2026, 5, 28))
    _write_archive_candidate_care_config(tmp_path, stale_after_days=30)
    cards = tmp_path / "memory" / "cards"
    card_path = cards / "old.md"
    _write_card(
        card_path,
        {
            "topic": "old",
            "last_reviewed": "2026-03-28",
            "fresh_until": "2026-12-01",
            "confidence": "high",
            "evidence": ["README.md"],
        },
    )
    (tmp_path / "MEMORY.md").write_text("- [old](memory/cards/old.md)\n")

    assert memory_cmd.scan(target=tmp_path, json_output=True) == 0
    capsys.readouterr()
    before = _snapshot_tree(tmp_path)
    assert before
    assert "memory/cards/old.md" in before
    assert ".brigade/memory-care/decay/scan-latest.json" in before
    assert ".brigade/memory-care/decay/refresh-queue.json" in before

    assert memory_cmd.status(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    archive = payload["archive_candidates"]
    assert archive["read_only"] is True
    assert archive["would_archive"] is False
    assert archive["approval_required"] is True
    assert archive["candidate_count"] == 1

    after = _snapshot_tree(tmp_path)
    assert after == before
    assert not any(path.name.endswith(".archived") or path.name.endswith(".bak") for path in tmp_path.rglob("*"))
    assert not (tmp_path / "memory" / "cards" / "archive").exists()
    assert list((tmp_path / ".brigade" / "memory-care").rglob("closeout.json")) == []


def test_memory_care_status_archive_candidate_threshold_uses_two_times_ttl_not_plus_thirty(
    tmp_path, monkeypatch, capsys
):
    """ttl*2 and ttl+30 collide when ttl=30; use ttl=45 so the formulas diverge."""
    monkeypatch.setattr(memory_cmd, "_today", lambda: date(2026, 5, 28))
    _write_archive_candidate_care_config(tmp_path, stale_after_days=45)
    cards = tmp_path / "memory" / "cards"
    # age 80: between (45+30=75) and (45*2=90) — candidate only under the wrong formula
    _write_card(
        cards / "between-formulas.md",
        {
            "topic": "between-formulas",
            "last_reviewed": "2026-03-09",
            "fresh_until": "2026-12-01",
            "confidence": "high",
            "evidence": ["README.md"],
        },
    )
    # age 90: exactly 2x TTL — not a candidate (strict >)
    _write_card(
        cards / "exactly-two-ttl.md",
        {
            "topic": "exactly-two-ttl",
            "last_reviewed": "2026-02-27",
            "fresh_until": "2026-12-01",
            "confidence": "high",
            "evidence": ["README.md"],
        },
    )
    # age 91: past 2x TTL — candidate
    _write_card(
        cards / "past-two-ttl.md",
        {
            "topic": "past-two-ttl",
            "last_reviewed": "2026-02-26",
            "fresh_until": "2026-12-01",
            "confidence": "high",
            "evidence": ["README.md"],
        },
    )
    (tmp_path / "MEMORY.md").write_text(
        "\n".join(
            [
                "- [between-formulas](memory/cards/between-formulas.md)",
                "- [exactly-two-ttl](memory/cards/exactly-two-ttl.md)",
                "- [past-two-ttl](memory/cards/past-two-ttl.md)",
            ]
        )
        + "\n"
    )

    assert memory_cmd.scan(target=tmp_path, json_output=True) == 0
    capsys.readouterr()
    assert memory_cmd.status(target=tmp_path, json_output=True) == 0
    archive = json.loads(capsys.readouterr().out)["archive_candidates"]
    assert archive["threshold"]["ttl_days"] == 45
    assert archive["threshold"]["candidate_after_days"] == 90
    assert archive["threshold"]["candidate_after_days"] != 45 + 30
    by_id = {item["card_id"]: item for item in archive["candidates"]}
    assert set(by_id) == {"past-two-ttl"}
    assert by_id["past-two-ttl"]["age_days"] == 91
    assert "between-formulas" not in by_id
    assert "exactly-two-ttl" not in by_id


def test_memory_care_status_archive_candidates_age_from_saved_scan_date_not_today(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(memory_cmd, "_today", lambda: date(2026, 5, 28))
    _write_archive_candidate_care_config(tmp_path, stale_after_days=30)
    cards = tmp_path / "memory" / "cards"
    # At scan_date 2026-05-28, age=57 (<=60) so not a candidate.
    # If status wrongly used a later "today", age would cross the threshold.
    _write_card(
        cards / "scan-anchored.md",
        {
            "topic": "scan-anchored",
            "last_reviewed": "2026-04-01",
            "fresh_until": "2026-12-01",
            "confidence": "high",
            "evidence": ["README.md"],
        },
    )
    (tmp_path / "MEMORY.md").write_text("- [scan-anchored](memory/cards/scan-anchored.md)\n")

    assert memory_cmd.scan(target=tmp_path, json_output=True) == 0
    scan_payload = json.loads(capsys.readouterr().out)
    assert scan_payload["scan_date"] == "2026-05-28"

    monkeypatch.setattr(memory_cmd, "_today", lambda: date(2026, 7, 1))
    assert memory_cmd.status(target=tmp_path, json_output=True) == 0
    archive = json.loads(capsys.readouterr().out)["archive_candidates"]
    assert archive["scan_date"] == "2026-05-28"
    assert archive["candidate_count"] == 0
    assert archive["candidates"] == []


def test_memory_care_status_prints_archive_candidates_in_human_output(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(memory_cmd, "_today", lambda: date(2026, 5, 28))
    _write_archive_candidate_care_config(tmp_path, stale_after_days=30)
    cards = tmp_path / "memory" / "cards"
    _write_card(
        cards / "human-old.md",
        {
            "topic": "human-old",
            "last_reviewed": "2026-03-28",
            "fresh_until": "2026-12-01",
            "confidence": "high",
            "evidence": ["README.md", "TOOLS.md"],
        },
    )
    (tmp_path / "MEMORY.md").write_text("- [human-old](memory/cards/human-old.md)\n")

    assert memory_cmd.scan(target=tmp_path, json_output=True) == 0
    capsys.readouterr()

    assert memory_cmd.status(target=tmp_path, json_output=False) == 0
    out = capsys.readouterr().out
    assert "memory care status:" in out
    assert (
        "archive_candidates: count=1 unassessable=0 threshold=age_days>60 approval_required=True would_archive=False"
    ) in out
    assert (
        "  archive_candidate: memory/cards/human-old.md "
        "age_days=61 last_reviewed=2026-03-28 evidence=README.md,TOOLS.md"
    ) in out


def test_memory_care_archive_candidates_unassessable_when_saved_scan_date_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_cmd, "_today", lambda: date(2026, 8, 1))
    _write_archive_candidate_care_config(tmp_path, stale_after_days=30)
    config = memory_cmd.load_config(tmp_path)
    assert config is not None
    scan_dir = tmp_path / ".brigade" / "memory-care" / "decay"
    scan_dir.mkdir(parents=True)
    (scan_dir / "scan-latest.json").write_text(
        json.dumps(
            {
                "cards": [
                    {
                        "card_id": "would-be-candidate",
                        "file": "memory/cards/would-be-candidate.md",
                        "last_reviewed": "2026-03-28",
                        "fresh_until": "2026-12-01",
                    },
                    {
                        "card_id": "missing-reviewed",
                        "file": "memory/cards/missing-reviewed.md",
                        "fresh_until": "2026-12-01",
                    },
                ],
            }
        )
    )
    archive = memory_cmd._archive_candidates_from_scan(
        memory_cmd._load_scan_payload(tmp_path, config),
        config,
    )
    assert archive["scan_date"] is None
    assert archive["candidate_count"] == 0
    assert archive["candidates"] == []
    assert archive["unassessable_count"] == 2


def test_memory_care_archive_candidates_from_legacy_scan_without_evidence_pointers(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_cmd, "_today", lambda: date(2026, 8, 1))
    _write_archive_candidate_care_config(tmp_path, stale_after_days=30)
    config = memory_cmd.load_config(tmp_path)
    assert config is not None
    scan_dir = tmp_path / ".brigade" / "memory-care" / "decay"
    scan_dir.mkdir(parents=True)
    (scan_dir / "scan-latest.json").write_text(
        json.dumps(
            {
                "scan_date": "2026-05-28",
                "cards": [
                    {
                        "card_id": "legacy",
                        "file": "memory/cards/legacy.md",
                        "last_reviewed": "2026-03-28",
                        "fresh_until": "2026-12-01",
                    }
                ],
            }
        )
    )
    archive = memory_cmd._archive_candidates_from_scan(
        memory_cmd._load_scan_payload(tmp_path, config),
        config,
    )
    assert archive["scan_date"] == "2026-05-28"
    assert archive["candidate_count"] == 1
    candidate = archive["candidates"][0]
    assert candidate["card_id"] == "legacy"
    assert candidate["age_days"] == 61
    assert candidate["evidence_pointers"] == []
    assert candidate["approval_required"] is True


def test_memory_care_bundled_runbook_templates_are_valid():
    """Packaged templates must stay mechanical, brigade-only, and model-free (#760)."""
    template_dir = _bundled_memory_care_runbook_dir()
    assert sorted(path.name for path in template_dir.glob("*.json")) == sorted(MEMORY_CARE_RUNBOOK_NAMES)

    for name in MEMORY_CARE_RUNBOOK_NAMES:
        path = template_dir / name
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload.get("id") == Path(name).stem
        _assert_mechanical_template_shape(payload, expect_pins=False)

        validated, error = runbook_cmd._read_runbook(path, require_pin_hashes=False)
        assert validated is not None, error
        assert error is None

        runs = [step["run"] for step in payload["steps"]]
        for fragment in EXPECTED_RUNBOOK_STEP_FRAGMENTS[name]:
            assert any(fragment in run for run in runs), f"{name} missing step containing {fragment!r}"


def test_memory_care_init_without_runbooks_writes_no_templates(tmp_path):
    assert memory_cmd.init(target=tmp_path, force=True) == 0
    runbook_dir = _memory_care_runbook_dir(tmp_path)
    assert not runbook_dir.exists() or list(runbook_dir.glob("*.json")) == []


def test_memory_care_init_with_runbooks_pin_failure_leaves_no_config_or_partial_runbooks(tmp_path, monkeypatch, capsys):
    def _fail_pin(_target, _payload):
        return None, "runbook step 1 argv[0] could not be resolved: brigade"

    monkeypatch.setattr(runbook_cmd, "_pin_payload_from_runbook", _fail_pin)

    assert memory_cmd.init(target=tmp_path, with_runbooks=True) == 2
    assert "could not be resolved" in capsys.readouterr().err

    assert not (tmp_path / ".brigade" / "memory-care.toml").exists()
    runbook_dir = _memory_care_runbook_dir(tmp_path)
    assert not runbook_dir.exists() or list(runbook_dir.glob("*.json")) == []


def test_memory_care_force_reinit_pin_failure_preserves_existing_runbooks_and_config(tmp_path, monkeypatch, capsys):
    assert memory_cmd.init(target=tmp_path, force=True, with_runbooks=True) == 0
    capsys.readouterr()

    config_path = tmp_path / ".brigade" / "memory-care.toml"
    config_path.write_text(config_path.read_text(encoding="utf-8") + "# user marker\n", encoding="utf-8")
    saved_config = config_path.read_bytes()
    saved_runbooks = {
        name: (_memory_care_runbook_dir(tmp_path) / name).read_bytes() for name in MEMORY_CARE_RUNBOOK_NAMES
    }

    def _fail_pin(_target, _payload):
        return None, "runbook step 1 argv[0] could not be resolved: brigade"

    monkeypatch.setattr(runbook_cmd, "_pin_payload_from_runbook", _fail_pin)

    assert memory_cmd.init(target=tmp_path, force=True, with_runbooks=True) == 2
    assert "could not be resolved" in capsys.readouterr().err

    assert config_path.read_bytes() == saved_config
    for name, payload in saved_runbooks.items():
        assert (_memory_care_runbook_dir(tmp_path) / name).read_bytes() == payload


def test_memory_care_init_with_runbooks_writes_three_pinned_templates(tmp_path, capsys):
    assert memory_cmd.init(target=tmp_path, force=True, with_runbooks=True) == 0
    captured = capsys.readouterr()
    assert "memory_care_runbooks:" in captured.out

    runbook_dir = _memory_care_runbook_dir(tmp_path)
    assert sorted(path.name for path in runbook_dir.glob("*.json")) == sorted(MEMORY_CARE_RUNBOOK_NAMES)

    runbooks = _load_memory_care_runbooks(tmp_path)
    for name, payload in runbooks.items():
        _assert_mechanical_brigade_only_runbook(payload)
        for fragment in EXPECTED_RUNBOOK_STEP_FRAGMENTS[name]:
            assert any(fragment in step["run"] for step in payload["steps"])

    for name in MEMORY_CARE_RUNBOOK_NAMES:
        assert runbook_cmd.plan(target=tmp_path, runbook=runbook_dir / name, json_output=True) == 0
        plan = json.loads(capsys.readouterr().out)
        assert plan["policy_valid"] is True


def test_memory_care_init_with_runbooks_is_idempotent(tmp_path):
    assert memory_cmd.init(target=tmp_path, force=True, with_runbooks=True) == 0
    first = {name: (_memory_care_runbook_dir(tmp_path) / name).read_bytes() for name in MEMORY_CARE_RUNBOOK_NAMES}

    assert memory_cmd.init(target=tmp_path, force=True, with_runbooks=True) == 0
    second = {name: (_memory_care_runbook_dir(tmp_path) / name).read_bytes() for name in MEMORY_CARE_RUNBOOK_NAMES}

    assert first == second


def test_memory_care_init_parser_exposes_with_runbooks_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["memory", "care", "init", "--help"])
    assert exc.value.code == 0
    assert "--with-runbooks" in capsys.readouterr().out

    parser = cli._build_parser()
    ns = parser.parse_args(["memory", "care", "init", "--with-runbooks"])
    assert ns.with_runbooks is True
    ns_default = parser.parse_args(["memory", "care", "init"])
    assert ns_default.with_runbooks is False
