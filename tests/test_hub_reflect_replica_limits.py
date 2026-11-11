"""Replica count and metadata limits precede full snapshot allocation."""

import contextlib
import hashlib
import json

import pytest

from brigade import memory_hub_store as store, memory_hub_sync as sync, memory_reflect_hub as reflect
from tests.test_memory_reflect_hub import pytestmark as pytestmark, run, setup as setup, writes


@pytest.mark.parametrize("declared", [50_001, 1, None])
def test_large_small_head_snapshot_is_refused_before_materialization(setup, capsys, monkeypatch, declared):
    target, hub, _ = setup
    settings = reflect.hubc.load_settings(target)
    sync.pull(target, reflect.hubc.client_for(settings), settings)
    root = store.root(target) / store.REPLICA
    raw = (
        "{" + ",".join(json.dumps(f"fleet\nsmall-{i}") + ': {"scope":"fleet","text":"x"}' for i in range(50_001)) + "}"
    ).encode()
    assert len(raw) < 64 * 1024 * 1024
    meta = json.loads((root / "meta.json").read_text())
    if declared is None:
        meta.pop("cards", None)
    else:
        meta["cards"] = declared
    meta["heads_sha256"] = hashlib.sha256(raw).hexdigest()
    store.heads_path(root, meta).write_bytes(raw)
    store.write_json(root / "meta.json", meta)
    from brigade import memory_hub_replica_limits as limits

    decoded = []

    def forbid(raw):
        decoded.append(True)
        raise AssertionError("oversized snapshot reached full dictionary decoding")

    monkeypatch.setattr(limits, "decode_heads", forbid)
    code, report = run(target, capsys, "run")
    assert code == 1 and report["error"] == "hub_reflect_scope_limit"
    assert decoded == [] and writes(hub) == []


def test_sync_stops_before_fetching_card_beyond_count_limit(setup, capsys, monkeypatch):
    target, hub, _ = setup
    hub._commit("card.put", "fleet", "third", "Unique", "live", reflect.hubc.event_id())
    monkeypatch.setattr(reflect, "MAX_SCOPE_CARDS", 2)
    code, report = run(target, capsys, "run")
    assert code == 1 and report["error"] == "hub_reflect_scope_limit"
    assert not any(c["action"] == "card" and c["card_id"] == "third" for c in hub.calls)
    assert writes(hub) == []


def test_save_refuses_excess_count_before_writing(tmp_path):
    from brigade import memory_hub_replica_limits as limits

    replica = store.Replica(tmp_path / "replica")
    replica.heads = {str(i): {"text": "x"} for i in range(3)}
    with limits.bounded(max_cards=2, max_bytes=1024):
        with pytest.raises(limits.ReplicaLimitError):
            replica.save()
    assert not list(replica.directory.glob("heads*.json"))


def test_deferred_replay_bounds_examined_superseded_events(tmp_path):
    from brigade import memory_hub_replica_limits as limits

    deferred = sync._Deferred(store.Replica(tmp_path / "replica"))
    event = {"kind": "card.put", "scope": "fleet", "card_id": "same", "revision": 1}
    with limits.bounded(max_cards=10, max_bytes=limits.REPLAY_ENTRY_OVERHEAD * 2):
        for revision in range(2):
            deferred.examine()
            event["revision"] = revision + 1
            deferred.add("fleet\nsame", event)
        with pytest.raises(limits.ReplicaLimitError, match="replay"):
            deferred.examine()


def test_reflect_bounds_metadata_before_parsing(setup, capsys):
    target, hub, _ = setup
    root = store.root(target) / store.REPLICA
    root.mkdir(parents=True)
    (root / "heads.json").write_text("{}")
    with (root / "meta.json").open("wb") as file:
        file.truncate(9 * 1024 * 1024)
    code, report = run(target, capsys, "run", "--scope", "fleet")
    assert code == 1 and report["error"] == "hub_reflect_replica_limit"
    assert hub.calls == []


