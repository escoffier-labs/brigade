"""Loopback transport regressions against the built front and managed upstream."""
import contextlib
import json
import http.client
import ipaddress
import os
from pathlib import Path
import re
import resource
import shlex
import sqlite3
import signal
import socket
import subprocess
import tempfile
import threading
import textwrap
import time
import unittest

HOST = str(ipaddress.IPv4Address(2130706433))
BINARY = Path(__file__).resolve().parents[1] / "target" / "debug" / "brigade-hub"
if not BINARY.exists():
    BINARY = BINARY.parents[1] / "release" / "brigade-hub"
BINARY = Path(os.environ.get("BRIGADE_HUB_BINARY", BINARY))
REAL_PYTHON = os.environ.get("BRIGADE_HUB_PYTHON", "python3")

# Stands in for `python -m brigade fleet serve` and never announces a port, so
# the front stays in its upstream wait. Every other interpreter call (the
# limits re-exec, path resolution, the key) goes to the real Python.
STALLED_UPSTREAM = """
import os, signal, sys, time
from pathlib import Path
state, mode = Path(sys.argv[1]), sys.argv[2]
with open("/proc/self/status", encoding="utf-8") as status:
    blocked = next(line.split()[1] for line in status if line.startswith("SigBlk:"))
(state / "mask").write_text(blocked, encoding="utf-8")
def stop(*_):
    (state / "term").write_text("1", encoding="utf-8")
    sys.exit(0)
signal.signal(signal.SIGTERM, stop if mode == "graceful" else signal.SIG_IGN)
(state / "pid").write_text(str(os.getpid()), encoding="utf-8")
time.sleep(60)
"""


def bound_port(log):
    """The port the front reports binding, or None before it has.

    Tests start the front on `--port 0` and read the assigned port back from
    its "listening on" line, so no port is picked and released before the bind.
    pread leaves the offset alone: the child shares it through the inherited log.
    """
    text = os.pread(log.fileno(), 1 << 20, 0).decode("utf-8", "replace")
    found = re.search(r"^listening on [\d.]+:(\d+)$", text, re.MULTILINE)
    return int(found.group(1)) if found else None


@contextlib.contextmanager
def refused_port():
    """A port nothing accepts on, held bound so no other process can take it."""
    with socket.socket() as held:
        held.bind((HOST, 0))
        yield held.getsockname()[1]


