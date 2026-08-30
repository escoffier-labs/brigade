"""First-party operations-relay connector: submit one finding, read delivery state."""

from __future__ import annotations

import hmac
import json
import os
import stat
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from . import grokbot_findings, grokbot_findings_relay, grokbot_mcp, grokbot_ops

PACK_ID = "operations-relay"
DEFAULT_BIND = "127.0.0.1:8777"
SERVICE_NAME = "grokbot-operations-relay"
TOOLS = frozenset({"automation_finding_status", "submit_automation_finding"})
SUBMIT_KEYS = grokbot_findings.REQUIRED_ENTRY_KEYS
STATUS_KEYS = frozenset({"producer", "finding_id", "revision"})
FORBIDDEN_TOOL_KEYS = frozenset(
    {
        "argv",
        "bearer",
        "command",
        "credential",
        "cwd",
        "dest",
        "destination",
        "executable",
        "file",
        "http",
        "https",
        "memory",
        "owner",
        "password",
        "path",
        "secret",
        "target",
        "token",
        "url",
    }
)
ERROR_MESSAGES = {
    "invalid_request": "Tool input failed validation",
    "unavailable": "Operations relay is unavailable",
}
MAX_REQUEST_BYTES = 16_384
PUBLIC_RESULT_LIMIT = 131_072
_TOOL_DESCRIPTIONS = {
    "submit_automation_finding": "Submit one automation finding for owner review",
    "automation_finding_status": "Read opaque delivery state for one finding",
}
FINDINGS_VALIDATION = frozenset(
    {
        "digest-mismatch",
        "invalid-body",
        "invalid-content-digest",
        "invalid-entry",
        "invalid-finding-id",
        "invalid-observed-at",
        "invalid-producer",
        "invalid-revision",
        "invalid-severity",
        "invalid-source-digest",
        "invalid-source-ref",
        "invalid-title",
    }
)


class OperationsRelayError(ValueError):
    """Stable public operations-relay failure. Messages never include paths or secrets."""

    def __init__(self, reason: str):
        self.reason = reason
        self.code = reason
        self.message = ERROR_MESSAGES[reason]
        super().__init__(self.message)

    def public_error(self) -> dict[str, dict[str, str]]:
        return {"error": {"code": self.reason, "message": ERROR_MESSAGES[self.reason]}}

    def __repr__(self) -> str:
        return f"OperationsRelayError({self.reason!r})"


@dataclass(frozen=True)
class OperationsRelayListenerConfig:
    target: Path
    bind_host: str
    bind_port: int
    allowed_hosts: tuple[str, ...]
    allowed_origins: tuple[str, ...]
    bearer: str
    owner_workspace: str

    def __repr__(self) -> str:
        return f"OperationsRelayListenerConfig(bind_host={self.bind_host!r}, bind_port={self.bind_port})"


def validate_owner_workspace(owner: object) -> str:
    if not isinstance(owner, (str, Path)) or (isinstance(owner, str) and not owner):
        raise OperationsRelayError("invalid_request")
    path = Path(owner)
    if not path.is_absolute() or "\x00" in str(path):
        raise OperationsRelayError("invalid_request")
    try:
        info = path.lstat()
    except OSError as exc:
        raise OperationsRelayError("invalid_request") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise OperationsRelayError("invalid_request")
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise OperationsRelayError("invalid_request")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise OperationsRelayError("invalid_request")
    try:
        validated = grokbot_findings._validate_owner(path)
    except grokbot_findings.FindingsError as exc:
        raise OperationsRelayError("invalid_request") from exc
    return str(validated)


def parse_submit_input(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict) or set(raw) & FORBIDDEN_TOOL_KEYS:
        raise OperationsRelayError("invalid_request")
    if set(raw) != SUBMIT_KEYS:
        raise OperationsRelayError("invalid_request")
    try:
        return grokbot_findings._validate_entry(raw, index=0)
    except grokbot_findings.FindingsError as exc:
        raise OperationsRelayError("invalid_request") from exc


def parse_status_input(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict) or set(raw) & FORBIDDEN_TOOL_KEYS:
        raise OperationsRelayError("invalid_request")
    if set(raw) != STATUS_KEYS:
        raise OperationsRelayError("invalid_request")
    try:
        return {
            "producer": grokbot_findings._bounded_identifier(raw["producer"], reason="invalid-producer", index=0),
            "finding_id": grokbot_findings._bounded_identifier(raw["finding_id"], reason="invalid-finding-id", index=0),
            "revision": grokbot_findings._bounded_identifier(raw["revision"], reason="invalid-revision", index=0),
        }
    except grokbot_findings.FindingsError as exc:
        raise OperationsRelayError("invalid_request") from exc


