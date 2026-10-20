"""Capture the Command Deck from a local fixture hub with synthetic data.

Starts a real hub on a free port with a temporary database, seeds three
generic stations, renders ``/deck`` and ``/deck/repos`` over HTTP, freezes the
HTML (the meta refresh is removed so a capture does not reload), serves the
frozen pages with the same nonce CSP the hub sends, and photographs them with
headless Chrome in both color schemes. No live fleet data is involved.

Usage: .venv/bin/python docs/assets/deck-overview-20261018/capture_fixture.py [--out DIR]
"""

from __future__ import annotations

import argparse
import http.client
import json
import re
import shutil
import subprocess
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from brigade import fleet_hub, fleet_hub_policy, fleet_hub_sessions, fleet_policy

HOST = "127.0.0.1"  # content-guard: allow loopback-ipv4
BEARER = "capture-fixture-bearer"
NODE_A = "11111111-1111-4111-8111-111111111111"
NODE_B = "22222222-2222-4222-8222-222222222222"
NODE_C = "33333333-3333-4333-8333-333333333333"
STATIONS = ((NODE_A, "linux-a", 10), (NODE_B, "linux-b", 8), (NODE_C, "win-a", 4))
# Chrome's blink setting: 0 renders the dark scheme, 1 the light scheme.
SCHEMES = {"dark": 0, "light": 1}
SHOTS = (
    ("deck", "deck-1440-dark", 1440, 1500, "dark"),
    ("deck", "deck-1440-light", 1440, 1500, "light"),
    ("deck", "deck-390-dark", 390, 2600, "dark"),
    ("deck", "deck-390-light", 390, 2600, "light"),
    ("deck-repos", "deck-repos-1440-light", 1440, 1100, "light"),
)


def request(port: int, method: str, path: str, body: object = None) -> tuple[int, str]:
    connection = http.client.HTTPConnection(HOST, port, timeout=10)
    headers = {"Authorization": f"Bearer {BEARER}", "Content-Type": "application/json"}
    connection.request(method, path, body=None if body is None else json.dumps(body), headers=headers)
    response = connection.getresponse()
    text = response.read().decode("utf-8")
    connection.close()
    return response.status, text


def seed(port: int, db: Path) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    for node_id, label in ((NODE_A, "linux-a"), (NODE_B, "linux-b"), (NODE_C, "win-a")):
        status, text = request(port, "POST", "/nodes", {"action": "add", "node_id": node_id, "label": label})
        assert status == 200, text
    events = [
        (NODE_A, "run-a1", "example-api", "run.started", 12),
        (NODE_A, "run-a2", "example-web", "run.started", 7),
        (NODE_B, "run-b1", "example-cli", "run.failed", 0),
    ]
    for node_id, run_id, repo, state, minutes in events:
        for sequence, stamp in ((1, now - timedelta(minutes=minutes)), (2, now)):
            event = {
                "node_id": node_id,
                "run_id": run_id,
                "repo": repo,
                "seat": "seat-a",
                "harness": "codex",
                "state": "run.created" if sequence == 1 else state,
                "ts": stamp.isoformat(),
                "sequence": sequence,
                "digest": f"d-{run_id}-{sequence}",
            }
            status, text = request(port, "POST", "/events", [event])
            assert status == 200, text
    conn = fleet_hub.open_db(db)
    try:
        document = {
            "schema": fleet_policy.POLICY_SCHEMA,
            "defaults": {},
            "machines": {
                "linux-a": {"os": "linux", "node_id": NODE_A, "concurrency": 10},
                "linux-b": {"os": "linux", "node_id": NODE_B, "concurrency": 8},
                "win-a": {"os": "windows", "node_id": NODE_C, "concurrency": 4},
            },
            "seats": {"seat-a": {"provider": "provider-a", "model": "model-a-1"}},
            "consumers": {},
            "repositories": {},
        }
        fleet_hub_policy.save_policy(conn, document, expected_version=1, actor="operator", reason="capture fixture")
        telemetry = ((NODE_A, now, 0.44), (NODE_B, now - timedelta(hours=2), 0.12), (NODE_C, now, 0.0))
        for node_id, observed, load in telemetry:
            conn.execute(
                "INSERT INTO fleet_machine_telemetry (node_id, machine, observed_at, ttl_seconds, status, load, "
                "running, active_claims, usable_seats, credential_state, observed_id, updated_at) "
                "VALUES (?, ?, ?, 300, 'available', ?, ?, '[]', '[\"seat-a\"]', 'ok', ?, ?)",
                (
                    node_id,
                    node_id,
                    observed.isoformat(),
                    load,
                    json.dumps({"session_ids": [], "run_ids": []}),
                    f"obs-{node_id[:8]}",
                    observed.isoformat(),
                ),
            )
        conn.commit()
        status, _payload = fleet_hub_sessions.handle_session(
            conn,
            {
                "action": "upsert",
                "node_id": NODE_A,
                "harness": "claude",
                "session_id": "sess-capture",
                "repo_identity": "github.com/example/example-api",
                "identity_scope": "fleet",
                "repo_label": "example-api",
                "checkout_path": "/work/example-api",
                "branch": "main",
                "dirty_paths": ["src/app.py"],
                "dirty_truncated": False,
                "ttl_seconds": 900,
            },
            caller_node=None,
        )
        assert status == 200
    finally:
        conn.close()


