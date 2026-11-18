"""Session-observed attribution for ``--capture auto`` is veto-only (#2589)."""

from __future__ import annotations

import contextlib
import io
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from brigade import outcome, outcome_attribution, outcome_cmd, scorecard
from brigade.work_cmd import verification

pytest_plugins = ["tests.test_scorecard"]

FINGERPRINT = "0123456789abcdef"


def _install_skill(target: Path, skill_id: str) -> None:
    skill = target / ".claude" / "skills" / skill_id
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(f"# {skill_id}\n")


def _write_state(
    target: Path, read_files: list[str], *, fingerprint: str = FINGERPRINT, name: str = "s-1.json"
) -> Path:
    sessions = target / ".brigade" / "work" / "claude-hooks" / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    path = sessions / name
    path.write_text(json.dumps({"session_fingerprint": fingerprint, "read_files": read_files}))
    return path


def _env(fingerprint: str = FINGERPRINT) -> dict[str, str]:
    return {"BRIGADE_CLAUDE_SESSION": fingerprint}


def _record(artifact_id: str, signal: int, ref: str, source: str = outcome.CALLER_TYPED) -> outcome.OutcomeRecord:
    return outcome.OutcomeRecord(
        artifact_id,
        "skill",
        "",
        "verify",
        signal,
        ref,
        "2026-06-20T00:00:00+00:00",
        attribution_source=source,
    )


NOW = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
SESSION_A = "aaaaaaaaaaaaaaaa"
SESSION_B = "bbbbbbbbbbbbbbbb"


def _receipt(
    target: Path, run_id: str, status: str, started: datetime, *, session: str = SESSION_A, command: str = "pytest -q"
) -> str:
    run_dir = target / ".brigade" / "work" / "verify-runs" / run_id
    run_dir.mkdir(parents=True)
    path = run_dir / "receipt.json"
    path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "status": status,
                "started_at": started.isoformat(),
                "planned_commands": [command],
                "harness_session": {"harness": "claude", "fingerprint": session},
            }
        )
    )
    return str(path)


def _observed_hurt(target: Path, run_id: str, age_days: float = 1.0, **kwargs) -> outcome.OutcomeRecord:
    started = NOW - timedelta(days=age_days)
    ref = _receipt(target, run_id, "failed", started, **kwargs)
    return outcome.OutcomeRecord(
        "alpha", "skill", "", "verify", -1, ref, started.isoformat(), attribution_source=outcome.SESSION_OBSERVED
    )


def _sessions(target: Path, records: list[outcome.OutcomeRecord], config: outcome.ReconcileConfig | None = None):
    cohorts = {"alpha": _cohorts(records)}
    return outcome_attribution.observed_hurt_sessions(
        target, records, cohorts, now=NOW, config=config or outcome.ReconcileConfig()
    ).get("alpha", 0)


# resolver


def test_resolver_returns_ordered_unique_known_ids(tmp_path):
    _write_state(
        tmp_path,
        [
            ".claude/skills/alpha/SKILL.md",
            "src/app.py",
            ".claude/skills/beta/SKILL.md",
            "skills/alpha/SKILL.md",
            ".claude/skills/ghost/SKILL.md",
            ".claude/skills/alpha/notes.md",
        ],
    )
    ids = outcome_attribution.resolve_observed_skill_ids(tmp_path, _env(), {"alpha", "beta"})
    assert ids == ["alpha", "beta"]


def test_resolver_unknown_ids_are_dropped(tmp_path):
    _write_state(tmp_path, [".claude/skills/ghost/SKILL.md"])
    assert outcome_attribution.resolve_observed_skill_ids(tmp_path, _env(), {"alpha"}) == []


def test_resolver_uses_installed_skills_when_known_set_omitted(tmp_path):
    _install_skill(tmp_path, "alpha")
    _write_state(tmp_path, [".claude/skills/alpha/SKILL.md", ".claude/skills/ghost/SKILL.md"])
    assert outcome_attribution.resolve_observed_skill_ids(tmp_path, _env()) == ["alpha"]


def test_resolver_no_match_for_other_session_or_missing_env(tmp_path):
    _write_state(tmp_path, [".claude/skills/alpha/SKILL.md"], fingerprint="ffffffffffffffff")
    assert outcome_attribution.resolve_observed_skill_ids(tmp_path, _env(), {"alpha"}) == []
    assert outcome_attribution.resolve_observed_skill_ids(tmp_path, {}, {"alpha"}) == []
    assert (
        outcome_attribution.resolve_observed_skill_ids(tmp_path, {"BRIGADE_CLAUDE_SESSION": "nothex"}, {"alpha"}) == []
    )


def test_resolver_no_session_directory_is_empty(tmp_path):
    assert outcome_attribution.resolve_observed_skill_ids(tmp_path, _env(), {"alpha"}) == []


def test_resolver_refuses_symlinked_state(tmp_path):
    real = tmp_path / "elsewhere.json"
    real.write_text(json.dumps({"session_fingerprint": FINGERPRINT, "read_files": [".claude/skills/alpha/SKILL.md"]}))
    sessions = tmp_path / ".brigade" / "work" / "claude-hooks" / "sessions"
    sessions.mkdir(parents=True)
    try:
        (sessions / "s-1.json").symlink_to(real)
    except OSError:
        pytest.skip("symlinks unavailable")
    assert outcome_attribution.resolve_observed_skill_ids(tmp_path, _env(), {"alpha"}) == []


