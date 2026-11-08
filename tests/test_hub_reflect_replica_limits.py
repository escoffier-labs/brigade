"""Replica count and metadata limits precede full snapshot allocation."""

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
    (root / "heads.json").write_bytes(raw)
    meta = json.loads((root / "meta.json").read_text())
    if declared is None:
        meta.pop("cards", None)
    else:
        meta["cards"] = declared
    meta["heads_sha256"] = hashlib.sha256(raw).hexdigest()
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
    assert not (replica.directory / "heads.json").exists()


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


@pytest.mark.parametrize("field,bad", [("cards", 1), ("heads_sha256", "f" * 64)])
def test_saved_count_and_digest_are_verified_before_decoding(tmp_path, monkeypatch, field, bad):
    from brigade import memory_hub_replica_limits as limits

    root = tmp_path / "replica"
    replica = store.Replica(root)
    replica.heads = {f"fleet\ncard-{i}": {"scope": "fleet", "text": "x"} for i in range(2)}
    replica.save()
    meta = store.read_json(root / "meta.json")
    assert meta["cards"] == 2
    assert meta["heads_sha256"] == hashlib.sha256((root / "heads.json").read_bytes()).hexdigest()
    meta[field] = bad
    store.write_json(root / "meta.json", meta)
    decoded = []

    def forbid(raw):
        decoded.append(True)
        raise AssertionError("unverified snapshot reached dictionary decoding")

    monkeypatch.setattr(limits, "decode_heads", forbid)
    with limits.bounded(max_cards=3, max_bytes=4096):
        with pytest.raises(OSError):
            store.Replica(root)
    assert decoded == []
