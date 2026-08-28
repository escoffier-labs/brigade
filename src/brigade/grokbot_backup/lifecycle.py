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
from .actions import BackupActionExecutor, BackupActionStore
from .adapters import BackupObservers
from .contracts import PACK_ID, TOOLS, BackupError
from .exec import Runner, create_process_limiter
from .ledger import BackupLedger
from .runtime_config import (
    assert_disjoint_paths,
    backup_adapter_environment,
    normalize_absolute_path,
    parse_backup_private_runtime,
    project_backup_registry,
    read_secure_runtime_text,
)
from .tools import BackupStewardTools

MAX_REQUEST_BYTES = 16_384
SERVICE_NAME = "grokbot-backup-steward"
BACKUP_PATH_KEYS = ("runtime_path", "ledger_path", "action_state_path", "approval_dir")
_TOOL_DESCRIPTIONS = {
    "backup_overview": "Read a bounded backup overview",
    "backup_target_status": "Read one backup target observation",
    "backup_restore_readiness": "Read restore-readiness evidence",
    "backup_operation_status": "Read a backup operation",
    "backup_propose_action": "Propose a backup action that still requires approval",
    "backup_execute_action": "Execute an approved backup action proposal",
}


@dataclass(frozen=True)
class BackupListenerConfig:
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
        return f"BackupListenerConfig(bind_host={self.bind_host!r}, bind_port={self.bind_port})"


def _environment_invalid() -> NoReturn:
    raise BackupError("invalid_request", "Backup environment is invalid")


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
        raise BackupError("invalid_request", "Backup environment is invalid") from exc
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


def _load_runtime(target: Path) -> tuple[BackupListenerConfig, str]:
    from .. import grokbot_packs

    payload = _load_validated_instance(target)
    host, port = grokbot_packs._parse_default_bind(str(payload["bind"]))
    bearer = _resolve_bearer(payload["bearer"])
    config = BackupListenerConfig(
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


def build_tools_from_config(
    config: BackupListenerConfig,
    *,
    env: Mapping[str, str] | None = None,
    runner: Runner | None = None,
    now: Callable[[], datetime] | None = None,
) -> BackupStewardTools:
    source = os.environ if env is None else env
    dispatch_token = source.get("GROKBOT_DISPATCH_TOKEN")
    if dispatch_token is not None and dispatch_token == config.bearer:
        _environment_invalid()
    runtime_path = validate_runtime_file(config.runtime_path)
    validate_state_directory(config.action_state_path, must_exist=False)
    validate_state_directory(config.approval_dir, must_exist=True)
    private_runtime = parse_backup_private_runtime(json.loads(read_secure_runtime_text(runtime_path)))
    for command in (*private_runtime["reporters"].values(), *private_runtime["action_runners"].values()):
        if not os.path.exists(command["executable"]):
            raise BackupError("invalid_request", "Backup runtime configuration is invalid")
    registry = project_backup_registry(private_runtime)
    secret_list = [
        config.bearer,
        config.runtime_path,
        config.ledger_path,
        config.action_state_path,
        config.approval_dir,
        *([dispatch_token] if dispatch_token else []),
        *(command["executable"] for command in private_runtime["reporters"].values()),
        *(command["executable"] for command in private_runtime["action_runners"].values()),
    ]
    ledger = BackupLedger(config.ledger_path)
    ledger.ready()
    store = BackupActionStore(action_state_path=config.action_state_path, approval_dir=config.approval_dir, now=now)
    store.ready()
    clock = now or (lambda: datetime.now(timezone.utc))
    limiter = create_process_limiter(private_runtime["max_concurrent_processes"])
    observers = BackupObservers(
        runtime=private_runtime,
        registry=registry,
        env=backup_adapter_environment(source),
        create_receipt_ref=_opaque_id,
        secrets=secret_list,
        runner=runner,
        process_limiter=limiter,
    )
    executor = BackupActionExecutor(
        runtime=private_runtime,
        store=store,
        env=backup_adapter_environment(source),
        now=clock,
        create_operation_id=_opaque_id,
        create_receipt_ref=_opaque_id,
        secrets=secret_list,
        runner=runner,
        process_limiter=limiter,
    )
    return BackupStewardTools(
        registry=registry,
        runtime=private_runtime,
        observers=observers,
        ledger=ledger,
        store=store,
        executor=executor,
        now=clock,
        request_id=_opaque_id,
        secrets=secret_list,
    )


class BackupAdapter:
    def __init__(self, config: BackupListenerConfig, tools: BackupStewardTools):
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
        except BackupError as exc:
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
    except (BackupError, grokbot_packs.PackError, grokbot_mcp.ConfigurationError, OSError, ValueError, KeyError):
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
        "Description=Brigade Grok Bot MCP listener (backup-steward)\n"
        "After=network.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={exec_start}\n"
        "Restart=on-failure\n"
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


def build_app(config: BackupListenerConfig, tools: BackupStewardTools) -> Callable[..., Any]:
    MCPServer, JSONResponse, _, TransportSecuritySettings = grokbot_mcp._load_mcp()
    adapter = BackupAdapter(config, tools)
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
    return _BackupGateASGI(app, _BackupRequestGate(config), adapter)


def run_listener(config: BackupListenerConfig, tools: BackupStewardTools) -> None:
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
) -> tuple[BackupListenerConfig, BackupStewardTools]:
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
    config = BackupListenerConfig(
        target=target.expanduser().resolve(),
        bind_host=host,
        bind_port=port,
        allowed_hosts=tuple(allowed_hosts),
        allowed_origins=tuple(allowed_origins),
        bearer=bearer,
        **paths,
    )
    return config, build_tools_from_config(config)


