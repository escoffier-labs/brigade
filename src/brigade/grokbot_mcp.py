"""Role-scoped Streamable HTTP adapter for the private Grok Bot queue.

The adapter deliberately delegates all queue authority to :mod:`grokbot_jobs`.
It contains no queue persistence and never accepts a caller-selected worker
identity, target, role, or lease duration.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from . import fleet_client, grokbot_jobs


DEFAULT_BIND = "127.0.0.1:8766"
MAX_REQUEST_BYTES = 16_384
LEASE_SECONDS = 300
MAX_LISTED_JOBS = 100
SDK_LOOPBACK_ALLOWED_HOSTS = ("localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*", "::1", "[::1]:*")
INSTANCES = frozenset({"operator", "repository-scout", "implementation-worker"})
_FLEET_HOLDER_DOMAIN = b"brigade.grokbot.fleet-holder"
_FLEET_SESSION_DOMAIN = b"brigade.grokbot.fleet-session"
_FLEET_BEST_EFFORT = frozenset({"no-hub", "no-identity", "hub-unavailable"})
ENVIRONMENT_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")

OPERATOR_TOOLS = frozenset(
    {"grokbot_queue_list", "grokbot_queue_status", "grokbot_queue_cancel", "grokbot_queue_expire"}
)
WORKER_TOOLS = frozenset(
    {
        "grokbot_queue_list",
        "grokbot_queue_status",
        "grokbot_queue_claim",
        "grokbot_queue_renew",
        "grokbot_queue_start",
        "grokbot_queue_complete",
        "grokbot_queue_fail",
        "grokbot_queue_ack_cancel",
    }
)


class ConfigurationError(ValueError):
    """Configuration was unsafe or incomplete. Details never reach clients."""


class OptionalDependencyError(RuntimeError):
    """The listener's optional MCP runtime is not installed."""


class AdapterError(ValueError):
    """Stable public error for every invalid or unauthorized tool request."""

    def public_error(self) -> dict[str, dict[str, str]]:
        return {"error": {"code": "invalid_request", "message": "Tool input failed validation"}}


@dataclass(frozen=True)
class ListenerConfig:
    """Non-secret listener configuration plus the already-resolved bearer."""

    target: Path
    instance: str
    bind_host: str
    bind_port: int
    allowed_hosts: tuple[str, ...]
    allowed_origins: tuple[str, ...]
    bearer: str

    def validate(self) -> None:
        if self.instance not in INSTANCES or not self.target.is_dir():
            raise ConfigurationError("invalid")
        if not isinstance(self.bearer, str):
            raise ConfigurationError("invalid")
        _validate_bearer(self.bearer)
        if not 1 <= self.bind_port <= 65_535:
            raise ConfigurationError("invalid")
        if any(not _valid_host(host) for host in self.allowed_hosts):
            raise ConfigurationError("invalid")
        if any(not _valid_origin(origin) for origin in self.allowed_origins):
            raise ConfigurationError("invalid")
        if not _is_loopback(self.bind_host) and (not self.allowed_hosts or not self.allowed_origins):
            raise ConfigurationError("invalid")

    @property
    def bot_id(self) -> str:
        """The identity is deployment-fixed and never taken from tool input."""
        return f"grokbot-{self.instance}"


def parse_bind(value: str) -> tuple[str, int]:
    """Parse a deliberately small host:port surface without IPv6 ambiguity."""
    if not isinstance(value, str) or value.count(":") != 1:
        raise ConfigurationError("invalid")
    host, port_text = value.rsplit(":", 1)
    if not host or not port_text.isdecimal():
        raise ConfigurationError("invalid")
    port = int(port_text)
    if not 1 <= port <= 65_535:
        raise ConfigurationError("invalid")
    return host, port


