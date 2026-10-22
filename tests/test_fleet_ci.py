"""CI ingestion, exact-head display and Hub security contracts using offline fixtures."""

import io
import json
from email.message import Message

import pytest

from brigade import fleet_hub, fleet_hub_http

NOW = "2026-10-21T12:00:00Z"
HEAD = "a" * 40
OLD = "b" * 40


def event(**changes):
    return dict(
        repo="example/public",
        event="workflow_run",
        action="completed",
        head_sha=HEAD,
        status="completed",
        conclusion="success",
        run_id=11,
        run_attempt=1,
        updated_at=NOW,
        **changes,
    )


def observation(repo="example/public", visibility="public", **changes):
    value = dict(
        repo=repo,
        visibility=visibility,
        default_branch="main",
        head_sha=HEAD,
        observed_at=NOW,
        required_checks=[{"context": "ci", "app_id": 7}],
        required_observed_at=NOW,
        required_complete=True,
        checks_complete=[HEAD],
        pulls_complete=True,
        pulls=[],
        complete=True,
        reason=None,
    )
    value.update(changes)
    return value


def select_policy(conn, *repos):
    from brigade import fleet_hub_policy

    document = fleet_hub_policy.current_policy(conn)["document"]
    document["repositories"] = {"github.com/" + repo: {} for repo in repos}
    conn.execute("UPDATE fleet_policy_revisions SET document=?", (json.dumps(document),))
    conn.commit()


def put(conn, *, events=(), repositories=()):
    from brigade import fleet_ci_board as ci

    return ci.store(conn, {"events": list(events), "repositories": list(repositories)}, now=NOW)


def check(**changes):
    value = dict(
        repo="example/public",
        event="check_run",
        head_sha=HEAD,
        status="completed",
        conclusion="success",
        check_run_id=22,
        run_attempt=1,
        name="ci",
        app_id=7,
        updated_at=NOW,
    )
    value.update(changes)
    return value


@pytest.fixture
def db(tmp_path):
    conn = fleet_hub.init_db(tmp_path / "hub.db")
    yield conn
    conn.close()


@pytest.mark.parametrize(
    "status,conclusion",
    [
        ("queued", None),
        ("in_progress", None),
        ("completed", "success"),
        ("completed", "cancelled"),
        ("completed", "skipped"),
        ("completed", "timed_out"),
    ],
)
def test_provider_attempts_preserve_lifecycle_without_false_green(db, status, conclusion):
    from brigade import fleet_ci_board as ci

    value = event()
    value.update(status=status, conclusion=conclusion)
    put(db, events=[value, value], repositories=[observation()])
    row = ci.load_view(db, operator=True, now=NOW)["repositories"][0]
    assert len(row["runs"]) == 1
    assert row["latest_run"]["status"] == status
    assert row["latest_run"]["conclusion"] == (conclusion or "unknown")
    assert row["required"]["state"] == "unknown"
    value.update(run_attempt=2, status="queued", conclusion=None)
    put(db, events=[value])
    row = ci.load_view(db, operator=True, now=NOW)["repositories"][0]
    assert len(row["runs"]) == 2
    assert row["latest_run"]["attempt"] == 2


def test_exact_head_old_completion_and_required_app_binding(db):
    from brigade import fleet_ci_board as ci

    put(db, events=[check(), check(check_run_id=23, app_id=8, conclusion="failure")], repositories=[observation()])
    assert ci.load_view(db, now=NOW)["repositories"][0]["required"]["state"] == "passing"
    newer = "2026-10-21T12:01:00Z"
    put(db, repositories=[observation(head_sha=OLD, observed_at=newer, checks_complete=[OLD])])
    late = event()
    late.update(updated_at="2026-10-21T12:02:00Z", head_branch="main")
    put(db, events=[late, check()])
    row = ci.load_view(db, now=newer)["repositories"][0]
    assert row["head"]["sha"] == OLD
    assert row["latest_run"]["head_relation"] == "stale-head"
    assert row["required"]["state"] == "unknown"
    assert row["last_event_received"] == NOW
    assert row["last_reconciled"] == newer


