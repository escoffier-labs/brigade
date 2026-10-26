"""Retained-result and adverse evidence contracts for the non-gating shadow."""

from copy import deepcopy
import datetime as datetime_module
from datetime import datetime
from http.client import HTTPException, IncompleteRead
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import textwrap
from urllib.error import HTTPError
import urllib.request

import pytest

from scripts import pr_tier_shadow as shadow

ROOT = Path(__file__).resolve().parents[1]
SHA = "a" * 40
PR_HEAD = "b" * 40
IDENTITY = {"sha": SHA, "pr_head": PR_HEAD, "run_id": 123, "run_attempt": 2}


def prefetch_source():
    job = (ROOT / ".github/workflows/ci.yml").read_text().split("  pr-tier-shadow:\n", 1)[1]
    first = re.split(r"^      - ", job.split("    steps:\n", 1)[1], flags=re.M)[1]
    assert "GH_TOKEN: ${{ github.token }}" in first
    assert "python3 -I -S - <<'PY'" in first
    return textwrap.dedent(first.split("python3 -I -S - <<'PY'\n", 1)[1].rsplit("          PY", 1)[0])


def test_shadow_token_is_confined_to_literal_first_step():
    job = (ROOT / ".github/workflows/ci.yml").read_text().split("  pr-tier-shadow:\n", 1)[1]
    steps = re.split(r"^      - ", job.split("    steps:\n", 1)[1], flags=re.M)[1:]
    prefetch_source()
    assert "scripts/" not in steps[0] and "pip install" not in steps[0]
    assert "actions/checkout@" in steps[1]
    assert "GH_TOKEN" not in "".join(steps[1:])
    assert "GITHUB_TOKEN" not in "".join(steps[1:])
    assert '--full-evidence "$RUNNER_TEMP/pr-tier-full"' in job


@pytest.mark.parametrize(
    "problem", [None, "missing", "partial", "stale-output", "wrong-attempt", "wrong-sha", "fetch-error"]
)
def test_finalize_uses_only_explicit_cached_evidence(tmp_path, monkeypatch, evidence, problem):
    run, jobs, logs, selected = deepcopy(evidence)
    output, cache = tmp_path / "output", tmp_path / "cache"
    shadow.guard(output, now=datetime.fromisoformat("2026-10-23T00:00:00+00:00"))
    report = json.loads((output / "report.json").read_text())
    report["identity"] = IDENTITY
    shadow.write_json(output / "report.json", report)
    shadow.write_json(output / "pytest-results.json", selected)
    if problem == "wrong-attempt":
        run["run_attempt"] = 1
    if problem == "wrong-sha":
        run["head_sha"] = "c" * 40
    if problem != "missing":
        shadow.write_json(cache / "workflow-run.json", run)
        shadow.write_json(cache / "jobs-page-1.json", {"total_count": 4, "jobs": jobs})
        if problem != "partial":
            shadow.write_json(
                cache / "fetch-errors.json", {"errors": ["log fetch failed"] if problem == "fetch-error" else []}
            )
        for job_id, text in logs.items():
            (cache / f"job-{job_id}.log").write_text(text)
        if problem == "stale-output":
            shutil.copytree(cache, output / "full")
            (cache / "fetch-errors.json").unlink()

    def no_network(*args, **kwargs):
        pytest.fail("offline finalization attempted network access")

    monkeypatch.setattr(urllib.request, "build_opener", no_network)
    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    assert shadow.main(["finalize", "--output", str(output), "--full-evidence", str(cache)]) == (2 if problem else 0)
    comparison = json.loads((output / "report.json").read_text())["comparison"]
    assert comparison["full_only_candidate_count"] is (None if problem else 0)
    if problem:
        assert comparison["errors"]
    if problem == "fetch-error":
        assert "log fetch failed" in comparison["errors"]
    if problem != "missing":
        assert (output / "full/workflow-run.json").read_bytes() == (cache / "workflow-run.json").read_bytes()


@pytest.fixture
def prefetch(tmp_path, monkeypatch, evidence):
    run, jobs, logs, _ = evidence
    requests = []
    clock = [shadow.TRIAL_START]
    failure = [None, None]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]

    class Response:
        def __init__(self, data):
            self.data = data

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, size=-1):
            data, self.data = self.data[:size], self.data[size:]
            return data

    class Opener:
        def open(self, request, *, timeout):
            assert 0 < timeout <= 30
            requests.append(request)
            url = request.full_url
            stage = "log" if url.endswith("/logs") else "metadata"
            if failure[0] and failure[1] in (None, stage):
                raise failure[0]
            if url.startswith("https://logs.example/"):
                assert not request.has_header("Authorization")
                return Response(b"\xef\xbb\xbf" + logs[int(url.rsplit("/", 1)[1])].encode())
            assert request.get_header("Authorization") == "Bearer fixture-token"
            if url.endswith("/logs"):
                job_id = url.split("/")[-2]
                raise HTTPError(url, 302, "Found", {"Location": f"https://logs.example/{job_id}"}, None)
            if "/jobs?" in url:
                all_jobs = [{"id": 1000 + i, "name": f"other job {i}"} for i in range(120)] + jobs
                page = int(url.rsplit("=", 1)[1])
                return Response(
                    json.dumps({"total_count": len(all_jobs), "jobs": all_jobs[(page - 1) * 100 : page * 100]}).encode()
                )
            assert url.endswith("/runs/123/attempts/2")
            return Response(json.dumps(run).encode())

    def opener(handler):
        request = urllib.request.Request("https://api.github.com/fixture")
        assert handler.redirect_request(request, None, 302, "Found", {}, "https://logs.example/500") is None
        return Opener()

    monkeypatch.setattr(datetime_module, "datetime", Clock)
    monkeypatch.setattr(urllib.request, "build_opener", opener)
    monkeypatch.setenv("GH_TOKEN", "fixture-token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/project")
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("FULL_SHARDS_RESULT", "success")

    def execute():
        namespace = {"__name__": "__main__"}
        exec(compile(prefetch_source(), "workflow-prefetch", "exec"), namespace)
        return namespace

    return execute, requests, clock, failure, tmp_path / "pr-tier-full"


