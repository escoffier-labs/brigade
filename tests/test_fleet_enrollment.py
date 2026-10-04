"""Dashboard capability enrollment at the HTTP and SQLite boundaries."""

import http.client
import threading

import pytest

from brigade import fleet_hub

import hashlib
import hmac
import json
import socket
import sqlite3
import ssl
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urlencode

from brigade import cli, fleet_client, fleet_client_enrollment as client, fleet_hub_enrollment as store
from brigade import fleet_hub_roster_page, fleet_hub_sessions, fleet_policy_page


ADMIN = "fixture-dashboard-admin"


@pytest.fixture()
def hub(tmp_path):
    db = tmp_path / "hub.db"
    server = fleet_hub.make_server("127.0.0.1", 0, db, ADMIN)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, db
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def request(hub, method, path, *, headers=None, body=None):
    conn = http.client.HTTPConnection(*hub[0].server_address, timeout=5)
    conn.request(method, path, body=body, headers=headers or {})
    response = conn.getresponse()
    result = response.status, dict(response.getheaders()), response.read().decode()
    conn.close()
    return result


@pytest.fixture(params=["native", "python310-empty"])
def query_parser(request, monkeypatch):
    if request.param == "python310-empty":
        native_parse_qsl = store.parse_qsl

        def parse_qsl_310_empty(raw, *args, **kwargs):
            # CPython 3.10 treats empty input as a malformed field in strict mode.
            # Delegate every nonempty input to the runtime's real parser.
            if raw == "" and kwargs.get("strict_parsing"):
                raise ValueError("bad query field: ''")
            return native_parse_qsl(raw, *args, **kwargs)

        monkeypatch.setattr(store, "parse_qsl", parse_qsl_310_empty)


def test_empty_params_accepted_but_missing_code_refused(query_parser):
    assert store.strict_params("") == {}
    with pytest.raises(store.EnrollmentError, match="^dashboard request refused$"):
        store.code_param("")
    assert store.strict_params("one=&two=2&three=3&four=4") == {"one": "", "two": "2", "three": "3", "four": "4"}


@pytest.mark.parametrize(
    "raw",
    [
        "missing-equals",
        "one=1&",
        "&",
        "one=1&one=2",
        "one=1&two=2&three=3&four=4&five=5",
        "one=%",
        "one=%ZZ",
        "one=%FF",
        "one=%C3%28",
        "one=" + "x" * store.BODY_LIMIT,
    ],
)
def test_nonempty_params_remain_strict(query_parser, raw):
    with pytest.raises(store.EnrollmentError, match="^dashboard request refused$"):
        store.strict_params(raw)


@pytest.mark.parametrize(
    "path", ["/", "/deck", "/deck/repos", "/view/machines", "/view/repos", "/deck/roster", "/deck/policy"]
)
def test_bearer_query_enrollment_refused_even_with_admin(hub, path):
    status, headers, text = request(hub, "GET", f"{path}?token={ADMIN}", headers={"Authorization": f"Bearer {ADMIN}"})
    assert status == 401
    assert "Set-Cookie" not in headers and "Location" not in headers
    assert ADMIN not in text


def admin_headers():
    return {"Authorization": f"Bearer {ADMIN}", "Content-Type": "application/json"}


def mint(hub, label=None):
    status, headers, text = request(
        hub,
        "POST",
        "/dashboard/enrollment",
        headers=admin_headers(),
        body=json.dumps({} if label is None else {"label": label}),
    )
    assert status == 201, text
    assert headers["Cache-Control"] == "no-store" and headers["Referrer-Policy"] == "no-referrer"
    result = json.loads(text)
    assert set(result) == {"code", "expires_at"}
    return result["code"]


def redeem(hub, code, **extra):
    return request(
        hub,
        "POST",
        "/enroll",
        headers={"Content-Type": "application/x-www-form-urlencoded", "Sec-Fetch-Site": "same-origin", **extra},
        body=urlencode({"code": code}),
    )


