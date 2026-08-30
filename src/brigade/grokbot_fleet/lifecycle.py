"""Pack lifecycle, listener, doctor, canary, and unit rendering."""

from __future__ import annotations

import json
import os
import secrets
import stat
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, NoReturn

from .. import grokbot_mcp, grokbot_ops
from .actions import CatalogRemediator, FleetActionStore, ResearchBridgeRestarter
from .contracts import PACK_ID, TOOLS, FleetError
from .exec import Runner
from .ledger import FleetLedger
from .probes import create_fleet_adapters
from .runtime_config import (
    assert_disjoint_paths,
    fleet_probe_environment,
    normalize_absolute_path,
    parse_fleet_private_runtime,
    project_fleet_public_registry,
    read_secure_runtime_text,
)
from .tools import FleetHubClaims, FleetStewardTools

MAX_REQUEST_BYTES = 16_384
SERVICE_NAME = "grokbot-fleet-steward"
FLEET_PATH_KEYS = ("runtime_path", "ledger_path", "action_state_path", "approval_dir")
_TOOL_DESCRIPTIONS = {
    "fleet_overview": "Read a bounded Fleet Steward overview",
    "host_status": "Read the registered host observation",
    "incident_bundle": "Read the registered incident bundle",
    "propose_remediation": "Propose a remediation that still requires approval",
    "service_health": "Read the registered service observation",
    "execute_remediation": "Execute an approved remediation proposal",
}


@dataclass(frozen=True)
class FleetListenerConfig:
    target: Path
    bind_host: str
    bind_port: int
    allowed_hosts: tuple[str, ...]
    allowed_origins: tuple[str, ...]
    bearer: str
    runtime_path: str
    ledger_path: str
    action_state_path: str
    approval_dir: str

    def __repr__(self) -> str:
        return f"FleetListenerConfig(bind_host={self.bind_host!r}, bind_port={self.bind_port})"


def _environment_invalid() -> NoReturn:
    raise FleetError("invalid_request", "Fleet environment is invalid")


def validate_absolute_reference(path_text: object) -> str:
    if not isinstance(path_text, str) or not path_text or "\0" in path_text:
        _environment_invalid()
    if not path_text.startswith("/") or any(part in {".", ".."} for part in path_text.split("/")):
        _environment_invalid()
    return normalize_absolute_path(path_text)


def validate_disjoint_state_paths(
    runtime_path: str,
    ledger_path: str,
    action_state_path: str,
    approval_dir: str,
) -> dict[str, str]:
    paths = {
        "runtime_path": validate_absolute_reference(runtime_path),
        "ledger_path": validate_absolute_reference(ledger_path),
        "action_state_path": validate_absolute_reference(action_state_path),
        "approval_dir": validate_absolute_reference(approval_dir),
    }
    assert_disjoint_paths(list(paths.values()))
    return paths


def _lstat_nofollow(path_text: str) -> os.stat_result:
    path = Path(path_text)
    if grokbot_ops._path_is_symlink(path) or path.is_symlink():
        _environment_invalid()
    parent = -1
    try:
        parent = grokbot_ops._open_parent_nofollow(path, create=False)
        info = os.stat(path.name, dir_fd=parent, follow_symlinks=False) if os.name == "posix" else path.lstat()
    except OSError as exc:
        raise FleetError("invalid_request", "Fleet environment is invalid") from exc
    finally:
        if parent != -1:
            os.close(parent)
    if stat.S_ISLNK(info.st_mode):
        _environment_invalid()
    return info


def validate_runtime_file(path_text: str) -> str:
    normalized = validate_absolute_reference(path_text)
    read_secure_runtime_text(normalized)
    return normalized


def validate_state_directory(path_text: str, *, must_exist: bool) -> str:
    normalized = validate_absolute_reference(path_text)
    path = Path(normalized)
    if not path.exists():
        if must_exist:
            _environment_invalid()
        return normalized
    info = _lstat_nofollow(normalized)
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
        _environment_invalid()
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        _environment_invalid()
    return normalized


