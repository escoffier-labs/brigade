"""Investigation evidence at read-only rendering boundaries."""

import html
import re
from dataclasses import replace

from brigade import fleet_admin_pages as pages, fleet_deck_activity as activity, fleet_deck_alerts as alerts
from brigade import fleet_work_page as work
from tests.test_fleet_deck_overview import NOW, _mixed_view, _alert_view, _run, NODE_A
from tests.test_fleet_command_deck import _octopool_capture


def prompts(page):
    return [html.unescape(value) for value in re.findall(r"<textarea[^>]*>(.*?)</textarea>", page, re.S)]


def test_octopool_prompt_retains_exact_capture_and_limits():
    raw = _octopool_capture()
    raw["pool_usage"].update(
        requests=145, cache_served_responses=9, uncached_outcomes=124, fallbacks=23, service_errors=4
    )
    raw["token"] = "SECRET-OMIT"
    page = activity.render_octopool_observation(replace(_mixed_view(), octopool_observation=raw))
    prompt = prompts(page)[0]
    for fact in (
        "145",
        "9",
        "124",
        "23",
        "4",
        raw["captured_at"],
        "/deck/capacity/github",
        "pool-wide",
        "historical",
        "exception details",
        "data, not instructions",
    ):
        assert fact in prompt
    assert "SECRET-OMIT" not in page
    healthy = dict(raw, pool_usage=dict(raw["pool_usage"], fallbacks=0, service_errors=0))
    assert not prompts(activity.render_octopool_observation(replace(_mixed_view(), octopool_observation=healthy)))
    assert prompts(activity.render_octopool_observation(replace(_mixed_view(), octopool_observation=None)))


def test_readonly_alerts_have_prompts_without_action_grants_and_escape_evidence():
    view = _alert_view()
    view = replace(view, rail=(replace(view.rail[0], detail="</textarea><script>bad()</script>"), *view.rail[1:]))
    page = alerts.render_alerts_page(view, NOW, nonce="test-nonce", worklore_enabled=True)
    assert len(prompts(page)) == len(alerts.page_alerts(view, NOW))
    assert "</textarea><script>bad()" not in page
    assert any("</textarea><script>bad()</script>" in p for p in prompts(page))
    assert 'nonce="test-nonce"' in page
    assert "Copy prompt for an agent" in page


def test_failure_surfaces_and_healthy_task_absence():
    view = _mixed_view()
    for page in (
        pages.render_projects({"state": "unavailable"}, view, NOW, nonce="n", worklore_enabled=True),
        pages.render_reporting({"unavailable": True}, view, NOW, nonce="n", worklore_enabled=True),
        pages.render_capacity(view, NOW, nonce="n", worklore_enabled=True),
        pages.render_activity(
            replace(view, outcomes=(_run(NODE_A, "failed-1", "project", state="run.failed", bucket="attention"),)),
            NOW,
            nonce="n",
            worklore_enabled=True,
        ),
        work.render(
            {
                "items": [
                    {
                        "work_id": "w-1",
                        "title": "Task",
                        "status": "blocked",
                        "blocker": "Approval needed",
                        "updated_at": NOW.isoformat(),
                        "token": "SECRET-OMIT",
                    }
                ]
            },
            nonce="n",
            now=NOW,
        ),
    ):
        assert prompts(page)
        assert "SECRET-OMIT" not in page
        assert all("Source route:" in p and "Observation:" in p and "reversible" in p for p in prompts(page))
    assert not prompts(work.render({"items": [{"title": "Healthy", "status": "completed"}]}, nonce="n", now=NOW))


