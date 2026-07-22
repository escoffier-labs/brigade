import json
from pathlib import Path

import pytest

from brigade import __version__ as BRIGADE_VERSION
from brigade import harness_profile_cmd, harness_profiles, skills_cmd


def test_user_profile_paths_cover_all_eight_harnesses(tmp_path):
    home, workspace = tmp_path / "home", tmp_path / "workspace"
    profiles = harness_profiles.resolve_profiles(harness="all", home=home, workspace=workspace, kimi_native_mcp=True)
    by_id = {p.harness: p for p in profiles}
    assert (
        tuple(by_id)
        == harness_profiles.HARNESS_IDS
        == (
            "claude",
            "codex",
            "openclaw",
            "kimi",
            "grok",
            "cursor",
            "opencode",
            "pi",
        )
    )
    assert by_id["claude"].instruction_path == home / ".claude" / "CLAUDE.md"
    assert by_id["claude"].skills_root == home / ".claude" / "skills"
    assert by_id["codex"].instruction_path == home / ".codex" / "AGENTS.md"
    assert by_id["openclaw"].instruction_path == workspace / "AGENTS.md"
    assert by_id["kimi"].instruction_path == home / ".kimi" / "AGENTS.md"
    assert by_id["grok"].instruction_path == home / ".grok" / "AGENTS.md"
    assert by_id["cursor"].instruction_path is None
    assert by_id["opencode"].instruction_path == home / ".config" / "opencode" / "AGENTS.md"
    assert by_id["pi"].instruction_path == home / ".pi" / "agent" / "AGENTS.md"
    assert all(p.state_path == p.user_root / "brigade" / "install-state.json" for p in profiles)
    assert by_id["kimi"].mcp_harness == "kimi-user"
    assert by_id["pi"].mcp_harness is None


def test_kimi_profile_root_uses_single_probe_value(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        harness_profiles,
        "probe_kimi_native_mcp",
        lambda: (calls.append(1), True)[1],
    )
    home, workspace = tmp_path / "home", tmp_path / "workspace"
    resolved = harness_profiles.resolve_profiles(harness="all", home=home, workspace=workspace)
    assert len(calls) == 1
    assert {p.harness: p.user_root for p in resolved}["kimi"] == home / ".kimi"

    legacy = harness_profiles.resolve_profiles(harness="kimi", home=home, workspace=workspace, kimi_native_mcp=False)[0]
    assert legacy.user_root == home / ".kimi-code"
    assert legacy.capabilities == {"kimi_native_mcp": False}
    assert len(calls) == 1  # explicit value must not re-probe


def test_managed_instruction_block_appends_updates_and_detects_invalid_markers(tmp_path):
    path = tmp_path / "AGENTS.md"
    start, end = harness_profiles.INSTRUCTION_START, harness_profiles.INSTRUCTION_END
    desired = harness_profiles.managed_instruction_text()

    # empty file -> missing/create, rendered carries exactly one bounded block
    path.write_text("")
    plan = harness_profile_cmd.plan_instruction(path=path, desired=desired, state={"instructions": {}})
    assert (plan.status, plan.action) == ("missing", "create")
    assert plan.rendered is not None
    assert plan.rendered.count(start) == 1 and plan.rendered.count(end) == 1

    # non-empty foreign file with no markers -> missing/create with a blank line before the block
    path.write_text("# my notes\n")
    plan = harness_profile_cmd.plan_instruction(path=path, desired=desired, state={"instructions": {}})
    assert (plan.status, plan.action) == ("missing", "create")
    assert plan.rendered.startswith("# my notes\n\n")

    # write the owned block; live == desired -> current/none
    path.write_text(plan.rendered)
    plan = harness_profile_cmd.plan_instruction(
        path=path, desired=desired, state={"instructions": {"digest": plan.desired_digest}}
    )
    assert (plan.status, plan.action) == ("current", "none")

    # a prior owned block whose live digest matches the stored digest but desired changed -> stale/update
    older = desired.replace("brigade run", "brigade dispatch")  # previous managed text
    path.write_text(f"{start}\n{older}\n{end}\n")
    stored = harness_profile_cmd.digest_text(older)
    plan = harness_profile_cmd.plan_instruction(path=path, desired=desired, state={"instructions": {"digest": stored}})
    assert (plan.status, plan.action) == ("stale", "update")

    # duplicate markers -> conflict/preserve
    path.write_text(f"{start}\nbody\n{end}\n{start}\nbody\n{end}\n")
    plan = harness_profile_cmd.plan_instruction(path=path, desired=desired, state={"instructions": {}})
    assert (plan.status, plan.action) == ("conflict", "preserve")

    # nested markers -> conflict/preserve
    path.write_text(f"{start}\n{start}\nbody\n{end}\n{end}\n")
    plan = harness_profile_cmd.plan_instruction(path=path, desired=desired, state={"instructions": {}})
    assert (plan.status, plan.action) == ("conflict", "preserve")

    # reversed markers -> conflict/preserve
    path.write_text(f"{end}\nbody\n{start}\n")
    plan = harness_profile_cmd.plan_instruction(path=path, desired=desired, state={"instructions": {}})
    assert (plan.status, plan.action) == ("conflict", "preserve")

    # truncated (start without end) -> conflict/preserve
    path.write_text(f"{start}\nbody\n")
    plan = harness_profile_cmd.plan_instruction(path=path, desired=desired, state={"instructions": {}})
    assert (plan.status, plan.action) == ("conflict", "preserve")


