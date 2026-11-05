"""The S1b additions to the verify-brigade hub contract drive.

The lifecycle cases run on a second hub that reads the drive's clock, and the
refusal text and sealed tombstones are compared with a golden captured from
the Python hub. The parity run lives in `test_verify_brigade_hub_contract.py`;
these tests prove the new checks catch a hub that drifts, and pin the golden
comparator itself without starting a hub.
"""

from __future__ import annotations

import importlib.util
import json
import shlex
import sys

from tests.test_verify_brigade_skill import SKILL_DIR, control

# The in-repo hub on the drive's clock, behind a proxy that drifts two ways a
# rewrite could: a tombstoned sealed message is reported as still sealed, and
# one refusal changes its wording. The clock seam is installed before anything
# imports datetime, as hub_clock.py does for `fleet serve`.
DRIFTING_CLOCKED_HUB = """\
import sys
sys.path.insert(0, sys.argv[1])
import hub_clock
hub_clock._install(sys.argv[2])
import json, threading, urllib.error, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from brigade import fleet_hub

host, port, db, token_file = sys.argv[3:7]
token = Path(token_file).read_text(encoding="utf-8").strip()
inner = fleet_hub.make_server(host, 0, Path(db), token)
threading.Thread(target=inner.serve_forever, daemon=True).start()
base = "http://%s:%d" % (host, inner.server_address[1])


def unexpire(value):
    if isinstance(value, dict):
        return {k: ("sealed" if k == "content_state" and v == "content_expired" else unexpire(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [unexpire(item) for item in value]
    return value


class Proxy(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def forward(self):
        length = int(self.headers.get("Content-Length") or 0)
        data = self.rfile.read(length) if length else None
        headers = {k: v for k, v in self.headers.items() if k.lower() in ("authorization", "content-type")}
        request = urllib.request.Request(base + self.path, data=data, headers=headers, method=self.command)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                status, ctype, body = response.status, response.headers.get("Content-Type", ""), response.read()
        except urllib.error.HTTPError as exc:
            status, ctype, body = exc.code, exc.headers.get("Content-Type", ""), exc.read()
        try:
            reply = json.loads(body)
        except ValueError:
            reply = None
        if isinstance(reply, dict):
            if reply.get("error") == "message expired before delivery":
                reply["error"] = "message is too old"
            body = json.dumps(unexpire(reply)).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = forward


ThreadingHTTPServer((host, int(port)), Proxy).serve_forever()
"""

DRIFTED_LIFECYCLE_CASES = {
    # the tombstones read back as sealed
    "lifecycle sent_lookup a sealed message acknowledged 8 days ago",
    "lifecycle sent_lookup when the 7-day lifetime ends",
    "lifecycle sent by ids across the sealed lifecycle",
    "lifecycle list across the sealed lifecycle",
    # the refusal wording the golden pins
    "lifecycle send enqueued past retention",
}


