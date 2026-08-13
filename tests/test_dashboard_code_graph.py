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


def _insight_payload() -> dict:
    return {
        "export": {
            "stats": {"symbols": 2737, "files": 40},
            "module_map": {
                "modules": [
                    {
                        "id": "src/brigade",
                        "label": "brigade",
                        "symbol_count": 2737,
                        "file_count": 12,
                        "package": "brigade",
                        "inbound_count": 9,
                        "dependents": ["other"],
                        "top_files": [{"path": "src/brigade/cli.py", "symbol_count": 80}],
                        "attributed_tests": ["tests/test_cli.py"],
                    },
                    {
                        "id": "src/other",
                        "label": "other",
                        "symbol_count": 4,
                        "file_count": 1,
                        "package": "other",
                        "inbound_count": 0,
                        "changed": True,
                        "dependents": [],
                        "top_files": [{"path": "src/other/util.py", "symbol_count": 4}],
                        "attributed_tests": [],
                    },
                ],
                "edges": [{"from": "src/other", "to": "src/brigade", "weight": 6}],
                "truncation": {
                    "shown_modules": 15,
                    "hidden_modules": 33,
                    "shown_edges": 1,
                    "hidden_edges": 40,
                    "note": "showing top 15 of 48 modules, 40 edges hidden",
                },
                "insights": {
                    "total_modules": 48,
                    "core": {"id": "src/brigade", "label": "brigade", "symbol_count": 2737},
                    "most_connected": {
                        "id": "src/brigade",
                        "label": "brigade",
                        "inbound": 9,
                    },
                    "isolated_count": 12,
                    "biggest_change": {"id": "src/other", "label": "other"},
                },
            },
            "change_overlay": {"changed_modules": ["src/other"], "changed_files": ["src/other/util.py"]},
        },
        "symbol": None,
    }


def test_render_leads_with_linked_plain_language_summary():
    fragment = code_graph.render(_insight_payload(), "nonce-summary")
    assert 'data-cg-summary="1"' in fragment
    assert "2737" in fragment
    assert "Most connected" in fragment
    assert "imported by" in fragment
    assert "isolated" in fragment
    assert "Biggest recent change impact" in fragment
    summary = fragment.split("data-cg-summary", 1)[1].split("</section>", 1)[0]
    assert summary.count("<a ") >= 3
    assert "#cg-hub" in summary or "cg-module-src/brigade" in summary or "data-cg-jump" in summary


def test_render_shows_labeled_truncation_not_hairball_caption():
    fragment = code_graph.render(_insight_payload(), "nonce-trunc")
    assert 'data-cg-truncation="1"' in fragment
    assert "showing top 15 of 48 modules, 40 edges hidden" in fragment


def test_render_module_click_shows_plain_word_card_not_raw_rows():
    fragment = code_graph.render(_insight_payload(), "nonce-card")
    assert 'data-cg-card="' in fragment or "data-cg-card='" in fragment
    assert 'id="cg-card"' in fragment
    assert "Top files" in fragment or "top symbols" in fragment.lower()
    assert "Attributed tests" in fragment
    assert "who depends" in fragment.lower() or "Imported by" in fragment
    assert "source_id" not in fragment.split('id="cg-card"', 1)[1].split("</aside>", 1)[0]


def test_code_view_warm_path_reuses_snapshot_cache(monkeypatch, tmp_path):
    import threading
    from pathlib import Path

    from brigade.center_cmd.serve import _make_server
    from tests.test_center_serve import _headers_and_body, _raw_request, _status_code, _wait_until_ready

    calls: list[int] = []

    def fake_fetch(target, query=None):
        del target, query
        calls.append(1)
        return _insight_payload()

    monkeypatch.setattr(code_graph, "fetch", fake_fetch)
    server = _make_server(host="127.0.0.1", port=0, token=None, allowed_hosts=None, target=Path(tmp_path))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        _wait_until_ready(server)
        host, port = server.server_address
        first = _raw_request(server, f"{host}:{port}", path="/view/code")
        second = _raw_request(server, f"{host}:{port}", path="/view/code")
        assert _status_code(first) == "200"
        assert _status_code(second) == "200"
        _, body = _headers_and_body(second)
        assert 'data-cg-summary="1"' in body
        assert "showing top 15 of 48 modules, 40 edges hidden" in body
        assert calls == [1]
    finally:
        server.shutdown()
        server.server_close()