def test_managed_instruction_uninstall_preserves_surrounding_bytes(tmp_path):
    path = tmp_path / "AGENTS.md"
    start = harness_profiles.INSTRUCTION_START
    desired = harness_profiles.managed_instruction_text()

    foreign = "# top\nsome notes\n"
    path.write_text(foreign)
    create = harness_profile_cmd.plan_instruction(path=path, desired=desired, state={"instructions": {}})
    path.write_text(create.rendered)
    owned_digest = create.desired_digest

    removal = harness_profile_cmd.plan_instruction_removal(path=path, state={"instructions": {"digest": owned_digest}})
    assert (removal.status, removal.action) == ("managed", "remove")
    assert removal.rendered is not None
    # the block and exactly the one newline install added are gone; foreign bytes survive
    assert start not in removal.rendered
    assert "some notes" in removal.rendered
    assert removal.rendered == foreign

    # a foreign-edited managed block (text inside the marked body) is preserved, not removed
    live = path.read_text()
    edited_block = live.replace("using-skillet", "user-edit-inside-block")
    path.write_text(edited_block)
    edited = harness_profile_cmd.plan_instruction_removal(path=path, state={"instructions": {"digest": owned_digest}})
    assert (edited.status, edited.action) == ("conflict", "preserve")


def test_unmanaged_prefix_suffix_changes_do_not_block_update_or_removal(tmp_path):
    path = tmp_path / "AGENTS.md"
    start, end = harness_profiles.INSTRUCTION_START, harness_profiles.INSTRUCTION_END
    desired = harness_profiles.managed_instruction_text()

    # install an owned block into a file that already has foreign prefix bytes
    foreign = "# top\nsome notes\n"
    path.write_text(foreign)
    create = harness_profile_cmd.plan_instruction(path=path, desired=desired, state={"instructions": {}})
    assert (create.status, create.action) == ("missing", "create")
    path.write_text(create.rendered)
    owned_digest = create.desired_digest

    # user later edits only unmanaged bytes: rewrite the prefix and append a suffix
    components = harness_profile_cmd._split_components(path.read_text(), start, end)
    assert components is not None
    installed_before, installed_body, installed_after = components
    assert installed_body == desired
    changed_prefix = "# top\nNEW prefix\n\n"  # keeps the blank separator line install added
    changed_suffix = "# trailing suffix\n"
    edited_live = changed_prefix + f"{start}\n{installed_body}\n{end}\n" + changed_suffix
    path.write_text(edited_live)

    # a desired change is a safe update: managed block swapped, unmanaged bytes preserved
    desired2 = desired.replace("using-skillet", "using-skillet-v2")
    update = harness_profile_cmd.plan_instruction(
        path=path, desired=desired2, state={"instructions": {"digest": owned_digest}}
    )
    assert (update.status, update.action) == ("stale", "update")
    assert update.rendered == changed_prefix + f"{start}\n{desired2}\n{end}\n" + changed_suffix

    # removal drops the block and exactly the separator newline install added
    removal = harness_profile_cmd.plan_instruction_removal(path=path, state={"instructions": {"digest": owned_digest}})
    assert (removal.status, removal.action) == ("managed", "remove")
    assert removal.rendered == "# top\nNEW prefix\n" + changed_suffix


def test_foreign_authored_block_is_conflict_with_recovery_command(tmp_path):
    path = tmp_path / "AGENTS.md"
    desired = harness_profiles.managed_instruction_text()
    path.write_text(
        f"{harness_profiles.INSTRUCTION_START}\nsomeone else wrote this\n{harness_profiles.INSTRUCTION_END}\n"
    )
    plan = harness_profile_cmd.plan_instruction(path=path, desired=desired, state={"instructions": {}})
    assert (plan.status, plan.action) == ("conflict", "preserve")
    assert "brigade harness install" in plan.detail
    assert "--adopt" in plan.detail

    # --adopt reclassifies a well-formed foreign block as stale/update
    adopted = harness_profile_cmd.plan_instruction(path=path, desired=desired, state={"instructions": {}}, adopt=True)
    assert (adopted.status, adopted.action) == ("stale", "update")


