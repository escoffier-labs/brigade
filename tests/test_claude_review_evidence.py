"""Offline contracts for the managed review evidence boundary.

All app/check IDs and repository names below are synthetic fixtures.
"""

from dataclasses import asdict

import pytest

from brigade.claude_review_evidence import normalize_check_run, parse_severity_marker


HEAD = "a" * 40
OLD_HEAD = "b" * 40
OBSERVED = "2026-10-06T12:00:00Z"
APP_ID = 424242
APP_SLUG = "example-managed-review"
MARKER = '<!-- bughunter-severity: {"normal": 2, "nit": 1, "pre_existing": 0} -->'
ZERO = '<!-- bughunter-severity: {"normal": 0, "nit": 0, "pre_existing": 0} -->'
URL = "https://github.com/example-owner/example-repo/runs/1001"


def check_run(**overrides):
    return {
        "id": 1001,
        "name": "Claude Code Review",
        "app": {"id": APP_ID, "slug": APP_SLUG},
        "head_sha": HEAD,
        "status": "completed",
        "conclusion": "neutral",
        "started_at": "2026-10-06T10:00:00Z",
        "completed_at": "2026-10-06T11:00:00Z",
        "details_url": URL,
        "output": {"text": "Findings table\n" + MARKER},
        **overrides,
    }


def normalize(value, **overrides):
    return normalize_check_run(
        value,
        **{
            "expected_app_id": APP_ID,
            "expected_app_slug": APP_SLUG,
            "current_head": HEAD,
            "observed_at": OBSERVED,
            **overrides,
        },
    )


@pytest.mark.parametrize("text,counts", [(MARKER, (2, 1, 0)), (ZERO, (0, 0, 0))])
def test_documented_marker_counts(text, counts):
    parsed = parse_severity_marker("Summary\n" + text + "\n")
    assert parsed.disposition == "known"
    assert parsed.reason is None
    assert parsed.counts is not None
    assert (parsed.counts.normal, parsed.counts.nit, parsed.counts.pre_existing) == counts


@pytest.mark.parametrize(
    "text",
    [
        None,
        {},
        "No findings",
        'bughunter-severity: {"normal": 0, "nit": 0, "pre_existing": 0}',
        '<!-- bughunter-severity: {"normal": true, "nit": 0, "pre_existing": 0} -->',
        '<!-- bughunter-severity: {"normal": -1, "nit": 0, "pre_existing": 0} -->',
        '<!-- bughunter-severity: {"normal": 1.0, "nit": 0, "pre_existing": 0} -->',
        '<!-- bughunter-severity: {"normal": "0", "nit": 0, "pre_existing": 0} -->',
        '<!-- bughunter-severity: {"normal": null, "nit": 0, "pre_existing": 0} -->',
        '<!-- bughunter-severity: {"normal": NaN, "nit": 0, "pre_existing": 0} -->',
        '<!-- bughunter-severity: {"normal": Infinity, "nit": 0, "pre_existing": 0} -->',
        '<!-- bughunter-severity: {"normal": 0, "nit": 0} -->',
        '<!-- bughunter-severity: {"normal": 0, "nit": 0, "pre_existing": 0, "version": 2} -->',
        '<!-- bughunter-severity: {"normal": 1, "normal": 0, "nit": 0, "pre_existing": 0} -->',
        '<!-- bughunter-severity: {"normal": 0, "nit": 0, "pre_existing": 0, "nit": 0} -->',
        "<!-- bughunter-severity: [] -->",
        "<!-- bughunter-severity: {broken} -->",
        ZERO.removesuffix(" -->"),
        ZERO + "\n" + ZERO,
        ZERO + "\n" + MARKER,
        ZERO + "\n<!-- bughunter-severity: {broken} -->",
        ZERO + "\nChanged output format",
        '<!-- bughunter-severity: {"normal": 2147483648, "nit": 0, "pre_existing": 0} -->',
        "<!-- bughunter-severity: " + "[" * 1000 + " -->",
        "x" * 65537 + ZERO,
        "é" * 40000 + ZERO,
        "\ud800" + ZERO,
    ],
)
def test_unknown_output_never_becomes_zero(text):
    parsed = parse_severity_marker(text)
    assert parsed.disposition == "unknown"
    assert parsed.reason
    assert parsed.counts is None


def test_neutral_with_findings_is_completed_evidence_without_approval():
    evidence = normalize(check_run())
    assert evidence is not None
    assert evidence.check_id == 1001
    assert evidence.app_id == APP_ID
    assert evidence.app_slug == APP_SLUG
    assert evidence.source == "managed-claude-code-review"
    assert evidence.provider == "github"
    assert evidence.reviewed_head == HEAD
    assert evidence.observed_at == OBSERVED
    assert evidence.started_at == "2026-10-06T10:00:00Z"
    assert evidence.completed_at == "2026-10-06T11:00:00Z"
    assert evidence.lifecycle == "completed"
    assert evidence.conclusion == "neutral"
    assert evidence.coverage == "current"
    assert evidence.current_findings_known
    assert not evidence.current_zero_findings
    assert evidence.severity.counts.normal == 2
    assert "approval" not in asdict(evidence)


@pytest.mark.parametrize(
    "head,coverage,zero", [(HEAD, "current", True), (OLD_HEAD, "stale", False), (None, "unknown", False)]
)
def test_zero_requires_completed_valid_current_head(head, coverage, zero):
    evidence = normalize(check_run(head_sha=head, output={"text": ZERO}))
    assert evidence.coverage == coverage
    assert evidence.current_zero_findings is zero
    assert evidence.reviewed_head == head


