"""Hub-served repos page (``/deck/repos``): repository policy joined to coordination.

The page must join only on explicit canonical repository identities, keep
claim-only targets visible with their linkage stated as unknown, and never
invent ownership, privacy, or telemetry it does not have.
"""

from __future__ import annotations

import http.client
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest

from brigade import fleet_command_deck as deck
from brigade import fleet_hub, fleet_hub_policy, fleet_policy, fleet_policy_page
from brigade import fleet_repo_policy_page as repos_page

NOW = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
NODE_A = "11111111-1111-4111-8111-111111111111"
TOKEN = "test-admin-token-repos"  # content-guard: allow api-key-assignment
LOOPBACK = "127.0.0.1"  # content-guard: allow loopback-ipv4

OVERRIDDEN = "acme/private-tool"
PLAIN = "acme/public-tool"


def _document() -> dict:
    return {
        "schema": fleet_policy.POLICY_SCHEMA,
        "defaults": {
            "roles": {"impl": "seat-alpha", "review": "seat-beta"},
            "data": {"allow_training": True, "retention": "30d"},
            "execution": {"concurrency": 2},
        },
        "machines": {"worker-linux-1": {"os": "linux", "concurrency": 2}},
        "seats": {
            "seat-alpha": {"provider": "provider-a", "model": "model-a-1"},
            "seat-beta": {"provider": "provider-b", "model": "model-b-1"},
        },
        "consumers": {"brigade-run": {"reload": "refreshable"}},
        "repositories": {
            PLAIN: {"privacy": "public", "owner": "team-example"},
            OVERRIDDEN: {
                "privacy": "private",
                "owner": "team-secret",
                "eligible_machines": ["worker-linux-1"],
                "patches": {"execution": {"concurrency": 8}, "roles": {"impl": "seat-beta"}},
            },
        },
    }


@pytest.fixture()
def conn(tmp_path):
    connection = fleet_hub.init_db(tmp_path / "fleet.db")
    try:
        saved = fleet_hub_policy.save_policy(
            connection,
            _document(),
            expected_version=fleet_hub_policy.current_policy(connection)["revision"],
            actor="operator",
            reason="seed",
        )
        fleet_hub_policy.record_session_policy(
            connection,
            node_id=NODE_A,
            session_id="session-one",
            consumer="brigade-run",
            repo_identity=OVERRIDDEN,
            revision=int(saved["revision"]),
            digest=str(saved["digest"]),
            source="test",
            loaded_at="2026-09-05T11:30:00Z",
        )
        # A later revision the session has not picked up, so the page has real
        # drift to report rather than a fabricated one.
        bumped = _document()
        bumped["defaults"]["execution"]["concurrency"] = 3
        fleet_hub_policy.save_policy(
            connection,
            bumped,
            expected_version=int(saved["revision"]),
            actor="operator",
            reason="raise default concurrency",
        )
        yield connection
    finally:
        connection.close()


def _policy(conn, known=()):
    return fleet_policy_page.load_view(conn, known_repositories=known)


def _deck_view(*rows) -> deck.DeckView:
    return deck.DeckView(stations=(), rail=(), repos=tuple(rows), outcomes=(), observers=())


def _claim_row(target: str, identity: str = "") -> deck.RepoRow:
    return deck.RepoRow(
        target=target,
        claim=deck.Claim(target=target, owner_node=NODE_A, owner_conductor="conductor-1", ttl_remaining=600),
        live=(),
        collision=False,
        repo_identity=identity,
    )


def _render(view, rows, **kwargs):
    return repos_page.render(view, rows=rows, nonce="nonce", now=NOW, **kwargs)


# --- projection ---------------------------------------------------------------