def _reviewed_package(*, skill_id="reviewed", files=None):
    return skills_cmd.UserProfileSkillPackage(
        skill_id=skill_id,
        source_identity=f"registry://skills/{skill_id}",
        source_fingerprint="s",
        metadata_fingerprint="m",
        files={} if files is None else files,
    )


def test_user_profile_skills_create_update_conflict_and_safe_uninstall(tmp_path):
    root = tmp_path / "skills"
    package = _reviewed_package(files={"SKILL.md": b"# a\n", "scripts/check.py": b"print(1)\n"})
    state = harness_profile_cmd.empty_profile_state(workspace=tmp_path, harness="codex")

    plans = harness_profile_cmd.plan_skills(skills_root=root, packages=(package,), state=state)
    by_rel = {(p.skill_id, p.relative_path): p for p in plans}
    assert by_rel[("reviewed", "SKILL.md")].status == "missing"
    assert by_rel[("reviewed", "SKILL.md")].action == "create"
    assert by_rel[("reviewed", "scripts/check.py")].status == "missing"
    assert by_rel[("reviewed", "scripts/check.py")].action == "create"
    for plan in plans:
        assert plan.desired_digest is not None

    new_state, written = harness_profile_cmd.apply_skill_plan(
        skills_root=root,
        packages=(package,),
        plans=plans,
        prior_state=state,
        state_path=tmp_path / "state.json",
    )
    assert sorted(written) == [
        str(root / "reviewed" / "SKILL.md"),
        str(root / "reviewed" / "scripts" / "check.py"),
    ]
    assert (root / "reviewed" / "SKILL.md").read_bytes() == b"# a\n"
    assert (root / "reviewed" / "scripts" / "check.py").read_bytes() == b"print(1)\n"
    assert new_state is not state
    assert new_state["skills"]["reviewed"]["files"] == {
        "SKILL.md": harness_profile_cmd.digest_bytes(b"# a\n"),
        "scripts/check.py": harness_profile_cmd.digest_bytes(b"print(1)\n"),
    }
    assert new_state["skills"]["reviewed"]["source_identity"] == "registry://skills/reviewed"
    assert sorted(new_state["skills"]["reviewed"]["created_directories"]) == [
        "reviewed",
        "reviewed/scripts",
    ]
    # prior_state supplied by caller is not mutated
    assert state["skills"] == {}

    # re-plan -> current/none for both files
    plans = harness_profile_cmd.plan_skills(skills_root=root, packages=(package,), state=new_state)
    assert all(p.status == "current" and p.action == "none" for p in plans)

    # desired content change with live equal to owned digest becomes stale/update
    package_v2 = _reviewed_package(files={"SKILL.md": b"# b\n", "scripts/check.py": b"print(1)\n"})
    plans = harness_profile_cmd.plan_skills(skills_root=root, packages=(package_v2,), state=new_state)
    by_rel = {(p.skill_id, p.relative_path): p for p in plans}
    assert by_rel[("reviewed", "SKILL.md")].status == "stale"
    assert by_rel[("reviewed", "SKILL.md")].action == "update"
    assert by_rel[("reviewed", "scripts/check.py")].status == "current"

    # foreign edit -> conflict/preserve, other file still reconcilable/current
    (root / "reviewed" / "scripts" / "check.py").write_bytes(b"user\n")
    plans = harness_profile_cmd.plan_skills(skills_root=root, packages=(package,), state=new_state)
    edited = next(p for p in plans if p.relative_path == "scripts/check.py")
    assert (edited.status, edited.action) == ("conflict", "preserve")
    top = next(p for p in plans if p.relative_path == "SKILL.md")
    assert (top.status, top.action) == ("current", "none")

    # uninstall removes only digest-matching owned files, preserves the foreign edit,
    # and only removes empty recorded directories
    removals = harness_profile_cmd.plan_skill_removals(skills_root=root, state=new_state)
    removed = harness_profile_cmd.apply_skill_removals(
        skills_root=root,
        plans=removals,
        state=new_state,
        state_path=tmp_path / "state.json",
    )
    assert sorted(removed) == [str(root / "reviewed" / "SKILL.md")]
    assert not (root / "reviewed" / "SKILL.md").exists()
    assert (root / "reviewed" / "scripts" / "check.py").read_bytes() == b"user\n"
    # only empty recorded directories are removed; the foreign-edited file keeps
    # its parent directories alive (rmdir, never recursive unlink)
    assert (root / "reviewed" / "scripts").is_dir()
    assert (root / "reviewed").is_dir()


