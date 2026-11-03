"""Keep the verify-brigade `prod-readonly` hub profile (v2 slice S29) honest.

The audit launches a throwaway hub through `hub_audit.py`, builds a fixture
with every kind of overdue row, and must find no write, no write lock and no
digest change while the listed operations run. These tests run it on the
in-repo hub, prove that each kind of added write fails it, prove an unlisted
case stops the run before any request, and probe a running hub with operator
credentials the way production does.
"""

from __future__ import annotations

import importlib.util
import os
import socket
import sqlite3
import sys
import threading
from pathlib import Path

import pytest

from tests._posix import requires_process_groups
from tests.test_verify_brigade_skill import REPO_ROOT, SKILL_DIR, control

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the audit wrapper and process checks are POSIX")

RUNBOOK = REPO_ROOT / "docs" / "runbooks" / "fleet-hub-proxmox.md"
RUST_HUB = os.environ.get("BRIGADE_HUB_RUST_BIN") or str(
    REPO_ROOT / "engines" / "hub" / "target" / "release" / "brigade-hub"
)


def _load(name: str):
    if str(SKILL_DIR) not in sys.path:
        sys.path.insert(0, str(SKILL_DIR))
    spec = importlib.util.spec_from_file_location(name, SKILL_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


hr = _load("hub_readonly")
audit_launcher = _load("hub_audit")


@pytest.mark.parametrize(
    ("statement", "mode"),
    [
        ("BEGIN IMMEDIATE", "immediate"),
        ("begin immediate;", "immediate"),
        ("/* read */ BEGIN EXCLUSIVE", "exclusive"),
        ("-- note\nBEGIN EXCLUSIVE TRANSACTION;", "exclusive"),
        ("BEGIN/**/IMMEDIATE", "immediate"),
        ("  BEGIN\n\tIMMEDIATE  ", "immediate"),
        ("BEGIN", None),
        ("BEGIN DEFERRED;", None),
        ("SELECT 'BEGIN IMMEDIATE'", None),
        ("/* BEGIN IMMEDIATE */ SELECT 1", None),
    ],
)
def test_begin_mode_reads_through_comments_and_punctuation(statement, mode):
    assert audit_launcher.begin_mode(statement) == mode


def _assert_clean_audit(payload: dict) -> None:
    assert payload["ok"] is True, payload
    assert payload["mismatches"] == []
    assert payload["operations"] == sorted(hr.ALLOWED_OPERATIONS)
    audit = payload["audit"]
    assert audit["problems"] == []
    assert audit["hooks_live"] is True and audit["sweepers_paused"] is True
    assert audit["instrumented_pids"] and audit["uninstrumented_holders"] == []
    assert audit["open_evidence"] == {"complete": True, "reason": None, "outside_opens": []}
    assert audit["certified"] is True
    assert audit["fixture_failures"] == []
    assert set(audit["overdue_rows"]) == {
        "messages past retention",
        "sealed acknowledged over 7 days ago",
        "send-ledger rows over a day old",
        "questions past their deadline",
        "paging snapshots past their TTL",
    }
    assert all(count > 0 for count in audit["overdue_rows"].values()), audit["overdue_rows"]
    assert audit["window_findings"] == []
    assert audit["digest_changes"] == []
    assert [item["operation"] for item in audit["controls"]] == ["question_status", "who", "list", "policy inventory"]
    assert all(item["ok"] and item["writes"] > 0 for item in audit["controls"]), audit["controls"]


def test_prod_readonly_audit_passes_on_the_python_hub(tmp_path):
    code, payload = control("hub-contract", "--profile", "prod-readonly", root=tmp_path / "state")
    assert code == 0, payload
    assert payload["mode"] == "audit"
    _assert_clean_audit(payload)
    capture = Path(payload["capture"]).read_text(encoding="utf-8")
    token = (Path(payload["hub"]["state_dir"]) / "admin-token").read_text(encoding="utf-8").strip()
    assert token not in capture


@pytest.mark.skipif(not os.access(RUST_HUB, os.X_OK), reason="needs a release build of engines/hub")
def test_prod_readonly_audit_passes_through_the_rust_front(tmp_path):
    template = f"{RUST_HUB} --host {{host}} --port {{port}} --db {{db}} --token-file {{token_file}}"
    code, payload = control(
        "hub-contract", "--profile", "prod-readonly", "--hub-cmd", template, root=tmp_path / "state"
    )
    assert code == 0, payload
    _assert_clean_audit(payload)
    assert payload["hub"]["argv"][0] == RUST_HUB


@pytest.mark.parametrize(
    ("mutation", "problems", "finding", "changed"),
    [
        (
            "GET /owners:write",
            ["a listed operation wrote, or took the write lock", "the database changed during the probe"],
            {"kind": "write", "action": "update", "target": "nodes"},
            ["nodes"],
        ),
        (
            "POST /messages count:noop",
            ["a listed operation wrote, or took the write lock"],
            {"kind": "write", "action": "update", "target": "nodes"},
            [],
        ),
        (
            "GET /status:begin",
            ["a listed operation wrote, or took the write lock"],
            {"kind": "begin", "mode": "immediate"},
            [],
        ),
        (
            "GET /status:begin-semicolon",
            ["a listed operation wrote, or took the write lock"],
            {"kind": "begin", "mode": "immediate"},
            [],
        ),
        (
            "GET /owners:begin-comment",
            ["a listed operation wrote, or took the write lock"],
            {"kind": "begin", "mode": "exclusive"},
            [],
        ),
    ],
)
def test_prod_readonly_audit_fails_when_a_listed_operation_writes(tmp_path, mutation, problems, finding, changed):
    code, payload = control(
        "hub-contract", "--profile", "prod-readonly", "--audit-mutation", mutation, root=tmp_path / "state"
    )
    assert code == 3, payload
    assert payload["ok"] is False
    # The responses still match the contract: only the audit sees the write.
    assert payload["mismatches"] == []
    audit = payload["audit"]
    assert audit["mutation"] == mutation
    assert audit["problems"] == problems
    assert [{key: item[key] for key in finding} for item in audit["window_findings"]] == [finding]
    assert audit["digest_changes"] == changed


def test_prod_readonly_audit_fails_when_a_helper_locks_the_database_and_exits(tmp_path):
    """A helper in the hub's process group runs BEGIN IMMEDIATE; ROLLBACK during GET /status and closes.

    Its connection has no hooks, the digest does not change and it is gone
    before any /proc scan, so only the open watch can see it.
    """
    mutation = "GET /status:helper"
    code, payload = control(
        "hub-contract", "--profile", "prod-readonly", "--audit-mutation", mutation, root=tmp_path / "state"
    )
    assert code == 3, payload
    assert payload["ok"] is False and payload["mismatches"] == []
    audit = payload["audit"]
    assert audit["mutation"] == mutation
    assert audit["problems"] == ["a process opened the database outside the audit hooks"]
    assert audit["certified"] is False
    assert audit["window_findings"] == [] and audit["digest_changes"] == []
    assert audit["uninstrumented_holders"] == []
    opens = audit["open_evidence"]["outside_opens"]
    assert {"phase": "probe", "name": "hub.db", "events": ["open"]} in opens, opens


class _NoInotify:
    """An open watch where inotify is unavailable."""

    events: list[dict] = []
    lost = "inotify is unavailable: test"

    def __init__(self, directory, name, inode=None) -> None:
        pass

    def drain(self, next_phase=None) -> None:
        pass

    def close(self) -> None:
        pass


def test_prod_readonly_audit_without_open_evidence_is_not_certified(tmp_path, monkeypatch):
    monkeypatch.setattr(hr, "OpenWatch", _NoInotify)
    state = tmp_path / "audit"
    state.mkdir()
    hub, result = hr.audit(
        brigade=[sys.executable, "-m", "brigade"],
        state_dir=state,
        hub_cmd=None,
        startup_timeout=30,
        mutation=None,
        only=["GET /health"],
    )
    audit = result["audit"]
    assert audit["certified"] is False
    assert audit["problems"] == [
        "no evidence that every database open went through the audit hooks"
        " (inotify is unavailable: test): not certified"
    ]
    assert audit["open_evidence"]["complete"] is False
    assert audit["window_findings"] == [] and audit["digest_changes"] == []


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="inotify is Linux-only")
def test_prod_readonly_open_watch_sees_an_open_through_the_configured_path(tmp_path):
    db, route, error = hr.prepare_route(tmp_path)
    assert error is None and route is not None
    identity = (db.lstat().st_dev, db.lstat().st_ino)
    watch = hr.OpenWatch(db.parent, db.name)
    try:
        assert watch.lost is None
        sqlite3.connect(route).close()
        (db.parent / "unrelated.txt").write_text("x", encoding="utf-8")
        watch.drain("probe")
        assert watch.events == []
        conn = sqlite3.connect(db)
        conn.close()
        watch.drain()
        assert watch.events == [{"phase": "probe", "name": "hub.db", "events": ["open"]}]
    finally:
        watch.close()
    assert hr.route_intact(db, route, identity)
    os.link(db, tmp_path / "another-link.db")
    assert not hr.route_intact(db, route, identity)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="inotify is Linux-only")
