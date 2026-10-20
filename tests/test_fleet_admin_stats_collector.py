"""Read allowlist and aggregate fixture collection, never executing wrangler or HTTP."""

import json
from datetime import datetime, timezone

import pytest


def test_runner_refuses_arbitrary_sql_and_subcommands(monkeypatch):
    from brigade import fleet_admin_stats_collector as collector

    def forbidden(*args, **kwargs):
        pytest.fail("refused command reached subprocess")

    monkeypatch.setattr(collector.subprocess, "run", forbidden)
    for sql in (
        "DELETE FROM checks",
        "SELECT * FROM checks",
        "WITH x AS (SELECT 1) DELETE FROM checks",
        "SELECT 1; DROP TABLE checks",
    ):
        with pytest.raises(ValueError):
            collector.run_wrangler(["wrangler", "d1", "execute", "example-db", "--remote", "--json", "--command", sql])
    for argv in (
        ["wrangler", "deploy"],
        ["wrangler", "d1", "execute", "--file", "example.sql"],
        ["sh", "-c", "wrangler whoami"],
    ):
        with pytest.raises(ValueError):
            collector.run_wrangler(argv)


def test_collector_uses_fixed_read_queries_and_suppresses(monkeypatch):
    from brigade import fleet_admin_stats_collector as collector

    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        query = argv[-1]
        assert query in collector.QUERIES.values()
        if query == collector.QUERIES["weekly"]:
            rows = [{"week": "2026-W43", "active_sources": 9, "retained_2d_sources": 3, "retained_3d_sources": 1}]
        elif query == collector.QUERIES["versions"]:
            rows = [{"week": "2026-W43", "version": "0.28.0", "active_sources": 9}]
        else:
            rows = []
        return [{"success": True, "results": rows}]

    monkeypatch.setattr(collector, "run_wrangler", runner)
    value = collector.collect({"database": "example-db"}, now=datetime(2026, 10, 20, tzinfo=timezone.utc))
    assert len(calls) == 3
    assert value["weeks"][0]["partial"] is True
    assert value["weeks"][0]["retained_2d_sources"] is None
    assert value["downloads"] == {"pypi": None, "npm": None}
    assert value["daily"] == []
    assert "example-db" not in json.dumps(value)


def test_wrangler_runner_oauth_env_and_sanitized_failure(monkeypatch):
    from brigade import fleet_admin_stats_collector as collector

    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "example-secret")

    def run(argv, **kwargs):
        assert kwargs["shell"] is False
        assert "CLOUDFLARE_API_TOKEN" not in kwargs["env"]
        assert "example-secret" not in str(argv)
        raise collector.subprocess.CalledProcessError(1, argv, stderr="example-secret")

    monkeypatch.setattr(collector.subprocess, "run", run)
    with pytest.raises(ValueError, match="D1 read failed") as error:
        collector.run_wrangler(collector.wrangler_argv("example-db", "weekly"))
    assert "example-secret" not in str(error.value)


def test_analytics_and_downloads_fixture_transports(monkeypatch):
    from brigade import fleet_admin_stats_collector as collector

    monkeypatch.setattr(collector, "run_wrangler", lambda argv, **kwargs: [{"success": True, "results": []}])
    requests = []

    def fetch(url, *, body=None, token=None):
        requests.append((url, body, token))
        if body:
            assert body["query"] == collector.ANALYTICS_QUERY
            assert body["variables"]["account"] == "example-account"
            return {
                "data": {
                    "viewer": {
                        "accounts": [
                            {
                                "workersInvocationsAdaptive": [
                                    {"dimensions": {"date": "2026-10-20"}, "sum": {"requests": 100, "errors": 0}}
                                ]
                            }
                        ]
                    }
                }
            }
        return {
            "schemaVersion": 1,
            "period": {"days": 30, "timezone": "UTC"},
            "sources": {"pypi": {"downloads": 123}, "npm": {"downloads": 0}},
        }

    monkeypatch.setattr(collector, "fetch_json", fetch)
    monkeypatch.setenv("EXAMPLE_ANALYTICS_TOKEN", "example-secret")
    value = collector.collect(
        {
            "database": "example-db",
            "account": "example-account",
            "worker": "example-worker",
            "analytics_token_env": "EXAMPLE_ANALYTICS_TOKEN",
            "downloads_url": "https://example.invalid/downloads.json",
        },
        now=datetime(2026, 10, 20, tzinfo=timezone.utc),
    )
    assert value["daily"] == [{"day": "2026-10-20", "worker_requests": 100, "worker_errors": 0}]
    assert value["downloads"] == {"pypi": 123, "npm": 0}
    assert len(requests) == 2
    assert "example-secret" not in json.dumps(value)


