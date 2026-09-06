"""Hub-served roster page: GET rendering, auth, and POST apply (roster page spec)."""

from __future__ import annotations

import http.client
import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from urllib.parse import urlencode

from datetime import datetime, timezone

from brigade import fleet_command_deck, fleet_hub, fleet_hub_preference, fleet_hub_roster_page
from brigade import fleet_hub_policy, fleet_policy, fleet_policy_page

TOKEN = "test-admin-token-roster"  # content-guard: allow api-key-assignment
NODE_A = "11111111-1111-4111-8111-111111111111"
SEATS = {
    "agy_flash": {
        "provider": "google",
        "model": "gemini-3.8-flash-high",
        "reasoning": "none",
        "brigade_cli": "antigravity",
        "t3_instance_id": "",
        "limit": 4,
    },
    "coder": {
        "provider": "openai",
        "model": "gpt-5.6-terra",
        "reasoning": "high",
        "brigade_cli": "codex",
        "t3_instance_id": "codex",
        "limit": 1,
    },
    "daybreak": {
        "provider": "openai",
        "model": "gpt-daybreak-blue-latest",
        "reasoning": "high",
        "brigade_cli": "codex",
        "t3_instance_id": "",
        "limit": 1,
    },
    "cursor_grok": {
        "provider": "cursor",
        "model": "cursor-grok-4.6-high-fast",
        "reasoning": "none",
        "brigade_cli": "cursor",
        "t3_instance_id": "cursor",
        "limit": 8,
    },
}


@contextmanager
def _hub(tmp_path, *, trust_tailscale: bool = False):
    db = tmp_path / "hub" / "fleet.db"
    server = fleet_hub.make_server(
        "127.0.0.1",  # content-guard: allow loopback-ipv4
        0,
        db,
        TOKEN,
        trust_tailscale_identity=trust_tailscale,
    )
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
        hub,
        method,
        path,
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
            hub,
            "POST",
            "/models",
            {"action": "set", "seat": seat, "enabled": True, "expected_revision": _revision(hub), **fields},
        )
        assert status == 200, payload
    status, payload = _json(
        hub,
        "PUT",
        "/preference",
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
    fields["expected_cloud_state"] = re.search(r'name="expected_cloud_state" value="([^"]*)"', page).group(1)
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
                for table in (
                    "model_policy",
                    "model_consumer_defaults",
                    "model_roster_meta",
                    "run_preference",
                    "cloud_provider_state",
                )
            )
        )
    finally:
        conn.close()


# --- GET -------------------------------------------------------------------


def test_roster_page_requires_auth_and_renders_every_block(tmp_path):
    with _hub(tmp_path) as (hub, _db):
        _seed(hub)
        status, _headers, unauth_page = _request(hub, "GET", "/deck/roster")
        assert status == 401
        assert "edits the roster" in unauth_page
        cookie = _login_cookie(hub)
        status, headers, page = _request(hub, "GET", "/deck/roster", headers={"Cookie": cookie})
        assert status == 200
        assert headers["cache-control"] == "no-store"
        assert 'http-equiv="refresh"' not in page
        assert "Roster" in page and 'href="/deck/roster"' in page
        # "Consumer defaults" was renamed: the control is the omitted-seat
        # admission fallback, not a default model or a second roster.
        for heading in ("Roles", "Seats", "Cloud lanes", "Admission fallback seat (legacy)", "Retired families"):
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


# --- POST ------------------------------------------------------------------


