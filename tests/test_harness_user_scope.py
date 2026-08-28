"""Issue #438 slice 1: Claude/Codex user-scope harness sync/doctor/uninstall."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brigade import harness_profile_cmd, harness_profiles, mcp_cmd, skills_cmd


def _use_home(monkeypatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("HOME", str(home))
    # Harness CLI calls must not spawn the detached update-notify cache writer.
    monkeypatch.setenv("BRIGADE_NO_UPDATE_CHECK", "1")
    return home


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace


def _sync_base(workspace: Path, target: str = "codex") -> list[str]:
    return [
        "harness",
        "sync",
        "--target",
        target,
        "--scope",
        "user",
        "--workspace",
        str(workspace),
    ]


def _add_reviewed_skill(workspace: Path, name: str = "reviewed") -> None:
    source = workspace / "sources" / name
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text(f"# {name.title()}\n\nUse this skill.\n")
    (source / "scripts").mkdir()
    (source / "scripts" / "check.py").write_text("print('ok')\n")
    (source / "skill.json").write_text(
        json.dumps(
            {
                "id": name,
                "title": name.title(),
                "version": "1.0.0",
                "required_tools": [],
                "required_mcp_servers": [],
                "supported_harnesses": ["claude", "codex"],
                "trust_level": "workspace",
                "tests": [],
            }
        )
    )
    assert skills_cmd.import_skill(target=workspace, source=source, json_output=True) == 0


def _uninstall_base(workspace: Path, target: str = "codex") -> list[str]:
    return [
        "harness",
        "uninstall",
        "--target",
        target,
        "--scope",
        "user",
        "--workspace",
        str(workspace),
    ]


def _file_snapshot(root: Path) -> dict[Path, bytes]:
    return {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def _tree_snapshot(root: Path) -> dict[Path, tuple[str, bytes | None, int]]:
    """Capture profile files, directories, and modes for transaction rollback assertions."""
    records: dict[Path, tuple[str, bytes | None, int]] = {}
    for path in root.rglob("*"):
        mode = path.stat().st_mode & 0o777
        if path.is_dir():
            records[path.relative_to(root)] = ("directory", None, mode)
        else:
            records[path.relative_to(root)] = ("file", path.read_bytes(), mode)
    return records


def test_slice1_targets_resolve_only_claude_and_codex(tmp_path):
    home, workspace = tmp_path / "home", tmp_path / "workspace"
    workspace.mkdir()
    profiles = harness_profiles.resolve_slice1_profiles(harness="all", home=home, workspace=workspace)
    assert tuple(p.harness for p in profiles) == ("claude", "codex")


def test_sync_dry_run_writes_nothing(tmp_path, monkeypatch, capsys):
    from brigade import cli

    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path)
    assert cli.main(_sync_base(workspace) + ["--json"]) == 0
    capsys.readouterr()
    assert not (home / ".codex").exists()


def test_sync_dry_run_reports_pending_until_applied(tmp_path, monkeypatch, capsys):
    """Issue #1170: a conflict-free dry run with planned writes is not "current"."""
    from brigade import cli

    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path)
    base = _sync_base(workspace)

    assert cli.main(base + ["--json"]) == 0
    planned = json.loads(capsys.readouterr().out)["results"][0]
    assert planned["status"] == "pending"
    assert planned["ready"] is True
    assert planned["receipt_state"] == "missing"
    assert any(item["action"] in {"create", "update", "remove"} for item in planned["items"])
    assert not (home / ".codex").exists()

    assert cli.main(base + ["--write", "--json"]) == 0
    capsys.readouterr()

    assert cli.main(base + ["--json"]) == 0
    applied = json.loads(capsys.readouterr().out)["results"][0]
    assert applied["status"] == "current"
    assert applied["receipt_state"] == "present"
    assert all(item["action"] not in {"create", "update", "remove"} for item in applied["items"])


