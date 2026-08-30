# content-guard: allow bearer-token file
"""First-party operations-relay connector pack: closed submit/status tools."""

from __future__ import annotations

import copy
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from brigade import cli, fleet_client, grokbot_findings, grokbot_ops, grokbot_packs
from brigade import grokbot_operations_relay as operations_relay

SECRET = "not-a-real-token-value-32chars!!"
SHORT_BEARER = "short-token"
SHARED_DISPATCH_BEARER = "shared-dispatch-bearer-32chars!!"
PRIVATE_TITLE = "PRIVATE_OPS_TITLE_TOKEN"
PRIVATE_BODY = (
    "Bearer ghp_exampleNotARealToken0000000000000001\n"
    "ssh root@192.0.2.10\n"
    "command: curl http://example.invalid/secret\n"
    "credential: hunter2-example\n"
    "path: /etc/shadow\n"
)
NOW = datetime(2026, 8, 30, 11, 0, tzinfo=timezone.utc)
PACK_ID = "operations-relay"
TOOLS = ("automation_finding_status", "submit_automation_finding")
FORBIDDEN_PUBLIC = {
    "title",
    "body",
    "address",
    "command",
    "credential",
    "payload",
    "token",
    "password",
    "secret",
    "producer",
    "finding_id",
    "source_ref",
    "source_digest",
    "events",
    "findings",
    "revision",
    "path",
    "url",
}
LEAK_TOKENS = (
    SECRET,
    PRIVATE_TITLE,
    "ghp_exampleNotARealToken0000000000000001",
    "192.0.2.10",
    "http://example.invalid/secret",
    "hunter2-example",
    "/etc/shadow",
)


def _reject(reason: str):
    return pytest.raises(grokbot_packs.PackError, match=reason)


def _owner(path: Path) -> Path:
    path.mkdir(mode=0o700)
    os.chmod(path, 0o700)
    return path


def _entry(
    *,
    producer: str = "n8n",
    finding_id: str = "workflow-stale",
    revision: str = "1",
    title: str = PRIVATE_TITLE,
    body: str = PRIVATE_BODY,
    source_ref: str = "n8n:workflow-stale",
    severity: str = "high",
) -> dict[str, str]:
    return {
        "producer": producer,
        "finding_id": finding_id,
        "revision": revision,
        "observed_at": "2026-08-30T11:00:00Z",
        "severity": severity,
        "title": title,
        "body": body,
        "source_ref": source_ref,
        "source_digest": grokbot_findings.content_digest("source", body),
        "content_digest": grokbot_findings.content_digest(title, body),
    }


def _handle(entry: dict[str, str]) -> str:
    return grokbot_findings.identity_digest(entry["producer"], entry["finding_id"], entry["revision"])


def _public_keys(value: object) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        keys.update(value)
        for item in value.values():
            keys.update(_public_keys(item))
    elif isinstance(value, list):
        for item in value:
            keys.update(_public_keys(item))
    return keys


def _assert_opaque(text: str) -> None:
    for token in LEAK_TOKENS:
        assert token not in text


def _assert_opaque_result(result: dict[str, object]) -> None:
    assert FORBIDDEN_PUBLIC.isdisjoint(_public_keys(result))
    _assert_opaque(json.dumps(result, sort_keys=True))


def _isolate_home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("BRIGADE_HOME", str(home))
    monkeypatch.delenv("BRIGADE_FLEET_HUB_URL", raising=False)
    monkeypatch.delenv("BRIGADE_FLEET_TOKEN", raising=False)
    return home


def _capture_events(monkeypatch) -> list[dict[str, object]]:
    captured: list[dict[str, object]] = []

    def _capture(event: dict[str, object], **_kwargs: object) -> bool:
        captured.append(copy.deepcopy(event))
        return True

    monkeypatch.setattr(fleet_client, "report_event", _capture)
    return captured


def _tools(tmp_path: Path, monkeypatch) -> tuple[operations_relay.OperationsRelayTools, Path, Path]:
    _isolate_home(tmp_path, monkeypatch)
    queue = tmp_path / "queue"
    owner = tmp_path / "owner"
    queue.mkdir()
    owner.mkdir()
    tools = operations_relay.OperationsRelayTools(target=queue, owner=owner, now=lambda: NOW)
    return tools, queue, owner


