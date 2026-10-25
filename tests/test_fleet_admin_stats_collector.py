"""Read allowlist and aggregate fixture collection, never executing wrangler or HTTP."""

import json
import re
from datetime import datetime, timezone

import pytest


def test_runner_refuses_arbitrary_sql_and_subcommands(monkeypatch):
    from brigade import fleet_admin_stats_collector as collector

    def forbidden(*args, **kwargs):
        pytest.fail("refused command reached subprocess")

    monkeypatch.setattr(collector.subprocess, "Popen", forbidden)
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
        ["wrangler", "rollback"],
        ["wrangler", "secret", "put", "EXAMPLE"],
        ["wrangler", "d1", "execute", "--file", "example.sql"],
        ["wrangler", "d1", "migrations", "apply", "example-db", "--remote"],
        ["wrangler", "d1", "delete", "example-db"],
        ["sh", "-c", "wrangler whoami"],
    ):
        with pytest.raises(ValueError):
            collector.run_wrangler(argv)


def test_status_wrangle_vectors_are_fixed_read_only_commands(monkeypatch):
    from brigade import fleet_admin_stats_collector as collector

    expected = {
        "deployments-status": ["wrangler", "deployments", "status", "--name", "example-worker", "--json"],
        "deployments-list": ["wrangler", "deployments", "list", "--name", "example-worker", "--json"],
        "versions-list": ["wrangler", "versions", "list", "--name", "example-worker", "--json"],
        "d1-info": ["wrangler", "d1", "info", "example-db", "--json"],
        "d1-counts": [
            "wrangler",
            "d1",
            "execute",
            "example-db",
            "--remote",
            "--json",
            "--command",
            collector.STATUS_QUERY,
        ],
    }
    for action, argv in expected.items():
        assert collector.status_wrangler_argv("example-worker", "example-db", action) == argv

    for argv in (
        ["wrangler", "deploy"],
        ["wrangler", "rollback", "example-worker"],
        ["wrangler", "secret", "put", "TOKEN"],
        ["wrangler", "d1", "execute", "example-db", "--file", "migration.sql"],
        ["wrangler", "d1", "migrations", "apply", "example-db", "--remote"],
        ["wrangler", "d1", "delete", "example-db"],
    ):
        with pytest.raises(ValueError):
            collector.run_wrangler(argv)


def test_cloudflare_status_without_credentials_is_unavailable(monkeypatch):
    from brigade import fleet_admin_stats_collector as collector

    monkeypatch.delenv("EXAMPLE_ANALYTICS_TOKEN", raising=False)

    def unavailable(*args, **kwargs):
        raise ValueError("Cloudflare read failed")

    monkeypatch.setattr(collector, "run_wrangler", unavailable)
    monkeypatch.setattr(collector, "fetch_json", unavailable)
    value = collector.cloudflare_status(
        {
            "worker": "example-worker",
            "database": "example-db",
            "account": "example-account",
            "analytics_token_env": "EXAMPLE_ANALYTICS_TOKEN",
            "status_panel": True,
        },
        now=datetime(2026, 10, 20, 12, tzinfo=timezone.utc),
    )
    assert value["credential"] == "unavailable"
    assert value["worker_version"] is None
    assert value["d1_rows"] is None
    assert value["requests_24h"] is None
    assert "token" not in json.dumps(value).lower()


def test_cloudflare_status_failed_reads_are_unknown_with_configured_credentials(monkeypatch):
    from brigade import fleet_admin_stats_collector as collector

    monkeypatch.setenv("EXAMPLE_ANALYTICS_TOKEN", "example-cloudflare-secret")

    def unavailable(*args, **kwargs):
        raise ValueError("Cloudflare read failed")

    monkeypatch.setattr(collector, "run_wrangler", unavailable)
    monkeypatch.setattr(collector, "fetch_json", unavailable)
    value = collector.cloudflare_status(
        {
            "worker": "example-worker",
            "database": "example-db",
            "account": "example-account",
            "analytics_token_env": "EXAMPLE_ANALYTICS_TOKEN",
            "status_panel": True,
        },
        now=datetime(2026, 10, 20, 12, tzinfo=timezone.utc),
    )

    assert value["credential"] == "unknown"
    assert value["worker_version"] is None
    assert value["d1_rows"] is None
    assert value["requests_24h"] is None
    assert "example-cloudflare-secret" not in json.dumps(value)


