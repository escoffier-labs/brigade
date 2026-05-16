from solo_mise.install import install_selection
from solo_mise.selection import Selection
from solo_mise.reconfigure import reconfigure
from solo_mise.config import load_config


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
