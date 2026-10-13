"""Atomic checkpoint admission at the authenticated HTTP/SQLite owner boundary."""

import json
import threading

import pytest

from brigade import fleet_hub
from brigade import worklore_store as store
from tests.test_worklore_ownership import (
    CHECKPOINT,
    NONCE_A,
    NONCE_B,
    OFFER,
    event_count,
    ledger as ledger,  # shared real SQLite fixture
    make_terminal,
    owned,
    post,
    request,
)


def another_owner(conn, *, node="node-b"):
    item = store.create_item(conn, {"title": "Other fixture", "kind": "repo"}, actor_id="admin")
    work_id = item["work_id"]
    post(conn, work_id, {**OFFER, "target_node": node}, admin=True, key="offer-other")
    post(conn, work_id, {"action": "accept", "generation": 0}, node=node, revision=1, key="accept-other", nonce=NONCE_B)
    return work_id


def checkpoint(conn, work_id, paths, *, repo="example/project", revision=2, node="node-a", nonce=NONCE_A, key="scope"):
    return request(
        conn,
        work_id,
        {**CHECKPOINT, "repo_identity": repo, "write_scope": paths},
        revision=revision,
        node=node,
        nonce=nonce,
        key=key,
    )


def snapshot(conn, work_id):
    return request(conn, work_id, method="GET")[1]["ownership"]


@pytest.mark.parametrize(
    ("held", "requested", "repo", "node", "conflicts"),
    [
        ("src/module.py", "src/module.py", "example/project", "node-b", True),
        ("src/module.py", "src", "example/project", "node-b", True),
        ("src", "src/module.py", "example/project", "node-b", True),
        ("src/module.py", "src", "EXAMPLE/Project", "node-b", True),
        ("src", "src/module.py", "example/project", "node-a", True),
        ("src/a", "src/ab", "example/project", "node-b", False),
        ("src/a", "src/b", "example/project", "node-b", False),
        ("src/a", "src/A", "example/project", "node-b", False),
        ("src", "src", "example/other", "node-b", False),
    ],
)
def test_checkpoint_path_components_and_repository_identity(ledger, held, requested, repo, node, conflicts):
    conn, work_id, _ = ledger
    owned(conn, work_id)
    other = another_owner(conn, node=node)
    assert checkpoint(conn, work_id, [held])[0] == 200
    before = snapshot(conn, other)
    status, reply = checkpoint(conn, other, [requested], repo=repo, node=node, nonce=NONCE_B)
    if conflicts:
        assert status == 409, reply
        assert reply["code"] == "path-scope-conflict"
        assert reply["conflicts"] == [
            {
                "work_id": work_id,
                "owner_node": "node-a",
                "requested_path": requested,
                "held_path": held,
                "resolution": "replace-scope-or-release-holder",
            }
        ]
        assert reply["truncated"] is False
        assert snapshot(conn, other) == before and event_count(conn, other) == 2
        assert all(secret not in json.dumps(reply) for secret in (NONCE_A, NONCE_B, "grant-1", "checks complete"))
    else:
        assert status == 200, reply
        assert reply["ownership"]["repo_identity"] == repo
        assert reply["ownership"]["conflict_check"] == "path-admission"
        assert reply["ownership"]["liveness"] == "unknown"


@pytest.mark.parametrize(
    "reservation",
    [
        "handoff",
        "accepted-handoff",
        "canceled",
        "completed",
        "archived",
        "revoked",
        "absent-presence",
        "expired-presence",
        "unchecked-import",
    ],
)
def test_reservations_survive_non_release_observations(ledger, reservation):
    conn, work_id, _ = ledger
    owned(conn, work_id)
    assert checkpoint(conn, work_id, ["src"])[0] == 200
    other = another_owner(conn)
    owner = "node-a"
    if reservation in {"handoff", "accepted-handoff"}:
        post(
            conn,
            work_id,
            {"action": "handoff", "generation": 1, "target_node": "node-b"},
            revision=3,
            key="handoff",
            nonce=NONCE_A,
        )
        if reservation == "accepted-handoff":
            post(
                conn,
                work_id,
                {"action": "accept", "generation": 1},
                revision=4,
                key="accept-handoff",
                node="node-b",
                nonce=NONCE_B,
            )
            owner = "node-b"
    elif reservation in {"canceled", "completed", "archived"}:
        make_terminal(conn, work_id, reservation)
    elif reservation == "revoked":
        conn.execute("UPDATE nodes SET revoked_at='2026-01-01T00:00:00Z' WHERE node_id='node-a'")
        conn.commit()
    elif reservation == "expired-presence":
        conn.execute(
            "INSERT INTO interactive_sessions (node_id,harness,session_id,repo_identity,identity_scope,"
            "repo_label,checkout_path,dirty_paths_json,dirty_truncated,state,started_at,heartbeat_at,ttl_seconds,expires_at) "
            "VALUES ('node-a','codex','fixture-session','example/project','fleet','Fixture','',"
            "'[]',0,'active','2000-01-01T00:00:00Z','2000-01-01T00:00:00Z',120,1)"
        )
        conn.commit()
    elif reservation == "unchecked-import":
        row = conn.execute(
            "SELECT seq, detail_json FROM work_events WHERE work_id=? ORDER BY seq DESC LIMIT 1", (work_id,)
        ).fetchone()
        detail = json.loads(row[1])
        detail["ownership"]["conflict_check"] = "not-performed"
        conn.execute("UPDATE work_events SET detail_json=? WHERE seq=?", (json.dumps(detail), row[0]))
        conn.commit()
    status, reply = checkpoint(conn, other, ["src/module.py"], node="node-b", nonce=NONCE_B)
    assert status == 409, reply
    assert reply["conflicts"][0]["owner_node"] == owner
    assert event_count(conn, other) == 2


