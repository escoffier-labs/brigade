"""Generic public capability projection. Live plugin IDs stay private."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Callable, Mapping, NoReturn

from .contracts import (
    ERROR_MESSAGES,
    PHASE1_ACTION_IDS,
    PHASE2_ONLY_ACTION_IDS,
    ObsidianError,
    parse_capabilities_result,
)

CAPABILITIES_FINGERPRINT_VERSION = 1


def _unavailable() -> NoReturn:
    raise ObsidianError("unavailable", ERROR_MESSAGES["unavailable"])


def canonicalize_fingerprint_input(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("version") != CAPABILITIES_FINGERPRINT_VERSION:
        _unavailable()
    plugins_raw = raw.get("plugins")
    commands_raw = raw.get("commands")
    if not isinstance(plugins_raw, list) or not isinstance(commands_raw, list):
        _unavailable()
    plugins: list[dict[str, Any]] = []
    seen_plugins: set[str] = set()
    for plugin in plugins_raw:
        if not isinstance(plugin, dict):
            _unavailable()
        plugin_id = plugin.get("id")
        version = plugin.get("version")
        action_ids = plugin.get("supported_action_ids")
        if not isinstance(plugin_id, str) or not plugin_id or not isinstance(version, str) or not version:
            _unavailable()
        if not isinstance(action_ids, list):
            _unavailable()
        parsed_ids = []
        seen: set[str] = set()
        for action_id in action_ids:
            if not isinstance(action_id, str) or not action_id:
                _unavailable()
            if action_id not in PHASE1_ACTION_IDS or action_id in PHASE2_ONLY_ACTION_IDS or action_id in seen:
                _unavailable()
            seen.add(action_id)
            parsed_ids.append(action_id)
        if plugin_id in seen_plugins:
            _unavailable()
        seen_plugins.add(plugin_id)
        plugins.append(
            {
                "id": plugin_id,
                "version": version,
                "supported_action_ids": sorted(parsed_ids),
            }
        )
    plugins.sort(key=lambda item: item["id"])
    commands = []
    seen_commands: set[str] = set()
    for command in commands_raw:
        if not isinstance(command, dict):
            _unavailable()
        command_id = command.get("id")
        name = command.get("name")
        if not isinstance(command_id, str) or not command_id or not isinstance(name, str) or not name:
            _unavailable()
        if command_id in seen_commands:
            _unavailable()
        seen_commands.add(command_id)
        commands.append({"id": command_id, "name": name})
    commands.sort(key=lambda item: (item["id"], item["name"]))
    return {"version": CAPABILITIES_FINGERPRINT_VERSION, "plugins": plugins, "commands": commands}


def hash_capabilities_fingerprint(raw: object) -> str:
    canonical = canonicalize_fingerprint_input(raw)
    encoded = json.dumps(
        {
            "version": canonical["version"],
            "plugins": canonical["plugins"],
            "commands": canonical["commands"],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def project_phase1(plugins: list[dict[str, Any]]) -> dict[str, Any]:
    sorted_plugins = sorted(
        (
            {
                "id": plugin["id"],
                "version": plugin["version"],
                "supported_action_ids": sorted(plugin["supported_action_ids"]),
            }
            for plugin in plugins
        ),
        key=lambda item: item["id"],
    )
    supported: set[str] = set()
    for plugin in sorted_plugins:
        supported.update(plugin["supported_action_ids"])
    return parse_capabilities_result(
        {
            "phase": "phase1",
            "search_backend": "native_bounded_search",
            "plugins": sorted_plugins,
            "supported_action_ids": sorted(supported),
        }
    )


def reconcile_capabilities(
    runtime: Mapping[str, Any],
    command_list: Callable[[], list[dict[str, str]]],
) -> dict[str, Any]:
    try:
        commands = command_list()
    except ObsidianError as exc:
        raise ObsidianError("unavailable", ERROR_MESSAGES["unavailable"]) from exc
    except Exception as exc:
        raise ObsidianError("unavailable", ERROR_MESSAGES["unavailable"]) from exc
    fingerprint = hash_capabilities_fingerprint(
        {
            "version": CAPABILITIES_FINGERPRINT_VERSION,
            "plugins": runtime["plugin_inventory"],
            "commands": commands,
        }
    )
    expected = runtime["command_fingerprint"]
    if not hmac.compare_digest(fingerprint.encode("utf-8"), expected.encode("utf-8")):
        _unavailable()
    canonical = canonicalize_fingerprint_input(
        {
            "version": CAPABILITIES_FINGERPRINT_VERSION,
            "plugins": runtime["plugin_inventory"],
            "commands": commands,
        }
    )
    return {"status": "ok", "capabilities": project_phase1(canonical["plugins"])}
