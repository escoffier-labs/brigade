import re
import socket
import threading
import time

import pytest

from brigade.center_cmd.serve import _make_server, serve, validate_bind_security
from brigade.center_cmd.dashboard import render


_FAKE_TOKEN = "fake-token-placeholder"


def _wait_until_ready(server, timeout: float = 5.0) -> None:
    host, port = server.server_address
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("server did not start")


def _raw_request(
    server,
    host_header: str,
    *,
    method: str = "GET",
    path: str = "/",
    auth: str | None = None,
) -> str:
    host, port = server.server_address
    lines = [f"{method} {path} HTTP/1.1", f"Host: {host_header}"]
    if auth is not None:
        lines.append(f"Authorization: Bearer {auth}")
    lines.append("")
    lines.append("")
    request = "\r\n".join(lines).encode("utf-8")
    with socket.create_connection((host, port), timeout=5) as sock:
        sock.sendall(request)
        sock.shutdown(socket.SHUT_WR)
        response = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            response += chunk
    return response.decode("utf-8", errors="replace")


def _status_line(response: str) -> str:
    return response.splitlines()[0]


def _headers_and_body(response: str):
    parts = response.split("\r\n\r\n", 1)
    if len(parts) != 2:
        return parts[0], ""
    return parts[0], parts[1]