@pytest.mark.parametrize("problem", [None, "http-error", "missing-shard"])
def test_literal_prefetch_to_offline_finalize_with_two_pages_and_bom(
    tmp_path, monkeypatch, prefetch, evidence, problem
):
    execute, requests, _, failure, cache = prefetch
    _, jobs, _, selected = evidence
    if problem == "missing-shard":
        jobs.pop()
    elif problem == "http-error":
        failure[:] = [
            HTTPError(
                "https://api.github.com/fixture", 403, "Forbidden", {"Location": "https://signed.example/secret"}, None
            ),
            "log",
        ]
    namespace = execute()
    assert namespace["TRIAL_START"] == shadow.TRIAL_START
    assert namespace["TRIAL_EXPIRY"] == shadow.TRIAL_EXPIRY
    assert [request.full_url for request in requests if "/jobs?" in request.full_url] == [
        "https://api.github.com/repos/example/project/actions/runs/123/attempts/2/jobs?per_page=100&page=1",
        "https://api.github.com/repos/example/project/actions/runs/123/attempts/2/jobs?per_page=100&page=2",
    ]
    if problem is None:
        assert len(requests) == 11  # Metadata, two job pages, four API/signed-host log pairs.
        assert json.loads((cache / "fetch-errors.json").read_text()) == {"errors": []}
        assert len(list(cache.glob("job-*.log"))) == 4
        assert (cache / "job-500.log").read_bytes().startswith(b"\xef\xbb\xbf")
    assert json.loads((cache / "workflow-run.json").read_text())["run_attempt"] == 2
    assert json.loads((cache / "jobs-page-2.json").read_text())["total_count"] == (
        123 if problem == "missing-shard" else 124
    )
    assert "fixture-token" not in "".join(path.read_text() for path in cache.iterdir())
    assert "signed.example" not in (cache / "fetch-errors.json").read_text()
    if problem == "http-error":
        assert not any(request.full_url.startswith("https://logs.example/") for request in requests)

    def no_network(*args, **kwargs):
        pytest.fail("offline finalization attempted network access")

    monkeypatch.setattr(urllib.request, "build_opener", no_network)
    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    output = tmp_path / "output"
    report = shadow.guard(output, now=shadow.TRIAL_START)
    report["identity"] = IDENTITY
    shadow.write_json(output / "report.json", report)
    shadow.write_json(output / "pytest-results.json", selected)
    assert shadow.finalize(output, cache) == (2 if problem else 0)
    comparison = json.loads((output / "report.json").read_text())["comparison"]
    assert comparison["status"] == ("incomplete" if problem else "complete")
    assert comparison["full_only_candidate_count"] == (None if problem else 0)
    if problem:
        assert comparison["errors"]
    assert {path.name: path.read_bytes() for path in (output / "full").iterdir()} == {
        path.name: path.read_bytes() for path in cache.iterdir()
    }


@pytest.mark.parametrize("reason", ["before", "expired", "cancelled", "invalid-repo", "invalid-run", "invalid-attempt"])
def test_literal_prefetch_does_no_api_work_when_disallowed(prefetch, monkeypatch, reason):
    execute, requests, clock, _, cache = prefetch
    if reason == "before":
        clock[0] = shadow.TRIAL_START - datetime_module.timedelta(seconds=1)
    elif reason == "expired":
        clock[0] = shadow.TRIAL_EXPIRY
    elif reason == "cancelled":
        monkeypatch.setenv("FULL_SHARDS_RESULT", "cancelled")
    else:
        key = {
            "invalid-repo": "GITHUB_REPOSITORY",
            "invalid-run": "GITHUB_RUN_ID",
            "invalid-attempt": "GITHUB_RUN_ATTEMPT",
        }[reason]
        monkeypatch.setenv(key, "../bad")
    execute()
    assert requests == []
    assert json.loads((cache / "fetch-errors.json").read_text())["errors"]


def test_literal_prefetch_failure_continues_without_leaking_exception_urls(prefetch):
    execute, _, _, failure, cache = prefetch
    failure[0] = OSError("https://signed.example/?token=secret")
    execute()
    errors = (cache / "fetch-errors.json").read_text()
    assert "OSError" in errors
    assert "secret" not in errors and "https://" not in errors


