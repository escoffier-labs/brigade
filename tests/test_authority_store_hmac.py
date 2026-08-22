"""Mutation tests for the HMAC authority store and residual-honesty boundary."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

from brigade import authority_broker, authority_key, authority_marker, cli, scanner_isolation
from brigade.security_cmd import AUTHORITY_STORE_ISOLATION_EXTERNAL_KEY
from brigade.work_cmd import constants, helpers, ledger


def _disable_external_key_isolation(tmp_path: Path) -> None:
    config = tmp_path / ".brigade" / "security.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        "\n".join(
            [
                'policy = "personal"',
                "",
                "[authority_store]",
                'isolation = "off"',
                "",
            ]
        ),
        encoding="utf-8",
    )


def _enable_external_key_isolation(tmp_path: Path) -> None:
    config = tmp_path / ".brigade" / "security.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        "\n".join(
            [
                'policy = "personal"',
                "",
                "[authority_store]",
                f'isolation = "{AUTHORITY_STORE_ISOLATION_EXTERNAL_KEY}"',
                "",
            ]
        ),
        encoding="utf-8",
    )


def _bind_workspace(tmp_path: Path, *, external_key: bool = True) -> dict[str, int]:
    if external_key:
        _enable_external_key_isolation(tmp_path)
    (tmp_path / ".brigade").mkdir(exist_ok=True)
    workspace = ledger._workspace_directory_identity(tmp_path)
    root = os.open(tmp_path / ".brigade", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        ledger._record_external_directory_authority(tmp_path, (".brigade",), root, workspace=workspace)
    finally:
        os.close(root)
    return workspace


def _store_path(tmp_path: Path) -> Path:
    return ledger._directory_authority_store_path(tmp_path)


def _write_g5_receipt(tmp_path: Path, item: dict) -> Path:
    scanner = dict(next(row for row in constants.SCANNER_DEFAULTS if row["id"] == "handoff-ingest"))
    metadata = item.setdefault("metadata", {})
    metadata.update({"scanner_id": scanner["id"], "scanner_run_id": "chosen-run"})
    receipt = {
        "run_id": "chosen-run",
        "scanner_id": scanner["id"],
        "source": scanner["source"],
        "command": scanner["command"],
        "status": "completed",
        "exit_code": 0,
        "self_import_proofs": {
            "scanner_id": scanner["id"],
            "source": scanner["source"],
            "scanner": scanner,
            "imports": [{"id": item["id"], "content_hash": ledger._locally_stamped_import_content_hash(item)}],
        },
    }
    descriptor = ledger._open_verifier_owned_directory(
        tmp_path,
        components=(".brigade", "scanners", "runs"),
        anchor_name=".runs.authority.json",
        create=True,
    )
    os.close(descriptor)
    path = helpers._scanner_runs_root(tmp_path) / "chosen-run" / "receipt.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        ledger._record_verifier_owned_directory(
            tmp_path,
            components=(".brigade", "scanners", "runs", "chosen-run"),
            directory=parent,
        )
    finally:
        os.close(parent)
    path.write_text(json.dumps(receipt), encoding="utf-8")
    data = path.read_bytes()
    handle = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        ledger._record_verifier_owned_file(
            tmp_path,
            components=(".brigade", "scanners", "runs", "chosen-run", "receipt.json"),
            descriptor=handle,
            data=data,
        )
    finally:
        os.close(handle)
    return path


def _g5_same_uid_store_rewrite(tmp_path: Path) -> tuple[object, Path]:
    """Reviewer probe: same-uid write directly to the binding store by path."""

    item = ledger._make_import("g5 forged identity", kind="task", source="handoff-ingest")
    receipt_path = _write_g5_receipt(tmp_path, item)
    ledger._write_persisted_import_proofs(tmp_path, [item], operation_id="0" * 32)
    assert ledger._legacy_import_source_content_identity(item, target=tmp_path) is not None

    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    payload["attacker_marker"] = "same-uid-store-rewrite"
    receipt_path.write_text(json.dumps(payload), encoding="utf-8")
    info = receipt_path.stat()
    binding = {
        "device": info.st_dev,
        "inode": info.st_ino,
        "sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
    }
    store = _store_path(tmp_path)
    current = json.loads(store.read_text(encoding="utf-8"))
    record = current.get("record") if current.get("envelope_version") == 1 else current
    forged = dict(record)
    files = dict(forged.get("files") or {})
    files[".brigade/scanners/runs/chosen-run/receipt.json"] = binding
    forged["files"] = files
    store.write_text(json.dumps(forged), encoding="utf-8")
    return ledger._legacy_import_source_content_identity(item, target=tmp_path), store


def test_positive_parent_write_validates(tmp_path: Path):
    _bind_workspace(tmp_path)
    path, payload = ledger._read_external_directory_authority(tmp_path)
    assert payload is not None
    assert payload["target"] == str(tmp_path.resolve())
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["envelope_version"] == 1
    assert raw["signature"]["alg"] == "HMAC-SHA256"


def test_unsigned_store_rewrite_is_refused_after_sequence_exists(tmp_path: Path):
    _bind_workspace(tmp_path)
    path = _store_path(tmp_path)
    forged = {
        "schema_version": 1,
        "target": str(tmp_path.resolve()),
        "workspace": {"device": 1, "inode": 2},
        "directories": {},
        "files": {".brigade/scanners/runs/x/receipt.json": {"device": 1, "inode": 2, "sha256": "a" * 64}},
    }
    path.write_text(json.dumps(forged), encoding="utf-8")
    with pytest.raises(OSError, match="downgrade|MAC|malformed|sequence"):
        ledger._read_external_directory_authority(tmp_path)


def test_store_rewrite_under_attacker_key_is_refused(tmp_path: Path, monkeypatch):
    _bind_workspace(tmp_path)
    path, payload = ledger._read_external_directory_authority(tmp_path)
    assert payload is not None
    attacker = os.urandom(32)
    forged = dict(payload)
    forged["files"] = {".brigade/scanners/runs/x/receipt.json": {"device": 1, "inode": 2, "sha256": "b" * 64}}
    envelope = authority_broker.sign_store_record(attacker, forged, 99, authority_key.key_id(attacker))
    path.write_text(json.dumps(envelope, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    with pytest.raises(OSError, match="key_id|MAC"):
        ledger._read_external_directory_authority(tmp_path)


def test_cross_target_splice_is_refused(tmp_path: Path):
    other = tmp_path / "other"
    other.mkdir()
    _bind_workspace(tmp_path)
    _bind_workspace(other)
    src = json.loads(_store_path(tmp_path).read_text(encoding="utf-8"))
    _store_path(other).write_text(json.dumps(src), encoding="utf-8")
    with pytest.raises(OSError, match="MAC|target|sequence"):
        ledger._read_external_directory_authority(other)


def test_copy_public_config_self_import_proof_is_refused(tmp_path: Path):
    from brigade.work_cmd import constants

    _bind_workspace(tmp_path)
    item = ledger._make_import("forged self import", kind="task", source="handoff-ingest")
    item["metadata"] = {
        "scanner_run_id": "forged-run",
        "scanner_id": "handoff-ingest",
        "provenance": item["metadata"]["provenance"],
    }
    receipt_dir = tmp_path / ".brigade" / "scanners" / "runs" / "forged-run"
    receipt_dir.mkdir(parents=True)
    receipt = {
        "run_id": "forged-run",
        "status": "completed",
        "exit_code": 0,
        "scanner_id": "handoff-ingest",
        "source": "handoff-ingest",
        "command": next(s["command"] for s in constants.SCANNER_DEFAULTS if s["id"] == "handoff-ingest"),
        "self_import_proofs": {
            "scanner_id": "handoff-ingest",
            "source": "handoff-ingest",
            "scanner": dict(next(s for s in constants.SCANNER_DEFAULTS if s["id"] == "handoff-ingest")),
            "imports": [{"id": item["id"], "content_hash": ledger._locally_stamped_import_content_hash(item)}],
        },
    }
    receipt_path = receipt_dir / "receipt.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    info = receipt_path.stat()
    binding = {
        "device": info.st_dev,
        "inode": info.st_ino,
        "sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
    }
    path, payload = ledger._read_external_directory_authority(tmp_path)
    assert payload is not None
    forged = dict(payload)
    files = dict(forged.get("files") or {})
    files[".brigade/scanners/runs/forged-run/receipt.json"] = binding
    forged["files"] = files
    path.write_text(json.dumps(forged), encoding="utf-8")
    assert ledger._legacy_import_source_content_identity(item, target=tmp_path) is None


def test_residual_honesty_crypto_tier_accepts_key_holder_forgery(tmp_path: Path):
    """Same-UID process that reads the persisted key can still forge under crypto."""

    _bind_workspace(tmp_path)
    path, original = ledger._read_external_directory_authority(tmp_path)
    assert original is not None
    secret, key_id = authority_key.load_key()
    forged = dict(original)
    forged["files"] = {
        ".brigade/scanners/runs/stolen/receipt.json": {"device": 9, "inode": 9, "sha256": "c" * 64},
    }
    sequence = authority_key.next_sequence(
        hashlib.sha256(str(tmp_path.resolve()).encode("utf-8")).hexdigest(),
        secret=secret,
        key_id=key_id,
    )
    envelope = authority_broker.sign_store_record(secret, forged, sequence, key_id)
    path.write_text(json.dumps(envelope, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    accepted, payload = ledger._read_external_directory_authority(tmp_path)
    assert payload is not None
    assert payload["files"][".brigade/scanners/runs/stolen/receipt.json"]["sha256"] == "c" * 64
    assert accepted == path


def test_residual_honesty_isolated_scanners_refuses_same_forgery(tmp_path: Path):
    """The exact key-holder forgery is refused only under --isolated-scanners."""

    status = scanner_isolation.probe_isolation()
    if not status.available:
        pytest.skip(status.reason)
    _bind_workspace(tmp_path)
    path, original = ledger._read_external_directory_authority(tmp_path)
    assert original is not None
    parent_config = os.environ["XDG_CONFIG_HOME"]
    parent_data = os.environ["XDG_DATA_HOME"]
    script = tmp_path / "forge-from-isolated-child.py"
    script.write_text(
        f"""
