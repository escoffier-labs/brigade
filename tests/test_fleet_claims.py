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
    # Legacy shared-token mode (pre-#1150): the one token posts as any node.
    server = fleet_hub.make_server("127.0.0.1", 0, db, token, allow_admin_writes=True)
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


def _claim(action: str = "acquire", node: str = NODE_A, target: str = "repo-a", holder: str = "h1", **extra) -> dict:
    return {"action": action, "node_id": node, "target": target, "holder": holder, **extra}


def _plant_home_identity(home: Path, node_id: str) -> None:
    """Write the per-user machine identity at ``<home>/.brigade/node.toml``."""
    from brigade import node as node_mod

    identity = node_mod.NodeIdentity(node_id=node_id, hostname="fleet-test", roles=(), platform="test")
    path = node_mod.node_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(node_mod._format_node_toml(identity), encoding="utf-8")


def _bound_find_workspace_to_tmp(monkeypatch, tmp_path: Path) -> None:
    """Keep real discovery, but never climb past the pytest sandbox.

    With ``TMPDIR`` under a host home that has ``~/.brigade/node.toml``,
    ``find_workspace_for_path`` otherwise returns that host workspace.
    """
    real = fleet_client.find_workspace_for_path
    try:
        bound = tmp_path.resolve()
    except OSError:
        bound = tmp_path

    def find_workspace_for_path(start: Path) -> Path | None:
        found = real(start)
        if found is None:
            return None
        try:
            return found if found.resolve().is_relative_to(bound) else None
        except OSError:
            return None

    monkeypatch.setattr(fleet_client, "find_workspace_for_path", find_workspace_for_path)


class TestMachineIdentity:
    """Hub-facing claim identity is the per-user home identity (#1161): the
    nearest workspace ``.brigade/node.toml`` stays local and is never sent."""

    def _env(self, hub, tmp_path, monkeypatch) -> Path:
        from brigade import node as node_mod

        url, token, _db = hub
        home = tmp_path / "home"
        monkeypatch.setenv("BRIGADE_HOME", str(home))
        _plant_home_identity(home, NODE_A)
        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", url)
        monkeypatch.setenv("BRIGADE_FLEET_TOKEN", token)
        ws = tmp_path / "ws"
        (ws / ".brigade").mkdir(parents=True)
        workspace_identity = node_mod.ensure_identity(ws)
        assert workspace_identity.node_id not in (NODE_A, NODE_B)
        return ws

    def test_claims_resolve_the_home_identity_through_base_path(self, hub, tmp_path, monkeypatch):
        ws = self._env(hub, tmp_path, monkeypatch)
        won = fleet_client.acquire_claim("ws", base_path=ws / "src" / "deep")
        assert won.granted is True
        assert won.claim is not None and won.claim["owner_node"] == NODE_A
        assert fleet_client.release_claim("ws", holder=won.holder, node_id=NODE_A).granted is True

    def test_repo_claim_holds_under_the_home_identity(self, hub, tmp_path, monkeypatch):
        ws = self._env(hub, tmp_path, monkeypatch)
        with fleet_client.repo_claim("ws", base_path=ws) as decision:
            assert decision.granted is True
            assert decision.claim is not None and decision.claim["owner_node"] == NODE_A
        assert fleet_client.fetch_claims() == []


class TestClaimAuthRefusal:
    """A claim HTTP 401/403 is a stable auth-failed answer (#1161): never
    retried, never degraded to the local run lock, and repo_claim fails
    closed instead of yielding a run onto rejected credentials."""

    def _machine_env(self, monkeypatch, tmp_path) -> None:
        home = tmp_path / "home"
        monkeypatch.setenv("BRIGADE_HOME", str(home))
        _plant_home_identity(home, NODE_A)
        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", "http://127.0.0.1:9")
        monkeypatch.setenv("BRIGADE_FLEET_TOKEN", "not-a-valid-token")

    def _refusing_hub(self, monkeypatch, status: int) -> list[int]:
        calls: list[int] = []

        def refuse(*args, **kwargs):
            calls.append(1)
            return status, {"error": f"HTTP {status}"}

        monkeypatch.setattr(fleet_client, "_post_claim_blocking", refuse)
        return calls

    @pytest.mark.parametrize("status", [401, 403], ids=["http-401", "http-403"])
    def test_auth_refusal_is_auth_failed_and_not_retried(self, tmp_path, monkeypatch, status):
        self._machine_env(monkeypatch, tmp_path)
        calls = self._refusing_hub(monkeypatch, status)
        decision = fleet_client.acquire_claim("repo-a")
        assert decision.granted is False
        assert decision.reason == "auth-failed"
        assert "HTTP" in (decision.detail or "")
        assert len(calls) == 1  # retrying cannot heal an enrollment problem

    @pytest.mark.parametrize("status", [401, 403], ids=["http-401", "http-403"])
    def test_repo_claim_raises_without_orphan_release_or_yield(self, tmp_path, monkeypatch, status):
        self._machine_env(monkeypatch, tmp_path)
        calls = self._refusing_hub(monkeypatch, status)
        orphan_releases: list[str] = []
        monkeypatch.setattr(
            fleet_client,
            "_schedule_orphan_release",
            lambda target, **kw: orphan_releases.append(target),
        )
        entered = False
        with pytest.raises(fleet_client.FleetClaimAuthError) as excinfo:
            with fleet_client.repo_claim("repo-a"):
                entered = True
        assert entered is False  # the guarded block never runs unprotected
        message = str(excinfo.value)
        assert "rejected this node's credentials" in message and "'repo-a'" in message
        assert "brigade fleet nodes add" in message
        assert len(calls) == 1  # no retry either
        assert orphan_releases == []  # no orphan-release fallback on bad credentials