def _register_tools(server: Any, adapter: BackupAdapter) -> None:
    def backup_overview() -> dict[str, Any]:
        return adapter.call_tool("backup_overview", {})

    def backup_target_status(target_alias: str) -> dict[str, Any]:
        return adapter.call_tool("backup_target_status", {"target_alias": target_alias})

    def backup_restore_readiness(target_alias: str) -> dict[str, Any]:
        return adapter.call_tool("backup_restore_readiness", {"target_alias": target_alias})

    def backup_operation_status(operation_id: str) -> dict[str, Any]:
        return adapter.call_tool("backup_operation_status", {"operation_id": operation_id})

    def backup_propose_action(target_alias: str, action_id: str, finding_id: str) -> dict[str, Any]:
        return adapter.call_tool(
            "backup_propose_action",
            {"target_alias": target_alias, "action_id": action_id, "finding_id": finding_id},
        )

    def backup_execute_action(proposal_id: str) -> dict[str, Any]:
        return adapter.call_tool("backup_execute_action", {"proposal_id": proposal_id})

    for name, handler in (
        ("backup_overview", backup_overview),
        ("backup_target_status", backup_target_status),
        ("backup_restore_readiness", backup_restore_readiness),
        ("backup_operation_status", backup_operation_status),
        ("backup_propose_action", backup_propose_action),
        ("backup_execute_action", backup_execute_action),
    ):
        handler.__name__ = name
        handler.__doc__ = _TOOL_DESCRIPTIONS[name]
        server.tool(name=name, description=_TOOL_DESCRIPTIONS[name])(handler)


class _BackupRequestGate:
    def __init__(self, config: BackupListenerConfig, *, max_request_bytes: int = MAX_REQUEST_BYTES):
        self.config = config
        self.max_request_bytes = max_request_bytes
        self._adapter = BackupAdapter(config, config_tools_placeholder(config))

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


def config_tools_placeholder(config: BackupListenerConfig) -> BackupStewardTools:
    return BackupStewardTools(  # pragma: no cover - constructed for auth only
        registry=None,  # type: ignore[arg-type]
        runtime={},
        observers=None,  # type: ignore[arg-type]
        ledger=None,  # type: ignore[arg-type]
        store=None,  # type: ignore[arg-type]
        executor=None,  # type: ignore[arg-type]
        now=lambda: datetime.now(timezone.utc),
        request_id=_opaque_id,
        secrets=[],
    )


class _BackupGateASGI:
    def __init__(self, app: Callable[..., Any], gate: _BackupRequestGate, adapter: BackupAdapter):
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
        if scope.get("path") == "/mcp" and _invalid_backup_tool_request(body):
            await grokbot_mcp._reject_tool(send, body)
            return
        await self.app(scope, grokbot_mcp._replay_messages(messages, receive), send)


def _invalid_backup_tool_request(body: bytes) -> bool:
    from .contracts import (
        parse_execute_input,
        parse_operation_input,
        parse_overview_input,
        parse_propose_input,
        parse_target_input,
    )

    try:
        request = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    if isinstance(request, list):
        return True
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
        "backup_overview": parse_overview_input,
        "backup_target_status": parse_target_input,
        "backup_restore_readiness": parse_target_input,
        "backup_operation_status": parse_operation_input,
        "backup_propose_action": parse_propose_input,
        "backup_execute_action": parse_execute_input,
    }[name]
    try:
        parser(arguments if arguments is not None else {})
    except BackupError:
        return True
    return False


def _health_check(config: BackupListenerConfig, bearer: str, timeout: int) -> bool:
    payload = grokbot_ops._request_json(
        f"http://{grokbot_ops._connect_host(config.bind_host)}:{config.bind_port}/health",
        bearer,
        timeout,
        method="GET",
    )
    return payload is not None and payload.get("ok") is True and payload.get("service") == SERVICE_NAME
