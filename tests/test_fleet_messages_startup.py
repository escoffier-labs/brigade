"""Messaging startup works with partial configuration and relocated hub storage."""

import errno
import hashlib
import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from brigade import cli, fleet_client, fleet_client_messages, fleet_hub


@pytest.mark.parametrize(
    ("hub", "token"),
    [("", ""), ("https://hub.invalid", ""), ("", "fixture-node-token")],
    ids=["unconfigured", "missing-token", "missing-hub"],
)
def test_flush_without_configuration_leaves_spool_untouched(tmp_path, monkeypatch, hub, token):
    home = tmp_path / "state"
    monkeypatch.setenv("BRIGADE_HOME", str(home))
    monkeypatch.setattr(fleet_client, "load_fleet_config", lambda: {"hub_url": hub})
    monkeypatch.setattr(fleet_client, "load_fleet_settings", lambda **_kwargs: {"hub_url": hub, "node_token": token})

    spool = fleet_client_messages._spool_path("fixture-node", token, hub_url=hub)
    spool.parent.mkdir(parents=True, mode=0o700)
    # No current writer should queue with an empty credential. Even a leftover
    # record must remain untouched until flushing is configured.
    record = {
        "action": "send",
        "to": "fixture-recipient",
        "subject": "Expired",
        "body": "Preserve until configured",
        "client_id": "fixture-expired",
        "enqueued_at": (datetime.now(timezone.utc) - timedelta(days=91)).isoformat(),
    }
    spool.write_text(json.dumps(record) + "\n", encoding="utf-8")
    before = spool.read_bytes()

    assert fleet_client_messages.flush("fixture-node") == 0
    assert spool.read_bytes() == before


@pytest.fixture
def configured_message_spool(tmp_path, monkeypatch):
    hub, token = "https://hub.invalid", "fixture-node-token"
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", hub)
    monkeypatch.setenv("BRIGADE_FLEET_NODE_TOKEN", token)
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: "fixture-node")
    return fleet_client_messages._spool_path("fixture-node", token, hub_url=hub)


@pytest.mark.parametrize("error_number", [errno.EACCES, errno.EIO])
def test_flush_command_surfaces_message_spool_parent_errors(
    configured_message_spool, monkeypatch, capsys, error_number
):
    spool = configured_message_spool
    spool.parent.mkdir(parents=True)
    spool.touch()
    is_file, open_file = Path.is_file, os.open

    def fail_spool_open(path, *args, **kwargs):
        if path == str(spool):
            raise OSError(error_number, "message spool parent unavailable")
        return open_file(path, *args, **kwargs)

    monkeypatch.setattr(Path, "is_file", lambda path: False if path == spool else is_file(path))
    monkeypatch.setattr(os, "open", fail_spool_open)

    assert cli.main(["fleet", "flush"]) == 1
    captured = capsys.readouterr()
    assert "flushed 0 spooled event(s)" in captured.out
    assert "warning: could not flush fleet messages:" in captured.err
    assert "message spool parent unavailable" in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.parametrize("parent_kind", ["missing", "directory", "file"])
def test_flush_missing_message_spool_returns_zero(configured_message_spool, parent_kind):
    spool = configured_message_spool
    if parent_kind == "directory":
        spool.parent.mkdir(parents=True)
    elif parent_kind == "file":
        spool.parent.parent.mkdir(parents=True)
        spool.parent.touch()

    assert fleet_client_messages.flush("fixture-node") == 0
    if parent_kind == "directory":
        assert list(spool.parent.iterdir()) == []
    elif parent_kind == "missing":
        assert not spool.parent.exists()


@pytest.mark.parametrize("unreadable", [False, True])
def test_principal_retry_distinguishes_unreadable_and_missing_candidate(tmp_path, monkeypatch, unreadable):
    name, token = "fixture-principal", "fixture-admin-token"
    name_scope = hashlib.sha256(name.encode()).hexdigest()[:16]
    token_scope = hashlib.sha256(token.encode()).hexdigest()
    candidate = tmp_path / f"principal-{name_scope}-{token_scope}.jsonl"
    candidate.touch()
    active = tmp_path / "current.jsonl"
    original_lstat = Path.lstat
    is_file = Path.is_file
    is_symlink = Path.is_symlink

    def fail_lstat(path, *args, **kwargs):
        if path == candidate:
            if unreadable:
                raise PermissionError("legacy retry parent unavailable")
            raise FileNotFoundError
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "is_file", lambda path: False if path == candidate else is_file(path))
    monkeypatch.setattr(Path, "is_symlink", lambda path: False if path == candidate else is_symlink(path))
    monkeypatch.setattr(Path, "lstat", fail_lstat)
    if unreadable:
        with pytest.raises(PermissionError, match="legacy retry parent unavailable"):
            fleet_client_messages._require_no_legacy_principal_requests(name, token, active)
    else:
        fleet_client_messages._require_no_legacy_principal_requests(name, token, active)


def test_principal_retry_distinguishes_unreadable_and_missing_spool_directory(tmp_path, monkeypatch):
    parent = tmp_path / "spool"
    active = parent / "current.jsonl"
    fleet_client_messages._require_no_legacy_principal_requests("fixture-principal", "fixture-admin-token", active)
    parent.mkdir()
    iterdir, glob = Path.iterdir, Path.glob

    def fail_iterdir(path):
        if path == parent:
            raise PermissionError("legacy retry directory unavailable")
        return iterdir(path)

    monkeypatch.setattr(Path, "iterdir", fail_iterdir)
    monkeypatch.setattr(Path, "glob", lambda path, *args: iter(()) if path == parent else glob(path, *args))
    with pytest.raises(PermissionError, match="legacy retry directory unavailable"):
        fleet_client_messages._require_no_legacy_principal_requests("fixture-principal", "fixture-admin-token", active)


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlinks and directory modes")
@pytest.mark.parametrize("existing_database", [False, True])
def test_hub_initializes_through_symlinked_database_parent(tmp_path, existing_database):
    real_parent = tmp_path / "real-state"
    real_parent.mkdir(mode=0o755)
    real_parent.chmod(0o755)
    linked_parent = tmp_path / "linked-state"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    db = linked_parent / "fleet.db"
    if existing_database:
        db.write_bytes(b"")
        db.chmod(0o644)

    conn = fleet_hub.init_db(db)
    try:
        assert conn.execute("PRAGMA database_list").fetchone()[2] == str((real_parent / "fleet.db").resolve())
        assert conn.execute("SELECT COUNT(*) FROM fleet_messages").fetchone()[0] == 0
        assert linked_parent.is_symlink()
        assert stat.S_IMODE(real_parent.stat().st_mode) == 0o755
        assert stat.S_IMODE(db.stat().st_mode) == 0o600
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = real_parent / f"fleet.db{suffix}"
            if sidecar.exists():
                assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600
    finally:
        conn.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink handling")
@pytest.mark.parametrize("suffix", ["", "-wal", "-shm", "-journal"])
def test_hub_rejects_symlinked_files_under_symlinked_parent(tmp_path, suffix):
    real_parent = tmp_path / "real-state"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-state"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    target = tmp_path / "untouched-file"
    target.write_bytes(b"preserve this file")
    linked_file = real_parent / f"fleet.db{suffix}"
    linked_file.symlink_to(target)

    with pytest.raises(fleet_hub.FleetHubError, match="database files must be regular files"):
        fleet_hub.init_db(linked_parent / "fleet.db")

    assert linked_file.is_symlink()
    assert target.read_bytes() == b"preserve this file"
