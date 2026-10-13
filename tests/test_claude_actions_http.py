"""Live transport contracts at an entirely offline standard-library HTTP boundary."""

import builtins
import http.client
import io
import json
import socket
import ssl
import traceback
import urllib.request
from collections import deque

import pytest

from brigade.claude_actions_evidence import ConfirmedWorkflow, normalize_run
from brigade.claude_actions_http import GitHubActionsTransport, TransportError
from brigade.claude_actions_import import collect_actions
from brigade.claude_actions_metadata import collect_metadata
from brigade.claude_review_import import GetResponse

REPO = "example-org/example-repo"
BASE = f"/repos/{REPO}/actions"
WORKFLOW = f"{BASE}/workflows/7"
RUNS = f"{WORKFLOW}/runs"
ATTEMPT = f"{BASE}/runs/11/attempts/1"
JOBS = f"{ATTEMPT}/jobs"
ARTIFACTS = f"{BASE}/runs/11/artifacts"
HEAD = "a" * 40
REVISION = "b" * 40
OBSERVED = "2026-10-10T12:00:00Z"
PRIVATE = "FAKE_PRIVATE_CREDENTIAL"
UNTRUSTED = f"https://provider.example/{PRIVATE}/download"


class OfflineSocket:
    def __init__(self, wire):
        self.stream = io.BytesIO(wire)

    def makefile(self, *args):
        return self.stream


class Boundary:
    def __init__(self):
        self.responses = deque()
        self.calls = []
        self.connections = []
        self.reads = []
        self.closed = []
        self.failure = None
        self.read_failure = None
        self.close_failure = None

    def reply(self, body=b"{}", status=200, headers=None, *, chunked=False):
        headers = dict(headers or {})
        if chunked:
            headers["Transfer-Encoding"] = "chunked"
            body = f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n\r\n"
        else:
            headers.setdefault("Content-Length", str(len(body)))
        wire = f"HTTP/1.1 {status} FAKE_PRIVATE_REASON\r\n".encode()
        wire += b"".join(f"{k}: {v}\r\n".encode() for k, v in headers.items())
        self.responses.append(wire + b"\r\n" + body)

    def json(self, payload):
        self.reply(json.dumps(payload).encode())


@pytest.fixture
def boundary(monkeypatch):
    boundary = Boundary()

    class Response(http.client.HTTPResponse):
        def read(self, amt=None):
            boundary.reads.append(amt)
            if boundary.read_failure:
                raise boundary.read_failure
            return super().read(amt)

        def close(self):
            boundary.closed.append(True)
            failure, boundary.close_failure = boundary.close_failure, None
            super().close()
            if failure:
                raise failure

    class Connection(http.client.HTTPSConnection):
        def __init__(self, host, timeout=None, **kwargs):
            self.host = host
            self.timeout = timeout
            self.sock = None
            boundary.connections.append(kwargs)

        def set_debuglevel(self, level):
            pass

        def request(self, method, url, body, headers, **kwargs):
            boundary.calls.append(
                {
                    "host": self.host,
                    "timeout": self.timeout,
                    "method": method,
                    "url": url,
                    "body": body,
                    "headers": {k.lower(): v for k, v in headers.items()},
                }
            )
            if boundary.failure:
                raise boundary.failure

        def getresponse(self):
            assert boundary.responses, "unexpected extra HTTP request"
            response = Response(OfflineSocket(boundary.responses.popleft()), method="GET")
            response.begin()
            return response

        def close(self):
            pass

    monkeypatch.setattr(http.client, "HTTPSConnection", Connection)
    return boundary


