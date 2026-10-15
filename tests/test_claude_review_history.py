"""History contracts at the offline transport and caller-owned SQLite boundary."""

import importlib
import json
import sqlite3

import pytest

from brigade.claude_review_import import GetResponse, Limits

HEAD = "a" * 40
OLD = "b" * 40
APP = {"id": 123, "slug": "example-review"}
ZERO = '<!-- bughunter-severity: {"normal": 0, "nit": 0, "pre_existing": 0} -->'
SUBJECT = dict(owner="example", repo="project", pr_number=7, expected_app_id=123, expected_app_slug="example-review")


def api():
    return importlib.import_module("brigade.claude_review_history")


def check(identity=10, **changes):
    row = dict(
        id=identity,
        name="Claude Code Review",
        app=APP,
        head_sha=HEAD,
        status="completed",
        conclusion="neutral",
        started_at="2026-01-01T00:00:00Z",
        completed_at="2026-01-02T00:00:00Z",
        html_url=f"https://github.com/example/project/runs/{identity}",
        output={"text": ZERO},
    )
    row.update(changes)
    return row


class FixtureGet:
    def __init__(self, checks=(), *, status=200, total=None, extras=None):
        self.checks = list(checks)
        self.status = status
        self.total = len(self.checks) if total is None else total
        self.extras = extras or {}
        self.calls = []

    def get(self, path, *, params, max_bytes):
        self.calls.append((path, dict(params), max_bytes))
        if path.endswith("/check-runs"):
            value = dict(total_count=self.total, check_runs=self.checks)
            return GetResponse(self.status, json.dumps(value).encode())
        return GetResponse(200, json.dumps(self.extras.get(path, [])).encode())


def ingest(conn, transport=None, *, observed="2026-01-03T00:00:00Z", head=HEAD, **options):
    return api().ingest_managed_review(
        conn, transport or FixtureGet([check()]), current_head=head, observed_at=observed, **(SUBJECT | options)
    )


def read(conn, *, head=HEAD, **options):
    return api().read_managed_review_history(conn, current_head=head, **(SUBJECT | options))


def test_persistence_exact_replay_normalization_and_revisions(tmp_path):
    path = tmp_path / "history.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA user_version=19")
        first = ingest(conn)
        replay = ingest(conn, observed="2026-01-02T19:00:00-05:00", owner="EXAMPLE", repo="PROJECT")
        assert first["observation_id"] == replay["observation_id"]
        assert first["duplicate"] is False and replay["duplicate"] is True
        ingest(conn, FixtureGet([check(conclusion="success")]), observed="2026-01-04T00:00:00Z")
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 19
    with sqlite3.connect(path) as conn:
        result = read(conn, owner="EXAMPLE", repo="PROJECT")
        assert len(result["history"]) == 2
        assert len(result["checks"]) == 1
        assert result["checks"][0]["conclusion"] == "success"
        assert result["checks"][0]["current_zero_findings"] is True
        json.dumps(result)


def test_mixed_case_source_url_survives_canonical_repository_replay():
    conn = sqlite3.connect(":memory:")
    url = "https://github.com/Example/Project/runs/10"
    first = ingest(conn, FixtureGet([check(html_url=url)]))
    replay = ingest(conn, FixtureGet([check(html_url=url)]), owner="EXAMPLE", repo="PROJECT")
    result = read(conn)
    assert result["repository"] == "example/project"
    assert result["checks"][0]["source_url"] == url
    assert result["history"][0]["checks"][0]["source_url"] == url
    assert len(result["history"]) == 1
    assert first["observation_id"] == replay["observation_id"]
    assert replay["duplicate"] is True


def _dict_row(cursor, row):
    return {column[0]: value for column, value in zip(cursor.description, row, strict=True)}


@pytest.mark.parametrize("row_factory", [None, sqlite3.Row, _dict_row], ids=["tuple", "row", "dict"])
@pytest.mark.parametrize("text_factory", [str, bytes], ids=["str", "bytes"])
@pytest.mark.parametrize("encoding", ["UTF-8", "UTF-16"])
def test_read_preserves_caller_factories_and_returns_json(row_factory, text_factory, encoding):
    with sqlite3.connect(":memory:") as conn:
        conn.execute(f"PRAGMA encoding='{encoding}'")
        conn.row_factory = row_factory
        conn.text_factory = text_factory
        conn.execute("PRAGMA user_version=19")
        first = ingest(conn)
        caller_cursor = conn.cursor()
        settings = (conn.in_transaction, conn.isolation_level, conn.total_changes)
        result = read(conn)
        assert json.loads(json.dumps(result)) == result
        assert result["history"][0]["observation_id"] == first["observation_id"]
        assert result["checks"][0]["current_zero_findings"] is True
        assert conn.row_factory is row_factory and conn.text_factory is text_factory
        assert (conn.in_transaction, conn.isolation_level, conn.total_changes) == settings
        for cursor in (caller_cursor, conn.cursor()):
            row = cursor.execute("SELECT 'caller' AS value").fetchone()
            if row_factory is None:
                assert isinstance(row, tuple)
                value = row[0]
            else:
                assert isinstance(row, dict if row_factory is _dict_row else sqlite3.Row)
                value = row["value"]
            assert value == (b"caller" if text_factory is bytes else "caller")
        version = conn.execute("PRAGMA user_version").fetchone()
        assert version[0 if row_factory is None else "user_version"] == 19


