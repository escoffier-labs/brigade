"""Contract tests for the forward-plan registry skill (#779).

Ships as a bundled registry skill (memory-care pack pattern), never a CLI
subcommand. Mechanical pieces: SKILL.md / skill.json / CHANGELOG.md plus
plan.template.json validity.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from brigade import agent_skill_format, skills_cmd
from brigade.templates import template_root

FORWARD_PLAN = "forward-plan"
PERMITTED_BASE_TOOLS = frozenset({"Read", "Grep", "Write"})
SCOPED_BRIGADE_BASH_RE = re.compile(r"^Bash\(brigade .+:\*\)$")
EXPECTED_SCOPED_BASH = frozenset(
    {
        "Bash(brigade work ready:*)",
        "Bash(brigade outcome rank:*)",
        "Bash(brigade work task:*)",
        "Bash(brigade work plans:*)",
        "Bash(brigade code:*)",
    }
)
REQUIRED_TEMPLATE_KEYS = frozenset(
    {
        "kind",
        "plan_id",
        "status",
        "inputs",
        "nodes",
        "proposed_edges",
        "graphtrail",
        "confirmation",
        "next_command",
        "receipt_paths",
    }
)


def _bundled_skill_dir(skill_id: str = FORWARD_PLAN) -> Path:
    return template_root() / "skills" / skill_id


def _skill_text() -> str:
    path = _bundled_skill_dir() / "SKILL.md"
    assert path.is_file(), "missing bundled SKILL.md for forward-plan"
    return path.read_text(encoding="utf-8")


def _skill_metadata() -> dict:
    path = _bundled_skill_dir() / "skill.json"
    assert path.is_file(), "missing bundled skill.json for forward-plan"
    return json.loads(path.read_text(encoding="utf-8"))


def _tool_is_permitted(tool: str) -> bool:
    if tool in PERMITTED_BASE_TOOLS:
        return True
    return SCOPED_BRIGADE_BASH_RE.fullmatch(tool) is not None


def test_forward_plan_is_present_in_bundled_registry(tmp_path, capsys):
    source = _bundled_skill_dir()
    assert (source / "SKILL.md").is_file()
    assert (source / "skill.json").is_file()
    assert (source / "CHANGELOG.md").is_file()
    assert (source / "plan.template.json").is_file()

    metadata = _skill_metadata()
    assert metadata.get("id") == FORWARD_PLAN

    assert skills_cmd.lint(target=tmp_path, skill=FORWARD_PLAN, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is True
    assert payload["skill_id"] == FORWARD_PLAN
    assert payload["source"]["kind"] == "brigade-bundle"
    assert payload["source"]["identity"] == f"brigade://bundled-skills/{FORWARD_PLAN}"

    assert skills_cmd.import_skill(target=tmp_path, source=source, json_output=True) == 0
    capsys.readouterr()
    assert skills_cmd.search(target=tmp_path, query=FORWARD_PLAN, json_output=True) == 0
    search = json.loads(capsys.readouterr().out)
    assert search["count"] >= 1
    assert any(row["metadata"].get("id") == FORWARD_PLAN for row in search["skills"])


def test_forward_plan_is_allowlist_bound():
    source = _bundled_skill_dir()
    format_result = agent_skill_format.validate(source, mode="strict")
    assert format_result.errors == ()
    allowed = format_result.fields.get("allowed-tools")
    assert isinstance(allowed, tuple) and allowed
    assert "Bash" not in allowed, f"must not grant unscoped Bash: {allowed}"
    assert all(_tool_is_permitted(str(tool)) for tool in allowed), f"allowed-tools outside closed permit set: {allowed}"
    assert PERMITTED_BASE_TOOLS.issubset(set(allowed))
    assert EXPECTED_SCOPED_BASH.issubset(set(allowed))
    assert set(allowed) == PERMITTED_BASE_TOOLS | EXPECTED_SCOPED_BASH

    metadata = _skill_metadata()
    required_tools = metadata.get("required_tools")
    assert isinstance(required_tools, list) and required_tools
    assert "Bash" not in required_tools
    assert set(required_tools) == set(allowed)

    text = _skill_text()
    assert re.search(r"(?i)\ballowlist\b", text)
    assert re.search(r"(?i)failure paths?", text)
    assert re.search(r"(?i)missing", text)
    assert re.search(r"(?i)\bempty\b", text)
    assert re.search(r"(?i)malformed", text)


def test_forward_plan_defines_roadmap_parse_contract():
    text = _skill_text()
    assert re.search(r"(?i)ROADMAP\.md parse contract", text)
    for heading in ("Now", "Next", "Later", "Vocabulary", "How items move"):
        assert heading in text, f"parse contract must name heading shape {heading!r}"
    assert re.search(r"(?i)\bignore\b", text)
    assert re.search(r"(?i)(read|collect).{0,40}bullet", text, flags=re.DOTALL)


def test_forward_plan_safety_gates_graph_input_and_confirmation():
    """Pin safety-clarity contract: plan draft != --graph input; confirmation is procedural."""
    text = _skill_text()
    assert re.search(r"(?i)safety gates?", text)
    # Pin plan-draft vs --graph ingest; SKILL.md bolds key negations (**not**, **zero**).
    normalized = re.sub(r"\*+", "", text)
    assert re.search(r"(?i)plan draft is not graph-ingest input", normalized)
    assert re.search(r"(?i)forward-plan draft", normalized)
    assert "brigade work task add --graph" in normalized
    assert re.search(
        r"(?i)not valid input.{0,200}brigade work task add --graph",
        normalized,
        flags=re.DOTALL,
    )
    assert re.search(
        r"(?i)(zero|0).{0,60}proposed edge",
        text,
        flags=re.DOTALL,
    )
    assert re.search(
        r"(?i)operator-in-the-loop",
        text,
    )
    assert re.search(
        r"(?i)Bash\(brigade work task:\*\).{0,120}does not enforce",
        text,
        flags=re.DOTALL,
    )
    assert re.search(
        r"(?i)must not run any mutating.{0,120}before.{0,80}explicit\s+confirmation",
        text,
        flags=re.DOTALL,
    )


def test_forward_plan_graphtrail_edge_rules_and_degradation():
    text = _skill_text()
    assert re.search(r"(?i)GraphTrail", text)
    assert re.search(r"(?i)def/?use", text)
    assert re.search(r"(?i)conflicts-with", text)
    assert re.search(r"(?i)write overlap", text)
    assert re.search(r"(?i)\bderived\b", text)
    assert re.search(r"(?i)confirm", text)
    assert re.search(r"(?i)(unavailable|degrade)", text)
    assert re.search(
        r"(?i)(propose\s+no|no\s+derived).{0,60}edge",
        text,
        flags=re.DOTALL,
    )
    assert re.search(r"(?i)auto-retract", text)


def test_forward_plan_output_contract_under_work_plans():
    text = _skill_text()
    assert ".brigade/work/plans/" in text
    assert re.search(r"(?i)work ready", text)
    assert re.search(r"(?i)outcome rank", text)
    assert "never a CLI subcommand" in text or re.search(r"(?i)never a subcommand", text)


def test_forward_plan_documents_dispatch_annotations():
    """#815: skill recipe must explain emitting seat_class / spend_by hints."""
    text = _skill_text()
    assert re.search(r"(?i)seat_class", text)
    assert re.search(r"(?i)spend_by", text)
    assert re.search(r"mechanical", text)
    assert re.search(r"judgment", text)
    assert re.search(r"\breview\b", text)
    assert re.search(r"(?i)work task annotate", text)
    assert re.search(r"(?i)never invent a model id", text)
    metadata = _skill_metadata()
    assert metadata.get("version") == "0.1.1"