def test_prod_readonly_open_watch_sees_a_link_made_and_removed_mid_run(tmp_path):
    db, route, error = hr.prepare_route(tmp_path)
    assert error is None and route is not None
    identity = (db.lstat().st_dev, db.lstat().st_ino)
    watch = hr.OpenWatch(db.parent, db.name, inode=db)
    try:
        db.chmod(0o600)  # attribute changes during launch are allowed
        watch.drain("probe")
        sqlite3.connect(route).close()
        watch.drain()
        assert watch.lost is None and watch.events == []
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        os.link(db, elsewhere / "copy.db")
        sqlite3.connect(elsewhere / "copy.db").close()
        os.unlink(elsewhere / "copy.db")
        watch.drain()
        assert watch.lost == "the database inode's links or attributes changed during the run"
        # The opens went through another directory, so only the inode watch saw them.
        assert watch.events == []
    finally:
        watch.close()
    # The link is gone by the end, so the final check alone would have passed.
    assert hr.route_intact(db, route, identity)


def test_prod_readonly_open_watch_off_linux_is_lost(tmp_path, monkeypatch):
    monkeypatch.setattr(hr.sys, "platform", "darwin")
    watch = hr.OpenWatch(tmp_path, "hub.db")
    assert watch.lost == "inotify is Linux-only and this is darwin" and watch.fd == -1
    watch.close()


