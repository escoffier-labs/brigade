import sqlite3

import pytest
from brigade import fleet_hub
from brigade import worklore_campaigns as campaigns
from brigade import worklore_store as store
from brigade.worklore_validate import DESCRIPTION_MAX, TITLE_MAX, WorkloreValidationError

SPEC = {"title": "Rotate keys", "description": "Rotate the deploy keys.", "acceptance": ["Keys rotated", "CI green"]}


def _conn(tmp_path):
    return fleet_hub.init_db(tmp_path / "campaigns.db")


def _body(repos=("acme/one", "acme/two"), **overrides):
    body = {"campaign_key": "camp-1", **SPEC, "members": [{"repo": repo} for repo in repos]}
    body.update(overrides)
    return body


def _create(conn, body):
    return campaigns.create_campaign(conn, body, actor_id="admin")


def _counts(conn):
    return tuple(
        conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("work_items", "work_events", "work_campaigns", "work_campaign_members")
    )


def test_create_then_identical_recreate_adds_nothing(tmp_path):
    conn = _conn(tmp_path)
    first = _create(conn, _body())
    assert first["created"] is True and first["added"] == ["acme/one", "acme/two"] and first["existing"] == []
    assert first["campaign"]["summary"]["total"] == 2 and first["campaign"]["summary"]["unowned"] == 2
    before = _counts(conn)
    again = _create(conn, _body())
    assert again["created"] is False and again["added"] == [] and again["existing"] == ["acme/one", "acme/two"]
    assert _counts(conn) == before


def test_recreate_with_extra_repo_adds_only_that_member(tmp_path):
    conn = _conn(tmp_path)
    _create(conn, _body())
    before = _counts(conn)
    result = _create(conn, _body(repos=("acme/one", "acme/two", "acme/three")))
    assert result["added"] == ["acme/three"] and result["existing"] == ["acme/one", "acme/two"]
    assert _counts(conn) == (before[0] + 1, before[1] + 2, before[2], before[3] + 1)
    campaign_work_id = result["campaign"]["work_id"]
    added = [e for e in store.list_events(conn, campaign_work_id) if e["event_type"] == "campaign-members-added"]
    assert [e["detail"] for e in added][-1] == {"campaign_key": "camp-1", "added": ["acme/three"]}


def test_changed_spec_is_refused_and_writes_nothing(tmp_path):
    conn = _conn(tmp_path)
    _create(conn, _body())
    before = _counts(conn)
    for change in ({"description": "Something else"}, {"acceptance": ["Different"]}):
        with pytest.raises(store.WorkloreConflict) as caught:
            _create(conn, _body(repos=("acme/one", "acme/new"), **change))
        assert caught.value.code == "campaign-spec-changed" and "patch the campaign item" in str(caught.value)
    assert _counts(conn) == before


def test_title_is_a_label_not_part_of_the_spec(tmp_path):
    conn = _conn(tmp_path)
    first = _create(conn, _body())
    again = _create(conn, _body(title="Renamed"))
    assert again["campaign"]["title"] == first["campaign"]["title"] == "Rotate keys"


def test_extension_uses_the_stored_campaign_title(tmp_path):
    conn = _conn(tmp_path)
    _create(conn, _body(repos=("acme/one",)))
    result = _create(conn, _body(repos=("acme/two",), title="Ignored rename"))
    shown = campaigns.show_campaign(conn, "camp-1")
    new_member = next(m for m in shown["members"] if m["repo"] == "acme/two")
    assert result["campaign"]["title"] == "Rotate keys"
    assert store.get_item(conn, new_member["work_id"])["title"] == "Rotate keys [acme/two]"


def test_override_mismatch_is_reported_not_rewritten(tmp_path):
    conn = _conn(tmp_path)
    _create(conn, _body(members=[{"repo": "acme/one", "override": "use vault A"}]))
    result = _create(conn, _body(members=[{"repo": "acme/one", "override": "use vault B"}]))
    assert result["override_conflicts"] == ["acme/one"] and result["added"] == []
    stored = conn.execute("SELECT override FROM work_campaign_members").fetchone()[0]
    assert stored == "use vault A"
    same = _create(conn, _body(members=[{"repo": "acme/one", "override": "use vault A"}]))
    assert same["override_conflicts"] == []