def _accepted_executable(name: str, resolved: str | None) -> str:
    if not isinstance(resolved, str) or not resolved:
        _environment_invalid()
    if not resolved.startswith("/") or "\0" in resolved:
        _environment_invalid()
    parts = resolved.split("/")
    for index, part in enumerate(parts):
        if index == 0:
            continue
        if part in {"", ".", ".."}:
            _environment_invalid()
        if index == len(parts) - 1 and (part.startswith("-") or part != name):
            _environment_invalid()
    return resolved


def resolve_executable(name: str, env: Mapping[str, str] | None = None) -> str | None:
    if name not in {"ssh", "systemctl"}:
        return None
    path_value = (env or os.environ).get("PATH")
    if not path_value:
        return None
    for directory in path_value.split(os.pathsep):
        if not directory:
            continue
        candidate = os.path.join(directory, name)
        try:
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return _accepted_executable(name, candidate)
        except FleetError:
            continue
    return None


def _opaque_id() -> str:
    return secrets.token_hex(16)


def _load_validated_instance(target: Path) -> dict[str, Any]:
    from .. import grokbot_packs

    payload = grokbot_packs._load_instance_config(target, PACK_ID)
    paths = validate_disjoint_state_paths(
        str(payload["runtime_path"]),
        str(payload["ledger_path"]),
        str(payload["action_state_path"]),
        str(payload["approval_dir"]),
    )
    grokbot_packs._parse_default_bind(str(payload["bind"]))
    grokbot_packs._validate_bearer_reference(payload["bearer"])
    validated = dict(payload)
    validated.update(paths)
    return validated


def _resolve_bearer(reference: Mapping[str, str]) -> str:
    bearer = grokbot_ops._resolve_bearer(dict(reference))
    if len(bearer) < 32:
        _environment_invalid()
    return bearer


def _load_runtime(target: Path) -> tuple[FleetListenerConfig, str]:
    from .. import grokbot_packs

    payload = _load_validated_instance(target)
    host, port = grokbot_packs._parse_default_bind(str(payload["bind"]))
    bearer = _resolve_bearer(payload["bearer"])
    config = FleetListenerConfig(
        target=target,
        bind_host=host,
        bind_port=port,
        allowed_hosts=tuple(payload["allowed_hosts"]),
        allowed_origins=tuple(payload["allowed_origins"]),
        bearer=bearer,
        runtime_path=payload["runtime_path"],
        ledger_path=payload["ledger_path"],
        action_state_path=payload["action_state_path"],
        approval_dir=payload["approval_dir"],
    )
    return config, bearer


def _optional_wazuh_source(target: Path) -> Any:
    try:
        from .. import grokbot_packs
        from ..grokbot_wazuh.store import WazuhStore

        payload = grokbot_packs._load_instance_config(target, "wazuh-triage")
        store = WazuhStore(str(payload["ledger_path"]))
        store.ready()
    except Exception:
        return None

    class _Source:
        def current_finding(self, finding_id: str) -> dict[str, Any] | None:
            return store.get_finding(finding_id)

        def suppressions(self) -> list[dict[str, str]]:
            return store.suppressions()

    return _Source()