def test_cloudflare_status_without_wrangler_login_is_unavailable(monkeypatch):
    from brigade import fleet_admin_stats_collector as collector

    monkeypatch.setenv("EXAMPLE_ANALYTICS_TOKEN", "example-cloudflare-secret")

    def no_login(*args, **kwargs):
        raise ValueError("Cloudflare read failed")

    def graphql(url, *, body=None, token=None):
        if body["query"] == collector.VIEWER_QUERY:
            return {"data": {"viewer": {"accounts": [{"accountTag": "example-account"}]}}}
        return {"data": {"viewer": {"accounts": [{"workersInvocationsAdaptive": []}]}}}

    monkeypatch.setattr(collector, "run_wrangler", no_login)
    monkeypatch.setattr(collector, "fetch_json", graphql)
    value = collector.cloudflare_status(
        {
            "worker": "example-worker",
            "database": "example-db",
            "account": "example-account",
            "analytics_token_env": "EXAMPLE_ANALYTICS_TOKEN",
            "status_panel": True,
        },
        now=datetime(2026, 10, 20, 12, tzinfo=timezone.utc),
    )

    assert value["credential"] == "unavailable"
    assert value["worker_version"] is None
    assert value["d1_rows"] is None
    assert "example-cloudflare-secret" not in json.dumps(value)


def test_cloudflare_status_normalizes_read_only_fixture_data(monkeypatch):
    from brigade import fleet_admin_stats_collector as collector

    monkeypatch.setenv("EXAMPLE_ANALYTICS_TOKEN", "example-cloudflare-secret")
    calls = []

    def wrangler(argv, **kwargs):
        calls.append(argv)
        assert "example-cloudflare-secret" not in json.dumps(kwargs)
        if argv[1:3] == ["deployments", "status"]:
            return {
                "id": "example-deployment",
                "created_on": "2026-10-20T11:54:00Z",
                "versions": [{"version_id": "example-version", "percentage": 100}],
            }
        if argv[1:3] == ["deployments", "list"]:
            return []
        if argv[1:3] == ["versions", "list"]:
            return [{"id": "example-version", "metadata": {"created_on": "2026-10-20T11:55:00Z"}}]
        if argv[1:3] == ["d1", "info"]:
            return {"name": "example-db"}
        return [{"success": True, "results": [{"row_count": 128}]}]

    def graphql(url, *, body=None, token=None):
        assert url.endswith("/graphql")
        assert token == "example-cloudflare-secret"
        if body["query"] == collector.VIEWER_QUERY:
            return {"data": {"viewer": {"accounts": [{"accountTag": "example-account"}]}}}
        assert dict(re.findall(r"\$(\w+):\s*(\w+!)", body["query"])) == {
            "account": "string!",
            "worker": "string!",
            "start": "Time!",
            "end": "Time!",
        }
        assert body["variables"]["start"] == "2026-10-19T12:00:00+00:00"
        assert body["variables"]["end"] == "2026-10-20T12:00:00+00:00"
        return {
            "data": {"viewer": {"accounts": [{"workersInvocationsAdaptive": [{"sum": {"requests": 20, "errors": 1}}]}]}}
        }

    monkeypatch.setattr(collector, "run_wrangler", wrangler)
    monkeypatch.setattr(collector, "fetch_json", graphql)
    value = collector.cloudflare_status(
        {
            "status_panel": True,
            "worker": "example-worker",
            "database": "example-db",
            "account": "example-account",
            "repository_head": "a" * 40,
            "analytics_token_env": "EXAMPLE_ANALYTICS_TOKEN",
        },
        now=datetime(2026, 10, 20, 12, tzinfo=timezone.utc),
    )
    assert value["credential"] == "available"
    assert value["worker_version"] == "example-version"
    assert value["worker_published_at"] == "2026-10-20T11:55:00Z"
    assert value["repository_head"] == "a" * 40
    assert value["d1_rows"] == 128
    assert value["last_purge"] is None
    assert value["requests_24h"] == 20
    assert value["errors_24h"] == 1
    assert len(calls) == 5
    assert "example-cloudflare-secret" not in json.dumps(value)


