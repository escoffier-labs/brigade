"""Fixed local, SSH, service, and incident probes."""

from __future__ import annotations

import os
import re
from typing import Any, Callable, Mapping, NoReturn

from .contracts import FleetError, HEALTH_CLASSES, REACHABILITY, is_offset_datetime
from .exec import EXEC_DEFAULT_OUTPUT_BYTES, ExecRequest, Runner, run_exec
from .normalize import sanitize_detail, utf8_byte_length
from .registry import FleetRegistry
from .runtime_config import FLEET_SAFE_SERVICE_UNIT_PATTERN

PROBE_WORKING_DIRECTORY = "/"
ROOT_FILESYSTEM_PATH = "/"
REBOOT_MARKER_PATH = "/run/reboot-required"
HOST_OBSERVATION_DETAIL = "Linux host summary is available"
SERVICE_OBSERVATION_DETAIL = "Service health is available"
LOCAL_FAILED_SERVICE_ARGS = ("--failed", "--no-legend", "--plain", "--type=service")
SSH_CONNECT_TIMEOUT_SECONDS = 8
LINUX_SSH_REMOTE_COMMAND = (
    "cut -d. -f1 /proc/uptime; df -P / | awk 'NR==2 {gsub(/%/,\"\",$5); print $5}'; "
    "failed=$(systemctl --failed --no-legend --plain --type=service) || exit 1; "
    "printf '%s\\n' \"$failed\" | awk 'BEGIN{c=0} NF{c++} END{print c}'; "
    "if test -e /run/reboot-required; then echo 1; else echo 0; fi"
)
ACTIVE_STATE_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
SSH_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
STORAGE_LINE_RE = re.compile(r"^(?:100(?:\.0+)?|[0-9]{1,2}(?:\.[0-9]+)?)$")

FLEET_FINDING_CATALOG = {
    "unreachable": {
        "severity_class": "critical",
        "summary": "Host is unreachable",
        "blast_radius": "one registered host",
        "proposed_action_id": "inspect-host",
        "verification_id": "verify-host-reachability",
        "rollback_id": "no-rollback",
    },
    "elevated-storage": {
        "severity_class": "warning",
        "summary": "Storage pressure is elevated",
        "blast_radius": "one registered host",
        "proposed_action_id": "inspect-storage",
        "verification_id": "verify-storage-pressure",
        "rollback_id": "no-rollback",
    },
    "critical-storage": {
        "severity_class": "critical",
        "summary": "Storage pressure is critical",
        "blast_radius": "one registered host",
        "proposed_action_id": "inspect-storage",
        "verification_id": "verify-storage-pressure",
        "rollback_id": "no-rollback",
    },
    "failed-services": {
        "severity_class": "warning",
        "summary": "Failed services were observed",
        "blast_radius": "one registered host",
        "proposed_action_id": "inspect-failed-services",
        "verification_id": "verify-failed-services",
        "rollback_id": "no-rollback",
    },
    "pending-reboot": {
        "severity_class": "warning",
        "summary": "A reboot is pending",
        "blast_radius": "one registered host",
        "proposed_action_id": "inspect-reboot",
        "verification_id": "verify-reboot-pending",
        "rollback_id": "no-rollback",
    },
    "unhealthy-service": {
        "severity_class": "critical",
        "summary": "Service health is unhealthy",
        "blast_radius": "one registered service",
        "proposed_action_id": "restart-service",
        "verification_id": "verify-service",
        "rollback_id": "rollback-service",
    },
}


def _invalid_request() -> NoReturn:
    raise FleetError("invalid_request", "Fleet observation request is invalid")


def _protocol_error() -> NoReturn:
    raise FleetError("protocol_error", "Fleet observation was invalid")


def _unavailable() -> NoReturn:
    raise FleetError("unavailable", "Fleet observation is unavailable")


def assert_fleet_ssh_alias(alias: str) -> str:
    if len(alias) < 3 or not SSH_ALIAS_RE.fullmatch(alias):
        _invalid_request()
    return alias


def fleet_ssh_argv(alias: str, remote_args: list[str] | tuple[str, ...]) -> list[str]:
    return [
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={SSH_CONNECT_TIMEOUT_SECONDS}",
        "--",
        assert_fleet_ssh_alias(alias),
        *remote_args,
    ]


def fleet_finding_id(scope: str, kind: str) -> str:
    return f"{scope}:{kind}"


def verify_catalogued_service(observation: Mapping[str, Any]) -> bool:
    return observation.get("health_class") == "healthy"


