"""Miller's authenticated listener, deployment probes and portable units."""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from brigade import grokbot_mcp, grokbot_ops, grokbot_packs
from brigade.grokbot_t3.lifecycle import _write_rendered
from . import contracts as c
from . import access_auth
from .access_auth import AccessVerifier, METADATA_PATH, bounded_headers, unique_object
from .adapter import MillerTools, build_tools, validate_arguments
from .events import EventService, EventsApp, IDENTITY
from .runtime_config import Runtime, load_runtime
from .safe_state import identity
from .server import register_tools
from .state import BridgeState, call_label

MAX_HTTP_BYTES = 32768
WATCH_SERVICE = "brigade-miller-watch.service"
WATCH_TIMER = "brigade-miller-watch.timer"


@dataclass(frozen=True)
class MillerListenerConfig:
    target: Path
    bind_host: str
    bind_port: int
    allowed_hosts: tuple[str, ...]
    allowed_origins: tuple[str, ...]
    runtime_path: str

    def __repr__(self) -> str:
        return f"MillerListenerConfig(bind_host={self.bind_host!r}, bind_port={self.bind_port})"


class _MillerGate:
    """Admit only metadata discovery and authenticated, bounded MCP POSTs."""

    def __init__(self, app: Any, config: MillerListenerConfig, verifier: AccessVerifier, recorder: Any = None):
        self.app, self.config, self.verifier, self.recorder = app, config, verifier, recorder

    async def record(self, label: str, ok: bool, started: float, error_code: str | None = None) -> None:
        if self.recorder is not None:
            try:
                await asyncio.to_thread(
                    self.recorder, label, ok, (time.monotonic() - started) * 1000, error_code=error_code
                )
            except Exception:
                pass

    def allowed_transport(self, scope: dict[str, Any]) -> bool:
        try:
            headers = bounded_headers(scope)
            hosts = [v.decode("ascii").casefold() for k, v in headers if k.lower() == b"host"]
            origins = [v.decode("ascii") for k, v in headers if k.lower() == b"origin"]
            if len(hosts) != 1 or len(origins) > 1 or not _valid_host(hosts[0]):
                return False
            allowed = {entry.casefold() for entry in self.config.allowed_hosts}
            if grokbot_mcp._is_loopback(self.config.bind_host):
                allowed.update({f"localhost:{self.config.bind_port}", f"127.0.0.1:{self.config.bind_port}"})
            return hosts[0] in allowed and (not origins or origins[0] in self.config.allowed_origins)
        except Exception:
            return False

    async def reject(self, send: Any, status: int) -> None:
        headers = [(b"content-type", b"application/json"), (b"cache-control", b"no-store")]
        if status == 401:
            headers.append(
                (b"www-authenticate", f'Bearer resource_metadata="{self.verifier.metadata_url}"'.encode("ascii"))
            )
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": b'{"error":"request_denied"}'})

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            return
        path, method = scope.get("path", ""), scope.get("method")
        if (
            (path, method) not in ((METADATA_PATH, "GET"), ("/mcp", "POST"))
            or scope.get("query_string", b"")
            or scope.get("raw_path", path.encode()) != path.encode()
        ):
            await self.reject(send, 404)
            return
        if not self.allowed_transport(scope):
            await self.reject(send, 403)
            return
        if path == METADATA_PATH:
            metadata_body = json.dumps(
                {"resource": self.verifier.resource, "authorization_servers": [self.verifier.authorization_server]}
            ).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"application/json"), (b"cache-control", b"no-store")],
                }
            )
            await send({"type": "http.response.body", "body": metadata_body})
            return
        try:
            c.require(self.verifier.validated is True)
            owner = await self.verifier.verify(scope)
            c.require(identity(owner) == self.verifier.config.approved_email)
        except Exception:
            await self.reject(send, 401)
            return
        body = bytearray()
        try:
            import anyio

            with anyio.fail_after(10):
                for _ in range(64):
                    message = await receive()
                    if message.get("type") != "http.request":
                        return
                    chunk = message.get("body", b"")
                    if len(body) + len(chunk) > MAX_HTTP_BYTES:
                        await self.reject(send, 413)
                        return
                    body.extend(chunk)
                    if not message.get("more_body", False):
                        break
                else:
                    await self.reject(send, 413)
                    return
        except TimeoutError:
            await self.reject(send, 408)
            return
        started, label = time.monotonic(), "unparsed"
        try:
            request = json.loads(body, object_pairs_hook=unique_object)
            c.require(isinstance(request, dict))
            label = call_label(request.get("method"))
            if request.get("method") == "tools/call":
                params = request.get("params")
                c.require(isinstance(params, dict))
                name = params.get("name")
                label = name if isinstance(name, str) and name in c.TOOLS else "tools/call"
                validate_arguments(name, params.get("arguments", {}))
        except Exception:
            await self.record(label, False, started, "invalid_request")
            await self.reject(send, 400)
            return
        consumed = False

        async def replay() -> Any:
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        observe = request.get("method") != "tools/call"
        status, response = None, bytearray()

        async def observed_send(message: dict[str, Any]) -> None:
            nonlocal status
            if message.get("type") == "http.response.start":
                status = message.get("status")
            elif message.get("type") == "http.response.body" and len(response) < 262144:
                response.extend(message.get("body", b"")[: 262144 - len(response)])
            await send(message)

        token = IDENTITY.set(owner)
        try:
            await self.app(scope, replay, observed_send if observe else send)
        finally:
            IDENTITY.reset(token)
        if observe:
            error = None if isinstance(status, int) and status < 400 else "http_" + str(status)
            try:
                reply = json.loads(response) if response else None
                if error is None and isinstance(reply, dict) and isinstance(reply.get("error"), dict):
                    error = "rpc_" + str(reply["error"].get("code"))[:16]
            except ValueError:
                pass
            await self.record(label, error is None, started, error)