def test_a_repository_with_overrides_shows_policy_and_coordination(conn):
    view = _deck_view(_claim_row("private-tool", OVERRIDDEN))
    rows = repos_page.build_rows(view, _policy(conn))
    row = next(item for item in rows if item.identity == OVERRIDDEN)

    assert row.linked is True
    assert row.in_policy is True
    assert row.privacy == "private"
    assert row.owner == "team-secret"
    assert row.eligible_machines == "worker-linux-1"
    assert row.training == "denied"  # the resolver's private-repository constraint
    assert row.policy_revision == str(_policy(conn).revision)
    assert row.editor_href == f"/deck/policy#{fleet_policy_page.repository_anchor(OVERRIDDEN)}"
    assert row.claim_owner and row.claim_ttl == "600s left"

    roles = {leaf.path: leaf for leaf in row.roles}
    assert roles["roles.impl"].value == "seat-beta"
    assert roles["roles.impl"].overridden is True
    assert roles["roles.review"].value == "seat-beta"
    assert roles["roles.review"].overridden is False

    settings = {leaf.path: leaf for leaf in row.overrides}
    assert settings["execution.concurrency"].value == "8"
    assert settings["execution.concurrency"].overridden is True
    assert settings["data.retention"].overridden is False
    assert "inherited from fleet-defaults" in settings["data.retention"].layer

    session = row.sessions[0]
    assert session.consumer == "brigade-run"
    assert "drift" in session.drift
    assert row.verification == deck.UNKNOWN


def test_a_repository_row_renders_its_facts_and_the_scoped_editor_link(conn):
    view = _deck_view(_claim_row("private-tool", OVERRIDDEN))
    rows = repos_page.build_rows(view, _policy(conn))
    html = _render(view, rows, policy_available=True)

    assert OVERRIDDEN in html
    assert f'href="/deck/policy#{fleet_policy_page.repository_anchor(OVERRIDDEN)}"' in html
    assert "<strong>override</strong>" in html
    assert "inherited" in html
    assert "training denied" in html
    assert "drift" in html
    assert "no verification receipt or memory handoff state" in html
    # No second policy store: this page never offers a policy write.
    assert "<form" not in html


def test_a_claim_target_without_an_identity_stays_visible_with_unknown_linkage(conn):
    view = _deck_view(_claim_row("some-checkout"))
    rows = repos_page.build_rows(view, _policy(conn))
    row = next(item for item in rows if item.label == "some-checkout")

    assert row.linked is False
    assert row.identity == ""
    # Nothing is guessed from the basename.
    assert row.owner == deck.UNKNOWN
    assert row.privacy == deck.UNKNOWN
    assert row.training == deck.UNKNOWN
    assert row.claim_ttl == "600s left"
    assert "cannot be joined" in row.linkage_note

    html = _render(view, rows, policy_available=True)
    assert "some-checkout" in html
    assert "Policy linkage unknown" in html


def test_a_basename_match_is_not_treated_as_an_identity(conn):
    """``private-tool`` looks like ``acme/private-tool``. It is not joined to it."""
    view = _deck_view(_claim_row("private-tool"))
    rows = repos_page.build_rows(view, _policy(conn))
    unlinked = next(item for item in rows if item.label == "private-tool")
    linked = next(item for item in rows if item.identity == OVERRIDDEN)
    assert unlinked.linked is False
    assert linked.claim_owner == ""


def test_an_identity_with_no_policy_record_is_inherited_not_invented(conn):
    identity = "acme/unlisted-tool"
    view = _deck_view(_claim_row("unlisted", identity))
    rows = repos_page.build_rows(view, _policy(conn, known=(identity,)))
    row = next(item for item in rows if item.identity == identity)
    assert row.linked is True
    assert row.in_policy is False
    assert row.owner == deck.UNKNOWN
    assert row.privacy == "unknown"
    assert "not invented" in row.linkage_note
    assert row.claim_ttl == "600s left"


def test_an_unavailable_policy_projection_is_reported_not_hidden(conn):
    view = _deck_view(_claim_row("private-tool", OVERRIDDEN))
    rows = repos_page.build_rows(view, None, unavailable_reason="the policy authority could not be read")
    assert [row.linked for row in rows] == [False]
    html = _render(view, rows, policy_available=False, unavailable_reason="the policy authority could not be read")
    assert "Repository policy is unknown" in html
    assert "the policy authority could not be read" in html
    # Coordination is unaffected by the policy read failing.
    assert "private-tool" in html


def test_the_coordination_table_still_lists_claims_and_live_runs(conn):
    view = _deck_view(_claim_row("private-tool", OVERRIDDEN))
    html = _render(view, repos_page.build_rows(view, _policy(conn)), policy_available=True)
    assert "Claims and live runs" in html
    assert "<th>Target</th>" in html
    assert "600s left" in html


