"""Import, storage and async contracts across supported Python platforms."""

import asyncio
import subprocess
import sys
from unittest.mock import AsyncMock

import pytest

from brigade.grokbot_mcp import OptionalDependencyError
from brigade.dot import access_auth, lifecycle, safe_state
from brigade.dot.events import EventService
from brigade.dot.runtime_config import parse_runtime
from tests.test_dot_runtime import _document


def test_core_imports_without_posix_or_optional_dependencies():
    script = """
import sys
for name in ("fcntl", "pwd", "grp", "resource", "termios", "mcp", "jwt", "anyio"):
    sys.modules[name] = None
import brigade.grokbot_packs
import brigade.dot
from brigade.cli import main
assert main(["--help"]) == 0
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("non_posix", [False, True])
def test_state_open_fails_closed_without_posix_locking(monkeypatch, dot_private_dir, non_posix):
    from brigade.dot.state import BridgeState

    monkeypatch.setitem(sys.modules, "fcntl", None)
    if non_posix:
        monkeypatch.setattr(safe_state, "os", type("Platform", (), {"name": "nt"})())
    with pytest.raises(safe_state.DotError, match="unsafe_state"):
        BridgeState(dot_private_dir)


def test_missing_rsa_support_fails_closed_and_doctor_reports_dependency(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "cryptography", None)
    monkeypatch.setattr(lifecycle.grokbot_mcp, "_load_mcp", lambda: None)
    checks = lifecycle.doctor(tmp_path)
    assert checks[0] == {"check": "dependency", "status": "fail"}
    verifier = access_auth.AccessVerifier(parse_runtime(_document()).auth)
    with pytest.raises(OptionalDependencyError):
        asyncio.run(verifier.verify({"headers": []}))


def test_missing_pyjwt_rsa_algorithm_fails_closed(monkeypatch):
    jwt = pytest.importorskip("jwt")
    monkeypatch.setattr(jwt.algorithms, "has_crypto", False)
    verifier = access_auth.AccessVerifier(parse_runtime(_document()).auth)
    with pytest.raises(OptionalDependencyError):
        asyncio.run(verifier.verify({"headers": []}))


def test_signing_key_deadline_on_python310_api(monkeypatch):
    anyio = pytest.importorskip("anyio")
    monkeypatch.delattr(asyncio, "timeout", raising=False)
    fail_after = anyio.fail_after
    monkeypatch.setattr(anyio, "fail_after", lambda seconds: fail_after(0.001))

    async def stalled():
        await asyncio.sleep(60)

    verifier = access_auth.AccessVerifier(parse_runtime(_document()).auth, fetch=stalled)
    with pytest.raises(TimeoutError):
        asyncio.run(verifier.signing_key("key"))
    assert verifier.keys == {}


def test_event_workers_run_concurrently_and_cancel_siblings(monkeypatch):
    pytest.importorskip("anyio")
    monkeypatch.delattr(asyncio, "TaskGroup", raising=False)
    service = object.__new__(EventService)
    service.hub = object()
    service.hub_poll_seconds = 30
    started, cancelled = set(), set()

    async def periodic(action, interval):
        started.add(action.__name__)
        try:
            while len(started) < 3:
                await asyncio.sleep(0)
            if action.__name__ == "deliver":
                raise RuntimeError("worker failed")
            await asyncio.sleep(60)
        finally:
            cancelled.add(action.__name__)

    monkeypatch.setattr(service, "periodic", periodic)
    with pytest.raises(Exception, match="unhandled errors in a TaskGroup") as caught:
        asyncio.run(service.run())
    assert str(caught.value.exceptions[0]) == "worker failed"
    assert started == {"poll_requests", "deliver", "poll_hub"}
    assert cancelled == started


def test_request_body_deadline_returns_408(monkeypatch):
    anyio = pytest.importorskip("anyio")
    from tests.test_dot_lifecycle import config, verifier

    fail_after = anyio.fail_after
    monkeypatch.setattr(anyio, "fail_after", lambda seconds: fail_after(0.001))
    monkeypatch.delattr(asyncio, "timeout", raising=False)
    gate = lifecycle._DotGate(AsyncMock(), config(), verifier())
    sent = []

    async def receive():
        await asyncio.sleep(60)

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "path": "/mcp", "method": "POST", "headers": [(b"host", b"dot.example.test")]}
    asyncio.run(gate(scope, receive, send))
    assert sent[0]["status"] == 408