@pytest.fixture
def no_token_server(tmp_target):
    server = _make_server(
        host="127.0.0.1",
        port=0,
        token=None,
        allowed_hosts=None,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _wait_until_ready(server)
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def token_server(tmp_target):
    server = _make_server(
        host="127.0.0.1",
        port=0,
        token=_FAKE_TOKEN,
        allowed_hosts=None,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _wait_until_ready(server)
    yield server
    server.shutdown()
    server.server_close()


def _status_code(response: str) -> str:
    return _status_line(response).split()[1]


def test_forged_host_returns_403(no_token_server):
    response = _raw_request(no_token_server, "evil.example.invalid")
    assert _status_code(response) == "403"


def test_good_host_returns_200(no_token_server):
    host, port = no_token_server.server_address
    response = _raw_request(no_token_server, f"{host}:{port}")
    assert _status_code(response) == "200"


def test_host_check_applies_to_unknown_paths(no_token_server):
    host, port = no_token_server.server_address
    bad = _raw_request(no_token_server, "evil.example.invalid", path="/missing")
    good_missing = _raw_request(no_token_server, f"{host}:{port}", path="/missing")
    assert _status_code(bad) == "403"
    assert _status_code(good_missing) == "404"


@pytest.mark.parametrize(
    ("host", "token", "allowed_hosts", "should_raise"),
    [
        ("192.0.2.1", None, None, True),
        ("192.0.2.1", _FAKE_TOKEN, None, True),
        ("192.0.2.1", None, ["dashboard.example.invalid"], True),
        ("192.0.2.1", _FAKE_TOKEN, ["dashboard.example.invalid"], False),
        ("0.0.0.0", _FAKE_TOKEN, ["dashboard.example.invalid"], False),
        ("::", _FAKE_TOKEN, ["dashboard.example.invalid"], False),
        ("127.0.0.1", None, None, False),
        ("localhost", None, None, False),
    ],
)
def test_bind_refusal(host, token, allowed_hosts, should_raise):
    if should_raise:
        with pytest.raises(ValueError):
            validate_bind_security(host, token, allowed_hosts)
    else:
        validate_bind_security(host, token, allowed_hosts)


def test_csp_headers_present_and_nonce_rotates(no_token_server):
    host, port = no_token_server.server_address
    first = _raw_request(no_token_server, f"{host}:{port}")
    second = _raw_request(no_token_server, f"{host}:{port}")

    for response in (first, second):
        headers, _ = _headers_and_body(response)
        assert "Content-Security-Policy:" in headers
        assert "default-src 'none'" in headers
        assert "script-src-attr 'none'" in headers
        assert re.search(r"script-src 'nonce-[^'\s]+'", headers)

    nonce_re = re.compile(r"script-src 'nonce-([^'\s]+)'")
    first_nonce = nonce_re.search(_headers_and_body(first)[0]).group(1)
    second_nonce = nonce_re.search(_headers_and_body(second)[0]).group(1)
    assert first_nonce != second_nonce


def test_html_has_no_inline_event_handlers_and_nonce_matches(no_token_server):
    host, port = no_token_server.server_address
    response = _raw_request(no_token_server, f"{host}:{port}")
    headers, body = _headers_and_body(response)

    assert re.search(r"\bon\w+\s*=", body, re.IGNORECASE) is None

    nonce_match = re.search(r"script-src 'nonce-([^'\s]+)'", headers)
    assert nonce_match is not None
    nonce = nonce_match.group(1)
    assert f'<script nonce="{nonce}"' in body


def test_token_required_for_access(token_server):
    host, port = token_server.server_address
    host_header = f"{host}:{port}"

    missing = _raw_request(token_server, host_header)
    wrong = _raw_request(token_server, host_header, auth="wrong-token")
    correct = _raw_request(token_server, host_header, auth=_FAKE_TOKEN)

    assert _status_code(missing) == "401"
    assert _status_code(wrong) == "401"
    assert _status_code(correct) == "200"


@pytest.mark.parametrize("method", ["POST", "DELETE", "PUT", "PATCH", "OPTIONS", "TRACE"])
def test_unsupported_methods_return_405(no_token_server, method):
    host, port = no_token_server.server_address
    response = _raw_request(no_token_server, f"{host}:{port}", method=method)
    assert _status_code(response) == "405"


@pytest.mark.parametrize("method", ["OPTIONS", "TRACE", "POST"])
def test_host_guard_precedes_method_handling(no_token_server, method):
    """A forged Host is rejected before any verb dispatch, and still gets CSP."""
    response = _raw_request(no_token_server, "evil.example.invalid", method=method)
    headers, _ = _headers_and_body(response)
    assert _status_code(response) == "403"
    assert "default-src 'none'" in headers
    assert "script-src-attr 'none'" in headers


def test_localhost_host_header_is_accepted_on_loopback_bind(no_token_server):
    """The operator may type localhost even though the bind prints 127.0.0.1."""
    _, port = no_token_server.server_address
    response = _raw_request(no_token_server, f"localhost:{port}")
    assert _status_code(response) == "200"


def test_bound_host_does_not_match_a_different_port(no_token_server):
    """Allowlist entries are pinned to the bound port, not bare hostnames."""
    host, port = no_token_server.server_address
    response = _raw_request(no_token_server, f"{host}:{port + 1}")
    assert _status_code(response) == "403"


def test_dashboard_refresh_reloads_nonce_scoped_view_scripts():
    page = render.page("Center", "nonce", "", "", reload_ms=1000)
    refresh = page.split("setInterval(function ()", 1)[1].split("</script>", 1)[0]

    assert "location.reload();" in refresh
    assert "fetch(" not in refresh
    assert "curMain.innerHTML" not in page


def test_missing_host_header_is_rejected(no_token_server):
    host, port = no_token_server.server_address
    request = b"GET / HTTP/1.0\r\n\r\n"
    with socket.create_connection((host, port), timeout=5) as sock:
        sock.sendall(request)
        sock.shutdown(socket.SHUT_WR)
        response = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            response += chunk
    assert _status_code(response.decode("utf-8", errors="replace")) == "403"


def test_serve_refuses_non_loopback_bind_with_clear_error(capsys, tmp_target):
    """The CLI path must print an actionable error, not raise a traceback."""
    exit_code = serve(target=tmp_target, host="192.0.2.1", port=0, token=None, allowed_hosts=None)
    captured = capsys.readouterr()
    assert exit_code == 2
    assert "192.0.2.1" in captured.err
    assert "--token" in captured.err
    assert "--allowed-host" in captured.err