@pytest.mark.parametrize("text_factory", [bytearray, memoryview])
@pytest.mark.parametrize("encoding", ["UTF-8", "UTF-16"])
def test_byteslike_text_factories_preserve_settings_and_json_after_reopen(tmp_path, text_factory, encoding):
    path = tmp_path / "history.sqlite"
    for fresh in (True, False):
        with sqlite3.connect(path) as conn:
            if fresh:
                conn.execute(f"PRAGMA encoding='{encoding}'")
                conn.execute("PRAGMA user_version=19")
            conn.row_factory = sqlite3.Row
            conn.text_factory = text_factory
            if fresh:
                first = ingest(conn, FixtureGet([check()]))
            stored_encoding = bytes(conn.execute("PRAGMA encoding").fetchone()[0]).decode("utf-8")
            assert stored_encoding.startswith(encoding)
            caller_cursor = conn.cursor()
            settings = (conn.in_transaction, conn.isolation_level, conn.total_changes)
            result = read(conn)
            assert json.loads(json.dumps(result)) == result
            assert result["history"][0]["observation_id"] == first["observation_id"]
            assert isinstance(result["history"][0]["observation_id"], str)
            assert result["checks"][0]["current_zero_findings"] is True
            assert conn.row_factory is sqlite3.Row and conn.text_factory is text_factory
            assert (conn.in_transaction, conn.isolation_level, conn.total_changes) == settings
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 19
            assert bytes(conn.execute("PRAGMA encoding").fetchone()[0]).decode("utf-8") == stored_encoding
            for cursor in (caller_cursor, conn.cursor()):
                row = cursor.execute("SELECT 'caller' AS value").fetchone()
                assert isinstance(row, sqlite3.Row)
                assert isinstance(row["value"], text_factory)
                assert row["value"] == text_factory(b"caller")


def test_caller_head_survives_delayed_old_completion_and_insertion_order():
    conn = sqlite3.connect(":memory:")
    ingest(conn, FixtureGet([check(11)]), observed="2026-01-04T00:00:00Z")
    ingest(conn, FixtureGet([check(head_sha=OLD)]), head=OLD, observed="2026-01-05T00:00:00Z")
    ingest(conn, FixtureGet([check(11, status="in_progress")]), observed="2026-01-03T00:00:00Z")
    result = read(conn)
    assert result["current_head"] == HEAD
    current, stale = {r["check_id"]: r for r in result["checks"]}[11], {r["check_id"]: r for r in result["checks"]}[10]
    assert current["current_zero_findings"] is True
    assert stale["coverage"] == "stale" and stale["current_findings_known"] is False
    assert result["history"][0]["observed_at"] == "2026-01-05T00:00:00Z"
    pushed = read(conn, head="c" * 40)
    assert pushed["checks"][0]["current_findings_known"] is False
    assert all(not c["current_zero_findings"] for snapshot in pushed["history"] for c in snapshot["checks"])


def test_newer_same_head_round_survives_omission_and_old_completion():
    conn = sqlite3.connect(":memory:")
    ingest(conn)
    ingest(
        conn,
        FixtureGet([check(11, started_at="2026-01-03T00:00:00Z", status="in_progress", completed_at=None)]),
        observed="2026-01-04T00:00:00Z",
    )
    ingest(conn, observed="2026-01-05T00:00:00Z")
    rows = {r["check_id"]: r for r in read(conn)["checks"]}
    assert rows[10]["superseded_by"] == 11
    assert all(not r["current_findings_known"] and not r["current_zero_findings"] for r in rows.values())
    assert all(not c["current_zero_findings"] for snapshot in read(conn)["history"] for c in snapshot["checks"])


@pytest.mark.parametrize("status,total", [(403, None), (404, None), (429, None), (200, 2)])
def test_newest_incomplete_enumeration_invalidates_prior_zero(status, total):
    conn = sqlite3.connect(":memory:")
    ingest(conn)
    ingest(conn, FixtureGet([check()], status=status, total=total), observed="2026-01-04T00:00:00Z")
    result = read(conn)
    assert result["latest_check_enumeration_complete"] is False
    assert result["checks"][0]["current_zero_findings"] is False
    assert result["checks"][0]["current_findings_known"] is False


@pytest.mark.parametrize("other", [check(status="in_progress", completed_at=None), check(11)])
def test_tied_conflicting_observations_do_not_choose_by_digest(other):
    conn = sqlite3.connect(":memory:")
    ingest(conn)
    ingest(conn, FixtureGet([other]))
    result = read(conn)
    assert all(not r["current_findings_known"] for r in result["checks"])
    assert result["latest_check_enumeration_complete"] is False


@pytest.mark.parametrize("started", [None, "2026-01-01T00:00:00Z"])
def test_cross_snapshot_unknown_round_order_is_conservative(started):
    conn = sqlite3.connect(":memory:")
    ingest(conn)
    ingest(conn, FixtureGet([check(11, started_at=started)]), observed="2026-01-04T00:00:00Z")
    assert all(not r["current_zero_findings"] for r in read(conn)["checks"])


def test_bounded_read_truncation_and_subject_isolation():
    conn = sqlite3.connect(":memory:")
    ingest(conn)
    ingest(conn, observed="2026-01-04T00:00:00Z")
    ingest(conn, pr_number=8)
    ingest(conn, expected_app_slug="another-review")
    result = read(conn, limit=1)
    assert len(result["history"]) == 1 and result["history_truncated"] is True
    assert result["checks"][0]["current_zero_findings"] is False
    assert len(read(conn)["history"]) == 2
    assert read(conn, expected_app_id=456)["history"] == []
    statements = []
    conn.set_trace_callback(statements.append)
    read(conn, limit=1)
    assert any("LIMIT 2" in s.upper() for s in statements)