def children(pid):
    """Live children of `pid`, from procfs."""
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        with contextlib.suppress(OSError):
            fields = (entry / "stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
            if int(fields[1]) == pid and fields[0] != "Z":
                found.append(int(entry.name))
    return found


def signal_mask(pid):
    with open(f"/proc/{pid}/status", encoding="utf-8") as status:
        return int(next(line.split()[1] for line in status if line.startswith("SigBlk:")), 16)


def alive(pid):
    """Running, neither gone nor a zombie waiting for its reaper."""
    try:
        state = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()[0]
    except OSError:
        return False
    return state != "Z"


@contextlib.contextmanager
def front(upstream=None, extra=(), relative_db=False, env_overrides=None, default_db=False, planted=False, work_dir=None):
    with tempfile.TemporaryDirectory() as work:
        if planted:
            for module in ("json", "pathlib", "secrets", "resource", "tomllib"):
                (Path(work) / f"{module}.py").write_text("raise RuntimeError('planted module imported')\n")
        command = [str(BINARY), "--host", HOST, "--port", "0", "--db", "hub.db" if relative_db else f"{work}/hub.db"]
        if default_db:
            command = command[:-2]
        command.extend(extra)
        if upstream:
            command += ["--upstream", f"http://{HOST}:{upstream}"]
        env = dict(os.environ, BRIGADE_FLEET_TOKEN="fake-admin-token")
        if planted:
            env["PYTHONPATH"] = work
        for name, value in (env_overrides or {}).items():
            if value is None:
                env.pop(name, None)
            else:
                env[name] = value
        with tempfile.TemporaryFile() as log:
            child = subprocess.Popen(command, env=env, stdout=log, stderr=log, cwd=work_dir or (work if relative_db or planted else None))
            try:
                port = None
                for _ in range(300):
                    if child.poll() is not None:
                        log.seek(0)
                        raise AssertionError(log.read().decode())
                    port = port or bound_port(log)
                    try:
                        if port and request(port, "/health")[0] == 200:
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
        # The test-only interpreter redirects the passwd fallback before the
        # resolver imports Path. Isolated startup never loads sitecustomize.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            forbidden = Path.home() / ".brigade"
            wrapper = root / "python"
            bootstrap = textwrap.dedent(f"""\
                import os, sys
                assert sys.flags.isolated == 1, "missing isolated mode"
                assert os.getcwd() == "/", "unsafe interpreter cwd"
                import pwd, runpy
                from pathlib import Path
                from types import SimpleNamespace
                forbidden = Path({str(forbidden)!r})
                def guard(event, args):
                    single = ("open", "sqlite3.connect", "os.mkdir", "os.makedirs")
                    pairs = ("os.rename", "os.replace", "os.link", "shutil.copyfile", "shutil.copymode",
                             "shutil.copystat", "shutil.copytree", "shutil.move")
                    paths = args[:1] if event in single else args[:2] if event in pairs else ()
                    for value in paths:
                        if isinstance(value, (str, bytes, os.PathLike)):
                            path = Path(os.fsdecode(value)).absolute()
                            if path == forbidden or forbidden in path.parents:
                                raise RuntimeError("real home storage is forbidden")
                sys.addaudithook(guard)
                pwd.getpwuid = lambda uid: SimpleNamespace(pw_dir={directory!r})
                if os.environ.get("BRIGADE_HUB_CHECKPOINT_OWNER") == "rust":
                    import brigade.fleet_hub_upstream as u
                    def forbidden_checkpoint(*args, **kwargs):
                        raise RuntimeError("duplicate Python checkpointer")
                    u.CheckpointMaintenance = forbidden_checkpoint
                mode, payload, *tail = sys.argv[1:]
                sys.argv = [payload if mode == "-m" else "-c", *tail]
                if mode == "-m":
                    runpy.run_module(payload, run_name="__main__", alter_sys=True)
                else:
                    assert mode == "-c", mode
                    exec(compile(payload, "<test interpreter>", "exec"), {{"__name__": "__main__"}})
                """)
            wrapper.write_text(
                "#!/bin/bash\n"
                'test "$1" = "-I" || exit 78\n'
                f'exec {shlex.quote(REAL_PYTHON)} "$1" -c {shlex.quote(bootstrap)} "${{@:2}}"\n'
            )
            wrapper.chmod(0o755)
            # Exercise the audit event without asking SQLite to open a real path.
            for event in ("open", "sqlite3.connect", "os.mkdir", "os.makedirs", "os.rename", "os.replace", "os.link",
                          "shutil.copyfile", "shutil.copymode", "shutil.copystat", "shutil.copytree", "shutil.move"):
                for paths in ((str(forbidden / "fleet-hub.db"), "/tmp/fake"),
                              ("/tmp/fake", str(forbidden / "fleet-hub.db"))) if event not in (
                                  "open", "sqlite3.connect", "os.mkdir", "os.makedirs") else ((str(forbidden),),):
                    probe = subprocess.run(
                        [str(wrapper), "-I", "-c", f"import sys; sys.audit({event!r}, *{paths!r})"],
                        cwd="/", capture_output=True, text=True,
                    )
                    self.assertNotEqual(probe.returncode, 0, event)
                    self.assertIn("real home storage is forbidden", probe.stderr, event)
            with front(default_db=True, planted=True,
                       env_overrides={"HOME": None, "BRIGADE_HUB_PYTHON": str(wrapper)}) as (_, port):
                database = root / ".brigade/fleet-hub.db"
                self.assertNotEqual(database, forbidden / "fleet-hub.db")
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

    def test_relative_environment_deck_config_and_cli_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "deck.json"
            config.write_text(json.dumps({"stations": [{"node_id": "fake-node", "name": "Fake", "capacity": 1}]}))
            key = root / "key.bin"
            key.write_bytes(b"x" * 32)
            interpreter = root / "python"
            interpreter.write_text(
                '#!/bin/sh\n'
                f'test "$BRIGADE_FLEET_FINDINGS_KEY_FILE" = {shlex.quote(str(key))} || exit 78\n'
                f'exec {shlex.quote(REAL_PYTHON)} "$@"\n'
            )
            interpreter.chmod(0o755)
            environment = {"BRIGADE_FLEET_FINDINGS_KEY_FILE": "key.bin", "BRIGADE_HUB_PYTHON": str(interpreter)}
            for upstream in (None, 1):
                with self.subTest(upstream=upstream):
                    with front(upstream, work_dir=directory,
                               env_overrides={**environment, "BRIGADE_FLEET_DECK_CONFIG": "deck.json"}) as (_, port):
                        self.assertEqual(request(port, "/health")[0], 200)
                    with front(upstream, work_dir=directory, extra=("--deck-config", str(config)),
                               env_overrides={**environment, "BRIGADE_FLEET_DECK_CONFIG": "missing.json"}) as (_, port):
                        self.assertEqual(request(port, "/health")[0], 200)

    def test_deleted_cwd_is_cleanly_handled(self):
        for relative in (False, True):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                gone = root / "gone"
                gone.mkdir()
                probe = (
                    "import os,sys; os.chdir(sys.argv[1]); os.rmdir(sys.argv[1]); "
                    "os.execv(sys.argv[2], sys.argv[2:])"
                )
                args = [str(BINARY), "--host", HOST, "--port", "0", "--db",
                        "relative.db" if relative else str(root / "hub.db"),
                        "--upstream", f"http://{HOST}:1"]
                env = dict(os.environ, BRIGADE_FLEET_TOKEN="fake-admin-token")
                env.pop("BRIGADE_FLEET_DECK_CONFIG", None)
                env.pop("BRIGADE_FLEET_FINDINGS_KEY_FILE", None)
                with tempfile.TemporaryFile() as log:
                    child = subprocess.Popen([REAL_PYTHON, "-I", "-c", probe, str(gone), *args],
                                             env=env, stdout=log, stderr=log)
                    try:
                        if relative:
                            self.assertEqual(child.wait(10), 2)
                        else:
                            for _ in range(300):
                                if bound_port(log) or child.poll() is not None:
                                    break
                                time.sleep(.02)
                            self.assertIsNone(child.poll())
                            self.assertIsNotNone(bound_port(log))
                    finally:
                        if child.poll() is None:
                            child.terminate()
                            child.wait(5)
                    log.seek(0)
                    output = log.read().decode()
                    self.assertNotIn("panicked", output)
                    if relative:
                        self.assertIn("error: working directory is unavailable", output)

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
        # Bound up front and listening only later: connects are refused until
        # then, and no other process can take the port in between.
        with socket.socket() as listener:
            listener.bind((HOST, 0))
            port = listener.getsockname()[1]
            with front(port) as (_, public):
                self.assertEqual(request(public, "/ready")[0], 503)
                answer = request(public, "/nodes")
                self.assertEqual(answer[0], 502)
                self.assertIn(b"upstream-down", answer[2])
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
                listener.close()
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

    def test_python_children_ignore_working_directory(self):
        # Covers limits re-exec, database lookup, key generation and upstream.
        with front(relative_db=True, planted=True) as (_, port):
            self.assertEqual(request(port, "/health")[0], 200)
        with tempfile.NamedTemporaryFile(mode="w") as clock:
            clock.write(str(time.time()))
            clock.flush()
            with front(relative_db=True, planted=True, extra=("--test-clock-file", clock.name)) as (_, port):
                self.assertEqual(request(port, "/health")[0], 200)
        # External validation has a separate launch path.
        with refused_port() as port:
            with front(port, relative_db=True, planted=True) as (_, front_port):
                self.assertEqual(request(front_port, "/health")[0], 200)

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

    def stop_during_upstream_startup(self, mode):
        """SIGTERM the front while its managed upstream has not announced a port."""
        with tempfile.TemporaryDirectory() as work, tempfile.TemporaryFile() as log:
            state = Path(work)
            fake = state / "python"
            fake.write_text(
                "#!/bin/sh\n"
                'if [ "$1" = "-I" ] && [ "$2" = "-m" ] && [ "$3" = "brigade" ]; then\n'
                f'  exec "{REAL_PYTHON}" -c \'{STALLED_UPSTREAM}\' "{state}" {mode}\n'
                "fi\n"
                f'exec "{REAL_PYTHON}" "$@"\n',
                encoding="utf-8",
            )
            fake.chmod(0o755)
            env = dict(os.environ, BRIGADE_FLEET_TOKEN="fake-admin-token", BRIGADE_HUB_PYTHON=str(fake))
            command = [str(BINARY), "--host", HOST, "--port", "0", "--db", f"{work}/hub.db"]
            child = subprocess.Popen(command, env=env, stdout=log, stderr=log)
            try:
                for _ in range(500):
                    if (state / "pid").exists() or child.poll() is not None:
                        break
                    time.sleep(0.02)
                self.assertIsNone(child.poll(), "front exited before its upstream started")
                upstream = int((state / "pid").read_text(encoding="utf-8"))
                started = time.monotonic()
                child.send_signal(signal.SIGTERM)
                code = child.wait(20)
                elapsed = time.monotonic() - started
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait()
            log.seek(0)
            output = log.read().decode()
            # Reaped by the front, not orphaned: no such process remains.
            with self.assertRaises(ProcessLookupError):
                os.kill(upstream, 0)
            # The front's stop-signal block never reaches the child.
            self.assertEqual(int((state / "mask").read_text(encoding="utf-8"), 16), 0)
            # Non-zero, and an exit, not death by the signal's default action.
            self.assertEqual(code, 2, output)
            self.assertIn("stop signal during startup", output)
            return elapsed, output, (state / "term").exists()

    @unittest.skipUnless(Path("/proc").exists(), "the stalled upstream reads procfs")
    def test_sigterm_during_upstream_startup_stops_the_child_gracefully(self):
        elapsed, output, termed = self.stop_during_upstream_startup("graceful")
        self.assertTrue(termed, "the upstream got SIGTERM, not SIGKILL")
        self.assertIn("managed upstream stopped cleanly after SIGTERM", output)
        self.assertLess(elapsed, 5)

    @unittest.skipUnless(Path("/proc").exists(), "the stalled upstream reads procfs")
    def test_sigterm_during_upstream_startup_kills_a_child_that_ignores_it(self):
        elapsed, output, _ = self.stop_during_upstream_startup("ignore")
        self.assertIn("managed upstream still running 10 s after SIGTERM; killing it", output)
        self.assertGreaterEqual(elapsed, 9)
        self.assertLess(elapsed, 15)

    def test_sigterm_right_after_launch_exits_without_serving(self):
        # Lands in the limits re-exec or an early startup step, while the
        # signals are blocked or before the accept loop exists.
        with tempfile.TemporaryDirectory() as work, tempfile.TemporaryFile() as log:
            env = dict(os.environ, BRIGADE_FLEET_TOKEN="fake-admin-token")
            command = [str(BINARY), "--host", HOST, "--port", "0", "--db", f"{work}/hub.db"]
            child = subprocess.Popen(command, env=env, stdout=log, stderr=log)
            time.sleep(0.1)
            child.send_signal(signal.SIGTERM)
            try:
                code = child.wait(20)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait()
            log.seek(0)
            output = log.read().decode()
            self.assertEqual(code, 2, output)
            self.assertIn("stop signal during startup", output)

    def test_managed_sigterm_stops_the_upstream_with_sigterm_and_exits_zero(self):
        with tempfile.TemporaryDirectory() as work, tempfile.TemporaryFile() as log:
            env = dict(os.environ, BRIGADE_FLEET_TOKEN="fake-admin-token")
            command = [str(BINARY), "--host", HOST, "--port", "0", "--db", f"{work}/hub.db"]
            child = subprocess.Popen(command, env=env, stdout=log, stderr=log)
            try:
                port = None
                for _ in range(300):
                    port = port or bound_port(log)
                    with contextlib.suppress(OSError):
                        if port and request(port, "/health")[0] == 200:
                            break
                    time.sleep(0.05)
                child.send_signal(signal.SIGTERM)
                code = child.wait(DRAIN_SECONDS + 15)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait()
            log.seek(0)
            output = log.read().decode()
            self.assertEqual(code, 0, output)
            self.assertIn("managed upstream stopped cleanly after SIGTERM", output)

    def managed(self, log, work, **popen):
        """Start a managed front and wait until it answers /health."""
        env = dict(os.environ, BRIGADE_FLEET_TOKEN="fake-admin-token")
        command = [str(BINARY), "--host", HOST, "--port", "0", "--db", f"{work}/hub.db"]
        child = subprocess.Popen(command, env=env, stdout=log, stderr=log, **popen)
        port = None
        for _ in range(300):
            port = port or bound_port(log)
            with contextlib.suppress(OSError):
                if port and request(port, "/health")[0] == 200:
                    return child
            time.sleep(0.05)
        child.kill()
        child.wait()
        self.fail("front never answered /health")

    @unittest.skipUnless(Path("/proc").exists(), "reads process groups and masks from procfs")
    def test_group_signal_stops_the_upstream_once_and_exits_zero(self):
        for signum in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=signum.name), tempfile.TemporaryDirectory() as work, tempfile.TemporaryFile() as log:
                # Its own session, so the group signal stays inside this test.
                child = self.managed(log, work, start_new_session=True)
                try:
                    (upstream,) = children(child.pid)
                    # Only brigade-hub shares the group, so it alone gets the
                    # group signal and stops the upstream with one SIGTERM.
                    self.assertEqual(os.getpgid(upstream), upstream)
                    self.assertEqual(os.getpgid(child.pid), child.pid)
                    self.assertEqual(signal_mask(upstream), 0)
                    os.killpg(child.pid, signum)
                    code = child.wait(DRAIN_SECONDS + 15)
                finally:
                    if child.poll() is None:
                        child.kill()
                        child.wait()
                log.seek(0)
                output = log.read().decode()
                self.assertEqual(code, 0, output)
                self.assertIn("managed upstream stopped cleanly after SIGTERM", output)

    @unittest.skipUnless(Path("/proc").exists(), "reads the process table from procfs")
    def test_bind_conflict_after_upstream_start_stops_it_with_sigterm(self):
        with socket.socket() as taken, tempfile.TemporaryDirectory() as work, tempfile.TemporaryFile() as log:
            taken.bind((HOST, 0))
            taken.listen()
            env = dict(os.environ, BRIGADE_FLEET_TOKEN="fake-admin-token")
            port = str(taken.getsockname()[1])
            command = [str(BINARY), "--host", HOST, "--port", port, "--db", f"{work}/hub.db"]
            child = subprocess.Popen(command, env=env, stdout=log, stderr=log)
            try:
                code = child.wait(60)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait()
            log.seek(0)
            output = log.read().decode()
            self.assertEqual(code, 2, output)
            self.assertIn("Address already in use", output)
            # The ready upstream ran its own shutdown and exited 0, not SIGKILL.
            self.assertIn("managed upstream stopped cleanly after SIGTERM", output)
            self.assertNotIn("listening on", output.replace("brigade fleet hub listening on", ""))

    @unittest.skipUnless(Path("/proc").exists(), "reads the process table from procfs")
    def test_upstream_stops_when_the_front_is_killed(self):
        with tempfile.TemporaryDirectory() as work, tempfile.TemporaryFile() as log:
            child = self.managed(log, work)
            (upstream,) = children(child.pid)
            child.kill()
            child.wait()
            # Its own process group, so the death signal is what ends it.
            for _ in range(200):
                if not alive(upstream):
                    break
                time.sleep(0.05)
            else:
                os.kill(upstream, signal.SIGKILL)
                self.fail("upstream outlived a killed front")

    def test_low_descriptor_limit_refuses_startup(self):
        def low_limit():
            resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))

        result = subprocess.run([str(BINARY), "--host", HOST], preexec_fn=low_limit, capture_output=True, timeout=3)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"RLIMIT_NOFILE hard limit 128 below required", result.stderr)


if __name__ == "__main__":
    unittest.main()
