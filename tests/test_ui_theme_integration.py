"""Shared fleet shell/navigation contracts at the actual renderer boundary."""

from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from brigade import fleet_command_deck as deck
from brigade import fleet_dashboard as board
from brigade import fleet_hub, fleet_hub_roster_page as roster
from brigade import fleet_policy_page as policy
from brigade import fleet_repo_policy_page as repos
from brigade import ui_theme

NOW = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)
BOARD_NONCE = "integration-nonce"
BOARD_HOSTILE = '"><img src=x onerror="alert(1)">&'


def render_populated_board_pages():
    """Pure synthetic renderer fixtures for pytest and an ephemeral browser preview."""
    runs = []
    claims = []
    nodes = []
    started_at = {}
    for label in ["alpha", "bravo"]:
        node = f"synthetic-node-{label}-" + "n" * 96 + BOARD_HOSTILE
        repo = f"synthetic-repo-{label}-" + "r" * 128 + BOARD_HOSTILE
        seat = f"synthetic-seat-{label}-" + "s" * 96 + BOARD_HOSTILE
        for kind, state in [("active", "approval.held"), ("completed", "run.completed")]:
            run_id = f"synthetic-{kind}-{label}"
            runs.append(
                {
                    "node_id": node,
                    "run_id": run_id,
                    "repo": repo,
                    "seat": seat,
                    "harness": "codex",
                    "state": state,
                    "ts": NOW.isoformat(),
                    "exit_status": 0 if kind == "completed" else None,
                }
            )
            started_at[node, run_id] = "2026-10-03T11:55:00+00:00"
        claims.append(
            {
                "target": repo,
                "owner_node": node,
                "owner_conductor": f"synthetic-conductor-{label}",
                "expires_at": "2026-10-03T13:00:00+00:00",
            }
        )
        nodes.append({"node_id": node, "last_received_at": NOW.isoformat(), "events": 4})
    return {
        "/view/" + view: board.render_page(
            view=view,
            query_string="all=1",
            runs=runs,
            claims=claims,
            nodes=nodes,
            started_at=started_at,
            nonce=BOARD_NONCE,
            now=NOW,
        )
        for view in ["machines", "repos"]
    }


