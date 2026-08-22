"""Tests for brigade doctor."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
import json
import os

import pytest

from brigade import cli
from brigade import doctor as doctor_mod
from brigade.install import install_selection
from brigade.memory_cmd import MemoryCareConfig
from brigade.selection import Selection


def test_doctor_passes_against_workspace_profile(tmp_target: Path, capsys):
    install_selection(
        tmp_target,
        Selection(depth="workspace", harnesses=["claude"], owner="claude", includes=[]),
    )
    rc = doctor_mod.run(target=tmp_target, harness="generic")
    assert rc == 0
    out = capsys.readouterr().out
    assert "triage:" in out
    assert "[fail]" not in out


def _crlf_projected_size(text: str) -> int:
    """Byte size after LF -> CRLF, the Windows working-tree size doctor counts."""
    encoded = text.encode("utf-8")
    return len(encoded) + text.count("\n") - text.count("\r\n")


def test_fresh_quickstart_workspace_passes_doctor(tmp_target: Path, capsys):
    """#1025: a fresh multi-harness quickstart must not fail its own doctor.

    The reported failure was AGENTS.md at 12066/12000 on Windows. Unix LF is
    already close to the cap; CRLF adds one byte per line and tips it over.
    This test uses the issue's harness set and asserts the CRLF-projected size,
    so putting the file-maintenance table back into AGENTS.md fails here.
    """
    install_selection(
        tmp_target,
        Selection(
            depth="workspace",
            harnesses=["openclaw", "claude", "codex", "grok"],
            owner="openclaw",
            includes=[],
        ),
    )
    agents = (tmp_target / "AGENTS.md").read_text(encoding="utf-8")
    limit = doctor_mod.BOOTSTRAP_BUDGETS["AGENTS.md"]
    assert _crlf_projected_size(agents) <= limit
    card = tmp_target / "memory" / "cards" / "workspace-file-maintenance.md"
    assert card.is_file()
    card_text = card.read_text(encoding="utf-8")
    assert "`USER.md`" in card_text
    assert "`SAFETY_RULES.md`" in card_text
    assert "workspace-file-maintenance.md" in agents
    rc = doctor_mod.run(target=tmp_target, harness="generic")
    out = capsys.readouterr().out
    assert rc == 0
    assert "[fail]" not in out


def test_rendered_agents_md_stays_under_budget_for_all_harnesses(tmp_path: Path):
    """Mutation guard: every known harness together still fits, including CRLF."""
    from brigade.selection import KNOWN_HARNESSES

    target = tmp_path / "all-harnesses"
    install_selection(
        target,
        Selection(
            depth="workspace",
            harnesses=list(KNOWN_HARNESSES),
            owner="openclaw",
            includes=[],
        ),
    )
    agents = (target / "AGENTS.md").read_text(encoding="utf-8")
    assert _crlf_projected_size(agents) <= doctor_mod.BOOTSTRAP_BUDGETS["AGENTS.md"]


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
    doctor_mod.run(target=tmp_target, harness="generic", full=True)
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


def test_doctor_triages_long_output_by_default(tmp_target: Path, capsys, monkeypatch):
    checks = [(doctor_mod.OK, f"check-{index}", "ready") for index in range(48)]
    checks.extend(
        [
            (doctor_mod.WARN, "warning-check", "run `brigade fix warning`"),
            (doctor_mod.FAIL, "failed-check", "run `brigade fix failure`"),
            (doctor_mod.MANUAL, "manual-check", "run `brigade configure`"),
        ]
    )
    monkeypatch.setattr(doctor_mod, "_gather_checks", lambda _ctx: checks)

    assert doctor_mod.run(target=tmp_target) == 1
    out = capsys.readouterr().out
    assert "triage: 51 checks, 48 ok, 1 warn, 1 failed, 1 manual, 0 info" in out
    assert "failures:" in out
    assert "warnings:" in out
    assert "manual actions:" in out
    assert out.index("failures:") < out.index("warnings:") < out.index("manual actions:")
    assert "warning-check" in out
    assert "failed-check" in out
    assert "manual-check" in out
    assert "check-0" not in out
    assert "run `brigade doctor --full` to show all checks" in out


def test_doctor_default_hides_ok_even_below_condensation_threshold(tmp_target: Path, capsys, monkeypatch):
    checks = [(doctor_mod.OK, f"check-{index}", "ready") for index in range(10)]
    checks.append((doctor_mod.WARN, "warning-check", "needs attention"))
    monkeypatch.setattr(doctor_mod, "_gather_checks", lambda _ctx: checks)

    doctor_mod.run(target=tmp_target)
    out = capsys.readouterr().out
    assert "check-0" not in out
    assert "warning-check" in out
    assert "warnings:" in out
    assert "run `brigade doctor --full` to show all checks" in out


def test_doctor_full_preserves_exhaustive_text_output(tmp_target: Path, capsys, monkeypatch):
    checks = [(doctor_mod.OK, f"check-{index}", "ready") for index in range(51)]
    monkeypatch.setattr(doctor_mod, "_gather_checks", lambda _ctx: checks)

    assert doctor_mod.run(target=tmp_target, full=True) == 0
    out = capsys.readouterr().out
    assert "ok:" in out
    assert "check-0" in out
    assert "check-50" in out
    assert "brigade doctor --full" not in out


def test_doctor_shared_reporter_is_exhaustive_by_default(capsys):
    checks = [(doctor_mod.OK, f"shared-check-{index}", "ready") for index in range(51)]

    assert doctor_mod._report(checks) == 0
    out = capsys.readouterr().out
    assert "ok:" in out
    assert "shared-check-0" in out
    assert "shared-check-50" in out
    assert "brigade doctor --full" not in out


def test_doctor_cli_forwards_full(tmp_path, monkeypatch):
    seen = {}

    def fake_run(**kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(doctor_mod, "run", fake_run)

    assert cli.main(["doctor", "--target", str(tmp_path), "--harness", "hermes", "--full"]) == 0
    assert seen == {
        "target": tmp_path,
        "harness": "hermes",
        "json_output": False,
        "full": True,
        "operator": False,
        "agent": False,
    }


def test_doctor_cli_forwards_agent(tmp_path, monkeypatch):
    seen = {}

    def fake_run(**kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(doctor_mod, "run", fake_run)

    assert cli.main(["doctor", "--target", str(tmp_path), "--agent", "--json"]) == 0
    assert seen == {
        "target": tmp_path,
        "harness": "generic",
        "json_output": True,
        "full": False,
        "operator": False,
        "agent": True,
    }


def test_doctor_agents_quality_warns_without_definition_of_done(tmp_target: Path, capsys):
    # issue #84: AGENTS.md existing is not enough; nudge toward a definition of done.
    tmp_target.mkdir(parents=True, exist_ok=True)
    (tmp_target / "AGENTS.md").write_text("# AGENTS\n\nsome guidance, but no done criteria here\n")
    doctor_mod.run(target=tmp_target, harness="generic", full=True)
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


def test_doctor_groups_operator_scoped_findings_with_operator_flag(tmp_target: Path, capsys):
    # issue #80 / #478: host-global findings belong under an operator header when
    # explicitly requested; they are hidden from the default target-scoped run.
    install_selection(
        tmp_target,
        Selection(depth="workspace", harnesses=["claude"], owner="claude", includes=[]),
    )
    capsys.readouterr()
    doctor_mod.run(target=tmp_target, harness="generic", full=True, operator=True)
    out = capsys.readouterr().out
    assert "operator/host (not specific to this target):" in out
    header_idx = out.index("operator/host (not specific to this target):")
    assert "guard: embedded content guard" in out[header_idx:]


def test_doctor_hides_operator_scoped_checks_by_default(tmp_target: Path, capsys):
    install_selection(
        tmp_target,
        Selection(depth="workspace", harnesses=["claude"], owner="claude", includes=[]),
    )
    capsys.readouterr()
    doctor_mod.run(target=tmp_target, harness="generic", full=True)
    out = capsys.readouterr().out
    assert "operator/host (not specific to this target):" not in out
    assert "guard: embedded content guard" not in out


def test_doctor_json_tags_operator_scope_when_requested(tmp_target: Path, capsys):
    install_selection(
        tmp_target,
        Selection(depth="workspace", harnesses=["claude"], owner="claude", includes=[]),
    )
    capsys.readouterr()
    doctor_mod.run(target=tmp_target, harness="generic", json_output=True, operator=True)
    payload = json.loads(capsys.readouterr().out)
    scopes = {c["name"]: c["scope"] for c in payload["checks"]}
    assert scopes.get("guard: embedded content guard") == "operator"
    assert any(scope == "target" for scope in scopes.values())


def test_doctor_first_contact_condenses_actionable_target_checks(tmp_target: Path, capsys):
    install_selection(
        tmp_target,
        Selection(depth="workspace", harnesses=["claude"], owner="claude", includes=[]),
    )
    capsys.readouterr()
    rc = doctor_mod.run(target=tmp_target, harness="generic", json_output=True)
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["total"] >= 40
    assert rc == (0 if payload["ready"] else 1)

    capsys.readouterr()
    doctor_mod.run(target=tmp_target, harness="generic")
    out = capsys.readouterr().out
    assert "[ok]" not in out
    assert "ok:" not in out
    assert "run `brigade doctor --full` to show all checks" in out


def test_doctor_selected_scope_counts_and_exit_unchanged(tmp_target: Path, capsys):
    install_selection(
        tmp_target,
        Selection(depth="workspace", harnesses=["claude"], owner="claude", includes=[]),
    )
    capsys.readouterr()
    rc_default = doctor_mod.run(target=tmp_target, harness="generic", json_output=True)
    default_payload = json.loads(capsys.readouterr().out)
    capsys.readouterr()
    rc_operator = doctor_mod.run(target=tmp_target, harness="generic", json_output=True, operator=True)
    operator_payload = json.loads(capsys.readouterr().out)

    assert default_payload["summary"]["total"] < operator_payload["summary"]["total"]
    assert all(check["scope"] == "target" for check in default_payload["checks"])
    assert any(check["scope"] == "operator" for check in operator_payload["checks"])
    assert rc_default == (0 if default_payload["summary"]["failed"] == 0 else 1)
    assert rc_operator == (0 if operator_payload["summary"]["failed"] == 0 else 1)


def test_doctor_explicit_operator_scope_not_name_inferred(tmp_target: Path, capsys, monkeypatch):
    from brigade import component_report

    marker = "ISSUE556_EXPLICIT_SCOPE_MARKER"

    def fake_component_checks(**kwargs):
        return [(doctor_mod.OK, "target-looking-name", marker)]

    install_selection(
        tmp_target,
        Selection(depth="workspace", harnesses=["claude"], owner="claude", includes=[]),
    )
    monkeypatch.setattr(component_report, "doctor_checks", fake_component_checks)
    capsys.readouterr()
    doctor_mod.run(target=tmp_target, harness="generic", json_output=True, operator=True)
    payload = json.loads(capsys.readouterr().out)
    host_check = next(check for check in payload["checks"] if check["name"] == "target-looking-name")
    assert host_check["scope"] == "operator"
    assert marker in host_check["detail"]


def test_doctor_json_omits_operator_checks_by_default(tmp_target: Path, capsys, monkeypatch):
    from brigade import component_report

    marker = "ISSUE478_HOST_GLOBAL_MARKER"

    def fake_component_checks(**kwargs):
        return [(doctor_mod.OK, "components: fake-host", marker)]

    install_selection(
        tmp_target,
        Selection(depth="workspace", harnesses=["claude"], owner="claude", includes=[]),
    )
    monkeypatch.setattr(component_report, "doctor_checks", fake_component_checks)
    capsys.readouterr()
    doctor_mod.run(target=tmp_target, harness="generic", json_output=True)
    payload = json.loads(capsys.readouterr().out)
    assert marker not in json.dumps(payload)
    assert not any(check["name"].startswith("components:") for check in payload["checks"])


def test_doctor_operator_flag_includes_host_global_state(tmp_target: Path, capsys, monkeypatch):
    from brigade import component_report

    marker = "ISSUE478_HOST_GLOBAL_MARKER"

    def fake_component_checks(**kwargs):
        return [(doctor_mod.OK, "components: fake-host", marker)]

    install_selection(
        tmp_target,
        Selection(depth="workspace", harnesses=["claude"], owner="claude", includes=[]),
    )
    monkeypatch.setattr(component_report, "doctor_checks", fake_component_checks)
    capsys.readouterr()
    doctor_mod.run(target=tmp_target, harness="generic", json_output=True, operator=True)
    payload = json.loads(capsys.readouterr().out)
    host_checks = [check for check in payload["checks"] if check["name"].startswith("components:")]
    assert host_checks
    assert any(marker in check["detail"] for check in host_checks)


def test_doctor_target_scoped_detail_names_workspace(tmp_target: Path, capsys):
    install_selection(
        tmp_target,
        Selection(depth="workspace", harnesses=["claude"], owner="claude", includes=[]),
    )
    capsys.readouterr()
    doctor_mod.run(target=tmp_target, harness="generic", full=True)
    out = capsys.readouterr().out
    assert f"target={tmp_target.resolve()}:" in out


def test_doctor_reports_failures_on_empty_dir(tmp_target: Path, capsys):
    tmp_target.mkdir()
    rc = doctor_mod.run(target=tmp_target, harness="generic", full=True)
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
                "findings": [],
                "policy": "personal",
            }
        )
    )
    (security_dir / "security-report.md").write_text("# Brigade Security Report\n")

    rc = doctor_mod.run(target=tmp_target, harness="generic", full=True)
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


def test_doctor_warns_when_authority_store_isolation_is_off(tmp_target: Path, capsys):
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
                "",
                "[authority_store]",
                'isolation = "off"',
                "",
            ]
        )
    )

    rc = doctor_mod.run(target=tmp_target, harness="generic")
    out = capsys.readouterr().out
    assert rc == 0
    assert "security: authority store" in out
    assert "[warn]" in out
    assert "same-uid" in out
    assert "external-key" in out


def test_doctor_warns_when_sticky_marker_exists_and_isolation_is_off(tmp_target: Path, capsys):
    from brigade import authority_key, authority_marker
    from brigade.work_cmd import ledger

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
                "",
                "[authority_store]",
                'isolation = "external-key"',
                "",
            ]
        )
    )
    (tmp_target / ".brigade").mkdir(exist_ok=True)
    workspace = ledger._workspace_directory_identity(tmp_target)
    root = os.open(tmp_target / ".brigade", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        ledger._record_external_directory_authority(tmp_target, (".brigade",), root, workspace=workspace)
    finally:
        os.close(root)
    assert authority_marker.marker_exists(tmp_target)
    security_config.write_text(
        "\n".join(
            [
                'policy = "personal"',
                "",
                "[authority_store]",
                'isolation = "off"',
                "",
            ]
        )
    )
    authority_key.clear_key_cache()

    rc = doctor_mod.run(target=tmp_target, harness="generic")
    out = capsys.readouterr().out
    assert rc == 0
    assert "security: authority store" in out
    assert "[warn]" in out
    assert "same-uid" in out
    assert "signed store with isolation off" in out
    assert "brigade security authority downgrade" in out


def test_doctor_ok_when_authority_store_external_key_is_provisioned(tmp_target: Path, capsys):
    from brigade import authority_key

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
                "",
                "[authority_store]",
                'isolation = "external-key"',
                "",
            ]
        )
    )
    authority_key.clear_key_cache()
    authority_key.generate_key()
    key = authority_key.key_path()
    assert not authority_key.key_is_inside_tree(key, tmp_target)

    rc = doctor_mod.run(target=tmp_target, harness="generic", full=True)
    out = capsys.readouterr().out
    assert rc == 0
    assert "security: authority store" in out
    assert "external-key HMAC enabled" in out
    assert "[fail]" not in out


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

    rc = doctor_mod.run(target=tmp_target, harness="generic", full=True)
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
    rc = doctor_mod.run(target=tmp_target, harness="openclaw", full=True, operator=True)
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
    rc = doctor_mod.run(target=tmp_target, harness="hermes", full=True)
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

    rc = doctor_mod.run(target=tmp_target, harness="generic", full=True)
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

    rc = doctor_mod.run(target=tmp_target, harness="generic", full=True)
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

    rc = doctor_mod.run(target=tmp_target, harness="generic", full=True)
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
    limit = MemoryCareConfig().max_card_bytes
    oversized = tmp_target / "memory" / "cards" / "oversized.md"
    oversized.write_text("x" * (limit + 1))

    rc = doctor_mod.run(target=tmp_target, harness="generic")
    out = capsys.readouterr().out
    assert rc == 1
    assert "memory-card: budget" in out
    assert "over hard limit" in out
    assert "memory/cards/oversized.md" in out


def test_doctor_memory_card_budget_honors_config(tmp_path: Path):
    # doctor's card-budget check must honor .brigade/memory-care.toml the same way
    # `brigade memory care` does: custom max_card_bytes and exclude_paths.
    target = tmp_path
    (target / ".brigade").mkdir(parents=True)
    (target / ".brigade" / "memory-care.toml").write_text(
        'exclude_paths = ["memory/cards/archive"]\nmax_card_bytes = 500\n'
    )
    cards = target / "memory" / "cards"
    (cards / "archive").mkdir(parents=True)
    (cards / "big-active.md").write_text("y" * 800)  # over 500 -> flagged
    (cards / "small.md").write_text("ok\n")  # under -> fine
    (cards / "archive" / "big-archived.md").write_text("x" * 800)  # over but excluded

    results = doctor_mod._check_memory_cards(target)
    budget = [r for r in results if r[1] == "memory-card: budget"]
    assert budget, "expected a memory-card: budget result"
    status, _, detail = budget[0]
    assert status == doctor_mod.FAIL
    assert "memory/cards/big-active.md" in detail
    assert "big-archived.md" not in detail  # excluded path not counted


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

    rc = doctor_mod.run(target=tmp_target, harness="openclaw", full=True, operator=True)
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
    capsys.readouterr()  # drain install output before the doctor run

    # Assert the production contract directly via structured output: a selected
    # Codex harness must produce an OK handoff-inbox check that names the real
    # .codex/memory-handoffs path. Do not depend on compact-output truncation
    # or on check ordering, which the managed-tool additions can shift.
    doctor_mod.run(tmp_target, json_output=True)
    payload = json.loads(capsys.readouterr().out)
    inbox_checks = {
        check["name"]: check for check in payload["checks"] if check["name"].startswith("adapter: handoff: codex")
    }
    assert "adapter: handoff: codex inbox" in inbox_checks
    codex_inbox = inbox_checks["adapter: handoff: codex inbox"]
    assert codex_inbox["status"] == doctor_mod.OK
    assert ".codex/memory-handoffs" in codex_inbox["detail"]


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
    assert checks["adapter: skills: claude default wired"]["status"] == "OK"
    assert "brigade-work" in checks["adapter: skills: claude default wired"]["detail"]
    assert checks["adapter: skills: codex default wired"]["status"] == "OK"
    assert ".codex/skills" in checks["adapter: skills: codex default wired"]["detail"]


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
    skill_check = checks["adapter: skills: codex default wired: ultra-work-scout"]

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


def test_build_context_unspecified_harness_without_config(tmp_target: Path, capsys):
    """Unconfigured targets must not inherit Claude harness checks by default."""
    tmp_target.mkdir()
    (tmp_target / "AGENTS.md").write_text("# Agents")

    ctx = doctor_mod.build_context(tmp_target)
    assert ctx.harnesses == []
    assert ctx.selection is None

    doctor_mod.run(target=tmp_target, harness="generic", json_output=True)
    payload = json.loads(capsys.readouterr().out)
    assert payload["harnesses"] == []

    names = {check["name"] for check in payload["checks"]}
    assert "claude work loop" not in names
    assert not any(name.startswith("adapter: handoff:") for name in names)
    assert not any(name.startswith("adapter: skills:") for name in names)

    capsys.readouterr()
    doctor_mod.run(target=tmp_target, harness="generic")
    out = capsys.readouterr().out
    assert "unspecified" in out.lower()
    assert "assuming claude" not in out.lower()


def test_build_context_claude_declared_labels_adapter_checks(tmp_target: Path, capsys):
    """Explicit Claude selection keeps native checks and labels adapter projections."""
    install_selection(
        tmp_target,
        Selection(depth="repo", harnesses=["claude"], owner="claude", includes=[]),
    )
    capsys.readouterr()

    ctx = doctor_mod.build_context(tmp_target)
    assert ctx.harnesses == ["claude"]

    doctor_mod.run(target=tmp_target, json_output=True)
    payload = json.loads(capsys.readouterr().out)
    names = {check["name"] for check in payload["checks"]}

    assert "adapter: handoff: claude inbox" in names
    assert "adapter: skills: claude default wired" in names
    assert not any(name.startswith("adapter: handoff: codex") for name in names)


def test_build_context_non_claude_harness_labels_adapter_checks_only(tmp_target: Path, capsys):
    """Non-Claude harness selection must not run Claude-native doctor checks."""
    install_selection(
        tmp_target,
        Selection(depth="repo", harnesses=["codex"], owner="codex", includes=[]),
    )
    capsys.readouterr()

    ctx = doctor_mod.build_context(tmp_target)
    assert ctx.harnesses == ["codex"]

    doctor_mod.run(target=tmp_target, json_output=True)
    payload = json.loads(capsys.readouterr().out)
    names = {check["name"] for check in payload["checks"]}

    assert "claude work loop" not in names
    assert "adapter: handoff: codex inbox" in names
    assert "adapter: skills: codex default wired" in names
    assert not any(name.startswith("adapter: handoff: claude") for name in names)


def test_doctor_includes_embedded_content_guard_without_external_binary(monkeypatch, tmp_target, capsys):
    from brigade.install import install_selection
    from brigade.selection import Selection
    from brigade import managed

    install_selection(tmp_target, Selection(depth="workspace", harnesses=["claude"], owner="claude", includes=[]))

    monkeypatch.setattr(managed.proc, "which", lambda c: None)

    doctor_mod.run(target=tmp_target, harness="generic", full=True, operator=True)
    out = capsys.readouterr().out
    assert "guard: embedded content guard" in out


def test_doctor_reports_absent_tool_as_manual(monkeypatch, tmp_target, capsys):
    from brigade.install import install_selection
    from brigade.selection import Selection
    from brigade import managed

    install_selection(tmp_target, Selection(depth="workspace", harnesses=["claude"], owner="claude", includes=[]))
    monkeypatch.setattr(managed.proc, "which", lambda c: None)  # nothing installed

    rc = doctor_mod.run(target=tmp_target, harness="generic", full=True, operator=True)
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
    monkeypatch.setattr(
        managed.component_bins, "resolve", lambda name, **kw: "/x/" + name if name == "agent-notify" else None
    )

    rc = doctor_mod.run(target=tmp_target, harness="generic", full=True, operator=True)
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
    doctor_mod.run(tmp_path, harness="generic", operator=True)
    out = capsys.readouterr().out
    manual_lines = [line for line in out.splitlines() if "not installed; run `brigade add" in line]
    assert len(manual_lines) <= 1, manual_lines
    assert "managed tools not installed" in out


# --- memory-care producer collision (#403) ---

FAKE_CRON_COMMAND = "FAKE-CRON-PAYLOAD-SECRET-TOKEN-abc123"


def _write_openclaw_cron(tmp_target: Path, jobs: list[dict], monkeypatch) -> None:
    openclaw_dir = tmp_target / ".openclaw"
    cron_dir = openclaw_dir / "cron"
    cron_dir.mkdir(parents=True)
    (openclaw_dir / "openclaw.json").write_text(
        json.dumps(
            {
                "plugins": {"entries": {"memory-core": {}}},
                "agents": {"defaults": {"model": {"primary": "example-provider/example-model"}}},
            }
        )
    )
    payload_jobs = []
    for job in jobs:
        entry = dict(job)
        entry.setdefault("command", FAKE_CRON_COMMAND)
        payload_jobs.append(entry)
    (cron_dir / "jobs.json").write_text(json.dumps({"jobs": payload_jobs}))
    monkeypatch.setenv("HOME", str(tmp_target))
    monkeypatch.setattr(Path, "home", lambda: tmp_target)


def _write_scanners_toml(tmp_target: Path, body: str) -> None:
    brigade = tmp_target / ".brigade"
    brigade.mkdir(parents=True, exist_ok=True)
    (brigade / "scanners.toml").write_text(body)


def _write_memory_care_config(tmp_target: Path, *, output_path: str | None = None) -> None:
    brigade = tmp_target / ".brigade"
    brigade.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if output_path is not None:
        lines.append(f"output_path = {json.dumps(output_path)}")
    (brigade / "memory-care.toml").write_text("\n".join(lines) + ("\n" if lines else ""))


def _producer_collision_checks(results: list) -> list:
    return [result for result in results if result[1] == "memory-care: producer collision"]


def _run_producer_collision_check(tmp_target: Path) -> list:
    return doctor_mod._check_memory_care_producer_collision(tmp_target)


def test_doctor_warns_memory_care_writer_collision(tmp_target: Path, monkeypatch):
    """A Brigade memory-care scan writer and legacy cron sharing one dir -> one warning."""
    _write_memory_care_config(tmp_target, output_path="memory/cards/decay")
    _write_scanners_toml(
        tmp_target,
        """
