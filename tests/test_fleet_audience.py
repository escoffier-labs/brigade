"""Admin versus agent information split: markup, defaults and the shared script."""

from dataclasses import replace

from brigade import fleet_admin_explorer as explorer, fleet_admin_pages as pages, fleet_deck_overview as overview
from tests.fleet_investigation_assertions import investigation_markup
from tests.fleet_script_dom import run_script
from tests.test_fleet_deck_overview import NOW, NODE_A, _mixed_view, _run
from tests.test_ui_theme_integration import Markup


def _agent_text(page):
    parsed = Markup(page)
    return " ".join(parsed.text[i] for i, (_, attrs) in enumerate(parsed.tags) if attrs.get("data-audience") == "agent")


def _outside_agent(page):
    """Text of every element that has no agent-data ancestor or self."""
    parsed = Markup(page)
    agent = {i for i, (_, attrs) in enumerate(parsed.tags) if attrs.get("data-audience") == "agent"}
    return " ".join(
        parsed.text[i]
        for i, (tag, _) in enumerate(parsed.tags)
        if i not in agent and not agent.intersection(parsed.ancestors[i]) and tag in {"dd", "p", "h2", "h3"}
    )


def test_toggle_is_rendered_hidden_with_both_buttons_and_pressed_default():
    page = pages.render_activity(_mixed_view(), NOW, nonce="n", worklore_enabled=True)
    parsed = Markup(page)
    group = next(a for t, a in parsed.tags if "audience-toggle" in a.get("class", "").split())
    assert "hidden" in group  # revealed by script, so no-JS keeps everything visible
    buttons = [a for t, a in parsed.tags if t == "button" and "data-audience-set" in a]
    assert [b["data-audience-set"] for b in buttons] == ["admin", "agent"]
    assert [b["aria-pressed"] for b in buttons] == ["true", "false"]
    assert "fleet-audience" in page and 'html[data-audience-mode="admin"] [data-audience="agent"]' in page


def test_activity_keeps_ids_and_codes_as_agent_data_and_humans_see_owner_and_model():
    run = _run(NODE_A, "run-xyz-123", "example/tool", state="run.failed")
    page = pages.render_activity(replace(_mixed_view(), outcomes=(run,)), NOW, nonce="n", worklore_enabled=True)
    agent = _agent_text(page)
    assert "run-xyz-123" in agent and NODE_A in agent and "run.failed" in agent
    human = _outside_agent(page)
    assert "run-xyz-123" not in human and NODE_A not in human
    assert "Open run history" in page


def test_investigation_copy_button_is_outside_agent_data_and_prompt_is_inside():
    run = _run(NODE_A, "run-xyz-123", "example/tool", state="run.failed")
    page = pages.render_activity(replace(_mixed_view(), outcomes=(run,)), NOW, nonce="n", worklore_enabled=True)
    parsed, roots = investigation_markup(page)
    button = next(i for i, (t, a) in enumerate(parsed.tags) if t == "button" and "data-copy-target" in a)
    textarea = next(i for i, (t, _) in enumerate(parsed.tags) if t == "textarea")
    agent = {i for i, (_, a) in enumerate(parsed.tags) if a.get("data-audience") == "agent"}
    assert parsed.text[button] == "Copy prompt for an agent"
    assert not agent.intersection(parsed.ancestors[button]) and button not in agent
    assert agent.intersection(parsed.ancestors[textarea])


def test_capacity_machine_details_split_human_and_agent_fields():
    page = pages.render_capacity(_mixed_view(), NOW, nonce="n", worklore_enabled=True)
    agent = _agent_text(page)
    assert NODE_A in agent and "Usable for selected work: unknown" in agent
    human = _outside_agent(page)
    assert NODE_A not in human and "Usable for selected work" not in human


def test_policy_provider_snapshot_is_agent_data():
    from brigade import fleet_policy_page as policy

    note = policy._inventory_note(policy.unavailable_inventory("inventory-store-unavailable"))
    assert "inventory-store-unavailable" in note  # unavailability stays visible to admins
    usable = policy.Inventory(
        providers={"provider-a": {"state": "fresh", "scope": "native CLI listing"}},
        source="fixture-snapshot",
        captured_at="2026-09-05T11:00:00Z",
    )
    snapshot = policy._inventory_note(usable)
    assert "fixture-snapshot" in _agent_text(snapshot) and "provider-a" in _agent_text(snapshot)
    assert "fixture-snapshot" not in _outside_agent(snapshot)


def test_overview_work_card_drops_duplicate_reported_pill_and_refresh_link():
    page = overview.render_overview(_mixed_view(), NOW, worklore_enabled=True)
    assert ">Refresh<" not in page and 'class="tag">Reported<' not in page
    assert 'class="pill' in page


def test_explorer_facts_escape_values_and_skip_empty():
    block = explorer.facts({"A": "<b>x</b>", "B": "", "C": None}, audience="agent")
    assert "&lt;b&gt;x&lt;/b&gt;" in block and "<b>x" not in block
    assert block.count("<dt>") == 1 and 'data-audience="agent"' in block


def test_shell_script_restores_stored_mode_and_toggle_updates_pressed_state():
    page = explorer.audience_toggle()
    run_script(
        page,
        explorer.SHELL_SCRIPT,
        """
const group=dom.querySelector('.audience-toggle');
assert.equal(group.hidden,false);
assert.equal(htmlRoot.attrs['data-audience-mode'],'admin');
const buttons=group.querySelectorAll('button');
buttons[1].handlers.click();
assert.equal(htmlRoot.attrs['data-audience-mode'],'agent');
assert.equal(buttons[1].attrs['aria-pressed'],'true');assert.equal(buttons[0].attrs['aria-pressed'],'false');
assert.equal(localStorage.getItem('fleet-audience'),'agent');
buttons[0].handlers.click();
assert.equal(htmlRoot.attrs['data-audience-mode'],'admin');
""",
    )
    run_script(
        page,
        "localStorage.setItem('fleet-audience','agent');" + explorer.SHELL_SCRIPT,
        "assert.equal(htmlRoot.attrs['data-audience-mode'],'agent');",
    )


def test_no_underlined_links_in_main_content_css():
    assert ".shell-main a, .shell-sidebar a { text-decoration:none; }" in explorer.SHELL_CSS