import hashlib, json, os, sys
from pathlib import Path
from brigade import authority_key, authority_broker
from brigade.work_cmd import ledger

os.environ["XDG_CONFIG_HOME"] = {parent_config!r}
os.environ["XDG_DATA_HOME"] = {parent_data!r}
os.environ.pop("BRIGADE_AUTHORITY_KEY_FILE", None)
authority_key.clear_key_cache()
target = Path({str(tmp_path)!r})
try:
    secret, key_id = authority_key.load_key()
except OSError:
    raise SystemExit(0)
try:
    path, original = ledger._read_external_directory_authority(target)
except OSError:
    raise SystemExit(0)
if original is None:
    raise SystemExit(0)
forged = dict(original)
forged["files"] = {{".brigade/scanners/runs/stolen/receipt.json": {{"device": 9, "inode": 9, "sha256": "c" * 64}}}}
digest = hashlib.sha256(str(target.resolve()).encode("utf-8")).hexdigest()
sequence = authority_key.next_sequence(digest, secret=secret, key_id=key_id)
envelope = authority_broker.sign_store_record(secret, forged, sequence, key_id)
path.write_text(json.dumps(envelope, sort_keys=True, separators=(",", ":")))
"""
    )
    from brigade.work_cmd import scanners as scanners_mod

    run = scanners_mod._scanner_run_one(
        tmp_path,
        {
            "id": "isolation-probe",
            "source": "isolation-probe",
            "command": f"{sys.executable} {script}",
            "cadence": "1h",
            "enabled": True,
            "timeout": 15,
            "output_path": "out.jsonl",
            "conflict_window": "1h",
        },
        isolated=True,
    )
    assert run.get("status") in {"completed", "failed"}
    _path, payload = ledger._read_external_directory_authority(tmp_path)
    assert payload is not None
    files = payload.get("files") or {}
    assert ".brigade/scanners/runs/stolen/receipt.json" not in files


def test_g5_same_uid_store_write_forged_identity_fails_with_external_key_hmac(tmp_path: Path) -> None:
    """HMAC on + key outside the scanner-reachable tree refuses the G5 rewrite."""

    _enable_external_key_isolation(tmp_path)
    key = authority_key.key_path()
    assert not authority_key.key_is_inside_tree(key, tmp_path)
    assert not authority_key.key_path_is_scanner_reachable(key)
    identity, _store = _g5_same_uid_store_rewrite(tmp_path)
    assert identity is None
    assert not authority_key.key_is_inside_tree(authority_key.key_path(), tmp_path)


def test_g5_forged_binding_is_accepted_when_hmac_verification_is_reverted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revert-check: without HMAC unwrap, the same same-uid write verifies."""

    def _passthrough(
        path: Path,
        payload: dict,
        *,
        env=None,
        key_material=None,
        workspace=None,
        allow_unsigned_upgrade=True,
    ) -> dict:
        if payload.get("envelope_version") == 1 and isinstance(payload.get("record"), dict):
            return dict(payload["record"])
        return payload

    _enable_external_key_isolation(tmp_path)
    monkeypatch.setattr(ledger, "_unwrap_authority_envelope", _passthrough)
    identity, _store = _g5_same_uid_store_rewrite(tmp_path)
    assert identity is not None
    assert identity[0] == "handoff-ingest"


