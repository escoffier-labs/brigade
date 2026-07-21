import json
from pathlib import Path

from brigade import managed
from brigade import station_manifest
from brigade import stations_cmd
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
            assert isinstance(surface.read_only, bool)


def test_tools_attach_to_known_stations():
    stations = {t.station for t in managed.all_tools()}
    assert stations <= {"memory", "guard", "tokens", "search", "pantry", "notifications", "evidence"}


def test_for_station_filters():
    names = {t.name for t in managed.for_station("memory")}
    assert names == {"bootstrap-doctor"}


def test_detect_uses_resolver(monkeypatch):
    t = managed.resolve("bootstrap-doctor")
    monkeypatch.setattr(managed.component_bins, "resolve", lambda name, **kw: None)
    assert t.detect() is False
    monkeypatch.setattr(managed.component_bins, "resolve", lambda name, **kw: "/usr/bin/" + name)
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
    assert surfaces["summary-json"] == ("token-glace", "stats", "--format", "json", "--timezone", "utc")


def test_agentpantry_doctor_unwired(monkeypatch):
    t = managed.resolve("agentpantry")
    assert t is not None and t.station == "pantry"

    def fake_run(args, **kw):
        assert args == ["agentpantry", "doctor", "--json", "--no-net"]
        return managed.proc.Result(
            code=2, stdout='{"configured": false, "fail_count": 1, "warn_count": 0, "checks": []}', stderr=""
        )

    monkeypatch.setattr(managed.proc, "run", fake_run)
    ctx = DoctorContext(target=Path("/tmp/ws"), selection=None, harnesses=[])
    results = t.doctor(ctx)
    assert any(status == "WARN" and "unwired" in detail for status, _, detail in results)


def test_agentpantry_declares_json_inventory_contract():
    t = managed.resolve("agentpantry")
    surfaces = {surface.kind: surface for surface in t.surfaces}
    doctor = surfaces["doctor-json"]
    assert doctor.command == ("agentpantry", "doctor", "--json", "--no-net")
    assert doctor.read_only is False
    assert doctor.timeout_seconds == 10.0
    assert doctor.probe == ("agentpantry", "doctor", "--help")
    assert doctor.probe_contains == ("-json", "-no-net")
    inventory = surfaces["summary-json"]
    assert inventory.command == ("agentpantry", "inventory", "--json")
    assert inventory.read_only is True
    assert inventory.timeout_seconds == 10.0
    assert inventory.max_chars == 4000
    assert inventory.probe == ("agentpantry", "inventory", "--help")
    assert inventory.probe_contains == ("-json",)
    assert surfaces["verify-exit"].command == ("agentpantry", "version", "--json")


def test_agentpantry_doctor_parses_status(monkeypatch):
    t = managed.resolve("agentpantry")

    def fake_run(args, **kw):
        assert args == ["agentpantry", "doctor", "--json", "--no-net"]
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
        assert args == ["agentpantry", "doctor", "--json", "--no-net"]
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
    assert calls == [["agentpantry", "doctor", "--json", "--no-net"], ["agentpantry", "status", "--json"]]


def test_agentpantry_doctor_falls_back_to_status_for_old_binary(monkeypatch):
    t = managed.resolve("agentpantry")
    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        if args == ["agentpantry", "doctor", "--json", "--no-net"]:
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
    assert calls == [["agentpantry", "doctor", "--json", "--no-net"], ["agentpantry", "status", "--json"]]


def test_evidence_tools_attach_to_evidence_station():
    t = managed.resolve("miseledger")
    assert t is not None and t.station == "evidence"
    names = {t.name for t in managed.for_station("evidence")}
    assert names == {"miseledger"}


def test_evidence_install_uses_verified_brigade_setup():
    t = managed.resolve("miseledger")
    assert t.install_args == ["brigade", "setup"]


def test_search_tools_attach_to_search_station():
    for name in ("code-search-api", "code-search-mcp", "graphtrail"):
        t = managed.resolve(name)
        assert t is not None and t.station == "search", name
    names = {t.name for t in managed.for_station("search")}
    assert names == {"code-search-api", "code-search-mcp", "graphtrail"}