@pytest.mark.parametrize("surface", ["finalize", "workflow"])
@pytest.mark.parametrize("existing", ["directory", "outside-symlink", "cache-symlink"])
def test_retained_full_replaces_old_artifact_without_following_symlinks(
    tmp_path, monkeypatch, prefetch, surface, existing
):
    execute, _, _, _, cache = prefetch
    execute()
    output = tmp_path / ".pr-tier-shadow"
    shadow.guard(output, now=shadow.TRIAL_START)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("untouched")
    full = output / "full"
    if existing == "directory":
        full.mkdir()
        (full / "stale.log").write_text("PR-committed artifact")
        (full / "job-500.log").symlink_to(sentinel)
    else:
        full.symlink_to(cache if existing == "cache-symlink" else outside, target_is_directory=True)
    expected = {path.name: path.read_bytes() for path in cache.iterdir()}
    if surface == "workflow":
        bash = shutil.which("bash")
        if bash is None:
            pytest.skip("Ubuntu shadow workflow retention requires Bash")
        monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "step-output"))
        job = (ROOT / ".github/workflows/ci.yml").read_text().split("  pr-tier-shadow:\n", 1)[1]
        script = job.split("      - name: Compare exact attempt and write summary\n", 1)[1]
        script = textwrap.dedent(script.split("        run: |\n", 1)[1].split("          if [ -f scripts/", 1)[0])
        result = subprocess.run([bash, "-e", "-c", script], cwd=tmp_path, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
    else:
        assert shadow.finalize(output, cache) == 2  # No selected run was supplied.
    assert not full.is_symlink()
    assert {path.name: path.read_bytes() for path in full.iterdir()} == expected
    assert sentinel.read_text() == "untouched"
    assert list(outside.iterdir()) == [sentinel]
    assert {path.name: path.read_bytes() for path in cache.iterdir()} == expected


@pytest.mark.parametrize(
    "scenario",
    [
        "output-symlink",
        "unlink-failure",
        "nested-file",
        "nested-directory",
        "deeper-link",
        "dangling-link",
        "root-after-finalize",
        "normal-complete",
        "normal-incomplete",
    ],
)
def test_workflow_upload_uses_safe_directory_even_when_comparison_is_incomplete(
    tmp_path, monkeypatch, prefetch, evidence, scenario
):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("Ubuntu shadow workflow comparison requires Bash")
    execute, _, _, _, cache = prefetch
    execute()
    expected_cache = {path.name: path.read_bytes() for path in cache.iterdir()}
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "private.txt"
    sentinel.write_text("must not be uploaded")
    output = tmp_path / ".pr-tier-shadow"
    if scenario in {"output-symlink", "unlink-failure"}:
        output.symlink_to(outside, target_is_directory=True)
    else:
        report = shadow.guard(output, now=shadow.TRIAL_START)
        if scenario == "normal-complete":
            report["identity"] = IDENTITY
            shadow.write_json(output / "report.json", report)
            shadow.write_json(output / "pytest-results.json", evidence[3])
        if scenario == "nested-file":
            (output / "leak").symlink_to(sentinel)
        elif scenario == "nested-directory":
            (output / "leak").symlink_to(outside, target_is_directory=True)
        elif scenario == "deeper-link":
            nested = output / "nested/deep"
            nested.mkdir(parents=True)
            (nested / "leak").symlink_to(outside, target_is_directory=True)
        elif scenario == "dangling-link":
            (output / "dangling").symlink_to(outside / "missing")
    (tmp_path / "scripts").symlink_to(ROOT / "scripts", target_is_directory=True)
    step_output = tmp_path / "step-output"
    summary = tmp_path / "summary"
    monkeypatch.setenv("GITHUB_OUTPUT", str(step_output))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.delenv("GH_TOKEN", raising=False)
    job = (ROOT / ".github/workflows/ci.yml").read_text().split("  pr-tier-shadow:\n", 1)[1]
    comparison, upload = job.split("      - name: Compare exact attempt and write summary\n", 1)[1].split(
        "      - name: Retain shadow evidence\n", 1
    )
    validation = None
    if "      - name: Validate shadow artifact tree\n" in comparison:
        comparison, validation = comparison.split("      - name: Validate shadow artifact tree\n", 1)
    script = textwrap.dedent(comparison.split("        run: |\n", 1)[1])
    if scenario == "unlink-failure":
        # A function intercepts the command in this Bash process on Windows too.
        script = "rm() { printf '%s\n' 'forced unlink failure' >&2; return 1; }\n" + script
    result = subprocess.run([bash, "-e", "-c", script], cwd=tmp_path, capture_output=True, text=True, timeout=30)
    values = dict(line.split("=", 1) for line in step_output.read_text().splitlines()) if step_output.exists() else {}
    safety_values = {}

    def condition(section):
        expression = re.search(r"^        if: (.+)$", section, re.M)[1]
        expression = (
            expression.replace("always()", "True")
            .replace("steps.comparison.outputs.upload_safe", repr(values.get("upload_safe", "")))
            .replace("steps.artifact_safety.outputs.upload_safe", repr(safety_values.get("upload_safe", "")))
            .replace("&&", "and")
        )
        return eval(expression, {"__builtins__": {}}, {})

    if scenario == "root-after-finalize":
        shutil.rmtree(output)
        output.symlink_to(outside, target_is_directory=True)
    if validation is not None and condition(validation):
        assert "env:" not in validation
        source = textwrap.dedent(validation.split("python3 -I -S - <<'PY'\n", 1)[1].rsplit("          PY", 1)[0])
        safety_output = tmp_path / "safety-output"
        monkeypatch.setenv("GITHUB_OUTPUT", str(safety_output))
        safety = subprocess.run(
            [sys.executable, "-I", "-S", "-"], input=source, cwd=tmp_path, capture_output=True, text=True, timeout=30
        )
        if safety_output.exists():
            safety_values = dict(line.split("=", 1) for line in safety_output.read_text().splitlines())
        assert safety.returncode == (0 if scenario in {"output-symlink", "normal-complete", "normal-incomplete"} else 2)
    eligible = condition(upload)
    assert sentinel.read_text() == "must not be uploaded"
    assert list(outside.iterdir()) == [sentinel]
    assert {path.name: path.read_bytes() for path in cache.iterdir()} == expected_cache
    if scenario == "unlink-failure":
        assert "forced unlink failure" in result.stderr
        assert result.returncode == 1
        assert output.is_symlink()
        assert eligible is False
    elif scenario in {"nested-file", "nested-directory", "deeper-link", "dangling-link", "root-after-finalize"}:
        assert result.returncode == 2, result.stderr
        assert eligible is False
    else:
        assert result.returncode == (0 if scenario == "normal-complete" else 2), result.stderr
        assert output.is_dir() and not output.is_symlink()
        assert eligible is True  # Complete and incomplete comparisons still upload.
        assert "path: .pr-tier-shadow/" in upload
        assert not (output / "private.txt").exists()
        report = json.loads((output / "report.json").read_text())
        assert report["comparison"]["status"] == ("complete" if scenario == "normal-complete" else "incomplete")
        assert report["comparison"]["full_only_candidate_count"] == (0 if scenario == "normal-complete" else None)
        assert {path.name: path.read_bytes() for path in (output / "full").iterdir()} == expected_cache
        if scenario == "output-symlink":
            assert any("symlink" in error for error in report["comparison"]["errors"])


def log(index, *, failed=None, checkout=SHA):
    failures = failed or []
    lines = [
        "2026-10-22T01:00:00Z [command]/usr/bin/git log -1 --format=%H",
        f"2026-10-22T01:00:00Z {checkout}",
        '##[group]Run python scripts/ci_pytest_shard.py --shard-index "0" --shard-count 4 -- -q',
        "##[endgroup]",
        f"2026-10-22T01:00:01Z CI shard {index + 1}/4: selected 2 of 8 tests",
    ]
    if failures:
        lines.append("================ short test summary info ================")
    lines += [f"2026-10-22T01:00:02Z FAILED {nodeid} - AssertionError" for nodeid in failures]
    counts = f"{len(failures)} failed, {2 - len(failures)} passed" if failures else "2 passed"
    lines.append(f"2026-10-22T01:00:03Z {counts}, 6 deselected in 10.00s")
    # A separate real pytest invocation in shard 0 must not replace the shard summary.
    lines += ["##[group]Run output=$(python -m pytest tests/test_grokbot_mcp.py -q 2>&1)", "##[endgroup]"]
    lines.append("2026-10-22T01:00:04Z 4 passed in 1.00s")
    lines.append("##[group]Run actions/upload-artifact@v7")
    return "\n".join(lines)


@pytest.fixture
def evidence():
    run = {"id": 123, "run_attempt": 2, "head_sha": PR_HEAD, "event": "pull_request"}
    jobs = [
        {
            "id": 500 + i,
            "name": f"test shard (3.12, {i})",
            "run_id": 123,
            "run_attempt": 2,
            "head_sha": PR_HEAD,
            "status": "completed",
            "conclusion": "success",
            "started_at": "2026-10-22T01:00:00Z",
            "completed_at": "2026-10-22T01:01:00Z",
        }
        for i in range(4)
    ]
    logs = {job["id"]: log(i) for i, job in enumerate(jobs)}
    selected = {
        "complete": True,
        "exit_code": 0,
        "failed_nodeids": [],
        "wall_seconds": 2.5,
        "test_duration_seconds": 1.0,
    }
    return run, jobs, logs, selected


@pytest.mark.parametrize("summary_suffix", ["", ", 13 subtests passed"])
def test_full_only_failures_stay_unconfirmed_and_timing_units_are_separate(evidence, summary_suffix):
    run, jobs, logs, selected = evidence
    jobs[1]["conclusion"] = "failure"
    logs[501] = log(1, failed=["tests/test_c.py::test_missed[param value]"])
    logs = {job_id: text.replace(" in ", summary_suffix + " in ") for job_id, text in logs.items()}
    result = shadow.compare(IDENTITY, run, jobs, logs, selected)
    assert result["status"] == "complete"
    assert result["full_only_candidates"] == ["tests/test_c.py::test_missed[param value]"]
    assert result["full_only_candidate_count"] == 1
    assert result["confirmed_real_miss_count"] is None
    assert result["timing"]["full_pytest_elapsed_seconds_sum"] == 40.0
    assert result["timing"]["full_job_wall_seconds_sum"] == 240.0
    assert result["timing"]["full_parallel_wall_seconds"] == 60.0
    assert result["timing"]["full_test_duration_seconds"] is None
    assert result["timing"]["selected_pytest_wall_seconds"] == 2.5
    assert result["timing"]["selected_test_duration_seconds"] == 1.0


@pytest.mark.parametrize(
    "problem",
    [
        "missing-job",
        "duplicate-job",
        "cancelled",
        "timed_out",
        "skipped",
        "missing-log",
        "api-error",
        "no-summary",
        "collection-error",
        "unattributed-failure",
        "truncated-log",
        "wrong-shard",
        "wrong-checkout",
        "wrong-attempt",
        "wrong-sha",
        "wrong-job-attempt",
        "wrong-job-sha",
        "wrong-run",
        "shadow-incomplete",
    ],
)
def test_incomplete_evidence_never_reports_zero_candidates(evidence, problem):
    run, jobs, logs, selected = deepcopy(evidence)
    errors = []
    if problem == "missing-job":
        jobs.pop()
    elif problem == "duplicate-job":
        jobs.append(dict(jobs[0], id=999))
    elif problem in {"cancelled", "timed_out", "skipped"}:
        jobs[0]["conclusion"] = problem
    elif problem == "missing-log":
        logs.pop(500)
    elif problem == "api-error":
        errors.append("HTTP 403 reading exact attempt jobs")
    elif problem == "no-summary":
        logs[500] = "setup failed"
    elif problem == "collection-error":
        logs[500] = log(0).replace("2 passed, 6 deselected", "ERROR collecting tests/test_broken.py\n1 error")
    elif problem == "unattributed-failure":
        logs[500] = log(0).replace("2 passed", "1 failed, 1 passed")
    elif problem == "truncated-log":
        logs[500] = log(0).split("2 passed")[0]
    elif problem == "wrong-shard":
        logs[500] = log(1)
    elif problem == "wrong-checkout":
        logs[500] = log(0, checkout="c" * 40)
    elif problem == "wrong-attempt":
        run["run_attempt"] = 1
    elif problem == "wrong-sha":
        run["head_sha"] = "c" * 40
    elif problem == "wrong-job-attempt":
        jobs[0]["run_attempt"] = 1
    elif problem == "wrong-job-sha":
        jobs[0]["head_sha"] = "c" * 40
    elif problem == "wrong-run":
        jobs[0]["run_id"] = 124
    elif problem == "shadow-incomplete":
        selected["complete"] = False
    result = shadow.compare(IDENTITY, run, jobs, logs, selected, errors=errors)
    assert result["status"] == "incomplete"
    assert result["full_only_candidate_count"] is None
    assert result["confirmed_real_miss_count"] is None
    assert result["errors"]


def test_complete_comparison_subtracts_observed_shadow_failures(evidence):
    run, jobs, logs, selected = evidence
    nodeid = "tests/test_a.py::test_same"
    logs[500] = log(0, failed=[nodeid])
    jobs[0]["conclusion"] = "failure"
    selected.update(failed_nodeids=[nodeid], exit_code=1)
    result = shadow.compare(IDENTITY, run, jobs, logs, selected)
    assert result["status"] == "complete"
    assert result["full_only_candidate_count"] == 0
    assert result["confirmed_real_miss_count"] is None


@pytest.mark.parametrize(
    "instant,status",
    [
        ("2026-10-22T23:59:59+00:00", "not-started"),
        ("2026-10-23T00:00:00+00:00", "active"),
        ("2026-11-05T23:59:59+00:00", "active"),
        ("2026-11-06T00:00:00+00:00", "expired"),
    ],
)
def test_trial_boundaries_are_utc_and_expiry_retains_report(tmp_path, monkeypatch, instant, status):
    summary = tmp_path / "summary.md"
    outputs = tmp_path / "outputs"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("GITHUB_OUTPUT", str(outputs))
    report = shadow.guard(tmp_path / "evidence", now=datetime.fromisoformat(instant))
    assert report["trial_status"] == status
    assert json.loads((tmp_path / "evidence/report.json").read_text())["trial_status"] == status
    assert f"active={'true' if status == 'active' else 'false'}" in outputs.read_text()
    assert not summary.exists()  # The always-run finalizer publishes the single job summary.
    shadow.finalize(tmp_path / "evidence")
    assert status in summary.read_text()
    assert not (tmp_path / "evidence/pytest-results.json").exists()


@pytest.mark.parametrize("broken", [False, True])
def test_real_pytest_run_retains_outcomes_and_durations(tmp_path, broken):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_tiny.py").write_text(
        "def test_ok():\n    assert True\n\ndef test_failure():\n    assert False\n"
        if not broken
        else "this is not valid Python !!!\n"
    )
    output = tmp_path / "evidence"
    result = shadow.run_pytest(tmp_path, ["tests/test_tiny.py"], output)
    assert json.loads((output / "pytest-results.json").read_text()) == result
    assert (output / "pytest.log").is_file()
    assert result["wall_seconds"] > 0
    if broken:
        assert result["complete"] is False
        assert result["collection_errors"]
    else:
        assert result["complete"] is True
        assert result["exit_code"] == 1
        assert result["failed_nodeids"] == ["tests/test_tiny.py::test_failure"]
        assert result["test_duration_seconds"] >= 0
        assert {item["nodeid"] for item in result["outcomes"]} == {
            "tests/test_tiny.py::test_ok",
            "tests/test_tiny.py::test_failure",
        }


