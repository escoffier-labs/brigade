"""Tests for the auto-gitignore behavior of `brigade init`."""

from __future__ import annotations

import subprocess
from pathlib import Path

from brigade import install as install_mod
from brigade.install import install_selection
from brigade.selection import Selection


def _repo_selection() -> Selection:
    return Selection(depth="repo", harnesses=["claude"], owner="claude", includes=[])


def _read_gi(target: Path) -> str:
    return (target / ".gitignore").read_text()


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def test_init_creates_gitignore_when_missing(tmp_target: Path):
    rc = install_selection(tmp_target, _repo_selection())
    assert rc == 0
    gi = _read_gi(tmp_target)
    assert install_mod.GITIGNORE_BEGIN in gi
    assert install_mod.GITIGNORE_END in gi
    assert ".claude/memory-handoffs/*" in gi
    assert "!.claude/memory-handoffs/TEMPLATE.md" in gi
    assert "memory/handoff-inbox/" in gi
    assert ".brigade/dogfood.toml" in gi
    assert ".brigade/handoff-sources.json" in gi
    assert ".brigade/daily.toml" in gi
    assert ".brigade/memory-care.toml" in gi
    assert ".brigade/vault.toml" in gi
    assert ".brigade/vault-propose/" in gi
    assert ".brigade/reviews.toml" in gi
    assert ".brigade/security.toml" in gi
    assert ".brigade/runs/" in gi
    assert ".brigade/scanners/" in gi
    assert ".brigade/security/" in gi
    assert ".brigade/chat-memory-sweeps/" in gi
    assert ".brigade/campaigns/" in gi
    assert ".brigade/work/" in gi


