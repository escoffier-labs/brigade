import json
from datetime import date, datetime, timezone

from brigade import cli
from brigade import dogfood_cmd
from brigade import memory_cmd
from brigade import work_cmd


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

    assert memory_cmd.doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "memory care doctor:" in out
    assert "memory_care_config" in out


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
        {"topic": "expired", "last_reviewed": "2026-05-20", "fresh_until": "2026-05-01", "confidence": "high", "evidence": ["README.md"]},
    )
    _write_card(cards / "undersourced.md", {"topic": "undersourced", "last_reviewed": "2026-05-20", "confidence": "low"})
    _write_card(cards / "orphan.md", {"topic": "orphan", "last_reviewed": "2026-05-20", "confidence": "high", "evidence": ["README.md"]})
    _write_card(cards / "dup-a.md", {"topic": "duplicate", "last_reviewed": "2026-05-20", "confidence": "high", "evidence": ["README.md"]})
    _write_card(cards / "dup-b.md", {"topic": "duplicate", "last_reviewed": "2026-05-20", "confidence": "high", "evidence": ["README.md"]})
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
        {"topic": "stale", "last_reviewed": "2026-01-01", "fresh_until": "2026-12-01", "confidence": "high", "evidence": ["README.md"]},
    )
    _write_card(
        cards / "expired.md",
        {"topic": "expired", "last_reviewed": "2026-05-01", "fresh_until": "2026-05-01", "confidence": "high", "evidence": ["README.md"]},
    )
    _write_card(cards / "undersourced.md", {"topic": "undersourced", "last_reviewed": "2026-05-01", "fresh_until": "2026-12-01", "confidence": "low"})
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
    queue = json.loads((tmp_path / "memory" / "cards" / "decay" / "refresh-queue.json").read_text())
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

    assert memory_cmd.import_issues(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    imported_types = {item["metadata"]["issue_type"] for item in payload["imports"]}
    assert "missing-reviewed" in imported_types
    assert "missing-freshness" in imported_types


def test_memory_care_imports_dedupe_and_respect_dismissed(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(memory_cmd, "_today", lambda: date(2026, 5, 28))
    monkeypatch.setattr(work_cmd, "_now", lambda: datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc))
    cards = tmp_path / "memory" / "cards"
    _write_card(cards / "stale.md", {"topic": "stale", "last_reviewed": "2026-01-01", "fresh_until": "2026-12-01", "confidence": "high", "evidence": ["README.md"]})
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
    monkeypatch.setattr(work_cmd, "_now", lambda: next(times))
    cards = tmp_path / "memory" / "cards"
    _write_card(cards / "stale.md", {"topic": "stale", "last_reviewed": "2026-01-01", "fresh_until": "2026-12-01", "confidence": "high", "evidence": ["README.md"]})
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
        (run_dir / "run.json").write_text(json.dumps({"started_at": "2026-05-28T12:10:00Z", "status": "ok", "task": task}))
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
    monkeypatch.setattr(memory_cmd, "status", fake_status)
    monkeypatch.setattr(memory_cmd, "doctor", fake_doctor)
    monkeypatch.setattr(memory_cmd, "import_issues", fake_import)
    assert cli.main(["memory", "care", "init", "--target", str(tmp_path), "--force", "--no-gitignore"]) == 0
    assert cli.main(["memory", "care", "scan", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["memory", "care", "status", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["memory", "care", "doctor", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["memory", "care", "import-issues", "--target", str(tmp_path), "--dry-run", "--json"]) == 0
    assert seen == [
        ("init", {"target": tmp_path, "force": True, "update_gitignore": False}),
        ("scan", {"target": tmp_path, "json_output": True}),
        ("status", {"target": tmp_path, "json_output": True}),
        ("doctor", {"target": tmp_path, "json_output": True}),
        ("import", {"target": tmp_path, "dry_run": True, "json_output": True}),
    ]
