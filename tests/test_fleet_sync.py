"""Fleet sync phase 2 tests (issue #1123): hub, client, spool, journal hook."""

from __future__ import annotations

import json
import threading
import urllib.request
from pathlib import Path

import pytest

from brigade import fleet_client, fleet_hub


def _event(node: str = "11111111-1111-4111-8111-111111111111", run_id: str = "r1", seq: int = 1, digest: str | None = None, state: str = "run.created") -> dict:
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
        assert payload["runs"][0]["state"] == "run.created"

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

    def test_interrupted_is_terminal_and_all_query_parses(self, hub):
        url, token, _db = hub
        _post(url, token, [_event(seq=1), _event(seq=2, state="run.interrupted")])
        assert _get(url, "/status", token)[1]["runs"] == []
        assert len(_get(url, "/status?all=true", token)[1]["runs"]) == 1
        assert len(_get(url, "/status?x=1&all=1", token)[1]["runs"]) == 1

    def test_batch_with_one_bad_event_stores_nothing(self, hub):
        url, token, db = hub
        status, _payload = _post(url, token, [_event(seq=1), {"node_id": "x"}])
        assert status == 400
        conn = fleet_hub.init_db(db)
        try:
            assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        finally:
            conn.close()

    def test_db_is_wal_with_schema_version(self, hub):
        _url, _token, db = hub
        conn = fleet_hub.init_db(db)
        try:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert conn.execute("PRAGMA user_version").fetchone()[0] == fleet_hub.SCHEMA_VERSION
        finally:
            conn.close()

    def test_newer_schema_refused(self, tmp_path):
        db = tmp_path / "future.db"
        conn = fleet_hub.init_db(db)
        conn.execute(f"PRAGMA user_version={fleet_hub.SCHEMA_VERSION + 1}")
        conn.commit()
        conn.close()
        with pytest.raises(fleet_hub.FleetHubError):
            fleet_hub.init_db(db)

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
            f'[fleet]\nhub_url = "{url}"\ntoken_file = "{token_file}"\n'
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

    def test_spooled_events_flush_in_order_before_new_event(self, hub, monkeypatch, tmp_path):
        url, token, db = hub
        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", "http://127.0.0.1:9")
        for seq in (1, 2, 3):
            fleet_client.report_event(_event(seq=seq))
        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", url)
        monkeypatch.setenv("BRIGADE_FLEET_TOKEN", token)
        assert fleet_client.report_event(_event(seq=4)) is True
        conn = fleet_hub.init_db(db)
        try:
            seqs = [r[0] for r in conn.execute("SELECT sequence FROM events ORDER BY rowid").fetchall()]
        finally:
            conn.close()
        assert seqs == [1, 2, 3, 4]
        assert list((self.home / "fleet-spool").glob("*.jsonl")) == []

    def test_partial_flush_keeps_undelivered_in_order(self, hub, monkeypatch):
        url, token, _db = hub
        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", "http://127.0.0.1:9")
        monkeypatch.setattr(fleet_client, "FLUSH_BATCH_SIZE", 2)
        for seq in (1, 2, 3):
            fleet_client.report_event(_event(seq=seq))
        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", url)
        monkeypatch.setenv("BRIGADE_FLEET_TOKEN", token)
        node_id = _event()["node_id"]
        assert fleet_client.flush_spool(node_id, max_batches=1) == 2
        spool = fleet_client.spool_path(node_id)
        remaining = [json.loads(line)["sequence"] for line in spool.read_text().splitlines()]
        assert remaining == [3]
        assert fleet_client.flush_spool(node_id) == 1
        assert not spool.exists()

    def test_no_config_is_noop(self):
        assert fleet_client.load_fleet_config() == {"hub_url": "", "token": ""}
        assert fleet_client.report_event(_event()) is False
        assert not (self.home / "fleet-spool").exists()

    def test_env_hub_url_overrides_config_but_token_file_still_read(self, hub, monkeypatch, tmp_path):
        url, token, _db = hub
        # File points at a dead hub but carries the real token; env URL must win
        # while the token_file is still honored.
        self._config(tmp_path, "http://127.0.0.1:9", token)
        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", url)
        config = fleet_client.load_fleet_config()
        assert config == {"hub_url": url, "token": token}
        assert fleet_client.report_event(_event()) is True
        assert not (self.home / "fleet-spool").exists()

    def test_env_token_overrides_token_file(self, hub, monkeypatch, tmp_path):
        url, token, _db = hub
        self._config(tmp_path, url, "stale-token")
        monkeypatch.setenv("BRIGADE_FLEET_TOKEN", token)
        assert fleet_client.load_fleet_config()["token"] == token
        assert fleet_client.report_event(_event()) is True

    def test_wrong_token_spools_without_raising(self, hub, monkeypatch, tmp_path):
        url, _token, _db = hub
        self._config(tmp_path, url, "wrong-token")
        assert fleet_client.report_event(_event()) is False
        assert len(list((self.home / "fleet-spool").glob("*.jsonl"))) == 1


