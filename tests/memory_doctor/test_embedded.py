"""Tests for memory-doctor verbs embedded in brigade."""

from __future__ import annotations

from pathlib import Path

from brigade.memory_doctor.lint import run as lint_run, scan_dead_links
from brigade.memory_doctor.paths import PathConfig
from brigade.memory_doctor.status import collect_status
from brigade.memory_doctor.compact import plan_compaction


def _cfg(memory_dir: Path, handoffs_dir: Path) -> PathConfig:
    return PathConfig(memory_dir=memory_dir, handoffs_dir=handoffs_dir, max_lines=180, max_bytes=24000)


def test_status_counts_cards_and_index(tmp_path: Path):
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "MEMORY.md").write_text("# Index\n- [a](a.md) hook\n")
    (mem / "a.md").write_text("body\n")
    hand = tmp_path / "handoffs"
    hand.mkdir()
    s = collect_status(_cfg(mem, hand))
    assert s.cards == 1
    assert s.memory_index_lines >= 1
    assert s.dead_links == 0


def test_status_counts_cards_subdir(tmp_path: Path):
    mem = tmp_path / "memory"
    cards = mem / "cards"
    cards.mkdir(parents=True)
    (mem / "MEMORY.md").write_text("# Index\n")
    (cards / "topic.md").write_text("body\n")
    hand = tmp_path / "handoffs"
    hand.mkdir()
    s = collect_status(_cfg(mem, hand))
    assert s.cards == 1


def test_lint_finds_dead_wiki_link(tmp_path: Path):
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "alpha.md").write_text("see [[beta]] for details\n")
    findings = scan_dead_links(mem)
    assert len(findings) == 1
    assert findings[0].link == "beta"


def test_lint_exit_codes(tmp_path: Path, capsys):
    mem = tmp_path / "memory"
    mem.mkdir()
    hand = tmp_path / "handoffs"
    hand.mkdir()
    (mem / "alpha.md").write_text("see [[nope]]\n")
    assert lint_run(_cfg(mem, hand)) == 1
    (mem / "nope.md").write_text("ok\n")
    assert lint_run(_cfg(mem, hand)) == 0


def test_compact_plans_flatten(tmp_path: Path):
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "topic.md").write_text("# Topic\n\n")
    # Multi-line MEMORY.md entry: bullet + indented continuation
    lines = [
        "# Memory",
        "- [Topic](topic.md) short hook",
        "  detail line one",
        "  detail line two",
    ]
    (mem / "MEMORY.md").write_text("\n".join(lines) + "\n")
    plan = plan_compaction(mem, max_lines=2, max_hook_chars=140)
    assert plan.original_lines >= 3
    assert plan.flattens or plan.projected_lines <= plan.original_lines


def test_cli_memory_status_json(tmp_path: Path, capsys):
    from brigade import memory_doctor_cmd

    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "MEMORY.md").write_text("x\n")
    hand = tmp_path / "handoffs"
    hand.mkdir()
    rc = memory_doctor_cmd.status(memory_dir=str(mem), handoffs_dir=str(hand), json_output=True)
    assert rc == 0
    out = capsys.readouterr().out
    assert '"cards"' in out
