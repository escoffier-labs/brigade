"""Offline collector contracts: bounded GETs, trust, history and redaction."""

import copy
import importlib
import json

import pytest

HEAD = "a" * 40
OLD = "b" * 40
OBSERVED = "2026-01-03T00:00:00Z"
APP = {"id": 123, "slug": "example-review"}
BASE = "/repos/example/project"
CHECKS = f"{BASE}/commits/{HEAD}/check-runs"
REVIEWS = f"{BASE}/pulls/7/reviews"
INLINE = f"{BASE}/pulls/7/comments"
COMMENTS = f"{BASE}/issues/7/comments"


def api():
    return importlib.import_module("brigade.claude_review_import")


def check(check_id=10, **changes):
    row = {
        "id": check_id,
        "name": "Claude Code Review",
        "app": APP,
        "head_sha": HEAD,
        "status": "completed",
        "conclusion": "neutral",
        "started_at": "2026-01-01T00:00:00Z",
        "completed_at": "2026-01-02T00:00:00Z",
        "details_url": "https://evil.example/secret",
        "html_url": f"https://github.com/example/project/runs/{check_id}",
        "output": {"text": '<!-- bughunter-severity: {"normal": 2, "nit": 0, "pre_existing": 0} -->'},
    }
    row.update(changes)
    return row


def annotation(**changes):
    row = {
        "path": "src/example.py",
        "start_line": 2,
        "end_line": 2,
        "annotation_level": "warning",
        "message": "PRIVATE FINDING",
        "raw_details": "PRIVATE CODE",
    }
    row.update(changes)
    return row


class FixtureGet:
    def __init__(self, pages=None):
        self.pages = pages or {}
        self.calls = []

    def get(self, path, *, params, max_bytes):
        self.calls.append((path, dict(params), max_bytes))
        value = self.pages.get((path, params["page"]), {"total_count": 0, "check_runs": []} if path == CHECKS else [])
        if isinstance(value, api().GetResponse):
            return value
        if isinstance(value, Exception):
            raise value
        if isinstance(value, tuple):
            status, body = value
        else:
            status, body = 200, json.dumps(value).encode()
        return api().GetResponse(status, body)


def collect(transport, **options):
    return api().collect_managed_review(
        transport,
        owner="example",
        repo="project",
        pr_number=7,
        current_head=HEAD,
        expected_app_id=123,
        expected_app_slug="example-review",
        observed_at=OBSERVED,
        **options,
    )


def test_exact_head_gets_are_allowlisted_and_neutral_findings_are_not_approval():
    transport = FixtureGet(
        {
            (CHECKS, 1): {"total_count": 1, "check_runs": [check()]},
            (f"{BASE}/check-runs/10/annotations", 1): [annotation()],
        }
    )
    result = collect(transport)
    evidence = result["checks"][0]
    assert evidence["coverage"] == "current"
    assert evidence["severity"]["counts"]["normal"] == 2
    assert evidence["current_zero_findings"] is False
    assert evidence["conclusion"] == "neutral"
    assert result["finding_totals"] is None
    assert "approval" not in result
    assert transport.calls[0][1]["filter"] == "all"
    assert {call[0] for call in transport.calls} == {
        CHECKS,
        REVIEWS,
        INLINE,
        COMMENTS,
        f"{BASE}/check-runs/10/annotations",
    }
    assert result["annotations"][0]["location"] == {"path": "src/example.py", "start_line": 2, "end_line": 2}
    retained = json.dumps(result)
    assert "PRIVATE" not in retained and "evil.example" not in retained


@pytest.mark.parametrize(
    "text,known,zero",
    [
        ('<!-- bughunter-severity: {"normal": 0, "nit": 0, "pre_existing": 0} -->', True, True),
        (None, False, False),
        ("malformed", False, False),
        ('<!-- bughunter-severity: {"future": 0} -->', False, False),
    ],
)
def test_severity_disposition_does_not_become_zero(text, known, zero):
    result = collect(FixtureGet({(CHECKS, 1): {"total_count": 1, "check_runs": [check(output={"text": text})]}}))
    row = result["checks"][0]
    assert row["current_findings_known"] is known
    assert row["current_zero_findings"] is zero


