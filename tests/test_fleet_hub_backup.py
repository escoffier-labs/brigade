"""`fleet backup`, its retention and free-space floor, the restore tool and the claim recovery floor (S31)."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from brigade import (
    fleet_hub,
    fleet_hub_backup as backup,
    fleet_hub_claims,
    fleet_hub_messages,
    fleet_hub_model_roster,
    fleet_hub_policy,
)
from brigade import cli

NODE_A, NODE_B = "node-a", "node-b"
T0 = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
ADV = backup.ADVANCE


def _claim(conn, action, target, holder="h1", node=NODE_A, **extra):
    return fleet_hub.handle_claim(
        conn, {"action": action, "node_id": node, "target": target, "holder": holder, "ttl_seconds": 600, **extra}
    )


def _hub(path: Path) -> sqlite3.Connection:
    conn = fleet_hub.init_db(path)
    fleet_hub.add_node(conn, NODE_A)
    fleet_hub.add_node(conn, NODE_B)
    return conn


def _send(conn, body="hello"):
    return fleet_hub_messages.send(conn, {"to": NODE_B, "subject": "s", "body": body}, sender=NODE_A)["id"]


def _bump_roster(conn):
    conn.execute("UPDATE model_roster_meta SET revision = revision + 1 WHERE singleton = 1")
    conn.commit()
    return fleet_hub_model_roster.raw_revision(conn)


def _plenty(_path):
    return SimpleNamespace(free=10**15)


def _populated(tmp_path: Path) -> Path:
    db = tmp_path / "live" / "fleet-hub.db"
    db.parent.mkdir()
    conn = _hub(db)
    try:
        for index in range(3):
            _send(conn, f"message {index}")
        assert _claim(conn, "acquire", "repo-a")[0] == 200
        assert _claim(conn, "acquire", "repo-b", holder="h2", node=NODE_B)[0] == 200
        _bump_roster(conn)
    finally:
        conn.close()
    return db


def _copy(tmp_path: Path, db: Path) -> Path:
    dest = tmp_path / "backups"
    report = backup.take_backup([db], dest, now=T0, disk_usage=_plenty)
    return Path(report["copy"]) / db.name


# --------------------------------------------------------------- recovery floor


def test_floor_allocates_above_it_for_new_and_existing_targets(tmp_path):
    conn = _hub(tmp_path / "hub.db")
    try:
        assert _claim(conn, "acquire", "seen")[1]["claim"]["generation"] == 1
        assert _claim(conn, "release", "seen", scope="holder", generation=1)[0] == 200
        conn.execute("BEGIN IMMEDIATE")
        floor = fleet_hub_claims.write_recovery_floor(conn, 0, advance=100, restored_at="t", copy_sha256="x")
        conn.commit()
        assert floor == 102
        assert fleet_hub_claims.recovery_floor(conn) == 102
        assert conn.execute("SELECT generation FROM claim_fences WHERE target='seen'").fetchone()[0] == 102
        status, fresh = _claim(conn, "acquire", "never-seen")
        assert status == 200 and fresh["claim"]["generation"] == 103
        assert _claim(conn, "acquire", "seen", holder="h3")[1]["claim"]["generation"] == 103
    finally:
        conn.close()


def test_floor_retry_accepts_only_the_exact_predecessor(tmp_path):
    conn = _hub(tmp_path / "hub.db")
    try:
        assert _claim(conn, "acquire", "seen")[1]["claim"]["generation"] == 1
        assert _claim(conn, "release", "seen", scope="holder", generation=1)[0] == 200
        conn.execute("BEGIN IMMEDIATE")
        floor = fleet_hub_claims.write_recovery_floor(conn, 0, advance=50, restored_at="t", copy_sha256="x")
        conn.commit()
        # A fence that existed at restore sits at the floor, so the first grant
        # after it has predecessor floor and its retry stays idempotent.
        first = _claim(conn, "acquire", "seen", generation=floor)
        assert first[1]["claim"]["generation"] == floor + 1
        retry = _claim(conn, "acquire", "seen", generation=floor)
        assert retry[0] == 200 and retry[1]["claim"]["generation"] == floor + 1
        # A replayed acquire from before the restore carries an old generation
        # and must not pass as that retry or replace the live claim.
        lock = retry[1]["claim"]["lock_token"] if "lock_token" in retry[1]["claim"] else None
        replay = _claim(conn, "acquire", "seen", generation=2)
        assert replay[0] == 409 and replay[1]["reason"] == "stale-generation"
        if lock is not None:
            assert _claim(conn, "inspect", "seen")[1]["claim"]["lock_token"] == lock
        assert _claim(conn, "acquire", "seen", holder="other", node=NODE_B, generation=floor)[0] == 409
        # A target with no fence row before the restore has predecessor 0 only
        # in name; its post-restore retry with generation 0 is refused safely.
        fresh = _claim(conn, "acquire", "t1", generation=0)
        assert fresh[0] == 200 and fresh[1]["claim"]["generation"] == floor + 1
        assert _claim(conn, "acquire", "t1", generation=0)[0] == 409
    finally:
        conn.close()


@pytest.mark.parametrize("value", ["not json", '{"claim_floor": -1}', '{"claim_floor": "7"}', "{}"])
def test_corrupt_floor_fails_closed(tmp_path, value):
    conn = _hub(tmp_path / "hub.db")
    try:
        conn.execute("INSERT INTO hub_meta (key, value) VALUES ('recovery', ?)", (value,))
        conn.commit()
        with pytest.raises(fleet_hub_claims.ClaimFenceError):
            _claim(conn, "acquire", "t1")
    finally:
        conn.close()


def test_schema_version_covers_the_floor_and_older_python_refuses(tmp_path, monkeypatch):
    assert fleet_hub.SCHEMA_VERSION >= 50
    path = tmp_path / "hub.db"
    fleet_hub.init_db(path).close()
    monkeypatch.setattr(fleet_hub, "SCHEMA_VERSION", 49)
    with pytest.raises(fleet_hub.FleetHubError):
        fleet_hub.init_db(path)


def test_v49_file_upgrades_without_ddl(tmp_path):
    path = tmp_path / "hub.db"
    conn = fleet_hub.init_db(path)
    conn.execute("PRAGMA user_version=49")
    conn.commit()
    conn.close()
    conn = fleet_hub.init_db(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == fleet_hub.SCHEMA_VERSION
        assert fleet_hub_claims.recovery_floor(conn) == 0
    finally:
        conn.close()


# ----------------------------------------------------------------------- backup


def test_backup_writes_an_integrity_checked_private_copy(tmp_path):
    db = _populated(tmp_path)
    dest = tmp_path / "backups"
    report = backup.take_backup([db], dest, now=T0, disk_usage=_plenty)
    copy = Path(report["copy"])
    assert copy.name == "20261104T120000Z"
    manifest = json.loads((copy / backup.MANIFEST).read_text(encoding="utf-8"))
    (entry,) = manifest["files"]
    assert entry["integrity_check"] == "ok" and entry["user_version"] == fleet_hub.SCHEMA_VERSION
    # The manifest leaves the host in the off-host backup set, so it names the
    # source by file name only and never carries a host path.
    assert entry["source"] == db.name
    assert str(tmp_path) not in (copy / backup.MANIFEST).read_text(encoding="utf-8")
    restored = sqlite3.connect(copy / db.name)
    try:
        assert restored.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert restored.execute("SELECT COUNT(*) FROM fleet_messages").fetchone()[0] == 3
    finally:
        restored.close()
    if os.name != "nt":
        assert stat.S_IMODE(dest.stat().st_mode) == 0o700
        assert stat.S_IMODE((copy / db.name).stat().st_mode) == 0o600
    assert not list(dest.glob(".partial-*"))


def test_backup_copies_a_database_whose_path_contains_uri_characters(tmp_path):
    # A raw `file:` URI reads `#` as a fragment and drops the rest of the name and
    # `?mode=rw`, which backed up an empty database at a truncated path.
    db = _populated(tmp_path)
    odd = db.with_name("hub#primary?x%41.db")
    db.rename(odd)
    report = backup.take_backup([odd], tmp_path / "backups", now=T0, disk_usage=_plenty)
    copy = Path(report["copy"]) / odd.name
    restored = sqlite3.connect(copy)
    try:
        assert restored.execute("SELECT COUNT(*) FROM fleet_messages").fetchone()[0] == 3
        assert restored.execute("PRAGMA user_version").fetchone()[0] == fleet_hub.SCHEMA_VERSION
    finally:
        restored.close()
    assert not (odd.parent / "hub").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_backup_refuses_a_directory_others_can_read(tmp_path):
    db = _populated(tmp_path)
    dest = tmp_path / "backups"
    dest.mkdir(mode=0o755)
    os.chmod(dest, 0o755)
    with pytest.raises(backup.BackupError, match="must be 0700"):
        backup.take_backup([db], dest, now=T0, disk_usage=_plenty)


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership")
def test_backup_owner_names_the_account_the_directory_must_belong_to(tmp_path, monkeypatch):
    db = _populated(tmp_path)
    me = (os.getuid(), os.getgid())
    calls = []
    real_chown = os.chown
    monkeypatch.setattr(backup.os, "chown", lambda path, uid, gid, **kw: calls.append(Path(path).name))
    report = backup.take_backup([db], tmp_path / "owned", now=T0, disk_usage=_plenty, owner=me)
    assert {"owned", db.name, backup.MANIFEST} <= set(calls)
    monkeypatch.setattr(backup.os, "chown", real_chown)
    with pytest.raises(backup.BackupError, match="not uid"):
        backup.take_backup([db], tmp_path / "owned", now=T0, disk_usage=_plenty, owner=(me[0] + 1, me[1]))
    assert Path(report["copy"]).is_dir()


def test_backup_refuses_a_missing_source_instead_of_creating_one(tmp_path):
    with pytest.raises(backup.BackupError, match="does not exist"):
        backup.take_backup([tmp_path / "absent.db"], tmp_path / "backups", now=T0, disk_usage=_plenty)
    assert not (tmp_path / "absent.db").exists()


def test_backup_fails_and_leaves_nothing_when_integrity_check_fails(tmp_path, monkeypatch):
    db = _populated(tmp_path)
    monkeypatch.setattr(backup, "_integrity", lambda path: "row 1 missing from index")
    with pytest.raises(backup.BackupError, match="integrity_check failed"):
        backup.take_backup([db], tmp_path / "backups", now=T0, disk_usage=_plenty)
    assert backup.list_copies(tmp_path / "backups") == []
    assert not list((tmp_path / "backups").glob(".partial-*"))


def test_retention_keeps_48_hourly_and_14_daily(tmp_path):
    db = _populated(tmp_path)
    dest = tmp_path / "backups"
    # Five days of hourly copies, then one per day for 20 days before that.
    stamps = [T0 - timedelta(hours=hour) for hour in range(5 * 24)]
    stamps += [T0 - timedelta(days=5 + day) for day in range(20)]
    for moment in sorted(stamps):
        backup.take_backup([db], dest, now=moment, disk_usage=_plenty)
    kept = backup.list_copies(dest)
    hourly, daily = backup.retained(kept, keep_hourly=48, keep_daily=14)
    assert {item.stamp for item in kept} == hourly | daily
    assert len(hourly) == 48 and len(daily) == 14
    assert kept[-1].stamp == "20261104T120000Z"
    assert len({item.stamp[:8] for item in kept}) == 14


def test_free_space_floor_drops_the_oldest_hourly_copy_first_then_refuses(tmp_path):
    db = _populated(tmp_path)
    dest = tmp_path / "backups"
    for hour in range(4):
        backup.take_backup([db], dest, now=T0 + timedelta(hours=hour), disk_usage=_plenty)
    wal = db.with_name(db.name + "-wal")
    need = db.stat().st_size + (wal.stat().st_size if wal.exists() else 0)
    floor = 1000

    def one_copy_short(_path):
        # Room for the new copy appears only after one old copy is removed.
        removed = 4 - len(backup.list_copies(dest))
        return SimpleNamespace(free=floor + need - 1 + need * removed)

    report = backup.take_backup(
        [db], dest, now=T0 + timedelta(hours=4), disk_usage=one_copy_short, keep_daily=0, min_free_bytes=floor
    )
    assert report["pruned_for_space"] == ["20261104T120000Z"]
    before = [item.stamp for item in backup.list_copies(dest)]
    with pytest.raises(backup.BackupError, match="refusing copy"):
        backup.take_backup(
            [db],
            dest,
            now=T0 + timedelta(hours=5),
            disk_usage=lambda _path: SimpleNamespace(free=0),
            keep_daily=0,
            min_free_bytes=10**12,
        )
    # A copy that cannot be made to fit removes nothing.
    assert [item.stamp for item in backup.list_copies(dest)] == before
    assert before[-1] == "20261104T160000Z"


def test_plan_retention_keeps_the_directory_under_a_tenth_of_free_space():
    plan = backup.plan_retention(copy_bytes=2 * 2**30, free_bytes=426 * 2**30)
    assert plan["keep_hourly"] == 7
    assert (plan["keep_hourly"] + backup.DEFAULT_KEEP_DAILY) * plan["copy_bytes"] <= plan["budget_bytes"]
    assert backup.plan_retention(copy_bytes=1, free_bytes=10**12)["keep_hourly"] == 48


def test_cli_backup_and_plan(tmp_path, capsys):
    db = _populated(tmp_path)
    dest = tmp_path / "cli-backups"
    assert cli.main(["fleet", "backup", "--db", str(db), "--dest", str(dest), "--min-free-gib", "0", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert Path(report["copy"]).is_dir()
    assert cli.main(["fleet", "backup", "--db", str(db), "--dest", str(dest), "--plan", "--json"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["files"] == {str(db): db.stat().st_size} and plan["keep_hourly"] >= 1


# ---------------------------------------------------------------------- restore


def _ledgers(tmp_path):
    """Both ledger copies, created empty the way backup setup creates them."""
    store, host = tmp_path / "backups" / backup.LEDGER_NAME, tmp_path / "host" / backup.LEDGER_NAME
    for path in (store, host):
        path.parent.mkdir(exist_ok=True)
        path.touch()
    return [store, host]


def test_restore_advances_every_counter_and_ends_every_claim(tmp_path):
    db = _populated(tmp_path)
    copy = _copy(tmp_path, db)
    target = tmp_path / "restored" / "fleet-hub.db"
    target.parent.mkdir()
    report = backup.restore(copy, target, ledgers=_ledgers(tmp_path), now=T0)
    assert report["claims_ended"] == 2
    assert report["message_sequence"] == 3 + ADV
    conn = fleet_hub.init_db(target)
    try:
        assert conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 0
        assert fleet_hub_claims.recovery_floor(conn) == report["claim_floor"] > ADV
        # A holder from copy time is refused as stale on its next renewal.
        status, renewed = _claim(conn, "renew", "repo-a", generation=1)
        assert status == 409 and renewed["renewed"] is False
        assert _send(conn) == 3 + ADV + 1
        assert fleet_hub_model_roster.raw_revision(conn) == report["roster_revision"] == 2 + ADV
        policy = fleet_hub_policy.current_policy(conn)
        assert policy["revision"] == report["policy_revision"] == 1 + ADV
        assert policy["actor"] == backup.RESTORE_ACTOR and policy["parent_revision"] == 1
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()
    for ledger in _ledgers(tmp_path):
        (record,) = backup.read_ledger(ledger)
        assert record["claim_floor"] == report["claim_floor"]
        assert record["copy"]["sha256"] == report["copy_sha256"]
    assert not target.with_name(target.name + ".restoring").exists()


def test_restored_file_is_refused_by_a_python_that_predates_the_floor(tmp_path, monkeypatch):
    db = _populated(tmp_path)
    target = tmp_path / "restored.db"
    backup.restore(_copy(tmp_path, db), target, ledgers=_ledgers(tmp_path), now=T0)
    monkeypatch.setattr(fleet_hub, "SCHEMA_VERSION", 49)
    with pytest.raises(fleet_hub.FleetHubError):
        fleet_hub.init_db(target)


def test_second_restore_from_the_same_copy_goes_past_everything_the_first_issued(tmp_path):
    db = _populated(tmp_path)
    copy = _copy(tmp_path, db)
    ledgers = _ledgers(tmp_path)
    first_path, second_path = tmp_path / "first.db", tmp_path / "second.db"
    first = backup.restore(copy, first_path, ledgers=ledgers, now=T0)
    conn = fleet_hub.init_db(first_path)
    try:
        cursor = _send(conn, "after first")
        for _ in range(5):
            last_first_message = _send(conn)
        issued = [_claim(conn, "acquire", f"t{index}")[1]["claim"]["generation"] for index in range(3)]
        first_roster = _bump_roster(conn)
        first_policy = fleet_hub_policy.current_policy(conn)["revision"]
    finally:
        conn.close()
    second = backup.restore(copy, second_path, ledgers=ledgers, now=T0 + timedelta(minutes=5))
    assert second["message_sequence"] > last_first_message
    assert second["claim_floor"] > max(issued) and second["claim_floor"] > first["claim_floor"]
    assert second["roster_revision"] > first_roster and second["policy_revision"] > first_policy
    conn = fleet_hub.init_db(second_path)
    try:
        sent_after = _send(conn, "after second")
        rows = conn.execute("SELECT id FROM fleet_messages WHERE id > ?", (cursor,)).fetchall()
        assert (sent_after,) in rows
    finally:
        conn.close()
    assert [len(backup.read_ledger(path)) for path in ledgers] == [2, 2]


def test_one_reachable_ledger_is_enough_and_its_records_count(tmp_path):
    db = _populated(tmp_path)
    copy = _copy(tmp_path, db)
    store = tmp_path / "backups" / backup.LEDGER_NAME
    store.touch()
    first = backup.restore(copy, tmp_path / "a.db", ledgers=[store], now=T0)
    missing_host = tmp_path / "gone" / "dir" / backup.LEDGER_NAME
    second = backup.restore(copy, tmp_path / "b.db", ledgers=[store, missing_host], now=T0)
    assert second["ledgers_unreachable"] == [str(missing_host)]
    assert second["claim_floor"] > first["claim_floor"]


def test_restore_refuses_without_a_ledger_unless_the_operator_states_no_prior_restore(tmp_path):
    db = _populated(tmp_path)
    copy = _copy(tmp_path, db)
    nowhere = tmp_path / "unmounted" / "store" / backup.LEDGER_NAME
    target = tmp_path / "r.db"
    with pytest.raises(backup.BackupError, match="no recovery ledger is reachable"):
        backup.restore(copy, target, ledgers=[nowhere], now=T0)
    assert not target.exists()
    report = backup.restore(copy, target, ledgers=[nowhere], assert_no_prior_restore=True, now=T0)
    (record,) = backup.read_ledger(nowhere)
    assert record["no_prior_restore_asserted"] is True and report["ledgers_written"] == [str(nowhere)]


def test_a_lost_ledger_in_a_writable_directory_does_not_count_as_reachable(tmp_path):
    db = _populated(tmp_path)
    copy = _copy(tmp_path, db)
    ledgers = _ledgers(tmp_path)
    first = backup.restore(copy, tmp_path / "a.db", ledgers=ledgers, now=T0)
    for path in ledgers:
        path.unlink()
    with pytest.raises(backup.BackupError, match="no recovery ledger is reachable"):
        backup.restore(copy, tmp_path / "b.db", ledgers=ledgers, now=T0)
    # One surviving copy is enough. The lost one stays absent: without the
    # assertion the tool never creates a ledger file, because its directory
    # may be the mount point of an unmounted volume.
    ledgers[0].write_text(json.dumps({**first, "copy": {}}, default=str) + "\n", encoding="utf-8")
    second = backup.restore(copy, tmp_path / "c.db", ledgers=ledgers, now=T0)
    assert second["claim_floor"] > first["claim_floor"]
    assert not ledgers[1].exists()
    assert second["ledgers_unreachable"] == [str(ledgers[1])]
    assert second["ledgers_written"] == [str(ledgers[0])]


def test_a_missing_ledger_beside_a_surviving_one_is_created_only_under_the_assertion(tmp_path):
    db = _populated(tmp_path)
    copy = _copy(tmp_path, db)
    store = tmp_path / "backups" / backup.LEDGER_NAME
    store.touch()
    mount_point = tmp_path / "unmounted-volume"
    mount_point.mkdir()
    hidden = mount_point / backup.LEDGER_NAME
    report = backup.restore(copy, tmp_path / "a.db", ledgers=[store, hidden], now=T0)
    assert not hidden.exists() and list(mount_point.iterdir()) == []
    assert report["ledgers_unreachable"] == [str(hidden)]
    asserted = backup.restore(copy, tmp_path / "b.db", ledgers=[store, hidden], assert_no_prior_restore=True, now=T0)
    assert asserted["ledgers_unreachable"] == []
    (record,) = backup.read_ledger(hidden)
    assert record["no_prior_restore_asserted"] is True and record["claim_floor"] == asserted["claim_floor"]


def test_ledger_records_name_the_copy_relative_to_the_backup_directory(tmp_path):
    db = _populated(tmp_path)
    copy = _copy(tmp_path, db)
    dest = tmp_path / "backups"
    ledgers = _ledgers(tmp_path)
    backup.restore(copy, tmp_path / "r.db", ledgers=ledgers, now=T0)
    for ledger in ledgers:
        text = ledger.read_text(encoding="utf-8")
        assert str(tmp_path) not in text
        (record,) = backup.read_ledger(ledger)
        assert record["copy"]["path"] == f"{copy.parent.name}/{copy.name}"
        assert (dest / record["copy"]["path"]).samefile(copy)


def test_a_copy_outside_a_dated_directory_is_recorded_by_file_name(tmp_path):
    db = _populated(tmp_path)
    loose = tmp_path / "elsewhere" / "fleet-hub.db"
    loose.parent.mkdir()
    shutil.copyfile(_copy(tmp_path, db), loose)
    ledgers = _ledgers(tmp_path)
    backup.restore(loose, tmp_path / "r.db", ledgers=ledgers, now=T0)
    (record,) = backup.read_ledger(ledgers[0])
    assert record["copy"]["path"] == "fleet-hub.db"


def test_ledger_records_with_absolute_copy_paths_still_parse_and_count(tmp_path):
    db = _populated(tmp_path)
    copy = _copy(tmp_path, db)
    ledgers = _ledgers(tmp_path)
    older = {
        "time": "2026-11-01T00:00:00Z",
        "copy": {"path": "/srv/hub-backups/20261101T000000Z/fleet-hub.db", "sha256": "0" * 64},
        "message_sequence": 7 * ADV,
        "claim_floor": 7 * ADV,
        "roster_revision": 7 * ADV,
        "policy_revision": 7 * ADV,
        "no_prior_restore_asserted": False,
    }
    ledgers[0].write_text(json.dumps(older) + "\n", encoding="utf-8")
    (parsed,) = backup.read_ledger(ledgers[0])
    assert parsed["copy"]["path"] == older["copy"]["path"]
    report = backup.restore(copy, tmp_path / "r.db", ledgers=ledgers, now=T0)
    for field in backup.LEDGER_FIELDS:
        assert report[field] >= 8 * ADV, field


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm", "-journal"])
def test_restore_refuses_any_hub_file_already_at_the_target(tmp_path, suffix):
    db = _populated(tmp_path)
    copy = _copy(tmp_path, db)
    target = tmp_path / "r.db"
    leftover = target.with_name(target.name + suffix)
    leftover.write_bytes(b"left over")
    with pytest.raises(backup.BackupError, match="already exists"):
        backup.restore(copy, target, ledgers=_ledgers(tmp_path), now=T0)
    assert leftover.read_bytes() == b"left over"
    assert all(len(backup.read_ledger(path)) == 0 for path in _ledgers(tmp_path))


def test_restore_clears_every_stale_staging_sidecar_including_the_journal(tmp_path, monkeypatch):
    db = _populated(tmp_path)
    copy = _copy(tmp_path, db)
    target = tmp_path / "r.db"
    staging = target.with_name(target.name + ".restoring")
    sidecars = [staging.with_name(staging.name + suffix) for suffix in ("", "-wal", "-shm", "-journal")]
    for path in sidecars:
        path.write_bytes(b"stale")
    real_copy = backup._online_copy
    seen = []

    def checked_copy(source, destination):
        # A stale hot journal beside the staging name would be rolled into it.
        seen.append([path.name for path in sidecars if path.exists()])
        return real_copy(source, destination)

    monkeypatch.setattr(backup, "_online_copy", checked_copy)
    backup.restore(copy, target, ledgers=_ledgers(tmp_path), now=T0)
    assert seen == [[]]
    assert not any(path.exists() for path in sidecars)
    assert backup._integrity(target) == "ok"


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm", "-journal"])
def test_publish_refuses_a_hub_file_that_appeared_during_the_restore(tmp_path, monkeypatch, suffix):
    db = _populated(tmp_path)
    copy = _copy(tmp_path, db)
    target = tmp_path / "r.db"
    staging = target.with_name(target.name + ".restoring")
    appeared = target.with_name(target.name + suffix)
    real_fsync = backup._fsync_file

    def hub_restarts(path):
        # The crash-looping hub comes back between the advances and the publish.
        real_fsync(path)
        if Path(path) == staging:
            appeared.write_bytes(b"live hub")

    monkeypatch.setattr(backup, "_fsync_file", hub_restarts)
    with pytest.raises(backup.BackupError, match="appeared during the restore"):
        backup.restore(copy, target, ledgers=_ledgers(tmp_path), now=T0)
    assert appeared.read_bytes() == b"live hub"
    assert not staging.exists()


def test_publish_never_replaces_a_target_created_after_the_last_check(tmp_path, monkeypatch):
    db = _populated(tmp_path)
    copy = _copy(tmp_path, db)
    target = tmp_path / "r.db"
    real_refuse = backup._refuse_existing_hub_files
    calls = []

    def racing_hub(path, message):
        real_refuse(path, message)
        calls.append(path)
        if len(calls) == 2:
            # The hub creates its file right after the pre-publish check.
            target.write_bytes(b"live hub")

    monkeypatch.setattr(backup, "_refuse_existing_hub_files", racing_hub)
    with pytest.raises(backup.BackupError, match="appeared during the restore"):
        backup.restore(copy, target, ledgers=_ledgers(tmp_path), now=T0)
    assert len(calls) == 2
    assert target.read_bytes() == b"live hub"
    assert not target.with_name(target.name + ".restoring").exists()


def test_a_second_restore_to_the_same_target_waits_for_the_staging_lock_and_leaves_the_first_alone(
    tmp_path, monkeypatch
):
    from brigade import inbox_lock

    db = _populated(tmp_path)
    copy = _copy(tmp_path, db)
    target = tmp_path / "r.db"
    staging = target.with_name(target.name + ".restoring")
    monkeypatch.setattr(backup, "RESTORE_LOCK_DEADLINE_SECONDS", 0.2)
    # The first restore holds the staging lock and has its staging file in progress.
    with inbox_lock.held_file_lock(backup.staging_lock_path(target), deadline_seconds=1):
        staging.write_bytes(b"first restore in progress")
        with pytest.raises(backup.BackupError, match="another restore"):
            backup.restore(copy, target, ledgers=_ledgers(tmp_path), now=T0)
        assert staging.read_bytes() == b"first restore in progress"
    assert all(len(backup.read_ledger(path)) == 0 for path in _ledgers(tmp_path))
    assert not target.exists()


def test_concurrent_restores_sharing_a_ledger_allocate_distinct_ranges(tmp_path, monkeypatch):
    import threading

    db = _populated(tmp_path)
    copy = _copy(tmp_path, db)
    ledgers = _ledgers(tmp_path)
    real_base = backup.ledger_base

    def slow_base(records):
        # Widen the window between reading the ledger and appending to it.
        base = real_base(records)
        threading.Event().wait(0.5)
        return base

    monkeypatch.setattr(backup, "ledger_base", slow_base)
    reports, errors = [], []

    def run(name):
        try:
            reports.append(backup.restore(copy, tmp_path / f"{name}.db", ledgers=ledgers, now=T0))
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(name,)) for name in ("x", "y")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not errors and len(reports) == 2
    for field in ("message_sequence", "claim_floor", "roster_revision", "policy_revision"):
        assert reports[0][field] != reports[1][field], field
    assert [len(backup.read_ledger(path)) for path in ledgers] == [2, 2]


def test_restore_refuses_an_existing_target_and_a_corrupt_ledger(tmp_path):
    db = _populated(tmp_path)
    copy = _copy(tmp_path, db)
    with pytest.raises(backup.BackupError, match="already exists"):
        backup.restore(copy, db, ledgers=_ledgers(tmp_path), now=T0)
    ledger = _ledgers(tmp_path)[0]
    ledger.write_text("{not json\n", encoding="utf-8")
    with pytest.raises(backup.BackupError, match="not JSON"):
        backup.restore(copy, tmp_path / "x.db", ledgers=[ledger], now=T0)
    assert not (tmp_path / "x.db").exists()


def test_a_second_hub_from_the_same_copy_cannot_rewind_the_restored_hub(tmp_path):
    db = _populated(tmp_path)
    copy = _copy(tmp_path, db)
    # The stand-in for writes the copy lost: a hub started from the same copy.
    stand_in = tmp_path / "stand-in.db"
    shutil.copyfile(copy, stand_in)
    conn = fleet_hub.init_db(stand_in)
    try:
        lost = _claim(conn, "acquire", "unseen-target", holder="lost-holder")[1]["claim"]["generation"]
        lost_roster = _bump_roster(conn)
    finally:
        conn.close()
    target = tmp_path / "restored.db"
    report = backup.restore(copy, target, ledgers=_ledgers(tmp_path), now=T0)
    conn = fleet_hub.init_db(target)
    try:
        status, renewal = _claim(conn, "renew", "unseen-target", holder="lost-holder", generation=lost)
        assert status == 409 and renewal["renewed"] is False
        fresh = _claim(conn, "acquire", "unseen-target", holder="new-holder")[1]["claim"]["generation"]
        assert fresh > report["claim_floor"] > lost
        assert fleet_hub_model_roster.raw_revision(conn) > lost_roster
    finally:
        conn.close()


def test_restore_with_activated_policy_authority_projects_the_roster_from_the_policy(tmp_path, monkeypatch):
    from brigade import fleet_policy_migration

    db = _populated(tmp_path)
    copy = _copy(tmp_path, db)
    monkeypatch.setattr(fleet_policy_migration, "is_activated", lambda conn: True)
    report = backup.restore(copy, tmp_path / "r.db", ledgers=_ledgers(tmp_path), now=T0)
    assert report["roster_revision"] == report["policy_revision"]


def test_cli_restore_reports_and_refuses(tmp_path, capsys):
    db = _populated(tmp_path)
    copy = _copy(tmp_path, db)
    ledger = tmp_path / "backups" / backup.LEDGER_NAME
    ledger.touch()
    target = tmp_path / "cli.db"
    args = ["fleet", "restore", "--copy", str(copy), "--db", str(target), "--ledger", str(ledger), "--json"]
    assert cli.main(args) == 0
    assert json.loads(capsys.readouterr().out)["claims_ended"] == 2
    assert cli.main(args) == 1
    assert "already exists" in capsys.readouterr().err


@pytest.mark.skipif(os.name == "nt", reason="POSIX signals")
def test_sigterm_ends_fleet_serve_with_a_clean_exit(tmp_path):
    import signal
    import socket
    import subprocess
    import sys
    import time
    import urllib.request

    token = tmp_path / "token"
    token.write_text("fake-admin-token-for-sigterm\n", encoding="utf-8")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "brigade",
            "fleet",
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--db",
            str(tmp_path / "hub.db"),
            "--token-file",
            str(token),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1).read()
                break
            except OSError:
                assert proc.poll() is None and time.monotonic() < deadline
                time.sleep(0.1)
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0, out
    assert "brigade fleet hub stopped cleanly" in out


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_online_copy_creates_its_destination_private_under_a_permissive_umask(tmp_path):
    # The restore staging file sits next to the hub database, which may be in a
    # directory others can traverse. It must never be readable mid-copy.
    db = _populated(tmp_path)
    destination = tmp_path / "staging.db"
    previous = os.umask(0o022)
    try:
        backup._online_copy(db, destination)
    finally:
        os.umask(previous)
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
