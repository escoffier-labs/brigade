"""Hub HTTP contracts for the ownership map: ``GET /owners`` and ``/deck/owners``."""

from __future__ import annotations

import http.client
import json
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from html.parser import HTMLParser

from brigade.fleet_deck_icons import NAV_ICONS

from brigade import fleet_hub, fleet_hub_policy, fleet_owners_page, fleet_policy
from brigade.fleet_owners import Holding, Overlap, OwnersView

TOKEN = "test-admin-token-owners"  # content-guard: allow api-key-assignment
LOOPBACK = "127.0.0.1"  # content-guard: allow loopback-ipv4


@contextmanager
def _hub(tmp_path, **kwargs):
    db = tmp_path / "fleet.db"
    server = fleet_hub.make_server(LOOPBACK, 0, db, TOKEN, **kwargs)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield (LOOPBACK, server.server_address[1]), db
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _request(hub, path, *, headers=None, method="GET", body=None):
    conn = http.client.HTTPConnection(*hub, timeout=5)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read().decode()
    finally:
        conn.close()


def _bearer(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


def _cookie(hub):
    status, _, body = _request(
        hub,
        "/dashboard/enrollment",
        method="POST",
        headers={**_bearer(), "Content-Type": "application/json"},
        body=b"{}",
    )
    assert status == 201
    code = json.loads(body)["code"]
    status, headers, _ = _request(
        hub,
        "/enroll",
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded", "Sec-Fetch-Site": "same-origin"},
        body=f"code={code}".encode(),
    )
    assert status == 303
    return {"Cookie": headers["Set-Cookie"].split(";")[0]}


def _seed_owners(db, owners):
    conn = fleet_hub.open_db(db)
    try:
        document = fleet_policy.empty_document()
        document["owners"] = owners
        revision = fleet_hub_policy.current_policy(conn)["revision"]
        fleet_hub_policy.save_policy(conn, document, expected_version=revision, actor="admin", reason="seed owners")
    finally:
        conn.close()


def _node_token(db, node_id="node-a-0001"):
    conn = fleet_hub.open_db(db)
    try:
        return fleet_hub.add_node(conn, node_id, "Node A")[1]
    finally:
        conn.close()


def test_owners_json_requires_a_bearer_token(tmp_path):
    with _hub(tmp_path) as (hub, _db):
        status, _, body = _request(hub, "/owners")
        assert status == 401 and json.loads(body) == {"error": "unauthorized"}
        assert _request(hub, "/owners", headers=_bearer("wrong-token"))[0] == 401
        assert _request(hub, "/owners", headers=_cookie(hub))[0] == 401


def test_owners_json_serves_the_v1_shape_to_admin_and_node_tokens(tmp_path):
    with _hub(tmp_path) as (hub, db):
        _seed_owners(db, {"alpha": {"label": "Alpha", "owns": ["Notes"], "match": {"harnesses": ["claude"]}}})
        for headers in (_bearer(), _bearer(_node_token(db))):
            status, response_headers, body = _request(hub, "/owners", headers=headers)
            assert status == 200
            assert response_headers["Content-Type"].startswith("application/json")
            payload = json.loads(body)
            assert payload["schema"] == "brigade.fleet_owners.v1"
            assert payload["policy_version"] == 2
            assert [owner["name"] for owner in payload["owners"]] == ["alpha"]
            assert payload["owners"][0]["owns"] == ["Notes"]
            assert payload["unassigned"] == [] and payload["overlaps"] == []


def test_owners_json_lists_live_claims_and_reads_without_writing(tmp_path):
    with _hub(tmp_path, allow_admin_writes=True) as (hub, db):
        _seed_owners(db, {"alpha": {"label": "Alpha", "match": {"harnesses": ["claude"]}}})
        claim = {
            "action": "acquire",
            "target": "example/repo",
            "node_id": "node-a-0001",
            "holder": "holder-1",
            "harness": "claude",
        }
        status, _, text = _request(
            hub,
            "/claims",
            method="POST",
            headers={**_bearer(), "Content-Type": "application/json"},
            body=json.dumps(claim),
        )
        assert status == 200, text
        conn = fleet_hub.open_db(db)
        try:
            before = list(conn.iterdump())
        finally:
            conn.close()
        status, _, body = _request(hub, "/owners", headers=_bearer())
        assert status == 200
        holdings = json.loads(body)["owners"][0]["holdings"]
        assert [(item["kind"], item["ref"]) for item in holdings] == [("claim", "example/repo")]
        conn = fleet_hub.open_db(db)
        try:
            assert list(conn.iterdump()) == before
        finally:
            conn.close()


def _seed_grokbot_job(db):
    conn = fleet_hub.open_db(db)
    try:
        stamp = "2026-01-01T12:00:00+00:00"
        conn.execute(
            "INSERT INTO grokbot_jobs ("
            "job_id, role, repository, label, task_digest, idempotency_key_hash, state, item_revision, "
            "sequence, created_at, updated_at, queued_at, timeout_seconds, artifact_kind, owner_node"
            ") VALUES ('grokbot-1', 'implementation-worker', 'example/repo', 'Job', ?, ?, 'queued', 1, 1, "
            "?, ?, ?, 900, 'draft-pr', 'queue-owner')",
            ("d" * 64, "f" * 64, stamp, stamp, stamp),
        )
        conn.commit()
    finally:
        conn.close()


def test_owners_json_hides_grokbot_jobs_from_node_tokens_but_not_admin(tmp_path):
    with _hub(tmp_path) as (hub, db):
        _seed_owners(db, {"bot": {"label": "Bot", "match": {"grokbot_jobs": True}}})
        _seed_grokbot_job(db)
        admin = json.loads(_request(hub, "/owners", headers=_bearer())[2])
        node = json.loads(_request(hub, "/owners", headers=_bearer(_node_token(db)))[2])
        assert [item["ref"] for item in admin["owners"][0]["holdings"]] == ["grokbot-1"]
        assert node["owners"][0]["holdings"] == [] and node["unassigned"] == []
        assert node["unknown_sources"] == []


def test_owners_page_shows_grokbot_jobs_only_to_editable_viewers(tmp_path):
    # Grok Bot job rows are admin-only elsewhere (/cloud, /grokbot), so a read-only
    # dashboard cookie must not see them here either.
    with _hub(tmp_path) as (hub, db):
        _seed_owners(db, {"bot": {"label": "Bot", "match": {"grokbot_jobs": True}}})
        _seed_grokbot_job(db)
        status, _, admin_body = _request(hub, "/deck/owners", headers=_bearer())
        assert status == 200 and "grokbot-1" in admin_body
        status, _, cookie_body = _request(hub, "/deck/owners", headers=_cookie(hub))
        assert status == 200 and "grokbot-1" not in cookie_body
        assert "Nothing held right now." in cookie_body


NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
ATTACK = '<img src=x onerror="alert(1)">'


def _match(**selectors):
    record = {
        "nodes": [],
        "harnesses": [],
        "conductors": [],
        "session_harnesses": [],
        "adapters": [],
        "principals": [],
        "grokbot_jobs": False,
    }
    record.update(selectors)
    return record


def _holding(ref="example/repo", kind="claim", **fields):
    return Holding(
        kind=kind,
        ref=ref,
        repo="example/repo",
        node_label="Node A",
        detail="impl claude",
        since="2026-01-01T00:00:00+00:00",
        **fields,
    )


def _view(**fields):
    base = {
        "policy_version": 3,
        "policy_error": None,
        "charters": {},
        "by_owner": {},
        "unassigned": [],
        "overlaps": [],
        "enrolled_principals": frozenset(),
        "unknown_sources": (),
        "truncated_sources": (),
    }
    base.update(fields)
    return OwnersView(**base)


def _render(view):
    return fleet_owners_page.render(view, nonce="test-nonce", now=NOW)


class _Tags(HTMLParser):
    def __init__(self, body):
        super().__init__()
        self.tags = []
        self.feed(body)

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)