def test_resolver_refuses_oversized_state(tmp_path):
    path = _write_state(tmp_path, [".claude/skills/alpha/SKILL.md"])
    path.write_text(
        json.dumps(
            {
                "session_fingerprint": FINGERPRINT,
                "read_files": [".claude/skills/alpha/SKILL.md"],
                "pad": "x" * (outcome_attribution.MAX_STATE_BYTES + 10),
            }
        )
    )
    assert outcome_attribution.resolve_observed_skill_ids(tmp_path, _env(), {"alpha"}) == []


def test_resolver_agent_session_env_also_matches(tmp_path):
    _write_state(tmp_path, [".claude/skills/alpha/SKILL.md"])
    env = {"BRIGADE_AGENT_SESSION": FINGERPRINT}
    assert outcome_attribution.resolve_observed_skill_ids(tmp_path, env, {"alpha"}) == ["alpha"]


def test_resolver_only_counts_installed_harness_and_registry_skill_paths(tmp_path):
    _write_state(
        tmp_path,
        [
            "tests/fixtures/skills/alpha/SKILL.md",
            "docs/skills/beta/SKILL.md",
            "skills/gamma/SKILL.md",
            ".brigade/skills/registry/delta/SKILL.md",
            ".cursor/skills/epsilon/SKILL.md",
            ".brigade/skills/zeta/SKILL.md",
        ],
    )
    known = {"alpha", "beta", "gamma", "delta", "epsilon", "zeta"}
    assert outcome_attribution.resolve_observed_skill_ids(tmp_path, _env(), known) == ["delta", "epsilon"]


def test_resolver_compares_case_insensitively_and_returns_the_known_spelling(tmp_path):
    _write_state(tmp_path, [".Claude/Skills/Alpha-Skill/skill.MD", ".claude/skills/BETA/SKILL.md"])
    ids = outcome_attribution.resolve_observed_skill_ids(tmp_path, _env(), {"alpha-skill", "Beta"})
    assert ids == ["alpha-skill", "Beta"]


def test_resolver_stops_at_the_first_matching_state(tmp_path, monkeypatch):
    for index in range(6):
        path = _write_state(
            tmp_path,
            [".claude/skills/alpha/SKILL.md"],
            fingerprint=FINGERPRINT if index == 5 else "ffffffffffffffff",
            name=f"s-{index}.json",
        )
        os.utime(path, (1_000_000 + index, 1_000_000 + index))
    parsed: list[int] = []
    real_loads = json.loads

    def counting(raw, *args, **kwargs):
        parsed.append(1)
        return real_loads(raw, *args, **kwargs)

    monkeypatch.setattr(outcome_attribution.json, "loads", counting)
    assert outcome_attribution.resolve_observed_skill_ids(tmp_path, _env(), {"alpha"}) == ["alpha"]
    assert len(parsed) == 1  # newest state matched; the five older files were never parsed


def test_explicit_claude_session_wins_over_the_ambient_agent_session(tmp_path):
    _write_state(tmp_path, [".claude/skills/alpha/SKILL.md"], fingerprint=FINGERPRINT, name="own.json")
    _write_state(tmp_path, [".claude/skills/beta/SKILL.md"], fingerprint="aaaaaaaaaaaaaaaa", name="parent.json")
    env = {"BRIGADE_CLAUDE_SESSION": FINGERPRINT, "BRIGADE_AGENT_SESSION": "aaaaaaaaaaaaaaaa"}
    assert outcome_attribution.resolve_observed_skill_ids(tmp_path, env, {"alpha", "beta"}) == ["alpha"]
    bad = {"BRIGADE_CLAUDE_SESSION": "nothex", "BRIGADE_AGENT_SESSION": "aaaaaaaaaaaaaaaa"}
    assert outcome_attribution.resolve_observed_skill_ids(tmp_path, bad, {"alpha", "beta"}) == []


def test_run_worker_does_not_read_the_parents_inherited_session(tmp_path):
    _write_state(tmp_path, [".claude/skills/alpha/SKILL.md"])
    inherited = {"BRIGADE_AGENT_SESSION": FINGERPRINT, "BRIGADE_RUN_ID": "run-1"}
    assert outcome_attribution.resolve_observed_skill_ids(tmp_path, inherited, {"alpha"}) == []
    assert outcome_attribution.resolve_observed_skill_ids(tmp_path, {**inherited, **_env()}, {"alpha"}) == ["alpha"]


def test_resolver_requires_the_state_session_id_to_match_its_fingerprint(tmp_path):
    from brigade import localio

    good = localio.stable_hash({"claude_session_id": "session-1"})
    sessions = tmp_path / ".brigade" / "work" / "claude-hooks" / "sessions"
    sessions.mkdir(parents=True)
    read = [".claude/skills/alpha/SKILL.md"]
    (sessions / "forged.json").write_text(
        json.dumps({"session_fingerprint": good, "session_id": "other", "read_files": read})
    )
    assert outcome_attribution.resolve_observed_skill_ids(tmp_path, {"BRIGADE_CLAUDE_SESSION": good}, {"alpha"}) == []
    (sessions / "forged.json").write_text(
        json.dumps({"session_fingerprint": good, "session_id": "session-1", "read_files": read})
    )
    assert outcome_attribution.resolve_observed_skill_ids(tmp_path, {"BRIGADE_CLAUDE_SESSION": good}, {"alpha"}) == [
        "alpha"
    ]


