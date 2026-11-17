import hashlib
import json
from pathlib import Path

import pytest
from brigade import __version__ as brigade_version
from brigade import install as install_mod
from brigade.templates import template_root
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
    assert ".brigade/receipt-signing-key" in block
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


@pytest.mark.parametrize(
    "harness", ["aider", "goose", "continue", "copilot", "qwen", "kimi", "adal", "openhands", "grok", "amp", "crush"]
)
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


def test_install_selection_reinstall_keeps_existing_and_rewires(tmp_path):
    # Re-running install (brownfield / upgrade) keeps existing files instead of
    # clobbering them, succeeds (rc 0), and re-wires the additive built-in skills.
    sel = Selection(depth="repo", harnesses=["claude"], owner="claude", includes=[])
    assert install_selection(tmp_path, sel) == 0
    agents_before = (tmp_path / "AGENTS.md").read_text()
    (tmp_path / "AGENTS.md").write_text(agents_before + "\n# local edit\n")
    code = install_selection(tmp_path, sel)
    assert code == 0
    # local edit preserved (no clobber without --force)
    assert "# local edit" in (tmp_path / "AGENTS.md").read_text()
    # built-in skills still wired
    assert (tmp_path / ".claude" / "skills" / "brigade-work" / "SKILL.md").is_file()
    assert (tmp_path / ".claude" / "skills" / "ultra-work-scout" / "SKILL.md").is_file()


def test_install_warns_when_default_wired_skill_template_is_missing(tmp_path, monkeypatch, capsys):
    sel = Selection(depth="repo", harnesses=["claude"], owner="claude", includes=[])
    monkeypatch.setattr(install_mod, "DEFAULT_WIRED_SKILLS", ("missing-default-skill",))

    code = install_selection(tmp_path, sel)

    captured = capsys.readouterr()
    assert code == 0
    assert "warning: built-in skill template missing" in captured.err
    assert "missing-default-skill" in captured.err


def test_install_renders_selected_writer_inboxes_in_agent_docs(tmp_path):
    sel = Selection(depth="repo", harnesses=["codex"], owner="codex", includes=["repo-extras"])
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


WORKSPACE_ONLY_REFS = (
    "SOUL.md",
    "USER.md",
    "MEMORY.md",
    "TOOLS.md",
    "IDENTITY.md",
    "HEARTBEAT.md",
    "memory/cards/",
)


def test_repo_depth_bootstrap_docs_reference_only_installed_files(tmp_path):
    # Repo-depth installs must not point agents at workspace-only files
    # that the install never creates (audit 2026-07-02, backlog item 1).
    sel = Selection(depth="repo", harnesses=["claude", "codex"], owner="claude", includes=["repo-extras"])
    assert install_selection(tmp_path, sel) == 0
    for doc in ("AGENTS.md", "INSTALL_FOR_AGENTS.md", "CLAUDE.md"):
        text = (tmp_path / doc).read_text()
        for ref in WORKSPACE_ONLY_REFS:
            assert ref not in text, f"{doc} references workspace-only {ref}"
        assert "{{" not in text, f"{doc} has unrendered placeholders"
    agents = (tmp_path / "AGENTS.md").read_text()
    assert ".claude/memory-handoffs/" in agents
    assert ".codex/memory-handoffs/" in agents
    assert "Memory Handoff" in agents
    assert "brigade work brief" in agents


def test_workspace_depth_keeps_workspace_bootstrap_docs(tmp_path):
    sel = Selection(depth="workspace", harnesses=["claude"], owner="claude", includes=[])
    assert install_selection(tmp_path, sel) == 0
    assert "SOUL.md" in (tmp_path / "AGENTS.md").read_text()
    assert "SOUL.md" in (tmp_path / "CLAUDE.md").read_text()
    assert (tmp_path / "SOUL.md").is_file()


# --- #1844: init never overwrites a wired SKILL.md -------------------------------

BRIGADE_WORK = Path(".claude/skills/brigade-work/SKILL.md")


def _claude_sel() -> Selection:
    return Selection(depth="repo", harnesses=["claude"], owner="claude", includes=[])


def _bundled_text(skill_id: str) -> str:
    return (template_root() / "skills" / skill_id / "SKILL.md").read_text(encoding="utf-8")


def _proposals(target: Path) -> list[dict]:
    inbox = target / ".brigade" / "skills" / "inbox"
    return [json.loads(p.read_text()) for p in sorted(inbox.glob("*/proposal.json"))]


def _wired(target: Path) -> dict:
    return json.loads((target / ".brigade" / "skills" / "wired.json").read_text())["skills"]


def test_missing_skill_md_is_written_and_recorded(tmp_path):
    assert install_selection(tmp_path, _claude_sel()) == 0
    installed = (tmp_path / BRIGADE_WORK).read_bytes()
    assert installed == _bundled_text("brigade-work").encode("utf-8")
    record = _wired(tmp_path)[BRIGADE_WORK.as_posix()]
    digest = hashlib.sha256(installed).hexdigest()
    assert record == {
        "harness": "claude",
        "skill_id": "brigade-work",
        "sha256": digest,
        "brigade_version": brigade_version,
        "template_sha256": digest,
    }
    assert _proposals(tmp_path) == []


def test_identical_skill_md_is_skipped_without_proposal(tmp_path, capsys):
    assert install_selection(tmp_path, _claude_sel()) == 0
    capsys.readouterr()
    before = (tmp_path / BRIGADE_WORK).stat().st_mtime_ns
    assert install_selection(tmp_path, _claude_sel()) == 0
    assert f"kept (identical): {tmp_path / BRIGADE_WORK}" in capsys.readouterr().out
    assert (tmp_path / BRIGADE_WORK).stat().st_mtime_ns == before
    assert _proposals(tmp_path) == []


