"""Projects page: open pull requests from the stored CI projection."""

import re
from datetime import datetime, timezone

import pytest

from brigade import fleet_admin_pages as pages
from brigade import fleet_ci_board as ci
from brigade import fleet_ci_reconcile as node
from brigade import fleet_hub
from brigade import fleet_projects_prs as prs
from tests.test_fleet_ci import HEAD, NOW as CI_NOW, OLD, check, observation, put
from tests.test_fleet_ci_node import Provider, reader
from tests.test_fleet_deck_overview import _mixed_view
from tests.test_fleet_work_page import _bearer, _cookie, _hub, _request

PAGE_NOW = datetime(2026, 10, 21, 12, 30, tzinfo=timezone.utc)
HOSTILE = "<img src=x onerror=alert(1)>\"'&"
SHA_A, SHA_B, SHA_C, SHA_D = "1" * 40, "2" * 40, "3" * 40, "4" * 40


def pull(number, sha, **extra):
    return {"number": number, "head_sha": sha, "updated_at": CI_NOW, **extra}


@pytest.fixture
def db(tmp_path):
    conn = fleet_hub.init_db(tmp_path / "hub.db")
    yield conn
    conn.close()


def _seed(db):
    pulls = [
        pull(1, SHA_A, title="Fix the parser", author="sample-dev", draft=False, author_bot=False),
        pull(2, SHA_B, title="Break the parser", author="sample-dev", draft=False, author_bot=False),
        pull(3, SHA_C, title="Wait for CI", author="sample-dev", draft=False, author_bot=False),
        pull(4, SHA_D, title="Draft work", author="sample-dev", draft=True, author_bot=False),
        pull(
            5,
            OLD,
            title="chore(deps): bump thing",
            author="dependabot[bot]",
            author_bot=True,
            draft=False,
            labels=[{"name": "dependencies", "color": "0366d6"}, {"name": "go", "color": "not-a-color"}],
        ),
        pull(6, "5" * 40, title="No check data"),
    ]
    events = [
        check(head_sha=SHA_A, check_run_id=1, name="ci"),
        check(head_sha=SHA_B, check_run_id=2, name="ci", conclusion="failure"),
        check(head_sha=SHA_C, check_run_id=3, name="ci", status="in_progress", conclusion=None),
        check(head_sha=SHA_D, check_run_id=4, name="ci"),
        check(head_sha=OLD, check_run_id=5, name="ci"),
    ]
    snapshot = observation(
        pulls=pulls,
        checks_complete=[HEAD, SHA_A, SHA_B, SHA_C, SHA_D, OLD],
    )
    other = observation(repo="example/other", pulls=[pull(9, "6" * 40)], pulls_complete=False, complete=False)
    put(db, events=events, repositories=[snapshot, other])
    return ci.load_view(db, operator=True, now=CI_NOW)


def test_check_state_and_groups_follow_exact_head_rollup(db):
    data = prs.collect(_seed(db), PAGE_NOW)
    by = {(r["repo"], r["number"]): r for r in data["rows"]}
    assert by[("example/public", 1)]["group"] == "ready" and by[("example/public", 1)]["check"] == "pass"
    assert by[("example/public", 2)]["group"] == "failing"
    assert by[("example/public", 3)]["group"] == "waiting"
    assert by[("example/public", 4)]["group"] == "drafts"
    assert by[("example/public", 5)]["group"] == "deps"
    assert by[("example/public", 6)]["group"] == "unknown" and by[("example/public", 6)]["check"] == "unknown"
    assert by[("example/other", 9)]["check"] == "unknown"
    assert data["incomplete"] == ["example/other"]
    assert data["complete_repositories"] == 1


def test_rendering_links_labels_and_honest_notes(db):
    section, css = prs.render(_seed(db), PAGE_NOW)
    assert 'href="https://github.com/example/public/pull/1"' in section
    assert 'target="_blank" rel="noopener noreferrer"' in section
    assert "PR list incomplete for example/other" in section
    assert (
        re.search(r"Updated \d+[mhd] ago from 1 repository", section) or "Updated just now from 1 repository" in section
    )
    assert "lc-0366d6" in section and ".pr-label.lc-0366d6{--lc:#0366d6}" in css
    assert "not-a-color" not in section and "not-a-color" not in css
    # No invented numbers or review state.
    assert not re.search(r"\+\d+\s*-\d+|additions|deletions", section)
    assert "approved" not in section.lower() and "changes requested" not in section.lower()
    order = [m for m in re.findall(r'data-group-key="(\w+)"', section)]
    assert order == ["failing", "ready", "waiting", "drafts", "deps", "unknown"]
    # Full no-JS list: controls render hidden, every row present.
    assert "data-pr-controls hidden" in section and section.count('class="pr-row"') == 7
    # Agent block is tagged.
    assert 'data-audience="agent"' in section and f"head <code>{SHA_A[:12]}</code>" in section


