"""Provider lifecycle and public native receipt contracts, using local fakes only."""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from brigade import fleet_action_adapters as adapters
from brigade.fleet_action_auth import ActionError

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Native transports require POSIX process groups and pipes")

REFERENCE = "example-target-example-controller-12345678-1234-1234-1234-123456789abc"
THREAD = "87654321-4321-4321-4321-cba987654321"


def executable(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "native-fixture"
    path.write_text(f"#!{sys.executable}\n" + body)
    path.chmod(0o700)
    return path


def t3_action() -> dict:
    return {"action_id": "aa_fixture", "request": {"target": {"family": "t3"}, "model_id": "example-seat"}}


def binding(**extra) -> dict:
    return {"host": "example-target", "repository": "example-repo", "source_revision": "a" * 40, **extra}


def json_executable(tmp_path: Path, payload: dict, *, code: int = 0) -> Path:
    return executable(
        tmp_path,
        "import json, pathlib, sys\n"
        "pathlib.Path(__file__).with_suffix('.argv').write_text(json.dumps(sys.argv[1:]))\n"
        f"print({json.dumps(payload)!r})\n"
        f"sys.exit({code})\n",
    )


@pytest.mark.parametrize("stream", [1, 2], ids=["stdout", "stderr"])
def test_stream_overflow_stops_indefinite_writer_and_reaps_child(tmp_path, monkeypatch, stream):
    monkeypatch.setattr(adapters, "MAX_OUTPUT", 8192)
    path = executable(
        tmp_path,
        "import os, pathlib\n"
        "pathlib.Path(__file__).with_suffix('.pid').write_text(str(os.getpid()))\n"
        f"while True: os.write({stream}, b'private-output' * 1024)\n",
    )
    started = time.monotonic()
    with pytest.raises(ActionError, match="^transport-acceptance-unknown$"):
        adapters.invoke(path, ["private-argument"], timeout=2)
    assert time.monotonic() - started < 1.5
    pid = int(path.with_suffix(".pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    # No provider stdout/stderr artifacts are left in the invocation directory.
    assert {p.name for p in tmp_path.iterdir()} == {path.name, path.with_suffix(".pid").name}


@pytest.mark.parametrize("failure", ["overflow", "timeout"])
def test_cleanup_stops_signal_ignoring_descendant_holding_pipes(tmp_path, monkeypatch, failure):
    if not Path("/proc/self/stat").exists():
        pytest.skip("Linux /proc distinguishes a dead adopted zombie from a live writer")
    monkeypatch.setattr(adapters, "MAX_OUTPUT", 8192)
    path = executable(
        tmp_path,
        "import os, pathlib, signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "child = os.fork()\n"
        "if child:\n"
        "    pathlib.Path(__file__).with_suffix('.child').write_text(str(child))\n"
        "    os._exit(0)\n"
        + ("while True: os.write(2, b'private-output' * 1024)\n" if failure == "overflow" else "time.sleep(30)\n"),
    )
    try:
        with pytest.raises(ActionError, match="^transport-acceptance-unknown$"):
            adapters.invoke(path, ["private-argument"], timeout=0.5)
        pid = int(path.with_suffix(".child").read_text())
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            stat = Path(f"/proc/{pid}/stat")
            if not stat.exists() or stat.read_text().split()[2] == "Z":
                break
            time.sleep(0.01)
        else:
            pytest.fail("provider descendant is still running")
    finally:
        child_file = path.with_suffix(".child")
        if child_file.exists():
            try:
                os.kill(int(child_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_both_streams_at_cap_accept_complete_json_without_deadlock(tmp_path, monkeypatch):
    monkeypatch.setattr(adapters, "MAX_OUTPUT", 8192)
    path = executable(
        tmp_path,
        "import os\nos.write(2, b'e' * 8192)\nos.write(1, b'{\"ok\":true}' + b' ' * (8192 - 11))\n",
    )
    assert adapters.invoke(path, [], timeout=2) == {"ok": True}


def test_non_posix_transport_is_refused_before_launch(tmp_path, monkeypatch):
    path = executable(tmp_path, "raise AssertionError('must not execute')\n")
    monkeypatch.setattr(adapters, "os", SimpleNamespace(name="nt"))
    with pytest.raises(ActionError, match="^transport-platform-unsupported$"):
        adapters.invoke(path, [])


@pytest.mark.parametrize(
    "native,expected",
    [
        ("queued", "accepted"),
        ("preflight", "accepted"),
        ("dispatching", "accepted"),
        ("running", "running"),
        ("needs_attention", "needs-input"),
        ("succeeded", "finished"),
        ("failed", "failed"),
        ("interrupted", "failed"),
        ("blocked_offline", "unknown"),
        ("unknown", "unknown"),
    ],
)
def test_real_t3_states_and_native_thread_survive_recovery(tmp_path, native, expected):
    path = json_executable(
        tmp_path,
        {
            "schema": "t3-fleet.result.v1",
            "request_id": REFERENCE,
            "state": native,
            "thread_id": THREAD,
            "output": "agent claims the alert is fixed",
        },
    )
    assert adapters.recover(t3_action(), path, REFERENCE) == {
        "status": expected,
        "destination_ref": REFERENCE,
        "destination_thread": THREAD,
    }
    assert json.loads(path.with_suffix(".argv").read_text()) == ["recover", "--request-id", REFERENCE]


@pytest.mark.parametrize("state", ["queued", "running", "succeeded"])
@pytest.mark.parametrize("reference", [None, "", "x" * 129])
def test_execution_receipts_require_bounded_native_reference(tmp_path, state, reference):
    path = json_executable(tmp_path, {"schema": "t3-fleet.result.v1", "request_id": reference, "state": state})
    with pytest.raises(ActionError, match="^transport-acceptance-unknown$"):
        adapters.dispatch(t3_action(), binding(), path, tmp_path / "prompt")


@pytest.mark.parametrize("thread", [None, "", "not-a-uuid", "x" * 10000])
def test_unusable_optional_thread_is_not_forwarded(tmp_path, thread):
    path = json_executable(
        tmp_path,
        {"schema": "t3-fleet.result.v1", "request_id": REFERENCE, "state": "running", "thread_id": thread},
    )
    assert adapters.recover(t3_action(), path, REFERENCE) == {"status": "running", "destination_ref": REFERENCE}


@pytest.mark.parametrize("runtime_mode", [None, "approval-required", "auto-accept-edits", "auto", "full-access"])
def test_peer_submit_uses_client_handle_and_protected_runtime_mode(tmp_path, runtime_mode):
    path = json_executable(
        tmp_path, {"schema": "t3-fleet.result.v1", "request_id": REFERENCE, "state": "running", "thread_id": THREAD}
    )
    local = binding(**({"runtime_mode": runtime_mode} if runtime_mode is not None else {}))
    assert adapters.initial_reference(t3_action(), local) is None
    receipt = adapters.dispatch(t3_action(), local, path, tmp_path / "prompt")
    assert receipt == {"status": "running", "destination_ref": REFERENCE, "destination_thread": THREAD}
    assert json.loads(path.with_suffix(".argv").read_text()) == [
        "submit",
        "--host",
        "example-target",
        "--repository",
        "example-repo",
        "--source-revision",
        "a" * 40,
        "--seat",
        "example-seat",
        "--title",
        "Repair alert aa_fixture",
        "--prompt-file",
        str(tmp_path / "prompt"),
        "--runtime-mode",
        runtime_mode or "approval-required",
    ]


@pytest.mark.parametrize("runtime_mode", ["", "bypass", None, [], {"mode": "full-access"}])
def test_invalid_runtime_mode_refused_before_provider_call(tmp_path, runtime_mode):
    path = executable(tmp_path, "raise AssertionError('must not execute')\n")
    with pytest.raises(ActionError, match="^invalid-runtime-mode$"):
        adapters.dispatch(t3_action(), binding(runtime_mode=runtime_mode), path, tmp_path / "prompt")


def test_peer_without_returned_handle_holds_unknown_without_resubmit(tmp_path):
    path = executable(tmp_path, "raise AssertionError('recovery must not execute without a handle')\n")
    assert adapters.initial_reference(t3_action(), binding()) is None
    assert adapters.recover(t3_action(), path, None) == {"status": "unknown"}


def test_t3_unknown_error_retains_returned_handle_without_private_detail(tmp_path):
    path = json_executable(
        tmp_path,
        {
            "schema": "t3-fleet.error.v1",
            "error": {"code": "unknown_state", "detail": "private-output"},
            "request_id": REFERENCE,
        },
        code=1,
    )
    assert adapters.dispatch(t3_action(), binding(), path, tmp_path / "prompt") == {
        "status": "unknown",
        "destination_ref": REFERENCE,
    }


@pytest.mark.parametrize("status", ["ok", "completed"])
def test_openclaw_gateway_json_and_explicit_timeout(tmp_path, status):
    path = json_executable(tmp_path, {"status": status, "runId": THREAD, "result": {"payloads": ["private-output"]}})
    action = {"action_id": "aa_fixture", "request": {"target": {"family": "openclaw"}}}
    assert adapters.dispatch(action, {"agent_id": "example-agent"}, path, tmp_path / "prompt") == {
        "status": "finished",
        "destination_ref": THREAD,
    }
    args = json.loads(path.with_suffix(".argv").read_text())
    assert args == [
        "agent",
        "--agent",
        "example-agent",
        "--session-key",
        "brigade-action-aa_fixture",
        "--message-file",
        str(tmp_path / "prompt"),
        "--timeout",
        "3500",
        "--json",
    ]