[[scanner]]
id = "memory-care-scan"
source = "memory-care"
command = "brigade memory care scan"
cadence = "daily@03:00"
enabled = true
timeout = 180
output_path = "memory/cards/decay/refresh-queue.json"
conflict_window = "02:55-03:15"
""",
    )
    _write_openclaw_cron(
        tmp_target,
        [
            {
                "name": "Card Decay Scanner (Daily)",
                "enabled": True,
                "schedule": {"kind": "cron", "expr": "30 5 * * *", "tz": "Example/Place"},
            },
        ],
        monkeypatch,
    )

    results = _run_producer_collision_check(tmp_target)
    collisions = _producer_collision_checks(results)

    assert len(collisions) == 1
    status, name, detail = collisions[0]
    assert status == doctor_mod.WARN
    assert name == "memory-care: producer collision"
    assert "memory/cards/decay" in detail
    assert "brigade memory-care" in detail
    assert "legacy Card Decay Scanner (Daily)" in detail


def test_doctor_no_collision_with_memory_care_consumer_scanner(tmp_target: Path, monkeypatch):
    """A consumer scanner that reads the queue must not be classified as a producer."""
    _write_scanners_toml(
        tmp_target,
        """
[[scanner]]
id = "memory-care"
source = "memory-care"
command = "brigade memory care import-issues --json"
cadence = "daily@03:00"
enabled = true
timeout = 180
output_path = "memory/cards/decay/refresh-queue.json"
conflict_window = "02:55-03:15"
""",
    )
    _write_openclaw_cron(
        tmp_target,
        [
            {
                "name": "Card Decay Scanner (Daily)",
                "enabled": True,
                "schedule": {"kind": "cron", "expr": "30 5 * * *", "tz": "Example/Place"},
            },
        ],
        monkeypatch,
    )

    results = _run_producer_collision_check(tmp_target)
    assert _producer_collision_checks(results) == []


def test_doctor_no_collision_with_refresh_consumer(tmp_target: Path, monkeypatch):
    _write_memory_care_config(tmp_target)
    _write_openclaw_cron(
        tmp_target,
        [
            {
                "name": "Card Decay Auto-Refresh (Safe)",
                "enabled": True,
                "schedule": {"kind": "cron", "expr": "40 5 * * *", "tz": "Example/Place"},
            },
        ],
        monkeypatch,
    )

    results = _run_producer_collision_check(tmp_target)

    assert _producer_collision_checks(results) == []


def test_doctor_no_collision_with_custom_memory_care_output_path(tmp_target: Path, monkeypatch):
    _write_memory_care_config(tmp_target, output_path=".brigade/memory-care/custom-decay")
    _write_openclaw_cron(
        tmp_target,
        [
            {
                "name": "Card Decay Scanner (Daily)",
                "enabled": True,
                "schedule": {"kind": "cron", "expr": "30 5 * * *", "tz": "Example/Place"},
            },
        ],
        monkeypatch,
    )

    results = _run_producer_collision_check(tmp_target)

    assert _producer_collision_checks(results) == []


def test_doctor_warns_when_custom_memory_care_output_collides(tmp_target: Path, monkeypatch):
    _write_memory_care_config(tmp_target, output_path="memory/cards/decay")
    _write_openclaw_cron(
        tmp_target,
        [
            {
                "name": "Card Decay Scanner (Daily)",
                "enabled": True,
                "schedule": {"kind": "cron", "expr": "30 5 * * *", "tz": "Example/Place"},
            },
        ],
        monkeypatch,
    )

    results = _run_producer_collision_check(tmp_target)
    collisions = _producer_collision_checks(results)

    assert len(collisions) == 1
    _, _, detail = collisions[0]
    assert "memory/cards/decay" in detail
    assert "brigade memory-care" in detail
    assert "legacy Card Decay Scanner (Daily)" in detail


def test_doctor_memory_care_collision_redacts_sensitive_cron_details(tmp_target: Path, monkeypatch):
    _write_memory_care_config(tmp_target, output_path="memory/cards/decay")
    _write_scanners_toml(
        tmp_target,
        """
