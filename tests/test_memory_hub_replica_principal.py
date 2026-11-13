"""A connector that reads as its own principal never gets an answer from a replica another credential pulled."""

from __future__ import annotations

import pytest

from brigade import memory_hub_client as hubc
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

PINNED = "agent:cloud-agent"


@pytest.fixture
def pulled(hub, tmp_path):  # noqa: F811
    """A read workspace whose replica holds a fleet card, pulled as the host node (`node-1`)."""
    target = workspace(tmp_path, mode="read")
    _put(hub, "fleet", "x", "otter secret text")
    sync.pull(target, client(hub), settings(target))
    assert store.replica(target).meta["principal"] == "node-1"
    hub.mode = "down"
    return target


def _replica_ids(found):
    return {m["card_id"] for m in found["matches"] if m.get("source") == "replica"}


def test_search_without_a_pin_still_falls_back_to_the_replica(hub, pulled):  # noqa: F811
    found = hub_search.search_payload(pulled, "otter", settings=settings(pulled), transport=hub)
    assert _replica_ids(found) == {"x"}


def test_search_with_a_pin_for_another_principal_has_no_replica_fallback(hub, pulled, monkeypatch):  # noqa: F811
    monkeypatch.setenv(hubc.ENV_REPLICA_PRINCIPAL, PINNED)
    found = hub_search.search_payload(pulled, "otter", settings=settings(pulled), transport=hub)
    assert _replica_ids(found) == set()
    assert "otter secret text" not in str(found)


def test_search_with_a_pin_for_the_pulling_principal_still_serves_it(hub, pulled, monkeypatch):  # noqa: F811
    monkeypatch.setenv(hubc.ENV_REPLICA_PRINCIPAL, "node-1")
    found = hub_search.search_payload(pulled, "otter", settings=settings(pulled), transport=hub)
    assert _replica_ids(found) == {"x"}


def test_card_with_a_pin_for_another_principal_has_no_replica_fallback(hub, pulled, capsys, monkeypatch):  # noqa: F811
    code, payload, _ = _card_x(pulled, capsys)
    assert code == 0 and payload["source"] == "replica"
    monkeypatch.setenv(hubc.ENV_REPLICA_PRINCIPAL, PINNED)
    code, payload, output = _card_x(pulled, capsys)
    assert code == 1 and payload.get("hub") == "unavailable"
    assert "otter secret text" not in output


def test_card_with_a_pin_for_the_pulling_principal_still_serves_it(hub, pulled, capsys, monkeypatch):  # noqa: F811
    monkeypatch.setenv(hubc.ENV_REPLICA_PRINCIPAL, "node-1")
    code, payload, _ = _card_x(pulled, capsys)
    assert code == 0 and payload["source"] == "replica"