def test_resolver_includes_skill_tool_invocations(tmp_path):
    sessions = tmp_path / ".brigade" / "work" / "claude-hooks" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "s.json").write_text(
        json.dumps(
            {
                "session_fingerprint": FINGERPRINT,
                "read_files": [".claude/skills/alpha/SKILL.md"],
                "invoked_skills": ["brigade:Beta", "superpowers:gamma", "gamma", "ghost"],
            }
        )
    )
    ids = outcome_attribution.resolve_observed_skill_ids(tmp_path, _env(), {"alpha", "beta", "gamma"})
    assert ids == ["alpha", "beta", "gamma"]


# capture auto


def _verify_auto(target: Path, monkeypatch, capsys) -> int:
    monkeypatch.setenv("BRIGADE_CLAUDE_SESSION", FINGERPRINT)
    with contextlib.redirect_stderr(io.StringIO()):
        rc = verification.verify_run(target=target, commands=["true"], capture="auto", reuse=False)
    capsys.readouterr()
    return rc


def test_capture_auto_writes_one_observed_row_per_skill_and_never_auto(tmp_path, monkeypatch, capsys):
    _install_skill(tmp_path, "alpha")
    _install_skill(tmp_path, "beta")
    _write_state(tmp_path, [".claude/skills/alpha/SKILL.md", ".claude/skills/beta/SKILL.md"])

    assert _verify_auto(tmp_path, monkeypatch, capsys) == 0

    rows = outcome_cmd.load_records(tmp_path)
    assert [row.artifact_id for row in rows] == ["alpha", "beta"]
    assert {row.attribution_source for row in rows} == {outcome_attribution.SESSION_OBSERVED}
    assert all(row.artifact_id != "auto" for row in rows)
    ledger = (tmp_path / "memory" / "outcome" / "records.jsonl").read_text()
    assert '"artifact_id": "auto"' not in ledger
    assert '"attribution_source": "session_observed"' in ledger


def test_capture_auto_without_observed_skill_falls_back_to_caller_typed_default(tmp_path, monkeypatch, capsys):
    _install_skill(tmp_path, "brigade-work")
    monkeypatch.setenv("BRIGADE_CLAUDE_SESSION", FINGERPRINT)
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = verification.verify_run(target=tmp_path, commands=["true"], capture="auto", reuse=False)
    capsys.readouterr()
    assert rc == 0
    [row] = outcome_cmd.load_records(tmp_path)
    assert (row.artifact_id, row.attribution_source) == ("brigade-work", outcome.CALLER_TYPED)
    notes = [line for line in err.getvalue().splitlines() if "auto found no skill" in line]
    assert len(notes) == 1


def test_capture_auto_in_a_plain_shell_still_records(tmp_path, monkeypatch, capsys):
    _install_skill(tmp_path, "brigade-work")
    for key in ("BRIGADE_CLAUDE_SESSION", "BRIGADE_AGENT_SESSION", "BRIGADE_RUN_ID"):
        monkeypatch.delenv(key, raising=False)
    with contextlib.redirect_stderr(io.StringIO()):
        assert verification.verify_run(target=tmp_path, commands=["true"], capture="auto", reuse=False) == 0
    capsys.readouterr()
    [row] = outcome_cmd.load_records(tmp_path)
    assert (row.artifact_id, row.attribution_source) == ("brigade-work", outcome.CALLER_TYPED)


def test_explicit_capture_is_caller_typed(tmp_path, monkeypatch, capsys):
    _install_skill(tmp_path, "alpha")
    monkeypatch.delenv("BRIGADE_CLAUDE_SESSION", raising=False)
    with contextlib.redirect_stderr(io.StringIO()):
        assert verification.verify_run(target=tmp_path, commands=["true"], capture="alpha", reuse=False) == 0
    capsys.readouterr()
    rows = outcome_cmd.load_records(tmp_path)
    assert [row.attribution_source for row in rows] == [outcome.CALLER_TYPED]


# ledger compatibility


def test_rows_without_the_field_read_as_caller_typed(tmp_path):
    ledger = tmp_path / "memory" / "outcome" / "records.jsonl"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        json.dumps(
            {
                "artifact_id": "alpha",
                "artifact_kind": "skill",
                "task_id": "",
                "source": "verify",
                "signal_value": 1,
                "evidence_ref": "r1",
                "ts": "2026-06-20T00:00:00+00:00",
            }
        )
        + "\n"
    )
    [row] = outcome_cmd.load_records(tmp_path)
    assert row.attribution_source == outcome.CALLER_TYPED
    assert outcome_attribution.normalize_attribution_source("garbage") == outcome.CALLER_TYPED


# rank: hurt only