def test_values_are_escaped(conn):
    document = _document()
    document["repositories"]["acme/hostile"] = {"privacy": "public", "owner": "<script>alert(1)</script>"}
    fleet_hub_policy.save_policy(
        conn,
        document,
        expected_version=fleet_hub_policy.current_policy(conn)["revision"],
        actor="operator",
        reason="hostile",
    )
    view = _deck_view()
    html = _render(view, repos_page.build_rows(view, _policy(conn)), policy_available=True)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


# --- route --------------------------------------------------------------------


@contextmanager
def _hub(tmp_path):
    db = tmp_path / "hub" / "fleet.db"
    server = fleet_hub.make_server(LOOPBACK, 0, db, TOKEN)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield (LOOPBACK, server.server_address[1]), db
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _get(hub, path):
    host, port = hub
    connection = http.client.HTTPConnection(host, port, timeout=5)
    connection.request("GET", path, headers={"Authorization": f"Bearer {TOKEN}"})
    response = connection.getresponse()
    text = response.read().decode("utf-8")
    status = response.status
    connection.close()
    return status, text


def test_repos_route_serves_the_repository_policy_projection(tmp_path):
    with _hub(tmp_path) as (hub, db):
        connection = fleet_hub.open_db(db)
        try:
            fleet_hub_policy.save_policy(
                connection,
                _document(),
                expected_version=fleet_hub_policy.current_policy(connection)["revision"],
                actor="operator",
                reason="seed",
            )
        finally:
            connection.close()
        status, page = _get(hub, "/deck/repos")
        assert status == 200
        assert "Repository policy" in page
        assert OVERRIDDEN in page
        assert f'href="/deck/policy#{fleet_policy_page.repository_anchor(OVERRIDDEN)}"' in page
        assert "Claims and live runs" in page
        assert TOKEN not in page


def test_repos_route_keeps_the_deck_navigation(tmp_path):
    with _hub(tmp_path) as (hub, _db):
        status, page = _get(hub, "/deck/repos")
        assert status == 200
        assert '<a href="/deck/policy">policy</a>' in page
        assert '<a href="/deck/roster">roster</a>' in page


# --- live-run text ------------------------------------------------------------

# Harmless text characters, not an exploit payload: the point is that a repo
# name or run state containing ordinary punctuation renders as the text the
# server sent, in both the linked and the unlinked repository paths.
TEXTY_REPO = 'a&b <notes> "q"'
TEXTY_STATE = "running <phase 2> & waiting"


def _live_run(identity: str = "", *, repo: str = TEXTY_REPO, state: str = TEXTY_STATE) -> deck.LiveRun:
    return deck.LiveRun(
        node_id=NODE_A,
        run_id="run-one",
        repo=repo,
        seat="seat-alpha",
        harness="claude",
        state=state,
        bucket="running",
        age_seconds=10,
        elapsed_seconds=10,
        repo_identity=identity,
    )


def _live_row(target: str, identity: str = "", **kwargs) -> deck.RepoRow:
    return deck.RepoRow(
        target=target,
        claim=None,
        live=(_live_run(identity, **kwargs),),
        collision=False,
        repo_identity=identity,
    )


def test_live_runs_are_carried_as_text_not_prebuilt_html(conn):
    view = _deck_view(_live_row("private-tool", OVERRIDDEN))
    row = next(item for item in repos_page.build_rows(view, _policy(conn)) if item.identity == OVERRIDDEN)
    line = row.live[0]
    assert isinstance(line, repos_page.LiveLine)
    # The projection stores the server's text verbatim; escaping happens once,
    # at render, so no field can reach the page pre-formatted.
    assert line.repo == TEXTY_REPO
    assert line.state == TEXTY_STATE
    assert line.node_id == NODE_A[:12]


def test_live_run_text_is_escaped_on_a_linked_repository(conn):
    view = _deck_view(_live_row("private-tool", OVERRIDDEN))
    html = _render(view, repos_page.build_rows(view, _policy(conn)), policy_available=True)

    assert "a&amp;b &lt;notes&gt; &quot;q&quot;" in html
    assert "running &lt;phase 2&gt; &amp; waiting" in html
    assert "<notes>" not in html
    assert "<phase 2>" not in html


def test_live_run_text_is_escaped_on_an_unlinked_target(conn):
    view = _deck_view(_live_row("some-checkout"))
    rows = repos_page.build_rows(view, _policy(conn))
    row = next(item for item in rows if item.label == "some-checkout")
    assert row.linked is False
    html = _render(view, rows, policy_available=True)

    assert "a&amp;b &lt;notes&gt; &quot;q&quot;" in html
    assert "running &lt;phase 2&gt; &amp; waiting" in html
    assert "<notes>" not in html


