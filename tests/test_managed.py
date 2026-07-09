from pathlib import Path

from brigade import managed
from brigade.station import DoctorContext


def test_all_tools_declare_required_fields():
    for t in managed.all_tools():
        assert t.name and t.station and t.command
        assert callable(t.doctor)
        assert callable(t.wire)
        assert isinstance(t.install_args, list) and t.install_args
        assert isinstance(t.surfaces, tuple)
        for surface in t.surfaces:
            assert surface.kind
            assert surface.command
            assert surface.read_only is True


def test_tools_attach_to_known_stations():
    stations = {t.station for t in managed.all_tools()}
    assert stations <= {"memory", "guard", "tokens", "search", "pantry", "notifications", "evidence"}


def test_for_station_filters():
    names = {t.name for t in managed.for_station("memory")}
    assert names == {"bootstrap-doctor"}


def test_detect_uses_which(monkeypatch):
    t = managed.resolve("bootstrap-doctor")
    monkeypatch.setattr(managed.proc, "which", lambda c: None)
    assert t.detect() is False
    monkeypatch.setattr(managed.proc, "which", lambda c: "/usr/bin/" + c)
    assert t.detect() is True


def test_memory_doctor_no_longer_external_managed_tool():
    # Folded into brigade.memory_doctor / brigade memory status|lint|compact.
    assert managed.resolve("memory-doctor") is None


def test_token_glace_installs_from_release_tarball():
    # npm has no token-glace package and a git spec does not build; the
    # GitHub release tarball is the only installable artifact.
    t = managed.resolve("token-glace")
    assert t.install_args[:3] == ["npm", "install", "-g"]
    assert t.install_args[3].startswith("https://github.com/escoffier-labs/token-glace/releases/download/")
    assert t.install_args[3].endswith(".tar.gz")


def test_token_glace_doctor_reads_status_field_not_exit(monkeypatch):
    t = managed.resolve("token-glace")
    monkeypatch.setattr(managed.proc, "which", lambda c: "/x/" + c)

    def fake_run(args, **kw):
        assert args == ["token-glace", "doctor", "hooks", "--format", "json"]
        # exit 0 but status warn -> must surface as WARN, not OK
        return managed.proc.Result(code=0, stdout='{"status": "warn", "integrations": {}}', stderr="")

    monkeypatch.setattr(managed.proc, "run", fake_run)
    ctx = DoctorContext(target=Path("/tmp/ws"), selection=None, harnesses=[])
    results = t.doctor(ctx)
    assert any(status == "WARN" for status, _, _ in results)


def test_token_glace_declares_stage_one_surfaces():
    t = managed.resolve("token-glace")
    surfaces = {surface.kind: surface.command for surface in t.surfaces}
    assert surfaces["doctor-json"] == ("token-glace", "doctor", "hooks", "--format", "json")
    assert surfaces["summary-json"] == ("token-glace", "stats", "--format", "json")


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


def test_agentpantry_declares_markdown_brief_surface():
    t = managed.resolve("agentpantry")
    surfaces = {surface.kind: surface for surface in t.surfaces}
    assert surfaces["doctor-json"].command == ("agentpantry", "doctor", "--json")
    assert surfaces["brief-markdown"].command == ("agentpantry", "inventory", "--markdown")
    assert surfaces["brief-markdown"].timeout_seconds == 10.0
    assert surfaces["brief-markdown"].max_chars == 4000


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
    t = managed.resolve("miseledger")
    assert t is not None and t.station == "evidence"
    names = {t.name for t in managed.for_station("evidence")}
    assert names == {"miseledger"}


def test_evidence_install_args_use_escoffier_labs():
    t = managed.resolve("miseledger")
    joined = " ".join(t.install_args)
    assert "github.com/escoffier-labs/miseledger/cmd/miseledger@latest" in joined


def test_search_tools_attach_to_search_station():
    for name in ("code-search-api", "code-search-mcp", "graphtrail"):
        t = managed.resolve(name)
        assert t is not None and t.station == "search", name
    names = {t.name for t in managed.for_station("search")}
    assert names == {"code-search-api", "code-search-mcp", "graphtrail"}


def test_graphtrail_doctor_reports_missing_db(monkeypatch, tmp_path):
    t = managed.resolve("graphtrail")
    assert t is not None
    monkeypatch.setattr(managed.proc, "which", lambda c: "/usr/bin/graphtrail" if c == "graphtrail" else None)
    ctx = DoctorContext(target=tmp_path, selection=None, harnesses=[])
    results = t.doctor(ctx)
    assert results[0][0] == "WARN"
    assert "graphtrail sync" in results[0][2]


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
        assert kw == {"timeout": 120.0}
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


def test_miseledger_doctor_warns_distinctly_on_timeout(monkeypatch):
    t = managed.resolve("miseledger")

    def fake_run(args, **kw):
        assert args == ["miseledger", "status", "--json"]
        assert kw == {"timeout": 120.0}
        return managed.proc.Result(code=124, stdout="", stderr="timeout after 120.0s")

    monkeypatch.setattr(managed.proc, "run", fake_run)
    ctx = DoctorContext(target=Path("/tmp/ws"), selection=None, harnesses=[])
    results = t.doctor(ctx)
    assert any(status == "WARN" and "timed out" in detail and "120s" in detail for status, _, detail in results)
    assert all(status != "FAIL" for status, _, _ in results)


def test_content_guard_install_args_use_escoffier_labs():
    t = managed.resolve("content-guard")
    assert "github.com/escoffier-labs/content-guard" in " ".join(t.install_args)


def test_agent_notify_install_args_use_escoffier_labs():
    t = managed.resolve("agent-notify")
    assert "github.com/escoffier-labs/agent-notify/cmd/agent-notify@latest" in " ".join(t.install_args)
