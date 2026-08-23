"""Fleet sync phase 2 tests (issue #1123): hub, client, spool, journal hook."""

from __future__ import annotations

import json
import threading
import urllib.request
from pathlib import Path

import pytest

from brigade import fleet_client, fleet_hub


def _event(node: str = "11111111-1111-4111-8111-111111111111", run_id: str = "r1", seq: int = 1, digest: str | None = None, state: str = "run.started") -> dict:
    return {
        "node_id": node,
        "run_id": run_id,
        "repo": "repo-a",
        "seat": "worker",
        "harness": "claude",
        "state": state,
        "ts": "2026-08-23T12:00:00+00:00",
        "sequence": seq,
        "digest": digest or f"d{seq}",
    }


@pytest.fixture()
def hub(tmp_path):
    db = tmp_path / "hub" / "fleet.db"
    token = "test-token-12345"
    server = fleet_hub.make_server("127.0.0.1", 0, db, token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", token, db
    server.shutdown()
    server.server_close()


def _post(url: str, token: str, body) -> tuple[int, dict]:
    request = urllib.request.Request(
        url + "/events",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:  # type: ignore[attr-defined]
        return exc.code, json.loads(exc.read())


def _get(url: str, path: str, token: str | None) -> tuple[int, dict]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    request = urllib.request.Request(url + path, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:  # type: ignore[attr-defined]
        return exc.code, json.loads(exc.read())


class TestHub:
    def test_health_no_auth(self, hub):
        url, _token, _db = hub
        status, payload = _get(url, "/health", None)
        assert status == 200
        assert payload["ok"] is True

    def test_events_rejected_without_token(self, hub):
        url, _token, _db = hub
        status, payload = _get(url, "/events", None)
        assert status == 404  # GET not routed; POST check below
        status, _payload = _post(url, "", _event())
        assert status == 401
        status, _payload = _post(url, "wrong-token", _event())
        assert status == 401

    def test_post_then_status_shows_run(self, hub):
        url, token, _db = hub
        status, counts = _post(url, token, _event())
        assert (status, counts) == (200, {"accepted": 1, "duplicate": 0})
        status, payload = _get(url, "/status", token)
        assert status == 200
        assert [r["run_id"] for r in payload["runs"]] == ["r1"]
        assert payload["runs"][0]["state"] == "run.started"

    def test_duplicate_post_counts_not_double_rows(self, hub):
        url, token, db = hub
        assert _post(url, token, _event())[1]["accepted"] == 1
        status, counts = _post(url, token, _event())
        assert (status, counts["duplicate"], counts["accepted"]) == (200, 1, 0)
        conn = fleet_hub.init_db(db)
        try:
            rows = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        finally:
            conn.close()
        assert rows == 1

    def test_batch_and_terminal_filter(self, hub):
        url, token, _db = hub
        batch = [
            _event(run_id="a", seq=1),
            _event(run_id="a", seq=2, digest="d2", state="run.completed"),
            _event(node="22222222-2222-4222-8222-222222222222", run_id="b"),
        ]
        counts = _post(url, token, batch)[1]
        assert counts == {"accepted": 3, "duplicate": 0}
        _status, payload = _get(url, "/status", token)
        assert sorted(r["run_id"] for r in payload["runs"]) == ["b"]
        _status, payload_all = _get(url, "/status?all=1", token)
        assert sorted(r["run_id"] for r in payload_all["runs"]) == ["a", "b"]

    def test_bad_body_rejected(self, hub):
        url, token, _db = hub
        status, payload = _post(url, token, {"node_id": "x"})
        assert status == 400


class TestClient:
    @pytest.fixture(autouse=True)
    def _home(self, tmp_path, monkeypatch):
        self.home = tmp_path / "brigade-home"
        monkeypatch.setenv("BRIGADE_HOME", str(self.home))

    def _config(self, tmp_path, url: str, token: str) -> None:
        token_file = tmp_path / "token.txt"
        token_file.write_text(token + "\n")
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "fleet.toml").write_text(
            f'[fleet]\nhub_url = "{url}"\ntoken_file = "{token_file.as_posix()}"\n'
        )

    def test_unreachable_hub_spools_does_not_raise(self, monkeypatch):
        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", "http://127.0.0.1:9")
        delivered = fleet_client.report_event(_event())
        assert delivered is False
        spool_files = list((self.home / "fleet-spool").glob("*.jsonl"))
        assert len(spool_files) == 1
        assert json.loads(spool_files[0].read_text().splitlines()[0])["run_id"] == "r1"

    def test_flush_after_hub_up_no_duplicates(self, hub, monkeypatch, tmp_path):
        url, token, db = hub
        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", "http://127.0.0.1:9")
        fleet_client.report_event(_event(seq=1))
        fleet_client.report_event(_event(seq=2))
        spool_files = list((self.home / "fleet-spool").glob("*.jsonl"))
        node_file = spool_files[0].name
        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", "")
        self._config(tmp_path, url, token)
        flushed = fleet_client.flush_spool(Path(node_file).stem)
        assert flushed == 2
        assert list((self.home / "fleet-spool").glob("*.jsonl")) == []
        conn = fleet_hub.init_db(db)
        try:
            rows = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        finally:
            conn.close()
        assert rows == 2
        # Flushing again yields no duplicates.
        assert fleet_client.flush_spool(Path(node_file).stem) == 0

    def test_successful_report_flushes_spool_first(self, hub, monkeypatch, tmp_path):
        url, token, db = hub
        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", "http://127.0.0.1:9")
        fleet_client.report_event(_event(seq=1))
        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", "")
        self._config(tmp_path, url, token)
        delivered = fleet_client.report_event(_event(seq=2))
        assert delivered is True
        conn = fleet_hub.init_db(db)
        try:
            rows = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        finally:
            conn.close()
        assert rows == 2
        assert list((self.home / "fleet-spool").glob("*.jsonl")) == []

    def test_no_config_is_noop(self):
        assert fleet_client.load_fleet_config() == {"hub_url": "", "token": ""}
        assert fleet_client.report_event(_event()) is False
        assert not (self.home / "fleet-spool").exists()

    def test_env_overrides_config(self, hub, monkeypatch, tmp_path):
        url, token, _db = hub
        self._config(tmp_path, "http://127.0.0.1:9", "stale")
        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", url)
        assert fleet_client.report_event(_event()) is True


class TestJournalHook:
    def test_append_reports_event_and_survives_failure(self, tmp_path, monkeypatch):
        from brigade import node as node_mod
        from brigade import run_events, run_journal

        workspace = tmp_path / "ws"
        (workspace / ".brigade").mkdir(parents=True)
        identity = node_mod.ensure_identity(workspace)
        journal = workspace / ".brigade" / "runs" / "runA" / "events" / "lifecycle.jsonl"

        seen: list[dict] = []

        def fake_report(event, *, base_path=None, node_id=None):
            seen.append(event)
            return False

        monkeypatch.setattr(fleet_client, "report_event", fake_report)
        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", "http://127.0.0.1:9")

        envelope = run_events.build_event(
            run_id="runA",
            sequence=1,
            event_type="run.started",
            payload={"status": "started"},
            idempotency_key="k1",
        )
        event = run_journal.append_event(
            journal,
            run_id="runA",
            event_type="run.started",
            payload={"status": "started"},
            idempotency_key="k1",
        )
        assert event.sequence == 1
        assert len(seen) == 1
        assert seen[0]["state"] == "run.started"
        assert seen[0]["sequence"] == 1
        assert seen[0]["digest"] == envelope["event_digest"]

        # A raising reporter must never break the journal writer.
        def boom(*a, **kw):
            raise RuntimeError("network down")

        monkeypatch.setattr(fleet_client, "report_event", boom)
        second = run_journal.append_event(
            journal,
            run_id="runA",
            event_type="run.completed",
            payload={"status": "completed"},
            idempotency_key="k2",
        )
        assert second.sequence == 2
        report = run_journal.read_journal(journal)
        assert len(report.events) == 2
