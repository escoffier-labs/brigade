import json
import re
import subprocess
from datetime import timedelta

import pytest

from scripts import ci_timing_report as report


def _job(name, label, start, end, conclusion="success"):
    base = "2026-11-09T10:"
    return {
        "name": name,
        "labels": [label],
        "conclusion": conclusion,
        "started_at": None if start is None else f"{base}{start}Z",
        "completed_at": None if end is None else f"{base}{end}Z",
    }


def _run(run_id, event="pull_request", conclusion="success", started="00:00", updated="10:00", attempt=1):
    return {
        "id": run_id,
        "event": event,
        "conclusion": conclusion,
        "head_sha": "a" * 40,
        "run_attempt": attempt,
        "created_at": "2026-11-09T09:59:00Z",
        "run_started_at": f"2026-11-09T10:{started}Z",
        "updated_at": f"2026-11-09T10:{updated}Z",
        # Fields the report must never copy through.
        "head_branch": "someone/private-branch",
        "display_title": "a private PR title",
        "actor": {"login": "someone"},
    }


@pytest.mark.parametrize(
    ("name", "family"),
    [
        ("test shard (3.12, 1)", "test shard 3.12"),
        ("test shard (3.11, 3)", "test shard 3.11"),
        ("mesh (windows-latest, floor)", "mesh (windows-latest, floor)"),
        ("install-from-source (repo+claude, --depth repo --harnesses claude)", "install-from-source"),
        ("hub-rust / hub (Rust stable)", "hub-rust / hub (Rust stable)"),
        ("hub-rust / hub (Rust 1.85.0)", "hub-rust / hub (Rust 1.85.0)"),
        ("windows-affected-tests", "windows-affected-tests"),
    ],
)
def test_job_family_merges_shards_but_keeps_distinct_matrix_cells(name, family):
    assert report.job_family(name) == family


def test_percentile_interpolates_linearly_and_handles_no_data():
    assert report.percentile([], 50) is None
    assert report.percentile([7], 90) == 7
    assert report.percentile([4, 1, 3, 2], 50) == 2.5
    assert report.percentile(list(range(1, 11)), 90) == pytest.approx(9.1)
    assert report.percentile([1, 2, 3], 100) == 3


def test_summarize_run_counts_runner_time_wall_time_and_drops_private_fields():
    jobs = [
        _job("lint", "ubuntu-latest", "00:00", "01:00"),
        _job("windows-affected-tests", "windows-latest", "00:00", "05:30"),
        _job("cancelled-job", "ubuntu-latest", "00:00", "00:30", conclusion="cancelled"),
        _job("skipped-job", "ubuntu-latest", "00:00", "00:00", conclusion="skipped"),
        _job("never-started", "ubuntu-latest", None, None, conclusion=None),
        _job("Feature configuration (" + "mise" + "ledger-only)", "ubuntu-latest", "00:00", "00:10"),
    ]
    out = report.summarize_run(_run(1, started="00:10", updated="12:40"), jobs)
    assert out["wall_minutes"] == 12.5  # run_started_at to updated_at, not created_at
    assert out["runner_minutes"] == pytest.approx((60 + 330 + 30 + 10) / 60, abs=1e-3)  # stored rounded to 3 places
    assert out["windows_runner_minutes"] == pytest.approx(5.5)
    assert out["job_count"] == 4
    assert out["skipped_jobs"] == 2
    assert [j["name"] for j in out["jobs"]] == [
        "Feature configuration (retired-name-only)",
        "cancelled-job",
        "lint",
        "windows-affected-tests",
    ]
    dumped = report.dumps(out)
    for private in ("someone", "private-branch", "private PR title"):
        assert private not in dumped


def test_aggregate_separates_events_and_successful_from_all_runs():
    def summary(run_id, event, conclusion, shard_seconds):
        jobs = [
            _job(f"test shard (3.12, {i})", "ubuntu-latest", "00:00", f"{s // 60:02d}:{s % 60:02d}")
            for i, s in enumerate(shard_seconds)
        ]
        return report.summarize_run(_run(run_id, event, conclusion, updated="20:00"), jobs)

    runs = [
        summary(1, "pull_request", "success", [60, 120]),
        summary(2, "pull_request", "success", [120, 180]),
        summary(3, "pull_request", "failure", [600]),
        summary(4, "merge_group", "success", [90]),
    ]
    agg = report.aggregate(runs)
    assert sorted(agg) == ["merge_group", "pull_request"]
    pr = agg["pull_request"]
    assert pr["successful"]["runs"] == 2
    assert pr["all"]["runs"] == 3
    assert pr["conclusions"] == {"failure": 1, "success": 2}
    family = pr["successful"]["job_families"]["test shard 3.12"]
    assert family["instances"] == 4  # two shards in each of two runs
    assert family["runs_with"] == 2
    assert family["ran_fraction"] == 1.0
    assert family["median_seconds"] == 120
    assert pr["all"]["job_families"]["test shard 3.12"]["max_seconds"] == 600
    assert agg["merge_group"]["successful"]["runner_minutes"]["median"] == 1.5


