import json
import stat
import sys
from pathlib import Path

from brigade import cli, localio, receipt_signing, runbook_cmd, work_cmd


def _write_key(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value)
    path.chmod(0o600)


def test_receipts_keygen_writes_private_key_and_refuses_without_force(tmp_path, capsys):
    rc = cli.main(["receipts", "keygen", "--target", str(tmp_path)])
    captured = capsys.readouterr()
    key_path = tmp_path / ".brigade" / "receipt-signing-key"

    assert rc == 0
    assert key_path.is_file()
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
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
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600


def test_work_verify_receipt_without_key_keeps_unsigned_digest_shape(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("BRIGADE_RECEIPT_SIGNING_KEY_FILE", str(tmp_path / "missing-key"))
    monkeypatch.setenv("GRAPHTRAIL_BIN", str(tmp_path / "missing-graphtrail"))
    monkeypatch.setenv("HOME", str(tmp_path))

    assert (
        work_cmd.verify_run(target=tmp_path, commands=[f"{sys.executable} -c \"print('ok')\""], json_output=True) == 0
    )
    receipt = json.loads(capsys.readouterr().out)

    assert set(receipt["digests"]) == {"algorithm", "logs", "receipt_sha256"}
    assert receipt["digests"]["receipt_sha256"] == localio.canonical_json_digest(receipt, exclude_keys={"digests"})


def test_work_verify_receipt_signs_receipt_sha256_when_key_exists(tmp_path, capsys, monkeypatch):
    key_path = tmp_path / ".brigade" / "receipt-signing-key"
    _write_key(key_path, "01" * 32)
    monkeypatch.setenv("GRAPHTRAIL_BIN", str(tmp_path / "missing-graphtrail"))
    monkeypatch.setenv("HOME", str(tmp_path))

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
