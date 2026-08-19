"""Mutation tests for issue #1014: unbounded hook stdin before timeout.

The unfixed hook_run path read and JSON-decoded stdin with no size bound,
then started the timed worker. These tests reproduce that attack on a
replica of the old path and confirm the fix rejects it before worker
creation. Reverting hook_run to ``sys.stdin.read()`` + ``json.loads``
makes this module fail.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from brigade.claude_hooks import envelope, runtime
from brigade.claude_hooks.package import PACKAGE_REF


class _CountingBytesIO(io.BytesIO):
    def __init__(self, payload: bytes) -> None:
        super().__init__(payload)
        self.bytes_read = 0

    def read(self, size: int | None = -1) -> bytes:
        chunk = super().read(-1 if size is None else size)
        self.bytes_read += len(chunk)
        return chunk


class _StdinProxy:
    """stdin stand-in whose text ``read()`` is the unfixed attack surface."""

    def __init__(self, payload: bytes) -> None:
        self.buffer = _CountingBytesIO(payload)

    def read(self, *args: object, **kwargs: object) -> str:
        raise AssertionError("unbounded sys.stdin.read() is the #1014 attack surface")


class _UnfixedCompatibleStdin:
    """stdin stand-in that still allows the pre-#1014 unbounded text read."""

    def __init__(self, payload: bytes) -> None:
        self.buffer = _CountingBytesIO(payload)
        self._text = io.TextIOWrapper(io.BytesIO(payload), encoding="utf-8")

    def read(self, *args: object, **kwargs: object) -> str:
        return self._text.read(*args, **kwargs)


def _unfixed_load_hook_stdin(raw: str) -> dict[str, Any]:
    """Replica of the pre-#1014 hook_run stdin path (the mutant)."""
    parsed = json.loads(raw) if raw.strip() else {}
    if not isinstance(parsed, dict):
        raise TypeError("hook stdin must be a JSON object")
    return parsed


def _nested_object(depth: int) -> dict[str, Any]:
    current: dict[str, Any] = {"leaf": True}
    for _ in range(depth):
        current = {"n": current}
    return current


def test_mutation_oversized_valid_json_is_accepted_by_unfixed_path_and_refuted_by_fix(monkeypatch, capsys):
    pad = "x" * runtime.MAX_HOOK_STDIN_BYTES
    raw = json.dumps({"cwd": ".", "pad": pad})
    assert len(raw.encode("utf-8")) > runtime.MAX_HOOK_STDIN_BYTES

    mutant = _unfixed_load_hook_stdin(raw)
    assert mutant["pad"] == pad

    worker_calls: list[tuple[str, str]] = []

    def _capture(event: str, payload_raw: str) -> dict[str, Any]:
        worker_calls.append((event, payload_raw))
        return {}

    monkeypatch.setattr(runtime, "_run_timed_handle_payload", _capture)
    capsys.readouterr()
    assert runtime.hook_run(event="SessionStart", package=PACKAGE_REF, stdin_text=raw) == 0
    assert json.loads(capsys.readouterr().out) == envelope.degraded_envelope("SessionStart")
    assert worker_calls == []


def test_mutation_trailing_flood_is_read_unbounded_by_unfixed_path_and_refuted_by_reader():
    prefix = b'{"cwd":"."}'
    flood = prefix + (b"x" * (runtime.MAX_HOOK_STDIN_BYTES * 4))

    unfixed_stream = _CountingBytesIO(flood)
    unfixed_raw = unfixed_stream.read()
    assert unfixed_stream.bytes_read == len(flood)
    with pytest.raises(json.JSONDecodeError):
        json.loads(unfixed_raw.decode("utf-8"))

    bounded = _CountingBytesIO(flood)
    with pytest.raises(runtime.HookDegraded, match="exceeds"):
        runtime._read_limited_binary(bounded, limit=runtime.MAX_HOOK_STDIN_BYTES)
    assert bounded.bytes_read <= runtime.MAX_HOOK_STDIN_BYTES + runtime._HOOK_STDIN_READ_CHUNK
    assert bounded.bytes_read < len(flood)


def test_mutation_nested_string_cap_is_accepted_by_unfixed_path_and_refuted_by_fix():
    oversized = "y" * (runtime.MAX_HOOK_STDIN_STRING_CHARS + 1)
    raw = json.dumps({"cwd": ".", "pad": oversized})
    assert len(raw.encode("utf-8")) <= runtime.MAX_HOOK_STDIN_BYTES

    mutant = _unfixed_load_hook_stdin(raw)
    assert mutant["pad"] == oversized

    with pytest.raises(runtime.HookDegraded, match="string exceeds"):
        runtime._parse_hook_stdin(raw.encode("utf-8"))


def test_mutation_nested_collection_cap_is_accepted_by_unfixed_path_and_refuted_by_fix():
    items = [0] * (runtime.MAX_HOOK_STDIN_COLLECTION_ITEMS + 1)
    raw = json.dumps({"cwd": ".", "items": items})
    assert len(raw.encode("utf-8")) <= runtime.MAX_HOOK_STDIN_BYTES

    mutant = _unfixed_load_hook_stdin(raw)
    assert mutant["items"] == items

    with pytest.raises(runtime.HookDegraded, match="array exceeds"):
        runtime._parse_hook_stdin(raw.encode("utf-8"))


