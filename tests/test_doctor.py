"""Tests for brigade doctor."""
from __future__ import annotations

from pathlib import Path
import json

import pytest

from brigade import doctor as doctor_mod
from brigade.install import install_selection
from brigade.selection import Selection


def test_doctor_passes_against_workspace_profile(tmp_target: Path, capsys):
    install_selection(
        tmp_target,
        Selection(depth="workspace", harnesses=["claude"], owner="claude", includes=[]),
    )
    rc = doctor_mod.run(target=tmp_target, harness="generic")
    assert rc == 0
    out = capsys.readouterr().out
    assert "[ok]" in out
    assert "[fail]" not in out


def test_doctor_reports_failures_on_empty_dir(tmp_target: Path, capsys):
    tmp_target.mkdir()
    rc = doctor_mod.run(target=tmp_target, harness="generic")
    assert rc == 1
    out = capsys.readouterr().out
    assert "[fail]" in out


def test_doctor_fails_when_bootstrap_file_exceeds_budget(tmp_target: Path, capsys):
    install_selection(
        tmp_target,
        Selection(depth="workspace", harnesses=["claude"], owner="claude", includes=[]),
    )
    limit = doctor_mod.BOOTSTRAP_BUDGETS["MEMORY.md"]
    (tmp_target / "MEMORY.md").write_text("x" * (limit + 1))

    rc = doctor_mod.run(target=tmp_target, harness="generic")
    out = capsys.readouterr().out
    assert rc == 1
    assert "[fail]" in out
    assert "bootstrap-budget: MEMORY.md" in out
    assert "over hard limit" in out


def test_doctor_reports_bootstrap_budget_ok(tmp_target: Path, capsys):
    install_selection(
        tmp_target,
        Selection(depth="workspace", harnesses=["claude"], owner="claude", includes=[]),
    )

    rc = doctor_mod.run(target=tmp_target, harness="generic")
    out = capsys.readouterr().out
    assert rc == 0
    assert "bootstrap-budget: AGENTS.md" in out
    assert "bootstrap-budget: MEMORY.md" in out


def test_doctor_openclaw_reports_manual_when_config_missing(tmp_target: Path, monkeypatch, capsys):
    install_selection(
        tmp_target,
        Selection(
            depth="workspace",
            harnesses=["claude", "openclaw"],
            owner="openclaw",
            includes=[],
        ),
    )
    monkeypatch.setenv("HOME", str(tmp_target))  # so ~/.openclaw resolves into the temp dir
    monkeypatch.setattr(Path, "home", lambda: tmp_target)
    rc = doctor_mod.run(target=tmp_target, harness="openclaw")
    out = capsys.readouterr().out
    assert "openclaw: config" in out
    # missing config is MANUAL, not FAIL -> exit 0
    assert rc == 0
    assert "[todo]" in out


def test_doctor_hermes_flags_experimental(tmp_target: Path, capsys):
    install_selection(
        tmp_target,
        Selection(depth="workspace", harnesses=["claude", "hermes"], owner="hermes", includes=[]),
    )
    rc = doctor_mod.run(target=tmp_target, harness="hermes")
    out = capsys.readouterr().out
    assert "hermes:" in out
    assert "experimental" in out or "Hermes adapter" in out
    assert rc == 0


def test_doctor_reports_memory_care_files(tmp_target: Path, capsys):
    install_selection(
        tmp_target,
        Selection(depth="workspace", harnesses=["claude"], owner="claude", includes=[]),
    )
    decay = tmp_target / "memory" / "cards" / "decay"
    decay.mkdir(exist_ok=True)
    (decay / "scan-latest.json").write_text(
        json.dumps({"scan_date": "2026-05-13", "counts": {"stale": 2}})
    )
    (decay / "refresh-queue.json").write_text(json.dumps({"cards": [{"file": "x.md"}]}))

    rc = doctor_mod.run(target=tmp_target, harness="generic")
    out = capsys.readouterr().out
    assert rc == 0
    assert "memory-care: scan-latest" in out
    assert "stale=2" in out
    assert "memory-care: refresh-queue" in out
    assert "1 queued" in out


