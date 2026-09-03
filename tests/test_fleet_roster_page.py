"""Hub-served roster page: GET rendering, auth, and POST apply (roster page spec)."""

from __future__ import annotations

import http.client
import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from urllib.parse import urlencode

import pytest

from brigade import fleet_hub, fleet_hub_roster_page

TOKEN = "test-admin-token-roster"
NODE_A = "11111111-1111-4111-8111-111111111111"
SEATS = {
    "agy_flash": {
        "provider": "google", "model": "gemini-3.8-flash-high", "reasoning": "none",
        "brigade_cli": "antigravity", "t3_instance_id": "", "limit": 4,
    },
    "coder": {
        "provider": "openai", "model": "gpt-5.6-terra", "reasoning": "high",
        "brigade_cli": "codex", "t3_instance_id": "codex", "limit": 1,
    },
    "daybreak": {
        "provider": "openai", "model": "gpt-daybreak-blue-latest", "reasoning": "high",
        "brigade_cli": "codex", "t3_instance_id": "", "limit": 1,
    },
    "cursor_grok": {
        "provider": "cursor", "model": "cursor-grok-4.6-high-fast", "reasoning": "none",
        "brigade_cli": "cursor", "t3_instance_id": "cursor", "limit": 8,
    },
}


@contextmanager
def _hub(tmp_path, *, trust_tailscale: bool = False):
    db = tmp_path / "hub" / "fleet.db"
    server = fleet_hub.make_server(
        "127.0.0.1", 0, db, TOKEN, trust_tailscale_identity=trust_tailscale
    )  # content-guard: allow loopback-ipv4
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield ("127.0.0.1", server.server_address[1]), db  # content-guard: allow loopback-ipv4
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _request(hub, method: str, path: str, *, headers: dict | None = None, body: bytes | None = None):
    host, port = hub
    conn = http.client.HTTPConnection(host, port, timeout=5)
    conn.request(method, path, body=body, headers=headers or {})
    response = conn.getresponse()
    text = response.read().decode("utf-8")
    result = (response.status, {k.lower(): v for k, v in response.getheaders()}, text)
    conn.close()
    return result


def _bearer() -> dict:
    return {"Authorization": f"Bearer {TOKEN}"}


def _json(hub, method: str, path: str, body: dict, *, token: str = TOKEN):
    status, _headers, text = _request(
        hub, method, path,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        body=json.dumps(body).encode("utf-8"),
    )
    return status, json.loads(text)


def _revision(hub) -> int:
    status, _headers, text = _request(hub, "GET", "/models", headers=_bearer())
    assert status == 200
    return int(json.loads(text)["revision"])


def _seed(hub) -> None:
    for seat, fields in SEATS.items():
        status, payload = _json(
            hub, "POST", "/models",
            {"action": "set", "seat": seat, "enabled": True, "expected_revision": _revision(hub), **fields},
        )
        assert status == 200, payload
    status, payload = _json(
        hub, "PUT", "/preference",
        {"impl": "coder", "review": "coder", "chef": "coder", "notes": "seeded"},
    )
    assert status == 200, payload


def _login_cookie(hub) -> str:
    status, headers, _text = _request(hub, "GET", f"/deck/roster?token={TOKEN}")
    assert status == 303
    assert headers["location"] == "/deck/roster"
    return headers["set-cookie"].split(";")[0]


def _enroll_node(db) -> str:
    conn = fleet_hub.open_db(db)
    try:
        _node, node_token = fleet_hub.add_node(conn, NODE_A, "node-a")
    finally:
        conn.close()
    return node_token


def _form(hub, fields: dict, *, cookie: str | None = None, extra: dict | None = None) -> tuple:
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Sec-Fetch-Site": "same-origin"}
    if cookie:
        headers["Cookie"] = cookie
    headers.update(extra or {})
    return _request(hub, "POST", "/deck/roster", headers=headers, body=urlencode(fields, doseq=True).encode())


