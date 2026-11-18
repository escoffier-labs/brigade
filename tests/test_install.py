import hashlib
import json
import os
import shutil
import sys
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
    # An unedited copy whose text is not a recorded release text (see tests/test_refresh_pristine.py
    # for the refresh of a recorded one) is kept, labelled and proposed, as before.
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


# --- #1844 fix round: newlines, held-descriptor walk, dedupe, provenance, wired.json ------

POSIX_ONLY = pytest.mark.skipif(os.name != "posix", reason="needs POSIX symlinks and FIFOs")
WINDOWS_ONLY = pytest.mark.skipif(sys.platform != "win32", reason="needs Windows junctions")


def test_crlf_copy_of_the_bundled_skill_reads_as_identical(tmp_path, capsys):
    # A copy an older release wrote on Windows (write_text turned LF into CRLF).
    path = tmp_path / BRIGADE_WORK
    path.parent.mkdir(parents=True)
    crlf = _bundled_text("brigade-work").replace("\n", "\r\n").encode("utf-8")
    path.write_bytes(crlf)

    assert install_selection(tmp_path, _claude_sel()) == 0

    assert f"kept (identical): {path}" in capsys.readouterr().out
    assert path.read_bytes() == crlf
    assert _proposals(tmp_path) == []


def test_a_crlf_pristine_stale_copy_is_labelled_by_its_normalized_hash(tmp_path, capsys):
    # Unrecorded text again: labelled, not refreshed.
    assert install_selection(tmp_path, _claude_sel()) == 0
    old_lf = b"# older bundled brigade-work\n"
    (tmp_path / BRIGADE_WORK).write_bytes(old_lf.replace(b"\n", b"\r\n"))
    state = tmp_path / ".brigade" / "skills" / "wired.json"
    payload = json.loads(state.read_text())
    payload["skills"][BRIGADE_WORK.as_posix()]["sha256"] = hashlib.sha256(old_lf).hexdigest()
    state.write_text(json.dumps(payload))
    capsys.readouterr()

    assert install_selection(tmp_path, _claude_sel()) == 0

    assert f"kept (pristine-stale): {tmp_path / BRIGADE_WORK}" in capsys.readouterr().out


def test_the_written_skill_is_lf_on_every_platform(tmp_path):
    assert install_selection(tmp_path, _claude_sel()) == 0
    assert b"\r" not in (tmp_path / BRIGADE_WORK).read_bytes()


def _install_open_hook(monkeypatch, hook) -> None:
    # dirfd only trusts descriptor-relative opens when os.open is in supports_dir_fd.
    monkeypatch.setattr(os, "open", hook)
    monkeypatch.setattr(os, "supports_dir_fd", set(os.supports_dir_fd) | {hook})


def _swap_parent_when_skill_is_created(monkeypatch, parent: Path, outside: Path) -> list[bool]:
    """Replace ``parent`` with a link to ``outside`` the instant SKILL.md is created."""
    real_open = os.open
    swapped: list[bool] = []

    def racing_open(path, flags, *args, **kwargs):
        if flags & os.O_EXCL and "SKILL.md" in str(path) and not swapped:
            swapped.append(True)
            parent.rename(parent.with_name(parent.name + ".moved"))
            parent.symlink_to(outside, target_is_directory=True)
        return real_open(path, flags, *args, **kwargs)

    _install_open_hook(monkeypatch, racing_open)
    return swapped


@POSIX_ONLY
def test_a_parent_swapped_for_a_link_mid_create_cannot_redirect_the_write(tmp_path, monkeypatch):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    ws = tmp_path / "ws"
    swapped = _swap_parent_when_skill_is_created(monkeypatch, ws / ".claude" / "skills" / "brigade-work", outside)
    (ws / ".claude" / "skills").mkdir(parents=True)

    assert install_selection(ws, _claude_sel()) == 0

    assert swapped, "the race hook never fired"
    assert list(outside.iterdir()) == []


