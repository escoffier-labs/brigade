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
    key = hmac.digest(master, b"brigade.findings.fingerprint.v1", "sha256")
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
    assert conn.execute("PRAGMA user_version").fetchone()[0] == fleet_hub.SCHEMA_VERSION
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


@pytest.mark.parametrize("platform", ["posix", "nt"])
def test_key_permission_bits_are_posix_only(tmp_path, monkeypatch, platform):
    import os
    import stat
    from types import SimpleNamespace

    conn = fleet_hub.init_db(tmp_path / "hub.db")
    try:
        expected = f._key(conn)
        # Windows reports synthetic mode bits, not POSIX access permissions.
        metadata = SimpleNamespace(st_mode=stat.S_IFREG | 0o666, st_nlink=1, st_uid=getattr(os, "getuid", lambda: 0)())
        monkeypatch.setattr(f, "os", SimpleNamespace(**{**vars(os), "name": platform, "fstat": lambda fd: metadata}))
        if platform == "posix":
            with pytest.raises(f.FindingError, match="unsafe findings key file"):
                f._key(conn)
        else:
            assert f._key(conn) == expected
    finally:
        conn.close()


def test_key_file_is_opened_in_binary_mode(tmp_path, monkeypatch):
    import os
    from types import SimpleNamespace

    binary = 0x8000  # Windows O_BINARY; absent on POSIX, so it is injected here.
    opened = []

    def recording_open(path, flags, *args):
        opened.append(flags)
        return os.open(path, flags & ~binary, *args)

    monkeypatch.setattr(f, "os", SimpleNamespace(**{**vars(os), "O_BINARY": binary, "open": recording_open}))
    conn = fleet_hub.init_db(tmp_path / "hub.db")
    try:
        f._key(conn)
    finally:
        conn.close()
    # Both the create and the read must avoid text-mode newline translation.
    assert len(opened) == 2
    assert all(flags & binary for flags in opened)


def test_pull_request_alias_shares_one_finding_and_reservation(tmp_path):
    conn = fleet_hub.init_db(tmp_path / "hub.db")
    owners = {"watcher": {"match": {"findings": ["pull-request", "pr"], "principals": ["watcher"]}}}
    try:
        first = f.handle(
            conn,
            {"action": "observe", "kind": "pull-request", "key": "Owner/Repo#12"},
            caller="agent:watcher",
            owners=owners,
            now=100,
        )
        alias = f.handle(
            conn,
            {"action": "observe", "kind": "pr", "key": "owner/repo#012"},
            caller="agent:watcher",
            owners=owners,
            now=101,
        )
        assert alias["finding_id"] == first["finding_id"]
        assert first["reservation"] is not None
        assert alias["reservation"] is None
        assert conn.execute("SELECT COUNT(*), MIN(kind) FROM fleet_findings").fetchone() == (1, "pull-request")
    finally:
        conn.close()


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://Example.test:443/jobs/1/", "https://example.test/jobs/1"),
        ("http://example.test:80/jobs/1", "https://example.test/jobs/1"),
        ("https://example.test:80/jobs/1", "https://example.test:80/jobs/1"),
        ("http://example.test:443/jobs/1", "https://example.test:443/jobs/1"),
        ("https://example.test:8443/jobs/1", "https://example.test:8443/jobs/1"),
        # A blank non-tracking parameter is part of the key; a blank tracking one is not.
        ("https://example.test/jobs?b=2&remote=&utm_source=", "https://example.test/jobs?b=2&remote="),
    ],
)
def test_job_url_keeps_ports_and_blank_parameters(raw, expected):
    assert f.canonical_key("job", raw) == expected


def _reserve(conn):
    owners = {"w": {"match": {"findings": ["email"], "principals": ["a", "b"]}}}
    first = f.handle(conn, {"action": "observe", "kind": "email", "key": "x@y"}, caller="agent:a", owners=owners, now=0)
    return (
        owners,
        first,
        {"action": "reported", "finding_id": first["finding_id"], **first["reservation"], "report_ref": "issue:1"},
    )


def test_review_live_holder_blocks_other_resolver(tmp_path):
    with fleet_hub.init_db(tmp_path / "hub.db") as conn:
        owners, first, report = _reserve(conn)
        with pytest.raises(f.FindingError) as error:
            f.handle(
                conn, {"action": "resolve", "finding_id": first["finding_id"]}, caller="agent:b", owners=owners, now=5
            )
        assert (error.value.status, error.value.code) == (409, "finding_reserved")
        assert f.handle(conn, report, caller="agent:a", owners=owners, now=6)["report_ref"] == "issue:1"


def test_review_admin_resolution_has_clear_report_error(tmp_path):
    with fleet_hub.init_db(tmp_path / "hub.db") as conn:
        owners, first, report = _reserve(conn)
        f.handle(
            conn,
            {"action": "resolve", "finding_id": first["finding_id"]},
            caller="admin",
            owners=owners,
            now=5,
            admin=True,
        )
        with pytest.raises(f.FindingError) as error:
            f.handle(conn, report, caller="agent:a", owners=owners, now=6)
        assert (error.value.status, error.value.code) == (409, "finding_resolved")


@pytest.mark.parametrize("now", [600, 1000000])
def test_review_former_holder_loses_resolve_authority(tmp_path, now):
    with fleet_hub.init_db(tmp_path / "hub.db") as conn:
        _, first, _ = _reserve(conn)
        owners = {"w": {"match": {"findings": ["email"], "principals": ["c"]}}}
        # Reassignment preserves a live lease, but not authority after its expiry.
        f.handle(
            conn, {"action": "resolve", "finding_id": first["finding_id"]}, caller="agent:a", owners=owners, now=599
        )
        conn.execute("UPDATE fleet_findings SET state='open'")
        conn.commit()
        with pytest.raises(f.FindingError) as error:
            f.handle(
                conn, {"action": "resolve", "finding_id": first["finding_id"]}, caller="agent:a", owners=owners, now=now
            )
        assert error.value.status == 403
        assert (
            f.handle(
                conn, {"action": "resolve", "finding_id": first["finding_id"]}, caller="agent:c", owners=owners, now=now
            )["state"]
            == "resolved"
        )