def freeze(page: str) -> str:
    return re.sub(r'<meta http-equiv="refresh"[^>]*>', "", page)


def serve(pages: dict[str, str]) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            page = pages.get(self.path.strip("/"))
            if page is None:
                self.send_error(404)
                return
            nonce = re.search(r'<style nonce="([^"]+)"', page).group(1)  # type: ignore[union-attr]
            csp = (
                f"default-src 'none'; script-src 'nonce-{nonce}'; script-src-attr 'none'; "
                f"style-src 'nonce-{nonce}'; img-src 'self'; connect-src 'none'; base-uri 'none'"
            )
            data = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Security-Policy", csp)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer((HOST, 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    chrome = shutil.which("google-chrome") or shutil.which("chromium")
    assert chrome, "google-chrome or chromium is required"
    args.out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as scratch:
        db = Path(scratch) / "hub" / "fleet.db"
        config = Path(scratch) / "deck.json"
        config.write_text(
            json.dumps(
                {"stations": [{"node_id": node, "name": name, "capacity": size} for node, name, size in STATIONS]}
            )
        )
        hub = fleet_hub.make_server(HOST, 0, db, BEARER, allow_admin_writes=True, deck_config_path=config)
        threading.Thread(target=hub.serve_forever, daemon=True).start()
        try:
            port = hub.server_address[1]
            seed(port, db)
            pages = {}
            for key, path in (("deck", "/deck"), ("deck-repos", "/deck/repos")):
                status, text = request(port, "GET", path)
                assert status == 200, (path, status)
                pages[key] = freeze(text)
        finally:
            hub.shutdown()
            hub.server_close()
    frozen = serve(pages)
    try:
        port = frozen.server_address[1]
        for key, name, width, height, scheme in SHOTS:
            target = args.out / f"{name}.png"
            subprocess.run(
                [
                    chrome,
                    "--headless=new",
                    "--no-sandbox",
                    "--disable-gpu",
                    "--hide-scrollbars",
                    f"--blink-settings=preferredColorScheme={SCHEMES[scheme]}",
                    f"--window-size={width},{height}",
                    f"--screenshot={target}",
                    f"http://{HOST}:{port}/{key}",  # content-guard: allow loopback-ipv4
                ],
                check=True,
                capture_output=True,
                timeout=120,
            )
            print(f"wrote {target}")
    finally:
        frozen.shutdown()
        frozen.server_close()


if __name__ == "__main__":
    main()
