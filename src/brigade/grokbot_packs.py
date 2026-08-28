"""First-party Grok Bot connector-pack registry and preview-first lifecycle.

The registry is closed: only packaged queue-role manifests are accepted. Local
instance config stores secret references, never secret values. Apply may write
owned config and an explicitly selected unit file. This module does not start,
stop, or reload services.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import grokbot_backup, grokbot_cerebro, grokbot_fleet, grokbot_mcp, grokbot_ops

PACK_SCHEMA = "brigade.grokbot.connector-pack.v1"
INSTANCE_SCHEMA = "brigade.grokbot.connector-instance.v1"
INSTANCE_DIR = Path(".brigade") / "grokbot" / "packs"
PACK_KEYS = frozenset({"schema", "id", "version", "kind", "instance", "default_bind", "public_route", "tools"})
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
DEFAULT_BINDS = {
    "implementation-worker": "127.0.0.1:8768",
    "operator": "127.0.0.1:8766",
    "repository-scout": "127.0.0.1:8767",
}
CONNECTOR_DEFAULT_BINDS = {
    "backup-steward": "127.0.0.1:8772",
    "cerebro-memory": "127.0.0.1:8770",
    "fleet-steward": "127.0.0.1:8771",
}
STEWARD_PACK_IDS = frozenset({"backup-steward", "fleet-steward"})
FLEET_INSTANCE_KEYS = INSTANCE_KEYS | frozenset(
    {
        "runtime_path",
        "ledger_path",
        "action_state_path",
        "approval_dir",
    }
)
CONNECTOR_INSTANCE_KEYS = INSTANCE_KEYS | frozenset({"cli_executable", "workdir"})
VERSION_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
PACK_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
PUBLIC_ROUTE_RE = re.compile(r"^$|^/[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)*$")
SECRET_BEARER_KEYS = frozenset({"value", "token", "secret", "password", "bearer"})


class PackError(ValueError):
    """A rejected pack registry or lifecycle request."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _queue_pack(instance: str) -> dict[str, Any]:
    return {
        "schema": PACK_SCHEMA,
        "id": instance,
        "version": "1.0.0",
        "kind": "queue-role",
        "instance": instance,
        "default_bind": DEFAULT_BINDS[instance],
        "public_route": "",
        "tools": sorted(grokbot_mcp.tools_for_instance(instance)),
    }


def _connector_tools(pack_id: str) -> frozenset[str]:
    if pack_id == "backup-steward":
        return grokbot_backup.TOOLS
    if pack_id == "cerebro-memory":
        return grokbot_cerebro.TOOLS
    if pack_id == "fleet-steward":
        return grokbot_fleet.TOOLS
    return frozenset()


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


_PACKAGED = tuple(
    [_queue_pack(instance) for instance in sorted(DEFAULT_BINDS)]
    + [_connector_pack(pack_id) for pack_id in sorted(CONNECTOR_DEFAULT_BINDS)]
)


def packaged_manifests() -> tuple[dict[str, Any], ...]:
    """Return copies of the closed first-party pack manifests."""
    return tuple(dict(pack) for pack in _PACKAGED)


def instance_config_path(target: Path, pack_id: str) -> Path:
    return target / INSTANCE_DIR / f"{pack_id}.json"


def list_packs() -> list[dict[str, Any]]:
    return [dict(pack) for pack in validate_registry(packaged_manifests())]


def show_pack(pack_id: str) -> dict[str, Any]:
    for pack in list_packs():
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
    cli_executable: str | Path | None = None,
    workdir: str | Path | None = None,
    runtime_path: str | Path | None = None,
    ledger_path: str | Path | None = None,
    action_state_path: str | Path | None = None,
    approval_dir: str | Path | None = None,
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
        cli_executable=cli_executable,
        workdir=workdir,
        runtime_path=runtime_path,
        ledger_path=ledger_path,
        action_state_path=action_state_path,
        approval_dir=approval_dir,
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
    cli_executable: str | Path | None = None,
    workdir: str | Path | None = None,
    runtime_path: str | Path | None = None,
    ledger_path: str | Path | None = None,
    action_state_path: str | Path | None = None,
    approval_dir: str | Path | None = None,
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
        cli_executable=cli_executable,
        workdir=workdir,
        runtime_path=runtime_path,
        ledger_path=ledger_path,
        action_state_path=action_state_path,
        approval_dir=approval_dir,
    )


