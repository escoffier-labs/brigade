"""Dot and Duet lifecycle compatibility for historical pack invocation paths."""

from __future__ import annotations

import inspect
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

from brigade import dot

from . import transport as grokbot_mcp, service_render as grokbot_ops

from .connector_files import (
    PackError,
    _assert_config_destination_safe,
    _assert_removable,
    _exists_or_link,
    _is_relative_to,
    _matches_regular_file_snapshot,
    _owned_unit_dir,
    _parse_version,
    _restore_regular_file,
    _snapshot_regular_file,
)

PACK_SCHEMA = "brigade.grokbot.connector-pack.v1"
INSTANCE_SCHEMA = "brigade.grokbot.connector-instance.v1"
INSTANCE_DIR = Path(".brigade") / "grokbot" / "packs"
PACK_KEYS = frozenset({"schema", "id", "version", "kind", "instance", "default_bind", "public_route", "tools"})
BIND_DRIFT_CHECK = "bind-drift"
INGRESS_PORT_MISMATCH_CHECK = "ingress-port-mismatch"
ALLOWED_HOST_MISSING_CHECK = "allowed-host-missing"
NO_PUBLIC_ROUTE_CHECK = "no-public-route"
REACHABILITY_NO_PUBLIC_ROUTE = "no-public-route"
REACHABILITY_HOST_LOCAL = "host-local"
REACHABILITY_PUBLIC = "public"
_MAX_CLOUDFLARED_PROCS = 16
_MAX_METRICS_ENDPOINTS = 8
_MAX_PUBLIC_HOSTS = 8
_MAX_CONFIG_BYTES = 65_536
_PROBE_TIMEOUT_SECONDS = 2
INSTANCE_KEYS = frozenset(
    {
        "schema",
        "pack_id",
        "pack_version",
        "bind",
        "allowed_hosts",
        "allowed_origins",
        "public_route",
        "bearer",
    }
)
INSTANCE_OPTIONAL_KEYS = frozenset({"host_local"})
CONNECTOR_DEFAULT_BINDS = {"dot": "127.0.0.1:8779", "duet": "127.0.0.1:8780"}
DOT_PACK_ID = "dot"
DUET_PACK_ID = "duet"
DOT_FAMILY_PACK_IDS = frozenset({DOT_PACK_ID, DUET_PACK_ID})
DOT_INSTANCE_KEYS = (INSTANCE_KEYS - {"bearer"}) | frozenset({"runtime_path"})
VERSION_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
PACK_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
PUBLIC_ROUTE_RE = re.compile(r"^$|^/[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)*$")


def _connector_tools(pack_id: str) -> frozenset[str]:
    if pack_id in DOT_FAMILY_PACK_IDS:
        return dot.profile_for(pack_id).tools
    return frozenset()


def _profile_kwargs(pack_id: str) -> dict[str, str]:
    """Dot keeps its original lifecycle calls; other profiles name themselves."""
    return {} if pack_id == DOT_PACK_ID else {"pack_id": pack_id}


def _connector_pack(pack_id: str) -> dict[str, Any]:
    return {
        "schema": PACK_SCHEMA,
        "id": pack_id,
        "version": "1.0.0",
        "kind": "connector",
        "instance": pack_id,
        "default_bind": CONNECTOR_DEFAULT_BINDS[pack_id],
        "public_route": "",
        "tools": sorted(_connector_tools(pack_id)),
    }


_PACKAGED = tuple(_connector_pack(pack_id) for pack_id in sorted(DOT_FAMILY_PACK_IDS))


def packaged_manifests() -> tuple[dict[str, Any], ...]:
    """Return copies of the closed first-party pack manifests."""
    return tuple(dict(pack) for pack in _PACKAGED)


def instance_config_path(target: Path, pack_id: str) -> Path:
    return target / INSTANCE_DIR / f"{pack_id}.json"


def _packaged_packs() -> list[dict[str, Any]]:
    return [dict(pack) for pack in validate_registry(packaged_manifests())]


