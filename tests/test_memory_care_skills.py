"""Contract tests for the memory-care skill pack (#766 / Track A).

Ship exactly two bundled registry skills: handoff-inbox-drain and card-refresh.
session-sweep stays out of scope until #466 defines capture-at-source.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from brigade import agent_skill_format, skills_cmd
from brigade.templates import template_root

MEMORY_CARE_SKILLS = ("handoff-inbox-drain", "card-refresh")
BLOCKED_SESSION_SWEEP = "session-sweep"
# Closed allowlist: bare Bash / Write / Edit / WebFetch and similar must fail.
PERMITTED_BASE_TOOLS = frozenset({"Read", "Grep"})
SCOPED_BRIGADE_BASH_RE = re.compile(r"^Bash\(brigade .+:\*\)$")
EXPECTED_SCOPED_BASH = {
    "handoff-inbox-drain": frozenset(
        {
            "Bash(brigade handoff list:*)",
            "Bash(brigade handoff show:*)",
            "Bash(brigade handoff archive:*)",
            "Bash(brigade handoff closeout:*)",
            "Bash(brigade handoff lint:*)",
            "Bash(brigade handoff doctor:*)",
        }
    ),
    "card-refresh": frozenset(
        {
            "Bash(brigade memory care status:*)",
            "Bash(brigade memory care plan-fixes:*)",
            "Bash(brigade memory care closeout:*)",
        }
    ),
}


def _bundled_skill_dir(skill_id: str) -> Path:
    return template_root() / "skills" / skill_id


def _require_bundled_skill_md(skill_id: str) -> Path:
    path = _bundled_skill_dir(skill_id) / "SKILL.md"
    assert path.is_file(), f"missing bundled SKILL.md for {skill_id}"
    return path


def _skill_text(skill_id: str) -> str:
    return _require_bundled_skill_md(skill_id).read_text(encoding="utf-8")


def _skill_metadata(skill_id: str) -> dict:
    path = _bundled_skill_dir(skill_id) / "skill.json"
    assert path.is_file(), f"missing bundled skill.json for {skill_id}"
    return json.loads(path.read_text(encoding="utf-8"))


def _tool_is_permitted(tool: str) -> bool:
    if tool in PERMITTED_BASE_TOOLS:
        return True
    return SCOPED_BRIGADE_BASH_RE.fullmatch(tool) is not None


def _bundled_skill_ids() -> set[str]:
    root = template_root() / "skills"
    if not root.is_dir():
        return set()
    return {path.name for path in root.iterdir() if path.is_dir() and (path / "SKILL.md").is_file()}


@pytest.mark.parametrize("skill_id", MEMORY_CARE_SKILLS)
def test_memory_care_skill_is_present_in_bundled_registry(skill_id, tmp_path, capsys):
    source = _bundled_skill_dir(skill_id)
    assert (source / "SKILL.md").is_file(), f"missing bundled SKILL.md for {skill_id}"
    assert (source / "skill.json").is_file(), f"missing bundled skill.json for {skill_id}"

    metadata = _skill_metadata(skill_id)
    assert metadata.get("id") == skill_id

    assert skills_cmd.lint(target=tmp_path, skill=skill_id, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is True
    assert payload["skill_id"] == skill_id
    assert payload["source"]["kind"] == "brigade-bundle"
    assert payload["source"]["identity"] == f"brigade://bundled-skills/{skill_id}"

    assert skills_cmd.import_skill(target=tmp_path, source=source, json_output=True) == 0
    capsys.readouterr()
    assert skills_cmd.search(target=tmp_path, query=skill_id, json_output=True) == 0
    search = json.loads(capsys.readouterr().out)
    assert search["count"] >= 1
    assert any(row["metadata"].get("id") == skill_id for row in search["skills"])


@pytest.mark.parametrize("skill_id", MEMORY_CARE_SKILLS)
def test_memory_care_skill_is_allowlist_bound(skill_id):
    source = _bundled_skill_dir(skill_id)
    _require_bundled_skill_md(skill_id)

    format_result = agent_skill_format.validate(source, mode="strict")
    assert format_result.errors == ()
    allowed = format_result.fields.get("allowed-tools")
    assert isinstance(allowed, tuple) and allowed, f"{skill_id} must declare non-empty allowed-tools"
    assert "Bash" not in allowed, f"{skill_id} must not grant unscoped Bash: {allowed}"
    assert all(_tool_is_permitted(str(tool)) for tool in allowed), (
        f"{skill_id} allowed-tools outside closed permit set: {allowed}"
    )
    assert PERMITTED_BASE_TOOLS.issubset(set(allowed)), (
        f"{skill_id} must include Read and Grep in allowed-tools: {allowed}"
    )
    assert EXPECTED_SCOPED_BASH[skill_id].issubset(set(allowed)), (
        f"{skill_id} missing scoped Bash allowlist entries: {EXPECTED_SCOPED_BASH[skill_id] - set(allowed)}"
    )
    assert set(allowed) == PERMITTED_BASE_TOOLS | EXPECTED_SCOPED_BASH[skill_id], (
        f"{skill_id} allowed-tools must equal the closed permit set, got {allowed}"
    )

    metadata = _skill_metadata(skill_id)
    required_tools = metadata.get("required_tools")
    assert isinstance(required_tools, list) and required_tools
    assert "Bash" not in required_tools, f"{skill_id} required_tools must not grant unscoped Bash"
    assert all(_tool_is_permitted(str(tool)) for tool in required_tools), (
        f"{skill_id} required_tools outside closed permit set: {required_tools}"
    )
    assert set(required_tools) == set(allowed), f"{skill_id} required_tools must match allowed-tools exactly"

    text = _skill_text(skill_id)
    assert re.search(r"(?i)\ballowlist\b", text), f"{skill_id} must bind work to an allowlist"
    assert re.search(r"(?i)failure paths?", text), f"{skill_id} must document failure paths"
    assert re.search(r"(?i)missing", text), f"{skill_id} must handle a missing inbox/queue"
    assert re.search(r"(?i)\bempty\b", text), f"{skill_id} must handle an empty set"
    assert re.search(r"(?i)malformed", text), f"{skill_id} must handle malformed entries"


@pytest.mark.parametrize("skill_id", MEMORY_CARE_SKILLS)
def test_memory_care_skill_enforces_grounded_edit_rules(skill_id):
    text = _skill_text(skill_id)
    assert re.search(r"(?i)grounded", text), f"{skill_id} must state grounded-edit rules"
    assert re.search(r"(?i)\bcite\b", text), f"{skill_id} must require edits to cite sources"
    assert re.search(r"(?i)on[- ]disk", text), f"{skill_id} must require citations of on-disk content"
    assert re.search(
        r"(?i)(edit|change|refresh).{0,80}(only|must).{0,80}(cite|verif|source of truth|on[- ]disk)",
        text,
        flags=re.DOTALL,
    ) or re.search(
        r"(?i)(cite|verif).{0,80}(existing|on[- ]disk|source of truth)",
        text,
        flags=re.DOTALL,
    ), f"{skill_id} must require edits to cite existing on-disk content"


@pytest.mark.parametrize("skill_id", MEMORY_CARE_SKILLS)
def test_memory_care_skill_never_deletes(skill_id):
    text = _skill_text(skill_id)
    for operation in ("delete", "remove", "prune"):
        assert re.search(
            rf"(?i)(?:(never|do\s+not|must\s+not|no)\s+{operation}\b|"
            rf"\b{operation}\b.{{0,40}}(not\s+permitted|forbidden|prohibited))",
            text,
        ), f"{skill_id} must forbid {operation} operations"
    assert re.search(r"(?i)archive(\s+only)?", text), f"{skill_id} should prefer archive-only retention"


@pytest.mark.parametrize("skill_id", MEMORY_CARE_SKILLS)
def test_memory_care_skill_carries_manual_dedup_pending_724(skill_id):
    text = _skill_text(skill_id)
    assert re.search(r"(?i)manual\s+dedup", text), f"{skill_id} must carry manual dedup instructions"
    assert re.search(r"#\s*724\b", text), f"{skill_id} must note manual dedup is pending #724"


def test_session_sweep_is_not_built_or_registered(tmp_path, capsys):
    """session-sweep is the #466 backstop and must not ship in this pack."""
    bundled = _bundled_skill_ids()
    assert BLOCKED_SESSION_SWEEP not in bundled
    assert not (_bundled_skill_dir(BLOCKED_SESSION_SWEEP) / "SKILL.md").exists()
    assert not skills_cmd._bundled_skill_exists(BLOCKED_SESSION_SWEEP)

    rc = skills_cmd.lint(target=tmp_path, skill=BLOCKED_SESSION_SWEEP, json_output=True)
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["valid"] is False
    assert payload["source"]["kind"] != "brigade-bundle"
    assert any("SKILL.md not found" in error for error in payload["errors"])