def test_roster_post_auth_csrf_origin_and_body_rules(tmp_path):
    with _hub(tmp_path) as (hub, db):
        _seed(hub)
        cookie = _login_cookie(hub)
        good = _current_form(hub, cookie)
        assert _form(hub, good)[0] == 401
        node_token = _enroll_node(db)
        assert _form(hub, good, extra={"Authorization": f"Bearer {node_token}"})[0] == 403
        bad_csrf = {**good, "csrf": "0" * 64}
        assert _form(hub, bad_csrf, cookie=cookie)[0] == 403
        assert _form(hub, good, cookie=cookie, extra={"Sec-Fetch-Site": "cross-site"})[0] == 403
        headers = {"Cookie": cookie, "Content-Type": "application/json"}
        assert _request(hub, "POST", "/deck/roster", headers=headers, body=b"{}")[0] == 415
        big = urlencode({**good, "notes": "x" * (64 * 1024)}).encode()
        headers = {
            "Cookie": cookie,
            "Content-Type": "application/x-www-form-urlencoded",
            "Sec-Fetch-Site": "same-origin",
        }
        # The hub answers 413 from Content-Length without reading the body; the
        # peer may see the response or a reset socket, never a write.
        try:
            assert _request(hub, "POST", "/deck/roster", headers=headers, body=big)[0] == 413
        except (BrokenPipeError, ConnectionResetError):
            pass
        assert _form(hub, {**good, "expected_revision": "x"}, cookie=cookie)[0] == 400


def test_roster_post_tailscale_identity_cannot_write(tmp_path):
    with _hub(tmp_path, trust_tailscale=True) as (hub, _db):
        _seed(hub)
        cookie = _login_cookie(hub)
        good = _current_form(hub, cookie)
        before = _tables(_db)
        status, _headers, _text = _form(hub, good, extra={"Tailscale-User-Login": "operator@example.test"})
        assert status == 403
        assert _tables(_db) == before


def test_roster_post_stale_revision_and_stale_preference_write_nothing(tmp_path):
    with _hub(tmp_path) as (hub, db):
        _seed(hub)
        cookie = _login_cookie(hub)
        form = _current_form(hub, cookie)
        # A CLI mutation lands between load and save.
        status, payload = _json(
            hub,
            "POST",
            "/models",
            {"action": "set", "seat": "coder", "enabled": False, "expected_revision": _revision(hub), **SEATS["coder"]},
        )
        assert status == 200, payload
        before = _tables(db)
        status, _headers, page = _form(hub, {**form, "role.security": "daybreak"}, cookie=cookie)
        assert status == 409
        assert "changed underneath you" in page
        assert _tables(db) == before
        assert 'name="seat.coder" value="1" checked' not in page
        assert '<option value="daybreak" selected' not in page
        assert f'name="expected_revision" value="{_revision(hub)}"' in page
        form = _current_form(hub, cookie)
        status, payload = _json(hub, "PUT", "/preference", {"impl": "agy_flash"})
        assert status == 200, payload
        before = _tables(db)
        status, _headers, page = _form(hub, {**form, "role.security": "daybreak"}, cookie=cookie)
        assert status == 409
        assert "run preference changed" in page
        assert _tables(db) == before


def test_roster_post_applies_everything_in_one_revision(tmp_path):
    with _hub(tmp_path) as (hub, db):
        _seed(hub)
        cookie = _login_cookie(hub)
        form = _current_form(hub, cookie)
        start = _revision(hub)
        form.pop("seat.cursor_grok")
        form.pop("seat.coder")
        form.pop("cloud.codex", None)
        form["cloud.claude"] = "1"
        form["role.impl"] = "agy_flash"
        form["role.review"] = ""
        form["role.chef"] = ""
        form["role.security"] = "daybreak"
        form["default.brigade-run"] = "agy_flash"
        form["notes"] = "cursor via Other Models only"
        status, headers, _text = _form(hub, form, cookie=cookie)
        assert status == 303
        assert headers["location"] == f"/deck/roster?saved={start + 1}"
        assert _revision(hub) == start + 1
        _status, _headers, models = _request(hub, "GET", "/models", headers=_bearer())
        roster = json.loads(models)
        enabled = {row["seat"]: row["enabled"] for row in roster["seats"]}
        assert enabled == {"agy_flash": True, "coder": False, "daybreak": True, "cursor_grok": False}
        assert roster["consumer_defaults"]["brigade-run"] == "agy_flash"
        _status, _headers, pref = _request(hub, "GET", "/preference", headers=_bearer())
        preference = json.loads(pref)["preference"]
        assert preference["impl"] == "agy_flash" and preference["security"] == "daybreak"
        assert preference.get("review") is None and preference["notes"] == "cursor via Other Models only"
        assert None not in preference.values()
        _status, _headers, cloud = _request(hub, "GET", "/cloud", headers=_bearer())
        providers = {row["provider"]: row["enabled"] for row in json.loads(cloud)["policy"]["providers"]}
        assert providers["claude"] is True and providers["codex"] is False
        page = _request(hub, "GET", f"/deck/roster?saved={start + 1}", headers={"Cookie": cookie})[2]
        assert f"saved as revision {start + 1}" in page
        assert "by deck-form" in page
        conn = sqlite3.connect(db)
        assert conn.execute("SELECT updated_by FROM run_preference WHERE id=1").fetchone()[0] == "deck-form"
        conn.close()
        # A no-op save leaves the revision alone.
        again = _current_form(hub, cookie)
        status, headers, _text = _form(hub, again, cookie=cookie)
        assert status == 303 and headers["location"] == f"/deck/roster?saved={start + 1}"
        assert _revision(hub) == start + 1


