"""Owner-boundary regressions for independent review findings."""

import json
import sqlite3
from collections import OrderedDict

import pytest

from brigade import fleet_ci_board as ci, fleet_ci_node as node, fleet_ci_reconcile as repair, fleet_hub
from brigade.github_reader import CommandResult, GitHubReader
from brigade.github_read_ledger import BudgetLedger
from tests.test_fleet_ci import HEAD, OLD, NOW, check, event, observation
from tests.test_fleet_ci_node import Provider, reader
from tests.test_github_reader import FakeRunner, gh, PATH


def reply(argv, status, value):
    if argv[0] == "octopool":
        return CommandResult(
            0, json.dumps({"status": status, "headers": {}, "body_encoding": "json", "body": value}).encode()
        )
    return CommandResult(0, f"HTTP/2 {status}\n\n".encode() + json.dumps(value).encode())


class BranchProvider(Provider):
    protected = True
    required_status = None
    metadata_status = None
    private = False

    def __call__(self, argv, **kw):
        if argv[:2] == ["octopool", "whoami"]:
            return super().__call__(argv, **kw)
        path = argv[argv.index("--path") + 1] if argv[0] == "octopool" else argv[7].split("?")[0]
        if path.endswith("/protection/required_status_checks") and self.required_status:
            self.calls.append(list(argv))
            return reply(argv, self.required_status, {})
        if path == "/repos/example/public/branches/main":
            self.calls.append(list(argv))
            return reply(argv, 200, {"commit": {"sha": HEAD}, "protected": self.protected})
        if path == "/repos/example/public":
            self.calls.append(list(argv))
            return reply(argv, self.metadata_status or 200, {"private": self.private, "default_branch": "main"})
        return super().__call__(argv, **kw)


def test_required_failure_retries_after_hour_not_next_day(tmp_path):
    provider = BranchProvider()
    provider.required_status = 403
    source = reader(tmp_path, provider, visibility="unknown")
    first = repair.reconcile_repo(source, now=NOW)["repositories"][0]
    assert first["required_observed_at"] is None
    provider.required_status = None
    second = repair.reconcile_repo(source, now="2026-10-21T13:01:00Z", previous=first)["repositories"][0]
    assert second["required_complete"]
    assert second["required_observed_at"] == "2026-10-21T13:01:00Z"


@pytest.mark.parametrize("protected,known", [(False, True), (True, False), (None, False)])
def test_unprotected_requires_positive_branch_and_successful_rules_evidence(tmp_path, protected, known):
    provider = BranchProvider()
    provider.protected = protected
    provider.required_status = 404
    result = repair.reconcile_repo(reader(tmp_path, provider, visibility="unknown"), now=NOW)["repositories"][0]
    assert result["required_complete"] is known
    if known:
        assert result["required_checks"] == []
        assert not any("/protection/" in str(call) for call in provider.calls)


def test_failed_metadata_hides_visibility_and_private_reclassification_stops_pool(tmp_path):
    provider = BranchProvider()
    provider.metadata_status = 404
    result = repair.reconcile_repo(reader(tmp_path, provider), now=NOW)["repositories"][0]
    assert result["visibility"] == "unknown"
    provider = BranchProvider()
    provider.private = True
    repair.reconcile_repo(reader(tmp_path, provider), now=NOW)
    metadata_index = next(
        i
        for i, call in enumerate(provider.calls)
        if "--path" in call and call[call.index("--path") + 1] == "/repos/example/public"
    )
    assert all(call[0] == "gh" for call in provider.calls[metadata_index + 1 :])


def test_refresh_consumed_after_incomplete_persist_and_newer_request_preserved(tmp_path):
    conn = fleet_hub.init_db(tmp_path / "hub.db")
    ci.request_refresh(conn, "example/public", now=NOW)
    ci.store(conn, {"repositories": [observation(complete=False, reason="access-denied", refresh_id=NOW)]}, now=NOW)
    assert ci.load_view(conn, now=NOW)["repositories"][0]["refresh_requested"] is None
    ci.request_refresh(conn, "example/public", now="2026-10-21T13:00:00Z")
    ci.store(conn, {"repositories": [observation(complete=False, refresh_id=NOW)]}, now=NOW)
    assert ci.load_view(conn, now=NOW)["repositories"][0]["refresh_requested"] == "2026-10-21T13:00:00Z"
    conn.close()


