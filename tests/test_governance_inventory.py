"""Contract tests for the bounded governance inventory export."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone

import pytest

from brigade import cli, fleet_model_roster, governance_inventory, receipt_schema, run_receipts
from brigade.run_transport import WorkerResult


NOW = datetime(2026, 9, 4, 17, 30, tzinfo=timezone.utc)


def _write(path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _configured_target(tmp_path):
    _write(
        tmp_path / ".brigade" / "roster.toml",
        """orchestrator = \"chef\"\n\n[agents.chef]\ncli = \"claude\"\nrole = \"orchestrator\"\nmodel = \"opus\"\npurpose = \"coordinate approved work\"\nenv = { CANARY = \"do-not-export\" }\n\n[agents.research]\ncli = \"codex\"\nrole = \"researcher\"\nmodel = \"gpt-5.6-luna\"\n""",
    )
    _write(
        tmp_path / ".brigade" / "tools.toml",
        """[[tool]]\nid = \"code-search\"\nname = \"Code Search\"\nfamily = \"search\"\ndescription = \"Find symbols\"\ncommand = \"/private/bin/code-search --token CANARY\"\ncapability = [\"code.search\"]\neffects = [\"read\"]\npermissions = [\"repository\"]\n""",
    )
    _write(
        tmp_path / ".brigade" / "mcp.json",
        json.dumps(
            {
                "version": 1,
                "servers": {
                    "local": {
                        "transport": "stdio",
                        "command": "/private/bin/local-mcp",
                        "args": ["--token", "CANARY"],
                        "env": {"TOKEN": {"literal": "CANARY"}},
                    },
                    "remote": {
                        "transport": "http",
                        "url": "https://example.test/api",
                        "headers": {"Authorization": {"literal": "CANARY"}},
                    },
                },
            }
        ),
    )
    run_dir = tmp_path / ".brigade" / "runs" / "run-1"
    _write(
        run_dir / "run.json",
        json.dumps(
            receipt_schema.stamp_run_receipt(
                {
                    "started_at": "2026-09-03T10:00:00Z",
                    "finished_at": "2026-09-03T10:01:00Z",
                    "task": "CANARY task text must not escape",
                }
            )
        ),
    )
    worker = WorkerResult(
        worker="research",
        task="CANARY worker task",
        text="CANARY output",
        ok=True,
        effective_model="gpt-5.6-luna",
    )
    _write(
        run_dir / "worker-results.json",
        json.dumps(
            receipt_schema.worker_results_document(run_receipts.worker_payload([worker]), producer_run_id=run_dir.name)
        ),
    )
    return tmp_path


def test_inventory_is_deterministic_and_never_exports_secret_or_path(tmp_path):
    target = _configured_target(tmp_path)

    first = governance_inventory.build_inventory(target=target, now=NOW)
    second = governance_inventory.build_inventory(target=target, now=NOW)

    assert governance_inventory.canonical_json(first) == governance_inventory.canonical_json(second)
    rendered = governance_inventory.canonical_json(first)
    assert "CANARY" not in rendered
    assert "/private/" not in rendered
    assert first["scope"]["notice"] == governance_inventory.SCOPE_NOTICE
    assert first["registries"]["mcp_servers"]["items"] == [
        {
            "component_type": "local-mcp-server",
            "enabled": True,
            "name": "local",
            "transport": "stdio",
        },
        {
            "component_type": "remote-mcp-service",
            "enabled": True,
            "endpoint": "https://example.test",
            "name": "remote",
            "transport": "http",
        },
    ]
    assert first["observed_runs"]["items"] == [
        {
            "first_seen": "2026-09-03T10:00:00Z",
            "last_seen": "2026-09-03T10:00:00Z",
            "model": "gpt-5.6-luna",
            "provider": "openai",
            "run_count": 1,
            "seat": "research",
            "unknown": {},
        }
    ]


def test_time_bounds_constrain_only_observed_run_facts(tmp_path):
    target = _configured_target(tmp_path)

    inventory = governance_inventory.build_inventory(
        target=target,
        since="2026-09-04T00:00:00Z",
        until="2026-09-05T00:00:00Z",
        now=NOW,
    )

    assert inventory["time_window"] == {
        "since": "2026-09-04T00:00:00Z",
        "until": "2026-09-05T00:00:00Z",
    }
    assert inventory["observed_runs"]["items"] == []
    assert inventory["registries"]["agents"]["items"]


