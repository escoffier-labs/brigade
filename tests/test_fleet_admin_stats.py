"""Aggregate-only snapshot, migration, retention and HTTP contracts, without sockets."""

import copy
import io
import json
import re
from datetime import datetime, timedelta, timezone
from email.message import Message

import pytest

from brigade import fleet_hub, fleet_hub_http

NOW = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)


def snapshot():
    from brigade import fleet_admin_stats as stats

    return stats.snapshot(
        taken_at=NOW,
        source_set=["d1", "worker", "downloads"],
        weeks=[
            {
                "week": "2026-W43",
                "partial": True,
                "active_sources": 20,
                "retained_2d_sources": 10,
                "retained_3d_sources": 5,
                "versions": [{"version": "0.28.0", "active_sources": 20}],
                "upgrades": [{"from_version": "0.27.0", "to_version": "0.28.0", "active_sources": 5}],
            }
        ],
        daily=[{"day": "2026-10-20", "worker_requests": None, "worker_errors": None}],
        downloads={"pypi": None, "npm": 0},
    )


def test_snapshot_suppresses_small_cells_and_checks_digest():
    from brigade import fleet_admin_stats as stats

    value = snapshot()
    value["weeks"][0].update(retained_2d_sources=4, retained_3d_sources=None)
    value = stats.snapshot(
        taken_at=NOW,
        source_set=value["source_set"],
        weeks=value["weeks"],
        daily=value["daily"],
        downloads=value["downloads"],
    )
    assert value["weeks"][0]["retained_2d_sources"] is None
    assert value["weeks"][0]["active_sources"] == 20
    assert stats.validate(value) == value
    bad = copy.deepcopy(value)
    bad["downloads"]["pypi"] = 100
    with pytest.raises(ValueError, match="digest"):
        stats.validate(bad)


@pytest.mark.parametrize("source", ["d1", "worker", "downloads"])
def test_snapshot_rejects_provider_data_without_its_source(source):
    from brigade import fleet_admin_stats as stats

    value = snapshot()
    value["source_set"].remove(source)
    value["digest"] = value["snapshot_id"] = stats._digest(value)
    with pytest.raises(ValueError, match="source set"):
        stats.validate(value)


@pytest.mark.parametrize("field", ["source_hash", "install_id", "ip", "hostname"])
@pytest.mark.parametrize("nested", [False, True])
def test_closed_schema_rejects_private_fields(field, nested):
    from brigade import fleet_admin_stats as stats

    value = snapshot()
    target = value["weeks"][0] if nested else value
    target[field] = "example-private-value"
    with pytest.raises(ValueError, match="fields"):
        stats.validate(value)