def test_unknown_branch_run_cannot_replace_default_workflow_or_claim_stale(tmp_path):
    conn = fleet_hub.init_db(tmp_path / "hub.db")
    ci.store(conn, {"events": [event()], "repositories": [observation(latest_run_id=11)]}, now=NOW)
    run = event()
    run.update(run_id=12, head_sha=OLD, created_at="2026-10-21T12:01:00Z", updated_at="2026-10-21T12:01:00Z")
    ci.store(conn, {"events": [run]}, now="2026-10-21T12:01:00Z")
    row = ci.load_view(conn, now=NOW)["repositories"][0]
    assert row["latest_run"]["object_id"] == 11
    assert next(r for r in row["runs"] if r["object_id"] == 12)["head_relation"] == "unknown-branch"
    conn.close()


def select_policy(conn, *repos):
    from brigade import fleet_hub_policy

    document = fleet_hub_policy.current_policy(conn)["document"]
    document["repositories"] = {"github.com/" + repo: {} for repo in repos}
    conn.execute("UPDATE fleet_policy_revisions SET document=?", (json.dumps(document),))
    conn.commit()


def test_plan_excludes_unselected_history_and_private_refresh_hashes(tmp_path):
    conn = fleet_hub.init_db(tmp_path / "hub.db")
    ci.store(
        conn,
        {"repositories": [observation(), observation("example/private", "private"), observation("example/old")]},
        now=NOW,
    )
    select_policy(conn, "example/public", "example/private")
    for repo in ("example/public", "example/private", "example/old"):
        ci.request_refresh(conn, repo, now=NOW)
    public = ci.work_plan(conn, operator=False)
    assert public["repositories"] == [{"repo": "example/public", "visibility": "public"}]
    assert public["refresh_requests"] == {ci.refresh_key("example/public"): NOW}
    operator = ci.work_plan(conn, operator=True)
    assert {r["repo"] for r in operator["repositories"]} == {"example/public", "example/private"}
    assert ci.refresh_key("example/private") in operator["refresh_requests"]
    assert ci.refresh_key("example/old") in operator["refresh_requests"]
    conn.close()


def test_hub_receipt_order_excludes_removed_checks_despite_node_clock_skew(tmp_path):
    conn = fleet_hub.init_db(tmp_path / "hub.db")
    ci.store(conn, {"events": [check()], "repositories": [observation()]}, now="2026-10-21T14:00:00Z")
    ci.store(
        conn,
        {"repositories": [observation(observed_at="2026-10-21T12:05:00Z", check_ids={HEAD: []})]},
        now="2026-10-21T14:05:00Z",
    )
    assert ci.load_view(conn, now="2026-10-21T14:05:00Z")["repositories"][0]["required"]["state"] == "unknown"
    conn.close()


def test_equal_provider_timestamp_cannot_regress_running_to_queued(tmp_path):
    conn = fleet_hub.init_db(tmp_path / "hub.db")
    value = event()
    value.update(status="in_progress", conclusion=None)
    ci.store(conn, {"events": [value]}, now=NOW)
    value.update(status="queued")
    ci.store(conn, {"events": [value]}, now=NOW)
    assert ci.load_view(conn, operator=True, now=NOW)["repositories"][0]["runs"][0]["status"] == "in_progress"
    conn.close()


@pytest.mark.parametrize("conclusion", [None, "unrecognized"])
def test_reconcile_repairs_unknown_terminal_evidence_at_same_provider_time(tmp_path, conclusion):
    conn = fleet_hub.init_db(tmp_path / "hub.db")
    run = event()
    run.update(run_attempt=2, conclusion=conclusion)
    ci.store(conn, {"events": [run, check(conclusion=conclusion)]}, now=NOW)
    payload = repair.reconcile_repo(reader(tmp_path, Provider()), now=NOW)
    ci.store(conn, payload, now="2026-10-21T12:01:00Z")
    row = ci.load_view(conn, now=NOW)["repositories"][0]
    assert row["required"]["state"] == "passing"
    assert row["latest_run"]["conclusion"] == "success"
    assert row["latest_run"]["source"] == "github-reconcile"
    saved = json.loads(conn.execute("SELECT payload FROM ci_check_runs WHERE object_id=22").fetchone()[0])
    assert (saved["conclusion"], saved["source"], saved["updated_at"]) == ("success", "github-reconcile", NOW)
    conn.close()


