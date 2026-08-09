from pathlib import Path

from brigade.center_cmd.dashboard.views import dispatch_waves


def test_fetch_uses_ready_json(monkeypatch, tmp_path):
    expected = {"ready": [], "ready_count": 0, "waves_unavailable": True}

    def fake_run_json(target: Path, args: list[str]) -> dict:
        if "--parallel-safe" in args:
            return {"error": "unrecognized arguments: --parallel-safe"}
        return expected

    monkeypatch.setattr("brigade.center_cmd.dashboard.data.run_json", fake_run_json)
    assert dispatch_waves.fetch(tmp_path)["ready_count"] == 0


def test_render_shows_seam_when_waves_absent():
    payload = {
        "ready_count": 2,
        "ready": [
            {"id": "task-a", "text": "Alpha work"},
            {"id": "task-b", "text": "Beta work"},
        ],
        "waves_unavailable": True,
        "parallel_safe": False,
    }

    fragment = dispatch_waves.render(payload, "wave-nonce")

    assert '<style nonce="wave-nonce">' in fragment
    assert "Waves unavailable" in fragment
    assert "parallel-safe" in fragment
    assert "Ready set (unpartitioned)" in fragment
    assert "task-a" in fragment
    assert "task-b" in fragment
    assert "wg-lane-preview" in fragment


def test_render_swim_lanes_from_parallel_safe_shape():
    payload = {
        "parallel_safe": True,
        "wave_count": 2,
        "partition_mode": "file_overlap",
        "partition_degraded": True,
        "partition_degraded_reason": "graphtrail binary not found",
        "waves": [
            {
                "index": 0,
                "exclusive": False,
                "task_ids": ["task-a", "task-b"],
                "tasks": [
                    {"id": "task-a", "text": "Alpha"},
                    {"id": "task-b", "text": "Beta"},
                ],
            },
            {
                "index": 1,
                "exclusive": True,
                "task_ids": ["task-c"],
                "tasks": [{"id": "task-c", "text": "Exclusive empty footprint"}],
            },
        ],
    }

    fragment = dispatch_waves.render(payload, "unused")

    assert "Parallel-safe partition" in fragment
    assert "file_overlap" in fragment
    assert "graphtrail binary not found" in fragment
    assert "Wave 0" in fragment
    assert "Wave 1 (exclusive)" in fragment
    assert "wg-lane-exclusive" in fragment
    assert "task-a" in fragment
    assert "task-c" in fragment
    assert "Waves unavailable" not in fragment


def test_render_escapes_hostile_wave_task_text():
    payload = {
        "parallel_safe": True,
        "wave_count": 1,
        "waves": [
            {
                "index": 0,
                "exclusive": False,
                "tasks": [
                    {
                        "id": "evil",
                        "text": "<script>alert(1)</script><img src=x onerror=alert(2)>",
                    }
                ],
            }
        ],
    }

    fragment = dispatch_waves.render(payload, "unused")
    assert "<script" not in fragment
    assert "<img" not in fragment
    assert "&lt;script&gt;" in fragment

    import re

    for tag in re.findall(r"<[^>]*>", fragment):
        assert "alert(" not in tag


def test_render_error_payload_degrades_safely():
    assert "boom" in dispatch_waves.render({"error": "boom"}, "unused")