def cookie(hub):
    code = mint(hub, "fixture browser")
    status, headers, text = request(hub, "GET", f"/enroll?code={code}")
    assert status == 200 and f'value="{code}"' in text
    assert headers["Cache-Control"] == "no-store" and headers["Referrer-Policy"] == "no-referrer"
    assert ADMIN not in text and "<script" not in text
    # Confirmation GET can be repeated without consuming the capability.
    assert request(hub, "GET", f"/enroll?code={code}")[0] == 200
    status, headers, text = redeem(hub, code)
    assert status == 303 and headers["Location"] == "/deck"
    assert code not in text + headers["Location"] and ADMIN not in str(headers)
    value = headers["Set-Cookie"]
    assert all(part in value for part in ("Path=/", "HttpOnly", "SameSite=Strict", "Max-Age=2592000"))
    assert "Domain=" not in value and "Secure" not in value
    assert headers["Cache-Control"] == "no-store" and headers["Referrer-Policy"] == "no-referrer"
    assert redeem(hub, code)[0] == 400
    return value.split(";")[0]


def test_read_only_cookie_cannot_mutate_with_correct_csrf_and_origin_or_access_json(hub):
    credential = cookie(hub)
    for path, csrf in (
        ("/deck/roster", fleet_hub_roster_page.csrf_value(ADMIN)),
        ("/deck/policy", fleet_policy_page.csrf_value(ADMIN)),
    ):
        status, _, text = request(hub, "GET", path, headers={"Cookie": credential})
        assert status == 200 and "read-only" in text and csrf not in text
        assert '<button type="submit"' not in text
        status, _, _ = request(
            hub,
            "POST",
            path,
            headers={
                "Cookie": credential,
                "Content-Type": "application/x-www-form-urlencoded",
                "Sec-Fetch-Site": "same-origin",
                "Origin": f"http://127.0.0.1:{hub[0].server_address[1]}",
            },
            body=urlencode({"csrf": csrf, "action": "preview", "scope": "defaults", "expected_version": "1"}),
        )
        assert status == 403
        status, _, editor = request(hub, "GET", path, headers=admin_headers())
        assert status == 200 and csrf in editor
    for path in (
        "/status",
        "/claims",
        "/nodes",
        "/cloud",
        "/models",
        "/preference",
        "/sessions",
        "/policy",
        "/policy/status",
        "/policy/inventory",
        "/dashboard/sessions",
    ):
        assert request(hub, "GET", path, headers={"Cookie": credential})[0] == 401
    for path in (
        "/events",
        "/claims",
        "/nodes",
        "/cloud",
        "/models",
        "/sessions",
        "/policy",
        "/dashboard/enrollment",
        "/dashboard/sessions",
    ):
        assert (
            request(hub, "POST", path, headers={"Cookie": credential, "Content-Type": "application/json"}, body="{}")[0]
            == 401
        )


def test_admin_only_control_and_revocation(hub, query_parser):
    conn = fleet_hub.open_db(hub[1])
    _, node = fleet_hub.add_node(conn, "11111111-1111-4111-8111-111111111111", "fixture node")
    conn.close()
    for method, path in (
        ("POST", "/dashboard/enrollment"),
        ("GET", "/dashboard/sessions"),
        ("POST", "/dashboard/sessions"),
    ):
        assert (
            request(
                hub,
                method,
                path,
                headers={"Authorization": f"Bearer {node}", "Content-Type": "application/json"},
                body="{}" if method == "POST" else None,
            )[0]
            == 403
        )
        assert (
            request(
                hub,
                method,
                path,
                headers={"Tailscale-User-Login": "fixture@example.test"},
                body="{}" if method == "POST" else None,
            )[0]
            == 401
        )
    credential = cookie(hub)
    status, _, text = request(hub, "GET", "/dashboard/sessions", headers=admin_headers())
    result = json.loads(text)
    assert status == 200 and result["next_after"] is None
    row = result["sessions"][0]
    assert set(row) == {"session_id", "label", "scope", "created_at", "expires_at", "revoked_at"}
    assert row["label"] == "fixture browser" and row["scope"] == "read-only"
    assert ADMIN not in text and credential.split("=")[1] not in text
    body = json.dumps({"action": "revoke", "session_id": row["session_id"]})
    assert request(hub, "POST", "/dashboard/sessions", headers=admin_headers(), body=body)[0] == 200
    first = json.loads(request(hub, "GET", "/dashboard/sessions?all=1", headers=admin_headers())[2])["sessions"][0][
        "revoked_at"
    ]
    assert request(hub, "POST", "/dashboard/sessions", headers=admin_headers(), body=body)[0] == 200
    assert (
        json.loads(request(hub, "GET", "/dashboard/sessions?all=1", headers=admin_headers())[2])["sessions"][0][
            "revoked_at"
        ]
        == first
    )
    assert json.loads(request(hub, "GET", "/dashboard/sessions", headers=admin_headers())[2])["sessions"] == []
    assert request(hub, "GET", "/deck", headers={"Cookie": credential})[0] == 401
    assert (
        request(
            hub,
            "POST",
            "/dashboard/sessions",
            headers=admin_headers(),
            body=json.dumps({"action": "revoke", "session_id": "ds_" + "0" * 32}),
        )[0]
        == 404
    )