@POSIX_ONLY
def test_a_skill_md_swapped_for_a_fifo_mid_read_does_not_hang(tmp_path, monkeypatch, capsys):
    import signal

    assert install_selection(tmp_path, _claude_sel()) == 0
    skill = tmp_path / BRIGADE_WORK
    real_open = os.open
    swapped: list[bool] = []

    def racing_open(path, flags, *args, **kwargs):
        if str(path).endswith("SKILL.md") and not flags & os.O_CREAT and not swapped:
            swapped.append(True)
            skill.unlink()
            os.mkfifo(skill)
        return real_open(path, flags, *args, **kwargs)

    def hung(*_args):
        raise AssertionError("reading the destination blocked on a FIFO")

    _install_open_hook(monkeypatch, racing_open)
    previous = signal.signal(signal.SIGALRM, hung)
    signal.alarm(10)
    capsys.readouterr()
    try:
        assert install_selection(tmp_path, _claude_sel()) == 0
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)

    assert swapped
    assert f"kept (not-a-regular-file): {skill}" in capsys.readouterr().out
    assert _proposals(tmp_path) == []


@pytest.mark.parametrize("error", [FileNotFoundError, NotADirectoryError])
def test_a_directory_race_is_kept_and_never_written(tmp_path, monkeypatch, capsys, error):
    from brigade import dirfd

    def lost_race(*_args, **_kwargs):
        raise error()

    monkeypatch.setattr(dirfd, "open_child_directory", lost_race)
    monkeypatch.setattr(dirfd, "mkdir_child", lost_race)

    assert install_selection(tmp_path, _claude_sel()) == 0

    assert "kept (not-a-regular-file)" in capsys.readouterr().out
    assert not (tmp_path / ".claude" / "skills").exists()


def test_a_reparse_refusal_is_kept_as_a_link(tmp_path, monkeypatch, capsys):
    from brigade import dirfd

    def refused(_error):
        return True

    def refuse(*_args, **_kwargs):
        raise OSError("reparse point refused")

    monkeypatch.setattr(dirfd, "is_reparse_point_refusal", refused)
    monkeypatch.setattr(dirfd, "open_child_directory", refuse)
    monkeypatch.setattr(dirfd, "mkdir_child", refuse)

    assert install_selection(tmp_path, _claude_sel()) == 0

    assert "kept (symlinked)" in capsys.readouterr().out
    assert _proposals(tmp_path) == []


@WINDOWS_ONLY
def test_a_junctioned_parent_directory_is_kept(tmp_path, capsys):
    import subprocess

    outside = tmp_path / "elsewhere"
    outside.mkdir()
    ws = tmp_path / "ws"
    (ws / ".claude").mkdir(parents=True)
    subprocess.run(["cmd", "/c", "mklink", "/J", str(ws / ".claude" / "skills"), str(outside)], check=True)

    assert install_selection(ws, _claude_sel(), force=True) == 0

    assert list(outside.iterdir()) == []
    assert "kept (symlinked)" in capsys.readouterr().out


