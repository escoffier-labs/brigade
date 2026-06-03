from pathlib import Path
import pytest
from brigade.install import resolve_manifests, install_selection
from brigade.selection import Selection


def test_resolve_manifests_repo_claude():
    sel = Selection(depth="repo", harnesses=["claude"], owner="claude", includes=[])
    files, dirs, notes = resolve_manifests(sel)
    dsts = [f["dst"] for f in files]
    assert "AGENTS.md" in dsts
    assert "CLAUDE.md" in dsts
    assert ".claude/memory-handoffs/TEMPLATE.md" in dsts
    assert ".claude/memory-handoffs/processed" in dirs


def test_resolve_manifests_workspace_claude_codex_openclaw():
    sel = Selection(
        depth="workspace",
        harnesses=["claude", "codex", "openclaw"],
        owner="openclaw",
        includes=[],
    )
    files, dirs, notes = resolve_manifests(sel)
    dsts = [f["dst"] for f in files]
    # Baseline
    assert "AGENTS.md" in dsts
    assert "MEMORY.md" in dsts
    # Claude
    assert "CLAUDE.md" in dsts
    assert ".claude/memory-handoffs/TEMPLATE.md" in dsts
    # Codex
    assert ".codex/memory-handoffs/TEMPLATE.md" in dsts
    # OpenClaw fragments
    assert ".brigade/openclaw/model-aliases.openclaw.json" in dsts
    # Each dst appears at most once
    assert len(dsts) == len(set(dsts))


def test_resolve_manifests_empty_harnesses():
    sel = Selection(depth="workspace", harnesses=[], owner="this-repo", includes=[])
    files, dirs, notes = resolve_manifests(sel)
    dsts = [f["dst"] for f in files]
    assert "CLAUDE.md" not in dsts
    assert not any(d.endswith("memory-handoffs/TEMPLATE.md") for d in dsts)
    assert "AGENTS.md" in dsts


def test_resolve_manifests_publisher_include():
    sel = Selection(depth="repo", harnesses=["claude"], owner="claude", includes=["publisher"])
    files, dirs, notes = resolve_manifests(sel)
    dsts = [f["dst"] for f in files]
    assert ".brigade/policies/public-content.json" in dsts
    assert ".brigade/scrub-cache" in dirs


def test_install_selection_writes_files(tmp_path):
    sel = Selection(depth="repo", harnesses=["claude"], owner="claude", includes=[])
    code = install_selection(tmp_path, sel)
    assert code == 0
    assert (tmp_path / "AGENTS.md").is_file()
    assert (tmp_path / "CLAUDE.md").is_file()
    assert (tmp_path / ".claude" / "memory-handoffs" / "TEMPLATE.md").is_file()
    assert (tmp_path / ".claude" / "memory-handoffs" / "processed").is_dir()


def test_install_selection_writes_config(tmp_path):
    from brigade.config import load_config
    sel = Selection(
        depth="workspace",
        harnesses=["claude", "codex", "openclaw"],
        owner="openclaw",
        includes=["publisher"],
    )
    install_selection(tmp_path, sel)
    cfg = load_config(tmp_path)
    assert cfg is not None
    assert cfg.selection.depth == "workspace"
    assert cfg.selection.harnesses == ["claude", "codex", "openclaw"]
    assert cfg.selection.owner == "openclaw"
    assert cfg.selection.includes == ["publisher"]


def test_opencode_install_creates_inbox_and_gitignore(tmp_path):
    from brigade.install import install_selection, build_gitignore_block
    from brigade.selection import Selection
    sel = Selection(depth="repo", harnesses=["opencode"], owner="opencode", includes=[])
    rc = install_selection(tmp_path, sel)
    assert rc == 0
    assert (tmp_path / ".opencode" / "memory-handoffs" / "TEMPLATE.md").is_file()
    assert (tmp_path / ".opencode" / "memory-handoffs" / "processed").is_dir()
    block = build_gitignore_block(sel)
    assert ".opencode/memory-handoffs/*" in block
    assert "!.opencode/memory-handoffs/TEMPLATE.md" in block


def test_install_selection_refuses_overwrite_without_force(tmp_path):
    sel = Selection(depth="repo", harnesses=["claude"], owner="claude", includes=[])
    install_selection(tmp_path, sel)
    code = install_selection(tmp_path, sel)
    assert code == 3  # matches existing init refuse-overwrite exit code