def test_reruns_duplicates_and_late_old_completion_preserve_coverage():
    earlier = check(10, completed_at="2026-01-02T23:00:00Z")
    newer = check(11, started_at="2026-01-02T00:00:00Z", status="in_progress", completed_at=None)
    stale = check(12, head_sha=OLD, completed_at="2026-01-02T23:30:00Z")
    result = collect(FixtureGet({(CHECKS, 1): {"total_count": 4, "check_runs": [earlier, newer, stale, earlier]}}))
    assert [c["check_id"] for c in result["checks"]] == [10, 11, 12]
    assert result["checks"][0]["superseded_by"] == 11
    assert result["checks"][0]["current_findings_known"] is False
    assert result["checks"][1]["superseded_by"] is None
    assert result["checks"][2]["coverage"] == "stale"
    assert result["checks"][2]["current_findings_known"] is False
    assert result["current_head"] == HEAD
    assert result["sources"][0]["duplicate_objects"] == 1


def test_annotation_ordinals_and_duplicate_representations_remain_ambiguous():
    inline = {
        "id": 50,
        "path": "src/example.py",
        "line": 2,
        "start_line": 2,
        "commit_id": HEAD,
        "original_commit_id": HEAD,
        "side": "RIGHT",
        "body": "PRIVATE CODE",
        "user": {"login": "example-review[bot]"},
        "performed_via_github_app": APP,
    }
    result = collect(
        FixtureGet(
            {
                (CHECKS, 1): {"total_count": 1, "check_runs": [check()]},
                (f"{BASE}/check-runs/10/annotations", 1): [annotation(), annotation()],
                (INLINE, 1): [inline, inline],
            }
        )
    )
    assert len(result["annotations"]) == 2
    assert len({a["identity_key"] for a in result["annotations"]}) == 2
    assert all(a["identity_kind"] == "parent-ordinal-location" for a in result["annotations"])
    assert all(a["ambiguity"] == "possible-duplicate-representation" for a in result["annotations"])
    assert len(result["comments"]) == 1
    assert result["comments"][0]["provider_identity"] == "unverified"
    assert result["finding_totals"] is None


def test_comment_app_metadata_is_required_and_review_login_is_never_identity():
    comment = {
        "id": 40,
        "performed_via_github_app": APP,
        "user": {"type": "Bot"},
        "body": "PRIVATE",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z",
        "html_url": "https://github.com/example/project/pull/7#issuecomment-40",
    }
    spoof = dict(comment, id=41, performed_via_github_app={"id": 456, "slug": "example-review"})
    review = {
        "id": 60,
        "state": "COMMENTED",
        "commit_id": OLD,
        "submitted_at": "2026-01-02T00:00:00Z",
        "user": {"login": "example-review[bot]"},
        "performed_via_github_app": APP,
        "html_url": "https://github.com/example/project/pull/7#pullrequestreview-60",
        "body": "PRIVATE",
    }
    result = collect(FixtureGet({(COMMENTS, 1): [comment, spoof], (REVIEWS, 1): [review]}))
    assert result["comments"][0]["provider_identity"] == "verified-app"
    assert result["comments"][0]["reviewed_head"] is None
    assert result["comments"][1]["provider_identity"] == "unverified"
    assert result["reviews"][0]["provider_identity"] == "unverified"
    assert result["reviews"][0]["state"] == "COMMENTED"
    assert result["reviews"][0]["coverage"] == "stale"
    assert "PRIVATE" not in json.dumps(result)
    assert any("identity-unverified" in r for r in result["incomplete_reasons"])


