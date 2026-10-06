"""Read-only Worklore projection and Command Deck HTTP contracts."""

from __future__ import annotations

import http.client
import html
import json
import re
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urlencode

import pytest

from brigade import fleet_hub, worklore_store as store

TOKEN = "test-admin-token-work-page"  # content-guard: allow api-key-assignment
LOOPBACK = "127.0.0.1"  # content-guard: allow loopback-ipv4


@pytest.fixture(autouse=True)
def _enabled_worklore(monkeypatch):
    monkeypatch.setenv("BRIGADE_WORKLORE_ENABLED", "1")


@contextmanager
def _hub(tmp_path, **kwargs):
    db = tmp_path / "fleet.db"
    server = fleet_hub.make_server(LOOPBACK, 0, db, TOKEN, **kwargs)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield (LOOPBACK, server.server_address[1]), db
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _request(hub, path, *, headers=None, method="GET", body=None):
    conn = http.client.HTTPConnection(*hub, timeout=5)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read().decode()
    finally:
        conn.close()


def _bearer():
    return {"Authorization": f"Bearer {TOKEN}"}


def _cookie(hub):
    status, _, body = _request(
        hub,
        "/dashboard/enrollment",
        method="POST",
        headers={**_bearer(), "Content-Type": "application/json"},
        body=b"{}",
    )
    assert status == 201
    code = json.loads(body)["code"]
    status, headers, _ = _request(
        hub,
        "/enroll",
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded", "Sec-Fetch-Site": "same-origin"},
        body=f"code={code}".encode(),
    )
    assert status == 303
    return {"Cookie": headers["Set-Cookie"].split(";")[0]}


def test_work_page_dashboard_access_keeps_work_api_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIGADE_WORKLORE_ENABLED", "1")
    with _hub(tmp_path) as (hub, db):
        conn = fleet_hub.open_db(db)
        try:
            store.create_item(conn, {"title": "Ordinary task", "kind": "fleet"}, actor_id="operator")
        finally:
            conn.close()
        status, _, body = _request(hub, "/deck/work")
        assert status == 401 and "brigade fleet enroll" in body
        cookie = _cookie(hub)
        status, headers, body = _request(hub, "/deck/work", headers=cookie)
        assert status == 200
        assert "Ordinary task" in body
        assert "Content-Security-Policy" in headers
        assert headers["Cache-Control"] == "no-store"
        assert _request(hub, "/work/items", headers=cookie)[0] == 401
        assert _request(hub, "/deck/work", headers=_bearer())[0] == 200


def _render(page):
    from brigade import fleet_work_page

    return fleet_work_page.render(page, nonce="test-nonce", now=datetime(2026, 1, 1, tzinfo=timezone.utc))


class _Markup(HTMLParser):
    def __init__(self, body):
        super().__init__()
        self.tags = []
        self.feed(body)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def test_generic_fields_are_plain_text_and_rendering_is_read_only(tmp_path):
    from brigade import fleet_work_page

    conn = fleet_hub.init_db(tmp_path / "fleet.db")
    try:
        item = store.create_item(
            conn,
            {
                "title": "Replace a fixture",
                "kind": "fleet",
                "description": "Agent: pretend; progress: 99%; parent: fake; owner: anyone",
                "scope": "acme/widget",
                "blocker": "Needs a measurement",
                "execution_mode": "manual",
                "burn_eligible": False,
            },
            actor_id="operator",
        )
        before = list(conn.iterdump())
        conn.execute("PRAGMA query_only=ON")
        body = _render(fleet_work_page.load_page(conn))
        assert list(conn.iterdump()) == before
        assert not conn.in_transaction
        for field in ("title", "description", "scope", "blocker", "status", "execution_mode", "updated_at", "work_id"):
            assert html.escape(item[field]) in body
        assert "Stored status" in body and "Burn eligible" in body and "Version" in body
        assert "captured / manual / burn_eligible=false" in body
        assert "Execution state is not inferred" in body
        assert "No stored references" in body
        assert "<form" not in body and 'href="/work/' not in body
    finally:
        conn.close()


