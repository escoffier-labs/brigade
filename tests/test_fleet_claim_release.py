"""Issue #1141: crash self-lockout recovery for hub-arbitrated claims.

A SIGKILLed run leaves an unexpired hub claim under its own node with a
holder token nobody has. Three ways out: ``brigade fleet claims --release``
(own node, or ``--force``), a same-node supersede on acquire once the local
lease reconcile has found the previous run dead — matched to the exact
claim row that run took under its ``run.lock`` lease — and ``brigade run
--no-fleet-claim``.
"""

from __future__ import annotations

import argparse
import json
import os
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
DEAD_PID = 99999999
DEAD_LOCK_ACQUIRED_AT = "2026-08-01T00:00:00+00:00"


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


def _get(url: str, path: str, token: str) -> tuple[int, dict]:
    request = urllib.request.Request(url + path, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, json.loads(response.read())


def _claim(action: str = "acquire", node: str = NODE_A, target: str = "repo-a", holder: str = "h1", **extra) -> dict:
    return {"action": action, "node_id": node, "target": target, "holder": holder, **extra}


def _lease(token: str = "dead-owner", acquired_at: str = DEAD_LOCK_ACQUIRED_AT, **extra) -> dict:
    """A hub-facing run.lock lease as the client sends it."""
    return {"token": token, "acquired_at": acquired_at, **extra}


def _owner(token: str = "dead-owner", acquired_at: str = DEAD_LOCK_ACQUIRED_AT, run_dir: str | None = None) -> dict:
    """A runguard ``owner.json`` payload."""
    return {
        "schema": "brigade.run_lock.v1",
        "owner_token": token,
        "pid": DEAD_PID,
        "run_dir": run_dir,
        "acquired_at": acquired_at,
    }


def _env(monkeypatch, url: str, token: str) -> None:
    monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", url)
    monkeypatch.setenv("BRIGADE_FLEET_TOKEN", token)


class TestHubScopes:
    def test_acquire_scope_node_supersedes_own_dead_lease_only(self, hub):
        url, token, _db = hub
        assert _post(url, token, _claim(holder="dead", conductor="chef", lock=_lease("L1")))[1]["granted"] is True
        # Default (holder) scope: the unexpired same-node row still wins.
        status, payload = _post(url, token, _claim(holder="h2"))
        assert status == 409 and payload["granted"] is False
        # Node scope presenting the row's own (now dead) lease takes it over.
        status, payload = _post(
            url, token, _claim(holder="h2", scope="node", conductor="sous", lock=_lease("L2"), supersede=_lease("L1"))
        )
        assert status == 200 and payload["granted"] is True
        assert payload["superseded"]["owner_node"] == NODE_A
        assert payload["superseded"]["owner_conductor"] == "chef"
        assert payload["claim"]["owner_conductor"] == "sous"
        # The dead token is fenced out; the new holder owns renew/release.
        assert _post(url, token, _claim("renew", holder="dead"))[0] == 409
        assert _post(url, token, _claim("renew", holder="h2"))[1]["renewed"] is True
        # A node-scoped acquire from another node is still a plain 409.
        status, payload = _post(url, token, _claim(node=NODE_B, holder="hb", scope="node", supersede=_lease("L2")))
        assert status == 409 and payload["owner"]["owner_node"] == NODE_A
        # Idempotent retry for the new holder reports nothing superseded.
        status, payload = _post(
            url, token, _claim(holder="h2", scope="node", lock=_lease("L2"), supersede=_lease("L1"))
        )
        assert status == 200 and "superseded" not in payload

    def test_acquire_scope_node_matches_the_exact_lease_row(self, hub):
        """A dead lock in another directory (same basename, one node) or on a
        cloned node identity is a different lease: it never replaces a live
        run's claim, and lease columns are never serialized."""
        url, token, _db = hub
        live = _claim(holder="live", conductor="chef", lock=_lease("lease-a", "2026-08-01T00:00:00+00:00"))
        assert _post(url, token, live)[1]["granted"] is True
        status, payload = _post(url, token, _claim(holder="h2", scope="node", supersede=_lease("lease-b")))
        assert status == 409 and payload["granted"] is False
        assert "another lease" in payload["error"] and payload["owner"]["owner_node"] == NODE_A
        assert _post(url, token, _claim("renew", holder="live"))[1]["renewed"] is True
        # Same token, but the row was taken under a newer lease stamp: refused.
        older = _lease("lease-a", "2026-07-31T23:59:59+00:00")
        assert _post(url, token, _claim(holder="h2", scope="node", supersede=older))[0] == 409
        assert _post(url, token, _claim("renew", holder="live"))[1]["renewed"] is True
        # The exact lease supersedes.
        exact = _lease("lease-a", "2026-08-01T00:00:00+00:00")
        status, payload = _post(url, token, _claim(holder="h2", scope="node", supersede=exact))
        assert status == 200 and payload["superseded"]["owner_conductor"] == "chef"
        listed = _get(url, "/claims", token)[1]["claims"]
        assert len(listed) == 1
        assert not {"lock_token", "lock_acquired_at", "lock_run_dir", "holder_token"} & set(listed[0])
        # A row acquired without a lease (a client outside run_lock) can never be superseded.
        assert _post(url, token, _claim("release", holder="h2"))[1]["released"] is True
        assert _post(url, token, _claim(holder="bare"))[1]["granted"] is True
        assert _post(url, token, _claim(holder="h3", scope="node", supersede=_lease("lease-a")))[0] == 409
        assert _post(url, token, _claim("renew", holder="bare"))[1]["renewed"] is True

    def test_release_scope_node_needs_no_token_but_same_owner_and_a_fence(self, hub):
        url, token, _db = hub
        granted = _post(url, token, _claim(holder="dead", conductor="chef"))[1]
        assert granted["granted"] is True
        fence = granted["claim"]["acquired_at"]
        # Unfenced node-scope delete is malformed, not match-anything.
        unfenced = _claim("release", scope="node")
        unfenced.pop("holder")
        status, payload = _post(url, token, unfenced)
        assert status == 400 and "acquired_at" in payload["error"]
        body = _claim("release", node=NODE_B, scope="node", acquired_at=fence)
        body.pop("holder")
        status, payload = _post(url, token, body)
        assert status == 409 and payload["released"] is False
        assert payload["owner"]["owner_node"] == NODE_A and NODE_B in payload["error"]
        body = _claim("release", scope="node", acquired_at=fence)
        body.pop("holder")
        status, payload = _post(url, token, body)
        assert status == 200 and payload["released"] is True
        assert payload["claim"]["owner_node"] == NODE_A and payload["claim"]["owner_conductor"] == "chef"
        assert _post(url, token, body) == (200, {"released": False})
        assert _post(url, token, _claim(node=NODE_B, holder="hb"))[1]["granted"] is True

    def test_release_scope_force_releases_any_owner(self, hub):
        url, token, _db = hub
        assert _post(url, token, _claim(holder="dead"))[1]["granted"] is True
        body = _claim("release", node=NODE_B, scope="force")
        body.pop("holder")
        status, payload = _post(url, token, body)
        assert status == 200 and payload["released"] is True and payload["claim"]["owner_node"] == NODE_A
        assert _post(url, token, _claim(node=NODE_B, holder="hb"))[1]["granted"] is True

    def test_scope_and_lease_validation(self, hub):
        url, token, _db = hub
        assert _post(url, token, _claim(scope="force"))[0] == 400
        assert _post(url, token, _claim("renew", scope="node"))[0] == 400
        assert _post(url, token, _claim("release", scope="bogus"))[0] == 400
        assert _post(url, token, _claim(scope=1))[0] == 400
        # A node-scoped acquire must present the reconciled dead lease.
        assert _post(url, token, _claim(scope="node"))[0] == 400
        assert _post(url, token, _claim(scope="node", supersede="L1"))[0] == 400
        assert _post(url, token, _claim(scope="node", supersede={"acquired_at": "x"}))[0] == 400
        assert _post(url, token, _claim(scope="node", supersede={"token": "L1", "acquired_at": 5}))[0] == 400
        assert _post(url, token, _claim(scope="node", supersede=_lease("x" * 2000)))[0] == 400
        # Leases belong to acquire only, and must be well-formed.
        assert _post(url, token, _claim("renew", supersede=_lease()))[0] == 400
        assert _post(url, token, _claim("release", lock=_lease()))[0] == 400
        assert _post(url, token, _claim(lock={"token": ""}))[0] == 400
        assert _post(url, token, _claim(lock={"token": "L1", "run_dir": 5}))[0] == 400
        # A holder-scoped release still needs its token.
        body = _claim("release")
        body.pop("holder")
        assert _post(url, token, body)[0] == 400
        # A token-less release still needs a real node identity.
        body = _claim("release", node="unknown", scope="force")
        body.pop("holder")
        assert _post(url, token, body)[0] == 400
        assert _get(url, "/claims", token)[1]["claims"] == []

    def test_inspect_reveals_run_dir_to_the_owner_only(self, hub):
        url, token, _db = hub
        body = _claim("inspect")
        body.pop("holder")
        assert _post(url, token, body) == (200, {"inspected": True, "claim": None, "owned": False})
        assert _post(url, token, _claim(lock=_lease(run_dir="/srv/api/.brigade/runs/r1")))[1]["granted"] is True
        status, payload = _post(url, token, body)
        assert status == 200 and payload["owned"] is True and payload["claim"]["owner_node"] == NODE_A
        assert payload["lock_run_dir"] == "/srv/api/.brigade/runs/r1"
        other = _claim("inspect", node=NODE_B)
        other.pop("holder")
        status, payload = _post(url, token, other)
        assert status == 200 and payload["owned"] is False and payload["claim"]["owner_node"] == NODE_A
        assert "lock_run_dir" not in payload
        assert _post(url, token, _claim("inspect", scope="node"))[0] == 400
        # The receipt of a token-less release is the row it deleted.
        release = _claim("release", scope="node", acquired_at=payload["claim"]["acquired_at"])
        release.pop("holder")
        status, payload = _post(url, token, release)
        assert status == 200 and payload["claim"]["owner_node"] == NODE_A
        assert _post(url, token, release) == (200, {"released": False})

    def test_token_less_release_is_fenced_to_the_inspected_row(self, hub):
        url, token, _db = hub
        assert _post(url, token, _claim(holder="dead"))[1]["granted"] is True
        inspect = _claim("inspect")
        inspect.pop("holder")
        inspected = _post(url, token, inspect)[1]["claim"]
        # A new run on the same node re-acquires between inspect and release.
        assert _post(url, token, _claim("release", holder="dead"))[1]["released"] is True
        time.sleep(0.002)
        assert _post(url, token, _claim(holder="fresh", conductor="new-run"))[1]["granted"] is True
        release = _claim("release", scope="node", acquired_at=inspected["acquired_at"])
        release.pop("holder")
        status, payload = _post(url, token, release)
        assert status == 409 and payload["released"] is False
        assert "re-acquired" in payload["error"] and payload["owner"]["owner_conductor"] == "new-run"
        assert _post(url, token, _claim("renew", holder="fresh"))[1]["renewed"] is True
        # The current row's acquired_at releases it; force is fenced too when given.
        current = _post(url, token, inspect)[1]["claim"]["acquired_at"]
        assert current != inspected["acquired_at"]
        release["acquired_at"] = current
        assert _post(url, token, release)[1]["released"] is True
        assert _post(url, token, _claim(holder="again"))[1]["granted"] is True
        forced = _claim("release", node=NODE_B, scope="force", acquired_at=inspected["acquired_at"])
        forced.pop("holder")
        assert _post(url, token, forced)[0] == 409
        assert _post(url, token, _claim("renew", holder="again"))[1]["renewed"] is True
        # acquired_at belongs to release only, and must be a string.
        assert _post(url, token, _claim(acquired_at="x"))[0] == 400
        assert _post(url, token, _claim("release", scope="node", acquired_at=5))[0] == 400
        no_fence = _claim("release", scope="node")
        no_fence.pop("holder")
        assert _post(url, token, no_fence)[0] == 400

    def test_v2_claim_rows_survive_the_lease_column_upgrade(self, tmp_path):
        db = tmp_path / "hub.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE claims (target TEXT NOT NULL PRIMARY KEY, owner_node TEXT NOT NULL, owner_conductor TEXT, "
            "holder_token TEXT NOT NULL, acquired_at TEXT NOT NULL, renewed_at TEXT NOT NULL, "
            "ttl_seconds INTEGER NOT NULL, expires_at REAL NOT NULL)"
        )
        conn.execute(
            "INSERT INTO claims VALUES ('repo-a', ?, 'chef', 'h1', 'then', 'then', 900, ?)", (NODE_A, time.time() + 900)
        )
        conn.execute("PRAGMA user_version=2")
        conn.commit()
        conn.close()
        conn = fleet_hub.init_db(db)
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == fleet_hub.SCHEMA_VERSION == 4
            assert [c["target"] for c in fleet_hub.list_claims(conn)] == ["repo-a"]
            # No lease recorded: never supersedable, only released or expired.
            status, _payload = fleet_hub.handle_claim(conn, _claim(holder="h2", scope="node", supersede=_lease("L1")))
            assert status == 409
            assert fleet_hub.handle_claim(conn, _claim("renew", holder="h1"))[1]["renewed"] is True
            # Re-opening is idempotent.
            assert fleet_hub.init_db(db).execute("PRAGMA user_version").fetchone()[0] == 4
        finally:
            conn.close()

    def test_lease_column_upgrade_is_safe_under_concurrent_init(self, tmp_path):
        """init_db runs per request on a threading server: many first-touch
        connections against a v2 database (one lease column already present,
        as a half-finished upgrade would leave it) must all succeed."""
        db = tmp_path / "hub.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE claims (target TEXT NOT NULL PRIMARY KEY, owner_node TEXT NOT NULL, owner_conductor TEXT, "
            "holder_token TEXT NOT NULL, acquired_at TEXT NOT NULL, renewed_at TEXT NOT NULL, "
            "ttl_seconds INTEGER NOT NULL, expires_at REAL NOT NULL, lock_token TEXT)"
        )
        conn.execute("INSERT INTO claims VALUES ('repo-a', ?, NULL, 'h1', 't', 't', 900, ?, NULL)", (NODE_A, 4e9))
        conn.execute("PRAGMA user_version=2")
        conn.commit()
        conn.close()
        errors: list[BaseException] = []
        barrier = threading.Barrier(8)

        def open_db() -> None:
            try:
                barrier.wait(timeout=5)
                c = fleet_hub.init_db(db)
                c.close()
            except BaseException as exc:  # noqa: BLE001 - collected for the assertion
                errors.append(exc)

        threads = [threading.Thread(target=open_db) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert errors == []
        conn = fleet_hub.init_db(db)
        try:
            columns = [row[1] for row in conn.execute("PRAGMA table_info(claims)").fetchall()]
            assert columns.count("lock_token") == 1 and "lock_acquired_at" in columns and "lock_run_dir" in columns
            assert [c["target"] for c in fleet_hub.list_claims(conn)] == ["repo-a"]
        finally:
            conn.close()


class TestClient:
    @pytest.fixture(autouse=True)
    def _home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BRIGADE_HOME", str(tmp_path / "brigade-home"))
        monkeypatch.setattr(fleet_client, "resolve_node_id", lambda base_path=None: NODE_A)

    def test_lease_from_lock_owner(self):
        assert fleet_client.lease_from_lock_owner(None) is None
        assert fleet_client.lease_from_lock_owner({}) is None
        assert fleet_client.lease_from_lock_owner({"owner_token": " ", "pid": 1}) is None
        assert fleet_client.lease_from_lock_owner({"owner_token": " t ", "pid": 1, "run_dir": None}) == {"token": "t"}
        assert fleet_client.lease_from_lock_owner(_owner("t", "when", "/r")) == {
            "token": "t",
            "acquired_at": "when",
            "run_dir": "/r",
        }

    def test_inspect_claim(self, hub, monkeypatch):
        url, token, _db = hub
        _env(monkeypatch, url, token)
        free = fleet_client.inspect_claim("repo-a", node_id=NODE_A)
        assert free.granted is True and free.claim is None and free.lock_run_dir is None
        assert fleet_client.acquire_claim("repo-a", node_id=NODE_B, lock_owner=_owner("t", run_dir="/x")).granted
        theirs = fleet_client.inspect_claim("repo-a", node_id=NODE_A)
        assert theirs.claim is not None and theirs.claim["owner_node"] == NODE_B and theirs.lock_run_dir is None
        mine = fleet_client.inspect_claim("repo-a", node_id=NODE_B)
        assert mine.lock_run_dir == "/x"
        assert fleet_client.inspect_claim("repo-a", node_id=NODE_A, hub_url="http://127.0.0.1:9").reason == (
            "hub-unavailable"
        )

    def test_release_claim_without_token_by_node_and_force(self, hub, monkeypatch):
        url, token, _db = hub
        _env(monkeypatch, url, token)
        won = fleet_client.acquire_claim("repo-a", node_id=NODE_A)
        assert won.granted is True and won.claim is not None
        fence = won.claim["acquired_at"]
        # An unfenced non-force operator release is refused by the hub.
        unfenced = fleet_client.release_claim("repo-a", node_id=NODE_A)
        assert unfenced.granted is False and unfenced.reason == "hub-unavailable"
        assert unfenced.detail is not None and "acquired_at" in unfenced.detail
        refused = fleet_client.release_claim("repo-a", node_id=NODE_B, acquired_at=fence)
        assert (refused.granted, refused.reason) == (False, "held")
        assert refused.owner is not None and refused.owner["owner_node"] == NODE_A
        released = fleet_client.release_claim("repo-a", node_id=NODE_A, acquired_at=fence)
        assert released.granted is True and released.claim is not None
        assert released.claim["owner_node"] == NODE_A
        assert fleet_client.release_claim("repo-a", node_id=NODE_A, acquired_at=fence).reason == "missing"
        assert fleet_client.acquire_claim("repo-a", node_id=NODE_A).granted is True
        assert fleet_client.release_claim("repo-a", node_id=NODE_B, force=True).granted is True
        assert fleet_client.fetch_claims() == []
        # Fenced to the inspected row.
        assert fleet_client.acquire_claim("repo-a", node_id=NODE_A).granted is True
        stale = fleet_client.release_claim("repo-a", node_id=NODE_A, acquired_at="1999-01-01T00:00:00+00:00")
        assert (stale.granted, stale.reason) == (False, "held") and stale.owner is not None
        current = fleet_client.inspect_claim("repo-a", node_id=NODE_A).claim
        assert current is not None
        assert fleet_client.release_claim("repo-a", node_id=NODE_A, acquired_at=current["acquired_at"]).granted

    def test_repo_claim_supersedes_dead_lease_and_logs_once(self, hub, monkeypatch, caplog):
        url, token, _db = hub
        _env(monkeypatch, url, token)
        dead = fleet_client.acquire_claim("repo-a", node_id=NODE_A, conductor="chef", lock_owner=_owner("dead")).claim
        assert dead is not None
        with caplog.at_level("WARNING", logger="brigade.fleet"):
            with fleet_client.repo_claim(
                "repo-a", conductor="sous", lock_owner=_owner("new"), supersede_dead_owner=_owner("dead")
            ) as decision:
                assert decision.granted is True
                assert decision.superseded is not None and decision.superseded["owner_conductor"] == "chef"
                assert [c["owner_conductor"] for c in fleet_client.fetch_claims()] == ["sous"]
        assert fleet_client.fetch_claims() == []
        assert sum("superseded this node's unexpired claim" in r.message for r in caplog.records) == 1

    def test_repo_claim_supersede_needs_the_exact_dead_lease(self, hub, monkeypatch):
        url, token, _db = hub
        _env(monkeypatch, url, token)
        assert fleet_client.acquire_claim("repo-a", node_id=NODE_A, conductor="chef", lock_owner=_owner("a")).granted
        # A different dead lease on the same node id (another directory, a cloned node).
        with pytest.raises(fleet_client.FleetClaimHeldError, match="another lease"):
            with fleet_client.repo_claim("repo-a", lock_owner=_owner("b"), supersede_dead_owner=_owner("dead-b")):
                pass  # pragma: no cover - never entered
        # An unattributable dead lock (no token) is no evidence at all.
        with pytest.raises(fleet_client.FleetClaimHeldError):
            with fleet_client.repo_claim("repo-a", supersede_dead_owner={"pid": DEAD_PID}):
                pass  # pragma: no cover - never entered
        assert [c["owner_conductor"] for c in fleet_client.fetch_claims()] == ["chef"]

    def test_repo_claim_without_supersede_names_the_way_out(self, hub, monkeypatch):
        url, token, _db = hub
        _env(monkeypatch, url, token)
        assert fleet_client.acquire_claim("repo-a", node_id=NODE_A).granted is True
        with pytest.raises(fleet_client.FleetClaimHeldError) as exc:
            with fleet_client.repo_claim("repo-a"):
                pass  # pragma: no cover - never entered
        message = str(exc.value)
        assert f"claimed by node {NODE_A}" in message
        assert "brigade fleet claims --release repo-a" in message and "--no-fleet-claim" in message
        # Another node's claim keeps the cross-machine wording.
        assert fleet_client.release_claim("repo-a", node_id=NODE_A, force=True).granted is True
        assert fleet_client.acquire_claim("repo-a", node_id=NODE_B).granted is True
        with pytest.raises(fleet_client.FleetClaimHeldError, match="another machine or conductor"):
            with fleet_client.repo_claim("repo-a"):
                pass  # pragma: no cover - never entered

    def test_repo_claim_supersede_never_steals_another_node(self, hub, monkeypatch):
        url, token, _db = hub
        _env(monkeypatch, url, token)
        assert fleet_client.acquire_claim("repo-a", node_id=NODE_B, lock_owner=_owner("dead")).granted is True
        with pytest.raises(fleet_client.FleetClaimHeldError):
            with fleet_client.repo_claim("repo-a", supersede_dead_owner=_owner("dead")):
                pass  # pragma: no cover - never entered
        assert [c["owner_node"] for c in fleet_client.fetch_claims()] == [NODE_B]


class TestReleaseCli:
    @pytest.fixture(autouse=True)
    def _home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BRIGADE_HOME", str(tmp_path / "brigade-home"))
        monkeypatch.setattr(fleet_client, "resolve_node_id", lambda base_path=None: NODE_A)

    def _dead_run(self, tmp_path, name: str = "api") -> tuple[Path, Path]:
        """A workspace and a finished run directory inside it, no run lock."""
        ws = tmp_path / name
        run_dir = ws / ".brigade" / "runs" / "r1"
        run_dir.mkdir(parents=True)
        (run_dir / "run.json").write_text(json.dumps({"status": "completed", "lock_workspace": str(ws)}))
        return ws, run_dir

    def test_release_own_node_claim_text_and_json(self, hub, tmp_path, monkeypatch, capsys):
        from brigade import cli

        url, token, _db = hub
        _env(monkeypatch, url, token)
        _ws, run_dir = self._dead_run(tmp_path)
        _post(url, token, _claim(holder="dead", conductor="chef", lock=_lease(run_dir=str(run_dir))))
        assert cli.main(["fleet", "claims", "--release", "repo-a"]) == 0
        out = capsys.readouterr().out
        assert "released claim on 'repo-a'" in out and NODE_A in out and "chef" in out
        assert fleet_client.fetch_claims() == []
        _post(url, token, _claim(holder="dead2", lock=_lease(run_dir=str(run_dir))))
        assert cli.main(["fleet", "claims", "--release", "repo-a", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["released"] is True and payload["forced"] is False
        assert payload["target"] == "repo-a" and payload["node_id"] == NODE_A
        assert payload["claim"]["owner_node"] == NODE_A
        assert fleet_client.fetch_claims() == []

    def test_release_other_node_refused_without_force(self, hub, monkeypatch, capsys):
        from brigade import cli

        url, token, _db = hub
        _env(monkeypatch, url, token)
        _post(url, token, _claim(node=NODE_B, holder="hb"))
        # The refusal happens at the probe: no release request is ever sent.
        real_release = fleet_client.release_claim
        released_calls: list = []
        monkeypatch.setattr(
            fleet_client, "release_claim", lambda *a, **kw: released_calls.append(1) or real_release(*a, **kw)
        )
        assert cli.main(["fleet", "claims", "--release", "repo-a"]) == 1
        err = capsys.readouterr().err
        assert f"held by node {NODE_B}" in err and "--force" in err
        assert released_calls == []
        assert [c["owner_node"] for c in fleet_client.fetch_claims()] == [NODE_B]
        assert cli.main(["fleet", "claims", "--release", "repo-a", "--json"]) == 1
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert payload["released"] is False and payload["owner"]["owner_node"] == NODE_B
        assert [c["owner_node"] for c in fleet_client.fetch_claims()] == [NODE_B]
        assert cli.main(["fleet", "claims", "--release", "repo-a", "--force", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["released"] is True and payload["forced"] is True
        assert payload["claim"]["owner_node"] == NODE_B
        assert fleet_client.fetch_claims() == []

    def test_release_node_override_needs_force_when_not_this_node(self, hub, monkeypatch, capsys):
        from brigade import cli

        url, token, _db = hub
        _env(monkeypatch, url, token)
        _post(url, token, _claim(node=NODE_B, holder="hb"))
        assert cli.main(["fleet", "claims", "--release", "repo-a", "--node", "unknown"]) == 1
        assert "not a usable fleet node identity" in capsys.readouterr().err
        assert cli.main(["fleet", "claims", "--release", "repo-a", "--node", NODE_B]) == 1
        err = capsys.readouterr().err
        assert "is not this node's identity" in err and "--force" in err
        assert [c["owner_node"] for c in fleet_client.fetch_claims()] == [NODE_B]
        assert cli.main(["fleet", "claims", "--release", "repo-a", "--node", NODE_B, "--force", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["released"] is True and payload["node_id"] == NODE_B and payload["forced"] is True
        assert fleet_client.fetch_claims() == []

    def test_release_bare_key_is_never_retargeted_by_a_same_named_directory(self, hub, tmp_path, monkeypatch, capsys):
        """cwd is workspace 'ws' holding a live claim; 'ws/repo-a' is an
        ordinary subdirectory. `--release repo-a` must release the claim on
        key 'repo-a', never walk up to 'ws'."""
        from brigade import cli, node as node_mod

        url, token, _db = hub
        _env(monkeypatch, url, token)
        ws = tmp_path / "ws"
        (ws / ".brigade").mkdir(parents=True)
        node_mod.ensure_identity(ws)
        (ws / "repo-a").mkdir()
        monkeypatch.chdir(ws)
        _other, run_dir = self._dead_run(tmp_path, "other")
        _post(url, token, _claim(target="ws", holder="live-ws", conductor="chef", lock=_lease("L1")))
        _post(url, token, _claim(target="repo-a", holder="dead", lock=_lease("L2", run_dir=str(run_dir))))
        assert cli.main(["fleet", "claims", "--release", "repo-a"]) == 0
        assert "released claim on 'repo-a'" in capsys.readouterr().out
        assert [c["target"] for c in fleet_client.fetch_claims()] == ["ws"]
        assert _post(url, token, _claim("renew", target="ws", holder="live-ws"))[1]["renewed"] is True
        # Even with --force the key is the key.
        _post(url, token, _claim(target="repo-a", holder="dead2"))
        assert cli.main(["fleet", "claims", "--release", "repo-a", "--force"]) == 0
        assert [c["target"] for c in fleet_client.fetch_claims()] == ["ws"]

    def test_release_bare_key_checks_liveness_through_the_recorded_run_dir(self, hub, tmp_path, monkeypatch, capsys):
        from brigade import cli, runguard

        url, token, _db = hub
        _env(monkeypatch, url, token)
        ws, run_dir = self._dead_run(tmp_path)
        _post(url, token, _claim(target="api", holder="dead", lock=_lease(run_dir=str(run_dir))))
        lock = runguard.lock_path(ws)
        lock.mkdir(parents=True)
        (lock / "pid").write_text(f"{os.getpid()}\n")
        # The run that took the claim is alive on its workspace lock: refused.
        assert cli.main(["fleet", "claims", "--release", "api"]) == 1
        err = capsys.readouterr().err
        assert "run owner is still alive" in err and str(ws) in err and "--force" in err
        assert [c["target"] for c in fleet_client.fetch_claims()] == ["api"]
        # Dead owner: released.
        (lock / "pid").write_text(f"{DEAD_PID}\n")
        assert cli.main(["fleet", "claims", "--release", "api"]) == 0
        assert fleet_client.fetch_claims() == []
        # No recorded run directory: cannot verify, refused without --force.
        _post(url, token, _claim(target="api", holder="bare"))
        assert cli.main(["fleet", "claims", "--release", "api"]) == 1
        err = capsys.readouterr().err
        assert "cannot verify" in err and "records no run directory" in err and "--force" in err
        assert [c["target"] for c in fleet_client.fetch_claims()] == ["api"]
        # A run directory that is not on this machine: same refusal.
        _post(url, token, _claim("release", target="api", holder="bare"))
        _post(url, token, _claim(target="api", holder="elsewhere", lock=_lease(run_dir="/nonexistent/.brigade/runs/x")))
        assert cli.main(["fleet", "claims", "--release", "api"]) == 1
        assert "does not exist on this machine" in capsys.readouterr().err
        assert cli.main(["fleet", "claims", "--release", "api", "--force", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["forced"] is True
        assert fleet_client.fetch_claims() == []

    def test_release_path_proves_the_claim_belongs_to_that_workspace(self, hub, tmp_path, monkeypatch, capsys):
        """A live run in ws-a holds 'api'. From any other directory named
        'api' (even an empty one), `--release <dir> --path` must not delete
        it: the proof runs on the claim's RECORDED run directory."""
        from brigade import cli, runguard

        url, token, _db = hub
        _env(monkeypatch, url, token)
        ws_a, run_dir = self._dead_run(tmp_path / "work")
        lock = runguard.lock_path(ws_a)
        lock.mkdir(parents=True)
        (lock / "pid").write_text(f"{os.getpid()}\n")
        _post(url, token, _claim(target="api", holder="live", conductor="chef", lock=_lease(run_dir=str(run_dir))))
        ws_b = tmp_path / "tmp" / "api"
        ws_b.mkdir(parents=True)
        assert cli.main(["fleet", "claims", "--release", str(ws_b), "--path"]) == 1
        err = capsys.readouterr().err
        assert "was taken by a run in" in err and str(ws_a) in err and "--force" in err
        assert _post(url, token, _claim("renew", target="api", holder="live"))[1]["renewed"] is True
        # Even with ws-a's run dead, another directory is not that workspace.
        (lock / "pid").write_text(f"{DEAD_PID}\n")
        assert cli.main(["fleet", "claims", "--release", str(ws_b), "--path"]) == 1
        assert "was taken by a run in" in capsys.readouterr().err
        assert [c["target"] for c in fleet_client.fetch_claims()] == ["api"]
        # The right workspace: alive → refused, malformed lock → refused, dead → released.
        (lock / "pid").write_text(f"{os.getpid()}\n")
        assert cli.main(["fleet", "claims", "--release", str(ws_a), "--path"]) == 1
        assert "run owner is still alive" in capsys.readouterr().err
        import shutil

        shutil.rmtree(lock)
        lock.write_text("not a lock directory")
        assert cli.main(["fleet", "claims", "--release", str(ws_a), "--path"]) == 1
        assert "malformed" in capsys.readouterr().err
        assert [c["target"] for c in fleet_client.fetch_claims()] == ["api"]
        lock.unlink()
        assert cli.main(["fleet", "claims", "--release", str(ws_a), "--path", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["released"] is True and payload["forced"] is False and payload["target"] == "api"
        assert fleet_client.fetch_claims() == []
        # --force skips the proof (and says so in the receipt).
        _post(url, token, _claim(target="api", holder="live2", lock=_lease(run_dir=str(run_dir))))
        lock.mkdir()
        (lock / "pid").write_text(f"{os.getpid()}\n")
        assert cli.main(["fleet", "claims", "--release", str(ws_b), "--path", "--force", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["forced"] is True
        assert fleet_client.fetch_claims() == []

    def test_release_path_uses_the_workspace_itself(self, hub, tmp_path, monkeypatch, capsys):
        from brigade import cli, node as node_mod

        url, token, _db = hub
        _env(monkeypatch, url, token)
        ws, run_dir = self._dead_run(tmp_path)
        identity = node_mod.ensure_identity(ws)
        # The identity must come from the target workspace, not the cwd.
        monkeypatch.setattr(
            fleet_client, "resolve_node_id", lambda base_path=None: identity.node_id if base_path else NODE_A
        )
        _post(url, token, _claim(node=identity.node_id, target="api", holder="dead", lock=_lease(run_dir=str(run_dir))))
        # A directory inside the workspace is refused, never resolved upward.
        (ws / "pkg").mkdir()
        assert cli.main(["fleet", "claims", "--release", str(ws / "pkg"), "--path"]) == 1
        assert "is inside the workspace" in capsys.readouterr().err
        assert cli.main(["fleet", "claims", "--release", str(tmp_path / "missing"), "--path"]) == 1
        assert "not a directory" in capsys.readouterr().err
        assert [c["target"] for c in fleet_client.fetch_claims()] == ["api"]
        assert cli.main(["fleet", "claims", "--release", str(ws), "--path"]) == 0
        out = capsys.readouterr().out
        assert "released claim on 'api'" in out and identity.node_id in out
        assert fleet_client.fetch_claims() == []

    def test_release_refuses_a_claim_re_acquired_after_inspect(self, hub, tmp_path, monkeypatch, capsys):
        from brigade import cli

        url, token, _db = hub
        _env(monkeypatch, url, token)
        _ws, run_dir = self._dead_run(tmp_path)
        _post(url, token, _claim(target="api", holder="dead", lock=_lease(run_dir=str(run_dir))))
        real_inspect = fleet_client.inspect_claim

        def inspect_then_new_run(target, **kwargs):
            probe = real_inspect(target, **kwargs)
            # A new run on this node takes the target between inspect and release.
            _post(url, token, _claim("release", target="api", holder="dead"))
            time.sleep(0.002)
            _post(url, token, _claim(target="api", holder="fresh", conductor="new-run"))
            return probe

        monkeypatch.setattr(fleet_client, "inspect_claim", inspect_then_new_run)
        assert cli.main(["fleet", "claims", "--release", "api"]) == 1
        assert "re-acquired since it was inspected" in capsys.readouterr().err
        # 409, nothing deleted: the fresh run's claim survives and renews.
        assert [c["owner_conductor"] for c in fleet_client.fetch_claims()] == ["new-run"]
        assert _post(url, token, _claim("renew", target="api", holder="fresh"))[1]["renewed"] is True

    def test_release_missing_misuse_and_no_hub(self, hub, tmp_path, monkeypatch, capsys):
        from brigade import cli

        url, token, _db = hub
        _env(monkeypatch, url, token)
        # Probe found nothing this node owns: refused without --force (a
        # fresh run could acquire the key between the probe and the delete).
        assert cli.main(["fleet", "claims", "--release", "repo-a"]) == 1
        err = capsys.readouterr().err
        assert "no claim owned by this node" in err and "--force" in err
        assert cli.main(["fleet", "claims", "--release", "repo-a", "--json"]) == 1
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert payload["released"] is False and payload["owner"] is None
        assert "no claim owned by this node" in captured.err
        assert cli.main(["fleet", "claims", "--release", "repo-a", "--force"]) == 0
        assert "no active claim on 'repo-a'" in capsys.readouterr().out
        for flag in (["--force"], ["--path"], ["--node", NODE_B]):
            assert cli.main(["fleet", "claims", *flag]) == 2
            assert "require --release" in capsys.readouterr().err
        monkeypatch.setattr(fleet_client, "resolve_node_id", lambda base_path=None: "unknown")
        assert cli.main(["fleet", "claims", "--release", "repo-a"]) == 1
        assert "no usable fleet node identity" in capsys.readouterr().err
        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", "http://127.0.0.1:9")
        monkeypatch.setattr(fleet_client, "resolve_node_id", lambda base_path=None: NODE_A)
        assert cli.main(["fleet", "claims", "--release", "repo-a"]) == 1
        assert "fleet hub claim lookup failed" in capsys.readouterr().err
        assert cli.main(["fleet", "claims", "--release", "repo-a", "--force"]) == 1
        assert "fleet hub claim release failed" in capsys.readouterr().err
        monkeypatch.delenv("BRIGADE_FLEET_HUB_URL", raising=False)
        assert cli.main(["fleet", "claims", "--release", "repo-a"]) == 1
        assert "no fleet hub configured" in capsys.readouterr().err

    def test_release_against_an_older_hub_names_the_skew(self, hub, monkeypatch, capsys):
        from brigade import cli

        url, token, _db = hub
        _env(monkeypatch, url, token)
        monkeypatch.setattr(
            fleet_client,
            "_post_claim_blocking",
            lambda *a, **kw: (400, {"error": "claim field 'action' must be one of: acquire, renew, release"}),
        )
        assert cli.main(["fleet", "claims", "--release", "repo-a"]) == 1
        err = capsys.readouterr().err
        assert "claim field 'action'" in err and "predates token-less release" in err


class TestDispatchRecovery:
    """SIGKILL-style: a dead run's lock and its unexpired hub claim, same node."""

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

    def _run(self, ws: Path, roster_path: Path, *extra: str) -> int:
        from brigade import cli

        return cli.main(["run", "do something", "--roster", str(roster_path), "--cwd", str(ws), *extra])

    def _plant_dead_run_lock(self, ws: Path, owner: dict | None = None) -> Path:
        """A run.lock whose owner pid is gone and whose run never finished."""
        from brigade import runguard

        abandoned = ws / ".brigade" / "runs" / "abandoned"
        abandoned.mkdir(parents=True)
        (abandoned / "run.json").write_text(json.dumps({"schema": "brigade.run.v1", "status": "dispatching"}))
        lock = runguard.lock_path(ws)
        lock.mkdir(parents=True)
        (lock / "pid").write_text(f"{DEAD_PID}\n")
        (lock / "owner.json").write_text(json.dumps(owner or _owner(run_dir=str(abandoned.resolve()))))
        return abandoned

    def test_dead_owner_lock_supersedes_own_stale_claim_without_ttl_wait(self, hub, workspace, monkeypatch, caplog):
        from brigade import aboyeur, node as node_mod

        url, token, _db = hub
        _env(monkeypatch, url, token)
        ws, roster_path = workspace
        identity = node_mod.load_identity(ws)
        assert identity is not None
        # The killed run's claim: this node, full default TTL, a token nobody
        # has, taken under the lease its run.lock still records.
        status, payload = _post(
            url, token, _claim(node=identity.node_id, target="ws", holder="dead", conductor="chef", lock=_lease())
        )
        assert status == 200
        dead_claim = payload["claim"]
        abandoned = self._plant_dead_run_lock(ws)
        held_during_run = []
        monkeypatch.setattr(aboyeur, "run", lambda *a, **kw: held_during_run.extend(fleet_client.fetch_claims()) or 0)
        started = time.monotonic()
        with caplog.at_level("WARNING", logger="brigade.fleet"):
            assert self._run(ws, roster_path) == 0
        assert time.monotonic() - started < dead_claim["ttl_seconds"] / 10
        assert [(c["target"], c["owner_node"]) for c in held_during_run] == [("ws", identity.node_id)]
        assert held_during_run[0]["acquired_at"] != dead_claim["acquired_at"]
        assert sum("superseded this node's unexpired claim" in r.message for r in caplog.records) == 1
        # The lease reconcile terminalized the dead run, and the run released its claim on exit.
        assert json.loads((abandoned / "run.json").read_text())["status"] == "failed"
        assert fleet_client.fetch_claims() == []

    def test_run_records_its_lease_so_its_own_crash_can_be_superseded(self, hub, workspace, monkeypatch, caplog):
        """End to end: the row a real run takes carries its run.lock lease, so
        after that run is SIGKILLed the next run's reconcile supersedes it."""
        from brigade import aboyeur, runguard

        url, token, _db = hub
        _env(monkeypatch, url, token)
        ws, roster_path = workspace
        captured: dict = {}

        def first_run(*a, **kw):
            captured["owner"] = runguard.read_lock_owner(runguard.lock_path(ws))
            return 0

        monkeypatch.setattr(aboyeur, "run", first_run)
        real_release = fleet_client.release_claim
        # SIGKILL stand-in: the run never gets to release its claim.
        monkeypatch.setattr(
            fleet_client,
            "release_claim",
            lambda *a, **kw: fleet_client.ClaimDecision(granted=False, reason="hub-unavailable"),
        )
        assert self._run(ws, roster_path) == 0
        monkeypatch.setattr(fleet_client, "release_claim", real_release)
        assert len(fleet_client.fetch_claims()) == 1
        owner = captured["owner"]
        assert owner is not None and owner["owner_token"] and owner["run_dir"]
        # The crashed run's lock, exactly as SIGKILL leaves it.
        lock = runguard.lock_path(ws)
        lock.mkdir(parents=True)
        (lock / "pid").write_text(f"{DEAD_PID}\n")
        (lock / "owner.json").write_text(json.dumps({**owner, "pid": DEAD_PID}))
        monkeypatch.setattr(aboyeur, "run", lambda *a, **kw: 0)
        with caplog.at_level("WARNING", logger="brigade.fleet"):
            assert self._run(ws, roster_path) == 0
        assert sum("superseded this node's unexpired claim" in r.message for r in caplog.records) == 1
        assert fleet_client.fetch_claims() == []

    def test_dead_lock_elsewhere_never_supersedes_a_live_run(self, hub, tmp_path, monkeypatch, capsys):
        """Two workspaces with one basename on one node, or two machines with
        a cloned node.toml: same node_id, same claim key. ws-a's live run
        holds 'api' under its lease; ws-b reconciles its own dead lock. The
        supersede presents ws-b's dead lease, which is not the row's: refused,
        and ws-a's run is undisturbed."""
        from brigade import aboyeur

        url, token, _db = hub
        _env(monkeypatch, url, token)
        monkeypatch.setenv("BRIGADE_HOME", str(tmp_path / "home"))
        monkeypatch.setattr(fleet_client, "resolve_node_id", lambda base_path=None: NODE_A)
        ws_b = tmp_path / "b" / "api"
        (ws_b / ".brigade").mkdir(parents=True)
        roster_path = ws_b / "roster.toml"
        roster_path.write_text('orchestrator = "chef"\n\n[agents.chef]\ncli = "codex"\nrole = "plan"\n')
        live = _claim(node=NODE_A, target="api", holder="live-a", conductor="chef", lock=_lease("lease-a"))
        assert _post(url, token, live)[0] == 200
        self._plant_dead_run_lock(ws_b, _owner("dead-in-b"))
        dispatched = []
        monkeypatch.setattr(aboyeur, "run", lambda *a, **kw: dispatched.append(1) or 0)
        assert self._run(ws_b, roster_path) == 2
        err = capsys.readouterr().err
        assert f"claimed by node {NODE_A}" in err and "brigade fleet claims --release api" in err
        assert dispatched == []
        assert [c["owner_conductor"] for c in fleet_client.fetch_claims()] == ["chef"]
        assert _post(url, token, _claim("renew", node=NODE_A, target="api", holder="live-a"))[1]["renewed"] is True

    def test_own_stale_claim_without_dead_lock_is_refused_with_hint(self, hub, workspace, monkeypatch, capsys):
        from brigade import aboyeur, node as node_mod

        url, token, _db = hub
        _env(monkeypatch, url, token)
        ws, roster_path = workspace
        identity = node_mod.load_identity(ws)
        assert identity is not None
        _post(url, token, _claim(node=identity.node_id, target="ws", holder="dead", lock=_lease()))
        dispatched = []
        monkeypatch.setattr(aboyeur, "run", lambda *a, **kw: dispatched.append(1) or 0)
        assert self._run(ws, roster_path) == 2
        err = capsys.readouterr().err
        assert f"claimed by node {identity.node_id}" in err
        assert "brigade fleet claims --release ws" in err and "--no-fleet-claim" in err
        assert dispatched == []
        assert [c["owner_node"] for c in fleet_client.fetch_claims()] == [identity.node_id]

    def test_dead_owner_lock_never_steals_another_nodes_claim(self, hub, workspace, monkeypatch, capsys):
        from brigade import aboyeur

        url, token, _db = hub
        _env(monkeypatch, url, token)
        ws, roster_path = workspace
        _post(url, token, _claim(node=NODE_B, target="ws", holder="hb", lock=_lease()))
        self._plant_dead_run_lock(ws)
        dispatched = []
        monkeypatch.setattr(aboyeur, "run", lambda *a, **kw: dispatched.append(1) or 0)
        assert self._run(ws, roster_path) == 2
        assert f"claimed by node {NODE_B}" in capsys.readouterr().err
        assert dispatched == []
        assert [c["owner_node"] for c in fleet_client.fetch_claims()] == [NODE_B]

    def test_no_fleet_claim_skips_hub_and_logs_once(self, hub, workspace, monkeypatch, caplog):
        from brigade import aboyeur

        url, token, _db = hub
        _env(monkeypatch, url, token)
        ws, roster_path = workspace
        _post(url, token, _claim(node=NODE_B, target="ws", holder="hb"))
        held_during_run = []
        monkeypatch.setattr(aboyeur, "run", lambda *a, **kw: held_during_run.extend(fleet_client.fetch_claims()) or 0)
        with caplog.at_level("WARNING", logger="brigade.fleet"):
            assert self._run(ws, roster_path, "--no-fleet-claim") == 0
        # Ran despite the other node's claim, and never touched it.
        assert [c["owner_node"] for c in held_during_run] == [NODE_B]
        assert [c["owner_node"] for c in fleet_client.fetch_claims()] == [NODE_B]
        skipped = [r.message for r in caplog.records if "--no-fleet-claim" in r.message]
        assert len(skipped) == 1 and "ws" in skipped[0]


def test_detached_child_argv_forwards_no_fleet_claim(tmp_path):
    from brigade import roster as roster_mod
    from brigade.cli import run as run_cli

    def args(**overrides):
        base = dict(
            task="do work",
            allow_dirty=False,
            worktree=False,
            show_plan=False,
            verbose=False,
            read_only=False,
            no_code_graph=False,
            no_evidence=False,
            sandbox=None,
            codex_transport=None,
            handoff=False,
            handoff_inbox=None,
            worker=None,
            wait=None,
            keep_going=False,
            scheduler="waves",
            run_budget=None,
        )
        base.update(overrides)
        return argparse.Namespace(**base)

    resolution = roster_mod.RosterResolution(path=(tmp_path / "roster.toml").resolve(), source="workspace", shadowed=())
    argv = run_cli._detached_child_argv(
        args(no_fleet_claim=True), run_cwd=tmp_path, roster_resolution=resolution, output_dir=tmp_path / "run"
    )
    assert "--no-fleet-claim" in argv
    argv = run_cli._detached_child_argv(
        args(no_fleet_claim=False), run_cwd=tmp_path, roster_resolution=resolution, output_dir=tmp_path / "run"
    )
    assert "--no-fleet-claim" not in argv


def test_run_lock_reports_reconciled_dead_owner_and_its_own_lease(tmp_path):
    from brigade import runguard

    repo = tmp_path / "repo"
    repo.mkdir()
    abandoned = tmp_path / "abandoned"
    abandoned.mkdir()
    (abandoned / "run.json").write_text(json.dumps({"schema": "brigade.run.v1", "status": "dispatching"}))
    lock = runguard.lock_path(repo)
    lock.mkdir(parents=True)
    (lock / "pid").write_text(f"{DEAD_PID}\n")
    (lock / "owner.json").write_text(json.dumps(_owner(run_dir=str(abandoned))))
    seen: list = []
    with runguard.run_lock(repo, run_dir=tmp_path / "new-run", on_reconcile=seen.append) as path:
        assert len(seen) == 1
        mine = runguard.read_lock_owner(path)
        assert mine is not None and mine["pid"] == os.getpid() and mine["owner_token"] != "dead-owner"
        assert fleet_client.lease_from_lock_owner(mine) == {
            "token": mine["owner_token"],
            "acquired_at": mine["acquired_at"],
            "run_dir": str((tmp_path / "new-run").resolve()),
        }
    assert seen[0] is not None and seen[0]["owner_token"] == "dead-owner" and seen[0]["pid"] == DEAD_PID
    assert runguard.read_lock_owner(path) is None
    # A free lock and a live owner never report a reconcile.
    seen.clear()
    with runguard.run_lock(repo, on_reconcile=seen.append):
        pass
    assert seen == []
    lock.mkdir(parents=True)
    (lock / "pid").write_text(f"{os.getpid()}\n")
    with pytest.raises(runguard.RunLockError):
        with runguard.run_lock(repo, on_reconcile=seen.append):
            pass  # pragma: no cover - never entered
    assert seen == []