@pytest.mark.parametrize("omitted_day,order", [(2, [10, 12, 11]), (4, [10, 11, 12])], ids=["between", "after"])
def test_truncated_history_cannot_establish_round_supersession(omitted_day, order):
    conn = sqlite3.connect(":memory:")
    ingest(
        conn,
        FixtureGet([check(12, started_at=f"2026-01-0{omitted_day}T00:00:00Z", completed_at="2026-01-06T00:00:00Z")]),
        observed="2026-01-10T00:00:00Z",
    )
    annotation = dict(path="src/example.py", start_line=2, end_line=2, annotation_level="warning")
    for observed_day, identity, start_day in ((11, 11, 3), (12, 10, 1)):
        extras = {f"/repos/example/project/check-runs/{identity}/annotations": [annotation]}
        row = check(
            identity,
            started_at=f"2026-01-0{start_day}T00:00:00Z",
            completed_at="2026-01-06T00:00:00Z",
            output={"annotations_count": 1, "text": ZERO},
        )
        ingest(conn, FixtureGet([row], extras=extras), observed=f"2026-01-{observed_day}T00:00:00Z")
    complete = read(conn, limit=3)
    assert complete["history_truncated"] is False
    expected = {identity: newer for identity, newer in zip(order, [*order[1:], None], strict=True)}
    for row in complete["checks"] + [c for s in complete["history"] for c in s["checks"]]:
        assert row["superseded_by"] == expected[row["check_id"]]
        assert row["supersession_disposition"] == (
            "latest-observed" if expected[row["check_id"]] is None else "superseded"
        )
    truncated = read(conn, limit=2)
    assert truncated["history_truncated"] is True
    assert truncated["latest_check_enumeration_complete"] is False
    assert [s["observed_at"] for s in truncated["history"]] == [s["observed_at"] for s in complete["history"][:2]]
    assert {c["check_id"] for c in truncated["checks"]} == {10, 11}
    assert truncated["annotations"] == complete["annotations"]
    preserved = ("reviewed_head", "current_head", "coverage", "severity", "started_at", "source_url")
    for truncated_rows, complete_rows in (
        (truncated["checks"], complete["checks"]),
        (
            [c for s in truncated["history"] for c in s["checks"]],
            [c for s in complete["history"][:2] for c in s["checks"]],
        ),
    ):
        original = {c["check_id"]: c for c in complete_rows}
        for row in truncated_rows:
            assert {k: row[k] for k in preserved} == {k: original[row["check_id"]][k] for k in preserved}
            assert row["current_findings_known"] is False
            assert row["current_zero_findings"] is False
            assert row["supersession_disposition"] == "unknown"
            assert row["superseded_by"] is None


@pytest.mark.parametrize("readonly", [False, True])
@pytest.mark.parametrize("active_transaction", [False, True])
def test_missing_schema_read_is_empty_without_writes_or_transaction_changes(tmp_path, readonly, active_transaction):
    path = tmp_path / "empty.sqlite"
    sqlite3.connect(path).close()
    with sqlite3.connect(f"file:{path}?mode={'ro' if readonly else 'rw'}", uri=True) as conn:
        if active_transaction:
            conn.execute("BEGIN")
        result = read(conn)
        assert all(result[kind] == [] for kind in ("history", "checks", "annotations", "reviews", "comments"))
        assert result["repository"] == "example/project"
        assert result["current_head"] == HEAD
        assert result["history_truncated"] is False
        assert result["latest_check_enumeration_complete"] is False
        assert result["finding_totals"] is None
        json.dumps(result)
        assert conn.execute("SELECT name FROM sqlite_master").fetchall() == []
        assert conn.total_changes == 0
        assert conn.in_transaction is active_transaction


@pytest.mark.parametrize(
    "options",
    [
        {"limit": True},
        {"limit": 0},
        {"limit": 101},
        {"owner": "../x"},
        {"repo": ".."},
        {"pr_number": True},
        {"current_head": "main"},
        {"expected_app_id": 0},
        {"expected_app_slug": "github-actions"},
    ],
)
def test_invalid_read_values_are_rejected(options):
    conn = sqlite3.connect(":memory:")
    values = SUBJECT | dict(current_head=HEAD) | options
    with pytest.raises(ValueError):
        api().read_managed_review_history(conn, **values)


def test_ingest_invalid_time_does_not_collect_or_write():
    conn = sqlite3.connect(":memory:")
    transport = FixtureGet()
    with pytest.raises(ValueError):
        ingest(conn, transport, observed="yesterday")
    assert transport.calls == []
    assert conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []


def test_schema_and_savepoint_preserve_caller_rollback_and_unrelated_data():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE caller(value)")
    conn.execute("INSERT INTO caller VALUES ('pending')")
    api().ensure_schema(conn)
    ingest(conn)
    assert conn.in_transaction
    conn.rollback()
    assert conn.execute("SELECT * FROM caller").fetchall() == []
    assert read(conn)["history"] == []
    ingest(conn)
    conn.rollback()
    assert read(conn)["history"] == []