@pytest.mark.parametrize("kind", ["workflow_run", "check_run", "status"])
@pytest.mark.parametrize("change", ["unknown", "conflict", "older", "queued", "other-head"])
def test_terminal_refinement_keeps_existing_ordering_guards(tmp_path, kind, change):
    conn = fleet_hub.init_db(tmp_path / "hub.db")
    value = check(event=kind)
    value.update(run_id=11, status_id=33, status="success" if kind == "status" else "completed")
    ci.store(conn, {"events": [value]}, now=NOW)
    if change == "unknown":
        value.update(conclusion=None, status="pending" if kind == "status" else "completed")
    elif change == "conflict":
        value.update(conclusion="failure", status="failure" if kind == "status" else "completed")
    elif change == "older":
        value.update(
            conclusion="failure",
            status="failure" if kind == "status" else "completed",
            updated_at="2026-10-21T11:59:00Z",
        )
    elif change == "queued":
        value.update(status="queued", conclusion=None)
    else:
        value.update(head_sha=OLD)
    ci.store(conn, {"events": [value]}, now="2026-10-21T12:01:00Z")
    table = "ci_runs" if kind == "workflow_run" else "ci_check_runs"
    saved = json.loads(conn.execute(f"SELECT payload FROM {table}").fetchone()[0])
    assert (saved["status"], saved["conclusion"], saved["head_sha"], saved["updated_at"]) == (
        "completed",
        "success",
        HEAD,
        NOW,
    )
    conn.close()


def test_native_etags_survive_node_state_and_reader_recreation(tmp_path):
    state = node.NodeState(tmp_path / "ci.db")
    cache = state.etag_cache("example/project")
    runner = FakeRunner(gh(body=b"[1]", etag='"first"'))
    source = GitHubReader(
        "example/project", visibility="private", runner=runner, ledger=BudgetLedger(tmp_path / "budget.db"), cache=cache
    )
    source.get(PATH, params={}, max_bytes=100)
    state.save_etag_cache("example/project", cache)
    reloaded = node.NodeState(tmp_path / "ci.db")
    second = FakeRunner(gh(304))
    source = GitHubReader(
        "example/project",
        visibility="private",
        runner=second,
        ledger=BudgetLedger(tmp_path / "budget.db"),
        cache=reloaded.etag_cache("example/project"),
    )
    result = source.get(PATH, params={}, max_bytes=100)
    assert result.body == b"[1]"
    assert result.state == "not-modified"
    assert 'If-None-Match: "first"' in second.calls[0][0]
    many = OrderedDict((f"/repos/example/project/pulls?page={i}", ('"v1"', b"[]")) for i in range(140))
    reloaded.save_etag_cache("example/project", many)
    assert len(reloaded.etag_cache("example/project")) <= 128


def test_hub_retention_and_history_queries_are_bounded(tmp_path):
    conn = fleet_hub.init_db(tmp_path / "hub.db")
    values = []
    for index in range(140):
        value = event()
        value.update(run_id=index + 1)
        values.append(value)
    old = check(head_sha=OLD, updated_at="2025-01-01T00:00:00Z")
    ci.store(conn, {"events": [*values, old], "repositories": [observation()]}, now=NOW)
    assert conn.execute("SELECT count(*) FROM ci_runs").fetchone()[0] <= 100
    assert conn.execute("SELECT count(*) FROM ci_check_runs").fetchone()[0] == 0
    assert len(ci.load_view(conn, now=NOW)["repositories"][0]["runs"]) <= 20
    conn.close()


def test_per_repo_hub_failure_does_not_starve_later_inventory(tmp_path, monkeypatch):
    monkeypatch.setattr(
        node,
        "inventory",
        lambda target: [{"repo": "example/a", "visibility": "unknown"}, {"repo": "example/b", "visibility": "unknown"}],
    )

    def reconcile(source, **kw):
        return {"source": "github-reconcile", "events": [], "repositories": [observation(source.repository, "unknown")]}

    monkeypatch.setattr(node, "reconcile_repo", reconcile)
    posts = []

    def transport(url, *, token, body=None):
        if url.endswith("/ci/work"):
            return {"repositories": [], "refresh_requests": {}}
        posts.append(body["repositories"][0]["repo"])
        if len(posts) == 1:
            raise ValueError("Hub unavailable")
        return {"repositories": 1}

    monkeypatch.setattr(node, "fetch_json", transport)
    result = node.reconcile(
        node.NodeState(tmp_path / "ci.db"),
        target=tmp_path,
        hub_url="https://hub.example",
        node_token="example-node",
        now=NOW,
    )
    assert posts == ["example/a", "example/b"]
    assert result["reconciled"] == 1
    assert result["incomplete"] == 1


