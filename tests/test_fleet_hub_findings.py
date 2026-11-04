"""Findings reservations, privacy and crash recovery."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from brigade import fleet_hub, fleet_hub_findings as f


def test_reservations_and_crash_recovery(tmp_path):
    path = tmp_path / "hub.db"
    fleet_hub.init_db(path).close()

    def observe():
        conn = fleet_hub.open_db(path)
        try:
            return f.handle(
                conn,
                {"action": "observe", "kind": "email", "key": "<EXAMPLE@MAIL>"},
                caller="agent:watcher",
                owners={"watcher": {"match": {"findings": ["email"], "principals": ["watcher"]}}},
                now=100,
            )
        finally:
            conn.close()

    with ThreadPoolExecutor(2) as pool:
        replies = list(pool.map(lambda _: observe(), range(2)))
    assert sum(r["reservation"] is not None for r in replies) == 1
    first = next(r for r in replies if r["reservation"])
    conn = fleet_hub.open_db(path)
    again = f.handle(
        conn,
        {"action": "observe", "kind": "email", "key": "example@mail"},
        caller="agent:watcher",
        owners={"watcher": {"match": {"findings": ["email"], "principals": ["watcher"]}}},
        now=701,
    )
    assert again["reservation"]["generation"] == 2
    assert again["reservation"]["report_key"] == first["reservation"]["report_key"]
    old = {"action": "reported", "finding_id": first["finding_id"], **first["reservation"], "report_ref": "issue:1"}
    old.pop("expires_at")
    old.pop("report_key")
    with pytest.raises(f.FindingError, match="expired"):
        f.handle(conn, old, caller="agent:watcher", owners={}, now=701)
    old.update(again["reservation"])
    old.pop("expires_at")
    old.pop("report_key")
    result = f.handle(conn, old, caller="agent:watcher", owners={}, now=1302)
    assert result["report_ref"] == "issue:1"
    with pytest.raises(f.FindingError) as error:
        f.handle(conn, old, caller="agent:watcher", owners={}, now=1303)
    assert error.value.code == "finding_already_reported"
    assert "example@mail" not in repr(conn.execute("SELECT * FROM fleet_findings").fetchall()).lower()
    conn.close()


@pytest.mark.parametrize(
    ("kind", "raw", "expected"),
    [
        ("email", "<Example@Mail>", "example@mail"),
        ("job", "http://EXAMPLE.COM/jobs/1/?utm_source=x&gclid=y&trk=feed&refId=tag#top", "https://example.com/jobs/1"),
        ("pull-request", "Owner/Repo#012", "owner/repo#12"),
        ("job", "ats:123", "ats:123"),
    ],
)
def test_canonical_keys(kind, raw, expected):
    assert f.canonical_key(kind, raw) == expected


def test_reassignment_and_resolve(tmp_path):
    conn = fleet_hub.init_db(tmp_path / "hub.db")
    owners = {"one": {"match": {"findings": ["job"], "principals": ["one"]}}}
    body = {"action": "observe", "kind": "job", "key": "ats:1"}
    first = f.handle(conn, body, caller="agent:one", owners=owners, now=0)
    owners = {"two": {"match": {"findings": ["job"], "principals": ["two"]}}}
    assert f.handle(conn, body, caller="agent:two", owners=owners, now=599)["reservation"] is None
    second = f.handle(conn, body, caller="agent:two", owners=owners, now=600)
    assert second["reservation"]["generation"] == 2
    with pytest.raises(f.FindingError):
        f.handle(conn, {"action": "resolve", "finding_id": first["finding_id"]}, caller="agent:one", owners=owners)
    assert (
        f.handle(conn, {"action": "resolve", "finding_id": first["finding_id"]}, caller="agent:two", owners=owners)[
            "state"
        ]
        == "resolved"
    )
    assert f.handle(conn, body, caller="agent:two", owners=owners)["reservation"] is None
    conn.close()


def test_key_loss_fails_closed(tmp_path):
    conn = fleet_hub.init_db(tmp_path / "hub.db")
    f.handle(conn, {"action": "observe", "kind": "email", "key": "private@example.test"}, caller="admin", owners={})
    (tmp_path / "hub.db.findings-key").unlink()
    with pytest.raises(f.FindingError, match="missing"):
        f.handle(conn, {"action": "observe", "kind": "email", "key": "private@example.test"}, caller="admin", owners={})
    conn.close()


def test_fingerprints_use_only_findings_domain(tmp_path):
    import hashlib
    import hmac

    conn = fleet_hub.init_db(tmp_path / "hub.db")
    body = {"action": "observe", "kind": "email", "key": "<Secret@Example.Test>"}
    result = f.handle(conn, body, caller="agent:one", owners={}, now=0)
    master = (tmp_path / "hub.db.findings-key").read_bytes()
    key = hmac.digest(master, b"brigade.findings.v1", "sha256")
    expected = hmac.new(key, b"email:secret@example.test", hashlib.sha256).hexdigest()
    assert conn.execute("SELECT fingerprint FROM fleet_findings").fetchone()[0] == expected
    assert result["reservation"] is None
    assert result["report_ref"] is None
    conn.close()


def test_grant_is_fenced_by_enrollment_and_transaction(tmp_path):
    from brigade import fleet_hub_messages as messages

    conn = fleet_hub.init_db(tmp_path / "hub.db")
    p = messages.add_principal(conn, {"name": "watcher"})
    grant = {"action": "grant", "principal_id": p["principal_id"], "enabled": True}
    f.handle(conn, grant, caller="admin", owners={}, admin=True)
    body = {"action": "observe", "kind": "email", "key": "example@mail"}
    f.handle(conn, body, caller=p["principal_id"], owners={}, enforce_auth=True)
    conn.execute("UPDATE fleet_message_principals SET authorization_enrollment_epoch='new-enrollment'")
    conn.commit()
    with pytest.raises(f.FindingError) as error:
        f.handle(conn, body, caller=p["principal_id"], owners={}, enforce_auth=True)
    assert error.value.status == 403
    conn.close()


def test_upgrade_from_48_adds_findings(tmp_path):
    path = tmp_path / "hub.db"
    conn = fleet_hub.init_db(path)
    conn.execute("DROP TABLE fleet_findings")
    conn.execute("DROP TABLE fleet_finding_grants")
    conn.execute("PRAGMA user_version=48")
    conn.commit()
    conn.close()
    conn = fleet_hub.init_db(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 49
    assert conn.execute("SELECT count(*) FROM fleet_findings").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM fleet_finding_grants").fetchone()[0] == 0
    conn.close()


def test_charter_findings_and_open_holdings(tmp_path):
    from brigade import fleet_hub_policy, fleet_policy, fleet_owners

    conn = fleet_hub.init_db(tmp_path / "hub.db")
    document = fleet_policy.empty_document()
    document["owners"] = {"watcher": {"label": "Watcher", "match": {"findings": ["email"], "principals": ["watcher"]}}}
    version = fleet_hub_policy.current_policy(conn)["revision"]
    fleet_hub_policy.save_policy(conn, document, expected_version=version, actor="admin", reason="fixture")
    finding = f.handle(
        conn, {"action": "observe", "kind": "email", "key": "example@mail"}, caller="agent:watcher", owners=None
    )
    view = fleet_owners.load_view(conn, now_epoch=0)
    assert [item.ref for item in view.by_owner["watcher"]] == [finding["finding_id"]]
    assert view.by_owner["watcher"][0].finding_kind == "email"
    f.handle(conn, {"action": "resolve", "finding_id": finding["finding_id"]}, caller="admin", owners=None, admin=True)
    assert not fleet_owners.load_view(conn, now_epoch=0).by_owner["watcher"]
    conn.close()
