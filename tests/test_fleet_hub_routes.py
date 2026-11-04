"""The Fleet Hub route-registration seam (v2 slice S22a)."""

from pathlib import Path

import pytest

from brigade import fleet_hub, fleet_hub_http
from brigade.fleet_hub_routes import RouteTable


def _handler(name):
    def handler(request, path, query):
        request.append((name, path, query))

    return handler


def test_exact_path_beats_prefix_and_longest_prefix_wins():
    routes = RouteTable()
    routes.add("GET", _handler("deck"), paths=("/deck",), prefixes=("/deck/",))
    routes.add("GET", _handler("actions"), prefixes=("/deck/actions/",))
    routes.add("GET", _handler("ci"), paths=("/deck/ci",))
    seen = []
    for path in ("/deck", "/deck/ci", "/deck/actions/a", "/deck/other", "/deck/actions"):
        assert routes.dispatch(seen, "GET", path, "q=1")
    assert [name for name, _path, _query in seen] == ["deck", "ci", "actions", "deck", "deck"]
    assert seen[0] == ("deck", "/deck", "q=1")


def test_methods_are_separate_and_a_miss_dispatches_nothing():
    routes = RouteTable()
    routes.add(("POST", "PATCH"), _handler("work"), prefixes=("/work/",))
    seen = []
    assert not routes.dispatch(seen, "GET", "/work/a", "")
    assert not routes.dispatch(seen, "POST", "/work", "")
    assert routes.dispatch(seen, "PATCH", "/work/a", "")
    assert seen == [("work", "/work/a", "")]


@pytest.mark.parametrize(
    "first, second",
    [
        ({"paths": ("/a",)}, {"paths": ("/a",)}),
        ({"prefixes": ("/a/",)}, {"prefixes": ("/a/",)}),
        ({}, {"prefixes": ("",)}),
    ],
    ids=["exact", "prefix", "empty-prefix"],
)
def test_a_second_claim_on_one_route_is_refused(first, second):
    routes = RouteTable()
    routes.add("GET", _handler("one"), **first)
    with pytest.raises(ValueError):
        routes.add("GET", _handler("two"), **second)
    routes.add("POST", _handler("other-method"), **first)


def test_hub_route_inventory_is_unchanged():
    table = fleet_hub_http.make_handler("fixture-token", Path("unused.db")).route_table
    get = [
        "/",
        "/audit",
        "/ci",
        "/ci/work",
        "/claims",
        "/cloud",
        "/dashboard/sessions",
        "/deck",
        "/deck/*",
        "/deck/actions/*",
        "/deck/ci",
        "/deck/mcp",
        "/deck/observed/*",
        "/deck/owners",
        "/deck/policy",
        "/deck/pr-rules",
        "/deck/repos",
        "/deck/roster",
        "/deck/skills",
        "/deck/skills/plan",
        "/deck/stats",
        "/deck/work",
        "/enroll",
        "/health",
        "/mesh/identity",
        "/models",
        "/nodes",
        "/owners",
        "/policy",
        "/policy/inventory",
        "/policy/status",
        "/preference",
        "/sessions",
        "/status",
        "/view/*",
        "/work",
        "/work/*",
        *fleet_hub._ASSET_ROUTES,
    ]
    post = [
        "/actions",
        "/ci",
        "/ci/refresh",
        "/claims",
        "/cloud",
        "/dashboard/enrollment",
        "/dashboard/sessions",
        "/deck/actions/*",
        "/deck/policy",
        "/deck/pr-rules",
        "/deck/roster",
        "/deck/skills",
        "/enroll",
        "/events",
        "/findings",
        "/grokbot",
        "/mcp/report",
        "/mesh/identity",
        "/mesh/statements",
        "/message-principals",
        "/messages",
        "/models",
        "/nodes",
        "/policy",
        "/projects/board",
        "/sessions",
        "/skills/report",
        "/stats",
        "/work/*",
    ]
    expected = sorted(
        [("GET", path) for path in get]
        + [("POST", path) for path in post]
        + [("PUT", "/preference"), ("PATCH", "/work/*"), ("DELETE", "/work/*")]
    )
    assert table.registered() == expected


def test_hub_specific_routes_win_over_the_deck_catch_all():
    table = fleet_hub_http.make_handler("fixture-token", Path("unused.db")).route_table
    deck = table.find("GET", "/deck/unknown")
    assert deck is table.find("GET", "/deck/repos") is table.find("GET", "/")
    for path in ("/deck/actions/a", "/deck/observed/a", "/deck/ci", "/deck/stats", "/deck/roster"):
        assert table.find("GET", path) is not deck
    assert table.find("GET", "/unknown") is None
    assert table.find("HEAD", "/health") is None
