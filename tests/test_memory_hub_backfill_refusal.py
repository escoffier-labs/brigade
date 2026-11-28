"""Refused scopes must replay their dropped heads after a grant is restored."""

from contextlib import nullcontext

import pytest

from brigade import memory_hub_client as hubc
from brigade import memory_hub_store as store
from brigade import memory_hub_sync as sync
from tests._home import set_home
from tests.test_memory_hub_client import (
    GRANT_MISSING,
    _put,
    _put_on,
    client,
    settings,
    workspace,
)


pytest_plugins = ["tests.test_memory_hub_client"]


@pytest.mark.parametrize("bounded", [False, True])
@pytest.mark.parametrize("interrupt", [False, True])
def test_restored_scope_replays_dropped_heads(hub, tmp_path, monkeypatch, bounded, interrupt):
    set_home(monkeypatch, tmp_path / "isolated")
    target = workspace(tmp_path / "reader", mode="read")
    scope = "repo:test/app"
    _put(hub, scope, "old", "an unchanged card")
    _put(hub, scope, "updated", "before refusal")
    _put(hub, "fleet", "kept", "a readable card")
    sync.pull(target, client(hub), settings(target))
    _put_on(hub, "updated", "after refusal", scope=scope)
    original_card = hub.do_card

    def refuse_scope(body):
        if body.get("scope") == scope:
            if interrupt:
                hub.mode = "down"
            return GRANT_MISSING
        return original_card(body)

    hub.do_card = refuse_scope
    bounds = store.limits.bounded(max_cards=10, max_bytes=1024 * 1024) if bounded else nullcontext()
    with bounds:
        if interrupt:
            with pytest.raises(hubc.HubUnavailable):
                sync.pull(target, client(hub), settings(target))
        else:
            sync.pull(target, client(hub), settings(target))
    refused = store.replica(target)
    assert {head["card_id"] for head in refused.heads.values()} == {"kept"}
    assert scope not in refused.meta["read_scopes"]
    assert scope not in refused.meta["backfill_watermarks"]
    assert "fleet" in refused.meta["backfill_watermarks"]

    hub.mode = "up"
    hub.do_card = original_card
    hub.calls.clear()
    sync.pull(target, client(hub), settings(target))
    assert [call["after_seq"] for call in hub.calls if call["action"] == "log"][0] == 0
    restored = store.replica(target)
    assert {head["card_id"] for head in restored.heads.values()} == {"old", "updated", "kept"}
    assert restored.heads[store.Replica.key(scope, "old")]["text"] == "an unchanged card\n"
    assert restored.heads[store.Replica.key(scope, "updated")]["text"] == "after refusal\n"
    assert restored.meta["backfill_watermarks"][scope] == hub.seq
