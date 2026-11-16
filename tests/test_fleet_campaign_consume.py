"""Node consumer contracts (#1128 slice 3), run against the real Hub store and ownership fencing."""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import pytest

from brigade import fleet_campaign_consume as consumer
from brigade import fleet_hub
from brigade.worklore_client import WorkloreClientError
from tests.test_work_dispatch import dispatch_target, requires_dispatch_apply  # noqa: F401
from tests.test_work_dispatch_apply import apply_target  # noqa: F401
from tests.test_worklore_campaign_take import OFFER, call, create, create_body, ledger  # noqa: F401

KEY = "camp-1"
REPOS = {"acme/one": Path("/unused/one"), "acme/two": Path("/unused/two"), "acme/excluded": Path("/unused/x")}
REVISION = "c" * 40


class Node:
    """The Worklore client surface for one authenticated node, backed by the in-process Hub."""

    def __init__(self, conn, node):
        self.conn, self.node, self.nonces = conn, node, []

    def _send(self, method, path, body=None, headers=None):
        status, payload = call(self.conn, path, body, node=self.node, headers=headers, method=method)
        if status >= 400:
            raise WorkloreClientError(str(payload.get("error")), code=payload.get("code"))
        return payload

    def get_campaign(self, campaign_key):
        return self._send("GET", f"/work/campaigns/{quote(campaign_key, safe='')}")

    def get_item(self, work_id):
        return self._send("GET", f"/work/items/{work_id}")

    def get_ownership(self, work_id):
        return self._send("GET", f"/work/items/{work_id}/ownership")

    def ownership_action(self, work_id, body, *, if_match, idempotency_key, holder_nonce):
        if body["action"] == "take":
            self.nonces.append(holder_nonce)
        headers = {"If-Match": str(if_match), "Idempotency-Key": idempotency_key, "X-Worklore-Holder": holder_nonce}
        return self._send("POST", f"/work/items/{work_id}/ownership", body, headers)

    def link_execution(self, work_id, body, *, generation, holder_nonce):
        headers = {"X-Worklore-Generation": str(generation), "X-Worklore-Holder": holder_nonce}
        return self._send("POST", f"/work/items/{work_id}/execution", body, headers)

    def record_attempt(self, work_id, body, *, if_match, generation, holder_nonce):
        headers = {
            "If-Match": str(if_match),
            "X-Worklore-Generation": str(generation),
            "X-Worklore-Holder": holder_nonce,
        }
        return self._send("POST", f"/work/items/{work_id}/attempts", body, headers)


class Runs:
    """A fake local launcher that records what it ran and tells the Hub about the run, as brigade run does."""

    def __init__(self, conn, node, *, state="completed", visible=True, die=False, launched=None):
        self.conn, self.node, self.state, self.visible, self.die = conn, node, state, visible, die
        self.launched: list[str] = [] if launched is None else launched

    def __call__(self, path, *, title, repo, work_id, description, acceptance):
        self.launched.append(repo)
        if self.die:
            raise Killed
        if self.state == "refused":
            return consumer.Launch("refused")
        run_id = f"run-{self.node}-{len(self.launched)}-{repo.replace('/', '_')}"
        if self.visible:
            self.conn.execute(
                "INSERT INTO events (node_id, run_id, sequence, digest, state, ts, received_at) "
                "VALUES (?, ?, 1, 'd', 'running', 'now', 'now')",
                (self.node, run_id),
            )
            self.conn.commit()
        return consumer.Launch(self.state, run_id, "brigade/dispatch/task-1", REVISION)


class Killed(BaseException):
    """The holder process dies inside the launcher; nothing after the take runs."""


@pytest.fixture
def hub(ledger):  # noqa: F811
    conn, path, clock = ledger
    create(conn)
    now = lambda: datetime.fromisoformat(clock[0])  # noqa: E731
    return conn, path, clock, now


def run(conn, node, clock_now, launcher, *, watch=True, tried=None, hub_override=None):
    events: list[dict] = []
    passes = consumer.consume(
        hub_override or Node(conn, node),
        watch=watch,
        campaign_key=KEY,
        node_id=node,
        repos=REPOS,
        launcher=launcher,
        emit=events.append,
        tried=set() if tried is None else tried,
        now=clock_now,
    )
    return passes, events


def member_ids(conn):
    return {m["repo"]: m["work_id"] for m in Node(conn, "node-a").get_campaign(KEY)["members"]}


def event_types(conn, work_id):
    return [
        row[0] for row in conn.execute("SELECT event_type FROM work_events WHERE work_id=? ORDER BY seq", (work_id,))
    ]


def test_two_consumers_never_work_the_same_member(hub):
    conn, _, _, now = hub
    first = Runs(conn, "node-a")
    second = Runs(conn, "node-b")
    passes_a, _ = run(conn, "node-a", now, first)
    passes_b, _ = run(conn, "node-b", now, second)
    # node-a holds acme/one under a one-slot offer, so it cannot also take acme/two; node-b gets that one.
    assert first.launched == ["acme/one"] and second.launched == ["acme/two"]
    assert [p.outcome for p in passes_a + passes_b] == ["parked", "parked"]
    # Nothing is left for either node: the third member is excluded and the others are held.
    assert [p.outcome for p in run(conn, "node-a", now, first)[0]] == ["idle"]
    assert [p.outcome for p in run(conn, "node-b", now, second)[0]] == ["idle"]
    assert first.launched == ["acme/one"] and second.launched == ["acme/two"]