def test_dumps_is_deterministic_and_keeps_small_objects_on_one_line():
    value = {"b": [{"name": "x", "seconds": 3}], "a": {"n": 1, "m": 2}}
    first = report.dumps(value)
    assert first == report.dumps(json.loads(first))
    assert '"a": {"m": 2, "n": 1}' in first
    assert '{"name": "x", "seconds": 3}' in first
    assert first.index('"a"') < first.index('"b"')


def test_gh_api_retries_rate_limit_and_server_errors_once_each_then_gives_up(monkeypatch):
    replies = [
        subprocess.CompletedProcess([], 1, "", "gh: You have exceeded a secondary rate limit (HTTP 403)"),
        subprocess.CompletedProcess([], 1, "", "gh: Server Error (HTTP 502)"),
        subprocess.CompletedProcess([], 0, '{"ok": true}', ""),
    ]
    calls = []
    monkeypatch.setattr(report.subprocess, "run", lambda *a, **k: calls.append(a) or replies.pop(0))
    sleeps = []
    assert report.gh_api("repos/o/r/actions/runs", sleep=sleeps.append) == {"ok": True}
    assert sleeps == [report.RATE_LIMIT_SLEEP_SECONDS, 5]

    replies[:] = [subprocess.CompletedProcess([], 1, "", "gh: Not Found (HTTP 404)")]
    with pytest.raises(RuntimeError, match="HTTP 404"):
        report.gh_api("repos/o/r/missing", sleep=sleeps.append)
    assert len(calls) == 4  # a 404 is not retried


def test_fetch_runs_slices_the_window_dedupes_overlap_and_filters_half_open(monkeypatch):
    since = report.parse_ts("2026-11-09T00:00:00Z")
    until = since + timedelta(hours=2 * report.SLICE_HOURS)
    boundary = since + timedelta(hours=report.SLICE_HOURS)
    rows = {
        1: since,
        2: boundary,  # GitHub returns it for both adjacent inclusive slices
        3: until,  # the window is half-open, so a run created exactly at until is out
        4: until + timedelta(minutes=1),  # outside the window
    }

    def fake_api(path, **_):
        lo, hi = path.split("created=")[1].split("&")[0].split("..")
        lo, hi = report.parse_ts(lo), report.parse_ts(hi)
        runs = [
            {"id": i, "created_at": ts.strftime("%Y-%m-%dT%H:%M:%SZ")}
            for i, ts in rows.items()
            if lo <= ts <= hi or i == 4
        ]
        return {"workflow_runs": runs, "total_count": len(runs)}

    monkeypatch.setattr(report, "gh_api", fake_api)
    got = report.fetch_runs("repos/o/r", "ci.yml", since, until)
    assert [r["id"] for r in got] == [1, 2]


def test_collect_inventory_counts_tests_and_reads_the_collect_summary(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "brigade").mkdir(parents=True)
    (tmp_path / "tests" / "test_a.py").write_text("def test_one():\n    pass\n\nasync def test_two():\n    pass\n")
    (tmp_path / "tests" / "conftest.py").write_text("def test_not_counted_here():\n    pass")
    (tmp_path / "src" / "brigade" / "mod.py").write_text("x = 1\ny = 2\n")
    inv = report.collect_inventory(
        tmp_path, "tests/test_a.py::test_one\n\n19000/19341 tests collected (341 deselected) in 4.2s\n"
    )
    assert (inv["test_files"], inv["test_defs"]) == (1, 2)
    assert inv["test_loc"] == 5 + 2  # unterminated last line still counts
    assert inv["src_loc"] == 2
    assert (inv["collected"], inv["collected_total"]) == (19000, 19341)
    with pytest.raises(ValueError, match="collection summary"):
        report.parse_collect_output("ERROR collecting tests/test_a.py\n")


def test_gh_api_retries_a_dropped_stream(monkeypatch):
    replies = [
        subprocess.CompletedProcess([], 1, "", "stream error: stream ID 1; CANCEL; received from peer"),
        subprocess.CompletedProcess([], 0, "[]", ""),
    ]
    monkeypatch.setattr(report.subprocess, "run", lambda *a, **k: replies.pop(0))
    sleeps = []
    assert report.gh_api("repos/o/r/actions/runs/1/jobs", sleep=sleeps.append) == []
    assert sleeps == [5]


