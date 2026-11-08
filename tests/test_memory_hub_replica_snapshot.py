"""A replica whose meta.json names a missing heads snapshot is never read as a caught-up empty replica."""

from __future__ import annotations

import errno
import hashlib
import os

import pytest

from brigade import memory_hub_replica_limits as limits
from brigade import memory_hub_search as hub_search
from brigade import memory_hub_store as store
from brigade import memory_hub_sync as sync
from tests.test_memory_hub_client import (  # noqa: F401
    _card_x,
    _put,
    client,
    hub,
    settings,
    workspace,
)

BOUNDS = {"max_cards": 50_000, "max_bytes": 64 * 1024 * 1024}


@pytest.fixture
def lost(hub, tmp_path):  # noqa: F811
    """A read workspace whose replica held a fleet card, then lost its named heads snapshot."""
    target = workspace(tmp_path, mode="read")
    _put(hub, "fleet", "x", "otter secret text")
    sync.pull(target, client(hub), settings(target))
    live = store.replica(target)
    assert live.heads and isinstance(live.meta.get("seq"), int)
    store.heads_path(live.directory, live.meta).unlink()
    return target


def test_unbounded_load_marks_the_replica_unusable(lost):
    replica = store.replica(lost)
    assert replica.snapshot_missing
    assert not replica.exists and replica.heads == {}
    assert replica.watermark("anything") is None
    assert store.replica_snapshot_missing(lost)


def test_bounded_load_marks_the_replica_unusable_without_decoding(lost, monkeypatch):
    def forbid(raw):
        raise AssertionError("a missing snapshot reached decoding")

    monkeypatch.setattr(limits, "decode_heads", forbid)
    with limits.bounded(**BOUNDS):
        replica = store.replica(lost)
    assert replica.snapshot_missing
    assert not replica.exists and replica.heads == {}
    assert replica.watermark("anything") is None


def test_bounded_pull_rebuilds_an_unusable_replica_from_the_hub(hub, lost):  # noqa: F811
    _put(hub, "fleet", "y", "badger text")
    with limits.bounded(**BOUNDS):
        report = sync.pull(lost, client(hub), settings(lost))
        live = store.replica(lost)
    assert report["rebuilt"] is True
    assert any("heads snapshot was missing" in warning for warning in report["warnings"])
    assert not live.snapshot_missing
    assert set(live.heads) == {store.Replica.key("fleet", "x"), store.Replica.key("fleet", "y")}
    assert live.meta["seq"] == report["seq"] and live.meta["cards"] == 2
    raw = store.heads_path(live.directory, live.meta).read_bytes()
    assert live.meta["heads_sha256"] == hashlib.sha256(raw).hexdigest()
    assert not store.replica_snapshot_missing(lost)


def test_bounded_rebuild_still_enforces_the_card_bound(hub, lost):  # noqa: F811
    _put(hub, "fleet", "y", "badger text")
    with limits.bounded(max_cards=1, max_bytes=BOUNDS["max_bytes"]), pytest.raises(limits.ReplicaLimitError):
        sync.pull(lost, client(hub), settings(lost))
    assert not store.replica(lost).exists


def test_search_does_not_answer_from_an_unusable_replica_as_if_current(hub, lost):  # noqa: F811
    hub.mode = "down"
    found = hub_search.search_payload(lost, "otter", settings=settings(lost), transport=hub)
    assert not [m for m in found["matches"] if m.get("source") == "replica"]
    assert found["retrieval"]["replica_unusable"] == "snapshot-missing"
    assert "heads snapshot is missing" in found["warning"]


def test_card_does_not_fall_back_to_an_unusable_replica(hub, lost, capsys):  # noqa: F811
    hub.mode = "down"
    code, payload, _ = _card_x(lost, capsys)
    assert payload.get("source") != "replica"
    assert "otter secret text" not in str(payload)
    assert code != 0


def test_status_reports_the_replica_unusable(hub, lost):  # noqa: F811
    report = sync.status(lost, settings(lost), client(hub))
    assert report["replica"] is None
    assert report["replica_unusable"] == "snapshot-missing"
    assert any("heads snapshot is missing" in warning for warning in report["warnings"])


def test_pull_rebuilds_an_unusable_replica_from_the_hub(hub, lost):  # noqa: F811
    _put(hub, "fleet", "y", "badger text")
    report = sync.pull(lost, client(hub), settings(lost))
    assert report["rebuilt"] is True
    assert any("heads snapshot was missing" in warning for warning in report["warnings"])
    live = store.replica(lost)
    assert not live.snapshot_missing
    assert set(live.heads) == {store.Replica.key("fleet", "x"), store.Replica.key("fleet", "y")}
    assert live.meta["seq"] == report["seq"] and live.meta["cards"] == 2
    assert store.heads_path(live.directory, live.meta).is_file()
    assert not store.replica_snapshot_missing(lost)
    again = sync.pull(lost, client(hub), settings(lost))
    assert again["rebuilt"] is False and again["warnings"] == []


# --- directory sync ----------------------------------------------------------------