@pytest.mark.parametrize("pending_caller_write", [False, True])
@pytest.mark.parametrize("reject_append", [False, True])
def test_ingest_schema_and_append_remain_in_caller_transaction(pending_caller_write, reject_append):
    conn = sqlite3.connect(":memory:")
    if pending_caller_write:
        conn.execute("CREATE TABLE caller(value)")
        conn.execute("INSERT INTO caller VALUES ('pending')")
    if reject_append:

        def deny_append(action, table, *_):
            return (
                sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_INSERT and table == api()._TABLE else sqlite3.SQLITE_OK
            )

        conn.set_authorizer(deny_append)
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            ingest(conn)
        # Python 3.10 cannot disable an authorizer with None.
        conn.set_authorizer(lambda *_: sqlite3.SQLITE_OK)
        assert conn.execute(f"SELECT count(*) FROM {api()._TABLE}").fetchone()[0] == 0
    else:
        ingest(conn)
        assert conn.execute(f"SELECT count(*) FROM {api()._TABLE}").fetchone()[0] == 1
    assert conn.in_transaction
    if pending_caller_write:
        assert conn.execute("SELECT * FROM caller").fetchall() == [("pending",)]
    conn.rollback()
    assert not conn.in_transaction
    assert conn.execute("SELECT name FROM sqlite_master WHERE name=?", (api()._TABLE,)).fetchall() == []
    if pending_caller_write:
        assert conn.execute("SELECT * FROM caller").fetchall() == []


def test_sqlite_transaction_abort_preserves_original_ingest_error():
    conn = sqlite3.connect(":memory:")
    api().ensure_schema(conn)
    conn.execute(
        f"CREATE TRIGGER abort_ingest BEFORE INSERT ON {api()._TABLE} "
        "BEGIN SELECT RAISE(ROLLBACK, 'fixture transaction abort'); END"
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="^fixture transaction abort$"):
        ingest(conn)
    assert not conn.in_transaction
    assert conn.execute(f"SELECT count(*) FROM {api()._TABLE}").fetchone()[0] == 0
    assert read(conn)["history"] == []


def test_quotas_refuse_atomic_write_without_pruning_and_allow_replay():
    conn = sqlite3.connect(":memory:")
    first = ingest(conn)
    for second in range(1, 200):
        ingest(conn, observed=f"2026-01-03T00:{second // 60:02}:{second % 60:02}Z")
    with pytest.raises(ValueError, match="subject"):
        ingest(conn, observed="2026-01-04T00:00:00Z")
    assert ingest(conn)["duplicate"] is True
    assert read(conn, limit=100)["history_truncated"] is True
    # Seed the global cap from one real sanitized row, without 20,000 GET collections.
    table = "brigade_managed_review_history"
    conn.execute(
        f"INSERT INTO {table} SELECT printf('%064x', n), 'other/project', n, app_id, app_slug, observed_at, observed_sort, current_head, payload FROM {table}, (WITH RECURSIVE seq(n) AS (VALUES(1000) UNION ALL SELECT n+1 FROM seq WHERE n<20799) SELECT n FROM seq) WHERE observation_id=?",
        (first["observation_id"],),
    )
    with pytest.raises(ValueError, match="global"):
        ingest(conn, pr_number=9)
    assert read(conn, pr_number=9)["history"] == []
    assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 20000


def test_snapshot_byte_cap_refuses_without_partial_row():
    conn = sqlite3.connect(":memory:")
    rows = [dict(path="x" * 500, start_line=1, end_line=1, annotation_level="warning") for _ in range(600)]
    extras = {"/repos/example/project/check-runs/10/annotations": rows}
    with pytest.raises(ValueError, match="snapshot"):
        ingest(conn, FixtureGet([check()], extras=extras), limits=Limits(per_page=100))
    assert read(conn)["history"] == []


def test_stored_redaction_annotation_findings_and_unverified_objects():
    conn = sqlite3.connect(":memory:")
    extras = {
        "/repos/example/project/check-runs/10/annotations": [
            dict(
                path="src/example.py",
                start_line=2,
                end_line=2,
                annotation_level="warning",
                message="PRIVATE secret",
                raw_details="PRIVATE",
            )
        ],
        "/repos/example/project/pulls/7/reviews": [dict(id=60, commit_id=OLD, state="COMMENTED", body="PRIVATE")],
        "/repos/example/project/pulls/7/comments": [
            dict(id=50, original_commit_id=OLD, path="src/example.py", line=2, side="RIGHT", body="PRIVATE")
        ],
        "/repos/example/project/issues/7/comments": [
            dict(id=40, performed_via_github_app=APP, user={"type": "Bot"}, body="PRIVATE")
        ],
    }
    ingest(conn, FixtureGet([check(output={"annotations_count": 1, "text": "PRIVATE"})], extras=extras))
    result = read(conn, head=OLD)
    assert result["checks"][0]["current_zero_findings"] is False
    assert result["annotations"][0]["coverage"] == "stale"
    assert result["annotations"][0]["location"]["start_line"] == 2
    assert result["annotations"][0]["ambiguity"] == "no-universal-immutable-id"
    assert result["reviews"][0]["coverage"] == "current"
    assert result["reviews"][0]["provider_identity"] == "unverified"
    assert all(r["thread_disposition"] == "not-collected" for r in result["comments"])
    assert result["finding_totals"] is None and "approval" not in result
    assert "PRIVATE" not in " ".join(conn.iterdump())
    assert "PRIVATE" not in json.dumps(result)


def test_fractional_observation_time_orders_after_whole_second():
    conn = sqlite3.connect(":memory:")
    ingest(conn, FixtureGet([check(status="in_progress", completed_at=None)]), observed="2026-01-03T00:00:00.1Z")
    ingest(conn)
    result = read(conn)
    assert result["history"][0]["observed_at"] == "2026-01-03T00:00:00.100000Z"
    assert result["checks"][0]["lifecycle"] == "in_progress"
    assert result["checks"][0]["current_zero_findings"] is False