def _server(config: MillerListenerConfig, tools: MillerTools) -> Any:
    MCPServer, _, _, _ = grokbot_mcp._load_mcp()
    server = MCPServer(c.SERVICE_NAME, version=c.SERVER_VERSION)
    register_tools(server, tools)
    return server


def build_app(
    config: MillerListenerConfig,
    tools: MillerTools,
    service: EventService,
    verifier: AccessVerifier,
    *,
    events_enabled: bool = True,
) -> Callable[..., Any]:
    _, _, _, TransportSecuritySettings = grokbot_mcp._load_mcp()
    c.require(verifier.validated is True, "authentication_inactive")
    server = _server(config, tools)
    allowed = list(dict.fromkeys([*config.allowed_hosts, f"{config.bind_host}:{config.bind_port}"]))
    if grokbot_mcp._is_loopback(config.bind_host):
        allowed = list(dict.fromkeys([*allowed, *grokbot_mcp.SDK_LOOPBACK_ALLOWED_HOSTS]))
    app = server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        max_request_body_size=MAX_HTTP_BYTES,
        host=config.bind_host,
        transport_security=TransportSecuritySettings(
            allowed_hosts=allowed, allowed_origins=list(config.allowed_origins)
        ),
    )
    return _MillerGate(
        EventsApp(app, service, enabled=events_enabled), config, verifier, getattr(tools, "record_call", None)
    )


