"""Session-observed attribution for ``--capture auto`` is veto-only (#2589)."""

from __future__ import annotations

import contextlib
import io
import json
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


def test_capture_auto_no_match_writes_nothing_and_notes_once(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BRIGADE_CLAUDE_SESSION", FINGERPRINT)
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = verification.verify_run(target=tmp_path, commands=["true"], capture="auto", reuse=False)
    capsys.readouterr()
    assert rc == 0
    assert outcome_cmd.load_records(tmp_path) == []
    notes = [line for line in err.getvalue().splitlines() if "auto found no skill" in line]
    assert len(notes) == 1


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


def test_observed_row_cannot_shadow_caller_typed_row_for_same_receipt():
    records = [
        _record("alpha", -1, "same", outcome.SESSION_OBSERVED),
        _record("alpha", 1, "same", outcome.CALLER_TYPED),
    ]
    score = outcome.score_records("alpha", records)
    assert (score.helped, score.hurt) == (1, 1)


def test_recency_weighted_counts_ignore_observed_helped():
    import datetime as dt

    now = dt.datetime(2026, 6, 21, tzinfo=dt.timezone.utc)
    helped, total = outcome.recency_weighted_counts([_record("alpha", 1, "o", outcome.SESSION_OBSERVED)], now, 3600)
    assert (helped, total) == (0.0, 0.0)


# reconcile: hold only


def _cohorts(records: list[outcome.OutcomeRecord]) -> outcome.FingerprintCohorts:
    return outcome.split_by_fingerprint("alpha", records, None)


def test_veto_turns_install_and_bump_into_observed_hold():
    cohorts = _cohorts([_record("alpha", -1, "o1", outcome.SESSION_OBSERVED)])
    for action, status in (("install", "promoted"), ("bump", "promoted")):
        decision = outcome.Decision("alpha", action, status, "ok")
        vetoed = outcome_attribution.apply_observed_veto(
            decision, cohorts, artifact_kind="skill", prior_status="candidate" if action == "install" else "promoted"
        )
        assert vetoed.action == "hold"
        assert vetoed.code == outcome_attribution.OBSERVED_REGRESSION_HOLD
        assert "observed" in vetoed.reason


def test_veto_never_creates_rollback_install_or_bump():
    cohorts = _cohorts([_record("alpha", -1, "o1", outcome.SESSION_OBSERVED)])
    held = outcome.Decision("alpha", "hold", "promoted", "no change")
    result = outcome_attribution.apply_observed_veto(held, cohorts, artifact_kind="skill", prior_status="promoted")
    assert result == held
    for status in ("candidate", "promoted"):
        result = outcome_attribution.apply_observed_veto(
            outcome.Decision("alpha", "hold", status, "cooldown active"),
            cohorts,
            artifact_kind="skill",
            prior_status=status,
        )
        assert result.action == "hold"


def test_veto_ignores_caller_typed_hurt_and_cards():
    typed = _cohorts([_record("alpha", -1, "t1")])
    decision = outcome.Decision("alpha", "install", "promoted", "ok")
    assert (
        outcome_attribution.apply_observed_veto(decision, typed, artifact_kind="skill", prior_status="candidate")
        == decision
    )
    observed = _cohorts([_record("alpha", -1, "o1", outcome.SESSION_OBSERVED)])
    assert (
        outcome_attribution.apply_observed_veto(decision, observed, artifact_kind="card", prior_status="candidate")
        == decision
    )


def test_reconcile_holds_install_on_observed_hurt_and_never_rolls_back(scoreable_target, capsys, monkeypatch):
    from tests.test_scorecard_reconcile import _seed_dual_gate_promotion_ready, _stub_execute

    _stub_execute(monkeypatch)
    _seed_dual_gate_promotion_ready(scoreable_target)
    outcome_cmd.append_records(
        scoreable_target, [_record("brigade-work", -1, "observed-hurt", outcome.SESSION_OBSERVED)]
    )

    assert outcome_cmd.reconcile(target=scoreable_target, apply=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    decision = payload["decisions"][0]
    assert decision["action"] == "hold"
    assert decision["new_status"] == "candidate"
    assert decision["reason_code"] == outcome_attribution.OBSERVED_REGRESSION_HOLD
    assert "observed" in decision["reason"]
    assert payload["applied"] == []


def test_reconcile_promoted_artifact_with_observed_hurt_is_not_rolled_back(scoreable_target, capsys, monkeypatch):
    from tests.test_scorecard_reconcile import _seed_dual_gate_promotion_ready, _stub_execute

    _stub_execute(monkeypatch)
    _seed_dual_gate_promotion_ready(scoreable_target)
    assert outcome_cmd.reconcile(target=scoreable_target, apply=True, json_output=True) == 0
    capsys.readouterr()
    outcome_cmd.append_records(
        scoreable_target, [_record("brigade-work", -1, "observed-hurt", outcome.SESSION_OBSERVED)]
    )

    assert outcome_cmd.reconcile(target=scoreable_target, apply=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert all(item["action"] not in {"rollback", "install", "bump"} for item in payload["decisions"])


# scorecards stay receipt-only


def test_scorecards_ignore_observed_rows(scoreable_target):
    from tests.test_scorecard_reconcile import _seed_dual_gate_promotion_ready

    _seed_dual_gate_promotion_ready(scoreable_target)
    before = [scorecard.subject_scorecard_to_dict(c) for c in scorecard.build_scorecards(scoreable_target)]
    outcome_cmd.append_records(
        scoreable_target,
        [
            _record("brigade-work", -1, "observed-hurt", outcome.SESSION_OBSERVED),
            _record("brigade-work", 1, "observed-helped", outcome.SESSION_OBSERVED),
        ],
    )
    after = [scorecard.subject_scorecard_to_dict(c) for c in scorecard.build_scorecards(scoreable_target)]
    assert before and before == after


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