def test_doctor_verifies_memory_index_card_links(tmp_target: Path, capsys):
    install_selection(
        tmp_target,
        Selection(depth="workspace", harnesses=["claude"], owner="claude", includes=[]),
    )

    rc = doctor_mod.run(target=tmp_target, harness="generic")
    out = capsys.readouterr().out
    assert rc == 0
    assert "memory-index: card links" in out
    assert "verified" in out


def test_doctor_fails_broken_memory_index_card_link(tmp_target: Path, capsys):
    install_selection(
        tmp_target,
        Selection(depth="workspace", harnesses=["claude"], owner="claude", includes=[]),
    )
    memory = tmp_target / "MEMORY.md"
    memory.write_text(memory.read_text() + "\n- [missing-card](memory/cards/missing-card.md)\n")

    rc = doctor_mod.run(target=tmp_target, harness="generic")
    out = capsys.readouterr().out
    assert rc == 1
    assert "memory-index: card links" in out
    assert "broken link" in out
    assert "memory/cards/missing-card.md" in out


def test_doctor_fails_when_memory_card_exceeds_budget(tmp_target: Path, capsys):
    install_selection(
        tmp_target,
        Selection(depth="workspace", harnesses=["claude"], owner="claude", includes=[]),
    )
    limit = doctor_mod.MEMORY_CARD_BUDGET_BYTES
    oversized = tmp_target / "memory" / "cards" / "oversized.md"
    oversized.write_text("x" * (limit + 1))

    rc = doctor_mod.run(target=tmp_target, harness="generic")
    out = capsys.readouterr().out
    assert rc == 1
    assert "memory-card: budget" in out
    assert "over hard limit" in out
    assert "memory/cards/oversized.md" in out


def test_doctor_warns_when_memory_card_is_empty(tmp_target: Path, capsys):
    install_selection(
        tmp_target,
        Selection(depth="workspace", harnesses=["claude"], owner="claude", includes=[]),
    )
    empty = tmp_target / "memory" / "cards" / "empty.md"
    empty.write_text("")

    rc = doctor_mod.run(target=tmp_target, harness="generic")
    out = capsys.readouterr().out
    assert rc == 0
    assert "memory-card: empty" in out
    assert "memory/cards/empty.md" in out


def test_doctor_openclaw_reports_cron_memory_jobs(tmp_target: Path, monkeypatch, capsys):
    install_selection(
        tmp_target,
        Selection(
            depth="workspace",
            harnesses=["claude", "openclaw"],
            owner="openclaw",
            includes=[],
        ),
    )
    openclaw_dir = tmp_target / ".openclaw"
    cron_dir = openclaw_dir / "cron"
    cron_dir.mkdir(parents=True)
    (openclaw_dir / "openclaw.json").write_text(
        json.dumps(
            {
                "plugins": {"entries": {"memory-core": {}}},
                "agents": {"defaults": {"model": {"primary": "openai-codex/gpt-5.5"}}},
            }
        )
    )
    (cron_dir / "jobs.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "name": "Claude Memory Handoff Ingest",
                        "enabled": True,
                        "schedule": {"kind": "every", "everyMs": 1800000},
                    },
                    {
                        "name": "Card Decay Scanner (Daily)",
                        "enabled": True,
                        "schedule": {
                            "kind": "cron",
                            "expr": "30 5 * * *",
                            "tz": "America/New_York",
                        },
                    },
                    {
                        "name": "Card Decay Auto-Refresh (Safe)",
                        "enabled": True,
                        "schedule": {
                            "kind": "cron",
                            "expr": "40 5 * * *",
                            "tz": "America/New_York",
                        },
                    },
                    {
                        "name": "Card Decay Deep Report (Weekly)",
                        "enabled": True,
                        "schedule": {
                            "kind": "cron",
                            "expr": "30 5 * * 0",
                            "tz": "America/New_York",
                        },
                    },
                ]
            }
        )
    )
    monkeypatch.setenv("HOME", str(tmp_target))
    monkeypatch.setattr(Path, "home", lambda: tmp_target)

    rc = doctor_mod.run(target=tmp_target, harness="openclaw")
    out = capsys.readouterr().out
    assert rc == 0
    assert "openclaw: handoff ingest cron" in out
    assert "every 30 min" in out
    assert "openclaw: card decay scanner" in out
    assert "openclaw: card decay refresh" in out
    assert "openclaw: card decay weekly" in out


