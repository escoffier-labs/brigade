"""Operator notification integration through agent-notify."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, cast

from . import component_bins, proc, toml_compat as tomllib

CHANNEL_ENVS = {
    "discord": ("DISCORD_WEBHOOK_URL",),
    "telegram": ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"),
    "signal": ("SIGNAL_CLI_URL", "SIGNAL_FROM", "SIGNAL_TO"),
}

CONFIG_PATH = Path.home() / ".config" / "agent-notify" / "config.toml"
EVENT_TYPES = (
    "ci-green",
    "ci-failed",
    "handoff-waiting",
    "handoff-ingested",
    "release-ready",
    "operator-alert",
)


def _json(data: dict[str, Any]) -> None:
    print(json.dumps(data, indent=2, sort_keys=True))


def _events_root(target: Path) -> Path:
    return target / ".brigade" / "notifications" / "events"


def _safe_id(value: str) -> str:
    import re

    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip().lower()).strip("-")
    return slug or "event"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())


def _profile_args(profile: str | None) -> list[str]:
    return ["--profile", profile] if profile else []


def _doctor_argv(profile: str | None) -> list[str]:
    binary = component_bins.resolve("agent-notify") or "agent-notify"
    return [binary, "doctor", "--json", "--skip-network", *_profile_args(profile)]


def _failure_class(code: int) -> str | None:
    if code == 0:
        return None
    if code == 2:
        return "configuration_error"
    if code == 3:
        return "delivery_error"
    if code == 124:
        return "timeout"
    if code == 127:
        return "not_found"
    return "child_error"


def _codex_snippet(profile: str | None) -> str:
    parts = ["agent-notify", "--hook", "codex-notify"] + _profile_args(profile)
    rendered = ", ".join(json.dumps(part) for part in parts)
    return f"notify = [{rendered}]"


def _claude_snippet(profile: str | None) -> str:
    profile_args = " ".join(_profile_args(profile))
    suffix = f" {profile_args}" if profile_args else ""
    stop_cmd = f"agent-notify --hook claude-code-stop{suffix}"
    notification_cmd = f"agent-notify --hook claude-code-notification{suffix}"
    return json.dumps(
        {
            "hooks": {
                "Stop": [{"hooks": [{"type": "command", "command": stop_cmd}]}],
                "Notification": [{"hooks": [{"type": "command", "command": notification_cmd}]}],
            }
        },
        indent=2,
    )


def _config_template(profile: str) -> str:
    return "\n".join(
        [
            "[channels.tg-personal]",
            'type = "telegram"',
            'bot_token_env = "TELEGRAM_BOT_TOKEN"',
            'chat_id_env = "TELEGRAM_CHAT_ID"',
            "",
            "[channels.discord-main]",
            'type = "discord"',
            'webhook_url_env = "DISCORD_WEBHOOK_URL"',
            "",
            "[channels.signal-personal]",
            'type = "signal"',
            'url_env  = "SIGNAL_CLI_URL"',
            'from_env = "SIGNAL_FROM"',
            'to_env   = "SIGNAL_TO"',
            "",
            f"[profiles.{profile}]",
            'channels = ["tg-personal", "discord-main"]',
            "default  = true",
            "",
        ]
    )


def _required_env_names(channel: dict[str, Any]) -> list[str]:
    channel_type = str(channel.get("type") or "").casefold()
    if channel_type == "discord":
        return [str(channel.get("webhook_url_env") or "DISCORD_WEBHOOK_URL")]
    if channel_type == "telegram":
        return [
            str(channel.get("bot_token_env") or "TELEGRAM_BOT_TOKEN"),
            str(channel.get("chat_id_env") or "TELEGRAM_CHAT_ID"),
        ]
    if channel_type == "signal":
        return [
            str(channel.get("url_env") or "SIGNAL_CLI_URL"),
            str(channel.get("from_env") or "SIGNAL_FROM"),
            str(channel.get("to_env") or "SIGNAL_TO"),
        ]
    return []


def _env_only_channels() -> list[str]:
    selected: list[str] = []
    for name, envs in CHANNEL_ENVS.items():
        if all(os.environ.get(env) for env in envs):
            selected.append(name)
    return selected


def _read_config(path: Path | None = None) -> tuple[dict[str, Any] | None, str | None]:
    path = path or CONFIG_PATH
    if not path.is_file():
        return None, None
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return None, str(exc)
    return (data if isinstance(data, dict) else None), None


def _selected_config_channels(config: dict[str, Any], profile: str | None) -> tuple[str | None, list[str], str | None]:
    channels_value = config.get("channels")
    channels = channels_value if isinstance(channels_value, dict) else {}
    profiles_value = config.get("profiles")
    profiles = profiles_value if isinstance(profiles_value, dict) else {}
    if profile:
        selected = profiles.get(profile)
        if not isinstance(selected, dict):
            return profile, [], f"profile {profile!r} not found in config"
        names_value = selected.get("channels")
        names = names_value if isinstance(names_value, list) else []
        return profile, [str(name) for name in names if str(name) in channels], None
    for name, candidate in profiles.items():
        if isinstance(candidate, dict) and candidate.get("default") is True:
            names_value = candidate.get("channels")
            names = names_value if isinstance(names_value, list) else []
            return str(name), [str(item) for item in names if str(item) in channels], None
    return None, sorted(str(name) for name in channels), None


def _checks_from_status(payload: dict[str, Any]) -> list[dict[str, Any]]:
    checks = payload.get("checks")
    if isinstance(checks, list):
        return [check for check in checks if isinstance(check, dict)]
    status = str(payload.get("status") or "warn")
    if status == "ok":
        detail = "agent-notify configured"
    elif not payload.get("installed"):
        detail = "agent-notify not installed"
    elif not payload.get("configured"):
        detail = "agent-notify installed but unwired"
    else:
        detail = "agent-notify has warning state"
    return [{"status": status.upper(), "name": "agent-notify", "detail": detail}]


def _status_payload(profile: str | None = None) -> dict[str, Any]:
    binary = component_bins.resolve("agent-notify")
    if binary is None:
        return {
            "installed": False,
            "configured": False,
            "status": "manual",
            "checks": [
                {
                    "status": "MANUAL",
                    "name": "agent-notify",
                    "detail": "not installed; run `brigade add notifications`",
                }
            ],
            "probe_exit_code": 127,
            "probe_failure_class": "not_found",
            "suggested_next_command": "brigade add notifications",
            "sends_notifications": False,
            "writes_hook_config": False,
            "stores_secrets": False,
        }

    result = proc.run(_doctor_argv(profile), timeout=30.0)
    doctor = result.json() if result.code == 0 else None
    valid_doctor = isinstance(doctor, dict)
    doctor_payload = cast(dict[str, Any], doctor) if valid_doctor else {}
    configured = doctor_payload.get("configured") is True
    failure_class = _failure_class(result.code)
    if result.code == 0 and not configured:
        failure_class = "child_error"

    selected_profile = profile
    selected_channels: list[str] = []
    fail_count = 0
    warn_count = 0
    config_exists: bool | None = None
    if valid_doctor:
        doctor_profile = doctor_payload.get("selected_profile")
        if isinstance(doctor_profile, str) and doctor_profile:
            selected_profile = doctor_profile
        channels_value = doctor_payload.get("selected_channels")
        if isinstance(channels_value, list):
            selected_channels = [item for item in channels_value if isinstance(item, str)]
        if type(doctor_payload.get("fail_count")) is int:
            fail_count = doctor_payload["fail_count"]
        if type(doctor_payload.get("warn_count")) is int:
            warn_count = doctor_payload["warn_count"]
        if isinstance(doctor_payload.get("config_file_exists"), bool):
            config_exists = doctor_payload["config_file_exists"]

    if configured:
        checks: list[dict[str, Any]] = [
            {
                "status": "OK",
                "name": "agent-notify-doctor",
                "detail": f"{len(selected_channels)} local channel(s) configured",
            }
        ]
    else:
        detail = f"doctor exited {result.code} ({failure_class})"
        checks = [{"status": "WARN", "name": "agent-notify-doctor", "detail": detail}]
    status = "ok" if configured else "warn"
    suggested = "brigade notifications setup plan" if not configured else "brigade notifications status --json"
    return {
        "installed": True,
        "binary": binary,
        "config_path": str(CONFIG_PATH),
        "config_exists": config_exists,
        "configured": configured,
        "status": status,
        "checks": checks,
        "selected_profile": selected_profile,
        "selected_channels": selected_channels,
        "missing_env": {},
        "doctor_fail_count": fail_count,
        "doctor_warn_count": warn_count,
        "probe_exit_code": result.code,
        "probe_failure_class": failure_class,
        "suggested_next_command": suggested,
        "sends_notifications": False,
        "writes_hook_config": False,
        "stores_secrets": False,
    }


def health(target: Path, profile: str | None = None) -> dict[str, Any]:
    target = target.expanduser().resolve()
    payload = _status_payload(profile)
    checks = _checks_from_status(payload)
    issue_checks = [check for check in checks if str(check.get("status") or "").lower() not in {"ok", "manual"}]
    if payload.get("status") == "manual":
        issue_checks = checks
    top_issue = issue_checks[0] if issue_checks else None
    channels_value = payload.get("selected_channels")
    channels = channels_value if isinstance(channels_value, list) else []
    return {
        "installed": bool(payload.get("installed")),
        "configured": bool(payload.get("configured")),
        "status": payload.get("status"),
        "profile": payload.get("selected_profile") if payload.get("selected_profile") else profile,
        "selected_channel_count": len(channels),
        "selected_channels": channels,
        "config_path": payload.get("config_path"),
        "config_exists": payload.get("config_exists"),
        "missing_env": payload.get("missing_env"),
        "checks": checks,
        "issue_count": len(issue_checks),
        "top_issue": top_issue,
        "suggested_next_command": payload.get("suggested_next_command") or "brigade notifications status",
        "sends_notifications": False,
        "writes_hook_config": False,
        "stores_secrets": False,
        "latest_event": _latest_event_summary(target),
    }


def _event_receipts(target: Path) -> list[dict[str, Any]]:
    root = _events_root(target)
    if not root.is_dir():
        return []
    receipts: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            data.setdefault("path", str(path))
            receipts.append(data)
    receipts.sort(key=lambda item: str(item.get("created_at") or item.get("event_id") or ""), reverse=True)
    return receipts


def _latest_event_summary(target: Path) -> dict[str, Any] | None:
    receipts = _event_receipts(target)
    if not receipts:
        return None
    latest = receipts[0]
    return {
        "event_id": latest.get("event_id"),
        "event_type": latest.get("event_type"),
        "created_at": latest.get("created_at"),
        "sent": latest.get("sent"),
        "send_exit_code": latest.get("send_exit_code"),
        "path": latest.get("path"),
    }


def status(*, target: Path, profile: str | None = None, json_output: bool = False) -> int:
    del target
    payload = _status_payload(profile)
    if json_output:
        _json(payload)
        return 0
    print("notifications status")
    print(f"installed:  {payload.get('installed')}")
    print(f"configured: {payload.get('configured')}")
    print(f"status:     {payload.get('status')}")
    if payload.get("binary"):
        print(f"binary:     {payload['binary']}")
    if payload.get("config_path"):
        print(f"config:     {payload['config_path']} exists={payload.get('config_exists')}")
    channels_value = payload.get("selected_channels")
    channels = channels_value if isinstance(channels_value, list) else []
    print(f"channels:   {len(channels)}")
    print(f"next:       {payload.get('suggested_next_command')}")
    return 0


def setup_plan(*, target: Path, profile: str = "operator", json_output: bool = False) -> int:
    del target
    status_payload = _status_payload(profile)
    payload: dict[str, Any] = {
        "profile": profile,
        "config_path": str(CONFIG_PATH),
        "commands": {
            "install": "brigade add notifications",
            "create_config_dir": f"mkdir -p {CONFIG_PATH.parent}",
            "status": "brigade notifications status --json",
        },
        "config_toml": _config_template(profile),
        "snippets": {
            "codex_config_toml": _codex_snippet(profile),
            "claude_code_settings_json": _claude_snippet(profile),
        },
        "doctor_probe": {
            "configured": status_payload["configured"],
            "probe_exit_code": status_payload.get("probe_exit_code"),
            "probe_failure_class": status_payload.get("probe_failure_class"),
        },
        "notes": [
            "Config references environment variable names only; keep webhook URLs and tokens in the environment.",
            "Brigade checks Agent Notify with `agent-notify doctor --json --skip-network`; the probe never sends.",
            "Brigade does not write harness notification hooks or send test notifications from setup plan.",
        ],
    }
    if json_output:
        _json(payload)
        return 0
    print("notifications setup plan")
    print()
    print("Install and initialize:")
    print(f"  {payload['commands']['install']}")
    print(f"  {payload['commands']['create_config_dir']}")
    print(f"  {payload['commands']['status']}")
    print()
    print(f"{payload['config_path']}:")
    print(payload["config_toml"])
    print()
    print("Codex ~/.codex/config.toml:")
    print(payload["snippets"]["codex_config_toml"])
    print()
    print("Claude Code ~/.claude/settings.json:")
    print(payload["snippets"]["claude_code_settings_json"])
    return 0


def _event_payload(
    target: Path,
    *,
    event_type: str,
    title: str,
    message: str,
    level: str,
    profile: str | None,
    source: str | None,
    send: bool,
) -> dict[str, Any]:
    created_at = _now()
    event_id = f"{created_at.replace(':', '').replace('+00:00', 'Z')}-{_safe_id(event_type)}"
    health_payload = health(target, profile=profile)
    selected_profile = health_payload.get("profile")
    effective_profile = selected_profile if isinstance(selected_profile, str) else profile
    argv = list(component_bins.resolve_argv(["agent-notify", "send", *_profile_args(effective_profile)]))
    return {
        "target": str(target),
        "event_id": event_id,
        "event_type": event_type,
        "level": level,
        "title": title,
        "message": message,
        "source": source,
        "profile": effective_profile,
        "created_at": created_at,
        "planned_argv": argv,
        "send_requested": send,
        "configured": bool(health_payload.get("configured")),
        "installed": bool(health_payload.get("installed")),
        "selected_channels": health_payload.get("selected_channels") or [],
        "sends_notifications": bool(send),
        "writes_hook_config": False,
        "stores_secrets": False,
    }


def _canonical_event_bytes(payload: dict[str, Any]) -> bytes:
    level = "warn" if payload["level"] == "warning" else payload["level"]
    canonical = {
        "body": payload["message"],
        "level": level,
        "source": payload.get("source") or "brigade",
        "tags": [payload["event_type"]],
        "title": payload["title"],
    }
    rendered = json.dumps(canonical, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return (rendered + "\n").encode()


def event_plan(
    *,
    target: Path,
    event_type: str,
    title: str,
    message: str,
    level: str = "info",
    profile: str | None = None,
    source: str | None = None,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}")
        return 2
    payload = _event_payload(
        target,
        event_type=event_type,
        title=title,
        message=message,
        level=level,
        profile=profile,
        source=source,
        send=False,
    )
    payload["would_write"] = False
    payload["receipt_path"] = str(_events_root(target) / f"{payload['event_id']}.json")
    if json_output:
        _json(payload)
        return 0
    print("notifications event plan")
    print(f"event_id: {payload['event_id']}")
    print(f"event_type: {event_type}")
    print(f"configured: {payload['configured']}")
    print("would_write: false")
    print(f"receipt: {payload['receipt_path']}")
    print("planned_argv: " + " ".join(payload["planned_argv"]))
    return 0


def event_record(
    *,
    target: Path,
    event_type: str,
    title: str,
    message: str,
    level: str = "info",
    profile: str | None = None,
    source: str | None = None,
    send: bool = False,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}")
        return 2
    payload = _event_payload(
        target,
        event_type=event_type,
        title=title,
        message=message,
        level=level,
        profile=profile,
        source=source,
        send=send,
    )
    result = None
    if send:
        if not payload["installed"]:
            payload["sent"] = False
            payload["send_exit_code"] = 127
            payload["send_failure_class"] = _failure_class(127)
        elif not payload["configured"]:
            payload["sent"] = False
            payload["send_exit_code"] = 2
            payload["send_failure_class"] = _failure_class(2)
        else:
            result = proc.run(
                payload["planned_argv"],
                timeout=30.0,
                cwd=target,
                stdin=_canonical_event_bytes(payload),
            )
            payload["sent"] = result.code == 0
            payload["send_exit_code"] = result.code
            payload["send_failure_class"] = _failure_class(result.code)
    else:
        payload["sent"] = False
        payload["send_exit_code"] = None
        payload["send_failure_class"] = None
    path = _events_root(target) / f"{payload['event_id']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload["path"] = str(path)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    if json_output:
        _json(payload)
        return 0 if not send or payload["sent"] else 1
    print("notifications event record")
    print(f"event_id: {payload['event_id']}")
    print(f"event_type: {event_type}")
    print(f"receipt: {path}")
    print(f"sent: {payload['sent']}")
    if send:
        print(f"send_exit_code: {payload['send_exit_code']}")
    return 0 if not send or payload["sent"] else 1
