"""Tests for the instruction-budget report, alert lines and the `instructions` tend stage."""

from __future__ import annotations

import json
import os
from datetime import date

import pytest

from brigade import memory_care_tend
from brigade.instruction_inventory import FileOverride, InstructionsConfig, discover, find
from brigade.memory_care_instructions import (
    alert_lines,
    apply,
    config_change_line,
    file_row,
    safe_text,
    severity,
    state_of,
)
from tests._home import set_home
from tests._instruction_fixtures import (
    field_workspace,
    finding_2_tree,
    openclaw_home,
    refuse_subprocess,
    sized,
)

TODAY = date(2026, 11, 13)
NEXT = 'next="brigade memory care tend --stage instructions --dry-run --json"'


def _rows(ws, home, env, config=None):
    payload = apply(ws, config or InstructionsConfig(), today=TODAY, dry_run=True, home=home, environ=env)
    return payload, {row["path"]: row for row in payload["files"]}


def test_finding_1_report_row_matches_the_design_example(tmp_path):
    ws, home, env = field_workspace(tmp_path)
    row = _rows(ws, home, env)[1]["USER.md"]
    assert {
        k: row[k]
        for k in (
            "loaders",
            "class",
            "bytes_before",
            "limit",
            "limit_source",
            "target_budget",
            "act_at",
            "trim_to",
            "state",
            "reason",
        )
    } == {
        "loaders": ["openclaw:main"],
        "class": "append",
        "bytes_before": 8241,
        "limit": 4000,
        "limit_source": "loader:openclaw:user-md-fixed-cap",
        "target_budget": 8000,
        "act_at": 3400,
        "trim_to": 2800,
        "state": "could-not-trim",
        "reason": "report-only",
    }
    assert row["bytes_after"] == row["bytes_before"]  # slice 1 moves nothing


@pytest.mark.parametrize(
    ("size", "state", "reason"),
    [
        (3400, "within", None),
        (3401, "over-act-at", None),
        (4000, "over-act-at", None),
        (4001, "could-not-trim", "report-only"),
    ],
)
def test_states_at_the_boundaries(tmp_path, size, state, reason):
    ws, home, env = field_workspace(tmp_path)
    sized(ws / "USER.md", size)
    user = find(discover(ws, InstructionsConfig(), home=home, environ=env), "USER.md")
    assert state_of(user) == (state, reason)


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_finding_2_alert_names_the_real_file(tmp_path):
    home, env = openclaw_home(tmp_path, None)
    finding_2_tree(home)
    ws = tmp_path / "ws"
    ws.mkdir()
    payload, _ = _rows(ws, home, env)
    assert payload["alerts"] == [
        f"instruction_budget: could-not-trim ~/.codex/AGENTS.md 24500/20000 reason=report-only {NEXT}"
    ]


def test_total_over_cap_raises_its_own_alert_after_the_file_alerts(tmp_path):
    ws, home, env = field_workspace(tmp_path, lean=True)  # lean shares the workspace: total cap 6000
    payload, _ = _rows(ws, home, env)
    assert payload["alerts"][-1] == f"instruction_budget: total-over-cap openclaw:lean 20441/6000 {NEXT}"
    assert all(line.startswith("instruction_budget: could-not-trim ") for line in payload["alerts"][:-1])
    assert {"loader": "openclaw:main", "bytes": 20441, "cap": 120000, "state": "within"} in payload["totals"]


def test_pending_pointers_list_overflow_cards_the_file_does_not_name(tmp_path):
    ws, home, env = field_workspace(tmp_path)
    for name in ("user-md--overflow-preference.md", "user-md--overflow-setup.md"):
        sized(ws / "memory/cards/instructions" / name, 20, head="---\ntitle: t\n---\n")
    with (ws / "USER.md").open("a", encoding="utf-8") as handle:
        handle.write("\nSee memory/cards/instructions/user-md--overflow-setup.md\n")
    assert _rows(ws, home, env)[1]["USER.md"]["pending_pointers"] == [
        "memory/cards/instructions/user-md--overflow-preference.md"
    ]