def test_migration_retention_and_idempotent_snapshot(tmp_path):
    from brigade import fleet_admin_stats as stats

    path = tmp_path / "hub.db"
    conn = fleet_hub.init_db(path)
    conn.execute("DROP TABLE admin_stats_snapshots")
    conn.execute("DROP TABLE admin_stats_weeks")
    conn.execute("PRAGMA user_version=34")
    conn.commit()
    conn.close()
    conn = fleet_hub.init_db(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == fleet_hub.SCHEMA_VERSION
    old = stats.snapshot(
        taken_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        source_set=[],
        weeks=[],
        daily=[],
        downloads={"pypi": None, "npm": None},
    )
    stats.store(conn, old, now=datetime(2024, 1, 1, tzinfo=timezone.utc))
    stats.store(conn, snapshot(), now=NOW)
    stats.store(conn, snapshot(), now=NOW)
    assert conn.execute("SELECT COUNT(*) FROM admin_stats_snapshots").fetchone()[0] == 1
    assert stats.load_view(conn, now=NOW)["latest"] == snapshot()
    conn.execute("PRAGMA query_only=ON")
    assert stats.load_view(conn, now=NOW)["weeks"][0]["week"] == "2026-W43"
    conn.close()


def test_load_view_keeps_retained_v1_snapshot_available(tmp_path):
    from brigade import fleet_admin_stats as stats

    value = snapshot()
    legacy = {key: item for key, item in value.items() if key != "cloudflare"}
    legacy["schema"] = stats.LEGACY_SCHEMA
    legacy["digest"] = legacy["snapshot_id"] = stats._digest(legacy)
    conn = fleet_hub.init_db(tmp_path / "hub.db")
    stats.store(conn, legacy, now=NOW)

    view = stats.load_view(conn, now=NOW)
    assert view["latest"] == legacy
    assert view["cloudflare"] is None
    conn.close()


@pytest.mark.parametrize("phase", ["startup", "periodic"])
def test_hub_physically_expires_stats_without_a_later_post(tmp_path, monkeypatch, phase):
    import threading
    import time
    from datetime import timedelta
    from brigade import fleet_admin_stats as stats

    clock = [NOW]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]

    monkeypatch.setattr(stats, "datetime", Clock)
    monkeypatch.setattr(stats, "RETENTION_INTERVAL_SECONDS", 0.01, raising=False)
    db = tmp_path / "hub.db"
    conn = fleet_hub.init_db(db)
    taken = NOW - timedelta(days=401 if phase == "startup" else 399)
    year, week, _ = taken.isocalendar()
    row = snapshot()["weeks"][0]
    row.update(week=f"{year}-W{week:02}", partial=True)
    value = stats.snapshot(
        taken_at=taken, source_set=["d1"], weeks=[row], daily=[], downloads={"pypi": None, "npm": None}
    )
    stats.store(conn, value, now=taken)

    def counts():
        return tuple(
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("admin_stats_snapshots", "admin_stats_weeks")
        )

    assert counts() == (1, 1)

    class Server:
        server_address = ("example-host", 0)
        closed = False

        def serve_forever(self):
            if phase == "periodic":
                assert counts() == (1, 1)
                clock[0] += timedelta(days=2)
                deadline = time.monotonic() + 2
                while counts() != (0, 0) and time.monotonic() < deadline:
                    threading.Event().wait(0.01)
            assert counts() == (0, 0)

        def server_close(self):
            self.closed = True

    server = Server()
    monkeypatch.setattr(fleet_hub, "BoundedThreadingHTTPServer", lambda *a, **k: server)
    monkeypatch.setattr(fleet_hub, "_load_token", lambda args: "example-admin")
    try:
        assert fleet_hub.run(host="localhost", port=0, db_path=db, token_file=None) == 0
        assert server.closed
    finally:
        conn.close()


def handler(tmp_path, *, path, bearer=None, cookie=None, body=None):
    db = tmp_path / "hub.db"
    fleet_hub.init_db(db).close()
    cls = fleet_hub_http.make_handler("example-admin", db, allow_admin_writes=True)
    instance = cls.__new__(cls)
    instance.path = path
    instance.headers = Message()
    if bearer:
        instance.headers["Authorization"] = "Bearer " + bearer
    if cookie:
        instance.headers["Cookie"] = cookie
    raw = json.dumps(body).encode() if body is not None else b""
    instance.headers["Content-Length"] = str(len(raw))
    instance.rfile = io.BytesIO(raw)
    result = []
    instance._send_json = lambda status, payload, **kw: result.append((status, payload, kw))
    instance._send_html = lambda status, payload, **kw: result.append((status, payload, kw))
    return instance, result, db


def test_stats_routes_node_only_closed_schema_and_browser_access(tmp_path, monkeypatch):
    from brigade import fleet_admin_stats as stats
    from brigade import fleet_hub_enrollment as enrollment

    monkeypatch.setattr(stats, "config", lambda: {"collector_nodes": ["example-node"]})
    h, result, db = handler(tmp_path, path="/stats", bearer="example-admin", body=snapshot())
    h.do_POST()
    assert result[-1][0] == 403  # even with allow_admin_writes
    conn = fleet_hub.open_db(db)
    _, token = fleet_hub.add_node(conn, "example-node", "Example")
    conn.close()
    value = snapshot()
    value.update(source_hash="example-hash", install_id="example-id", ip="example-address", hostname="example-host")
    h, result, _ = handler(tmp_path, path="/stats", bearer=token, body=value)
    h.do_POST()
    assert result[-1][0] == 400
    h, result, _ = handler(tmp_path, path="/stats", bearer=token, body=snapshot())
    h.do_POST()
    assert result[-1][0] == 200
    h, result, _ = handler(tmp_path, path="/deck/stats")
    h.do_GET()
    assert result[-1][0] == 401
    # Use the real enrollment store rather than opening a network listener.
    conn = fleet_hub.open_db(db)
    credential = enrollment.redeem(conn, enrollment.mint(conn)["code"])
    conn.close()
    h, result, _ = handler(tmp_path, path="/deck/stats", cookie="brigade_fleet_view=" + credential)
    h.do_GET()
    assert result[-1][0] == 200
    assert "partial" in result[-1][1]
    h, result, _ = handler(tmp_path, path="/deck/stats?filter=anything", bearer="example-admin")
    h.do_GET()
    assert result[-1][0] == 400