def test_failed_sources_stay_unknown(monkeypatch):
    from brigade import fleet_admin_stats_collector as collector

    def fail(*args, **kwargs):
        raise ValueError("unavailable")

    monkeypatch.setattr(collector, "run_wrangler", fail)
    monkeypatch.setattr(collector, "fetch_json", fail)
    value = collector.collect(
        {
            "database": "example-db",
            "account": "example-account",
            "worker": "example-worker",
            "downloads_url": "https://example.invalid/counts.json",
        }
    )
    assert value["source_set"] == []
    assert value["weeks"] == []
    assert value["daily"] == []
    assert value["downloads"] == {"pypi": None, "npm": None}


def test_cli_collect_uses_operator_settings_and_node_only_publish(tmp_path, monkeypatch, capsys):
    from brigade import fleet_admin_stats as stats, fleet_admin_stats_collector as collector
    from brigade.cli import _build_parser

    (tmp_path / "fleet.toml").write_text('[fleet.stats]\ncollector_nodes = ["example-node"]\ndatabase = "example-db"\n')
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path))
    assert stats.config()["database"] == "example-db"
    monkeypatch.setattr(collector, "run_wrangler", lambda argv, **kwargs: [{"success": True, "results": []}])
    args = _build_parser().parse_args(["fleet", "stats", "collect", "--no-publish"])
    assert args.func(args) == 0
    assert json.loads(capsys.readouterr().out)["schema"] == stats.SCHEMA
    monkeypatch.setattr(
        collector.fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": "https://example.invalid", "node_token": "", "admin_token": "example-admin"},
    )
    with pytest.raises(ValueError, match="node token"):
        collector.publish(collector.collect({}))


def test_fixed_sql_runs_on_local_fixture_and_excludes_internal_observations(monkeypatch):
    import sqlite3
    from datetime import timedelta
    from brigade import fleet_admin_stats_collector as collector

    now = datetime.now(timezone.utc)
    monday = now.date() - timedelta(days=now.weekday() + 7)
    year, number, _ = monday.isocalendar()
    week = f"{year}-W{number:02}"
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE version_observations(week, hash, day, version, first_seen, internal)")
    for source in range(8):
        for day in range(3):
            conn.execute(
                "INSERT INTO version_observations VALUES (?, ?, ?, ?, ?, ?)",
                (
                    week,
                    f"example-source-{source}",
                    (monday + timedelta(days=day)).isoformat(),
                    "0.27.0" if day == 0 else "0.28.0",
                    day,
                    int(source == 7),
                ),
            )

    def runner(argv, **kwargs):
        return [{"success": True, "results": [dict(row) for row in conn.execute(argv[-1])]}]

    monkeypatch.setattr(collector, "run_wrangler", runner)
    value = collector.collect({"database": "example-db"}, now=now)
    row = value["weeks"][0]
    assert row["active_sources"] == 7
    assert row["retained_2d_sources"] == 7
    assert row["retained_3d_sources"] == 7
    assert row["upgrades"] == [{"from_version": "0.27.0", "to_version": "0.28.0", "active_sources": 7}]
    assert row["partial"] is False
    conn.close()


def test_oldest_truncated_week_is_partial(monkeypatch):
    from brigade import fleet_admin_stats_collector as collector

    def runner(argv, **kwargs):
        rows = (
            [{"week": "2026-W30", "active_sources": 8, "retained_2d_sources": 8, "retained_3d_sources": 8}]
            if argv[-1] == collector.QUERIES["weekly"]
            else []
        )
        return [{"success": True, "results": rows}]

    monkeypatch.setattr(collector, "run_wrangler", runner)
    value = collector.collect({"database": "example-db"}, now=datetime(2026, 10, 20, tzinfo=timezone.utc))
    assert value["weeks"][0]["partial"] is True  # 90-day window starts midweek.


def test_wrangler_account_setting_is_environment_only(monkeypatch):
    from types import SimpleNamespace
    from brigade import fleet_admin_stats_collector as collector

    def run(argv, **kwargs):
        assert "example-account" not in argv
        assert kwargs["env"]["CLOUDFLARE_ACCOUNT_ID"] == "example-account"
        return SimpleNamespace(stdout='[{"success": true, "results": []}]')

    monkeypatch.setattr(collector.subprocess, "run", run)
    collector.run_wrangler(collector.wrangler_argv("example-db", "weekly"), account="example-account")