def test_expired_cli_does_no_git_or_pytest_or_api_work(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/pr_tier_shadow.py"),
            "run",
            "--repo",
            str(tmp_path),
            "--output",
            str(tmp_path / "evidence"),
            "--now",
            "2026-11-06T00:00:00Z",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads((tmp_path / "evidence/report.json").read_text())
    assert report["trial_status"] == "expired"
    assert report["comparison"]["full_only_candidate_count"] is None
    assert not (tmp_path / "evidence/pytest.log").exists()


def test_merge_identity_validates_second_parent_and_uses_current_first_parent(
    tmp_path, monkeypatch, evidence, prefetch
):
    summary = tmp_path / "step-summary"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))

    def git(*args):
        return subprocess.check_output(["git", "-C", str(tmp_path), *args], text=True).strip()

    git("init", "-q", "-b", "main")
    git("config", "user.email", "contributor@example.com")
    git("config", "user.name", "Contributor")
    (tmp_path / "base").write_text("base")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_cli_help.py").write_text("def test_ok():\n    assert True\n")
    git("add", ".")
    git("commit", "-qm", "base")
    git("switch", "-qc", "feature")
    (tmp_path / "feature").write_text("feature")
    git("add", ".")
    git("commit", "-qm", "feature")
    head = git("rev-parse", "HEAD")
    git("switch", "-q", "main")
    (tmp_path / "other").write_text("other")
    git("add", ".")
    git("commit", "-qm", "base advanced")
    base = git("rev-parse", "HEAD")
    git("merge", "-q", "--no-ff", "--no-edit", "feature")
    merge = git("rev-parse", "HEAD")
    monkeypatch.setenv("GITHUB_SHA", merge)
    monkeypatch.setenv("PR_HEAD_SHA", head)
    assert shadow.merge_identity(tmp_path) == {"sha": merge, "base": base, "pr_head": head, "changed": ["feature"]}
    monkeypatch.setenv("PR_HEAD_SHA", base)
    with pytest.raises(ValueError, match="second parent"):
        shadow.merge_identity(tmp_path)
    monkeypatch.setenv("PR_HEAD_SHA", head)
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/project")
    output = tmp_path / "evidence"
    cli = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/pr_tier_shadow.py"),
            "run",
            "--repo",
            str(tmp_path),
            "--output",
            str(output),
            "--now",
            "2026-10-23T00:00:00Z",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert cli.returncode == 0, cli.stderr
    local = json.loads((output / "report.json").read_text())
    assert local["identity"]["base"] == base
    assert local["selected_pytest"]["collected"] == 1
    assert local["selected_pytest"]["wall_seconds"] > 0
    assert (output / "selected.txt").read_text() == "tests/test_cli_help.py\n"
    run, jobs, logs, _ = evidence
    run["head_sha"] = head
    for job in jobs:
        job["head_sha"] = head

    for job_id in logs:
        logs[job_id] = log(job_id - 500, checkout=merge)
    execute, _, _, _, cache = prefetch
    execute()
    assert shadow.finalize(output, cache) == 0
    report = json.loads((output / "report.json").read_text())
    assert report["identity"]["sha"] == merge
    assert report["comparison"]["status"] == "complete"
    assert report["comparison"]["full_only_candidate_count"] == 0
    assert report["comparison"]["confirmed_real_miss_count"] is None
    assert merge in (output / "summary.md").read_text()
    assert summary.read_text().count("# PR test tier shadow\n") == 1
    assert "Comparison: complete" in summary.read_text()