def test_page_unknown_partial_privacy_and_docs():
    from pathlib import Path
    from brigade import fleet_admin_stats as stats, fleet_admin_stats_page as page

    value = snapshot()
    html = page.render({"latest": value, "weeks": value["weeks"]}, nonce="example", now=NOW)
    for text in ("unknown", "partial", "active sources, not people or installs", "Solomon Neas", "erasure", "below 5"):
        assert text in html
    assert 'href="https://github.com/escoffier-labs/brigade/blob/main/SECURITY.md"' in html
    assert set(re.findall(r'data-field="([^"]+)"', html)) == {
        field for field in stats.FIELD_PATHS if not field.startswith("cloudflare")
    }
    for doc in ("docs/technical-guide.md", "docs/fleet-sync.md"):
        listed = Path(doc).read_text().split("expose these fields:\n\n", 1)[1].split("\n\n", 1)[0]
        assert set(re.findall(r"`([^`]+)`", listed)) == set(stats.FIELD_PATHS)
    assert "unknown" in page.render({"latest": None, "weeks": []}, nonce="example", now=NOW)


def test_cloudflare_panel_shows_fixture_and_hides_stale_values():
    from brigade import fleet_admin_stats as stats, fleet_admin_stats_page as page

    cloudflare = {
        "collected_at": "2026-10-20T12:00:00Z",
        "credential": "available",
        "worker_version": "example-version",
        "worker_published_at": "2026-10-20T11:55:00Z",
        "repository_head": "a" * 40,
        "d1_rows": 120,
        "last_purge": "2026-10-20T11:00:00Z",
        "requests_24h": 840,
        "errors_24h": 2,
    }
    value = stats.snapshot(
        taken_at=NOW,
        source_set=["cloudflare"],
        weeks=[],
        daily=[],
        downloads={"pypi": None, "npm": None},
        cloudflare=cloudflare,
    )
    fresh = page.render({"latest": value, "cloudflare": value["cloudflare"], "weeks": []}, nonce="example", now=NOW)
    assert 'data-field="cloudflare.worker_version">example-version</span>' in fresh
    assert "credential unavailable" not in fresh
    unavailable = dict(cloudflare, credential="unavailable", worker_version=None, d1_rows=None)
    unavailable_snapshot = stats.snapshot(
        taken_at=NOW,
        source_set=["cloudflare"],
        weeks=[],
        daily=[],
        downloads={"pypi": None, "npm": None},
        cloudflare=unavailable,
    )
    unavailable_html = page.render(
        {"latest": unavailable_snapshot, "cloudflare": unavailable_snapshot["cloudflare"], "weeks": []},
        nonce="example",
        now=NOW,
    )
    assert "credential unavailable" in unavailable_html
    assert 'data-field="cloudflare.d1_rows">unknown</span>' in unavailable_html
    stale = page.render(
        {"latest": value, "cloudflare": value["cloudflare"], "weeks": []},
        nonce="example",
        now=NOW + timedelta(minutes=16),
    )
    assert 'data-field="cloudflare.credential">unknown</span>' in stale
    assert "credential unavailable" not in stale
    assert 'data-field="cloudflare.worker_version">unknown</span>' in stale


def test_cloudflare_panel_is_hidden_without_an_observation():
    from brigade import fleet_admin_stats_page as page

    html = page.render({"latest": snapshot(), "weeks": []}, nonce="example", now=NOW)
    assert 'data-field="cloudflare"' not in html


