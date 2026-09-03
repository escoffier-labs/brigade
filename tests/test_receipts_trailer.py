from __future__ import annotations

import json
import subprocess
from pathlib import Path

from brigade.cli.receipts import dispatch
from brigade.causal_receipt import receipt_digest


class DummyArgs:
    def __init__(self, receipts_command, run=None, commit=None, target=Path("."), json=False):
        self.receipts_command = receipts_command
        self.run = run
        self.commit = commit
        self.target = target
        self.json = json


def test_trailer_missing_run(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    args = DummyArgs("trailer", run="does-not-exist")
    assert dispatch(args) == 1
    out, err = capsys.readouterr()
    assert "unknown run" in out


def test_trailer_success(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run_id = "test-run-123"
    run_dir = tmp_path / ".brigade/runs" / run_id
    run_dir.mkdir(parents=True)
    receipt = {
        "schema": "brigade.causal_receipt.v1",
        "schema_version": "1.0",
        "subject": {"kind": "run", "id": run_id},
        "parents": [],
    }
    with open(run_dir / "run.json", "w") as f:
        json.dump(receipt, f)

    args = DummyArgs("trailer", run=run_id)
    assert dispatch(args) == 0
    out, err = capsys.readouterr()
    digest = receipt_digest(receipt)
    assert f"Brigade-Run: {run_id}" in out
    assert f"Brigade-Receipt: sha256:{digest}" in out


def test_verify_missing_commit(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    subprocess.check_call(["git", "init"])
    # Not creating a commit, or missing SHA
    args = DummyArgs("verify", commit="HEAD")
    assert dispatch(args) == 1
    out, err = capsys.readouterr()
    assert "missing trailer" in out


def test_verify_no_trailer(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    subprocess.check_call(["git", "init"])
    subprocess.check_call(["git", "config", "user.email", "test@example.com"])
    subprocess.check_call(["git", "config", "user.name", "Test User"])
    with open("test.txt", "w") as f:
        f.write("test")
    subprocess.check_call(["git", "add", "test.txt"])
    subprocess.check_call(["git", "commit", "-m", "No trailer here"])

    args = DummyArgs("verify", commit="HEAD")
    assert dispatch(args) == 1
    out, err = capsys.readouterr()
    assert "missing trailer" in out


def test_verify_success(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    subprocess.check_call(["git", "init"])
    subprocess.check_call(["git", "config", "user.email", "test@example.com"])
    subprocess.check_call(["git", "config", "user.name", "Test User"])

    run_id = "test-run-123"
    run_dir = tmp_path / ".brigade/runs" / run_id
    run_dir.mkdir(parents=True)
    receipt = {
        "schema": "brigade.causal_receipt.v1",
        "schema_version": "1.0",
        "subject": {"kind": "run", "id": run_id},
        "parents": [],
    }
    with open(run_dir / "run.json", "w") as f:
        json.dump(receipt, f)
    digest = receipt_digest(receipt)

    msg = f"Test commit\n\nBrigade-Run: {run_id}\nBrigade-Receipt: sha256:{digest}\n"
    with open("test.txt", "w") as f:
        f.write("test")
    subprocess.check_call(["git", "add", "test.txt"])
    subprocess.check_call(["git", "commit", "-m", msg])

    args = DummyArgs("verify", commit="HEAD")
    assert dispatch(args) == 0
    out, err = capsys.readouterr()
    assert "ok" in out


def test_verify_tampered_receipt(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    subprocess.check_call(["git", "init"])
    subprocess.check_call(["git", "config", "user.email", "test@example.com"])
    subprocess.check_call(["git", "config", "user.name", "Test User"])

    run_id = "test-run-123"
    run_dir = tmp_path / ".brigade/runs" / run_id
    run_dir.mkdir(parents=True)
    receipt = {
        "schema": "brigade.causal_receipt.v1",
        "schema_version": "1.0",
        "subject": {"kind": "run", "id": run_id},
        "parents": [],
    }
    with open(run_dir / "run.json", "w") as f:
        json.dump(receipt, f)
    digest = receipt_digest(receipt)

    msg = f"Test commit\n\nBrigade-Run: {run_id}\nBrigade-Receipt: sha256:{digest}\n"
    with open("test.txt", "w") as f:
        f.write("test")
    subprocess.check_call(["git", "add", "test.txt"])
    subprocess.check_call(["git", "commit", "-m", msg])

    # tamper receipt
    receipt["parents"] = [{"kind": "foo", "id": "bar"}]
    with open(run_dir / "run.json", "w") as f:
        json.dump(receipt, f)

    args = DummyArgs("verify", commit="HEAD")
    assert dispatch(args) == 1
    out, err = capsys.readouterr()
    assert "digest mismatch" in out
