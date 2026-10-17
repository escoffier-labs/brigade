"""Review regressions for ingestion complexity and definite claim refusals."""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from unittest.mock import Mock

import pytest

from brigade import fleet_client, fleet_hub, fleet_hub_event_bounds as bounds
from brigade import worklore_http
from tests.test_fleet_hub_request_bounds import _claim_snapshot, hub as hub
from tests.test_fleet_claims import NODE_A, _claim, _plant_home_identity

_MAX_BODY_SCAN_CPU_SECONDS = 5.0


def test_complexity_attack_is_refused_before_decoding(monkeypatch):
    inner = b"[" + b",".join([b"[{},{}]"] * 1000) + b"]"
    raw = b"[" + b",".join([inner] * 1000) + b"]"
    assert len(raw) == 8_002_001
    decoder = Mock(side_effect=AssertionError("unsafe document reached decoder"))
    monkeypatch.setattr(json, "loads", decoder)
    with pytest.raises(bounds.RequestBoundsError, match="JSON.*(containers|items)"):
        bounds.bounded_json_loads(raw)
    decoder.assert_not_called()


def _container_document(total):
    groups = (total - 1 + 1000) // 1001
    remaining = total - 1 - groups
    rows = []
    for _ in range(groups):
        size = min(1000, remaining)
        rows.append([{}] * size)
        remaining -= size
    return json.dumps(rows, separators=(",", ":")).encode()


def _item_document(total):
    groups = (total + 1000) // 1001
    remaining = total - groups
    rows = []
    for _ in range(groups):
        size = min(1000, remaining)
        rows.append([0] * size)
        remaining -= size
    return json.dumps(rows, separators=(",", ":")).encode()


@pytest.mark.parametrize(
    "kind,budget,document", [("containers", 32768, _container_document), ("items", 65536, _item_document)]
)
def test_aggregate_complexity_boundary(monkeypatch, kind, budget, document):
    raw = document(budget)
    assert bounds.bounded_json_loads(raw) == json.loads(raw)
    decoder = Mock(side_effect=AssertionError("over-budget document reached decoder"))
    monkeypatch.setattr(json, "loads", decoder)
    with pytest.raises(bounds.RequestBoundsError, match=f"JSON.*{kind}"):
        bounds.bounded_json_loads(document(budget + 1))
    decoder.assert_not_called()


def test_complexity_scan_counts_duplicate_keys_and_ignores_quoted_punctuation(monkeypatch):
    monkeypatch.setattr(bounds, "MAX_JSON_ITEMS", 2)
    raw = b'{"x":"\\"[{},{}]","x":"\\"[{},{}]"}'
    assert bounds.bounded_json_loads(raw) == {"x": '"[{},{}]'}
    decoder = Mock(side_effect=AssertionError("duplicate keys bypassed item budget"))
    monkeypatch.setattr(json, "loads", decoder)
    with pytest.raises(bounds.RequestBoundsError, match="JSON aggregate items"):
        bounds.bounded_json_loads(b'{"x":0,"x":0,"x":0}')
    decoder.assert_not_called()


def test_full_body_scan_cost_is_recorded(record_property):
    # A valid, string-heavy 8 MiB body must traverse the entire byte scan.
    raw = b'"' + b"x" * (fleet_hub.MAX_BODY_BYTES - 2) + b'"'
    samples = []
    for _ in range(5):
        started = time.process_time()
        bounds._scan_json(raw)
        samples.append(time.process_time() - started)
    record_property("body_scan_cpu_seconds", samples)
    print(f"8 MiB pre-decode scan CPU seconds: {samples}")
    # CPU time avoids contention-based wall-clock failures. The measured
    # baseline is about 0.13 seconds, leaving ample room on slower runners.
    assert max(samples) < _MAX_BODY_SCAN_CPU_SECONDS