[[scanner]]
id = "memory-care-scan"
source = "memory-care"
command = "brigade memory care scan"
cadence = "daily@03:00"
enabled = true
timeout = 180
output_path = "memory/cards/decay/refresh-queue.json"
conflict_window = "02:55-03:15"
""",
    )
    _write_openclaw_cron(
        tmp_target,
        [
            {
                "name": "Card Decay Scanner (Daily)",
                "enabled": True,
                "schedule": {"kind": "cron", "expr": "30 5 * * *", "tz": "Example/Place"},
            },
        ],
        monkeypatch,
    )

    results = _run_producer_collision_check(tmp_target)
    collisions = _producer_collision_checks(results)
    assert len(collisions) == 1

    detail = collisions[0][2]
    assert "memory/cards/decay" in detail
    assert FAKE_CRON_COMMAND not in detail
    assert "SECRET-TOKEN" not in detail
    assert str(tmp_target) not in detail
    assert "/home/" not in detail
    assert "30 5 * * *" not in detail
    assert "Example/Place" not in detail
    assert "jobs.json" not in detail
    assert ".openclaw" not in detail


def test_doctor_memory_care_collision_suggested_next_step_is_read_only(tmp_target: Path, monkeypatch):
    _write_memory_care_config(tmp_target, output_path="memory/cards/decay")
    _write_scanners_toml(
        tmp_target,
        """