def test_uninstall_dry_run_reports_pending_before_apply(tmp_path, monkeypatch, capsys):
    """Issue #1170 dry-run parity for uninstall: planned removals are not "current"."""
    from brigade import cli

    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path)
    assert cli.main(_sync_base(workspace) + ["--write", "--json"]) == 0
    capsys.readouterr()
    before = {path: path.read_bytes() for path in sorted((home / ".codex").rglob("*")) if path.is_file()}

    assert cli.main(_uninstall_base(workspace) + ["--json"]) == 0
    planned = json.loads(capsys.readouterr().out)["results"][0]
    assert planned["status"] == "pending"
    assert planned["ready"] is True
    after = {path: path.read_bytes() for path in sorted((home / ".codex").rglob("*")) if path.is_file()}
    assert after == before

    assert cli.main(_uninstall_base(workspace) + ["--write", "--json"]) == 0
    applied = json.loads(capsys.readouterr().out)["results"][0]
    assert applied["status"] == "updated"


def test_sync_write_then_resync_is_idempotent(tmp_path, monkeypatch, capsys):
    from brigade import cli

    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path)
    base = _sync_base(workspace)

    assert cli.main(base + ["--write", "--json"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["results"][0]["status"] == "updated"
    agents = home / ".codex" / "AGENTS.md"
    assert agents.is_file()
    first_text = agents.read_text()

    assert cli.main(base + ["--write", "--json"]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["results"][0]["status"] == "current"
    assert second["results"][0]["files_written"] == []
    assert agents.read_text() == first_text


def test_sync_state_write_failure_restores_every_profile_surface(tmp_path, monkeypatch, capsys):
    """A failed ownership-state mutation cannot leave a partially synced profile."""
    from brigade import cli

    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path)
    _add_reviewed_skill(workspace)
    before = _tree_snapshot(home)

    original_write_json = harness_profile_cmd.localio.write_json

    def fail_state_write(path: Path, payload: dict) -> None:
        if path.name == "install-state.json":
            raise OSError("injected profile state failure")
        original_write_json(path, payload)

    monkeypatch.setattr(harness_profile_cmd.localio, "write_json", fail_state_write)

    with pytest.raises(OSError, match="injected profile state failure"):
        cli.main(_sync_base(workspace) + ["--write", "--json"])

    assert _tree_snapshot(home) == before


def test_uninstall_state_mutation_failure_restores_every_profile_surface(tmp_path, monkeypatch, capsys):
    """A failed ownership-state removal cannot leave a partially uninstalled profile."""
    from brigade import cli

    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path)
    _add_reviewed_skill(workspace)
    assert cli.main(_sync_base(workspace) + ["--write", "--json"]) == 0
    capsys.readouterr()
    state_path = home / ".codex" / "brigade" / "install-state.json"
    before = _tree_snapshot(home)
    original_apply = harness_profile_cmd.projection._apply_mutation

    def fail_state_remove(spec):
        if spec.destination == state_path:
            raise OSError("injected profile state removal failure")
        original_apply(spec)

    monkeypatch.setattr(harness_profile_cmd.projection, "_apply_mutation", fail_state_remove)

    with pytest.raises(OSError, match="injected profile state removal failure"):
        cli.main(_uninstall_base(workspace) + ["--write", "--json"])

    assert _tree_snapshot(home) == before


def test_sync_preserves_instruction_symlink_swapped_after_preflight(tmp_path, monkeypatch, capsys):
    """The commit-time kernel check must not replace a symlink introduced after planning."""
    from brigade import cli

    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path)
    agents = home / ".codex" / "AGENTS.md"
    outside = tmp_path / "outside"
    outside.write_text("user content\n")
    original = harness_profile_cmd._profile_mutation

    def swap_instruction(path: Path, desired: bytes | None, **kwargs):
        mutation = original(path, desired, **kwargs)
        if path == agents and mutation is not None:
            agents.parent.mkdir(parents=True, exist_ok=True)
            agents.symlink_to(outside)
        return mutation

    monkeypatch.setattr(harness_profile_cmd, "_profile_mutation", swap_instruction)

    assert cli.main(_sync_base(workspace) + ["--write", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)["results"][0]
    assert payload["status"] == "conflict"
    assert agents.is_symlink()
    assert agents.resolve() == outside
    assert outside.read_text() == "user content\n"
    assert not (home / ".codex" / "brigade" / "install-state.json").exists()


def test_second_identical_sync_does_not_rewrite_state_or_receipt(tmp_path, monkeypatch, capsys):
    from brigade import cli

    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path)
    base = _sync_base(workspace)

    assert cli.main(base + ["--write", "--json"]) == 0
    capsys.readouterr()
    state = home / ".codex" / "brigade" / "install-state.json"
    receipt = home / ".codex" / "brigade" / "profile-receipt.json"
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (state, receipt)}

    assert cli.main(base + ["--write", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["results"][0]["files_written"] == []
    assert payload["results"][0]["receipt_state"] == "current"
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (state, receipt)} == before


def test_hand_authored_agents_section_survives_sync(tmp_path, monkeypatch, capsys):
    from brigade import cli

    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path)
    agents = home / ".codex" / "AGENTS.md"
    agents.parent.mkdir(parents=True)
    agents.write_text("# My notes\nKeep this paragraph.\n")

    assert cli.main(_sync_base(workspace) + ["--write", "--json"]) == 0
    capsys.readouterr()
    text = agents.read_text()
    assert "# My notes" in text
    assert "Keep this paragraph." in text
    assert "BEGIN BRIGADE INTEGRATION" in text
    assert harness_profiles.managed_instruction_text().strip() in text