def load_bearer(*, bearer_file: Path | None, bearer_env: str | None) -> str:
    """Resolve exactly one bearer reference without exposing its value or path."""
    if (bearer_file is None) == (bearer_env is None):
        raise ConfigurationError("invalid")
    if bearer_env is not None:
        if not ENVIRONMENT_NAME_RE.fullmatch(bearer_env):
            raise ConfigurationError("invalid")
        value = os.environ.get(bearer_env)
        if value is None:
            raise ConfigurationError("invalid")
        return _validate_bearer(value)

    assert bearer_file is not None
    try:
        info = bearer_file.stat()
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
            raise ConfigurationError("invalid")
        return _validate_bearer(bearer_file.read_text(encoding="utf-8").rstrip("\r\n"))
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigurationError("invalid") from exc


def _validate_bearer(value: str) -> str:
    if not value or len(value) > 4096 or "\x00" in value or not value.isascii():
        raise ConfigurationError("invalid")
    return value


class GrokbotAdapter:
    """Thin role boundary over the Brigade-owned private queue."""

    def __init__(self, config: ListenerConfig):
        config.validate()
        self.config = config
        self._fleet_generations: dict[str, int] = {}

    def tool_inventory(self) -> list[dict[str, str]]:
        return [{"name": name, "description": _TOOL_DESCRIPTIONS[name]} for name in sorted(self._tools())]

    def authorized(self, authorization: str | None) -> bool:
        if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
            return False
        bearer = authorization[7:]
        if not bearer.isascii():
            return False
        return hmac.compare_digest(bearer, self.config.bearer)

    def health_payload(self) -> dict[str, object]:
        return {"ok": True, "service": "grokbot-mcp", "role": self.config.instance}

    def call_tool(self, name: str, arguments: object) -> dict[str, Any]:
        """Execute one exposed tool with fixed authority and safe failures."""
        if name not in self._tools() or not isinstance(arguments, dict):
            raise AdapterError()
        try:
            return self._call(name, arguments)
        except (AdapterError, grokbot_jobs.GrokbotJobError, ValueError, OSError):
            raise AdapterError() from None

    def _tools(self) -> frozenset[str]:
        return OPERATOR_TOOLS if self.config.instance == "operator" else WORKER_TOOLS

    def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "grokbot_queue_list":
            _require_keys(arguments, set())
            jobs = grokbot_jobs.status(self.config.target)["jobs"]
            if self.config.instance != "operator":
                jobs = [job for job in jobs if job["role"] == self.config.instance]
            return {"jobs": jobs[:MAX_LISTED_JOBS]}
        if name == "grokbot_queue_status":
            job = self._eligible_job(arguments)
            return job
        if name in {"grokbot_queue_cancel", "grokbot_queue_expire"}:
            job_id = _job_id(arguments)
            operation = grokbot_jobs.cancel if name.endswith("cancel") else grokbot_jobs.expire
            return operation(self.config.target, job_id)
        if name == "grokbot_queue_claim":
            job = self._eligible_job(arguments, allowed={"job_id", "lease_id"})
            lease_id = _lease_id(arguments)
            holder, session = fleet_holder(job["job_id"], lease_id), fleet_session(job["job_id"], lease_id)
            decision = self._fleet_acquire(job["job_id"], holder, session)
            if _fleet_refused(decision):
                raise AdapterError()
            try:
                result = grokbot_jobs.claim_execution_context(
                    self.config.target, job["job_id"], self.config.bot_id, lease_id, LEASE_SECONDS
                )
            except Exception:
                if decision.granted:
                    self._fleet_release(holder)
                raise
            self._fleet_event(job["job_id"], session, "external.claimed")
            return result
        if name == "grokbot_queue_renew":
            job_id, lease_id = _job_id(arguments, {"job_id", "lease_id"}), _lease_id(arguments)
            holder, session = fleet_holder(job_id, lease_id), fleet_session(job_id, lease_id)
            decision = self._fleet_renew(holder)
            if not decision.granted and decision.reason == "missing":
                decision = self._fleet_acquire(job_id, holder, session)
            if _fleet_refused(decision):
                raise AdapterError()
            try:
                result = grokbot_jobs.renew(self.config.target, job_id, self.config.bot_id, lease_id, LEASE_SECONDS)
            except Exception:
                if decision.granted:
                    self._fleet_release(holder)
                raise
            self._fleet_event(job_id, session, "external.heartbeat")
            return result
        if name in {"grokbot_queue_start", "grokbot_queue_fail", "grokbot_queue_ack_cancel"}:
            job_id, lease_id = _job_id(arguments, {"job_id", "lease_id"}), _lease_id(arguments)
            holder, session = fleet_holder(job_id, lease_id), fleet_session(job_id, lease_id)
            if name.endswith("start"):
                result = grokbot_jobs.transition(self.config.target, job_id, self.config.bot_id, lease_id, "running")
                self._fleet_event(job_id, session, "external.running")
                return result
            if name.endswith("fail"):
                result = grokbot_jobs.transition(self.config.target, job_id, self.config.bot_id, lease_id, "failed")
                self._fleet_event(job_id, session, "external.failed")
                self._fleet_release(holder)
                return result
            result = grokbot_jobs.acknowledge_cancel(self.config.target, job_id, self.config.bot_id, lease_id)
            self._fleet_event(job_id, session, "external.canceled")
            self._fleet_release(holder)
            return result
        if name == "grokbot_queue_complete":
            job = self._eligible_job(arguments, allowed={"job_id", "lease_id", "artifact"})
            artifact = arguments.get("artifact")
            if not isinstance(artifact, dict):
                raise AdapterError()
            lease_id = _lease_id(arguments)
            holder, session = fleet_holder(job["job_id"], lease_id), fleet_session(job["job_id"], lease_id)
            result = grokbot_jobs.transition(
                self.config.target,
                job["job_id"],
                self.config.bot_id,
                lease_id,
                "completed",
                artifact=artifact,
            )
            self._fleet_event(job["job_id"], session, "external.completed")
            self._fleet_release(holder)
            return result
        raise AdapterError()

    def _fleet_target(self) -> str:
        return fleet_client.resolve_claim_target(self.config.target)

    def _remember_fleet_generation(self, holder: str, decision: fleet_client.ClaimDecision) -> None:
        claim = decision.claim
        if decision.granted and isinstance(claim, dict):
            value = claim.get("generation")
            if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
                self._fleet_generations[holder] = value
                return
        if decision.reason == "stale-generation":
            self._fleet_generations.pop(holder, None)

    def _fleet_acquire(self, job_id: str, holder: str, session: str) -> fleet_client.ClaimDecision:
        try:
            kwargs: dict[str, Any] = {
                "holder": holder,
                "ttl_seconds": LEASE_SECONDS,
                "harness": "grokbot",
                "role": self.config.instance,
                "job": job_id,
                "session": session,
            }
            generation = self._fleet_generations.get(holder)
            if generation is not None:
                kwargs["generation"] = generation
            decision = fleet_client.acquire_claim(self._fleet_target(), **kwargs)
            self._remember_fleet_generation(holder, decision)
            return decision
        except Exception:
            return fleet_client.ClaimDecision(granted=False, reason="hub-unavailable", holder=holder)

    def _fleet_renew(self, holder: str) -> fleet_client.ClaimDecision:
        try:
            kwargs: dict[str, Any] = {"holder": holder, "ttl_seconds": LEASE_SECONDS}
            generation = self._fleet_generations.get(holder)
            if generation is not None:
                kwargs["generation"] = generation
            decision = fleet_client.renew_claim(self._fleet_target(), **kwargs)
            self._remember_fleet_generation(holder, decision)
            return decision
        except Exception:
            return fleet_client.ClaimDecision(granted=False, reason="hub-unavailable", holder=holder)

    def _fleet_release(self, holder: str) -> None:
        try:
            fleet_client.release_claim(self._fleet_target(), holder=holder)
        except Exception:
            return
        self._fleet_generations.pop(holder, None)

    def _fleet_event(self, job_id: str, session: str, state: str) -> None:
        try:
            fleet_client.report_external_event(
                target=self._fleet_target(),
                harness="grokbot",
                role=self.config.instance,
                job=job_id,
                session=session,
                state=state,
            )
        except Exception:
            return

    def _eligible_job(self, arguments: dict[str, Any], allowed: set[str] | None = None) -> dict[str, Any]:
        job_id = _job_id(arguments, allowed or {"job_id"})
        job = grokbot_jobs.get_job(self.config.target, job_id)
        if self.config.instance != "operator" and job["role"] != self.config.instance:
            raise AdapterError()
        return job


