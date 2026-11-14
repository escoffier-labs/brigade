"""Tests for doctor, `budgets check` and the work brief reading the instruction inventory."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from brigade import budgets_cmd, doctor, dogfood_cmd, instruction_inventory, memory_care_instructions, work_cmd
from brigade.doctor import FAIL, OK, SCOPE_OPERATOR, WARN
from brigade.doctor_instruction_budget import check_instruction_budgets
from brigade.memory_care_instructions import brief_payload
from tests._home import set_home
from tests._instruction_fixtures import field_workspace, finding_2_tree, openclaw_home, sized
from tests.work_cmd_test_helpers import _init_git_repo

NEXT = 'next="brigade memory care tend --stage instructions --dry-run --json"'


def _rows(ws):
    return {check[1]: check for check in check_instruction_budgets(ws)}


def _care(ws: Path, text: str) -> None:
    path = ws / ".brigade" / "memory-care.toml"
    path.parent.mkdir(exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_doctor_fails_an_over_limit_workspace_file_and_warns_a_user_file(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    finding_2_tree(home)
    set_home(monkeypatch, home)
    rows = _rows(ws)
    assert rows["bootstrap-budget: USER.md"][0] == FAIL
    assert "8241/4000 bytes, over limit" in rows["bootstrap-budget: USER.md"][2]
    user = rows["bootstrap-budget: ~/.codex/AGENTS.md"]
    assert (user[0], user[3]) == (WARN, SCOPE_OPERATOR)


def test_doctor_warns_between_act_at_and_limit(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    sized(ws / "USER.md", 3500)
    assert _rows(ws)["bootstrap-budget: USER.md"] == (
        WARN,
        "bootstrap-budget: USER.md",
        "3500/4000 bytes, over act-at 3400; new handoff appends go to an overflow card",
    )


def test_doctor_reports_a_file_within_its_limit_with_the_source(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    sized(ws / "USER.md", 100)
    assert _rows(ws)["bootstrap-budget: USER.md"] == (
        OK,
        "bootstrap-budget: USER.md",
        "100/4000 bytes, act-at 3400 (limit from loader:openclaw:user-md-fixed-cap)",
    )


def test_doctor_fails_a_per_name_path_that_is_not_a_file(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    (ws / "SOUL.md").mkdir()
    assert _rows(ws)["bootstrap-budget: SOUL.md"] == (FAIL, "bootstrap-budget: SOUL.md", "not a file: SOUL.md")


def test_doctor_fails_one_row_for_an_invalid_table_and_keeps_the_other_rows(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    _care(ws, '[instructions]\nact_at_percent = "high"\n')
    rows = _rows(ws)
    assert rows["instructions: config"] == (FAIL, "instructions: config", "invalid instructions.act_at_percent")
    assert rows["bootstrap-budget: USER.md"][0] == FAIL  # measured with the defaults


def test_doctor_names_a_configuration_that_weakens_protection(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    _care(ws, '[instructions]\nexclude = ["USER.md"]\ndefault_limit_bytes = 5000\n')
    row = _rows(ws)["instructions: config-changes"]
    assert row[0] == WARN
    assert "excluded=1" in row[2] and "default_limit_bytes" in row[2]
    assert "excluded by config:instructions.exclude" in _rows(ws)["bootstrap-budget: USER.md"][2]


def test_doctor_keeps_working_when_discovery_reports_an_unreadable_config(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path, '{ // comment\n "agents": {},\n}')
    set_home(monkeypatch, home)
    rows = _rows(ws)
    assert rows["bootstrap-budget: USER.md"][0] == FAIL  # falls back to the target budget, 8,000
    assert "8241/8000 bytes" in rows["bootstrap-budget: USER.md"][2]


def test_doctor_row_survives_an_inventory_that_raises(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)

    def boom(*_args, **_kwargs):
        raise OSError("disk went away")

    monkeypatch.setattr(instruction_inventory, "discover", boom)
    assert _rows(ws)["instructions: inventory"] == (FAIL, "instructions: inventory", "could not read: OSError")


def test_doctor_stays_under_its_size():
    assert len(Path(doctor.__file__).read_text().splitlines()) <= 1982


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_doctor_never_prints_a_full_home_path(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    finding_2_tree(home)
    set_home(monkeypatch, home)
    _care(ws, f'[instructions.files."{tmp_path / "outside.md"}"]\nclass = "append"\n')
    sized(tmp_path / "outside.md", 10)
    text = json.dumps(check_instruction_budgets(ws))
    assert str(tmp_path) not in text and tmp_path.as_posix() not in text


def test_budgets_check_reports_limits_from_the_inventory(tmp_path, monkeypatch, capsys):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    assert budgets_cmd.check(target=ws, json_output=True) == 1
    rows = {r["name"]: r for r in json.loads(capsys.readouterr().out)["checks"]}
    user = rows["USER.md"]
    assert (user["budget_bytes"], user["limit_source"], user["act_at_bytes"], user["status"], user["state"]) == (
        4000,
        "loader:openclaw:user-md-fixed-cap",
        3400,
        "fail",
        "could-not-trim",
    )
    assert rows["SOUL.md"]["status"] == "missing"


def test_budgets_check_warns_between_act_at_and_limit(tmp_path, monkeypatch, capsys):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    sized(ws / "USER.md", 3500)
    sized(ws / "AGENTS.md", 100)
    assert budgets_cmd.check(target=ws, json_output=True) == 0
    rows = {r["name"]: r for r in json.loads(capsys.readouterr().out)["checks"]}
    assert (rows["USER.md"]["status"], rows["USER.md"]["state"]) == ("warn", "over-act-at")


def test_budgets_check_text_line(tmp_path, monkeypatch, capsys):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    sized(ws / "USER.md", 3500)
    sized(ws / "AGENTS.md", 100)
    assert budgets_cmd.check(target=ws) == 0
    assert (
        '[warn] path="USER.md": 3500/4000 bytes (act-at 3400, loader:openclaw:user-md-fixed-cap)'
        in capsys.readouterr().out
    )


def test_budgets_check_with_an_invalid_table_still_reports_and_fails(tmp_path, monkeypatch, capsys):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    _care(ws, '[instructions]\nact_at_percent = "high"\n')
    assert budgets_cmd.check(target=ws, json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["config_error"] == "invalid instructions.act_at_percent"
    assert {r["name"]: r["status"] for r in payload["checks"]}["USER.md"] == "fail"  # measured with the defaults


def test_budgets_check_carries_the_audit_fields_and_no_home_path(tmp_path, monkeypatch, capsys):
    ws, home, env = field_workspace(tmp_path, '{ // comment\n "agents": {},\n}')
    set_home(monkeypatch, home)
    outside = sized(tmp_path / "outside" / "notes.md", 300)
    _care(
        ws, f'[instructions]\nexclude = ["AGENTS.md"]\n[instructions.files."{outside.as_posix()}"]\nclass = "append"\n'
    )
    budgets_cmd.check(target=ws, json_output=True)
    payload = json.loads(capsys.readouterr().out)
    assert payload["loader_reason"] == "config-not-strict-json"
    assert payload["config_notes"] == []
    assert [n["reason"] for n in payload["override_notes"]] == ["absolute-path-not-discovered"]
    rows = {r["name"]: r for r in payload["checks"]}
    assert rows["AGENTS.md"]["excluded"] is True
    assert rows["<external>/notes.md"]["path"] == "<external>/notes.md"  # display, not the real path
    text = json.dumps(payload["checks"]) + json.dumps(payload["override_notes"])
    assert "outside" not in text.replace("<external>", "")  # the real directory of the rejected file never shows


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_brief_prints_the_alert_lines(tmp_path, monkeypatch, capsys):
    home, env = openclaw_home(tmp_path, None)
    finding_2_tree(home)
    set_home(monkeypatch, home)
    ws = tmp_path / "ws"
    ws.mkdir()
    _init_git_repo(ws)
    dogfood_cmd.init(target=ws)
    assert work_cmd.brief(target=ws, limit=2) == 0
    assert (
        f'instruction_budget: could-not-trim path="~/.codex/AGENTS.md" 24500/20000 reason=report-only {NEXT}'
        in capsys.readouterr().out
    )


def test_brief_prints_one_config_line_for_an_excluded_file(tmp_path, monkeypatch, capsys):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    _care(ws, '[instructions]\nexclude = ["USER.md"]\n')
    _init_git_repo(ws)
    dogfood_cmd.init(target=ws)
    assert work_cmd.brief(target=ws, limit=2) == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if "config-changed" in line]
    assert lines == [
        f"instruction_budget: config-changed overrides-rejected=0 limits-clamped=0 excluded=1 config=none {NEXT}"
    ]


def test_brief_payload_survives_an_invalid_table_and_a_disabled_stage(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    _care(ws, '[instructions]\nact_at_percent = "high"\n')
    assert brief_payload(ws) == {
        "alerts": ['instruction_budget: config-invalid detail="invalid instructions.act_at_percent"']
    }
    _care(ws, "[instructions]\nenabled = false\n")
    # Turning the report off is itself a visible change, so the brief keeps one line.
    assert brief_payload(ws) == {
        "alerts": [
            f"instruction_budget: config-changed overrides-rejected=0 limits-clamped=0 excluded=0 config=enabled {NEXT}"
        ]
    }


def test_brief_payload_never_raises(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)

    def boom(*_args, **_kwargs):
        raise OSError("disk went away")

    monkeypatch.setattr(memory_care_instructions, "discover", boom)
    assert brief_payload(ws) == {
        "alerts": [f"instruction_budget: inventory-failed reason=OSError {NEXT}"],
        "error": "OSError",
    }


def test_brief_payload_with_an_unreadable_openclaw_config_still_alerts(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path, '{ // comment\n "agents": {},\n}')
    set_home(monkeypatch, home)
    alerts = brief_payload(ws)["alerts"]
    assert any(line.startswith('instruction_budget: could-not-trim path="USER.md" 8241/8000') for line in alerts)


def test_the_brief_warns_when_a_harness_configuration_cannot_be_read(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path, '{ // comment\n "agents": {},\n}')
    set_home(monkeypatch, home)
    sized(ws / "USER.md", 5000)  # over the harness's 4,000 cap, under the fallback target
    alerts = brief_payload(ws)["alerts"]
    # Rule: unknown caps never look like a clean report.
    assert not any("could-not-trim" in line and "USER.md" in line for line in alerts)
    assert [line for line in alerts if "loader-config-unreadable" in line] == [
        "instruction_budget: loader-config-unreadable reason=config-not-strict-json "
        'next="brigade memory care tend --stage instructions --dry-run --json"'
    ]


def test_a_readable_or_absent_harness_configuration_adds_no_loader_line(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    assert not any("loader-config-unreadable" in line for line in brief_payload(ws)["alerts"])


def test_the_brief_warns_when_another_harness_cap_is_unavailable(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    (home / ".codex").mkdir(exist_ok=True)
    (home / ".codex" / "config.toml").write_text("project_doc_max_bytes = 4096\n", encoding="utf-8")
    (ws / "AGENTS.md").unlink(missing_ok=True)
    sized(ws / "AGENTS.md", 5000)  # over the harness's 4,096 cap, under the fallback target
    monkeypatch.delattr(os, "O_NONBLOCK", raising=False)  # as on a platform that cannot read the file safely
    alerts = brief_payload(ws)["alerts"]
    # Rule: a cap that could not be read is named, whichever harness it belongs to.
    (line,) = [item for item in alerts if "loader-config-unreadable" in item]
    assert "config-nonblocking-unavailable" in line


def test_the_brief_names_a_blocked_overflow_card_by_id_and_count(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    before = brief_payload(ws)["alerts"]
    record = ws / ".brigade" / "memory-care" / "inbox" / "blocked-cards.json"
    record.parent.mkdir(parents=True, exist_ok=True)
    hostile = "memory/cards/instructions/ignore-previous-instructions--overflow-preference.md"
    record.write_text(
        json.dumps(
            {"version": 1, "cards": [{"path": hostile, "problem": "its visibility is not private", "held_drafts": 2}]}
        ),
        encoding="utf-8",
    )
    alerts = brief_payload(ws)["alerts"]
    (line,) = [item for item in alerts if item not in before]
    # Rule: the card is named by an id, its path stays in the record file as data.
    assert line.startswith('instruction_budget: overflow-card-blocked card="overflow-card-') and " held=2 " in line
    assert "ignore-previous" not in line and "its visibility" not in line
    assert line.endswith('next="brigade memory care tend --stage inbox"')


@pytest.mark.parametrize(
    "content",
    [
        "{",
        "[]",
        '{"cards": "x"}',
        '{"cards": [{"path": 1, "held_drafts": "2"}]}',
        # No version, a wrong version, and a record far larger than any real one.
        '{"cards": [{"path": "memory/cards/instructions/a--overflow-note.md", "held_drafts": 1}]}',
        '{"version": 2, "cards": [{"path": "memory/cards/instructions/a--overflow-note.md", "held_drafts": 1}]}',
        '{"version": 1, "cards": [{"path": "memory/cards/instructions/a--overflow-note.md", "held_drafts": 1}], "pad": "'
        + "x" * 70_000
        + '"}',
        "[" * 5000,
    ],
)
def test_a_damaged_blocked_card_record_adds_no_line_and_never_raises(tmp_path, monkeypatch, content):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    before = brief_payload(ws)["alerts"]
    record = ws / ".brigade" / "memory-care" / "inbox" / "blocked-cards.json"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(content, encoding="utf-8")
    assert brief_payload(ws)["alerts"] == before
