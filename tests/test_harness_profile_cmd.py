from brigade import harness_profile_cmd, harness_profiles


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
