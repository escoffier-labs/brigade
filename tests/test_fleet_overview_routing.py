"""Overview live routing agrees with the served read-only authority view."""

import html
from urllib.parse import parse_qs, urlsplit

import pytest

from brigade import fleet_hub, fleet_hub_roster_page as roster, fleet_policy_page as policy
from tests.test_fleet_policy_page import _RouteHub, _document, _seed
from tests.test_ui_theme_integration import Markup


@pytest.mark.parametrize(
    "state,conflict", [("active", False), ("active", True), ("staged", True), ("unavailable", True), ("unknown", True)]
)
def test_served_overview_routing_matches_live_routing(tmp_path, monkeypatch, state, conflict):
    activation = policy.Activation("active" if state == "unavailable" else state, "fixture", source="test")
    monkeypatch.setattr(policy, "probe_activation", lambda conn: activation)
    if state == "unavailable":
        monkeypatch.setattr(roster, "_policy_context", lambda conn: roster.PolicyContext())
    with _RouteHub(tmp_path) as hub:
        conn = fleet_hub.open_db(hub.db)
        try:
            doc = _document()
            doc["defaults"]["roles"].update(chef="seat-alpha", research="seat-beta")
            _seed(conn, doc)
            if conflict:
                conn.execute(
                    "INSERT INTO model_policy (seat,provider,model,reasoning,enabled,updated_at) "
                    "VALUES ('seat-alpha','legacy-provider','legacy-model','low',1,'fixture')"
                )
                conn.execute(
                    "INSERT INTO run_preference (id,impl,review,chef,research,updated_at) VALUES (1,'seat-alpha','seat-alpha','seat-alpha','seat-alpha','fixture') ON CONFLICT(id) DO UPDATE SET impl='seat-alpha',review='seat-alpha',chef='seat-alpha',research='seat-alpha'"
                )
                conn.commit()
            schema = list(conn.execute("SELECT name,sql FROM sqlite_master ORDER BY name"))
        finally:
            conn.close()
        headers = {"Cookie": hub.cookie()}
        status, _, settings = hub.request("GET", "/deck/roster", headers=headers)
        assert status == 200
        for path in ("/", "/deck"):
            status, _, page = hub.request("GET", path, headers=headers)
            assert status == 200
            parsed = Markup(page)
            root = next(i for i, (_, attrs) in enumerate(parsed.tags) if attrs.get("id") == "overview-routing")
            cells = [
                (i, attrs)
                for i, (_, attrs) in enumerate(parsed.tags)
                if "data-routing-role" in attrs and root in parsed.ancestors[i]
            ]
            assert [attrs["data-routing-role"] for _, attrs in cells] == [
                "impl",
                "review",
                "chef",
                "research",
                "security",
                "scout",
            ]
            expected = (
                "Assignment unknown" if state == "unavailable" else "model-a-1" if state == "active" else "legacy-model"
            )
            for i, attrs in cells:
                role = attrs["data-routing-role"]
                title = {
                    "impl": "Worker",
                    "review": "Reviewer",
                    "chef": "Orchestrator",
                    "research": "Researcher",
                    "security": "Security reviewer",
                    "scout": "Scout",
                }[role]
                detail = settings.split("<h2>" + title + "</h2>", 1)[1].split("</article>", 1)[0]
                model = expected if role not in {"security", "scout"} or state == "unavailable" else "Not assigned"
                if state == "active" and role in {"review", "research"}:
                    model = "model-b-1"
                assert model in parsed.text[i] and model in detail
                assert title in parsed.text[i]
                url = urlsplit(attrs["href"])
                assert not url.fragment and parse_qs(url.query)["selected"] == [role]
                status, _, selected_page = hub.request("GET", attrs["href"], headers=headers)
                assert status == 200
                settings_markup = Markup(selected_page)
                selected = next(
                    j
                    for j, (tag, values) in enumerate(settings_markup.tags)
                    if tag == "article" and "data-record-detail" in values and "hidden" not in values
                )
                assert title in settings_markup.text[selected] and model in settings_markup.text[selected]
            summary = parsed.text[root]
            assert ("active policy" in summary) == (state in {"active", "unavailable"})
            assert ("run preference" in summary) == (state in {"staged", "unknown"})
            assert ("Assignment unknown" in summary) == (state == "unavailable")
            assert ("Copy prompt for an agent" in summary) == (state in {"unavailable", "unknown"})
            assert "provider availability and sign-in are unverified" in summary
            assert any(
                tag == "a" and attrs.get("href") == "/deck/roster" and parsed.text[i] == "View live routing"
                for i, (tag, attrs) in enumerate(parsed.tags)
                if root in parsed.ancestors[i]
            )
            assert all(
                tag not in {"form", "input", "select"}
                for i, (tag, _) in enumerate(parsed.tags)
                if root in parsed.ancestors[i]
            )
            assert sum(attrs.get("class") == "admin-card" for _, attrs in parsed.tags) == 2
            assert "Needs attention" in page and "Work" in page and "Machines (" in page
            if state in {"active", "unavailable"}:
                assert "legacy-model" not in summary
        conn = fleet_hub.open_db(hub.db)
        try:
            assert list(conn.execute("SELECT name,sql FROM sqlite_master ORDER BY name")) == schema
        finally:
            conn.close()


