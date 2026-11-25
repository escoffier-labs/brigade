"""Deprecation diagnostics leave stdout and protocol output unchanged."""

from __future__ import annotations

import pytest

from brigade import component_bins, component_manifest, engine_deprecation, engine_paths


@pytest.fixture(autouse=True)
def reset_notices(monkeypatch):
    monkeypatch.setattr(engine_deprecation, "_seen", set())


@pytest.mark.parametrize("legacy,canonical", component_manifest.COMPONENT_FAMILIES.items())
def test_binary_environment_notice_once_per_spelling(tmp_path, capsys, legacy, canonical):
    executable = tmp_path / canonical
    executable.write_text("fixture")
    executable.chmod(0o755)
    variable = component_bins.ENV_OVERRIDES[legacy]
    replacement = component_bins.ENV_OVERRIDES[canonical]
    env = {variable: str(executable), "HOME": str(tmp_path), "PATH": ""}
    assert component_bins.resolve(canonical, env=env) == str(executable)
    assert component_bins.resolve(canonical, env=env) == str(executable)
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err == f"{variable} is deprecated; use {replacement}; removed in v0.33.0\n"


def test_canonical_environment_is_silent(tmp_path, capsys):
    component_bins.resolve("brigade-code-graph", env={"HOME": str(tmp_path), "PATH": ""})
    engine_paths.code_graph_db(tmp_path, env={engine_paths.CODE_GRAPH_DB_ENV: "explicit.db"})
    assert capsys.readouterr().err == ""


def test_projection_refuses_conflicting_overrides(monkeypatch, capsys):
    legacy, canonical = next(iter(component_manifest.COMPONENT_FAMILIES.items()))
    monkeypatch.setenv(component_bins.ENV_OVERRIDES[legacy], "old-executable")
    monkeypatch.setenv(component_bins.ENV_OVERRIDES[canonical], "new-executable")
    with pytest.raises(ValueError, match="invalid or conflicting"):
        component_bins.canonical_command(legacy)
    assert capsys.readouterr().err.count("deprecated") == 1