class OperationsRelayTools:
    """Closed public operations-relay tool surface."""

    def __init__(
        self,
        *,
        target: Path,
        owner: Path,
        now: Callable[[], datetime] | None = None,
        secrets: list[str] | None = None,
    ):
        self.target = Path(target)
        self.owner = Path(owner)
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.secrets = list(secrets or [])

    @classmethod
    def placeholder(cls, config: OperationsRelayListenerConfig) -> "OperationsRelayTools":
        return cls(target=config.target, owner=Path(config.owner_workspace))

    def inventory(self) -> list[str]:
        return sorted(TOOLS)

    def call_tool(self, name: str, arguments: object) -> dict[str, Any]:
        handlers = {
            "submit_automation_finding": self.submit_automation_finding,
            "automation_finding_status": self.automation_finding_status,
        }
        if name not in handlers:
            raise OperationsRelayError("invalid_request")
        result = handlers[name](arguments)
        encoded = json.dumps(result, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > PUBLIC_RESULT_LIMIT:
            raise OperationsRelayError("unavailable")
        return result

    def submit_automation_finding(self, raw: object) -> dict[str, Any]:
        entry = parse_submit_input(raw)
        handle = grokbot_findings.identity_digest(entry["producer"], entry["finding_id"], entry["revision"])
        try:
            delivered = grokbot_findings_relay.relay_apply(
                [entry],
                self.target,
                self.owner,
                limit=1,
                now=self.now(),
            )
        except grokbot_findings.FindingsError as exc:
            if exc.reason in FINDINGS_VALIDATION:
                raise OperationsRelayError("invalid_request") from exc
            raise OperationsRelayError("unavailable") from exc
        return {
            "handle": handle,
            "created": delivered["created"],
            "known": delivered["known"],
            "reported": delivered["reported"],
            "pending": delivered["pending"],
            "state": _delivery_state(self.target, handle),
        }

    def automation_finding_status(self, raw: object) -> dict[str, Any]:
        parsed = parse_status_input(raw)
        handle = grokbot_findings.identity_digest(parsed["producer"], parsed["finding_id"], parsed["revision"])
        return {"handle": handle, "state": _delivery_state(self.target, handle)}


class OperationsRelayAdapter:
    def __init__(self, config: OperationsRelayListenerConfig, tools: OperationsRelayTools):
        self.config = config
        self.tools = tools

    def authorized(self, authorization: str | None) -> bool:
        if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
            return False
        bearer = authorization[7:]
        if not bearer.isascii():
            return False
        try:
            return hmac.compare_digest(bearer, self.config.bearer)
        except ValueError:
            return False

    def health_payload(self) -> dict[str, object]:
        return {"ok": True, "service": SERVICE_NAME}

    def tool_inventory(self) -> list[dict[str, str]]:
        return [{"name": name, "description": _TOOL_DESCRIPTIONS[name]} for name in sorted(TOOLS)]

    def call_tool(self, name: str, arguments: object) -> dict[str, Any]:
        try:
            return self.tools.call_tool(name, arguments)
        except OperationsRelayError as exc:
            return exc.public_error()


def doctor(target: Path, *, timeout: int | None = None) -> list[dict[str, str]]:
    from . import grokbot_packs

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
    except (
        OperationsRelayError,
        grokbot_packs.PackError,
        grokbot_mcp.ConfigurationError,
        OSError,
        ValueError,
        KeyError,
    ):
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
            str((Path(target) / grokbot_ops.QUEUE_STATE_DIR).resolve()),
            instance["owner_workspace"],
        )
    )
    return (
        "# Generated by brigade run cloud grokbot pack install-service.\n"
        f"# Unit: {grokbot_ops.unit_name(PACK_ID)}\n"
        "[Unit]\n"
        "Description=Brigade Grok Bot MCP listener (operations-relay)\n"
        "After=network.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={exec_start}\n"
        "Restart=on-failure\n"
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


def build_app(config: OperationsRelayListenerConfig, tools: OperationsRelayTools) -> Callable[..., Any]:
    MCPServer, JSONResponse, _, TransportSecuritySettings = grokbot_mcp._load_mcp()
    adapter = OperationsRelayAdapter(config, tools)
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
    return _OperationsRelayGateASGI(app, _OperationsRelayRequestGate(config), adapter)


def run_listener(config: OperationsRelayListenerConfig, tools: OperationsRelayTools) -> None:
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
) -> tuple[OperationsRelayListenerConfig, OperationsRelayTools]:
    from . import grokbot_packs

    instance = grokbot_packs._load_instance_config(target, PACK_ID)
    chosen_bind = bind or instance["bind"]
    host, port = grokbot_packs._parse_default_bind(chosen_bind)
    owner = validate_owner_workspace(str(instance["owner_workspace"]))
    bearer = _require_isolated_bearer(grokbot_mcp.load_bearer(bearer_file=bearer_file, bearer_env=bearer_env))
    config = OperationsRelayListenerConfig(
        target=target.expanduser().resolve(),
        bind_host=host,
        bind_port=port,
        allowed_hosts=tuple(allowed_hosts),
        allowed_origins=tuple(allowed_origins),
        bearer=bearer,
        owner_workspace=owner,
    )
    return config, OperationsRelayTools(
        target=config.target,
        owner=Path(owner),
        secrets=[bearer, owner],
    )


