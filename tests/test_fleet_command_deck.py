"""Tests for the fleet command deck module (config, projections, bounded reads)."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import http.client
import threading
import time
from contextlib import contextmanager

import pytest

from brigade import fleet_command_deck as deck
from brigade import fleet_hub

NODE_A = "11111111-1111-4111-8111-111111111111"
NODE_B = "22222222-2222-4222-8222-222222222222"
NOW = datetime(2026, 8, 25, 12, 0, 0, tzinfo=timezone.utc)

_EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    node_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    digest TEXT NOT NULL,
    repo TEXT,
    seat TEXT,
    harness TEXT,
    state TEXT NOT NULL,
    ts TEXT NOT NULL,
    received_at TEXT NOT NULL,
    PRIMARY KEY (node_id, run_id, sequence, digest)
);
"""


def _ts(offset_seconds: int) -> str:
    return (NOW - timedelta(seconds=offset_seconds)).isoformat()


def _insert(
    conn: sqlite3.Connection,
    node_id: str,
    run_id: str,
    sequence: int,
    state: str,
    *,
    ts: str | None = None,
    repo: str = "repo",
) -> None:
    conn.execute(
        "INSERT INTO events (node_id, run_id, sequence, digest, repo, seat, harness, state, ts, received_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            node_id,
            run_id,
            sequence,
            f"{node_id[:8]}-{run_id}-{sequence:04d}",
            repo,
            "seat",
            "claude",
            state,
            ts if ts is not None else _ts(10),
            _ts(5),
        ),
    )


@pytest.fixture()
def conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(_EVENTS_SCHEMA)
    yield conn
    conn.close()


def seed_events(
    conn: sqlite3.Connection, *, terminal_then_older_live: bool, extra_live: int, extra_terminal: int
) -> None:
    if terminal_then_older_live:
        # Terminal latest row for one key, with an older live event underneath:
        # the key must be excluded from live runs and appear once in outcomes.
        _insert(conn, NODE_A, "base", 1, "run.created", ts=_ts(600), repo="repo")
        _insert(conn, NODE_A, "base", 2, "run.completed", ts=_ts(300), repo="repo")
    for index in range(extra_live):
        node = NODE_A if index % 2 == 0 else NODE_B
        _insert(conn, node, f"live-{index:04d}", 1, "run.created", ts=_ts(30 + index), repo=f"repo-{index % 7}")
    for index in range(extra_terminal):
        _insert(conn, NODE_B, f"done-{index:04d}", 1, "run.completed", ts=_ts(120 + index), repo="repo")


def test_load_config_validates_and_strips_controls(tmp_path):
    path = tmp_path / "deck.json"
    path.write_text(json.dumps({"stations": [{"node_id": NODE_A, "name": "Alpha\x00", "capacity": 6}]}))
    assert deck.load_config(path).stations == (deck.StationConfig(NODE_A, "Alpha", 6),)
    for raw in ({}, {"stations": []}, {"stations": [{"node_id": "unknown", "name": "A", "capacity": 1}]}):
        path.write_text(json.dumps(raw))
        with pytest.raises(deck.DeckConfigError):
            deck.load_config(path)


def test_bucket_and_collision_rules():
    assert deck.bucket_for("run.dispatch.failed", age_seconds=1, stale_after_seconds=1800) == "failed"
    assert deck.bucket_for("run.paused", age_seconds=3600, stale_after_seconds=1800) == "awaiting approval"
    assert deck.bucket_for("run.created", age_seconds=1, stale_after_seconds=1800) == "queued"
    rows = [
        deck.LiveRun(NODE_A, "a", "repo", "", "", "run.created", "queued", 1, 1),
        deck.LiveRun(NODE_B, "b", "repo", "", "", "run.created", "queued", 1, 1),
    ]
    assert deck.collides("repo", rows, {"repo": deck.Claim("repo", NODE_A, "", 60)}) is True