class TestHeartbeatCredentialRefusal:
    """A credential refusal seen by the *mid-run heartbeat* (#1161) is handed
    to ``on_credential_failure`` exactly once and stops the heartbeat;
    transport/5xx failures keep the existing retry behavior and never call it."""

    @pytest.fixture(autouse=True)
    def _machine(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setenv("BRIGADE_HOME", str(home))
        _plant_home_identity(home, NODE_A)
        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", "http://127.0.0.1:9")
        monkeypatch.setenv("BRIGADE_FLEET_TOKEN", "any-bearer")

    @pytest.mark.parametrize("status", [401, 403], ids=["http-401", "http-403"])
    def test_midrun_refusal_invokes_the_callback_once_and_stops(self, tmp_path, monkeypatch, status):
        monkeypatch.setattr(fleet_client, "_claim_renew_interval", lambda ttl: 0)
        state = {"renew_attempts": 0}

        def post(hub_url, tok, body, *, timeout):
            action = body["action"]
            if action == "acquire":
                return 200, {"granted": True, "claim": {"target": body["target"], "owner_node": body["node_id"]}}
            if action == "renew":
                state["renew_attempts"] += 1
                return status, {"error": f"token revoked (HTTP {status})"}
            return 200, {"released": True}

        monkeypatch.setattr(fleet_client, "_post_claim_blocking", post)
        seen: list[str | None] = []
        with fleet_client.repo_claim("repo-a", ttl_seconds=60, on_credential_failure=seen.append) as decision:
            assert decision.granted is True
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not seen:
                time.sleep(0.02)
            assert seen == [f"token revoked (HTTP {status})"]
        # Exactly one refused heartbeat handed over the hub's detail, and the
        # stopped thread made no further attempts before the bounded join.
        assert len(seen) == 1
        assert state["renew_attempts"] == 1

    @pytest.mark.parametrize("refusal", ["http-500", "timeout"], ids=["http-500", "network-timeout"])
    def test_transport_failures_keep_retrying_without_the_callback(self, tmp_path, monkeypatch, refusal):
        monkeypatch.setattr(fleet_client, "_claim_renew_interval", lambda ttl: 0)
        state = {"renew_attempts": 0}

        def post(hub_url, tok, body, *, timeout):
            action = body["action"]
            if action == "acquire":
                return 200, {"granted": True, "claim": {"target": body["target"], "owner_node": body["node_id"]}}
            if action == "renew":
                state["renew_attempts"] += 1
                if refusal == "timeout":
                    raise TimeoutError("hub unreachable")
                return 500, {"error": "boom"}
            return 200, {"released": True}

        monkeypatch.setattr(fleet_client, "_post_claim_blocking", post)
        seen: list[str | None] = []
        with fleet_client.repo_claim("repo-a", ttl_seconds=60, on_credential_failure=seen.append) as decision:
            assert decision.granted is True
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and state["renew_attempts"] < 3:
                time.sleep(0.02)
        assert seen == []  # a transport failure is never a credential answer
        assert state["renew_attempts"] >= 3, "heartbeat stopped retrying on a transport failure"


class TestHubClaims:
    def test_acquire_preserves_validated_external_harness_labels(self, hub):
        url, token, _db = hub
        status, payload = _post(
            url,
            token,
            _claim(
                harness="grokbot",
                role="implementation-worker",
                job="grokbot-" + "a" * 24,
                session="lease-a",
            ),
        )

        assert status == 200 and payload["granted"] is True
        claim = payload["claim"]
        assert {key: claim[key] for key in ("harness", "role", "job", "session")} == {
            "harness": "grokbot",
            "role": "implementation-worker",
            "job": "grokbot-" + "a" * 24,
            "session": "lease-a",
        }
        assert "holder_token" not in claim
        listed = _get(url, "/claims", token)[1]["claims"]
        assert len(listed) == 1
        assert {key: listed[0][key] for key in ("harness", "role", "job", "session")} == {
            "harness": "grokbot",
            "role": "implementation-worker",
            "job": "grokbot-" + "a" * 24,
            "session": "lease-a",
        }

    def test_acquire_rejects_invalid_opaque_labels(self, hub):
        url, token, _db = hub
        for field, value, status in (
            ("harness", " ", 400),
            ("role", "x" * 129, 400),
            ("job", "job\x1b[31m", 422),
            ("session", "lease\x07", 422),
        ):
            code, payload = _post(url, token, _claim(**{field: value}))
            assert code == status, (field, value, code, payload)
            assert field in payload["error"] or "control" in payload["error"]
        assert _get(url, "/claims", token)[1]["claims"] == []

    def test_opaque_labels_are_acquire_only(self, hub):
        url, token, _db = hub
        assert _post(url, token, _claim(harness="grokbot", session="lease-a"))[0] == 200
        for action in ("renew", "release"):
            status, payload = _post(url, token, _claim(action, harness="grokbot"))
            assert status == 400, action
            assert "harness" in payload["error"]
        assert _get(url, "/claims", token)[1]["claims"][0]["harness"] == "grokbot"

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
        assert "holder_token" not in claim  # the fencing token is never echoed
        # Machine B collides: one winner, one refusal naming the owner.
        status, payload = _post(url, token, _claim(node=NODE_B, holder="h2"))
        assert status == 409 and payload["granted"] is False
        assert NODE_A in payload["error"]
        assert payload["owner"]["owner_node"] == NODE_A
        assert payload["owner"]["expires_at"] == claim["expires_at"]
        assert "holder_token" not in payload["owner"]

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
        assert _post(url, token, _claim(node=NODE_B, holder="h2"))[0] == 409  # not silently stolen
        clock["now"] += 2  # past the TTL: crashed owner auto-reclaims
        status, payload = _post(url, token, _claim(node=NODE_B, holder="h2"))
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
        assert _post(url, token, _claim(conductor="c1", holder="hc1"))[1]["granted"] is True
        status, payload = _post(url, token, _claim(conductor="c2", holder="hc2"))
        assert status == 409 and payload["owner"]["owner_conductor"] == "c1"
        assert _post(url, token, _claim(conductor="c1", holder="hc1"))[1]["granted"] is True

    def test_holder_token_fences_sibling_release_and_renew(self, hub):
        """A sibling run sharing the node identity cannot touch a live claim."""
        url, token, _db = hub
        assert _post(url, token, _claim(holder="hA"))[1]["granted"] is True
        # Same node, same conductor, different (or unknown) fencing token:
        # release and renew are both refused and the row survives.
        status, payload = _post(url, token, _claim("release", holder="hB"))
        assert status == 409 and payload["released"] is False
        assert payload["owner"]["owner_node"] == NODE_A
        status, payload = _post(url, token, _claim("renew", holder="hB"))
        assert status == 409 and payload["renewed"] is False
        assert [c["target"] for c in _get(url, "/claims", token)[1]["claims"]] == ["repo-a"]
        # The real holder still can.
        assert _post(url, token, _claim("renew", holder="hA"))[1]["renewed"] is True
        assert _post(url, token, _claim("release", holder="hA"))[1]["released"] is True

    def test_validation_rejects_malformed_requests(self, hub):
        url, token, _db = hub
        for bad in (
            [],
            {**_claim(), "action": "steal"},
            {**_claim(), "action": None},
            {**_claim(), "target": " "},
            {"action": "acquire", "target": "repo-a", "holder": "h1"},
            {"action": "acquire", "target": "repo-a", "node_id": NODE_A},
            {**_claim(), "ttl_seconds": 0},
            {**_claim(), "ttl_seconds": fleet_hub.CLAIM_TTL_MAX_SECONDS + 1},
            {**_claim(), "ttl_seconds": True},
            {**_claim(), "ttl_seconds": "900"},
        ):
            status, payload = _post(url, token, bad)
            assert status == 400, bad
            assert "error" in payload
        assert _post(url, token, _claim(ttl_seconds=fleet_hub.CLAIM_TTL_MIN_SECONDS))[0] == 200

    def test_unknown_and_malformed_node_ids_rejected(self, hub):
        """Two identity-less or cloned nodes must never both be granted."""
        url, token, _db = hub
        for bad_id in ("unknown", "a b", "-leading-dash", "x" * 200, "node\nid"):
            status, payload = _post(url, token, _claim(node=bad_id))
            assert status == 400, bad_id
            assert "node_id" in payload["error"]
        # The holder token gets the same treatment.
        assert _post(url, token, _claim(holder="unknown"))[0] == 400
        assert _post(url, token, _claim(holder="h 1"))[0] == 400

    def test_control_characters_rejected_at_claim_ingestion(self, hub, tmp_path):
        url, token, _db = hub
        for field, value in (
            ("target", "repo\x1b[31m"),
            ("target", "repo\x07"),
            ("target", "repo\x9b"),
            ("conductor", "cond\x1b[31m"),
            ("conductor", "cond\x80"),
            ("conductor", "cond\x7f"),
        ):
            status, payload = _post(url, token, _claim(**{field: value}))
            assert status == 422, (field, value)
            assert "control" in payload["error"]
        assert _get(url, "/claims", token)[1]["claims"] == []
        # Existing identity / length caps stay 400; clean display fields still ingest.
        status, payload = _post(url, token, _claim(node="x" * 200))
        assert status == 400 and "node_id" in payload["error"]
        assert _post(url, token, _claim(target="repo-clean", conductor="cond-1"))[0] == 200
        conn = fleet_hub.init_db(tmp_path / "direct.db")
        try:
            with pytest.raises(fleet_hub.FleetHubUnprocessable):
                fleet_hub.handle_claim(conn, _claim(target="repo\x1b"))
            assert conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 0
        finally:
            conn.close()

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
        assert all("holder_token" not in c for c in everything)

    def test_acquire_prunes_expired_rows(self, hub, clock):
        url, token, db = hub
        _post(url, token, _claim(target="repo-a", ttl_seconds=100))
        _post(url, token, _claim(target="repo-b", ttl_seconds=100))
        clock["now"] += 200
        assert _post(url, token, _claim(target="repo-c", holder="h3"))[1]["granted"] is True
        conn = fleet_hub.init_db(db)
        try:
            targets = [r[0] for r in conn.execute("SELECT target FROM claims ORDER BY target").fetchall()]
        finally:
            conn.close()
        assert targets == ["repo-c"]

    def test_phase2_database_upgrades_in_place(self, tmp_path):
        db = tmp_path / "phase2.db"
        conn = sqlite3.connect(str(db))
        conn.execute(fleet_hub._SCHEMA)
        conn.execute("PRAGMA user_version=1")
        conn.commit()
        conn.close()
        conn = fleet_hub.init_db(db)
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == fleet_hub.SCHEMA_VERSION == 5
            assert conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 0
        finally:
            conn.close()

    def test_acquire_after_release_is_refused_as_stale_generation(self, hub):
        """A late-committing acquire for the released holder cannot resurrect the row."""
        url, token, _db = hub
        first = _post(url, token, _claim())[1]["claim"]
        assert first["generation"] == 1
        assert _post(url, token, _claim("release")) == (200, {"released": True})
        status, payload = _post(url, token, _claim())
        assert status == 409
        assert payload["granted"] is False
        assert payload["reason"] == "stale-generation"
        assert payload["owner"] is None
        assert _get(url, "/claims", token)[1]["claims"] == []
        # A new holder on the same node, or the same holder on another node, may take the target.
        status, payload = _post(url, token, _claim(holder="h2"))
        assert status == 200 and payload["granted"] is True
        assert payload["claim"]["generation"] == 2
        assert _post(url, token, _claim("release", holder="h2"))[1]["released"] is True
        status, payload = _post(url, token, _claim(node=NODE_B))
        assert status == 200 and payload["granted"] is True
        assert payload["claim"]["generation"] == 3

    def test_late_acquire_with_stale_generation_or_lease_stamp_is_refused(self, hub):
        url, token, _db = hub
        lock = {"token": "run-lock-1", "acquired_at": "2026-08-26T00:00:00+00:00"}
        granted = _post(url, token, _claim(lock=lock))[1]["claim"]
        assert granted["generation"] == 1
        assert _post(url, token, _claim("release"))[1]["released"] is True
        status, payload = _post(url, token, _claim(generation=1))
        assert status == 409 and payload["reason"] == "stale-generation"
        status, payload = _post(url, token, _claim(holder="h-late", lock=lock))
        assert status == 409 and payload["reason"] == "stale-generation"
        status, payload = _post(url, token, _claim("renew", generation=1))
        assert status == 409 and payload["reason"] == "stale-generation"
        assert payload["renewed"] is False
        assert _get(url, "/claims", token)[1]["claims"] == []

    def test_no_row_release_still_tombs_an_in_flight_acquire(self, hub):
        """Release before the first acquire lands still fences that holder."""
        url, token, _db = hub
        assert _post(url, token, _claim("release"))[1]["released"] is False
        status, payload = _post(url, token, _claim())
        assert status == 409 and payload["reason"] == "stale-generation"
        assert _post(url, token, _claim(holder="h-fresh"))[1]["granted"] is True

    def test_released_holder_stays_stale_after_claim_ttl(self, hub, clock):
        """Generation floor survives claim TTL: a released holder cannot
        re-acquire as generation 1 after the tombstone's former expiry."""
        url, token, _db = hub
        first = _post(url, token, _claim(ttl_seconds=60))[1]["claim"]
        assert first["generation"] == 1
        assert _post(url, token, _claim("release"))[1]["released"] is True
        clock["now"] += 61
        status, payload = _post(url, token, _claim(ttl_seconds=60))
        assert status == 409 and payload["reason"] == "stale-generation"
        assert payload["granted"] is False
        status, payload = _post(url, token, _claim(ttl_seconds=60, generation=1))
        assert status == 409 and payload["reason"] == "stale-generation"
        assert _get(url, "/claims", token)[1]["claims"] == []
        status, payload = _post(url, token, _claim(holder="h2", ttl_seconds=60))
        assert status == 200 and payload["granted"] is True
        assert payload["claim"]["generation"] == 2

    def test_generation_is_only_valid_on_acquire_or_renew(self, hub):
        url, token, _db = hub
        assert _post(url, token, _claim("release", generation=1))[0] == 400
        assert _post(url, token, _claim(generation=0))[0] == 400
        assert _post(url, token, _claim(generation=True))[0] == 400

    def test_schema_v4_rows_gain_generation_without_dropping_live_claims(self, tmp_path):
        db = tmp_path / "v4.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE claims (target TEXT NOT NULL PRIMARY KEY, owner_node TEXT NOT NULL, "
            "owner_conductor TEXT, harness TEXT, role TEXT, job TEXT, session TEXT, "
            "holder_token TEXT NOT NULL, acquired_at TEXT NOT NULL, renewed_at TEXT NOT NULL, "
            "ttl_seconds INTEGER NOT NULL, expires_at REAL NOT NULL, lock_token TEXT, "
            "lock_acquired_at TEXT, lock_run_dir TEXT)"
        )
        conn.execute(
            "INSERT INTO claims VALUES ('repo-a', ?, 'chef', NULL, NULL, NULL, NULL, 'h1', "
            "'then', 'then', 900, ?, NULL, NULL, NULL)",
            (NODE_A, time.time() + 900),
        )
        conn.execute("PRAGMA user_version=4")
        conn.commit()
        conn.close()
        conn = fleet_hub.init_db(db)
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == fleet_hub.SCHEMA_VERSION == 5
            columns = {row[1] for row in conn.execute("PRAGMA table_info(claims)").fetchall()}
            assert "generation" in columns
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            assert "claim_tombstones" in tables
            assert "claim_generation_floors" in tables
            listed = fleet_hub.list_claims(conn)
            assert listed[0]["target"] == "repo-a"
            assert listed[0]["generation"] == 1
            # A live migrated claim is not a released floor; lost-row
            # re-acquire of this generation must still be possible.
            assert (
                conn.execute("SELECT generation FROM claim_generation_floors WHERE target = 'repo-a'").fetchone()
                is None
            )
            status, payload = fleet_hub.handle_claim(conn, _claim("renew", holder="h1"))
            assert status == 200 and payload["claim"]["generation"] == 1
            assert fleet_hub.handle_claim(conn, _claim("release", holder="h1"))[1]["released"] is True
            assert conn.execute(
                "SELECT generation FROM claim_generation_floors WHERE target = 'repo-a'"
            ).fetchone() == (1,)
        finally:
            conn.close()

    def test_newer_schema_is_refused_by_init_and_open(self, tmp_path):
        db = tmp_path / "future.db"
        conn = fleet_hub.init_db(db)
        conn.execute(f"PRAGMA user_version={fleet_hub.SCHEMA_VERSION + 1}")
        conn.commit()
        conn.close()
        with pytest.raises(fleet_hub.FleetHubError, match="schema version"):
            fleet_hub.init_db(db)
        with pytest.raises(fleet_hub.FleetHubError, match="schema version"):
            fleet_hub.open_db(db)

    def test_tombstones_are_bounded_per_target_node(self, hub):
        """No-row release spam cannot grow tombstones without limit; the
        newest in-flight holder fence is kept."""
        url, token, db = hub
        limit = fleet_hub.CLAIM_TOMBSTONE_LIMIT_PER_TARGET_NODE
        for i in range(limit + 8):
            holder = f"h{i:02d}"
            assert _post(url, token, _claim("release", holder=holder, target="repo-a"))[1]["released"] is False
        conn = fleet_hub.init_db(db)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM claim_tombstones WHERE target = 'repo-a' AND owner_node = ?",
                (NODE_A,),
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == limit
        last = f"h{limit + 7:02d}"
        status, payload = _post(url, token, _claim(holder=last, target="repo-a"))
        assert status == 409 and payload["reason"] == "stale-generation"
        status, payload = _post(url, token, _claim(holder="h-fresh", target="repo-a"))
        assert status == 200 and payload["granted"] is True

    def test_pre_holder_token_claims_table_is_recreated(self, tmp_path):
        db = tmp_path / "early-v2.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE claims (target TEXT PRIMARY KEY, owner_node TEXT, expires_at REAL)")
        conn.execute("INSERT INTO claims VALUES ('stale', 'n', 0)")
        conn.execute("PRAGMA user_version=2")
        conn.commit()
        conn.close()
        conn = fleet_hub.init_db(db)
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(claims)").fetchall()}
            assert "holder_token" in columns
            assert conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 0
        finally:
            conn.close()