def test_skill_paths_must_stay_inside_the_profile_skills_root(tmp_path):
    root = tmp_path / "skills"
    escape = _reviewed_package(skill_id="evil", files={"../../escape.txt": b"nope\n"})
    with pytest.raises(ValueError, match="outside the profile skills root"):
        harness_profile_cmd.plan_skills(skills_root=root, packages=(escape,), state={"skills": {}})

    absolute = _reviewed_package(skill_id="evil", files={"/etc/passwd": b"nope\n"})
    with pytest.raises(ValueError, match="outside the profile skills root"):
        harness_profile_cmd.plan_skills(skills_root=root, packages=(absolute,), state={"skills": {}})


def test_skill_uninstall_removes_empty_recorded_directories(tmp_path):
    root = tmp_path / "skills"
    package = _reviewed_package(files={"SKILL.md": b"# a\n", "scripts/check.py": b"print(1)\n"})
    state = harness_profile_cmd.empty_profile_state(workspace=tmp_path, harness="codex")
    plans = harness_profile_cmd.plan_skills(skills_root=root, packages=(package,), state=state)
    new_state, _ = harness_profile_cmd.apply_skill_plan(
        skills_root=root,
        packages=(package,),
        plans=plans,
        prior_state=state,
        state_path=tmp_path / "state.json",
    )
    assert (root / "reviewed" / "scripts" / "check.py").exists()

    removals = harness_profile_cmd.plan_skill_removals(skills_root=root, state=new_state)
    removed = harness_profile_cmd.apply_skill_removals(
        skills_root=root,
        plans=removals,
        state=new_state,
        state_path=tmp_path / "state.json",
    )
    assert sorted(removed) == [
        str(root / "reviewed" / "SKILL.md"),
        str(root / "reviewed" / "scripts" / "check.py"),
    ]
    # all recorded directories became empty and are removed; the skills root stays
    assert not (root / "reviewed").exists()
    assert root.exists()
    persisted = json.loads((tmp_path / "state.json").read_text())
    assert persisted["skills"] == {}


def test_skill_symlinked_intermediate_directory_resolving_outside_is_rejected(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "evil").symlink_to(outside)
    package = _reviewed_package(skill_id="evil", files={"x.txt": b"x\n"})
    with pytest.raises(ValueError, match="outside the profile skills root"):
        harness_profile_cmd.plan_skills(skills_root=root, packages=(package,), state={"skills": {}})


