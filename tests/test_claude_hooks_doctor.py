"""RED: adherence-aware doctor classification for Claude hooks (issue #249)."""

from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path

from brigade import doctor as doctor_mod
from brigade import localio
from brigade.claude_hooks.install_cmd import hooks_uninstall
from brigade.claude_hooks.runtime import write_session_state
from brigade.install import install_selection
from brigade.selection import Selection


def _wired(tmp_path: Path) -> Path:
    target = tmp_path / "repo"
    assert (
        install_selection(
            target,
            Selection(depth="repo", harnesses=["claude"], owner="claude", includes=[]),
        )
        == 0
    )
    return target


def _loop_check(target: Path, capsys):
    capsys.readouterr()
    assert doctor_mod.run(target, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    return next(item for item in payload["checks"] if item["name"] == "claude work loop")


def test_doctor_reports_enforced_current_package(tmp_path: Path, capsys):
    check = _loop_check(_wired(tmp_path), capsys)
    assert check["status"] == "OK"
    assert "enforced" in check["detail"]


def test_doctor_reports_advisory_only_without_hooks(tmp_path: Path, capsys):
    target = _wired(tmp_path)
    assert hooks_uninstall(target=target) == 0
    check = _loop_check(target, capsys)
    assert check["status"] == "WARN"
    assert "advisory-only" in check["detail"]


def test_doctor_reports_partial_for_unreadable_hook_settings(tmp_path: Path, capsys):
    target = _wired(tmp_path)
    (target / ".claude" / "settings.json").write_text("{not-json\n")

    check = _loop_check(target, capsys)

    assert check["status"] == "WARN"
    assert "partial" in check["detail"]
    assert "settings" in check["detail"]


def test_doctor_reports_missing_hook_sidecar(tmp_path: Path, capsys):
    target = _wired(tmp_path)
    (target / ".brigade" / "claude-hooks.json").unlink()

    check = _loop_check(target, capsys)

    assert check["status"] == "WARN"
    assert "partial" in check["detail"]
    assert "sidecar" in check["detail"]


def test_doctor_reports_partial_for_stale_skill(tmp_path: Path, capsys):
    target = _wired(tmp_path)
    (target / ".claude" / "skills" / "brigade-work" / "SKILL.md").write_text("stale\n")
    check = _loop_check(target, capsys)
    assert check["status"] == "WARN"
    assert "partial" in check["detail"]
    assert "stale skill" in check["detail"]


def test_doctor_reports_partial_for_missing_instruction_import(tmp_path: Path, capsys):
    target = _wired(tmp_path)
    claude_md = target / "CLAUDE.md"
    claude_md.write_text(claude_md.read_text().replace("@AGENTS.md", "AGENTS.md"))
    check = _loop_check(target, capsys)
    assert check["status"] == "WARN"
    assert "partial" in check["detail"]
    assert "instruction import" in check["detail"]


def test_doctor_rejects_commented_instruction_import(tmp_path: Path, capsys):
    target = _wired(tmp_path)
    (target / "CLAUDE.md").write_text("# @AGENTS.md disabled\n")

    check = _loop_check(target, capsys)

    assert check["status"] == "WARN"
    assert "partial" in check["detail"]
    assert "instruction import" in check["detail"]


def test_doctor_reports_dormant_recent_write_without_receipt(tmp_path: Path, capsys):
    target = _wired(tmp_path)
    write_session_state(
        target,
        "recent-write",
        {
            "session_id": "recent-write",
            "target": str(target.resolve()),
            "started_at": localio.utc_now_iso(),
            "briefed": True,
            "write_observed": True,
        },
    )
    check = _loop_check(target, capsys)
    assert check["status"] == "WARN"
    assert "dormant" in check["detail"]
    assert "recent work has no verification receipt" in check["detail"]


def test_doctor_ignores_old_or_future_verifyless_session_state(tmp_path: Path, capsys):
    target = _wired(tmp_path)
    now = localio.utc_now()
    for session_id, started_at in (
        ("old-write", now - timedelta(days=30)),
        ("future-write", now + timedelta(days=1)),
    ):
        write_session_state(
            target,
            session_id,
            {
                "session_id": session_id,
                "target": str(target.resolve()),
                "started_at": started_at.isoformat(),
                "briefed": True,
                "write_observed": True,
            },
        )

    check = _loop_check(target, capsys)
    assert check["status"] == "OK"
    assert "enforced" in check["detail"]


def test_doctor_ignores_malformed_recent_session_state(tmp_path: Path, capsys):
    target = _wired(tmp_path)
    for session_id, state in (
        (
            "missing-session",
            {
                "target": str(target.resolve()),
                "started_at": localio.utc_now_iso(),
                "write_observed": True,
            },
        ),
        (
            "wrong-target",
            {
                "session_id": "wrong-target",
                "target": str(tmp_path / "other"),
                "started_at": localio.utc_now_iso(),
                "write_observed": True,
            },
        ),
        (
            "non-boolean-write",
            {
                "session_id": "non-boolean-write",
                "target": str(target.resolve()),
                "started_at": localio.utc_now_iso(),
                "write_observed": "yes",
            },
        ),
    ):
        write_session_state(target, session_id, state)

    check = _loop_check(target, capsys)
    assert check["status"] == "OK"
    assert "enforced" in check["detail"]


def test_doctor_uses_last_write_time_for_receipt_threshold(tmp_path: Path, capsys):
    target = _wired(tmp_path)
    now = localio.utc_now()
    session_id = "write-after-receipt"
    from brigade.claude_hooks import runtime

    fingerprint = runtime._session_fingerprint(session_id)
    write_session_state(
        target,
        session_id,
        {
            "session_id": session_id,
            "session_fingerprint": fingerprint,
            "target": str(target.resolve()),
            "started_at": (now - timedelta(hours=2)).isoformat(),
            "last_write_at": (now - timedelta(minutes=30)).isoformat(),
            "briefed": True,
            "write_observed": True,
            "verify_denied_count": 0,
        },
    )
    run_dir = target / ".brigade" / "work" / "verify-runs" / "before-write"
    run_dir.mkdir(parents=True)
    (run_dir / "receipt.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "started_at": (now - timedelta(hours=1)).isoformat(),
                "harness_session": {"harness": "claude", "fingerprint": fingerprint},
            }
        )
        + "\n"
    )

    check = _loop_check(target, capsys)

    assert check["status"] == "WARN"
    assert "dormant" in check["detail"]