def test_page_requires_dashboard_access_and_is_read_only(tmp_path):
    with _hub(tmp_path, trust_tailscale_identity=False) as (hub, db):
        status, _, body = _request(hub, "/deck/owners")
        assert status == 401 and "brigade fleet enroll" in body
        assert _request(hub, "/deck/owners", headers=_bearer(_node_token(db)))[0] == 401
        for headers in (_bearer(), _cookie(hub)):
            status, response_headers, body = _request(hub, "/deck/owners", headers=headers)
            assert status == 200
            assert "Content-Security-Policy" in response_headers
            assert response_headers["Cache-Control"] == "no-store"
            assert "No charters declared" in body
            assert not {"form", "input", "button"} & set(_Tags(body).tags)
        assert _request(hub, "/deck/owners?cursor=1", headers=_bearer())[0] == 400


def test_page_shows_charters_beside_live_holdings_and_unassigned_items(tmp_path):
    with _hub(tmp_path, allow_admin_writes=True) as (hub, db):
        _seed_owners(
            db,
            {
                "alpha": {
                    "label": "Alpha Agent",
                    "summary": "Builds things.",
                    "owns": ["Approved issues"],
                    "never": ["Deploys"],
                    "intake": ["Approval label"],
                    "contact": "agent:alpha",
                    "match": {"harnesses": ["claude"]},
                },
                "quiet": {"label": "Quiet Gateway"},
            },
        )
        for target, harness in (("example/held", "claude"), ("example/loose", "codex")):
            claim = {
                "action": "acquire",
                "target": target,
                "node_id": "node-a-0001",
                "holder": "h1",
                "harness": harness,
            }
            status, _, text = _request(
                hub,
                "/claims",
                method="POST",
                headers={**_bearer(), "Content-Type": "application/json"},
                body=json.dumps(claim),
            )
            assert status == 200, text
        status, _, body = _request(hub, "/deck/owners", headers=_bearer())
        assert status == 200
        for text in ("Alpha Agent", "Builds things.", "Approved issues", "Deploys", "Approval label", "agent:alpha"):
            assert text in body
        assert "Quiet Gateway" in body and "Charter only, no live attribution." in body
        alpha_card, _, rest = body.partition("Quiet Gateway")
        assert "example/held" in alpha_card and "example/loose" not in alpha_card
        unassigned = rest.partition("<h2>Unassigned</h2>")[2]
        assert "example/loose" in unassigned and "example/held" not in unassigned
        assert "No overlaps." in body


