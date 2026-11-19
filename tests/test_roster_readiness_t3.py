"""Parent-reported T3 route support plus Brigade governance for route t3."""

from __future__ import annotations

import copy
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from brigade import native_t3_admission, native_t3_capacity, roster as roster_mod, roster_readiness as readiness
from brigade import roster_readiness_t3 as t3

ROSTER = """\
orchestrator = "lead"

[limits]
max_workers = 4

[agents.lead]
cli = "codex"
role = "lead"

[agents.coder]
cli = "codex"
role = "coder"
model = "launch-model"

[t3_targets.coder]
origin_environment = "env-local"
origin_thread = "thread-parent"
target_environment = "env-local"
target_project = "project-test"
provider_instance = "codex-test"
driver = "codex"
model = "launch-model"
isolation = "worktree"
max_live = 2

[t3_targets.coder.options]
reasoningEffort = "high"
"""


def _iso(offset: float = 0.0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset)).isoformat()


def _payload(**changes: object) -> dict:
    payload: dict = {
        "schema": t3.SCHEMA,
        "origin_environment": "env-local",
        "origin_project": "project-test",
        "reported_at": _iso(),
        "capabilities": {
            "parentThreadId": "thread-parent",
            "inheritedProviderInstanceId": "codex-test",
            "inheritedModel": "launch-model",
            "inheritedOptions": [],
            "runtimeMode": "auto",
            "interactionMode": "default",
            "providers": [
                {
                    "providerInstanceId": "codex-test",
                    "driverKind": "codex",
                    "displayName": "Codex Test",
                    "models": [
                        {
                            "id": "launch-model",
                            "label": "Launch Model",
                            "options": [{"id": "reasoningEffort", "values": ["low", "high"], "default": "high"}],
                        }
                    ],
                    "omittedModels": 0,
                    "runtimeModes": ["auto", "full-access"],
                    "canRunChildTask": True,
                    "canRunCrossProviderChildTask": True,
                    "constraints": [],
                }
            ],
            "features": {
                "appOwnedSubagents": True,
                "asyncPolling": True,
                "cancellation": True,
                "batchThreadCreation": True,
                "threadManagement": True,
                "incrementalThreadRead": True,
                "scheduledTasks": True,
                "maxBatchThreads": 20,
            },
        },
    }
    payload.update(changes)
    return payload


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test User")
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-q", "-m", "initial")
    (repo / ".brigade").mkdir()
    (repo / ".brigade" / "roster.toml").write_text(ROSTER, encoding="utf-8")
    return repo


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch):
    """Every test reads a real, empty Brigade home, so Hub reads are honest."""
    home = tmp_path / "brigade-home"
    home.mkdir()
    monkeypatch.setenv("BRIGADE_HOME", str(home))


def _roster(workspace: Path, text: str | None = None) -> roster_mod.Roster:
    path = workspace / ".brigade" / "roster.toml"
    if text is not None:
        path.write_text(text, encoding="utf-8")
    resolution = roster_mod.resolve_roster(workspace, None)
    return roster_mod.load_roster(resolution.path, resolution=resolution)


def _write(tmp_path: Path, payload: object, name: str = "caps.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _inspect(
    workspace: Path,
    tmp_path: Path,
    *,
    payload: object | None = None,
    caps_path: Path | None = None,
    roster_text: str | None = None,
    isolation: str = "worktree",
    concurrency: int = 1,
    workers: tuple[str, ...] = ("coder",),
    hub_discovery_possible: bool = False,
) -> dict:
    report = t3.load_capabilities(caps_path) if caps_path is not None else None
    if payload is not None:
        report = t3.load_capabilities(_write(tmp_path, payload))
    return readiness.inspect_readiness(
        _roster(workspace, roster_text),
        readiness.ReadinessRequest(workers, concurrency=concurrency, route="t3", isolation=isolation),
        workspace=workspace,
        t3_report=report,
        hub_discovery_possible=hub_discovery_possible,
    )


def _row(report: dict, category: str, seat: str | None = None) -> dict:
    return next(
        item
        for item in report["evidence"]
        if item["category"] == category and (seat is None or item.get("seat") == seat)
    )


