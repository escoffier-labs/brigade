"""Fleet quota observation contract: parser, immutable store, and admission."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from brigade import fleet_hub, fleet_hub_policy, fleet_policy, fleet_quota


def _now() -> datetime:
    return datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)


def _iso(stamp: datetime) -> str:
    return stamp.isoformat()


def _routing() -> dict:
    return {
        "enabled": True,
        "telemetry_ttl_seconds": 300,
        "reservation_ttl_seconds": 300,
        "quota_reserve_percent": 4,
        "workload_requirements": {},
        "quota_pools": {
            "pool-grok": {
                "account_id": "acct-generic-grok",
                "provider": "xai",
                "reserve_percent": 4,
                "freshness_seconds": 300,
            },
            "pool-grokbot": {
                "account_id": "acct-generic-grokbot",
                "provider": "xai",
                "reserve_percent": 4,
                "freshness_seconds": 300,
            },
        },
    }


def _window(window_id: str, used: float, *, limit: float = 100.0, resets_in: int = 3600) -> dict:
    return {
        "window_id": window_id,
        "label": window_id,
        "used": used,
        "limit": limit,
        "unit": "percent",
        "resets_at": _iso(_now() + timedelta(seconds=resets_in)),
        "period_duration_ms": 3_600_000,
    }


def _observation(
    observation_id: str,
    *,
    pool_id: str = "pool-grok",
    account_id: str = "acct-generic-grok",
    source: str = "crossusage-probe",
    status: str = "current",
    used: float = 10.0,
    windows: list[dict] | None = None,
    collected_at: str | None = None,
    provider_time: str | None = None,
    expires_at: str | None = None,
    reason: str | None = None,
    overrides_observation_id: str | None = None,
) -> dict:
    stamp = collected_at or _iso(_now())
    body = {
        "observation_id": observation_id,
        "account_id": account_id,
        "pool_id": pool_id,
        "provider": "xai",
        "source": source,
        "source_ref": f"ref-{observation_id}",
        "collected_at": stamp,
        "provider_time": provider_time or stamp,
        "expires_at": expires_at or _iso(_now() + timedelta(seconds=300)),
        "status": status,
        "windows": windows or [_window("hourly", used)],
    }
    if reason is not None:
        body["reason"] = reason
    if overrides_observation_id is not None:
        body["overrides_observation_id"] = overrides_observation_id
    return body


def _envelope(observations: list[dict], *, collected_at: str | None = None) -> dict:
    return {
        "schema": fleet_quota.QUOTA_SCHEMA,
        "collected_at": collected_at or _iso(_now()),
        "observations": observations,
    }


@pytest.fixture()
def conn(tmp_path):
    connection = fleet_hub.init_db(tmp_path / "fleet.db")
    try:
        yield connection
    finally:
        connection.close()


def test_parse_observations_accepts_the_exact_v1_contract():
    parsed = fleet_quota.parse_observations(_envelope([_observation("obs-1")]))
    assert parsed["schema"] == fleet_quota.QUOTA_SCHEMA
    assert parsed["observations"][0]["source"] == "crossusage-probe"
    assert parsed["observations"][0]["windows"][0]["unit"] == "percent"


@pytest.mark.parametrize(
    "mutate, fragment",
    [
        (lambda body: body.update(schema="brigade.fleet_quota_observations.v0"), "schema"),
        (lambda body: body["observations"][0].update(source="guess"), "source"),
        (lambda body: body["observations"][0].update(status="maybe"), "status"),
        (lambda body: body["observations"][0]["windows"][0].update(unit="tokens"), "unit"),
        (lambda body: body["observations"][0]["windows"][0].update(used=float("nan")), "finite"),
        (lambda body: body["observations"][0]["windows"][0].update(limit=float("inf")), "finite"),
        (lambda body: body["observations"][0].pop("observation_id"), "observation_id"),
        (lambda body: body["observations"][0].update(pool_id="pool grok"), "pool_id"),
    ],
)
def test_parse_observations_rejects_malformed_input(mutate, fragment):
    body = _envelope([_observation("obs-1")])
    mutate(body)
    with pytest.raises(fleet_quota.FleetQuotaError) as excinfo:
        fleet_quota.parse_observations(body)
    assert fragment in str(excinfo.value)


def test_ingest_is_idempotent_for_exact_content_and_rejects_conflicting_ids(conn, monkeypatch):
    monkeypatch.setattr(fleet_quota, "_now", _now)
    first = fleet_quota.ingest_observations(conn, _envelope([_observation("obs-1", used=12.0)]))
    assert first["accepted"] == 1
    assert first["duplicate"] == 0
    again = fleet_quota.ingest_observations(conn, _envelope([_observation("obs-1", used=12.0)]))
    assert again["accepted"] == 0
    assert again["duplicate"] == 1
    assert again["version"] == first["version"]
    with pytest.raises(fleet_quota.FleetQuotaError) as excinfo:
        fleet_quota.ingest_observations(conn, _envelope([_observation("obs-1", used=40.0)]))
    assert "conflict" in str(excinfo.value).lower()


def test_quota_ingest_never_increments_policy_version(conn, monkeypatch):
    monkeypatch.setattr(fleet_quota, "_now", _now)
    before = fleet_hub_policy.current_policy(conn)
    fleet_quota.ingest_observations(conn, _envelope([_observation("obs-1")]))
    after = fleet_hub_policy.current_policy(conn)
    assert after["revision"] == before["revision"]
    assert after["digest"] == before["digest"]
    assert fleet_quota.quota_version(conn) >= 1


def test_missing_and_error_readings_are_unknown_not_zero_or_unlimited(conn, monkeypatch):
    monkeypatch.setattr(fleet_quota, "_now", _now)
    routing = _routing()
    missing = fleet_quota.admit_pool(conn, "pool-grok", routing=routing, now=_now())
    assert missing["admitted"] is False
    assert "quota-unknown" in missing["reasons"]
    assert "quota-exhausted" not in missing["reasons"]

    fleet_quota.ingest_observations(
        conn, _envelope([_observation("obs-error", status="error", used=0.0, reason="probe failed")])
    )
    errored = fleet_quota.admit_pool(conn, "pool-grok", routing=routing, now=_now())
    assert errored["admitted"] is False
    assert "quota-unknown" in errored["reasons"]
    assert "quota-exhausted" not in errored["reasons"]


def test_quota_exhaustion_is_distinct_from_missing(conn, monkeypatch):
    monkeypatch.setattr(fleet_quota, "_now", _now)
    routing = _routing()
    fleet_quota.ingest_observations(conn, _envelope([_observation("obs-full", used=97.0)]))
    exhausted = fleet_quota.admit_pool(conn, "pool-grok", routing=routing, now=_now())
    assert exhausted["admitted"] is False
    assert "quota-exhausted" in exhausted["reasons"]
    assert "quota-unknown" not in exhausted["reasons"]

    fleet_quota.ingest_observations(
        conn,
        _envelope(
            [
                _observation(
                    "obs-bot",
                    pool_id="pool-grokbot",
                    account_id="acct-generic-grokbot",
                    used=10.0,
                )
            ]
        ),
    )
    ok = fleet_quota.admit_pool(conn, "pool-grokbot", routing=routing, now=_now())
    assert ok["admitted"] is True
    grok = fleet_quota.admit_pool(conn, "pool-grok", routing=routing, now=_now())
    assert grok["admitted"] is False


def test_operator_override_is_exact_account_pool_window_until_expiry(conn, monkeypatch):
    monkeypatch.setattr(fleet_quota, "_now", _now)
    routing = _routing()
    fleet_quota.ingest_observations(conn, _envelope([_observation("obs-sensor", used=97.0)]))
    with pytest.raises(fleet_quota.FleetQuotaError):
        fleet_quota.ingest_observations(
            conn,
            _envelope(
                [
                    _observation(
                        "obs-op",
                        source="operator",
                        used=10.0,
                        overrides_observation_id="obs-sensor",
                    )
                ]
            ),
        )
    fleet_quota.ingest_observations(
        conn,
        _envelope(
            [
                _observation(
                    "obs-op",
                    source="operator",
                    used=10.0,
                    reason="operator corrected the hourly window",
                    overrides_observation_id="obs-sensor",
                    expires_at=_iso(_now() + timedelta(seconds=120)),
                )
            ]
        ),
    )
    admitted = fleet_quota.admit_pool(conn, "pool-grok", routing=routing, now=_now())
    assert admitted["admitted"] is True

    expired = fleet_quota.admit_pool(conn, "pool-grok", routing=routing, now=_now() + timedelta(seconds=200))
    assert expired["admitted"] is False
    assert "quota-unknown" in expired["reasons"] or "quota-exhausted" in expired["reasons"]


def test_past_reset_does_not_auto_zero_used(conn, monkeypatch):
    monkeypatch.setattr(fleet_quota, "_now", _now)
    routing = _routing()
    stale_collect = _iso(_now() - timedelta(seconds=900))
    fleet_quota.ingest_observations(
        conn,
        _envelope(
            [
                _observation(
                    "obs-reset",
                    used=90.0,
                    collected_at=stale_collect,
                    provider_time=stale_collect,
                    windows=[_window("hourly", 90.0, resets_in=-60)],
                )
            ]
        ),
    )
    decision = fleet_quota.admit_pool(conn, "pool-grok", routing=routing, now=_now())
    assert decision["admitted"] is False
    assert "quota-unknown" in decision["reasons"]
    assert "quota-exhausted" not in decision["reasons"]


def test_overlapping_windows_are_checked_independently_never_summed(conn, monkeypatch):
    monkeypatch.setattr(fleet_quota, "_now", _now)
    routing = _routing()
    fleet_quota.ingest_observations(
        conn,
        _envelope(
            [
                _observation(
                    "obs-overlap",
                    windows=[_window("hourly", 10.0), _window("daily", 97.0, resets_in=86400)],
                )
            ]
        ),
    )
    decision = fleet_quota.admit_pool(conn, "pool-grok", routing=routing, now=_now())
    assert decision["admitted"] is False
    assert "quota-exhausted" in decision["reasons"]
    window_ids = {row["window_id"] for row in decision["windows"]}
    assert window_ids == {"hourly", "daily"}


def test_conflicting_current_readings_are_unknown(conn, monkeypatch):
    monkeypatch.setattr(fleet_quota, "_now", _now)
    routing = _routing()
    first = _observation("obs-a", used=10.0)
    first["source_ref"] = "probe-a"
    second = _observation("obs-b", used=40.0)
    second["source_ref"] = "probe-b"
    fleet_quota.ingest_observations(conn, _envelope([first]))
    fleet_quota.ingest_observations(conn, _envelope([second]))
    decision = fleet_quota.admit_pool(conn, "pool-grok", routing=routing, now=_now())
    assert decision["admitted"] is False
    assert "quota-unknown" in decision["reasons"]


def test_name_heuristics_do_not_alias_grok_and_grokbot_pools(conn, monkeypatch):
    monkeypatch.setattr(fleet_quota, "_now", _now)
    routing = _routing()
    fleet_quota.ingest_observations(
        conn,
        _envelope(
            [
                _observation(
                    "obs-named",
                    pool_id="pool-grok",
                    windows=[_window("hourly", 10.0, limit=100.0)],
                )
            ]
        ),
    )
    grok = fleet_quota.admit_pool(conn, "pool-grok", routing=routing, now=_now())
    bot = fleet_quota.admit_pool(conn, "pool-grokbot", routing=routing, now=_now())
    assert grok["admitted"] is True
    assert bot["admitted"] is False
    assert "quota-unknown" in bot["reasons"]


def test_sanitized_raw_is_retained_and_bounded(conn, monkeypatch):
    monkeypatch.setattr(fleet_quota, "_now", _now)
    fleet_quota.ingest_observations(conn, _envelope([_observation("obs-raw", used=11.0)]))
    stored = fleet_quota.list_observations(conn)
    assert stored[0]["observation_id"] == "obs-raw"
    assert stored[0]["raw"]["windows"][0]["used"] == 11.0


def test_nonfinite_window_is_unknown_not_admitted(conn, monkeypatch):
    monkeypatch.setattr(fleet_quota, "_now", _now)
    with pytest.raises(fleet_quota.FleetQuotaError):
        fleet_quota.parse_observations(_envelope([_observation("obs-nan", windows=[_window("hourly", float("nan"))])]))


def test_quota_schema_is_additive_on_hub_init(conn):
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "fleet_quota_observations" in tables
    assert "fleet_quota_meta" in tables
    assert fleet_hub_policy.current_policy(conn)["revision"] == 1


def test_routing_defaults_are_generic_and_disabled():
    parsed = fleet_policy.parse_document(fleet_policy.empty_document())
    routing = parsed["routing"]
    assert routing["enabled"] is False
    assert routing["telemetry_ttl_seconds"] == 300
    assert routing["reservation_ttl_seconds"] == 300
    assert routing["quota_reserve_percent"] == 4
    assert routing["workload_requirements"] == {}
    assert routing["quota_pools"] == {}


def test_unknown_pool_is_not_admitted(conn, monkeypatch):
    monkeypatch.setattr(fleet_quota, "_now", _now)
    decision = fleet_quota.admit_pool(conn, "pool-missing", routing=_routing(), now=_now())
    assert decision["admitted"] is False
    assert "quota-unknown" in decision["reasons"]


def test_null_provider_time_uses_collected_at_and_does_not_invent_a_stamp(conn, monkeypatch):
    monkeypatch.setattr(fleet_quota, "_now", _now)
    observation = _observation("obs-null-provider", used=10.0)
    observation["provider_time"] = None
    fleet_quota.ingest_observations(conn, _envelope([observation]))
    stored = fleet_quota.list_observations(conn)[0]
    assert stored["provider_time"] is None
    decision = fleet_quota.admit_pool(conn, "pool-grok", routing=_routing(), now=_now())
    assert decision["admitted"] is True


def test_latest_error_is_unknown_even_when_an_older_current_reading_exists(conn, monkeypatch):
    monkeypatch.setattr(fleet_quota, "_now", _now)
    earlier = _iso(_now() - timedelta(seconds=30))
    fleet_quota.ingest_observations(
        conn, _envelope([_observation("obs-old-good", used=10.0, collected_at=earlier, provider_time=earlier)])
    )
    fleet_quota.ingest_observations(
        conn, _envelope([_observation("obs-new-error", status="error", used=0.0, reason="probe failed")])
    )
    decision = fleet_quota.admit_pool(conn, "pool-grok", routing=_routing(), now=_now())
    assert decision["admitted"] is False
    assert "quota-unknown" in decision["reasons"]
    assert "quota-exhausted" not in decision["reasons"]


def test_later_current_reading_replaces_an_earlier_one_instead_of_conflicting(conn, monkeypatch):
    monkeypatch.setattr(fleet_quota, "_now", _now)
    earlier = _iso(_now() - timedelta(seconds=20))
    fleet_quota.ingest_observations(
        conn, _envelope([_observation("obs-earlier", used=97.0, collected_at=earlier, provider_time=earlier)])
    )
    fleet_quota.ingest_observations(conn, _envelope([_observation("obs-later", used=10.0)]))
    decision = fleet_quota.admit_pool(conn, "pool-grok", routing=_routing(), now=_now())
    assert decision["admitted"] is True


def test_operator_correction_wins_exact_target_until_expiry_even_against_a_newer_probe(conn, monkeypatch):
    monkeypatch.setattr(fleet_quota, "_now", _now)
    fleet_quota.ingest_observations(conn, _envelope([_observation("obs-sensor", used=97.0)]))
    fleet_quota.ingest_observations(
        conn,
        _envelope(
            [
                _observation(
                    "obs-op",
                    source="operator",
                    used=10.0,
                    reason="operator corrected the hourly window",
                    overrides_observation_id="obs-sensor",
                    expires_at=_iso(_now() + timedelta(seconds=120)),
                )
            ]
        ),
    )
    later = _iso(_now() + timedelta(seconds=5))
    fleet_quota.ingest_observations(
        conn,
        _envelope(
            [_observation("obs-newer-probe", used=98.0, collected_at=later, provider_time=later)],
            collected_at=later,
        ),
    )
    admitted = fleet_quota.admit_pool(conn, "pool-grok", routing=_routing(), now=_now() + timedelta(seconds=10))
    assert admitted["admitted"] is True


def test_same_instant_conflicting_operator_corrections_are_unknown(conn, monkeypatch):
    monkeypatch.setattr(fleet_quota, "_now", _now)
    fleet_quota.ingest_observations(conn, _envelope([_observation("obs-sensor", used=50.0)]))
    stamp = _iso(_now())
    expires = _iso(_now() + timedelta(seconds=120))
    first = _observation(
        "obs-op-a",
        source="operator",
        used=10.0,
        reason="operator a",
        overrides_observation_id="obs-sensor",
        expires_at=expires,
        collected_at=stamp,
        provider_time=stamp,
    )
    second = _observation(
        "obs-op-b",
        source="operator",
        used=20.0,
        reason="operator b",
        overrides_observation_id="obs-sensor",
        expires_at=expires,
        collected_at=stamp,
        provider_time=stamp,
    )
    fleet_quota.ingest_observations(conn, _envelope([first]))
    fleet_quota.ingest_observations(conn, _envelope([second]))
    decision = fleet_quota.admit_pool(conn, "pool-grok", routing=_routing(), now=_now())
    assert decision["admitted"] is False
    assert "quota-unknown" in decision["reasons"]


def test_provider_or_account_mismatch_is_unknown_not_capacity(conn, monkeypatch):
    monkeypatch.setattr(fleet_quota, "_now", _now)
    mismatched = _observation("obs-mismatch", used=10.0)
    mismatched["provider"] = "other-provider"
    fleet_quota.ingest_observations(conn, _envelope([mismatched]))
    decision = fleet_quota.admit_pool(conn, "pool-grok", routing=_routing(), now=_now())
    assert decision["admitted"] is False
    assert "quota-unknown" in decision["reasons"]


def test_requests_unit_converts_reserve_percent_and_does_not_treat_raw_remaining_as_percent(conn, monkeypatch):
    monkeypatch.setattr(fleet_quota, "_now", _now)
    observation = _observation(
        "obs-requests",
        used=90.0,
        windows=[
            {
                "window_id": "hourly",
                "label": "hourly",
                "used": 90.0,
                "limit": 1000.0,
                "unit": "requests",
                "resets_at": _iso(_now() + timedelta(seconds=3600)),
                "period_duration_ms": 3_600_000,
            }
        ],
    )
    fleet_quota.ingest_observations(conn, _envelope([observation]))
    decision = fleet_quota.admit_pool(conn, "pool-grok", routing=_routing(), now=_now())
    assert decision["admitted"] is True
    later_stamp = _iso(_now() + timedelta(seconds=5))
    exhausted = _observation(
        "obs-requests-full",
        used=970.0,
        collected_at=later_stamp,
        provider_time=later_stamp,
        windows=[
            {
                "window_id": "hourly",
                "label": "hourly",
                "used": 970.0,
                "limit": 1000.0,
                "unit": "requests",
                "resets_at": _iso(_now() + timedelta(seconds=3600)),
                "period_duration_ms": 3_600_000,
            }
        ],
    )
    fleet_quota.ingest_observations(conn, _envelope([exhausted], collected_at=later_stamp))
    later = fleet_quota.admit_pool(conn, "pool-grok", routing=_routing(), now=_now() + timedelta(seconds=5))
    assert later["admitted"] is False
    assert "quota-exhausted" in later["reasons"]


def test_configured_windows_must_all_be_present(conn, monkeypatch):
    monkeypatch.setattr(fleet_quota, "_now", _now)
    routing = _routing()
    routing["quota_pools"]["pool-grok"]["windows"] = {
        "hourly": {"unit": "percent", "limit": 100},
        "daily": {"unit": "percent", "limit": 100},
    }
    fleet_quota.ingest_observations(conn, _envelope([_observation("obs-partial", used=10.0)]))
    decision = fleet_quota.admit_pool(conn, "pool-grok", routing=routing, now=_now())
    assert decision["admitted"] is False
    assert "quota-unknown" in decision["reasons"]


def test_fresh_observation_with_elapsed_reset_is_unknown_not_capacity(conn, monkeypatch):
    monkeypatch.setattr(fleet_quota, "_now", _now)
    fleet_quota.ingest_observations(
        conn,
        _envelope([_observation("obs-reset-fresh", used=10.0, windows=[_window("hourly", 10.0, resets_in=-1)])]),
    )
    decision = fleet_quota.admit_pool(conn, "pool-grok", routing=_routing(), now=_now())
    assert decision["admitted"] is False
    assert "quota-unknown" in decision["reasons"]
    assert "quota-exhausted" not in decision["reasons"]
