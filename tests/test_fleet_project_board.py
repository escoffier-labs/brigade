"""Projects board: node collector, Hub intake and validation, and the rendered page (fake data only)."""

from __future__ import annotations

import copy
import io
import json
import os
import re
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from email.message import Message

import pytest

from brigade import fleet_admin_pages as pages
from brigade import fleet_hub, fleet_hub_http
from brigade import fleet_project_board as store
from brigade import fleet_project_board_collect as collect
from brigade import fleet_project_board_page as page
from brigade.cli import main
from tests.fixtures import fleet_project_board as fixture
from tests.test_fleet_deck_overview import _mixed_view
from tests.test_fleet_work_page import _bearer, _cookie, _hub, _request

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
HOSTILE = "<img src=x onerror=alert(1)>\"'&"


def text_only(markup: str) -> str:
    return re.sub(r"<script.*?</script>|<style.*?</style>", "", markup, flags=re.S)


# ---------------------------------------------------------------- collector


def node(number, *, title="Sample issue", status="Ready", **content):
    base = {
        "__typename": "Issue",
        "number": number,
        "title": title,
        "state": "OPEN",
        "url": f"https://github.com/example-org/widgets/issues/{number}",
        "updatedAt": "2026-10-27T10:00:00Z",
        "repository": {"nameWithOwner": "example-org/widgets"},
        "labels": {"nodes": []},
        "assignees": {"nodes": []},
        "parent": None,
        "subIssuesSummary": {"total": 0, "completed": 0, "percentCompleted": 0},
        "closedByPullRequestsReferences": {"nodes": []},
    }
    base.update(content)
    return {"fieldValueByName": {"name": status} if status else None, "content": base}


def graphql(nodes, *, more=False, cursor="c1", cost=1, remaining=4990):
    return {
        "data": {
            "organization": {
                "projectV2": {
                    "id": "PVT_example0001",
                    "title": "Example Delivery",
                    "public": False,
                    "field": {"options": [{"name": n} for n in fixture.OPTIONS]},
                    "items": {"pageInfo": {"hasNextPage": more, "endCursor": cursor}, "nodes": nodes},
                }
            },
            "rateLimit": {"cost": cost, "remaining": remaining},
        }
    }


class Gh:
    def __init__(self, *payloads, code=0, stderr=""):
        self.payloads, self.calls, self.code, self.stderr = list(payloads), [], code, stderr

    def __call__(self, argv):
        self.calls.append(list(argv))
        payload = self.payloads[min(len(self.calls), len(self.payloads)) - 1] if self.payloads else {}
        return self.code, json.dumps(payload), self.stderr


def test_query_is_one_read_only_page_per_hundred_items():
    gh = Gh(graphql([node(1)]))
    snap = collect.collect("example-org", 7, runner=gh, now=NOW)
    assert len(gh.calls) == 1
    argv = gh.calls[0]
    assert argv[:3] == ["gh", "api", "graphql"]
    query = next(a for a in argv if a.startswith("query="))
    assert "first: 100" in query and "mutation" not in query.lower()
    assert "owner=example-org" in argv and "number=7" in argv and not any(a.startswith("after=") for a in argv)
    assert snap["complete"] is True and snap["rate_limit"] == {"cost": 1, "remaining": 4990}
    assert snap["url"] == "https://github.com/orgs/example-org/projects/7" and snap["project_id"] == "PVT_example0001"
    assert snap["fields"]["status_options"] == fixture.OPTIONS


def test_paging_follows_cursors_and_stops_at_the_cap_marking_the_snapshot_incomplete():
    pages_ = [graphql([node(p * 100 + i + 1) for i in range(100)], more=True, cursor=f"c{p}") for p in range(10)]
    gh = Gh(*pages_)
    snap = collect.collect("example-org", 7, runner=gh, now=NOW)
    assert len(gh.calls) == collect.MAX_PAGES == 5
    assert len(snap["items"]) == 500 and snap["complete"] is False and snap["rate_limit"]["cost"] == 5
    assert [a for a in gh.calls[1] if a.startswith("after=")] == ["after=c0"]
    two = Gh(graphql([node(1)], more=True, cursor="next"), graphql([node(2)]))
    snap = collect.collect("example-org", 7, runner=two, now=NOW)
    assert len(two.calls) == 2 and snap["complete"] is True and len(snap["items"]) == 2