def doctor(target: Path, pack_id: str) -> list[dict[str, str]]:
    pack = show_pack(pack_id)
    if pack["kind"] == "queue-role":
        return grokbot_ops.doctor(target, pack["instance"])
    if pack["id"] == "fleet-steward":
        return grokbot_fleet.doctor(target)
    if pack["id"] == "backup-steward":
        return grokbot_backup.doctor(target)
    return grokbot_cerebro.doctor(target)


def canary(target: Path, pack_id: str) -> dict[str, Any]:
    pack = show_pack(pack_id)
    if pack["kind"] == "queue-role":
        return grokbot_ops.canary(target, pack["instance"])
    if pack["id"] == "fleet-steward":
        return grokbot_fleet.canary(target)
    if pack["id"] == "backup-steward":
        return grokbot_backup.canary(target)
    return grokbot_cerebro.canary(target)


def render_install_service(target: Path, pack_id: str) -> str:
    pack = show_pack(pack_id)
    try:
        if pack["kind"] == "queue-role":
            config = grokbot_ops.load_config(target, pack["instance"])
            return grokbot_ops.render_unit(config, python=sys.executable, exec_root=target)
        if pack["id"] == "fleet-steward":
            return grokbot_fleet.render_unit(target, python=sys.executable)
        if pack["id"] == "backup-steward":
            return grokbot_backup.render_unit(target, python=sys.executable)
        return grokbot_cerebro.render_unit(target, python=sys.executable)
    except PackError:
        raise
    except (grokbot_mcp.ConfigurationError, grokbot_ops.ServiceRenderError, OSError) as exc:
        raise PackError("unsafe-path") from exc
    except Exception as exc:
        if isinstance(exc, (grokbot_backup.BackupError, grokbot_cerebro.CerebroError, grokbot_fleet.FleetError)):
            raise PackError("unsafe-path") from exc
        raise


