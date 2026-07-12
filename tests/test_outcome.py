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


def _fp_record(evidence_ref, ts, fingerprint, signal=1):
    return outcome.OutcomeRecord(
        "skill-x", "skill", "t", "verify", signal, evidence_ref, ts, content_fingerprint=fingerprint
    )


def test_split_by_fingerprint_grandfathers_legacy_and_drops_proven_stale():
    records = [
        _fp_record("r1", "2026-07-01T00:00:00+00:00", "old-rev"),
        _fp_record("r2", "2026-07-02T00:00:00+00:00", "old-rev", signal=-1),
        _fp_record("r3", "2026-07-03T00:00:00+00:00", "new-rev"),
        _fp_record("r4", "2026-07-04T00:00:00+00:00", None),  # pre-fingerprint capture
    ]
    cohorts = outcome.split_by_fingerprint("skill-x", records, "new-rev")
    assert cohorts.pinned
    # new-rev (+1) plus the legacy record (+1, unprovable either way); the two
    # old-rev signals are proven stale and drop out.
    assert (cohorts.current.helped, cohorts.current.hurt) == (2, 0)
    assert (cohorts.lifetime.helped, cohorts.lifetime.hurt) == (3, 1)
    assert cohorts.stale_records == 2
    assert cohorts.legacy_records == 1


def test_split_by_fingerprint_unpinned_falls_back_to_lifetime():
    # Without a resolvable current fingerprint the split is unavailable, so the
    # default score must not silently drop to zero.
    records = [
        _fp_record("r1", "2026-07-01T00:00:00+00:00", "old-rev"),
        _fp_record("r2", "2026-07-02T00:00:00+00:00", None),
    ]
    cohorts = outcome.split_by_fingerprint("skill-x", records, None)
    assert not cohorts.pinned
    assert cohorts.current == cohorts.lifetime
    assert cohorts.current.helped == 2
    assert (cohorts.stale_records, cohorts.legacy_records) == (0, 0)


def test_split_by_fingerprint_dedups_before_counting_cohorts():
    # The same physical receipt re-captured must not inflate any cohort.
    duplicate = _fp_record("r1", "2026-07-01T00:00:00+00:00", "old-rev")
    cohorts = outcome.split_by_fingerprint("skill-x", [duplicate, duplicate], "new-rev")
    assert cohorts.stale_records == 1
    assert cohorts.lifetime.helped == 1


def test_fingerprint_cohort_names_current_stale_and_legacy():
    current = _fp_record("r1", "2026-07-01T00:00:00+00:00", "new-rev")
    stale = _fp_record("r2", "2026-07-02T00:00:00+00:00", "old-rev")
    legacy = _fp_record("r3", "2026-07-03T00:00:00+00:00", None)
    assert outcome.fingerprint_cohort(current, "new-rev") == "current"
    assert outcome.fingerprint_cohort(stale, "new-rev") == "stale"
    assert outcome.fingerprint_cohort(legacy, "new-rev") == "legacy"
    # Unpinned: fingerprinted records count as current (lifetime fallback), but
    # legacy stays a distinct cohort.
    assert outcome.fingerprint_cohort(stale, None) == "current"
    assert outcome.fingerprint_cohort(legacy, None) == "legacy"


# --- Phase 2: capability cohorts, shrinkage --------------------------------


def _cap_record(fp_content, fp_cap, signal=1, ref="r", ts="2026-06-20T00:00:00+00:00"):
    return outcome.OutcomeRecord(
        "s",
        "skill",
        "t",
        "verify",
        signal,
        ref,
        ts,
        content_fingerprint=fp_content,
        capability_fingerprint=fp_cap,
    )


def test_shrink_rate_is_prior_with_no_trials_and_converges():
    assert outcome.shrink_rate(0, 0, 0.5) == 0.5  # no evidence -> the prior
    assert outcome.shrink_rate(3, 3, 0.5, kappa=4.0) == (3 + 4.0 * 0.5) / (3 + 4.0)
    near_one = outcome.shrink_rate(50, 50, 0.1)  # many clean trials pull away from a low prior
    assert near_one > 0.85


def test_current_content_records_drops_stale_keeps_legacy():
    recs = [
        _cap_record("c1", "capA", ref="r1"),
        _cap_record("c2", "capA", ref="r2"),  # proven stale content
        _cap_record(None, "capA", ref="r3"),  # pre-fingerprint content, grandfathered
    ]
    keep = {r.evidence_ref for r in outcome.current_content_records(recs, "c1")}
    assert keep == {"r1", "r3"}


def test_split_by_capability_grandfathers_legacy_and_excludes_off_cap():
    content_current = [
        _cap_record("c1", "capA", ref="r1"),
        _cap_record("c1", "capA", ref="r2"),
        _cap_record("c1", "capB", signal=-1, ref="r3"),  # different capability
        _cap_record("c1", None, ref="r4"),  # pre-context capability, grandfathered
    ]
    ch = outcome.split_by_capability("s", content_current, "capA")
    assert ch.pinned
    # capA (2) + legacy (1) count; the capB hurt is off-capability and excluded.
    assert (ch.capability.helped, ch.capability.hurt) == (3, 0)
    assert ch.off_capability_records == 1
    assert ch.capability_legacy_records == 1
    # pooled still sees all four: 3 helped / 1 hurt.
    assert (ch.pooled.helped, ch.pooled.hurt) == (3, 1)
    assert ch.shrunk_rate == outcome.shrink_rate(3, 3, 3 / 4)


def test_split_by_capability_unresolved_equals_pooled():
    content_current = [_cap_record("c1", "capA", ref="r1")]
    ch = outcome.split_by_capability("s", content_current, None)
    assert not ch.pinned
    assert ch.capability == ch.pooled
    assert ch.off_capability_records == 0