@pytest.mark.parametrize("path", [WORKFLOW, RUNS, ATTEMPT, JOBS, ARTIFACTS])
def test_only_supported_metadata_routes_use_fixed_https_get(boundary, path):
    boundary.reply()
    transport = GitHubActionsTransport(repository=REPO.upper(), token=PRIVATE, timeout_seconds=3)
    assert transport.get(path, params={}, max_bytes=128) == GetResponse(200, b"{}")
    call = boundary.calls[0]
    assert call["host"] == "api.github.com"
    assert call["method"] == "GET" and call["body"] is None
    assert call["url"] == path and call["timeout"] == 3
    assert call["headers"]["authorization"] == f"Bearer {PRIVATE}"
    assert call["headers"]["accept"] == "application/vnd.github+json"
    assert call["headers"]["host"] == "api.github.com"
    assert boundary.closed
    assert PRIVATE not in repr(transport)


@pytest.mark.parametrize(
    "path",
    [
        f"https://api.github.com{WORKFLOW}",
        f"http://api.github.com{WORKFLOW}",
        f"https://provider.example{WORKFLOW}",
        f"//api.github.com{WORKFLOW}",
        WORKFLOW + "?page=1",
        WORKFLOW + "#fragment",
        WORKFLOW + "/",
        WORKFLOW.replace(REPO, "example-org/other"),
        WORKFLOW.replace(REPO, REPO.upper()),
        WORKFLOW.replace("7", "0"),
        WORKFLOW.replace("7", "-1"),
        WORKFLOW.replace("7", "01"),
        WORKFLOW.replace("7", "+1"),
        WORKFLOW.replace("7", "1.0"),
        WORKFLOW.replace("7", "١"),
        WORKFLOW.replace("7", "9223372036854775808"),
        WORKFLOW.replace("7", "9" * 10000),
        WORKFLOW.replace("7", "claude.yml"),
        WORKFLOW.replace("7", "%37"),
        WORKFLOW.replace("/actions/", "/../actions/"),
        WORKFLOW + "\n",
        f"{BASE}/runs/11",
        f"{BASE}/runs/11/jobs",
        f"{BASE}/runs/11/logs",
        f"{BASE}/jobs/21/logs",
        f"{BASE}/artifacts/31/zip",
        f"{BASE}/artifacts/31",
        f"{BASE}/workflows/7/dispatches",
        f"{BASE}/runs/11/rerun",
        f"{BASE}/runs/11/cancel",
        ATTEMPT.replace("/1", "/0"),
        JOBS.replace("/11/", "/01/"),
        "",
        None,
        7,
    ],
)
def test_invalid_routes_are_rejected_before_io(boundary, path):
    with pytest.raises(ValueError) as error:
        GitHubActionsTransport(repository=REPO).get(path, params={}, max_bytes=64)
    assert boundary.calls == []
    assert PRIVATE not in str(error.value) and "provider.example" not in str(error.value)


@pytest.mark.parametrize("params", [{"page": 2, "per_page": 100}, {"page": "2", "per_page": "100"}])
def test_list_routes_accept_only_bounded_pagination(boundary, params):
    boundary.reply()
    GitHubActionsTransport(repository=REPO).get(RUNS, params=params, max_bytes=64)
    assert boundary.calls[0]["url"] == RUNS + "?page=2&per_page=100"
    assert "authorization" not in boundary.calls[0]["headers"]


@pytest.mark.parametrize(
    "params",
    [
        {"page": 0},
        {"page": 101},
        {"page": True},
        {"page": 1.0},
        {"page": "01"},
        {"page": "1&token=" + PRIVATE},
        {"per_page": 101},
        {"per_page": -1},
        {"page": "١"},
        {"per_page": None},
        {"page": "9" * 10000},
        {"name": PRIVATE},
        {"event": "push"},
        {"url": UNTRUSTED},
        {"page": []},
        None,
        [],
    ],
)
def test_invalid_params_are_rejected_before_io(boundary, params):
    with pytest.raises(ValueError) as error:
        GitHubActionsTransport(repository=REPO).get(RUNS, params=params, max_bytes=64)
    assert boundary.calls == [] and PRIVATE not in str(error.value)


@pytest.mark.parametrize("path", [WORKFLOW, ATTEMPT])
def test_non_list_routes_do_not_accept_pagination(boundary, path):
    with pytest.raises(ValueError):
        GitHubActionsTransport(repository=REPO).get(path, params={"page": 1}, max_bytes=64)
    assert boundary.calls == []


