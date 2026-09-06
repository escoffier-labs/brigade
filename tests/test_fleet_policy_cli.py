"""CLI transport for `brigade fleet policy` (slice 2)."""

from __future__ import annotations

import json
import subprocess
from io import StringIO

from brigade import cli, fleet_client, fleet_policy
from brigade.fleet_client_policy import FleetPolicyClientError


def _run(monkeypatch, argv: list[str]) -> tuple[int, str, str]:
    out = StringIO()
    err = StringIO()
    monkeypatch.setattr("sys.stdout", out)
    monkeypatch.setattr("sys.stderr", err)
    try:
        rc = cli.main(["fleet", "policy", *argv])
    except SystemExit as exc:
        rc = int(exc.code or 0)
    return rc, out.getvalue(), err.getvalue()


def test_help_lists_required_subcommands_and_flags(monkeypatch):
    rc, out, err = _run(monkeypatch, ["--help"])
    assert rc == 0
    text = out + err
    for name in (
        "show",
        "history",
        "preview",
        "save",
        "rollback",
        "resolve",
        "route",
        "reservation",
        "observe",
        "quota",
        "inventory",
        "status",
    ):
        assert name in text


def test_resolve_json_and_structured_auth_error(monkeypatch):
    monkeypatch.setattr(
        "brigade.fleet_client_policy.resolve_policy",
        lambda **kwargs: {
            "schema": fleet_policy.POLICY_SCHEMA,
            "version": 2,
            "digest": "sha256:" + ("ab" * 32),
            "effective": {"data": {"allow_training": False}},
            "sources": {"data.allow_training": {"value": False, "layer": "fleet-defaults", "chain": []}},
            "ack_required": True,
        },
    )
    rc, out, err = _run(
        monkeypatch,
        [
            "resolve",
            "--consumer",
            "t3-code",
            "--repo",
            "repo/public",
            "--session-id",
            "session-1",
            "--origin",
            "local",
            "--json",
        ],
    )
    assert rc == 0
    assert err == ""
    payload = json.loads(out)
    assert payload["ack_required"] is True
    assert payload["schema"] == fleet_policy.POLICY_SCHEMA
    assert "token" not in json.dumps(payload)

    def _auth(**kwargs):
        raise FleetPolicyClientError("auth-failed", "hub returned HTTP 401")

    monkeypatch.setattr("brigade.fleet_client_policy.resolve_policy", _auth)
    rc, out, err = _run(
        monkeypatch,
        [
            "resolve",
            "--consumer",
            "t3-code",
            "--repo",
            "repo/public",
            "--session-id",
            "session-1",
            "--origin",
            "local",
            "--json",
        ],
    )
    assert rc != 0
    body = json.loads(out or err)
    assert body["error"]["code"] == "auth-failed"
    assert "token" not in json.dumps(body).lower()


def test_network_and_policy_unsupported_and_revision_conflict_are_distinct(monkeypatch, tmp_path):
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(json.dumps(fleet_policy.empty_document()), encoding="utf-8")
    cases = (
        ("network", FleetPolicyClientError("network", "fleet hub policy request failed: timed out")),
        ("policy-unsupported", FleetPolicyClientError("policy-unsupported", "hub returned HTTP 404")),
        ("revision-conflict", FleetPolicyClientError("revision-conflict", "expected 2, current 3")),
    )
    for code, exc in cases:

        def _raise(*_args: object, current: FleetPolicyClientError = exc, **_kwargs: object) -> None:
            raise current

        monkeypatch.setattr("brigade.fleet_client_policy.save_policy", _raise)
        rc, out, err = _run(
            monkeypatch,
            ["save", "--file", str(policy_file), "--expected-version", "2", "--reason", "x"],
        )
        assert rc != 0
        body = json.loads(out or err)
        assert body["error"]["code"] == code


