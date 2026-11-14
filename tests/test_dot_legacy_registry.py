"""Surviving Dot lifecycle checks ported before deleting the queue pack tests."""

import json
from pathlib import Path
import pytest
from brigade.dot import legacy_registry as grokbot_packs
from tests._posix import requires_dirfd

SECRET = "not-a-real-token"
PUBLIC_HOST = "connector.example.test"


def _reject(reason: str):
    return pytest.raises(grokbot_packs.PackError, match=reason)


def _mark_host_local(target: Path, pack_id: str) -> None:
    path = grokbot_packs.instance_config_path(target, pack_id)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["host_local"] = True
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)


@requires_dirfd
def test_pack_doctor_reports_manual_no_public_route_for_connector(tmp_path: Path, memory_runtime, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    monkeypatch.setattr(grokbot_packs, "_cloudflared_metrics_binds", lambda: ())
    grokbot_packs.apply_setup(
        tmp_path,
        "dot",
        runtime_path=memory_runtime,
    )
    checks = grokbot_packs.doctor(tmp_path, "dot")
    rendered = json.dumps(checks)
    assert {
        "check": "no-public-route",
        "status": "manual",
    } in checks
    assert all(check["status"] != "fail" or check["check"] != "no-public-route" for check in checks)
    assert SECRET not in rendered
    assert str(tmp_path) not in rendered


@requires_dirfd
def test_pack_doctor_stays_quiet_when_connector_has_public_host(tmp_path: Path, memory_runtime, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    monkeypatch.setattr(grokbot_packs, "_cloudflared_metrics_binds", lambda: ())
    grokbot_packs.apply_setup(
        tmp_path,
        "dot",
        allowed_hosts=[PUBLIC_HOST],
        runtime_path=memory_runtime,
    )
    checks = grokbot_packs.doctor(tmp_path, "dot")
    rendered = json.dumps(checks)
    assert all(check["check"] != "no-public-route" for check in checks)
    assert SECRET not in rendered
    assert PUBLIC_HOST not in rendered
    assert str(tmp_path) not in rendered


@requires_dirfd
def test_pack_doctor_stays_quiet_when_connector_is_host_local(tmp_path: Path, memory_runtime, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    monkeypatch.setattr(grokbot_packs, "_cloudflared_metrics_binds", lambda: ())
    grokbot_packs.apply_setup(
        tmp_path,
        "dot",
        runtime_path=memory_runtime,
    )
    _mark_host_local(tmp_path, "dot")
    checks = grokbot_packs.doctor(tmp_path, "dot")
    rendered = json.dumps(checks)
    assert all(check["check"] != "no-public-route" for check in checks)
    assert SECRET not in rendered
    assert str(tmp_path) not in rendered


@pytest.fixture
def dot_runtime(dot_private_dir):
    from tests.test_dot_runtime import _document, _write_private

    runtime = dot_private_dir.parent / "runtime.json"
    _write_private(runtime, json.dumps(_document()).encode())
    return runtime


@pytest.fixture
def memory_runtime(dot_private_dir):
    from tests.test_dot_runtime import _write_private
    from tests.test_dot_runtime import _document

    runtime = dot_private_dir.parent / "memory-runtime.json"
    _write_private(runtime, json.dumps(_document()).encode())
    return runtime


@requires_dirfd
def test_dot_setup_has_runtime_without_bearer(tmp_path, dot_runtime, monkeypatch):
    payloads = []
    validate = grokbot_packs._validate_instance_config

    def capture(payload, pack_id):
        payloads.append(dict(payload))
        return validate(payload, pack_id)

    monkeypatch.setattr(grokbot_packs, "_validate_instance_config", capture)
    grokbot_packs.preview_setup(tmp_path, "dot", runtime_path=dot_runtime)
    assert payloads[-1]["runtime_path"] == str(dot_runtime)
    assert "bearer" not in payloads[-1]
    assert not grokbot_packs.instance_config_path(tmp_path, "dot").exists()
    grokbot_packs.apply_setup(tmp_path, "dot", runtime_path=dot_runtime)
    config = grokbot_packs._load_instance_config(tmp_path, "dot")
    assert "bearer" not in config
    assert config["runtime_path"] == str(dot_runtime)
    config["bearer"] = {"kind": "env", "name": "TEST_BEARER"}
    grokbot_packs.instance_config_path(tmp_path, "dot").write_text(json.dumps(config), encoding="utf-8")
    with _reject("unexpected-key"):
        grokbot_packs._load_instance_config(tmp_path, "dot")


@requires_dirfd
@pytest.mark.parametrize(
    "kwargs",
    [
        {"bearer_file": Path("/run/example.token")},
        {"bearer_env": "TEST_BEARER"},
        {"bearer": {"kind": "env", "name": "TEST_BEARER"}},
        {"ledger_path": "/srv/example/ledger"},
        {"owner_workspace": "/srv/example"},
    ],
)
def test_dot_setup_rejects_unexpected_fields(tmp_path, dot_runtime, kwargs):
    with _reject("unexpected-key"):
        grokbot_packs.preview_setup(tmp_path, "dot", runtime_path=dot_runtime, **kwargs)


@requires_dirfd
def test_dot_setup_validates_runtime(tmp_path):
    with _reject("missing-path-reference"):
        grokbot_packs.preview_setup(tmp_path, "dot")
    with _reject("unsafe-path"):
        grokbot_packs.preview_setup(tmp_path, "dot", runtime_path="relative.json")


def test_dot_lifecycle_delegates(tmp_path, monkeypatch):
    from brigade import dot

    calls = []

    def doctor(target):
        calls.append(("doctor", target))
        return [{"check": "dot", "status": "ok"}]

    def canary(target):
        calls.append(("canary", target))
        return {"ok": True, "pack_id": "dot"}

    def render(target, *, python):
        calls.append(("render", target, python))
        return "dot unit"

    def write(target, out_dir, *, force):
        calls.append(("write", target, out_dir, force))
        return out_dir / "brigade-dot.service"

    monkeypatch.setattr(dot, "doctor", doctor)
    monkeypatch.setattr(dot, "canary", canary)
    monkeypatch.setattr(dot, "render_unit", render)
    monkeypatch.setattr(dot, "write_unit", write)
    assert {"check": "dot", "status": "ok"} in grokbot_packs.doctor(tmp_path, "dot")
    assert grokbot_packs.canary(tmp_path, "dot") == {"ok": True, "pack_id": "dot"}
    assert grokbot_packs.render_install_service(tmp_path, "dot") == "dot unit"
    assert (
        grokbot_packs.apply_install_service(tmp_path, "dot", out_dir=tmp_path, force=True)["unit"]
        == "brigade-dot.service"
    )
    import sys

    assert calls == [
        ("doctor", tmp_path),
        ("canary", tmp_path),
        ("render", tmp_path, sys.executable),
        ("write", tmp_path, tmp_path, True),
    ]
