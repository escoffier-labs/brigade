from __future__ import annotations

import pytest

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


def test_allowed_tools_preserve_parenthesized_command_scopes(tmp_path):
    directory = _skill(
        tmp_path,
        "scoped-bash",
        "name: scoped-bash\n"
        "description: Scope bash to named commands.\n"
        "allowed-tools: Read, Grep, Bash(brigade handoff list:*), Bash(brigade memory care status:*)\n",
    )
    result = agent_skill_format.validate(directory, mode="strict")
    assert result.errors == ()
    assert result.fields["allowed-tools"] == (
        "Read",
        "Grep",
        "Bash(brigade handoff list:*)",
        "Bash(brigade memory care status:*)",
    )


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


def test_digit_bearing_names_are_valid(tmp_path):
    directory = _skill(
        tmp_path,
        "pdf2text-v2",
        "name: pdf2text-v2\ndescription: Convert PDFs.\n",
    )
    result = agent_skill_format.validate(directory, mode="strict")
    assert result.errors == ()


def test_compatibility_over_500_characters_is_strict_error_lenient_diagnostic(tmp_path):
    frontmatter = f"name: code-review\ndescription: Review a change.\ncompatibility: {'x' * 501}\n"
    directory = _skill(tmp_path, "code-review", frontmatter)
    strict = agent_skill_format.validate(directory, mode="strict")
    lenient = agent_skill_format.validate(directory, mode="lenient")
    assert "frontmatter compatibility exceeds 500 characters" in strict.errors
    assert lenient.errors == ()
    assert "frontmatter compatibility exceeds 500 characters" in lenient.diagnostics


@pytest.mark.parametrize("mode", ["strict", "lenient"])
@pytest.mark.parametrize(
    ("indicator", "expected"),
    [
        (">", "First line. Second line.\nNext paragraph.\n"),
        (">-", "First line. Second line.\nNext paragraph."),
        (">+", "First line. Second line.\nNext paragraph.\n\n\n"),
        ("|", "First line.\nSecond line.\n\nNext paragraph.\n"),
        ("|-", "First line.\nSecond line.\n\nNext paragraph."),
        ("|+", "First line.\nSecond line.\n\nNext paragraph.\n\n\n"),
    ],
)
def test_block_scalar_folding_and_chomping(tmp_path, mode, indicator, expected):
    directory = _skill(
        tmp_path,
        "code-review",
        f"name: code-review\ndescription: {indicator}\n"
        "  First line.\n  Second line.\n\n  Next paragraph.\n\n\n"
        "license: MIT\nmetadata:\n  owner: team\n",
    )
    result = agent_skill_format.validate(directory, mode=mode)
    assert result.errors == ()
    assert result.fields["description"] == expected
    assert result.fields["license"] == "MIT"
    assert result.fields["metadata"] == {"owner": "team"}


@pytest.mark.parametrize("indicator", [">", "|"])
def test_block_scalars_preserve_leading_lines_indentation_and_content(tmp_path, indicator):
    directory = _skill(
        tmp_path,
        "code-review",
        f"name: code-review\ndescription: {indicator}- # header comment\n"
        "\n  First line.  \n\n    - item\n    key: value\n\n  # content\n  'quoted'\n",
    )
    result = agent_skill_format.validate(directory, mode="strict")
    assert result.errors == ()
    last_separator = " " if indicator == ">" else "\n"
    assert result.fields["description"] == (
        "\nFirst line.  \n\n  - item\n  key: value\n\n# content" + last_separator + "'quoted'"
    )


@pytest.mark.parametrize("indicator", [">2-", ">-2", "|2-", "|-2"])
def test_block_scalar_explicit_indentation_preserves_leading_spaces(tmp_path, indicator):
    directory = _skill(
        tmp_path,
        "code-review",
        f"name: code-review\ndescription: {indicator}\n    First line.\n  Last line.\n",
    )
    result = agent_skill_format.validate(directory, mode="strict")
    assert result.errors == ()
    assert result.fields["description"] == "  First line.\nLast line."


@pytest.mark.parametrize("indicator", [">", ">-", ">+", "|", "|-", "|+"])
@pytest.mark.parametrize("content", ["", "\n  \n"])
def test_empty_block_scalars_are_chomped_before_validation(tmp_path, indicator, content):
    directory = _skill(tmp_path, "code-review", f"name: code-review\ndescription: {indicator}\n{content}")
    result = agent_skill_format.validate(directory, mode="strict")
    expected = "\n\n" if indicator.endswith("+") and content else ""
    assert result.fields["description"] == expected
    assert result.errors == (() if expected else ("frontmatter description is required",))


def test_empty_block_scalar_ends_at_next_top_level_field(tmp_path):
    directory = _skill(
        tmp_path,
        "code-review",
        "name: code-review\ndescription: >+\n  \nlicense: MIT\n",
    )
    result = agent_skill_format.validate(directory, mode="strict")
    assert result.errors == ()
    assert result.fields["description"] == "\n"
    assert result.fields["license"] == "MIT"


@pytest.mark.parametrize("mode", ["strict", "lenient"])
def test_block_scalars_apply_to_other_top_level_scalar_fields(tmp_path, mode):
    directory = _skill(
        tmp_path,
        "code-review",
        "name: |-\n  code-review\ndescription: Review a change.\n"
        "license: >-\n  MIT\ncompatibility: |\n  Requires git.\n"
        "allowed-tools: >-\n  Read,\n  Bash(git status:*)\nfuture-field: |+\n  retained\n\n",
    )
    result = agent_skill_format.validate(directory, mode=mode)
    assert result.errors == (("unknown frontmatter field: future-field",) if mode == "strict" else ())
    assert result.fields["name"] == "code-review"
    assert result.fields["license"] == "MIT"
    assert result.fields["compatibility"] == "Requires git.\n"
    assert result.fields["allowed-tools"] == ("Read", "Bash(git status:*)")
    assert result.fields["future-field"] == "retained\n\n"


@pytest.mark.parametrize("mode", ["strict", "lenient"])
@pytest.mark.parametrize(
    "declaration",
    ["description: Plain text\n  continuation", "description:\n  nested: value", "description:\n  - item"],
)
def test_indented_continuations_without_block_header_still_error(tmp_path, mode, declaration):
    directory = _skill(tmp_path, "code-review", f"name: code-review\n{declaration}\n")
    result = agent_skill_format.validate(directory, mode=mode)
    assert "frontmatter line 4 has unsupported nesting" in result.errors


@pytest.mark.parametrize("indicator", [">0", "|10", ">--", "| text"])
def test_invalid_block_scalar_headers_have_clear_error(tmp_path, indicator):
    directory = _skill(tmp_path, "code-review", f"name: code-review\ndescription: {indicator}\n  text\n")
    result = agent_skill_format.validate(directory, mode="strict")
    assert "frontmatter line 3 has invalid block scalar header" in result.errors


@pytest.mark.parametrize("content", ["    first\n  dedented\n", "\ttext\n", "    \n  text\n"])
def test_invalid_block_scalar_indentation_still_errors(tmp_path, content):
    directory = _skill(tmp_path, "code-review", f"name: code-review\ndescription: >-\n{content}")
    result = agent_skill_format.validate(directory, mode="strict")
    assert result.errors
