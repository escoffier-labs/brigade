"""Unit tests for the shared HMAC broker and capability hand-off."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from brigade import authority_broker, authority_key, scanner_isolation
from brigade.work_cmd import scanners as scanners_mod


def test_mac_is_domain_separated():
    secret = os.urandom(32)
    payload = b"same-bytes"
    store = authority_broker.mac(secret, authority_broker.STORE_DOMAIN, payload)
    cap = authority_broker.mac(secret, authority_broker.CAPABILITY_DOMAIN, payload)
    assert store != cap
    assert authority_broker.verify_mac(secret, authority_broker.STORE_DOMAIN, payload, store)
    assert not authority_broker.verify_mac(secret, authority_broker.CAPABILITY_DOMAIN, payload, store)


def test_mac_without_secret_is_refused():
    with pytest.raises(ValueError, match="without a secret"):
        authority_broker.mac(b"", authority_broker.STORE_DOMAIN, b"x")


def test_capability_round_trip_and_rebind():
    secret = authority_broker.new_capability_secret()
    cap = authority_broker.mint_trust_capability(
        secret,
        item_id="item-a",
        from_digest="a" * 64,
        to_label="verified",
        mark_injection_clean=True,
    )
    body = authority_broker.verify_trust_capability(
        secret,
        cap,
        item_id="item-a",
        from_digest="a" * 64,
        to_label="verified",
        mark_injection_clean=True,
    )
    assert body["nonce"] == cap["nonce"]
    with pytest.raises(ValueError, match="item_id"):
        authority_broker.verify_trust_capability(
            secret,
            cap,
            item_id="item-b",
            from_digest="a" * 64,
            to_label="verified",
            mark_injection_clean=True,
        )
    with pytest.raises(ValueError, match="mark_injection_clean"):
        authority_broker.verify_trust_capability(
            secret,
            cap,
            item_id="item-a",
            from_digest="a" * 64,
            to_label="verified",
            mark_injection_clean=False,
        )
    expired = dict(cap)
    expired["expiry"] = 1
    expired["mac"] = authority_broker.mac(
        secret,
        authority_broker.CAPABILITY_DOMAIN,
        authority_broker.canonical_dumps(authority_broker.capability_mac_body(expired)),
    )
    with pytest.raises(ValueError, match="expired"):
        authority_broker.verify_trust_capability(
            secret,
            expired,
            item_id="item-a",
            from_digest="a" * 64,
            to_label="verified",
            mark_injection_clean=True,
        )


def test_handoff_never_puts_secret_in_env_construction():
    source = Path(scanners_mod.__file__).read_text(encoding="utf-8")
    notify = Path(__file__).resolve().parents[1] / "src" / "brigade" / "trust_gate.py"
    text = notify.read_text(encoding="utf-8")
    assert "BRIGADE_AUTHORITY_KEY_FILE" not in scanners_mod._SCANNER_CHILD_ENV_ALLOWLIST
    assert "input=handoff.decode" in text
    assert 'child_env["BRIGADE_REQUIRE_TRUST_CAPABILITY"]' in text
    assert "BRIGADE_CAPABILITY_SECRET" not in source
    assert "capture_output" in source


def test_key_load_refuses_world_readable(tmp_path: Path, monkeypatch):
    if os.name != "posix":
        pytest.skip("POSIX file-mode bits are the authority-key residual check")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    authority_key.clear_key_cache()
    authority_key.generate_key()
    path = authority_key.key_path()
    os.chmod(path, 0o644)
    authority_key.clear_key_cache()
    with pytest.raises(OSError, match="group or world readable"):
        authority_key.load_key()


def test_authority_store_open_flags_are_windows_safe():
    """Bare os.O_NOFOLLOW / os.O_DIRECTORY AttributeError on native Windows."""

    import inspect

    from brigade.work_cmd import ledger as ledger_mod

    key_src = Path(authority_key.__file__).read_text(encoding="utf-8")
    write = inspect.getsource(ledger_mod._write_external_directory_authority)
    read = inspect.getsource(ledger_mod._read_external_directory_authority_path)
    for source in (key_src, write, read):
        assert "os.O_NOFOLLOW" not in source
        assert "os.O_DIRECTORY" not in source
    assert 'getattr(os, "O_NOFOLLOW"' in write
    assert 'getattr(os, "O_NOFOLLOW"' in read
    assert "sync_parent_directory" in write
    close_before_replace = write.find("os.close(descriptor)")
    replace_at = write.find("os.replace(temporary, path)")
    assert close_before_replace != -1 and replace_at != -1
    assert close_before_replace < replace_at


def test_doctor_isolation_is_never_silent():
    status, name, detail = scanner_isolation.doctor_check()
    assert status in {"OK", "WARN", "MANUAL", "FAIL"}
    assert name == "security: scanner isolation"
    assert detail