@pytest.mark.parametrize(
    "query",
    [
        "all=0",
        "all=1&all=1",
        "after=bad",
        "after=",
        "after=ds_" + "0" * 32 + "&after=ds_" + "0" * 32,
        "other=1",
        "all=1&other=1",
        "all=%ZZ",
    ],
)
def test_session_list_strict_query(hub, query):
    assert request(hub, "GET", "/dashboard/sessions?" + query, headers=admin_headers())[0] == 400


@pytest.mark.parametrize(
    "path,body",
    [
        ("/dashboard/enrollment", '{"label":"one","label":"two"}'),
        ("/dashboard/enrollment", '{"unknown":1}'),
        ("/dashboard/enrollment", '{"label":1}'),
        ("/dashboard/enrollment", json.dumps({"label": "x" * 65})),
        ("/dashboard/enrollment", '{"label":"bad\\nlabel"}'),
        ("/dashboard/enrollment", "[]"),
        ("/dashboard/enrollment", "{"),
        ("/dashboard/sessions", '{"action":"unknown","session_id":"ds_' + "0" * 32 + '"}'),
        ("/dashboard/sessions", '{"action":"revoke","session_id":"bad"}'),
        ("/dashboard/sessions", '{"action":"revoke","session_id":"ds_' + "0" * 32 + '","extra":1}'),
    ],
)
def test_mutation_rejects_malformed_and_ambiguous_json(hub, path, body):
    status, _, text = request(hub, "POST", path, headers=admin_headers(), body=body)
    assert status == 400 and text == '{"error": "dashboard request refused"}'


@pytest.mark.parametrize("path", ["/dashboard/enrollment", "/dashboard/sessions"])
def test_mutation_deep_json_returns_bounded_generic_error(hub, path):
    status, headers, text = request(hub, "POST", path, headers=admin_headers(), body="[" * 16000)
    assert status == 400
    assert text == '{"error": "dashboard request refused"}'
    assert int(headers["Content-Length"]) == len(text.encode()) < 100
    assert "Set-Cookie" not in headers and "Location" not in headers
    assert json.loads(request(hub, "GET", "/dashboard/sessions", headers=admin_headers())[2])["sessions"] == []
    conn = fleet_hub.open_db(hub[1])
    assert conn.execute("SELECT COUNT(*) FROM dashboard_enrollment_codes").fetchone()[0] == 0
    conn.close()


def test_mutation_body_bounds_and_auth_before_body(hub):
    assert (
        request(
            hub, "POST", "/dashboard/enrollment", headers={**admin_headers(), "Content-Type": "text/plain"}, body="{}"
        )[0]
        == 415
    )
    assert request(hub, "POST", "/dashboard/enrollment", headers=admin_headers(), body="x" * (16384 + 1))[0] == 413
    # Deliberately omit declared bytes. A correct server rejects before reading them.
    assert (
        request(
            hub, "POST", "/dashboard/enrollment", headers={"Content-Length": "100", "Content-Type": "application/json"}
        )[0]
        == 401
    )


@pytest.mark.parametrize(
    "suffix",
    ["", "code=", "code=bad", "code={code}&code={code}", "code={code}&other=1", "code=%ZZ", "code=" + "x" * 44],
)
def test_enrollment_code_parse(hub, suffix, query_parser):
    code = mint(hub)
    suffix = suffix.format(code=code)
    assert request(hub, "GET", "/enroll?" + suffix)[0] == 400
    assert (
        request(
            hub,
            "POST",
            "/enroll",
            headers={"Content-Type": "application/x-www-form-urlencoded", "Sec-Fetch-Site": "same-origin"},
            body=suffix or "other=1",
        )[0]
        == 400
    )
    assert redeem(hub, code)[0] == 303


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Origin": "null"},
        {"Sec-Fetch-Site": "same-site"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "none"},
        {"Referer": "http://example.test/enroll"},
        {"Origin": "http://other.test", "Sec-Fetch-Site": "same-origin"},
        {"Origin": "http://example.test", "Sec-Fetch-Site": "cross-site"},
        {"Origin": "http://example.test/"},
        {"Origin": "http://example.test/path"},
        {"Origin": "http://example.test?x=1"},
        {"Origin": "http://user@example.test"},
        {"Origin": "http://example.test#fragment"},
        {"Origin": "http://example.test:"},
    ],
)
def test_enrollment_origin_refusals_do_not_consume(hub, headers):
    code = mint(hub)
    status, _, _ = request(
        hub,
        "POST",
        "/enroll",
        headers={"Host": "example.test", "Content-Type": "application/x-www-form-urlencoded", **headers},
        body=f"code={code}",
    )
    assert status == 403
    assert redeem(hub, code)[0] == 303


