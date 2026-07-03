"""Runtime helpers and commands for the tools command family."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from ..render import emit
from . import config, constants, helpers, issues as issues_mod, paths, safety


def _facade(name: str) -> Any:
    return getattr(sys.modules[__package__], name)


def _find_runtime(target: Path, runtime_id: str) -> tuple[dict[str, Any] | None, list[str]]:
    runtimes, errors = config._load_runtime_config(target)
    for runtime in runtimes:
        if runtime.get("enabled", True) and runtime.get("id") == runtime_id:
            return runtime, errors
    if not errors:
        errors.append(f"runtime not found: {runtime_id}")
    return None, errors


def _runtime_file(target: Path, runtime: dict[str, Any], field: str, default_suffix: str) -> Path:
    runtime_id = str(runtime.get("id") or "runtime")
    configured = runtime.get(field)
    if isinstance(configured, str) and configured.strip():
        return helpers._as_path(target, configured) or (
            paths.runtime_state_path(target) / f"{runtime_id}{default_suffix}"
        )
    return paths.runtime_state_path(target) / f"{runtime_id}{default_suffix}"


def _runtime_pid_path(target: Path, runtime: dict[str, Any]) -> Path:
    return _runtime_file(target, runtime, "pid_path", ".pid")


def _runtime_metadata_path(target: Path, runtime: dict[str, Any]) -> Path:
    return paths.runtime_state_path(target) / f"{runtime.get('id')}.json"


def _runtime_health_path(target: Path, runtime: dict[str, Any]) -> Path | None:
    value = runtime.get("health_path")
    return helpers._as_path(target, value) if value else None


def _runtime_log_paths(target: Path, runtime: dict[str, Any]) -> tuple[Path, Path]:
    runtime_id = str(runtime.get("id") or "runtime")
    configured = runtime.get("log_path")
    base = (
        helpers._as_path(target, configured) if configured else paths.runtime_state_path(target) / f"{runtime_id}.log"
    )
    assert base is not None
    return base.with_suffix(base.suffix + ".stdout"), base.with_suffix(base.suffix + ".stderr")


def _read_pid(path: Path) -> int | None:
    try:
        raw = path.read_text().strip()
    except OSError:
        return None
    try:
        pid = int(raw)
    except ValueError:
        return None
    return pid if pid > 0 else None


def _process_alive(pid: int | None) -> bool:
    if pid is None:
        return False
    stat_path = Path(f"/proc/{pid}/stat")
    if stat_path.is_file():
        try:
            parts = stat_path.read_text().split()
        except OSError:
            parts = []
        if len(parts) > 2 and parts[2] == "Z":
            return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _read_runtime_metadata(target: Path, runtime: dict[str, Any]) -> dict[str, Any] | None:
    path = _runtime_metadata_path(target, runtime)
    payload, error = helpers._read_json(path) if path.is_file() else (None, None)
    if error is not None or not isinstance(payload, dict):
        return None
    return payload


def _write_runtime_metadata(target: Path, runtime: dict[str, Any], metadata: dict[str, Any]) -> None:
    path = _runtime_metadata_path(target, runtime)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")


def _port_in_use(port: object) -> bool:
    if not isinstance(port, int):
        return False
    loopback = ".".join(("127", "0", "0", "1"))
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.1)
        return sock.connect_ex((loopback, port)) == 0


def _runtime_cwd(target: Path, runtime: dict[str, Any]) -> Path:
    cwd = helpers._as_path(target, runtime.get("cwd"))
    return cwd or target


def _runtime_status_item(target: Path, runtime: dict[str, Any], *, run_health: bool = True) -> dict[str, Any]:
    pid_path = _runtime_pid_path(target, runtime)
    metadata_path = _runtime_metadata_path(target, runtime)
    stdout_path, stderr_path = _runtime_log_paths(target, runtime)
    pid = _read_pid(pid_path)
    alive = _process_alive(pid)
    stale_pid = pid is not None and not alive
    metadata = _read_runtime_metadata(target, runtime)
    managed = bool(metadata and metadata.get("runtime_id") == runtime.get("id") and metadata.get("pid") == pid)
    cwd = _runtime_cwd(target, runtime)
    issues: list[dict[str, Any]] = []
    if safety._high_risk_command(runtime.get("command")):
        issues.append(
            issues_mod._tool_issue(
                {"id": runtime.get("id"), "family": "runtime"},
                "runtime_high_risk_command",
                "runtime command shape is high risk",
            )
        )
    if not safety._command_parts(runtime.get("command")):
        issues.append(
            issues_mod._tool_issue(
                {"id": runtime.get("id"), "family": "runtime"},
                "runtime_bad_command",
                "runtime command could not be parsed",
            )
        )
    if not cwd.is_dir():
        issues.append(
            issues_mod._tool_issue(
                {"id": runtime.get("id"), "family": "runtime"}, "runtime_missing_cwd", f"runtime cwd missing: {cwd}"
            )
        )
    if stale_pid:
        issues.append(
            issues_mod._tool_issue(
                {"id": runtime.get("id"), "family": "runtime"}, "runtime_stale_pid", f"stale pid file: {pid_path}"
            )
        )
    if isinstance(runtime.get("port"), int) and _port_in_use(runtime["port"]) and not alive:
        issues.append(
            issues_mod._tool_issue(
                {"id": runtime.get("id"), "family": "runtime"},
                "runtime_port_conflict",
                f"port is already in use: {runtime['port']}",
            )
        )
    health_path = _runtime_health_path(target, runtime)
    health_ok = True
    health_detail = "not configured"
    if alive and health_path is not None:
        if health_path.exists():
            health_detail = f"health path present: {health_path}"
        else:
            health_ok = False
            health_detail = f"health path missing: {health_path}"
            issues.append(
                issues_mod._tool_issue(
                    {"id": runtime.get("id"), "family": "runtime"}, "runtime_health_failed", health_detail
                )
            )
    health_command = runtime.get("health_command")
    if alive and run_health and isinstance(health_command, str) and health_command.strip():
        parts = safety._command_parts(health_command)
        if not parts:
            health_ok = False
            health_detail = "health command could not be parsed"
            issues.append(
                issues_mod._tool_issue(
                    {"id": runtime.get("id"), "family": "runtime"}, "runtime_health_failed", health_detail
                )
            )
        elif safety._high_risk_command(health_command):
            health_ok = False
            health_detail = "health command shape is high risk"
            issues.append(
                issues_mod._tool_issue(
                    {"id": runtime.get("id"), "family": "runtime"}, "runtime_health_failed", health_detail
                )
            )
        else:
            try:
                completed = subprocess.run(
                    parts,
                    cwd=cwd if cwd.is_dir() else target,
                    text=True,
                    capture_output=True,
                    timeout=float(runtime.get("timeout") or 5),
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                health_ok = False
                health_detail = f"health command failed: {helpers._short(str(exc))}"
                issues.append(
                    issues_mod._tool_issue(
                        {"id": runtime.get("id"), "family": "runtime"}, "runtime_health_failed", health_detail
                    )
                )
            else:
                health_ok = completed.returncode == 0
                health_detail = f"health command exit_code={completed.returncode}"
                if completed.returncode != 0:
                    issues.append(
                        issues_mod._tool_issue(
                            {"id": runtime.get("id"), "family": "runtime"}, "runtime_health_failed", health_detail
                        )
                    )
    state = "running" if alive else ("stale" if stale_pid else "stopped")
    return {
        "id": runtime.get("id"),
        "name": runtime.get("name"),
        "enabled": runtime.get("enabled", True),
        "command": runtime.get("command"),
        "cwd": str(cwd),
        "port": runtime.get("port"),
        "pid": pid,
        "state": state,
        "running": alive,
        "managed": managed,
        "stale_pid": stale_pid,
        "pid_path": str(pid_path),
        "metadata_path": str(metadata_path),
        "stdout_log_path": str(stdout_path),
        "stderr_log_path": str(stderr_path),
        "health_path": str(health_path) if health_path is not None else None,
        "health_ok": health_ok if alive else None,
        "health_detail": health_detail,
        "metadata": metadata,
        "issues": issues,
        "issue_count": len(issues),
    }


def _runtime_payload(target: Path, runtime_id: str | None = None, *, run_health: bool = True) -> dict[str, Any]:
    target = target.expanduser().resolve()
    runtimes, errors = config._load_runtime_config(target)
    if runtime_id is not None:
        runtimes = [runtime for runtime in runtimes if runtime.get("id") == runtime_id]
        if not runtimes and not errors:
            errors.append(f"runtime not found: {runtime_id}")
    statuses = [
        _runtime_status_item(target, runtime, run_health=run_health)
        for runtime in runtimes
        if runtime.get("enabled", True)
    ]
    issues = [issue for item in statuses for issue in item.get("issues", [])]
    if errors:
        issues.insert(
            0,
            {
                "status": constants.WARN,
                "name": "runtime_config",
                "issue_type": "runtime_config",
                "detail": "; ".join(errors),
            },
        )
    counts: dict[str, int] = {}
    for item in statuses:
        state = str(item.get("state") or "unknown")
        counts[state] = counts.get(state, 0) + 1
    return {
        "target": str(target),
        "config_path": str(paths.runtimes_config_path(target)),
        "state_path": str(paths.runtime_state_path(target)),
        "valid": not errors,
        "errors": errors,
        "runtimes": statuses,
        "runtime_count": len(statuses),
        "counts": counts,
        "issues": issues,
        "issue_count": len(issues),
        "top_issue": issues[0] if issues else None,
    }


def _tool_runtime_issues(
    target: Path, tools: list[dict[str, Any]], runtime_payload: dict[str, Any]
) -> list[dict[str, Any]]:
    runtimes_by_id = {str(item.get("id")): item for item in runtime_payload.get("runtimes", []) if item.get("id")}
    issues: list[dict[str, Any]] = []
    for tool in tools:
        if not tool.get("enabled", True):
            continue
        runtime_id = tool.get("runtime_id")
        requires_runtime = bool(tool.get("requires_runtime"))
        if requires_runtime and (not isinstance(runtime_id, str) or not runtime_id.strip()):
            issues.append(
                issues_mod._tool_issue(tool, "runtime_missing", "tool requires a runtime but runtime_id is missing")
            )
            continue
        if not isinstance(runtime_id, str) or not runtime_id.strip():
            continue
        runtime = runtimes_by_id.get(runtime_id)
        if runtime is None:
            issues.append(
                issues_mod._tool_issue(tool, "runtime_missing", f"tool runtime is not configured: {runtime_id}")
            )
            continue
        if requires_runtime and not runtime.get("running"):
            issues.append(
                issues_mod._tool_issue(tool, "runtime_stopped", f"required runtime is not running: {runtime_id}")
            )
        if requires_runtime and runtime.get("health_ok") is False:
            issues.append(
                issues_mod._tool_issue(tool, "runtime_unhealthy", f"required runtime is unhealthy: {runtime_id}")
            )
    return issues


def _start_runtime_payload(target: Path, runtime_id: str) -> tuple[dict[str, Any], int]:
    target = target.expanduser().resolve()
    runtime, errors = _find_runtime(target, runtime_id)
    if runtime is None:
        return {"target": str(target), "error": "; ".join(errors)}, 1
    status = _runtime_status_item(target, runtime, run_health=False)
    blockers: list[str] = []
    if status.get("running"):
        return {
            "target": str(target),
            "runtime": status,
            "started": 0,
            "skipped": 1,
            "reason": "runtime already running",
        }, 0
    command = str(runtime.get("command") or "")
    parts = safety._command_parts(command)
    cwd = _runtime_cwd(target, runtime)
    if safety._high_risk_command(command):
        blockers.append("runtime command shape is high risk")
    if not parts:
        blockers.append("runtime command could not be parsed")
    if not cwd.is_dir():
        blockers.append(f"runtime cwd missing: {cwd}")
    if status.get("stale_pid"):
        blockers.append(f"stale pid file: {status.get('pid_path')}")
    if isinstance(runtime.get("port"), int) and _port_in_use(runtime["port"]):
        blockers.append(f"port is already in use: {runtime['port']}")
    if blockers:
        return {
            "target": str(target),
            "runtime": status,
            "started": 0,
            "blockers": blockers,
            "error": "runtime is not startable",
        }, 1
    pid_path = _runtime_pid_path(target, runtime)
    metadata_path = _runtime_metadata_path(target, runtime)
    stdout_path, stderr_path = _runtime_log_paths(target, runtime)
    for path in (pid_path, metadata_path, stdout_path, stderr_path):
        path.parent.mkdir(parents=True, exist_ok=True)
    stdout_file = stdout_path.open("a")
    stderr_file = stderr_path.open("a")
    try:
        process = subprocess.Popen(
            parts,
            cwd=cwd,
            stdout=stdout_file,
            stderr=stderr_file,
            text=True,
            start_new_session=True,
        )
    finally:
        stdout_file.close()
        stderr_file.close()
    started_at = _facade("_now")().isoformat()
    pid_path.write_text(f"{process.pid}\n")
    metadata = {
        "runtime_id": runtime.get("id"),
        "pid": process.pid,
        "command": command,
        "cwd": str(cwd),
        "started_at": started_at,
        "pid_path": str(pid_path),
        "stdout_log_path": str(stdout_path),
        "stderr_log_path": str(stderr_path),
    }
    _write_runtime_metadata(target, runtime, metadata)
    health_path = _runtime_health_path(target, runtime)
    if health_path is not None:
        health_path.parent.mkdir(parents=True, exist_ok=True)
        health_path.write_text(
            json.dumps({"runtime_id": runtime.get("id"), "pid": process.pid, "started_at": started_at}, sort_keys=True)
            + "\n"
        )
    status = _runtime_status_item(target, runtime)
    return {
        "target": str(target),
        "runtime": status,
        "started": 1,
        "skipped": 0,
        "pid": process.pid,
        "pid_path": str(pid_path),
        "metadata_path": str(metadata_path),
    }, 0


def _stop_runtime_payload(target: Path, runtime_id: str) -> tuple[dict[str, Any], int]:
    target = target.expanduser().resolve()
    runtime, errors = _find_runtime(target, runtime_id)
    if runtime is None:
        return {"target": str(target), "error": "; ".join(errors)}, 1
    pid_path = _runtime_pid_path(target, runtime)
    pid = _read_pid(pid_path)
    metadata = _read_runtime_metadata(target, runtime)
    if pid is None:
        return {
            "target": str(target),
            "runtime": _runtime_status_item(target, runtime),
            "stopped": 0,
            "reason": "runtime is not running",
        }, 0
    if (
        not metadata
        or metadata.get("runtime_id") != runtime.get("id")
        or metadata.get("pid") != pid
        or metadata.get("command") != runtime.get("command")
    ):
        return {
            "target": str(target),
            "runtime": _runtime_status_item(target, runtime),
            "stopped": 0,
            "error": "refusing to stop unmanaged runtime process",
        }, 1
    if not _facade("_process_alive")(pid):
        pid_path.unlink(missing_ok=True)
        metadata["stopped_at"] = _facade("_now")().isoformat()
        metadata["stop_reason"] = "stale pid"
        _write_runtime_metadata(target, runtime, metadata)
        return {
            "target": str(target),
            "runtime": _runtime_status_item(target, runtime),
            "stopped": 0,
            "reason": "stale pid removed",
        }, 0
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        return {
            "target": str(target),
            "runtime": _runtime_status_item(target, runtime),
            "stopped": 0,
            "error": str(exc),
        }, 1
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if not _facade("_process_alive")(pid):
            break
        time.sleep(0.05)
    if _facade("_process_alive")(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    pid_path.unlink(missing_ok=True)
    metadata["stopped_at"] = _facade("_now")().isoformat()
    _write_runtime_metadata(target, runtime, metadata)
    return {"target": str(target), "runtime": _runtime_status_item(target, runtime), "stopped": 1, "pid": pid}, 0


def _restart_runtime_payload(target: Path, runtime_id: str) -> tuple[dict[str, Any], int]:
    stop_payload, stop_rc = _stop_runtime_payload(target, runtime_id)
    if stop_rc != 0:
        return {
            "target": str(target.expanduser().resolve()),
            "stop": stop_payload,
            "error": stop_payload.get("error"),
        }, stop_rc
    start_payload, start_rc = _start_runtime_payload(target, runtime_id)
    return {
        "target": str(target.expanduser().resolve()),
        "stop": stop_payload,
        "start": start_payload,
        "runtime": start_payload.get("runtime"),
    }, start_rc


def runtime_init(*, target: Path, force: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    path = paths.runtimes_config_path(target)
    if path.exists() and not force:
        print(f"error: tool runtime config already exists: {path}", file=sys.stderr)
        return 2
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(helpers._format_runtimes_toml())
    paths.runtime_state_path(target).mkdir(parents=True, exist_ok=True)
    print(f"runtime_config: {path}")
    print(f"runtimes: {len(constants.DEFAULT_RUNTIMES)}")
    print("next_command: brigade tools runtime list")
    return 0


def runtime_list(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = _runtime_payload(target, run_health=False)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["valid"] else 1
    print(f"tools runtime list: {target}")
    print(f"config_path: {payload['config_path']}")
    if payload["errors"]:
        for error in payload["errors"]:
            print(f"error: {error}")
        return 1
    print(f"runtimes: {payload['runtime_count']}")
    for runtime in payload["runtimes"]:
        print(f"- {runtime.get('id')} [{runtime.get('state')}] port={runtime.get('port') or ''}")
    return 0


def runtime_show(*, target: Path, runtime_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = _runtime_payload(target, runtime_id=runtime_id, run_health=False)
    runtime = payload["runtimes"][0] if payload["runtimes"] else None
    result = {**payload, "runtime": runtime}
    if json_output:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if runtime is not None and payload["valid"] else 1
    if runtime is None:
        print(f"error: runtime not found: {runtime_id}", file=sys.stderr)
        return 1
    print(f"runtime: {runtime.get('id')}")
    print(f"name: {runtime.get('name')}")
    print(f"state: {runtime.get('state')}")
    print(f"pid: {runtime.get('pid') or ''}")
    print(f"command: {runtime.get('command')}")
    print(f"cwd: {runtime.get('cwd')}")
    print(f"pid_path: {runtime.get('pid_path')}")
    return 0


def runtime_status(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = _runtime_payload(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["valid"] else 1
    print(f"tools runtime status: {target}")
    print(f"config_path: {payload['config_path']}")
    if payload["errors"]:
        for error in payload["errors"]:
            print(f"error: {error}")
        return 1
    for state, count in sorted(payload["counts"].items()):
        print(f"{state}: {count}")
    for runtime in payload["runtimes"]:
        print(f"- {runtime.get('id')} [{runtime.get('state')}] health={runtime.get('health_ok')}")
    return 0


def runtime_start(*, target: Path, runtime_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload, rc = _start_runtime_payload(target, runtime_id)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return rc
    print(f"tools runtime start: {runtime_id}")
    if payload.get("error"):
        print(f"error: {payload['error']}", file=sys.stderr)
        for blocker in payload.get("blockers", []):
            print(f"- {blocker}", file=sys.stderr)
        return rc
    print(f"started: {payload.get('started', 0)}")
    print(f"skipped: {payload.get('skipped', 0)}")
    print(f"pid: {payload.get('pid') or payload.get('runtime', {}).get('pid') or ''}")
    return rc


def runtime_stop(*, target: Path, runtime_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload, rc = _stop_runtime_payload(target, runtime_id)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return rc
    print(f"tools runtime stop: {runtime_id}")
    if payload.get("error"):
        print(f"error: {payload['error']}", file=sys.stderr)
        return rc
    print(f"stopped: {payload.get('stopped', 0)}")
    if payload.get("reason"):
        print(f"reason: {payload['reason']}")
    return rc


def runtime_restart(*, target: Path, runtime_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload, rc = _restart_runtime_payload(target, runtime_id)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return rc
    print(f"tools runtime restart: {runtime_id}")
    if payload.get("error"):
        print(f"error: {payload['error']}", file=sys.stderr)
        return rc
    print(f"state: {payload.get('runtime', {}).get('state')}")
    print(f"pid: {payload.get('runtime', {}).get('pid') or ''}")
    return rc


def runtime_doctor(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = _runtime_payload(target)
    text_lines = [f"tools runtime doctor: {target}", f"config_path: {payload['config_path']}"]
    if payload["errors"]:
        for error in payload["errors"]:
            text_lines.append(f"[warn] runtime_config: {error}")
    if payload["issues"]:
        for issue in payload["issues"]:
            text_lines.append(f"[{issue.get('status', constants.WARN)}] {issue.get('name')}: {issue.get('detail')}")
    else:
        text_lines.append("[ok] tool_runtimes: no issues")
    text_lines.append(f"runtime_issues: {payload['issue_count']}")
    return emit(payload, json_output, text_lines, 0 if payload["valid"] else 1)