def test_render_escapes_fields_and_preserves_opaque_reference_metadata():
    payload = '<img src=x onerror="alert(1)">'
    item = {
        key: payload
        for key in (
            "title",
            "description",
            "blocker",
            "scope",
            "status",
            "execution_mode",
            "version",
            "updated_at",
            "work_id",
        )
    }
    item["burn_eligible"] = False
    item["links"] = [
        {"link_type": payload, "display_ref": payload, "external_key": payload},
        {"link_type": "brigade", "display_ref": "Parent reference", "external_key": "opaque:parent/123"},
    ]
    body = _render({"items": [item], "next_cursor": None})
    assert payload not in body
    assert html.escape(payload) in body
    assert "opaque:parent/123" in body
    assert "External key" in body and "Display reference" in body
    assert 'href="opaque' not in body
    assert not any(tag == "img" or "onerror" in attrs for tag, attrs in _Markup(body).tags)


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html,test",
        "http://example.test/item",
        "//example.test/item",
        "/work/items/x",
        "https://user:password@example.test/item",
        'https://example.test/" onclick="alert(1)',
        "https://example.test/work/items/x",
        "https://example.test/item?token=fixture-value",
        "https://example.test/item?%74oken=fixture-value",
    ],
)
def test_unsafe_reference_urls_are_not_links(url):
    body = _render({"items": [{"title": "Task", "links": [{"external_key": "opaque", "url": url}]}]})
    assert html.escape(url) in body
    assert f'href="{html.escape(url)}"' not in body
    assert not any("onclick" in attrs for _, attrs in _Markup(body).tags)


def test_safe_reference_url_is_escaped_without_invented_urls():
    url = "https://example.test/item?a=1&b=2"
    body = _render(
        {
            "items": [
                {"title": "Task", "links": [{"external_key": "opaque:evidence", "display_ref": "Evidence", "url": url}]}
            ]
        }
    )
    assert 'href="https://example.test/item?a=1&amp;b=2"' in body
    assert 'rel="noopener noreferrer"' in body
    assert "opaque:evidence" in body


def test_long_fields_and_both_link_truncation_sources_are_explicit(tmp_path):
    from brigade import fleet_work_page

    conn = fleet_hub.init_db(tmp_path / "fleet.db")
    try:
        item = store.create_item(
            conn,
            {"title": "Long task", "kind": "fleet", "description": "d" * 8000, "blocker": "b" * 2000},
            actor_id="operator",
        )
        for index in range(store.ITEM_LINK_PROJECTION_MAX + 1):
            store.add_link(
                conn,
                item["work_id"],
                {"link_type": "brigade", "external_key": f"opaque:{index}", "display_ref": "r" * 256},
                actor_id="operator",
            )
        page = fleet_work_page.load_page(conn)
        assert page["items"][0]["links_truncated"] is True
        body = _render(page)
        assert "d" * 1001 not in body and "b" * 501 not in body
        assert "[truncated at 1000 characters]" in body
        assert "[truncated at 500 characters]" in body
        assert "Showing 8 of 50 projected references" in body
        assert "Store reports additional references omitted from this projection" in body
        assert body.count("<br>External key:") == 8
        page["items"][0]["links_truncated"] = False
        assert "Store reports additional" not in _render(page)
    finally:
        conn.close()


def test_http_cursor_pagination_and_no_database_mutation(tmp_path, monkeypatch):
    from brigade import worklore_store

    with _hub(tmp_path) as (hub, db):
        conn = fleet_hub.open_db(db)
        try:
            for index in range(26):
                store.create_item(conn, {"title": f"Task {index:02}", "kind": "fleet"}, actor_id="operator")
            before = list(conn.iterdump())
        finally:
            conn.close()

        def no_migration(*args, **kwargs):
            raise AssertionError("GET must not ensure schema")

        monkeypatch.setattr(worklore_store, "ensure_schema", no_migration)
        status, _, first = _request(hub, "/deck/work", headers=_bearer())
        assert status == 200 and first.count('class="work-item panel"') == 25
        next_link = html.unescape(re.search(r'<a href="([^"]+)">Next page</a>', first)[1])
        status, _, second = _request(hub, next_link, headers=_bearer())
        assert status == 200 and second.count('class="work-item panel"') == 1
        assert 'href="/deck/work">First page</a>' in second
        assert "Next page</a>" not in second
        first_ids = set(re.findall(r"wl-[a-f0-9]{24}", first))
        second_ids = set(re.findall(r"wl-[a-f0-9]{24}", second))
        assert len(first_ids | second_ids) == 26 and not first_ids & second_ids
        assert 'href="/deck/work"' in _request(hub, "/deck", headers=_bearer())[2]
        conn = fleet_hub.open_db(db)
        try:
            assert list(conn.iterdump()) == before
        finally:
            conn.close()