def test_roster_post_rejects_role_on_seat_disabled_in_same_save(tmp_path):
    with _hub(tmp_path) as (hub, db):
        _seed(hub)
        cookie = _login_cookie(hub)
        form = _current_form(hub, cookie)
        form.pop("seat.daybreak")
        form["role.security"] = "daybreak"
        before = _tables(db)
        status, _headers, page = _form(hub, form, cookie=cookie)
        assert status == 422
        assert "role security names seat daybreak" in page
        assert 'name="role.security"' in page and '<option value="daybreak" selected' in page
        assert _tables(db) == before
        # A consumer default needs the consumer's binding.
        form = _current_form(hub, cookie)
        form["default.t3-fleet"] = "agy_flash"
        status, _headers, page = _form(hub, form, cookie=cookie)
        assert status == 422 and "no t3-fleet binding" in page
        # Notes still go through the secret regexes.
        form = _current_form(hub, cookie)
        form["notes"] = "see keepass://roster for the real pins"
        status, _headers, page = _form(hub, form, cookie=cookie)
        assert status == 422 and "home paths" in page
        assert "keepass://roster" in page  # echoed back, escaped, so the operator can fix it
        assert _tables(db) == before


def test_roster_post_stale_cloud_lane_writes_nothing(tmp_path):
    with _hub(tmp_path) as (hub, db):
        _seed(hub)
        cookie = _login_cookie(hub)
        form = _current_form(hub, cookie)
        status, payload = _json(
            hub, "POST", "/cloud", {"action": "policy", "provider": "jules", "enabled": False, "reason": "quota"}
        )
        assert status == 200, payload
        before = _tables(db)
        status, _headers, page = _form(hub, {**form, "notes": "unrelated edit"}, cookie=cookie)
        assert status == 409
        assert "cloud lanes changed" in page
        assert _tables(db) == before
        # Reload, re-enable the lane on purpose: the stale reason is cleared.
        form = _current_form(hub, cookie)
        assert "cloud.jules" not in form
        form["cloud.jules"] = "1"
        status, _headers, _text = _form(hub, form, cookie=cookie)
        assert status == 303
        _status, _headers, cloud = _request(hub, "GET", "/cloud", headers=_bearer())
        jules = next(row for row in json.loads(cloud)["policy"]["providers"] if row["provider"] == "jules")
        assert jules["enabled"] is True and jules.get("reason") is None


# --- honest labelling and policy-authority compatibility ----------------------


def _view(db, *, activation=None):
    conn = fleet_hub.open_db(db)
    try:
        return fleet_hub_roster_page.load_view(conn, fleet_command_deck.DeckConfig(), activation=activation)
    finally:
        conn.close()