def apply_install_service(
    target: Path,
    pack_id: str,
    *,
    out_dir: Path,
    force: bool = False,
) -> dict[str, Any]:
    pack = show_pack(pack_id)
    try:
        if pack["kind"] == "queue-role":
            config = grokbot_ops.load_config(target, pack["instance"])
            path = grokbot_ops.write_unit(config, out_dir, exec_root=target, force=force)
        elif pack["id"] == "fleet-steward":
            path = grokbot_fleet.write_unit(target, out_dir, force=force)
        elif pack["id"] == "backup-steward":
            path = grokbot_backup.write_unit(target, out_dir, force=force)
        else:
            path = grokbot_cerebro.write_unit(target, out_dir, force=force)
    except PackError:
        raise
    except (grokbot_mcp.ConfigurationError, grokbot_ops.ServiceRenderError, OSError) as exc:
        raise PackError("unsafe-path") from exc
    except Exception as exc:
        if isinstance(exc, (grokbot_backup.BackupError, grokbot_cerebro.CerebroError, grokbot_fleet.FleetError)):
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
    cli_executable: str | Path | None,
    workdir: str | Path | None,
    runtime_path: str | Path | None = None,
    ledger_path: str | Path | None = None,
    action_state_path: str | Path | None = None,
    approval_dir: str | Path | None = None,
) -> dict[str, Any]:
    pack = show_pack(pack_id)
    reference = _bearer_reference(bearer_env=bearer_env, bearer_file=bearer_file, bearer=bearer)
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
        "bearer": reference,
    }
    fleet_paths = (runtime_path, ledger_path, action_state_path, approval_dir)
    if pack["kind"] == "queue-role":
        if cli_executable is not None or workdir is not None or any(path is not None for path in fleet_paths):
            raise PackError("unexpected-key")
    elif pack["id"] in STEWARD_PACK_IDS:
        if cli_executable is not None or workdir is not None:
            raise PackError("unexpected-key")
        payload.update(_steward_path_references(pack["id"], runtime_path, ledger_path, action_state_path, approval_dir))
    else:
        if any(path is not None for path in fleet_paths):
            raise PackError("unexpected-key")
        payload["cli_executable"] = _connector_path_reference(cli_executable, kind="executable")
        payload["workdir"] = _connector_path_reference(workdir, kind="directory")
        if payload["cli_executable"] == payload["workdir"]:
            raise PackError("unsafe-path")
    _validate_instance_config(payload, pack_id)
    _assert_setup_bind_available(target, pack_id, chosen_bind)
    writes = [str(instance_config_path(Path("."), pack_id))]
    if pack["kind"] == "queue-role":
        writes.append(str(grokbot_ops.config_path(Path("."), pack["instance"])))
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
    reference: Mapping[str, str],
) -> None:
    pack_path = instance_config_path(target, pack_id)
    destinations: list[Path] = [pack_path]
    _assert_config_destination_safe(pack_path)
    if kind == "queue-role":
        legacy_path = grokbot_ops.config_path(target, instance)
        _assert_config_destination_safe(legacy_path)
        destinations.append(legacy_path)
    snapshots = [_snapshot_regular_file(path) for path in destinations]
    try:
        _write_instance_config(target, pack_id, payload)
        if kind == "queue-role":
            grokbot_ops.save_config(
                target,
                instance=instance,
                bind=bind,
                allowed_hosts=hosts,
                allowed_origins=origins,
                bearer_env=reference["name"] if reference["kind"] == "env" else None,
                bearer_file=Path(reference["path"]) if reference["kind"] == "file" else None,
            )
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
    if pack["kind"] == "queue-role":
        legacy = grokbot_ops.config_path(target, pack["instance"])
        if _exists_or_link(legacy):
            candidates.append((legacy, True))
    if unit_dir is not None:
        candidates.append((unit_dir / grokbot_ops.unit_name(pack["instance"]), False))
    paths: list[Path] = []
    for path, config_file in candidates:
        _assert_removable(path, config_file=config_file)
        paths.append(path)
    return paths


def _owned_unit_dir(unit_dir: Path, instance: str) -> bool:
    if unit_dir.is_symlink() or not unit_dir.is_dir():
        return False
    unit = unit_dir / grokbot_ops.unit_name(instance)
    return unit.is_file() and not unit.is_symlink()


def _assert_removable(path: Path, *, config_file: bool) -> None:
    if grokbot_ops._path_is_symlink(path) or path.is_symlink():
        raise PackError("symlink")
    try:
        info = path.lstat()
    except OSError as exc:
        raise PackError("unsafe-path") from exc
    if not stat.S_ISREG(info.st_mode):
        raise PackError("unsafe-path")
    if config_file and stat.S_IMODE(info.st_mode) & 0o077:
        raise PackError("unsafe-permissions")
    parent = -1
    try:
        parent = grokbot_ops._open_parent_nofollow(path, create=False)
    except OSError as exc:
        raise PackError("unsafe-path") from exc
    finally:
        if parent != -1:
            os.close(parent)