def test_page_lists_overlaps_and_principal_state():
    item = _holding()
    view = _view(
        charters={
            "alpha": {"label": "Alpha", "match": _match(harnesses=["claude"], principals=["miller", "ghost"])},
            "beta": {"label": "Beta", "match": _match(nodes=["node-a"])},
        },
        by_owner={"alpha": [item], "beta": [item]},
        overlaps=[Overlap(item=item, owners=("alpha", "beta"))],
        enrolled_principals=frozenset({"miller"}),
    )
    body = _render(view)
    assert "claim example/repo: alpha, beta" in body
    assert "miller: enrolled" in body and "ghost: not enrolled" in body
    assert "revision 3" in body


def test_policy_failure_banner_still_lists_live_items_as_unassigned():
    view = _view(policy_version=None, policy_error="policy read failed: FleetHubError", unassigned=[_holding()])
    body = _render(view)
    assert "Policy unavailable" in body and "policy read failed: FleetHubError" in body
    assert "revision unknown" in body
    assert "No charters declared" not in body
    assert "Unassigned" in body and "example/repo" in body


def test_unknown_source_is_called_out_instead_of_shown_as_empty():
    view = _view(
        charters={"alpha": {"label": "Alpha", "match": _match(harnesses=["claude"])}},
        by_owner={"alpha": []},
        unknown_sources=("claims", "sessions"),
    )
    body = _render(view)
    assert "Holdings unknown: could not read claims, sessions." in body
    assert "Could not read: claims, sessions." in body
    assert "Nothing held right now." not in body


def test_a_principals_read_failure_does_not_mark_holdings_unknown():
    view = _view(
        charters={"alpha": {"label": "Alpha", "match": _match(harnesses=["claude"])}},
        by_owner={"alpha": []},
        unknown_sources=("principals",),
    )
    body = _render(view)
    assert "Holdings unknown" not in body
    assert "Nothing held right now." in body


def test_long_overlap_lists_are_capped_with_a_note():
    overlaps = [Overlap(item=_holding(ref=f"example/repo-{index}"), owners=("alpha", "beta")) for index in range(60)]
    body = _render(_view(overlaps=overlaps))
    assert "Showing 50 of 60 overlaps." in body
    assert "Showing" not in _render(_view(overlaps=overlaps[:3]))


def test_truncated_sources_are_noted_on_the_page():
    body = _render(_view(unassigned=[_holding()], truncated_sources=("claims", "sessions")))
    assert "Truncated at the source limit: claims, sessions." in body
    assert "Truncated at the source limit" not in _render(_view(unassigned=[_holding()]))


def test_stored_text_is_escaped_everywhere():
    evil = _holding(ref=ATTACK)
    view = _view(
        charters={
            "alpha": {
                "label": ATTACK,
                "summary": ATTACK,
                "owns": [ATTACK],
                "never": [ATTACK],
                "intake": [ATTACK],
                "contact": ATTACK,
                "match": _match(harnesses=["claude"], principals=[ATTACK]),
            }
        },
        by_owner={"alpha": [evil]},
        unassigned=[evil],
        overlaps=[Overlap(item=evil, owners=("alpha", ATTACK))],
    )
    body = _render(view)
    assert ATTACK not in body
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in body


def test_long_holding_lists_are_capped_with_a_note():
    items = [_holding(ref=f"example/repo-{index:03}") for index in range(fleet_owners_page.DISPLAY_LIMIT + 1)]
    view = _view(unassigned=items)
    body = _render(view)
    assert f"Showing {fleet_owners_page.DISPLAY_LIMIT} of {len(items)} items." in body
    assert "example/repo-049" in body and "example/repo-050" not in body


def test_owners_link_is_in_the_navigation_of_every_deck_page_and_board(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIGADE_WORKLORE_ENABLED", "1")
    with _hub(tmp_path) as (hub, _db):
        for path in (
            "/deck",
            "/deck/repos",
            "/deck/roster",
            "/deck/policy",
            "/deck/work",
            "/deck/owners",
            "/view/machines",
            "/view/repos",
        ):
            status, _, body = _request(hub, path, headers=_bearer())
            assert status == 200, path
            assert 'href="/deck/owners"' in body, path
        _, _, body = _request(hub, "/deck/owners", headers=_bearer())
        assert f'<a href="/deck/owners" aria-current="page">{NAV_ICONS["owners"]}owners</a>' in body
        assert '<a href="/deck" aria-current="page">' not in body