def test_empty_query_bypasses_legacy_strict_parser_and_nonempty_queries_delegate(monkeypatch):
    from brigade import fleet_work_page

    parse_qs = fleet_work_page.parse_qs
    calls = []

    def legacy_parse_qs(query, **kwargs):
        calls.append(query)
        if query == "" and kwargs.get("strict_parsing"):
            raise ValueError("bad query field: ''")
        return parse_qs(query, **kwargs)

    monkeypatch.setattr(fleet_work_page, "parse_qs", legacy_parse_qs)
    assert fleet_work_page.parse_query("") is None
    assert calls == []
    assert fleet_work_page.parse_query("cursor=opaque%2B%2F%3D") == "opaque+/="
    invalid = (" ", "&", "cursor", "cursor=a&cursor=b", "unknown=x", "token=not-a-credential")
    for query in invalid:
        with pytest.raises(ValueError):
            fleet_work_page.parse_query(query)
    assert calls == ["cursor=opaque%2B%2F%3D", *invalid]


def test_next_cursor_link_is_percent_encoded_and_attribute_safe():
    cursor = 'opaque+/=&"<>'
    body = _render({"items": [], "next_cursor": cursor})
    assert f'href="/deck/work?{html.escape(urlencode({"cursor": cursor}))}"' in body
    assert "No Worklore tasks on this page" in body


def test_bad_queries_are_safe_400_and_token_refusal_precedes_auth(tmp_path):
    with _hub(tmp_path) as (hub, _):
        for query in (
            "cursor=bad",
            "cursor=",
            "cursor=%E2%98%83",
            "cursor=" + "a" * 2049,
            "cursor=a&cursor=b",
            "unknown=x",
            "cursor",
            "cursor=e30",
        ):
            status, _, body = _request(hub, f"/deck/work?{query}", headers=_bearer())
            assert status == 400
            assert "Traceback" not in body and "sqlite" not in body and query not in body
        for headers in ({}, _bearer()):
            status, _, body = _request(hub, "/deck/work?token=not-a-credential&cursor=bad", headers=headers)
            assert status == 401 and "not-a-credential" not in body


def test_empty_store_and_trusted_identity_read_access(tmp_path):
    with _hub(tmp_path, trust_tailscale_identity=True) as (hub, _):
        status, _, body = _request(hub, "/deck/work", headers={"Tailscale-User-Login": "viewer@example.test"})
        assert status == 200 and "No Worklore tasks on this page" in body
        assert 'href="/deck/work">First page</a>' in body
        assert "viewer@example.test" not in body


def test_disabled_worklore_page_never_loads_populated_store(tmp_path, monkeypatch):
    from brigade import fleet_work_page

    with _hub(tmp_path) as (hub, db):
        conn = fleet_hub.open_db(db)
        try:
            store.create_item(conn, {"title": "Hidden task", "kind": "fleet"}, actor_id="operator")
        finally:
            conn.close()

        def forbidden_load(*args, **kwargs):
            raise AssertionError("Disabled Worklore must not read stored tasks")

        monkeypatch.setenv("BRIGADE_WORKLORE_ENABLED", "0")
        monkeypatch.setattr(fleet_work_page, "load_page", forbidden_load)
        for headers in ({}, _bearer(), _cookie(hub)):
            status, _, body = _request(hub, "/deck/work", headers=headers)
            assert status == 404
            assert "Hidden task" not in body


def _group_raw(**changes):
    return {
        "key": "sample-source",
        "label": "Sample observations via Relay",
        "coverage": "Five retained task observations only",
        "source_ref": "opaque:sample-source",
        "proxy_ref": "opaque:relay",
        "parent_ref": "opaque:sample-parent",
        "work_ids": [f"wl-{index:024x}" for index in range(5)],
        **changes,
    }


def _group_config(tmp_path, groups=None):
    from brigade import fleet_command_deck as deck

    path = tmp_path / "deck.json"
    path.write_text(
        json.dumps(
            {
                "stations": [{"node_id": "sample-node", "capacity": 1}],
                "observed_work_groups": [_group_raw()] if groups is None else groups,
            }
        )
    )
    return deck.load_config(path)