def test_capped_queries_select_latest_then_limit(conn):
    seed_events(conn, terminal_then_older_live=True, extra_live=201, extra_terminal=101)
    assert len(deck.fetch_live_runs(conn, now=NOW, stale_after_seconds=1800)) == 200
    assert len(deck.fetch_outcomes(conn, outcome_window=100)) == 100
    assert all(
        row.state not in deck.TERMINAL_STATES for row in deck.fetch_live_runs(conn, now=NOW, stale_after_seconds=1800)
    )


def test_terminal_suffix_states_never_appear_in_live_reads_or_outcomes(conn):
    states = (
        "run.dispatch.completed",
        "run.dispatch.interrupted",
        "run.dispatch.failed",
        "worker.cancelled",
        "worker.canceled",
        "run.timed_out",
        "engine.timeout",
    )
    for index, state in enumerate(states):
        _insert(conn, NODE_A, f"key-{index}", 1, "run.created", ts=_ts(600), repo=f"repo-{index}")
        _insert(conn, NODE_A, f"key-{index}", 2, state, ts=_ts(60), repo=f"repo-{index}")
    assert deck.fetch_live_runs(conn, now=NOW, stale_after_seconds=1800) == []
    failed = deck.fetch_failed_outcomes(conn, now=NOW, lookback_seconds=86400)
    assert sorted(run.state for run in failed) == [
        "engine.timeout",
        "run.dispatch.failed",
        "run.timed_out",
        "worker.canceled",
        "worker.cancelled",
    ]
    assert all(run.bucket == "failed" for run in failed)
    outcomes = deck.fetch_outcomes(conn, outcome_window=20)
    # Every terminal suffix reaches outcomes except the internal milestones,
    # which are terminal for the event stream but noise for an operator.
    assert sorted(run.state for run in outcomes) == [
        "engine.timeout",
        "run.dispatch.failed",
        "run.dispatch.interrupted",
        "run.timed_out",
        "worker.canceled",
        "worker.cancelled",
    ]
    assert "run.dispatch.completed" in deck.INTERNAL_OUTCOME_STATES
    assert all(run.state not in deck.INTERNAL_OUTCOME_STATES for run in outcomes)
    for state in states:
        assert deck.is_terminal_state(state)
    assert not deck.is_terminal_state("run.unfailed")


def test_unfailed_latest_row_remains_live(conn):
    _insert(conn, NODE_A, "unfailed", 1, "run.unfailed", ts=_ts(30), repo="repo-unfailed")
    runs = deck.fetch_live_runs(conn, now=NOW, stale_after_seconds=1800)
    assert [run.run_id for run in runs] == ["unfailed"]


def test_defensive_stale_runs_stay_in_rail_but_out_of_active_projections():
    config = deck.DeckConfig(
        stations=(deck.StationConfig(NODE_A, "Alpha", 2), deck.StationConfig(NODE_B, "Bravo", 2)),
        stale_after_seconds=1800,
    )
    stale_run = deck.LiveRun(
        node_id=NODE_B,
        run_id="ghost",
        repo="shared",
        seat="seat",
        harness="claude",
        state="run.started",
        bucket="stale",
        age_seconds=3600,
        elapsed_seconds=3600,
    )
    stale_only = deck.LiveRun(
        node_id=NODE_A,
        run_id="stale-only",
        repo="stale-only",
        seat="seat",
        harness="claude",
        state="run.started",
        bucket="stale",
        age_seconds=3600,
        elapsed_seconds=3600,
    )
    fresh_run = deck.LiveRun(
        node_id=NODE_A,
        run_id="fresh",
        repo="shared",
        seat="seat",
        harness="claude",
        state="run.started",
        bucket="running",
        age_seconds=30,
        elapsed_seconds=30,
    )
    view = deck.build_view(
        config,
        live_runs=[stale_run, stale_only, fresh_run],
        claims=[],
        enrolled_labels={NODE_A: "Alpha", NODE_B: "Bravo"},
        last_heard={},
        outcomes=[],
        failed_outcomes=[],
        observers=[],
        now=NOW,
    )
    alpha, bravo = view.stations
    assert [tile.run.run_id for tile in alpha.tiles] == ["fresh"]
    assert alpha.busy == 1
    assert bravo.tiles == () and bravo.busy == 0
    assert [entry.run_id for entry in view.rail] == ["ghost", "stale-only"]
    assert [(row.target, tuple(run.run_id for run in row.live), row.collision) for row in view.repos] == [
        ("shared", ("fresh",), False)
    ]


