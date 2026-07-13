from __future__ import annotations

from brigade import agent_skill_format


def _skill(tmp_path, name: str, frontmatter: str):
    directory = tmp_path / name
    directory.mkdir()
    (directory / "SKILL.md").write_text(f"---\n{frontmatter}---\n\n# Body\n")
    return directory


def test_strict_accepts_agent_skills_fields_and_treats_tools_as_requirements(tmp_path):
    directory = _skill(
        tmp_path,
        "code-review",
        "name: code-review\n"
        "description: Review a change.\n"
        "license: MIT\n"
        "compatibility: Requires git.\n"
        "allowed-tools: read, grep\n"
        "metadata:\n  owner: team\n",
    )
    result = agent_skill_format.validate(directory, mode="strict")
    assert result.errors == ()
    assert result.fields["allowed-tools"] == ("read", "grep")
    assert result.fields["metadata"] == {"owner": "team"}


def test_strict_rejects_unknown_and_lenient_retains_diagnostic(tmp_path):
    directory = _skill(
        tmp_path,
        "code-review",
        "name: code-review\ndescription: Review a change.\nfuture-field: value\n",
    )
    strict = agent_skill_format.validate(directory, mode="strict")
    lenient = agent_skill_format.validate(directory, mode="lenient")
    assert "unknown frontmatter field: future-field" in strict.errors
    assert lenient.errors == ()
    assert lenient.diagnostics == ("unknown frontmatter field retained: future-field",)
    assert lenient.fields["future-field"] == "value"


def test_name_description_and_exact_casing_are_enforced(tmp_path):
    directory = _skill(tmp_path, "Bad_Name", "name: Bad_Name\ndescription: x\n")
    result = agent_skill_format.validate(directory, mode="strict")
    assert any("lowercase identifier" in error for error in result.errors)

    wrong = tmp_path / "lowercase"
    wrong.mkdir()
    (wrong / "skill.md").write_text("---\nname: lowercase\ndescription: x\n---\n")
    result = agent_skill_format.validate(wrong, mode="strict")
    assert any("SKILL.md not found" in error for error in result.errors)
