"""Enrolled Fleet session prepare/apply/ack and the native launcher."""

from __future__ import annotations

import json
import subprocess
import threading

import pytest

from brigade import agents, fleet_client, fleet_model_admission, fleet_policy, fleet_session_bootstrap
from brigade.aboyeur import planning
from brigade.fleet_client_policy import FleetPolicyClientError
from brigade.roster import Agent


DIGEST = "sha256:" + ("ab" * 32)
CONTEXT_HASH = "sha256:" + ("cd" * 32)
INSTRUCTIONS = "schema=brigade.fleet_policy.v1 version=4 digest=" + DIGEST
REPO = "github.com/example/repository"


def _prepared(**fields):
    body = {
        "schema": "brigade.fleet_policy.v1",
        "version": 4,
        "digest": DIGEST,
        "effective": {},
        "sources": {"data.allow_training": {"value": False, "layer": "fleet-defaults"}},
        "ack_required": True,
        "selected": {
            "seat": "seat-alpha",
            "provider": "openai",
            "model": "model-hyphen-slug",
            "reasoning": "high",
            "instance_id": "codex",
        },
        "instructions": INSTRUCTIONS,
        "repo_identity": REPO,
        "context_hash": CONTEXT_HASH,
    }
    body.update(fields)
    return body


def _ack_body(session_id: str, **fields):
    body = {
        "schema": fleet_policy.POLICY_SESSION_SCHEMA,
        "session_id": session_id,
        "consumer": "brigade-run",
        "repo_identity": REPO,
        "version": 4,
        "digest": DIGEST,
        "state": "current",
        "applied": True,
        "context_hash": CONTEXT_HASH,
        "loaded_at": "2026-09-05T00:00:00Z",
    }
    body.update(fields)
    return body


def _patch_launch_gate(monkeypatch):
    monkeypatch.setattr(
        fleet_model_admission,
        "admit_model",
        lambda **kwargs: fleet_model_admission.ModelAdmissionDecision(True, 0, "admitted", {"state": "authoritative"}),
    )
    monkeypatch.setattr(
        fleet_client,
        "acquire_model_lease",
        lambda *args, **kwargs: fleet_client.ModelLeaseDecision(True, "ok", "lease-1", "holder-1"),
    )


def _init_repo(path, identity: str = REPO):
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "remote", "add", "origin", f"https://{identity}.git"], check=True)
    fleet_session_bootstrap.fleet_session_presence.clear_repository_identity_cache()


def _enrolled_snapshot():
    return {
        "state": "authoritative",
        "schema": "brigade.fleet_model_roster.v1",
        "fleet_policy": {"active": True, "version": 4, "digest": DIGEST},
        "models": [],
        "seats": [],
    }


def test_unenrolled_legacy_does_not_call_network(monkeypatch):
    calls = []
    monkeypatch.setattr(
        fleet_session_bootstrap.fleet_client_cloud,
        "load_model_policy_snapshot",
        lambda: {"state": "unconfigured", "models": []},
    )
    monkeypatch.setattr(
        fleet_session_bootstrap.fleet_client_policy,
        "prepare_session",
        lambda **kwargs: calls.append(kwargs) or _prepared(),
    )
    prompt = fleet_session_bootstrap.ensure_prompt("do work", cli_ref="codex", model="model-a-1", cwd=None)
    assert prompt == "do work"
    assert calls == []


@pytest.mark.parametrize(
    "snapshot",
    (
        {"state": "unavailable", "models": []},
        {"state": "auth-failed", "models": []},
        {"state": "malformed-policy", "models": [], "error": "malformed-authority"},
        {
            "state": "authoritative",
            "fleet_policy": {"active": "yes"},
            "models": [],
        },
    ),
)
def test_enrolled_unavailable_or_malformed_fails_before_provider(snapshot):
    assert fleet_session_bootstrap.classify_enrollment(snapshot) == "denied"