@pytest.mark.parametrize(
    "host,origin",
    [
        ("EXAMPLE.TEST", "http://example.test:80"),
        ("example.test:80", "http://EXAMPLE.TEST"),
        ("example.test:1234", "http://EXAMPLE.TEST:1234"),
        ("[::1]:80", "http://[::1]"),
    ],
)
def test_enrollment_canonical_default_port_and_host_case(hub, host, origin):
    code = mint(hub)
    status, _, _ = request(
        hub,
        "POST",
        "/enroll",
        headers={"Host": host, "Origin": origin, "Content-Type": "application/x-www-form-urlencoded"},
        body=f"code={code}",
    )
    assert status == 303


def raw_request(hub, method, path, headers, body=None):
    conn = http.client.HTTPConnection(*hub[0].server_address, timeout=5)
    conn.putrequest(method, path, skip_host=True)
    for key, value in headers:
        conn.putheader(key, value)
    if body is not None:
        conn.putheader("Content-Length", str(len(body)))
    conn.endheaders(body.encode() if body is not None else None)
    response = conn.getresponse()
    result = response.status, dict(response.getheaders()), response.read().decode()
    conn.close()
    return result


@pytest.mark.parametrize(
    "ambiguous",
    [
        [("Host", "example.test"), ("Host", "example.test")],
        [("Host", "example.test"), ("Origin", "http://example.test"), ("Origin", "http://example.test")],
        [("Host", "example.test"), ("Sec-Fetch-Site", "same-origin"), ("Sec-Fetch-Site", "same-origin")],
        [],
        [("Host", "bad host")],
        [("Host", "user@example.test")],
    ],
)
def test_duplicate_or_invalid_origin_headers_fail_closed(hub, ambiguous):
    code = mint(hub)
    assert (
        raw_request(
            hub,
            "POST",
            "/enroll",
            [*ambiguous, ("Content-Type", "application/x-www-form-urlencoded"), ("Sec-Fetch-Site", "same-origin")],
            f"code={code}",
        )[0]
        == 403
    )
    assert redeem(hub, code)[0] == 303


def test_prefetch_refused_without_consuming(hub):
    code = mint(hub)
    for header in ("Purpose", "Sec-Purpose"):
        assert request(hub, "GET", f"/enroll?code={code}", headers={header: "prefetch"})[0] == 400
        assert redeem(hub, code, **{header: "prefetch"})[0] == 403
    assert redeem(hub, code)[0] == 303


def test_duplicate_cookie_and_legacy_cookie_refused(hub):
    credential = cookie(hub)
    host = f"127.0.0.1:{hub[0].server_address[1]}"
    assert request(hub, "GET", "/deck", headers={"Cookie": "unrelated=valid; " + credential})[0] == 200
    legacy = hmac.new(ADMIN.encode(), b"brigade-fleet-dashboard-cookie-v1", hashlib.sha256).hexdigest()
    for header in (
        credential + "; " + credential,
        credential + "; brigade_fleet_view=wrong",
        "brigade_fleet_view=" + legacy,
        "brigade_fleet_view=" + "x" * 44,
        'brigade_fleet_view="' + credential.split("=")[1] + '"',
    ):
        assert request(hub, "GET", "/deck", headers={"Cookie": header})[0] == 401
    assert (
        raw_request(hub, "GET", "/deck", [("Host", host), ("Cookie", credential), ("Cookie", "other=valid")])[0] == 401
    )