def test_annotation_ordinal_relocation_keeps_history_and_projection_ambiguity():
    conn = sqlite3.connect(":memory:")
    endpoint = "/repos/example/project/check-runs/10/annotations"
    for day, line in ((3, 2), (4, 3)):
        annotation = dict(path="src/example.py", start_line=line, end_line=line, annotation_level="warning")
        ingest(conn, FixtureGet([check()], extras={endpoint: [annotation]}), observed=f"2026-01-0{day}T00:00:00Z")
    result = read(conn)
    assert len(result["annotations"]) == 1
    assert [s["annotations"][0]["location"]["start_line"] for s in result["history"]] == [3, 2]
    assert result["annotations"][0]["ambiguity"] == "ordinal-location-revised"


def test_relocated_annotation_keeps_duplicate_representation_ambiguity():
    conn = sqlite3.connect(":memory:")
    for day, line in ((3, 2), (4, 3)):
        annotation = dict(path="src/example.py", start_line=line, end_line=line, annotation_level="warning")
        inline = dict(id=50, path="src/example.py", line=line, side="RIGHT", original_commit_id=HEAD)
        extras = {
            "/repos/example/project/check-runs/10/annotations": [annotation],
            "/repos/example/project/pulls/7/comments": [inline],
        }
        ingest(conn, FixtureGet([check()], extras=extras), observed=f"2026-01-0{day}T00:00:00Z")
    result = read(conn)
    assert len(result["annotations"]) == 1
    assert result["annotations"][0]["location"]["start_line"] == 3
    assert result["annotations"][0]["ambiguity"] == "possible-duplicate-representation"
    assert result["annotations"][0]["location_disposition"] == "ordinal-location-revised"
    assert [s["annotations"][0]["location"]["start_line"] for s in result["history"]] == [3, 2]
    assert all(s["annotations"][0]["ambiguity"] == "possible-duplicate-representation" for s in result["history"])


def test_later_completed_round_can_be_current_when_older_round_is_omitted():
    conn = sqlite3.connect(":memory:")
    ingest(conn)
    ingest(conn, FixtureGet([check(11, started_at="2026-01-02T00:00:00Z")]), observed="2026-01-04T00:00:00Z")
    rows = {r["check_id"]: r for r in read(conn)["checks"]}
    assert rows[10]["current_zero_findings"] is False
    assert rows[10]["superseded_by"] == 11
    assert rows[11]["current_findings_known"] is True
    assert rows[11]["current_zero_findings"] is True


def test_invalid_collection_limits_do_not_collect_or_write():
    conn = sqlite3.connect(":memory:")
    transport = FixtureGet()
    with pytest.raises(ValueError):
        ingest(conn, transport, limits={"max_pages": 1})
    assert transport.calls == []
    assert conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []


def test_tied_object_head_conflict_has_unknown_projection_coverage():
    conn = sqlite3.connect(":memory:")
    ingest(conn)
    ingest(conn, FixtureGet([check(head_sha=OLD)]))
    row = read(conn)["checks"][0]
    assert row["history_disposition"] == "conflicting-observations"
    assert row["reviewed_head"] is None
    assert row["coverage"] == "unknown"
    assert row["current_findings_known"] is False


def _unknown_check(row):
    assert row["coverage"] == "unknown"
    assert row["current_findings_known"] is False and row["current_zero_findings"] is False


@pytest.mark.parametrize("newer", [HEAD, OLD], ids=["head-newer", "old-newer"])
def test_cross_head_check_identity_is_unknown_and_keeps_both_claims(newer):
    conn = sqlite3.connect(":memory:")
    endpoint = "/repos/example/project/check-runs/10/annotations"
    annotation = dict(path="src/example.py", start_line=2, end_line=2, annotation_level="warning")
    for head, day, others in (
        (OLD, 3 + (newer == OLD), []),
        (HEAD, 3 + (newer == HEAD), [check(11, started_at="2026-01-02T00:00:00Z")]),
    ):
        row = check(head_sha=head, output={"annotations_count": 1, "text": ZERO})
        ingest(
            conn,
            FixtureGet([row, *others], extras={endpoint: [annotation]}),
            head=head,
            observed=f"2026-01-0{day}T00:00:00Z",
        )
    result = read(conn)
    rows = {r["check_id"]: r for r in result["checks"]}
    conflicted, independent = rows[10], rows[11]
    assert conflicted["reviewed_head"] is None and conflicted["history_disposition"] == "conflicting-observations"
    assert conflicted["supersession_disposition"] == "unknown" and conflicted["superseded_by"] is None
    _unknown_check(conflicted)
    assert independent["coverage"] == "current" and independent["current_zero_findings"] is True
    assert result["annotations"][0]["reviewed_head"] is None and result["annotations"][0]["coverage"] == "unknown"
    history = [c for s in result["history"] for c in s["checks"] if c["check_id"] == 10]
    assert {c["reviewed_head"] for c in history} == {OLD, HEAD}
    for c in history:
        _unknown_check(c)
        assert c["supersession_disposition"] == "unknown" and c["superseded_by"] is None
        assert c["severity"]["disposition"] == "known" and c["source_url"].endswith("/runs/10")
    assert {a["reviewed_head"] for s in result["history"] for a in s["annotations"]} == {OLD, HEAD}
    assert all(a["coverage"] == "unknown" for s in result["history"] for a in s["annotations"])


def test_later_known_head_replaces_earlier_missing_head():
    conn = sqlite3.connect(":memory:")
    ingest(conn, FixtureGet([check(head_sha=None)]), observed="2026-01-03T00:00:00Z")
    ingest(conn, observed="2026-01-04T00:00:00Z")
    row = read(conn)["checks"][0]
    assert row["reviewed_head"] == HEAD and row["history_disposition"] == "latest-observed"
    assert row["coverage"] == "current" and row["current_zero_findings"] is True


