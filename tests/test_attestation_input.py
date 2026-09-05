from __future__ import annotations

import math

import pytest

from brigade import attestation_input


def test_strict_json_rejects_duplicate_names() -> None:
    source = b'{"version":1,"nested":{"name":"ok"}}'

    assert attestation_input.strict_json_loads(source) == {
        "version": 1,
        "nested": {"name": "ok"},
    }

    with pytest.raises(attestation_input.AttestationInputError, match="duplicate"):
        attestation_input.strict_json_loads('{"name":"first","name":"second"}')


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


def test_mapping_input_cannot_bypass_document_byte_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(attestation_input, "MAX_JSON_BYTES", 10)

    with pytest.raises(attestation_input.AttestationInputError, match="byte limit"):
        attestation_input.validate_json_value({"x": "abc"})


def test_dsse_base64_has_strict_bounds() -> None:
    decoded = attestation_input.decode_dsse_base64
    assert decoded("+/8=", label="payload", max_bytes=2) == b"\xfb\xff"
    assert decoded("-_8=", label="payload", max_bytes=2) == b"\xfb\xff"
    with pytest.raises(attestation_input.AttestationInputError, match="base64"):
        decoded("YWJj\n", label="payload", max_bytes=4)
    with pytest.raises(attestation_input.AttestationInputError, match="limit"):
        decoded("YWJj", label="payload", max_bytes=2)


def test_bounded_reader_refuses_when_no_follow_open_is_unavailable(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    path = tmp_path / "input.json"
    path.write_text("{}", encoding="utf-8")

    def unavailable(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise OSError("no-follow file open is unavailable")

    monkeypatch.setattr(attestation_input.dirfd, "open_file_nofollow", unavailable)

    with pytest.raises(OSError, match="no-follow"):
        attestation_input.read_bounded_file(path)
