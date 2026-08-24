"""Non-secret Grok Bot instance configuration, diagnostics, canary, and units."""

from __future__ import annotations

import asyncio
import json
import os
import re
import stat
import sys
import uuid
import urllib.error
import urllib.request
import unicodedata
from pathlib import Path
from typing import Any

from . import grokbot_jobs, grokbot_mcp


CONFIG_SCHEMA = "brigade.grokbot-config/1"
CONFIG_DIR = Path(".brigade") / "grokbot"
QUEUE_STATE_DIR = Path(".brigade") / "cloud" / "grokbot"
DEFAULT_TIMEOUT_SECONDS = 5
MAX_CANARY_BODY_BYTES = 65_536
_SYSTEMD_UNQUOTED_ARG_RE = re.compile(r"^[A-Za-z0-9_@%+=:,./-]+$")


class ServiceRenderError(RuntimeError):
    """Unit rendering or installation was refused."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Turn every redirect response into a bounded request failure."""

    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def unit_name(instance: str) -> str:
    return f"brigade-grokbot-{instance}.service"


def config_path(target: Path, instance: str) -> Path:
    return target / CONFIG_DIR / f"{instance}.json"


def save_config(
    target: Path,
    *,
    instance: str,
    bind: str,
    allowed_hosts: list[str],
    allowed_origins: list[str],
    bearer_env: str | None,
    bearer_file: Path | None,
) -> dict[str, Any]:
    """Persist only non-secret settings plus a secret reference."""
    if instance not in grokbot_mcp.INSTANCES:
        raise grokbot_mcp.ConfigurationError("invalid")
    host, port = grokbot_mcp.parse_bind(bind)
    if (bearer_env is None) == (bearer_file is None):
        raise grokbot_mcp.ConfigurationError("invalid")
    if bearer_env is not None:
        if not grokbot_mcp.ENVIRONMENT_NAME_RE.fullmatch(bearer_env):
            raise grokbot_mcp.ConfigurationError("invalid")
        reference: dict[str, str] = {"kind": "env", "name": bearer_env}
    else:
        assert bearer_file is not None
        reference = {"kind": "file", "path": str(bearer_file.expanduser().resolve())}
    payload = {
        "schema": CONFIG_SCHEMA,
        "instance": instance,
        "bind": f"{host}:{port}",
        "allowed_hosts": sorted(set(allowed_hosts)),
        "allowed_origins": sorted(set(allowed_origins)),
        "bearer": reference,
    }
    _validate_config(payload)
    grokbot_jobs.status(target)
    path = config_path(target, instance)
    try:
        _write_text_nofollow_atomic(path, json.dumps(payload, indent=2, sort_keys=True) + "\n", mode=0o600)
    except OSError as exc:
        raise grokbot_mcp.ConfigurationError("invalid") from exc
    return payload


def load_config(target: Path, instance: str) -> dict[str, Any]:
    path = config_path(target, instance)
    try:
        payload = json.loads(_read_regular_text(path))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise grokbot_mcp.ConfigurationError("invalid") from exc
    if not isinstance(payload, dict) or payload.get("schema") != CONFIG_SCHEMA:
        raise grokbot_mcp.ConfigurationError("invalid")
    if payload.get("instance") != instance:
        raise grokbot_mcp.ConfigurationError("invalid")
    return _validate_config(payload)


def _resolve_bearer(reference: dict[str, Any]) -> str:
    kind = reference.get("kind")
    if kind == "env":
        name = reference.get("name")
        if not isinstance(name, str):
            raise grokbot_mcp.ConfigurationError("invalid")
        return grokbot_mcp.load_bearer(bearer_file=None, bearer_env=name)
    if kind == "file":
        raw_path = reference.get("path")
        if not isinstance(raw_path, str):
            raise grokbot_mcp.ConfigurationError("invalid")
        return grokbot_mcp.load_bearer(bearer_file=Path(raw_path), bearer_env=None)
    raise grokbot_mcp.ConfigurationError("invalid")


def build_request_config(target: Path, instance: str) -> tuple[dict[str, Any], str]:
    config = load_config(target, instance)
    return config, _resolve_bearer(config["bearer"])