def _current_form(hub, cookie: str) -> dict:
    """The field set a browser would submit from the freshly rendered page."""
    status, _headers, page = _request(hub, "GET", "/deck/roster", headers={"Cookie": cookie})
    assert status == 200
    fields = {"csrf": fleet_hub_roster_page.csrf_value(TOKEN)}
    fields["expected_revision"] = re.search(r'name="expected_revision" value="(\d+)"', page).group(1)
    fields["expected_preference_updated_at"] = re.search(
        r'name="expected_preference_updated_at" value="([^"]*)"', page
    ).group(1)
    for seat in re.findall(r'name="seat\.([a-z0-9._-]+)" value="1" checked', page):
        fields[f"seat.{seat}"] = "1"
    for provider in re.findall(r'name="cloud\.([a-z0-9-]+)" value="1" checked', page):
        fields[f"cloud.{provider}"] = "1"
    for role in fleet_hub_roster_page.ROLES:
        match = re.search(rf'name="role\.{role}".*?<option value="([^"]*)" selected', page, re.S)
        fields[f"role.{role}"] = match.group(1) if match else ""
    for consumer in fleet_hub_roster_page.CONSUMERS:
        match = re.search(rf'name="default\.{consumer}".*?<option value="([^"]*)" selected', page, re.S)
        fields[f"default.{consumer}"] = match.group(1) if match else ""
    notes = re.search(r'<textarea name="notes"[^>]*>([^<]*)</textarea>', page)
    fields["notes"] = notes.group(1) if notes else ""
    return fields


def _tables(db) -> str:
    conn = sqlite3.connect(db)
    try:
        return "\n".join(
            line
            for line in conn.iterdump()
            if any(
                table in line
                for table in ("model_policy", "model_consumer_defaults", "model_roster_meta", "run_preference", "cloud_provider_state")
            )
        )
    finally:
        conn.close()


# --- GET -------------------------------------------------------------------


def test_roster_page_requires_auth_and_renders_every_block(tmp_path):
    with _hub(tmp_path) as (hub, _db):
        _seed(hub)
        assert _request(hub, "GET", "/deck/roster")[0] == 401
        cookie = _login_cookie(hub)
        status, headers, page = _request(hub, "GET", "/deck/roster", headers={"Cookie": cookie})
        assert status == 200
        assert headers["cache-control"] == "no-store"
        assert 'http-equiv="refresh"' not in page
        assert "Roster" in page and 'href="/deck/roster"' in page
        for heading in ("Roles", "Seats", "Cloud lanes", "Consumer defaults", "Retired families"):
            assert heading in page
        assert 'name="role.security"' in page and 'name="role.scout"' in page
        assert 'name="seat.agy_flash" value="1" checked' in page
        assert 'name="cloud.jules" value="1" checked' in page
        assert 'name="cloud.claude" value="1"' in page and 'name="cloud.claude" value="1" checked' not in page
        assert 'name="default.brigade-run"' in page and 'name="default.t3-fleet"' in page
        assert "gpt-5.4" in page and "permanent" in page
        assert f'name="expected_revision" value="{_revision(hub)}"' in page
        assert 'name="csrf" value=' in page
        assert '<button type="submit">Save</button>' in page
        assert TOKEN not in page and cookie.split("=", 1)[1] not in page
        # bearer works too
        assert _request(hub, "GET", "/deck/roster", headers=_bearer())[0] == 200


def test_roster_page_deck_nav_links_to_it(tmp_path):
    with _hub(tmp_path) as (hub, _db):
        deck = _request(hub, "GET", "/deck", headers=_bearer())[2]
        assert '<a href="/deck/roster">roster</a>' in deck


def test_roster_page_read_only_under_tailscale_identity(tmp_path):
    with _hub(tmp_path, trust_tailscale=True) as (hub, _db):
        _seed(hub)
        status, _headers, page = _request(
            hub, "GET", "/deck/roster", headers={"Tailscale-User-Login": "operator@example.test"}
        )
        assert status == 200
        assert "read-only" in page
        assert "<button" not in page
        assert page.count("disabled") >= 10
        assert "operator@example.test" not in page


def test_roster_page_escapes_hostile_notes(tmp_path):
    with _hub(tmp_path) as (hub, _db):
        _seed(hub)
        status, payload = _json(hub, "PUT", "/preference", {"notes": "<script>alert(1)</script>"})
        assert status == 200, payload
        page = _request(hub, "GET", "/deck/roster", headers=_bearer())[2]
        assert "<script>alert(1)</script>" not in page
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
