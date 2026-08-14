"""Reusable projection conformance fixture (issue #910)."""

from __future__ import annotations

import inspect
from pathlib import Path

from brigade.projection import fixture, kernel


def test_conformance_fixture_does_not_import_command_modules() -> None:
    source = inspect.getsource(fixture)
    assert "brigade.cli" not in source
    assert "projection_cmd" not in source
    assert "mcp" not in source
    assert "harness_profile" not in source
    assert "skills_cmd" not in source


def test_conformance_fixture_mixed_operation_and_injected_failure(tmp_path: Path) -> None:
    report = fixture.run_conformance(tmp_path)
    assert report["committed"] is True
    assert report["restored_after_inject"] is True
    assert report["boundaries"] == [
        "commit:0:before",
        "commit:0:after",
        "commit:1:before",
        "commit:1:after",
        "commit:2:before",
        "commit:2:after",
    ]
    assert kernel.DEPENDENCY_DECISION["implementation"] == "native"