[[scanner]]
id = "memory-care-scan"
source = "memory-care"
command = "brigade memory care scan"
cadence = "daily@03:00"
enabled = true
timeout = 180
output_path = "memory/cards/decay/refresh-queue.json"
conflict_window = "02:55-03:15"
""",
    )
    _write_openclaw_cron(
        tmp_target,
        [
            {
                "name": "Card Decay Scanner (Daily)",
                "enabled": True,
                "schedule": {"kind": "cron", "expr": "30 5 * * *", "tz": "Example/Place"},
            },
        ],
        monkeypatch,
    )

    results = _run_producer_collision_check(tmp_target)
    collisions = _producer_collision_checks(results)
    assert len(collisions) == 1

    detail = collisions[0][2].lower()
    assert "brigade memory care status" in detail
    assert "read-only" in detail
    for destructive in ("rm ", "unlink(", "write_text", "edit card", "overwrite"):
        assert destructive not in detail
    # The diagnostic must not leak the raw cron payload, schedule, or workspace path.
    assert FAKE_CRON_COMMAND not in detail
    assert str(tmp_target) not in detail
    assert "30 5 * * *" not in detail
    assert "example/place" not in detail
    assert "jobs.json" not in detail
    assert ".openclaw" not in detail


def test_doctor_no_collision_with_shipped_scanners_toml(tmp_target: Path, monkeypatch):
    """A scanners.toml of only memory-care CONSUMERS (like the shipped one) is no collision.

    Mirrors the shipped registry's memory-care entries (queue readers, not the
    ``brigade memory care scan`` writer) as an inline fixture, so the test stays
    hermetic and never reads the repo's own gitignored .brigade/scanners.toml.
    """
    _write_memory_care_config(tmp_target)  # default Brigade output path
    shipped_consumers = (
        "[[scanner]]\n"
        "id = 'memory-refresh'\n"
        "source = 'memory-refresh'\n"
        "command = 'brigade work import memory-refresh --json'\n"
        "cadence = 'daily@02:45'\n"
        "enabled = true\n"
        "output_path = 'memory/cards/decay/refresh-queue.json'\n\n"
        "[[scanner]]\n"
        "id = 'memory-care'\n"
        "source = 'memory-care'\n"
        "command = 'brigade memory care import-issues --json'\n"
        "cadence = 'daily@03:00'\n"
        "enabled = false\n"
        "output_path = 'memory/cards/decay/refresh-queue.json'\n"
    )
    _write_scanners_toml(tmp_target, shipped_consumers)
    # Point HOME at the temp target so any host-global cron state is isolated.
    openclaw_dir = tmp_target / ".openclaw"
    (openclaw_dir / "cron").mkdir(parents=True)
    (openclaw_dir / "cron" / "jobs.json").write_text(json.dumps({"jobs": []}))
    monkeypatch.setenv("HOME", str(tmp_target))
    monkeypatch.setattr(Path, "home", lambda: tmp_target)

    results = _run_producer_collision_check(tmp_target)
    assert _producer_collision_checks(results) == []


# -- Issue #568 slice 5 Task 6: read-only recovery checkpoint doctor check --


_RUN_ID = "20260727-153045-a1b2c3d4"
_RECOVERED_AT = "2026-07-27T15:31:00.123456+00:00"
_OWNER_PID = 42424242


def _recovery_writer_bytes(obj: dict) -> bytes:
    return (json.dumps(obj, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _recovery_write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _recovery_write_lock_owner(workspace: Path, run_dir: Path, *, pid: int = 99999999) -> Path:
    lock_path = workspace / ".brigade" / "run.lock"
    lock_path.mkdir(parents=True)
    (lock_path / "pid").write_text(f"{pid}\n")
    _recovery_write_json(
        lock_path / "owner.json",
        {
            "schema": "brigade.run_lock.v1",
            "owner_token": "owner",
            "pid": pid,
            "run_dir": str(run_dir.resolve()),
            "acquired_at": "2026-07-16T00:00:00+00:00",
        },
    )
    return lock_path


def _activate_recovery_journal_with_checkpoint(
    workspace: Path,
    run_dir: Path,
    run_json_obj: dict,
    *,
    paired_event_type: str | None = "run.planning.started",
    pairing_key: str | None = None,
    leave_stale_lock: bool = False,
) -> bytes:
    import shutil

    from brigade import localio, run_checkpoint, run_lifecycle, runguard

    run_dir.parent.mkdir(parents=True, exist_ok=True)
    run_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_bytes = _recovery_writer_bytes(run_json_obj)
    localio.write_json(
        run_dir / "run.json",
        {"schema": "brigade.run.v1", **run_json_obj, "lifecycle_journal_requested": True},
    )
    with runguard.run_lock(workspace, run_dir=run_dir):
        run_lifecycle.prepare_lifecycle_journal(run_dir, workspace=workspace)
        run_checkpoint.write_checkpoint(
            run_dir,
            checkpoint_bytes,
            workspace=workspace,
            paired_event_type=paired_event_type,
            pairing_key=pairing_key,
        )
    lock_path = workspace / ".brigade" / "run.lock"
    if leave_stale_lock:
        _recovery_write_lock_owner(workspace, run_dir)
    elif lock_path.exists():
        shutil.rmtree(lock_path)
    return checkpoint_bytes


def _stale_recovery_receipt(
    checkpoint_obj: dict,
    *,
    owner_pid: int = _OWNER_PID,
    recovered_at: str = _RECOVERED_AT,
    lock_workspace: str | None = None,
    lock_acquired_at: str | None = None,
) -> dict:
    from brigade import runguard

    payload = json.loads(json.dumps(checkpoint_obj))
    raw_status = payload.get("status")
    if isinstance(raw_status, str) and raw_status:
        prior_status = raw_status
    else:
        prior_status = "artifact-unavailable"
    return runguard._build_stale_lock_recovery_receipt(
        payload,
        owner_pid=owner_pid,
        recovered_at=recovered_at,
        prior_status=prior_status,
        lock_workspace=lock_workspace,
        lock_acquired_at=lock_acquired_at,
        persist_recovery_provenance=True,
    )


def _recovery_check(target: Path, *, full: bool = False) -> tuple[str, str, str]:
    return doctor_mod._check_recovery_checkpoints(target, full=full)


def _write_headroom_journal(run_dir: Path, event_count: int) -> None:
    from brigade import run_events

    journal = run_dir / "events" / "lifecycle.jsonl"
    journal.parent.mkdir(parents=True, exist_ok=True)
    previous_digest: str | None = None
    with journal.open("ab") as handle:
        for sequence in range(1, event_count + 1):
            envelope = run_events.build_event(
                run_id=run_dir.name,
                sequence=sequence,
                event_type="run.planning.started",
                payload={"detail": "measurement"},
                idempotency_key=f"headroom-{sequence}",
                recorded_at="2026-07-27T15:30:45.000000Z",
                previous_digest=previous_digest,
            )
            handle.write(run_events.canonical_bytes(envelope) + b"\n")
            previous_digest = envelope["event_digest"]


@pytest.mark.parametrize(
    ("event_count", "expected"),
    [(1535, []), (1536, [doctor_mod.WARN]), (1537, [doctor_mod.WARN]), (2048, [doctor_mod.WARN])],
)
def test_doctor_journal_event_headroom_warns_at_75_percent_boundary(
    tmp_path: Path, event_count: int, expected: list[str]
):
    from brigade import run_checkpoint

    assert run_checkpoint.MAX_JOURNAL_EVENTS == 2048
    workspace = tmp_path / "workspace"
    run_dir = workspace / ".brigade" / "runs" / f"headroom-{event_count}"
    _write_headroom_journal(run_dir, event_count)

    checks = doctor_mod._check_journal_event_headroom(workspace)

    assert [status for status, _name, _detail in checks] == expected
    if expected:
        status, name, detail = checks[0]
        assert status == doctor_mod.WARN
        assert name == "runs: journal event headroom"
        assert detail == (
            f"lifecycle journal at {event_count}/2048 events "
            f"({event_count * 100 // 2048}%); raise or segment before the hard ceiling halts appends"
        )


def test_doctor_journal_event_headroom_skips_partial_tail_and_chain_errors(tmp_path: Path):
    from brigade import run_journal

    workspace = tmp_path / "workspace"
    partial_run = workspace / ".brigade" / "runs" / "partial"
    chain_run = workspace / ".brigade" / "runs" / "chain"
    _write_headroom_journal(partial_run, 1536)
    _write_headroom_journal(chain_run, 1536)
    partial_journal = partial_run / "events" / "lifecycle.jsonl"
    chain_journal = chain_run / "events" / "lifecycle.jsonl"
    partial_journal.write_bytes(partial_journal.read_bytes() + b'{"partial":')
    chain_journal.write_bytes(chain_journal.read_bytes().replace(b'"previous_digest":"', b'"previous_digest":"f', 1))

    assert run_journal.read_journal_bounded(partial_journal).partial_tail is not None
    assert run_journal.read_journal_bounded(chain_journal).chain_errors
    assert doctor_mod._check_journal_event_headroom(workspace) == []


def test_doctor_validates_checkpoint_before_reporting_pending_dispatch(tmp_path: Path):
    from brigade import run_checkpoint, run_journal, run_lifecycle, runguard

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / "dispatch-pending"
    run_meta = {"status": "dispatching", "task": "demo", "cwd": str(workspace)}
    _activate_recovery_journal_with_checkpoint(workspace, run_dir, run_meta)
    with runguard.run_lock(workspace, run_dir=run_dir):
        run_lifecycle.record_dispatch_fact(
            run_dir,
            workspace=workspace,
            event_type="run.dispatch.requested",
            seat="coder",
        )

    verdict, reason = doctor_mod._recovery_checkpoint_run_verdict(workspace, run_dir)
    assert verdict == "warn"
    assert "at-least-once dispatch recovery required" in reason

    (run_dir / "run.json").unlink()
    verdict, reason = doctor_mod._recovery_checkpoint_run_verdict(workspace, run_dir)
    assert verdict == "warn"
    assert "run.json missing with valid checkpoint" in reason
    assert "at-least-once dispatch recovery required" in reason

    (run_dir / "run.json").write_text("not json")
    verdict, reason = doctor_mod._recovery_checkpoint_run_verdict(workspace, run_dir)
    assert verdict == "warn"
    assert "run.json unparseable with valid checkpoint" in reason
    assert "at-least-once dispatch recovery required" in reason

    latest = run_checkpoint.latest_checkpoint_event(
        run_journal.read_journal(run_lifecycle._journal_path(run_dir)).events
    )
    assert latest is not None
    checkpoint_path = run_checkpoint.checkpoint_path(run_dir, latest.payload["sha256"])
    checkpoint_path.write_bytes(b"x" * latest.payload["byte_size"])

    verdict, reason = doctor_mod._recovery_checkpoint_run_verdict(workspace, run_dir)
    assert verdict == "fail"
    assert "at-least-once" not in reason


@pytest.mark.parametrize(
    "event_type",
    [
        "run.dispatch.requested",
        "run.dispatch.observed",
        "run.dispatch.completed",
        "run.dispatch.failed",
    ],
)
def test_doctor_fails_dispatch_pairing_checkpoint_at_tail_as_incomplete(tmp_path: Path, event_type: str):
    from brigade import run_checkpoint

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / f"dispatch-incomplete-{event_type.rsplit('.', 1)[-1]}"
    run_meta = {"status": "dispatching", "task": "demo", "cwd": str(workspace)}
    _activate_recovery_journal_with_checkpoint(
        workspace,
        run_dir,
        run_meta,
        paired_event_type=event_type,
        pairing_key=run_checkpoint.dispatch_pairing_key(event_type, "coder", 1),
    )

    verdict, reason = doctor_mod._recovery_checkpoint_run_verdict(workspace, run_dir)

    assert verdict == "fail"
    assert "incomplete" in reason


def _snapshot_run_tree(run_dir: Path) -> dict[str, object]:
    from brigade import run_checkpoint

    entries = sorted(p.name for p in run_dir.iterdir()) if run_dir.is_dir() else []
    journal = run_dir / "events" / "lifecycle.jsonl"
    run_json = run_dir / "run.json"
    checkpoint_dir = run_dir / "events" / run_checkpoint.CHECKPOINT_DIR_NAME
    checkpoint_bytes = (
        {str(path.relative_to(run_dir)): path.read_bytes() for path in checkpoint_dir.rglob("*") if path.is_file()}
        if checkpoint_dir.is_dir()
        else {}
    )
    return {
        "entries": entries,
        "journal": journal.read_bytes() if journal.is_file() else b"",
        "run_json": run_json.read_bytes() if run_json.is_file() else b"",
        "checkpoints": checkpoint_bytes,
    }


def test_doctor_includes_recovery_checkpoints_check(tmp_path: Path):
    from brigade.station import DoctorContext

    ctx = DoctorContext(target=tmp_path, selection=None, harnesses=[])
    checks = doctor_mod.core_station_checks(ctx)
    recovery = [check for check in checks if check[1] == "runs: recovery checkpoints"]
    assert len(recovery) == 1
    status, name, detail = recovery[0]
    assert len(recovery[0]) == 3
    assert name == "runs: recovery checkpoints"
    assert status in {doctor_mod.OK, doctor_mod.WARN, doctor_mod.FAIL}
    assert "ok=" in detail and "omitted=" in detail


def test_doctor_scans_at_most_50_immediate_run_dirs_newest_first(tmp_path: Path):
    import os

    workspace = tmp_path / "workspace"
    runs_root = workspace / ".brigade" / "runs"
    runs_root.mkdir(parents=True)
    base_mtime = 1_700_000_000.0
    for index in range(55):
        run_dir = runs_root / f"run-{index:03d}"
        run_obj = {"status": "planning", "cwd": str(workspace), "task": f"t{index}"}
        checkpoint_bytes = _activate_recovery_journal_with_checkpoint(workspace, run_dir, run_obj)
        (run_dir / "run.json").write_bytes(checkpoint_bytes)
        os.utime(run_dir, (base_mtime + index, base_mtime + index))

    status, _name, detail = _recovery_check(workspace)
    assert status == doctor_mod.OK
    assert "ok=50" in detail
    assert "omitted=5" in detail


def test_doctor_fail_on_bound_exceeded_without_parsing_further(tmp_path: Path, monkeypatch):
    import hashlib
    import os

    from brigade import run_checkpoint, run_journal, run_lifecycle, runguard

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    run_json_obj = {"status": "planning", "cwd": str(workspace), "task": "demo"}
    checkpoint_bytes = _activate_recovery_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    (run_dir / "run.json").write_bytes(checkpoint_bytes)
    journal_path = run_lifecycle._journal_path(run_dir)

    real_fstat = os.fstat

    def big_journal_fstat(fd):
        st = real_fstat(fd)
        if st.st_size <= run_checkpoint.MAX_JOURNAL_BYTES:
            return st
        return os.stat_result(
            (
                st.st_mode,
                st.st_ino,
                st.st_dev,
                st.st_nlink,
                st.st_uid,
                st.st_gid,
                run_checkpoint.MAX_JOURNAL_BYTES + 1,
                st.st_atime,
                st.st_mtime,
                st.st_ctime,
            )
        )

    journal_path.write_bytes(journal_path.read_bytes() + b"x" * (run_checkpoint.MAX_JOURNAL_BYTES + 1))
    monkeypatch.setattr(os, "fstat", big_journal_fstat)

    status, _name, detail = _recovery_check(workspace)
    assert status == doctor_mod.FAIL
    assert "bound exceeded" in detail

    run_dir2 = workspace / ".brigade" / "runs" / "big-checkpoint"
    run_dir2.mkdir(parents=True)
    with runguard.run_lock(workspace, run_dir=run_dir2):
        run_lifecycle.prepare_lifecycle_journal(run_dir2, workspace=workspace)
    sha = hashlib.sha256(checkpoint_bytes).hexdigest()
    payload = {
        "path": f"events/recovery-checkpoints/{sha}.json",
        "sha256": sha,
        "media_type": run_checkpoint.CHECKPOINT_MEDIA_TYPE,
        "byte_size": run_checkpoint.MAX_CHECKPOINT_BYTES + 1,
        "privacy_class": run_checkpoint.CHECKPOINT_PRIVACY_CLASS,
        "paired_event_type": None,
    }
    journal2 = run_lifecycle._journal_path(run_dir2)
    run_journal.append_event(
        journal2,
        run_id=run_dir2.name,
        event_type=run_checkpoint.CHECKPOINT_EVENT_TYPE,
        payload=payload,
        idempotency_key=f"checkpoint:{sha}:none",
        expected_previous_sequence=0,
    )
    (run_dir2 / "run.json").write_bytes(checkpoint_bytes)

    status2, _name2, detail2 = _recovery_check(workspace)
    assert status2 == doctor_mod.FAIL
    assert "bound exceeded" in detail2


def test_doctor_fail_on_partial_tail_without_quarantine(tmp_path: Path, monkeypatch):
    from brigade import run_checkpoint, run_journal, run_lifecycle

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    run_json_obj = {"status": "planning", "cwd": str(workspace), "task": "demo"}
    checkpoint_bytes = _activate_recovery_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    (run_dir / "run.json").write_bytes(checkpoint_bytes)
    journal = run_lifecycle._journal_path(run_dir)
    partial = b'{"schema":"brigade.run_event.v1","event_type":"run.plan'
    journal.write_bytes(journal.read_bytes() + partial)

    def _forbid_recover_partial_tail(*_args, **_kwargs):
        raise AssertionError("recover_partial_tail must not be called from doctor")

    monkeypatch.setattr(run_journal, "recover_partial_tail", _forbid_recover_partial_tail)
    monkeypatch.setattr(
        run_checkpoint,
        "recover_from_checkpoint",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("recover_from_checkpoint")),
    )

    status, _name, detail = _recovery_check(workspace)
    assert status == doctor_mod.FAIL
    assert _RUN_ID in detail


def test_doctor_per_run_verdicts(tmp_path: Path):
    import os

    from brigade import runguard

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runs_root = workspace / ".brigade" / "runs"

    ok_dir = runs_root / "ok-run"
    ok_obj = {"status": "planning", "cwd": str(workspace), "task": "ok"}
    ok_bytes = _activate_recovery_journal_with_checkpoint(workspace, ok_dir, ok_obj)
    (ok_dir / "run.json").write_bytes(ok_bytes)

    warn_dir = runs_root / "warn-run"
    warn_obj = {"status": "planning", "cwd": str(workspace), "task": "warn"}
    _activate_recovery_journal_with_checkpoint(workspace, warn_dir, warn_obj)
    (warn_dir / "run.json").unlink()

    fail_dir = runs_root / "fail-run"
    fail_obj = {"status": "planning", "cwd": str(workspace), "task": "fail"}
    fail_bytes = _activate_recovery_journal_with_checkpoint(workspace, fail_dir, fail_obj)
    mismatched = json.loads(fail_bytes.decode("utf-8"))
    mismatched["task"] = "wrong-task"
    (fail_dir / "run.json").write_bytes(_recovery_writer_bytes(mismatched))

    legacy_dir = runs_root / "legacy-run"
    legacy_dir.mkdir(parents=True)
    _recovery_write_json(legacy_dir / "run.json", {"status": "ok", "cwd": str(workspace)})

    live_dir = runs_root / "live-run"
    live_obj = {"status": "planning", "cwd": str(workspace), "task": "live"}
    live_bytes = _activate_recovery_journal_with_checkpoint(workspace, live_dir, live_obj)
    (live_dir / "run.json").write_bytes(live_bytes)
    _recovery_write_lock_owner(workspace, live_dir, pid=os.getpid())

    status, _name, detail = _recovery_check(workspace)
    assert status == doctor_mod.FAIL
    assert "ok=2" in detail
    assert "warn=1" in detail
    assert "fail=1" in detail
    assert "omitted=1" in detail
    assert "fail-run (run.json does not match checkpoint)" in detail
    assert runguard.run_lock_state(workspace, live_dir) == "live"


def test_doctor_accepts_valid_stale_recovery_terminal_receipt(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    checkpoint_obj = {
        "status": "dispatching",
        "cwd": str(workspace),
        "task": "inspect",
        "orchestrator": "chef",
        "active_seats": ["coder"],
    }
    checkpoint_bytes = _activate_recovery_journal_with_checkpoint(workspace, run_dir, checkpoint_obj)
    receipt = _stale_recovery_receipt(
        json.loads(checkpoint_bytes.decode("utf-8")),
        lock_workspace=str(workspace),
        lock_acquired_at="2026-07-16T00:00:00+00:00",
    )
    (run_dir / "run.json").write_bytes(_recovery_writer_bytes(receipt))

    status, _name, detail = _recovery_check(workspace)
    assert status == doctor_mod.OK
    assert "fail=0" in detail

    bad_receipt = dict(receipt)
    bad_receipt["error"] = "wrong detail"
    (run_dir / "run.json").write_bytes(_recovery_writer_bytes(bad_receipt))

    status2, _name2, detail2 = _recovery_check(workspace)
    assert status2 == doctor_mod.FAIL
    assert _RUN_ID in detail2
    assert "run.json does not match checkpoint" in detail2


def test_doctor_accepts_stale_recovery_receipt_with_orchestrator_attribution(tmp_path: Path):
    """A planning checkpoint with no ``active_seats`` attributes the stale-lock
    failure to the orchestrator, matching production ``_recover_run_artifact``.

    The test helper ``_stale_recovery_receipt`` must apply the same
    phase_owner -> worker -> orchestrator precedence as production and the
    doctor reconstruction, or the candidate receipt diverges and the verdict
    fails for the wrong reason.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    checkpoint_obj = {
        "status": "planning",
        "cwd": str(workspace),
        "task": "inspect",
        "orchestrator": "chef",
    }
    checkpoint_bytes = _activate_recovery_journal_with_checkpoint(workspace, run_dir, checkpoint_obj)
    receipt = _stale_recovery_receipt(
        json.loads(checkpoint_bytes.decode("utf-8")),
        lock_workspace=str(workspace),
        lock_acquired_at="2026-07-16T00:00:00+00:00",
    )
    (run_dir / "run.json").write_bytes(_recovery_writer_bytes(receipt))

    status, _name, detail = _recovery_check(workspace)
    assert status == doctor_mod.OK, detail
    assert "fail=0" in detail


