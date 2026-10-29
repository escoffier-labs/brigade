"""Exact fleet approval must not survive changed bytes or copy sets."""

import copy
import dataclasses

import pytest

from brigade import fleet_skills_review as review
from tests.test_fleet_skills_review import seeded, view_for


def bound_view(conn):
    view = view_for(conn)
    skills = []
    for skill in view.skills:
        copies = []
        for item in skill.copies:
            binding = {
                "schema_version": 1,
                "node": item.node_id,
                "workspace": "a" * 64,
                "skill_id": skill.skill_id,
                "harness": item.harness,
                "source_selector": item.update_selector,
                "installed": "b" * 64,
                "source": "c" * 64,
                "target": "c" * 64,
                "rollback": "d" * 64,
                "contract": None,
                "evaluation": None,
            }
            copies.append(dataclasses.replace(item, binding=binding))
        skills.append(dataclasses.replace(skill, copies=tuple(copies)))
    return dataclasses.replace(view, skills=tuple(skills))


def test_saved_approval_invalidated_by_bytes_or_new_copy(tmp_path):
    conn, _, _ = seeded(tmp_path)
    view = bound_view(conn)
    item = next(s for s in view.skills if s.can_update)
    review.save_decision(conn, view, item.skill_id, "update", {}, display_token=review.review_token(item))
    draft = review.load_draft(conn)
    assert review.build_plan(view, draft).count > 0
    changed = copy.deepcopy(view)
    changed_item = next(s for s in changed.skills if s.skill_id == item.skill_id)
    changed_item.copies[0].binding["target"] = "e" * 64
    assert review.build_plan(changed, draft).count == 0
    assert review.build_plan(changed, draft).blockers
    extra = dataclasses.replace(item.copies[0], node_id="new-node")
    changed_item = dataclasses.replace(item, copies=(*item.copies, extra))
    changed = dataclasses.replace(
        view, skills=tuple(changed_item if s.skill_id == item.skill_id else s for s in view.skills)
    )
    assert review.build_plan(changed, draft).count == 0


def test_stale_display_and_legacy_draft_cannot_approve(tmp_path):
    conn, _, _ = seeded(tmp_path)
    view = bound_view(conn)
    item = next(s for s in view.skills if s.can_update)
    with pytest.raises(review.DecisionError, match="review-stale"):
        review.save_decision(conn, view, item.skill_id, "update", {}, display_token="old")
    assert review.build_plan(view, {item.skill_id: {"default": "update"}}).count == 0
    legacy = view_for(conn)
    item = next(s for s in legacy.skills if s.can_update)
    with pytest.raises(review.DecisionError, match="provenance"):
        review.save_decision(conn, legacy, item.skill_id, "update", {}, display_token=review.review_token(item))


def test_stale_browser_post_cannot_approve_current_bytes(tmp_path):
    import re
    from brigade import fleet_hub
    from tests.test_fleet_skills_review import Hub, skill

    conn, path, _ = seeded(tmp_path, bindings=True)
    before = review.review_token(skill(view_for(conn), "sample-lint"))
    conn.close()
    with Hub(tmp_path, path) as hub:
        status, _, html = hub.request("GET", "/deck/skills", headers={"Authorization": f"Bearer {hub.token}"})
        assert status == 200 and before in html
        csrf = re.search(r'name="csrf" value="([^"]+)"', html).group(1)
        conn = fleet_hub.open_db(path)
        row = conn.execute("SELECT node_id, report FROM fleet_skill_reports LIMIT 1").fetchone()
        import json

        report = json.loads(row[1])
        for c in report["copies"]:
            if c["skill_id"] == "sample-lint":
                c["binding"]["target"] = "e" * 64
        review.store_report(conn, row[0], report)
        conn.close()
        fields = {"action": "save", "csrf": csrf, "skill": "sample-lint", "decision": "update", "display_token": before}
        assert hub.form(fields)[0] == 422
        conn = fleet_hub.open_db(path)
        assert review.load_draft(conn) == {}
        conn.close()


def test_disappeared_skill_is_review_stale(tmp_path):
    conn, _, _ = seeded(tmp_path)
    view = bound_view(conn)
    item = next(s for s in view.skills if s.can_update)
    review.save_decision(conn, view, item.skill_id, "update", {}, display_token=review.review_token(item))
    plan = review.build_plan(dataclasses.replace(view, skills=()), review.load_draft(conn))
    assert plan.count == 0
    assert any("review-stale" in b for b in plan.blockers)