class Markup(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.tags = []
        self.ancestors = []
        self.stack = []
        self.text = []
        self.nav_links = []
        self.in_nav = False
        self.section_depth = 0
        self.top_sections = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        self.ancestors.append(tuple(self.stack))
        self.text.append("")
        self.tags.append((tag, dict(attrs)))
        if tag not in {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }:
            self.stack.append(len(self.tags) - 1)
        if tag == "section":
            if self.section_depth == 0:
                self.top_sections.append(dict(attrs))
            self.section_depth += 1
        if tag == "nav":
            self.in_nav = True
        if tag == "a" and self.in_nav:
            self.nav_links.append(dict(attrs))

    def handle_endtag(self, tag):
        if self.stack and self.tags[self.stack[-1]][0] == tag:
            self.stack.pop()
        if tag == "section":
            self.section_depth -= 1
        if tag == "nav":
            self.in_nav = False

    def elements(self, tag):
        return [attrs for name, attrs in self.tags if name == tag]

    def handle_data(self, data):
        for index in self.stack:
            self.text[index] += data

    def descendants(self, index, tag):
        return [child for child, (name, _) in enumerate(self.tags) if name == tag and index in self.ancestors[child]]


def test_shell_escapes_hostile_metadata_without_injecting_elements():
    hostile = '"><img src=x onerror="alert(1)">&'
    rendered = ui_theme.document(
        hostile, hostile, '<main class="deck-shell">safe</main>', as_of=hostile, script="tick();"
    )
    markup = Markup(rendered)
    assert markup.elements("img") == []
    assert rendered.count("&lt;img") == 4  # title, style nonce, script nonce, as-of
    assert len(markup.elements("html")) == len(markup.elements("body")) == len(markup.elements("main")) == 1
    assert markup.elements("body")[0]["data-as-of"] == hostile
    for tag in ["style", "script"]:
        assert len(markup.elements(tag)) == 1
        assert markup.elements(tag)[0] == {"nonce": hostile}
    links = {attrs["href"] for attrs in markup.elements("link")}
    assert {"/favicon.ico", "/favicon-32x32.png", "/apple-touch-icon.png", "/site.webmanifest"} <= links


@pytest.fixture()
def fleet_pages(tmp_path):
    view = deck.DeckView(stations=(), rail=(), repos=(), outcomes=(), observers=())
    conn = fleet_hub.init_db(tmp_path / "fleet.db")
    try:
        policy_view = policy.load_view(conn)
        roster_view = roster.load_view(conn, deck.DeckConfig())
        return {
            "/deck": deck.render_deck(view, nonce="integration-nonce", now=NOW),
            "/deck/repos": repos.render(view, nonce="integration-nonce", now=NOW),
            "/deck/roster": roster.render(
                roster_view, nonce="integration-nonce", now=NOW, csrf="fixture", editable=True
            ),
            "/deck/policy": policy.render(
                policy_view, nonce="integration-nonce", now=NOW, csrf="fixture", editable=True
            ),
            **render_populated_board_pages(),
        }
    finally:
        conn.close()


def test_all_fleet_renderers_use_one_shared_shell_and_navigation(fleet_pages):
    for route, rendered in fleet_pages.items():
        markup = Markup(rendered)
        assert len(markup.elements("html")) == len(markup.elements("body")) == len(markup.elements("main")) == 1, route
        assert rendered.count("--canvas:") == 1, route
        for tag in ["style", "script"]:
            assert len(markup.elements(tag)) == 1, route
            assert markup.elements(tag)[0]["nonce"] == "integration-nonce", route
        assert markup.elements("nav") == [{"class": "deck-nav", "aria-label": "Command Deck"}], route
        routes = {urlsplit(attrs["href"]).path for attrs in markup.elements("a") if "href" in attrs}
        assert {"/deck/repos", "/deck/roster", "/deck/policy", "/view/machines"} <= routes, route
        assert "/" in routes or "/deck" in routes, route


@pytest.mark.parametrize("view, table_count", [("machines", 2), ("repos", 1)])
def test_populated_board_tables_keep_rows_semantics_and_individual_scroll_wrappers(fleet_pages, view, table_count):
    markup = Markup(fleet_pages["/view/" + view])
    tables = [index for index, (tag, _) in enumerate(markup.tags) if tag == "table"]
    assert len(tables) == table_count
    wrappers = []
    headers = (
        ["Run", "Repo", "Seat/harness", "State", "Elapsed", "Last event"]
        if view == "machines"
        else ["Repo", "Running where", "Claim", "Last outcome", "Flags"]
    )
    for table in tables:
        assert "data-table" in markup.tags[table][1]["class"].split()
        wrapper = markup.ancestors[table][-1]
        tag, attrs = markup.tags[wrapper]
        assert tag == "div" and "table-wrap" in attrs.get("class", "").split()
        assert markup.descendants(wrapper, "table") == [table]
        wrappers.append(wrapper)
        (thead,) = markup.descendants(table, "thead")
        (tbody,) = markup.descendants(table, "tbody")
        assert markup.ancestors[thead][-1] == markup.ancestors[tbody][-1] == table
        assert [markup.text[index] for index in markup.descendants(thead, "th")] == headers
        rows = markup.descendants(tbody, "tr")
        assert len(rows) == 2
        for row in rows:
            assert markup.ancestors[row][-1] == tbody
            cells = markup.descendants(row, "td")
            assert len(cells) == len(headers)
            assert all(markup.ancestors[cell][-1] == row for cell in cells)
    assert len(set(wrappers)) == table_count
    for label in ["alpha", "bravo"]:
        assert f"synthetic-repo-{label}-" + "r" * 128 + BOARD_HOSTILE in "".join(markup.text[table] for table in tables)
        assert f"synthetic-seat-{label}-" + "s" * 96 + BOARD_HOSTILE in "".join(markup.text[table] for table in tables)
        assert f"synthetic-active-{label}" in "".join(markup.text[table] for table in tables)
        assert f"synthetic-conductor-{label}" in markup.text[0]
    if view == "machines":
        assert sorted(
            attrs["title"] for attrs in markup.elements("td") if "fleet-run-id" in attrs.get("class", "")
        ) == [
            "synthetic-active-alpha",
            "synthetic-active-bravo",
            "synthetic-completed-alpha",
            "synthetic-completed-bravo",
        ]
        assert all(attrs["title"].endswith(BOARD_HOSTILE) for attrs in markup.elements("h2"))
    else:
        assert (
            len(
                [
                    attrs
                    for attrs in markup.elements("span")
                    if "fleet-state-succeeded" in attrs.get("class", "").split()
                ]
            )
            == 2
        )
        assert all(
            attrs["title"].endswith(BOARD_HOSTILE)
            for attrs in markup.elements("span")
            if "fleet-node" in attrs.get("class", "").split()
        )
    assert markup.elements("img") == []
    assert not [attrs for _, attrs in markup.tags if "onerror" in attrs]
    for name in ["machines", "repos"]:
        link = next(attrs for attrs in markup.nav_links if urlsplit(attrs["href"]).path == "/view/" + name)
        assert parse_qs(urlsplit(link["href"]).query) == {"all": ["1"]}
        assert ("aria-current" in link) == (name == view)


@pytest.mark.parametrize("view", ["machines", "repos"])
def test_board_navigation_and_filters_preserve_query_state(view):
    query = {
        "sort": ["seat"],
        "repo": ["example & tool"],
        "node": ["fixture-node"],
        "seat": ["example-seat"],
        "state": ["running"],
        "attention": ["1"],
        "all": ["1"],
    }
    rendered = board.render_page(
        view=view,
        query_string=urlencode({key: values[0] for key, values in query.items()}),
        runs=[],
        claims=[],
        nodes=[],
        started_at={},
        nonce="n",
        now=NOW,
    )
    markup = Markup(rendered)
    for name in ["machines", "repos"]:
        link = next(attrs for attrs in markup.nav_links if urlsplit(attrs["href"]).path == "/view/" + name)
        assert parse_qs(urlsplit(link["href"]).query) == query
        assert ("aria-current" in link) == (name == view)
    fields = {attrs["name"]: attrs for attrs in markup.elements("input") if "name" in attrs}
    for name in ["repo", "node", "seat", "state"]:
        assert fields[name]["value"] == query[name][0]
    assert "checked" in fields["attention"] and "checked" in fields["all"]
    assert markup.elements("form")[0]["action"] == ("/" if view == "machines" else "/view/repos")


def test_badge_palette_uses_nonce_stylesheet_without_inline_styles():
    import re

    from brigade.fleet_deck_brands import harness_brand, provider_brand

    hostile = '"><img src=x onerror="alert(1)">'
    names = ["claude", "codex", "cursor", "opencode", "google", "t3-fleet", "grok", "grok-bot", "anthropic", hostile]
    tiles = tuple(
        deck.Tile(
            deck.LiveRun(
                "fixture-node",
                str(index),
                "example-tool",
                "example-seat",
                name,
                "run.started",
                "running",
                1,
                1,
                provider=name,
            ),
            None,
            False,
        )
        for index, name in enumerate(names)
    )
    station = deck.StationView(
        deck.StationConfig("fixture-node", "Example", 20), "Example", True, len(tiles), NOW.isoformat(), tiles
    )
    view = deck.DeckView(stations=(station,), rail=(), repos=(), outcomes=(), observers=())
    rendered = deck.render_deck(view, nonce="badge-nonce", now=NOW)
    markup = Markup(rendered)
    assert not [(tag, attrs) for tag, attrs in markup.tags if "style" in attrs]
    assert markup.elements("img") == []
    assert markup.elements("style") == [{"nonce": "badge-nonce"}]
    stylesheet = rendered.split('<style nonce="badge-nonce">', 1)[1].split("</style>", 1)[0]
    badges = [attrs for attrs in markup.elements("span") if "badge" in attrs.get("class", "").split()]
    assert len(badges) == 2 * len(names)
    for attrs, brand in zip(
        badges, [brand for name in names for brand in (harness_brand(name), provider_brand(name))], strict=True
    ):
        assert any(
            re.search(
                r"\." + re.escape(cls) + r"\s*\{[^}]*background:\s*" + re.escape(brand.accent) + r"\s*;", stylesheet
            )
            for cls in attrs["class"].split()
        )
    assert [attrs["title"] for attrs in badges[-2:]] == [hostile, hostile]


def test_populated_deck_separates_station_grid_from_following_panel():
    station = deck.StationView(
        deck.StationConfig("fixture-node", "Example", 2), "Example", True, 0, NOW.isoformat(), ()
    )
    view = deck.DeckView(stations=(station,), rail=(), repos=(), outcomes=(), observers=())
    rendered = deck.render_deck(view, nonce="spacing-nonce", now=NOW)
    markup = Markup(rendered)
    assert [attrs["class"] for attrs in markup.top_sections[:2]] == ["stations", "panel"]
    assert markup.top_sections[1]["aria-labelledby"] == "control-plane"
    stylesheet = rendered.split('<style nonce="spacing-nonce">', 1)[1].split("</style>", 1)[0]
    assert ".stations + .panel { margin-top: 16px; }" in stylesheet
