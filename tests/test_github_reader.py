"""Offline contracts for the node-local, GET-only GitHub adapter."""

import json
import sqlite3
import sys
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

import pytest

from brigade.claude_review_import import GetResponse, GetTransport
from brigade.github_reader import CommandResult, GitHubReader
from brigade.github_read_ledger import BudgetLedger

PATH = "/repos/example/project/pulls"


def gh(status=200, body=b"[]", **headers):
    head = f"HTTP/2.0 {status} status\r\n"
    head += "".join(f"{key.replace('_', '-')}: {value}\r\n" for key, value in headers.items())
    return CommandResult(0 if status < 400 else 1, head.encode() + b"\r\n" + body)


def relay(status=200, body=None, *, metadata=None, **headers):
    return CommandResult(
        0,
        json.dumps(
            {
                "status": status,
                "headers": headers,
                "body": [] if body is None else body,
                "body_encoding": "json",
                "relay": metadata or {},
            }
        ).encode(),
    )


class FakeRunner:
    def __init__(self, *responses, healthy=True):
        self.responses = deque(responses)
        self.calls = []
        self.healthy = healthy

    def __call__(self, argv, *, env, max_bytes, timeout):
        self.calls.append((list(argv), dict(env), max_bytes, timeout))
        if argv == ["octopool", "whoami"]:
            return CommandResult(0 if self.healthy else 1, b"")
        result = self.responses.popleft()
        if isinstance(result, Exception):
            raise result
        return result


def reader(tmp_path, runner, **options):
    ledger = BudgetLedger(tmp_path / "reads.sqlite")
    return GitHubReader("example/project", runner=runner, ledger=ledger, **options)


def test_public_healthy_routes_with_freshness_and_cached_probe(tmp_path):
    clock = [1000.0]
    runner = FakeRunner(relay(), relay(), relay())
    client = reader(tmp_path, runner, visibility="public", clock=lambda: clock[0])
    transport: GetTransport = client
    assert isinstance(transport.get(PATH, params={"page": 1}, max_bytes=100), GetResponse)
    client.get(PATH, params={"page": 2}, max_bytes=100, max_age=45)
    assert [c[0] for c in runner.calls].count(["octopool", "whoami"]) == 1
    argv, env, _, _ = runner.calls[1]
    assert argv[:4] == ["octopool", "request", "--method", "GET"]
    assert "cache-control=max-age=120" in argv
    assert "cache-control=max-age=45" in runner.calls[2][0]
    assert env == {"OCTOPOOL_NO_FALLBACK": "1", "OCTOPOOL_FRESH": None}
    clock[0] += 600
    client.get(PATH, params={}, max_bytes=100)
    assert [c[0] for c in runner.calls].count(["octopool", "whoami"]) == 2


@pytest.mark.parametrize("visibility", ["private", "unknown"])
def test_private_and_unknown_never_produce_octopool_argv(tmp_path, visibility):
    runner = FakeRunner(gh())
    client = reader(tmp_path, runner, visibility=visibility)
    assert client.get(PATH, params={}, max_bytes=100).status == 200
    assert all(call[0][0] == "gh" for call in runner.calls)
    client.observation(refresh_octopool_stats=True)
    assert all(call[0][0] == "gh" for call in runner.calls)


@pytest.mark.parametrize("allow_fallback", [False, True])
def test_unhealthy_probe_honors_fallback_opt_in_and_is_cached(tmp_path, allow_fallback):
    clock = [1000.0]
    runner = FakeRunner(*([gh(), gh()] if allow_fallback else []), relay(), healthy=False)
    client = reader(tmp_path, runner, visibility="public", allow_public_fallback=allow_fallback, clock=lambda: clock[0])
    for _ in range(2):
        result = client.get(PATH, params={}, max_bytes=100)
        if not allow_fallback:
            assert all(call[0][0] == "octopool" for call in runner.calls)
        assert (result.status, result.state) == ((200, "ok") if allow_fallback else (502, "inconclusive"))
    assert [c[0][0] for c in runner.calls] == (["octopool", "gh", "gh"] if allow_fallback else ["octopool"])
    summary = client.observation()["github_reads"]
    assert summary["requests"] == (2 if allow_fallback else 0)
    assert summary["fallbacks"] == (2 if allow_fallback else 0)
    clock[0] += 600
    runner.healthy = True
    result = client.get(PATH, params={}, max_bytes=100)
    assert (result.status, result.route) == (200, "octopool")
    assert runner.calls[-2][0] == ["octopool", "whoami"]