def test_sqlite_cli_errors_are_sanitized(tmp_path, monkeypatch, capsys):
    from brigade import cli

    monkeypatch.setenv("BRIGADE_EXTRAS", "0")
    monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", "https://hub.example")
    monkeypatch.setenv("BRIGADE_FLEET_NODE_TOKEN", "example-node")

    def broken(*a, **kw):
        raise sqlite3.OperationalError("example private database path")

    monkeypatch.setattr(node, "NodeState", broken)
    assert cli.main(["fleet", "ci", "reconcile", "--state", str(tmp_path / "ci.db")]) == 2
    assert "example private database path" not in capsys.readouterr().err


def test_hub_receipt_accepts_new_observation_from_a_slow_node_clock(tmp_path):
    conn = fleet_hub.init_db(tmp_path / "hub.db")
    ci.store(conn, {"events": [check()], "repositories": [observation()]}, now="2026-10-21T14:00:00Z")
    ci.store(
        conn,
        {
            "repositories": [
                observation(
                    observed_at="2026-10-21T11:55:00Z", head_sha=OLD, check_ids={OLD: []}, checks_complete=[OLD]
                )
            ]
        },
        now="2026-10-21T14:05:00Z",
    )
    row = ci.load_view(conn, now="2026-10-21T14:05:00Z")["repositories"][0]
    assert row["head"]["sha"] == OLD
    assert row["required"]["state"] == "unknown"
    conn.close()


def test_workflow_history_cap_preserves_required_check_completeness(tmp_path):
    conn = fleet_hub.init_db(tmp_path / "hub.db")
    runs = [dict(event(), run_id=i + 1) for i in range(140)]
    ci.store(conn, {"events": [check(), *runs], "repositories": [observation()]}, now=NOW)
    row = ci.load_view(conn, now=NOW)["repositories"][0]
    assert row["complete"]
    assert row["required"]["state"] == "passing"
    assert conn.execute("SELECT count(*) FROM ci_runs").fetchone()[0] == 100
    conn.close()


@pytest.mark.parametrize("history_kind", ["check_run", "check_suite"])
def test_check_cap_prunes_unreferenced_heads_before_current_scope(tmp_path, history_kind):
    conn = fleet_hub.init_db(tmp_path / "hub.db")
    ci.store(conn, {"events": [check()], "repositories": [observation()]}, now=NOW)
    history = [check(check_run_id=i + 100, head_sha=OLD, updated_at="2026-10-21T12:01:00Z") for i in range(1000)]
    if history_kind == "check_suite":
        for value in history:
            value.update(event="check_suite", check_suite_id=value.pop("check_run_id"), head_sha=HEAD)
    ci.store(conn, {"events": history}, now="2026-10-21T12:01:00Z")
    row = ci.load_view(conn, now=NOW)["repositories"][0]
    assert row["complete"]
    assert row["required"]["state"] == "passing"
    assert conn.execute("SELECT count(*) FROM ci_check_runs").fetchone()[0] == 1000
    conn.close()


def test_check_cap_invalidates_only_the_referenced_head_it_trims(tmp_path):
    conn = fleet_hub.init_db(tmp_path / "hub.db")
    snapshot = observation(pulls=[{"number": 5, "head_sha": OLD}], checks_complete=[HEAD, OLD])
    ci.store(conn, {"events": [check(updated_at="2026-10-21T12:01:00Z")], "repositories": [snapshot]}, now=NOW)
    history = [check(check_run_id=i + 100, head_sha=OLD) for i in range(1000)]
    ci.store(conn, {"events": history}, now=NOW)
    row = ci.load_view(conn, now=NOW)["repositories"][0]
    assert row["required"]["state"] == "passing"
    assert row["pulls"][0]["rollup"]["state"] == "unknown"
    assert not row["complete"]
    assert conn.execute("SELECT count(*) FROM ci_check_runs").fetchone()[0] == 1000
    conn.close()


def test_relay_invalidation_respects_hourly_due_but_admin_refresh_bypasses_it(tmp_path, monkeypatch):
    monkeypatch.setattr(node.time, "monotonic", lambda: 100.0)
    conn = fleet_hub.init_db(tmp_path / "hub.db")
    ci.store(conn, {"events": [check()], "repositories": [observation()]}, now=NOW)
    select_policy(conn, "example/public")
    state = node.NodeState(tmp_path / "ci.db")
    state.save(observation(attempted_at=NOW))
    ci.store(
        conn,
        {"invalidations": [{"repo": "example/public", "head_sha": HEAD, "reason": "relay-check-context-unknown"}]},
        now="2026-10-21T12:01:00Z",
    )
    reads = []
    posted = []
    marker = "2026-10-21T12:02:00Z"

    def transport(url, *, token, body=None):
        if url.endswith("/ci/work"):
            return dict(ci.work_plan(conn, operator=False), as_of=marker)
        posted.append(body["repositories"][0])
        return ci.store(conn, body, now=marker)

    def collect(source, **kw):
        reads.append(source.repository)
        return {"repositories": [observation(refresh_id=kw.get("refresh_id"))], "events": []}

    monkeypatch.setattr(node, "fetch_json", transport)
    monkeypatch.setattr(node, "reconcile_repo", collect)
    monkeypatch.setattr(node, "inventory", lambda target: [])
    options = dict(target=tmp_path, hub_url="https://hub.example", node_token="example-node", now=marker)
    assert node.reconcile(state, **options)["reconciled"] == 0
    assert reads == []
    ci.request_refresh(conn, "example/public", now=marker)
    assert node.reconcile(state, **options)["reconciled"] == 1
    assert reads == ["example/public"]
    assert posted[0]["repair_started_at"] == marker
    conn.close()


