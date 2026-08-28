"""Pack lifecycle, listener, doctor, canary, and unit rendering."""

from __future__ import annotations

import json
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, NoReturn

from .. import grokbot_mcp, grokbot_ops
from .actions import ObsidianExecutor
from .adapters import ExcalidrawAdapter, NativeMcpClient, NativeMcpPort, allowlisted_excalidraw_env
from .excalidraw_client import create_excalidraw_stdio_client
from .capabilities import reconcile_capabilities
from .contracts import (
    ERROR_MESSAGES,
    PACK_ID,
    PUBLIC_ROUTE,
    TOOLS,
    ObsidianError,
    parse_action_status_input,
    parse_capabilities_input,
    parse_capabilities_result,
    parse_execute_input,
    parse_propose_input,
    parse_read_input,
    parse_search_input,
)
from .native_client import create_native_mcp_client
from .path_policy import vault_path_policy_from_runtime
from .runtime_config import (
    adapter_environment,
    assert_disjoint_paths,
    normalize_absolute_path,
    parse_runtime_json_text,
    read_secure_runtime_text,
    required_upstream_url,
    validate_regular_executable,
)
from .store import ObsidianActionStore
from .tools import ObsidianTools

MAX_REQUEST_BYTES = 16_384
SERVICE_NAME = "grokbot-obsidian-operator"
OBSIDIAN_PATH_KEYS = ("runtime_path", "action_state_path", "approval_dir", "staging_dir", "excalidraw_bin")
PEER_TOKEN_ENVS = (
    "GROKBOT_DISPATCH_TOKEN",
    "GROKBOT_FLEET_TOKEN",
    "GROKBOT_CEREBRO_TOKEN",
    "GROKBOT_BACKUP_TOKEN",
    "GROKBOT_OBSIDIAN_TOKEN",
)
_TOOL_DESCRIPTIONS = {
    "obsidian_capabilities": "Read generic Phase 1 capability projection",
    "obsidian_search": "Search vault notes through the bounded native backend",
    "obsidian_read": "Read one vault-relative note or structured target",
    "obsidian_action_status": "Read one action proposal status",
    "obsidian_propose_action": "Propose a Phase 1 action that still requires approval",
    "obsidian_execute_action": "Execute an approved Phase 1 action proposal",
}
_PARSERS = {
    "obsidian_capabilities": parse_capabilities_input,
    "obsidian_search": parse_search_input,
    "obsidian_read": parse_read_input,
    "obsidian_action_status": parse_action_status_input,
    "obsidian_propose_action": parse_propose_input,
    "obsidian_execute_action": parse_execute_input,
}


@dataclass(frozen=True)
class ObsidianListenerConfig:
    target: Path
    bind_host: str
    bind_port: int
    allowed_hosts: tuple[str, ...]
    allowed_origins: tuple[str, ...]
    bearer: str
    runtime_path: str
    action_state_path: str
    approval_dir: str
    staging_dir: str
    excalidraw_bin: str
    upstream_url: str
    upstream_key: Mapping[str, str]

    def __repr__(self) -> str:
        return f"ObsidianListenerConfig(bind_host={self.bind_host!r}, bind_port={self.bind_port})"


def _environment_invalid() -> NoReturn:
    raise ObsidianError("invalid_request", "Obsidian environment is invalid")


def validate_absolute_reference(path_text: object) -> str:
    if not isinstance(path_text, str) or not path_text or "\0" in path_text:
        _environment_invalid()
    if not path_text.startswith("/") or any(part in {".", ".."} for part in path_text.split("/")):
        _environment_invalid()
    return normalize_absolute_path(path_text)


def validate_disjoint_state_paths(
    runtime_path: str,
    action_state_path: str,
    approval_dir: str,
    staging_dir: str,
    excalidraw_bin: str,
) -> dict[str, str]:
    paths = {
        "runtime_path": validate_absolute_reference(runtime_path),
        "action_state_path": validate_absolute_reference(action_state_path),
        "approval_dir": validate_absolute_reference(approval_dir),
        "staging_dir": validate_absolute_reference(staging_dir),
        "excalidraw_bin": validate_regular_executable(validate_absolute_reference(excalidraw_bin)),
    }
    assert_disjoint_paths(
        [paths["runtime_path"], paths["action_state_path"], paths["approval_dir"], paths["staging_dir"]]
    )
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
        raise ObsidianError("invalid_request", "Obsidian environment is invalid") from exc
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


