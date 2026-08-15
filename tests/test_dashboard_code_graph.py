"""Unit tests for the Code Graph Center view (#887)."""

from __future__ import annotations

import html as html_lib
import re

from brigade import code_export
from brigade.center_cmd.dashboard.views import code_graph

_SVG_CHAR_EM = 0.62


def _approx_label_width(text: str, font_size: int = 12) -> int:
    return max(0, int(round(len(text) * max(6.0, font_size * _SVG_CHAR_EM))))


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
                    "core": {"id": "src/brigade", "label": "brigade", "symbol_count": 2741},
                    "largest": {"id": "src/brigade", "label": "brigade", "symbol_count": 2737},
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


def _tests_as_core_payload() -> dict:
    return {
        "export": {
            "stats": {"symbols": 8966, "files": 40},
            "module_map": {
                "modules": [
                    {
                        "id": "tests",
                        "label": "tests",
                        "symbol_count": 8966,
                        "file_count": 200,
                        "package": "tests",
                    },
                    {
                        "id": "src/brigade",
                        "label": "brigade",
                        "symbol_count": 100,
                        "file_count": 12,
                        "package": "brigade",
                    },
                    {
                        "id": "src/brigade/work_cmd",
                        "label": "work_cmd",
                        "symbol_count": 7,
                        "file_count": 3,
                        "package": "brigade",
                    },
                    {
                        "id": "src/brigade/claude_hooks",
                        "label": "claude_hooks",
                        "symbol_count": 12,
                        "file_count": 2,
                        "package": "brigade",
                    },
                    {
                        "id": "src/other",
                        "label": "other",
                        "symbol_count": 4,
                        "file_count": 1,
                        "package": "other",
                        "changed": True,
                    },
                ],
                "edges": [],
                "insights": {
                    "total_modules": 37,
                    "core": {"id": "src/brigade", "label": "brigade", "symbol_count": 9089},
                    "largest": {"id": "tests", "label": "tests", "symbol_count": 8966},
                    "most_connected": {"id": "src/brigade", "label": "brigade", "inbound": 3},
                    "isolated_count": 2,
                    "biggest_change": {"id": "src/other", "label": "other"},
                },
            },
        },
        "symbol": None,
    }


def test_summary_names_package_not_tests_and_calls_out_largest():
    fragment = code_graph.render(_tests_as_core_payload(), "nonce-package")
    summary = fragment.split("data-cg-summary", 1)[1].split("</section>", 1)[0]
    assert "core package" not in summary
    assert "tests's" not in summary
    assert "brigade:" in summary
    assert "8,966" in summary or "8966" in summary
    assert "across 37 modules" in summary
    assert "largest:" in summary.lower()
    assert "tests" in summary
    assert "Most connected" in summary
    assert "isolated" in summary


def test_module_labels_fit_in_boxes_without_ok_chips():
    fragment = code_graph.render(_tests_as_core_payload(), "nonce-labels")
    assert re.search(r'cg-svg-chip-word">OK<', fragment) is None
    assert re.search(r'cg-svg-chip-word">CHANGED<', fragment)
    assert "<title>work_cmd (7)</title>" in fragment
    assert "<title>claude_hooks (12)</title>" in fragment

    for match in re.finditer(r'<g class="cg-module"[^>]*>(.*?)</g>', fragment, re.DOTALL):
        box = match.group(1)
        box_rect = re.search(r'<rect x="(\d+)" y="[^"]*" width="(\d+)" height="(\d+)"', box)
        label = re.search(r'<text x="(\d+)" y="[^"]*" class="cg-svg-label">([^<]*)</text>', box)
        assert box_rect is not None
        assert label is not None
        box_x = int(box_rect.group(1))
        box_w = int(box_rect.group(2))
        text_x = int(label.group(1))
        visible = html_lib.unescape(label.group(2))
        text_end = text_x + _approx_label_width(visible)
        assert text_end <= box_x + box_w - 4
        chip = re.search(r'<rect x="(\d+)" y="[^"]*" width="(\d+)" height="16"', box)
        if chip:
            chip_x = int(chip.group(1))
            assert text_end <= chip_x
            assert "CHANGED" in box