def test_session_prepare_and_acknowledge_flags(monkeypatch):
    captured_prepare: dict[str, object] = {}
    context_hash = "sha256:" + ("ee" * 32)

    def _prepare(**kwargs: object) -> dict[str, object]:
        captured_prepare.update(kwargs)
        return {
            "schema": fleet_policy.POLICY_SCHEMA,
            "version": 4,
            "digest": "sha256:" + ("cd" * 32),
            "effective": {},
            "sources": {},
            "ack_required": True,
            "selected": {
                "seat": "seat-alpha",
                "provider": "provider-a",
                "model": "model-a-1",
                "reasoning": "high",
                "instance_id": "grok-native",
            },
            "instructions": "schema=brigade.fleet_policy.v1 version=4",
            "repo_identity": "repo/public",
            "context_hash": context_hash,
        }

    monkeypatch.setattr("brigade.fleet_client_policy.prepare_session", _prepare)
    rc, out, err = _run(
        monkeypatch,
        [
            "session",
            "prepare",
            "--consumer",
            "t3-code",
            "--repo",
            "repo/public",
            "--session-id",
            "session-1",
            "--origin",
            "local",
            "--provider",
            "provider-a",
            "--model",
            "model-a-1",
            "--instance-id",
            "grok-native",
            "--reasoning",
            "high",
            "--json",
        ],
    )
    assert rc == 0
    assert err == ""
    payload = json.loads(out)
    assert payload["selected"]["seat"] == "seat-alpha"
    assert payload["ack_required"] is True
    assert "instructions" in payload
    assert payload["context_hash"] == context_hash
    assert captured_prepare["model"] == "model-a-1"
    assert captured_prepare["instance_id"] == "grok-native"

    rc, help_out, help_err = _run(monkeypatch, ["session", "acknowledge", "--help"])
    assert rc == 0
    assert "--context-hash" in help_out + help_err

    captured_ack: dict[str, object] = {}

    def _ack(**kwargs: object) -> dict[str, object]:
        captured_ack.update(kwargs)
        return {
            "schema": "brigade.fleet_policy_session.v1",
            "session_id": "session-1",
            "consumer": "t3-code",
            "repo_identity": "repo/public",
            "version": 4,
            "digest": "sha256:" + ("cd" * 32),
            "state": "current",
            "loaded_at": "2026-09-05T00:00:00+00:00",
            "applied": True,
            "context_hash": kwargs.get("context_hash"),
        }

    monkeypatch.setattr("brigade.fleet_client_policy.acknowledge_session", _ack)
    rc, out, err = _run(
        monkeypatch,
        [
            "session",
            "acknowledge",
            "--session-id",
            "session-1",
            "--consumer",
            "t3-code",
            "--repo",
            "repo/public",
            "--version",
            "4",
            "--digest",
            "sha256:" + ("cd" * 32),
            "--context-hash",
            context_hash,
            "--json",
        ],
    )
    assert rc == 0
    payload = json.loads(out)
    assert payload["state"] == "current"
    assert payload["applied"] is True
    assert payload["schema"] == "brigade.fleet_policy_session.v1"
    assert payload["context_hash"] == context_hash
    assert captured_ack["context_hash"] == context_hash
    assert captured_ack["session_id"] == "session-1"
    assert captured_ack["version"] == 4


def test_resolve_canonicalizes_existing_checkout_and_rejects_git_urls(monkeypatch, tmp_path):
    captured: dict[str, object] = {}

    def _resolve(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {
            "schema": fleet_policy.POLICY_SCHEMA,
            "version": 2,
            "digest": "sha256:" + ("ab" * 32),
            "effective": {},
            "sources": {},
            "ack_required": True,
        }

    monkeypatch.setattr("brigade.fleet_client_policy.resolve_policy", _resolve)
    repo = tmp_path / "checkout"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/example/brigade.git"],
        cwd=repo,
        check=True,
    )
    rc, out, err = _run(
        monkeypatch,
        [
            "resolve",
            "--consumer",
            "t3-code",
            "--repo",
            str(repo),
            "--session-id",
            "session-1",
            "--origin",
            "local",
            "--json",
        ],
    )
    assert rc == 0, err
    assert captured["repo"] == "github.com/example/brigade"
    assert "token" not in str(captured["repo"]).lower()

    rc, out, err = _run(
        monkeypatch,
        [
            "resolve",
            "--consumer",
            "t3-code",
            "--repo",
            "https://user:super-secret-token@github.com/example/brigade.git",
            "--session-id",
            "session-1",
            "--origin",
            "local",
            "--json",
        ],
    )
    assert rc != 0
    body = json.loads(out or err)
    assert body["error"]["code"] == "invalid-request"
    assert "super-secret-token" not in json.dumps(body)


