"""Hub work-record gaps from #1595: conflict details at the client and holder-bound ownership reads.

Both run against the real authenticated handler and SQLite store, and the client test
also crosses a real HTTP socket.
"""

import argparse
import json

import pytest

from brigade import fleet_hub
from brigade import worklore_client as client
from brigade import worklore_store as store
from brigade.cli import fleet as fleet_cli
from tests.test_worklore_ownership import (
    CHECKPOINT,
    NONCE_A,
    NONCE_B,
    NONCE_C,
    event_count,
    ledger as ledger,  # shared real SQLite fixture
    owned,
    real_hub,
    request,
)
from tests.test_worklore_scope import another_owner, checkpoint


OFFLINE_SETTINGS = {"hub_url": "http://127.0.0.1:9", "node_token": "t"}  # content-guard: allow loopback-ipv4


def ownership_get(conn, work_id, **kwargs):
    return request(conn, work_id, method="GET", revision=None, key=None, **kwargs)


# Gap 3: an ownership read that presents a holder capability must be the holder.


def test_holder_reads_its_own_ownership_and_the_view_matches_the_public_read(ledger):
    conn, work_id, _ = ledger
    owned(conn, work_id)
    public_status, public = ownership_get(conn, work_id)
    status, reply = ownership_get(conn, work_id, nonce=NONCE_A)
    assert public_status == status == 200
    assert reply == public
    assert reply["ownership"]["state"] == "owned" and reply["ownership"]["owner_node"] == "node-a"
    assert NONCE_A not in json.dumps(reply)


@pytest.mark.parametrize(
    ("node", "admin", "nonce"),
    [
        ("node-a", False, NONCE_B),  # the holder node with a foreign capability
        ("node-b", False, NONCE_A),  # another node replaying the holder's capability
        ("node-b", False, NONCE_B),  # another node with its own capability
        (None, True, NONCE_A),  # admin cannot act as a holder
    ],
)
def test_foreign_holder_capability_is_refused_with_the_existing_shape(ledger, node, admin, nonce):
    conn, work_id, _ = ledger
    owned(conn, work_id)
    before = (event_count(conn, work_id), ownership_get(conn, work_id)[1])
    status, reply = ownership_get(conn, work_id, node=node, admin=admin, nonce=nonce)
    assert status == 403
    assert set(reply) == {"error", "code"} and reply["code"] == "holder-mismatch"
    assert all(secret not in json.dumps(reply) for secret in (NONCE_A, NONCE_B, "grant-1"))
    assert (event_count(conn, work_id), ownership_get(conn, work_id)[1]) == before


def test_capability_read_of_an_item_with_no_holder_is_refused(ledger):
    conn, work_id, _ = ledger
    status, reply = ownership_get(conn, work_id, nonce=NONCE_A)
    assert status == 403 and reply["code"] == "holder-mismatch"


def test_stale_holder_capability_is_refused_after_a_new_holder_accepts(ledger):
    conn, work_id, _ = ledger
    owned(conn, work_id)
    release = {"action": "release", "generation": 1}
    assert request(conn, work_id, release, revision=2, key="release", nonce=NONCE_A)[0] == 200
    assert ownership_get(conn, work_id, nonce=NONCE_A)[0] == 403


def test_malformed_capability_is_refused_without_a_read(ledger):
    conn, work_id, _ = ledger
    owned(conn, work_id)
    status, reply = ownership_get(conn, work_id, nonce="not-a-capability")
    assert status == 400 and "not-a-capability" not in json.dumps(reply)


def test_reads_without_a_capability_are_unchanged_for_every_caller(ledger):
    conn, work_id, _ = ledger
    owned(conn, work_id)
    assert all(
        ownership_get(conn, work_id, node=node, admin=admin)[0] == 200
        for node, admin in (("node-a", False), ("node-b", False), (None, True))
    )


# Gap 1: the client carries the Hub's structured path-scope refusal to the caller.


def conflicting_pair(tmp_path, monkeypatch, *, requested):
    monkeypatch.setenv("BRIGADE_WORKLORE_ENABLED", "1")
    server_cm = real_hub(tmp_path)
    server = server_cm.__enter__()
    db = fleet_hub.open_db(tmp_path / "http.db")
    tokens = {node: fleet_hub.add_node(db, node, node)[1] for node in ("node-a", "node-b")}
    held = store.create_item(db, {"title": "Held", "kind": "repo"}, actor_id="admin", actor_type="operator")
    owned(db, held["work_id"])
    assert checkpoint(db, held["work_id"], ["src"])[0] == 200
    other = another_owner(db, node="node-b")
    settings = {
        "hub_url": f"http://127.0.0.1:{server.server_address[1]}",  # content-guard: allow loopback-ipv4
        "node_token": tokens["node-b"],
        "admin_token": "test-admin-token",
    }  # content-guard: allow loopback-ipv4
    monkeypatch.setattr(client, "load_fleet_settings", lambda: settings)
    body = {**CHECKPOINT, "write_scope": requested}
    return server_cm, db, held["work_id"], other, body