# --------------------------------------------------------------------------
# First-attempt measurement for re-runs
# --------------------------------------------------------------------------


def _rerun_fake_api(calls):
    """Attempt 1 failed after a 25 minute Windows job; attempt 2 re-ran only lint and finished fast."""
    windows = _job("windows-affected-tests", "windows-latest", "00:30", "25:30")
    lint_failed = _job("lint", "ubuntu-latest", "00:00", "01:00", conclusion="failure")
    lint_rerun = _job("lint", "ubuntu-latest", "40:10", "40:50")
    carried = dict(windows, id=202)  # the API repeats a carried-over job under the later attempt, new id
    first_run = _run(7, conclusion="failure", started="00:00", updated="26:00", attempt=1)

    def fake_api(path, **_):
        calls.append(path)
        if path.endswith("/actions/runs/7/attempts/1"):
            return first_run
        if "/attempts/1/jobs" in path:
            return {"total_count": 2, "jobs": [dict(windows, id=101), dict(lint_failed, id=102)]}
        if "/attempts/2/jobs" in path:
            return {"total_count": 2, "jobs": [carried, dict(lint_rerun, id=203)]}
        raise AssertionError(f"unexpected path {path}")

    return fake_api


def test_rerun_is_measured_on_its_first_attempt_not_the_latest(monkeypatch):
    calls = []
    monkeypatch.setattr(report, "gh_api", _rerun_fake_api(calls))
    listed = _run(7, conclusion="success", started="40:00", updated="41:00", attempt=2)
    warnings = report.Warnings()
    out = report.summarize_listed_run("repos/o/r", listed, warnings)
    assert out["run_attempt"] == 2 and out["reran"] is True
    assert (out["conclusion"], out["latest_conclusion"]) == ("failure", "success")
    # Wall time is attempt 1's 26 minutes, not the 1 minute from the latest attempt's own timestamps.
    assert out["wall_minutes"] == 26.0
    assert out["wall_minutes"] * 60 >= max(j["seconds"] for j in out["jobs"])
    assert out["runner_minutes"] == pytest.approx((1500 + 60) / 60)
    # Every attempt: the carried-over Windows job counts once, the lint re-run counts again.
    assert out["runner_minutes_all_attempts"] == pytest.approx((1500 + 60 + 40) / 60, abs=1e-3)
    assert warnings.items() == []
    assert any("attempts/1/jobs" in c for c in calls) and any("attempts/2/jobs" in c for c in calls)
    assert not any("filter=" in c for c in calls)


def test_latest_attempt_data_for_a_rerun_trips_the_wall_check():
    # The pre-fix shape: latest-attempt wall (1 min) next to a carried-over 25 min job.
    jobs = [_job("windows-affected-tests", "windows-latest", "00:30", "25:30")]
    listed = _run(8, conclusion="success", started="40:00", updated="41:00", attempt=2)
    broken = report.summarize_run(listed, jobs)
    assert report.wall_conflicts_with_jobs(broken)
    fixed = report.summarize_run(listed, jobs, first_attempt=_run(8, started="00:00", updated="26:00"))
    assert not report.wall_conflicts_with_jobs(fixed)


def test_a_run_that_was_not_rerun_uses_the_listed_run_and_one_jobs_call(monkeypatch):
    calls = []

    def fake_api(path, **_):
        calls.append(path)
        return {"total_count": 1, "jobs": [_job("lint", "ubuntu-latest", "00:00", "01:00")]}

    monkeypatch.setattr(report, "gh_api", fake_api)
    out = report.summarize_listed_run("repos/o/r", _run(9, started="00:00", updated="02:00"), report.Warnings())
    assert out["reran"] is False and out["runner_minutes_all_attempts"] == out["runner_minutes"] == 1.0
    assert len(calls) == 1 and "filter=latest" in calls[0]


def test_aggregate_counts_reruns_and_reports_all_attempt_cost_separately():
    first = report.summarize_run(
        _run(1, started="00:00", updated="10:00"), [_job("a", "ubuntu-latest", "00:00", "10:00")]
    )
    rerun = report.summarize_run(
        _run(2, conclusion="success", started="30:00", updated="31:00", attempt=2),
        [_job("a", "ubuntu-latest", "00:00", "10:00")],
        first_attempt=_run(2, conclusion="success", started="00:00", updated="10:00"),
        all_jobs=[_job("a", "ubuntu-latest", "00:00", "10:00"), _job("a", "ubuntu-latest", "30:10", "30:50")],
    )
    agg = report.aggregate([first, rerun])["pull_request"]
    assert agg["reruns"] == 1
    assert agg["successful"]["runner_minutes"]["total"] == 20.0
    assert agg["successful"]["runner_minutes_all_attempts"]["total"] == pytest.approx(20.67, abs=0.01)


