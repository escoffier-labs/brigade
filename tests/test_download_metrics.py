"""Downloads are summed only over complete, validated, matching UTC days."""

import importlib.util
import json
import os
import shlex
import shutil
import sys
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from http.client import HTTPException
from io import BytesIO
from pathlib import Path
from urllib.error import URLError

import pytest

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 6, 20, 12, tzinfo=timezone.utc)


def _workflow_bash() -> str:
    if os.name != "nt":
        bash = shutil.which("bash")
        if bash is not None:
            return bash
        pytest.fail("workflow shell execution requires Bash; install Bash to run these assertions")
    # Windows' PATH may resolve bash.exe to the WSL launcher. Select Git Bash,
    # including non-default Git installations, as in test_ci_workflow.py.
    git = shutil.which("git")
    roots = [Path(git).resolve().parent.parent] if git else []
    roots.extend(Path(os.environ[key]) / "Git" for key in ("ProgramFiles", "ProgramFiles(x86)") if key in os.environ)
    if "LOCALAPPDATA" in os.environ:
        roots.append(Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Git")
    for root in roots:
        bash = root / "bin" / "bash.exe"
        if bash.is_file():
            return str(bash)
    pytest.fail("Install Git for Windows with Git Bash to run these assertions; WSL bash.exe is unsuitable")


@pytest.fixture
def metrics(monkeypatch):
    source = ROOT / "scripts/download_metrics.py"
    assert source.is_file(), "The combined downloads generator is missing"
    spec = importlib.util.spec_from_file_location("download_metrics", source)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class FrozenClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz else NOW.replace(tzinfo=None)

    monkeypatch.setattr(module, "datetime", FrozenClock)
    return module


@pytest.fixture
def payloads():
    days = [(NOW.date() - timedelta(days=n)).isoformat() for n in range(35, 0, -1)]
    pypi = {
        "package": "brigade-cli",
        "type": "overall_downloads",
        "data": [
            {"category": category, "date": day, "downloads": count}
            for category, count in [("without_mirrors", 10), ("with_mirrors", 900)]
            for day in days[:-1]
        ],
    }
    npm = {
        "package": "@escoffier-labs/brigade",
        "start": days[0],
        "end": days[-1],
        "downloads": [{"day": day, "downloads": 2} for day in days],
    }
    return pypi, npm


def test_combines_thirty_matching_completed_days_and_excludes_mirrors(metrics, payloads):
    badge = metrics.aggregate(*payloads, now=NOW)
    assert badge == {
        "schemaVersion": 1,
        "label": "downloads/month",
        "message": "360",
        "color": "blue",
        "total": 360,
        "sources": {
            "pypi": {"package": "brigade-cli", "downloads": 300, "mirrors": False},
            "npm": {"package": "@escoffier-labs/brigade", "downloads": 60},
        },
        "period": {"start": "2026-05-20", "end": "2026-06-18", "days": 30, "timezone": "UTC"},
        "updatedAt": "2026-06-20T12:00:00Z",
    }


@pytest.mark.parametrize("source", [0, 1], ids=["pypi", "npm"])
@pytest.mark.parametrize("count", [-1, 1.5, "2", True, None, 2**53])
def test_rejects_invalid_counts(metrics, payloads, source, count):
    rows = payloads[source]["data" if source == 0 else "downloads"]
    rows[-1]["downloads"] = count
    with pytest.raises(ValueError):
        metrics.aggregate(*payloads, now=NOW)


@pytest.mark.parametrize("source", [0, 1], ids=["pypi", "npm"])
@pytest.mark.parametrize(
    "damage", ["duplicate", "gap", "bad-date", "empty", "wrong-package", "future", "missing-count"]
)
def test_rejects_incomplete_or_malformed_series(metrics, payloads, source, damage):
    payload = payloads[source]
    rows = payload["data" if source == 0 else "downloads"]
    if damage == "duplicate":
        rows.append(deepcopy(rows[10]))
    elif damage == "gap":
        del rows[10]
    elif damage == "bad-date":
        rows[10]["date" if source == 0 else "day"] = "2026-02-30"
    elif damage == "empty":
        rows.clear()
    elif damage == "wrong-package":
        payload["package"] = "@example/companion"
    elif damage == "future":
        rows[10]["date" if source == 0 else "day"] = NOW.date().isoformat()
    else:
        del rows[10]["downloads"]
    with pytest.raises(ValueError):
        metrics.aggregate(*payloads, now=NOW)


@pytest.mark.parametrize("source", [0, 1], ids=["pypi", "npm"])
def test_rejects_stale_watermark_in_either_source(metrics, payloads, source):
    payload = payloads[source]
    key = "data" if source == 0 else "downloads"
    date_key = "date" if source == 0 else "day"
    payload[key] = [row for row in payload[key] if row[date_key] <= "2026-06-16"]
    if source == 1:
        payload["end"] = "2026-06-16"
    with pytest.raises(ValueError, match="stale"):
        metrics.aggregate(*payloads, now=NOW)


@pytest.mark.parametrize("field,value", [("start", "2026-05-01"), ("end", "2026-06-18")])
def test_rejects_npm_envelope_that_disagrees_with_daily_data(metrics, payloads, field, value):
    payloads[1][field] = value
    with pytest.raises(ValueError):
        metrics.aggregate(*payloads, now=NOW)


def test_explicit_zero_days_are_valid(metrics, payloads):
    for row in payloads[1]["downloads"]:
        row["downloads"] = 0
    assert metrics.aggregate(*payloads, now=NOW)["total"] == 300


@pytest.mark.parametrize("source", [0, 1], ids=["pypi", "npm"])
def test_accepts_largest_exact_javascript_integer(metrics, payloads, source):
    for payload, key in zip(payloads, ["data", "downloads"], strict=True):
        for row in payload[key]:
            row["downloads"] = 0
    payloads[source]["data" if source == 0 else "downloads"][10]["downloads"] = 2**53 - 1
    badge = metrics.aggregate(*payloads, now=NOW)
    assert badge["total"] == 9007199254740991
    assert badge["message"] == "9,007,199,254,740,991"
    assert badge["sources"]["pypi" if source == 0 else "npm"]["downloads"] == 9007199254740991


@pytest.mark.parametrize("overflow", ["pypi-subtotal", "npm-subtotal", "combined"])
def test_rejects_overflow_from_individually_valid_counts(metrics, payloads, overflow):
    for payload, key in zip(payloads, ["data", "downloads"], strict=True):
        for row in payload[key]:
            row["downloads"] = 0
    if overflow == "combined":
        payloads[0]["data"][10]["downloads"] = 2**52
        payloads[1]["downloads"][10]["downloads"] = 2**52
    else:
        rows = payloads[0]["data"] if overflow == "pypi-subtotal" else payloads[1]["downloads"]
        rows[10]["downloads"] = 2**53 - 1
        rows[11]["downloads"] = 1
    with pytest.raises(ValueError):
        metrics.aggregate(*payloads, now=NOW)


@pytest.mark.parametrize(
    "failure",
    [URLError("offline"), ValueError("bad JSON"), TimeoutError("timeout"), HTTPException("remote-controlled-detail")],
)
def test_fetch_failure_preserves_last_successful_metric(metrics, monkeypatch, tmp_path, capsys, failure):
    output = tmp_path / "downloads.json"
    output.write_text('{"previous": "successful"}\n')

    def fail(_url):
        raise failure

    monkeypatch.setattr(metrics, "fetch_json", fail)
    assert metrics.main([str(output)]) == 1
    assert output.read_text() == '{"previous": "successful"}\n'
    assert list(tmp_path.iterdir()) == [output]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "Downloads refresh failed. Previous metric retained.\n"


def test_nested_json_failure_is_sanitized_and_preserves_metric(metrics, monkeypatch, tmp_path, capsys):
    # CPython's JSON decoder can have a separate limit from Python recursion.
    depth = 10_000
    body = b"[" * depth + b"0" + b"]" * depth
    monkeypatch.setattr(metrics, "urlopen", lambda *_args, **_kwargs: BytesIO(body))
    output = tmp_path / "downloads.json"
    previous = b'{"previous": "successful"}\n'
    output.write_bytes(previous)
    assert metrics.main([str(output)]) == 1
    assert output.read_bytes() == previous
    assert list(tmp_path.iterdir()) == [output]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "Downloads refresh failed. Previous metric retained.\n"


def test_cli_writes_metric_only_after_both_official_sources_validate(metrics, payloads, monkeypatch, tmp_path):
    urls = []

    def fetch(url):
        urls.append(url)
        return payloads[len(urls) - 1]

    monkeypatch.setattr(metrics, "fetch_json", fetch)
    output = tmp_path / "downloads.json"
    assert metrics.main([str(output)]) == 0
    badge = json.loads(output.read_text())
    assert badge["total"] == 360
    assert badge["sources"]["pypi"]["downloads"] == 300
    assert badge["sources"]["npm"]["downloads"] == 60
    assert urls == [
        "https://pypistats.org/api/packages/brigade-cli/overall?mirrors=false",
        "https://api.npmjs.org/downloads/range/last-year/@escoffier-labs/brigade",
    ]


@pytest.mark.parametrize("body", [b'{"downloads": 1, "downloads": 2}', b'{"downloads": NaN}', b"not JSON"])
def test_fetch_rejects_ambiguous_or_invalid_json(metrics, monkeypatch, body):
    monkeypatch.setattr(metrics, "urlopen", lambda *_args, **_kwargs: BytesIO(body))
    with pytest.raises(ValueError):
        metrics.fetch_json("https://example.com/stats")


def test_invalid_source_data_also_preserves_existing_metric(metrics, payloads, monkeypatch, tmp_path):
    payloads[1]["downloads"][10]["downloads"] = -1
    responses = iter(payloads)
    monkeypatch.setattr(metrics, "fetch_json", lambda _url: next(responses))
    output = tmp_path / "downloads.json"
    output.write_text("previous success\n")
    assert metrics.main([str(output)]) == 1
    assert output.read_text() == "previous success\n"


@pytest.mark.parametrize("existing", ["absent", "owned", "unrelated"])
def test_workflow_bootstraps_only_absent_branch_and_preserves_owned_contents(tmp_path, existing):
    import subprocess
    import textwrap

    def git(*args, cwd=tmp_path):
        return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout

    remote = tmp_path / "remote.git"
    git("init", "--bare", str(remote))
    badge = {
        "schemaVersion": 1,
        "label": "downloads/month",
        "total": 360,
        "sources": {"pypi": {"package": "brigade-cli"}, "npm": {"package": "@escoffier-labs/brigade"}},
    }
    if existing != "absent":
        seed = tmp_path / "seed"
        git("init", str(seed))
        git("checkout", "--orphan", "download-metrics", cwd=seed)
        (seed / "ref").write_text("preserve this content\n")
        if existing == "owned":
            (seed / "downloads.json").write_text(json.dumps(badge))
        git("add", ".", cwd=seed)
        git("-c", "user.name=Example", "-c", "user.email=example@example.invalid", "commit", "-m", "seed", cwd=seed)
        git("push", str(remote), "HEAD:refs/heads/download-metrics", cwd=seed)
    runner = tmp_path / "runner"
    runner.mkdir()
    badge["total"] = 720
    (runner / "downloads.json").write_text(json.dumps(badge))
    workflow = (ROOT / ".github/workflows/download-metrics.yml").read_text()
    # Execute the shipped publishing step against an isolated local bare remote.
    shell = textwrap.dedent(workflow.split("        run: |\n", 1)[1])
    # Git Bash need not have Python on PATH. Use this test's interpreter for
    # both publishing and replay, with paths that Bash can interpret on Windows.
    python = shlex.quote(Path(sys.executable).as_posix())
    command = [_workflow_bash(), "-c", f'python() {{ {python} "$@"; }}\n' + shell]
    env = {
        **os.environ,
        "RUNNER_TEMP": runner.as_posix(),
        "GITHUB_SERVER_URL": tmp_path.as_posix(),
        "GITHUB_REPOSITORY": "remote",
        "GH_TOKEN": "",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    result = subprocess.run(command, env=env, cwd=tmp_path, capture_output=True, text=True)
    if existing == "unrelated":
        assert result.returncode != 0
        assert "Refusing unrelated" in result.stderr
        assert git("--git-dir", str(remote), "ls-tree", "--name-only", "download-metrics").splitlines() == ["ref"]
    else:
        assert result.returncode == 0, result.stderr
        stored = json.loads(git("--git-dir", str(remote), "show", "download-metrics:downloads.json"))
        assert stored["total"] == 720
        files = git("--git-dir", str(remote), "ls-tree", "--name-only", "download-metrics").splitlines()
        assert files == (["downloads.json", "ref"] if existing == "owned" else ["downloads.json"])
        if existing == "owned":
            assert git("--git-dir", str(remote), "show", "download-metrics:ref") == "preserve this content\n"
        head = git("--git-dir", str(remote), "rev-parse", "download-metrics")
        replay = subprocess.run(command, env=env, cwd=tmp_path, capture_output=True, text=True)
        assert replay.returncode == 0, replay.stderr
        assert git("--git-dir", str(remote), "rev-parse", "download-metrics") == head
    assert git("--git-dir", str(remote), "for-each-ref", "--format=%(refname)").splitlines() == [
        "refs/heads/download-metrics"
    ]
