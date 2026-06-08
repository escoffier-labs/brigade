"""Tests for the auto-gitignore behavior of `brigade init`."""
from __future__ import annotations

from pathlib import Path

from brigade import install as install_mod
from brigade.install import install_selection
from brigade.selection import Selection


def _repo_selection() -> Selection:
    return Selection(depth="repo", harnesses=["claude"], owner="claude", includes=[])


def _read_gi(target: Path) -> str:
    return (target / ".gitignore").read_text()


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
    assert ".brigade/reviews.toml" in gi
    assert ".brigade/security.toml" in gi
    assert ".brigade/runs/" in gi
    assert ".brigade/scanners/" in gi
    assert ".brigade/security/" in gi
    assert ".brigade/chat-memory-sweeps/" in gi
    assert ".brigade/work/" in gi


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
    assert ".claude/memory-handoffs/*" in block
    assert "!.claude/memory-handoffs/TEMPLATE.md" in block
    assert ".brigade/dogfood.toml" in block
    assert ".brigade/handoff-sources.json" in block
    assert ".brigade/memory-care.toml" in block
    assert ".brigade/runs/" in block
    assert ".brigade/scanners/" in block
    assert ".brigade/security/" in block
    assert ".brigade/work/" in block
    assert ".codex/memory-handoffs" not in block


def test_gitignore_block_includes_codex_section_when_selected():
    from brigade.install import build_gitignore_block
    sel = Selection(depth="repo", harnesses=["claude", "codex"], owner="claude", includes=[])
    block = build_gitignore_block(sel)
    assert ".claude/memory-handoffs/*" in block
    assert ".codex/memory-handoffs/*" in block
    assert "!.codex/memory-handoffs/TEMPLATE.md" in block


def test_gitignore_block_includes_hermes_section_when_selected():
    from brigade.install import build_gitignore_block
    sel = Selection(depth="repo", harnesses=["hermes"], owner="hermes", includes=[])
    block = build_gitignore_block(sel)
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