def test_skill_ownership_is_persisted_per_file_not_per_command(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    state_path = tmp_path / "brigade" / "install-state.json"
    package = _reviewed_package(files={"SKILL.md": b"# a\n", "scripts/check.py": b"print(1)\n"})
    real = harness_profile_cmd.write_profile_state
    saves = []

    def counting(*, state_path, state):
        saves.append(sorted(state["skills"].get("reviewed", {}).get("files", {})))
        if len(saves) == 2:
            raise KeyboardInterrupt
        real(state_path=state_path, state=state)

    monkeypatch.setattr(harness_profile_cmd, "write_profile_state", counting)
    state = harness_profile_cmd.empty_profile_state(workspace=tmp_path, harness="codex")
    plans = harness_profile_cmd.plan_skills(skills_root=root, packages=(package,), state=state)
    with pytest.raises(KeyboardInterrupt):
        harness_profile_cmd.apply_skill_plan(
            skills_root=root,
            packages=(package,),
            plans=plans,
            prior_state=state,
            state_path=state_path,
        )
    # first file and first ownership record already exist on disk after the crash
    assert (root / "reviewed" / "SKILL.md").exists()
    assert saves[0] == ["SKILL.md"]
    assert json.loads(state_path.read_text())["skills"]["reviewed"]["files"] == {
        "SKILL.md": harness_profile_cmd.digest_bytes(b"# a\n")
    }


def _valid_state_payload(base_workspace: Path, **overrides) -> dict:
    payload = {
        "schema_version": harness_profiles.PROFILE_STATE_VERSION,
        "package_version": BRIGADE_VERSION,
        "harness": "codex",
        "workspace": str(base_workspace.expanduser().resolve()),
        "instructions": {},
        "skills": {},
        "generated": {},
        "mcp": {},
    }
    payload.update(overrides)
    return payload


def test_load_profile_state_missing_returns_seeded_schema_v2_state(tmp_path):
    state_path = tmp_path / "brigade" / "install-state.json"
    state_path.parent.mkdir(parents=True)

    loaded = harness_profile_cmd.load_profile_state(state_path=state_path, workspace=tmp_path, harness="codex")
    assert loaded.error is None
    assert loaded.state["schema_version"] == harness_profiles.PROFILE_STATE_VERSION
    assert loaded.state["package_version"] == BRIGADE_VERSION
    assert loaded.state["harness"] == "codex"
    assert loaded.state["workspace"] == str(tmp_path.expanduser().resolve())
    for section in ("instructions", "skills", "generated", "mcp"):
        assert loaded.state[section] == {}
    assert "artifacts" not in loaded.state


def test_load_profile_state_rejects_unreadable_bytes_and_invalid_json(tmp_path):
    state_path = tmp_path / "brigade" / "install-state.json"
    state_path.parent.mkdir(parents=True)

    state_path.write_bytes(b"\xff\xfe\x00")
    assert (
        harness_profile_cmd.load_profile_state(state_path=state_path, workspace=tmp_path, harness="codex").error
        == "ownership state is unreadable"
    )

    state_path.write_text("{not json")
    assert (
        harness_profile_cmd.load_profile_state(state_path=state_path, workspace=tmp_path, harness="codex").error
        == "ownership state is unreadable"
    )


def test_load_profile_state_rejects_non_object_json(tmp_path):
    state_path = tmp_path / "brigade" / "install-state.json"
    state_path.parent.mkdir(parents=True)

    state_path.write_text("[]")
    assert (
        harness_profile_cmd.load_profile_state(state_path=state_path, workspace=tmp_path, harness="codex").error
        == "ownership state is not an object"
    )


def test_load_profile_state_rejects_unsupported_or_missing_schema_version(tmp_path):
    state_path = tmp_path / "brigade" / "install-state.json"
    state_path.parent.mkdir(parents=True)

    state_path.write_text(
        json.dumps(
            {
                "schema_version": 999,
                "harness": "codex",
                "workspace": str(tmp_path.resolve()),
            }
        )
    )
    assert (
        harness_profile_cmd.load_profile_state(state_path=state_path, workspace=tmp_path, harness="codex").error
        == "unsupported ownership state version: 999"
    )

    state_path.write_text(
        json.dumps(
            {
                "harness": "codex",
                "workspace": str(tmp_path.resolve()),
            }
        )
    )
    assert (
        harness_profile_cmd.load_profile_state(state_path=state_path, workspace=tmp_path, harness="codex").error
        == "unsupported ownership state version: None"
    )


def test_load_profile_state_rejects_harness_mismatch(tmp_path):
    state_path = tmp_path / "brigade" / "install-state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps(_valid_state_payload(tmp_path, harness="claude")))

    assert (
        harness_profile_cmd.load_profile_state(state_path=state_path, workspace=tmp_path, harness="codex").error
        == "ownership harness mismatch: claude != codex"
    )


def test_load_profile_state_rejects_workspace_mismatch(tmp_path):
    state_path = tmp_path / "brigade" / "install-state.json"
    state_path.parent.mkdir(parents=True)
    other = tmp_path / "other"
    other.mkdir()
    state_path.write_text(json.dumps(_valid_state_payload(tmp_path, workspace=str(other.expanduser().resolve()))))

    assert harness_profile_cmd.load_profile_state(state_path=state_path, workspace=tmp_path, harness="codex").error == (
        f"ownership workspace mismatch: {other.expanduser().resolve()} != {tmp_path.expanduser().resolve()}"
    )


def test_load_profile_state_rejects_non_object_sections_in_order(tmp_path):
    state_path = tmp_path / "brigade" / "install-state.json"
    state_path.parent.mkdir(parents=True)

    for bad_section in ("instructions", "skills", "generated", "mcp"):
        payload = _valid_state_payload(tmp_path)
        for section in ("instructions", "skills", "generated", "mcp"):
            payload[section] = [] if section == bad_section else {}
        state_path.write_text(json.dumps(payload))
        assert (
            harness_profile_cmd.load_profile_state(state_path=state_path, workspace=tmp_path, harness="codex").error
            == f"ownership state section is not an object: {bad_section}"
        )


def test_load_profile_state_returns_valid_state_unchanged(tmp_path):
    state_path = tmp_path / "brigade" / "install-state.json"
    state_path.parent.mkdir(parents=True)
    original = _valid_state_payload(
        tmp_path,
        instructions={"digest": "abc"},
        skills={"reviewed": {"files": {"SKILL.md": "deadbeef"}}},
        generated={"pi": {"files": {"index.ts": "deadbeef"}}},
        mcp={"servers": {"brigade": {}}},
    )
    state_path.write_text(json.dumps(original))
    before = state_path.read_bytes()

    loaded = harness_profile_cmd.load_profile_state(state_path=state_path, workspace=tmp_path, harness="codex")
    assert loaded.error is None
    assert loaded.state == original
    # no rewrite when package_version is current
    assert state_path.read_bytes() == before


def test_load_profile_state_refreshes_stale_package_version_atomically(tmp_path):
    state_path = tmp_path / "brigade" / "install-state.json"
    state_path.parent.mkdir(parents=True)
    stale = _valid_state_payload(
        tmp_path,
        package_version="0.0.0-old",
        instructions={"digest": "abc"},
        skills={"reviewed": {"files": {"SKILL.md": "deadbeef"}}},
    )
    state_path.write_text(json.dumps(stale))

    loaded = harness_profile_cmd.load_profile_state(state_path=state_path, workspace=tmp_path, harness="codex")
    assert loaded.error is None
    assert loaded.state["package_version"] == BRIGADE_VERSION

    persisted = json.loads(state_path.read_text())
    assert persisted["package_version"] == BRIGADE_VERSION
    # refresh does not add public mutation/report fields
    assert set(persisted.keys()) == set(stale.keys())
    assert set(loaded.state.keys()) == set(stale.keys())
    assert "reload_required" not in persisted
    assert "written_files" not in persisted
    # non-version fields are preserved
    assert persisted["instructions"] == stale["instructions"]
    assert persisted["skills"] == stale["skills"]


# --- Issue #438 Task 7: aggregate brigade harness CLI ---


def test_harness_cli_parser_and_dispatch_exact_contract(tmp_path, monkeypatch):
    from brigade import cli

    calls: dict[str, dict] = {}

    def recorder(name):
        def _f(**kwargs):
            calls[name] = kwargs
            return 0

        return _f

    monkeypatch.setattr(harness_profile_cmd, "install", recorder("install"), raising=False)
    monkeypatch.setattr(harness_profile_cmd, "uninstall", recorder("uninstall"), raising=False)
    monkeypatch.setattr(harness_profile_cmd, "doctor", recorder("doctor"), raising=False)
    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)

    # choices cover all eight harness ids plus all
    for harness in (*harness_profiles.HARNESS_IDS, "all"):
        cli.main(["harness", "install", harness, "--scope", "user", "--json"])
        assert calls["install"]["harness"] == harness
        assert calls["install"]["workspace"] == tmp_path  # default Path.cwd()
        assert calls["install"]["write"] is False
        assert calls["install"]["allow_global_stdio"] is False
        assert calls["install"]["adopt"] is False
        assert calls["install"]["json_output"] is True

    # install flags pass through exactly
    ws = tmp_path / "ws"
    ws.mkdir()
    cli.main(
        [
            "harness",
            "install",
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
    assert calls["install"] == {
        "harness": "all",
        "workspace": ws,
        "write": True,
        "allow_global_stdio": True,
        "adopt": True,
        "json_output": True,
    }

    # uninstall: mutually exclusive --dry-run/--write, default dry-run
    cli.main(["harness", "uninstall", "codex", "--scope", "user", "--json"])
    assert calls["uninstall"] == {
        "harness": "codex",
        "workspace": tmp_path,
        "write": False,
        "json_output": True,
    }
    cli.main(
        [
            "harness",
            "uninstall",
            "codex",
            "--scope",
            "user",
            "--workspace",
            str(ws),
            "--write",
            "--json",
        ]
    )
    assert calls["uninstall"] == {
        "harness": "codex",
        "workspace": ws,
        "write": True,
        "json_output": True,
    }

    # doctor: --verify-mcp
    cli.main(["harness", "doctor", "kimi", "--scope", "user", "--json"])
    assert calls["doctor"] == {
        "harness": "kimi",
        "workspace": tmp_path,
        "verify_mcp": False,
        "json_output": True,
    }
    cli.main(
        [
            "harness",
            "doctor",
            "kimi",
            "--scope",
            "user",
            "--workspace",
            str(ws),
            "--verify-mcp",
            "--json",
        ]
    )
    assert calls["doctor"] == {
        "harness": "kimi",
        "workspace": ws,
        "verify_mcp": True,
        "json_output": True,
    }

    # --scope is required
    with pytest.raises(SystemExit):
        cli.main(["harness", "install", "cursor", "--json"])

    # no sync verb
    with pytest.raises(SystemExit):
        cli.main(["harness", "sync", "cursor", "--scope", "user", "--json"])


def test_harness_all_reports_partial_failure_without_rollback(tmp_path, monkeypatch, capsys):
    from brigade import cli

    home, workspace = tmp_path / "home", tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(harness_profiles, "probe_kimi_native_mcp", lambda: True)

    codex_file = home / ".codex" / "AGENTS.md"
    codex_file.parent.mkdir(parents=True)
    codex_file.write_text(f"{harness_profiles.INSTRUCTION_START}\nuser edit\n{harness_profiles.INSTRUCTION_END}\n")

    assert (
        cli.main(
            [
                "harness",
                "install",
                "all",
                "--scope",
                "user",
                "--workspace",
                str(workspace),
                "--write",
                "--json",
            ]
        )
        == 1
    )
    payload = json.loads(capsys.readouterr().out)
    by_id = {row["harness"]: row for row in payload["results"]}

    assert payload["schema_version"] == 1
    assert payload["operation"] == "install"
    assert tuple(by_id) == harness_profiles.HARNESS_IDS
    assert by_id["claude"]["status"] == "updated"
    assert by_id["codex"]["status"] == "conflict"
    assert (home / ".claude" / "CLAUDE.md").is_file()
    # no rollback: claude write survives the codex conflict
    assert codex_file.read_text().endswith(f"user edit\n{harness_profiles.INSTRUCTION_END}\n")
    # private digests are stripped from public output recursively
    text = json.dumps(payload)
    assert "digest" not in text and "fingerprint" not in text


def test_harness_openclaw_tracked_agents_md_is_conflict_untracked_can_receive(tmp_path, monkeypatch, capsys):
    from brigade import cli

    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(harness_profiles, "probe_kimi_native_mcp", lambda: True)

    # initialize a git repo at the workspace and track AGENTS.md
    import subprocess

    subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True)
    (workspace / "AGENTS.md").write_text("# tracked\n")
    subprocess.run(["git", "add", "AGENTS.md"], cwd=workspace, check=True, capture_output=True)

    assert (
        cli.main(
            [
                "harness",
                "install",
                "openclaw",
                "--scope",
                "user",
                "--workspace",
                str(workspace),
                "--write",
                "--json",
            ]
        )
        == 1
    )
    payload = json.loads(capsys.readouterr().out)
    row = payload["results"][0]
    assert row["status"] == "conflict"
    # tracked file bytes are unchanged
    assert (workspace / "AGENTS.md").read_text() == "# tracked\n"

    # an untracked workspace AGENTS.md can receive the bounded block
    untracked = tmp_path / "untracked-ws"
    untracked.mkdir()
    subprocess.run(["git", "init"], cwd=untracked, check=True, capture_output=True)
    (untracked / "AGENTS.md").write_text("# my notes\n")
    assert (
        cli.main(
            [
                "harness",
                "install",
                "openclaw",
                "--scope",
                "user",
                "--workspace",
                str(untracked),
                "--write",
                "--json",
            ]
        )
        == 0
    )
    out = json.loads(capsys.readouterr().out)
    assert out["results"][0]["status"] == "updated"
    assert harness_profiles.INSTRUCTION_START in (untracked / "AGENTS.md").read_text()


def test_harness_doctor_reports_ready_and_uninstall_removes_owned_instruction(tmp_path, monkeypatch, capsys):
    from brigade import cli

    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(harness_profiles, "probe_kimi_native_mcp", lambda: True)

    base = ["harness", "install", "codex", "--scope", "user", "--workspace", str(workspace)]
    assert cli.main(base + ["--write", "--json"]) == 0
    capsys.readouterr()

    assert cli.main(base + ["--json", "--dry-run"]) == 0
    doctor = json.loads(capsys.readouterr().out)
    row = doctor["results"][0]
    assert row["ready"] is True
    assert row["instruction_ready"] is True
    assert row["status"] in {"current", "ready"}

    # corrupt state blocks all mutation
    state = home / ".codex" / "brigade" / "install-state.json"
    state.write_bytes(b"\xff\xfe")
    assert cli.main(base + ["--write", "--json"]) == 1
    install_payload = json.loads(capsys.readouterr().out)
    assert install_payload["results"][0]["status"] == "conflict"
    assert install_payload["results"][0]["files_written"] == []
    assert state.read_bytes() == b"\xff\xfe"

    # repair state and uninstall removes only the owned block
    state.unlink()
    assert cli.main(base + ["--write", "--json"]) == 0
    capsys.readouterr()
    agents = home / ".codex" / "AGENTS.md"
    agents.write_text(f"# keep me\n{harness_profiles.INSTRUCTION_START}\nbody\n{harness_profiles.INSTRUCTION_END}\n")
    # corrupt the owned block so uninstall must conflict, not remove
    assert (
        cli.main(
            ["harness", "uninstall", "codex", "--scope", "user", "--workspace", str(workspace), "--write", "--json"]
        )
        == 1
    )
    uninstall_payload = json.loads(capsys.readouterr().out)
    row = uninstall_payload["results"][0]
    assert row["status"] == "conflict"
    assert harness_profiles.INSTRUCTION_START in agents.read_text()


def _seed_two_file_reviewed_skill(workspace: Path, name="two-file"):
    skill_dir = workspace / "sources" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(f"# {name}\n\nDo the work.\n")
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
                "supported_harnesses": list(harness_profiles.HARNESS_IDS),
                "trust_level": "workspace",
                "enabled": True,
                "tests": [],
            }
        )
    )
    assert skills_cmd.import_skill(target=workspace, source=skill_dir, json_output=True) == 0
    return skill_dir


