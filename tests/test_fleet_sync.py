"""Fleet sync phase 2 tests (issue #1123): hub, client, spool, journal hook."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from brigade import fleet_client, fleet_hub

NODE_A = "11111111-1111-4111-8111-111111111111"
NODE_B = "22222222-2222-4222-8222-222222222222"


def _plant_home_identity(home: Path, node_id: str) -> None:
    """Write the per-user machine identity at ``<home>/.brigade/node.toml``."""
    from brigade import node as node_mod

    identity = node_mod.NodeIdentity(node_id=node_id, hostname="fleet-test", roles=(), platform="test")
    path = node_mod.node_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(node_mod._format_node_toml(identity), encoding="utf-8")


def _event(
    node: str = NODE_A,
    run_id: str = "r1",
    seq: int = 1,
    digest: str | None = None,
    state: str = "run.created",
) -> dict:
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
    # Legacy shared-token mode (pre-#1150): the one token posts as any node.
    server = fleet_hub.make_server("127.0.0.1", 0, db, token, allow_admin_writes=True)
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
        assert payload == {"ok": True, "service": "brigade-fleet-hub"}

    def test_health_server_header_omits_python_banner(self, hub):
        url, _token, _db = hub
        request = urllib.request.Request(url + "/health")
        with urllib.request.urlopen(request, timeout=5) as response:
            server = response.headers.get("Server", "")
            payload = json.loads(response.read())
        assert response.status == 200
        assert payload == {"ok": True, "service": "brigade-fleet-hub"}
        assert "Python" not in server
        assert sys.version.split()[0] not in server
        handler = fleet_hub.make_handler("tok", Path("/nonexistent"))
        assert handler.server_version == "brigade-fleet-hub/1"
        assert handler.sys_version == ""
        banner = f"{handler.server_version} {handler.sys_version}"
        assert "Python" not in banner
        assert sys.version.split()[0] not in banner

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

    def test_terminal_event_keeps_last_known_seat(self, hub):
        url, token, _db = hub
        started = {**_event(run_id="seat-run", seq=1, digest="ds1"), "seat": "cursor_grok"}
        finished = {
            **_event(run_id="seat-run", seq=2, digest="ds2", state="run.completed"),
            "seat": "",
        }
        assert _post(url, token, [started, finished])[1] == {"accepted": 2, "duplicate": 0}
        rows = _get(url, "/status?all=1", token)[1]["runs"]
        match = next(row for row in rows if row["run_id"] == "seat-run")
        assert match["state"] == "run.completed"
        assert match["seat"] == "cursor_grok"

    def test_preference_get_put_and_rejects_secrets(self, hub):
        url, token, _db = hub
        status, payload = _get(url, "/preference", token)
        assert status == 200
        assert payload["preference"] == {"impl": None, "review": None, "chef": None, "notes": None}
        request = urllib.request.Request(
            url + "/preference",
            data=json.dumps({"preference": {"impl": "cursor_grok", "review": "claude_standby"}}).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            method="PUT",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            stored = json.loads(response.read())
        assert stored["preference"]["impl"] == "cursor_grok"
        assert _get(url, "/preference", token)[1]["preference"]["review"] == "claude_standby"
        bad = urllib.request.Request(
            url + "/preference",
            data=json.dumps({"token": "leak"}).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            method="PUT",
        )
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(bad, timeout=5)
        assert excinfo.value.code == 400

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

    def test_verify_and_external_completion_are_terminal_and_drop_secrets(self, hub):
        url, token, _db = hub
        verify = {
            **_event(run_id="vr1", state="verify.completed", digest="dv"),
            "exit_status": 0,
            "capability_fingerprint": "fp-verify",
            "token": "secret-token",
            "headers": {"Authorization": "Bearer leaked"},
            "artifact": {"diff": "NOPE"},
        }
        external = {**_event(run_id="ex1", state="external.completed", digest="de"), "prompt": "do not store"}
        assert _post(url, token, [verify, external])[0] == 200
        assert _get(url, "/status", token)[1]["runs"] == []
        rows = {row["run_id"]: row for row in _get(url, "/status?all=1", token)[1]["runs"]}
        assert rows["vr1"]["exit_status"] == 0
        assert rows["vr1"]["capability_fingerprint"] == "fp-verify"
        assert rows["ex1"]["state"] == "external.completed"
        dumped = json.dumps(list(rows.values()))
        assert "secret-token" not in dumped
        assert "Bearer leaked" not in dumped
        assert "NOPE" not in dumped
        assert "do not store" not in dumped

    def test_status_active_view_excludes_terminal_suffixes_and_expired_heartbeats(self, hub):
        url, token, db = hub
        batch = [
            _event(run_id="suffix-done", state="run.dispatch.completed", digest="done"),
            _event(run_id="abandoned", state="run.started", digest="old"),
            _event(run_id="fresh", state="run.started", digest="fresh"),
        ]
        assert _post(url, token, batch)[0] == 200
        conn = sqlite3.connect(str(db))
        try:
            conn.execute(
                "UPDATE events SET received_at = ? WHERE run_id = ?",
                ((datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(), "abandoned"),
            )
            conn.commit()
        finally:
            conn.close()

        active = {row["run_id"] for row in _get(url, "/status", token)[1]["runs"]}
        history = {row["run_id"] for row in _get(url, "/status?all=1", token)[1]["runs"]}

        assert active == {"fresh"}
        assert history == {"suffix-done", "abandoned", "fresh"}

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
        # This machine's hub-facing identity is the home one (#1161): events
        # are stamped NODE_A and spooled under fleet-spool/NODE_A.jsonl.
        _plant_home_identity(self.home, NODE_A)

    def _config(self, tmp_path, url: str, token: str) -> None:
        token_file = tmp_path / "token.txt"
        token_file.write_text(token + "\n")
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "fleet.toml").write_text(f'[fleet]\nhub_url = "{url}"\ntoken_file = "{token_file.as_posix()}"\n')

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
        monkeypatch.delenv("BRIGADE_FLEET_HUB_URL", raising=False)
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
        monkeypatch.delenv("BRIGADE_FLEET_HUB_URL", raising=False)
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

    def test_empty_env_values_fall_through_to_file(self, hub, monkeypatch, tmp_path):
        url, token, _db = hub
        self._config(tmp_path, url, token)
        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", "")
        monkeypatch.setenv("BRIGADE_FLEET_TOKEN", "  ")
        assert fleet_client.load_fleet_config() == {"hub_url": url, "token": token}

    def test_backslash_token_file_path_is_tolerated(self, hub, tmp_path):
        url, token, _db = hub
        token_file = tmp_path / "token.txt"
        token_file.write_text(token + "\n")
        self.home.mkdir(parents=True, exist_ok=True)
        # A Windows-style token_file path: the parsed TOML value carries
        # single backslashes. Written as a basic string with escaped
        # backslashes so the 3.10 fallback parser and stdlib tomllib agree
        # (the fallback mis-handles TOML literal strings).
        raw = str(token_file).replace("/", "\\")
        escaped = raw.replace("\\", "\\\\")
        (self.home / "fleet.toml").write_text(f'[fleet]\nhub_url = "{url}"\ntoken_file = "{escaped}"\n')
        assert fleet_client._read_fleet_section(self.home / "fleet.toml")["token_file"] == raw
        assert fleet_client.load_fleet_config()["token"] == token

    def test_no_config_is_noop(self):
        assert fleet_client.load_fleet_config() == {"hub_url": "", "token": ""}
        assert fleet_client.report_event(_event()) is False
        assert not (self.home / "fleet-spool").exists()

    def test_hub_traffic_uses_home_machine_identity(self, hub, tmp_path, monkeypatch):
        """Hub-facing identity is the per-user home identity: never the nearest
        workspace ``.brigade/node.toml`` and never an event payload's node_id."""
        from brigade import node as node_mod

        url, token, db = hub
        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", url)
        monkeypatch.setenv("BRIGADE_FLEET_TOKEN", token)
        _plant_home_identity(self.home, NODE_A)
        workspace = tmp_path / "ws"
        (workspace / ".brigade").mkdir(parents=True)
        workspace_identity = node_mod.ensure_identity(workspace)
        assert workspace_identity.node_id not in (NODE_A, NODE_B)
        journal = workspace / ".brigade" / "runs" / "runA" / "events" / "lifecycle.jsonl"
        # Resolution ignores where it is asked: always the home identity.
        assert fleet_client.resolve_node_id(journal) == NODE_A
        # A payload claiming another node is re-stamped with the machine identity.
        assert fleet_client.report_event(_event(NODE_B)) is True
        conn = fleet_hub.init_db(db)
        try:
            rows = [r[0] for r in conn.execute("SELECT node_id FROM events").fetchall()]
        finally:
            conn.close()
        assert rows == [NODE_A]
        assert not (self.home / "fleet-spool").exists()

    def test_report_event_node_id_keyword_is_accepted_but_ignored(self, hub, tmp_path, monkeypatch):
        """Source compatibility (#1161): the legacy ``node_id=`` keyword still
        parses (callers keep working) but never reaches the hub; the delivered
        row is stamped with ``resolve_node_id()`` alone."""
        from brigade import node as node_mod

        url, token, db = hub
        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", url)
        monkeypatch.setenv("BRIGADE_FLEET_TOKEN", token)
        workspace = tmp_path / "ws"
        (workspace / ".brigade").mkdir(parents=True)
        workspace_identity = node_mod.ensure_identity(workspace)
        assert workspace_identity.node_id not in (NODE_A, NODE_B)
        # Keyword AND payload both claim another node; base_path points at a
        # workspace whose own identity is also wrong. All accepted, all ignored.
        assert (
            fleet_client.report_event(
                _event(NODE_B),
                base_path=workspace,
                node_id=workspace_identity.node_id,
            )
            is True
        )
        conn = fleet_hub.init_db(db)
        try:
            rows = [r[0] for r in conn.execute("SELECT node_id FROM events").fetchall()]
        finally:
            conn.close()
        assert rows == [NODE_A]

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

        home = tmp_path / "home"
        monkeypatch.setenv("BRIGADE_HOME", str(home))
        _plant_home_identity(home, NODE_A)
        workspace = tmp_path / "ws"
        (workspace / ".brigade").mkdir(parents=True)
        node_mod.ensure_identity(workspace)
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
        # The real resolver returns the per-machine home identity, not the
        # workspace-local one that also exists here.
        assert fleet_client.resolve_node_id(journal) == NODE_A

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

    def test_journal_event_spools_with_machine_node_id(self, tmp_path, monkeypatch):
        """End to end: journal append -> unreachable hub -> spool keyed by the
        per-machine home identity while ``repo`` stays the journal workspace."""
        from brigade import node as node_mod
        from brigade import run_journal

        home = tmp_path / "home"
        monkeypatch.setenv("BRIGADE_HOME", str(home))
        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", "http://127.0.0.1:9")
        _plant_home_identity(home, NODE_A)
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
        spool = fleet_client.spool_path(NODE_A)
        assert spool.is_file()
        spooled = json.loads(spool.read_text(encoding="utf-8").splitlines()[0])
        assert spooled["node_id"] == NODE_A
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
        assert (
            cli.main(
                [
                    "fleet",
                    "serve",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "4000",
                    "--db",
                    str(db),
                    "--token-file",
                    str(token_file),
                ]
            )
            == 0
        )
        assert captured == {
            "host": "127.0.0.1",
            "port": 4000,
            "db_path": db,
            "token_file": token_file,
            "allow_admin_writes": False,
            "deck_config_path": None,
            "trust_tailscale_identity": False,
        }

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

    def test_status_includes_run_preference(self, hub, monkeypatch, capsys):
        from brigade import cli, fleet_hub

        url, token, db = hub
        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", url)
        monkeypatch.setenv("BRIGADE_FLEET_TOKEN", token)
        conn = sqlite3.connect(str(db))
        try:
            fleet_hub.set_run_preference(
                conn,
                {"impl": "cursor_grok", "review": "claude_standby"},
                updated_by="admin",
            )
        finally:
            conn.close()
        assert cli.main(["fleet", "status", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["preference"]["impl"] == "cursor_grok"
        assert payload["preference"]["review"] == "claude_standby"
        assert cli.main(["fleet", "status"]) == 0
        out = capsys.readouterr().out
        assert out.splitlines()[0].split()[-1] == "age"
        assert "run preference (hub)" in out
        assert "impl: cursor_grok" in out

    def test_preference_client_rejects_cleartext_remote_hub(self, monkeypatch, tmp_path):
        monkeypatch.setenv("BRIGADE_HOME", str(tmp_path / "home"))
        monkeypatch.setenv("BRIGADE_FLEET_TOKEN", "admin-token")
        monkeypatch.setenv("BRIGADE_FLEET_NODE_TOKEN", "node-token")
        opened: list[str] = []

        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return b'{"preference": {}}'

        def fake_open(request, timeout=None):
            opened.append(request.full_url)
            return FakeResponse()

        monkeypatch.setattr(fleet_client, "_hub_open", fake_open)
        with pytest.raises(fleet_client.FleetClientError, match="https"):
            fleet_client.fetch_run_preference(hub_url="http://example.com")
        with pytest.raises(fleet_client.FleetClientError, match="https"):
            fleet_client.put_run_preference({"impl": "cursor_grok"}, hub_url="http://example.com")
        assert opened == []

        assert fleet_client.fetch_run_preference(hub_url="http://127.0.0.1") == {}
        assert fleet_client.fetch_run_preference(hub_url="https://hub.example") == {}
        assert fleet_client.put_run_preference({"impl": "cursor_grok"}, hub_url="http://127.0.0.1") == {}
        assert fleet_client.put_run_preference({"impl": "cursor_grok"}, hub_url="https://hub.example") == {}
        assert opened == [
            "http://127.0.0.1/preference",
            "https://hub.example/preference",
            "http://127.0.0.1/preference",
            "https://hub.example/preference",
        ]

    def test_status_table_shows_verify_exit_without_receipt_body(self, hub, monkeypatch, capsys):
        from brigade import cli

        url, token, _db = hub
        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", url)
        monkeypatch.setenv("BRIGADE_FLEET_TOKEN", token)
        event = {**_event(run_id="vr-status", state="verify.completed"), "exit_status": 3}
        assert _post(url, token, event)[0] == 200
        assert cli.main(["fleet", "status", "--all"]) == 0
        out = capsys.readouterr().out
        header = out.splitlines()[0]
        assert "exit" in header
        assert "verify.completed" in out and "3" in out
        assert "commands" not in out and "stdout" not in out

    def test_status_table_renders_legacy_nonprintables_inert(self, hub, monkeypatch, capsys):
        from brigade import cli

        url, token, db = hub
        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", url)
        monkeypatch.setenv("BRIGADE_FLEET_TOKEN", token)
        conn = sqlite3.connect(str(db))
        try:
            conn.execute(
                "INSERT INTO events "
                "(node_id, run_id, sequence, digest, repo, seat, harness, state, ts, received_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    NODE_A,
                    "r-legacy",
                    1,
                    "d1",
                    "repo\x1b[31mred",
                    "seat\x9b",
                    "claude",
                    "run.created",
                    "2026-08-23T12:00:00+00:00",
                    "2026-08-23T12:00:00+00:00",
                ),
            )
            conn.commit()
        finally:
            conn.close()
        assert cli.main(["fleet", "status", "--all", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["runs"][0]["repo"] == "repo\x1b[31mred"
        assert cli.main(["fleet", "status", "--all"]) == 0
        out = capsys.readouterr().out
        assert "\x1b" not in out
        assert "\x9b" not in out
        assert "\\x1b[31mred" in out
        assert "\\x9b" in out

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
        _plant_home_identity(home, NODE_A)
        monkeypatch.setenv("BRIGADE_FLEET_TOKEN", token)
        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", "http://127.0.0.1:9")
        workspace = tmp_path / "ws"
        (workspace / ".brigade").mkdir(parents=True)
        node_mod.ensure_identity(workspace)
        monkeypatch.chdir(workspace)
        fleet_client.report_event(_event())
        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", url)
        assert cli.main(["fleet", "flush"]) == 0
        assert f"flushed 1 spooled event(s) for node {NODE_A}" in capsys.readouterr().out
        conn = fleet_hub.init_db(db)
        try:
            assert conn.execute("SELECT node_id FROM events").fetchone()[0] == NODE_A
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


class TestSpoolHardening:
    """Review HOLD items on d2342dcc: atomic rewrite, lock, cap, poison, deadline, interrupts."""

    @pytest.fixture(autouse=True)
    def _home(self, tmp_path, monkeypatch):
        self.home = tmp_path / "brigade-home"
        monkeypatch.setenv("BRIGADE_HOME", str(self.home))
        # Spooling is keyed by the home machine identity (#1161): report_event
        # appends to fleet-spool/NODE_A.jsonl, matching the _spooled() helper.
        _plant_home_identity(self.home, NODE_A)

    def _spooled(self, node: str = "11111111-1111-4111-8111-111111111111", n: int = 3) -> Path:
        spool = fleet_client.spool_path(node)
        spool.parent.mkdir(parents=True, exist_ok=True)
        spool.write_text("".join(json.dumps(_event(node=node, seq=i), sort_keys=True) + "\n" for i in range(1, n + 1)))
        return spool

    def test_failed_flush_does_not_rewrite_spool(self, monkeypatch):
        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", "http://127.0.0.1:9")
        spool = self._spooled()
        before = spool.stat()
        writes: list[Path] = []
        real = fleet_client._write_spool_atomic
        monkeypatch.setattr(fleet_client, "_write_spool_atomic", lambda p, e: writes.append(p) or real(p, e))
        assert fleet_client.flush_spool(_event()["node_id"]) == 0
        assert writes == []
        assert spool.stat().st_size == before.st_size
        assert spool.stat().st_mtime_ns == before.st_mtime_ns

    def test_failed_report_with_spool_only_appends(self, monkeypatch):
        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", "http://127.0.0.1:9")
        spool = self._spooled()
        writes: list[Path] = []
        real = fleet_client._write_spool_atomic
        monkeypatch.setattr(fleet_client, "_write_spool_atomic", lambda p, e: writes.append(p) or real(p, e))
        assert fleet_client.report_event(_event(seq=4)) is False
        assert writes == []
        assert [json.loads(line)["sequence"] for line in spool.read_text().splitlines()] == [1, 2, 3, 4]

    def test_partial_flush_rewrites_via_tmp_and_replace(self, hub, monkeypatch):
        url, token, _db = hub
        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", url)
        monkeypatch.setenv("BRIGADE_FLEET_TOKEN", token)
        monkeypatch.setattr(fleet_client, "FLUSH_BATCH_SIZE", 1)
        spool = self._spooled()
        replaced: list[tuple[str, str, dict]] = []
        real_replace = fleet_client.os.replace

        def tracking_replace(a, b, **kwargs):
            replaced.append((str(a), str(b), kwargs))
            return real_replace(a, b, **kwargs)

        monkeypatch.setattr(fleet_client.os, "replace", tracking_replace)
        assert fleet_client.flush_spool(_event()["node_id"], max_batches=1) == 1
        # #1154: the rewrite goes through an exclusive, unpredictable temp
        # file in the spool directory — never the predictable ".tmp" path.
        assert len(replaced) == 1
        src_name, dst_name, replace_kwargs = replaced[0]
        if "src_dir_fd" in replace_kwargs:
            # #1157 round 3: descriptor-scoped rewrite — both names resolve
            # inside the validated spool directory itself.
            assert replace_kwargs["src_dir_fd"] == replace_kwargs["dst_dir_fd"]
            assert Path(dst_name).name == spool.name
            assert Path(src_name).parent == Path(".")
            assert src_name != spool.name + ".tmp"
        else:
            src = Path(src_name)
            assert src.parent == spool.parent and src.name != spool.name + ".tmp"
            assert dst_name == str(spool)
            assert not src.exists()
        assert not list(spool.parent.glob("*.tmp"))
        assert [json.loads(line)["sequence"] for line in spool.read_text().splitlines()] == [2, 3]

    def test_concurrent_append_and_flush_lose_nothing(self, hub, monkeypatch):
        """Threads append while another flushes; every event reaches the hub exactly once."""
        url, token, db = hub
        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", url)
        monkeypatch.setenv("BRIGADE_FLEET_TOKEN", token)
        monkeypatch.setattr(fleet_client, "FLUSH_BATCH_SIZE", 5)
        node = _event()["node_id"]
        self._spooled(node, n=20)
        errors: list[BaseException] = []

        def appender(start: int) -> None:
            try:
                for seq in range(start, start + 20):
                    with fleet_client._spool_lock(fleet_client.spool_path(node)):
                        fleet_client._spool_append_locked(fleet_client.spool_path(node), _event(node=node, seq=seq))
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        def flusher() -> None:
            try:
                for _ in range(6):
                    fleet_client.flush_spool(node)
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        threads = [
            threading.Thread(target=appender, args=(100,)),
            threading.Thread(target=appender, args=(200,)),
            threading.Thread(target=flusher),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        assert errors == []
        fleet_client.flush_spool(node)
        conn = fleet_hub.init_db(db)
        try:
            seqs = sorted(r[0] for r in conn.execute("SELECT sequence FROM events").fetchall())
        finally:
            conn.close()
        assert seqs == list(range(1, 21)) + list(range(100, 120)) + list(range(200, 220))
        assert not fleet_client.spool_path(node).exists()

    def test_spool_lock_is_exclusive_across_holders(self, tmp_path):
        import fcntl as _fcntl

        spool = fleet_client.spool_path("lock-node")
        with fleet_client._spool_lock(spool):
            lock_path = spool.with_name(spool.name + ".lock")
            fd = os.open(str(lock_path), os.O_RDWR)
            try:
                with pytest.raises(BlockingIOError):
                    _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            finally:
                os.close(fd)

    def test_spool_cap_drops_oldest_and_logs(self, monkeypatch, caplog):
        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", "http://127.0.0.1:9")
        one = len(json.dumps(_event(seq=1), sort_keys=True)) + 1
        monkeypatch.setattr(fleet_client, "MAX_SPOOL_BYTES", one * 10)
        with caplog.at_level("WARNING", logger="brigade.fleet"):
            for seq in range(1, 13):
                fleet_client.report_event(_event(seq=seq))
        spool = fleet_client.spool_path(_event()["node_id"])
        seqs = [json.loads(line)["sequence"] for line in spool.read_text().splitlines()]
        assert seqs[-1] == 12
        assert len(seqs) <= 10 and seqs == sorted(seqs)
        assert spool.stat().st_size <= one * 10
        assert any("dropped" in r.message for r in caplog.records)

    def test_poison_4xx_batch_is_dropped_not_retried(self, hub, monkeypatch, caplog):
        url, token, db = hub
        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", url)
        monkeypatch.setenv("BRIGADE_FLEET_TOKEN", token)
        monkeypatch.setattr(fleet_client, "FLUSH_BATCH_SIZE", 1)
        node = _event()["node_id"]
        spool = fleet_client.spool_path(node)
        spool.parent.mkdir(parents=True, exist_ok=True)
        spool.write_text(
            json.dumps({"node_id": node, "run_id": ""})
            + "\n"
            + json.dumps(_event(node=node, seq=2), sort_keys=True)
            + "\n"
        )
        with caplog.at_level("WARNING", logger="brigade.fleet"):
            assert fleet_client.flush_spool(node) == 1
        assert not spool.exists()
        assert any("dropping 1 event" in r.message for r in caplog.records)
        conn = fleet_hub.init_db(db)
        try:
            assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
        finally:
            conn.close()

    def test_401_keeps_spool(self, hub, monkeypatch):
        url, _token, _db = hub
        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", url)
        monkeypatch.setenv("BRIGADE_FLEET_TOKEN", "rotated-away")
        node = _event()["node_id"]
        self._spooled(node, n=2)
        assert fleet_client.flush_spool(node) == 0
        assert len(fleet_client.spool_path(node).read_text().splitlines()) == 2

    def test_report_auth_refusals_keep_the_bounded_spool(self, tmp_path, monkeypatch):
        """Unlike claims (#1161), events fail soft on HTTP 401/403: every
        refused event lands in the spool keyed by this machine's identity,
        which stays ordered and capped by MAX_SPOOL_BYTES."""
        db = tmp_path / "hub" / "fleet.db"
        server = fleet_hub.make_server("127.0.0.1", 0, db, "strict-admin-token", allow_admin_writes=False)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}"
            monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", url)
            one = len(json.dumps(_event(seq=1), sort_keys=True)) + 1
            monkeypatch.setattr(fleet_client, "MAX_SPOOL_BYTES", one * 4)
            # Admin bearer against a strict hub: every write is a real 403...
            monkeypatch.setenv("BRIGADE_FLEET_TOKEN", "strict-admin-token")
            for seq in range(1, 6):
                assert fleet_client.report_event(_event(seq=seq)) is False
            # ...and an enrolled-nowhere node bearer: a real 401 on top.
            monkeypatch.setenv("BRIGADE_FLEET_NODE_TOKEN", "never-enrolled")
            for seq in range(6, 11):
                assert fleet_client.report_event(_event(seq=seq)) is False
            spool = fleet_client.spool_path(NODE_A)
            assert spool.is_file()
            seqs = [json.loads(line)["sequence"] for line in spool.read_text().splitlines()]
            assert seqs == sorted(seqs) and seqs[-1] == 10  # newest kept, order preserved
            assert spool.stat().st_size <= one * 4  # never unbounded
        finally:
            server.shutdown()
            server.server_close()

    def test_report_deadline_covers_slow_resolution(self, monkeypatch):
        """A hung getaddrinfo (dead resolver) cannot hold the journal writer past the deadline."""
        import socket
        import time

        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", "http://hub.invalid.tailnet:3774")
        monkeypatch.setattr(fleet_client, "REPORT_TIMEOUT_SECONDS", 0.5)
        release = threading.Event()

        def hung_getaddrinfo(*args, **kwargs):
            release.wait(10)
            raise socket.gaierror("resolver dead")

        monkeypatch.setattr(socket, "getaddrinfo", hung_getaddrinfo)
        started = time.monotonic()
        try:
            assert fleet_client.report_event(_event()) is False
        finally:
            release.set()
        assert time.monotonic() - started < 1.5
        spool = fleet_client.spool_path(_event()["node_id"])
        assert spool.is_file()

    def test_report_deadline_covers_slow_drip_response(self, monkeypatch):
        import time
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Drip(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                # Drip the request's *response* slowly: the status line only
                # arrives after the client's deadline has passed.
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                time.sleep(3)
                try:
                    self.send_response(200)
                    self.send_header("Content-Length", "2")
                    self.end_headers()
                    self.wfile.write(b"{}")
                except OSError:
                    pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Drip)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", f"http://127.0.0.1:{server.server_address[1]}")
            monkeypatch.setattr(fleet_client, "REPORT_TIMEOUT_SECONDS", 0.5)
            started = time.monotonic()
            assert fleet_client.report_event(_event()) is False
            assert time.monotonic() - started < 1.5
        finally:
            server.shutdown()
            server.server_close()

    def test_keyboard_interrupt_during_post_spools_then_reraises(self, monkeypatch):
        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", "http://127.0.0.1:9")

        def interrupt(*a, **kw):
            raise KeyboardInterrupt

        monkeypatch.setattr(fleet_client, "_post_events", interrupt)
        with pytest.raises(KeyboardInterrupt):
            fleet_client.report_event(_event())
        spool = fleet_client.spool_path(_event()["node_id"])
        assert [json.loads(line)["sequence"] for line in spool.read_text().splitlines()] == [1]
        # With a spool present the event is appended before the flush runs.
        with pytest.raises(KeyboardInterrupt):
            fleet_client.report_event(_event(seq=2))
        assert [json.loads(line)["sequence"] for line in spool.read_text().splitlines()] == [1, 2]

    def test_journal_hook_keeps_event_durable_on_interrupt(self, tmp_path, monkeypatch):
        from brigade import node as node_mod
        from brigade import run_journal

        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", "http://127.0.0.1:9")
        workspace = tmp_path / "ws"
        (workspace / ".brigade").mkdir(parents=True)
        node_mod.ensure_identity(workspace)
        journal = workspace / ".brigade" / "runs" / "runA" / "events" / "lifecycle.jsonl"

        def interrupt(*a, **kw):
            raise KeyboardInterrupt

        monkeypatch.setattr(fleet_client, "_post_events", interrupt)
        with pytest.raises(KeyboardInterrupt):
            run_journal.append_event(
                journal,
                run_id="runA",
                event_type="run.created",
                payload={"status": "created"},
                idempotency_key="k1",
                expected_previous_sequence=0,
            )
        # Journal line is durable and the event is in the spool, not lost.
        assert len(run_journal.read_journal(journal).events) == 1
        spool_files = list((self.home / "fleet-spool").glob("*.jsonl"))
        assert len(spool_files) == 1
        assert json.loads(spool_files[0].read_text().splitlines()[0])["sequence"] == 1

    def test_non_interrupt_base_exception_in_hook_is_swallowed(self, tmp_path, monkeypatch):
        from brigade import node as node_mod
        from brigade import run_journal

        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", "http://127.0.0.1:9")
        workspace = tmp_path / "ws"
        (workspace / ".brigade").mkdir(parents=True)
        node_mod.ensure_identity(workspace)
        journal = workspace / ".brigade" / "runs" / "runA" / "events" / "lifecycle.jsonl"

        class Odd(BaseException):
            pass

        def boom(*a, **kw):
            raise Odd

        monkeypatch.setattr(fleet_client, "report_journal_event", boom)
        event = run_journal.append_event(
            journal,
            run_id="runA",
            event_type="run.created",
            payload={"status": "created"},
            idempotency_key="k1",
            expected_previous_sequence=0,
        )
        assert event.sequence == 1

    def test_home_identity_fallback_path(self, tmp_path, monkeypatch):
        from brigade import node as node_mod

        home = tmp_path / "operator" / ".brigade"
        monkeypatch.setenv("BRIGADE_HOME", str(home))
        identity = node_mod.ensure_identity(tmp_path / "operator")  # writes <operator>/.brigade/node.toml
        assert node_mod.node_path(tmp_path / "operator") == home / "node.toml"
        assert fleet_client.resolve_node_id(tmp_path / "elsewhere") == identity.node_id


class TestHubHardening:
    def test_empty_strings_and_negative_sequence_rejected(self, hub):
        url, token, _db = hub
        for bad in (
            {**_event(), "node_id": ""},
            {**_event(), "run_id": "  "},
            {**_event(), "digest": ""},
            {**_event(), "sequence": -1},
            {**_event(), "sequence": True},
        ):
            status, payload = _post(url, token, bad)
            assert status == 400, bad
        assert _post(url, token, {**_event(), "sequence": 0})[0] == 200

    def test_status_one_row_per_run_when_sequence_has_two_digests(self, hub):
        url, token, _db = hub
        _post(url, token, [_event(seq=1, digest="a"), _event(seq=1, digest="b")])
        runs = _get(url, "/status", token)[1]["runs"]
        assert len(runs) == 1
        assert runs[0]["digest"] == "b"

    def test_handler_has_idle_timeout_and_constant_time_auth(self, hub):
        handler = fleet_hub.make_handler("tok", Path("/nonexistent"))
        assert handler.timeout == 30
        import inspect

        assert "compare_digest" in inspect.getsource(handler._authorized)

    def test_sqlite_operational_error_is_500(self, hub, monkeypatch):
        import sqlite3

        url, token, _db = hub

        def broken(conn, raw, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(fleet_hub, "store_events", broken)
        status, payload = _post(url, token, _event())
        assert status == 500
        assert "database" in payload["error"]
        status, payload = _get(url, "/status", token)
        assert status == 200

    def test_control_characters_rejected_at_event_ingestion(self, hub, tmp_path):
        url, token, db = hub
        crafted = (
            {**_event(), "node_id": "node\x1b[31m"},
            {**_event(), "run_id": "run\x07"},
            {**_event(), "state": "run.\x9bstarted"},
            {**_event(), "ts": "2026-08-23T12:00:00+00:00\x00"},
            {**_event(), "digest": "d1\x1f"},
            {**_event(), "repo": "repo\x1b[31mred"},
            {**_event(), "seat": "worker\x80"},
            {**_event(), "harness": "claude\x7f"},
        )
        for bad in crafted:
            status, payload = _post(url, token, bad)
            assert status == 422, bad
            assert "control" in payload["error"]
        conn = sqlite3.connect(str(db))
        try:
            assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        finally:
            conn.close()
        # Clean values still ingest; length-style emptiness stays 400.
        assert _post(url, token, _event())[0] == 200
        status, payload = _post(url, token, {**_event(seq=2), "repo": ""})
        assert status == 200
        status, payload = _post(url, token, {**_event(), "node_id": ""})
        assert status == 400
        conn = fleet_hub.init_db(tmp_path / "direct.db")
        try:
            with pytest.raises(fleet_hub.FleetHubUnprocessable):
                fleet_hub.store_events(conn, {**_event(), "repo": "r\x1b"})
            assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        finally:
            conn.close()


def test_conftest_clears_fleet_env():
    assert "BRIGADE_FLEET_HUB_URL" not in os.environ
    assert "BRIGADE_FLEET_TOKEN" not in os.environ