def test_member_items_shape_and_events(tmp_path):
    conn = _conn(tmp_path)
    result = _create(conn, _body(members=[{"repo": "acme/one", "override": "only the app dir"}]))
    shown = campaigns.show_campaign(conn, "camp-1")
    campaign_item = store.get_item(conn, result["campaign"]["work_id"])
    assert campaign_item["kind"] == "fleet" and campaign_item["burn_eligible"] is False
    assert campaign_item["execution_mode"] == "manual"
    member = store.get_item(conn, shown["members"][0]["work_id"])
    assert member["kind"] == "repo" and member["status"] == "captured" and member["burn_eligible"] is False
    assert member["execution_mode"] == "agent" and member["description"] == "only the app dir"
    assert member["acceptance"] == SPEC["acceptance"] and member["title"] == "Rotate keys [acme/one]"
    for work_id in (campaign_item["work_id"], member["work_id"]):
        assert store.list_events(conn, work_id)[0]["event_type"] == "created"


def test_long_member_title_is_cut(tmp_path):
    conn = _conn(tmp_path)
    result = _create(conn, _body(title="T" * (TITLE_MAX - 4)))
    member = store.get_item(conn, campaigns.show_campaign(conn, "camp-1")["members"][0]["work_id"])
    assert len(member["title"]) == TITLE_MAX and result["created"]


def test_drift_appears_after_patching_campaign_spec(tmp_path):
    conn = _conn(tmp_path)
    result = _create(conn, _body(members=[{"repo": "acme/one"}, {"repo": "acme/two", "override": "x"}]))
    assert result["campaign"]["summary"]["drifted"] == 0
    work_id = result["campaign"]["work_id"]
    store.patch_item(conn, work_id, {"description": "Rotate them all twice"}, expected_version=1, actor_id="admin")
    shown = campaigns.show_campaign(conn, "camp-1")
    assert shown["campaign"]["summary"]["drifted"] == 2
    assert all(member["drifted"] for member in shown["members"])
    stored = {r[0]: r[1] for r in conn.execute("SELECT repo, spec_fingerprint FROM work_campaign_members")}
    assert stored["acme/one"] == campaigns.member_fingerprint(
        campaigns.spec_fingerprint(SPEC["description"], SPEC["acceptance"]), ""
    )
    # A re-create with the old spec is now refused instead of rewriting members.
    with pytest.raises(store.WorkloreConflict):
        _create(conn, _body())


@pytest.mark.parametrize(
    ("status", "attempts", "held", "bucket"),
    [
        ("captured", 0, None, "unowned"),
        ("ready", 1, {"state": "offered"}, "unowned"),
        ("ready", 0, {"state": "owned", "owner_node": "n1"}, "in_flight"),
        ("ready", 0, {"state": "handoff-pending"}, "in_flight"),
        ("claimed", 0, None, "in_flight"),
        ("running", 1, None, "in_flight"),
        ("verifying", 0, None, "awaiting_gate"),
        ("verifying", 2, {"state": "owned"}, "awaiting_gate"),
        ("blocked", 0, None, "blocked"),
        ("running", 2, None, "in_flight"),
        ("claimed", 3, None, "in_flight"),
        ("ready", 3, {"state": "owned"}, "in_flight"),
        ("ready", 2, None, "attempt_limited"),
        ("ready", 3, {"state": "offered"}, "attempt_limited"),
        ("deferred", 0, None, "unowned"),
        ("defining", 0, None, "unowned"),
        ("completed", 2, None, "completed"),
        ("canceled", 0, {"state": "owned"}, "canceled"),
        ("archived", 0, None, "archived"),
    ],
)
def test_member_bucket_mapping(status, attempts, held, bucket):
    assert campaigns.member_bucket({"status": status, "attempt_count": attempts}, held) == bucket


