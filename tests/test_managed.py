from pathlib import Path

from brigade import managed
from brigade.station import DoctorContext


def test_all_tools_declare_required_fields():
    for t in managed.all_tools():
        assert t.name and t.station and t.command
        assert callable(t.doctor)
        assert callable(t.wire)
        assert isinstance(t.install_args, list) and t.install_args


def test_tools_attach_to_known_stations():
    stations = {t.station for t in managed.all_tools()}
    assert stations <= {"memory", "guard", "tokens", "search", "pantry", "notifications", "evidence"}


def test_for_station_filters():
    names = {t.name for t in managed.for_station("memory")}
    assert names == {"memory-doctor", "bootstrap-doctor"}


def test_detect_uses_which(monkeypatch):
    t = managed.resolve("content-guard")
    monkeypatch.setattr(managed.proc, "which", lambda c: None)
    assert t.detect() is False
    monkeypatch.setattr(managed.proc, "which", lambda c: "/usr/bin/" + c)
    assert t.detect() is True


def test_memory_doctor_doctor_parses_status(monkeypatch):
    t = managed.resolve("memory-doctor")
    monkeypatch.setattr(managed.proc, "which", lambda c: "/x/" + c)

    def fake_run(args, **kw):
        return managed.proc.Result(code=0, stdout='{"cards": 4, "dead_links": 0, "pending_handoffs": 1}', stderr="")

    monkeypatch.setattr(managed.proc, "run", fake_run)
    ctx = DoctorContext(target=Path("/tmp/ws"), selection=None, harnesses=[])
    results = t.doctor(ctx)
    assert any(status == "OK" and "memory-doctor" in name for status, name, _ in results)


def test_tokenjuice_doctor_reads_status_field_not_exit(monkeypatch):
    t = managed.resolve("tokenjuice")
    monkeypatch.setattr(managed.proc, "which", lambda c: "/x/" + c)

    def fake_run(args, **kw):
        # exit 0 but status warn -> must surface as WARN, not OK
        return managed.proc.Result(code=0, stdout='{"status": "warn", "integrations": {}}', stderr="")

    monkeypatch.setattr(managed.proc, "run", fake_run)
    ctx = DoctorContext(target=Path("/tmp/ws"), selection=None, harnesses=[])
    results = t.doctor(ctx)
    assert any(status == "WARN" for status, _, _ in results)


def test_agentpantry_doctor_unwired(monkeypatch):
    t = managed.resolve("agentpantry")
    assert t is not None and t.station == "pantry"

    def fake_run(args, **kw):
        assert args == ["agentpantry", "doctor", "--json"]
        return managed.proc.Result(
            code=2, stdout='{"configured": false, "fail_count": 1, "warn_count": 0, "checks": []}', stderr=""
        )

    monkeypatch.setattr(managed.proc, "run", fake_run)
    ctx = DoctorContext(target=Path("/tmp/ws"), selection=None, harnesses=[])
    results = t.doctor(ctx)
    assert any(status == "WARN" and "unwired" in detail for status, _, detail in results)


def test_agentpantry_doctor_parses_status(monkeypatch):
    t = managed.resolve("agentpantry")

    def fake_run(args, **kw):
        assert args == ["agentpantry", "doctor", "--json"]
        return managed.proc.Result(
            code=0,
            stdout='{"role": "source", "configured": true, "peer": "127.0.0.1:8787",'
            ' "surfaces": ["sidecar"], "browser_count": 1,'
            ' "fail_count": 0, "warn_count": 0, "checks": []}',
            stderr="",
        )

    monkeypatch.setattr(managed.proc, "run", fake_run)
    ctx = DoctorContext(target=Path("/tmp/ws"), selection=None, harnesses=[])
    results = t.doctor(ctx)
    assert any(status == "OK" and "agentpantry" in name for status, name, _ in results)


def test_agentpantry_never_fails_workspace(monkeypatch):
    # Advisory/operator-scoped: agentpantry FAIL checks become Brigade WARNs.
    t = managed.resolve("agentpantry")

    def fake_run(args, **kw):
        assert args == ["agentpantry", "doctor", "--json"]
        return managed.proc.Result(
            code=1,
            stdout='{"role": "sink", "configured": true, "peer": "0.0.0.0:8787",'
            ' "surfaces": ["sidecar"], "browser_count": 0,'
            ' "fail_count": 1, "warn_count": 1,'
            ' "checks": [{"name": "key", "status": "FAIL", "detail": "PSK not found"}]}',
            stderr="",
        )

    monkeypatch.setattr(managed.proc, "run", fake_run)
    ctx = DoctorContext(target=Path("/tmp/ws"), selection=None, harnesses=[])
    results = t.doctor(ctx)
    assert all(status != "FAIL" for status, _, _ in results)
    assert any(status == "WARN" for status, _, _ in results)