@pytest.mark.parametrize("forgery", ["cards", "digest", "missing"])
def test_saved_count_and_digest_are_verified_before_decoding(tmp_path, monkeypatch, forgery):
    from brigade import memory_hub_replica_limits as limits

    root = tmp_path / "replica"
    replica = store.Replica(root)
    replica.heads = {f"fleet\ncard-{i}": {"scope": "fleet", "text": "x"} for i in range(2)}
    replica.save()
    meta = store.read_json(root / "meta.json")
    assert meta["cards"] == 2
    saved = store.heads_path(root, meta)
    assert meta["heads_sha256"] == hashlib.sha256(saved.read_bytes()).hexdigest()
    if forgery == "cards":
        meta["cards"] = 1
    elif forgery == "digest":
        # A forged digest whose named file exists but holds other bytes.
        meta["heads_sha256"] = "f" * 64
        store.heads_path(root, meta).write_bytes(saved.read_bytes())
    else:
        saved.unlink()
    store.write_json(root / "meta.json", meta)
    decoded = []

    def forbid(raw):
        decoded.append(True)
        raise AssertionError("unverified snapshot reached dictionary decoding")

    monkeypatch.setattr(limits, "decode_heads", forbid)
    with limits.bounded(max_cards=3, max_bytes=4096):
        if forgery == "missing":
            # Nothing to decode: the replica is unusable, so a pull rebuilds it.
            replica = store.Replica(root)
            assert replica.snapshot_missing and not replica.exists and replica.heads == {}
        else:
            with pytest.raises(OSError):
                store.Replica(root)
    assert decoded == []


def test_saved_digest_matches_the_file_under_windows_newline_translation(tmp_path, monkeypatch):
    from brigade import memory_hub_replica_limits as limits

    real_fdopen = store.os.fdopen

    def windows_fdopen(fd, mode="r", *args, **kwargs):
        # Text mode on Windows writes CRLF unless the caller asks for no translation.
        if "b" not in mode:
            kwargs.setdefault("newline", "\r\n")
        return real_fdopen(fd, mode, *args, **kwargs)

    monkeypatch.setattr(store.os, "fdopen", windows_fdopen)
    root = tmp_path / "replica"
    replica = store.Replica(root)
    replica.heads = {f"fleet\ncard-{i}": {"scope": "fleet", "text": "x"} for i in range(2)}
    replica.save()
    meta = store.read_json(root / "meta.json")
    raw = store.heads_path(root, meta).read_bytes()
    assert b"\r\n" not in raw
    assert meta["heads_sha256"] == hashlib.sha256(raw).hexdigest()
    with limits.bounded(max_cards=10, max_bytes=1024 * 1024):
        assert store.Replica(root).heads == replica.heads


BOUNDS = {"max_cards": reflect.MAX_SCOPE_CARDS, "max_bytes": reflect.MAX_REPLICA_BYTES + reflect.REPLICA_FILE_SLACK}


def _bounded_pull(target):
    from brigade import memory_hub_replica_limits as limits

    settings = reflect.hubc.load_settings(target)
    with limits.bounded(**BOUNDS):
        return sync.pull(target, reflect.hubc.client_for(settings), settings)


def _bounded_load(target):
    from brigade import memory_hub_replica_limits as limits

    with limits.bounded(**BOUNDS):
        return store.replica(target)


def _assert_consistent(replica):
    raw = store.heads_path(replica.directory, replica.meta).read_bytes()
    assert replica.meta["heads_sha256"] == hashlib.sha256(raw).hexdigest()
    assert replica.meta["cards"] == len(replica.heads) == len(json.loads(raw))


def test_save_interrupted_before_metadata_keeps_the_previous_pair(setup, monkeypatch):
    target, hub, _ = setup
    _bounded_pull(target)
    third = store.Replica.key("fleet", "third")
    hub._commit("card.put", "fleet", "third", "Unique", "live", reflect.hubc.event_id())
    original = store.write_private

    def crash_on_metadata(path, data):
        if path.name == "meta.json" and path.parent.name == store.REPLICA:
            raise OSError("simulated interruption before the metadata replace")
        original(path, data)

    monkeypatch.setattr(store, "write_private", crash_on_metadata)
    with pytest.raises(OSError, match="simulated interruption"):
        _bounded_pull(target)
    monkeypatch.setattr(store, "write_private", original)
    root = store.root(target) / store.REPLICA
    assert len(list(root.glob("heads-*.json"))) == 2  # the new snapshot landed, unnamed
    previous = _bounded_load(target)
    _assert_consistent(previous)
    assert third not in previous.heads
    report = _bounded_pull(target)
    assert report["warnings"] == [] and report["rebuilt"] is False
    live = _bounded_load(target)
    _assert_consistent(live)
    assert third in live.heads
    assert [path.name for path in root.glob("heads-*.json")] == [f"heads-{live.meta['heads_sha256']}.json"]