def test_generate_key_refuses_workspace_brigade_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    inside = tmp_path / ".brigade" / "authority" / "store-hmac.key"
    monkeypatch.setenv("BRIGADE_AUTHORITY_KEY_FILE", str(inside))
    authority_key.clear_key_cache()
    with pytest.raises(OSError, match="scanner-reachable"):
        authority_key.generate_key(workspace=tmp_path)
    assert not inside.exists()


def test_workspace_root_key_override_is_refused_and_cannot_verify_forgery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reviewer probe: BRIGADE_AUTHORITY_KEY_FILE inside the workspace must fail closed."""

    _enable_external_key_isolation(tmp_path)
    planted = tmp_path / "operator-authority.key"
    monkeypatch.setenv("BRIGADE_AUTHORITY_KEY_FILE", str(planted))
    authority_key.clear_key_cache()
    with pytest.raises(OSError, match="workspace or scanner-reachable"):
        authority_key.generate_key(workspace=tmp_path)
    assert not planted.exists()
    with pytest.raises(OSError, match="workspace or scanner-reachable"):
        _bind_workspace(tmp_path)
    assert not planted.exists()
    item = ledger._make_import("in-workspace key forge", kind="task", source="handoff-ingest")
    assert ledger._legacy_import_source_content_identity(item, target=tmp_path) is None


def test_feature_on_rejects_forgery_that_feature_off_accepts(tmp_path: Path) -> None:
    """The security.toml flag controls HMAC verify, not just doctor output."""

    off = tmp_path / "off"
    on = tmp_path / "on"
    off.mkdir()
    on.mkdir()

    identity_off, store_off = _g5_same_uid_store_rewrite(off)
    assert identity_off is not None
    off_raw = json.loads(store_off.read_text(encoding="utf-8"))
    assert off_raw.get("envelope_version") != 1

    _enable_external_key_isolation(on)
    identity_on, store_on = _g5_same_uid_store_rewrite(on)
    assert identity_on is None
    # After the unsigned rewrite the on-store is also unsigned; bind itself was enveloped.
    # Recreate a clean on-bind to show the flag changes the written bytes.
    other = tmp_path / "on-bind"
    other.mkdir()
    _bind_workspace(other)
    bound = json.loads(_store_path(other).read_text(encoding="utf-8"))
    assert bound.get("envelope_version") == 1
    assert bound["signature"]["alg"] == "HMAC-SHA256"


def test_signed_envelope_mac_is_checked_after_flag_flipped_off(tmp_path: Path) -> None:
    """Existing HMAC envelopes stay fail-closed after security.toml is flipped off."""

    _bind_workspace(tmp_path)
    path = _store_path(tmp_path)
    envelope = json.loads(path.read_text(encoding="utf-8"))
    assert envelope["envelope_version"] == 1
    stale_mac = envelope["signature"]["mac"]
    record = dict(envelope["record"])
    files = dict(record.get("files") or {})
    files[".brigade/scanners/runs/stolen/receipt.json"] = {
        "device": 1,
        "inode": 2,
        "sha256": "d" * 64,
    }
    record["files"] = files
    envelope["record"] = record
    path.write_text(json.dumps(envelope, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    assert json.loads(path.read_text(encoding="utf-8"))["signature"]["mac"] == stale_mac
    _disable_external_key_isolation(tmp_path)
    with pytest.raises(OSError, match="MAC"):
        ledger._read_external_directory_authority(tmp_path)
    item = ledger._make_import("flag-flip forge", kind="task", source="handoff-ingest")
    assert ledger._legacy_import_source_content_identity(item, target=tmp_path) is None


def _symlink_or_skip(target: Path, link: Path) -> None:
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")


def test_directory_symlink_prefix_refuses_key_with_zero_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "ws"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    _enable_external_key_isolation(workspace)
    link = workspace / "escape-link"
    _symlink_or_skip(outside, link)
    candidate = link / "k.key"
    monkeypatch.setenv("BRIGADE_AUTHORITY_KEY_FILE", str(candidate))
    authority_key.clear_key_cache()
    with pytest.raises(OSError, match="workspace or scanner-reachable"):
        authority_key.generate_key(workspace=workspace)
    assert not (outside / "k.key").exists()
    assert not candidate.exists()
    with pytest.raises(OSError, match="workspace or scanner-reachable"):
        authority_key.load_key(workspace=workspace)


def test_file_symlink_inside_workspace_refuses_key_with_zero_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "ws"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    _enable_external_key_isolation(workspace)
    dest = outside / "real.key"
    link = workspace / "k.key"
    _symlink_or_skip(dest, link)
    monkeypatch.setenv("BRIGADE_AUTHORITY_KEY_FILE", str(link))
    authority_key.clear_key_cache()
    with pytest.raises(OSError, match="workspace or scanner-reachable"):
        authority_key.generate_key(workspace=workspace)
    assert not dest.exists()
    with pytest.raises(OSError, match="workspace or scanner-reachable"):
        authority_key.load_key(workspace=workspace)


def test_dotdot_reentry_refuses_key_with_zero_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "ws"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / "nested").mkdir()
    _enable_external_key_isolation(workspace)
    candidate = workspace / "nested" / ".." / ".." / "outside" / "reentry.key"
    monkeypatch.setenv("BRIGADE_AUTHORITY_KEY_FILE", str(candidate))
    authority_key.clear_key_cache()
    with pytest.raises(OSError, match="workspace or scanner-reachable"):
        authority_key.generate_key(workspace=workspace)
    assert not (outside / "reentry.key").exists()
    with pytest.raises(OSError, match="workspace or scanner-reachable"):
        authority_key.load_key(workspace=workspace)


def _strip_store_to_raw_record(tmp_path: Path) -> dict:
    path = _store_path(tmp_path)
    envelope = json.loads(path.read_text(encoding="utf-8"))
    assert envelope.get("envelope_version") == 1
    record = dict(envelope["record"])
    path.write_text(json.dumps(record, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    return record


def test_stripped_envelope_is_refused_after_flag_flipped_off(tmp_path: Path) -> None:
    """Round-3 probe: flag-off + envelope strip cannot downgrade a marked target."""

    _bind_workspace(tmp_path)
    assert authority_marker.marker_exists(tmp_path)
    marker = authority_marker.signed_marker_path(authority_marker.target_fingerprint(tmp_path))
    assert marker.is_file()
    assert not marker.resolve().is_relative_to(tmp_path.resolve())
    _strip_store_to_raw_record(tmp_path)
    _disable_external_key_isolation(tmp_path)
    with pytest.raises(OSError, match="raw unsigned record"):
        ledger._read_external_directory_authority(tmp_path)
    item = ledger._make_import("strip forge", kind="task", source="handoff-ingest")
    assert ledger._legacy_import_source_content_identity(item, target=tmp_path) is None


def test_stripped_envelope_is_refused_after_external_key_deleted(tmp_path: Path) -> None:
    """Fail closed: sticky marker still rejects a raw record after the HMAC key is gone."""

    _bind_workspace(tmp_path)
    _strip_store_to_raw_record(tmp_path)
    _disable_external_key_isolation(tmp_path)
    key = authority_key.key_path()
    key.unlink()
    authority_key.clear_key_cache()
    with pytest.raises(OSError, match="raw unsigned record"):
        ledger._read_external_directory_authority(tmp_path)


def test_authority_downgrade_with_confirm_accepts_raw_records_and_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("LOGNAME", "login-name")
    monkeypatch.setenv("USER", "test-operator")
    _bind_workspace(tmp_path)
    _strip_store_to_raw_record(tmp_path)
    _disable_external_key_isolation(tmp_path)
    with pytest.raises(OSError, match="raw unsigned record"):
        ledger._read_external_directory_authority(tmp_path)
    assert (
        cli.main(
            [
                "security",
                "authority",
                "downgrade",
                "--target",
                str(tmp_path),
                "--confirm",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "removed sticky marker" in out
    assert "test-operator" in out
    assert not authority_marker.marker_exists(tmp_path)
    _path, payload = ledger._read_external_directory_authority(tmp_path)
    assert payload is not None
    assert payload.get("envelope_version") != 1
    audit = authority_marker.audit_path().read_text(encoding="utf-8")
    line = json.loads(audit.strip().splitlines()[-1])
    assert line["action"] == "authority-downgrade"
    assert line["actor"] == "test-operator"
    assert line["target_fingerprint"] == authority_marker.target_fingerprint(tmp_path)
    assert line["target"] == str(tmp_path.resolve())
    assert line["removed"] is True


def test_authority_downgrade_persists_unsigned_store_without_recreating_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Confirmed downgrade must unwrap the store; a later read must not recreate the marker."""

    monkeypatch.setenv("USER", "test-operator")
    _bind_workspace(tmp_path)
    assert authority_marker.marker_exists(tmp_path)
    assert json.loads(_store_path(tmp_path).read_text(encoding="utf-8")).get("envelope_version") == 1
    assert (
        cli.main(
            [
                "security",
                "authority",
                "downgrade",
                "--target",
                str(tmp_path),
                "--confirm",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "removed sticky marker" in out
    assert "unsigned" in out
    raw = json.loads(_store_path(tmp_path).read_text(encoding="utf-8"))
    assert raw.get("envelope_version") != 1
    assert "schema_version" in raw
    assert not authority_marker.marker_exists(tmp_path)
    path, payload = ledger._read_external_directory_authority(tmp_path)
    assert payload is not None
    assert payload.get("envelope_version") != 1
    assert json.loads(path.read_text(encoding="utf-8")).get("envelope_version") != 1
    assert not authority_marker.marker_exists(tmp_path)
    identity, _store = _g5_same_uid_store_rewrite(tmp_path)
    assert identity is not None
    audit = authority_marker.audit_path().read_text(encoding="utf-8")
    line = json.loads(audit.strip().splitlines()[-1])
    assert line["action"] == "authority-downgrade"
    assert line["actor"] == "test-operator"
    assert line["removed"] is True
    assert line.get("store_unwrapped") is True
    _enable_external_key_isolation(tmp_path)
    _path, signed = ledger._read_external_directory_authority(tmp_path)
    assert signed is not None
    assert json.loads(_store_path(tmp_path).read_text(encoding="utf-8")).get("envelope_version") == 1
    assert authority_marker.marker_exists(tmp_path)


def test_authority_downgrade_fails_closed_when_external_key_removed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _bind_workspace(tmp_path)
    marker = authority_marker.signed_marker_path(authority_marker.target_fingerprint(tmp_path))
    before_marker = marker.read_bytes()
    before_store = _store_path(tmp_path).read_bytes()
    authority_key.key_path().unlink()
    authority_key.clear_key_cache()
    assert (
        cli.main(
            [
                "security",
                "authority",
                "downgrade",
                "--target",
                str(tmp_path),
                "--confirm",
            ]
        )
        == 2
    )
    err = capsys.readouterr().err
    assert "HMAC key is unavailable" in err
    assert "were not changed" in err
    assert marker.is_file()
    assert marker.read_bytes() == before_marker
    assert _store_path(tmp_path).read_bytes() == before_store
    assert json.loads(before_store).get("envelope_version") == 1
    assert authority_marker.marker_exists(tmp_path)
    assert not authority_marker.audit_path().exists()


def test_authority_downgrade_tty_no_cancels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class _Stdin:
        def isatty(self) -> bool:
            return True

        def readline(self) -> str:
            return "n\n"

    _bind_workspace(tmp_path)
    monkeypatch.setattr(sys, "stdin", _Stdin())
    assert (
        cli.main(
            [
                "security",
                "authority",
                "downgrade",
                "--target",
                str(tmp_path),
                "--confirm",
            ]
        )
        == 2
    )
    err = capsys.readouterr().err
    assert "cancelled" in err
    assert authority_marker.marker_exists(tmp_path)
    assert not authority_marker.audit_path().exists()


def test_authority_downgrade_without_confirm_refuses(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _bind_workspace(tmp_path)
    marker = authority_marker.signed_marker_path(authority_marker.target_fingerprint(tmp_path))
    before = marker.read_bytes()
    assert (
        cli.main(
            [
                "security",
                "authority",
                "downgrade",
                "--target",
                str(tmp_path),
            ]
        )
        == 2
    )
    err = capsys.readouterr().err
    assert "--confirm" in err
    assert marker.read_bytes() == before
    assert authority_marker.marker_exists(tmp_path)
    assert not authority_marker.audit_path().exists()


def test_fresh_unsigned_target_has_no_marker(tmp_path: Path) -> None:
    _bind_workspace(tmp_path, external_key=False)
    assert not authority_marker.marker_exists(tmp_path)
    path, payload = ledger._read_external_directory_authority(tmp_path)
    assert payload is not None
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw.get("envelope_version") != 1
    identity, _store = _g5_same_uid_store_rewrite(tmp_path)
    assert identity is not None


def test_workspace_relative_marker_plant_is_ignored(tmp_path: Path) -> None:
    """A plant or delete under the workspace cannot create or clear the sticky bit."""

    _bind_workspace(tmp_path, external_key=False)
    fingerprint = authority_marker.target_fingerprint(tmp_path)
    planted = tmp_path / "authority-signed" / fingerprint
    planted.parent.mkdir(parents=True)
    planted.write_text("planted", encoding="utf-8")
    brigade_plant = tmp_path / ".brigade" / "authority-signed" / fingerprint
    brigade_plant.parent.mkdir(parents=True, exist_ok=True)
    brigade_plant.write_text("planted", encoding="utf-8")
    assert not authority_marker.marker_exists(tmp_path)
    _path, payload = ledger._read_external_directory_authority(tmp_path)
    assert payload is not None

    _enable_external_key_isolation(tmp_path)
    _bind_workspace(tmp_path)
    real = authority_marker.signed_marker_path(fingerprint)
    assert real.is_file()
    assert not real.resolve().is_relative_to(tmp_path.resolve())
    planted.unlink()
    brigade_plant.unlink()
    _strip_store_to_raw_record(tmp_path)
    _disable_external_key_isolation(tmp_path)
    with pytest.raises(OSError, match="raw unsigned record"):
        ledger._read_external_directory_authority(tmp_path)


def test_record_signed_marker_refuses_workspace_user_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRIGADE_USER_DIR", str(tmp_path))
    with pytest.raises(OSError, match="inside the workspace"):
        authority_marker.record_signed_marker(tmp_path)
    planted = tmp_path / "authority-signed" / authority_marker.target_fingerprint(tmp_path)
    assert not planted.exists()
    assert not authority_marker.marker_exists(tmp_path)


def test_default_home_brigade_marker_is_created_and_blocks_strip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default ``$HOME/.brigade`` must actually receive a marker; strip then fails closed."""

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("BRIGADE_USER_DIR", raising=False)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _bind_workspace(workspace)
    fingerprint = authority_marker.target_fingerprint(workspace)
    marker = home / ".brigade" / "authority-signed" / fingerprint
    assert marker.is_file()
    assert marker.resolve().is_relative_to((home / ".brigade").resolve())
    assert ".brigade" in marker.parts
    assert not marker.resolve().is_relative_to(workspace.resolve())
    assert authority_marker.marker_exists(workspace)
    _strip_store_to_raw_record(workspace)
    _disable_external_key_isolation(workspace)
    with pytest.raises(OSError, match="raw unsigned record"):
        ledger._read_external_directory_authority(workspace)
    authority_key.key_path().unlink()
    authority_key.clear_key_cache()
    with pytest.raises(OSError, match="raw unsigned record"):
        ledger._read_external_directory_authority(workspace)


def test_marker_rejects_workspace_symlink_and_dotdot_escape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "ws"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / "nested").mkdir()
    link = workspace / "escape-link"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    monkeypatch.setenv("BRIGADE_USER_DIR", str(link / ".brigade"))
    with pytest.raises(OSError, match="inside the workspace"):
        authority_marker.record_signed_marker(workspace)
    assert not (outside / ".brigade" / "authority-signed" / authority_marker.target_fingerprint(workspace)).exists()

    monkeypatch.setenv(
        "BRIGADE_USER_DIR",
        str(workspace / "nested" / ".." / ".." / "outside" / ".brigade"),
    )
    with pytest.raises(OSError, match="inside the workspace"):
        authority_marker.record_signed_marker(workspace)
    assert not (outside / ".brigade" / "authority-signed" / authority_marker.target_fingerprint(workspace)).exists()


def test_reanchor_rejects_forged_raw_candidate_for_marked_destination(tmp_path: Path) -> None:
    """Round-5 probe: a raw dummy-target candidate cannot re-sign forged bindings."""

    dest = tmp_path / "dest"
    dest.mkdir()
    _bind_workspace(dest)
    item = ledger._make_import("reanchor forge", kind="task", source="handoff-ingest")
    receipt_path = _write_g5_receipt(dest, item)
    ledger._write_persisted_import_proofs(dest, [item], operation_id="0" * 32)
    assert ledger._legacy_import_source_content_identity(item, target=dest) is not None
    assert authority_marker.marker_exists(dest)

    store = _store_path(dest)
    envelope = json.loads(store.read_text(encoding="utf-8"))
    record = dict(envelope["record"])
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["attacker_marker"] = "reanchor-raw-candidate"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    info = receipt_path.stat()
    forged_binding = {
        "device": info.st_dev,
        "inode": info.st_ino,
        "sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
    }
    dummy = tmp_path / "dummy-unmarked"
    dummy.mkdir()
    dummy_target = str(dummy.resolve())
    dummy_digest = hashlib.sha256(dummy_target.encode("utf-8")).hexdigest()
    candidate = store.parent / f"{dummy_digest}.json"
    planted = {
        "schema_version": record["schema_version"],
        "target": dummy_target,
        "workspace": record["workspace"],
        "directories": dict(record.get("directories") or {}),
        "files": {
            **dict(record.get("files") or {}),
            ".brigade/scanners/runs/chosen-run/receipt.json": forged_binding,
        },
    }
    store.unlink()
    candidate.write_text(json.dumps(planted, sort_keys=True, separators=(",", ":")), encoding="utf-8")

    with pytest.raises(OSError):
        descriptor = ledger._open_verifier_owned_directory(
            dest,
            components=(".brigade", "scanners", "runs"),
            anchor_name=".runs.authority.json",
            create=True,
        )
        os.close(descriptor)

    assert ledger._legacy_import_source_content_identity(item, target=dest) is None
    if store.exists():
        raw = json.loads(store.read_text(encoding="utf-8"))
        inner = raw["record"] if raw.get("envelope_version") == 1 else raw
        files = (inner or {}).get("files") or {}
        bound = files.get(".brigade/scanners/runs/chosen-run/receipt.json") or {}
        assert bound.get("sha256") != forged_binding["sha256"]


def test_reanchor_accepts_enveloped_candidate_signed_with_real_key(tmp_path: Path) -> None:
    original = tmp_path / "original-workspace"
    original.mkdir()
    _bind_workspace(original)
    item = ledger._make_import("reanchor-legit", kind="task", source="handoff-ingest")
    _write_g5_receipt(original, item)
    ledger._write_persisted_import_proofs(original, [item], operation_id="0" * 32)
    assert ledger._legacy_import_source_content_identity(item, target=original) is not None
    assert json.loads(_store_path(original).read_text(encoding="utf-8")).get("envelope_version") == 1

    relocated = tmp_path / "relocated-workspace"
    original.rename(relocated)
    descriptor = ledger._open_import_proof_directory(relocated, create=True)
    os.close(descriptor)
    assert ledger._legacy_import_source_content_identity(item, target=relocated) is not None
    relocated_store = json.loads(_store_path(relocated).read_text(encoding="utf-8"))
    assert relocated_store.get("envelope_version") == 1


def test_downgrade_does_not_remove_marker_when_audit_unwritable(tmp_path: Path) -> None:
    """Downgrade must not drop the sticky marker when audit.jsonl cannot be written."""

    _bind_workspace(tmp_path)
    marker = authority_marker.signed_marker_path(authority_marker.target_fingerprint(tmp_path))
    before = marker.read_bytes()
    assert marker.is_file()
    audit = authority_marker.audit_path()
    audit.parent.mkdir(parents=True, exist_ok=True)
    if audit.exists() or audit.is_symlink():
        if audit.is_dir() and not audit.is_symlink():
            audit.rmdir()
        else:
            audit.unlink()
    audit.mkdir()
    with pytest.raises(OSError, match="audit"):
        authority_marker.remove_signed_marker(tmp_path, actor="test-operator")
    assert marker.is_file()
    assert marker.read_bytes() == before
    _strip_store_to_raw_record(tmp_path)
    _disable_external_key_isolation(tmp_path)
    with pytest.raises(OSError, match="raw unsigned record"):
        ledger._read_external_directory_authority(tmp_path)
    assert (
        cli.main(
            [
                "security",
                "authority",
                "downgrade",
                "--target",
                str(tmp_path),
                "--confirm",
            ]
        )
        == 2
    )
    assert marker.is_file()
    assert marker.read_bytes() == before
