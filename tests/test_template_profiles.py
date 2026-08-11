from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from brigade import cli, template_profiles
from brigade.install import build_render_context, install_selection, resolve_manifests
from brigade.selection import Selection

FIXTURES = Path(__file__).parents[1] / "docs" / "research" / "fixtures" / "harness-contract.v1"


def test_bundled_snapshot_schema_renders_and_harness_contracts():
    payload = template_profiles.load_snapshot(verify_contracts=True)
    assert payload["schema"] == template_profiles.SCHEMA
    assert set(payload["profiles"]) == set(template_profiles.BUILTIN_PROFILES)
    for name, profile in payload["profiles"].items():
        assert "identity" not in profile
        assert "supported_harness_versions" not in profile
        selection = Selection(**profile["selection"])
        selection.validate()
        assert payload["renders"][name] == template_profiles.compute_profile_renders(selection)
        expected = template_profiles.harness_compatibility_from_contracts(selection.harnesses)
        assert profile["harness_compatibility"] == expected
        for harness_id, compat in profile["harness_compatibility"].items():
            assert compat["contract_id"] == template_profiles.HARNESS_CONTRACT_FIXTURES[harness_id]
            for capability in compat["capabilities"]:
                assert set(capability) == {"id", "tested_version", "provenance", "evidence"}
                assert capability["evidence"]


def test_build_snapshot_matches_committed_snapshot():
    built = template_profiles.build_snapshot()
    committed = template_profiles.load_snapshot(verify_contracts=True)
    assert built["profiles"] == committed["profiles"]
    assert built["renders"] == committed["renders"]
    assert built["brigade_version"] == committed["brigade_version"]


def test_resolve_profile_returns_valid_selection():
    selection = template_profiles.resolve_profile("repo-claude")
    assert selection.depth == "repo"
    assert selection.harnesses == ["claude"]
    assert selection.owner == "claude"
    assert selection.includes == []


def test_unknown_profile_raises():
    with pytest.raises(template_profiles.UnknownTemplateProfile, match="missing-profile"):
        template_profiles.resolve_profile("missing-profile")


def test_harness_compatibility_refuses_unmapped_harness():
    with pytest.raises(ValueError, match="no harness-contract.v1 fixture mapping"):
        template_profiles.harness_compatibility_from_contracts(["aider"])


def test_harness_compatibility_tracks_fixture_tested_version_provenance_evidence():
    fixture = json.loads((FIXTURES / "claude-code.json").read_text(encoding="utf-8"))
    projected = template_profiles.harness_compatibility_from_contracts(["claude"])["claude"]
    by_id = {item["id"]: item for item in projected["capabilities"]}
    for capability in fixture["capabilities"]:
        projected_cap = by_id[capability["id"]]
        assert projected_cap["tested_version"] == capability["tested_version"]
        assert projected_cap["provenance"] == capability["provenance"]
        assert projected_cap["evidence"] == [
            {"kind": item["kind"], "reference": item["reference"]} for item in capability["evidence"]
        ]


def test_render_hashes_are_deterministic_for_same_selection():
    selection = template_profiles.resolve_profile("workspace-claude-codex")
    first = template_profiles.compute_profile_renders(selection)
    second = template_profiles.compute_profile_renders(selection)
    assert first == second
    assert first
    assert all(len(item["sha256"]) == 64 for item in first)


def test_render_hash_matches_installed_bytes(tmp_path):
    selection = template_profiles.resolve_profile("repo-claude")
    renders = {item["dst"]: item for item in template_profiles.compute_profile_renders(selection)}
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


def test_init_profile_conflicts_with_harnesses(tmp_path, capsys):
    rc = cli.main(
        [
            "init",
            "--target",
            str(tmp_path),
            "--profile",
            "repo-claude",
            "--harnesses",
            "claude",
        ]
    )
    assert rc == 2
    assert "--profile cannot be combined" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_init_profile_merges_includes_with_first_seen_dedupe(tmp_path):
    rc = cli.main(
        [
            "init",
            "--target",
            str(tmp_path),
            "--profile",
            "repo-claude-full",
            "--include",
            "publisher",
            "--include",
            "repo-extras",
            "--include",
            "publisher",
            "--no-wire",
        ]
    )
    assert rc == 0
    config = json.loads((tmp_path / ".brigade" / "config.json").read_text(encoding="utf-8"))
    assert config["includes"] == ["repo-extras", "publisher"]
    assert (tmp_path / "INSTALL_FOR_AGENTS.md").is_file()


def test_init_profile_owner_override_propagates(tmp_path):
    rc = cli.main(
        [
            "init",
            "--target",
            str(tmp_path),
            "--profile",
            "workspace-claude-codex",
            "--owner",
            "codex",
            "--no-wire",
        ]
    )
    assert rc == 0
    config = json.loads((tmp_path / ".brigade" / "config.json").read_text(encoding="utf-8"))
    assert config["owner"] == "codex"
    assert config["harnesses"] == ["claude", "codex"]


def test_init_profile_full_appends_repo_extras_after_include_merge(tmp_path):
    rc = cli.main(
        [
            "init",
            "--target",
            str(tmp_path),
            "--profile",
            "repo-claude",
            "--include",
            "publisher",
            "--full",
            "--no-wire",
        ]
    )
    assert rc == 0
    config = json.loads((tmp_path / ".brigade" / "config.json").read_text(encoding="utf-8"))
    assert config["includes"] == ["publisher", "repo-extras"]
    assert (tmp_path / "INSTALL_FOR_AGENTS.md").is_file()


def test_init_profile_dry_run_leaves_no_files(tmp_path, capsys):
    before = list(tmp_path.iterdir())
    rc = cli.main(
        [
            "init",
            "--target",
            str(tmp_path),
            "--profile",
            "repo-claude",
            "--dry-run",
            "--no-wire",
        ]
    )
    assert rc == 0
    assert list(tmp_path.iterdir()) == before
    out = capsys.readouterr().out
    assert "[dry-run]" in out
    assert "repo" in out


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


def test_snapshot_rejects_stale_render_digest(tmp_path):
    payload = template_profiles.build_snapshot(["repo-claude"])
    payload["renders"]["repo-claude"][0]["sha256"] = "0" * 64
    path = tmp_path / "template-profile-snapshot.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="render digest mismatch"):
        template_profiles.load_snapshot(path, verify_contracts=True)


def test_snapshot_rejects_stale_harness_compatibility(tmp_path):
    payload = template_profiles.build_snapshot(["repo-claude"])
    payload["profiles"]["repo-claude"]["harness_compatibility"]["claude"]["capabilities"][0]["tested_version"] = "9.9.9"
    path = tmp_path / "template-profile-snapshot.json"
    path.write_text(template_profiles.render_snapshot(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="harness_compatibility does not match"):
        template_profiles.load_snapshot(path, verify_contracts=True)


def test_no_public_registry_vocabulary_in_module_surface():
    assert not hasattr(template_profiles, "check_harness_version")
    assert not hasattr(template_profiles, "UnsupportedHarnessVersion")
    assert "template_registry" not in template_profiles.SCHEMA
    assert "brigade://" not in template_profiles.render_snapshot(template_profiles.build_snapshot(["repo-claude"]))


def test_ci_and_verify_check_template_profile_snapshot():
    root = Path(__file__).resolve().parents[1]
    verify = (root / "scripts" / "verify").read_text(encoding="utf-8")
    ci = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "scripts/template_profile_snapshot.py --check" in verify
    assert "python scripts/template_profile_snapshot.py --check" in ci
