"""MCP servers page: node payload, hub intake, classification, page, routes, and the CLI."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

from brigade import (
    __version__,
    fleet_deck_sidebar,
    fleet_hub,
    fleet_mcp_inventory as inventory,
    fleet_mcp_inventory_page as page_mod,
    fleet_mcp_report,
)
from tests._home import set_home
from tests.fixtures import fleet_mcp_reports as fixture
from tests.test_fleet_skills_review import Hub as _Hub

NOW = datetime(2026, 10, 27, 12, tzinfo=timezone.utc)
LINUX1 = "11111111-1111-4111-8111-111111111111"
LINUX2 = "22222222-2222-4222-8222-222222222222"
WINDOWS1 = "33333333-3333-4333-8333-333333333333"
NODES = {LINUX1: "worker-linux-1", LINUX2: "worker-linux-2", WINDOWS1: "worker-windows-1"}
SLOTS = {"linux1": LINUX1, "linux2": LINUX2, "windows1": WINDOWS1}


def seeded(tmp_path, *, only=None):
    path = tmp_path / "hub" / "fleet.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    fleet_hub.init_db(path).close()
    conn = fleet_hub.open_db(path)
    tokens = {}
    for node, label in NODES.items():
        _status, payload = fleet_hub.handle_node_request(conn, {"action": "add", "node_id": node, "label": label})
        tokens[node] = payload["token"]
    for slot, report in fixture.reports(NOW, hub_version=__version__).items():
        if only is None or slot in only:
            inventory.store_report(conn, SLOTS[slot], report, now=NOW)
    return conn, path, tokens


def view_for(conn):
    return inventory.load_view(conn)


def server(view, name):
    return next(item for item in view.servers if item.name == name)


def valid():
    return copy.deepcopy(fixture.reports(NOW)["linux1"])


# ---------------------------------------------------------------- node payload

SECRETS = ("SECRETENV", "SECRETHEADER", "SECRETARG", "SECRETQUERY", "SECRETUSER", "SECRETPASS", "SECRETCMD")


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    set_home(monkeypatch, home)
    ws = tmp_path / "ws"
    (ws / ".brigade").mkdir(parents=True)
    return ws


def _hostile_workspace(ws):
    catalog = {
        "version": 1,
        "servers": {
            "sample-remote": {
                "transport": "http",
                "url": "https://SECRETUSER:SECRETPASS@Mcp.Sample.Example:8443/path/SECRETQUERY?token=SECRETQUERY",  # content-guard: allow email
                "headers": {"Authorization": {"literal": "Bearer SECRETHEADER"}},
            },
            "sample-local": {
                "transport": "stdio",
                "command": "run-sample-SECRETCMD",
                "args": ["--key=SECRETARG"],
                "env": {"API_KEY": {"literal": "SECRETENV"}},
            },
            "sample-off": {"transport": "stdio", "command": "run-off", "enabled": False},
        },
    }
    (ws / ".brigade" / "mcp.json").write_text(json.dumps(catalog))
    native = {
        "mcpServers": {
            "sample-local": {"command": "run-sample-SECRETCMD", "args": ["--key=SECRETARG", "--edited"]},
            "sample-native": {
                "url": "https://SECRETUSER:SECRETPASS@native.sample.example:9000/x?token=SECRETQUERY",  # content-guard: allow email
                "headers": {"Authorization": "Bearer SECRETHEADER"},
            },
        }
    }
    (ws / ".mcp.json").write_text(json.dumps(native))


def test_hostile_config_never_leaks_secrets_paths_or_urls(workspace, tmp_path):
    _hostile_workspace(workspace)
    report = fleet_mcp_report.build_report(workspace, now=NOW)
    text = json.dumps(report)
    for secret in SECRETS:
        assert secret not in text
    for fragment in (str(tmp_path), "/home/", "https://", "8443", "9000", "/path", "token=", "Authorization", "--key"):
        assert fragment not in text
    rows = {(r["name"], r["harness"], r["scope"]): r for r in report["servers"]}
    assert rows[("sample-native", "claude", "project")]["remote_host"] == "native.sample.example"
    assert rows[("sample-native", "claude", "project")]["status"] == "unmanaged"
    assert rows[("sample-native", "claude", "project")]["managed"] is False
    assert rows[("sample-remote", "claude", "project")]["status"] == "missing"
    assert rows[("sample-remote", "claude", "project")]["remote_host"] == "mcp.sample.example"
    assert rows[("sample-local", "claude", "project")]["status"] in inventory.DRIFT_STATUSES
    assert rows[("sample-local", "claude", "project")]["remote_host"] is None
    assert rows[("sample-off", "claude", "project")]["status"] == "disabled"
    assert rows[("sample-off", "claude", "project")]["enabled"] is False
    inventory.validate_report(report)  # the node's own output passes the Hub's validation


def test_every_server_row_and_harness_row_uses_only_the_closed_fields(workspace):
    _hostile_workspace(workspace)
    report = fleet_mcp_report.build_report(workspace, now=NOW)
    assert set(report) == {
        "schema_version",
        "brigade_version",
        "observed_at",
        "catalog_present",
        "truncated",
        "servers",
        "harnesses",
    }
    assert all(
        set(row) == {"name", "harness", "scope", "transport", "enabled", "status", "managed", "remote_host"}
        for row in report["servers"]
    )
    assert all(set(row) == {"harness", "scope", "config_found", "readable"} for row in report["harnesses"])
    assert all(row["status"] in inventory.STATUSES for row in report["servers"])


def test_no_servers_is_not_the_same_as_no_config_or_unreadable_config(workspace):
    report = fleet_mcp_report.build_report(workspace, now=NOW)
    claude = next(h for h in report["harnesses"] if (h["harness"], h["scope"]) == ("claude", "project"))
    assert report["catalog_present"] is False and report["servers"] == []
    assert claude == {"harness": "claude", "scope": "project", "config_found": False, "readable": True}
    (workspace / ".mcp.json").write_text("{}")
    claude = next(
        h
        for h in fleet_mcp_report.build_report(workspace)["harnesses"]
        if h["harness"] == "claude" and h["scope"] == "project"
    )
    assert claude["config_found"] is True and claude["readable"] is True
    (workspace / ".mcp.json").write_text("{not json")
    broken = fleet_mcp_report.build_report(workspace)
    claude = next(h for h in broken["harnesses"] if h["harness"] == "claude" and h["scope"] == "project")
    assert claude["config_found"] is True and claude["readable"] is False
    assert not [r for r in broken["servers"] if r["harness"] == "claude"]


def test_report_is_bounded_and_marks_truncation(monkeypatch, workspace):
    rows = [fixture.server(f"sample-{i:04d}", "claude") for i in range(320)]
    monkeypatch.setattr(fleet_mcp_report, "build_servers", lambda target: (True, rows, [fixture.harness("claude")]))
    report = fleet_mcp_report.build_report(workspace, now=NOW)
    assert len(report["servers"]) == fleet_mcp_report.MAX_ROWS == inventory.MAX_ROWS and report["truncated"] is True
    inventory.validate_report(report)


@pytest.mark.parametrize(
    ("url", "host"),
    [
        ("https://user:pw@Host.Example:8443/a?t=1", "host.example"),
        ("http://plain.example", "plain.example"),
        ("http://[::1]/x", None),
        ("file:///etc/passwd", None),
        ("https://bad_host.example/", None),
        ("not a url", None),
        (None, None),
        (42, None),
    ],
)
def test_remote_host_is_a_bare_hostname_or_nothing(url, host):
    assert fleet_mcp_report.remote_host(url) == host


# ---------------------------------------------------------------- validation


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r["servers"][0].update(env={"KEY": "value"}),
        lambda r: r["servers"][0].update(url="https://host.example/path?token=x"),
        lambda r: r["servers"][0].update(args=["--key=x"]),
        lambda r: r["servers"][0].update(remote_host="host.example/path"),
        lambda r: r["servers"][0].update(remote_host="user@host.example"),  # content-guard: allow email
        lambda r: r["servers"][0].update(remote_host="host.example:8443"),
        lambda r: r["servers"][0].update(remote_host="https://host.example"),
        lambda r: r["servers"][0].update(transport="stdio", remote_host="host.example"),
        lambda r: r["servers"][0].update(status="exploded"),
        lambda r: r["servers"][0].update(transport="carrier-pigeon"),
        lambda r: r["servers"][0].update(scope="global"),
        lambda r: r["servers"][0].update(name="/home/someone/server"),  # content-guard: allow home-path
        lambda r: r["servers"][0].update(name=""),
        lambda r: r["servers"][0].update(name="x" * 500),
        lambda r: r["servers"][0].update(name="line\nbreak"),
        lambda r: r["servers"][0].update(enabled="yes"),
        lambda r: r["servers"][0].update(managed=1),
        lambda r: r["servers"].append(dict(r["servers"][0])),
        lambda r: r["servers"].__imul__(100),
        lambda r: r["harnesses"].append(dict(r["harnesses"][0])),
        lambda r: r["harnesses"][0].update(readable="yes"),
        lambda r: r["harnesses"][0].update(path="/home/someone"),  # content-guard: allow home-path
        lambda r: r.update(schema_version=2),
        lambda r: r.update(schema_version=True),
        lambda r: r.update(observed_at="yesterday"),
        lambda r: r.update(catalog_present="yes"),
        lambda r: r.update(headers={"a": "b"}),
        lambda r: r.pop("servers"),
        lambda r: r.update(servers="all of them"),
    ],
)
def test_validation_rejects_malformed_oversized_or_secret_bearing_reports(mutate):
    report = valid()
    mutate(report)
    with pytest.raises(inventory.McpReportError):
        inventory.validate_report(report)


def test_stored_row_that_no_longer_validates_reads_as_no_report(tmp_path):
    conn, _path, _tokens = seeded(tmp_path)
    conn.execute("UPDATE fleet_mcp_reports SET report='{broken' WHERE node_id=?", (LINUX1,))
    conn.commit()
    view = view_for(conn)
    assert next(m for m in view.machines if m.node_id == LINUX1).reported is False


# ---------------------------------------------------------------- classification


def test_issue_classification_pills_and_filters(tmp_path):
    conn, _path, _tokens = seeded(tmp_path)
    view = view_for(conn)
    assert server(view, "sample-memory").issue == "ok" and not server(view, "sample-memory").flags
    assert server(view, "sample-search").issue == "drift"
    assert server(view, "sample-docs").issue == "missing on 1 machine"
    assert server(view, "sample-browser").issue == "unmanaged"
    assert server(view, "sample-browser").flags == {"unmanaged", "off"}
    counts = {key: sum(1 for s in view.servers if inventory.matches(s, key)) for key, _ in inventory.FILTERS}
    assert counts == {"needs": 3, "drift": 1, "unmanaged": 1, "all": 4}
    assert [s.name for s in view.servers] == sorted(s.name for s in view.servers)


def test_off_only_server_is_not_needs_attention(tmp_path):
    conn, _path, _tokens = seeded(tmp_path, only=set())
    report = valid()
    report["servers"] = [fixture.server("sample-quiet", "claude", "disabled", enabled=False)]
    inventory.store_report(conn, LINUX1, report, now=NOW)
    only = server(view_for(conn), "sample-quiet")
    assert only.issue == "off" and not inventory.matches(only, "needs")


def test_missing_on_several_machines_is_counted_once_per_machine(tmp_path):
    conn, _path, _tokens = seeded(tmp_path, only=set())
    for node in (LINUX1, LINUX2):
        report = valid()
        report["servers"] = [
            fixture.server("sample-gap", "claude", "missing"),
            fixture.server("sample-gap", "codex", "missing"),
        ]
        inventory.store_report(conn, node, report, now=NOW)
    assert server(view_for(conn), "sample-gap").issue == "missing on 2 machines"


def test_every_plan_status_has_a_label():
    from brigade import mcp_cmd  # the statuses the node can report come from the plan engine

    assert set(inventory.STATUSES) - {"unmanaged", "disabled"} <= set(inventory.LABELS)
    assert set(inventory.LABELS) == set(inventory.STATUSES) | set(inventory.ACCOUNT_STATUSES)
    assert mcp_cmd  # imported for the shared vocabulary above


def test_verdict_sentences_use_real_rows(tmp_path):
    conn, _path, _tokens = seeded(tmp_path)
    view = view_for(conn)
    assert inventory.verdict(server(view, "sample-memory")) == (
        "Configured on 3 machines in Claude, Codex, Gemini. Matches the Brigade catalog everywhere."
    )
    assert "Missing on worker-windows-1." in inventory.verdict(server(view, "sample-docs"))
    assert "Differs from the Brigade catalog on worker-linux-1." in inventory.verdict(server(view, "sample-search"))
    assert "Not in the Brigade catalog on worker-linux-2." in inventory.verdict(server(view, "sample-browser"))
    assert "Turned off on worker-linux-1." in inventory.verdict(server(view, "sample-browser"))


def test_replaced_report_and_older_report_ordering(tmp_path):
    conn, _path, _tokens = seeded(tmp_path)
    older = valid()
    older["observed_at"] = (NOW - timedelta(days=1)).isoformat()
    older["servers"] = older["servers"][:1]
    assert inventory.store_report(conn, LINUX1, older, now=NOW)["ignored"] is True
    newer = valid()
    newer["observed_at"] = (NOW + timedelta(hours=1)).isoformat()
    newer["servers"] = newer["servers"][:1]
    assert inventory.store_report(conn, LINUX1, newer, now=NOW)["ignored"] is False
    machine = next(m for m in view_for(conn).machines if m.node_id == LINUX1)
    assert len(machine.rows) == 1


# ---------------------------------------------------------------- page


def _render(tmp_path, *, only=None, **kw):
    conn, _path, _tokens = seeded(tmp_path, only=only)
    return page_mod.render(view_for(conn), nonce="n", now=NOW, **kw)


def _detail(page, key):
    from brigade import fleet_admin_explorer as explorer

    return page.split(f'id="{explorer.anchor(key)}"', 1)[1].split("</article>", 1)[0]


def test_server_view_default_filter_chips_and_counts(tmp_path):
    page = _render(tmp_path)
    for label in ("Needs attention 3", "Drift 1", "Unmanaged 1", "All 4"):
        assert label in page
    assert '<span class="explorer-count" data-explorer-count>3</span>' in page
    assert "sample-search" in page and "sample-docs" in page and "sample-browser" in page
    assert page.count('<a href="#record-') == 3  # sample-memory is ok and filtered out
    assert "missing on 1 machine" in page and ">drift<" in page and ">unmanaged<" in page
    for line in ("3 machines, 2 harnesses", "3 machines, 1 harness", "2 machines, 2 harnesses"):
        assert line in page
    assert "3 machines, 3 harnesses" not in page  # sample-memory is ok and filtered out
    everything = _render(tmp_path / "again", filter_key="all")
    assert everything.count('<a href="#record-') == 4 and ">ok<" in everything
    assert "3 machines, 3 harnesses" in everything


def test_matrix_shows_status_pills_dashes_unreadable_and_missing_reports(tmp_path):
    page = _render(tmp_path, filter_key="all")
    docs = _detail(page, "server:sample-docs")
    human = docs.split('data-audience="agent"', 1)[0]
    assert "<th>Machine</th><th>Claude</th><th>Codex</th><th>Cursor</th><th>Gemini</th>" in human
    assert human.count("<tr>") == 4  # header plus three machines
    assert "Missing on worker-windows-1." in human
    memory_human = _detail(page, "server:sample-memory").split('data-audience="agent"', 1)[0]
    assert 'class="none">-</td>' in memory_human
    assert 'pill--warn">unreadable<' in memory_human  # windows1 gemini config exists but cannot be read
    partial = _render(tmp_path / "partial", only={"linux1"}, filter_key="all")
    human = _detail(partial, "server:sample-memory").split('data-audience="agent"', 1)[0]
    assert human.count("No MCP report") == 2 and "worker-linux-2" in human and "worker-windows-1" in human
    assert "No MCP report: worker-linux-2, worker-windows-1" in partial


def test_machine_view_lists_harnesses_with_server_pills(tmp_path):
    page = _render(tmp_path, group="machine")
    assert "Needs attention" not in page  # server filters belong to the server view
    detail = _detail(page, f"machine:{LINUX1}")
    human = detail.split('data-audience="agent"', 1)[0]
    assert "sample-search (drift)" in human and "sample-browser (off)" in human
    assert "No config found" in human  # linux1 has no cursor config
    assert "1 needs attention" in human
    windows = _detail(page, f"machine:{WINDOWS1}").split('data-audience="agent"', 1)[0]
    assert "unreadable" in windows and "sample-docs (missing)" in windows
    partial = _render(tmp_path / "partial", only={"linux1"}, group="machine")
    unreported = _detail(partial, f"machine:{LINUX2}").split('data-audience="agent"', 1)[0]
    assert "No MCP report" in unreported and "mcp report" in unreported
    assert "pill--ok" not in unreported


def test_unreported_machine_is_never_healthy(tmp_path):
    conn, _path, _tokens = seeded(tmp_path, only={"linux1"})
    machine = next(m for m in view_for(conn).machines if m.node_id == LINUX2)
    assert machine.reported is False and machine.attention == 0
    pill = page_mod._machine_pill(machine)
    assert "No MCP report" in pill and "pill--ok" not in pill


def test_audience_tagging_hides_codes_scopes_and_hosts_from_admins(tmp_path):
    page = _render(tmp_path, filter_key="all")
    browser = _detail(page, "server:sample-browser")
    human, _sep, agent = browser.partition('data-audience="agent"')
    for hidden in ("<code>unmanaged</code>", "browser.sample.example", "<code>http</code>", "Status code", "Transport"):
        assert hidden not in human
        assert hidden in agent
    search = _detail(page, "server:sample-search").partition('data-audience="agent"')
    assert "<code>stale</code>" not in search[0] and "<code>stale</code>" in search[2]
    assert "brigade fleet mcp report" in search[2] and "brigade mcp sync" in search[2]
    note = page.rsplit('data-audience="agent"', 1)[1]
    assert "Only locally configured MCP servers are shown" in note
    assert "Only locally configured" not in page.split('data-audience="agent"', 1)[0]
    machines = _render(tmp_path / "m", group="machine")
    agent_machine = _detail(machines, f"machine:{WINDOWS1}").partition('data-audience="agent"')
    assert "Config found" in agent_machine[2] and "Config found" not in agent_machine[0]


def test_empty_state_names_the_publish_command(tmp_path):
    page = _render(tmp_path, only=set())
    assert "brigade fleet mcp report" in page and "data-command=" in page and "No MCP reports yet" in page
    assert '<section class="admin-explorer"' not in page


def test_hostile_server_machine_and_host_names_are_escaped(tmp_path):
    conn, _path, _tokens = seeded(tmp_path, only=set())
    hostile = '<img src=x onerror="alert(1)">&"\''
    conn.execute("UPDATE nodes SET label=? WHERE node_id=?", (hostile, LINUX1))
    conn.commit()
    report = valid()
    report["servers"] = [fixture.server(hostile, "claude", "stale")]
    inventory.store_report(conn, LINUX1, report, now=NOW)
    for kwargs in ({"filter_key": "all"}, {"group": "machine"}):
        page = page_mod.render(view_for(conn), nonce="n", now=NOW, **kwargs)
        assert "<img" not in page and 'onerror="' not in page
        assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in page


def test_no_em_dashes_or_inline_styles_or_folds_in_copy(tmp_path):
    conn, _path, _tokens = seeded(tmp_path)
    view = view_for(conn)
    pages = [
        page_mod.render(view, nonce="n", now=NOW, filter_key=key, group=group)
        for group in ("server", "machine")
        for key in ("needs", "drift", "unmanaged", "all")
    ]
    pages.append(_render(tmp_path / "e", only=set()))
    pages.append(_render(tmp_path / "p", only={"linux1"}, group="machine"))
    assert not any("—" in p or "–" in p for p in pages)
    assert not any(" style=" in p for p in pages)
    assert not any("<details" in p for p in pages)  # compact page: no folds


def test_unknown_group_and_filter_fall_back_to_defaults(tmp_path):
    page = _render(tmp_path, group="bogus", filter_key="bogus")
    assert page == _render(tmp_path / "again")


def test_sidebar_tab_follows_skills_and_marks_the_current_page(tmp_path):
    tabs = fleet_deck_sidebar.render_section_tabs("mcp", worklore_enabled=False)
    labels = [part.split(">")[1].split("<")[0] for part in tabs.split("<a ")[1:]]
    assert labels[labels.index("Skills") + 1] == "MCP servers"
    assert '<a href="/deck/mcp" aria-current="page">MCP servers</a>' in tabs
    assert fleet_deck_sidebar.section_for("mcp") == "roster"
    page = _render(tmp_path)
    assert '<a href="/deck/mcp" aria-current="page">MCP servers</a>' in page


# ---------------------------------------------------------------- live HTTP


class Hub(_Hub):
    def mcp_report(self, token, body, path="/mcp/report"):
        return self.request(
            "POST",
            path,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            body=json.dumps(body).encode(),
        )


def test_report_endpoint_requires_a_node_token_and_validates(tmp_path):
    conn, path, tokens = seeded(tmp_path, only=set())
    conn.close()
    with Hub(tmp_path, path) as hub:
        body = fixture.reports(NOW)["linux1"]
        assert hub.request("POST", "/mcp/report", body=b"{}")[0] == 401
        assert hub.mcp_report("not-a-token", body)[0] == 401
        assert hub.mcp_report(hub.token, body)[0] == 403  # the admin token cannot post for a node
        status, _h, text = hub.mcp_report(tokens[LINUX1], body)
        assert status == 200 and json.loads(text)["servers"] == len(body["servers"])
        for mutate in (
            lambda b: b["servers"][0].update(remote_host="host.example/path?token=x"),
            lambda b: b["servers"][0].update(env={"KEY": "value"}),
            lambda b: b["servers"][0].update(name="/home/someone/private"),  # content-guard: allow home-path
            lambda b: b["servers"].__imul__(100),
            lambda b: b.update(extra="field"),
        ):
            bad = copy.deepcopy(body)
            mutate(bad)
            assert hub.mcp_report(tokens[LINUX1], bad)[0] == 400
        assert hub.mcp_report(tokens[LINUX1], body, "/mcp/report?x=1")[0] == 400
        assert (
            hub.request(
                "POST",
                "/mcp/report",
                headers={"Authorization": f"Bearer {tokens[LINUX1]}", "Content-Type": "application/json"},
                body=b"[]",
            )[0]
            == 400
        )
        older = copy.deepcopy(body)
        older["observed_at"] = (NOW - timedelta(days=1)).isoformat()
        assert json.loads(hub.mcp_report(tokens[LINUX1], older)[2])["ignored"] is True
        status, _h, text = hub.mcp_report(
            tokens[LINUX1], {**body, "servers": [{**body["servers"][0], "status": "bogus"}]}
        )
        assert status == 400 and "status must be one of" in text


def test_mcp_route_auth_and_read_only_markup(tmp_path):
    conn, path, tokens = seeded(tmp_path)
    conn.close()
    with Hub(tmp_path, path) as hub:
        assert hub.request("GET", "/deck/mcp")[0] == 401
        assert hub.request("GET", "/deck/mcp", headers={"Authorization": "Bearer wrong"})[0] == 401
        assert hub.request("GET", "/deck/mcp", headers={"Authorization": f"Bearer {tokens[LINUX1]}"})[0] == 401
        cookie = hub.cookie()
        status, headers, page = hub.request("GET", "/deck/mcp", headers={"Cookie": cookie})
        assert status == 200 and "content-security-policy" in headers and headers["cache-control"] == "no-store"
        assert "sample-search" in page and hub.token not in page and cookie.split("=", 1)[1] not in page
        assert "<form" not in page and 'method="post"' not in page  # no write actions in phase 1
        status, _h, machines = hub.request("GET", "/deck/mcp?group=machine", headers={"Cookie": cookie})
        assert status == 200 and "worker-linux-1" in machines
        status, _h, again = hub.request("GET", "/deck/mcp?group=nope&filter=nope", headers={"Cookie": cookie})
        assert status == 200 and "Needs attention 3" in again
        assert hub.request(
            "POST",
            "/deck/mcp",
            headers={
                "Authorization": f"Bearer {hub.token}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Sec-Fetch-Site": "same-origin",
            },
            body=b"x=1",
        )[0] in {404, 405}


def test_hub_survives_a_missing_table_by_creating_it_additively(tmp_path):
    path = tmp_path / "hub" / "fleet.db"
    path.parent.mkdir(parents=True)
    fleet_hub.init_db(path).close()
    conn = fleet_hub.open_db(path)
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='fleet_mcp_reports'").fetchone() is None
    assert view_for(conn).reports == 0
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='fleet_mcp_reports'").fetchone() is not None


# ---------------------------------------------------------------- CLI


def test_cli_report_dry_run_and_serve_command_still_parses(monkeypatch, workspace, capsys):
    from brigade import cli, fleet_mcp

    (workspace / ".mcp.json").write_text(json.dumps({"mcpServers": {"sample-native": {"command": "run-sample"}}}))
    assert cli.main(["fleet", "mcp", "report", "--dry-run", "--json", "--target", str(workspace)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert any(r["name"] == "sample-native" and r["status"] == "unmanaged" for r in payload["servers"])
    inventory.validate_report(payload)
    calls = []
    monkeypatch.setattr(fleet_mcp, "serve_mcp", lambda **kwargs: calls.append(kwargs) or 0)
    assert cli.main(["fleet", "mcp", "--http", "--port", "9"]) == 0
    assert calls and calls[0]["http"] is True and calls[0]["port"] == 9


# ---------------------------------------------------------------- account connectors (opt-in)

HOSTILE_LIST = """Checking MCP server health...

