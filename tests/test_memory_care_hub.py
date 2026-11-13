from brigade import memory_care_hub as bridge, memory_hub_client as hubc, memory_hub_store as store


class FakeHub:
    identity = "fake-hub"

    def __init__(self):
        self.calls = []
        self.unavailable = False

    def call(self, action, **fields):
        self.calls.append((action, fields))
        if self.unavailable:
            raise hubc.HubUnavailable("offline")
        if action == "status":
            return {"credential": "node", "log_epoch": "epoch"}
        return {"log_epoch": "epoch", "event": {"revision": 3}}


def test_retire_revive_and_pending(tmp_path, monkeypatch):
    fake = FakeHub()
    monkeypatch.setattr(hubc, "load_settings", lambda target: hubc.HubSettings(mode="sync", scope="fleet"))
    monkeypatch.setattr(hubc, "client_for", lambda settings: fake)
    with store.locked(tmp_path):
        store.save_published(
            tmp_path,
            {
                "fake-hub": {
                    "fleet": {
                        "old": {"revision": 1, "log_epoch": "epoch", "path": "memory/cards/old.md", "written": True}
                    }
                }
            },
        )
    fake.unavailable = True
    pending = bridge.retire(tmp_path, card_id="old", reason="ended", path="memory/cards/old.md")
    assert pending["status"] == "pending"
    fake.unavailable = False
    done = bridge.retire(tmp_path, card_id="old", reason="ended", path="memory/cards/old.md")
    assert done["status"] == "done"
    fields = [f for a, f in fake.calls if a == "retire"][-1]
    assert set(fields) == {"scope", "card_id", "base_revision", "base_epoch", "reason", "client_event_id"}
    assert bridge.revive(tmp_path, card_id="old", path="memory/cards/old.md", text="old")["status"] == "done"
    assert fake.calls[-1][0] == "put"
    assert fake.calls[-1][1]["base_revision"] == 3


def test_off_skips(tmp_path):
    assert bridge.retire(tmp_path, card_id="old", reason="ended", path="memory/cards/old.md")["status"] == "skipped"


def test_pending_retire_then_offline_undo_retries_in_order(tmp_path, monkeypatch):
    from brigade import memory_archive, memory_cmd, memory_care_journal as journal

    fake = FakeHub()
    monkeypatch.setattr(hubc, "load_settings", lambda target: hubc.HubSettings(mode="sync", scope="fleet"))
    monkeypatch.setattr(hubc, "client_for", lambda settings: fake)
    card = tmp_path / "memory/cards/old.md"
    card.parent.mkdir(parents=True)
    card.write_text("---\ncard_id: old\n---\nOriginal\n")
    with store.locked(tmp_path):
        store.save_published(
            tmp_path,
            {
                "fake-hub": {
                    "fleet": {
                        "old": {"revision": 1, "log_epoch": "epoch", "path": "memory/cards/old.md", "written": True}
                    }
                }
            },
        )
    fake.unavailable = True
    d = memory_archive.archive_card(
        tmp_path, "memory/cards/old.md", reason="ended", config=memory_cmd.MemoryCareConfig()
    )
    undo = memory_archive.restore(tmp_path, d)
    assert undo.hub["status"] == "pending"
    fake.unavailable = False
    bridge.retry_pending(tmp_path)
    assert [a for a, _ in fake.calls if a != "status"] == ["retire", "put"]
    assert journal.load(tmp_path, undo.decision_id).hub["status"] == "done"