@pytest.mark.parametrize("failed_action", ["d1-info", "d1-counts"])
def test_cloudflare_status_preserves_successful_reads_when_a_later_read_fails(monkeypatch, failed_action):
    from brigade import fleet_admin_stats_collector as collector

    monkeypatch.setenv("EXAMPLE_ANALYTICS_TOKEN", "example-cloudflare-secret")

    def wrangler(argv, **kwargs):
        action = argv[1:3]
        if failed_action == "d1-info" and action == ["d1", "info"]:
            raise ValueError("Cloudflare read failed")
        if failed_action == "d1-counts" and action == ["d1", "execute"]:
            raise ValueError("Cloudflare read failed")
        if action == ["deployments", "status"]:
            return {
                "id": "example-deployment",
                "created_on": "2026-10-20T11:54:00Z",
                "versions": [{"version_id": "example-version", "percentage": 100}],
            }
        if action == ["deployments", "list"]:
            return []
        if action == ["versions", "list"]:
            return [{"id": "example-version", "metadata": {"created_on": "2026-10-20T11:55:00Z"}}]
        if action == ["d1", "info"]:
            return {"name": "example-db"}
        return [{"success": True, "results": [{"row_count": 128}]}]

    def graphql(url, *, body=None, token=None):
        if body["query"] == collector.VIEWER_QUERY:
            return {"data": {"viewer": {"accounts": [{"accountTag": "example-account"}]}}}
        return {
            "data": {"viewer": {"accounts": [{"workersInvocationsAdaptive": [{"sum": {"requests": 20, "errors": 1}}]}]}}
        }

    monkeypatch.setattr(collector, "run_wrangler", wrangler)
    monkeypatch.setattr(collector, "fetch_json", graphql)
    value = collector.cloudflare_status(
        {
            "status_panel": True,
            "worker": "example-worker",
            "database": "example-db",
            "account": "example-account",
            "analytics_token_env": "EXAMPLE_ANALYTICS_TOKEN",
        },
        now=datetime(2026, 10, 20, 12, tzinfo=timezone.utc),
    )

    assert value["credential"] == "available"
    assert value["worker_version"] == "example-version"
    assert value["worker_published_at"] == "2026-10-20T11:55:00Z"
    assert value["d1_rows"] == (None if failed_action == "d1-counts" else 128)
    assert value["requests_24h"] == 20
    assert value["errors_24h"] == 1


def test_cloudflare_status_reads_worker_without_database(monkeypatch):
    from brigade import fleet_admin_stats_collector as collector

    monkeypatch.setenv("EXAMPLE_ANALYTICS_TOKEN", "example-cloudflare-secret")
    calls = []

    def wrangler(argv, **kwargs):
        calls.append(argv)
        if argv[1:3] == ["deployments", "status"]:
            return {"id": "example-deployment", "versions": [{"version_id": "example-version"}]}
        if argv[1:3] == ["versions", "list"]:
            return [{"id": "example-version", "metadata": {"created_on": "2026-10-20T11:55:00Z"}}]
        if argv[1:3] == ["deployments", "list"]:
            return []
        pytest.fail(f"unexpected Wrangler action: {argv[1:3]}")

    def graphql(url, *, body=None, token=None):
        if body["query"] == collector.VIEWER_QUERY:
            return {"data": {"viewer": {"accounts": [{"accountTag": "example-account"}]}}}
        return {"data": {"viewer": {"accounts": [{"workersInvocationsAdaptive": []}]}}}

    monkeypatch.setattr(collector, "run_wrangler", wrangler)
    monkeypatch.setattr(collector, "fetch_json", graphql)
    value = collector.cloudflare_status(
        {
            "status_panel": True,
            "worker": "example-worker",
            "account": "example-account",
            "analytics_token_env": "EXAMPLE_ANALYTICS_TOKEN",
        },
        now=datetime(2026, 10, 20, 12, tzinfo=timezone.utc),
    )

    assert value["worker_version"] == "example-version"
    assert value["worker_published_at"] == "2026-10-20T11:55:00Z"
    assert value["d1_rows"] is None
    assert len(calls) == 3


def test_cloudflare_status_reads_database_without_worker(monkeypatch):
    from brigade import fleet_admin_stats_collector as collector

    monkeypatch.delenv("EXAMPLE_ANALYTICS_TOKEN", raising=False)
    calls = []

    def wrangler(argv, **kwargs):
        calls.append(argv)
        if argv[1:3] == ["d1", "info"]:
            return {"name": "example-db"}
        if argv[1:3] == ["d1", "execute"]:
            return [{"success": True, "results": [{"row_count": 128}]}]
        pytest.fail(f"unexpected Wrangler action: {argv[1:3]}")

    def graphql(*args, **kwargs):
        pytest.fail("Worker analytics read ran without a configured Worker")

    monkeypatch.setattr(collector, "run_wrangler", wrangler)
    monkeypatch.setattr(collector, "fetch_json", graphql)
    value = collector.cloudflare_status(
        {"status_panel": True, "database": "example-db", "d1_token_env": "EXAMPLE_D1_TOKEN"},
        now=datetime(2026, 10, 20, 12, tzinfo=timezone.utc),
    )

    assert value["d1_rows"] == 128
    assert value["credential"] == "available"
    assert value["worker_version"] is None
    assert len(calls) == 2


