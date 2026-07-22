from brigade import harness_profiles


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
