from pathlib import Path

from brigade.center_cmd.dashboard.views import status_grid


def test_fetch_uses_work_brief_json(monkeypatch, tmp_path):
    expected = {"handoff_issues": {"count": 2}}
    calls: list[tuple[Path, list[str], float | None]] = []

    def fake_run_json(target: Path, args: list[str], *, timeout: float | None = None) -> dict:
        calls.append((target, args, timeout))
        return expected

    monkeypatch.setattr("brigade.center_cmd.dashboard.data.run_json", fake_run_json)

    assert status_grid.fetch(tmp_path) == expected
    assert calls == [(tmp_path, ["work", "brief"], 60.0)]


def test_docstring_states_operator_question():
    doc = status_grid.__doc__ or ""
    assert "attention" in doc.lower()
    for topic in ("handoffs", "memory", "verify loop", "inbox hygiene"):
        assert topic in doc.lower()


def test_render_shows_summary_strip_and_plain_word_tiles():
    payload = {
        "handoff_issues": {"count": 2, "known_count": 3, "total_count": 5},
        "memory_care": {"issue_count": 0},
        "outcome_loop": {
            "verify_run_count": 12,
            "record_count": 9,
            "eligible_receipt_count": 7,
            "ineligible_receipt_count": 1,
        },
        "pending_tasks": [{"id": "task-a"}, {"id": "task-b"}],
        "inbox_hygiene": {"issue_count": 1},
    }

    fragment = status_grid.render(payload, "status-nonce")

    # The stylesheet must carry the CSP nonce or the browser refuses it.
    assert '<style nonce="status-nonce">' in fragment
    assert "<style>" not in fragment
    # Chip colors live in the nonce'd stylesheet, not style= attributes (CSP blocks those).
    assert "style=" not in fragment.split("<style", 1)[-1].split("</style>", 1)[-1]
    assert 'class="sg-chip-icon sg-chip-warning"' in fragment
    assert 'class="sg-chip-icon sg-chip-good"' in fragment
    assert ".sg-chip-good { background: #0ca30c; }" in fragment
    assert ".sg-chip-warning { background: #fab219; }" in fragment
    assert ".sg-chip-serious { background: #ec835a; }" in fragment
    assert 'data-sg-summary="1"' in fragment
    assert "new handoff issues are waiting" in fragment
    assert "No cards are waiting on care review" in fragment
    assert "The work inbox has" in fragment

    for label in (
        "Handoffs",
        "Memory care",
        "Scheduled care",
        "Verify loop",
        "Inbox hygiene",
        "Pending tasks",
    ):
        assert label in fragment
    assert "No care entries are failing repeatedly" in fragment
    # Plain words, never color alone.
    for word in ("ATTENTION", "OK"):
        assert word in fragment
    assert "MISSING" not in fragment

    assert "Raw ids" in fragment
    assert "task-a" in fragment
    assert "Latest verify receipt" not in fragment
    assert "Latest verify signal" not in fragment


def test_render_missing_sections_show_missing_tiles():
    fragment = status_grid.render({"handoff_issues": {"count": 0}}, "unused")
    assert "MISSING" in fragment
    assert "not available" in fragment or "unavailable" in fragment
    assert 'class="sg-chip-icon sg-chip-serious"' in fragment
    assert "style=" not in fragment.split("<style", 1)[-1].split("</style>", 1)[-1]


def test_render_surfaces_repeated_scheduled_care_failures():
    payload = {
        "handoff_issues": {"count": 0},
        "memory_care": {"issue_count": 0},
        "scheduled_care": {
            "threshold": 2,
            "count": 1,
            "entries": [{"id": "evidence-crawl", "consecutive_failures": 4}],
        },
        "outcome_loop": {"verify_run_count": 0, "record_count": 0},
        "pending_tasks": [],
        "inbox_hygiene": {"issue_count": 0},
    }
    fragment = status_grid.render(payload, "unused")
    assert "1 scheduled care entry is failing repeatedly" in fragment
    assert "evidence-crawl" in fragment
    assert "care entry has repeated failed receipts" in fragment
    assert 'data-sg-tile="scheduled_care"' in fragment
    assert "ATTENTION" in fragment


def test_render_error_and_empty_payloads_degrade_safely():
    error_fragment = status_grid.render({"error": "boom"}, "unused")
    assert "boom" in error_fragment
    assert "Status" in error_fragment

    empty_fragment = status_grid.render({}, "unused")
    assert "Nothing here." in empty_fragment