def test_cloudflare_status_requires_viewer_access_to_configured_account(monkeypatch):
    from brigade import fleet_admin_stats_collector as collector

    monkeypatch.setenv("EXAMPLE_ANALYTICS_TOKEN", "example-cloudflare-secret")
    analytics_queries = []

    def wrangler(argv, **kwargs):
        if argv[1:3] == ["deployments", "status"]:
            return {"id": "example-deployment", "versions": [{"version_id": "example-version"}]}
        if argv[1:3] == ["versions", "list"]:
            return [{"id": "example-version"}]
        return []

    def graphql(url, *, body=None, token=None):
        analytics_queries.append(body["query"])
        return {"data": {"viewer": {"accounts": [{"accountTag": "different-account"}]}}}

    monkeypatch.setattr(collector, "run_wrangler", wrangler)
    monkeypatch.setattr(collector, "fetch_json", graphql)
    value = collector.cloudflare_status(
        {
            "status_panel": True,
            "worker": "example-worker",
            "account": "example-account",
            "analytics_token_env": "EXAMPLE_ANALYTICS_TOKEN",
        },
        now=datetime(2026, 10, 20, 12, tzinfo=timezone.utc),
    )

    assert analytics_queries == [collector.VIEWER_QUERY]
    assert value["credential"] == "unknown"
    assert value["requests_24h"] is None
    assert value["errors_24h"] is None


def test_cloudflare_status_does_not_choose_one_gradual_deployment_version(monkeypatch):
    from brigade import fleet_admin_stats_collector as collector

    monkeypatch.setenv("EXAMPLE_ANALYTICS_TOKEN", "example-cloudflare-secret")

    def wrangler(argv, **kwargs):
        if argv[1:3] == ["deployments", "status"]:
            return {
                "id": "example-deployment",
                "versions": [
                    {"version_id": "example-version-a", "percentage": 50},
                    {"version_id": "example-version-b", "percentage": 50},
                ],
            }
        if argv[1:3] == ["versions", "list"]:
            return [
                {"id": "example-version-a", "metadata": {"created_on": "2026-10-20T11:55:00Z"}},
                {"id": "example-version-b", "metadata": {"created_on": "2026-10-20T11:56:00Z"}},
            ]
        return []

    def graphql(url, *, body=None, token=None):
        if body["query"] == collector.VIEWER_QUERY:
            return {"data": {"viewer": {"accounts": [{"accountTag": "example-account"}]}}}
        return {"data": {"viewer": {"accounts": [{"workersInvocationsAdaptive": []}]}}}

    monkeypatch.setattr(collector, "run_wrangler", wrangler)
    monkeypatch.setattr(collector, "fetch_json", graphql)
    value = collector.cloudflare_status(
        {
            "status_panel": True,
            "worker": "example-worker",
            "account": "example-account",
            "analytics_token_env": "EXAMPLE_ANALYTICS_TOKEN",
        },
        now=datetime(2026, 10, 20, 12, tzinfo=timezone.utc),
    )

    assert value["credential"] == "available"
    assert value["worker_version"] is None
    assert value["worker_published_at"] is None


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

    import sys

    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "example-secret")
    monkeypatch.setenv("EXAMPLE_D1_TOKEN", "example-d1-secret")
    monkeypatch.setenv("EXAMPLE_ANALYTICS_TOKEN", "example-analytics-secret")
    original = collector.subprocess.Popen

    def run(argv, **kwargs):
        assert kwargs["shell"] is False
        assert kwargs["stdin"] == collector.subprocess.DEVNULL
        assert "CLOUDFLARE_API_TOKEN" not in kwargs["env"]
        assert "EXAMPLE_D1_TOKEN" not in kwargs["env"]
        assert "EXAMPLE_ANALYTICS_TOKEN" not in kwargs["env"]
        assert "example-secret" not in str(argv)
        return original([sys.executable, "-c", "import sys; sys.stderr.write('example-secret'); sys.exit(1)"], **kwargs)

    monkeypatch.setattr(collector.subprocess, "Popen", run)
    with pytest.raises(ValueError, match="D1 read failed") as error:
        collector.run_wrangler(
            collector.wrangler_argv("example-db", "weekly"),
            token_envs=("EXAMPLE_D1_TOKEN", "EXAMPLE_ANALYTICS_TOKEN"),
        )
    assert "example-secret" not in str(error.value)