def _load_validated_instance(target: Path) -> dict[str, Any]:
    from .. import grokbot_packs

    payload = grokbot_packs._load_instance_config(target, PACK_ID)
    paths = validate_disjoint_state_paths(
        str(payload["runtime_path"]),
        str(payload["action_state_path"]),
        str(payload["approval_dir"]),
        str(payload["staging_dir"]),
        str(payload["excalidraw_bin"]),
    )
    grokbot_packs._parse_default_bind(str(payload["bind"]))
    grokbot_packs._validate_bearer_reference(payload["bearer"])
    validated = dict(payload)
    validated.update(paths)
    validated["upstream_url"] = required_upstream_url(payload.get("upstream_url"))
    validated["upstream_key"] = grokbot_packs._validate_bearer_reference(payload.get("upstream_key"))
    return validated


def _resolve_bearer(reference: Mapping[str, str]) -> str:
    bearer = grokbot_ops._resolve_bearer(dict(reference))
    if len(bearer) < 32:
        _environment_invalid()
    return bearer


def _load_runtime(target: Path) -> tuple[ObsidianListenerConfig, str]:
    from .. import grokbot_packs

    payload = _load_validated_instance(target)
    host, port = grokbot_packs._parse_default_bind(str(payload["bind"]))
    bearer = _resolve_bearer(payload["bearer"])
    config = ObsidianListenerConfig(
        target=target,
        bind_host=host,
        bind_port=port,
        allowed_hosts=tuple(payload["allowed_hosts"]),
        allowed_origins=tuple(payload["allowed_origins"]),
        bearer=bearer,
        runtime_path=payload["runtime_path"],
        action_state_path=payload["action_state_path"],
        approval_dir=payload["approval_dir"],
        staging_dir=payload["staging_dir"],
        excalidraw_bin=payload["excalidraw_bin"],
        upstream_url=payload["upstream_url"],
        upstream_key=payload["upstream_key"],
    )
    return config, bearer


def _resolve_upstream_key(reference: Mapping[str, str]) -> str:
    from .. import grokbot_mcp, grokbot_ops

    try:
        key = grokbot_ops._resolve_bearer(dict(reference))
    except grokbot_mcp.ConfigurationError as exc:
        raise ObsidianError("invalid_request", "Obsidian environment is invalid") from exc
    if len(key) < 16:
        _environment_invalid()
    return key


def _assert_distinct_secrets(value: str, *peers: str | None) -> None:
    for peer in peers:
        if peer and value == peer:
            _environment_invalid()


def _peer_tokens(source: Mapping[str, str]) -> tuple[str, ...]:
    return tuple(source[name] for name in PEER_TOKEN_ENVS if name in source and source[name])


