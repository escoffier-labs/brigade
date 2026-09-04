"""Contract tests for the bounded governance inventory export."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pytest

from brigade import cli, governance_inventory


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
    _write(
        tmp_path / ".brigade" / "runs" / "run-1" / "run.json",
        json.dumps(
            {
                "started_at": "2026-09-03T10:00:00Z",
                "finished_at": "2026-09-03T10:01:00Z",
                "task": "CANARY task text must not escape",
                "workers": [
                    {
                        "worker": "research",
                        "effective_model": "gpt-5.6-luna",
                        "task": "CANARY worker task",
                        "text": "CANARY output",
                    }
                ],
            }
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
            "endpoint": "https://example.test/api",
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
    assert bom["services"] == [{"bom-ref": "mcp:remote", "endpoints": ["https://example.test/api"], "name": "remote"}]
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
