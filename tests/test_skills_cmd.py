from __future__ import annotations

import contextlib
import errno
import io
import json
import ntpath
import os
import shutil
import signal
import stat as stat_module
import sys
from pathlib import Path

import pytest

from brigade import cli, skills_cmd
from brigade.projection import kernel as projection
from brigade.skills_cmd.packs import UnsupportedLexicalPathError, _absolute_lexical_path


def _write_skill(root, name="security-review"):
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Security Review\n\nReview code with the configured tools.\n")
    (skill_dir / "CHANGELOG.md").write_text("# 0.1.0\n\n- Initial reviewed workflow.\n")
    (skill_dir / "skill.json").write_text(
        json.dumps(
            {
                "id": name,
                "title": "Security Review",
                "version": "0.1.0",
                "required_tools": ["git"],
                "required_mcp_servers": ["github"],
                "supported_harnesses": [
                    "codex",
                    "claude",
                    "opencode",
                    "antigravity",
                    "pi",
                    "cursor",
                    "openclaw",
                    "hermes",
                    "mcp",
                ],
                "trust_level": "workspace",
                "tests": ["brigade skills lint security-review"],
            }
        )
    )
    return skill_dir


def test_skills_import_lint_search_and_install_all(tmp_path, capsys):
    source = _write_skill(tmp_path / "source")

    assert skills_cmd.import_skill(target=tmp_path, source=source, json_output=True) == 0
    imported = json.loads(capsys.readouterr().out)
    assert imported["skill_id"] == "security-review"
    assert imported["lint"]["valid"] is True

    assert skills_cmd.search(target=tmp_path, query="security github", json_output=True) == 0
    search = json.loads(capsys.readouterr().out)
    assert search["count"] == 1

    assert skills_cmd.install(workspace=tmp_path, skill="security-review", harness="all", json_output=True) == 0
    install = json.loads(capsys.readouterr().out)
    assert install["receipt"]["targets"] == [
        "codex",
        "claude",
        "opencode",
        "antigravity",
        "pi",
        "cursor",
        "aider",
        "goose",
        "continue",
        "copilot",
        "qwen",
        "kimi",
        "adal",
        "openhands",
        "grok",
        "amp",
        "crush",
        "openclaw",
        "hermes",
        "mcp",
    ]
    codex_skill = tmp_path / ".codex" / "skills" / "security-review" / "SKILL.md"
    assert codex_skill.is_file()
    codex_text = codex_skill.read_text()
    assert codex_text.startswith("---\n")
    assert 'name: "security-review"' in codex_text
    assert "# Security Review" in codex_text
    assert (tmp_path / ".claude" / "skills" / "security-review" / "SKILL.md").is_file()
    assert (tmp_path / ".opencode" / "skills" / "security-review" / "SKILL.md").is_file()
    assert (tmp_path / ".antigravity" / "skills" / "security-review" / "SKILL.md").is_file()
    assert (tmp_path / ".pi" / "skills" / "security-review" / "SKILL.md").is_file()
    assert (tmp_path / ".cursor" / "skills" / "security-review" / "SKILL.md").is_file()
    assert not (tmp_path / ".agents" / "skills" / "security-review" / "SKILL.md").exists()
    assert (tmp_path / ".openclaw" / "skills" / "security-review" / "SKILL.md").is_file()
    # hermes installs into the user's Hermes store (where Hermes actually reads), not repo-local
    assert (Path(os.environ["HERMES_HOME"]) / "skills" / "brigade-imports" / "security-review" / "SKILL.md").is_file()
    assert not (tmp_path / ".hermes" / "skills" / "security-review").exists()
    assert (tmp_path / ".brigade" / "skills" / "mcp-resources" / "security-review" / "SKILL.md").is_file()


def test_skills_cli_install_uses_target_for_harness(tmp_path):
    source = _write_skill(tmp_path / "source")
    assert cli.main(["skills", "import", str(source), "--target", str(tmp_path), "--json"]) == 0

    assert (
        cli.main(["skills", "install", "security-review", "--workspace", str(tmp_path), "--target", "all", "--json"])
        == 0
    )

    assert (tmp_path / ".codex" / "skills" / "security-review" / "SKILL.md").is_file()