def remediation_for_finding(finding_id: str) -> dict[str, str] | None:
    kinds = sorted(FLEET_FINDING_CATALOG, key=len, reverse=True)
    for kind in kinds:
        suffix = f":{kind}"
        if not finding_id.endswith(suffix):
            continue
        scope = finding_id[: -len(suffix)]
        if ":" in scope:
            continue
        try:
            from .contracts import parse_identifier

            parse_identifier(scope)
        except FleetError:
            continue
        if finding_id != fleet_finding_id(scope, kind):
            continue
        entry = FLEET_FINDING_CATALOG[kind]
        return {
            "proposed_action_id": entry["proposed_action_id"],
            "verification_id": entry["verification_id"],
            "rollback_id": entry["rollback_id"],
            "blast_radius": entry["blast_radius"],
        }
    return None


def _read_uptime(uptime: Callable[[], float]) -> int:
    try:
        seconds = int(uptime())
        if seconds < 0:
            _protocol_error()
        return seconds
    except FleetError:
        raise
    except Exception:
        _unavailable()


def _read_storage_percent(statvfs: Callable[[str], os.statvfs_result]) -> float:
    try:
        stats = statvfs(ROOT_FILESYSTEM_PATH)
        blocks = int(stats.f_blocks)
        available = int(stats.f_bavail)
        if blocks <= 0 or available < 0 or available > blocks:
            _protocol_error()
        percent = ((blocks - available) / blocks) * 100
        if not 0 <= percent <= 100:
            _protocol_error()
        return percent
    except FleetError:
        raise
    except Exception:
        _unavailable()


def _read_reboot_pending(exists: Callable[[str], bool]) -> bool:
    try:
        return exists(REBOOT_MARKER_PATH) is True
    except Exception:
        _unavailable()


def _count_failed_services(stdout: str) -> int:
    count = 0
    for line in stdout.splitlines():
        if not line.strip():
            continue
        unit = line.split(None, 1)[0] if line.split() else ""
        if not FLEET_SAFE_SERVICE_UNIT_PATTERN.fullmatch(unit):
            _protocol_error()
        count += 1
    return count


def _parse_remote_summary(stdout: str) -> dict[str, Any]:
    lines = [line.strip() for line in stdout.replace("\r", "").rstrip().split("\n")]
    if len(lines) != 4:
        _protocol_error()
    uptime_line, storage_line, failed_line, reboot_line = lines
    if not uptime_line.isdigit() or not failed_line.isdigit():
        _protocol_error()
    if not STORAGE_LINE_RE.fullmatch(storage_line) or reboot_line not in {"0", "1"}:
        _protocol_error()
    uptime_seconds = int(uptime_line)
    storage_percent = float(storage_line)
    failed_services = int(failed_line)
    if uptime_seconds < 0 or failed_services < 0 or not 0 <= storage_percent <= 100:
        _protocol_error()
    return {
        "uptime_seconds": uptime_seconds,
        "storage_percent": storage_percent,
        "failed_services": failed_services,
        "reboot_pending": reboot_line == "1",
    }


def _systemctl_is_active_args(unit: str, manager: str) -> list[str]:
    return ["--user", "is-active", unit] if manager == "user" else ["is-active", unit]


def _parse_active_state_token(stdout: str) -> str | None:
    token = stdout.replace("\r", "").strip()
    if not ACTIVE_STATE_RE.fullmatch(token):
        return None
    return token


def _health_class(state: str) -> str:
    if state == "active":
        return "healthy"
    if state in {"activating", "reloading"}:
        return "degraded"
    if state in {"failed", "inactive"}:
        return "unhealthy"
    return "unknown"


def observe_local_host(
    target: Mapping[str, Any],
    probe_id: str,
    *,
    systemctl_file: str,
    env: Mapping[str, str],
    now: Callable[[], Any],
    uptime: Callable[[], float],
    statvfs: Callable[[str], os.statvfs_result],
    exists: Callable[[str], bool],
    runner: Runner | None = None,
) -> dict[str, Any]:
    if target["adapter"] != "local" or probe_id not in target["probe_ids"]:
        _invalid_request()
    uptime_seconds = _read_uptime(uptime)
    storage_percent = _read_storage_percent(statvfs)
    reboot_pending = _read_reboot_pending(exists)
    result = run_exec(
        ExecRequest(
            file=systemctl_file,
            args=LOCAL_FAILED_SERVICE_ARGS,
            cwd=PROBE_WORKING_DIRECTORY,
            timeout_ms=target["timeout_ms"],
            max_buffer_bytes=EXEC_DEFAULT_OUTPUT_BYTES,
            env=env,
        ),
        runner=runner,
    )
    return {
        "alias": target["alias"],
        "probe_id": probe_id,
        "observed_at": now().isoformat().replace("+00:00", "Z") if now().tzinfo else now().isoformat() + "Z",
        "reachability": "reachable",
        "uptime_seconds": uptime_seconds,
        "storage_percent": storage_percent,
        "failed_services": _count_failed_services(result.stdout),
        "reboot_pending": reboot_pending,
        "detail": HOST_OBSERVATION_DETAIL,
    }