@POSIX_ONLY
def test_the_same_edit_in_two_harnesses_files_two_proposals(tmp_path, capsys):
    sel = Selection(depth="repo", harnesses=["claude", "codex"], owner="claude", includes=[])
    assert install_selection(tmp_path, sel) == 0
    claude = tmp_path / ".claude" / "skills" / "brigade-work" / "SKILL.md"
    codex = tmp_path / ".codex" / "skills" / "brigade-work" / "SKILL.md"
    claude.write_bytes(b"# mine in claude\n")
    codex.write_bytes(b"# mine in codex\n")

    assert install_selection(tmp_path, sel) == 0
    proposals = _proposals(tmp_path)
    assert len(proposals) == 2
    assert len({p["dedupe_key"] for p in proposals}) == 2

    # Rejecting one must not mute the other destination on the next run.
    from brigade import skills_cmd

    victim = next(p for p in proposals if ".claude/" in p["dedupe_key"])
    keeper = next(p for p in proposals if ".codex/" in p["dedupe_key"])
    assert skills_cmd.inbox_reject(target=tmp_path, proposal_id=victim["proposal_id"], reason="no") == 0
    capsys.readouterr()

    # The codex destination loses its proposal entirely, so a refiling is observable.
    shutil.rmtree(tmp_path / ".brigade" / "skills" / "inbox" / keeper["proposal_id"])
    assert [p["proposal_id"] for p in _proposals(tmp_path)] == [victim["proposal_id"]]

    assert install_selection(tmp_path, sel) == 0

    after = {p["dedupe_key"]: p["proposal_id"] for p in _proposals(tmp_path)}
    assert keeper["dedupe_key"] in after, "the rejected claude proposal muted the codex destination"
    assert after[victim["dedupe_key"]] == victim["proposal_id"]  # the rejected one stays rejected, not refiled
    assert len(after) == 2


def test_a_proposal_records_stable_provenance_not_the_staging_dir(tmp_path):
    assert install_selection(tmp_path, _claude_sel()) == 0
    (tmp_path / BRIGADE_WORK).write_bytes(b"# mine\n")
    assert install_selection(tmp_path, _claude_sel()) == 0

    (proposal,) = _proposals(tmp_path)
    assert proposal["source"] == f"brigade://bundled-skills/brigade-work@{brigade_version}"

    from brigade import skills_cmd

    assert skills_cmd.inbox_accept(target=tmp_path, proposal_id=proposal["proposal_id"], force=True) == 0
    accepted = _proposals(tmp_path)[0]
    assert "brigade-init-skill-" not in json.dumps(accepted)


def test_a_corrupt_wired_json_is_backed_up_not_silently_dropped(tmp_path, capsys):
    state = tmp_path / ".brigade" / "skills" / "wired.json"
    state.parent.mkdir(parents=True)
    state.write_bytes(b"{not json")

    assert install_selection(tmp_path, _claude_sel()) == 0

    err = capsys.readouterr().err
    assert "wired.json was unreadable" in err
    backups = list(state.parent.glob("wired.json.corrupt-*"))
    assert [b.read_bytes() for b in backups] == [b"{not json"]
    assert BRIGADE_WORK.as_posix() in _wired(tmp_path)


def test_a_second_wirer_re_reads_wired_json_in_finish_and_keeps_the_first_records(tmp_path):
    from brigade.skill_wiring import SkillWirer

    (tmp_path / ".brigade").mkdir()
    first, second = SkillWirer(tmp_path), SkillWirer(tmp_path)
    template_dir = template_root() / "skills" / "brigade-work"
    text = _bundled_text("brigade-work")
    first.place(
        (".claude", "skills", "brigade-work", "SKILL.md"), template_dir, text, skill_id="brigade-work", harness="claude"
    )
    second.place(
        (".codex", "skills", "brigade-work", "SKILL.md"), template_dir, text, skill_id="brigade-work", harness="codex"
    )

    first.finish()
    second.finish()

    assert set(_wired(tmp_path)) == {".claude/skills/brigade-work/SKILL.md", ".codex/skills/brigade-work/SKILL.md"}


# --- #1844 fix round 2: atomic publish and non-race errors ---------------------------------


