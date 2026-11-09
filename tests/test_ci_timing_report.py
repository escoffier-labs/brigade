import json
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


def test_fetch_runs_slices_the_window_dedupes_overlap_and_filters_by_created_at(monkeypatch):
    since = report.parse_ts("2026-11-09T00:00:00Z")
    until = since + timedelta(hours=2 * report.SLICE_HOURS)
    boundary = since + timedelta(hours=report.SLICE_HOURS)
    rows = {
        1: since,
        2: boundary,  # GitHub returns it for both adjacent inclusive slices
        3: until,
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
        return {"workflow_runs": runs}

    monkeypatch.setattr(report, "gh_api", fake_api)
    got = report.fetch_runs("repos/o/r", "ci.yml", since, until)
    assert [r["id"] for r in got] == [1, 2, 3]


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