def test_eligible_seat_is_ready_with_exact_qualifiers_and_mixed_sources(workspace, tmp_path):
    report = _inspect(workspace, tmp_path, payload=_payload())
    assert report["status"] == "ready"
    assert report["exit_code"] == 0
    assert (report["assurance"], report["launch_guaranteed"]) == ("parent-reported", False)
    assert (report["capacity_reserved"], report["parent_active"]) == (False, "unverified")
    assert report["admitted_capacity"] == 2
    assert report["t3_reported_at"] <= report["t3_received_at"]
    assert _row(report, "route-support")["source"] == "parent-reported"
    for category in ("isolation", "admission", "capacity"):
        assert _row(report, category)["source"] == "brigade-governance"


def test_missing_envelope_is_unverified_caller_context(workspace, tmp_path):
    report = _inspect(workspace, tmp_path)
    assert report["status"] == "unverified"
    row = _row(report, "route-support")
    assert (row["status"], row["reason"]) == ("unknown", "t3-caller-context-unavailable")
    assert report["assurance"] == "parent-reported"
    assert report["parent_active"] == "unverified"


@pytest.mark.parametrize(
    "changes",
    [
        {"origin_environment": "env-other"},
        {"origin_project": "project-other"},
        {"capabilities.parentThreadId": "thread-other"},
    ],
)
def test_context_mismatch_is_unsupported(workspace, tmp_path, changes):
    payload = _payload()
    for dotted, value in changes.items():
        target = payload
        parts = dotted.split(".")
        for part in parts[:-1]:
            target = target[part]
        target[parts[-1]] = value
    report = _inspect(workspace, tmp_path, payload=payload)
    assert report["status"] == "blocked"
    assert _row(report, "route-support")["status"] == "unsupported"


@pytest.mark.parametrize(
    "mutate,reason",
    [
        (lambda p: p["capabilities"]["providers"].clear(), "provider-missing"),
        (lambda p: p["capabilities"]["providers"][0].update(driverKind="claudeAgent"), "driver-mismatch"),
        (lambda p: p["capabilities"]["providers"][0].update(canRunChildTask=False), "child-task-disabled"),
        (
            lambda p: p["capabilities"]["providers"][0].update(constraints=["provider is unavailable"]),
            "provider-constrained",
        ),
        (
            lambda p: (
                p["capabilities"].update(inheritedProviderInstanceId="other-provider")
                or p["capabilities"]["providers"][0].update(canRunCrossProviderChildTask=False)
            ),
            "cross-provider-disabled",
        ),
    ],
)
def test_provider_refusals_block(workspace, tmp_path, mutate, reason):
    payload = _payload()
    mutate(payload)
    report = _inspect(workspace, tmp_path, payload=payload)
    assert report["status"] == "blocked"
    assert _row(report, "route-support")["reason"] == reason


@pytest.mark.parametrize(
    "omitted,status,reason", [(0, "blocked", "model-unavailable"), (2, "unverified", "model-not-observed")]
)
def test_unlisted_model_depends_on_omitted_count(workspace, tmp_path, omitted, status, reason):
    payload = _payload()
    provider = payload["capabilities"]["providers"][0]
    provider["models"] = []
    provider["omittedModels"] = omitted
    report = _inspect(workspace, tmp_path, payload=payload)
    assert report["status"] == status
    assert _row(report, "route-support")["reason"] == reason


def test_option_value_outside_allowed_values_blocks(workspace, tmp_path):
    payload = _payload()
    payload["capabilities"]["providers"][0]["models"][0]["options"][0]["values"] = ["low"]
    report = _inspect(workspace, tmp_path, payload=payload)
    assert report["status"] == "blocked"
    assert _row(report, "route-support")["reason"] == "option-value-undeclared"


def test_absent_option_descriptors_pass_with_unchecked_label(workspace, tmp_path):
    payload = _payload()
    del payload["capabilities"]["providers"][0]["models"][0]["options"]
    report = _inspect(workspace, tmp_path, payload=payload)
    assert report["status"] == "ready"
    row = _row(report, "route-support")
    assert row["status"] == "passed"
    assert row["reason"] == "options-unchecked-by-t3"


