"""Fleet sync phase 4 tests (issue #1125): hub-arbitrated cross-machine claims."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from brigade import fleet_client, fleet_hub

NODE_A = "11111111-1111-4111-8111-111111111111"
NODE_B = "22222222-2222-4222-8222-222222222222"


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


@pytest.fixture()
def clock(monkeypatch):
    """Controllable hub clock so TTL expiry needs no real sleeping."""
    state = {"now": 1_700_000_000.0}
    monkeypatch.setattr(fleet_hub, "_now_epoch", lambda: state["now"])
    return state


def _post(url: str, token: str, body, path: str = "/claims") -> tuple[int, dict]:
    request = urllib.request.Request(
        url + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _get(url: str, path: str, token: str | None) -> tuple[int, dict]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    request = urllib.request.Request(url + path, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _claim(action: str = "acquire", node: str = NODE_A, target: str = "repo-a", **extra) -> dict:
    return {"action": action, "node_id": node, "target": target, **extra}


class TestHubClaims:
    def test_claims_require_auth(self, hub):
        url, _token, _db = hub
        assert _post(url, "", _claim())[0] == 401
        assert _post(url, "wrong-token", _claim())[0] == 401
        assert _get(url, "/claims", None)[0] == 401

    def test_acquire_grants_then_refuses_other_node(self, hub):
        url, token, _db = hub
        status, payload = _post(url, token, _claim(ttl_seconds=120))
        assert status == 200 and payload["granted"] is True
        claim = payload["claim"]
        assert claim["target"] == "repo-a"
        assert claim["owner_node"] == NODE_A
        assert claim["ttl_seconds"] == 120
        assert claim["expires_at"] > claim["acquired_at"]
        # Machine B collides: one winner, one refusal naming the owner.
        status, payload = _post(url, token, _claim(node=NODE_B))
        assert status == 409 and payload["granted"] is False
        assert NODE_A in payload["error"]
        assert payload["owner"]["owner_node"] == NODE_A
        assert payload["owner"]["expires_at"] == claim["expires_at"]

    def test_acquire_is_idempotent_for_owner_and_preserves_acquired_at(self, hub, clock):
        url, token, _db = hub
        first = _post(url, token, _claim(ttl_seconds=100))[1]["claim"]
        clock["now"] += 50
        again = _post(url, token, _claim(ttl_seconds=100))
        assert again[0] == 200 and again[1]["granted"] is True
        assert again[1]["claim"]["acquired_at"] == first["acquired_at"]
        assert again[1]["claim"]["expires_at"] > first["expires_at"]

    def test_expired_claim_is_reclaimable_unexpired_is_not(self, hub, clock):
        url, token, _db = hub
        assert _post(url, token, _claim(ttl_seconds=100))[1]["granted"] is True
        clock["now"] += 99
        assert _post(url, token, _claim(node=NODE_B))[0] == 409  # not silently stolen
        clock["now"] += 2  # past the TTL: crashed owner auto-reclaims
        status, payload = _post(url, token, _claim(node=NODE_B))
        assert status == 200 and payload["granted"] is True
        assert payload["claim"]["owner_node"] == NODE_B
        # The old owner's renew is refused, not honored.
        status, payload = _post(url, token, _claim("renew"))
        assert status == 409 and payload["renewed"] is False
        assert payload["owner"]["owner_node"] == NODE_B

    def test_renew_keeps_ownership_past_original_ttl(self, hub, clock):
        url, token, _db = hub
        acquired = _post(url, token, _claim(ttl_seconds=100))[1]["claim"]
        clock["now"] += 90
        status, payload = _post(url, token, _claim("renew", ttl_seconds=100))
        assert status == 200 and payload["renewed"] is True
        assert payload["claim"]["acquired_at"] == acquired["acquired_at"]
        assert payload["claim"]["renewed_at"] > acquired["renewed_at"]
        clock["now"] += 90  # original TTL long gone; renewal kept it alive
        assert _post(url, token, _claim(node=NODE_B))[0] == 409

    def test_renew_refused_for_non_owner_and_after_expiry(self, hub, clock):
        url, token, _db = hub
        _post(url, token, _claim(ttl_seconds=100))
        status, payload = _post(url, token, _claim("renew", node=NODE_B))
        assert status == 409 and payload["owner"]["owner_node"] == NODE_A
        clock["now"] += 101
        status, payload = _post(url, token, _claim("renew"))
        assert status == 409 and payload["renewed"] is False and payload["owner"] is None
        assert "expired or missing" in payload["error"]

    def test_release_frees_target_and_is_owner_only(self, hub):
        url, token, _db = hub
        _post(url, token, _claim())
        status, payload = _post(url, token, _claim("release", node=NODE_B))
        assert status == 409 and payload["released"] is False
        assert payload["owner"]["owner_node"] == NODE_A
        status, payload = _post(url, token, _claim("release"))
        assert (status, payload) == (200, {"released": True})
        status, payload = _post(url, token, _claim("release"))
        assert (status, payload) == (200, {"released": False})
        assert _post(url, token, _claim(node=NODE_B))[1]["granted"] is True

    def test_conductor_distinguishes_owners_on_one_node(self, hub):
        url, token, _db = hub
        assert _post(url, token, _claim(conductor="c1"))[1]["granted"] is True
        status, payload = _post(url, token, _claim(conductor="c2"))
        assert status == 409 and payload["owner"]["owner_conductor"] == "c1"
        assert _post(url, token, _claim(conductor="c1"))[1]["granted"] is True

    def test_validation_rejects_malformed_requests(self, hub):
        url, token, _db = hub
        for bad in (
            [],
            {**_claim(), "action": "steal"},
            {**_claim(), "action": None},
            {**_claim(), "target": " "},
            {"action": "acquire", "target": "repo-a"},
            {**_claim(), "ttl_seconds": 0},
            {**_claim(), "ttl_seconds": fleet_hub.CLAIM_TTL_MAX_SECONDS + 1},
            {**_claim(), "ttl_seconds": True},
            {**_claim(), "ttl_seconds": "900"},
        ):
            status, payload = _post(url, token, bad)
            assert status == 400, bad
            assert "error" in payload
        assert _post(url, token, _claim(ttl_seconds=fleet_hub.CLAIM_TTL_MIN_SECONDS))[0] == 200

    def test_claims_listing_filters_expired_unless_all(self, hub, clock):
        url, token, _db = hub
        _post(url, token, _claim(target="repo-a", ttl_seconds=100))
        _post(url, token, _claim(target="repo-b", node=NODE_B, ttl_seconds=1000))
        clock["now"] += 500
        active = _get(url, "/claims", token)[1]["claims"]
        assert [c["target"] for c in active] == ["repo-b"]
        assert active[0]["expired"] is False
        everything = _get(url, "/claims?all=1", token)[1]["claims"]
        assert [(c["target"], c["expired"]) for c in everything] == [("repo-a", True), ("repo-b", False)]

    def test_phase2_database_upgrades_in_place(self, tmp_path):
        db = tmp_path / "phase2.db"
        conn = sqlite3.connect(str(db))
        conn.execute(fleet_hub._SCHEMA)
        conn.execute("PRAGMA user_version=1")
        conn.commit()
        conn.close()
        conn = fleet_hub.init_db(db)
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == fleet_hub.SCHEMA_VERSION == 2
            assert conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 0
        finally:
            conn.close()


class TestClientClaims:
    @pytest.fixture(autouse=True)
    def _home(self, tmp_path, monkeypatch):
        self.home = tmp_path / "brigade-home"
        monkeypatch.setenv("BRIGADE_HOME", str(self.home))

    def _env(self, monkeypatch, url: str, token: str) -> None:
        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", url)
        monkeypatch.setenv("BRIGADE_FLEET_TOKEN", token)

    def test_acquire_across_two_nodes_one_winner(self, hub, monkeypatch):
        url, token, _db = hub
        self._env(monkeypatch, url, token)
        won = fleet_client.acquire_claim("repo-a", node_id=NODE_A)
        assert won.granted is True and won.reason == "ok"
        assert won.claim is not None and won.claim["owner_node"] == NODE_A
        lost = fleet_client.acquire_claim("repo-a", node_id=NODE_B)
        assert lost.granted is False and lost.reason == "held"
        assert lost.owner is not None and lost.owner["owner_node"] == NODE_A

    def test_renew_and_release_roundtrip(self, hub, monkeypatch):
        url, token, _db = hub
        self._env(monkeypatch, url, token)
        fleet_client.acquire_claim("repo-a", node_id=NODE_A)
        assert fleet_client.renew_claim("repo-a", node_id=NODE_A).granted is True
        assert fleet_client.renew_claim("repo-a", node_id=NODE_B).reason == "held"
        assert fleet_client.release_claim("repo-a", node_id=NODE_A).granted is True
        assert fleet_client.renew_claim("repo-a", node_id=NODE_A).reason == "missing"
        assert fleet_client.fetch_claims() == []

    def test_no_hub_and_unreachable_hub_reasons(self, monkeypatch):
        decision = fleet_client.acquire_claim("repo-a", node_id=NODE_A)
        assert (decision.granted, decision.reason) == (False, "no-hub")
        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", "http://127.0.0.1:9")
        decision = fleet_client.acquire_claim("repo-a", node_id=NODE_A)
        assert (decision.granted, decision.reason) == (False, "hub-unavailable")

    def test_repo_claim_holds_then_releases(self, hub, monkeypatch):
        url, token, _db = hub
        self._env(monkeypatch, url, token)
        with fleet_client.repo_claim("repo-a") as decision:
            assert decision.granted is True
            held = [c["target"] for c in fleet_client.fetch_claims()]
            assert held == ["repo-a"]
        assert fleet_client.fetch_claims() == []

    def test_repo_claim_refusal_names_owner_and_expiry(self, hub, monkeypatch):
        url, token, _db = hub
        self._env(monkeypatch, url, token)
        owner = fleet_client.acquire_claim("repo-a", node_id=NODE_B).claim
        assert owner is not None
        with pytest.raises(fleet_client.FleetClaimHeldError) as exc:
            with fleet_client.repo_claim("repo-a"):
                pass  # pragma: no cover - never entered
        message = str(exc.value)
        assert f"claimed by node {NODE_B}" in message
        assert owner["expires_at"] in message
        assert exc.value.owner is not None and exc.value.owner["owner_node"] == NODE_B
        # The winner's claim was not disturbed by the refused attempt.
        assert [c["owner_node"] for c in fleet_client.fetch_claims()] == [NODE_B]

    def test_repo_claim_hub_down_fails_open_with_one_log_line(self, monkeypatch, caplog):
        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", "http://127.0.0.1:9")
        with caplog.at_level("WARNING", logger="brigade.fleet"):
            with fleet_client.repo_claim("repo-a") as decision:
                assert decision.granted is False and decision.reason == "hub-unavailable"
        assert sum("falling back to the local run lock" in r.message for r in caplog.records) == 1

    def test_repo_claim_no_hub_is_noop(self):
        with fleet_client.repo_claim("repo-a") as decision:
            assert decision.reason == "no-hub"

    def test_repo_claim_heartbeat_renews_and_reacquires(self, hub, monkeypatch):
        url, token, db = hub
        self._env(monkeypatch, url, token)
        monkeypatch.setattr(fleet_client, "_claim_renew_interval", lambda ttl: 0.05)
        with fleet_client.repo_claim("repo-a", ttl_seconds=60) as decision:
            assert decision.granted is True
            acquired_at = (decision.claim or {})["acquired_at"]
            deadline = time.monotonic() + 5
            renewed = False
            while time.monotonic() < deadline and not renewed:
                claims = fleet_client.fetch_claims()
                renewed = bool(claims) and claims[0]["renewed_at"] > acquired_at
                time.sleep(0.02)
            assert renewed, "heartbeat never renewed the claim"
            # Simulate the hub losing the row (restart from backup): the
            # heartbeat re-acquires instead of running unprotected.
            conn = fleet_hub.init_db(db)
            try:
                conn.execute("DELETE FROM claims")
                conn.commit()
            finally:
                conn.close()
            deadline = time.monotonic() + 5
            reacquired = False
            while time.monotonic() < deadline and not reacquired:
                reacquired = [c["target"] for c in fleet_client.fetch_claims()] == ["repo-a"]
                time.sleep(0.02)
            assert reacquired, "heartbeat never re-acquired the lost claim"
        assert fleet_client.fetch_claims() == []

    def test_resolve_claim_target_uses_workspace_name(self, tmp_path):
        from brigade import node as node_mod

        workspace = tmp_path / "ws"
        nested = workspace / "src" / "deep"
        nested.mkdir(parents=True)
        (workspace / ".brigade").mkdir()
        node_mod.ensure_identity(workspace)
        assert fleet_client.resolve_claim_target(nested) == "ws"
        bare = tmp_path / "just-a-dir"
        bare.mkdir()
        assert fleet_client.resolve_claim_target(bare) == "just-a-dir"


class TestFleetClaimsCli:
    def test_claims_table_and_json(self, hub, monkeypatch, capsys):
        from brigade import cli

        url, token, _db = hub
        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", url)
        monkeypatch.setenv("BRIGADE_FLEET_TOKEN", token)
        _post(url, token, _claim(target="repo-a", conductor="cond-1"))
        assert cli.main(["fleet", "claims", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert [c["target"] for c in payload["claims"]] == ["repo-a"]
        assert cli.main(["fleet", "claims"]) == 0
        out = capsys.readouterr().out
        assert "repo-a" in out and NODE_A[:12] in out and "cond-1" in out
        assert out.splitlines()[0].split()[0] == "target"

    def test_claims_empty_and_no_hub(self, hub, tmp_path, monkeypatch, capsys):
        from brigade import cli

        url, token, _db = hub
        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", url)
        monkeypatch.setenv("BRIGADE_FLEET_TOKEN", token)
        assert cli.main(["fleet", "claims"]) == 0
        assert "(no active fleet claims)" in capsys.readouterr().out
        monkeypatch.setenv("BRIGADE_HOME", str(tmp_path / "home"))
        monkeypatch.delenv("BRIGADE_FLEET_HUB_URL", raising=False)
        assert cli.main(["fleet", "claims"]) == 1
        assert "no fleet hub configured" in capsys.readouterr().err


class TestDispatchWiring:
    """The run dispatch path asks the hub before taking the local lease."""

    @pytest.fixture()
    def workspace(self, tmp_path, monkeypatch):
        from brigade import node as node_mod

        monkeypatch.setenv("BRIGADE_HOME", str(tmp_path / "home"))
        ws = tmp_path / "ws"
        (ws / ".brigade").mkdir(parents=True)
        node_mod.ensure_identity(ws)
        roster_path = ws / "roster.toml"
        roster_path.write_text('orchestrator = "chef"\n\n[agents.chef]\ncli = "codex"\nrole = "plan"\n')
        return ws, roster_path

    def _run(self, ws: Path, roster_path: Path) -> int:
        from brigade import cli

        return cli.main(["run", "do something", "--roster", str(roster_path), "--cwd", str(ws)])

    def test_refused_when_other_machine_holds_claim(self, hub, workspace, monkeypatch, capsys):
        from brigade import aboyeur

        url, token, _db = hub
        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", url)
        monkeypatch.setenv("BRIGADE_FLEET_TOKEN", token)
        ws, roster_path = workspace
        _post(url, token, _claim(node=NODE_B, target="ws"))
        dispatched = []
        monkeypatch.setattr(aboyeur, "run", lambda *a, **kw: dispatched.append(1) or 0)
        assert self._run(ws, roster_path) == 2
        err = capsys.readouterr().err
        assert f"claimed by node {NODE_B}" in err and "until" in err
        assert dispatched == []
        # The winner's claim survives the refused dispatch.
        assert [c["owner_node"] for c in fleet_client.fetch_claims()] == [NODE_B]

    def test_granted_run_dispatches_and_releases(self, hub, workspace, monkeypatch):
        from brigade import aboyeur, node as node_mod

        url, token, _db = hub
        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", url)
        monkeypatch.setenv("BRIGADE_FLEET_TOKEN", token)
        ws, roster_path = workspace
        identity = node_mod.load_identity(ws)
        assert identity is not None
        held_during_run = []

        def fake_run(*a, **kw):
            held_during_run.extend(fleet_client.fetch_claims())
            return 0

        monkeypatch.setattr(aboyeur, "run", fake_run)
        assert self._run(ws, roster_path) == 0
        assert [(c["target"], c["owner_node"]) for c in held_during_run] == [("ws", identity.node_id)]
        assert fleet_client.fetch_claims() == []

    def test_hub_down_falls_back_to_local_lock(self, workspace, monkeypatch):
        from brigade import aboyeur

        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", "http://127.0.0.1:9")
        monkeypatch.setenv("BRIGADE_FLEET_TOKEN", "irrelevant")
        ws, roster_path = workspace
        dispatched = []
        monkeypatch.setattr(aboyeur, "run", lambda *a, **kw: dispatched.append(1) or 0)
        assert self._run(ws, roster_path) == 0
        assert dispatched == [1]