def test_observed_rows_never_raise_helped():
    records = [
        _record("alpha", 1, "r1", outcome.SESSION_OBSERVED),
        _record("alpha", 1, "r2", outcome.SESSION_OBSERVED),
    ]
    score = outcome.score_records("alpha", records)
    assert score.helped == 0
    assert score.score == 0.0


def test_observed_hurt_lowers_rank_and_is_reported():
    typed = [_record("alpha", 1, f"t{i}") for i in range(3)]
    baseline = outcome.score_records("alpha", typed)
    hurt = outcome.score_records("alpha", [*typed, _record("alpha", -1, "o1", outcome.SESSION_OBSERVED)])
    assert hurt.hurt == 1
    assert hurt.observed_hurt == 1
    assert hurt.score < baseline.score


def test_one_receipt_scores_once_and_the_caller_typed_row_wins():
    observed_first = [
        _record("alpha", -1, "same", outcome.SESSION_OBSERVED),
        _record("alpha", 1, "same", outcome.CALLER_TYPED),
    ]
    typed_first = list(reversed(observed_first))
    for records in (observed_first, typed_first):
        score = outcome.score_records("alpha", records)
        assert (score.helped, score.hurt, score.observed_hurt) == (1, 0, 0)


def test_observed_only_receipt_still_counts_once_as_hurt():
    records = [_record("alpha", -1, "same", outcome.SESSION_OBSERVED)] * 2
    score = outcome.score_records("alpha", records)
    assert (score.hurt, score.observed_hurt) == (1, 1)


def test_recency_weighted_counts_ignore_observed_helped():
    import datetime as dt

    now = dt.datetime(2026, 6, 21, tzinfo=dt.timezone.utc)
    helped, total = outcome.recency_weighted_counts([_record("alpha", 1, "o", outcome.SESSION_OBSERVED)], now, 3600)
    assert (helped, total) == (0.0, 0.0)


# reconcile: hold only


def _cohorts(records: list[outcome.OutcomeRecord]) -> outcome.FingerprintCohorts:
    return outcome.split_by_fingerprint("alpha", records, None)


CONFIG = outcome.ReconcileConfig()


def test_veto_turns_install_and_bump_into_observed_hold():
    for action, status in (("install", "promoted"), ("bump", "promoted")):
        decision = outcome.Decision("alpha", action, status, "ok")
        vetoed = outcome_attribution.apply_observed_veto(
            decision, 2, prior_status="candidate" if action == "install" else "promoted", config=CONFIG
        )
        assert vetoed.action == "hold"
        assert vetoed.code == outcome_attribution.OBSERVED_REGRESSION_HOLD
        assert "observed" in vetoed.reason


def test_veto_never_creates_rollback_install_or_bump():
    held = outcome.Decision("alpha", "hold", "promoted", "no change")
    assert outcome_attribution.apply_observed_veto(held, 5, prior_status="promoted", config=CONFIG) == held
    for status in ("candidate", "promoted"):
        result = outcome_attribution.apply_observed_veto(
            outcome.Decision("alpha", "hold", status, "cooldown active"), 5, prior_status=status, config=CONFIG
        )
        assert result.action == "hold"
    rollback = outcome.Decision("alpha", "rollback", "demoted", "verified regression measured")
    assert outcome_attribution.apply_observed_veto(rollback, 5, prior_status="promoted", config=CONFIG) == rollback


def test_veto_needs_the_configured_number_of_sessions():
    decision = outcome.Decision("alpha", "install", "promoted", "ok")
    assert outcome_attribution.apply_observed_veto(decision, 1, prior_status="candidate", config=CONFIG) == decision
    one = outcome.ReconcileConfig(observed_hurt_min_sessions=1)
    assert outcome_attribution.apply_observed_veto(decision, 1, prior_status="candidate", config=one).action == "hold"
    assert outcome_attribution.apply_observed_veto(decision, 0, prior_status="candidate", config=one) == decision


def test_observed_hurt_never_rolls_back_a_promoted_card_and_vetoes_its_bump():
    config = outcome.ReconcileConfig(cooldown_seconds=0)
    now = datetime(2026, 6, 21, tzinfo=timezone.utc)
    typed = [_record("alpha", 1, f"t{i}") for i in range(config.bump_min_helped)]
    observed = _record("alpha", -1, "o1", outcome.SESSION_OBSERVED)
    cohorts = _cohorts([*typed, observed])
    assert cohorts.current.observed_hurt == 1
    decision = outcome.decide(cohorts.current, current_status="promoted", last_action_ts=None, now=now, config=config)
    assert decision.action == "bump"  # the decision core itself no longer rolls back on observed hurt
    vetoed = outcome_attribution.apply_observed_veto(decision, 2, prior_status="promoted", config=config)
    assert (vetoed.action, vetoed.code) == ("hold", outcome.OBSERVED_REGRESSION_HOLD)
    rollback_only = _cohorts([observed])
    assert (
        outcome.decide(
            rollback_only.current, current_status="promoted", last_action_ts=None, now=now, config=config
        ).action
        != "rollback"
    )