@pytest.mark.parametrize("second", [None, "not-a-sha"], ids=["missing", "malformed"])
def test_unreviewed_head_checks_never_establish_supersession(second):
    conn = sqlite3.connect(":memory:")
    rows = [
        check(10, head_sha=None, started_at="2026-01-01T00:00:00Z"),
        check(11, head_sha=second, started_at="2026-01-01T12:00:00Z"),
        check(12, started_at="2026-01-02T00:00:00Z"),
    ]
    ingest(conn, FixtureGet(rows))
    result = read(conn)
    for row in (*result["checks"], *result["history"][0]["checks"]):
        if row["check_id"] == 12:
            assert row["coverage"] == "current" and row["supersession_disposition"] == "latest-observed"
        else:
            _unknown_check(row)
            assert row["supersession_disposition"] == "unknown" and row["superseded_by"] is None


@pytest.mark.parametrize("newest_head", [OLD, None], ids=["contradicted", "missing"])
@pytest.mark.parametrize("omitted", [False, True], ids=["present", "omitted"])
def test_uncertain_later_head_claim_cannot_restore_older_peer_confidence(newest_head, omitted):
    conn = sqlite3.connect(":memory:")
    later = check(10, started_at="2026-01-01T12:00:00Z")
    ingest(conn, FixtureGet([later, check(11)]))
    assert {r["check_id"]: r for r in read(conn)["checks"]}[11]["superseded_by"] == 10
    ingest(conn, FixtureGet([check(10, head_sha=newest_head)]), head=OLD, observed="2026-01-04T00:00:00Z")
    ingest(conn, FixtureGet([check(11)] if omitted else [later, check(11)]), observed="2026-01-05T00:00:00Z")
    result = read(conn)
    for row in (*result["checks"], *(c for s in result["history"] for c in s["checks"])):
        assert row["supersession_disposition"] == "unknown" and row["superseded_by"] is None
        assert row["current_findings_known"] is False and row["current_zero_findings"] is False
    claims = [c for s in result["history"] for c in s["checks"] if c["check_id"] == 10]
    assert {c["reviewed_head"] for c in claims} == {HEAD, newest_head}
    assert {c["started_at"] for c in claims} == {later["started_at"], check()["started_at"]}


@pytest.mark.parametrize("tied_observation", [False, True], ids=["different-observations", "tied-observations"])
@pytest.mark.parametrize("peer_day", [2, 3, 4], ids=["between", "tied-start", "known-later"])
@pytest.mark.parametrize("omitted", [False, True], ids=["present", "omitted"])
def test_all_start_claims_bound_peer_order_without_digest_selection(tied_observation, peer_day, omitted):
    conn = sqlite3.connect(":memory:")
    first = check(10, completed_at="2026-01-03T00:00:00Z")
    second = check(10, started_at="2026-01-03T00:00:00Z", completed_at="2026-01-03T00:00:00Z")
    ingest(conn, FixtureGet([first]), observed=f"2026-01-0{4 if tied_observation else 3}T00:00:00Z")
    ingest(conn, FixtureGet([second]), observed="2026-01-04T00:00:00Z")
    peer = check(100, started_at=f"2026-01-0{peer_day}T00:00:00Z", completed_at="2026-01-04T00:00:00Z")
    transport = FixtureGet([peer] if omitted else [first, peer])
    ingest(conn, transport, observed="2026-01-05T00:00:00Z")
    replay = ingest(conn, transport, observed="2026-01-05T00:00:00Z")
    assert replay["duplicate"] is True
    result = read(conn)
    rows = {r["check_id"]: r for r in result["checks"]}
    uncertain = rows[10]
    assert uncertain["supersession_disposition"] == "unknown" and uncertain["superseded_by"] is None
    assert uncertain["current_findings_known"] is False and uncertain["current_zero_findings"] is False
    assert rows[100]["supersession_disposition"] == ("latest-observed" if peer_day == 4 else "unknown")
    assert rows[100]["superseded_by"] is None
    assert rows[100]["current_findings_known"] is (peer_day == 4)
    assert rows[100]["current_zero_findings"] is (peer_day == 4)
    for snapshot in result["history"]:
        for row in snapshot["checks"]:
            assert row["supersession_disposition"] == rows[row["check_id"]]["supersession_disposition"]
            assert row["superseded_by"] is None
            if row["check_id"] == 10 or peer_day != 4:
                assert row["current_findings_known"] is False and row["current_zero_findings"] is False
    assert {c["started_at"] for s in result["history"] for c in s["checks"] if c["check_id"] == 10} == {
        first["started_at"],
        second["started_at"],
    }


def test_newest_missing_head_keeps_older_claim_in_peer_order():
    conn = sqlite3.connect(":memory:")
    later = check(10, started_at="2026-01-01T12:00:00Z")
    ingest(conn, FixtureGet([later, check(11)]))
    ingest(
        conn,
        FixtureGet([check(10, head_sha=None, started_at=later["started_at"])]),
        head=OLD,
        observed="2026-01-04T00:00:00Z",
    )
    ingest(conn, FixtureGet([check(11)]), observed="2026-01-05T00:00:00Z")
    result = read(conn)
    assert {r["check_id"]: r for r in result["checks"]}[10]["reviewed_head"] is None
    for row in (*result["checks"], *(c for s in result["history"] for c in s["checks"])):
        assert row["supersession_disposition"] == "unknown" and row["superseded_by"] is None
        assert row["current_findings_known"] is False and row["current_zero_findings"] is False