def test_forward_plan_template_manifest_validity():
    template_path = _bundled_skill_dir() / "plan.template.json"
    payload = json.loads(template_path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    missing = REQUIRED_TEMPLATE_KEYS - set(payload)
    assert not missing, f"plan.template.json missing keys: {sorted(missing)}"
    assert payload["kind"] == "forward-plan"
    assert payload["status"] == "draft"
    assert isinstance(payload["nodes"], list)
    assert isinstance(payload["proposed_edges"], list)
    assert payload["proposed_edges"], "template should illustrate derived edge shape"
    for edge in payload["proposed_edges"]:
        assert edge.get("derived") is True
        assert edge.get("confirmed") is False
        assert edge.get("type") in {"blocks", "conflicts-with"}
        assert edge.get("rule") in {"def-use", "write-overlap"}
    graphtrail = payload["graphtrail"]
    assert isinstance(graphtrail, dict)
    assert graphtrail.get("available") is False
    assert "unavailable" in str(graphtrail.get("degradation") or "").casefold()
    confirmation = payload["confirmation"]
    assert confirmation.get("required_for_derived_edges") is True
    assert confirmation.get("auto_apply_forbidden") is True
    assert confirmation.get("only_derived_may_be_auto_retracted") is True
    inputs = payload["inputs"]
    assert "ready" in inputs and "outcome_rank" in inputs and "roadmap" in inputs
    roadmap = inputs["roadmap"]
    assert "Now" in roadmap.get("sections_read", [])
    assert "Vocabulary" in roadmap.get("sections_ignored", [])


def test_forward_plan_is_not_a_cli_subcommand():
    """Regression guard: #779 ships a skill, never a brigade forward-plan command."""
    import argparse

    from brigade import cli

    parser = cli._build_parser()
    subparsers_action = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))
    choices = set(subparsers_action.choices or {})
    assert FORWARD_PLAN not in choices
    assert "forward" not in choices