@pytest.mark.parametrize("exit", ["release", "replace", "empty"])
@pytest.mark.parametrize("historical_marker", ["path-admission", "not-performed"])
def test_explicit_scope_exit_and_immutable_replay(ledger, exit, historical_marker):
    conn, work_id, _ = ledger
    owned(conn, work_id)
    original = checkpoint(conn, work_id, ["src"])[1]["ownership"]
    if historical_marker == "not-performed":
        row = conn.execute(
            "SELECT seq, detail_json FROM work_events WHERE work_id=? ORDER BY seq DESC LIMIT 1", (work_id,)
        ).fetchone()
        detail = json.loads(row[1])
        detail["ownership"]["conflict_check"] = historical_marker
        conn.execute("UPDATE work_events SET detail_json=? WHERE seq=?", (json.dumps(detail), row[0]))
        conn.commit()
        original["conflict_check"] = historical_marker
    if exit == "release":
        post(conn, work_id, {"action": "release", "generation": 1}, revision=3, key="exit", nonce=NONCE_A)
    else:
        assert checkpoint(conn, work_id, [] if exit == "empty" else ["docs"], revision=3, key="exit")[0] == 200
    other = another_owner(conn)
    assert checkpoint(conn, other, ["src/module.py"], node="node-b", nonce=NONCE_B)[0] == 200
    before = snapshot(conn, work_id)
    count = event_count(conn, work_id)
    assert checkpoint(conn, work_id, ["src"])[1]["ownership"] == original
    assert snapshot(conn, work_id) == before and event_count(conn, work_id) == count
    assert original["conflict_check"] == historical_marker


def test_failed_expansion_keeps_previous_reservation(ledger):
    conn, work_id, _ = ledger
    owned(conn, work_id)
    assert checkpoint(conn, work_id, ["docs"])[0] == 200
    other = another_owner(conn)
    assert checkpoint(conn, other, ["src"], node="node-b", nonce=NONCE_B)[0] == 200
    before = snapshot(conn, work_id)
    status, reply = checkpoint(conn, work_id, ["docs", "src/module.py"], revision=3, key="expand")
    assert status == 409 and reply["code"] == "path-scope-conflict"
    assert snapshot(conn, work_id) == before and event_count(conn, work_id) == 3
    status, reply = checkpoint(conn, other, ["docs"], revision=3, node="node-b", nonce=NONCE_B, key="replace")
    assert status == 409 and reply["conflicts"][0]["held_path"] == "docs"