def _valid_host(value: object) -> bool:
    if not isinstance(value, str) or not grokbot_mcp._valid_host(value):
        return False
    try:
        parsed = urlsplit("//" + value)
        return bool(
            parsed.hostname
            and re.fullmatch(r"[A-Za-z0-9.:[\]-]+", value)
            and parsed.port != 0
            and not parsed.username
            and not parsed.password
            and not parsed.path
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        return False


def _load_config(
    target: Path,
    *,
    bind: str | None = None,
    allowed_hosts: list[str] | None = None,
    allowed_origins: list[str] | None = None,
) -> tuple[MillerListenerConfig, Runtime]:
    instance = grokbot_packs._load_instance_config(target, c.PACK_ID)
    host, port = grokbot_packs._parse_default_bind(bind if bind is not None else instance["bind"])
    hosts = instance["allowed_hosts"] if allowed_hosts is None else allowed_hosts
    origins = instance["allowed_origins"] if allowed_origins is None else allowed_origins
    c.require(
        all(_valid_host(h) for h in hosts) and all(grokbot_mcp._valid_origin(o) for o in origins), "invalid_config"
    )
    config = MillerListenerConfig(target.resolve(), host, port, tuple(hosts), tuple(origins), instance["runtime_path"])
    return config, load_runtime(Path(config.runtime_path))


def build_listener_from_target(
    target: Path,
    *,
    bind: str | None = None,
    allowed_hosts: list[str] | None = None,
    allowed_origins: list[str] | None = None,
) -> tuple[MillerListenerConfig, MillerTools, Runtime]:
    config, runtime = _load_config(target, bind=bind, allowed_hosts=allowed_hosts, allowed_origins=allowed_origins)
    tools, _, _ = build_tools(runtime)
    return config, tools, runtime


def run_listener(config: MillerListenerConfig, tools: MillerTools, runtime: Runtime) -> None:
    try:
        import uvicorn
    except ImportError:
        raise grokbot_mcp.OptionalDependencyError() from None
    # Fail startup when RS256 cannot be verified; otherwise every login would 401 behind a passing canary.
    access_auth._jwt()
    try:
        try:
            tools.state.prune_call_log()
        except c.MillerError:
            pass
        tools.state.hub_set("inbox_backend", "hub")
        service = EventService(
            tools.state,
            fleet_status=tools.fleet.fleet_status,
            hub=tools.inbox.hub if runtime.events_enabled else None,
            hub_poll_seconds=runtime.hub.poll_seconds,
            callback_hosts=frozenset(runtime.callback_hosts),
        )
        app = build_app(config, tools, service, AccessVerifier(runtime.auth), events_enabled=runtime.events_enabled)
        uvicorn.run(
            app,
            host=config.bind_host,
            port=config.bind_port,
            workers=1,
            proxy_headers=False,
            access_log=False,
            server_header=False,
            http="h11",
            ws="none",
            lifespan="on",
            log_level="warning",
            timeout_keep_alive=5,
            limit_concurrency=16,
            h11_max_incomplete_event_size=32768,
        )
    finally:
        tools.state.close()


def _base(config: MillerListenerConfig) -> str:
    host = grokbot_ops._connect_host(config.bind_host)
    return f"http://{'[' + host + ']' if ':' in host else host}:{config.bind_port}"


def _probe(url: str, *, method: str = "GET", host: str | None = None, timeout: int = 10) -> tuple[int, Any]:
    headers = {"User-Agent": "brigade-miller/1", **({"Host": host} if host else {})}
    request = urllib.request.Request(url, data=b"{}" if method == "POST" else None, method=method, headers=headers)
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=timeout) as response:
            data = response.read(65537)
            return response.status, json.loads(data) if len(data) <= 65536 else None
    except urllib.error.HTTPError as error:
        return error.code, None
    except (OSError, ValueError):
        return 0, None


def doctor(target: Path) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []

    def record(name: str, action: Callable[[], Any]) -> None:
        try:
            ok = action() is not False
        except Exception:
            ok = False
        checks.append({"check": name, "status": "ok" if ok else "fail"})

    def dependencies() -> None:
        grokbot_mcp._load_mcp()
        from .access_auth import _jwt

        _jwt()

    record("dependency", dependencies)
    try:
        config, runtime = _load_config(target)
        checks.append({"check": "config", "status": "ok"})
    except Exception:
        return checks + [{"check": "config", "status": "fail"}]

    checks.append({"check": "writable-paths", "status": "ok" if runtime.writable_paths else "warn"})

    def state_check() -> None:
        with BridgeState(runtime.paths.state_dir):
            pass

    from brigade.fleet_client_message_credentials import read_message_token_file

    record("state", state_check)
    record("access-config", lambda: AccessVerifier(runtime.auth))
    record("hub-token", lambda: read_message_token_file(str(runtime.hub.message_token_file)))

    def fleet_check() -> bool:
        tools, state, _ = build_tools(runtime)
        try:
            return tools.fleet.fleet_capacity().get("ok") is True
        finally:
            state.close()

    record("t3-fleet", fleet_check)
    record("endpoint", lambda: _probe(_base(config) + "/mcp", method="POST")[0] == 401)
    return checks


