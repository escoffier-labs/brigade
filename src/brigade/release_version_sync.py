"""brigade release version-sync: reconcile in-tree version stamps against one source."""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from . import toml_compat

MANIFEST_REL = Path(".brigade") / "version-sync.toml"


@dataclass(frozen=True)
class Source:
    file: str
    key: str | None
    regex: str | None


@dataclass(frozen=True)
class Location:
    path: str | None
    glob: str | None
    pattern: str
    guard: str | None
    required: bool


@dataclass(frozen=True)
class Manifest:
    source: Source
    locations: tuple[Location, ...]


@dataclass(frozen=True)
class LocationResult:
    path: str
    status: str  # "ok" | "mismatch" | "missing"
    found: tuple[str, ...]


def _as_str(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _require_one_group(pattern: str, field: str) -> None:
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"{field} is not a valid regex: {exc}") from exc
    if compiled.groups != 1:
        raise ValueError(f"{field} must have exactly one capture group, found {compiled.groups}")


def _parse_location(item: object, index: int) -> Location:
    where = f"location[{index}]"
    if not isinstance(item, dict):
        raise ValueError(f"{where} must be a table")
    path = item.get("path")
    glob = item.get("glob")
    if (path is None) == (glob is None):
        raise ValueError(f"{where} must set exactly one of `path` or `glob`")
    pattern = _as_str(item.get("pattern"), f"{where}.pattern")
    _require_one_group(pattern, f"{where}.pattern")
    guard = item.get("guard")
    if guard is not None:
        guard = _as_str(guard, f"{where}.guard")
    if path is not None and guard is not None:
        raise ValueError(f"{where}.guard is only valid with `glob`")
    required = item.get("required", True)
    if not isinstance(required, bool):
        raise ValueError(f"{where}.required must be a boolean")
    return Location(
        path=_as_str(path, f"{where}.path") if path is not None else None,
        glob=_as_str(glob, f"{where}.glob") if glob is not None else None,
        pattern=pattern,
        guard=guard,
        required=required,
    )


def load_manifest(target: Path) -> Manifest:
    manifest_path = target / MANIFEST_REL
    if not manifest_path.is_file():
        raise FileNotFoundError(manifest_path)
    data = toml_compat.loads(manifest_path.read_text())
    src = data.get("source")
    if not isinstance(src, dict):
        raise ValueError("[source] table is required")
    file = _as_str(src.get("file"), "source.file")
    key = src.get("key")
    regex = src.get("regex")
    if (key is None) == (regex is None):
        raise ValueError("source must set exactly one of `key` or `regex`")
    if key is not None:
        key = _as_str(key, "source.key")
    if regex is not None:
        regex = _as_str(regex, "source.regex")
        _require_one_group(regex, "source.regex")
    raw = data.get("location")
    if not isinstance(raw, list) or not raw:
        raise ValueError("at least one [[location]] is required")
    locations = tuple(_parse_location(item, i) for i, item in enumerate(raw))
    return Manifest(source=Source(file, key, regex), locations=locations)


def resolve_source(manifest: Manifest, target: Path) -> str:
    src_path = target / manifest.source.file
    if not src_path.is_file():
        raise ValueError(f"source file not found: {manifest.source.file}")
    text = src_path.read_text()
    if manifest.source.regex is not None:
        match = re.search(manifest.source.regex, text)
        if not match:
            raise ValueError(f"source regex found no version in {manifest.source.file}")
        return match.group(1)
    data = toml_compat.loads(text) if src_path.suffix == ".toml" else json.loads(text)
    value: object = data
    for part in manifest.source.key.split("."):
        if not isinstance(value, dict) or part not in value:
            raise ValueError(f"source key `{manifest.source.key}` not found in {manifest.source.file}")
        value = value[part]
    if not isinstance(value, str):
        raise ValueError(f"source key `{manifest.source.key}` is not a string")
    return value


def _resolve_files(location: Location, target: Path) -> list[Path]:
    if location.path is not None:
        return [target / location.path]
    return sorted(target.glob(location.glob))


def scan(manifest: Manifest, target: Path, expected: str) -> list[LocationResult]:
    results: list[LocationResult] = []
    for location in manifest.locations:
        for file in _resolve_files(location, target):
            rel = file.relative_to(target).as_posix()
            if not file.is_file():
                if location.path is not None:
                    results.append(LocationResult(rel, "missing", ()))
                continue
            text = file.read_text()
            if location.guard is not None and location.guard not in text:
                continue
            found = tuple(re.findall(location.pattern, text))
            if not found:
                if location.required:
                    results.append(LocationResult(rel, "missing", ()))
                continue
            bad = any(v != expected for v in found)
            results.append(LocationResult(rel, "mismatch" if bad else "ok", found))
    return results


def _rewrite(text: str, pattern: str, expected: str) -> str:
    return re.sub(pattern, lambda m: m.group(0).replace(m.group(1), expected), text)


def apply(manifest: Manifest, target: Path, expected: str) -> list[str]:
    changed: list[str] = []
    source_path = (target / manifest.source.file).resolve()
    for location in manifest.locations:
        for file in _resolve_files(location, target):
            if not file.is_file():
                continue
            if file.resolve() == source_path:
                continue
            text = file.read_text()
            if location.guard is not None and location.guard not in text:
                continue
            new_text = _rewrite(text, location.pattern, expected)
            if new_text != text:
                file.write_text(new_text)
                changed.append(file.relative_to(target).as_posix())
    return changed


def version_sync(*, target: Path, write: bool = False, json_output: bool = False) -> int:
    try:
        manifest = load_manifest(target)
        expected = resolve_source(manifest, target)
    except FileNotFoundError as exc:
        print(f"error: no version-sync manifest: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"error: invalid version-sync manifest: {exc}", file=sys.stderr)
        return 2

    if write:
        changed = apply(manifest, target, expected)
        if json_output:
            print(json.dumps({"version": expected, "changed": changed}, indent=2, sort_keys=True))
        elif changed:
            print(f"version={expected} rewrote {len(changed)} file(s):")
            for rel in changed:
                print(f"  {rel}")
        else:
            print(f"version={expected} already in sync")
        return 0

    results = scan(manifest, target, expected)
    problems = [r for r in results if r.status != "ok"]
    if json_output:
        payload = {
            "version": expected,
            "source": manifest.source.file,
            "checked": len(results),
            "results": [{"path": r.path, "status": r.status, "found": list(r.found)} for r in results],
            "ok": not problems,
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 1 if problems else 0

    print(f"version={expected} source={manifest.source.file} checked={len(results)} locations")
    github = bool(os.environ.get("GITHUB_ACTIONS"))
    for result in problems:
        if result.status == "mismatch":
            declared = ", ".join(sorted(set(result.found)))
            msg = f"{result.path} declares {declared}, {manifest.source.file} says {expected}"
        else:
            msg = f"{result.path}: version token not found (pattern drifted or asset not re-rendered)"
        print(f"{'::error::' if github else 'FAIL '}{msg}", file=sys.stderr)
    return 1 if problems else 0