def test_storage_lifecycle_fixed_expiry_digests_independence_and_rollback(tmp_path, monkeypatch):
    conn = fleet_hub.init_db(tmp_path / "hub.db")
    clock = [1000.0]
    monkeypatch.setattr(store.time, "time", lambda: clock[0])
    first = store.mint(conn, "  first device  ")
    raw = conn.execute("SELECT * FROM dashboard_enrollment_codes").fetchone()
    assert raw == (hashlib.sha256(first["code"].encode()).hexdigest(), "first device", 1000, 1300)
    clock[0] = 1300
    assert not store.available(conn, first["code"])
    with pytest.raises(store.EnrollmentError, match="^dashboard request refused$"):
        store.redeem(conn, first["code"])
    fresh = store.mint(conn, " ")
    assert conn.execute("SELECT COUNT(*) FROM dashboard_enrollment_codes").fetchone()[0] == 1
    conn.execute(
        "CREATE TRIGGER deny_session BEFORE INSERT ON dashboard_sessions BEGIN SELECT RAISE(ABORT, 'fixture insertion failure'); END"
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="fixture insertion failure"):
        store.redeem(conn, fresh["code"])
    assert store.available(conn, fresh["code"])
    assert not conn.in_transaction
    conn.execute("DROP TRIGGER deny_session")
    conn.commit()
    one = store.redeem(conn, fresh["code"])
    two = store.redeem(conn, store.mint(conn)["code"])
    assert one != two and one != fresh["code"] and store.valid_credential(one)
    rows = conn.execute(
        "SELECT session_id, credential_digest, label, scope, created_at, expires_at FROM dashboard_sessions"
    ).fetchall()
    assert len({row[0] for row in rows}) == 2 and len({row[1] for row in rows}) == 2
    assert all(row[2:] == (None, "read-only", 1300, 1300 + 2592000) for row in rows)
    dump = "\n".join(conn.iterdump())
    assert all(secret not in dump for secret in (fresh["code"], one, two))
    assert store.authorized(conn, one) and store.authorized(conn, two)
    clock[0] += 10
    assert store.authorized(conn, one)
    assert all(
        row["expires_at"] == datetime.fromtimestamp(1300 + 2592000, timezone.utc).isoformat()
        for row in store.list_sessions(conn)["sessions"]
    )
    clock[0] = 1300 + 2592000
    assert not store.authorized(conn, one) and store.list_sessions(conn)["sessions"] == []
    assert len(store.list_sessions(conn, include_all=True)["sessions"]) == 2
    conn.close()


def test_two_connections_single_use_and_expiry_checked_after_lock(tmp_path, monkeypatch):
    db = tmp_path / "hub.db"
    conn = fleet_hub.init_db(db)
    code = store.mint(conn)["code"]
    barrier = threading.Barrier(2)

    def consume():
        local = fleet_hub.open_db(db)
        try:
            barrier.wait(timeout=5)
            try:
                return store.redeem(local, code)
            except store.EnrollmentError:
                return None
        finally:
            local.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: consume(), range(2)))
    assert sum(value is not None for value in results) == 1
    assert conn.execute("SELECT COUNT(*) FROM dashboard_sessions").fetchone()[0] == 1
    clock = [2000.0]
    monkeypatch.setattr(store.time, "time", lambda: clock[0])
    code = store.mint(conn)["code"]
    conn.execute("BEGIN IMMEDIATE")
    started = threading.Event()

    def blocked():
        local = fleet_hub.open_db(db)
        try:
            started.set()
            with pytest.raises(store.EnrollmentError):
                store.redeem(local, code)
        finally:
            local.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(blocked)
        assert started.wait(timeout=5)
        clock[0] = 2300
        conn.commit()
        future.result(timeout=5)
    assert conn.execute("SELECT COUNT(*) FROM dashboard_sessions").fetchone()[0] == 1
    conn.close()


def test_bearer_rotation_does_not_revoke_random_session(hub):
    credential = cookie(hub)
    hub[0].RequestHandlerClass = fleet_hub.make_handler("fixture-rotated-admin", hub[1])
    assert request(hub, "GET", "/deck", headers={"Cookie": credential})[0] == 200
    assert request(hub, "GET", "/dashboard/sessions", headers=admin_headers())[0] == 403
    assert (
        request(
            hub,
            "GET",
            "/dashboard/sessions",
            headers={"Authorization": "Bearer fixture-rotated-admin"},  # content-guard: allow bearer-token
        )[0]
        == 200
    )
    conn = fleet_hub.open_db(hub[1])
    conn.execute("DROP TABLE dashboard_sessions")
    conn.commit()
    conn.close()
    assert request(hub, "GET", "/deck", headers={"Cookie": credential})[0] == 401