class RequestGate:
    """Header/body admission checks for the HTTP adapter."""

    def __init__(self, config: ListenerConfig, *, max_request_bytes: int = MAX_REQUEST_BYTES):
        config.validate()
        if not 1 <= max_request_bytes <= MAX_REQUEST_BYTES:
            raise ConfigurationError("invalid")
        self.config = config
        self.max_request_bytes = max_request_bytes

    def reject_reason(self, headers: Mapping[str, str], body_size: int) -> str | None:
        if body_size < 0 or body_size > self.max_request_bytes:
            return "too-large"
        host = headers.get("host", "").split(":", 1)[0].casefold()
        if not self._host_allowed(host):
            return "forbidden"
        origin = headers.get("origin")
        if origin is not None and origin not in self.config.allowed_origins:
            return "forbidden"
        if not GrokbotAdapter(self.config).authorized(headers.get("authorization")):
            return "unauthorized"
        return None

    def _host_allowed(self, host: str) -> bool:
        if _is_loopback(self.config.bind_host) and host in {"localhost", "127.0.0.1", "::1", ""}:
            return True
        return host in {entry.casefold().split(":", 1)[0] for entry in self.config.allowed_hosts}


def build_listener_config(
    *,
    target: Path,
    instance: str,
    bind: str,
    allowed_hosts: list[str],
    allowed_origins: list[str],
    bearer_file: Path | None,
    bearer_env: str | None,
) -> ListenerConfig:
    host, port = parse_bind(bind)
    config = ListenerConfig(
        target=target.expanduser().resolve(),
        instance=instance,
        bind_host=host,
        bind_port=port,
        allowed_hosts=tuple(allowed_hosts),
        allowed_origins=tuple(allowed_origins),
        bearer=load_bearer(bearer_file=bearer_file, bearer_env=bearer_env),
    )
    config.validate()
    return config