def _iso_now(now: Callable[[], Any]) -> str:
    stamp = now()
    if hasattr(stamp, "isoformat"):
        text = stamp.isoformat()
        if text.endswith("+00:00"):
            return text[:-6] + "Z"
        if stamp.tzinfo is None and not text.endswith("Z"):
            return text + "Z"
        return text
    return str(stamp)


def observe_linux_ssh_host(
    target: Mapping[str, Any],
    probe_id: str,
    *,
    ssh_file: str,
    ssh_alias: str,
    env: Mapping[str, str],
    now: Callable[[], Any],
    runner: Runner | None = None,
) -> dict[str, Any]:
    if target["adapter"] != "linux" or probe_id not in target["probe_ids"]:
        _invalid_request()
    result = run_exec(
        ExecRequest(
            file=ssh_file,
            args=tuple(fleet_ssh_argv(ssh_alias, [LINUX_SSH_REMOTE_COMMAND])),
            cwd=PROBE_WORKING_DIRECTORY,
            timeout_ms=target["timeout_ms"],
            max_buffer_bytes=EXEC_DEFAULT_OUTPUT_BYTES,
            env=env,
        ),
        runner=runner,
    )
    parsed = _parse_remote_summary(result.stdout)
    return {
        "alias": target["alias"],
        "probe_id": probe_id,
        "observed_at": _iso_now(now),
        "reachability": "reachable",
        "uptime_seconds": parsed["uptime_seconds"],
        "storage_percent": parsed["storage_percent"],
        "failed_services": parsed["failed_services"],
        "reboot_pending": parsed["reboot_pending"],
        "detail": HOST_OBSERVATION_DETAIL,
    }


def observe_service(
    target: Mapping[str, Any],
    service: Mapping[str, Any],
    *,
    transport: Mapping[str, str],
    mapping: Mapping[str, str],
    env: Mapping[str, str],
    now: Callable[[], Any],
    runner: Runner | None = None,
) -> dict[str, Any]:
    if service["target_alias"] != target["alias"] or service["probe_id"] not in target["probe_ids"]:
        _invalid_request()
    kind = transport.get("kind")
    if kind == "local" and target["adapter"] != "local":
        _invalid_request()
    if kind == "ssh" and target["adapter"] != "linux":
        _invalid_request()
    unit = mapping.get("unit")
    manager = mapping.get("manager")
    if not isinstance(unit, str) or not FLEET_SAFE_SERVICE_UNIT_PATTERN.fullmatch(unit):
        _invalid_request()
    if manager not in {"user", "system"}:
        _invalid_request()
    remote_args = ["systemctl", *_systemctl_is_active_args(unit, manager)]
    if kind == "local":
        file = transport["systemctl_file"]
        args = tuple(_systemctl_is_active_args(unit, manager))
    elif kind == "ssh":
        file = transport["ssh_file"]
        args = tuple(fleet_ssh_argv(transport["ssh_alias"], remote_args))
    else:
        _invalid_request()
    result = run_exec(
        ExecRequest(
            file=file,
            args=args,
            cwd=PROBE_WORKING_DIRECTORY,
            timeout_ms=target["timeout_ms"],
            max_buffer_bytes=EXEC_DEFAULT_OUTPUT_BYTES,
            env=env,
            accept_numeric_exit=True,
        ),
        runner=runner,
    )
    state = _parse_active_state_token(result.stdout)
    if state is None:
        _protocol_error()
    return {
        "service_id": service["service_id"],
        "target_alias": target["alias"],
        "probe_id": service["probe_id"],
        "observed_at": _iso_now(now),
        "health_class": _health_class(state),
        "detail": SERVICE_OBSERVATION_DETAIL,
    }