def test_observed_config_is_optional_bounded_and_frozen(tmp_path):
    from brigade import fleet_command_deck as deck

    config = _group_config(tmp_path)
    group = config.observed_work_groups[0]
    assert group.key == "sample-source" and len(group.work_ids) == 5
    assert group.snapshot_observed_at is None
    assert isinstance(group.work_ids, tuple)
    assert deck.DeckConfig().observed_work_groups == ()
    assert _group_config(tmp_path, []).observed_work_groups == ()
    assert config.cloud == deck.default_cloud_config()
    assert config.stations == (deck.StationConfig("sample-node", "", 1),)
    assert len(_group_config(tmp_path, [_group_raw(key=f"sample-{i}") for i in range(8)]).observed_work_groups) == 8
    assert (
        len(
            _group_config(tmp_path, [_group_raw(work_ids=[f"wl-{i:024x}" for i in range(25)])])
            .observed_work_groups[0]
            .work_ids
        )
        == 25
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"key": "../work"},
        {"key": "sample?token=x"},
        {"key": "a" * 65},
        {"key": "x\n"},
        {"label": ""},
        {"label": "x" * 65},
        {"label": 1},
        {"coverage": "x" * 257},
        {"source_ref": "x\n"},
        {"parent_ref": None},
        {"proxy_ref": ""},
        {"work_ids": []},
        {"work_ids": "wl-example"},
        {"work_ids": [1]},
        {"work_ids": ["wl-example"] * 2},
        {"work_ids": [f"wl-{i:024x}" for i in range(26)]},
        {"work_ids": ["x" * 129]},
        {"work_ids": ["x\n"]},
        {"snapshot_observed_at": "2026-01-01T00:00:00"},
        {"snapshot_observed_at": "unknown"},
        {"snapshot_observed_at": "2026-01-01\x0012:00:00+00:00"},
        {"snapshot_observed_at": 1},
        {"snapshot_observed_at": None},
        {"unknown": "secret-input"},
    ],
)
def test_observed_config_rejects_invalid_fields_without_reflection(tmp_path, changes):
    from brigade import fleet_command_deck as deck

    with pytest.raises(deck.DeckConfigError, match="^invalid observed work groups$"):
        _group_config(tmp_path, [_group_raw(**changes)])


@pytest.mark.parametrize(
    "groups", [None, {}, ["bad"], [_group_raw()] * 2, [_group_raw(key=f"sample-{i}") for i in range(9)]]
)
def test_observed_config_rejects_invalid_collections(tmp_path, groups):
    from brigade import fleet_command_deck as deck

    path = tmp_path / "deck.json"
    path.write_text(
        json.dumps({"stations": [{"node_id": "sample-node", "capacity": 1}], "observed_work_groups": groups})
    )
    with pytest.raises(deck.DeckConfigError, match="^invalid observed work groups$"):
        deck.load_config(path)


@pytest.mark.parametrize(
    ("stamp", "freshness"),
    [
        (None, "unknown"),
        ("2025-12-31T00:00:00Z", "stale"),
        ("2026-01-01T00:01:00+00:00", "future"),
        ("2026-01-01T01:00:00+01:00", "recent"),
    ],
)
def test_observed_projection_only_reads_configured_ids_and_honest_times(tmp_path, monkeypatch, stamp, freshness):
    from brigade import fleet_work_page

    conn = fleet_hub.init_db(tmp_path / "fleet.db")
    try:
        item = store.create_item(
            conn, {"title": "Retained task", "kind": "fleet", "description": "Owner: pretend"}, actor_id="operator"
        )
        archived = store.create_item(conn, {"title": "Archived task", "kind": "fleet"}, actor_id="operator")
        # Establish an archived fixture without changing transition policy.
        conn.execute(
            "UPDATE work_items SET status='archived', archived_at='2025-01-01T00:00:00Z' WHERE work_id=?",
            (archived["work_id"],),
        )
        conn.commit()
        store.create_item(conn, {"title": "Unconfigured task", "kind": "fleet"}, actor_id="operator")
        raw = _group_raw(work_ids=[item["work_id"], archived["work_id"], "wl-unavailable"])
        if stamp is not None:
            raw["snapshot_observed_at"] = stamp
        config = _group_config(tmp_path, [raw])
        before = list(conn.iterdump())
        conn.execute("PRAGMA query_only=ON")

        def forbidden(*args, **kwargs):
            raise AssertionError("group GET must not migrate or enumerate")

        monkeypatch.setattr(store, "ensure_schema", forbidden)
        monkeypatch.setattr(store, "list_items", forbidden)
        page = fleet_work_page.load_observed_group(conn, config.observed_work_groups[0])
        body = fleet_work_page.render_observed_group(
            page,
            nonce="test",
            now=datetime(2026, 1, 1, tzinfo=timezone.utc),
            stale_after_seconds=config.stale_after_seconds,
        )
        assert "Retained task" in body and "Unconfigured task" not in body
        assert "Archived task" not in body and body.count("Unavailable work reference") == 2
        assert "archived" in body and "wl-unavailable" in body
        assert "3 configured observation records" in body
        assert f"Snapshot freshness: {freshness}" in body
        assert (
            "Work-record update time" in body and "Recent observation metadata does not establish live activity" in body
        )
        for key in ("label", "coverage", "source_ref", "proxy_ref", "parent_ref"):
            assert raw[key] in body
        assert list(conn.iterdump()) == before and not conn.in_transaction
    finally:
        conn.close()