def test_attempt_limit_is_shared_with_the_burn_queue():
    assert store.ATTEMPT_LIMIT == 2
    item = {
        "status": "ready",
        "burn_eligible": True,
        "effective_acceptance": ["x"],
        "execution_mode": "agent",
        "attempt_count": store.ATTEMPT_LIMIT,
        "blocker": None,
        "review_after": None,
    }
    assert store.exclusion_bucket(item, []) == "attempt-limit"


def test_rollup_counts_and_done(tmp_path):
    conn = _conn(tmp_path)
    _create(conn, _body(repos=("acme/a", "acme/b", "acme/c")))
    rows = {r["repo"]: r["work_id"] for r in campaigns.show_campaign(conn, "camp-1")["members"]}
    conn.execute("UPDATE work_items SET status='completed' WHERE work_id=?", (rows["acme/a"],))
    conn.execute("UPDATE work_items SET status='verifying' WHERE work_id=?", (rows["acme/b"],))
    conn.commit()
    summary = campaigns.show_campaign(conn, "camp-1")["campaign"]["summary"]
    assert (summary["total"], summary["completed"], summary["awaiting_gate"], summary["unowned"]) == (3, 1, 1, 1)
    assert summary["done"] is False
    conn.execute("UPDATE work_items SET status='completed'")
    conn.commit()
    assert campaigns.show_campaign(conn, "camp-1")["campaign"]["summary"]["done"] is True


def test_member_rows_carry_latest_links(tmp_path):
    conn = _conn(tmp_path)
    _create(conn, _body(repos=("acme/one",)))
    work_id = campaigns.show_campaign(conn, "camp-1")["members"][0]["work_id"]
    for link_id, link_type, ref, synced in (
        ("l1", "github", "acme/one#1", "2026-01-01"),
        ("l2", "github", "acme/one#2", "2026-02-01"),
        ("l3", "brigade", "run-9", "2026-01-01"),
    ):
        conn.execute(
            "INSERT INTO work_links (link_id, work_id, link_type, external_key, display_ref, url, external_state, "
            "source_policy, source_acceptance_json, synced_at) VALUES (?, ?, ?, ?, ?, NULL, 'open', 'eligible', '[]', ?)",
            (link_id, work_id, link_type, link_id, ref, synced),
        )
    conn.commit()
    member = campaigns.show_campaign(conn, "camp-1")["members"][0]
    assert member["github"]["display_ref"] == "acme/one#2" and member["brigade"]["display_ref"] == "run-9"
    assert member["owner_node"] is None


def test_member_projects_execution_link_separately_from_brigade_task(tmp_path):
    conn = _conn(tmp_path)
    _create(conn, _body(repos=("acme/one",)))
    work_id = campaigns.show_campaign(conn, "camp-1")["members"][0]["work_id"]
    for run_id in ("run-1", "run-2"):
        conn.execute(
            "INSERT INTO events (node_id, run_id, sequence, digest, state, ts, received_at) "
            "VALUES ('node-a', ?, 1, 'd', 'run.created', 't', 't')",
            (run_id,),
        )
        conn.commit()
        store.link_execution(
            conn, work_id, node_id="node-a", run_id=run_id, actor_id="admin", actor_type="operator", is_admin=True
        )
    member = campaigns.show_campaign(conn, "camp-1")["members"][0]
    assert member["fleet_run"] == {"display_ref": "node-a/run-2", "url": None, "external_state": None}
    assert member["brigade"] is None


def test_list_orders_newest_first_and_bounds_limit(tmp_path):
    conn = _conn(tmp_path)
    for index in range(3):
        _create(conn, _body(campaign_key=f"camp-{index}"))
    keys = [c["campaign_key"] for c in campaigns.list_campaigns(conn)["campaigns"]]
    assert keys == ["camp-2", "camp-1", "camp-0"]
    assert len(campaigns.list_campaigns(conn, limit="2")["campaigns"]) == 2
    for bad in (0, 51, "abc", True, -1):
        with pytest.raises(WorkloreValidationError):
            campaigns.list_campaigns(conn, limit=bad)


