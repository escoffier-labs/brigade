"""Fleet hub per-node credentials (issue #1150): a node token *is* the node's
identity, so one fleet member can no longer post events or claim operations
as another; the shared bearer becomes the admin token."""

from __future__ import annotations

import hashlib
import inspect
import json
import sqlite3
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from brigade import fleet_client, fleet_hub

NODE_A = "11111111-1111-4111-8111-111111111111"
NODE_B = "22222222-2222-4222-8222-222222222222"
ADMIN = "admin-token-12345"


def _serve(tmp_path, *, allow_admin_writes: bool):
    db = tmp_path / "hub" / "fleet.db"
    server = fleet_hub.make_server("127.0.0.1", 0, db, ADMIN, allow_admin_writes=allow_admin_writes)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, db


@pytest.fixture()
def hub(tmp_path):
    """A hub in its default mode: writes need a node token."""
    server, db = _serve(tmp_path, allow_admin_writes=False)
    yield f"http://127.0.0.1:{server.server_address[1]}", db
    server.shutdown()
    server.server_close()


@pytest.fixture()
def legacy_hub(tmp_path):
    """A hub started with --allow-admin-writes (the pre-#1150 shared-token mode)."""
    server, db = _serve(tmp_path, allow_admin_writes=True)
    yield f"http://127.0.0.1:{server.server_address[1]}", db
    server.shutdown()
    server.server_close()


def _request(url: str, path: str, token: str | None, body=None, *, raw_auth: str | None = None) -> tuple[int, dict]:
    headers: dict[str, str] = {}
    if raw_auth is not None:
        headers["Authorization"] = raw_auth
    elif token is not None:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url + path, data=data, headers=headers, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _event(node: str = NODE_A, seq: int = 1, run_id: str = "r1") -> dict:
    return {
        "node_id": node,
        "run_id": run_id,
        "repo": "repo-a",
        "seat": "worker",
        "harness": "claude",
        "state": "run.started",
        "ts": "2026-08-24T12:00:00+00:00",
        "sequence": seq,
        "digest": f"d{seq}",
    }


def _claim(action: str = "acquire", node: str = NODE_A, target: str = "repo-a", holder: str = "h1", **extra) -> dict:
    return {"action": action, "node_id": node, "target": target, "holder": holder, **extra}


def _enroll(url: str, node_id: str, label: str | None = None) -> str:
    body = {"action": "add", "node_id": node_id}
    if label:
        body["label"] = label
    status, payload = _request(url, "/nodes", ADMIN, body)
    assert status == 200 and payload["added"] is True, payload
    return payload["token"]


def _event_count(db, node: str) -> int:
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute("SELECT COUNT(*) FROM events WHERE node_id = ?", (node,)).fetchone()[0]
    finally:
        conn.close()


def _plant_home_identity(home: Path, node_id: str) -> None:
    """Write the per-user machine identity at ``<home>/.brigade/node.toml``."""
    from brigade import node as node_mod

    identity = node_mod.NodeIdentity(node_id=node_id, hostname="fleet-test", roles=(), platform="test")
    path = node_mod.node_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(node_mod._format_node_toml(identity), encoding="utf-8")