def test_status_payload_reports_legacy_handlers(tmp_path: Path):
    target = _wired(tmp_path)
    settings = target / ".claude" / "settings.json"
    payload = json.loads(settings.read_text())
    payload["hooks"]["SessionStart"] = [
        {
            "hooks": [
                {
                    "type": "command",
                    "command": "python3 hooks/brigade-work-loop.py --event SessionStart",
                }
            ]
        }
    ]
    payload["hooks"]["Stop"] = [
        {
            "hooks": [
                {
                    "type": "command",
                    "command": "python3 hooks/brigade-work-loop.py --event Stop",
                }
            ]
        }
    ]
    settings.write_text(json.dumps(payload, indent=2) + "\n")

    from brigade.claude_hooks.install_cmd import status_payload

    status = status_payload(target)

    assert status["legacy_handler_count"] == 2
    assert status["legacy_events"] == ["SessionStart", "Stop"]


def test_doctor_warns_for_legacy_handlers_with_repair_command(tmp_path: Path, capsys):
    target = _wired(tmp_path)
    settings = target / ".claude" / "settings.json"
    payload = json.loads(settings.read_text())
    payload["hooks"]["SessionStart"] = [
        {
            "hooks": [
                {
                    "type": "command",
                    "command": "python3 hooks/brigade-work-loop.py --event SessionStart",
                }
            ]
        }
    ]
    settings.write_text(json.dumps(payload, indent=2) + "\n")

    check = _loop_check(target, capsys)

    assert check["status"] == "WARN"
    assert ".claude/settings.json" in check["detail"]
    assert f"brigade work hooks install --target {target}" in check["detail"]
    # The base state detail is preserved, not replaced by the legacy-only message.
    assert "state=" in check["detail"]


def test_doctor_reports_normal_state_when_no_legacy_handler(tmp_path: Path, capsys):
    target = _wired(tmp_path)
    check = _loop_check(target, capsys)
    assert check["status"] == "OK"
    assert "state=enforced" in check["detail"]
    assert "legacy" not in check["detail"]


def test_doctor_skips_old_state_files_and_caps_recent_candidates(tmp_path: Path, capsys, monkeypatch):
    target = _wired(tmp_path)
    from brigade.claude_hooks import runtime

    root = target / ".brigade" / "work" / "claude-hooks" / "sessions"
    root.mkdir(parents=True, exist_ok=True)
    old = root / "old.json"
    old.write_text("{}\n")
    old_timestamp = (localio.utc_now() - timedelta(days=30)).timestamp()
    os.utime(old, (old_timestamp, old_timestamp))
    for index in range(runtime.MAX_RECENT_SESSION_STATES + 8):
        (root / f"recent-{index:04d}.json").write_text("{}\n")

    original = localio.read_json_dict
    session_reads: list[Path] = []

    def tracked(path: Path):
        if path.parent == root:
            assert path != old
            session_reads.append(path)
        return original(path)

    monkeypatch.setattr(localio, "read_json_dict", tracked)
    check = _loop_check(target, capsys)

    assert check["status"] == "OK"
    assert len(session_reads) <= runtime.MAX_RECENT_SESSION_STATES
