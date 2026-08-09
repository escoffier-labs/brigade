from pathlib import Path

from brigade.center_cmd.dashboard.views import ready_graph


def test_fetch_uses_ready_json_with_parallel_safe_seam(monkeypatch, tmp_path):
    calls: list[list[str]] = []

    def fake_run_json(target: Path, args: list[str]) -> dict:
        calls.append(list(args))
        if "--parallel-safe" in args:
            return {"error": "unrecognized arguments: --parallel-safe"}
        return {
            "ready": [{"id": "t-ready", "text": "Ready task", "status": "pending"}],
            "ready_count": 1,
            "blocked_count": 0,
            "edges": [],
        }

    monkeypatch.setattr("brigade.center_cmd.dashboard.data.run_json", fake_run_json)

    payload = ready_graph.fetch(tmp_path)
    assert calls[0] == ["work", "ready", "--explain", "--parallel-safe"]
    assert calls[1] == ["work", "ready", "--explain"]
    assert payload["ready_count"] == 1
    assert payload.get("waves_unavailable") is True
    assert payload.get("parallel_safe") is False


def test_fetch_keeps_parallel_safe_payload_when_available(monkeypatch, tmp_path):
    def fake_run_json(target: Path, args: list[str]) -> dict:
        assert "--parallel-safe" in args
        return {
            "ready": [],
            "ready_count": 0,
            "blocked_count": 0,
            "edges": [],
            "parallel_safe": True,
            "waves": [],
            "wave_count": 0,
        }

    monkeypatch.setattr("brigade.center_cmd.dashboard.data.run_json", fake_run_json)
    payload = ready_graph.fetch(tmp_path)
    assert payload["parallel_safe"] is True
    assert "waves_unavailable" not in payload


def test_render_builds_svg_graph_with_ready_and_blocked():
    payload = {
        "ready_count": 1,
        "blocked_count": 1,
        "cycle_count": 0,
        "ready": [{"id": "task-a", "text": "Ready alpha", "status": "pending"}],
        "blocked": [
            {
                "id": "task-b",
                "title": "Blocked beta",
                "status": "pending",
                "reason": "open_blockers",
                "blockers": [{"id": "task-a", "status": "pending", "via": ["blocks"]}],
            }
        ],
        "edges": [
            {"source": "task-a", "target": "task-b", "type": "blocks"},
            {"source": "task-parent", "target": "task-b", "type": "parent-child"},
            {"source": "task-b", "target": "task-c", "type": "discovered-from"},
        ],
    }

    fragment = ready_graph.render(payload, "graph-nonce")

    assert '<style nonce="graph-nonce">' in fragment
    assert 'style="' not in fragment
    assert 'svg class="wg-graph"' in fragment
    assert "task-a" in fragment
    assert "task-b" in fragment
    assert "wg-node-ready" in fragment
    assert "wg-node-blocked" in fragment
    assert "wg-edge-blocks" in fragment
    assert "wg-edge-parent-child" in fragment
    # discovered-from must not draw and must not invent a node
    assert "task-c" not in fragment
    assert "task-parent" in fragment
    assert ">1<" in fragment  # ready/blocked counts


def test_render_escapes_hostile_task_titles():
    payload = {
        "ready": [
            {
                "id": "evil",
                "text": "<script>alert(1)</script><img src=x onerror=alert(2)>",
                "status": "pending",
            }
        ],
        "blocked": [],
        "edges": [],
        "ready_count": 1,
        "blocked_count": 0,
        "cycle_count": 0,
    }

    fragment = ready_graph.render(payload, "unused")
    assert "<script" not in fragment
    assert "<img" not in fragment
    assert "&lt;script&gt;" in fragment

    import re

    for tag in re.findall(r"<[^>]*>", fragment):
        assert "alert(" not in tag


def test_render_error_and_empty_payloads_degrade_safely():
    assert "boom" in ready_graph.render({"error": "boom"}, "unused")
    assert "Nothing here." in ready_graph.render({"ready": [], "blocked": [], "edges": []}, "unused")