def test_hostile_values_are_escaped_and_bad_repository_or_number_is_skipped():
    view = {
        "as_of": CI_NOW,
        "repositories": [
            {
                "repo": "example/safe",
                "head": {"observed_at": CI_NOW},
                "pulls_complete": True,
                "pulls": [
                    {
                        "number": 3,
                        "head_sha": SHA_A,
                        "title": HOSTILE,
                        "author": HOSTILE,
                        "labels": [{"name": HOSTILE, "color": 'fff"onmouseover="x'}],
                        "requested_reviewers": [HOSTILE],
                        "rollup": {"state": "passing"},
                        "updated_at": CI_NOW,
                    },
                    {"number": True, "head_sha": SHA_A, "rollup": {"state": "passing"}},
                    {"number": "7", "head_sha": SHA_A, "rollup": {"state": "passing"}},
                ],
            },
            {"repo": 'example/x"><script>', "pulls_complete": True, "pulls": [{"number": 1, "head_sha": SHA_A}]},
        ],
    }
    section, css = prs.render(view, PAGE_NOW)
    assert "<img" not in section and "onmouseover" not in section + css and "<script" not in section
    assert "&lt;img" in section
    assert section.count('class="pr-row"') == 1
    assert "pull/3" in section and "pull/True" not in section and "pull/7" not in section


def test_empty_states():
    section, _ = prs.render({"repositories": [], "as_of": CI_NOW}, PAGE_NOW)
    assert "No open pull requests observed" in section and "brigade fleet ci reconcile" in section
    seen = {"repositories": [{"repo": "example/empty", "head": {}, "pulls": [], "pulls_complete": True}]}
    section, _ = prs.render(seen, PAGE_NOW)
    assert "No open pull requests observed" in section and "reconcile" not in section
    unknown = {"repositories": [{"repo": "example/empty", "head": {}, "pulls": [], "pulls_complete": False}]}
    assert "PR list incomplete for example/empty" in prs.render(unknown, PAGE_NOW)[0]


def test_projects_page_has_no_pull_request_rows_and_links_to_the_pull_requests_page(db):
    view = _mixed_view()
    page = {"state": "empty", "campaigns": [], "selected": None}
    projects = pages.render_projects(page, view, PAGE_NOW, nonce="n", worklore_enabled=True)
    assert "data-pr-root" not in projects and "pr-row" not in projects
    assert "No projects yet" in projects
    assert '<a href="/deck/pulls">View open pull requests</a>' in projects
    assert "No project goals reported." not in projects
    pulls = pages.render_pulls(_seed(db), view, PAGE_NOW, nonce="n", worklore_enabled=True)
    assert "data-pr-root" in pulls and pulls.count('class="pr-row"') == 7
    assert '<a href="/deck/pulls" aria-current="page">Pull requests</a>' in pulls
    assert "No projects yet" not in pulls
    assert "—" not in re.sub(r"<script.*?</script>|<style.*?</style>", "", pulls, flags=re.S)
    unavailable = pages.render_pulls(None, view, PAGE_NOW, nonce="n", worklore_enabled=True)
    assert "Pull requests unavailable" in unavailable and "data-pr-root" not in unavailable


def test_pull_requests_tab_follows_projects_in_the_repository_section():
    from brigade import fleet_deck_sidebar as sidebar

    keys = [key for key, _, _ in sidebar.SECTION_ROUTES["repos"]]
    assert keys[:3] == ["projects", "pulls", "repos"]
    assert sidebar.section_for("pulls") == "repos"
    assert dict((k, (h, label)) for k, h, label in sidebar.SECTION_ROUTES["repos"])["pulls"] == (
        "/deck/pulls",
        "Pull requests",
    )


