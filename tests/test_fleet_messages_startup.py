"""Messaging startup works with partial configuration and relocated hub storage."""

import json
import os
import stat
from datetime import datetime, timedelta, timezone

import pytest

from brigade import fleet_client, fleet_client_messages, fleet_hub


@pytest.mark.parametrize(
    ("hub", "token"),
    [("", ""), ("https://hub.invalid", ""), ("", "fixture-node-token")],
    ids=["unconfigured", "missing-token", "missing-hub"],
)
def test_flush_without_configuration_leaves_spool_untouched(tmp_path, monkeypatch, hub, token):
    home = tmp_path / "state"
    monkeypatch.setenv("BRIGADE_HOME", str(home))
    monkeypatch.setattr(fleet_client, "load_fleet_config", lambda: {"hub_url": hub})
    monkeypatch.setattr(fleet_client, "load_fleet_settings", lambda: {"node_token": token})

    spool = fleet_client_messages._spool_path("fixture-node", token)
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
