"""Fleet client hardening (#1154): proxy-disabled hub transport and private,
no-follow spool files."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import urllib.request
from pathlib import Path

import pytest

from brigade import fleet_client


@pytest.fixture()
def home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("BRIGADE_HOME", str(home))
    monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", "http://127.0.0.1:9")  # nothing listens
    monkeypatch.setenv("BRIGADE_FLEET_TOKEN", "irrelevant")
    return home


def _spool() -> Path:
    return fleet_client.spool_path(fleet_client.resolve_node_id())


class TestProxyDisabledTransport:
    def test_hub_traffic_ignores_proxy_environment(self, monkeypatch):
        """A configured proxy never receives hub traffic or the bearer token."""
        import threading

        from brigade import fleet_hub

        db = Path(tempfile.mkdtemp()) / "fleet.db"
        token = "test-token-12345"
        server = fleet_hub.make_server("127.0.0.1", 0, db, token, allow_admin_writes=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            # A blackholed proxy would break this delivery if it were used.
            monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
            monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
            monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
            url = f"http://127.0.0.1:{server.server_address[1]}"
            fleet_client._post_events_blocking(
                url,
                token,
                [
                    {
                        "node_id": "node-a",
                        "run_id": "run-1",
                        "state": "run.started",
                        "ts": "2026-01-01T00:00:00Z",
                        "sequence": 1,
                        "digest": "d" * 64,
                    }
                ],
                timeout=5,
            )
            request = urllib.request.Request(url + "/status", headers={"Authorization": f"Bearer {token}"})
            with fleet_client._hub_open(request, timeout=5) as response:
                payload = json.loads(response.read())
            assert [r["run_id"] for r in payload["runs"]] == ["run-1"]
        finally:
            server.shutdown()
            server.server_close()

    def test_events_go_through_the_hub_opener(self, monkeypatch):
        opened = []

        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_open(request, timeout=None):
            opened.append((request.full_url, timeout))
            return FakeResponse()

        monkeypatch.setattr(fleet_client, "_HUB_OPENER", type("O", (), {"open": staticmethod(fake_open)})())
        fleet_client._post_events_blocking("http://hub.example:1", "tok", [{"a": 1}], timeout=0.5)
        assert opened == [("http://hub.example:1/events", 0.5)]


class TestSpoolPrivacy:
    @pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
    def test_spool_dir_and_file_are_private(self, home):
        assert fleet_client.report_event({"run_id": "r1", "state": "run.started"}) is False
        spool = _spool()
        assert spool.is_file(), "failed delivery must spool the event"
        assert stat.S_IMODE(os.stat(spool).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(spool.parent).st_mode) == 0o700

    @pytest.mark.skipif(os.name != "posix", reason="symlink semantics under test are POSIX-only")
    def test_symlinked_spool_path_is_refused(self, home, tmp_path):
        victim = tmp_path / "victim.jsonl"
        victim.write_text("do not touch\n", encoding="utf-8")
        spool = _spool()
        spool.parent.mkdir(parents=True)
        spool.symlink_to(victim)
        assert fleet_client.report_event({"run_id": "r1", "state": "run.started"}) is False
        assert victim.read_text(encoding="utf-8") == "do not touch\n"

    @pytest.mark.skipif(os.name != "posix", reason="os.mkfifo is POSIX-only")
    def test_non_regular_spool_file_is_refused_without_blocking(self, home):
        spool = _spool()
        spool.parent.mkdir(parents=True)
        os.mkfifo(spool)
        assert fleet_client.report_event({"run_id": "r1", "state": "run.started"}) is False
        assert stat.S_ISFIFO(os.stat(spool).st_mode)

    def test_atomic_rewrite_uses_exclusive_unpredictable_temp_names(self, home, tmp_path, monkeypatch):
        spool = _spool()
        spool.parent.mkdir(parents=True, exist_ok=True)
        spool.write_text(json.dumps({"run_id": "old"}) + "\n", encoding="utf-8")
        used: list[Path] = []
        real_mkstemp = tempfile.mkstemp

        def tracking_mkstemp(*args, **kwargs):
            fd, name = real_mkstemp(*args, **kwargs)
            used.append(Path(name))
            return fd, name

        monkeypatch.setattr(fleet_client.tempfile, "mkstemp", tracking_mkstemp)
        fleet_client._write_spool_atomic(spool, [{"run_id": "new"}])
        assert used and used[0] != spool.with_name(spool.name + ".tmp")
        assert used[0].parent == spool.parent
        assert not used[0].exists() and not list(spool.parent.glob("*.tmp"))
        assert [e["run_id"] for e in fleet_client._read_spool(spool)] == ["new"]