@pytest.mark.parametrize("full_result", ["success", "failure"])
@pytest.mark.parametrize(
    "event_name,repository,head_repository,expected",
    [
        ("pull_request", "example/project", "example/project", True),
        ("pull_request", "example/project", "contributor/project", False),
        ("pull_request", "example/project", None, False),
        ("pull_request", "example/project", "", False),
        ("pull_request", None, None, False),
        ("pull_request", None, "example/project", False),
        ("push", "example/project", "example/project", False),
        ("merge_group", "example/project", "example/project", False),
    ],
)
def test_shadow_job_admission_requires_same_repository_pr(
    event_name, repository, head_repository, expected, full_result
):
    text = (ROOT / ".github/workflows/ci.yml").read_text()
    header = text.split("  pr-tier-shadow:\n", 1)[1].split("    steps:\n", 1)[0]
    expression = re.search(r"^    if: \$\{\{ (.+) \}\}$", header, re.M)[1]
    github = {"event_name": event_name, "event": {}}
    if repository is not None:
        github["repository"] = repository
    if head_repository is not None:
        github["event"] = {"pull_request": {"head": {"repo": {"full_name": head_repository}}}}

    def lookup(match):
        value = github
        for component in match[0].split(".")[1:]:
            value = value.get(component, "") if isinstance(value, dict) else ""
        return repr(value)

    # Evaluate the literal job expression, with absent Actions properties empty.
    expression = re.sub(r"\bgithub\.[A-Za-z_][A-Za-z0-9_.]*", lookup, expression)
    expression = expression.replace("needs.changes.result", repr("success"))
    expression = expression.replace("needs.changes.outputs.docs_only", repr("false"))
    expression = expression.replace("!cancelled()", "not cancelled()")
    expression = expression.replace("&&", "and").replace("||", "or")
    functions = {
        "always": lambda: True,
        "success": lambda: full_result == "success",
        "failure": lambda: full_result == "failure",
        "cancelled": lambda: False,
    }
    assert bool(eval(expression, {"__builtins__": {}}, functions)) is expected