def test_analytics_and_downloads_fixture_transports(monkeypatch):
    from brigade import fleet_admin_stats_collector as collector

    monkeypatch.setattr(collector, "run_wrangler", lambda argv, **kwargs: [{"success": True, "results": []}])
    requests = []

    def fetch(url, *, body=None, token=None):
        requests.append((url, body, token))
        if body:
            # Cloudflare uses its lowercase string scalar for account and script filters.
            assert dict(re.findall(r"\$(\w+):\s*(\w+!)", body["query"])) == {
                "account": "string!",
                "worker": "string!",
                "start": "Time!",
                "end": "Time!",
            }
            assert body["variables"]["account"] == "example-account"
            assert body["variables"]["start"] == "2026-09-20T00:00:00+00:00"
            assert body["variables"]["end"] == "2026-10-20T00:00:00+00:00"
            return {
                "data": {
                    "viewer": {
                        "accounts": [
                            {
                                "workersInvocationsAdaptive": [
                                    {"dimensions": {"date": "2026-10-19"}, "sum": {"requests": 100, "errors": 0}}
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
        now=datetime(2026, 10, 20, 12, tzinfo=timezone.utc),
    )
    assert value["daily"] == [{"day": "2026-10-19", "worker_requests": 100, "worker_errors": 0}]
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


@pytest.mark.parametrize(
    "now", [datetime(2026, 10, 20, tzinfo=timezone.utc), datetime(2026, 10, 18, 23, 59, 30, tzinfo=timezone.utc)]
)
def test_oldest_truncated_week_is_partial(monkeypatch, now):
    from brigade import fleet_admin_stats_collector as collector

    def runner(argv, **kwargs):
        rows = (
            [{"week": "2026-W30", "active_sources": 8, "retained_2d_sources": 8, "retained_3d_sources": 8}]
            if argv[-1] == collector.QUERIES["weekly"]
            else []
        )
        return [{"success": True, "results": rows}]

    monkeypatch.setattr(collector, "run_wrangler", runner)
    value = collector.collect({"database": "example-db"}, now=now)
    # Include the boundary Monday because D1 may evaluate after UTC midnight.
    assert value["weeks"][0]["partial"] is True


def test_wrangler_account_setting_is_environment_only(monkeypatch):
    import sys
    from brigade import fleet_admin_stats_collector as collector

    original = collector.subprocess.Popen

    def run(argv, **kwargs):
        assert "example-account" not in argv
        assert kwargs["env"]["CLOUDFLARE_ACCOUNT_ID"] == "example-account"
        return original([sys.executable, "-c", 'print(\'[{"success": true, "results": []}]\')'], **kwargs)

    monkeypatch.setattr(collector.subprocess, "Popen", run)
    assert collector.run_wrangler(collector.wrangler_argv("example-db", "weekly"), account="example-account") == [
        {"success": True, "results": []}
    ]


@pytest.mark.parametrize("malformed", [False, True])
def test_collector_omits_invalid_cells_and_keeps_valid_histograms_and_other_sources(monkeypatch, malformed):
    from brigade import fleet_admin_stats_collector as collector

    def runner(argv, **kwargs):
        if argv[-1] == collector.QUERIES["weekly"]:
            rows = [{"week": "2026-W43", "active_sources": 20, "retained_2d_sources": 20, "retained_3d_sources": 20}]
        elif argv[-1] == collector.QUERIES["versions"]:
            rows = [
                {"week": "2026-W43", "version": version, "active_sources": 20}
                for version in (
                    ("dev", "0.28.0+local", "0.28.0-beta.1", "0.31.0.dev20260808")
                    if malformed
                    else ("0.31.0.dev20260808",)
                )
            ]
        else:
            rows = [
                {"week": "2026-W43", "from_version": version, "to_version": "0.31.0.dev20260808", "active_sources": 20}
                for version in (("dev", "0.28.0") if malformed else ("0.28.0",))
            ]
        if malformed and argv[-1] != collector.QUERIES["weekly"]:
            rows.extend([{"week": "2026-W43"}, {"week": "2026-W43", "version": "0.28.0", "active_sources": "bad"}])
        return [{"success": True, "results": rows}]

    monkeypatch.setattr(collector, "run_wrangler", runner)
    monkeypatch.setattr(
        collector,
        "fetch_json",
        lambda *a, **k: {
            "schemaVersion": 1,
            "period": {"days": 30, "timezone": "UTC"},
            "sources": {"pypi": {"downloads": 12}, "npm": {"downloads": 8}},
        },
    )
    value = collector.collect(
        {"database": "example-db", "downloads_url": "https://example.invalid/counts.json"},
        now=datetime(2026, 10, 20, tzinfo=timezone.utc),
    )
    assert value["source_set"] == ["d1", "downloads"]
    assert value["weeks"][0]["versions"] == [{"version": "0.31.0.dev20260808", "active_sources": 20}]
    assert value["weeks"][0]["upgrades"] == [
        {"from_version": "0.28.0", "to_version": "0.31.0.dev20260808", "active_sources": 20}
    ]
    assert value["downloads"] == {"pypi": 12, "npm": 8}


@pytest.mark.parametrize("counts", [(10, 6, 4), (8, 9, 3)])
def test_rejected_version_keeps_unrelated_valid_cells_and_week_totals(monkeypatch, counts):
    from brigade import fleet_admin_stats_collector as collector

    def runner(argv, **kwargs):
        if argv[-1] == collector.QUERIES["weekly"]:
            rows = [{"week": "2026-W43", "active_sources": 20, "retained_2d_sources": 20, "retained_3d_sources": 20}]
        elif argv[-1] == collector.QUERIES["versions"]:
            rows = [
                {"week": "2026-W43", "version": version, "active_sources": count}
                for version, count in zip(("0.27.0", "0.28.0", "dev"), counts, strict=True)
            ]
        else:
            rows = []
        return [{"success": True, "results": rows}]

    monkeypatch.setattr(collector, "run_wrangler", runner)
    value = collector.collect({"database": "example-db"}, now=datetime(2026, 10, 20, tzinfo=timezone.utc))
    assert value["weeks"][0]["active_sources"] == 20
    assert value["weeks"][0]["versions"] == [
        {"version": version, "active_sources": count}
        for version, count in zip(("0.27.0", "0.28.0"), counts[:2], strict=True)
    ]


@pytest.mark.parametrize("kind,limit", [("versions", 6000), ("upgrades", 12000)])
@pytest.mark.parametrize("at_limit", [False, True])
def test_global_histogram_limit_cannot_expose_truncated_week(monkeypatch, kind, limit, at_limit):
    from brigade import fleet_admin_stats_collector as collector

    def cell(week, index):
        return {
            "week": week,
            "active_sources": 10,
            **(
                {"version": f"0.28.{index}"}
                if kind == "versions"
                else {"from_version": "0.27.0", "to_version": f"0.28.{index}"}
            ),
        }

    def runner(argv, **kwargs):
        if argv[-1] == collector.QUERIES["weekly"]:
            rows = [{"week": "2026-W42", "active_sources": 20, "retained_2d_sources": 20, "retained_3d_sources": 20}]
        elif argv[-1] == collector.QUERIES[kind]:
            # The global SQL limit can cut off an older week's small group,
            # even though that week has fewer cells than its per-week cap.
            rows = [cell("2026-W43", i) for i in range(limit - (1 if at_limit else 2))]
            rows.append(cell("2026-W42", 0))
        else:
            rows = []
        return [{"success": True, "results": rows}]

    monkeypatch.setattr(collector, "run_wrangler", runner)
    value = collector.collect({"database": "example-db"}, now=datetime(2026, 10, 20, tzinfo=timezone.utc))
    assert value["source_set"] == ["d1"]
    expected = [] if at_limit else [{key: val for key, val in cell("2026-W42", 0).items() if key != "week"}]
    assert value["weeks"][0][kind] == expected


@pytest.mark.parametrize("kind,limit", [("versions", 100), ("upgrades", 200)])
def test_oversized_week_histogram_becomes_unknown(monkeypatch, kind, limit):
    from brigade import fleet_admin_stats_collector as collector

    def runner(argv, **kwargs):
        if argv[-1] == collector.QUERIES["weekly"]:
            rows = [{"week": "2026-W43", "active_sources": 20, "retained_2d_sources": 8, "retained_3d_sources": 5}]
        elif argv[-1] == collector.QUERIES[kind]:
            rows = [
                {
                    "week": "2026-W43",
                    "active_sources": 5,
                    **(
                        {"version": f"0.28.{i}"}
                        if kind == "versions"
                        else {"from_version": "0.27.0", "to_version": f"0.28.{i}"}
                    ),
                }
                for i in range(limit + 1)
            ]
        else:
            rows = []
        return [{"success": True, "results": rows}]

    monkeypatch.setattr(collector, "run_wrangler", runner)
    value = collector.collect({"database": "example-db"}, now=datetime(2026, 10, 20, tzinfo=timezone.utc))
    assert value["source_set"] == ["d1"]
    assert all(value["weeks"][0][field] is None for field in collector.stats.SOURCE_FIELDS)
    assert value["weeks"][0]["versions"] == value["weeks"][0]["upgrades"] == []


@pytest.mark.parametrize("kind,limit", [("versions", 6000), ("upgrades", 12000)])
@pytest.mark.parametrize("failure", ["limit", "unavailable"])
def test_incomplete_histogram_hides_both_distributions(monkeypatch, kind, limit, failure):
    from brigade import fleet_admin_stats_collector as collector

    def runner(argv, **kwargs):
        query = next(key for key, sql in collector.QUERIES.items() if sql == argv[-1])
        if query == "weekly":
            rows = [{"week": "2026-W43", "active_sources": 20, "retained_2d_sources": 20, "retained_3d_sources": 20}]
        elif query == kind:
            if failure == "unavailable":
                raise ValueError("unavailable")
            rows = [{}] * limit
        elif query == "versions":
            rows = [
                {"week": "2026-W43", "version": version, "active_sources": count}
                for version, count in (("0.27.0", 12), ("0.28.0", 9))
            ]
        else:
            rows = [{"week": "2026-W43", "from_version": "0.27.0", "to_version": "0.28.0", "active_sources": 5}]
        return [{"success": True, "results": rows}]

    monkeypatch.setattr(collector, "run_wrangler", runner)
    value = collector.collect({"database": "example-db"}, now=datetime(2026, 10, 20, tzinfo=timezone.utc))
    assert value["source_set"] == ["d1"]
    week = value["weeks"][0]
    assert week["versions"] == week["upgrades"] == []
    assert week["active_sources"] == 20


def test_equivalent_release_spellings_are_not_upgrades(monkeypatch):
    from brigade import fleet_admin_stats_collector as collector

    def runner(argv, **kwargs):
        if argv[-1] == collector.QUERIES["weekly"]:
            rows = [{"week": "2026-W43", "active_sources": 8, "retained_2d_sources": 8, "retained_3d_sources": 8}]
        elif argv[-1] == collector.QUERIES["upgrades"]:
            rows = [{"week": "2026-W43", "from_version": "0.28", "to_version": "0.28.0", "active_sources": 8}]
        else:
            rows = []
        return [{"success": True, "results": rows}]

    monkeypatch.setattr(collector, "run_wrangler", runner)
    value = collector.collect({"database": "example-db"}, now=datetime(2026, 10, 20, tzinfo=timezone.utc))
    assert value["weeks"][0]["upgrades"] == []


def test_wrangler_output_is_bounded_while_child_is_running(monkeypatch):
    import subprocess
    import sys
    import time
    from brigade import fleet_admin_stats_collector as collector

    original = subprocess.Popen
    children = []

    def launch(argv, **kwargs):
        # Substitute a local writer, never invoke wrangler. It stays alive after
        # overflowing stdout so rejection must terminate it before the timeout.
        child = original(
            [
                sys.executable,
                "-c",
                "import sys,time; sys.stdout.buffer.write(b'x'*8192); sys.stdout.flush(); time.sleep(30)",
            ],
            **kwargs,
        )
        children.append(child)
        return child

    monkeypatch.setattr(collector, "_MAX_RESPONSE", 4096)
    monkeypatch.setattr(collector.subprocess, "Popen", launch)
    started = time.monotonic()
    with pytest.raises(ValueError, match="D1 read failed"):
        collector.run_wrangler(collector.wrangler_argv("example-db", "weekly"))
    assert time.monotonic() - started < 5
    assert children[0].poll() is not None


@pytest.mark.parametrize("no_publish", [False, True])
def test_cli_refuses_empty_source_set(monkeypatch, capsys, no_publish):
    import argparse
    from brigade import fleet_admin_stats_collector as collector

    monkeypatch.setattr(collector.stats, "config", lambda: {})
    monkeypatch.setattr(collector, "publish", lambda value: pytest.fail("empty snapshot was published"))
    assert collector.dispatch(argparse.Namespace(no_publish=no_publish)) == 1
    captured = capsys.readouterr()
    assert not captured.out
    assert "Stats collection failed" in captured.err


@pytest.mark.parametrize("day", ["not-a-date", "2026-10-21", "20261019"])
def test_invalid_analytics_preserves_successful_providers(monkeypatch, day):
    from brigade import fleet_admin_stats_collector as collector

    monkeypatch.setenv("EXAMPLE_ANALYTICS_TOKEN", "example-secret")
    monkeypatch.setattr(collector, "run_wrangler", lambda *a, **k: [{"success": True, "results": []}])

    def fetch(url, **kwargs):
        if kwargs.get("body"):
            return {
                "data": {
                    "viewer": {
                        "accounts": [
                            {
                                "workersInvocationsAdaptive": [
                                    {"dimensions": {"date": day}, "sum": {"requests": 100, "errors": 0}}
                                ]
                            }
                        ]
                    }
                }
            }
        return {
            "schemaVersion": 1,
            "period": {"days": 30, "timezone": "UTC"},
            "sources": {"pypi": {"downloads": 12}, "npm": {"downloads": 8}},
        }

    monkeypatch.setattr(collector, "fetch_json", fetch)
    value = collector.collect(
        {
            "database": "example-db",
            "account": "example-account",
            "worker": "example-worker",
            "analytics_token_env": "EXAMPLE_ANALYTICS_TOKEN",
            "downloads_url": "https://example.invalid/counts.json",
        },
        now=datetime(2026, 10, 20, tzinfo=timezone.utc),
    )
    assert value["source_set"] == ["d1", "downloads"]
    assert value["daily"] == []
    assert value["downloads"] == {"pypi": 12, "npm": 8}


def test_wrangler_excludes_custom_and_brigade_credentials(monkeypatch):
    from brigade import fleet_admin_stats_collector as collector

    for name in (
        "EXAMPLE_ANALYTICS_TOKEN",
        "EXAMPLE_D1_TOKEN",
        "BRIGADE_FLEET_TOKEN",
        "BRIGADE_FLEET_NODE_TOKEN",
        "BRIGADE_MCP_HTTP_TOKEN",
        "BRIGADE_AUTHORITY_KEY_FILE",
    ):
        monkeypatch.setenv(name, "example-secret")

    def output(argv, env):
        leaked = [
            key for key in env if key.startswith("BRIGADE_") or key in ("EXAMPLE_ANALYTICS_TOKEN", "EXAMPLE_D1_TOKEN")
        ]
        assert leaked == []
        return b'[{"success": true, "results": []}]'

    monkeypatch.setattr(collector, "_bounded_output", output)
    value = collector.collect(
        {"database": "example-db", "analytics_token_env": "EXAMPLE_ANALYTICS_TOKEN", "d1_token_env": "EXAMPLE_D1_TOKEN"}
    )
    assert value["source_set"] == ["d1"]


def test_d1_token_transport_uses_only_fixed_queries(monkeypatch):
    from brigade import fleet_admin_stats_collector as collector

    monkeypatch.setenv("EXAMPLE_D1_TOKEN", "example-secret")
    monkeypatch.setattr(collector, "run_wrangler", lambda *a, **k: pytest.fail("token transport invoked wrangler"))
    calls = []

    def fetch(url, *, body, token):
        calls.append(body["sql"])
        assert url == "https://api.cloudflare.com/client/v4/accounts/example-account/d1/database/example-db-id/query"
        assert token == "example-secret"
        assert body["sql"] in collector.QUERIES.values()
        rows = (
            [{"week": "2026-W43", "active_sources": 10, "retained_2d_sources": 10, "retained_3d_sources": 10}]
            if body["sql"] == collector.QUERIES["weekly"]
            else []
        )
        return {"success": True, "result": [{"success": True, "results": rows}]}

    monkeypatch.setattr(collector, "fetch_json", fetch)
    value = collector.collect(
        {"account": "example-account", "database_id": "example-db-id", "d1_token_env": "EXAMPLE_D1_TOKEN"},
        now=datetime(2026, 10, 20, tzinfo=timezone.utc),
    )
    assert calls == list(collector.QUERIES.values())
    assert value["source_set"] == ["d1"]
    assert value["weeks"][0]["active_sources"] == 10
    assert "example-secret" not in json.dumps(value)


@pytest.mark.parametrize(
    "response",
    [
        {"success": False, "errors": ["example-private-error"]},
        {"success": True, "result": [{"success": False, "results": []}]},
    ],
)
def test_failed_d1_rest_does_not_fall_back_to_wrangler(monkeypatch, response):
    from brigade import fleet_admin_stats_collector as collector

    monkeypatch.setenv("EXAMPLE_D1_TOKEN", "example-secret")
    monkeypatch.setattr(collector, "run_wrangler", lambda *a, **k: pytest.fail("REST failure invoked wrangler"))
    monkeypatch.setattr(collector, "fetch_json", lambda *a, **k: response)
    value = collector.collect(
        {
            "account": "example-account",
            "database": "example-db",
            "database_id": "example-db-id",
            "d1_token_env": "EXAMPLE_D1_TOKEN",
        }
    )
    assert value["source_set"] == []
    assert value["weeks"] == []
    assert "example-private-error" not in json.dumps(value)


def test_d1_rest_refuses_arbitrary_sql_before_transport(monkeypatch):
    from brigade import fleet_admin_stats_collector as collector

    monkeypatch.setattr(collector, "fetch_json", lambda *a, **k: pytest.fail("unapproved SQL reached transport"))
    with pytest.raises(ValueError, match="unknown stats query"):
        collector._d1(
            {"account": "example-account", "database_id": "example-db-id"}, "SELECT * FROM version_observations"
        )