def test_competing_consumers_on_separate_connections_split_the_members(hub):
    conn, path, _, now = hub
    barrier = threading.Barrier(2)
    launched, errors = [], []

    def contender(node):
        db = fleet_hub.open_db(path)
        try:
            runs = Runs(db, node, launched=launched)
            barrier.wait(timeout=5)
            run(db, node, now, runs)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            db.close()

    threads = [threading.Thread(target=contender, args=(n,)) for n in ("node-a", "node-b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert not errors and all(not thread.is_alive() for thread in threads)
    assert sorted(launched) == ["acme/one", "acme/two"]


def test_killed_holder_is_fenced_out_by_the_next_holders_generation(hub):
    conn, _, clock, now = hub
    ids = member_ids(conn)
    work_id = ids["acme/one"]
    dead = Node(conn, "node-a")
    with pytest.raises(Killed):
        run(conn, "node-a", now, Runs(conn, "node-a", die=True), watch=False, hub_override=dead)
    stale_nonce = dead.nonces[-1]  # the excluded member was refused first
    assert dead.get_ownership(work_id)["ownership"]["generation"] == 1
    # The lease has not run out, so another node must not take the member.
    assert [p.outcome for p in run(conn, "node-b", now, Runs(conn, "node-b"), watch=False)[0]] == ["parked"]
    assert Node(conn, "node-b").get_ownership(ids["acme/two"])["ownership"]["generation"] == 1
    # The dead holder had already linked its run under generation 1 before it stopped.
    run_id = "run-node-a-late"
    conn.execute(
        "INSERT INTO events (node_id, run_id, sequence, digest, state, ts, received_at) "
        "VALUES ('node-a', ?, 1, 'd', 'running', 'now', 'now')",
        (run_id,),
    )
    conn.commit()
    dead.link_execution(work_id, {"node_id": "node-a", "run_id": run_id}, generation=1, holder_nonce=stale_nonce)
    clock[0] = "2026-01-01T00:02:00+00:00"
    passes, _ = run(conn, "node-b", now, Runs(conn, "node-b"), watch=False)
    assert [(p.repo, p.outcome, p.generation) for p in passes] == [("acme/one", "parked", 2)]
    owned = Node(conn, "node-b").get_ownership(work_id)["ownership"]
    assert owned["generation"] == 2 and owned["owner_node"] == "node-b"
    attempts = conn.execute("SELECT attempt_count FROM work_items WHERE work_id=?", (work_id,)).fetchone()[0]
    # The old holder wakes up late and tries to spend an attempt on its old generation.
    version = dead.get_item(work_id)["item"]["version"]
    with pytest.raises(WorkloreClientError) as attempt:
        dead.record_attempt(
            work_id, {"action": "failed", "run_id": run_id}, if_match=version, generation=1, holder_nonce=stale_nonce
        )
    assert attempt.value.code in {"stale-generation", "holder-mismatch"}
    assert conn.execute("SELECT attempt_count FROM work_items WHERE work_id=?", (work_id,)).fetchone()[0] == attempts
    stale = {"action": "release", "generation": 1}
    with pytest.raises(WorkloreClientError) as release:
        dead.ownership_action(
            work_id, stale, if_match=owned["revision"], idempotency_key="late", holder_nonce=stale_nonce
        )
    assert release.value.code in {"stale-generation", "holder-mismatch", "forbidden"}
    assert Node(conn, "node-b").get_ownership(work_id)["ownership"]["generation"] == 2


def test_resume_dispatches_only_members_that_are_still_eligible(hub):
    conn, _, _, now = hub
    ids = member_ids(conn)
    conn.execute("UPDATE work_items SET status='completed' WHERE work_id=?", (ids["acme/one"],))
    conn.commit()
    first = Runs(conn, "node-a")
    assert [p.repo for p in run(conn, "node-a", now, first)[0]] == ["acme/two"]
    resumed = Runs(conn, "node-a")
    assert [p.outcome for p in run(conn, "node-a", now, resumed)[0]] == ["idle"]
    assert first.launched == ["acme/two"] and resumed.launched == []
    # A terminal member is not selectable even when its ownership record is untouched.
    shown = Node(conn, "node-a").get_campaign(KEY)["members"]
    assert {m["repo"]: m["bucket"] for m in shown}["acme/one"] == "completed"


def test_successful_run_leaves_the_member_open_and_is_not_rerun(hub):
    conn, _, clock, now = hub
    work_id = member_ids(conn)["acme/one"]
    runs = Runs(conn, "node-a")
    passes, events = run(conn, "node-a", now, runs, watch=False)
    assert [p.outcome for p in passes] == ["parked"]
    item = Node(conn, "node-a").get_item(work_id)["item"]
    assert item["status"] not in {"completed", "canceled", "archived"} and item["attempt_count"] == 0
    parked = Node(conn, "node-a").get_ownership(work_id)["ownership"]
    assert parked["next_action"]["kind"] == "await-review"
    assert parked["evidence_refs"][0]["ref"] == passes[0].run_id
    assert "brigade/dispatch/task-1" in parked["next_action"]["resume_condition"]
    # Every step left a Hub event, and the local receipt names the same steps.
    assert event_types(conn, work_id)[1:] == [
        "ownership-taken",
        "execution-linked",
        "attempt-started",
        "ownership-checkpoint",
    ]
    assert [e["event"] for e in events] == ["take", "launch", "attempt", "checkpoint"]
    # The lease runs out; a finished run is parked, so no consumer reclaims and reruns it.
    clock[0] = "2026-01-01T00:10:00+00:00"
    again = Runs(conn, "node-b")
    assert [p.repo for p in run(conn, "node-b", now, again)[0] if p.outcome != "idle"] == ["acme/two"]
    assert "acme/one" not in again.launched
    assert Node(conn, "node-a").get_item(work_id)["item"]["status"] not in {"completed", "canceled", "archived"}


def test_failed_run_counts_an_attempt_and_stops_at_the_attempt_limit(hub):
    conn, _, _, now = hub
    ids = member_ids(conn)
    runs = Runs(conn, "node-a", state="failed")
    first, events = run(conn, "node-a", now, runs)
    # Each member is tried once per call, so a failure is not retried in a tight loop.
    assert sorted(runs.launched) == ["acme/one", "acme/two"] and {p.outcome for p in first} == {"failed"}
    assert "attempt-failed" in event_types(conn, ids["acme/one"])
    assert [e["action"] for e in events if e["event"] == "attempt"] == ["started", "failed"] * 2
    run(conn, "node-a", now, runs)
    third, _ = run(conn, "node-a", now, runs)
    assert [p.outcome for p in third] == ["idle"] and len(runs.launched) == 4
    shown = {m["repo"]: m for m in Node(conn, "node-a").get_campaign(KEY)["members"]}
    assert shown["acme/one"]["bucket"] == "attempt_limited" and shown["acme/one"]["attempt_count"] == 2


def test_run_the_hub_never_saw_is_parked_for_an_operator_and_not_repeated(hub):
    conn, _, clock, now = hub
    work_id = member_ids(conn)["acme/one"]
    runs = Runs(conn, "node-a", visible=False)
    passes, _ = run(conn, "node-a", now, runs, watch=False)
    assert [p.outcome for p in passes] == ["unlinked"]
    assert Node(conn, "node-a").get_ownership(work_id)["ownership"]["next_action"]["kind"] == "blocked"
    clock[0] = "2026-01-01T00:10:00+00:00"
    later = Runs(conn, "node-b")
    run(conn, "node-b", now, later)
    assert "acme/one" not in later.launched


def test_refused_launch_hands_the_member_back_without_an_attempt(hub):
    conn, _, _, now = hub
    work_id = member_ids(conn)["acme/one"]
    passes, _ = run(conn, "node-a", now, Runs(conn, "node-a", state="refused"), watch=False)
    assert [p.outcome for p in passes] == ["refused"]
    assert Node(conn, "node-a").get_ownership(work_id)["ownership"]["state"] == "unowned"
    assert conn.execute("SELECT attempt_count FROM work_items WHERE work_id=?", (work_id,)).fetchone()[0] == 0


def test_receipt_writer_appends_one_json_line_per_step(tmp_path):
    emit = consumer.receipt_writer(tmp_path, KEY)
    emit({"event": "take", "repo": "acme/one"})
    emit({"event": "launch", "repo": "acme/one"})
    lines = (tmp_path / ".brigade" / "campaign-consume" / f"{KEY}.jsonl").read_text().splitlines()
    assert [json.loads(line)["event"] for line in lines] == ["take", "launch"]


@requires_dispatch_apply
def test_launch_local_runs_only_its_own_task_through_dispatch_apply(apply_target):  # noqa: F811
    launch = consumer.launch_local(
        apply_target,
        title="Update fixtures",
        repo="acme/one",
        work_id="wl-1",
        description="Shared spec",
        acceptance=["Checks pass"],
        worker="coder",
    )
    assert launch.state == "completed" and launch.run_id and launch.branch.startswith("brigade/dispatch/")
    assert len(launch.revision or "") == 40
    intents = [
        json.loads(path.read_text()) for path in (apply_target / ".brigade/dispatch").glob("dispatch-*/intents/*.json")
    ]
    # The ledger already held a ready task. Only the campaign task was dispatched.
    assert [row["task_id"] for row in intents] != ["task-0"] and all(row["task_id"] != "task-0" for row in intents)
    assert len(intents) == 1


def test_cli_refuses_without_local_repositories_before_touching_the_hub(tmp_path, capsys):
    from brigade import cli

    code = cli.main(["fleet", "campaign", "consume", KEY, "--target", str(tmp_path), "--worker", "coder", "--json"])
    assert code == 2
    assert json.loads(capsys.readouterr().err) == {"error": "repository_selection_refused"}