def build_app(config: ListenerConfig) -> Callable[..., Any]:
    """Build the separate Streamable HTTP process app with a strict edge gate."""
    MCPServer, JSONResponse, _, TransportSecuritySettings = _load_mcp()
    adapter = GrokbotAdapter(config)
    server = MCPServer("brigade-grokbot", version="1")

    @server.custom_route("/health", methods=["GET"])
    async def health(_: Any) -> Any:
        return JSONResponse(adapter.health_payload())

    for tool in adapter.tool_inventory():
        _register_tool(server, adapter, tool["name"])

    app = server.streamable_http_app(
        json_response=True,
        stateless_http=True,
        max_request_body_size=MAX_REQUEST_BYTES,
        host=config.bind_host,
        transport_security=TransportSecuritySettings(
            allowed_hosts=_transport_allowed_hosts(config),
            allowed_origins=list(config.allowed_origins),
        ),
    )
    return _GateASGI(app, RequestGate(config), adapter)


def run_listener(config: ListenerConfig) -> None:
    """Run the remote adapter in its own process, never inside fleet serve."""
    _, _, _, _ = _load_mcp()
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - bundled with mcp at runtime.
        raise OptionalDependencyError() from exc
    uvicorn.run(build_app(config), host=config.bind_host, port=config.bind_port, access_log=False, log_level="warning")