@pytest.mark.parametrize("status", ["queued", "in_progress", "future-state", None, {}])
def test_incomplete_or_unknown_execution_cannot_claim_zero(status):
    evidence = normalize(check_run(status=status, output={"text": ZERO}))
    assert evidence.lifecycle == (status if status in ("queued", "in_progress") else "unknown")
    assert not evidence.current_findings_known
    assert not evidence.current_zero_findings


@pytest.mark.parametrize(
    "overrides",
    [
        {"app": {"id": 424243, "slug": APP_SLUG}},
        {"app": {"id": APP_ID, "slug": "spoof-review"}},
        {"app": {"id": True, "slug": APP_SLUG}},
        {"app": {"id": str(APP_ID), "slug": APP_SLUG}},
        {"app": {"slug": APP_SLUG}},
        {"app": None},
        {"app": []},
        {"name": "install-from-source (claude)"},
        {"name": "Claude Code Review", "app": {"id": 15368, "slug": "github-actions"}},
        {"id": None},
        {"id": True},
        {"id": -1},
        {"id": "1001"},
        {"id": 2**63},
    ],
)
def test_untrusted_or_unidentifiable_check_is_rejected(overrides):
    assert normalize(check_run(**overrides)) is None


def test_github_actions_is_rejected_even_if_caller_misconfigures_identity():
    value = check_run(app={"id": 15368, "slug": "github-actions"})
    assert normalize(value, expected_app_id=15368, expected_app_slug="github-actions") is None


@pytest.mark.parametrize("value", [None, [], "Claude Code Review"])
def test_non_object_check_is_rejected(value):
    assert normalize(value) is None


def test_delayed_old_head_and_reruns_retain_distinct_provider_ids():
    current = normalize(check_run(output={"text": ZERO}))
    delayed = normalize(check_run(id=1002, head_sha=OLD_HEAD, completed_at="2026-10-06T11:30:00Z"))
    assert current.current_zero_findings
    assert delayed.coverage == "stale"
    assert delayed.severity.counts.normal == 2
    assert not delayed.current_findings_known
    assert current.identity_key != delayed.identity_key
    assert normalize(check_run()).identity_key == current.identity_key


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/example-owner/example-repo/runs/1001",
        "https://github.com.evil.example/example-owner/example-repo/runs/1001",
        "https://user:secret@github.com/example-owner/example-repo/runs/1001",  # content-guard: allow email
        "https://github.com:443/example-owner/example-repo/runs/1001",
        URL + "?token=private-fixture",
        URL + "#private-fixture",
        URL + "?",
        URL + "#",
        URL + "?#",
        "https://github.com/example-owner/../runs/1001",
        "https://github.com/example-owner/example-repo/runs/%31%30%30%31",
        "https://github.com/example-owner/example-repo/runs/1002",
        "https://github.com/login",
        "https://[broken",
        "https://github.com/\nexample-owner/example-repo/runs/1001",
        "x" * 2049,
        None,
        {},
    ],
)
def test_unsafe_or_unbound_details_url_is_omitted(url):
    evidence = normalize(check_run(details_url=url))
    assert evidence.source_url is None
    assert evidence.severity.disposition == "known"


def test_malformed_output_keeps_safe_link_and_discards_raw_provider_content():
    evidence = normalize(check_run(output={"text": "private-fixture", "summary": "private-fixture"}))
    assert evidence.source_url == URL
    assert evidence.severity.disposition == "unknown"
    assert not evidence.current_findings_known
    assert "private-fixture" not in repr(asdict(evidence))
    fallback = normalize(check_run(details_url="https://evil.example/", html_url=URL))
    assert fallback.source_url == URL


@pytest.mark.parametrize("output", [None, [], {}, {"text": []}])
def test_missing_or_wrong_type_output_remains_unknown(output):
    evidence = normalize(check_run(output=output))
    assert evidence.severity.disposition == "unknown"
    assert evidence.severity.counts is None
    assert not evidence.current_zero_findings


def test_provider_metadata_is_validated_without_throwing_or_promoting_review_states():
    evidence = normalize(
        check_run(head_sha="private-fixture", conclusion="COMMENTED", started_at={}, completed_at="not-a-date")
    )
    assert evidence.reviewed_head is None
    assert evidence.coverage == "unknown"
    assert evidence.conclusion is None
    assert evidence.started_at is None
    assert evidence.completed_at is None
    assert "private-fixture" not in repr(asdict(evidence))
    offset = normalize(check_run(started_at="2026-10-06T12:00:00+02:00", head_sha=HEAD.upper()))
    assert offset.started_at == "2026-10-06T10:00:00Z"
    assert offset.reviewed_head == HEAD


@pytest.mark.parametrize("identity", [{"expected_app_id": True}, {"expected_app_slug": ""}])
def test_invalid_trusted_identity_fails_closed(identity):
    assert normalize(check_run(), **identity) is None


@pytest.mark.parametrize("offset", ["+00:60", "-00:99", "+12:75", "+99:00"])
def test_invalid_offset_is_unknown_for_provider_and_rejected_for_caller(offset):
    invalid = "2026-10-06T10:00:00" + offset
    evidence = normalize(check_run(started_at=invalid, completed_at=invalid))
    assert evidence.started_at is None
    assert evidence.completed_at is None
    with pytest.raises(ValueError):
        normalize(check_run(), observed_at=invalid)