def test_shadow_job_is_read_only_and_outside_required_dag():
    text = (ROOT / ".github/workflows/ci.yml").read_text()
    match = re.search(r"^  pr-tier-shadow:\n(.*?)(?=^  [a-z][a-z0-9-]*:|\Z)", text, re.M | re.S)
    assert match, "missing non-gating shadow job"
    job = match.group()
    assert "needs: [changes, test-shards]" in job
    assert "!cancelled() && needs.changes.result == 'success'" in job
    assert "github.event_name == 'pull_request'" in job
    assert "continue-on-error: true" in job
    assert "contents: read" in job and "actions: read" in job
    assert "persist-credentials: false" in job
    assert "fetch-depth: 2" in job and "ref: ${{ github.sha }}" in job
    assert "actions/checkout@v7" in job and "actions/setup-python@v7" in job
    assert "actions/upload-artifact@v7" in job
    assert "retention-days: 30" in job
    assert job.count("if: always()") >= 2
    assert "steps.trial.outputs.active == 'true'" in job
    assert "python scripts/pr_tier_shadow.py run" in job
    assert "python3 scripts/pr_tier_shadow.py finalize" in job
    remainder = text.replace(job, "")
    assert "pr-tier-shadow" not in remainder
    assert "pull_request_target" not in text


def test_full_log_collects_auxiliary_failure_without_replacing_shard_elapsed(evidence):
    run, jobs, logs, selected = evidence
    jobs[0]["conclusion"] = "failure"
    logs[500] = log(0).rsplit("4 passed", 1)[0] + (
        "================ short test summary info ================\n"
        "FAILED tests/test_grokbot_mcp.py::test_auxiliary - AssertionError\n1 failed, 3 passed in 1.00s\n"
    )
    result = shadow.compare(IDENTITY, run, jobs, logs, selected)
    assert result["status"] == "complete"
    assert result["full_only_candidates"] == ["tests/test_grokbot_mcp.py::test_auxiliary"]
    assert result["timing"]["full_pytest_elapsed_seconds_sum"] == 40.0
    assert result["full_shards"][0]["auxiliary_pytest_elapsed_seconds"] == [1.0]


@pytest.mark.parametrize(
    "suffix",
    [
        "ERROR collecting tests/test_auxiliary.py\n1 error in 1.00s\n",
        "FAILED tests/test_auxiliary.py::test_truncated - AssertionError\n",
    ],
)
def test_auxiliary_unparseable_or_incomplete_pytest_is_an_evidence_gap(evidence, suffix):
    run, jobs, logs, selected = evidence
    jobs[0]["conclusion"] = "failure"
    logs[500] += "\n" + suffix
    result = shadow.compare(IDENTITY, run, jobs, logs, selected)
    assert result["status"] == "incomplete"
    assert result["full_only_candidate_count"] is None


def test_failed_job_without_attributable_test_outcome_is_incomplete(evidence):
    run, jobs, logs, selected = evidence
    jobs[0]["conclusion"] = "failure"
    result = shadow.compare(IDENTITY, run, jobs, logs, selected)
    assert result["status"] == "incomplete"
    assert result["full_only_candidate_count"] is None


def test_nodeid_parameter_separator_is_not_truncated(evidence):
    run, jobs, logs, selected = evidence
    jobs[0]["conclusion"] = "failure"
    nodeid = "tests/test_a.py::test_value[left - right]"
    logs[500] = log(0, failed=[nodeid])
    assert shadow.compare(IDENTITY, run, jobs, logs, selected)["full_only_candidates"] == [nodeid]


def test_empty_selection_does_not_fall_through_to_full_pytest_discovery(tmp_path):
    (tmp_path / "test_unselected.py").write_text("raise RuntimeError('must not collect')\n")
    output = tmp_path / "evidence"
    result = shadow.run_pytest(tmp_path, [], output)
    assert result["complete"] is False
    assert result["exit_code"] is None
    assert "empty selection" in result["error"]
    assert "RuntimeError" not in (output / "pytest.log").read_text()