def test_doctor_reports_apparent_harness_shape(tmp_target: Path, capsys):
    sel = Selection(
        depth="workspace",
        harnesses=["claude", "codex", "openclaw"],
        owner="openclaw",
        includes=[],
    )
    install_selection(tmp_target, sel)
    doctor_mod.run(tmp_target)
    out = capsys.readouterr().out
    assert "harnesses:" in out
    assert "claude" in out
    assert "codex" in out
    assert "openclaw" in out
    assert "owner=openclaw" in out


def test_doctor_checks_codex_inbox_when_selected(tmp_target: Path, capsys):
    sel = Selection(
        depth="repo",
        harnesses=["claude", "codex"],
        owner="claude",
        includes=[],
    )
    install_selection(tmp_target, sel)
    doctor_mod.run(tmp_target)
    out = capsys.readouterr().out
    assert ".codex/memory-handoffs" in out


def test_doctor_warns_for_orphan_inbox(tmp_target: Path, capsys):
    """If config says claude only, but .codex/memory-handoffs exists, warn."""
    sel = Selection(
        depth="repo",
        harnesses=["claude"],
        owner="claude",
        includes=[],
    )
    install_selection(tmp_target, sel)
    (tmp_target / ".codex" / "memory-handoffs").mkdir(parents=True)
    doctor_mod.run(tmp_target)
    out = capsys.readouterr().out
    assert "orphan" in out.lower() or "unselected" in out.lower()


def test_doctor_falls_back_to_v0_2_behavior_when_no_config(tmp_target: Path, capsys):
    """A target without .solo-mise/config.json should still run (legacy targets)."""
    tmp_target.mkdir()
    (tmp_target / "AGENTS.md").write_text("# Agents")
    doctor_mod.run(tmp_target)
    out = capsys.readouterr().out
    assert "doctor" in out


def test_doctor_includes_installed_managed_tool(monkeypatch, tmp_target, capsys):
    from brigade.install import install_selection
    from brigade.selection import Selection
    from brigade import managed
    install_selection(tmp_target, Selection(depth="workspace", harnesses=["claude"], owner="claude", includes=[]))

    # Pretend content-guard is installed and healthy.
    monkeypatch.setattr(managed.proc, "which", lambda c: "/x/" + c if c == "content-guard" else None)
    monkeypatch.setattr(managed.proc, "run", lambda args, **kw: managed.proc.Result(0, '{"ok": true}', ""))

    doctor_mod.run(target=tmp_target, harness="generic")
    out = capsys.readouterr().out
    assert "content-guard" in out


def test_doctor_reports_absent_tool_as_manual(monkeypatch, tmp_target, capsys):
    from brigade.install import install_selection
    from brigade.selection import Selection
    from brigade import managed
    install_selection(tmp_target, Selection(depth="workspace", harnesses=["claude"], owner="claude", includes=[]))
    monkeypatch.setattr(managed.proc, "which", lambda c: None)  # nothing installed

    rc = doctor_mod.run(target=tmp_target, harness="generic")
    out = capsys.readouterr().out
    # absent managed tools must not fail the run
    assert rc == 0
    assert "not installed" in out