def test_cursor_updates_are_numeric_and_cannot_rewind(tmp_path):
    state = node.NodeState(tmp_path / "ci.db")
    other = node.NodeState(tmp_path / "ci.db")
    state.set_cursor("https://relay.example", "9")
    other.set_cursor("https://relay.example", "10")
    state.set_cursor("https://relay.example", "8")
    assert other.cursor("https://relay.example") == "10"


def test_replayed_relay_page_cannot_revive_removed_checks_or_invalidate_repair(tmp_path):
    from brigade.fleet_ci_relay import page_batch
    from tests.test_fleet_ci_relay import relay_page

    conn = fleet_hub.init_db(tmp_path / "hub.db")
    page = relay_page()
    payload, _ = page_batch(page, after="0", limit=100, now=NOW)
    # pull supplies the relay origin key, never a provider object identity.
    payload["relay"] = ci.refresh_key("https://relay.example")
    ci.store(conn, {"repositories": [observation()]}, now=NOW)
    ci.store(conn, payload, now="2026-10-21T12:01:00Z")
    snapshot = observation(repair_started_at="2026-10-21T12:02:00Z", check_ids={HEAD: ["check_run:99:1"]})
    ci.store(
        conn,
        {"source": "github-reconcile", "events": [check(check_run_id=99)], "repositories": [snapshot]},
        now="2026-10-21T12:03:00Z",
    )
    before = conn.execute("SELECT payload FROM ci_check_runs WHERE object_id=22").fetchone()[0]
    ci.store(conn, payload, now="2026-10-21T12:04:00Z")
    row = ci.load_view(conn, now=NOW)["repositories"][0]
    assert row["required"]["state"] == "passing"
    assert row["complete"]
    assert row["last_event_received"] == "2026-10-21T12:01:00Z"
    assert conn.execute("SELECT payload FROM ci_check_runs WHERE object_id=22").fetchone()[0] == before
    conn.close()


@pytest.mark.parametrize("marker", ["2026-10-21T12:01:00Z", None])
def test_slow_repair_cannot_erase_a_newer_relay_invalidation(tmp_path, monkeypatch, marker):
    conn = fleet_hub.init_db(tmp_path / "hub.db")
    ci.store(conn, {"events": [check()], "repositories": [observation()]}, now=NOW)
    select_policy(conn, "example/public")
    stamp = ci.stamp
    clock = ["2026-10-21T12:01:00Z"]
    monkeypatch.setattr(ci, "stamp", lambda value=None: stamp(value or clock[0]))
    plan = ci.work_plan(conn, operator=False)
    assert plan["repositories"] == [{"repo": "example/public", "visibility": "public"}]
    marker = plan["as_of"] if marker else None
    ci.store(
        conn,
        {"invalidations": [{"repo": "example/public", "head_sha": HEAD, "reason": "relay-check-context-unknown"}]},
        now="2026-10-21T12:02:00Z",
    )
    ci.store(
        conn,
        {"source": "github-reconcile", "events": [check()], "repositories": [observation(repair_started_at=marker)]},
        now="2026-10-21T12:03:00Z",
    )
    assert ci.load_view(conn, now=NOW)["repositories"][0]["required"]["state"] == "unknown"
    clock[0] = "2026-10-21T12:04:00Z"
    later_plan = ci.work_plan(conn, operator=False)
    ci.store(
        conn,
        {
            "source": "github-reconcile",
            "events": [check()],
            "repositories": [observation(repair_started_at=later_plan["as_of"])],
        },
        now="2026-10-21T12:05:00Z",
    )
    assert ci.load_view(conn, now=NOW)["repositories"][0]["required"]["state"] == "passing"
    conn.close()