def test_caller_typed_hurt_still_rolls_back_a_promoted_card():
    config = outcome.ReconcileConfig(cooldown_seconds=0)
    cohorts = _cohorts([_record("alpha", -1, "t1")])
    decision = outcome.decide(
        cohorts.current,
        current_status="promoted",
        last_action_ts=None,
        now=datetime(2026, 6, 21, tzinfo=timezone.utc),
        config=config,
    )
    assert decision.action == "rollback"


def test_observed_veto_is_surfaced_for_every_prior_status():
    for prior in ("candidate", "promoted"):
        held = outcome.Decision("alpha", "hold", prior, "withheld", code=outcome.OBSERVED_REGRESSION_HOLD)
        assert outcome_cmd._surface_hold_decision(held, prior)
    quiet = outcome.Decision("alpha", "hold", "promoted", "no change")
    assert not outcome_cmd._surface_hold_decision(quiet, "promoted")


# the veto is bounded: iteration, recency, session count, release


def test_decision_core_ignores_observed_hurt_for_a_candidate_card():
    config = outcome.ReconcileConfig(cooldown_seconds=0)
    typed = [_record("alpha", 1, f"t{i}") for i in range(config.install_min_helped)]
    cohorts = _cohorts([*typed, _record("alpha", -1, "o1", outcome.SESSION_OBSERVED)])
    decision = outcome.decide(cohorts.current, current_status="candidate", last_action_ts=None, now=NOW, config=config)
    assert decision.action == "install"  # only the bounded veto may hold on observed hurt


def test_a_failure_fixed_later_in_the_same_session_is_iteration_not_regression(tmp_path):
    failing = _observed_hurt(tmp_path, "run-1")
    assert _sessions(tmp_path, [failing]) == 1
    _receipt(tmp_path, "run-2", "completed", NOW - timedelta(hours=1))
    assert _sessions(tmp_path, [failing]) == 0


def test_a_pass_that_is_not_a_fix_does_not_clear_the_failure(tmp_path):
    failing = _observed_hurt(tmp_path, "run-1")
    _receipt(tmp_path, "other-session", "completed", NOW - timedelta(hours=1), session=SESSION_B)
    _receipt(tmp_path, "other-command", "completed", NOW - timedelta(hours=1), command="ruff check")
    _receipt(tmp_path, "earlier", "completed", NOW - timedelta(days=2))
    _receipt(tmp_path, "still-failing", "failed", NOW - timedelta(hours=1))
    assert _sessions(tmp_path, [failing]) == 1


def test_two_sessions_with_unresolved_failures_hold_and_one_does_not(tmp_path):
    a = _observed_hurt(tmp_path, "run-a", session=SESSION_A)
    b = _observed_hurt(tmp_path, "run-b", session=SESSION_B)
    assert _sessions(tmp_path, [a]) == 1
    assert _sessions(tmp_path, [a, a]) == 1  # one noisy session never reaches the threshold
    assert _sessions(tmp_path, [a, b]) == 2
    install = outcome.Decision("alpha", "install", "promoted", "ok")
    for rows, action in (([a], "install"), ([a, b], "hold")):
        veto = outcome_attribution.apply_observed_veto(
            install, _sessions(tmp_path, rows), prior_status="candidate", config=CONFIG
        )
        assert veto.action == action


def test_fixing_one_of_two_sessions_releases_the_hold(tmp_path):
    a = _observed_hurt(tmp_path, "run-a", session=SESSION_A)
    b = _observed_hurt(tmp_path, "run-b", session=SESSION_B)
    assert _sessions(tmp_path, [a, b]) == 2
    _receipt(tmp_path, "run-b-fix", "completed", NOW - timedelta(minutes=5), session=SESSION_B)
    assert _sessions(tmp_path, [a, b]) == 1


def test_observed_hurt_outside_the_window_releases_the_hold(tmp_path):
    fresh = _observed_hurt(tmp_path, "run-a", age_days=29, session=SESSION_A)
    old = _observed_hurt(tmp_path, "run-b", age_days=31, session=SESSION_B)
    assert _sessions(tmp_path, [fresh, old]) == 1
    # The same evidence holds while it is inside the window, and the window is configurable.
    young = _observed_hurt(tmp_path, "run-c", age_days=5, session=SESSION_B)
    assert _sessions(tmp_path, [fresh, young]) == 2
    assert _sessions(tmp_path, [fresh, young], outcome.ReconcileConfig(observed_hurt_window_days=3)) == 0
    assert _sessions(tmp_path, [fresh, old], outcome.ReconcileConfig(observed_hurt_window_days=60)) == 2


def test_a_caller_typed_twin_of_the_receipt_wins_over_the_observed_row(tmp_path):
    a = _observed_hurt(tmp_path, "run-a", session=SESSION_A)
    b = _observed_hurt(tmp_path, "run-b", session=SESSION_B)
    twin = outcome.OutcomeRecord(
        "alpha", "skill", "", "verify", -1, a.evidence_ref, a.ts, attribution_source=outcome.CALLER_TYPED
    )
    assert _sessions(tmp_path, [a, b, twin]) == 1


def test_an_unattributable_failure_counts_alone_and_never_clears(tmp_path):
    a = _observed_hurt(tmp_path, "run-a")
    ghost = outcome.OutcomeRecord(
        "alpha",
        "skill",
        "",
        "verify",
        -1,
        str(tmp_path / "gone.json"),
        NOW.isoformat(),
        attribution_source=outcome.SESSION_OBSERVED,
    )
    assert _sessions(tmp_path, [a, ghost]) == 2