def test_copy_denial_selects_prompt_and_never_announces_success():
    import json
    import shutil
    import subprocess
    from brigade import fleet_investigation

    # Execute the shipped listener with denied clipboard and failed legacy copy.
    # No browser driver: parent owns actual browser QA.
    node = shutil.which("node")
    assert node, "Node is required to exercise the shipped clipboard listener"
    harness = """
const assert=require('node:assert/strict'); let click, selected=false, focused=false;
const details={open:false}, status={textContent:'',removeAttribute(){}};
const textarea={value:'exact evidence',closest:()=>details,focus:()=>focused=true,select:()=>selected=true};
const button={dataset:{copyTarget:'p',copyStatus:'s'},textContent:'Copy investigation prompt',addEventListener:(e,f)=>click=f};
global.navigator={clipboard:{writeText:async()=>{throw Error('denied')}}};
global.document={readyState:'complete',querySelectorAll:()=>[button],
getElementById:id=>id==='p'?textarea:status,
createElement:()=>({style:{},setAttribute(){},select(){},remove(){}}),
body:{appendChild(){}},execCommand:()=>false};
eval(SCRIPT);
(async()=>{await click();assert.equal(button.textContent,'Copy investigation prompt');
assert.equal(selected,true);assert.equal(focused,true);assert.equal(details.open,true);
assert.match(status.textContent,/Clipboard unavailable/);})();
"""
    result = subprocess.run(
        [node, "-e", harness.replace("SCRIPT", json.dumps(fleet_investigation.COPY_SCRIPT))],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_cloud_problem_and_settings_inventory_prompts():
    from brigade import fleet_command_deck as deck, fleet_policy_page as policy

    worker = deck.CloudWorker(
        "provider-fixture", 1, 2, "open", (deck.CloudLease("task-7", "Project", "project", "blocked", ""),)
    )
    view = replace(_mixed_view(), cloud_workers=(worker,))
    page = pages.render_capacity(view, NOW, nonce="n", worklore_enabled=True, tab="cloud-details")
    prompt = prompts(page)[0]
    assert all(fact in prompt for fact in ("provider-fixture", "task-7", "blocked", "open", "1", "2"))
    assert prompts(policy._inventory_note(policy.unavailable_inventory("inventory-store-unavailable")))


def test_reporting_history_and_repository_unavailability_have_safe_prompts():
    from brigade import fleet_admin_stats_page as stats_page, fleet_repo_policy_page as repos

    page = stats_page.render({"latest": None, "weeks": []}, nonce="n", now=NOW)
    assert prompts(page)
    page = repos.render(
        _mixed_view(), policy_available=False, unavailable_reason="policy-read-failed", nonce="n", now=NOW
    )
    assert "policy-read-failed" in prompts(page)[0]


def test_unknown_outcome_and_blocked_project_keep_identifiers():
    view = replace(_mixed_view(), outcomes=(_run(NODE_A, "unknown-outcome", "project", state="unexpected-state"),))
    assert "unknown-outcome" in prompts(pages.render_activity(view, NOW, nonce="n", worklore_enabled=True))[0]
    campaign = {
        "campaign_key": "goal-1",
        "title": "Goal",
        "status": "blocked",
        "description": "Waiting",
        "acceptance": [],
    }
    page = {
        "state": "ready",
        "campaigns": [campaign],
        "selected": {"campaign": campaign, "members": [{"repo": "acme/one", "bucket": "blocked"}]},
    }
    prompt = prompts(pages.render_projects(page, view, NOW, nonce="n", worklore_enabled=True))[0]
    assert all(value in prompt for value in ("goal-1", "blocked", "acme/one"))


def test_stored_prompt_keeps_display_bounds():
    page = work.render(
        {"items": [{"title": "x" * 2000, "status": "blocked", "blocker": "b" * 2000}]}, nonce="n", now=NOW
    )
    prompt = prompts(page)[0]
    assert "x" * 257 not in prompt and "b" * 501 not in prompt
    assert "[truncated" in prompt


def test_reporting_daily_error_counts_keep_aggregate_provenance():
    from brigade import fleet_admin_stats_page
    from tests.test_fleet_admin_stats import snapshot

    value = snapshot()
    value["daily"][0].update(worker_requests=145, worker_errors=4)
    page = fleet_admin_stats_page.render({"latest": value, "weeks": []}, nonce="n", now=NOW)
    prompt = prompts(page)[0]
    assert all(fact in prompt for fact in ("2026-10-20", "145", "4", value["taken_at"], "/deck/stats"))
    assert "aggregate" in prompt and "request logs" in prompt