def test_items_are_bounded_sanitised_and_links_come_from_validated_parts():
    long_title = "t" * 500
    labels = [{"name": f"l{i}", "color": "00AA11"} for i in range(12)]
    labels[1] = {"name": HOSTILE, "color": 'fff"onmouseover="x'}
    nodes = [
        node(1, title="Line\x00one\ntwo\x07 " + long_title, labels={"nodes": labels}),
        node(2, url="https://evil.example/example-org/widgets/issues/2"),
        node(3, repository={"nameWithOwner": 'bad"><repo'}),
        {"fieldValueByName": None, "content": {"__typename": "DraftIssue", "title": "draft"}},
        {"fieldValueByName": None, "content": None},
        node(4, status="Not a column"),
        node(
            5,
            assignees={"nodes": [{"login": "sample-dev"}, {"login": "bad login"}]},
            parent={"number": 1},
            subIssuesSummary={"total": 3, "completed": 9, "percentCompleted": 100},
            closedByPullRequestsReferences={
                "nodes": [
                    {"number": 50, "repository": {"nameWithOwner": "example-org/widgets"}},
                    {"number": 51, "repository": {"nameWithOwner": "other/elsewhere"}},
                ]
            },
        ),
        node(1),  # duplicate
    ]
    snap = collect.collect("example-org", 7, runner=Gh(graphql(nodes)), now=NOW)
    by = {i["number"]: i for i in snap["items"]}
    assert set(by) == {1, 4, 5} and snap["skipped"] == 5
    assert len(by[1]["title"]) == 200 and "\x00" not in by[1]["title"] and by[1]["title"].startswith("Line one two")
    assert len(by[1]["labels"]) == 8 and by[1]["labels"][1]["color"] is None
    assert by[1]["url"] == "https://github.com/example-org/widgets/issues/1"
    assert by[4]["status"] is None
    assert by[5]["assignees"] == ["sample-dev"] and by[5]["parent"] == 1
    assert (by[5]["sub_total"], by[5]["sub_done"]) == (3, 0)
    assert by[5]["linked_prs"] == [50]


def test_missing_scope_gh_and_auth_failures_are_clear():
    scope = {
        "errors": [{"type": "INSUFFICIENT_SCOPES", "message": "Your token has not been granted the required scopes"}]
    }
    with pytest.raises(collect.CollectError, match=r"read:project.*gh auth refresh -s read:project"):
        collect.collect("example-org", 7, runner=Gh(scope, code=1))
    with pytest.raises(collect.CollectError, match="read:project"):
        collect.collect("example-org", 7, runner=Gh({}, code=1, stderr="error: requires the read:project scope"))
    with pytest.raises(collect.CollectError, match="not found"):
        collect.collect("example-org", 7, runner=Gh({"data": {"organization": None}}))
    with pytest.raises(collect.CollectError, match="gh auth login"):
        collect.collect(
            "example-org", 7, runner=Gh({}, code=4, stderr="To get started with GitHub CLI, please run: gh auth login")
        )
    with pytest.raises(collect.CollectError, match="owner or project number"):
        collect.collect("bad owner", 7, runner=Gh())