def test_skills_sync_plans_mixed_registry_state_across_harnesses(tmp_path, capsys):
    for name in ("current", "missing", "changed", "unreviewed"):
        source = _write_skill(tmp_path / "sources", name=name)
        metadata_path = source / "skill.json"
        metadata = json.loads(metadata_path.read_text())
        metadata["supported_harnesses"] = ["codex", "cursor"]
        if name == "unreviewed":
            metadata["trust_level"] = "unreviewed"
        metadata_path.write_text(json.dumps(metadata))
        assert skills_cmd.import_skill(target=tmp_path, source=source, json_output=True) == 0
        capsys.readouterr()

    for skill in ("current", "changed"):
        for harness in ("codex", "cursor"):
            assert (
                skills_cmd.install(
                    workspace=tmp_path,
                    skill=f"registry:{skill}",
                    harness=harness,
                    json_output=True,
                )
                == 0
            )
            capsys.readouterr()
    changed = tmp_path / ".brigade" / "skills" / "registry" / "changed" / "SKILL.md"
    changed.write_text(changed.read_text() + "\nUpdated workflow.\n")

    assert (
        skills_cmd.sync(
            workspace=tmp_path,
            harness="all",
            trust="workspace",
            write=False,
            json_output=True,
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    rows = {(row["skill_id"], row["harness"]): row for row in payload["items"]}

    for harness in ("codex", "cursor"):
        assert rows[("current", harness)]["state"] == "current"
        assert rows[("current", harness)]["action"] == "none"
        assert rows[("missing", harness)]["state"] == "missing"
        assert rows[("missing", harness)]["action"] == "install"
        assert rows[("changed", harness)]["state"] == "changed"
        assert rows[("changed", harness)]["action"] == "update"
        assert rows[("unreviewed", harness)]["state"] == "excluded"
        assert rows[("unreviewed", harness)]["reason"] == "trust level unreviewed is below workspace"
    assert payload["write"] is False
    assert payload["counts"]["current"] == 2
    assert payload["counts"]["missing"] == 2
    assert payload["counts"]["changed"] == 2
    assert not (tmp_path / ".codex" / "skills" / "missing").exists()


def test_skills_sync_write_installs_only_missing_and_changed_pairs(tmp_path, capsys):
    for name in ("current", "missing", "changed"):
        source = _write_skill(tmp_path / "sources", name=name)
        metadata_path = source / "skill.json"
        metadata = json.loads(metadata_path.read_text())
        metadata["supported_harnesses"] = ["codex", "cursor"]
        metadata_path.write_text(json.dumps(metadata))
        assert skills_cmd.import_skill(target=tmp_path, source=source, json_output=True) == 0
        capsys.readouterr()

    for skill in ("current", "changed"):
        for harness in ("codex", "cursor"):
            assert (
                skills_cmd.install(
                    workspace=tmp_path,
                    skill=f"registry:{skill}",
                    harness=harness,
                    json_output=True,
                )
                == 0
            )
            capsys.readouterr()
    current_receipts = {
        harness: json.loads((tmp_path / ".brigade" / "skills" / "installs" / f"current-{harness}.json").read_text())[
            "receipt_id"
        ]
        for harness in ("codex", "cursor")
    }
    changed = tmp_path / ".brigade" / "skills" / "registry" / "changed" / "SKILL.md"
    changed.write_text(changed.read_text() + "\nUpdated workflow.\n")

    assert skills_cmd.sync(workspace=tmp_path, harness="all", trust="workspace", write=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    rows = {(row["skill_id"], row["harness"]): row for row in payload["items"]}

    for harness in ("codex", "cursor"):
        assert rows[("current", harness)]["result"] == "unchanged"
        assert rows[("missing", harness)]["result"] == "installed"
        assert rows[("changed", harness)]["result"] == "updated"
        assert (tmp_path / f".{harness}" / "skills" / "missing" / "SKILL.md").is_file()
        changed_receipt = json.loads(
            (tmp_path / ".brigade" / "skills" / "installs" / f"changed-{harness}.json").read_text()
        )
        assert changed_receipt["rollback_snapshot"]
        current_receipt = json.loads(
            (tmp_path / ".brigade" / "skills" / "installs" / f"current-{harness}.json").read_text()
        )
        assert current_receipt["receipt_id"] == current_receipts[harness]
    assert payload["applied"]["installed"] == 2
    assert payload["applied"]["updated"] == 2
    assert payload["applied"]["failed"] == 0
    assert payload["projection"]["projector"] == "skills"
    assert payload["projection"]["terminal_state"] == "committed"


def test_skills_sync_write_with_only_current_targets_creates_no_batch_material(tmp_path, capsys):
    source = _write_skill(tmp_path / "source", name="current-only")
    metadata_path = source / "skill.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["supported_harnesses"] = ["codex"]
    metadata_path.write_text(json.dumps(metadata))
    assert skills_cmd.import_skill(target=tmp_path, source=source, json_output=True) == 0
    capsys.readouterr()
    assert skills_cmd.install(workspace=tmp_path, skill="registry:current-only", harness="codex", json_output=True) == 0
    capsys.readouterr()

    assert skills_cmd.sync(workspace=tmp_path, harness="codex", trust="workspace", write=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["items"][0]["result"] == "unchanged"
    assert payload["projection"] is None


def test_skills_sync_human_write_output_includes_batch_operation_id(tmp_path, capsys):
    source = _write_skill(tmp_path / "source", name="human-batch")
    metadata_path = source / "skill.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["supported_harnesses"] = ["codex"]
    metadata_path.write_text(json.dumps(metadata))
    assert skills_cmd.import_skill(target=tmp_path, source=source, json_output=True) == 0
    capsys.readouterr()

    assert skills_cmd.sync(workspace=tmp_path, harness="codex", trust="workspace", write=True) == 0
    output = capsys.readouterr().out

    assert "projection operation: skills-" in output
    assert "projection status: committed" in output


def test_skills_sync_restores_all_outputs_after_projection_write_failure(tmp_path, capsys, monkeypatch):
    source = _write_skill(tmp_path / "source", name="partial")
    metadata_path = source / "skill.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["supported_harnesses"] = ["codex", "cursor"]
    metadata_path.write_text(json.dumps(metadata))
    assert skills_cmd.import_skill(target=tmp_path, source=source, json_output=True) == 0
    capsys.readouterr()

    real_write = projection._publish_bytes_via_parent
    cursor_parent = tmp_path / ".cursor" / "skills" / "partial"

    def fail_cursor_bundle(parent_fd: int, name: str, data: bytes) -> None:
        if name == "SKILL.md" and cursor_parent.exists():
            parent_info = os.fstat(parent_fd)
            expected = os.stat(cursor_parent)
            if (parent_info.st_dev, parent_info.st_ino) == (expected.st_dev, expected.st_ino):
                raise OSError("simulated cursor write failure")
        return real_write(parent_fd, name, data)

    monkeypatch.setattr(projection, "_publish_bytes_via_parent", fail_cursor_bundle)
    assert skills_cmd.sync(workspace=tmp_path, harness="all", trust="workspace", write=True, json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    rows = {(row["skill_id"], row["harness"]): row for row in payload["items"]}

    assert rows[("partial", "codex")]["result"] == "restored"
    assert rows[("partial", "cursor")]["result"] == "restored"
    assert payload["projection"]["terminal_state"] == "restored"
    assert not (tmp_path / ".codex" / "skills" / "partial" / "SKILL.md").exists()
    assert not (tmp_path / ".brigade" / "skills" / "installs" / "partial-codex.json").exists()
    assert not (tmp_path / ".brigade" / "skills" / "installs" / "partial-cursor.json").exists()
    assert not (tmp_path / ".brigade" / "skills" / "installs" / "history.jsonl").exists()


def test_skills_sync_ignores_other_projectors_pending_recovery(tmp_path, capsys):
    source = _write_skill(tmp_path / "source", name="ignore-mcp-recovery")
    metadata_path = source / "skill.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["supported_harnesses"] = ["codex"]
    metadata_path.write_text(json.dumps(metadata))
    assert skills_cmd.import_skill(target=tmp_path, source=source, json_output=True) == 0
    capsys.readouterr()
    foreign = projection.build_plan(
        operation_id="mcp-pending",
        projector="mcp",
        source_fingerprint="fixture",
        mutations=[
            projection.mutation(
                destination=tmp_path / "foreign-mcp.txt",
                mutation="create",
                expected_before=projection.ABSENT,
                desired_after=projection.content_digest(b"pending\n"),
                staged_bytes=b"pending\n",
            )
        ],
        target=tmp_path,
    )
    projection.write_crash_fixture(foreign, target=tmp_path, committed_count=0)

    assert skills_cmd.sync(workspace=tmp_path, harness="codex", trust="workspace", write=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["projection"]["projector"] == "skills"
    assert (tmp_path / ".codex" / "skills" / "ignore-mcp-recovery" / "SKILL.md").is_file()


def test_skills_sync_preserves_foreign_bundle_without_receipt_or_history_write(tmp_path, capsys):
    source = _write_skill(tmp_path / "source", name="foreign-bundle")
    metadata_path = source / "skill.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["supported_harnesses"] = ["codex"]
    metadata_path.write_text(json.dumps(metadata))
    assert skills_cmd.import_skill(target=tmp_path, source=source, json_output=True) == 0
    capsys.readouterr()
    foreign = tmp_path / ".codex" / "skills" / "foreign-bundle"
    foreign.mkdir(parents=True)
    (foreign / "SKILL.md").write_text("foreign\n")

    assert skills_cmd.sync(workspace=tmp_path, harness="codex", trust="workspace", write=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["projection"] is None
    assert (foreign / "SKILL.md").read_text() == "foreign\n"
    assert not (tmp_path / ".brigade" / "skills" / "installs" / "foreign-bundle-codex.json").exists()
    assert not (tmp_path / ".brigade" / "skills" / "installs" / "history.jsonl").exists()


def test_skills_sync_blocks_only_its_own_pending_recovery(tmp_path, capsys):
    source = _write_skill(tmp_path / "source", name="pending-skills-recovery")
    metadata_path = source / "skill.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["supported_harnesses"] = ["codex"]
    metadata_path.write_text(json.dumps(metadata))
    assert skills_cmd.import_skill(target=tmp_path, source=source, json_output=True) == 0
    capsys.readouterr()
    pending = projection.build_plan(
        operation_id="skills-pending",
        projector="skills",
        source_fingerprint="fixture",
        mutations=[
            projection.mutation(
                destination=tmp_path / "pending-skills.txt",
                mutation="create",
                expected_before=projection.ABSENT,
                desired_after=projection.content_digest(b"pending\n"),
                staged_bytes=b"pending\n",
            )
        ],
        target=tmp_path,
    )
    projection.write_crash_fixture(pending, target=tmp_path, committed_count=0)

    assert skills_cmd.sync(workspace=tmp_path, harness="codex", trust="workspace", write=True, json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)

    assert payload["terminal_state"] == "recovery-required"
    assert payload["recovery"][0]["operation_id"] == "skills-pending"
    assert not (tmp_path / ".codex" / "skills" / "pending-skills-recovery").exists()


def test_skills_sync_excludes_unavailable_hermes_target(tmp_path, capsys, monkeypatch):
    source = _write_skill(tmp_path / "source", name="no-hermes")
    assert skills_cmd.import_skill(target=tmp_path, source=source, json_output=True) == 0
    capsys.readouterr()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "absent-hermes"))

    assert skills_cmd.sync(workspace=tmp_path, harness="all", trust="workspace", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    row = next(item for item in payload["items"] if item["skill_id"] == "no-hermes" and item["harness"] == "hermes")

    assert row["state"] == "excluded"
    assert row["result"] == "excluded"
    assert row["reason"] == "Hermes home not found (is Hermes installed?)"
    assert payload["counts"]["blocked"] == 0


def test_skills_sync_uses_same_metadata_fallback_for_plan_and_install(tmp_path, capsys):
    source = _write_skill(tmp_path / "source", name="fallback")
    metadata_path = source / "skill.json"
    metadata = json.loads(metadata_path.read_text())
    metadata.pop("title")
    metadata.pop("description", None)
    metadata["supported_harnesses"] = ["codex"]
    metadata_path.write_text(json.dumps(metadata))
    assert skills_cmd.import_skill(target=tmp_path, source=source, json_output=True) == 0
    capsys.readouterr()

    assert skills_cmd.sync(workspace=tmp_path, harness="codex", trust="workspace", write=True) == 0
    capsys.readouterr()
    assert skills_cmd.sync(workspace=tmp_path, harness="codex", trust="workspace", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["items"][0]["state"] == "current"
    assert payload["items"][0]["drift"]["content_changed"] is False


def test_skills_sync_enforces_higher_trust_floors(tmp_path, capsys):
    for name, trust_level in (("workspace-only", "workspace"), ("team-skill", "team"), ("public-skill", "public")):
        source = _write_skill(tmp_path / "sources", name=name)
        metadata_path = source / "skill.json"
        metadata = json.loads(metadata_path.read_text())
        metadata["trust_level"] = trust_level
        metadata["supported_harnesses"] = ["cursor"]
        metadata_path.write_text(json.dumps(metadata))
        assert skills_cmd.import_skill(target=tmp_path, source=source, json_output=True) == 0
        capsys.readouterr()

    assert skills_cmd.sync(workspace=tmp_path, harness="cursor", trust="team", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    rows = {row["skill_id"]: row for row in payload["items"]}

    assert rows["workspace-only"]["state"] == "excluded"
    assert rows["team-skill"]["state"] == "missing"
    assert rows["public-skill"]["state"] == "missing"

    assert skills_cmd.sync(workspace=tmp_path, harness="cursor", trust="public", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    rows = {row["skill_id"]: row for row in payload["items"]}
    assert rows["team-skill"]["state"] == "excluded"
    assert rows["public-skill"]["state"] == "missing"


def test_skills_sync_excludes_unsupported_and_blocks_unknown_provenance(tmp_path, capsys):
    unsupported = _write_skill(tmp_path / "sources", name="unsupported")
    metadata_path = unsupported / "skill.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["supported_harnesses"] = ["codex"]
    metadata_path.write_text(json.dumps(metadata))
    assert skills_cmd.import_skill(target=tmp_path, source=unsupported, json_output=True) == 0
    capsys.readouterr()

    unknown = _write_skill(tmp_path / "sources", name="unknown-copy")
    metadata_path = unknown / "skill.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["supported_harnesses"] = ["cursor"]
    metadata_path.write_text(json.dumps(metadata))
    assert skills_cmd.import_skill(target=tmp_path, source=unknown, json_output=True) == 0
    capsys.readouterr()
    installed = tmp_path / ".cursor" / "skills" / "unknown-copy"
    shutil.copytree(unknown, installed)

    assert skills_cmd.sync(workspace=tmp_path, harness="cursor", trust="workspace", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    rows = {row["skill_id"]: row for row in payload["items"]}

    assert rows["unsupported"]["state"] == "excluded"
    assert rows["unsupported"]["reason"] == "harness cursor is not supported by registry metadata"
    assert rows["unknown-copy"]["state"] == "blocked"
    assert rows["unknown-copy"]["reason"] == "installed copy has no valid provenance receipt"
    assert rows["unknown-copy"]["action"] == "none"


def test_skills_sync_cli_is_dry_run_until_write(tmp_path, capsys):
    source = _write_skill(tmp_path / "source", name="cli-sync")
    assert skills_cmd.import_skill(target=tmp_path, source=source, json_output=True) == 0
    capsys.readouterr()

    command = [
        "skills",
        "sync",
        "--workspace",
        str(tmp_path),
        "--target",
        "cursor",
        "--trust",
        "workspace",
        "--json",
    ]
    assert cli.main(command) == 0
    dry_run = json.loads(capsys.readouterr().out)
    assert dry_run["items"][0]["result"] == "planned"
    assert not (tmp_path / ".cursor" / "skills" / "cli-sync").exists()

    assert cli.main([*command, "--write"]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["items"][0]["result"] == "installed"
    assert (tmp_path / ".cursor" / "skills" / "cli-sync" / "SKILL.md").is_file()


def test_hermes_skill_installs_into_global_hermes_home_for_real_discovery(tmp_path):
    source = _write_skill(tmp_path / "source")
    with contextlib.redirect_stdout(io.StringIO()):
        assert skills_cmd.import_skill(target=tmp_path, source=source, json_output=True) == 0
        assert skills_cmd.install(workspace=tmp_path, skill="security-review", harness="hermes", json_output=True) == 0
    hermes_home = Path(os.environ["HERMES_HOME"])
    assert (hermes_home / "skills" / "brigade-imports" / "security-review" / "SKILL.md").is_file()
    # the repo-local .hermes/skills path, which real Hermes ignores, is NOT used
    assert not (tmp_path / ".hermes" / "skills" / "security-review").exists()


def test_hermes_skill_install_skipped_when_hermes_not_installed(tmp_path, monkeypatch):
    absent = tmp_path / "no-hermes-here"
    monkeypatch.setenv("HERMES_HOME", str(absent))
    source = _write_skill(tmp_path / "source")
    with contextlib.redirect_stdout(io.StringIO()):
        assert skills_cmd.import_skill(target=tmp_path, source=source, json_output=True) == 0
        rc = skills_cmd.install(workspace=tmp_path, skill="security-review", harness="hermes", json_output=True)
    assert rc == 0
    # no ~/.hermes is created for a user who does not run Hermes
    assert not absent.exists()
    # and nothing is projected repo-local either
    assert not (tmp_path / ".hermes" / "skills" / "security-review").exists()


def test_hermes_install_adds_hermes_frontmatter_so_hermes_discovers_it(tmp_path):
    source = _write_skill(tmp_path / "source")
    with contextlib.redirect_stdout(io.StringIO()):
        assert skills_cmd.import_skill(target=tmp_path, source=source, json_output=True) == 0
        assert skills_cmd.install(workspace=tmp_path, skill="security-review", harness="hermes", json_output=True) == 0
    installed = Path(os.environ["HERMES_HOME"]) / "skills" / "brigade-imports" / "security-review" / "SKILL.md"
    text = installed.read_text()
    # real Hermes only lists a local skill whose SKILL.md has YAML frontmatter
    assert text.startswith("---\n")
    assert 'name: "security-review"' in text
    assert "version:" in text
    assert "# Security Review" in text


def test_skills_serve_mcp_reports_resource_contract(capsys, tmp_path):
    source = _write_skill(tmp_path / "source")
    assert skills_cmd.import_skill(target=tmp_path, source=source, json_output=True) == 0
    capsys.readouterr()

    assert skills_cmd.serve_mcp(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ready"
    assert payload["read_only"] is True
    assert "search_skills" in payload["tools"]
    assert "install_skill" in payload["blocked_tools"]
    assert payload["resource_count"] == 1
    assert payload["registered_resources"][0]["skill"] == "skill://registry/security-review/SKILL.md"
    assert payload["registered_resources"][0]["history"] == "skill://registry/security-review/history.json"


def test_skills_inbox_add_diff_accept_and_reject(tmp_path, capsys):
    source = _write_skill(tmp_path / "source")

    assert skills_cmd.inbox_add(target=tmp_path, source=source, summary="review me", json_output=True) == 0
    proposal = json.loads(capsys.readouterr().out)
    proposal_id = proposal["proposal_id"]
    assert proposal["status"] == "pending"

    assert skills_cmd.inbox_list(target=tmp_path, json_output=True) == 0
    listing = json.loads(capsys.readouterr().out)
    assert listing["proposal_count"] == 1

    assert skills_cmd.inbox_diff(target=tmp_path, proposal_id=proposal_id, json_output=True) == 0
    diff = json.loads(capsys.readouterr().out)
    assert any("Security Review" in line for line in diff["diff"])

    assert skills_cmd.inbox_accept(target=tmp_path, proposal_id=proposal_id, json_output=True) == 0
    accepted = json.loads(capsys.readouterr().out)
    assert accepted["status"] == "accepted"
    assert (tmp_path / ".brigade" / "skills" / "registry" / "security-review" / "SKILL.md").is_file()

    other = _write_skill(tmp_path / "source2", name="docs-review")
    assert skills_cmd.inbox_add(target=tmp_path, source=other, json_output=True) == 0
    other_proposal = json.loads(capsys.readouterr().out)
    assert (
        skills_cmd.inbox_reject(
            target=tmp_path, proposal_id=other_proposal["proposal_id"], reason="duplicate", json_output=True
        )
        == 0
    )
    rejected = json.loads(capsys.readouterr().out)
    assert rejected["status"] == "rejected"
    assert rejected["reason"] == "duplicate"


def test_inbox_add_same_second_distinct_proposals(tmp_path, capsys):
    source = _write_skill(tmp_path / "source")

    assert skills_cmd.inbox_add(target=tmp_path, source=source, summary="first", json_output=True) == 0
    first = json.loads(capsys.readouterr().out)
    assert skills_cmd.inbox_add(target=tmp_path, source=source, summary="second", json_output=True) == 0
    second = json.loads(capsys.readouterr().out)

    assert first["proposal_id"] != second["proposal_id"]
    inbox = tmp_path / ".brigade" / "skills" / "inbox"
    assert (inbox / first["proposal_id"]).is_dir()
    assert (inbox / second["proposal_id"]).is_dir()
    assert first["status"] == "pending"
    assert second["status"] == "pending"


def test_inbox_reject_after_accept_fails(tmp_path, capsys):
    source = _write_skill(tmp_path / "source")
    assert skills_cmd.inbox_add(target=tmp_path, source=source, summary="review me", json_output=True) == 0
    proposal_id = json.loads(capsys.readouterr().out)["proposal_id"]
    assert skills_cmd.inbox_accept(target=tmp_path, proposal_id=proposal_id, json_output=True) == 0
    capsys.readouterr()

    rc = skills_cmd.inbox_reject(target=tmp_path, proposal_id=proposal_id, reason="too late")

    assert rc == 2
    captured = capsys.readouterr()
    assert "pending" in captured.err
    meta = json.loads((tmp_path / ".brigade" / "skills" / "inbox" / proposal_id / "proposal.json").read_text())
    assert meta["status"] == "accepted"
    assert (tmp_path / ".brigade" / "skills" / "registry" / "security-review" / "SKILL.md").is_file()


def test_inbox_accept_rereads_current_state(tmp_path, capsys, monkeypatch):
    source = _write_skill(tmp_path / "source")
    assert skills_cmd.inbox_add(target=tmp_path, source=source, summary="review me", json_output=True) == 0
    proposal_id = json.loads(capsys.readouterr().out)["proposal_id"]
    meta_path = tmp_path / ".brigade" / "skills" / "inbox" / proposal_id / "proposal.json"
    holds = {"n": 0}
    real_held = skills_cmd._held_state_root

    @contextlib.contextmanager
    def flip_before_accept_hold(target):
        holds["n"] += 1
        if holds["n"] == 2:
            current = json.loads(meta_path.read_text())
            current["status"] = "rejected"
            current["reason"] = "flipped-before-lock"
            meta_path.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
        with real_held(target) as anchor:
            yield anchor

    monkeypatch.setattr(skills_cmd, "_held_state_root", flip_before_accept_hold)

    rc = skills_cmd.inbox_accept(target=tmp_path, proposal_id=proposal_id)

    assert rc == 2
    assert "pending" in capsys.readouterr().err
    meta = json.loads(meta_path.read_text())
    assert meta["status"] == "rejected"
    assert meta["reason"] == "flipped-before-lock"
    assert not (tmp_path / ".brigade" / "skills" / "registry" / "security-review").exists()


def test_changelog_payload_rejects_escaping_path_when_not_contained(tmp_path):
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Skill\n")
    secret = tmp_path / "secret-changelog.md"
    secret.write_text("# Secret heading\n\noutside file\n")

    payload = skills_cmd._changelog_payload(skill_dir, {"changelog_path": "../secret-changelog.md"}, contain=False)

    assert payload["present"] is False
    assert payload["path"] is None
    assert payload["fingerprint"] is None
    assert payload["headings"] == []


def test_skills_cli_inbox_and_adapters(tmp_path, capsys):
    source = _write_skill(tmp_path / "source")
    assert cli.main(["skills", "inbox", "add", str(source), "--target", str(tmp_path), "--json"]) == 0
    proposal = json.loads(capsys.readouterr().out)

    assert cli.main(["skills", "inbox", "show", proposal["proposal_id"], "--target", str(tmp_path), "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["proposal_id"] == proposal["proposal_id"]

    assert cli.main(["skills", "adapters", "init", "--target", str(tmp_path), "--json"]) == 0
    init_payload = json.loads(capsys.readouterr().out)
    assert init_payload["adapter_count"] == 0

    assert cli.main(["skills", "adapters", "list", "--target", str(tmp_path), "--include-planned", "--json"]) == 0
    adapters = json.loads(capsys.readouterr().out)
    ids = {item["id"] for item in adapters["adapters"]}
    assert {"codex", "cursor", "antigravity", "pi"} <= ids
    antigravity = next(item for item in adapters["adapters"] if item["id"] == "antigravity")
    assert antigravity["status"] == "built-in"
    pi = next(item for item in adapters["adapters"] if item["id"] == "pi")
    assert pi["status"] == "built-in"
    cursor = next(item for item in adapters["adapters"] if item["id"] == "cursor")
    assert cursor["status"] == "built-in"

    assert cli.main(["skills", "adapters", "show", "cursor", "--target", str(tmp_path), "--json"]) == 0
    cursor = json.loads(capsys.readouterr().out)
    assert cursor["status"] == "built-in"


def test_skills_compatibility_reports_installed_and_planned_adapters(tmp_path, capsys):
    source = _write_skill(tmp_path / "source")
    assert skills_cmd.import_skill(target=tmp_path, source=source, json_output=True) == 0
    capsys.readouterr()
    assert skills_cmd.install(workspace=tmp_path, skill="security-review", harness="codex", json_output=True) == 0
    capsys.readouterr()

    assert skills_cmd.compatibility(target=tmp_path, skill="security-review", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    by_id = {row["id"]: row for row in payload["adapters"]}
    assert by_id["codex"]["installed"] is True
    assert by_id["codex"]["render_valid"] is True
    assert by_id["codex"]["render_errors"] == []
    assert by_id["codex"]["installed_version"] == "0.1.0"
    assert by_id["codex"]["install_history_count"] == 1
    assert payload["trust_score"]["score"] > 0
    assert payload["changelog"]["present"] is True
    assert "adapter planned" not in by_id["cursor"]["blockers"]
    assert by_id["cursor"]["render_valid"] is True


def test_skills_compatibility_does_not_count_partial_install_directory(tmp_path, capsys):
    source = _write_skill(tmp_path / "source")
    assert skills_cmd.import_skill(target=tmp_path, source=source, json_output=True) == 0
    capsys.readouterr()
    partial = tmp_path / ".cursor" / "skills" / "security-review"
    partial.mkdir(parents=True)

    payload = skills_cmd._compatibility_payload(tmp_path, "registry:security-review")
    cursor = next(row for row in payload["adapters"] if row["id"] == "cursor")

    assert cursor["installed"] is False
    assert cursor["drift"]["overall"] == "missing"


def test_skills_compatibility_hides_fields_from_malformed_receipt(tmp_path, capsys):
    assert skills_cmd.install(workspace=tmp_path, skill="brigade-work", harness="cursor", json_output=True) == 0
    capsys.readouterr()
    receipt_path = tmp_path / ".brigade" / "skills" / "installs" / "brigade-work-cursor.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["source"].pop("kind")
    receipt_path.write_text(json.dumps(receipt))

    payload = skills_cmd._compatibility_payload(tmp_path, "brigade-work")
    cursor = next(row for row in payload["adapters"] if row["id"] == "cursor")

    assert cursor["drift"]["receipt_known"] is False
    assert cursor["installed_at"] is None
    assert cursor["installed_version"] is None
    assert cursor["installed_source_fingerprint"] is None
    assert cursor["installed_render_fingerprint"] is None
    assert cursor["version_drift"] is False


def test_skills_compatibility_human_output_lists_adapters(tmp_path, capsys):
    source = _write_skill(tmp_path / "source")
    assert skills_cmd.import_skill(target=tmp_path, source=source, json_output=True) == 0
    capsys.readouterr()

    assert skills_cmd.compatibility(target=tmp_path, skill="security-review", json_output=False) == 0
    out = capsys.readouterr().out
    assert "skill compatibility: security-review" in out
    assert "valid: true" in out
    assert "- codex [" in out


def test_skills_serve_mcp_human_output_counts_resources(tmp_path, capsys):
    source = _write_skill(tmp_path / "source")
    assert skills_cmd.import_skill(target=tmp_path, source=source, json_output=True) == 0
    capsys.readouterr()

    assert skills_cmd.serve_mcp(target=tmp_path, json_output=False) == 0
    out = capsys.readouterr().out
    assert "skills MCP resources: ready read_only=true" in out
    assert "registered_resources: 1" in out


def test_skills_lint_can_validate_harness_rendering(tmp_path, capsys):
    source = _write_skill(tmp_path / "source")
    assert skills_cmd.import_skill(target=tmp_path, source=source, json_output=True) == 0
    capsys.readouterr()

    assert skills_cmd.lint(target=tmp_path, skill="security-review", harness="codex", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["harness"] == "codex"
    assert payload["render_errors"] == []
    assert payload["valid"] is True

    assert skills_cmd.lint(target=tmp_path, skill="security-review", harness="cursor", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is True
    assert payload["render_errors"] == []


def test_skills_doctor_and_import_issues_surface_registry_health(tmp_path, capsys):
    source = _write_skill(tmp_path / "source", name="rough-skill")
    (source / "CHANGELOG.md").unlink()
    (source / "skill.json").write_text(
        json.dumps(
            {
                "id": "rough-skill",
                "title": "Rough Skill",
                "version": "0.1.0",
                "trust_level": "unreviewed",
                "tests": [],
            }
        )
    )
    assert skills_cmd.import_skill(target=tmp_path, source=source, json_output=True) == 0
    capsys.readouterr()

    assert skills_cmd.doctor(target=tmp_path, json_output=True) == 0
    doctor = json.loads(capsys.readouterr().out)
    issue_types = {issue["issue_type"] for issue in doctor["issues"]}
    assert {"unreviewed_trust", "tests_missing", "changelog_missing"} <= issue_types

    assert skills_cmd.import_issues(target=tmp_path, json_output=True) == 0
    imported = json.loads(capsys.readouterr().out)
    assert imported["source"] == "skill-registry"
    assert imported["imported_count"] >= 3
    assert all(item["source"] == "skill-registry" for item in imported["imports"])


def test_skills_codex_install_preserves_frontmatter_and_adds_missing_keys(tmp_path, capsys):
    source = _write_skill(tmp_path / "source")
    (source / "SKILL.md").write_text("---\nmode: careful\n---\n# Security Review\n\nReview code.\n")
    assert skills_cmd.import_skill(target=tmp_path, source=source, json_output=True) == 0
    capsys.readouterr()

    assert skills_cmd.install(workspace=tmp_path, skill="security-review", harness="codex", json_output=True) == 0
    capsys.readouterr()

    text = (tmp_path / ".codex" / "skills" / "security-review" / "SKILL.md").read_text()
    assert text.startswith("---\n")
    assert "mode: careful\n" in text
    assert 'name: "security-review"\n' in text
    assert 'description: "Security Review"\n' in text


def test_skills_rollback_restores_previous_install(tmp_path, capsys):
    source = _write_skill(tmp_path / "source")
    assert skills_cmd.import_skill(target=tmp_path, source=source, json_output=True) == 0
    capsys.readouterr()
    assert skills_cmd.install(workspace=tmp_path, skill="security-review", harness="claude", json_output=True) == 0
    capsys.readouterr()
    installed = tmp_path / ".claude" / "skills" / "security-review" / "SKILL.md"
    installed.write_text("# Security Review\n\nchanged locally\n")

    updated = _write_skill(tmp_path / "source2")
    (updated / "SKILL.md").write_text("# Security Review\n\nnew version\n")
    assert skills_cmd.import_skill(target=tmp_path, source=updated, force=True, json_output=True) == 0
    capsys.readouterr()
    assert (
        skills_cmd.install(workspace=tmp_path, skill="security-review", harness="claude", force=True, json_output=True)
        == 0
    )
    capsys.readouterr()
    assert "new version" in installed.read_text()

    assert skills_cmd.rollback(workspace=tmp_path, skill="security-review", harness="claude", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["target"] == "claude"
    assert "changed locally" in installed.read_text()


def test_skills_rollback_restores_transformed_codex_install(tmp_path, capsys):
    source = _write_skill(tmp_path / "source")
    assert skills_cmd.import_skill(target=tmp_path, source=source, json_output=True) == 0
    capsys.readouterr()
    assert skills_cmd.install(workspace=tmp_path, skill="security-review", harness="codex", json_output=True) == 0
    capsys.readouterr()
    installed = tmp_path / ".codex" / "skills" / "security-review" / "SKILL.md"
    installed.write_text('---\nname: "security-review"\ndescription: "local"\n---\n# Local\n')

    updated = _write_skill(tmp_path / "source2")
    (updated / "SKILL.md").write_text("# Security Review\n\nnew version\n")
    assert skills_cmd.import_skill(target=tmp_path, source=updated, force=True, json_output=True) == 0
    capsys.readouterr()
    assert (
        skills_cmd.install(workspace=tmp_path, skill="security-review", harness="codex", force=True, json_output=True)
        == 0
    )
    capsys.readouterr()
    assert "new version" in installed.read_text()

    assert skills_cmd.rollback(workspace=tmp_path, skill="security-review", harness="codex", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["target"] == "codex"
    assert 'description: "local"' in installed.read_text()


def test_skills_rollback_restores_previous_source_provenance(tmp_path, capsys):
    source = _write_skill(tmp_path / "source", name="brigade-work")
    assert skills_cmd.import_skill(target=tmp_path, source=source, json_output=True) == 0
    capsys.readouterr()
    assert (
        skills_cmd.install(
            workspace=tmp_path,
            skill="registry:brigade-work",
            harness="cursor",
            json_output=True,
        )
        == 0
    )
    capsys.readouterr()
    assert (
        skills_cmd.install(
            workspace=tmp_path,
            skill="brigade-work",
            harness="cursor",
            force=True,
            json_output=True,
        )
        == 0
    )
    capsys.readouterr()

    assert skills_cmd.rollback(workspace=tmp_path, skill="brigade-work", harness="cursor", json_output=True) == 0
    rollback_payload = json.loads(capsys.readouterr().out)
    receipt_path = tmp_path / ".brigade" / "skills" / "installs" / "brigade-work-cursor.json"
    receipt = json.loads(receipt_path.read_text())
    row = next(
        item
        for item in skills_cmd._fleet_status_payload(tmp_path)["copies"]
        if item["skill_id"] == "brigade-work" and item["harness"] == "cursor"
    )

    assert rollback_payload["restored_receipt"] is True
    assert receipt["source"]["kind"] == "registry"
    assert row["source"]["kind"] == "registry"
    assert "brigade skills install brigade-work " not in (row["update_command"] or "")


def test_skills_rollback_consumes_snapshots_in_receipt_order(tmp_path, capsys):
    source = _write_skill(tmp_path / "source", name="repeat")
    installed = tmp_path / ".cursor" / "skills" / "repeat" / "SKILL.md"
    (source / "SKILL.md").write_text("# A\n")
    assert skills_cmd.install(workspace=tmp_path, skill=str(source), harness="cursor", json_output=True) == 0
    capsys.readouterr()
    (source / "SKILL.md").write_text("# B\n")
    assert (
        skills_cmd.install(
            workspace=tmp_path,
            skill=str(source),
            harness="cursor",
            force=True,
            json_output=True,
        )
        == 0
    )
    capsys.readouterr()
    (source / "SKILL.md").write_text("# C\n")
    assert (
        skills_cmd.install(
            workspace=tmp_path,
            skill=str(source),
            harness="cursor",
            force=True,
            json_output=True,
        )
        == 0
    )
    capsys.readouterr()

    assert skills_cmd.rollback(workspace=tmp_path, skill="repeat", harness="cursor", json_output=True) == 0
    first = json.loads(capsys.readouterr().out)
    assert installed.read_text() == "# B\n"
    assert first["snapshot_consumed"] is True
    assert not Path(first["snapshot"]).exists()

    assert skills_cmd.rollback(workspace=tmp_path, skill="repeat", harness="cursor", json_output=True) == 0
    second = json.loads(capsys.readouterr().out)
    receipt_path = tmp_path / ".brigade" / "skills" / "installs" / "repeat-cursor.json"
    receipt = json.loads(receipt_path.read_text())
    assert installed.read_text() == "# A\n"
    assert not Path(second["snapshot"]).exists()
    assert receipt["installed"]["skill_fingerprint"] == skills_cmd._text_fingerprint(installed.read_text())


def test_skills_rollback_uses_snapshot_bound_to_current_receipt(tmp_path, capsys):
    source = _write_skill(tmp_path / "source", name="bound")
    installed = tmp_path / ".cursor" / "skills" / "bound" / "SKILL.md"
    (source / "SKILL.md").write_text("# A\n")
    assert skills_cmd.install(workspace=tmp_path, skill=str(source), harness="cursor", json_output=True) == 0
    capsys.readouterr()
    (source / "SKILL.md").write_text("# B\n")
    assert (
        skills_cmd.install(workspace=tmp_path, skill=str(source), harness="cursor", force=True, json_output=True) == 0
    )
    capsys.readouterr()
    receipt_path = tmp_path / ".brigade" / "skills" / "installs" / "bound-cursor.json"
    receipt = json.loads(receipt_path.read_text())
    recorded_snapshot = Path(receipt["rollback_snapshot"])
    orphan = recorded_snapshot.parent / "zzzz-orphan"
    orphan.mkdir()
    (orphan / "SKILL.md").write_text("# orphan\n")

    assert skills_cmd.rollback(workspace=tmp_path, skill="bound", harness="cursor", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)

    assert installed.read_text() == "# A\n"
    assert Path(payload["snapshot"]) == recorded_snapshot
    assert not recorded_snapshot.exists()
    assert orphan.exists()


def test_skills_rollback_rejects_malformed_current_receipt_before_mutation(tmp_path, capsys):
    source = _write_skill(tmp_path / "source", name="malformed-current")
    installed = tmp_path / ".cursor" / "skills" / "malformed-current" / "SKILL.md"
    (source / "SKILL.md").write_text("# A\n")
    assert skills_cmd.install(workspace=tmp_path, skill=str(source), harness="cursor", json_output=True) == 0
    capsys.readouterr()
    (source / "SKILL.md").write_text("# B\n")
    assert (
        skills_cmd.install(workspace=tmp_path, skill=str(source), harness="cursor", force=True, json_output=True) == 0
    )
    capsys.readouterr()
    receipt_path = tmp_path / ".brigade" / "skills" / "installs" / "malformed-current-cursor.json"
    receipt = json.loads(receipt_path.read_text())
    snapshot = Path(receipt["rollback_snapshot"])
    receipt["source"].pop("kind")
    receipt_path.write_text(json.dumps(receipt))

    assert skills_cmd.rollback(workspace=tmp_path, skill="malformed-current", harness="cursor") == 1

    assert "invalid rollback receipt" in capsys.readouterr().err
    assert installed.read_text() == "# B\n"
    assert snapshot.is_dir()
    assert json.loads(receipt_path.read_text()) == receipt


def test_skills_rollback_rejects_malformed_previous_receipt_before_mutation(tmp_path, capsys):
    source = _write_skill(tmp_path / "source", name="malformed-previous")
    installed = tmp_path / ".cursor" / "skills" / "malformed-previous" / "SKILL.md"
    (source / "SKILL.md").write_text("# A\n")
    assert skills_cmd.install(workspace=tmp_path, skill=str(source), harness="cursor", json_output=True) == 0
    capsys.readouterr()
    (source / "SKILL.md").write_text("# B\n")
    assert (
        skills_cmd.install(workspace=tmp_path, skill=str(source), harness="cursor", force=True, json_output=True) == 0
    )
    capsys.readouterr()
    receipt_path = tmp_path / ".brigade" / "skills" / "installs" / "malformed-previous-cursor.json"
    receipt = json.loads(receipt_path.read_text())
    snapshot = Path(receipt["rollback_snapshot"])
    receipt["previous_receipt"]["source"].pop("kind")
    receipt_path.write_text(json.dumps(receipt))

    assert skills_cmd.rollback(workspace=tmp_path, skill="malformed-previous", harness="cursor") == 1

    assert "invalid previous rollback receipt" in capsys.readouterr().err
    assert installed.read_text() == "# B\n"
    assert snapshot.is_dir()
    assert json.loads(receipt_path.read_text()) == receipt


def test_skills_history_and_diff_report_install_drift(tmp_path, capsys):
    source = _write_skill(tmp_path / "source")
    assert skills_cmd.import_skill(target=tmp_path, source=source, json_output=True) == 0
    capsys.readouterr()
    assert skills_cmd.install(workspace=tmp_path, skill="security-review", harness="claude", json_output=True) == 0
    capsys.readouterr()

    assert skills_cmd.history(target=tmp_path, skill="security-review", harness="claude", json_output=True) == 0
    history = json.loads(capsys.readouterr().out)
    assert history["count"] == 1
    assert history["history"][0]["version"] == "0.1.0"
    assert history["history"][0]["render_fingerprint"]

    installed = tmp_path / ".claude" / "skills" / "security-review" / "SKILL.md"
    installed.write_text("# Security Review\n\nchanged locally\n")
    assert skills_cmd.diff(target=tmp_path, skill="security-review", harness="claude", json_output=True) == 0
    diff_payload = json.loads(capsys.readouterr().out)
    assert diff_payload["changed"] is True
    assert any("changed locally" in line for line in diff_payload["diff"])


def test_bundled_install_and_diff_share_canonical_source(tmp_path, capsys):
    stale = _write_skill(tmp_path / ".brigade" / "skills" / "registry", name="brigade-work")
    (stale / "SKILL.md").write_text("# stale registry copy\n")

    assert skills_cmd.install(workspace=tmp_path, skill="brigade-work", harness="cursor", json_output=True) == 0
    installed = json.loads(capsys.readouterr().out)
    receipt = installed["receipt"]["receipts"][0]
    installed_skill = tmp_path / ".cursor" / "skills" / "brigade-work" / "SKILL.md"

    assert "# brigade-work" in installed_skill.read_text()
    assert receipt["schema_version"] == skills_cmd.RECEIPT_SCHEMA_VERSION
    assert receipt["source"]["kind"] == "brigade-bundle"
    assert receipt["source"]["identity"] == "brigade://bundled-skills/brigade-work"
    assert receipt["trust_score"]["trust_level"] == "bundled"
    assert "trust_level is unreviewed" not in receipt["trust_score"]["signals"]
    assert receipt["render"]["fingerprint"] == receipt["installed"]["skill_fingerprint"]
    assert receipt["installed"]["bundle_fingerprint"] == skills_cmd._fingerprint(installed_skill.parent)

    assert skills_cmd.diff(target=tmp_path, skill="brigade-work", harness="cursor", json_output=True) == 0
    diff = json.loads(capsys.readouterr().out)
    assert diff["changed"] is False
    assert diff["drift"] == {
        "content_changed": False,
        "local_edit": "current",
        "overall": "current",
        "receipt_known": True,
        "render": "current",
        "source": "current",
    }


def test_skills_diff_defaults_to_bundled_template_over_stale_registry(tmp_path, capsys):
    stale = _write_skill(tmp_path / ".brigade" / "skills" / "registry", name="brigade-work")
    (stale / "SKILL.md").write_text("# stale registry copy\n")

    assert (
        skills_cmd.install(workspace=tmp_path, skill="registry:brigade-work", harness="claude", json_output=True) == 0
    )
    capsys.readouterr()

    assert skills_cmd.diff(target=tmp_path, skill="brigade-work", harness="claude", json_output=True) == 0
    diff = json.loads(capsys.readouterr().out)
    assert diff["against"] == "bundled"
    assert diff["baseline_skill"] == "bundled:brigade-work"
    assert diff["source"]["kind"] == "brigade-bundle"
    assert diff["changed"] is True
    assert diff["drift"]["content_changed"] is True


def test_skills_diff_registry_selector_still_uses_bundled_by_default(tmp_path, capsys):
    stale = _write_skill(tmp_path / ".brigade" / "skills" / "registry", name="brigade-work")
    (stale / "SKILL.md").write_text("# stale registry copy\n")

    assert (
        skills_cmd.install(workspace=tmp_path, skill="registry:brigade-work", harness="claude", json_output=True) == 0
    )
    capsys.readouterr()

    assert skills_cmd.diff(target=tmp_path, skill="registry:brigade-work", harness="claude", json_output=True) == 0
    diff = json.loads(capsys.readouterr().out)
    assert diff["baseline_skill"] == "bundled:brigade-work"
    assert diff["changed"] is True


def test_skills_diff_against_registry_compares_installed_to_registry(tmp_path, capsys):
    stale = _write_skill(tmp_path / ".brigade" / "skills" / "registry", name="brigade-work")
    (stale / "SKILL.md").write_text("# stale registry copy\n")

    assert (
        skills_cmd.install(workspace=tmp_path, skill="registry:brigade-work", harness="claude", json_output=True) == 0
    )
    capsys.readouterr()

    assert (
        skills_cmd.diff(
            target=tmp_path,
            skill="brigade-work",
            harness="claude",
            against="registry",
            json_output=True,
        )
        == 0
    )
    diff = json.loads(capsys.readouterr().out)
    assert diff["against"] == "registry"
    assert diff["baseline_skill"] == "registry:brigade-work"
    assert diff["source"]["kind"] == "registry"
    assert diff["changed"] is False


def test_skills_diff_matches_bundled_template_reports_clean(tmp_path, capsys):
    assert skills_cmd.install(workspace=tmp_path, skill="brigade-work", harness="cursor", json_output=True) == 0
    capsys.readouterr()

    assert skills_cmd.diff(target=tmp_path, skill="brigade-work", harness="cursor", json_output=True) == 0
    diff = json.loads(capsys.readouterr().out)
    assert diff["baseline_skill"] == "bundled:brigade-work"
    assert diff["changed"] is False
    assert diff["drift"]["content_changed"] is False


def test_explicit_registry_source_does_not_inherit_bundled_review(tmp_path, capsys):
    source = _write_skill(tmp_path / "source", name="brigade-work")
    assert skills_cmd.import_skill(target=tmp_path, source=source, json_output=True) == 0
    imported = json.loads(capsys.readouterr().out)
    # The freshly written entry is linted through its anchored registry
    # identity; the point of this test is that it does not inherit bundled
    # "reviewed" provenance.
    assert imported["lint"]["source"]["kind"] == "registry"
    assert imported["lint"]["source"]["reviewed"] is False
    assert imported["lint"]["trust_score"]["trust_level"] == "workspace"

    assert (
        skills_cmd.install(
            workspace=tmp_path,
            skill="registry:brigade-work",
            harness="claude",
            json_output=True,
        )
        == 0
    )
    receipt = json.loads(capsys.readouterr().out)["receipt"]["receipts"][0]
    assert receipt["source"]["kind"] == "registry"
    assert receipt["source"]["reviewed"] is False
    assert receipt["trust_score"]["trust_level"] == "workspace"


def test_skills_diff_classifies_local_source_and_renderer_drift(tmp_path, capsys, monkeypatch):
    assert skills_cmd.install(workspace=tmp_path, skill="brigade-work", harness="cursor", json_output=True) == 0
    capsys.readouterr()
    installed = tmp_path / ".cursor" / "skills" / "brigade-work" / "SKILL.md"
    installed.write_text(installed.read_text() + "\nlocal edit\n")

    assert skills_cmd.diff(target=tmp_path, skill="brigade-work", harness="cursor", json_output=True) == 0
    local = json.loads(capsys.readouterr().out)
    assert local["drift"]["source"] == "current"
    assert local["drift"]["render"] == "current"
    assert local["drift"]["local_edit"] == "changed"
    assert local["changed"] is True
    assert local["diff"][0].startswith("--- ")
    assert local["diff"][1].startswith("+++ brigade://bundled-skills/brigade-work#cursor")

    assert (
        skills_cmd.install(workspace=tmp_path, skill="brigade-work", harness="cursor", force=True, json_output=True)
        == 0
    )
    capsys.readouterr()
    fake_templates = tmp_path / "fake-templates"
    shutil.copytree(skills_cmd.template_root(), fake_templates)
    fake_skill = fake_templates / "skills" / "brigade-work" / "SKILL.md"
    fake_skill.write_text(fake_skill.read_text() + "\nbundle upgrade\n")
    monkeypatch.setattr(skills_cmd, "template_root", lambda: fake_templates)

    assert skills_cmd.diff(target=tmp_path, skill="brigade-work", harness="cursor", json_output=True) == 0
    source = json.loads(capsys.readouterr().out)
    assert source["drift"]["source"] == "changed"
    assert source["drift"]["render"] == "current"
    assert source["drift"]["local_edit"] == "current"
    assert any("bundle upgrade" in line for line in source["diff"])

    monkeypatch.setattr(skills_cmd, "RENDERER_SCHEMA_VERSION", skills_cmd.RENDERER_SCHEMA_VERSION + 1)
    assert skills_cmd.diff(target=tmp_path, skill="brigade-work", harness="cursor", json_output=True) == 0
    renderer = json.loads(capsys.readouterr().out)
    assert renderer["drift"]["source"] == "changed"
    assert renderer["drift"]["render"] == "changed"


def test_skills_diff_marks_legacy_receipt_unknown(tmp_path, capsys):
    assert skills_cmd.install(workspace=tmp_path, skill="brigade-work", harness="cursor", json_output=True) == 0
    capsys.readouterr()
    receipt = tmp_path / ".brigade" / "skills" / "installs" / "brigade-work-cursor.json"
    receipt.write_text(json.dumps({"skill_id": "brigade-work", "target": "cursor", "fingerprint": "legacy"}))

    assert skills_cmd.diff(target=tmp_path, skill="brigade-work", harness="cursor", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["changed"] is False
    assert payload["drift"]["overall"] == "unknown"
    assert payload["drift"]["receipt_known"] is False
    assert payload["drift"]["source"] == "unknown"
    assert payload["drift"]["render"] == "unknown"
    assert payload["drift"]["local_edit"] == "unknown"


def test_skills_fleet_does_not_repair_missing_copy_with_legacy_receipt(tmp_path):
    receipt = tmp_path / ".brigade" / "skills" / "installs" / "brigade-work-cursor.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text(json.dumps({"skill_id": "brigade-work", "target": "cursor", "fingerprint": "legacy"}))

    payload = skills_cmd._fleet_status_payload(tmp_path)
    row = next(item for item in payload["copies"] if item["skill_id"] == "brigade-work")

    assert row["status"] == "unknown"
    assert row["drift"]["overall"] == "unknown"
    assert row["update_command"] is None


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform cannot create symlinks")
def test_skills_fleet_status_reports_external_symlinked_skill(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside"
    _write_skill(outside, name="external-example")
    skills_root = workspace / ".claude" / "skills"
    skills_root.mkdir(parents=True)
    (skills_root / "external-example").symlink_to(outside / "external-example", target_is_directory=True)

    payload = skills_cmd._fleet_status_payload(workspace)
    row = next(item for item in payload["copies"] if item["skill_id"] == "external-example")

    assert row["harness"] == "claude"
    assert row["status"] == "external"
    assert row["installed_path"] == str(skills_root / "external-example")
    assert row["drift"]["receipt_known"] is False
    assert row["update_command"] is None
    assert payload["checked_count"] == 1
    assert payload["external_count"] == 1


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform cannot create symlinks")
def test_skills_fleet_status_survives_installed_copy_swapped_for_external_symlink(tmp_path, capsys):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    assert skills_cmd.install(workspace=workspace, skill="brigade-work", harness="cursor", json_output=True) == 0
    capsys.readouterr()
    installed = workspace / ".cursor" / "skills" / "brigade-work"
    shutil.rmtree(installed)
    installed.symlink_to(outside, target_is_directory=True)

    assert skills_cmd.fleet_status(target=workspace) == 0
    out = capsys.readouterr().out
    assert "external=1" in out
    assert "brigade-work [cursor] external" in out

    payload = skills_cmd._fleet_status_payload(workspace)
    row = next(item for item in payload["copies"] if item["skill_id"] == "brigade-work")
    assert row["status"] == "external"
    assert row["source"] is not None
    assert row["source"]["kind"] == "brigade-bundle"


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform cannot create symlinks")
def test_skills_fleet_status_reports_copies_under_external_skills_root(tmp_path, capsys):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    shared = tmp_path / "shared-skills"
    _write_skill(shared, name="root-linked-example")
    (workspace / ".claude").mkdir()
    (workspace / ".claude" / "skills").symlink_to(shared, target_is_directory=True)

    assert skills_cmd.fleet_status(target=workspace) == 0
    assert "root-linked-example [claude] external" in capsys.readouterr().out

    payload = skills_cmd._fleet_status_payload(workspace)
    row = next(item for item in payload["copies"] if item["skill_id"] == "root-linked-example")
    assert row["status"] == "external"


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform cannot create symlinks")
def test_install_dir_still_rejects_external_symlink_target(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = workspace / ".claude" / "skills" / "example"
    link.parent.mkdir(parents=True)
    link.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="escapes workspace"):
        skills_cmd._install_dir(workspace, "claude", "example")


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform cannot create symlinks")
def test_install_refuses_symlinked_brigade_state_root(tmp_path, capsys):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    source = _write_skill(tmp_path / "source")
    assert skills_cmd.import_skill(target=workspace, source=source) == 0
    outside_state = tmp_path / "outside-state"
    shutil.move(str(workspace / ".brigade"), str(outside_state))
    (workspace / ".brigade").symlink_to(outside_state, target_is_directory=True)
    capsys.readouterr()

    rc = skills_cmd.install(workspace=workspace, skill="security-review", harness="claude", json_output=True)

    assert rc == 2
    captured = capsys.readouterr()
    assert "state root" in captured.err
    installs = outside_state / "skills" / "installs"
    if installs.is_dir():
        assert not any(installs.iterdir())
    assert not (workspace / ".claude" / "skills").exists()


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform cannot create symlinks")
def test_import_refuses_symlinked_brigade_state_root(tmp_path, capsys):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    source = _write_skill(tmp_path / "source", name="state-root-victim")
    assert skills_cmd.import_skill(target=workspace, source=_write_skill(tmp_path / "seed")) == 0
    outside_state = tmp_path / "outside-state"
    shutil.move(str(workspace / ".brigade"), str(outside_state))
    (workspace / ".brigade").symlink_to(outside_state, target_is_directory=True)
    capsys.readouterr()

    payload, error, rc = skills_cmd._registry_import_payload(
        target=workspace, source=source, skill_id=None, force=False
    )

    assert payload is None
    assert error is not None
    assert "state root" in error
    assert rc == 2
    assert not (outside_state / "skills" / "registry" / "state-root-victim").exists()


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="platform lacks O_NOFOLLOW descriptor anchoring")
def test_state_root_anchor_detects_ancestor_swap_between_validation_and_write(tmp_path):
    workspace = tmp_path / "ws"
    (workspace / ".brigade").mkdir(parents=True)
    anchor = skills_cmd._hold_state_root(workspace)
    try:
        swapped_root = tmp_path / "swapped-state"
        shutil.copytree(workspace / ".brigade", swapped_root)
        (workspace / ".brigade").rmdir()
        (workspace / ".brigade").symlink_to(swapped_root, target_is_directory=True)

        with pytest.raises(skills_cmd.SkillsStatePathError):
            anchor.revalidate()
    finally:
        anchor.close()


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="platform lacks O_NOFOLLOW descriptor anchoring")
def test_write_state_file_lands_relative_to_held_descriptor(tmp_path):
    workspace = tmp_path / "ws"
    (workspace / ".brigade").mkdir(parents=True)
    with skills_cmd._held_state_root(workspace) as anchor:
        skills_cmd._write_state_file(anchor, "skills", "installs", "receipt.json", data=b"{}\n")
        skills_cmd._append_state_line(anchor, "skills", "installs", "history.jsonl", data=b'{"row": true}\n')
        skills_cmd._append_state_line(anchor, "skills", "installs", "history.jsonl", data=b'{"row": 2}\n')

    receipt = workspace / ".brigade" / "skills" / "installs" / "receipt.json"
    history = workspace / ".brigade" / "skills" / "installs" / "history.jsonl"
    assert receipt.read_bytes() == b"{}\n"
    assert history.read_text().count("\n") == 2


def _seed_workspace_with_skill(workspace: Path, tmp_path: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    assert skills_cmd.import_skill(target=workspace, source=_write_skill(tmp_path / "seed")) == 0


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform cannot create symlinks")
def test_import_refuses_symlinked_registry_component(tmp_path):
    workspace = tmp_path / "ws"
    _seed_workspace_with_skill(workspace, tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    registry = workspace / ".brigade" / "skills" / "registry"
    shutil.move(str(registry), str(tmp_path / "real-registry"))
    registry.symlink_to(outside, target_is_directory=True)

    payload, error, rc = skills_cmd._registry_import_payload(
        target=workspace, source=_write_skill(tmp_path / "source"), skill_id=None, force=False
    )

    assert payload is None
    assert error is not None
    assert rc == 2
    assert not any(outside.iterdir())


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform cannot create symlinks")
def test_import_refuses_symlinked_skills_component(tmp_path):
    workspace = tmp_path / "ws"
    _seed_workspace_with_skill(workspace, tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    skills_dir = workspace / ".brigade" / "skills"
    shutil.move(str(skills_dir), str(tmp_path / "real-skills"))
    skills_dir.symlink_to(outside, target_is_directory=True)

    payload, error, rc = skills_cmd._registry_import_payload(
        target=workspace, source=_write_skill(tmp_path / "source"), skill_id=None, force=False
    )

    assert payload is None
    assert error is not None
    assert rc == 2
    assert not any(outside.iterdir())


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform cannot create symlinks")
def test_install_snapshot_refuses_symlinked_rollback_component(tmp_path, capsys):
    workspace = tmp_path / "ws"
    _seed_workspace_with_skill(workspace, tmp_path)
    assert skills_cmd.install(workspace=workspace, skill="security-review", harness="claude", json_output=True) == 0
    capsys.readouterr()
    outside = tmp_path / "outside"
    outside.mkdir()
    rollback = workspace / ".brigade" / "skills" / "rollback"
    rollback.symlink_to(outside, target_is_directory=True)

    rc = skills_cmd.install(
        workspace=workspace, skill="security-review", harness="claude", force=True, json_output=True
    )

    assert rc == 2
    captured = capsys.readouterr()
    assert "state root" in captured.err or "state path" in captured.err
    assert not any(outside.iterdir())
    # refusal happens before the destructive removal of the installed copy
    assert (workspace / ".claude" / "skills" / "security-review" / "SKILL.md").is_file()


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform cannot create symlinks")
def test_install_receipt_write_refuses_symlinked_installs_component(tmp_path, capsys):
    workspace = tmp_path / "ws"
    _seed_workspace_with_skill(workspace, tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    installs = workspace / ".brigade" / "skills" / "installs"
    installs.symlink_to(outside, target_is_directory=True)

    rc = skills_cmd.install(workspace=workspace, skill="security-review", harness="claude", json_output=True)

    assert rc == 2
    assert not any(outside.iterdir())


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform cannot create symlinks")
def test_uninstall_receipt_delete_refuses_symlinked_installs_component(tmp_path, capsys):
    workspace = tmp_path / "ws"
    _seed_workspace_with_skill(workspace, tmp_path)
    assert skills_cmd.install(workspace=workspace, skill="security-review", harness="claude", json_output=True) == 0
    capsys.readouterr()
    outside = tmp_path / "outside"
    outside.mkdir()
    victim_receipt = outside / "security-review-claude.json"
    victim_receipt.write_text("{}\n")
    installs = workspace / ".brigade" / "skills" / "installs"
    shutil.move(str(installs), str(tmp_path / "real-installs"))
    installs.symlink_to(outside, target_is_directory=True)

    rc = skills_cmd.uninstall(workspace=workspace, skill="security-review", harness="claude")

    assert rc == 2
    assert victim_receipt.read_text() == "{}\n"
    # refusal happens before the installed copy is removed
    assert (workspace / ".claude" / "skills" / "security-review").exists()


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform cannot create symlinks")
def test_uninstall_internal_state_delete_refuses_symlinked_resources_component(tmp_path, capsys):
    workspace = tmp_path / "ws"
    _seed_workspace_with_skill(workspace, tmp_path)
    assert skills_cmd.install(workspace=workspace, skill="security-review", harness="mcp", json_output=True) == 0
    capsys.readouterr()
    outside = tmp_path / "outside"
    victim_dir = outside / "security-review"
    victim_dir.mkdir(parents=True)
    (victim_dir / "SKILL.md").write_text("victim\n")
    resources = workspace / ".brigade" / "skills" / "mcp-resources"
    shutil.move(str(resources), str(tmp_path / "real-resources"))
    resources.symlink_to(outside, target_is_directory=True)

    rc = skills_cmd.uninstall(workspace=workspace, skill="security-review", harness="mcp")

    assert rc == 2
    assert (victim_dir / "SKILL.md").read_text() == "victim\n"


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform cannot create symlinks")
def test_fallback_state_writes_refuse_symlinked_components(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    installs = workspace / ".brigade" / "skills" / "installs"
    installs.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = workspace / ".brigade" / "skills" / "linked"
    linked.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(skills_cmd, "_HAS_DESCRIPTOR_ANCHOR", False)
    anchor = skills_cmd._hold_state_root(workspace)
    try:
        with pytest.raises(skills_cmd.SkillsStatePathError):
            skills_cmd._write_state_file(anchor, "skills", "linked", "receipt.json", data=b"x")
        with pytest.raises(skills_cmd.SkillsStatePathError):
            skills_cmd._append_state_line(anchor, "skills", "linked", "history.jsonl", data=b"x")
        (installs / "receipt.json").symlink_to(outside / "victim.txt")
        (outside / "victim.txt").write_text("victim\n")
        with pytest.raises(skills_cmd.SkillsStatePathError):
            skills_cmd._write_state_file(anchor, "skills", "installs", "receipt.json", data=b"x")
    finally:
        anchor.close()

    assert (outside / "victim.txt").read_text() == "victim\n"
    assert not (outside / "receipt.json").exists()
    assert not (outside / "history.jsonl").exists()


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform cannot create symlinks")
def test_simulated_fallback_install_refuses_symlinked_installs_component(tmp_path, monkeypatch, capsys):
    workspace = tmp_path / "ws"
    _seed_workspace_with_skill(workspace, tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    installs = workspace / ".brigade" / "skills" / "installs"
    installs.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(skills_cmd, "_HAS_DESCRIPTOR_ANCHOR", False)

    rc = skills_cmd.install(workspace=workspace, skill="security-review", harness="claude", json_output=True)

    assert rc == 2
    assert not any(outside.iterdir())


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="platform lacks O_NOFOLLOW descriptor anchoring")
def test_missing_state_root_creation_refuses_swapped_ancestor(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    workspace = home / "ws"
    workspace.mkdir()
    evil = tmp_path / "evil"
    evil.mkdir()
    real_mkdir = os.mkdir

    def swap_during_creation(path, mode=0o777, *, dir_fd=None):
        shutil.move(str(workspace), str(tmp_path / "ws-original"))
        (home / "ws").symlink_to(evil, target_is_directory=True)
        if dir_fd is None:
            return real_mkdir(path, mode)
        return real_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "mkdir", swap_during_creation)

    with pytest.raises(skills_cmd.SkillsStatePathError):
        skills_cmd._open_state_root_descriptor(workspace)

    monkeypatch.undo()
    assert not (evil / ".brigade").exists()
    assert (tmp_path / "ws-original" / ".brigade").is_dir()


def _workspace_with_rollback_snapshot(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "ws"
    _seed_workspace_with_skill(workspace, tmp_path)
    source = _write_skill(tmp_path / "source")
    installed = workspace / ".claude" / "skills" / "security-review" / "SKILL.md"
    assert skills_cmd.install(workspace=workspace, skill=str(source), harness="claude", json_output=True) == 0
    (source / "SKILL.md").write_text("# B\n")
    assert (
        skills_cmd.install(workspace=workspace, skill=str(source), harness="claude", force=True, json_output=True) == 0
    )
    return workspace, installed


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform cannot create symlinks")
def test_rollback_copy_refuses_symlinked_rollback_component(tmp_path, capsys):
    workspace, installed = _workspace_with_rollback_snapshot(tmp_path)
    receipt_path = workspace / ".brigade" / "skills" / "installs" / "security-review-claude.json"
    stamp = Path(json.loads(receipt_path.read_text())["rollback_snapshot"]).name
    outside = tmp_path / "outside"
    outside.mkdir()
    victim_tree = outside / "victim-tree"
    victim_tree.mkdir()
    (victim_tree / "SECRET.md").write_text("external secret\n")
    planted = outside / stamp
    planted.mkdir()
    (planted / "SKILL.md").write_text("# attacker content\n")
    rollback_component = workspace / ".brigade" / "skills" / "rollback" / "security-review" / "claude"
    shutil.move(str(rollback_component), str(tmp_path / "real-rollback-component"))
    rollback_component.symlink_to(outside, target_is_directory=True)

    rc = skills_cmd.rollback(workspace=workspace, skill="security-review", harness="claude")

    assert rc == 2
    captured = capsys.readouterr()
    assert "state root" in captured.err or "state path" in captured.err
    # the symlink must never redirect the copy or the deletion
    assert (victim_tree / "SECRET.md").read_text() == "external secret\n"
    assert sorted(path.name for path in outside.iterdir()) == sorted(["victim-tree", stamp])
    # refusal happens before the installed copy is touched
    assert installed.read_text() == "# B\n"


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform cannot create symlinks")
def test_rollback_receipt_writes_refuse_symlinked_installs_component(tmp_path, capsys):
    workspace, installed = _workspace_with_rollback_snapshot(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    victim_receipt = outside / "victim-receipt.json"
    victim_receipt.write_text("keep me\n")
    installs = workspace / ".brigade" / "skills" / "installs"
    shutil.move(str(installs), str(tmp_path / "real-installs"))
    installs.symlink_to(outside, target_is_directory=True)

    rc = skills_cmd.rollback(workspace=workspace, skill="security-review", harness="claude")

    assert rc == 2
    captured = capsys.readouterr()
    assert "state root" in captured.err or "state path" in captured.err
    assert sorted(path.name for path in outside.iterdir()) == ["victim-receipt.json"]
    assert victim_receipt.read_text() == "keep me\n"
    # refusal happens before the installed copy is removed
    assert installed.read_text() == "# B\n"


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform cannot create symlinks")
def test_inbox_add_refuses_symlinked_inbox_component(tmp_path, capsys):
    workspace = tmp_path / "ws"
    _seed_workspace_with_skill(workspace, tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep\n")
    inbox = workspace / ".brigade" / "skills" / "inbox"
    inbox.symlink_to(outside, target_is_directory=True)
    source = _write_skill(tmp_path / "source")

    rc = skills_cmd.inbox_add(target=workspace, source=source, summary="review me")

    assert rc == 2
    captured = capsys.readouterr()
    assert "state root" in captured.err or "state path" in captured.err
    assert [path.name for path in outside.iterdir()] == ["keep.txt"]


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform cannot create symlinks")
def test_inbox_force_replace_refuses_symlinked_inbox_component(tmp_path, capsys):
    workspace = tmp_path / "ws"
    _seed_workspace_with_skill(workspace, tmp_path)
    capsys.readouterr()
    source = _write_skill(tmp_path / "source")
    assert skills_cmd.inbox_add(target=workspace, source=source, summary="first", json_output=True) == 0
    proposal_id = json.loads(capsys.readouterr().out)["proposal_id"]

    outside = tmp_path / "outside"
    outside.mkdir()
    matching = outside / proposal_id
    matching.mkdir()
    (matching / "proposal.json").write_text("victim proposal\n")
    inbox = workspace / ".brigade" / "skills" / "inbox"
    shutil.move(str(inbox), str(tmp_path / "real-inbox"))
    inbox.symlink_to(outside, target_is_directory=True)

    rc = skills_cmd.inbox_add(target=workspace, source=source, summary="second", force=True)

    assert rc == 2
    captured = capsys.readouterr()
    assert "state root" in captured.err or "state path" in captured.err
    # the forced replace must never delete the matching external subtree
    assert (matching / "proposal.json").read_text() == "victim proposal\n"


@pytest.mark.skipif(not hasattr(os, "link"), reason="platform cannot create hardlinks")
def test_import_refuses_hardlinked_source_file(tmp_path, capsys):
    workspace = tmp_path / "ws"
    _seed_workspace_with_skill(workspace, tmp_path)
    source = _write_skill(tmp_path / "source", name="hardlinked-skill")
    os.link(source / "SKILL.md", tmp_path / "alias.md")

    with pytest.raises(skills_cmd.SkillsStatePathError, match="hardlink"):
        skills_cmd._collect_source_tree(source)

    rc = skills_cmd.import_skill(target=workspace, source=source)

    assert rc == 2
    assert "hardlink" in capsys.readouterr().err
    assert not (workspace / ".brigade" / "skills" / "registry" / "hardlinked-skill").exists()


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="platform lacks O_NOFOLLOW descriptor anchoring")
def test_collect_source_tree_refuses_entry_swapped_to_symlink(tmp_path, monkeypatch):
    source = _write_skill(tmp_path / "source")
    secret = tmp_path / "secret.md"
    secret.write_text("outside secret\n")
    swapped = source / "CHANGELOG.md"
    swapped.unlink()
    swapped.symlink_to(secret)

    real_lstat = os.lstat

    def lying_lstat(name, *, dir_fd=None):
        st = real_lstat(name, dir_fd=dir_fd) if dir_fd is not None else real_lstat(name)
        if name == "CHANGELOG.md":
            # Pretend the entry passed the regular-file check so only the
            # O_NOFOLLOW open can catch that it is really a symlink.
            st = os.stat_result(
                (
                    stat_module.S_IFREG | stat_module.S_IMODE(st.st_mode),
                    st.st_ino,
                    st.st_dev,
                    1,
                    st.st_uid,
                    st.st_gid,
                    st.st_size,
                    st.st_atime,
                    st.st_mtime,
                    st.st_ctime,
                )
            )
        return st

    monkeypatch.setattr(os, "lstat", lying_lstat)

    with pytest.raises(skills_cmd.SkillsStatePathError, match="changed while being read"):
        skills_cmd._collect_source_tree(source)

    monkeypatch.undo()
    assert secret.read_text() == "outside secret\n"


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="platform lacks O_NOFOLLOW descriptor anchoring")
def test_remove_tree_error_path_raises_typed_error_not_name_error(tmp_path):
    workspace = tmp_path / "ws"
    (workspace / ".brigade" / "skills" / "installs").mkdir(parents=True)
    long_name = "n" * 400

    with skills_cmd._held_state_root(workspace) as anchor:
        with pytest.raises(skills_cmd.SkillsStatePathError, match="not removable safely"):
            skills_cmd._remove_tree_in_anchor(anchor, "skills", "installs", long_name)


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="platform lacks O_NOFOLLOW descriptor anchoring")
def test_unlink_error_path_raises_typed_error_not_name_error(tmp_path):
    workspace = tmp_path / "ws"
    (workspace / ".brigade" / "skills" / "installs").mkdir(parents=True)
    long_name = "n" * 400

    with skills_cmd._held_state_root(workspace) as anchor:
        with pytest.raises(skills_cmd.SkillsStatePathError, match="could not be removed safely"):
            skills_cmd._unlink_in_anchor(anchor, "skills", "installs", long_name)


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="platform lacks O_NOFOLLOW descriptor anchoring")
def test_state_write_append_and_read_refuse_hardlinked_targets(tmp_path):
    workspace = tmp_path / "ws"
    (workspace / ".brigade" / "skills" / "installs").mkdir(parents=True)
    victim_receipt = tmp_path / "victim-receipt.txt"
    victim_history = tmp_path / "victim-history.txt"
    receipt = workspace / ".brigade" / "skills" / "installs" / "receipt.json"
    history = workspace / ".brigade" / "skills" / "installs" / "history.jsonl"

    with skills_cmd._held_state_root(workspace) as anchor:
        skills_cmd._write_state_file(anchor, "skills", "installs", "receipt.json", data=b'{"v": 1}\n')
        skills_cmd._append_state_line(anchor, "skills", "installs", "history.jsonl", data=b'{"row": 1}\n')
        os.link(receipt, victim_receipt)
        os.link(history, victim_history)

        with pytest.raises(skills_cmd.SkillsStatePathError):
            skills_cmd._write_state_file(anchor, "skills", "installs", "receipt.json", data=b'{"v": 2}\n')
        with pytest.raises(skills_cmd.SkillsStatePathError):
            skills_cmd._append_state_line(anchor, "skills", "installs", "history.jsonl", data=b'{"row": 2}\n')
        with pytest.raises(skills_cmd.SkillsStatePathError):
            skills_cmd._read_state_file_bytes(anchor, "skills", "installs", "receipt.json")

    assert receipt.read_bytes() == b'{"v": 1}\n'
    assert history.read_bytes() == b'{"row": 1}\n'
    # the victims share inodes with the state files: unchanged because every
    # mutating call was refused before any byte moved
    assert victim_receipt.read_bytes() == b'{"v": 1}\n'
    assert victim_history.read_bytes() == b'{"row": 1}\n'


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="platform cannot create FIFOs")
@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="platform lacks O_NOFOLLOW descriptor anchoring")
def test_state_read_refuses_fifo_instead_of_blocking(tmp_path):
    workspace = tmp_path / "ws"
    (workspace / ".brigade" / "skills" / "installs").mkdir(parents=True)
    os.mkfifo(workspace / ".brigade" / "skills" / "installs" / "pipe.json")

    with skills_cmd._held_state_root(workspace) as anchor:
        with pytest.raises(skills_cmd.SkillsStatePathError):
            skills_cmd._read_state_file_bytes(anchor, "skills", "installs", "pipe.json")


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="platform lacks O_NOFOLLOW descriptor anchoring")
def test_install_refuses_hardlinked_receipt_and_history_without_touching_victims(tmp_path, capsys):
    workspace = tmp_path / "ws"
    _seed_workspace_with_skill(workspace, tmp_path)
    assert skills_cmd.install(workspace=workspace, skill="security-review", harness="claude", json_output=True) == 0
    capsys.readouterr()
    installs = workspace / ".brigade" / "skills" / "installs"
    victim_receipt = tmp_path / "victim-receipt.json"
    victim_history = tmp_path / "victim-history.jsonl"
    os.link(installs / "security-review-claude.json", victim_receipt)
    os.link(installs / "history.jsonl", victim_history)
    before_receipt = victim_receipt.read_bytes()
    before_history = victim_history.read_bytes()

    rc = skills_cmd.install(
        workspace=workspace, skill="security-review", harness="claude", force=True, json_output=True
    )

    assert rc == 2
    captured = capsys.readouterr()
    assert "error:" in captured.err
    assert "Traceback" not in captured.err
    assert victim_receipt.read_bytes() == before_receipt
    assert victim_history.read_bytes() == before_history


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform cannot create symlinks")
@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="platform lacks O_NOFOLLOW descriptor anchoring")
def test_install_refuses_symlinked_previous_receipt_and_leaks_no_outside_content(tmp_path, capsys):
    workspace = tmp_path / "ws"
    _seed_workspace_with_skill(workspace, tmp_path)
    assert skills_cmd.install(workspace=workspace, skill="security-review", harness="claude", json_output=True) == 0
    capsys.readouterr()
    outside = tmp_path / "outside.json"
    outside_payload = {"schema_version": 99, "injected": "OUTSIDE-RECEIPT-CONTENT"}
    outside.write_text(json.dumps(outside_payload) + "\n")
    receipt = workspace / ".brigade" / "skills" / "installs" / "security-review-claude.json"
    receipt.unlink()
    receipt.symlink_to(outside)
    history = workspace / ".brigade" / "skills" / "installs" / "history.jsonl"

    rc = skills_cmd.install(
        workspace=workspace, skill="security-review", harness="claude", force=True, json_output=True
    )

    assert rc == 2
    captured = capsys.readouterr()
    assert "error:" in captured.err
    assert "OUTSIDE-RECEIPT-CONTENT" not in captured.out
    assert "OUTSIDE-RECEIPT-CONTENT" not in history.read_text()
    assert outside.read_text() == json.dumps(outside_payload) + "\n"


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform cannot create symlinks")
def test_symlinked_source_skill_md_never_reaches_state_or_rendered_install(tmp_path, capsys):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    source = _write_skill(tmp_path / "source")
    secret = tmp_path / "outside-secret.md"
    secret.write_text("# OUTSIDE SKILL SECRET\n")
    (source / "SKILL.md").unlink()
    (source / "SKILL.md").symlink_to(secret)

    for harness in ("mcp", "claude"):
        rc = skills_cmd.install(workspace=workspace, skill=str(source), harness=harness, json_output=True)
        captured = capsys.readouterr()
        assert rc == 2, captured.out + captured.err
        assert "OUTSIDE SKILL SECRET" not in captured.out

    for root in (workspace / ".brigade", workspace / ".claude"):
        if root.is_dir():
            for path in root.rglob("*"):
                if path.is_file():
                    assert "OUTSIDE SKILL SECRET" not in path.read_text(errors="replace")


def test_fallback_without_descriptor_anchor_fails_closed_for_state_mutations(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    installs = workspace / ".brigade" / "skills" / "installs"
    installs.mkdir(parents=True)
    (installs / "existing.json").write_text("{}\n")
    (installs / "subtree").mkdir()
    (installs / "subtree" / "keep.txt").write_text("keep\n")
    monkeypatch.setattr(skills_cmd, "_HAS_DESCRIPTOR_ANCHOR", False)
    dirs: list[tuple[str, ...]] = [("nested",)]
    files: dict[tuple[str, ...], bytes] = {("nested", "new.txt"): b"data\n"}
    anchor = skills_cmd._hold_state_root(workspace)
    try:
        assert skills_cmd._read_state_file_bytes(anchor, "skills", "installs", "existing.json") == b"{}\n"
        with pytest.raises(skills_cmd.SkillsStatePathError):
            skills_cmd._write_state_file(anchor, "skills", "installs", "written.json", data=b"x\n")
        with pytest.raises(skills_cmd.SkillsStatePathError):
            skills_cmd._append_state_line(anchor, "skills", "installs", "history.jsonl", data=b"x\n")
        with pytest.raises(skills_cmd.SkillsStatePathError):
            skills_cmd._unlink_in_anchor(anchor, "skills", "installs", "existing.json")
        with pytest.raises(skills_cmd.SkillsStatePathError):
            skills_cmd._remove_tree_in_anchor(anchor, "skills", "installs", "subtree")
        with pytest.raises(skills_cmd.SkillsStatePathError):
            skills_cmd._write_collected_tree_into_anchor(dirs, files, anchor, "skills", "installs", "planted")
    finally:
        anchor.close()

    assert (installs / "existing.json").read_text() == "{}\n"
    assert (installs / "subtree" / "keep.txt").read_text() == "keep\n"
    assert not (installs / "written.json").exists()
    assert not (installs / "history.jsonl").exists()
    assert not (installs / "planted").exists()


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform cannot create symlinks")
def test_fallback_read_keeps_working_but_refuses_symlinked_state_file(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    installs = workspace / ".brigade" / "skills" / "installs"
    installs.mkdir(parents=True)
    (installs / "plain.txt").write_text("plain\n")
    (installs / "link.txt").symlink_to(tmp_path / "target.txt")
    monkeypatch.setattr(skills_cmd, "_HAS_DESCRIPTOR_ANCHOR", False)
    anchor = skills_cmd._hold_state_root(workspace)
    try:
        assert skills_cmd._read_state_file_bytes(anchor, "skills", "installs", "plain.txt") == b"plain\n"
        with pytest.raises(skills_cmd.SkillsStatePathError):
            skills_cmd._read_state_file_bytes(anchor, "skills", "installs", "link.txt")
    finally:
        anchor.close()


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="platform lacks O_NOFOLLOW descriptor anchoring")
def test_anchor_primitives_translate_injected_os_errors_into_typed_refusals(tmp_path, monkeypatch):
    def _raise_io(*_args, **_kwargs):
        raise OSError(errno.EIO, "injected failure")

    workspace = tmp_path / "ws"
    installs = workspace / ".brigade" / "skills" / "installs"
    installs.mkdir(parents=True)
    (installs / "existing.json").write_text("{}\n")
    (installs / "child").mkdir()
    (installs / "child" / "leaf.txt").write_text("leaf\n")

    with skills_cmd._held_state_root(workspace) as anchor:
        monkeypatch.setattr(os, "write", _raise_io)
        with pytest.raises(skills_cmd.SkillsStatePathError) as exc_info:
            skills_cmd._write_state_file(anchor, "skills", "installs", "written.json", data=b"x\n")
        assert isinstance(exc_info.value.__cause__, OSError)
        with pytest.raises(skills_cmd.SkillsStatePathError) as exc_info:
            skills_cmd._append_state_line(anchor, "skills", "installs", "history.jsonl", data=b"x\n")
        assert isinstance(exc_info.value.__cause__, OSError)
        monkeypatch.undo()

        monkeypatch.setattr(os, "rmdir", _raise_io)
        with pytest.raises(skills_cmd.SkillsStatePathError) as exc_info:
            skills_cmd._remove_tree_in_anchor(anchor, "skills", "installs", "child")
        assert isinstance(exc_info.value.__cause__, OSError)
        monkeypatch.undo()

        monkeypatch.setattr(os, "unlink", _raise_io)
        with pytest.raises(skills_cmd.SkillsStatePathError) as exc_info:
            skills_cmd._unlink_in_anchor(anchor, "skills", "installs", "existing.json")
        assert isinstance(exc_info.value.__cause__, OSError)
        monkeypatch.undo()

        monkeypatch.setattr(os, "write", _raise_io)
        with pytest.raises(skills_cmd.SkillsStatePathError) as exc_info:
            skills_cmd._write_collected_tree_into_anchor(
                [("d",)], {("d", "f.txt"): b"x"}, anchor, "skills", "installs", "planted"
            )
        assert isinstance(exc_info.value.__cause__, OSError)
        monkeypatch.undo()

    # the injected failures never corrupted unrelated state files
    assert (installs / "existing.json").read_text() == "{}\n"


def test_skills_fleet_does_not_guess_source_for_malformed_v2_receipt(tmp_path, capsys):
    assert skills_cmd.install(workspace=tmp_path, skill="brigade-work", harness="cursor", json_output=True) == 0
    capsys.readouterr()
    receipt_path = tmp_path / ".brigade" / "skills" / "installs" / "brigade-work-cursor.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["source"].pop("kind")
    receipt_path.write_text(json.dumps(receipt))
    installed = tmp_path / ".cursor" / "skills" / "brigade-work" / "SKILL.md"
    installed.write_text(installed.read_text() + "\nlocal edit\n")

    payload = skills_cmd._fleet_status_payload(tmp_path)
    row = next(item for item in payload["copies"] if item["skill_id"] == "brigade-work")

    assert row["status"] == "unknown"
    assert row["drift"]["receipt_known"] is False
    assert row["update_command"] is None


def test_skills_fleet_rejects_malformed_renderer_contract(tmp_path, capsys):
    assert skills_cmd.install(workspace=tmp_path, skill="brigade-work", harness="cursor", json_output=True) == 0
    capsys.readouterr()
    receipt_path = tmp_path / ".brigade" / "skills" / "installs" / "brigade-work-cursor.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["render"]["format"] = "tampered-format"
    receipt_path.write_text(json.dumps(receipt))

    payload = skills_cmd._fleet_status_payload(tmp_path)
    row = next(item for item in payload["copies"] if item["skill_id"] == "brigade-work")

    assert row["status"] == "unknown"
    assert row["drift"]["receipt_known"] is False
    assert row["update_command"] is None


def test_skills_fleet_suppresses_removal_for_malformed_receipt(tmp_path, capsys):
    source = _write_skill(tmp_path / "source", name="malformed-unsupported")
    metadata_path = source / "skill.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["supported_harnesses"] = ["cursor"]
    metadata_path.write_text(json.dumps(metadata))
    assert skills_cmd.import_skill(target=tmp_path, source=source, json_output=True) == 0
    capsys.readouterr()
    assert (
        skills_cmd.install(
            workspace=tmp_path,
            skill="registry:malformed-unsupported",
            harness="cursor",
            json_output=True,
        )
        == 0
    )
    capsys.readouterr()
    receipt_path = tmp_path / ".brigade" / "skills" / "installs" / "malformed-unsupported-cursor.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["render"].pop("format")
    receipt_path.write_text(json.dumps(receipt))
    registry_metadata = tmp_path / ".brigade" / "skills" / "registry" / "malformed-unsupported" / "skill.json"
    metadata = json.loads(registry_metadata.read_text())
    metadata["supported_harnesses"] = ["codex"]
    registry_metadata.write_text(json.dumps(metadata))

    payload = skills_cmd._fleet_status_payload(tmp_path)
    row = next(item for item in payload["copies"] if item["skill_id"] == "malformed-unsupported")

    assert row["drift"]["receipt_known"] is False
    assert row["status"] == "unknown"
    assert row["remove_command"] is None


def test_skills_fleet_does_not_remove_partial_unsupported_copy(tmp_path, capsys):
    source = _write_skill(tmp_path / "source", name="partial-unsupported")
    metadata_path = source / "skill.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["supported_harnesses"] = ["cursor"]
    metadata_path.write_text(json.dumps(metadata))
    assert skills_cmd.import_skill(target=tmp_path, source=source, json_output=True) == 0
    capsys.readouterr()
    assert (
        skills_cmd.install(
            workspace=tmp_path,
            skill="registry:partial-unsupported",
            harness="cursor",
            json_output=True,
        )
        == 0
    )
    capsys.readouterr()
    installed = tmp_path / ".cursor" / "skills" / "partial-unsupported"
    (installed / "SKILL.md").unlink()
    registry_metadata = tmp_path / ".brigade" / "skills" / "registry" / "partial-unsupported" / "skill.json"
    metadata = json.loads(registry_metadata.read_text())
    metadata["supported_harnesses"] = ["codex"]
    registry_metadata.write_text(json.dumps(metadata))

    payload = skills_cmd._fleet_status_payload(tmp_path)
    row = next(item for item in payload["copies"] if item["skill_id"] == "partial-unsupported")

    assert row["drift"]["receipt_known"] is True
    assert row["status"] == "missing"
    assert row["remove_command"] is None


def test_skills_fleet_status_is_stable_and_emits_one_update_per_stale_copy(tmp_path, capsys):
    for harness in ("cursor", "codex"):
        assert skills_cmd.install(workspace=tmp_path, skill="brigade-work", harness=harness, json_output=True) == 0
        capsys.readouterr()
    cursor = tmp_path / ".cursor" / "skills" / "brigade-work" / "SKILL.md"
    cursor.write_text(cursor.read_text() + "\nlocal edit\n")
    shutil.rmtree(tmp_path / ".codex" / "skills" / "brigade-work")
    claude = tmp_path / ".claude" / "skills" / "brigade-work"
    claude.mkdir(parents=True)
    claude.joinpath("SKILL.md").write_text(
        (skills_cmd.template_root() / "skills" / "brigade-work" / "SKILL.md").read_text()
    )

    argv = ["skills", "fleet", "status", "--target", str(tmp_path), "--json"]
    assert cli.main(argv) == 0
    first_text = capsys.readouterr().out
    assert cli.main(argv) == 0
    second_text = capsys.readouterr().out
    assert first_text == second_text
    payload = json.loads(first_text)
    rows = [row for row in payload["copies"] if row["skill_id"] == "brigade-work"]
    assert [(row["skill_id"], row["harness"]) for row in payload["copies"]] == sorted(
        (row["skill_id"], row["harness"]) for row in payload["copies"]
    )
    by_harness = {row["harness"]: row for row in rows}
    assert by_harness["cursor"]["status"] == "stale"
    assert by_harness["codex"]["status"] == "missing"
    assert by_harness["claude"]["status"] == "unknown"
    assert by_harness["cursor"]["update_command"].endswith("--target cursor --force")
    assert by_harness["codex"]["update_command"].endswith("--target codex --force")
    assert by_harness["claude"]["update_command"] is None
    assert all(row["update_command"] for row in payload["stale"])

    assert skills_cmd.fleet_status(target=tmp_path) == 0
    text = capsys.readouterr().out
    assert "- brigade-work [claude] unknown" in text
    assert "update:" not in text.split("- brigade-work [claude] unknown", 1)[1].split("\n- ", 1)[0]


def test_skills_fleet_preserves_explicit_registry_provenance(tmp_path, capsys):
    source = _write_skill(tmp_path / "source", name="brigade-work")
    assert skills_cmd.import_skill(target=tmp_path, source=source, json_output=True) == 0
    capsys.readouterr()
    assert (
        skills_cmd.install(
            workspace=tmp_path,
            skill="registry:brigade-work",
            harness="cursor",
            json_output=True,
        )
        == 0
    )
    capsys.readouterr()

    installed = tmp_path / ".cursor" / "skills" / "brigade-work" / "SKILL.md"
    installed.write_text(installed.read_text() + "\nlocal edit\n")
    payload = skills_cmd._fleet_status_payload(tmp_path)
    row = next(item for item in payload["copies"] if item["skill_id"] == "brigade-work")

    assert row["source"]["kind"] == "registry"
    assert row["status"] == "stale"
    assert "registry:brigade-work" in row["update_command"]


def test_skills_fleet_discovers_explicit_path_installs(tmp_path, capsys):
    source = _write_skill(tmp_path / "external", name="path-only")
    assert skills_cmd.install(workspace=tmp_path, skill=str(source), harness="cursor", json_output=True) == 0
    capsys.readouterr()

    installed = tmp_path / ".cursor" / "skills" / "path-only" / "SKILL.md"
    installed.write_text(installed.read_text() + "\nlocal edit\n")
    payload = skills_cmd._fleet_status_payload(tmp_path)
    row = next(item for item in payload["copies"] if item["skill_id"] == "path-only")

    assert row["source"]["kind"] == "path"
    assert row["status"] == "stale"
    assert str(source) in row["update_command"]


def test_skills_fleet_detects_metadata_only_source_drift(tmp_path, capsys):
    source = _write_skill(tmp_path / "source", name="metadata-drift")
    assert skills_cmd.import_skill(target=tmp_path, source=source, json_output=True) == 0
    capsys.readouterr()
    assert (
        skills_cmd.install(
            workspace=tmp_path,
            skill="registry:metadata-drift",
            harness="cursor",
            json_output=True,
        )
        == 0
    )
    capsys.readouterr()

    metadata_path = tmp_path / ".brigade" / "skills" / "registry" / "metadata-drift" / "skill.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["version"] = "0.2.0"
    metadata_path.write_text(json.dumps(metadata))
    payload = skills_cmd._fleet_status_payload(tmp_path)
    row = next(item for item in payload["copies"] if item["skill_id"] == "metadata-drift")

    assert row["status"] == "stale"
    assert row["drift"]["source"] == "changed"
    assert row["drift"]["local_edit"] == "current"


def test_skills_registry_mcp_compatibility_stays_on_registry_source(tmp_path):
    source = _write_skill(tmp_path / ".brigade" / "skills" / "registry", name="brigade-work")
    metadata = json.loads((source / "skill.json").read_text())
    metadata["trust_level"] = "workspace"
    (source / "skill.json").write_text(json.dumps(metadata))

    text, mime_type = skills_cmd._mcp_read_resource(
        tmp_path,
        "skill://registry/brigade-work/compatibility.json",
    )
    payload = json.loads(text)

    assert mime_type == "application/json"
    assert payload["source"]["kind"] == "registry"
    assert payload["trust_score"]["trust_level"] == "workspace"


def test_skills_fleet_reports_copy_after_harness_support_is_removed(tmp_path, capsys):
    source = _write_skill(tmp_path / "source", name="support-drift")
    metadata_path = source / "skill.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["supported_harnesses"] = ["cursor"]
    metadata_path.write_text(json.dumps(metadata))
    assert skills_cmd.import_skill(target=tmp_path, source=source, json_output=True) == 0
    capsys.readouterr()
    assert (
        skills_cmd.install(
            workspace=tmp_path,
            skill="registry:support-drift",
            harness="cursor",
            json_output=True,
        )
        == 0
    )
    capsys.readouterr()

    registry_metadata = tmp_path / ".brigade" / "skills" / "registry" / "support-drift" / "skill.json"
    metadata = json.loads(registry_metadata.read_text())
    metadata["supported_harnesses"] = ["codex"]
    registry_metadata.write_text(json.dumps(metadata))
    payload = skills_cmd._fleet_status_payload(tmp_path)
    row = next(item for item in payload["copies"] if item["skill_id"] == "support-drift")

    assert row["harness"] == "cursor"
    assert row["supported"] is False
    assert row["status"] == "unsupported"
    assert row["update_command"] is None
    assert row["remove_command"].endswith("--target cursor")
    assert payload["unsupported_count"] == 1
    assert row not in payload["stale"]

    assert skills_cmd.fleet_status(target=tmp_path) == 0
    text = capsys.readouterr().out
    assert "support-drift [cursor] unsupported supported=false" in text
    assert "update:" not in text
    assert "remove: brigade skills uninstall support-drift" in text


def test_skills_fleet_marks_current_unsupported_copy_separately(tmp_path, capsys):
    source = _write_skill(tmp_path / "source", name="unsupported-current")
    metadata_path = source / "skill.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["supported_harnesses"] = ["codex"]
    metadata_path.write_text(json.dumps(metadata))
    assert skills_cmd.import_skill(target=tmp_path, source=source, json_output=True) == 0
    capsys.readouterr()
    assert (
        skills_cmd.install(
            workspace=tmp_path,
            skill="registry:unsupported-current",
            harness="cursor",
            json_output=True,
        )
        == 0
    )
    capsys.readouterr()

    payload = skills_cmd._fleet_status_payload(tmp_path)
    row = next(item for item in payload["copies"] if item["skill_id"] == "unsupported-current")

    assert row["drift"]["overall"] == "current"
    assert row["status"] == "unsupported"
    assert row["supported"] is False
    assert row["update_command"] is None
    assert row["remove_command"].endswith("--target cursor")


def test_skills_pack_build_show_import_and_archive(tmp_path, capsys):
    source = _write_skill(tmp_path / "source")
    assert skills_cmd.import_skill(target=tmp_path, source=source, json_output=True) == 0
    capsys.readouterr()

    assert skills_cmd.pack_build(target=tmp_path, json_output=True) == 0
    pack = json.loads(capsys.readouterr().out)
    pack_path = pack["path"]
    assert pack["skill_count"] == 1
    assert pack["skills"][0]["trust_score"]["provenance"]["kind"] == "registry"
    assert pack["skills"][0]["trust_score"]["provenance"]["identity"] == "registry://skills/security-review"
    assert (
        tmp_path / ".brigade" / "skills" / "packs" / pack["pack_id"] / "skills" / "security-review" / "SKILL.md"
    ).is_file()

    assert skills_cmd.pack_show(target=tmp_path, pack_id="latest", json_output=True) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["pack"]["pack_id"] == pack["pack_id"]

    other = tmp_path / "other"
    other.mkdir()
    assert (
        skills_cmd.pack_import(
            target=other, pack=tmp_path / ".brigade" / "skills" / "packs" / pack["pack_id"], json_output=True
        )
        == 0
    )
    imported = json.loads(capsys.readouterr().out)
    assert imported["imported_count"] == 1
    assert (other / ".brigade" / "skills" / "registry" / "security-review" / "SKILL.md").is_file()

    assert cli.main(["skills", "pack", "list", "--target", str(tmp_path), "--json"]) == 0
    listing = json.loads(capsys.readouterr().out)
    assert listing["pack_count"] == 1

    assert skills_cmd.pack_archive(target=tmp_path, pack_id=pack["pack_id"], json_output=True) == 0
    archived = json.loads(capsys.readouterr().out)
    assert archived["status"] == "archived"
    assert archived["archive_path"].endswith(pack["pack_id"])
    assert pack_path not in archived["archive_path"]


def test_skills_mcp_stdio_serves_read_only_resources(tmp_path, capsys, monkeypatch):
    source = _write_skill(tmp_path / "source")
    assert skills_cmd.import_skill(target=tmp_path, source=source, json_output=True) == 0
    capsys.readouterr()
    requests = (
        "\n".join(
            json.dumps(item)
            for item in (
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "resources/list", "params": {}},
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "resources/read",
                    "params": {"uri": "skill://registry/security-review/SKILL.md"},
                },
                {"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {}},
                {
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "tools/call",
                    "params": {"name": "get_skill_metadata", "arguments": {"skill_id": "security-review"}},
                },
                {
                    "jsonrpc": "2.0",
                    "id": 6,
                    "method": "tools/call",
                    "params": {"name": "install_skill", "arguments": {"skill_id": "security-review"}},
                },
            )
        )
        + "\n"
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(requests))

    assert cli.main(["skills", "serve-mcp", "--target", str(tmp_path), "--stdio"]) == 0
    responses = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    by_id = {item["id"]: item for item in responses}
    assert by_id[1]["result"]["serverInfo"]["name"] == "brigade-skills-readonly"
    assert any(item["uri"] == "skill://registry/security-review/SKILL.md" for item in by_id[2]["result"]["resources"])
    assert "# Security Review" in by_id[3]["result"]["contents"][0]["text"]
    tool_names = {item["name"] for item in by_id[4]["result"]["tools"]}
    assert "get_skill_metadata" in tool_names
    assert "install_skill" not in tool_names
    assert '"id": "security-review"' in by_id[5]["result"]["content"][0]["text"]
    assert by_id[6]["result"]["isError"] is True


# --- user_profile_skill_packages (Task 3 of issue #438) -----------------------

_USER_PROFILE_HARNESSES = ["codex", "claude"]


def _write_reviewed_skill(root, name="reviewed", *, supported_harnesses=None, trust_level="workspace", enabled=True):
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(f"# {name.title()}\n\nDo the work.\n")
    (skill_dir / "scripts").mkdir()
    (skill_dir / "scripts" / "check.py").write_text("print(1)\n")
    (skill_dir / "skill.json").write_text(
        json.dumps(
            {
                "id": name,
                "title": name.title(),
                "version": "1.0.0",
                "required_tools": [],
                "required_mcp_servers": [],
                "supported_harnesses": supported_harnesses
                if supported_harnesses is not None
                else list(_USER_PROFILE_HARNESSES),
                "trust_level": trust_level,
                "enabled": enabled,
                "tests": [],
            }
        )
    )
    return skill_dir


def test_user_profile_skill_packages_select_reviewed_supported_full_packages(tmp_path, capsys):
    source = _write_reviewed_skill(tmp_path / "source", name="reviewed")
    assert skills_cmd.import_skill(target=tmp_path, source=source, json_output=True) == 0
    capsys.readouterr()

    packages = skills_cmd.user_profile_skill_packages(workspace=tmp_path, harness="codex", minimum_trust="workspace")

    assert [p.skill_id for p in packages] == ["reviewed"]
    pkg = packages[0]
    assert pkg.source_identity == "registry://skills/reviewed"
    assert isinstance(pkg.source_fingerprint, str) and pkg.source_fingerprint
    assert isinstance(pkg.metadata_fingerprint, str) and pkg.metadata_fingerprint
    # nested regular file included byte-for-byte
    assert "scripts/check.py" in pkg.files
    assert pkg.files["scripts/check.py"] == b"print(1)\n"
    # SKILL.md is rendered for the harness, not copied verbatim
    assert "SKILL.md" in pkg.files
    assert pkg.files["SKILL.md"] != b"# Reviewed\n\nDo the work.\n"
    assert pkg.files["SKILL.md"].startswith(b"---\n")
    assert b'name: "reviewed"' in pkg.files["SKILL.md"]
    # file keys are sorted by POSIX relative path
    assert list(pkg.files) == sorted(pkg.files)


def test_user_profile_skill_packages_reject_unsafe_and_disabled_entries(tmp_path, capsys):
    disabled = _write_reviewed_skill(tmp_path / "sources", name="disabled", enabled=False)
    assert skills_cmd.import_skill(target=tmp_path, source=disabled, json_output=True) == 0
    capsys.readouterr()

    linked = _write_reviewed_skill(tmp_path / "sources", name="linked")
    assert skills_cmd.import_skill(target=tmp_path, source=linked, json_output=True) == 0
    capsys.readouterr()
    registry_linked = tmp_path / ".brigade" / "skills" / "registry" / "linked"
    # a symlink to a directory outside the skill (must be skipped, not followed)
    (registry_linked / "escape").symlink_to(tmp_path)
    # a symlinked regular file pointing outside (must be skipped)
    outside = tmp_path / "outside.txt"
    outside.write_text("nope\n")
    (registry_linked / "escape-file.txt").symlink_to(outside)
    # a nested symlinked directory
    (registry_linked / "nested").mkdir()
    (registry_linked / "nested" / "linkdir").symlink_to(tmp_path)
    (registry_linked / "nested" / "real.txt").write_text("ok\n")
    # a .git directory with descendants (must be skipped entirely)
    (registry_linked / ".git").mkdir()
    (registry_linked / ".git" / "config").write_text("[core]\n")
    (registry_linked / ".git" / "hooks").mkdir()
    (registry_linked / ".git" / "hooks" / "post").write_text("x\n")

    packages = skills_cmd.user_profile_skill_packages(workspace=tmp_path, harness="codex", minimum_trust="workspace")

    ids = [p.skill_id for p in packages]
    assert "disabled" not in ids
    linked_pkg = next(p for p in packages if p.skill_id == "linked")
    assert all(not key.startswith(".git/") and key != ".git" for key in linked_pkg.files)
    assert "escape" not in linked_pkg.files
    assert "escape-file.txt" not in linked_pkg.files
    assert "nested/linkdir" not in linked_pkg.files
    assert all(not key.startswith("escape/") for key in linked_pkg.files)
    assert all(not key.startswith("nested/linkdir/") for key in linked_pkg.files)
    # no key with a ".." component
    assert all(".." not in Path(key).parts for key in linked_pkg.files)
    # nested real file still included
    assert linked_pkg.files["nested/real.txt"] == b"ok\n"


def test_user_profile_skill_packages_excludes_unsupported_and_below_trust(tmp_path, capsys):
    unsupported = _write_reviewed_skill(tmp_path / "sources", name="unsupported", supported_harnesses=["claude"])
    assert skills_cmd.import_skill(target=tmp_path, source=unsupported, json_output=True) == 0
    capsys.readouterr()

    lowtrust = _write_reviewed_skill(tmp_path / "sources", name="lowtrust", trust_level="unreviewed")
    assert skills_cmd.import_skill(target=tmp_path, source=lowtrust, json_output=True) == 0
    capsys.readouterr()

    packages = skills_cmd.user_profile_skill_packages(workspace=tmp_path, harness="codex", minimum_trust="workspace")
    ids = [p.skill_id for p in packages]
    assert "unsupported" not in ids
    assert "lowtrust" not in ids


def test_user_profile_skill_packages_rejects_unknown_harness_and_trust(tmp_path, capsys):
    source = _write_reviewed_skill(tmp_path / "source", name="reviewed")
    assert skills_cmd.import_skill(target=tmp_path, source=source, json_output=True) == 0
    capsys.readouterr()

    with pytest.raises(ValueError):
        skills_cmd.user_profile_skill_packages(
            workspace=tmp_path, harness="not-a-real-harness", minimum_trust="workspace"
        )
    with pytest.raises(ValueError):
        skills_cmd.user_profile_skill_packages(workspace=tmp_path, harness="codex", minimum_trust="not-a-trust-level")


def test_user_profile_skill_packages_deterministic_order(tmp_path, capsys):
    for name in ("bravo", "alpha"):
        source = _write_reviewed_skill(tmp_path / "sources", name=name)
        assert skills_cmd.import_skill(target=tmp_path, source=source, json_output=True) == 0
        capsys.readouterr()

    packages = skills_cmd.user_profile_skill_packages(workspace=tmp_path, harness="codex", minimum_trust="workspace")
    assert [p.skill_id for p in packages] == ["alpha", "bravo"]


def test_user_profile_skill_packages_caps_exclude_whole_package(tmp_path, capsys):
    many = tmp_path / "source" / "many"
    many.mkdir(parents=True)
    (many / "SKILL.md").write_text("# Many\n\nDo the work.\n")
    (many / "skill.json").write_text(
        json.dumps(
            {
                "id": "many",
                "title": "Many",
                "version": "1.0.0",
                "required_tools": [],
                "required_mcp_servers": [],
                "supported_harnesses": list(_USER_PROFILE_HARNESSES),
                "trust_level": "workspace",
                "tests": [],
            }
        )
    )
    for index in range(513):
        (many / f"f{index}.txt").write_text("x\n")
    assert skills_cmd.import_skill(target=tmp_path, source=many, json_output=True) == 0
    capsys.readouterr()

    packages = skills_cmd.user_profile_skill_packages(workspace=tmp_path, harness="codex", minimum_trust="workspace")
    assert "many" not in [p.skill_id for p in packages]

    big = tmp_path / "source" / "big"
    big.mkdir(parents=True)
    (big / "SKILL.md").write_text("# Big\n\nDo the work.\n")
    (big / "skill.json").write_text(
        json.dumps(
            {
                "id": "big",
                "title": "Big",
                "version": "1.0.0",
                "required_tools": [],
                "required_mcp_servers": [],
                "supported_harnesses": list(_USER_PROFILE_HARNESSES),
                "trust_level": "workspace",
                "tests": [],
            }
        )
    )
    (big / "blob.bin").write_bytes(b"0" * (8 * 1024 * 1024 + 1))
    assert skills_cmd.import_skill(target=tmp_path, source=big, json_output=True) == 0
    capsys.readouterr()

    packages = skills_cmd.user_profile_skill_packages(workspace=tmp_path, harness="codex", minimum_trust="workspace")
    assert "big" not in [p.skill_id for p in packages]


def test_user_profile_skill_packages_creates_no_directories(tmp_path, capsys):
    source = _write_reviewed_skill(tmp_path / "source", name="reviewed")
    assert skills_cmd.import_skill(target=tmp_path, source=source, json_output=True) == 0
    capsys.readouterr()

    before = {p for p in tmp_path.rglob("*") if p.is_dir()}
    packages = skills_cmd.user_profile_skill_packages(workspace=tmp_path, harness="codex", minimum_trust="workspace")
    after = {p for p in tmp_path.rglob("*") if p.is_dir()}
    assert packages
    assert before == after


# --- Round-5 review findings (PR #1211) -------------------------------------


def _state_receipt(workspace: Path, skill_id: str, harness: str) -> dict:
    return json.loads((workspace / ".brigade" / "skills" / "installs" / f"{skill_id}-{harness}.json").read_text())


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="platform lacks O_NOFOLLOW descriptor anchoring")
def test_install_refuses_source_swapped_between_lint_and_collection(tmp_path, capsys, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    source = _write_skill(tmp_path / "source")
    real_collect = skills_cmd._collect_source_tree

    def swap_then_collect(source_dir):
        # The attacker swaps the registry generation after the lint read but
        # before the bytes are collected; the swapped generation declares an
        # unknown trust level and must never be installed unlinted.
        (source_dir / "skill.json").write_text(
            json.dumps({"id": "security-review", "title": "Security Review", "trust_level": "not-a-level"})
        )
        return real_collect(source_dir)

    monkeypatch.setattr(skills_cmd, "_collect_source_tree", swap_then_collect)

    rc = skills_cmd.install(workspace=workspace, skill=str(source), harness="claude", json_output=True)

    captured = capsys.readouterr()
    assert rc != 0, captured.out + captured.err
    assert not (workspace / ".claude" / "skills" / "security-review").exists()


def test_rollback_refuses_modified_snapshot_and_restores_pristine(tmp_path, capsys):
    workspace, installed = _workspace_with_rollback_snapshot(tmp_path)
    receipt_path = workspace / ".brigade" / "skills" / "installs" / "security-review-claude.json"
    original_receipt = receipt_path.read_text()
    stamp = Path(json.loads(original_receipt)["rollback_snapshot"]).name
    snapshot_dir = workspace / ".brigade" / "skills" / "rollback" / "security-review" / "claude" / stamp
    pristine = (snapshot_dir / "SKILL.md").read_bytes()
    capsys.readouterr()

    (snapshot_dir / "SKILL.md").write_bytes(b"# tampered snapshot\n")
    rc = skills_cmd.rollback(workspace=workspace, skill="security-review", harness="claude")

    assert rc == 2
    captured = capsys.readouterr()
    assert "fingerprint" in captured.err
    assert installed.read_text() == "# B\n"
    assert snapshot_dir.is_dir()

    # an unbound legacy snapshot (no recorded fingerprint) is refused too
    receipt = json.loads(original_receipt)
    receipt["rollback_snapshot_fingerprint"] = None
    receipt_path.write_text(json.dumps(receipt))
    (snapshot_dir / "SKILL.md").write_bytes(pristine)
    rc = skills_cmd.rollback(workspace=workspace, skill="security-review", harness="claude")

    assert rc == 2
    assert installed.read_text() == "# B\n"
    assert snapshot_dir.is_dir()

    # the pristine bound snapshot still rolls back
    receipt_path.write_text(original_receipt)
    rc = skills_cmd.rollback(workspace=workspace, skill="security-review", harness="claude", json_output=True)

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["target"] == "claude"
    assert installed.read_bytes() == pristine
    assert not snapshot_dir.exists()


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform cannot create symlinks")
def test_publish_writes_proposal_through_anchor_not_symlinked_component(tmp_path, capsys):
    workspace = tmp_path / "ws"
    _seed_workspace_with_skill(workspace, tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "victim.json").write_text("keep\n")
    proposals = workspace / ".brigade" / "skills" / "publish-proposals"
    proposals.symlink_to(outside, target_is_directory=True)

    rc = skills_cmd.publish(target=workspace, skill="security-review", scope="public")

    assert rc == 2
    captured = capsys.readouterr()
    assert "state root" in captured.err or "state path" in captured.err or "refusing" in captured.err
    assert [path.name for path in outside.iterdir()] == ["victim.json"]
    assert not (workspace / ".brigade" / "skills" / "publish-proposals" / "security-review-public.json").exists()


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform cannot create symlinks")
def test_inbox_accept_and_reject_refuse_symlinked_proposal_directory(tmp_path, capsys):
    workspace = tmp_path / "ws"
    _seed_workspace_with_skill(workspace, tmp_path)
    capsys.readouterr()
    other = _write_skill(tmp_path / "other-source", name="docs-review")
    assert skills_cmd.inbox_add(target=workspace, source=other, summary="review", json_output=True) == 0
    proposal_id = json.loads(capsys.readouterr().out)["proposal_id"]
    inbox = workspace / ".brigade" / "skills" / "inbox"
    real_name = sorted(path.name for path in inbox.iterdir())[0]
    outside = tmp_path / "outside"
    planted = outside / real_name
    planted.mkdir(parents=True)
    (planted / "proposal.json").write_text(
        json.dumps({"proposal_id": real_name, "skill_id": "docs-review", "status": "pending"})
    )
    (planted / "skill").mkdir()
    (planted / "skill" / "SKILL.md").write_text("# outside planted skill\n")
    shutil.move(str(inbox / real_name), str(tmp_path / "real-proposal"))
    (inbox / real_name).symlink_to(planted, target_is_directory=True)
    capsys.readouterr()

    rc_accept = skills_cmd.inbox_accept(target=workspace, proposal_id=proposal_id)
    rc_reject = skills_cmd.inbox_reject(target=workspace, proposal_id=proposal_id, reason="no")

    assert rc_accept == 2
    assert rc_reject == 2
    meta = json.loads((planted / "proposal.json").read_text())
    assert meta["status"] == "pending"
    # the import must never launder the outside tree into the registry
    assert not (workspace / ".brigade" / "skills" / "registry" / "docs-review").exists()
    assert (planted / "skill" / "SKILL.md").read_text() == "# outside planted skill\n"


def test_pack_archive_never_honours_manifest_supplied_path(tmp_path, capsys):
    workspace = tmp_path / "ws"
    _seed_workspace_with_skill(workspace, tmp_path)
    trusted = tmp_path / "trusted-victim"
    trusted.mkdir()
    (trusted / "precious.txt").write_text("do not move\n")
    pack_id = "20990101-000000-forged-pack"
    pack_dir = workspace / ".brigade" / "skills" / "packs" / pack_id
    (pack_dir / "skills").mkdir(parents=True)
    (pack_dir / "skill-pack.json").write_text(
        json.dumps(
            {"pack_id": pack_id, "created_at": "2099-01-01T00:00:00+00:00", "skill_count": 0, "path": str(trusted)}
        )
    )

    rc = skills_cmd.pack_archive(target=workspace, pack_id=pack_id)

    assert rc == 0
    # the manifest-supplied physical path was never honoured as the move source
    assert (trusted / "precious.txt").is_file()
    archived = workspace / ".brigade" / "skills" / "packs-archive" / pack_id
    assert (archived / "skill-pack.json").is_file()
    assert not pack_dir.exists()


def test_round5_commands_fail_closed_without_descriptor_anchor(tmp_path, monkeypatch, capsys):
    workspace = tmp_path / "ws"
    _seed_workspace_with_skill(workspace, tmp_path)
    capsys.readouterr()
    candidate = _write_skill(tmp_path / "candidate-source", name="candidate-skill")
    assert skills_cmd.inbox_add(target=workspace, source=candidate, summary="c", json_output=True) == 0
    proposal_id = json.loads(capsys.readouterr().out)["proposal_id"]
    pack_id = "20990101-000000-packed"
    pack_dir = workspace / ".brigade" / "skills" / "packs" / pack_id
    pack_dir.mkdir(parents=True)
    (pack_dir / "skill-pack.json").write_text(
        json.dumps({"pack_id": pack_id, "created_at": "2099-01-01T00:00:00+00:00", "skill_count": 0})
    )
    monkeypatch.setattr(skills_cmd, "_HAS_DESCRIPTOR_ANCHOR", False)
    capsys.readouterr()

    assert skills_cmd.publish(target=workspace, skill="security-review", scope="public") == 2
    assert not (workspace / ".brigade" / "skills" / "publish-proposals").exists()

    assert skills_cmd.inbox_accept(target=workspace, proposal_id=proposal_id) == 2
    inbox_meta = json.loads(next((workspace / ".brigade" / "skills" / "inbox").glob("*/proposal.json")).read_text())
    assert inbox_meta["status"] == "pending"
    assert not (workspace / ".brigade" / "skills" / "registry" / "candidate-skill").exists()

    assert skills_cmd.inbox_reject(target=workspace, proposal_id=proposal_id, reason="r") == 2
    inbox_meta = json.loads(next((workspace / ".brigade" / "skills" / "inbox").glob("*/proposal.json")).read_text())
    assert inbox_meta["status"] == "pending"

    assert skills_cmd.pack_build(target=workspace) == 2
    assert [path.name for path in (workspace / ".brigade" / "skills" / "packs").iterdir()] == [pack_id]

    assert skills_cmd.pack_archive(target=workspace, pack_id=pack_id) == 2
    assert pack_dir.is_dir()
    assert not (workspace / ".brigade" / "skills" / "packs-archive").exists()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="platform cannot create FIFOs")
@pytest.mark.skipif(not hasattr(signal, "alarm"), reason="platform lacks alarm-based hang detection")
def test_install_refuses_fifo_receipt_and_history_without_blocking(tmp_path, capsys):
    workspace = tmp_path / "ws"
    _seed_workspace_with_skill(workspace, tmp_path)
    assert skills_cmd.install(workspace=workspace, skill="security-review", harness="claude", json_output=True) == 0
    capsys.readouterr()
    installs = workspace / ".brigade" / "skills" / "installs"
    original_receipt = (installs / "security-review-claude.json").read_bytes()

    class _Alarm(BaseException):
        pass

    def _raise_alarm(*_args):
        raise _Alarm()

    previous_handler = signal.signal(signal.SIGALRM, _raise_alarm)
    try:
        (installs / "security-review-claude.json").unlink()
        os.mkfifo(installs / "security-review-claude.json")
        signal.alarm(10)
        try:
            rc_receipt = skills_cmd.install(
                workspace=workspace, skill="security-review", harness="claude", force=True, json_output=True
            )
        except _Alarm:
            pytest.fail("install blocked on a FIFO planted at the receipt path")
        finally:
            signal.alarm(0)
        assert rc_receipt == 2
        capsys.readouterr()

        # restore a real receipt so only history.jsonl is hostile: the append
        # primitive itself must refuse the FIFO instead of blocking on open
        (installs / "security-review-claude.json").unlink()
        (installs / "security-review-claude.json").write_bytes(original_receipt)
        (installs / "history.jsonl").unlink(missing_ok=True)
        os.mkfifo(installs / "history.jsonl")
        signal.alarm(10)
        try:
            rc_history = skills_cmd.install(
                workspace=workspace, skill="security-review", harness="claude", force=True, json_output=True
            )
        except _Alarm:
            pytest.fail("install blocked on a FIFO planted at history.jsonl")
        finally:
            signal.alarm(0)
        assert rc_history == 2
        assert "error:" in capsys.readouterr().err
    finally:
        signal.signal(signal.SIGALRM, previous_handler)


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform cannot create symlinks")
def test_diff_and_history_reads_never_follow_symlinked_state_files(tmp_path, capsys):
    workspace = tmp_path / "ws"
    _seed_workspace_with_skill(workspace, tmp_path)
    assert skills_cmd.install(workspace=workspace, skill="security-review", harness="claude", json_output=True) == 0
    capsys.readouterr()
    installs = workspace / ".brigade" / "skills" / "installs"
    outside_receipt = tmp_path / "outside-receipt.json"
    marker = {"schema_version": 3, "marker": "OUTSIDE-RECEIPT-MARKER"}
    outside_receipt.write_text(json.dumps(marker))
    (installs / "security-review-claude.json").unlink()
    (installs / "security-review-claude.json").symlink_to(outside_receipt)
    outside_history = tmp_path / "outside-history.jsonl"
    outside_history.write_text('{"row": "OUTSIDE-HISTORY-MARKER"}\n')
    (installs / "history.jsonl").unlink()
    (installs / "history.jsonl").symlink_to(outside_history)

    assert skills_cmd.diff(target=workspace, skill="security-review", harness="claude", json_output=True) == 0
    diff_out = capsys.readouterr().out
    assert "OUTSIDE-RECEIPT-MARKER" not in diff_out
    assert json.loads(diff_out)["drift"]["receipt_known"] is False

    assert skills_cmd.history(target=workspace, json_output=True) == 0
    history_payload = json.loads(capsys.readouterr().out)
    assert "OUTSIDE-HISTORY-MARKER" not in json.dumps(history_payload)
    assert history_payload["count"] == 0

    assert outside_receipt.read_text() == json.dumps(marker)
    assert outside_history.read_text() == '{"row": "OUTSIDE-HISTORY-MARKER"}\n'


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform cannot create symlinks")
def test_inbox_read_commands_refuse_outside_proposal_content(tmp_path, capsys):
    workspace = tmp_path / "ws"
    _seed_workspace_with_skill(workspace, tmp_path)
    capsys.readouterr()
    source = _write_skill(tmp_path / "leak-source", name="leaky-skill")
    assert skills_cmd.inbox_add(target=workspace, source=source, summary="x", json_output=True) == 0
    proposal_id = json.loads(capsys.readouterr().out)["proposal_id"]
    inbox = workspace / ".brigade" / "skills" / "inbox"
    real_name = sorted(path.name for path in inbox.iterdir())[0]

    outside = tmp_path / "outside"
    planted = outside / real_name
    planted.mkdir(parents=True)
    (planted / "proposal.json").write_text(
        json.dumps(
            {
                "proposal_id": real_name,
                "skill_id": "leaky-skill",
                "status": "pending",
                "marker": "OUTSIDE-PROPOSAL-MARKER",
            }
        )
    )
    (planted / "skill").mkdir()
    (planted / "skill" / "SKILL.md").write_text("# OUTSIDE-SKILL-MARKER\n")
    shutil.move(str(inbox / real_name), str(tmp_path / "real-proposal"))
    (inbox / real_name).symlink_to(planted, target_is_directory=True)

    assert skills_cmd.inbox_list(target=workspace, json_output=True) == 0
    listing = json.loads(capsys.readouterr().out)
    assert listing["proposal_count"] == 0
    assert "OUTSIDE-PROPOSAL-MARKER" not in json.dumps(listing)

    assert skills_cmd.inbox_show(target=workspace, proposal_id=proposal_id) == 2
    assert "not found" in capsys.readouterr().err

    assert skills_cmd.inbox_diff(target=workspace, proposal_id=proposal_id) == 2
    diff_err = capsys.readouterr().err + capsys.readouterr().out
    assert "OUTSIDE-SKILL-MARKER" not in diff_err

    # a symlinked proposal file inside a plain proposal directory is refused too
    plain = inbox / "20980101-000000-second"
    plain.mkdir()
    outside_file = tmp_path / "outside-meta.json"
    outside_file.write_text('{"marker": "OUTSIDE-FILE-MARKER"}')
    (plain / "proposal.json").symlink_to(outside_file)

    assert skills_cmd.inbox_list(target=workspace, json_output=True) == 0
    listing = json.loads(capsys.readouterr().out)
    assert listing["proposal_count"] == 0
    assert "OUTSIDE-FILE-MARKER" not in json.dumps(listing)
    assert outside_file.read_text() == '{"marker": "OUTSIDE-FILE-MARKER"}'


# --- round-5 review findings: close the state-root read class -----------------


def _import_sync_skill(tmp_path, name="sync-victim"):
    """Import one registry skill into tmp_path and return the workspace."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    source = _write_skill(tmp_path / "sources", name=name)
    metadata_path = source / "skill.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["supported_harnesses"] = ["codex"]
    metadata_path.write_text(json.dumps(metadata))
    assert skills_cmd.import_skill(target=workspace, source=source, json_output=True) == 0
    return workspace


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform cannot create symlinks")
def test_sync_write_installs_exactly_the_linted_snapshot_bytes(tmp_path, capsys, monkeypatch):
    workspace = _import_sync_skill(tmp_path)
    # Control run: capture exactly what a clean sync writes.
    assert skills_cmd.sync(workspace=workspace, harness="codex", trust="workspace", write=True, json_output=True) == 0
    installed = workspace / ".codex" / "skills" / "sync-victim" / "SKILL.md"
    expected_bytes = installed.read_bytes()
    shutil.rmtree(workspace / ".codex" / "skills" / "sync-victim")

    real_lint_payload = skills_cmd._lint_payload
    staged_lint_calls = {"count": 0}
    registry_md = workspace / ".brigade" / "skills" / "registry" / "sync-victim" / "SKILL.md"

    def lint_then_swap_source(target, skill_or_path, harness=None, *, mode="lenient"):
        payload = real_lint_payload(target, skill_or_path, harness=harness, mode=mode)
        # The attacker swaps the registry generation right after the
        # projection pass has linted its anchored snapshot; the installed
        # bytes must still be exactly the snapshot that was linted in that
        # same pass, never a generation nobody linted.
        if "brigade-sync-lint-" in str(skill_or_path):
            staged_lint_calls["count"] += 1
            registry_md.write_bytes(b"# SWAPPED-AFTER-LINT-MARKER\n")
        return payload

    monkeypatch.setattr(skills_cmd, "_lint_payload", lint_then_swap_source)

    assert skills_cmd.sync(workspace=workspace, harness="codex", trust="workspace", write=True, json_output=True) == 0
    capsys.readouterr()
    assert staged_lint_calls["count"] >= 1
    assert installed.is_file()
    assert installed.read_bytes() == expected_bytes
    assert b"SWAPPED-AFTER-LINT-MARKER" not in installed.read_bytes()


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform cannot create symlinks")
def test_sync_write_refuses_symlinked_history(tmp_path, capsys):
    workspace = _import_sync_skill(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim-history.jsonl"
    victim.write_text('{"note": "VICTIM-HISTORY-MARKER"}\n')
    installs = workspace / ".brigade" / "skills" / "installs"
    installs.mkdir(parents=True)
    (installs / "history.jsonl").symlink_to(victim)

    rc = skills_cmd.sync(workspace=workspace, harness="codex", trust="workspace", write=True, json_output=True)

    captured = capsys.readouterr()
    assert rc != 0, captured.out + captured.err
    # The anchored read refuses before any plan material exists; the generic
    # projection-parent refusal would name no file at all.
    assert "history.jsonl" in captured.err
    assert "VICTIM-HISTORY-MARKER" not in captured.out + captured.err
    assert not (workspace / ".codex" / "skills" / "sync-victim").exists()


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform cannot create symlinks")
def test_sync_write_refuses_symlinked_receipt(tmp_path, capsys):
    workspace = _import_sync_skill(tmp_path)
    assert skills_cmd.install(workspace=workspace, skill="registry:sync-victim", harness="codex", json_output=True) == 0
    capsys.readouterr()
    # Force an install action even though the receipt is unreadable.
    shutil.rmtree(workspace / ".codex" / "skills" / "sync-victim")

    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim-receipt.json"
    victim.write_text('{"note": "VICTIM-RECEIPT-MARKER"}')
    receipt = workspace / ".brigade" / "skills" / "installs" / "sync-victim-codex.json"
    receipt.unlink()
    receipt.symlink_to(victim)

    rc = skills_cmd.sync(workspace=workspace, harness="codex", trust="workspace", write=True, json_output=True)

    captured = capsys.readouterr()
    assert rc != 0, captured.out + captured.err
    assert "sync-victim-codex.json" in captured.err
    assert "VICTIM-RECEIPT-MARKER" not in captured.out + captured.err


def _registry_with_symlinked_entry(tmp_path, placement):
    """Registry entry whose dir, SKILL.md, or skill.json is an outside symlink."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside" / "linked-skill"
    outside.mkdir(parents=True)
    (outside / "SKILL.md").write_text("# LINKED-SKILL-MD-MARKER\n")
    (outside / "CHANGELOG.md").write_text("# LINKED-CHANGELOG-MARKER\n\n- outside note.\n")
    (outside / "skill.json").write_text(
        json.dumps(
            {
                "id": "linked-skill",
                "title": "Linked Skill LINKED-TITLE-MARKER",
                "trust_level": "LINKED-TRUST-MARKER",
            }
        )
    )
    registry = workspace / ".brigade" / "skills" / "registry"
    if placement == "dir":
        entry = registry / "linked-skill"
        entry.parent.mkdir(parents=True)
        entry.symlink_to(outside, target_is_directory=True)
    else:
        plain = _write_skill(registry, name="linked-skill")
        if placement == "skill_md":
            (plain / "SKILL.md").unlink()
            (plain / "SKILL.md").symlink_to(outside / "SKILL.md")
        elif placement == "skill_json":
            (plain / "skill.json").unlink()
            (plain / "skill.json").symlink_to(outside / "skill.json")
        else:
            raise AssertionError(f"unknown placement: {placement}")
    return workspace


_MCP_READ_TOOLS = (
    ("get_skill", {"skill_id": "linked-skill"}),
    ("get_skill_metadata", {"skill_id": "linked-skill"}),
    ("get_skill_changelog", {"skill_id": "linked-skill"}),
    ("get_skill_compatibility", {"skill_id": "linked-skill"}),
    ("lint_skill", {"skill_id": "linked-skill"}),
)


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform cannot create symlinks")
@pytest.mark.parametrize("placement", ["dir", "skill_md", "skill_json"])
def test_registry_read_commands_never_echo_symlinked_entry_content(tmp_path, capsys, placement):
    workspace = _registry_with_symlinked_entry(tmp_path, placement)
    markers = (
        "LINKED-SKILL-MD-MARKER",
        "LINKED-CHANGELOG-MARKER",
        "LINKED-TITLE-MARKER",
        "LINKED-TRUST-MARKER",
    )

    def _assert_no_marker(text, context):
        for marker in markers:
            assert marker not in text, f"{context} leaked {marker}"

    # lint (text and JSON): refused or absent, never echoing outside content
    assert skills_cmd.lint(target=workspace, skill="registry:linked-skill") != 0
    captured = capsys.readouterr()
    _assert_no_marker(captured.out + captured.err, "lint text")
    assert skills_cmd.lint(target=workspace, skill="registry:linked-skill", json_output=True) != 0
    lint_out = capsys.readouterr().out
    _assert_no_marker(lint_out, "lint json")

    # diff --against registry
    diff_rc = skills_cmd.diff(target=workspace, skill="registry:linked-skill", harness="codex", against="registry")
    captured = capsys.readouterr()
    assert diff_rc != 0
    _assert_no_marker(captured.out + captured.err, "diff text")
    skills_cmd.diff(
        target=workspace, skill="registry:linked-skill", harness="codex", against="registry", json_output=True
    )
    _assert_no_marker(capsys.readouterr().out + capsys.readouterr().err, "diff json")

    # search --json metadata
    assert skills_cmd.search(target=workspace, query="linked", json_output=True) == 0
    search_out = capsys.readouterr().out
    _assert_no_marker(search_out, "search json")
    payload = json.loads(search_out)
    if placement == "dir":
        assert payload["count"] == 0

    # MCP reads and tool calls
    text, found = skills_cmd._mcp_read_resource(workspace, "skill://registry/linked-skill/SKILL.md")
    if text is not None:
        _assert_no_marker(text, "mcp resource SKILL.md")
    text, found = skills_cmd._mcp_read_resource(workspace, "skill://registry/linked-skill/skill.json")
    if text is not None:
        _assert_no_marker(text, "mcp resource skill.json")
    text, found = skills_cmd._mcp_read_resource(workspace, "skill://registry/linked-skill/CHANGELOG.md")
    assert text is None or "LINKED-CHANGELOG-MARKER" not in text
    for tool, arguments in _MCP_READ_TOOLS:
        result, is_error = skills_cmd._mcp_tool_call(workspace, tool, arguments)
        _assert_no_marker(json.dumps(result, default=str), f"mcp tool {tool}")

    # fleet status payload
    fleet_payload = skills_cmd._fleet_status_payload(workspace)
    _assert_no_marker(json.dumps(fleet_payload), "fleet status")

    # profile packages
    packages = skills_cmd.user_profile_skill_packages(workspace=workspace, harness="codex", minimum_trust="workspace")
    _assert_no_marker(repr(packages), "profile packages")
    assert all(package.skill_id != "linked-skill" for package in packages)

    # doctor-style health issues
    issues = skills_cmd._skill_health_issues(workspace)
    _assert_no_marker(json.dumps(issues), "health issues")


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform cannot create symlinks")
def test_pack_list_and_show_refuse_symlinked_pack_dir_and_manifest(tmp_path, capsys):
    workspace = _import_sync_skill(tmp_path)
    capsys.readouterr()
    assert skills_cmd.pack_build(target=workspace, json_output=True) == 0
    built = json.loads(capsys.readouterr().out)
    packs_root = workspace / ".brigade" / "skills" / "packs"
    real_pack = packs_root / built["pack_id"]

    # case 1: whole pack directory symlinked to an outside tree with a marker manifest
    outside_pack = tmp_path / "outside-pack"
    shutil.move(str(real_pack), str(outside_pack))
    pristine_manifest = (outside_pack / "skill-pack.json").read_bytes()
    manifest = json.loads(pristine_manifest)
    manifest["PACK-DIR-MANIFEST-MARKER"] = True
    (outside_pack / "skill-pack.json").write_text(json.dumps(manifest))
    real_pack.symlink_to(outside_pack, target_is_directory=True)

    def _assert_no_marker(text, context):
        assert "PACK-DIR-MANIFEST-MARKER" not in text, context

    assert skills_cmd.pack_list(target=workspace, json_output=True) == 0
    listing = capsys.readouterr().out
    _assert_no_marker(listing, "pack list json")
    assert json.loads(listing)["pack_count"] == 0
    assert skills_cmd.pack_show(target=workspace, pack_id=built["pack_id"]) != 0
    captured = capsys.readouterr()
    _assert_no_marker(captured.out + captured.err, "pack show text")

    # restore the plain pack with its pristine manifest, then symlink only
    # the manifest outside
    real_pack.unlink()
    shutil.move(str(outside_pack), str(real_pack))
    (real_pack / "skill-pack.json").write_bytes(pristine_manifest)
    outside_manifest = tmp_path / "outside-manifest" / "skill-pack.json"
    (real_pack / "skill-pack.json").unlink()
    outside_manifest.parent.mkdir()
    manifest_text = pristine_manifest.decode()
    (real_pack / "skill-pack.json").symlink_to(outside_manifest)
    assert "PACK-DIR-MANIFEST-MARKER" not in manifest_text

    assert skills_cmd.pack_list(target=workspace, json_output=True) == 0
    listing = capsys.readouterr().out
    _assert_no_marker(listing, "pack list json with symlinked manifest")
    assert json.loads(listing)["pack_count"] == 0

    assert skills_cmd.pack_show(target=workspace, pack_id="latest") != 0
    captured = capsys.readouterr()
    _assert_no_marker(captured.out + captured.err, "pack show latest")


def test_lint_missing_skill_json_source_has_registry_kind(tmp_path, capsys):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    assert skills_cmd.lint(target=workspace, skill="session-sweep", json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    source = payload["source"]
    assert isinstance(source, dict)
    assert isinstance(source["kind"], str)
    assert source["kind"] == "registry"
    assert source["identity"] == "registry://skills/session-sweep"
    assert payload["valid"] is False
    assert payload["errors"]


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform cannot create symlinks")
def test_lint_refused_symlinked_registry_entry_source_kind_and_no_content_echo(tmp_path, capsys):
    workspace = _registry_with_symlinked_entry(tmp_path, "skill_json")
    assert skills_cmd.lint(target=workspace, skill="registry:linked-skill", json_output=True) == 1
    out = capsys.readouterr()
    for marker in ("LINKED-SKILL-MD-MARKER", "LINKED-TITLE-MARKER", "LINKED-TRUST-MARKER"):
        assert marker not in out.out + out.err
    payload = json.loads(out.out)
    source = payload["source"]
    assert isinstance(source, dict)
    assert source["kind"] == "registry"
    assert source["identity"] == "registry://skills/linked-skill"
    assert payload["valid"] is False
    assert any("skill.json" in message for message in payload["errors"])


# --- round-6 review findings: pathname reads of state-root content -------------


def _round6_workspace(tmp_path, capsys, name="state-victim"):
    """Fresh workspace with one imported registry skill (mcp-capable)."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    source = _write_skill(tmp_path / "sources", name=name)
    assert skills_cmd.import_skill(target=workspace, source=source, json_output=True) == 0
    capsys.readouterr()
    return workspace


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform cannot create symlinks")
def test_state_backed_install_reads_go_through_the_anchor(tmp_path, capsys):
    workspace = _round6_workspace(tmp_path, capsys, name="mcp-victim")
    assert skills_cmd.install(workspace=workspace, skill="registry:mcp-victim", harness="mcp", json_output=True) == 0
    capsys.readouterr()
    installed_md = workspace / ".brigade" / "skills" / "mcp-resources" / "mcp-victim" / "SKILL.md"
    assert installed_md.is_file()
    outside = tmp_path / "outside-secret.md"
    outside.write_text("# INSTALLED-SYMLINK-MARKER\n")
    installed_md.unlink()
    installed_md.symlink_to(outside)

    # Advance the registry so diff and sync have a changed generation to compare.
    registry_md = workspace / ".brigade" / "skills" / "registry" / "mcp-victim" / "SKILL.md"
    registry_md.write_text("# Security Review v2\n\nChanged guidance.\n")

    assert skills_cmd.diff(target=workspace, skill="registry:mcp-victim", harness="mcp") == 0
    diff_out = capsys.readouterr().out
    assert "INSTALLED-SYMLINK-MARKER" not in diff_out

    assert skills_cmd.compatibility(target=workspace, skill="registry:mcp-victim", json_output=True) in {0, 1}
    compat_out = capsys.readouterr().out
    assert "INSTALLED-SYMLINK-MARKER" not in compat_out

    assert skills_cmd.fleet_status(target=workspace, json_output=True) == 0
    fleet_out = capsys.readouterr().out
    assert "INSTALLED-SYMLINK-MARKER" not in fleet_out

    assert skills_cmd.sync(workspace=workspace, harness="mcp", trust="workspace", write=True, json_output=True) == 0
    sync_out = capsys.readouterr().out
    assert "INSTALLED-SYMLINK-MARKER" not in sync_out
    # The contaminated state-backed destination is refused for this run: the
    # planted symlink is neither followed, overwritten blind, nor copied into
    # rollback material.
    sync_payload = json.loads(sync_out)
    assert sync_payload["applied"]["updated"] == 0
    assert any(item["result"] == "excluded" for item in sync_payload["items"])
    assert installed_md.is_symlink()
    rollback_root = workspace / ".brigade" / "skills" / "rollback"
    if rollback_root.is_dir():
        for rollback_file in rollback_root.rglob("*"):
            if rollback_file.is_file():
                assert b"INSTALLED-SYMLINK-MARKER" not in rollback_file.read_bytes()

    # A clean reinstall through install --force heals the destination from
    # registry bytes only.
    assert skills_cmd.install(workspace=workspace, skill="registry:mcp-victim", harness="mcp", force=True) == 0
    capsys.readouterr()
    assert installed_md.is_file() and not installed_md.is_symlink()
    assert b"INSTALLED-SYMLINK-MARKER" not in installed_md.read_bytes()


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform cannot create symlinks")
def test_explicit_registry_pathname_selects_the_anchored_entry(tmp_path, capsys):
    workspace = _round6_workspace(tmp_path, capsys, name="path-victim")
    entry_md = workspace / ".brigade" / "skills" / "registry" / "path-victim" / "SKILL.md"
    outside = tmp_path / "outside-registry-secret.md"
    outside.write_text("# REGISTRY-PATH-MARKER\n")
    entry_md.unlink()
    entry_md.symlink_to(outside)

    explicit = str(workspace / ".brigade" / "skills" / "registry" / "path-victim")
    assert skills_cmd.lint(target=workspace, skill=explicit) == 1
    lint_cap = capsys.readouterr()
    assert "REGISTRY-PATH-MARKER" not in lint_cap.out + lint_cap.err

    # An existing state-root pathname is never treated as a trusted external
    # source: it resolves to the anchored registry entry, which refuses.
    assert skills_cmd.diff(target=workspace, skill=explicit, harness="codex") == 1
    diff_cap = capsys.readouterr()
    assert "REGISTRY-PATH-MARKER" not in diff_cap.out + diff_cap.err


def test_import_and_inbox_lint_consume_collected_bytes(tmp_path, capsys, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    import_source = _write_skill(tmp_path / "sources", name="collect-victim")
    inbox_source = _write_skill(tmp_path / "sources2", name="inbox-collect-victim")
    real_write = skills_cmd._write_collected_tree_into_anchor
    swapped = {"count": 0}

    def write_then_swap(dirs, files, anchor, *relative):
        real_write(dirs, files, anchor, *relative)
        swapped["count"] += 1
        outside = tmp_path / f"swapped-secret-{swapped['count']}.md"
        marker = "# COLLECTED-SWAP-MARKER\n" if relative[1] == "registry" else "# INBOX-COLLECTED-SWAP-MARKER\n"
        outside.write_text(marker)
        entry_md = (workspace / ".brigade").joinpath(*relative, "SKILL.md")
        entry_md.unlink()
        entry_md.symlink_to(outside)

    monkeypatch.setattr(skills_cmd, "_write_collected_tree_into_anchor", write_then_swap)

    expected_import = skills_cmd._files_fingerprint(skills_cmd._collect_source_tree(import_source)[1])
    assert skills_cmd.import_skill(target=workspace, source=import_source, json_output=True) == 0
    imported = json.loads(capsys.readouterr().out)
    assert imported["lint"]["valid"] is True
    # The recorded fingerprint describes the collected bytes, not whatever the
    # state-root pathname points at after the copy.
    assert imported["lint"]["fingerprint"] == expected_import
    assert "COLLECTED-SWAP-MARKER" not in json.dumps(imported)

    expected_inbox = skills_cmd._files_fingerprint(skills_cmd._collect_source_tree(inbox_source)[1])
    assert skills_cmd.inbox_add(target=workspace, source=inbox_source, summary="pending", json_output=True) == 0
    proposal_out = capsys.readouterr().out
    proposal = json.loads(proposal_out)
    assert proposal["lint"]["valid"] is True
    assert proposal["lint"]["fingerprint"] == expected_inbox
    assert "INBOX-COLLECTED-SWAP-MARKER" not in proposal_out
    # The staged-lint repointer keeps temporary staging directories out of the
    # persisted proposal metadata.
    skill_dest_display = workspace / ".brigade" / "skills" / "inbox" / str(proposal["proposal_id"]) / "skill"
    assert "brigade-inbox-lint-" not in proposal_out
    if proposal["lint"].get("changelog", {}).get("present"):
        assert proposal["lint"]["changelog"]["path"] == str(skill_dest_display / "CHANGELOG.md")
    assert swapped["count"] == 2


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform cannot create symlinks")
def test_sync_write_regates_generation_swapped_before_projection(tmp_path, capsys, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    source = _write_skill(tmp_path / "sources", name="swap-victim")
    assert skills_cmd.import_skill(target=workspace, source=source, json_output=True) == 0
    capsys.readouterr()
    registry_dir = workspace / ".brigade" / "skills" / "registry" / "swap-victim"

    real_lint_payload = skills_cmd._lint_payload
    fired = {"swapped": False}

    def lint_then_swap_before_projection(target, skill_or_path, harness=None, *, mode="lenient"):
        payload = real_lint_payload(target, skill_or_path, harness=harness, mode=mode)
        # Fire once the planning pass has gated on generation A: generation B
        # (lint-valid, unreviewed metadata) appears before the projection
        # snapshot, and sync --write must re-gate on it instead of installing.
        if str(skill_or_path).startswith("registry:") and not fired["swapped"]:
            fired["swapped"] = True
            md = registry_dir / "SKILL.md"
            md.write_text("# SWAPPED-PROJECTION-MARKER\n\nUnreviewed body.\n")
            meta = json.loads((registry_dir / "skill.json").read_text())
            meta["trust_level"] = "unreviewed"
            (registry_dir / "skill.json").write_text(json.dumps(meta))
        return payload

    monkeypatch.setattr(skills_cmd, "_lint_payload", lint_then_swap_before_projection)

    rc = skills_cmd.sync(workspace=workspace, harness="codex", trust="workspace", write=True, json_output=True)
    assert fired["swapped"]
    payload = json.loads(capsys.readouterr().out)
    installed = workspace / ".codex" / "skills" / "swap-victim" / "SKILL.md"
    assert not installed.exists()
    assert payload["applied"]["installed"] == 0
    assert rc == 0


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform cannot create symlinks")
def test_pack_commands_refuse_symlinked_state_pack_directory(tmp_path, capsys):
    workspace = _round6_workspace(tmp_path, capsys, name="pack-victim")
    assert skills_cmd.pack_build(target=workspace, json_output=True) == 0
    built = json.loads(capsys.readouterr().out)
    pack_display = Path(built["path"])
    pack_name = pack_display.name
    assert pack_display.is_dir()

    outside = tmp_path / "outside-pack"
    outside.mkdir(parents=True)
    manifest = dict(built)
    manifest["pack_id"] = pack_name
    manifest["note"] = "PACK-LINK-MARKER"
    (outside / "skill-pack.json").write_text(json.dumps(manifest))
    shutil.copytree(pack_display, outside / "skills" / "linked-victim")
    shutil.rmtree(pack_display)
    pack_display.symlink_to(outside)

    assert skills_cmd.pack_show(target=workspace, pack_id=str(pack_display)) != 0
    show_cap = capsys.readouterr()
    assert "PACK-LINK-MARKER" not in show_cap.out + show_cap.err

    assert skills_cmd.pack_import(target=workspace, pack=pack_display) != 0
    import_cap = capsys.readouterr()
    assert "PACK-LINK-MARKER" not in import_cap.out + import_cap.err
    assert not (workspace / ".brigade" / "skills" / "registry" / "linked-victim").exists()


def test_no_tmp_staging_paths_in_install_errors_or_proposal_metadata(tmp_path, capsys):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    bad = _write_skill(tmp_path / "sources", name="bad-metadata")
    meta = json.loads((bad / "skill.json").read_text())
    meta["trust_level"] = "bogus-level"
    (bad / "skill.json").write_text(json.dumps(meta))

    assert skills_cmd.install(workspace=workspace, skill=str(bad), harness="codex", json_output=True) == 1
    captured = capsys.readouterr()
    blob = captured.out + captured.err
    assert "brigade-install-lint-" not in blob
    assert "brigade-import-lint-" not in blob
    error_payload = json.loads(captured.out)
    # Changelog references point at the real source, never the staging copy.
    assert error_payload["lint"]["changelog"]["path"] == str(bad / "CHANGELOG.md")
    assert error_payload["lint"]["trust_score"]["changelog"]["path"] == str(bad / "CHANGELOG.md")

    good = _write_skill(tmp_path / "sources2", name="provenance-victim")
    original_source = str(good.resolve())
    assert skills_cmd.inbox_add(target=workspace, source=good, json_output=True) == 0
    added = json.loads(capsys.readouterr().out)
    assert skills_cmd.inbox_accept(target=workspace, proposal_id=str(added["proposal_id"]), json_output=True) == 0
    accepted = json.loads(capsys.readouterr().out)
    registry_meta = json.loads(
        (workspace / ".brigade" / "skills" / "registry" / "provenance-victim" / "skill.json").read_text()
    )
    # Accepted proposals keep their original provenance, never the private
    # staging directory the import ran from.
    assert registry_meta.get("source") == original_source
    assert "brigade-proposal-import-" not in json.dumps(accepted)


# --- #1214 findings 1-3: classify / snapshot / alias residuals -----------------


def _trusted_private_skill(workspace: Path, tmp_path: Path, name: str = "private") -> Path:
    """A trusted in-workspace .claude skill used as a symlink follow target."""
    trusted = _write_skill(workspace / ".claude" / "skills", name=name)
    (trusted / "SKILL.md").write_text("# TRUSTED-PRIVATE-SKILL-MARKER\n")
    return trusted


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform cannot create symlinks")
def test_import_refuses_state_root_symlink_to_trusted_skill(tmp_path, capsys):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    trusted = _trusted_private_skill(workspace, tmp_path)
    planted = workspace / ".brigade" / "skills" / "planted-import"
    planted.parent.mkdir(parents=True)
    planted.symlink_to(trusted, target_is_directory=True)

    rc = skills_cmd.import_skill(target=workspace, source=planted, json_output=True)

    assert rc == 2
    captured = capsys.readouterr()
    assert "TRUSTED-PRIVATE-SKILL-MARKER" not in captured.out + captured.err
    registry_root = workspace / ".brigade" / "skills" / "registry"
    if registry_root.is_dir():
        for path in registry_root.rglob("*"):
            if path.is_file() and not path.is_symlink():
                assert b"TRUSTED-PRIVATE-SKILL-MARKER" not in path.read_bytes()
    assert trusted.joinpath("SKILL.md").read_text() == "# TRUSTED-PRIVATE-SKILL-MARKER\n"


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform cannot create symlinks")
def test_inbox_add_refuses_state_root_symlink_to_trusted_skill(tmp_path, capsys):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    trusted = _trusted_private_skill(workspace, tmp_path, name="inbox-private")
    planted = workspace / ".brigade" / "skills" / "planted-inbox"
    planted.parent.mkdir(parents=True)
    planted.symlink_to(trusted, target_is_directory=True)

    rc = skills_cmd.inbox_add(target=workspace, source=planted, summary="review me")

    assert rc == 2
    captured = capsys.readouterr()
    assert "TRUSTED-PRIVATE-SKILL-MARKER" not in captured.out + captured.err
    inbox_root = workspace / ".brigade" / "skills" / "inbox"
    if inbox_root.is_dir():
        for path in inbox_root.rglob("*"):
            if path.is_file() and not path.is_symlink():
                assert b"TRUSTED-PRIVATE-SKILL-MARKER" not in path.read_bytes()
    assert trusted.joinpath("SKILL.md").read_text() == "# TRUSTED-PRIVATE-SKILL-MARKER\n"


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform cannot create symlinks")
def test_forced_install_directory_symlink_does_not_snapshot_trusted_bytes(tmp_path, capsys):
    workspace = _round6_workspace(tmp_path, capsys, name="mcp-victim")
    assert skills_cmd.install(workspace=workspace, skill="registry:mcp-victim", harness="mcp", json_output=True) == 0
    assert skills_cmd.install(workspace=workspace, skill="registry:mcp-victim", harness="claude", json_output=True) == 0
    capsys.readouterr()
    trusted = workspace / ".claude" / "skills" / "mcp-victim"
    assert trusted.is_dir()
    (trusted / "SKILL.md").write_text("# TRUSTED-CLAUDE-INSTALL-MARKER\n")

    resources = workspace / ".brigade" / "skills" / "mcp-resources"
    shutil.move(str(resources), str(tmp_path / "real-mcp-resources"))
    resources.symlink_to(trusted.parent, target_is_directory=True)

    rc = skills_cmd.install(workspace=workspace, skill="registry:mcp-victim", harness="mcp", force=True)

    assert rc == 2
    captured = capsys.readouterr()
    assert "TRUSTED-CLAUDE-INSTALL-MARKER" not in captured.out + captured.err
    rollback_root = workspace / ".brigade" / "skills" / "rollback"
    if rollback_root.is_dir():
        for rollback_file in rollback_root.rglob("*"):
            if rollback_file.is_file() and not rollback_file.is_symlink():
                assert b"TRUSTED-CLAUDE-INSTALL-MARKER" not in rollback_file.read_bytes()
    assert trusted.joinpath("SKILL.md").read_text() == "# TRUSTED-CLAUDE-INSTALL-MARKER\n"


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform cannot create symlinks")
def test_state_root_selector_matches_symlinked_workspace_alias(tmp_path, capsys):
    workspace = _round6_workspace(tmp_path, capsys, name="alias-victim")
    entry_md = workspace / ".brigade" / "skills" / "registry" / "alias-victim" / "SKILL.md"
    outside = tmp_path / "outside-alias-secret.md"
    outside.write_text("# ALIAS-REGISTRY-MARKER\n")
    entry_md.unlink()
    entry_md.symlink_to(outside)

    alias = tmp_path / "ws-alias"
    alias.symlink_to(workspace, target_is_directory=True)
    requested = str(alias / ".brigade" / "skills" / "registry" / "alias-victim")

    assert skills_cmd._state_root_selector_kind(workspace.resolve(), requested) == "registry"
    assert skills_cmd.lint(target=workspace, skill=requested) == 1
    lint_cap = capsys.readouterr()
    assert "ALIAS-REGISTRY-MARKER" not in lint_cap.out + lint_cap.err


# --- #1214 Daybreak residuals: case-variant .brigade and raw-lexical .. escape -


def _assert_trusted_bytes_not_copied(workspace: Path, marker: bytes = b"TRUSTED-PRIVATE-SKILL-MARKER") -> None:
    for root_name in ("registry", "inbox"):
        root = workspace / ".brigade" / "skills" / root_name
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path.is_file() and not path.is_symlink():
                assert marker not in path.read_bytes()


@pytest.mark.parametrize("brigade_name", [".brigade", ".BRIGADE", ".Brigade"])
def test_state_root_classification_is_case_insensitive_for_brigade_component(tmp_path, monkeypatch, brigade_name):
    """Bypass A: a case-variant .brigade component must classify as state-root.

    Windows (and other case-insensitive filesystems) treat ``.BRIGADE`` as the
    state root. The comparison uses ``os.path.normcase`` so the parametrized
    casings also fail-closed on Linux when normcase is the identity.
    """
    monkeypatch.setattr(os.path, "normcase", lambda p: str(p).casefold())
    workspace = tmp_path / "ws"
    workspace.mkdir()
    source = workspace / brigade_name / "skills" / "planted"
    assert skills_cmd._lexical_state_root_parts(workspace, source) == ("skills", "planted")
    assert skills_cmd._state_root_selector_kind(workspace, str(source)) == "refuse"


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform cannot create symlinks")
@pytest.mark.parametrize("brigade_name", [".brigade", ".BRIGADE", ".Brigade"])
def test_import_refuses_case_variant_state_root_symlink_to_trusted_skill(tmp_path, capsys, monkeypatch, brigade_name):
    """Bypass A: ``.BRIGADE/skills/planted`` must not resolve and copy trusted bytes."""
    monkeypatch.setattr(os.path, "normcase", lambda p: str(p).casefold())
    workspace = tmp_path / "ws"
    workspace.mkdir()
    trusted = _trusted_private_skill(workspace, tmp_path)
    planted = workspace / ".brigade" / "skills" / "planted"
    planted.parent.mkdir(parents=True)
    planted.symlink_to(trusted, target_is_directory=True)
    source = workspace / brigade_name / "skills" / "planted"
    assert skills_cmd._lexical_state_root_parts(workspace, source) == ("skills", "planted")

    rc = skills_cmd.import_skill(target=workspace, source=source, json_output=True)

    assert rc == 2
    captured = capsys.readouterr()
    assert "TRUSTED-PRIVATE-SKILL-MARKER" not in captured.out + captured.err
    _assert_trusted_bytes_not_copied(workspace)
    assert trusted.joinpath("SKILL.md").read_text() == "# TRUSTED-PRIVATE-SKILL-MARKER\n"


def test_import_refuses_state_root_dotdot_escape_without_copying_bytes(tmp_path, capsys):
    """Bypass B: ``.brigade/skills/../../.claude/skills/private`` must refuse, not resolve."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    trusted = _trusted_private_skill(workspace, tmp_path)
    source = workspace / ".brigade" / "skills" / ".." / ".." / ".claude" / "skills" / trusted.name
    assert ".." in source.parts
    assert skills_cmd._lexical_state_root_parts(workspace, source) is not None
    assert skills_cmd._state_root_selector_kind(workspace, str(source)) == "refuse"

    rc = skills_cmd.import_skill(target=workspace, source=source, json_output=True)

    assert rc == 2
    captured = capsys.readouterr()
    assert "TRUSTED-PRIVATE-SKILL-MARKER" not in captured.out + captured.err
    _assert_trusted_bytes_not_copied(workspace)
    assert trusted.joinpath("SKILL.md").read_text() == "# TRUSTED-PRIVATE-SKILL-MARKER\n"


# --- #1214 Daybreak residual: Windows drive-relative and current-drive-rooted paths -


_WINDOWS_DRIVE_RELATIVE_SOURCES = (
    r"D:.BRIGADE\skills\planted",
    r"D:.brigade\skills\..\..\.claude\skills\private",
)
_WINDOWS_ROOTED_WITHOUT_DRIVE = r"\.brigade\skills\x"


@pytest.mark.parametrize("raw", [*_WINDOWS_DRIVE_RELATIVE_SOURCES, _WINDOWS_ROOTED_WITHOUT_DRIVE])
@pytest.mark.parametrize("pathmod", [ntpath, os.path], ids=["ntpath", "os.path"])
def test_absolute_lexical_path_refuses_windows_unrooted_forms(raw, pathmod, monkeypatch):
    """Drive-relative / current-drive-rooted forms must not join process cwd."""
    cwd_calls: list[str] = []

    def _track_cwd() -> str:
        cwd_calls.append("called")
        return r"C:\other"

    monkeypatch.setattr(os, "getcwd", _track_cwd)
    with pytest.raises(UnsupportedLexicalPathError, match="drive-relative"):
        _absolute_lexical_path(Path(raw), pathmod=pathmod)
    assert cwd_calls == []


@pytest.mark.parametrize("raw", _WINDOWS_DRIVE_RELATIVE_SOURCES)
def test_import_refuses_windows_drive_relative_source_without_copying_bytes(tmp_path, capsys, raw):
    """Bypass C: Windows drive-relative sources must refuse before any copy."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    trusted = _trusted_private_skill(workspace, tmp_path)
    planted = workspace / ".brigade" / "skills" / "planted"
    planted.parent.mkdir(parents=True)
    if hasattr(os, "symlink"):
        planted.symlink_to(trusted, target_is_directory=True)
    source = Path(raw)
    assert skills_cmd._state_root_selector_kind(workspace, raw) == "refuse"

    rc = skills_cmd.import_skill(target=workspace, source=source, json_output=True)

    assert rc == 2
    captured = capsys.readouterr()
    assert "drive-relative" in captured.err
    assert "TRUSTED-PRIVATE-SKILL-MARKER" not in captured.out + captured.err
    _assert_trusted_bytes_not_copied(workspace)
    assert trusted.joinpath("SKILL.md").read_text() == "# TRUSTED-PRIVATE-SKILL-MARKER\n"


def test_import_refuses_windows_rooted_without_drive_source_without_copying_bytes(tmp_path, capsys):
    """Current-drive-rooted ``\\.brigade\\...`` must refuse the same way."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    trusted = _trusted_private_skill(workspace, tmp_path)
    source = Path(_WINDOWS_ROOTED_WITHOUT_DRIVE)
    assert skills_cmd._state_root_selector_kind(workspace, _WINDOWS_ROOTED_WITHOUT_DRIVE) == "refuse"

    rc = skills_cmd.import_skill(target=workspace, source=source, json_output=True)

    assert rc == 2
    captured = capsys.readouterr()
    assert "drive-relative" in captured.err
    assert "TRUSTED-PRIVATE-SKILL-MARKER" not in captured.out + captured.err
    _assert_trusted_bytes_not_copied(workspace)
    assert trusted.joinpath("SKILL.md").read_text() == "# TRUSTED-PRIVATE-SKILL-MARKER\n"
