"""Inspect Brigade's built-in station catalog and discover external station.json files."""

from __future__ import annotations

import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import subprocess
import tempfile
import time
from typing import Any

from . import managed, profiles, registry, station_manifest
from .install import DEFAULT_WIRED_SKILLS


VERIFY_SCHEMA = "brigade.stations.verify.v1"
OUTPUT_LIMIT_BYTES = 64 * 1024
_DETAIL_LIMIT = 240
_SUPPORT_ARGUMENTS = frozenset({"--help", "-h", "--version", "version"})


def _json_print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _selection_for(station_name: str, profile: profiles.StationProfile) -> str:
    if station_name in profile.selected_stations:
        return "selected"
    if station_name in profile.optional_stations:
        return "optional"
    return "not selected"


def _surface_payload(surface: managed.MachineSurface) -> dict[str, Any]:
    return {
        "kind": surface.kind,
        "command": list(surface.command),
        "read_only": surface.read_only,
        "timeout_seconds": surface.timeout_seconds,
        "max_chars": surface.max_chars,
    }


def list_stations(*, profile_name: str = "repo", json_output: bool = False) -> int:
    profile = profiles.resolve(profile_name)
    if profile is None:
        print(f"unknown profile: {profile_name}")
        return 2

    rows: list[dict[str, Any]] = []
    for station in registry.all_stations():
        tools = []
        for tool in managed.for_station(station.name):
            tools.append(
                {
                    "name": tool.name,
                    "command": tool.command,
                    "summary": tool.summary,
                    "install_args": list(tool.install_args),
                    "surfaces": [_surface_payload(surface) for surface in tool.surfaces],
                }
            )
        rows.append(
            {
                "station": station.name,
                "selection": _selection_for(station.name, profile),
                "summary": station.summary,
                "aliases": list(station.aliases),
                "tools": tools,
                "built_in_skills": list(DEFAULT_WIRED_SKILLS) if station.name == "skills" else [],
            }
        )

    payload = {"profile": profile.name, "stations": rows}
    if json_output:
        _json_print(payload)
        return 0

    print(f"brigade stations: profile={profile.name}")
    width = max((len(row["station"]) for row in rows), default=8)
    for row in rows:
        tool_labels = [tool["name"] for tool in row["tools"]]
        tool_labels.extend(row["built_in_skills"])
        tool_names = ", ".join(tool_labels) or "built-in"
        print(f"  {row['station'].ljust(width)}  [{row['selection']}]  {tool_names}  - {row['summary']}")
        for tool in row["tools"]:
            surfaces = tool.get("surfaces") or []
            if not surfaces:
                continue
            labels = ", ".join(surface["kind"] for surface in surfaces)
            print(f"  {'':{width}}    surfaces: {tool['name']}: {labels}")
    return 0


def _default_discover_roots() -> list[Path]:
    home = Path.home()
    roots = [
        Path.cwd(),
        home / "repos",
        home / "src",
        home / "code",
    ]
    # De-dupe while preserving order; keep only existing dirs.
    seen: set[Path] = set()
    out: list[Path] = []
    for root in roots:
        try:
            resolved = root.expanduser().resolve()
        except OSError:
            continue
        if resolved in seen or not resolved.is_dir():
            continue
        seen.add(resolved)
        out.append(resolved)
    return out