def test_observed_group_http_discovery_auth_and_read_only(tmp_path, monkeypatch):
    from brigade import fleet_work_page

    conn = fleet_hub.init_db(tmp_path / "fleet.db")
    try:
        ids = [
            store.create_item(conn, {"title": f"Observation {i}", "kind": "fleet"}, actor_id="operator")["work_id"]
            for i in range(5)
        ]
        store.create_item(conn, {"title": "Other work", "kind": "fleet"}, actor_id="operator")
    finally:
        conn.close()
    _group_config(tmp_path, [_group_raw(work_ids=ids)])
    route = "/deck/observed/sample-source"
    with _hub(tmp_path, deck_config_path=tmp_path / "deck.json", trust_tailscale_identity=True) as (hub, db):
        assert _request(hub, route)[0] == 401
        conn = fleet_hub.open_db(db)
        try:
            _, node_token = fleet_hub.add_node(conn, "sample-api-node", "Sample node")
        finally:
            conn.close()
        assert _request(hub, route, headers={"Authorization": f"Bearer {node_token}"})[0] == 401
        cookie = _cookie(hub)
        conn = fleet_hub.open_db(db)
        try:
            before = list(conn.iterdump())
        finally:
            conn.close()

        def no_migration(*args, **kwargs):
            raise AssertionError("GET must not initialize or migrate")

        monkeypatch.setattr(fleet_hub, "init_db", no_migration)
        monkeypatch.setattr(store, "ensure_schema", no_migration)
        (tmp_path / "deck.json").write_text("invalid after startup")
        for auth in (_bearer(), cookie, {"Tailscale-User-Login": "viewer@example.test"}):
            status, headers, body = _request(hub, route, headers=auth)
            assert status == 200 and body.count('class="work-item panel"') == 5
            assert "Other work" not in body and "5 configured observation records" in body
            assert "Content-Security-Policy" in headers and headers["Cache-Control"] == "no-store"
            assert f'href="{route}"' in _request(hub, "/deck", headers=auth)[2]
            assert _request(hub, route + "?cursor=bad", headers=auth)[0] == 400
            assert _request(hub, "/deck/observed/unknown", headers=auth)[0] == 404
        assert _request(hub, "/work/items", headers=cookie)[0] == 401
        assert "Other work" in _request(hub, "/deck/work", headers=_bearer())[2]
        assert _request(hub, route + "?token=not-a-credential")[0] == 401

        def forbidden(*args, **kwargs):
            raise AssertionError("disabled groups must not read tasks")

        monkeypatch.setenv("BRIGADE_WORKLORE_ENABLED", "0")
        monkeypatch.setattr(fleet_work_page, "load_observed_group", forbidden)
        for auth in ({}, _bearer(), cookie):
            assert _request(hub, route, headers=auth)[0] == 404
        assert route not in _request(hub, "/deck", headers=_bearer())[2]
        conn = fleet_hub.open_db(db)
        try:
            assert list(conn.iterdump()) == before
        finally:
            conn.close()


def test_absent_observed_group_route_is_not_available(tmp_path):
    with _hub(tmp_path) as (hub, _):
        assert _request(hub, "/deck/observed/sample-source", headers=_bearer())[0] == 404
        assert "/deck/observed/" not in _request(hub, "/deck", headers=_bearer())[2]


def test_observed_config_text_is_escaped_and_references_are_plain(tmp_path):
    from brigade import fleet_work_page

    payload = '<img src=x onerror="alert(1)">'
    config = _group_config(
        tmp_path,
        [
            _group_raw(
                label=payload,
                coverage=payload,
                source_ref="javascript:alert(1)",
                proxy_ref="https://example.test/work/items/x",
                parent_ref=payload,
            )
        ],
    )
    body = fleet_work_page.render_observed_group(
        {"group": config.observed_work_groups[0], "items": [], "unavailable": []},
        nonce="test",
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
        stale_after_seconds=1800,
    )
    assert payload not in body and html.escape(payload) in body
    assert 'href="javascript:' not in body and 'href="https://example.test/work/' not in body
    assert not any(tag == "img" or "onerror" in attrs for tag, attrs in _Markup(body).tags)