def _exists_or_link(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _assert_config_destination_safe(path: Path) -> None:
    if grokbot_ops._path_is_symlink(path) or path.is_symlink():
        raise PackError("unsafe-path")
    parent = -1
    try:
        parent = grokbot_ops._open_parent_nofollow(path, create=False)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise PackError("unsafe-path") from exc
    finally:
        if parent != -1:
            os.close(parent)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise PackError("unsafe-path") from exc
    if not stat.S_ISREG(info.st_mode):
        raise PackError("unsafe-path")


def _snapshot_regular_file(path: Path) -> tuple[str, int] | None:
    if grokbot_ops._path_is_symlink(path) or path.is_symlink():
        raise PackError("unsafe-path")
    try:
        text = grokbot_ops._read_regular_text(path)
        mode = stat.S_IMODE(path.lstat().st_mode)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PackError("unsafe-path") from exc
    return text, mode


def _restore_regular_file(path: Path, snapshot: tuple[str, int] | None) -> None:
    if snapshot is None:
        if grokbot_ops._path_is_symlink(path) or path.is_symlink():
            raise PackError("unsafe-path")
        try:
            grokbot_ops.remove_regular_file(path)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise PackError("unsafe-path") from exc
        return
    if _matches_regular_file_snapshot(path, snapshot):
        return
    text, mode = snapshot
    try:
        grokbot_ops._write_text_nofollow_atomic(path, text, mode=mode)
    except OSError as exc:
        raise PackError("unsafe-path") from exc


def _matches_regular_file_snapshot(path: Path, snapshot: tuple[str, int] | None) -> bool:
    """Return True only when existence, exact contents, and mode match the snapshot."""
    try:
        current = _snapshot_regular_file(path)
    except PackError:
        return False
    if current is None or snapshot is None:
        return current is None and snapshot is None
    current_text, current_mode = current
    expected_text, expected_mode = snapshot
    return current_text.encode("utf-8") == expected_text.encode("utf-8") and current_mode == expected_mode


def _assert_setup_bind_available(target: Path, pack_id: str, chosen_bind: str) -> None:
    """Refuse a bind whose port is owned by another packaged or installed instance."""
    _host, chosen_port = _parse_default_bind(chosen_bind)
    if chosen_port in _occupied_setup_ports(target, exclude_pack_id=pack_id):
        raise PackError("duplicate-port")


def _occupied_setup_ports(target: Path, *, exclude_pack_id: str) -> set[int]:
    ports: set[int] = set()
    for pack in list_packs():
        if pack["id"] == exclude_pack_id:
            continue
        _host, port = _parse_default_bind(pack["default_bind"])
        ports.add(port)
        pack_port = _bind_port_from_existing_config(instance_config_path(target, pack["id"]))
        if pack_port is not None:
            ports.add(pack_port)
        if pack["kind"] == "queue-role":
            legacy_port = _bind_port_from_existing_config(grokbot_ops.config_path(target, pack["instance"]))
            if legacy_port is not None:
                ports.add(legacy_port)
    return ports


def _bind_port_from_existing_config(path: Path) -> int | None:
    """Read one bind port from a first-party config without following unsafe paths."""
    if grokbot_ops._path_is_symlink(path) or path.is_symlink():
        raise PackError("unsafe-path")
    try:
        raw = grokbot_ops._read_regular_text(path)
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise PackError("unsafe-path") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PackError("unsafe-path") from exc
    if not isinstance(payload, dict):
        raise PackError("unsafe-path")
    bind = payload.get("bind")
    if not isinstance(bind, str):
        raise PackError("unsafe-path")
    _host, port = _parse_default_bind(bind)
    return port


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
    if kind == "queue-role":
        if instance != pack_id or instance not in grokbot_mcp.INSTANCES:
            raise PackError("unexpected-key")
        expected_tools = grokbot_mcp.tools_for_instance(instance)
    elif kind == "connector":
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
    if set(payload) != expected_keys or payload.get("schema") != INSTANCE_SCHEMA:
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
    _validate_bearer_reference(payload.get("bearer"))
    if pack["id"] in STEWARD_PACK_IDS:
        _steward_path_references(
            pack["id"],
            payload.get("runtime_path"),
            payload.get("ledger_path"),
            payload.get("action_state_path"),
            payload.get("approval_dir"),
        )
    elif pack["kind"] == "connector":
        _connector_path_reference(payload.get("cli_executable"), kind="executable")
        workdir = _connector_path_reference(payload.get("workdir"), kind="directory")
        if payload.get("cli_executable") == workdir:
            raise PackError("unsafe-path")
    return dict(payload)


def _bearer_reference(
    *,
    bearer_env: str | None,
    bearer_file: Path | None,
    bearer: dict[str, Any] | None,
) -> dict[str, str]:
    if bearer is not None:
        if bearer_env is not None or bearer_file is not None:
            raise PackError("missing-secret-reference")
        return _validate_bearer_reference(bearer)
    if (bearer_env is None) == (bearer_file is None):
        raise PackError("missing-secret-reference")
    if bearer_env is not None:
        return _validate_bearer_reference({"kind": "env", "name": bearer_env})
    assert bearer_file is not None
    return _validate_bearer_reference({"kind": "file", "path": str(bearer_file.expanduser().resolve())})


def _validate_bearer_reference(reference: object) -> dict[str, str]:
    if not isinstance(reference, dict) or set(reference) & SECRET_BEARER_KEYS:
        raise PackError("weak-secret-reference")
    kind = reference.get("kind")
    if kind == "env" and set(reference) == {"kind", "name"}:
        name = reference.get("name")
        if isinstance(name, str) and grokbot_mcp.ENVIRONMENT_NAME_RE.fullmatch(name):
            return {"kind": "env", "name": name}
    if kind == "file" and set(reference) == {"kind", "path"}:
        path = reference.get("path")
        if isinstance(path, str) and path and "\x00" not in path:
            grokbot_ops._systemd_quote(path)
            return {"kind": "file", "path": path}
    raise PackError("weak-secret-reference")


def _instance_keys(pack: Mapping[str, Any]) -> frozenset[str]:
    if pack["kind"] == "queue-role":
        return INSTANCE_KEYS
    if pack["id"] in STEWARD_PACK_IDS:
        return FLEET_INSTANCE_KEYS
    return CONNECTOR_INSTANCE_KEYS


def _steward_path_references(
    pack_id: str,
    runtime_path: object,
    ledger_path: object,
    action_state_path: object,
    approval_dir: object,
) -> dict[str, str]:
    if any(value is None for value in (runtime_path, ledger_path, action_state_path, approval_dir)):
        raise PackError("missing-path-reference")
    try:
        if pack_id == "backup-steward":
            return grokbot_backup.validate_disjoint_state_paths(
                str(runtime_path),
                str(ledger_path),
                str(action_state_path),
                str(approval_dir),
            )
        return grokbot_fleet.validate_disjoint_state_paths(
            str(runtime_path),
            str(ledger_path),
            str(action_state_path),
            str(approval_dir),
        )
    except (grokbot_backup.BackupError, grokbot_fleet.FleetError) as exc:
        raise PackError("unsafe-path") from exc


def _connector_path_reference(value: object, *, kind: str) -> str:
    if value is None:
        raise PackError("missing-path-reference")
    if not isinstance(value, (str, Path)) or (isinstance(value, str) and not value):
        raise PackError("unsafe-path")
    from . import grokbot_cerebro

    try:
        text = str(value)
        if kind == "executable":
            return grokbot_cerebro.validate_cli_executable(text)
        return grokbot_cerebro.validate_workdir(text)
    except grokbot_cerebro.CerebroError as exc:
        raise PackError("unsafe-path") from exc


def _parse_default_bind(value: str) -> tuple[str, int]:
    try:
        host, port = grokbot_mcp.parse_bind(value)
    except grokbot_mcp.ConfigurationError as exc:
        raise PackError("unsafe-bind") from exc
    if not grokbot_mcp._is_loopback(host):
        raise PackError("unsafe-bind")
    return host, port


def _parse_version(value: str) -> tuple[int, int, int]:
    if not VERSION_RE.fullmatch(value):
        raise PackError("invalid-version")
    major, minor, patch = value.split(".")
    return int(major), int(minor), int(patch)


def _routes_overlap(left: str, right: str) -> bool:
    if not left or not right:
        return False
    left_parts = left.split("/")
    right_parts = right.split("/")
    length = min(len(left_parts), len(right_parts))
    return left_parts[:length] == right_parts[:length]


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