def test_unknown_campaign_is_not_found(tmp_path):
    with pytest.raises(store.WorkloreNotFound):
        campaigns.show_campaign(_conn(tmp_path), "missing")


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (lambda b: b.update(members=[{"repo": f"acme/r{i}"} for i in range(257)]), "field-bound"),
        (lambda b: b.update(members=[]), "field-bound"),
        (lambda b: b.update(campaign_key="bad key"), "field-bound"),
        (lambda b: b.update(campaign_key="latest"), "field-bound"),
        (lambda b: b.update(members=[{"repo": "acme/one"}, {"repo": "acme/one"}]), "field-bound"),
        (lambda b: b.update(members=[{"repo": "acme/one@wt"}]), "field-bound"),
        (lambda b: b.update(members=[{"repo": "acme/../one"}]), "field-bound"),
        (lambda b: b.update(members=[{"repo": "/acme"}]), "field-bound"),
        (lambda b: b.update(members=[{"repo": "acme//one"}]), "field-bound"),
        (lambda b: b.update(members=[{"repo": "acme/one/"}]), "field-bound"),
        (lambda b: b.update(members=[{"repo": "acme/./one"}]), "field-bound"),
        (lambda b: b.update(members=[{"repo": "one"}]), "field-bound"),
        (lambda b: b.update(members=[{"repo": "a/" + "b" * 254}]), "field-bound"),
        (lambda b: b.update(members=[{"repo": "acme/one"}, {"repo": "ACME/One"}]), "field-bound"),
        (lambda b: b.update(members=[{"repo": 5}]), "field-bound"),
        (lambda b: b.update(members=[{"repo": "acme/one", "override": "x" * (DESCRIPTION_MAX + 1)}]), "field-bound"),
        (lambda b: b.update(members=[{"repo": "acme/one", "override": "token=" + "a1" * 12}]), "private-data"),
        (lambda b: b.update(members=[{"repo": "acme/one", "extra": 1}]), "unknown-field"),
        (lambda b: b.update(surprise=1), "unknown-field"),
        (lambda b: b.update(acceptance=[]), "acceptance-required"),
        (lambda b: b.update(acceptance=["a"] * 21), "field-bound"),
        (lambda b: b.update(description="key ghp_" + "a" * 20), "private-data"),
        (lambda b: b.pop("members"), "field-bound"),
    ],
)
def test_request_bounds_refuse_and_write_nothing(tmp_path, mutation, code):
    conn = _conn(tmp_path)
    body = _body()
    mutation(body)
    with pytest.raises(WorkloreValidationError) as caught:
        _create(conn, body)
    assert caught.value.code == code
    assert _counts(conn) == (0, 0, 0, 0)


def test_max_members_accepted(tmp_path):
    conn = _conn(tmp_path)
    result = _create(conn, _body(repos=tuple(f"acme/r{i}" for i in range(256))))
    assert len(result["added"]) == 256


def test_aggregate_member_limit_is_atomic_and_allows_retry_at_capacity(tmp_path):
    conn = _conn(tmp_path)
    repos = tuple(f"acme/r{i}" for i in range(256))
    _create(conn, _body(repos=repos[:128]))
    _create(conn, _body(repos=repos[128:]))
    before = tuple(
        conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
        for table in ("work_items", "work_events", "work_campaigns", "work_campaign_members")
    )
    retry = _create(conn, _body(repos=tuple(f"github.com/{repo.upper()}" for repo in repos)))
    assert retry["added"] == [] and retry["existing"] == list(repos)
    with pytest.raises(WorkloreValidationError) as caught:
        _create(conn, _body(repos=("acme/r0", "acme/overflow")))
    assert caught.value.code == "field-bound"
    after = tuple(
        conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
        for table in ("work_items", "work_events", "work_campaigns", "work_campaign_members")
    )
    assert after == before