def _append_unresolved_observed_hurt(target: Path, *, sessions: int, age_days: float = 1.0) -> None:
    """Observed failing verify rows for ``brigade-work``, one per session, with real receipts."""
    now = datetime.now(timezone.utc)
    rows = []
    for index in range(sessions):
        started = now - timedelta(days=age_days, minutes=index)
        ref = _receipt(
            target, f"observed-fail-{index}", "failed", started, session=f"{index:016x}", command=f"pytest {index}"
        )
        rows.append(
            outcome.OutcomeRecord(
                "brigade-work",
                "skill",
                "",
                "verify",
                -1,
                ref,
                started.isoformat(),
                attribution_source=outcome.SESSION_OBSERVED,
            )
        )
    outcome_cmd.append_records(target, rows)


def test_reconcile_holds_install_on_observed_hurt_and_never_rolls_back(scoreable_target, capsys, monkeypatch):
    from tests.test_scorecard_reconcile import _seed_dual_gate_promotion_ready

    _seed_dual_gate_promotion_ready(scoreable_target)
    _append_unresolved_observed_hurt(scoreable_target, sessions=2)

    assert outcome_cmd.reconcile(target=scoreable_target, apply=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    decision = payload["decisions"][0]
    assert decision["action"] == "hold"
    assert decision["new_status"] == "candidate"
    assert decision["reason_code"] == outcome_attribution.OBSERVED_REGRESSION_HOLD
    assert "observed" in decision["reason"]
    assert payload["applied"] == []


def test_reconcile_promoted_artifact_with_observed_hurt_is_not_rolled_back(scoreable_target, capsys, monkeypatch):
    from tests.test_scorecard_reconcile import _seed_dual_gate_promotion_ready

    _seed_dual_gate_promotion_ready(scoreable_target)
    # No cooldown and a one-clear bump threshold: the second pass really decides, and
    # without the observed row it would bump (control), so a veto is visible, not vacuous.
    cfg = outcome.ReconcileConfig(cooldown_seconds=0, bump_min_helped=1)
    assert outcome_cmd.reconcile(target=scoreable_target, apply=True, config=cfg, json_output=True) == 0
    [promotion] = json.loads(capsys.readouterr().out)["decisions"]
    assert (promotion["action"], promotion["new_status"]) == ("install", "promoted")
    status_path = scoreable_target / "memory" / "outcome" / "status.json"
    assert json.loads(status_path.read_text())["artifacts"]["brigade-work"]["status"] == "promoted"

    assert outcome_cmd.reconcile(target=scoreable_target, apply=False, config=cfg, json_output=True) == 0
    [control] = json.loads(capsys.readouterr().out)["decisions"]
    assert control["action"] == "bump"

    _append_unresolved_observed_hurt(scoreable_target, sessions=2)
    assert outcome_cmd.reconcile(target=scoreable_target, apply=True, config=cfg, json_output=True) == 0
    decisions = json.loads(capsys.readouterr().out)["decisions"]

    assert [item["action"] for item in decisions] == ["hold"], decisions
    assert decisions[0]["reason_code"] == outcome_attribution.OBSERVED_REGRESSION_HOLD
    assert decisions[0]["new_status"] == "promoted"
    assert json.loads(status_path.read_text())["artifacts"]["brigade-work"]["status"] == "promoted"


def test_one_observed_session_does_not_hold_reconcile(scoreable_target, capsys, monkeypatch):
    from tests.test_scorecard_reconcile import _seed_dual_gate_promotion_ready

    _seed_dual_gate_promotion_ready(scoreable_target)
    _append_unresolved_observed_hurt(scoreable_target, sessions=1)
    assert outcome_cmd.reconcile(target=scoreable_target, apply=False, json_output=True) == 0
    [decision] = json.loads(capsys.readouterr().out)["decisions"]
    assert decision["action"] == "install"


def test_reconcile_hold_releases_once_the_hurt_ages_out(scoreable_target, capsys, monkeypatch):
    from tests.test_scorecard_reconcile import _seed_dual_gate_promotion_ready

    _seed_dual_gate_promotion_ready(scoreable_target)
    _append_unresolved_observed_hurt(scoreable_target, sessions=2, age_days=31)
    assert outcome_cmd.reconcile(target=scoreable_target, apply=False, json_output=True) == 0
    [decision] = json.loads(capsys.readouterr().out)["decisions"]
    assert decision["action"] == "install"


def _append_unresolved_observed_hurt_second_session(target: Path) -> None:
    now = datetime.now(timezone.utc)
    ref = _receipt(target, "observed-fail-second", "failed", now - timedelta(days=2), session="f" * 16, command="x")
    outcome_cmd.append_records(
        target,
        [
            outcome.OutcomeRecord(
                "brigade-work",
                "skill",
                "",
                "verify",
                -1,
                ref,
                (now - timedelta(days=2)).isoformat(),
                attribution_source=outcome.SESSION_OBSERVED,
            )
        ],
    )


def _fork_artifact(target: Path, tmp_path: Path) -> dict:
    out = tmp_path / "fork.json"
    assert outcome_cmd.fork(target=target, out=out, json_output=False) == 0
    return json.loads(out.read_text())["artifacts"]["brigade-work"]


def test_fork_projection_applies_the_same_veto_as_reconcile(scoreable_target, tmp_path, monkeypatch, capsys):
    from tests.test_scorecard_reconcile import _seed_dual_gate_promotion_ready

    _seed_dual_gate_promotion_ready(scoreable_target)
    assert _fork_artifact(scoreable_target, tmp_path)["action"] == "install"  # control
    _append_unresolved_observed_hurt(scoreable_target, sessions=1)
    assert _fork_artifact(scoreable_target, tmp_path)["action"] == "install"
    _append_unresolved_observed_hurt_second_session(scoreable_target)
    held = _fork_artifact(scoreable_target, tmp_path)
    assert (held["action"], held["new_status"]) == ("hold", "candidate")
    assert "observed" in held["reason"]


def test_fleet_skills_report_applies_the_same_veto_as_reconcile(scoreable_target):
    from brigade import fleet_skills_report
    from tests.test_scorecard_reconcile import _seed_dual_gate_promotion_ready

    _seed_dual_gate_promotion_ready(scoreable_target)
    now = datetime.now(timezone.utc)

    def suggestion() -> str:
        [row] = [
            r for r in fleet_skills_report.build_scores(scoreable_target, now) if r["artifact_id"] == "brigade-work"
        ]
        return row["suggestion"]

    assert suggestion() == "install"  # control
    _append_unresolved_observed_hurt(scoreable_target, sessions=2)
    assert suggestion() == "hold"


def test_observed_hold_is_counted_in_the_ratchet_report():
    from dataclasses import dataclass

    from brigade import outcome_ratchet_report as report_mod

    @dataclass
    class _Item:
        decision: outcome.Decision
        prior_status: str
        cohorts: object = None
        scorecard: object = None
        artifact_kind: str = "skill"

    held = outcome.Decision("alpha", "hold", "promoted", "withheld", code=outcome_attribution.OBSERVED_REGRESSION_HOLD)
    report = report_mod.build_report(
        [_Item(held, "promoted")],
        apply=False,
        generated_at=datetime(2026, 11, 17, tzinfo=timezone.utc),
        target=Path("."),
    )
    assert report["summary"]["regression_holds"] == 1
    assert report["summary"]["regression_hold_ids"] == ["alpha"]
    assert [row["reason_code"] for row in report["regressions"]] == [outcome_attribution.OBSERVED_REGRESSION_HOLD]


# scorecards stay receipt-only


def test_real_auto_capture_leaves_scorecards_untouched(scoreable_target, monkeypatch, capsys):
    from tests.test_scorecard_reconcile import _seed_dual_gate_promotion_ready

    _seed_dual_gate_promotion_ready(scoreable_target)
    _install_skill(scoreable_target, "brigade-work")
    _write_state(scoreable_target, [".claude/skills/brigade-work/SKILL.md"])

    def cards() -> list[dict]:
        return [scorecard.subject_scorecard_to_dict(c) for c in scorecard.build_scorecards(scoreable_target)]

    before = cards()
    assert before
    monkeypatch.setenv("BRIGADE_CLAUDE_SESSION", FINGERPRINT)
    for command, expected_signal in (("true", 1), ("false", -1)):
        with contextlib.redirect_stderr(io.StringIO()):
            verification.verify_run(target=scoreable_target, commands=[command], capture="auto", reuse=False)
        capsys.readouterr()
        row = outcome_cmd.load_records(scoreable_target)[-1]
        assert (row.artifact_id, row.attribution_source) == ("brigade-work", outcome_attribution.SESSION_OBSERVED)
        assert row.signal_value == expected_signal
    assert len(outcome_cmd.load_records(scoreable_target)) == 2
    assert cards() == before


# hook contract


def test_resolver_reads_real_hook_state_and_auto_is_never_an_exercised_artifact(tmp_path):
    from brigade.claude_hooks import runtime
    from tests.test_claude_hooks_runtime import _payload, _wired_claude

    target = _wired_claude(tmp_path)
    _install_skill(target, "taste")
    skill = target / ".claude" / "skills" / "taste" / "SKILL.md"
    runtime.handle_payload(
        "PostToolUse",
        _payload(target, "PostToolUse", tool_name="Read", tool_input={"file_path": str(skill)}),
    )
    runtime.handle_payload(
        "PostToolUse",
        _payload(
            target,
            "PostToolUse",
            tool_name="Bash",
            tool_input={"command": 'brigade work verify run --target . --command "true" --capture auto'},
        ),
    )

    env = {"BRIGADE_CLAUDE_SESSION": runtime._session_fingerprint("session-1")}
    assert outcome_attribution.resolve_observed_skill_ids(target, env) == ["taste"]
    state = runtime.read_session_state(target, "session-1")
    assert state is not None
    assert state.get("exercised_artifact_id") != "auto"


# receipt stamp, retry hint, hook target lookup, breakdowns


def test_observed_capture_stamps_source_every_id_and_session_on_the_receipt(tmp_path, monkeypatch, capsys):
    _install_skill(tmp_path, "alpha")
    _install_skill(tmp_path, "beta")
    _write_state(tmp_path, [".claude/skills/alpha/SKILL.md", ".claude/skills/beta/SKILL.md"])
    assert _verify_auto(tmp_path, monkeypatch, capsys) == 0
    receipt = verification._verify_receipts(tmp_path)[0]
    assert receipt["outcome_capture"] == {
        "artifact_id": "alpha",
        "artifact_kind": "skill",
        "attribution_source": outcome.SESSION_OBSERVED,
        "observed_artifact_ids": ["alpha", "beta"],
        "session_fingerprint": FINGERPRINT,
    }


def test_explicit_and_fallback_captures_carry_no_observed_stamp(tmp_path, monkeypatch, capsys):
    _install_skill(tmp_path, "alpha")
    monkeypatch.delenv("BRIGADE_CLAUDE_SESSION", raising=False)
    with contextlib.redirect_stderr(io.StringIO()):
        assert verification.verify_run(target=tmp_path, commands=["true"], capture="alpha", reuse=False) == 0
    capsys.readouterr()
    assert verification._verify_receipts(tmp_path)[0]["outcome_capture"] == {
        "artifact_id": "alpha",
        "artifact_kind": "skill",
    }


def test_retry_hint_for_an_observed_stamp_suggests_auto_not_the_observed_id(tmp_path, monkeypatch, capsys):
    _install_skill(tmp_path, "alpha")
    _write_state(tmp_path, [".claude/skills/alpha/SKILL.md"])
    monkeypatch.setenv("BRIGADE_CLAUDE_SESSION", FINGERPRINT)
    monkeypatch.setattr(outcome_attribution, "capture_artifacts", lambda **_: 0)  # the first run dies before capture
    with contextlib.redirect_stderr(io.StringIO()):
        verification.verify_run(target=tmp_path, commands=["false"], capture="auto", reuse=False)
    failed = verification._verify_receipts(tmp_path)[0]
    assert outcome_attribution.receipt_is_observed(failed)
    capsys.readouterr()
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        verification.verify_run(target=tmp_path, commands=["false"], capture="auto", reuse=False)
    text = err.getvalue()
    assert f"brigade outcome capture auto --run-id {failed['run_id']}" in text
    assert "brigade outcome capture alpha" not in text


def test_outcome_capture_auto_writes_observed_rows_for_a_past_run(tmp_path, monkeypatch, capsys):
    _install_skill(tmp_path, "alpha")
    _write_state(tmp_path, [".claude/skills/alpha/SKILL.md"])
    with contextlib.redirect_stderr(io.StringIO()):
        assert verification.verify_run(target=tmp_path, commands=["true"], reuse=False) == 0
    capsys.readouterr()
    monkeypatch.setenv("BRIGADE_CLAUDE_SESSION", FINGERPRINT)
    assert outcome_attribution.capture_auto(target=tmp_path, artifact_kind="skill", run_id=None, run_receipt=None) == 0
    capsys.readouterr()
    [row] = outcome_cmd.load_records(tmp_path)
    assert (row.artifact_id, row.attribution_source) == ("alpha", outcome.SESSION_OBSERVED)
    assert outcome_attribution.capture_auto(target=tmp_path, artifact_kind="card", run_id=None, run_receipt=None) == 1


def test_resolver_falls_back_to_the_hook_target_when_the_verifier_checkout_differs(tmp_path):
    hook_target = tmp_path / "hook"
    verify_target = tmp_path / "verify"
    for repo in (hook_target, verify_target):
        repo.mkdir()
        _install_skill(repo, "alpha")
    _write_state(hook_target, [".claude/skills/alpha/SKILL.md"])
    assert outcome_attribution.resolve_observed_skill_ids(verify_target, _env()) == []
    env = {**_env(), outcome_attribution.SESSION_TARGET_ENV: str(hook_target)}
    assert outcome_attribution.resolve_observed_skill_ids(verify_target, env) == ["alpha"]
    relative = {**_env(), outcome_attribution.SESSION_TARGET_ENV: "hook"}
    assert outcome_attribution.resolve_observed_skill_ids(verify_target, relative) == []


def test_route_and_capability_breakdowns_exclude_observed_helped():
    route = {"followed": True, "path": "fast", "size": "s"}
    context = {"harness": "claude", "model_family": "opus"}
    rows = [
        outcome.OutcomeRecord(
            "alpha",
            "skill",
            "",
            "verify",
            signal,
            ref,
            "2026-06-20T00:00:00+00:00",
            route=route,
            route_fingerprint="rf",
            context=context,
            capability_fingerprint="cf",
            attribution_source=source,
        )
        for signal, ref, source in (
            (1, "r1", outcome.CALLER_TYPED),
            (1, "r2", outcome.SESSION_OBSERVED),
            (-1, "r3", outcome.SESSION_OBSERVED),
        )
    ]
    [routed] = outcome_cmd._route_breakdown(rows) or []
    [capability] = outcome_cmd._capability_breakdown(rows) or []
    for group in (routed, capability):
        assert (group["helped"], group["hurt"]) == (1, 1)
