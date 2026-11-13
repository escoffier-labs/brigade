"""Offline drive of connector discovery and a setup preview."""

import json
import tempfile
from pathlib import Path


def add_parser(sub, helper):
    parser = sub.add_parser("hub-connector", help="Drive connector list and cloud-agent setup preview offline.")
    parser.add_argument("--target", required=True, help="Temp target created by new-target.")
    parser.set_defaults(func=lambda args: command(args, helper))


def command(args, helper):
    root = helper.state_root(args)
    target = helper.guard_target(Path(args.target), root)
    work = Path(tempfile.mkdtemp(prefix="hub-connector-", dir=target))
    work.chmod(0o700)
    runtime = work / "runtime.json"
    # Access-mode placeholders need no credentials for validation. Nothing is started.
    document = {
        "schema": "brigade.cloud-agent.runtime.v1",
        "auth": {
            "mode": "access",
            "issuer": "https://team.example.test",
            "team_domain": "team.example.test",
            "audience": "c" * 64,
            "approved_email": "agent@example.test",
            "resource": "https://connector.example.test/mcp",
        },
        "paths": {
            "state_dir": str(work / "state"),
            "workspace": str(work / "workspace"),
            "bin": {name: str(work / name) for name in ("brigade", "git", "gh")},
        },
        "hub": {
            "principal": "agent:cloud-agent",
            "node_id": "00000000-0000-4000-8000-000000000004",
            "message_token_file": str(work / "absent-token"),
            "poll_seconds": 30,
            "recipients": {},
        },
    }
    runtime.write_text(json.dumps(document), encoding="utf-8")
    runtime.chmod(0o600)
    base = helper.brigade_args(args) + ["hub", "connector"]
    listing = helper.run(base + ["list", "--target", str(work)], root=root, name="hub-connector-list")
    preview = helper.run(
        base + ["setup", "--target", str(work), "--id", "cloud-agent", "--runtime-path", str(runtime)],
        root=root,
        name="hub-connector-setup",
    )
    packs = listing.get("json")
    result = preview.get("json")
    ok = (
        listing["returncode"] == preview["returncode"] == 0
        and isinstance(packs, list)
        and {p["id"] for p in packs} == {"dot", "duet", "cloud-agent"}
        and isinstance(result, dict)
        and result.get("action") == "setup"
        and result.get("apply") is False
        and result.get("pack_id") == "cloud-agent"
        and result.get("bind") == "127.0.0.1:8783"
        and not (work / ".brigade").exists()
        and not (work / "absent-token").exists()
    )
    document["auth"] = {"mode": "token", "token_file": str(work / "absent-listener")}
    runtime.write_text(json.dumps(document), encoding="utf-8")
    token_preview = helper.run(
        base + ["setup", "--target", str(work), "--id", "cloud-agent", "--runtime-path", str(runtime)],
        root=root,
        name="hub-connector-token-setup",
        env_extra={
            "BRIGADE_HOME": str(work / "fleet-home"),
            "BRIGADE_FLEET_TOKEN": "",
            "BRIGADE_FLEET_NODE_TOKEN": "",
        },
    )
    token_result = token_preview.get("json")
    ok = (
        ok
        and token_preview["returncode"] == 0
        and isinstance(token_result, dict)
        and str(work / "absent-listener") in token_result.get("writes", [])
        and not (work / "absent-listener").exists()
        and not (work / ".brigade").exists()
    )
    return helper.emit(
        {
            "action": "hub-connector",
            "ok": ok,
            "list": helper.slim(listing),
            "preview": helper.slim(preview),
            "token_preview": helper.slim(token_preview),
        },
        0 if ok else 3,
    )