def _page(db, *, activation=None, editable=True, policy_csrf="policy-csrf"):
    view = _view(db, activation=activation)
    return fleet_hub_roster_page.render(
        view,
        nonce="nonce",
        now=datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc),
        csrf="csrf",
        editable=editable,
        policy_csrf=policy_csrf,
    )


POLICY_DOCUMENT = {
    "schema": fleet_policy.POLICY_SCHEMA,
    "defaults": {"roles": {"impl": "seat-alpha", "review": "seat-beta"}},
    "seats": {
        "seat-alpha": {"provider": "provider-a", "model": "model-a-1"},
        "seat-beta": {"provider": "provider-b", "model": "model-b-1"},
    },
    "consumers": {"brigade-run": {"reload": "refreshable"}, "t3-fleet": {"reload": "restart-required"}},
}


def _seed_policy(db) -> int:
    conn = fleet_hub.open_db(db)
    try:
        saved = fleet_hub_policy.save_policy(
            conn,
            POLICY_DOCUMENT,
            expected_version=fleet_hub_policy.current_policy(conn)["revision"],
            actor="operator",
            reason="roster page test seed",
        )
        return int(saved["revision"])
    finally:
        conn.close()


def _form_fields(page: str, marker: str) -> dict:
    """Every input/select value of the form containing ``marker``."""
    form = next(part for part in page.split("<form ")[1:] if marker in part).split("</form>", 1)[0]
    fields = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', form))
    for name, body in re.findall(r'<select name="([^"]+)"[^>]*>(.*?)</select>', form, re.S):
        selected = re.search(r'<option value="([^"]*)" selected', body)
        fields[name] = selected.group(1) if selected else ""
    return fields


def test_admission_fallback_section_explains_what_it_actually_does(tmp_path):
    with _hub(tmp_path) as (hub, db):
        _seed(hub)
        page = _page(db)
        assert "Admission fallback seat (legacy)" in page
        assert "Consumer defaults" not in page
        assert "without naming a seat" in page
        assert "not a model" in page
        assert "default-missing" in page
        assert "admission_default" in page
        assert 'href="/deck/policy"' in page


def test_roles_keep_their_keys_and_gain_labels(tmp_path):
    with _hub(tmp_path) as (hub, db):
        _seed(hub)
        page = _page(db)
        for role in fleet_hub_roster_page.ROLES:
            assert f'name="role.{role}"' in page
            assert f"({role})" in page
        assert "Worker (impl)" in page
        assert "Orchestrator (chef)" in page


def test_staged_policy_is_labelled_and_legacy_writes_still_work(tmp_path):
    staged = fleet_policy_page.Activation("staged", "not activated", "legacy-writable", "test")
    with _hub(tmp_path) as (hub, db):
        _seed(hub)
        page = _page(db, activation=staged)
        assert "staged policy" in page
        assert "still write the legacy run preference" in page
        assert "disabled" not in page.split('name="role.impl"')[1].split("</select>")[0]


def test_active_authority_keeps_the_dropdowns_usable_on_the_policy_document(tmp_path):
    """Active authority moves where a dropdown writes, not whether it is usable.

    Replaces the earlier read-only expectation: the approved requirement is
    that the existing dropdown UX keeps working and routes through the
    authoritative preview, so the assertions below pin the canonical form
    action, the canonical field names, and preview-only controls.
    """
    active = fleet_policy_page.Activation("active", "activated", "authority-owned", "test")
    with _hub(tmp_path) as (hub, db):
        _seed(hub)
        revision = _seed_policy(db)
        page = _page(db, activation=active)
        assert "staged policy" not in page
        assert "owned by the fleet policy document" in page
        assert 'href="/deck/policy"' in page

        roles = _form_fields(page, 'name="field.role_impl"')
        assert roles["scope"] == "defaults"
        assert roles["expected_version"] == str(revision)
        assert roles["csrf"] == "policy-csrf"
        assert roles["field.role_impl"] == "seat-alpha"
        assert "disabled" not in page.split('name="field.role_impl"')[1].split("</select>")[0]

        admission = _form_fields(page, f'name="field.role_{fleet_policy_page.ADMISSION_ROLE}"')
        assert admission["scope"] == "consumer"
        assert admission["target"] == "brigade-run"
        assert f"field.role_{fleet_policy_page.ADMISSION_ROLE}" in admission

        # Preview only: the confirm-save step belongs to the policy page.
        assert page.count('action="/deck/policy" class="roster-form"') == 3
        assert 'value="preview"' in page
        assert 'name="action" value="save"' not in page
        # Seat toggles still belong to the legacy roster form.
        assert 'action="/deck/roster"' in page


