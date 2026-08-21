"""Mutation tests for the HMAC authority store and residual-honesty boundary."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

from brigade import authority_broker, authority_key, scanner_isolation
from brigade.security_cmd import AUTHORITY_STORE_ISOLATION_EXTERNAL_KEY
from brigade.work_cmd import constants, helpers, ledger


def _bind_workspace(tmp_path: Path) -> dict[str, int]:
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
    ) -> dict:
        if payload.get("envelope_version") == 1 and isinstance(payload.get("record"), dict):
            return dict(payload["record"])
        return payload

    monkeypatch.setattr(ledger, "_unwrap_authority_envelope", _passthrough)
    identity, _store = _g5_same_uid_store_rewrite(tmp_path)
    assert identity is not None
    assert identity[0] == "handoff-ingest"


def test_generate_key_refuses_workspace_brigade_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    inside = tmp_path / ".brigade" / "authority" / "store-hmac.key"
    monkeypatch.setenv("BRIGADE_AUTHORITY_KEY_FILE", str(inside))
    authority_key.clear_key_cache()
    with pytest.raises(OSError, match="scanner-reachable"):
        authority_key.generate_key()
    assert not inside.exists()