def test_overview_routing_load_failure_is_bounded_and_other_pages_do_not_load_it(tmp_path, monkeypatch):
    def unavailable(*args, **kwargs):
        raise ValueError("private exception text must stay private")

    monkeypatch.setattr(roster, "load_view", unavailable)
    with _RouteHub(tmp_path) as hub:
        headers = {"Cookie": hub.cookie()}
        status, _, page = hub.request("GET", "/deck", headers=headers)
        assert status == 200
        assert 'id="overview-routing"' in page and "Assignment unknown" in page and "Copy prompt for an agent" in page
        assert "ValueError" in html.unescape(page) and "private exception text" not in page
        status, _, page = hub.request("GET", "/deck/activity", headers=headers)
        assert status == 200 and 'id="overview-routing"' not in page


def test_overview_routing_escapes_display_fields_and_excludes_stored_notes(tmp_path, monkeypatch):
    hostile = 'fixture</strong><script>bad("x")</script>'
    monkeypatch.setattr(policy, "probe_activation", lambda conn: policy.Activation("staged", "fixture"))
    with _RouteHub(tmp_path) as hub:
        conn = fleet_hub.open_db(hub.db)
        try:
            conn.execute(
                "INSERT INTO model_policy (seat,provider,model,reasoning,notes,enabled,updated_at) "
                "VALUES ('fixture-seat','fixture-provider',?,?,?,1,'fixture')",
                (hostile, hostile, "private-notes-omit"),
            )
            conn.execute(
                "INSERT INTO run_preference (id,impl,updated_at) VALUES (1,'fixture-seat','fixture') "
                "ON CONFLICT(id) DO UPDATE SET impl='fixture-seat'"
            )
            conn.commit()
        finally:
            conn.close()
        status, _, page = hub.request("GET", "/deck", headers={"Cookie": hub.cookie()})
        assert status == 200 and hostile not in page and html.escape(hostile) in page
        assert "private-notes-omit" not in page
        parsed = Markup(page)
        worker = next(i for i, (_, attrs) in enumerate(parsed.tags) if attrs.get("data-routing-role") == "impl")
        assert hostile in parsed.text[worker]
        root = next(i for i, (_, attrs) in enumerate(parsed.tags) if attrs.get("id") == "overview-routing")
        assert not any(
            tag in {"textarea", "details", "input", "form", "select"}
            for i, (tag, _) in enumerate(parsed.tags)
            if root in parsed.ancestors[i]
        )