def _register_tool(server: Any, adapter: GrokbotAdapter, name: str) -> None:
    """Register a name-specific typed wrapper so the SDK inventory stays exact."""
    description = _TOOL_DESCRIPTIONS[name]

    if name == "grokbot_queue_list":

        def invoke_list() -> dict[str, Any]:
            return _invoke_adapter(adapter, name, {})

        handler: Callable[..., Any] = invoke_list
    elif name in {"grokbot_queue_status", "grokbot_queue_cancel", "grokbot_queue_expire"}:

        def invoke_job_id(job_id: str) -> dict[str, Any]:
            return _invoke_adapter(adapter, name, {"job_id": job_id})

        handler = invoke_job_id
    elif name == "grokbot_queue_complete":

        def invoke_complete(job_id: str, lease_id: str, artifact: dict[str, Any]) -> dict[str, Any]:
            return _invoke_adapter(adapter, name, {"job_id": job_id, "lease_id": lease_id, "artifact": artifact})

        handler = invoke_complete
    else:

        def invoke_lease(job_id: str, lease_id: str) -> dict[str, Any]:
            return _invoke_adapter(adapter, name, {"job_id": job_id, "lease_id": lease_id})

        handler = invoke_lease

    handler.__name__ = name
    handler.__doc__ = description
    server.tool(name=name, description=description)(handler)


def _invoke_adapter(adapter: GrokbotAdapter, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        return adapter.call_tool(name, arguments)
    except AdapterError as exc:
        return exc.public_error()


class _GateASGI:
    """Reject untrusted requests before the SDK sees headers or a large body."""

    def __init__(self, app: Callable[..., Any], gate: RequestGate, adapter: GrokbotAdapter):
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
            await _reject_http(send, 413, "Request body is too large")
            return
        reason = self.gate.reject_reason(headers, 0)
        if reason is not None:
            await _reject_http(
                send,
                401 if reason == "unauthorized" else 403,
                "Unauthorized" if reason == "unauthorized" else "Forbidden",
            )
            return
        body, messages, too_large = await _read_bounded_body(receive, self.gate.max_request_bytes)
        if too_large:
            await _reject_http(send, 413, "Request body is too large")
            return
        if scope.get("path") == "/mcp" and _invalid_tool_request(body, self.adapter):
            await _reject_tool(send, body)
            return
        await self.app(scope, _replay_messages(messages), send)


async def _read_bounded_body(receive: Callable[..., Any], maximum: int) -> tuple[bytes, list[dict[str, Any]], bool]:
    body = bytearray()
    messages: list[dict[str, Any]] = []
    while True:
        message = await receive()
        messages.append(message)
        if message.get("type") != "http.request":
            return bytes(body), messages, False
        body.extend(message.get("body", b""))
        if len(body) > maximum:
            return b"", messages, True
        if not message.get("more_body", False):
            return bytes(body), messages, False


def _replay_messages(messages: list[dict[str, Any]]) -> Callable[..., Any]:
    pending = iter(messages)

    async def replay() -> dict[str, Any]:
        return next(pending, {"type": "http.disconnect"})

    return replay


def _invalid_tool_request(body: bytes, adapter: GrokbotAdapter) -> bool:
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
    return name not in adapter._tools() or not _valid_tool_arguments(name, params.get("arguments"))


async def _reject_http(send: Callable[..., Any], status: int, message: str) -> None:
    body = json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32001, "message": message}}).encode("utf-8")
    await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": body})