@pytest.mark.parametrize(
    "path,limit", [("/work/items", bounds.WORKLORE_BODY_BYTES), ("/work/imports", bounds.WORKLORE_IMPORT_BODY_BYTES)]
)
def test_worklore_body_byte_boundary(hub, monkeypatch, path, limit):
    url, token, _db = hub
    handled = []
    monkeypatch.setattr(worklore_http, "enabled", lambda: True)

    def handle(conn, request):
        handled.append(request.body)
        return 200, {"accepted": True}

    monkeypatch.setattr(worklore_http, "handle", handle)
    for extra in (0, 1):
        request = urllib.request.Request(
            url + path,
            data=b"{}".ljust(limit + extra),
            headers={"Authorization": f"Bearer {token}"},
            method="POST",
        )
        if extra:
            parsed = urlsplit(url)
            conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
            try:
                conn.putrequest("POST", path)
                conn.putheader("Authorization", f"Bearer {token}")
                conn.putheader("Content-Length", str(limit + 1))
                conn.endheaders()
                response = conn.getresponse()
                assert response.status == 413
                assert "body" in json.loads(response.read())["error"]
                assert response.will_close
            finally:
                conn.close()
        else:
            with urllib.request.urlopen(request, timeout=5) as response:
                assert response.status == 200
    assert handled == [{}]


@pytest.mark.parametrize("framing", ["chunked", "missing-length", "duplicate-length"])
def test_ambiguous_framing_is_refused_without_reading_body(hub, framing):
    url, token, db = hub
    parsed = urlsplit(url)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
    try:
        conn.putrequest("POST", "/events")
        conn.putheader("Authorization", f"Bearer {token}")
        if framing == "chunked":
            conn.putheader("Transfer-Encoding", "chunked")
        elif framing == "duplicate-length":
            conn.putheader("Content-Length", "10")
            conn.putheader("Content-Length", "10")
        conn.endheaders()
        # No body is sent. A handler that reads it blocks instead of replying.
        response = conn.getresponse()
        assert response.status == 400
        assert "Content-Length" in json.loads(response.read())["error"]
        assert response.will_close
    finally:
        conn.close()
    with fleet_hub.open_db(db) as db_conn:
        assert db_conn.execute("SELECT count(*) FROM events").fetchone()[0] == 0