def test_agents_expose_each_required_unknown_field(tmp_path):
    agents = governance_inventory.build_inventory(target=_configured_target(tmp_path), now=NOW)["registries"]["agents"][
        "items"
    ]

    assert agents[1]["unknown"] == {
        "environment": {"reason": "environment is not configured", "value": "unknown"},
        "lifecycle": {"reason": "lifecycle is not configured", "value": "unknown"},
        "owner": {"reason": "owner is not configured", "value": "unknown"},
        "privilege": {"reason": "privilege is not configured", "value": "unknown"},
        "purpose": {"reason": "purpose is not configured", "value": "unknown"},
    }


def test_observed_runs_use_worker_results_and_configured_provider_attribution(tmp_path):
    target = _configured_target(tmp_path)
    run_dir = target / ".brigade" / "runs" / "run-1"
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    run["workers"] = [{"worker": "wrong", "effective_model": "wrong-model"}]
    _write(run_dir / "run.json", json.dumps(run))

    inventory = governance_inventory.build_inventory(target=target, now=NOW)

    assert inventory["observed_runs"]["items"] == [
        {
            "first_seen": "2026-09-03T10:00:00Z",
            "last_seen": "2026-09-03T10:00:00Z",
            "model": "gpt-5.6-luna",
            "provider": "openai",
            "run_count": 1,
            "seat": "research",
            "unknown": {},
        }
    ]


def test_observed_provider_without_a_configured_seat_is_explicitly_unknown(tmp_path):
    target = _configured_target(tmp_path)
    run_dir = target / ".brigade" / "runs" / "run-1"
    worker = WorkerResult(worker="retired-seat", task="CANARY", text="CANARY", ok=True, effective_model="gpt-5.6")
    _write(
        run_dir / "worker-results.json",
        json.dumps(
            receipt_schema.worker_results_document(run_receipts.worker_payload([worker]), producer_run_id=run_dir.name)
        ),
    )

    observed = governance_inventory.build_inventory(target=target, now=NOW)["observed_runs"]["items"]

    assert observed[0]["provider"] == "unknown"
    assert observed[0]["unknown"] == {
        "provider_attribution": {
            "reason": "observed seat is not configured in the workspace roster",
            "value": "unknown",
        }
    }


@pytest.mark.parametrize(
    "mutation",
    [
        lambda document, run_dir: document.pop("schema"),
        lambda document, run_dir: document.update({"producer_run_id": "other-run"}),
        lambda document, run_dir: document["results"].append({"worker": "", "effective_model": "gpt-5.6"}),
    ],
)
def test_observed_runs_refuse_unverifiable_worker_documents(tmp_path, mutation):
    target = _configured_target(tmp_path)
    run_dir = target / ".brigade" / "runs" / "run-1"
    document = json.loads((run_dir / "worker-results.json").read_text(encoding="utf-8"))
    mutation(document, run_dir)
    _write(run_dir / "worker-results.json", json.dumps(document))

    observed = governance_inventory.build_inventory(target=target, now=NOW)["observed_runs"]

    assert observed["items"] == []
    assert observed["errors"]


def test_observed_run_budget_is_global_and_aggregates_incrementally(tmp_path, monkeypatch):
    target = _configured_target(tmp_path)
    source = target / ".brigade" / "runs" / "run-1"
    second = target / ".brigade" / "runs" / "run-2"
    second.mkdir(parents=True)
    _write(second / "run.json", (source / "run.json").read_text(encoding="utf-8"))
    worker = json.loads((source / "worker-results.json").read_text(encoding="utf-8"))
    worker["producer_run_id"] = second.name
    _write(second / "worker-results.json", json.dumps(worker))
    monkeypatch.setattr(governance_inventory, "MAX_WORKER_ROWS", 1)

    observed = governance_inventory.build_inventory(target=target, now=NOW)["observed_runs"]

    assert observed["items"][0]["run_count"] == 1
    assert observed["errors"] == ["run-receipts: worker-row-limit-exceeded"]


def test_remote_mcp_ipv6_endpoint_preserves_brackets(tmp_path):
    target = _configured_target(tmp_path)
    _write(
        target / ".brigade" / "mcp.json",
        json.dumps({"servers": {"v6": {"transport": "http", "url": "https://[2001:db8::1]:8443/private"}}}),
    )

    endpoint = governance_inventory.build_inventory(target=target, now=NOW)["registries"]["mcp_servers"]["items"][0][
        "endpoint"
    ]

    assert endpoint == "https://[2001:db8::1]:8443"


@pytest.mark.parametrize(
    "since, until",
    [
        ("not-a-time", None),
        ("2026-09-05T00:00:00Z", "2026-09-04T00:00:00Z"),
    ],
)
def test_time_bounds_fail_closed(since, until, tmp_path):
    with pytest.raises(ValueError):
        governance_inventory.build_inventory(target=tmp_path, since=since, until=until, now=NOW)


