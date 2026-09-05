from __future__ import annotations

import math
import os
import stat
from types import SimpleNamespace

import pytest

from brigade import attestation_input


def test_strict_json_rejects_duplicate_names() -> None:
    source = b'{"version":1,"nested":{"name":"ok"}}'

    assert attestation_input.strict_json_loads(source) == {
        "version": 1,
        "nested": {"name": "ok"},
    }

    with pytest.raises(attestation_input.AttestationInputError) as exc_info:
        attestation_input.strict_json_loads('{"name":"first","name":"second"}')

    assert str(exc_info.value) == "JSON contains duplicate property names"


@pytest.mark.parametrize("source", ['{"value":NaN}', '{"value":Infinity}', '{"value":1e9999}'])
def test_strict_json_loads_rejects_nonfinite_numbers(source: str) -> None:
    with pytest.raises(attestation_input.AttestationInputError, match="finite"):
        attestation_input.strict_json_loads(source)


def test_mapping_validation_rejects_unsafe_values() -> None:
    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic

    with pytest.raises(attestation_input.AttestationInputError, match="cycle"):
        attestation_input.validate_json_value(cyclic)
    with pytest.raises(attestation_input.AttestationInputError, match="string"):
        attestation_input.validate_json_value({1: "no"})
    with pytest.raises(attestation_input.AttestationInputError, match="Unicode"):
        attestation_input.validate_json_value({"value": "\ud800"})
    with pytest.raises(attestation_input.AttestationInputError, match="finite"):
        attestation_input.validate_json_value({"value": math.inf})


def test_json_limits_are_inclusive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(attestation_input, "MAX_JSON_NODES", 2)
    monkeypatch.setattr(attestation_input, "MAX_JSON_DEPTH", 10)

    assert attestation_input.strict_json_loads("[0]") == [0]
    with pytest.raises(attestation_input.AttestationInputError, match="node"):
        attestation_input.strict_json_loads("[0,1]")
    monkeypatch.setattr(attestation_input, "MAX_JSON_NODES", 10)
    monkeypatch.setattr(attestation_input, "MAX_JSON_DEPTH", 2)
    with pytest.raises(attestation_input.AttestationInputError, match="depth"):
        attestation_input.strict_json_loads("[[[0]]]")


def test_mapping_validation_returns_plain_snapshot_and_checks_root_depth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = {"nested": {"values": ["one"]}}

    snapshot = attestation_input.validate_json_value(value)

    assert snapshot == value
    assert snapshot is not value
    assert snapshot["nested"] is not value["nested"]
    assert snapshot["nested"]["values"] is not value["nested"]["values"]

    monkeypatch.setattr(attestation_input, "MAX_JSON_DEPTH", 2)
    assert attestation_input.validate_json_value({"nested": {"value": 1}}) == {"nested": {"value": 1}}
    with pytest.raises(attestation_input.AttestationInputError, match="depth"):
        attestation_input.validate_json_value({"nested": {"deeper": {}}})


def test_mapping_input_cannot_bypass_document_byte_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(attestation_input, "MAX_JSON_BYTES", 10)

    assert attestation_input.validate_json_value({"x": "ab"}) == {"x": "ab"}
    with pytest.raises(attestation_input.AttestationInputError, match="byte limit"):
        attestation_input.validate_json_value({"x": "abc"})


def test_integer_byte_limit_includes_exact_boundary_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(attestation_input, "MAX_JSON_BYTES", 1)

    assert attestation_input.validate_json_value(8) == 8
    with pytest.raises(attestation_input.AttestationInputError, match="byte limit"):
        attestation_input.validate_json_value(10)


def test_strict_json_rejects_oversized_text_before_encoding(monkeypatch: pytest.MonkeyPatch) -> None:
    class TextThatMustNotBeEncoded(str):
        def encode(self, *args: object, **kwargs: object) -> bytes:
            raise AssertionError("oversized text should be rejected before encoding")

    monkeypatch.setattr(attestation_input, "MAX_JSON_BYTES", 10)

    with pytest.raises(attestation_input.AttestationInputError, match="byte limit"):
        attestation_input.strict_json_loads(TextThatMustNotBeEncoded("x" * 11), max_bytes=10)


def test_strict_json_normalizes_decoder_value_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def invalid_decoder(*args: object, **kwargs: object) -> object:
        raise ValueError("synthetic decoder failure")

    monkeypatch.setattr(attestation_input.json, "loads", invalid_decoder)

    with pytest.raises(attestation_input.AttestationInputError, match="JSON document is invalid"):
        attestation_input.strict_json_loads("{}")


