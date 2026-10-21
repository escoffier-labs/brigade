"""Real stdlib HTTP forms exercise the operator trust boundary."""

import html
import http.client
import json
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.parse import urlencode

import pytest

from brigade import fleet_hub
from tests.test_fleet_actions import setup_action

LOGIN = "operator@example.test"
_POSIX_BROKER = pytest.mark.skipif(
    os.name != "posix", reason="Broker execution requires POSIX private files, process groups, and systemd"
)


@contextmanager
def action_hub(tmp_path):
    conn, path, grant, request = setup_action(tmp_path)
    conn.close()
    server = fleet_hub.make_server(
        "127.0.0.1",
        0,
        path,
        "fixture-admin",  # content-guard: allow loopback-ipv4
        trust_tailscale_identity=True,
        trust_forwarded_proto=True,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address, path, grant, request
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def request_http(address, path, *, data=None, headers=None):
    connection = http.client.HTTPConnection(*address, timeout=5)
    defaults = {"Tailscale-User-Login": LOGIN}
    if data is not None:
        defaults.update(
            {
                "Origin": "http://" + address[0] + ":" + str(address[1]),
                "Sec-Fetch-Site": "same-origin",
                "Content-Type": "application/x-www-form-urlencoded",
            }
        )
    connection.request(
        "POST" if data is not None else "GET",
        path,
        body=urlencode(data) if data is not None else None,
        headers={**defaults, **(headers or {})},
    )
    response = connection.getresponse()
    result = response.status, dict(response.getheaders()), response.read().decode()
    connection.close()
    return result


def fields(body):
    result = {name: html.unescape(value) for name, value in re.findall(r'name="([^"]+)" value="([^"]*)"', body)}
    for name, content in re.findall(r'<select name="([^"]+)">(.*?)</select>', body, re.S):
        options = re.findall(r'<option value="([^"]*)"([^>]*)>', content)
        selected = next((value for value, attrs in options if "selected" in attrs), options[0][0] if options else "")
        result[name] = html.unescape(selected)
    return result


def enable(address):
    code, headers, body = request_http(address, "/deck/actions/enable")
    assert code == 200
    cookie = headers["Set-Cookie"].split(";", 1)[0]
    code, headers, _ = request_http(address, "/deck/actions/enable", data=fields(body), headers={"Cookie": cookie})
    assert code == 303
    assert "HttpOnly" in headers["Set-Cookie"] and "SameSite=Strict" in headers["Set-Cookie"]
    return headers["Set-Cookie"].split(";", 1)[0]


def test_real_forms_explicit_activation_and_escaped_prompt(tmp_path):
    with action_hub(tmp_path) as (address, path, grant, request):
        url = "/deck/actions/compose?" + urlencode({"source": json.dumps(request["source"])})
        code, _, body = request_http(address, url)
        assert code == 200 and "Enable operator access" in body
        assert request_http(address, "/deck/actions/submit", data=request)[0] == 403
        cookie = enable(address)
        code, headers, body = request_http(address, url, headers={"Cookie": cookie})
        assert code == 200 and "form-action 'self'" in headers["Content-Security-Policy"]
        submitted = fields(body)
        submitted.update(prompt=request["prompt"], target_id="repair", model_id="")
        code, headers, _ = request_http(address, "/deck/actions/submit", data=submitted, headers={"Cookie": cookie})
        assert code == 303
        location = headers["Location"]
        code, _, body = request_http(address, location, headers={"Cookie": cookie})
        assert code == 200 and "&lt;script&gt;bad()&lt;/script&gt;" in body and "雪" in body
        conn = fleet_hub.open_db(path)
        assert conn.execute("SELECT count(*) FROM agent_actions").fetchone()[0] == 1
        conn.close()


@pytest.mark.parametrize(
    "failure", [fleet_hub.FleetHubError, KeyError, TypeError, ValueError, sqlite3.OperationalError]
)
def test_deck_optional_action_links_fail_without_losing_read_only_page(tmp_path, monkeypatch, failure):
    from brigade import fleet_action_auth as auth, fleet_action_targets as targets
    from tests.test_fleet_sync import _event

    calls = []

    def broken_links(conn, source_refs):
        calls.append(source_refs)
        raise failure("fixture optional policy failure")

    with action_hub(tmp_path) as (address, path, _, _):
        conn = fleet_hub.open_db(path)
        source = {"kind": "run", "node_id": "node-fixture", "run_id": "failed-run"}
        fleet_hub.store_events(
            conn,
            [
                {
                    **_event(node="node-fixture", run_id="failed-run", state="run.failed"),
                    "ts": datetime.now(timezone.utc).isoformat(),
                }
            ],
            caller_node="node-fixture",
        )
        target = json.loads(conn.execute("SELECT document FROM action_targets").fetchone()[0])
        auth.grant_broker(conn, "node-fixture", ["repair"], [source])
        targets.sync(conn, "node-fixture", [{**target, "subjects": [source]}])
        conn.close()
        code, _, body = request_http(address, "/deck")
        assert code == 200 and "Have an agent fix this" in body
        monkeypatch.setattr(targets, "linked_subjects", broken_links)
        code, _, body = request_http(address, "/deck")
        assert len(calls) == 1
        assert auth.canonical(source) in calls[0]
        if failure is sqlite3.OperationalError:
            assert code == 500 and "hub database error" in body
        else:
            assert code == 200 and "Command Deck" in body
            assert "Have an agent fix this" not in body and "/deck/actions/compose?" not in body


@pytest.mark.parametrize(
    "bad_headers",
    [
        {"Origin": ""},
        {"Origin": "http://elsewhere.test"},
        {"Origin": "null"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Cookie": ""},
        {"Tailscale-User-Login": "viewer@example.test"},
    ],
)
def test_enable_refuses_wrong_origin_or_identity(tmp_path, bad_headers):
    with action_hub(tmp_path) as (address, _, _, _):
        _, headers, body = request_http(address, "/deck/actions/enable")
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        assert (
            request_http(address, "/deck/actions/enable", data=fields(body), headers={"Cookie": cookie, **bad_headers})[
                0
            ]
            == 403
        )


def test_submit_csrf_revocation_and_stale_review_preserve_edit(tmp_path):
    from brigade import fleet_action_auth as auth

    with action_hub(tmp_path) as (address, path, grant, request):
        cookie = enable(address)
        url = "/deck/actions/compose?" + urlencode({"source": json.dumps(request["source"])})
        _, _, body = request_http(address, url, headers={"Cookie": cookie})
        submission = {**fields(body), "prompt": "my edited diagnosis", "target_id": "repair", "model_id": ""}
        assert (
            request_http(
                address, "/deck/actions/submit", data={**submission, "csrf": "wrong"}, headers={"Cookie": cookie}
            )[0]
            == 403
        )
        conn = fleet_hub.open_db(path)
        with conn:
            conn.execute("UPDATE fleet_machine_telemetry SET credential_state='error'")
        code, _, body = request_http(address, "/deck/actions/submit", data=submission, headers={"Cookie": cookie})
        assert code == 409 and "my edited diagnosis" in body and submission["client_id"] in body
        auth.revoke_operator(conn, grant)
        assert request_http(address, "/deck/actions/submit", data=submission, headers={"Cookie": cookie})[0] == 403
        conn.close()


def test_non_admin_show_requires_live_broker_grant_after_start(tmp_path):
    from brigade import fleet_action_auth as auth, fleet_action_http as api, fleet_hub_actions as actions
    from brigade.fleet_action_auth import ActionError

    conn, _, grant, request = setup_action(tmp_path)
    action = actions.submit(conn, grant, request)
    claimed = actions.claim(conn, "node-fixture")
    actions.start(conn, "node-fixture", action["action_id"], claimed["generation"])
    before = api.handle_api(conn, (False, "node-fixture"), {"op": "show", "action_id": action["action_id"]})
    assert before["request"]["prompt"] == request["prompt"] and before["events"]
    fleet_hub.add_node(conn, "other-node")
    with pytest.raises(ActionError, match="broker-scope-refused"):
        api.handle_api(conn, (False, "other-node"), {"op": "show", "action_id": action["action_id"]})
    auth.revoke_broker(conn, "node-fixture")
    with pytest.raises(ActionError, match="enrolled-broker-required"):
        api.handle_api(conn, (False, "node-fixture"), {"op": "show", "action_id": action["action_id"]})
    assert (
        api.handle_api(conn, (True, None), {"op": "show", "action_id": action["action_id"]})["action_id"]
        == action["action_id"]
    )
    conn.close()


@pytest.mark.parametrize("narrow", ["target", "subject"])
def test_non_admin_show_obeys_current_broker_scope_after_replacement(tmp_path, narrow):
    from brigade import fleet_action_auth as auth, fleet_action_http as api, fleet_hub_actions as actions
    from brigade.fleet_action_auth import ActionError

    conn, _, grant, request = setup_action(tmp_path)
    action = actions.submit(conn, grant, request)
    claimed = actions.claim(conn, "node-fixture")
    actions.start(conn, "node-fixture", action["action_id"], claimed["generation"])
    source = request["source"]
    auth.grant_broker(
        conn,
        "node-fixture",
        ["elsewhere"] if narrow == "target" else ["repair"],
        [{"kind": "telemetry", "node_id": "other-node"}] if narrow == "subject" else [source],
    )
    with pytest.raises(ActionError, match="broker-scope-refused"):
        api.handle_api(conn, (False, "node-fixture"), {"op": "show", "action_id": action["action_id"]})
    assert (
        api.handle_api(conn, (True, None), {"op": "show", "action_id": action["action_id"]})["action_id"]
        == action["action_id"]
    )
    conn.close()


@_POSIX_BROKER
def test_form_to_broker_fake_destination_and_receipt(tmp_path, monkeypatch):
    from brigade import fleet_action_runner as runner, fleet_client_actions as client
    from tests.test_fleet_action_runner import fake_executable, manifest

    with action_hub(tmp_path) as (address, path, _, submission):
        conn = fleet_hub.open_db(path)
        # Node credentials are generated by the real enrollment path, then used only on loopback.
        fleet_hub.revoke_node(conn, "node-fixture")
        _, owner_token = fleet_hub.add_node(conn, "node-fixture")
        target = json.loads(conn.execute("SELECT document FROM action_targets").fetchone()[0])
        exe, log = fake_executable(tmp_path, {"status": "ok", "runId": "fixture-flow-turn"})
        manifest_path = manifest(tmp_path, exe, target)
        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", f"http://{address[0]}:{address[1]}")
        monkeypatch.setenv("BRIGADE_FLEET_NODE_TOKEN", owner_token)
        monkeypatch.setenv("BRIGADE_FLEET_TOKEN", "fixture-admin")
        runner.sync_targets(manifest_path)
        cookie = enable(address)
        url = "/deck/actions/compose?" + urlencode({"source": json.dumps(submission["source"])})
        _, _, body = request_http(address, url, headers={"Cookie": cookie})
        post = {**fields(body), "prompt": "Investigate fixture", "model_id": ""}
        code, headers, _ = request_http(address, "/deck/actions/submit", data=post, headers={"Cookie": cookie})
        assert code == 303
        action_id = headers["Location"].rsplit("/", 1)[1]
        assert runner.tick(manifest_path)["scheduled"] == 1
        action = client.request({"op": "show", "action_id": action_id})
        runner.execute(manifest_path, action_id, action["generation"])
        code, _, body = request_http(address, headers["Location"], headers={"Cookie": cookie})
        assert code == 200 and "Finished, verification pending" in body and "fixture-flow-turn" in body
        assert any(json.loads(line)["args"][:1] == ["agent"] for line in log.read_text().splitlines())
        conn.close()


def test_https_cookie_duplicate_login_and_bearer_do_not_bypass_operator(tmp_path):
    with action_hub(tmp_path) as (address, _, _, _):
        _, headers, _ = request_http(address, "/deck/actions/enable", headers={"X-Forwarded-Proto": "https"})
        assert "; Secure" in headers["Set-Cookie"]
        assert (
            request_http(
                address,
                "/deck/actions/enable",
                headers={"Tailscale-User-Login": "", "Authorization": "Bearer fixture-admin"},
            )[0]
            == 403
        )
        connection = http.client.HTTPConnection(*address, timeout=5)
        connection.putrequest("GET", "/deck/actions/enable")
        connection.putheader("Tailscale-User-Login", LOGIN)
        connection.putheader("Tailscale-User-Login", LOGIN)
        connection.endheaders()
        response = connection.getresponse()
        assert response.status == 403
        response.read()
        connection.close()


def test_unicode_prompt_within_byte_limit_survives_form_encoding(tmp_path):
    with action_hub(tmp_path) as (address, _, _, submission):
        cookie = enable(address)
        _, _, body = request_http(
            address,
            "/deck/actions/compose?" + urlencode({"source": json.dumps(submission["source"])}),
            headers={"Cookie": cookie},
        )
        post = {**fields(body), "prompt": "雪" * 3500, "model_id": ""}
        assert request_http(address, "/deck/actions/submit", data=post, headers={"Cookie": cookie})[0] == 303


def test_action_forms_preserve_same_origin_for_browser_post(tmp_path):
    with action_hub(tmp_path) as (address, _, _, _):
        code, headers, _ = request_http(address, "/deck/actions/enable")
        assert code == 200
        assert headers["Referrer-Policy"] == "same-origin"


def test_client_id_conflict_preserves_edit_and_requires_explicit_new_submit(tmp_path):
    with action_hub(tmp_path) as (address, path, _, request):
        cookie = enable(address)
        url = "/deck/actions/compose?" + urlencode({"source": json.dumps(request["source"])})
        _, _, body = request_http(address, url, headers={"Cookie": cookie})
        original = {**fields(body), "prompt": "first diagnosis", "model_id": ""}
        assert request_http(address, "/deck/actions/submit", data=original, headers={"Cookie": cookie})[0] == 303
        edited = {**original, "prompt": "edited diagnosis after back button"}
        code, _, body = request_http(address, "/deck/actions/submit", data=edited, headers={"Cookie": cookie})
        assert code == 409 and edited["prompt"] in body
        replacement = fields(body)
        assert replacement["client_id"] != original["client_id"]
        conn = fleet_hub.open_db(path)
        assert conn.execute("SELECT count(*) FROM agent_actions").fetchone()[0] == 1
        assert (
            request_http(
                address,
                "/deck/actions/submit",
                data={**replacement, "prompt": edited["prompt"], "model_id": ""},
                headers={"Cookie": cookie},
            )[0]
            == 303
        )
        assert conn.execute("SELECT count(*) FROM agent_actions").fetchone()[0] == 2
        conn.close()
