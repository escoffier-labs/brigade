"""Server-selected details survive ordinary navigation without a script runtime."""

from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from brigade import fleet_admin_explorer as explorer, fleet_admin_pages, fleet_command_deck as deck
from brigade import fleet_hub, fleet_hub_model_roster, fleet_hub_roster_page as roster, fleet_policy_page as policy
from tests.test_fleet_deck_overview import NOW, NODE_B, _mixed_view
from tests.test_fleet_policy_page import _seed, _fresh_inventory
from tests.test_fleet_roster_page import _hub, _request, _bearer, _seed as seed_roster
from tests.test_ui_theme_integration import Markup


def assert_selection(page, key):
    markup = Markup(page)
    links = [attrs for tag, attrs in markup.tags if tag == "a" and "data-record-link" in attrs]
    details = [attrs for tag, attrs in markup.tags if tag == "article" and "data-record-detail" in attrs]
    assert len(links) > 1
    assert len(details) == len(links)
    selected = [attrs for attrs in links if attrs.get("aria-current") == "true"]
    assert len(selected) == 1
    assert parse_qs(urlsplit(selected[0]["href"]).query)["selected"] == [key]
    assert [attrs["id"] for attrs in details if "hidden" not in attrs] == [selected[0]["data-record-link"]]
    assert all(attrs["href"].startswith("?") for attrs in links)
    return links, markup


@pytest.fixture()
def pages(tmp_path):
    conn = fleet_hub.init_db(tmp_path / "fixture.db")
    try:
        _seed(conn)
        for seat in ("seat-alpha", "seat-beta"):
            fleet_hub_model_roster.set_model_policy(
                conn,
                {
                    "seat": seat,
                    "provider": "provider-a",
                    "model": "model-a-1",
                    "enabled": True,
                    "reasoning": "none",
                    "expected_revision": fleet_hub_model_roster.raw_revision(conn),
                },
            )
        roster_view = roster.load_view(conn, deck.DeckConfig())
        policy_view = policy.load_view(conn, inventory=_fresh_inventory())
        yield {
            "roster": lambda query: roster.render(
                roster_view, nonce="fixture", now=NOW, csrf="fixture-csrf", editable=True, query=query
            ),
            "policy": lambda query: policy.render(
                policy_view, nonce="fixture", now=NOW, csrf="fixture-csrf", editable=True, query=query
            ),
            "machines": lambda query: fleet_admin_pages.render_capacity(
                _mixed_view(), NOW, nonce="fixture", worklore_enabled=False, query=query
            ),
        }
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("name", "first", "selected"),
    [
        ("roster", "roles", "seat:seat-beta"),
        ("policy", "defaults", "seat:seat-beta"),
        ("machines", "11111111-1111-4111-8111-111111111111", NODE_B),
    ],
)
def test_each_page_has_one_default_and_link_selected_detail(pages, name, first, selected):
    render = pages[name]
    assert_selection(render(""), first)
    query = urlencode({"selected": selected, "saved": "2", "filter": '<script>"&'})
    page = render(query)
    links, _ = assert_selection(page, selected)
    assert '<script>"&' not in page
    for link in links:
        params = parse_qs(urlsplit(link["href"]).query)
        assert params["saved"] == ["2"]
        assert params["filter"] == ['<script>"&']
        # Each link is independently renderable after a reload with scripts disabled.
        assert_selection(render(urlsplit(link["href"]).query), params["selected"][0])


@pytest.mark.parametrize("bad", ['<script>alert("x")</script>', "not-a-fixture-record", "x" * 257, "x" * 9000])
@pytest.mark.parametrize(
    ("name", "first"),
    [("roster", "roles"), ("policy", "defaults"), ("machines", "11111111-1111-4111-8111-111111111111")],
)
def test_bad_selected_ids_fall_back_without_reflection(pages, name, first, bad):
    page = pages[name](urlencode({"selected": bad}))
    assert_selection(page, first)
    assert bad not in page


def test_query_selection_is_bounded_and_duplicate_ids_are_invalid():
    entries = [
        explorer.Entry("first", 'First <"&', "Ready", "<p>First detail</p>"),
        explorer.Entry("second", "Second", "Ready", "<p>Second detail</p>"),
    ]
    for query in ("selected=second&selected=first", "&".join(["x=1"] * 33) + "&selected=second"):
        assert_selection(explorer.render(entries, label="entries", query=query), "first")
    page = explorer.render(entries, label="entries", query="selected=second")
    assert_selection(page, "second")
    assert "First &lt;&quot;&amp;" in page
    empty = explorer.render([], label="entries", query="selected=missing")
    assert "No records reported." in empty and "data-record-detail" not in empty