class TestHubNodeCredentials:
    def test_admin_writes_off_by_default(self, hub):
        url, db = hub
        status, payload = _request(url, "/events", ADMIN, _event())
        assert status == 403 and "--allow-admin-writes" in payload["error"]
        status, payload = _request(url, "/claims", ADMIN, _claim())
        assert status == 403 and "nodes add" in payload["error"]
        assert _event_count(db, NODE_A) == 0
        # The admin token is still the control plane and may read.
        assert _request(url, "/status", ADMIN)[0] == 200
        assert _request(url, "/claims", ADMIN)[0] == 200
        assert _request(url, "/nodes", ADMIN) == (200, {"nodes": []})

    def test_allow_admin_writes_restores_shared_token_mode(self, legacy_hub):
        url, db = legacy_hub
        status, payload = _request(url, "/events", ADMIN, [_event(NODE_A), _event(NODE_B)])
        assert (status, payload) == (200, {"accepted": 2, "duplicate": 0})
        assert _request(url, "/claims", ADMIN, _claim(node=NODE_B))[1]["granted"] is True

    def test_matching_node_token_accepted(self, hub):
        url, db = hub
        token_a = _enroll(url, NODE_A, "shadowfax")
        status, payload = _request(url, "/events", token_a, _event(NODE_A))
        assert (status, payload) == (200, {"accepted": 1, "duplicate": 0})
        status, payload = _request(url, "/claims", token_a, _claim(node=NODE_A))
        assert status == 200 and payload["granted"] is True
        assert payload["claim"]["owner_node"] == NODE_A
        assert _request(url, "/claims", token_a, _claim("renew", node=NODE_A))[1]["renewed"] is True
        assert _request(url, "/claims", token_a, _claim("release", node=NODE_A))[1]["released"] is True
        # A node token may read too, so `brigade fleet status` works on a node.
        assert _request(url, "/status", token_a)[0] == 200
        assert _request(url, "/claims", token_a)[0] == 200

    def test_impersonation_rejected_with_403(self, hub):
        url, db = hub
        token_a = _enroll(url, NODE_A)
        token_b = _enroll(url, NODE_B)
        status, payload = _request(url, "/events", token_a, _event(NODE_B))
        assert status == 403 and NODE_B in payload["error"] and NODE_A in payload["error"]
        # One foreign event poisons the whole batch: nothing is stored.
        status, _ = _request(url, "/events", token_a, [_event(NODE_A, seq=1), _event(NODE_B, seq=1)])
        assert status == 403
        assert _event_count(db, NODE_A) == 0 and _event_count(db, NODE_B) == 0
        # B holds the target; A cannot acquire, renew, release, supersede, or inspect as B.
        assert _request(url, "/claims", token_b, _claim(node=NODE_B, holder="hb"))[1]["granted"] is True
        for body in (
            _claim("acquire", node=NODE_B, holder="hb"),
            _claim("renew", node=NODE_B, holder="hb"),
            _claim("release", node=NODE_B, holder="hb"),
            _claim("release", node=NODE_B, holder=None, scope="force"),
            _claim("inspect", node=NODE_B, holder=None),
        ):
            status, payload = _request(url, "/claims", token_a, {k: v for k, v in body.items() if v is not None})
            assert status == 403, (body, payload)
            assert "does not match the caller's node token" in payload["error"]
        status, payload = _request(url, "/claims", token_b, _claim("inspect", node=NODE_B, holder="hb"))
        assert status == 200 and payload["claim"]["owner_node"] == NODE_B

    def test_revoked_token_rejected(self, hub):
        url, db = hub
        token_a = _enroll(url, NODE_A)
        status, payload = _request(url, "/nodes", ADMIN, {"action": "revoke", "node_id": NODE_A})
        assert status == 200 and payload["revoked"] is True and payload["node"]["revoked_at"]
        status, payload = _request(url, "/events", token_a, _event(NODE_A))
        assert status == 401 and "revoked" in payload["error"]
        assert _request(url, "/claims", token_a, _claim())[0] == 401
        assert _request(url, "/status", token_a)[0] == 401
        assert _event_count(db, NODE_A) == 0
        # Revoke is idempotent and keeps the first stamp.
        first = _request(url, "/nodes", ADMIN)[1]["nodes"][0]["revoked_at"]
        again = _request(url, "/nodes", ADMIN, {"action": "revoke", "node_id": NODE_A})[1]
        assert again["revoked"] is True and again["node"]["revoked_at"] == first
        # Re-enrolling rotates: a fresh token works, the old one stays dead.
        token_a2 = _enroll(url, NODE_A)
        assert token_a2 != token_a
        assert _request(url, "/events", token_a2, _event(NODE_A))[0] == 200
        assert _request(url, "/events", token_a, _event(NODE_A))[0] == 401
        assert _request(url, "/nodes", ADMIN)[1]["nodes"][0]["revoked_at"] is None

    def test_unknown_or_malformed_bearer_is_401(self, hub):
        url, _db = hub
        assert _request(url, "/events", None, _event())[0] == 401
        assert _request(url, "/events", "not-a-token", _event())[0] == 401
        assert _request(url, "/events", None, _event(), raw_auth=f"Basic {ADMIN}")[0] == 401
        assert _request(url, "/status", None, raw_auth="Bearer")[0] == 401
        assert _request(url, "/nodes", "not-a-token")[0] == 401
        assert _request(url, "/nodes", None, {"action": "add", "node_id": NODE_A})[0] == 401

    def test_nodes_endpoints_need_the_admin_token(self, hub):
        url, _db = hub
        token_a = _enroll(url, NODE_A)
        status, payload = _request(url, "/nodes", token_a)
        assert status == 403 and "admin token" in payload["error"]
        status, payload = _request(url, "/nodes", token_a, {"action": "add", "node_id": NODE_B})
        assert status == 403
        status, payload = _request(url, "/nodes", token_a, {"action": "revoke", "node_id": NODE_A})
        assert status == 403
        assert _request(url, "/nodes", ADMIN)[1]["nodes"][0]["revoked_at"] is None

    def test_add_is_refused_while_enrolled_and_validates_fields(self, hub):
        url, _db = hub
        _enroll(url, NODE_A, "first")
        status, payload = _request(url, "/nodes", ADMIN, {"action": "add", "node_id": NODE_A})
        assert status == 409 and payload["added"] is False and "revoke" in payload["error"]
        assert "token" not in payload
        for bad in ("unknown", "", "bad id!", "-leading", "x" * 129, 7):
            status, payload = _request(url, "/nodes", ADMIN, {"action": "add", "node_id": bad})
            assert status == 400, bad
            assert "token" not in payload
        assert _request(url, "/nodes", ADMIN, {"action": "add", "node_id": NODE_B, "label": "x" * 129})[0] == 400
        assert _request(url, "/nodes", ADMIN, {"action": "rotate", "node_id": NODE_B})[0] == 400
        assert _request(url, "/nodes", ADMIN, ["add"])[0] == 400
        status, payload = _request(url, "/nodes", ADMIN, {"action": "revoke", "node_id": NODE_B})
        assert status == 404 and payload["revoked"] is False
        nodes = _request(url, "/nodes", ADMIN)[1]["nodes"]
        assert [n["node_id"] for n in nodes] == [NODE_A]
        assert nodes[0]["label"] == "first"

    def test_token_is_stored_only_as_sha256(self, hub):
        url, db = hub
        token_a = _enroll(url, NODE_A)
        conn = sqlite3.connect(str(db))
        try:
            columns = [row[1] for row in conn.execute("PRAGMA table_info(nodes)")]
            assert columns == ["node_id", "token_sha256", "label", "created_at", "revoked_at"]
            rows = conn.execute("SELECT * FROM nodes").fetchall()
        finally:
            conn.close()
        assert len(rows) == 1
        assert rows[0][1] == hashlib.sha256(token_a.encode()).hexdigest() == fleet_hub.hash_node_token(token_a)
        assert all(token_a not in str(cell) for cell in rows[0])
        # The listing never carries the digest either.
        assert "token_sha256" not in _request(url, "/nodes", ADMIN)[1]["nodes"][0]

    def test_both_token_kinds_compare_in_constant_time(self):
        handler = fleet_hub.make_handler("tok", Path("/nonexistent"))
        assert "compare_digest" in inspect.getsource(handler._authorized)
        assert "compare_digest" in inspect.getsource(fleet_hub.lookup_node_token)

    def test_v3_database_gains_nodes_table_in_place(self, tmp_path):
        db = tmp_path / "v3.db"
        conn = sqlite3.connect(str(db))
        conn.execute(fleet_hub._SCHEMA)
        conn.execute(fleet_hub._CLAIMS_SCHEMA)
        conn.execute("PRAGMA user_version=3")
        conn.commit()
        conn.close()
        conn = fleet_hub.init_db(db)
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == fleet_hub.SCHEMA_VERSION == 4
            node, token = fleet_hub.add_node(conn, NODE_A, "label")
            assert node["node_id"] == NODE_A and node["revoked_at"] is None and token
            assert fleet_hub.lookup_node_token(conn, token) == (NODE_A, False)
            assert fleet_hub.lookup_node_token(conn, "nope") == (None, False)
            assert fleet_hub.revoke_node(conn, NODE_A)["revoked_at"]
            assert fleet_hub.lookup_node_token(conn, token) == (NODE_A, True)
            assert fleet_hub.revoke_node(conn, NODE_B) is None
        finally:
            conn.close()

    def test_binding_is_enforced_in_the_functions_not_just_the_routes(self, tmp_path):
        conn = fleet_hub.init_db(tmp_path / "f.db")
        try:
            with pytest.raises(fleet_hub.FleetHubForbidden):
                fleet_hub.store_events(conn, [_event(NODE_A), _event(NODE_B)], caller_node=NODE_A)
            assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
            assert fleet_hub.store_events(conn, _event(NODE_A), caller_node=NODE_A) == {"accepted": 1, "duplicate": 0}
            with pytest.raises(fleet_hub.FleetHubForbidden):
                fleet_hub.handle_claim(conn, _claim(node=NODE_B), caller_node=NODE_A)
            assert conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 0
            assert fleet_hub.handle_claim(conn, _claim(node=NODE_A), caller_node=NODE_A)[0] == 200
            # Malformed requests are still 400-class, checked before the binding.
            with pytest.raises(fleet_hub.FleetHubError) as excinfo:
                fleet_hub.handle_claim(conn, {"action": "acquire"}, caller_node=NODE_A)
            assert not isinstance(excinfo.value, fleet_hub.FleetHubForbidden)
        finally:
            conn.close()