def test_mapping_validation_normalizes_scalar_serialization_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    def invalid_scalar(*args: object, **kwargs: object) -> str:
        raise ValueError("synthetic serialization failure")

    monkeypatch.setattr(attestation_input.json, "dumps", invalid_scalar)

    with pytest.raises(attestation_input.AttestationInputError, match="cannot be serialized"):
        attestation_input.validate_json_value({"value": 1})


def test_dsse_base64_has_strict_bounds() -> None:
    decoded = attestation_input.decode_dsse_base64
    assert decoded("+/8=", label="payload", max_bytes=2) == b"\xfb\xff"
    assert decoded("-_8=", label="payload", max_bytes=2) == b"\xfb\xff"
    with pytest.raises(attestation_input.AttestationInputError, match="base64"):
        decoded("YWJj\n", label="payload", max_bytes=4)
    with pytest.raises(attestation_input.AttestationInputError, match="base64"):
        decoded("+_8=", label="payload", max_bytes=2)
    with pytest.raises(attestation_input.AttestationInputError, match="limit"):
        decoded("YWJj", label="payload", max_bytes=2)


def test_dsse_base64_rejects_oversized_text_before_ascii_encoding() -> None:
    class TextThatMustNotBeEncoded(str):
        def encode(self, *args: object, **kwargs: object) -> bytes:
            raise AssertionError("oversized base64 text should be rejected before encoding")

    with pytest.raises(attestation_input.AttestationInputError, match="limit"):
        attestation_input.decode_dsse_base64(TextThatMustNotBeEncoded("aaaaa"), label="payload", max_bytes=2)


def test_bounded_reader_refuses_when_no_follow_open_is_unavailable(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    path = tmp_path / "input.json"
    path.write_text("{}", encoding="utf-8")

    def unavailable(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise OSError("no-follow file open is unavailable")

    monkeypatch.setattr(attestation_input.dirfd, "open_file_nofollow", unavailable)

    with pytest.raises(OSError, match="no-follow"):
        attestation_input.read_bounded_file(path)


def test_bounded_reader_delegates_read_only_open_and_closes_descriptor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    path = tmp_path / "input.json"
    open_calls: list[tuple[object, int]] = []
    closed: list[int] = []

    def open_file_nofollow(opened_path: object, flags: int) -> int:
        open_calls.append((opened_path, flags))
        return 47

    reads = iter((b"{}", b""))

    monkeypatch.setattr(attestation_input.dirfd, "open_file_nofollow", open_file_nofollow)
    monkeypatch.setattr(attestation_input.os, "fstat", lambda _: SimpleNamespace(st_mode=stat.S_IFREG))
    monkeypatch.setattr(attestation_input.os, "read", lambda *_: next(reads))
    monkeypatch.setattr(attestation_input.os, "close", closed.append)

    assert attestation_input.read_bounded_file(path) == b"{}"
    assert open_calls == [(path, os.O_RDONLY)]
    assert closed == [47]


def test_bounded_reader_refuses_nonregular_descriptor_and_closes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    path = tmp_path / "input.json"
    closed: list[int] = []

    monkeypatch.setattr(attestation_input.dirfd, "open_file_nofollow", lambda *_: 48)
    monkeypatch.setattr(attestation_input.os, "fstat", lambda _: SimpleNamespace(st_mode=stat.S_IFDIR))
    monkeypatch.setattr(attestation_input.os, "close", closed.append)

    with pytest.raises(attestation_input.AttestationInputError, match="regular file"):
        attestation_input.read_bounded_file(path)

    assert closed == [48]


def test_bounded_reader_accepts_boundary_and_closes_after_overflow(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    boundary = tmp_path / "boundary.json"
    overflow = tmp_path / "overflow.json"
    boundary.write_bytes(b"{}")
    overflow.write_bytes(b"{}!")
    closed: list[int] = []
    close = attestation_input.os.close

    def record_close(descriptor: int) -> None:
        closed.append(descriptor)
        close(descriptor)

    monkeypatch.setattr(attestation_input.os, "close", record_close)

    assert attestation_input.read_bounded_file(boundary, max_bytes=2) == b"{}"
    with pytest.raises(attestation_input.AttestationInputError, match="byte limit"):
        attestation_input.read_bounded_file(overflow, max_bytes=2)

    assert len(closed) == 2
