"""The Fleet Hub route-registration seam (v2 slice S22a)."""

import json
from http.server import BaseHTTPRequestHandler
from io import BytesIO
from pathlib import Path

import pytest

from brigade import fleet_dashboard, fleet_deck_pages_http, fleet_hub, fleet_hub_http
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
        "/models/usage",
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


@pytest.mark.parametrize(
    ("target", "parsed"),
    [
        ("//health", "/health"),
        ("//policy?mode=fixture%2Bvalue", "/policy?mode=fixture%2Bvalue"),
        ("///work//items", "/work//items"),
        ("//", "/"),
        ("//unknown", "/unknown"),
        ("//deck/actions/aa_x", "/deck/actions/aa_x"),
    ],
)
def test_stdlib_normalizes_only_leading_request_slashes(target, parsed):
    handler = BaseHTTPRequestHandler.__new__(BaseHTTPRequestHandler)
    handler.raw_requestline = f"GET {target} HTTP/1.1\r\n".encode("ascii")
    handler.rfile = BytesIO(b"Host: example.test\r\n\r\n")
    assert handler.parse_request()
    assert handler.path == parsed


def test_request_log_fixture_covers_registered_python_routes():
    fixture = Path(__file__).resolve().parents[1] / "engines" / "hub" / "tests" / "request-log-routes.json"
    inventory = json.loads(fixture.read_text(encoding="utf-8"))
    table = fleet_hub_http.make_handler("fixture-token", Path("unused.db")).route_table
    exact = {path for _, path in table.registered() if not path.endswith("*")}
    prefixes = inventory["python_prefixes"]
    assert set(inventory["python_exact"]) == exact
    assert set(prefixes) == {path for _, path in table.registered() if path.endswith("*")}

    for method, pattern in table.registered():
        if not pattern.endswith("*"):
            continue
        prefix = pattern.removesuffix("*")
        handler = table.find(method, prefix)
        assert handler is not None, (method, pattern)
        assert prefixes[pattern], pattern
        for sample in prefixes[pattern]:
            assert sample.startswith(prefix) and sample != prefix, sample
            assert table.find(method, sample) is handler, (method, pattern, sample)

    assert set(prefixes["/deck/*"]) == fleet_deck_pages_http.DECK_PATHS - exact
    assert set(prefixes["/view/*"]) == {f"/view/{view}" for view in fleet_dashboard.VIEWS}


def test_hub_specific_routes_win_over_the_deck_catch_all():
    table = fleet_hub_http.make_handler("fixture-token", Path("unused.db")).route_table
    deck = table.find("GET", "/deck/unknown")
    assert deck is table.find("GET", "/deck/repos") is table.find("GET", "/")
    for path in ("/deck/actions/a", "/deck/observed/a", "/deck/ci", "/deck/stats", "/deck/roster"):
        assert table.find("GET", path) is not deck
    assert table.find("GET", "/unknown") is None
    assert table.find("HEAD", "/health") is None
