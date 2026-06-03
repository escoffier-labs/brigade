"""Pantry station commands for agentpantry integration.

These commands deliberately plan and report. They do not generate keys, copy
secret material, mutate auth files, or start services.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import proc


DEFAULT_CONFIG_PATH = "~/.config/agentpantry/config.toml"
DEFAULT_KEY_PATH = "~/.config/agentpantry/psk.key"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_slug(value: str) -> str:
    chars = [ch.lower() if ch.isalnum() else "-" for ch in value.strip()]
    slug = "".join(chars).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug or "plan"


def _json_print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _run_json(args: list[str]) -> dict[str, Any]:
    result = proc.run(args)
    data = result.json()
    return {
        "command": args,
        "exit_code": result.code,
        "stdout_json": data if isinstance(data, dict) else None,
        "stdout_unparsed": None if isinstance(data, dict) else result.stdout[:500],
        "stderr": result.stderr[:500],
    }


def _pantry_root(target: Path) -> Path:
    return target / ".brigade" / "pantry"


def status_payload(target: Path) -> dict[str, Any]:
    installed = proc.which("agentpantry") is not None
    payload: dict[str, Any] = {
        "target": str(target),
        "installed": installed,
        "summary": "agentpantry not installed; run `brigade add pantry`",
        "status": None,
        "doctor": None,
        "advisory": True,
    }
    if not installed:
        return payload

    status_result = _run_json(["agentpantry", "status", "--json"])
    doctor_result = _run_json(["agentpantry", "doctor", "--json"])
    payload["status"] = status_result
    payload["doctor"] = doctor_result

    status_data = status_result.get("stdout_json") or {}
    doctor_data = doctor_result.get("stdout_json") or {}
    if status_result["exit_code"] == 2 or doctor_data.get("configured") is False:
        payload["summary"] = "agentpantry installed but unwired (no config)"
        return payload
    if not isinstance(status_data, dict) or not isinstance(doctor_data, dict):
        payload["summary"] = "agentpantry installed but machine-readable output was incomplete"
        return payload

    fail_count = int(doctor_data.get("fail_count") or 0)
    warn_count = int(doctor_data.get("warn_count") or 0)
    role = status_data.get("role") or doctor_data.get("role") or "?"
    peer = status_data.get("peer") or doctor_data.get("peer") or "?"
    last_sync = status_data.get("last_sync") or "unknown"
    payload["summary"] = (
        f"role={role}, peer={peer}, last_sync={last_sync}, "
        f"checks={fail_count} fail/{warn_count} warn"
    )
    return payload


def status(*, target: Path, json_output: bool = False) -> int:
    payload = status_payload(target)
    if json_output:
        _json_print(payload)
        return 0
    print(f"pantry: {payload['summary']}")
    if not payload["installed"]:
        return 0
    status_data = ((payload.get("status") or {}).get("stdout_json") or {})
    if isinstance(status_data, dict) and status_data:
        print(f"role: {status_data.get('role') or '?'}")
        print(f"peer: {status_data.get('peer') or '?'}")
        print(f"surfaces: {', '.join(status_data.get('surfaces') or []) or 'none'}")
        print(f"last_sync: {status_data.get('last_sync') or 'unknown'}")
        print(f"last_counts: cookies={status_data.get('last_cookies', 0)} secrets={status_data.get('last_secrets', 0)}")
    doctor_data = ((payload.get("doctor") or {}).get("stdout_json") or {})
    if isinstance(doctor_data, dict) and doctor_data.get("checks"):
        print("checks:")
        for row in doctor_data.get("checks") or []:
            if isinstance(row, dict):
                print(f"- {row.get('status')}: {row.get('name')} - {row.get('detail')}")
    return 0


def setup_plan_payload(
    *,
    target: Path,
    role: str,
    peer: str,
    config_path: str,
    key_path: str,
) -> dict[str, Any]:
    commands = [
        ["agentpantry", "init", "--role", role, "--config", config_path],
    ]
    if role == "sink":
        commands.extend(
            [
                ["agentpantry", "keygen", "--out", key_path],
                ["agentpantry", "doctor", "--config", config_path],
                ["agentpantry", "sink", "--config", config_path],
            ]
        )
        manual_steps = [
            "Copy the generated PSK to the source machine over a secure channel.",
            f"Edit {config_path}: set peer to {peer}, choose surfaces/adapters, and keep the sink bind loopback or VPN-scoped.",
        ]
    else:
        commands.extend(
            [
                ["agentpantry", "doctor", "--config", config_path, "--no-net"],
                ["agentpantry", "source", "--config", config_path],
            ]
        )
        manual_steps = [
            "Copy the sink-generated PSK into the configured key path on this source machine.",
            f"Edit {config_path}: set peer to the sink address {peer}, add browser entries, and add explicit domain allow rules.",
        ]
    return {
        "target": str(target),
        "kind": "setup",
        "created_at": _now(),
        "role": role,
        "peer": peer,
        "config_path": config_path,
        "key_path": key_path,
        "commands": commands,
        "manual_steps": manual_steps,
        "boundaries": [
            "Brigade does not generate or copy PSKs.",
            "Brigade does not start agentpantry services.",
            "Brigade does not mutate browser, GitHub, OpenClaw, or other auth files.",
            "Run agentpantry doctor before starting source or sink.",
        ],
    }


def service_plan_payload(*, target: Path, role: str, config_path: str) -> dict[str, Any]:
    return {
        "target": str(target),
        "kind": "service",
        "created_at": _now(),
        "role": role,
        "config_path": config_path,
        "commands": [
            ["agentpantry", "doctor", "--config", config_path],
            ["agentpantry", "install-service", "--config", config_path],
        ],
        "manual_steps": [
            "Review the systemd unit path or Windows Scheduled Task command printed by agentpantry.",
            "Enable/start the service manually only after doctor is clean enough for your threat model.",
        ],
        "boundaries": [
            "Brigade only plans service setup.",
            "Brigade does not run systemctl, schtasks, or background services.",
        ],
    }


def _render_plan_md(payload: dict[str, Any]) -> str:
    lines = [
        f"# agentpantry {payload['kind']} plan",
        "",
        f"- role: {payload.get('role')}",
        f"- config: {payload.get('config_path')}",
    ]
    if payload.get("peer"):
        lines.append(f"- peer: {payload.get('peer')}")
    if payload.get("key_path"):
        lines.append(f"- key: {payload.get('key_path')}")
    lines.extend(["", "## Commands", ""])
    for command in payload.get("commands") or []:
        lines.append("```sh")
        lines.append(" ".join(str(part) for part in command))
        lines.append("```")
        lines.append("")
    lines.extend(["## Manual Steps", ""])
    for step in payload.get("manual_steps") or []:
        lines.append(f"- {step}")
    lines.extend(["", "## Boundaries", ""])
    for boundary in payload.get("boundaries") or []:
        lines.append(f"- {boundary}")
    return "\n".join(lines).rstrip() + "\n"


def _write_plan(target: Path, payload: dict[str, Any]) -> dict[str, Any]:
    created = str(payload.get("created_at") or _now())
    stamp = created.replace(":", "").replace("+", "Z").replace(".", "-")
    plan_id = f"{stamp}-{_safe_slug(str(payload.get('kind')))}-{_safe_slug(str(payload.get('role')))}"
    plan_dir = _pantry_root(target) / "plans" / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    json_path = plan_dir / "plan.json"
    md_path = plan_dir / "PLAN.md"
    payload = dict(payload)
    payload["plan_id"] = plan_id
    payload["plan_path"] = str(md_path)
    payload["receipt_path"] = str(json_path)
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    md_path.write_text(_render_plan_md(payload))
    return payload


def setup_plan(
    *,
    target: Path,
    role: str,
    peer: str,
    config_path: str = DEFAULT_CONFIG_PATH,
    key_path: str = DEFAULT_KEY_PATH,
    write: bool = False,
    json_output: bool = False,
) -> int:
    payload = setup_plan_payload(target=target, role=role, peer=peer, config_path=config_path, key_path=key_path)
    if write:
        payload = _write_plan(target, payload)
    if json_output:
        _json_print(payload)
        return 0
    if write:
        print(f"wrote pantry setup plan: {payload['plan_path']}")
    else:
        print(_render_plan_md(payload), end="")
    return 0


def service_plan(
    *,
    target: Path,
    role: str,
    config_path: str = DEFAULT_CONFIG_PATH,
    write: bool = False,
    json_output: bool = False,
) -> int:
    payload = service_plan_payload(target=target, role=role, config_path=config_path)
    if write:
        payload = _write_plan(target, payload)
    if json_output:
        _json_print(payload)
        return 0
    if write:
        print(f"wrote pantry service plan: {payload['plan_path']}")
    else:
        print(_render_plan_md(payload), end="")
    return 0
