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
from .contracts import PACK_ID, TOOLS, WazuhError
from .store import WazuhStore, read_secure_text
from .tools import WazuhTriageTools

MAX_REQUEST_BYTES = 16_384
SERVICE_NAME = "grokbot-wazuh-triage"
WAZUH_PATH_KEYS = ("runtime_path", "ledger_path", "action_state_path", "approval_dir")
RUNTIME_SCHEMA = "brigade.grokbot.wazuh-runtime.v1"
_TOOL_DESCRIPTIONS = {
    "wazuh_ingest": "Ingest a bounded Wazuh alert batch",
    "wazuh_alert_status": "Read current Wazuh alert counts",
    "wazuh_classify": "Read one alert classification",
    "wazuh_incident_bundle": "Read grouped public Wazuh findings",
    "wazuh_propose_remediation": "Propose remediation for an escalated finding",
    "wazuh_action_status": "Read opaque remediation proposal state",
}


@dataclass(frozen=True)
class WazuhListenerConfig:
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
        return f"WazuhListenerConfig(bind_host={self.bind_host!r}, bind_port={self.bind_port})"


def _environment_invalid() -> NoReturn:
    raise WazuhError("invalid_request", "Wazuh environment is invalid")


def normalize_absolute_path(path_value: str) -> str:
    trimmed = os.path.normpath(path_value).rstrip("/")
    return trimmed if trimmed else "/"


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
    values = list(paths.values())
    for left_index, left in enumerate(values):
        for right in values[left_index + 1 :]:
            if left == right or left.startswith(f"{right}/") or right.startswith(f"{left}/"):
                _environment_invalid()
    return paths


def _lstat_nofollow(path_text: str, *, directory: bool = False) -> os.stat_result:
    path = Path(path_text)
    parent = -1
    descriptor = -1
    try:
        parent = grokbot_ops._open_parent_nofollow(path, create=False)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        if directory:
            flags |= getattr(os, "O_DIRECTORY", 0)
        if os.name == "posix":
            descriptor = os.open(path.name, flags, dir_fd=parent)
        else:
            from ..work_cmd import nt_dirfd

            if directory:
                descriptor = nt_dirfd.open_child_directory(parent, path.name, writable=False)
            else:
                descriptor = nt_dirfd.open_file(parent, path.name, os.O_RDONLY)
        info = os.fstat(descriptor)
    except OSError as exc:
        raise WazuhError("invalid_request", "Wazuh environment is invalid") from exc
    finally:
        if descriptor != -1:
            os.close(descriptor)
        if parent != -1:
            os.close(parent)
    if stat.S_ISLNK(info.st_mode):
        _environment_invalid()
    return info


def validate_runtime_file(path_text: str) -> str:
    normalized = validate_absolute_reference(path_text)
    try:
        read_secure_text(normalized)
    except (OSError, WazuhError) as exc:
        raise WazuhError("invalid_request", "Wazuh environment is invalid") from exc
    return normalized


def validate_state_directory(path_text: str, *, must_exist: bool) -> str:
    normalized = validate_absolute_reference(path_text)
    path = Path(normalized)
    if not path.exists():
        if must_exist:
            _environment_invalid()
        return normalized
    info = _lstat_nofollow(normalized, directory=True)
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


def _load_runtime(target: Path) -> tuple[WazuhListenerConfig, str]:
    from .. import grokbot_packs

    payload = _load_validated_instance(target)
    host, port = grokbot_packs._parse_default_bind(str(payload["bind"]))
    bearer = _resolve_bearer(payload["bearer"])
    config = WazuhListenerConfig(
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


def _owner_from_runtime(runtime_path: str, default: Path) -> Path:
    try:
        text = read_secure_text(validate_absolute_reference(runtime_path))
    except (OSError, WazuhError) as exc:
        raise WazuhError("invalid_request", "Wazuh environment is invalid") from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise WazuhError("invalid_request", "Wazuh environment is invalid") from exc
    if not isinstance(payload, dict):
        _environment_invalid()
    if not payload:
        return default
    if set(payload) != {"schema", "owner_path"} or payload.get("schema") != RUNTIME_SCHEMA:
        _environment_invalid()
    return Path(validate_absolute_reference(payload.get("owner_path")))


def build_tools_from_config(
    config: WazuhListenerConfig,
    *,
    env: Mapping[str, str] | None = None,
    now: Callable[[], datetime] | None = None,
) -> WazuhTriageTools:
    source = os.environ if env is None else env
    dispatch_token = source.get("GROKBOT_DISPATCH_TOKEN")
    if dispatch_token is not None and dispatch_token == config.bearer:
        _environment_invalid()
    runtime_path = validate_runtime_file(config.runtime_path)
    validate_state_directory(config.action_state_path, must_exist=False)
    validate_state_directory(config.approval_dir, must_exist=True)
    owner = _owner_from_runtime(runtime_path, config.target)
    store = WazuhStore(config.ledger_path)
    store.ready()
    secrets = [
        config.bearer,
        config.runtime_path,
        config.ledger_path,
        config.action_state_path,
        config.approval_dir,
        *([dispatch_token] if dispatch_token else []),
    ]
    return WazuhTriageTools(
        store=store,
        target=config.target,
        owner=owner,
        now=now or (lambda: datetime.now(timezone.utc)),
        request_id=_opaque_id,
        create_proposal_id=_opaque_id,
        secrets=secrets,
    )


class WazuhAdapter:
    def __init__(self, config: WazuhListenerConfig, tools: WazuhTriageTools):
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
        except WazuhError as exc:
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
    except (WazuhError, grokbot_packs.PackError, grokbot_mcp.ConfigurationError, OSError, ValueError, KeyError):
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
        "Description=Brigade Grok Bot MCP listener (wazuh-triage)\n"
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


def build_app(config: WazuhListenerConfig, tools: WazuhTriageTools) -> Callable[..., Any]:
    MCPServer, JSONResponse, _, TransportSecuritySettings = grokbot_mcp._load_mcp()
    adapter = WazuhAdapter(config, tools)
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
    return _WazuhGateASGI(app, _WazuhRequestGate(config), adapter)


def run_listener(config: WazuhListenerConfig, tools: WazuhTriageTools) -> None:
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
) -> tuple[WazuhListenerConfig, WazuhTriageTools]:
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
    config = WazuhListenerConfig(
        target=target.expanduser().resolve(),
        bind_host=host,
        bind_port=port,
        allowed_hosts=tuple(allowed_hosts),
        allowed_origins=tuple(allowed_origins),
        bearer=bearer,
        **paths,
    )
    return config, build_tools_from_config(config)