def test_upgrade_v22_preserves_nodes_events_claims_presence_and_rollback_refuses(tmp_path, monkeypatch):
    db = tmp_path / "hub.db"
    conn = fleet_hub.init_db(db)
    node = "11111111-1111-4111-8111-111111111111"
    fleet_hub.add_node(conn, node, "fixture node")
    fleet_hub.store_events(
        conn,
        [
            {
                "node_id": node,
                "run_id": "fixture-run",
                "state": "run.created",
                "ts": "2026-01-01T00:00:00Z",
                "sequence": 1,
                "digest": "fixture",
            }
        ],
    )
    fleet_hub.handle_claim(
        conn, {"action": "acquire", "target": "example/project", "node_id": node, "holder": "fixture-holder"}
    )
    fleet_hub_sessions.handle_session(
        conn,
        {
            "action": "upsert",
            "harness": "codex",
            "session_id": "fixture-presence",
            "repo_identity": "example/project",
            "identity_scope": "fleet",
            "repo_label": "fixture",
            "checkout_path": "/tmp/fixture",
            "branch": "main",
            "dirty_paths": [],
            "dirty_truncated": False,
            "ttl_seconds": 900,
        },
        caller_node=node,
    )
    tables = ("nodes", "events", "claims", "interactive_sessions")
    before = {table: conn.execute(f"SELECT * FROM {table}").fetchall() for table in tables}
    conn.execute("DROP TABLE dashboard_enrollment_codes")
    conn.execute("DROP TABLE dashboard_sessions")
    conn.execute("PRAGMA user_version=22")
    conn.commit()
    conn.close()
    conn = fleet_hub.init_db(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 23
    assert {table: conn.execute(f"SELECT * FROM {table}").fetchall() for table in tables} == before
    assert store.mint(conn)["code"]
    conn.close()
    monkeypatch.setattr(fleet_hub, "SCHEMA_VERSION", 22)
    with pytest.raises(fleet_hub.FleetHubError):
        fleet_hub.init_db(db)


def test_keyset_pagination_includes_expired_and_revoked_only_with_all(hub):
    conn = fleet_hub.open_db(hub[1])
    now = time.time()
    conn.executemany(
        "INSERT INTO dashboard_sessions VALUES (?, ?, NULL, 'read-only', ?, ?, NULL)",
        [
            ("ds_" + f"{i:032x}", hashlib.sha256(f"fixture-{i}".encode()).hexdigest(), now, now + 2592000)
            for i in range(103)
        ],
    )
    conn.execute("UPDATE dashboard_sessions SET expires_at = ? WHERE session_id = ?", (now, "ds_" + "0" * 32))
    store.revoke(conn, "ds_" + f"{1:032x}")
    conn.close()
    first = json.loads(request(hub, "GET", "/dashboard/sessions", headers=admin_headers())[2])
    assert len(first["sessions"]) == 100 and first["sessions"][0]["session_id"] == "ds_" + f"{2:032x}"
    second = json.loads(
        request(hub, "GET", "/dashboard/sessions?after=" + first["next_after"], headers=admin_headers())[2]
    )
    assert len(second["sessions"]) == 1 and second["next_after"] is None
    assert len({row["session_id"] for row in first["sessions"] + second["sessions"]}) == 101
    all_rows = json.loads(request(hub, "GET", "/dashboard/sessions?all=1", headers=admin_headers())[2])
    assert len(all_rows["sessions"]) == 100 and all_rows["sessions"][0]["session_id"] == "ds_" + "0" * 32


@pytest.mark.parametrize(
    "trusted,proto,secure",
    [
        (False, "https", False),
        (True, "https", True),
        (True, "http", False),
        (True, "https,http", False),
        (True, "HTTPS", False),
    ],
)
def test_forwarded_scheme_only_explicit_trusted_https(hub, trusted, proto, secure):
    hub[0].RequestHandlerClass = fleet_hub.make_handler(ADMIN, hub[1], trust_forwarded_proto=trusted)
    code = mint(hub)
    scheme = "https" if secure else "http"
    status, headers, _ = redeem(
        hub,
        code,
        **{
            "X-Forwarded-Proto": proto,
            "Host": "example.test",
            "Origin": f"{scheme}://EXAMPLE.TEST:{443 if secure else 80}",
            "X-Forwarded-Host": "other.test",
            "X-Forwarded-Port": "9999",
        },
    )
    assert status == 303
    assert ("; Secure" in headers["Set-Cookie"]) is secure


def test_duplicate_forwarded_scheme_does_not_assert_tls(hub):
    hub[0].RequestHandlerClass = fleet_hub.make_handler(ADMIN, hub[1], trust_forwarded_proto=True)
    code = mint(hub)
    headers = [
        ("Host", "example.test"),
        ("Origin", "http://example.test"),
        ("X-Forwarded-Proto", "https"),
        ("X-Forwarded-Proto", "https"),
        ("Content-Type", "application/x-www-form-urlencoded"),
    ]
    status, response_headers, _ = raw_request(hub, "POST", "/enroll", headers, f"code={code}")
    assert status == 303 and "Secure" not in response_headers["Set-Cookie"]


def test_proxy_opt_in_requires_numeric_loopback_bind_and_peer(tmp_path):
    for host in ("0.0.0.0", "localhost", "example.test"):
        with pytest.raises(fleet_hub.FleetHubError, match="numeric loopback"):
            fleet_hub.make_server(host, 0, tmp_path / "hub.db", ADMIN, trust_forwarded_proto=True)
    handler_type = fleet_hub.make_handler(ADMIN, tmp_path / "hub.db", trust_forwarded_proto=True)
    handler = object.__new__(handler_type)
    handler.connection = socket.socket()
    handler.client_address = ("192.0.2.1", 1234)
    from email.message import Message

    handler.headers = Message()
    handler.headers["X-Forwarded-Proto"] = "https"
    handler.server = type("Server", (), {"server_address": ("127.0.0.1", 1)})()
    assert not handler._secure_transport()
    handler.connection.close()
    # Actual SSLSocket is recognized independently of proxy opt-in or identity.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    with context.wrap_socket(socket.socket(), server_side=True, do_handshake_on_connect=False) as connection:
        handler.connection = connection
        assert handler._secure_transport()


@pytest.mark.parametrize(
    "value",
    [
        "http://remote.example.test",
        "https://user:password@example.test",
        "https://example.test/path",
        "https://example.test?code=fixture-secret",
        "https://example.test#fragment",
        "https://example.test:",
        "https://example.test:99999",
        "https://bad host",
        "https://example.test\n",
        "file:///tmp/fixture",
        "//example.test",
        "https://example.test/?",
        "https://example.test/#",
    ],
)
def test_client_url_refused_before_mint(hub, monkeypatch, value):
    base = f"http://127.0.0.1:{hub[0].server_address[1]}"
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": base, "admin_token": ADMIN, "node_token": "fixture-node"},
    )
    with pytest.raises(client.DashboardClientError) as error:
        client.enroll(base_url=value)
    assert "fixture-secret" not in str(error.value) and ADMIN not in str(error.value)
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": value, "admin_token": ADMIN, "node_token": "fixture-node"},
    )
    with pytest.raises(client.DashboardClientError):
        client.enroll()
    conn = fleet_hub.open_db(hub[1])
    assert conn.execute("SELECT COUNT(*) FROM dashboard_enrollment_codes").fetchone()[0] == 0
    conn.close()