def test_current_database_repairs_claim_owner_index_and_query_uses_it(tmp_path):
    from brigade.fleet_hub_claim_cardinality import check_new_claim

    path = tmp_path / "hub.db"
    with fleet_hub.init_db(path) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.execute("DROP INDEX IF EXISTS claims_owner_expiry")
    conn.close()
    with fleet_hub.init_db(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == version
        assert [r[2] for r in conn.execute("PRAGMA index_info(claims_owner_expiry)")] == ["owner_node", "expires_at"]
        statements = []
        conn.set_trace_callback(statements.append)
        check_new_claim(conn, "fake-node", 0)
        conn.set_trace_callback(None)
        query = next(sql for sql in statements if sql.startswith("SELECT count(*)"))
        plan = conn.execute("EXPLAIN QUERY PLAN " + query).fetchall()
        assert any("claims_owner_expiry" in row[3] for row in plan)
        conn.execute("BEGIN IMMEDIATE")
        # Once repaired, another startup must keep the existing read-only fast path.
        reopened = fleet_hub.init_db(path)
        reopened.close()
        conn.rollback()
    conn.close()


@pytest.fixture()
def claim_client(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("BRIGADE_HOME", str(home))
    _plant_home_identity(home, NODE_A)
    monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", "http://hub.invalid")
    monkeypatch.setenv("BRIGADE_FLEET_TOKEN", "fake-token")


def _quota_payload():
    return {"granted": False, "reason": "claim_cardinality_exceeded", "error": "active claim quota exceeded"}


def test_http_quota_is_a_definite_client_refusal(claim_client, monkeypatch):
    import io

    def refuse(request, **kwargs):
        raise urllib.error.HTTPError(
            request.full_url, 409, "quota", {}, io.BytesIO(json.dumps(_quota_payload()).encode())
        )

    monkeypatch.setattr(fleet_client, "_hub_open", refuse)
    decision = fleet_client.acquire_claim("fake-repo")
    assert not decision.granted
    assert decision.reason == "quota"
    assert decision.detail == "active claim quota exceeded"


def test_initial_quota_never_yields_or_retries(claim_client, monkeypatch):
    post = Mock(return_value=(409, _quota_payload()))
    orphan = Mock()
    monkeypatch.setattr(fleet_client, "_post_claim_blocking", post)
    monkeypatch.setattr(fleet_client, "_schedule_orphan_release", orphan)
    with pytest.raises(fleet_client.FleetClaimHeldError, match="quota"):
        with fleet_client.repo_claim("fake-repo"):
            pytest.fail("quota refusal admitted unprotected work")
    assert post.call_count == 1
    orphan.assert_not_called()


def test_heartbeat_quota_terminates_the_lease(claim_client, monkeypatch):
    import threading

    calls = []
    lost = []
    canceled = threading.Event()
    monkeypatch.setattr(fleet_client, "_claim_renew_interval", lambda ttl: 0.01)

    def post(hub, token, body, **kwargs):
        action = body["action"]
        calls.append(action)
        if action == "acquire":
            return (200, {"granted": True}) if calls.count(action) == 1 else (409, _quota_payload())
        if action == "renew":
            return 409, {"error": "missing"}
        return 200, {"released": True}

    def on_lost(reason):
        lost.append(reason)
        canceled.set()

    monkeypatch.setattr(fleet_client, "_post_claim_blocking", post)

    def guarded():
        # A non-main owner uses the cooperative cancellation channel.
        with fleet_client.repo_claim("fake-repo", ttl_seconds=60, on_claim_lost=on_lost) as decision:
            assert canceled.wait(5), "quota did not cancel the guarded run"
            assert decision.cancel_event.is_set()

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(guarded).result(timeout=10)
    assert lost == ["quota"]
    assert calls == ["acquire", "renew", "acquire", "release"]


@pytest.mark.parametrize("legacy", [True, False], ids=["base-client", "new-client"])
def test_live_quota_refusal_never_falls_back_to_local_work(hub, claim_client, monkeypatch, legacy):
    from brigade import fleet_hub_claim_cardinality as cardinality

    url, token, db = hub
    monkeypatch.setattr(cardinality, "MAX_ACTIVE_CLAIMS_PER_NODE", 1)
    monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", url)
    monkeypatch.setenv("BRIGADE_FLEET_TOKEN", token)
    with fleet_hub.open_db(db) as conn:
        assert fleet_hub.handle_claim(conn, _claim(node=NODE_A, target="occupied"))[0] == 200
        before = _claim_snapshot(conn)
    orphan = Mock()
    monkeypatch.setattr(fleet_client, "_schedule_orphan_release", orphan)
    if legacy:

        def base_acquire(target, **kwargs):
            # Base client: 409 is held only with a dict owner. Without it,
            # repo_claim retries a missing answer then admits local-only work.
            status, payload = fleet_client._post_claim_blocking(
                url, token, _claim(node=NODE_A, target=target), timeout=5
            )
            assert status == 409
            owner = payload.get("owner") if isinstance(payload.get("owner"), dict) else None
            return fleet_client.ClaimDecision(
                granted=False, reason="held" if owner is not None else "missing", owner=owner
            )

        monkeypatch.setattr(fleet_client, "acquire_claim", base_acquire)
    with pytest.raises(fleet_client.FleetClaimHeldError):
        with fleet_client.repo_claim("overflow"):
            pytest.fail("hub quota refusal admitted local-only work")
    orphan.assert_not_called()
    with fleet_hub.open_db(db) as conn:
        assert _claim_snapshot(conn) == before