def test_active_session_prepares_source_and_version(tmp_path, monkeypatch):
    prepares = []
    acks = []
    monkeypatch.setattr(
        fleet_session_bootstrap.fleet_client_policy,
        "prepare_session",
        lambda **kwargs: (
            prepares.append(kwargs)
            or _prepared(selected={**_prepared()["selected"], "launch_model": "openai/model-slash-id"})
        ),
    )
    monkeypatch.setattr(
        fleet_session_bootstrap.fleet_client_policy,
        "acknowledge_session",
        lambda **kwargs: acks.append(kwargs) or _ack_body("session-1"),
    )
    ctx = fleet_session_bootstrap.prepare_session_launch(
        consumer="brigade-run",
        provider="openai",
        model="openai/model-slash-id",
        instance_id="codex",
        repo=REPO,
        origin="local",
        session_id="session-1",
        cwd=tmp_path,
        snapshot=_enrolled_snapshot(),
    )
    assert ctx.version == 4
    assert ctx.digest == DIGEST
    assert ctx.sources["data.allow_training"]["layer"] == "fleet-defaults"
    assert ctx.status == "loaded"
    assert ctx.started is False
    assert ctx.receipt_path is not None and ctx.receipt_path.is_file()
    receipt = json.loads(ctx.receipt_path.read_text())
    assert receipt["status"] == "loaded"
    assert receipt["started"] is False
    assert "token" not in json.dumps(receipt)
    applied = fleet_session_bootstrap.apply_instructions("do work", ctx)
    assert applied.startswith("do work")
    assert INSTRUCTIONS in applied
    fleet_session_bootstrap.acknowledge_launch(ctx)
    assert ctx.applied is True
    assert ctx.status == "applied"
    assert prepares and prepares[0]["instance_id"] == "codex"
    assert acks and acks[0]["version"] == 4 and acks[0]["digest"] == DIGEST


def test_wrong_identity_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(
        fleet_session_bootstrap.fleet_client_policy,
        "prepare_session",
        lambda **kwargs: _prepared(selected={**_prepared()["selected"], "instance_id": "other-cli"}),
    )
    with pytest.raises(fleet_session_bootstrap.PreflightDenial, match="instance"):
        fleet_session_bootstrap.prepare_session_launch(
            consumer="brigade-run",
            provider="openai",
            model="model-hyphen-slug",
            instance_id="codex",
            repo=REPO,
            origin="local",
            session_id="session-bad",
            cwd=tmp_path,
            snapshot=_enrolled_snapshot(),
        )


def test_ack_drift_does_not_run(tmp_path, monkeypatch):
    monkeypatch.setattr(
        fleet_session_bootstrap.fleet_client_policy,
        "prepare_session",
        lambda **kwargs: _prepared(),
    )
    monkeypatch.setattr(
        fleet_session_bootstrap.fleet_client_policy,
        "acknowledge_session",
        lambda **kwargs: (_ for _ in ()).throw(FleetPolicyClientError("revision-conflict", "stale revision")),
    )
    ctx = fleet_session_bootstrap.prepare_session_launch(
        consumer="brigade-run",
        provider="openai",
        model="model-hyphen-slug",
        instance_id="codex",
        repo=REPO,
        origin="local",
        session_id="session-stale",
        cwd=tmp_path,
        snapshot=_enrolled_snapshot(),
    )
    with pytest.raises(fleet_session_bootstrap.PreflightDenial, match="stale revision"):
        fleet_session_bootstrap.acknowledge_launch(ctx)
    assert ctx.started is False
    assert ctx.status == "failed"
    assert json.loads(ctx.receipt_path.read_text())["started"] is False


def test_append_happens_before_ack(tmp_path, monkeypatch):
    order = []
    monkeypatch.setattr(
        fleet_session_bootstrap.fleet_client_policy,
        "prepare_session",
        lambda **kwargs: _prepared(),
    )

    def _ack(**kwargs):
        order.append("ack")
        return _ack_body("session-order")

    monkeypatch.setattr(fleet_session_bootstrap.fleet_client_policy, "acknowledge_session", _ack)
    _patch_launch_gate(monkeypatch)
    ctx = fleet_session_bootstrap.prepare_session_launch(
        consumer="brigade-run",
        provider="openai",
        model="model-hyphen-slug",
        instance_id="codex",
        repo=REPO,
        origin="local",
        session_id="session-order",
        cwd=tmp_path,
        snapshot=_enrolled_snapshot(),
    )
    with fleet_session_bootstrap.scoped_context(ctx):
        order.append("apply")
        text = fleet_session_bootstrap.ensure_prompt("task", cli_ref="codex", model="model-hyphen-slug", cwd=tmp_path)
    assert INSTRUCTIONS in text
    assert order == ["apply", "ack"]