# --------------------------------------------------------------------------
# Window and pagination
# --------------------------------------------------------------------------


def test_adjacent_windows_never_share_a_run(monkeypatch):
    t0 = report.parse_ts("2026-11-09T00:00:00Z")
    mid = t0 + timedelta(hours=1)
    end = t0 + timedelta(hours=2)
    rows = [{"id": 1, "created_at": "2026-11-09T00:30:00Z"}, {"id": 2, "created_at": "2026-11-09T01:00:00Z"}]

    def fake_api(path, **_):
        lo, hi = (report.parse_ts(x) for x in path.split("created=")[1].split("&")[0].split(".."))
        got = [r for r in rows if lo <= report.parse_ts(r["created_at"]) <= hi]
        return {"workflow_runs": got, "total_count": len(got)}

    monkeypatch.setattr(report, "gh_api", fake_api)
    left = {r["id"] for r in report.fetch_runs("repos/o/r", "ci.yml", t0, mid)}
    right = {r["id"] for r in report.fetch_runs("repos/o/r", "ci.yml", mid, end)}
    assert left == {1} and right == {2}


def _page_of(path):
    return int(re.search(r"[?&]page=(\d+)", path).group(1))


def _paged_api(key, total_count, rows):
    def fake_api(path, **_):
        page = _page_of(path)
        start = (page - 1) * report.PER_PAGE
        return {key: rows[start : start + report.PER_PAGE], "total_count": total_count}

    return fake_api


def test_run_listing_follows_a_second_page(monkeypatch):
    rows = [{"id": i, "created_at": "2026-11-09T00:30:00Z"} for i in range(103)]
    monkeypatch.setattr(report, "gh_api", _paged_api("workflow_runs", 103, rows))
    warnings = report.Warnings()
    since = report.parse_ts("2026-11-09T00:00:00Z")
    got = report.fetch_runs("repos/o/r", "ci.yml", since, since + timedelta(hours=1), warnings)
    assert len(got) == 103 and warnings.items() == []


def test_run_listing_warns_when_rows_disagree_with_total_count(monkeypatch):
    rows = [{"id": i, "created_at": "2026-11-09T00:30:00Z"} for i in range(103)]
    monkeypatch.setattr(report, "gh_api", _paged_api("workflow_runs", 105, rows))
    warnings = report.Warnings()
    since = report.parse_ts("2026-11-09T00:00:00Z")
    report.fetch_runs("repos/o/r", "ci.yml", since, since + timedelta(hours=1), warnings)
    assert len(warnings.items()) == 1 and "103 rows but total_count was 105" in warnings.items()[0]


def test_fetch_jobs_follows_a_second_page_and_warns_on_mismatch(monkeypatch):
    rows = [{"id": i, "name": f"j{i}"} for i in range(103)]
    monkeypatch.setattr(report, "gh_api", _paged_api("jobs", 103, rows))
    warnings = report.Warnings()
    assert len(report.fetch_jobs("repos/o/r", 5, warnings)) == 103
    assert warnings.items() == []
    monkeypatch.setattr(report, "gh_api", _paged_api("jobs", 110, rows))
    assert len(report.fetch_jobs("repos/o/r", 5, warnings, attempt=1)) == 103
    assert "run 5 jobs (attempt 1): 103 rows but total_count was 110" in warnings.items()


def _minute_rows(count, same_second=False):
    base = report.parse_ts("2026-11-09T00:00:00Z")
    return [
        {"id": i, "created_at": (base + timedelta(minutes=0 if same_second else i)).strftime("%Y-%m-%dT%H:%M:%SZ")}
        for i in range(count)
    ]


def _capped_api(rows):
    """Behaves like the Actions API: total_count is the true count, but only CAP rows are ever served."""

    def fake_api(path, **_):
        lo, hi = (report.parse_ts(x) for x in path.split("created=")[1].split("&")[0].split(".."))
        page = _page_of(path)
        inside = [r for r in rows if lo <= report.parse_ts(r["created_at"]) <= hi][: report.SEARCH_RESULT_CAP]
        start = (page - 1) * report.PER_PAGE
        return {"workflow_runs": inside[start : start + report.PER_PAGE], "total_count": len(inside)}

    return fake_api


