"""Fleet sync phase 3 tests (issue #1124): hub-served web Fleet dashboard."""

from __future__ import annotations

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
    status, headers, _text = _request(hub, "GET", f"/?token={TOKEN}")
    assert status == 303
    return headers["set-cookie"].split(";")[0]


class TestDashboardAuth:
    def test_requires_bearer_or_cookie(self, hub):
        for path in ("/", "/view/machines", "/view/repos"):
            status, headers, text = _request(hub, "GET", path)
            assert status == 401, path
            assert "set-cookie" not in headers
            assert TOKEN not in text
        status, _headers, _text = _request(hub, "GET", "/", headers={"Authorization": "Bearer nope"})
        assert status == 401
        status, _headers, _text = _request(hub, "GET", "/", headers={"Cookie": f"{fleet_hub.DASHBOARD_COOKIE}=junk"})
        assert status == 401

    def test_bearer_renders_html_with_security_headers(self, hub):
        status, headers, text = _request(hub, "GET", "/", headers=_bearer())
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
        status, headers, _text = _request(hub, "GET", "/?token=wrong")
        assert status == 401
        assert "set-cookie" not in headers

    def test_cookie_grants_dashboard_only(self, hub):
        cookie = _login_cookie(hub)
        status, _headers, text = _request(hub, "GET", "/", headers={"Cookie": cookie})
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
        status, _headers, text = _request(hub, "GET", "/", headers=_bearer())
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
        _status, _headers, text = _request(hub, "GET", "/?all=1", headers=_bearer())
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
        _status, _headers, text = _request(hub, "GET", "/", headers=_bearer())
        assert "No fleet events recorded yet." in text
        _status, _headers, text = _request(hub, "GET", "/view/repos", headers=_bearer())
        assert "No repos match." in text


class TestSortAndFilter:
    def test_node_filter(self, hub):
        _seed(hub)
        _status, _headers, text = _request(hub, "GET", "/?node=1111", headers=_bearer())
        assert "runalpha" in text and "runbravo" not in text
        assert f'data-node="{NODE_B}"' not in text

    def test_attention_only(self, hub):
        _seed(hub)
        _status, _headers, text = _request(hub, "GET", "/?attention=1", headers=_bearer())
        assert "runbravo" in text and "runalpha" not in text
        _status, _headers, text = _request(hub, "GET", "/view/repos?attention=1", headers=_bearer())
        assert "repo-b" in text and "repo-c" not in text
        assert "repo-a" in text  # collision counts as needing attention

    def test_state_and_seat_filters(self, hub):
        _seed(hub)
        _status, _headers, text = _request(hub, "GET", "/?state=paused", headers=_bearer())
        assert "runbravo" in text and "runalpha" not in text
        _status, _headers, text = _request(hub, "GET", "/?state=running", headers=_bearer())
        assert "runalpha" in text and "runbravo" not in text
        _status, _headers, text = _request(hub, "GET", "/?seat=codex", headers=_bearer())
        assert "runbravo" in text and "runalpha" not in text

    def test_repo_filter_on_repo_board(self, hub):
        _seed(hub)
        _status, _headers, text = _request(hub, "GET", "/view/repos?repo=repo-b", headers=_bearer())
        assert "repo-b" in text and "repo-a" not in text.split("<tbody>")[1]

    def test_sort_orders_cards(self, hub):
        _seed(hub)
        # Default attention sort: the machine with an awaiting-approval run comes first.
        _status, _headers, text = _request(hub, "GET", "/", headers=_bearer())
        assert text.index(f'data-node="{NODE_B}"') < text.index(f'data-node="{NODE_A}"')
        _status, _headers, text = _request(hub, "GET", "/?sort=node", headers=_bearer())
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
        _status, _headers, text = _request(hub, "GET", "/?sort=age&node=1111&attention=1", headers=_bearer())
        assert 'href="/view/repos?sort=age&amp;node=1111&amp;attention=1"' in text
        assert 'name="node" value="1111"' in text
        assert 'name="attention" value="1" checked' in text


class TestNoSecretsInHtml:
    def test_no_token_cookie_or_holder_in_any_page(self, hub):
        _seed(hub)
        cookie = _login_cookie(hub)
        cookie_value = cookie.split("=", 1)[1]
        for path in ("/", "/view/machines", "/view/repos", "/?all=1&sort=node"):
            for headers in (_bearer(), {"Cookie": cookie}):
                status, _headers, text = _request(hub, "GET", path, headers=headers)
                assert status == 200
                assert TOKEN not in text
                assert cookie_value not in text
                assert HOLDER_TOKEN not in text
                assert "Authorization" not in text

    def test_filter_values_are_escaped(self, hub):
        _seed(hub)
        _status, _headers, text = _request(hub, "GET", "/?node=%3Cscript%3Ex", headers=_bearer())
        assert "<script>x" not in text
        assert "&lt;script&gt;x" in text


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
