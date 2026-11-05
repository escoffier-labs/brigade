"""Hub memory client (S14) against an in-process fake of `POST /memory`."""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import io
import json
import os
import stat
import threading
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from brigade import cli, memory_cmd
from brigade import memory_hub_client as hubc
from brigade import memory_hub_search as hub_search
from brigade import memory_hub_store as store
from brigade import memory_hub_sync as sync

# Built at runtime: fixtures, not credentials.
NODE_CREDENTIAL = "-".join(("fixture", "node", "credential", "0123456789"))
OTHER_CREDENTIAL = "-".join(("fixture", "other", "credential", "0123456789"))
ADMIN_CREDENTIAL = "-".join(("fixture", "admin", "credential", "0123456789"))
HUB_URL = "http://127.0.0.1:3774"
CHUNK = 16  # the fake hub's card chunk, small so the client follows next_offset
POSIX_DOT = pytest.mark.skipif(
    os.name != "posix",
    reason="Dot reads cards through POSIX directory descriptors; trusted_directory refuses on Windows",
)


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
        # Credentials: token to principal, and what each principal may read.
        # None means a node's defaults: fleet, every repo: scope and its own principal: scope.
        self.principals = {NODE_CREDENTIAL: "node-1", OTHER_CREDENTIAL: "node-2", ADMIN_CREDENTIAL: "hub:admin"}
        self.grants: dict[str, set[str] | None] = {"node-1": None, "node-2": None, "hub:admin": {"*"}}
        self.who = ""
        self.status_reply: tuple[int, dict] | None = None
        self.search_reply: tuple[int, dict] | None = None
        self.on_status = None

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
        if self.mode.isdigit():
            return int(self.mode), {}
        if token not in self.principals:
            return 401, {"error": "unauthorized"}
        self.who = self.principals[token]
        if body["action"] in {"status", "search", "log", "card"} and not self.reads_anything():
            return 403, {"error": "no read grant", "code": "memory_grant_missing"}
        status, reply = getattr(self, "do_" + body["action"])(body)
        if self.drop_reply_once and body["action"] == "put":
            # The hub committed, then the connection died before the answer arrived.
            self.drop_reply_once = False
            raise ConnectionResetError("reset after commit")
        return status, reply

    def puts(self) -> list[dict]:
        return [c for c in self.calls if c["action"] == "put"]

    def readable(self, scope: str) -> bool:
        grants = self.grants.get(self.who)
        if grants is None:
            return scope == "fleet" or scope.startswith("repo:") or scope == f"principal:{self.who}"
        return "*" in grants or scope in grants

    def reads_anything(self) -> bool:
        grants = self.grants.get(self.who)
        return grants is None or bool(grants)

    # -- actions -------------------------------------------------------------
    def do_status(self, body):
        if self.on_status is not None:
            self.on_status()
        if self.status_reply is not None:
            return self.status_reply
        live = sum(1 for c in self.cards.values() if c["state"] == "live")
        hub_time = datetime.now(timezone.utc).isoformat()
        extra = {}
        if body.get("scopes") is not None:
            extra["readable_scopes"] = sorted({s for s in body["scopes"] if self.readable(s)})
        return 200, {
            **extra,
            "principal": self.who,
            "credential": "admin" if self.who == "hub:admin" else "node",
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
            scopes = [s for (s, c) in self.cards if c == body["card_id"] and self.readable(s)]
            if len(scopes) > 1:
                return 409, {"error": "ambiguous", "code": "memory_card_ambiguous"}
            if not scopes:
                return 404, {"error": "card not found", "code": "memory_card_not_found"}
            scope = scopes[0]
        if not self.readable(scope):
            return 403, {"error": "no read grant", "code": "memory_grant_missing"}
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
        page = [e for e in self.events if e["seq"] > after][: body.get("limit", 100)]
        events = [e for e in page if self.readable(e["scope"])]
        return 200, {"events": events, "next_seq": page[-1]["seq"] if page else after, "log_epoch": self.epoch}

    def do_search(self, body):
        if self.search_reply is not None:
            return self.search_reply
        requested = body.get("scopes")
        if requested is not None and not all(self.readable(s) for s in requested):
            return 403, {"error": "no read grant on a requested scope", "code": "memory_grant_missing"}
        word = body["query"].lower()
        matches = []
        for (scope, card_id), head in sorted(self.cards.items()):
            hits = head["text"].lower().count(word)
            wanted = scope in requested if requested is not None else self.readable(scope)
            if head["state"] == "live" and hits and wanted:
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


def workspace(
    root: Path,
    mode: str | None = "read",
    scope: str | None = "repo:test/app",
    read_scopes: tuple[str, ...] | None = ("fleet", "repo:test/app"),
    **cards: str,
) -> Path:
    (root / "memory" / "cards").mkdir(parents=True, exist_ok=True)
    if mode is not None:
        (root / ".brigade").mkdir(exist_ok=True)
        lines = ["[hub]", f'mode = "{mode}"'] + ([f'scope = "{scope}"'] if scope else [])
        if read_scopes is not None:
            lines.append("read_scopes = [" + ", ".join(f'"{s}"' for s in read_scopes) + "]")
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
    # Incremental: nothing new, nothing refetched. The last status call checks the epoch before the save.
    before = len(hub.calls)
    assert sync.pull(reader, client(hub), settings(reader))["applied"] == 0
    assert [c["action"] for c in hub.calls[before:]] == ["status", "log", "status"]


def test_replica_and_spool_are_private(hub, tmp_path):
    target = workspace(tmp_path, mode="sync", one="# One\n")
    hub.mode = "down"
    sync.push(target, client(hub), settings(target), yes=True)
    spool = store.root(target) / store.SPOOL
    entries = list(spool.glob("*/*.json"))
    assert len(entries) == 1
    # Everything under hub/, discarded replicas and temporary files included, is ignored.
    assert (store.root(target) / ".gitignore").read_text(encoding="utf-8") == "*\n"
    if os.name != "posix":
        pytest.skip("POSIX file modes; Windows reports 0o777 or 0o666 whatever chmod asked")
    for directory in (store.root(target), spool, entries[0].parent):
        assert stat.S_IMODE(os.stat(directory).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(entries[0]).st_mode) == 0o600


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


def test_a_spooled_write_is_withheld_once_its_card_turns_private(hub, tmp_path):
    target = workspace(tmp_path, mode="sync", one="# One\nconfidential draft\n")
    hub.mode = "down"
    report = sync.push(target, client(hub), settings(target), yes=True)
    assert [s["card_id"] for s in report["spooled"]] == ["one"]
    card = target / "memory" / "cards" / "one.md"
    card.write_text("---\nvisibility: private\n---\n# One\nconfidential draft\n", encoding="utf-8")
    hub.mode = "up"
    report = sync.push(target, client(hub), settings(target))
    assert hub.puts() == [] and report["published"] == []
    assert {"path": "memory/cards/one.md", "reason": "visibility-private"} in report["skipped"]
    assert [(p["card_id"], p["reason"]) for p in report["proposals"]] == [("one", "card-now-private")]
    assert store.spool_count(target) == 0
    (kept,) = list(store.proposals(target))
    assert kept["entry"]["text"] == "# One\nconfidential draft\n"
    # A later push still sends nothing for the private card.
    sync.push(target, client(hub), settings(target))
    assert hub.puts() == []


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
    assert search == [
        {
            "action": "search",
            "query": "koala",
            "limit": 20,
            "query_source": "explicit",
            "scopes": ["fleet", "repo:test/app"],
        }
    ]


@pytest.mark.parametrize("mode", ["down", "timeout", "404", "503", "307", "308", "408", "429"])
def test_search_falls_back_to_local_cards_and_the_replica(hub, tmp_path, mode):
    target = workspace(tmp_path, mode="read", mine="# Mine\nwombat\n")
    client(hub).call(
        "put", scope="fleet", card_id="theirs", base_revision=None, text="wombat", client_event_id=hubc.event_id()
    )
    sync.pull(target, client(hub), settings(target))
    hub.mode = mode
    payload = hub_search.search_payload(target, "wombat", settings=settings(target))
    reason = {"down": "unreachable", "timeout": "timeout", "404": "no-hub-memory", "503": "memory-unavailable"}.get(
        mode, f"http-{mode}"
    )
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
            # JSON escapes the backslashes of a Windows path.
            resolved = str(target.resolve())
            outputs.append(out.replace(json.dumps(resolved)[1:-1], "<target>").replace(resolved, "<target>"))
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


@POSIX_DOT
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


# --- review round 1: scopes, credentials and the replica's principal ------------------------


class _CliRunner:
    """Runs `brigade ...` in this process, as Dot's subprocess call would."""

    def __init__(self) -> None:
        self.argv: list[list[str]] = []

    def run(self, argv, **kwargs):
        self.argv.append(list(argv))
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            cli.main(list(argv[1:]))
        return SimpleNamespace(timed_out=False, overflow=False, stdout=out.getvalue().encode())


def _dot_cli(target: Path):
    for directory in (target, target / "memory", target / "memory" / "cards"):
        directory.chmod(0o755)
    tools = _dot_memory(target, {})
    tools.commands = _CliRunner()
    return tools


def _put(hub: FakeHub, scope: str, card_id: str, text: str, token: str = NODE_CREDENTIAL) -> dict:
    writer = hubc.MemoryHubClient(HUB_URL, token, transport=hub)
    return writer.call(
        "put", scope=scope, card_id=card_id, base_revision=None, text=text, client_event_id=hubc.event_id()
    )


def _card_ids(replica: store.Replica) -> set[str]:
    return {head["card_id"] for head in replica.heads.values()}


def test_dot_reads_only_the_configured_scope(hub, tmp_path):
    # This credential can read as much as an admin, private scopes included.
    hub.grants["node-1"] = {"*", "principal:alice"}
    target = workspace(tmp_path, mode="read", scope="repo:me/app", read_scopes=None)
    for scope, card_id in (("principal:alice", "alice-notes"), ("repo:me/app", "app-pay"), ("fleet", "fleet-pay")):
        _put(hub, scope, card_id, f"salary {card_id}")
    tools = _dot_cli(target)
    found = tools.memory_search("salary")
    assert [(m["card_id"], m["scope"], m["source"]) for m in found["matches"]] == [("app-pay", "repo:me/app", "hub")]
    assert [c["scopes"] for c in hub.calls if c["action"] == "search"] == [["repo:me/app"]]
    for name, scope in (("alice-notes", "principal:alice"), ("fleet-pay", "fleet")):
        assert tools.memory_card(name, scope=scope) == {"error": "card_not_found", "name": name}
    assert tools.memory_card("app-pay", scope="repo:me/app")["content"] == "salary app-pay\n"
    if os.name == "posix":
        # A read without a scope opens the local cards directory first, which needs POSIX.
        for name in ("alice-notes", "fleet-pay"):
            assert tools.memory_card(name) == {"error": "card_not_found", "name": name}
        assert tools.memory_card("app-pay")["content"] == "salary app-pay\n"
    assert {c.get("scope") for c in hub.calls if c["action"] == "card"} == {"repo:me/app"}


def test_memory_never_sends_the_admin_token(hub, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("BRIGADE_FLEET_NODE_TOKEN")
    monkeypatch.setenv("BRIGADE_FLEET_TOKEN", ADMIN_CREDENTIAL)
    target = workspace(tmp_path, mode="sync", one="# One\nsalary\n")
    code, out, err = run(["memory", "search", "--target", str(target), "--json", "salary"], capsys)
    assert code == 0 and json.loads(out)["retrieval"]["reason"] == "no-node-credential"
    code, out, err = run(["memory", "hub", "status", "--target", str(target), "--json"], capsys)
    assert code == 0 and json.loads(out)["hub"] == {"available": False, "reason": "no-node-credential"}
    for command in (["push", "--yes"], ["pull"], ["card", "one"]):
        code, out, err = run(["memory", "hub", command[0], "--target", str(target), *command[1:]], capsys)
        assert code == 1 and "never uses the shared admin token" in out, command
        assert ADMIN_CREDENTIAL not in out + err
    assert hub.calls == [] and hub.tokens == set()


def test_a_replica_pulled_as_another_principal_is_never_served(hub, tmp_path, monkeypatch):
    hub.grants["node-2"] = {"repo:test/app"}
    target = workspace(tmp_path, mode="read", mine="# Mine\nnothing\n")
    _put(hub, "fleet", "secret", "otter secret")
    _put(hub, "repo:test/app", "shared", "otter shared")
    sync.pull(target, client(hub), settings(target))
    assert _card_ids(store.replica(target)) == {"secret", "shared"}
    assert store.replica(target).meta["principal"] == "node-1"
    monkeypatch.setenv("BRIGADE_FLEET_NODE_TOKEN", OTHER_CREDENTIAL)
    current = settings(target)
    report = sync.pull(target, hubc.client_for(current, transport=hub), current)
    assert report["rebuilt"] and any("another principal" in w for w in report["warnings"])
    assert _card_ids(store.replica(target)) == {"shared"}
    hub.mode = "down"
    found = hub_search.search_payload(target, "otter", settings=current, transport=hub)
    assert {(m["card_id"], m["source"]) for m in found["matches"]} == {("shared", "replica")}


def test_a_revoked_scope_grant_drops_its_replicated_heads(hub, tmp_path):
    target = workspace(tmp_path, mode="sync", mine="# Mine\nnothing\n")
    _put(hub, "fleet", "secret", "otter secret")
    _put(hub, "repo:test/app", "shared", "otter shared")
    sync.pull(target, client(hub), settings(target))
    hub.grants["node-1"] = {"repo:test/app"}
    report = sync.pull(target, client(hub), settings(target))
    assert report["dropped"] == 1 and _card_ids(store.replica(target)) == {"shared"}
    hub.mode = "down"
    found = hub_search.search_payload(target, "otter", settings=settings(target))
    assert {m["card_id"] for m in found["matches"]} == {"shared"}


def test_heads_dropped_for_a_revoked_grant_stay_dropped_when_the_pull_then_fails(hub, tmp_path):
    target = workspace(tmp_path, mode="read")
    _put(hub, "fleet", "secret", "otter secret")
    sync.pull(target, client(hub), settings(target))
    hub.grants["node-1"] = {"repo:test/app"}

    def gone(body):
        raise ConnectionRefusedError("gone mid-pull")

    hub.do_log = gone
    with pytest.raises(hubc.HubUnavailable):
        sync.pull(target, client(hub), settings(target))
    hub.mode = "down"
    found = hub_search.search_payload(target, "otter", settings=settings(target))
    assert found["matches"] == []


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_search_never_answers_from_the_replica(hub, tmp_path, status):
    target = workspace(tmp_path, mode="read", mine="# Mine\nwombat\n")
    _put(hub, "fleet", "theirs", "wombat")
    sync.pull(target, client(hub), settings(target))
    code = "memory_grant_missing" if status == 403 else "unauthorized"
    hub.search_reply = (status, {"error": "refused", "code": code} if status == 403 else {"error": "unauthorized"})
    payload = hub_search.search_payload(target, "wombat", settings=settings(target))
    assert payload["retrieval"] == {
        "hub": "refused",
        "degraded": True,
        "reason": code,
        "status": status,
        "fallback": ["local"],
    }
    assert {(m["card_id"], m["source"]) for m in payload["matches"]} == {("mine", "local")}
    assert payload["warning"] == f"hub search refused ({status} {code}); answered from local"


# --- review round 1: pull ------------------------------------------------------------------


def test_an_epoch_change_seen_in_a_card_answer_discards_the_pull(hub, tmp_path):
    reader = workspace(tmp_path, mode="read")
    _put(hub, "fleet", "a", "a")
    sync.pull(reader, client(hub), settings(reader))
    _put(hub, "fleet", "b", "b")
    original = hub.do_card

    def restored_before_the_card(body):
        status, reply = original(body)
        return status, {**reply, "log_epoch": hubc.event_id()}

    hub.do_card = restored_before_the_card
    report = sync.pull(reader, client(hub), settings(reader))
    assert report["rebuilt"] and "run pull again" in report["warnings"][-1]
    assert not store.replica(reader).exists and not store.building(reader).exists


def test_the_epoch_is_checked_again_before_the_save(hub, tmp_path):
    reader = workspace(tmp_path, mode="read")
    _put(hub, "fleet", "a", "a")
    seen = []

    def restore_after_the_first_status():
        seen.append(1)
        if len(seen) == 2:
            hub.epoch = hubc.event_id()

    hub.on_status = restore_after_the_first_status
    report = sync.pull(reader, client(hub), settings(reader))
    assert report["rebuilt"] and report["seq"] is None
    assert not store.replica(reader).exists and not store.building(reader).exists


def test_a_cold_rebuild_writes_heads_a_few_times(hub, tmp_path, monkeypatch):
    reader = workspace(tmp_path, mode="read")
    for number in range(12):
        _put(hub, "fleet", f"c{number}", f"card {number}")
    monkeypatch.setattr(sync, "LOG_PAGE", 1)
    writes: list[str] = []
    original = store.write_json

    def counting(path, value):
        writes.append(path.name)
        original(path, value)

    monkeypatch.setattr(store, "write_json", counting)
    sync.pull(reader, client(hub), settings(reader))
    assert len(store.replica(reader).heads) == 12
    # One empty write when the rebuild starts and one at the end, not one per page.
    assert writes.count("heads.json") == 2


def test_cards_left_out_for_the_cap_come_back_when_space_frees(hub, tmp_path):
    reader = workspace(tmp_path, mode="read", local="# Local\nheron\n")
    _put(hub, "fleet", "big", "heron " + "x" * 600)
    _put(hub, "fleet", "small", "heron small")
    sync.pull(reader, client(hub), settings(reader))
    sizes = {head["card_id"]: store.head_size(key, head) for key, head in store.replica(reader).heads.items()}
    store.discard_replica(reader)
    capped = dataclasses.replace(settings(reader), replica_max_bytes=sizes["big"] + sizes["small"] - 1)
    report = sync.pull(reader, client(hub), capped)
    assert _card_ids(store.replica(reader)) == {"big"}
    assert store.replica(reader).meta["full"] is True and "1 card(s) are not replicated yet" in report["warnings"][-1]
    hub.mode = "down"
    found = hub_search.search_payload(reader, "heron", settings=capped)
    assert found["retrieval"]["replica_incomplete"] is True and "replica is incomplete" in found["warning"]
    hub.mode = "up"
    hub._commit("card.retire", "fleet", "big", "", "retired", hubc.event_id())
    report = sync.pull(reader, client(hub), capped)
    replica = store.replica(reader)
    assert _card_ids(replica) == {"small"} and replica.meta["full"] is False and replica.missing == {}
    assert not any("cap" in w for w in report["warnings"])


# --- review round 1: push --------------------------------------------------------------------


def test_concurrent_pushes_after_an_edit_put_once(hub, tmp_path):
    target = workspace(tmp_path, mode="sync", one="# One\nfirst\n")
    sync.push(target, client(hub), settings(target), yes=True)
    (target / "memory" / "cards" / "one.md").write_text("# One\nsecond\n", encoding="utf-8")
    # Without the lock both runs would pass status together and both put.
    barrier = threading.Barrier(2, timeout=0.5)

    def meet():
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass

    hub.on_status = meet
    reports: list[dict] = []
    threads = [
        threading.Thread(target=lambda: reports.append(sync.push(target, client(hub), settings(target))))
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(hub.puts()) == 2  # the first publish, then exactly one put for the edit
    assert all(report["proposals"] == [] for report in reports)
    assert sorted(len(report["published"]) for report in reports) == [0, 1]
    assert list(store.proposals(target)) == []


def test_spool_entries_for_another_hub_neither_block_nor_count(hub, tmp_path, monkeypatch):
    target = workspace(tmp_path, mode="sync", one="# One\n")
    elsewhere = hubc.hub_identity("https://hub.example.test")
    entry = {
        "client_event_id": hubc.event_id(),
        "hub": elsewhere,
        "action": "put",
        "scope": "repo:test/app",
        "card_id": "one",
        "base_revision": None,
        "text": "# One\n",
        "origin": "interactive",
        "path": "memory/cards/one.md",
    }
    store.spool_append(target, entry)
    monkeypatch.setattr(store, "SPOOL_CAP", 1)
    hub.mode = "down"
    report = sync.push(target, client(hub), settings(target), yes=True)
    assert [s["card_id"] for s in report["spooled"]] == ["one"] and "kept_local" not in report
    hub.mode = "up"
    assert [p["card_id"] for p in sync.push(target, client(hub), settings(target))["published"]] == ["one"]
    assert store.spool_count(target, elsewhere) == 1 and store.spool_count(target) == 1


def test_dropping_or_resolving_a_proposal_unblocks_its_card(hub, tmp_path, capsys):
    target = workspace(tmp_path, mode="sync", one="# One\nfirst\n")
    card = target / "memory" / "cards" / "one.md"
    sync.push(target, client(hub), settings(target), yes=True)

    def conflict(theirs: str, mine: str) -> None:
        head = hub.cards[("repo:test/app", "one")]["revision"]
        client(hub).call(
            "put",
            scope="repo:test/app",
            card_id="one",
            base_revision=head,
            base_epoch=hub.epoch,
            text=theirs,
            client_event_id=hubc.event_id(),
        )
        card.write_text(mine, encoding="utf-8")
        assert [p["reason"] for p in sync.push(target, client(hub), settings(target))["proposals"]] == [
            "memory_base_mismatch"
        ]

    conflict("theirs\n", "# One\nmine\n")
    code, out, _ = run(["memory", "hub", "proposals", "list", "--target", str(target), "--json"], capsys)
    [row] = json.loads(out)["proposals"]
    assert code == 0 and (row["card_id"], row["scope"]) == ("one", "repo:test/app")
    assert sync.push(target, client(hub), settings(target))["published"] == []
    # Drop after review, keeping the local text: the replicated hub head becomes the base.
    sync.pull(target, client(hub), settings(target))
    code, out, _ = run(
        ["memory", "hub", "proposals", "drop", "--target", str(target), "--json", "--keep-local", row["proposal_id"]],
        capsys,
    )
    dropped = json.loads(out)
    assert code == 0 and dropped["rebased_on"] == 2 and dropped["keep"] == "keep-local"
    kept = json.loads(Path(dropped["dropped_to"]).read_text(encoding="utf-8"))
    assert kept["entry"]["text"] == "# One\nmine\n" and kept["outcome"] == "keep-local"
    assert Path(dropped["dropped_to"]).parent == store.dropped_dir(target)
    report = sync.push(target, client(hub), settings(target))
    assert [p["card_id"] for p in report["published"]] == ["one"] and report["proposals"] == []
    assert hub.cards[("repo:test/app", "one")]["text"] == "# One\nmine\n"
    # Resolve by taking the hub's text: the proposal closes and nothing is put.
    conflict("theirs again\n", "# One\nmine again\n")
    [open_id] = [p["proposal_id"] for p in store.proposals(target)]
    sync.pull(target, client(hub), settings(target))
    card.write_text("theirs again\n", encoding="utf-8")
    before = len(hub.puts())
    report = sync.push(target, client(hub), settings(target))
    assert report["resolved"] == [open_id] and report["published"] == [] and len(hub.puts()) == before
    assert list(store.proposals(target)) == []
    # The resolved proposal's own text is kept, not deleted.
    resolved = json.loads(Path(report["resolved_kept"][open_id]).read_text(encoding="utf-8"))
    assert resolved["entry"]["text"] == "# One\nmine again\n"
    assert any("kept under" in w for w in report["warnings"])
    code, out, _ = run(["memory", "hub", "proposals", "drop", "--target", str(target), "--keep-local", open_id], capsys)
    assert code == 1 and "no proposal" in out


def test_a_401_at_the_push_preflight_spools(hub, tmp_path):
    target = workspace(tmp_path, mode="sync", one="# One\n")
    hub.status_reply = (401, {"error": "unauthorized"})
    report = sync.push(target, client(hub), settings(target), yes=True)
    assert [s["card_id"] for s in report["spooled"]] == ["one"] and hub.puts() == []
    assert any("401" in w for w in report["warnings"])
    hub.status_reply = None
    assert [p["card_id"] for p in sync.push(target, client(hub), settings(target))["published"]] == ["one"]


def test_a_write_only_credential_still_pushes(hub, tmp_path):
    target = workspace(tmp_path, mode="sync", one="# One\nfirst\n")
    hub.status_reply = (403, {"error": "no read grant", "code": "memory_grant_missing"})
    report = sync.push(target, client(hub), settings(target), yes=True)
    assert [p["card_id"] for p in report["published"]] == ["one"]
    (target / "memory" / "cards" / "one.md").write_text("# One\nsecond\n", encoding="utf-8")
    report = sync.push(target, client(hub), settings(target))
    assert [p["card_id"] for p in report["published"]] == ["one"] and report["proposals"] == []
    # The base epoch came from the hub's answer to the first put.
    assert hub.puts()[-1]["base_epoch"] == hub.epoch


@pytest.mark.parametrize("status", [307, 308, 408, 429])
def test_redirects_timeouts_and_rate_limits_spool_instead_of_proposing(hub, tmp_path, status):
    target = workspace(tmp_path, mode="sync", one="# One\n")
    hub.put_reply = (status, {})
    report = sync.push(target, client(hub), settings(target), yes=True)
    assert [s["card_id"] for s in report["spooled"]] == ["one"] and report["proposals"] == []
    assert any(f"http-{status}" in w for w in report["warnings"])
    hub.put_reply = None
    assert [p["card_id"] for p in sync.push(target, client(hub), settings(target))["published"]] == ["one"]


# --- review round 1: Dot card scope, config errors, files, fusion ---------------------------------


def test_dot_memory_card_opens_a_card_held_in_two_read_scopes(hub, tmp_path, capsys):
    from brigade.dot.adapter import validate_arguments
    from brigade.dot.contracts import DotError

    target = workspace(tmp_path, mode="read")
    _put(hub, "fleet", "twin", "fleet twin")
    _put(hub, "repo:test/app", "twin", "app twin")
    code, out, _ = run(["memory", "hub", "card", "--target", str(target), "--json", "twin"], capsys)
    assert code == 1 and json.loads(out)["code"] == "memory_card_ambiguous"
    tools = _dot_cli(target)
    if os.name == "posix":
        # A read without a scope opens the local cards directory first, which needs POSIX.
        assert tools.memory_card("twin") == {"error": "card_not_found", "name": "twin"}
    got = tools.memory_card("twin", scope="fleet")
    assert (got["scope"], got["content"]) == ("fleet", "fleet twin\n")
    assert tools.commands.argv[-1][-4:] == ["--scope", "fleet", "--", "twin"]
    # The tool contract takes an optional scope and still accepts calls without one.
    assert validate_arguments("memory_card", {"name": "twin", "scope": "fleet"}) == {
        "name": "twin",
        "offset": 0,
        "scope": "fleet",
    }
    assert validate_arguments("memory_card", {"name": "twin"}) == {"name": "twin", "offset": 0}
    with pytest.raises(DotError):
        validate_arguments("memory_card", {"name": "twin", "scope": "nowhere"})


def test_an_unreadable_config_keeps_todays_error(hub, tmp_path, capsys):
    target = workspace(tmp_path, mode=None, one="# One\nwords\n")
    (target / ".brigade").mkdir(exist_ok=True)
    (target / ".brigade" / "memory-care.toml").write_bytes(b'[hub]\nmode = "read"\ntitle = "caf\xe9"\n')
    expected = memory_cmd.search(target=target, query="words")
    today = capsys.readouterr()
    code, out, err = run(["memory", "search", "--target", str(target), "words"], capsys)
    assert (code, out, err) == (expected, today.out, today.err) and code == 2
    assert err.startswith("error: invalid memory-care config:")
    code, _, err = run(["memory", "hub", "status", "--target", str(target)], capsys)
    assert code == 2 and err.startswith("error: invalid memory-care config:")
    if os.name != "posix":
        pytest.skip("Dot reads cards through POSIX directory descriptors; trusted_directory refuses on Windows")
    for directory in (target, target / "memory", target / "memory" / "cards"):
        directory.chmod(0o755)
    tools = _dot_memory(target, {"ok": True, "card": {"text": "never read"}})
    assert tools.memory_card("missing") == {"error": "card_not_found", "name": "missing"}
    assert tools.commands.argv == [] and hub.calls == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory modes; Windows reports 0o777 whatever chmod asked")
def test_hub_state_leaves_the_shared_memory_dir_alone(hub, tmp_path):
    target = workspace(tmp_path, mode="sync", one="# One\n")
    shared = target / ".brigade" / "memory"
    shared.mkdir(parents=True)
    shared.chmod(0o755)
    hub.mode = "down"
    sync.push(target, client(hub), settings(target), yes=True)
    assert stat.S_IMODE(os.stat(shared).st_mode) == 0o755
    assert stat.S_IMODE(os.stat(store.root(target)).st_mode) == 0o700


def test_a_symlinked_ignore_marker_is_refused(hub, tmp_path):
    target = workspace(tmp_path, mode="sync", one="# One\n")
    store.root(target).mkdir(parents=True, mode=0o700)
    (store.root(target) / ".gitignore").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(store.StoreError, match="symlink"):
        sync.push(target, client(hub), settings(target), yes=True)
    assert not (tmp_path / "elsewhere").exists() and hub.puts() == []


def test_fusion_keeps_scopes_apart_caps_the_hub_limit_and_reports_its_count(hub, tmp_path):
    target = workspace(tmp_path, mode="read", mine="# Mine\nlynx\n")
    _put(hub, "fleet", "a", "lynx same")
    _put(hub, "repo:test/app", "a", "lynx same")
    payload = hub_search.search_payload(target, "lynx", limit=50, settings=settings(target))
    assert {(m["card_id"], m["scope"]) for m in payload["matches"] if m["source"] == "hub"} == {
        ("a", "fleet"),
        ("a", "repo:test/app"),
    }
    assert [c["limit"] for c in hub.calls if c["action"] == "search"] == [20]
    assert payload["retrieval"]["hub_limit"] == 20 and "--limit 50 was capped" in payload["warning"]
    assert payload["hub_match_count"] == 2 and payload["match_count"] == 3


def test_event_ids_from_one_process_strictly_increase():
    ids = [hubc.event_id() for _ in range(2000)]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)


# --- review round 2: admin credential, locks, dropped proposals, card reads ----------------------


def _conflict(hub: FakeHub, target: Path, theirs: str, mine: str) -> str:
    """Another writer moves the hub head; this workspace's edit becomes a proposal. Returns its id."""
    head = hub.cards[("repo:test/app", "one")]["revision"]
    client(hub).call(
        "put",
        scope="repo:test/app",
        card_id="one",
        base_revision=head,
        base_epoch=hub.epoch,
        text=theirs,
        client_event_id=hubc.event_id(),
    )
    (target / "memory" / "cards" / "one.md").write_text(mine, encoding="utf-8")
    [proposal] = sync.push(target, client(hub), settings(target))["proposals"]
    return str(proposal["proposal_id"])


def test_an_admin_token_in_the_node_slot_is_refused(hub, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BRIGADE_FLEET_NODE_TOKEN", ADMIN_CREDENTIAL)
    target = workspace(tmp_path, mode="sync", one="# One\nsalary\n")
    hub.import_reply = (201, {"event": {"revision": 1}, "log_epoch": hub.epoch})
    commands = (
        ["status", "--json"],
        ["pull"],
        ["push", "--yes"],
        ["import", "--scope", "principal:alice", str(target / "memory" / "cards" / "one.md")],
    )
    for command in commands:
        code, out, err = run(["memory", "hub", command[0], "--target", str(target), *command[1:]], capsys)
        assert code == 2 and "admin token" in err, command
        assert ADMIN_CREDENTIAL not in out + err
    admin = hubc.MemoryHubClient(HUB_URL, ADMIN_CREDENTIAL, transport=hub)
    with pytest.raises(hubc.HubConfigError, match="admin token"):
        sync.drop_proposal(target, admin, settings(target), "hub-0", keep=sync.DROP_KEEP_LOCAL)
    # Only status calls reached the hub: nothing was read, written or imported as the admin.
    assert {c["action"] for c in hub.calls} == {"status"}
    assert not store.replica(target).exists and store.spool_count(target) == 0


def test_a_long_pull_never_blocks_a_push_and_a_busy_lock_says_so(hub, tmp_path, monkeypatch, capsys):
    target = workspace(tmp_path, mode="sync", one="# One\n")
    _put(hub, "fleet", "theirs", "theirs")
    original = hub.do_log
    free: list[bool] = []

    def log_while_checking_the_state_lock(body):
        # The pull's network calls run without the state lock.
        with store.locked(target, deadline_seconds=0.1):
            free.append(True)
        return original(body)

    hub.do_log = log_while_checking_the_state_lock
    sync.pull(target, client(hub), settings(target))
    assert free and _card_ids(store.replica(target)) == {"theirs"}
    hub.do_log = original
    monkeypatch.setattr(store, "LOCK_DEADLINE_SECONDS", 0.1)
    with store.pulling(target):
        code, out, err = run(["memory", "hub", "push", "--target", str(target), "--yes"], capsys)
        assert code == 0 and "1 published" in out
        code, out, err = run(["memory", "hub", "pull", "--target", str(target)], capsys)
        assert code == 75 and "another memory hub push or pull is running" in err and "unusable" not in err
    with store.locked(target):
        code, out, err = run(["memory", "hub", "push", "--target", str(target)], capsys)
        assert code == 75 and "another memory hub push or pull is running" in err and "unusable" not in err


def test_keep_hub_rewrites_the_card_and_keeps_the_local_text(hub, tmp_path, capsys):
    target = workspace(tmp_path, mode="sync", one="# One\nfirst\n")
    card = target / "memory" / "cards" / "one.md"
    sync.push(target, client(hub), settings(target), yes=True)
    proposal_id = _conflict(hub, target, "theirs\n", "# One\nmine\n")
    sync.pull(target, client(hub), settings(target))
    code, out, err = run(
        ["memory", "hub", "proposals", "drop", "--target", str(target), "--keep-hub", proposal_id], capsys
    )
    assert code == 0, err
    assert "holds hub revision 2" in out and "kept at" in out
    assert card.read_text(encoding="utf-8") == "theirs\n"
    [kept] = list(store.dropped_dir(target).glob("*.json"))
    record = json.loads(kept.read_text(encoding="utf-8"))
    assert record["entry"]["text"] == "# One\nmine\n" and record["card_text_replaced"] == "# One\nmine\n"
    assert record["outcome"] == "keep-hub"
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(kept).st_mode) == 0o600
    before = len(hub.puts())
    report = sync.push(target, client(hub), settings(target))
    assert report["published"] == [] and report["proposals"] == [] and len(hub.puts()) == before
    assert hub.cards[("repo:test/app", "one")]["text"] == "theirs\n"


def test_drop_refuses_a_replica_from_an_old_epoch_or_without_one(hub, tmp_path, capsys):
    target = workspace(tmp_path, mode="sync", one="# One\nfirst\n")
    sync.push(target, client(hub), settings(target), yes=True)
    proposal_id = _conflict(hub, target, "theirs\n", "# One\nmine\n")
    argv = ["memory", "hub", "proposals", "drop", "--target", str(target), "--keep-local", proposal_id]
    code, _, err = run(argv, capsys)
    assert code == 2 and "pull" in err and "first" in err
    sync.pull(target, client(hub), settings(target))
    hub.restore(hub.snapshot())  # a restore: same data, a new log_epoch
    code, _, err = run(argv, capsys)
    assert code == 2 and "current log_epoch" in err
    # Nothing changed: the proposal is still open and nothing was moved aside.
    assert [p["proposal_id"] for p in store.proposals(target)] == [proposal_id]
    assert not store.dropped_dir(target).exists()
    sync.pull(target, client(hub), settings(target))
    code, out, _ = run(argv, capsys)
    assert code == 0 and "over hub revision 2" in out


def test_card_reads_fall_back_to_the_replica_only_when_the_hub_is_unavailable(hub, tmp_path, capsys):
    target = workspace(tmp_path, mode="read")
    _put(hub, "fleet", "theirs", "replicated text that spans several chunks")
    sync.pull(target, client(hub), settings(target))
    hub.mode = "down"
    code, out, err = run(["memory", "hub", "card", "--target", str(target), "--all", "--json", "theirs"], capsys)
    payload = json.loads(out)
    assert code == 0 and payload["source"] == "replica" and payload["retrieval"]["reason"] == "unreachable"
    assert payload["card"]["text"] == "replicated text that spans several chunks\n"
    assert payload["card"]["scope"] == "fleet" and payload["card"]["next_offset"] is None
    code, out, err = run(["memory", "hub", "card", "--target", str(target), "--all", "theirs"], capsys)
    assert out == "replicated text that spans several chunks\n\n" and "replica" in err
    code, out, _ = run(["memory", "hub", "card", "--target", str(target), "--json", "missing"], capsys)
    assert code == 1 and json.loads(out)["hub"] == "unavailable"
    # A refusal never falls back.
    hub.mode = "up"
    hub.do_card = lambda body: (403, {"error": "no read grant", "code": "memory_grant_missing"})
    code, out, _ = run(
        ["memory", "hub", "card", "--target", str(target), "--scope", "fleet", "--json", "theirs"], capsys
    )
    assert code == 1 and json.loads(out)["code"] == "memory_grant_missing"


def test_dot_memory_card_opens_a_replica_match_while_the_hub_is_down(hub, tmp_path):
    target = workspace(tmp_path, mode="read")
    _put(hub, "fleet", "theirs", "otter replicated")
    sync.pull(target, client(hub), settings(target))
    hub.mode = "down"
    tools = _dot_cli(target)
    [match] = tools.memory_search("otter")["matches"]
    assert (match["card_id"], match["source"]) == ("theirs", "replica")
    got = tools.memory_card("theirs", scope=match["scope"])
    assert (got["source"], got["content"], got["scope"]) == ("replica", "otter replicated\n", "fleet")
    hub.mode = "up"
    hub.do_card = lambda body: (401, {"error": "unauthorized"})
    assert tools.memory_card("theirs", scope="fleet") == {"error": "card_not_found", "name": "theirs"}


def test_card_all_never_splices_chunks_from_two_epochs(hub, tmp_path, capsys):
    target = workspace(tmp_path, mode="read")
    text = "first version, long enough for several chunks"
    _put(hub, "fleet", "long", text)
    original = hub.do_card
    restores = [1]

    def restored_between_chunks(body):
        if body.get("offset") and restores:
            restores.pop()
            snap = hub.snapshot()
            hub.restore(snap)  # same revision numbers under a new log_epoch
            key = ("fleet", "long", hub.cards[("fleet", "long")]["revision"])
            other = hubc.normalize_text("other version entirely, also long enough to chunk")
            changed = {**hub.revisions[key], "text": other, "hash": hashlib.sha256(other.encode()).hexdigest()}
            hub.revisions[key] = changed
            hub.cards[("fleet", "long")] = dict(changed)
        return original(body)

    hub.do_card = restored_between_chunks
    argv = ["memory", "hub", "card", "--target", str(target), "--all", "--json", "long"]
    code, out, _ = run(argv, capsys)
    payload = json.loads(out)
    # The read restarted under the new epoch and returned one whole version.
    assert code == 0 and payload["card"]["text"] == "other version entirely, also long enough to chunk\n"
    assert payload["log_epoch"] == hub.epoch
    assert hubc.content_hash(payload["card"]["text"]) == payload["card"]["content_hash"]

    def always_moving(body):
        if body.get("offset"):
            hub.epoch = hubc.event_id()
        return original(body)

    hub.do_card = always_moving
    code, out, _ = run(argv, capsys)
    assert code == 1 and json.loads(out)["code"] == "memory_epoch_changed"

    def wrong_hash(body):
        status, reply = original(body)
        if status != 200:
            return status, reply
        return status, {**reply, "card": {**reply["card"], "content_hash": "00" * 32}}

    hub.do_card = wrong_hash
    code, out, _ = run(argv, capsys)
    assert code == 1 and json.loads(out)["code"] == "memory_card_hash_mismatch"


# --- review round 3: refused scopes, stale drops, admin card reads, moved cards -----------------


@pytest.mark.parametrize(
    "refusal",
    [
        (403, {"error": "no read grant", "code": "memory_grant_missing"}),
        (404, {"error": "card not found", "code": "memory_card_not_found"}),
    ],
    ids=["revoked-grant", "retired-card"],
)
def test_a_scope_the_hub_refused_is_never_served_from_the_replica(hub, tmp_path, capsys, refusal):
    target = workspace(tmp_path, mode="read", scope="repo:a", read_scopes=("repo:a", "repo:b"))
    _put(hub, "repo:a", "x", "refused scope text")
    sync.pull(target, client(hub), settings(target))
    original = hub.do_card

    def refuse_a_then_rate_limit_b(body):
        if body.get("scope") == "repo:a":
            return refusal
        if body.get("scope") == "repo:b":
            return 429, {}
        return original(body)

    hub.do_card = refuse_a_then_rate_limit_b
    code, out, err = run(["memory", "hub", "card", "--target", str(target), "--all", "--json", "x"], capsys)
    assert code == 1 and "refused scope text" not in out + err
    assert json.loads(out).get("source") != "replica"
    tools = _dot_cli(target)
    if os.name == "posix":
        # The scope-less read opens the local cards directory first, which needs POSIX.
        assert tools.memory_card("x") == {"error": "card_not_found", "name": "x"}
    # A scope the hub never refused still falls back while the hub is unavailable.
    _put(hub, "repo:b", "y", "rate limited scope text")
    hub.do_card = original
    sync.pull(target, client(hub), settings(target))
    hub.do_card = refuse_a_then_rate_limit_b
    got = tools.memory_card("y", scope="repo:b")
    assert (got["source"], got["content"]) == ("replica", "rate limited scope text\n")


@pytest.mark.parametrize("keep", ["--keep-hub", "--keep-local"])
def test_drop_refuses_a_replica_pulled_before_the_conflicting_write(hub, tmp_path, capsys, keep):
    target = workspace(tmp_path, mode="sync", one="# One\nfirst\n")
    card = target / "memory" / "cards" / "one.md"
    sync.push(target, client(hub), settings(target), yes=True)
    # A routine pull before the other writer's edit: same epoch, same principal, an old head.
    sync.pull(target, client(hub), settings(target))
    proposal_id = _conflict(hub, target, "theirs\n", "# One\nmine\n")
    argv = ["memory", "hub", "proposals", "drop", "--target", str(target), keep, proposal_id]
    code, out, err = run(argv, capsys)
    assert code == 2 and "pull" in err and "first" in err, out + err
    assert [p["proposal_id"] for p in store.proposals(target)] == [proposal_id]
    assert not store.dropped_dir(target).exists()
    assert card.read_text(encoding="utf-8") == "# One\nmine\n"
    # A pull after the conflict, then a third write: still behind the hub.
    sync.pull(target, client(hub), settings(target))
    _put_on(hub, "one", "third\n")
    code, out, err = run(argv, capsys)
    assert code == 2 and "pull" in err, out + err
    sync.pull(target, client(hub), settings(target))
    code, out, err = run(argv, capsys)
    head = hub.cards[("repo:test/app", "one")]["revision"]
    assert code == 0 and f"revision {head}" in out, err


def _put_on(hub: FakeHub, card_id: str, text: str, scope: str = "repo:test/app") -> None:
    """Another writer moves the head of an existing card."""
    head = hub.cards[(scope, card_id)]["revision"]
    client(hub).call(
        "put",
        scope=scope,
        card_id=card_id,
        base_revision=head,
        base_epoch=hub.epoch,
        text=text,
        client_event_id=hubc.event_id(),
    )


def test_card_refuses_an_admin_token_in_the_node_slot(hub, tmp_path, monkeypatch, capsys):
    hub.grants["node-1"] = {"repo:test/app"}
    target = workspace(tmp_path, mode="read", read_scopes=("repo:test/app", "principal:alice"))
    _put(hub, "principal:alice", "alice-notes", "alice private text", token=ADMIN_CREDENTIAL)
    code, out, _ = run(
        ["memory", "hub", "card", "--target", str(target), "--scope", "principal:alice", "--json", "alice-notes"],
        capsys,
    )
    assert code == 1 and json.loads(out)["code"] == "memory_grant_missing"
    monkeypatch.setenv("BRIGADE_FLEET_NODE_TOKEN", ADMIN_CREDENTIAL)
    hub.calls.clear()
    code, out, err = run(
        ["memory", "hub", "card", "--target", str(target), "--scope", "principal:alice", "--json", "alice-notes"],
        capsys,
    )
    assert code == 2 and "admin token" in err and "alice private text" not in out + err
    assert ADMIN_CREDENTIAL not in out + err
    assert [c["action"] for c in hub.calls] == ["status"]
    tools = _dot_cli(target)
    assert tools.memory_card("alice-notes", scope="principal:alice") == {
        "error": "card_not_found",
        "name": "alice-notes",
    }


def test_keep_hub_follows_a_moved_card_by_id(hub, tmp_path, capsys):
    target = workspace(tmp_path, mode="sync", one="---\nid: one\n---\n# One\nfirst\n")
    cards = target / "memory" / "cards"
    sync.push(target, client(hub), settings(target), yes=True)
    theirs = "---\nid: one\n---\n# One\ntheirs\n"
    proposal_id = _conflict(hub, target, theirs, "---\nid: one\n---\n# One\nmine\n")
    sync.pull(target, client(hub), settings(target))
    (cards / "archive").mkdir()
    (cards / "one.md").rename(cards / "archive" / "one.md")
    argv = ["memory", "hub", "proposals", "drop", "--target", str(target), "--keep-hub", proposal_id]
    # Two local cards hold the id: refuse rather than pick one.
    (cards / "one.md").write_text("---\nid: one\n---\n# One\ncopy\n", encoding="utf-8")
    code, out, err = run(argv, capsys)
    assert code == 2 and "more than one" in err, out + err
    (cards / "one.md").unlink()
    code, out, err = run(argv, capsys)
    assert code == 0, err
    assert not (cards / "one.md").exists()
    assert (cards / "archive" / "one.md").read_text(encoding="utf-8") == hubc.normalize_text(theirs)
    before = len(hub.puts())
    report = sync.push(target, client(hub), settings(target))
    assert report["published"] == [] and report["proposals"] == [] and len(hub.puts()) == before


def test_keep_hub_refuses_a_card_file_that_is_not_utf8(hub, tmp_path, capsys):
    target = workspace(tmp_path, mode="sync", one="# One\nfirst\n")
    card = target / "memory" / "cards" / "one.md"
    sync.push(target, client(hub), settings(target), yes=True)
    proposal_id = _conflict(hub, target, "theirs\n", "# One\nmine\n")
    sync.pull(target, client(hub), settings(target))
    card.write_bytes(b"# One\n\xff\xfe not utf-8\n")
    argv = ["memory", "hub", "proposals", "drop", "--target", str(target), "--keep-hub", proposal_id]
    code, out, err = run(argv, capsys)
    assert code == 2 and "Traceback" not in err and "one.md" in err, out + err
    assert card.read_bytes() == b"# One\n\xff\xfe not utf-8\n"
    assert [p["proposal_id"] for p in store.proposals(target)] == [proposal_id]
