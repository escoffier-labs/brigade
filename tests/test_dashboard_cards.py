from pathlib import Path

from brigade.center_cmd.dashboard.views import memory_cards


def test_fetch_uses_memory_search_json(monkeypatch, tmp_path):
    expected = {"matches": [], "match_count": 0}
    calls: list[tuple[Path, list[str]]] = []

    def fake_run_json(target: Path, args: list[str]) -> dict:
        calls.append((target, args))
        return expected

    monkeypatch.setattr("brigade.center_cmd.dashboard.data.run_json", fake_run_json)

    assert memory_cards.fetch(tmp_path) == expected
    assert calls == [(tmp_path, ["memory", "search", ":", "--limit", "500"])]


def test_render_displays_card_rows_and_filter_wiring():
    payload = {
        "match_count": 2,
        "matches": [
            {
                "path": "memory/cards/fixture-alpha.md",
                "title": "Fixture Alpha Card",
                "tags": ["alpha", "fixture"],
                "summary": "Example alpha summary",
                "score": 3,
            },
            {
                "path": "memory/cards/fixture-beta.md",
                "title": "Fixture Beta Card",
                "tags": ["beta"],
                "summary": "Example beta summary",
                "score": 3,
                "last_reviewed": "2026-05-01",
                "fresh_until": "2026-12-01",
            },
        ],
    }

    fragment = memory_cards.render(payload, "unused")

    assert 'data-filter-target="memory-cards-table"' in fragment
    assert 'id="memory-cards-table"' in fragment
    assert "Fixture Alpha Card" in fragment
    assert "Fixture Beta Card" in fragment
    assert "alpha, fixture" in fragment
    assert "beta" in fragment
    assert "2026-05-01" in fragment
    assert "2026-12-01" in fragment
    assert ">-<" in fragment
    assert "<th>Title</th>" in fragment
    assert "<th>Tags</th>" in fragment
    assert "<th>Reviewed</th>" in fragment
    assert "<th>Freshness</th>" in fragment


def test_render_escapes_hostile_card_title():
    payload = {
        "matches": [
            {
                "title": "<script>alert(1)</script><img src=x onerror=alert(2)>",
                "tags": ["<b>tag</b>"],
                "summary": "hostile fixture",
                "score": 1,
            }
        ]
    }

    fragment = memory_cards.render(payload, "unused")

    # The XSS boundary is whether a tag can form, not whether the string
    # "onerror=" appears. Once < and > are entities the payload is text, so
    # assert no tag opens and that the payload survives only in escaped form.
    assert "<script" not in fragment
    assert "<img" not in fragment
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in fragment
    assert "&lt;img src=x onerror=alert(2)&gt;" in fragment
    assert "&lt;b&gt;tag&lt;/b&gt;" in fragment

    # No raw angle bracket from the hostile input reached the output: every
    # tag in the fragment is one the view itself emitted.
    import re

    for tag in re.findall(r"<[^>]*>", fragment):
        assert "alert(" not in tag


def test_render_error_and_empty_payloads_degrade_safely():
    assert "boom" in memory_cards.render({"error": "boom"}, "unused")

    assert "Nothing here." in memory_cards.render({}, "unused")
    assert "Nothing here." in memory_cards.render({"matches": []}, "unused")
