"""Mutation tests for the HMAC authority store and residual-honesty boundary."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

from brigade import authority_broker, authority_key, scanner_isolation
from brigade.work_cmd import ledger


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
