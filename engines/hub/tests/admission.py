"""S3c front regressions through one local HTTPS terminator."""
import asyncio
import contextlib
import http.client
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from transport import HOST, front, request


@contextlib.contextmanager
def https_front(upstream):
    with tempfile.TemporaryDirectory() as work:
        key, cert = Path(work)/"key.pem", Path(work)/"cert.pem"
        subprocess.run(["openssl","req","-x509","-newkey","rsa:2048","-nodes","-keyout",str(key),"-out",str(cert),"-days","1","-subj","/CN=example.invalid"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert,key)
        loop = asyncio.new_event_loop()
        ready = threading.Event()
        state = {}

        async def relay(reader, writer):
            try:
                other_reader, other_writer = await asyncio.open_connection(HOST,upstream)
                async def pump(source, target):
                    try:
                        while data := await source.read(65536):
                            target.write(data)
                            await target.drain()
                    finally:
                        target.close()
                await asyncio.gather(pump(reader,other_writer),pump(other_reader,writer))
            except (OSError,ConnectionError):
                writer.close()

        async def start():
            server = await asyncio.start_server(relay,HOST,0,ssl=context,backlog=1024)
            state["server"] = server
            state["port"] = server.sockets[0].getsockname()[1]
            ready.set()
        def run():
            asyncio.set_event_loop(loop)
            loop.run_until_complete(start())
            loop.run_forever()
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            loop.run_until_complete(asyncio.gather(*pending,return_exceptions=True))
            loop.close()
        thread = threading.Thread(target=run)
        thread.start()
        assert ready.wait(5)
        try:
            yield state["port"]
        finally:
            loop.call_soon_threadsafe(state["server"].close)
            loop.call_soon_threadsafe(loop.stop)
            thread.join()


def connect(port):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context.wrap_socket(socket.create_connection((HOST,port),timeout=3),server_hostname="example.invalid")


class Admission(unittest.TestCase):
    def test_every_proxied_route_refuses_before_body_bytes(self):
        from brigade import fleet_hub

        with tempfile.TemporaryDirectory() as work:
            server = fleet_hub.make_server(HOST, 0, Path(work)/"hub.db", "fake-admin-token")
            thread = threading.Thread(target=server.serve_forever,daemon=True)
            thread.start()
            try:
                with front() as (_, public):
                    for method, pattern in server.RequestHandlerClass.route_table.registered():
                        path = pattern.replace("*", "fake")
                        with self.subTest(method=method,path=path):
                            headers = {"Authorization":"Bearer junk", "Content-Length":"1048576", "Content-Type":"application/x-www-form-urlencoded", "Sec-Fetch-Site":"same-origin"}
                            expected = request(server.server_port,path,method,headers=headers,timeout=2)
                            actual = request(public,path,method,headers=headers,timeout=2)
                            self.assertEqual(actual[0],expected[0])
                            self.assertEqual(actual[2],expected[2])
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_slow_junk_enrollments_do_not_delay_node_claim_or_large_event(self):
        import hashlib
        import json
        import sqlite3
        from brigade import fleet_client

        node = "11111111-1111-4111-8111-111111111111"
        token = "fake-token"
        with front(extra=("--trust-tailscale-identity",)) as (child,public), https_front(public) as port, contextlib.ExitStack() as stack:
            db = child.args[child.args.index("--db")+1]
            with sqlite3.connect(db) as conn:
                conn.execute("INSERT INTO nodes(node_id,token_sha256,created_at) VALUES (?,?,?)",(node,hashlib.sha256(token.encode()).hexdigest(),"2026-01-01T00:00:00Z"))
            for i in range(24):
                sock = stack.enter_context(connect(port))
                sock.sendall((f"POST /enroll HTTP/1.1\r\nHost: example.invalid\r\nSec-Fetch-Site: same-origin\r\nAuthorization: Bearer fake-{i}\r\nContent-Length: 6\r\n\r\nc").encode())
            start = time.monotonic()
            decision = fleet_client.acquire_claim("fake-repo",hub_url=f"http://{HOST}:{public}",token=token,node_id=node)
            self.assertTrue(decision.granted)
            self.assertLess(time.monotonic()-start,2.5)
            event = {"node_id":node,"run_id":"fake-run","sequence":0,"digest":"fake-digest","state":"completed","ts":"2026-01-01T00:00:00Z"}
            body = json.dumps(event).encode()
            body += b" "*(8388608-len(body))
            answer = request(public,"/events","POST",body,{"Authorization":f"Bearer {token}","Content-Type":"application/json"},timeout=5)
            self.assertEqual(answer[0],200)
            self.assertEqual(json.loads(answer[2])["accepted"],1)
            with sqlite3.connect(db) as conn:
                self.assertEqual(conn.execute("SELECT count(*) FROM events WHERE run_id='fake-run'").fetchone()[0],1)

    def test_25_requests_one_credential_hold_only_four_proxy_slots(self):
        import hashlib
        import sqlite3
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        release = threading.Event()
        accepted = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                if self.headers.get("Authorization") == "Bearer fake-token":
                    accepted.append(1)
                    release.wait(5)
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")
        server = ThreadingHTTPServer((HOST,0), Handler)
        thread = threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        try:
            with front(server.server_port) as (child,public), contextlib.ExitStack() as stack:
                db = child.args[child.args.index("--db")+1]
                with sqlite3.connect(db) as conn:
                    conn.execute("INSERT INTO nodes(node_id,token_sha256,created_at) VALUES (?,?,?)",("fake-node",hashlib.sha256(b"fake-token").hexdigest(),"2026-01-01T00:00:00Z"))
                sockets = []
                for _ in range(25):
                    sock = stack.enter_context(socket.create_connection((HOST,public),timeout=3))
                    sock.sendall(b"GET /nodes HTTP/1.1\r\nHost: example.invalid\r\nAuthorization: Bearer fake-token\r\n\r\n")
                    sockets.append(sock)
                time.sleep(.5)
                self.assertEqual(len(accepted),4)
                self.assertEqual(request(public,"/nodes",headers={"Authorization":"Bearer fake-admin-token"})[0],200)
                release.set()
                statuses = []
                for sock in sockets:
                    response = http.client.HTTPResponse(sock)
                    response.begin()
                    statuses.append(response.status)
                    response.read()
                self.assertEqual(statuses.count(200),4)
                self.assertEqual(statuses.count(503),21)
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            thread.join()

    def test_300_connections_through_one_https_source(self):
        with front(extra=("--trust-tailscale-identity",)) as (_, public), https_front(public) as port, contextlib.ExitStack() as stack:
            sockets = []
            for _ in range(300):
                sock = stack.enter_context(connect(port))
                sock.sendall(b"GET /health HTTP/1.1\r\nHost: example.invalid\r\n\r\n")
                response = http.client.HTTPResponse(sock)
                response.begin()
                self.assertEqual(response.status,200)
                response.read()
                sockets.append(sock)
            self.assertEqual(len(sockets),300)
            self.assertEqual(request(public,"/nodes",headers={"Authorization":"Bearer fake-admin-token"})[0],200)

    def test_junk_large_bodies_leave_credential_and_browser_capacity(self):
        with front(extra=("--trust-tailscale-identity",)) as (_, public), https_front(public) as port, contextlib.ExitStack() as stack:
            held = []
            for i in range(24):
                sock = stack.enter_context(connect(port))
                sock.sendall((f"POST /enroll HTTP/1.1\r\nHost: example.invalid\r\nSec-Fetch-Site: same-origin\r\nAuthorization: Bearer fake-{i}\r\nContent-Type: application/x-www-form-urlencoded\r\nContent-Length: 8388608\r\n\r\n").encode())
                held.append(sock)
            for sock in held:
                response = http.client.HTTPResponse(sock)
                response.begin()
                self.assertIn(response.status,(413,503))
                response.read()
            start = time.monotonic()
            self.assertEqual(request(public,"/nodes",headers={"Authorization":"Bearer fake-admin-token"})[0],200)
            self.assertLess(time.monotonic()-start,2.5)
            for path in ("/ready","/deck"):
                for _ in range(100):
                    status = request(public,path,headers={"Tailscale-User-Login":"viewer@example.invalid"})[0]
                    if status == 200:
                        break
                    time.sleep(.02)
                self.assertEqual(status,200)
            # Neither token rejection nor enrollment requires a single body byte.
            for path, expected in (("/nodes",401),("/enroll",413)):
                sock = stack.enter_context(connect(port))
                sock.sendall((f"POST {path} HTTP/1.1\r\nHost: example.invalid\r\nSec-Fetch-Site: same-origin\r\nAuthorization: Bearer junk\r\nContent-Type: application/x-www-form-urlencoded\r\nContent-Length: 1048576\r\n\r\n").encode())
                response = http.client.HTTPResponse(sock)
                response.begin()
                self.assertEqual(response.status,expected)
                if expected == 401:
                    self.assertEqual(response.read(),b'{"error": "unauthorized"}')


if __name__ == "__main__":
    unittest.main()