def test_concurrent_contexts_stay_isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(
        fleet_session_bootstrap.fleet_client_policy,
        "prepare_session",
        lambda **kwargs: _prepared(selected={**_prepared()["selected"], "instance_id": kwargs["instance_id"]}),
    )
    seen: dict[str, str] = {}

    def _worker(instance_id: str):
        ctx = fleet_session_bootstrap.prepare_session_launch(
            consumer="brigade-run",
            provider="openai",
            model="model-hyphen-slug",
            instance_id=instance_id,
            repo=REPO,
            origin="local",
            session_id=f"session-{instance_id}",
            cwd=tmp_path,
            snapshot=_enrolled_snapshot(),
        )
        with fleet_session_bootstrap.scoped_context(ctx):
            seen[instance_id] = fleet_session_bootstrap.current_context().instance_id

    threads = [threading.Thread(target=_worker, args=("codex",)), threading.Thread(target=_worker, args=("claude",))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert seen == {"codex": "codex", "claude": "claude"}


def test_fallback_attempts_get_independent_session_ids():
    first = fleet_session_bootstrap.session_id_for(run_key="run-a", seat="coder", attempt=1)
    second = fleet_session_bootstrap.session_id_for(run_key="run-a", seat="coder", attempt=2)
    other = fleet_session_bootstrap.session_id_for(run_key="run-a", seat="reviewer", attempt=1)
    assert first != second
    assert first != other
    assert first == fleet_session_bootstrap.session_id_for(run_key="run-a", seat="coder", attempt=1)


def test_run_agent_appends_and_acks_before_spawn(tmp_path, monkeypatch):
    order = []
    ctx = fleet_session_bootstrap.LaunchContext(
        session_id="session-run",
        consumer="brigade-run",
        provider="openai",
        model="model-hyphen-slug",
        instance_id="codex",
        version=4,
        digest=DIGEST,
        instructions=INSTRUCTIONS,
        sources={},
        loaded_at="2026-09-05T00:00:00Z",
        receipt_path=tmp_path / "receipt.json",
        selected={"seat": "seat-alpha", "provider": "openai", "model": "model-hyphen-slug", "instance_id": "codex"},
        repo_identity=REPO,
        context_hash=CONTEXT_HASH,
    )
    monkeypatch.setattr(agents.proc, "which", lambda c: "/x/" + c)

    def _run(argv, **kwargs):
        order.append(("run", argv, kwargs.get("stdin")))
        return agents.proc.Result(0, "answer", "")

    monkeypatch.setattr(agents.proc, "run", _run)
    monkeypatch.setattr(
        fleet_session_bootstrap.fleet_client_policy,
        "acknowledge_session",
        lambda **kwargs: order.append(("ack", kwargs["digest"])) or _ack_body("session-run"),
    )
    _patch_launch_gate(monkeypatch)
    with fleet_session_bootstrap.scoped_context(ctx):
        result = agents.run_agent("codex", "do it", model="model-hyphen-slug", cwd=tmp_path)
    assert result.ok is True
    assert order[0][0] == "ack"
    assert order[1][0] == "run"
    stdin = order[1][2]
    assert stdin is not None and INSTRUCTIONS.encode() in stdin


def test_ack_failure_skips_run_agent_spawn(tmp_path, monkeypatch):
    ctx = fleet_session_bootstrap.LaunchContext(
        session_id="session-deny",
        consumer="brigade-run",
        provider="openai",
        model="model-hyphen-slug",
        instance_id="codex",
        version=4,
        digest=DIGEST,
        instructions=INSTRUCTIONS,
        sources={},
        loaded_at="2026-09-05T00:00:00Z",
        receipt_path=tmp_path / "receipt.json",
        selected={"seat": "seat-alpha", "provider": "openai", "model": "model-hyphen-slug", "instance_id": "codex"},
        repo_identity=REPO,
        context_hash=CONTEXT_HASH,
    )
    spawned = []
    monkeypatch.setattr(agents.proc, "which", lambda c: "/x/" + c)
    monkeypatch.setattr(
        agents.proc, "run", lambda *args, **kwargs: spawned.append(True) or agents.proc.Result(0, "x", "")
    )
    monkeypatch.setattr(
        fleet_session_bootstrap.fleet_client_policy,
        "acknowledge_session",
        lambda **kwargs: (_ for _ in ()).throw(FleetPolicyClientError("revision-conflict", "stale")),
    )
    with fleet_session_bootstrap.scoped_context(ctx):
        result = agents.run_agent("codex", "do it", model="model-hyphen-slug", cwd=tmp_path)
    assert result.ok is False
    assert result.failure_phase == "preflight"
    assert spawned == []


def test_codex_appserver_acks_before_run_turn(tmp_path, monkeypatch):
    order = []
    ctx = fleet_session_bootstrap.LaunchContext(
        session_id="session-app",
        consumer="brigade-run",
        provider="openai",
        model="model-hyphen-slug",
        instance_id="codex",
        version=4,
        digest=DIGEST,
        instructions=INSTRUCTIONS,
        sources={},
        loaded_at="2026-09-05T00:00:00Z",
        receipt_path=tmp_path / "receipt.json",
        selected={"seat": "seat-alpha", "provider": "openai", "model": "model-hyphen-slug", "instance_id": "codex"},
        repo_identity=REPO,
        context_hash=CONTEXT_HASH,
    )

    class _Turn:
        text = "done"
        ok = True
        status = "completed"
        thread_id = "thread-1"
        detail = None
        timed_out = False
        output_limit_exceeded = False

    class _Thread:
        def run_turn(self, prompt, **kwargs):
            order.append(("run_turn", prompt))
            return _Turn()

    class _Server:
        def start_thread(self, **kwargs):
            order.append(("start_thread", kwargs.get("model")))
            return _Thread()

    monkeypatch.setattr(
        fleet_session_bootstrap.fleet_client_policy,
        "acknowledge_session",
        lambda **kwargs: order.append(("ack", kwargs["version"])) or _ack_body("session-app"),
    )
    _patch_launch_gate(monkeypatch)
    with fleet_session_bootstrap.scoped_context(ctx):
        result = planning._run_codex_appserver_worker(
            _Server(),
            Agent("coder", "codex", "code", model="model-hyphen-slug"),
            "coder",
            "do work",
            timeout=5.0,
            cwd=tmp_path,
            read_only=False,
            sandbox=None,
            registry=None,
        )
    assert result.ok is True
    assert [item[0] for item in order] == ["ack", "start_thread", "run_turn"]
    assert INSTRUCTIONS in order[-1][1]


def test_launcher_argv_keeps_prompt_as_one_argument(tmp_path, monkeypatch):
    _init_repo(tmp_path)
    monkeypatch.setattr(
        fleet_session_bootstrap.fleet_client_policy,
        "prepare_session",
        lambda **kwargs: _prepared(),
    )
    monkeypatch.setattr(
        fleet_session_bootstrap.fleet_client_policy,
        "acknowledge_session",
        lambda **kwargs: _ack_body(kwargs["session_id"]),
    )
    _patch_launch_gate(monkeypatch)
    captured = {}

    class _Proc:
        def wait(self):
            return 0

    def _spawn(argv, **kwargs):
        captured["argv"] = list(argv)
        captured["cwd"] = kwargs.get("cwd")
        return _Proc()

    rc = fleet_session_bootstrap.launch_enrolled(
        harness="codex",
        consumer="brigade-run",
        provider="openai",
        model="model-hyphen-slug",
        instance_id="codex",
        repo=REPO,
        origin="local",
        prompt="fix the 'quoted' path",
        extra=["--", "--ask-for-approval", "on-request"],
        cwd=tmp_path,
        spawn=_spawn,
    )
    assert rc == 0
    argv = captured["argv"]
    assert argv[0] == "codex"
    assert "--model" in argv and argv[argv.index("--model") + 1] == "model-hyphen-slug"
    assert argv[-1].startswith("fix the 'quoted' path")
    assert INSTRUCTIONS in argv[-1]
    assert "--ask-for-approval" in argv
    assert "--sandbox" not in argv
    receipt = next((tmp_path / ".brigade" / "fleet-session").glob("*.json"))
    payload = json.loads(receipt.read_text())
    assert payload["started"] is True
    assert payload["applied"] is True


def test_launcher_spawn_failure_is_not_started(tmp_path, monkeypatch):
    _init_repo(tmp_path)
    monkeypatch.setattr(
        fleet_session_bootstrap.fleet_client_policy,
        "prepare_session",
        lambda **kwargs: _prepared(
            selected={**_prepared()["selected"], "provider": kwargs["provider"], "instance_id": kwargs["instance_id"]}
        ),
    )
    monkeypatch.setattr(
        fleet_session_bootstrap.fleet_client_policy,
        "acknowledge_session",
        lambda **kwargs: _ack_body(kwargs["session_id"]),
    )
    _patch_launch_gate(monkeypatch)

    def _spawn(argv, **kwargs):
        raise OSError("codex missing")

    with pytest.raises(fleet_session_bootstrap.PreflightDenial, match="failed to start"):
        fleet_session_bootstrap.launch_enrolled(
            harness="claude",
            consumer="brigade-run",
            provider="anthropic",
            model="model-hyphen-slug",
            instance_id="claude",
            repo=REPO,
            origin="local",
            cwd=tmp_path,
            spawn=_spawn,
        )
    receipt = next((tmp_path / ".brigade" / "fleet-session").glob("*.json"))
    payload = json.loads(receipt.read_text())
    assert payload["started"] is False
    assert payload["status"] == "failed"


def test_claude_launcher_uses_append_system_prompt_without_broadening_permissions(tmp_path, monkeypatch):
    _init_repo(tmp_path)
    monkeypatch.setattr(
        fleet_session_bootstrap.fleet_client_policy,
        "prepare_session",
        lambda **kwargs: _prepared(
            selected={**_prepared()["selected"], "provider": "anthropic", "instance_id": "claude"}
        ),
    )
    monkeypatch.setattr(
        fleet_session_bootstrap.fleet_client_policy,
        "acknowledge_session",
        lambda **kwargs: _ack_body(kwargs["session_id"]),
    )
    _patch_launch_gate(monkeypatch)
    captured = {}

    class _Proc:
        def wait(self):
            return 0

    def _spawn(argv, **kwargs):
        captured["argv"] = list(argv)
        return _Proc()

    rc = fleet_session_bootstrap.launch_enrolled(
        harness="claude",
        consumer="brigade-run",
        provider="anthropic",
        model="model-hyphen-slug",
        instance_id="claude",
        repo=REPO,
        origin="local",
        prompt="hello",
        cwd=tmp_path,
        spawn=_spawn,
    )
    assert rc == 0
    argv = captured["argv"]
    assert argv[0] == "claude"
    assert "--append-system-prompt" in argv
    assert "--dangerously-skip-permissions" not in argv
    assert "--permission-mode" not in argv


def test_unsupported_cloud_is_honest():
    with pytest.raises(fleet_session_bootstrap.PreflightDenial, match="do not support"):
        with fleet_session_bootstrap.scoped_context(
            fleet_session_bootstrap.LaunchContext(
                session_id="s",
                consumer="brigade-run",
                provider="openai",
                model="m",
                instance_id="codex-cloud:env",
                version=1,
                digest=DIGEST,
                instructions="x",
                sources={},
                loaded_at="t",
            )
        ):
            fleet_session_bootstrap.ensure_prompt("hi", cli_ref="codex-cloud:env", model="m", cwd=None)


def test_training_override_is_denied(tmp_path, monkeypatch):
    monkeypatch.setattr(
        fleet_session_bootstrap.fleet_client_policy,
        "prepare_session",
        lambda **kwargs: (_ for _ in ()).throw(
            FleetPolicyClientError("training-disallowed", "contributor training model is not permitted")
        ),
    )
    with pytest.raises(fleet_session_bootstrap.PreflightDenial, match="not permitted"):
        fleet_session_bootstrap.prepare_session_launch(
            consumer="brigade-run",
            provider="openai",
            model="model-contrib-train-1",
            instance_id="codex",
            repo="repo/private",
            origin="local",
            session_id="session-train",
            cwd=tmp_path,
            snapshot=_enrolled_snapshot(),
        )


def test_launcher_rejects_harness_provider_mismatch(tmp_path, monkeypatch):
    _init_repo(tmp_path)
    monkeypatch.setattr(
        fleet_session_bootstrap.fleet_client_policy,
        "prepare_session",
        lambda **kwargs: _prepared(),
    )
    with pytest.raises(fleet_session_bootstrap.PreflightDenial, match="harness"):
        fleet_session_bootstrap.launch_enrolled(
            harness="claude",
            consumer="brigade-run",
            provider="openai",
            model="model-hyphen-slug",
            instance_id="codex",
            repo=REPO,
            origin="local",
            cwd=tmp_path,
            spawn=lambda *args, **kwargs: None,
        )


def test_main_requires_exact_identity_flags():
    with pytest.raises(SystemExit) as excinfo:
        fleet_session_bootstrap.main(["--harness", "codex"])
    assert excinfo.value.code != 0


def test_launch_model_is_compared_when_present(tmp_path, monkeypatch):
    monkeypatch.setattr(
        fleet_session_bootstrap.fleet_client_policy,
        "prepare_session",
        lambda **kwargs: _prepared(selected={**_prepared()["selected"], "launch_model": "openai/model-slash-id"}),
    )
    ctx = fleet_session_bootstrap.prepare_session_launch(
        consumer="brigade-run",
        provider="openai",
        model="openai/model-slash-id",
        instance_id="codex",
        repo=REPO,
        origin="local",
        session_id="session-launch-model",
        cwd=tmp_path,
        snapshot=_enrolled_snapshot(),
    )
    assert ctx.selected["launch_model"] == "openai/model-slash-id"
    with pytest.raises(fleet_session_bootstrap.PreflightDenial, match="model"):
        fleet_session_bootstrap.prepare_session_launch(
            consumer="brigade-run",
            provider="openai",
            model="model-hyphen-slug",
            instance_id="codex",
            repo=REPO,
            origin="local",
            session_id="session-launch-mismatch",
            cwd=tmp_path,
            snapshot=_enrolled_snapshot(),
        )


def test_wrong_model_identity_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(
        fleet_session_bootstrap.fleet_client_policy,
        "prepare_session",
        lambda **kwargs: _prepared(),
    )
    with pytest.raises(fleet_session_bootstrap.PreflightDenial, match="model"):
        fleet_session_bootstrap.prepare_session_launch(
            consumer="brigade-run",
            provider="openai",
            model="other-model",
            instance_id="codex",
            repo=REPO,
            origin="local",
            session_id="session-model",
            cwd=tmp_path,
            snapshot=_enrolled_snapshot(),
        )


def test_ack_body_mismatch_is_not_applied(tmp_path, monkeypatch):
    monkeypatch.setattr(
        fleet_session_bootstrap.fleet_client_policy,
        "prepare_session",
        lambda **kwargs: _prepared(),
    )
    monkeypatch.setattr(
        fleet_session_bootstrap.fleet_client_policy,
        "acknowledge_session",
        lambda **kwargs: {"applied": True, "version": 4, "digest": DIGEST},
    )
    ctx = fleet_session_bootstrap.prepare_session_launch(
        consumer="brigade-run",
        provider="openai",
        model="model-hyphen-slug",
        instance_id="codex",
        repo=REPO,
        origin="local",
        session_id="session-ack-body",
        cwd=tmp_path,
        snapshot=_enrolled_snapshot(),
    )
    with pytest.raises(fleet_session_bootstrap.PreflightDenial, match="acknowledgement"):
        fleet_session_bootstrap.acknowledge_launch(ctx)
    assert ctx.applied is False
    assert ctx.status == "failed"


def test_launcher_rejects_conflicting_model_extra(tmp_path, monkeypatch):
    _init_repo(tmp_path)
    monkeypatch.setattr(
        fleet_session_bootstrap.fleet_client_policy,
        "prepare_session",
        lambda **kwargs: _prepared(),
    )
    with pytest.raises(fleet_session_bootstrap.PreflightDenial, match="conflicts"):
        fleet_session_bootstrap.launch_enrolled(
            harness="codex",
            consumer="brigade-run",
            provider="openai",
            model="model-hyphen-slug",
            instance_id="codex",
            repo=REPO,
            origin="local",
            extra=["--model=other"],
            cwd=tmp_path,
            spawn=lambda *args, **kwargs: None,
        )


def test_launcher_rejects_unverified_or_mismatched_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(
        fleet_session_bootstrap.fleet_client_policy,
        "prepare_session",
        lambda **kwargs: _prepared(),
    )
    with pytest.raises(fleet_session_bootstrap.PreflightDenial, match="unverifiable"):
        fleet_session_bootstrap.launch_enrolled(
            harness="codex",
            consumer="brigade-run",
            provider="openai",
            model="model-hyphen-slug",
            instance_id="codex",
            repo=REPO,
            origin="local",
            cwd=tmp_path,
            spawn=lambda *args, **kwargs: None,
        )
    _init_repo(tmp_path, identity="github.com/example/other")
    with pytest.raises(fleet_session_bootstrap.PreflightDenial, match="does not match"):
        fleet_session_bootstrap.launch_enrolled(
            harness="codex",
            consumer="brigade-run",
            provider="openai",
            model="model-hyphen-slug",
            instance_id="codex",
            repo=REPO,
            origin="local",
            cwd=tmp_path,
            spawn=lambda *args, **kwargs: None,
        )