def test_active_authority_dropdown_edits_reach_the_policy_preview_and_save(tmp_path):
    active = fleet_policy_page.Activation("active", "activated", "authority-owned", "test")
    with _hub(tmp_path) as (hub, db):
        _seed(hub)
        revision = _seed_policy(db)
        cookie = _login_cookie(hub)
        page = _page(db, activation=active, policy_csrf=fleet_policy_page.csrf_value(TOKEN))
        fields = _form_fields(page, 'name="field.role_impl"')
        fields["field.role_impl"] = "seat-beta"
        fields["action"] = "preview"

        status, _headers, previewed = _request(
            hub,
            "POST",
            "/deck/policy",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Sec-Fetch-Site": "same-origin",
                "Cookie": cookie,
            },
            body=urlencode(fields).encode(),
        )
        assert status == 200
        assert "defaults.roles.impl" in previewed
        assert "Confirm save" in previewed
        conn = fleet_hub.open_db(db)
        try:
            assert fleet_hub_policy.current_policy(conn)["revision"] == revision
        finally:
            conn.close()

        confirm = next(part for part in previewed.split("<form ")[1:] if "confirm-save" in part).split("</form>")[0]
        status, headers, _text = _request(
            hub,
            "POST",
            "/deck/policy",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Sec-Fetch-Site": "same-origin",
                "Cookie": cookie,
            },
            body=urlencode(dict(re.findall(r'name="([^"]+)" value="([^"]*)">', confirm))).encode(),
        )
        assert status == 303
        assert headers["location"] == f"/deck/policy?saved={revision + 1}"
        conn = fleet_hub.open_db(db)
        try:
            current = fleet_hub_policy.current_policy(conn)
            assert current["document"]["defaults"]["roles"]["impl"] == "seat-beta"
        finally:
            conn.close()


def test_active_authority_leaves_the_legacy_roster_post_refused(tmp_path):
    """The dropdowns moving to policy must not re-open the legacy write path."""
    active = fleet_policy_page.Activation("active", "activated", "authority-owned", "test")
    with _hub(tmp_path) as (hub, db):
        _seed(hub)
        cookie = _login_cookie(hub)
        fields = _current_form(hub, cookie)
        fields["role.impl"] = "agy_flash"
        submission = fleet_hub_roster_page.parse_form(urlencode(fields).encode())
        conn = fleet_hub.open_db(db)
        try:
            result = fleet_hub_roster_page.apply(conn, fleet_command_deck.DeckConfig(), submission, activation=active)
        finally:
            conn.close()
        assert result.status == "invalid"
        assert "policy page" in result.message
        conn = fleet_hub.open_db(db)
        try:
            assert fleet_hub_preference.get_run_preference(conn)["impl"] == "coder"
        finally:
            conn.close()


def test_active_authority_refuses_a_legacy_role_write(tmp_path):
    active = fleet_policy_page.Activation("active", "activated", "authority-owned", "test")
    with _hub(tmp_path) as (hub, db):
        _seed(hub)
        cookie = _login_cookie(hub)
        fields = _current_form(hub, cookie)
        fields["role.impl"] = "agy_flash"
        submission = fleet_hub_roster_page.parse_form(urlencode(fields).encode())
        conn = fleet_hub.open_db(db)
        try:
            result = fleet_hub_roster_page.apply(conn, fleet_command_deck.DeckConfig(), submission, activation=active)
        finally:
            conn.close()
        assert result.status == "invalid"
        assert "policy page" in result.message
        conn = fleet_hub.open_db(db)
        try:
            assert fleet_hub_preference.get_run_preference(conn)["impl"] == "coder"
        finally:
            conn.close()