@pytest.mark.parametrize(
    "response",
    [
        relay(404),
        relay(403),
        relay(424),
        CommandResult(1, b""),
        relay(metadata={"stale_ok": True}),
        relay(metadata={"fallback_local": True}),
    ],
)
@pytest.mark.parametrize("allow_fallback", [False, True])
def test_octopool_failure_is_inconclusive_and_fallback_is_explicit(tmp_path, response, allow_fallback):
    runner = FakeRunner(response, gh(404))
    client = reader(tmp_path, runner, visibility="public", allow_public_fallback=allow_fallback)
    result = client.get(PATH, params={}, max_bytes=100)
    assert result.state == ("unavailable" if allow_fallback else "inconclusive")
    assert result.status == (404 if allow_fallback else 502)
    assert sum(call[0][0] == "gh" for call in runner.calls) == int(allow_fallback)
    summary = client.observation()["github_reads"]
    assert summary["inconclusive"] == 1
    assert summary["fallbacks"] == int(allow_fallback)


def test_gh_etag_revalidation_keeps_body_and_counts_304(tmp_path):
    runner = FakeRunner(gh(body=b"[1]", etag='"first"'), gh(304), gh(body=b"[2]"))
    client = reader(tmp_path, runner, visibility="private")
    assert client.get(PATH, params={"page": 1}, max_bytes=100).body == b"[1]"
    result = client.get(PATH, params={"page": 1}, max_bytes=2)
    assert result.status == 200 and result.body == b"[1" and result.truncated
    assert result.state == "not-modified" and result.upstream_status == 304
    assert 'If-None-Match: "first"' in runner.calls[1][0]
    client.get(PATH, params={"page": 2}, max_bytes=100)
    assert not any("If-None-Match" in value for value in runner.calls[2][0])
    assert client.observation()["github_reads"]["etag_hits"] == 1


def test_304_polls_do_not_exhaust_the_primary_cap(tmp_path):
    runner = FakeRunner(gh(etag='"v1"'), *[gh(304) for _ in range(5)], gh())
    client = reader(tmp_path, runner, visibility="private", hourly_caps={"user": 2})
    for _ in range(7):
        assert client.get(PATH, params={}, max_bytes=100).status == 200
    assert client.get(PATH, params={}, max_bytes=100).state == "budget-exhausted"
    summary = client.observation()["github_reads"]
    assert (summary["requests"], summary["etag_hits"]) == (7, 5)


@pytest.mark.parametrize("status,state", [(403, "access-denied"), (404, "unavailable"), (429, "rate-limited")])
def test_gh_http_errors_are_explicit(tmp_path, status, state):
    runner = FakeRunner(gh(status))
    result = reader(tmp_path, runner, visibility="private").get(PATH, params={}, max_bytes=100)
    assert (result.status, result.state) == (status, state)


@pytest.mark.parametrize("status", [403, 429])
@pytest.mark.parametrize("retry_after", ["30", "Thu, 01 Jan 1970 00:17:10 GMT"])
def test_retry_after_defers_other_endpoints_and_survives_reopen(tmp_path, status, retry_after):
    clock = [1000.0]
    runner = FakeRunner(gh(status, retry_after=retry_after), gh())
    client = reader(tmp_path, runner, visibility="private", clock=lambda: clock[0])
    client.get(PATH, params={}, max_bytes=100)
    reopened = reader(tmp_path, runner, visibility="private", clock=lambda: clock[0])
    result = reopened.get(PATH + "/1", params={}, max_bytes=100)
    assert result.state == "deferred" and result.retry_at == 1030
    assert len(runner.calls) == 1
    clock[0] = 1030
    assert reopened.get(PATH, params={}, max_bytes=100).status == 200


