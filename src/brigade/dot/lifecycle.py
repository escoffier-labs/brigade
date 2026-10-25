"""Dot's authenticated listener, deployment probes and portable units."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from brigade import grokbot_mcp, grokbot_ops, grokbot_packs
from brigade.grokbot_t3.lifecycle import _write_rendered
from brigade.isolated_exec import module_argv, trusted_cwd
from . import contracts as c
from . import access_auth
from .access_auth import AccessVerifier, METADATA_PATH, bounded_headers, unique_object
from .adapter import DotTools, build_tools, validate_arguments
from .events import EventService, EventsApp, IDENTITY
from .hub_inbox import authenticated_authority
from .profile import DOT, ConnectorProfile, profile_for
from .runtime_config import Runtime, load_runtime
from .safe_state import identity, require_local_platform
from .server import register_tools
from .state import BridgeState, call_label, client_label

MAX_HTTP_BYTES = 32768
WATCH_SERVICE = DOT.watch_service
WATCH_TIMER = DOT.watch_timer


@dataclass(frozen=True)
class DotListenerConfig:
    target: Path
    bind_host: str
    bind_port: int
    allowed_hosts: tuple[str, ...]
    allowed_origins: tuple[str, ...]
    runtime_path: str

    def __repr__(self) -> str:
        return f"DotListenerConfig(bind_host={self.bind_host!r}, bind_port={self.bind_port})"


class _DotGate:
    """Admit only metadata discovery and authenticated, bounded MCP POSTs."""

    def __init__(
        self,
        app: Any,
        config: DotListenerConfig,
        verifier: AccessVerifier,
        recorder: Any = None,
        *,
        profile: ConnectorProfile = DOT,
    ):
        self.app, self.config, self.verifier, self.recorder = app, config, verifier, recorder
        self.profile = profile

    async def record(
        self, label: str, ok: bool, started: float, error_code: str | None = None, client_name: str | None = None
    ) -> None:
        if self.recorder is not None:
            extra = {} if client_name is None else {"client_name": client_name}
            try:
                await asyncio.to_thread(
                    self.recorder, label, ok, (time.monotonic() - started) * 1000, error_code=error_code, **extra
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
        started, label, client_name = time.monotonic(), "unparsed", None
        try:
            request = json.loads(body, object_pairs_hook=unique_object)
            c.require(isinstance(request, dict))
            label = call_label(request.get("method"))
            if request.get("method") == "initialize":
                client_name = _client_name(request.get("params"))
            if request.get("method") == "tools/call":
                params = request.get("params")
                c.require(isinstance(params, dict))
                name = params.get("name")
                tools = self.profile.tools
                label = name if isinstance(name, str) and name in tools else "tools/call"
                validate_arguments(name, params.get("arguments", {}), tools=tools)
        except Exception:
            await self.record(label, False, started, "invalid_request", client_name)
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
            await self.record(label, error is None, started, error, client_name)


def _client_name(params: Any) -> str | None:
    info = params.get("clientInfo") if isinstance(params, dict) else None
    return client_label(info.get("name")) if isinstance(info, dict) else None


def _server(config: DotListenerConfig, tools: DotTools, profile: ConnectorProfile = DOT) -> Any:
    MCPServer, _, _, _ = grokbot_mcp._load_mcp()
    server = MCPServer(profile.service_name, version=c.SERVER_VERSION)
    register_tools(server, tools, profile)
    return server


def build_app(
    config: DotListenerConfig,
    tools: DotTools,
    service: EventService,
    verifier: AccessVerifier,
    *,
    events_enabled: bool = True,
    profile: ConnectorProfile = DOT,
) -> Callable[..., Any]:
    _, _, _, TransportSecuritySettings = grokbot_mcp._load_mcp()
    c.require(verifier.validated is True, "authentication_inactive")
    server = _server(config, tools, profile)
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
    return _DotGate(
        EventsApp(app, service, enabled=events_enabled),
        config,
        verifier,
        getattr(tools, "record_call", None),
        profile=profile,
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
    profile: ConnectorProfile = DOT,
) -> tuple[DotListenerConfig, Runtime]:
    require_local_platform()
    instance = grokbot_packs._load_instance_config(target, profile.pack_id)
    host, port = grokbot_packs._parse_default_bind(bind if bind is not None else instance["bind"])
    hosts = instance["allowed_hosts"] if allowed_hosts is None else allowed_hosts
    origins = instance["allowed_origins"] if allowed_origins is None else allowed_origins
    c.require(
        all(_valid_host(h) for h in hosts) and all(grokbot_mcp._valid_origin(o) for o in origins), "invalid_config"
    )
    config = DotListenerConfig(target.resolve(), host, port, tuple(hosts), tuple(origins), instance["runtime_path"])
    return config, load_runtime(Path(config.runtime_path), schema=profile.runtime_schema)


def build_listener_from_target(
    target: Path,
    *,
    bind: str | None = None,
    allowed_hosts: list[str] | None = None,
    allowed_origins: list[str] | None = None,
    pack_id: str = c.PACK_ID,
) -> tuple[DotListenerConfig, DotTools, Runtime]:
    profile = profile_for(pack_id)
    config, runtime = _load_config(
        target, bind=bind, allowed_hosts=allowed_hosts, allowed_origins=allowed_origins, profile=profile
    )
    # A hosted profile sharing Dot's Access app, token or state would let its credential reach Dot's tools.
    if not profile.fleet_dispatch:
        c.require(_isolated_from_dot(target, runtime), "not_isolated_from_dot")
    tools, _, _ = build_tools(runtime, profile)
    return config, tools, runtime


def run_listener(config: DotListenerConfig, tools: DotTools, runtime: Runtime) -> None:
    # Exported entry point: refuse before any import or startup, whatever built the config.
    require_local_platform()
    try:
        import uvicorn
    except ImportError:
        raise grokbot_mcp.OptionalDependencyError() from None
    # Fail startup when RS256 cannot be verified; otherwise every login would 401 behind a passing canary.
    access_auth._jwt()
    try:
        if not tools.profile.fleet_dispatch:
            tools.inbox.hub.require_identity()
        try:
            tools.state.prune_call_log()
        except c.DotError:
            pass
        tools.state.hub_set("inbox_backend", "hub")
        fleet = tools.fleet
        service = EventService(
            tools.state,
            fleet_status=fleet.fleet_status if fleet is not None else _no_fleet_status,
            hub=tools.inbox.hub if runtime.events_enabled or not tools.profile.fleet_dispatch else None,
            hub_poll_seconds=runtime.hub.poll_seconds,
            callback_hosts=frozenset(runtime.callback_hosts),
        )
        app = build_app(
            config,
            tools,
            service,
            AccessVerifier(runtime.auth),
            events_enabled=runtime.events_enabled,
            profile=tools.profile,
        )
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


def _no_fleet_status(request_id: str) -> dict[str, Any]:
    return {"error": "fleet_unavailable"}


def _base(config: DotListenerConfig) -> str:
    host = grokbot_ops._connect_host(config.bind_host)
    return f"http://{'[' + host + ']' if ':' in host else host}:{config.bind_port}"


def _probe(url: str, *, method: str = "GET", host: str | None = None, timeout: int = 10) -> tuple[int, Any]:
    headers = {"User-Agent": "brigade-dot/1", **({"Host": host} if host else {})}
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


def doctor(target: Path, *, pack_id: str = c.PACK_ID) -> list[dict[str, str]]:
    profile = profile_for(pack_id)
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
        config, runtime = _load_config(target, profile=profile)
        checks.append({"check": "config", "status": "ok"})
    except c.DotError as error:
        if error.code == "unsupported_on_windows":
            return checks + [{"check": "platform", "status": "fail", "reason": error.code}]
        return checks + [{"check": "config", "status": "fail"}]
    except Exception:
        return checks + [{"check": "config", "status": "fail"}]

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
            return tools.fleet is not None and tools.fleet.fleet_capacity().get("ok") is True
        finally:
            state.close()

    if profile.fleet_dispatch:
        record("t3-fleet", fleet_check)
    else:
        try:
            authenticated_authority(runtime.hub)
        except c.DotError as error:
            checks.append({"check": "hub-identity", "status": "fail", "reason": error.code})
        else:
            checks.append({"check": "hub-identity", "status": "ok"})
        record("isolation", lambda: _isolated_from_dot(target, runtime))
    record("endpoint", lambda: _probe(_base(config) + "/mcp", method="POST")[0] == 401)
    return checks


def _isolated_from_dot(target: Path, runtime: Runtime) -> bool:
    """A hosted profile never shares Dot's Hub identity, inbox, state or Access application."""
    if not grokbot_packs.instance_config_path(target, DOT.pack_id).exists():
        return True
    _, dot_runtime = _load_config(target, profile=DOT)
    return (
        runtime.hub.principal != dot_runtime.hub.principal
        and not _same_location(runtime.hub.message_token_file, dot_runtime.hub.message_token_file)
        and not _same_token(runtime.hub.message_token_file, dot_runtime.hub.message_token_file)
        and not _same_location(runtime.paths.state_dir, dot_runtime.paths.state_dir)
        and runtime.auth.audience != dot_runtime.auth.audience
        and runtime.auth.resource != dot_runtime.auth.resource
    )