@pytest.mark.parametrize(
    "changes",
    [
        {"app": {"id": 456, "slug": "example-review"}},
        {"app": {"id": 123, "slug": "github-actions"}},
        {"name": "install Claude tests"},
        {"id": True},
    ],
)
def test_spoofed_app_and_ci_names_never_fetch_annotations(changes):
    transport = FixtureGet({(CHECKS, 1): {"total_count": 1, "check_runs": [check(**changes)]}})
    assert collect(transport)["checks"] == []
    assert all("annotations" not in path for path, _, _ in transport.calls)


@pytest.mark.parametrize("status,reason", [(403, "access-denied"), (404, "unavailable"), (429, "rate-limited")])
def test_http_errors_are_independent_not_absence(status, reason):
    result = collect(FixtureGet({(CHECKS, 1): (status, b"PRIVATE HTTP ERROR")}))
    assert reason in result["sources"][0]["reasons"]
    assert result["sources"][0]["complete"] is False
    assert result["sources"][0]["http_status"] == status
    assert result["complete"] is False
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        b'{"check_runs":[],"total_count":true}',
        b'{"check_runs":[],"total_count":0,"total_count":1}',
        b'{"check_runs":{}}',
    ],
)
def test_malformed_and_future_payloads_are_incomplete(body):
    result = collect(FixtureGet({(CHECKS, 1): (200, body)}))
    assert "malformed-payload" in result["sources"][0]["reasons"]


def test_each_budget_stops_before_excess_and_reports_truncation():
    pages = {(CHECKS, 1): {"total_count": 5, "check_runs": [check(), check(11)]}}
    for limits, reason in [
        (api().Limits(max_pages=1, per_page=2), "page-budget"),
        (api().Limits(max_requests=1), "request-budget"),
        (api().Limits(max_objects=1), "object-budget"),
        (api().Limits(max_bytes=8), "byte-budget"),
    ]:
        transport = FixtureGet(pages)
        result = collect(transport, limits=limits)
        assert any(reason in r for r in result["incomplete_reasons"])
        assert result["usage"]["requests"] <= limits.max_requests
        assert result["usage"]["objects"] <= limits.max_objects
        assert result["usage"]["bytes"] <= limits.max_bytes
        assert all(cap <= limits.max_bytes for _, _, cap in transport.calls)


def test_repeated_pages_stop_and_repeated_ids_are_not_new_rounds():
    page = {"total_count": 10, "check_runs": [check(), check(11)]}
    transport = FixtureGet({(CHECKS, 1): page, (CHECKS, 2): copy.deepcopy(page)})
    result = collect(transport, limits=api().Limits(per_page=2))
    assert "repeated-page" in result["sources"][0]["reasons"]
    assert len(result["checks"]) == 2
    assert len([c for c in transport.calls if c[0] == CHECKS]) == 2


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/other/project/runs/10",
        "https://github.com/example/project/runs/10?secret=1",
        "https://github.com:443/example/project/runs/10",
        "https://evil.example/code",
    ],
)
def test_links_are_bound_to_repo_and_never_fetched(url):
    transport = FixtureGet({(CHECKS, 1): {"total_count": 1, "check_runs": [check(details_url=url, html_url=url)]}})
    assert collect(transport)["checks"][0]["source_url"] is None
    assert all(path.startswith(BASE + "/") for path, _, _ in transport.calls)


def test_future_timestamps_and_unavailable_transport_remain_unknown():
    result = collect(
        FixtureGet(
            {
                (CHECKS, 1): {"total_count": 1, "check_runs": [check(completed_at="2099-01-01T00:00:00Z")]},
                (REVIEWS, 1): OSError("PRIVATE NETWORK"),
            }
        )
    )
    assert result["checks"][0]["completed_at"] is None
    assert result["checks"][0]["current_findings_known"] is False
    assert any("future-timestamp" in r for r in result["incomplete_reasons"])
    assert any("transport-unavailable" in r for r in result["incomplete_reasons"])
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.parametrize(
    "field,value",
    [
        ("owner", "../escape"),
        ("repo", ".."),
        ("pr_number", True),
        ("current_head", "main"),
        ("expected_app_slug", "github-actions"),
        ("expected_app_id", 0),
        ("observed_at", "yesterday"),
    ],
)
def test_invalid_inputs_make_no_requests(field, value):
    transport = FixtureGet()
    options = dict(
        owner="example",
        repo="project",
        pr_number=7,
        current_head=HEAD,
        expected_app_id=123,
        expected_app_slug="example-review",
        observed_at=OBSERVED,
    )
    options[field] = value
    with pytest.raises(ValueError):
        api().collect_managed_review(transport, **options)
    assert transport.calls == []