def test_malformed_or_unsafe_mcp_entries_are_bounded_errors(tmp_path):
    target = _configured_target(tmp_path)
    _write(
        target / ".brigade" / "mcp.json",
        json.dumps({"servers": {"bad": {"transport": "socket", "url": "file:///private/secret"}}}),
    )

    inventory = governance_inventory.build_inventory(target=target, now=NOW)

    registry = inventory["registries"]["mcp_servers"]
    assert registry["items"] == []
    assert registry["errors"] == ["bad: unsupported transport"]


def test_invalid_remote_port_is_a_bounded_mcp_error(tmp_path):
    target = _configured_target(tmp_path)
    _write(
        target / ".brigade" / "mcp.json",
        json.dumps({"servers": {"bad": {"transport": "http", "url": "https://example.test:bad/mcp"}}}),
    )

    inventory = governance_inventory.build_inventory(target=target, now=NOW)

    assert inventory["registries"]["mcp_servers"]["items"] == []
    assert inventory["registries"]["mcp_servers"]["errors"] == ["bad: unsafe endpoint"]


def test_remote_userinfo_query_and_fragment_are_rejected(tmp_path):
    target = _configured_target(tmp_path)
    _write(
        target / ".brigade" / "mcp.json",
        json.dumps(
            {
                "servers": {
                    "bad": {
                        "transport": "http",
                        "url": "https://user:CANARY@example.test/api?token=CANARY#fragment",
                    }
                }
            }
        ),
    )

    inventory = governance_inventory.build_inventory(target=target, now=NOW)

    assert "CANARY" not in governance_inventory.canonical_json(inventory)
    assert inventory["registries"]["mcp_servers"]["items"] == []
    assert inventory["registries"]["mcp_servers"]["errors"] == ["bad: unsafe endpoint"]


def test_remote_mcp_endpoint_discards_its_path(tmp_path):
    target = _configured_target(tmp_path)

    endpoint = governance_inventory.build_inventory(target=target, now=NOW)["registries"]["mcp_servers"]["items"][1][
        "endpoint"
    ]

    assert endpoint == "https://example.test"


def test_non_finite_mcp_values_are_bounded_errors(tmp_path):
    target = _configured_target(tmp_path)
    _write(
        target / ".brigade" / "mcp.json",
        '{"servers":{"bad":{"transport":"http","url":"https://example.test","timeout":NaN}}}',
    )

    inventory = governance_inventory.build_inventory(target=target, now=NOW)

    assert inventory["registries"]["mcp_servers"] == {
        "schema": governance_inventory.MCP_SCHEMA,
        "items": [],
        "errors": ["mcp-catalog: non-finite"],
        "unknown": {},
    }


def test_symlinked_or_oversized_mcp_input_is_refused(tmp_path):
    target = _configured_target(tmp_path)
    mcp = target / ".brigade" / "mcp.json"
    secret = target / "secret.json"
    _write(secret, "{}")
    mcp.unlink()
    os.symlink(secret, mcp)

    symlinked = governance_inventory.build_inventory(target=target, now=NOW)["registries"]["mcp_servers"]

    assert symlinked["items"] == []
    assert symlinked["errors"] == ["mcp-catalog: symlink"]

    mcp.unlink()
    mcp.touch()
    with mcp.open("r+b") as handle:
        handle.truncate(governance_inventory.MAX_INPUT_BYTES + 1)
    oversized = governance_inventory.build_inventory(target=target, now=NOW)["registries"]["mcp_servers"]

    assert oversized["items"] == []
    assert oversized["errors"] == ["mcp-catalog: oversized"]


def test_mcp_input_uses_a_descriptor_relative_read(tmp_path, monkeypatch):
    target = _configured_target(tmp_path)
    calls = []
    original = governance_inventory.dirfd.open_child_file

    def open_child(parent_fd, name, flags, mode=0o600):
        calls.append(name)
        return original(parent_fd, name, flags, mode)

    monkeypatch.setattr(governance_inventory.dirfd, "open_child_file", open_child)

    registry = governance_inventory.build_inventory(target=target, now=NOW)["registries"]["mcp_servers"]

    assert registry["items"]
    assert "mcp.json" in calls