def test_harness_uninstall_removes_safe_surfaces_and_preserves_conflicts_partially(tmp_path, monkeypatch, capsys):
    from brigade import cli

    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(harness_profiles, "probe_kimi_native_mcp", lambda: True)
    _seed_two_file_reviewed_skill(workspace)

    install = ["harness", "install", "codex", "--scope", "user", "--workspace", str(workspace), "--write", "--json"]
    uninstall = ["harness", "uninstall", "codex", "--scope", "user", "--workspace", str(workspace), "--write", "--json"]

    assert cli.main(install) == 0
    capsys.readouterr()
    skill_root = home / ".codex" / "skills" / "two-file"
    assert (skill_root / "SKILL.md").is_file()
    assert (skill_root / "scripts" / "check.py").is_file()
    state_path = home / ".codex" / "brigade" / "install-state.json"
    owned = json.loads(state_path.read_text())["skills"]["two-file"]["files"]
    assert set(owned) == {"SKILL.md", "skill.json", "scripts/check.py"}

    # foreign-edit one of the owned skill files (scripts/check.py)
    (skill_root / "scripts" / "check.py").write_text("print('user edit')\n")

    assert cli.main(uninstall) == 1
    payload = json.loads(capsys.readouterr().out)
    row = payload["results"][0]
    assert row["status"] == "conflict"
    assert row["ready"] is False
    # the digest-matching owned files are removed even though the sibling conflicts
    assert not (skill_root / "SKILL.md").exists()
    assert not (skill_root / "skill.json").exists()
    # the foreign-edited file is preserved
    assert (skill_root / "scripts" / "check.py").read_text() == "print('user edit')\n"
    assert str(skill_root / "SKILL.md") in row["files_removed"]
    assert str(skill_root / "skill.json") in row["files_removed"]
    assert str(skill_root / "scripts" / "check.py") not in row["files_removed"]
    # state retains only the edited file's ownership; the conflict keeps state alive
    retained = json.loads(state_path.read_text())["skills"]["two-file"]["files"]
    assert set(retained) == {"scripts/check.py"}
    assert state_path.exists()

    # restoring the owned bytes lets a second uninstall finish cleanly
    (skill_root / "scripts" / "check.py").write_text("print(1)\n")
    assert cli.main(uninstall) == 0
    finish = json.loads(capsys.readouterr().out)
    assert finish["results"][0]["conflicts"] == []
    assert not (skill_root / "scripts" / "check.py").exists()
    # all owned sections empty -> state file removed
    assert not state_path.exists()