def build_tools_from_config(
    config: FleetListenerConfig,
    *,
    env: Mapping[str, str] | None = None,
    runner: Runner | None = None,
    now: Callable[[], datetime] | None = None,
) -> FleetStewardTools:
    source = os.environ if env is None else env
    dispatch_token = source.get("GROKBOT_DISPATCH_TOKEN")
    if dispatch_token is not None and dispatch_token == config.bearer:
        _environment_invalid()
    runtime_path = validate_runtime_file(config.runtime_path)
    validate_state_directory(config.action_state_path, must_exist=False)
    validate_state_directory(config.approval_dir, must_exist=True)
    private_runtime = parse_fleet_private_runtime(json.loads(read_secure_runtime_text(runtime_path)))
    registry = project_fleet_public_registry(private_runtime)
    ssh_file = _accepted_executable("ssh", resolve_executable("ssh", source))
    systemctl_file = _accepted_executable("systemctl", resolve_executable("systemctl", source))
    ssh_aliases = {
        "hypervisor": private_runtime["roles"]["hypervisor"]["ssh_alias"],
        "worker": private_runtime["roles"]["worker"]["ssh_alias"],
    }
    service_mappings = {
        name: dict(private_runtime["services"][name])
        for name in ("research-bridge", "virtualization-api", "overlay-network")
    }
    secret_list = [
        config.bearer,
        config.runtime_path,
        config.ledger_path,
        config.action_state_path,
        config.approval_dir,
        *([dispatch_token] if dispatch_token else []),
        ssh_aliases["hypervisor"],
        ssh_aliases["worker"],
        *(mapping["unit"] for mapping in service_mappings.values()),
        ssh_file,
        systemctl_file,
    ]
    ledger = FleetLedger(config.ledger_path)
    ledger.ready()
    store = FleetActionStore(action_state_path=config.action_state_path, approval_dir=config.approval_dir)
    store.ready()
    probe_env = fleet_probe_environment(source)
    executor = ResearchBridgeRestarter(
        systemctl_file=systemctl_file,
        mapping=service_mappings["research-bridge"],
        env=probe_env,
        runner=runner,
    )
    remediator = CatalogRemediator(executor)
    probes = create_fleet_adapters(
        registry=registry,
        systemctl_file=systemctl_file,
        ssh_file=ssh_file,
        env=probe_env,
        now=now or (lambda: datetime.now(timezone.utc)),
        uptime=_uptime_seconds,
        statvfs=os.statvfs,
        exists=os.path.exists,
        create_receipt_ref=_opaque_id,
        secrets=secret_list,
        ssh_aliases=ssh_aliases,
        service_mappings=service_mappings,
        runner=runner,
    )
    return FleetStewardTools(
        registry=registry,
        probes=probes,
        ledger=ledger,
        store=store,
        executor=executor,
        now=now or (lambda: datetime.now(timezone.utc)),
        request_id=_opaque_id,
        create_proposal_id=_opaque_id,
        create_nonce=_opaque_id,
        create_receipt_id=_opaque_id,
        secrets=secret_list,
        wazuh_source=_optional_wazuh_source(config.target),
        remediator=remediator,
        claims=FleetHubClaims(),
    )


def _uptime_seconds() -> float:
    try:
        return float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
    except OSError:
        return 0.0


class FleetAdapter:
    def __init__(self, config: FleetListenerConfig, tools: FleetStewardTools):
        self.config = config
        self.tools = tools

    def authorized(self, authorization: str | None) -> bool:
        if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
            return False
        bearer = authorization[7:]
        if not bearer.isascii():
            return False
        import hmac

        return hmac.compare_digest(bearer, self.config.bearer)

    def health_payload(self) -> dict[str, object]:
        return {"ok": True, "service": SERVICE_NAME}

    def tool_inventory(self) -> list[dict[str, str]]:
        return [{"name": name, "description": _TOOL_DESCRIPTIONS[name]} for name in sorted(TOOLS)]

    def call_tool(self, name: str, arguments: object) -> dict[str, Any]:
        try:
            return self.tools.call_tool(name, arguments)
        except FleetError as exc:
            return exc.public_error()


def doctor(target: Path, *, timeout: int | None = None) -> list[dict[str, str]]:
    from .. import grokbot_packs

    checks: list[dict[str, str]] = []

    def record(name: str, ok: bool) -> None:
        checks.append({"check": name, "status": "ok" if ok else "fail"})

    try:
        from mcp.server import MCPServer  # noqa: F401

        record("dependency", True)
    except ImportError:
        record("dependency", False)
    try:
        config, bearer = _load_runtime(target)
        record("config", True)
    except (FleetError, grokbot_packs.PackError, grokbot_mcp.ConfigurationError, OSError, ValueError, KeyError):
        record("config", False)
        return checks
    parent = grokbot_packs.instance_config_path(target, PACK_ID).parent
    record("permissions", os.access(parent, os.W_OK) if parent.is_dir() else False)
    record("endpoint", _health_check(config, bearer, timeout or grokbot_ops.DEFAULT_TIMEOUT_SECONDS))
    return checks