def test_agentpantry_doctor_handles_garbage_output(monkeypatch):
    # A misbehaving binary (non-2 exit, non-JSON stdout) must degrade to WARN,
    # never throw - the doctor loop runs adapters unguarded.
    t = managed.resolve("agentpantry")
    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        if args == ["agentpantry", "status", "--json"]:
            return managed.proc.Result(code=1, stdout="boom", stderr="kaboom")
        return managed.proc.Result(code=1, stdout="boom", stderr="kaboom")

    monkeypatch.setattr(managed.proc, "run", fake_run)
    ctx = DoctorContext(target=Path("/tmp/ws"), selection=None, harnesses=[])
    results = t.doctor(ctx)
    assert any(status == "WARN" and "unexpected output" in detail for status, _, detail in results)
    assert all(status != "FAIL" for status, _, _ in results)
    assert calls == [["agentpantry", "doctor", "--json"], ["agentpantry", "status", "--json"]]


def test_agentpantry_doctor_falls_back_to_status_for_old_binary(monkeypatch):
    t = managed.resolve("agentpantry")
    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        if args == ["agentpantry", "doctor", "--json"]:
            return managed.proc.Result(code=2, stdout="", stderr="flag provided but not defined: -json")
        return managed.proc.Result(
            code=0,
            stdout='{"role": "sink", "configured": true, "peer": "127.0.0.1:8787",'
            ' "key_present": true, "surfaces": ["sidecar"], "browsers": 0,'
            ' "allow": [], "deny": []}',
            stderr="",
        )

    monkeypatch.setattr(managed.proc, "run", fake_run)
    ctx = DoctorContext(target=Path("/tmp/ws"), selection=None, harnesses=[])
    results = t.doctor(ctx)
    assert any(status == "OK" and "role=sink" in detail for status, _, detail in results)
    assert calls == [["agentpantry", "doctor", "--json"], ["agentpantry", "status", "--json"]]


def test_evidence_tools_attach_to_evidence_station():
    for name in ("miseledger", "stationtrail", "sourceharvest"):
        t = managed.resolve(name)
        assert t is not None and t.station == "evidence", name
    names = {t.name for t in managed.for_station("evidence")}
    assert names == {"miseledger", "stationtrail", "sourceharvest"}


def test_evidence_install_args_use_escoffier_labs():
    for name in ("miseledger", "stationtrail", "sourceharvest"):
        t = managed.resolve(name)
        joined = " ".join(t.install_args)
        assert f"github.com/escoffier-labs/{name}/cmd/{name}@latest" in joined, name


def test_search_tools_attach_to_search_station():
    for name in ("code-search-api", "code-search-mcp"):
        t = managed.resolve(name)
        assert t is not None and t.station == "search", name
    names = {t.name for t in managed.for_station("search")}
    assert names == {"code-search-api", "code-search-mcp"}


def test_search_install_args_keep_npm_scope_distinction():
    api = managed.resolve("code-search-api")
    mcp = managed.resolve("code-search-mcp")
    assert "github.com/escoffier-labs/code-search-api" in " ".join(api.install_args)
    # GitHub moved to escoffier-labs, but npm remains under Solomon's existing scope.
    assert "@solomonneas/code-search-mcp" in " ".join(mcp.install_args)


def test_code_search_api_doctor_reads_http_health(monkeypatch):
    t = managed.resolve("code-search-api")
    monkeypatch.setenv("CODE_SEARCH_API_URL", "http://search.local")
    seen = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"status": "ok", "version": "2.0.1", "chunks": 12, "embedded": 11, "summarized": 7}'

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(managed.urllib.request, "urlopen", fake_urlopen)
    ctx = DoctorContext(target=Path("/tmp/ws"), selection=None, harnesses=[])
    results = t.doctor(ctx)
    assert seen == {"url": "http://search.local/api/health", "timeout": 2.0}
    assert any(status == "OK" and "chunks=12" in detail for status, _, detail in results)


def test_code_search_api_doctor_warns_when_service_unavailable(monkeypatch):
    t = managed.resolve("code-search-api")

    def fake_urlopen(request, timeout):
        raise OSError("connection refused")

    monkeypatch.setattr(managed.urllib.request, "urlopen", fake_urlopen)
    ctx = DoctorContext(target=Path("/tmp/ws"), selection=None, harnesses=[])
    results = t.doctor(ctx)
    assert any(status == "WARN" and "service health unavailable" in detail for status, _, detail in results)
    assert all(status != "FAIL" for status, _, _ in results)


def test_code_search_mcp_doctor_is_presence_only():
    t = managed.resolve("code-search-mcp")
    ctx = DoctorContext(target=Path("/tmp/ws"), selection=None, harnesses=[])
    results = t.doctor(ctx)
    assert any(status == "OK" and "MCP clients" in detail for status, _, detail in results)


def test_miseledger_doctor_parses_status(monkeypatch):
    t = managed.resolve("miseledger")

    def fake_run(args, **kw):
        assert args == ["miseledger", "status", "--json"]
        return managed.proc.Result(
            code=0,
            stdout='{"schema_version": 7, "items": 42, "sources": 3, "artifacts": 5, "fts": "ok", "source_counts": {}}',
            stderr="",
        )

    monkeypatch.setattr(managed.proc, "run", fake_run)
    ctx = DoctorContext(target=Path("/tmp/ws"), selection=None, harnesses=[])
    results = t.doctor(ctx)
    assert any(status == "OK" and "items=42" in detail for status, _, detail in results)