def test_doctor_accepts_stale_recovery_receipt_with_phase_owner_attribution(tmp_path: Path):
    """A result-processing checkpoint attributes the failure to ``phase_owner``,
    taking precedence over worker/orchestrator, matching production precedence.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    checkpoint_obj = {
        "status": "result-processing",
        "cwd": str(workspace),
        "task": "inspect",
        "phase_owner": "reviewer",
        "worker": "coder",
        "orchestrator": "chef",
    }
    checkpoint_bytes = _activate_recovery_journal_with_checkpoint(workspace, run_dir, checkpoint_obj)
    receipt = _stale_recovery_receipt(
        json.loads(checkpoint_bytes.decode("utf-8")),
        lock_workspace=str(workspace),
        lock_acquired_at="2026-07-16T00:00:00+00:00",
    )
    (run_dir / "run.json").write_bytes(_recovery_writer_bytes(receipt))

    status, _name, detail = _recovery_check(workspace)
    assert status == doctor_mod.OK, detail
    assert "fail=0" in detail


def test_doctor_checkpoint_check_never_mutates(tmp_path: Path, monkeypatch):
    from brigade import run_checkpoint, run_journal, run_lifecycle

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    run_json_obj = {"status": "planning", "cwd": str(workspace), "task": "demo"}
    checkpoint_bytes = _activate_recovery_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    (run_dir / "run.json").write_bytes(checkpoint_bytes)
    journal = run_lifecycle._journal_path(run_dir)
    journal.write_bytes(journal.read_bytes() + b'{"partial":')

    before = _snapshot_run_tree(run_dir)

    def _forbid_recover_partial_tail(*_args, **_kwargs):
        raise AssertionError("recover_partial_tail must not be called from doctor")

    monkeypatch.setattr(run_journal, "recover_partial_tail", _forbid_recover_partial_tail)
    monkeypatch.setattr(
        run_checkpoint,
        "recover_from_checkpoint",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("recover_from_checkpoint")),
    )

    _recovery_check(workspace)
    after = _snapshot_run_tree(run_dir)
    assert before == after


def test_doctor_recovery_failures_default_8_versus_full_all(tmp_path: Path):
    import os

    workspace = tmp_path / "workspace"
    runs_root = workspace / ".brigade" / "runs"
    runs_root.mkdir(parents=True)
    base_mtime = 1_700_000_000.0
    for index in range(12):
        run_dir = runs_root / f"fail-{index:02d}"
        run_obj = {"status": "planning", "cwd": str(workspace), "task": f"f{index}"}
        checkpoint_bytes = _activate_recovery_journal_with_checkpoint(workspace, run_dir, run_obj)
        mismatched = json.loads(checkpoint_bytes.decode("utf-8"))
        mismatched["task"] = f"wrong-{index}"
        (run_dir / "run.json").write_bytes(_recovery_writer_bytes(mismatched))
        os.utime(run_dir, (base_mtime + index, base_mtime + index))

    _status, _name, default_detail = _recovery_check(workspace, full=False)
    assert "fail=12" in default_detail
    assert "failures:" in default_detail
    assert "fail-11" in default_detail
    assert "fail-03" not in default_detail
    assert "4 more" in default_detail

    _status2, _name2, full_detail = _recovery_check(workspace, full=True)
    for index in range(12):
        assert f"fail-{index:02d}" in full_detail
    assert "more" not in full_detail


# -- Sendback: independent-review blockers and lows for the Doctor diff --


def _activate_mismatched_recovery_run(workspace: Path, run_dir: Path, run_obj: dict, *, task: str) -> None:
    """Activate a journal+checkpoint, then overwrite run.json with a mismatched task."""
    checkpoint_bytes = _activate_recovery_journal_with_checkpoint(workspace, run_dir, run_obj)
    mismatched = json.loads(checkpoint_bytes.decode("utf-8"))
    mismatched["task"] = task
    (run_dir / "run.json").write_bytes(_recovery_writer_bytes(mismatched))


def _install_clean_workspace(target: Path) -> None:
    install_selection(
        target,
        Selection(depth="workspace", harnesses=["claude"], owner="claude", includes=[]),
    )


def test_doctor_run_full_false_previews_8_recovery_failures(tmp_path: Path, capsys):
    """End-to-end ``doctor_mod.run(full=False)`` previews 8 of 12 failures."""
    import os

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _install_clean_workspace(workspace)
    runs_root = workspace / ".brigade" / "runs"
    runs_root.mkdir(parents=True)
    base_mtime = 1_700_000_000.0
    for index in range(12):
        run_dir = runs_root / f"fail-{index:02d}"
        _activate_mismatched_recovery_run(
            workspace,
            run_dir,
            {"status": "planning", "cwd": str(workspace), "task": f"f{index}"},
            task=f"wrong-{index}",
        )
        os.utime(run_dir, (base_mtime + index, base_mtime + index))

    capsys.readouterr()
    rc = doctor_mod.run(target=workspace, harness="generic", full=False)
    out = capsys.readouterr().out
    assert rc == 1
    assert "runs: recovery checkpoints" in out
    assert "fail=12" in out
    assert "fail-11" in out
    assert "fail-04" in out
    assert "fail-03" not in out
    assert "4 more" in out

    capsys.readouterr()
    doctor_mod.run(target=workspace, harness="generic", json_output=True, full=False)
    payload = json.loads(capsys.readouterr().out)
    recovery = next(c for c in payload["checks"] if c["name"] == "runs: recovery checkpoints")
    assert recovery["status"] == "FAIL"
    assert "fail=12" in recovery["detail"]
    assert "fail-11" in recovery["detail"]
    assert "fail-03" not in recovery["detail"]
    assert "4 more" in recovery["detail"]


def test_doctor_run_full_true_lists_every_recovery_failure(tmp_path: Path, capsys):
    """End-to-end ``doctor_mod.run(full=True)`` lists every failure name."""
    import os

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _install_clean_workspace(workspace)
    runs_root = workspace / ".brigade" / "runs"
    runs_root.mkdir(parents=True)
    base_mtime = 1_700_000_000.0
    for index in range(12):
        run_dir = runs_root / f"fail-{index:02d}"
        _activate_mismatched_recovery_run(
            workspace,
            run_dir,
            {"status": "planning", "cwd": str(workspace), "task": f"f{index}"},
            task=f"wrong-{index}",
        )
        os.utime(run_dir, (base_mtime + index, base_mtime + index))

    capsys.readouterr()
    rc = doctor_mod.run(target=workspace, harness="generic", full=True)
    out = capsys.readouterr().out
    assert rc == 1
    assert "fail=12" in out
    for index in range(12):
        assert f"fail-{index:02d}" in out
    assert " more" not in out

    capsys.readouterr()
    doctor_mod.run(target=workspace, harness="generic", json_output=True, full=True)
    payload = json.loads(capsys.readouterr().out)
    recovery = next(c for c in payload["checks"] if c["name"] == "runs: recovery checkpoints")
    assert recovery["status"] == "FAIL"
    assert "fail=12" in recovery["detail"]
    for index in range(12):
        assert f"fail-{index:02d}" in recovery["detail"]
    assert " more" not in recovery["detail"]


def test_doctor_fails_on_forged_prior_status(tmp_path: Path):
    """A stale-lock-recovery receipt with a forged ``prior_status`` must FAIL.

    ``prior_status`` is derived from the checkpoint object's status exactly as
    runguard derives it; the candidate's ``failure.prior_status`` is never
    trusted. A forged value diverges from the reconstruction.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    checkpoint_obj = {
        "status": "planning",
        "cwd": str(workspace),
        "task": "inspect",
        "orchestrator": "chef",
    }
    checkpoint_bytes = _activate_recovery_journal_with_checkpoint(workspace, run_dir, checkpoint_obj)
    receipt = _stale_recovery_receipt(
        json.loads(checkpoint_bytes.decode("utf-8")),
        lock_workspace=str(workspace),
        lock_acquired_at="2026-07-16T00:00:00+00:00",
    )
    receipt["failure"]["prior_status"] = "forged-status"
    (run_dir / "run.json").write_bytes(_recovery_writer_bytes(receipt))

    status, _name, detail = _recovery_check(workspace)
    assert status == doctor_mod.FAIL
    assert _RUN_ID in detail
    assert "run.json does not match checkpoint" in detail