def test_edited_skill_md_is_kept_and_proposed_once(tmp_path, capsys):
    assert install_selection(tmp_path, _claude_sel()) == 0
    edited = b"# my local skill\n"
    (tmp_path / BRIGADE_WORK).write_bytes(edited)
    capsys.readouterr()

    assert install_selection(tmp_path, _claude_sel()) == 0
    out = capsys.readouterr().out
    assert (tmp_path / BRIGADE_WORK).read_bytes() == edited
    (proposal,) = _proposals(tmp_path)
    assert f"kept (edited): {tmp_path / BRIGADE_WORK}" in out
    assert proposal["proposal_id"] in out
    assert proposal["skill_id"] == "brigade-work"
    assert proposal["behavioral_contract"] is None
    assert proposal["contract_status"] == "incomplete"
    assert proposal["dedupe_key"].startswith("init:brigade-work:")
    proposed = Path(proposal["skill_path"]) / "SKILL.md"
    assert proposed.read_text(encoding="utf-8") == _bundled_text("brigade-work")

    # A second init is idempotent: same proposal id, nothing new.
    assert install_selection(tmp_path, _claude_sel()) == 0
    assert proposal["proposal_id"] in capsys.readouterr().out
    assert [p["proposal_id"] for p in _proposals(tmp_path)] == [proposal["proposal_id"]]


def test_force_never_overwrites_skill_md_but_still_overwrites_other_files(tmp_path):
    assert install_selection(tmp_path, _claude_sel()) == 0
    (tmp_path / BRIGADE_WORK).write_bytes(b"# mine\n")
    (tmp_path / "AGENTS.md").write_text("# pre-existing\n")

    assert install_selection(tmp_path, _claude_sel(), force=True) == 0

    assert (tmp_path / BRIGADE_WORK).read_bytes() == b"# mine\n"
    assert "# pre-existing" not in (tmp_path / "AGENTS.md").read_text()
    assert len(_proposals(tmp_path)) == 1


def test_stale_but_unedited_copy_is_labelled_pristine_stale(tmp_path, capsys):
    assert install_selection(tmp_path, _claude_sel()) == 0
    # Simulate an older bundled template: the installed copy is what init once wrote.
    old = b"# older bundled brigade-work\n"
    (tmp_path / BRIGADE_WORK).write_bytes(old)
    state = tmp_path / ".brigade" / "skills" / "wired.json"
    payload = json.loads(state.read_text())
    payload["skills"][BRIGADE_WORK.as_posix()]["sha256"] = hashlib.sha256(old).hexdigest()
    state.write_text(json.dumps(payload))
    capsys.readouterr()

    assert install_selection(tmp_path, _claude_sel()) == 0

    assert f"kept (pristine-stale): {tmp_path / BRIGADE_WORK}" in capsys.readouterr().out
    assert (tmp_path / BRIGADE_WORK).read_bytes() == old
    assert len(_proposals(tmp_path)) == 1


def test_symlinked_skill_md_is_untouched_and_not_proposed(tmp_path, capsys):
    outside = tmp_path / "elsewhere" / "SKILL.md"
    outside.parent.mkdir()
    outside.write_text("# lives elsewhere\n")
    link = tmp_path / "ws" / BRIGADE_WORK
    link.parent.mkdir(parents=True)
    link.symlink_to(outside)

    assert install_selection(tmp_path / "ws", _claude_sel(), force=True) == 0

    assert link.is_symlink()
    assert outside.read_text() == "# lives elsewhere\n"
    assert f"kept (symlinked): {link}" in capsys.readouterr().out
    assert _proposals(tmp_path / "ws") == []


def test_dangling_symlinked_skill_md_is_not_written_through(tmp_path):
    missing = tmp_path / "elsewhere" / "SKILL.md"
    link = tmp_path / "ws" / BRIGADE_WORK
    link.parent.mkdir(parents=True)
    (tmp_path / "elsewhere").mkdir()
    link.symlink_to(missing)

    assert install_selection(tmp_path / "ws", _claude_sel()) == 0

    assert not missing.exists()
    assert link.is_symlink()


@pytest.mark.parametrize("linked", [".claude/skills", ".claude/skills/brigade-work"])
def test_symlinked_parent_directory_is_untouched_and_not_proposed(tmp_path, capsys, linked):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    ws = tmp_path / "ws"
    (ws / linked).parent.mkdir(parents=True)
    (ws / linked).symlink_to(outside, target_is_directory=True)

    assert install_selection(ws, _claude_sel(), force=True) == 0

    assert list(outside.iterdir()) == []
    assert "kept (symlinked)" in capsys.readouterr().out
    assert _proposals(ws) == []


def test_missing_template_writes_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(install_mod, "DEFAULT_WIRED_SKILLS", ("missing-default-skill",))
    assert install_selection(tmp_path, _claude_sel()) == 0
    assert "warning: built-in skill template missing" in capsys.readouterr().err
    assert not (tmp_path / ".claude" / "skills").exists()


def test_failed_wired_record_is_a_warning(tmp_path, monkeypatch, capsys):
    from brigade.skills_cmd import registry

    def refuse(*_args, **_kwargs):
        raise registry.SkillsStatePathError("state is not writable")

    monkeypatch.setattr(registry, "_write_state_file", refuse)
    assert install_selection(tmp_path, _claude_sel()) == 0
    assert "warning: could not record wired skills" in capsys.readouterr().err
    assert (tmp_path / BRIGADE_WORK).is_file()