def test_fetch_live_runs_drops_old_nonterminal_latest_rows(conn):
    _insert(conn, NODE_A, "ancient", 1, "run.started", ts=_ts(7200), repo="repo-old")
    _insert(conn, NODE_A, "fresh", 1, "run.started", ts=_ts(30), repo="repo-new")
    runs = deck.fetch_live_runs(conn, now=NOW, stale_after_seconds=1800)
    assert [run.run_id for run in runs] == ["fresh"]


def test_outcomes_hide_internal_milestones_and_keep_distinct_run_ids(conn):
    _insert(conn, NODE_A, "dispatch.alpha", 1, "run.dispatch.completed", repo="repo-a")
    _insert(conn, NODE_A, "dispatch.beta", 1, "run.synthesis.completed", repo="repo-b")
    _insert(conn, NODE_A, "external.alpha", 1, "run.completed", repo="repo-a")
    _insert(conn, NODE_A, "external.beta", 1, "provider.completed", repo="repo-b")

    assert [(run.run_id, run.state) for run in deck.fetch_outcomes(conn, outcome_window=10)] == [
        ("external.alpha", "run.completed"),
        ("external.beta", "provider.completed"),
    ]


def test_failed_attention_keeps_newest_per_node_repo_and_suppresses_active_retries(conn):
    _insert(conn, NODE_A, "dispatch.old", 1, "run.dispatch.failed", ts=_ts(300), repo="retrying")
    _insert(conn, NODE_A, "dispatch.active", 1, "run.dispatch.observed", ts=_ts(30), repo="retrying")
    _insert(conn, NODE_A, "failure.old", 1, "run.dispatch.failed", ts=_ts(240), repo="same-repo")
    _insert(conn, NODE_A, "failure.new", 1, "run.dispatch.failed", ts=_ts(60), repo="same-repo")
    _insert(conn, NODE_A, "prefix.alpha", 1, "run.dispatch.failed", ts=_ts(90), repo="repo-a")
    _insert(conn, NODE_A, "prefix.beta", 1, "run.dispatch.failed", ts=_ts(120), repo="repo-b")

    assert [(run.run_id, run.repo) for run in deck.fetch_failed_outcomes(conn, now=NOW, lookback_seconds=86400)] == [
        ("failure.new", "same-repo"),
        ("prefix.alpha", "repo-a"),
        ("prefix.beta", "repo-b"),
    ]


# --- HTTP integration: hub-served Command Deck (task 2) ----------------------

TOKEN = "test-token-12345"
HOLDER_TOKEN = "secret-holder-token-zz"
NODE_C = "33333333-3333-4333-8333-333333333333"

CONFIG = {
    "stations": [
        {"node_id": NODE_A, "name": "Alpha", "capacity": 4},
        {"node_id": NODE_B, "capacity": 2},
    ]
}


@contextmanager
def _start_hub(tmp_path, config: dict | None):
    db = tmp_path / "hub" / "fleet.db"
    config_path = None
    if config is not None:
        config_path = tmp_path / "deck.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
    server = fleet_hub.make_server(
        "127.0.0.1", 0, db, TOKEN, allow_admin_writes=True, deck_config_path=config_path
    )  # content-guard: allow loopback-ipv4
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield ("127.0.0.1", server.server_address[1]), db  # content-guard: allow loopback-ipv4
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _request(hub, method: str, path: str, *, headers: dict | None = None, body=None):
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


def _login_cookie(hub, path: str = "/") -> str:
    status, headers, _text = _request(hub, "GET", f"{path}?token={TOKEN}")
    assert status == 303
    return headers["set-cookie"].split(";")[0]