def list_packs(target: Path | None = None) -> list[dict[str, Any]]:
    """Return packaged manifests plus installed bind and reachability.

    ``installed_bind`` and ``reachability`` are read from
    ``.brigade/grokbot/packs/<id>.json``. The pack CLI dispatcher does not pass
    ``--target`` into this function, so a missing target uses that command's
    target when present, else ``.``.
    """
    resolved = target if target is not None else _cli_pack_target()
    packs = _packaged_packs()
    for pack in packs:
        pack["installed_bind"] = _peek_installed_bind(resolved, pack["id"])
        pack["reachability"] = _pack_reachability(resolved, pack)
    return packs


def show_pack(pack_id: str, target: Path | None = None) -> dict[str, Any]:
    for pack in list_packs(target):
        if pack["id"] == pack_id:
            return pack
    raise PackError("unknown-pack")


def validate_registry(manifests: Iterable[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    """Validate exact-key first-party manifests and cross-pack collisions."""
    validated: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_ports: set[int] = set()
    routes: list[str] = []
    for raw in manifests:
        pack = _validate_pack(raw)
        if pack["id"] in seen_ids:
            raise PackError("duplicate-pack-id")
        host, port = _parse_default_bind(pack["default_bind"])
        if not grokbot_mcp._is_loopback(host):
            raise PackError("unsafe-bind")
        if port in seen_ports:
            raise PackError("duplicate-port")
        route = pack["public_route"]
        if any(_routes_overlap(route, existing) for existing in routes):
            raise PackError("overlapping-public-route")
        seen_ids.add(pack["id"])
        seen_ports.add(port)
        routes.append(route)
        validated.append(pack)
    return tuple(sorted(validated, key=lambda item: item["id"]))


def preview_setup(
    target: Path,
    pack_id: str,
    *,
    bind: str | None = None,
    allowed_hosts: list[str] | None = None,
    allowed_origins: list[str] | None = None,
    bearer_env: str | None = None,
    bearer_file: Path | None = None,
    bearer: dict[str, Any] | None = None,
    runtime_path: str | Path | None = None,
    ledger_path: str | Path | None = None,
    action_state_path: str | Path | None = None,
    approval_dir: str | Path | None = None,
    owner_workspace: str | Path | None = None,
) -> dict[str, Any]:
    return _setup(
        target,
        pack_id,
        apply=False,
        bind=bind,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
        bearer_env=bearer_env,
        bearer_file=bearer_file,
        bearer=bearer,
        runtime_path=runtime_path,
        ledger_path=ledger_path,
        action_state_path=action_state_path,
        approval_dir=approval_dir,
        owner_workspace=owner_workspace,
    )


def apply_setup(
    target: Path,
    pack_id: str,
    *,
    bind: str | None = None,
    allowed_hosts: list[str] | None = None,
    allowed_origins: list[str] | None = None,
    bearer_env: str | None = None,
    bearer_file: Path | None = None,
    bearer: dict[str, Any] | None = None,
    runtime_path: str | Path | None = None,
    ledger_path: str | Path | None = None,
    action_state_path: str | Path | None = None,
    approval_dir: str | Path | None = None,
    owner_workspace: str | Path | None = None,
) -> dict[str, Any]:
    return _setup(
        target,
        pack_id,
        apply=True,
        bind=bind,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
        bearer_env=bearer_env,
        bearer_file=bearer_file,
        bearer=bearer,
        runtime_path=runtime_path,
        ledger_path=ledger_path,
        action_state_path=action_state_path,
        approval_dir=approval_dir,
        owner_workspace=owner_workspace,
    )


def doctor(target: Path, pack_id: str, *, service_result: bool = False) -> list[dict[str, str]]:
    pack = show_pack(pack_id, target)
    if pack["id"] in DOT_FAMILY_PACK_IDS:
        checks = dot.doctor(target, **_profile_kwargs(pack["id"]))
    else:
        raise PackError("unknown-pack")
    checks.extend(_installed_bind_checks(target, pack))
    checks.extend(_no_public_route_checks(target, pack))
    if service_result:
        checks.append(grokbot_ops.inspect_service_result(pack["instance"]))
    return checks


def canary(target: Path, pack_id: str) -> dict[str, Any]:
    pack = show_pack(pack_id)
    if pack["id"] in DOT_FAMILY_PACK_IDS:
        return dot.canary(target, **_profile_kwargs(pack["id"]))
    raise PackError("unknown-pack")


_PLATFORM_REFUSALS = frozenset({"unsupported-platform", "unsupported_on_windows"})


def _raise_platform_refusal(exc: Exception) -> None:
    """Keep an explicit platform refusal instead of folding it into unsafe-path."""
    if isinstance(exc, dot.DotError) and getattr(exc, "code", None) in _PLATFORM_REFUSALS:
        raise PackError(str(exc)) from exc


def render_install_service(target: Path, pack_id: str) -> str:
    pack = show_pack(pack_id)
    try:
        if pack["id"] in DOT_FAMILY_PACK_IDS:
            if pack["id"] == DOT_PACK_ID:
                return dot.render_unit(target, python=sys.executable)
            return dot.render_unit(target, python=sys.executable, pack_id=pack["id"])
        raise PackError("unknown-pack")
    except PackError:
        raise
    except (grokbot_mcp.ConfigurationError, grokbot_ops.ServiceRenderError, OSError) as exc:
        raise PackError("unsafe-path") from exc
    except Exception as exc:
        _raise_platform_refusal(exc)
        if isinstance(exc, (dot.DotError,)):
            raise PackError("unsafe-path") from exc
        raise


def apply_install_service(target: Path, pack_id: str, *, out_dir: Path, force: bool = False) -> dict[str, Any]:
    pack = show_pack(pack_id)
    try:
        if pack["id"] in DOT_FAMILY_PACK_IDS:
            path = dot.write_unit(target, out_dir, force=force, **_profile_kwargs(pack["id"]))
        else:
            raise PackError("unknown-pack")
    except PackError:
        raise
    except (grokbot_mcp.ConfigurationError, grokbot_ops.ServiceRenderError, OSError) as exc:
        raise PackError("unsafe-path") from exc
    except Exception as exc:
        _raise_platform_refusal(exc)
        if isinstance(exc, (dot.DotError,)):
            raise PackError("unsafe-path") from exc
        raise
    return {"action": "install-service", "apply": True, "pack_id": pack_id, "unit": path.name}


def preview_update(target: Path, pack_id: str) -> dict[str, Any]:
    return _update(target, pack_id, apply=False)


def apply_update(target: Path, pack_id: str) -> dict[str, Any]:
    return _update(target, pack_id, apply=True)


def preview_remove(target: Path, pack_id: str, *, unit_dir: Path | None = None) -> dict[str, Any]:
    return _remove(target, pack_id, apply=False, unit_dir=unit_dir)


def apply_remove(target: Path, pack_id: str, *, unit_dir: Path | None = None) -> dict[str, Any]:
    return _remove(target, pack_id, apply=True, unit_dir=unit_dir)


def _setup(
    target: Path,
    pack_id: str,
    *,
    apply: bool,
    bind: str | None,
    allowed_hosts: list[str] | None,
    allowed_origins: list[str] | None,
    bearer_env: str | None,
    bearer_file: Path | None,
    bearer: dict[str, Any] | None,
    runtime_path: str | Path | None = None,
    ledger_path: str | Path | None = None,
    action_state_path: str | Path | None = None,
    approval_dir: str | Path | None = None,
    owner_workspace: str | Path | None = None,
) -> dict[str, Any]:
    pack = show_pack(pack_id)
    if pack_id in DOT_FAMILY_PACK_IDS and any(value is not None for value in (bearer_env, bearer_file, bearer)):
        raise PackError("unexpected-key")
    reference = None
    chosen_bind = bind or pack["default_bind"]
    _parse_default_bind(chosen_bind)
    hosts = sorted(set(allowed_hosts or []))
    origins = sorted(set(allowed_origins or []))
    payload = {
        "schema": INSTANCE_SCHEMA,
        "pack_id": pack_id,
        "pack_version": pack["version"],
        "bind": chosen_bind,
        "allowed_hosts": hosts,
        "allowed_origins": origins,
        "public_route": pack["public_route"],
    }
    if reference is not None:
        payload["bearer"] = reference
    if pack["id"] in DOT_FAMILY_PACK_IDS:
        if (
            owner_workspace is not None
            or ledger_path is not None
            or action_state_path is not None
            or approval_dir is not None
        ):
            raise PackError("unexpected-key")
        payload["runtime_path"] = _dot_runtime_reference(runtime_path, pack_id)
    else:
        raise PackError("unexpected-key")
    _validate_instance_config(payload, pack_id)
    _assert_setup_bind_available(target, pack_id, chosen_bind)
    writes = [str(instance_config_path(Path("."), pack_id))]
    result = {"action": "setup", "apply": apply, "pack_id": pack_id, "bind": chosen_bind, "writes": writes}
    if not apply:
        return result
    _apply_setup_configs(
        target,
        pack_id,
        instance=pack["instance"],
        kind=pack["kind"],
        payload=payload,
        bind=chosen_bind,
        hosts=hosts,
        origins=origins,
        reference=reference,
    )
    return result


def _apply_setup_configs(
    target: Path,
    pack_id: str,
    *,
    instance: str,
    kind: str,
    payload: dict[str, Any],
    bind: str,
    hosts: list[str],
    origins: list[str],
    reference: Mapping[str, str] | None,
) -> None:
    pack_path = instance_config_path(target, pack_id)
    destinations: list[Path] = [pack_path]
    _assert_config_destination_safe(pack_path)
    snapshots = [_snapshot_regular_file(path) for path in destinations]
    try:
        _write_instance_config(target, pack_id, payload)
    except (grokbot_mcp.ConfigurationError, OSError, PackError) as exc:
        rollback_failed = False
        for dest, snap in zip(destinations, snapshots, strict=True):
            try:
                _restore_regular_file(dest, snap)
            except PackError:
                rollback_failed = True
            if not _matches_regular_file_snapshot(dest, snap):
                rollback_failed = True
        if rollback_failed:
            raise PackError("rollback-failed") from exc
        if isinstance(exc, PackError):
            raise
        raise PackError("unsafe-path") from exc


def _update(target: Path, pack_id: str, *, apply: bool) -> dict[str, Any]:
    pack = show_pack(pack_id)
    config = _load_instance_config(target, pack_id)
    installed = _parse_version(config["pack_version"])
    packaged = _parse_version(pack["version"])
    if installed[0] != packaged[0] or installed > packaged:
        raise PackError("incompatible-version")
    result = {
        "action": "update",
        "apply": apply,
        "pack_id": pack_id,
        "from_version": config["pack_version"],
        "to_version": pack["version"],
    }
    if not apply or config["pack_version"] == pack["version"]:
        return result
    updated = dict(config)
    updated["pack_version"] = pack["version"]
    _write_instance_config(target, pack_id, updated)
    return result


def _remove(target: Path, pack_id: str, *, apply: bool, unit_dir: Path | None) -> dict[str, Any]:
    pack = show_pack(pack_id)
    paths = _removal_paths(target, pack, unit_dir)
    result = {
        "action": "remove",
        "apply": apply,
        "pack_id": pack_id,
        "paths": [str(path.relative_to(target) if _is_relative_to(path, target) else path.name) for path in paths],
    }
    if not apply:
        return result
    for path in paths:
        try:
            grokbot_ops.remove_regular_file(path)
        except OSError as exc:
            raise PackError("unsafe-path") from exc
    return result


def _removal_paths(target: Path, pack: Mapping[str, Any], unit_dir: Path | None) -> list[Path]:
    if unit_dir is not None and not _owned_unit_dir(unit_dir, pack["instance"]):
        raise PackError("outside-owned-path")
    candidates: list[tuple[Path, bool]] = []
    pack_path = instance_config_path(target, pack["id"])
    if _exists_or_link(pack_path):
        candidates.append((pack_path, True))
    if unit_dir is not None:
        candidates.append((unit_dir / grokbot_ops.unit_name(pack["instance"]), False))
    paths: list[Path] = []
    for path, config_file in candidates:
        _assert_removable(path, config_file=config_file)
        paths.append(path)
    return paths


def _assert_setup_bind_available(target: Path, pack_id: str, chosen_bind: str) -> None:
    """Refuse a bind whose port is owned by another packaged or installed instance."""
    _host, chosen_port = _parse_default_bind(chosen_bind)
    if chosen_port in _occupied_setup_ports(target, exclude_pack_id=pack_id):
        raise PackError("duplicate-port")


def _occupied_setup_ports(target: Path, *, exclude_pack_id: str) -> set[int]:
    from .connector_registry import instance_config_path as connector_path

    ports: set[int] = {8766, 8767, 8768, 8771, 8773, 8777, 8778, 8781, 8782, 8783}
    for name in ("grokbot-memory", "grokbot-inbox", "fleet-steward", "obsidian-operator", "t3-dispatch"):
        path = target / INSTANCE_DIR / f"{name}.json"
        if path != instance_config_path(target, exclude_pack_id):
            port = _bind_port_from_existing_config(path)
            if port is not None:
                ports.add(port)
    generic_port = _bind_port_from_existing_config(connector_path(target, "cloud-agent"))
    if generic_port is not None:
        ports.add(generic_port)
    # Preserve the former registry's sorted reservation reads and error precedence.
    for name in ("dot", "duet", "implementation-worker", "operations-relay", "operator", "repository-scout"):
        if name == exclude_pack_id:
            continue
        if name in CONNECTOR_DEFAULT_BINDS:
            ports.add(_parse_default_bind(CONNECTOR_DEFAULT_BINDS[name])[1])
        port = _bind_port_from_existing_config(instance_config_path(target, name))
        if port is not None:
            ports.add(port)
        if name in {"operator", "repository-scout", "implementation-worker"}:
            port = _bind_port_from_existing_config(grokbot_ops.config_path(target, name))
            if port is not None:
                ports.add(port)
    return ports


def _write_instance_config(target: Path, pack_id: str, payload: dict[str, Any]) -> None:
    path = instance_config_path(target, pack_id)
    try:
        grokbot_ops._write_text_nofollow_atomic(
            path,
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            mode=0o600,
        )
    except OSError as exc:
        raise PackError("unsafe-path") from exc


def _load_instance_config(target: Path, pack_id: str) -> dict[str, Any]:
    path = instance_config_path(target, pack_id)
    try:
        payload = json.loads(grokbot_ops._read_regular_text(path))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PackError("unsafe-path") from exc
    if not isinstance(payload, dict):
        raise PackError("unexpected-key")
    return _validate_instance_config(payload, pack_id)


def _validate_pack(raw: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != PACK_KEYS:
        raise PackError("unexpected-key")
    pack_id = raw.get("id")
    version = raw.get("version")
    instance = raw.get("instance")
    kind = raw.get("kind")
    route = raw.get("public_route")
    tools = raw.get("tools")
    if raw.get("schema") != PACK_SCHEMA:
        raise PackError("unexpected-key")
    if not isinstance(pack_id, str) or not PACK_ID_RE.fullmatch(pack_id):
        raise PackError("unexpected-key")
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise PackError("invalid-version")
    if kind == "connector":
        if pack_id not in CONNECTOR_DEFAULT_BINDS or instance != pack_id:
            raise PackError("unexpected-key")
        expected_tools = _connector_tools(pack_id)
    else:
        raise PackError("unexpected-key")
    if not isinstance(route, str) or not PUBLIC_ROUTE_RE.fullmatch(route):
        raise PackError("overlapping-public-route" if route else "unexpected-key")
    if not isinstance(tools, list) or any(not isinstance(name, str) for name in tools):
        raise PackError("inventory-mismatch")
    if set(tools) != expected_tools or len(tools) != len(set(tools)):
        raise PackError("inventory-mismatch")
    bind = raw.get("default_bind")
    if not isinstance(bind, str):
        raise PackError("unsafe-bind")
    _parse_default_bind(bind)
    return {
        "schema": PACK_SCHEMA,
        "id": pack_id,
        "version": version,
        "kind": kind,
        "instance": instance,
        "default_bind": bind,
        "public_route": route,
        "tools": sorted(tools),
    }


def _validate_instance_config(payload: Mapping[str, Any], pack_id: str) -> dict[str, Any]:
    pack = show_pack(pack_id)
    expected_keys = _instance_keys(pack)
    present = set(payload)
    optional = present & INSTANCE_OPTIONAL_KEYS
    if present - optional != expected_keys or payload.get("schema") != INSTANCE_SCHEMA:
        raise PackError("unexpected-key")
    host_local = payload.get("host_local")
    if host_local is not None and host_local is not True and host_local is not False:
        raise PackError("unexpected-key")
    if payload.get("pack_id") != pack_id:
        raise PackError("unexpected-key")
    version = payload.get("pack_version")
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise PackError("invalid-version")
    bind = payload.get("bind")
    if not isinstance(bind, str):
        raise PackError("unsafe-bind")
    _parse_default_bind(bind)
    hosts = payload.get("allowed_hosts")
    origins = payload.get("allowed_origins")
    route = payload.get("public_route")
    if not isinstance(hosts, list) or not isinstance(origins, list):
        raise PackError("unexpected-key")
    if any(not grokbot_mcp._valid_host(value) for value in hosts):
        raise PackError("unexpected-key")
    if any(not grokbot_mcp._valid_origin(value) for value in origins):
        raise PackError("unexpected-key")
    if not isinstance(route, str) or not PUBLIC_ROUTE_RE.fullmatch(route):
        raise PackError("unexpected-key")
    if pack["id"] in DOT_FAMILY_PACK_IDS:
        _dot_runtime_reference(payload.get("runtime_path"), pack["id"])
    return dict(payload)


def _instance_keys(pack: Mapping[str, Any]) -> frozenset[str]:
    if pack["id"] in DOT_FAMILY_PACK_IDS:
        return DOT_INSTANCE_KEYS
    raise PackError("unknown-pack")


def _dot_runtime_reference(runtime_path: object, pack_id: str = DOT_PACK_ID) -> str:
    if runtime_path is None:
        raise PackError("missing-path-reference")
    try:
        schema = dot.profile_for(pack_id).runtime_schema
        return dot.validate_runtime_reference(str(runtime_path), schema=schema)
    except dot.DotError as exc:
        raise PackError("unsafe-path") from exc


def _cli_pack_target() -> Path:
    """Honor ``pack --target`` when the dispatcher calls ``list_packs()`` with no args."""
    for item in inspect.stack(context=0):
        if item.function == "_dispatch_grokbot_pack":
            candidate = item.frame.f_locals.get("target")
            if isinstance(candidate, Path):
                return candidate
    return Path(".")


def _peek_instance_payload(target: Path, pack_id: str) -> dict[str, Any] | None:
    path = instance_config_path(target, pack_id)
    try:
        if grokbot_ops._path_is_symlink(path) or path.is_symlink():
            return None
        payload = json.loads(grokbot_ops._read_regular_text(path))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _peek_installed_bind(target: Path, pack_id: str) -> str | None:
    payload = _peek_instance_payload(target, pack_id)
    if payload is None:
        return None
    bind = payload.get("bind")
    if not isinstance(bind, str):
        return None
    try:
        _parse_default_bind(bind)
    except PackError:
        return None
    return bind


def _peek_allowed_hosts(target: Path, pack_id: str) -> list[str]:
    payload = _peek_instance_payload(target, pack_id)
    if payload is None:
        return []
    hosts = payload.get("allowed_hosts")
    if not isinstance(hosts, list):
        return []
    return [host for host in hosts if isinstance(host, str)]


def _peek_public_route(target: Path, pack_id: str) -> str:
    payload = _peek_instance_payload(target, pack_id)
    if payload is None:
        return ""
    route = payload.get("public_route")
    return route if isinstance(route, str) else ""


def _peek_host_local(target: Path, pack_id: str) -> bool:
    payload = _peek_instance_payload(target, pack_id)
    return payload is not None and payload.get("host_local") is True


def _pack_reachability(target: Path, pack: Mapping[str, Any]) -> str | None:
    if pack["kind"] != "connector":
        return None
    pack_id = str(pack["id"])
    if _peek_installed_bind(target, pack_id) is None:
        return None
    if _peek_host_local(target, pack_id):
        return REACHABILITY_HOST_LOCAL
    if _peek_allowed_hosts(target, pack_id) or _peek_public_route(target, pack_id):
        return REACHABILITY_PUBLIC
    return REACHABILITY_NO_PUBLIC_ROUTE


def _no_public_route_checks(target: Path, pack: Mapping[str, Any]) -> list[dict[str, str]]:
    if pack["kind"] != "connector":
        return []
    pack_id = str(pack["id"])
    if _peek_installed_bind(target, pack_id) is None:
        return []
    if _peek_host_local(target, pack_id):
        return []
    if _peek_allowed_hosts(target, pack_id) or _peek_public_route(target, pack_id):
        return []
    return [
        {
            "check": NO_PUBLIC_ROUTE_CHECK,
            "status": "manual",
        }
    ]


def _public_allowed_hosts(hosts: Iterable[str]) -> tuple[str, ...]:
    public: list[str] = []
    for raw in hosts:
        if not grokbot_mcp._valid_host(raw):
            continue
        name = raw.casefold().split(":", 1)[0]
        if grokbot_mcp._is_loopback(name):
            continue
        if name not in public:
            public.append(name)
        if len(public) >= _MAX_PUBLIC_HOSTS:
            break
    return tuple(public)


def _installed_bind_checks(target: Path, pack: Mapping[str, Any]) -> list[dict[str, str]]:
    pack_id = str(pack["id"])
    installed = _peek_installed_bind(target, pack_id)
    if installed is None:
        return []
    checks: list[dict[str, str]] = [
        {
            "check": BIND_DRIFT_CHECK,
            "status": "ok" if installed == pack["default_bind"] else "warn",
        }
    ]
    public = _public_allowed_hosts(_peek_allowed_hosts(target, pack_id))
    if not public:
        return checks
    ingress = _ingress_bind_status(public, installed)
    if ingress is not None:
        checks.append({"check": INGRESS_PORT_MISMATCH_CHECK, "status": ingress})
    allowed = _listener_allows_public_hosts(installed, public)
    if allowed is not None:
        checks.append({"check": ALLOWED_HOST_MISSING_CHECK, "status": "ok" if allowed else "fail"})
    return checks


def _ingress_bind_status(public_hosts: tuple[str, ...], installed_bind: str) -> str | None:
    try:
        _host, installed_port = _parse_default_bind(installed_bind)
    except PackError:
        return None
    public = set(public_hosts)
    observed_mismatch = False
    observed_match = False
    for metrics_bind in _cloudflared_metrics_binds()[:_MAX_METRICS_ENDPOINTS]:
        payload = _fetch_metrics_config(metrics_bind)
        if payload is None:
            continue
        for hostname, origin_port in _ingress_origin_ports(payload):
            if hostname not in public:
                continue
            if origin_port == installed_port:
                observed_match = True
            else:
                observed_mismatch = True
    if observed_mismatch:
        return "fail"
    if observed_match:
        return "ok"
    return None


def _listener_allows_public_hosts(installed_bind: str, public_hosts: tuple[str, ...]) -> bool | None:
    try:
        host, port = _parse_default_bind(installed_bind)
    except PackError:
        return None
    url = f"http://{grokbot_ops._connect_host(host)}:{port}/health"
    seen = False
    for hostname in public_hosts:
        status = _http_status(url, host_header=hostname)
        if status is None:
            continue
        seen = True
        if status == 403:
            return False
    if not seen:
        return None
    return True


def _cloudflared_metrics_binds() -> tuple[str, ...]:
    root = Path("/proc")
    if not root.is_dir():
        return ()
    found: list[str] = []
    scanned = 0
    try:
        entries = list(root.iterdir())
    except OSError:
        return ()
    for entry in entries:
        if scanned >= _MAX_CLOUDFLARED_PROCS or len(found) >= _MAX_METRICS_ENDPOINTS:
            break
        if not entry.name.isdigit():
            continue
        try:
            comm = (entry / "comm").read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if comm != "cloudflared":
            continue
        scanned += 1
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        argv = [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]
        bind = _metrics_bind_from_argv(argv)
        if bind is not None:
            found.append(bind)
    return tuple(found)


def _metrics_bind_from_argv(argv: list[str]) -> str | None:
    for index, arg in enumerate(argv):
        if arg == "--metrics" and index + 1 < len(argv):
            return _loopback_metrics_bind(argv[index + 1])
        if arg.startswith("--metrics="):
            return _loopback_metrics_bind(arg.split("=", 1)[1])
    return None


def _loopback_metrics_bind(value: str) -> str | None:
    try:
        host, port = grokbot_mcp.parse_bind(value)
    except grokbot_mcp.ConfigurationError:
        return None
    connect = grokbot_ops._connect_host(host)
    if not grokbot_mcp._is_loopback(connect):
        return None
    return f"{connect}:{port}"


def _fetch_metrics_config(metrics_bind: str) -> dict[str, Any] | None:
    parsed = _loopback_metrics_bind(metrics_bind)
    if parsed is None:
        return None
    host, port = grokbot_mcp.parse_bind(parsed)
    payload = _fetch_json(f"http://{host}:{port}/config")
    return payload


def _ingress_origin_ports(payload: Mapping[str, Any]) -> list[tuple[str, int]]:
    config = payload.get("config")
    raw: object = None
    if isinstance(config, Mapping):
        raw = config.get("ingress")
    if raw is None:
        raw = payload.get("ingress")
    if not isinstance(raw, list):
        return []
    rules: list[tuple[str, int]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        hostname = item.get("hostname")
        service = item.get("service")
        if not isinstance(hostname, str) or not hostname or not isinstance(service, str):
            continue
        origin = _origin_port(service)
        if origin is None:
            continue
        rules.append((hostname.casefold().split(":", 1)[0], origin))
    return rules


def _origin_port(service: str) -> int | None:
    parsed = urlsplit(service)
    if parsed.scheme not in {"http", "https"}:
        return None
    host = parsed.hostname
    if host is None or not grokbot_mcp._is_loopback(host):
        return None
    if parsed.port is not None:
        return parsed.port
    return 80 if parsed.scheme == "http" else 443


def _fetch_json(url: str) -> dict[str, Any] | None:
    request = urllib.request.Request(url, method="GET")
    try:
        with grokbot_mcp._open_http(request, _PROBE_TIMEOUT_SECONDS) as response:
            body = response.read(_MAX_CONFIG_BYTES + 1)
    except urllib.error.HTTPError:
        return None
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None
    if len(body) > _MAX_CONFIG_BYTES:
        return None
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _http_status(url: str, *, host_header: str) -> int | None:
    request = urllib.request.Request(url, method="GET")
    request.add_unredirected_header("Host", host_header)
    try:
        with grokbot_mcp._open_http(request, _PROBE_TIMEOUT_SECONDS) as response:
            response.read(256)
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None


def _parse_default_bind(value: str) -> tuple[str, int]:
    try:
        host, port = grokbot_mcp.parse_bind(value)
    except grokbot_mcp.ConfigurationError as exc:
        raise PackError("unsafe-bind") from exc
    if not grokbot_mcp._is_loopback(host):
        raise PackError("unsafe-bind")
    return host, port


def _routes_overlap(left: str, right: str) -> bool:
    if not left or not right:
        return False
    left_parts = left.split("/")
    right_parts = right.split("/")
    length = min(len(left_parts), len(right_parts))
    return left_parts[:length] == right_parts[:length]


def _bind_port_from_existing_config(path: Path) -> int | None:
    """Read one bind port from a first-party config without following unsafe paths."""
    if grokbot_ops._path_is_symlink(path) or path.is_symlink():
        raise PackError("unsafe-path")
    try:
        raw = grokbot_ops._read_regular_text(path, max_bytes=65_536)
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise PackError("unsafe-path") from exc
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise PackError("unsafe-path") from exc
    if not isinstance(payload, dict):
        raise PackError("unsafe-path")
    bind = payload.get("bind")
    if not isinstance(bind, str):
        raise PackError("unsafe-path")
    _host, port = _parse_default_bind(bind)
    return port