def test_doctor_raw_journal_oserror_is_bounded_fail(tmp_path: Path, monkeypatch):
    """A raw ``OSError`` from ``read_journal_bounded`` must be a bounded FAIL."""
    from brigade import run_journal, run_lifecycle

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    run_obj = {"status": "planning", "cwd": str(workspace), "task": "demo"}
    _activate_mismatched_recovery_run(workspace, run_dir, run_obj, task="wrong")

    monkeypatch.setattr(run_journal, "read_journal_bounded", lambda _p: (_ for _ in ()).throw(OSError("simulated")))

    status, _name, detail = _recovery_check(workspace)
    assert status == doctor_mod.FAIL
    assert "journal unreadable" in detail
    journal = run_lifecycle._journal_path(run_dir)
    assert journal.is_file()


def test_doctor_run_json_present_but_unreadable_is_fail(tmp_path: Path, monkeypatch):
    """A present-but-unreadable ``run.json`` must FAIL (not WARN)."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    run_obj = {"status": "planning", "cwd": str(workspace), "task": "demo"}
    checkpoint_bytes = _activate_recovery_journal_with_checkpoint(workspace, run_dir, run_obj)
    (run_dir / "run.json").write_bytes(checkpoint_bytes)

    real_read_bytes = Path.read_bytes

    def _unreadable_run_json(self: Path) -> bytes:
        if self.name == "run.json":
            raise OSError("permission denied")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", _unreadable_run_json)

    status, _name, detail = _recovery_check(workspace)
    assert status == doctor_mod.FAIL
    assert "run.json present but unreadable" in detail
    assert (run_dir / "run.json").exists()


def test_doctor_run_json_recursion_error_is_warn(tmp_path: Path):
    """A ``RecursionError`` while parsing ``run.json`` is an unparseable WARN."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    run_obj = {"status": "planning", "cwd": str(workspace), "task": "demo"}
    _activate_recovery_journal_with_checkpoint(workspace, run_dir, run_obj)
    nesting = 2000
    blob = "{" * nesting + '"v":1' + "}" * nesting
    (run_dir / "run.json").write_text(blob)

    status, _name, detail = _recovery_check(workspace)
    assert status == doctor_mod.WARN
    assert "warn=1" in detail
    assert "fail=0" in detail


