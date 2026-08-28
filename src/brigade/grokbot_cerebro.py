"""First-party Cerebro Memory connector adapter.

Ports the closed Cerebro connector contract into Brigade using the optional
``mcp>=2,<3`` runtime. Child process paths, stdout, and stderr never appear in
public errors, diagnostics, canaries, units, or receipts.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from weakref import WeakKeyDictionary

from . import grokbot_mcp, grokbot_ops, proc

PACK_ID = "cerebro-memory"
DEFAULT_BIND = "127.0.0.1:8770"
TOOLS = frozenset(
    {
        "cerebro_search",
        "cerebro_show",
        "cerebro_propose",
        "cerebro_proposal_status",
        "cerebro_health",
    }
)
UNTRUSTED_VAULT_CONTENT = "untrusted_vault_content"
ALLOWED_SCOPES = frozenset({"agent-inbox", "agent-work-log"})
HEALTH_STATUSES = frozenset({"ok", "warning", "error"})
DELIVERY_STATES = frozenset({"delivered", "delivery_failed_retained", "delivery_failed_unretained"})
CLI_SCHEMAS = {
    "search": "cerebro-agents.search.v1",
    "show": "cerebro-agents.show.v1",
    "propose": "cerebro-agents.proposal.v1",
    "status": "cerebro-agents.status.v1",
    "health": "cerebro-agents.doctor.v1",
}
ERROR_MESSAGES = {
    "invalid_request": "Tool input failed validation",
    "denied": "Cerebro request was denied",
    "not_found": "Cerebro resource was not found",
    "unavailable": "Cerebro observation is unavailable",
    "timeout": "Cerebro observation timed out",
    "protocol_error": "Cerebro observation failed",
    "conflict": "Cerebro request conflicted",
}
CHILD_ENVIRONMENT_KEYS = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
)
EXEC_MIN_TIMEOUT_MS = 250
EXEC_MAX_TIMEOUT_MS = 30_000
EXEC_DEFAULT_TIMEOUT_MS = 15_000
EXEC_MIN_OUTPUT_BYTES = 1_024
EXEC_MAX_OUTPUT_BYTES = 262_144
EXEC_DEFAULT_OUTPUT_BYTES = 65_536
PUBLIC_RESULT_LIMIT = 131_072
SHOW_TEXT_LIMIT = 65_536
RELATIVE_PATH_MAX_BYTES = 512
MAX_ACTIVE_READS = 4
MAX_ACTIVE_MUTATIONS = 1
MAX_REQUEST_BYTES = 16_384
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
CONTENT_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
ALLOWED_TOP_LEVEL = frozenset(
    {
        "schema",
        "results",
        "note",
        "request_id",
        "id",
        "proposal_id",
        "delivered",
        "duplicate",
        "phase",
        "delivery_state",
        "receipt_ref",
        "ok",
        "summary",
    }
)
_CHILD_FAILURES: WeakKeyDictionary["CerebroError", tuple[int, str, str]] = WeakKeyDictionary()


class CerebroError(Exception):
    """Stable public Cerebro failure. Messages never include paths or child output."""

    def __init__(self, code: str, message: str | None = None):
        self.code = code
        self.message = message if message is not None else ERROR_MESSAGES[code]
        super().__init__(self.message)

    def public_error(self) -> dict[str, dict[str, str]]:
        return {"error": {"code": self.code, "message": ERROR_MESSAGES[self.code]}}

    def __repr__(self) -> str:
        return f"CerebroError({self.code!r})"


class ChildTimeout(Exception):
    """Internal marker for a timed-out child process."""


class ChildOversize(Exception):
    """Internal marker for stdout/stderr that exceeded the output cap."""


class ChildFailure(Exception):
    """Internal child exit with privately retained bounded output."""

    def __init__(self, exit_code: int, stdout: bytes, stderr: bytes):
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        super().__init__("child failed")


@dataclass(frozen=True)
class ExecRequest:
    file: str
    args: tuple[str, ...]
    cwd: str
    timeout_ms: int
    max_buffer_bytes: int
    env: Mapping[str, str]
    stdin: bytes | None = None
    shell: bool = False


@dataclass(frozen=True)
class PrivateExecResult:
    stdout: str


@dataclass(frozen=True)
class CerebroListenerConfig:
    target: Path
    bind_host: str
    bind_port: int
    allowed_hosts: tuple[str, ...]
    allowed_origins: tuple[str, ...]
    bearer: str
    cli_executable: str
    workdir: str

    def __repr__(self) -> str:
        return f"CerebroListenerConfig(bind_host={self.bind_host!r}, bind_port={self.bind_port})"


Runner = Callable[[ExecRequest], PrivateExecResult]


def utf8_byte_length(value: str) -> int:
    return len(value.encode("utf-8"))


def truncate_utf8(text: str, max_bytes: int) -> str:
    result: list[str] = []
    used = 0
    for char in text:
        next_size = utf8_byte_length(char)
        if used + next_size > max_bytes:
            break
        result.append(char)
        used += next_size
    return "".join(result)


def child_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    env_source = os.environ if source is None else source
    environment: dict[str, str] = {}
    for key in CHILD_ENVIRONMENT_KEYS:
        value = env_source.get(key)
        if value is not None:
            environment[key] = value
    return environment


def parse_search_input(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict) or not set(raw) <= {"query", "scope", "limit"} or "query" not in raw:
        raise CerebroError("invalid_request")
    query = raw["query"]
    if not isinstance(query, str) or not 1 <= utf8_byte_length(query) <= 512:
        raise CerebroError("invalid_request")
    limit = raw.get("limit", 5)
    if type(limit) is not int or not 1 <= limit <= 10:
        raise CerebroError("invalid_request")
    parsed: dict[str, Any] = {"query": query, "limit": limit}
    if "scope" in raw:
        if raw["scope"] not in ALLOWED_SCOPES:
            raise CerebroError("invalid_request")
        parsed["scope"] = raw["scope"]
    return parsed


def parse_show_input(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict) or set(raw) != {"note_id"}:
        raise CerebroError("invalid_request")
    return {"note_id": _note_id(raw["note_id"])}


def parse_propose_input(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != {"request_id", "title", "body", "source_refs"}:
        raise CerebroError("invalid_request")
    title = raw["title"]
    body = raw["body"]
    refs = raw["source_refs"]
    if not isinstance(title, str) or not 1 <= len(title) <= 120:
        raise CerebroError("invalid_request")
    if not isinstance(body, str) or not 1 <= utf8_byte_length(body) <= 12_288:
        raise CerebroError("invalid_request")
    if not isinstance(refs, list) or len(refs) > 16:
        raise CerebroError("invalid_request")
    parsed_refs = []
    for item in refs:
        if not isinstance(item, dict) or set(item) != {"note_id", "content_hash"}:
            raise CerebroError("invalid_request")
        digest = item["content_hash"]
        if not isinstance(digest, str) or not CONTENT_HASH_RE.fullmatch(digest):
            raise CerebroError("invalid_request")
        parsed_refs.append({"note_id": _note_id(item["note_id"]), "content_hash": digest})
    return {
        "request_id": _request_id(raw["request_id"]),
        "title": title,
        "body": body,
        "source_refs": parsed_refs,
    }


def parse_status_input(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict) or set(raw) != {"request_id"}:
        raise CerebroError("invalid_request")
    return {"request_id": _request_id(raw["request_id"])}


def parse_health_input(raw: object) -> dict[str, Any]:
    if raw != {}:
        raise CerebroError("invalid_request")
    return {}


def validate_cli_executable(path_text: str) -> str:
    normalized = _normalize_absolute(path_text)
    info = _lstat_nofollow(normalized)
    if not stat.S_ISREG(info.st_mode) or not os.access(normalized, os.X_OK):
        raise CerebroError("invalid_request", "Cerebro environment is invalid")
    return normalized


def validate_workdir(path_text: str) -> str:
    normalized = _normalize_absolute(path_text)
    info = _lstat_nofollow(normalized)
    if not stat.S_ISDIR(info.st_mode):
        raise CerebroError("invalid_request", "Cerebro environment is invalid")
    return normalized


def run_exec(request: ExecRequest, *, runner: Runner | None = None) -> PrivateExecResult:
    if not _bounded_int(request.timeout_ms, EXEC_MIN_TIMEOUT_MS, EXEC_MAX_TIMEOUT_MS):
        raise CerebroError("invalid_request", "Cerebro execution request is invalid")
    if not _bounded_int(request.max_buffer_bytes, EXEC_MIN_OUTPUT_BYTES, EXEC_MAX_OUTPUT_BYTES):
        raise CerebroError("invalid_request", "Cerebro execution request is invalid")
    env = child_environment(request.env)
    prepared = ExecRequest(
        file=request.file,
        args=tuple(request.args),
        cwd=request.cwd,
        timeout_ms=request.timeout_ms,
        max_buffer_bytes=request.max_buffer_bytes,
        env=env,
        stdin=request.stdin,
        shell=False,
    )
    try:
        return (runner or _subprocess_runner)(prepared)
    except ChildTimeout:
        raise CerebroError("timeout", "Cerebro observation timed out") from None
    except ChildOversize:
        raise CerebroError("protocol_error", "Cerebro observation was invalid") from None
    except ChildFailure as exc:
        error = CerebroError("unavailable", "Cerebro observation is unavailable")
        _CHILD_FAILURES[error] = (
            exc.exit_code,
            _decode_bounded(exc.stdout, request.max_buffer_bytes),
            _decode_bounded(exc.stderr, request.max_buffer_bytes),
        )
        raise error from None
    except CerebroError:
        raise
    except Exception:
        raise CerebroError("unavailable", "Cerebro observation is unavailable") from None


class CerebroTools:
    """Bounded Cerebro tool surface over a path-free CLI projection."""

    def __init__(
        self,
        *,
        cli_executable: str,
        workdir: str,
        env: Mapping[str, str] | None = None,
        runner: Runner | None = None,
    ):
        self._bin = cli_executable
        self._cwd = workdir
        self._env = child_environment(env)
        self._runner = runner
        self._reads = threading.BoundedSemaphore(MAX_ACTIVE_READS)
        self._mutations = threading.BoundedSemaphore(MAX_ACTIVE_MUTATIONS)

    def search(self, raw: object) -> dict[str, Any]:
        return self._guard(lambda: self._search(parse_search_input(raw)))

    def show(self, raw: object) -> dict[str, Any]:
        return self._guard(lambda: self._show(parse_show_input(raw)))

    def propose(self, raw: object) -> dict[str, Any]:
        return self._guard(lambda: self._propose(parse_propose_input(raw)))

    def status(self, raw: object) -> dict[str, Any]:
        return self._guard(lambda: self._status(parse_status_input(raw)))

    def health(self, raw: object) -> dict[str, Any]:
        parse_health_input(raw)
        return self._guard(self._health)

    def call_tool(self, name: str, arguments: object) -> dict[str, Any]:
        handlers = {
            "cerebro_search": self.search,
            "cerebro_show": self.show,
            "cerebro_propose": self.propose,
            "cerebro_proposal_status": self.status,
            "cerebro_health": self.health,
        }
        handler = handlers.get(name)
        if handler is None:
            raise CerebroError("invalid_request")
        return handler(arguments if arguments is not None else {})

    def tool_inventory(self) -> list[dict[str, str]]:
        return [{"name": name, "description": _TOOL_DESCRIPTIONS[name]} for name in sorted(TOOLS)]

    def _search(self, parsed: dict[str, Any]) -> dict[str, Any]:
        args = ["search", "--json", "--limit", str(parsed["limit"])]
        if "scope" in parsed:
            args.extend(["--scope", parsed["scope"]])
        args.extend(["--", parsed["query"]])
        payload = self._cli_json(args, CLI_SCHEMAS["search"])
        results = payload.get("results")
        if not isinstance(results, list):
            raise CerebroError("protocol_error")
        return {
            "results": [
                item for item in (_project_search_hit(item) for item in results) if item["scope"] in ALLOWED_SCOPES
            ]
        }

    def _show(self, parsed: dict[str, str]) -> dict[str, Any]:
        shown = _project_show(
            self._cli_json(["show", "--json", "--", parsed["note_id"]], CLI_SCHEMAS["show"]).get("note")
        )
        if shown["scope"] not in ALLOWED_SCOPES:
            raise CerebroError("denied")
        return shown

    def _propose(self, parsed: dict[str, Any]) -> dict[str, Any]:
        args = ["propose", "--request-id", parsed["request_id"], "--title", parsed["title"], "--json"]
        for ref in parsed["source_refs"]:
            args.extend(["--source-ref", f"{ref['content_hash']}:{ref['note_id']}"])
        projected = _project_propose(
            self._cli_json(
                args, CLI_SCHEMAS["propose"], stdin=parsed["body"].encode("utf-8"), allow_delivery_failure=True
            )
        )
        return {
            "request_id": projected["request_id"],
            "proposal_id": projected["proposal_id"],
            "delivery_state": projected["delivery_state"],
            "duplicate": projected["duplicate"],
            "receipt_ref": projected["receipt_ref"],
        }

    def _status(self, parsed: dict[str, str]) -> dict[str, Any]:
        return _project_receipt_fields(
            self._cli_json(["status", "--request-id", parsed["request_id"], "--json"], CLI_SCHEMAS["status"])
        )

    def _health(self) -> dict[str, Any]:
        return _project_health(self._cli_json(["doctor", "--json"], CLI_SCHEMAS["health"]))

    def _cli_json(
        self,
        args: list[str],
        schema: str,
        *,
        stdin: bytes | None = None,
        allow_delivery_failure: bool = False,
    ) -> dict[str, Any]:
        with self._mutations if args[:1] == ["propose"] else self._reads:
            try:
                result = run_exec(
                    ExecRequest(
                        file=self._bin,
                        args=tuple(args),
                        cwd=self._cwd,
                        timeout_ms=EXEC_DEFAULT_TIMEOUT_MS,
                        max_buffer_bytes=EXEC_DEFAULT_OUTPUT_BYTES,
                        env=self._env,
                        stdin=stdin,
                    ),
                    runner=self._runner,
                )
                stdout = result.stdout
            except CerebroError as exc:
                child = _CHILD_FAILURES.get(exc)
                if child is not None and child[0] == 2 and child[2] == "idempotency conflict\n":
                    raise CerebroError("conflict") from None
                if allow_delivery_failure and child is not None and child[0] == 5:
                    stdout = child[1]
                elif exc.code in ERROR_MESSAGES:
                    raise
                else:
                    raise CerebroError("unavailable") from None
        return _parse_cli_json(stdout, schema)

    def _guard(self, work: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        try:
            result = work()
            if utf8_byte_length(json.dumps(result, separators=(",", ":"))) > PUBLIC_RESULT_LIMIT:
                raise CerebroError("protocol_error")
            return result
        except CerebroError:
            raise
        except Exception:
            raise CerebroError("protocol_error") from None


class CerebroAdapter:
    """HTTP-facing adapter used by doctor, canary, and the MCP listener."""

    def __init__(self, config: CerebroListenerConfig, tools: CerebroTools):
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
        return {"ok": True, "service": "grokbot-cerebro-memory"}

    def _tools(self) -> frozenset[str]:
        return TOOLS

    def tool_inventory(self) -> list[dict[str, str]]:
        return self.tools.tool_inventory()

    def call_tool(self, name: str, arguments: object) -> dict[str, Any]:
        try:
            return self.tools.call_tool(name, arguments)
        except CerebroError as exc:
            return exc.public_error()


def doctor(target: Path, *, timeout: int = grokbot_ops.DEFAULT_TIMEOUT_SECONDS) -> list[dict[str, str]]:
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
    except (CerebroError, grokbot_packs.PackError, grokbot_mcp.ConfigurationError, OSError, ValueError, KeyError):
        record("config", False)
        return checks

    parent = grokbot_packs.instance_config_path(target, PACK_ID).parent
    record("permissions", os.access(parent, os.W_OK) if parent.is_dir() else False)
    record("endpoint", _health_check(config, bearer, timeout))
    return checks


def canary(target: Path, *, timeout: int = grokbot_ops.DEFAULT_TIMEOUT_SECONDS) -> dict[str, Any]:
    from . import grokbot_packs

    result: dict[str, Any] = {"ok": False, "pack_id": PACK_ID}
    try:
        config, bearer = _load_runtime(target)
    except (CerebroError, grokbot_packs.PackError, grokbot_mcp.ConfigurationError, OSError, ValueError, KeyError):
        result["reason"] = "config"
        return result

    base = f"http://{grokbot_ops._connect_host(config.bind_host)}:{config.bind_port}"
    health = grokbot_ops._request_json(f"{base}/health", bearer, timeout, method="GET")
    anonymous_status = grokbot_ops._anonymous_health_status(f"{base}/health", timeout)
    tools_response = grokbot_ops._tools_list(base, bearer, timeout)
    if health is None or health.get("ok") is not True or health.get("service") != "grokbot-cerebro-memory":
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
    probe = _call_health_tool(base, bearer, timeout)
    if probe is None or probe.get("ok") is not True:
        result["reason"] = "health"
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
    writable_state = grokbot_ops._systemd_quote(instance["workdir"])
    return (
        "# Generated by brigade run cloud grokbot pack install-service.\n"
        f"# Unit: {grokbot_ops.unit_name(PACK_ID)}\n"
        "[Unit]\n"
        "Description=Brigade Grok Bot MCP listener (cerebro-memory)\n"
        "After=network.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={exec_start}\n"
        "Restart=on-failure\n"
        "NoNewPrivileges=yes\n"
        "PrivateTmp=yes\n"
        "ProtectSystem=strict\n"
        "ProtectHome=read-only\n\n"
        f"ReadWritePaths={writable_state}\n\n"
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


def build_app(config: CerebroListenerConfig, tools: CerebroTools) -> Callable[..., Any]:
    MCPServer, JSONResponse, _, TransportSecuritySettings = grokbot_mcp._load_mcp()
    adapter = CerebroAdapter(config, tools)
    server = MCPServer("grokbot-cerebro-memory", version="1")

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
    return _CerebroGateASGI(app, _CerebroRequestGate(config), adapter)


def run_listener(config: CerebroListenerConfig, tools: CerebroTools) -> None:
    grokbot_mcp._load_mcp()
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - bundled with mcp at runtime.
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
) -> tuple[CerebroListenerConfig, CerebroTools]:
    from . import grokbot_packs

    instance = grokbot_packs._load_instance_config(target, PACK_ID)
    chosen_bind = bind or instance["bind"]
    host, port = grokbot_packs._parse_default_bind(chosen_bind)
    executable = validate_cli_executable(str(instance["cli_executable"]))
    workdir = validate_workdir(str(instance["workdir"]))
    if executable == workdir:
        raise CerebroError("invalid_request", "Cerebro environment is invalid")
    bearer = grokbot_mcp.load_bearer(bearer_file=bearer_file, bearer_env=bearer_env)
    config = CerebroListenerConfig(
        target=target.expanduser().resolve(),
        bind_host=host,
        bind_port=port,
        allowed_hosts=tuple(allowed_hosts),
        allowed_origins=tuple(allowed_origins),
        bearer=bearer,
        cli_executable=executable,
        workdir=workdir,
    )
    tools = CerebroTools(cli_executable=executable, workdir=workdir, env=os.environ)
    return config, tools


def _register_tools(server: Any, adapter: CerebroAdapter) -> None:
    def cerebro_search(query: str, scope: str | None = None, limit: int = 5) -> dict[str, Any]:
        payload: dict[str, Any] = {"query": query, "limit": limit}
        if scope is not None:
            payload["scope"] = scope
        return adapter.call_tool("cerebro_search", payload)

    def cerebro_show(note_id: str) -> dict[str, Any]:
        return adapter.call_tool("cerebro_show", {"note_id": note_id})

    def cerebro_propose(
        request_id: str,
        title: str,
        body: str,
        source_refs: list[dict[str, str]],
    ) -> dict[str, Any]:
        return adapter.call_tool(
            "cerebro_propose",
            {"request_id": request_id, "title": title, "body": body, "source_refs": source_refs},
        )

    def cerebro_proposal_status(request_id: str) -> dict[str, Any]:
        return adapter.call_tool("cerebro_proposal_status", {"request_id": request_id})

    def cerebro_health() -> dict[str, Any]:
        return adapter.call_tool("cerebro_health", {})

    for name, handler in (
        ("cerebro_search", cerebro_search),
        ("cerebro_show", cerebro_show),
        ("cerebro_propose", cerebro_propose),
        ("cerebro_proposal_status", cerebro_proposal_status),
        ("cerebro_health", cerebro_health),
    ):
        handler.__name__ = name
        handler.__doc__ = _TOOL_DESCRIPTIONS[name]
        server.tool(name=name, description=_TOOL_DESCRIPTIONS[name])(handler)


class _CerebroRequestGate:
    def __init__(self, config: CerebroListenerConfig, *, max_request_bytes: int = MAX_REQUEST_BYTES):
        self.config = config
        self.max_request_bytes = max_request_bytes
        self._adapter = CerebroAdapter(
            config,
            CerebroTools(cli_executable=config.cli_executable, workdir=config.workdir),
        )

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


class _CerebroGateASGI:
    def __init__(self, app: Callable[..., Any], gate: _CerebroRequestGate, adapter: CerebroAdapter):
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
        if scope.get("path") == "/mcp" and _invalid_cerebro_tool_request(body):
            await grokbot_mcp._reject_tool(send, body)
            return
        await self.app(scope, grokbot_mcp._replay_messages(messages, receive), send)


def _invalid_cerebro_tool_request(body: bytes) -> bool:
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
        "cerebro_search": parse_search_input,
        "cerebro_show": parse_show_input,
        "cerebro_propose": parse_propose_input,
        "cerebro_proposal_status": parse_status_input,
        "cerebro_health": parse_health_input,
    }[name]
    try:
        parser(arguments if arguments is not None else {})
    except CerebroError:
        return True
    return False


def _load_validated_instance(target: Path) -> dict[str, Any]:
    from . import grokbot_packs

    payload = grokbot_packs._load_instance_config(target, PACK_ID)
    executable = validate_cli_executable(str(payload["cli_executable"]))
    workdir = validate_workdir(str(payload["workdir"]))
    if executable == workdir:
        raise CerebroError("invalid_request", "Cerebro environment is invalid")
    grokbot_packs._parse_default_bind(str(payload["bind"]))
    grokbot_packs._validate_bearer_reference(payload["bearer"])
    validated = dict(payload)
    validated["cli_executable"] = executable
    validated["workdir"] = workdir
    return validated


def _load_runtime(target: Path) -> tuple[CerebroListenerConfig, str]:
    from . import grokbot_packs

    payload = _load_validated_instance(target)
    host, port = grokbot_packs._parse_default_bind(str(payload["bind"]))
    bearer = grokbot_ops._resolve_bearer(payload["bearer"])
    config = CerebroListenerConfig(
        target=target,
        bind_host=host,
        bind_port=port,
        allowed_hosts=tuple(payload["allowed_hosts"]),
        allowed_origins=tuple(payload["allowed_origins"]),
        bearer=bearer,
        cli_executable=payload["cli_executable"],
        workdir=payload["workdir"],
    )
    return config, bearer


def _health_check(config: CerebroListenerConfig, bearer: str, timeout: int) -> bool:
    payload = grokbot_ops._request_json(
        f"http://{grokbot_ops._connect_host(config.bind_host)}:{config.bind_port}/health",
        bearer,
        timeout,
        method="GET",
    )
    return payload is not None and payload.get("ok") is True and payload.get("service") == "grokbot-cerebro-memory"


def _call_health_tool(base: str, bearer: str, timeout: int) -> dict[str, Any] | None:
    try:
        import asyncio

        return asyncio.run(asyncio.wait_for(_call_health_tool_async(base, bearer), timeout=timeout))
    except Exception:
        return None


async def _call_health_tool_async(base: str, bearer: str) -> dict[str, Any]:
    client_session, streamable_http_client, async_client = grokbot_ops._mcp_client_components()
    async with async_client(headers={"Authorization": f"Bearer {bearer}"}, trust_env=False) as http_client:
        async with streamable_http_client(f"{base}/mcp", http_client=http_client) as streams:
            read_stream, write_stream = streams
            async with client_session(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool("cerebro_health", {})
    payload = result.structuredContent if getattr(result, "structuredContent", None) else None
    if isinstance(payload, dict):
        return payload
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
    raise CerebroError("protocol_error")


_READ_CHUNK_BYTES = 4096
_CHILD_CLEANUP_SECONDS = 0.5


def _close_pipe(stream: Any) -> None:
    if stream is None:
        return
    try:
        stream.close()
    except OSError:
        pass


def _write_child_stdin(process: subprocess.Popen[bytes], payload: bytes | None) -> None:
    if process.stdin is None:
        return
    try:
        if payload is not None:
            process.stdin.write(payload)
    except (BrokenPipeError, OSError):
        pass
    finally:
        _close_pipe(process.stdin)


def _reap_child_group(process: subprocess.Popen[bytes]) -> None:
    proc.terminate_process_tree(process, terminate_grace=0.2, kill_grace=0.2)
    for stream in (process.stdin, process.stdout, process.stderr):
        _close_pipe(stream)


def _join_bounded(threads: tuple[threading.Thread, ...], timeout: float) -> None:
    deadline = time.monotonic() + max(timeout, 0.0)
    for thread in threads:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        thread.join(timeout=remaining)


def _read_chunk(stream: Any) -> bytes:
    read1 = getattr(stream, "read1", None)
    if callable(read1):
        return read1(_READ_CHUNK_BYTES)
    fileno = getattr(stream, "fileno", None)
    if callable(fileno):
        try:
            return os.read(fileno(), _READ_CHUNK_BYTES)
        except BlockingIOError:
            return b""
    return stream.read(_READ_CHUNK_BYTES)


def _subprocess_runner(request: ExecRequest) -> PrivateExecResult:
    try:
        process = subprocess.Popen(
            [request.file, *request.args],
            cwd=request.cwd,
            env=dict(request.env),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            **proc.process_group_kwargs(),
        )
    except OSError as exc:
        raise ChildFailure(-1, b"", b"") from exc

    cap = request.max_buffer_bytes
    stdout = bytearray()
    stderr = bytearray()
    overflowed = threading.Event()
    wake = threading.Event()

    def read_into(stream: Any, buf: bytearray) -> None:
        try:
            while True:
                chunk = _read_chunk(stream)
                if not chunk:
                    return
                if overflowed.is_set() or len(buf) + len(chunk) > cap:
                    overflowed.set()
                    return
                buf.extend(chunk)
        except (OSError, ValueError):
            return
        finally:
            wake.set()

    readers = (
        threading.Thread(target=read_into, args=(process.stdout, stdout), daemon=True),
        threading.Thread(target=read_into, args=(process.stderr, stderr), daemon=True),
    )
    for reader in readers:
        reader.start()
    stdin_thread = threading.Thread(target=_write_child_stdin, args=(process, request.stdin), daemon=True)
    stdin_thread.start()

    deadline = time.monotonic() + (request.timeout_ms / 1000)
    timed_out = False
    child_exited_at: float | None = None
    try:
        while True:
            readers_alive = any(reader.is_alive() for reader in readers)
            if overflowed.is_set() or (process.poll() is not None and not readers_alive):
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            wait_for = remaining
            if process.poll() is not None:
                if child_exited_at is None:
                    child_exited_at = time.monotonic()
                drain_left = _CHILD_CLEANUP_SECONDS - (time.monotonic() - child_exited_at)
                if drain_left <= 0:
                    timed_out = True
                    break
                wait_for = min(wait_for, drain_left)
            wake.clear()
            readers_alive = any(reader.is_alive() for reader in readers)
            if overflowed.is_set() or (process.poll() is not None and not readers_alive):
                break
            wake.wait(timeout=wait_for)
    finally:
        if timed_out or overflowed.is_set():
            _reap_child_group(process)
        _join_bounded((*readers, stdin_thread), _CHILD_CLEANUP_SECONDS)
        if process.poll() is None:
            _reap_child_group(process)
            try:
                process.wait(timeout=_CHILD_CLEANUP_SECONDS)
            except (OSError, subprocess.TimeoutExpired):
                pass

    if overflowed.is_set():
        raise ChildOversize()
    if timed_out:
        raise ChildTimeout()
    if process.returncode:
        raise ChildFailure(int(process.returncode), bytes(stdout), bytes(stderr))
    return PrivateExecResult(stdout=_decode_bounded(bytes(stdout), cap))


def _decode_bounded(data: bytes, maximum: int) -> str:
    return data[:maximum].decode("utf-8", errors="replace")


def _parse_cli_json(stdout: str, schema: str) -> dict[str, Any]:
    try:
        parsed = json.loads(stdout)
    except (TypeError, ValueError, UnicodeDecodeError):
        raise CerebroError("protocol_error") from None
    if not isinstance(parsed, dict):
        raise CerebroError("protocol_error")
    kept: dict[str, Any] = {}
    for key, value in parsed.items():
        if key == "checks":
            continue
        if key not in ALLOWED_TOP_LEVEL:
            raise CerebroError("protocol_error")
        kept[key] = value
    if kept.get("schema") != schema:
        raise CerebroError("protocol_error")
    return kept


def _project_search_hit(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CerebroError("protocol_error")
    return {
        "id": _required_string(value.get("id")),
        "title": _required_string(value.get("title")),
        "scope": _required_string(value.get("scope")),
        "relative_path": _required_relative_path(value.get("relative_path")),
        "content_hash": _required_string(value.get("content_hash")),
        "updated_at": _required_string(value.get("updated_at")),
        "tags": _required_tags(value.get("tags")),
        "snippet": value.get("snippet") if isinstance(value.get("snippet"), str) else "",
        "score": _required_score(value.get("score")),
        "trust": UNTRUSTED_VAULT_CONTENT,
    }


def _project_show(note: object) -> dict[str, Any]:
    if not isinstance(note, dict):
        raise CerebroError("protocol_error")
    original = _required_string(note.get("text"))
    too_big = utf8_byte_length(original) > SHOW_TEXT_LIMIT
    return {
        "id": _required_string(note.get("id")),
        "title": _required_string(note.get("title")),
        "scope": _required_string(note.get("scope")),
        "relative_path": _required_relative_path(note.get("relative_path")),
        "content_hash": _required_string(note.get("content_hash")),
        "updated_at": _required_string(note.get("updated_at")),
        "tags": _required_tags(note.get("tags")),
        "text": truncate_utf8(original, SHOW_TEXT_LIMIT) if too_big else original,
        "truncated": too_big or note.get("truncated") is True,
        "trust": UNTRUSTED_VAULT_CONTENT,
    }


def _project_propose(payload: Mapping[str, Any]) -> dict[str, Any]:
    receipt = _project_receipt_fields(payload)
    if receipt.get("phase") != "terminal" or "delivery_state" not in receipt or "receipt_ref" not in receipt:
        raise CerebroError("protocol_error")
    delivered = payload.get("delivered")
    if not isinstance(delivered, bool):
        raise CerebroError("protocol_error")
    if delivered is not (receipt["delivery_state"] == "delivered"):
        raise CerebroError("protocol_error")
    duplicate = payload.get("duplicate")
    if not isinstance(duplicate, bool):
        raise CerebroError("protocol_error")
    return {
        "request_id": receipt["request_id"],
        "proposal_id": receipt["proposal_id"],
        "delivered": delivered,
        "duplicate": duplicate,
        "phase": "terminal",
        "delivery_state": receipt["delivery_state"],
        "receipt_ref": receipt["receipt_ref"],
    }


def _project_receipt_fields(payload: Mapping[str, Any]) -> dict[str, Any]:
    phase = payload.get("phase")
    if phase not in {"in_progress", "terminal"}:
        raise CerebroError("protocol_error")
    delivery_state = payload.get("delivery_state")
    receipt_ref = payload.get("receipt_ref")
    if delivery_state is not None and (not isinstance(delivery_state, str) or not delivery_state):
        raise CerebroError("protocol_error")
    if receipt_ref is not None and (not isinstance(receipt_ref, str) or not receipt_ref):
        raise CerebroError("protocol_error")
    proposal = payload.get("proposal_id", payload.get("id"))
    if phase == "in_progress":
        if delivery_state is not None or receipt_ref is not None:
            raise CerebroError("protocol_error")
        return {
            "request_id": _required_string(payload.get("request_id")),
            "proposal_id": _required_string(proposal),
            "phase": phase,
        }
    if delivery_state not in DELIVERY_STATES or receipt_ref is None:
        raise CerebroError("protocol_error")
    return {
        "request_id": _required_string(payload.get("request_id")),
        "proposal_id": _required_string(proposal),
        "phase": phase,
        "delivery_state": delivery_state,
        "receipt_ref": receipt_ref,
    }


def _project_health(payload: Mapping[str, Any]) -> dict[str, Any]:
    ok = payload.get("ok")
    summary = payload.get("summary")
    if not isinstance(ok, bool) or not isinstance(summary, dict):
        raise CerebroError("protocol_error")
    index_age = summary.get("index_age_seconds")
    if index_age is not None and (type(index_age) is not int or index_age < 0):
        raise CerebroError("protocol_error")
    projected: dict[str, Any] = {
        "cli": _required_health_status(summary.get("cli")),
        "configuration": _required_health_status(summary.get("configuration")),
        "index": _required_health_status(summary.get("index")),
        "proposal_bridge": _required_health_status(summary.get("proposal_bridge")),
    }
    if type(index_age) is int:
        projected["index_age_seconds"] = index_age
    return {"ok": ok, "summary": projected}


def _required_string(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise CerebroError("protocol_error")
    return value


def _required_tags(value: object) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(tag, str) for tag in value):
        raise CerebroError("protocol_error")
    return value


def _required_score(value: object) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not _is_finite(value):
        raise CerebroError("protocol_error")
    return float(value)


def _required_health_status(value: object) -> str:
    if not isinstance(value, str) or value not in HEALTH_STATUSES:
        raise CerebroError("protocol_error")
    return value


def _required_relative_path(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise CerebroError("protocol_error")
    if utf8_byte_length(value) > RELATIVE_PATH_MAX_BYTES or "\0" in value or "\\" in value:
        raise CerebroError("protocol_error")
    try:
        decoded = urllib.parse.unquote(value, errors="strict")
    except Exception:
        raise CerebroError("protocol_error") from None
    if decoded.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", decoded) or "\\" in decoded or "://" in decoded:
        raise CerebroError("protocol_error")
    segments = decoded.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise CerebroError("protocol_error")
    return value


def _note_id(value: object) -> str:
    if not isinstance(value, str) or not 1 <= utf8_byte_length(value) <= 512:
        raise CerebroError("invalid_request")
    if "\0" in value or "\n" in value or value.startswith("-"):
        raise CerebroError("invalid_request")
    return value


def _request_id(value: object) -> str:
    if not isinstance(value, str) or not 8 <= len(value) <= 64 or not REQUEST_ID_RE.fullmatch(value):
        raise CerebroError("invalid_request")
    return value


def _normalize_absolute(path_text: str) -> str:
    if not isinstance(path_text, str) or not path_text or "\0" in path_text:
        raise CerebroError("invalid_request", "Cerebro environment is invalid")
    if not os.path.isabs(path_text):
        raise CerebroError("invalid_request", "Cerebro environment is invalid")
    parts = path_text.split(os.sep)
    if any(part in {".", ".."} for part in parts):
        raise CerebroError("invalid_request", "Cerebro environment is invalid")
    normalized = os.path.normpath(path_text)
    if normalized != "/" and normalized.endswith(os.sep):
        normalized = normalized.rstrip(os.sep)
    return normalized


def _lstat_nofollow(path_text: str) -> os.stat_result:
    path = Path(path_text)
    if grokbot_ops._path_is_symlink(path) or path.is_symlink():
        raise CerebroError("invalid_request", "Cerebro environment is invalid")
    parent = -1
    try:
        parent = grokbot_ops._open_parent_nofollow(path, create=False)
        info = os.stat(path.name, dir_fd=parent, follow_symlinks=False) if os.name == "posix" else path.lstat()
    except OSError as exc:
        raise CerebroError("invalid_request", "Cerebro environment is invalid") from exc
    finally:
        if parent != -1:
            os.close(parent)
    if stat.S_ISLNK(info.st_mode):
        raise CerebroError("invalid_request", "Cerebro environment is invalid")
    return info


def _bounded_int(value: object, minimum: int, maximum: int) -> bool:
    return type(value) is int and minimum <= value <= maximum


def _is_finite(value: float) -> bool:
    return value == value and value not in {float("inf"), float("-inf")}


_TOOL_DESCRIPTIONS = {
    "cerebro_search": "Search cited Cerebro notes",
    "cerebro_show": "Show a cited Cerebro note",
    "cerebro_propose": "Propose a Cerebro memory note",
    "cerebro_proposal_status": "Read a Cerebro proposal receipt",
    "cerebro_health": "Read path-free Cerebro health",
}