def test_operations_relay_pack_is_closed_on_8777_with_exact_inventory():
    pack = grokbot_packs.show_pack(PACK_ID)
    assert pack["kind"] == "connector"
    assert pack["instance"] == PACK_ID
    assert pack["default_bind"] == "127.0.0.1:8777"
    assert pack["public_route"] == ""
    assert pack["tools"] == list(TOOLS)
    assert set(operations_relay.TOOLS) == set(TOOLS)
    with _reject("unknown-pack"):
        grokbot_packs.show_pack("operations")
    binds = {pack["id"]: pack["default_bind"] for pack in grokbot_packs.list_packs()}
    assert binds[PACK_ID] == "127.0.0.1:8777"
    ports = {grokbot_packs._parse_default_bind(bind)[1] for bind in binds.values()}
    assert len(ports) == len(binds)


def test_setup_preview_apply_and_rejects_foreign_keys(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    owner = _owner(tmp_path / "owner")
    preview = grokbot_packs.preview_setup(
        tmp_path,
        PACK_ID,
        bearer_env="TEST_GROKBOT_BEARER",
        owner_workspace=owner,
    )
    assert preview["apply"] is False
    assert preview["bind"] == "127.0.0.1:8777"
    dumped = json.dumps(preview)
    assert str(owner) not in dumped
    assert SECRET not in dumped
    assert not grokbot_packs.instance_config_path(tmp_path, PACK_ID).exists()
    assert not grokbot_ops.config_path(tmp_path, PACK_ID).exists()

    applied = grokbot_packs.apply_setup(
        tmp_path,
        PACK_ID,
        bearer_env="TEST_GROKBOT_BEARER",
        owner_workspace=owner,
    )
    pack_path = grokbot_packs.instance_config_path(tmp_path, PACK_ID)
    assert applied["apply"] is True
    assert pack_path.is_file()
    assert pack_path.stat().st_mode & 0o777 == 0o600
    assert not grokbot_ops.config_path(tmp_path, PACK_ID).exists()
    config = json.loads(pack_path.read_text(encoding="utf-8"))
    assert set(config) == grokbot_packs.OPERATIONS_RELAY_INSTANCE_KEYS
    assert SECRET not in json.dumps(config)
    assert "cli_executable" not in config
    assert config["owner_workspace"] == str(owner)
    assert config["bearer"] == {"kind": "env", "name": "TEST_GROKBOT_BEARER"}

    with _reject("unexpected-key"):
        grokbot_packs.apply_setup(
            tmp_path,
            PACK_ID,
            bearer_env="TEST_GROKBOT_BEARER",
            owner_workspace=owner,
            cli_executable=tmp_path / "bin",
        )
    with _reject("unexpected-key"):
        grokbot_packs.apply_setup(
            tmp_path,
            "operator",
            bearer_env="TEST_GROKBOT_BEARER",
            owner_workspace=owner,
        )
    with _reject("missing-path-reference"):
        grokbot_packs.preview_setup(tmp_path, PACK_ID, bearer_env="TEST_GROKBOT_BEARER")


def test_setup_rejects_insecure_owner_and_sanitizes_errors(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    owner = tmp_path / "owner"
    owner.mkdir(mode=0o755)
    os.chmod(owner, 0o755)
    with _reject("unsafe-path") as caught:
        grokbot_packs.apply_setup(
            tmp_path,
            PACK_ID,
            bearer_env="TEST_GROKBOT_BEARER",
            owner_workspace=owner,
        )
    assert SECRET not in str(caught.value)
    assert SECRET not in caught.value.reason
    assert str(owner) not in str(caught.value)
    assert str(owner) not in caught.value.reason
    assert not grokbot_packs.instance_config_path(tmp_path, PACK_ID).exists()

    linked = tmp_path / "linked-owner"
    linked.symlink_to(owner, target_is_directory=True)
    with _reject("unsafe-path") as linked_caught:
        grokbot_packs.apply_setup(
            tmp_path,
            PACK_ID,
            bearer_env="TEST_GROKBOT_BEARER",
            owner_workspace=linked,
        )
    assert str(linked) not in str(linked_caught.value)
    assert SECRET not in str(linked_caught.value)


def _setup_pack(tmp_path: Path, monkeypatch, bearer: str, env_name: str = "TEST_GROKBOT_BEARER") -> Path:
    monkeypatch.setenv(env_name, bearer)
    owner = _owner(tmp_path / "owner")
    grokbot_packs.apply_setup(
        tmp_path,
        PACK_ID,
        bearer_env=env_name,
        owner_workspace=owner,
    )
    return owner


def _assert_isolated_bearer_rejection(caught, *secrets: str) -> None:
    assert caught.value.reason == "invalid_request"
    text = str(caught.value)
    for secret in (SECRET, *secrets):
        assert secret not in text
        assert secret not in caught.value.reason
        assert secret not in caught.value.message


def test_lifecycle_rejects_short_bearer(tmp_path: Path, monkeypatch):
    owner = _setup_pack(tmp_path, monkeypatch, SHORT_BEARER)
    with pytest.raises(operations_relay.OperationsRelayError) as resolved:
        operations_relay._resolve_bearer({"kind": "env", "name": "TEST_GROKBOT_BEARER"})
    _assert_isolated_bearer_rejection(resolved, SHORT_BEARER)
    with pytest.raises(operations_relay.OperationsRelayError) as served:
        operations_relay.build_listener_from_target(
            tmp_path,
            bind=None,
            allowed_hosts=[],
            allowed_origins=[],
            bearer_file=None,
            bearer_env="TEST_GROKBOT_BEARER",
        )
    _assert_isolated_bearer_rejection(served, SHORT_BEARER)
    checks = operations_relay.doctor(tmp_path)
    canary = operations_relay.canary(tmp_path)
    sanitized = json.dumps({"checks": checks, "canary": canary, "owner": str(owner)})
    assert any(check["check"] == "config" and check["status"] == "fail" for check in checks)
    assert canary["ok"] is False
    assert canary["reason"] == "config"
    assert SHORT_BEARER not in sanitized
    assert SECRET not in sanitized


def test_lifecycle_rejects_shared_dispatch_bearer(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GROKBOT_DISPATCH_TOKEN", SHARED_DISPATCH_BEARER)
    owner = _setup_pack(tmp_path, monkeypatch, SHARED_DISPATCH_BEARER)
    with pytest.raises(operations_relay.OperationsRelayError) as resolved:
        operations_relay._resolve_bearer({"kind": "env", "name": "TEST_GROKBOT_BEARER"})
    _assert_isolated_bearer_rejection(resolved, SHARED_DISPATCH_BEARER)
    with pytest.raises(operations_relay.OperationsRelayError) as served:
        operations_relay.build_listener_from_target(
            tmp_path,
            bind=None,
            allowed_hosts=[],
            allowed_origins=[],
            bearer_file=None,
            bearer_env="TEST_GROKBOT_BEARER",
        )
    _assert_isolated_bearer_rejection(served, SHARED_DISPATCH_BEARER)
    checks = operations_relay.doctor(tmp_path)
    canary = operations_relay.canary(tmp_path)
    sanitized = json.dumps({"checks": checks, "canary": canary})
    assert any(check["check"] == "config" and check["status"] == "fail" for check in checks)
    assert canary["ok"] is False
    assert canary["reason"] == "config"
    assert SHARED_DISPATCH_BEARER not in sanitized
    assert str(owner) not in sanitized


def test_auth_gate_and_inventory():
    config = operations_relay.OperationsRelayListenerConfig(
        target=Path("/tmp/operations-relay-target"),
        bind_host="127.0.0.1",
        bind_port=8777,
        allowed_hosts=(),
        allowed_origins=(),
        bearer=SECRET,
        owner_workspace="/tmp/operations-relay-owner",
    )
    gate = operations_relay._OperationsRelayRequestGate(config)
    assert gate.reject_reason({"host": "127.0.0.1", "authorization": f"Bearer {SECRET}"}, 0) is None
    assert gate.reject_reason({"host": "127.0.0.1"}, 0) == "unauthorized"
    assert gate.reject_reason({"host": "127.0.0.1", "authorization": "Bearer wrong-token"}, 0) == "unauthorized"
    adapter = operations_relay.OperationsRelayAdapter(config, operations_relay.OperationsRelayTools.placeholder(config))
    assert adapter.authorized(f"Bearer {SECRET}") is True
    assert adapter.authorized("Bearer wrong-token") is False
    assert adapter.authorized(None) is False
    names = {tool["name"] for tool in adapter.tool_inventory()}
    assert names == set(TOOLS)
    assert operations_relay.OperationsRelayTools.placeholder(config).inventory() == list(TOOLS)


def test_submit_rejects_secrets_paths_commands_urls_and_unexpected_keys(tmp_path: Path, monkeypatch):
    tools, queue, owner = _tools(tmp_path, monkeypatch)
    entry = _entry()
    rejected = [
        {**entry, "path": "/etc/passwd"},
        {**entry, "command": "curl http://example.invalid"},
        {**entry, "url": "https://example.invalid/hook"},
        {**entry, "credential": "hunter2-example"},
        {**entry, "token": SECRET},
        {**entry, "owner": str(owner)},
        {"schema": grokbot_findings.FINDINGS_SCHEMA, "entries": [entry]},
        {key: value for key, value in entry.items() if key != "title"},
        {**entry, "producer": "https://example.invalid"},
        {**entry, "finding_id": "/var/run/secret"},
        {**entry, "revision": "rm -rf /"},
    ]
    for payload in rejected:
        with pytest.raises(operations_relay.OperationsRelayError) as caught:
            tools.call_tool("submit_automation_finding", payload)
        assert caught.value.reason == "invalid_request"
        assert SECRET not in str(caught.value)
        assert PRIVATE_TITLE not in str(caught.value)
        assert str(owner) not in str(caught.value)
        assert str(queue) not in str(caught.value)
    with pytest.raises(operations_relay.OperationsRelayError) as unknown:
        tools.call_tool("hidden_tool", entry)
    assert unknown.value.reason == "invalid_request"
    assert not (owner / "memory" / "handoff-inbox").exists()


def test_first_delivery_writes_one_draft_and_one_fleet_report(tmp_path: Path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    captured = _capture_events(monkeypatch)
    tools, queue, owner = _tools(tmp_path, monkeypatch)
    entry = _entry()

    result = tools.call_tool("submit_automation_finding", entry)

    assert result["handle"] == _handle(entry)
    assert result["created"] == 1
    assert result["reported"] == 1
    assert result["pending"] == 0
    assert result["state"] == "reported"
    _assert_opaque_result(result)
    drafts = list((owner / "memory" / "handoff-inbox").glob("*.md"))
    assert len(drafts) == 1
    assert PRIVATE_TITLE in drafts[0].read_text(encoding="utf-8")
    assert len(captured) == 1
    assert captured[0]["run_id"] == result["handle"]
    assert captured[0]["seat"] == "findings-relay"
    assert set(captured[0]) == {"run_id", "seat", "harness", "state", "ts", "sequence", "digest"}
    _assert_opaque(json.dumps(captured[0], sort_keys=True))


def test_replay_is_idempotent(tmp_path: Path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    captured = _capture_events(monkeypatch)
    tools, _queue, owner = _tools(tmp_path, monkeypatch)
    entry = _entry()

    first = tools.call_tool("submit_automation_finding", entry)
    second = tools.call_tool("submit_automation_finding", entry)

    assert first["handle"] == second["handle"] == _handle(entry)
    assert first["created"] == 1
    assert second["created"] == 0
    assert second["known"] == 1
    assert second["state"] == "reported"
    _assert_opaque_result(second)
    drafts = list((owner / "memory" / "handoff-inbox").glob("*.md"))
    assert len(drafts) == 1
    assert len(captured) == 1


def test_status_returns_digest_and_state_without_entry_content(tmp_path: Path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    _capture_events(monkeypatch)
    tools, _queue, owner = _tools(tmp_path, monkeypatch)
    entry = _entry()
    missing = tools.call_tool(
        "automation_finding_status",
        {"producer": entry["producer"], "finding_id": entry["finding_id"], "revision": entry["revision"]},
    )
    assert missing["handle"] == _handle(entry)
    assert missing["state"] == "unknown"
    _assert_opaque_result(missing)

    tools.call_tool("submit_automation_finding", entry)
    status = tools.call_tool(
        "automation_finding_status",
        {"producer": entry["producer"], "finding_id": entry["finding_id"], "revision": entry["revision"]},
    )
    assert status["handle"] == _handle(entry)
    assert status["state"] == "reported"
    _assert_opaque_result(status)
    with pytest.raises(operations_relay.OperationsRelayError):
        tools.call_tool(
            "automation_finding_status",
            {
                "producer": entry["producer"],
                "finding_id": entry["finding_id"],
                "revision": entry["revision"],
                "path": str(owner),
            },
        )


def test_status_degrades_when_findings_storage_is_unsafe(tmp_path: Path, monkeypatch):
    tools, _queue, _owner = _tools(tmp_path, monkeypatch)
    entry = _entry()

    def unsafe(_target: Path):
        raise grokbot_findings.FindingsError("unsafe-storage")

    monkeypatch.setattr(grokbot_findings, "_open_findings_readonly", unsafe)
    result = tools.call_tool(
        "automation_finding_status",
        {"producer": entry["producer"], "finding_id": entry["finding_id"], "revision": entry["revision"]},
    )
    assert result == {"handle": _handle(entry), "state": "unknown"}
    _assert_opaque_result(result)


def test_doctor_canary_unit_and_cli_hide_secrets(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", SECRET)
    owner = _owner(tmp_path / "owner")
    grokbot_packs.apply_setup(
        tmp_path,
        PACK_ID,
        bearer_env="TEST_GROKBOT_BEARER",
        owner_workspace=owner,
    )
    checks = grokbot_packs.doctor(tmp_path, PACK_ID)
    canary = grokbot_packs.canary(tmp_path, PACK_ID)
    unit = grokbot_packs.render_install_service(tmp_path, PACK_ID)
    sanitized = json.dumps({"checks": checks, "canary": canary})
    assert SECRET not in sanitized
    assert str(owner) not in sanitized
    assert all(set(check) == {"check", "status"} for check in checks)
    assert canary["ok"] is False
    assert SECRET not in unit
    assert "--pack" in unit
    assert PACK_ID in unit
    assert "127.0.0.1:8777" in unit
    assert "NoNewPrivileges=yes" in unit
    unit_dir = tmp_path / "units"
    installed = grokbot_packs.apply_install_service(tmp_path, PACK_ID, out_dir=unit_dir)
    assert installed["unit"] == grokbot_ops.unit_name(PACK_ID)

    assert cli.main(["run", "cloud", "grokbot", "pack", "list", "--target", str(tmp_path), "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert any(pack["id"] == PACK_ID for pack in listed["packs"])
    assert (
        cli.main(
            [
                "run",
                "cloud",
                "grokbot",
                "pack",
                "setup",
                "--target",
                str(tmp_path),
                "--id",
                PACK_ID,
                "--bearer-env",
                "TEST_GROKBOT_BEARER",
                "--owner",
                str(owner),
                "--json",
            ]
        )
        == 0
    )
    preview = json.loads(capsys.readouterr().out)
    assert preview["apply"] is False
    assert SECRET not in json.dumps(preview)
    assert str(owner) not in json.dumps(preview)

    relay_owner = _owner(tmp_path / "relay-owner")
    assert (
        cli.main(
            [
                "run",
                "cloud",
                "grokbot",
                "pack",
                "relay-setup",
                "--target",
                str(tmp_path),
                "--owner",
                str(relay_owner),
                "--json",
            ]
        )
        == 0
    )
    relay_preview = json.loads(capsys.readouterr().out)
    assert relay_preview["apply"] is False
    assert SECRET not in json.dumps(relay_preview)
