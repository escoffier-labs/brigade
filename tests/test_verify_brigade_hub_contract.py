"""Keep the verify-brigade Fleet Hub contract drive honest.

`control-brigade.py hub-contract` starts a hub, drives it over HTTP only, and
compares the answers to the recorded wire contract. It is the acceptance suite
a hub rewrite must pass, so these tests assert on what the helper reports and
on the inventory in `docs/phase-hub-contract.md`. The parity run launches the
in-repo hub once; the drift run launches one deliberately wrong stand-in, so
the suite cannot pass vacuously.
"""

from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path

from tests.test_verify_brigade_skill import REPO_ROOT, SKILL_DIR, control

INVENTORY = REPO_ROOT / "docs" / "phase-hub-contract.md"
FEATURE = SKILL_DIR / "features" / "hub-contract.md"
ROW = re.compile(r"^\| (GET|POST|PUT|PATCH|DELETE) \| (\S+) \|.*\| ([^|]+) \|\s*$")


def inventory_rows() -> list[tuple[str, str, str]]:
    rows = []
    for line in INVENTORY.read_text(encoding="utf-8").splitlines():
        found = ROW.match(line)
        if found:
            rows.append((found.group(1), found.group(2), found.group(3).strip()))
    return rows


def covered_in_inventory() -> list[str]:
    return sorted(f"{method} {path}" for method, path, status in inventory_rows() if status.startswith("covered"))


# A stand-in hub: the real hub in-process behind a proxy that drifts three ways
# a rewrite could. /health names another service, an unauthenticated /status
# answers 403 instead of 401, and message pages lose `next_cursor`. Everything
# else passes through untouched, so only those cases may be reported.
DRIFTING_HUB = """\
import json, sys, threading, urllib.error, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from brigade import fleet_hub

host, port, db, token_file = sys.argv[1:5]
token = Path(token_file).read_text(encoding="utf-8").strip()
inner = fleet_hub.make_server(host, 0, Path(db), token)
threading.Thread(target=inner.serve_forever, daemon=True).start()
base = "http://%s:%d" % (host, inner.server_address[1])


class Proxy(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def forward(self):
        length = int(self.headers.get("Content-Length") or 0)
        data = self.rfile.read(length) if length else None
        headers = {k: v for k, v in self.headers.items() if k.lower() in ("authorization", "content-type")}
        request = urllib.request.Request(base + self.path, data=data, headers=headers, method=self.command)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                status, ctype, body = response.status, response.headers.get("Content-Type", ""), response.read()
        except urllib.error.HTTPError as exc:
            status, ctype, body = exc.code, exc.headers.get("Content-Type", ""), exc.read()
        path = self.path.partition("?")[0]
        if path == "/health":
            body = json.dumps({"ok": True, "service": "another-hub"}).encode()
        if path == "/status" and "Authorization" not in self.headers:
            status = 403
        if path == "/messages" and status == 200 and data and json.loads(data).get("action") == "list":
            page = json.loads(body)
            page.pop("next_cursor", None)
            body = json.dumps(page).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = forward


ThreadingHTTPServer((host, int(port)), Proxy).serve_forever()
"""

DRIFTED_CASES = {
    "health",
    "status without a token",
    "messages list",
    "messages list after a cursor",
    "messages list past the end",
    "messages list unacked",
}