def test_nameless_lifecycle_preserves_established_check_identity(tmp_path):
    conn = fleet_hub.init_db(tmp_path / "hub.db")
    ci.store(conn, {"events": [check()], "repositories": [observation()]}, now=NOW)
    ci.store(
        conn,
        {
            "events": [
                check(name=None, app_id=None, status="in_progress", conclusion=None, updated_at="2026-10-21T12:01:00Z")
            ]
        },
        now=NOW,
    )
    row = ci.load_view(conn, now=NOW)["repositories"][0]
    assert row["required"]["state"] == "pending"
    conn.close()


def test_native_etag_bound_is_per_repository(tmp_path):
    state = node.NodeState(tmp_path / "ci.db")
    for repo in ("example/first", "example/second"):
        cache = OrderedDict((f"/repos/{repo}/pulls?page={i}", ('"v1"', b"[]")) for i in range(140))
        state.save_etag_cache(repo, cache)
    assert len(state.etag_cache("example/first")) == 128
    assert len(state.etag_cache("example/second")) == 128
    large = OrderedDict((f"/repos/example/third/pulls?page={i}", ('"v1"', b"x" * (9 * 1024 * 1024))) for i in range(2))
    state.save_etag_cache("example/third", large)
    with sqlite3.connect(state.path) as conn:
        assert conn.execute("SELECT count(*) FROM ci_etags WHERE repo='example/third'").fetchone()[0] == 1
        assert conn.execute("SELECT sum(length(body)) FROM ci_etags").fetchone()[0] <= 16 * 1024 * 1024


def test_workflow_startup_failure_is_a_known_failed_conclusion(tmp_path):
    conn = fleet_hub.init_db(tmp_path / "hub.db")
    ci.store(
        conn,
        {
            "events": [dict(event(), conclusion="startup_failure"), check(conclusion="startup_failure")],
            "repositories": [observation()],
        },
        now=NOW,
    )
    row = ci.load_view(conn, now=NOW)["repositories"][0]
    assert row["latest_run"]["conclusion"] == "startup_failure"
    assert row["required"]["state"] == "not-passing"
    conn.close()


def test_unrepaired_invalidation_bookkeeping_remains_bounded_and_unknown(tmp_path):
    conn = fleet_hub.init_db(tmp_path / "hub.db")
    ci.store(conn, {"events": [check()], "repositories": [observation()]}, now=NOW)
    values = [
        {"repo": "example/public", "head_sha": f"{i:040x}", "reason": "relay-check-context-unknown"}
        for i in range(1000)
    ]
    ci.store(conn, {"invalidations": values}, now="2026-10-21T12:01:00Z")
    ci.store(
        conn,
        {"invalidations": [{"repo": "example/public", "head_sha": HEAD, "reason": "relay-check-context-unknown"}]},
        now="2026-10-21T12:02:00Z",
    )
    assert conn.execute("SELECT count(*) FROM ci_invalidations").fetchone()[0] <= 1000
    ci.store(
        conn, {"repositories": [observation(repair_started_at="2026-10-21T12:01:30Z")]}, now="2026-10-21T12:03:00Z"
    )
    assert ci.load_view(conn, now=NOW)["repositories"][0]["required"]["state"] == "unknown"
    conn.close()


