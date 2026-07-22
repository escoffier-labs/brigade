"""Safe user-scoped Cursor onboarding for the Brigade work loop."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from . import __version__, component_bins

STATE_VERSION = 1
MANAGED_MCP_NAMES = ("brigade", "graphtrail", "miseledger")


def _home_dir() -> Path:
    return Path.home()


def _cursor_root() -> Path:
    return _home_dir().expanduser().resolve() / ".cursor"


def _digest_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _digest_value(value: object) -> str:
    rendered = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return _digest_text(rendered)


def _json_text(payload: object) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _coowned_json_text(payload: object) -> str:
    return json.dumps(payload, indent=2, sort_keys=False) + "\n"


def _mcp_servers() -> dict[str, dict[str, Any]]:
    # Engine servers resolve to the managed absolute path from `brigade setup`
    # so the generated config does not depend on Cursor's spawn-time PATH. The
    # bare name is kept only as a pre-setup fallback.
    def _absolute(resolved: str | None, fallback: str) -> str:
        return str(Path(resolved).expanduser().resolve()) if resolved else fallback

    return {
        "brigade": {
            "command": "brigade",
            "args": ["memory", "serve-mcp", "--stdio", "--target", "."],
            "timeout": 60,
        },
        "graphtrail": {"command": _absolute(component_bins.resolve("graphtrail-mcp"), "graphtrail-mcp")},
        "miseledger": {
            "command": _absolute(component_bins.resolve("miseledger"), "miseledger"),
            "args": ["mcp"],
            "timeout": 60,
        },
    }


def _plugin_manifest() -> str:
    return _json_text(
        {
            "name": "brigade-loop",
            "displayName": "Brigade Work Loop",
            "version": __version__,
            "description": "Applies the Brigade, GraphTrail, and MiseLedger evidence loop to Cursor agent workspaces.",
            "author": {"name": "Escoffier Labs"},
            "license": "MIT",
            "keywords": ["brigade", "graphtrail", "miseledger", "verification", "receipts"],
            "category": "developer-tools",
            "rules": "./rules/",
        }
    )


def _rule_text() -> str:
    return """---
alwaysApply: true
---

# Brigade work loop