def test_explicit_runtime_mode_must_be_declared(workspace, tmp_path):
    text = ROSTER.replace("max_live = 2", 'max_live = 2\nruntime_mode = "full-access"')
    payload = _payload()
    payload["capabilities"]["providers"][0]["runtimeModes"] = ["auto"]
    report = _inspect(workspace, tmp_path, payload=payload, roster_text=text)
    assert report["status"] == "blocked"
    assert _row(report, "route-support")["reason"] == "runtime-mode-undeclared"


def test_explicit_mode_ceiling_is_labelled_unchecked(workspace, tmp_path):
    text = ROSTER.replace("max_live = 2", 'max_live = 2\nruntime_mode = "auto"')
    report = _inspect(workspace, tmp_path, payload=_payload(), roster_text=text)
    assert report["status"] == "ready"
    assert "mode-ceiling-unchecked" in _row(report, "route-support")["detail"]


@pytest.mark.parametrize("field", ["appOwnedSubagents", "cancellation"])
def test_required_features_block(workspace, tmp_path, field):
    payload = _payload()
    payload["capabilities"]["features"][field] = False
    report = _inspect(workspace, tmp_path, payload=payload)
    assert report["status"] == "blocked"


@pytest.mark.parametrize(
    "offset,status,reason",
    [(-120, "unverified", "t3-capabilities-stale"), (120, "unverified", "t3-capabilities-future")],
)
def test_reported_at_must_be_fresh(workspace, tmp_path, offset, status, reason):
    payload = _payload(reported_at=_iso(offset))
    report = _inspect(workspace, tmp_path, payload=payload)
    assert report["status"] == status
    assert _row(report, "route-support")["reason"] == reason


def test_shared_isolation_is_unsupported(workspace, tmp_path):
    report = _inspect(workspace, tmp_path, payload=_payload(), isolation="shared")
    assert report["status"] == "blocked"
    assert _row(report, "isolation")["reason"] == "isolation-shared-unsupported"


def test_zero_capacity_blocks_and_non_authoritative_is_unknown(workspace, tmp_path, monkeypatch):
    monkeypatch.setattr(
        native_t3_capacity, "governed_occupancy", lambda _workspace: native_t3_capacity.Occupancy(4, (), True)
    )
    report = _inspect(workspace, tmp_path, payload=_payload())
    assert report["status"] == "blocked"
    assert _row(report, "capacity")["reason"] == "insufficient-capacity"

    monkeypatch.setattr(
        native_t3_capacity,
        "governed_occupancy",
        lambda _workspace: native_t3_capacity.Occupancy(0, (), False, "run-unverified"),
    )
    report = _inspect(workspace, tmp_path, payload=_payload())
    assert report["status"] == "unverified"
    assert _row(report, "capacity")["status"] == "unknown"


def test_enrolled_unreadable_hub_policy_is_admission_unknown(workspace, tmp_path, monkeypatch):
    monkeypatch.setattr(
        native_t3_admission,
        "read_hub_authority",
        lambda _workspace: native_t3_admission.HubAuthority(state="unavailable"),
    )
    report = _inspect(workspace, tmp_path, payload=_payload())
    assert report["status"] == "unverified"
    assert _row(report, "admission")["status"] == "unknown"


MALFORMED = {
    "oversized": "x" * (t3.MAX_BYTES + 1),
    "too-many-providers": None,
    "too-many-models": None,
    "duplicate-provider": None,
    "duplicate-model": None,
    "duplicate-option": None,
    "duplicate-value": None,
    "bad-boolean": None,
}


def _malformed_payload(name: str) -> dict:
    payload = _payload()
    provider = payload["capabilities"]["providers"][0]
    if name == "too-many-providers":
        payload["capabilities"]["providers"] = [
            {**copy.deepcopy(provider), "providerInstanceId": f"provider-{index}"}
            for index in range(t3.MAX_PROVIDERS + 1)
        ]
    elif name == "too-many-models":
        provider["models"] = [
            {"id": f"model-{index}", "label": "x", "options": []} for index in range(t3.MAX_MODELS + 1)
        ]
    elif name == "duplicate-provider":
        payload["capabilities"]["providers"].append(copy.deepcopy(provider))
    elif name == "duplicate-model":
        provider["models"].append(copy.deepcopy(provider["models"][0]))
    elif name == "duplicate-option":
        provider["models"][0]["options"].append(copy.deepcopy(provider["models"][0]["options"][0]))
    elif name == "duplicate-value":
        provider["models"][0]["options"][0]["values"] = ["high", "high"]
    elif name == "bad-boolean":
        provider["canRunChildTask"] = 1
    return payload


