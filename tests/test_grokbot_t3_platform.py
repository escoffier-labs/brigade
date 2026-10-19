"""Unsupported platforms fail before private storage or command side effects."""

from types import SimpleNamespace

import pytest

from brigade.grokbot_t3 import runtime_config as rc
from brigade.grokbot_t3.contracts import T3DispatchError
from brigade.grokbot_t3.ledger import Ledger
from brigade.grokbot_t3.watch import _append_log


@pytest.mark.parametrize("operation", ["read", "ledger", "log"])
@pytest.mark.parametrize("existing_parent", [False, True])
def test_windows_storage_refuses_before_touching_disk(tmp_path, monkeypatch, operation, existing_parent):
    monkeypatch.setattr(rc, "sys", SimpleNamespace(platform="win32"))
    parent = tmp_path / "private"
    if existing_parent:
        parent.mkdir()
    path = parent / "state"
    with pytest.raises(T3DispatchError, match="unsupported on Windows") as error:
        if operation == "read":
            rc.read_owner_file(path, max_bytes=100)
        elif operation == "ledger":
            Ledger(path)
        else:
            _append_log(path, {"event": "test"})
    assert error.value.code == "unsupported-platform"
    assert not path.exists()
    assert parent.exists() == existing_parent


@pytest.mark.parametrize("command", ["watch", "intake", "install-watch", "install-intake"])
def test_windows_cli_reports_platform_before_loading_config(tmp_path, monkeypatch, capsys, command):
    from brigade.grokbot_t3.__main__ import main

    monkeypatch.setattr(rc, "sys", SimpleNamespace(platform="win32"))
    args = [command, "--target", str(tmp_path / "missing")]
    if command.startswith("install"):
        args += ["--out-dir", str(tmp_path / "units")]
    assert main(args) == 2
    assert "unsupported on Windows" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("operation", ["listener", "watch", "intake", "reference"])
def test_windows_lifecycle_refuses_before_loading_instance(tmp_path, monkeypatch, operation):
    from brigade.grokbot_t3 import lifecycle

    monkeypatch.setattr(rc, "sys", SimpleNamespace(platform="win32"))
    with pytest.raises(T3DispatchError, match="unsupported on Windows"):
        if operation == "listener":
            lifecycle.build_listener_from_target(
                tmp_path, bind=None, allowed_hosts=[], allowed_origins=[], bearer_file=None, bearer_env=None
            )
        elif operation == "reference":
            rc.validate_runtime_reference(str(tmp_path / "missing.json"))
        else:
            getattr(lifecycle, f"{operation}_once")(tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("command", ["serve", "setup", "doctor", "canary"])
def test_windows_public_cli_explains_platform(tmp_path, monkeypatch, capsys, command):
    from brigade import cli

    monkeypatch.setattr(rc, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setenv("TEST_DISPATCH_BEARER", "test-value-that-is-long-enough-123456")
    args = ["run", "cloud", "grokbot"]
    if command == "serve":
        args += ["serve", "--pack", "t3-dispatch", "--bearer-env", "TEST_DISPATCH_BEARER"]
    else:
        args += ["pack", command, "--id", "t3-dispatch"]
    args += ["--target", str(tmp_path)]
    if command == "setup":
        args += ["--bearer-env", "TEST_DISPATCH_BEARER", "--runtime-path", str(tmp_path / "runtime.json")]
    assert cli.main(args) != 0
    output = capsys.readouterr()
    assert "unsupported on Windows" in output.out + output.err
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("apply", [False, True])
def test_windows_install_service_keeps_the_platform_error(tmp_path, monkeypatch, apply):
    from brigade import grokbot_packs

    monkeypatch.setattr(rc, "sys", SimpleNamespace(platform="win32"))
    out_dir = tmp_path / "units"
    with pytest.raises(grokbot_packs.PackError, match="unsupported on Windows"):
        if apply:
            grokbot_packs.apply_install_service(tmp_path, grokbot_packs.T3_DISPATCH_PACK_ID, out_dir=out_dir)
        else:
            grokbot_packs.render_install_service(tmp_path, grokbot_packs.T3_DISPATCH_PACK_ID)
    assert not out_dir.exists()