def _golden():
    if str(SKILL_DIR) not in sys.path:
        sys.path.insert(0, str(SKILL_DIR))
    spec = importlib.util.spec_from_file_location(
        "hub_contract_golden_under_test", SKILL_DIR / "hub_contract_golden.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_hub_contract_lifecycle_reports_a_drifting_clocked_hub(tmp_path):
    root = tmp_path / "state"
    script = tmp_path / "drifting_clocked_hub.py"
    script.write_text(DRIFTING_CLOCKED_HUB, encoding="utf-8")
    template = " ".join(
        [
            shlex.quote(sys.executable),
            shlex.quote(str(script)),
            shlex.quote(str(SKILL_DIR)),
            "{clock_file} {host} {port} {db} {token_file}",
        ]
    )
    code, payload = control("hub-contract", "--clock-hub-cmd", template, root=root)
    assert code == 3, payload
    assert payload["lifecycle"]["ran"] is True
    reported = {item["case"] for item in payload["mismatches"]}
    assert reported == DRIFTED_LIFECYCLE_CASES, payload["mismatches"]
    by_case = {item["case"]: item for item in payload["mismatches"]}
    assert by_case["lifecycle send enqueued past retention"] == {
        "case": "lifecycle send enqueued past retention",
        "check": "error-text",
        "expected": {"status": 400, "error": "message expired before delivery"},
        "observed": {"status": 400, "error": "message is too old"},
    }
    tombstone = by_case["lifecycle sent_lookup a sealed message acknowledged 8 days ago"]
    assert tombstone["path"] == "$.message.content_state"
    failed = {case["case"] for case in payload["cases"] if not case["ok"]}
    assert failed == DRIFTED_LIFECYCLE_CASES


def test_hub_contract_clock_template_needs_a_clock_file(tmp_path):
    root = tmp_path / "state"
    code, payload = control(
        "hub-contract", "--clock-hub-cmd", "{brigade} fleet serve --port {port}", "--dry-run", root=root
    )
    assert code == 1, payload
    assert "{clock_file}" in payload["error"]


def test_hub_contract_write_golden_records_only_the_in_repo_hub(tmp_path):
    root = tmp_path / "state"
    code, payload = control("hub-contract", "--write-golden", "--hub-cmd", "x {port}", root=root)
    assert code == 1, payload
    assert "--write-golden records the in-repo Python hub alone" in payload["error"]


def test_hub_contract_golden_normalizes_run_values():
    golden = _golden()
    response = {
        "error": "target 'repo:example-org/x-abc123' was re-acquired (now acquired 2026-11-03T17:42:11.638577+00:00)",
        "code": "c",
        "id": 7,
        "owner": {"node": "contract-a-abc123"},
    }
    assert golden.entry(409, response, "abc123") == {
        "status": 409,
        "error": "target 'repo:example-org/x-{tag}' was re-acquired (now acquired {timestamp})",
        "code": "c",
    }
    assert golden.entry(401, "Dashboard request refused.\n", None) == {
        "status": 401,
        "body": "Dashboard request refused.\n",
    }


def test_hub_contract_golden_compare_reports_each_drift():
    golden = _golden()
    table = {
        "refusals": {
            "core": {"a": {"status": 400, "error": "bad a"}, "gone": {"status": 404, "error": "not found"}},
            "lifecycle": {"l": {"status": 404, "error": "message not found"}},
        },
        "tombstones": {"s-old": "{}"},
    }
    observed = {"core": {"a": {"status": 400, "error": "worse a"}, "new": {"status": 400, "error": "new"}}}
    problems = golden.compare(table, observed, {"core": {"a", "new"}}, None)
    by_case = {problem["case"]: problem for problem in problems}
    assert by_case["a"]["observed"] == {"status": 400, "error": "worse a"}
    assert by_case["new"]["expected"] == "an entry in hub-contract-golden.json"
    assert by_case["error-text golden (core)"]["observed"] == {"never sent": ["gone"]}
    # The lifecycle section did not run, so neither its refusals nor its tombstones are compared.
    assert set(by_case) == {"a", "new", "error-text golden (core)"}
    problems = golden.compare(
        table,
        {"lifecycle": {"l": {"status": 404, "error": "message not found"}}},
        {"lifecycle": {"l"}},
        {"s-old": "[]"},
    )
    assert problems == [{"case": "lifecycle tombstone s-old", "check": "tombstone", "expected": "{}", "observed": "[]"}]


def test_hub_contract_golden_is_checked_in_and_complete():
    golden = _golden()
    table = json.loads(golden.GOLDEN_PATH.read_text(encoding="utf-8"))
    assert table["contract_version"] == 1
    assert set(table["refusals"]) == {"core", "lifecycle"}
    assert len(table["refusals"]["core"]) >= 150
    assert all(entry["status"] >= 400 for section in table["refusals"].values() for entry in section.values())
    assert sorted(table["tombstones"]) == ["s-mid", "s-old"]
    for text in table["tombstones"].values():
        value = json.loads(text)
        assert "ct" not in value and "recipients" not in value
        assert text == json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