def _seed_run(hub, node: str, run_id: str, state: str, *, repo: str, ts: str | None = None) -> None:
    event = {
        "node_id": node,
        "run_id": run_id,
        "repo": repo,
        "seat": "worker",
        "harness": "claude",
        "state": state,
        "ts": ts or datetime.now(timezone.utc).isoformat(),
        "sequence": 1,
        "digest": f"d-{node[:4]}-{run_id}",
    }
    status, _headers, text = _request(hub, "POST", "/events", headers=_bearer(), body=[event])
    assert status == 200, text


def _seed_claim(hub, target: str, *, node: str = NODE_B, ttl: int = 900) -> None:
    claim = {"action": "acquire", "target": target, "node_id": node, "holder": HOLDER_TOKEN, "ttl_seconds": ttl}
    status, _headers, text = _request(hub, "POST", "/claims", headers=_bearer(), body=claim)
    assert status == 200, text


def test_deck_requires_bearer_or_cookie(tmp_path):
    with _start_hub(tmp_path, CONFIG) as (hub, _db):
        assert _request(hub, "GET", "/deck")[0] == 401
        assert _request(hub, "GET", "/deck/repos", headers=_bearer())[0] == 200
        cookie = _login_cookie(hub, "/deck")
        assert _request(hub, "GET", "/deck", headers={"Cookie": cookie})[0] == 200
        assert _request(hub, "GET", "/status", headers={"Cookie": cookie})[0] == 401


def test_command_deck_is_root_and_legacy_boards_stay_under_view(tmp_path):
    with _start_hub(tmp_path, CONFIG) as (hub, _db):
        root = _request(hub, "GET", "/", headers=_bearer())[2]
        alias = _request(hub, "GET", "/deck", headers=_bearer())[2]
        assert "Command Deck" in root and "Alpha" in root
        assert "Command Deck" in alias and "Alpha" in alias
        assert 'href="/view/machines"' in root and 'href="/view/repos"' in root
        # Root is the deck itself, so href="/" is the valid self-link here; what
        # must not exist is a legacy board served from root.
        assert '<a href="/">deck</a>' in root
        assert 'href="/machines"' not in root and 'href="/repos"' not in root
        for path, title in (
            ("/view/machines", "Fleet: Machines"),
            ("/view/repos", "Fleet: Repos"),
        ):
            page = _request(hub, "GET", path, headers=_bearer())[2]
            assert title in page
            assert 'href="/"' not in page


def test_headers_secrets_and_responsive_css(tmp_path):
    with _start_hub(tmp_path, CONFIG) as (hub, _db):
        status, headers, body = _request(hub, "GET", "/deck", headers=_bearer())
        assert status == 200 and headers["cache-control"] == "no-store"
        assert "frame-ancestors 'none'" in headers["content-security-policy"]
        assert all(secret not in body for secret in (TOKEN, HOLDER_TOKEN, _login_cookie(hub).split("=", 1)[1]))
        assert "overflow-wrap: anywhere" in body
        assert "table-layout: fixed" in body
        assert "@media (max-width: 700px)" in body
        assert ".stations { grid-template-columns: 1fr; }" in body


def test_invalid_config_refused_at_make_server(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"stations": []}), encoding="utf-8")
    db = tmp_path / "hub" / "fleet.db"
    with pytest.raises(deck.DeckConfigError):
        fleet_hub.make_server("127.0.0.1", 0, db, TOKEN, allow_admin_writes=True, deck_config_path=bad)
    assert not db.exists()