@pytest.mark.parametrize("cap", [True, 0, -1, 1.0, "64", None, 1024 * 1024 + 1])
def test_invalid_read_caps_are_rejected_before_io(boundary, cap):
    with pytest.raises(ValueError):
        GitHubActionsTransport(repository=REPO).get(WORKFLOW, params={}, max_bytes=cap)
    assert boundary.calls == []


@pytest.mark.parametrize("timeout", [True, 0, -1, float("nan"), float("inf"), "3", None, 301, 10**1000])
def test_invalid_socket_timeout_is_rejected_before_io(boundary, timeout):
    with pytest.raises(ValueError):
        GitHubActionsTransport(repository=REPO, timeout_seconds=timeout)
    assert boundary.calls == []


@pytest.mark.parametrize("repository", ["", None, "../repo", "example/..", "example/repo.git", UNTRUSTED])
def test_invalid_repository_is_rejected_before_io(boundary, repository):
    with pytest.raises(ValueError) as error:
        GitHubActionsTransport(repository=repository)
    assert boundary.calls == [] and PRIVATE not in str(error.value)


@pytest.mark.parametrize("token", ["", True, b"token", "a\r\nb", "a b", "a\x00b", "é", "a" * 4097])
def test_invalid_bearer_tokens_are_rejected_without_echoing_them(boundary, token):
    with pytest.raises(ValueError) as error:
        GitHubActionsTransport(repository=REPO, token=token)
    assert str(error.value) == "invalid bearer token"
    assert boundary.calls == []


@pytest.mark.parametrize("size", [0, 7, 8, 9, 100000])
@pytest.mark.parametrize("chunked", [False, True])
def test_response_bytes_are_bounded_and_truncation_is_explicit(boundary, size, chunked):
    boundary.reply(b"x" * size, chunked=chunked)
    response = GitHubActionsTransport(repository=REPO).get(WORKFLOW, params={}, max_bytes=8)
    assert response == GetResponse(200, b"x" * min(size, 8), size > 8)
    assert all(type(amount) is int and 0 < amount <= 9 for amount in boundary.reads)
    assert sum(boundary.reads) <= 9
    assert boundary.closed


@pytest.mark.parametrize("advertised", [30, 1000])
def test_short_body_under_longer_content_length_is_not_reported_complete(boundary, advertised):
    # The connection ends after valid JSON but before the advertised length.
    # http.client returns that partial body from read(amt) without an error.
    body = json.dumps({"id": 7}).encode()
    assert len(body) < advertised
    boundary.reply(body, headers={"Content-Length": str(advertised)})
    with pytest.raises(TransportError) as error:
        GitHubActionsTransport(repository=REPO).get(WORKFLOW, params={}, max_bytes=64)
    assert str(error.value) == "metadata transport unavailable"
    assert error.value.__context__ is None and error.value.__cause__ is None
    assert len(boundary.calls) == 1 and boundary.closed


