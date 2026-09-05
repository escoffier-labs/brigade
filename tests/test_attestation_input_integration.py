from __future__ import annotations

from brigade import attestation


def test_verify_rejects_duplicate_names(monkeypatch) -> None:
    called = False

    def fake_which(_: str) -> str:
        return "ssh-keygen"

    def fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal called
        called = True
        raise AssertionError("signature program must not run for invalid input")

    monkeypatch.setattr(attestation.shutil, "which", fake_which)
    monkeypatch.setattr(attestation.subprocess, "run", fake_run)

    result = attestation.verify_attestation(
        '{"payloadType":"application/vnd.in-toto+json","payload":"e30=","payload":"e30=","signatures":[],"brigade":{}}'
    )

    assert result.status == attestation.STATUS_UNVERIFIABLE_SIGNATURE
    assert called is False
