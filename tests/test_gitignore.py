"""Tests for the auto-gitignore behavior of `solo-mise init`."""
from __future__ import annotations

from pathlib import Path

from solo_mise import init as init_mod
from solo_mise.selection import Selection


def _read_gi(target: Path) -> str:
    return (target / ".gitignore").read_text()


def test_init_creates_gitignore_when_missing(tmp_target: Path):
    rc = init_mod.run(target=tmp_target, profile_id="repo")
    assert rc == 0
    gi = _read_gi(tmp_target)
    assert init_mod.GITIGNORE_BEGIN in gi
    assert init_mod.GITIGNORE_END in gi
    assert ".claude/memory-handoffs/*" in gi
    assert "!.claude/memory-handoffs/TEMPLATE.md" in gi
    assert "memory/handoff-inbox/" in gi


def test_init_appends_block_to_existing_gitignore(tmp_target: Path):
    tmp_target.mkdir()
    pre_existing = "# project rules\n*.log\n.env\n"
    (tmp_target / ".gitignore").write_text(pre_existing)
    rc = init_mod.run(target=tmp_target, profile_id="repo")
    assert rc == 0
    gi = _read_gi(tmp_target)
    assert pre_existing.strip() in gi, "should preserve existing rules"
    assert init_mod.GITIGNORE_BEGIN in gi
    assert init_mod.GITIGNORE_END in gi
    # block appears exactly once
    assert gi.count(init_mod.GITIGNORE_BEGIN) == 1
    assert gi.count(init_mod.GITIGNORE_END) == 1


def test_init_idempotent_replaces_block(tmp_target: Path):
    rc = init_mod.run(target=tmp_target, profile_id="repo")
    assert rc == 0
    first = _read_gi(tmp_target)

    # Tamper with the block to confirm replacement happens, not append.
    tampered = first.replace(".claude/memory-handoffs/*", "GARBAGE_LINE")
    (tmp_target / ".gitignore").write_text(tampered)

    rc = init_mod.run(target=tmp_target, profile_id="repo", force=True)
    assert rc == 0
    second = _read_gi(tmp_target)
    assert ".claude/memory-handoffs/*" in second
    assert "GARBAGE_LINE" not in second
    # still exactly one block
    assert second.count(init_mod.GITIGNORE_BEGIN) == 1


def test_init_preserves_user_edits_outside_block(tmp_target: Path):
    tmp_target.mkdir()
    pre = "node_modules/\n# user rules\n*.swp\n"
    (tmp_target / ".gitignore").write_text(pre)
    init_mod.run(target=tmp_target, profile_id="repo")
    # Add user content AFTER the block; re-run should keep it.
    gi_text = _read_gi(tmp_target)
    gi_text += "\n# after block\n.local-cache/\n"
    (tmp_target / ".gitignore").write_text(gi_text)

    init_mod.run(target=tmp_target, profile_id="repo", force=True)
    final = _read_gi(tmp_target)
    assert "node_modules/" in final
    assert "*.swp" in final
    assert ".local-cache/" in final


def test_no_gitignore_flag_skips_creation(tmp_target: Path):
    rc = init_mod.run(target=tmp_target, profile_id="repo", update_gitignore=False)
    assert rc == 0
    assert not (tmp_target / ".gitignore").exists()


def test_no_gitignore_flag_leaves_existing_alone(tmp_target: Path):
    tmp_target.mkdir()
    pre = "# only my rules\n*.tmp\n"
    (tmp_target / ".gitignore").write_text(pre)
    rc = init_mod.run(
        target=tmp_target, profile_id="repo", update_gitignore=False
    )
    assert rc == 0
    assert _read_gi(tmp_target) == pre


def test_dry_run_does_not_write_gitignore(tmp_target: Path):
    rc = init_mod.run(target=tmp_target, profile_id="repo", dry_run=True)
    assert rc == 0
    assert not (tmp_target / ".gitignore").exists()
    assert not tmp_target.exists()


def test_workspace_profile_also_adds_block(tmp_target: Path):
    rc = init_mod.run(target=tmp_target, profile_id="workspace")
    assert rc == 0
    gi = _read_gi(tmp_target)
    assert init_mod.GITIGNORE_BEGIN in gi
    assert "memory/handoff-inbox/" in gi


def test_gitignore_block_includes_claude_section_when_selected():
    from solo_mise.install import build_gitignore_block
    sel = Selection(depth="repo", harnesses=["claude"], owner="claude", includes=[])
    block = build_gitignore_block(sel)
    assert ".claude/memory-handoffs/*" in block
    assert "!.claude/memory-handoffs/TEMPLATE.md" in block
    assert ".codex/memory-handoffs" not in block


def test_gitignore_block_includes_codex_section_when_selected():
    from solo_mise.install import build_gitignore_block
    sel = Selection(depth="repo", harnesses=["claude", "codex"], owner="claude", includes=[])
    block = build_gitignore_block(sel)
    assert ".claude/memory-handoffs/*" in block
    assert ".codex/memory-handoffs/*" in block
    assert "!.codex/memory-handoffs/TEMPLATE.md" in block


def test_gitignore_block_no_inbox_section_for_readers_only():
    from solo_mise.install import build_gitignore_block
    sel = Selection(depth="workspace", harnesses=["openclaw"], owner="openclaw", includes=[])
    block = build_gitignore_block(sel)
    assert "memory-handoffs" not in block


def test_install_writes_gitignore_block(tmp_path):
    from solo_mise.install import install_selection
    sel = Selection(depth="repo", harnesses=["claude", "codex"], owner="claude", includes=[])
    install_selection(tmp_path, sel)
    gi = (tmp_path / ".gitignore").read_text()
    assert "# >>> solo-mise gitignore block >>>" in gi
    assert ".claude/memory-handoffs/*" in gi
    assert ".codex/memory-handoffs/*" in gi