def doctor(target: Path, instance: str, *, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> list[dict[str, str]]:
    """Run sanitized diagnostics; details never include credentials or job content."""
    checks: list[dict[str, str]] = []

    def record(name: str, ok: bool) -> None:
        checks.append({"check": name, "status": "ok" if ok else "fail"})

    try:
        from mcp.server import MCPServer  # noqa: F401

        record("dependency", True)
    except ImportError:
        record("dependency", False)

    try:
        config, bearer = build_request_config(target, instance)
        record("config", True)
    except (grokbot_mcp.ConfigurationError, OSError, ValueError, KeyError):
        record("config", False)
        return checks

    queue_dir = target / QUEUE_STATE_DIR
    writable = (
        os.access(queue_dir, os.W_OK)
        if queue_dir.is_dir()
        else os.access(config_path(target, instance).parent, os.W_OK)
    )
    record("permissions", bool(writable))

    try:
        grokbot_jobs.status(target)
        record("queue", True)
    except (grokbot_jobs.GrokbotJobError, ValueError, OSError):
        record("queue", False)

    record("endpoint", _health_check(config, bearer, timeout))
    return checks


def canary(target: Path, instance: str, *, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Bounded non-mutating authentication and inventory verification."""
    result: dict[str, Any] = {"ok": False, "instance": instance}
    try:
        config, bearer = build_request_config(target, instance)
    except (grokbot_mcp.ConfigurationError, OSError, ValueError, KeyError):
        result["reason"] = "config"
        return result

    base = _base_url(config)
    health = _request_json(f"{base}/health", bearer, timeout, method="GET")
    anonymous_status = _anonymous_health_status(f"{base}/health", timeout)
    tools_response = _tools_list(base, bearer, timeout)

    if health is None or health.get("ok") is not True or health.get("role") != instance:
        result["reason"] = "health"
        return result
    if anonymous_status not in {401, 403}:
        result["reason"] = "auth"
        return result
    if tools_response is None:
        result["reason"] = "inventory-unreachable"
        return result
    names = {tool.get("name") for tool in tools_response if isinstance(tool, dict)}
    expected = set(grokbot_mcp.OPERATOR_TOOLS if instance == "operator" else grokbot_mcp.WORKER_TOOLS)
    if names != expected:
        result["reason"] = "inventory-mismatch"
        return result

    result.update(
        {
            "ok": True,
            "health": health,
            "auth_rejected_without_bearer": True,
            "tools": sorted(expected),
        }
    )
    return result


def render_unit(config: dict[str, Any], *, python: str, exec_root: Path) -> str:
    config = _validate_config(config)
    instance = config["instance"]
    bind = config["bind"]
    reference = config["bearer"]
    args = [
        python,
        "-m",
        "brigade",
        "run",
        "cloud",
        "grokbot",
        "serve",
        "--instance",
        instance,
        "--target",
        str(exec_root),
        "--bind",
        bind,
    ]
    for host in config.get("allowed_hosts", []):
        args += ["--allow-host", host]
    for origin in config.get("allowed_origins", []):
        args += ["--allow-origin", origin]
    if reference["kind"] == "file":
        args += ["--bearer-file", reference["path"]]
    elif reference["kind"] == "env":
        args += ["--bearer-env", reference["name"]]
    exec_start = " ".join(_systemd_quote(argument) for argument in args)
    writable_state = _systemd_quote(str((exec_root / QUEUE_STATE_DIR).resolve()))
    return (
        "# Generated by brigade run cloud grokbot install-service.\n"
        f"# Unit: {unit_name(instance)}\n"
        "[Unit]\n"
        f"Description=Brigade Grok Bot MCP listener ({instance})\n"
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


def write_unit(
    config: dict[str, Any],
    out_dir: Path,
    *,
    exec_root: Path,
    force: bool = False,
    python: str | None = None,
) -> Path:
    """Render one role-scoped unit; never overwrites anything without force."""
    rendered = render_unit(config, python=python or sys.executable, exec_root=exec_root)
    path = out_dir / unit_name(config["instance"])
    try:
        existing = _read_regular_text(path)
    except FileNotFoundError:
        existing = None
    except OSError as exc:
        if not force or not _path_is_symlink(path):
            raise ServiceRenderError(f"refusing unsafe unit path: {path.name}") from exc
        existing = None
    if existing == rendered:
        return path
    if existing is not None and not force:
        raise ServiceRenderError(f"refusing to overwrite existing unit: {path.name}")
    try:
        _write_text_nofollow_atomic(path, rendered, mode=0o644, replace_symlink=force)
    except OSError as exc:
        raise ServiceRenderError(f"refusing unsafe unit path: {path.name}") from exc
    return path


def _path_is_symlink(path: Path) -> bool:
    """Return whether a final path component is a link or Windows reparse point."""
    try:
        metadata = os.lstat(path)
    except OSError:
        return False
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_point)


def _open_parent_nofollow(path: Path, *, create: bool) -> int:
    """Hold ``path.parent`` while rejecting every POSIX symlink component."""
    parent = path.parent.absolute()
    if os.name != "posix" or not getattr(os, "O_NOFOLLOW", 0):
        return _open_windows_parent_nofollow(parent, create=create)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(parent.anchor, flags)
    try:
        for component in parent.parts[1:]:
            if component in {"", ".", ".."}:
                raise OSError("unsafe output directory")
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(component, 0o755, dir_fd=descriptor)
                child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_windows_parent_nofollow(parent: Path, *, create: bool) -> int:
    """Hold a Windows parent directory through the reparse-point-safe adapter."""
    if sys.platform != "win32":
        raise OSError("no-follow directory operations are unavailable")
    from .work_cmd import nt_dirfd

    anchor = Path(parent.anchor)
    descriptor = nt_dirfd.open_root_directory(anchor, writable=create)
    try:
        for component in parent.relative_to(anchor).parts:
            try:
                child = nt_dirfd.open_child_directory(descriptor, component, writable=create)
            except FileNotFoundError:
                if not create:
                    raise
                nt_dirfd.mkdir_child(descriptor, component)
                child = nt_dirfd.open_child_directory(descriptor, component, writable=create)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read_regular_text(path: Path) -> str:
    """Read one regular file without traversing a symlink or reparse point."""
    parent = _open_parent_nofollow(path, create=False)
    descriptor = -1
    try:
        if os.name == "posix":
            descriptor = os.open(
                path.name,
                os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                dir_fd=parent,
            )
        else:
            from .work_cmd import nt_dirfd

            descriptor = nt_dirfd.open_file(parent, path.name, os.O_RDONLY)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("output is not a regular file")
        with os.fdopen(os.dup(descriptor), "r", encoding="utf-8") as handle:
            return handle.read()
    finally:
        if descriptor != -1:
            os.close(descriptor)
        os.close(parent)


def _write_text_nofollow_atomic(path: Path, data: str, *, mode: int, replace_symlink: bool = False) -> None:
    """Atomically publish a regular file without following its destination link."""
    parent = _open_parent_nofollow(path, create=True)
    temporary = f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor = -1
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        if os.name == "posix":
            descriptor = os.open(temporary, flags | os.O_NOFOLLOW, mode, dir_fd=parent)
        else:
            from .work_cmd import nt_dirfd

            descriptor = nt_dirfd.open_file(parent, temporary, flags, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            if os.name == "posix":
                os.fchmod(handle.fileno(), mode)
        if os.name == "posix":
            try:
                existing = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                existing = None
            if existing is not None and stat.S_ISLNK(existing.st_mode) and not replace_symlink:
                raise OSError("output is a symlink")
            if existing is not None and not (stat.S_ISREG(existing.st_mode) or stat.S_ISLNK(existing.st_mode)):
                raise OSError("output is not a regular file")
            os.replace(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        else:
            from .work_cmd import nt_dirfd

            if not replace_symlink and _path_is_symlink(path):
                raise OSError("output is a reparse point")
            nt_dirfd.replace_children(parent, temporary, path.name)
    except BaseException:
        try:
            if os.name == "posix":
                os.unlink(temporary, dir_fd=parent)
            else:
                from .work_cmd import nt_dirfd

                nt_dirfd.unlink_child(parent, temporary)
        except FileNotFoundError:
            pass
        raise
    finally:
        if descriptor != -1:
            os.close(descriptor)
        os.close(parent)


def _base_url(config: dict[str, Any]) -> str:
    host, port = grokbot_mcp.parse_bind(config["bind"])
    return f"http://{_connect_host(host)}:{port}"


def _connect_host(host: str) -> str:
    if host in {"localhost", "0.0.0.0", "::", ""} or host.startswith("::"):
        return "127.0.0.1"
    return host


def _health_check(config: dict[str, Any], bearer: str, timeout: int) -> bool:
    payload = _request_json(f"{_base_url(config)}/health", bearer, timeout, method="GET")
    return payload is not None and payload.get("ok") is True and payload.get("service") == "grokbot-mcp"


def _anonymous_health_status(url: str, timeout: int) -> int | None:
    request = urllib.request.Request(url, method="GET")
    try:
        with _open_http(request, timeout) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _open_http(request: urllib.request.Request, timeout: int) -> Any:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    return opener.open(request, timeout=timeout)


def _request_json(url: str, bearer: str | None, timeout: int, *, method: str) -> dict[str, Any] | None:
    request = urllib.request.Request(url, method=method)
    if bearer is not None:
        request.add_header("Authorization", f"Bearer {bearer}")
    try:
        with _open_http(request, timeout) as response:
            body = response.read(MAX_CANARY_BODY_BYTES + 1)
            if len(body) > MAX_CANARY_BODY_BYTES:
                return None
            payload = json.loads(body.decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _tools_list(base: str, bearer: str, timeout: int) -> list[Any] | None:
    try:
        return asyncio.run(asyncio.wait_for(_tools_list_with_session(base, bearer), timeout=timeout))
    except Exception:
        return None


async def _tools_list_with_session(base: str, bearer: str) -> list[Any]:
    client_session, streamable_http_client, async_client = _mcp_client_components()
    async with async_client(headers={"Authorization": f"Bearer {bearer}"}, trust_env=False) as http_client:
        async with streamable_http_client(f"{base}/mcp", http_client=http_client) as streams:
            read_stream, write_stream = streams
            async with client_session(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.list_tools()
    return [tool.model_dump() for tool in result.tools]


def _mcp_client_components() -> tuple[Any, Any, Any]:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    import httpx2

    return ClientSession, streamable_http_client, httpx2.AsyncClient


def _validate_config(config: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(config, dict) or config.get("schema") != CONFIG_SCHEMA:
        raise grokbot_mcp.ConfigurationError("invalid")
    if set(config) != {"schema", "instance", "bind", "allowed_hosts", "allowed_origins", "bearer"}:
        raise grokbot_mcp.ConfigurationError("invalid")
    instance = config.get("instance")
    bind = config.get("bind")
    allowed_hosts = config.get("allowed_hosts")
    allowed_origins = config.get("allowed_origins")
    if not isinstance(instance, str) or instance not in grokbot_mcp.INSTANCES or not isinstance(bind, str):
        raise grokbot_mcp.ConfigurationError("invalid")
    host, _ = grokbot_mcp.parse_bind(bind)
    if not isinstance(allowed_hosts, list) or not isinstance(allowed_origins, list):
        raise grokbot_mcp.ConfigurationError("invalid")
    if any(not grokbot_mcp._valid_host(value) for value in allowed_hosts):
        raise grokbot_mcp.ConfigurationError("invalid")
    if any(not grokbot_mcp._valid_origin(value) for value in allowed_origins):
        raise grokbot_mcp.ConfigurationError("invalid")
    if not grokbot_mcp._is_loopback(host) and (not allowed_hosts or not allowed_origins):
        raise grokbot_mcp.ConfigurationError("invalid")
    reference = config.get("bearer")
    if not isinstance(reference, dict):
        raise grokbot_mcp.ConfigurationError("invalid")
    kind = reference.get("kind")
    if kind == "env" and set(reference) == {"kind", "name"} and isinstance(reference.get("name"), str):
        if grokbot_mcp.ENVIRONMENT_NAME_RE.fullmatch(reference["name"]):
            return config
    if kind == "file" and set(reference) == {"kind", "path"} and isinstance(reference.get("path"), str):
        _systemd_quote(reference["path"])
        return config
    raise grokbot_mcp.ConfigurationError("invalid")


def _systemd_quote(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "$" in value
        or "%" in value
        or any(unicodedata.category(char).startswith("C") for char in value)
    ):
        raise grokbot_mcp.ConfigurationError("invalid")
    if _SYSTEMD_UNQUOTED_ARG_RE.fullmatch(value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