def test_miseledger_doctor_warns_on_unavailable_fts(monkeypatch):
    t = managed.resolve("miseledger")

    def fake_run(args, **kw):
        return managed.proc.Result(
            code=0,
            stdout='{"schema_version": 7, "items": 0, "sources": 0, "fts": "unavailable"}',
            stderr="",
        )

    monkeypatch.setattr(managed.proc, "run", fake_run)
    ctx = DoctorContext(target=Path("/tmp/ws"), selection=None, harnesses=[])
    results = t.doctor(ctx)
    assert any(status == "WARN" for status, _, _ in results)
    assert all(status != "FAIL" for status, _, _ in results)


def test_miseledger_doctor_handles_garbage_output(monkeypatch):
    t = managed.resolve("miseledger")

    def fake_run(args, **kw):
        return managed.proc.Result(code=1, stdout="boom", stderr="kaboom")

    monkeypatch.setattr(managed.proc, "run", fake_run)
    ctx = DoctorContext(target=Path("/tmp/ws"), selection=None, harnesses=[])
    results = t.doctor(ctx)
    assert any(status == "WARN" and "unwired or errored" in detail for status, _, detail in results)
    assert all(status != "FAIL" for status, _, _ in results)


def test_stationtrail_doctor_parses_report(monkeypatch):
    t = managed.resolve("stationtrail")

    def fake_run(args, **kw):
        assert args == ["stationtrail", "doctor", "--json"]
        return managed.proc.Result(
            code=0,
            stdout='{"ok": true, "warnings": [],'
            ' "sources": [{"kind": "codex", "status": "ready"},'
            ' {"kind": "claude", "status": "missing"}]}',
            stderr="",
        )

    monkeypatch.setattr(managed.proc, "run", fake_run)
    ctx = DoctorContext(target=Path("/tmp/ws"), selection=None, harnesses=[])
    results = t.doctor(ctx)
    assert any(status == "OK" and "sources=2" in detail and "ready=1" in detail for status, _, detail in results)


def test_stationtrail_doctor_warns_but_never_fails(monkeypatch):
    # ok=false (a source could not be read) is advisory, not a workspace failure.
    t = managed.resolve("stationtrail")

    def fake_run(args, **kw):
        return managed.proc.Result(
            code=0,
            stdout='{"ok": false, "warnings": ["codex status is error"],'
            ' "sources": [{"kind": "codex", "status": "error"}]}',
            stderr="",
        )

    monkeypatch.setattr(managed.proc, "run", fake_run)
    ctx = DoctorContext(target=Path("/tmp/ws"), selection=None, harnesses=[])
    results = t.doctor(ctx)
    assert any(status == "WARN" for status, _, _ in results)
    assert all(status != "FAIL" for status, _, _ in results)


def test_stationtrail_doctor_handles_garbage_output(monkeypatch):
    t = managed.resolve("stationtrail")

    def fake_run(args, **kw):
        return managed.proc.Result(code=2, stdout="not json", stderr="flag provided but not defined")

    monkeypatch.setattr(managed.proc, "run", fake_run)
    ctx = DoctorContext(target=Path("/tmp/ws"), selection=None, harnesses=[])
    results = t.doctor(ctx)
    assert any(status == "WARN" and "unreadable" in detail for status, _, detail in results)
    assert all(status != "FAIL" for status, _, _ in results)


def test_sourceharvest_doctor_present(monkeypatch):
    t = managed.resolve("sourceharvest")

    def fake_run(args, **kw):
        assert args == ["sourceharvest", "version"]
        return managed.proc.Result(code=0, stdout="sourceharvest 0.1.0", stderr="")

    monkeypatch.setattr(managed.proc, "run", fake_run)
    ctx = DoctorContext(target=Path("/tmp/ws"), selection=None, harnesses=[])
    results = t.doctor(ctx)
    assert any(status == "OK" and "sourceharvest 0.1.0" in detail for status, _, detail in results)


def test_sourceharvest_doctor_warns_when_not_runnable(monkeypatch):
    t = managed.resolve("sourceharvest")

    def fake_run(args, **kw):
        return managed.proc.Result(code=127, stdout="", stderr="boom")

    monkeypatch.setattr(managed.proc, "run", fake_run)
    ctx = DoctorContext(target=Path("/tmp/ws"), selection=None, harnesses=[])
    results = t.doctor(ctx)
    assert any(status == "WARN" and "not runnable" in detail for status, _, detail in results)
    assert all(status != "FAIL" for status, _, _ in results)


def test_content_guard_install_args_use_escoffier_labs():
    t = managed.resolve("content-guard")
    assert "github.com/escoffier-labs/content-guard" in " ".join(t.install_args)


def test_agent_notify_install_args_use_escoffier_labs():
    t = managed.resolve("agent-notify")
    assert "github.com/escoffier-labs/agent-notify/cmd/agent-notify@latest" in " ".join(t.install_args)