def test_doctor_immediate_run_dir_stat_oserror_is_omitted(tmp_path: Path, monkeypatch):
    """A vanishing/stat OSError during immediate run scanning must not crash Doctor."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runs_root = workspace / ".brigade" / "runs"
    runs_root.mkdir(parents=True)
    run_dir = runs_root / "fail-run"
    run_obj = {"status": "planning", "cwd": str(workspace), "task": "demo"}
    _activate_mismatched_recovery_run(workspace, run_dir, run_obj, task="wrong")

    real_stat = Path.stat

    def _flaky_stat(self: Path, *args, **kwargs):
        if self == run_dir:
            raise OSError("vanished mid-scan")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", _flaky_stat)
    status, _name, detail = _recovery_check(workspace)
    assert status in {doctor_mod.OK, doctor_mod.WARN}
    assert "omitted=1" in detail
    assert "fail=0" in detail
    monkeypatch.undo()

    real_iterdir = Path.iterdir

    def _flaky_iterdir(self: Path):
        if self == runs_root:
            raise OSError("iterdir boom")
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", _flaky_iterdir)
    status2, _name2, detail2 = _recovery_check(workspace)
    assert status2 in {doctor_mod.OK, doctor_mod.WARN}
    assert "fail=0" in detail2
    monkeypatch.undo()

    real_is_dir = Path.is_dir

    def _flaky_is_dir(self: Path, *args, **kwargs):
        if self == runs_root:
            raise OSError("is_dir boom")
        return real_is_dir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "is_dir", _flaky_is_dir)
    status3, _name3, detail3 = _recovery_check(workspace)
    assert status3 in {doctor_mod.OK, doctor_mod.WARN}
    assert "fail=0" in detail3


def test_doctor_invalid_recovered_at_iso_is_fail(tmp_path: Path):
    """An invalid ISO ``recovered_at`` must FAIL the stale-lock-recovery match."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    checkpoint_obj = {"status": "planning", "cwd": str(workspace), "task": "inspect"}
    checkpoint_bytes = _activate_recovery_journal_with_checkpoint(workspace, run_dir, checkpoint_obj)
    receipt = _stale_recovery_receipt(
        json.loads(checkpoint_bytes.decode("utf-8")),
        lock_workspace=str(workspace),
        lock_acquired_at="2026-07-16T00:00:00+00:00",
        recovered_at="not-a-timestamp",
    )
    (run_dir / "run.json").write_bytes(_recovery_writer_bytes(receipt))

    status, _name, detail = _recovery_check(workspace)
    assert status == doctor_mod.FAIL
    assert "run.json does not match checkpoint" in detail


def test_doctor_events_above_ceiling_fail_bound_exceeded_without_checkpoint_parsing(tmp_path: Path, monkeypatch):
    """A valid chain above the ceiling must FAIL before checkpoint parsing."""
    from brigade import run_checkpoint, run_events, run_lifecycle

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    run_dir.parent.mkdir(parents=True, exist_ok=True)
    run_dir.mkdir(parents=True)
    _recovery_write_json(
        run_dir / "run.json",
        {"schema": "brigade.run.v1", "status": "planning", "cwd": str(workspace), "task": "demo"},
    )
    journal = run_lifecycle._journal_path(run_dir)
    journal.parent.mkdir(parents=True, exist_ok=True)
    previous_digest: str | None = None
    with journal.open("ab") as handle:
        for index in range(run_checkpoint.MAX_JOURNAL_EVENTS + 1):
            env = run_events.build_event(
                run_id=_RUN_ID,
                sequence=index + 1,
                event_type="run.planning.started",
                payload={"detail": f"step-{index}"},
                idempotency_key=f"progress-{index}",
                recorded_at="2026-07-27T15:30:45.000000Z",
                previous_digest=previous_digest,
            )
            handle.write(run_events.canonical_bytes(env) + b"\n")
            previous_digest = env["event_digest"]

    monkeypatch.setattr(
        run_checkpoint,
        "validate_checkpoint",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("validate_checkpoint")),
    )

    status, _name, detail = _recovery_check(workspace)
    assert status == doctor_mod.FAIL
    assert "bound exceeded" in detail


def test_doctor_sparse_checkpoint_over_16mib_fail_bound_exceeded_before_body(tmp_path: Path):
    """A real sparse checkpoint file > 16 MiB must FAIL bound exceeded before body parse."""
    import hashlib
    import os as _os

    from brigade import run_checkpoint, run_journal

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    run_obj = {"status": "planning", "cwd": str(workspace), "task": "demo"}
    checkpoint_bytes = _activate_recovery_journal_with_checkpoint(workspace, run_dir, run_obj)
    (run_dir / "run.json").write_bytes(checkpoint_bytes)
    sha = hashlib.sha256(checkpoint_bytes).hexdigest()
    final_path = run_checkpoint.checkpoint_path(run_dir, sha)
    assert final_path.is_file()
    fd = run_journal._open_nofollow(final_path, _os.O_WRONLY)
    try:
        _os.ftruncate(fd, run_checkpoint.MAX_CHECKPOINT_BYTES + 1)
    finally:
        _os.close(fd)
    assert final_path.stat().st_size == run_checkpoint.MAX_CHECKPOINT_BYTES + 1

    status, _name, detail = _recovery_check(workspace)
    assert status == doctor_mod.FAIL
    assert "bound exceeded" in detail


def test_doctor_declared_checkpoint_byte_size_bound_exceeded(tmp_path: Path):
    """A checkpoint event declaring ``byte_size > MAX`` must FAIL bound exceeded."""
    import hashlib

    from brigade import run_checkpoint, run_journal, run_lifecycle

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / "declared-bound"
    run_obj = {"status": "planning", "cwd": str(workspace), "task": "demo"}
    checkpoint_bytes = _activate_recovery_journal_with_checkpoint(workspace, run_dir, run_obj)
    (run_dir / "run.json").write_bytes(checkpoint_bytes)
    sha = hashlib.sha256(checkpoint_bytes).hexdigest()
    payload = {
        "path": f"events/{run_checkpoint.CHECKPOINT_DIR_NAME}/{sha}.json",
        "sha256": sha,
        "media_type": run_checkpoint.CHECKPOINT_MEDIA_TYPE,
        "byte_size": run_checkpoint.MAX_CHECKPOINT_BYTES + 1,
        "privacy_class": run_checkpoint.CHECKPOINT_PRIVACY_CLASS,
        "paired_event_type": None,
    }
    journal = run_lifecycle._journal_path(run_dir)
    run_journal.append_event(
        journal,
        run_id=run_dir.name,
        event_type=run_checkpoint.CHECKPOINT_EVENT_TYPE,
        payload=payload,
        idempotency_key=f"checkpoint:{sha}:none",
        expected_previous_sequence=1,
    )

    status, _name, detail = _recovery_check(workspace)
    assert status == doctor_mod.FAIL
    assert "bound exceeded" in detail


