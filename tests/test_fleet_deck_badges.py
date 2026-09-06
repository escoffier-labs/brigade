"""Deck harness/model badges and the worker-run seat backfill."""

import sqlite3
from datetime import datetime, timezone

import pytest

from brigade import fleet_command_deck as deck

NODE_A = "11111111-1111-4111-8111-111111111111"
NOW = datetime(2026, 9, 6, 12, 0, 0, tzinfo=timezone.utc)

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
    repo_identity TEXT,
    exit_status INTEGER,
    capability_fingerprint TEXT,
    PRIMARY KEY (node_id, run_id, sequence, digest)
);
"""

_MODEL_POLICY_SCHEMA = """
CREATE TABLE IF NOT EXISTS model_policy (
    seat TEXT NOT NULL PRIMARY KEY,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    reasoning TEXT NOT NULL DEFAULT 'none',
    enabled INTEGER NOT NULL,
    limit_count INTEGER,
    brigade_cli TEXT NOT NULL DEFAULT '',
    brigade_model TEXT NOT NULL DEFAULT '',
    t3_instance_id TEXT NOT NULL DEFAULT '',
    t3_service_tier TEXT NOT NULL DEFAULT '',
    notes TEXT,
    updated_at TEXT NOT NULL
);
"""


@pytest.fixture()
def conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(_EVENTS_SCHEMA)
    conn.execute(_MODEL_POLICY_SCHEMA)
    yield conn
    conn.close()


def _event(
    conn: sqlite3.Connection,
    run_id: str,
    sequence: int,
    state: str,
    *,
    seat: str | None = None,
    harness: str | None = None,
) -> None:
    stamp = NOW.isoformat()
    conn.execute(
        "INSERT INTO events (node_id, run_id, sequence, digest, repo, seat, harness,"
        " state, ts, received_at, repo_identity)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (NODE_A, run_id, sequence, f"digest-{run_id}-{sequence}", "repo", seat, harness, state, stamp, stamp, ""),
    )


def _policy(
    conn: sqlite3.Connection,
    seat: str,
    provider: str,
    model: str,
    *,
    brigade_cli: str = "",
    brigade_model: str = "",
) -> None:
    conn.execute(
        "INSERT INTO model_policy (seat, provider, model, reasoning, enabled, brigade_cli,"
        " brigade_model, updated_at) VALUES (?, ?, ?, 'high', 1, ?, ?, ?)",
        (seat, provider, model, brigade_cli, brigade_model, NOW.isoformat()),
    )


def test_brand_lookups_known_and_unknown() -> None:
    claude = deck.harness_brand("claude")
    assert claude.label == "Claude" and claude.accent == "#8a5a2b"
    assert "<svg" in claude.svg and "viewBox" in claude.svg
    assert "<script" not in claude.svg
    codex = deck.harness_brand("codex")
    assert codex.label == "Codex"
    assert codex.svg == deck.provider_brand("openai").svg != ""
    assert "viewBox" in codex.svg
    assert deck.harness_brand("opencode").svg != ""
    assert deck.harness_brand("t3-fleet").label == "T3 Fleet"
    assert ">T3<" in deck.harness_brand("t3-fleet").svg
    grokbot = deck.harness_brand("grokbot")
    assert grokbot.svg == deck.harness_brand("grok").svg.replace(
        'fill="currentColor">', 'fill="currentColor"><title>bot</title>'
    )
    assert "<title>bot</title>" in grokbot.svg
    assert deck.provider_brand("anthropic").svg == claude.svg
    assert deck.provider_brand("xai").svg == deck.harness_brand("grok").svg
    assert deck.provider_brand("google").svg == deck.harness_brand("antigravity").svg
    assert "viewBox" in deck.provider_brand("google").svg
    for unknown in ("nope", "", "   "):
        assert deck.harness_brand(unknown) == deck.provider_brand(unknown)
        assert deck.harness_brand(unknown).label == "??"
        assert deck.harness_brand(unknown).svg == ""
        assert deck.harness_brand(unknown).accent == "#4b5563"
    # Normalization: underscores and cloud suffixes still resolve.
    assert deck.harness_brand("T3_Fleet").label == "T3 Fleet"
    assert deck.provider_brand("cursor-cloud").label == "Cursor"
    assert "<svg" in deck.provider_brand("cursor-cloud").svg


def test_badge_html_embeds_svg_and_escapes_title() -> None:
    html = deck.harness_badge_html("claude")
    assert "<svg" in html and "viewBox" in html
    assert "<script" not in html
    assert 'title="claude"' in html and 'aria-label="Claude"' in html
    assert 'class="badge"' in html


def test_hostile_brand_names_are_escaped() -> None:
    hostile = '<script>alert("x")</script>'
    harness_html = deck.harness_badge_html(hostile)
    provider_html = deck.provider_badge_html(hostile)
    for html in (harness_html, provider_html):
        assert "<script>" not in html
        assert "&lt;script&gt;" in html
        assert "??" in html
        assert 'class="badge"' in html


def test_worker_run_seat_backfilled_from_dispatch_event(conn: sqlite3.Connection) -> None:
    # run.dispatch.requested names the seat; the later status event is blank.
    _event(conn, "worker-1", 1, "run.dispatch.requested", seat="muse13", harness="opencode")
    _event(conn, "worker-1", 2, "run.started")
    _policy(
        conn,
        "muse13",
        "opencode",
        "opencode-muse-spark-1.3-contributor-free",
        brigade_cli="opencode",
        brigade_model="opencode/muse-spark-1.3-contributor-free",
    )
    (run,) = deck.fetch_live_runs(conn, now=NOW, stale_after_seconds=1800)
    assert run.seat == "muse13"
    assert run.harness == "opencode"
    assert run.provider == "opencode"
    assert run.model == "opencode-muse-spark-1.3-contributor-free"
    assert run.launch_model == "opencode/muse-spark-1.3-contributor-free"


def test_genuinely_absent_fields_stay_unknown(conn: sqlite3.Connection) -> None:
    _event(conn, "ghost-1", 1, "run.started")
    (run,) = deck.fetch_live_runs(conn, now=NOW, stale_after_seconds=1800)
    assert run.seat == ""
    assert run.harness == ""
    assert run.provider == ""
    html = deck._tile_html(deck.Tile(run=run, claim=None, collision=False))
    assert "unknown" in html
    assert ">/" not in html.replace("load/capacity", "")


def test_tile_falls_back_to_claim_conductor_for_seat() -> None:
    run = deck.LiveRun(
        node_id=NODE_A,
        run_id="worker-9",
        repo="repo",
        seat="",
        harness="",
        state="run.started",
        bucket="running",
        age_seconds=10,
        elapsed_seconds=10,
    )
    claim = deck.Claim(target="repo", owner_node="node-label", owner_conductor="muse13", ttl_remaining=887)
    html = deck._tile_html(deck.Tile(run=run, claim=claim, collision=False))
    assert "muse13" in html
    assert "seat /" not in html


def test_card_renders_both_badges_and_model_text() -> None:
    run = deck.LiveRun(
        node_id=NODE_A,
        run_id="run-claude-1",
        repo="repo",
        seat="claude_standby",
        harness="claude",
        state="run.started",
        bucket="running",
        age_seconds=10,
        elapsed_seconds=10,
        provider="anthropic",
        model="opus",
        launch_model="opus",
    )
    html = deck._tile_html(deck.Tile(run=run, claim=None, collision=False))
    assert html.count("<svg") == 2
    assert 'title="claude"' in html and 'aria-label="Claude"' in html
    assert 'title="anthropic"' in html and 'aria-label="Anthropic"' in html
    assert "opus" in html
    assert "claude_standby" in html
    # Model name sits in the right-hand column beneath the badges.
    assert html.index("tile-model") < html.index("tile-facts")
    assert '<p class="tile-model">opus</p>' in html


def test_card_snapshot() -> None:
    run = deck.LiveRun(
        node_id=NODE_A,
        run_id="run-muse13-1",
        repo="repo",
        seat="muse13",
        harness="opencode",
        state="run.started",
        bucket="running",
        age_seconds=10,
        elapsed_seconds=10,
        provider="opencode",
        model="opencode-muse-spark-1.3-contributor-free",
        launch_model="opencode/muse-spark-1.3-contributor-free",
    )
    html = deck._tile_html(deck.Tile(run=run, claim=None, collision=False))
    oc = deck.harness_brand("opencode")
    assert oc.svg != ""
    assert html == (
        '<article class="tile tile--running" data-elapsed="10">'
        '<header class="tile-head"><p class="repo-name">repo</p>'
        '<div class="tile-side"><p class="state">running</p>'
        '<p class="tile-badges">'
        '<span class="badge" style="background:#2b6cb0" title="opencode" aria-label="OpenCode">' + oc.svg + "</span>"
        '<span class="badge" style="background:#2b6cb0" title="opencode" aria-label="OpenCode">' + oc.svg + "</span>"
        "</p>"
        '<p class="tile-model">opencode/muse-spark-1.3-contributor-free</p>'
        "</div></header>"
        '<dl class="tile-facts">'
        "<dt>seat</dt><dd>muse13/opencode</dd>"
        "<dt>model</dt><dd>opencode/muse-spark-1.3-contributor-free</dd>"
        '<dt>elapsed</dt><dd class="elapsed">10s</dd>'
        '<dt>run</dt><dd class="run-id" title="run-muse13-1">run-muse13-1</dd>'
        "</dl></article>"
    )


def test_cloud_and_session_badges() -> None:
    worker = deck.CloudWorker(provider="cursor", used=1, limit=3, circuit_state="closed", leases=())
    html = deck._cloud_worker_html(worker)
    assert 'title="cursor"' in html and 'aria-label="Cursor"' in html and "<svg" in html
    session = deck.InteractiveSession(
        node_id=NODE_A,
        harness="claude",
        session_id="sess-1",
        repo_identity="github.com/example/project",
        identity_scope="fleet",
        repo_label="project",
        checkout_path="/tmp/project",
        branch="main",
        dirty_paths=("src/a.py",),
        dirty_truncated=False,
    )
    session_html = deck._interactive_session_html(session)
    assert "<svg" in session_html and 'aria-label="Claude"' in session_html
    unknown_session = deck.InteractiveSession(
        node_id=NODE_A,
        harness="",
        session_id="sess-2",
        repo_identity="github.com/example/other",
        identity_scope="fleet",
        repo_label="other",
        checkout_path="/tmp/other",
        branch=None,
        dirty_paths=(),
        dirty_truncated=False,
    )
    unknown_html = deck._interactive_session_html(unknown_session)
    assert ">??<" in unknown_html