def test_failure_mid_create_rolls_back_everything(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    original = store._insert_item
    calls = []

    def flaky(connection, item):
        calls.append(item["work_id"])
        if len(calls) == 3:
            raise sqlite3.OperationalError("disk full")
        original(connection, item)

    monkeypatch.setattr(store, "_insert_item", flaky)
    with pytest.raises(sqlite3.OperationalError):
        _create(conn, _body())
    assert _counts(conn) == (0, 0, 0, 0)
    monkeypatch.setattr(store, "_insert_item", original)
    assert _create(conn, _body())["created"] is True


def test_v39_database_upgrades_to_v40_with_campaign_tables(tmp_path):
    db = tmp_path / "hub.db"
    conn = fleet_hub.init_db(db)
    conn.execute("DROP TABLE work_campaign_members")
    conn.execute("DROP TABLE work_campaigns")
    conn.execute("PRAGMA user_version=39")
    conn.commit()
    conn.close()
    assert fleet_hub.SCHEMA_VERSION == 44
    migrated = fleet_hub.init_db(db)
    tables = {row[0] for row in migrated.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"work_campaigns", "work_campaign_members"} <= tables
    assert migrated.execute("PRAGMA user_version").fetchone()[0] == fleet_hub.SCHEMA_VERSION
    migrated.close()


def test_repo_identity_is_lowercased_before_storing_and_deduping(tmp_path):
    conn = _conn(tmp_path)
    result = _create(conn, _body(repos=("Acme/One",)))
    assert result["added"] == ["acme/one"]
    again = _create(conn, _body(repos=("acme/ONE", "Acme/Two")))
    assert again["existing"] == ["acme/one"] and again["added"] == ["acme/two"]
    assert conn.execute("SELECT COUNT(*) FROM work_campaign_members").fetchone()[0] == 2


def test_github_host_prefix_is_the_same_member(tmp_path):
    conn = _conn(tmp_path)
    assert _create(conn, _body(repos=("GitHub.com/Acme/One",)))["added"] == ["acme/one"]
    again = _create(conn, _body(repos=("acme/one",)))
    assert again["existing"] == ["acme/one"] and again["added"] == []


def test_repo_longest_identity_is_accepted(tmp_path):
    conn = _conn(tmp_path)
    repo = "a/" + "b" * 253
    assert len(repo) == 255
    assert _create(conn, _body(repos=(repo,)))["added"] == [repo]


def test_credential_shaped_repo_is_refused_even_with_a_long_title(tmp_path):
    conn = _conn(tmp_path)
    for title in (SPEC["title"], "T" * (TITLE_MAX - 1)):
        with pytest.raises(WorkloreValidationError) as caught:
            _create(conn, _body(title=title, repos=("acme/ghp_" + "a1" * 18,)))
        assert caught.value.code == "private-data"
    assert _counts(conn) == (0, 0, 0, 0)


def test_latest_link_is_deterministic_when_synced_at_ties(tmp_path):
    conn = _conn(tmp_path)
    _create(conn, _body(repos=("acme/one",)))
    work_id = campaigns.show_campaign(conn, "camp-1")["members"][0]["work_id"]
    for link_id, ref, updated in (
        ("l9", "acme/one#9", "2026-01-01T00:00:00Z"),
        ("l1", "acme/one#1", "2026-03-01T00:00:00Z"),
        ("l5", "acme/one#5", None),
    ):
        conn.execute(
            "INSERT INTO work_links (link_id, work_id, link_type, external_key, display_ref, url, external_state, "
            "source_policy, source_acceptance_json, synced_at, external_updated_at) "
            "VALUES (?, ?, 'github', ?, ?, NULL, 'open', 'eligible', '[]', '2026-04-01', ?)",
            (link_id, work_id, link_id, ref, updated),
        )
    conn.commit()
    member = campaigns.show_campaign(conn, "camp-1")["members"][0]
    assert member["github"]["display_ref"] == "acme/one#1"


def test_two_connections_creating_the_same_key_converge_on_one_campaign(tmp_path):
    db = tmp_path / "shared.db"
    first = fleet_hub.init_db(db)
    second = fleet_hub.init_db(db)
    one = _create(first, _body(repos=("acme/one", "acme/extra-a")))
    two = _create(second, _body(repos=("acme/one", "acme/extra-b")))
    assert one["created"] is True and two["created"] is False
    assert two["added"] == ["acme/extra-b"] and two["existing"] == ["acme/one"]
    assert first.execute("SELECT COUNT(*) FROM work_campaigns").fetchone()[0] == 1
    assert first.execute("SELECT COUNT(*) FROM work_campaign_members").fetchone()[0] == 3
