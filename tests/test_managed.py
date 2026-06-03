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
    assert stations <= {"memory", "guard", "tokens", "pantry"}


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
        return managed.proc.Result(code=2, stdout='{"configured": false, "fail_count": 1, "warn_count": 0, "checks": []}', stderr="")

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
