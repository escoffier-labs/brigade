from __future__ import annotations

import json

import pytest

from brigade import skills_cmd
from brigade.templates import template_root

BUNDLED_TEMPLATES = ("brigade-work", "note", "ultra-work-scout")


def _doctor_issue_names(skill_id, target, capsys):
    assert skills_cmd.doctor(target=target, json_output=True) == 0
    doctor = json.loads(capsys.readouterr().out)
    return {issue["name"] for issue in doctor["issues"] if issue.get("skill_id") == skill_id}


@pytest.mark.parametrize("skill_id", BUNDLED_TEMPLATES)
def test_bundled_template_imports_without_tests_or_changelog_warnings(skill_id, tmp_path, capsys):
    source = template_root() / "skills" / skill_id
    assert (source / "SKILL.md").is_file()

    assert skills_cmd.import_skill(target=tmp_path, source=source, json_output=True) == 0
    capsys.readouterr()

    names = _doctor_issue_names(skill_id, tmp_path, capsys)
    # tests + changelog advisories must be resolved by shipped metadata...
    assert "skill_tests_missing" not in names
    assert "skill_changelog_missing" not in names
    # ...but templates must NOT pre-approve their own trust: the unreviewed
    # advisory is correct and must survive import.
    assert "skill_unreviewed_trust" in names


@pytest.mark.parametrize("skill_id", BUNDLED_TEMPLATES)
def test_bundled_template_ships_tests_and_changelog_inside_dir(skill_id):
    source = template_root() / "skills" / skill_id
    # CHANGELOG.md must live inside the skill dir so it travels with the
    # copytree registry import; a changelog_path pointing outside would dangle.
    assert (source / "CHANGELOG.md").is_file()

    metadata = json.loads((source / "skill.json").read_text())
    tests = metadata.get("tests")
    assert isinstance(tests, list) and tests, "template must declare at least one honest test"
    # trust is a per-workspace review decision; templates stay unreviewed.
    assert "trust_level" not in metadata


@pytest.mark.parametrize("skill_id", BUNDLED_TEMPLATES)
def test_canonical_bundled_source_has_reviewed_runtime_provenance(skill_id, tmp_path, capsys):
    assert skills_cmd.lint(target=tmp_path, skill=skill_id, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["source"]["kind"] == "brigade-bundle"
    assert payload["source"]["identity"] == f"brigade://bundled-skills/{skill_id}"
    assert payload["source"]["reviewed"] is True
    assert payload["trust_score"]["trust_level"] == "bundled"
    assert "trust_level is unreviewed" not in payload["trust_score"]["signals"]