def test_client_surfaces_conflict_details_from_a_real_hub_refusal(tmp_path, monkeypatch, capsys):
    server_cm, db, held, other, body = conflicting_pair(tmp_path, monkeypatch, requested=["src/module.py"])
    try:
        with pytest.raises(client.WorkloreClientError) as error:
            client.ownership_action(other, body, if_match=2, idempotency_key="scope", holder_nonce=NONCE_B)
        assert error.value.code == "path-scope-conflict"
        assert error.value.details == {
            "resolution": "replace-scope-or-release-holder",
            "conflicts": [
                {
                    "work_id": held,
                    "owner_node": "node-a",
                    "requested_path": "src/module.py",
                    "held_path": "src",
                    "resolution": "replace-scope-or-release-holder",
                }
            ],
            "truncated": False,
        }
        assert all(secret not in repr(error.value.details) for secret in (NONCE_A, NONCE_B, "grant-1"))
        # The operator-facing printers show the same bounded fields, JSON and human.
        args = argparse.Namespace(json=True)
        assert fleet_cli._run_work(args, lambda: raise_(error.value)) == 1
        printed = json.loads(capsys.readouterr().err)
        assert printed["code"] == "path-scope-conflict" and printed["conflicts"][0]["held_path"] == "src"
        assert fleet_cli._run_work(argparse.Namespace(json=False), lambda: raise_(error.value)) == 1
        text = capsys.readouterr().err
        assert f"src/module.py overlaps src held by {held} (owner node-a)" in text
        assert "resolution: replace-scope-or-release-holder" in text
    finally:
        db.close()
        server_cm.__exit__(None, None, None)


def raise_(exc):
    raise exc


def test_client_get_ownership_sends_the_capability_only_under_the_node_token(tmp_path, monkeypatch):
    server_cm, db, held, _, _ = conflicting_pair(tmp_path, monkeypatch, requested=["lib"])
    try:
        settings = client.load_fleet_settings()
        with pytest.raises(client.WorkloreClientError) as error:
            client.get_ownership(held, holder_nonce=NONCE_C)  # node-b is not the holder of this item
        assert error.value.code == "holder-mismatch" and NONCE_C not in str(error.value)
        settings["node_token"] = ""
        with pytest.raises(client.FleetClientError, match="no fleet node token"):
            client.get_ownership(held, holder_nonce=NONCE_A)  # never falls back to admin
        assert client.get_ownership(held)["ownership"]["owner_node"] == "node-a"  # plain read still falls back
    finally:
        db.close()
        server_cm.__exit__(None, None, None)


def test_non_scope_refusals_carry_no_conflict_details(monkeypatch):
    import io
    import urllib.error

    refusal = io.BytesIO(
        json.dumps({"code": "holder-mismatch", "error": "no", "conflicts": [{"work_id": "x"}]}).encode()
    )

    def fake_open(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, refusal)

    monkeypatch.setattr(client, "load_fleet_settings", lambda: OFFLINE_SETTINGS)
    monkeypatch.setattr(client, "_hub_open", fake_open)
    with pytest.raises(client.WorkloreClientError) as error:
        client.get_ownership("wl-1")
    assert error.value.details == {}


def test_hostile_conflict_bodies_are_bounded_and_terminal_safe(monkeypatch):
    import io
    import urllib.error

    hostile = [{"work_id": f"wl-{i}", "held_path": "src/\x1b[2J" + "x" * 1000, "extra": "dropped"} for i in range(30)]
    body = {"code": "path-scope-conflict", "error": "overlap", "resolution": "replace-scope-or-release-holder"}
    body |= {"conflicts": hostile, "truncated": False}
    refusal = io.BytesIO(json.dumps(body).encode())

    def fake_open(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 409, "Conflict", {}, refusal)

    monkeypatch.setattr(client, "load_fleet_settings", lambda: OFFLINE_SETTINGS)
    monkeypatch.setattr(client, "_hub_open", fake_open)
    with pytest.raises(client.WorkloreClientError) as error:
        client.get_ownership("wl-1")
    conflicts = error.value.details["conflicts"]
    assert len(conflicts) == client.MAX_CONFLICT_DETAILS and error.value.details["truncated"] is True
    assert all(set(record) == {"work_id", "held_path"} for record in conflicts)
    assert all(
        len(record["held_path"]) <= client.MAX_CONFLICT_TEXT and "\x1b" not in record["held_path"]
        for record in conflicts
    )


def test_conflict_text_drops_bidi_and_zero_width_characters():
    from brigade import worklore_client

    assert worklore_client._safe_terminal_text("ab\u202ecd\u200bef\u2066gh") == "ab cd ef gh"
