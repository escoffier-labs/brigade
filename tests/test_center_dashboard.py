import re
import subprocess
import threading
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from brigade.center_cmd.dashboard import data, render
from brigade.center_cmd.dashboard.views import all_views, view_by_name
from brigade.center_cmd.serve import _make_server
from tests.test_center_serve import _headers_and_body, _raw_request, _status_code, _wait_until_ready


@pytest.fixture
def dashboard_server(tmp_target):
    server = _make_server(
        host="127.0.0.1",
        port=0,
        token=None,
        allowed_hosts=None,
        target=tmp_target,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _wait_until_ready(server)
    yield server
    server.shutdown()
    server.server_close()


def test_esc_escapes_html_special_characters():
    assert render.esc("<>&\"'") == "&lt;&gt;&amp;&quot;&#x27;"


def test_run_json_returns_error_on_nonzero_exit(monkeypatch, tmp_target):
    def fake_run(*args, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="command failed badly")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = data.run_json(tmp_target, ["status"])
    assert result == {"error": "command failed badly"}


def test_run_json_returns_error_on_malformed_json(monkeypatch, tmp_target):
    def fake_run(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout="not-json", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = data.run_json(tmp_target, ["status"])
    assert result == {"error": "invalid JSON from command"}


def test_run_json_never_uses_shell_true(monkeypatch, tmp_target):
    captured: dict[str, object] = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    data.run_json(tmp_target, ["status"])
    assert captured.get("shell") is not True


@pytest.mark.parametrize(
    "attr",
    ["NAME", "TITLE", "ORDER", "fetch", "render"],
)
def test_registered_views_expose_contract(attr):
    for module in all_views():
        assert hasattr(module, attr)


def test_registered_view_names_are_unique():
    names = [module.NAME for module in all_views()]
    assert len(names) == len(set(names))


def test_get_view_status_returns_200(dashboard_server):
    host, port = dashboard_server.server_address
    response = _raw_request(dashboard_server, f"{host}:{port}", path="/view/status")
    assert _status_code(response) == "200"


@pytest.mark.parametrize("view_name", [module.NAME for module in all_views()])
def test_get_each_registered_view_returns_200(dashboard_server, view_name):
    host, port = dashboard_server.server_address
    response = _raw_request(dashboard_server, f"{host}:{port}", path=f"/view/{view_name}")
    assert _status_code(response) == "200"
    _, body = _headers_and_body(response)
    assert "failed to render" not in body


def test_agents_view_renders_machine_cards_and_tiles(dashboard_server, monkeypatch):
    from brigade.center_cmd.dashboard.views import agent_activity

    def fake_fetch(target):
        del target
        return {
            "agent_activity_summary": [{"provider": "brigade", "state_counts": {"running": 1}}],
            "agent_activity": [
                {
                    "activity_id": "brigade:run:demo",
                    "parent_activity_id": None,
                    "provider": "brigade",
                    "harness": "brigade-run",
                    "kind": "run",
                    "host": "rocinante",
                    "label": "Brigade run",
                    "task_label": "Browser check task",
                    "model": "gpt-5.6-terra",
                    "state": "running",
                    "started_at": "2026-08-12T12:00:00+00:00",
                    "last_updated_at": "2026-08-12T12:01:00+00:00",
                    "elapsed_seconds": 60,
                    "source": {"name": "brigade-run-journal", "authority": "authoritative"},
                    "links": {},
                }
            ],
        }

    monkeypatch.setattr(agent_activity, "fetch", fake_fetch)
    host, port = dashboard_server.server_address
    response = _raw_request(dashboard_server, f"{host}:{port}", path="/view/agents")
    assert _status_code(response) == "200"
    _, body = _headers_and_body(response)
    assert 'class="machine-card"' in body
    assert 'class="agent-tile"' in body
    assert "Browser check task" in body
    assert 'data-host="cloud"' in body


def test_get_unknown_view_returns_404(dashboard_server):
    host, port = dashboard_server.server_address
    response = _raw_request(dashboard_server, f"{host}:{port}", path="/view/nope")
    assert _status_code(response) == "404"


@pytest.mark.parametrize(
    ("legacy", "location"),
    [
        ("/view/graph", "/view/work#ready"),
        ("/view/waves", "/view/work#ready"),
        ("/view/claims", "/view/work#claims"),
        ("/view/activity", "/view/work#recent"),
    ],
)
def test_folded_work_routes_redirect_to_work_anchors(dashboard_server, legacy, location):
    host, port = dashboard_server.server_address
    response = _raw_request(dashboard_server, f"{host}:{port}", path=legacy)
    assert _status_code(response) == "301"
    headers, _body = _headers_and_body(response)
    assert f"Location: {location}" in headers


def test_handoffs_route_redirects_to_memory_handoffs_tab(dashboard_server):
    host, port = dashboard_server.server_address
    response = _raw_request(dashboard_server, f"{host}:{port}", path="/view/handoffs")
    assert _status_code(response) == "301"
    headers, _body = _headers_and_body(response)
    assert "Location: /view/memory#handoffs" in headers


def test_work_view_browser_check_answers_operator_questions(dashboard_server, monkeypatch):
    from brigade.center_cmd.dashboard.views import work as work_view

    def fake_fetch(target):
        del target
        return {
            "ready": {
                "ready_count": 2,
                "blocked_count": 0,
                "ready": [
                    {"id": "task-a", "text": "Browser check ready A", "status": "pending"},
                    {"id": "task-b", "text": "Browser check ready B", "status": "pending"},
                ],
                "blocked": [],
                "edges": [],
                "waves": [
                    {
                        "index": 0,
                        "exclusive": True,
                        "tasks": [{"id": "task-a", "text": "Browser check ready A"}],
                    },
                    {
                        "index": 1,
                        "exclusive": True,
                        "tasks": [{"id": "task-b", "text": "Browser check ready B"}],
                    },
                ],
            },
            "tasks": {"tasks": []},
            "runs": {
                "runs": [
                    {
                        "run_id": "run-browser",
                        "status": "completed",
                        "started_at": "2026-08-13T12:00:00Z",
                        "commands": [{"command": "pytest -q", "exit_code": 0}],
                    }
                ]
            },
        }

    monkeypatch.setattr(work_view, "fetch", fake_fetch)
    host, port = dashboard_server.server_address
    response = _raw_request(dashboard_server, f"{host}:{port}", path="/view/work")
    assert _status_code(response) == "200"
    _, body = _headers_and_body(response)
    assert "failed to render" not in body
    assert ">Work<" in body
    assert 'id="ready"' in body
    assert 'id="claims"' in body
    assert 'id="recent"' in body
    assert "Browser check ready A" in body
    assert "pytest -q" in body
    assert ">Graph<" not in body
    assert ">Waves<" not in body
    assert ">Claims<" not in body


def test_rendered_page_has_no_inline_handlers_and_nonce_matches(dashboard_server):
    host, port = dashboard_server.server_address
    response = _raw_request(dashboard_server, f"{host}:{port}", path="/view/status")
    headers, body = _headers_and_body(response)

    assert re.search(r"\bon\w+\s*=", body, re.IGNORECASE) is None

    nonce_match = re.search(r"script-src 'nonce-([^'\s]+)'", headers)
    assert nonce_match is not None
    nonce = nonce_match.group(1)
    assert f'<script nonce="{nonce}"' in body
    assert f'<style nonce="{nonce}"' in body


def test_rendered_page_includes_polling_reload(dashboard_server):
    host, port = dashboard_server.server_address
    response = _raw_request(dashboard_server, f"{host}:{port}", path="/view/status")
    _, body = _headers_and_body(response)
    assert "location.reload" in body


def test_view_exception_degrades_to_panel_not_500(monkeypatch):
    """A raising view must render an inline error, never a traceback page."""
    from brigade.center_cmd.dashboard.views import status_grid

    def _boom(target):
        raise RuntimeError("fixture failure")

    monkeypatch.setattr(status_grid, "fetch", _boom)

    server = _make_server(host="127.0.0.1", port=0, token=None, allowed_hosts=None)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        response = _raw_request(server, f"{host}:{port}", path="/view/status")
        headers, body = _headers_and_body(response)
        assert _status_code(response) == "200"
        assert "default-src 'none'" in headers
        assert "failed to render" in body
        assert "Traceback" not in body
        assert "fixture failure" not in body
    finally:
        server.shutdown()
        server.server_close()


# --- Memory Operations view (#875 Task 3) ---


def _inventory_item(index: int, **overrides: object) -> dict:
    item = {
        "id": f"card:memory/cards/card-{index:04d}.md",
        "title": f"Card {index:04d}",
        "canonical_path": f"memory/cards/card-{index:04d}.md",
        "logical_destination": "cards",
        "store_type": "card",
        "category": "ops",
        "tags": ["alpha", "beta"],
        "created_at": "2026-01-01",
        "updated_at": "2026-01-02",
        "last_reviewed": "2026-01-03",
        "fresh_until": "2099-01-01",
        "freshness": "fresh",
        "review_state": "reviewed",
        "evidence_state": "present",
        "source_harness": "claude",
        "source_handoff": ".claude/memory-handoffs/example.md",
        "owning_workflow": "ingest",
        "last_mutation": {
            "workflow": "ingest",
            "receipt": {
                "evidence_state": "present",
                "status": "ok",
                "run_id": f"run-{index:04d}",
                "completed_at": "2026-01-04T00:00:00Z",
                "duration_seconds": 1.5,
            },
        },
        "care": {"issues": ["stale"], "queued_action": "refresh"},
    }
    item.update(overrides)
    return item


def _topology_payload(**overrides: object) -> dict:
    payload = {
        "schema_version": 1,
        "schema": {"name": "memory-topology", "version": 1},
        "nodes": [
            {
                "id": "harness:claude",
                "kind": "harness",
                "label": "claude",
                "owner": "harness",
                "authority": "queue_producer",
                "state": "enabled",
                "path": None,
                "schedule": None,
                "counts": {},
                "latest_run": None,
                "next_action": "write handoff",
            },
            {
                "id": "inbox:claude",
                "kind": "inbox",
                "label": "claude handoff inbox",
                "owner": "claude",
                "authority": "queue_producer",
                "state": "present",
                "path": ".claude/memory-handoffs",
                "schedule": None,
                "counts": {},
                "latest_run": None,
                "next_action": None,
            },
            {
                "id": "stage:lint",
                "kind": "stage",
                "label": "lint",
                "owner": "brigade",
                "authority": "read_only",
                "state": "enabled",
                "path": None,
                "schedule": None,
                "counts": {},
                "latest_run": None,
                "next_action": None,
            },
            {
                "id": "stage:ingest",
                "kind": "stage",
                "label": "ingest",
                "owner": "brigade",
                "authority": "canonical_writer",
                "state": "enabled",
                "path": None,
                "schedule": None,
                "counts": {},
                "latest_run": None,
                "next_action": None,
            },
            {
                "id": "canonical:cards",
                "kind": "canonical",
                "label": "Memory cards",
                "owner": "canonical memory system",
                "authority": "canonical_writer",
                "state": "canonical",
                "path": "memory/cards",
                "schedule": None,
                "counts": {},
                "latest_run": None,
                "next_action": None,
            },
            {
                "id": "care_job:daily-care",
                "kind": "care_job",
                "label": "daily-care",
                "owner": "scheduler",
                "authority": "queue_producer",
                "state": "disabled",
                "path": None,
                "schedule": {"source": "brigade_care", "cadence": "daily"},
                "counts": {},
                "latest_run": None,
                "next_action": "enable care entry",
            },
        ],
        "edges": [
            {
                "id": "edge:claude:emit",
                "from": "harness:claude",
                "to": "inbox:claude",
                "flow": "emit",
                "authority": "queue_producer",
                "writer_component": None,
                "enabled": True,
                "latest_receipt": {"evidence_state": "missing"},
            },
            {
                "id": "edge:claude:lint",
                "from": "inbox:claude",
                "to": "stage:lint",
                "flow": "validate",
                "authority": "read_only",
                "writer_component": None,
                "enabled": True,
                "latest_receipt": {"evidence_state": "missing"},
            },
            {
                "id": "edge:lint:ingest",
                "from": "stage:lint",
                "to": "stage:ingest",
                "flow": "advance",
                "authority": "read_only",
                "writer_component": None,
                "enabled": True,
                "latest_receipt": {"evidence_state": "missing"},
            },
            {
                "id": "edge:ingest:write:cards",
                "from": "stage:ingest",
                "to": "canonical:cards",
                "flow": "write",
                "authority": "canonical_writer",
                "writer_component": "ingest",
                "enabled": True,
                "latest_receipt": {
                    "evidence_state": "present",
                    "status": "ok",
                    "run_id": "ingest-1",
                    "completed_at": "2026-01-04T00:00:00Z",
                    "duration_seconds": 2.25,
                },
            },
        ],
        "paths": [
            {
                "id": "path:claude",
                "harness": "claude",
                "node_ids": [
                    "harness:claude",
                    "inbox:claude",
                    "stage:lint",
                    "stage:ingest",
                    "canonical:cards",
                ],
                "edge_ids": [
                    "edge:claude:emit",
                    "edge:claude:lint",
                    "edge:lint:ingest",
                    "edge:ingest:write:cards",
                ],
            }
        ],
        "flags": [
            {
                "kind": "duplicate_enabled_writer",
                "destination": "canonical:cards",
                "writers": ["ingest", "manual"],
                "detail": "2 enabled writers for canonical:cards",
            },
            {
                "kind": "cycle",
                "nodes": ["a", "b", "a"],
                "detail": "a -> b -> a",
            },
            {
                "kind": "missing_owner",
                "node_id": "adapter:unknown",
                "detail": "owner unknown for adapter:unknown",
            },
            {
                "kind": "missing_closeout",
                "detail": "refresh queue has items but no memory-care closeout",
            },
            {
                "kind": "stale_receipt",
                "edge_id": "edge:ingest:write:cards",
                "writer_component": "ingest",
                "detail": "receipt older than 1d for cadence",
            },
            {
                "kind": "duplicate_enabled_queue_producer",
                "destination": "stage:refresh_queue",
                "writer_component": "care-scan",
                "detail": "2 enabled queue producers for stage:refresh_queue",
            },
        ],
        "health": {
            "care_scan": {"status": "ok", "issue_count": 1},
            "refresh_queue": {"status": "warn", "count": 3},
            "handoff_backlog": {"status": "warn", "pending": 2, "draft_pending": 1, "draft_reviewed": 0},
            "quarantine": {"status": "unknown", "count": None},
            "closeout": {"status": "missing", "closeout_id": None, "created_at": None},
            "evidence_projection": {"status": "ok", "healthy": True, "capability": "memory"},
        },
    }
    payload.update(overrides)
    return payload


def _stub_memory_ops_json(monkeypatch, *, topology=None, inventory_pages=None):
    """Route data.run_json for memory topology/inventory; record every call."""
    calls: list[list[str]] = []
    topology = _topology_payload() if topology is None else topology
    if inventory_pages is None:
        inventory_pages = [
            {
                "items": [_inventory_item(0)],
                "pagination": {
                    "offset": 0,
                    "limit": 500,
                    "total": 1,
                    "returned": 1,
                    "has_more": False,
                    "next_offset": None,
                },
            }
        ]

    def fake_run_json(target: Path, args, **kwargs):
        del target, kwargs
        argv = [str(part) for part in args]
        calls.append(argv)
        if argv[:2] == ["memory", "topology"]:
            return topology
        if argv[:2] == ["memory", "inventory"]:
            offset = 0
            if "--offset" in argv:
                offset = int(argv[argv.index("--offset") + 1])
            for page in inventory_pages:
                page_offset = page.get("pagination", {}).get("offset", 0)
                if page_offset == offset:
                    return page
            return {"error": f"unexpected inventory offset {offset}"}
        return {"error": f"unexpected command {argv}"}

    monkeypatch.setattr(data, "run_json", fake_run_json)
    return calls


def test_memory_operations_replaces_cards_and_uses_only_contracts(monkeypatch, tmp_target):
    names = [module.NAME for module in all_views()]
    titles = [module.TITLE for module in all_views()]
    assert "cards" not in names
    assert "Cards" not in titles
    view = view_by_name("memory")
    assert view is not None
    assert view.TITLE == "Memory Operations"

    calls = _stub_memory_ops_json(monkeypatch)
    payload = view.fetch(tmp_target)
    assert isinstance(payload, dict)
    assert {tuple(call[:2]) for call in calls} <= {
        ("memory", "topology"),
        ("memory", "inventory"),
        ("handoff", "lint"),
    }
    assert ("memory", "topology") in {tuple(call[:2]) for call in calls}
    assert ("memory", "inventory") in {tuple(call[:2]) for call in calls}
    assert all("search" not in call for call in calls)


def test_memory_operations_fetches_second_inventory_page_and_exposes_501st_item(monkeypatch, tmp_target):
    page1_items = [_inventory_item(i) for i in range(500)]
    page2_items = [_inventory_item(i) for i in range(500, 505)]
    inventory_pages = [
        {
            "items": page1_items,
            "pagination": {
                "offset": 0,
                "limit": 500,
                "total": 505,
                "returned": 500,
                "has_more": True,
                "next_offset": 500,
            },
        },
        {
            "items": page2_items,
            "pagination": {
                "offset": 500,
                "limit": 500,
                "total": 505,
                "returned": 5,
                "has_more": False,
                "next_offset": None,
            },
        },
    ]
    calls = _stub_memory_ops_json(monkeypatch, inventory_pages=inventory_pages)
    view = view_by_name("memory")
    assert view is not None
    payload = view.fetch(tmp_target)
    html = view.render(payload, nonce="test-nonce")

    inventory_offsets = []
    for call in calls:
        if call[:2] == ["memory", "inventory"] and "--offset" in call:
            inventory_offsets.append(int(call[call.index("--offset") + 1]))
        elif call[:2] == ["memory", "inventory"]:
            inventory_offsets.append(0)
    assert 0 in inventory_offsets
    assert 500 in inventory_offsets
    assert "memory/cards/card-0500.md" in html
    assert "Card 0500" in html
    assert 'data-mo-total="505"' in html or "505" in html


def test_memory_operations_renders_topology_health_ownership_and_inventory_fields(monkeypatch, tmp_target):
    calls = _stub_memory_ops_json(monkeypatch)
    view = view_by_name("memory")
    assert view is not None
    payload = view.fetch(tmp_target)
    html = view.render(payload, nonce="test-nonce")
    del calls

    assert 'class="mo-summary"' in html or "data-mo-summary" in html
    assert "<svg" in html and ('class="mo-pipeline"' in html or "data-mo-pipeline" in html)
    assert "mo-health-tile" in html or "mo-health-tiles" in html

    for label in (
        "Care scan",
        "Refresh queue",
        "Handoff backlog",
        "Quarantine",
        "Closeout",
        "Evidence projection",
    ):
        assert label in html

    assert "Claude" in html
    assert "Lint" in html
    assert "Ingest" in html or "Ingestion" in html
    assert "Memory cards" in html or "Canonical cards" in html
    # Raw receipt / edge jargon stays in details or title attributes, not primary copy.
    primary = html.split("<details", 1)[0]
    assert "ingest-1" not in primary
    assert "stage:lint" not in primary
    assert "edge:ingest:write:cards" not in primary
    # Flag kinds remain discoverable (details ok).
    assert "duplicate" in html.lower()
    assert "missing" in html.lower()
    assert "cycle" in html.lower() or "loop" in html.lower()

    assert "Card 0000" in html
    assert "memory/cards/card-0000.md" in html
    assert "ops" in html
    assert "alpha" in html
    assert "fresh" in html
    assert "reviewed" in html
    assert "present" in html
    assert "claude" in html.lower()
    assert "stale" in html
    assert "refresh" in html.lower()
    assert "Topology" in html
    assert "Cards" in html
    assert 'data-mo-mode="topology"' in html
    assert 'data-mo-mode="cards"' in html
    assert 'data-mo-mode="handoffs"' in html


def test_memory_operations_cards_density_matches_inventory_contract(monkeypatch, tmp_target):
    inventory_pages = [
        {
            "master_index": {"canonical_path": "MEMORY.md", "size_bytes": 2048},
            "items": [
                _inventory_item(
                    0,
                    title="Architecture card",
                    category="architecture",
                    tags=["system", "design"],
                    updated_at=(date.today() - timedelta(days=3)).isoformat(),
                    size_bytes=401,
                ),
                _inventory_item(
                    1,
                    title="Workflow card",
                    category="workflow",
                    tags=["system", "run"],
                    updated_at="2026-07-03",
                    size_bytes=800,
                ),
            ],
            "pagination": {
                "offset": 0,
                "limit": 500,
                "total": 2,
                "returned": 2,
                "has_more": False,
                "next_offset": None,
            },
        }
    ]
    _stub_memory_ops_json(monkeypatch, inventory_pages=inventory_pages)
    view = view_by_name("memory")
    assert view is not None

    rendered = view.render(view.fetch(tmp_target), nonce="density")

    assert 'data-mo-mode="cards"' in rendered
    assert 'data-mo-mode="handoffs"' in rendered
    assert 'data-mo-mode="cards" aria-selected="true"' in rendered
    assert 'data-mo-panel="topology" role="tabpanel" aria-labelledby="mo-tab-topology" hidden' in rendered
    assert 'class="mo-corpus-stats"' in rendered
    assert "2 cards" in rendered
    assert "2 categories" in rendered
    assert "~301 tokens" in rendered
    assert 'class="mo-master-index"' in rendered
    assert "MEMORY.md" in rendered and "2 KiB" in rendered
    assert 'class="mo-filter-chip"' in rendered
    assert "architecture (1)" in rendered
    assert "system (2)" in rendered
    assert "<select" not in rendered
    assert 'class="mo-category-section"' in rendered
    assert "ARCHITECTURE (1)" in rendered
    assert 'class="mo-card-row"' in rendered
    assert 'class="mo-card-detail"' in rendered
    assert ">3d<" in rendered
    assert "0h ago" not in rendered


def test_memory_operations_humanizes_card_age():
    from brigade.center_cmd.dashboard.views import memory_operations

    assert memory_operations._human_age("2026-08-11", today=date(2026, 8, 14)) == "3d"
    assert memory_operations._human_age("2026-07-03", today=date(2026, 8, 14)) == "6w"


def test_memory_view_browser_check_shows_dense_cards_and_handoffs(dashboard_server, monkeypatch):
    from brigade.center_cmd.dashboard.views import memory_operations

    def fake_fetch(target):
        del target
        return {
            "topology": _topology_payload(),
            "inventory": {
                "items": [
                    _inventory_item(
                        0,
                        title="Browser density card",
                        category="workflow",
                        tags=["browser", "memory"],
                        size_bytes=480,
                        updated_at="2026-08-11",
                    )
                ],
                "total": 1,
                "master_index": {"canonical_path": "MEMORY.md", "size_bytes": 1024},
            },
            "handoffs": {"count": 0, "results": []},
        }

    monkeypatch.setattr(memory_operations, "fetch", fake_fetch)
    host, port = dashboard_server.server_address
    response = _raw_request(dashboard_server, f"{host}:{port}", path="/view/memory")
    assert _status_code(response) == "200"
    _headers, body = _headers_and_body(response)
    assert 'data-mo-mode="cards"' in body
    assert 'data-mo-mode="handoffs"' in body
    assert 'class="mo-corpus-stats"' in body
    assert 'class="mo-master-index"' in body
    assert 'class="mo-card-row"' in body
    assert "Browser density card" in body


def test_memory_operations_escapes_hostile_labels_and_has_no_inline_handlers(monkeypatch, tmp_target):
    hostile = '<img src=x onerror=alert(1) data-x="&">'
    topology = _topology_payload(
        nodes=[
            {
                "id": hostile,
                "kind": "harness",
                "label": hostile,
                "owner": hostile,
                "authority": hostile,
                "state": hostile,
                "path": hostile,
                "schedule": {"source": hostile, "cadence": hostile},
                "counts": {"pending": hostile},
                "latest_run": hostile,
                "next_action": hostile,
            }
        ],
        edges=[
            {
                "id": hostile,
                "from": hostile,
                "to": hostile,
                "flow": hostile,
                "authority": hostile,
                "writer_component": hostile,
                "enabled": True,
                "latest_receipt": {
                    "evidence_state": hostile,
                    "status": hostile,
                    "run_id": hostile,
                    "completed_at": hostile,
                    "duration_seconds": hostile,
                },
            }
        ],
        paths=[{"id": hostile, "harness": hostile, "node_ids": [hostile], "edge_ids": [hostile]}],
        flags=[{"kind": hostile, "detail": hostile, "destination": hostile, "writers": [hostile]}],
        health={
            "care_scan": {"status": hostile, "issue_count": hostile},
            "refresh_queue": {"status": hostile, "count": hostile},
            "handoff_backlog": {
                "status": hostile,
                "pending": hostile,
                "draft_pending": hostile,
                "draft_reviewed": hostile,
            },
            "quarantine": {"status": hostile, "count": hostile},
            "closeout": {"status": hostile, "closeout_id": hostile, "created_at": hostile},
            "evidence_projection": {
                "status": hostile,
                "healthy": hostile,
                "capability": hostile,
            },
        },
    )
    inventory_pages = [
        {
            "items": [
                _inventory_item(
                    0,
                    title=hostile,
                    id=hostile,
                    canonical_path=hostile,
                    logical_destination=hostile,
                    store_type=hostile,
                    category=hostile,
                    tags=[hostile],
                    created_at=hostile,
                    updated_at=hostile,
                    last_reviewed=hostile,
                    fresh_until=hostile,
                    freshness=hostile,
                    review_state=hostile,
                    evidence_state=hostile,
                    source_harness=hostile,
                    source_handoff=hostile,
                    owning_workflow=hostile,
                    last_mutation={
                        "workflow": hostile,
                        "receipt": {
                            "evidence_state": hostile,
                            "status": hostile,
                            "run_id": hostile,
                            "completed_at": hostile,
                            "duration_seconds": hostile,
                        },
                    },
                    care={"issues": [hostile], "queued_action": hostile},
                )
            ],
            "pagination": {
                "offset": 0,
                "limit": 500,
                "total": 1,
                "returned": 1,
                "has_more": False,
                "next_offset": None,
            },
        }
    ]
    _stub_memory_ops_json(monkeypatch, topology=topology, inventory_pages=inventory_pages)
    view = view_by_name("memory")
    assert view is not None
    html = view.render(view.fetch(tmp_target), nonce="test-nonce")

    assert "<img" not in html
    assert "&lt;img" in html
    # Our only scripts are nonced delegated modules; no attacker <script> tags.
    assert re.search(r"<script(?![^>]*\bnonce=)", html, re.IGNORECASE) is None
    # Real inline handlers are attributes named on*; ignore substrings inside
    # quoted/escaped attribute values.
    for tag in re.findall(r"<[^>]*>", html):
        stripped = re.sub(r'"[^"]*"', '""', tag)
        stripped = re.sub(r"'[^']*'", "''", stripped)
        for name in re.findall(r"\s([A-Za-z_:][\w:.-]*)\s*=", stripped):
            assert not re.fullmatch(r"on\w+", name, re.IGNORECASE), tag
    assert "\u2014" not in html and "\u2013" not in html


def test_memory_operations_malformed_pagination_and_independent_errors_degrade(monkeypatch, tmp_target):
    view = view_by_name("memory")
    assert view is not None

    # Topology fails; inventory succeeds.
    calls = _stub_memory_ops_json(
        monkeypatch,
        topology={"error": "topology unavailable"},
        inventory_pages=[
            {
                "items": [_inventory_item(1, title="Survives topology failure")],
                "pagination": {
                    "offset": 0,
                    "limit": 500,
                    "total": 1,
                    "returned": 1,
                    "has_more": False,
                    "next_offset": None,
                },
            }
        ],
    )
    html = view.render(view.fetch(tmp_target), nonce="n")
    assert "topology unavailable" in html
    assert "Survives topology failure" in html
    assert any(call[:2] == ["memory", "inventory"] for call in calls)

    # Inventory fails; topology succeeds.
    _stub_memory_ops_json(
        monkeypatch,
        topology=_topology_payload(),
        inventory_pages=[{"error": "inventory unavailable", "pagination": {}, "items": []}],
    )

    def fake_inventory_error(target, args, **kwargs):
        del target, kwargs
        argv = [str(part) for part in args]
        if argv[:2] == ["memory", "topology"]:
            return _topology_payload()
        return {"error": "inventory unavailable"}

    monkeypatch.setattr(data, "run_json", fake_inventory_error)
    html = view.render(view.fetch(tmp_target), nonce="n")
    assert "inventory unavailable" in html
    assert "Care scan" in html or "care scan" in html.lower()

    # Non-progress / malformed pagination must stop without hanging and keep first page.
    pages = [
        {
            "items": [_inventory_item(0, title="First page kept")],
            "pagination": {
                "offset": 0,
                "limit": 500,
                "total": 999,
                "returned": 1,
                "has_more": True,
                "next_offset": 0,  # non-progress
            },
        }
    ]
    call_count = {"n": 0}

    def fake_nonprogress(target, args, **kwargs):
        del target, kwargs
        argv = [str(part) for part in args]
        if argv[:2] == ["memory", "topology"]:
            return _topology_payload()
        call_count["n"] += 1
        if call_count["n"] > 5:
            raise AssertionError("pagination loop did not stop on non-progress")
        return pages[0]

    monkeypatch.setattr(data, "run_json", fake_nonprogress)
    html = view.render(view.fetch(tmp_target), nonce="n")
    assert "First page kept" in html
    assert call_count["n"] <= 2


def test_memory_operations_fetch_writes_no_dashboard_state(monkeypatch, tmp_target):
    view = view_by_name("memory")
    assert view is not None
    _stub_memory_ops_json(monkeypatch)
    before = {path.relative_to(tmp_target): path.stat().st_mtime_ns for path in tmp_target.rglob("*") if path.is_file()}
    view.fetch(tmp_target)
    after_paths = {path.relative_to(tmp_target) for path in tmp_target.rglob("*") if path.is_file()}
    assert after_paths == set(before)
    for rel, mtime in before.items():
        assert (tmp_target / rel).stat().st_mtime_ns == mtime

    html = view.render(view.fetch(tmp_target), nonce="n")
    assert 'method="post"' not in html.lower()
    assert "/api/" not in html
    assert "localStorage" not in html
    assert "indexedDB" not in html


def test_memory_operations_rendered_copy_has_no_em_en_dashes_or_inline_handlers(monkeypatch, tmp_target):
    """Rendered-copy preflight for frontend-taste contrast/copy rules on this view."""
    _stub_memory_ops_json(monkeypatch)
    view = view_by_name("memory")
    assert view is not None
    html = view.render(view.fetch(tmp_target), nonce="preflight-nonce")
    assert "\u2014" not in html and "\u2013" not in html
    assert re.search(r"<script(?![^>]*\bnonce=)", html, re.IGNORECASE) is None
    for tag in re.findall(r"<[^>]*>", html):
        stripped = re.sub(r'"[^"]*"', '""', tag)
        stripped = re.sub(r"'[^']*'", "''", stripped)
        for name in re.findall(r"\s([A-Za-z_:][\w:.-]*)\s*=", stripped):
            assert not re.fullmatch(r"on\w+", name, re.IGNORECASE), tag
    source = Path(view.__file__).read_text(encoding="utf-8")
    assert "\u2014" not in source and "\u2013" not in source
    assert "onclick=" not in source
    registry = Path("src/brigade/center_cmd/dashboard/views/__init__.py").read_text(encoding="utf-8")
    assert "\u2014" not in registry and "\u2013" not in registry


def _fanout_topology_two_harnesses() -> dict:
    """Two harnesses fan out to five canonical stores via real edge_ids only."""
    canonical = ["cards", "rules", "tools", "user", "learnings"]
    nodes = [
        {
            "id": "harness:claude",
            "kind": "harness",
            "label": "claude",
            "owner": "claude",
            "authority": "queue_producer",
            "state": "present",
            "path": None,
            "schedule": None,
            "counts": {},
            "latest_run": {
                "status": "LRSTAT_ok_sentinel",
                "run_id": "LRRUN_claude_sentinel",
                "completed_at": "LRDONE_2026-08-01T12:00:00Z_sentinel",
                "duration_seconds": 3.25,
                "evidence_state": "LREVID_present_sentinel",
            },
            "next_action": "write handoff",
        },
        {
            "id": "inbox:claude",
            "kind": "inbox",
            "label": "claude inbox",
            "owner": "claude",
            "authority": "queue_producer",
            "state": "present",
            "path": ".claude/memory-handoffs",
            "schedule": None,
            "counts": {"pending": 1},
            "latest_run": None,
            "next_action": None,
        },
        {
            "id": "harness:codex",
            "kind": "harness",
            "label": "codex",
            "owner": "codex",
            "authority": "queue_producer",
            "state": "present",
            "path": None,
            "schedule": None,
            "counts": {},
            "latest_run": {
                "status": None,
                "run_id": None,
                "completed_at": None,
                "duration_seconds": None,
                "evidence_state": None,
            },
            "next_action": None,
        },
        {
            "id": "inbox:codex",
            "kind": "inbox",
            "label": "codex inbox",
            "owner": "codex",
            "authority": "queue_producer",
            "state": "present",
            "path": ".codex/memory-handoffs",
            "schedule": None,
            "counts": {},
            "latest_run": None,
            "next_action": None,
        },
        {
            "id": "stage:lint",
            "kind": "stage",
            "label": "lint",
            "owner": "brigade",
            "authority": "read_only",
            "state": "enabled",
            "path": None,
            "schedule": None,
            "counts": {},
            "latest_run": None,
            "next_action": None,
        },
        {
            "id": "stage:ingest",
            "kind": "stage",
            "label": "ingest",
            "owner": "brigade",
            "authority": "canonical_writer",
            "state": "enabled",
            "path": None,
            "schedule": None,
            "counts": {},
            "latest_run": None,
            "next_action": None,
        },
    ]
    for key in canonical:
        nodes.append(
            {
                "id": f"canonical:{key}",
                "kind": "canonical",
                "label": key,
                "owner": "canonical memory system",
                "authority": "canonical_writer",
                "state": "canonical",
                "path": f"memory/{key}",
                "schedule": None,
                "counts": {},
                "latest_run": None,
                "next_action": None,
            }
        )
    edges = [
        {
            "id": "edge:claude:emit",
            "from": "harness:claude",
            "to": "inbox:claude",
            "flow": "emit",
            "authority": "queue_producer",
            "writer_component": None,
            "enabled": True,
            "latest_receipt": {"evidence_state": "missing"},
        },
        {
            "id": "edge:claude:lint",
            "from": "inbox:claude",
            "to": "stage:lint",
            "flow": "validate",
            "authority": "read_only",
            "writer_component": None,
            "enabled": True,
            "latest_receipt": {"evidence_state": "missing"},
        },
        {
            "id": "edge:codex:emit",
            "from": "harness:codex",
            "to": "inbox:codex",
            "flow": "emit",
            "authority": "queue_producer",
            "writer_component": None,
            "enabled": True,
            "latest_receipt": {"evidence_state": "missing"},
        },
        {
            "id": "edge:codex:lint",
            "from": "inbox:codex",
            "to": "stage:lint",
            "flow": "validate",
            "authority": "read_only",
            "writer_component": None,
            "enabled": True,
            "latest_receipt": {"evidence_state": "missing"},
        },
        {
            "id": "edge:lint:ingest",
            "from": "stage:lint",
            "to": "stage:ingest",
            "flow": "advance",
            "authority": "read_only",
            "writer_component": None,
            "enabled": True,
            "latest_receipt": {"evidence_state": "missing"},
        },
    ]
    for key in canonical:
        edges.append(
            {
                "id": f"edge:ingest:write:{key}",
                "from": "stage:ingest",
                "to": f"canonical:{key}",
                "flow": "write",
                "authority": "canonical_writer",
                "writer_component": "ingest",
                "enabled": True,
                "latest_receipt": {
                    "evidence_state": "present",
                    "status": "ok",
                    "run_id": f"ingest-{key}",
                    "completed_at": "2026-01-04T00:00:00Z",
                    "duration_seconds": 1.0,
                },
            }
        )
    shared_edge_ids = ["edge:lint:ingest", *[f"edge:ingest:write:{key}" for key in canonical]]
    claude_edge_ids = ["edge:claude:emit", "edge:claude:lint", *shared_edge_ids]
    codex_edge_ids = ["edge:codex:emit", "edge:codex:lint", *shared_edge_ids]
    # Contract path node_ids intentionally list fan-out destinations after ingest;
    # joining them with arrows would invent phantom canonical->canonical edges.
    node_ids_claude = [
        "harness:claude",
        "inbox:claude",
        "stage:lint",
        "stage:ingest",
        *[f"canonical:{key}" for key in canonical],
    ]
    node_ids_codex = [
        "harness:codex",
        "inbox:codex",
        "stage:lint",
        "stage:ingest",
        *[f"canonical:{key}" for key in canonical],
    ]
    return _topology_payload(
        nodes=nodes,
        edges=edges,
        paths=[
            {
                "id": "path:claude",
                "harness": "claude",
                "node_ids": node_ids_claude,
                "edge_ids": claude_edge_ids,
            },
            {
                "id": "path:codex",
                "harness": "codex",
                "node_ids": node_ids_codex,
                "edge_ids": codex_edge_ids,
            },
        ],
    )


def test_memory_operations_topology_paths_use_edges_not_node_id_chains(monkeypatch, tmp_target):
    topology = _fanout_topology_two_harnesses()
    _stub_memory_ops_json(monkeypatch, topology=topology)
    view = view_by_name("memory")
    assert view is not None
    html = view.render(view.fetch(tmp_target), nonce="topo-fidelity")

    assert "<svg" in html
    assert "Claude" in html and "Codex" in html
    assert "Lint" in html
    assert "Ingest" in html or "Ingestion" in html
    for store in ("cards", "rules", "tools", "user", "learnings"):
        assert store in html.lower()

    # Edge contract ids remain available for debug (title/details), not as primary bullets.
    for edge_id in (
        "edge:claude:emit",
        "edge:claude:lint",
        "edge:codex:emit",
        "edge:lint:ingest",
        "edge:ingest:write:cards",
        "edge:ingest:write:rules",
        "edge:ingest:write:tools",
        "edge:ingest:write:user",
        "edge:ingest:write:learnings",
    ):
        assert edge_id in html, edge_id

    # Phantom connections that node_id chaining invents must be absent.
    for phantom in (
        "canonical:cards -> canonical:rules",
        "canonical:rules -> canonical:tools",
        "canonical:tools -> canonical:user",
        "canonical:user -> canonical:learnings",
        "canonical:learnings -> harness:codex",
        "canonical:learnings -> inbox:codex",
        "harness:claude -> harness:codex",
        "inbox:claude -> harness:codex",
        "canonical:cards -> canonical:learnings",
    ):
        escaped = phantom.replace(" -> ", " -&gt; ")
        assert phantom not in html and escaped not in html, phantom


def test_memory_operations_renders_complete_latest_run_and_nulls_as_dash(monkeypatch, tmp_target):
    topology = _fanout_topology_two_harnesses()
    _stub_memory_ops_json(monkeypatch, topology=topology)
    view = view_by_name("memory")
    assert view is not None
    html = view.render(view.fetch(tmp_target), nonce="latest-run")

    for sentinel in (
        "LRSTAT_ok_sentinel",
        "LRRUN_claude_sentinel",
        "LRDONE_2026-08-01T12:00:00Z_sentinel",
        "3.25",
        "LREVID_present_sentinel",
    ):
        assert sentinel in html, sentinel
    # Explicit null latest_run fields on harness:codex must render as '-', never 'None'.
    assert "None" not in html
    assert "write handoff" in html


def test_memory_operations_renders_complete_last_mutation_receipt(monkeypatch, tmp_target):
    item = _inventory_item(
        7,
        title="MUTTITLE_unique_sentinel",
        last_mutation={
            "workflow": "MUTWF_ingest_sentinel",
            "receipt": {
                "evidence_state": "MUTEVID_present_sentinel",
                "status": "MUTSTAT_ok_sentinel",
                "run_id": "MUTRUN_xyz_sentinel",
                "completed_at": "MUTDONE_2026-08-02T00:00:00Z_sentinel",
                "duration_seconds": 12.34,
            },
        },
    )
    inventory_pages = [
        {
            "items": [item],
            "pagination": {
                "offset": 0,
                "limit": 500,
                "total": 1,
                "returned": 1,
                "has_more": False,
                "next_offset": None,
            },
        }
    ]
    _stub_memory_ops_json(monkeypatch, inventory_pages=inventory_pages)
    view = view_by_name("memory")
    assert view is not None
    html = view.render(view.fetch(tmp_target), nonce="mutation")
    assert "MUTTITLE_unique_sentinel" in html
    for sentinel in (
        "MUTWF_ingest_sentinel",
        "MUTEVID_present_sentinel",
        "MUTSTAT_ok_sentinel",
        "MUTRUN_xyz_sentinel",
        "MUTDONE_2026-08-02T00:00:00Z_sentinel",
        "12.34",
    ):
        assert sentinel in html, sentinel


def test_memory_operations_partial_inventory_warns_with_loaded_and_contract_total(monkeypatch, tmp_target):
    view = view_by_name("memory")
    assert view is not None

    # Non-progress pagination with has_more=true: keep items, warn partial, keep contract total.
    pages = [
        {
            "items": [
                _inventory_item(0, title="PARTIAL_FIRST_kept_sentinel"),
                _inventory_item(1, title="PARTIAL_SECOND_kept_sentinel"),
            ],
            "pagination": {
                "offset": 0,
                "limit": 500,
                "total": 999,
                "returned": 2,
                "has_more": True,
                "next_offset": 0,
            },
        }
    ]
    call_count = {"n": 0}

    def fake_nonprogress(target, args, **kwargs):
        del target, kwargs
        argv = [str(part) for part in args]
        if argv[:2] == ["memory", "topology"]:
            return _topology_payload()
        call_count["n"] += 1
        if call_count["n"] > 5:
            raise AssertionError("pagination loop did not stop on non-progress")
        return pages[0]

    monkeypatch.setattr(data, "run_json", fake_nonprogress)
    payload = view.fetch(tmp_target)
    html = view.render(payload, nonce="partial-warn")
    assert "PARTIAL_FIRST_kept_sentinel" in html
    assert "PARTIAL_SECOND_kept_sentinel" in html
    assert "partial" in html.lower()
    assert "2" in html
    assert "999" in html
    inv = payload["inventory"]
    assert inv.get("partial") is True
    assert inv.get("warning")
    assert "partial" in str(inv.get("warning")).lower()
    assert inv.get("contract_total") == 999
    assert len(inv.get("items") or []) == 2
    # Must not relabel loaded length as the complete contract total.
    assert inv.get("total") != 2
    assert 'data-mo-total="2"' not in html

    # Explicit page command failure after partial results also warns.
    page1 = {
        "items": [_inventory_item(0, title="AFTERFAIL_kept_sentinel")],
        "pagination": {
            "offset": 0,
            "limit": 500,
            "total": 777,
            "returned": 1,
            "has_more": True,
            "next_offset": 500,
        },
    }

    def fake_second_page_error(target, args, **kwargs):
        del target, kwargs
        argv = [str(part) for part in args]
        if argv[:2] == ["memory", "topology"]:
            return _topology_payload()
        offset = 0
        if "--offset" in argv:
            offset = int(argv[argv.index("--offset") + 1])
        if offset == 0:
            return page1
        return {"error": "PAGEFAIL_inventory_sentinel"}

    monkeypatch.setattr(data, "run_json", fake_second_page_error)
    payload2 = view.fetch(tmp_target)
    html2 = view.render(payload2, nonce="page-fail")
    assert "AFTERFAIL_kept_sentinel" in html2
    assert "PAGEFAIL_inventory_sentinel" in html2 or "partial" in html2.lower()
    inv2 = payload2["inventory"]
    assert len(inv2.get("items") or []) == 1
    assert inv2.get("warning")
    assert inv2.get("contract_total") == 777 or "777" in html2
