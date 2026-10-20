"""Deck harness/model badges and the worker-run seat backfill."""

import re
import sqlite3
from datetime import datetime, timezone
from xml.etree import ElementTree

import pytest

from brigade import fleet_command_deck as deck
from brigade import fleet_deck_brands as brands
from brigade import fleet_deck_overview as overview

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
    badge = ElementTree.fromstring(html)
    assert "badge" in badge.attrib["class"].split()
    assert all("style" not in element.attrib for element in badge.iter())


def test_hostile_brand_names_are_escaped() -> None:
    hostile = '<script>alert("x")</script>'
    harness_html = deck.harness_badge_html(hostile)
    provider_html = deck.provider_badge_html(hostile)
    for html in (harness_html, provider_html):
        assert "<script>" not in html
        assert "&lt;script&gt;" in html
        assert "??" in html
        badge = ElementTree.fromstring(html)
        assert "badge" in badge.attrib["class"].split()
        assert all("style" not in element.attrib for element in badge.iter())


def test_brand_table_palette_and_svg_are_csp_compliant() -> None:
    for brand in [*brands.BRANDS.values(), brands.UNKNOWN_BRAND]:
        assert re.fullmatch(r"#[0-9a-f]{6}", brand.accent), brand.label
        if brand.svg:
            svg = ElementTree.fromstring(brand.svg)
            assert all("style" not in element.attrib for element in svg.iter()), brand.label
            assert all(element.tag not in ("style", "script") for element in svg.iter()), brand.label


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


def _station_view(*tiles: deck.Tile) -> deck.DeckView:
    station = deck.StationView(
        station=deck.StationConfig(NODE_A, "Example", 4),
        label="Example",
        enrolled=True,
        busy=len(tiles),
        last_heard=None,
        tiles=tiles,
    )
    return deck.DeckView(stations=(station,), rail=(), repos=(), outcomes=(), observers=())


def _working_row(tile: deck.Tile) -> str:
    html = overview.render_work_lists(_station_view(tile))
    match = re.search(r'<li class="row".*?</li>', html, re.DOTALL)
    assert match
    return match.group(0)


def test_genuinely_absent_fields_stay_unknown(conn: sqlite3.Connection) -> None:
    _event(conn, "ghost-1", 1, "run.started")
    (run,) = deck.fetch_live_runs(conn, now=NOW, stale_after_seconds=1800)
    assert run.seat == ""
    assert run.harness == ""
    assert run.provider == ""
    html = _working_row(deck.Tile(run=run, claim=None, collision=False))
    assert "unknown" in html
    assert ">/" not in html


def test_row_falls_back_to_claim_conductor_for_seat() -> None:
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
    html = _working_row(deck.Tile(run=run, claim=claim, collision=False))
    assert "muse13" in html
    assert "seat /" not in html


def test_row_renders_the_harness_badge_and_keeps_the_model_in_a_tooltip() -> None:
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
    html = _working_row(deck.Tile(run=run, claim=None, collision=False))
    assert html.count("<svg") == 1
    assert 'title="claude"' in html and 'aria-label="Claude"' in html
    assert 'title="anthropic"' not in html
    assert '<p class="row-meta" title="opus">' in html
    assert "claude_standby" in html


def test_row_snapshot() -> None:
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
    html = _working_row(deck.Tile(run=run, claim=None, collision=False))
    oc = deck.harness_brand("opencode")
    assert oc.svg != ""
    assert html == (
        '<li class="row" title="run-muse13-1"><span class="dot dot--working" aria-hidden="true"></span>'
        '<div class="row-body"><p class="row-title">'
        f'<a href="/view/machines?node={NODE_A}&amp;repo=repo">repo</a></p>'
        '<p class="row-meta" title="opencode/muse-spark-1.3-contributor-free">'
        '<span class="badge badge-color-2b6cb0" title="opencode" aria-label="OpenCode">' + oc.svg + "</span> "
        "muse13 · Example</p></div>"
        '<span class="row-time">10s</span></li>'
    )


def test_cloud_and_session_badges() -> None:
    worker = deck.CloudWorker(provider="cursor", used=1, limit=3, circuit_state="closed", leases=())
    view = deck.DeckView(stations=(), rail=(), repos=(), outcomes=(), observers=(), cloud_workers=(worker,))
    html = overview.render_cloud_workers(view)
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
    sessions = deck.DeckView(
        stations=(), rail=(), repos=(), outcomes=(), observers=(), interactive_sessions=(session, unknown_session)
    )
    session_html = overview.render_sessions(sessions)
    assert "<svg" in session_html and 'aria-label="Claude"' in session_html
    assert ">??<" in session_html