@pytest.mark.parametrize("name", list(MALFORMED))
def test_malformed_envelopes_block(workspace, tmp_path, name):
    if name == "oversized":
        caps_path = tmp_path / "oversized.json"
        caps_path.write_text(json.dumps(_payload()) + "x" * t3.MAX_BYTES, encoding="utf-8")
        report = _inspect(workspace, tmp_path, caps_path=caps_path)
    else:
        report = _inspect(workspace, tmp_path, payload=_malformed_payload(name))
    assert report["status"] == "blocked"
    assert _row(report, "route-support")["reason"] == "t3-capabilities-malformed"


def test_parser_refuses_unknown_and_duplicate_json_keys(tmp_path):
    path = _write(tmp_path, _payload())
    path.write_text(path.read_text(encoding="utf-8").replace('"schema":', '"extra": 1, "schema":', 1), encoding="utf-8")
    assert t3.load_capabilities(path).state == "malformed"
    path.write_text('{"schema": "a", "schema": "b"}', encoding="utf-8")
    assert t3.load_capabilities(path).state == "malformed"


def test_target_change_invalidates_scope(workspace, tmp_path):
    loaded = _roster(workspace)
    request = readiness.ReadinessRequest(("coder",), route="t3", isolation="worktree")
    payload = _payload()
    report = t3.load_capabilities(_write(tmp_path, payload))
    context, observations = t3.t3_route_readiness(loaded, request, workspace=workspace, report=report)
    first = readiness.operation_scope(loaded, request, workspace, t3_context=context)
    assert all(item.scope == first for items in observations.values() for item in items)
    reloaded = t3.load_capabilities(_write(tmp_path, payload, "again.json"))
    reloaded_context, _ = t3.t3_route_readiness(loaded, request, workspace=workspace, report=reloaded)
    assert readiness.operation_scope(loaded, request, workspace, t3_context=reloaded_context) == first
    changed = _roster(workspace, ROSTER.replace('model = "launch-model"', 'model = "other-model"'))
    changed_context, _ = t3.t3_route_readiness(changed, request, workspace=workspace, report=report)
    assert readiness.operation_scope(changed, request, workspace, t3_context=changed_context) != first


def test_no_raw_input_value_or_path_reaches_output(workspace, tmp_path, capsys):
    sentinel = "NEVER-ECHO-VALUE"
    payload = _payload()
    payload["capabilities"]["providers"][0]["displayName"] = sentinel
    payload["capabilities"]["providers"][0]["models"][0]["label"] = sentinel
    payload["capabilities"]["providers"][0]["constraints"] = [sentinel]
    caps_path = _write(tmp_path, payload)
    report = _inspect(workspace, tmp_path, caps_path=caps_path)
    assert report["status"] == "blocked"
    rendered = json.dumps(report, sort_keys=True)
    assert sentinel not in rendered
    assert str(caps_path) not in rendered
    from brigade import roster_cmd

    roster_cmd.ready(
        workspace,
        workers=("coder",),
        route="t3",
        isolation="worktree",
        t3_capabilities=caps_path,
    )
    human = capsys.readouterr().out
    assert sentinel not in human
    assert str(caps_path) not in human


def test_inspection_writes_nothing_and_takes_no_lock(workspace, tmp_path, monkeypatch):
    # Refresh Git's stat cache before the snapshot so status cannot look like a write.
    _git(workspace, "status", "--porcelain")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("BRIGADE_HOME", str(home))

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("readiness must not take a capacity or run lock")

    def _no_network(*_args, **_kwargs):
        raise AssertionError("unenrolled readiness must not call the Hub")

    monkeypatch.setattr(native_t3_capacity, "capacity_lock", _forbidden)
    monkeypatch.setattr("brigade.runguard._wait_to_acquire_lock", _forbidden)
    monkeypatch.setattr("brigade.fleet_client._hub_open", _no_network)
    before = {str(path.relative_to(workspace)) for path in workspace.rglob("*")}
    report = _inspect(workspace, tmp_path, payload=_payload())
    assert report["status"] == "ready"
    assert {str(path.relative_to(workspace)) for path in workspace.rglob("*")} == before
    # The documented spool read creates only its private directory and lock.
    from brigade import fleet_model_admission

    home_after = {str(path.relative_to(home)) for path in home.rglob("*")}
    if fleet_model_admission.nofollow_supported():
        assert home_after == {"fleet-spool", "fleet-spool/model-roster.high-water.lock"}
        assert (home / "fleet-spool" / "model-roster.high-water.lock").read_bytes() == b""
    else:
        assert home_after == set()