def test_station_order_labels_capacity_and_timeline_bounds(tmp_path):
    config = {
        "stations": [
            {"node_id": NODE_B, "capacity": 1},
            {"node_id": NODE_A, "name": "Alpha", "capacity": 2},
        ],
        "outcome_window": 2,
    }
    with _start_hub(tmp_path, config) as (hub, _db):
        status, _headers, text = _request(
            hub,
            "POST",
            "/nodes",
            headers=_bearer(),
            body={"action": "add", "node_id": NODE_B, "label": "BravoBox"},
        )
        assert status == 200, text
        status, _headers, text = _request(
            hub, "POST", "/nodes", headers=_bearer(), body={"action": "add", "node_id": NODE_A}
        )
        assert status == 200, text
        _seed_run(hub, NODE_A, "ra", "run.created", repo="repo-a")
        _seed_run(hub, NODE_B, "rb", "run.created", repo="repo-b")
        for index in range(3):
            _seed_run(hub, NODE_A, f"done-{index}", "run.completed", repo="repo-a")
        _seed_claim(hub, "repo-b", node=NODE_B)
        _status, _headers, body = _request(hub, "GET", "/deck", headers=_bearer())
        # Fixed station order follows the config, not events.
        assert body.index(NODE_B[:12]) < body.index("Alpha")
        # Unnamed station falls back to the enrolled label then node id prefix.
        heading = f'title="{NODE_B}"'
        assert heading in body and "BravoBox" in body
        assert "not enrolled" not in body
        # Capacity and busy counts.
        assert "1/1 busy" in body and "1/2 busy" in body
        # Terminal-only timeline, bounded by outcome_window.
        assert "Recent outcomes" in body
        assert body.count("run.completed") <= 2


def test_collision_markers_and_observers_and_expired_claims(tmp_path):
    with _start_hub(tmp_path, CONFIG) as (hub, _db):
        for node_id, label in ((NODE_A, "Rocinante"), (NODE_B, "Shadowfax")):
            status, _headers, text = _request(
                hub,
                "POST",
                "/nodes",
                headers=_bearer(),
                body={"action": "add", "node_id": node_id, "label": label},
            )
            assert status == 200, text
        _seed_run(hub, NODE_A, "ca", "run.created", repo="shared")
        _seed_run(hub, NODE_B, "cb", "run.created", repo="shared")
        _seed_claim(hub, "shared", node=NODE_A)
        _seed_claim(hub, "gone", node=NODE_A, ttl=1)
        _seed_run(hub, NODE_C, "obs", "run.created", repo="observer-repo")
        time.sleep(1.5)
        _status, _headers, deck_body = _request(hub, "GET", "/deck", headers=_bearer())
        assert deck_body.count("! collision") >= 1
        assert "collision" in deck_body.split('id="rail"')[1].split("</section>")[0]
        rail = deck_body.split('id="rail"')[1].split("</section>")[0]
        assert "shared" in rail and "held by Rocinante" in rail
        assert f"held by {NODE_A}" not in rail
        _status, _headers, repos_body = _request(hub, "GET", "/deck/repos", headers=_bearer())
        first_row = repos_body.split("<tbody>")[1].split("<tr>")[1]
        assert "! collision" in first_row and "shared" in first_row
        # Expired claims are absent.
        assert "gone" not in repos_body
        # Observers are excluded from station totals.
        assert "observer-repo" not in deck_body.split('id="rail"')[1]
        assert "Other observers" in deck_body and NODE_C[:12] in deck_body


def test_hostile_labels_are_escaped_and_no_config_hint(tmp_path):
    hostile = {
        "stations": [
            {
                "node_id": NODE_A,
                "name": '<script>alert("x")</script>',
                "capacity": 3,
            }
        ]
    }
    with _start_hub(tmp_path, hostile) as (hub, _db):
        _status, _headers, body = _request(hub, "GET", "/deck", headers=_bearer())
        assert "<script>" not in body
        assert "&lt;script&gt;" in body
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"stations": [{"node_id": NODE_A, "name": "Solo", "capacity": 1}]}), encoding="utf-8")
    with _start_hub(tmp_path / "no-config", None) as (hub, _db):
        _status, _headers, body = _request(hub, "GET", "/deck", headers=_bearer())
        assert "No stations configured" in body


