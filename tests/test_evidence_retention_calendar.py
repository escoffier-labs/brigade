"""Calendar periods selected by the adopting organization."""

import pytest

from brigade.evidence_retention import calendar_expiry


@pytest.mark.parametrize(
    ("start", "period", "expected"),
    [
        ("2023-08-31T12:34:56Z", "P6M", "2024-02-29T12:34:56Z"),
        ("2024-08-31T12:34:56Z", "P6M", "2025-02-28T12:34:56Z"),
        ("2024-02-29T00:00:00Z", "P1Y", "2025-02-28T00:00:00Z"),
        ("2024-01-31T00:00:00Z", "P1Y1M1D", "2025-03-01T00:00:00Z"),
        ("2024-02-29T00:00:00Z", "P7Y", "2031-02-28T00:00:00Z"),
        ("2024-01-31T00:00:00Z", "P18M", "2025-07-31T00:00:00Z"),
        ("2024-02-28T00:00:00Z", "P1D", "2024-02-29T00:00:00Z"),
    ],
)
def test_calendar_expiry_clamps_months_then_adds_days(start, period, expected):
    assert calendar_expiry(start, period) == expected


@pytest.mark.parametrize(
    "period", ["P", "P0D", "P0Y0M", "P-1M", "PT6H", "P1.5M", "P1W", "P1D1Y", "P6M\n", "P" + "9" * 100 + "Y", None, True]
)
def test_calendar_expiry_rejects_malformed_or_zero_periods(period):
    with pytest.raises(ValueError, match="retention calendar"):
        calendar_expiry("2024-01-31T00:00:00Z", period)


@pytest.mark.parametrize(
    ("start", "period"),
    [
        ("9999-12-31T00:00:00Z", "P1D"),
        ("9999-12-31T00:00:00Z", "P1M"),
        ("2024-01-01T00:00:00Z", "P999999999D"),
        ("2024-02-30T00:00:00Z", "P6M"),
        ("2024-01-01", "P6M"),
        ("2024-01-01T00:00:00+00:00", "P6M"),
        (None, "P6M"),
    ],
)
def test_calendar_expiry_rejects_overflow_and_invalid_times(start, period):
    with pytest.raises(ValueError, match="retention calendar"):
        calendar_expiry(start, period)