class _Recorder:
    """A loopback socket that counts connections, to prove no request was sent."""

    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind((hr.hc.LOOPBACK, 0))
        self.sock.listen(8)
        self.sock.settimeout(0.2)
        self.connections = 0
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            self.connections += 1
            conn.close()

    @property
    def url(self) -> str:
        return f"http://{hr.hc.LOOPBACK}:{self.sock.getsockname()[1]}"

    def close(self) -> None:
        self.stop.set()
        self.thread.join(timeout=2)
        self.sock.close()


@pytest.mark.parametrize("only", ["POST /messages list", "GET /policy/inventory", "POST /messages question_status"])
def test_prod_readonly_refuses_an_unlisted_operation_before_any_request(tmp_path, only):
    recorder = _Recorder()
    try:
        env = {hr.NODE_TOKEN_ENV: "probe-node-token-x", hr.PROBE_TOKEN_ENV: "probe-principal-token-x"}
        code, payload = control(
            "hub-contract",
            "--profile",
            "prod-readonly",
            "--hub-url",
            recorder.url,
            "--only",
            only,
            root=tmp_path / "state",
            env_extra=env,
        )
    finally:
        recorder.close()
    assert code == 1, payload
    assert f"refusing --only {only!r}" in payload["error"]
    assert recorder.connections == 0