def test_hub_contract_holds_on_the_in_repo_hub(tmp_path):
    """One launched hub: parity, coverage against the inventory, private state, and cleanup."""
    root = tmp_path / "state"
    code, payload = control("hub-contract", root=root)
    assert code == 0, payload
    assert payload["ok"] is True
    assert payload["mismatches"] == []
    assert payload["contract_version"] == 1
    assert payload["cases_failed"] == 0
    assert payload["cases_total"] == len(payload["cases"]) >= 70
    assert all(case["ok"] for case in payload["cases"])

    # The drive, the module constant, and the inventory name the same routes.
    assert payload["covered_routes"] == covered_in_inventory()
    code, dry = control("hub-contract", "--dry-run", root=root)
    assert code == 0, dry
    assert dry["covered_routes"] == payload["covered_routes"]

    hub = payload["hub"]
    assert hub["mode"] == "launched"
    assert hub["argv"][-10:-8] == ["fleet", "serve"], hub["argv"]
    state_dir = Path(hub["state_dir"])
    assert state_dir.parent == (root / "targets").resolve()
    token_file = state_dir / "admin-token"
    assert token_file.is_file()
    if sys.platform != "win32":
        assert token_file.stat().st_mode & 0o777 == 0o600

    # The capture keeps every exchange but no bearer material.
    capture = Path(payload["capture"]).read_text(encoding="utf-8")
    assert token_file.read_text(encoding="utf-8").strip() not in capture
    assert '"[redacted]"' in capture

    code, removed = control("cleanup", root=root)
    assert code == 0, removed
    assert not state_dir.exists()


def test_hub_contract_reports_a_drifting_hub(tmp_path):
    root = tmp_path / "state"
    script = tmp_path / "drifting_hub.py"
    script.write_text(DRIFTING_HUB, encoding="utf-8")
    template = " ".join([shlex.quote(sys.executable), shlex.quote(str(script)), "{host} {port} {db} {token_file}"])

    code, payload = control("hub-contract", "--hub-cmd", template, root=root)
    assert code == 3, payload
    assert payload["ok"] is False
    assert payload["hub"]["mode"] == "launched"
    reported = {item["case"] for item in payload["mismatches"]}
    assert reported == DRIFTED_CASES, payload["mismatches"]
    by_case = {item["case"]: item for item in payload["mismatches"]}
    assert by_case["health"]["path"] == "$.service"
    assert by_case["status without a token"] == {
        "case": "status without a token",
        "check": "status",
        "expected": 401,
        "observed": 403,
    }
    paths = {item.get("path") for item in payload["mismatches"] if item["case"] == "messages list"}
    assert "$.next_cursor" in paths
    failed = {case["case"] for case in payload["cases"] if not case["ok"]}
    assert failed == DRIFTED_CASES


# A stand-in that answers every request `200 [1]`: healthy enough to start,
# wrong everywhere else. The drive must report every case, not crash.
JUNK_HUB = """\
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Junk(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def answer(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "3")
        self.end_headers()
        self.wfile.write(b"[1]")

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = answer


ThreadingHTTPServer((sys.argv[1], int(sys.argv[2])), Junk).serve_forever()
"""


def test_hub_contract_reports_every_case_on_a_junk_hub(tmp_path):
    root = tmp_path / "state"
    script = tmp_path / "junk_hub.py"
    script.write_text(JUNK_HUB, encoding="utf-8")
    template = " ".join([shlex.quote(sys.executable), shlex.quote(str(script)), "{host} {port}"])

    code, payload = control("hub-contract", "--hub-cmd", template, root=root)
    assert code == 3, payload
    assert payload["ok"] is False
    assert payload["cases_total"] >= 70
    assert payload["cases_failed"] == payload["cases_total"]


def test_hub_contract_reports_a_hub_that_never_starts(tmp_path):
    root = tmp_path / "state"
    template = " ".join([shlex.quote(sys.executable), "-c", shlex.quote("import sys; sys.exit(4)"), "{port}"])

    code, payload = control("hub-contract", "--hub-cmd", template, "--startup-timeout", "10", root=root)
    assert code == 1, payload
    assert payload["ok"] is False
    assert "exited with 4 before it was healthy" in payload["error"]


def test_hub_contract_dry_run_starts_nothing(tmp_path):
    root = tmp_path / "state"
    code, payload = control("hub-contract", "--dry-run", root=root)
    assert code == 0, payload
    assert payload["dry_run"] is True
    assert payload["mode"] == "launched"
    assert "serve" in payload["would_launch"]
    assert "<port>" in payload["would_launch"]
    assert payload["covered_routes"] == covered_in_inventory()
    assert list((root / "targets").iterdir()) == []