def test_cli_enroll_session_list_revoke_and_activity_compatibility(hub, monkeypatch, capsys):
    base = f"http://127.0.0.1:{hub[0].server_address[1]}"
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": base, "admin_token": ADMIN, "node_token": "fixture-node"},
    )
    assert (
        cli.main(["fleet", "enroll", "--label", "  fixture browser  ", "--base-url", "https://browser.example.test"])
        == 0
    )
    url = capsys.readouterr().out.strip()
    assert url.startswith("https://browser.example.test/enroll?code=") and ADMIN not in url
    code = url.split("code=")[1]
    assert redeem(hub, code)[0] == 303
    assert cli.main(["fleet", "sessions", "--dashboard", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    row = result["sessions"][0]
    assert row["label"] == "fixture browser"
    assert cli.main(["fleet", "sessions", "--dashboard", "--revoke", row["session_id"], "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"revoked": True, "session_id": row["session_id"]}
    assert cli.main(["fleet", "sessions", "--dashboard", "--all", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["sessions"][0]["revoked_at"] is not None
    for flags in (
        ["--revoke", row["session_id"]],
        ["--after", row["session_id"]],
        ["--dashboard", "--revoke", row["session_id"], "--all"],
        ["--dashboard", "--revoke", row["session_id"], "--after", row["session_id"]],
    ):
        assert cli.main(["fleet", "sessions", *flags]) == 2
        assert "browser session" in capsys.readouterr().err
    calls = []

    def fetch(*, include_all):
        calls.append(include_all)
        return [{"session_id": "fixture-presence"}]

    monkeypatch.setattr(fleet_client, "fetch_sessions", fetch)
    assert cli.main(["fleet", "sessions", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == [{"session_id": "fixture-presence"}]
    assert cli.main(["fleet", "sessions", "--all", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == [{"session_id": "fixture-presence"}]
    assert calls == [False, True]


@pytest.mark.parametrize(
    "flags,status,error",
    [
        (["--revoke", ""], 2, "require --dashboard"),
        (["--after", ""], 2, "require --dashboard"),
        (["--dashboard", "--revoke", ""], 1, "invalid dashboard session id"),
        (["--dashboard", "--after", ""], 1, "invalid dashboard session cursor"),
        (["--dashboard", "--revoke", "", "--all"], 2, "cannot combine --all or --after"),
        (["--dashboard", "--revoke", "", "--after", "ds_" + "0" * 32], 2, "cannot combine --all or --after"),
        (["--dashboard", "--revoke", "ds_" + "0" * 32, "--after", ""], 2, "cannot combine --all or --after"),
    ],
)
def test_cli_sessions_explicit_empty_options_are_not_omitted(hub, monkeypatch, capsys, flags, status, error):
    base = f"http://127.0.0.1:{hub[0].server_address[1]}"
    monkeypatch.setattr(fleet_client, "load_fleet_settings", lambda: {"hub_url": base, "admin_token": ADMIN})
    monkeypatch.setattr(fleet_client, "fetch_sessions", lambda *, include_all: [])
    assert cli.main(["fleet", "sessions", "--json", *flags]) == status
    output = capsys.readouterr()
    assert output.out == ""
    assert error in output.err


def test_dashboard_client_never_falls_back_to_node(hub, monkeypatch):
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {
            "hub_url": f"http://127.0.0.1:{hub[0].server_address[1]}",
            "admin_token": "",
            "node_token": "fixture-node",
        },
    )
    for operation in (client.enroll, client.sessions, lambda: client.revoke("ds_" + "0" * 32)):
        with pytest.raises(client.DashboardClientError, match="configured admin token"):
            operation()


@pytest.mark.parametrize("mode", ["redirect", "oversized", "error", "malformed", "duplicate", "unsafe-metadata"])
def test_client_transport_redirect_bounds_and_secret_safe_errors(hub, monkeypatch, mode, capsys):
    from http.server import BaseHTTPRequestHandler

    observed = []
    fake_secret = "fixture-response-secret"

    class Reply(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            observed.append(self.path)
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            status = 302 if mode == "redirect" else 400 if mode == "error" else 201
            body = fake_secret.encode()
            if mode == "oversized":
                body = b"x" * (131072 + 1)
            elif mode == "duplicate":
                body = b'{"code":"' + b"x" * 43 + b'","code":"' + b"y" * 43 + b'","expires_at":"2026-01-01T00:00:00Z"}'
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            if mode == "redirect":
                self.send_header("Location", "/fixture-secret-redirect")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            observed.append(self.path)
            row = {
                "session_id": "ds_" + "0" * 32,
                "label": "bad\nlabel",
                "scope": "read-only",
                "created_at": "2026-01-01T00:00:00Z",
                "expires_at": "2026-02-01T00:00:00Z",
                "revoked_at": None,
            }
            body = json.dumps({"sessions": [row], "next_after": None}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    hub[0].RequestHandlerClass = Reply
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {
            "hub_url": f"http://127.0.0.1:{hub[0].server_address[1]}",
            "admin_token": ADMIN,
            "node_token": "fixture-node",
        },
    )
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    flags = ["fleet", "sessions", "--dashboard"] if mode == "unsafe-metadata" else ["fleet", "enroll"]
    assert cli.main(flags) == 1
    output = capsys.readouterr()
    assert not output.out and fake_secret not in output.err and ADMIN not in output.err
    assert len(observed) == 1 and "fixture-secret-redirect" not in observed[0]