def test_authority_refusal_is_scoped_to_roles_and_the_admission_fallback(tmp_path):
    """The page-level refusal covers what this form writes to run preference.

    Whether a seat toggle survives activation is the migration module's call
    (``refuse_legacy_write``), not this page's; the page must not invent a
    second, looser answer for the fields it does own.
    """
    active = fleet_policy_page.Activation("active", "activated", "authority-owned", "test")
    with _hub(tmp_path) as (hub, db):
        _seed(hub)
        cookie = _login_cookie(hub)
        fields = _current_form(hub, cookie)
        unchanged = fleet_hub_roster_page.parse_form(urlencode(fields).encode())
        changed_role = fleet_hub_roster_page.parse_form(urlencode({**fields, "role.impl": "agy_flash"}).encode())
        changed_default = fleet_hub_roster_page.parse_form(
            urlencode({**fields, "default.brigade-run": "agy_flash"}).encode()
        )
        view = _view(db, activation=active)
        assert fleet_hub_roster_page._authority_refusal(view, unchanged) is None
        assert "policy page" in fleet_hub_roster_page._authority_refusal(view, changed_role)
        assert "admission_default" in fleet_hub_roster_page._authority_refusal(view, changed_default)


def test_roster_apply_without_activation_keeps_legacy_behaviour(tmp_path):
    with _hub(tmp_path) as (hub, db):
        _seed(hub)
        cookie = _login_cookie(hub)
        fields = _current_form(hub, cookie)
        fields["role.impl"] = "agy_flash"
        submission = fleet_hub_roster_page.parse_form(urlencode(fields).encode())
        conn = fleet_hub.open_db(db)
        try:
            result = fleet_hub_roster_page.apply(conn, fleet_command_deck.DeckConfig(), submission)
        finally:
            conn.close()
        assert result.status == "saved", result.message
        conn = fleet_hub.open_db(db)
        try:
            assert fleet_hub_preference.get_run_preference(conn)["impl"] == "agy_flash"
        finally:
            conn.close()


def test_the_saved_banner_states_what_the_save_did_to_runtime(tmp_path):
    """The redirect target says a revision landed; the banner says what that means."""
    with _hub(tmp_path) as (hub, db):
        _seed(hub)
        revision = _seed_policy(db)
        cookie = _login_cookie(hub)
        status, _headers, page = _request(hub, "GET", f"/deck/policy?saved={revision}", headers={"Cookie": cookie})
        assert status == 200
        assert f"saved as revision {revision}" in page
        # The migration reports a fresh hub as not activated, so the banner
        # must say the revision is staged rather than imply it took effect.
        assert "The policy authority is not activated" in page
        assert "does not change runtime routing" in page
        assert "reloaded" not in page


def test_the_roster_page_renders_no_em_dash(tmp_path):
    active = fleet_policy_page.Activation("active", "activated", "authority-owned", "test")
    with _hub(tmp_path) as (hub, db):
        _seed(hub)
        _seed_policy(db)
        page = _page(db, activation=active, policy_csrf=fleet_policy_page.csrf_value(TOKEN))
        assert "&mdash;" not in page
        assert "—" not in page


def test_a_read_only_identity_gets_no_policy_write_control(tmp_path):
    active = fleet_policy_page.Activation("active", "activated", "authority-owned", "test")
    with _hub(tmp_path) as (hub, db):
        _seed(hub)
        _seed_policy(db)
        page = _page(db, activation=active, editable=False, policy_csrf=fleet_policy_page.csrf_value(TOKEN))
        assert "read-only" in page
        # The authoritative dropdowns are an editor affordance, so a read-only
        # identity is not offered a preview it cannot submit.
        assert 'action="/deck/policy"' not in page
