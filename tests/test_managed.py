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
    assert stations <= {"memory", "guard", "tokens"}


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