def test_a_slice_at_the_row_cap_is_split_and_nothing_is_lost(monkeypatch):
    monkeypatch.setattr(report, "PER_PAGE", 2)
    monkeypatch.setattr(report, "SEARCH_RESULT_CAP", 4)
    monkeypatch.setattr(report, "gh_api", _capped_api(_minute_rows(7)))
    since = report.parse_ts("2026-11-09T00:00:00Z")
    warnings = report.Warnings()
    got = report.fetch_runs("repos/o/r", "ci.yml", since, since + timedelta(minutes=10), warnings)
    assert [r["id"] for r in got] == list(range(7))
    assert warnings.items() == []


def test_a_slice_that_cannot_be_split_fails_instead_of_returning_partial_data(monkeypatch):
    monkeypatch.setattr(report, "PER_PAGE", 2)
    monkeypatch.setattr(report, "SEARCH_RESULT_CAP", 4)
    monkeypatch.setattr(report, "gh_api", _capped_api(_minute_rows(6, same_second=True)))
    since = report.parse_ts("2026-11-09T00:00:00Z")
    with pytest.raises(RuntimeError, match="cannot be split"):
        report.fetch_runs("repos/o/r", "ci.yml", since, since + timedelta(seconds=1))


# --------------------------------------------------------------------------
# Privacy, noise and output
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("labels", "expected"),
    [
        (["ubuntu-latest"], "ubuntu-latest"),
        (["ubuntu-24.04-arm"], "ubuntu-24.04-arm"),
        (["windows-2022"], "windows-2022"),
        (["macos-14-xlarge"], "macos-14-xlarge"),
        (["self-hosted", "linux", "x64"], "self-hosted"),
        (["build-box-17", "self-hosted"], "other"),
        (["linux-build-box-17"], "linux"),
        (["WINDOWS-ci-host"], "WINDOWS-ci-host"),
        ([], None),
    ],
)
def test_runner_label_is_published_only_when_it_cannot_name_a_machine(labels, expected):
    assert report.safe_runner(labels) == expected


def test_job_names_holding_the_branch_or_actor_are_redacted():
    run = _run(1)
    run["head_branch"] = "Feat/Hub-Seven"
    run["actor"] = {"login": "Alice"}
    run["triggering_actor"] = {"login": "bob-the-builder"}
    jobs = [
        _job("deploy (feat/hub-seven)", "ubuntu-latest", "00:00", "00:10"),
        _job("notify ALICE", "ubuntu-latest", "00:00", "00:20"),
        _job("rerun by bob-the-builder", "ubuntu-latest", "00:00", "00:30"),
        _job("lint", "ubuntu-latest", "00:00", "00:40"),
    ]
    out = report.summarize_run(run, jobs)
    assert sorted(j["name"] for j in out["jobs"]) == ["lint", "redacted", "redacted", "redacted"]
    assert out["redacted_jobs"] == 3
    dumped = report.dumps(out).lower()
    for private in ("hub-seven", "alice", "bob-the-builder"):
        assert private not in dumped


def test_empty_branch_or_actor_never_redacts_everything():
    run = _run(1)
    run["head_branch"], run["actor"] = "", None
    out = report.summarize_run(run, [_job("lint", "ubuntu-latest", "00:00", "00:10")])
    assert [j["name"] for j in out["jobs"]] == ["lint"] and out["redacted_jobs"] == 0


def test_unexpanded_matrix_jobs_with_zero_seconds_are_not_counted():
    jobs = [
        _job("test (${{ matrix.python }})", "ubuntu-latest", "00:00", "00:00", conclusion="cancelled"),
        _job("mesh cross-version (${{ matrix.crypto }})", "ubuntu-latest", "00:00", "00:00"),
        _job("test (${{ matrix.python }})", "ubuntu-latest", "00:00", "00:07", conclusion="cancelled"),
        _job("lint", "ubuntu-latest", "00:00", "00:00"),  # a real zero-second job still counts
    ]
    out = report.summarize_run(_run(1), jobs)
    assert out["job_count"] == 2 and out["skipped_jobs"] == 2
    assert sorted(j["seconds"] for j in out["jobs"]) == [0, 7]


def test_markdown_escapes_pipes_in_table_cells():
    run = report.summarize_run(_run(1), [_job("build | package", "ubuntu-latest", "00:00", "01:00")])
    doc = report.build_report(
        [run], workflow="ci.yml", since="a", until="b", label="x", repo="o/r", warnings=["a warning"]
    )
    text = report.render_markdown(doc)
    assert "| build \\| package |" in text
    assert "- Warning: a warning" in text
    row = next(line for line in text.splitlines() if line.startswith("| build"))
    assert row.replace("\\|", "").count("|") == 7