def test_failed_pytest_child_cannot_reuse_stale_success(tmp_path, monkeypatch):
    output = tmp_path / "evidence"
    output.mkdir()
    (output / "pytest-results.json").write_text(json.dumps({"complete": True, "exit_code": 1}))
    monkeypatch.setattr(shadow.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess([], 1))
    result = shadow.run_pytest(tmp_path, ["tests/test_a.py"], output)
    assert result["complete"] is False
    assert result["failed_nodeids"] == []


def test_finalize_retains_readable_exact_identity_and_missing_install_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/project")
    output = tmp_path / "evidence"
    shadow.guard(output, now=datetime.fromisoformat("2026-10-23T00:00:00+00:00"))
    assert shadow.finalize(output) == 2
    report = json.loads((output / "report.json").read_text())
    assert report["comparison"]["status"] == "incomplete"
    assert report["comparison"]["full_only_candidate_count"] is None
    assert "unknown" in (output / "summary.md").read_text()


@pytest.mark.parametrize("exit_code,nodeids", [(1, []), (0, ["tests/test_a.py::test_failed"])])
def test_inconsistent_selected_outcomes_are_incomplete(evidence, exit_code, nodeids):
    run, jobs, logs, selected = evidence
    selected.update(exit_code=exit_code, failed_nodeids=nodeids)
    result = shadow.compare(IDENTITY, run, jobs, logs, selected)
    assert result["status"] == "incomplete"
    assert result["full_only_candidate_count"] is None


def test_traceback_text_ending_in_seconds_is_not_a_terminal_summary(evidence):
    run, jobs, logs, selected = evidence
    jobs[0]["conclusion"] = "failure"
    logs[500] = log(0, failed=["tests/test_a.py::test_failure"]).replace(
        "FAILED tests/", "E   Failed: operation did not finish in 1.00s\nFAILED tests/", 1
    )
    result = shadow.compare(IDENTITY, run, jobs, logs, selected)
    assert result["status"] == "complete"
    assert result["full_only_candidate_count"] == 1


def test_successful_shard_zero_requires_its_auxiliary_pytest_summary(evidence):
    run, jobs, logs, selected = evidence
    logs[500] = log(0).split("##[group]Run output=", 1)[0]
    result = shadow.compare(IDENTITY, run, jobs, logs, selected)
    assert result["status"] == "incomplete"
    assert result["full_only_candidate_count"] is None


def test_worker_uses_full_shard_coverage_settings_in_actual_child(tmp_path):
    output = tmp_path / "evidence"
    (tmp_path / "test_settings.py").write_text(
        "import os\n"
        "assert os.environ['COVERAGE_CORE'] == 'sysmon'\n"
        f"assert os.environ['COVERAGE_FILE'] == {str(output / '.coverage')!r}\n"
        "def test_settings(pytestconfig):\n"
        "    assert pytestconfig.getoption('cov_source') == ['brigade']\n"
        "    assert pytestconfig.getoption('cov_report') == {'': None}\n"
        "    assert pytestconfig.getoption('cov_fail_under') == 0\n"
    )
    result = shadow.run_pytest(tmp_path, ["test_settings.py"], output)
    assert result["complete"] is True, (output / "pytest.log").read_text()
    assert result["exit_code"] == 0, (output / "pytest.log").read_text()
    assert (output / ".coverage").is_file()


def test_actual_subtest_durations_are_not_added_twice_and_failures_are_retained(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_sub.py").write_text(
        "import time\n"
        "def test_sub(subtests):\n"
        "    for i in range(2):\n"
        "        with subtests.test(i=i):\n"
        "            time.sleep(0.02)\n"
        "            assert i < 0\n"
        "def test_log():\n"
        "    import logging\n"
        "    logging.error('ERROR brigade.worker: expected failure')\n"
        "    print('FAILED tests/fake.py::test_fake - fake')\n"
        "    print('2 passed in 0.01s')\n"
        "    assert False\n"
    )
    (tmp_path / "conftest.py").write_text(
        "import json, pytest\n"
        "from pathlib import Path\n"
        "rows = []\n"
        "def pytest_runtest_logreport(report):\n"
        "    rows.append([isinstance(report, pytest.SubtestReport), report.duration])\n"
        "def pytest_sessionfinish(session, exitstatus):\n"
        "    Path('durations.json').write_text(json.dumps(rows))\n"
    )
    output = tmp_path / "evidence"
    result = shadow.run_pytest(tmp_path, ["tests/test_sub.py"], output)
    assert result["complete"] is True
    assert result["failed_nodeids"] == ["tests/test_sub.py::test_log", "tests/test_sub.py::test_sub"]
    rows = json.loads((tmp_path / "durations.json").read_text())
    assert sum(child for child, _ in rows) == 2
    assert result["test_duration_seconds"] == pytest.approx(sum(duration for child, duration in rows if not child))
    text = log(1).split("CI shard", 1)[0] + "CI shard 2/4: selected 2 of 2 tests\n"
    parsed = shadow.parse_full_log(text + (output / "pytest.log").read_text(), 1, SHA)
    assert parsed["counts"]["failed"] == 4
    assert parsed["failed_nodeids"] == result["failed_nodeids"]


@pytest.mark.parametrize(
    "captured",
    [
        "ERROR brigade.worker: expected failure",
        "FAILED tests/fake.py::test_fake - fake",
        "3 passed in 0.01s",
        "ERROR collecting tests/fake.py",
        "INTERNALERROR fake",
        "================ short test summary info ================\nFAILED tests/fake.py::test_fake - fake\n1 failed, 1 passed in 0.01s",
    ],
)
def test_captured_output_does_not_poison_real_short_summary(evidence, captured):
    run, jobs, logs, selected = evidence
    jobs[1]["conclusion"] = "failure"
    logs[501] = log(1, failed=["tests/test_a.py::test_failure"]).replace(
        "FAILED tests/test_a.py",
        f"---------------- Captured stdout call ----------------\n{captured}\n"
        "================ short test summary info ================\nFAILED tests/test_a.py",
        1,
    )
    result = shadow.compare(IDENTITY, run, jobs, logs, selected)
    assert result["status"] == "complete", result["errors"]
    assert result["full_only_candidates"] == ["tests/test_a.py::test_failure"]
    assert result["full_shards"][1]["pytest_elapsed_seconds"] == 10.0
    assert result["full_shards"][1]["auxiliary_pytest_elapsed_seconds"] == [1.0]


