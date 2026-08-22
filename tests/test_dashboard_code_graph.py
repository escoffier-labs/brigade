"""Unit tests for the Code Graph Center view (#887)."""

from __future__ import annotations

import html as html_lib
import random
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
    payload = code_graph.fetch(tmp_path, query={"symbol": "dispatch", "map": "full", "worktrees": "1"})
    assert payload["symbol"] == "dispatch"
    assert payload["show_full_map"] is True
    assert payload["show_worktrees"] is True
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
                    {
                        "id": "src/unrelated",
                        "label": "unrelated",
                        "symbol_count": 2,
                        "file_count": 1,
                        "package": "unrelated",
                        "inbound_count": 0,
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
    assert 'data-cg-summary-scope="overlay"' in fragment
    assert "changed on this branch" in fragment
    assert "direct neighbors" in fragment
    assert "Most connected" in fragment
    assert "imported by" in fragment
    assert "Biggest recent change impact" in fragment
    summary = fragment.split("data-cg-summary", 1)[1].split("</section>", 1)[0]
    assert "across 48 modules" not in summary
    assert summary.count("<a ") >= 3
    assert "#cg-hub" in summary or "cg-module-src/brigade" in summary or "data-cg-jump" in summary


def test_render_shows_labeled_truncation_not_hairball_caption():
    payload = _insight_payload()
    payload["show_full_map"] = True
    fragment = code_graph.render(payload, "nonce-trunc")
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
        assert "changed on this branch" in body
        assert 'data-cg-overlay="1"' in body
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


def _zero_changed_payload() -> dict:
    payload = _insight_payload()
    payload["export"]["change_overlay"] = {"changed_modules": [], "changed_files": []}
    payload["export"]["module_map"]["modules"][1]["changed"] = False
    payload["export"]["module_map"]["insights"]["biggest_change"] = {}
    return payload


def _worktrees_payload() -> dict:
    return {
        "export": {
            "stats": {"symbols": 100, "files": 4},
            "module_map": {
                "modules": [
                    {
                        "id": "tests",
                        "label": "tests",
                        "symbol_count": 80,
                        "file_count": 10,
                        "package": "tests",
                    },
                    {
                        "id": ".worktrees/pr-906/tests",
                        "label": "tests",
                        "symbol_count": 80,
                        "file_count": 10,
                        "package": "tests",
                    },
                    {
                        "id": ".worktrees/pr-906/src/brigade",
                        "label": "brigade",
                        "symbol_count": 12,
                        "file_count": 2,
                        "package": "brigade",
                    },
                    {
                        "id": "src/brigade",
                        "label": "brigade",
                        "symbol_count": 12,
                        "file_count": 2,
                        "package": "brigade",
                    },
                ],
                "edges": [],
                "insights": {
                    "total_modules": 4,
                    "core": {"id": "src/brigade", "label": "brigade", "symbol_count": 100},
                    "largest": {"id": "tests", "label": "tests", "symbol_count": 80},
                    "most_connected": {"id": "src/brigade", "label": "brigade", "inbound": 0},
                    "isolated_count": 4,
                    "biggest_change": {},
                },
            },
            "change_overlay": {"changed_modules": [], "changed_files": []},
        },
        "symbol": None,
    }


def test_default_view_is_branch_overlay_not_whole_repo_grid():
    fragment = code_graph.render(_insight_payload(), "nonce-overlay")
    assert 'data-cg-overlay="1"' in fragment
    assert 'id="cg-module-src/other"' in fragment
    assert 'id="cg-module-src/brigade"' in fragment
    assert 'id="cg-module-src/unrelated"' not in fragment
    assert "unrelated" not in fragment.split('class="cg-map"', 1)[-1].split("</svg>", 1)[0]
    assert "CHANGED" in fragment
    assert "across 48 modules" not in fragment.split("data-cg-summary", 1)[1].split("</section>", 1)[0]


def test_zero_changed_shows_full_map_and_banner():
    fragment = code_graph.render(_zero_changed_payload(), "nonce-empty")
    assert 'data-cg-empty-change="1"' in fragment
    assert "Nothing changed locally" in fragment
    assert 'id="cg-module-src/other"' in fragment
    assert 'id="cg-module-src/brigade"' in fragment
    assert 'id="cg-module-src/unrelated"' in fragment
    assert 'data-cg-overlay="1"' not in fragment
    assert 'data-cg-summary-scope="full"' in fragment


def test_worktrees_collapsed_by_default_and_toggle_reveals_them():
    hidden = code_graph.render(_worktrees_payload(), "nonce-wt-hide")
    assert 'id="cg-module-tests"' in hidden
    assert 'id="cg-module-src/brigade"' in hidden
    assert 'id="cg-module-.worktrees/pr-906/tests"' not in hidden
    assert 'id="cg-module-.worktrees/pr-906/src/brigade"' not in hidden
    assert 'data-cg-worktrees-toggle="show"' in hidden
    assert "Show .worktrees copies" in hidden
    titles = re.findall(r"<title>([^<]*)</title>", hidden)
    assert titles.count("tests (80)") == 1

    shown = code_graph.render({**_worktrees_payload(), "show_worktrees": True}, "nonce-wt-show")
    assert 'id="cg-module-.worktrees/pr-906/tests"' in shown
    assert 'id="cg-module-.worktrees/pr-906/src/brigade"' in shown
    assert 'data-cg-worktrees-toggle="hide"' in shown
    assert "Hide .worktrees copies" in shown


def test_impact_start_from_changed_modules_shortcut():
    fragment = code_graph.render(_insight_payload(), "nonce-shortcut")
    assert 'data-cg-changed-shortcut="1"' in fragment
    assert "Start from changed modules" in fragment
    assert 'href="/view/code?symbol=src%2Fother"' in fragment or 'href="/view/code?symbol=src/other"' in fragment


def test_impact_diagram_truncation_note():
    callers = [
        {
            "source": f"caller-{index}",
            "target": "fn",
            "kind": "calls",
            "hops": 1,
            "source_id": f"src{index:02d}",
            "target_id": "focus",
        }
        for index in range(11)
    ]
    edges = [
        {
            "source": f"edge-{index}",
            "target": "fn",
            "kind": "calls",
            "hops": 2,
            "source_id": f"e{index:02d}",
            "target_id": "focus",
        }
        for index in range(14)
    ]
    payload = {
        "export": {
            "module_map": {"modules": [], "edges": []},
            "stats": {},
            "change_overlay": {"changed_modules": []},
            "impact": {
                "query": "fn",
                "resolved_symbol": {"name": "fn", "file_path": "src/a.py"},
                "callers": callers,
                "edges": edges,
                "affected_tests": {"affected_tests": []},
            },
        },
        "symbol": "fn",
    }
    fragment = code_graph.render(payload, "nonce-impact-trunc")
    assert 'data-cg-impact-truncation="1"' in fragment
    assert "showing top 8 callers, 3 hidden" in fragment
    assert "showing top 10 impact edges, 4 hidden" in fragment
    labels = re.findall(r'class="cg-svg-label">([^<]*)</text>', fragment)
    assert "caller-0" in labels
    assert "caller-10" not in labels
    assert "edge-0" in labels
    assert "edge-13" not in labels


def _blast_view_payload(
    *,
    focus: str = "src/other",
    mode: str = "blast",
    upstream: list[dict] | None = None,
    downstream: list[dict] | None = None,
    changed_modules: list[str] | None = None,
    up_hops: int = 1,
    down_hops: int = 1,
    resolved: bool = True,
) -> dict:
    if changed_modules is None:
        changed_modules = ["src/other"]
    if upstream is None:
        upstream = [
            {"id": "src/caller-a", "label": "caller-a", "hops": 1, "weight": 4},
            {"id": "src/caller-b", "label": "caller-b", "hops": 1, "weight": 2},
        ]
    if downstream is None:
        downstream = [
            {"id": "src/brigade", "label": "brigade", "hops": 1, "weight": 6},
        ]
    payload = _insight_payload()
    payload["mode"] = mode
    payload["focus"] = focus
    payload["up_hops"] = up_hops
    payload["down_hops"] = down_hops
    payload["export"]["change_overlay"]["changed_modules"] = changed_modules
    payload["export"]["blast_radius"] = {
        "focus": {"id": focus, "label": focus.split("/")[-1], "changed": focus in changed_modules},
        "resolved": resolved,
        "upstream": upstream,
        "downstream": downstream,
        "upstream_depth": up_hops,
        "downstream_depth": down_hops,
    }
    return payload


def test_blast_radius_is_primary_mode_map_stays_default():
    fragment = code_graph.render(_insight_payload(), "nonce-blast-default")
    assert 'data-cg-mode="blast"' in fragment
    assert "Blast radius" in fragment
    assert re.search(r'data-cg-mode="map"[^>]*aria-selected="true"', fragment)
    assert re.search(r'data-cg-mode="blast"[^>]*aria-selected="false"', fragment)
    assert 'id="cg-map"' in fragment
    assert 'data-cg-panel="blast" role="tabpanel" hidden' in fragment
    assert 'data-cg-panel="map" role="tabpanel" hidden' not in fragment.split('id="cg-impact"', 1)[0]


def test_blast_radius_zones_and_counts_for_focus():
    fragment = code_graph.render(_blast_view_payload(), "nonce-blast-zones")
    assert 'data-cg-blast="1"' in fragment
    assert 'data-cg-blast-zone="upstream"' in fragment
    assert 'data-cg-blast-zone="focus"' in fragment
    assert 'data-cg-blast-zone="downstream"' in fragment
    assert "What breaks if you change this" in fragment
    assert "What this leans on" in fragment
    assert "2 modules depend on this" in fragment
    assert "this depends on 1 module" in fragment
    assert "caller-a" in fragment
    assert "caller-b" in fragment
    assert "brigade" in fragment
    assert 'data-cg-blast-focus="src/other"' in fragment
    assert "force-directed" not in fragment.lower()


def test_blast_radius_default_one_hop_and_expand():
    fragment = code_graph.render(_blast_view_payload(), "nonce-blast-expand")
    assert 'data-cg-blast-expand="upstream"' in fragment
    assert 'data-cg-blast-expand="downstream"' in fragment
    assert "Expand one more hop" in fragment
    assert "up=2" in fragment
    assert "down=2" in fragment
    assert "2 hops away" not in fragment

    expanded = code_graph.render(
        _blast_view_payload(
            up_hops=2,
            upstream=[
                {"id": "src/caller-a", "label": "caller-a", "hops": 1, "weight": 4},
                {"id": "src/caller-c", "label": "caller-c", "hops": 2, "weight": 1},
            ],
        ),
        "nonce-blast-hop2",
    )
    assert "2 hops away" in expanded
    assert "caller-c" in expanded
    assert "up=3" in expanded


def test_blast_radius_seeds_from_changed_and_picker():
    one = code_graph.render(_insight_payload(), "nonce-blast-seed")
    assert 'data-cg-blast-picker="1"' in one
    assert 'data-cg-blast-seeds="1"' in one
    assert 'name="focus"' in one
    assert "Show blast radius" in one
    assert "other" in one.split('data-cg-blast-seeds="1"', 1)[1].split("</div>", 1)[0]

    several = _insight_payload()
    several["export"]["change_overlay"]["changed_modules"] = ["src/other", "src/brigade"]
    several["mode"] = "blast"
    fragment = code_graph.render(several, "nonce-blast-stack")
    seeds = fragment.split('data-cg-blast-seeds="1"', 1)[1].split("</div>", 1)[0]
    assert "Changed on this branch" in seeds
    assert "focus=src%2Fother" in seeds or "focus=src/other" in seeds
    assert "focus=src%2Fbrigade" in seeds or "focus=src/brigade" in seeds
    assert 'data-cg-blast-picker="1"' in fragment


def test_blast_radius_empty_zones_use_plain_language():
    fragment = code_graph.render(
        _blast_view_payload(upstream=[], downstream=[]),
        "nonce-blast-empty",
    )
    assert "nothing depends on this yet" in fragment
    assert "this does not lean on other modules yet" in fragment
    assert 'data-cg-blast-empty="upstream"' in fragment
    assert 'data-cg-blast-empty="downstream"' in fragment
    assert 'data-cg-blast-count="upstream"' not in fragment
    assert 'data-cg-blast-count="downstream"' not in fragment


def test_blast_radius_truncation_states_true_total():
    upstream = [{"id": f"src/dep-{index}", "label": f"dep-{index}", "hops": 1, "weight": 1} for index in range(34)]
    fragment = code_graph.render(_blast_view_payload(upstream=upstream), "nonce-blast-trunc")
    assert 'data-cg-blast-truncation="upstream"' in fragment
    assert "showing 10 of 34" in fragment
    assert "34 modules depend on this" in fragment
    assert "dep-0" in fragment
    assert "dep-9" in fragment
    assert (
        "dep-10" not in fragment.split('data-cg-blast-zone="upstream"', 1)[1].split('data-cg-blast-zone="focus"', 1)[0]
    )


def test_blast_radius_uses_full_graph_neighbors_not_capped_export():
    nodes = {f"src/top{index}/file.py": 50 for index in range(20)}
    edges: list[tuple[str, str, int]] = []
    for index in range(20):
        for other in range(index + 1, min(index + 4, 20)):
            edges.append((f"src/top{index}/file.py", f"src/top{other}/file.py", 5))
    nodes["src/focus/mod.py"] = 2
    nodes["src/hidden/util.py"] = 1
    nodes["src/need/lib.py"] = 1
    edges.append(("src/hidden/util.py", "src/focus/mod.py", 2))
    edges.append(("src/focus/mod.py", "src/need/lib.py", 3))
    file_graph = {"nodes": nodes, "edges": edges}

    mapped = code_export._build_module_map(file_graph, changed_files=["src/focus/mod.py"])
    shown_ids = {row["id"] for row in mapped["modules"]}
    shown_edge_pairs = {(row["from"], row["to"]) for row in mapped["edges"]}
    assert "src/hidden" not in shown_ids or ("src/hidden", "src/focus") not in shown_edge_pairs

    blast = code_export.focus_neighborhood(file_graph, "src/focus", changed_modules={"src/focus"})
    up_ids = {row["id"] for row in blast["upstream"]}
    down_ids = {row["id"] for row in blast["downstream"]}
    assert "src/hidden" in up_ids
    assert "src/need" in down_ids

    payload = {
        "export": {
            "module_map": mapped,
            "change_overlay": {"changed_modules": ["src/focus"], "changed_files": ["src/focus/mod.py"]},
            "blast_radius": blast,
            "stats": {},
        },
        "mode": "blast",
        "focus": "src/focus",
    }
    fragment = code_graph.render(payload, "nonce-blast-fullgraph")
    upstream_html = fragment.split('data-cg-blast-zone="upstream"', 1)[1].split('data-cg-blast-zone="focus"', 1)[0]
    downstream_html = fragment.split('data-cg-blast-zone="downstream"', 1)[1]
    assert "hidden" in upstream_html
    assert "need" in downstream_html
    assert "1 module depends on this" in fragment
    assert "this depends on 1 module" in fragment


def test_blast_radius_has_nonce_and_no_inline_style():
    nonce = "nonce-blast-csp"
    fragment = code_graph.render(_blast_view_payload(), nonce)
    assert f'<style nonce="{nonce}">' in fragment
    assert f'<script nonce="{nonce}">' in fragment
    assert re.search(r"\sstyle\s*=", fragment) is None
    assert re.search(r"\bon\w+\s*=", fragment, re.IGNORECASE) is None


def test_fetch_passes_focus_and_hop_flags(monkeypatch, tmp_path):
    calls: list[list[str]] = []

    def fake_run_json(target, args, **kwargs):
        calls.append(list(args))
        return {"schema": "brigade.code-graph-export.v1", "module_map": {"modules": [], "edges": []}}

    monkeypatch.setattr("brigade.center_cmd.dashboard.data.run_json", fake_run_json)
    payload = code_graph.fetch(tmp_path, query={"focus": "src/other", "up": "2", "mode": "blast"})
    assert payload["focus"] == "src/other"
    assert payload["up_hops"] == 2
    assert payload["mode"] == "blast"
    assert calls == [["code", "export", "--overlay", "--focus", "src/other", "--up", "2"]]


def _multi_parent_file_graph() -> dict:
    """Focus with two hop-1 parents; hop-2 modules reachable through both.

    First-parent-wins would rank hop-2 modules differently depending on which
    parent set-iteration discovered first. Summed weights make the order unique.
    """
    nodes = {
        "src/focus/mod.py": 2,
        "src/mida/a.py": 1,
        "src/midb/b.py": 1,
        "src/outa/a.py": 1,
        "src/outb/b.py": 1,
    }
    edges: list[tuple[str, str, int]] = [
        ("src/mida/a.py", "src/focus/mod.py", 1),
        ("src/midb/b.py", "src/focus/mod.py", 1),
        ("src/focus/mod.py", "src/outa/a.py", 1),
        ("src/focus/mod.py", "src/outb/b.py", 1),
    ]
    # (name, weight via A, weight via B) — sums are unique and first-parent-wins
    # would invert leaf-aa vs leaf-bb if B were discovered first.
    specs = [
        ("leaf-aa", 100, 1),
        ("leaf-bb", 2, 50),
        ("leaf-cc", 3, 40),
        ("leaf-dd", 4, 30),
        ("leaf-ee", 5, 20),
        ("leaf-ff", 6, 10),
        ("leaf-gg", 7, 8),
        ("leaf-hh", 8, 6),
        ("leaf-ii", 9, 4),
        ("leaf-jj", 10, 2),
        ("leaf-kk", 1, 1),
        ("leaf-ll", 11, 1),
    ]
    for name, via_a, via_b in specs:
        nodes[f"src/{name}/x.py"] = 1
        edges.append((f"src/{name}/x.py", "src/mida/a.py", via_a))
        edges.append((f"src/{name}/x.py", "src/midb/b.py", via_b))
        nodes[f"src/d-{name}/x.py"] = 1
        edges.append(("src/outa/a.py", f"src/d-{name}/x.py", via_a))
        edges.append(("src/outb/b.py", f"src/d-{name}/x.py", via_b))
    return {"nodes": nodes, "edges": edges}


def _shuffle_file_graph(file_graph: dict, rng: random.Random) -> dict:
    nodes = list(file_graph["nodes"].items())
    rng.shuffle(nodes)
    edges = list(file_graph["edges"])
    rng.shuffle(edges)
    return {"nodes": dict(nodes), "edges": edges}


def _shuffle_adjacency(
    adjacency: dict[str, set[str]],
    edge_weight: dict[tuple[str, str], int],
    rng: random.Random,
) -> tuple[dict[str, set[str]], dict[tuple[str, str], int]]:
    items = list(adjacency.items())
    rng.shuffle(items)
    shuffled_adj = {}
    for key, neighbors in items:
        values = list(neighbors)
        rng.shuffle(values)
        shuffled_adj[key] = set(values)
    weight_items = list(edge_weight.items())
    rng.shuffle(weight_items)
    return shuffled_adj, dict(weight_items)


def _rank_tuples(rows: list[dict]) -> list[tuple[str, int, int]]:
    return [(str(row["id"]), int(row["hops"]), int(row["weight"])) for row in rows]


def test_focus_neighborhood_multi_parent_rank_is_deterministic():
    base = _multi_parent_file_graph()
    first = code_export.focus_neighborhood(base, "src/focus", up_hops=2, down_hops=2)
    expected_up = _rank_tuples(first["upstream"])
    expected_down = _rank_tuples(first["downstream"])
    assert [row[0] for row in expected_up if row[1] == 2][:3] == [
        "src/leaf-aa",
        "src/leaf-bb",
        "src/leaf-cc",
    ]
    assert expected_up[expected_up.index(("src/leaf-aa", 2, 101))]
    assert ("src/leaf-bb", 2, 52) in expected_up
    assert [row[0] for row in expected_down if row[1] == 2][:2] == ["src/d-leaf-aa", "src/d-leaf-bb"]

    for seed in range(8):
        shuffled = _shuffle_file_graph(base, random.Random(seed))
        again = code_export.focus_neighborhood(shuffled, "src/focus", up_hops=2, down_hops=2)
        assert _rank_tuples(again["upstream"]) == expected_up
        assert _rank_tuples(again["downstream"]) == expected_down

    graph = code_export._uncapped_module_graph(base)
    labels = graph["labels"]
    walk_up = _rank_tuples(
        code_export._walk_hops(
            "src/focus",
            graph["inbound"],
            2,
            edge_weight=graph["edge_weight"],
            labels=labels,
            inbound=True,
        )
    )
    assert walk_up == expected_up
    for seed in range(8):
        adj, weights = _shuffle_adjacency(graph["inbound"], graph["edge_weight"], random.Random(seed + 20))
        walked = code_export._walk_hops(
            "src/focus",
            adj,
            2,
            edge_weight=weights,
            labels=labels,
            inbound=True,
        )
        assert _rank_tuples(walked) == expected_up


def test_focus_neighborhood_duplicate_label_is_ambiguous_until_full_id():
    file_graph = {
        "nodes": {
            "src/util/file.py": 2,
            "engines/util/lib.py": 3,
            "src/focus/mod.py": 1,
        },
        "edges": [],
    }
    by_label = code_export.focus_neighborhood(file_graph, "util")
    assert by_label["resolved"] is False
    assert by_label["ambiguous"] is True
    candidate_ids = [str(row["id"]) for row in by_label["candidates"]]
    assert candidate_ids == ["engines/util", "src/util"]
    assert by_label["upstream"] == []
    assert by_label["downstream"] == []
    assert by_label["focus"]["id"] == "util"

    for seed in range(6):
        shuffled = _shuffle_file_graph(file_graph, random.Random(seed))
        again = code_export.focus_neighborhood(shuffled, "util")
        assert [str(row["id"]) for row in again["candidates"]] == ["engines/util", "src/util"]
        assert again["resolved"] is False
        assert again["ambiguous"] is True

    by_id = code_export.focus_neighborhood(file_graph, "src/util")
    assert by_id["resolved"] is True
    assert by_id["ambiguous"] is False
    assert by_id["focus"]["id"] == "src/util"
    assert by_id["candidates"] == []

    other_id = code_export.focus_neighborhood(file_graph, "engines/util")
    assert other_id["resolved"] is True
    assert other_id["focus"]["id"] == "engines/util"

    fragment = code_graph.render(
        {
            "export": {
                "module_map": {"modules": [], "edges": []},
                "change_overlay": {"changed_modules": []},
                "blast_radius": by_label,
                "stats": {},
            },
            "mode": "blast",
            "focus": "util",
        },
        "nonce-blast-ambiguous",
    )
    assert 'data-cg-blast-ambiguous="1"' in fragment
    assert "That name matches more than one module" in fragment
    assert "src/util" in fragment
    assert "engines/util" in fragment
    assert "focus=src%2Futil" in fragment or "focus=src/util" in fragment
    assert "focus=engines%2Futil" in fragment or "focus=engines/util" in fragment
    assert 'data-cg-blast="1"' not in fragment