def test_doctor_checkpoint_check_never_mutates_across_new_failures(tmp_path: Path, monkeypatch):
    """The verdict must stay read-only across raw OSError, unreadable run.json, and recursion."""
    from brigade import run_journal

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    run_obj = {"status": "planning", "cwd": str(workspace), "task": "demo"}
    checkpoint_bytes = _activate_recovery_journal_with_checkpoint(workspace, run_dir, run_obj)
    (run_dir / "run.json").write_bytes(checkpoint_bytes)

    def _tree_snapshot() -> dict[str, object]:
        # Read via builtin open() so a Path.read_bytes monkeypatch cannot mask reads.
        from brigade import run_checkpoint as _rc

        def _read_bytes(path: Path) -> bytes:
            if not path.is_file():
                return b""
            with open(path, "rb") as fh:
                return fh.read()

        entries = sorted(p.name for p in run_dir.iterdir()) if run_dir.is_dir() else []
        journal = run_dir / "events" / "lifecycle.jsonl"
        run_json = run_dir / "run.json"
        checkpoint_dir = run_dir / "events" / _rc.CHECKPOINT_DIR_NAME
        checkpoints: dict[str, bytes] = {}
        if checkpoint_dir.is_dir():
            for p in checkpoint_dir.rglob("*"):
                if p.is_file():
                    checkpoints[str(p.relative_to(run_dir))] = _read_bytes(p)
        return {
            "entries": entries,
            "journal": _read_bytes(journal),
            "run_json": _read_bytes(run_json),
            "checkpoints": checkpoints,
        }

    real_read_bytes = Path.read_bytes

    def _unreadable_run_json(self: Path) -> bytes:
        if self.name == "run.json":
            raise OSError("permission denied")
        return real_read_bytes(self)

    nesting = 2000
    recursion_blob = ("{" * nesting + '"v":1' + "}" * nesting).encode("utf-8")

    scenarios = [
        (
            "raw journal OSError",
            lambda: monkeypatch.setattr(
                run_journal, "read_journal_bounded", lambda _p: (_ for _ in ()).throw(OSError("boom"))
            ),
        ),
        ("unreadable run.json", lambda: monkeypatch.setattr(Path, "read_bytes", _unreadable_run_json)),
        ("recursion run.json", lambda: (run_dir / "run.json").write_bytes(recursion_blob)),
    ]
    for _label, scenario in scenarios:
        scenario()
        before = _tree_snapshot()
        _recovery_check(workspace)
        after = _tree_snapshot()
        assert before == after, f"verdict mutated the run tree under {_label}"
        (run_dir / "run.json").write_bytes(checkpoint_bytes)
        monkeypatch.undo()


# -- Issue #568 slice 6 Task 6: authority-aware reproject-before-compare -------


def _activate_authority_recovery_journal_with_checkpoint(
    workspace: Path,
    run_dir: Path,
    base_obj: dict,
    *,
    paired_event_type: str | None = "run.planning.started",
    leave_stale_lock: bool = False,
):
    """Activate the journal and append one base-stripped authority checkpoint.

    Bootstrap run.json carries only the lifecycle journal request; the
    authority request lives in the checkpointed base (both durable request
    fields True). The checkpoint is written with ``body_kind="base-stripped"``
    so the stored bytes are the canonical stripped base and the journal
    payload carries ``body_kind``. Returns the appended checkpoint RunEvent.
    """
    import shutil

    from brigade import localio, run_checkpoint, run_lifecycle, runguard

    run_dir.parent.mkdir(parents=True, exist_ok=True)
    run_dir.mkdir(parents=True, exist_ok=True)
    base_bytes = _recovery_writer_bytes(base_obj)
    localio.write_json(
        run_dir / "run.json",
        {"schema": "brigade.run.v1", **base_obj, "lifecycle_journal_requested": True},
    )
    with runguard.run_lock(workspace, run_dir=run_dir):
        run_lifecycle.prepare_lifecycle_journal(run_dir, workspace=workspace)
        event = run_checkpoint.write_checkpoint(
            run_dir,
            base_bytes,
            workspace=workspace,
            paired_event_type=paired_event_type,
            body_kind="base-stripped",
        )
    assert event is not None
    lock_path = workspace / ".brigade" / "run.lock"
    if leave_stale_lock:
        _recovery_write_lock_owner(workspace, run_dir)
    elif lock_path.exists():
        shutil.rmtree(lock_path)
    return event


def _authority_base(workspace: Path, **overrides) -> dict:
    base = {
        "schema": "brigade.run.v1",
        "status": "planning",
        "task": "demo",
        "cwd": str(workspace),
        "orchestrator": "chef",
        "lifecycle_journal_requested": True,
        "run_journal_authority_requested": True,
    }
    base.update(overrides)
    return base


def _authority_projected(run_dir: Path, event):
    """Reproject the stripped checkpoint base over the verified journal events."""
    from brigade import run_checkpoint, run_journal, run_lifecycle, run_projector

    stripped = run_checkpoint.checkpoint_path(run_dir, event.payload["sha256"]).read_bytes()
    base = json.loads(stripped)
    events = run_journal.read_journal(run_lifecycle._journal_path(run_dir)).events
    projection = run_projector.project_run_snapshot(base, events, journal_present=True)
    return projection


def test_doctor_authority_base_stripped_matching_projection_is_ok(tmp_path: Path):
    """A run.json equal to the projected snapshot is OK (not the stripped bytes)."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    base = _authority_base(workspace)
    event = _activate_authority_recovery_journal_with_checkpoint(workspace, run_dir, base)
    projected = _authority_projected(run_dir, event)
    (run_dir / "run.json").write_bytes(projected.to_bytes())

    status, _name, detail = _recovery_check(workspace)
    assert status == doctor_mod.OK, detail
    assert "fail=0" in detail
    # The stripped checkpoint bytes are NOT the projected bytes; the verdict
    # must compare the projection, not the stripped body.
    stripped = (
        __import__("brigade.run_checkpoint", fromlist=["checkpoint_path"])
        .checkpoint_path(run_dir, event.payload["sha256"])
        .read_bytes()
    )
    assert stripped != projected.to_bytes()


def test_doctor_authority_base_stripped_missing_run_json_is_warn(tmp_path: Path):
    """A missing run.json is WARN only when projection succeeds."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    base = _authority_base(workspace)
    _activate_authority_recovery_journal_with_checkpoint(workspace, run_dir, base)
    (run_dir / "run.json").unlink()

    status, _name, detail = _recovery_check(workspace)
    assert status == doctor_mod.WARN
    assert "warn=1" in detail
    assert "fail=0" in detail
    assert not (run_dir / "run.json").exists()


def test_doctor_authority_base_stripped_unparseable_run_json_is_warn(tmp_path: Path):
    """An unparseable run.json is WARN only when projection succeeds."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    base = _authority_base(workspace)
    _activate_authority_recovery_journal_with_checkpoint(workspace, run_dir, base)
    (run_dir / "run.json").write_text("not json at all")

    status, _name, detail = _recovery_check(workspace)
    assert status == doctor_mod.WARN
    assert "warn=1" in detail
    assert "fail=0" in detail


def test_doctor_authority_base_stripped_projection_failure_is_fail(tmp_path: Path, monkeypatch):
    """A projection failure is FAIL even when run.json would otherwise match."""
    from brigade import run_projector

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    base = _authority_base(workspace)
    event = _activate_authority_recovery_journal_with_checkpoint(workspace, run_dir, base)
    projected = _authority_projected(run_dir, event)
    (run_dir / "run.json").write_bytes(projected.to_bytes())

    def boom(_base, _events, *, journal_present):
        raise run_projector.ProjectionError("raw projector detail " + "x" * 500)

    monkeypatch.setattr(run_projector, "project_run_snapshot", boom)

    status, _name, detail = _recovery_check(workspace)
    assert status == doctor_mod.FAIL
    assert "projection failed" in detail
    # run.json is untouched (doctor never writes).
    assert (run_dir / "run.json").read_bytes() == projected.to_bytes()


def test_doctor_authority_base_stripped_legacy_full_verbatim_is_ok(tmp_path: Path):
    """Absent body_kind remains the legacy verbatim compare (no projection)."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    run_obj = {"status": "planning", "cwd": str(workspace), "task": "demo"}
    checkpoint_bytes = _activate_recovery_journal_with_checkpoint(workspace, run_dir, run_obj)
    (run_dir / "run.json").write_bytes(checkpoint_bytes)

    status, _name, detail = _recovery_check(workspace)
    assert status == doctor_mod.OK, detail
    assert "fail=0" in detail


def test_doctor_authority_base_stripped_stale_lock_recovery_exact_reconstruction_is_ok(tmp_path: Path):
    """A stale-lock-recovery receipt built from the projected base is OK."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    base = _authority_base(workspace)
    event = _activate_authority_recovery_journal_with_checkpoint(workspace, run_dir, base)
    projected = _authority_projected(run_dir, event)
    receipt = _stale_recovery_receipt(
        projected.snapshot,
        lock_workspace=str(workspace),
        lock_acquired_at="2026-07-16T00:00:00+00:00",
    )
    (run_dir / "run.json").write_bytes(_recovery_writer_bytes(receipt))

    status, _name, detail = _recovery_check(workspace)
    assert status == doctor_mod.OK, detail
    assert "fail=0" in detail

    bad_receipt = dict(receipt)
    bad_receipt["error"] = "wrong detail"
    (run_dir / "run.json").write_bytes(_recovery_writer_bytes(bad_receipt))

    status2, _name2, detail2 = _recovery_check(workspace)
    assert status2 == doctor_mod.FAIL
    assert _RUN_ID in detail2
    assert "does not match" in detail2


def test_doctor_authority_base_stripped_mismatch_is_fail(tmp_path: Path):
    """A run.json that matches neither the projection nor a recovery receipt is FAIL."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    base = _authority_base(workspace)
    _activate_authority_recovery_journal_with_checkpoint(workspace, run_dir, base)
    mismatched = _authority_base(workspace, task="wrong-task")
    (run_dir / "run.json").write_bytes(_recovery_writer_bytes(mismatched))

    status, _name, detail = _recovery_check(workspace)
    assert status == doctor_mod.FAIL
    assert _RUN_ID in detail
    assert "does not match" in detail


def test_doctor_authority_base_stripped_never_mutates_run_tree(tmp_path: Path, monkeypatch):
    """The authority verdict stays read-only across matching, missing, unparseable, and projection-failure scenarios."""
    from brigade import run_projector

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    base = _authority_base(workspace)
    event = _activate_authority_recovery_journal_with_checkpoint(workspace, run_dir, base)
    projected = _authority_projected(run_dir, event)
    projected_bytes = projected.to_bytes()

    scenarios = [
        ("matching projection", lambda: (run_dir / "run.json").write_bytes(projected_bytes)),
        ("missing run.json", lambda: (run_dir / "run.json").unlink()),
        ("unparseable run.json", lambda: (run_dir / "run.json").write_text("not json")),
        (
            "projection failure",
            lambda: monkeypatch.setattr(
                run_projector,
                "project_run_snapshot",
                lambda _b, _e, *, journal_present: (_ for _ in ()).throw(run_projector.ProjectionError("boom")),
            ),
        ),
    ]
    for _label, scenario in scenarios:
        scenario()
        before = _snapshot_run_tree(run_dir)
        _recovery_check(workspace)
        after = _snapshot_run_tree(run_dir)
        assert before == after, f"authority verdict mutated the run tree under {_label}"
        (run_dir / "run.json").write_bytes(projected_bytes)
        monkeypatch.undo()
