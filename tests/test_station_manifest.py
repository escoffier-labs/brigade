"""Tests for additive ``brigade.station.v1`` manifest fields."""

from __future__ import annotations

import json

import pytest

from brigade import station_manifest


def _write_manifest(tmp_path, **overrides):
    manifest = {
        "schema": station_manifest.SCHEMA,
        "name": "example-sidecar",
        "station": "evidence",
        "summary": "example station contract",
        "tools": [
            {
                "name": "example-sidecar",
                "command": "example-sidecar",
                "summary": "example tool",
                "surfaces": [
                    {
                        "kind": "verify-exit",
                        "command": ["example-sidecar", "--version"],
                    }
                ],
            }
        ],
    }
    manifest.update(overrides)
    path = tmp_path / "station.json"
    path.write_text(json.dumps(manifest))
    return path


def test_legacy_manifest_defaults_to_active_executable(tmp_path):
    manifest = station_manifest.load(str(_write_manifest(tmp_path)))

    assert manifest.lifecycle == "active"
    assert manifest.owner == ""
    assert manifest.contract_version == 1
    assert manifest.requires_brigade.min_version is None
    assert manifest.requires_brigade.max_version_exclusive is None
    assert manifest.compatibility.status == "compatible"
    assert manifest.tools[0].kind == "executable"
    assert manifest.tools[0].command == "example-sidecar"
    assert manifest.tools[0].surfaces[0].probe == ()
    assert manifest.tools[0].surfaces[0].probe_contains == ()
    assert manifest.tools[0].surfaces[0].placeholders == ()


@pytest.mark.parametrize("lifecycle", ["active", "embedded", "deprecated", "historical"])
def test_manifest_accepts_each_lifecycle(tmp_path, lifecycle):
    overrides = {"lifecycle": lifecycle}
    if lifecycle != "active":
        overrides["owner"] = "maintained-package"
    manifest = station_manifest.load(str(_write_manifest(tmp_path, **overrides)))

    assert manifest.lifecycle == lifecycle


def test_manifest_rejects_unknown_lifecycle(tmp_path):
    path = _write_manifest(tmp_path, lifecycle="retired")

    with pytest.raises(ValueError, match="lifecycle"):
        station_manifest.load(str(path))


def test_manifest_accepts_additive_compatibility_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(station_manifest, "_BRIGADE_VERSION", "1.2.3")
    manifest = station_manifest.load(
        str(
            _write_manifest(
                tmp_path,
                contract_version=2,
                requires_brigade={
                    "min_version": "1.2.0",
                    "max_version_exclusive": "2.0.0",
                },
            )
        )
    )

    assert manifest.contract_version == 2
    assert manifest.requires_brigade.min_version == "1.2.0"
    assert manifest.requires_brigade.max_version_exclusive == "2.0.0"
    assert manifest.compatibility.status == "compatible"
    assert manifest.compatibility.current_version == "1.2.3"


@pytest.mark.parametrize(
    ("requires", "detail"),
    [
        ({"min_version": "9.0.0"}, "requires Brigade >= 9.0.0"),
        ({"max_version_exclusive": "1.2.3"}, "requires Brigade < 1.2.3"),
    ],
)
def test_manifest_reports_strict_semver_incompatibility(tmp_path, monkeypatch, requires, detail):
    monkeypatch.setattr(station_manifest, "_BRIGADE_VERSION", "1.2.3")
    manifest = station_manifest.load(str(_write_manifest(tmp_path, requires_brigade=requires)))

    assert manifest.compatibility.status == "incompatible"
    assert manifest.compatibility.compatible is False
    assert detail in manifest.compatibility.detail


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("contract_version", 0),
        ("contract_version", True),
        ("requires_brigade", []),
        ("requires_brigade", {"min_version": "1.2"}),
        ("requires_brigade", {"max_version_exclusive": "1.2.x"}),
    ],
)
def test_manifest_rejects_invalid_compatibility_fields(tmp_path, field, value):
    path = _write_manifest(tmp_path, **{field: value})

    with pytest.raises(ValueError, match=field):
        station_manifest.load(str(path))


def test_manifest_tool_dependency_arrays_are_optional_strings(tmp_path):
    manifest = station_manifest.load(
        str(
            _write_manifest(
                tmp_path,
                tools=[
                    {
                        "name": "example-sidecar",
                        "command": "example-sidecar",
                        "produces": ["context"],
                        "consumes": ["task"],
                        "dependencies": ["miseledger"],
                    }
                ],
            )
        )
    )

    tool = manifest.tools[0]
    assert tool.produces == ("context",)
    assert tool.consumes == ("task",)
    assert tool.dependencies == ("miseledger",)


def test_active_manifest_still_requires_at_least_one_tool(tmp_path):
    path = _write_manifest(tmp_path, tools=[])

    with pytest.raises(ValueError, match="active station manifest requires at least one tool"):
        station_manifest.load(str(path))


