"""Resolve engine storage locations across the canonical and legacy layouts.

One rule for the code graph index and the evidence directory set
(docs/phase-engine-rename.md, "Storage locations"):

1. An explicit location wins: ``--db``, then the canonical environment
   variable, then the legacy one. Canonical and legacy variables set to
   different values are refused.
2. The canonical location wins when it exists.
3. The legacy location is used in place when it exists and the canonical one
   does not. Writes go there too. Nothing is copied or moved.
4. Otherwise the canonical location is new and will be created by the writer.

``legacy_present`` stays true whenever the legacy location exists, so doctor
can warn when both exist. The evidence database, MAC key, config and cache
resolve as one directory set, never one file from each layout.

The legacy names below are compatibility constants for the rename window and
live only in this module.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

Source = Literal["explicit", "canonical", "legacy", "new"]

CODE_GRAPH_DB = Path(".brigade") / "code-graph" / "code-graph.db"
CODE_GRAPH_DB_ENV = "BRIGADE_CODE_GRAPH_DB"
# Legacy compatibility constants (v0.31 and v0.32 only).
LEGACY_CODE_GRAPH_DB = Path(".graphtrail") / "graphtrail.db"
LEGACY_CODE_GRAPH_DB_ENV = "GRAPHTRAIL_DB"

EVIDENCE_LEAF = Path("brigade") / "evidence"
EVIDENCE_DB_NAME = "evidence.db"
# Legacy compatibility constants; the evidence read has no fixed end yet
# (docs/phase-engine-rename.md, open question 1).
LEGACY_EVIDENCE_LEAF = Path("miseledger")
LEGACY_EVIDENCE_DB_NAME = "miseledger.db"
EVIDENCE_MAC_KEY_NAME = "evidence-bundle-mac.key"
EVIDENCE_CONFIG_NAME = "config.toml"
CODE_GRAPH_ALLOW_UNSAFE_ROOT_ENV = "BRIGADE_CODE_GRAPH_ALLOW_UNSAFE_ROOT"
LEGACY_CODE_GRAPH_ALLOW_UNSAFE_ROOT_ENV = "GRAPHTRAIL_ALLOW_UNSAFE_ROOT"
LEGACY_REMOVAL_RELEASE = "v0.33.0"

# A doctor row: (status, name, detail, scope). Status and scope use the doctor
# module's string values; this module does not import doctor.
Finding = tuple[str, str, str, str]


@dataclass(frozen=True)
class ResolvedLocation:
    path: Path
    source: Source
    legacy_present: bool


@dataclass(frozen=True)
class EvidenceDirs:
    """One evidence directory set; every path comes from the same layout."""

    data_dir: Path
    db_path: Path
    mac_key_path: Path
    config_path: Path
    cache_dir: Path
    source: Source
    legacy_present: bool

    @property
    def location(self) -> ResolvedLocation:
        return ResolvedLocation(self.data_dir, self.source, self.legacy_present)


def code_graph_db_paths(target: Path) -> tuple[Path, Path]:
    """Return the ``(canonical, legacy)`` index paths for one repository."""
    return target / CODE_GRAPH_DB, target / LEGACY_CODE_GRAPH_DB


def _explicit_db(explicit: str | os.PathLike[str] | None, environment: Mapping[str, str]) -> str | None:
    if explicit is not None and str(explicit):
        return str(explicit)
    canonical = environment.get(CODE_GRAPH_DB_ENV) or None
    legacy = environment.get(LEGACY_CODE_GRAPH_DB_ENV) or None
    if canonical and legacy and canonical != legacy:
        raise ValueError(
            f"conflicting code graph database overrides: {CODE_GRAPH_DB_ENV} and {LEGACY_CODE_GRAPH_DB_ENV}"
        )
    return canonical or legacy


def code_graph_db(
    target: Path,
    *,
    explicit: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> ResolvedLocation:
    """Resolve the code graph index for ``target``.

    A relative explicit path is taken relative to ``target``. Raises
    ``ValueError`` when the canonical and legacy variables disagree.
    """
    environment = os.environ if env is None else env
    canonical, legacy = code_graph_db_paths(target)
    legacy_present = legacy.is_file()
    override = _explicit_db(explicit, environment)
    if override is not None:
        path = Path(override).expanduser()
        return ResolvedLocation(path if path.is_absolute() else target / path, "explicit", legacy_present)
    if canonical.is_file():
        return ResolvedLocation(canonical, "canonical", legacy_present)
    if legacy_present:
        return ResolvedLocation(legacy, "legacy", True)
    return ResolvedLocation(canonical, "new", False)


def code_graph_repo_root(db: Path) -> Path | None:
    """Repository root for an index path in either layout, or ``None`` for any other location.

    Mirrors ``repo_root_for_db`` in engines/code-graph/src/store/db.rs. The engine's
    ``sync`` indexes its working directory, so callers run it from this root.
    """
    graph_dir = db.parent
    if graph_dir.name == LEGACY_CODE_GRAPH_DB.parent.name:
        return graph_dir.parent
    if graph_dir.name == CODE_GRAPH_DB.parent.name and graph_dir.parent.name == CODE_GRAPH_DB.parent.parent.name:
        return graph_dir.parent.parent
    return None


def _root(environment: Mapping[str, str], variable: str, *fallback: str) -> Path:
    # Same roots and fallbacks as engines/evidence-ledger/internal/app/paths.go.
    value = environment.get(variable)
    if value:
        return Path(value)
    home = environment.get("HOME")
    if not home:
        raise ValueError(f"evidence paths require {variable} or HOME")
    return Path(home).joinpath(*fallback)


def _evidence_set(
    roots: tuple[Path, Path, Path], leaf: Path, db_name: str, source: Source, legacy: bool
) -> EvidenceDirs:
    data_root, config_root, cache_root = roots
    data_dir = data_root / leaf
    return EvidenceDirs(
        data_dir=data_dir,
        db_path=data_dir / db_name,
        mac_key_path=data_dir / EVIDENCE_MAC_KEY_NAME,
        config_path=config_root / leaf / EVIDENCE_CONFIG_NAME,
        cache_dir=cache_root / leaf,
        source=source,
        legacy_present=legacy,
    )


def _evidence_roots(environment: Mapping[str, str]) -> tuple[Path, Path, Path]:
    return (
        _root(environment, "XDG_DATA_HOME", ".local", "share"),
        _root(environment, "XDG_CONFIG_HOME", ".config"),
        _root(environment, "XDG_CACHE_HOME", ".cache"),
    )


def _evidence_layout_dirs(roots: tuple[Path, Path, Path], leaf: Path) -> list[Path]:
    """The data and config directories that make a layout exist."""
    data_root, config_root, _ = roots
    return [path for path in (data_root / leaf, config_root / leaf) if path.is_dir()]


def evidence_dirs(*, env: Mapping[str, str] | None = None) -> EvidenceDirs:
    """Resolve the evidence directory set.

    A layout exists when its data or config directory exists. The cache is
    disposable and never decides the layout.
    """
    environment = os.environ if env is None else env
    roots = _evidence_roots(environment)
    legacy_present = bool(_evidence_layout_dirs(roots, LEGACY_EVIDENCE_LEAF))
    if _evidence_layout_dirs(roots, EVIDENCE_LEAF):
        return _evidence_set(roots, EVIDENCE_LEAF, EVIDENCE_DB_NAME, "canonical", legacy_present)
    if legacy_present:
        return _evidence_set(roots, LEGACY_EVIDENCE_LEAF, LEGACY_EVIDENCE_DB_NAME, "legacy", True)
    return _evidence_set(roots, EVIDENCE_LEAF, EVIDENCE_DB_NAME, "new", False)


def _modified(path: Path) -> str:
    try:
        stamp = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return "modified time unknown"
    return f"modified {stamp.strftime('%Y-%m-%dT%H:%M:%SZ')}"


def _legacy_variables() -> Iterator[tuple[str, str, bool]]:
    """Yield ``(legacy, canonical, empty_is_unset)`` for every renamed variable.

    The engines treat an empty value as unset. ``component_bins`` treats a set
    but empty executable override as a value, so it can conflict.
    """
    from . import component_bins, component_manifest

    yield LEGACY_CODE_GRAPH_DB_ENV, CODE_GRAPH_DB_ENV, True
    yield LEGACY_CODE_GRAPH_ALLOW_UNSAFE_ROOT_ENV, CODE_GRAPH_ALLOW_UNSAFE_ROOT_ENV, True
    for legacy, canonical in component_manifest.COMPONENT_FAMILIES.items():
        yield component_bins.ENV_OVERRIDES[legacy], component_bins.ENV_OVERRIDES[canonical], False


def legacy_findings(target: Path, *, env: Mapping[str, str] | None = None) -> list[Finding]:
    """Doctor rows for legacy engine names still in effect.

    Both layouts present is a WARN with both paths and their modification
    times; the canonical one is used and nothing is merged or deleted. A legacy
    environment variable is INFO in v0.31. Conflicting old and new values are a
    WARN because every reader refuses them. Nothing here prints to stderr: the
    deprecation lines for legacy names land in R5 (docs/phase-engine-rename.md).
    """
    environment = os.environ if env is None else env
    findings: list[Finding] = []
    canonical, legacy = code_graph_db_paths(target)
    if canonical.is_file() and legacy.is_file():
        findings.append(
            (
                "WARN",
                "code graph index layout",
                f"both {canonical} ({_modified(canonical)}) and {legacy} ({_modified(legacy)}) exist; "
                "the canonical index is used and nothing is merged or deleted",
                "target",
            )
        )
    try:
        roots = _evidence_roots(environment)
    except ValueError:
        roots = None
    if roots is not None:
        current = _evidence_layout_dirs(roots, EVIDENCE_LEAF)
        retired = _evidence_layout_dirs(roots, LEGACY_EVIDENCE_LEAF)
        if current and retired:
            listed = ", ".join(f"{path} ({_modified(path)})" for path in (*current, *retired))
            findings.append(
                (
                    "WARN",
                    "evidence directory layout",
                    f"canonical and legacy evidence directories both exist: {listed}; "
                    "the canonical set is used and nothing is merged or deleted",
                    "operator",
                )
            )
    for old, new, empty_is_unset in _legacy_variables():
        old_value = environment.get(old)
        if old_value is None or (empty_is_unset and not old_value):
            continue
        new_value = environment.get(new)
        if empty_is_unset and not new_value:
            new_value = None
        if new_value is not None and new_value != old_value:
            findings.append(
                (
                    "WARN",
                    "legacy environment variable",
                    f"{old} and {new} differ and are refused; unset {old}",
                    "operator",
                )
            )
        else:
            findings.append(
                (
                    "INFO",
                    "legacy environment variable",
                    f"{old} is set; use {new} ({old} is removed in {LEGACY_REMOVAL_RELEASE})",
                    "operator",
                )
            )
    return findings