def test_short_chunked_body_is_not_reported_complete(boundary):
    boundary.responses.append(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n40\r\n" + b'{"id": 7}')
    with pytest.raises(TransportError):
        GitHubActionsTransport(repository=REPO).get(WORKFLOW, params={}, max_bytes=64)
    assert len(boundary.calls) == 1 and boundary.closed


def test_content_length_beyond_cap_remains_explicit_truncation(boundary):
    boundary.reply(b"x" * 20, headers={"Content-Length": "1000"})
    response = GitHubActionsTransport(repository=REPO).get(WORKFLOW, params={}, max_bytes=8)
    assert response == GetResponse(200, b"x" * 8, True)


@pytest.mark.parametrize("status", [201, 204, 301, 302, 303, 307, 308, 403, 404, 429, 500])
def test_status_errors_drop_bodies_and_never_follow_redirects(boundary, status):
    boundary.reply(PRIVATE.encode(), status=status, headers={"Location": UNTRUSTED})
    response = GitHubActionsTransport(repository=REPO, token=PRIVATE).get(WORKFLOW, params={}, max_bytes=64)
    assert response == GetResponse(status, b"")
    assert len(boundary.calls) == 1
    assert boundary.reads == [] and boundary.closed
    assert PRIVATE not in repr(response) and "provider.example" not in repr(response)


@pytest.mark.parametrize("location", [WORKFLOW + "/runs", f"https://api.github.com{RUNS}"])
def test_even_same_origin_redirects_are_not_followed(boundary, location):
    boundary.reply(status=302, headers={"Location": location})
    assert GitHubActionsTransport(repository=REPO, token=PRIVATE).get(WORKFLOW, params={}, max_bytes=64) == GetResponse(
        302, b""
    )
    assert len(boundary.calls) == 1


@pytest.mark.parametrize("during_read", [False, True])
@pytest.mark.parametrize("failure", [TimeoutError(UNTRUSTED), OSError(PRIVATE), http.client.HTTPException(UNTRUSTED)])
def test_network_failures_have_safe_errors_without_retry_or_exception_chains(boundary, during_read, failure):
    boundary.reply()
    if during_read:
        boundary.read_failure = failure
    else:
        boundary.failure = failure
    with pytest.raises(TransportError) as error:
        GitHubActionsTransport(repository=REPO, token=PRIVATE).get(WORKFLOW, params={}, max_bytes=64)
    rendered = "".join(traceback.format_exception(error.value))
    assert str(error.value) == "metadata transport unavailable"
    assert PRIVATE not in repr(error.value) and PRIVATE not in rendered
    assert "provider.example" not in rendered
    assert error.value.__context__ is None and error.value.__cause__ is None
    assert len(boundary.calls) == 1
    if during_read:
        assert boundary.closed


def test_transport_does_not_read_tokens_files_proxies_or_global_opener(boundary, monkeypatch):
    boundary.reply()
    monkeypatch.setenv("GH_TOKEN", PRIVATE)
    monkeypatch.setenv("GITHUB_TOKEN", PRIVATE)
    monkeypatch.setenv("HTTPS_PROXY", UNTRUSTED)

    def forbidden(*args, **kwargs):
        raise AssertionError("unexpected ambient IO")

    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(urllib.request, "getproxies", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    response = GitHubActionsTransport(repository=REPO).get(WORKFLOW, params={}, max_bytes=64)
    assert response == GetResponse(200, b"{}")
    assert "authorization" not in boundary.calls[0]["headers"]


def test_raw_metadata_survives_only_until_existing_collectors_sanitize_it(boundary):
    row = {
        "id": 11,
        "run_attempt": 2,
        "workflow_id": 7,
        "repository": {"full_name": REPO},
        "head_sha": HEAD,
        "status": "completed",
        "conclusion": "success",
        "name": PRIVATE,
    }
    boundary.json({"id": 7, "name": PRIVATE, "state": "active"})
    boundary.json({"total_count": 1, "workflow_runs": [row]})
    boundary.json({**row, "run_attempt": 1})
    workflow = ConfirmedWorkflow(REPO, 7, REVISION)
    transport = GitHubActionsTransport(repository=REPO, token=PRIVATE)
    actions = collect_actions(
        transport,
        repository=REPO,
        workflow_id=7,
        current_head_sha=HEAD,
        observed_at=OBSERVED,
        confirmations={(11, 1): workflow, (11, 2): workflow},
    )
    assert actions["complete"] and {r["run_attempt"] for r in actions["runs"]} == {1, 2}
    boundary.json(
        {
            "total_count": 1,
            "jobs": [
                {
                    "id": 21,
                    "run_id": 11,
                    "run_attempt": 1,
                    "head_sha": HEAD,
                    "name": PRIVATE,
                    "runner_name": PRIVATE,
                    "status": "completed",
                }
            ],
        }
    )
    boundary.json(
        {
            "total_count": 1,
            "artifacts": [
                {
                    "id": 31,
                    "workflow_run": {"id": 11, "head_sha": HEAD},
                    "name": PRIVATE,
                    "archive_download_url": UNTRUSTED,
                    "expired": False,
                }
            ],
        }
    )
    metadata = collect_metadata(
        transport,
        run=normalize_run({**row, "run_attempt": 1}, workflow=workflow, observed_at=OBSERVED),
        current_head_sha=HEAD,
        observed_at=OBSERVED,
    )
    assert metadata["complete"] and metadata["jobs"][0]["job_id"] == 21
    assert metadata["artifacts"][0]["artifact_id"] == 31
    rendered = json.dumps([actions, metadata])
    assert PRIVATE not in rendered and "provider.example" not in rendered
    assert [call["url"].split("?")[0] for call in boundary.calls] == [WORKFLOW, RUNS, ATTEMPT, JOBS, ARTIFACTS]


@pytest.mark.parametrize("status,reason", [(403, "access-denied"), (404, "unavailable"), (429, "rate-limited")])
def test_collectors_receive_http_denial_status_without_private_error_bodies(boundary, status, reason):
    boundary.reply(UNTRUSTED.encode(), status=status)
    boundary.reply(UNTRUSTED.encode(), status=status)
    actions = collect_actions(
        GitHubActionsTransport(repository=REPO),
        repository=REPO,
        workflow_id=7,
        current_head_sha=HEAD,
        observed_at=OBSERVED,
    )
    assert not actions["complete"] and reason in actions["incomplete_reasons"]
    assert actions["sources"][0]["http_status"] == status
    assert PRIVATE not in json.dumps(actions)


def test_success_bytes_are_not_rewritten_by_transport(boundary):
    body = json.dumps({"name": PRIVATE, "archive_download_url": UNTRUSTED}).encode()
    boundary.reply(body)
    assert GitHubActionsTransport(repository=REPO).get(WORKFLOW, params={}, max_bytes=1024).body == body


@pytest.mark.parametrize("status", [302, 403, 404, 429])
def test_error_response_cleanup_cannot_expose_network_exception_text(boundary, status):
    boundary.reply(UNTRUSTED.encode(), status=status, headers={"Location": UNTRUSTED})
    boundary.close_failure = OSError(UNTRUSTED)
    response = GitHubActionsTransport(repository=REPO, token=PRIVATE).get(WORKFLOW, params={}, max_bytes=64)
    assert response == GetResponse(status, b"")
    assert boundary.reads == [] and len(boundary.calls) == 1


def test_tls_setup_waits_for_validation_and_its_failures_are_redacted(boundary, monkeypatch):
    setups = []

    def fail_setup(*args, **kwargs):
        setups.append(True)
        raise ssl.SSLError(UNTRUSTED)

    monkeypatch.setattr(ssl, "create_default_context", fail_setup)
    transport = GitHubActionsTransport(repository=REPO, token=PRIVATE)
    with pytest.raises(ValueError):
        transport.get(WORKFLOW, params={}, max_bytes=0)
    assert setups == [] and boundary.calls == []
    with pytest.raises(TransportError) as error:
        transport.get(WORKFLOW, params={}, max_bytes=64)
    assert str(error.value) == "metadata transport unavailable"
    assert PRIVATE not in "".join(traceback.format_exception(error.value))
    assert error.value.__context__ is None and error.value.__cause__ is None
    assert setups == [True] and boundary.calls == []


def test_global_tls_verification_opt_out_cannot_reach_the_request(boundary, monkeypatch):
    # A process-wide opt-out hands out an unverified context to the default HTTPS factory.
    monkeypatch.setattr(ssl, "_create_default_https_context", ssl._create_unverified_context)
    boundary.reply()
    assert GitHubActionsTransport(repository=REPO).get(WORKFLOW, params={}, max_bytes=64).status == 200
    (connection,) = boundary.connections
    context = connection["context"]
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname is True