def _registered_names(config: MillerListenerConfig, tools: MillerTools) -> set[str]:
    return {tool.name for tool in asyncio.run(_server(config, tools).list_tools())}


def canary(target: Path) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": False, "pack_id": c.PACK_ID}
    try:
        config, runtime = _load_config(target)
    except Exception:
        return {**result, "reason": "config"}
    if _probe(_base(config) + "/mcp", method="POST")[0] != 401:
        return {**result, "reason": "auth"}
    status, metadata = _probe(_base(config) + METADATA_PATH)
    if status != 200 or not isinstance(metadata, dict) or metadata.get("resource") != runtime.auth.resource:
        return {**result, "reason": "metadata"}
    try:
        tools, state, _ = build_tools(runtime)
        try:
            names = _registered_names(config, tools)
        finally:
            state.close()
        if names != c.TOOLS:
            return {**result, "reason": "inventory-mismatch"}
    except Exception:
        return {**result, "reason": "inventory-mismatch"}
    return {**result, "ok": True}


def _hardening(runtime: Runtime) -> str:
    paths = [runtime.paths.state_dir, runtime.paths.workspace / ".codex" / "memory-handoffs", *runtime.writable_paths]
    writable = " ".join(grokbot_ops._systemd_quote(str(p)) for p in dict.fromkeys(paths))
    return (
        "NoNewPrivileges=yes\nPrivateTmp=yes\nProtectSystem=strict\nProtectHome=read-only\n"
        f"UMask=0077\nReadWritePaths={writable}\n"
    )


def render_unit(target: Path, *, python: str | None = None) -> str:
    instance = grokbot_packs._load_instance_config(target, c.PACK_ID)
    runtime = load_runtime(Path(instance["runtime_path"]))
    args = [
        python or sys.executable,
        "-m",
        "brigade",
        "run",
        "cloud",
        "grokbot",
        "serve",
        "--pack",
        c.PACK_ID,
        "--target",
        str(target),
        "--bind",
        instance["bind"],
    ]
    for flag, key in (("--allow-host", "allowed_hosts"), ("--allow-origin", "allowed_origins")):
        for value in instance[key]:
            args += [flag, value]
    command = " ".join(grokbot_ops._systemd_quote(arg) for arg in args)
    return (
        "# Generated by brigade run cloud grokbot pack install-service.\n# Unit: brigade-miller.service\n"
        "[Unit]\nDescription=Brigade Miller MCP listener\nAfter=network.target\n\n"
        f"[Service]\nType=simple\nExecStart={command}\nRestart=on-failure\nRestartSec=10s\n"
        f"{_hardening(runtime)}\n[Install]\nWantedBy=default.target\n"
    )


def render_watch_units(target: Path, *, python: str | None = None) -> tuple[str, str]:
    _, runtime = _load_config(target)
    args = [python or sys.executable, "-m", "brigade.miller", "watch", "--target", str(target)]
    command = " ".join(grokbot_ops._systemd_quote(arg) for arg in args)
    header = "# Generated by Brigade Miller watch installation.\n"
    service = (
        header + "[Unit]\nDescription=Watch the Miller inbox and route\nAfter=network.target\n\n"
        f"[Service]\nType=oneshot\nExecStart={command}\nTimeoutStartSec=180\n{_hardening(runtime)}"
    )
    timer = (
        header + "[Unit]\nDescription=Watch Miller every five minutes\n\n[Timer]\nOnBootSec=5min\n"
        f"OnUnitActiveSec=5min\nAccuracySec=30s\nUnit={WATCH_SERVICE}\n\n[Install]\nWantedBy=timers.target\n"
    )
    return service, timer


def write_unit(target: Path, out_dir: Path, *, force: bool = False, python: str | None = None) -> Path:
    return _write_rendered(out_dir / "brigade-miller.service", render_unit(target, python=python), force=force)


def write_watch_units(target: Path, out_dir: Path, *, force: bool = False, python: str | None = None) -> list[Path]:
    service, timer = render_watch_units(target, python=python)
    return [
        _write_rendered(out_dir / WATCH_SERVICE, service, force=force),
        _write_rendered(out_dir / WATCH_TIMER, timer, force=force),
    ]