class TestClientClaims:
    @pytest.fixture(autouse=True)
    def _home(self, tmp_path, monkeypatch):
        self.home = tmp_path / "brigade-home"
        monkeypatch.setenv("BRIGADE_HOME", str(self.home))
        # Claims require a real node identity; give these tests one.
        monkeypatch.setattr(fleet_client, "resolve_node_id", lambda base_path=None: NODE_A)

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
        won = fleet_client.acquire_claim("repo-a", node_id=NODE_A)
        holder = won.holder
        assert won.granted is True and holder
        assert fleet_client.renew_claim("repo-a", node_id=NODE_A, holder=holder).granted is True
        assert fleet_client.renew_claim("repo-a", node_id=NODE_B, holder="h-other").reason == "held"
        assert fleet_client.release_claim("repo-a", node_id=NODE_A, holder=holder).granted is True
        assert fleet_client.renew_claim("repo-a", node_id=NODE_A, holder=holder).reason == "stale-generation"
        assert fleet_client.fetch_claims() == []

    def test_late_acquire_after_release_is_terminal_stale_generation(self, hub, monkeypatch):
        url, token, _db = hub
        self._env(monkeypatch, url, token)
        won = fleet_client.acquire_claim("repo-a", node_id=NODE_A)
        holder = won.holder
        generation = (won.claim or {}).get("generation")
        assert won.granted is True and holder and generation == 1
        assert fleet_client.release_claim("repo-a", node_id=NODE_A, holder=holder).granted is True
        late = fleet_client.acquire_claim("repo-a", node_id=NODE_A, holder=holder, generation=generation)
        assert late.granted is False and late.reason == "stale-generation"
        assert fleet_client.fetch_claims(include_all=True) == []
        fresh = fleet_client.acquire_claim("repo-a", node_id=NODE_A)
        assert fresh.granted is True and (fresh.claim or {}).get("generation") == 2

    def test_client_released_holder_stays_stale_after_claim_ttl(self, hub, clock, monkeypatch):
        url, token, _db = hub
        self._env(monkeypatch, url, token)
        won = fleet_client.acquire_claim("repo-a", node_id=NODE_A, ttl_seconds=60)
        holder = won.holder
        assert won.granted is True and (won.claim or {}).get("generation") == 1
        assert fleet_client.release_claim("repo-a", node_id=NODE_A, holder=holder).granted is True
        clock["now"] += 61
        late = fleet_client.acquire_claim("repo-a", node_id=NODE_A, holder=holder, ttl_seconds=60, generation=1)
        assert late.granted is False and late.reason == "stale-generation"
        assert fleet_client.fetch_claims(include_all=True) == []

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

    def test_repo_claim_without_identity_stays_local(self, hub, monkeypatch, caplog):
        """No usable node identity: never contact the hub, warn, run on the local lock."""
        url, token, _db = hub
        self._env(monkeypatch, url, token)
        monkeypatch.setattr(fleet_client, "resolve_node_id", lambda base_path=None: "unknown")
        with caplog.at_level("WARNING", logger="brigade.fleet"):
            with fleet_client.repo_claim("repo-a") as decision:
                assert (decision.granted, decision.reason) == (False, "no-identity")
        assert fleet_client.fetch_claims(include_all=True) == []
        assert sum("no usable fleet node identity" in r.message for r in caplog.records) == 1

    def test_lost_acquire_response_recovers_with_same_holder(self, hub, monkeypatch):
        """The hub commits but the response is lost: one retry with the same
        fencing token is granted idempotently and the run stays protected."""
        url, token, _db = hub
        self._env(monkeypatch, url, token)
        real = fleet_client._post_claim_blocking
        calls = {"acquire": 0}

        def lossy(hub_url, tok, body, *, timeout):
            status, payload = real(hub_url, tok, body, timeout=timeout)
            if body["action"] == "acquire":
                calls["acquire"] += 1
                if calls["acquire"] == 1:
                    raise TimeoutError("response lost after commit")
            return status, payload

        monkeypatch.setattr(fleet_client, "_post_claim_blocking", lossy)
        with fleet_client.repo_claim("repo-a") as decision:
            assert decision.granted is True
            assert [c["target"] for c in fleet_client.fetch_claims()] == ["repo-a"]
        assert calls["acquire"] == 2
        assert fleet_client.fetch_claims() == []

    def test_lost_acquire_twice_schedules_orphan_release(self, hub, monkeypatch):
        """Both acquire responses lost: fail open locally, but a background
        release clears the row the hub committed so other machines are not
        blocked for the full TTL."""
        url, token, _db = hub
        self._env(monkeypatch, url, token)
        monkeypatch.setattr(fleet_client, "ORPHAN_RELEASE_RETRY_SECONDS", 0.05)
        real = fleet_client._post_claim_blocking

        def lossy(hub_url, tok, body, *, timeout):
            status, payload = real(hub_url, tok, body, timeout=timeout)
            if body["action"] == "acquire":
                raise TimeoutError("response lost after commit")
            return status, payload

        monkeypatch.setattr(fleet_client, "_post_claim_blocking", lossy)
        with fleet_client.repo_claim("repo-a", ttl_seconds=60) as decision:
            assert (decision.granted, decision.reason) == (False, "hub-unavailable")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and fleet_client.fetch_claims(include_all=True):
                time.sleep(0.02)
            assert fleet_client.fetch_claims(include_all=True) == [], "orphaned claim never released"

    def test_freed_mid_request_is_retried_once(self, hub, monkeypatch):
        """A 409 with no owner means the target freed mid-request; retry
        instead of refusing a target nobody holds."""
        url, token, _db = hub
        self._env(monkeypatch, url, token)
        real = fleet_client._post_claim_blocking
        calls = {"n": 0}

        def freed_once(hub_url, tok, body, *, timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                return 409, {"granted": False, "owner": None, "error": "raced"}
            return real(hub_url, tok, body, timeout=timeout)

        monkeypatch.setattr(fleet_client, "_post_claim_blocking", freed_once)
        with fleet_client.repo_claim("repo-a") as decision:
            assert decision.granted is True
            assert [c["target"] for c in fleet_client.fetch_claims()] == ["repo-a"]
        assert fleet_client.fetch_claims() == []

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

    def test_timed_out_heartbeat_reacquire_releases_orphan(self, hub, monkeypatch):
        """A re-acquire that commits after its deadline is released after exit."""
        url, token, db = hub
        self._env(monkeypatch, url, token)
        monkeypatch.setattr(fleet_client, "_claim_renew_interval", lambda ttl: 0)
        monkeypatch.setattr(fleet_client, "ORPHAN_RELEASE_RETRY_SECONDS", 0)
        real_post = fleet_client._post_claim_blocking
        real_deadline = fleet_client._run_with_deadline
        real_release = fleet_client.release_claim
        real_schedule = fleet_client._schedule_orphan_release
        allow_missing = threading.Event()
        allow_reacquire = threading.Event()
        reacquire_started = threading.Event()
        reacquire_finished = threading.Event()
        timeout_observed = threading.Event()
        cleanup_scheduled = threading.Event()
        acquire_calls = 0
        deadline_calls = 0
        release_calls = 0
        renew_calls = 0

        def blocked_reacquire(hub_url, tok, body, *, timeout):
            nonlocal acquire_calls
            if body["action"] == "acquire":
                acquire_calls += 1
                if acquire_calls == 2:
                    reacquire_started.set()
                    assert allow_reacquire.wait(30)
                    result = real_post(hub_url, tok, body, timeout=timeout)
                    reacquire_finished.set()
                    return result
            return real_post(hub_url, tok, body, timeout=timeout)

        def controlled_deadline(fn, *, timeout):
            nonlocal deadline_calls
            deadline_calls += 1
            if deadline_calls == 2:
                worker = threading.Thread(target=fn, daemon=True)
                worker.start()
                assert reacquire_started.wait(30)
                timeout_observed.set()
                raise TimeoutError("controlled abandoned re-acquire")
            return real_deadline(fn, timeout=timeout)

        def controlled_renew(target, **kwargs):
            nonlocal renew_calls
            assert allow_missing.wait(30)
            renew_calls += 1
            if renew_calls == 1:
                return fleet_client.ClaimDecision(granted=False, reason="missing", holder=kwargs.get("holder"))
            return fleet_client.ClaimDecision(granted=False, reason="held", holder=kwargs.get("holder"))

        def controlled_release(target, **kwargs):
            nonlocal release_calls
            release_calls += 1
            if release_calls == 1:
                return fleet_client.ClaimDecision(granted=False, reason="hub-unavailable", holder=kwargs.get("holder"))
            return real_release(target, **kwargs)

        def tracking_schedule(*args, **kwargs):
            cleanup_scheduled.set()
            return real_schedule(*args, **kwargs)

        monkeypatch.setattr(fleet_client, "_post_claim_blocking", blocked_reacquire)
        monkeypatch.setattr(fleet_client, "_run_with_deadline", controlled_deadline)
        monkeypatch.setattr(fleet_client, "renew_claim", controlled_renew)
        monkeypatch.setattr(fleet_client, "release_claim", controlled_release)
        monkeypatch.setattr(fleet_client, "_schedule_orphan_release", tracking_schedule)
        with fleet_client.repo_claim("repo-a", ttl_seconds=60):
            conn = fleet_hub.init_db(db)
            try:
                conn.execute("DELETE FROM claims")
                conn.commit()
            finally:
                conn.close()
            allow_missing.set()
            assert reacquire_started.wait(30)
            assert timeout_observed.wait(30), "heartbeat did not abandon the blocked re-acquire"
            allow_reacquire.set()
            assert reacquire_finished.wait(30), "abandoned re-acquire never committed"

        assert release_calls == 2, "exit did not retry the unavailable release inline"
        assert not cleanup_scheduled.is_set(), "exit scheduled daemon cleanup"
        assert fleet_client.fetch_claims(include_all=True) == [], "timed-out re-acquire left an orphaned claim"

    def test_timed_out_heartbeat_reacquire_stays_held_until_exit(self, hub, monkeypatch):
        """Late re-acquire cleanup cannot release a claim while its run is live."""
        url, token, db = hub
        self._env(monkeypatch, url, token)
        monkeypatch.setattr(fleet_client, "_claim_renew_interval", lambda ttl: 0)
        monkeypatch.setattr(fleet_client, "ORPHAN_RELEASE_RETRY_SECONDS", 0)
        real_post = fleet_client._post_claim_blocking
        real_deadline = fleet_client._run_with_deadline
        real_renew = fleet_client.renew_claim
        real_schedule = fleet_client._schedule_orphan_release
        allow_missing = threading.Event()
        allow_reacquire = threading.Event()
        reacquire_started = threading.Event()
        reacquire_finished = threading.Event()
        timeout_observed = threading.Event()
        renew_succeeded = threading.Event()
        cleanup_scheduled = threading.Event()
        acquire_calls = 0
        deadline_calls = 0
        renew_calls = 0

        def blocked_reacquire(hub_url, tok, body, *, timeout):
            nonlocal acquire_calls
            if body["action"] == "acquire":
                acquire_calls += 1
                if acquire_calls == 2:
                    reacquire_started.set()
                    assert allow_reacquire.wait(30)
                    result = real_post(hub_url, tok, body, timeout=timeout)
                    reacquire_finished.set()
                    return result
            return real_post(hub_url, tok, body, timeout=timeout)

        def controlled_deadline(fn, *, timeout):
            nonlocal deadline_calls
            deadline_calls += 1
            if deadline_calls == 2:
                worker = threading.Thread(target=fn, daemon=True)
                worker.start()
                assert reacquire_started.wait(30)
                timeout_observed.set()
                raise TimeoutError("controlled abandoned re-acquire")
            return real_deadline(fn, timeout=timeout)

        def controlled_renew(target, **kwargs):
            nonlocal renew_calls
            assert allow_missing.wait(30)
            renew_calls += 1
            if renew_calls == 1:
                return fleet_client.ClaimDecision(granted=False, reason="missing", holder=kwargs.get("holder"))
            if renew_calls > 2:
                return fleet_client.ClaimDecision(granted=False, reason="held", holder=kwargs.get("holder"))
            assert reacquire_finished.wait(30)
            result = real_renew(target, **kwargs)
            if result.granted:
                renew_succeeded.set()
            return result

        def tracking_schedule(*args, **kwargs):
            cleanup_scheduled.set()
            return real_schedule(*args, **kwargs)

        monkeypatch.setattr(fleet_client, "_post_claim_blocking", blocked_reacquire)
        monkeypatch.setattr(fleet_client, "_run_with_deadline", controlled_deadline)
        monkeypatch.setattr(fleet_client, "renew_claim", controlled_renew)
        monkeypatch.setattr(fleet_client, "_schedule_orphan_release", tracking_schedule)
        with fleet_client.repo_claim("repo-a", ttl_seconds=60):
            conn = fleet_hub.init_db(db)
            try:
                conn.execute("DELETE FROM claims")
                conn.commit()
            finally:
                conn.close()
            allow_missing.set()
            assert reacquire_started.wait(30)
            assert timeout_observed.wait(30), "heartbeat did not abandon the blocked re-acquire"
            assert not cleanup_scheduled.is_set(), "orphan cleanup was scheduled while the run was live"
            allow_reacquire.set()
            assert renew_succeeded.wait(30), "heartbeat never renewed the late re-acquire"
            assert not cleanup_scheduled.is_set(), "orphan cleanup was scheduled while the run was live"
            assert [c["target"] for c in fleet_client.fetch_claims()] == ["repo-a"]

        assert not cleanup_scheduled.is_set(), "definitive exit release scheduled extra cleanup"
        assert fleet_client.fetch_claims(include_all=True) == []

    def test_release_with_renew_in_flight_never_resurrects(self, hub, monkeypatch):
        """A heartbeat renew that lands after release must not re-acquire the
        row: the released target stays free instead of leaking for a TTL."""
        url, token, _db = hub
        self._env(monkeypatch, url, token)
        monkeypatch.setattr(fleet_client, "_claim_renew_interval", lambda ttl: 0.01)
        monkeypatch.setattr(fleet_client, "CLAIM_TIMEOUT_SECONDS", 0.2)  # bounds the exit join
        renew_started = threading.Event()
        allow_renew = threading.Event()

        def stuck_renew(target, **kwargs):
            renew_started.set()
            allow_renew.wait(5)
            # By the time this "lands", the main thread has already released:
            # the hub's answer for the deleted row is "missing".
            return fleet_client.ClaimDecision(granted=False, reason="missing", holder=kwargs.get("holder"))

        monkeypatch.setattr(fleet_client, "renew_claim", stuck_renew)
        with fleet_client.repo_claim("repo-a", ttl_seconds=60):
            assert renew_started.wait(5)
        # Exit drained what it could (bounded join), then released.
        assert fleet_client.fetch_claims(include_all=True) == []
        # Let the stale renew finish and tempt the re-acquire path.
        allow_renew.set()
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            assert fleet_client.fetch_claims(include_all=True) == [], "released claim was resurrected"
            time.sleep(0.02)

    def test_client_rejects_invalid_opaque_labels_without_posting(self, hub, monkeypatch):
        url, token, _db = hub
        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", url)
        monkeypatch.setenv("BRIGADE_FLEET_TOKEN", token)
        posted: list[dict[str, object]] = []
        real = fleet_client._post_claim_blocking

        def capture(hub_url, tok, body, *, timeout):
            posted.append(body)
            return real(hub_url, tok, body, timeout=timeout)

        monkeypatch.setattr(fleet_client, "_post_claim_blocking", capture)
        for kwargs in (
            {"harness": " "},
            {"role": "x" * 129},
            {"job": "job\x1b[31m"},
            {"session": "lease\x07"},
        ):
            decision = fleet_client.acquire_claim("repo-a", node_id=NODE_A, **kwargs)
            assert decision.granted is False
            assert decision.reason != "ok"
        assert posted == []
        assert fleet_client.acquire_claim("repo-a", node_id=NODE_A, harness="grokbot", session="lease-a").granted
        assert posted[-1]["harness"] == "grokbot"
        assert posted[-1]["session"] == "lease-a"

    def test_resolve_claim_target_uses_workspace_name(self, tmp_path, monkeypatch):
        from brigade import node as node_mod

        _bound_find_workspace_to_tmp(monkeypatch, tmp_path)
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
        assert payload["claims"][0]["generation"] == 1
        assert cli.main(["fleet", "claims"]) == 0
        out = capsys.readouterr().out
        assert "repo-a" in out and NODE_A[:12] in out and "cond-1" in out
        header = out.splitlines()[0].split()
        assert header[0] == "target" and "gen" in header
        assert out.splitlines()[1].split()[header.index("gen")] == "1"

    def test_claims_table_renders_legacy_nonprintables_inert(self, hub, monkeypatch, capsys):
        from brigade import cli

        url, token, db = hub
        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", url)
        monkeypatch.setenv("BRIGADE_FLEET_TOKEN", token)
        conn = sqlite3.connect(str(db))
        try:
            conn.execute(
                "INSERT INTO claims "
                "(target, owner_node, owner_conductor, holder_token, acquired_at, renewed_at, "
                "ttl_seconds, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "repo\x1b[31mred",
                    NODE_A,
                    "cond\x9b",
                    "h1",
                    "2026-08-23T12:00:00+00:00",
                    "2026-08-23T12:00:00+00:00",
                    900,
                    4_000_000_000.0,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        assert cli.main(["fleet", "claims", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["claims"][0]["target"] == "repo\x1b[31mred"
        assert payload["claims"][0]["owner_conductor"] == "cond\x9b"
        assert cli.main(["fleet", "claims"]) == 0
        out = capsys.readouterr().out
        assert "\x1b" not in out
        assert "\x9b" not in out
        assert "\\x1b[31mred" in out
        assert "\\x9b" in out

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

    def _session_env(self, hub, tmp_path, monkeypatch):
        url, token, _db = hub
        home = tmp_path / "home"
        monkeypatch.setenv("BRIGADE_HOME", str(home))
        _plant_home_identity(home, NODE_A)
        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", url)
        monkeypatch.setenv("BRIGADE_FLEET_TOKEN", token)
        return url, token

    def test_acquire_heartbeat_and_holder_release_fence_the_session(self, hub, tmp_path, monkeypatch, capsys):
        from brigade import cli

        self._session_env(hub, tmp_path, monkeypatch)
        assert (
            cli.main(
                [
                    "fleet",
                    "claims",
                    "--acquire",
                    "repo-a",
                    "--harness",
                    "t3",
                    "--session",
                    "sess-1",
                    "--role",
                    "coder",
                    "--job",
                    "job-1",
                    "--holder",
                    "holder-a",
                    "--json",
                ]
            )
            == 0
        )
        acquired = json.loads(capsys.readouterr().out)
        assert acquired["granted"] is True
        assert acquired["holder"] == "holder-a"
        assert {key: acquired["claim"][key] for key in ("harness", "role", "job", "session")} == {
            "harness": "t3",
            "role": "coder",
            "job": "job-1",
            "session": "sess-1",
        }
        assert "holder_token" not in acquired["claim"]
        assert cli.main(["fleet", "claims", "--heartbeat", "repo-a", "--holder", "holder-b"]) == 1
        assert "held" in capsys.readouterr().err or "holder" in capsys.readouterr().err
        assert cli.main(["fleet", "claims", "--heartbeat", "repo-a", "--holder", "holder-a", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["renewed"] is True
        assert cli.main(["fleet", "claims", "--release", "repo-a", "--holder", "holder-b"]) == 1
        assert _get(hub[0], "/claims", hub[1])[1]["claims"]
        assert cli.main(["fleet", "claims", "--release", "repo-a", "--holder", "holder-a", "--json"]) == 0
        released = json.loads(capsys.readouterr().out)
        assert released["released"] is True
        assert released.get("forced") is not True
        assert _get(hub[0], "/claims", hub[1])[1]["claims"] == []

    def test_holder_release_reports_terminal_outcome_without_queue_content(self, hub, tmp_path, monkeypatch, capsys):
        from brigade import cli

        url, token = self._session_env(hub, tmp_path, monkeypatch)
        events: list[dict[str, object]] = []
        monkeypatch.setattr(fleet_client, "report_external_event", lambda **kwargs: events.append(kwargs) or True)
        assert (
            cli.main(
                [
                    "fleet",
                    "claims",
                    "--acquire",
                    "repo-a",
                    "--harness",
                    "cursor-cloud",
                    "--session",
                    "sess-2",
                    "--holder",
                    "holder-c",
                ]
            )
            == 0
        )
        capsys.readouterr()
        assert (
            cli.main(
                [
                    "fleet",
                    "claims",
                    "--release",
                    "repo-a",
                    "--holder",
                    "holder-c",
                    "--outcome",
                    "external.completed",
                    "--harness",
                    "cursor-cloud",
                    "--session",
                    "sess-2",
                    "--json",
                ]
            )
            == 0
        )
        payload = json.loads(capsys.readouterr().out)
        assert payload["released"] is True
        assert events == [
            {
                "target": "repo-a",
                "harness": "cursor-cloud",
                "role": None,
                "job": None,
                "session": "sess-2",
                "state": "external.completed",
            }
        ]
        assert "prompt" not in json.dumps(events)
        assert "holder-c" not in json.dumps(events)
        listed = _get(url, "/claims", token)[1]["claims"]
        assert listed == []

    def test_wrong_holder_outcome_releases_nothing_and_publishes_nothing(self, hub, tmp_path, monkeypatch, capsys):
        from brigade import cli

        url, token = self._session_env(hub, tmp_path, monkeypatch)
        events: list[dict[str, object]] = []
        monkeypatch.setattr(fleet_client, "report_external_event", lambda **kwargs: events.append(kwargs) or True)
        assert (
            cli.main(
                [
                    "fleet",
                    "claims",
                    "--acquire",
                    "repo-a",
                    "--harness",
                    "t3",
                    "--session",
                    "sess-3",
                    "--holder",
                    "holder-c",
                ]
            )
            == 0
        )
        capsys.readouterr()
        assert (
            cli.main(
                [
                    "fleet",
                    "claims",
                    "--release",
                    "repo-a",
                    "--holder",
                    "holder-wrong",
                    "--outcome",
                    "external.failed",
                    "--harness",
                    "t3",
                    "--session",
                    "sess-3",
                    "--json",
                ]
            )
            == 1
        )
        payload = json.loads(capsys.readouterr().out)
        assert payload["released"] is False
        assert payload.get("outcome") in (None, "external.failed")
        assert events == []
        listed = _get(url, "/claims", token)[1]["claims"]
        assert [claim["session"] for claim in listed] == ["sess-3"]

    def test_holder_release_reports_outcome_only_after_granted_release(self, hub, tmp_path, monkeypatch, capsys):
        from brigade import cli

        self._session_env(hub, tmp_path, monkeypatch)
        order: list[str] = []
        real_release = fleet_client.release_claim

        def release(target, **kwargs):
            order.append("release")
            return real_release(target, **kwargs)

        monkeypatch.setattr(fleet_client, "release_claim", release)
        monkeypatch.setattr(
            fleet_client,
            "report_external_event",
            lambda **kwargs: order.append("event") or True,
        )
        assert (
            cli.main(
                [
                    "fleet",
                    "claims",
                    "--acquire",
                    "repo-a",
                    "--harness",
                    "cursor-cloud",
                    "--session",
                    "sess-4",
                    "--holder",
                    "holder-d",
                ]
            )
            == 0
        )
        capsys.readouterr()
        assert (
            cli.main(
                [
                    "fleet",
                    "claims",
                    "--release",
                    "repo-a",
                    "--holder",
                    "holder-d",
                    "--outcome",
                    "external.completed",
                    "--harness",
                    "cursor-cloud",
                    "--session",
                    "sess-4",
                    "--json",
                ]
            )
            == 0
        )
        assert order == ["release", "event"]

    def test_holder_release_rejects_invalid_opaque_labels_without_releasing(self, hub, tmp_path, monkeypatch, capsys):
        from brigade import cli

        url, token = self._session_env(hub, tmp_path, monkeypatch)
        events: list[dict[str, object]] = []
        monkeypatch.setattr(fleet_client, "report_external_event", lambda **kwargs: events.append(kwargs) or True)
        assert (
            cli.main(
                [
                    "fleet",
                    "claims",
                    "--acquire",
                    "repo-a",
                    "--harness",
                    "cursor-cloud",
                    "--session",
                    "sess-5",
                    "--holder",
                    "holder-e",
                ]
            )
            == 0
        )
        capsys.readouterr()
        invalid = (
            ("--harness", "x" * 129),
            ("--session", "sess\x07"),
            ("--role", "x" * 129),
            ("--job", "job\x1b[31m"),
        )
        for flag, value in invalid:
            argv = [
                "fleet",
                "claims",
                "--release",
                "repo-a",
                "--holder",
                "holder-e",
                "--outcome",
                "external.completed",
                "--harness",
                "cursor-cloud",
                "--session",
                "sess-5",
                flag,
                value,
            ]
            assert cli.main(argv) == 2
            err = capsys.readouterr().err
            assert "control" in err or "at most" in err or "non-empty" in err
        assert events == []
        listed = _get(url, "/claims", token)[1]["claims"]
        assert [claim["session"] for claim in listed] == ["sess-5"]

    def test_holder_release_uses_normalized_opaque_labels(self, hub, tmp_path, monkeypatch, capsys):
        from brigade import cli

        url, token = self._session_env(hub, tmp_path, monkeypatch)
        events: list[dict[str, object]] = []
        monkeypatch.setattr(fleet_client, "report_external_event", lambda **kwargs: events.append(kwargs) or True)
        assert (
            cli.main(
                [
                    "fleet",
                    "claims",
                    "--acquire",
                    "repo-a",
                    "--harness",
                    "cursor-cloud",
                    "--session",
                    "sess-6",
                    "--role",
                    "worker",
                    "--job",
                    "job-6",
                    "--holder",
                    "holder-f",
                ]
            )
            == 0
        )
        capsys.readouterr()
        assert (
            cli.main(
                [
                    "fleet",
                    "claims",
                    "--release",
                    "repo-a",
                    "--holder",
                    "holder-f",
                    "--outcome",
                    "external.completed",
                    "--harness",
                    "  cursor-cloud  ",
                    "--session",
                    "  sess-6  ",
                    "--role",
                    "  worker  ",
                    "--job",
                    "  job-6  ",
                    "--json",
                ]
            )
            == 0
        )
        payload = json.loads(capsys.readouterr().out)
        assert payload["released"] is True
        assert payload["outcome"] == "external.completed"
        assert events == [
            {
                "target": "repo-a",
                "harness": "cursor-cloud",
                "role": "worker",
                "job": "job-6",
                "session": "sess-6",
                "state": "external.completed",
            }
        ]
        assert _get(url, "/claims", token)[1]["claims"] == []

    def test_claims_table_renders_external_harness_labels(self, hub, monkeypatch, capsys):
        from brigade import cli

        url, token, _db = hub
        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", url)
        monkeypatch.setenv("BRIGADE_FLEET_TOKEN", token)
        _post(url, token, _claim(harness="grokbot", role="implementation-worker", job="job-9", session="lease-z"))
        assert cli.main(["fleet", "claims"]) == 0
        out = capsys.readouterr().out
        header = out.splitlines()[0]
        assert "harness" in header and "session" in header
        assert "grokbot" in out and "lease-z" in out
        assert "holder_token" not in out


class TestDispatchWiring:
    """The run dispatch path asks the hub before taking the local lease."""

    @pytest.fixture()
    def workspace(self, tmp_path, monkeypatch):
        from brigade import node as node_mod

        monkeypatch.setenv("BRIGADE_HOME", str(tmp_path / "home"))
        _plant_home_identity(tmp_path / "home", NODE_A)
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
        from brigade import aboyeur

        url, token, _db = hub
        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", url)
        monkeypatch.setenv("BRIGADE_FLEET_TOKEN", token)
        ws, roster_path = workspace
        held_during_run = []

        def fake_run(*a, **kw):
            held_during_run.extend(fleet_client.fetch_claims())
            return 0

        monkeypatch.setattr(aboyeur, "run", fake_run)
        assert self._run(ws, roster_path) == 0
        assert [(c["target"], c["owner_node"]) for c in held_during_run] == [("ws", NODE_A)]
        # The dispatch path names its conductor (the orchestrator seat).
        assert held_during_run[0]["owner_conductor"] == "chef"
        assert fleet_client.fetch_claims() == []

    def test_claim_taken_after_local_lock(self, hub, workspace, monkeypatch):
        """The hub claim is only ever held by the run that owns run.lock, so a
        queued sibling can neither run unclaimed nor release the winner's claim."""
        from contextlib import contextmanager

        from brigade import aboyeur, runguard

        url, token, _db = hub
        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", url)
        monkeypatch.setenv("BRIGADE_FLEET_TOKEN", token)
        ws, roster_path = workspace
        order = []
        real_lock = runguard.run_lock
        real_claim = fleet_client.repo_claim

        @contextmanager
        def tracking_lock(*a, **kw):
            with real_lock(*a, **kw) as path:
                order.append("lock")
                yield path

        @contextmanager
        def tracking_claim(*a, **kw):
            with real_claim(*a, **kw) as decision:
                order.append("claim")
                yield decision

        monkeypatch.setattr(runguard, "run_lock", tracking_lock)
        monkeypatch.setattr(fleet_client, "repo_claim", tracking_claim)
        monkeypatch.setattr(aboyeur, "run", lambda *a, **kw: 0)
        assert self._run(ws, roster_path) == 0
        assert order == ["lock", "claim"]

    def test_hub_down_falls_back_to_local_lock(self, workspace, monkeypatch):
        from brigade import aboyeur

        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", "http://127.0.0.1:9")
        monkeypatch.setenv("BRIGADE_FLEET_TOKEN", "irrelevant")
        ws, roster_path = workspace
        dispatched = []
        monkeypatch.setattr(aboyeur, "run", lambda *a, **kw: dispatched.append(1) or 0)
        assert self._run(ws, roster_path) == 0
        assert dispatched == [1]