def test_hub_contract_refuses_a_template_without_a_port(tmp_path):
    root = tmp_path / "state"
    code, payload = control("hub-contract", "--hub-cmd", "{brigade} fleet serve", "--dry-run", root=root)
    assert code == 1, payload
    assert "{port}" in payload["error"]


def test_hub_contract_refuses_a_remote_hub_url(tmp_path):
    root = tmp_path / "state"
    for url in ("http://hub.example.test:8080", "ftp://hub.example.test", "http://[::1]:8080/api"):
        code, payload = control("hub-contract", "--hub-url", url, "--dry-run", root=root)
        assert code == 1, (url, payload)
        assert "refusing --hub-url" in payload["error"], payload


def test_hub_contract_needs_an_admin_token_for_a_running_hub(tmp_path):
    root = tmp_path / "state"
    code, payload = control(
        "hub-contract",
        "--hub-url",
        "http://[::1]:9",
        "--admin-token-env",
        "VERIFY_BRIGADE_UNSET_ADMIN_TOKEN",
        root=root,
        env_extra={"VERIFY_BRIGADE_UNSET_ADMIN_TOKEN": ""},
    )
    assert code == 1, payload
    assert "admin token" in payload["error"]


def test_hub_contract_refuses_both_hub_modes(tmp_path):
    root = tmp_path / "state"
    code, payload = control("hub-contract", "--hub-cmd", "x {port}", "--hub-url", "http://[::1]:9", root=root)
    assert code == 1, payload
    assert "not both" in payload["error"]


def test_hub_contract_refuses_a_target_outside_the_state_root(tmp_path):
    root = tmp_path / "state"
    stranger = tmp_path / "not-a-target"
    stranger.mkdir()
    code, payload = control("hub-contract", "--target", str(stranger), root=root)
    assert code == 1, payload
    assert "refusing to use" in payload["error"], payload
    assert list(stranger.iterdir()) == []


def test_hub_contract_inventory_is_well_formed():
    rows = inventory_rows()
    assert rows, "no route rows parsed from the inventory"
    keys = [f"{method} {path}" for method, path, _ in rows]
    assert len(keys) == len(set(keys)), "a route is listed twice"
    for _, _, status in rows:
        assert status.startswith("covered") or status == "not yet", status
    text = INVENTORY.read_text(encoding="utf-8")
    assert f"The tables list {len(rows)} method and path pairs" in text
    assert f"The drive covers {len(covered_in_inventory())}," in text
    assert "contract_version` is **1**" in text


def test_hub_contract_feature_is_registered():
    features = (SKILL_DIR / "features" / "README.md").read_text(encoding="utf-8")
    doc = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "[hub-contract.md](hub-contract.md)" in features
    assert "$C hub-contract" in doc
    feature = FEATURE.read_text(encoding="utf-8")
    for heading in (
        "## Sub-features",
        "## How to get to it (user POV)",
        "## Driving it with control-brigade",
        "## Gotchas",
    ):
        assert heading in feature, heading


def _load_hub_contract():
    import importlib.util

    spec = importlib.util.spec_from_file_location("hub_contract_under_test", SKILL_DIR / "hub_contract.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Dataclasses resolve their module through sys.modules, so register it first.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_hub_contract_requests_do_not_follow_redirects():
    # A redirect is the hub's answer, and following it would forward the bearer
    # token to another origin, so the drive must report the 3xx as-is.
    import http.server
    import threading

    hits: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            hits.append(self.path)
            if self.path == "/start":
                self.send_response(302)
                self.send_header("Location", "/landed")
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        hub_contract = _load_hub_contract()
        resp = hub_contract.request(f"http://127.0.0.1:{server.server_port}", "GET", "/start", token="secret")
    finally:
        server.shutdown()
        server.server_close()
    assert resp.status == 302
    assert hits == ["/start"]


def test_hub_contract_keys_skip_malformed_identifiers():
    hub_contract = _load_hub_contract()
    items = [{"run_id": []}, {"run_id": {"x": 1}}, {"run_id": None}, {"run_id": "run-1", "state": "ok"}]
    assert hub_contract._keyed(items, "run_id") == {"run-1": {"run_id": "run-1", "state": "ok"}}