def test_save_interrupted_before_cleanup_reads_the_new_pair(setup, monkeypatch):
    target, hub, _ = setup
    _bounded_pull(target)
    third = store.Replica.key("fleet", "third")
    hub._commit("card.put", "fleet", "third", "Unique", "live", reflect.hubc.event_id())
    original = store.Path.unlink

    def keep_snapshots(path, *args, **kwargs):
        if path.name.startswith("heads-"):
            raise OSError("simulated interruption before the stale snapshot is removed")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(store.Path, "unlink", keep_snapshots)
    _bounded_pull(target)
    monkeypatch.setattr(store.Path, "unlink", original)
    root = store.root(target) / store.REPLICA
    assert len(list(root.glob("heads-*.json"))) == 2
    live = _bounded_load(target)
    _assert_consistent(live)
    assert third in live.heads
    hub._commit("card.put", "fleet", "fourth", "Other", "live", reflect.hubc.event_id())
    _bounded_pull(target)
    live = _bounded_load(target)
    _assert_consistent(live)
    assert [path.name for path in root.glob("heads-*.json")] == [f"heads-{live.meta['heads_sha256']}.json"]


@pytest.mark.parametrize("bounded", [True, False])
def test_reader_retries_when_a_save_replaces_its_snapshot(tmp_path, monkeypatch, bounded):
    from brigade import memory_hub_replica_limits as limits

    root = tmp_path / "replica"
    first = store.Replica(root)
    first.meta = {"seq": 1}
    first.heads = {"fleet\na": {"scope": "fleet", "text": "a"}}
    first.save()
    original = store.read_json
    raced: list[bool] = []

    def racing(path, **kwargs):
        value = original(path, **kwargs)
        if path.name == "meta.json" and not raced:
            # Another run saves between this reader's metadata and snapshot reads.
            raced.append(True)
            writer = store.Replica(root)
            writer.heads["fleet\nb"] = {"scope": "fleet", "text": "b"}
            writer.save()
        return value

    monkeypatch.setattr(store, "read_json", racing)
    with limits.bounded(**BOUNDS) if bounded else contextlib.nullcontext():
        reader = store.Replica(root)
    assert raced and set(reader.heads) == {"fleet\na", "fleet\nb"}
    assert reader.meta["cards"] == 2


def test_legacy_snapshot_loads_and_moves_to_a_named_generation(tmp_path):
    from brigade import memory_hub_replica_limits as limits

    root = tmp_path / "replica"
    root.mkdir(mode=0o700)
    heads = {"fleet\na": {"scope": "fleet", "text": "a"}}
    store.write_json(root / "heads.json", heads)
    store.write_json(root / "meta.json", {"seq": 1, "cards": 1})
    with limits.bounded(**BOUNDS):
        legacy = store.Replica(root)
    assert legacy.heads == heads
    legacy.save()
    assert not (root / "heads.json").exists()
    with limits.bounded(**BOUNDS):
        moved = store.Replica(root)
    assert moved.heads == heads
    _assert_consistent(moved)


def test_snapshot_name_from_metadata_is_never_a_path(tmp_path):
    with pytest.raises(store.StoreError, match="invalid replica snapshot digest"):
        store.heads_path(tmp_path, {"heads_sha256": "../" + "a" * 61})


def _lose_snapshot(target):
    settings = reflect.hubc.load_settings(target)
    sync.pull(target, reflect.hubc.client_for(settings), settings)
    live = store.replica(target)
    store.heads_path(live.directory, live.meta).unlink()
    assert store.replica_snapshot_missing(target)


def test_reflect_rebuilds_a_replica_whose_snapshot_is_missing(setup, capsys):
    target, hub, _ = setup
    _lose_snapshot(target)
    # The first run rebuilds under the reflect bounds; its rebuild warning still
    # stops this run from proposing, as for any other rebuild.
    code, report = run(target, capsys, "run")
    assert code == 1 and report["error"] == "hub_reflect_replica_incomplete"
    assert writes(hub) == []
    live = _bounded_load(target)
    assert not live.snapshot_missing and len(live.heads) == 2
    _assert_consistent(live)
    # The next scheduled run proceeds without any unbounded pull in between.
    code, report = run(target, capsys, "run")
    assert code == 0 and len(report["proposals"]) == 1
    assert report["schedule"]["consecutive_failures"] == 0


def test_reflect_rebuild_of_a_missing_snapshot_keeps_the_card_bound(setup, capsys, monkeypatch):
    target, hub, _ = setup
    _lose_snapshot(target)
    monkeypatch.setattr(reflect, "MAX_SCOPE_CARDS", 1)
    code, report = run(target, capsys, "run")
    assert code == 1 and report["error"] == "hub_reflect_scope_limit"
    assert writes(hub) == []
    assert not store.replica(target).exists


def test_reflect_rebuild_of_a_missing_snapshot_keeps_the_byte_bound(setup, capsys, monkeypatch):
    target, hub, _ = setup
    _lose_snapshot(target)
    monkeypatch.setattr(reflect, "MAX_REPLICA_BYTES", 64)
    code, report = run(target, capsys, "run")
    assert code == 1 and report["error"] == "hub_reflect_replica_limit"
    assert writes(hub) == []