def _same_location(left: Path, right: Path) -> bool:
    # Spellings like a/../b or a symlink name the same file; an unreadable pair counts as shared.
    try:
        if left.exists() and right.exists():
            return os.path.samefile(left, right)
        return left.resolve() == right.resolve()
    except OSError:
        return True


def _same_token(left: Path, right: Path) -> bool:
    # Two files holding one Hub token are one credential; a token that exists but cannot be read counts as shared.
    from brigade.fleet_client_message_credentials import FleetMessageHubError, read_message_token_file

    if not (os.path.lexists(left) and os.path.lexists(right)):
        return False
    try:
        tokens = read_message_token_file(str(left)), read_message_token_file(str(right))
    except FleetMessageHubError:
        return True
    return hmac.compare_digest(tokens[0].encode("ascii"), tokens[1].encode("ascii"))


def _registered_names(config: DotListenerConfig, tools: DotTools) -> set[str]:
    profile = getattr(tools, "profile", DOT)
    return {tool.name for tool in asyncio.run(_server(config, tools, profile).list_tools())}


def canary(target: Path, *, pack_id: str = c.PACK_ID) -> dict[str, Any]:
    profile = profile_for(pack_id)
    result: dict[str, Any] = {"ok": False, "pack_id": profile.pack_id}
    try:
        config, runtime = _load_config(target, profile=profile)
    except c.DotError as error:
        return {**result, "reason": error.code if error.code == "unsupported_on_windows" else "config"}
    except Exception:
        return {**result, "reason": "config"}
    if _probe(_base(config) + "/mcp", method="POST")[0] != 401:
        return {**result, "reason": "auth"}
    status, metadata = _probe(_base(config) + METADATA_PATH)
    if status != 200 or not isinstance(metadata, dict) or metadata.get("resource") != runtime.auth.resource:
        return {**result, "reason": "metadata"}
    try:
        tools, state, _ = build_tools(runtime, profile)
        try:
            names = _registered_names(config, tools)
        finally:
            state.close()
        if names != profile.tools:
            return {**result, "reason": "inventory-mismatch"}
    except Exception:
        return {**result, "reason": "inventory-mismatch"}
    return {**result, "ok": True}


