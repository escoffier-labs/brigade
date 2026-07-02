"""Tests for brigade doctor."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
import json


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


def test_doctor_memory_care_freshness_compares_in_utc(monkeypatch):
    # Regression for issue #83: the scanner stamps scan_date in UTC, but doctor
    # compared it against the host's LOCAL date. Run in the evening in a timezone
    # behind UTC, a same-day scan then read as "in the future". Pin the wall clock
    # to an instant that is already the 14th in UTC; doctor must read the same UTC
    # date (not the host's local date), so a 14th-stamped scan reads as today.
    fixed_utc = datetime(2026, 5, 14, 1, 0, tzinfo=timezone.utc)

    class _FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_utc if tz is not None else fixed_utc.replace(tzinfo=None)

    monkeypatch.setattr(doctor_mod, "datetime", _FixedClock)
    assert doctor_mod._memory_care_today() == date(2026, 5, 14)

    status, name, detail = doctor_mod._check_memory_care_scan_freshness(Path("decay/scan-latest.json"), "2026-05-14")
    assert name == "memory-care: scan freshness"
    assert "in the future" not in detail
    assert status == doctor_mod.OK


def test_doctor_workspace_profile_wires_memory_care_decay_dir(tmp_target: Path, capsys):
    # Regression for issue #79: a fresh workspace init must create the decay dir
    # doctor actually looks for (.brigade/memory-care/decay), so first contact does
    # not warn "staleness scanner not wired" about a dir init was meant to create.
    install_selection(
        tmp_target,
        Selection(depth="workspace", harnesses=["claude"], owner="claude", includes=[]),
    )
    assert (tmp_target / ".brigade" / "memory-care" / "decay").is_dir()
    doctor_mod.run(target=tmp_target, harness="generic")
    out = capsys.readouterr().out
    assert "staleness scanner not wired" not in out


def test_doctor_json_output_is_structured(tmp_target: Path, capsys):
    install_selection(
        tmp_target,
        Selection(depth="workspace", harnesses=["claude"], owner="claude", includes=[]),
    )
    capsys.readouterr()  # drain install output so only the doctor JSON remains
    rc = doctor_mod.run(target=tmp_target, harness="generic", json_output=True)
    payload = json.loads(capsys.readouterr().out)
    assert payload["target"].endswith("ws")
    assert payload["harnesses"] == ["claude"]
    assert payload["owner"] == "claude"
    assert payload["depth"] == "workspace"
    assert payload["checks"] and {"status", "name", "detail"} <= set(payload["checks"][0])
    assert payload["summary"]["total"] == len(payload["checks"])
    assert payload["ready"] is (rc == 0)


def test_doctor_agents_quality_warns_without_definition_of_done(tmp_target: Path, capsys):
    # issue #84: AGENTS.md existing is not enough; nudge toward a definition of done.
    tmp_target.mkdir(parents=True, exist_ok=True)
    (tmp_target / "AGENTS.md").write_text("# AGENTS\n\nsome guidance, but no done criteria here\n")
    doctor_mod.run(target=tmp_target, harness="generic")
    out = capsys.readouterr().out
    assert "agents-quality: AGENTS.md" in out
    assert "Definition of Done" in out


def test_doctor_agents_quality_ok_for_seeded_workspace(tmp_target: Path, capsys):
    install_selection(
        tmp_target,
        Selection(depth="workspace", harnesses=["claude"], owner="claude", includes=[]),
    )
    capsys.readouterr()
    doctor_mod.run(target=tmp_target, harness="generic", json_output=True)
    payload = json.loads(capsys.readouterr().out)
    agents = [c for c in payload["checks"] if c["name"] == "agents-quality: AGENTS.md"]
    assert agents and agents[0]["status"] == "OK"


def test_doctor_groups_machine_level_findings(tmp_target: Path, capsys):
    # issue #80: host-global findings (a content-guard clone) must not read as
    # this repo's responsibility; they go under a machine-level header.
    install_selection(
        tmp_target,
        Selection(depth="workspace", harnesses=["claude"], owner="claude", includes=[]),
    )
    capsys.readouterr()
    doctor_mod.run(target=tmp_target, harness="generic")
    out = capsys.readouterr().out
    assert "machine-level (not specific to this repo):" in out
    header_idx = out.index("machine-level (not specific to this repo):")
    assert "publish: content-guard" in out[header_idx:]


def test_doctor_json_tags_machine_scope(tmp_target: Path, capsys):
    install_selection(
        tmp_target,
        Selection(depth="workspace", harnesses=["claude"], owner="claude", includes=[]),
    )
    capsys.readouterr()
    doctor_mod.run(target=tmp_target, harness="generic", json_output=True)
    payload = json.loads(capsys.readouterr().out)
    scopes = {c["name"]: c["scope"] for c in payload["checks"]}
    assert scopes.get("publish: content-guard") == "machine"
    assert any(scope == "repo" for scope in scopes.values())


def test_doctor_reports_failures_on_empty_dir(tmp_target: Path, capsys):
    tmp_target.mkdir()
    rc = doctor_mod.run(target=tmp_target, harness="generic")
    assert rc == 1
    out = capsys.readouterr().out
    assert "[fail]" in out


def test_doctor_reports_security_config_and_evidence_bundle(tmp_target: Path, capsys):
    install_selection(
        tmp_target,
        Selection(depth="workspace", harnesses=["claude"], owner="claude", includes=[]),
    )
    from brigade import security_cmd

    security_cmd.init(target=tmp_target)
    security_dir = tmp_target / ".brigade" / "security" / "latest"
    security_dir.mkdir(parents=True)
    (security_dir / "security-report.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-05-26T12:00:00Z",
                "finding_count": 0,
                "policy": "personal",
            }
        )
    )
    (security_dir / "security-report.md").write_text("# Brigade Security Report\n")

    rc = doctor_mod.run(target=tmp_target, harness="generic")
    out = capsys.readouterr().out
    assert rc == 0
    assert "security: config" in out
    assert "policy=personal" in out
    assert "security: evidence bundle" in out
    assert "findings=0" in out


def test_doctor_fails_invalid_security_config(tmp_target: Path, capsys):
    install_selection(
        tmp_target,
        Selection(depth="workspace", harnesses=["claude"], owner="claude", includes=[]),
    )
    security_config = tmp_target / ".brigade" / "security.toml"
    security_config.parent.mkdir(exist_ok=True)
    security_config.write_text('policy = "not-real"\n')

    rc = doctor_mod.run(target=tmp_target, harness="generic")
    out = capsys.readouterr().out
    assert rc == 1
    assert "security: config" in out
    assert "invalid" in out


def test_doctor_warns_on_stale_security_suppressions(tmp_target: Path, capsys):
    install_selection(
        tmp_target,
        Selection(depth="workspace", harnesses=["claude"], owner="claude", includes=[]),
    )
    security_config = tmp_target / ".brigade" / "security.toml"
    security_config.parent.mkdir(exist_ok=True)
    security_config.write_text(
        "\n".join(
            [
                'policy = "personal"',
                'fail_on = "critical"',
                "include_templates = false",
                "",
                "[suppressions]",
                'fingerprints = ["0123456789abcdef"]',
                "",
                "[suppression_reasons]",
                "",
            ]
        )
    )

    rc = doctor_mod.run(target=tmp_target, harness="generic")
    out = capsys.readouterr().out
    assert rc == 0
    assert "security: stale suppressions" in out
    assert "security: suppression reasons" in out


def test_doctor_warns_on_misconfigured_security_enrichment(tmp_target: Path, capsys):
    install_selection(
        tmp_target,
        Selection(depth="workspace", harnesses=["claude"], owner="claude", includes=[]),
    )
    security_config = tmp_target / ".brigade" / "security.toml"
    security_config.parent.mkdir(exist_ok=True)
    security_config.write_text(
        "\n".join(
            [
                'policy = "personal"',
                'fail_on = "critical"',
                "include_templates = false",
                "",
                "[enrichment]",
                'provider = "misp"',
                'misp_url = ""',
                'misp_api_key_env = "BRIGADE_TEST_MISP_KEY"',
                "",
            ]
        )
    )

    rc = doctor_mod.run(target=tmp_target, harness="generic")
    out = capsys.readouterr().out
    assert rc == 0
    assert "security: enrichment" in out
    assert "missing misp_url" in out


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


def test_doctor_hermes_runtime_validation(tmp_target: Path, capsys):
    install_selection(
        tmp_target,
        Selection(depth="workspace", harnesses=["claude", "hermes"], owner="hermes", includes=[]),
    )
    rc = doctor_mod.run(target=tmp_target, harness="hermes")
    out = capsys.readouterr().out
    assert "hermes:" in out
    assert "hermes: workspace handoff inbox" in out
    assert "hermes: memory handoff inbox" in out
    assert "hermes: processed handoff inbox" in out
    assert ".hermes/memory-handoffs" in out
    assert ".claude/memory-handoffs" not in (tmp_target / ".brigade" / "hermes" / "workspace.harness.json").read_text()
    assert (
        ".claude/memory-handoffs"
        not in (tmp_target / ".brigade" / "hermes" / "memory-handoff.harness.json").read_text()
    )
    assert "hermes: runtime validation" in out
    assert rc == 0


def test_doctor_reports_memory_care_files(tmp_target: Path, monkeypatch, capsys):
    monkeypatch.setattr(doctor_mod, "_memory_care_today", lambda: date(2026, 5, 14))
    install_selection(
        tmp_target,
        Selection(depth="workspace", harnesses=["claude"], owner="claude", includes=[]),
    )
    decay = tmp_target / "memory" / "cards" / "decay"
    decay.mkdir(exist_ok=True)
    (decay / "scan-latest.json").write_text(json.dumps({"scan_date": "2026-05-13", "counts": {"stale": 2}}))
    (decay / "refresh-queue.json").write_text(json.dumps({"cards": [{"file": "x.md"}]}))

    rc = doctor_mod.run(target=tmp_target, harness="generic")
    out = capsys.readouterr().out
    assert rc == 0
    assert "memory-care: scan-latest" in out
    assert "stale=2" in out
    assert "memory-care: scan freshness" in out
    assert "last scan 1 days ago" in out
    assert "memory-care: refresh-queue" in out
    assert "1 queued" in out


def test_doctor_warns_when_memory_care_scan_is_stale(tmp_target: Path, monkeypatch, capsys):
    monkeypatch.setattr(doctor_mod, "_memory_care_today", lambda: date(2026, 5, 26))
    install_selection(
        tmp_target,
        Selection(depth="workspace", harnesses=["claude"], owner="claude", includes=[]),
    )
    decay = tmp_target / "memory" / "cards" / "decay"
    decay.mkdir(exist_ok=True)
    (decay / "scan-latest.json").write_text(json.dumps({"scan_date": "2026-05-01", "counts": {"stale": 4}}))
    (decay / "refresh-queue.json").write_text(json.dumps({"cards": []}))

    rc = doctor_mod.run(target=tmp_target, harness="generic")
    out = capsys.readouterr().out
    assert rc == 0
    assert "memory-care: scan freshness" in out
    assert "last scan 25 days ago" in out
    assert "run memory-care scanner" in out


def test_doctor_fails_when_memory_care_state_is_invalid(tmp_target: Path, capsys):
    install_selection(
        tmp_target,
        Selection(depth="workspace", harnesses=["claude"], owner="claude", includes=[]),
    )
    decay = tmp_target / "memory" / "cards" / "decay"
    decay.mkdir(exist_ok=True)
    (decay / "scan-latest.json").write_text("{not-json")
    (decay / "refresh-queue.json").write_text(json.dumps({"cards": "not-a-list"}))

    rc = doctor_mod.run(target=tmp_target, harness="generic")
    out = capsys.readouterr().out
    assert rc == 1
    assert "memory-care: scan-latest" in out
    assert "invalid JSON" in out
    assert "memory-care: refresh-queue" in out
    assert "`cards` must be a list" in out


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


def test_doctor_reports_default_wired_skills_for_selected_harnesses(tmp_target: Path, capsys):
    sel = Selection(
        depth="repo",
        harnesses=["claude", "codex"],
        owner="claude",
        includes=[],
    )
    install_selection(tmp_target, sel)
    capsys.readouterr()

    rc = doctor_mod.run(tmp_target, json_output=True)
    payload = json.loads(capsys.readouterr().out)
    checks = {item["name"]: item for item in payload["checks"]}

    assert rc == 0
    assert checks["skills: claude default wired"]["status"] == "OK"
    assert "brigade-work" in checks["skills: claude default wired"]["detail"]
    assert checks["skills: codex default wired"]["status"] == "OK"
    assert ".codex/skills" in checks["skills: codex default wired"]["detail"]


def test_doctor_warns_when_default_wired_skill_is_missing(tmp_target: Path, capsys):
    sel = Selection(
        depth="repo",
        harnesses=["codex"],
        owner="codex",
        includes=[],
    )
    install_selection(tmp_target, sel)
    (tmp_target / ".codex" / "skills" / "ultra-work-scout" / "SKILL.md").unlink()
    capsys.readouterr()

    rc = doctor_mod.run(tmp_target, json_output=True)
    payload = json.loads(capsys.readouterr().out)
    checks = {item["name"]: item for item in payload["checks"]}
    skill_check = checks["skills: codex default wired: ultra-work-scout"]

    assert rc == 0
    assert skill_check["status"] == "WARN"
    assert "harness=codex" in skill_check["detail"]
    assert "skill=ultra-work-scout" in skill_check["detail"]
    assert ".codex/skills/ultra-work-scout/SKILL.md" in skill_check["detail"]
    assert f"brigade skills install ultra-work-scout --workspace {tmp_target} --target codex" in skill_check["detail"]
    assert "brigade-work" not in skill_check["detail"]


def test_doctor_warns_when_pending_handoff_is_not_watched(tmp_target: Path, capsys):
    sel = Selection(
        depth="repo",
        harnesses=["claude"],
        owner="claude",
        includes=[],
    )
    install_selection(tmp_target, sel)
    (tmp_target / ".claude" / "memory-handoffs" / "2026-05-27-note.md").write_text("# Memory Handoff\n")

    doctor_mod.run(tmp_target)

    out = capsys.readouterr().out
    assert "handoff-source: handoff_warning" in out
    assert "pending handoff" in out


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


def test_doctor_includes_agent_notify_managed_tool(monkeypatch, tmp_target, capsys):
    from brigade.install import install_selection
    from brigade.selection import Selection
    from brigade import managed, notifications_cmd

    install_selection(tmp_target, Selection(depth="workspace", harnesses=["claude"], owner="claude", includes=[]))
    config_path = tmp_target / ".config" / "agent-notify" / "config.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "\n".join(
            [
                "[channels.telegram-personal]",
                'type = "telegram"',
                'bot_token_env = "TEST_TELEGRAM_BOT_TOKEN"',
                'chat_id_env = "TEST_TELEGRAM_CHAT_ID"',
                "",
                "[profiles.operator]",
                'channels = ["telegram-personal"]',
                "default = true",
                "",
            ]
        )
    )
    monkeypatch.setattr(notifications_cmd, "CONFIG_PATH", config_path)
    monkeypatch.setenv("TEST_TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TEST_TELEGRAM_CHAT_ID", "chat")
    monkeypatch.setattr(managed.proc, "which", lambda c: "/x/" + c if c == "agent-notify" else None)

    rc = doctor_mod.run(target=tmp_target, harness="generic")
    out = capsys.readouterr().out

    assert rc == 0
    assert "agent-notify" in out
    assert "operator notifications" in out


def test_doctor_collapses_missing_managed_tools_to_one_line(tmp_path, capsys, monkeypatch):
    from brigade import doctor as doctor_mod
    from brigade.install import install_selection
    from brigade.selection import Selection

    install_selection(tmp_path, Selection(depth="repo", harnesses=["codex"], owner="codex", includes=[]))
    capsys.readouterr()
    doctor_mod.run(tmp_path, harness="generic")
    out = capsys.readouterr().out
    manual_lines = [line for line in out.splitlines() if "not installed; run `brigade add" in line]
    assert len(manual_lines) <= 1, manual_lines
    assert "managed tools not installed" in out