def test_annotation_only_unknown_severity_keeps_location_and_parent_link():
    result = collect(
        FixtureGet(
            {
                (CHECKS, 1): {"total_count": 1, "check_runs": [check(output={"annotations_count": 1})]},
                (f"{BASE}/check-runs/10/annotations", 1): [annotation()],
            }
        )
    )
    assert result["checks"][0]["severity"]["disposition"] == "unknown"
    assert result["checks"][0]["current_zero_findings"] is False
    assert result["annotations"][0]["source_url"] == "https://github.com/example/project/runs/10"
    assert result["annotations"][0]["location"]["start_line"] == 2
    assert result["finding_totals"] is None


def test_partial_annotations_and_malformed_ordinals_are_not_absent():
    result = collect(
        FixtureGet(
            {
                (CHECKS, 1): {"total_count": 1, "check_runs": [check(output={"annotations_count": 4})]},
                (f"{BASE}/check-runs/10/annotations", 1): [None, annotation()],
            }
        )
    )
    assert [a["ordinal"] for a in result["annotations"]] == [1, 2]
    assert result["annotations"][0]["location"] is None
    assert result["annotations"][1]["location"] is not None
    assert "annotations:10:annotations-count-mismatch" in result["incomplete_reasons"]


def test_truncation_and_http_denial_are_both_retained():
    result = collect(FixtureGet({(CHECKS, 1): api().GetResponse(403, b"PRIVATE", truncated=True)}))
    assert set(result["sources"][0]["reasons"]) == {"access-denied", "byte-budget", "truncated-response"}
    assert "PRIVATE" not in json.dumps(result)


def test_different_repeated_objects_are_ambiguous_without_replacing_evidence():
    first = check(10, status="in_progress", completed_at=None)
    transport = FixtureGet(
        {
            (CHECKS, 1): {"total_count": 2, "check_runs": [first]},
            (CHECKS, 2): {"total_count": 2, "check_runs": [check(10)]},
        }
    )
    result = collect(transport, limits=api().Limits(per_page=1))
    assert len(result["checks"]) == 1
    assert result["checks"][0]["lifecycle"] == "in_progress"
    assert result["checks"][0]["current_findings_known"] is False
    assert "conflicting-object" in result["sources"][0]["reasons"]


def test_foreign_details_link_falls_back_to_bound_html_link():
    result = collect(
        FixtureGet(
            {
                (CHECKS, 1): {
                    "total_count": 1,
                    "check_runs": [check(details_url="https://github.com/other/project/runs/10")],
                }
            }
        )
    )
    assert result["checks"][0]["source_url"] == "https://github.com/example/project/runs/10"


@pytest.mark.parametrize("changes", [{"started_at": None}, {"started_at": "malformed"}])
def test_missing_or_malformed_round_order_cannot_claim_current_zero(changes):
    zero = {"text": '<!-- bughunter-severity: {"normal": 0, "nit": 0, "pre_existing": 0} -->'}
    result = collect(
        FixtureGet({(CHECKS, 1): {"total_count": 2, "check_runs": [check(output=zero), check(11, **changes)]}})
    )
    assert result["checks"][0]["current_zero_findings"] is False
    assert result["checks"][0]["supersession_disposition"] == "unknown"