@pytest.mark.parametrize("barrier", [False, True])
@pytest.mark.parametrize("late_marker", ["older", "missing", "equal"])
def test_crossing_repairs_cannot_replace_newer_pending_snapshot(tmp_path, monkeypatch, barrier, late_marker):
    conn = fleet_hub.init_db(tmp_path / "hub.db")
    ci.store(conn, {"events": [check()], "repositories": [observation(check_ids={HEAD: ["check_run:22:1"]})]}, now=NOW)
    select_policy(conn, "example/public")
    stamp = ci.stamp
    clock = ["2026-10-21T12:01:00Z"]
    monkeypatch.setattr(ci, "stamp", lambda value=None: stamp(value or clock[0]))
    plan_a = ci.work_plan(conn, operator=False)
    if barrier:
        ci.store(
            conn,
            {"invalidations": [{"repo": "example/public", "head_sha": HEAD, "reason": "relay-check-context-unknown"}]},
            now="2026-10-21T12:02:00Z",
        )
    clock[0] = "2026-10-21T12:04:00Z"
    plan_b = ci.work_plan(conn, operator=False)
    running = check(
        check_run_id=23,
        status="in_progress",
        conclusion=None,
        created_at="2026-10-21T12:03:00Z",
        updated_at="2026-10-21T12:03:00Z",
    )
    ci.store(
        conn,
        {
            "source": "github-reconcile",
            "events": [running],
            "repositories": [observation(repair_started_at=plan_b["as_of"], check_ids={HEAD: ["check_run:23:1"]})],
        },
        now="2026-10-21T12:05:00Z",
    )
    assert ci.load_view(conn, now=NOW)["repositories"][0]["required"]["state"] == "pending"
    assert conn.execute("SELECT count(*) FROM ci_invalidations").fetchone()[0] == 0
    before = conn.execute("SELECT snapshot FROM ci_repo_state").fetchone()[0]
    # A request not consumed by the newer collector must not be consumed by A.
    ci.request_refresh(conn, "example/public", now=NOW)
    marker = plan_a["as_of"] if late_marker == "older" else plan_b["as_of"] if late_marker == "equal" else None
    result = ci.store(
        conn,
        {
            "source": "github-reconcile",
            "events": [check(), check(check_run_id=99, name="optional")],
            "repositories": [
                observation(repair_started_at=marker, check_ids={HEAD: ["check_run:22:1"]}, refresh_id=NOW)
            ],
        },
        now="2026-10-21T12:06:00Z",
    )
    assert result == {"events": 2, "repositories": 1}
    row = ci.load_view(conn, now=NOW)["repositories"][0]
    assert row["required"]["state"] == "pending"
    assert row["last_reconciled"] == NOW
    assert row["refresh_requested"] == NOW
    assert conn.execute("SELECT snapshot FROM ci_repo_state").fetchone()[0] == before
    assert conn.execute("SELECT count(*) FROM ci_check_runs WHERE object_id=99").fetchone()[0] == 1
    conn.close()