def test_claude_hand_authored_section_survives_sync(tmp_path, monkeypatch, capsys):
    from brigade import cli

    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path)
    claude_md = home / ".claude" / "CLAUDE.md"
    claude_md.parent.mkdir(parents=True)
    claude_md.write_text("## Personal routing\nDo not remove me.\n")

    assert cli.main(_sync_base(workspace, "claude") + ["--write", "--json"]) == 0
    capsys.readouterr()
    text = claude_md.read_text()
    assert "Personal routing" in text
    assert "BEGIN BRIGADE INTEGRATION" in text


def test_foreign_managed_block_reports_conflict(tmp_path, monkeypatch, capsys):
    from brigade import cli

    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path)
    agents = home / ".codex" / "AGENTS.md"
    agents.parent.mkdir(parents=True)
    agents.write_text(f"{harness_profiles.INSTRUCTION_START}\nuser-owned body\n{harness_profiles.INSTRUCTION_END}\n")

    assert cli.main(_sync_base(workspace) + ["--write", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    row = payload["results"][0]
    assert row["status"] == "conflict"
    assert "user-owned body" in agents.read_text()


@pytest.mark.parametrize(
    "text",
    [
        f"{harness_profiles.INSTRUCTION_END}\nbody\n{harness_profiles.INSTRUCTION_START}\n",
        f"{harness_profiles.INSTRUCTION_START}body\n{harness_profiles.INSTRUCTION_END}\n",
        f"{harness_profiles.INSTRUCTION_START}\nbody{harness_profiles.INSTRUCTION_END}\n",
    ],
)
def test_split_components_returns_none_for_malformed_marker_framing(text):
    assert harness_profile_cmd._split_components(text) is None


def test_uninstall_preserves_state_and_receipt_for_end_only_instruction_marker(tmp_path, monkeypatch, capsys):
    from brigade import cli

    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path)
    assert cli.main(_sync_base(workspace) + ["--write", "--json"]) == 0
    capsys.readouterr()
    agents = home / ".codex" / "AGENTS.md"
    agents.write_text(f"{harness_profiles.INSTRUCTION_END}\n")
    before = _file_snapshot(home)

    assert cli.main(_uninstall_base(workspace) + ["--write", "--json"]) == 1
    result = json.loads(capsys.readouterr().out)["results"][0]
    assert result["status"] == "conflict"
    assert any(item["surface"] == "instruction" for item in result["conflicts"])
    assert _file_snapshot(home) == before


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_sync_rejects_symlinked_profile_root(tmp_path, monkeypatch, capsys, target):
    from brigade import cli

    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / f".{target}").symlink_to(outside, target_is_directory=True)

    assert cli.main(_sync_base(workspace, target) + ["--write", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["results"][0]["status"] == "conflict"
    assert _file_snapshot(outside) == {}


@pytest.mark.parametrize(
    "surface", ["AGENTS.md", "brigade/install-state.json", "brigade/profile-receipt.json", "config.toml"]
)
def test_sync_rejects_symlinked_codex_non_skill_surface(tmp_path, monkeypatch, capsys, surface):
    from brigade import cli

    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    path = home / ".codex" / surface
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(outside / "escaped")

    assert cli.main(_sync_base(workspace) + ["--write", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["results"][0]["status"] == "conflict"
    assert _file_snapshot(outside) == {}


@pytest.mark.parametrize("receipt_change", ["missing", "malformed", "mismatched", "drifted"])
def test_uninstall_rejects_invalid_profile_receipt_before_removing_artifacts(
    tmp_path, monkeypatch, capsys, receipt_change
):
    from brigade import cli

    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path)
    assert cli.main(_sync_base(workspace) + ["--write", "--json"]) == 0
    capsys.readouterr()
    receipt = home / ".codex" / "brigade" / "profile-receipt.json"
    if receipt_change == "missing":
        receipt.unlink()
    elif receipt_change == "malformed":
        receipt.write_text("not json")
    else:
        payload = json.loads(receipt.read_text())
        if receipt_change == "mismatched":
            payload["harness"] = "claude"
        else:
            payload["ownership_fingerprints"]["instruction_fingerprint"] = "drifted"
        receipt.write_text(json.dumps(payload))
    before = _file_snapshot(home)

    assert cli.main(_uninstall_base(workspace) + ["--write", "--json"]) == 1
    result = json.loads(capsys.readouterr().out)["results"][0]
    assert result["status"] == "conflict"
    assert any(item["surface"] == "profile-receipt" for item in result["conflicts"])
    assert _file_snapshot(home) == before


@pytest.mark.parametrize("operation", ["sync", "uninstall"])
def test_target_all_preflights_both_profiles_before_any_write(tmp_path, monkeypatch, capsys, operation):
    from brigade import cli

    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path)
    if operation == "uninstall":
        assert cli.main(_sync_base(workspace, "all") + ["--write", "--json"]) == 0
        capsys.readouterr()
    agents = home / ".codex" / "AGENTS.md"
    agents.parent.mkdir(parents=True, exist_ok=True)
    agents.write_text(f"{harness_profiles.INSTRUCTION_START}\nforeign\n{harness_profiles.INSTRUCTION_END}\n")
    before = _file_snapshot(home)
    command = _sync_base(workspace, "all") if operation == "sync" else _uninstall_base(workspace, "all")

    assert cli.main(command + ["--write", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert [result["harness"] for result in payload["results"]] == list(harness_profiles.USER_SCOPE_HARNESS_IDS)
    codex = next(result for result in payload["results"] if result["harness"] == "codex")
    assert codex["status"] == "conflict"
    assert _file_snapshot(home) == before


def test_identical_unowned_instruction_block_requires_adopt_without_rewrite(tmp_path, monkeypatch, capsys):
    from brigade import cli

    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path)
    agents = home / ".codex" / "AGENTS.md"
    agents.parent.mkdir(parents=True)
    agents.write_text(harness_profile_cmd._block(harness_profiles.managed_instruction_text()))
    before = agents.read_bytes()

    assert cli.main(_sync_base(workspace) + ["--write", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["results"][0]["status"] == "conflict"
    assert agents.read_bytes() == before

    assert cli.main(_sync_base(workspace) + ["--adopt", "--write", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["results"][0]["files_written"]
    assert agents.read_bytes() == before


def test_sync_reconciles_removed_owned_skill_files_and_records_provenance(tmp_path, monkeypatch, capsys):
    from brigade import cli

    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path)
    _add_reviewed_skill(workspace)
    capsys.readouterr()

    assert cli.main(_sync_base(workspace) + ["--write", "--json"]) == 0
    capsys.readouterr()
    installed = home / ".codex" / "skills" / "reviewed"
    assert (installed / "SKILL.md").is_file()
    assert (installed / "scripts" / "check.py").is_file()
    registry = workspace / ".brigade" / "skills" / "registry" / "reviewed"
    for path in sorted(registry.rglob("*"), reverse=True):
        if path.is_file():
            path.unlink()
        elif path.is_dir():
            path.rmdir()

    assert cli.main(_sync_base(workspace) + ["--write", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)["results"][0]
    assert not installed.exists()
    assert any(item["surface"] == "skill" and item["action"] == "remove" for item in payload["items"])
    state = json.loads((home / ".codex" / "brigade" / "install-state.json").read_text())
    assert "reviewed" not in state["skills"]


def test_doctor_reports_owned_skill_missing_changed_and_removed_registry_without_writes(tmp_path, monkeypatch, capsys):
    from brigade import cli

    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path)
    _add_reviewed_skill(workspace)
    capsys.readouterr()
    assert cli.main(_sync_base(workspace) + ["--write", "--json"]) == 0
    capsys.readouterr()
    profile = home / ".codex" / "brigade"
    state, receipt = profile / "install-state.json", profile / "profile-receipt.json"
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (state, receipt)}
    installed = home / ".codex" / "skills" / "reviewed"
    (installed / "SKILL.md").write_text("edited\n")

    assert (
        cli.main(["harness", "doctor", "--target", "codex", "--scope", "user", "--workspace", str(workspace), "--json"])
        == 1
    )
    payload = json.loads(capsys.readouterr().out)["results"][0]
    assert any(item["surface"] == "skill" and item["status"] == "changed" for item in payload["items"])
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (state, receipt)} == before


def test_uninstall_preflights_skill_conflict_before_any_mcp_write(tmp_path, monkeypatch, capsys):
    from brigade import cli

    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace_with_stdio_server(tmp_path, monkeypatch, capsys, ["codex-user"])
    _add_reviewed_skill(workspace)
    capsys.readouterr()
    assert cli.main(_sync_base(workspace) + ["--allow-global-stdio", "--write", "--json"]) == 0
    capsys.readouterr()
    installed = home / ".codex" / "skills" / "reviewed" / "SKILL.md"
    installed.write_text("edited\n")
    config = home / ".codex" / "config.toml"
    before = config.read_bytes()

    assert cli.main(_uninstall_base(workspace) + ["--write", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)["results"][0]
    assert payload["status"] == "conflict"
    assert config.read_bytes() == before


def test_skill_root_symlink_is_conflict_and_does_not_escape_home(tmp_path, monkeypatch, capsys):
    from brigade import cli

    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path)
    _add_reviewed_skill(workspace)
    capsys.readouterr()
    outside = tmp_path / "outside"
    outside.mkdir()
    codex = home / ".codex"
    codex.mkdir()
    (codex / "skills").symlink_to(outside, target_is_directory=True)

    assert cli.main(_sync_base(workspace) + ["--write", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)["results"][0]
    assert payload["status"] == "conflict"
    assert not any(outside.rglob("*"))


def test_doctor_and_check_report_stale_block_with_fix_command(tmp_path, monkeypatch, capsys):
    from brigade import cli

    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path)
    assert cli.main(_sync_base(workspace) + ["--write", "--json"]) == 0
    capsys.readouterr()

    agents = home / ".codex" / "AGENTS.md"
    old_body = "old managed body\n"
    agents.write_text(harness_profile_cmd._block(old_body))
    state_path = home / ".codex" / "brigade" / "install-state.json"
    state = json.loads(state_path.read_text())
    state["instructions"]["digest"] = harness_profile_cmd.digest_text(old_body)
    state_path.write_text(json.dumps(state))

    assert (
        cli.main(["harness", "doctor", "--target", "codex", "--scope", "user", "--workspace", str(workspace), "--json"])
        == 1
    )
    doctor = json.loads(capsys.readouterr().out)["results"][0]
    instruction = next(item for item in doctor["items"] if item["surface"] == "instruction")
    assert instruction["status"] == "stale"
    assert "brigade harness sync --target codex --scope user --write" in instruction["detail"]

    assert cli.main(_sync_base(workspace) + ["--check", "--json"]) == 1
    checked = json.loads(capsys.readouterr().out)["results"][0]
    assert checked["ready"] is False
    check_instruction = next(item for item in checked["items"] if item["surface"] == "instruction")
    assert check_instruction["status"] == "stale"


def test_force_overwrites_locally_modified_instruction_block(tmp_path, monkeypatch, capsys):
    from brigade import cli

    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path)
    assert cli.main(_sync_base(workspace) + ["--write", "--json"]) == 0
    capsys.readouterr()
    agents = home / ".codex" / "AGENTS.md"
    text = agents.read_text()
    agents.write_text(text.replace("brigade run", "brigade dispatch", 1))

    assert cli.main(_sync_base(workspace) + ["--write", "--json"]) == 1
    assert "brigade dispatch" in agents.read_text()
    capsys.readouterr()

    assert cli.main(_sync_base(workspace) + ["--force", "--write", "--json"]) == 0
    capsys.readouterr()
    restored = agents.read_text()
    assert "brigade run" in restored
    assert "brigade dispatch" not in restored
    assert "BEGIN BRIGADE INTEGRATION" in restored
    assert "hash:" in restored


def test_doctor_reports_drift_without_mutating(tmp_path, monkeypatch, capsys):
    from brigade import cli

    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path)
    base = _sync_base(workspace)
    assert cli.main(base + ["--write", "--json"]) == 0
    capsys.readouterr()

    agents = home / ".codex" / "AGENTS.md"
    agents.write_text(agents.read_text().replace("brigade run", "brigade dispatch"))

    assert (
        cli.main(["harness", "doctor", "--target", "codex", "--scope", "user", "--workspace", str(workspace), "--json"])
        == 1
    )
    doctor = json.loads(capsys.readouterr().out)
    row = doctor["results"][0]
    assert row["ready"] is False
    assert row["instruction_ready"] is False


def _doctor_json(workspace: Path) -> list[str]:
    return ["harness", "doctor", "--target", "codex", "--scope", "user", "--workspace", str(workspace), "--json"]


def _assert_check_and_doctor_report_drift(workspace: Path, capsys, *, surface: str) -> tuple[dict, dict]:
    from brigade import cli

    assert cli.main(_sync_base(workspace) + ["--check", "--json"]) == 1
    checked = json.loads(capsys.readouterr().out)["results"][0]
    assert checked["status"] != "current"
    assert checked["ready"] is False
    assert any(
        item["surface"] == surface and item["action"] in {"create", "update", "remove"} for item in checked["items"]
    )

    assert cli.main(_doctor_json(workspace)) == 1
    doctor = json.loads(capsys.readouterr().out)["results"][0]
    assert doctor["status"] != "current"
    assert doctor["ready"] is False
    assert any(
        item["surface"] == surface and item["action"] in {"create", "update", "remove"} for item in doctor["items"]
    )
    return checked, doctor


def test_doctor_and_check_report_state_only_package_version_drift(tmp_path, monkeypatch, capsys):
    """Issue #1218: install-state.json package_version drift is not current."""
    from brigade import cli

    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path)
    assert cli.main(_sync_base(workspace) + ["--write", "--json"]) == 0
    capsys.readouterr()

    state_path = home / ".codex" / "brigade" / "install-state.json"
    state = json.loads(state_path.read_text())
    state["package_version"] = "old"
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")

    _assert_check_and_doctor_report_drift(workspace, capsys, surface="ownership-state")


@pytest.mark.parametrize("receipt_change", ["missing", "drifted"])
def test_doctor_and_check_report_receipt_only_drift(tmp_path, monkeypatch, capsys, receipt_change):
    """Issue #1218: missing or fingerprint-drifted sync receipt is not current."""
    from brigade import cli

    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path)
    assert cli.main(_sync_base(workspace) + ["--write", "--json"]) == 0
    capsys.readouterr()

    receipt = home / ".codex" / "brigade" / "profile-receipt.json"
    if receipt_change == "missing":
        receipt.unlink()
    else:
        payload = json.loads(receipt.read_text())
        payload["ownership_fingerprints"]["instruction_fingerprint"] = "drifted"
        receipt.write_text(json.dumps(payload))

    checked, doctor = _assert_check_and_doctor_report_drift(workspace, capsys, surface="profile-receipt")
    if receipt_change == "missing":
        assert checked["receipt_state"] == "missing"
        assert doctor["receipt_state"] == "missing"