def test_a_node_id_with_markup_characters_renders_as_text(conn):
    view = _deck_view(
        deck.RepoRow(
            target="some-checkout",
            claim=None,
            live=(
                deck.LiveRun(
                    node_id='<b>node</b>&"',
                    run_id="run-two",
                    repo="acme/plain",
                    seat="seat-alpha",
                    harness="claude",
                    state="running",
                    bucket="running",
                    age_seconds=1,
                    elapsed_seconds=1,
                ),
            ),
            collision=False,
        )
    )
    html = _render(view, repos_page.build_rows(view, _policy(conn)), policy_available=True)
    assert "<b>node</b>" not in html
    assert "&lt;b&gt;node&lt;/b&gt;" in html


# --- inherited machine policy -------------------------------------------------


def test_a_repository_without_a_machine_override_is_inherited_not_unknown(conn):
    view = _deck_view(_claim_row("public-tool", PLAIN))
    row = next(item for item in repos_page.build_rows(view, _policy(conn)) if item.identity == PLAIN)

    # Absent override is a policy fact, not a hole in the hub's knowledge.
    assert row.eligible_machines == repos_page.INHERITED_MACHINES
    assert row.eligible_machines != deck.UNKNOWN
    assert row.eligible_machines_note == repos_page.MACHINE_POLICY_NOTE
    # And no concrete machine list is invented for it.
    assert "worker-linux-1" not in row.eligible_machines


def test_an_explicit_machine_override_keeps_its_own_value(conn):
    view = _deck_view(_claim_row("private-tool", OVERRIDDEN))
    row = next(item for item in repos_page.build_rows(view, _policy(conn)) if item.identity == OVERRIDDEN)
    assert row.eligible_machines == "worker-linux-1"
    assert row.eligible_machines_note == ""


def test_the_inherited_machine_note_is_rendered_and_links_to_the_policy_editor(conn):
    view = _deck_view(_claim_row("public-tool", PLAIN))
    html = _render(view, repos_page.build_rows(view, _policy(conn)), policy_available=True)

    assert repos_page.INHERITED_MACHINES in html
    assert "fleet machine policy applies" in html
    assert "seat and workload constraints" in html
    assert f'href="/deck/policy#{fleet_policy_page.repository_anchor(PLAIN)}"' in html


def test_absent_linkage_stays_unknown_rather_than_inherited(conn):
    """An unlinked target has no policy record to inherit from: that is unknown."""
    view = _deck_view(_claim_row("some-checkout"))
    row = next(item for item in repos_page.build_rows(view, _policy(conn)) if item.label == "some-checkout")
    assert row.eligible_machines == deck.UNKNOWN
    assert row.eligible_machines_note == ""


def test_the_repos_page_renders_no_em_dash(conn):
    view = _deck_view(_claim_row("private-tool", OVERRIDDEN), _live_row("some-checkout"))
    html = _render(view, repos_page.build_rows(view, _policy(conn)), policy_available=True)
    assert "&mdash;" not in html
    assert "—" not in html


def test_repository_free_permission_override_and_rendering(conn):
    doc = _document()
    doc["repositories"][PLAIN]["patches"] = {"data": {"allow_free": True}}
    current = fleet_hub_policy.current_policy(conn)
    fleet_hub_policy.save_policy(
        conn, doc, expected_version=current["revision"], actor="tester", reason="free-override"
    )

    view = _deck_view(_claim_row("public-tool", PLAIN), _claim_row("unlinked-checkout"))
    rows = repos_page.build_rows(view, _policy(conn))
    plain_row = next(r for r in rows if r.identity == PLAIN)
    assert plain_row.free == "allowed"

    overridden_row = next(r for r in rows if r.identity == OVERRIDDEN)
    assert overridden_row.free == "denied"

    unlinked_row = next(r for r in rows if r.label == "unlinked-checkout")
    assert unlinked_row.free == deck.UNKNOWN

    html = _render(view, rows, policy_available=True)
    assert "&middot; free allowed" in html
    assert "free models allowed" in html
    assert "&middot; free denied" in html
    assert "free models denied" in html
