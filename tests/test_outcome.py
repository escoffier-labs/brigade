import datetime as dt

from brigade import outcome


def test_wilson_lower_bound_zero_when_no_trials():
    assert outcome.wilson_lower_bound(0, 0) == 0.0


def test_wilson_lower_bound_below_one_and_grows_with_more_confirming_trials():
    few = outcome.wilson_lower_bound(2, 2)
    many = outcome.wilson_lower_bound(20, 20)
    assert 0.0 < few < 1.0
    assert few < many < 1.0


def test_wilson_lower_bound_penalizes_a_miss():
    clean = outcome.wilson_lower_bound(5, 5)
    mixed = outcome.wilson_lower_bound(4, 5)
    assert mixed < clean


def test_signal_value_rewards_only_model_unauthored_success():
    assert outcome.signal_value("verify", "completed") == 1
    assert outcome.signal_value("verify", "failed") == -1
    assert outcome.signal_value("verify", "timed_out") == -1
    assert outcome.signal_value("friction", "cleared") == 1
    assert outcome.signal_value("friction", "recurred") == -1
    assert outcome.signal_value("learnings", "recurred") == -1


def test_signal_value_treats_weak_or_unknown_signals_as_neutral():
    # aboyeur "ok" is only "CLI exited cleanly", not a verified outcome
    assert outcome.signal_value("aboyeur", "ok") == 0
    # manual replay comparison is advisory, not a real signal
    assert outcome.signal_value("replay", "better") == 0
    assert outcome.signal_value("mystery", "whatever") == 0


def test_score_records_folds_counts_wilson_and_last_signal():
    records = [
        outcome.OutcomeRecord("skill-x", "skill", "t1", "verify", 1, "ref1", "2026-06-20T00:00:00+00:00"),
        outcome.OutcomeRecord("skill-x", "skill", "t2", "verify", 1, "ref2", "2026-06-20T01:00:00+00:00"),
        outcome.OutcomeRecord("skill-x", "skill", "t3", "aboyeur", 0, "ref3", "2026-06-20T02:00:00+00:00"),
    ]
    score = outcome.score_records("skill-x", records)
    assert score.artifact_id == "skill-x"
    assert (score.helped, score.hurt, score.neutral) == (2, 0, 1)
    assert score.score == outcome.wilson_lower_bound(2, 2)
    assert score.last_signal_ts == "2026-06-20T02:00:00+00:00"


def test_score_records_dedups_identical_signals():
    # A re-captured/retried verify run produces a byte-identical record; the same
    # physical receipt must not be counted twice (P1: double-count auto-installs).
    duplicate = outcome.OutcomeRecord("skill-x", "skill", "t1", "verify", 1, "ref1", "2026-06-20T00:00:00+00:00")
    score = outcome.score_records("skill-x", [duplicate, duplicate])
    assert score.helped == 1


def test_score_records_keeps_distinct_evidenceless_manual_signals():
    # Two `outcome record` signals written WITHOUT --evidence cannot be proven
    # duplicates, so they must count as 2 (not collapse to 1) or a manual producer
    # could never reach install_min_helped no matter how many real clears occur.
    a = outcome.OutcomeRecord("skill-x", "skill", "", "friction", 1, "", "2026-06-20T00:00:00+00:00")
    b = outcome.OutcomeRecord("skill-x", "skill", "", "friction", 1, "", "2026-06-20T01:00:00+00:00")
    assert outcome.score_records("skill-x", [a, b]).helped == 2


def test_score_records_dedup_does_not_promote_a_single_genuine_positive():
    # Two identical positives are one signal, which is below install_min_helped=2,
    # so a clean candidate must NOT be promoted off a single genuine trial.
    duplicate = outcome.OutcomeRecord("skill-x", "skill", "t1", "verify", 1, "ref1", "2026-06-20T00:00:00+00:00")
    score = outcome.score_records("skill-x", [duplicate, duplicate])
    decision = outcome.decide(
        score,
        current_status="candidate",
        last_action_ts=None,
        now=dt.datetime(2026, 6, 21),
        config=outcome.ReconcileConfig(),
    )
    assert decision.action == "hold"
    assert decision.new_status == "candidate"


def test_score_records_empty_is_zero():
    score = outcome.score_records("skill-x", [])
    assert (score.helped, score.hurt, score.neutral) == (0, 0, 0)
    assert score.score == 0.0
    assert score.last_signal_ts is None


def _score(helped, hurt, neutral=0):
    total = helped + hurt
    return outcome.OutcomeScore(
        "skill-x",
        helped=helped,
        hurt=hurt,
        neutral=neutral,
        score=outcome.wilson_lower_bound(helped, total),
        last_signal_ts="2026-06-20T00:00:00+00:00",
    )


def test_decide_installs_a_clean_candidate():
    decision = outcome.decide(
        _score(2, 0),
        current_status="candidate",
        last_action_ts=None,
        now=dt.datetime(2026, 6, 21),
        config=outcome.ReconcileConfig(),
    )
    assert decision.action == "install"
    assert decision.new_status == "promoted"


def test_decide_withholds_candidate_with_a_verified_regression():
    decision = outcome.decide(
        _score(3, 1),
        current_status="candidate",
        last_action_ts=None,
        now=dt.datetime(2026, 6, 21),
        config=outcome.ReconcileConfig(),
    )
    assert decision.action == "hold"
    assert decision.new_status == "candidate"


def test_decide_withholds_candidate_with_insufficient_evidence():
    decision = outcome.decide(
        _score(1, 0),
        current_status="candidate",
        last_action_ts=None,
        now=dt.datetime(2026, 6, 21),
        config=outcome.ReconcileConfig(),
    )
    assert decision.action == "hold"


def test_decide_rolls_back_a_promoted_artifact_on_regression():
    decision = outcome.decide(
        _score(5, 1),
        current_status="promoted",
        last_action_ts=None,
        now=dt.datetime(2026, 6, 21),
        config=outcome.ReconcileConfig(),
    )
    assert decision.action == "rollback"
    assert decision.new_status == "demoted"


def test_decide_holds_inside_cooldown_window():
    now = dt.datetime(2026, 6, 21, 12, 0, 0)
    recent = dt.datetime(2026, 6, 21, 11, 0, 0)  # 1h ago, default cooldown 24h
    decision = outcome.decide(
        _score(2, 0), current_status="candidate", last_action_ts=recent, now=now, config=outcome.ReconcileConfig()
    )
    assert decision.action == "hold"
    assert "cooldown" in decision.reason


def test_rank_score_blends_confidence_outcome_keyword():
    assert outcome.rank_score(confidence=1.0, outcome=0.0, keyword=0.0) == 0.5
    assert outcome.rank_score(confidence=0.0, outcome=1.0, keyword=0.0) == 0.4
    assert outcome.rank_score(confidence=0.0, outcome=0.0, keyword=1.0) == 0.1
    blended = outcome.rank_score(confidence=0.2, outcome=0.5, keyword=1.0)
    assert abs(blended - (0.5 * 0.2 + 0.4 * 0.5 + 0.1 * 1.0)) < 1e-9


def test_rank_score_prefers_verified_outcome_when_other_signals_tie():
    proven = outcome.rank_score(confidence=0.3, outcome=0.8, keyword=0.5)
    unproven = outcome.rank_score(confidence=0.3, outcome=0.1, keyword=0.5)
    assert proven > unproven