def test_reconcile_extracts_optional_fields_from_the_list_response_and_bounds_them(tmp_path, monkeypatch):
    provider = Provider()
    api = {
        "number": 5,
        "head": {"sha": OLD},
        "updated_at": CI_NOW,
        "created_at": "2026-10-20T10:00:00Z",
        "title": "t" * 500,
        "draft": True,
        "user": {"login": "dependabot[bot]", "type": "Bot"},
        "labels": [{"name": f"l{i}", "color": "00AA11"} for i in range(20)] + [{"name": HOSTILE, "color": "zzzzzz"}],
        "requested_reviewers": [{"login": f"rev{i}"} for i in range(30)],
    }
    real = node._pages

    def pages_(reader_, suffix, **kw):
        return [api] if suffix == "/pulls" else real(reader_, suffix, **kw)

    monkeypatch.setattr(node, "_pages", pages_)
    snapshot = node.reconcile_repo(reader(tmp_path, provider), now=CI_NOW)["repositories"][0]
    saved = snapshot["pulls"][0]
    assert len(saved["title"]) == 200 and saved["draft"] is True and saved["author_bot"] is True
    assert saved["author"] == "dependabot[bot]" and saved["created_at"] == "2026-10-20T10:00:00Z"
    assert len(saved["labels"]) == 8 and saved["labels"][0] == {"name": "l0", "color": "00aa11"}
    assert len(saved["requested_reviewers"]) == 10
    # No additions, deletions or review state are ever stored.
    assert not {"additions", "deletions", "review_decision", "mergeable"} & set(saved)
    # A hostile label past the bound is dropped; one within bound keeps its text but loses a bad colour.
    api["labels"] = [{"name": HOSTILE, "color": "zzzzzz"}]
    saved = node.reconcile_repo(reader(tmp_path, provider), now=CI_NOW)["repositories"][0]["pulls"][0]
    assert saved["labels"] == [{"name": HOSTILE, "color": None}]


def test_old_snapshots_without_optional_fields_validate_and_store(db):
    clean = ci.validate_snapshot(observation(pulls=[pull(5, OLD)]))
    assert clean["pulls"] == [{"number": 5, "head_sha": OLD, "updated_at": CI_NOW}]
    put(db, repositories=[observation(pulls=[pull(5, OLD)])])
    row = ci.load_view(db, now=CI_NOW)["repositories"][0]["pulls"][0]
    assert (
        "title" not in row
        and prs.collect({"repositories": [dict(ci.load_view(db, now=CI_NOW)["repositories"][0])]}, PAGE_NOW)["rows"][0][
            "title"
        ]
        is None
    )


def test_invalid_optional_fields_are_dropped_not_defaulted():
    clean = ci.validate_snapshot(
        observation(
            pulls=[
                pull(
                    5,
                    OLD,
                    title=7,
                    author={"login": "x"},
                    draft="yes",
                    author_bot=1,
                    labels="nope",
                    requested_reviewers=[1, "ok-user", "bad user", "ok-user"],
                    created_at="yesterday",
                )
            ]
        )
    )["pulls"][0]
    assert set(clean) == {"number", "head_sha", "updated_at", "requested_reviewers"}
    assert clean["requested_reviewers"] == ["ok-user"]


def test_relay_pull_request_event_keeps_stored_optional_fields(db):
    put(
        db,
        repositories=[observation(pulls=[pull(5, OLD, title="Keep me", labels=[{"name": "go", "color": "00ADD8"}])])],
    )
    event = dict(
        repo="example/public",
        event="pull_request",
        pr_number=5,
        head_sha=HEAD,
        status="unknown",
        updated_at="2026-10-21T12:05:00Z",
        action="synchronize",
    )
    put(db, events=[event])
    row = ci.load_view(db, now=CI_NOW)["repositories"][0]["pulls"][0]
    assert row["head_sha"] == HEAD and row["title"] == "Keep me" and row["labels"][0]["name"] == "go"
    event.update(action="closed", updated_at="2026-10-21T12:06:00Z")
    put(db, events=[event])
    assert ci.load_view(db, now=CI_NOW)["repositories"][0]["pulls"] == []


def test_route_lists_pull_requests_and_hides_private_repositories_from_browser_sessions(tmp_path):
    with _hub(tmp_path) as (hub, db_path):
        conn = fleet_hub.open_db(db_path)
        try:
            put(
                conn,
                repositories=[
                    observation(pulls=[pull(5, OLD, title="Public change")]),
                    observation(
                        repo="example/secret", visibility="private", pulls=[pull(8, OLD, title="Private change")]
                    ),
                ],
            )
        finally:
            conn.close()
        assert _request(hub, "/deck/pulls")[0] == 401
        status, _, admin = _request(hub, "/deck/pulls", headers=_bearer())
        assert status == 200 and "Public change" in admin and "Private change" in admin
        status, _, browser = _request(hub, "/deck/pulls", headers=_cookie(hub))
        assert status == 200 and "Public change" in browser and "Private change" not in browser
        assert "<script nonce=" in browser and "data-pr-root" in browser
        # The Projects page no longer carries the list, but is still guarded and still renders.
        assert _request(hub, "/deck/projects")[0] == 401
        status, _, projects = _request(hub, "/deck/projects", headers=_bearer())
        assert status == 200 and "Public change" not in projects and "data-pr-root" not in projects
        assert "/deck/pulls" in projects