def test_rerun_after_all_uncertain_starts_can_establish_current_zero():
    conn = sqlite3.connect(":memory:")
    ingest(conn, FixtureGet([check(10), check(11, started_at="2026-01-01T12:00:00Z")]))
    ingest(conn, FixtureGet([check(10, head_sha=OLD)]), head=OLD, observed="2026-01-04T00:00:00Z")
    rerun = check(12, started_at="2026-01-02T00:00:00Z")
    ingest(conn, FixtureGet([rerun]), observed="2026-01-05T00:00:00Z")
    rows = {r["check_id"]: r for r in read(conn)["checks"]}
    assert rows[10]["supersession_disposition"] == "unknown" and rows[10]["superseded_by"] is None
    assert rows[11]["superseded_by"] == 12 and rows[11]["current_zero_findings"] is False
    assert rows[12]["supersession_disposition"] == "latest-observed" and rows[12]["current_zero_findings"] is True


@pytest.mark.parametrize(
    "identity,selected_missing", [(10, False), (11, True)], ids=["selected-valid", "selected-missing"]
)
@pytest.mark.parametrize("omitted", [False, True], ids=["present", "omitted"])
def test_tied_missing_start_blocks_peer_for_both_selected_revisions(identity, selected_missing, omitted):
    conn = sqlite3.connect(":memory:")
    peer = check(100, started_at="2026-01-02T00:00:00Z")
    for start in (None, "2026-01-01T00:00:00Z"):
        ingest(
            conn,
            FixtureGet([check(identity, started_at=start), *([] if omitted else [peer])]),
            observed="2026-01-04T00:00:00Z",
        )
    if omitted:
        ingest(conn, FixtureGet([peer]), observed="2026-01-05T00:00:00Z")
    result = read(conn)
    rows = {r["check_id"]: r for r in result["checks"]}
    assert (rows[identity]["started_at"] is None) is selected_missing
    assert rows[identity]["history_disposition"] == "conflicting-observations"
    assert {c["started_at"] for s in result["history"] for c in s["checks"] if c["check_id"] == identity} == {
        None,
        "2026-01-01T00:00:00Z",
    }
    for row in (*result["checks"], *(c for s in result["history"] for c in s["checks"])):
        assert row["supersession_disposition"] == "unknown" and row["superseded_by"] is None
        assert row["current_findings_known"] is False and row["current_zero_findings"] is False


@pytest.mark.parametrize("omitted", [False, True], ids=["present", "omitted"])
def test_historical_missing_start_on_contradicted_run_blocks_later_peer(omitted):
    conn = sqlite3.connect(":memory:")
    ingest(conn, FixtureGet([check(10, started_at=None)]))
    ingest(conn, FixtureGet([check(10, head_sha=OLD)]), head=OLD, observed="2026-01-04T00:00:00Z")
    peer = check(100, started_at="2026-01-02T00:00:00Z")
    ingest(conn, FixtureGet([peer] if omitted else [check(10), peer]), observed="2026-01-05T00:00:00Z")
    result = read(conn)
    uncertain = {r["check_id"]: r for r in result["checks"]}[10]
    assert uncertain["history_disposition"] == "conflicting-observations" and uncertain["reviewed_head"] is None
    assert {c["started_at"] for s in result["history"] for c in s["checks"] if c["check_id"] == 10} == {
        None,
        "2026-01-01T00:00:00Z",
    }
    for row in (*result["checks"], *(c for s in result["history"] for c in s["checks"])):
        assert row["supersession_disposition"] == "unknown" and row["superseded_by"] is None
        assert row["current_findings_known"] is False and row["current_zero_findings"] is False


@pytest.mark.parametrize("initial_head", [HEAD, None], ids=["known-head", "missing-head"])
def test_unique_latest_valid_start_recovers_certain_run_metadata(initial_head):
    conn = sqlite3.connect(":memory:")
    ingest(conn, FixtureGet([check(10, head_sha=initial_head, started_at=None)]))
    ingest(conn, FixtureGet([check(10)]), observed="2026-01-04T00:00:00Z")
    recovered = read(conn)["checks"][0]
    assert recovered["history_disposition"] == "latest-observed" and recovered["reviewed_head"] == HEAD
    assert recovered["supersession_disposition"] == "latest-observed" and recovered["current_zero_findings"] is True
    ingest(conn, FixtureGet([check(100, started_at="2026-01-02T00:00:00Z")]), observed="2026-01-05T00:00:00Z")
    result = read(conn)
    rows = {r["check_id"]: r for r in result["checks"]}
    assert rows[10]["supersession_disposition"] == "superseded" and rows[10]["superseded_by"] == 100
    assert rows[100]["supersession_disposition"] == "latest-observed" and rows[100]["current_zero_findings"] is True
    historical = [c for s in result["history"] for c in s["checks"] if c["check_id"] == 10 and c["started_at"] is None]
    assert len(historical) == 1 and historical[0]["superseded_by"] == 100
    assert historical[0]["current_findings_known"] is False and historical[0]["current_zero_findings"] is False