@pytest.mark.parametrize("headers", [{"retry_after": "30"}, {"x_ratelimit_remaining": "0"}])
def test_rate_limit_403_is_not_a_permission_denial(tmp_path, headers):
    client = reader(tmp_path, FakeRunner(gh(403, **headers)), visibility="private")
    result = client.get(PATH, params={}, max_bytes=100)
    assert (result.status, result.state, result.upstream_status) == (429, "rate-limited", 403)
    assert client.observation()["github_reads"]["denied"] == 0


def test_poll_interval_and_primary_reset_prevent_early_requests(tmp_path):
    clock = [1000.0]
    runner = FakeRunner(gh(x_poll_interval=20), gh(x_ratelimit_remaining=0, x_ratelimit_reset=1100), gh())
    client = reader(tmp_path, runner, visibility="private", clock=lambda: clock[0])
    client.get(PATH, params={}, max_bytes=100)
    assert client.get(PATH, params={}, max_bytes=100).state == "deferred"
    clock[0] = 1020
    client.get(PATH, params={}, max_bytes=100)
    assert client.get(PATH + "/1", params={}, max_bytes=100).state == "budget-exhausted"
    assert len(runner.calls) == 2
    clock[0] = 1100
    assert client.get(PATH, params={}, max_bytes=100).status == 200


def test_ledger_totals_caps_and_rolling_window_are_durable(tmp_path):
    clock = [1000.0]
    runner = FakeRunner(gh(etag='"v1"', x_ratelimit_remaining=99, x_ratelimit_reset=4600), gh(304), gh(404), gh())
    client = reader(tmp_path, runner, visibility="private", hourly_caps={"user": 2}, clock=lambda: clock[0])
    for _ in range(3):
        client.get(PATH, params={}, max_bytes=100)
    reopened = reader(tmp_path, runner, visibility="private", hourly_caps={"user": 2}, clock=lambda: clock[0])
    assert reopened.get(PATH, params={}, max_bytes=100).state == "budget-exhausted"
    assert len(runner.calls) == 3
    summary = reopened.observation()["github_reads"]
    assert (summary["requests"], summary["etag_hits"], summary["budget_exhausted"]) == (3, 1, 1)
    assert summary["credentials"]["user"]["remaining"] == 99
    assert summary["credentials"]["user"]["reset"] == 4600
    assert summary["statuses"] == {"200": 1, "304": 1, "404": 1}
    clock[0] = 4600
    assert reopened.get(PATH, params={}, max_bytes=100).status == 200


@pytest.mark.parametrize("caps", [{"octopool": 0}, {"octopool": 0, "user": 0}])
def test_exhausted_cap_issues_no_probe_or_read(tmp_path, caps):
    runner = FakeRunner()
    client = reader(tmp_path, runner, visibility="public", hourly_caps=caps)
    assert client.get(PATH, params={}, max_bytes=100).state == "budget-exhausted"
    assert runner.calls == []


def test_requests_are_serial_across_readers_sharing_a_ledger(tmp_path):
    active = 0
    maximum = 0
    mutex = Lock()

    def run(argv, **kwargs):
        nonlocal active, maximum
        with mutex:
            active += 1
            maximum = max(maximum, active)
        # A second request must wait for the SQLite write transaction.
        import time

        time.sleep(0.01)
        with mutex:
            active -= 1
        return gh()

    clients = [reader(tmp_path, run, visibility="private", hourly_caps={"user": 2}) for _ in range(4)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda client: client.get(PATH, params={}, max_bytes=100), clients))
    assert maximum == 1
    assert sorted(result.state for result in results) == ["budget-exhausted", "budget-exhausted", "ok", "ok"]