@pytest.mark.parametrize("conclusion", ["neutral", "skipped", "cancelled", "timed_out", "failure"])
def test_required_checks_only_success_passes(db, conclusion):
    from brigade import fleet_ci_board as ci

    put(db, events=[check(conclusion=conclusion)], repositories=[observation()])
    assert ci.load_view(db, now=NOW)["repositories"][0]["required"]["state"] == "not-passing"


def test_out_of_order_delivery_cannot_regress_completed_object(db):
    from brigade import fleet_ci_board as ci

    put(db, events=[check()], repositories=[observation()])
    put(db, events=[check(status="queued", conclusion=None, updated_at="2026-10-21T11:59:00Z")])
    assert ci.load_view(db, now=NOW)["repositories"][0]["required"]["state"] == "passing"


def test_unknown_privacy_is_private_and_stale_facts_are_labelled(db):
    from brigade import fleet_ci_board as ci

    put(
        db,
        repositories=[
            observation(),
            observation("example/private", "private"),
            observation("example/unknown", "unknown"),
        ],
    )
    assert [r["repo"] for r in ci.load_view(db, now=NOW)["repositories"]] == ["example/public"]
    rows = ci.load_view(db, operator=True, now="2026-10-21T14:00:00Z")["repositories"]
    assert len(rows) == 3
    assert all(r["head"]["freshness"] == "stale" for r in rows)
    assert all(r["head"]["source"] == "github-reconcile" for r in rows)


def test_migration_from_37_is_repeatable(tmp_path):
    path = tmp_path / "hub.db"
    conn = fleet_hub.init_db(path)
    for table in ("ci_runs", "ci_check_runs", "ci_repo_state"):
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.execute("PRAGMA user_version=37")
    conn.commit()
    conn.close()
    for _ in range(2):
        conn = fleet_hub.init_db(path)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 38
        assert (
            conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE name IN ('ci_runs','ci_check_runs','ci_repo_state')"
            ).fetchone()[0]
            == 3
        )
        conn.close()


def handler(tmp_path, path, bearer=None, body=None, *, trust_tailscale_identity=False):
    db = tmp_path / "hub.db"
    fleet_hub.init_db(db).close()
    cls = fleet_hub_http.make_handler("example-admin", db, trust_tailscale_identity=trust_tailscale_identity)
    h = cls.__new__(cls)
    h.path = path
    h.headers = Message()
    if bearer:
        h.headers["Authorization"] = "Bearer " + bearer
    raw = json.dumps(body).encode() if body is not None else b""
    h.headers["Content-Length"] = str(len(raw))
    h.rfile = io.BytesIO(raw)
    result = []
    h._send_json = lambda status, payload, **kw: result.append((status, payload))
    h._send_html = lambda status, payload, **kw: result.append((status, payload))
    return h, result, db


def test_hub_network_denied_ingest_read_and_admin_refresh(tmp_path, monkeypatch):
    def deny(*a, **kw):
        pytest.fail("Hub attempted outbound network")

    monkeypatch.setattr("socket.create_connection", deny)
    monkeypatch.setattr("socket.socket.connect", deny)
    monkeypatch.setattr("urllib.request.OpenerDirector.open", deny)
    monkeypatch.setattr("subprocess.Popen", deny)
    h, out, path = handler(tmp_path, "/ci")
    conn = fleet_hub.open_db(path)
    _, token = fleet_hub.add_node(conn, "example-node")
    conn.close()
    h, out, _ = handler(tmp_path, "/ci", token, {"events": [event()], "repositories": [observation()]})
    h.do_POST()
    assert out[0][0] == 200
    h, out, _ = handler(tmp_path, "/ci/refresh", "example-admin", {"repo": "example/public"})
    h.do_POST()
    assert out[0] == (202, {"queued": True})
    h, out, _ = handler(tmp_path, "/deck/ci", "example-admin")
    h.do_GET()
    assert out[0][0] == 200
    assert "CI board" in out[0][1]
    assert "/deck/ci" in out[0][1]
    assert "<form" not in out[0][1]
    for route in ("/ci", "/ci/work"):
        h, out, _ = handler(tmp_path, route, "example-admin")
        h.do_GET()
        assert out[0][0] == 200
    conn = fleet_hub.open_db(path)
    assert conn.execute("SELECT refresh_requested FROM ci_repo_state").fetchone()[0]
    conn.close()