def test_read_hub_authority_is_called_once_per_inspection(workspace, tmp_path, monkeypatch):
    calls: list[int] = []

    def _authority(_workspace):
        calls.append(1)
        return native_t3_admission.HubAuthority(state="unenrolled")

    monkeypatch.setattr(native_t3_admission, "read_hub_authority", _authority)
    report = _inspect(workspace, tmp_path, payload=_payload())
    assert report["status"] == "ready"
    assert len(calls) == 1


def test_spool_lock_timeout_is_admission_unknown(workspace, tmp_path, monkeypatch):
    from brigade import fleet_client, fleet_model_admission

    def _timeout(**_kwargs):
        raise fleet_client.FleetSpoolLockTimeout("fleet spool lock timed out")

    monkeypatch.setattr(fleet_model_admission, "enrollment_activation_observed", _timeout)
    monkeypatch.setattr(
        "brigade.fleet_client._hub_open",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("timeout must not reach the Hub")),
    )
    report = _inspect(workspace, tmp_path, payload=_payload())
    assert report["status"] == "unverified"
    assert _row(report, "admission")["status"] == "unknown"


def test_over_limit_occupancy_clamps_to_zero_and_blocks(workspace, tmp_path, monkeypatch):
    monkeypatch.setattr(
        native_t3_capacity,
        "governed_occupancy",
        lambda _workspace: native_t3_capacity.Occupancy(6, (("coder", 6),), True),
    )
    report = _inspect(workspace, tmp_path, payload=_payload())
    assert report["status"] == "blocked"
    assert report["admitted_capacity"] == 0
    assert _row(report, "capacity")["reason"] == "insufficient-capacity"


def test_huge_integer_is_malformed_without_a_traceback(workspace, tmp_path, capsys):
    from brigade import cli

    payload = _payload()
    payload["capabilities"]["features"]["maxBatchThreads"] = 10**400
    caps_path = _write(tmp_path, payload)
    code = cli.main(
        [
            "roster",
            "ready",
            "--target",
            str(workspace),
            "--worker",
            "coder",
            "--route",
            "t3",
            "--isolation",
            "worktree",
            "--t3-capabilities",
            str(caps_path),
            "--json",
        ]
    )
    captured = capsys.readouterr()
    assert code == 2
    assert "Traceback" not in captured.out + captured.err
    report = json.loads(captured.out)
    assert report["limiting_reason"] == "t3-capabilities-malformed"


def test_scope_binds_inherited_provider_and_features(workspace, tmp_path):
    loaded = _roster(workspace)
    request = readiness.ReadinessRequest(("coder",), route="t3", isolation="worktree")
    base = _payload()
    scopes = {}
    for name, mutate in {
        "base": lambda payload: None,
        "inherited-provider": lambda payload: payload["capabilities"].update(
            inheritedProviderInstanceId="other-provider"
        ),
        "app-owned-subagents": lambda payload: payload["capabilities"]["features"].update(appOwnedSubagents=False),
        "cancellation": lambda payload: payload["capabilities"]["features"].update(cancellation=False),
        "runtime-mode": lambda payload: payload["capabilities"].update(runtimeMode="full-access"),
        "interaction-mode": lambda payload: payload["capabilities"].update(interactionMode="plan"),
    }.items():
        payload = copy.deepcopy(base)
        mutate(payload)
        report = t3.load_capabilities(_write(tmp_path, payload, f"{name}.json"))
        context, _ = t3.t3_route_readiness(loaded, request, workspace=workspace, report=report)
        scopes[name] = readiness.operation_scope(loaded, request, workspace, t3_context=context)
    assert len(set(scopes.values())) == len(scopes)
    assert scopes["base"] != scopes["inherited-provider"]