def test_prod_readonly_guard_refuses_unlisted_cases_before_any_request(monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(hr.hc, "request", lambda *args, **kwargs: sent.append(args[2]))
    listed = hr.profile_cases()
    unlisted = [
        hr.Case("list", "POST", "/messages", 200, None, "principal", {"action": "list"}),
        hr.Case(
            "identity with peers",
            "POST",
            "/messages",
            200,
            None,
            "principal",
            {"action": "identity", "peers": ["agent:x"]},
        ),
        hr.Case("status with a query", "GET", "/status?all=1", 200, None, "node"),
        hr.Case("inventory", "GET", "/policy/inventory", 200, None, "node"),
    ]
    for bad in unlisted:
        with pytest.raises(hr.hc.ContractError, match="refusing case"):
            # The bad case comes last, so a guard that ran lazily would already have sent the others.
            hr.run_profile("http://unused.invalid", node_token="n" * 8, principal_token="p" * 8, cases=[*listed, bad])
    assert sent == []


def test_prod_readonly_profile_is_exactly_the_listed_operations():
    assert sorted({hr.operation(case) for case in hr.profile_cases()}) == sorted(hr.ALLOWED_OPERATIONS)
    hr.guard_cases(hr.profile_cases())
    for excluded in ("list", "sent", "sent_lookup", "ack", "who", "claims"):
        assert f"POST /messages {excluded}" in hr.EXCLUDED_OPERATIONS
        assert f"POST /messages {excluded}" not in hr.ALLOWED_OPERATIONS


def test_prod_readonly_dry_run_starts_nothing(tmp_path):
    root = tmp_path / "state"
    code, payload = control("hub-contract", "--profile", "prod-readonly", "--dry-run", root=root)
    assert code == 0, payload
    assert payload["operations"] == sorted(hr.ALLOWED_OPERATIONS)
    assert "GET /policy/inventory" in payload["excluded"]
    assert not (root / "targets").exists() or list((root / "targets").iterdir()) == []


def test_prod_readonly_probe_needs_operator_credentials_and_a_loopback_hub(tmp_path):
    root = tmp_path / "state"
    recorder = _Recorder()
    url = recorder.url
    try:
        code, payload = control("hub-contract", "--profile", "prod-readonly", "--hub-url", url, root=root)
    finally:
        recorder.close()
    assert code == 1, payload
    assert hr.NODE_TOKEN_ENV in payload["error"] and hr.PROBE_TOKEN_ENV in payload["error"]
    assert recorder.connections == 0
    code, payload = control(
        "hub-contract", "--profile", "prod-readonly", "--hub-url", "http://hub.example.test:8080", root=root
    )
    assert code == 1 and "refusing --hub-url" in payload["error"], payload
    code, payload = control(
        "hub-contract",
        "--profile",
        "prod-readonly",
        "--hub-url",
        url,
        "--audit-mutation",
        "GET /owners:write",
        root=root,
    )
    assert code == 1 and "never to a running hub" in payload["error"], payload


def test_prod_readonly_probe_runs_against_a_running_hub_without_enrolling(tmp_path):
    hc = hr.hc
    state = tmp_path / "hub"
    state.mkdir()
    hub = hc.launch_hub(
        hc.DEFAULT_HUB_CMD, brigade=[sys.executable, "-m", "brigade"], state_dir=state, startup_timeout=30
    )
    try:
        # The operator's side: one node and one probe principal with no grants.
        node = hc.request(
            hub.url, "POST", "/nodes", body={"action": "add", "node_id": "probe-node"}, token=hub.admin_token
        )
        principal = hc.request(
            hub.url, "POST", "/message-principals", body={"action": "add", "name": "probe"}, token=hub.admin_token
        )
        env = {hr.NODE_TOKEN_ENV: node.json["token"], hr.PROBE_TOKEN_ENV: principal.json["principal"]["token"]}
        before = hc.request(hub.url, "GET", "/nodes", token=hub.admin_token).json["nodes"]
        code, payload = control(
            "hub-contract", "--profile", "prod-readonly", "--hub-url", hub.url, root=tmp_path / "state", env_extra=env
        )
        after = hc.request(hub.url, "GET", "/nodes", token=hub.admin_token).json["nodes"]
    finally:
        hc.stop_hub(hub)
    assert code == 0, payload
    assert payload["mode"] == "probe" and payload["audit"] is None
    assert payload["operations"] == sorted(hr.ALLOWED_OPERATIONS)
    assert after == before
    capture = Path(payload["capture"]).read_text(encoding="utf-8")
    assert env[hr.NODE_TOKEN_ENV] not in capture and env[hr.PROBE_TOKEN_ENV] not in capture


@requires_process_groups
@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="needs /proc")
def test_prod_readonly_finds_a_process_holding_the_database_without_hooks(tmp_path):
    db = tmp_path / "hub.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE t (x)")
        conn.commit()
        holders = hr.uninstrumented_holders(os.getpgid(0), set(), db)
        assert os.getpid() in {item["pid"] for item in holders}
        instrumented = hr.uninstrumented_holders(os.getpgid(0), {os.getpid()}, db)
        assert all(item["pid"] != os.getpid() for item in instrumented)
    finally:
        conn.close()


def test_prod_readonly_runbook_names_the_only_production_drive_run():
    runbook = " ".join(RUNBOOK.read_text(encoding="utf-8").split())
    assert "### Read-only drive probe" in runbook
    assert "The only drive run allowed against the production hub is `--profile prod-readonly`." in runbook
    feature = (SKILL_DIR / "features" / "hub-contract.md").read_text(encoding="utf-8")
    assert "This is the only drive run allowed against production" in feature
    for op in hr.ALLOWED_OPERATIONS:
        method, path, *action = op.split(" ")
        assert f"`{method} {path}`" in feature or (action and f"`{action[0]}`" in feature), op