@pytest.mark.parametrize("lifecycle", ["embedded", "deprecated", "historical"])
def test_non_active_manifest_may_have_no_tools(tmp_path, lifecycle):
    manifest = station_manifest.load(
        str(_write_manifest(tmp_path, lifecycle=lifecycle, owner="maintained-package", tools=[]))
    )

    assert manifest.lifecycle == lifecycle
    assert manifest.owner == "maintained-package"
    assert manifest.tools == ()


@pytest.mark.parametrize("lifecycle", ["embedded", "deprecated", "historical"])
def test_non_active_manifest_rejects_omitted_owner(tmp_path, lifecycle):
    path = _write_manifest(tmp_path, lifecycle=lifecycle, tools=[])

    with pytest.raises(ValueError, match="owner"):
        station_manifest.load(str(path))


@pytest.mark.parametrize("lifecycle", ["embedded", "deprecated", "historical"])
def test_non_active_manifest_rejects_blank_owner(tmp_path, lifecycle):
    path = _write_manifest(tmp_path, lifecycle=lifecycle, owner="   ", tools=[])

    with pytest.raises(ValueError, match="owner"):
        station_manifest.load(str(path))


def test_executable_kind_still_requires_command(tmp_path):
    path = _write_manifest(
        tmp_path,
        tools=[{"name": "example-sidecar", "summary": "example tool"}],
    )

    with pytest.raises(ValueError, match="command"):
        station_manifest.load(str(path))


def test_skill_roster_kind_permits_no_command(tmp_path):
    path = _write_manifest(
        tmp_path,
        tools=[
            {
                "name": "example-skills",
                "kind": "skill-roster",
                "summary": "portable skills",
                "install": ["npx", "skills", "add", "example.invalid/skills"],
                "surfaces": [
                    {
                        "kind": "verify-exit",
                        "command": ["bash", "tests/lint-skills.sh"],
                        "read_only": True,
                    }
                ],
            }
        ],
    )

    tool = station_manifest.load(str(path)).tools[0]
    assert tool.kind == "skill-roster"
    assert tool.command == ""


def test_manifest_rejects_unknown_tool_kind(tmp_path):
    path = _write_manifest(
        tmp_path,
        tools=[
            {
                "name": "example-sidecar",
                "kind": "service",
                "command": "example-sidecar",
            }
        ],
    )

    with pytest.raises(ValueError, match="tool.kind"):
        station_manifest.load(str(path))


def test_surface_parses_probe_assertions_and_allowed_placeholders(tmp_path):
    path = _write_manifest(
        tmp_path,
        tools=[
            {
                "name": "example-sidecar",
                "command": "example-sidecar",
                "surfaces": [
                    {
                        "kind": "brief-markdown",
                        "command": ["example-sidecar", "evidence", "<task>", "--markdown"],
                        "read_only": False,
                        "probe": ["example-sidecar", "evidence", "--help"],
                        "probe_contains": ["--markdown", "--limit"],
                        "timeout_seconds": 10,
                        "max_chars": 4000,
                    },
                    {
                        "kind": "query-json",
                        "command": ["example-sidecar", "query", "<query>", "--json"],
                    },
                ],
            }
        ],
    )

    surfaces = station_manifest.load(str(path)).tools[0].surfaces
    assert surfaces[0].probe == ("example-sidecar", "evidence", "--help")
    assert surfaces[0].probe_contains == ("--markdown", "--limit")
    assert surfaces[0].placeholders == ("task",)
    assert surfaces[1].placeholders == ("query",)


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), float("-inf")])
def test_surface_rejects_non_finite_timeout(tmp_path, timeout):
    path = _write_manifest(
        tmp_path,
        tools=[
            {
                "name": "example-sidecar",
                "command": "example-sidecar",
                "surfaces": [
                    {
                        "kind": "verify-exit",
                        "command": ["example-sidecar", "--version"],
                        "timeout_seconds": timeout,
                    }
                ],
            }
        ],
    )

    with pytest.raises(ValueError, match="finite"):
        station_manifest.load(str(path))


def test_surface_rejects_integer_timeout_too_large_for_float(tmp_path):
    path = _write_manifest(
        tmp_path,
        tools=[
            {
                "name": "example-sidecar",
                "command": "example-sidecar",
                "surfaces": [
                    {
                        "kind": "verify-exit",
                        "command": ["example-sidecar", "--version"],
                        "timeout_seconds": 10**400,
                    }
                ],
            }
        ],
    )

    with pytest.raises(ValueError, match="range"):
        station_manifest.load(str(path))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("command", ["example-sidecar", "export", "<directory>"]),
        ("probe", ["example-sidecar", "query", "<task>"]),
    ],
)
def test_surface_rejects_unsupported_or_executed_placeholders(tmp_path, field, value):
    surface = {"kind": "verify-exit", "command": ["example-sidecar", "--version"]}
    surface[field] = value
    path = _write_manifest(
        tmp_path,
        tools=[{"name": "example-sidecar", "command": "example-sidecar", "surfaces": [surface]}],
    )

    with pytest.raises(ValueError, match="placeholder"):
        station_manifest.load(str(path))
