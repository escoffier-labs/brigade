import json
import sys

import pytest
from pathlib import Path

from brigade import cli, localio, receipt_signing, runbook_cmd, work_cmd
from tests.support import PRIVATE_FILE_MODE, assert_private_mode
from tests._home import set_home


def _write_key(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value)
    path.chmod(PRIVATE_FILE_MODE)


def test_receipts_keygen_writes_private_key_and_refuses_without_force(tmp_path, capsys):
    rc = cli.main(["receipts", "keygen", "--target", str(tmp_path)])
    captured = capsys.readouterr()
    key_path = tmp_path / ".brigade" / "receipt-signing-key"

    assert rc == 0
    assert key_path.is_file()
    assert_private_mode(key_path, PRIVATE_FILE_MODE)
    key_text = key_path.read_text().strip()
    assert len(key_text) == 64
    int(key_text, 16)
    assert "key_id:" in captured.out
    assert "gitignored" in captured.out

    assert cli.main(["receipts", "keygen", "--target", str(tmp_path)]) == 1
    refused = capsys.readouterr()
    assert "already exists" in refused.err
    assert key_path.read_text().strip() == key_text

    assert cli.main(["receipts", "keygen", "--target", str(tmp_path), "--force"]) == 0
    capsys.readouterr()
    assert key_path.read_text().strip() != key_text
    assert_private_mode(key_path, PRIVATE_FILE_MODE)