def _register_tools(server: Any, adapter: WazuhAdapter) -> None:
    def wazuh_ingest(alerts: list[dict[str, Any]]) -> dict[str, Any]:
        return adapter.call_tool("wazuh_ingest", {"alerts": alerts})

    def wazuh_alert_status() -> dict[str, Any]:
        return adapter.call_tool("wazuh_alert_status", {})

    def wazuh_classify(fingerprint: str) -> dict[str, Any]:
        return adapter.call_tool("wazuh_classify", {"fingerprint": fingerprint})

    def wazuh_incident_bundle(scope: str) -> dict[str, Any]:
        return adapter.call_tool("wazuh_incident_bundle", {"scope": scope})

    def wazuh_propose_remediation(finding_id: str) -> dict[str, Any]:
        return adapter.call_tool("wazuh_propose_remediation", {"finding_id": finding_id})

    def wazuh_action_status(proposal_id: str) -> dict[str, Any]:
        return adapter.call_tool("wazuh_action_status", {"proposal_id": proposal_id})

    for name, handler in (
        ("wazuh_ingest", wazuh_ingest),
        ("wazuh_alert_status", wazuh_alert_status),
        ("wazuh_classify", wazuh_classify),
        ("wazuh_incident_bundle", wazuh_incident_bundle),
        ("wazuh_propose_remediation", wazuh_propose_remediation),
        ("wazuh_action_status", wazuh_action_status),
    ):
        handler.__name__ = name
        handler.__doc__ = _TOOL_DESCRIPTIONS[name]
        server.tool(name=name, description=_TOOL_DESCRIPTIONS[name])(handler)


class _WazuhRequestGate:
    def __init__(self, config: WazuhListenerConfig, *, max_request_bytes: int = MAX_REQUEST_BYTES):
        self.config = config
        self.max_request_bytes = max_request_bytes
        self._adapter = WazuhAdapter(config, config_tools_placeholder(config))

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


def config_tools_placeholder(config: WazuhListenerConfig) -> WazuhTriageTools:
    return WazuhTriageTools(  # pragma: no cover - constructed for auth only
        store=None,  # type: ignore[arg-type]
        target=config.target,
        owner=config.target,
        now=lambda: datetime.now(timezone.utc),
        request_id=_opaque_id,
        create_proposal_id=_opaque_id,
        secrets=[],
    )


class _WazuhGateASGI:
    def __init__(self, app: Callable[..., Any], gate: _WazuhRequestGate, adapter: WazuhAdapter):
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
        if scope.get("path") == "/mcp" and _invalid_wazuh_tool_request(body):
            await grokbot_mcp._reject_tool(send, body)
            return
        await self.app(scope, grokbot_mcp._replay_messages(messages, receive), send)


def _invalid_wazuh_tool_request(body: bytes) -> bool:
    from .contracts import (
        parse_action_status_input,
        parse_alert_status_input,
        parse_classify_input,
        parse_incident_input,
        parse_ingest_input,
        parse_propose_input,
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
        "wazuh_ingest": parse_ingest_input,
        "wazuh_alert_status": parse_alert_status_input,
        "wazuh_classify": parse_classify_input,
        "wazuh_incident_bundle": parse_incident_input,
        "wazuh_propose_remediation": parse_propose_input,
        "wazuh_action_status": parse_action_status_input,
    }[name]
    try:
        parser(arguments if arguments is not None else {})
    except WazuhError:
        return True
    return False


def _health_check(config: WazuhListenerConfig, bearer: str, timeout: int) -> bool:
    payload = grokbot_ops._request_json(
        f"http://{grokbot_ops._connect_host(config.bind_host)}:{config.bind_port}/health",
        bearer,
        timeout,
        method="GET",
    )
    return payload is not None and payload.get("ok") is True and payload.get("service") == SERVICE_NAME