@pytest.mark.parametrize("elapsed,expected", [(45.5, "2026-10-21T12:00:45.500000Z"), (-10, NOW)])
def test_each_repo_repair_marker_uses_hub_time_plus_nonnegative_monotonic_elapsed(
    tmp_path, monkeypatch, elapsed, expected
):
    clock = [100.0]
    monkeypatch.setattr(node.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(node, "inventory", lambda target: [])
    posted = []

    def transport(url, *, token, body=None):
        if url.endswith("/ci/work"):
            return {
                "as_of": NOW,
                "repositories": [
                    {"repo": "example/a", "visibility": "public"},
                    {"repo": "example/b", "visibility": "public"},
                ],
                "refresh_requests": {},
            }
        posted.append(body["repositories"][0])
        return {"repositories": 1}

    def collect(source, **kw):
        # This boundary is immediately before reconcile_repo's first provider GET.
        if source.repository == "example/a":
            clock[0] += elapsed
        return {"source": "github-reconcile", "events": [], "repositories": [observation(source.repository)]}

    monkeypatch.setattr(node, "fetch_json", transport)
    monkeypatch.setattr(node, "reconcile_repo", collect)
    result = node.reconcile(
        node.NodeState(tmp_path / "ci.db"),
        target=tmp_path,
        hub_url="https://hub.example",
        node_token="example-node",
        now="2040-01-01T00:00:00Z",
    )
    assert result == {"reconciled": 2, "incomplete": 0}
    assert posted[0]["repair_started_at"] == NOW
    assert posted[1]["repair_started_at"] == expected


@pytest.mark.parametrize("action", ["closed", "synchronize"])
def test_ahead_node_clock_cannot_suppress_newer_provider_pr_update(tmp_path, action):
    conn = fleet_hub.init_db(tmp_path / "hub.db")
    ci.store(
        conn,
        {
            "repositories": [
                observation(
                    observed_at="2026-10-21T13:00:00Z",
                    repair_started_at=NOW,
                    pulls=[{"number": 5, "head_sha": OLD, "updated_at": NOW}],
                )
            ]
        },
        now="2026-10-21T12:01:00Z",
    )
    pr = dict(
        repo="example/public",
        event="pull_request",
        pr_number=5,
        action=action,
        head_sha=HEAD,
        status="unknown",
        updated_at="2026-10-21T12:02:00Z",
    )
    ci.store(conn, {"events": [pr]}, now="2026-10-21T12:03:00Z")
    pulls = ci.load_view(conn, now="2026-10-21T12:03:00Z")["repositories"][0]["pulls"]
    assert [p["head_sha"] for p in pulls] == ([] if action == "closed" else [HEAD])
    conn.close()


@pytest.mark.parametrize("action,repaired_head", [("synchronize", HEAD), ("closed", HEAD), ("reopened", None)])
def test_late_old_pr_delivery_after_repair_cannot_undo_head_or_closed_inventory(tmp_path, action, repaired_head):
    conn = fleet_hub.init_db(tmp_path / "hub.db")
    pulls = (
        []
        if repaired_head is None
        else [{"number": 5, "head_sha": repaired_head, "updated_at": "2026-10-21T12:04:00Z"}]
    )
    ci.store(
        conn,
        {
            "repositories": [
                observation(observed_at="2026-10-21T11:00:00Z", repair_started_at="2026-10-21T12:05:00Z", pulls=pulls)
            ]
        },
        now="2026-10-21T12:06:00Z",
    )
    old = dict(
        repo="example/public",
        event="pull_request",
        pr_number=5,
        action=action,
        head_sha=OLD,
        status="unknown",
        updated_at="2026-10-21T12:03:00Z",
    )
    ci.store(conn, {"events": [old]}, now="2026-10-21T12:07:00Z")
    current = ci.load_view(conn, now="2026-10-21T12:07:00Z")["repositories"][0]["pulls"]
    assert [p["head_sha"] for p in current] == ([] if repaired_head is None else [repaired_head])
    conn.close()


@pytest.mark.parametrize("updated", [NOW, None])
def test_reconcile_preserves_optional_provider_pr_timestamp_through_hub(tmp_path, updated):
    class PullProvider(Provider):
        def __call__(self, argv, **kw):
            if argv[:2] != ["octopool", "whoami"]:
                path = argv[argv.index("--path") + 1] if argv[0] == "octopool" else argv[7].split("?")[0]
                if path.endswith("/pulls"):
                    self.calls.append(list(argv))
                    return reply(argv, 200, [{"number": 5, "head": {"sha": OLD}, "updated_at": updated}])
            return super().__call__(argv, **kw)

    payload = repair.reconcile_repo(reader(tmp_path, PullProvider()), now="2026-10-21T13:00:00Z")
    assert payload["repositories"][0]["pulls"][0].get("updated_at") == updated
    conn = fleet_hub.init_db(tmp_path / "hub.db")
    ci.store(conn, payload, now="2026-10-21T12:01:00Z")
    stored = json.loads(conn.execute("SELECT snapshot FROM ci_repo_state").fetchone()[0])
    assert stored["pulls"][0].get("updated_at") == updated
    conn.close()


@pytest.mark.parametrize("branch,expected", [(None, 12), ("main", 12), ("feature", 11)])
def test_ahead_node_clock_does_not_hide_new_workflow_or_promote_late_old_completion(tmp_path, branch, expected):
    conn = fleet_hub.init_db(tmp_path / "hub.db")
    ci.store(
        conn,
        {
            "source": "github-reconcile",
            "events": [dict(event(), created_at=NOW)],
            "repositories": [observation(observed_at="2026-10-21T13:00:00Z", latest_run_id=11)],
        },
        now="2026-10-21T12:01:00Z",
    )
    newer = dict(
        event(),
        run_id=12,
        status="queued",
        conclusion=None,
        head_branch=branch,
        created_at="2026-10-21T12:02:00Z",
        updated_at="2026-10-21T12:02:00Z",
    )
    ci.store(conn, {"events": [newer]}, now="2026-10-21T12:03:00Z")
    assert ci.load_view(conn, now="2026-10-21T12:03:00Z")["repositories"][0]["latest_run"]["object_id"] == expected
    old = dict(
        event(), run_id=10, created_at="2026-10-21T11:59:00Z", updated_at="2026-10-21T12:04:00Z", head_branch="main"
    )
    ci.store(conn, {"events": [old]}, now="2026-10-21T12:05:00Z")
    assert ci.load_view(conn, now="2026-10-21T12:05:00Z")["repositories"][0]["latest_run"]["object_id"] == expected
    conn.close()


def test_workflow_evidence_received_before_repair_cannot_override_rest_anchor(tmp_path):
    conn = fleet_hub.init_db(tmp_path / "hub.db")
    history = dict(event(), run_id=12, created_at="2026-10-21T12:02:00Z", updated_at="2026-10-21T12:02:00Z")
    ci.store(conn, {"events": [history]}, now="2026-10-21T12:03:00Z")
    ci.store(
        conn,
        {
            "source": "github-reconcile",
            "events": [dict(event(), created_at=NOW)],
            "repositories": [observation(observed_at="2026-10-21T11:00:00Z", latest_run_id=11)],
        },
        now="2026-10-21T12:04:00Z",
    )
    assert ci.load_view(conn, now="2026-10-21T12:04:00Z")["repositories"][0]["latest_run"]["object_id"] == 11
    conn.close()