def test_mutation_object_collection_cap_is_accepted_by_unfixed_path_and_refuted_by_fix():
    fields = {f"k{index}": index for index in range(runtime.MAX_HOOK_STDIN_COLLECTION_ITEMS + 1)}
    raw = json.dumps(fields)
    assert len(raw.encode("utf-8")) <= runtime.MAX_HOOK_STDIN_BYTES

    mutant = _unfixed_load_hook_stdin(raw)
    assert len(mutant) == runtime.MAX_HOOK_STDIN_COLLECTION_ITEMS + 1

    with pytest.raises(runtime.HookDegraded, match="object exceeds"):
        runtime._parse_hook_stdin(raw.encode("utf-8"))


def test_mutation_trailing_input_within_limit_is_rejected_before_worker(monkeypatch, capsys):
    raw = json.dumps({"cwd": ".", "session_id": "s"}) + '\n{"extra":true}'
    mutant_ok = False
    try:
        _unfixed_load_hook_stdin(raw)
    except json.JSONDecodeError:
        mutant_ok = True
    assert mutant_ok is True

    worker_calls: list[object] = []
    monkeypatch.setattr(runtime, "_run_timed_handle_payload", lambda *args, **kwargs: worker_calls.append(args) or {})
    capsys.readouterr()
    assert runtime.hook_run(event="SessionStart", package=PACKAGE_REF, stdin_text=raw) == 0
    assert json.loads(capsys.readouterr().out) == envelope.degraded_envelope("SessionStart")
    assert worker_calls == []
    with pytest.raises(runtime.HookDegraded, match="trailing input"):
        runtime._parse_hook_stdin(raw.encode("utf-8"))


def test_hook_run_reads_real_stdin_through_limited_binary_reader(monkeypatch, capsys, tmp_path: Path):
    payload = {"session_id": "s", "cwd": str(tmp_path), "hook_event_name": "SessionStart"}
    raw = json.dumps(payload).encode("utf-8")
    proxy = _StdinProxy(raw)
    worker_calls: list[str] = []

    def _capture(event: str, payload_raw: str) -> dict[str, Any]:
        worker_calls.append(payload_raw)
        return {}

    monkeypatch.setattr(sys, "stdin", proxy)
    monkeypatch.setattr(runtime, "_run_timed_handle_payload", _capture)
    capsys.readouterr()
    assert runtime.hook_run(event="SessionStart", package=PACKAGE_REF) == 0
    assert json.loads(capsys.readouterr().out) == envelope.empty_envelope("SessionStart")
    assert worker_calls == [raw.decode("utf-8")]
    assert proxy.buffer.bytes_read == len(raw)


def test_hook_run_rejects_oversized_binary_stdin_before_worker(monkeypatch, capsys):
    raw = b'{"pad":"' + (b"z" * runtime.MAX_HOOK_STDIN_BYTES) + b'"}'
    proxy = _UnfixedCompatibleStdin(raw)
    worker_calls: list[object] = []
    monkeypatch.setattr(sys, "stdin", proxy)
    monkeypatch.setattr(runtime, "_run_timed_handle_payload", lambda *args, **kwargs: worker_calls.append(args) or {})
    capsys.readouterr()
    assert runtime.hook_run(event="SessionStart", package=PACKAGE_REF) == 0
    assert json.loads(capsys.readouterr().out) == envelope.degraded_envelope("SessionStart")
    assert worker_calls == []
    assert proxy.buffer.bytes_read <= runtime.MAX_HOOK_STDIN_BYTES + runtime._HOOK_STDIN_READ_CHUNK


def test_parse_hook_stdin_accepts_empty_and_whitespace_only():
    assert runtime._parse_hook_stdin(b"") == {}
    assert runtime._parse_hook_stdin(b"  \n\t") == {}


def test_parse_hook_stdin_rejects_non_object():
    with pytest.raises(runtime.HookDegraded, match="JSON object"):
        runtime._parse_hook_stdin(b"[1, 2]")


def test_parse_hook_stdin_rejects_invalid_utf8():
    with pytest.raises(runtime.HookDegraded, match="UTF-8"):
        runtime._parse_hook_stdin(b'{"cwd":"\xff"}')


def test_enforce_shape_rejects_nesting_deeper_than_cap():
    raw = json.dumps(_nested_object(runtime.MAX_HOOK_STDIN_NESTING_DEPTH + 1))
    mutant = _unfixed_load_hook_stdin(raw)
    assert "n" in mutant
    with pytest.raises(runtime.HookDegraded, match="nesting exceeds"):
        runtime._parse_hook_stdin(raw.encode("utf-8"))


def test_fork_worker_applies_bounds_before_process_start(monkeypatch):
    started: list[object] = []

    class _BoomProcess:
        def __init__(self, *args: object, **kwargs: object) -> None:
            started.append("created")

        def start(self) -> None:
            started.append("started")

        def is_alive(self) -> bool:
            return False

    class _Ctx:
        def Queue(self) -> object:
            return object()

        def Process(self, *args: object, **kwargs: object) -> _BoomProcess:
            return _BoomProcess()

    monkeypatch.setattr(runtime.mp, "get_context", lambda _name: _Ctx())
    raw = json.dumps({"pad": "n" * (runtime.MAX_HOOK_STDIN_STRING_CHARS + 1)})
    with pytest.raises(runtime.HookDegraded, match="string exceeds"):
        runtime._fork_timed_handle_payload_worker("SessionStart", raw, timeout=1.0)
    assert started == []