def test_review_pr_charter_alias_reserves_and_attributes(tmp_path):
    from brigade import fleet_hub_policy, fleet_policy, fleet_owners

    with fleet_hub.init_db(tmp_path / "hub.db") as conn:
        document = fleet_policy.empty_document()
        document["owners"] = {"w": {"label": "Watcher", "match": {"findings": ["pr"], "principals": ["a"]}}}
        fleet_hub_policy.save_policy(
            conn,
            document,
            expected_version=fleet_hub_policy.current_policy(conn)["revision"],
            actor="admin",
            reason="fixture",
        )
        first = f.handle(
            conn, {"action": "observe", "kind": "pr", "key": "o/r#1"}, caller="agent:a", owners=None, now=0
        )
        assert first["reservation"] is not None
        assert [item.ref for item in fleet_owners.load_view(conn, now_epoch=0).by_owner["w"]] == [first["finding_id"]]


def test_review_hmac_domains_do_not_overlap(tmp_path):
    with fleet_hub.init_db(tmp_path / "hub.db") as conn:
        _, first, _ = _reserve(conn)
        other = f.handle(
            conn,
            {"action": "observe", "kind": "report", "key": first["finding_id"]},
            caller="agent:a",
            owners={},
            now=1,
        )
        fingerprint = conn.execute(
            "SELECT fingerprint FROM fleet_findings WHERE finding_id=?", (other["finding_id"],)
        ).fetchone()[0]
        assert fingerprint != first["reservation"]["report_key"]


def test_review_key_crash_before_sync_does_not_publish(tmp_path, monkeypatch):
    import os

    with fleet_hub.init_db(tmp_path / "hub.db") as conn:
        original = os.fsync

        def crash(fd):
            raise OSError("simulated crash before sync")

        with monkeypatch.context() as patch:
            patch.setattr(f.os, "fsync", crash)
            with pytest.raises(OSError, match="simulated crash"):
                f._key(conn)
        assert not (tmp_path / "hub.db.findings-key").exists()
        assert not list(tmp_path.glob("*.tmp"))
        assert f._key(conn)
        assert original is os.fsync


def test_review_key_directory_is_synced(tmp_path, monkeypatch):
    from brigade import dirfd

    synced = []
    monkeypatch.setattr(dirfd, "fsync_directory", lambda fd: synced.append(fd))
    with fleet_hub.init_db(tmp_path / "hub.db") as conn:
        f._key(conn)
    assert len(synced) == 1


def test_review_key_docs_describe_windows_acl():
    from pathlib import Path

    text = Path("docs/hub-findings.md").read_text(encoding="utf-8")
    assert "Windows" in text and "ACL" in text


def test_review_port_zero_is_preserved():
    assert f.canonical_key("job", "https://example.test:0/x") == "https://example.test:0/x"


@pytest.mark.parametrize("populated", [False, True])
def test_short_key_is_not_silently_regenerated(tmp_path, populated):
    with fleet_hub.init_db(tmp_path / "hub.db") as conn:
        if populated:
            _reserve(conn)
        path = tmp_path / "hub.db.findings-key"
        path.write_bytes(b"short")
        path.chmod(0o600)
        with pytest.raises(f.FindingError, match="invalid findings key file"):
            f._key(conn)
        assert path.read_bytes() == b"short"


def test_key_publication_recovers_interrupted_extra_link(tmp_path):
    import os

    with fleet_hub.init_db(tmp_path / "hub.db") as conn:
        expected = f._key(conn)
        path = tmp_path / "hub.db.findings-key"
        os.link(path, path.with_name(path.name + ".tmp"))
        assert f._key(conn) == expected
        assert path.stat().st_nlink == 1


def test_secret_key_file_is_used_read_only_and_never_created(tmp_path, monkeypatch):
    import hashlib
    import hmac
    import os

    secret = tmp_path / "secrets" / "findings_key"
    secret.parent.mkdir(mode=0o700)
    monkeypatch.setenv(f.KEY_FILE_ENV, str(secret))
    conn = fleet_hub.init_db(tmp_path / "hub.db")
    body = {"action": "observe", "kind": "email", "key": "<Secret@Example.Test>"}
    with pytest.raises(f.FindingError, match="key file missing"):
        f.handle(conn, body, caller="admin", owners={}, now=0)
    assert not secret.exists()
    master = bytes(range(32))
    secret.write_bytes(master)
    os.chmod(secret, 0o444)
    f.handle(conn, body, caller="agent:one", owners={}, now=0)
    key = hmac.digest(master, b"brigade.findings.fingerprint.v1", "sha256")
    expected = hmac.new(key, b"email:secret@example.test", hashlib.sha256).hexdigest()
    assert conn.execute("SELECT fingerprint FROM fleet_findings").fetchone()[0] == expected
    assert not (tmp_path / "hub.db.findings-key").exists()
    if os.name == "posix":
        os.chmod(secret, 0o666)
        with pytest.raises(f.FindingError, match="unsafe"):
            f.handle(conn, body, caller="agent:one", owners={}, now=0)
        os.chmod(secret, 0o400)
    secret.chmod(0o600)
    secret.write_bytes(b"short")
    with pytest.raises(f.FindingError, match="invalid"):
        f.handle(conn, body, caller="agent:one", owners={}, now=0)
    conn.close()