def test_graphtrail_doctor_reports_missing_db(monkeypatch, tmp_path):
    t = managed.resolve("graphtrail")
    assert t is not None
    monkeypatch.setattr(
        managed.component_bins, "resolve", lambda name, **kw: "/usr/bin/graphtrail" if name == "graphtrail" else None
    )
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
    monkeypatch.setattr(managed.component_bins, "resolve", lambda name, **kw: "miseledger")

    def fake_run(args, **kw):
        assert args == ["miseledger", "doctor", "--json"]
        assert kw == {"timeout": 120.0}
        return managed.proc.Result(
            code=0,
            stdout='{"ok": true, "checks": [{"name": "schema", "ok": true, "detail": "version 7"}]}',
            stderr="",
        )

    monkeypatch.setattr(managed.proc, "run", fake_run)
    ctx = DoctorContext(target=Path("/tmp/ws"), selection=None, harnesses=[])
    results = t.doctor(ctx)
    assert any(status == "OK" and "1 check(s) passed" in detail for status, _, detail in results)


def test_miseledger_doctor_warns_on_unavailable_fts(monkeypatch):
    t = managed.resolve("miseledger")
    monkeypatch.setattr(managed.component_bins, "resolve", lambda name, **kw: "miseledger")

    def fake_run(args, **kw):
        return managed.proc.Result(
            code=1,
            stdout='{"ok": false, "checks": [{"name": "fts", "ok": false, "detail": "sqlite fts5"}]}',
            stderr="",
        )

    monkeypatch.setattr(managed.proc, "run", fake_run)
    ctx = DoctorContext(target=Path("/tmp/ws"), selection=None, harnesses=[])
    results = t.doctor(ctx)
    assert any(status == "WARN" for status, _, _ in results)
    assert all(status != "FAIL" for status, _, _ in results)


def test_miseledger_doctor_handles_garbage_output(monkeypatch):
    t = managed.resolve("miseledger")
    monkeypatch.setattr(managed.component_bins, "resolve", lambda name, **kw: "miseledger")

    def fake_run(args, **kw):
        return managed.proc.Result(code=1, stdout="boom", stderr="kaboom")

    monkeypatch.setattr(managed.proc, "run", fake_run)
    ctx = DoctorContext(target=Path("/tmp/ws"), selection=None, harnesses=[])
    results = t.doctor(ctx)
    assert any(status == "WARN" and "unwired or errored" in detail for status, _, detail in results)
    assert all(status != "FAIL" for status, _, _ in results)


def test_miseledger_doctor_warns_distinctly_on_timeout(monkeypatch):
    t = managed.resolve("miseledger")
    monkeypatch.setattr(managed.component_bins, "resolve", lambda name, **kw: "miseledger")

    def fake_run(args, **kw):
        assert args == ["miseledger", "doctor", "--json"]
        assert kw == {"timeout": 120.0}
        return managed.proc.Result(code=124, stdout="", stderr="timeout after 120.0s")

    monkeypatch.setattr(managed.proc, "run", fake_run)
    ctx = DoctorContext(target=Path("/tmp/ws"), selection=None, harnesses=[])
    results = t.doctor(ctx)
    assert any(status == "WARN" and "timed out" in detail and "120s" in detail for status, _, detail in results)
    assert all(status != "FAIL" for status, _, _ in results)


def test_miseledger_declares_bounded_evidence_contract():
    t = managed.resolve("miseledger")
    surfaces = {surface.kind: surface for surface in t.surfaces}
    doctor = surfaces["doctor-json"]
    assert doctor.command == ("miseledger", "doctor", "--json")
    assert doctor.read_only is False
    assert doctor.timeout_seconds == 120.0
    assert doctor.probe == ("miseledger", "doctor", "--help")
    assert doctor.probe_contains == ("--json", "--mcp", "--archive")
    evidence = surfaces["brief-markdown"]
    assert evidence.command == ("miseledger", "evidence", "<task>", "--markdown", "--limit", "5")
    assert evidence.read_only is False
    assert evidence.timeout_seconds == 10.0
    assert evidence.max_chars == 4000
    assert evidence.probe == ("miseledger", "evidence", "--help")
    assert evidence.probe_contains == ("--markdown", "--limit")
    assert surfaces["verify-exit"].command == ("miseledger", "version")


def test_usage_tracker_declares_no_write_bounded_summary_contract():
    t = managed.resolve("usage-tracker")
    assert t is not None
    assert len(t.surfaces) == 1
    summary = t.surfaces[0]
    assert summary.kind == "summary-json"
    assert summary.command == (
        "usage-tracker",
        "export",
        "--since",
        "30d",
        "--summary-json",
        "--no-write",
    )
    assert summary.read_only is True
    assert summary.timeout_seconds == 30.0
    assert summary.max_chars == 4000
    assert summary.probe == ("usage-tracker", "export", "--help")
    assert summary.probe_contains == ("--since", "--summary-json", "--no-write")