@pytest.mark.parametrize(
    "path,method,bearer,expected",
    [
        ("/ci", "POST", None, 401),
        ("/ci", "POST", "example-admin", 403),
        ("/ci/refresh", "POST", "node", 403),
        ("/deck/ci", "GET", None, 401),
    ],
)
def test_ci_authorization(tmp_path, path, method, bearer, expected):
    h, out, db = handler(tmp_path, path, body={"events": [], "repositories": []})
    if bearer == "node":
        conn = fleet_hub.open_db(db)
        _, bearer = fleet_hub.add_node(conn, "example-node")
        conn.close()
    if bearer:
        h.headers["Authorization"] = "Bearer " + bearer
    getattr(h, "do_" + method)()
    assert out[0][0] == expected


def test_node_reads_never_reveal_private_or_unknown_names(tmp_path):
    from brigade import fleet_ci_board as ci

    h, out, path = handler(tmp_path, "/ci")
    conn = fleet_hub.open_db(path)
    _, token = fleet_hub.add_node(conn, "example-node")
    put(
        conn,
        repositories=[
            observation(),
            observation("example/private", "private"),
            observation("example/unknown", "unknown"),
        ],
    )
    ci.request_refresh(conn, "example/private", now=NOW)
    select_policy(conn, "example/public", "example/private", "example/unknown")
    conn.close()
    for route in ["/ci", "/ci/work"]:
        h, out, _ = handler(tmp_path, route, token)
        h.do_GET()
        assert out[0][0] == 200
        text = json.dumps(out[0][1])
        assert "example/public" in text
        assert "example/private" not in text
        assert "example/unknown" not in text
    h, out, _ = handler(tmp_path, "/ci/work", "example-admin")
    h.do_GET()
    assert "example/private" in json.dumps(out[0][1])


def test_refresh_ack_cannot_erase_newer_operator_request(db):
    from brigade import fleet_ci_board as ci

    ci.request_refresh(db, "example/public", now="2026-10-21T12:02:00Z")
    put(db, repositories=[observation(refresh_id=NOW)])
    assert db.execute("SELECT refresh_requested FROM ci_repo_state").fetchone()[0] == "2026-10-21T12:02:00Z"


def test_failed_scope_does_not_turn_old_check_rows_into_current_success(db):
    from brigade import fleet_ci_board as ci

    put(db, events=[check()], repositories=[observation()])
    put(db, repositories=[observation(complete=False, checks_complete=[], reason="access-denied")])
    assert ci.load_view(db, now=NOW)["repositories"][0]["required"]["state"] == "unknown"


def test_authoritative_repair_does_not_reuse_removed_successful_checks(db):
    from brigade import fleet_ci_board as ci

    put(db, events=[check()], repositories=[observation()])
    put(db, repositories=[observation(observed_at="2026-10-21T12:05:00Z", check_ids={HEAD: []})])
    assert ci.load_view(db, now="2026-10-21T12:05:00Z")["repositories"][0]["required"]["state"] == "unknown"


def test_reordered_pr_events_cannot_restore_closed_or_older_head(db):
    from brigade import fleet_ci_board as ci

    put(db, repositories=[observation()])
    pr = dict(
        repo="example/public",
        event="pull_request",
        pr_number=5,
        head_sha=OLD,
        status="unknown",
        updated_at="2026-10-21T12:05:00Z",
        action="closed",
    )
    put(db, events=[pr])
    pr.update(action="synchronize", head_sha=HEAD, updated_at="2026-10-21T12:03:00Z")
    put(db, events=[pr])
    assert ci.load_view(db, now=NOW)["repositories"][0]["pulls"] == []


def test_work_plan_includes_policy_only_repositories_for_operator(db):
    from brigade import fleet_ci_board as ci, fleet_hub_policy

    policy = fleet_hub_policy.current_policy(db)["document"]
    policy["repositories"]["github.com/example/policy-only"] = {}
    # Exercise the real persisted authority, independent of CI observations.
    db.execute("UPDATE fleet_policy_revisions SET document=?", (json.dumps(policy),))
    db.commit()
    assert {"repo": "example/policy-only", "visibility": "unknown"} in ci.work_plan(db, operator=True)["repositories"]
    assert ci.work_plan(db, operator=False)["repositories"] == []


