"""Aggregate-only snapshot, migration, retention and HTTP contracts, without sockets."""

import copy
import io
import json
import re
from datetime import datetime, timezone
from email.message import Message

import pytest

from brigade import fleet_hub, fleet_hub_http

NOW = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)


def snapshot():
    from brigade import fleet_admin_stats as stats

    return stats.snapshot(
        taken_at=NOW,
        source_set=["d1"],
        weeks=[
            {
                "week": "2026-W43",
                "partial": True,
                "active_sources": 20,
                "retained_2d_sources": 4,
                "retained_3d_sources": None,
                "versions": [{"version": "0.28.0", "active_sources": 12}],
                "upgrades": [{"from_version": "0.27.0", "to_version": "0.28.0", "active_sources": 5}],
            }
        ],
        daily=[{"day": "2026-10-20", "worker_requests": None, "worker_errors": None}],
        downloads={"pypi": None, "npm": 0},
    )


def test_snapshot_suppresses_small_cells_and_checks_digest():
    from brigade import fleet_admin_stats as stats

    value = snapshot()
    assert value["weeks"][0]["retained_2d_sources"] is None
    assert stats.validate(value) == value
    bad = copy.deepcopy(value)
    bad["downloads"]["pypi"] = 100
    with pytest.raises(ValueError, match="digest"):
        stats.validate(bad)


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
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 36
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
    assert set(re.findall(r'data-field="([^"]+)"', html)) == set(stats.FIELD_PATHS)
    for doc in ("docs/technical-guide.md", "docs/fleet-sync.md"):
        listed = Path(doc).read_text().split("expose these fields:\n\n", 1)[1].split("\n\n", 1)[0]
        assert set(re.findall(r"`([^`]+)`", listed)) == set(stats.FIELD_PATHS)
    assert "unknown" in page.render({"latest": None, "weeks": []}, nonce="example", now=NOW)


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
    week.update(week="2026-W30", partial=False, active_sources=50)
    complete = stats.snapshot(
        taken_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
        source_set=["d1"],
        weeks=[week],
        daily=[],
        downloads={"pypi": None, "npm": None},
    )
    stats.store(conn, complete, now=NOW)
    week.update(partial=True, active_sources=5)
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
            source_set=[],
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
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 36
    assert stats.load_view(conn, now=NOW)["weeks"] == value["weeks"]
    conn.close()