def discover_payload(
    *,
    roots: list[Path] | None = None,
    max_depth: int = 3,
) -> dict[str, Any]:
    search_roots = roots or _default_discover_roots()
    found: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    skip_dir_names = {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        "dist",
        "build",
        ".tox",
        "target",
    }

    for root in search_roots:
        root = root.expanduser().resolve()
        if not root.is_dir():
            errors.append({"path": str(root), "error": "not a directory"})
            continue
        # Always check root/station.json
        candidates = [root / "station.json"]
        if max_depth >= 1:
            try:
                for child in sorted(root.iterdir()):
                    if not child.is_dir() or child.name in skip_dir_names or child.name.startswith("."):
                        continue
                    candidates.append(child / "station.json")
                    if max_depth >= 2:
                        try:
                            for grand in sorted(child.iterdir()):
                                if not grand.is_dir() or grand.name in skip_dir_names or grand.name.startswith("."):
                                    continue
                                candidates.append(grand / "station.json")
                        except OSError:
                            continue
            except OSError as exc:
                errors.append({"path": str(root), "error": str(exc)})
                continue

        seen_paths: set[Path] = set()
        for path in candidates:
            if path in seen_paths or not path.is_file():
                continue
            seen_paths.add(path)
            try:
                manifest = station_manifest.load(str(path))
            except ValueError as exc:
                errors.append({"path": str(path), "error": str(exc)})
                continue
            tools = []
            for tool in manifest.tools:
                tools.append(
                    {
                        "name": tool.name,
                        "kind": tool.kind,
                        "command": tool.command,
                        "summary": tool.summary,
                        "install": list(tool.install),
                        "surfaces": [
                            {
                                "kind": surface.kind,
                                "command": list(surface.command),
                                "read_only": surface.read_only,
                                "timeout_seconds": surface.timeout_seconds,
                                "max_chars": surface.max_chars,
                                "probe": list(surface.probe),
                                "probe_contains": list(surface.probe_contains),
                                "placeholders": list(surface.placeholders),
                            }
                            for surface in tool.surfaces
                        ],
                    }
                )
            found.append(
                {
                    "path": str(manifest.path),
                    "name": manifest.name,
                    "station": manifest.station,
                    "summary": manifest.summary,
                    "lifecycle": manifest.lifecycle,
                    "owner": manifest.owner,
                    "tools": tools,
                    "add_command": f"brigade add {manifest.path.parent}",
                }
            )

    lifecycle_counts = {lifecycle: 0 for lifecycle in station_manifest.LIFECYCLES}
    for manifest in found:
        lifecycle_counts[manifest["lifecycle"]] += 1

    return {
        "roots": [str(r) for r in search_roots],
        "max_depth": max_depth,
        "count": len(found),
        "active_count": lifecycle_counts["active"],
        "non_active_count": len(found) - lifecycle_counts["active"],
        "lifecycle_counts": lifecycle_counts,
        "manifests": found,
        "errors": errors,
        "docs": {
            "schema": station_manifest.SCHEMA,
            "add": "brigade add <path-to-dir-or-station.json> [--install]",
            "list": "brigade stations list",
        },
    }


def discover(
    *,
    roots: list[Path] | None = None,
    max_depth: int = 3,
    json_output: bool = False,
) -> int:
    payload = discover_payload(roots=roots, max_depth=max_depth)
    if json_output:
        _json_print(payload)
        return 0
    print(
        f"brigade stations discover: {payload['count']} station.json file(s) "
        f"({payload['active_count']} active, {payload['non_active_count']} non-active)"
    )
    for row in payload["manifests"]:
        tool_names = ", ".join(tool["name"] for tool in row["tools"]) or "(none)"
        print(f"  {row['name']}  station={row['station']}  lifecycle={row['lifecycle']}  tools={tool_names}")
        print(f"    path: {row['path']}")
        print(f"    next: {row['add_command']}")
    if payload["errors"]:
        print(f"errors: {len(payload['errors'])}")
        for err in payload["errors"][:10]:
            print(f"  {err['path']}: {err['error']}")
    if not payload["manifests"]:
        print("next: place a station.json (schema brigade.station.v1) in a sidecar repo, then re-run discover")
    return 0


def _detail(value: str) -> str:
    """Return a bounded single-line verifier detail without child output."""
    clean = " ".join(value.replace("\x00", "").split())
    if len(clean) <= _DETAIL_LIMIT:
        return clean
    return clean[: _DETAIL_LIMIT - 3] + "..."


def _isolated_environment(root: Path) -> dict[str, str]:
    env = os.environ.copy()
    values = {
        "HOME": root / "home",
        "XDG_CONFIG_HOME": root / "config",
        "XDG_CACHE_HOME": root / "cache",
        "XDG_DATA_HOME": root / "data",
    }
    if os.name == "nt":  # pragma: no cover - exercised on Windows CI only
        values.update(
            {
                "USERPROFILE": root / "home",
                "APPDATA": root / "config",
                "LOCALAPPDATA": root / "data",
            }
        )
    for name, path in values.items():
        path.mkdir(parents=True, exist_ok=True)
        env[name] = str(path)
    return env


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:  # pragma: no cover - exercised on Windows CI only
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    shell=False,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    check=False,
                )
            except OSError:
                process.kill()
    except ProcessLookupError:
        pass


