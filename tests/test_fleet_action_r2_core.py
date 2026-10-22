"""R2 regressions at source, broker journal, ledger, and browser boundaries."""

import json
import os
import re
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import pytest

from brigade import fleet_hub, fleet_hub_actions as actions
from brigade import fleet_action_auth as auth, fleet_action_sources as sources
from tests.test_fleet_actions import setup_action, seed_t3_roster
from tests.test_fleet_action_runner import broker_fixture, fake_executable, manifest
from tests.test_fleet_action_http import action_hub, enable, fields, request_http
from tests.test_fleet_sync import _event


requires_posix = pytest.mark.skipif(
    os.name != "posix", reason="Broker execution requires POSIX private files, process groups, and systemd"
)


def run_action(
    tmp_path, *, state="run.completed", exit_status=1, t3=False, monkeypatch=None, repo="repo/fixture", native=True
):
    from brigade import fleet_action_targets as targets, fleet_hub_policy, fleet_policy
    from brigade import fleet_action_runner as runner, fleet_client_actions as client
    from brigade.fleet_action_http import handle_api

    conn, _, _, request = setup_action(tmp_path)
    source = {"kind": "run", "node_id": "node-fixture", "run_id": "source-run"}
    fleet_hub.store_events(
        conn,
        [
            {
                **_event(node="node-fixture", run_id="source-run", state=state),
                "repo_identity": repo,
                "exit_status": exit_status,
                "ts": (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat(),
            }
        ],
        caller_node="node-fixture",
    )
    target = json.loads(conn.execute("SELECT document FROM action_targets").fetchone()[0])
    target = {**target, "subjects": [source]}
    if t3:
        seed_t3_roster(conn)
        document = fleet_policy.empty_document()
        document["seats"] = {"fixture-seat": {"provider": "fixture-provider", "model": "current-model"}}
        fleet_hub_policy.save_policy(conn, document, expected_version=1, actor="fixture", reason="test")
        target = {k: v for k, v in target.items() if k not in {"agent_id"}}
        target.update(
            target_id="repair-t3",
            family="t3",
            host="fixture-host",
            repository="fixture-repo",
            repo_identity="repo/fixture",
            source_revision="a" * 40,
            models=[{"id": "fixture-seat", "label": "model"}],
        )
    auth.grant_broker(conn, "node-fixture", [target["target_id"]], [source])
    grant = auth.grant_operator(conn, "operator@example.test", [{"target_id": target["target_id"], "subject": source}])
    targets.sync(conn, "node-fixture", [target])
    path = log = None
    if t3 and native:
        exe, log = fake_executable(
            tmp_path,
            {
                "schema": "t3-fleet.result.v1",
                "request_id": "fixture-host-fixture-repo-12345678-1234-1234-1234-123456789abc",
                "state": "running",
            },
        )
        path = manifest(
            tmp_path,
            exe,
            target,
            binding={k: target[k] for k in ("host", "repository", "repo_identity", "source_revision")},
        )
        monkeypatch.setattr(client, "request", lambda body, **kw: handle_api(conn, (False, "node-fixture"), body))
        runner.sync_targets(path)
    action = actions.submit(
        conn,
        grant,
        {
            **request,
            "source": source,
            "target_id": target["target_id"],
            "model_id": "fixture-seat" if t3 else "",
            "observed_revision": sources.resolve(conn, source)["revision"],
        },
    )
    return conn, action, path, log


@pytest.mark.parametrize(
    "state,exit_status",
    [
        ("run.started", None),
        ("external.started", None),
        ("run.awaiting_approval", None),
        ("unknown", None),
        ("run.completed", 1),
        ("external.completed", 2),
        ("run.failed", None),
    ],
)
def test_unrelated_success_never_clears_original_source(tmp_path, state, exit_status):
    conn, action, _, _ = run_action(tmp_path, state=state, exit_status=exit_status)
    fleet_hub.store_events(
        conn,
        [
            {
                **_event(node="node-fixture", run_id="other", state="run.completed"),
                "repo_identity": "repo/fixture",
                "exit_status": 0,
                "ts": datetime.now(timezone.utc).isoformat(),
            }
        ],
        caller_node="node-fixture",
    )
    assert actions.show(conn, action["action_id"])["verification"] == "pending"
    conn.close()


@pytest.mark.parametrize("repo", [None, "repo/fixture"])
@pytest.mark.parametrize("completion", ["run.completed", "external.completed"])
def test_exact_source_success_can_verify_itself(tmp_path, repo, completion):
    conn, action, _, _ = run_action(tmp_path, state="run.started", exit_status=None, repo=repo)
    event = {
        **_event(node="node-fixture", run_id="source-run", state=completion),
        "sequence": 2,
        "repo_identity": "repo/fixture",
        "exit_status": 0,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    fleet_hub.store_events(conn, [event], caller_node="node-fixture")
    assert actions.show(conn, action["action_id"])["verification"] == "condition-cleared"
    conn.close()


@requires_posix
def test_native_regression_keeps_running_then_finishes_with_one_submit(tmp_path, monkeypatch):
    from brigade import fleet_action_runner as runner

    conn, action, path, log = run_action(tmp_path, t3=True, monkeypatch=monkeypatch)
    claim = actions.claim(conn, "node-fixture")
    assert runner.execute(path, action["action_id"], claim["generation"])["status"] == "running"
    fake_executable(
        tmp_path,
        {
            "schema": "t3-fleet.result.v1",
            "request_id": "fixture-host-fixture-repo-12345678-1234-1234-1234-123456789abc",
            "state": "queued",
        },
    )
    assert runner.execute(path, action["action_id"], claim["generation"])["status"] == "running"
    fake_executable(
        tmp_path,
        {
            "schema": "t3-fleet.result.v1",
            "request_id": "fixture-host-fixture-repo-12345678-1234-1234-1234-123456789abc",
            "state": "succeeded",
        },
    )
    assert runner.execute(path, action["action_id"], claim["generation"])["status"] == "finished"
    assert sum(json.loads(line)["args"][0] == "submit" for line in log.read_text().splitlines()) == 1
    conn.close()


@requires_posix
def test_refused_stale_outbox_is_audited_then_polled_again(tmp_path, monkeypatch):
    from brigade import fleet_action_runner as runner, fleet_client_actions as client

    conn, path, log, action = broker_fixture(tmp_path, monkeypatch)
    claim = actions.claim(conn, "node-fixture")
    normal = client.request

    def refuse(body, **kw):
        if body["op"] == "report":
            raise auth.ActionError("transition-refused", 409)
        return normal(body, **kw)

    monkeypatch.setattr(client, "request", refuse)
    with pytest.raises(auth.ActionError, match="transition-refused"):
        runner.execute(path, action["action_id"], claim["generation"])
    monkeypatch.setattr(client, "request", normal)
    calls = []
    monkeypatch.setattr(
        runner.adapters,
        "recover",
        lambda *a: calls.append(1) or {"status": "finished", "destination_ref": "fixture-turn"},
    )
    assert runner.execute(path, action["action_id"], claim["generation"])["status"] == "finished"
    assert calls == [1]
    record = json.loads((tmp_path / "journal" / (action["action_id"] + ".jsonl")).read_text())
    assert record["rejections"][0]["reason"] == "transition-refused"
    assert len(log.read_text().splitlines()) == 1
    conn.close()


def test_t3_closed_refusal_and_lease_contract(tmp_path, monkeypatch):
    conn, action, _, _ = run_action(tmp_path, t3=True, native=False)
    claim = actions.claim(conn, "node-fixture")
    started = actions.start(conn, "node-fixture", action["action_id"], claim["generation"])
    assert started["claim_until"] - started["started_at"] >= 240
    result = actions.report(
        conn,
        "node-fixture",
        action["action_id"],
        claim["generation"],
        {"event_id": "refused", "status": "failed", "rejection_code": "policy_rejected"},
    )
    assert result["status"] == "failed" and result["destination_ref"] is None
    conn.close()


def test_compose_single_post_preserves_selected_destination_and_prompt(tmp_path):
    from brigade import fleet_action_targets as targets

    with action_hub(tmp_path) as (address, path, _, request):
        conn = fleet_hub.open_db(path)
        original = json.loads(conn.execute("SELECT document FROM action_targets").fetchone()[0])
        beta = {**original, "target_id": "beta", "agent_id": "fixture-beta", "label": "Beta"}
        auth.grant_broker(conn, "node-fixture", ["repair", "beta"], [request["source"]])
        auth.grant_operator(
            conn, "operator@example.test", [{"target_id": t, "subject": request["source"]} for t in ("repair", "beta")]
        )
        targets.sync(conn, "node-fixture", [original, beta])
        conn.close()
        cookie = enable(address)
        url = "/deck/actions/compose?" + urlencode(
            {
                "source": json.dumps(request["source"]),
                "prompt": "injected draft",
                "client_id": "injected-id",
                "model_id": "injected-model",
            }
        )
        code, _, body = request_http(address, url, headers={"Cookie": cookie})
        assert code == 200 and "injected draft" not in body and "injected-id" not in body
        forms = re.findall(r"<form.*?</form>", body, re.S)
        assert len(forms) == 1 and 'method="post"' in forms[0] and '<select name="target_id">' in forms[0]
        post = {**fields(body), "target_id": "beta", "model_id": "", "family": "", "prompt": "edited diagnosis"}
        code, headers, _ = request_http(address, "/deck/actions/submit", data=post, headers={"Cookie": cookie})
        assert code == 303
        conn = fleet_hub.open_db(path)
        saved = actions.show(conn, headers["Location"].rsplit("/", 1)[1])
        assert saved["target_id"] == "beta" and saved["request"]["prompt"] == "edited diagnosis"
        conn.close()


@pytest.mark.parametrize(
    "values,expected_target,expected_model",
    [
        ({}, "repair-t3", "fixture-seat"),
        ({"model_id": ""}, "repair-t3", "fixture-seat"),
        ({"target_id": "repair-t3-z", "model_id": "fixture-z-seat"}, "repair-t3-z", "fixture-z-seat"),
        ({"target_id": "repair-openclaw"}, "repair-openclaw", ""),
    ],
)
def test_compose_model_selection_belongs_to_chosen_destination(tmp_path, values, expected_target, expected_model):
    from brigade import fleet_action_page as page, fleet_action_targets as targets, fleet_hub_model_roster as roster

    conn, action, _, _ = run_action(tmp_path, t3=True, native=False)
    source = action["request"]["source"]
    roster.set_model_policy(
        conn,
        {
            "action": "set",
            "expected_revision": roster.raw_revision(conn),
            "provider": "openai",
            "model": "alternate-model",
            "seat": "fixture-z-seat",
            "enabled": True,
            "reasoning": "high",
            "t3_instance_id": "fixture-instance",
            "t3_model": "alternate-native-model",
        },
    )
    original = {k: v for k, v in action["request"]["target"].items() if k in targets.FIELDS}
    original["models"].append({"id": "fixture-z-seat", "label": "alternate"})
    twin = {**original, "target_id": "repair-t3-z", "label": "Second T3"}
    claw = {
        **original,
        "target_id": "repair-openclaw",
        "family": "openclaw",
        "agent_id": "fixture-agent",
        "models": [],
    }
    auth.grant_broker(conn, "node-fixture", [t["target_id"] for t in (original, twin, claw)], [source])
    targets.sync(conn, "node-fixture", [original, twin, claw])
    # Keep the initial destination T3 while also offering a non-T3 default.
    catalog = targets.available(conn, source=source)
    catalog.sort(key=lambda t: t["family"] != "t3")
    body = page.compose(sources.resolve(conn, source), catalog, nonce="fixture", csrf="fixture", values=values)
    submitted = fields(body)
    selected = targets.selected(conn, submitted["target_id"], source, submitted["model_id"])
    assert selected["target_id"] == expected_target
    assert submitted["model_id"] == expected_model
    model_select = re.search(r'<select name="model_id">(.*?)</select>', body, re.S).group(1)
    selected_groups = [
        label
        for label, options in re.findall(r'<optgroup label="([^"]*)">(.*?)</optgroup>', model_select, re.S)
        if re.search(r"<option[^>]* selected", options)
    ]
    assert selected_groups == ([selected["label"]] if expected_model else [])
    assert len(re.findall(r"<option[^>]* selected", model_select)) == bool(expected_model)
    with pytest.raises(auth.ActionError, match="target-unavailable"):
        targets.selected(conn, "repair-t3-z", source, "")
    conn.close()


def test_inbox_status_does_not_claim_execution():
    from brigade.fleet_action_page import status

    html = status(
        {
            "action_id": "aa_fixture",
            "status": "accepted",
            "verification": "pending",
            "delivery": "delivered",
            "destination_ref": "7",
            "request": {"target": {"capability": "inbox"}, "prompt": "inspect", "evidence": {}},
            "events": [],
        },
        "nonce",
    )
    assert "Execution: not reported" in html and "Delivered, awaiting pickup" in html


def inbox_action(tmp_path, monkeypatch):
    from brigade import fleet_action_runner as runner, fleet_action_targets as targets, fleet_client_actions as client
    from brigade.fleet_action_http import handle_api

    conn, _, _, request = setup_action(tmp_path)
    original = json.loads(conn.execute("SELECT document FROM action_targets").fetchone()[0])
    inbox = {k: v for k, v in original.items() if k != "agent_id"}
    inbox.update(target_id="fixture-inbox", family="dot", capability="inbox", principal="node-fixture")
    auth.grant_broker(conn, "node-fixture", ["fixture-inbox"], [request["source"]])
    grant = auth.grant_operator(
        conn, "operator@example.test", [{"target_id": "fixture-inbox", "subject": request["source"]}]
    )
    targets.sync(conn, "node-fixture", [inbox])
    exe, log = fake_executable(tmp_path, {"error": "ambiguous reply"})
    path = manifest(tmp_path, exe, inbox)
    data = json.loads(path.read_text())
    data["targets"][0]["binding"] = {}
    path.write_text(json.dumps(data))
    monkeypatch.setattr(client, "request", lambda body, **kw: handle_api(conn, (False, "node-fixture"), body))
    runner.sync_targets(path)
    action = actions.submit(conn, grant, {**request, "target_id": "fixture-inbox"})
    return conn, path, log, action


@pytest.mark.parametrize("queued", [False, True])
@pytest.mark.parametrize("found", [False, True])
@requires_posix
def test_attempted_inbox_send_only_reads_stable_id_and_never_invents_pending(tmp_path, monkeypatch, found, queued):
    from brigade import fleet_action_runner as runner

    conn, path, log, action = inbox_action(tmp_path, monkeypatch)
    claim = actions.claim(conn, "node-fixture")
    normal = runner.adapters.invoke
    helper_calls = []

    def invoke(executable, argv, **kwargs):
        if argv[:3] == ["-m", "brigade.fleet_action_adapters", "--inbox-send"]:
            assert executable == Path(sys.executable)
            helper_calls.append(argv)
            return {"queued": True, "client_id": action["action_id"]} if queued else {"error": "ambiguous"}
        return normal(executable, argv, **kwargs)

    monkeypatch.setattr(runner.adapters, "invoke", invoke)
    # Queue-only output is no longer an accepted initial-action receipt.
    assert runner.execute(path, action["action_id"], claim["generation"])["status"] == "unknown"
    fake_executable(
        tmp_path, {"messages": [{"id": 7, "client_id": action["action_id"], "acked_at": None}] if found else []}
    )
    result = runner.execute(path, action["action_id"], claim["generation"])
    calls = [json.loads(line)["args"][:3] for line in log.read_text().splitlines()]
    assert len(helper_calls) == 1
    assert calls == [["fleet", "msg", "sent"]]
    assert result["status"] == ("accepted" if found else "unknown")
    assert result["delivery"] == ("delivered" if found else "none")
    journal = json.loads((tmp_path / "journal" / (action["action_id"] + ".jsonl")).read_text())
    assert journal["receipt"]["status"] == ("accepted" if found else "unknown")
    if not found:
        assert "delivery" not in journal["receipt"]
    conn.close()


@pytest.mark.parametrize("failure", ["broker-scope-refused", "fence-refused", "source-changed"])
@requires_posix
def test_terminal_report_refusal_is_held_without_repeat_calls(tmp_path, monkeypatch, failure):
    from brigade import fleet_action_runner as runner, fleet_client_actions as client

    conn, path, log, action = broker_fixture(tmp_path, monkeypatch)
    claim = actions.claim(conn, "node-fixture")
    normal = client.request
    calls = []

    def reject(body, **kw):
        calls.append(body["op"])
        if body["op"] == "report":
            raise auth.ActionError(failure, 403 if failure == "broker-scope-refused" else 409)
        return normal(body, **kw)

    monkeypatch.setattr(client, "request", reject)
    with pytest.raises(auth.ActionError, match=failure):
        runner.execute(path, action["action_id"], claim["generation"])
    before = list(calls)
    with pytest.raises(auth.ActionError, match=failure):
        runner.execute(path, action["action_id"], claim["generation"])
    assert calls == before and len(log.read_text().splitlines()) == 1
    conn.close()


@requires_posix
def test_stale_generation_is_held_without_repeated_hub_calls(tmp_path, monkeypatch):
    from brigade import fleet_action_runner as runner, fleet_client_actions as client

    conn, path, log, action = broker_fixture(tmp_path, monkeypatch)
    claim = actions.claim(conn, "node-fixture")
    normal = client.request
    calls = []

    def counted(body, **kw):
        calls.append(body["op"])
        return normal(body, **kw)

    monkeypatch.setattr(client, "request", counted)
    for _ in range(2):
        with pytest.raises(auth.ActionError, match="fence-refused"):
            runner.execute(path, action["action_id"], claim["generation"] + 1)
    assert calls == ["show"] and not log.exists()
    assert runner.execute(path, action["action_id"], claim["generation"])["status"] == "finished"
    conn.close()


@pytest.mark.parametrize(
    "code", [None, "unknown_state", "host_unavailable", "fabricated", ["policy_rejected"], {"code": "policy_rejected"}]
)
def test_no_reference_failure_needs_closed_t3_preaccept_code(tmp_path, monkeypatch, code):
    conn, action, _, _ = run_action(tmp_path, t3=True, native=False)
    claim = actions.claim(conn, "node-fixture")
    actions.start(conn, "node-fixture", action["action_id"], claim["generation"])
    receipt = {"event_id": "refusal", "status": "failed"}
    if code is not None:
        receipt["rejection_code"] = code
    with pytest.raises(auth.ActionError):
        actions.report(conn, "node-fixture", action["action_id"], claim["generation"], receipt)
    assert actions.show(conn, action["action_id"])["status"] == "dispatching"
    conn.close()


@requires_posix
def test_queued_native_state_preserves_latest_hub_state_beyond_history_page(tmp_path, monkeypatch):
    from brigade import fleet_action_runner as runner

    conn, action, path, _ = run_action(tmp_path, t3=True, monkeypatch=monkeypatch)
    claim = actions.claim(conn, "node-fixture")
    runner.execute(path, action["action_id"], claim["generation"])
    for index in range(101):
        actions.report(
            conn,
            "node-fixture",
            action["action_id"],
            claim["generation"],
            {"event_id": "progress-" + str(index), "status": "running"},
        )
    actions.report(
        conn,
        "node-fixture",
        action["action_id"],
        claim["generation"],
        {"event_id": "input-required", "status": "needs-input"},
    )
    fake_executable(
        tmp_path,
        {
            "schema": "t3-fleet.result.v1",
            "request_id": "fixture-host-fixture-repo-12345678-1234-1234-1234-123456789abc",
            "state": "queued",
            "thread_id": "12345678-1234-1234-1234-123456789abc",
        },
    )
    assert runner.execute(path, action["action_id"], claim["generation"])["status"] == "needs-input"
    for native, expected in (("unknown", "unknown"), ("queued", "needs-input")):
        fake_executable(
            tmp_path,
            {
                "schema": "t3-fleet.result.v1",
                "request_id": "fixture-host-fixture-repo-12345678-1234-1234-1234-123456789abc",
                "state": native,
                "thread_id": "12345678-1234-1234-1234-123456789abc",
            },
        )
        assert runner.execute(path, action["action_id"], claim["generation"])["status"] == expected
    conn.close()


@requires_posix
def test_repeated_native_state_after_unknown_is_a_new_observation_not_old_ack(tmp_path, monkeypatch):
    from brigade import fleet_action_runner as runner

    conn, action, path, log = run_action(tmp_path, t3=True, monkeypatch=monkeypatch)
    claim = actions.claim(conn, "node-fixture")
    for native, expected in (("running", "running"), ("unknown", "unknown"), ("running", "running")):
        fake_executable(
            tmp_path,
            {
                "schema": "t3-fleet.result.v1",
                "request_id": "fixture-host-fixture-repo-12345678-1234-1234-1234-123456789abc",
                "state": native,
            },
        )
        assert runner.execute(path, action["action_id"], claim["generation"])["status"] == expected
    calls = [json.loads(line)["args"][0] for line in log.read_text().splitlines()]
    assert calls == ["submit", "status", "recover"]
    conn.close()
