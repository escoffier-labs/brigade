"""Opaque, dependency-free run-event cursor for Brigade issue #604.

A cursor binds four coordinates from a verified journal event:

  - the cursor schema/version (``SUPPORTED_SCHEMA``),
  - the owning ``run_id`` (non-empty string),
  - a positive 1-based ``sequence`` index inside that run's event stream, and
  - a lowercase 64-hex ``digest`` of the event (a SHA-256 style fingerprint).

The cursor is **opaque**, not a MAC. It carries no secret and proves nothing
on its own. Integrity comes from the consumer matching the cursor's
coordinates against a verified #568 journal record: the consumer looks up the
journal entry for ``run_id`` at ``sequence`` and confirms that record's
``digest`` equals the cursor's ``digest``. ``validate`` is the helper that
performs that comparison against caller-supplied verified coordinates and
reports mismatches with stable, bounded error codes suitable for CLI
diagnostics.

This module:

  - uses only the Python standard library,
  - is deterministic (canonical compact JSON, sorted keys, urlsafe base64
    without padding, canonical pad bits),
  - fails closed on every malformed input (bad base64, non-ASCII or
    surrogate-containing cursor strings, bad UTF-8, non-object JSON,
    duplicate JSON object keys, missing or extra fields, unsupported schema,
    empty or invalid ``run_id``, bool / non-int / non-positive ``sequence``,
    invalid ``digest``, non-canonical representation, and oversized input),
  - validates every field on both :func:`encode` and :func:`decode` with the
    same stable :class:`CursorError` codes, so a hand-constructed
    :class:`DecodedCursor` cannot smuggle invalid values through ``encode``,
  - never persists subscriber state and never carries event payloads, and
  - is owned by the journal consumer; encoding and validation live here while
    ``runs events`` and control idempotency live in the CLI / control layers.

The cursor is intentionally not a MAC. Do not add HMAC material here.

Canonical representation contract (enforced by :func:`decode`):

  - non-empty ASCII base64url alphabet ``[A-Za-z0-9_-]``,
  - no ``=`` padding and no whitespace,
  - canonical (zero) pad bits in the final base64 character, and
  - canonical compact JSON (sorted keys, ``","``/``":"`` separators, no
    whitespace, no duplicate object keys).

After parsing and field validation, :func:`decode` re-encodes the decoded
cursor and requires byte-for-byte equality with the input, which rejects
any non-canonical representation that decodes to otherwise-valid data.

Error messages are bounded and never echo untrusted schema values or
missing/extra field names; the stable ``code`` attribute is the public
diagnostic contract.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from typing import Any

SUPPORTED_SCHEMA = "brigade.run_event_cursor.v1"

_MAX_CURSOR_BYTES = 8192
_MAX_RUN_ID_LEN = 256
_FIELDS = ("schema", "run_id", "sequence", "digest")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
# Match the lifecycle journal run_id alphabet (run_events._RUN_ID_RE) so a
# cursor cannot bind a run_id the journal would refuse.
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,256}$")
# Canonical base64url alphabet: no padding, no whitespace, ASCII only.
_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class CursorError(RuntimeError):
    """Cursor encode/decode/validation failure with a stable ``code``.

    ``code`` is a stable, bounded string suitable for CLI diagnostics and
    test assertions. Error messages are bounded and never echo untrusted
    cursor values or field names. The bounded set of codes is:

      - ``cursor_malformed``         (non-string / non-ASCII / surrogate /
        empty / bad base64 / bad UTF-8 / bad JSON / duplicate JSON object
        keys / non-canonical representation)
      - ``cursor_non_object``        (valid JSON but not an object)
      - ``cursor_oversized``         (raw input exceeds the size cap)
      - ``cursor_schema_unsupported`` (missing or unknown schema field)
      - ``cursor_field_missing``     (a required field is absent)
      - ``cursor_field_extra``       (an unknown field is present)
      - ``cursor_run_id_invalid``    (empty / non-string / oversized / control chars)
      - ``cursor_sequence_invalid``  (bool / non-int / non-positive)
      - ``cursor_digest_invalid``    (non-string / wrong length / non-lowercase-hex)
      - ``cursor_run_id_mismatch``   (validate: run_id differs)
      - ``cursor_sequence_mismatch`` (validate: sequence differs)
      - ``cursor_digest_mismatch``   (validate: digest differs)
    """

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class DecodedCursor:
    """A decoded, validated cursor.

    Field invariants are enforced by :func:`decode` and :func:`encode`; a
    manually constructed instance is validated by :func:`encode` before it
    is ever emitted.
    """

    schema: str
    run_id: str
    sequence: int
    digest: str


def encode(cursor: DecodedCursor) -> str:
    """Encode a cursor to an opaque, URL-safe, deterministic string.

    Validates every field with the same stable :class:`CursorError` codes as
    :func:`decode`, so a hand-constructed :class:`DecodedCursor` with invalid
    fields fails closed instead of producing an opaque string that decodes
    to different values. The payload is canonical compact JSON (sorted keys,
    no whitespace) wrapped in urlsafe base64 without padding.
    """
    _validate_fields(cursor.schema, cursor.run_id, cursor.sequence, cursor.digest)
    payload = {
        "schema": cursor.schema,
        "run_id": cursor.run_id,
        "sequence": cursor.sequence,
        "digest": cursor.digest,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode(cursor: str) -> DecodedCursor:
    """Decode and fully validate an opaque cursor string.

    Accepts exactly one canonical representation: non-empty ASCII base64url
    alphabet ``[A-Za-z0-9_-]`` with no ``=`` padding, no whitespace, and
    canonical (zero) pad bits. Duplicate JSON object keys are rejected
    (instead of ``json.loads`` last-key-wins). After parsing and field
    validation, the decoded cursor is re-encoded and required to equal the
    input byte-for-byte, which also rejects non-canonical JSON key order or
    whitespace. Fails closed with :class:`CursorError` on any malformed or
    invalid input.
    """
    if not isinstance(cursor, str):
        raise CursorError("cursor must be a string", code="cursor_malformed")
    # Reject non-ASCII and surrogate-containing cursor strings up front.
    if not cursor.isascii():
        raise CursorError("cursor must be ASCII", code="cursor_malformed")
    if not cursor:
        raise CursorError("cursor must not be empty", code="cursor_malformed")
    if len(cursor.encode("utf-8")) > _MAX_CURSOR_BYTES:
        raise CursorError("cursor input exceeds size limit", code="cursor_oversized")
    # Canonical alphabet, no padding, no whitespace. fullmatch (not match)
    # so a trailing newline cannot satisfy the ``$`` anchor.
    if not _B64URL_RE.fullmatch(cursor):
        raise CursorError("cursor base64 is malformed", code="cursor_malformed")
    try:
        raw = base64.urlsafe_b64decode(_add_b64_padding(cursor))
    except (ValueError, binascii.Error):
        raise CursorError("cursor base64 is malformed", code="cursor_malformed") from None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise CursorError("cursor payload is not valid UTF-8", code="cursor_malformed") from None
    try:
        payload = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except (json.JSONDecodeError, _DuplicateKeyError):
        raise CursorError("cursor payload is not valid JSON", code="cursor_malformed") from None
    if not isinstance(payload, dict):
        raise CursorError("cursor payload is not a JSON object", code="cursor_non_object")

    schema, run_id, sequence, digest = _extract_fields(payload)
    _validate_fields(schema, run_id, sequence, digest)

    decoded = DecodedCursor(schema=schema, run_id=run_id, sequence=sequence, digest=digest)
    # Canonical round-trip: re-encode and require byte-for-byte equality.
    # Rejects non-canonical pad bits, non-canonical JSON key order/whitespace,
    # and any other non-canonical representation that decodes to the same data.
    if encode(decoded) != cursor:
        raise CursorError("cursor is not in canonical form", code="cursor_malformed")
    return decoded


def validate(
    cursor: DecodedCursor,
    *,
    expected_run_id: str,
    event_sequence: int,
    event_digest: str,
) -> None:
    """Compare a decoded cursor against a verified journal event's coordinates.

    ``expected_run_id`` / ``event_sequence`` / ``event_digest`` must come from a
    journal record the caller has already verified. On success returns
    ``None``; on any mismatch raises :class:`CursorError` with a stable
    ``code`` (``cursor_run_id_mismatch`` / ``cursor_sequence_mismatch`` /
    ``cursor_digest_mismatch``). The caller-supplied coordinates are also
    sanity-checked and rejected with the corresponding ``*_invalid`` code so
    callers cannot silently compare against garbage.

    :class:`DecodedCursor` is public and manually constructible, so the
    cursor's own fields are validated with the shared field validator before
    any comparison; an invalid cursor schema/run_id/sequence/digest fails
    with the corresponding ``*_invalid`` / ``cursor_schema_unsupported``
    code rather than being silently compared.
    """
    # Validate the cursor itself first: a hand-constructed DecodedCursor may
    # carry invalid fields, and we must not compare garbage against garbage.
    _validate_fields(cursor.schema, cursor.run_id, cursor.sequence, cursor.digest)

    if not _is_valid_run_id(expected_run_id):
        raise CursorError("invalid expected run_id", code="cursor_run_id_invalid")
    if not _is_valid_sequence(event_sequence):
        raise CursorError("invalid event sequence", code="cursor_sequence_invalid")
    if not _is_valid_digest(event_digest):
        raise CursorError("invalid event digest", code="cursor_digest_invalid")

    if cursor.run_id != expected_run_id:
        raise CursorError(
            "cursor run_id does not match the verified journal record",
            code="cursor_run_id_mismatch",
        )
    if cursor.sequence != event_sequence:
        raise CursorError(
            "cursor sequence does not match the verified journal record",
            code="cursor_sequence_mismatch",
        )
    if cursor.digest != event_digest:
        raise CursorError(
            "cursor digest does not match the verified journal record",
            code="cursor_digest_mismatch",
        )


class _DuplicateKeyError(ValueError):
    """Raised by the JSON object_pairs_hook on duplicate object keys."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise _DuplicateKeyError("duplicate key")
        seen[key] = value
    return seen