claude.ai Sample Mail: https://mail.sample.example/mcp/SECRETPATH?token=SECRETQUERY - ✔ Connected
claude.ai Sample Drive: https://drive.sample.example:8443/v1 - ! Needs authentication
claude.ai Sample Broken: https://broken.sample.example/x - ✘ Failed to connect — <img src=x onerror=1> https://leak.example/SECRETERR account 1234567
claude.ai Sample Odd: https://odd.sample.example/x - something new Connected
local-sample: npx run-sample --key=SECRETARG - ✔ Connected
other.ai Sample: https://other.sample.example/ - ✔ Connected
"""


def test_parser_reads_only_claude_ai_lines_and_only_the_status_word():
    rows = fleet_mcp_report.parse_account_connectors(HOSTILE_LIST)
    by_name = {r["name"]: r for r in rows}
    assert set(by_name) == {
        "claude.ai Sample Mail",
        "claude.ai Sample Drive",
        "claude.ai Sample Broken",
        "claude.ai Sample Odd",
    }
    assert by_name["claude.ai Sample Mail"]["status"] == "connected"
    assert by_name["claude.ai Sample Drive"]["status"] == "needs_auth"
    assert by_name["claude.ai Sample Broken"]["status"] == "failed"
    assert by_name["claude.ai Sample Odd"]["status"] == "unknown"
    assert by_name["claude.ai Sample Drive"]["remote_host"] == "drive.sample.example"
    text = json.dumps(rows)
    for leaked in (
        "SECRET",
        "leak.example",
        "1234567",
        "onerror",
        "/mcp",
        "8443",
        "token=",
        "https://",
        "Failed to connect",
    ):
        assert leaked not in text
    assert all(
        r["harness"] == "claude" and r["scope"] == "account" and r["transport"] == "http" and r["managed"] is False
        for r in rows
    )
    inventory.validate_report(fixture.with_account_connectors(valid(), extra=[]))
    report = valid()
    report["servers"] += rows
    report["harnesses"].append({"harness": "claude", "scope": "account", "config_found": True, "readable": True})
    inventory.validate_report(report)  # the node's own output passes the Hub's validation


class _Run:
    def __init__(self, stdout="", returncode=0, error=None):
        self.stdout, self.returncode, self.error, self.calls = stdout, returncode, error, []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self.error:
            raise self.error
        return type("Done", (), {"stdout": self.stdout, "returncode": self.returncode})()


def test_account_connectors_are_off_by_default_and_bounded_when_on(workspace):
    run = _Run(HOSTILE_LIST)
    off = fleet_mcp_report.build_report(workspace, now=NOW, run=run)
    assert run.calls == [] and not [r for r in off["servers"] if r["scope"] == "account"]
    assert not [h for h in off["harnesses"] if h["scope"] == "account"]
    on = fleet_mcp_report.build_report(workspace, now=NOW, include_account_connectors=True, run=run)
    argv, kwargs = run.calls[0]
    assert argv == ["claude", "mcp", "list"] and kwargs["timeout"] == 60 and kwargs["capture_output"] is True
    assert len([r for r in on["servers"] if r["scope"] == "account"]) == 4
    marker = next(h for h in on["harnesses"] if h["scope"] == "account")
    assert marker == {"harness": "claude", "scope": "account", "config_found": True, "readable": True}
    inventory.validate_report(on)


@pytest.mark.parametrize(
    ("run", "found", "readable"),
    [
        (_Run(error=FileNotFoundError("claude")), False, True),
        (_Run(error=__import__("subprocess").TimeoutExpired(["claude"], 60)), True, False),
        (_Run(stdout="boom", returncode=2), True, False),
    ],
)
def test_missing_or_hung_claude_is_recorded_not_reported_as_zero_connectors(workspace, run, found, readable):
    report = fleet_mcp_report.build_report(workspace, now=NOW, include_account_connectors=True, run=run)
    marker = next(h for h in report["harnesses"] if h["scope"] == "account")
    assert (marker["config_found"], marker["readable"]) == (found, readable)
    assert not [r for r in report["servers"] if r["scope"] == "account"]
    inventory.validate_report(report)


def test_opt_in_node_setting_reads_fleet_toml(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path))
    assert fleet_mcp_report.account_connectors_enabled() is False
    (tmp_path / "fleet.toml").write_text("[fleet.mcp]\ninclude_account_connectors = true\n")
    assert fleet_mcp_report.account_connectors_enabled() is True
    (tmp_path / "fleet.toml").write_text('[fleet.mcp]\ninclude_account_connectors = "yes"\n')
    assert fleet_mcp_report.account_connectors_enabled() is False


def test_cli_flag_runs_claude_and_default_does_not(monkeypatch, workspace, capsys):
    from brigade import cli

    run = _Run(HOSTILE_LIST)
    monkeypatch.setattr(fleet_mcp_report.subprocess, "run", run)
    monkeypatch.setenv("BRIGADE_HOME", str(workspace.parent / "bhome"))
    assert cli.main(["fleet", "mcp", "report", "--dry-run", "--json", "--target", str(workspace)]) == 0
    assert run.calls == [] and "claude.ai" not in capsys.readouterr().out
    assert (
        cli.main(
            [
                "fleet",
                "mcp",
                "report",
                "--dry-run",
                "--json",
                "--include-account-connectors",
                "--target",
                str(workspace),
            ]
        )
        == 0
    )
    assert "claude.ai Sample Mail" in capsys.readouterr().out and len(run.calls) == 1


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r["servers"][-1].update(status="current"),  # a local status on an account row
        lambda r: r["servers"][0].update(status="connected"),  # a connector status on a local row
        lambda r: r["servers"][-1].update(transport="stdio", remote_host=None),
        lambda r: r["servers"][-1].update(managed=True),
        lambda r: r["servers"][-1].update(status="down"),
        lambda r: r["servers"][-1].update(error="failed: https://x.example/SECRET"),
        lambda r: r["servers"][-1].update(remote_host="drive.sample.example/path"),
    ],
)
def test_validator_stays_strict_for_account_rows(mutate):
    report = fixture.with_account_connectors(valid())
    mutate(report)
    with pytest.raises(inventory.McpReportError):
        inventory.validate_report(report)


def _account_hub(tmp_path):
    conn, path, tokens = seeded(tmp_path, only=set())
    for slot, report in fixture.reports(NOW, hub_version=__version__).items():
        if slot == "linux1":
            report = fixture.with_account_connectors(report)
        inventory.store_report(conn, SLOTS[slot], report, now=NOW)
    return conn


def test_account_connectors_classification_and_page(tmp_path):
    conn = _account_hub(tmp_path)
    view = view_for(conn)
    mail, drive = server(view, "claude.ai Sample Mail"), server(view, "claude.ai Sample Drive")
    assert mail.issue == "connected" and not inventory.matches(mail, "needs")
    assert drive.issue == "needs auth" and inventory.matches(drive, "needs")
    page = page_mod.render(view, nonce="n", now=NOW)
    assert (
        "Needs attention 4" in page
        and "claude.ai Sample Drive" in page
        and "claude.ai Sample Mail" not in page.split("data-record-detail", 1)[0]
    )
    assert ">Account<" in page and ">needs auth<" in page
    everything = page_mod.render(view, nonce="n", now=NOW, filter_key="all")
    assert ">connected<" in everything
    detail = _detail(everything, "server:claude.ai Sample Drive")
    human, _sep, agent = detail.partition('data-audience="agent"')
    assert "Claude account" in human and "Needs authentication on worker-linux-1." in human
    assert "claude mcp list" in agent and "<code>needs_auth</code>" in agent and "<code>account</code>" in agent
    assert "needs_auth" not in human
    machine = _detail(page_mod.render(view, nonce="n", now=NOW, group="machine"), f"machine:{LINUX1}")
    assert "claude.ai Sample Drive (needs auth)" in machine and "claude.ai Sample Mail" in machine
    assert "2 need attention" in machine.split('data-audience="agent"', 1)[0]
    assert not any("—" in p for p in (page, everything, machine))


def test_failed_connector_counts_and_unknown_does_not(tmp_path):
    conn, _path, _tokens = seeded(tmp_path, only=set())
    report = fixture.with_account_connectors(
        valid(),
        extra=[
            fixture.account_row("claude.ai Sample Down", "failed"),
            fixture.account_row("claude.ai Sample Odd", "unknown"),
        ],
    )
    inventory.store_report(conn, LINUX1, report, now=NOW)
    view = view_for(conn)
    assert server(view, "claude.ai Sample Down").issue == "failed" and inventory.matches(
        server(view, "claude.ai Sample Down"), "needs"
    )
    assert server(view, "claude.ai Sample Odd").issue == "unknown" and not inventory.matches(
        server(view, "claude.ai Sample Odd"), "needs"
    )


def test_hostile_connector_names_are_escaped(tmp_path):
    conn, _path, _tokens = seeded(tmp_path, only=set())
    hostile = 'claude.ai <img src=x onerror="alert(1)">'
    report = fixture.with_account_connectors(valid(), extra=[fixture.account_row(hostile, "failed")])
    inventory.store_report(conn, LINUX1, report, now=NOW)
    for kwargs in ({"filter_key": "all"}, {"group": "machine"}):
        page = page_mod.render(view_for(conn), nonce="n", now=NOW, **kwargs)
        assert "<img" not in page and 'onerror="' not in page


def test_matrix_fits_its_box_and_group_switch_has_no_floating_label(tmp_path):
    page = _render(tmp_path, filter_key="all")
    assert '<table class="mcp-matrix">' in page
    assert "table-layout:fixed" in page
    assert "<span>Group by</span>" not in page and 'aria-label="Group by"' in page


def test_transport_and_host_come_from_the_native_entry_when_it_drifts(workspace):
    catalog = {"version": 1, "servers": {"sample-drift": {"transport": "stdio", "command": "run-drift"}}}
    (workspace / ".brigade" / "mcp.json").write_text(json.dumps(catalog))
    native = {"mcpServers": {"sample-drift": {"url": "https://drift.sample.example/mcp"}}}
    (workspace / ".mcp.json").write_text(json.dumps(native))
    report = fleet_mcp_report.build_report(workspace, now=NOW)
    row = next(r for r in report["servers"] if r["name"] == "sample-drift" and r["harness"] == "claude")
    assert (row["transport"], row["remote_host"]) == ("http", "drift.sample.example")
    inventory.validate_report(report)  # the Hub accepts it


def test_existing_config_outside_the_selected_harnesses_is_still_inventoried(workspace):
    from brigade.config import Config, write_config
    from brigade.selection import Selection

    write_config(workspace, Config(version=1, selection=Selection(depth="repo", harnesses=["claude"])))
    catalog = {"version": 1, "servers": {"sample-local": {"transport": "stdio", "command": "run-sample"}}}
    (workspace / ".brigade" / "mcp.json").write_text(json.dumps(catalog))
    (workspace / ".cursor").mkdir()
    (workspace / ".cursor" / "mcp.json").write_text(json.dumps({"mcpServers": {"sample-cursor": {"command": "run-c"}}}))
    report = fleet_mcp_report.build_report(workspace, now=NOW)
    cursor_rows = [r for r in report["servers"] if (r["harness"], r["scope"]) == ("cursor", "project")]
    assert [(r["name"], r["status"], r["managed"]) for r in cursor_rows] == [("sample-cursor", "unmanaged", False)]
    cursor = next(h for h in report["harnesses"] if (h["harness"], h["scope"]) == ("cursor", "project"))
    assert cursor["config_found"] is True and cursor["readable"] is True


def test_user_configs_are_inventoried_but_not_treated_as_sync_targets(workspace):
    from brigade.config import Config, write_config
    from brigade.selection import Selection

    write_config(workspace, Config(version=1, selection=Selection(depth="repo", harnesses=["claude"])))
    catalog = {"version": 1, "servers": {"sample-local": {"transport": "stdio", "command": "run-sample"}}}
    (workspace / ".brigade" / "mcp.json").write_text(json.dumps(catalog))
    report = fleet_mcp_report.build_report(workspace, now=NOW)
    assert [r for r in report["servers"] if r["scope"] == "user"] == []
    assert {(h["harness"], h["scope"]) for h in report["harnesses"] if h["scope"] == "user"}  # markers still reported


def test_antigravity_scope_comes_from_its_adapter(workspace):
    from brigade import mcp_adapters

    adapter = mcp_adapters.ADAPTERS["antigravity"]
    assert adapter.user_scope is True
    report = fleet_mcp_report.build_report(workspace, now=NOW)
    assert ("antigravity", "user") in {(h["harness"], h["scope"]) for h in report["harnesses"]}
    assert ("antigravity", "project") not in {(h["harness"], h["scope"]) for h in report["harnesses"]}


def test_unreadable_config_is_never_reported_as_healthy():
    machine = inventory.Machine(
        node_id="node-a",
        label="sample-machine",
        reported=True,
        catalog_present=True,
        harnesses=(inventory.HarnessConfig("claude", "project", config_found=True, readable=False),),
    )
    assert machine.attention == 0 and machine.unreadable == 1
    pill = page_mod._machine_pill(machine)
    assert "unreadable config" in pill and "pill--ok" not in pill
    verdict = inventory.machine_verdict(machine)
    assert "Everything matches" not in verdict and "1 config could not be read" in verdict


def test_malformed_user_config_is_unreadable_even_though_it_is_inventory_only(workspace, tmp_path):
    (tmp_path / "home" / ".claude.json").write_text("{not json")
    report = fleet_mcp_report.build_report(workspace, now=NOW)
    claude_user = next(h for h in report["harnesses"] if (h["harness"], h["scope"]) == ("claude", "user"))
    assert claude_user == {"harness": "claude", "scope": "user", "config_found": True, "readable": False}


def test_machines_sharing_a_label_are_counted_separately():
    def row(node_id):
        return inventory.Row(
            node_id, "unnamed machine", "sample-tool", "claude", "project", "stdio", True, "missing", True, None
        )

    server = inventory.Server("sample-tool", (row("node-a"), row("node-b")), frozenset({"missing"}))
    assert server.machines == ("unnamed machine", "unnamed machine")
    assert server.issue == "missing on 2 machines"
    assert inventory.summary(server).startswith("2 machines")
    assert "Missing on unnamed machine, unnamed machine." in inventory.verdict(server)
