"""Build and exercise the Fleet Hub OCI image using a local Docker daemon."""

from __future__ import annotations

import argparse
import http.client
import json
import pathlib
import secrets
import ssl
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


ROOT = pathlib.Path(__file__).resolve().parents[1]
IMAGE = "brigade-fleet-hub:issue-1635-test"
BRIGADE_REF = "e6009f6969dc6e45db2ab93790aa3e407c791409"

PROXY = """
import http.client
import ssl
from http.server import BaseHTTPRequestHandler, HTTPServer

class Proxy(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        self.forward()

    def do_POST(self):
        self.forward()

    def forward(self):
        length = int(self.headers.get('Content-Length', '0'))
        body = self.rfile.read(length) if length else None
        excluded = {'connection', 'content-length', 'host', 'transfer-encoding'}
        headers = {
            key: value for key, value in self.headers.items()
            if key.lower() not in excluded and not key.lower().startswith('x-forwarded-')
        }
        headers['X-Forwarded-Proto'] = 'https'
        headers['Host'] = self.headers['Host']
        upstream = http.client.HTTPConnection('127.0.0.1', 3774, timeout=10)
        upstream.request(self.command, self.path, body=body, headers=headers)
        response = upstream.getresponse()
        payload = response.read()
        self.send_response(response.status)
        for key, value in response.getheaders():
            if key.lower() not in excluded:
                self.send_header(key, value)
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        upstream.close()

server = HTTPServer(('0.0.0.0', 8443), Proxy)
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain('/tmp/proxy.crt', '/tmp/proxy.key')
server.socket = context.wrap_socket(server.socket, server_side=True)
server.serve_forever()
"""


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["docker", *args], check=False, capture_output=True, text=True, cwd=ROOT)
    if check and result.returncode:
        details = "\n".join(part for part in (result.stdout.strip(), result.stderr.strip()) if part)
        raise RuntimeError(f"docker {args[0]} failed ({result.returncode}): {details}")
    return result


def request(
    url: str,
    path: str,
    token: str | None = None,
    payload: object | None = None,
    *,
    context: ssl.SSLContext | None = None,
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, dict, bytes]:
    headers = dict(extra_headers or {})
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode()
    req = urllib.request.Request(url + path, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=3, context=context) as response:
            return response.status, dict(response.headers.items()), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers.items()), error.read()


def run_hub(
    name: str,
    volume: str,
    token_file: pathlib.Path,
    *,
    publish_port: int,
    host: str = "0.0.0.0",
    trust_forwarded_proto: bool = False,
    platform: str | None = None,
) -> str:
    docker(
        "run",
        "--detach",
        "--name",
        name,
        *(["--platform", platform] if platform else []),
        "--publish",
        f"127.0.0.1::{publish_port}",
        "--mount",
        f"type=bind,src={token_file},dst=/run/secrets/admin_token,readonly",
        "--mount",
        f"type=volume,src={volume},dst=/var/lib/brigade",
        IMAGE,
        "--host",
        host,
        "--port",
        "3774",
        "--db",
        "/var/lib/brigade/fleet-hub.db",
        "--token-file",
        "/run/secrets/admin_token",
        *(["--trust-forwarded-proto"] if trust_forwarded_proto else []),
    )
    return docker("port", name, f"{publish_port}/tcp").stdout.strip().rsplit(":", 1)[1]


def wait_for_health(url: str, *, context: ssl.SSLContext | None = None) -> None:
    deadline = time.monotonic() + 20
    last: object = None
    while time.monotonic() < deadline:
        try:
            status, _, _ = request(url, "/health", context=context)
            if status == 200:
                return
            last = status
        except (OSError, urllib.error.URLError) as error:
            last = error
        time.sleep(0.25)
    raise RuntimeError(f"Fleet Hub did not become healthy: {last!r}")


