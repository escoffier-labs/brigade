"""One status model for the Cloudflare panel, as tables.

Every Codex finding on the panel's PR maps to a row below. A row id starting `F<n>` is
finding n of the PR's review threads, in thread order, so a regression points at the rule
it broke; `M` rows pin the model itself.

F1 Wrangler JSON shapes      F2 later read failure erases peers   F3 compaction drops fresh Cloudflare
F4 panel shown when opted out F5 retained v1 snapshot KeyError     F6 older day with wider coverage wins
F7 one resource configured    F8 database-only credential          F9 token for another account
F10 gradual deployment        F11 newest opt-in lost to compaction

Collection: what one read of each configured resource produces, and credential health.
Compaction: which same-day snapshots survive and what the page reads from them.
Panel: what a reader sees for hidden, healthy, unavailable, unknown and stale.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from brigade import fleet_admin_stats as stats, fleet_admin_stats_collector as collector, fleet_admin_stats_page as page
from brigade import fleet_hub

NOW = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
CANARY = "example-cloudflare-secret"
GOOD = object()  # a graphql mode: the token sees the configured account
OTHER_ACCOUNT = object()  # a graphql mode: the token sees a different account
DOWN = object()  # a graphql mode: the request fails

DEPLOY = {"id": "dep", "created_on": "2026-10-20T11:54:00Z", "versions": [{"version_id": "v1", "percentage": 100}]}
GRADUAL = {
    "id": "dep",
    "versions": [{"version_id": "v1", "percentage": 50}, {"version_id": "v2", "percentage": 50}],
}
VERSIONS = [
    {"id": "v1", "metadata": {"created_on": "2026-10-20T11:55:00Z"}},
    {"id": "v2", "metadata": {"created_on": "2026-10-20T11:56:00Z"}},
]
COUNT = [{"success": True, "results": [{"row_count": 128}]}]
WORKER_READS = {"deployments status": DEPLOY, "deployments list": [], "versions list": VERSIONS}
D1_READS = {"d1 info": {"name": "example-db"}, "d1 execute": COUNT}
BOTH = {**WORKER_READS, **D1_READS}
WORKER_SETTINGS = {"worker": "example-worker", "account": "example-account", "analytics_token_env": "EXAMPLE_TOKEN"}
DB_SETTINGS = {"database": "example-db"}

# id, settings, token set, wrangler reads that succeed, graphql mode, expected cells, reads attempted.
# The expected cells are the fields that differ from "unknown/None".
COLLECTION = [
    pytest.param(
        {**WORKER_SETTINGS, **DB_SETTINGS},
        True,
        BOTH,
        GOOD,
        dict(credential="available", worker_version="v1", worker_published_at="2026-10-20T11:55:00Z", d1_rows=128),
        5,
        id="F1-actual-wrangler-shapes-healthy",
    ),
    pytest.param(
        {**WORKER_SETTINGS, **DB_SETTINGS},
        True,
        {k: v for k, v in BOTH.items() if k != "d1 info"},
        GOOD,
        dict(credential="available", worker_version="v1", worker_published_at="2026-10-20T11:55:00Z", d1_rows=128),
        5,
        id="F2-d1-info-fails-peers-survive",
    ),
    pytest.param(
        {**WORKER_SETTINGS, **DB_SETTINGS},
        True,
        {k: v for k, v in BOTH.items() if k != "d1 execute"},
        GOOD,
        dict(credential="available", worker_version="v1", worker_published_at="2026-10-20T11:55:00Z", d1_rows=None),
        5,
        id="F2-d1-count-fails-peers-survive",
    ),
    pytest.param(
        {**WORKER_SETTINGS, **DB_SETTINGS},
        True,
        WORKER_READS,
        GOOD,
        dict(credential="unknown", worker_version="v1", worker_published_at="2026-10-20T11:55:00Z", d1_rows=None),
        5,
        id="F2-d1-denied-is-unknown-not-unavailable",
    ),
    pytest.param(
        WORKER_SETTINGS,
        True,
        WORKER_READS,
        GOOD,
        dict(credential="available", worker_version="v1", worker_published_at="2026-10-20T11:55:00Z", d1_rows=None),
        3,
        id="F7-worker-only-reads-worker-cells",
    ),
    pytest.param(
        DB_SETTINGS,
        False,
        D1_READS,
        GOOD,
        dict(credential="available", d1_rows=128),
        2,
        id="F7-database-only-reads-d1-cells",
    ),
    pytest.param(
        DB_SETTINGS,
        False,
        D1_READS,
        DOWN,
        dict(credential="available", d1_rows=128),
        2,
        id="F8-database-only-oauth-without-analytics-token-is-available",
    ),
    pytest.param(
        {**WORKER_SETTINGS, **DB_SETTINGS},
        True,
        {**BOTH, "deployments status": {**GRADUAL}},
        GOOD,
        dict(credential="available", d1_rows=128),
        5,
        id="F10-gradual-deployment-names-no-single-version",
    ),
    pytest.param(
        WORKER_SETTINGS,
        True,
        {
            "deployments list": [
                {"id": "old", "created_on": "2026-10-19T10:00:00Z", "versions": [{"version_id": "v2"}]},
                {"id": "new", "created_on": "2026-10-20T11:54:00Z", "versions": [{"version_id": "v1"}]},
            ],
            "versions list": VERSIONS,
        },
        GOOD,
        dict(credential="available", worker_version="v1", worker_published_at="2026-10-20T11:55:00Z"),
        3,
        id="M-list-fallback-uses-newest-deployment-not-first",
    ),
    pytest.param(
        WORKER_SETTINGS,
        True,
        WORKER_READS,
        OTHER_ACCOUNT,
        dict(credential="unknown", worker_version="v1", worker_published_at="2026-10-20T11:55:00Z"),
        3,
        id="F9-token-for-another-account-is-not-accepted",
    ),
    pytest.param(
        {**WORKER_SETTINGS, **DB_SETTINGS},
        False,
        BOTH,
        DOWN,
        dict(credential="unavailable", worker_version="v1", worker_published_at="2026-10-20T11:55:00Z", d1_rows=128),
        5,
        id="M-missing-token-is-unavailable-but-reads-survive",
    ),
    pytest.param(
        {**WORKER_SETTINGS, **DB_SETTINGS},
        True,
        {},
        GOOD,
        dict(credential="unavailable"),
        5,
        id="M-token-works-without-wrangler-login-is-unavailable",
    ),
    pytest.param(
        {**WORKER_SETTINGS, **DB_SETTINGS},
        True,
        {},
        DOWN,
        dict(credential="unknown"),
        5,
        id="M-everything-fails-with-a-token-is-unknown",
    ),
    pytest.param(
        DB_SETTINGS,
        False,
        {"d1 info": None, "d1 execute": [{"success": True, "results": [{"row_count": -1}]}]},
        DOWN,
        dict(credential="available"),
        2,
        id="M-invalid-count-stays-unknown",
    ),
]


def run_collection(settings, token, reads, graphql, monkeypatch):
    if token:
        monkeypatch.setenv("EXAMPLE_TOKEN", CANARY)
    else:
        monkeypatch.delenv("EXAMPLE_TOKEN", raising=False)
    attempted = []

    def wrangler(argv, **kwargs):
        attempted.append(argv)
        assert CANARY not in json.dumps(kwargs)
        key = " ".join(argv[1:3])
        if key not in reads:
            raise ValueError("Cloudflare read failed")
        return reads[key]

    def api(url, *, body=None, token=None):
        if graphql is DOWN:
            raise ValueError("stats source read failed")
        if body["query"] == collector.VIEWER_QUERY:
            tag = "different-account" if graphql is OTHER_ACCOUNT else "example-account"
            return {"data": {"viewer": {"accounts": [{"accountTag": tag}]}}}
        return {
            "data": {"viewer": {"accounts": [{"workersInvocationsAdaptive": [{"sum": {"requests": 20, "errors": 1}}]}]}}
        }

    monkeypatch.setattr(collector, "run_wrangler", wrangler)
    monkeypatch.setattr(collector, "fetch_json", api)
    value = collector.cloudflare_status({"status_panel": True, **settings}, now=NOW)
    return value, attempted


@pytest.mark.parametrize("settings, token, reads, graphql, expected, attempts", COLLECTION)
def test_one_collection_per_configured_resource(monkeypatch, settings, token, reads, graphql, expected, attempts):
    value, attempted = run_collection(settings, token, reads, graphql, monkeypatch)
    blank = dict.fromkeys(stats_cloudflare_cells(), None)
    # Analytics cells come from the one GraphQL read, which needs a Worker, a token and a matching account.
    if "worker" in settings and token and graphql is GOOD:
        blank.update(requests_24h=20, errors_24h=1)
    assert {key: value[key] for key in blank} == {**blank, **expected}
    assert len(attempted) == attempts
    assert value["last_purge"] is None
    assert CANARY not in json.dumps(value)


def stats_cloudflare_cells():
    return ("credential", "worker_version", "worker_published_at", "d1_rows", "requests_24h", "errors_24h")


@pytest.mark.parametrize("settings", [{}, {"worker": ""}, {"worker": None, "database": None}, {"account": "x"}])
def test_no_configured_resource_means_no_observation(settings):
    assert collector.cloudflare_status({"status_panel": True, **settings}, now=NOW) is None


@pytest.mark.parametrize("status_panel", [None, False, "true", 1])
def test_status_panel_is_opt_in_with_a_true_boolean(status_panel):
    assert collector.cloudflare_status({"status_panel": status_panel, **WORKER_SETTINGS}, now=NOW) is None


# Each row stores snapshots in order: (hours after NOW, source_set, has a Cloudflare observation).
# Expected: index of the latest regular snapshot, whether the newest snapshot supplies the
# panel, and how many rows survive compaction.
DAY = 24
COMPACTION = [
    pytest.param(
        [(0, ["d1", "worker", "downloads"], False), (1, ["d1", "cloudflare"], True)],
        0,
        1,
        2,
        id="F3-fewer-sources-later-cloudflare-snapshot-survives",
    ),
    pytest.param(
        [(0, ["d1", "worker", "downloads", "cloudflare"], True), (1, ["d1"], False)],
        0,
        None,
        2,
        id="F11-later-opt-out-with-fewer-sources-is-not-overruled",
    ),
    pytest.param(
        [(0, ["d1", "cloudflare"], True), (1, ["d1", "worker", "downloads"], False)],
        1,
        None,
        1,
        id="F11-later-opt-out-with-more-sources-hides-panel",
    ),
    pytest.param(
        [(-DAY, ["d1", "worker", "downloads"], False), (0, ["d1", "worker"], False)],
        1,
        None,
        2,
        id="F6-newest-day-wins-over-older-wider-coverage",
    ),
    pytest.param(
        [(-DAY, ["d1", "cloudflare"], True), (0, ["d1"], False)],
        1,
        None,
        2,
        id="M-older-day-observation-is-not-resurrected",
    ),
    pytest.param(
        [(0, ["d1", "worker"], False), (1, ["cloudflare"], True), (2, ["d1"], False), (3, ["d1", "cloudflare"], True)],
        0,
        3,
        2,
        id="M-newest-of-many-same-day-supplies-state",
    ),
]


def cloudflare_cell(at):
    return {
        "collected_at": at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "credential": "available",
        "worker_version": "example-version",
        "worker_published_at": None,
        "repository_head": None,
        "d1_rows": 120,
        "last_purge": None,
        "requests_24h": 840,
        "errors_24h": 2,
    }


@pytest.mark.parametrize("series, latest, panel, retained", COMPACTION)
def test_same_day_compaction_and_view(tmp_path, series, latest, panel, retained):
    conn = fleet_hub.init_db(tmp_path / "hub.db")
    stored = []
    for hours, sources, has_cloudflare in series:
        at = NOW + timedelta(hours=hours)
        stored.append(
            stats.snapshot(
                taken_at=at,
                source_set=sources,
                weeks=[],
                daily=[],
                downloads={"pypi": 12, "npm": 5} if "downloads" in sources else {"pypi": None, "npm": None},
                cloudflare=cloudflare_cell(at) if has_cloudflare else None,
            )
        )
    for value in stored:
        stats.store(conn, value, now=NOW + timedelta(hours=max(h for h, *_ in series)))
    view = stats.load_view(conn, now=NOW + timedelta(hours=max(h for h, *_ in series)))
    assert view["latest"] == stored[latest]
    assert view["cloudflare"] == (stored[panel]["cloudflare"] if panel is not None else None)
    assert conn.execute("SELECT COUNT(*) FROM admin_stats_snapshots").fetchone()[0] == retained
    conn.close()


def test_f5_f4_retained_v1_snapshot_reads_as_opted_out(tmp_path):
    full = stats.snapshot(
        taken_at=NOW,
        source_set=["d1", "cloudflare"],
        weeks=[],
        daily=[],
        downloads={"pypi": None, "npm": None},
        cloudflare=cloudflare_cell(NOW),
    )
    older_shape = stats.snapshot(
        taken_at=NOW + timedelta(minutes=1),
        source_set=["d1"],
        weeks=[],
        daily=[],
        downloads={"pypi": None, "npm": None},
    )
    legacy = {key: item for key, item in older_shape.items() if key != "cloudflare"}
    legacy["schema"] = stats.LEGACY_SCHEMA
    legacy["digest"] = legacy["snapshot_id"] = stats._digest(legacy)
    conn = fleet_hub.init_db(tmp_path / "hub.db")
    stats.store(conn, full, now=NOW)
    stats.store(conn, legacy, now=NOW + timedelta(minutes=1))
    view = stats.load_view(conn, now=NOW + timedelta(minutes=1))
    assert view["latest"] == legacy
    assert view["cloudflare"] is None
    html = page.render(view, nonce="example", now=NOW + timedelta(minutes=1))
    assert 'data-field="cloudflare"' not in html
    conn.close()


def render(cloudflare, now=NOW):
    return page.render({"latest": None, "cloudflare": cloudflare, "weeks": []}, nonce="example", now=now)


def cell(html, field):
    marker = f'data-field="cloudflare.{field}">'
    return html.split(marker, 1)[1].split("</span>", 1)[0]


healthy = cloudflare_cell(NOW)
unavailable = {**healthy, "credential": "unavailable", "worker_version": None, "d1_rows": None, "requests_24h": None}
unknown = {**healthy, "credential": "unknown", "worker_version": None, "d1_rows": None}
# id, observation, minutes after collection, credential text, version, rows, requests
PANEL = [
    pytest.param(healthy, 0, "available", "example-version", "120", "840", id="healthy"),
    pytest.param(healthy, 15, "available", "example-version", "120", "840", id="healthy-at-the-15-minute-edge"),
    pytest.param(
        unavailable, 0, "credential unavailable", "unknown", "unknown", "unknown", id="credential-unavailable"
    ),
    pytest.param(unknown, 0, "unknown", "unknown", "unknown", "840", id="credential-unknown-keeps-successful-cells"),
    pytest.param(healthy, 16, "unknown", "unknown", "unknown", "unknown", id="stale-is-unknown"),
    pytest.param(
        unavailable, 16, "unknown", "unknown", "unknown", "unknown", id="stale-unavailable-is-unknown-not-unavailable"
    ),
    pytest.param(healthy, -10, "unknown", "unknown", "unknown", "unknown", id="future-dated-is-unknown"),
]


@pytest.mark.parametrize("observation, minutes, credential, version, rows, requests", PANEL)
def test_panel_states(observation, minutes, credential, version, rows, requests):
    html = render(observation, now=NOW + timedelta(minutes=minutes))
    assert cell(html, "credential") == credential
    assert cell(html, "worker_version") == version
    assert cell(html, "d1_rows") == rows
    assert cell(html, "requests_24h") == requests
    assert cell(html, "last_purge") == "unknown"
    assert CANARY not in html


def test_f4_panel_is_hidden_when_disabled_or_unobserved():
    assert 'data-field="cloudflare"' not in render(None)
    assert 'data-field="cloudflare"' in render(healthy)