@pytest.mark.parametrize("selected_count,expected_status", [(2, "complete"), (4, "incomplete")])
def test_pytest9_subfailed_summaries_count_reports_then_deduplicate_parent(evidence, selected_count, expected_status):
    run, jobs, logs, selected = evidence
    jobs[1]["conclusion"] = "failure"
    logs[501] = (
        log(1, failed=["tests/test_a.py::test_sub"])
        .replace(
            "FAILED tests/test_a.py::test_sub",
            "SUBFAILED(i=0) tests/test_a.py::test_sub - AssertionError\n"
            "SUBFAILED[message with spaces] (i=1) tests/test_a.py::test_sub - AssertionError\n"
            "FAILED tests/test_a.py::test_sub",
            1,
        )
        .replace("1 failed, 1 passed", "3 failed, 1 passed")
        .replace("selected 2 of", f"selected {selected_count} of")
    )
    result = shadow.compare(IDENTITY, run, jobs, logs, selected)
    assert result["status"] == expected_status, result["errors"]
    if expected_status == "complete":
        assert result["full_only_candidates"] == ["tests/test_a.py::test_sub"]
    else:
        assert result["full_only_candidate_count"] is None


@pytest.mark.parametrize("where", ["metadata", "log"])
@pytest.mark.parametrize("error", [HTTPException("bad HTTP"), IncompleteRead(b"partial", 10)])
def test_http_protocol_failures_retain_incomplete_report(tmp_path, evidence, prefetch, where, error):
    _, _, _, selected = evidence
    execute, _, _, failure, cache = prefetch
    failure[:] = [error, where]
    execute()
    assert type(error).__name__ in (cache / "fetch-errors.json").read_text()
    output = tmp_path / "evidence"
    report = shadow.guard(output, now=shadow.TRIAL_START)
    report["identity"] = IDENTITY
    shadow.write_json(output / "report.json", report)
    shadow.write_json(output / "pytest-results.json", selected)
    assert shadow.finalize(output, cache) == 2
    result = json.loads((output / "report.json").read_text())["comparison"]
    assert result["status"] == "incomplete"
    assert result["full_only_candidate_count"] is None
    assert any(type(error).__name__ in problem for problem in result["errors"])


@pytest.mark.parametrize("trial", ["active", "expired"])
def test_step_summary_is_published_once_at_lifecycle_end(tmp_path, monkeypatch, trial):
    summary = tmp_path / "step-summary"
    output = tmp_path / "evidence"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    instant = datetime.fromisoformat("2026-10-23T00:00:00+00:00" if trial == "active" else "2026-11-06T00:00:00+00:00")
    shadow.guard(output, now=instant)
    shadow.run_shadow(tmp_path, output, instant)  # Invalid checkout retains incomplete evidence.
    shadow.finalize(output)
    assert summary.read_text().count("# PR test tier shadow\n") == 1
    assert "Comparison: incomplete" in summary.read_text()


@pytest.mark.parametrize(
    "outcome,expected", [("success", True), ("failure", True), ("cancelled", False), ("skipped", False)]
)
@pytest.mark.parametrize("active", [True, False])
def test_shadow_test_work_requires_finished_full_shards(outcome, expected, active):
    text = (ROOT / ".github/workflows/ci.yml").read_text()
    job = text.split("  pr-tier-shadow:\n", 1)[1]
    conditions = re.findall(r"^        if: (.+)$", job, re.M)
    test_conditions = [condition for condition in conditions if "steps.trial.outputs.active" in condition]
    assert len(test_conditions) == 3
    for condition in test_conditions:
        expression = condition.removeprefix("${{ ").removesuffix(" }}")
        expression = expression.replace("steps.trial.outputs.active", repr("true" if active else "false"))
        expression = (
            expression.replace("needs.test-shards.result", repr(outcome)).replace("&&", "and").replace("||", "or")
        )
        assert eval(expression, {"__builtins__": {}}, {}) is (expected and active)


@pytest.mark.parametrize(
    "problem", ["missing-execution", "missing-short-summary", "ambiguous-summary", "truncated-captured-summary"]
)
def test_unattributed_terminal_output_is_incomplete(evidence, problem):
    run, jobs, logs, selected = evidence
    if problem == "missing-execution":
        logs[501] = log(1).replace("Run python scripts/ci_pytest_shard.py", "Run unknown")
    elif problem == "missing-short-summary":
        jobs[1]["conclusion"] = "failure"
        logs[501] = log(1, failed=["tests/test_a.py::test_failed"]).replace(
            "================ short test summary info ================\n", ""
        )
    elif problem == "ambiguous-summary":
        logs[501] = log(1).replace("2 passed, 6 deselected", "2 passed in 0.01s\n2 passed, 6 deselected")
    else:
        logs[501] = log(1).split("CI shard", 1)[0] + (
            "CI shard 2/4: selected 2 of 8 tests\n"
            "---------------- Captured stdout call ----------------\n2 passed in 0.01s\n"
        )
    result = shadow.compare(IDENTITY, run, jobs, logs, selected)
    assert result["status"] == "incomplete"
    assert result["full_only_candidate_count"] is None


def test_runner_cleanup_after_failed_step_does_not_replace_terminal_summary(evidence):
    run, jobs, logs, selected = evidence
    jobs[1]["conclusion"] = "failure"
    logs[501] = log(1, failed=["tests/test_a.py::test_failure"]).split("##[group]Run output=", 1)[0] + (
        "##[error]Process completed with exit code 1.\nPost job cleanup.\n[command]/usr/bin/git version\n"
    )
    result = shadow.compare(IDENTITY, run, jobs, logs, selected)
    assert result["status"] == "complete", result["errors"]
    assert result["full_only_candidates"] == ["tests/test_a.py::test_failure"]
