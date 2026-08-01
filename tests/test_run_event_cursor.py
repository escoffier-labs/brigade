"""Failing-first tests for the dependency-free run-event cursor (issue #604).

These tests pin the hardened contract from the independent Ollama review:
canonical-only decode, duplicate-key rejection, bounded/redacted error
messages, encode-side field validation, and non-ASCII / surrogate rejection.
They use only the standard library.
"""

from __future__ import annotations

import base64
import json

import pytest

from brigade import run_event_cursor
from brigade.run_event_cursor import (
    CursorError,
    DecodedCursor,
    decode,
    encode,
    validate,
)

_SCHEMA = "brigade.run_event_cursor.v1"
_GOOD_RUN_ID = "run-20260728-aa74565d"
_GOOD_SEQUENCE = 7
_GOOD_DIGEST = "a" * 64


def _good_cursor() -> DecodedCursor:
    return DecodedCursor(
        schema=_SCHEMA,
        run_id=_GOOD_RUN_ID,
        sequence=_GOOD_SEQUENCE,
        digest=_GOOD_DIGEST,
    )


def _encode_payload(payload: dict) -> str:
    """Canonical encoding helper: sorted keys, compact separators, no padding."""
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _encode_raw_bytes(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


# --- encode / decode round trip -------------------------------------------------


def test_encode_is_deterministic_and_url_safe():
    c = _good_cursor()
    a = encode(c)
    b = encode(c)
    assert a == b
    assert all(ch not in a for ch in "+/=\n\r\t ")
    assert a.isascii()


def test_encode_decode_round_trip_preserves_fields():
    c = _good_cursor()
    decoded = decode(encode(c))
    assert decoded == c


def test_encoded_payload_is_canonical_compact_sorted_json():
    c = _good_cursor()
    raw = base64.urlsafe_b64decode(encode(c).encode("ascii") + b"==")
    payload = json.loads(raw.decode("utf-8"))
    assert payload == {
        "schema": _SCHEMA,
        "run_id": _GOOD_RUN_ID,
        "sequence": _GOOD_SEQUENCE,
        "digest": _GOOD_DIGEST,
    }
    assert raw.decode("utf-8") == json.dumps(
        {
            "schema": _SCHEMA,
            "run_id": _GOOD_RUN_ID,
            "sequence": _GOOD_SEQUENCE,
            "digest": _GOOD_DIGEST,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


# --- decode: input shape rejections --------------------------------------------


def test_decode_rejects_non_string_input():
    for bad in (123, None, b"not-a-str"):
        with pytest.raises(CursorError) as exc:
            decode(bad)  # type: ignore[arg-type]
        assert exc.value.code == "cursor_malformed"


def test_decode_rejects_non_ascii_cursor_string():
    with pytest.raises(CursorError) as exc:
        decode("caf\xc3\xa9")  # type: ignore[arg-type]
    assert exc.value.code == "cursor_malformed"


def test_decode_rejects_surrogate_containing_cursor_string():
    with pytest.raises(CursorError) as exc:
        decode("ab\ud800cd")
    assert exc.value.code == "cursor_malformed"


def test_decode_rejects_empty_cursor_string():
    with pytest.raises(CursorError) as exc:
        decode("")
    assert exc.value.code == "cursor_malformed"


def test_decode_rejects_malformed_base64():
    with pytest.raises(CursorError) as exc:
        decode("!!!not-base64!!!")
    assert exc.value.code == "cursor_malformed"


def test_decode_rejects_padded_base64():
    c = _good_cursor()
    padded = encode(c) + "="
    with pytest.raises(CursorError) as exc:
        decode(padded)
    assert exc.value.code == "cursor_malformed"


def test_decode_rejects_whitespace_in_cursor():
    c = _good_cursor()
    with pytest.raises(CursorError) as exc:
        decode(" " + encode(c) + " ")
    assert exc.value.code == "cursor_malformed"


def test_decode_rejects_newline_in_cursor():
    c = _good_cursor()
    with pytest.raises(CursorError) as exc:
        decode(encode(c) + "\n")
    assert exc.value.code == "cursor_malformed"


def test_decode_rejects_non_url_safe_alphabet():
    # '+' and '/' are standard base64, not base64url. Substituting one into a
    # otherwise-valid cursor must be rejected at the alphabet check.
    c = _good_cursor()
    bad = "+" + encode(c)[1:]
    with pytest.raises(CursorError) as exc:
        decode(bad)
    assert exc.value.code == "cursor_malformed"
    bad2 = "/" + encode(c)[1:]
    with pytest.raises(CursorError) as exc2:
        decode(bad2)
    assert exc2.value.code == "cursor_malformed"


def test_decode_rejects_oversized_input():
    c = _good_cursor()
    bloated = encode(c) + ("A" * 8192)
    with pytest.raises(CursorError) as exc:
        decode(bloated)
    assert exc.value.code == "cursor_oversized"


def test_decode_rejects_non_utf8_payload():
    bad = _encode_raw_bytes(b"\xff\xfe\x00\x01")
    with pytest.raises(CursorError) as exc:
        decode(bad)
    assert exc.value.code == "cursor_malformed"


def test_decode_rejects_non_object_json():
    arr = _encode_raw_bytes(b"[1,2,3]")
    with pytest.raises(CursorError) as exc:
        decode(arr)
    assert exc.value.code == "cursor_non_object"
    scalar = _encode_raw_bytes(b'"hello"')
    with pytest.raises(CursorError) as exc2:
        decode(scalar)
    assert exc2.value.code == "cursor_non_object"


# --- decode: canonical representation rejections --------------------------------


def test_decode_rejects_duplicate_json_keys():
    raw = (
        '{"schema": "'
        + _SCHEMA
        + '", "run_id": "'
        + _GOOD_RUN_ID
        + '", "sequence": '
        + str(_GOOD_SEQUENCE)
        + ', "digest": "'
        + _GOOD_DIGEST
        + '", "digest": "'
        + ("b" * 64)
        + '"}'
    ).encode("utf-8")
    bad = _encode_raw_bytes(raw)
    with pytest.raises(CursorError) as exc:
        decode(bad)
    assert exc.value.code == "cursor_malformed"


def test_decode_rejects_non_canonical_json_key_order():
    # Same fields, valid values, but keys emitted in non-sorted order.
    raw = (
        '{"run_id": "'
        + _GOOD_RUN_ID
        + '", "schema": "'
        + _SCHEMA
        + '", "digest": "'
        + _GOOD_DIGEST
        + '", "sequence": '
        + str(_GOOD_SEQUENCE)
        + "}"
    ).encode("utf-8")
    bad = _encode_raw_bytes(raw)
    with pytest.raises(CursorError) as exc:
        decode(bad)
    assert exc.value.code == "cursor_malformed"


def test_decode_rejects_non_canonical_json_whitespace():
    raw = (
        '{ "schema": "'
        + _SCHEMA
        + '", "run_id": "'
        + _GOOD_RUN_ID
        + '", "sequence": '
        + str(_GOOD_SEQUENCE)
        + ', "digest": "'
        + _GOOD_DIGEST
        + '" }'
    ).encode("utf-8")
    bad = _encode_raw_bytes(raw)
    with pytest.raises(CursorError) as exc:
        decode(bad)
    assert exc.value.code == "cursor_malformed"


def test_decode_rejects_non_canonical_pad_bits():
    # Build a valid cursor whose JSON payload length is NOT a multiple of 3,
    # so the final base64 char carries non-zero-width pad bits we can flip
    # without changing the decoded bytes.
    cursor = DecodedCursor(
        schema=_SCHEMA,
        run_id="run-20260728-aa74565d-x",  # length tuned so payload % 3 != 0
        sequence=_GOOD_SEQUENCE,
        digest=_GOOD_DIGEST,
    )
    encoded = encode(cursor)
    raw = base64.urlsafe_b64decode(_pad(encoded))
    rem = len(raw) % 3
    assert rem != 0, "test payload must have pad bits to corrupt"
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    # Pad bits are the LOW bits of the final char. Canonical form has them
    # zero; flipping the lowest pad bit yields a non-canonical string that
    # decodes to the same bytes.
    increment = 1
    last_val = alphabet.index(encoded[-1])
    new_val = last_val + increment
    assert new_val < 64
    bad = encoded[:-1] + alphabet[new_val]
    # Sanity: decodes to the same raw bytes (pad bits ignored by decoder).
    assert base64.urlsafe_b64decode(_pad(bad)) == raw
    with pytest.raises(CursorError) as exc:
        decode(bad)
    assert exc.value.code == "cursor_malformed"


def _pad(value: str) -> str:
    pad = (-len(value)) % 4
    return value + ("=" * pad)


# --- decode: field validation rejections ---------------------------------------


def _bad_cursor(payload: dict) -> str:
    return _encode_payload(payload)


def test_decode_rejects_unsupported_schema():
    bad = _bad_cursor(
        {
            "schema": "brigade.run_event_cursor.v999",
            "run_id": _GOOD_RUN_ID,
            "sequence": _GOOD_SEQUENCE,
            "digest": _GOOD_DIGEST,
        }
    )
    with pytest.raises(CursorError, match="schema") as exc:
        decode(bad)
    assert exc.value.code == "cursor_schema_unsupported"


def test_decode_rejects_missing_schema_field():
    bad = _bad_cursor({"run_id": _GOOD_RUN_ID, "sequence": _GOOD_SEQUENCE, "digest": _GOOD_DIGEST})
    with pytest.raises(CursorError, match="missing") as exc:
        decode(bad)
    assert exc.value.code == "cursor_field_missing"


def test_decode_rejects_missing_run_id_field():
    bad = _bad_cursor({"schema": _SCHEMA, "sequence": _GOOD_SEQUENCE, "digest": _GOOD_DIGEST})
    with pytest.raises(CursorError, match="missing") as exc:
        decode(bad)
    assert exc.value.code == "cursor_field_missing"


def test_decode_rejects_missing_sequence_field():
    bad = _bad_cursor({"schema": _SCHEMA, "run_id": _GOOD_RUN_ID, "digest": _GOOD_DIGEST})
    with pytest.raises(CursorError, match="missing") as exc:
        decode(bad)
    assert exc.value.code == "cursor_field_missing"


def test_decode_rejects_missing_digest_field():
    bad = _bad_cursor({"schema": _SCHEMA, "run_id": _GOOD_RUN_ID, "sequence": _GOOD_SEQUENCE})
    with pytest.raises(CursorError, match="missing") as exc:
        decode(bad)
    assert exc.value.code == "cursor_field_missing"


def test_decode_rejects_extra_field():
    bad = _bad_cursor(
        {
            "schema": _SCHEMA,
            "run_id": _GOOD_RUN_ID,
            "sequence": _GOOD_SEQUENCE,
            "digest": _GOOD_DIGEST,
            "payload": "sneaky",
        }
    )
    with pytest.raises(CursorError, match="extra") as exc:
        decode(bad)
    assert exc.value.code == "cursor_field_extra"


def test_decode_rejects_empty_run_id():
    bad = _bad_cursor({"schema": _SCHEMA, "run_id": "", "sequence": _GOOD_SEQUENCE, "digest": _GOOD_DIGEST})
    with pytest.raises(CursorError, match="run_id") as exc:
        decode(bad)
    assert exc.value.code == "cursor_run_id_invalid"


def test_decode_rejects_non_string_run_id():
    bad = _bad_cursor({"schema": _SCHEMA, "run_id": 42, "sequence": _GOOD_SEQUENCE, "digest": _GOOD_DIGEST})
    with pytest.raises(CursorError, match="run_id") as exc:
        decode(bad)
    assert exc.value.code == "cursor_run_id_invalid"


def test_decode_rejects_run_id_with_control_characters():
    bad = _bad_cursor({"schema": _SCHEMA, "run_id": "bad\nid", "sequence": _GOOD_SEQUENCE, "digest": _GOOD_DIGEST})
    with pytest.raises(CursorError, match="run_id") as exc:
        decode(bad)
    assert exc.value.code == "cursor_run_id_invalid"


@pytest.mark.parametrize("run_id", ["has space", "has/slash", "has@at", "emoji-🙂"])
def test_decode_rejects_run_id_outside_journal_alphabet(run_id):
    """Cursor run_id must match the lifecycle journal alphabet (issue #604)."""
    bad = _bad_cursor({"schema": _SCHEMA, "run_id": run_id, "sequence": _GOOD_SEQUENCE, "digest": _GOOD_DIGEST})
    with pytest.raises(CursorError, match="run_id") as exc:
        decode(bad)
    assert exc.value.code == "cursor_run_id_invalid"


def test_decode_rejects_oversized_run_id():
    bad = _bad_cursor({"schema": _SCHEMA, "run_id": "x" * 1024, "sequence": _GOOD_SEQUENCE, "digest": _GOOD_DIGEST})
    with pytest.raises(CursorError, match="run_id") as exc:
        decode(bad)
    assert exc.value.code == "cursor_run_id_invalid"


def test_decode_rejects_bool_sequence():
    bad = _bad_cursor({"schema": _SCHEMA, "run_id": _GOOD_RUN_ID, "sequence": True, "digest": _GOOD_DIGEST})
    with pytest.raises(CursorError, match="sequence") as exc:
        decode(bad)
    assert exc.value.code == "cursor_sequence_invalid"


def test_decode_rejects_non_int_sequence():
    bad = _bad_cursor({"schema": _SCHEMA, "run_id": _GOOD_RUN_ID, "sequence": "7", "digest": _GOOD_DIGEST})
    with pytest.raises(CursorError, match="sequence") as exc:
        decode(bad)
    assert exc.value.code == "cursor_sequence_invalid"


def test_decode_rejects_zero_sequence():
    bad = _bad_cursor({"schema": _SCHEMA, "run_id": _GOOD_RUN_ID, "sequence": 0, "digest": _GOOD_DIGEST})
    with pytest.raises(CursorError, match="sequence") as exc:
        decode(bad)
    assert exc.value.code == "cursor_sequence_invalid"


def test_decode_rejects_negative_sequence():
    bad = _bad_cursor({"schema": _SCHEMA, "run_id": _GOOD_RUN_ID, "sequence": -1, "digest": _GOOD_DIGEST})
    with pytest.raises(CursorError, match="sequence") as exc:
        decode(bad)
    assert exc.value.code == "cursor_sequence_invalid"


def test_decode_rejects_non_string_digest():
    bad = _bad_cursor({"schema": _SCHEMA, "run_id": _GOOD_RUN_ID, "sequence": _GOOD_SEQUENCE, "digest": 64})
    with pytest.raises(CursorError, match="digest") as exc:
        decode(bad)
    assert exc.value.code == "cursor_digest_invalid"


def test_decode_rejects_uppercase_digest():
    bad = _bad_cursor({"schema": _SCHEMA, "run_id": _GOOD_RUN_ID, "sequence": _GOOD_SEQUENCE, "digest": "A" * 64})
    with pytest.raises(CursorError, match="digest") as exc:
        decode(bad)
    assert exc.value.code == "cursor_digest_invalid"


def test_decode_rejects_wrong_length_digest():
    bad = _bad_cursor({"schema": _SCHEMA, "run_id": _GOOD_RUN_ID, "sequence": _GOOD_SEQUENCE, "digest": "a" * 63})
    with pytest.raises(CursorError, match="digest") as exc:
        decode(bad)
    assert exc.value.code == "cursor_digest_invalid"


def test_decode_rejects_non_hex_digest():
    bad = _bad_cursor({"schema": _SCHEMA, "run_id": _GOOD_RUN_ID, "sequence": _GOOD_SEQUENCE, "digest": "g" * 64})
    with pytest.raises(CursorError, match="digest") as exc:
        decode(bad)
    assert exc.value.code == "cursor_digest_invalid"


def test_decode_error_carries_stable_code():
    with pytest.raises(CursorError) as exc:
        decode("!!!not-base64!!!")
    assert exc.value.code == "cursor_malformed"


# --- encode: manual DecodedCursor field validation -----------------------------


def test_encode_rejects_unsupported_schema():
    c = DecodedCursor(
        schema="brigade.run_event_cursor.v999",
        run_id=_GOOD_RUN_ID,
        sequence=_GOOD_SEQUENCE,
        digest=_GOOD_DIGEST,
    )
    with pytest.raises(CursorError) as exc:
        encode(c)
    assert exc.value.code == "cursor_schema_unsupported"


def test_encode_rejects_empty_run_id():
    c = DecodedCursor(schema=_SCHEMA, run_id="", sequence=_GOOD_SEQUENCE, digest=_GOOD_DIGEST)
    with pytest.raises(CursorError) as exc:
        encode(c)
    assert exc.value.code == "cursor_run_id_invalid"


def test_encode_rejects_non_string_run_id():
    c = DecodedCursor(
        schema=_SCHEMA,
        run_id=42,
        sequence=_GOOD_SEQUENCE,
        digest=_GOOD_DIGEST,  # type: ignore[arg-type]
    )
    with pytest.raises(CursorError) as exc:
        encode(c)
    assert exc.value.code == "cursor_run_id_invalid"


def test_encode_rejects_bool_sequence():
    c = DecodedCursor(
        schema=_SCHEMA,
        run_id=_GOOD_RUN_ID,
        sequence=True,
        digest=_GOOD_DIGEST,  # type: ignore[arg-type]
    )
    with pytest.raises(CursorError) as exc:
        encode(c)
    assert exc.value.code == "cursor_sequence_invalid"


def test_encode_rejects_zero_sequence():
    c = DecodedCursor(schema=_SCHEMA, run_id=_GOOD_RUN_ID, sequence=0, digest=_GOOD_DIGEST)
    with pytest.raises(CursorError) as exc:
        encode(c)
    assert exc.value.code == "cursor_sequence_invalid"


def test_encode_rejects_non_int_sequence():
    c = DecodedCursor(
        schema=_SCHEMA,
        run_id=_GOOD_RUN_ID,
        sequence="7",
        digest=_GOOD_DIGEST,  # type: ignore[arg-type]
    )
    with pytest.raises(CursorError) as exc:
        encode(c)
    assert exc.value.code == "cursor_sequence_invalid"


def test_encode_rejects_uppercase_digest():
    c = DecodedCursor(schema=_SCHEMA, run_id=_GOOD_RUN_ID, sequence=_GOOD_SEQUENCE, digest="A" * 64)
    with pytest.raises(CursorError) as exc:
        encode(c)
    assert exc.value.code == "cursor_digest_invalid"


def test_encode_rejects_wrong_length_digest():
    c = DecodedCursor(schema=_SCHEMA, run_id=_GOOD_RUN_ID, sequence=_GOOD_SEQUENCE, digest="a" * 63)
    with pytest.raises(CursorError) as exc:
        encode(c)
    assert exc.value.code == "cursor_digest_invalid"


def test_encode_rejects_non_hex_digest():
    c = DecodedCursor(schema=_SCHEMA, run_id=_GOOD_RUN_ID, sequence=_GOOD_SEQUENCE, digest="g" * 64)
    with pytest.raises(CursorError) as exc:
        encode(c)
    assert exc.value.code == "cursor_digest_invalid"


def test_encode_then_decode_round_trips_for_valid_manual_cursor():
    c = _good_cursor()
    assert decode(encode(c)) == c


# --- bounded / redacted error messages -----------------------------------------


def test_error_message_does_not_echo_untrusted_schema_value():
    bad = _bad_cursor(
        {
            "schema": "UNTRUSTED-SECRET-SCHEMA-VALUE",
            "run_id": _GOOD_RUN_ID,
            "sequence": _GOOD_SEQUENCE,
            "digest": _GOOD_DIGEST,
        }
    )
    with pytest.raises(CursorError) as exc:
        decode(bad)
    assert exc.value.code == "cursor_schema_unsupported"
    assert "UNTRUSTED-SECRET-SCHEMA-VALUE" not in str(exc.value)


def test_error_message_does_not_echo_missing_field_names():
    bad = _bad_cursor({"run_id": _GOOD_RUN_ID, "sequence": _GOOD_SEQUENCE, "digest": _GOOD_DIGEST})
    with pytest.raises(CursorError) as exc:
        decode(bad)
    assert exc.value.code == "cursor_field_missing"
    msg = str(exc.value)
    assert "schema" not in msg
    assert "run_id" not in msg
    assert "sequence" not in msg
    assert "digest" not in msg


def test_error_message_does_not_echo_extra_field_names():
    bad = _bad_cursor(
        {
            "schema": _SCHEMA,
            "run_id": _GOOD_RUN_ID,
            "sequence": _GOOD_SEQUENCE,
            "digest": _GOOD_DIGEST,
            "secret_extra": "leak",
        }
    )
    with pytest.raises(CursorError) as exc:
        decode(bad)
    assert exc.value.code == "cursor_field_extra"
    msg = str(exc.value)
    assert "secret_extra" not in msg
    assert "leak" not in msg


def test_error_messages_are_bounded():
    # Every CursorError message is a short, fixed string with no interpolated
    # untrusted content. Assert a hard upper bound to catch future regressions.
    cases = [
        ("cursor_malformed", "!!!not-base64!!!"),
        ("cursor_malformed", "caf\xc3\xa9"),
        ("cursor_malformed", "ab\ud800cd"),
        ("cursor_malformed", ""),
        ("cursor_oversized", encode(_good_cursor()) + ("A" * 8192)),
        ("cursor_non_object", _encode_raw_bytes(b"[1,2,3]")),
        ("cursor_schema_unsupported", _bad_cursor({**_good_payload(), "schema": "x"})),
        ("cursor_field_missing", _bad_cursor({"run_id": _GOOD_RUN_ID})),
        ("cursor_field_extra", _bad_cursor({**_good_payload(), "extra": 1})),
        ("cursor_run_id_invalid", _bad_cursor({**_good_payload(), "run_id": ""})),
        ("cursor_sequence_invalid", _bad_cursor({**_good_payload(), "sequence": True})),
        ("cursor_digest_invalid", _bad_cursor({**_good_payload(), "digest": "A" * 64})),
    ]
    for expected_code, bad_input in cases:
        with pytest.raises(CursorError) as exc:
            decode(bad_input)
        assert exc.value.code == expected_code
        assert len(str(exc.value)) <= 80, (expected_code, str(exc.value))


def _good_payload() -> dict:
    return {
        "schema": _SCHEMA,
        "run_id": _GOOD_RUN_ID,
        "sequence": _GOOD_SEQUENCE,
        "digest": _GOOD_DIGEST,
    }


# --- validate helper -----------------------------------------------------------


def test_validate_accepts_matching_coordinates():
    c = _good_cursor()
    validate(
        c,
        expected_run_id=_GOOD_RUN_ID,
        event_sequence=_GOOD_SEQUENCE,
        event_digest=_GOOD_DIGEST,
    )


def test_validate_rejects_run_id_mismatch_with_stable_code():
    c = _good_cursor()
    with pytest.raises(CursorError, match="run_id") as exc:
        validate(c, expected_run_id="other-run", event_sequence=_GOOD_SEQUENCE, event_digest=_GOOD_DIGEST)
    assert exc.value.code == "cursor_run_id_mismatch"


def test_validate_rejects_sequence_mismatch_with_stable_code():
    c = _good_cursor()
    with pytest.raises(CursorError, match="sequence") as exc:
        validate(c, expected_run_id=_GOOD_RUN_ID, event_sequence=_GOOD_SEQUENCE + 1, event_digest=_GOOD_DIGEST)
    assert exc.value.code == "cursor_sequence_mismatch"


def test_validate_rejects_digest_mismatch_with_stable_code():
    c = _good_cursor()
    with pytest.raises(CursorError, match="digest") as exc:
        validate(c, expected_run_id=_GOOD_RUN_ID, event_sequence=_GOOD_SEQUENCE, event_digest="b" * 64)
    assert exc.value.code == "cursor_digest_mismatch"


def test_validate_rejects_empty_expected_run_id():
    c = _good_cursor()
    with pytest.raises(CursorError, match="run_id"):
        validate(c, expected_run_id="", event_sequence=_GOOD_SEQUENCE, event_digest=_GOOD_DIGEST)


def test_validate_rejects_non_positive_event_sequence():
    c = _good_cursor()
    with pytest.raises(CursorError, match="sequence"):
        validate(c, expected_run_id=_GOOD_RUN_ID, event_sequence=0, event_digest=_GOOD_DIGEST)


def test_validate_rejects_invalid_event_digest():
    c = _good_cursor()
    with pytest.raises(CursorError, match="digest"):
        validate(c, expected_run_id=_GOOD_RUN_ID, event_sequence=_GOOD_SEQUENCE, event_digest="bad")


# --- regression: trailing-newline rejection through fullmatch -------------------
#
# ``$`` in a Python regex can match before a final newline, so a run_id or
# digest ending in ``\n`` would slip past ``_RUN_ID_RE`` / ``_DIGEST_RE`` when
# used with ``.match()``. These tests pin the stricter ``fullmatch`` behavior
# across encode(), decode(), and direct validate().


def test_encode_rejects_run_id_with_trailing_newline():
    c = DecodedCursor(
        schema=_SCHEMA,
        run_id=_GOOD_RUN_ID + "\n",
        sequence=_GOOD_SEQUENCE,
        digest=_GOOD_DIGEST,
    )
    with pytest.raises(CursorError) as exc:
        encode(c)
    assert exc.value.code == "cursor_run_id_invalid"


def test_decode_rejects_run_id_with_trailing_newline():
    # Canonical JSON with a run_id value that ends in a newline. The shared
    # field validator (called from decode) must reject it before any
    # canonical round-trip comparison runs.
    raw = (
        '{"digest": "'
        + _GOOD_DIGEST
        + '", "run_id": "'
        + _GOOD_RUN_ID
        + '\\n", "schema": "'
        + _SCHEMA
        + '", "sequence": '
        + str(_GOOD_SEQUENCE)
        + "}"
    ).encode("utf-8")
    bad = _encode_raw_bytes(raw)
    with pytest.raises(CursorError) as exc:
        decode(bad)
    assert exc.value.code == "cursor_run_id_invalid"


def test_validate_rejects_cursor_run_id_with_trailing_newline():
    # DecodedCursor is public and manually constructible; validate() must
    # validate the cursor's own fields, not just the caller-supplied coords.
    c = DecodedCursor(
        schema=_SCHEMA,
        run_id=_GOOD_RUN_ID + "\n",
        sequence=_GOOD_SEQUENCE,
        digest=_GOOD_DIGEST,
    )
    with pytest.raises(CursorError) as exc:
        validate(
            c,
            expected_run_id=_GOOD_RUN_ID,
            event_sequence=_GOOD_SEQUENCE,
            event_digest=_GOOD_DIGEST,
        )
    assert exc.value.code == "cursor_run_id_invalid"


def test_encode_rejects_digest_with_trailing_newline():
    c = DecodedCursor(
        schema=_SCHEMA,
        run_id=_GOOD_RUN_ID,
        sequence=_GOOD_SEQUENCE,
        digest=_GOOD_DIGEST + "\n",
    )
    with pytest.raises(CursorError) as exc:
        encode(c)
    assert exc.value.code == "cursor_digest_invalid"


def test_decode_rejects_digest_with_trailing_newline():
    raw = (
        '{"digest": "'
        + _GOOD_DIGEST
        + '\\n", "run_id": "'
        + _GOOD_RUN_ID
        + '", "schema": "'
        + _SCHEMA
        + '", "sequence": '
        + str(_GOOD_SEQUENCE)
        + "}"
    ).encode("utf-8")
    bad = _encode_raw_bytes(raw)
    with pytest.raises(CursorError) as exc:
        decode(bad)
    assert exc.value.code == "cursor_digest_invalid"


def test_validate_rejects_cursor_digest_with_trailing_newline():
    c = DecodedCursor(
        schema=_SCHEMA,
        run_id=_GOOD_RUN_ID,
        sequence=_GOOD_SEQUENCE,
        digest=_GOOD_DIGEST + "\n",
    )
    with pytest.raises(CursorError) as exc:
        validate(
            c,
            expected_run_id=_GOOD_RUN_ID,
            event_sequence=_GOOD_SEQUENCE,
            event_digest=_GOOD_DIGEST,
        )
    assert exc.value.code == "cursor_digest_invalid"


def test_validate_rejects_manually_constructed_unsupported_schema():
    # A hand-built DecodedCursor with an unsupported schema must be rejected
    # by validate() with cursor_schema_unsupported, not silently compared.
    c = DecodedCursor(
        schema="brigade.run_event_cursor.v999",
        run_id=_GOOD_RUN_ID,
        sequence=_GOOD_SEQUENCE,
        digest=_GOOD_DIGEST,
    )
    with pytest.raises(CursorError) as exc:
        validate(
            c,
            expected_run_id=_GOOD_RUN_ID,
            event_sequence=_GOOD_SEQUENCE,
            event_digest=_GOOD_DIGEST,
        )
    assert exc.value.code == "cursor_schema_unsupported"


def test_validate_rejects_manually_constructed_invalid_sequence():
    # Symmetric coverage: a hand-built cursor with a bool sequence must be
    # rejected by validate() via the shared field validator.
    c = DecodedCursor(
        schema=_SCHEMA,
        run_id=_GOOD_RUN_ID,
        sequence=True,  # type: ignore[arg-type]
        digest=_GOOD_DIGEST,
    )
    with pytest.raises(CursorError) as exc:
        validate(
            c,
            expected_run_id=_GOOD_RUN_ID,
            event_sequence=_GOOD_SEQUENCE,
            event_digest=_GOOD_DIGEST,
        )
    assert exc.value.code == "cursor_sequence_invalid"


# --- module surface ------------------------------------------------------------


def test_module_exposes_supported_schema_constant():
    assert run_event_cursor.SUPPORTED_SCHEMA == _SCHEMA


def test_module_docstring_documents_mac_boundary():
    assert "not a MAC" in run_event_cursor.__doc__ or "not a mac" in (run_event_cursor.__doc__ or "").lower()


def test_module_docstring_documents_canonical_representation():
    assert "canonical" in (run_event_cursor.__doc__ or "").lower()
