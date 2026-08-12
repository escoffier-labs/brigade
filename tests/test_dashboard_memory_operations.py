"""Unit tests for the Memory Operations dashboard view."""

from __future__ import annotations

import json
import re
from pathlib import Path

from brigade.center_cmd.dashboard.views import memory_operations


def test_fetch_uses_topology_and_inventory_json(monkeypatch, tmp_path):
    calls: list[tuple[Path, list[str]]] = []

    def fake_run_json(target: Path, args: list[str]) -> dict:
        calls.append((target, list(args)))
        if list(args)[:2] == ["memory", "topology"]:
            return {"nodes": [], "edges": [], "paths": [], "flags": [], "health": {}}
        return {
            "items": [],
            "pagination": {
                "offset": 0,
                "limit": 500,
                "total": 0,
                "returned": 0,
                "has_more": False,
                "next_offset": None,
            },
        }

    monkeypatch.setattr("brigade.center_cmd.dashboard.data.run_json", fake_run_json)

    payload = memory_operations.fetch(tmp_path)
    assert "topology" in payload
    assert "inventory" in payload
    assert (tmp_path, ["memory", "topology"]) in calls
    assert any(args[:2] == ["memory", "inventory"] for _, args in calls)
    assert all("search" not in args for _, args in calls)


def test_render_mode_tabs_and_inventory_fields():
    payload = {
        "topology": {
            "health": {
                "care_scan": {"status": "ok", "issue_count": 0},
                "refresh_queue": {"status": "ok", "count": 0},
                "handoff_backlog": {"status": "ok", "pending": 0},
                "quarantine": {"status": "unknown", "count": None},
                "closeout": {"status": "missing"},
                "evidence_projection": {"status": "ok"},
            },
            "flags": [],
            "paths": [],
            "nodes": [],
            "edges": [],
        },
        "inventory": {
            "items": [
                {
                    "id": "card:memory/cards/fixture-alpha.md",
                    "title": "Fixture Alpha Card",
                    "canonical_path": "memory/cards/fixture-alpha.md",
                    "logical_destination": "cards",
                    "store_type": "card",
                    "category": "fixture",
                    "tags": ["alpha", "fixture"],
                    "created_at": "2026-01-01",
                    "updated_at": "2026-01-02",
                    "last_reviewed": "2026-05-01",
                    "fresh_until": "2026-12-01",
                    "freshness": "fresh",
                    "review_state": "reviewed",
                    "evidence_state": "present",
                    "source_harness": "claude",
                    "source_handoff": None,
                    "owning_workflow": "ingest",
                    "last_mutation": None,
                    "care": {"issues": [], "queued_action": None},
                }
            ],
            "total": 1,
        },
    }

    fragment = memory_operations.render(payload, "unused-nonce")

    assert 'data-mo-mode="topology"' in fragment
    assert 'data-mo-mode="inventory"' in fragment
    assert "care scan" in fragment
    assert "Fixture Alpha Card" in fragment
    assert "memory/cards/fixture-alpha.md" in fragment
    assert "alpha, fixture" in fragment
    assert "2026-05-01" in fragment
    assert "2026-12-01" in fragment
    assert "<th>Title</th>" in fragment
    assert "<th>Freshness</th>" in fragment
    assert "<th>Evidence</th>" in fragment


def test_render_escapes_hostile_inventory_title():
    payload = {
        "topology": {"health": {}, "flags": [], "paths": [], "nodes": [], "edges": []},
        "inventory": {
            "items": [
                {
                    "title": "<script>alert(1)</script><img src=x onerror=alert(2)>",
                    "id": "card:x",
                    "canonical_path": "memory/cards/x.md",
                    "logical_destination": "cards",
                    "store_type": "card",
                    "category": "<b>tag</b>",
                    "tags": ["<b>tag</b>"],
                    "created_at": None,
                    "updated_at": None,
                    "last_reviewed": None,
                    "fresh_until": None,
                    "freshness": "missing",
                    "review_state": "missing",
                    "evidence_state": "missing",
                    "source_harness": "unknown",
                    "source_handoff": None,
                    "owning_workflow": "unknown",
                    "last_mutation": None,
                    "care": {"issues": [], "queued_action": None},
                }
            ],
            "total": 1,
        },
    }

    fragment = memory_operations.render(payload, "unused-nonce")

    assert "<img" not in fragment
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in fragment
    assert "&lt;img src=x onerror=alert(2)&gt;" in fragment
    assert "&lt;b&gt;tag&lt;/b&gt;" in fragment

    assert re.search(r"<script(?![^>]*\bnonce=)", fragment, re.IGNORECASE) is None
    for tag in re.findall(r"<[^>]*>", fragment):
        stripped = re.sub(r'"[^"]*"', '""', tag)
        stripped = re.sub(r"'[^']*'", "''", stripped)
        for name in re.findall(r"\s([A-Za-z_:][\w:.-]*)\s*=", stripped):
            assert not re.fullmatch(r"on\w+", name, re.IGNORECASE), tag


def test_render_error_and_empty_payloads_degrade_safely():
    fragment = memory_operations.render(
        {"topology": {"error": "boom"}, "inventory": {"error": "bang"}},
        "unused-nonce",
    )
    assert "boom" in fragment
    assert "bang" in fragment

    empty = memory_operations.render(
        {
            "topology": {"health": {}, "flags": [], "paths": [], "nodes": [], "edges": []},
            "inventory": {"items": [], "total": 0},
        },
        "unused-nonce",
    )
    assert "Nothing here." in empty


