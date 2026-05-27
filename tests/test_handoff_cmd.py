from __future__ import annotations

import json
import os

from brigade import cli
from brigade import handoff_cmd


def test_handoff_doctor_warns_for_pending_handoff_without_source_config(tmp_path, capsys):
    inbox = tmp_path / ".claude" / "memory-handoffs"
    inbox.mkdir(parents=True)
    (inbox / "TEMPLATE.md").write_text("# Template\n")
    (inbox / "2026-05-27-note.md").write_text("# Memory Handoff\n")

    assert handoff_cmd.doctor(target=tmp_path) == 0

    out = capsys.readouterr().out
    assert "handoff doctor:" in out
    assert "[warn] handoff_sources: not configured" in out
    assert ".claude/memory-handoffs" in out
    assert "pending=1" in out
    assert "watched=no" in out


def test_handoff_doctor_accepts_configured_claude_and_codex_inboxes(tmp_path, capsys):
    for rel in (".claude/memory-handoffs", ".codex/memory-handoffs"):
        inbox = tmp_path / rel
        inbox.mkdir(parents=True)
        (inbox / "2026-05-27-note.md").write_text("# Memory Handoff\n")
    config = tmp_path / ".brigade" / "handoff-sources.json"
    config.parent.mkdir()
    config.write_text(
        json.dumps(
            {
                "canonical_owner": "openclaw",
                "sources": [
                    {
                        "root": ".",
                        "inboxes": [
                            ".claude/memory-handoffs",
                            ".codex/memory-handoffs",
                        ],
                    }
                ],
            }
        )
    )

    assert handoff_cmd.doctor(target=tmp_path) == 0

    out = capsys.readouterr().out
    assert "[ok] handoff_sources:" in out
    assert "pending=1" in out
    assert "watched=yes" in out
    assert "handoff_warning" not in out


def test_handoff_doctor_fails_invalid_source_config(tmp_path, capsys):
    config = tmp_path / ".brigade" / "handoff-sources.json"
    config.parent.mkdir()
    config.write_text("{broken")

    assert handoff_cmd.doctor(target=tmp_path) == 1

    out = capsys.readouterr().out
    assert "[fail] handoff_sources:" in out
    assert "invalid JSON" in out


def test_handoff_doctor_json_output(tmp_path, capsys):
    inbox = tmp_path / ".codex" / "memory-handoffs"
    inbox.mkdir(parents=True)
    (inbox / "2026-05-27-note.md").write_text("# Memory Handoff\n")

    assert handoff_cmd.doctor(target=tmp_path, json_output=True) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["sources_loaded"] is False
    assert payload["inboxes"][1]["pending"] == 1
    assert payload["ingestor"]["configured"] is False


def test_handoff_doctor_warns_for_ingestor_warning_log(tmp_path, capsys):
    log = tmp_path / ".brigade" / "handoff-ingest" / "latest.log"
    log.parent.mkdir(parents=True)
    log.write_text(
        "\n".join(
            [
                "=== handoff-dir: .claude/memory-handoffs ===",
                "SKIP bad-note.md: no recognizable markdown sections found",
                "Warnings: 1",
                "NO_REPLY",
                "",
            ]
        )
    )
    config = tmp_path / ".brigade" / "handoff-sources.json"
    config.write_text(
        json.dumps(
            {
                "sources": [{"root": ".", "inboxes": [".claude/memory-handoffs"]}],
                "ingestor": {"last_run_log": ".brigade/handoff-ingest/latest.log"},
            }
        )
    )

    assert handoff_cmd.doctor(target=tmp_path) == 0

    out = capsys.readouterr().out
    assert "[warn] handoff_ingestor:" in out
    assert "SKIP bad-note.md" in out
    assert "hidden behind NO_REPLY" in out


def test_handoff_doctor_warns_for_stale_ingestor_log(tmp_path, capsys):
    log = tmp_path / "latest.log"
    log.write_text("Processed 0 handoff(s)\n")
    os.utime(log, (1, 1))
    config = tmp_path / ".brigade" / "handoff-sources.json"
    config.parent.mkdir()
    config.write_text(
        json.dumps(
            {
                "sources": [{"root": ".", "inboxes": [".claude/memory-handoffs"]}],
                "ingestor": {"last_run_log": "latest.log", "stale_after_minutes": 1},
            }
        )
    )

    assert handoff_cmd.doctor(target=tmp_path) == 0

    out = capsys.readouterr().out
    assert "[warn] handoff_ingestor:" in out
    assert "handoff ingestor log is stale" in out


def test_handoff_doctor_cli(tmp_path, monkeypatch):
    seen = {}

    def fake_doctor(**kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(handoff_cmd, "doctor", fake_doctor)

    assert cli.main(["handoff", "doctor", "--target", str(tmp_path), "--json"]) == 0
    assert seen == {"target": tmp_path, "sources": None, "json_output": True}
