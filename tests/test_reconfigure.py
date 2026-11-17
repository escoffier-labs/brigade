from brigade.install import install_selection
from brigade.selection import Selection
from brigade.reconfigure import reconfigure
from brigade.config import load_config


def test_reconfigure_adds_new_harness(tmp_path):
    sel = Selection(depth="repo", harnesses=["claude"], owner="claude", includes=[])
    install_selection(tmp_path, sel)

    new_sel = Selection(depth="repo", harnesses=["claude", "codex"], owner="claude", includes=[])
    rc = reconfigure(tmp_path, new_selection=new_sel, prune=False)
    assert rc == 0
    assert (tmp_path / ".codex" / "memory-handoffs" / "TEMPLATE.md").is_file()
    assert (tmp_path / ".claude" / "memory-handoffs" / "TEMPLATE.md").is_file()
    cfg = load_config(tmp_path)
    assert "codex" in cfg.selection.harnesses


def test_reconfigure_prune_removes_dropped_harness(tmp_path):
    sel = Selection(depth="repo", harnesses=["claude", "codex"], owner="claude", includes=[])
    install_selection(tmp_path, sel)
    assert (tmp_path / ".codex").is_dir()

    new_sel = Selection(depth="repo", harnesses=["claude"], owner="claude", includes=[])
    rc = reconfigure(tmp_path, new_selection=new_sel, prune=True)
    assert rc == 0
    assert not (tmp_path / ".codex").exists()
    assert (tmp_path / ".claude").is_dir()
    cfg = load_config(tmp_path)
    assert cfg.selection.harnesses == ["claude"]


def test_reconfigure_no_prune_leaves_orphan(tmp_path):
    sel = Selection(depth="repo", harnesses=["claude", "codex"], owner="claude", includes=[])
    install_selection(tmp_path, sel)

    new_sel = Selection(depth="repo", harnesses=["claude"], owner="claude", includes=[])
    rc = reconfigure(tmp_path, new_selection=new_sel, prune=False)
    assert rc == 0
    # Without --prune, codex inbox dir stays (will be flagged by doctor as orphan).
    assert (tmp_path / ".codex" / "memory-handoffs").is_dir()


def test_reconfigure_never_overwrites_a_wired_skill_md(tmp_path):
    sel = Selection(depth="repo", harnesses=["claude"], owner="claude", includes=[])
    install_selection(tmp_path, sel)
    skill = tmp_path / ".claude" / "skills" / "brigade-work" / "SKILL.md"
    skill.write_bytes(b"# edited by the operator\n")

    new_sel = Selection(depth="repo", harnesses=["claude", "codex"], owner="claude", includes=[])
    assert reconfigure(tmp_path, new_selection=new_sel, prune=False) == 0

    assert skill.read_bytes() == b"# edited by the operator\n"
    # the newly added harness still gets its missing skill
    assert (tmp_path / ".codex" / "skills" / "brigade-work" / "SKILL.md").is_file()
    assert len(list((tmp_path / ".brigade" / "skills" / "inbox").glob("*/proposal.json"))) == 1
