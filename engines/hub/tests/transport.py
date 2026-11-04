"""Loopback transport regressions against the built front and managed upstream."""
import contextlib
import http.client
import ipaddress
import os
from pathlib import Path
import resource
import sqlite3
import signal
import socket
import subprocess
import tempfile
import threading
import time
import unittest

HOST = str(ipaddress.IPv4Address(2130706433))
BINARY = Path(__file__).resolve().parents[1] / "target" / "debug" / "brigade-hub"
if not BINARY.exists():
    BINARY = BINARY.parents[1] / "release" / "brigade-hub"


def free_port():
    with socket.socket() as sock:
        sock.bind((HOST, 0))
        return sock.getsockname()[1]


@contextlib.contextmanager
def front(upstream=None, extra=(), relative_db=False, env_overrides=None, default_db=False):
    with tempfile.TemporaryDirectory() as work:
        port = free_port()
        command = [str(BINARY), "--host", HOST, "--port", str(port), "--db", "hub.db" if relative_db else f"{work}/hub.db"]
        if default_db:
            command = command[:-2]
        command.extend(extra)
        if upstream:
            command += ["--upstream", f"http://{HOST}:{upstream}"]
        env = dict(os.environ, BRIGADE_FLEET_TOKEN="fake-admin-token")
        for name, value in (env_overrides or {}).items():
            if value is None:
                env.pop(name, None)
            else:
                env[name] = value
        with tempfile.TemporaryFile() as log:
            child = subprocess.Popen(command, env=env, stdout=log, stderr=log, cwd=work if relative_db else None)
            try:
                for _ in range(300):
                    if child.poll() is not None:
                        log.seek(0)
                        raise AssertionError(log.read().decode())
                    try:
                        if request(port, "/health")[0] == 200:
                            break
                    except OSError:
                        pass
                    time.sleep(0.02)
                else:
                    raise AssertionError("front startup timeout")
                yield child, port
            except BaseException:
                log.seek(0)
                print(log.read().decode())
                raise
            finally:
                if child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(5)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait()


DRAIN_SECONDS = 25
SLOW_ENROLL = b"POST /enroll HTTP/1.1\r\nHost: fake\r\nSec-Fetch-Site: same-origin\r\nContent-Type: application/x-www-form-urlencoded\r\nAuthorization: Bearer %s\r\nContent-Length: 6\r\n\r\ncode="


def request(port, path, method="GET", body=None, headers=None, timeout=3):
    conn = http.client.HTTPConnection(HOST, port, timeout=timeout)
    try:
        conn.request(method, path, body, headers or {})
        response = conn.getresponse()
        return response.status, response.getheaders(), response.read()
    finally:
        conn.close()


@contextlib.contextmanager
def _accepted(conn, data, keep):
    with conn:
        yield (conn, data) if keep else conn


def accept_request(listener, prefix, keep=False):
    """Accept the front's request, skipping stray local probes of the port.

    Some hosts probe newly opened loopback ports, so a stub upstream must not
    treat the first accepted connection as the front's.
    """
    while True:
        conn, _ = listener.accept()
        data = conn.recv(4096)
        if data.startswith(prefix):
            return _accepted(conn, data, keep)
        conn.close()


def raw(port, data):
    with socket.create_connection((HOST, port), timeout=3) as sock:
        sock.sendall(data)
        result = b""
        while data := sock.recv(65536):
            result += data
        return result


