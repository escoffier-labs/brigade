"""Pantry station commands for agentpantry integration.

These commands deliberately plan and report. They do not generate keys, copy
secret material, mutate auth files, or start services.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import proc
from .localio import utc_now_iso as _now


DEFAULT_CONFIG_PATH = "~/.config/agentpantry/config.toml"
DEFAULT_KEY_PATH = "~/.config/agentpantry/psk.key"


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
        "health": "missing",
        "summary": "agentpantry not installed; run `brigade add pantry`",
        "status": None,
        "doctor": None,
        "advisory": True,
        "next_commands": [
            "brigade add pantry",
            "brigade pantry setup plan --role sink --peer 127.0.0.1:8787",
            "brigade pantry doctor",
        ],
        "docs": {
            "product": "https://brigade.tools/agentpantry",
            "repo": "https://github.com/escoffier-labs/agentpantry",
        },
        "boundaries": [
            "Agent Pantry stays a process-boundary Go binary; Brigade only installs, plans, and health-checks.",
            "Brigade does not generate PSKs, start source/sink, or mutate browser auth files.",
        ],
    }
    if not installed:
        return payload

    # Before invoking any installed agentpantry status/doctor surface, probe
    # the version and enforce the evidence-backed floor
    # (AGENTPANTRY_MIN_VERSION = 0.5.0). v0.5.0 is the first release exposing
    # every Brigade-invoked surface; v0.4.1 lacks inventory. On
    # incompatibility Brigade must not invoke status/doctor and must report
    # unhealthy rather than ``pantry: ok``. See brigade.pantry_compat.
    from . import pantry_compat

    probe = pantry_compat.probe_agentpantry_version()
    payload["version"] = probe.observed
    payload["version_compatible"] = probe.compatible
    if probe.incompatible:
        payload["health"] = "unhealthy"
        payload["summary"] = probe.detail
        payload["next_commands"] = [
            "brigade add pantry",
            "agentpantry version",
            "brigade pantry doctor",
        ]
        return payload

    status_result = _run_json(["agentpantry", "status", "--json"])
    doctor_result = _run_json(["agentpantry", "doctor", "--json"])
    payload["status"] = status_result
    payload["doctor"] = doctor_result

    status_data = status_result.get("stdout_json") or {}
    doctor_data = doctor_result.get("stdout_json") or {}
    if status_result["exit_code"] == 2 or doctor_data.get("configured") is False:
        payload["health"] = "unwired"
        payload["summary"] = "agentpantry installed but unwired (no config)"
        payload["next_commands"] = [
            "brigade pantry setup plan --role sink --peer 127.0.0.1:8787",
            "brigade pantry setup plan --role source --peer <sink-host>:8787",
            "brigade pantry doctor",
        ]
        return payload
    if not isinstance(status_data, dict) or not isinstance(doctor_data, dict):
        payload["health"] = "incomplete"
        payload["summary"] = "agentpantry installed but machine-readable output was incomplete"
        payload["next_commands"] = ["agentpantry doctor", "brigade pantry doctor"]
        return payload

    fail_count = int(doctor_data.get("fail_count") or 0)
    warn_count = int(doctor_data.get("warn_count") or 0)
    role = status_data.get("role") or doctor_data.get("role") or "?"
    peer = status_data.get("peer") or doctor_data.get("peer") or "?"
    last_sync = status_data.get("last_sync") or "unknown"
    if fail_count:
        payload["health"] = "fail"
    elif warn_count:
        payload["health"] = "warn"
    else:
        payload["health"] = "ok"
    payload["summary"] = f"role={role}, peer={peer}, last_sync={last_sync}, checks={fail_count} fail/{warn_count} warn"
    payload["next_commands"] = [
        "brigade pantry expiry-alert",
        "brigade pantry service plan --role " + str(role),
        "agentpantry doctor",
    ]
    if fail_count:
        payload["next_commands"] = [
            "agentpantry doctor",
            f"brigade pantry setup plan --role {role}",
            "brigade pantry service plan --role " + str(role),
        ]
    return payload


def _print_next_commands(payload: dict[str, Any]) -> None:
    next_commands = payload.get("next_commands") or []
    if not next_commands:
        return
    print("next:")
    for command in next_commands:
        print(f"  {command}")


def status(*, target: Path, json_output: bool = False) -> int:
    payload = status_payload(target)
    if json_output:
        _json_print(payload)
        return 0
    print(f"pantry: {payload['summary']}")
    print(f"health: {payload.get('health') or 'unknown'} (advisory; never fails workspace doctor)")
    if not payload["installed"]:
        _print_next_commands(payload)
        return 0
    status_data = (payload.get("status") or {}).get("stdout_json") or {}
    if isinstance(status_data, dict) and status_data:
        print(f"role: {status_data.get('role') or '?'}")
        print(f"peer: {status_data.get('peer') or '?'}")
        print(f"surfaces: {', '.join(str(s) for s in (status_data.get('surfaces') or [])) or 'none'}")
        print(f"last_sync: {status_data.get('last_sync') or 'unknown'}")
        print(f"last_counts: cookies={status_data.get('last_cookies', 0)} secrets={status_data.get('last_secrets', 0)}")
    doctor_data = (payload.get("doctor") or {}).get("stdout_json") or {}
    if isinstance(doctor_data, dict) and doctor_data.get("checks"):
        print("checks:")
        for row in doctor_data.get("checks") or []:
            if isinstance(row, dict):
                print(f"- {row.get('status')}: {row.get('name')} - {row.get('detail')}")
    _print_next_commands(payload)
    return 0


def doctor(*, target: Path, json_output: bool = False) -> int:
    """Advisory pantry health with a nonzero exit only on agentpantry doctor fails."""
    payload = status_payload(target)
    payload["command"] = "pantry doctor"
    if json_output:
        _json_print(payload)
    else:
        print(f"pantry doctor: {payload['summary']}")
        print(f"health: {payload.get('health') or 'unknown'}")
        doctor_data = (payload.get("doctor") or {}).get("stdout_json") or {}
        if isinstance(doctor_data, dict) and doctor_data.get("checks"):
            print("checks:")
            for row in doctor_data.get("checks") or []:
                if isinstance(row, dict):
                    print(f"- {row.get('status')}: {row.get('name')} - {row.get('detail')}")
        _print_next_commands(payload)
        print(
            "note: pantry checks are advisory for workspace doctor; "
            "status 1 occurs for unhealthy, incomplete, or nonzero agentpantry fail_count"
        )
    health = payload.get("health")
    if health == "fail":
        return 1
    if health == "incomplete":
        return 1
    if health == "unhealthy":
        return 1
    return 0


def _near_expiry_message(near: list[dict[str, Any]], *, expiry_days: int) -> str:
    lines = [f"Agent Pantry: {len(near)} session cookie(s) expire within {expiry_days} day(s)."]
    for item in near[:10]:
        lines.append(f"- {item.get('host')}/{item.get('name')} expires {item.get('expires')}")
    if len(near) > 10:
        lines.append(f"- plus {len(near) - 10} more")
    return "\n".join(lines)


def expiry_alert_payload(*, expiry_days: int = 14, profile: str = "agent-stop", send: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "created_at": _now(),
        "expiry_days": expiry_days,
        "profile": profile,
        "send": send,
        "agentpantry_installed": proc.which("agentpantry") is not None,
        "agent_notify_installed": proc.which("agent-notify") is not None,
        "near_expiry_count": 0,
        "message": "",
        "sent": False,
    }
    if expiry_days < 0:
        payload["error"] = "expiry_days must not be negative"
        return payload
    if not payload["agentpantry_installed"]:
        payload["error"] = "agentpantry is not installed"
        payload["next_commands"] = ["brigade add pantry", "brigade pantry doctor"]
        return payload

    # Before invoking the installed agentpantry inventory surface, probe the
    # version and enforce the evidence-backed floor. On incompatibility do not
    # invoke inventory; return this command's structured failure path with a
    # precise version compatibility error. See brigade.pantry_compat.
    from . import pantry_compat

    probe = pantry_compat.probe_agentpantry_version()
    payload["version"] = probe.observed
    payload["version_compatible"] = probe.compatible
    if probe.incompatible:
        payload["error"] = probe.detail
        payload["next_commands"] = ["brigade add pantry", "agentpantry version", "brigade pantry doctor"]
        return payload

    inventory = _run_json(["agentpantry", "inventory", "--json", "--expiry-days", str(expiry_days)])
    payload["inventory"] = inventory
    data = inventory.get("stdout_json") or {}
    near = data.get("near_expiry") if isinstance(data, dict) else []
    near_items = [item for item in near if isinstance(item, dict)] if isinstance(near, list) else []
    payload["near_expiry_count"] = len(near_items)
    if inventory.get("exit_code") != 0:
        payload["error"] = "agentpantry inventory failed"
        payload["next_commands"] = ["agentpantry doctor", "brigade pantry doctor"]
        return payload
    if not near_items:
        payload["summary"] = "no near-expiry sessions"
        payload["next_commands"] = ["brigade pantry status", "brigade pantry doctor"]
        return payload

    message = _near_expiry_message(near_items, expiry_days=expiry_days)
    payload["message"] = message
    payload["summary"] = f"{len(near_items)} near-expiry session(s)"
    payload["planned_argv"] = ["agent-notify", "send", "--profile", profile, message]
    if not send:
        payload["next_commands"] = [
            "brigade pantry expiry-alert --send",
            "brigade add notifications",
        ]
        return payload
    if not payload["agent_notify_installed"]:
        payload["error"] = "agent-notify is not installed"
        payload["next_commands"] = [
            "brigade add notifications",
            "brigade notifications setup plan",
            "brigade pantry expiry-alert --send",
        ]
        return payload
    result = proc.run(["agent-notify", "send", "--profile", profile, message], timeout=15.0)
    payload["notify_exit_code"] = result.code
    payload["notify_stderr"] = result.stderr[:500]
    payload["sent"] = result.code == 0
    if result.code != 0:
        payload["error"] = "agent-notify send failed"
        payload["next_commands"] = ["agent-notify doctor", "brigade notifications status"]
    else:
        payload["next_commands"] = ["brigade pantry status"]
    return payload


def expiry_alert(
    *, expiry_days: int = 14, profile: str = "agent-stop", send: bool = False, json_output: bool = False
) -> int:
    payload = expiry_alert_payload(expiry_days=expiry_days, profile=profile, send=send)
    if json_output:
        _json_print(payload)
        return 0 if not payload.get("error") or payload.get("near_expiry_count") == 0 else 1
    print(f"pantry expiry alert: {payload.get('summary') or payload.get('error') or 'ready'}")
    if payload.get("message"):
        print(payload["message"])
    if payload.get("planned_argv") and not payload.get("send"):
        print("send: false (preview only; nothing left the machine)")
    _print_next_commands(payload)
    return 0 if not payload.get("error") or payload.get("near_expiry_count") == 0 else 1


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
            "Agent Pantry remains a separate Go binary (process boundary); this plan only documents the operator path.",
        ],
        "next_commands": [
            "Review the commands below, then run them yourself.",
            "brigade pantry doctor",
            "brigade pantry expiry-alert",
        ],
        "docs": {
            "product": "https://brigade.tools/agentpantry",
            "repo": "https://github.com/escoffier-labs/agentpantry",
        },
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
    docs_raw = payload.get("docs")
    docs: dict[str, Any] = docs_raw if isinstance(docs_raw, dict) else {}
    if docs.get("product"):
        lines.append(f"- product: {docs['product']}")
    if docs.get("repo"):
        lines.append(f"- repo: {docs['repo']}")
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
    next_commands = payload.get("next_commands") or []
    if next_commands:
        lines.extend(["", "## Next", ""])
        for command in next_commands:
            lines.append(f"- {command}")
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
