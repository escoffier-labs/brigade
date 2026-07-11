"""Operator notification integration through agent-notify."""

from __future__ import annotations

import json
import os
import re
import stat
import time
from pathlib import Path
from typing import Any

from . import proc, toml_compat as tomllib

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
EVIDENCE_TEXT_LIMIT = 180
EVIDENCE_COMMAND_LIMIT = 3
EVIDENCE_CANDIDATE_LIMIT = 32
EVIDENCE_RECEIPT_BYTE_LIMIT = 256 * 1024


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
    binary = proc.which("agent-notify")
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
            "suggested_next_command": "brigade add notifications",
            "sends_notifications": False,
            "writes_hook_config": False,
            "stores_secrets": False,
        }

    config, config_error = _read_config()
    checks: list[dict[str, Any]] = []
    selected_profile: str | None = profile
    selected_channels: list[str] = []
    missing_env: dict[str, list[str]] = {}
    if config_error:
        checks.append({"status": "WARN", "name": "agent-notify-config", "detail": f"config unreadable: {config_error}"})
    elif config is not None:
        selected_profile, selected_channels, selection_error = _selected_config_channels(config, profile)
        if selection_error:
            checks.append({"status": "WARN", "name": "agent-notify-profile", "detail": selection_error})
        channels_value = config.get("channels")
        channels = channels_value if isinstance(channels_value, dict) else {}
        for name in selected_channels:
            channel = channels.get(name)
            if not isinstance(channel, dict):
                continue
            missing = [env for env in _required_env_names(channel) if not os.environ.get(env)]
            if missing:
                missing_env[name] = missing
    else:
        selected_channels = _env_only_channels()

    if not selected_channels:
        checks.append(
            {
                "status": "WARN",
                "name": "agent-notify-channels",
                "detail": "no configured notification channels selected",
            }
        )
    for name, envs in missing_env.items():
        checks.append(
            {"status": "WARN", "name": "agent-notify-env", "detail": f"{name} missing env: {', '.join(envs)}"}
        )
    if not checks:
        checks.append(
            {"status": "OK", "name": "agent-notify", "detail": f"{len(selected_channels)} local channel(s) configured"}
        )
    configured = (
        bool(selected_channels) and not missing_env and not any(str(check.get("status")) == "WARN" for check in checks)
    )
    status = "ok" if configured else "warn"
    suggested = "brigade notifications setup plan" if not configured else "brigade notifications status --json"
    return {
        "installed": True,
        "binary": binary,
        "config_path": str(CONFIG_PATH),
        "config_exists": config is not None,
        "configured": configured,
        "status": status,
        "checks": checks,
        "selected_profile": selected_profile,
        "selected_channels": selected_channels,
        "missing_env": missing_env,
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
    for path in _receipt_candidates(target, root, "*.json"):
        data = _read_json_object(path)
        if data is not None:
            data.setdefault("path", str(path))
            return [data]
    return []


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


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _receipt_candidates(target: Path, root: Path, pattern: str) -> list[Path]:
    try:
        target = target.resolve(strict=True)
        root = root.resolve(strict=True)
        root.relative_to(target)
    except (OSError, ValueError):
        return []
    candidates: list[tuple[int, str, Path]] = []
    try:
        paths = root.glob(pattern)
        for path in paths:
            try:
                metadata = path.lstat()
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > EVIDENCE_RECEIPT_BYTE_LIMIT:
                    continue
                resolved = path.resolve(strict=True)
                resolved.relative_to(target)
                if not resolved.is_file():
                    continue
            except (OSError, ValueError):
                continue
            candidates.append((metadata.st_mtime_ns, path.as_posix(), path))
    except OSError:
        return []
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [path for _mtime, _name, path in candidates[:EVIDENCE_CANDIDATE_LIMIT]]


def _target_path(target: Path, value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    try:
        return path.resolve().relative_to(target).as_posix()
    except (OSError, ValueError):
        return path.name


def _sanitize_text(target: Path, value: object, *, limit: int = EVIDENCE_TEXT_LIMIT) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    text = " ".join(value.split())
    text = re.sub(r"(/[A-Za-z0-9._~+-]+)+", "[path]", text)
    text = text.replace(str(target), "[path]")
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


def _latest_receipt(target: Path, root: Path, filename: str) -> tuple[Path, dict[str, Any]] | None:
    for path in _receipt_candidates(target, root, f"*/{filename}"):
        data = _read_json_object(path)
        if data is not None:
            return path, data
    return None


def _verify_summary(target: Path, receipt: dict[str, Any], path: Path) -> dict[str, Any]:
    commands: list[dict[str, Any]] = []
    raw_commands = receipt.get("commands")
    command_items: list[Any] = raw_commands if isinstance(raw_commands, list) else []
    for item in command_items:
        if not isinstance(item, dict):
            continue
        command = {
            "command": _sanitize_text(target, item.get("command")),
            "status": item.get("status"),
            "exit_code": item.get("exit_code"),
        }
        stdout = _sanitize_text(target, item.get("stdout_summary"))
        stderr = _sanitize_text(target, item.get("stderr_summary"))
        if stdout:
            command["stdout_summary"] = stdout
        if stderr:
            command["stderr_summary"] = stderr
        commands.append(command)
        if len(commands) >= EVIDENCE_COMMAND_LIMIT:
            break
    return {
        "run_id": receipt.get("run_id"),
        "status": receipt.get("status"),
        "started_at": receipt.get("started_at"),
        "completed_at": receipt.get("completed_at"),
        "path": _target_path(target, str(path)),
        "commands": commands,
        "command_count": len(command_items),
    }


def _run_summary(target: Path, receipt: dict[str, Any], path: Path) -> dict[str, Any]:
    task = receipt.get("task")
    task_text = task.get("text") if isinstance(task, dict) else task
    return {
        "status": receipt.get("status"),
        "task": _sanitize_text(target, task_text),
        "started_at": receipt.get("started_at"),
        "completed_at": receipt.get("completed_at"),
        "path": _target_path(target, str(path)),
        "artifacts": _target_path(target, receipt.get("artifacts")),
    }


def _notification_summary(target: Path, receipt: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": receipt.get("event_id"),
        "event_type": receipt.get("event_type"),
        "created_at": receipt.get("created_at"),
        "sent": receipt.get("sent"),
        "send_exit_code": receipt.get("send_exit_code"),
        "path": _target_path(target, receipt.get("path")),
    }


def _event_evidence(target: Path, *, enabled: bool) -> dict[str, Any]:
    if not enabled:
        return {"attached": False, "disabled": True}
    verify = _latest_receipt(target, target / ".brigade" / "work" / "verify-runs", "receipt.json")
    run = _latest_receipt(target, target / ".brigade" / "runs", "run.json")
    latest_notifications = _event_receipts(target)
    sources = {
        "latest_verify": _verify_summary(target, verify[1], verify[0]) if verify is not None else None,
        "latest_run": _run_summary(target, run[1], run[0]) if run is not None else None,
        "latest_notification": _notification_summary(target, latest_notifications[0]) if latest_notifications else None,
    }
    return {
        "attached": any(value is not None for value in sources.values()),
        "sources": sources,
        "limits": {
            "latest_per_source": 1,
            "command_limit": EVIDENCE_COMMAND_LIMIT,
            "text_limit": EVIDENCE_TEXT_LIMIT,
            "candidate_limit": EVIDENCE_CANDIDATE_LIMIT,
            "receipt_byte_limit": EVIDENCE_RECEIPT_BYTE_LIMIT,
            "raw_logs_read": False,
        },
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
        "notes": [
            "Config references environment variable names only; keep webhook URLs and tokens in the environment.",
            "agent-notify has no init or doctor subcommand; Brigade status inspects local config and env names without sending.",
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
    evidence: bool,
) -> dict[str, Any]:
    created_at = _now()
    event_id = f"{created_at.replace(':', '').replace('+00:00', 'Z')}-{_safe_id(event_type)}"
    argv = [
        "agent-notify",
        "--hook",
        "brigade-event",
        "--event",
        event_type,
        "--level",
        level,
        "--title",
        title,
        "--message",
        message,
    ]
    if profile:
        argv.extend(["--profile", profile])
    if source:
        argv.extend(["--source", source])
    health_payload = health(target, profile=profile)
    return {
        "target": str(target),
        "event_id": event_id,
        "event_type": event_type,
        "level": level,
        "title": title,
        "message": message,
        "source": source,
        "profile": profile,
        "created_at": created_at,
        "planned_argv": argv,
        "send_policy": "explicit-record-send-only",
        "send_requested": send,
        "configured": bool(health_payload.get("configured")),
        "installed": bool(health_payload.get("installed")),
        "selected_channels": health_payload.get("selected_channels") or [],
        "sends_notifications": bool(send),
        "writes_hook_config": False,
        "stores_secrets": False,
        "evidence": _event_evidence(target, enabled=evidence),
    }


def event_plan(
    *,
    target: Path,
    event_type: str,
    title: str,
    message: str,
    level: str = "info",
    profile: str | None = None,
    source: str | None = None,
    evidence: bool = True,
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
        evidence=evidence,
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
    evidence: bool = True,
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
        evidence=evidence,
    )
    result = None
    if send:
        if not payload["installed"] or not payload["configured"]:
            payload["sent"] = False
            payload["send_exit_code"] = 1
            payload["send_error"] = "agent-notify is not installed or configured"
        else:
            result = proc.run(payload["planned_argv"], timeout=30.0, cwd=target)
            payload["sent"] = result.code == 0
            payload["send_exit_code"] = result.code
            payload["stdout_summary"] = " ".join(result.stdout.split())[:400]
            payload["stderr_summary"] = " ".join(result.stderr.split())[:400]
    else:
        payload["sent"] = False
        payload["send_exit_code"] = None
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
