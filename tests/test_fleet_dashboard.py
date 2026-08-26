"""Fleet sync phase 3 tests (issue #1124): hub-served web Fleet dashboard."""

from __future__ import annotations

import argparse
import http.client
import json
import threading
from datetime import datetime, timedelta, timezone

import pytest

from brigade import fleet_dashboard, fleet_hub

NODE_A = "11111111-1111-4111-8111-111111111111"
NODE_B = "22222222-2222-4222-8222-222222222222"
TOKEN = "test-token-12345"
HOLDER_TOKEN = "secret-holder-token-zz"


@pytest.fixture()
def hub(tmp_path):
    db = tmp_path / "hub" / "fleet.db"
    # Legacy shared-token mode (pre-#1150): the one token posts as any node.
    server = fleet_hub.make_server("127.0.0.1", 0, db, TOKEN, allow_admin_writes=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield ("127.0.0.1", server.server_address[1])
    server.shutdown()
    server.server_close()


def _request(hub, method: str, path: str, *, headers: dict | None = None, body=None):
    """Raw request so redirects are not followed and Set-Cookie is visible."""
    host, port = hub
    conn = http.client.HTTPConnection(host, port, timeout=5)
    data = json.dumps(body).encode() if body is not None else None
    conn.request(method, path, body=data, headers=headers or {})
    response = conn.getresponse()
    text = response.read().decode("utf-8")
    result = (response.status, {k.lower(): v for k, v in response.getheaders()}, text)
    conn.close()
    return result


def _bearer(extra: dict | None = None) -> dict:
    return {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json", **(extra or {})}


def _event(node: str, run_id: str, seq: int, state: str, *, repo: str, harness: str, ts: str) -> dict:
    return {
        "node_id": node,
        "run_id": run_id,
        "repo": repo,
        "seat": "worker",
        "harness": harness,
        "state": state,
        "ts": ts,
        "sequence": seq,
        "digest": f"d{seq}",
    }


def _seed(hub) -> None:
    """Two machines, a running run, a paused run, a finished run, and one claim held by B on A's repo."""
    now = datetime.now(timezone.utc)
    ts_new = now.isoformat()
    ts_old = (now - timedelta(minutes=5)).isoformat()
    events = [
        _event(NODE_A, "runalpha", 1, "run.created", repo="repo-a", harness="claude", ts=ts_old),
        _event(NODE_A, "runalpha", 2, "run.dispatch.observed", repo="repo-a", harness="claude", ts=ts_new),
        _event(NODE_B, "runbravo", 1, "run.created", repo="repo-b", harness="codex", ts=ts_old),
        _event(NODE_B, "runbravo", 2, "run.paused", repo="repo-b", harness="codex", ts=ts_new),
        _event(NODE_A, "runcharlie", 1, "run.created", repo="repo-c", harness="claude", ts=ts_old),
        _event(NODE_A, "runcharlie", 2, "run.completed", repo="repo-c", harness="claude", ts=ts_new),
    ]
    status, _headers, text = _request(hub, "POST", "/events", headers=_bearer(), body=events)
    assert status == 200, text
    claim = {"action": "acquire", "target": "repo-a", "node_id": NODE_B, "holder": HOLDER_TOKEN, "conductor": "orch"}
    status, _headers, text = _request(hub, "POST", "/claims", headers=_bearer(), body=claim)
    assert status == 200, text


def _login_cookie(hub) -> str:
    status, headers, _text = _request(hub, "GET", f"/view/machines?token={TOKEN}")
    assert status == 303
    return headers["set-cookie"].split(";")[0]


class TestDashboardAuth:
    def test_requires_bearer_or_cookie(self, hub):
        for path in ("/view/machines", "/view/repos"):
            status, headers, text = _request(hub, "GET", path)
            assert status == 401, path
            assert "set-cookie" not in headers
            assert TOKEN not in text
        status, _headers, _text = _request(hub, "GET", "/", headers={"Authorization": "Bearer nope"})
        assert status == 401
        status, _headers, _text = _request(hub, "GET", "/", headers={"Cookie": f"{fleet_hub.DASHBOARD_COOKIE}=junk"})
        assert status == 401

    def test_bearer_renders_html_with_security_headers(self, hub):
        status, headers, text = _request(hub, "GET", "/view/machines", headers=_bearer())
        assert status == 200
        assert headers["content-type"].startswith("text/html")
        assert "nonce-" in headers["content-security-policy"]
        assert "frame-ancestors 'none'" in headers["content-security-policy"]
        assert headers["cache-control"] == "no-store"
        assert headers["referrer-policy"] == "no-referrer"
        assert "<!doctype html>" in text
        assert '<meta http-equiv="refresh" content="10">' in text

    def test_unknown_view_is_404(self, hub):
        status, _headers, _text = _request(hub, "GET", "/view/nope", headers=_bearer())
        assert status == 404
        status, _headers, _text = _request(hub, "GET", "/view/", headers=_bearer())
        assert status == 404

    def test_token_query_sets_httponly_cookie_and_redirects_without_token(self, hub):
        status, headers, text = _request(hub, "GET", f"/view/repos?token={TOKEN}&sort=node&repo=a")
        assert status == 303
        assert headers["location"] == "/view/repos?sort=node&repo=a"
        assert "token" not in headers["location"]
        cookie = headers["set-cookie"]
        assert cookie.startswith(f"{fleet_hub.DASHBOARD_COOKIE}=")
        assert "HttpOnly" in cookie
        assert "SameSite=Strict" in cookie
        assert "Path=/" in cookie
        value = cookie.split(";")[0].split("=", 1)[1]
        assert value != TOKEN
        assert TOKEN not in cookie
        assert value == fleet_hub.dashboard_cookie_value(TOKEN)
        assert TOKEN not in text

    def test_wrong_token_query_sets_no_cookie(self, hub):
        status, headers, _text = _request(hub, "GET", "/view/machines?token=wrong")
        assert status == 401
        assert "set-cookie" not in headers

    def test_cookie_grants_dashboard_only(self, hub):
        cookie = _login_cookie(hub)
        status, _headers, text = _request(hub, "GET", "/view/machines", headers={"Cookie": cookie})
        assert status == 200
        assert "Fleet: Machines" in text
        status, _headers, _text = _request(hub, "GET", "/view/repos", headers={"Cookie": cookie})
        assert status == 200
        # The cookie is a read-only capability: JSON and write endpoints still need the token.
        assert _request(hub, "GET", "/status", headers={"Cookie": cookie})[0] == 401
        assert _request(hub, "GET", "/claims", headers={"Cookie": cookie})[0] == 401
        status, _headers, _text = _request(
            hub, "POST", "/events", headers={"Cookie": cookie, "Content-Type": "application/json"}, body=[]
        )
        assert status == 401

    def test_cookie_is_derived_not_the_token(self):
        value = fleet_hub.dashboard_cookie_value(TOKEN)
        assert value != TOKEN and TOKEN not in value
        assert value != fleet_hub.dashboard_cookie_value("other-token")


class TestBoards:
    def test_machine_board_renders_seeded_events_and_claims(self, hub):
        _seed(hub)
        status, _headers, text = _request(hub, "GET", "/view/machines", headers=_bearer())
        assert status == 200
        assert f'data-node="{NODE_A}"' in text
        assert f'data-node="{NODE_B}"' in text
        assert "runalpha" in text
        assert "runbravo" in text
        assert "runcharlie" not in text  # terminal runs hidden by default
        assert "awaiting approval" in text
        assert "run.paused" in text
        assert "2 live runs, 2 machines, 1 needs attention, 1 claim held." in text
        # Node B holds the claim on repo-a: owner short id, conductor, TTL remaining.
        assert "holds" in text
        assert "repo-a: 22222222-222 · orch ·" in text
        assert "left" in text
        assert "last event" in text
        assert 'data-since="' in text  # live elapsed ticker for running rows

    def test_include_finished_shows_terminal_runs(self, hub):
        _seed(hub)
        _status, _headers, text = _request(hub, "GET", "/view/machines?all=1", headers=_bearer())
        assert "runcharlie" in text
        assert "succeeded" in text

    def test_repo_board_renders_running_where_claims_and_outcome(self, hub):
        _seed(hub)
        status, _headers, text = _request(hub, "GET", "/view/repos", headers=_bearer())
        assert status == 200
        assert "Fleet: Repos" in text
        assert 'id="repo-board"' in text
        rows = text.split("<tr")[2:]  # skip head row
        by_repo = {row.split("<td>")[1].split("</td>")[0]: row for row in rows}
        assert set(by_repo) == {"repo-a", "repo-b", "repo-c"}
        repo_a = by_repo["repo-a"]
        assert "11111111-111" in repo_a and "worker/claude" in repo_a and "running" in repo_a
        assert "22222222-222 · orch ·" in repo_a and "left" in repo_a
        assert "collision" in repo_a  # running on A, claimed by B
        assert 'data-collision="1"' in repo_a
        repo_b = by_repo["repo-b"]
        assert "awaiting approval" in repo_b and 'data-collision="0"' in repo_b
        repo_c = by_repo["repo-c"]
        assert "idle" in repo_c and "succeeded" in repo_c

    def test_empty_hub_renders(self, hub):
        _status, _headers, text = _request(hub, "GET", "/view/machines", headers=_bearer())
        assert "No fleet events recorded yet." in text
        _status, _headers, text = _request(hub, "GET", "/view/repos", headers=_bearer())
        assert "No repos match." in text


class TestSortAndFilter:
    def test_node_filter(self, hub):
        _seed(hub)
        _status, _headers, text = _request(hub, "GET", "/view/machines?node=1111", headers=_bearer())
        assert "runalpha" in text and "runbravo" not in text
        assert f'data-node="{NODE_B}"' not in text

    def test_attention_only(self, hub):
        _seed(hub)
        _status, _headers, text = _request(hub, "GET", "/view/machines?attention=1", headers=_bearer())
        assert "runbravo" in text and "runalpha" not in text
        _status, _headers, text = _request(hub, "GET", "/view/repos?attention=1", headers=_bearer())
        assert "repo-b" in text and "repo-c" not in text
        assert "repo-a" in text  # collision counts as needing attention

    def test_state_and_seat_filters(self, hub):
        _seed(hub)
        _status, _headers, text = _request(hub, "GET", "/view/machines?state=paused", headers=_bearer())
        assert "runbravo" in text and "runalpha" not in text
        _status, _headers, text = _request(hub, "GET", "/view/machines?state=running", headers=_bearer())
        assert "runalpha" in text and "runbravo" not in text
        _status, _headers, text = _request(hub, "GET", "/view/machines?seat=codex", headers=_bearer())
        assert "runbravo" in text and "runalpha" not in text

    def test_repo_filter_on_repo_board(self, hub):
        _seed(hub)
        _status, _headers, text = _request(hub, "GET", "/view/repos?repo=repo-b", headers=_bearer())
        assert "repo-b" in text and "repo-a" not in text.split("<tbody>")[1]

    def test_sort_orders_cards(self, hub):
        _seed(hub)
        # Default attention sort: the machine with an awaiting-approval run comes first.
        _status, _headers, text = _request(hub, "GET", "/view/machines", headers=_bearer())
        assert text.index(f'data-node="{NODE_B}"') < text.index(f'data-node="{NODE_A}"')
        _status, _headers, text = _request(hub, "GET", "/view/machines?sort=node", headers=_bearer())
        assert text.index(f'data-node="{NODE_A}"') < text.index(f'data-node="{NODE_B}"')
        assert '<option value="node" selected>' in text

    def test_sort_orders_repo_rows(self, hub):
        _seed(hub)
        _status, _headers, text = _request(hub, "GET", "/view/repos", headers=_bearer())
        body = text.split("<tbody>")[1]
        assert body.index("repo-a") < body.index("repo-b") < body.index("repo-c")  # collision, attention, idle
        _status, _headers, text = _request(hub, "GET", "/view/repos?sort=repo", headers=_bearer())
        body = text.split("<tbody>")[1]
        assert body.index("repo-a") < body.index("repo-b") < body.index("repo-c")
        _status, _headers, text = _request(hub, "GET", "/view/repos?sort=seat", headers=_bearer())
        body = text.split("<tbody>")[1]
        assert body.index("repo-a") < body.index("repo-b")  # worker/claude before worker/codex

    def test_filters_round_trip_in_links_and_form(self, hub):
        _seed(hub)
        _status, _headers, text = _request(
            hub, "GET", "/view/machines?sort=age&node=1111&attention=1", headers=_bearer()
        )
        assert 'href="/view/repos?sort=age&amp;node=1111&amp;attention=1"' in text
        assert 'href="/view/machines?sort=age&amp;node=1111&amp;attention=1"' in text
        assert 'name="node" value="1111"' in text
        assert 'name="attention" value="1" checked' in text


class TestNoSecretsInHtml:
    def test_no_token_cookie_or_holder_in_any_page(self, hub):
        _seed(hub)
        cookie = _login_cookie(hub)
        cookie_value = cookie.split("=", 1)[1]
        for path in ("/view/machines", "/view/repos", "/view/machines?all=1&sort=node"):
            for headers in (_bearer(), {"Cookie": cookie}):
                status, _headers, text = _request(hub, "GET", path, headers=headers)
                assert status == 200
                assert TOKEN not in text
                assert cookie_value not in text
                assert HOLDER_TOKEN not in text
                assert "Authorization" not in text

    def test_filter_values_are_escaped(self, hub):
        _seed(hub)
        _status, _headers, text = _request(hub, "GET", "/view/machines?node=%3Cscript%3Ex", headers=_bearer())
        assert "<script>x" not in text
        assert "&lt;script&gt;x" in text


def _asset_request(hub, path: str):
    """Raw bytes request for binary assets; does not decode as text."""
    host, port = hub
    conn = http.client.HTTPConnection(host, port, timeout=5)
    conn.request("GET", path, headers={})
    response = conn.getresponse()
    data = response.read()
    headers = {k.lower(): v for k, v in response.getheaders()}
    result = (response.status, headers, data)
    conn.close()
    return result


class TestAssets:
    def test_asset_routes_served_without_auth(self, hub):
        for path, expected_type in (
            ("/favicon.ico", "image/x-icon"),
            ("/favicon-32x32.png", "image/png"),
            ("/apple-touch-icon.png", "image/png"),
            ("/icon-192.png", "image/png"),
            ("/icon-512.png", "image/png"),
            ("/site.webmanifest", "application/manifest+json"),
        ):
            status, headers, body = _asset_request(hub, path)
            assert status == 200, path
            assert headers["content-type"].startswith(expected_type), path
            assert int(headers["content-length"]) == len(body), path
            assert headers["x-content-type-options"] == "nosniff", path
            assert "public" in headers["cache-control"], path

    def test_webmanifest_shape(self, hub):
        status, _headers, body = _asset_request(hub, "/site.webmanifest")
        assert status == 200
        manifest = json.loads(body.decode("utf-8"))
        assert manifest["name"] == "Brigade Fleet Hub"
        assert manifest["short_name"] == "Fleet Hub"
        assert manifest["start_url"] == "/"
        assert manifest["display"] == "standalone"
        assert manifest["theme_color"] == "#111617"
        assert manifest["background_color"] == "#182022"
        icons = {icon["sizes"]: icon for icon in manifest["icons"]}
        assert "192x192" in icons and "512x512" in icons
        for icon in manifest["icons"]:
            assert icon["purpose"] == "any maskable"
            assert icon["type"] == "image/png"

    def test_unknown_asset_like_paths_still_404(self, hub):
        for path in ("/favicon-64x64.png", "/site.webmanifest.json", "/assets/favicon.ico", "/icon-999.png"):
            status, _headers, _text = _request(hub, "GET", path)
            assert status == 404, path

    def test_legacy_pages_include_asset_metadata(self, hub):
        for path in ("/view/machines", "/view/repos"):
            status, _headers, body = _request(hub, "GET", path, headers=_bearer())
            assert status == 200, path
            assert '<link rel="icon" type="image/x-icon" href="/favicon.ico"' in body
            assert '<link rel="icon" type="image/png" sizes="32x32" href="/favicon-32x32.png"' in body
            assert '<link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png"' in body
            assert '<link rel="manifest" href="/site.webmanifest"' in body
            assert '<meta name="theme-color" content="#111617"' in body
            assert '<meta name="application-name" content="Fleet Hub"' in body
            assert '<meta name="apple-mobile-web-app-title" content="Fleet Hub"' in body

    def test_api_routes_still_require_auth(self, hub):
        for path in ("/status", "/claims", "/nodes", "/cloud", "/models"):
            assert _request(hub, "GET", path)[0] == 401, path


class TestPureRendering:
    def test_bucket_mapping(self):
        assert fleet_dashboard.bucket_for("run.completed", age_seconds=0) == "succeeded"
        assert fleet_dashboard.bucket_for("run.failed", age_seconds=0) == "failed"
        assert fleet_dashboard.bucket_for("run.dispatch.failed", age_seconds=0) == "failed"
        assert fleet_dashboard.bucket_for("run.interrupted", age_seconds=0) == "interrupted"
        assert fleet_dashboard.bucket_for("run.paused", age_seconds=0) == "awaiting approval"
        assert fleet_dashboard.bucket_for("approval.requested", age_seconds=0) == "awaiting approval"
        assert fleet_dashboard.bucket_for("run.created", age_seconds=0) == "queued"
        assert fleet_dashboard.bucket_for("run.dispatch.observed", age_seconds=0) == "running"
        assert fleet_dashboard.bucket_for("run.dispatch.observed", age_seconds=3600) == "stale"
        assert fleet_dashboard.bucket_for("run.paused", age_seconds=3600) == "awaiting approval"

    def test_parse_query_sanitises(self):
        query = fleet_dashboard.parse_query("sort=bogus&node=" + "x" * 500 + "&attention=yes&all=0", view="nope")
        assert query.view == "machines"
        assert query.sort == "attention"
        assert len(query.node) == 128
        assert query.attention_only is True
        assert query.include_all is False
        assert fleet_dashboard.parse_query("sort=Repo", view="repos").sort == "repo"

    def test_attention_sort_puts_failed_first_then_oldest(self):
        now = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
        runs = [
            {"node_id": "n", "run_id": "young-running", "state": "run.dispatch.observed", "ts": now.isoformat()},
            {"node_id": "n", "run_id": "old-running", "state": "run.dispatch.observed", "ts": now.isoformat()},
            {"node_id": "n", "run_id": "failed", "state": "run.dispatch.failed", "ts": now.isoformat()},
        ]
        started = {
            ("n", "young-running"): (now - timedelta(minutes=1)).isoformat(),
            ("n", "old-running"): (now - timedelta(hours=1)).isoformat(),
            ("n", "failed"): (now - timedelta(minutes=2)).isoformat(),
        }
        rows = fleet_dashboard.build_rows(runs, started, now=now)
        ordered = [row.run_id for row in fleet_dashboard.sort_rows(rows, "attention")]
        assert ordered == ["failed", "old-running", "young-running"]
        by_age = [row.run_id for row in fleet_dashboard.sort_rows(rows, "age")]
        assert by_age == ["old-running", "failed", "young-running"]

    def test_stale_run_needs_attention(self):
        now = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
        old = (now - timedelta(hours=2)).isoformat()
        rows = fleet_dashboard.build_rows(
            [{"node_id": "n", "run_id": "r", "state": "run.dispatch.observed", "ts": old}], {("n", "r"): old}, now=now
        )
        assert rows[0].bucket == "stale"
        assert fleet_dashboard.filter_rows(rows, fleet_dashboard.DashboardQuery(attention_only=True)) == rows

    def test_format_duration(self):
        assert fleet_dashboard.format_duration(None) == "-"
        assert fleet_dashboard.format_duration(42) == "42s"
        assert fleet_dashboard.format_duration(185) == "3m 05s"
        assert fleet_dashboard.format_duration(3600 * 2 + 60 * 14) == "2h 14m"
        assert fleet_dashboard.format_duration(86400 * 5 + 3600 * 3) == "5d 3h"

    def test_render_page_without_socket(self):
        page = fleet_dashboard.render_page(
            view="repos", query_string="", runs=[], claims=[], nodes=[], started_at={}, nonce="abc"
        )
        assert 'nonce="abc"' in page
        assert "Fleet: Repos" in page


TAILSCALE_USER = "tailscale-user@example.invalid"


@pytest.fixture()
def hub_tailscale(tmp_path):
    db = tmp_path / "hub" / "fleet.db"
    server = fleet_hub.make_server("127.0.0.1", 0, db, TOKEN, allow_admin_writes=True, trust_tailscale_identity=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield ("127.0.0.1", server.server_address[1])
    server.shutdown()
    server.server_close()


def _tailscale_headers(identity: str = TAILSCALE_USER, extra: dict | None = None) -> dict:
    headers = {fleet_hub._TAILSCALE_IDENTITY_HEADER: identity, **(extra or {})}
    return headers


class TestTailscaleIdentity:
    def test_default_off_rejects_tailscale_header(self, hub):
        for path in ("/", "/deck", "/deck/repos", "/view/machines", "/view/repos"):
            status, _headers, text = _request(hub, "GET", path, headers=_tailscale_headers())
            assert status == 401, path
            assert TAILSCALE_USER not in text

    def test_trusted_loopback_dashboard_success(self, hub_tailscale):
        for path in ("/", "/deck", "/deck/repos", "/view/machines", "/view/repos"):
            status, headers, text = _request(hub_tailscale, "GET", path, headers=_tailscale_headers())
            assert status == 200, path
            assert headers["content-type"].startswith("text/html")
            assert TAILSCALE_USER not in text

    def test_trusted_loopback_allows_bearer_and_cookie_unchanged(self, hub_tailscale):
        status, _headers, text = _request(hub_tailscale, "GET", "/deck", headers=_bearer())
        assert status == 200
        cookie = _login_cookie(hub_tailscale)
        status, _headers, text = _request(hub_tailscale, "GET", "/deck", headers={"Cookie": cookie})
        assert status == 200

    def test_missing_empty_invalid_identity_fails(self, hub_tailscale):
        for identity in (
            "",
            "   ",
            "a" * 257,
            "user@example\x01invalid.test",
            "user@example\x1cinvalid.test",
            "user@example\x7finvalid.test",
        ):
            status, _headers, text = _request(hub_tailscale, "GET", "/deck", headers=_tailscale_headers(identity))
            assert status == 401, repr(identity)
            if identity:
                assert identity not in text

    def test_missing_header_fails(self, hub_tailscale):
        status, _headers, _text = _request(hub_tailscale, "GET", "/deck")
        assert status == 401

    def test_non_loopback_peer_fails_even_with_valid_header(self, hub_tailscale, monkeypatch):
        monkeypatch.setattr(fleet_hub, "_is_loopback_address", lambda address: False)
        status, _headers, text = _request(hub_tailscale, "GET", "/deck", headers=_tailscale_headers())
        assert status == 401
        assert TAILSCALE_USER not in text

    def test_tailscale_identity_never_authorizes_api_routes(self, hub_tailscale):
        for path in ("/status", "/claims", "/nodes", "/cloud", "/models"):
            status, _headers, _text = _request(hub_tailscale, "GET", path, headers=_tailscale_headers())
            assert status == 401, path
        for path in ("/events", "/claims", "/nodes", "/cloud", "/models"):
            status, _headers, _text = _request(
                hub_tailscale,
                "POST",
                path,
                headers=_tailscale_headers(extra={"Content-Type": "application/json"}),
                body={},
            )
            assert status == 401, path

    def test_tailscale_identity_not_in_dashboard_body(self, hub_tailscale):
        _seed(hub_tailscale)
        for path in ("/", "/deck", "/deck/repos", "/view/machines", "/view/repos"):
            status, _headers, text = _request(hub_tailscale, "GET", path, headers=_tailscale_headers())
            assert status == 200, path
            assert TAILSCALE_USER not in text


class TestLoopbackAddressHelper:
    def test_is_loopback_address_ipv4(self):
        assert fleet_hub._is_loopback_address("127.0.0.1") is True

    def test_is_loopback_address_ipv6(self):
        assert fleet_hub._is_loopback_address("::1") is True

    def test_is_loopback_address_ipv4_mapped_ipv6(self):
        assert fleet_hub._is_loopback_address("::ffff:127.0.0.1") is True

    def test_is_loopback_address_non_loopback(self):
        assert fleet_hub._is_loopback_address("192.168.1.1") is False
        assert fleet_hub._is_loopback_address("::ffff:192.168.1.1") is False
        assert fleet_hub._is_loopback_address("not-an-address") is False


@pytest.mark.parametrize("host", ["0.0.0.0", "192.0.2.10", "::", "2001:db8::10", "localhost"])
def test_tailscale_identity_rejects_nonliteral_or_routable_bind(tmp_path, host):
    with pytest.raises(fleet_hub.FleetHubError, match="requires a loopback --host"):
        fleet_hub.make_server(
            host,
            0,
            tmp_path / "fleet.db",
            TOKEN,
            trust_tailscale_identity=True,
        )


def test_dispatch_serve_forwards_trust_tailscale_identity(monkeypatch):
    from brigade.cli.fleet import _dispatch_serve

    captured = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr("brigade.fleet_hub.run", fake_run)
    monkeypatch.setattr("brigade.fleet_command_deck.resolve_config_path", lambda path, env: None)
    args = argparse.Namespace(
        host="127.0.0.1",
        port=3774,
        db=None,
        token_file=None,
        allow_admin_writes=False,
        deck_config=None,
        trust_tailscale_identity=True,
    )
    assert _dispatch_serve(args) == 0
    assert captured["trust_tailscale_identity"] is True
    assert captured["host"] == "127.0.0.1"


def test_cli_serve_trust_tailscale_identity_defaults_off():
    from brigade import cli

    args = cli._build_parser().parse_args(["fleet", "serve", "--host", "127.0.0.1"])

    assert args.trust_tailscale_identity is False