def _parse_host_raw(
    raw: object,
    target: Mapping[str, Any],
    probe_id: str,
    secrets: list[str],
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        _protocol_error()
    required = {
        "alias",
        "probe_id",
        "observed_at",
        "reachability",
        "uptime_seconds",
        "storage_percent",
        "failed_services",
        "reboot_pending",
        "detail",
    }
    if set(raw) != required:
        _protocol_error()
    if raw.get("alias") != target["alias"] or raw.get("probe_id") != probe_id:
        _protocol_error()
    if not is_offset_datetime(raw.get("observed_at")) or raw.get("reachability") not in REACHABILITY:
        _protocol_error()
    detail = raw.get("detail")
    if not isinstance(detail, str) or utf8_byte_length(detail) > 16_384:
        _protocol_error()
    return {**dict(raw), "detail": sanitize_detail(detail, secrets)}


def _parse_service_raw(
    raw: object,
    target: Mapping[str, Any],
    service: Mapping[str, Any],
    secrets: list[str],
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        _protocol_error()
    required = {
        "service_id",
        "target_alias",
        "probe_id",
        "observed_at",
        "health_class",
        "detail",
    }
    if set(raw) != required:
        _protocol_error()
    if (
        raw.get("service_id") != service["service_id"]
        or raw.get("target_alias") != target["alias"]
        or raw.get("probe_id") != service["probe_id"]
    ):
        _protocol_error()
    if not is_offset_datetime(raw.get("observed_at")) or raw.get("health_class") not in HEALTH_CLASSES:
        _protocol_error()
    detail = raw.get("detail")
    if not isinstance(detail, str) or utf8_byte_length(detail) > 16_384:
        _protocol_error()
    return {**dict(raw), "detail": sanitize_detail(detail, secrets)}


def _finding_summary(scope: str, kind: str, observed_at: str) -> dict[str, str]:
    entry = FLEET_FINDING_CATALOG[kind]
    return {
        "finding_id": fleet_finding_id(scope, kind),
        "scope": scope,
        "severity_class": entry["severity_class"],
        "summary": entry["summary"],
        "observed_at": observed_at,
    }


def _trusted_target(registry: FleetRegistry, target: Mapping[str, Any]) -> Mapping[str, Any]:
    trusted = registry.target(target["alias"])
    if trusted["adapter"] != target["adapter"] or trusted["tier"] != target["tier"]:
        _invalid_request()
    return trusted


def _trusted_service(
    registry: FleetRegistry,
    target: Mapping[str, Any],
    service: Mapping[str, Any],
) -> Mapping[str, Any]:
    if service["target_alias"] != target["alias"] or service["probe_id"] not in target["probe_ids"]:
        _invalid_request()
    trusted = registry.service(service["service_id"])
    if trusted["target_alias"] != target["alias"] or trusted["probe_id"] != service["probe_id"]:
        _invalid_request()
    return trusted


class FleetProbes:
    """Adapter-backed probe port used by the tool surface."""

    def __init__(
        self,
        *,
        registry: FleetRegistry,
        observe_host_raw: Callable[[Mapping[str, Any], str], dict[str, Any]],
        observe_service_raw: Callable[[Mapping[str, Any], Mapping[str, Any]], dict[str, Any]],
        now: Callable[[], Any],
        create_receipt_ref: Callable[[], str],
        secrets: list[str],
    ):
        self.registry = registry
        self._observe_host_raw = observe_host_raw
        self._observe_service_raw = observe_service_raw
        self._now = now
        self._create_receipt_ref = create_receipt_ref
        self._secrets = secrets

    def observe_host(self, target: Mapping[str, Any], probe_id: str) -> dict[str, Any]:
        trusted = _trusted_target(self.registry, target)
        if probe_id not in trusted["probe_ids"]:
            _invalid_request()
        raw = _parse_host_raw(self._observe_host_raw(trusted, probe_id), trusted, probe_id, self._secrets)
        return {"raw": raw, "receipt_ref": _validated_receipt_ref(self._create_receipt_ref())}

    def observe_service(self, target: Mapping[str, Any], service: Mapping[str, Any]) -> dict[str, Any]:
        trusted_target = _trusted_target(self.registry, target)
        trusted_service = _trusted_service(self.registry, trusted_target, service)
        raw = _parse_service_raw(
            self._observe_service_raw(trusted_target, trusted_service),
            trusted_target,
            trusted_service,
            self._secrets,
        )
        return {"raw": raw, "receipt_ref": _validated_receipt_ref(self._create_receipt_ref())}

    def collect_incident(self, target: Mapping[str, Any], service: Mapping[str, Any] | None = None) -> dict[str, Any]:
        trusted = _trusted_target(self.registry, target)
        scoped = None if service is None else _trusted_service(self.registry, trusted, service)
        scope = trusted["alias"] if scoped is None else scoped["service_id"]
        probe_id = trusted["probe_ids"][0]
        observed_at = _iso_now(self._now)
        findings: list[dict[str, str]] = []
        host = None
        try:
            host = _parse_host_raw(self._observe_host_raw(trusted, probe_id), trusted, probe_id, self._secrets)
        except FleetError as exc:
            if exc.code not in {"unavailable", "timeout"}:
                raise
        if host is None or host["reachability"] == "unreachable":
            findings.append(_finding_summary(scope, "unreachable", observed_at))
        else:
            storage = host.get("storage_percent")
            if storage is not None and storage >= 90:
                findings.append(_finding_summary(scope, "critical-storage", observed_at))
            elif storage is not None and storage >= 75:
                findings.append(_finding_summary(scope, "elevated-storage", observed_at))
            if host.get("failed_services", 0) > 0:
                findings.append(_finding_summary(scope, "failed-services", observed_at))
            if host.get("reboot_pending"):
                findings.append(_finding_summary(scope, "pending-reboot", observed_at))
            if scoped is not None:
                observed = _parse_service_raw(
                    self._observe_service_raw(trusted, scoped),
                    trusted,
                    scoped,
                    self._secrets,
                )
                if observed["health_class"] == "unhealthy":
                    findings.append(_finding_summary(scope, "unhealthy-service", observed_at))
        status = "unavailable" if host is None or host["reachability"] == "unreachable" else "ok"
        return {
            "raw": {
                "scope": scope,
                "status": status,
                "observed_at": observed_at,
                "findings": findings,
            },
            "receipt_ref": _validated_receipt_ref(self._create_receipt_ref()),
        }


def _validated_receipt_ref(value: str) -> str:
    from .contracts import parse_identifier

    try:
        return parse_identifier(value)
    except FleetError:
        _protocol_error()


def create_fleet_adapters(
    *,
    registry: FleetRegistry,
    systemctl_file: str,
    ssh_file: str,
    env: Mapping[str, str],
    now: Callable[[], Any],
    uptime: Callable[[], float],
    statvfs: Callable[[str], os.statvfs_result],
    exists: Callable[[str], bool],
    create_receipt_ref: Callable[[], str],
    secrets: list[str],
    ssh_aliases: Mapping[str, str],
    service_mappings: Mapping[str, Mapping[str, str]],
    runner: Runner | None = None,
) -> FleetProbes:
    adapter_secrets = [
        *secrets,
        *ssh_aliases.values(),
        *(mapping["unit"] for mapping in service_mappings.values()),
        systemctl_file,
        ssh_file,
    ]

    def observe_host_raw(target: Mapping[str, Any], probe_id: str) -> dict[str, Any]:
        if target["adapter"] == "local":
            return observe_local_host(
                target,
                probe_id,
                systemctl_file=systemctl_file,
                env=env,
                now=now,
                uptime=uptime,
                statvfs=statvfs,
                exists=exists,
                runner=runner,
            )
        if target["adapter"] == "linux":
            alias = ssh_aliases.get(target["alias"])
            if alias is None:
                _invalid_request()
            return observe_linux_ssh_host(
                target,
                probe_id,
                ssh_file=ssh_file,
                ssh_alias=alias,
                env=env,
                now=now,
                runner=runner,
            )
        _invalid_request()

    def observe_service_raw(target: Mapping[str, Any], service: Mapping[str, Any]) -> dict[str, Any]:
        mapping = service_mappings.get(service["service_id"])
        if mapping is None:
            _invalid_request()
        if target["adapter"] == "local":
            transport = {"kind": "local", "systemctl_file": systemctl_file}
        elif target["adapter"] == "linux":
            alias = ssh_aliases.get(target["alias"])
            if alias is None:
                _invalid_request()
            transport = {"kind": "ssh", "ssh_file": ssh_file, "ssh_alias": alias}
        else:
            _invalid_request()
        return observe_service(
            target,
            service,
            transport=transport,
            mapping=mapping,
            env=env,
            now=now,
            runner=runner,
        )

    return FleetProbes(
        registry=registry,
        observe_host_raw=observe_host_raw,
        observe_service_raw=observe_service_raw,
        now=now,
        create_receipt_ref=create_receipt_ref,
        secrets=adapter_secrets,
    )