def _delivery_state(target: Path, handle: str) -> str:
    try:
        for record in grokbot_findings_relay._read_outbox_records(target):
            if record.get("relay_id") == handle and record.get("status") in grokbot_findings_relay.OUTBOX_STATUSES:
                return str(record["status"])
        if _marker_exists(target, handle):
            return "delivered"
    except grokbot_findings.FindingsError:
        return "unknown"
    return "unknown"


def _marker_exists(target: Path, handle: str) -> bool:
    directory = grokbot_findings._open_findings_readonly(target)
    try:
        if directory is None:
            return False
        location: int | Path = directory.descriptor if directory.descriptor is not None else directory.path
        try:
            names = os.listdir(location)
        except OSError:
            return False
        return f"{handle}.json" in names
    finally:
        if directory is not None and directory.descriptor is not None:
            os.close(directory.descriptor)


def _load_validated_instance(target: Path) -> dict[str, Any]:
    from . import grokbot_packs

    payload = grokbot_packs._load_instance_config(target, PACK_ID)
    owner = validate_owner_workspace(str(payload["owner_workspace"]))
    grokbot_packs._parse_default_bind(str(payload["bind"]))
    grokbot_packs._validate_bearer_reference(payload["bearer"])
    validated = dict(payload)
    validated["owner_workspace"] = owner
    return validated


def _environment_invalid() -> None:
    raise OperationsRelayError("invalid_request")


def _require_isolated_bearer(bearer: str) -> str:
    if len(bearer) < 32:
        _environment_invalid()
    dispatch_token = os.environ.get("GROKBOT_DISPATCH_TOKEN")
    if dispatch_token is not None and bearer == dispatch_token:
        _environment_invalid()
    return bearer


def _resolve_bearer(reference: Mapping[str, str]) -> str:
    return _require_isolated_bearer(grokbot_ops._resolve_bearer(dict(reference)))


def _load_runtime(target: Path) -> tuple[OperationsRelayListenerConfig, str]:
    payload = _load_validated_instance(target)
    from . import grokbot_packs

    host, port = grokbot_packs._parse_default_bind(str(payload["bind"]))
    bearer = _resolve_bearer(payload["bearer"])
    config = OperationsRelayListenerConfig(
        target=target,
        bind_host=host,
        bind_port=port,
        allowed_hosts=tuple(payload["allowed_hosts"]),
        allowed_origins=tuple(payload["allowed_origins"]),
        bearer=bearer,
        owner_workspace=payload["owner_workspace"],
    )
    return config, bearer


def _register_tools(server: Any, adapter: OperationsRelayAdapter) -> None:
    def submit_automation_finding(
        producer: str,
        finding_id: str,
        revision: str,
        observed_at: str,
        severity: str,
        title: str,
        body: str,
        source_ref: str,
        source_digest: str,
        content_digest: str,
    ) -> dict[str, Any]:
        return adapter.call_tool(
            "submit_automation_finding",
            {
                "producer": producer,
                "finding_id": finding_id,
                "revision": revision,
                "observed_at": observed_at,
                "severity": severity,
                "title": title,
                "body": body,
                "source_ref": source_ref,
                "source_digest": source_digest,
                "content_digest": content_digest,
            },
        )

    def automation_finding_status(producer: str, finding_id: str, revision: str) -> dict[str, Any]:
        return adapter.call_tool(
            "automation_finding_status",
            {"producer": producer, "finding_id": finding_id, "revision": revision},
        )

    for name, handler in (
        ("submit_automation_finding", submit_automation_finding),
        ("automation_finding_status", automation_finding_status),
    ):
        handler.__name__ = name
        handler.__doc__ = _TOOL_DESCRIPTIONS[name]
        server.tool(name=name, description=_TOOL_DESCRIPTIONS[name])(handler)


class _OperationsRelayRequestGate:
    def __init__(self, config: OperationsRelayListenerConfig, *, max_request_bytes: int = MAX_REQUEST_BYTES):
        self.config = config
        self.max_request_bytes = max_request_bytes
        self._adapter = OperationsRelayAdapter(config, OperationsRelayTools.placeholder(config))

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


class _OperationsRelayGateASGI:
    def __init__(
        self,
        app: Callable[..., Any],
        gate: _OperationsRelayRequestGate,
        adapter: OperationsRelayAdapter,
    ):
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
        if scope.get("path") == "/mcp" and _invalid_tool_request(body):
            await grokbot_mcp._reject_tool(send, body)
            return
        await self.app(scope, grokbot_mcp._replay_messages(messages, receive), send)


def _invalid_tool_request(body: bytes) -> bool:
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
        "submit_automation_finding": parse_submit_input,
        "automation_finding_status": parse_status_input,
    }[name]
    try:
        parser(arguments if arguments is not None else {})
    except OperationsRelayError:
        return True
    return False


def _health_check(config: OperationsRelayListenerConfig, bearer: str, timeout: int) -> bool:
    payload = grokbot_ops._request_json(
        f"http://{grokbot_ops._connect_host(config.bind_host)}:{config.bind_port}/health",
        bearer,
        timeout,
        method="GET",
    )
    return payload is not None and payload.get("ok") is True and payload.get("service") == SERVICE_NAME