def test_a_failed_write_leaves_no_skill_md_and_the_next_install_writes_the_bundled_text(tmp_path, monkeypatch, capsys):
    import errno

    from brigade import skill_wiring

    real_fdopen = os.fdopen
    bundled = _bundled_text("brigade-work").encode("utf-8")

    class FullDisk:
        def __init__(self, handle):
            self._handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._handle.close()

        def __getattr__(self, name):
            return getattr(self._handle, name)

        def write(self, data):
            if data != bundled:  # fdopen is process-wide: fail only the SKILL.md write
                return self._handle.write(data)
            raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(skill_wiring.os, "fdopen", lambda fd, *a, **k: FullDisk(real_fdopen(fd, *a, **k)))

    assert install_selection(tmp_path, _claude_sel()) == 0

    err = capsys.readouterr().err
    assert "could not place" in err and str(tmp_path / BRIGADE_WORK) in err and f"errno {errno.ENOSPC}" in err
    assert list((tmp_path / ".claude" / "skills" / "brigade-work").iterdir()) == [], (
        "a truncated SKILL.md or temp file was left"
    )
    wired_json = tmp_path / ".brigade" / "skills" / "wired.json"
    assert not wired_json.exists() or BRIGADE_WORK.as_posix() not in _wired(tmp_path)

    monkeypatch.undo()
    capsys.readouterr()
    assert install_selection(tmp_path, _claude_sel()) == 0
    assert (tmp_path / BRIGADE_WORK).read_bytes() == _bundled_text("brigade-work").encode("utf-8")
    assert "kept (edited)" not in capsys.readouterr().out
    assert _proposals(tmp_path) == []


def test_an_interrupt_mid_write_leaves_no_skill_md(tmp_path, monkeypatch):
    from brigade import skill_wiring

    real_fdopen = os.fdopen
    bundled = _bundled_text("brigade-work").encode("utf-8")

    class Interrupted:
        def __init__(self, handle):
            self._handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._handle.close()

        def __getattr__(self, name):
            return getattr(self._handle, name)

        def write(self, data):
            if data != bundled:  # fdopen is process-wide: interrupt only the SKILL.md write
                return self._handle.write(data)
            self._handle.write(data[:10])  # a partial write reaches the temp file, then Ctrl-C
            self._handle.flush()
            raise KeyboardInterrupt

    monkeypatch.setattr(skill_wiring.os, "fdopen", lambda fd, *a, **k: Interrupted(real_fdopen(fd, *a, **k)))

    with pytest.raises(KeyboardInterrupt):
        install_selection(tmp_path, _claude_sel())

    assert list((tmp_path / ".claude" / "skills" / "brigade-work").iterdir()) == []


@POSIX_ONLY
def test_a_name_taken_during_the_publish_is_kept_and_not_overwritten(tmp_path, monkeypatch, capsys):
    from brigade import dirfd

    real_replace = dirfd.replace_children
    theirs = b"# written by another process\n"

    def lose_the_race(parent, source, destination, **kwargs):
        if destination == "SKILL.md":
            fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644, dir_fd=parent)
            with os.fdopen(fd, "wb") as handle:
                handle.write(theirs)
        return real_replace(parent, source, destination, **kwargs)

    monkeypatch.setattr(dirfd, "replace_children", lose_the_race)

    assert install_selection(tmp_path, _claude_sel()) == 0

    assert (tmp_path / BRIGADE_WORK).read_bytes() == theirs
    leftovers = [p.name for p in (tmp_path / ".claude" / "skills" / "brigade-work").iterdir()]
    assert leftovers == ["SKILL.md"], "the temp sibling was left behind"
    assert "kept (not-a-regular-file)" in capsys.readouterr().out


def _assert_init_went_on(tmp_path: Path, captured, *, denied: Path, written: list[Path]) -> None:
    import errno

    assert "could not place" in captured.err and f"errno {errno.EACCES}" in captured.err
    assert str(denied) in captured.err
    assert not denied.exists()
    wired = _wired(tmp_path)
    for path in written:
        assert path.read_bytes() == _bundled_text(path.parent.name).encode("utf-8")
        assert path.relative_to(tmp_path).as_posix() in wired  # finish() still recorded it
    assert denied.relative_to(tmp_path).as_posix() not in wired
    assert (tmp_path / ".brigade" / "config.json").is_file()  # the rest of init ran