def canary(target: Path, *, timeout: int | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": False, "pack_id": PACK_ID}
    try:
        config, bearer = _load_runtime(target)
    except Exception:
        result["reason"] = "config"
        return result
    wait = timeout or grokbot_ops.DEFAULT_TIMEOUT_SECONDS
    base = f"http://{grokbot_ops._connect_host(config.bind_host)}:{config.bind_port}"
    health = grokbot_ops._request_json(f"{base}/health", bearer, wait, method="GET")
    anonymous_status = grokbot_ops._anonymous_health_status(f"{base}/health", wait)
    tools_response = grokbot_ops._tools_list(base, bearer, wait)
    if health is None or health.get("ok") is not True or health.get("service") != SERVICE_NAME:
        result["reason"] = "health"
        return result
    if anonymous_status not in {401, 403}:
        result["reason"] = "auth"
        return result
    if tools_response is None:
        result["reason"] = "inventory-unreachable"
        return result
    names = {tool.get("name") for tool in tools_response if isinstance(tool, dict)}
    if names != set(TOOLS):
        result["reason"] = "inventory-mismatch"
        return result
    result.update(
        {
            "ok": True,
            "health": health,
            "auth_rejected_without_bearer": True,
            "tools": sorted(TOOLS),
        }
    )
    return result


def render_unit(target: Path, *, python: str | None = None) -> str:
    instance = _load_validated_instance(target)
    reference = instance["bearer"]
    bind = instance["bind"]
    args = [
        python or sys.executable,
        "-m",
        "brigade",
        "run",
        "cloud",
        "grokbot",
        "serve",
        "--pack",
        PACK_ID,
        "--target",
        str(target),
        "--bind",
        bind,
    ]
    for host in instance.get("allowed_hosts", []):
        args += ["--allow-host", host]
    for origin in instance.get("allowed_origins", []):
        args += ["--allow-origin", origin]
    if reference["kind"] == "file":
        args += ["--bearer-file", reference["path"]]
    elif reference["kind"] == "env":
        args += ["--bearer-env", reference["name"]]
    exec_start = " ".join(grokbot_ops._systemd_quote(argument) for argument in args)
    writable = " ".join(
        grokbot_ops._systemd_quote(path)
        for path in (
            str(Path(instance["ledger_path"]).parent),
            instance["action_state_path"],
        )
    )
    return (
        "# Generated by brigade run cloud grokbot pack install-service.\n"
        f"# Unit: {grokbot_ops.unit_name(PACK_ID)}\n"
        "[Unit]\n"
        "Description=Brigade Grok Bot MCP listener (fleet-steward)\n"
        "After=network.target\n"
        f"{grokbot_ops.LISTENER_RECOVERY_UNIT_FRAGMENT}"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={exec_start}\n"
        f"{grokbot_ops.LISTENER_RECOVERY_SERVICE_FRAGMENT}"
        "KillMode=mixed\n"
        "TimeoutStopSec=20\n"
        "StandardOutput=journal\n"
        "StandardError=journal\n"
        "NoNewPrivileges=yes\n"
        "PrivateTmp=yes\n"
        "ProtectSystem=strict\n"
        "ProtectHome=read-only\n\n"
        f"ReadWritePaths={writable}\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def write_unit(target: Path, out_dir: Path, *, force: bool = False, python: str | None = None) -> Path:
    rendered = render_unit(target, python=python)
    path = out_dir / grokbot_ops.unit_name(PACK_ID)
    try:
        existing = grokbot_ops._read_regular_text(path)
    except FileNotFoundError:
        existing = None
    except OSError as exc:
        if not force or not grokbot_ops._path_is_symlink(path):
            raise grokbot_ops.ServiceRenderError(f"refusing unsafe unit path: {path.name}") from exc
        existing = None
    if existing == rendered:
        return path
    if existing is not None and not force:
        raise grokbot_ops.ServiceRenderError(f"refusing to overwrite existing unit: {path.name}")
    try:
        grokbot_ops._write_text_nofollow_atomic(
            path,
            rendered,
            mode=0o644,
            replace_symlink=force,
            replace=force or existing is not None,
        )
    except OSError as exc:
        raise grokbot_ops.ServiceRenderError(f"refusing unsafe unit path: {path.name}") from exc
    return path


def build_app(config: FleetListenerConfig, tools: FleetStewardTools) -> Callable[..., Any]:
    MCPServer, JSONResponse, _, TransportSecuritySettings = grokbot_mcp._load_mcp()
    adapter = FleetAdapter(config, tools)
    server = MCPServer(SERVICE_NAME, version="1")

    @server.custom_route("/health", methods=["GET"])
    async def health(_: Any) -> Any:
        return JSONResponse(adapter.health_payload())

    _register_tools(server, adapter)
    allowed_hosts = list(dict.fromkeys((*grokbot_mcp.SDK_LOOPBACK_ALLOWED_HOSTS, *config.allowed_hosts)))
    if not grokbot_mcp._is_loopback(config.bind_host):
        allowed_hosts = list(config.allowed_hosts)
    app = server.streamable_http_app(
        json_response=True,
        stateless_http=True,
        max_request_body_size=MAX_REQUEST_BYTES,
        host=config.bind_host,
        transport_security=TransportSecuritySettings(
            allowed_hosts=allowed_hosts,
            allowed_origins=list(config.allowed_origins),
        ),
    )
    return _FleetGateASGI(app, _FleetRequestGate(config), adapter)


def run_listener(config: FleetListenerConfig, tools: FleetStewardTools) -> None:
    grokbot_mcp._load_mcp()
    try:
        import uvicorn
    except ImportError as exc:
        raise grokbot_mcp.OptionalDependencyError() from exc
    uvicorn.run(
        build_app(config, tools), host=config.bind_host, port=config.bind_port, access_log=False, log_level="warning"
    )


def build_listener_from_target(
    target: Path,
    *,
    bind: str | None,
    allowed_hosts: list[str],
    allowed_origins: list[str],
    bearer_file: Path | None,
    bearer_env: str | None,
) -> tuple[FleetListenerConfig, FleetStewardTools]:
    from .. import grokbot_packs

    instance = grokbot_packs._load_instance_config(target, PACK_ID)
    chosen_bind = bind or instance["bind"]
    host, port = grokbot_packs._parse_default_bind(chosen_bind)
    paths = validate_disjoint_state_paths(
        str(instance["runtime_path"]),
        str(instance["ledger_path"]),
        str(instance["action_state_path"]),
        str(instance["approval_dir"]),
    )
    bearer = grokbot_mcp.load_bearer(bearer_file=bearer_file, bearer_env=bearer_env)
    if len(bearer) < 32:
        _environment_invalid()
    dispatch_token = os.environ.get("GROKBOT_DISPATCH_TOKEN")
    if dispatch_token is not None and bearer == dispatch_token:
        _environment_invalid()
    config = FleetListenerConfig(
        target=target.expanduser().resolve(),
        bind_host=host,
        bind_port=port,
        allowed_hosts=tuple(allowed_hosts),
        allowed_origins=tuple(allowed_origins),
        bearer=bearer,
        **paths,
    )
    return config, build_tools_from_config(config)


def _register_tools(server: Any, adapter: FleetAdapter) -> None:
    def fleet_overview() -> dict[str, Any]:
        return adapter.call_tool("fleet_overview", {})

    def host_status(alias: str) -> dict[str, Any]:
        return adapter.call_tool("host_status", {"alias": alias})

    def service_health(service_id: str) -> dict[str, Any]:
        return adapter.call_tool("service_health", {"service_id": service_id})

    def incident_bundle(scope: str) -> dict[str, Any]:
        return adapter.call_tool("incident_bundle", {"scope": scope})

    def propose_remediation(finding_id: str) -> dict[str, Any]:
        return adapter.call_tool("propose_remediation", {"finding_id": finding_id})

    def execute_remediation(proposal_id: str) -> dict[str, Any]:
        return adapter.call_tool("execute_remediation", {"proposal_id": proposal_id})

    for name, handler in (
        ("fleet_overview", fleet_overview),
        ("host_status", host_status),
        ("incident_bundle", incident_bundle),
        ("propose_remediation", propose_remediation),
        ("service_health", service_health),
        ("execute_remediation", execute_remediation),
    ):
        handler.__name__ = name
        handler.__doc__ = _TOOL_DESCRIPTIONS[name]
        server.tool(name=name, description=_TOOL_DESCRIPTIONS[name])(handler)


class _FleetRequestGate:
    def __init__(self, config: FleetListenerConfig, *, max_request_bytes: int = MAX_REQUEST_BYTES):
        self.config = config
        self.max_request_bytes = max_request_bytes
        self._adapter = FleetAdapter(config, config_tools_placeholder(config))

    def reject_reason(self, headers: Mapping[str, str], body_size: int) -> str | None:
        if body_size < 0 or body_size > self.max_request_bytes:
            return "too-large"
        host = headers.get("host", "").split(":", 1)[0].casefold()
        if grokbot_mcp._is_loopback(self.config.bind_host) and host in {"localhost", "127.0.0.1", "::1", ""}:
            pass
        elif host not in {entry.casefold().split(":", 1)[0] for entry in self.config.allowed_hosts}:
            return "forbidden"
        origin = headers.get("origin")
        if origin is not None and origin not in self.config.allowed_origins:
            return "forbidden"
        if not self._adapter.authorized(headers.get("authorization")):
            return "unauthorized"
        return None


def config_tools_placeholder(config: FleetListenerConfig) -> FleetStewardTools:
    # Gate only needs authorized(); tool methods are unused here.
    return FleetStewardTools(  # pragma: no cover - constructed for auth only
        registry=None,  # type: ignore[arg-type]
        probes=None,  # type: ignore[arg-type]
        ledger=None,  # type: ignore[arg-type]
        store=None,
        executor=None,
        now=lambda: datetime.now(timezone.utc),
        request_id=_opaque_id,
        create_proposal_id=_opaque_id,
        create_nonce=_opaque_id,
        create_receipt_id=_opaque_id,
        secrets=[],
    )


class _FleetGateASGI:
    def __init__(self, app: Callable[..., Any], gate: _FleetRequestGate, adapter: FleetAdapter):
        self.app = app
        self.gate = gate
        self.adapter = adapter

    async def __call__(self, scope: dict[str, Any], receive: Callable[..., Any], send: Callable[..., Any]) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.decode("latin-1").casefold(): value.decode("latin-1") for key, value in scope.get("headers", [])}
        content_length = headers.get("content-length")
        if content_length is not None and (
            not content_length.isdecimal() or int(content_length) > self.gate.max_request_bytes
        ):
            await grokbot_mcp._reject_http(send, 413, "Request body is too large")
            return
        reason = self.gate.reject_reason(headers, 0)
        if reason is not None:
            await grokbot_mcp._reject_http(
                send,
                401 if reason == "unauthorized" else 403,
                "Unauthorized" if reason == "unauthorized" else "Forbidden",
            )
            return
        body, messages, too_large = await grokbot_mcp._read_bounded_body(receive, self.gate.max_request_bytes)
        if too_large:
            await grokbot_mcp._reject_http(send, 413, "Request body is too large")
            return
        if scope.get("path") == "/mcp" and _invalid_fleet_tool_request(body):
            await grokbot_mcp._reject_tool(send, body)
            return
        await self.app(scope, grokbot_mcp._replay_messages(messages, receive), send)


def _invalid_fleet_tool_request(body: bytes) -> bool:
    from .contracts import (
        parse_execute_input,
        parse_host_status_input,
        parse_incident_input,
        parse_overview_input,
        parse_propose_input,
        parse_service_health_input,
    )

    try:
        request = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(request, dict) or request.get("method") != "tools/call":
        return False
    params = request.get("params")
    if not isinstance(params, dict):
        return True
    name = params.get("name")
    if name not in TOOLS:
        return True
    arguments = params.get("arguments")
    parser = {
        "fleet_overview": parse_overview_input,
        "host_status": parse_host_status_input,
        "service_health": parse_service_health_input,
        "incident_bundle": parse_incident_input,
        "propose_remediation": parse_propose_input,
        "execute_remediation": parse_execute_input,
    }[name]
    try:
        parser(arguments if arguments is not None else {})
    except FleetError:
        return True
    return False


def _health_check(config: FleetListenerConfig, bearer: str, timeout: int) -> bool:
    payload = grokbot_ops._request_json(
        f"http://{grokbot_ops._connect_host(config.bind_host)}:{config.bind_port}/health",
        bearer,
        timeout,
        method="GET",
    )
    return payload is not None and payload.get("ok") is True and payload.get("service") == SERVICE_NAME