def _run_bounded(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: float,
) -> tuple[dict[str, Any], bytes, bytes]:
    started = time.monotonic()
    popen_kwargs: dict[str, Any] = {
        "cwd": cwd,
        "env": env,
        "shell": False,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "bufsize": 0,
    }
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True
    elif hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):  # pragma: no cover
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

    try:
        process = subprocess.Popen(argv, **popen_kwargs)
    except OSError:
        result = {
            "exit_code": None,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "stdout_bytes": 0,
            "stderr_bytes": 0,
            "total_bytes": 0,
            "timed_out": False,
            "overflowed": False,
            "detail": "process could not be started",
        }
        return result, b"", b""

    assert process.stdout is not None
    assert process.stderr is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    chunks: dict[str, list[bytes]] = {"stdout": [], "stderr": []}
    byte_counts = {"stdout": 0, "stderr": 0}
    total = 0
    timed_out = False
    overflowed = False

    while selector.get_map():
        elapsed = time.monotonic() - started
        if elapsed >= timeout_seconds:
            timed_out = True
            _terminate_process_group(process)
        wait = 0 if timed_out or overflowed else min(0.05, max(0.0, timeout_seconds - elapsed))
        events = selector.select(wait)
        if not events and (timed_out or overflowed):
            break
        for key, _ in events:
            stream = key.fileobj
            try:
                chunk = os.read(stream.fileno(), 4096)
            except OSError:
                chunk = b""
            if not chunk:
                selector.unregister(stream)
                continue
            remaining = OUTPUT_LIMIT_BYTES - total
            accepted = chunk[:remaining]
            if accepted:
                chunks[key.data].append(accepted)
                size = len(accepted)
                byte_counts[key.data] += size
                total += size
            if len(chunk) > remaining:
                overflowed = True
                _terminate_process_group(process)
        if timed_out or overflowed:
            # One short drain pass lets closed pipes report EOF without waiting
            # on a descendant that ignored or outlived its parent.
            for key, _ in selector.select(0.05):
                try:
                    selector.unregister(key.fileobj)
                except KeyError:
                    pass
            break

    selector.close()
    process.stdout.close()
    process.stderr.close()
    try:
        exit_code = process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        _terminate_process_group(process)
        try:
            exit_code = process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:  # pragma: no cover - OS process failure
            exit_code = None

    duration_ms = round((time.monotonic() - started) * 1000)
    if timed_out:
        detail = f"probe exceeded {timeout_seconds:g}s timeout"
    elif overflowed:
        detail = f"combined output exceeded {OUTPUT_LIMIT_BYTES} byte limit"
    elif exit_code:
        detail = f"probe exited with status {exit_code}"
    else:
        detail = "probe exited successfully"
    result = {
        "exit_code": exit_code,
        "duration_ms": duration_ms,
        "stdout_bytes": byte_counts["stdout"],
        "stderr_bytes": byte_counts["stderr"],
        "total_bytes": total,
        "timed_out": timed_out,
        "overflowed": overflowed,
        "detail": detail,
    }
    return result, b"".join(chunks["stdout"]), b"".join(chunks["stderr"])


def _surface_base(surface: station_manifest.ManifestSurface) -> dict[str, Any]:
    return {
        "kind": surface.kind,
        "status": "failed",
        "execution": "none",
        "executed": False,
        "exit_code": None,
        "duration_ms": 0,
        "stdout_bytes": 0,
        "stderr_bytes": 0,
        "total_bytes": 0,
        "timed_out": False,
        "overflowed": False,
        "detail": "surface was not checked",
    }


def _surface_preflight(
    tool: station_manifest.ManifestTool,
    surface: station_manifest.ManifestSurface,
    *,
    manifest_dir: Path,
) -> tuple[str | None, tuple[str, ...], str]:
    if surface.timeout_seconds is None or surface.timeout_seconds <= 0:
        return "failed", (), "executed surfaces require a positive timeout_seconds"
    if surface.kind.startswith(("brief-", "summary-")) and (surface.max_chars is None or surface.max_chars <= 0):
        return "failed", (), "brief and summary surfaces require a positive max_chars"

    if surface.probe:
        argv = surface.probe
        execution = "probe"
        if any(station_manifest._PLACEHOLDER_RE.search(argument) for argument in argv):
            return "failed", (), "executed argv contains a placeholder"
        if tool.kind == "executable":
            if not argv or argv[0] != tool.command:
                return "failed", (), "executed argv does not match declared executable"
            if not any(argument in _SUPPORT_ARGUMENTS for argument in argv[1:]):
                return "failed", (), "probe is not a safe support command"
        else:
            if surface.kind != "verify-exit":
                return "failed", (), "skill-roster probes must use a verify-exit surface"
            local_targets = []
            for argument in argv[1:]:
                if argument.startswith("-"):
                    continue
                candidate = Path(argument)
                if not candidate.is_absolute():
                    candidate = manifest_dir / candidate
                try:
                    resolved = candidate.resolve()
                except OSError:
                    continue
                if resolved.is_relative_to(manifest_dir.resolve()) and resolved.is_file():
                    local_targets.append(resolved)
            if not local_targets:
                return "failed", (), "skill-roster probe requires a manifest-local file target"
        if not surface.probe_contains:
            return "failed", (), "probes require at least one bounded probe_contains assertion"
        return None, argv, execution

    if not surface.command or not surface.read_only or surface.placeholders:
        return "unverified", (), "surface has no safe executable command or probe"
    if tool.kind == "skill-roster":
        return "unverified", (), "skill-roster verification requires a manifest-local probe"
    if surface.command[0] != tool.command:
        return "failed", (), "executed argv does not match declared executable"
    return None, surface.command, "command"