def test_save_rejects_malformed_nonfinite_and_oversized_json(monkeypatch, tmp_path):
    monkeypatch.setattr("brigade.fleet_client_policy.save_policy", lambda **kwargs: {"revision": 2})
    inf = tmp_path / "inf.json"
    inf.write_text('{"schema":"brigade.fleet_policy.v1","value":Infinity}', encoding="utf-8")
    rc, out, err = _run(
        monkeypatch,
        ["save", "--file", str(inf), "--expected-version", "1", "--reason", "x"],
    )
    assert rc != 0
    assert json.loads(out or err)["error"]["code"] == "invalid-request"

    listed = tmp_path / "list.json"
    listed.write_text("[]", encoding="utf-8")
    rc, out, err = _run(
        monkeypatch,
        ["save", "--file", str(listed), "--expected-version", "1", "--reason", "x"],
    )
    assert rc != 0

    controls = tmp_path / "controls.json"
    controls.write_text('{"schema":"brigade.fleet_policy.v1","notes":"bad\\u0007"}', encoding="utf-8")
    rc, out, err = _run(
        monkeypatch,
        ["save", "--file", str(controls), "--expected-version", "1", "--reason", "x"],
    )
    assert rc != 0

    huge = tmp_path / "huge.json"
    huge.write_bytes(b"{" + (b"a" * (fleet_policy.MAX_DOCUMENT_BYTES + 1)) + b"}")
    rc, out, err = _run(
        monkeypatch,
        ["save", "--file", str(huge), "--expected-version", "1", "--reason", "x"],
    )
    assert rc != 0


def test_cli_never_prints_configured_tokens(monkeypatch):
    monkeypatch.setenv("BRIGADE_FLEET_TOKEN", "super-secret-admin-token")
    monkeypatch.setenv("BRIGADE_FLEET_NODE_TOKEN", "super-secret-node-token")

    def _boom(**kwargs):
        raise fleet_client.FleetClientError("fleet hub policy request failed: HTTP 401")

    monkeypatch.setattr("brigade.fleet_client_policy.show_policy", _boom)
    rc, out, err = _run(monkeypatch, ["show", "--json"])
    assert rc != 0
    combined = out + err
    assert "super-secret-admin-token" not in combined
    assert "super-secret-node-token" not in combined


def test_route_help_and_nonzero_denial(monkeypatch):
    rc, out, err = _run(monkeypatch, ["route", "--help"])
    assert rc == 0
    text = out + err
    assert "--workload" in text
    assert "--decision-id" in text

    monkeypatch.setattr(
        "brigade.fleet_client_policy.route_work",
        lambda **kwargs: {
            "schema": "brigade.fleet_route.v1",
            "decision_id": "dec-1",
            "policy_version": 2,
            "policy_digest": "sha256:" + ("ab" * 32),
            "selected": None,
            "reason": "no-eligible-candidate",
            "candidates": [{"machine": "worker-linux-1", "seat": "seat-alpha", "eligible": False, "reason": "stale"}],
            "reservation_id": None,
            "expires_at": None,
        },
    )
    rc, out, err = _run(
        monkeypatch,
        [
            "route",
            "--consumer",
            "t3-code",
            "--repo",
            "repo/public",
            "--session-id",
            "session-1",
            "--origin",
            "local",
            "--workload",
            "general",
            "--json",
        ],
    )
    assert rc != 0
    payload = json.loads(out)
    assert payload["selected"] is None
    assert set(payload["candidates"][0]) == {"machine", "seat", "eligible", "reason"}


def test_observe_defaults_omitted_node_id_and_preserves_explicit(monkeypatch, tmp_path):
    local_node = "11111111-1111-4111-8111-111111111111"
    spoofed = "22222222-2222-4222-8222-222222222222"
    captured: dict[str, object] = {}

    monkeypatch.setattr("brigade.fleet_client.resolve_node_id", lambda base_path=None: local_node)

    def _observe(body: dict) -> dict:
        captured.clear()
        captured.update(body)
        return {"schema": "brigade.fleet_machine_observation.v1", "accepted": True}

    monkeypatch.setattr("brigade.fleet_client_policy.observe_telemetry", _observe)

    omitted = tmp_path / "observe-omitted.json"
    omitted.write_text(
        json.dumps(
            {
                "observed_at": "2026-09-05T12:00:00+00:00",
                "ttl_seconds": 300,
                "status": "available",
                "load": 0.0,
                "running": {"session_ids": [], "run_ids": []},
                "active_claims": [],
                "usable_seats": ["seat-alpha"],
                "credential_state": "ok",
            }
        ),
        encoding="utf-8",
    )
    rc, out, err = _run(monkeypatch, ["observe", "--file", str(omitted), "--json"])
    assert rc == 0, err
    assert captured["node_id"] == local_node
    assert json.loads(out)["accepted"] is True

    explicit = tmp_path / "observe-explicit.json"
    explicit.write_text(
        json.dumps(
            {
                "node_id": spoofed,
                "observed_at": "2026-09-05T12:00:00+00:00",
                "ttl_seconds": 300,
                "status": "available",
                "credential_state": "ok",
            }
        ),
        encoding="utf-8",
    )
    rc, out, err = _run(monkeypatch, ["observe", "--file", str(explicit), "--json"])
    assert rc == 0, err
    assert captured["node_id"] == spoofed

    monkeypatch.setattr("brigade.fleet_client.resolve_node_id", lambda base_path=None: "unknown")
    rc, out, err = _run(monkeypatch, ["observe", "--file", str(omitted), "--json"])
    assert rc != 0
    body = json.loads(out or err)
    assert body["error"]["code"] == "invalid-request"
    assert "not enrolled" in body["error"]["message"]