def test_a_permission_error_creating_a_directory_keeps_going(tmp_path, monkeypatch, capsys):
    import errno

    from brigade import dirfd

    sel = Selection(depth="repo", harnesses=["claude", "codex"], owner="claude", includes=[])
    real = dirfd.mkdir_child

    seen: list[str] = []

    def deny_second_brigade_work(parent, name, *args, **kwargs):
        if name == "brigade-work":
            seen.append(name)
            if len(seen) == 2:  # claude's directory is made; codex's is denied
                raise PermissionError(errno.EACCES, "Permission denied")
        return real(parent, name, *args, **kwargs)

    monkeypatch.setattr(dirfd, "mkdir_child", deny_second_brigade_work)

    assert install_selection(tmp_path, sel) == 0

    skills = tmp_path / ".claude" / "skills"
    _assert_init_went_on(
        tmp_path,
        capsys.readouterr(),
        denied=tmp_path / ".codex" / "skills" / "brigade-work" / "SKILL.md",
        written=[
            skills / "brigade-work" / "SKILL.md",
            skills / "ultra-work-scout" / "SKILL.md",
            tmp_path / ".codex" / "skills" / "ultra-work-scout" / "SKILL.md",
        ],
    )


def test_a_permission_error_creating_the_file_keeps_going(tmp_path, monkeypatch, capsys):
    import errno

    from brigade import dirfd

    sel = Selection(depth="repo", harnesses=["claude", "codex"], owner="claude", includes=[])
    real = dirfd.open_child_file
    creates: list[str] = []

    def deny_second_create(parent, name, *args, **kwargs):
        if ".SKILL.md." in name:
            creates.append(name)
            if len(creates) == 2:  # claude brigade-work passes; codex brigade-work is denied
                raise PermissionError(errno.EACCES, "Permission denied")
        return real(parent, name, *args, **kwargs)

    monkeypatch.setattr(dirfd, "open_child_file", deny_second_create)

    assert install_selection(tmp_path, sel) == 0

    assert len(creates) == 4, "later skills were skipped after the error"
    _assert_init_went_on(
        tmp_path,
        capsys.readouterr(),
        denied=tmp_path / ".codex" / "skills" / "brigade-work" / "SKILL.md",
        written=[
            tmp_path / ".claude" / "skills" / "brigade-work" / "SKILL.md",
            tmp_path / ".codex" / "skills" / "ultra-work-scout" / "SKILL.md",
        ],
    )


def test_an_io_error_while_reading_an_existing_skill_is_kept_not_fatal(tmp_path, monkeypatch, capsys):
    import errno

    from brigade import skill_wiring

    assert install_selection(tmp_path, _claude_sel()) == 0
    capsys.readouterr()

    def eio(_fd, _size):
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(skill_wiring.os, "read", eio)

    assert install_selection(tmp_path, _claude_sel()) == 0

    captured = capsys.readouterr()
    assert f"errno {errno.EIO}" in captured.err
    assert "kept (not-a-regular-file)" in captured.out
    assert (tmp_path / BRIGADE_WORK).read_bytes() == _bundled_text("brigade-work").encode("utf-8")


def test_two_wirers_in_threads_never_lose_a_record(tmp_path):
    import threading

    from brigade.skill_wiring import SkillWirer

    (tmp_path / ".brigade").mkdir()
    template_dir = template_root() / "skills" / "brigade-work"
    text = _bundled_text("brigade-work")
    harnesses = [".claude", ".codex", ".cursor", ".gemini", ".opencode", ".pi"]
    barrier = threading.Barrier(len(harnesses))
    errors: list[BaseException] = []

    def run(prefix: str) -> None:
        try:
            wirer = SkillWirer(tmp_path)
            wirer.place(
                (prefix, "skills", "brigade-work", "SKILL.md"),
                template_dir,
                text,
                skill_id="brigade-work",
                harness=prefix.lstrip("."),
            )
            barrier.wait(timeout=10)
            wirer.finish()
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(h,)) for h in harnesses]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert set(_wired(tmp_path)) == {f"{h}/skills/brigade-work/SKILL.md" for h in harnesses}