def test_cloudflare_snapshot_rejects_unapproved_fields():
    from brigade import fleet_admin_stats as stats

    cloudflare = {
        "collected_at": "2026-10-20T12:00:00Z",
        "credential": "available",
        "worker_version": None,
        "worker_published_at": None,
        "repository_head": None,
        "d1_rows": None,
        "last_purge": None,
        "requests_24h": None,
        "errors_24h": None,
        "token": "example-secret",
    }
    with pytest.raises(ValueError, match="fields"):
        stats.snapshot(
            taken_at=NOW,
            source_set=["cloudflare"],
            weeks=[],
            daily=[],
            downloads={"pypi": None, "npm": None},
            cloudflare=cloudflare,
        )


@pytest.mark.parametrize("enabled", [False, True])
def test_stats_page_preserves_worklore_navigation(monkeypatch, enabled):
    from brigade import fleet_admin_stats_page as page, worklore_http

    monkeypatch.setattr(worklore_http, "enabled", lambda: enabled)
    html = page.render({"latest": None, "weeks": []}, nonce="example", now=NOW)
    assert ('href="/deck/work"' in html) is enabled


@pytest.mark.parametrize(
    "path", [("weeks", 0, "versions", 0), ("weeks", 0, "upgrades", 0), ("daily", 0), ("downloads",)]
)
def test_deep_unknown_fields_are_not_persisted(tmp_path, path):
    from brigade import fleet_admin_stats as stats

    value = snapshot()
    target = value
    for part in path:
        target = target[part]
    target["source_hash"] = "example-hash"
    conn = fleet_hub.init_db(tmp_path / "hub.db")
    with pytest.raises(ValueError, match="fields"):
        stats.store(conn, value, now=NOW)
    assert conn.execute("SELECT COUNT(*) FROM admin_stats_snapshots").fetchone()[0] == 0
    conn.close()


def test_weekly_history_keeps_last_observation_for_each_week(tmp_path):
    from brigade import fleet_admin_stats as stats

    conn = fleet_hub.init_db(tmp_path / "hub.db")
    earlier = snapshot()["weeks"][0]
    earlier.update(week="2026-W42", partial=False)
    older = stats.snapshot(
        taken_at=datetime(2026, 10, 19, tzinfo=timezone.utc),
        source_set=["d1"],
        weeks=[earlier],
        daily=[],
        downloads={"pypi": None, "npm": None},
    )
    stats.store(conn, older, now=NOW)
    stats.store(conn, snapshot(), now=NOW)
    view = stats.load_view(conn, now=NOW)
    assert [row["week"] for row in view["weeks"]] == ["2026-W43", "2026-W42"]
    assert view["latest"] == snapshot()
    conn.close()


def test_history_prefers_complete_over_newer_truncated_week(tmp_path):
    from brigade import fleet_admin_stats as stats

    conn = fleet_hub.init_db(tmp_path / "hub.db")
    week = snapshot()["weeks"][0]
    week.update(week="2026-W30", partial=False, active_sources=50, retained_2d_sources=50, retained_3d_sources=50)
    complete = stats.snapshot(
        taken_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
        source_set=["d1"],
        weeks=[week],
        daily=[],
        downloads={"pypi": None, "npm": None},
    )
    stats.store(conn, complete, now=NOW)
    week.update(partial=True, active_sources=5, retained_2d_sources=5, retained_3d_sources=5, versions=[], upgrades=[])
    truncated = stats.snapshot(
        taken_at=NOW, source_set=["d1"], weeks=[week], daily=[], downloads={"pypi": None, "npm": None}
    )
    stats.store(conn, truncated, now=NOW)
    # Out-of-order replay must also leave the complete observation in place.
    stats.store(conn, complete, now=NOW)
    assert stats.load_view(conn, now=NOW)["weeks"] == complete["weeks"]
    conn.close()


