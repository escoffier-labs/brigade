from __future__ import annotations

import json
from pathlib import Path

from brigade import handoff_cmd

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "handoff_lint" / "readability"

FENCED_CARD_HANDOFF = """# Memory Handoff

## Type
bugfix

## Title
Fenced card readability regression

## Summary
Suggested card content may be wrapped in a markdown fence.

## Durable facts

- widget-parser.py keeps an explicit subject in every durable fact.

## Evidence

- files changed: `tests/test_handoff_readability.py`

## Recommended memory action
create-card

## Target card
example-widget.md

## Suggested card content

```markdown
---
name: example-widget
---

It was reverted last week because that file was wrong.
```
"""

OUT_OF_SCOPE_HANDOFF = """# Memory Handoff

## Type
bugfix

## Title
Out-of-scope readability noise

## Summary
It was reverted last week because that file was wrong.

## Durable facts

- widget-parser.py keeps an explicit subject in every durable fact.

## Evidence

- It was reverted last week because that file was wrong.

## Recommended memory action
create-card

## Target card
example-widget.md

## Suggested card content
---
name: example-widget
description: Placeholder card with explicit subjects only.
tags: [fixture, readability]
---

### Widget parser guard

widget-parser.py documents the retry guard with explicit module names.
"""


def test_lint_file_bare_pronoun_fixture_warns_without_failing():
    result = handoff_cmd.lint_file(FIXTURES / "bare-pronoun.md")

    assert result.valid is True
    assert len(result.readability) == 1
    assert result.readability[0].category == "bare-pronoun"
    assert any("[readability/bare-pronoun]" in warning for warning in result.warnings)


def test_lint_file_deictic_fixture_warns():
    result = handoff_cmd.lint_file(FIXTURES / "deictic.md")

    assert result.valid is True
    assert len(result.readability) == 1
    assert result.readability[0].category == "deictic"


def test_lint_file_relative_date_fixture_warns_with_absolute_date_guidance():
    result = handoff_cmd.lint_file(FIXTURES / "relative-date.md")

    assert result.valid is True
    assert len(result.readability) == 1
    assert result.readability[0].category == "relative-date"
    assert any("absolute date" in warning for warning in result.warnings)


def test_lint_file_clean_antecedent_fixture_has_no_readability_findings():
    result = handoff_cmd.lint_file(FIXTURES / "clean-antecedent.md")

    assert result.valid is True
    assert result.readability == ()


def test_lint_file_compliant_fixture_has_no_readability_findings():
    result = handoff_cmd.lint_file(FIXTURES / "compliant.md")

    assert result.valid is True
    assert result.readability == ()


def test_lint_file_ignores_pronouns_outside_scanned_sections(tmp_path):
    path = tmp_path / "out-of-scope.md"
    path.write_text(OUT_OF_SCOPE_HANDOFF)

    result = handoff_cmd.lint_file(path)

    assert result.valid is True
    assert result.readability == ()


def test_lint_file_scans_fenced_suggested_card_content(tmp_path):
    path = tmp_path / "fenced-card.md"
    path.write_text(FENCED_CARD_HANDOFF)

    result = handoff_cmd.lint_file(path)

    assert result.valid is True
    assert {finding.category for finding in result.readability} == {
        "bare-pronoun",
        "deictic",
        "relative-date",
    }
    assert all(finding.section == "Suggested card content" for finding in result.readability)


def test_default_lint_over_fixture_directory_exits_zero_and_prints_warnings(capsys):
    paths = sorted(FIXTURES.glob("*.md"))

    assert handoff_cmd.lint(target=FIXTURES, paths=paths) == 0

    out = capsys.readouterr().out
    assert "[readability/bare-pronoun]" in out
    assert "[readability/deictic]" in out
    assert "[readability/relative-date]" in out
    assert "readability:" in out
    assert "--strict to fail" in out


def test_strict_lint_promotes_readability_findings_to_errors(capsys):
    path = FIXTURES / "bare-pronoun.md"

    assert handoff_cmd.lint(target=FIXTURES, paths=[path], strict=True, json_output=True) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is False
    assert payload["strict"] is True
    assert payload["readability_flagged_count"] == 1
    result = payload["results"][0]
    assert result["valid"] is False
    assert any("[readability/bare-pronoun]" in error for error in result["errors"])


def test_json_shape_stays_compatible_for_default_lint_with_findings(capsys):
    path = FIXTURES / "bare-pronoun.md"

    assert handoff_cmd.lint(target=FIXTURES, paths=[path], json_output=True) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is True
    for key in ("target", "count", "valid", "results", "content_guard"):
        assert key in payload
    assert payload["strict"] is False
    assert payload["readability_flagged_count"] == 1
    result = payload["results"][0]
    for key in ("path", "action", "valid", "errors", "warnings", "hints", "readability"):
        assert key in result
    assert result["valid"] is True
    assert len(result["readability"]) == 1
    assert result["readability"][0]["category"] == "bare-pronoun"