def test_tend_runs_the_stage_and_saves_it(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    refuse_subprocess(monkeypatch)
    before = {path: path.read_bytes() for path in (ws / "USER.md", ws / "AGENTS.md")}
    payload = memory_care_tend.run(ws, stages=["instructions"], today=TODAY)
    user = next(r for r in payload["stages"]["instructions"]["files"] if r["path"] == "USER.md")
    assert (user["limit"], user["state"]) == (4000, "could-not-trim")
    saved = json.loads((ws / memory_care_tend.STATE_REL).read_text())
    assert saved["stages"]["instructions"]["alerts"] == payload["stages"]["instructions"]["alerts"]
    assert {path: path.read_bytes() for path in before} == before  # nothing written


def test_disabled_stage_reports_nothing(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    (ws / ".brigade").mkdir()
    (ws / ".brigade" / "memory-care.toml").write_text("[instructions]\nenabled = false\n")
    assert memory_care_tend.run(ws, stages=["instructions"], dry_run=True)["stages"]["instructions"] == {
        "enabled": False
    }


def test_invalid_table_fails_the_stage_like_tend_config(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    (ws / ".brigade").mkdir()
    (ws / ".brigade" / "memory-care.toml").write_text('[instructions]\nact_at_percent = "x"\n')
    with pytest.raises(ValueError, match="invalid instructions.act_at_percent"):
        memory_care_tend.run(ws, stages=["instructions"], dry_run=True)


# --- additions: nothing a configuration says is silent, and no reader blocks ----


def test_a_default_run_reports_an_invalid_table_and_still_finishes_the_other_stages(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    (ws / ".brigade").mkdir()
    (ws / ".brigade" / "memory-care.toml").write_text('[instructions]\nact_at_percent = "x"\n')
    payload = memory_care_tend.run(ws, dry_run=True)  # no stage named: the full default set
    stage = payload["stages"]["instructions"]
    assert stage["error"] == "config-invalid"
    assert stage["alerts"] == ['instruction_budget: config-invalid detail="invalid instructions.act_at_percent"']
    assert set(payload["stages"]) == set(memory_care_tend.STAGES)


def test_an_excluded_file_is_marked_skipped_by_alerts_and_still_totalled(tmp_path):
    ws, home, env = field_workspace(tmp_path)
    payload, rows = _rows(ws, home, env, InstructionsConfig(exclude=("USER.md",)))
    user = rows["USER.md"]
    assert (user["excluded"], user["exclusion_source"], user["class"]) == (
        True,
        "config:instructions.exclude",
        "report",
    )
    assert user["state"] == "could-not-trim"  # still measured
    assert not any("USER.md" in line and "could-not-trim" in line for line in payload["alerts"])
    assert {"loader": "openclaw:main", "bytes": 20441, "cap": 120000, "state": "within"} in payload["totals"]
    assert payload["alerts"][-1].startswith(
        "instruction_budget: config-changed overrides-rejected=0 limits-clamped=0 excluded=1"
    )


def test_rejected_overrides_and_clamped_limits_are_in_the_payload_and_the_config_line(tmp_path):
    ws, home, env = field_workspace(tmp_path)
    outside = sized(tmp_path / "outside" / "notes.md", 300)
    config = InstructionsConfig(
        files=((str(outside), FileOverride("append", 100)), ("AGENTS.md", FileOverride(None, 50000)))
    )
    payload, rows = _rows(ws, home, env, config)
    assert payload["override_notes"] == [
        {"name": "<external>/notes.md", "reason": "absolute-path-not-discovered", "action": "forced-report"}
    ]
    notes = rows["<external>/notes.md"]
    assert (notes["override_reason"], notes["requested_class"], notes["requested_limit_bytes"]) == (
        "absolute-path-not-discovered",
        "append",
        100,
    )
    assert (notes["class"], notes["class_source"]) == ("report", "rejected-override")
    agents = rows["AGENTS.md"]
    assert (agents["limit_clamped"], agents["requested_limit_bytes"], agents["limit"]) == (True, 50000, 12000)
    assert payload["alerts"][-1].startswith("instruction_budget: config-changed overrides-rejected=1 limits-clamped=1")


def test_keys_that_move_every_limit_are_named(tmp_path):
    ws, home, env = field_workspace(tmp_path)
    payload, _ = _rows(ws, home, env, InstructionsConfig(act_at_percent=60, trim_to_percent=40))
    assert payload["config_notes"] == ["act_at_percent", "trim_to_percent"]
    inventory = discover(ws, InstructionsConfig(act_at_percent=60, trim_to_percent=40), home=home, environ=env)
    assert config_change_line(inventory) == (
        "instruction_budget: config-changed overrides-rejected=0 limits-clamped=0 excluded=0 "
        f"config=act_at_percent,trim_to_percent {NEXT}"
    )


def test_a_clean_configuration_has_no_config_line(tmp_path):
    ws, home, env = field_workspace(tmp_path)
    inventory = discover(ws, InstructionsConfig(), home=home, environ=env)
    assert config_change_line(inventory) is None
    assert not any("config-changed" in line for line in alert_lines(inventory))


def test_an_unreadable_openclaw_config_still_reports_and_says_why(tmp_path):
    ws, home, env = field_workspace(tmp_path, '{ // comment\n "agents": {},\n}')
    payload, rows = _rows(ws, home, env)
    assert payload["loader_reason"] == "config-not-strict-json"
    assert rows["USER.md"]["limit"] == 8000  # falls back to the target budget
    assert payload["totals"] == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_the_report_shows_display_paths_only(tmp_path):
    home, env = openclaw_home(tmp_path, None)
    real = finding_2_tree(home)
    outside = sized(tmp_path / "elsewhere" / "AGENTS.md", 200)
    link = tmp_path / "ws" / "AGENTS.md"
    sized(link.parent / "USER.md", 100)
    link.symlink_to(outside)
    payload, _ = _rows(link.parent, home, env)
    text = json.dumps(payload)
    for private in (str(tmp_path), tmp_path.as_posix(), str(real), str(home)):
        assert private not in text
    assert "<external>/AGENTS.md" in text


def test_safe_text_flattens_control_characters_and_shortens_known_directories(tmp_path):
    home, target = tmp_path / "h", tmp_path / "t"
    forged = "good\ninstruction_budget: could-not-trim fake"
    assert "\n" not in safe_text(forged)
    assert safe_text(f"bad key {home / 'x'} in {target / 'y'}", home=home, target=target).startswith("bad key ~")
    assert str(tmp_path) not in safe_text(f"{home}/a {target}/b", home=home, target=target)
    assert len(safe_text("x" * 5000)) <= 200


def test_severity_fails_only_a_workspace_file_the_target_owns(tmp_path):
    ws, home, env = field_workspace(tmp_path)
    sized(ws / "AGENTS.md", 100)
    inventory = discover(ws, InstructionsConfig(), home=home, environ=env)
    assert severity(find(inventory, "USER.md"), ws) == "fail"
    assert severity(find(inventory, "AGENTS.md"), ws) == "ok"
    sized(ws / "USER.md", 3500)
    inventory = discover(ws, InstructionsConfig(), home=home, environ=env)
    assert severity(find(inventory, "USER.md"), ws) == "warn"
    row = file_row(find(inventory, "USER.md"), target=ws)
    assert "real_path" not in row and str(tmp_path) not in json.dumps(row)