def test_frequent_collection_bounds_snapshots_without_losing_history(tmp_path):
    from datetime import timedelta
    from brigade import fleet_admin_stats as stats

    conn = fleet_hub.init_db(tmp_path / "hub.db")
    week = snapshot()["weeks"][0]
    week.update(week="2026-W01", partial=False)
    old = stats.snapshot(
        taken_at=NOW - timedelta(days=280),
        source_set=["d1"],
        weeks=[week],
        daily=[],
        downloads={"pypi": None, "npm": None},
    )
    stats.store(conn, old, now=NOW)
    for index in range(1602):
        value = stats.snapshot(
            taken_at=NOW - timedelta(seconds=1602 - index),
            source_set=["downloads"],
            weeks=[],
            daily=[],
            downloads={"pypi": index, "npm": None},
        )
        stats.store(conn, value, now=NOW)
    assert conn.execute("SELECT COUNT(*) FROM admin_stats_snapshots").fetchone()[0] == 2
    view = stats.load_view(conn, now=NOW)
    assert view["weeks"] == old["weeks"]
    assert view["latest"] == value
    conn.close()


def test_history_skips_invalid_stored_snapshot(tmp_path):
    from brigade import fleet_admin_stats as stats

    conn = fleet_hub.init_db(tmp_path / "hub.db")
    stats.store(conn, snapshot(), now=NOW)
    conn.execute("UPDATE admin_stats_snapshots SET payload_json = '{}' ")
    view = stats.load_view(conn, now=NOW)
    assert view["latest"] is None
    assert view["weeks"] == snapshot()["weeks"]
    conn.close()