def _resolve_argv(
    tool: station_manifest.ManifestTool,
    argv: tuple[str, ...],
    *,
    resolved_tool: str | None,
) -> tuple[list[str] | None, str | None]:
    if tool.kind == "executable":
        if not resolved_tool:
            return None, "declared executable is not available on PATH"
        return [resolved_tool, *argv[1:]], None
    executable = argv[0]
    if Path(executable).is_absolute():
        resolved = executable
    else:
        resolved = shutil.which(executable)
    if not resolved:
        return None, "probe executable is not available on PATH"
    return [str(Path(resolved).resolve()), *argv[1:]], None


def _verify_surface(
    tool: station_manifest.ManifestTool,
    surface: station_manifest.ManifestSurface,
    *,
    resolved_tool: str | None,
    manifest_dir: Path,
    env: dict[str, str],
) -> dict[str, Any]:
    payload = _surface_base(surface)
    preflight_status, argv, execution = _surface_preflight(tool, surface, manifest_dir=manifest_dir)
    if preflight_status:
        payload.update(status=preflight_status, detail=_detail(execution))
        return payload
    resolved_argv, error = _resolve_argv(tool, argv, resolved_tool=resolved_tool)
    if error:
        payload.update(status="failed", detail=error)
        return payload
    assert resolved_argv is not None
    assert surface.timeout_seconds is not None
    result, stdout, stderr = _run_bounded(
        resolved_argv,
        cwd=manifest_dir,
        env=env,
        timeout_seconds=surface.timeout_seconds,
    )
    payload.update(result)
    payload.update(execution=execution, executed=True)
    if result["timed_out"]:
        payload["status"] = "timeout"
        return payload
    if result["overflowed"]:
        payload["status"] = "overflow"
        return payload
    if result["exit_code"] != 0:
        payload["status"] = "failed"
        return payload

    combined_text = (stdout + stderr).decode("utf-8", errors="replace")
    if execution == "probe":
        missing = [expected for expected in surface.probe_contains if expected not in combined_text]
        if missing:
            payload.update(
                status="failed",
                detail=_detail(f"probe output did not contain {', '.join(missing)}"),
            )
            return payload
    elif surface.kind.endswith("-json"):
        try:
            json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload.update(status="invalid-json", detail="operational surface did not produce valid JSON")
            return payload
    elif surface.kind.endswith("-markdown") and not stdout.decode("utf-8", errors="replace").strip():
        payload.update(status="empty-markdown", detail="operational surface produced empty Markdown")
        return payload
    payload.update(status="passed", detail="conformance probe passed")
    return payload


def _managed_surface_contract(surface: managed.MachineSurface) -> dict[str, Any]:
    return {
        "kind": surface.kind,
        "command": list(surface.command),
        "read_only": surface.read_only,
        "timeout_seconds": surface.timeout_seconds,
        "max_chars": surface.max_chars,
        "probe": list(getattr(surface, "probe", ())),
        "probe_contains": list(getattr(surface, "probe_contains", ())),
    }


def _manifest_surface_contract(surface: station_manifest.ManifestSurface) -> dict[str, Any]:
    return {
        "kind": surface.kind,
        "command": list(surface.command),
        "read_only": surface.read_only,
        "timeout_seconds": surface.timeout_seconds,
        "max_chars": surface.max_chars,
        "probe": list(surface.probe),
        "probe_contains": list(surface.probe_contains),
    }