def test_work_verify_receipt_without_key_keeps_unsigned_digest_shape(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("BRIGADE_RECEIPT_SIGNING_KEY_FILE", str(tmp_path / "missing-key"))
    monkeypatch.setenv("BRIGADE_CODE_GRAPH_BIN", str(tmp_path / "missing-graphtrail"))
    set_home(monkeypatch, str(tmp_path))

    assert (
        work_cmd.verify_run(target=tmp_path, commands=[f"{sys.executable} -c \"print('ok')\""], json_output=True) == 0
    )
    receipt = json.loads(capsys.readouterr().out)

    assert set(receipt["digests"]) == {"algorithm", "logs", "receipt_sha256"}
    assert receipt["digests"]["receipt_sha256"] == localio.canonical_json_digest(receipt, exclude_keys={"digests"})


def test_work_verify_receipt_signs_receipt_sha256_when_key_exists(tmp_path, capsys, monkeypatch):
    key_path = tmp_path / ".brigade" / "receipt-signing-key"
    _write_key(key_path, "01" * 32)
    monkeypatch.setenv("BRIGADE_CODE_GRAPH_BIN", str(tmp_path / "missing-graphtrail"))
    set_home(monkeypatch, str(tmp_path))

    assert (
        work_cmd.verify_run(target=tmp_path, commands=[f"{sys.executable} -c \"print('ok')\""], json_output=True) == 0
    )
    receipt = json.loads(capsys.readouterr().out)
    key = receipt_signing.load_key(tmp_path)

    assert key is not None
    key_bytes, key_id = key
    assert receipt["digests"]["key_id"] == key_id
    assert receipt["digests"]["signature"] == receipt_signing.sign(receipt["digests"]["receipt_sha256"], key_bytes)
    assert receipt["digests"]["receipt_sha256"] == localio.canonical_json_digest(receipt, exclude_keys={"digests"})


def test_runbook_receipt_signs_receipt_sha256_when_key_exists(tmp_path, capsys):
    _write_key(tmp_path / ".brigade" / "receipt-signing-key", "02" * 32)
    runbook = tmp_path / "runbook.json"
    runbook.write_text(
        json.dumps(
            {
                "id": "signed",
                "allowed_commands": ["printf"],
                "steps": [{"id": "hello", "run": "printf hello"}],
            }
        )
    )

    assert runbook_cmd.run(target=tmp_path, runbook=runbook, approved=True, json_output=True) == 0
    receipt = json.loads(capsys.readouterr().out)
    key = receipt_signing.load_key(tmp_path)

    assert key is not None
    key_bytes, key_id = key
    assert receipt["digests"]["key_id"] == key_id
    assert receipt["digests"]["signature"] == receipt_signing.sign(receipt["digests"]["receipt_sha256"], key_bytes)
    assert receipt["digests"]["receipt_sha256"] == localio.canonical_json_digest(receipt, exclude_keys={"digests"})


def test_verify_receipt_records_producing_work_session(tmp_path, capsys, monkeypatch):
    set_home(monkeypatch, str(tmp_path))
    assert work_cmd.start(target=tmp_path, title="receipt session") == 0
    session = (tmp_path / ".brigade/work/current").read_text().strip()
    capsys.readouterr()
    for reuse in (False, True):
        assert (
            work_cmd.verify_run(
                target=tmp_path, commands=[f'{sys.executable} -c "print(1)"'], json_output=True, reuse=reuse
            )
            == 0
        )
        receipt = json.loads(capsys.readouterr().out)
        assert receipt["producer_work_session_id"] == session
        assert receipt["digests"]["receipt_sha256"] == localio.canonical_json_digest(receipt, exclude_keys={"digests"})


def test_confined_key_ignores_environment_override(tmp_path, monkeypatch):
    trusted = tmp_path / ".brigade/receipt-signing-key"
    other = tmp_path / "other-key"
    _write_key(trusted, "01" * 32)
    _write_key(other, "02" * 32)
    monkeypatch.setenv(receipt_signing.KEY_ENV, str(other))
    if sys.platform == "win32":
        assert receipt_signing.load_workspace_key(tmp_path) is None
    else:
        key = bytes.fromhex("01" * 32)
        assert receipt_signing.load_workspace_key(tmp_path) == (key, receipt_signing.key_id(key))
    trusted.unlink()
    assert receipt_signing.load_workspace_key(tmp_path) is None


def test_confined_key_rejects_links_and_public_permissions(tmp_path):
    if sys.platform == "win32":
        return  # Windows cannot establish this authority without ACL confinement.
    path = tmp_path / ".brigade/receipt-signing-key"
    outside = tmp_path / "key"
    _write_key(outside, "01" * 32)
    path.parent.mkdir()
    path.symlink_to(outside)
    assert receipt_signing.load_workspace_key(tmp_path) is None
    path.unlink()
    path.hardlink_to(outside)
    assert receipt_signing.load_workspace_key(tmp_path) is None
    path.unlink()
    _write_key(path, "01" * 32)
    path.chmod(0o644)
    assert receipt_signing.load_workspace_key(tmp_path) is None


def test_confined_key_rejects_linked_parent(tmp_path):
    if sys.platform == "win32":
        return
    outside = tmp_path / "other"
    outside.mkdir()
    _write_key(outside / "receipt-signing-key", "01" * 32)
    (tmp_path / ".brigade").symlink_to(outside, target_is_directory=True)
    assert receipt_signing.load_workspace_key(tmp_path) is None


@pytest.mark.parametrize("unsafe", ["missing", "public", "malformed", "wrong-owner"])
def test_unconfined_workspace_key_never_authenticates_a_receipt(tmp_path, monkeypatch, unsafe):
    from types import SimpleNamespace

    trusted = tmp_path / ".brigade/receipt-signing-key"
    _write_key(trusted, "01" * 32)
    receipt = {"run_id": "example", "producer_work_session_id": "example-session"}
    receipt["digests"] = {"receipt_sha256": localio.canonical_json_digest(receipt)}
    receipt_signing.stamp_workspace_signature(tmp_path, receipt)
    if unsafe == "missing":
        trusted.unlink()
    elif unsafe == "public":
        trusted.chmod(0o644)
    elif unsafe == "malformed":
        trusted.write_text("invalid key")
    else:
        original = receipt_signing.os.fstat

        def other_owner(fd):
            info = original(fd)
            return SimpleNamespace(
                **{name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
                | {"st_uid": info.st_uid + 1}
            )

        monkeypatch.setattr(receipt_signing.os, "fstat", other_owner)
    assert not receipt_signing.authentic_workspace_receipt(tmp_path, receipt)


def test_workspace_authority_is_separate_from_audit_override(tmp_path, capsys, monkeypatch):
    fixed = tmp_path / ".brigade/receipt-signing-key"
    audit = tmp_path / "audit-key"
    _write_key(fixed, "01" * 32)
    _write_key(audit, "02" * 32)
    monkeypatch.setenv(receipt_signing.KEY_ENV, str(audit))
    assert work_cmd.verify_run(target=tmp_path, commands=[f'{sys.executable} -c "print(1)"'], json_output=True) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["digests"]["key_id"] == receipt_signing.key_id(bytes.fromhex("02" * 32))
    assert receipt_signing.authentic_workspace_receipt(tmp_path, receipt) is (sys.platform != "win32")
    assert not receipt_signing.authentic_workspace_receipt(tmp_path, {**receipt, "producer_work_session_id": "forged"})


def test_audit_signature_cannot_be_relabelled_as_workspace_authority(tmp_path):
    _write_key(tmp_path / ".brigade/receipt-signing-key", "01" * 32)
    receipt = {"run_id": "example", "producer_work_session_id": "example-session"}
    digest = localio.canonical_json_digest(receipt)
    key = bytes.fromhex("01" * 32)
    receipt["digests"] = {
        "receipt_sha256": digest,
        "workspace_signature": receipt_signing.sign(digest, key),
        "workspace_key_id": receipt_signing.key_id(key),
    }
    assert not receipt_signing.authentic_workspace_receipt(tmp_path, receipt)
