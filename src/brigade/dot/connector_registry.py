"""Closed connector lifecycle, independent of queue and steward registries."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from brigade import grokbot_mcp, grokbot_ops
from . import connector_files as files
from .connector_files import PackError as PackError
from .profile import profile_for
from .runtime_config import load_runtime

_IDS = ("dot", "duet", "cloud-agent")
_BINDS = dict(zip(_IDS, ("127.0.0.1:8779", "127.0.0.1:8780", "127.0.0.1:8783"), strict=True))
INSTANCE_SCHEMA = "brigade.grokbot.connector-instance.v1"


def _profile(pack_id: str):
    if pack_id not in _IDS:
        raise PackError("unknown-pack")
    return profile_for(pack_id)


def instance_config_path(target: Path, pack_id: str) -> Path:
    _profile(pack_id)
    if pack_id == "cloud-agent":
        return target / ".brigade/connectors/cloud-agent/instance.json"
    return target / ".brigade/grokbot/packs" / f"{pack_id}.json"


def packaged_manifests() -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "schema": "brigade.grokbot.connector-pack.v1",
            "id": name,
            "version": "1.0.0",
            "kind": "connector",
            "instance": name,
            "default_bind": _BINDS[name],
            "public_route": "",
            "tools": sorted(_profile(name).tools),
        }
        for name in _IDS
    )


def list_packs(target: Path | None = None) -> list[dict[str, Any]]:
    packs = list(packaged_manifests())
    for pack in packs:
        path = instance_config_path(target or Path("."), pack["id"])
        port = files._bind_port_from_existing_config(path)
        pack["installed_bind"] = None
        if port is not None:
            pack["installed_bind"] = json.loads(grokbot_ops._read_regular_text(path))["bind"]
        pack["reachability"] = "host-local" if port is not None else None
    return packs


def show_pack(pack_id: str, target: Path | None = None) -> dict[str, Any]:
    _profile(pack_id)
    return next(p for p in list_packs(target) if p["id"] == pack_id)


def _validate(payload: Any, pack_id: str) -> dict[str, Any]:
    profile = _profile(pack_id)
    keys = {
        "schema",
        "pack_id",
        "pack_version",
        "bind",
        "allowed_hosts",
        "allowed_origins",
        "public_route",
        "runtime_path",
    }
    if not isinstance(payload, dict) or set(payload) - {"host_local"} != keys:
        raise PackError("unexpected-key")
    if payload["schema"] != INSTANCE_SCHEMA or payload["pack_id"] != pack_id:
        raise PackError("unexpected-key")
    files._parse_version(payload["pack_version"])
    host, _ = files._parse_default_bind(payload["bind"])
    hosts, origins = payload["allowed_hosts"], payload["allowed_origins"]
    if not isinstance(hosts, list) or not isinstance(origins, list):
        raise PackError("unexpected-key")
    if any(not grokbot_mcp._valid_host(v) for v in hosts) or any(not grokbot_mcp._valid_origin(v) for v in origins):
        raise PackError("unexpected-key")
    if (
        not isinstance(payload["public_route"], str)
        or not re.fullmatch(r"^$|^/[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)*$", payload["public_route"])
        or ("host_local" in payload and type(payload["host_local"]) is not bool)
    ):
        raise PackError("unexpected-key")
    try:
        path = Path(payload["runtime_path"])
        if not path.is_absolute():
            raise ValueError
        load_runtime(path, schema=profile.runtime_schema)
    except Exception:
        raise PackError("unsafe-path") from None
    if not grokbot_mcp._is_loopback(host) and (pack_id != "cloud-agent" or not hosts):
        raise PackError("unsafe-bind")
    return dict(payload)


def load_instance_config(target: Path, pack_id: str) -> dict[str, Any]:
    try:
        return _validate(json.loads(grokbot_ops._read_regular_text(instance_config_path(target, pack_id))), pack_id)
    except (OSError, ValueError, TypeError):
        raise PackError("unsafe-path") from None


def _write(target: Path, pack_id: str, payload: dict[str, Any]) -> None:
    path = instance_config_path(target, pack_id)
    files._assert_config_destination_safe(path)
    before = files._snapshot_regular_file(path)
    try:
        grokbot_ops._write_text_nofollow_atomic(path, json.dumps(payload, indent=2, sort_keys=True) + "\n", mode=0o600)
    except OSError:
        files._restore_regular_file(path, before)
        if not files._matches_regular_file_snapshot(path, before):
            raise PackError("rollback-failed") from None
        raise PackError("unsafe-path") from None


def _setup(
    target: Path,
    pack_id: str,
    *,
    apply: bool,
    bind: str | None,
    allowed_hosts: list[str] | None,
    allowed_origins: list[str] | None,
    runtime_path: str | Path | None,
) -> dict[str, Any]:
    _profile(pack_id)
    payload: dict[str, Any] = dict(
        schema=INSTANCE_SCHEMA,
        pack_id=pack_id,
        pack_version="1.0.0",
        bind=bind or _BINDS[pack_id],
        allowed_hosts=sorted(set(allowed_hosts or [])),
        allowed_origins=sorted(set(allowed_origins or [])),
        public_route="",
        runtime_path=str(runtime_path),
    )
    _validate(payload, pack_id)
    port = files._parse_default_bind(payload["bind"])[1]
    # Include old installed profiles and queue listeners without importing their registry.
    reserved = {8766, 8767, 8768, 8771, 8773, 8777, 8778, 8781, 8782}
    for other in _IDS:
        if other != pack_id:
            reserved.add(files._parse_default_bind(_BINDS[other])[1])
    paths = list((target / ".brigade/grokbot/packs").glob("*.json"))
    paths += [
        grokbot_ops.config_path(target, name) for name in ("operator", "repository-scout", "implementation-worker")
    ]
    paths += [instance_config_path(target, other) for other in _IDS if other != pack_id]
    for path in paths:
        if path == instance_config_path(target, pack_id):
            continue
        occupied = files._bind_port_from_existing_config(path)
        if occupied is not None:
            reserved.add(occupied)
    if port in reserved:
        raise PackError("duplicate-port")
    runtime = load_runtime(Path(payload["runtime_path"]), schema=_profile(pack_id).runtime_schema)
    from .lifecycle import _isolated_from_dot

    if not _isolated_from_dot(target, runtime):
        raise PackError("not-isolated")
    result = dict(
        action="setup",
        apply=apply,
        pack_id=pack_id,
        bind=payload["bind"],
        writes=[str(instance_config_path(Path("."), pack_id))],
    )
    if apply:
        _write(target, pack_id, payload)
    return result


def preview_setup(
    target: Path,
    pack_id: str,
    *,
    bind: str | None = None,
    allowed_hosts: list[str] | None = None,
    allowed_origins: list[str] | None = None,
    runtime_path: str | Path | None = None,
) -> dict[str, Any]:
    return _setup(
        target,
        pack_id,
        apply=False,
        bind=bind,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
        runtime_path=runtime_path,
    )


def apply_setup(
    target: Path,
    pack_id: str,
    *,
    bind: str | None = None,
    allowed_hosts: list[str] | None = None,
    allowed_origins: list[str] | None = None,
    runtime_path: str | Path | None = None,
) -> dict[str, Any]:
    return _setup(
        target,
        pack_id,
        apply=True,
        bind=bind,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
        runtime_path=runtime_path,
    )


def preview_update(target: Path, pack_id: str) -> dict[str, Any]:
    config = load_instance_config(target, pack_id)
    if files._parse_version(config["pack_version"]) != (1, 0, 0):
        raise PackError("incompatible-version")
    return dict(action="update", apply=False, pack_id=pack_id, from_version=config["pack_version"], to_version="1.0.0")


def apply_update(target: Path, pack_id: str) -> dict[str, Any]:
    return {**preview_update(target, pack_id), "apply": True}


def preview_remove(target: Path, pack_id: str, *, unit_dir: Path | None = None) -> dict[str, Any]:
    paths = [instance_config_path(target, pack_id)]
    if unit_dir is not None:
        if not files._owned_unit_dir(unit_dir, pack_id):
            raise PackError("outside-owned-path")
        paths.append(unit_dir / _profile(pack_id).unit_name)
    present = [p for p in paths if files._exists_or_link(p)]
    for path in present:
        files._assert_removable(path, config_file=path == paths[0])
    return dict(action="remove", apply=False, pack_id=pack_id, paths=[str(p) for p in present])


def apply_remove(target: Path, pack_id: str, *, unit_dir: Path | None = None) -> dict[str, Any]:
    result = preview_remove(target, pack_id, unit_dir=unit_dir)
    for path in result["paths"]:
        grokbot_ops.remove_regular_file(Path(path))
    return {**result, "apply": True}


def doctor(target: Path, pack_id: str, *, service_result: bool = False) -> list[dict[str, str]]:
    from . import lifecycle

    _profile(pack_id)
    checks = lifecycle.doctor(target, pack_id=pack_id)
    if service_result:
        checks.append(grokbot_ops.inspect_service_result(pack_id))
    return checks


def canary(target: Path, pack_id: str) -> dict[str, Any]:
    from . import lifecycle

    _profile(pack_id)
    return lifecycle.canary(target, pack_id=pack_id)


def render_install_service(target: Path, pack_id: str) -> str:
    from . import lifecycle

    _profile(pack_id)
    return lifecycle.render_unit(target, python=sys.executable, pack_id=pack_id, canonical=True)


def apply_install_service(target: Path, pack_id: str, *, out_dir: Path, force: bool = False) -> dict[str, Any]:
    from . import lifecycle

    _profile(pack_id)
    path = lifecycle._write_rendered(
        out_dir / _profile(pack_id).unit_name, render_install_service(target, pack_id), force=force
    )
    return dict(action="install-service", apply=True, pack_id=pack_id, paths=[str(path)])
