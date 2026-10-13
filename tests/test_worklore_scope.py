"""Atomic checkpoint admission at the authenticated HTTP/SQLite owner boundary."""

import base64
import hashlib
import json
import threading

import pytest

from brigade import fleet_hub
from brigade import worklore_store as store
from tests.test_worklore_ownership import (
    CHECKPOINT,
    NONCE_A,
    NONCE_B,
    NONCE_C,
    OFFER,
    dot_report,
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


def clone_histories(conn, template, count):
    """Expand one real lifecycle, including its template, in one durable transaction."""
    work_ids = [template]
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        item = conn.execute(
            f"SELECT {', '.join(store._ITEM_COLUMNS)} FROM work_items WHERE work_id=?", (template,)
        ).fetchone()
        events = conn.execute(
            f"SELECT {', '.join(store._EVENT_COLUMNS)} FROM work_events WHERE work_id=? ORDER BY seq", (template,)
        ).fetchall()
        for _ in range(count - 1):
            work_id = store._mint_id("wl-")
            values = dict(zip(store._ITEM_COLUMNS, item, strict=True))
            values["work_id"] = work_id
            conn.execute(
                f"INSERT INTO work_items ({', '.join(store._ITEM_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in store._ITEM_COLUMNS)})",
                tuple(values[column] for column in store._ITEM_COLUMNS),
            )
            for event in events:
                values = dict(zip(store._EVENT_COLUMNS, event, strict=True))
                values.update(work_id=work_id, event_id=store._mint_id("evt-"))
                detail = json.loads(values["detail_json"])
                if "ownership" in detail:
                    detail["ownership"]["last_seq"] = conn.execute(f"SELECT {store._EVENT_SEQ_SQL}").fetchone()[0]
                    values["detail_json"] = json.dumps(detail)
                conn.execute(
                    f"INSERT INTO work_events ({', '.join(store._EVENT_INSERT_COLUMNS)}) "
                    f"VALUES ({', '.join('?' for _ in store._EVENT_COLUMNS)}, {store._EVENT_SEQ_SQL})",
                    tuple(values[column] for column in store._EVENT_COLUMNS),
                )
            work_ids.append(work_id)
    return work_ids


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


def test_disjoint_maximum_scopes_keep_credential_work_bounded(ledger, monkeypatch):
    conn, work_id, _ = ledger
    owned(conn, work_id)
    # Accepted historical declarations exercise the full production scan bound.
    others = clone_histories(conn, another_owner(conn), 1024)
    with conn:
        for index, other in enumerate(others):
            row = conn.execute(
                "SELECT seq, detail_json FROM work_events WHERE work_id=? ORDER BY seq DESC LIMIT 1", (other,)
            ).fetchone()
            detail = json.loads(row[1])
            detail["ownership"].update(
                repo_identity="example/project",
                write_scope=[f"held/{index:04d}/{part:02d}/" + "0f" * 121 + "0" for part in range(32)],
            )
            conn.execute("UPDATE work_events SET detail_json=? WHERE seq=?", (json.dumps(detail), row[0]))
    paths = [f"candidate/{part:02d}/" + "0f" * 121 + "0" for part in range(32)]
    operations = {"decode": 0, "hash": 0}
    decode, sha256 = base64.b64decode, hashlib.sha256

    def count_decode(*args, **kwargs):
        operations["decode"] += 1
        return decode(*args, **kwargs)

    def count_hash(*args, **kwargs):
        operations["hash"] += 1
        return sha256(*args, **kwargs)

    with monkeypatch.context() as counting:
        counting.setattr(base64, "b64decode", count_decode)
        counting.setattr(hashlib, "sha256", count_hash)
        status, reply = checkpoint(conn, work_id, paths)
    assert status == 200, reply
    assert reply["ownership"]["write_scope"] == paths
    assert reply["ownership"]["conflict_check"] == "path-admission"
    assert snapshot(conn, work_id)["revision"] == 3 and event_count(conn, work_id) == 3
    # Allow ample normal header authentication/fingerprinting, independent of
    # peer path count. Disjoint declarations disclose no credential candidates.
    assert sum(operations.values()) <= 64, operations


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
    # Full histories from a real offer/withdraw cycle exceed the 1,024-item bound.
    item = store.create_item(conn, {"title": "Departed fixture", "kind": "repo"}, actor_id="admin")
    other = item["work_id"]
    post(conn, other, OFFER, admin=True, key="offer")
    post(conn, other, {"action": "withdraw"}, admin=True, revision=1, key="withdraw")
    clone_histories(conn, other, 1025)
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


@pytest.mark.parametrize("action", ["accept", "checkpoint", "report", "handoff"])
@pytest.mark.parametrize(
    "corruption",
    ["unowned", "offered", "wrong-state", "action", "owner", "principal", "unsafe-principal", "digest"],
)
def test_inconsistent_active_event_never_frees_reservation(ledger, action, corruption):
    conn, work_id, _ = ledger
    owned(conn, work_id)
    if action != "accept":
        assert checkpoint(conn, work_id, ["src"])[0] == 200
    if action == "report":
        post(
            conn,
            work_id,
            {"action": "report", "generation": 1, "report": dot_report()},
            revision=3,
            key="report",
            nonce=NONCE_A,
        )
    elif action == "handoff":
        post(
            conn,
            work_id,
            {"action": "handoff", "generation": 1, "target_node": "node-b"},
            revision=3,
            key="handoff",
            nonce=NONCE_A,
        )
    other = another_owner(conn)
    row = conn.execute(
        "SELECT seq, detail_json, node_id FROM work_events WHERE work_id=? ORDER BY seq DESC LIMIT 1", (work_id,)
    ).fetchone()
    detail = json.loads(row[1])
    detail["ownership"].update(repo_identity="example/project", write_scope=["src"])
    principal = row[2]
    if corruption in {"unowned", "offered", "wrong-state"}:
        detail["ownership"]["state"] = (
            ("owned" if action == "handoff" else "handoff-pending") if corruption == "wrong-state" else corruption
        )
    elif corruption == "action":
        detail["action"] = "release"
    elif corruption == "owner":
        detail["ownership"]["owner_node"] = "node-b"
    elif corruption in {"principal", "unsafe-principal"}:
        principal = "node-b" if corruption == "principal" else "token=" + "q" * 40
    else:
        detail["ownership"]["holder_hash"] = "not-a-digest"
    conn.execute("UPDATE work_events SET detail_json=?, node_id=? WHERE seq=?", (json.dumps(detail), principal, row[0]))
    conn.commit()
    before = snapshot(conn, other)
    status, reply = checkpoint(conn, other, ["src/module.py"], node="node-b", nonce=NONCE_B)
    assert status == 409 and reply["code"] == "path-scope-unavailable", reply
    assert "conflicts" not in reply and "q" * 40 not in json.dumps(reply)
    assert snapshot(conn, other) == before and event_count(conn, other) == 2


@pytest.mark.parametrize("field", ["work_id", "owner_node", "held_path", "requested_path"])
@pytest.mark.parametrize("encoding", ["digest-upper", "nonce-url", "nonce-hex"])
def test_stored_capability_cannot_be_reflected_to_another_same_node_holder(ledger, field, encoding):
    conn, work_id, _ = ledger
    decoded = base64.urlsafe_b64decode(NONCE_A + "=")
    digest = hashlib.sha256(decoded).hexdigest()
    private = {"digest-upper": digest.upper(), "nonce-url": NONCE_A, "nonce-hex": decoded.hex().upper()}[encoding]
    embedded = "fixture-" + private + "-tail"
    node = embedded if field == "owner_node" else "node-a"
    if field == "work_id":
        conn.execute("PRAGMA defer_foreign_keys=ON")
        conn.execute("UPDATE work_items SET work_id=? WHERE work_id=?", (embedded, work_id))
        conn.execute("UPDATE work_events SET work_id=? WHERE work_id=?", (embedded, work_id))
        conn.commit()
        work_id = embedded
    if node != "node-a":
        fleet_hub.add_node(conn, node, node)
    post(conn, work_id, {**OFFER, "target_node": node}, admin=True, key="offer")
    post(conn, work_id, {"action": "accept", "generation": 0}, node=node, revision=1, key="accept", nonce=NONCE_A)
    assert checkpoint(conn, work_id, ["src"], node=node)[0] == 200
    other = another_owner(conn, node=node)
    row = conn.execute(
        "SELECT seq, detail_json FROM work_events WHERE work_id=? ORDER BY seq DESC LIMIT 1", (work_id,)
    ).fetchone()
    detail = json.loads(row[1])
    held = "src/" + embedded + "/module.py" if field == "held_path" else "src"
    detail["ownership"]["write_scope"] = [held]
    conn.execute("UPDATE work_events SET detail_json=? WHERE seq=?", (json.dumps(detail), row[0]))
    conn.commit()
    requested = "src/" + embedded + "/module.py" if field == "requested_path" else "src"
    before = snapshot(conn, other)
    status, reply = checkpoint(conn, other, [requested], node=node, nonce=NONCE_B)
    assert status == 409 and reply["code"] == "path-scope-unavailable", reply
    assert "conflicts" not in reply
    assert all(secret not in json.dumps(reply) for secret in (private, digest, NONCE_A, NONCE_B))
    assert snapshot(conn, other) == before and event_count(conn, other) == 2
    # The separate item's legitimate nonce never authorizes the reserved item.
    status, reply = request(
        conn, work_id, {"action": "release", "generation": 1}, node=node, revision=3, key="release-other", nonce=NONCE_B
    )
    assert status == 403 and reply["code"] == "holder-mismatch"
    assert event_count(conn, work_id) == 3


@pytest.mark.parametrize("encoding", ["url", "url-padded", "standard", "standard-unpadded", "hex", "hex-upper"])
def test_embedded_common_nonce_spellings_are_not_reflected(ledger, encoding):
    conn, work_id, _ = ledger
    decoded = b"\xfb" + b"a" * 31
    nonce = base64.urlsafe_b64encode(decoded).decode().rstrip("=")
    standard = base64.b64encode(decoded).decode()
    private = {
        "url": nonce,
        "url-padded": nonce + "=",
        "standard": standard,
        "standard-unpadded": standard.rstrip("="),
        "hex": decoded.hex(),
        "hex-upper": decoded.hex().upper(),
    }[encoding]
    post(conn, work_id, OFFER, admin=True, key="offer")
    post(conn, work_id, {"action": "accept", "generation": 0}, revision=1, key="accept", nonce=nonce)
    assert checkpoint(conn, work_id, ["src"], nonce=nonce)[0] == 200
    other = another_owner(conn, node="node-a")
    row = conn.execute(
        "SELECT seq, detail_json FROM work_events WHERE work_id=? ORDER BY seq DESC LIMIT 1", (work_id,)
    ).fetchone()
    detail = json.loads(row[1])
    detail["ownership"]["write_scope"] = ["src/prefix" + private + "suffix/module.py"]
    conn.execute("UPDATE work_events SET detail_json=? WHERE seq=?", (json.dumps(detail), row[0]))
    conn.commit()
    status, reply = checkpoint(conn, other, ["src"], nonce=NONCE_B)
    assert status == 409 and reply["code"] == "path-scope-unavailable", reply
    assert "conflicts" not in reply and private not in json.dumps(reply)
    assert event_count(conn, other) == 2


@pytest.mark.parametrize("digest", [None, 42, "a" * 63, "A" * 64])
@pytest.mark.parametrize("scope", [[], ["other"]])
def test_malformed_holder_digest_refuses_even_empty_or_other_repository_scope(ledger, digest, scope):
    conn, work_id, _ = ledger
    owned(conn, work_id)
    other = another_owner(conn)
    row = conn.execute(
        "SELECT seq, detail_json FROM work_events WHERE work_id=? ORDER BY seq DESC LIMIT 1", (other,)
    ).fetchone()
    detail = json.loads(row[1])
    detail["ownership"].update(holder_hash=digest, repo_identity="example/other", write_scope=scope)
    conn.execute("UPDATE work_events SET detail_json=? WHERE seq=?", (json.dumps(detail), row[0]))
    conn.commit()
    status, reply = checkpoint(conn, work_id, ["src"])
    assert status == 409 and reply["code"] == "path-scope-unavailable", reply
    assert "conflicts" not in reply and event_count(conn, work_id) == 2


@pytest.mark.parametrize(
    ("field", "encoding", "protected"),
    [
        ("held_path", "url", "late"),  # Primary: B accepts A's nonce through B's real fenced API.
        ("held_path", "url", "early"),
        ("requested_path", "url", "late"),
        ("work_id", "url", "late"),
        ("owner_node", "url", "late"),
        ("held_path", "hex", "late"),
        ("held_path", "digest", "late"),
        ("requested_path", "digest-upper", "other-repo"),
        ("requested_path", "hex-upper", "empty"),
        ("work_id", "digest-upper", "other-repo"),
        ("owner_node", "digest", "handoff"),
        ("held_path", "url-padded", "empty"),
        ("held_path", "standard", "other-repo"),
        ("requested_path", "standard-unpadded", "handoff"),
        ("owner_node", "hex", "empty"),
        ("work_id", "hex-upper", "handoff"),
        ("held_path", "url", "caller"),
        ("requested_path", "digest-upper", "caller"),
        ("work_id", "hex", "caller"),
        ("owner_node", "digest", "caller"),
        ("held_path", "ascii", "late"),
        ("held_path", "ascii", "caller"),
        ("held_path", "url", "full-output"),
        ("held_path", "ascii", "invalid-after-full"),
    ],
)
def test_cross_holder_conflict_privacy_at_fenced_http_boundary(ledger, field, encoding, protected):
    conn, work_id, _ = ledger

    def rename_item(old, new):
        # Fixture identities force scan order and otherwise unwritable identity fields.
        conn.execute("PRAGMA defer_foreign_keys=ON")
        conn.execute("UPDATE work_items SET work_id=? WHERE work_id=?", (new, old))
        conn.execute("UPDATE work_events SET work_id=? WHERE work_id=?", (new, old))
        conn.commit()
        return new

    work_id = rename_item(work_id, "fixture-a-protected" if protected == "early" else "fixture-z-protected")
    owned(conn, work_id)
    if protected != "empty":
        assert (
            checkpoint(
                conn,
                work_id,
                ["protected"],
                repo="example/other" if protected in {"other-repo", "handoff"} else "example/project",
            )[0]
            == 200
        )
    if protected == "handoff":
        post(
            conn,
            work_id,
            {"action": "handoff", "generation": 1, "target_node": "node-b"},
            revision=3,
            key="handoff-protected",
            nonce=NONCE_A,
        )
    # C owns a different item as A's authenticated node, with a distinct capability.
    caller = store.create_item(conn, {"title": "Caller fixture", "kind": "repo"}, actor_id="admin")["work_id"]
    post(conn, caller, OFFER, admin=True, key="offer-caller")
    post(conn, caller, {"action": "accept", "generation": 0}, revision=1, key="accept-caller", nonce=NONCE_C)
    nonce = NONCE_C if protected == "caller" else NONCE_A
    decoded = base64.urlsafe_b64decode(nonce + "=")
    digest = hashlib.sha256(decoded).hexdigest()
    private = {
        "url": nonce,
        "url-padded": nonce + "=",
        "standard": base64.b64encode(decoded).decode(),
        "standard-unpadded": base64.b64encode(decoded).decode().rstrip("="),
        "hex": decoded.hex(),
        "hex-upper": decoded.hex().upper(),
        "digest": digest,
        "digest-upper": digest.upper(),
        "ascii": "q" * 80,
    }[encoding]
    embedded = "fixture-b-" + private + "-tail"
    carrier_node = embedded if field == "owner_node" else "node-b"
    if carrier_node != "node-b":
        fleet_hub.add_node(conn, carrier_node, carrier_node)
    carrier = another_owner(conn, node=carrier_node)
    carrier = rename_item(carrier, embedded if field == "work_id" else "fixture-b-carrier")
    held = "src/" + embedded + "/module.py" if field == "held_path" else "src"
    # No forged carrier snapshot: B legitimately checkpoints foreign private metadata.
    held_paths = (
        [held.replace("/module.py", f"/module-{index}.py") for index in range(9)]
        if protected in {"full-output", "invalid-after-full"}
        else [held]
    )
    assert checkpoint(conn, carrier, held_paths, node=carrier_node, nonce=NONCE_B)[0] == 200
    if protected == "invalid-after-full":
        row = conn.execute(
            "SELECT seq, detail_json FROM work_events WHERE work_id=? ORDER BY seq DESC LIMIT 1", (work_id,)
        ).fetchone()
        detail = json.loads(row[1])
        detail["ownership"]["holder_hash"] = "invalid-fixture-digest"
        conn.execute("UPDATE work_events SET detail_json=? WHERE seq=?", (json.dumps(detail), row[0]))
        conn.commit()
    requested = "src/" + embedded + "/module.py" if field == "requested_path" else "src"
    before, count = snapshot(conn, caller), event_count(conn, caller)
    protected_before, protected_count = snapshot(conn, work_id), event_count(conn, work_id)
    status, reply = checkpoint(conn, caller, [requested], nonce=NONCE_C)
    assert status == 409
    if encoding == "ascii" and protected != "invalid-after-full":
        assert reply["code"] == "path-scope-conflict"
        assert reply["conflicts"][0]["held_path"] == held
    else:
        assert reply["code"] == "path-scope-unavailable"
        assert "conflicts" not in reply and "truncated" not in reply
        assert all(secret not in json.dumps(reply) for secret in (private, digest, NONCE_A, NONCE_B, NONCE_C))
    assert len(json.dumps(reply).encode()) < 8192
    assert snapshot(conn, caller) == before and event_count(conn, caller) == count
    # Same-node authentication plus C's legitimate nonce cannot mutate A.
    status, reply = request(
        conn,
        work_id,
        {"action": "release", "generation": 1},
        revision=protected_before["revision"],
        key="release-by-caller",
        nonce=NONCE_C,
    )
    assert status == 403 and reply["code"] == "holder-mismatch"
    assert snapshot(conn, work_id) == protected_before and event_count(conn, work_id) == protected_count