def test_init_gitignore_leaves_mcp_catalog_trackable_but_ignores_local_state(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    assert _git(repo, "init").returncode == 0

    rc = install_selection(repo, _repo_selection())

    assert rc == 0
    assert _git(repo, "check-ignore", ".brigade/mcp.json").returncode == 1
    for ignored in (
        ".brigade/mcp/state.json",
        ".brigade/work/verify-runs/run.json",
        ".brigade/security/cache.json",
        ".brigade/runs/session.json",
    ):
        result = _git(repo, "check-ignore", ignored)
        assert result.returncode == 0, result.stderr


def test_init_appends_block_to_existing_gitignore(tmp_target: Path):
    tmp_target.mkdir()
    pre_existing = "# project rules\n*.log\n.env\n"
    (tmp_target / ".gitignore").write_text(pre_existing)
    rc = install_selection(tmp_target, _repo_selection())
    assert rc == 0
    gi = _read_gi(tmp_target)
    assert pre_existing.strip() in gi, "should preserve existing rules"
    assert install_mod.GITIGNORE_BEGIN in gi
    assert install_mod.GITIGNORE_END in gi
    # block appears exactly once
    assert gi.count(install_mod.GITIGNORE_BEGIN) == 1
    assert gi.count(install_mod.GITIGNORE_END) == 1


def test_init_idempotent_replaces_block(tmp_target: Path):
    rc = install_selection(tmp_target, _repo_selection())
    assert rc == 0
    first = _read_gi(tmp_target)

    # Tamper with the block to confirm replacement happens, not append.
    tampered = first.replace(".claude/memory-handoffs/*", "GARBAGE_LINE")
    (tmp_target / ".gitignore").write_text(tampered)

    rc = install_selection(tmp_target, _repo_selection(), force=True)
    assert rc == 0
    second = _read_gi(tmp_target)
    assert ".claude/memory-handoffs/*" in second
    assert "GARBAGE_LINE" not in second
    # still exactly one block
    assert second.count(install_mod.GITIGNORE_BEGIN) == 1


def test_init_replaces_legacy_solo_mise_gitignore_block(tmp_target: Path):
    tmp_target.mkdir()
    (tmp_target / ".gitignore").write_text(
        "\n".join(
            [
                "# user rules",
                "*.log",
                "",
                install_mod.LEGACY_GITIGNORE_BEGIN,
                "GARBAGE_LINE",
                install_mod.LEGACY_GITIGNORE_END,
                "",
                "# after block",
                ".local-cache/",
                "",
            ]
        )
    )

    rc = install_selection(tmp_target, _repo_selection())

    assert rc == 0
    gi = _read_gi(tmp_target)
    assert "# user rules" in gi
    assert ".local-cache/" in gi
    assert install_mod.GITIGNORE_BEGIN in gi
    assert install_mod.GITIGNORE_END in gi
    assert install_mod.LEGACY_GITIGNORE_BEGIN not in gi
    assert install_mod.LEGACY_GITIGNORE_END not in gi
    assert "GARBAGE_LINE" not in gi
    assert gi.count(install_mod.GITIGNORE_BEGIN) == 1


def test_init_collapses_current_and_legacy_gitignore_blocks(tmp_target: Path):
    tmp_target.mkdir()
    (tmp_target / ".gitignore").write_text(
        "\n".join(
            [
                "# user rules",
                "*.log",
                "",
                install_mod.GITIGNORE_BEGIN,
                "STALE_CURRENT_LINE",
                install_mod.GITIGNORE_END,
                "",
                "# between blocks",
                ".cache/",
                "",
                install_mod.LEGACY_GITIGNORE_BEGIN,
                "STALE_LEGACY_LINE",
                install_mod.LEGACY_GITIGNORE_END,
                "",
                "# after block",
                ".local-cache/",
                "",
            ]
        )
    )

    rc = install_selection(tmp_target, _repo_selection())

    assert rc == 0
    gi = _read_gi(tmp_target)
    assert "# user rules" in gi
    assert "# between blocks" in gi
    assert ".cache/" in gi
    assert ".local-cache/" in gi
    assert "STALE_CURRENT_LINE" not in gi
    assert "STALE_LEGACY_LINE" not in gi
    assert install_mod.LEGACY_GITIGNORE_BEGIN not in gi
    assert install_mod.LEGACY_GITIGNORE_END not in gi
    assert gi.count(install_mod.GITIGNORE_BEGIN) == 1
    assert gi.count(install_mod.GITIGNORE_END) == 1


def test_init_preserves_user_edits_outside_block(tmp_target: Path):
    tmp_target.mkdir()
    pre = "node_modules/\n# user rules\n*.swp\n"
    (tmp_target / ".gitignore").write_text(pre)
    install_selection(tmp_target, _repo_selection())
    # Add user content AFTER the block; re-run should keep it.
    gi_text = _read_gi(tmp_target)
    gi_text += "\n# after block\n.local-cache/\n"
    (tmp_target / ".gitignore").write_text(gi_text)

    install_selection(tmp_target, _repo_selection(), force=True)
    final = _read_gi(tmp_target)
    assert "node_modules/" in final
    assert "*.swp" in final
    assert ".local-cache/" in final


def test_dry_run_does_not_write_gitignore(tmp_target: Path):
    rc = install_selection(tmp_target, _repo_selection(), dry_run=True)
    assert rc == 0
    assert not (tmp_target / ".gitignore").exists()
    # dry-run also must not materialize the target directory
    assert not tmp_target.exists()


def test_workspace_profile_also_adds_block(tmp_target: Path):
    rc = install_selection(
        tmp_target,
        Selection(depth="workspace", harnesses=["claude"], owner="claude", includes=[]),
    )
    assert rc == 0
    gi = _read_gi(tmp_target)
    assert install_mod.GITIGNORE_BEGIN in gi
    assert "memory/handoff-inbox/" in gi


def test_gitignore_block_includes_claude_section_when_selected():
    from brigade.install import build_gitignore_block

    sel = Selection(depth="repo", harnesses=["claude"], owner="claude", includes=[])
    block = build_gitignore_block(sel)
    assert "!.claude/" in block
    assert ".claude/*" in block
    assert "!.claude/memory-handoffs/" in block
    assert ".claude/memory-handoffs/*" in block
    assert "!.claude/memory-handoffs/TEMPLATE.md" in block
    assert ".brigade/dogfood.toml" in block
    assert ".brigade/handoff-sources.json" in block
    assert ".brigade/memory-care.toml" in block
    assert ".brigade/vault.toml" in block
    assert ".brigade/vault-propose/" in block
    assert ".brigade/runs/" in block
    assert ".brigade/scanners/" in block
    assert ".brigade/security/" in block
    assert ".brigade/campaigns/" in block
    assert ".brigade/work/" in block
    assert ".codex/memory-handoffs" not in block


def test_gitignore_block_includes_codex_section_when_selected():
    from brigade.install import build_gitignore_block

    sel = Selection(depth="repo", harnesses=["claude", "codex"], owner="claude", includes=[])
    block = build_gitignore_block(sel)
    assert "!.codex/" in block
    assert ".codex/*" in block
    assert "!.codex/memory-handoffs/" in block
    assert ".claude/memory-handoffs/*" in block
    assert ".codex/memory-handoffs/*" in block
    assert "!.codex/memory-handoffs/TEMPLATE.md" in block


def test_gitignore_block_includes_hermes_section_when_selected():
    from brigade.install import build_gitignore_block

    sel = Selection(depth="repo", harnesses=["hermes"], owner="hermes", includes=[])
    block = build_gitignore_block(sel)
    assert "!.hermes/" in block
    assert ".hermes/*" in block
    assert "!.hermes/memory-handoffs/" in block
    assert ".hermes/memory-handoffs/*" in block
    assert "!.hermes/memory-handoffs/TEMPLATE.md" in block
    assert "!.hermes/memory-handoffs/.gitkeep" in block


def test_gitignore_block_no_inbox_section_for_readers_only():
    from brigade.install import build_gitignore_block

    sel = Selection(depth="workspace", harnesses=["openclaw"], owner="openclaw", includes=[])
    block = build_gitignore_block(sel)
    assert "memory-handoffs" not in block


def test_gitignore_block_includes_generated_tool_projection_roots():
    from brigade.install import build_gitignore_block

    sel = Selection(depth="repo", harnesses=["codex"], owner="codex", includes=[])
    block = build_gitignore_block(sel)
    for pattern in (
        ".claude/commands/",
        ".codex/skills/",
        ".opencode/commands/",
        ".opencode/superpowers/",
        ".antigravity/commands/",
        ".antigravity/superpowers/",
        ".pi/commands/",
        ".pi/superpowers/",
        ".cursor/rules/",
        ".cursor/skills/",
        ".hermes/commands/",
        ".hermes/superpowers/",
        ".openclaw/commands/",
        ".openclaw/superpowers/",
        ".mcp/",
        "scripts/*.md",
    ):
        assert pattern in block


def test_install_writes_gitignore_block(tmp_path):
    from brigade.install import install_selection

    sel = Selection(depth="repo", harnesses=["claude", "codex"], owner="claude", includes=[])
    install_selection(tmp_path, sel)
    gi = (tmp_path / ".gitignore").read_text()
    assert "# >>> brigade gitignore block >>>" in gi
    assert ".claude/memory-handoffs/*" in gi
    assert ".codex/memory-handoffs/*" in gi
    assert ".brigade/dogfood.toml" in gi
    assert ".brigade/runs/" in gi
    assert ".brigade/work/" in gi


def test_claude_handoff_template_stages_without_force_when_parent_claude_ignored(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    assert _git(repo, "init").returncode == 0

    # Parent `.claude/` ignore (brigade source-repo side-harness class or global excludesFile).
    (repo / ".gitignore").write_text(".claude/\n")

    rc = install_selection(repo, _repo_selection())
    assert rc == 0

    template = repo / ".claude" / "memory-handoffs" / "TEMPLATE.md"
    assert template.is_file()

    check_template = _git(repo, "check-ignore", ".claude/memory-handoffs/TEMPLATE.md")
    assert check_template.returncode == 1, check_template.stdout + check_template.stderr

    session_note = repo / ".claude" / "memory-handoffs" / "2026-08-10-session.md"
    session_note.write_text("# note\n")
    settings = repo / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text("{}\n")

    assert _git(repo, "check-ignore", ".claude/memory-handoffs/2026-08-10-session.md").returncode == 0
    assert _git(repo, "check-ignore", ".claude/settings.json").returncode == 0

    add = _git(repo, "add", ".claude/memory-handoffs/TEMPLATE.md")
    assert add.returncode == 0, add.stderr

    status = _git(repo, "status", "--porcelain", ".claude/memory-handoffs/TEMPLATE.md")
    assert status.stdout.strip() == "A  .claude/memory-handoffs/TEMPLATE.md"


def test_upgrade_legacy_inbox_only_block_to_parent_unignore_on_rerun(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    assert _git(repo, "init").returncode == 0

    legacy_block = "\n".join(
        [
            install_mod.GITIGNORE_BEGIN,
            "# claude: handoffs are session-local and may contain private context.",
            ".claude/memory-handoffs/*",
            "!.claude/memory-handoffs/TEMPLATE.md",
            "!.claude/memory-handoffs/.gitkeep",
            install_mod.GITIGNORE_END,
            "",
        ]
    )
    (repo / ".gitignore").write_text("# user rules\n*.log\n\n" + legacy_block)

    rc = install_selection(repo, _repo_selection(), force=True)
    assert rc == 0

    gi = _read_gi(repo)
    assert "# user rules" in gi
    assert "!.claude/" in gi
    assert ".claude/*" in gi
    assert "!.claude/memory-handoffs/" in gi
    assert gi.count(install_mod.GITIGNORE_BEGIN) == 1

    template = repo / ".claude" / "memory-handoffs" / "TEMPLATE.md"
    assert template.is_file()
    add = _git(repo, "add", ".claude/memory-handoffs/TEMPLATE.md")
    assert add.returncode == 0, add.stderr


def test_idempotent_rerun_without_force_preserves_user_rules_and_refreshes_block(
    tmp_target: Path,
):
    rc = install_selection(tmp_target, _repo_selection())
    assert rc == 0
    user_tail = "\n# after block\n.local-cache/\n"
    (tmp_target / ".gitignore").write_text(_read_gi(tmp_target) + user_tail)

    rc = install_selection(tmp_target, _repo_selection())
    assert rc == 0
    gi = _read_gi(tmp_target)
    assert ".local-cache/" in gi
    assert "!.claude/" in gi
    assert gi.count(install_mod.GITIGNORE_BEGIN) == 1


def test_non_force_rerun_upgrades_managed_block_without_overwriting_manifest_files(
    tmp_target: Path,
):
    assert install_selection(tmp_target, _repo_selection()) == 0

    agents = tmp_target / "AGENTS.md"
    claude = tmp_target / "CLAUDE.md"
    agents.write_text("# Custom AGENTS\n")
    claude.write_text("# Custom CLAUDE\n")

    gi = _read_gi(tmp_target).replace("!.claude/\n.claude/*\n!.claude/memory-handoffs/\n", "")
    user_tail = "\n# after block\n.local-cache/\n"
    (tmp_target / ".gitignore").write_text(gi + user_tail)

    assert install_selection(tmp_target, _repo_selection()) == 0

    assert agents.read_text() == "# Custom AGENTS\n"
    assert claude.read_text() == "# Custom CLAUDE\n"
    gi_final = _read_gi(tmp_target)
    assert "!.claude/" in gi_final
    assert ".claude/*" in gi_final
    assert "!.claude/memory-handoffs/" in gi_final
    assert ".local-cache/" in gi_final
    assert gi_final.count(install_mod.GITIGNORE_BEGIN) == 1