def test_v35_migration_backfills_weekly_history(tmp_path):
    from brigade import fleet_admin_stats as stats

    path = tmp_path / "hub.db"
    conn = fleet_hub.init_db(path)
    value = snapshot()
    stats.store(conn, value, now=NOW)
    conn.execute("DROP TABLE admin_stats_weeks")
    conn.execute("PRAGMA user_version=35")
    conn.commit()
    conn.close()
    conn = fleet_hub.init_db(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == fleet_hub.SCHEMA_VERSION
    assert stats.load_view(conn, now=NOW)["weeks"] == value["weeks"]
    conn.close()


@pytest.mark.parametrize("retention", [(6, 5, 5), (16, 12, 12), (20, 10, 6)])
def test_retention_does_not_disclose_small_complements(retention):
    from brigade import fleet_admin_stats as stats

    week = snapshot()["weeks"][0]
    week.update(zip(stats.SOURCE_FIELDS, retention, strict=True))
    week.update(versions=[], upgrades=[])
    value = stats.snapshot(
        taken_at=NOW, source_set=["d1"], weeks=[week], daily=[], downloads={"pypi": None, "npm": None}
    )
    assert value["weeks"][0]["active_sources"] is None
    assert value["weeks"][0]["retained_2d_sources"] is None
    assert value["weeks"][0]["retained_3d_sources"] is None
    # A correctly digested upload must not bypass complementary suppression.
    value["weeks"][0].update(zip(stats.SOURCE_FIELDS, retention, strict=True))
    value["digest"] = value["snapshot_id"] = stats._digest(value)
    with pytest.raises(ValueError, match="suppression"):
        stats.validate(value)


@pytest.mark.parametrize("kind", ["versions", "upgrades"])
def test_histograms_do_not_disclose_single_suppressed_cell(kind):
    from brigade import fleet_admin_stats as stats, fleet_admin_stats_page as page

    week = snapshot()["weeks"][0]
    week.update(active_sources=16, retained_2d_sources=16, retained_3d_sources=16)
    week.update(versions=[], upgrades=[])
    if kind == "upgrades":
        week["versions"] = [{"version": "0.27.0", "active_sources": 16}]
    week[kind] = [
        {
            **(
                {"version": f"0.28.{i}"}
                if kind == "versions"
                else {"from_version": "0.27.0", "to_version": f"0.28.{i}"}
            ),
            "active_sources": count,
        }
        for i, count in enumerate((12, 4))
    ]
    value = stats.snapshot(
        taken_at=NOW, source_set=["d1"], weeks=[week], daily=[], downloads={"pypi": None, "npm": None}
    )
    assert value["weeks"][0]["versions"] == value["weeks"][0]["upgrades"] == []
    html = page.render({"latest": value, "weeks": value["weeks"]}, nonce="example", now=NOW)
    assert "0.28.1" not in json.dumps(value)
    assert "0.28.1" not in html
    assert html.count("other (suppressed)") == 2
    # A freshly digested upload cannot keep a suppressed category's label.
    value["weeks"][0][kind] = [{**cell, "active_sources": None} for cell in week[kind]]
    value["digest"] = value["snapshot_id"] = stats._digest(value)
    with pytest.raises(ValueError, match="suppression"):
        stats.validate(value)


@pytest.mark.parametrize("sources", [[], ["d1"], ["worker", "downloads"]])
def test_daily_compaction_prefers_coverage_over_recency(tmp_path, sources):
    from datetime import timedelta
    from brigade import fleet_admin_stats as stats

    conn = fleet_hub.init_db(tmp_path / "hub.db")
    full = stats.snapshot(
        taken_at=NOW, source_set=["d1", "worker", "downloads"], weeks=[], daily=[], downloads={"pypi": 12, "npm": 5}
    )
    fewer = stats.snapshot(
        taken_at=NOW + timedelta(hours=1), source_set=sources, weeks=[], daily=[], downloads={"pypi": None, "npm": None}
    )
    stats.store(conn, full, now=NOW)
    stats.store(conn, fewer, now=NOW + timedelta(hours=1))
    assert stats.load_view(conn, now=NOW)["latest"] == full
    # The newest snapshot is kept beside the best one: it carries the current opt-in state.
    assert conn.execute("SELECT COUNT(*) FROM admin_stats_snapshots").fetchone()[0] == 2
    conn.close()


def test_daily_compaction_keeps_best_regular_and_newest_snapshot(tmp_path):
    from datetime import timedelta
    from brigade import fleet_admin_stats as stats

    conn = fleet_hub.init_db(tmp_path / "hub.db")
    full = stats.snapshot(
        taken_at=NOW,
        source_set=["d1", "worker", "downloads"],
        weeks=[],
        daily=[],
        downloads={"pypi": 12, "npm": 5},
    )
    collected = NOW + timedelta(hours=1)
    cloudflare = {
        "collected_at": collected.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "credential": "available",
        "worker_version": "example-version",
        "worker_published_at": None,
        "repository_head": None,
        "d1_rows": 120,
        "last_purge": None,
        "requests_24h": 840,
        "errors_24h": 2,
    }
    partial = stats.snapshot(
        taken_at=collected,
        source_set=["d1", "cloudflare"],
        weeks=[],
        daily=[],
        downloads={"pypi": None, "npm": None},
        cloudflare=cloudflare,
    )
    stats.store(conn, full, now=NOW)
    stats.store(conn, partial, now=collected)

    view = stats.load_view(conn, now=collected)
    assert view["latest"] == full
    assert view["cloudflare"] == cloudflare
    assert conn.execute("SELECT COUNT(*) FROM admin_stats_snapshots").fetchone()[0] == 2
    conn.close()


def test_load_view_prefers_newest_day_before_source_coverage(tmp_path):
    from datetime import timedelta
    from brigade import fleet_admin_stats as stats

    conn = fleet_hub.init_db(tmp_path / "hub.db")
    older = stats.snapshot(
        taken_at=NOW - timedelta(days=1),
        source_set=["d1", "worker", "downloads"],
        weeks=[],
        daily=[],
        downloads={"pypi": 12, "npm": 5},
    )
    today = stats.snapshot(
        taken_at=NOW,
        source_set=["d1", "worker"],
        weeks=[],
        daily=[],
        downloads={"pypi": None, "npm": None},
    )
    stats.store(conn, older, now=NOW)
    stats.store(conn, today, now=NOW)

    assert stats.load_view(conn, now=NOW)["latest"] == today
    conn.close()


def test_stats_post_accepts_validator_maximum_snapshot(tmp_path, monkeypatch):
    from datetime import timedelta
    from brigade import fleet_admin_stats as stats

    monkeypatch.setattr(stats, "config", lambda: {"collector_nodes": ["example-node"]})
    conn = fleet_hub.init_db(tmp_path / "hub.db")
    _, token = fleet_hub.add_node(conn, "example-node", "Example")
    conn.close()
    now = datetime.now(timezone.utc)
    weeks = []
    for index in range(60):
        day = now.date() - timedelta(weeks=index)
        year, number, _ = day.isocalendar()
        weeks.append(
            {
                "week": f"{year}-W{number:02}",
                "partial": True,
                "active_sources": 10**15,
                "retained_2d_sources": 10**15,
                "retained_3d_sources": 10**15,
                "versions": [
                    {"version": f"999999.999999.999999.{i}.dev99999999", "active_sources": 10**15} for i in range(100)
                ],
                "upgrades": [
                    {
                        "from_version": "999999.999999.999998.999999.dev99999999",
                        "to_version": f"999999.999999.999999.{i}.dev99999999",
                        "active_sources": 10**15,
                    }
                    for i in range(200)
                ],
            }
        )
    value = stats.snapshot(
        taken_at=now, source_set=["d1"], weeks=weeks, daily=[], downloads={"pypi": None, "npm": None}
    )
    assert len(json.dumps(value).encode()) > 1024 * 1024
    h, result, _ = handler(tmp_path, path="/stats", bearer=token, body=value)
    h.do_POST()
    assert result[-1][0] == 200


@pytest.mark.parametrize("upgrade_count", [None, 1, 5])
@pytest.mark.parametrize("active,counts", [(20, [12, 9]), (30, [20, 11]), (20, [7, 7, 7]), (20, [12, 9, None])])
def test_version_overlap_cannot_reconstruct_suppressed_upgrade(upgrade_count, active, counts):
    from brigade import fleet_admin_stats as stats

    week = snapshot()["weeks"][0]
    week.update(active_sources=active, retained_2d_sources=active, retained_3d_sources=active)
    # The excess version observations reveal a small group using multiple versions.
    week["versions"] = [{"version": f"0.28.{i}", "active_sources": count} for i, count in enumerate(counts)]
    week["upgrades"] = (
        []
        if upgrade_count is None
        else [{"from_version": "0.27.0", "to_version": "0.28.0", "active_sources": upgrade_count}]
    )
    value = stats.snapshot(
        taken_at=NOW, source_set=["d1"], weeks=[week], daily=[], downloads={"pypi": None, "npm": None}
    )
    assert value["weeks"][0]["versions"] == []
    assert all(value["weeks"][0][field] is None for field in stats.SOURCE_FIELDS)
    assert value["weeks"][0]["upgrades"] == []
    # Authorized producers cannot bypass suppression with a fresh digest.
    if upgrade_count != 1:
        value["weeks"] = [week]
        value["digest"] = value["snapshot_id"] = stats._digest(value)
        with pytest.raises(ValueError, match="suppression"):
            stats.validate(value)


@pytest.mark.parametrize(
    "counts,hidden", [((20, 5, 5, 16), True), ((20, 10, 5, 11), False), ((20, None, None, 10), False)]
)
def test_only_containing_weekly_source_counts_trigger_gap_suppression(counts, hidden):
    from brigade import fleet_admin_stats as stats

    active, retained_2d, retained_3d, version = counts
    week = {
        "week": "2026-W43",
        "partial": True,
        "active_sources": active,
        "retained_2d_sources": retained_2d,
        "retained_3d_sources": retained_3d,
        "versions": [{"version": "0.28.0", "active_sources": version}],
        "upgrades": [],
    }
    value = stats.snapshot(
        taken_at=NOW, source_set=["d1"], weeks=[week], daily=[], downloads={"pypi": None, "npm": None}
    )
    result = value["weeks"][0]
    if hidden:
        assert all(result[field] is None for field in stats.SOURCE_FIELDS)
        assert result["versions"] == []
    else:
        assert result == week


@pytest.mark.parametrize("small_counts", [[], [None], [0], [2]])
def test_unrelated_version_counts_and_small_cells_preserve_week_totals(small_counts):
    from brigade import fleet_admin_stats as stats, fleet_admin_stats_page as page

    week = snapshot()["weeks"][0]
    # At 60 active sources the excess-observations guard would still hide
    # these versions (62 observations), independently of unrelated gaps.
    week.update(active_sources=70, retained_2d_sources=30, retained_3d_sources=12)
    visible = [
        {"version": version, "active_sources": count}
        for version, count in (("0.28.0", 40), ("0.27.0", 13), ("0.26.0", 9))
    ]
    week["versions"] = visible + [{"version": "0.25.0", "active_sources": count} for count in small_counts]
    week["upgrades"] = [
        {"from_version": "0.25.0", "to_version": "0.28.0", "active_sources": count} for count in small_counts
    ]
    value = stats.snapshot(
        taken_at=NOW, source_set=["d1"], weeks=[week], daily=[], downloads={"pypi": None, "npm": None}
    )
    result = value["weeks"][0]
    assert [result[field] for field in stats.SOURCE_FIELDS] == [70, 30, 12]
    assert result["versions"] == visible
    assert result["upgrades"] == []
    html = page.render({"latest": value, "weeks": value["weeks"]}, nonce="example", now=NOW)
    assert "0.25.0" not in json.dumps(value)
    assert "0.25.0" not in html
    assert html.count("other (suppressed): unknown") == 2
    assert stats.validate(value) == value


@pytest.mark.parametrize("parent", ["from_version", "to_version"])
def test_version_upgrade_containment_gap_is_suppressed(parent):
    from brigade import fleet_admin_stats as stats

    week = snapshot()["weeks"][0]
    week.update(active_sources=60, retained_2d_sources=45, retained_3d_sources=15)
    upgrade = {"from_version": "0.27.0", "to_version": "0.28.0", "active_sources": 27}
    week["versions"] = [{"version": upgrade[parent], "active_sources": 30}]
    week["upgrades"] = [upgrade]
    value = stats.snapshot(
        taken_at=NOW, source_set=["d1"], weeks=[week], daily=[], downloads={"pypi": None, "npm": None}
    )
    assert all(value["weeks"][0][field] is None for field in stats.SOURCE_FIELDS)
    assert value["weeks"][0]["versions"] == value["weeks"][0]["upgrades"] == []


def test_dropped_histogram_cells_do_not_trigger_gap_suppression():
    from brigade import fleet_admin_stats as stats

    week = snapshot()["weeks"][0]
    week.update(active_sources=6, retained_2d_sources=6, retained_3d_sources=6)
    week["versions"] = [{"version": "0.28.0", "active_sources": 2}]
    week["upgrades"] = [{"from_version": "0.27.0", "to_version": "0.28.0", "active_sources": 1}]
    value = stats.snapshot(
        taken_at=NOW, source_set=["d1"], weeks=[week], daily=[], downloads={"pypi": None, "npm": None}
    )
    assert [value["weeks"][0][field] for field in stats.SOURCE_FIELDS] == [6, 6, 6]
    assert value["weeks"][0]["versions"] == value["weeks"][0]["upgrades"] == []


@pytest.mark.parametrize(
    "active,retained_2d,retained_3d,version,upgrade",
    [(5, 10, 5, 5, 5), (20, 10, 15, 5, 5), (10, None, 11, 5, 5), (5, 5, 5, 11, 5), (5, 5, 5, 5, 11), (3, 5, 5, 3, 3)],
)
@pytest.mark.parametrize("boundary", ["snapshot", "store"])
def test_impossible_source_subsets_are_rejected(tmp_path, active, retained_2d, retained_3d, version, upgrade, boundary):
    from brigade import fleet_admin_stats as stats

    value = snapshot()
    row = value["weeks"][0]
    row.update(active_sources=active, retained_2d_sources=retained_2d, retained_3d_sources=retained_3d)
    row["versions"][0]["active_sources"] = version
    row["upgrades"][0]["active_sources"] = upgrade
    if boundary == "snapshot":
        with pytest.raises(ValueError, match="subset"):
            stats.snapshot(
                taken_at=NOW, source_set=["d1"], weeks=[row], daily=[], downloads={"pypi": None, "npm": None}
            )
    else:
        value["digest"] = value["snapshot_id"] = stats._digest(value)
        conn = fleet_hub.init_db(tmp_path / "hub.db")
        try:
            with pytest.raises(ValueError):
                stats.store(conn, value, now=NOW)
            assert conn.execute("SELECT COUNT(*) FROM admin_stats_snapshots").fetchone()[0] == 0
        finally:
            conn.close()
