"""Unit tests for the Code Graph Center view (#887)."""

from __future__ import annotations

import re

from brigade import code_export
from brigade.center_cmd.dashboard.views import code_graph


def test_fetch_uses_code_export_contract(monkeypatch, tmp_path):
    calls: list[list[str]] = []

    def fake_run_json(target, args, **kwargs):
        calls.append(list(args))
        return {
            "schema": "brigade.code-graph-export.v1",
            "module_map": {
                "modules": [],
                "edges": [],
                "truncation": {"shown_modules": 0, "hidden_modules": 0, "shown_edges": 0, "hidden_edges": 0},
            },
            "stats": {},
        }

    monkeypatch.setattr("brigade.center_cmd.dashboard.data.run_json", fake_run_json)
    payload = code_graph.fetch(tmp_path, query={"symbol": "dispatch"})
    assert payload["symbol"] == "dispatch"
    assert calls == [["code", "export", "--symbol", "dispatch", "--overlay"]]


def test_render_module_map_truncation_note():
    payload = {
        "export": {
            "stats": {"symbols": 100, "files": 10},
            "module_map": {
                "modules": [
                    {"id": "src/pkg", "label": "pkg", "symbol_count": 12, "file_count": 2},
                ],
                "edges": [],
                "truncation": {
                    "shown_modules": 1,
                    "hidden_modules": 4,
                    "shown_edges": 0,
                    "hidden_edges": 0,
                    "note": "showing top 1 modules, 4 hidden",
                },
            },
        },
        "symbol": None,
    }
    fragment = code_graph.render(payload, "nonce-test")
    assert "showing top 1 modules, 4 hidden" in fragment
    assert 'class="cg-map"' in fragment
    assert "pkg" in fragment
    assert "<title>" in fragment


def test_render_impact_hides_raw_ids_in_primary_cells():
    payload = {
        "export": {
            "module_map": {
                "modules": [],
                "edges": [],
                "truncation": {"shown_modules": 0, "hidden_modules": 0, "shown_edges": 0, "hidden_edges": 0},
            },
            "stats": {},
            "impact": {
                "query": "fn",
                "resolved_symbol": {"name": "fn", "file_path": "src/a.py"},
                "callers": [
                    {
                        "source": "caller",
                        "target": "fn",
                        "kind": "calls",
                        "hops": 1,
                        "source_id": "deadbeef",
                        "target_id": "cafebabe",
                    }
                ],
                "edges": [],
                "affected_tests": {"affected_tests": []},
            },
        },
        "symbol": "fn",
    }
    fragment = code_graph.render(payload, "nonce-impact")
    assert "Direct callers" in fragment
    assert "deadbeef" not in fragment.split("<summary>")[0]
    assert "deadbeef" in fragment
    assert re.search(r"\bon\w+\s*=", fragment, re.IGNORECASE) is None


def test_fit_svg_label_truncates_with_tooltip_source():
    visible, full = code_export.fit_svg_label("brigade.work_cmd.footprint.predict", 80)
    assert len(visible) < len(full)
    assert full.startswith("brigade")