class TestClientNodeToken:
    @pytest.fixture(autouse=True)
    def _home(self, tmp_path, monkeypatch):
        self.home = tmp_path / "brigade-home"
        self.home.mkdir(parents=True)
        monkeypatch.setenv("BRIGADE_HOME", str(self.home))
        # This machine is enrolled as NODE_A: its home identity matches its
        # node token, so hub traffic is stamped with NODE_A.
        _plant_home_identity(self.home, NODE_A)
        for name in ("BRIGADE_FLEET_HUB_URL", "BRIGADE_FLEET_TOKEN", "BRIGADE_FLEET_NODE_TOKEN"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setattr(fleet_client, "_SHARED_TOKEN_WARNED", False)

    def _config(self, url: str, *, admin: str | None = ADMIN, node: str | None = None) -> None:
        lines = ["[fleet]", f'hub_url = "{url}"']
        if admin is not None:
            (self.home / "admin.token").write_text(admin + "\n")
            lines.append(f'token_file = "{(self.home / "admin.token").as_posix()}"')
        if node is not None:
            (self.home / "node.token").write_text(node + "\n")
            lines.append(f'node_token_file = "{(self.home / "node.token").as_posix()}"')
        (self.home / "fleet.toml").write_text("\n".join(lines) + "\n")

    def test_node_token_file_is_the_write_credential(self, hub, caplog):
        url, db = hub
        token_a = _enroll(url, NODE_A)
        self._config(url, node=token_a)
        assert fleet_client.load_fleet_settings() == {"hub_url": url, "admin_token": ADMIN, "node_token": token_a}
        assert fleet_client.load_fleet_config() == {"hub_url": url, "token": token_a}
        with caplog.at_level("WARNING", logger="brigade.fleet"):
            assert fleet_client.report_event(_event(NODE_A)) is True
            decision = fleet_client.acquire_claim("repo-a", node_id=NODE_A)
        assert decision.granted is True and decision.claim["owner_node"] == NODE_A
        assert fleet_client.release_claim("repo-a", holder=decision.holder, node_id=NODE_A).granted is True
        assert not any("shared-token" in r.message for r in caplog.records)
        assert not (self.home / "fleet-spool").exists()
        assert _event_count(db, NODE_A) == 1
        assert fleet_client.fetch_status(include_all=True)[0]["node_id"] == NODE_A
        assert fleet_client.fetch_nodes()[0]["node_id"] == NODE_A

    def test_payload_node_id_is_never_the_hub_facing_identity(self, hub, caplog):
        """An event payload claiming another node is re-stamped with this
        machine's identity before it is sent; the delivered row is ours."""
        url, db = hub
        self._config(url, node=_enroll(url, NODE_A))
        with caplog.at_level("WARNING", logger="brigade.fleet"):
            assert fleet_client.report_event(_event(NODE_B)) is True
        assert _event_count(db, NODE_B) == 0
        assert not (self.home / "fleet-spool").exists()
        conn = sqlite3.connect(str(db))
        try:
            rows = [r[0] for r in conn.execute("SELECT node_id FROM events").fetchall()]
        finally:
            conn.close()
        assert rows == [NODE_A]
        assert not any("shared-token" in r.message for r in caplog.records)

    def test_shared_token_fallback_still_works_with_one_warning(self, legacy_hub, caplog):
        url, db = legacy_hub
        self._config(url)  # token_file only: the pre-#1150 layout
        with caplog.at_level("WARNING", logger="brigade.fleet"):
            assert fleet_client.load_fleet_config() == {"hub_url": url, "token": ADMIN}
            assert fleet_client.report_event(_event(NODE_A)) is True
            assert fleet_client.report_event(_event(NODE_A, seq=2)) is True
            assert fleet_client.acquire_claim("repo-a", node_id=NODE_A).granted is True
        warnings = [r for r in caplog.records if "deprecated shared-token mode" in r.message]
        assert len(warnings) == 1
        assert warnings[0].levelname == "WARNING"
        assert "node_token_file" in warnings[0].message and "brigade fleet nodes add" in warnings[0].message
        assert ADMIN not in warnings[0].message
        assert _event_count(db, NODE_A) == 2

    def test_shared_token_against_a_strict_hub_spools_and_warns(self, hub, caplog):
        url, db = hub
        self._config(url)
        with caplog.at_level("WARNING", logger="brigade.fleet"):
            assert fleet_client.report_event(_event(NODE_A)) is False
        assert fleet_client.spool_path(NODE_A).exists()
        assert _event_count(db, NODE_A) == 0
        assert any("deprecated shared-token mode" in r.message for r in caplog.records)
        # Reads still work with the admin token.
        assert fleet_client.fetch_status() == []

    def test_no_warning_without_a_hub_or_with_a_node_token(self, caplog, monkeypatch):
        (self.home / "fleet.toml").write_text('[fleet]\ntoken_file = "nope"\n')
        monkeypatch.setenv("BRIGADE_FLEET_TOKEN", ADMIN)
        with caplog.at_level("WARNING", logger="brigade.fleet"):
            assert fleet_client.load_fleet_config() == {"hub_url": "", "token": ADMIN}
        assert not caplog.records
        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", "http://127.0.0.1:9")
        monkeypatch.setenv("BRIGADE_FLEET_NODE_TOKEN", "node-env-token")
        with caplog.at_level("WARNING", logger="brigade.fleet"):
            assert fleet_client.load_fleet_config() == {"hub_url": "http://127.0.0.1:9", "token": "node-env-token"}
        assert not caplog.records

    def test_env_node_token_overrides_file_and_admin_stays_separate(self, monkeypatch):
        self._config("http://127.0.0.1:9", node="file-node-token")
        monkeypatch.setenv("BRIGADE_FLEET_NODE_TOKEN", "env-node-token")
        settings = fleet_client.load_fleet_settings()
        assert settings["node_token"] == "env-node-token" and settings["admin_token"] == ADMIN
        monkeypatch.setenv("BRIGADE_FLEET_NODE_TOKEN", "   ")
        assert fleet_client.load_fleet_settings()["node_token"] == "file-node-token"
        assert fleet_client.load_fleet_config()["token"] == "file-node-token"

    def test_admin_calls_need_the_admin_token(self, hub):
        url, _db = hub
        self._config(url, admin=None, node=_enroll(url, NODE_A))
        with pytest.raises(fleet_client.FleetClientError, match="admin token"):
            fleet_client.add_node(NODE_B)
        # A node token presented as the admin token is refused by the hub, verbatim.
        self._config(url, admin=_enroll(url, NODE_B))
        with pytest.raises(fleet_client.FleetClientError, match="HTTP 403"):
            fleet_client.fetch_nodes()


class TestNodesCli:
    @pytest.fixture(autouse=True)
    def _home(self, tmp_path, monkeypatch):
        self.home = tmp_path / "brigade-home"
        self.home.mkdir(parents=True)
        monkeypatch.setenv("BRIGADE_HOME", str(self.home))
        for name in ("BRIGADE_FLEET_HUB_URL", "BRIGADE_FLEET_TOKEN", "BRIGADE_FLEET_NODE_TOKEN"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setattr(fleet_client, "_SHARED_TOKEN_WARNED", False)

    def _config(self, url: str, admin: str = ADMIN) -> None:
        (self.home / "admin.token").write_text(admin + "\n")
        (self.home / "fleet.toml").write_text(
            f'[fleet]\nhub_url = "{url}"\ntoken_file = "{(self.home / "admin.token").as_posix()}"\n'
        )

    def test_add_list_revoke_round_trip(self, hub, capsys):
        from brigade import cli

        url, _db = hub
        self._config(url)
        assert cli.main(["fleet", "nodes", "add", NODE_A, "--label", "shadowfax"]) == 0
        out = capsys.readouterr().out
        assert f"enrolled node {NODE_A}" in out and "shown once" in out
        token_a = out.strip().splitlines()[-1]
        assert len(token_a) >= 40
        assert _request(url, "/events", token_a, _event(NODE_A))[0] == 200
        assert _request(url, "/events", token_a, _event(NODE_B))[0] == 403

        assert cli.main(["fleet", "nodes", "list"]) == 0
        table = capsys.readouterr().out
        assert NODE_A in table and "shadowfax" in table and "active" in table
        assert token_a not in table
        assert cli.main(["fleet", "nodes", "list", "--json"]) == 0
        listed = json.loads(capsys.readouterr().out)["nodes"]
        assert listed[0]["node_id"] == NODE_A and listed[0]["revoked_at"] is None and "token" not in listed[0]

        assert cli.main(["fleet", "nodes", "revoke", NODE_A]) == 0
        assert f"revoked node {NODE_A}" in capsys.readouterr().out
        assert _request(url, "/events", token_a, _event(NODE_A))[0] == 401
        assert cli.main(["fleet", "nodes", "list"]) == 0
        assert "revoked" in capsys.readouterr().out

        assert cli.main(["fleet", "nodes", "add", NODE_A, "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["added"] is True and payload["token"] != token_a
        assert _request(url, "/events", payload["token"], _event(NODE_A, seq=2))[0] == 200
        assert cli.main(["fleet", "nodes", "revoke", NODE_A, "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["revoked"] is True

    def test_errors_are_reported_not_raised(self, hub, capsys):
        from brigade import cli

        url, _db = hub
        (self.home / "fleet.toml").write_text(f'[fleet]\nhub_url = "{url}"\n')
        assert cli.main(["fleet", "nodes", "add", NODE_A]) == 1
        assert "no fleet admin token" in capsys.readouterr().err
        self._config(url, admin="wrong-admin")
        assert cli.main(["fleet", "nodes", "list"]) == 1
        assert "HTTP 401" in capsys.readouterr().err
        self._config(url)
        _enroll(url, NODE_A)
        assert cli.main(["fleet", "nodes", "add", NODE_A]) == 1
        err = capsys.readouterr().err
        assert "HTTP 409" in err and "revoke" in err
        assert cli.main(["fleet", "nodes", "revoke", NODE_B]) == 1
        assert "HTTP 404" in capsys.readouterr().err
        assert cli.main(["fleet", "nodes", "add", "unknown"]) == 1
        assert "HTTP 400" in capsys.readouterr().err
        (self.home / "fleet.toml").write_text("[fleet]\n")
        assert cli.main(["fleet", "nodes", "list"]) == 1
        assert "no fleet hub configured" in capsys.readouterr().err

    def test_serve_passes_allow_admin_writes(self, monkeypatch):
        from brigade import cli

        captured: dict = {}
        monkeypatch.setattr(fleet_hub, "run", lambda **kw: captured.update(kw) or 0)
        assert cli.main(["fleet", "serve", "--host", "100.64.0.1"]) == 0
        assert captured["allow_admin_writes"] is False
        assert cli.main(["fleet", "serve", "--host", "100.64.0.1", "--allow-admin-writes"]) == 0
        assert captured["allow_admin_writes"] is True

    def test_serve_banner_names_the_mode(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("BRIGADE_FLEET_TOKEN", ADMIN)

        def _serve_forever(self):
            raise KeyboardInterrupt

        monkeypatch.setattr(fleet_hub.ThreadingHTTPServer, "serve_forever", _serve_forever)
        assert fleet_hub.run(host="127.0.0.1", port=0, db_path=tmp_path / "x.db", token_file=None) == 0
        assert "node tokens required for writes" in capsys.readouterr().out
        assert (
            fleet_hub.run(host="127.0.0.1", port=0, db_path=tmp_path / "x.db", token_file=None, allow_admin_writes=True)
            == 0
        )
        assert "admin writes allowed" in capsys.readouterr().out


class TestServerStartupMigration:
    """#1161: schema creation/migration happens exactly once at server
    startup, before the socket can serve anything; request handlers open
    non-migrating connections (``open_db``) so enrollments and revocations
    commit-visible to one request are visible to the very next one."""

    def _pre_holder_token_db(self, db: Path) -> None:
        """An early-v2 database: a claims table predating holder tokens."""
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db))
        try:
            conn.execute("CREATE TABLE claims (target TEXT PRIMARY KEY, node_id TEXT, expires_at TEXT)")
            conn.execute("PRAGMA user_version=2")
            conn.commit()
        finally:
            conn.close()

    def test_startup_migrates_once_before_the_socket_exists(self, tmp_path, monkeypatch):
        db = tmp_path / "hub" / "fleet.db"
        self._pre_holder_token_db(db)
        init_calls: list[Path] = []
        real_init_db = fleet_hub.init_db

        def counting_init(path):
            init_calls.append(Path(path))
            return real_init_db(path)

        monkeypatch.setattr(fleet_hub, "init_db", counting_init)
        server = fleet_hub.make_server("127.0.0.1", 0, db, ADMIN, allow_admin_writes=False)
        try:
            # make_server returned before any request could possibly arrive.
            assert init_calls == [db]
            conn = sqlite3.connect(str(db))
            try:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(claims)").fetchall()}
                version = conn.execute("PRAGMA user_version").fetchone()[0]
            finally:
                conn.close()
            assert version == fleet_hub.SCHEMA_VERSION
            assert {"holder_token", "lock_token", "lock_acquired_at", "lock_run_dir"} <= columns
        finally:
            server.server_close()

    def test_startup_migration_refuses_to_serve_an_unusable_database(self, tmp_path):
        """Startup migration is the one place the hub touches the schema: if
        it cannot be done, make_server fails before any socket exists."""
        db = tmp_path / "hub" / "fleet.db"
        self._pre_holder_token_db(db)
        conn = sqlite3.connect(str(db))
        try:
            conn.execute(f"PRAGMA user_version={fleet_hub.SCHEMA_VERSION + 1}")
            conn.commit()
        finally:
            conn.close()
        with pytest.raises(fleet_hub.FleetHubError):
            fleet_hub.make_server("127.0.0.1", 0, db, ADMIN, allow_admin_writes=False)

    def test_handlers_open_the_database_without_migrating(self, tmp_path, monkeypatch):
        db = tmp_path / "hub" / "fleet.db"
        init_calls: list[str] = []
        open_calls: list[str] = []
        real_init_db, real_open_db = fleet_hub.init_db, fleet_hub.open_db

        def counting_init(path):
            init_calls.append(str(path))
            return real_init_db(path)

        def counting_open(path):
            open_calls.append(str(path))
            return real_open_db(path)

        monkeypatch.setattr(fleet_hub, "init_db", counting_init)
        monkeypatch.setattr(fleet_hub, "open_db", counting_open)
        server = fleet_hub.make_server("127.0.0.1", 0, db, ADMIN, allow_admin_writes=False)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}"
            opens_after_startup = len(open_calls)  # init_db itself opened once internally
            assert init_calls == [str(db)]
            assert _request(url, "/status", ADMIN)[0] == 200
            assert _request(url, "/claims", ADMIN)[0] == 200
            assert len(open_calls) >= opens_after_startup + 2  # each db-touching request opened fresh
            assert init_calls == [str(db)]  # never again after startup
        finally:
            server.shutdown()
            server.server_close()

    def test_running_server_observes_enroll_and_revoke_immediately(self, tmp_path, monkeypatch):
        db = tmp_path / "hub" / "fleet.db"
        server = fleet_hub.make_server("127.0.0.1", 0, db, ADMIN, allow_admin_writes=False)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}"
            # Enrolled after startup; the next claim request already sees the row.
            token_a = _enroll(url, NODE_A)
            monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", url)
            monkeypatch.setenv("BRIGADE_FLEET_TOKEN", token_a)
            granted = fleet_client.acquire_claim("repo-a", node_id=NODE_A)
            assert granted.granted is True and granted.claim is not None
            assert granted.claim["owner_node"] == NODE_A
            # Revoked mid-flight; the very next request is refused and the
            # client maps it to the stable auth-failed outcome (#1161).
            status, payload = _request(url, "/nodes", ADMIN, {"action": "revoke", "node_id": NODE_A})
            assert status == 200 and payload["revoked"] is True
            refused = fleet_client.acquire_claim("repo-b", node_id=NODE_A)
            assert refused.granted is False and refused.reason == "auth-failed"
        finally:
            server.shutdown()
            server.server_close()


class TestOpenDbSideEffectFree:
    """#1161: ``open_db`` is a plain read/write open of an existing hub
    database: it never creates a file and never rewrites persistence mode;
    schema creation (v4, WAL) stays with ``init_db`` at server startup."""

    def test_missing_database_raises_and_creates_no_file(self, tmp_path):
        db = tmp_path / "hub" / "fleet.db"
        with pytest.raises(sqlite3.OperationalError):
            fleet_hub.open_db(db)
        assert list(tmp_path.iterdir()) == []

    def test_opening_an_existing_database_does_not_change_its_journal_mode(self, tmp_path):
        db = tmp_path / "hub" / "fleet.db"
        db.parent.mkdir(parents=True)
        plain = sqlite3.connect(str(db))
        try:
            plain.execute("CREATE TABLE t (x)")
            plain.commit()
            before = plain.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            plain.close()
        assert before != "wal"
        opened = fleet_hub.open_db(db)
        try:
            after = opened.execute("PRAGMA journal_mode").fetchone()[0]
            version = opened.execute("PRAGMA user_version").fetchone()[0]
        finally:
            opened.close()
        assert after == before
        assert version == 0  # no schema appeared behind the caller's back
        assert sorted(p.name for p in db.parent.iterdir()) == [db.name]

    def test_wal_database_stays_wal_across_open_db(self, tmp_path):
        db = tmp_path / "hub" / "fleet.db"
        first = fleet_hub.init_db(db)
        try:
            assert first.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        finally:
            first.close()
        reopened = fleet_hub.open_db(db)
        try:
            assert reopened.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        finally:
            reopened.close()

    def test_startup_creates_schema_v4_in_wal_mode(self, tmp_path):
        db = tmp_path / "hub" / "fleet.db"
        server = fleet_hub.make_server("127.0.0.1", 0, db, ADMIN, allow_admin_writes=False)
        server.server_close()
        conn = sqlite3.connect(str(db))
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
        assert mode == "wal"
        assert version == fleet_hub.SCHEMA_VERSION == 4
        assert {"events", "claims", "nodes"} <= tables
