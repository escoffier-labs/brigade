"""Hub memory client (S14) against an in-process fake of `POST /memory`."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from brigade import cli, memory_cmd
from brigade import memory_hub_client as hubc
from brigade import memory_hub_search as hub_search
from brigade import memory_hub_store as store
from brigade import memory_hub_sync as sync

# Built at runtime: a fixture, not a credential.
NODE_CREDENTIAL = "-".join(("fixture", "node", "credential", "0123456789"))
HUB_URL = "http://127.0.0.1:3774"
CHUNK = 16  # the fake hub's card chunk, small so the client follows next_offset


class FakeHub:
    """Enough of the S13 memory contract to drive the client: CAS, epochs, idempotency, chunks."""

    def __init__(self) -> None:
        self.epoch = hubc.event_id()
        self.seq = 0
        self.cards: dict[tuple[str, str], dict] = {}
        self.revisions: dict[tuple[str, str, int], dict] = {}
        self.events: list[dict] = []
        self.idempotency: dict[str, tuple[str, tuple[int, dict]]] = {}
        self.mode = "up"
        self.calls: list[dict] = []
        self.tokens: set[str] = set()
        self.import_reply: tuple[int, dict] = (403, {"error": "no import grant", "code": "memory_grant_missing"})
        self.put_reply: tuple[int, dict] | None = None
        self.drop_reply_once = False

    # -- transport -----------------------------------------------------------
    def __call__(self, url: str, token: str, body: dict, timeout: float):
        assert url == HUB_URL + "/memory"
        self.tokens.add(token)
        self.calls.append(copy.deepcopy(body))
        if self.mode == "down":
            raise ConnectionRefusedError("connection refused")
        if self.mode == "timeout":
            raise TimeoutError("deadline")
        if self.mode == "404":
            return 404, {"error": "not found"}
        if self.mode == "503":
            return 503, {"error": "hub busy", "reason": "memory-unavailable"}
        status, reply = getattr(self, "do_" + body["action"])(body)
        if self.drop_reply_once and body["action"] == "put":
            # The hub committed, then the connection died before the answer arrived.
            self.drop_reply_once = False
            raise ConnectionResetError("reset after commit")
        return status, reply

    def puts(self) -> list[dict]:
        return [c for c in self.calls if c["action"] == "put"]

    # -- actions -------------------------------------------------------------
    def do_status(self, body):
        live = sum(1 for c in self.cards.values() if c["state"] == "live")
        hub_time = datetime.now(timezone.utc).isoformat()
        return 200, {
            "cards": live,
            "revisions": len(self.revisions),
            "seq": self.seq,
            "indexed_seq": self.seq,
            "log_epoch": self.epoch,
            "hub_time": hub_time,
            "scan": {"available": False, "ruleset_version": None},
            "embedders": [],
            "default_mode": "lexical",
        }

    def _commit(self, kind, scope, card_id, text, state, cid):
        self.seq += 1
        normalized = hubc.normalize_text(text)
        digest = hashlib.sha256(normalized.encode()).hexdigest()
        head = {"revision": self.seq, "text": normalized, "hash": digest, "state": state, "trust": "agent-unreviewed"}
        self.cards[(scope, card_id)] = head
        self.revisions[(scope, card_id, self.seq)] = dict(head)
        self.events.append(
            {
                "seq": self.seq,
                "kind": kind,
                "scope": scope,
                "card_id": card_id,
                "revision": self.seq,
                "client_event_id": cid,
            }
        )
        event = {
            "seq": self.seq,
            "revision": self.seq,
            "content_hash": digest,
            "trust": "agent-unreviewed",
            "state": state,
        }
        return {"event": event, "log_epoch": self.epoch}

    def do_put(self, body, kind="card.put"):
        if self.put_reply is not None:
            return self.put_reply
        cid = body["client_event_id"]
        digest = json.dumps(body, sort_keys=True)
        if cid in self.idempotency:
            stored_digest, reply = self.idempotency[cid]
            if stored_digest != digest:
                return 409, {"error": "conflict", "code": "memory_client_event_conflict"}
            return reply
        stamp = hubc.event_id_ms(cid)
        now = int(datetime.now(timezone.utc).timestamp() * 1000)
        if stamp is None or now - stamp > 29 * 86400000:
            return 409, {"error": "expired", "code": "memory_client_event_expired"}
        key = (body["scope"], body["card_id"])
        head = self.cards.get(key)
        base = body.get("base_revision")
        if base is not None and body.get("base_epoch") != self.epoch:
            return 409, {"error": "epoch", "code": "memory_epoch_changed", "log_epoch": self.epoch}
        if (base is None and head is not None) or (base is not None and (head is None or head["revision"] != base)):
            reply = {"error": "stale", "code": "memory_base_mismatch"}
            if head:
                reply.update(current_revision=head["revision"], current_hash=head["hash"])
            return 409, reply
        reply = (201, self._commit(kind, body["scope"], body["card_id"], body["text"], "live", cid))
        self.idempotency[cid] = (digest, reply)
        return reply

    def do_import(self, body):
        return self.import_reply

    def do_card(self, body):
        scope = body.get("scope")
        if scope is None:
            scopes = [s for (s, c) in self.cards if c == body["card_id"]]
            if len(scopes) != 1:
                return 404, {"error": "card not found", "code": "memory_card_not_found"}
            scope = scopes[0]
        if body.get("revision") is not None:
            head = self.revisions.get((scope, body["card_id"], body["revision"]))
        else:
            head = self.cards.get((scope, body["card_id"]))
        if head is None:
            return 404, {"error": "card not found", "code": "memory_card_not_found"}
        offset = body.get("offset", 0)
        text = head["text"]
        chunk = text[offset : offset + CHUNK]
        card = {
            "scope": scope,
            "card_id": body["card_id"],
            "revision": head["revision"],
            "content_hash": head["hash"],
            "text": chunk,
            "next_offset": offset + CHUNK if offset + CHUNK < len(text) else None,
            "trust": head["trust"],
            "state": head["state"],
            "seq": head["revision"],
        }
        return 200, {"card": card, "log_epoch": self.epoch}

    def do_log(self, body):
        after = body.get("after_seq", 0)
        events = [e for e in self.events if e["seq"] > after][: body.get("limit", 100)]
        return 200, {"events": events, "next_seq": events[-1]["seq"] if events else after, "log_epoch": self.epoch}

    def do_search(self, body):
        word = body["query"].lower()
        matches = []
        for (scope, card_id), head in sorted(self.cards.items()):
            hits = head["text"].lower().count(word)
            if head["state"] == "live" and hits:
                matches.append(
                    {
                        "path": f"cards/{card_id}.md",
                        "scope": scope,
                        "card_id": card_id,
                        "card_aliases": [],
                        "revision": head["revision"],
                        "title": card_id,
                        "summary": "",
                        "tags": [],
                        "score": float(hits),
                        "content_hash": head["hash"],
                        "trust": head["trust"],
                        "ranks": {"lexical": 0},
                    }
                )
        matches.sort(key=lambda m: -m["score"])
        return 200, {
            "query": body["query"],
            "matches": matches[: body.get("limit", 10)],
            "match_count": len(matches),
            "retrieval": {"mode": "lexical", "degraded": False, "stale": False},
            "indexed_seq": self.seq,
        }

    # -- operator actions ----------------------------------------------------
    def snapshot(self) -> dict:
        return copy.deepcopy({k: getattr(self, k) for k in ("seq", "cards", "revisions", "events", "idempotency")})

    def restore(self, snap: dict) -> None:
        """Replace the file with an older copy: a new log_epoch, and seq may be reissued."""
        for key, value in copy.deepcopy(snap).items():
            setattr(self, key, value)
        self.epoch = hubc.event_id()


@pytest.fixture
def hub(monkeypatch, tmp_path):
    fake = FakeHub()
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", HUB_URL)
    monkeypatch.setenv("BRIGADE_FLEET_NODE_TOKEN", NODE_CREDENTIAL)
    monkeypatch.delenv("BRIGADE_FLEET_TOKEN", raising=False)
    monkeypatch.setattr(hubc, "urllib_transport", fake)
    return fake


def workspace(root: Path, mode: str | None = "read", scope: str | None = "repo:test/app", **cards: str) -> Path:
    (root / "memory" / "cards").mkdir(parents=True, exist_ok=True)
    if mode is not None:
        (root / ".brigade").mkdir(exist_ok=True)
        lines = ["[hub]", f'mode = "{mode}"'] + ([f'scope = "{scope}"'] if scope else [])
        (root / ".brigade" / "memory-care.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    for name, text in cards.items():
        (root / "memory" / "cards" / f"{name}.md").write_text(text, encoding="utf-8")
    return root


def settings(target: Path) -> hubc.HubSettings:
    return hubc.load_settings(target)


def client(fake: FakeHub) -> hubc.MemoryHubClient:
    return hubc.MemoryHubClient(HUB_URL, NODE_CREDENTIAL, transport=fake)


def run(argv: list[str], capsys) -> tuple[int, str, str]:
    code = cli.main(argv)
    out, err = capsys.readouterr()
    return code, out, err


# --- ids, hashes, config ----------------------------------------------------------


def test_event_ids_are_time_ordered_crockford_and_round_trip():
    first, second = hubc.event_id(1_000), hubc.event_id(2_000)
    assert len(first) == 26 and first < second
    assert hubc.event_id_ms(first) == 1_000
    assert hubc.event_id_ms("not-an-id") is None


def test_content_hash_matches_the_hub_normalizer():
    assert hubc.normalize_text("a\r\nb\r\n\n\n") == "a\nb\n"
    assert hubc.content_hash("x") == hashlib.sha256(b"x\n").hexdigest()


def test_config_errors_are_specific(tmp_path):
    target = workspace(tmp_path, mode="sync", scope=None)
    with pytest.raises(hubc.HubConfigError, match="needs a scope"):
        hubc.load_settings(target, fleet={})
    (target / ".brigade" / "memory-care.toml").write_text('[hub]\nmode = "on"\n', encoding="utf-8")
    with pytest.raises(hubc.HubConfigError, match="mode must be one of"):
        hubc.load_settings(target, fleet={})
    (target / ".brigade" / "memory-care.toml").write_text('[hub]\nmode = "read"\nscope = "nowhere"\n', encoding="utf-8")
    with pytest.raises(hubc.HubConfigError, match="scope must be"):
        hubc.load_settings(target, fleet={})


def test_the_credential_never_appears_in_output(hub, tmp_path, capsys):
    target = workspace(tmp_path)
    code, out, err = run(["memory", "hub", "status", "--target", str(target), "--json"], capsys)
    assert code == 0 and NODE_CREDENTIAL not in out + err
    assert json.loads(out)["hub"]["available"] is True
    assert NODE_CREDENTIAL not in repr(client(hub))
    assert hub.tokens == {NODE_CREDENTIAL}


# --- push and pull -----------------------------------------------------------------


def test_first_sync_prints_the_count_and_needs_yes(hub, tmp_path, capsys):
    target = workspace(
        tmp_path, mode="sync", one="# One\nalpha\n", two="# Two\nbeta\n", secret="---\nvisibility: private\n---\nno\n"
    )
    code, out, _ = run(["memory", "hub", "push", "--target", str(target)], capsys)
    assert code == 1
    assert "first publish to repo:test/app would publish 2 card(s)" in out and "--yes" in out
    assert hub.puts() == []
    code, out, _ = run(["memory", "hub", "push", "--target", str(target), "--json", "--yes"], capsys)
    report = json.loads(out)
    assert code == 0 and {p["card_id"] for p in report["published"]} == {"one", "two"}
    assert {"path": "memory/cards/secret.md", "reason": "visibility-private"} in report["skipped"]
    # A second push needs no confirmation and finds nothing new.
    code, out, _ = run(["memory", "hub", "push", "--target", str(target), "--json"], capsys)
    assert code == 0 and json.loads(out)["published"] == []
    assert len(hub.puts()) == 2


def test_put_and_pull_round_trip_with_chunked_cards(hub, tmp_path):
    writer = workspace(tmp_path / "w", mode="sync", note="# Note\n" + "long text " * 10)
    sync.push(writer, client(hub), settings(writer), yes=True)
    (writer / "memory" / "cards" / "note.md").write_text("# Note\nedited\n", encoding="utf-8")
    report = sync.push(writer, client(hub), settings(writer))
    assert [p["card_id"] for p in report["published"]] == ["note"]
    assert hub.puts()[-1]["base_revision"] == 1 and hub.puts()[-1]["base_epoch"] == hub.epoch

    reader = workspace(tmp_path / "r", mode="read")
    pulled = sync.pull(reader, client(hub), settings(reader))
    assert pulled["cards"] == 1 and pulled["seq"] == hub.seq
    replica = store.replica(reader)
    head = replica.heads[store.Replica.key("repo:test/app", "note")]
    assert head["text"] == "# Note\nedited\n" and head["revision"] == 2
    # Incremental: nothing new, nothing refetched.
    before = len(hub.calls)
    assert sync.pull(reader, client(hub), settings(reader))["applied"] == 0
    assert [c["action"] for c in hub.calls[before:]] == ["status", "log"]


def test_replica_and_spool_are_private(hub, tmp_path):
    target = workspace(tmp_path, mode="sync", one="# One\n")
    hub.mode = "down"
    sync.push(target, client(hub), settings(target), yes=True)
    spool = store.root(target) / store.SPOOL
    entries = list(spool.glob("*.json"))
    assert len(entries) == 1
    assert stat.S_IMODE(os.stat(spool).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(entries[0]).st_mode) == 0o600
    ignore = (store.root(target) / ".gitignore").read_text(encoding="utf-8")
    assert "/hub-spool/" in ignore and "/hub-replica/" in ignore


# --- spool and replay ------------------------------------------------------------------


def test_spool_replays_after_an_outage_without_duplicates(hub, tmp_path):
    target = workspace(tmp_path, mode="sync", one="# One\n", two="# Two\n")
    hub.mode = "down"
    report = sync.push(target, client(hub), settings(target), yes=True)
    assert len(report["spooled"]) == 2 and report["published"] == []
    assert any("spooled" in w for w in report["warnings"])
    # A second push while down does not spool the same cards again.
    sync.push(target, client(hub), settings(target))
    assert store.spool_count(target) == 2
    hub.mode = "up"
    # The first replayed put commits but its answer is lost: it stays spooled.
    hub.drop_reply_once = True
    report = sync.push(target, client(hub), settings(target))
    assert store.spool_count(target) == 2 and report["published"] == []
    report = sync.push(target, client(hub), settings(target))
    assert {p["card_id"] for p in report["published"]} == {"one", "two"}
    assert store.spool_count(target) == 0
    # Three put requests reached the hub for two cards, and only two events were appended.
    ids = [c["client_event_id"] for c in hub.puts()]
    assert len(ids) == 3 and len(set(ids)) == 2
    assert [e["card_id"] for e in hub.events] == ["one", "two"]


def test_conflicting_offline_edit_becomes_a_proposal(hub, tmp_path):
    target = workspace(tmp_path, mode="sync", one="# One\nfirst\n")
    sync.push(target, client(hub), settings(target), yes=True)
    # Someone else edits the hub card; this workspace edits offline.
    other = client(hub).call(
        "put",
        scope="repo:test/app",
        card_id="one",
        base_revision=1,
        base_epoch=hub.epoch,
        text="theirs",
        client_event_id=hubc.event_id(),
    )
    hub.mode = "down"
    (target / "memory" / "cards" / "one.md").write_text("# One\nmine\n", encoding="utf-8")
    sync.push(target, client(hub), settings(target))
    hub.mode = "up"
    report = sync.push(target, client(hub), settings(target))
    assert report["published"] == [] and store.spool_count(target) == 0
    [proposal] = list(store.proposals(target))
    assert proposal["reason"] == "memory_base_mismatch"
    assert proposal["entry"]["text"] == "# One\nmine\n"
    assert proposal["hub"]["current_revision"] == other["event"]["revision"]
    assert hub.cards[("repo:test/app", "one")]["text"] == "theirs\n"
    # The card stays out of later pushes while its proposal is open.
    assert sync.push(target, client(hub), settings(target))["published"] == []


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        ("old", "spool-entry-expired"),
        ("epoch", "memory_epoch_changed"),
        ("expired", "memory_client_event_expired"),
    ],
)
def test_spool_entries_the_hub_cannot_take_become_proposals(hub, tmp_path, setup, reason):
    target = workspace(tmp_path, mode="sync", one="# One\n")
    entry = {
        "client_event_id": hubc.event_id(),
        "hub": hubc.hub_identity(HUB_URL),
        "action": "put",
        "scope": "repo:test/app",
        "card_id": "one",
        "base_revision": None,
        "text": "# One\n",
        "origin": "interactive",
        "path": "memory/cards/one.md",
    }
    if setup == "old":
        entry["client_event_id"] = hubc.event_id(hubc.event_id_ms(hubc.event_id()) - 29 * 86400000)
    if setup == "epoch":
        entry.update(base_revision=4, base_epoch=hubc.event_id(0))
    if setup == "expired":
        hub.put_reply = (409, {"error": "expired", "code": "memory_client_event_expired"})
    store.spool_append(target, entry)
    store.save_published(target, {hubc.hub_identity(HUB_URL): {"repo:test/app": {}}})
    report = sync.push(target, client(hub), settings(target))
    assert [p["reason"] for p in report["proposals"]] == [reason]
    assert store.spool_count(target) == 0
    assert all(c["client_event_id"] != entry["client_event_id"] for c in hub.puts()) or setup == "expired"


def test_a_404_with_a_replica_watermark_spools_instead_of_dropping(hub, tmp_path):
    target = workspace(tmp_path, mode="sync", one="# One\n")
    hub.mode = "404"
    report = sync.push(target, client(hub), settings(target), yes=True)
    assert report == {
        "ok": False,
        "scope": "repo:test/app",
        "hub_memory": False,
        "error": "the hub has no memory (404)",
    }
    assert store.spool_count(target) == 0
    hub.mode = "up"
    sync.pull(target, client(hub), settings(target))
    hub.mode = "404"
    report = sync.push(target, client(hub), settings(target), yes=True)
    assert [s["card_id"] for s in report["spooled"]] == ["one"]
    assert any("replica watermark" in w for w in report["warnings"])


def test_refusals_surface_their_code(hub, tmp_path, capsys):
    target = workspace(tmp_path, mode="sync", one="# One\n")
    code, out, _ = run(
        ["memory", "hub", "import", "--target", str(target), "--json", str(target / "memory/cards/one.md")], capsys
    )
    assert code == 1
    [result] = json.loads(out)["results"]
    assert (result["status"], result["code"], result["http"]) == ("refused", "memory_grant_missing", 403)
    hub.put_reply = (403, {"error": "no write grant", "code": "memory_grant_missing"})
    report = sync.push(target, client(hub), settings(target), yes=True)
    assert report["refused"] == [{"card_id": "one", "status": 403, "code": "memory_grant_missing"}]
    assert [p["reason"] for p in report["proposals"]] == ["memory_grant_missing"]
    code, out, _ = run(["memory", "hub", "card", "--target", str(target), "--json", "absent"], capsys)
    assert code == 1 and json.loads(out)["code"] == "memory_card_not_found"


# --- epochs ------------------------------------------------------------------------------


def test_a_changed_log_epoch_discards_the_old_replica(hub, tmp_path):
    reader = workspace(tmp_path, mode="read", local="# Local\nzebra local\n")
    writer = client(hub)
    writer.call(
        "put", scope="fleet", card_id="kept", base_revision=None, text="zebra kept", client_event_id=hubc.event_id()
    )
    backup = hub.snapshot()
    writer.call(
        "put", scope="fleet", card_id="late", base_revision=None, text="zebra late", client_event_id=hubc.event_id()
    )
    sync.pull(reader, client(hub), settings(reader))
    assert {h["card_id"] for h in store.replica(reader).heads.values()} == {"kept", "late"}
    hub.restore(backup)
    # The rebuild starts, then the hub goes away mid-pull.
    calls = 0
    original = hub.do_log

    def failing_log(body):
        nonlocal calls
        calls += 1
        raise ConnectionRefusedError("gone mid-pull")

    hub.do_log = failing_log
    with pytest.raises(hubc.HubUnavailable):
        sync.pull(reader, client(hub), settings(reader))
    assert calls == 1 and not store.replica(reader).exists
    hub.mode = "down"
    during = hub_search.search_payload(reader, "zebra", settings=settings(reader))
    assert during["retrieval"]["degraded"] and during["retrieval"]["fallback"] == ["local"]
    assert {m["card_id"] for m in during["matches"]} == {"local"}
    hub.mode, hub.do_log = "up", original
    report = sync.pull(reader, client(hub), settings(reader))
    assert report["cards"] == 1
    assert {h["card_id"] for h in store.replica(reader).heads.values()} == {"kept"}
    hub.mode = "down"
    after = hub_search.search_payload(reader, "zebra", settings=settings(reader))
    assert {m["card_id"] for m in after["matches"]} == {"local", "kept"}


def test_an_epoch_change_during_a_pull_serves_nothing_from_the_old_epoch(hub, tmp_path):
    reader = workspace(tmp_path, mode="read")
    client(hub).call("put", scope="fleet", card_id="a", base_revision=None, text="a", client_event_id=hubc.event_id())
    sync.pull(reader, client(hub), settings(reader))
    assert store.replica(reader).exists
    client(hub).call("put", scope="fleet", card_id="b", base_revision=None, text="b", client_event_id=hubc.event_id())
    original = hub.do_log

    def restored_between_calls(body):
        status, reply = original(body)
        return status, {**reply, "log_epoch": hubc.event_id()}

    hub.do_log = restored_between_calls
    report = sync.pull(reader, client(hub), settings(reader))
    assert report["rebuilt"] and "run pull again" in report["warnings"][-1]
    assert not store.replica(reader).exists and not store.building(reader).exists


# --- search ------------------------------------------------------------------------------


def test_search_fuses_hub_and_local_and_deduplicates(hub, tmp_path):
    target = workspace(tmp_path, mode="read", same="# Same\nkoala\n", mine="# Mine\nkoala koala\n")
    writer = client(hub)
    for card_id, text in (("same", "# Same\nkoala\n"), ("theirs", "# Theirs\nkoala koala koala\n")):
        writer.call(
            "put", scope="fleet", card_id=card_id, base_revision=None, text=text, client_event_id=hubc.event_id()
        )
    payload = hub_search.search_payload(target, "koala", settings=settings(target))
    assert payload["retrieval"] == {"hub": "ok", "degraded": False, "effective": "lexical", "stale": False}
    sources = {m["card_id"]: (m["source"], sorted(m["ranks"])) for m in payload["matches"]}
    assert sources == {
        "same": ("hub", ["hub", "local"]),
        "theirs": ("hub", ["hub"]),
        "mine": ("local", ["local"]),
    }
    assert payload["match_count"] == 3
    search = [c for c in hub.calls if c["action"] == "search"]
    assert search == [{"action": "search", "query": "koala", "limit": 20, "query_source": "explicit"}]


@pytest.mark.parametrize("mode", ["down", "timeout", "404", "503"])
def test_search_falls_back_to_local_cards_and_the_replica(hub, tmp_path, mode):
    target = workspace(tmp_path, mode="read", mine="# Mine\nwombat\n")
    client(hub).call(
        "put", scope="fleet", card_id="theirs", base_revision=None, text="wombat", client_event_id=hubc.event_id()
    )
    sync.pull(target, client(hub), settings(target))
    hub.mode = mode
    payload = hub_search.search_payload(target, "wombat", settings=settings(target))
    reason = {"down": "unreachable", "timeout": "timeout", "404": "no-hub-memory", "503": "memory-unavailable"}[mode]
    assert payload["retrieval"] == {
        "hub": "unavailable",
        "degraded": True,
        "reason": reason,
        "fallback": ["local", "replica"],
    }
    assert {(m["card_id"], m["source"]) for m in payload["matches"]} == {("mine", "local"), ("theirs", "replica")}
    assert payload["warning"].startswith(f"hub search unavailable ({reason})")


def test_search_cli_labels_a_degraded_answer(hub, tmp_path, capsys):
    target = workspace(tmp_path, mode="read", mine="# Mine\nwombat\n")
    hub.mode = "down"
    code, out, _ = run(["memory", "search", "--target", str(target), "wombat"], capsys)
    assert code == 0
    assert out.splitlines()[1] == "warning: hub search unavailable (unreachable); answered from local"
    assert "[local]" in out


def test_off_mode_output_is_byte_identical(hub, tmp_path, capsys):
    cards = {"alpha": "---\ntitle: Alpha\n---\nhub memory words\n", "beta": "# Beta\nwords\n"}
    plain = workspace(tmp_path / "plain", mode=None, **cards)
    off = workspace(tmp_path / "off", mode="off", **cards)
    outputs = []
    for target in (plain, off):
        for extra in ([], ["--json"]):
            code, out, err = run(["memory", "search", "--target", str(target), *extra, "words"], capsys)
            assert code == 0 and err == ""
            outputs.append(out.replace(str(target.resolve()), "<target>"))
    assert outputs[0] == outputs[2] and outputs[1] == outputs[3]
    assert hub.calls == []
    memory_cmd.search(target=plain, query="words")
    assert capsys.readouterr().out.replace(str(plain.resolve()), "<target>") == outputs[0]


def test_a_prompt_query_is_never_written_on_the_client(hub, tmp_path):
    sentinel = "sentinelprompt7f3a91"
    target = workspace(tmp_path, mode="sync", one="# One\nnothing here\n")
    sync.push(target, client(hub), settings(target), yes=True)
    sync.pull(target, client(hub), settings(target))
    hub_search.search_payload(target, f"what about {sentinel}", query_source="prompt", settings=settings(target))
    hub.mode = "down"
    hub_search.search_payload(target, f"what about {sentinel}", query_source="prompt", settings=settings(target))
    for path in target.rglob("*"):
        if path.is_file():
            assert sentinel.encode() not in path.read_bytes(), path
    assert [c["query_source"] for c in hub.calls if c["action"] == "search"] == ["prompt", "prompt"]


# --- Dot and Duet --------------------------------------------------------------------------


class _Runner:
    def __init__(self, reply: dict) -> None:
        self.reply, self.argv = reply, []

    def run(self, argv, **kwargs):
        self.argv.append(argv)
        return SimpleNamespace(timed_out=False, overflow=False, stdout=json.dumps(self.reply).encode())


def _dot_memory(target: Path, reply: dict):
    from brigade.dot.memory_tools import MemoryTools
    from brigade.dot.profile import DUET

    tools = MemoryTools.__new__(MemoryTools)
    tools.workspace, tools.cards, tools.profile = target, target / "memory" / "cards", DUET
    tools.commands = _Runner(reply)
    tools.runtime = SimpleNamespace(paths=SimpleNamespace(bin=SimpleNamespace(brigade="brigade")))
    tools.environment = {}
    return tools


def test_dot_memory_card_reads_a_hub_only_card(tmp_path):
    target = workspace(tmp_path, mode="read")
    for directory in (target, target / "memory", target / "memory" / "cards"):
        directory.chmod(0o755)
    card = {"scope": "fleet", "card_id": "theirs", "revision": 3, "content_hash": "ab" * 32, "text": "hub text\n"}
    tools = _dot_memory(target, {"ok": True, "card": card})
    got = tools.memory_card("theirs")
    assert (got["source"], got["content"], got["revision"], got["next_offset"]) == ("hub", "hub text\n", 3, None)
    assert tools.commands.argv[0][:4] == ["brigade", "memory", "hub", "card"]
    (target / ".brigade" / "memory-care.toml").write_text('[hub]\nmode = "off"\n', encoding="utf-8")
    tools.commands.argv.clear()
    assert tools.memory_card("theirs") == {"error": "card_not_found", "name": "theirs"}
    assert tools.commands.argv == []


def test_dot_memory_search_keeps_hub_labels_only_when_hub_memory_answered(tmp_path):
    match = {"card_id": "a", "title": "A", "path": "cards/a.md", "score": 0.1, "summary": "", "tags": []}
    hub_reply = {
        "match_count": 1,
        "matches": [{**match, "source": "replica", "scope": "fleet", "revision": 2, "trust": "agent-unreviewed"}],
        "retrieval": {"hub": "unavailable", "degraded": True, "reason": "timeout", "fallback": ["local", "replica"]},
        "warning": "hub search unavailable (timeout); answered from local and replica",
    }
    got = _dot_memory(tmp_path, hub_reply).memory_search("a")
    assert got["matches"][0]["source"] == "replica" and got["retrieval"]["degraded"] is True
    assert got["warning"].startswith("hub search unavailable")
    plain = _dot_memory(tmp_path, {"match_count": 1, "matches": [match]}).memory_search("a")
    assert "source" not in plain["matches"][0] and "retrieval" not in plain
