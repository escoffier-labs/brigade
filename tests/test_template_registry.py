from __future__ import annotations

import hashlib
import json

import pytest

from brigade import cli, template_registry
from brigade.install import build_render_context, install_selection, resolve_manifests
from brigade.selection import Selection


def test_bundled_registry_schema_and_render_digests():
    payload = template_registry.load_registry()
    assert payload["schema"] == template_registry.SCHEMA
    assert set(payload["profiles"]) == set(template_registry.BUILTIN_PROFILES)
    for name, profile in payload["profiles"].items():
        assert profile["identity"] == f"brigade://template-profiles/{name}"
        selection = Selection(**profile["selection"])
        selection.validate()
        assert payload["renders"][name] == template_registry.compute_profile_renders(selection)


def test_build_registry_matches_committed_snapshot():
    built = template_registry.build_registry()
    committed = template_registry.load_registry()
    assert built["profiles"] == committed["profiles"]
    assert built["renders"] == committed["renders"]


def test_resolve_profile_returns_valid_selection():
    selection = template_registry.resolve_profile("repo-claude")
    assert selection.depth == "repo"
    assert selection.harnesses == ["claude"]
    assert selection.owner == "claude"
    assert selection.includes == []


def test_unknown_profile_raises():
    with pytest.raises(template_registry.UnknownTemplateProfile, match="missing-profile"):
        template_registry.resolve_profile("missing-profile")


def test_check_harness_version_accepts_supported_minimum():
    template_registry.check_harness_version("repo-claude", "claude", "1.2.3")


@pytest.fixture()
def strict_profile_registry(tmp_path):
    selection = Selection(depth="repo", harnesses=["claude"], owner="claude", includes=[])
    payload = {
        "schema": template_registry.SCHEMA,
        "brigade_version": "0.0.0",
        "profiles": {
            "test": {
                "description": "test profile",
                "identity": "brigade://template-profiles/test",
                "selection": {
                    "depth": "repo",
                    "harnesses": ["claude"],
                    "owner": "claude",
                    "includes": [],
                },
                "supported_harness_versions": {
                    "claude": {"min": "2.0.0", "tested": "2.1.0"},
                },
            }
        },
        "renders": {"test": template_registry.compute_profile_renders(selection)},
    }
    path = tmp_path / "template-registry.json"
    path.write_text(template_registry.render_registry(payload), encoding="utf-8")
    return path


def test_check_harness_version_rejects_below_minimum(strict_profile_registry):
    with pytest.raises(template_registry.UnsupportedHarnessVersion, match="below profile minimum"):
        template_registry.check_harness_version(
            "test",
            "claude",
            "1.9.9",
            path=strict_profile_registry,
        )


def test_render_hashes_are_deterministic_for_same_selection():
    selection = template_registry.resolve_profile("workspace-claude-codex")
    first = template_registry.compute_profile_renders(selection)
    second = template_registry.compute_profile_renders(selection)
    assert first == second
    assert first
    assert all(len(item["sha256"]) == 64 for item in first)


def test_render_hash_matches_installed_bytes(tmp_path):
    selection = template_registry.resolve_profile("repo-claude")
    renders = {item["dst"]: item for item in template_registry.compute_profile_renders(selection)}
    assert install_selection(tmp_path, selection) == 0
    for dst, record in renders.items():
        content = (tmp_path / dst).read_bytes()
        assert hashlib.sha256(content).hexdigest() == record["sha256"]
        assert len(content) == record["byte_size"]


def test_build_render_context_uses_owner_inbox_first():
    selection = Selection(
        depth="workspace",
        harnesses=["claude", "codex"],
        owner="codex",
        includes=[],
    )
    context = build_render_context(selection)
    assert context["handoff_inbox"] == ".codex/memory-handoffs/"


def test_init_profile_installs_named_selection(tmp_path, capsys):
    rc = cli.main(["init", "--target", str(tmp_path), "--profile", "repo-claude", "--no-wire"])
    assert rc == 0
    assert (tmp_path / "CLAUDE.md").is_file()
    capsys.readouterr()


def test_init_profile_unknown_returns_error(tmp_path, capsys):
    rc = cli.main(["init", "--target", str(tmp_path), "--profile", "missing-profile"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "unknown template profile" in err


def test_init_profile_conflicts_with_depth(tmp_path, capsys):
    rc = cli.main(
        [
            "init",
            "--target",
            str(tmp_path),
            "--profile",
            "repo-claude",
            "--depth",
            "repo",
        ]
    )
    assert rc == 2
    assert "--profile cannot be combined" in capsys.readouterr().err


def test_init_legacy_default_without_profile_unchanged(tmp_path):
    """Profile-less installs keep the existing depth/harness default path."""
    rc = cli.main(
        [
            "init",
            "--target",
            str(tmp_path),
            "--depth",
            "repo",
            "--harnesses",
            "claude",
            "--no-wire",
        ]
    )
    assert rc == 0
    files, _, _ = resolve_manifests(Selection(depth="repo", harnesses=["claude"], owner="claude"))
    for entry in files:
        assert (tmp_path / entry["dst"]).is_file()


def test_registry_rejects_stale_render_digest(tmp_path):
    payload = template_registry.build_registry(["repo-claude"])
    payload["renders"]["repo-claude"][0]["sha256"] = "0" * 64
    path = tmp_path / "template-registry.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="render digest mismatch"):
        template_registry.load_registry(path)