def test_uninstall_removes_only_brigade_owned_block(tmp_path, monkeypatch, capsys):
    from brigade import cli

    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path)
    agents = home / ".codex" / "AGENTS.md"
    agents.parent.mkdir(parents=True)
    agents.write_text("# keep\n")

    assert cli.main(_sync_base(workspace) + ["--write", "--json"]) == 0
    capsys.readouterr()

    assert (
        cli.main(
            [
                "harness",
                "uninstall",
                "--target",
                "codex",
                "--scope",
                "user",
                "--workspace",
                str(workspace),
                "--write",
                "--json",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert agents.read_text() == "# keep\n"
    assert not (home / ".codex" / "brigade" / "install-state.json").exists()


def _workspace_with_stdio_server(tmp_path: Path, monkeypatch, capsys, targets: list[str]) -> Path:
    workspace = _workspace(tmp_path)
    mcp_cmd.init(target=workspace, json_output=True)
    capsys.readouterr()
    mcp_cmd.add(
        target=workspace,
        name="brigade",
        command="brigade",
        args=["memory", "serve-mcp", "--stdio", "--target", "."],
        timeout=60,
        targets=targets,
        json_output=True,
    )
    capsys.readouterr()
    return workspace


@pytest.mark.parametrize(
    ("target", "mcp_target", "config_rel"),
    [
        ("codex", "codex-user", ".codex/config.toml"),
        ("claude", "claude-user", ".claude.json"),
    ],
)
def test_mcp_stdio_requires_allow_global_stdio_gate(tmp_path, monkeypatch, capsys, target, mcp_target, config_rel):
    from brigade import cli

    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace_with_stdio_server(tmp_path, monkeypatch, capsys, [mcp_target])

    assert cli.main(_sync_base(workspace, target) + ["--write", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["results"][0]["status"] == "conflict"
    assert not (home / config_rel).exists()

    assert cli.main(_sync_base(workspace, target) + ["--allow-global-stdio", "--write", "--json"]) == 0
    capsys.readouterr()
    assert (home / config_rel).is_file()


def test_second_mcp_enabled_sync_preserves_ownership_without_writes(tmp_path, monkeypatch, capsys):
    from brigade import cli

    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace_with_stdio_server(tmp_path, monkeypatch, capsys, ["codex-user"])
    base = _sync_base(workspace) + ["--allow-global-stdio", "--write", "--json"]

    assert cli.main(base) == 0
    capsys.readouterr()
    profile = home / ".codex" / "brigade"
    state = profile / "install-state.json"
    receipt = profile / "profile-receipt.json"
    config = home / ".codex" / "config.toml"
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (state, receipt, config)}
    assert json.loads(state.read_text())["mcp"]["brigade"]["managed"] is True

    assert cli.main(base) == 0
    payload = json.loads(capsys.readouterr().out)["results"][0]
    assert payload["files_written"] == []
    assert payload["receipt_state"] == "current"
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (state, receipt, config)} == before
    assert json.loads(state.read_text())["mcp"]["brigade"]["managed"] is True


def test_mcp_uninstall_removes_only_owned_server(tmp_path, monkeypatch, capsys):
    from brigade import cli

    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace_with_stdio_server(tmp_path, monkeypatch, capsys, ["codex-user"])
    codex_cfg = home / ".codex" / "config.toml"
    codex_cfg.parent.mkdir(parents=True)
    codex_cfg.write_text('[mcp_servers.foreign]\ncommand = "keep"\n')

    assert cli.main(_sync_base(workspace) + ["--allow-global-stdio", "--write", "--json"]) == 0
    capsys.readouterr()
    assert "brigade" in codex_cfg.read_text()
    assert "foreign" in codex_cfg.read_text()

    assert (
        cli.main(
            [
                "harness",
                "uninstall",
                "--target",
                "codex",
                "--scope",
                "user",
                "--workspace",
                str(workspace),
                "--write",
                "--json",
            ]
        )
        == 0
    )
    text = codex_cfg.read_text()
    assert "brigade" not in text
    assert "foreign" in text


def test_matching_unowned_mcp_server_requires_adopt_and_uninstall_preserves_it(tmp_path, monkeypatch, capsys):
    from brigade import cli

    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace_with_stdio_server(tmp_path, monkeypatch, capsys, ["codex-user"])
    codex_cfg = home / ".codex" / "config.toml"
    codex_cfg.parent.mkdir(parents=True)
    codex_cfg.write_text(
        '[mcp_servers.brigade]\ncommand = "brigade"\nargs = ["memory", "serve-mcp", "--stdio", "--target", "."]\ntimeout = 60\n'
    )
    before = codex_cfg.read_bytes()

    assert cli.main(_sync_base(workspace) + ["--allow-global-stdio", "--write", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["results"][0]["status"] == "conflict"
    assert codex_cfg.read_bytes() == before
    assert not (home / ".codex" / "brigade" / "install-state.json").exists()

    assert cli.main(_sync_base(workspace) + ["--allow-global-stdio", "--adopt", "--write", "--json"]) == 0
    capsys.readouterr()
    assert (
        cli.main(
            [
                "harness",
                "uninstall",
                "--target",
                "codex",
                "--scope",
                "user",
                "--workspace",
                str(workspace),
                "--write",
                "--json",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert codex_cfg.read_bytes() == before


@pytest.mark.parametrize(("target", "mcp_target"), [("codex", "codex-user"), ("claude", "claude-user")])
def test_doctor_verify_mcp_is_read_only_and_reports_projection_drift(tmp_path, monkeypatch, capsys, target, mcp_target):
    from brigade import cli

    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace_with_stdio_server(tmp_path, monkeypatch, capsys, [mcp_target])
    assert cli.main(_sync_base(workspace, target) + ["--allow-global-stdio", "--write", "--json"]) == 0
    capsys.readouterr()
    profile = home / (".codex" if target == "codex" else ".claude") / "brigade"
    state = profile / "install-state.json"
    receipt = profile / "profile-receipt.json"
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (state, receipt)}

    assert (
        cli.main(
            [
                "harness",
                "doctor",
                "--target",
                target,
                "--scope",
                "user",
                "--workspace",
                str(workspace),
                "--verify-mcp",
                "--json",
            ]
        )
        == 0
    )
    ready = json.loads(capsys.readouterr().out)["results"][0]
    assert ready["mcp"]["status"] == "ready"
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (state, receipt)} == before

    config = home / (".codex/config.toml" if target == "codex" else ".claude.json")
    config.write_text(config.read_text().replace("memory", "edited", 1))
    assert (
        cli.main(
            [
                "harness",
                "doctor",
                "--target",
                target,
                "--scope",
                "user",
                "--workspace",
                str(workspace),
                "--verify-mcp",
                "--json",
            ]
        )
        == 1
    )
    drift = json.loads(capsys.readouterr().out)["results"][0]
    assert drift["mcp"]["status"] == "conflict"
    assert drift["mcp"]["items"][0]["status"] == "edited"
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (state, receipt)} == before


def test_sync_reports_a_secret_free_profile_receipt(tmp_path, monkeypatch, capsys):
    from brigade import cli

    _use_home(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path)
    assert cli.main(_sync_base(workspace) + ["--write", "--json"]) == 0
    row = json.loads(capsys.readouterr().out)["results"][0]
    receipt = Path(row["receipt_path"])
    assert row["receipt_state"] == "applied"
    payload = json.loads(receipt.read_text())
    assert payload["harness"] == "codex"
    assert payload["operation"] == "sync"
    assert payload["ownership_fingerprints"]
    assert "secret" not in receipt.read_text().lower()


def test_harness_sync_cli_dispatch_contract(tmp_path, monkeypatch):
    from brigade import cli

    calls: dict[str, dict] = {}

    def recorder(name):
        def _f(**kwargs):
            calls[name] = kwargs
            return 0

        return _f

    monkeypatch.setattr(harness_profile_cmd, "sync", recorder("sync"), raising=False)
    monkeypatch.setattr(harness_profile_cmd, "uninstall", recorder("uninstall"), raising=False)
    monkeypatch.setattr(harness_profile_cmd, "doctor", recorder("doctor"), raising=False)
    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)

    for target in harness_profiles.USER_SCOPE_SLICE1_TARGETS:
        cli.main(["harness", "sync", "--target", target, "--scope", "user", "--json"])
        assert calls["sync"]["harness"] == target
        assert calls["sync"]["write"] is False

    ws = tmp_path / "ws"
    ws.mkdir()
    cli.main(
        [
            "harness",
            "sync",
            "--target",
            "all",
            "--scope",
            "user",
            "--workspace",
            str(ws),
            "--write",
            "--allow-global-stdio",
            "--adopt",
            "--json",
        ]
    )
    assert calls["sync"]["harness"] == "all"
    assert calls["sync"]["workspace"] == ws
    assert calls["sync"]["write"] is True
    assert calls["sync"]["allow_global_stdio"] is True
    assert calls["sync"]["adopt"] is True

    cli.main(["harness", "uninstall", "--target", "codex", "--scope", "user", "--json"])
    assert calls["uninstall"]["harness"] == "codex"

    cli.main(["harness", "doctor", "--target", "claude", "--scope", "user", "--verify-mcp", "--json"])
    assert calls["doctor"]["harness"] == "claude"
    assert calls["doctor"]["verify_mcp"] is True