def _managed_parity(
    manifest: station_manifest.StationManifest,
    *,
    check_managed: bool,
) -> dict[str, Any]:
    drift: list[dict[str, Any]] = []
    exemptions: list[dict[str, str]] = []
    checked: list[str] = []
    for tool in manifest.tools:
        if tool.kind == "skill-roster":
            exemptions.append({"tool": tool.name, "reason": "skill-roster"})
            continue
        expected = managed.resolve(tool.name)
        if expected is None:
            exemptions.append({"tool": tool.name, "reason": "not-in-managed-catalog"})
            continue
        checked.append(tool.name)
        fields = {
            "station": (manifest.station, expected.station),
            "command": (tool.command, expected.command),
            "install": (list(tool.install), list(expected.install_args)),
            "surfaces": (
                [_manifest_surface_contract(surface) for surface in tool.surfaces],
                [_managed_surface_contract(surface) for surface in expected.surfaces],
            ),
        }
        different = sorted(field for field, pair in fields.items() if pair[0] != pair[1])
        if different:
            drift.append({"tool": tool.name, "fields": different})
    status = "drift" if drift else "matched"
    return {
        "status": status,
        "advisory": not check_managed,
        "checked_tools": checked,
        "drift": drift,
        "exemptions": exemptions,
    }


def verify_payload(ref: str, *, check_managed: bool = False) -> dict[str, Any]:
    """Verify one explicitly selected manifest without running install commands."""
    manifest = station_manifest.load(ref)
    lifecycle_counts = {lifecycle: 0 for lifecycle in station_manifest.LIFECYCLES}
    lifecycle_counts[manifest.lifecycle] = 1
    base: dict[str, Any] = {
        "schema": VERIFY_SCHEMA,
        "status": "failed",
        "ok": False,
        "check_managed": check_managed,
        "output_limit_bytes": OUTPUT_LIMIT_BYTES,
        "manifest": {
            "path": str(manifest.path),
            "name": manifest.name,
            "station": manifest.station,
            "lifecycle": manifest.lifecycle,
            "owner": manifest.owner,
        },
        "lifecycle_counts": lifecycle_counts,
        "tools": [],
    }
    if manifest.lifecycle != "active":
        base.update(
            status=f"{manifest.lifecycle}-skip",
            ok=True,
            managed_parity={
                "status": "skipped",
                "advisory": not check_managed,
                "checked_tools": [],
                "drift": [],
                "exemptions": [],
            },
        )
        return base

    with tempfile.TemporaryDirectory(prefix="brigade-station-verify-") as temp:
        env = _isolated_environment(Path(temp))
        tool_payloads: list[dict[str, Any]] = []
        for tool in manifest.tools:
            resolved_tool = shutil.which(tool.command) if tool.kind == "executable" else None
            if resolved_tool:
                resolved_tool = str(Path(resolved_tool).resolve())
            surfaces = [
                _verify_surface(
                    tool,
                    surface,
                    resolved_tool=resolved_tool,
                    manifest_dir=manifest.path.parent,
                    env=env,
                )
                for surface in tool.surfaces
            ]
            if tool.kind == "executable" and not resolved_tool:
                status = "unavailable"
            elif not surfaces:
                status = "failed"
            elif all(surface["status"] == "passed" for surface in surfaces):
                status = "passed"
            else:
                status = "failed"
            tool_payloads.append(
                {
                    "name": tool.name,
                    "kind": tool.kind,
                    "status": status,
                    "surface_count": len(surfaces),
                    "surfaces": surfaces,
                }
            )
    base["tools"] = tool_payloads
    parity = _managed_parity(manifest, check_managed=check_managed)
    base["managed_parity"] = parity
    conformance_ok = bool(tool_payloads) and all(tool["status"] == "passed" for tool in tool_payloads)
    parity_ok = parity["status"] != "drift" or not check_managed
    base["ok"] = conformance_ok and parity_ok
    base["status"] = "passed" if base["ok"] else "failed"
    return base


def verify(ref: str, *, json_output: bool = False, check_managed: bool = False) -> int:
    try:
        payload = verify_payload(ref, check_managed=check_managed)
    except ValueError as exc:
        error = {
            "schema": VERIFY_SCHEMA,
            "status": "error",
            "ok": False,
            "exit_code": 2,
            "detail": _detail(str(exc)),
        }
        if json_output:
            _json_print(error)
        else:
            print(f"brigade stations verify: error: {error['detail']}")
        return 2
    if json_output:
        _json_print(payload)
    else:
        print(
            f"brigade stations verify: {payload['status']} "
            f"name={payload['manifest']['name']} lifecycle={payload['manifest']['lifecycle']}"
        )
        for tool in payload["tools"]:
            print(f"  {tool['name']} [{tool['status']}]")
            for surface in tool["surfaces"]:
                print(f"    {surface['kind']} [{surface['status']}]: {surface['detail']}")
        parity = payload["managed_parity"]
        if parity["status"] == "drift":
            label = "gate" if check_managed else "advisory"
            print(f"  managed parity [{label}]: {len(parity['drift'])} tool(s) drifted")
    return 0 if payload["ok"] else 1