class TestJournalHook:
    def test_append_reports_event_and_survives_failure(self, tmp_path, monkeypatch):
        from brigade import node as node_mod
        from brigade import run_journal

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

        event = run_journal.append_event(
            journal,
            run_id="runA",
            event_type="run.created",
            payload={"status": "created"},
            idempotency_key="k1",
            expected_previous_sequence=0,
        )
        assert event.sequence == 1
        assert len(seen) == 1
        assert seen[0]["state"] == "run.created"
        assert seen[0]["sequence"] == 1
        assert seen[0]["digest"] == event.event_digest
        assert seen[0]["ts"] == event.recorded_at
        assert seen[0]["run_id"] == "runA"
        assert seen[0]["repo"] == "ws"
        # The real resolver finds the workspace identity from the journal path.
        assert fleet_client.resolve_node_id(journal) == identity.node_id

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
            expected_previous_sequence=1,
        )
        assert second.sequence == 2
        report = run_journal.read_journal(journal)
        assert len(report.events) == 2

    def test_journal_event_spools_with_real_node_id(self, tmp_path, monkeypatch):
        """End to end: journal append -> unreachable hub -> spool keyed by node_id."""
        from brigade import node as node_mod
        from brigade import run_journal

        home = tmp_path / "home"
        monkeypatch.setenv("BRIGADE_HOME", str(home))
        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", "http://127.0.0.1:9")
        workspace = tmp_path / "ws"
        (workspace / ".brigade").mkdir(parents=True)
        identity = node_mod.ensure_identity(workspace)
        journal = workspace / ".brigade" / "runs" / "runA" / "events" / "lifecycle.jsonl"
        run_journal.append_event(
            journal,
            run_id="runA",
            event_type="run.created",
            payload={"status": "created"},
            idempotency_key="k1",
            expected_previous_sequence=0,
        )
        spool = fleet_client.spool_path(identity.node_id)
        assert spool.is_file()
        spooled = json.loads(spool.read_text(encoding="utf-8").splitlines()[0])
        assert spooled["node_id"] == identity.node_id
        assert spooled["repo"] == "ws"
        assert spooled["state"] == "run.created"

    def test_no_hub_configured_does_not_spool(self, tmp_path, monkeypatch):
        from brigade import node as node_mod
        from brigade import run_journal

        home = tmp_path / "home"
        monkeypatch.setenv("BRIGADE_HOME", str(home))
        monkeypatch.delenv("BRIGADE_FLEET_HUB_URL", raising=False)
        workspace = tmp_path / "ws"
        (workspace / ".brigade").mkdir(parents=True)
        node_mod.ensure_identity(workspace)
        journal = workspace / ".brigade" / "runs" / "runA" / "events" / "lifecycle.jsonl"
        run_journal.append_event(
            journal,
            run_id="runA",
            event_type="run.created",
            payload={"status": "created"},
            idempotency_key="k1",
            expected_previous_sequence=0,
        )
        assert not (home / "fleet-spool").exists()