In a Brigade-wired repository, invoke the global `brigade-work` skill before substantive work. Start with `brigade work brief --target .`. Run checks that should count through `brigade work verify run --target . --command "<test command>" --capture brigade-work`. Capture failures too, export new receipts to MiseLedger when it is installed, and finish durable work with a Memory Handoff.
"""


def _hook_text() -> str:
    context = (
        "BRIGADE WORK LOOP: In a Brigade-wired repository, invoke the global brigade-work skill before substantive "
        "work. Start with brigade work brief --target .. Run checks that should count through brigade work verify "
        "run with --capture brigade-work. Capture failures, export new receipts to MiseLedger when installed, and "
        "finish durable work with a Memory Handoff."
    )
    return (
        "#!/bin/sh\ncat >/dev/null\nprintf '%s\\n' "
        + _shell_single_quote(_json_text({"additional_context": context}).strip())
        + "\n"
    )


def _shell_single_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _desired_files(root: Path) -> dict[Path, tuple[str, bool, str]]:
    package = Path(__file__).parent / "templates" / "skills" / "brigade-work"
    plugin = root / "plugins" / "local" / "brigade-loop"
    skill = root / "skills" / "brigade-work"
    desired: dict[Path, tuple[str, bool, str]] = {
        plugin / ".cursor-plugin" / "plugin.json": (_plugin_manifest(), False, "plugin"),
        plugin / "rules" / "brigade-loop.mdc": (_rule_text(), False, "rule"),
        root / "hooks" / "brigade-session-start": (_hook_text(), True, "hook"),
        root / "brigade" / "mcp.json": (
            _json_text({"version": 1, "servers": _mcp_servers()}),
            False,
            "mcp-catalog",
        ),
    }
    for name in ("SKILL.md", "skill.json", "CHANGELOG.md"):
        desired[skill / name] = ((package / name).read_text(), False, "skill")
    return desired


<<<<<<< HEAD
=======
def cursor_generated_files(root: Path) -> dict[Path, tuple[str, bool, str]]:
    """Return only the Brigade-generated Cursor surfaces: plugin, rule, hook.

    The bundled ``brigade-work`` skill copy is intentionally excluded: the
    registry projection from the shared profile layer is its single owner, so
    migration retires the legacy copy rather than double-owning it. The
    Brigade-internal MCP catalog and the co-owned ``hooks.json``/``mcp.json``
    configs are owned by other stages and are not generated surfaces here.
    """
    desired = _desired_files(root)
    return {path: record for path, record in desired.items() if record[2] in {"plugin", "rule", "hook"}}


@dataclass(frozen=True)
class CursorV1Migration:
    """Result of migrating a Cursor ownership state from schema version 1 to 2.

    ``state`` is the schema-v2 ownership state with legacy file/hook ownership
    moved into ``generated`` and ``instructions``/``skills``/``mcp`` left empty
    for the shared profile stages to own. ``retire_mcp_names`` is the sorted
    tuple of legacy ``~/.cursor/mcp.json`` server names whose live value still
    digest-matches the recorded v1 entry, so the caller can delete exactly
    those before persisting v2 state. ``retire_file_paths`` is the sorted tuple
    of absolute ``Path`` objects for legacy ``_desired_files`` surfaces
    ``"skill"`` and ``"mcp-catalog"`` whose live bytes still digest-match the
    recorded v1 entry, so Task 7 can retire them instead of double-owning them
    under schema-v2 generated ownership. ``error`` is a stable, concise
    conflict reason; when it is set ``state`` is empty, ``retire_mcp_names``
    is empty, and ``retire_file_paths`` is empty. The migration is pure: it
    never writes state or mutates MCP config.
    """

    state: dict[str, Any]
    retire_mcp_names: tuple[str, ...]
    retire_file_paths: tuple[Path, ...]
    error: str | None
    retire_file_ownership: tuple[tuple[Path, str], ...] = ()
    retire_mcp_ownership: tuple[tuple[str, str], ...] = ()


def migrate_v1_state(*, root: Path, workspace: Path, state: dict[str, Any]) -> CursorV1Migration:
    """Migrate an exact Cursor ownership state version 1 into schema version 2.

    Accepts only ``state["version"] == 1`` with no ``schema_version`` and
    object-valued ``files``, ``hooks``, and ``mcp`` sections whose entries are
    string keys and string digests. Reads ``root / mcp.json`` via
    ``_read_json_object``; when legacy MCP ownership is nonempty, the live
    ``mcpServers`` must be an object and every recorded name must still exist
    with a value whose digest matches the stored one. Any deviation is a
    migration conflict: ``error`` is set and no state/config writes occur.
    """
    if state.get("version") != STATE_VERSION or "schema_version" in state:
        return CursorV1Migration({}, (), (), "legacy cursor state must be version 1 without schema_version")
    files = state.get("files")
    hooks = state.get("hooks")
    mcp = state.get("mcp")
    if not isinstance(files, dict) or not isinstance(hooks, dict) or not isinstance(mcp, dict):
        return CursorV1Migration({}, (), (), "legacy cursor state sections must be objects")
    for rel, digest in files.items():
        if not isinstance(rel, str) or not isinstance(digest, str):
            return CursorV1Migration({}, (), (), "legacy cursor file ownership is malformed")
    for name, digest in hooks.items():
        if not isinstance(name, str) or not isinstance(digest, str):
            return CursorV1Migration({}, (), (), "legacy cursor hook ownership is malformed")
    for name, digest in mcp.items():
        if not isinstance(name, str) or not isinstance(digest, str):
            return CursorV1Migration({}, (), (), "legacy cursor mcp ownership is malformed")

    live_doc, read_error = _read_json_object(root / "mcp.json")
    if live_doc is None:
        return CursorV1Migration({}, (), (), read_error or "cursor mcp config is unreadable")
    live_servers = live_doc.get("mcpServers")
    if mcp and not isinstance(live_servers, dict):
        return CursorV1Migration({}, (), (), "cursor mcp config mcpServers must be an object")

    retire_mcp: list[str] = []
    retire_mcp_own: list[tuple[str, str]] = []
    for name, stored in mcp.items():
        if not isinstance(live_servers, dict) or name not in live_servers:
            return CursorV1Migration({}, (), (), f"legacy cursor mcp entry is missing: {name}")
        if _digest_value(live_servers[name]) != stored:
            return CursorV1Migration({}, (), (), f"legacy cursor mcp entry was edited: {name}")
        retire_mcp.append(name)
        retire_mcp_own.append((name, stored))

    # Classify every legacy file entry against the exact current _desired_files
    # shape. plugin/rule/hook become schema-v2 generated ownership; skill and
    # mcp-catalog become retirement candidates for Task 7; any other key is a
    # conflict because "exact v1 shape" forbids guessing. The recorded file keys
    # must be exactly the set of relative keys _desired_files emits: a missing
    # expected key or an extra unexpected key is a conflict, checked before any
    # live read. Each entry must be a regular file under root with no symlink at
    # the leaf or in any intermediate component under root, and its live bytes
    # must digest-match the recorded digest. Retirement paths are the original
    # absolute leaf paths (never a symlink-resolved target) so Task 7 retires
    # exactly the recorded surface.
    root_resolved = root.resolve()
    desired = _desired_files(root)
    rel_to_surface = {_relative(root, path): surface for path, (_t, _e, surface) in desired.items()}
    expected_rels = set(rel_to_surface)
    recorded_rels = set(files)
    if recorded_rels != expected_rels:
        missing = sorted(expected_rels - recorded_rels)
        unexpected = sorted(recorded_rels - expected_rels)
        shape_parts: list[str] = []
        if missing:
            shape_parts.append("missing: " + ", ".join(missing))
        if unexpected:
            shape_parts.append("unexpected: " + ", ".join(unexpected))
        return CursorV1Migration({}, (), (), "legacy cursor file shape mismatch: " + "; ".join(shape_parts))
    generated_files: dict[str, str] = {}
    retire_paths: list[Path] = []
    retire_file_own: list[tuple[Path, str]] = []
    for rel, stored in files.items():
        if not _is_contained_rel(rel):
            return CursorV1Migration({}, (), (), f"legacy cursor file path escapes root: {rel}")
        surface = rel_to_surface[rel]
        raw_path = root / rel
        # Reject any symlink at the leaf or in an intermediate component under
        # root so a retirement path can never resolve to a different target.
        if raw_path.is_symlink():
            return CursorV1Migration({}, (), (), f"legacy cursor file path is a symlink: {rel}")
        intermediate = root
        for component in Path(rel).parts[:-1]:
            intermediate = intermediate / component
            if intermediate.is_symlink():
                return CursorV1Migration({}, (), (), f"legacy cursor file path crosses a symlink: {rel}")
        resolved = raw_path.resolve()
        if not resolved.is_relative_to(root_resolved) or not resolved.is_file():
            return CursorV1Migration({}, (), (), f"legacy cursor file is missing: {rel}")
        try:
            live_text = resolved.read_text()
        except (OSError, UnicodeError) as exc:
            return CursorV1Migration({}, (), (), f"legacy cursor file is unreadable: {rel}: {exc}")
        if _digest_text(live_text) != stored:
            return CursorV1Migration({}, (), (), f"legacy cursor file was edited: {rel}")
        if surface in {"plugin", "rule", "hook"}:
            generated_files[rel] = stored
        elif surface in {"skill", "mcp-catalog"}:
            # retire the original absolute leaf path, never a resolved target
            retire_paths.append(raw_path.absolute())
            retire_file_own.append((raw_path.absolute(), stored))
        else:  # defensive: rel_to_surface only holds known surfaces
            return CursorV1Migration({}, (), (), f"legacy cursor file surface is unexpected: {rel}")

    from . import harness_profile_cmd  # local import avoids a module-level cycle

    new_state = harness_profile_cmd.empty_profile_state(workspace=workspace, harness="cursor")
    new_state["generated"] = {
        "files": generated_files,
        "hooks": dict(hooks),
        "created_directories": [],
    }
    return CursorV1Migration(
        new_state,
        tuple(sorted(retire_mcp)),
        tuple(sorted(retire_paths)),
        None,
        tuple(sorted(retire_file_own)),
        tuple(retire_mcp_own),
    )


>>>>>>> ba8e21f (test(harness): cover user profile lifecycle)
def _state_path(root: Path) -> Path:
    return root / "brigade" / "install-state.json"


def _load_state(root: Path) -> dict[str, Any]:
    path = _state_path(root)
    empty: dict[str, Any] = {"version": STATE_VERSION, "files": {}, "hooks": {}, "mcp": {}}
    if not path.exists():
        return empty
    payload, error = _read_json_object(path)
    if payload is None:
        empty["_read_error"] = error or "ownership state is unreadable"
        return empty
    if payload.get("version") != STATE_VERSION:
        empty["_read_error"] = f"unsupported ownership state version: {payload.get('version')!r}"
        return empty
    for key in ("files", "hooks", "mcp"):
        if not isinstance(payload.get(key), dict):
            payload[key] = {}
    return payload


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _file_item(
    root: Path, path: Path, text: str, executable: bool, surface: str, state: dict[str, Any]
) -> dict[str, Any]:
    desired = _digest_text(text)
    stored = state["files"].get(_relative(root, path))
    if not path.exists():
        status, action = "missing", "create"
    elif not path.is_file():
        status, action = "conflict", "skip"
    else:
        try:
            live = _digest_text(path.read_text())
            mode_current = not executable or bool(path.stat().st_mode & 0o111)
        except (OSError, UnicodeError) as exc:
            return {
                "surface": surface,
                "path": str(path),
                "status": "conflict",
                "action": "skip",
                "detail": str(exc),
                "desired_fingerprint": desired,
            }
        if live == desired and mode_current:
            status, action = "current", "none"
        elif live == desired:
            status, action = "changed", "update"
        elif stored == live:
            status, action = "changed", "update"
        else:
            status, action = "conflict", "skip"
    return {
        "surface": surface,
        "path": str(path),
        "status": status,
        "action": action,
        "desired_fingerprint": desired,
    }


def _read_json_object(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not path.exists():
        return {}, None
    try:
        payload = json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, str(exc)
    if not isinstance(payload, dict):
        return None, "existing JSON configuration must be an object"
    return payload, None


def _hook_entry(root: Path) -> dict[str, str]:
    return {"command": str(root / "hooks" / "brigade-session-start")}


def _plan_hook(root: Path, state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None, str | None]:
    path = root / "hooks.json"
    doc, error = _read_json_object(path)
    item: dict[str, Any] = {"surface": "hook-config", "path": str(path), "name": "sessionStart"}
    if doc is None:
        item.update(status="conflict", action="skip", detail=error or "could not read hooks configuration")
        return item, None, error
    hooks = doc.get("hooks")
    if hooks is None:
        hooks = {}
    if not isinstance(hooks, dict):
        item.update(status="conflict", action="skip", detail="existing hooks field must be an object")
        return item, None, str(item["detail"])
    entries = hooks.get("sessionStart")
    if entries is None:
        entries = []
    if not isinstance(entries, list):
        item.update(status="conflict", action="skip", detail="existing sessionStart hooks must be a list")
        return item, None, str(item["detail"])
    desired = _hook_entry(root)
    desired_fp = _digest_value(desired)
    if desired in entries:
        item.update(status="current", action="none", desired_fingerprint=desired_fp)
    else:
        prior = state["hooks"].get("sessionStart")
        prior_index = next((index for index, entry in enumerate(entries) if _digest_value(entry) == prior), None)
        if prior_index is None:
            item.update(status="missing", action="create", desired_fingerprint=desired_fp)
        else:
            item.update(status="changed", action="update", desired_fingerprint=desired_fp, prior_index=prior_index)
    return item, doc, None


def _plan_mcp(root: Path, state: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any] | None, str | None]:
    path = root / "mcp.json"
    doc, error = _read_json_object(path)
    if doc is None:
        return (
            [{"surface": "mcp-config", "path": str(path), "status": "conflict", "action": "skip", "detail": error}],
            None,
            error,
        )
    servers = doc.get("mcpServers")
    if servers is None:
        servers = {}
    if not isinstance(servers, dict):
        detail = "existing mcpServers field must be an object"
        return (
            [{"surface": "mcp-config", "path": str(path), "status": "conflict", "action": "skip", "detail": detail}],
            None,
            detail,
        )
    items: list[dict[str, Any]] = []
    for name, desired in _mcp_servers().items():
        desired_fp = _digest_value(desired)
        live = servers.get(name)
        if live == desired:
            status, action = "current", "none"
        elif name not in servers:
            status, action = "missing", "create"
        elif state["mcp"].get(name) == _digest_value(live):
            status, action = "changed", "update"
        else:
            status, action = "conflict", "skip"
        items.append(
            {
                "surface": "mcp-config",
                "path": str(path),
                "name": name,
                "status": status,
                "action": action,
                "desired_fingerprint": desired_fp,
            }
        )
    return items, doc, None


def _public(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    private = {"desired_fingerprint", "prior_index"}
    return [{key: value for key, value in item.items() if key not in private} for item in items]


def _emit(payload: dict[str, Any], *, json_output: bool, rc: int) -> int:
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        action = payload.get("operation", "cursor user profile")
        print(f"cursor user profile: {action}")
        for item in payload.get("items", []):
            name = f" [{item['name']}]" if item.get("name") else ""
            print(f"- {item['status']}: {item['path']}{name}")
        if payload.get("reload_required"):
            print("next: reload Cursor windows")
    return rc


def install(*, write: bool = False, json_output: bool = False) -> int:
    """Legacy wrapper over the aggregate ``harness_profile_cmd.install`` for Cursor.

    Delegates with ``harness="cursor"`` and ``workspace=Path.cwd()`` so the
    shared instruction/skill/generated orchestration owns every surface. The
    aggregate payload (``results`` list, top-level ``reload_required``) is
    printed by the aggregate emitter; this wrapper only forwards the exit code.
    """
    from . import harness_profile_cmd

    return harness_profile_cmd.install(
        harness="cursor",
        workspace=Path.cwd(),
        write=write,
        allow_global_stdio=False,
        adopt=False,
        json_output=json_output,
        home=_home_dir(),
    )


def _remove_owned_leaf_dirs(root: Path) -> None:
    plugin = root / "plugins" / "local" / "brigade-loop"
    for current in (
        plugin / ".cursor-plugin",
        plugin / "rules",
        plugin,
        root / "skills" / "brigade-work",
        root / "brigade",
    ):
        if not current.is_dir():
            continue
        try:
            current.rmdir()
        except OSError:
            continue


def uninstall(*, write: bool = False, json_output: bool = False) -> int:
    """Legacy wrapper over the aggregate ``harness_profile_cmd.uninstall`` for Cursor."""
    from . import harness_profile_cmd

    return harness_profile_cmd.uninstall(
        harness="cursor",
        workspace=Path.cwd(),
        write=write,
        json_output=json_output,
        home=_home_dir(),
    )


def _check(check_id: str, ok: bool, detail: str) -> dict[str, str]:
    return {"id": check_id, "status": "OK" if ok else "FAIL", "detail": detail}


def doctor(*, json_output: bool = False) -> int:
    """Legacy wrapper over the aggregate ``harness_profile_cmd.doctor`` for Cursor."""
    from . import harness_profile_cmd

    return harness_profile_cmd.doctor(
        harness="cursor",
        workspace=Path.cwd(),
        verify_mcp=False,
        json_output=json_output,
        home=_home_dir(),
    )


def _read_text(path: Path) -> str:
    try:
        return path.read_text()
    except (OSError, UnicodeError):
        return ""


def _file_matches(path: Path, expected: str) -> bool:
    return path.is_file() and _read_text(path) == expected