def test_representative_sidecar_manifest_contracts_match_managed_catalog(tmp_path):
    manifests = [
        {
            "name": "usage-tracker",
            "station": "tokens",
            "summary": "local usage export",
            "tools": [
                {
                    "name": "usage-tracker",
                    "command": "usage-tracker",
                    "install": ["pipx", "install", "git+https://github.com/escoffier-labs/usage-tracker"],
                    "surfaces": [
                        {
                            "kind": "summary-json",
                            "command": [
                                "usage-tracker",
                                "export",
                                "--since",
                                "30d",
                                "--summary-json",
                                "--no-write",
                            ],
                            "timeout_seconds": 30,
                            "max_chars": 4000,
                            "probe": ["usage-tracker", "export", "--help"],
                            "probe_contains": ["--since", "--summary-json", "--no-write"],
                        }
                    ],
                }
            ],
        },
        {
            "name": "agentpantry",
            "station": "pantry",
            "summary": "session synchronization",
            "tools": [
                {
                    "name": "agentpantry",
                    "command": "agentpantry",
                    "install": [
                        "go",
                        "install",
                        "github.com/escoffier-labs/agentpantry/cmd/agentpantry@latest",
                    ],
                    "surfaces": [
                        {
                            "kind": "doctor-json",
                            "command": ["agentpantry", "doctor", "--json", "--no-net"],
                            "read_only": False,
                            "timeout_seconds": 10,
                            "probe": ["agentpantry", "doctor", "--help"],
                            "probe_contains": ["-json", "-no-net"],
                        },
                        {
                            "kind": "summary-json",
                            "command": ["agentpantry", "inventory", "--json"],
                            "timeout_seconds": 10,
                            "max_chars": 4000,
                            "probe": ["agentpantry", "inventory", "--help"],
                            "probe_contains": ["-json"],
                        },
                        {
                            "kind": "verify-exit",
                            "command": ["agentpantry", "version", "--json"],
                            "timeout_seconds": 10,
                        },
                    ],
                }
            ],
        },
    ]
    for index, payload in enumerate(manifests):
        root = tmp_path / str(index)
        root.mkdir()
        payload = {"schema": station_manifest.SCHEMA, "lifecycle": "active", **payload}
        (root / "station.json").write_text(json.dumps(payload))
        manifest = station_manifest.load(str(root))
        parity = stations_cmd._managed_parity(manifest, check_managed=True)
        assert parity["status"] == "matched", parity


def test_agent_notify_declares_skip_network_contract():
    t = managed.resolve("agent-notify")
    surfaces = {surface.kind: surface for surface in t.surfaces}
    doctor = surfaces["doctor-json"]
    assert doctor.command == ("agent-notify", "doctor", "--json", "--skip-network")
    assert doctor.timeout_seconds == 10.0
    assert doctor.probe == ("agent-notify", "doctor", "--help")
    assert doctor.probe_contains == ("--json", "--skip-network")
    assert surfaces["verify-exit"].command == ("agent-notify", "version", "--json")


def test_token_glace_declares_utc_summary_contract():
    t = managed.resolve("token-glace")
    surfaces = {surface.kind: surface for surface in t.surfaces}
    assert surfaces["doctor-json"].command == ("token-glace", "doctor", "hooks", "--format", "json")
    summary = surfaces["summary-json"]
    assert summary.command == ("token-glace", "stats", "--format", "json", "--timezone", "utc")
    assert summary.timeout_seconds == 30.0
    assert summary.max_chars == 4000
    assert summary.probe == ("token-glace", "--help")
    assert summary.probe_contains == ("--format", "--timezone")


def test_graphtrail_declares_verified_brigade_install_and_bounded_context_contract():
    t = managed.resolve("graphtrail")
    assert t.install_args == ["brigade", "setup"]
    surfaces = {surface.kind: surface for surface in t.surfaces}
    context = surfaces["brief-markdown"]
    assert context.command == ("graphtrail", "context", "<task>", "--markdown")
    assert context.timeout_seconds == 10.0
    assert context.max_chars == 4000
    assert context.probe == ("graphtrail", "context", "--help")
    assert context.probe_contains == ("--markdown",)
    doctor = surfaces["doctor-json"]
    assert doctor.command == ("graphtrail", "doctor", "--json")
    assert doctor.probe == ("graphtrail", "doctor", "--help")
    assert doctor.probe_contains == ("--json",)
    assert surfaces["verify-exit"].command == ("graphtrail", "--version")


def test_standalone_content_guard_is_not_a_managed_install():
    assert managed.resolve("content-guard") is None
    assert {tool.name for tool in managed.for_station("guard")} == {"plating"}


def test_agent_notify_install_args_use_escoffier_labs():
    t = managed.resolve("agent-notify")
    assert "github.com/escoffier-labs/agent-notify/cmd/agent-notify@latest" in " ".join(t.install_args)