@pytest.mark.parametrize("missing_head", [None, "not-a-sha"], ids=["missing", "malformed"])
@pytest.mark.parametrize("omitted", [False, True], ids=["present", "omitted"])
@pytest.mark.parametrize("peer_later", [False, True], ids=["older-peer", "known-later-peer"])
def test_headless_listing_is_only_an_uncertain_rival(missing_head, omitted, peer_later):
    conn = sqlite3.connect(":memory:")
    headless = check(10, head_sha=missing_head, started_at="2026-01-01T12:00:00Z")
    peer = check(11, started_at=f"2026-01-0{2 if peer_later else 1}T00:00:00Z")
    ingest(conn, FixtureGet([headless, peer]))
    ingest(conn, FixtureGet([peer] if omitted else [headless, peer]), observed="2026-01-05T00:00:00Z")
    result = read(conn)
    rows = {r["check_id"]: r for r in result["checks"]}
    for row in (*result["checks"], *(c for s in result["history"] for c in s["checks"])):
        if row["check_id"] == 10:
            _unknown_check(row)
            assert row["reviewed_head"] is None
            assert row["supersession_disposition"] == "unknown" and row["superseded_by"] is None
        else:
            assert row["reviewed_head"] == HEAD and row["coverage"] == "current"
            assert row["supersession_disposition"] == ("latest-observed" if peer_later else "unknown")
            assert row["superseded_by"] is None
            if not peer_later:
                assert row["current_findings_known"] is False and row["current_zero_findings"] is False
    assert result["latest_check_enumeration_complete"] is omitted
    assert rows[11]["current_findings_known"] is (peer_later and omitted)
    assert rows[11]["current_zero_findings"] is (peer_later and omitted)


@pytest.mark.parametrize("missing_head", [None, "not-a-sha"], ids=["missing", "malformed"])
def test_headless_listing_does_not_taint_unrelated_head_or_app(missing_head):
    conn = sqlite3.connect(":memory:")
    headless = check(10, head_sha=missing_head, started_at="2026-01-01T12:00:00Z")
    ingest(conn, FixtureGet([headless]), head=OLD)
    ingest(conn, FixtureGet([check(11)]), observed="2026-01-05T00:00:00Z")
    rows = {r["check_id"]: r for r in read(conn)["checks"]}
    assert rows[11]["supersession_disposition"] == "latest-observed" and rows[11]["current_zero_findings"] is True
    assert rows[10]["reviewed_head"] is None and rows[10]["superseded_by"] is None
    _unknown_check(rows[10])
    ingest(
        conn,
        FixtureGet([headless | {"app": {"id": 456, "slug": APP["slug"]}}]),
        observed="2026-01-06T00:00:00Z",
        expected_app_id=456,
    )
    assert read(conn, expected_app_id=456)["checks"][0]["reviewed_head"] is None
    rows = {r["check_id"]: r for r in read(conn)["checks"]}
    assert rows[11]["supersession_disposition"] == "latest-observed" and rows[11]["current_zero_findings"] is True
    assert len(read(conn)["history"]) == 2


@pytest.mark.parametrize("missing_head", [None, "not-a-sha"], ids=["missing", "malformed"])
@pytest.mark.parametrize("omitted", [False, True], ids=["present", "omitted"])
@pytest.mark.parametrize("recovered_head", [HEAD, OLD], ids=["same-head", "different-head"])
@pytest.mark.parametrize("peer_later", [False, True], ids=["older-peer", "known-later-peer"])
def test_headless_listing_recovery_preserves_rivals_after_reopen(
    tmp_path, missing_head, omitted, recovered_head, peer_later
):
    path = tmp_path / "history.sqlite"
    peer = check(11, started_at=f"2026-01-0{2 if peer_later else 1}T00:00:00Z")
    headless = check(12, head_sha=missing_head, started_at="2026-01-01T12:00:00Z")
    recovered = headless | {"head_sha": recovered_head}
    with sqlite3.connect(path) as conn:
        ingest(conn, FixtureGet([peer, headless]))
        before = {r["check_id"]: r for r in read(conn)["checks"]}
        assert before[11]["supersession_disposition"] == ("latest-observed" if peer_later else "unknown")
        assert before[11]["current_findings_known"] is False and before[11]["current_zero_findings"] is False
        others = [check(13, head_sha=OLD)] if recovered_head == OLD else []
        ingest(
            conn,
            FixtureGet([recovered, *others]),
            head=recovered_head,
            observed="2026-01-04T00:00:00Z",
        )
        ingest(conn, FixtureGet([peer] if omitted else [peer, recovered]), observed="2026-01-05T00:00:00Z")
    with sqlite3.connect(path) as conn:
        result = read(conn)
    rows = {r["check_id"]: r for r in result["checks"]}
    disagreement = recovered_head == OLD
    peer_order = "latest-observed" if peer_later else "unknown" if disagreement else "superseded"
    assert rows[11]["supersession_disposition"] == peer_order
    assert rows[11]["superseded_by"] == (12 if not peer_later and not disagreement else None)
    eligible = peer_later and (omitted or not disagreement)
    assert rows[11]["current_findings_known"] is eligible and rows[11]["current_zero_findings"] is eligible
    assert rows[12]["reviewed_head"] == recovered_head
    assert rows[12]["coverage"] == ("stale" if disagreement else "current")
    assert rows[12]["history_disposition"] == "latest-observed"
    for row in (*result["checks"], *(c for s in result["history"] for c in s["checks"])):
        if disagreement:
            assert row["supersession_disposition"] == (peer_order if row["check_id"] == 11 else "unknown")
            assert row["superseded_by"] is None
        if row["check_id"] != 11 or not eligible:
            if disagreement or peer_later or omitted:
                assert row["current_findings_known"] is False and row["current_zero_findings"] is False
        if row["reviewed_head"] is None:
            _unknown_check(row)
    if not disagreement:
        assert rows[12]["supersession_disposition"] == ("superseded" if peer_later else "latest-observed")
        assert rows[12]["superseded_by"] == (11 if peer_later else None)
        assert rows[12]["current_zero_findings"] is (not peer_later and not omitted)
    claims = [c for s in result["history"] for c in s["checks"] if c["check_id"] == 12]
    assert {c["reviewed_head"] for c in claims} == {None, recovered_head}
