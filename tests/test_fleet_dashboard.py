"""Fleet sync phase 3 tests (issue #1124): hub-served web Fleet dashboard."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import html
import re
import http.client
import json
import sys
import threading
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from brigade import (
    fleet_client,
    fleet_dashboard,
    fleet_hub,
    fleet_hub_grokbot,
    fleet_hub_http,
    fleet_hub_sessions,
    fleet_hub_status,
)

NODE_A = "11111111-1111-4111-8111-111111111111"
NODE_B = "22222222-2222-4222-8222-222222222222"
TOKEN = "test-token-12345"
HOLDER_TOKEN = "secret-holder-token-zz"


@pytest.fixture()
def hub(tmp_path):
    db = tmp_path / "hub" / "fleet.db"
    # Legacy shared-token mode (pre-#1150): the one token posts as any node.
    server = fleet_hub.make_server("127.0.0.1", 0, db, TOKEN, allow_admin_writes=True)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    yield ("127.0.0.1", server.server_address[1], db)
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
    assert not thread.is_alive(), "Fleet Hub accept loop survived shutdown (#1916)"


def _request(hub, method: str, path: str, *, headers: dict | None = None, body=None):
    """Raw request so redirects are not followed and Set-Cookie is visible."""
    host, port = hub[0], hub[1]
    conn = http.client.HTTPConnection(host, port, timeout=5)
    data = body if isinstance(body, bytes) else json.dumps(body).encode() if body is not None else None
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


def _insert_grokbot_job(conn, *, job_id: str, state: str, lease_expires_at: str) -> None:
    stamp = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO grokbot_jobs ("
        "job_id, role, repository, label, task_digest, idempotency_key_hash, state, item_revision, sequence, "
        "created_at, updated_at, queued_at, timeout_seconds, artifact_kind, owner_node, claimed_at, "
        "lease_expires_at, claimant_node, claimant_worker"
        ") VALUES (?, 'implementation-worker', ?, ?, ?, ?, ?, 1, 1, ?, ?, ?, 900, 'draft-pr', "
        "'fixture-queue-owner', ?, ?, ?, ?)",
        (
            job_id,
            "fixture/http-active",
            "HTTP active Grok job",
            f"fixture-private-task-digest-{job_id}",
            job_id[-24:].ljust(64, "f"),
            state,
            stamp,
            stamp,
            stamp,
            stamp,
            lease_expires_at,
            NODE_B,
            "grokbot-worker",
        ),
    )


def _login_cookie(hub) -> str:
    status, _headers, text = _request(hub, "POST", "/dashboard/enrollment", headers=_bearer(), body={})
    assert status == 201
    code = json.loads(text)["code"]
    status, headers, _text = _request(
        hub,
        "POST",
        "/enroll",
        headers={"Content-Type": "application/x-www-form-urlencoded", "Sec-Fetch-Site": "same-origin"},
        body=f"code={code}".encode(),
    )
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

    def test_enrollment_sets_httponly_cookie_and_redirects_without_secret(self, hub):
        cookie = _login_cookie(hub)
        assert cookie.startswith(f"{fleet_hub.DASHBOARD_COOKIE}=")
        assert TOKEN not in cookie

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

    def test_readonly_dashboard_does_not_scan_or_show_unread_totals(self, hub, monkeypatch):
        calls = []

        def unread_counts(_conn):
            calls.append(True)
            return {NODE_A: 3}

        monkeypatch.setattr(fleet_hub_http.fleet_hub_messages, "unread_counts", unread_counts)
        cookie = _login_cookie(hub)

        status, _headers, page = _request(hub, "GET", "/view/machines", headers={"Cookie": cookie})

        assert status == 200
        assert "unread messages" not in page
        assert calls == []

    def test_legacy_derived_cookie_is_refused(self, hub):
        value = hmac.new(TOKEN.encode(), b"brigade-fleet-dashboard-cookie-v1", hashlib.sha256).hexdigest()
        assert _request(hub, "GET", "/deck", headers={"Cookie": f"{fleet_hub.DASHBOARD_COOKIE}={value}"})[0] == 401


class TestBoards:
    def test_http_claim_projection_renders_active_grokbot_jobs_without_events(self, hub, monkeypatch):
        now = datetime.now(timezone.utc)
        active_job = "grokbot-" + "a" * 24
        expired_job = "grokbot-" + "b" * 24
        terminal_job = "grokbot-" + "c" * 24
        status, _headers, text = _request(
            hub,
            "POST",
            "/claims",
            headers=_bearer(),
            body={"action": "acquire", "target": "fixture/ordinary", "node_id": NODE_A, "holder": HOLDER_TOKEN},
        )
        assert status == 200, text
        conn = fleet_hub.init_db(hub[2])
        try:
            fleet_hub_grokbot.ensure_schema(conn)
            _insert_grokbot_job(
                conn, job_id=active_job, state="claimed", lease_expires_at=(now + timedelta(minutes=5)).isoformat()
            )
            _insert_grokbot_job(
                conn, job_id=expired_job, state="running", lease_expires_at=(now - timedelta(seconds=1)).isoformat()
            )
            _insert_grokbot_job(
                conn, job_id=terminal_job, state="completed", lease_expires_at=(now + timedelta(minutes=5)).isoformat()
            )
            conn.commit()
            assert [claim["target"] for claim in fleet_hub.list_claims(conn)] == ["fixture/ordinary"]
        finally:
            conn.close()

        status, _headers, body = _request(hub, "GET", "/claims", headers=_bearer())
        assert status == 200
        claims = json.loads(body)["claims"]
        assert {claim.get("job") for claim in claims if claim.get("harness") == "grokbot"} == {active_job}
        assert {claim["target"] for claim in claims} == {
            "fixture/ordinary",
            f"fixture/http-active · HTTP active Grok job [{active_job}]",
        }
        assert TOKEN not in body and HOLDER_TOKEN not in body and "fixture-private-task-digest" not in body

        status, _headers, all_body = _request(hub, "GET", "/claims?all=1", headers=_bearer())
        assert status == 200
        assert {claim.get("job") for claim in json.loads(all_body)["claims"] if claim.get("harness") == "grokbot"} == {
            active_job,
            expired_job,
        }
        assert terminal_job not in all_body

        for view in ("machines", "repos"):
            status, _headers, page = _request(hub, "GET", f"/view/{view}", headers=_bearer())
            assert status == 200
            assert active_job in page
            assert expired_job not in page and terminal_job not in page
            assert TOKEN not in page and HOLDER_TOKEN not in page and "fixture-private-task-digest" not in page
            status, _headers, all_page = _request(hub, "GET", f"/view/{view}?all=1", headers=_bearer())
            assert status == 200
            assert active_job in all_page
            assert expired_job not in all_page and terminal_job not in all_page

        monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", f"http://{hub[0]}:{hub[1]}")
        monkeypatch.setenv("BRIGADE_FLEET_TOKEN", TOKEN)
        assert {claim.get("job") for claim in fleet_client.fetch_claims() if claim.get("harness") == "grokbot"} == {
            active_job
        }
        assert {
            claim.get("job")
            for claim in fleet_client.fetch_claims(include_all=True)
            if claim.get("harness") == "grokbot"
        } == {
            active_job,
            expired_job,
        }

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
        # Claim-owner drift is visible in the two columns, but only two
        # concurrently active nodes qualify as a collision.
        assert 'data-collision="0"' in repo_a
        repo_b = by_repo["repo-b"]
        assert "awaiting approval" in repo_b and 'data-collision="0"' in repo_b
        repo_c = by_repo["repo-c"]
        assert "idle" in repo_c and "succeeded" in repo_c

    def test_claims_and_boards_render_external_labels_and_hide_finished_verify(self, hub):
        now = datetime.now(timezone.utc).isoformat()
        events = [
            {
                **_event(NODE_A, "ext-1", 1, "external.claimed", repo="repo-x", harness="grokbot", ts=now),
                "seat": "implementation-worker",
            },
            {
                **_event(NODE_A, "vr-1", 1, "verify.completed", repo="repo-x", harness="brigade-work", ts=now),
                "exit_status": 0,
                "capability_fingerprint": "fp-1",
            },
        ]
        status, _headers, text = _request(hub, "POST", "/events", headers=_bearer(), body=events)
        assert status == 200, text
        claim = {
            "action": "acquire",
            "target": "repo-x",
            "node_id": NODE_A,
            "holder": "holder-secret",
            "harness": "grokbot",
            "role": "implementation-worker",
            "job": "job-x",
            "session": "lease-x",
        }
        status, _headers, text = _request(hub, "POST", "/claims", headers=_bearer(), body=claim)
        assert status == 200, text
        _status, _headers, text = _request(hub, "GET", "/view/machines", headers=_bearer())
        assert "repo-x" in text
        assert "grokbot" in text and "lease-x" in text
        assert "holder-secret" not in text
        assert "vr-1" not in text
        _status, _headers, all_text = _request(hub, "GET", "/view/machines?all=1", headers=_bearer())
        assert "vr-1" in all_text
        assert "succeeded" in all_text
        assert "holder-secret" not in all_text

    def test_machine_page_does_not_resurrect_old_grokbot_revision(self, hub):
        stamp = datetime.now(timezone.utc).isoformat()
        old_stamp = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        events = [
            _event(NODE_A, "stable-machine-run", 1, "run.started", repo="repo", harness="codex", ts=stamp),
            _event(NODE_A, "shared-grok", 1, "external.queued", repo="repo", harness="grokbot", ts=stamp),
            _event(NODE_B, "shared-grok", 2, "external.completed", repo="repo", harness="grokbot", ts=old_stamp),
        ]
        status, _headers, text = _request(hub, "POST", "/events", headers=_bearer(), body=events)
        assert status == 200, text
        conn = fleet_hub.open_db(hub[2])
        try:
            conn.execute(
                "UPDATE events SET received_at = ? WHERE run_id = ? AND sequence = 2", (old_stamp, "shared-grok")
            )
            conn.commit()
        finally:
            conn.close()

        _status, _headers, page = _request(hub, "GET", "/view/machines", headers=_bearer())

        assert "shared-grok" not in page

    def test_grokbot_last_known_seat_is_global_across_nodes(self, hub):
        stamp = datetime.now(timezone.utc).isoformat()
        events = [
            {
                **_event(NODE_A, "cross-node-seat", 1, "external.claimed", repo="repo", harness="grokbot", ts=stamp),
                "seat": "implementation-worker",
            },
            {
                **_event(NODE_B, "cross-node-seat", 2, "external.completed", repo="repo", harness="grokbot", ts=stamp),
                "seat": "-",
            },
        ]
        status, _headers, text = _request(hub, "POST", "/events", headers=_bearer(), body=events)
        assert status == 200, text

        status, _headers, page = _request(
            hub, "GET", "/view/machines?all=1&seat=implementation-worker", headers=_bearer()
        )

        assert status == 200
        assert f'data-node="{NODE_B}"' in page
        assert 'title="cross-node-seat"' in page
        assert "implementation-worker" in page

    def test_finished_verify_buckets_use_exit_status(self, hub):
        now = datetime.now(timezone.utc).isoformat()
        events = [
            {
                **_event(NODE_A, "vr-fail", 1, "verify.completed", repo="repo-fail", harness="brigade-work", ts=now),
                "exit_status": 1,
            },
            {
                **_event(NODE_A, "vr-int", 1, "verify.completed", repo="repo-int", harness="brigade-work", ts=now),
                "exit_status": 130,
            },
        ]
        status, _headers, text = _request(hub, "POST", "/events", headers=_bearer(), body=events)
        assert status == 200, text
        _status, _headers, all_text = _request(hub, "GET", "/view/machines?all=1", headers=_bearer())
        fail_row = next(row for row in all_text.split("<tr") if "vr-fail" in row)
        int_row = next(row for row in all_text.split("<tr") if "vr-int" in row)
        assert 'data-bucket="failed"' in fail_row
        assert 'data-bucket="interrupted"' in int_row

    def test_empty_hub_renders(self, hub):
        _status, _headers, text = _request(hub, "GET", "/view/machines", headers=_bearer())
        assert "No fleet events recorded yet." in text
        _status, _headers, text = _request(hub, "GET", "/view/repos", headers=_bearer())
        assert "No repos match." in text

    def test_claim_only_node_and_claim_text_are_rendered_safely(self):
        now = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
        claim = {
            "target": "example/brigade · <script>alert(1)</script> [grokbot-aaaaaaaaaaaaaaaaaaaaaaaa]",
            "owner_node": "claim-only-node",
            "owner_conductor": "<img src=x>",
            "harness": "grokbot",
            "role": "implementation-worker",
            "job": "grokbot-aaaaaaaaaaaaaaaaaaaaaaaa",
            "acquired_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=5)).isoformat(),
        }
        for view in ("machines", "repos"):
            page = fleet_dashboard.render_page(
                view=view,
                query_string="",
                runs=[],
                claims=[claim],
                nodes=[],
                started_at={},
                nonce="test-nonce",
                now=now,
            )
            assert "<script>alert(1)</script>" not in page
            assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
            assert "&lt;img src=x&gt;" in page
            if view == "machines":
                assert 'data-node="claim-only-node"' in page
                assert "0 live runs, 1 machine, 0 need attention, 1 claim held." in page


class TestSortAndFilter:
    def test_machine_start_lookup_seeks_the_node_run_index(self, hub):
        conn = fleet_hub.open_db(hub[2])
        try:
            sql = fleet_hub_status._latest_sql(resolve_started_at=True)
            plan = [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql)]
        finally:
            conn.close()
        # The generic start lookup runs once per latest run. A scan here makes
        # a machine-board request grow with runs times journal history.
        assert any("SEARCH e2 USING INDEX events_run_seq (node_id=? AND run_id=?)" in detail for detail in plan)

    @pytest.mark.parametrize("upgrade_from_v29", [False, True], ids=["fresh", "v29-upgrade"])
    def test_grokbot_start_lookup_seeks_without_sorting(self, tmp_path, upgrade_from_v29):
        db = tmp_path / "fleet.db"
        conn = fleet_hub.init_db(db)
        try:
            _insert_event(conn, NODE_A, "grokbot-run", 1, "external.created", "2026-09-07T12:00:00Z", harness="grokbot")
            conn.commit()
            if upgrade_from_v29:
                conn.execute("DROP INDEX IF EXISTS events_grokbot_run_seq")
                conn.execute("PRAGMA user_version=29")
                conn.commit()
                conn.close()
                conn = fleet_hub.init_db(db)
            assert conn.execute("SELECT run_id FROM events").fetchall() == [("grokbot-run",)]
            sql = fleet_hub_status._latest_sql(resolve_started_at=True)
            plan = conn.execute("EXPLAIN QUERY PLAN " + sql).fetchall()
            grokbot_searches = [
                row for row in plan if "SEARCH e2 USING INDEX events_grokbot_run_seq (run_id=?)" in row[3]
            ]
            assert grokbot_searches, plan
            # The index must also satisfy the tie-break order for LIMIT 1.
            subquery_id = grokbot_searches[0][1]
            assert not any(row[1] == subquery_id and "TEMP B-TREE" in row[3] for row in plan)
            assert not any("SCAN e2" in row[3] for row in plan)
            assert conn.execute("PRAGMA user_version").fetchone()[0] == fleet_hub.SCHEMA_VERSION
        finally:
            conn.close()

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
        assert "repo-a" not in text  # claim-owner drift alone is not a collision

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

    def test_attention_sort_paginates_nodes_after_priority(self, hub):
        node_ids = [f"00000000-0000-4000-8000-{index:012d}" for index in range(fleet_hub.BOARD_LIMIT)]
        failed_node = "ffffffff-ffff-4fff-8fff-000000000001"
        stamp = datetime.now(timezone.utc).isoformat()
        events = [
            _event(node_id, f"run-{index}", 1, "run.started", repo="repo", harness="codex", ts=stamp)
            for index, node_id in enumerate(node_ids)
        ]
        events.append(_event(failed_node, "failed", 1, "run.failed", repo="repo", harness="codex", ts=stamp))
        status, _headers, text = _request(hub, "POST", "/events", headers=_bearer(), body=events)
        assert status == 200, text

        _status, _headers, first = _request(hub, "GET", "/view/machines?all=1&sort=attention", headers=_bearer())

        assert f'data-node="{failed_node}"' in first
        assert first.index(f'data-node="{failed_node}"') < first.index(f'data-node="{node_ids[0]}"')

    @pytest.mark.parametrize("sort", ["attention", "age", "state"])
    def test_machine_page_matches_full_order_within_one_bucket(self, hub, sort):
        node_ids = [f"00000000-0000-4000-8000-{index:012d}" for index in range(fleet_hub.BOARD_LIMIT + 1)]
        now = datetime.now(timezone.utc)
        events = []
        for index, node_id in enumerate(node_ids):
            # All runs share a bucket. Age and state order oppose node-ID order.
            start = (now - timedelta(minutes=index + 1)).isoformat()
            state = "run.dispatch.observed" if index % 2 else "run.started"
            events.extend(
                [
                    _event(node_id, f"run-{index}", 1, "run.created", repo="repo", harness="codex", ts=start),
                    _event(node_id, f"run-{index}", 2, state, repo="repo", harness="codex", ts=now.isoformat()),
                ]
            )
        status, _headers, text = _request(hub, "POST", "/events", headers=_bearer(), body=events)
        assert status == 200, text
        expected = sorted(
            range(len(node_ids)),
            key=lambda index: (
                (("run.dispatch.observed" if index % 2 else "run.started"), -index) if sort == "state" else (-index,)
            ),
        )

        status, _headers, first = _request(hub, "GET", f"/view/machines?sort={sort}", headers=_bearer())
        assert status == 200
        assert re.findall(r'data-node="([^"]+)"', first) == [
            node_ids[index] for index in expected[: fleet_hub.BOARD_LIMIT]
        ]
        more = re.search(r'class="fleet-more"><a href="([^"]+)"', first)
        assert more
        status, _headers, second = _request(hub, "GET", html.unescape(more[1]), headers=_bearer())
        assert status == 200
        assert re.findall(r'data-node="([^"]+)"', second) == [node_ids[expected[-1]]]

    def test_sort_orders_repo_rows(self, hub):
        _seed(hub)
        _status, _headers, text = _request(hub, "GET", "/view/repos", headers=_bearer())
        body = text.split("<tbody>")[1]
        assert body.index("repo-b") < body.index("repo-a") < body.index("repo-c")  # attention, running, idle
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
    host, port = hub[0], hub[1]
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
        assert manifest["theme_color"] == "#0a0a0a"
        assert manifest["background_color"] == "#0a0a0a"
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
            assert '<meta name="theme-color" content="#0a0a0a"' in body
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
        assert fleet_dashboard.bucket_for("verify.completed", age_seconds=0, exit_status=0) == "succeeded"
        assert fleet_dashboard.bucket_for("verify.completed", age_seconds=0, exit_status=130) == "interrupted"
        assert fleet_dashboard.bucket_for("verify.completed", age_seconds=0, exit_status=1) == "failed"
        assert fleet_dashboard.bucket_for("verify.completed", age_seconds=0, exit_status=137) == "failed"

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

    def test_terminal_suffixes_and_abandoned_rows_are_not_active(self):
        now = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
        old = (now - timedelta(hours=2)).isoformat()
        rows = fleet_dashboard.build_rows(
            [
                {"node_id": "n", "run_id": "done", "state": "run.dispatch.completed", "ts": now.isoformat()},
                {"node_id": "n", "run_id": "timed", "state": "provider.timed_out", "ts": now.isoformat()},
                {"node_id": "n", "run_id": "abandoned", "state": "run.started", "ts": old},
            ],
            {("n", "done"): old, ("n", "timed"): old, ("n", "abandoned"): old},
            now=now,
        )

        by_id = {row.run_id: row for row in rows}
        assert by_id["done"].live is False and by_id["done"].bucket == "succeeded"
        assert by_id["timed"].live is False and by_id["timed"].bucket == "interrupted"
        assert by_id["abandoned"].live is True and by_id["abandoned"].active is False
        assert fleet_dashboard.filter_rows(rows, fleet_dashboard.DashboardQuery()) == []
        assert fleet_dashboard.filter_rows(rows, fleet_dashboard.DashboardQuery(attention_only=True)) == [
            by_id["abandoned"]
        ]

    def test_repo_collision_requires_two_concurrently_active_nodes(self):
        now = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
        fresh = fleet_dashboard.build_rows(
            [{"node_id": "a", "run_id": "fresh", "repo": "repo", "state": "run.started", "ts": now.isoformat()}],
            {("a", "fresh"): now.isoformat()},
            now=now,
        )[0]
        old = (now - timedelta(hours=2)).isoformat()
        abandoned = fleet_dashboard.build_rows(
            [{"node_id": "b", "run_id": "old", "repo": "repo", "state": "run.started", "ts": old}],
            {("b", "old"): old},
            now=now,
        )[0]
        second = fleet_dashboard.build_rows(
            [{"node_id": "b", "run_id": "fresh-b", "repo": "repo", "state": "run.started", "ts": now.isoformat()}],
            {("b", "fresh-b"): now.isoformat()},
            now=now,
        )[0]

        assert fleet_dashboard._RepoEntry("repo", [fresh], [fresh, abandoned], None, None).collision is False
        assert fleet_dashboard._RepoEntry("repo", [fresh, second], [fresh, second], None, None).collision is True
        html = fleet_dashboard.render_page(
            view="repos",
            query_string="",
            runs=[
                {"node_id": row.node_id, "run_id": row.run_id, "repo": row.repo, "state": row.state, "ts": row.last_ts}
                for row in (fresh, second)
            ],
            claims=[],
            nodes=[],
            started_at={},
            nonce="abc",
            now=now,
        )
        assert "parallel activity" in html and "Same repo label" in html
        assert "File overlap unknown" in html
        assert "! collision" not in html and "fleet-state-failed" not in html.split("<tbody>")[1]
        events = [
            {"node_id": row.node_id, "run_id": row.run_id, "repo": row.repo, "state": row.state, "ts": row.last_ts}
            for row in (fresh, second)
        ]
        events.append(
            {
                "node_id": "c",
                "run_id": "bad",
                "repo": "approval-repo",
                "state": "approval.requested",
                "ts": now.isoformat(),
            }
        )
        attention = fleet_dashboard.render_page(
            view="repos",
            query_string="attention=1",
            runs=events,
            claims=[],
            nodes=[],
            started_at={},
            nonce="abc",
            now=now,
        )
        assert "<td>repo</td>" not in attention and "<td>approval-repo</td>" in attention
        ranked = fleet_dashboard.render_page(
            view="repos", query_string="", runs=events, claims=[], nodes=[], started_at={}, nonce="abc", now=now
        )
        assert ranked.index("<td>approval-repo</td>") < ranked.index("<td>repo</td>")

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


@pytest.mark.parametrize("view", ["machines", "repos"])
def test_boards_render_through_the_shared_deck_shell(hub, view, monkeypatch):
    monkeypatch.setattr(fleet_dashboard, "REFRESH_SECONDS", 17)
    query = {
        "sort": "seat",
        "node": NODE_A,
        "repo": "example & tool",
        "seat": "worker",
        "state": "awaiting approval",
        "attention": "1",
        "all": "1",
    }
    event = _event(
        NODE_A,
        "shell-regression-run",
        1,
        "run.paused",
        repo=query["repo"],
        harness="codex",
        ts=datetime.now(timezone.utc).isoformat(),
    )
    assert _request(hub, "POST", "/events", headers=_bearer(), body=[event])[0] == 200
    status, headers, page = _request(hub, "GET", f"/view/{view}?{urlencode(query)}", headers=_bearer())
    assert status == 200
    assert '<div class="shell" data-active="boards">' in page
    assert '<div class="shell-main"><main class="deck-shell fleet-board">' in page
    sidebar = re.search(r'<aside class="shell-sidebar"[^>]*>(.*?)</aside>', page, re.S)
    assert sidebar is not None
    assert page.count('<nav class="deck-nav" aria-label="Command Deck">') == 1
    assert 'href="/view/machines" aria-current="page"' in sidebar[1]
    for route in ["/deck", "/deck/repos", "/deck/roster", "/deck/policy", "/deck/owners"]:
        assert f'href="{route}"' in sidebar[1]
    for label in ["Resources", "Docs", "Report a bug", "Star on GitHub", 'class="sidebar-meta"', "sidebar-version"]:
        assert label in sidebar[1]
    assert "tree-machines" not in sidebar[1]  # Boards have no DeckView projection.
    assert 'class="status-dot' not in page and 'class="live-pill' not in page
    assert "--canvas: #0a0a0a;" in page
    assert "prefers-color-scheme: light" in page
    assert '<body class="deck"' in page
    assert f"<h1>{view.title()} board</h1>" in page
    assert '<header class="page-head">' in page
    assert "background: #fff" not in page
    nonce = re.search(r"'nonce-([^']+)'", headers["content-security-policy"])[1]
    for tag in ["style", "script"]:
        assert re.findall(rf"<{tag}([^>]*)>", page) == [f' nonce="{nonce}"']
    assert '<meta http-equiv="refresh" content="17">' in page
    assert "Refreshes every 17s" in page
    assert "setInterval(tick, 1000)" in page and 'data-filter-all="1"' in page
    pivots = re.search(r'<nav class="board-nav" aria-label="Board views">(.*?)</nav>', page)[1]
    for name in ["machines", "repos"]:
        link = re.search(rf'<a href="([^"]+)"([^>]*)>{name} board</a>', pivots)
        assert link is not None
        url = urlsplit(html.unescape(link[1]))
        assert url.path == f"/view/{name}"
        assert parse_qs(url.query) == {key: [value] for key, value in query.items()}
        assert ('aria-current="page"' in link[2]) == (name == view)
    for key in ["node", "repo", "seat", "state"]:
        assert f'name="{key}" value="{html.escape(query[key], quote=True)}"' in page
    assert '<option value="seat" selected>' in page
    for key in ["attention", "all"]:
        assert f'name="{key}" value="1" checked' in page
    assert f'method="get" action="/view/{view}"' in page
    assert "example &amp; tool" in page and 'class="fleet-state fleet-state-awaiting-approval"' in page
    if view == "machines":
        assert 'data-bucket="awaiting approval"' in page
        assert "shell-regression-run" in page and "worker/codex" in page


def test_machine_summary_counts_nodes_with_only_unread_messages():
    page = fleet_dashboard.render_page(
        view="machines",
        query_string="",
        runs=[],
        claims=[],
        nodes=[{"node_id": NODE_A}],
        started_at={},
        nonce="abc",
        unread_counts={NODE_B: 3},
    )

    assert "2 machines" in page
    assert f'data-node="{NODE_A}"' in page
    assert f'data-node="{NODE_B}"' in page


def test_interactive_sessions_stay_off_legacy_boards_and_station_capacity(tmp_path):
    config_path = tmp_path / "deck.json"
    config_path.write_text(
        json.dumps({"stations": [{"node_id": NODE_A, "name": "Alpha", "capacity": 10}]}),
        encoding="utf-8",
    )
    db = tmp_path / "hub" / "fleet.db"
    server = fleet_hub.make_server(
        "127.0.0.1",
        0,
        db,
        TOKEN,
        allow_admin_writes=True,
        deck_config_path=config_path,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        hub = ("127.0.0.1", server.server_address[1])
        conn = fleet_hub.open_db(db)
        try:
            status, _payload = fleet_hub_sessions.handle_session(
                conn,
                {
                    "action": "upsert",
                    "node_id": NODE_A,
                    "harness": "claude",
                    "session_id": "sess-board",
                    "repo_identity": "github.com/example/project",
                    "identity_scope": "fleet",
                    "repo_label": "project",
                    "checkout_path": "<script>alert(1)</script>",
                    "branch": "main",
                    "dirty_paths": ["src/a.py"],
                    "dirty_truncated": False,
                    "ttl_seconds": 900,
                },
                caller_node=None,
            )
            assert status == 200
        finally:
            conn.close()
        status, _headers, deck_body = _request(hub, "GET", "/deck", headers=_bearer())
        assert status == 200
        assert "Reported agent sessions (1)" in deck_body
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in deck_body
        assert "<script>alert(1)</script>" not in deck_body
        assert "10 unavailable" in deck_body
        status, _headers, machines = _request(hub, "GET", "/view/machines", headers=_bearer())
        assert status == 200
        assert "sess-board" not in machines
        assert "<script>alert(1)</script>" not in machines
        assert "No fleet events recorded yet." in machines
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


TAILSCALE_USER = "tailscale-user@example.invalid"


@pytest.fixture()
def hub_tailscale(tmp_path):
    db = tmp_path / "hub" / "fleet.db"
    server = fleet_hub.make_server("127.0.0.1", 0, db, TOKEN, allow_admin_writes=True, trust_tailscale_identity=True)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
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


def _insert_event(
    conn,
    node: str,
    run_id: str,
    seq: int,
    state: str,
    received_at: str,
    *,
    repo: str = "repo",
    harness: str = "claude",
) -> None:
    conn.execute(
        "INSERT INTO events (node_id, run_id, sequence, digest, repo, seat, harness, state, ts, received_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (node, run_id, seq, f"{run_id}-{seq}", repo, "worker", harness, state, received_at, received_at),
    )


class TestBoardBounds:
    def test_board_sql_shape_is_windowed_or_paginated(self):
        windowed = fleet_hub_status.board_latest_sql(windowed=True)
        assert "received_at >=" in windowed
        assert "LIMIT ? OFFSET ?" in windowed
        assert "ROW_NUMBER()" in windowed
        history = fleet_hub_status.board_latest_sql(windowed=False)
        assert "received_at >=" not in history
        assert "LIMIT ? OFFSET ?" in history
        assert "ROW_NUMBER()" in history

    def test_events_received_at_index_exists(self, tmp_path):
        conn = fleet_hub.init_db(tmp_path / "fleet.db")
        try:
            names = {row[1] for row in conn.execute("PRAGMA index_list(events)")}
        finally:
            conn.close()
        assert "events_received_at" in names
        assert "events_run_seq" in names

    def test_node_summary_uses_rendered_runs_not_the_journal(self):
        runs = [
            {"node_id": NODE_A, "received_at": "2026-08-01T00:00:00+00:00"},
            {"node_id": NODE_A, "received_at": "2026-08-02T00:00:00+00:00"},
            {"node_id": NODE_B, "received_at": "2026-08-01T12:00:00+00:00"},
        ]
        assert fleet_hub.node_summary(runs) == [
            {"node_id": NODE_A, "last_received_at": "2026-08-02T00:00:00+00:00", "events": 2},
            {"node_id": NODE_B, "last_received_at": "2026-08-01T12:00:00+00:00", "events": 1},
        ]

    def test_run_started_at_looks_up_only_rendered_keys(self, tmp_path):
        conn = fleet_hub.init_db(tmp_path / "fleet.db")
        try:
            now = datetime(2026, 8, 23, 12, tzinfo=timezone.utc)
            old = (now - timedelta(hours=48)).isoformat()
            recent = now.isoformat()
            _insert_event(conn, NODE_A, "kept", 1, "run.created", old)
            _insert_event(conn, NODE_A, "kept", 2, "run.dispatch.observed", recent)
            _insert_event(conn, NODE_A, "ignored", 1, "run.created", old)
            conn.commit()
            started = fleet_hub.run_started_at(conn, [(NODE_A, "kept", "claude")])
            assert started == {(NODE_A, "kept"): old}
        finally:
            conn.close()

    def test_board_status_windows_terminals_and_keeps_latest_per_run(self, tmp_path):
        conn = fleet_hub.init_db(tmp_path / "fleet.db")
        try:
            now = datetime(2026, 8, 23, 12, tzinfo=timezone.utc)
            old = (now - timedelta(hours=48)).isoformat()
            recent = (now - timedelta(hours=2)).isoformat()
            live = now.isoformat()
            _insert_event(conn, NODE_A, "ancient", 1, "run.created", old, repo="repo-old")
            _insert_event(conn, NODE_A, "ancient", 2, "run.completed", old, repo="repo-old")
            _insert_event(conn, NODE_A, "recent-done", 1, "run.created", recent, repo="repo-new")
            _insert_event(conn, NODE_A, "recent-done", 2, "run.completed", recent, repo="repo-new")
            _insert_event(conn, NODE_A, "live", 1, "run.created", live, repo="repo-live")
            _insert_event(conn, NODE_A, "live", 2, "run.dispatch.observed", live, repo="repo-live")
            conn.commit()
            default = fleet_hub.board_status(conn, now=now)
            default_ids = {row["run_id"] for row in default.runs}
            assert default_ids == {"recent-done", "live"}
            assert default.more is False
            history = fleet_hub.board_status(conn, include_all=True, now=now)
            assert {row["run_id"] for row in history.runs} == {"ancient", "recent-done", "live"}
            live_row = next(row for row in default.runs if row["run_id"] == "live")
            assert live_row["state"] == "run.dispatch.observed"
            assert live_row["sequence"] == 2
        finally:
            conn.close()

    def test_board_status_all_is_paginated(self, tmp_path):
        conn = fleet_hub.init_db(tmp_path / "fleet.db")
        try:
            now = datetime(2026, 8, 23, 12, tzinfo=timezone.utc)
            for index in range(5):
                stamp = (now - timedelta(minutes=5 - index)).isoformat()
                _insert_event(conn, NODE_A, f"done-{index}", 1, "run.completed", stamp, repo=f"repo-{index}")
            conn.commit()
            first = fleet_hub.board_status(conn, include_all=True, now=now, limit=3, offset=0)
            assert [row["run_id"] for row in first.runs] == ["done-4", "done-3", "done-2"]
            assert first.more is True
            assert first.limit == 3
            second = fleet_hub.board_status(conn, include_all=True, now=now, limit=3, offset=3)
            assert [row["run_id"] for row in second.runs] == ["done-1", "done-0"]
            assert second.more is False
        finally:
            conn.close()

    def test_board_status_does_not_delete_events(self, tmp_path):
        conn = fleet_hub.init_db(tmp_path / "fleet.db")
        try:
            now = datetime(2026, 8, 23, 12, tzinfo=timezone.utc)
            _insert_event(conn, NODE_A, "r", 1, "run.completed", now.isoformat())
            conn.commit()
            fleet_hub.board_status(conn, include_all=True, now=now)
            assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
        finally:
            conn.close()

    def test_render_page_emits_more_link_from_owned_http_flag(self):
        page = fleet_dashboard.render_page(
            view="machines",
            query_string="all=1",
            runs=[],
            claims=[],
            nodes=[],
            started_at={},
            nonce="abc",
            more_href="/view/machines?all=1&offset=200",
        )
        assert 'class="fleet-more"' in page
        assert 'href="/view/machines?all=1&amp;offset=200"' in page
        assert ">more<" in page

    def test_default_board_omits_old_terminal_outcome_all_keeps_it(self, hub):
        now = datetime.now(timezone.utc)
        old = (now - timedelta(hours=48)).isoformat()
        recent = now.isoformat()
        events = [
            _event(NODE_A, "old-done", 1, "run.completed", repo="repo-old", harness="claude", ts=old),
            _event(NODE_A, "new-done", 1, "run.completed", repo="repo-new", harness="claude", ts=recent),
            _event(NODE_A, "live-run", 1, "run.dispatch.observed", repo="repo-live", harness="claude", ts=recent),
        ]
        status, _headers, text = _request(hub, "POST", "/events", headers=_bearer(), body=events)
        assert status == 200, text
        conn = fleet_hub.open_db(hub[2])
        try:
            conn.execute("UPDATE events SET received_at = ? WHERE run_id = ?", (old, "old-done"))
            conn.commit()
        finally:
            conn.close()
        _status, _headers, machines = _request(hub, "GET", "/view/machines", headers=_bearer())
        assert "live-run" in machines
        assert "old-done" not in machines
        assert "new-done" not in machines
        _status, _headers, repos = _request(hub, "GET", "/view/repos", headers=_bearer())
        assert "repo-live" in repos
        assert "repo-new" in repos
        assert "repo-old" not in repos
        _status, _headers, all_machines = _request(hub, "GET", "/view/machines?all=1", headers=_bearer())
        assert "old-done" in all_machines
        assert "new-done" in all_machines
        _status, _headers, all_repos = _request(hub, "GET", "/view/repos?all=1", headers=_bearer())
        assert "repo-old" in all_repos

    def test_unread_candidates_do_not_repeat_across_event_pages(self, hub, monkeypatch):
        now = datetime.now(timezone.utc).isoformat()
        node_ids = [f"00000000-0000-4000-8000-{index:012d}" for index in range(fleet_hub.BOARD_LIMIT + 1)]
        events = [
            _event(node_id, f"run-{index}", 1, "run.dispatch.observed", repo="repo", harness="codex", ts=now)
            for index, node_id in enumerate(node_ids)
        ]
        status, _headers, text = _request(hub, "POST", "/events", headers=_bearer(), body=events)
        assert status == 200, text
        monkeypatch.setattr(
            fleet_hub_http.fleet_hub_messages,
            "unread_counts",
            lambda _conn: {node_id: 1 for node_id in node_ids},
        )

        _status, _headers, first = _request(hub, "GET", "/view/machines", headers=_bearer())
        assert "offset=200" in first
        _status, _headers, second = _request(hub, "GET", "/view/machines?offset=200", headers=_bearer())
        first_ids = {node_id for node_id in node_ids if f'data-node="{node_id}"' in first}
        second_ids = {node_id for node_id in node_ids if f'data-node="{node_id}"' in second}
        assert len(first_ids) == fleet_hub.BOARD_LIMIT
        assert len(second_ids) == 1
        assert first_ids.isdisjoint(second_ids)
        assert first_ids | second_ids == set(node_ids)

    def test_machine_event_page_uses_same_unread_exclusions_as_pagination(self, hub, monkeypatch):
        stamp = datetime.now(timezone.utc).isoformat()
        monkeypatch.setattr(fleet_dashboard, "MACHINE_CARD_LIMIT", 4)
        event_node_ids = ["00000000-0000-4000-8000-000000000000"]
        claim_node_id = "ffffffff-ffff-4fff-8fff-000000000001"
        events = [
            _event(node_id, f"event-{index}", 1, "run.started", repo="repo", harness="codex", ts=stamp)
            for index, node_id in enumerate(event_node_ids)
        ]
        status, _headers, text = _request(hub, "POST", "/events", headers=_bearer(), body=events)
        assert status == 200, text
        status, _headers, text = _request(
            hub,
            "POST",
            "/claims",
            headers=_bearer(),
            body={
                "action": "acquire",
                "target": "repo",
                "node_id": claim_node_id,
                "holder": HOLDER_TOKEN,
                "conductor": "test",
            },
        )
        assert status == 200, text
        unread_node_ids = [f"unread-only-{index:03d}" for index in range(3)]
        unread = {claim_node_id: 1, **dict.fromkeys(unread_node_ids, 1)}
        monkeypatch.setattr(fleet_hub_http.fleet_hub_messages, "unread_counts", lambda _conn: unread)

        status, _headers, first = _request(hub, "GET", "/view/machines", headers=_bearer())

        assert status == 200
        assert f'data-node="{claim_node_id}"' not in first
        assert all(f'data-node="{node_id}"' in first for node_id in event_node_ids + unread_node_ids)
        more = re.search(r'class="fleet-more"><a href="([^"]+)"', first)
        assert more
        next_path = html.unescape(more[1])
        status, _headers, second = _request(hub, "GET", next_path, headers=_bearer())

        assert status == 200
        assert f'data-node="{claim_node_id}"' in second

    def test_terminal_only_node_with_unread_message_gets_supplemental_card(self, hub, monkeypatch):
        now = datetime.now(timezone.utc).isoformat()
        status, _headers, text = _request(
            hub,
            "POST",
            "/events",
            headers=_bearer(),
            body=[_event(NODE_A, "finished", 1, "run.completed", repo="repo", harness="codex", ts=now)],
        )
        assert status == 200, text
        monkeypatch.setattr(fleet_hub_http.fleet_hub_messages, "unread_counts", lambda _conn: {NODE_A: 1})

        status, _headers, page = _request(hub, "GET", "/view/machines", headers=_bearer())

        assert status == 200
        assert f'data-node="{NODE_A}"' in page
        assert "1 unread message</div>" in page

    def test_machine_pages_bound_runs_per_node_without_splitting_machine(self, hub, monkeypatch):
        run_ids = [f"run-{index:03d}" for index in range(201)]
        stamp = datetime.now(timezone.utc).isoformat()
        events = [
            _event(NODE_A, run_id, 1, "run.completed", repo="repo", harness="codex", ts=stamp) for run_id in run_ids
        ]
        for start in range(0, len(events), 1000):
            status, _headers, text = _request(
                hub, "POST", "/events", headers=_bearer(), body=events[start : start + 1000]
            )
            assert status == 200, text

        node_queries = []
        original_node_query = fleet_hub_status.board_machine_page

        def count_node_queries(*args, **kwargs):
            node_queries.append(True)
            return original_node_query(*args, **kwargs)

        page_queries = []
        original_page_query = fleet_hub_status.board_status_for_nodes

        def count_page_queries(conn, node_ids, **kwargs):
            page_queries.append(node_ids)
            return original_page_query(conn, node_ids, **kwargs)

        monkeypatch.setattr(fleet_hub_status, "board_machine_page", count_node_queries)
        monkeypatch.setattr(fleet_hub_status, "board_status_for_nodes", count_page_queries)
        status, _headers, page = _request(hub, "GET", "/view/machines?all=1", headers=_bearer())

        assert status == 200
        assert page.count(f'data-node="{NODE_A}"') == 1
        assert 'class="fleet-more"' not in page
        shown_run_ids = {run_id for run_id in run_ids if f'title="{run_id}"' in page}
        assert len(shown_run_ids) == 100
        assert "older runs omitted" in page
        assert node_queries == [True]
        assert page_queries == [[NODE_A]]

    def test_machine_page_keeps_run_that_matched_before_history_cap(self, hub):
        old_stamp = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        recent_stamp = datetime.now(timezone.utc).isoformat()
        events = [
            _event(NODE_A, "matching-active", 1, "run.started", repo="repo", harness="codex", ts=old_stamp),
            _event(NODE_A, "matching-terminal", 1, "run.completed", repo="repo", harness="codex", ts=old_stamp),
        ]
        events.extend(
            _event(
                NODE_A,
                f"newer-terminal-{index:03d}",
                1,
                "run.completed",
                repo="other",
                harness="codex",
                ts=recent_stamp,
            )
            for index in range(100)
        )
        status, _headers, text = _request(hub, "POST", "/events", headers=_bearer(), body=events)
        assert status == 200, text
        conn = fleet_hub.open_db(hub[2])
        try:
            conn.execute(
                "UPDATE events SET received_at = ? WHERE run_id IN (?, ?)",
                (old_stamp, "matching-active", "matching-terminal"),
            )
            conn.execute("UPDATE events SET received_at = ? WHERE run_id LIKE 'newer-terminal-%'", (recent_stamp,))
            conn.commit()
        finally:
            conn.close()

        status, _headers, default_page = _request(hub, "GET", "/view/machines", headers=_bearer())
        assert status == 200
        assert 'title="matching-active"' in default_page

        status, _headers, filtered_page = _request(hub, "GET", "/view/machines?all=1&repo=repo", headers=_bearer())
        assert status == 200
        assert 'title="matching-active"' in filtered_page
        assert 'title="matching-terminal"' in filtered_page

    def test_machine_page_global_cap_keeps_all_matching_runs_first(self, hub):
        stamp = datetime.now(timezone.utc).isoformat()
        node_ids = [f"00000000-0000-4000-8000-{index:012d}" for index in range(21)]
        matching_run_ids = [f"matching-{index:03d}" for index in range(100)]
        events = []
        for node_index, node_id in enumerate(node_ids):
            for run_index in range(100):
                matches = node_index == 0 or run_index == 0
                run_id = matching_run_ids[run_index] if node_index == 0 else f"node-{node_index:02d}-{run_index:03d}"
                events.append(
                    _event(
                        node_id,
                        run_id,
                        1,
                        "run.completed",
                        repo="matched" if matches else "other",
                        harness="codex",
                        ts=stamp,
                    )
                )

        for start in range(0, len(events), 1000):
            status, _headers, text = _request(
                hub, "POST", "/events", headers=_bearer(), body=events[start : start + 1000]
            )
            assert status == 200, text

        status, _headers, page = _request(hub, "GET", "/view/machines?all=1&repo=matched", headers=_bearer())

        assert status == 200
        assert all(f'title="{run_id}"' in page for run_id in matching_run_ids)

    def test_machine_page_caps_total_runs_and_marks_truncation(self, hub):
        node_ids = [f"00000000-0000-4000-8000-{index:012d}" for index in range(21)]
        stamp = datetime.now(timezone.utc).isoformat()
        events = [
            _event(
                node_id,
                f"n{node_index:02d}-r{run_index:03d}",
                1,
                "run.completed",
                repo="repo",
                harness="codex",
                ts=stamp,
            )
            for node_index, node_id in enumerate(node_ids)
            for run_index in range(100)
        ]
        for start in range(0, len(events), 1000):
            status, _headers, text = _request(
                hub, "POST", "/events", headers=_bearer(), body=events[start : start + 1000]
            )
            assert status == 200, text

        status, _headers, page = _request(hub, "GET", "/view/machines?all=1", headers=_bearer())

        assert status == 200
        assert page.count("<tr data-bucket=") == 2000
        assert "older runs omitted" in page

    @pytest.mark.xfail(
        sys.platform == "win32",
        raises=TimeoutError,
        reason="Windows loopback HTTP socket intermittently exceeds 5 seconds (#1947)",
        strict=False,
    )
    def test_machine_pages_resolve_history_seats_without_per_run_queries(self, hub, monkeypatch):
        stamp = datetime.now(timezone.utc).isoformat()
        events = [
            _event(NODE_A, "historical-seat", 1, "run.started", repo="repo", harness="codex", ts=stamp),
            _event(NODE_A, "historical-seat", 2, "run.completed", repo="repo", harness="codex", ts=stamp),
        ]
        events[1]["seat"] = "-"
        status, _headers, text = _request(hub, "POST", "/events", headers=_bearer(), body=events)
        assert status == 200, text
        statements = []
        original_open_db = fleet_hub_http.open_db

        def trace_open_db(*args, **kwargs):
            conn = original_open_db(*args, **kwargs)
            conn.set_trace_callback(statements.append)
            return conn

        monkeypatch.setattr(fleet_hub_http, "open_db", trace_open_db)

        status, _headers, filtered_page = _request(hub, "GET", "/view/machines?all=1&seat=worker", headers=_bearer())

        assert status == 200
        assert "historical-seat" in filtered_page
        assert "worker/codex" in filtered_page
        status, _headers, unfiltered_page = _request(hub, "GET", "/view/machines?all=1", headers=_bearer())
        assert status == 200
        assert "historical-seat" in unfiltered_page
        assert "worker/codex" in unfiltered_page
        assert not any("SELECT seat FROM events WHERE node_id" in statement for statement in statements)

    def test_machine_page_resolves_seat_when_latest_event_has_no_harness(self, hub):
        stamp = datetime.now(timezone.utc).isoformat()
        events = [
            _event(NODE_A, "nullable-harness-seat", 1, "run.started", repo="repo", harness="codex", ts=stamp),
            _event(NODE_A, "nullable-harness-seat", 2, "run.completed", repo="repo", harness="codex", ts=stamp),
        ]
        events[1]["harness"] = None
        events[1]["seat"] = None
        status, _headers, text = _request(hub, "POST", "/events", headers=_bearer(), body=events)
        assert status == 200, text

        status, _headers, page = _request(hub, "GET", "/view/machines?all=1&seat=worker", headers=_bearer())

        assert status == 200
        assert "nullable-harness-seat" in page
        assert ">worker<" in page

    def test_claim_only_machine_cards_are_bounded_and_paginated(self, hub, monkeypatch):
        node_ids = [f"00000000-0000-4000-8000-{index:012d}" for index in range(fleet_hub.BOARD_LIMIT + 1)]
        stamp = datetime.now(timezone.utc).isoformat()
        expires = datetime.now(timezone.utc).timestamp() + 3600
        conn = fleet_hub.open_db(hub[2])
        try:
            conn.executemany(
                "INSERT INTO claims "
                "(target, owner_node, holder_token, acquired_at, renewed_at, ttl_seconds, expires_at) "
                "VALUES (?, ?, 'test-holder-token', ?, ?, 3600, ?)",
                [(f"repo-{index:03d}", node_id, stamp, stamp, expires) for index, node_id in enumerate(node_ids)],
            )
            conn.executemany(
                "INSERT INTO claim_fences (target, generation) VALUES (?, 1)",
                [(f"repo-{index:03d}",) for index in range(len(node_ids))],
            )
            conn.commit()
        finally:
            conn.close()

        monkeypatch.setattr(fleet_hub_http.fleet_hub_messages, "unread_counts", lambda _conn: {node_ids[-1]: 7})
        _status, _headers, first = _request(hub, "GET", "/view/machines", headers=_bearer())
        assert "claim_only=1" in first
        assert "claim_offset=200" in first
        _status, _headers, second = _request(
            hub, "GET", "/view/machines?claim_only=1&claim_offset=200", headers=_bearer()
        )
        first_ids = {node_id for node_id in node_ids if f'data-node="{node_id}"' in first}
        second_ids = {node_id for node_id in node_ids if f'data-node="{node_id}"' in second}
        assert len(first_ids) == fleet_hub.BOARD_LIMIT
        assert len(second_ids) == 1
        assert first_ids.isdisjoint(second_ids)
        assert first_ids | second_ids == set(node_ids)
        assert "7 unread messages" in second

    def test_final_claim_only_page_resumes_remaining_event_pages(self, hub):
        event_ids = [f"33333333-3333-4333-8333-{index:012d}" for index in range(fleet_hub.BOARD_LIMIT + 1)]
        stamp = datetime.now(timezone.utc).isoformat()
        events = [
            _event(node_id, f"resume-event-{index}", 1, "run.dispatch.observed", repo="repo", harness="codex", ts=stamp)
            for index, node_id in enumerate(event_ids)
        ]
        status, _headers, text = _request(hub, "POST", "/events", headers=_bearer(), body=events)
        assert status == 200, text

        claim_id = "44444444-4444-4444-8444-000000000001"
        expires = datetime.now(timezone.utc).timestamp() + 3600
        conn = fleet_hub.open_db(hub[2])
        try:
            conn.execute(
                "INSERT INTO claims "
                "(target, owner_node, holder_token, acquired_at, renewed_at, ttl_seconds, expires_at) "
                "VALUES (?, ?, 'test-holder-token', ?, ?, 3600, ?)",
                ("claim-only", claim_id, stamp, stamp, expires),
            )
            conn.execute("INSERT INTO claim_fences (target, generation) VALUES ('claim-only', 1)")
            conn.commit()
        finally:
            conn.close()

        _status, _headers, claim_page = _request(
            hub, "GET", "/view/machines?claim_only=1&claim_offset=0", headers=_bearer()
        )
        assert f'data-node="{claim_id}"' in claim_page
        more = re.search(r'class="fleet-more"><a href="([^"]+)"', claim_page)
        assert more
        next_path = html.unescape(more[1])
        assert "offset=200" in next_path

        status, _headers, event_page = _request(hub, "GET", next_path, headers=_bearer())
        assert status == 200
        assert f'data-node="{event_ids[-1]}"' in event_page

    def test_more_links_cover_events_and_claim_only_nodes_once(self, hub, monkeypatch):
        count = fleet_hub.BOARD_LIMIT * 2 + 1
        event_ids = [f"11111111-1111-4111-8111-{index:012d}" for index in range(count)]
        claim_ids = [f"22222222-2222-4222-8222-{index:012d}" for index in range(count)]
        stamp = datetime.now(timezone.utc).isoformat()
        events = [
            _event(node, f"event-{index}", 1, "run.dispatch.observed", repo="events", harness="codex", ts=stamp)
            for index, node in enumerate(event_ids)
        ]
        status, _headers, text = _request(hub, "POST", "/events", headers=_bearer(), body=events)
        assert status == 200, text
        conn = fleet_hub.open_db(hub[2])
        try:
            conn.executemany(
                "INSERT INTO claims "
                "(target, owner_node, holder_token, acquired_at, renewed_at, ttl_seconds, expires_at) "
                "VALUES (?, ?, 'test-holder-token', ?, ?, 3600, ?)",
                [
                    (f"claim-{index}", node, stamp, stamp, datetime.now(timezone.utc).timestamp() + 3600)
                    for index, node in enumerate(claim_ids)
                ],
            )
            conn.executemany(
                "INSERT INTO claim_fences (target, generation) VALUES (?, 1)",
                [(f"claim-{index}",) for index in range(count)],
            )
            conn.commit()
        finally:
            conn.close()
        unread_ids = {"unread-only-fixture": 1}
        monkeypatch.setattr(fleet_hub_http.fleet_hub_messages, "unread_counts", lambda _conn: unread_ids)
        path = "/view/machines"
        visited = set()
        seen = set()
        while path:
            assert path not in visited, "pagination repeated a more link"
            visited.add(path)
            status, _headers, page = _request(hub, "GET", path, headers=_bearer())
            assert status == 200, page
            page_ids = set(re.findall(r'data-node="([^"]+)"', page))
            assert page_ids
            assert len(page_ids) <= fleet_dashboard.MACHINE_CARD_LIMIT
            assert seen.isdisjoint(page_ids), "pagination repeated machine cards"
            seen.update(page_ids)
            match = re.search(r'class="fleet-more"><a href="([^"]+)"', page)
            path = html.unescape(match[1]) if match else ""
        assert seen == set(event_ids) | set(claim_ids) | set(unread_ids)

    def test_repo_view_skips_complete_machine_node_query(self, hub, monkeypatch):
        _seed(hub)

        calls = []
        original_query = fleet_hub_status.board_machine_page

        def complete_node_query(*args, **kwargs):
            calls.append(args)
            return original_query(*args, **kwargs)

        monkeypatch.setattr(fleet_hub_status, "board_machine_page", complete_node_query)
        status, _headers, page = _request(hub, "GET", "/view/repos", headers=_bearer())
        assert status == 200
        assert "repo-a" in page
        assert calls == []

    def test_repo_view_skips_unread_message_scan(self, hub, monkeypatch):
        calls = []
        original_query = fleet_hub_http.fleet_hub_messages.unread_counts

        def unread_scan(conn):
            calls.append(conn)
            return original_query(conn)

        monkeypatch.setattr(fleet_hub_http.fleet_hub_messages, "unread_counts", unread_scan)

        status, _headers, page = _request(hub, "GET", "/view/repos", headers=_bearer())

        assert status == 200
        assert "Fleet: Repos" in page
        assert calls == []

    def test_unread_only_machine_cards_are_bounded_and_paginated(self):
        unread = {f"unread-node-{index:03d}": 1 for index in range(300)}
        existing_nodes = [{"node_id": f"existing-node-{index:03d}"} for index in range(100)]

        def render(offset):
            return fleet_dashboard.render_page(
                view="machines",
                query_string="",
                runs=[],
                claims=[],
                nodes=existing_nodes,
                started_at={},
                nonce="test",
                unread_counts=unread,
                unread_offset=offset,
            )

        first = render(0)
        second = render(100)
        third = render(200)
        first_ids = {node_id for node_id in unread if f'data-node="{node_id}"' in first}
        second_ids = {node_id for node_id in unread if f'data-node="{node_id}"' in second}
        third_ids = {node_id for node_id in unread if f'data-node="{node_id}"' in third}

        assert len(first_ids) == 100
        assert len(second_ids) == 100
        assert len(third_ids) == 100
        assert first_ids.isdisjoint(second_ids)
        assert second_ids.isdisjoint(third_ids)
        assert first_ids | second_ids | third_ids == set(unread)

    def test_unread_more_link_advances_by_unread_cards_consumed(self, hub, monkeypatch):
        now = datetime.now(timezone.utc).isoformat()
        events = [
            _event(
                f"33333333-3333-4333-8333-{index:012d}",
                f"unread-page-{index}",
                1,
                "run.started",
                repo=f"repo-{index}",
                harness="claude",
                ts=now,
            )
            for index in range(100)
        ]
        status, _headers, text = _request(hub, "POST", "/events", headers=_bearer(), body=events)
        assert status == 200, text
        unread = {f"unread-node-{index:03d}": 1 for index in range(300)}
        monkeypatch.setattr(fleet_hub_http.fleet_hub_messages, "unread_counts", lambda _conn: unread)

        _status, _headers, page = _request(hub, "GET", "/view/machines", headers=_bearer())

        assert "unread_offset=100" in page
        assert "unread_only=1" in page
        assert 'class="fleet-more"' in page

    def test_full_machine_page_links_to_unread_only_cards(self, hub, monkeypatch):
        now = datetime.now(timezone.utc).isoformat()
        known_node_ids = [f"44444444-4444-4444-8444-{index:012d}" for index in range(fleet_hub.BOARD_LIMIT)]
        events = [
            _event(
                node_id,
                f"full-page-{index}",
                1,
                "run.dispatch.observed",
                repo=f"repo-{index}",
                harness="claude",
                ts=now,
            )
            for index, node_id in enumerate(known_node_ids)
        ]
        status, _headers, text = _request(hub, "POST", "/events", headers=_bearer(), body=events)
        assert status == 200, text
        monkeypatch.setattr(
            fleet_hub_http.fleet_hub_messages,
            "unread_counts",
            lambda _conn: {**dict.fromkeys(known_node_ids, 1), "unread-only-node": 1},
        )

        _status, _headers, first = _request(hub, "GET", "/view/machines", headers=_bearer())
        assert "unread_only=1" in first
        _status, _headers, second = _request(hub, "GET", "/view/machines?unread_only=1", headers=_bearer())
        assert 'data-node="unread-only-node"' in second
        assert second.count('class="machine-card"') == 1
        assert 'class="fleet-more"' not in second

    def test_node_filter_paginates_more_than_two_hundred_unread_cards(self, hub, monkeypatch):
        unread = {f"filtered-target-{index:03d}": 1 for index in range(fleet_hub.BOARD_LIMIT + 1)}
        monkeypatch.setattr(fleet_hub_http.fleet_hub_messages, "unread_counts", lambda _conn: unread)

        _status, _headers, first = _request(hub, "GET", "/view/machines?node=filtered-target", headers=_bearer())
        assert "unread_offset=200" in first
        assert "node=filtered-target" in first
        _status, _headers, second = _request(
            hub,
            "GET",
            "/view/machines?node=filtered-target&unread_offset=200&unread_only=1",
            headers=_bearer(),
        )
        assert 'data-node="filtered-target-200"' in second
        assert 'class="fleet-more"' not in second