def test_cloud_workers_render_safe_active_leases_without_using_station_capacity(tmp_path):
    with _start_hub(tmp_path, CONFIG) as (hub, db_path):
        conn = fleet_hub.open_db(db_path)
        try:
            status, admitted = fleet_hub.handle_cloud(
                conn,
                {
                    "action": "admit",
                    "provider": "cursor-cloud",
                    "lease_id": "lease-cursor",
                    "node_id": NODE_A,
                    "holder": HOLDER_TOKEN,
                    "repo": '<repo & "one">',
                    "label": '<task & "one">',
                    "prompt_hash": "a" * 64,
                },
                config=deck.load_config(tmp_path / "deck.json"),
            )
            assert status == 200 and admitted["admitted"] is True
            status, bound = fleet_hub.handle_cloud(
                conn,
                {
                    "action": "bind",
                    "lease_id": "lease-cursor",
                    "node_id": NODE_A,
                    "holder": HOLDER_TOKEN,
                    "provider_task_id": '<task-id & "one">',
                    "artifact_ref": "https://example.invalid/task/1",
                },
                config=deck.load_config(tmp_path / "deck.json"),
            )
            assert status == 200 and bound["bound"] is True
        finally:
            conn.close()

        _seed_run(hub, NODE_A, "physical", "run.created", repo="physical-repo")
        status, headers, body = _request(hub, "GET", "/", headers=_bearer())
        assert status == 200 and "frame-ancestors 'none'" in headers["content-security-policy"]
        assert "Cloud workers" in body
        assert "<h3>cursor</h3>" in body and ">1/3<" in body
        assert "closed" in body
        assert "<h3>codex</h3>" in body and ">0/2<" in body and "No active leases." in body
        assert "1/4 busy" in body
        assert "&lt;task &amp; &quot;one&quot;&gt;" in body
        assert "&lt;repo &amp; &quot;one&quot;&gt;" in body
        assert "&lt;task-id &amp; &quot;one&quot;&gt;" in body
        assert HOLDER_TOKEN not in body and "a" * 64 not in body


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


def test_command_deck_asset_routes_and_metadata(tmp_path):
    with _start_hub(tmp_path, CONFIG) as (hub, _db):
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

        for path in ("/", "/deck", "/deck/repos"):
            status, _headers, body = _request(hub, "GET", path, headers=_bearer())
            assert status == 200, path
            assert '<link rel="icon" type="image/x-icon" href="/favicon.ico"' in body
            assert '<link rel="icon" type="image/png" sizes="32x32" href="/favicon-32x32.png"' in body
            assert '<link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png"' in body
            assert '<link rel="manifest" href="/site.webmanifest"' in body
            assert '<meta name="theme-color" content="#111617"' in body
            assert '<meta name="application-name" content="Fleet Hub"' in body
            assert '<meta name="apple-mobile-web-app-title" content="Fleet Hub"' in body


class TestDeckCli:
    def test_flag_precedence_env_fallback_and_forwarding(self, tmp_path, monkeypatch):
        from brigade import cli

        captured: dict = {}
        monkeypatch.setattr(fleet_hub, "run", lambda **kw: captured.update(kw) or 0)
        flag = tmp_path / "flag.json"
        env = tmp_path / "env.json"
        monkeypatch.setenv("BRIGADE_FLEET_DECK_CONFIG", str(env))
        argv = ["fleet", "serve", "--host", "127.0.0.1", "--db", str(tmp_path / "x.db"), "--deck-config", str(flag)]
        assert cli.main(argv) == 0
        assert captured["deck_config_path"] == flag
        monkeypatch.delenv("BRIGADE_FLEET_DECK_CONFIG")
        argv.pop()
        argv.pop()
        assert cli.main(argv) == 0
        assert captured["deck_config_path"] is None
        monkeypatch.setenv("BRIGADE_FLEET_DECK_CONFIG", str(env))
        assert cli.main(argv) == 0
        assert captured["deck_config_path"] == env
        monkeypatch.setenv("BRIGADE_FLEET_DECK_CONFIG", "   ")
        assert cli.main(argv) == 0
        assert captured["deck_config_path"] is None