def test_harness_openclaw_doctor_does_not_conflict_solely_due_to_tracked_file(tmp_path, monkeypatch, capsys):
    from brigade import cli
    import subprocess

    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(harness_profiles, "probe_kimi_native_mcp", lambda: True)

    subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True)
    agents = workspace / "AGENTS.md"
    agents.write_text("# tracked\n")
    subprocess.run(["git", "add", "AGENTS.md"], cwd=workspace, check=True, capture_output=True)

    # install --write still refuses to mutate the tracked file (conflict/preserve)
    assert (
        cli.main(
            ["harness", "install", "openclaw", "--scope", "user", "--workspace", str(workspace), "--write", "--json"]
        )
        == 1
    )
    install_payload = json.loads(capsys.readouterr().out)
    assert install_payload["results"][0]["status"] == "conflict"
    assert agents.read_text() == "# tracked\n"

    # doctor only checks current content; a tracked file is not a conflict by itself
    assert cli.main(["harness", "doctor", "openclaw", "--scope", "user", "--workspace", str(workspace), "--json"]) == 1
    doctor = json.loads(capsys.readouterr().out)
    row = doctor["results"][0]
    assert row["status"] == "conflict"
    # the conflict is the foreign content, not the tracked-ness
    assert not any("tracked by git" in (c.get("detail") or "") for c in row["conflicts"])

    # when the tracked file holds the current managed block, doctor is ready
    desired = harness_profiles.managed_instruction_text()
    agents.write_text(f"{harness_profiles.INSTRUCTION_START}\n{desired}\n{harness_profiles.INSTRUCTION_END}\n")
    state = home / ".openclaw" / "brigade" / "install-state.json"
    state.parent.mkdir(parents=True)
    state.write_text(
        json.dumps(
            {
                "schema_version": harness_profiles.PROFILE_STATE_VERSION,
                "package_version": BRIGADE_VERSION,
                "harness": "openclaw",
                "workspace": str(workspace.resolve()),
                "instructions": {"digest": harness_profile_cmd.digest_text(desired)},
                "skills": {},
                "generated": {},
                "mcp": {},
            }
        )
    )
    assert cli.main(["harness", "doctor", "openclaw", "--scope", "user", "--workspace", str(workspace), "--json"]) == 0
    ready = json.loads(capsys.readouterr().out)
    rrow = ready["results"][0]
    assert rrow["ready"] is True
    assert rrow["instruction_ready"] is True
    assert rrow["status"] in {"current", "ready"}
