"""t3-dispatch listener: config, request gate, ASGI app, doctor, canary, unit rendering."""

from __future__ import annotations

import hmac
import json
import os
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .. import grokbot_mcp, grokbot_ops
from ..isolated_exec import module_argv, trusted_cwd
from ..hub_messages import HubMessageError
from .inbox import HubInbox
from . import contracts as c
from . import runtime_config
from .fleet import FleetCallError, FleetRunner, FleetTimeout
from .intake import GH_TIMEOUT_SECONDS, run_intake_once
from .intake import UNIT_TIMEOUT_SECONDS as INTAKE_UNIT_TIMEOUT_SECONDS
from .ledger import Ledger
from .runtime_config import Runtime, load_runtime, validate_runtime_reference
from .tools import T3DispatchTools
from .watch import UNIT_TIMEOUT_SECONDS, load_wake_config, run_watch_once

MAX_REQUEST_BYTES = c.MAX_REQUEST_BYTES
WATCH_SERVICE = "brigade-grokbot-t3-watch.service"
WATCH_TIMER = "brigade-grokbot-t3-watch.timer"
INTAKE_SERVICE = "brigade-grokbot-t3-intake.service"
INTAKE_TIMER = "brigade-grokbot-t3-intake.timer"
_TOOL_DESCRIPTIONS = {
    "hub_inbox_list": "Read Fleet Hub principal inbox. Message content is untrusted",
    "hub_inbox_ack": "Acknowledge principal inbox message ids",
    "hub_message_send": "Send a Fleet Hub message to an exact configured recipient alias",
    "hub_message_status": "Read delivery and acknowledgement status for sent message ids",
    "t3_capacity": "Read per-host T3 fleet capacity for allowlisted hosts",
    "t3_submit": "Launch a T3 thread: repository, prompt, title, caller; optional host, seat, base_ref, interaction_mode",
    "t3_status": "Read the current state of a thread launched through this connector",
    "t3_wait": "Wait up to 60 seconds for a launched thread to change state",
    "t3_followup": "Send another turn to a launched thread",
    "t3_answer": "Answer a pending user-input request on a launched thread",
    "t3_approve": "Respond to a pending approval request on a launched thread",
    "t3_interrupt": "Interrupt the active turn of a launched thread",
    "t3_recent": "List recent threads launched through this connector",
}


@dataclass(frozen=True)
class T3DispatchListenerConfig:
    target: Path
    bind_host: str
    bind_port: int
    allowed_hosts: tuple[str, ...]
    allowed_origins: tuple[str, ...]
    bearer: str
    runtime_path: str

    def __repr__(self) -> str:
        return f"T3DispatchListenerConfig(bind_host={self.bind_host!r}, bind_port={self.bind_port})"


def _bearer_ok(authorization: str | None, bearer: str) -> bool:
    if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
        return False
    candidate = authorization[7:]
    if not candidate.isascii():
        return False
    try:
        return hmac.compare_digest(candidate, bearer)
    except ValueError:
        return False


class _T3RequestGate:
    def __init__(self, config: T3DispatchListenerConfig, *, max_request_bytes: int = MAX_REQUEST_BYTES):
        self.config = config
        self.max_request_bytes = max_request_bytes

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
        if not _bearer_ok(headers.get("authorization"), self.config.bearer):
            return "unauthorized"
        return None


class _T3GateASGI:
    def __init__(self, app: Callable[..., Any], gate: _T3RequestGate):
        self.app = app
        self.gate = gate

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
    if name not in c.TOOLS:
        return True
    arguments = params.get("arguments")
    if name not in c.HUB_TOOLS and isinstance(arguments, dict):
        arguments = {key: value for key, value in arguments.items() if value is not None}
    try:
        c.PARSERS[name](arguments if arguments is not None else {})
    except c.T3DispatchError:
        return True
    return False