def test_roster_controls_keep_the_bulk_form_owner_even_in_hidden_details(pages):
    _, markup = assert_selection(pages["roster"]("selected=seat%3Aseat-beta"), "seat:seat-beta")
    fields = [
        (index, attrs)
        for index, (tag, attrs) in enumerate(markup.tags)
        if tag in {"input", "select", "textarea"} and "name" in attrs
    ]
    form = next(
        index for index, (tag, attrs) in enumerate(markup.tags) if tag == "form" and attrs.get("id") == "roster-save"
    )
    assert {attrs["name"] for _, attrs in fields} >= {"csrf", "expected_revision", "seat.seat-alpha", "seat.seat-beta"}
    assert all(attrs.get("form") == "roster-save" or form in markup.ancestors[index] for index, attrs in fields)


def test_readonly_pages_select_without_offering_write_controls(tmp_path):
    conn = fleet_hub.init_db(tmp_path / "fixture.db")
    try:
        _seed(conn)
        view = policy.load_view(conn, inventory=_fresh_inventory())
        page = policy.render(
            view, nonce="fixture", now=NOW, csrf="secret-fixture", editable=False, query="selected=seat%3Aseat-beta"
        )
        assert_selection(page, "seat:seat-beta")
        assert "<form" not in page and "secret-fixture" not in page
        view = roster.load_view(conn, deck.DeckConfig())
        initial = roster.render(view, nonce="fixture", now=NOW, csrf="secret-fixture", editable=False)
        links = [attrs for tag, attrs in Markup(initial).tags if tag == "a" and "data-record-link" in attrs]
        query = urlsplit(links[-1]["href"]).query
        selected = parse_qs(query)["selected"][0]
        assert_selection(
            roster.render(view, nonce="fixture", now=NOW, csrf="secret-fixture", editable=False, query=query), selected
        )
    finally:
        conn.close()


def test_http_roster_and_policy_forward_selected_query(tmp_path):
    with _hub(tmp_path) as (address, db):
        seed_roster(address)
        conn = fleet_hub.open_db(db)
        try:
            _seed(conn)
        finally:
            conn.close()
        for route, selected in (("roster", "seat:coder"), ("policy", "seat:seat-beta")):
            path = "/deck/" + route + "?" + urlencode({"selected": selected})
            status, headers, page = _request(address, "GET", path, headers=_bearer())
            assert status == 200
            assert_selection(page, selected)
            assert "nonce-" in headers["content-security-policy"]
            assert_selection(_request(address, "GET", path, headers=_bearer())[2], selected)


def test_capacity_adapter_forwards_query():
    from brigade import fleet_deck_pages_http as adapter

    page = adapter.render(
        "/deck/capacity",
        _mixed_view(),
        NOW,
        nonce="fixture",
        worklore_enabled=False,
        extras=adapter.AdminExtras(),
        board_options=None,
        query="selected=" + NODE_B,
    )
    assert_selection(page, NODE_B)


def test_shared_explorer_stacks_below_mobile_breakpoint():
    mobile = explorer.CSS.split("@media (max-width:700px)", 1)[1]
    assert ".admin-explorer { grid-template-columns:minmax(0,1fr); }" in mobile
    assert ".explorer-list { border-right:0; border-bottom:1px solid var(--line); }" in mobile


def test_existing_hostile_key_is_encoded_in_links_and_selected_safely():
    key = 'seat:<"&'
    page = explorer.render(
        [
            explorer.Entry("first", "First", "Ready", "<p>First</p>"),
            explorer.Entry(key, 'Seat <"&', "Ready", "<p>Selected</p>"),
        ],
        label="entries",
        query=urlencode({"selected": key}),
    )
    assert_selection(page, key)
    assert key not in page
    assert "selected=seat%3A%3C%22%26" in page


def test_long_valid_repository_identity_has_an_addressable_policy_detail(tmp_path):
    from tests.test_fleet_policy_page import _document, _submission

    identity = "example/" + "a" * 241
    document = _document()
    document["repositories"] = {identity: {"privacy": "public"}}
    conn = fleet_hub.init_db(tmp_path / "fixture.db")
    try:
        _seed(conn, document)
        view = policy.load_view(conn, inventory=_fresh_inventory())
        key = "repository:" + identity
        assert len(key) > 256
        href = explorer.selection_href(key)
        selected = parse_qs(urlsplit(href).query)["selected"][0]
        assert len(selected) <= 256
        page = policy.render(
            view, nonce="fixture", now=NOW, csrf="fixture-csrf", editable=True, query=urlsplit(href).query
        )
        assert_selection(page, selected)
        assert identity in page
        draft = _submission("repository", "preview", view.revision, target=identity)
        refused = policy.render(
            view, nonce="fixture", now=NOW, csrf="fixture-csrf", editable=True, draft=draft, error="Draft refused"
        )
        assert_selection(refused, selected)
    finally:
        conn.close()