def build_image(platform: str | None) -> None:
    command = ["buildx", "build", "--load"] if platform else ["build"]
    docker(
        *command,
        "--pull",
        *(["--platform", platform] if platform else []),
        "--file",
        "ops/fleet-hub/Containerfile",
        "--build-arg",
        f"BRIGADE_REF={BRIGADE_REF}",
        "--tag",
        IMAGE,
        ".",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", help="build and run this OCI platform with Docker Buildx")
    args = parser.parse_args()
    build_image(args.platform)

    containers = [f"brigade-fleet-test-{uuid.uuid4().hex[:10]}" for _ in range(4)]
    volumes = [f"brigade-fleet-test-{uuid.uuid4().hex[:10]}" for _ in range(3)]
    with tempfile.TemporaryDirectory(prefix="brigade-fleet-test-") as secret_dir:
        token_file = pathlib.Path(secret_dir, "admin-token.txt")
        token = secrets.token_urlsafe(32)
        token_file.write_text(token + "\n", encoding="utf-8")
        token_file.chmod(0o444)
        for volume in volumes:
            docker("volume", "create", volume)
        try:
            port = run_hub(containers[0], volumes[0], token_file, publish_port=3774, platform=args.platform)
            url = f"http://127.0.0.1:{port}"
            wait_for_health(url)
            runtime = docker(
                "exec",
                containers[0],
                "python",
                "-c",
                "import json,platform,sqlite3,ssl,sys; print(json.dumps({'os': platform.freedesktop_os_release()['PRETTY_NAME'], 'architecture': platform.machine(), 'python': sys.version.split()[0], 'openssl': ssl.OPENSSL_VERSION, 'sqlite': sqlite3.sqlite_version}))",
            )
            print(f"container runtime: {runtime.stdout.strip()}")
            docker(
                "exec",
                containers[0],
                "python",
                "-c",
                "import ssl; assert ssl.OPENSSL_VERSION_INFO >= (3, 0, 0, 20, 0), ssl.OPENSSL_VERSION",
            )
            assert docker("exec", containers[0], "id", "-u").stdout.strip() == "10001"
            assert request(url, "/nodes", token)[0] == 200

            node_id = "container-test-node"
            status, _, enrolled = request(url, "/nodes", token, {"action": "add", "node_id": node_id})
            node_token = json.loads(enrolled)["token"]
            assert status == 200
            event = {
                "node_id": node_id,
                "run_id": "container-smoke",
                "repo": "test/repo",
                "seat": "test",
                "harness": "codex",
                "state": "run.started",
                "ts": "2026-10-13T00:00:00+00:00",
                "sequence": 1,
                "digest": "container-smoke-1",
            }
            assert request(url, "/events", node_token, [event])[0] == 200

            docker("kill", containers[0])
            docker("rm", containers[0])
            port = run_hub(containers[0], volumes[0], token_file, publish_port=3774, platform=args.platform)
            url = f"http://127.0.0.1:{port}"
            wait_for_health(url)
            status, _, nodes = request(url, "/nodes", token)
            assert status == 200 and any(node["node_id"] == node_id for node in json.loads(nodes)["nodes"])
            status, _, runs = request(url, "/status", token)
            assert status == 200 and any(run["run_id"] == "container-smoke" for run in json.loads(runs)["runs"])

            docker(
                "run",
                "--rm",
                "--user",
                "10001:10001",
                "--mount",
                f"type=volume,src={volumes[0]},dst=/source",
                "--mount",
                f"type=volume,src={volumes[1]},dst=/var/lib/brigade",
                "--entrypoint",
                "python",
                IMAGE,
                "-c",
                "import sqlite3; source=sqlite3.connect('/source/fleet-hub.db'); target=sqlite3.connect('/var/lib/brigade/fleet-hub.db.backup'); source.backup(target); target.close(); source.close()",
            )
            empty_port = run_hub(containers[1], volumes[2], token_file, publish_port=3774, platform=args.platform)
            wait_for_health(f"http://127.0.0.1:{empty_port}")
            docker("stop", containers[1])
            docker("rm", containers[1])
            docker(
                "run",
                "--rm",
                "--user",
                "10001:10001",
                "--mount",
                f"type=volume,src={volumes[1]},dst=/source,readonly",
                "--mount",
                f"type=volume,src={volumes[2]},dst=/var/lib/brigade",
                "--entrypoint",
                "python",
                IMAGE,
                "-c",
                "import os,shutil; source='/source/fleet-hub.db.backup'; target='/var/lib/brigade/fleet-hub.db'; assert os.path.getsize(source)>0; [os.unlink(target+suffix) for suffix in ('','-wal','-shm') if os.path.exists(target+suffix)]; shutil.copyfile(source,target)",
            )
            restored_port = run_hub(containers[1], volumes[2], token_file, publish_port=3774, platform=args.platform)
            restored_url = f"http://127.0.0.1:{restored_port}"
            wait_for_health(restored_url)
            status, _, runs = request(restored_url, "/status", token)
            assert status == 200 and any(run["run_id"] == "container-smoke" for run in json.loads(runs)["runs"])

            openssl = subprocess.run(
                [
                    "openssl",
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-nodes",
                    "-keyout",
                    str(pathlib.Path(secret_dir, "proxy.key")),
                    "-out",
                    str(pathlib.Path(secret_dir, "proxy.crt")),
                    "-days",
                    "1",
                    "-subj",
                    "/CN=127.0.0.1",
                    "-addext",
                    "subjectAltName=IP:127.0.0.1",
                ],
                check=True,
                capture_output=True,
                cwd=ROOT,
            )
            del openssl
            certificate = pathlib.Path(secret_dir, "proxy.crt")
            private_key = pathlib.Path(secret_dir, "proxy.key")
            certificate.chmod(0o444)
            private_key.chmod(0o444)
            docker("rm", "--force", containers[0])
            https_port = run_hub(
                containers[0],
                volumes[0],
                token_file,
                publish_port=8443,
                host="127.0.0.1",
                trust_forwarded_proto=True,
                platform=args.platform,
            )
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                ready = docker(
                    "exec",
                    containers[0],
                    "python",
                    "-c",
                    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:3774/health',timeout=1)",
                    check=False,
                )
                if ready.returncode == 0:
                    break
                time.sleep(0.25)
            else:
                logs = docker("logs", containers[0], check=False)
                raise RuntimeError(f"loopback-bound Fleet Hub did not become healthy: {logs.stderr}{logs.stdout}")
            proxy_name = containers[2]
            docker(
                "run",
                "--detach",
                "--name",
                proxy_name,
                "--network",
                f"container:{containers[0]}",
                *(["--platform", args.platform] if args.platform else []),
                "--mount",
                f"type=bind,src={certificate},dst=/tmp/proxy.crt,readonly",
                "--mount",
                f"type=bind,src={private_key},dst=/tmp/proxy.key,readonly",
                "--entrypoint",
                "python",
                IMAGE,
                "-c",
                PROXY,
            )
            https_url = f"https://127.0.0.1:{https_port}"
            tls = ssl._create_unverified_context()
            try:
                wait_for_health(https_url, context=tls)
            except RuntimeError as error:
                logs = docker("logs", proxy_name, check=False)
                raise RuntimeError(f"HTTPS proxy did not become healthy: {logs.stderr}{logs.stdout}") from error
            status, _, enrollment = request(
                https_url,
                "/dashboard/enrollment",
                token,
                {},
                context=tls,
                extra_headers={"X-Forwarded-Proto": "http"},
            )
            code = json.loads(enrollment)["code"]
            assert status == 201
            origin = https_url
            body = urllib.parse.urlencode({"code": code}).encode()
            connection = http.client.HTTPSConnection("127.0.0.1", int(https_port), context=tls, timeout=5)
            connection.request(
                "POST",
                "/enroll",
                body=body,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": origin,
                    "Sec-Fetch-Site": "same-origin",
                    "X-Forwarded-Proto": "http",
                },
            )
            response = connection.getresponse()
            cookie = response.getheader("Set-Cookie", "")
            assert response.status == 303 and "; Secure" in cookie
            connection.close()
            print(
                "Fleet Hub build, non-root secret read, node authentication, event retention through crash, online backup/restore, and loopback HTTPS proxy enrollment: passed"
            )
        finally:
            for container in containers:
                docker("rm", "--force", container, check=False)
            for volume in volumes:
                docker("volume", "rm", volume, check=False)


if __name__ == "__main__":
    main()