def test_completion_before_start_cannot_claim_current_zero():
    zero = {"text": '<!-- bughunter-severity: {"normal": 0, "nit": 0, "pre_existing": 0} -->'}
    result = collect(
        FixtureGet(
            {
                (CHECKS, 1): {
                    "total_count": 1,
                    "check_runs": [
                        check(
                            started_at="2026-01-02T00:00:00Z",
                            completed_at="2026-01-01T00:00:00Z",
                            output=zero,
                        )
                    ],
                }
            }
        )
    )
    assert result["checks"][0]["current_zero_findings"] is False
    assert result["checks"][0]["current_findings_known"] is False
    assert "check-runs:contradictory-timestamps" in result["incomplete_reasons"]


@pytest.mark.parametrize("field", ["max_pages", "max_requests", "max_objects", "max_bytes", "per_page"])
@pytest.mark.parametrize("value", [True, 0, -1, 2**63])
def test_limit_configuration_is_bounded_before_any_get(field, value):
    with pytest.raises(ValueError):
        api().Limits(**{field: value})


def test_overlapping_pagination_deduplicates_ids_without_claiming_completeness():
    transport = FixtureGet(
        {
            (CHECKS, 1): {"total_count": 4, "check_runs": [check(), check(11)]},
            (CHECKS, 2): {"total_count": 4, "check_runs": [check(11), check(12)]},
        }
    )
    result = collect(transport, limits=api().Limits(per_page=2))
    assert [c["check_id"] for c in result["checks"]] == [10, 11, 12]
    assert result["sources"][0]["complete"] is False
    assert "check-runs:overlapping-pagination" in result["incomplete_reasons"]
    assert result["sources"][0]["duplicate_objects"] == 1
    assert [params["page"] for path, params, _ in transport.calls if path == CHECKS] == [1, 2]
    assert result["usage"]["bytes"] == sum(s["bytes"] for s in result["sources"])


def test_overlapping_pages_cannot_hide_a_newer_round_and_claim_current_zero():
    zero = {"text": '<!-- bughunter-severity: {"normal": 0, "nit": 0, "pre_existing": 0} -->'}
    second = check(11, started_at="2026-01-01T01:00:00Z")
    third = check(12, started_at="2026-01-01T02:00:00Z", output=zero)
    result = collect(
        FixtureGet(
            {
                (CHECKS, 1): {"total_count": 4, "check_runs": [check(), second]},
                (CHECKS, 2): {"total_count": 4, "check_runs": [second, third]},
            }
        ),
        limits=api().Limits(per_page=2),
    )
    assert result["checks"][-1]["current_zero_findings"] is False
    assert result["checks"][-1]["current_findings_known"] is False


def test_inline_locations_preserve_diff_side_and_annotation_ambiguity():
    rows = [
        {"id": n, "path": "src/example.py", "line": 2, "side": side, "original_commit_id": HEAD}
        for n, side in ((50, "LEFT"), (51, "RIGHT"))
    ]
    result = collect(
        FixtureGet(
            {
                (CHECKS, 1): {"total_count": 1, "check_runs": [check()]},
                (f"{BASE}/check-runs/10/annotations", 1): [annotation()],
                (INLINE, 1): rows,
            }
        )
    )
    assert [c["location"]["side"] for c in result["comments"]] == ["LEFT", "RIGHT"]
    assert result["comments"][0]["location"] != result["comments"][1]["location"]
    assert result["annotations"][0]["ambiguity"] == "possible-duplicate-representation"


@pytest.mark.parametrize(
    "changes",
    [{"start_line": True}, {"path": "../private.py"}, {"end_line": 1}, {"annotation_level": {"future": "value"}}],
)
def test_malformed_annotation_location_or_level_is_omitted(changes):
    result = collect(
        FixtureGet(
            {
                (CHECKS, 1): {"total_count": 1, "check_runs": [check()]},
                (f"{BASE}/check-runs/10/annotations", 1): [annotation(**changes)],
            }
        )
    )
    assert "annotations:10:malformed-annotation" in result["incomplete_reasons"]
    assert result["annotations"][0]["location"] is None or result["annotations"][0]["annotation_level"] is None