def _hardening() -> str:
    # User-namespace sandboxes map root-owned ancestors to the overflow uid,
    # breaking the private-directory ownership check. Keep that check fail-closed.
    return "NoNewPrivileges=yes\nUMask=0077\nPrivateTmp=no\n"


def render_unit(target: Path, *, python: str | None = None, pack_id: str = c.PACK_ID) -> str:
    profile = profile_for(pack_id)
    target = target.expanduser().resolve()
    require_local_platform()
    instance = grokbot_packs._load_instance_config(target, profile.pack_id)
    load_runtime(Path(instance["runtime_path"]), schema=profile.runtime_schema)
    args = module_argv(
        "brigade",
        "run",
        "cloud",
        "grokbot",
        "serve",
        "--pack",
        profile.pack_id,
        "--target",
        str(target),
        "--bind",
        instance["bind"],
        python=python,
    )
    for flag, key in (("--allow-host", "allowed_hosts"), ("--allow-origin", "allowed_origins")):
        for value in instance[key]:
            args += [flag, value]
    command = " ".join(grokbot_ops._systemd_quote(arg) for arg in args)
    return (
        f"# Generated by brigade run cloud grokbot pack install-service.\n# Unit: {profile.unit_name}\n"
        f"[Unit]\nDescription=Brigade {profile.label} MCP listener\nAfter=network.target\n\n"
        "[Service]\nType=simple\n"
        f"WorkingDirectory={grokbot_ops._systemd_quote(str(trusted_cwd()))}\n"
        f"ExecStart={command}\nRestart=on-failure\nRestartSec=5\n"
        f"{_hardening()}\n[Install]\nWantedBy=default.target\n"
    )


def render_watch_units(target: Path, *, python: str | None = None, pack_id: str = c.PACK_ID) -> tuple[str, str]:
    profile = profile_for(pack_id)
    target = target.expanduser().resolve()
    _load_config(target, profile=profile)
    watch_args = ["watch", "--target", str(target)]
    if profile is not DOT:
        watch_args += ["--pack", profile.pack_id]
    args = module_argv("brigade.dot", *watch_args, python=python)
    command = " ".join(grokbot_ops._systemd_quote(arg) for arg in args)
    header = f"# Generated by Brigade {profile.label} watch installation.\n"
    service = (
        header + f"[Unit]\nDescription=Watch the {profile.label} inbox and route\nAfter=network.target\n\n"
        "[Service]\nType=oneshot\n"
        f"WorkingDirectory={grokbot_ops._systemd_quote(str(trusted_cwd()))}\n"
        f"ExecStart={command}\nTimeoutStartSec=180\n{_hardening()}"
    )
    timer = (
        header + f"[Unit]\nDescription=Watch {profile.label} every five minutes\n\n[Timer]\nOnBootSec=5min\n"
        f"OnUnitActiveSec=5min\nAccuracySec=30s\nUnit={profile.watch_service}\n\n[Install]\nWantedBy=timers.target\n"
    )
    return service, timer


def write_unit(
    target: Path, out_dir: Path, *, force: bool = False, python: str | None = None, pack_id: str = c.PACK_ID
) -> Path:
    profile = profile_for(pack_id)
    rendered = render_unit(target, python=python, pack_id=pack_id)
    return _write_rendered(out_dir / profile.unit_name, rendered, force=force)


def write_watch_units(
    target: Path, out_dir: Path, *, force: bool = False, python: str | None = None, pack_id: str = c.PACK_ID
) -> list[Path]:
    profile = profile_for(pack_id)
    service, timer = render_watch_units(target, python=python, pack_id=pack_id)
    return [
        _write_rendered(out_dir / profile.watch_service, service, force=force),
        _write_rendered(out_dir / profile.watch_timer, timer, force=force),
    ]