def _record(monkeypatch, directory):
    """Record replaces into `directory` and fsyncs of it, noting the snapshots present at each sync."""
    events: list[tuple[str, object]] = []
    opened: dict[int, str] = {}
    real_open, real_fsync, real_replace = os.open, os.fsync, os.replace

    def fake_open(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        opened[fd] = os.fspath(path)
        return fd

    def fake_fsync(fd):
        if opened.get(fd) == os.fspath(directory):
            events.append(("fsync", sorted(p.name for p in directory.glob("heads-*.json"))))
        return real_fsync(fd)

    def fake_replace(src, dst, *args, **kwargs):
        if os.path.dirname(os.fspath(dst)) == os.fspath(directory):
            events.append(("replace", os.path.basename(os.fspath(dst))))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(store.os, "open", fake_open)
    monkeypatch.setattr(store.os, "fsync", fake_fsync)
    monkeypatch.setattr(store.os, "replace", fake_replace)
    return events


def _replica_with_card(tmp_path, text):
    replica = store.Replica(tmp_path / "replica")
    replica.meta = {"hub": "h", "seq": 1, "principal": "p", "log_epoch": "e", "read_scopes": ["fleet"]}
    replica.heads[store.Replica.key("fleet", "x")] = {"scope": "fleet", "card_id": "x", "text": text}
    return replica


@pytest.mark.skipif(os.name == "nt", reason="directories cannot be synced on Windows")
def test_save_syncs_the_directory_after_each_rename_and_before_cleanup(tmp_path, monkeypatch):
    replica = _replica_with_card(tmp_path, "one")
    replica.save()
    old = store.heads_path(replica.directory, replica.meta).name
    replica.heads[store.Replica.key("fleet", "x")]["text"] = "two"
    events = _record(monkeypatch, replica.directory)
    replica.save()
    new = store.heads_path(replica.directory, replica.meta).name
    assert new != old
    assert events == [
        ("replace", new),
        ("fsync", sorted([old, new])),
        ("replace", "meta.json"),
        ("fsync", sorted([old, new])),  # the previous snapshot is still there when meta.json is durable
    ]
    assert [p.name for p in replica.directory.glob("heads-*.json")] == [new]


def test_save_skips_the_directory_sync_where_unsupported(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_DIR_FSYNC", False)
    synced: list[int] = []
    real_fsync = os.fsync

    def fake_fsync(fd):
        synced.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(store.os, "fsync", fake_fsync)
    replica = _replica_with_card(tmp_path, "one")
    replica.save()
    assert len(synced) == 2  # the two file syncs only
    assert store.Replica(replica.directory).heads == replica.heads


def _refuse_directory_open(monkeypatch, code):
    real_open = os.open

    def refuse_directories(path, flags, *args, **kwargs):
        if os.path.isdir(path):
            raise OSError(code, "cannot open a directory")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(store.os, "open", refuse_directories)


@pytest.mark.parametrize("code", [errno.EINVAL, errno.EOPNOTSUPP])
def test_directory_open_unsupported_is_skipped(tmp_path, monkeypatch, code):
    monkeypatch.setattr(store, "_DIR_FSYNC", True)
    _refuse_directory_open(monkeypatch, code)
    replica = _replica_with_card(tmp_path, "one")
    replica.save()
    assert store.Replica(replica.directory).heads == replica.heads


@pytest.mark.parametrize("code", [errno.EIO, errno.EMFILE, errno.ENFILE, errno.EACCES])
def test_directory_open_failure_propagates(tmp_path, monkeypatch, code):
    monkeypatch.setattr(store, "_DIR_FSYNC", True)
    _refuse_directory_open(monkeypatch, code)
    with pytest.raises(OSError) as raised:
        _replica_with_card(tmp_path, "one").save()
    assert raised.value.errno == code


def test_directory_sync_refused_by_the_filesystem_is_skipped(tmp_path, monkeypatch):
    if os.name == "nt":
        pytest.skip("directories cannot be synced on Windows")
    real_fsync = os.fsync
    directory = tmp_path / "replica"

    def refuse(fd):
        if os.path.samestat(os.fstat(fd), os.stat(directory)):
            raise OSError(errno.EINVAL, "directory sync unsupported")
        return real_fsync(fd)

    monkeypatch.setattr(store.os, "fsync", refuse)
    replica = _replica_with_card(tmp_path, "one")
    replica.save()
    assert store.Replica(directory).heads == replica.heads


def test_directory_sync_io_error_propagates(tmp_path, monkeypatch):
    if os.name == "nt":
        pytest.skip("directories cannot be synced on Windows")
    real_fsync = os.fsync
    directory = tmp_path / "replica"

    def fail(fd):
        if os.path.samestat(os.fstat(fd), os.stat(directory)):
            raise OSError(errno.EIO, "disk error")
        return real_fsync(fd)

    monkeypatch.setattr(store.os, "fsync", fail)
    with pytest.raises(OSError, match="disk error"):
        _replica_with_card(tmp_path, "one").save()