def test_tag_filter_encoding_is_exact_not_substring():
    """Tag data attrs must encode discrete tags; JS must exact-match, not substring."""
    payload = {
        "topology": {"health": {}, "flags": [], "paths": [], "nodes": [], "edges": []},
        "inventory": {
            "items": [
                {
                    "id": "card:tag-exact",
                    "title": "TAGROW_exact_sentinel",
                    "canonical_path": "memory/cards/tag-exact.md",
                    "logical_destination": "cards",
                    "store_type": "card",
                    "category": "ops",
                    "tags": ["fix", "fixture"],
                    "created_at": None,
                    "updated_at": None,
                    "last_reviewed": None,
                    "fresh_until": None,
                    "freshness": "fresh",
                    "review_state": "reviewed",
                    "evidence_state": "present",
                    "source_harness": "claude",
                    "source_handoff": None,
                    "owning_workflow": "ingest",
                    "last_mutation": None,
                    "care": {"issues": [], "queued_action": None},
                }
            ],
            "total": 1,
        },
    }
    fragment = memory_operations.render(payload, "nonce-tag")
    assert "TAGROW_exact_sentinel" in fragment
    assert 'data-mo-tags="' in fragment or "data-mo-tags='" in fragment
    # Encoded tags must be a JSON array of discrete values (not a comma string for matching).
    match = re.search(r'data-mo-tags="([^"]*)"', fragment)
    assert match is not None
    tags = json.loads(match.group(1).replace("&quot;", '"'))
    assert tags == ["fix", "fixture"]
    # Exact-match logic in script: parse JSON tags; no indexOf on the tag attribute.
    script = fragment[fragment.find("<script") :]
    assert "JSON.parse" in script
    assert 'getAttribute("data-mo-tags")' in script or "getAttribute('data-mo-tags')" in script
    assert "indexOf(wanted)" not in script


def test_mode_tabs_expose_aria_and_js_disabled_anchors():
    fragment = memory_operations.render(
        {
            "topology": {"health": {}, "flags": [], "paths": [], "nodes": [], "edges": []},
            "inventory": {
                "items": [
                    {
                        "id": "card:aria",
                        "title": "ARIAROW_sentinel",
                        "canonical_path": "memory/cards/aria.md",
                        "logical_destination": "cards",
                        "store_type": "card",
                        "category": "ops",
                        "tags": ["aria"],
                        "created_at": None,
                        "updated_at": None,
                        "last_reviewed": None,
                        "fresh_until": None,
                        "freshness": "fresh",
                        "review_state": "reviewed",
                        "evidence_state": "present",
                        "source_harness": "claude",
                        "source_handoff": None,
                        "owning_workflow": "ingest",
                        "last_mutation": None,
                        "care": {"issues": [], "queued_action": None},
                    }
                ],
                "total": 1,
            },
        },
        "nonce-aria",
    )
    assert "ARIAROW_sentinel" in fragment
    assert 'id="mo-tab-topology"' in fragment
    assert 'id="mo-tab-inventory"' in fragment
    assert 'aria-labelledby="mo-tab-topology"' in fragment
    assert 'aria-labelledby="mo-tab-inventory"' in fragment
    assert 'aria-controls="mo-topology"' in fragment
    assert 'aria-controls="mo-inventory"' in fragment
    assert 'href="#mo-topology"' in fragment
    assert 'href="#mo-inventory"' in fragment
    assert 'tabindex="0"' in fragment
    assert 'tabindex="-1"' in fragment
    # Pagination/filter controls stay delegated + nonced (no inline handlers).
    assert "data-mo-page=" in fragment or 'data-mo-page="' in fragment
    assert "data-mo-filter=" in fragment or "data-mo-filter-text" in fragment
    assert "nonce-aria" in fragment
    assert "onclick=" not in fragment


def test_inventory_pager_structure_exposes_second_page_item_and_prior_next():
    items = []
    for i in range(51):
        items.append(
            {
                "id": f"card:memory/cards/page-{i:04d}.md",
                "title": f"PAGEITEM_{i:04d}_SENTINEL",
                "canonical_path": f"memory/cards/page-{i:04d}.md",
                "logical_destination": "cards",
                "store_type": "card",
                "category": "ops",
                "tags": ["pager"],
                "created_at": None,
                "updated_at": None,
                "last_reviewed": None,
                "fresh_until": None,
                "freshness": "fresh",
                "review_state": "reviewed",
                "evidence_state": "present",
                "source_harness": "claude",
                "source_handoff": None,
                "owning_workflow": "ingest",
                "last_mutation": None,
                "care": {"issues": [], "queued_action": None},
            }
        )
    fragment = memory_operations.render(
        {
            "topology": {"health": {}, "flags": [], "paths": [], "nodes": [], "edges": []},
            "inventory": {"items": items, "total": 51},
        },
        "nonce-pager",
    )
    assert "PAGEITEM_0000_SENTINEL" in fragment
    assert "PAGEITEM_0050_SENTINEL" in fragment
    assert 'data-mo-page-size="50"' in fragment
    assert 'data-mo-page="prev"' in fragment
    assert 'data-mo-page="next"' in fragment
    assert "data-mo-page-status" in fragment
    # Prior starts disabled; Next remains enabled in markup when more than one page.
    assert re.search(r'data-mo-page="prev"[^>]*disabled', fragment) is not None
    next_btn = re.search(r'<button[^>]*data-mo-page="next"[^>]*>', fragment)
    assert next_btn is not None
    assert "disabled" not in next_btn.group(0)