class TestCli:
    def test_serve_requires_host(self, capsys):
        from brigade import cli

        with pytest.raises(SystemExit) as exc:
            cli.main(["fleet", "serve"])
        assert exc.value.code == 2
        assert "--host" in capsys.readouterr().err

    def test_serve_defaults_and_missing_token(self, tmp_path, monkeypatch, capsys):
        from brigade import cli
        from brigade.cli import fleet as fleet_cli

        monkeypatch.delenv("BRIGADE_FLEET_TOKEN", raising=False)
        captured: dict = {}

        def fake_run(**kwargs):
            captured.update(kwargs)
            return 0

        monkeypatch.setattr(fleet_hub, "run", fake_run)
        assert cli.main(["fleet", "serve", "--host", "100.64.0.1"]) == 0
        assert captured["host"] == "100.64.0.1"
        assert captured["port"] == fleet_cli.DEFAULT_PORT == 3774
        assert captured["db_path"] == Path.home() / ".brigade" / "fleet-hub.db"
        assert captured["token_file"] is None

    def test_serve_without_token_exits_before_binding(self, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv("BRIGADE_FLEET_TOKEN", raising=False)
        assert fleet_hub.run(host="127.0.0.1", port=0, db_path=tmp_path / "x.db", token_file=None) == 2
        assert "BRIGADE_FLEET_TOKEN" in capsys.readouterr().err
        assert not (tmp_path / "x.db").exists()
        assert fleet_hub.run(host="", port=0, db_path=tmp_path / "x.db", token_file=None) == 2

    def test_serve_token_file_and_db_flags(self, tmp_path, monkeypatch):
        from brigade import cli

        captured: dict = {}
        monkeypatch.setattr(fleet_hub, "run", lambda **kw: captured.update(kw) or 0)
        token_file = tmp_path / "tok"
        db = tmp_path / "hub.db"
        assert cli.main(["fleet", "serve", "--host", "127.0.0.1", "--port", "4000", "--db", str(db), "--token-file", str(token_file)]) == 0
        assert captured == {"host": "127.0.0.1", "port": 4000, "db_path": db, "token_file": token_file}

    def test_status_json_and_table(self, hub, monkeypatch, capsys):
        from brigade import cli

        url, token, _db = hub
        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", url)
        monkeypatch.setenv("BRIGADE_FLEET_TOKEN", token)
        _post(url, token, [_event(run_id="a"), _event(node="22222222-2222-4222-8222-222222222222", run_id="b")])
        assert cli.main(["fleet", "status", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert sorted(r["run_id"] for r in payload["runs"]) == ["a", "b"]
        assert cli.main(["fleet", "status"]) == 0
        out = capsys.readouterr().out
        assert "repo-a" in out and "worker/claude" in out and "run.created" in out
        assert out.splitlines()[0].split()[-1] == "age"

    def test_status_without_hub_is_error(self, tmp_path, monkeypatch, capsys):
        from brigade import cli

        monkeypatch.setenv("BRIGADE_HOME", str(tmp_path / "home"))
        monkeypatch.delenv("BRIGADE_FLEET_HUB_URL", raising=False)
        assert cli.main(["fleet", "status"]) == 1
        assert "no fleet hub configured" in capsys.readouterr().err

    def test_flush_command(self, hub, tmp_path, monkeypatch, capsys):
        from brigade import cli, node as node_mod

        url, token, db = hub
        home = tmp_path / "home"
        monkeypatch.setenv("BRIGADE_HOME", str(home))
        monkeypatch.setenv("BRIGADE_FLEET_TOKEN", token)
        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", "http://127.0.0.1:9")
        workspace = tmp_path / "ws"
        (workspace / ".brigade").mkdir(parents=True)
        identity = node_mod.ensure_identity(workspace)
        monkeypatch.chdir(workspace)
        fleet_client.report_event(_event(node=identity.node_id))
        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", url)
        assert cli.main(["fleet", "flush"]) == 0
        assert f"flushed 1 spooled event(s) for node {identity.node_id}" in capsys.readouterr().out
        conn = fleet_hub.init_db(db)
        try:
            assert conn.execute("SELECT node_id FROM events").fetchone()[0] == identity.node_id
        finally:
            conn.close()

    def test_format_age(self):
        from datetime import datetime, timezone

        from brigade.cli.fleet import _format_age

        now = datetime(2026, 8, 23, 12, 1, 0, tzinfo=timezone.utc)
        assert _format_age("2026-08-23T12:00:30+00:00", now=now) == "30s"
        assert _format_age("2026-08-23T11:00:00Z", now=now) == "1h"
        assert _format_age("not-a-date", now=now) == "-"
        assert _format_age(None, now=now) == "-"
