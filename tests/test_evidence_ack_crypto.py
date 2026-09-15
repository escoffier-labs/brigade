"""One-key acknowledgement SSHSIG observation contracts."""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from brigade import attestation, evidence_ack_crypto as crypto, evidence_ack_protocol, proc


REQUEST_ID = "df95b4a17011964e625f3e37c570c19eb6cd5ca6df506895e1bf9dfee5ea9e00"
KEY = b"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAABAgMEBQYHCAkKCwwNDg8QERITFBUWFxgZGhscHR4f"
FINGERPRINT = "SHA256:ZkAslGjFiUHdGf/WUL8rQvkib4PTvQatUV0OUQSncCA"
ARMOR = b"-----BEGIN SSH SIGNATURE-----\nYQ==\n-----END SSH SIGNATURE-----\n"
REQUEST_FIXTURE = {
    "schema": "brigade.evidence_ack_request.v1",
    "nonce": "2" * 32,
    "destinationId": "archive-a",
    "artifact": {
        "schema": "brigade.evidence_package.v1",
        "representation": "manifest-bytes",
        "sha256": "0" * 64,
        "verifyRunId": "verify-1",
        "receiptSha256": "1" * 64,
    },
    "createdAt": "2026-09-08T00:00:00Z",
    "expiresAt": "2026-09-09T00:00:00Z",
}


def _statement(*, result: str = "received") -> dict[str, object]:
    return {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": [{"name": "evidence-ack-request", "digest": {"sha256": REQUEST_ID}}],
        "predicateType": "https://brigade.dev/attestation/evidence-ack/v1",
        "predicate": {
            "schemaVersion": 1,
            "result": result,
            "destinationReference": "record-1",
            "acknowledgedAt": "2026-09-08T01:00:00Z",
        },
    }


def _wire(*, keyid: str = "SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", armor: bytes = ARMOR) -> bytes:
    statement = json.dumps(_statement(), sort_keys=True, separators=(",", ":")).encode()
    envelope = {
        "payloadType": attestation.DSSE_PAYLOAD_TYPE,
        "payload": base64.b64encode(statement).decode("ascii"),
        "signatures": [{"keyid": keyid, "sig": base64.b64encode(armor).decode("ascii")}],
        "brigade": {"profile": attestation.ATTESTATION_PROFILE, "namespace": "brigade-evidence-ack"},
    }
    return json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()


def _result(code: int = 0, **changes: object) -> proc.Result:
    result = proc.Result(code=code, stdout="irrelevant", stderr="irrelevant", descendants_reaped=True)
    for name, value in changes.items():
        setattr(result, name, value)
    return result


def _fake_runner(
    monkeypatch: pytest.MonkeyPatch,
    result: object,
    captured: dict[str, object] | None = None,
) -> None:
    monkeypatch.setattr(crypto.shutil, "which", lambda _name: "ssh-keygen")

    def run(executable, signers, signature, directory, pae, remaining):
        if captured is not None:
            captured.update(
                executable=executable,
                signers=signers.read_bytes(),
                signature=signature.read_bytes(),
                directory=directory,
                pae=pae,
                remaining=remaining,
            )
        return result

    monkeypatch.setattr(crypto, "_run_verify", run)


def _key_for_blob(blob: bytes) -> bytes:
    return b"ssh-ed25519 " + base64.b64encode(blob)


def _neutral(observed: crypto.AckCryptoObservation) -> tuple[str, str, str | None]:
    return observed.signature, observed.keyid, observed.verified_fingerprint


