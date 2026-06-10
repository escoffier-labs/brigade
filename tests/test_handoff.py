"""Tests for brigade handoff-template."""

from __future__ import annotations

from pathlib import Path

from brigade import handoff as handoff_mod
from brigade.install import install_selection
from brigade.selection import Selection


def test_handoff_template_prints_packaged(capsys):
    rc = handoff_mod.run(target=None)
    assert rc == 0
    out = capsys.readouterr().out
    assert "# Memory Handoff" in out
    assert "Recommended memory action" in out


def test_handoff_template_prefers_local_install(tmp_target: Path, capsys):
    install_selection(
        tmp_target,
        Selection(depth="repo", harnesses=["claude"], owner="claude", includes=[]),
    )
    local = tmp_target / ".claude" / "memory-handoffs" / "TEMPLATE.md"
    local.write_text("# Local override\n")
    rc = handoff_mod.run(target=tmp_target)
    assert rc == 0
    out = capsys.readouterr().out
    assert "# Local override" in out
