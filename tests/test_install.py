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


def test_antigravity_install_creates_inbox_and_gitignore(tmp_path):
    from brigade.install import install_selection, build_gitignore_block
    from brigade.selection import Selection
    sel = Selection(depth="repo", harnesses=["antigravity"], owner="antigravity", includes=[])
    rc = install_selection(tmp_path, sel)
    assert rc == 0
    assert (tmp_path / ".antigravity" / "memory-handoffs" / "TEMPLATE.md").is_file()
    assert (tmp_path / ".antigravity" / "memory-handoffs" / "processed").is_dir()
    block = build_gitignore_block(sel)
    assert ".antigravity/memory-handoffs/*" in block
    assert "!.antigravity/memory-handoffs/TEMPLATE.md" in block


def test_pi_install_creates_inbox_and_gitignore(tmp_path):
    from brigade.install import install_selection, build_gitignore_block
    from brigade.selection import Selection
    sel = Selection(depth="repo", harnesses=["pi"], owner="pi", includes=[])
    rc = install_selection(tmp_path, sel)
    assert rc == 0
    assert (tmp_path / ".pi" / "memory-handoffs" / "TEMPLATE.md").is_file()
    assert (tmp_path / ".pi" / "memory-handoffs" / "processed").is_dir()
    block = build_gitignore_block(sel)
    assert ".pi/memory-handoffs/*" in block
    assert "!.pi/memory-handoffs/TEMPLATE.md" in block


def test_cursor_install_creates_inbox_and_gitignore(tmp_path):
    from brigade.install import install_selection, build_gitignore_block
    from brigade.selection import Selection
    sel = Selection(depth="repo", harnesses=["cursor"], owner="cursor", includes=[])
    rc = install_selection(tmp_path, sel)
    assert rc == 0
    assert (tmp_path / ".cursor" / "memory-handoffs" / "TEMPLATE.md").is_file()
    assert (tmp_path / ".cursor" / "memory-handoffs" / "processed").is_dir()
    block = build_gitignore_block(sel)
    assert ".cursor/memory-handoffs/*" in block
    assert "!.cursor/memory-handoffs/TEMPLATE.md" in block


@pytest.mark.parametrize("harness", ["aider", "goose", "continue", "copilot", "qwen", "kimi", "adal", "openhands"])
def test_expanded_cli_harness_install_creates_inbox_and_gitignore(tmp_path, harness):
    from brigade.install import install_selection, build_gitignore_block
    from brigade.selection import Selection

    sel = Selection(depth="repo", harnesses=[harness], owner=harness, includes=[])
    rc = install_selection(tmp_path, sel)
    assert rc == 0
    assert (tmp_path / f".{harness}" / "memory-handoffs" / "TEMPLATE.md").is_file()
    assert (tmp_path / f".{harness}" / "memory-handoffs" / "processed").is_dir()
    block = build_gitignore_block(sel)
    assert f".{harness}/memory-handoffs/*" in block
    assert f"!.{harness}/memory-handoffs/TEMPLATE.md" in block


def test_hermes_install_creates_adapter_inbox_and_gitignore(tmp_path):
    import json

    from brigade.install import build_gitignore_block, install_selection
    from brigade.selection import Selection

    sel = Selection(depth="workspace", harnesses=["hermes"], owner="hermes", includes=[])
    rc = install_selection(tmp_path, sel)
    assert rc == 0
    assert (tmp_path / ".hermes" / "memory-handoffs" / "TEMPLATE.md").is_file()
    assert (tmp_path / ".hermes" / "memory-handoffs" / "processed").is_dir()
    assert (tmp_path / ".brigade" / "hermes" / "README.md").is_file()
    workspace = json.loads((tmp_path / ".brigade" / "hermes" / "workspace.harness.json").read_text())
    handoff = json.loads((tmp_path / ".brigade" / "hermes" / "memory-handoff.harness.json").read_text())
    assert workspace["workspace"]["handoff_inbox"] == ".hermes/memory-handoffs"
    assert handoff["memory_handoff"]["inbox_dir"] == ".hermes/memory-handoffs"
    assert handoff["memory_handoff"]["processed_dir"] == ".hermes/memory-handoffs/processed"
    assert ".claude/memory-handoffs" not in json.dumps({"workspace": workspace, "handoff": handoff})
    block = build_gitignore_block(sel)
    assert ".hermes/memory-handoffs/*" in block
    assert "!.hermes/memory-handoffs/TEMPLATE.md" in block


def test_install_selection_refuses_overwrite_without_force(tmp_path):
    sel = Selection(depth="repo", harnesses=["claude"], owner="claude", includes=[])
    install_selection(tmp_path, sel)
    code = install_selection(tmp_path, sel)
    assert code == 3  # matches existing init refuse-overwrite exit code


def test_install_renders_selected_writer_inboxes_in_agent_docs(tmp_path):
    sel = Selection(depth="repo", harnesses=["codex"], owner="codex", includes=[])
    assert install_selection(tmp_path, sel) == 0
    agents = (tmp_path / "AGENTS.md").read_text()
    assert ".codex/memory-handoffs/" in agents
    assert ".claude/memory-handoffs" not in agents
    assert "~/.openclaw/workspace" not in agents
    install_doc = (tmp_path / "INSTALL_FOR_AGENTS.md").read_text()
    assert ".codex/memory-handoffs/" in install_doc
    assert ".claude/memory-handoffs" not in install_doc


def test_install_renders_all_selected_writer_inboxes(tmp_path):
    sel = Selection(depth="repo", harnesses=["claude", "codex"], owner="claude", includes=[])
    assert install_selection(tmp_path, sel) == 0
    agents = (tmp_path / "AGENTS.md").read_text()
    assert ".claude/memory-handoffs/" in agents
    assert ".codex/memory-handoffs/" in agents
    assert "~/.openclaw/workspace" not in agents


def test_install_workspace_owner_without_writer_inbox_uses_writer_harness(tmp_path):
    sel = Selection(depth="workspace", harnesses=["openclaw", "hermes"], owner="openclaw", includes=[])
    assert install_selection(tmp_path, sel) == 0
    agents = (tmp_path / "AGENTS.md").read_text()
    assert ".hermes/memory-handoffs/" in agents
    assert ".claude/memory-handoffs" not in agents
    card = (tmp_path / "memory" / "cards" / "handoff-flow.md").read_text()
    assert ".hermes/memory-handoffs/" in card
    assert ".claude/memory-handoffs" not in card
    assert "{{" not in card