def test_busy_queue_is_visible_without_spending_a_request(tmp_path):
    runner = FakeRunner(gh())
    ledger = BudgetLedger(tmp_path / "reads.sqlite")
    client = GitHubReader("example/project", runner=runner, ledger=ledger, visibility="private")
    # Exercise a real lock timeout without holding the suite for 30 seconds.
    with sqlite3.connect(ledger.path) as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        original_connect = sqlite3.connect

        def impatient_connect(*args, **kwargs):
            kwargs["timeout"] = 0.01
            return original_connect(*args, **kwargs)

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(sqlite3, "connect", impatient_connect)
            result = client.get(PATH, params={}, max_bytes=100)
        assert (result.status, result.state) == (503, "queue-busy")
        assert runner.calls == []
        blocker.rollback()
    assert client.get(PATH, params={}, max_bytes=100).status == 200


@pytest.mark.parametrize("message", ["database is locked", "database table is locked", "unable to open database file"])
def test_queue_errors_without_sqlite_errorcode(tmp_path, monkeypatch, message):
    runner = FakeRunner()
    client = reader(tmp_path, runner, visibility="private")

    def fail_connect(*args, **kwargs):
        # Python 3.10 sqlite errors have no sqlite_errorcode attribute.
        raise sqlite3.OperationalError(message)

    monkeypatch.setattr(sqlite3, "connect", fail_connect)
    if "locked" in message:
        result = client.get(PATH, params={}, max_bytes=100)
        assert (result.status, result.state) == (503, "queue-busy")
    else:
        with pytest.raises(sqlite3.OperationalError, match=message):
            client.get(PATH, params={}, max_bytes=100)
    assert runner.calls == []


@pytest.mark.parametrize("failure", [CommandResult(1, b""), CommandResult(0, b"", truncated=True), OSError()])
@pytest.mark.parametrize("allow_fallback", [False, True])
def test_failed_relay_marks_health_unavailable_until_probe_ttl(tmp_path, failure, allow_fallback):
    clock = [1000.0]
    runner = FakeRunner(failure, *([gh(), gh()] if allow_fallback else []), relay())
    client = reader(tmp_path, runner, visibility="public", allow_public_fallback=allow_fallback, clock=lambda: clock[0])
    for _ in range(2):
        result = client.get(PATH, params={}, max_bytes=100)
        if not allow_fallback:
            assert all(call[0][0] == "octopool" for call in runner.calls)
        assert (result.status, result.state) == ((200, "ok") if allow_fallback else (502, "inconclusive"))
    expected_calls = ["octopool", "octopool"] + (["gh", "gh"] if allow_fallback else [])
    assert [call[0][0] for call in runner.calls] == expected_calls
    summary = client.observation()["github_reads"]
    assert summary["requests"] == (3 if allow_fallback else 1)
    assert summary["fallbacks"] == (2 if allow_fallback else 0)
    clock[0] += 600
    assert client.get(PATH, params={}, max_bytes=100).route == "octopool"
    assert runner.calls[-2][0] == ["octopool", "whoami"]


@pytest.mark.parametrize("limit", ["entries", "bytes"])
def test_etag_body_cache_evicts_least_recently_used_endpoint(tmp_path, monkeypatch, limit):
    from brigade import github_reader

    monkeypatch.setattr(github_reader, "_CACHE_MAX_ENTRIES", 2 if limit == "entries" else 128, raising=False)
    monkeypatch.setattr(github_reader, "_CACHE_MAX_BYTES", 6 if limit == "bytes" else 16 * 1024 * 1024, raising=False)
    runner = FakeRunner(
        gh(body=b"[1]", etag='"v1"'),
        gh(body=b"[2]", etag='"v2"'),
        gh(304),
        gh(body=b"[3]", etag='"v3"'),
        gh(304),
        gh(304),
    )
    client = reader(tmp_path, runner, visibility="private")
    for page in (1, 2, 1, 3):
        assert client.get(PATH, params={"page": page}, max_bytes=100).status == 200
    assert client.get(PATH, params={"page": 1}, max_bytes=100).body == b"[1]"
    assert client.get(PATH, params={"page": 2}, max_bytes=100).state == "inconclusive"
    assert not any("If-None-Match" in arg for arg in runner.calls[-1][0])


