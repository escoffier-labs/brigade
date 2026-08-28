"""#1233: package-split review findings that moved verbatim from ledger.py."""

from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path

import pytest

from brigade import work_cmd
from brigade.security_cmd import AUTHORITY_STORE_ISOLATION_EXTERNAL_KEY
from brigade.work_cmd import inbox_lock, ledger
from brigade.work_cmd.ledger import descriptor_anchors
from tests.work_cmd_test_helpers import _init_git_repo, _plan_task_id


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


def _bind_workspace(tmp_path: Path) -> dict[str, int]:
    _enable_external_key_isolation(tmp_path)
    (tmp_path / ".brigade").mkdir(exist_ok=True)
    workspace = ledger._workspace_directory_identity(tmp_path)
    root = os.open(tmp_path / ".brigade", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        ledger._record_external_directory_authority(tmp_path, (".brigade",), root, workspace=workspace)
    finally:
        os.close(root)
    return workspace


def test_rebind_directory_authority_reports_data_root_oserror(tmp_path: Path, monkeypatch, capsys):
    def boom(*, env=None, system=None):
        raise ValueError("component data root requires HOME")

    monkeypatch.setattr(ledger.component_paths, "data_root", boom)

    assert ledger.rebind_directory_authority(target=tmp_path) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ")
    assert "external directory authority storage is unavailable" in err
    assert "Traceback" not in err


def test_record_verifier_owned_file_holds_inbox_writer_lock(tmp_path: Path, monkeypatch):
    _bind_workspace(tmp_path)
    receipt = tmp_path / ".brigade" / "scanners" / "runs" / "run-a" / "receipt.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_bytes(b'{"run_id":"run-a"}')
    original = ledger._write_external_directory_authority
    held: list[bool] = []

    def wrapped(*args, **kwargs):
        inbox_lock.verify_inbox_writer_lock(tmp_path)
        held.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(ledger, "_write_external_directory_authority", wrapped)
    data = receipt.read_bytes()
    descriptor = os.open(receipt, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        ledger._record_verifier_owned_file(
            tmp_path,
            components=(".brigade", "scanners", "runs", "run-a", "receipt.json"),
            descriptor=descriptor,
            data=data,
        )
    finally:
        os.close(descriptor)
    assert held == [True]


def test_record_verifier_owned_file_concurrent_updates_do_not_lose_writes(tmp_path: Path, monkeypatch):
    """Two same-process threads must both land; the writer lock alone is reentrant."""
    _bind_workspace(tmp_path)
    receipts: list[tuple[tuple[str, ...], Path]] = []
    for run_id in ("run-a", "run-b"):
        receipt = tmp_path / ".brigade" / "scanners" / "runs" / run_id / "receipt.json"
        receipt.parent.mkdir(parents=True)
        receipt.write_bytes(f'{{"run_id":"{run_id}"}}'.encode())
        receipts.append(((".brigade", "scanners", "runs", run_id, "receipt.json"), receipt))

    both_read = threading.Event()
    readers = 0
    readers_guard = threading.Lock()
    original_read = ledger._read_external_directory_authority

    def gated_read(*args, **kwargs):
        nonlocal readers
        result = original_read(*args, **kwargs)
        with readers_guard:
            readers += 1
            if readers >= 2:
                both_read.set()
        both_read.wait(timeout=0.3)
        return result

    monkeypatch.setattr(ledger, "_read_external_directory_authority", gated_read)
    errors: list[str] = []

    def record(components: tuple[str, ...], receipt: Path) -> None:
        data = receipt.read_bytes()
        descriptor = os.open(receipt, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            ledger._record_verifier_owned_file(tmp_path, components=components, descriptor=descriptor, data=data)
        except BaseException as exc:
            errors.append(repr(exc))
        finally:
            os.close(descriptor)

    threads = [threading.Thread(target=record, args=item) for item in receipts]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    assert errors == []
    _path, payload = original_read(tmp_path)
    assert payload is not None
    files = payload.get("files")
    assert isinstance(files, dict)
    scopes = {ledger._directory_authority_scope(components) for components, _receipt in receipts}
    assert scopes <= set(files)


def test_record_verifier_owned_file_refuses_replaced_writer_lock(tmp_path: Path, monkeypatch):
    _bind_workspace(tmp_path)
    receipt = tmp_path / ".brigade" / "scanners" / "runs" / "run-a" / "receipt.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_bytes(b'{"run_id":"run-a"}')
    original_write = ledger._write_external_directory_authority
    writes: list[bool] = []

    def boom(target: Path) -> None:
        raise OSError("import inbox lock was replaced while held")

    def wrapped(*args, **kwargs):
        writes.append(True)
        return original_write(*args, **kwargs)

    monkeypatch.setattr(inbox_lock, "verify_inbox_writer_lock", boom)
    monkeypatch.setattr(ledger, "_write_external_directory_authority", wrapped)
    data = receipt.read_bytes()
    descriptor = os.open(receipt, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        with pytest.raises(OSError, match="replaced while held"):
            ledger._record_verifier_owned_file(
                tmp_path,
                components=(".brigade", "scanners", "runs", "run-a", "receipt.json"),
                descriptor=descriptor,
                data=data,
            )
    finally:
        os.close(descriptor)
    assert writes == []


def test_open_verifier_owned_directory_closes_child_on_exception(tmp_path: Path, monkeypatch):
    descriptor = ledger._open_verifier_owned_directory(
        tmp_path,
        components=(".brigade", "work", "imports", "proofs"),
        anchor_name=".proofs.authority.json",
        create=True,
    )
    os.close(descriptor)

    opened: list[tuple[str, int]] = []
    closed: list[int] = []
    real_open = ledger._dirfd_open_dir
    real_close = os.close

    def tracking_open(parent: int, name: str) -> int:
        child = real_open(parent, name)
        opened.append((name, child))
        return child

    def tracking_close(fd: int) -> None:
        closed.append(fd)
        return real_close(fd)

    monkeypatch.setattr(ledger, "_dirfd_open_dir", tracking_open)
    monkeypatch.setattr(os, "close", tracking_close)
    monkeypatch.setattr(
        ledger,
        "_write_compatibility_directory_anchor",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("injected")),
    )

    with pytest.raises(RuntimeError, match="injected"):
        ledger._open_verifier_owned_directory(
            tmp_path,
            components=(".brigade", "work", "imports", "proofs"),
            anchor_name=".proofs.authority.json",
            create=True,
        )

    child_fds = [fd for name, fd in opened if name == "proofs"]
    assert child_fds
    assert child_fds[-1] in closed


def test_open_verifier_owned_directory_parent_close_handoff_is_interrupt_safe(tmp_path: Path, monkeypatch):
    """Closing parent must drop ownership first so a later handler cannot close a reused fd."""
    descriptor = ledger._open_verifier_owned_directory(
        tmp_path,
        components=(".brigade", "work", "imports", "proofs"),
        anchor_name=".proofs.authority.json",
        create=True,
    )
    os.close(descriptor)

    opened: dict[str, int] = {}
    recycled: list[int] = []
    real_open = ledger._dirfd_open_dir
    real_close = os.close

    def tracking_open(parent: int, name: str) -> int:
        child = real_open(parent, name)
        opened[name] = child
        return child

    def close_parent_then_recycle(fd: int) -> None:
        real_close(fd)
        if recycled or fd != opened.get("imports"):
            return
        dummy = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
        recycled.append(dummy)
        raise RuntimeError("interrupt after parent close")

    monkeypatch.setattr(ledger, "_dirfd_open_dir", tracking_open)
    monkeypatch.setattr(os, "close", close_parent_then_recycle)

    with pytest.raises(RuntimeError, match="interrupt after parent close"):
        ledger._open_verifier_owned_directory(
            tmp_path,
            components=(".brigade", "work", "imports", "proofs"),
            anchor_name=".proofs.authority.json",
            create=True,
        )

    assert recycled
    dummy = recycled[0]
    # The number may have been closed earlier in the walk; the dummy
    # opened after parent close must still be live.
    os.fstat(dummy)
    real_close(dummy)


def test_read_verified_authority_snapshot_reads_past_one_chunk(tmp_path: Path, monkeypatch):
    _bind_workspace(tmp_path)
    store = ledger._directory_authority_store_path(tmp_path)
    raw = store.read_bytes()
    assert len(raw) > 16
    monkeypatch.setattr(descriptor_anchors, "_AUTHORITY_SNAPSHOT_READ_CHUNK_BYTES", 16)

    record = ledger._read_verified_authority_snapshot(tmp_path)
    assert record is not None
    assert isinstance(record.get("directories"), dict)
    assert record.get("target") == str(tmp_path.expanduser().resolve())


def test_plan_write_refuses_corrupt_receipt(tmp_path: Path, capsys):
    _init_git_repo(tmp_path)
    task_id = _plan_task_id(tmp_path, capsys)
    assert work_cmd.task_plan(target=tmp_path, task_id=task_id[:12], write=True) == 0
    capsys.readouterr()
    json_path, _ = work_cmd._plan_paths(tmp_path, task_id)
    json_path.write_text("{not-json")

    assert work_cmd.task_plan(target=tmp_path, task_id=task_id[:12], write=True, title="Overwrite") != 0
    err = capsys.readouterr().err
    assert "corrupt" in err
    assert json_path.read_text() == "{not-json"


def test_read_github_issue_timeout_is_read_failure(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(ledger.shutil, "which", lambda name: "/usr/bin/gh" if name == "gh" else None)

    def boom(*args, **kwargs):
        assert kwargs.get("timeout") is not None
        raise subprocess.TimeoutExpired(cmd=["gh", "issue", "view"], timeout=kwargs["timeout"])

    monkeypatch.setattr(ledger.subprocess, "run", boom)
    payload, labels, detail = ledger._read_github_issue(tmp_path, "9")
    assert payload is None
    assert labels == []
    assert detail is not None
    assert "timed out" in detail.lower() or "timeout" in detail.lower()