def test_fractional_provider_times_order_chronologically(db):
    from brigade import fleet_ci_board as ci

    put(db, events=[check(status="queued", conclusion=None)], repositories=[observation()])
    put(db, events=[check(updated_at="2026-10-21T12:00:00.100Z")])
    assert ci.load_view(db, now=NOW)["repositories"][0]["required"]["state"] == "passing"
    put(db, repositories=[observation(head_sha=OLD, observed_at="2026-10-21T12:00:00.100Z")])
    assert ci.load_view(db, now=NOW)["repositories"][0]["head"]["sha"] == OLD


def test_repair_refreshes_observation_of_unchanged_completed_run(db):
    from brigade import fleet_ci_board as ci

    put(db, events=[event()], repositories=[observation()])
    later = "2026-10-21T14:00:00Z"
    ci.store(db, {"source": "github-reconcile", "events": [event()]}, now=later)
    latest = ci.load_view(db, now=later)["repositories"][0]["latest_run"]
    assert latest["freshness"] == "fresh"
    assert latest["source"] == "github-reconcile"
    assert latest["observed_at"] == later


def test_pr_rollup_includes_failing_nonrequired_checks(db):
    from brigade import fleet_ci_board as ci

    put(
        db,
        events=[check(), check(check_run_id=23, name="optional-integration", conclusion="failure")],
        repositories=[observation(pulls=[{"number": 5, "head_sha": HEAD}])],
    )
    row = ci.load_view(db, now=NOW)["repositories"][0]
    assert row["required"]["state"] == "passing"
    assert row["pulls"][0]["rollup"]["state"] == "not-passing"


@pytest.mark.parametrize("status,conclusion", [("unknown", None), ("completed", "unknown")])
def test_unrecognized_check_evidence_is_unknown_not_a_known_outcome(db, status, conclusion):
    from brigade import fleet_ci_board as ci

    put(
        db,
        events=[check(status=status, conclusion=conclusion)],
        repositories=[observation(pulls=[{"number": 5, "head_sha": HEAD}])],
    )
    row = ci.load_view(db, now=NOW)["repositories"][0]
    assert row["required"]["state"] == "unknown"
    assert row["pulls"][0]["rollup"]["state"] == "unknown"


def test_late_old_completion_does_not_replace_api_latest_or_hide_stale_attempt(db):
    from brigade import fleet_ci_board as ci, fleet_ci_page as page

    current = event()
    current.update(run_id=12, status="queued", conclusion=None, updated_at="2026-10-21T12:01:00Z")
    put(db, events=[current], repositories=[observation(latest_run_id=12, latest_run_attempt=1)])
    old = event()
    old.update(head_sha=OLD, updated_at="2026-10-21T12:02:00Z", head_branch="main")
    put(db, events=[old])
    view = ci.load_view(db, now=NOW)
    assert view["repositories"][0]["latest_run"]["object_id"] == 12
    rendered = page.render(view, nonce="example-nonce")
    assert "stale-head" in rendered
    assert "Run 11" in rendered
    assert "Run 12" in rendered


@pytest.mark.parametrize("capability", ["cookie", "tailscale", "operator"])
def test_deck_ci_visibility_follows_actual_roster_capability(tmp_path, capability):
    from brigade import fleet_hub_enrollment as enrollment
    from ipaddress import IPv4Address

    h, out, path = handler(tmp_path, "/deck/ci", trust_tailscale_identity=capability == "tailscale")
    conn = fleet_hub.open_db(path)
    put(
        conn,
        repositories=[
            observation(),
            observation("example/private", "private"),
            observation("example/unknown", "unknown"),
        ],
    )
    if capability == "cookie":
        credential = enrollment.redeem(conn, enrollment.mint(conn)["code"])
        h.headers["Cookie"] = f"{fleet_hub.DASHBOARD_COOKIE}={credential}"
    elif capability == "tailscale":
        h.client_address = (str(IPv4Address(0x7F000001)), 0)
        h.headers[fleet_hub._TAILSCALE_IDENTITY_HEADER] = "reader@example.invalid"
    else:
        h.headers["Authorization"] = "Bearer example-admin"
    conn.close()
    h.do_GET()
    assert out[0][0] == 200
    html = out[0][1]
    assert "example/public" in html
    for hidden in ("example/private", "example/unknown"):
        assert (hidden in html) is (capability == "operator")
    assert "reader@example.invalid" not in html