def test_inventory_cli_ingest_status_and_probe_are_mocked(monkeypatch, tmp_path):
    captured: dict[str, object] = {}

    def _ingest(body, *, admin=False):
        captured["body"] = dict(body)
        captured["admin"] = admin
        return {"observation_id": "obs-1", "status": "ok", "projection_action": "created"}

    monkeypatch.setattr("brigade.fleet_client_policy.ingest_inventory", _ingest)
    monkeypatch.setattr(
        "brigade.fleet_client_policy.status_inventory",
        lambda: {"schema": "brigade.fleet_model_inventory.snapshot.v1", "providers": {}},
    )

    def _boom(*_args, **_kwargs):
        raise AssertionError("inventory probe must not spawn a process")

    monkeypatch.setattr("brigade.proc.run", _boom)
    monkeypatch.setattr(
        "brigade.fleet_model_inventory.probe_cli_inventory",
        lambda harness, provider=None, **kwargs: {
            "schema": "brigade.fleet_model_inventory.v1",
            "source": "cli:agy",
            "provider": provider or "google",
            "harness": harness,
            "account_id": "",
            "status": "ok",
            "models": [{"model_id": "model-a-1"}],
        },
    )

    ingest_path = tmp_path / "inventory.json"
    ingest_path.write_text(
        json.dumps(
            {
                "schema": "brigade.fleet_model_inventory.v1",
                "source": "cli:agy",
                "provider": "google",
                "harness": "agy",
                "account_id": "acct-generic",
                "scope": "complete",
                "status": "ok",
                "captured_at": "2026-09-05T12:00:00Z",
                "expires_at": "2026-09-05T14:00:00Z",
                "models": ["model-a-1"],
            }
        ),
        encoding="utf-8",
    )
    rc, out, err = _run(monkeypatch, ["inventory", "ingest", "--file", str(ingest_path), "--json"])
    assert rc == 0, err
    assert captured["admin"] is False
    assert json.loads(out)["observation_id"] == "obs-1"

    rc, out, err = _run(monkeypatch, ["inventory", "status", "--json"])
    assert rc == 0, err
    assert json.loads(out)["schema"] == "brigade.fleet_model_inventory.snapshot.v1"

    output = tmp_path / "probe.json"
    rc, out, err = _run(
        monkeypatch,
        [
            "inventory",
            "probe",
            "--harness",
            "agy",
            "--provider",
            "google",
            "--account-id",
            "acct-generic",
            "--output",
            str(output),
        ],
    )
    assert rc == 0, err
    probe = json.loads(output.read_text(encoding="utf-8"))
    assert probe["harness"] == "agy"
    assert probe["account_id"] == "acct-generic"
    assert "publish" not in captured or captured.get("published") is None

    captured.clear()
    rc, out, err = _run(
        monkeypatch,
        [
            "inventory",
            "probe",
            "--harness",
            "agy",
            "--provider",
            "google",
            "--account-id",
            "acct-generic",
            "--publish",
        ],
    )
    assert rc == 0, err
    assert captured["body"]["account_id"] == "acct-generic"
    assert json.loads(out)["status"] == "ok"


def test_client_error_codes_are_bounded_and_do_not_echo_server_secrets():
    from brigade.fleet_client_policy import _classify

    err = _classify(
        500,
        {"error": {"code": "sqlite-traceback", "message": "token=super-secret-db"}},
        "hub returned HTTP 500",
    )
    assert err.code in {
        "auth-failed",
        "policy-unsupported",
        "revision-conflict",
        "network",
        "invalid-request",
        "enrollment-required",
        "seat-unresolved",
        "seat-disabled",
        "training-disallowed",
        "ambiguous-seat",
        "retired",
        "policy-blocked",
    }
    assert err.code != "sqlite-traceback"
    assert "super-secret-db" not in err.message
    assert "token=" not in err.message.lower()