def test_force_fresh_requires_a_named_merge_gate(tmp_path):
    runner = FakeRunner(relay())
    client = reader(tmp_path, runner, visibility="public")
    with pytest.raises(ValueError):
        client.get(PATH, params={}, max_bytes=100, max_age=0)
    assert runner.calls == []
    client.get(PATH, params={}, max_bytes=100, merge_gating_reason="required checks for exact head")
    assert runner.calls[1][1]["OCTOPOOL_FRESH"] == "1"
    assert "cache-control=max-age=0" in runner.calls[1][0]


@pytest.mark.parametrize(
    "path", ["/user", PATH + "/../issues", PATH + "?page=1", "/repos/example/private/pulls", PATH + "/%2e%2e"]
)
def test_endpoint_validation_stops_cross_repo_or_option_injection(tmp_path, path):
    runner = FakeRunner()
    client = reader(tmp_path, runner, visibility="public")
    with pytest.raises(ValueError):
        client.get(path, params={}, max_bytes=100)
    assert runner.calls == []


def test_truncation_is_preserved_and_not_cached(tmp_path):
    runner = FakeRunner(gh(body=b"[1234]", etag='"v1"'), gh(304))
    client = reader(tmp_path, runner, visibility="private")
    result = client.get(PATH, params={}, max_bytes=2)
    assert result.body == b"[1" and result.truncated
    assert client.get(PATH, params={}, max_bytes=100).state == "inconclusive"
    assert not any("If-None-Match" in value for value in runner.calls[1][0])


def test_stats_summary_is_opt_in_and_excludes_identity_or_error_text(tmp_path):
    runner = FakeRunner(
        relay(), CommandResult(0, b'{"requests":12,"hits":3,"login":"redacted-example","token":"never-retain"}')
    )
    client = reader(tmp_path, runner, visibility="public")
    client.get(PATH, params={}, max_bytes=100)
    assert client.observation()["octopool_stats"] is None
    summary = client.observation(refresh_octopool_stats=True)
    assert summary["octopool_stats"] == {"requests": 12, "hits": 3}
    assert runner.calls[-1][0] == ["octopool", "stats", "--json"]
    assert "never-retain" not in json.dumps(summary)


def test_runner_failure_is_counted_without_retaining_exception(tmp_path):
    runner = FakeRunner(RuntimeError("sensitive transport details"))
    client = reader(tmp_path, runner, visibility="private")
    assert client.get(PATH, params={}, max_bytes=100).state == "transport-unavailable"
    assert "sensitive" not in json.dumps(client.observation())
    assert client.observation()["github_reads"]["requests"] == 1


def test_importer_keeps_truncated_pagination_explicit(tmp_path):
    from brigade.claude_review_import import Limits, collect_managed_review

    def run(argv, **kwargs):
        endpoint = next(value for value in argv if value.startswith("/repos/"))
        payload = {"total_count": 2, "check_runs": [{"id": 1}]} if "/check-runs?" in endpoint else []
        return gh(body=json.dumps(payload).encode())

    client = reader(tmp_path, run, visibility="private")
    result = collect_managed_review(
        client,
        owner="example",
        repo="project",
        pr_number=1,
        current_head="a" * 40,
        expected_app_id=123,
        expected_app_slug="example-review",
        observed_at="2026-01-03T00:00:00Z",
        limits=Limits(max_pages=1, per_page=1),
    )
    assert "page-budget" in result["sources"][0]["reasons"]
    assert not result["sources"][0]["complete"]
    assert client.observation()["github_reads"]["requests"] == 4


@pytest.mark.parametrize("encoding,body,expected", [("text", "example", b"example"), ("base64", "AP8=", b"\x00\xff")])
def test_relay_body_encodings_are_decoded_and_capped(tmp_path, encoding, body, expected):
    response = {"status": 200, "body_encoding": encoding, "body": body, "headers": {}, "relay": {}}
    runner = FakeRunner(CommandResult(0, json.dumps(response).encode()))
    result = reader(tmp_path, runner, visibility="public").get(PATH, params={}, max_bytes=1)
    assert result.body == expected[:1] and result.truncated