def _extract_fields(payload: dict[str, Any]) -> tuple[Any, Any, Any, Any]:
    keys = set(payload.keys())
    expected = set(_FIELDS)
    missing = expected - keys
    if missing:
        raise CursorError("cursor missing required field(s)", code="cursor_field_missing")
    extra = keys - expected
    if extra:
        raise CursorError("cursor has extra field(s)", code="cursor_field_extra")
    return payload["schema"], payload["run_id"], payload["sequence"], payload["digest"]


def _validate_fields(schema: Any, run_id: Any, sequence: Any, digest: Any) -> None:
    if not isinstance(schema, str) or schema != SUPPORTED_SCHEMA:
        raise CursorError("unsupported cursor schema", code="cursor_schema_unsupported")
    if not _is_valid_run_id(run_id):
        raise CursorError("invalid run_id", code="cursor_run_id_invalid")
    if not _is_valid_sequence(sequence):
        raise CursorError("invalid sequence", code="cursor_sequence_invalid")
    if not _is_valid_digest(digest):
        raise CursorError("invalid digest", code="cursor_digest_invalid")


def _add_b64_padding(value: str) -> str:
    # urlsafe_b64decode tolerates missing padding in modern Python, but
    # restoring it explicitly keeps the failure mode narrow and predictable.
    pad = (-len(value)) % 4
    return value + ("=" * pad)


def _is_valid_run_id(value: Any) -> bool:
    # fullmatch (not match) so the trailing ``$`` anchor cannot be satisfied
    # before a final newline - ``"run-id\n"`` must NOT validate.
    return isinstance(value, str) and bool(_RUN_ID_RE.fullmatch(value)) and len(value) <= _MAX_RUN_ID_LEN


def _is_valid_sequence(value: Any) -> bool:
    # bool is a subclass of int; reject it explicitly so True/False are not
    # silently coerced to 1/0. Positive non-bool integer is the shared
    # requirement; #568 owns any sequence upper bound.
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _is_valid_digest(value: Any) -> bool:
    # fullmatch (not match) so a trailing newline cannot slip past ``$``.
    return isinstance(value, str) and bool(_DIGEST_RE.fullmatch(value))


__all__ = [
    "SUPPORTED_SCHEMA",
    "CursorError",
    "DecodedCursor",
    "decode",
    "encode",
    "validate",
]