def test_expansion_and_acquisition_serialize_across_connections(ledger):
    conn, work_id, path = ledger
    owned(conn, work_id)
    assert checkpoint(conn, work_id, ["docs"])[0] == 200
    other = another_owner(conn)
    barrier = threading.Barrier(2)
    results, errors = {}, []

    def contend(identifier, paths, revision, node, nonce):
        db = fleet_hub.open_db(path)
        try:
            barrier.wait(timeout=5)
            results[identifier] = checkpoint(
                db, identifier, paths, revision=revision, node=node, nonce=nonce, key="race"
            )
        except BaseException as exc:
            errors.append(exc)
        finally:
            db.close()

    threads = [
        threading.Thread(target=contend, args=(work_id, ["docs", "src"], 3, "node-a", NONCE_A)),
        threading.Thread(target=contend, args=(other, ["src/module.py"], 2, "node-b", NONCE_B)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not errors and all(not thread.is_alive() for thread in threads)
    assert sorted(status for status, _ in results.values()) == [200, 409]
    loser = next(identifier for identifier, (status, _) in results.items() if status == 409)
    assert results[loser][1]["code"] == "path-scope-conflict"
    assert snapshot(conn, work_id)["write_scope"] == (["docs"] if loser == work_id else ["docs", "src"])
    assert event_count(conn, work_id) + event_count(conn, other) == 6


@pytest.mark.parametrize(
    ("guard", "status", "code"),
    [
        ("cas", 409, "version-conflict"),
        ("holder", 403, "holder-mismatch"),
        ("capacity", 409, "ownership-capacity-exhausted"),
        ("state", 409, "ownership-conflict"),
    ],
)
def test_fences_precede_scope_conflict_disclosure(ledger, monkeypatch, guard, status, code):
    from brigade import worklore_ownership

    conn, work_id, _ = ledger
    owned(conn, work_id)
    assert checkpoint(conn, work_id, ["src"])[0] == 200
    other = another_owner(conn)
    revision = 2
    if guard == "state":
        post(
            conn,
            other,
            {"action": "handoff", "generation": 1, "target_node": "node-a"},
            node="node-b",
            revision=2,
            key="handoff-other",
            nonce=NONCE_B,
        )
        revision = 3
    before = event_count(conn, other)
    if guard == "capacity":
        monkeypatch.setattr(worklore_ownership, "ITEM_MAX_OWNERSHIP_EVENTS", 2)
    actual, reply = checkpoint(
        conn,
        other,
        ["src"],
        node="node-b",
        nonce=NONCE_A if guard == "holder" else NONCE_B,
        revision=0 if guard == "cas" else revision,
    )
    assert actual == status and reply["code"] == code
    assert "conflicts" not in reply and event_count(conn, other) == before


def test_latest_snapshot_scan_bound_refuses_without_truncating_admission(ledger, monkeypatch):
    from brigade import worklore_scope

    conn, work_id, _ = ledger
    owned(conn, work_id)
    for _ in range(2):
        another_owner(conn)
    monkeypatch.setattr(worklore_scope, "MAX_SCOPE_SNAPSHOTS", 1)
    before = snapshot(conn, work_id)
    status, reply = checkpoint(conn, work_id, ["src"])
    assert status == 409 and reply["code"] == "path-scope-capacity-exhausted"
    assert reply["resolution"] == "reduce-snapshot-set-or-retry-after-scope-review"
    assert snapshot(conn, work_id) == before and event_count(conn, work_id) == 2
    # Empty scope is observational and needs no global writer admission.
    assert checkpoint(conn, work_id, [])[0] == 200


@pytest.mark.parametrize("reset", ["release", "offer", "withdraw"])
def test_departed_ownership_histories_do_not_consume_admission_budget(ledger, monkeypatch, reset):
    from brigade import worklore_scope

    conn, work_id, _ = ledger
    owned(conn, work_id)
    for index in range(2):
        other = another_owner(conn)
        assert checkpoint(conn, other, [f"retired/{index}"], node="node-b", nonce=NONCE_B)[0] == 200
        post(
            conn, other, {"action": "release", "generation": 1}, node="node-b", revision=3, key="release", nonce=NONCE_B
        )
        if reset in {"offer", "withdraw"}:
            post(conn, other, OFFER, admin=True, revision=4, key="reoffer")
        if reset == "withdraw":
            post(conn, other, {"action": "withdraw"}, admin=True, revision=5, key="withdraw")
    monkeypatch.setattr(worklore_scope, "MAX_SCOPE_SNAPSHOTS", 1)
    status, reply = checkpoint(conn, work_id, ["retired"])
    assert status == 200, reply
    assert reply["ownership"]["write_scope"] == ["retired"]
    assert event_count(conn, work_id) == 3


def test_withdrawn_histories_beyond_default_bound_allow_checkpoint(ledger):
    conn, work_id, _ = ledger
    owned(conn, work_id)
    # Real completed offer/withdraw cycles exceed the production 1,024-item bound.
    for _ in range(1025):
        item = store.create_item(conn, {"title": "Departed fixture", "kind": "repo"}, actor_id="admin")
        other = item["work_id"]
        post(conn, other, OFFER, admin=True, key="offer")
        post(conn, other, {"action": "withdraw"}, admin=True, revision=1, key="withdraw")
    status, reply = checkpoint(conn, work_id, ["src"])
    assert status == 200, reply
    assert event_count(conn, work_id) == 3


def test_active_overflow_refuses_until_explicit_holder_release(ledger, monkeypatch):
    from brigade import worklore_scope

    conn, work_id, _ = ledger
    owned(conn, work_id)
    other = another_owner(conn)
    another_owner(conn)
    monkeypatch.setattr(worklore_scope, "MAX_SCOPE_SNAPSHOTS", 1)
    before = snapshot(conn, work_id)
    status, reply = checkpoint(conn, work_id, ["src"])
    assert status == 409 and reply["code"] == "path-scope-capacity-exhausted"
    assert snapshot(conn, work_id) == before and event_count(conn, work_id) == 2
    post(conn, other, {"action": "release", "generation": 1}, node="node-b", revision=2, key="release", nonce=NONCE_B)
    status, reply = checkpoint(conn, work_id, ["src"])
    assert status == 200, reply
    assert event_count(conn, work_id) == 3


@pytest.mark.parametrize("repo", ["example/other", "EXAMPLE/Project", None, "example/../other"])
def test_repository_identity_is_assessed_before_peer_paths(ledger, repo):
    conn, work_id, _ = ledger
    owned(conn, work_id)
    assert checkpoint(conn, work_id, ["src"])[0] == 200
    other = another_owner(conn)
    row = conn.execute(
        "SELECT seq, detail_json FROM work_events WHERE work_id=? ORDER BY seq DESC LIMIT 1", (work_id,)
    ).fetchone()
    detail = json.loads(row[1])
    private = "token=" + "q" * 40
    detail["ownership"].update(repo_identity=repo, write_scope=[private])
    conn.execute("UPDATE work_events SET detail_json=? WHERE seq=?", (json.dumps(detail), row[0]))
    conn.commit()
    status, reply = checkpoint(conn, other, ["src"], node="node-b", nonce=NONCE_B)
    if repo == "example/other":
        assert status == 200, reply
        assert event_count(conn, other) == 3
    else:
        assert status == 409 and reply["code"] == "path-scope-unavailable"
        assert "conflicts" not in reply and event_count(conn, other) == 2
    assert private not in json.dumps(reply)


def test_serialized_byte_fence_precedes_scope_conflict_disclosure(ledger):
    conn, work_id, _ = ledger
    owned(conn, work_id)
    assert checkpoint(conn, work_id, ["src"])[0] == 200
    other = another_owner(conn)
    before = snapshot(conn, other)
    paths = ["src"] + [f"wide/{i:02d}" + "😀" * 240 for i in range(31)]
    status, reply = checkpoint(conn, other, paths, node="node-b", nonce=NONCE_B)
    assert status == 400 and reply["code"] == "field-bound", reply
    assert all(field not in reply for field in ("conflicts", "resolution", "truncated"))
    assert snapshot(conn, other) == before and event_count(conn, other) == 2


@pytest.mark.parametrize("wide", [False, True])
def test_conflict_output_is_bounded_and_does_not_evict_imported_duplicates(ledger, wide):
    conn, work_id, _ = ledger
    owned(conn, work_id)
    assert checkpoint(conn, work_id, ["src"])[0] == 200
    other = another_owner(conn)
    assert checkpoint(conn, other, ["docs"], node="node-b", nonce=NONCE_B)[0] == 200
    # Model pre-upgrade unchecked declarations without an importer/provider seam.
    row = conn.execute(
        "SELECT seq, detail_json FROM work_events WHERE work_id=? ORDER BY seq DESC LIMIT 1", (other,)
    ).fetchone()
    detail = json.loads(row[1])
    paths = [f"src/{i}" + "😀" * 240 for i in range(12)] if wide else [f"src/file-{i}.py" for i in range(32)]
    detail["ownership"].update(write_scope=paths, conflict_check="not-performed")
    conn.execute("UPDATE work_events SET detail_json=? WHERE seq=?", (json.dumps(detail), row[0]))
    conn.commit()
    before = snapshot(conn, other)
    status, reply = checkpoint(conn, work_id, ["src"], revision=3, key="new")
    assert status == 409 and reply["code"] == "path-scope-conflict"
    assert len(reply["conflicts"]) == (2 if wide else 8) and reply["truncated"] is True
    assert len(json.dumps(reply).encode()) < 8192
    assert snapshot(conn, other) == before and event_count(conn, work_id) == 3


@pytest.mark.parametrize("field", ["owner_node", "write_scope", "detail_json"])
def test_unsafe_imported_reservation_refuses_without_reflection(ledger, field):
    conn, work_id, _ = ledger
    owned(conn, work_id)
    assert checkpoint(conn, work_id, ["src"])[0] == 200
    other = another_owner(conn)
    row = conn.execute(
        "SELECT seq, detail_json FROM work_events WHERE work_id=? ORDER BY seq DESC LIMIT 1", (work_id,)
    ).fetchone()
    detail = json.loads(row[1])
    private = "token=" + "q" * 40
    if field == "detail_json":
        raw = '{"private": "' + private
    else:
        detail["ownership"][field] = [private] if field == "write_scope" else private
        raw = json.dumps(detail)
    conn.execute("UPDATE work_events SET detail_json=? WHERE seq=?", (raw, row[0]))
    conn.commit()
    status, reply = checkpoint(conn, other, ["src"], node="node-b", nonce=NONCE_B)
    assert status == 409 and reply["code"] == "path-scope-unavailable"
    assert private not in json.dumps(reply) and "conflicts" not in reply
    assert event_count(conn, other) == 2