async def _reject_tool(send: Callable[..., Any], body: bytes) -> None:
    request = json.loads(body)
    request_id = request.get("id") if isinstance(request, dict) else None
    payload = {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": "Invalid request"}}
    await send({"type": "http.response.start", "status": 400, "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": json.dumps(payload).encode("utf-8")})


def _load_mcp() -> tuple[Any, Any, Any, Any]:
    try:
        from mcp.server import MCPServer
        from mcp.server.transport_security import TransportSecuritySettings
        from starlette.requests import Request
        from starlette.responses import JSONResponse
    except ImportError as exc:
        raise OptionalDependencyError() from exc
    return MCPServer, JSONResponse, Request, TransportSecuritySettings


def _require_keys(arguments: dict[str, Any], expected: set[str]) -> None:
    if set(arguments) != expected:
        raise AdapterError()


def _job_id(arguments: dict[str, Any], expected: set[str] | None = None) -> str:
    _require_keys(arguments, expected or {"job_id"})
    job_id = arguments.get("job_id")
    if not isinstance(job_id, str):
        raise AdapterError()
    return job_id


def _lease_id(arguments: dict[str, Any]) -> str:
    lease_id = arguments.get("lease_id")
    if not isinstance(lease_id, str):
        raise AdapterError()
    return lease_id


def _fleet_derivation_message(job_id: str, lease_id: str) -> bytes:
    """Length-prefixed so job_id and lease_id cannot collide across concatenations."""
    return f"{len(job_id)}:{job_id}:{len(lease_id)}:{lease_id}".encode("utf-8")


def fleet_holder(job_id: str, lease_id: str) -> str:
    """Full fencing token derived from the job-scoped queue lease, never the lease itself."""
    return hmac.new(_FLEET_HOLDER_DOMAIN, _fleet_derivation_message(job_id, lease_id), hashlib.sha256).hexdigest()


def fleet_session(job_id: str, lease_id: str) -> str:
    """Opaque non-capability session label, domain-separated from the holder."""
    return hmac.new(_FLEET_SESSION_DOMAIN, _fleet_derivation_message(job_id, lease_id), hashlib.sha256).hexdigest()[:32]


def _fleet_refused(decision: fleet_client.ClaimDecision) -> bool:
    return not decision.granted and decision.reason not in _FLEET_BEST_EFFORT


def _valid_tool_arguments(name: object, arguments: object) -> bool:
    schemas: dict[str, dict[str, type[object]]] = {
        "grokbot_queue_list": {},
        "grokbot_queue_status": {"job_id": str},
        "grokbot_queue_cancel": {"job_id": str},
        "grokbot_queue_expire": {"job_id": str},
        "grokbot_queue_claim": {"job_id": str, "lease_id": str},
        "grokbot_queue_renew": {"job_id": str, "lease_id": str},
        "grokbot_queue_start": {"job_id": str, "lease_id": str},
        "grokbot_queue_complete": {"job_id": str, "lease_id": str, "artifact": dict},
        "grokbot_queue_fail": {"job_id": str, "lease_id": str},
        "grokbot_queue_ack_cancel": {"job_id": str, "lease_id": str},
    }
    if not isinstance(name, str):
        return False
    expected = schemas.get(name)
    if expected == {} and arguments is None:
        return True
    return (
        expected is not None
        and isinstance(arguments, dict)
        and set(arguments) == set(expected)
        and all(isinstance(arguments[key], value_type) for key, value_type in expected.items())
    )


def _transport_allowed_hosts(config: ListenerConfig) -> list[str]:
    if _is_loopback(config.bind_host):
        return list(dict.fromkeys((*SDK_LOOPBACK_ALLOWED_HOSTS, *config.allowed_hosts)))
    return list(config.allowed_hosts)


def _valid_host(value: str) -> bool:
    return isinstance(value, str) and bool(value) and len(value) <= 253 and all(part for part in value.split("."))


def _valid_origin(value: str) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"https?://[A-Za-z0-9.-]+(?::[1-9][0-9]{0,4})?", value))


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


_TOOL_DESCRIPTIONS = {
    "grokbot_queue_list": "List safe projections for this Grok Bot queue role.",
    "grokbot_queue_status": "Read one safe Grok Bot queue projection.",
    "grokbot_queue_cancel": "Cancel a Grok Bot queue job.",
    "grokbot_queue_expire": "Expire an elapsed Grok Bot queue job.",
    "grokbot_queue_claim": "Claim one job for this fixed worker identity.",
    "grokbot_queue_renew": "Renew this worker's current queue lease.",
    "grokbot_queue_start": "Mark this worker's claimed job as running.",
    "grokbot_queue_complete": "Complete this worker's running job.",
    "grokbot_queue_fail": "Fail this worker's live job.",
    "grokbot_queue_ack_cancel": "Acknowledge an operator cancellation request.",
}