class Transport(unittest.TestCase):
    def test_python_default_without_home_and_single_checkpoint_owner(self):
        # Give Python's Unix password-database fallback a disposable home. Never
        # open the operator database while testing HOME-unset startup.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sitecustomize.py").write_text(
                "import os, pwd\n"
                "from types import SimpleNamespace\n"
                f"pwd.getpwuid = lambda uid: SimpleNamespace(pw_dir={directory!r})\n"
                "if os.environ.get('BRIGADE_HUB_CHECKPOINT_OWNER') == 'rust':\n"
                " import brigade.fleet_hub_upstream as u\n"
                " def forbidden(*args, **kwargs): raise RuntimeError('duplicate Python checkpointer')\n"
                " u.CheckpointMaintenance = forbidden\n"
            )
            pythonpath = os.pathsep.join([directory, str(Path(__file__).resolve().parents[3] / "src"), os.environ.get("PYTHONPATH", "")])
            with front(default_db=True, env_overrides={"HOME": None, "PYTHONPATH": pythonpath}) as (_, port):
                database = root / ".brigade/fleet-hub.db"
                self.assertTrue(database.exists())
                with sqlite3.connect(database) as writer:
                    writer.execute("PRAGMA wal_autocheckpoint=0")
                    writer.execute("INSERT INTO nodes(node_id,token_sha256,created_at) VALUES ('checkpoint-probe','fake-digest','2026-01-01')")
                    sizes = []
                    for _ in range(8):
                        writer.execute("UPDATE nodes SET label=hex(randomblob(524288)) WHERE node_id='checkpoint-probe'")
                        writer.commit()
                        time.sleep(1.5)
                        sizes.append(Path(str(database) + "-wal").stat().st_size)
                    self.assertLess(max(sizes), 4 * 1024 * 1024, sizes)
                self.assertEqual(request(port, "/ready")[0], 200)

    def test_relative_database_admits_events_and_sessions(self):
        with socket.socket() as listener:
            listener.bind((HOST, 0))
            listener.listen()
            forwarded = []

            def upstream():
                for route in (b"/events", b"/sessions", b"/events", b"/sessions"):
                    with accept_request(listener, b"POST " + route + b" ") as conn:
                        forwarded.append(route)
                        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")

            worker = threading.Thread(target=upstream, daemon=True)
            worker.start()
            with front(listener.getsockname()[1], relative_db=True) as (_, port):
                for route in ("/events", "/sessions"):
                    self.assertEqual(request(port, route, "POST")[0], 200)
                # Exercise the periodic sampler as well as startup admission.
                time.sleep(1.2)
                for route in ("/events", "/sessions"):
                    self.assertEqual(request(port, route, "POST")[0], 200)
            worker.join(3)
            self.assertEqual(forwarded, [b"/events", b"/sessions"] * 2)

    def test_down_and_readiness_recovery(self):
        port = free_port()
        with front(port) as (_, public):
            self.assertEqual(request(public, "/ready")[0], 503)
            answer = request(public, "/nodes")
            self.assertEqual(answer[0], 502)
            self.assertIn(b"upstream-down", answer[2])
            with socket.socket() as listener:
                listener.bind((HOST, port))
                listener.listen()

                def healthy():
                    with accept_request(listener, b"GET /health ") as conn:
                        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")

                worker = threading.Thread(target=healthy)
                worker.start()
                for _ in range(150):
                    if request(public, "/ready")[0] == 200:
                        break
                    time.sleep(0.02)
                self.assertEqual(request(public, "/ready")[0], 200)
                worker.join(3)
            time.sleep(1.2)
            self.assertEqual(request(public, "/ready")[0], 503)

    def test_reset_after_commit_has_no_retry(self):
        with socket.socket() as listener, tempfile.TemporaryDirectory() as work:
            listener.bind((HOST, 0))
            listener.listen()
            commit = Path(work) / "commit"
            attempts = []

            def upstream():
                with accept_request(listener, b"POST /nodes ", keep=True) as (conn, data):
                    while b"\r\n\r\n" not in data:
                        data += conn.recv(4096)
                    head, body = data.split(b"\r\n\r\n", 1)
                    while len(body) < 2:
                        body += conn.recv(2)
                    commit.write_bytes(body)
                    attempts.append(head)
                listener.settimeout(1)
                try:
                    with accept_request(listener, b"POST /nodes "):
                        attempts.append(b"unexpected retry")
                except TimeoutError:
                    return

            worker = threading.Thread(target=upstream)
            worker.start()
            with front(listener.getsockname()[1]) as (_, port):
                response = request(port, "/nodes", "POST", b"{}")
                self.assertEqual(response[0], 502)
                self.assertIn(b"upstream-outcome-unknown", response[2])
                self.assertEqual(commit.read_bytes(), b"{}")
                worker.join(3)
                self.assertEqual(len(attempts), 1)

    def test_framing_targets_and_body_cap(self):
        with front() as (_, port):
            for target, method in [(b"*", b"OPTIONS"), (b"example.invalid:443", b"CONNECT")]:
                response = raw(port, method + b" " + target + b" HTTP/1.1\r\nHost: fake\r\nConnection: close\r\n\r\n")
                self.assertIn(b"400", response.split(b"\r\n")[0])
            for framing in [b"Transfer-Encoding: chunked\r\nContent-Length: 2", b"Content-Length: 2\r\nContent-Length: 3"]:
                response = raw(port, b"POST /nodes HTTP/1.1\r\nHost: fake\r\nConnection: close\r\n" + framing + b"\r\n\r\n{}")
                self.assertIn(b"400", response.split(b"\r\n")[0])
            response = request(port, "/nodes", "POST", headers={"Content-Length": "8388609"})
            self.assertEqual(response[0], 413)
            self.assertIn(b"request body exceeds 8388608 bytes", response[2])

    def test_slow_body_and_loopback_admission(self):
        with front() as (_, port), contextlib.ExitStack() as stack:
            held = []
            for _ in range(9):
                sock = stack.enter_context(socket.create_connection((HOST, port), timeout=3))
                sock.sendall(b"POST /enroll HTTP/1.1\r\nHost: fake\r\nOrigin: http://fake\r\nSec-Fetch-Site: same-origin\r\nContent-Type: application/x-www-form-urlencoded\r\nAuthorization: Bearer fake-admin-token\r\nContent-Length: 6\r\n\r\ncode=")
                held.append(sock)
            time.sleep(0.1)
            self.assertEqual(request(port, "/nodes", headers={"Authorization": "Bearer fake-admin-token"})[0], 200)
            for sock in held:
                sock.sendall(b"x")
                self.assertTrue((reply := sock.recv(4096)).startswith(b"HTTP/1.0 400 Bad Request"), repr(reply))

    def test_proxy_overload_returns_own_503(self):
        # The upstream accepts nothing, so each complete request holds its slot
        # while it waits for response headers.
        with socket.socket() as listener, contextlib.ExitStack() as stack:
            listener.bind((HOST, 0))
            listener.listen(64)
            with front(listener.getsockname()[1]) as (_, port):
                for _ in range(24):
                    sock = stack.enter_context(socket.create_connection((HOST, port), timeout=3))
                    sock.sendall(b"GET /nodes HTTP/1.1\r\nHost: fake\r\nAuthorization: Bearer fake-admin-token\r\n\r\n")
                time.sleep(0.3)
                response = request(port, "/nodes", headers={"Authorization": "Bearer fake-admin-token"})
                self.assertEqual(response[0], 503)
                self.assertIn(("retry-after", "1"), response[1])
                self.assertIn(b"credential-inflight-full", response[2])
                listener.close()

    def test_slow_bodies_with_fake_credentials_leave_proxy_capacity(self):
        # Unvalidated Authorization values are distinct admission keys. Their
        # unfinished bodies must not hold the shared proxied slots.
        with front() as (_, port), contextlib.ExitStack() as stack:
            for i in range(32):
                sock = stack.enter_context(socket.create_connection((HOST, port), timeout=3))
                sock.sendall(SLOW_ENROLL % f"fake-{i}".encode())
            time.sleep(0.3)
            self.assertEqual(request(port, "/nodes", headers={"Authorization": "Bearer fake-admin-token"})[0], 200)

    def test_query_wait_ms_does_not_shorten_response_timeout(self):
        # Python reads wait_ms from the JSON body only.
        with socket.socket() as listener:
            listener.bind((HOST, 0))
            listener.listen()

            def upstream():
                with accept_request(listener, b"GET /messages?wait_ms=0 ") as conn:
                    time.sleep(6)
                    conn.sendall(b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: 2\r\n\r\n{}")

            worker = threading.Thread(target=upstream)
            worker.start()
            with front(listener.getsockname()[1]) as (_, port):
                self.assertEqual(request(port, "/messages?wait_ms=0", timeout=10)[0], 200)
            worker.join(3)

    def test_cookies_and_spoofed_headers(self):
        with socket.socket() as listener:
            listener.bind((HOST, 0))
            listener.listen()
            seen = []

            def upstream():
                with accept_request(listener, b"GET /deck ", keep=True) as (conn, data):
                    seen.append(data.lower())
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nSet-Cookie: a=1\r\nSet-Cookie: b=2\r\n\r\n{}")

            worker = threading.Thread(target=upstream)
            worker.start()
            with front(listener.getsockname()[1]) as (_, port):
                response = request(port, "/deck", headers={"Tailscale-User-Login": "fake", "X-Forwarded-Proto": "https", "X-Brigade-Client-Address": "fake", "Cookie": "fake=1"})
                self.assertEqual([v for k, v in response[1] if k.lower() == "set-cookie"], ["a=1", "b=2"])
            worker.join(3)
            self.assertNotIn(b"tailscale-user-login", seen[0])
            self.assertNotIn(b"x-forwarded-proto", seen[0])
            self.assertNotIn(b"x-brigade-client-address", seen[0])
            self.assertIn(b"cookie: fake=1", seen[0])

    def test_trust_flags_apply_only_on_browser_routes(self):
        with socket.socket() as listener:
            listener.bind((HOST, 0))
            listener.listen()
            seen = []

            def upstream():
                for path in (b"/deck", b"/nodes"):
                    with accept_request(listener, b"GET " + path + b" ", keep=True) as (conn, data):
                        seen.append(data.lower())
                        conn.sendall(b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: 2\r\n\r\n{}")

            worker = threading.Thread(target=upstream)
            worker.start()
            with front(listener.getsockname()[1], ("--trust-tailscale-identity", "--trust-forwarded-proto")) as (_, port):
                for path in ("/deck", "/nodes"):
                    self.assertEqual(request(port, path, headers={"Tailscale-User-Login": "fake", "X-Forwarded-Proto": "https"})[0], 200)
            worker.join(3)
            for name in (b"tailscale-user-login", b"x-forwarded-proto"):
                self.assertIn(name, seen[0])
                self.assertNotIn(name, seen[1])

    def test_body_timeout_before_headers_reach_client(self):
        with socket.socket() as listener:
            listener.bind((HOST, 0))
            listener.listen()
            stop = threading.Event()

            def upstream():
                with accept_request(listener, b"GET /nodes ") as conn:
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n")
                    stop.wait(35)

            worker = threading.Thread(target=upstream)
            worker.start()
            try:
                with front(listener.getsockname()[1]) as (_, port):
                    conn = http.client.HTTPConnection(HOST, port, timeout=34)
                    try:
                        conn.request("GET", "/nodes")
                        response = conn.getresponse()
                        self.assertEqual(response.status, 504)
                        self.assertIn(b"upstream-body-timeout", response.read())
                    finally:
                        conn.close()
            finally:
                stop.set()
                worker.join(3)

    @unittest.skipUnless(Path("/proc").exists(), "child discovery requires procfs")
    def test_managed_child_death(self):
        with front() as (child, _):
            children = Path(f"/proc/{child.pid}/task/{child.pid}/children").read_text().split()
            self.assertEqual(len(children), 1)
            os.kill(int(children[0]), signal.SIGKILL)
            self.assertNotEqual(child.wait(3), 0)

    def test_sigterm_closes_idle_keepalive_connection(self):
        with front() as (child, port):
            conn = http.client.HTTPConnection(HOST, port, timeout=3)
            try:
                conn.request("GET", "/health")
                response = conn.getresponse()
                response.read()
                self.assertEqual(response.status, 200)
                child.send_signal(signal.SIGTERM)
                self.assertEqual(child.wait(5), 0)
            finally:
                conn.close()

    def test_sigterm_drain_is_bounded(self):
        with socket.socket() as listener:
            listener.bind((HOST, 0))
            listener.listen()
            stop = threading.Event()

            def upstream():
                with accept_request(listener, b"GET /nodes ") as conn, contextlib.suppress(OSError):
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\nx")
                    while not stop.wait(2):
                        conn.sendall(b"x")

            worker = threading.Thread(target=upstream)
            worker.start()
            try:
                with front(listener.getsockname()[1]) as (child, port):
                    conn = http.client.HTTPConnection(HOST, port, timeout=3)
                    try:
                        conn.request("GET", "/nodes")
                        self.assertEqual(conn.getresponse().status, 200)
                        child.send_signal(signal.SIGTERM)
                        self.assertEqual(child.wait(DRAIN_SECONDS + 5), 0)
                    finally:
                        conn.close()
            finally:
                stop.set()
                worker.join(5)

    def test_low_descriptor_limit_refuses_startup(self):
        def low_limit():
            resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))

        result = subprocess.run([str(BINARY), "--host", HOST], preexec_fn=low_limit, capture_output=True, timeout=3)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"RLIMIT_NOFILE hard limit 128 below required", result.stderr)


if __name__ == "__main__":
    unittest.main()