def test_output_cap_is_passed_to_runner_and_truncated_frames_are_unknown(tmp_path):
    runner = FakeRunner(CommandResult(0, b"incomplete envelope", truncated=True))
    result = reader(tmp_path, runner, visibility="public").get(PATH, params={}, max_bytes=10)
    assert result.state == "inconclusive" and result.truncated
    assert runner.calls[-1][2] == 65556


def test_nested_octopool_stats_and_denials_remain_visible_without_client_labels(tmp_path):
    stats = {
        "totals": {"requests": 8, "cache_served_responses": 3, "denied": 2},
        "clients": [{"client": "private-example", "requests": 8}],
    }
    runner = FakeRunner(relay(403), CommandResult(0, json.dumps(stats).encode()))
    client = reader(tmp_path, runner, visibility="public")
    client.get(PATH, params={}, max_bytes=100)
    result = client.observation(refresh_octopool_stats=True)
    assert result["github_reads"]["denied"] == 1
    assert result["octopool_stats"] == {"totals": stats["totals"]}
    assert "private-example" not in json.dumps(result)


def test_fallback_cannot_bypass_native_credential_cap(tmp_path):
    runner = FakeRunner(relay(404))
    client = reader(tmp_path, runner, visibility="public", allow_public_fallback=True, hourly_caps={"user": 0})
    result = client.get(PATH, params={}, max_bytes=100)
    assert result.state == "budget-exhausted"
    assert all(call[0][0] == "octopool" for call in runner.calls)
    assert client.observation()["github_reads"]["requests"] == 1


def test_supplied_subprocess_runner_bounds_output_and_discards_stderr():
    from brigade.github_reader import SubprocessRunner

    run = SubprocessRunner()
    result = run(
        [
            sys.executable,
            "-c",
            "import os, sys; sys.stderr.write('discard-me'); sys.stdout.write(os.environ['EXAMPLE'])",
        ],
        env={"EXAMPLE": "literal $(no-shell)"},
        max_bytes=100,
        timeout=5,
    )
    assert result == CommandResult(0, b"literal $(no-shell)")
    overflow = run(
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'x' * 1000000)"],
        env={},
        max_bytes=100,
        timeout=5,
    )
    assert overflow.stdout == b"x" * 100 and overflow.truncated


def test_supplied_subprocess_runner_timeout_cannot_expose_partial_http_success():
    from brigade.github_reader import SubprocessRunner

    result = SubprocessRunner()(
        [sys.executable, "-c", "import sys, time; print('HTTP/2.0 200 OK\\n\\n[]', flush=True); time.sleep(10)"],
        env={},
        max_bytes=100,
        timeout=0.1,
    )
    assert result.returncode != 0 and result.stdout == b""


@pytest.mark.parametrize("merge_gating_reason", [None, "required checks for exact head"])
def test_subprocess_reads_clear_inherited_fresh_unless_merge_gating(tmp_path, monkeypatch, merge_gating_reason):
    from brigade.github_reader import SubprocessRunner

    # Use a real child without contacting GitHub or reading operator config.
    class EnvironmentRunner(SubprocessRunner):
        def __call__(self, argv, **kwargs):
            script = (
                "import os, sys; "
                "sys.stdout.write('HTTP/2.0 200 OK\\r\\n\\r\\n'); "
                "sys.stdout.write(os.environ.get('OCTOPOOL_FRESH', 'unset'))"
            )
            return super().__call__([sys.executable, "-c", script], **kwargs)

    monkeypatch.setenv("OCTOPOOL_FRESH", "1")
    client = reader(tmp_path, EnvironmentRunner(), visibility="private")
    result = client.get(PATH, params={}, max_bytes=100, merge_gating_reason=merge_gating_reason)
    assert (result.status, result.body) == (200, b"1" if merge_gating_reason else b"unset")
