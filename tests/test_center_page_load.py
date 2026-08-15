"""Page-load budget mechanisms: snapshot cache, scan bounds, staleness stamps."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from brigade.center_cmd import agent_activity as activity_records
from brigade.center_cmd.dashboard import render
from brigade.center_cmd.dashboard.snapshot import Snapshot, SnapshotCache
from brigade.center_cmd.serve import _make_server
from tests.test_center_serve import _headers_and_body, _raw_request, _status_code, _wait_until_ready


def test_snapshot_cache_hit_does_not_recall_loader():
    cache = SnapshotCache(ttl_seconds=30)
    calls = []

    def loader():
        calls.append(1)
        return {"ok": True, "n": len(calls)}

    first = cache.get("status", loader, wait_ms=1000)
    second = cache.get("status", loader, wait_ms=1000)
    assert first.status == "fresh"
    assert second.status == "fresh"
    assert first.payload == second.payload == {"ok": True, "n": 1}
    assert calls == [1]


def test_snapshot_cache_serves_stale_copy_while_refresh_runs():
    cache = SnapshotCache(ttl_seconds=0.05)
    phase = {"n": 0}

    def loader():
        phase["n"] += 1
        if phase["n"] == 1:
            return {"generation": 1}
        time.sleep(0.2)
        return {"generation": 2}

    first = cache.get("memory", loader, wait_ms=1000)
    time.sleep(0.08)
    second = cache.get("memory", loader, wait_ms=0)
    assert first.payload == {"generation": 1}
    assert second.payload == {"generation": 1}
    assert second.status == "stale"
    assert second.fetched_at is not None


def test_snapshot_cache_returns_loading_when_miss_exceeds_wait():
    cache = SnapshotCache(ttl_seconds=30)
    started = threading.Event()
    release = threading.Event()

    def loader():
        started.set()
        release.wait(timeout=2)
        return {"ok": True}

    snap = cache.get("agents", loader, wait_ms=50)
    assert started.wait(timeout=1)
    assert snap.status == "loading"
    assert snap.payload.get("_center_loading") is True
    release.set()


def test_freshness_banner_includes_clock_and_refresh_link():
    fetched = datetime(2026, 8, 13, 15, 34, 52, tzinfo=timezone.utc)
    snap = Snapshot(
        payload={"ok": True},
        fetched_at=fetched,
        status="stale",
        age_seconds=12.0,
    )
    html = render.freshness_banner(snap, "/view/status")
    assert "data as of" in html.lower()
    assert "refresh" in html.lower()
    assert 'href="/view/status"' in html
    assert 'data-center-freshness="stale"' in html


def test_freshness_banner_marks_loading_shell():
    snap = Snapshot(payload={"_center_loading": True}, fetched_at=None, status="loading", age_seconds=None)
    html = render.freshness_banner(snap, "/view/agents")
    assert 'data-center-loading="1"' in html
    assert "loading" in html.lower()


def test_head_json_objects_does_not_slurp_whole_file(tmp_path, monkeypatch):
    path = tmp_path / "rollout-huge.jsonl"
    line = json.dumps({"type": "session_meta", "payload": {"cwd": "/tmp/demo-task"}}) + "\n"
    path.write_bytes(line.encode("utf-8") + (b"x" * 250_000))
    read_sizes: list[int] = []
    real_open = Path.open

    def tracking_open(self, mode="r", *args, **kwargs):
        handle = real_open(self, mode, *args, **kwargs)
        if self.resolve() != path.resolve():
            return handle
        orig_read = handle.read

        def tracked_read(size=-1):
            data = orig_read() if size is None or size < 0 else orig_read(size)
            read_sizes.append(len(data) if size is None or size < 0 else size)
            return data

        handle.read = tracked_read  # type: ignore[method-assign]
        return handle

    def boom(self):  # noqa: ARG001
        raise AssertionError("read_bytes slurps the whole file")

    monkeypatch.setattr(Path, "open", tracking_open)
    monkeypatch.setattr(Path, "read_bytes", boom)
    objects = activity_records._head_json_objects(path, max_bytes=4096)
    assert objects
    assert read_sizes
    assert all(size <= 4096 for size in read_sizes)


def test_session_scan_skips_old_files_and_oversized_reads(tmp_path, monkeypatch):
    now = datetime(2026, 8, 13, 15, 0, tzinfo=timezone.utc)
    root = tmp_path / "sessions" / "2026" / "08"
    root.mkdir(parents=True)
    recent = root / "rollout-recent.jsonl"
    old = root / "rollout-old.jsonl"
    huge = root / "rollout-huge.jsonl"
    recent.write_text('{"type":"session_meta","payload":{"cwd":"/tmp/recent-task"}}\n')
    old.write_text('{"type":"session_meta","payload":{"cwd":"/tmp/old-task"}}\n')
    huge.write_bytes(b'{"type":"x"}\n' + (b"z" * (activity_records._SESSION_READ_SIZE_CAP + 50)))
    recent_mtime = now.timestamp()
    old_mtime = (now - timedelta(days=21)).timestamp()
    huge_mtime = now.timestamp()
    import os

    os.utime(recent, (recent_mtime, recent_mtime))
    os.utime(old, (old_mtime, old_mtime))
    os.utime(huge, (huge_mtime, huge_mtime))

    read_paths: list[str] = []
    real_head = activity_records._head_json_objects

    def tracked_head(path, **kwargs):
        read_paths.append(path.name)
        return real_head(path, **kwargs)

    monkeypatch.setattr(activity_records, "_head_json_objects", tracked_head)
    records = activity_records._session_file_records(
        root.parent.parent, "codex", "codex-cli", "Codex session", now, "rocinante"
    )
    labels = {record["task_label"] for record in records}
    assert any("recent" in label.lower() or label != activity_records._UNKNOWN_TASK for label in labels) or records
    assert "rollout-old.jsonl" not in read_paths
    assert "rollout-huge.jsonl" not in read_paths


def test_collect_does_not_live_probe_cloud_providers(tmp_path, monkeypatch):
    called = []

    def fake_observe(target):  # noqa: ARG001
        called.append(1)
        return {}, {"branches": [], "prs": []}, False

    monkeypatch.setattr("brigade.cloud_tracker.observe_providers", fake_observe)
    activity_records.collect(tmp_path)
    assert called == []


def test_settled_page_polls_snapshot_every_15s_without_cache_bust():
    html = render.page("Agent Activity - Brigade Center", "test-nonce", "<nav></nav>", "<p>body</p>", reload_ms=15000)
    assert "15000" in html
    # Reloading lets every snapshot issue a matching nonce for page-specific
    # inline assets. Replacing main.innerHTML leaves those assets inert.
    assert "location.reload()" in html
    assert "fetch(" not in html
    assert "Date.now()" not in html
    assert "_=" not in html.split("<script", 1)[1]


def test_status_view_renders_freshness_stamp(monkeypatch):
    from brigade.center_cmd.dashboard.views import status_grid

    def fake_fetch(target):
        del target
        return {"pending_tasks": []}

    monkeypatch.setattr(status_grid, "fetch", fake_fetch)
    server = _make_server(host="127.0.0.1", port=0, token=None, allowed_hosts=None, target=Path("."))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        _wait_until_ready(server)
        host, port = server.server_address
        response = _raw_request(server, f"{host}:{port}", path="/view/status")
        assert _status_code(response) == "200"
        _, body = _headers_and_body(response)
        assert "data as of" in body.lower()
        assert "refresh" in body.lower()
        assert 'data-center-freshness="' in body
    finally:
        server.shutdown()
        server.server_close()