def _refusal_sentinels(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    clock_calls: list[float] = []

    def clock() -> float:
        clock_calls.append(0.0)
        return 0.0

    monkeypatch.setattr(crypto.time, "monotonic", clock)
    monkeypatch.setattr(crypto.evidence_ack_protocol, "parse_envelope", lambda _raw: pytest.fail("parser"))
    monkeypatch.setattr(crypto.shutil, "which", lambda _name: pytest.fail("executable"))
    monkeypatch.setattr(crypto.tempfile, "TemporaryDirectory", lambda: pytest.fail("temporary directory"))
    monkeypatch.setattr(crypto, "_write_private", lambda *_args: pytest.fail("private write"))
    monkeypatch.setattr(crypto, "_run_verify", lambda *_args: pytest.fail("runner"))
    return clock_calls


def _independent_canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")


def _independent_request_id() -> str:
    return hashlib.sha256(
        b"brigade.evidence_ack_request.v1\x00" + _independent_canonical_bytes(REQUEST_FIXTURE)
    ).hexdigest()


def _independent_envelope_id(raw: bytes) -> str:
    return hashlib.sha256(
        b"brigade.evidence_ack_response.v1\x00" + _independent_canonical_bytes(json.loads(raw))
    ).hexdigest()


def test_literal_key_vector_and_exact_runner_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    _fake_runner(monkeypatch, _result(), captured)
    observed = crypto.observe_ack_sshsig(_wire(keyid=FINGERPRINT), signer_public_key=KEY)
    assert observed.signature == "VALID"
    assert observed.keyid == "MATCH"
    assert observed.verified_fingerprint == FINGERPRINT
    assert captured["signers"] == b'brigade-ack-observer namespaces="brigade-evidence-ack" ' + KEY + b"\n"
    assert len(captured["signers"]) == 136
    assert captured["signature"] == ARMOR
    parsed = evidence_ack_protocol.parse_envelope(_wire())
    assert captured["pae"] == attestation.dsse_pae(attestation.DSSE_PAYLOAD_TYPE, parsed.statement_bytes)
    assert 0 < captured["remaining"] <= 5.0


@pytest.mark.parametrize(
    "raw,key,code",
    [
        ("not-bytes", KEY, "type"),
        (_wire(), "not-bytes", "type"),
        (b"x" * 131073, b"bad", "size"),
        (_wire(), b"x" * 257, "size"),
        (_wire(), b"x" * 256, "key"),
        (_wire(), KEY + b"\n", "key"),
        (_wire(), KEY + b"\n\n", "key"),
        (_wire(), KEY + b" comment", "key"),
        (_wire(), KEY.replace(b"A", b"-", 1), "key"),
    ],
)
def test_input_admission_and_key_grammar(raw: object, key: object, code: str) -> None:
    with pytest.raises(crypto.AckCryptoInputError, match=rf"^ack crypto input: {code}$") as raised:
        crypto.observe_ack_sshsig(raw, signer_public_key=key)  # type: ignore[arg-type]
    assert raised.value.code == code


@pytest.mark.parametrize(
    "raw,key",
    [((_wire(),), KEY), (_wire(), (KEY,))],
    ids=("raw-tuple", "key-tuple"),
)
def test_tuple_arguments_refused_before_operations(monkeypatch: pytest.MonkeyPatch, raw: object, key: object) -> None:
    clock_calls = _refusal_sentinels(monkeypatch)
    with pytest.raises(crypto.AckCryptoInputError, match="^ack crypto input: type$") as raised:
        crypto.observe_ack_sshsig(raw, signer_public_key=key)  # type: ignore[arg-type]
    assert raised.value.code == "type"
    assert str(raised.value) == "ack crypto input: type"
    assert clock_calls == [0.0]


def test_both_argument_types_precede_raw_length(monkeypatch: pytest.MonkeyPatch) -> None:
    clock_calls = _refusal_sentinels(monkeypatch)
    with pytest.raises(crypto.AckCryptoInputError, match="^ack crypto input: type$") as raised:
        crypto.observe_ack_sshsig(b"x" * 131_073, signer_public_key="not-bytes")  # type: ignore[arg-type]
    assert raised.value.code == "type"
    assert str(raised.value) == "ack crypto input: type"
    assert clock_calls == [0.0]


def test_exact_key_cap_refuses_syntax_before_parser_io(monkeypatch: pytest.MonkeyPatch) -> None:
    clock_calls = _refusal_sentinels(monkeypatch)
    with pytest.raises(crypto.AckCryptoInputError, match="^ack crypto input: key$") as raised:
        crypto.observe_ack_sshsig(_wire(), signer_public_key=b"x" * 256)
    assert raised.value.code == "key"
    assert str(raised.value) == "ack crypto input: key"
    assert clock_calls == [0.0]


def test_admission_precedence_and_hostile_types(monkeypatch: pytest.MonkeyPatch) -> None:
    class HostileBytes(bytes):
        def __len__(self) -> int:
            raise AssertionError("subclass hook must not run")

    monkeypatch.setattr(crypto.evidence_ack_protocol, "parse_envelope", lambda _raw: pytest.fail("parser"))
    for raw, key in (
        (HostileBytes(_wire()), KEY),
        (_wire(), HostileBytes(KEY)),
        (bytearray(_wire()), KEY),
        (memoryview(_wire()), KEY),
        (_wire(), bytearray(KEY)),
        (_wire(), memoryview(KEY)),
        (_wire(), Path("key")),
        (_wire(), [KEY]),
        ({}, KEY),
    ):
        with pytest.raises(crypto.AckCryptoInputError, match="^ack crypto input: type$"):
            crypto.observe_ack_sshsig(raw, signer_public_key=key)  # type: ignore[arg-type]
    with pytest.raises(crypto.AckCryptoInputError, match="^ack crypto input: size$"):
        crypto.observe_ack_sshsig(b"x" * 131_073, signer_public_key=b"bad")
    with pytest.raises(crypto.AckCryptoInputError, match="^ack crypto input: size$"):
        crypto.observe_ack_sshsig(b"x" * 131_073, signer_public_key=b"x" * 257)


def test_raw_cap_reaches_parser_before_key_or_io(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[bytes] = []

    def parser(raw: bytes) -> evidence_ack_protocol.AckEnvelope:
        seen.append(raw)
        raise evidence_ack_protocol.AckWireError("json")

    monkeypatch.setattr(crypto.evidence_ack_protocol, "parse_envelope", parser)
    monkeypatch.setattr(crypto.shutil, "which", lambda _name: pytest.fail("executable"))
    with pytest.raises(evidence_ack_protocol.AckWireError, match="^ack wire: json$"):
        crypto.observe_ack_sshsig(b"x" * 131_072, signer_public_key=KEY)
    assert seen == [b"x" * 131_072]


@pytest.mark.parametrize(
    "key",
    [
        b"",
        KEY[:-1],
        KEY + b"x",
        KEY.replace(b" ", b"\t", 1),
        KEY + b"\r",
        KEY[:20] + b"\xff" + KEY[21:],
        b"ssh-rsa " + KEY[12:],
        b"ssh-ed25519 " + b"A" * 67 + b"=",
        _key_for_blob(b"\x00\x00\x00\x0assh-ed25519\x00\x00\x00 " + bytes(range(32))),
        _key_for_blob(b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x1f" + bytes(range(32))),
        _key_for_blob(b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00 " + bytes(range(33))),
    ],
)
def test_full_key_grammar_refuses_without_parser(monkeypatch: pytest.MonkeyPatch, key: bytes) -> None:
    monkeypatch.setattr(crypto.evidence_ack_protocol, "parse_envelope", lambda _raw: pytest.fail("parser"))
    with pytest.raises(crypto.AckCryptoInputError, match="^ack crypto input: key$"):
        crypto.observe_ack_sshsig(_wire(), signer_public_key=key)


def test_constructed_wire_object_is_refused_before_operations(monkeypatch: pytest.MonkeyPatch) -> None:
    parsed = evidence_ack_protocol.parse_envelope(_wire())
    clock_calls = _refusal_sentinels(monkeypatch)
    with pytest.raises(crypto.AckCryptoInputError, match="^ack crypto input: type$") as raised:
        crypto.observe_ack_sshsig(parsed, signer_public_key=KEY)  # type: ignore[arg-type]
    assert raised.value.code == "type"
    assert str(raised.value) == "ack crypto input: type"
    assert clock_calls == [0.0]


def test_malformed_admitted_wire_propagates_without_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(crypto.shutil, "which", lambda _name: pytest.fail("must not resolve executable"))
    with pytest.raises(evidence_ack_protocol.AckWireError):
        crypto.observe_ack_sshsig(b"{}", signer_public_key=KEY)


def test_key_translation_suppresses_base64_context() -> None:
    with pytest.raises(crypto.AckCryptoInputError) as raised:
        crypto.observe_ack_sshsig(_wire(), signer_public_key=KEY.replace(b"A", b"-", 1))
    assert raised.value.code == "key"
    assert raised.value.__suppress_context__ is True


def test_runner_operational_failure_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(crypto.shutil, "which", lambda _name: "ssh-keygen")

    def raises(*_args: object) -> proc.Result:
        raise RuntimeError("runner failure")

    monkeypatch.setattr(crypto, "_run_verify", raises)
    assert crypto.observe_ack_sshsig(_wire(), signer_public_key=KEY).signature == "UNAVAILABLE"


def test_programming_error_outside_runner_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_runner(monkeypatch, _result())

    def raises(*_args: object) -> crypto.AckCryptoObservation:
        raise ValueError("classification failure")

    monkeypatch.setattr(crypto, "_classified", raises)
    with pytest.raises(ValueError, match="^classification failure$"):
        crypto.observe_ack_sshsig(_wire(), signer_public_key=KEY)


def test_programming_oserror_outside_runner_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """A classifier bug is not an operational runner failure."""

    _fake_runner(monkeypatch, _result())

    def raises(*_args: object) -> crypto.AckCryptoObservation:
        raise OSError("classification failure")

    monkeypatch.setattr(crypto, "_classified", raises)
    with pytest.raises(OSError, match="^classification failure$"):
        crypto.observe_ack_sshsig(_wire(), signer_public_key=KEY)


@pytest.mark.parametrize(
    "code,expected",
    [
        (1, "NOT-VERIFIED"),
        (2, "NOT-VERIFIED"),
        (255, "NOT-VERIFIED"),
        (-1, "UNAVAILABLE"),
        (124, "UNAVAILABLE"),
        (125, "UNAVAILABLE"),
        (126, "UNAVAILABLE"),
        (127, "UNAVAILABLE"),
    ],
)
def test_result_code_mapping(monkeypatch: pytest.MonkeyPatch, code: int, expected: str) -> None:
    _fake_runner(monkeypatch, _result(code))
    observed = crypto.observe_ack_sshsig(_wire(), signer_public_key=KEY)
    assert observed.signature == expected
    assert observed.verified_fingerprint is None
    assert observed.keyid == "UNRESOLVED"


@pytest.mark.parametrize(
    "field,value",
    [
        ("stdout_decode_error", "bad"),
        ("stderr_decode_error", "bad"),
        ("output_limit_exceeded", True),
        ("stream_limit_exceeded", True),
        ("incomplete_process_group", True),
        ("descendants_reaped", False),
    ],
)
def test_result_flags_are_unavailable(monkeypatch: pytest.MonkeyPatch, field: str, value: object) -> None:
    _fake_runner(monkeypatch, _result(**{field: value}))
    assert crypto.observe_ack_sshsig(_wire(), signer_public_key=KEY).signature == "UNAVAILABLE"


@pytest.mark.parametrize(
    "result",
    [
        object(),
        type("Subclass", (proc.Result,), {})(0, "", "", descendants_reaped=True),
        _result(True),
        _result(0, output_limit_exceeded=1),
        _result(0, stdout_decode_error=b"bad"),
        _result(1, stderr_decode_error=object()),
        _result(0, output_limit_exceeded=True, stdout="verified revoked timeout"),
        _result(1, stream_limit_exceeded=True, stderr="signature verified"),
    ],
)
def test_malformed_result_shapes_and_failure_precedence(monkeypatch: pytest.MonkeyPatch, result: object) -> None:
    _fake_runner(monkeypatch, result)
    assert _neutral(crypto.observe_ack_sshsig(_wire(), signer_public_key=KEY)) == ("UNAVAILABLE", "UNRESOLVED", None)


@pytest.mark.parametrize(
    "field",
    [
        "output_limit_exceeded",
        "stream_limit_exceeded",
        "incomplete_process_group",
        "descendants_reaped",
    ],
)
def test_every_result_flag_requires_exact_bool(monkeypatch: pytest.MonkeyPatch, field: str) -> None:
    _fake_runner(monkeypatch, _result(**{field: 1}))
    assert _neutral(crypto.observe_ack_sshsig(_wire(), signer_public_key=KEY)) == ("UNAVAILABLE", "UNRESOLVED", None)


def test_valid_mismatch_keeps_signature_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_runner(monkeypatch, _result())
    observed = crypto.observe_ack_sshsig(_wire(), signer_public_key=KEY)
    assert (observed.signature, observed.keyid, observed.verified_fingerprint) == ("VALID", "MISMATCH", FINGERPRINT)


def test_unavailable_when_executable_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(crypto.shutil, "which", lambda _name: None)
    observed = crypto.observe_ack_sshsig(_wire(), signer_public_key=KEY)
    assert observed.signature == "UNAVAILABLE"
    assert observed.asserted_request_id == REQUEST_ID
    assert observed.asserted_result == "received"


def test_deadline_before_launch_and_after_runner_clear_provisional_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    launched = False
    monkeypatch.setattr(crypto.shutil, "which", lambda _name: "ssh-keygen")

    def no_launch(*_args: object) -> proc.Result:
        nonlocal launched
        launched = True
        return _result()

    monkeypatch.setattr(crypto, "_run_verify", no_launch)
    before_launch = iter((0.0, 5.0))
    monkeypatch.setattr(crypto.time, "monotonic", lambda: next(before_launch))
    assert _neutral(crypto.observe_ack_sshsig(_wire(), signer_public_key=KEY)) == ("UNAVAILABLE", "UNRESOLVED", None)
    assert not launched

    _fake_runner(monkeypatch, _result())
    after_runner = iter((0.0, 0.0, 0.0, 0.0, 5.0, 5.0))
    monkeypatch.setattr(crypto.time, "monotonic", lambda: next(after_runner))
    assert _neutral(crypto.observe_ack_sshsig(_wire(keyid=FINGERPRINT), signer_public_key=KEY)) == (
        "UNAVAILABLE",
        "UNRESOLVED",
        None,
    )


def test_cleanup_failure_and_post_cleanup_deadline_clear_valid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class TemporaryDirectory:
        def __init__(self) -> None:
            self.name = str(tmp_path)

        def cleanup(self) -> None:
            raise OSError("cleanup")

    monkeypatch.setattr(crypto.tempfile, "TemporaryDirectory", TemporaryDirectory)
    _fake_runner(monkeypatch, _result())
    assert _neutral(crypto.observe_ack_sshsig(_wire(keyid=FINGERPRINT), signer_public_key=KEY)) == (
        "UNAVAILABLE",
        "UNRESOLVED",
        None,
    )


@pytest.mark.parametrize("operation", ["temporary", "write"])
def test_temp_operations_fail_closed(monkeypatch: pytest.MonkeyPatch, operation: str) -> None:
    monkeypatch.setattr(crypto.shutil, "which", lambda _name: "ssh-keygen")
    if operation == "temporary":

        def temporary() -> None:
            raise OSError("temporary")

        monkeypatch.setattr(crypto.tempfile, "TemporaryDirectory", temporary)
    else:
        monkeypatch.setattr(crypto, "_write_private", lambda *_args: (_ for _ in ()).throw(OSError("write")))
    assert _neutral(crypto.observe_ack_sshsig(_wire(), signer_public_key=KEY)) == ("UNAVAILABLE", "UNRESOLVED", None)


def test_normal_cleanup_removes_tempdir_and_programming_boundaries_propagate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    _fake_runner(monkeypatch, _result(), captured)
    assert crypto.observe_ack_sshsig(_wire(), signer_public_key=KEY).signature == "VALID"
    assert not Path(str(captured["directory"])).exists()
    _fake_runner(monkeypatch, _result())
    monkeypatch.setattr(crypto.attestation, "dsse_pae", lambda *_args: (_ for _ in ()).throw(OSError("pae")))
    with pytest.raises(OSError, match="^pae$"):
        crypto.observe_ack_sshsig(_wire(), signer_public_key=KEY)


def test_direct_private_close_error_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    parsed = evidence_ack_protocol.parse_envelope(_wire())
    real_open = os.open
    real_close = os.close
    real_temporary_directory = crypto.tempfile.TemporaryDirectory
    opened: set[int] = set()
    closed: set[int] = set()
    allowed_signers: set[int] = set()
    close_failures = 0

    class TrackedTemporaryDirectory:
        def __init__(self) -> None:
            self._directory = real_temporary_directory()
            self.name = self._directory.name
            self.cleaned = False
            directories.append(self)

        def cleanup(self) -> None:
            self._directory.cleanup()
            self.cleaned = True

    directories: list[TrackedTemporaryDirectory] = []

    def open_wrapper(path: str | bytes | os.PathLike[str] | os.PathLike[bytes], *args: object, **kwargs: object) -> int:
        descriptor = real_open(path, *args, **kwargs)  # type: ignore[arg-type]
        opened.add(descriptor)
        if Path(path).name == "allowed_signers":
            allowed_signers.add(descriptor)
        return descriptor

    def close_wrapper(descriptor: int) -> None:
        nonlocal close_failures
        if descriptor in allowed_signers and descriptor not in closed:
            real_close(descriptor)
            closed.add(descriptor)
            close_failures += 1
            raise OSError("injected close")
        real_close(descriptor)
        closed.add(descriptor)

    monkeypatch.setattr(crypto.tempfile, "TemporaryDirectory", TrackedTemporaryDirectory)
    monkeypatch.setattr(crypto.os, "open", open_wrapper)
    monkeypatch.setattr(crypto.os, "close", close_wrapper)
    monkeypatch.setattr(crypto, "_run_verify", lambda *_args: pytest.fail("runner"))
    try:
        observed = crypto.observe_ack_sshsig(_wire(), signer_public_key=KEY)
        assert dataclasses.astuple(observed) == (
            "UNAVAILABLE",
            "UNRESOLVED",
            None,
            parsed.claim.request_id,
            parsed.claim.result,
            parsed.envelope_id,
        )
        assert len(allowed_signers) == 1
        assert close_failures == 1
        assert all(directory.cleaned for directory in directories)
        assert all(not Path(directory.name).exists() for directory in directories)
    finally:
        for descriptor in opened - closed:
            try:
                real_close(descriptor)
            except OSError:
                pass
        for directory in directories:
            if not directory.cleaned:
                directory.cleanup()


def test_post_cleanup_deadline_alone_clears_provisional_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    real_cleanup = crypto.tempfile.TemporaryDirectory.cleanup
    state = [0.0]
    clock_reads: list[float] = []
    runner_calls = 0
    cleanup_succeeded = False
    classified: list[tuple[str, str, str | None]] = []

    def clock() -> float:
        value = state[0]
        clock_reads.append(value)
        state[0] = 1.0
        return value

    def cleanup(directory: object) -> None:
        nonlocal cleanup_succeeded
        real_cleanup(directory)  # type: ignore[arg-type]
        cleanup_succeeded = True
        state[0] = 5.0

    real_classified = crypto._classified

    def classified_wrapper(*args: object) -> crypto.AckCryptoObservation:
        observed = real_classified(*args)  # type: ignore[arg-type]
        classified.append(_neutral(observed))
        return observed

    def runner(*_args: object) -> proc.Result:
        nonlocal runner_calls
        runner_calls += 1
        return _result()

    monkeypatch.setattr(crypto.time, "monotonic", clock)
    monkeypatch.setattr(crypto.shutil, "which", lambda _name: "ssh-keygen")
    monkeypatch.setattr(crypto, "_run_verify", runner)
    monkeypatch.setattr(crypto, "_classified", classified_wrapper)
    monkeypatch.setattr(crypto.tempfile.TemporaryDirectory, "cleanup", cleanup)
    observed = crypto.observe_ack_sshsig(_wire(keyid=FINGERPRINT), signer_public_key=KEY)
    assert runner_calls == 1
    assert cleanup_succeeded
    assert classified == [("VALID", "MATCH", FINGERPRINT)]
    assert clock_reads[0] == 0.0
    assert all(read < 5.0 for read in clock_reads[:-1])
    assert clock_reads[-1] == 5.0
    assert dataclasses.astuple(observed) == (
        "UNAVAILABLE",
        "UNRESOLVED",
        None,
        REQUEST_ID,
        "received",
        parsed_envelope_id := evidence_ack_protocol.parse_envelope(_wire(keyid=FINGERPRINT)).envelope_id,
    )
    assert parsed_envelope_id == observed.envelope_id


def test_preparation_time_reduces_runner_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    state = [10.0]
    phases: list[str] = []
    captured: dict[str, object] = {}
    real_write_private = crypto._write_private

    def clock() -> float:
        return state[0]

    def write_private(path: Path, contents: bytes) -> None:
        real_write_private(path, contents)
        phases.append(f"write:{path.name}")
        state[0] += 1.25

    def runner(*args: object) -> proc.Result:
        phases.append("runner")
        captured["remaining"] = args[-1]
        captured["directory"] = args[3]
        return _result()

    monkeypatch.setattr(crypto.time, "monotonic", clock)
    monkeypatch.setattr(crypto.shutil, "which", lambda _name: "ssh-keygen")
    monkeypatch.setattr(crypto, "_write_private", write_private)
    monkeypatch.setattr(crypto, "_run_verify", runner)
    observed = crypto.observe_ack_sshsig(_wire(keyid=FINGERPRINT), signer_public_key=KEY)
    assert phases == ["write:allowed_signers", "write:signature.sig", "runner"]
    assert captured["remaining"] == 2.5
    assert not Path(str(captured["directory"])).exists()
    assert dataclasses.astuple(observed) == (
        "VALID",
        "MATCH",
        FINGERPRINT,
        REQUEST_ID,
        "received",
        evidence_ack_protocol.parse_envelope(_wire(keyid=FINGERPRINT)).envelope_id,
    )


@pytest.mark.parametrize(
    "code,stdout,stderr,expected",
    [
        (0, "verification failed revoked timeout", "bad signature", ("VALID", "MATCH", FINGERPRINT)),
        (1, "Good signature verified", "signature verification succeeded", ("NOT-VERIFIED", "UNRESOLVED", None)),
    ],
    ids=("clean-zero-failure-text", "clean-one-success-text"),
)
def test_clean_result_ignores_output_prose(
    monkeypatch: pytest.MonkeyPatch,
    code: int,
    stdout: str,
    stderr: str,
    expected: tuple[str, str, str | None],
) -> None:
    result = proc.Result(
        code=code,
        stdout=stdout,
        stderr=stderr,
        stdout_decode_error=None,
        stderr_decode_error=None,
        output_limit_exceeded=False,
        stream_limit_exceeded=False,
        incomplete_process_group=False,
        descendants_reaped=True,
    )
    runner_calls = 0
    captured: dict[str, object] = {}

    def runner(*args: object) -> proc.Result:
        nonlocal runner_calls
        runner_calls += 1
        captured["directory"] = args[3]
        return result

    monkeypatch.setattr(crypto.shutil, "which", lambda _name: "ssh-keygen")
    monkeypatch.setattr(crypto, "_run_verify", runner)
    observed = crypto.observe_ack_sshsig(_wire(keyid=FINGERPRINT), signer_public_key=KEY)
    assert runner_calls == 1
    assert not Path(str(captured["directory"])).exists()
    assert dataclasses.astuple(observed) == (
        *expected,
        REQUEST_ID,
        "received",
        evidence_ack_protocol.parse_envelope(_wire(keyid=FINGERPRINT)).envelope_id,
    )


def test_unexpected_clock_oserror_propagates_after_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def monotonic() -> float:
        nonlocal calls
        calls += 1
        if calls == 5:
            raise OSError("clock")
        return 0.0

    _fake_runner(monkeypatch, _result())
    monkeypatch.setattr(crypto.time, "monotonic", monotonic)
    with pytest.raises(OSError, match="^clock$"):
        crypto.observe_ack_sshsig(_wire(), signer_public_key=KEY)


def test_constructor_is_closed_and_bounds_hostile_subclasses() -> None:
    class Hostile(str):
        def __len__(self) -> int:
            raise AssertionError("subclass hook must not run")

    valid = ("VALID", "MATCH", FINGERPRINT, REQUEST_ID, "received", "0" * 64)
    assert crypto.AckCryptoObservation(*valid).signature == "VALID"
    for values in (
        (Hostile("VALID"), "MATCH", FINGERPRINT, REQUEST_ID, "received", "0" * 64),
        ("VALID", "MATCH", "x" * 51, REQUEST_ID, "received", "0" * 64),
        ("NOT-VERIFIED", "MATCH", None, REQUEST_ID, "received", "0" * 64),
        ("VALID", "UNRESOLVED", FINGERPRINT, REQUEST_ID, "received", "0" * 64),
    ):
        with pytest.raises(ValueError, match="^invalid acknowledgement crypto observation$"):
            crypto.AckCryptoObservation(*values)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="^invalid acknowledgement crypto input error code$"):
        crypto.AckCryptoInputError(Hostile("type"))
    with pytest.raises(ValueError, match="^invalid acknowledgement crypto input error code$"):
        crypto.AckCryptoInputError("long")


@pytest.mark.parametrize(
    "values",
    [
        ("VALID", "OTHER", FINGERPRINT, REQUEST_ID, "received", "0" * 64),
        ("VALID", "MATCH", FINGERPRINT, REQUEST_ID, "pending", "0" * 64),
        ("VALID", "MATCH", FINGERPRINT, REQUEST_ID, "received", "g" * 64),
        ("UNAVAILABLE", "MATCH", None, REQUEST_ID, "received", "0" * 64),
        ("UNAVAILABLE", "UNRESOLVED", FINGERPRINT, REQUEST_ID, "received", "0" * 64),
    ],
    ids=(
        "keyid-under-cap",
        "asserted-result-under-cap",
        "envelope-digest-nonhex",
        "unavailable-resolved-hint",
        "unavailable-fingerprint",
    ),
)
def test_constructor_missing_lexical_and_neutral_invariants(
    monkeypatch: pytest.MonkeyPatch, values: tuple[str | None, ...]
) -> None:
    monkeypatch.setattr(crypto.evidence_ack_protocol, "parse_envelope", lambda _raw: pytest.fail("parser"))
    monkeypatch.setattr(crypto.shutil, "which", lambda _name: pytest.fail("executable"))
    monkeypatch.setattr(crypto.tempfile, "TemporaryDirectory", lambda: pytest.fail("temporary directory"))
    monkeypatch.setattr(crypto, "_write_private", lambda *_args: pytest.fail("private write"))
    monkeypatch.setattr(crypto, "_run_verify", lambda *_args: pytest.fail("runner"))
    monkeypatch.setattr(crypto.time, "monotonic", lambda: pytest.fail("clock"))
    with pytest.raises(ValueError, match="^invalid acknowledgement crypto observation$") as raised:
        crypto.AckCryptoObservation(*values)  # type: ignore[arg-type]
    assert str(raised.value) == "invalid acknowledgement crypto observation"


def test_constructor_surface_and_every_field_boundary() -> None:
    valid = ("VALID", "MATCH", FINGERPRINT, REQUEST_ID, "received", "0" * 64)
    observed = crypto.AckCryptoObservation(*valid)
    assert tuple(field.name for field in dataclasses.fields(observed)) == (
        "signature",
        "keyid",
        "verified_fingerprint",
        "asserted_request_id",
        "asserted_result",
        "envelope_id",
    )
    assert not hasattr(observed, "__dict__")
    with pytest.raises(dataclasses.FrozenInstanceError):
        observed.signature = "UNAVAILABLE"  # type: ignore[misc]

    class Hostile(str):
        def __len__(self) -> int:
            raise AssertionError("subclass hook must not run")

    for index, value in enumerate(valid):
        hostile = list(valid)
        hostile[index] = Hostile(value)
        with pytest.raises(ValueError, match="^invalid acknowledgement crypto observation$"):
            crypto.AckCryptoObservation(*hostile)  # type: ignore[arg-type]
    invalids = (
        ("V" * 13, "MATCH", FINGERPRINT, REQUEST_ID, "received", "0" * 64),
        ("VALID", "M" * 11, FINGERPRINT, REQUEST_ID, "received", "0" * 64),
        ("VALID", "MATCH", "x" * 51, REQUEST_ID, "received", "0" * 64),
        ("VALID", "MATCH", FINGERPRINT, "0" * 65, "received", "0" * 64),
        ("VALID", "MATCH", FINGERPRINT, REQUEST_ID, "r" * 9, "0" * 64),
        ("VALID", "MATCH", FINGERPRINT, REQUEST_ID, "received", "0" * 65),
        ("VALID", "MATCH", "SHA256:" + "A" * 43, "F" * 64, "received", "0" * 64),
        ("VALID", "MATCH", "SHA256:" + "A" * 42 + "=", REQUEST_ID, "received", "0" * 64),
        ("NOT-VERIFIED", "UNRESOLVED", None, REQUEST_ID, "received", "0" * 64),
        ("UNAVAILABLE", "UNRESOLVED", None, REQUEST_ID, "rejected", "0" * 64),
        ("VALID", "MISMATCH", FINGERPRINT, REQUEST_ID, "received", "0" * 64),
        ("VALID", "MATCH", FINGERPRINT, REQUEST_ID, "received", "0" * 64),
        ("INVALID", "UNRESOLVED", None, REQUEST_ID, "received", "0" * 64),
    )
    for values in invalids[:8]:
        with pytest.raises(ValueError, match="^invalid acknowledgement crypto observation$"):
            crypto.AckCryptoObservation(*values)
    assert crypto.AckCryptoObservation(*invalids[8]).signature == "NOT-VERIFIED"
    assert crypto.AckCryptoObservation(*invalids[9]).signature == "UNAVAILABLE"
    assert crypto.AckCryptoObservation(*invalids[10]).keyid == "MISMATCH"
    assert crypto.AckCryptoObservation(*invalids[11]).signature == "VALID"
    with pytest.raises(ValueError, match="^invalid acknowledgement crypto observation$"):
        crypto.AckCryptoObservation(*invalids[12])
    for code in (None, 1, "types"):
        with pytest.raises(ValueError, match="^invalid acknowledgement crypto input error code$"):
            crypto.AckCryptoInputError(code)  # type: ignore[arg-type]
    error = crypto.AckCryptoInputError("key")
    with pytest.raises(AttributeError):
        error.code = "type"  # type: ignore[misc]


def test_write_private_flags_mode_partial_zero_and_cleanup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[object] = []
    writes = iter((2, 1))
    monkeypatch.setattr(crypto.os, "open", lambda *args: calls.append(args) or 41)
    monkeypatch.setattr(crypto.os, "write", lambda fd, data: calls.append((fd, data)) or next(writes))
    monkeypatch.setattr(crypto.os, "close", lambda fd: calls.append(("close", fd)))
    crypto._write_private(tmp_path / "private", b"abc")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    assert calls[0] == (tmp_path / "private", flags, 0o600)
    assert calls[1:3] == [(41, b"abc"), (41, b"c")]
    assert calls[-1] == ("close", 41)
    monkeypatch.setattr(crypto.os, "write", lambda _fd, _data: 0)
    with pytest.raises(OSError, match="^short write$"):
        crypto._write_private(tmp_path / "zero", b"x")
    assert calls[-1] == ("close", 41)


def test_private_runner_uses_one_exact_proc_call(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def run(*args: object, **kwargs: object) -> proc.Result:
        captured.append((args, kwargs))
        return _result()

    monkeypatch.setattr(crypto.proc, "run", run)
    signers = tmp_path / "allowed_signers"
    signature = tmp_path / "signature.sig"
    result = crypto._run_verify("ssh-keygen", signers, signature, tmp_path, b"pae", 1.25)
    assert result.code == 0
    assert captured == [
        (
            (
                [
                    "ssh-keygen",
                    "-Y",
                    "verify",
                    "-f",
                    str(signers),
                    "-I",
                    "brigade-ack-observer",
                    "-n",
                    "brigade-evidence-ack",
                    "-s",
                    str(signature),
                ],
            ),
            {"timeout": 1.25, "cwd": tmp_path, "stdin": b"pae", "supervise_group": True},
        )
    ]


def test_maximum_private_pae_arithmetic() -> None:
    payload = b"x" * evidence_ack_protocol.MAX_STATEMENT_BYTES
    assert len(attestation.dsse_pae(attestation.DSSE_PAYLOAD_TYPE, payload)) == 8_236


def _generate_key(tmp_path: Path, name: str) -> tuple[Path, bytes, str]:
    key_path = tmp_path / name
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key_path)],
        check=True,
        capture_output=True,
    )
    fields = Path(f"{key_path}.pub").read_bytes().split()
    assert len(fields) >= 2
    public_key = b" ".join(fields[:2])
    assert len(public_key) == 80
    blob = base64.b64decode(public_key.split(b" ")[1], validate=True)
    fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")
    return key_path, public_key, fingerprint


def _signed_wire(key_path: Path) -> bytes:
    envelope = attestation.create_envelope(_statement(), key_path, namespace="brigade-evidence-ack")
    return json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()


def test_return_surface_and_forbidden_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_runner(monkeypatch, _result())
    for name in (
        "verify_attestation",
        "get_key_fingerprint",
        "get_public_key_fingerprint",
        "extract_keyid_from_sshsig",
        "default_allowed_signers_path",
        "default_revoked_keys_path",
    ):
        monkeypatch.setattr(
            crypto.attestation,
            name,
            lambda *_args, _name=name: pytest.fail(_name),
            raising=False,
        )
    monkeypatch.setattr(crypto.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("second process"))
    observed = crypto.observe_ack_sshsig(_wire(keyid=FINGERPRINT), signer_public_key=KEY)
    assert tuple(observed.__slots__) == (
        "signature",
        "keyid",
        "verified_fingerprint",
        "asserted_request_id",
        "asserted_result",
        "envelope_id",
    )
    assert all(type(value) in {str, type(None)} for value in dataclasses.astuple(observed))
    assert KEY.decode("ascii") not in repr(observed)
    assert ARMOR.decode("ascii") not in repr(observed)


@pytest.mark.parametrize(
    "case,mutate,key_selector,expected,asserted_result",
    [
        ("same-key", lambda raw: raw, "signer", ("VALID", "MATCH", "fingerprint"), "received"),
        (
            "changed-hint",
            lambda raw: {**raw, "signatures": [{**raw["signatures"][0], "keyid": "SHA256:" + "A" * 43}]},
            "signer",
            ("VALID", "MISMATCH", "fingerprint"),
            "received",
        ),
        ("different-key", lambda raw: raw, "other", ("NOT-VERIFIED", "UNRESOLVED", None), "received"),
        (
            "changed-pae",
            lambda raw: {
                **raw,
                "payload": base64.b64encode(
                    json.dumps(_statement(result="rejected"), sort_keys=True, separators=(",", ":")).encode()
                ).decode(),
            },
            "signer",
            ("NOT-VERIFIED", "UNRESOLVED", None),
            "rejected",
        ),
        (
            "malformed-inner-sshsig",
            lambda raw: {**raw, "signatures": [{**raw["signatures"][0], "sig": base64.b64encode(ARMOR).decode()}]},
            "signer",
            ("NOT-VERIFIED", "UNRESOLVED", None),
            "received",
        ),
    ],
    ids=("same-key", "changed-hint", "different-key", "changed-pae", "malformed-inner-sshsig"),
)
def test_real_ed25519_observation(
    tmp_path: Path,
    case: str,
    mutate: object,
    key_selector: str,
    expected: tuple[str, str, str | None],
    asserted_result: str,
) -> None:
    assert shutil.which("ssh-keygen"), "ssh-keygen is required for acknowledgement crypto acceptance"
    key_path, public_key, fingerprint = _generate_key(tmp_path, "signer")
    other_path, other_public_key, _ = _generate_key(tmp_path, "other")
    envelope = json.loads(_signed_wire(key_path))
    assert callable(mutate)
    raw = json.dumps(mutate(envelope), sort_keys=True, separators=(",", ":")).encode()
    key = public_key if key_selector == "signer" else other_public_key
    observed = crypto.observe_ack_sshsig(raw, signer_public_key=key)
    outcome = _neutral(observed)
    fingerprint_expected = fingerprint if expected[2] == "fingerprint" else expected[2]
    assert outcome == (expected[0], expected[1], fingerprint_expected), case
    assert _independent_request_id() == REQUEST_ID
    decoded_statement = json.loads(base64.b64decode(json.loads(raw)["payload"], validate=True))
    assert decoded_statement["subject"][0]["digest"]["sha256"] == REQUEST_ID
    assert dataclasses.astuple(observed) == (
        expected[0],
        expected[1],
        fingerprint_expected,
        _independent_request_id(),
        asserted_result,
        _independent_envelope_id(raw),
    )
    assert other_path.exists()