def close_native_client(tools: Any) -> None:
    native = getattr(tools, "native", None)
    closer = getattr(native, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception:
            return


def build_tools_from_config(
    config: ObsidianListenerConfig,
    *,
    native: NativeMcpPort | None = None,
    native_client_factory: Callable[..., NativeMcpClient] | None = None,
    excalidraw_client_factory: Callable[..., Any] | None = None,
    executor: Any | None = None,
    env: Mapping[str, str] | None = None,
) -> ObsidianTools:
    source = os.environ if env is None else env
    dispatch_token = source.get("GROKBOT_DISPATCH_TOKEN")
    if dispatch_token is not None and dispatch_token == config.bearer:
        _environment_invalid()
    runtime_path = validate_runtime_file(config.runtime_path)
    validate_state_directory(config.action_state_path, must_exist=False)
    validate_state_directory(config.approval_dir, must_exist=True)
    validate_state_directory(config.staging_dir, must_exist=False)
    private_runtime = parse_runtime_json_text(read_secure_runtime_text(runtime_path))
    policy = vault_path_policy_from_runtime(private_runtime)
    target_config = {
        "flashcardNote": private_runtime["flashcard_note"],
        "excalidrawSuffix": private_runtime["excalidraw"]["verified_suffix"],
    }
    store = ObsidianActionStore(
        action_state_path=config.action_state_path,
        approval_dir=config.approval_dir,
        target_config=target_config,
    )
    store.ready()
    if native is None:
        upstream_key = _resolve_upstream_key(config.upstream_key)
        _assert_distinct_secrets(upstream_key, config.bearer, *_peer_tokens(source))
        factory = native_client_factory or create_native_mcp_client
        native_port = NativeMcpPort(
            factory(
                url=required_upstream_url(config.upstream_url),
                api_key=upstream_key,
                ca_bytes=private_runtime["upstream_tls"]["ca_bytes"],
                pins=private_runtime["upstream_tls"]["spki_sha256"],
            ),
            policy=policy,
        )
    else:
        native_port = native
        if isinstance(native_port, NativeMcpPort):
            native_port.policy = dict(policy)
    if executor is None:
        executor = ObsidianExecutor(
            native=native_port,
            policy=policy,
            flashcard_note=private_runtime["flashcard_note"],
            flashcard_heading=private_runtime["flashcard_heading"],
            templates=private_runtime["templates"]["catalog"],
            excalidraw=ExcalidrawAdapter(
                bin_path=config.excalidraw_bin,
                staging_dir=config.staging_dir,
                native=native_port,
                policy=policy,
                read_proof=lambda: {
                    "enabled": private_runtime["excalidraw"]["enabled"],
                    "verified_suffix": private_runtime["excalidraw"]["verified_suffix"],
                    "probe_receipt_sha256": private_runtime["excalidraw"]["probe_receipt_sha256"],
                    "receipt": private_runtime["excalidraw"]["probe_receipt_bytes"] or b"",
                },
                start_client=lambda _spec: (excalidraw_client_factory or create_excalidraw_stdio_client)(
                    executable=config.excalidraw_bin,
                    staging_dir=config.staging_dir,
                    env=allowlisted_excalidraw_env(adapter_environment(source)),
                ),
                env=adapter_environment(source),
            ),
        )
    return ObsidianTools(
        native=native_port,
        policy=policy,
        store=store,
        reconcile=lambda: reconcile_capabilities(private_runtime, native_port.command_list),
        target_config=target_config,
        templates=private_runtime["templates"]["catalog"],
        executor=executor,
    )


class _UnavailableNative:
    def call_tool(self, name: str, arguments: Mapping[str, Any] | None = None) -> object:
        raise ObsidianError("unavailable", "Obsidian observation is unavailable")

    def close(self) -> None:
        return None


class ObsidianAdapter:
    def __init__(self, config: ObsidianListenerConfig, tools: ObsidianTools):
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
        except ObsidianError as exc:
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
    except (ObsidianError, grokbot_packs.PackError, grokbot_mcp.ConfigurationError, OSError, ValueError, KeyError):
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
    try:
        capabilities = _call_capabilities_tool(base, bearer, wait)
    except ObsidianError:
        result["reason"] = "capabilities"
        return result
    if capabilities is None:
        result["reason"] = "capabilities"
        return result
    try:
        parsed = parse_capabilities_result(capabilities)
    except ObsidianError:
        result["reason"] = "capabilities"
        return result
    if parsed.get("phase") != "phase1" or parsed.get("search_backend") != "native_bounded_search":
        result["reason"] = "capabilities"
        return result
    result.update(
        {
            "ok": True,
            "health": health,
            "auth_rejected_without_bearer": True,
            "tools": sorted(TOOLS),
            "capabilities": {
                "phase": parsed["phase"],
                "search_backend": parsed["search_backend"],
            },
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
    args += ["--upstream-url", instance["upstream_url"]]
    upstream_key = instance["upstream_key"]
    if upstream_key["kind"] == "file":
        args += ["--upstream-key-file", upstream_key["path"]]
    elif upstream_key["kind"] == "env":
        args += ["--upstream-key-env", upstream_key["name"]]
    exec_start = " ".join(grokbot_ops._systemd_quote(argument) for argument in args)
    writable = " ".join(
        grokbot_ops._systemd_quote(path) for path in (instance["action_state_path"], instance["staging_dir"])
    )
    return (
        "# Generated by brigade run cloud grokbot pack install-service.\n"
        f"# Unit: {grokbot_ops.unit_name(PACK_ID)}\n"
        "[Unit]\n"
        "Description=Brigade Grok Bot MCP listener (obsidian-operator)\n"
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


def build_app(config: ObsidianListenerConfig, tools: ObsidianTools) -> Callable[..., Any]:
    MCPServer, JSONResponse, _, TransportSecuritySettings = grokbot_mcp._load_mcp()
    adapter = ObsidianAdapter(config, tools)
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
    return _ObsidianGateASGI(app, _ObsidianRequestGate(config), adapter)


def run_listener(config: ObsidianListenerConfig, tools: ObsidianTools) -> None:
    grokbot_mcp._load_mcp()
    try:
        import uvicorn
    except ImportError as exc:
        raise grokbot_mcp.OptionalDependencyError() from exc
    try:
        uvicorn.run(
            build_app(config, tools),
            host=config.bind_host,
            port=config.bind_port,
            access_log=False,
            log_level="warning",
        )
    finally:
        close_native_client(tools)


def build_listener_from_target(
    target: Path,
    *,
    bind: str | None,
    allowed_hosts: list[str],
    allowed_origins: list[str],
    bearer_file: Path | None,
    bearer_env: str | None,
    upstream_url: str | None = None,
    upstream_key_file: Path | None = None,
    upstream_key_env: str | None = None,
) -> tuple[ObsidianListenerConfig, ObsidianTools]:
    from .. import grokbot_packs

    instance = grokbot_packs._load_instance_config(target, PACK_ID)
    chosen_bind = bind or instance["bind"]
    host, port = grokbot_packs._parse_default_bind(chosen_bind)
    paths = validate_disjoint_state_paths(
        str(instance["runtime_path"]),
        str(instance["action_state_path"]),
        str(instance["approval_dir"]),
        str(instance["staging_dir"]),
        str(instance["excalidraw_bin"]),
    )
    bearer = grokbot_mcp.load_bearer(bearer_file=bearer_file, bearer_env=bearer_env)
    if len(bearer) < 32:
        _environment_invalid()
    dispatch_token = os.environ.get("GROKBOT_DISPATCH_TOKEN")
    if dispatch_token is not None and bearer == dispatch_token:
        _environment_invalid()
    if upstream_key_file is not None or upstream_key_env is not None:
        upstream_key = grokbot_packs._bearer_reference(
            bearer_env=upstream_key_env,
            bearer_file=upstream_key_file,
            bearer=None,
        )
    else:
        upstream_key = grokbot_packs._validate_bearer_reference(instance["upstream_key"])
    config = ObsidianListenerConfig(
        target=target.expanduser().resolve(),
        bind_host=host,
        bind_port=port,
        allowed_hosts=tuple(allowed_hosts),
        allowed_origins=tuple(allowed_origins),
        bearer=bearer,
        upstream_url=required_upstream_url(upstream_url or instance["upstream_url"]),
        upstream_key=upstream_key,
        **paths,
    )
    return config, build_tools_from_config(config)


def _register_tools(server: Any, adapter: ObsidianAdapter) -> None:
    def obsidian_capabilities() -> dict[str, Any]:
        return adapter.call_tool("obsidian_capabilities", {})

    def obsidian_search(query: str, limit: int = 5) -> dict[str, Any]:
        return adapter.call_tool("obsidian_search", {"query": query, "limit": limit})

    def obsidian_read(path: str, target: dict[str, Any] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"path": path}
        if target is not None:
            payload["target"] = target
        return adapter.call_tool("obsidian_read", payload)

    def obsidian_action_status(action_id: str) -> dict[str, Any]:
        return adapter.call_tool("obsidian_action_status", {"action_id": action_id})

    def obsidian_propose_action(request_id: str, action: dict[str, Any]) -> dict[str, Any]:
        return adapter.call_tool("obsidian_propose_action", {"request_id": request_id, "action": action})

    def obsidian_execute_action(action_id: str, approval_receipt: str) -> dict[str, Any]:
        return adapter.call_tool(
            "obsidian_execute_action",
            {"action_id": action_id, "approval_receipt": approval_receipt},
        )

    for name, handler in (
        ("obsidian_capabilities", obsidian_capabilities),
        ("obsidian_search", obsidian_search),
        ("obsidian_read", obsidian_read),
        ("obsidian_action_status", obsidian_action_status),
        ("obsidian_propose_action", obsidian_propose_action),
        ("obsidian_execute_action", obsidian_execute_action),
    ):
        handler.__name__ = name
        handler.__doc__ = _TOOL_DESCRIPTIONS[name]
        server.tool(name=name, description=_TOOL_DESCRIPTIONS[name])(handler)


class _ObsidianRequestGate:
    def __init__(self, config: ObsidianListenerConfig, *, max_request_bytes: int = MAX_REQUEST_BYTES):
        self.config = config
        self.max_request_bytes = max_request_bytes
        self._adapter = ObsidianAdapter(config, config_tools_placeholder(config))

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


def config_tools_placeholder(config: ObsidianListenerConfig) -> ObsidianTools:
    return ObsidianTools(  # pragma: no cover - constructed for auth only
        native=NativeMcpPort(_UnavailableNative()),
        policy={},
        store=ObsidianActionStore(config.action_state_path, config.approval_dir),
    )


class _ObsidianGateASGI:
    def __init__(self, app: Callable[..., Any], gate: _ObsidianRequestGate, adapter: ObsidianAdapter):
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
        if scope.get("path") == PUBLIC_ROUTE and _invalid_obsidian_tool_request(body):
            await grokbot_mcp._reject_tool(send, body)
            return
        await self.app(scope, grokbot_mcp._replay_messages(messages, receive), send)


def _invalid_obsidian_tool_request(body: bytes) -> bool:
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
    try:
        _PARSERS[name](arguments if arguments is not None else {})
    except ObsidianError:
        return True
    return False


def _call_capabilities_tool(base: str, bearer: str, timeout: int) -> dict[str, Any] | None:
    try:
        import asyncio

        return asyncio.run(asyncio.wait_for(_call_capabilities_tool_async(base, bearer), timeout=timeout))
    except ObsidianError:
        raise
    except Exception:
        return None


async def _call_capabilities_tool_async(base: str, bearer: str) -> dict[str, Any]:
    client_session, streamable_http_client, async_client = grokbot_ops._mcp_client_components()
    async with async_client(headers={"Authorization": f"Bearer {bearer}"}, trust_env=False) as http_client:
        async with streamable_http_client(f"{base}{PUBLIC_ROUTE}", http_client=http_client) as streams:
            read_stream, write_stream = streams
            async with client_session(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool("obsidian_capabilities", {})
    payload = result.structuredContent if getattr(result, "structuredContent", None) else None
    if isinstance(payload, dict):
        if "error" in payload:
            raise ObsidianError("unavailable", ERROR_MESSAGES["unavailable"])
        return payload
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                if "error" in parsed:
                    raise ObsidianError("unavailable", ERROR_MESSAGES["unavailable"])
                return parsed
    raise ObsidianError("unavailable", ERROR_MESSAGES["unavailable"])


def _health_check(config: ObsidianListenerConfig, bearer: str, timeout: int) -> bool:
    payload = grokbot_ops._request_json(
        f"http://{grokbot_ops._connect_host(config.bind_host)}:{config.bind_port}/health",
        bearer,
        timeout,
        method="GET",
    )
    return payload is not None and payload.get("ok") is True and payload.get("service") == SERVICE_NAME