def _drop_none(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


def _register_tools(tools: T3DispatchTools) -> list[Any]:
    from mcp.server.mcpserver.tools import Tool
    from mcp.server.mcpserver.utilities.func_metadata import ArgModelBase, FuncMetadata
    from pydantic import ConfigDict, create_model, model_validator

    class HubFuncMetadata(FuncMetadata):
        def pre_parse_json(self, data: dict[str, Any]) -> dict[str, Any]:
            # Hub arguments are already JSON values. Decoding a string again
            # corrupts message text and nullable metadata such as client_id="null".
            return data.copy()

    def hub_tool(handler: Callable[..., Any]) -> Any:
        name = handler.__name__
        schema = c.HUB_INPUT_SCHEMAS[name]

        class HubArguments(ArgModelBase):
            model_config = ConfigDict(extra="forbid")

            @model_validator(mode="before")
            @classmethod
            def validate_input(cls, value: Any) -> dict[str, Any]:
                try:
                    return c.PARSERS[name](value)
                except c.T3DispatchError:
                    raise ValueError("Tool input failed validation") from None

        # MCP 2.x accepts public Tool objects in MCPServer(tools=...). Use its
        # public argument model and our runtime parser so SDK coercion cannot
        # erase unknown keys or weaken integer, uniqueness, or byte validation.
        tool = Tool.from_function(handler, description=_TOOL_DESCRIPTIONS[name])
        model = create_model(
            f"{name}Arguments",
            __base__=(HubArguments, tool.fn_metadata.arg_model),
        )
        metadata = HubFuncMetadata(
            arg_model=model,
            output_schema=tool.fn_metadata.output_schema,
            output_model=tool.fn_metadata.output_model,
            wrap_output=tool.fn_metadata.wrap_output,
        )
        return tool.model_copy(
            update={
                "parameters": schema,
                "fn_metadata": metadata,
            }
        )

    def t3_capacity() -> dict[str, Any]:
        return tools.call_tool("t3_capacity", {})

    def t3_submit(
        repository: str,
        prompt: str,
        title: str,
        caller: str,
        host: str | None = None,
        seat: str | None = None,
        base_ref: str | None = None,
        interaction_mode: str | None = None,
    ) -> dict[str, Any]:
        return tools.call_tool(
            "t3_submit",
            _drop_none(
                {
                    "repository": repository,
                    "prompt": prompt,
                    "title": title,
                    "caller": caller,
                    "host": host,
                    "seat": seat,
                    "base_ref": base_ref,
                    "interaction_mode": interaction_mode,
                }
            ),
        )

    def t3_status(request_id: str) -> dict[str, Any]:
        return tools.call_tool("t3_status", {"request_id": request_id})

    def t3_wait(request_id: str, timeout_s: int | None = None) -> dict[str, Any]:
        return tools.call_tool("t3_wait", _drop_none({"request_id": request_id, "timeout_s": timeout_s}))

    def t3_followup(request_id: str, message: str) -> dict[str, Any]:
        return tools.call_tool("t3_followup", {"request_id": request_id, "message": message})

    def t3_answer(request_id: str, input_id: str, answers: dict[str, Any]) -> dict[str, Any]:
        return tools.call_tool("t3_answer", {"request_id": request_id, "input_id": input_id, "answers": answers})

    def t3_approve(request_id: str, approval_id: str, decision: str) -> dict[str, Any]:
        return tools.call_tool(
            "t3_approve", {"request_id": request_id, "approval_id": approval_id, "decision": decision}
        )

    def t3_interrupt(request_id: str) -> dict[str, Any]:
        return tools.call_tool("t3_interrupt", {"request_id": request_id})

    def t3_recent(caller: str | None = None, limit: int | None = None) -> dict[str, Any]:
        return tools.call_tool("t3_recent", _drop_none({"caller": caller, "limit": limit}))

    def hub_inbox_list(unacked_only: bool = True, limit: int = 20) -> dict[str, Any]:
        return tools.call_tool("hub_inbox_list", {"unacked_only": unacked_only, "limit": limit})

    def hub_inbox_ack(ids: list[int]) -> dict[str, Any]:
        return tools.call_tool("hub_inbox_ack", {"ids": ids})

    def hub_message_send(
        alias: str,
        subject: str,
        body: str,
        ref: str | None = None,
        priority: str = "normal",
        thread: str | None = None,
        kind: str = "message",
        client_id: str | None = None,
    ) -> dict[str, Any]:
        return tools.call_tool(
            "hub_message_send",
            _drop_none(
                dict(
                    alias=alias,
                    subject=subject,
                    body=body,
                    ref=ref,
                    priority=priority,
                    thread=thread,
                    kind=kind,
                    client_id=client_id,
                )
            ),
        )

    def hub_message_status(ids: list[int]) -> dict[str, Any]:
        return tools.call_tool("hub_message_status", {"ids": ids})

    registered = []
    for handler in (
        hub_inbox_list,
        hub_inbox_ack,
        hub_message_send,
        hub_message_status,
        t3_capacity,
        t3_submit,
        t3_status,
        t3_wait,
        t3_followup,
        t3_answer,
        t3_approve,
        t3_interrupt,
        t3_recent,
    ):
        name = handler.__name__
        if name not in tools.inventory():
            continue
        handler.__doc__ = _TOOL_DESCRIPTIONS[name]
        registered.append(
            hub_tool(handler)
            if name in c.HUB_TOOLS
            else Tool.from_function(handler, description=_TOOL_DESCRIPTIONS[name])
        )
    return registered


def build_app(config: T3DispatchListenerConfig, tools: T3DispatchTools) -> Callable[..., Any]:
    MCPServer, JSONResponse, _, TransportSecuritySettings = grokbot_mcp._load_mcp()
    server = MCPServer(c.SERVICE_NAME, version="1", tools=_register_tools(tools))

    @server.custom_route("/health", methods=["GET"])
    async def health(_: Any) -> Any:
        return JSONResponse({"ok": True, "service": c.SERVICE_NAME})

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
    return _T3GateASGI(app, _T3RequestGate(config))


def run_listener(config: T3DispatchListenerConfig, tools: T3DispatchTools) -> None:
    grokbot_mcp._load_mcp()
    try:
        import uvicorn
    except ImportError as exc:
        raise grokbot_mcp.OptionalDependencyError() from exc
    uvicorn.run(
        build_app(config, tools), host=config.bind_host, port=config.bind_port, access_log=False, log_level="warning"
    )


def _require_isolated_bearer(bearer: str) -> str:
    if len(bearer) < 32:
        raise c.T3DispatchError("invalid_request")
    dispatch_token = os.environ.get("GROKBOT_DISPATCH_TOKEN")
    if dispatch_token is not None and bearer == dispatch_token:
        raise c.T3DispatchError("invalid_request")
    return bearer


def _load_validated_instance(target: Path) -> dict[str, Any]:
    runtime_config.require_posix_storage()
    from .. import grokbot_packs

    payload = grokbot_packs._load_instance_config(target, c.PACK_ID)
    grokbot_packs._parse_default_bind(str(payload["bind"]))
    grokbot_packs._validate_bearer_reference(payload["bearer"])
    validated = dict(payload)
    validated["runtime_path"] = validate_runtime_reference(payload["runtime_path"])
    return validated


def _require_runtime_identity(runtime: Runtime) -> None:
    if runtime.messaging_configured:
        try:
            HubInbox(token_file=str(runtime.hub_token_file)).capture()
        except HubMessageError:
            raise c.T3DispatchError("hub-error") from None


def _tools_for(runtime: Runtime) -> T3DispatchTools:
    return T3DispatchTools(
        runtime=runtime,
        runner=FleetRunner(runtime.t3_fleet_bin, runtime.git_bin, runtime_mode="full-access"),
        ledger=Ledger(runtime.ledger_path),
    )


def build_listener_from_target(
    target: Path,
    *,
    bind: str | None,
    allowed_hosts: list[str],
    allowed_origins: list[str],
    bearer_file: Path | None,
    bearer_env: str | None,
) -> tuple[T3DispatchListenerConfig, T3DispatchTools]:
    from .. import grokbot_packs

    instance = _load_validated_instance(target)
    host, port = grokbot_packs._parse_default_bind(bind or instance["bind"])
    runtime = load_runtime(Path(instance["runtime_path"]))
    bearer = _require_isolated_bearer(grokbot_mcp.load_bearer(bearer_file=bearer_file, bearer_env=bearer_env))
    config = T3DispatchListenerConfig(
        target=target.expanduser().resolve(),
        bind_host=host,
        bind_port=port,
        allowed_hosts=tuple(allowed_hosts),
        allowed_origins=tuple(allowed_origins),
        bearer=bearer,
        runtime_path=instance["runtime_path"],
    )
    return config, _tools_for(runtime)


def watch_once(target: Path) -> dict[str, int]:
    instance = _load_validated_instance(target)
    runtime = load_runtime(Path(instance["runtime_path"]))
    tools = _tools_for(runtime)
    return run_watch_once(
        runner=tools.runner,
        runtime=runtime,
        ledger=tools.ledger,
        wake_targets=load_wake_config(runtime.wake_path),
        log_path=runtime.wake_path.parent / "wake-notify.jsonl",
    )


def intake_once(target: Path) -> dict[str, Any]:
    instance = _load_validated_instance(target)
    runtime = load_runtime(Path(instance["runtime_path"]))
    tools = _tools_for(runtime)
    policy = runtime.intake

    def gh(argv: list[str]) -> tuple[int, str]:
        if policy is None:
            return 127, ""
        try:
            result = subprocess.run(
                [policy.gh_bin, *argv],
                capture_output=True,
                text=True,
                shell=False,
                timeout=GH_TIMEOUT_SECONDS,
                check=False,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return 124, ""
        except OSError:
            return 127, ""
        return result.returncode, result.stdout

    return run_intake_once(policy=policy, runtime=runtime, tools=tools, gh=gh, now=datetime.now(timezone.utc))


def _load_runtime_config(target: Path) -> tuple[T3DispatchListenerConfig, str, Runtime]:
    from .. import grokbot_packs

    instance = _load_validated_instance(target)
    host, port = grokbot_packs._parse_default_bind(str(instance["bind"]))
    bearer = _require_isolated_bearer(grokbot_ops._resolve_bearer(dict(instance["bearer"])))
    runtime = load_runtime(Path(instance["runtime_path"]))
    config = T3DispatchListenerConfig(
        target=target,
        bind_host=host,
        bind_port=port,
        allowed_hosts=tuple(instance["allowed_hosts"]),
        allowed_origins=tuple(instance["allowed_origins"]),
        bearer=bearer,
        runtime_path=instance["runtime_path"],
    )
    return config, bearer, runtime


def doctor(target: Path, *, timeout: int | None = None) -> list[dict[str, str]]:
    from .. import grokbot_packs

    try:
        runtime_config.require_posix_storage()
    except c.T3DispatchError as exc:
        return [{"check": str(exc), "status": "fail", "reason": exc.code}]
    checks: list[dict[str, str]] = []

    def record(name: str, ok: bool) -> None:
        checks.append({"check": name, "status": "ok" if ok else "fail"})

    try:
        from mcp.server import MCPServer  # noqa: F401

        record("dependency", True)
    except ImportError:
        record("dependency", False)
    try:
        config, bearer, runtime = _load_runtime_config(target)
        record("config", True)
    except (c.T3DispatchError, grokbot_packs.PackError, grokbot_mcp.ConfigurationError, OSError, ValueError, KeyError):
        record("config", False)
        return checks
    if runtime.messaging_configured:
        try:
            _require_runtime_identity(runtime)
            record("hub-identity", True)
        except c.T3DispatchError:
            record("hub-identity", False)
    parent = grokbot_packs.instance_config_path(target, c.PACK_ID).parent
    record("permissions", os.access(parent, os.W_OK) if parent.is_dir() else False)
    try:
        load_wake_config(runtime.wake_path)
        record("wake-config", True)
    except (c.T3DispatchError, OSError):
        record("wake-config", False)
    try:
        ledger = Ledger(runtime.ledger_path)
        ledger.close()
        record("ledger", True)
    except (c.T3DispatchError, OSError, sqlite3.Error):
        record("ledger", False)
    checks.append({"check": "writable-paths", "status": "ok" if runtime.writable_paths else "warn"})
    try:
        FleetRunner(runtime.t3_fleet_bin, runtime.git_bin).capacity()
        record("t3-fleet", True)
    except (FleetCallError, FleetTimeout):
        record("t3-fleet", False)
    wait = timeout or grokbot_ops.DEFAULT_TIMEOUT_SECONDS
    payload = grokbot_ops._request_json(
        f"http://{grokbot_ops._connect_host(config.bind_host)}:{config.bind_port}/health", bearer, wait, method="GET"
    )
    record("endpoint", payload is not None and payload.get("ok") is True and payload.get("service") == c.SERVICE_NAME)
    return checks


def canary(target: Path, *, timeout: int | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": False, "pack_id": c.PACK_ID}
    try:
        runtime_config.require_posix_storage()
    except c.T3DispatchError as exc:
        return {**result, "reason": str(exc), "code": exc.code}
    try:
        config, bearer, runtime = _load_runtime_config(target)
    except Exception:
        result["reason"] = "config"
        return result
    wait = timeout or grokbot_ops.DEFAULT_TIMEOUT_SECONDS
    base = f"http://{grokbot_ops._connect_host(config.bind_host)}:{config.bind_port}"
    health = grokbot_ops._request_json(f"{base}/health", bearer, wait, method="GET")
    anonymous_status = grokbot_ops._anonymous_health_status(f"{base}/health", wait)
    tools_response = grokbot_ops._tools_list(base, bearer, wait)
    if health is None or health.get("ok") is not True or health.get("service") != c.SERVICE_NAME:
        result["reason"] = "health"
        return result
    if anonymous_status not in {401, 403}:
        result["reason"] = "auth"
        return result
    if tools_response is None:
        result["reason"] = "inventory-unreachable"
        return result
    names = {tool.get("name") for tool in tools_response if isinstance(tool, dict)}
    expected = c.TOOLS if runtime.messaging_configured else c.T3_TOOLS
    if names != set(expected):
        result["reason"] = "inventory-mismatch"
        return result
    if runtime.messaging_configured:
        try:
            HubInbox(token_file=str(runtime.hub_token_file)).capture()
        except HubMessageError:
            result["reason"] = "hub-identity"
            return result
    result.update({"ok": True, "health": health, "auth_rejected_without_bearer": True, "tools": sorted(expected)})
    return result


def _writable(runtime: Runtime) -> str:
    paths = [str(runtime.ledger_path.parent), *runtime.writable_paths]
    if runtime.wake_path.parent != runtime.ledger_path.parent:
        paths.insert(1, str(runtime.wake_path.parent))
    return " ".join(grokbot_ops._systemd_quote(path) for path in dict.fromkeys(paths))


def _hardening(runtime: Runtime) -> str:
    return (
        "NoNewPrivileges=yes\n"
        "PrivateTmp=yes\n"
        "ProtectSystem=strict\n"
        "ProtectHome=read-only\n"
        f"ReadWritePaths={_writable(runtime)}\n"
    )


def render_unit(target: Path, *, python: str | None = None) -> str:
    target = target.expanduser().resolve()
    instance = _load_validated_instance(target)
    runtime = load_runtime(Path(instance["runtime_path"]))
    args = module_argv(
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
        python=python,
    )
    for host in instance.get("allowed_hosts", []):
        args += ["--allow-host", host]
    for origin in instance.get("allowed_origins", []):
        args += ["--allow-origin", origin]
    reference = instance["bearer"]
    if reference["kind"] == "file":
        args += ["--bearer-file", reference["path"]]
    else:
        args += ["--bearer-env", reference["name"]]
    exec_start = " ".join(grokbot_ops._systemd_quote(argument) for argument in args)
    return (
        "# Generated by brigade run cloud grokbot pack install-service.\n"
        f"# Unit: {grokbot_ops.unit_name(c.PACK_ID)}\n"
        "[Unit]\n"
        "Description=Brigade Grok Bot MCP listener (t3-dispatch)\n"
        "After=network.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"WorkingDirectory={grokbot_ops._systemd_quote(str(trusted_cwd()))}\nExecStart={exec_start}\n"
        "Restart=on-failure\n"
        "RestartSec=10s\n"
        f"{_hardening(runtime)}\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def render_watch_units(target: Path, *, python: str | None = None) -> tuple[str, str]:
    target = target.expanduser().resolve()
    instance = _load_validated_instance(target)
    runtime = load_runtime(Path(instance["runtime_path"]))
    args = module_argv("brigade.grokbot_t3", "watch", "--target", str(target), python=python)
    exec_start = " ".join(grokbot_ops._systemd_quote(argument) for argument in args)
    service = (
        "# Generated by python -m brigade.grokbot_t3 install-watch.\n"
        "[Unit]\n"
        "Description=Brigade Grok Bot t3-dispatch wake watcher\n"
        "After=network.target\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"WorkingDirectory={grokbot_ops._systemd_quote(str(trusted_cwd()))}\nExecStart={exec_start}\n"
        f"TimeoutStartSec={UNIT_TIMEOUT_SECONDS}\n"
        f"{_hardening(runtime)}"
    )
    timer = (
        "# Generated by python -m brigade.grokbot_t3 install-watch.\n"
        "[Unit]\n"
        "Description=Run the t3-dispatch wake watcher every minute\n\n"
        "[Timer]\n"
        "OnBootSec=2min\n"
        "OnUnitActiveSec=1min\n"
        f"Unit={WATCH_SERVICE}\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    return service, timer


def render_intake_units(target: Path, *, python: str | None = None) -> tuple[str, str]:
    target = target.expanduser().resolve()
    instance = _load_validated_instance(target)
    runtime = load_runtime(Path(instance["runtime_path"]))
    args = module_argv("brigade.grokbot_t3", "intake", "--target", str(target), python=python)
    exec_start = " ".join(grokbot_ops._systemd_quote(argument) for argument in args)
    service = (
        "# Generated by python -m brigade.grokbot_t3 install-intake.\n"
        "[Unit]\n"
        "Description=Brigade Grok Bot t3-dispatch issue intake\n"
        "After=network.target\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"WorkingDirectory={grokbot_ops._systemd_quote(str(trusted_cwd()))}\nExecStart={exec_start}\n"
        f"TimeoutStartSec={INTAKE_UNIT_TIMEOUT_SECONDS}\n"
        f"{_hardening(runtime)}"
    )
    timer = (
        "# Generated by python -m brigade.grokbot_t3 install-intake.\n"
        "[Unit]\n"
        "Description=Run the t3-dispatch issue intake every 15 minutes\n\n"
        "[Timer]\n"
        "OnBootSec=5min\n"
        "OnUnitActiveSec=15min\n"
        f"Unit={INTAKE_SERVICE}\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    return service, timer


def _write_rendered(path: Path, rendered: str, *, force: bool) -> Path:
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
            path, rendered, mode=0o644, replace_symlink=force, replace=force or existing is not None
        )
    except OSError as exc:
        raise grokbot_ops.ServiceRenderError(f"refusing unsafe unit path: {path.name}") from exc
    return path


def write_unit(target: Path, out_dir: Path, *, force: bool = False, python: str | None = None) -> Path:
    return _write_rendered(out_dir / grokbot_ops.unit_name(c.PACK_ID), render_unit(target, python=python), force=force)


def write_watch_units(target: Path, out_dir: Path, *, force: bool = False, python: str | None = None) -> list[Path]:
    service, timer = render_watch_units(target, python=python)
    return [
        _write_rendered(out_dir / WATCH_SERVICE, service, force=force),
        _write_rendered(out_dir / WATCH_TIMER, timer, force=force),
    ]


def write_intake_units(target: Path, out_dir: Path, *, force: bool = False, python: str | None = None) -> list[Path]:
    service, timer = render_intake_units(target, python=python)
    return [
        _write_rendered(out_dir / INTAKE_SERVICE, service, force=force),
        _write_rendered(out_dir / INTAKE_TIMER, timer, force=force),
    ]