def test_overview_and_sidebar_links_select_their_machine_without_fragments():
    from brigade import fleet_deck_sidebar

    view = _mixed_view()
    for content in (
        fleet_admin_pages.render_overview_machines(view, NOW),
        fleet_deck_sidebar._capacity_inventory(view),
    ):
        links = [
            attrs["href"]
            for tag, attrs in Markup(content).tags
            if tag == "a" and attrs.get("href", "").startswith("/deck/capacity?")
        ]
        assert len(links) == len(view.stations)
        assert all(not urlsplit(href).fragment for href in links)
        for href, station in zip(links, view.stations, strict=True):
            page = fleet_admin_pages.render_capacity(
                view, NOW, nonce="fixture", worklore_enabled=False, query=urlsplit(href).query
            )
            assert_selection(page, station.station.node_id)


def test_overview_routing_links_select_the_role_without_fragments(tmp_path):
    from brigade import fleet_live_routing

    conn = fleet_hub.init_db(tmp_path / "fixture.db")
    try:
        view = roster.load_view(conn, deck.DeckConfig())
        routing = fleet_live_routing.project(view)
        content = fleet_live_routing.render_overview(routing, NOW)
        links = [attrs for tag, attrs in Markup(content).tags if tag == "a" and "data-routing-role" in attrs]
        assert links
        for link in links:
            url = urlsplit(link["href"])
            assert url.path == "/deck/roster" and not url.fragment
            page = roster.render(view, nonce="fixture", now=NOW, csrf="fixture-csrf", editable=False, query=url.query)
            assert_selection(page, link["data-routing-role"])
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("state", "expected_worker", "expected_reviewer"),
    [("active", "seat-alpha", "seat-beta"), ("staged", "seat-beta", None)],
)
def test_approved_models_display_preserves_dispatch_projection(
    tmp_path, monkeypatch, state, expected_worker, expected_reviewer
):
    from brigade import fleet_hub_preference, fleet_live_routing, fleet_policy_migration, run_preference

    monkeypatch.setattr(fleet_policy_migration, "is_activated", lambda conn: state == "active")
    conn = fleet_hub.init_db(tmp_path / "fixture.db")
    try:
        _seed(conn)
        conn.execute(
            "INSERT INTO run_preference (id,impl,updated_at) VALUES (1,'seat-beta','fixture') "
            "ON CONFLICT(id) DO UPDATE SET impl='seat-beta'"
        )
        activation = policy.Activation(state, "fixture")
        view = roster.load_view(conn, deck.DeckConfig(), activation=activation)
        before = fleet_hub_preference.get_run_preference(conn)
        routing = fleet_live_routing.project(view)
        expected_roles = dict.fromkeys(run_preference.ROLE_FIELDS) | {
            "impl": expected_worker,
            "review": expected_reviewer,
        }
        assert {role: before[role] for role in run_preference.ROLE_FIELDS} == expected_roles
        assert {role.key: role.seat for role in routing.roles} == expected_roles

        page = fleet_live_routing.render_overview(routing, NOW)
        assert fleet_hub_preference.get_run_preference(conn) == before
        assert fleet_live_routing.project(roster.load_view(conn, deck.DeckConfig(), activation=activation)) == routing
    finally:
        conn.close()

    markup = Markup(page)
    section = next(i for i, (_, attrs) in enumerate(markup.tags) if attrs.get("aria-labelledby") == "approved-models")
    assert "Approved models (display only)" in markup.text[section]
    assert "Dispatch uses the assignment above." in markup.text[section]
    lanes = markup.descendants(section, "span")
    models = markup.descendants(section, "strong")
    assert [markup.text[i] for i in lanes] == [
        "Worker",
        "Reviewer",
        "Orchestrator",
        "Researcher and Scout",
        "Security reviewer",
        "Second opinion",
        "Writing",
    ]
    assert [markup.text[i] for i in models] == [
        "GPT 6.2 Sol (medium), GPT 6 Luna, DeepSeek V4.1 Flash (opencode Go), Claude Sonnet 5.5 (Claude builds)",
        "DeepSeek V4.1 Flash (opencode Go) and Gemini 4 Hydrogen (first pass), Claude Opus 5.5 (final)",
        "Claude Opus 5.5",
        "DeepSeek V4.1 Flash (opencode Go) first",
        "GPT Daybreak Blue",
        "GPT 6.2 Astra and Claude Fable 5.2, low or medium effort, sign-off required",
        "Gemini 4 Neon (via agy)",
    ]
    assert not markup.descendants(section, "a")