def test_workspace_inventory_never_falls_back_to_the_user_roster(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _write(
        home / ".brigade" / "roster.toml",
        'orchestrator = "home"\n\n[agents.home]\ncli = "codex"\nrole = "worker"\nmodel = "gpt-5.6"\n',
    )
    monkeypatch.setenv("HOME", str(home))
    target = tmp_path / "workspace"
    target.mkdir()

    agents = governance_inventory.build_inventory(target=target, now=NOW)["registries"]["agents"]

    assert agents["items"] == []
    assert agents["source"] == {"kind": "workspace-roster", "state": "unavailable"}
    assert agents["unknown"] == {"ownership": {"reason": "workspace roster is not configured", "value": "unknown"}}


def _lkg_record() -> dict[str, object]:
    roster = {
        "schema": fleet_model_roster.ROSTER_SCHEMA,
        "revision": 7,
        "revision_updated_at": "2026-09-04T17:20:00Z",
        "issued_at": "2026-09-04T17:20:00Z",
        "expires_at": "2026-09-04T17:35:00Z",
        "audience_node_id": "node-a",
        "seats": [
            {
                "seat": "research",
                "provider": "openai",
                "model": "gpt-5.6-luna",
                "reasoning": "medium",
                "enabled": True,
                "bindings": {"brigade": {"cli": "codex"}, "t3_fleet": {"instance_id": "codex", "service_tier": None}},
            },
            {
                "seat": "disabled",
                "provider": "openai",
                "model": "gpt-5.5",
                "reasoning": "high",
                "enabled": False,
                "bindings": {"brigade": {"cli": "codex"}, "t3_fleet": {"instance_id": "codex", "service_tier": None}},
            },
        ],
        "consumer_defaults": {"brigade-run": "research"},
        "retired_models": [],
    }
    roster["document_sha256"] = fleet_model_roster.roster_digest(roster)
    roster["mac"] = {
        "algorithm": fleet_model_roster.MAC_ALGORITHM,
        "value": fleet_model_roster.roster_mac("node-token", roster),
    }
    return {"cached_at": "2026-09-04T17:25:00Z", "highest_revision": 7, "roster": roster}


def test_model_registry_projects_a_valid_local_fleet_lkg(tmp_path, monkeypatch):
    monkeypatch.setattr(governance_inventory.fleet_model_admission, "_load_lkg_record", _lkg_record)
    monkeypatch.setattr(governance_inventory.fleet_model_admission, "_node_token", lambda: "node-token")
    monkeypatch.setattr(governance_inventory.fleet_model_admission._client, "resolve_node_id", lambda: "node-a")
    monkeypatch.setattr(governance_inventory.fleet_model_admission, "utc_now", lambda: NOW)

    policy = governance_inventory.build_inventory(target=_configured_target(tmp_path), now=NOW)["registries"][
        "model_providers"
    ]["fleet_policy"]

    assert policy == {
        "admissions": [{"model": "gpt-5.6-luna", "provider": "openai", "reasoning": "medium", "seat": "research"}],
        "denials": [{"model": "gpt-5.5", "provider": "openai", "reason": "seat-disabled", "seat": "disabled"}],
        "source": {
            "cached_at": "2026-09-04T17:25:00Z",
            "kind": "node-local-fleet-model-policy-lkg",
            "revision": 7,
            "state": "available",
        },
        "unknown": {},
    }


def test_model_registry_reports_a_missing_fleet_lkg_without_network(tmp_path, monkeypatch):
    monkeypatch.setattr(
        governance_inventory.fleet_model_admission, "_load_lkg_record", lambda: (_ for _ in ()).throw(OSError())
    )

    policy = governance_inventory.build_inventory(target=_configured_target(tmp_path), now=NOW)["registries"][
        "model_providers"
    ]["fleet_policy"]

    assert policy == {
        "admissions": [],
        "denials": [],
        "source": {
            "cached_at": None,
            "kind": "node-local-fleet-model-policy-lkg",
            "revision": None,
            "state": "unavailable",
        },
        "unknown": {
            "fleet_policy": {"reason": "no valid node-local Fleet model-policy LKG is available", "value": "unknown"}
        },
    }


def test_duplicate_mcp_names_fail_closed(tmp_path):
    target = _configured_target(tmp_path)
    _write(
        target / ".brigade" / "mcp.json",
        """{"servers":{"same":{"transport":"stdio","command":"one"},"same":{"transport":"stdio","command":"two"}}}""",
    )

    inventory = governance_inventory.build_inventory(target=target, now=NOW)

    assert inventory["registries"]["mcp_servers"]["items"] == []
    assert inventory["registries"]["mcp_servers"]["errors"] == ["mcp-catalog: duplicate-key"]


def test_cyclonedx_mapping_is_optional_and_uses_17_contract(tmp_path):
    inventory = governance_inventory.build_inventory(target=_configured_target(tmp_path), now=NOW)

    bom = governance_inventory.cyclonedx_bom(inventory)

    assert bom["$schema"] == "https://cyclonedx.org/schema/bom-1.7.schema.json"
    assert bom["bomFormat"] == "CycloneDX"
    assert bom["specVersion"] == "1.7"
    assert bom["metadata"]["properties"] == [
        {"name": "brigade:time_window:since", "value": ""},
        {"name": "brigade:time_window:until", "value": ""},
    ]
    assert any(item["name"] == "local" for item in bom["components"])
    assert bom["services"] == [{"bom-ref": "mcp:remote", "endpoints": ["https://example.test"], "name": "remote"}]
    assert {
        "bom-ref": "model:openai:gpt-5.6-luna",
        "group": "openai",
        "name": "gpt-5.6-luna",
        "type": "machine-learning-model",
        "version": "configured",
    } in bom["components"]
    assert all("modelCard" not in item for item in bom["components"])


def test_artifact_manifest_is_detached_and_covers_final_bytes(tmp_path):
    target = _configured_target(tmp_path)
    output = tmp_path / "export"

    artifacts = governance_inventory.write_artifacts(target=target, output_dir=output, now=NOW, cyclonedx=True)

    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert artifacts == ["cyclonedx.json", "inventory.json", "manifest.json"]
    assert [item["path"] for item in manifest["artifacts"]] == ["cyclonedx.json", "inventory.json"]
    assert manifest["time_window"] == {"since": None, "until": None}
    for item in manifest["artifacts"]:
        data = (output / item["path"]).read_bytes()
        assert item["bytes"] == len(data)
        assert item["sha256"] == hashlib.sha256(data).hexdigest()

    (output / "inventory.json").write_text("tampered\n", encoding="utf-8")
    inventory_item = next(item for item in manifest["artifacts"] if item["path"] == "inventory.json")
    assert inventory_item["sha256"] != hashlib.sha256((output / "inventory.json").read_bytes()).hexdigest()


def test_artifact_publishing_uses_the_guarded_parent_fsync_helper(tmp_path, monkeypatch):
    target = _configured_target(tmp_path)
    output = tmp_path / "export"
    calls = []
    monkeypatch.setattr(governance_inventory.localio, "_fsync_parent_directory", lambda path: calls.append(path))

    governance_inventory.write_artifacts(target=target, output_dir=output, now=NOW)

    assert calls == [output.parent]


def test_windows_publish_falls_back_only_when_atomic_directory_replacement_is_unavailable(tmp_path, monkeypatch):
    temporary = tmp_path / "temporary"
    destination = tmp_path / "destination"
    temporary.mkdir()
    destination.mkdir()
    calls = []
    replace = governance_inventory.os.replace

    def replace_after_windows_refusal(source, target):
        calls.append((source, target))
        if len(calls) == 1:
            raise FileExistsError()
        replace(source, target)

    monkeypatch.setattr(governance_inventory.os, "name", "nt")
    monkeypatch.setattr(governance_inventory.os, "replace", replace_after_windows_refusal)

    governance_inventory._publish_directory(temporary, destination)

    assert calls == [(temporary, destination), (temporary, destination)]
    assert destination.is_dir()


def test_cli_emits_combined_json_or_refuses_nonempty_output(tmp_path, capsys):
    target = _configured_target(tmp_path)

    assert cli.main(["governance", "inventory", "--target", str(target), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["schema"] == governance_inventory.INVENTORY_SCHEMA

    assert cli.main(["governance", "inventory", "--target", str(target), "--cyclonedx"]) == 0
    assert json.loads(capsys.readouterr().out)["cyclonedx"]["specVersion"] == "1.7"

    out = target / "out"
    out.mkdir()
    (out / "present").write_text("x", encoding="utf-8")
    assert cli.main(["governance", "inventory", "--target", str(target), "--output-dir", str(out)]) == 2


def test_cli_json_output_does_not_echo_an_absolute_output_path(tmp_path, capsys):
    target = _configured_target(tmp_path)
    output = tmp_path / "export"

    assert cli.main(["governance", "inventory", "--target", str(target), "--output-dir", str(output), "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload == {"artifacts": ["inventory.json", "manifest.json"]}


def test_cli_filesystem_failure_has_a_bounded_error(tmp_path, monkeypatch, capsys):
    target = _configured_target(tmp_path)
    monkeypatch.setattr(governance_inventory, "build_inventory", lambda **_kwargs: (_ for _ in ()).throw(OSError()))

    assert cli.main(["governance", "inventory", "--target", str(target)]) == 2

    assert capsys.readouterr().err == "error: governance inventory input or output is inaccessible\n"