def test_missing_gh_binary_is_reported(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("gh")

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(collect.CollectError, match="gh CLI is not installed"):
        collect.run_gh(["gh", "api", "graphql"])


def test_minimum_interval_guard(tmp_path):
    assert collect.too_soon(tmp_path, "example-org", 7, NOW) is None
    collect.record_success(tmp_path, "example-org", 7, NOW)
    wait = collect.too_soon(tmp_path, "example-org", 7, NOW + timedelta(minutes=2))
    assert wait == timedelta(minutes=3)
    assert collect.too_soon(tmp_path, "example-org", 7, NOW + timedelta(minutes=5)) is None
    assert collect.too_soon(tmp_path, "example-org", 8, NOW) is None
    if os.name == "posix":  # Windows has no group/other mode bits to check
        assert (tmp_path / "projects-board-state.json").stat().st_mode & 0o077 == 0


def test_cli_sync_guard_failure_keeps_state_and_dry_run_sends_nothing(tmp_path, monkeypatch, capsys):
    from brigade import fleet_client_policy

    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path))
    snap = fixture.snapshot(NOW)
    queried, sent = [], []
    monkeypatch.setattr(collect, "collect", lambda owner, number, **kw: queried.append((owner, number)) or snap)
    monkeypatch.setattr(fleet_client_policy, "_hub_and_token", lambda admin: ("http://127.0.0.1:1", "node-token"))

    def request(method, path, **kw):
        sent.append((method, path, kw["body"]["owner"]))
        if len(sent) == 1:
            raise fleet_client_policy.FleetPolicyClientError("network", "hub unreachable")
        return 200, {"ok": True}

    monkeypatch.setattr(fleet_client_policy, "_request", request)
    args = ["fleet", "projects", "sync", "--owner", "example-org", "--number", "7"]
    assert main(args + ["--dry-run", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["owner"] == "example-org" and not sent
    assert main(args) == 1 and "hub unreachable" in capsys.readouterr().err
    assert collect.last_success(tmp_path, "example-org", 7) is None
    assert main(args) == 0 and sent[-1][:2] == ("POST", "/projects/board")
    assert collect.last_success(tmp_path, "example-org", 7) is not None
    before = len(queried)
    assert main(args) == 0 and "under 5 minutes" in capsys.readouterr().err and len(queried) == before
    assert main(args + ["--force"]) == 0 and len(queried) == before + 1


# ---------------------------------------------------------------- hub validation and storage


def reject(mutate, match=None):
    value = fixture.snapshot(NOW)
    mutate(value)
    with pytest.raises(store.BoardError, match=match):
        store.validate_snapshot(value)


def test_fake_fixture_validates_and_round_trips():
    value = fixture.snapshot(NOW)
    assert store.validate_snapshot(copy.deepcopy(value)) == value


@pytest.mark.parametrize(
    "mutate",
    [
        lambda v: v.update(extra=1),
        lambda v: v.update(schema_version=True),
        lambda v: v.update(url="https://evil.example/orgs/example-org/projects/7"),
        lambda v: v.update(owner="bad owner"),
        lambda v: v["items"][0].update(url="https://github.com/example-org/other/issues/101"),
        lambda v: v["items"][0].update(repo="bad repo"),
        lambda v: v["items"][0].update(extra=1),
        lambda v: v["items"][0].update(state="MERGED"),
        lambda v: v["items"][0].update(status="Nope"),
        lambda v: v["items"][0].update(title="bad\x00title"),
        lambda v: v["items"][0].update(title="x" * 201),
        lambda v: v["items"][0].update(sub_done=9),
        lambda v: v["items"][0].update(labels=[{"name": "x", "color": "FFFFFF"}]),
        lambda v: v["items"][0].update(labels=[{"name": "x", "color": 'a"b'}]),
        lambda v: v["items"][0].update(labels=[{"name": "x", "color": None}] * 9),
        lambda v: v["items"][0].update(assignees=["bad login"]),
        lambda v: v["items"][0].update(linked_prs=[1] * 6),
        lambda v: v["items"][0].update(parent=True),
        lambda v: v["items"].append(copy.deepcopy(v["items"][0])),
        lambda v: v["items"].extend(copy.deepcopy(v["items"][0]) for _ in range(501)),
        lambda v: v["fields"].update(status_options=["a", "a"]),
        lambda v: v.update(observed_at="yesterday"),
        lambda v: v.update(project_id="bad id!"),
        lambda v: v.update(rate_limit={"cost": 1}),
    ],
)
def test_validation_rejects_malformed_or_out_of_bound_snapshots(mutate):
    reject(mutate)


def test_store_keeps_newest_and_never_drops_the_last_good_snapshot(tmp_path):
    conn = fleet_hub.init_db(tmp_path / "hub.db")
    try:
        assert store.load_board(conn) is None
        good = fixture.snapshot(NOW)
        assert store.store_snapshot(conn, good, now=NOW)["items"] == 11
        bad = fixture.snapshot(NOW + timedelta(minutes=10))
        bad["items"][0]["title"] = "bad\x00"
        with pytest.raises(store.BoardError):
            store.store_snapshot(conn, bad)
        assert store.load_board(conn)["observed_at"] == good["observed_at"]
        older = fixture.snapshot(NOW - timedelta(hours=1))
        assert store.store_snapshot(conn, older)["ignored"] is True
        assert store.load_board(conn)["observed_at"] == good["observed_at"]
        newer = fixture.snapshot(NOW + timedelta(minutes=10), complete=False)
        assert store.store_snapshot(conn, newer)["ignored"] is False
        assert store.load_board(conn)["complete"] is False
    finally:
        conn.close()


def test_load_is_read_only_when_the_table_is_absent_and_ignores_a_corrupt_row(tmp_path):
    conn = sqlite3.connect(tmp_path / "x.db")
    try:
        before = list(conn.iterdump())
        assert store.load_board(conn) is None
        assert list(conn.iterdump()) == before
        store.ensure_schema(conn)
        conn.execute("INSERT INTO fleet_project_boards VALUES ('o', 1, '2026-10-27T00:00:00+00:00', '{bad', 'x')")
        assert store.load_board(conn) is None
    finally:
        conn.close()


# ---------------------------------------------------------------- hub endpoint


def handler(tmp_path, *, bearer=None, body=None, query=""):
    db = tmp_path / "hub.db"
    fleet_hub.init_db(db).close()
    cls = fleet_hub_http.make_handler("example-admin", db, allow_admin_writes=True)
    instance = cls.__new__(cls)
    instance.path = "/projects/board" + query
    instance.headers = Message()
    if bearer:
        instance.headers["Authorization"] = "Bearer " + bearer
    raw = json.dumps(body).encode() if body is not None else b""
    instance.headers["Content-Length"] = str(len(raw))
    instance.rfile = io.BytesIO(raw)
    result = []
    instance._send_json = lambda status, payload, **kw: result.append((status, payload))
    instance._send_html = lambda status, payload, **kw: result.append((status, payload))
    return instance, result, db


def test_intake_requires_a_node_token_and_validates(tmp_path):
    h, result, db = handler(tmp_path, body=fixture.snapshot(NOW))
    h.do_POST()
    assert result[-1][0] == 401
    h, result, _ = handler(tmp_path, bearer="example-admin", body=fixture.snapshot(NOW))
    h.do_POST()
    assert result[-1][0] == 403
    conn = fleet_hub.open_db(db)
    _, token = fleet_hub.add_node(conn, "example-node", "Example")
    conn.close()
    h, result, _ = handler(tmp_path, bearer=token, body=fixture.snapshot(NOW), query="?x=1")
    h.do_POST()
    assert result[-1][0] == 400
    hostile = fixture.snapshot(NOW)
    hostile["items"][0]["url"] = "https://evil.example/x"
    h, result, _ = handler(tmp_path, bearer=token, body=hostile)
    h.do_POST()
    assert result[-1][0] == 400 and "evil" not in json.dumps(result[-1][1])
    h, result, _ = handler(tmp_path, bearer=token, body=fixture.snapshot(NOW))
    h.do_POST()
    assert result[-1] == (
        200,
        {"ok": True, "ignored": False, "observed_at": fixture.snapshot(NOW)["observed_at"], "items": 11},
    )
    conn = fleet_hub.open_db(db)
    try:
        assert store.load_board(conn)["title"] == "Example Delivery"
    finally:
        conn.close()


def test_route_serves_the_board_to_authenticated_identities_only_with_hostile_text_escaped(tmp_path):
    hostile = fixture.snapshot(NOW)
    hostile["title"] = HOSTILE
    hostile["items"][2]["title"] = HOSTILE
    hostile["items"][2]["labels"] = [{"name": HOSTILE, "color": "1d76db"}]
    with _hub(tmp_path) as (hub, db_path):
        conn = fleet_hub.open_db(db_path)
        try:
            store.store_snapshot(conn, hostile)
        finally:
            conn.close()
        assert _request(hub, "/deck/projects")[0] == 401
        assert _request(hub, "/deck/projects?view=list")[0] == 401
        status, _, body = _request(hub, "/deck/projects", headers=_cookie(hub))
        assert status == 200 and "data-bd-root" in body
        assert "<img" not in body and "&lt;img" in body
        status, _, filtered = _request(hub, "/deck/projects?epic=101&repo=example-org%2Fwidgets", headers=_bearer())
        assert status == 200 and "Showing epic #101" in filtered
        for query in (
            "view=bogus",
            "epic=abc",
            "epic=1&epic=2",
            "epic=-1",
            "view=list&other=1",
            "epic=1234567890",
            "epic=101",
            "repo=example-org%2Fwidgets",
            "epic=101&repo=not%20a%20repo",
        ):
            assert _request(hub, "/deck/projects?" + query, headers=_bearer())[0] == 400, query


# ---------------------------------------------------------------- page


def render(board=None, options=None, ci_view=None):
    return page.render(board or fixture.snapshot(NOW), options or page.Options(), NOW, ci_view=ci_view)


def test_epics_are_items_with_sub_issues_sorted_active_first_with_progress_text():
    board = fixture.snapshot(NOW)
    assert [e["number"] for e in page.epics_of(board["items"])] == [101, 102]
    board["items"][0]["status"] = "Done"
    assert [e["number"] for e in page.epics_of(board["items"])] == [102, 101]
    board = fixture.snapshot(NOW)
    board["items"][0]["sub_done"] = board["items"][0]["sub_total"]
    assert [e["number"] for e in page.epics_of(board["items"])] == [102, 101]
    section, _, _ = render()
    assert "2 of 4 done" in section and "1 of 2 done" in section
    assert '<progress class="bd-bar" value="2" max="4"' in section
    assert 'class="bd-epic-title" title="Sample alpha rollout">Sample alpha rollout</span>' in section
    assert 'title="epic: Sample' not in section
    assert page.epic_title("EPIC:  Thing") == "Thing" and page.epic_title("epic:") == "epic:"


def test_board_columns_follow_snapshot_order_with_done_last_and_blocked_marked():
    section, _, script = render()
    heads = re.findall(r'<h3 class="bd-col-head">(.*?)<span class="bd-count"', section)
    names = [re.sub(r"<[^>]+>", "", h) for h in heads]
    assert names == ["No status", "Backlog", "Ready", "In Progress", "Review", "Merge queue", "Blocked", "Done"]
    assert '<span class="pill pill--warn">Blocked</span>' in section
    # Done is collapsed by script only: every card is still in the no-JS HTML.
    assert section.count('class="bd-card"') == 11 and "data-collapsible" in section
    assert "is-collapsed" in script and "bd-col[data-collapsible]" in script
    custom = fixture.snapshot(NOW)
    custom["fields"]["status_options"] = ["Done", "Zeta", "Alpha"]
    for item in custom["items"]:
        item["status"] = "Alpha" if item["status"] else None
    section, _, _ = render(custom)
    names = [
        re.sub(r"<[^>]+>", "", h) for h in re.findall(r'<h3 class="bd-col-head">(.*?)<span class="bd-count"', section)
    ]
    assert names == ["No status", "Zeta", "Alpha", "Done"]


def test_card_chips_epic_chip_and_linked_pull_requests():
    section, css, _ = render()
    assert (
        'href="https://github.com/example-org/widgets/issues/103" target="_blank" rel="noopener noreferrer"' in section
    )
    assert 'class="pr-label lc-1d76db"' in section and ".pr-label.lc-1d76db{--lc:#1d76db}" in css
    assert 'class="pr-label">no-color' in section
    assert (
        '<a class="bd-epic-chip" href="/deck/projects?epic=101&amp;repo=example-org%2Fwidgets">epic #101</a>' in section
    )
    pr = re.search(r'<a class="bd-pr" href="([^"]+)"[^>]*>.*?<span class="bd-pr-count">(\d+)</span>', section)
    assert pr.group(1) == "https://github.com/example-org/widgets/pull/301" and pr.group(2) == "2"
    assert "pr-check" not in section  # no pulls snapshot, no invented check state
    ci_view = {
        "repositories": [
            {
                "repo": "example-org/widgets",
                "head": {"observed_at": "2026-10-27T11:00:00Z"},
                "pulls_complete": True,
                "pulls": [{"number": 301, "head_sha": "a" * 40, "rollup": {"state": "passing"}}],
            }
        ]
    }
    with_checks, _, _ = render(ci_view=ci_view)
    assert 'aria-label="Checks passing"' in with_checks and with_checks.count("pr-check--") >= 1


def test_epic_filter_shows_epic_and_children_with_a_clear_chip():
    section, _, _ = render(options=page.Options(epic=101, epic_repo=fixture.REPO))
    numbers = re.findall(r'class="bd-card" data-bd-item data-number="(\d+)"', section)
    assert sorted(numbers, key=int) == ["101", "103", "104", "105", "106"]
    assert "Showing epic #101 · " in section and '<a href="/deck/projects">Clear</a>' in section
    unknown, _, _ = render(options=page.Options(epic=999, epic_repo=fixture.REPO))
    assert "Epic not found #999" in unknown and 'class="bd-card"' not in unknown


def test_epic_filter_keeps_repositories_apart():
    # Issue numbers repeat across repositories: other-org/gadgets#101 is not widgets#101.
    board = fixture.snapshot(NOW)
    other = "other-org/gadgets"
    board["items"] += [
        {**board["items"][0], "repo": other, "url": f"https://github.com/{other}/issues/101"},
        {
            **board["items"][2],
            "number": 201,
            "repo": other,
            "url": f"https://github.com/{other}/issues/201",
            "parent_repo": other,
        },
    ]
    board = store.validate_snapshot(board)
    widgets, _, _ = render(board, page.Options(epic=101, epic_repo=fixture.REPO))
    assert "201" not in re.findall(r'data-number="(\d+)"', widgets)
    gadgets, _, _ = render(board, page.Options(epic=101, epic_repo=other))
    assert sorted(re.findall(r'class="bd-card" data-bd-item data-number="(\d+)"', gadgets), key=int) == ["101", "201"]
    assert gadgets.count("epic=101&amp;repo=other-org%2Fgadgets") >= 1


def test_views_epics_only_and_list_grouped_by_status():
    section, _, _ = render(options=page.Options(view="epics"))
    assert "bd-epics--page" in section and 'class="bd-card"' not in section and "bd-columns" not in section
    assert 'href="/deck/projects?view=epics" aria-current="page"' in section
    listing, _, _ = render(options=page.Options(view="list"))
    groups = re.findall(r'<section class="pr-group"[^>]*><h3><span>([^<]+)</span>', listing)
    assert groups[0] == "No status" and groups[-1] == "Done" and "Blocked" in groups
    assert listing.count('class="pr-row"') == 11 and "bd-columns" not in listing
    none = fixture.snapshot(NOW)
    none["items"] = [i for i in none["items"] if i["sub_total"] == 0]
    assert "No epics on this board." in render(none, page.Options(view="epics"))[0]
    assert "No issues on this board." in render({**none, "items": []})[0]


def test_header_links_updated_age_search_and_partial_note():
    section, _, script = render()
    assert (
        '<a href="https://github.com/orgs/example-org/projects/7" target="_blank" rel="noopener noreferrer">Example Delivery</a>'
        in section
    )
    assert "Updated 6m ago" in section
    assert 'placeholder="Search issues, or label:fleet"' in section and "data-bd-search hidden" in section
    assert "label" in script and "Board incomplete" not in section
    partial = fixture.snapshot(NOW, complete=False)
    assert "Board incomplete" in render(partial)[0]
    for name in ("board", "epics", "list"):
        assert f">{name.title()}</a>" in section


def test_agent_data_is_tagged_and_carries_the_sync_command_and_rate_limit():
    section, _, _ = render()
    agent = re.search(r'<p class="bd-agent" data-audience="agent">(.*?)</p>', section, re.S).group(1)
    for expected in (
        "board example-org/7",
        "PVT_example0001",
        "observed 2026-10-27T11:54:00+00:00",
        "rate limit cost 1, remaining 4999",
        "snapshot complete",
        "brigade fleet projects sync --owner escoffier-labs --number 1",
    ):
        assert expected in agent
    assert "snapshot incomplete" in render(fixture.snapshot(NOW, complete=False))[0]


def test_hostile_values_are_escaped_in_every_view():
    board = fixture.snapshot(NOW)
    board["title"] = HOSTILE
    board["items"][0]["title"] = "epic: " + HOSTILE
    board["items"][2]["title"] = HOSTILE
    board["items"][2]["labels"] = [{"name": HOSTILE, "color": "1d76db"}]
    for view in page.VIEWS:
        section, css, _ = render(board, page.Options(view=view))
        assert "<img" not in section
        assert "onmouseover" not in section + css
    assert "&lt;img" in render(board)[0]


def test_projects_page_composition_and_empty_states():
    view = _mixed_view()
    empty = {"state": "empty", "campaigns": [], "selected": None}
    no_board = pages.render_projects(empty, view, NOW, nonce="n", worklore_enabled=True)
    assert "No projects yet. GitHub Projects boards will appear here once connected." in no_board
    assert (
        "brigade fleet projects sync --owner escoffier-labs --number 1" in no_board and "data-bd-root" not in no_board
    )
    with_board = pages.render_projects(
        empty, view, NOW, nonce="n", worklore_enabled=True, board=fixture.snapshot(NOW), board_options=page.Options()
    )
    assert "data-bd-root" in with_board and "No projects yet" not in with_board
    for state in ("disabled", "unavailable"):
        shown = pages.render_projects(
            {"state": state, "campaigns": [], "selected": None},
            view,
            NOW,
            nonce="n",
            worklore_enabled=True,
            board=fixture.snapshot(NOW),
        )
        assert "data-bd-root" in shown
    assert "—" not in text_only(with_board)
    assert "<details" not in section_of(with_board)


def section_of(markup):
    start = markup.index("data-bd-root")
    return markup[start : markup.index("</section>", start)]


def test_query_parsing_keeps_the_goals_selection_and_adds_view_and_epic():
    assert pages.parse_projects_query("") == (None, page.Options())
    assert pages.parse_projects_query("view=list&epic=12&repo=example-org/widgets") == (
        None,
        page.Options("list", 12, "example-org/widgets"),
    )
    assert pages.parse_projects_query("project=goal-a")[0] == "goal-a"
    for bad in (
        "project=",
        "view=x",
        "epic=0",
        "epic=a",
        "x=1",
        "view=list&view=board",
        "project=a&view=board&epic=1&x=1",
        "epic=12",
        "repo=example-org/widgets",
        "epic=12&repo=not a repo",
    ):
        with pytest.raises(ValueError):
            pages.parse_projects_query(bad)
