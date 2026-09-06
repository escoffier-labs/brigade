"""Source-side fleet routing with the t3-fleet client as remote worker transport.

Every t3-fleet call is a mocked process client. Hosts, repositories, node ids,
and request ids are fake. Nothing here dispatches live work.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brigade import agents, fleet_policy, fleet_t3_transport as t3, run_transport
from brigade.roster import Agent, Roster
from brigade.run_transport import Assignment

WINDOWS_NODE = "node-windows-origin"
LINUX_NODE = "node-linux-target"
REPO = "example.test/org/repository"
REVISION = "0123456789abcdef0123456789abcdef01234567"
DECISION = "dec-fixed"
RESERVATION = "res-fixed"
DELEGATION = "del-fixed"
QUALIFIED_REQUEST = "linux-worker/req-0001"


def _document() -> dict:
    return {
        "schema": fleet_policy.POLICY_SCHEMA,
        "machines": {
            "windows-origin": {"os": "windows", "node_id": WINDOWS_NODE, "concurrency": 1},
            "linux-worker": {"os": "linux", "node_id": LINUX_NODE, "concurrency": 2},
        },
        "seats": {
            "coder": {
                "provider": "provider-a",
                "model": "model-a-1",
                "bindings": {
                    "brigade": {"cli": "cursor-agent"},
                    "t3_fleet": {"instance_id": "cursor", "service_tier": "standard"},
                    "native": {"instance_id": "cursor-native", "model": "model-a-1"},
                },
            },
            "cli-only": {
                "provider": "provider-b",
                "model": "model-b-1",
                "bindings": {"brigade": {"cli": "codex"}},
            },
        },
        "consumers": {"brigade-run": {"reload": "refreshable", "coverage": "verified"}},
        "repositories": {REPO: {"privacy": "private"}},
        "routing": {"enabled": True},
    }


class FakePolicyClient:
    """Records every Hub call so ownership boundaries stay assertable."""

    def __init__(
        self,
        *,
        selected_machine: str | None = "linux-worker",
        selected_seat: str = "coder",
        reason: str = "selected",
    ):
        self.selected_machine = selected_machine
        self.selected_seat = selected_seat
        self.reason = reason
        self.route_calls: list[dict] = []
        self.delegation_calls: list[dict] = []
        self.renewals: list[dict] = []
        self.releases: list[dict] = []
        self.decision_id = DECISION
        self.reservation_id = RESERVATION

    def show_policy(self) -> dict:
        return {"version": 7, "digest": "sha256:" + "a" * 64, "document": _document()}

    def route_work(self, **kwargs):
        self.route_calls.append(kwargs)
        selected = (
            {"machine": self.selected_machine, "seat": kwargs.get("seat") or self.selected_seat}
            if self.selected_machine
            else None
        )
        return {
            "schema": "brigade.fleet_route.v1",
            "decision_id": self.decision_id,
            "policy_version": 7,
            "policy_digest": "sha256:" + "a" * 64,
            "selected": selected,
            "reason": self.reason,
            "candidates": [],
            "reservation_id": self.reservation_id if selected else None,
            "expires_at": "2099-01-01T00:00:00Z",
        }

    def create_delegation(self, **kwargs):
        self.delegation_calls.append(kwargs)
        return {
            "schema": "brigade.fleet_policy_delegation.v1",
            "delegation_id": DELEGATION,
            "launch_authorized": False,
            "consumer": "brigade-run",
            "adapter": "t3-fleet",
            "decision_id": kwargs["decision_id"],
            "reservation_id": RESERVATION,
            "source_revision": kwargs["source_revision"],
            "target_machine": "linux-worker",
            "target_node": LINUX_NODE,
        }

    def renew_reservation(self, **kwargs):  # pragma: no cover - must never be called
        self.renewals.append(kwargs)
        raise AssertionError("the source must not renew a target-owned reservation")

    def release_reservation(self, **kwargs):  # pragma: no cover - must never be called
        self.releases.append(kwargs)
        raise AssertionError("the source must not release a target-owned reservation")


class FakeProcess:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class FakeT3Runner:
    """Scripted t3-fleet process client. Records full argv for leak assertions."""

    def __init__(self, responses: dict[str, list[dict] | dict]):
        self.responses = responses
        self.argv: list[list[str]] = []
        self.prompt_files: list[str] = []

    def __call__(self, command, **kwargs):
        self.argv.append(list(command))
        verb = command[1]
        if "--prompt-file" in command:
            path = command[command.index("--prompt-file") + 1]
            self.prompt_files.append(Path(path).read_text(encoding="utf-8"))
        entry = self.responses.get(verb)
        if entry is None:
            raise AssertionError(f"unexpected t3-fleet call: {verb}")
        payload = entry.pop(0) if isinstance(entry, list) else entry
        if payload.get("_returncode"):
            return FakeProcess(
                stdout=json.dumps(payload.get("body", {})),
                stderr=payload.get("stderr", ""),
                returncode=int(payload["_returncode"]),
            )
        return FakeProcess(stdout=json.dumps(payload))

    def verbs(self) -> list[str]:
        return [call[1] for call in self.argv]


def _eligible() -> dict:
    return {"schema": "t3-fleet.result.v1", "hosts": [{"host": "linux-worker", "state": "eligible"}]}


def _succeeded(**extra) -> dict:
    body = {
        "schema": "t3-fleet.result.v1",
        "request_id": QUALIFIED_REQUEST,
        "result": {"state": "succeeded", "output": "remote task finished"},
    }
    body["result"].update(extra)
    return body


def _client(runner: FakeT3Runner) -> t3.T3FleetClient:
    return t3.T3FleetClient(executable="t3-fleet", runner=runner)


def _transport(tmp_path: Path, policy: FakePolicyClient, runner: FakeT3Runner, **kwargs) -> t3.SourceTransport:
    return t3.SourceTransport(
        cwd=tmp_path,
        run_id="run-0001",
        repo_identity=REPO,
        node_id=WINDOWS_NODE,
        document=_document(),
        policy_client=policy,
        client=_client(runner),
        **kwargs,
    )


@pytest.fixture
def clean_source(monkeypatch):
    """A clean, immutable source revision without touching a real checkout."""
    monkeypatch.setattr(t3, "immutable_source_revision", lambda cwd, **kw: REVISION)


# --- route resolution --------------------------------------------------------


def test_windows_origin_routes_to_the_allowed_linux_target(tmp_path, clean_source):
    policy = FakePolicyClient()
    runner = FakeT3Runner(
        {"doctor": _eligible(), "capacity": _eligible(), "submit": _succeeded()},
    )
    dispatch = _transport(tmp_path, policy, runner)("coder", "build it", "prompt body")

    assert dispatch is not None and not dispatch.blocked
    assert dispatch.resolution.route == t3.ROUTE_REMOTE
    assert dispatch.resolution.origin_machine == "windows-origin"
    assert dispatch.resolution.target_machine == "linux-worker"
    assert policy.route_calls[0]["origin"] == "windows-origin"
    assert policy.route_calls[0]["consumer"] == "brigade-run"
    assert dispatch.outcome is not None and dispatch.outcome.ok


def test_origin_selection_keeps_the_local_path(tmp_path):
    policy = FakePolicyClient(selected_machine="windows-origin")
    runner = FakeT3Runner({})
    dispatch = _transport(tmp_path, policy, runner)("coder", "build it", "prompt body")

    assert dispatch is None
    assert runner.argv == []
    assert policy.delegation_calls == []


def test_explicit_machine_override_is_recorded_as_a_policy_receipt(tmp_path, clean_source):
    policy = FakePolicyClient()
    runner = FakeT3Runner({"doctor": _eligible(), "capacity": _eligible(), "submit": _succeeded()})
    transport = _transport(
        tmp_path,
        policy,
        runner,
        machine_override="linux-worker",
        override_reason="operator pinned the linux worker",
    )
    dispatch = transport("coder", "build it", "prompt body")

    assert policy.route_calls[0]["machine"] == "linux-worker"
    assert policy.route_calls[0]["override_reason"] == "operator pinned the linux worker"
    assert dispatch is not None
    receipt = dispatch.receipt()["route"]
    assert receipt["override_reason"] == "operator pinned the linux worker"
    assert receipt["origin_machine"] == "windows-origin"


def test_unmapped_origin_identity_is_blocked_not_local(tmp_path):
    policy = FakePolicyClient()
    runner = FakeT3Runner({})
    transport = _transport(tmp_path, policy, runner)
    transport.node_id = "node-not-in-policy"
    dispatch = transport("coder", "build it", "prompt body")

    assert dispatch is not None and dispatch.blocked
    assert dispatch.resolution.reason == "origin-identity-unresolved"
    assert policy.route_calls == []


def test_cli_only_seat_without_a_t3_binding_is_denied(tmp_path, clean_source):
    policy = FakePolicyClient(selected_seat="cli-only")
    runner = FakeT3Runner({})
    dispatch = _transport(tmp_path, policy, runner)("cli-only", "build it", "prompt body")

    assert dispatch is not None and dispatch.blocked
    assert dispatch.resolution.reason == "t3-binding-missing"
    assert runner.argv == []
    assert policy.delegation_calls == []


def test_unverified_repository_is_denied_before_routing(tmp_path):
    policy = FakePolicyClient()
    transport = _transport(tmp_path, policy, FakeT3Runner({}))
    transport.repo_identity = None
    dispatch = transport("coder", "build it", "prompt body")

    assert dispatch is not None and dispatch.blocked
    assert dispatch.resolution.reason == "repo-unverified"
    assert policy.route_calls == []


def test_no_selectable_target_queues_with_the_hub_reason(tmp_path):
    policy = FakePolicyClient(selected_machine=None, reason="capacity-exhausted")
    runner = FakeT3Runner({})
    dispatch = _transport(tmp_path, policy, runner)("coder", "build it", "prompt body")

    assert dispatch is not None and dispatch.blocked
    assert dispatch.resolution.reason == "capacity-exhausted"
    assert runner.argv == []


def test_ineligible_target_blocks_instead_of_falling_back(tmp_path, clean_source):
    policy = FakePolicyClient()
    runner = FakeT3Runner(
        {
            "doctor": {
                "schema": "t3-fleet.result.v1",
                "hosts": [{"host": "linux-worker", "state": "blocked_offline"}],
            }
        }
    )
    dispatch = _transport(tmp_path, policy, runner)("coder", "build it", "prompt body")

    assert dispatch is not None and dispatch.outcome is not None
    assert dispatch.outcome.ok is False
    assert dispatch.outcome.failure_kind == "t3-target-ineligible"
    assert "submit" not in runner.verbs()


# --- immutable source context ------------------------------------------------


def test_dirty_source_is_blocked_with_remote_context_unavailable(tmp_path):
    def runner(command, **kwargs):
        if "rev-parse" in command:
            return FakeProcess(stdout=REVISION + "\n")
        return FakeProcess(stdout=" M src/brigade/run_transport.py\n")

    with pytest.raises(t3.TransportDenial) as excinfo:
        t3.immutable_source_revision(tmp_path, runner=runner)
    assert excinfo.value.code == "remote-context-unavailable"


def test_untracked_content_also_blocks_the_delegated_context(tmp_path):
    def runner(command, **kwargs):
        if "rev-parse" in command:
            return FakeProcess(stdout=REVISION + "\n")
        return FakeProcess(stdout="?? notes.md\n")

    with pytest.raises(t3.TransportDenial) as excinfo:
        t3.immutable_source_revision(tmp_path, runner=runner)
    assert excinfo.value.code == "remote-context-unavailable"


def test_clean_source_returns_the_immutable_forty_character_revision(tmp_path):
    def runner(command, **kwargs):
        return FakeProcess(stdout=(REVISION + "\n") if "rev-parse" in command else "")

    assert t3.immutable_source_revision(tmp_path, runner=runner) == REVISION


def test_dirty_source_never_becomes_a_local_run(tmp_path, monkeypatch):
    monkeypatch.setattr(
        t3,
        "immutable_source_revision",
        lambda cwd, **kw: (_ for _ in ()).throw(
            t3.TransportDenial("remote-context-unavailable", "uncommitted content")
        ),
    )
    policy = FakePolicyClient()
    runner = FakeT3Runner({})
    dispatch = _transport(tmp_path, policy, runner)("coder", "build it", "prompt body")

    assert dispatch is not None and dispatch.outcome is not None
    assert dispatch.outcome.failure_kind == "remote-context-unavailable"
    assert policy.delegation_calls == []
    assert runner.argv == []


# --- delegation adoption and the qualified handle ----------------------------


def test_one_delegation_adopts_the_existing_decision_without_a_second_reservation(tmp_path, clean_source):
    policy = FakePolicyClient()
    runner = FakeT3Runner(
        {
            "doctor": [_eligible(), _eligible()],
            "capacity": [_eligible(), _eligible()],
            "submit": [_succeeded(), _succeeded()],
        }
    )
    transport = _transport(tmp_path, policy, runner)
    transport("coder", "build it", "prompt body")
    transport("coder", "build it", "prompt body")

    assert len(policy.delegation_calls) == 2
    assert {call["decision_id"] for call in policy.delegation_calls} == {DECISION}
    assert {call["parent_request_id"] for call in policy.delegation_calls} == {"brigade-run:run-0001:coder"}
    assert {call["source_revision"] for call in policy.delegation_calls} == {REVISION}
    assert {call["session_id"] for call in policy.route_calls} == {"run-0001:coder"}


def test_submit_passes_only_the_delegation_id_and_never_a_generated_request_id(tmp_path, clean_source):
    policy = FakePolicyClient()
    runner = FakeT3Runner({"doctor": _eligible(), "capacity": _eligible(), "submit": _succeeded()})
    _transport(tmp_path, policy, runner)("coder", "build it", "prompt body")

    submit = next(call for call in runner.argv if call[1] == "submit")
    assert "--delegation-id" in submit and submit[submit.index("--delegation-id") + 1] == DELEGATION
    assert "--request-id" not in submit
    assert "--host" not in submit and "--repository" not in submit


def test_the_qualified_request_id_is_persisted_before_polling(tmp_path, clean_source):
    handles: list[tuple[str, str]] = []
    policy = FakePolicyClient()
    runner = FakeT3Runner(
        {
            "doctor": _eligible(),
            "capacity": _eligible(),
            "submit": {"schema": "t3-fleet.result.v1", "request_id": QUALIFIED_REQUEST, "state": "running"},
            "wait": _succeeded(),
        }
    )
    transport = _transport(tmp_path, policy, runner, on_handle=lambda d, r: handles.append((d, r)))
    dispatch = transport("coder", "build it", "prompt body")

    assert handles == [(DELEGATION, QUALIFIED_REQUEST)]
    assert runner.verbs().index("wait") > runner.verbs().index("submit")
    assert dispatch is not None and dispatch.outcome is not None
    assert dispatch.outcome.request_id == QUALIFIED_REQUEST


def test_unknown_outcome_recovers_instead_of_resubmitting(tmp_path, clean_source):
    policy = FakePolicyClient()
    runner = FakeT3Runner(
        {
            "doctor": _eligible(),
            "capacity": _eligible(),
            "submit": {
                "_returncode": 1,
                "body": {
                    "schema": "t3-fleet.error.v1",
                    "error": {"code": "unknown_state", "message": "controller lost the reply"},
                    "request_id": QUALIFIED_REQUEST,
                },
            },
            "recover": {
                "schema": "t3-fleet.result.v1",
                "request_id": QUALIFIED_REQUEST,
                "result": {"state": "running"},
            },
        }
    )
    dispatch = _transport(tmp_path, policy, runner)("coder", "build it", "prompt body")

    assert runner.verbs().count("submit") == 1
    assert "recover" in runner.verbs()
    assert dispatch is not None and dispatch.outcome is not None
    assert dispatch.outcome.ok is False
    assert dispatch.outcome.state == "running"
    assert dispatch.outcome.request_id == QUALIFIED_REQUEST


def test_wait_timeout_is_not_terminal_success(tmp_path, clean_source):
    policy = FakePolicyClient()
    runner = FakeT3Runner(
        {
            "doctor": _eligible(),
            "capacity": _eligible(),
            "submit": {"schema": "t3-fleet.result.v1", "request_id": QUALIFIED_REQUEST, "state": "running"},
            "wait": {
                "schema": "t3-fleet.result.v1",
                "request_id": QUALIFIED_REQUEST,
                "result": {"state": "running"},
            },
        }
    )
    dispatch = _transport(tmp_path, policy, runner)("coder", "build it", "prompt body")

    assert dispatch is not None and dispatch.outcome is not None
    assert dispatch.outcome.ok is False
    assert dispatch.outcome.state == "running"
    assert dispatch.outcome.disposition is None
    assert policy.releases == [] and policy.renewals == []


@pytest.mark.parametrize(
    "state,kind",
    [
        ("failed", "t3-failed"),
        ("interrupted", "t3-interrupted"),
        ("needs_attention", "t3-needs-attention"),
        ("blocked_offline", "t3-blocked-offline"),
    ],
)
def test_non_success_states_stay_distinct(tmp_path, clean_source, state, kind):
    policy = FakePolicyClient()
    runner = FakeT3Runner(
        {
            "doctor": _eligible(),
            "capacity": _eligible(),
            "submit": {
                "schema": "t3-fleet.result.v1",
                "request_id": QUALIFIED_REQUEST,
                "result": {"state": state},
            },
        }
    )
    dispatch = _transport(tmp_path, policy, runner)("coder", "build it", "prompt body")

    assert dispatch is not None and dispatch.outcome is not None
    assert dispatch.outcome.state == state
    assert dispatch.outcome.failure_kind == kind
    assert dispatch.outcome.ok is False


def test_delegation_flag_rejected_by_an_older_client_is_a_routing_rejection(tmp_path, clean_source):
    policy = FakePolicyClient()
    runner = FakeT3Runner(
        {
            "doctor": _eligible(),
            "capacity": _eligible(),
            "submit": {
                "_returncode": 2,
                "body": {},
                "stderr": "t3-fleet submit: error: unrecognized arguments: --delegation-id del-fixed",
            },
        }
    )
    dispatch = _transport(tmp_path, policy, runner)("coder", "build it", "prompt body")

    assert dispatch is not None and dispatch.outcome is not None
    assert dispatch.outcome.failure_kind == "t3-delegation-unsupported"
    assert runner.verbs().count("submit") == 1


# --- results stay target-owned -----------------------------------------------


def test_writable_remote_result_is_pending_handoff_not_locally_implemented(tmp_path, clean_source):
    policy = FakePolicyClient()
    runner = FakeT3Runner(
        {
            "doctor": _eligible(),
            "capacity": _eligible(),
            "submit": _succeeded(
                disposition="pending-handoff",
                handoff={
                    "target_machine": "linux-worker",
                    "repository": "configured-repo-alias",
                    "branch": "work/remote",
                    "source_revision": REVISION,
                    "request_id": QUALIFIED_REQUEST,
                    "worktree_ref": "opaque-target-ref",
                },
            ),
        }
    )
    dispatch = _transport(tmp_path, policy, runner)("coder", "build it", "prompt body")

    assert dispatch is not None and dispatch.outcome is not None
    outcome = dispatch.outcome
    assert outcome.ok is True and outcome.state == "succeeded"
    assert outcome.disposition == "pending-handoff"
    assert set(outcome.handoff) == set(t3.HANDOFF_FIELDS)
    assert outcome.handoff["worktree_ref"] == "opaque-target-ref"
    receipt = dispatch.receipt()
    assert receipt["disposition"] == "pending-handoff"
    assert receipt["handoff"]["target_machine"] == "linux-worker"


def test_a_running_state_never_claims_a_completed_handoff(tmp_path, clean_source):
    policy = FakePolicyClient()
    runner = FakeT3Runner(
        {
            "doctor": _eligible(),
            "capacity": _eligible(),
            "submit": {"schema": "t3-fleet.result.v1", "request_id": QUALIFIED_REQUEST, "state": "running"},
            "wait": {
                "schema": "t3-fleet.result.v1",
                "request_id": QUALIFIED_REQUEST,
                "result": {
                    "state": "running",
                    "disposition": "pending-handoff",
                    "handoff": {"target_machine": "linux-worker"},
                },
            },
        }
    )
    dispatch = _transport(tmp_path, policy, runner)("coder", "build it", "prompt body")

    assert dispatch is not None and dispatch.outcome is not None
    assert dispatch.outcome.disposition is None
    assert dispatch.outcome.handoff == {}


def test_the_source_never_leaks_the_prompt_or_a_secret_onto_argv(tmp_path, clean_source):
    policy = FakePolicyClient()
    runner = FakeT3Runner({"doctor": _eligible(), "capacity": _eligible(), "submit": _succeeded()})
    secret_prompt = "do the work\nAPI_TOKEN=not-a-real-token"
    _transport(tmp_path, policy, runner)("coder", "build it", secret_prompt)

    flattened = " ".join(" ".join(call) for call in runner.argv)
    assert "not-a-real-token" not in flattened
    assert "do the work" not in flattened
    assert runner.prompt_files == [secret_prompt]


def test_the_prompt_file_is_removed_after_submit(tmp_path, clean_source):
    seen: list[str] = []

    def runner(command, **kwargs):
        if command[1] == "submit":
            seen.append(command[command.index("--prompt-file") + 1])
            return FakeProcess(stdout=json.dumps(_succeeded()))
        return FakeProcess(stdout=json.dumps(_eligible()))

    client = t3.T3FleetClient(executable="t3-fleet", runner=runner)
    resolution = t3.RouteResolution(
        route=t3.ROUTE_REMOTE,
        reason="remote-selected",
        seat="coder",
        origin_machine="windows-origin",
        target_machine="linux-worker",
        decision_id=DECISION,
    )
    t3.execute_remote(
        resolution,
        "prompt body",
        title="build it",
        cwd=tmp_path,
        client=client,
        policy_client=FakePolicyClient(),
        parent_request_id="brigade-run:run-0001:coder",
        source_revision=REVISION,
    )
    assert seen and not Path(seen[0]).exists()


# --- run_transport interception ----------------------------------------------


def _roster() -> Roster:
    return Roster(
        orchestrator="chef",
        agents={
            "chef": Agent(name="chef", cli="codex", role="plan"),
            "coder": Agent(name="coder", cli="coder", role="write"),
        },
        max_workers=1,
    )


def _dispatch(monkeypatch, tmp_path, *, remote_transport, lease_calls, provider_calls):
    def fake_run_agent(cli_ref, prompt, **kwargs):
        provider_calls.append(cli_ref)
        return agents.AgentResult(text="local output", ok=True)

    monkeypatch.setattr(agents, "run_agent", fake_run_agent)

    class _Lease:
        def __init__(self, agent):
            self.agent = agent

        def __enter__(self):
            lease_calls.append(self.agent.name)
            return None

        def __exit__(self, *exc):
            return False

    return run_transport.dispatch(
        [Assignment(worker="coder", task="build it")],
        _roster(),
        build_prompt=lambda agent, assignment, **kw: assignment.task,
        run_appserver_worker=lambda *a, **kw: agents.AgentResult(text="", ok=False, detail="unused"),
        event_writer=lambda events_dir, worker, verbose=False: None,
        cwd=tmp_path,
        read_only=True,
        output_dir=tmp_path,
        model_lease=_Lease,
        remote_transport=remote_transport,
    )


def test_remote_route_takes_no_local_lease_and_calls_no_provider(monkeypatch, tmp_path, clean_source):
    policy = FakePolicyClient()
    runner = FakeT3Runner(
        {
            "doctor": _eligible(),
            "capacity": _eligible(),
            "submit": _succeeded(
                disposition="pending-handoff",
                handoff={
                    "target_machine": "linux-worker",
                    "repository": "configured-repo-alias",
                    "branch": "work/remote",
                    "source_revision": REVISION,
                    "request_id": QUALIFIED_REQUEST,
                    "worktree_ref": "opaque-target-ref",
                },
            ),
        }
    )
    leases: list[str] = []
    providers: list[str] = []
    results = _dispatch(
        monkeypatch,
        tmp_path,
        remote_transport=_transport(tmp_path, policy, runner),
        lease_calls=leases,
        provider_calls=providers,
    )

    assert leases == []
    assert providers == []
    assert results[0].ok is True
    assert results[0].transport == "t3-fleet"
    assert results[0].request_id == QUALIFIED_REQUEST
    assert results[0].remote is not None
    assert results[0].remote["disposition"] == "pending-handoff"
    assert results[0].remote["handoff"]["worktree_ref"] == "opaque-target-ref"
    assert results[0].remote["route"]["target_machine"] == "linux-worker"


def test_origin_route_still_leases_and_calls_the_local_provider(monkeypatch, tmp_path):
    policy = FakePolicyClient(selected_machine="windows-origin")
    leases: list[str] = []
    providers: list[str] = []
    results = _dispatch(
        monkeypatch,
        tmp_path,
        remote_transport=_transport(tmp_path, policy, FakeT3Runner({})),
        lease_calls=leases,
        provider_calls=providers,
    )

    assert leases == ["coder"]
    assert providers == ["coder"]
    assert results[0].ok is True
    assert results[0].text == "local output"
    assert results[0].remote is None


def test_blocked_route_is_a_routing_failure_not_a_silent_local_run(monkeypatch, tmp_path):
    policy = FakePolicyClient(selected_machine=None, reason="privacy-denied")
    leases: list[str] = []
    providers: list[str] = []
    results = _dispatch(
        monkeypatch,
        tmp_path,
        remote_transport=_transport(tmp_path, policy, FakeT3Runner({})),
        lease_calls=leases,
        provider_calls=providers,
    )

    assert leases == []
    assert providers == []
    assert results[0].ok is False
    assert results[0].failure_phase == "routing"
    assert results[0].failure_kind == "privacy-denied"
    assert results[0].remote is not None
    assert results[0].remote["routing"]["reason"] == "privacy-denied"


def test_no_transport_wired_keeps_the_existing_local_path(monkeypatch, tmp_path):
    leases: list[str] = []
    providers: list[str] = []
    results = _dispatch(
        monkeypatch,
        tmp_path,
        remote_transport=None,
        lease_calls=leases,
        provider_calls=providers,
    )

    assert leases == ["coder"] and providers == ["coder"]
    assert results[0].remote is None


# --- wiring gates ------------------------------------------------------------


def test_routing_disabled_policy_builds_no_transport(tmp_path, monkeypatch):
    from brigade import fleet_session_bootstrap

    monkeypatch.setattr(fleet_session_bootstrap, "classify_enrollment", lambda snapshot: "enrolled")
    document = _document()
    document["routing"] = {"enabled": False}
    built = t3.build_source_transport(
        cwd=tmp_path,
        run_id="run-0001",
        snapshot={"state": "authoritative"},
        policy_client=FakePolicyClient(),
        node_id=WINDOWS_NODE,
        document=document,
    )
    assert built is None


def test_enabled_routing_builds_a_transport_with_the_resolved_origin(tmp_path, monkeypatch):
    from brigade import fleet_session_bootstrap

    monkeypatch.setattr(fleet_session_bootstrap, "classify_enrollment", lambda snapshot: "enrolled")
    built = t3.build_source_transport(
        cwd=tmp_path,
        run_id="run-0001",
        snapshot={"state": "authoritative"},
        policy_client=FakePolicyClient(),
        node_id=WINDOWS_NODE,
        document=_document(),
    )
    assert built is not None
    assert built.node_id == WINDOWS_NODE
    assert t3.resolve_origin_machine(built.document, built.node_id) == "windows-origin"


def test_unenrolled_snapshot_builds_no_transport(tmp_path):
    assert (
        t3.build_source_transport(
            cwd=tmp_path,
            run_id="run-0001",
            snapshot={"state": "unconfigured", "models": []},
            policy_client=FakePolicyClient(),
            node_id=WINDOWS_NODE,
            document=_document(),
        )
        is None
    )


def test_the_orchestrator_attempt_never_receives_the_remote_transport():
    """Local orchestration capacity stays distinct from delegated worker capacity."""
    import inspect

    from brigade.aboyeur import planning

    assert "remote_transport" not in inspect.signature(planning._run_orchestrator).parameters
    assert "remote_transport" not in inspect.signature(planning.plan).parameters
    assert "remote_transport" in inspect.signature(planning.dispatch).parameters
    assert "remote_transport" in inspect.signature(run_transport.dispatch).parameters


def test_the_orchestrator_keeps_its_own_local_lease(monkeypatch):
    """A chef seat is never rerouted into a recursive T3 delegate."""
    from brigade.aboyeur import planning

    source = inspect_source(planning._run_orchestrator)
    assert "remote_transport" not in source
    assert "model_lease(orchestrator)" in source


def inspect_source(function) -> str:
    import inspect

    return inspect.getsource(function)