def test_hard_blocker_wins_over_unobserved_model(workspace, tmp_path):
    payload = _payload()
    provider = payload["capabilities"]["providers"][0]
    provider["models"] = []
    provider["omittedModels"] = 4
    payload["capabilities"]["features"]["cancellation"] = False
    report = _inspect(workspace, tmp_path, payload=payload)
    assert report["status"] == "blocked"
    assert _row(report, "route-support")["reason"] == "cancellation-disabled"


def test_malformed_envelope_survives_missing_seat_hub_discovery(workspace, tmp_path):
    payload = _payload()
    payload["schema"] = "wrong"
    report = _inspect(
        workspace,
        tmp_path,
        payload=payload,
        workers=("coder", "ghost"),
        hub_discovery_possible=True,
    )
    rows = [item for item in report["evidence"] if item["category"] == "route-support"]
    assert rows
    assert {item["reason"] for item in rows} == {"t3-capabilities-malformed"}


def test_empty_observation_sequence_is_malformed(workspace, tmp_path):
    loaded = _roster(workspace)
    request = readiness.ReadinessRequest(("coder",), route="t3", isolation="worktree")
    parsed = t3.load_capabilities(_write(tmp_path, _payload()))
    context, observations = t3.t3_route_readiness(loaded, request, workspace=workspace, report=parsed)
    observations = dict(observations)
    observations["admission"] = ()
    result = readiness.evaluate_readiness(
        loaded, request, workspace=workspace, observations=observations, t3_context=context
    )
    assert result["status"] == "unverified"
    row = next(item for item in result["evidence"] if item["category"] == "admission")
    assert (row["status"], row["reason"]) == ("unknown", "observation-malformed")


def test_route_t3_ignores_the_run_preference_orchestrator_overlay(workspace, tmp_path, capsys):
    from brigade import cli, run_preference

    (workspace / ".brigade" / "roster.toml").write_text(
        ROSTER.replace('orchestrator = "lead"', 'orchestrator = "coder"'), encoding="utf-8"
    )
    run_preference.write_cached(run_preference.RunPreference(orchestrator="lead"))
    caps_path = _write(tmp_path, _payload())
    code = cli.main(
        [
            "roster",
            "ready",
            "--target",
            str(workspace),
            "--worker",
            "coder",
            "--route",
            "t3",
            "--isolation",
            "worktree",
            "--t3-capabilities",
            str(caps_path),
            "--json",
        ]
    )
    report = json.loads(capsys.readouterr().out)
    assert code == 2
    assert report["status"] == "blocked"
    assert any(
        item["category"] == "selection" and item["reason"] == "orchestrator-selected" for item in report["evidence"]
    )


def test_cli_refuses_capabilities_outside_route_t3(workspace, tmp_path, capsys):
    from brigade import cli

    caps_path = _write(tmp_path, _payload())
    code = cli.main(
        [
            "roster",
            "ready",
            "--target",
            str(workspace),
            "--worker",
            "coder",
            "--route",
            "brigade",
            "--t3-capabilities",
            str(caps_path),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload["limiting_reason"] == "t3-capabilities-route-mismatch"


def test_cli_ready_human_line_and_json_qualifiers(workspace, tmp_path, capsys):
    from brigade import cli

    caps_path = _write(tmp_path, _payload())
    code = cli.main(
        [
            "roster",
            "ready",
            "--target",
            str(workspace),
            "--worker",
            "coder",
            "--route",
            "t3",
            "--isolation",
            "worktree",
            "--t3-capabilities",
            str(caps_path),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["status"] == "ready"
    assert payload["assurance"] == "parent-reported"
    assert payload["launch_guaranteed"] is False
    assert payload["capacity_reserved"] is False
    assert payload["parent_active"] == "unverified"

    code = cli.main(
        [
            "roster",
            "ready",
            "--target",
            str(workspace),
            "--worker",
            "coder",
            "--route",
            "t3",
            "--isolation",
            "worktree",
            "--t3-capabilities",
            str(caps_path),
        ]
    )
    assert code == 0
    assert capsys.readouterr().out.splitlines()[0] == "ready (parent-reported route support; non-reserving snapshot)"
