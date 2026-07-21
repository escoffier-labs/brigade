"""Crawler runtime selection and compatibility checks for evidence sources.

Brigade does not embed the crawler.  It discovers the crawler on PATH (or via
an explicit override), checks that the resolved binary is compatible, and only
then asks MiseLedger to crawl.  All destructive or archive-mutating crawler
subcommands are driven by MiseLedger; this module only runs the read-only
``version`` and ``doctor --json`` probes.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import proc


@dataclass(frozen=True)
class CrawlerDefaults:
    """Static contract for a source's crawler."""

    binary_name: str
    min_version: str
    required_capabilities: list[str]


@dataclass(frozen=True)
class CrawlerRuntime:
    """Resolved crawler identity, or a structured resolution error."""

    source: str
    binary_name: str
    resolved_path: str | None
    version: str | None
    capabilities: list[str]
    min_version: str
    required_capabilities: list[str]
    override: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class CompatResult:
    """Outcome of a non-mutating compatibility check."""

    state: str  # ok | warn | fail
    resolved_path: str | None
    version: str | None
    database: str | None
    config_path: str | None
    missing_capabilities: list[str]
    detail: str


# Source contracts encoded in code.  Optional .brigade/evidence.toml parsing may
# be added later, but defaults must work with no config.
_CRAWLER_DEFAULTS: dict[str, CrawlerDefaults] = {
    "discord": CrawlerDefaults(
        binary_name="discrawl",
        min_version="0.8.0",
        required_capabilities=["export"],
    ),
}

_CAPABILITY_CANDIDATES = ("version", "doctor", "export", "crawl")
_READ_ONLY_TIMEOUT = 30.0


def _env_override(source: str, env: dict[str, str]) -> str | None:
    """Return the explicit binary override for a source, if any.

    Precedence: ``<SOURCE>_CRAWLER_BIN``, then ``DISCRAWL_BIN`` for the
    Discord/Discrawl contract.
    """
    specific = env.get(f"{source.upper()}_CRAWLER_BIN")
    if specific:
        return specific
    if source == "discord":
        return env.get("DISCRAWL_BIN")
    return None


def _source_defaults(source: str) -> CrawlerDefaults | None:
    return _CRAWLER_DEFAULTS.get(source)


def known_sources() -> list[str]:
    """Return sources with an in-code crawler contract."""
    return list(_CRAWLER_DEFAULTS.keys())


def _resolve_path(spec: str, env: dict[str, str]) -> str | None:
    """Resolve an override spec to an absolute executable path."""

    expanded = Path(spec).expanduser()
    if expanded.is_file() and os.access(expanded, os.X_OK):
        return str(expanded.resolve())
    if expanded.is_file():
        return str(expanded.resolve())
    return shutil.which(spec, path=env.get("PATH"))


def _parse_version(value: str | None) -> tuple[int, ...] | None:
    if not value:
        return None
    parts: list[int] = []
    for part in value.split("."):
        try:
            parts.append(int(part))
        except ValueError:
            break
    return tuple(parts) if parts else None


def _probe_version(binary_path: str, env: dict[str, str]) -> str | None:
    result = proc.run([binary_path, "version"], env=env, timeout=_READ_ONLY_TIMEOUT)
    if result.code != 0:
        return None
    first = (result.stdout or "").strip().splitlines()
    return first[0] if first else None


def _probe_capabilities(binary_path: str, env: dict[str, str]) -> list[str]:
    result = proc.run([binary_path, "--help"], env=env, timeout=_READ_ONLY_TIMEOUT)
    if result.code != 0:
        return []
    text = result.stdout or ""
    found: list[str] = []
    for candidate in _CAPABILITY_CANDIDATES:
        if re.search(rf"\b{re.escape(candidate)}\b", text, re.IGNORECASE):
            found.append(candidate)
    return found


def resolve_crawler(source: str, env: dict[str, str] | None = None) -> CrawlerRuntime | None:
    """Resolve a crawler runtime for ``source``.

    Precedence: explicit override (``DISCRAWL_BIN`` / ``<SOURCE>_CRAWLER_BIN``),
    configured binary name, then PATH via ``shutil.which``.  Returns ``None``
    when the source has no known crawler contract and no override.  Resolution
    failures return a :class:`CrawlerRuntime` with ``error`` set and
    ``resolved_path`` set to ``None``.
    """

    if env is None:
        env = dict(os.environ)

    defaults = _source_defaults(source)
    override = _env_override(source, env)

    if defaults is None and override is None:
        return None

    if defaults is not None:
        binary_name = defaults.binary_name
        min_version = defaults.min_version
        required_capabilities = list(defaults.required_capabilities)
    else:
        assert override is not None
        binary_name = os.path.basename(override)
        min_version = "0.0.0"
        required_capabilities = []

    if override is not None:
        resolved_path = _resolve_path(override, env)
        override_name = override
    else:
        resolved_path = shutil.which(binary_name, path=env.get("PATH"))
        override_name = None

    if resolved_path is None:
        return CrawlerRuntime(
            source=source,
            binary_name=binary_name,
            resolved_path=None,
            version=None,
            capabilities=[],
            min_version=min_version,
            required_capabilities=required_capabilities,
            override=override_name,
            error=f"no executable found for {source}: tried {override or binary_name}",
        )

    version = _probe_version(resolved_path, env)
    capabilities = _probe_capabilities(resolved_path, env)
    return CrawlerRuntime(
        source=source,
        binary_name=binary_name,
        resolved_path=resolved_path,
        version=version,
        capabilities=capabilities,
        min_version=min_version,
        required_capabilities=required_capabilities,
        override=override_name,
    )


def check_compatibility(runtime: CrawlerRuntime, env: dict[str, str] | None = None) -> CompatResult:
    """Run a non-mutating compatibility check for a resolved crawler.

    This invokes only ``discrawl version`` (already cached in ``runtime``) and
    ``discrawl doctor --json``.  It never runs a mutating subcommand.

    Returns a :class:`CompatResult` with ``state`` in ``ok | warn | fail`` and
    ``detail`` containing expected-vs-observed signals on mismatch.
    """

    if env is None:
        env = dict(os.environ)

    base_fields: dict[str, Any] = {
        "resolved_path": runtime.resolved_path,
        "version": runtime.version,
        "database": None,
        "config_path": None,
    }

    if runtime.error or runtime.resolved_path is None:
        return CompatResult(
            state="fail",
            **base_fields,
            missing_capabilities=[],
            detail=runtime.error or "crawler not resolved",
        )

    missing_capabilities = [cap for cap in runtime.required_capabilities if cap not in runtime.capabilities]
    if missing_capabilities:
        return CompatResult(
            state="fail",
            **base_fields,
            missing_capabilities=missing_capabilities,
            detail=(
                f"missing required capabilities: {', '.join(missing_capabilities)}; observed {runtime.capabilities}"
            ),
        )

    observed_version = _parse_version(runtime.version)
    required_version = _parse_version(runtime.min_version)
    state = "ok"
    detail_parts: list[str] = []
    if runtime.version is None or observed_version is None:
        state = "fail"
        detail_parts.append(f"version not parseable: expected >= {runtime.min_version}, observed {runtime.version!r}")
    elif required_version is not None and observed_version < required_version:
        state = "fail"
        detail_parts.append(f"version below floor: expected >= {runtime.min_version}, observed {runtime.version}")
    elif runtime.version != runtime.min_version:
        state = "warn"
        detail_parts.append(
            f"version drift: expected {runtime.min_version}, observed {runtime.version}; archive readable"
        )

    result = proc.run(
        [runtime.resolved_path, "doctor", "--json"],
        env=env,
        timeout=_READ_ONLY_TIMEOUT,
    )
    exit_code = result.code
    data = result.json()
    database: str | None = None
    config_path: str | None = None
    if isinstance(data, dict):
        database = data.get("database")
        config_path = data.get("config_path")

    base_fields["database"] = database
    base_fields["config_path"] = config_path

    readable = database == "ok" and exit_code == 0
    if not readable:
        detail_parts.append(
            f"archive unreadable: expected database='ok' and exit_code=0, "
            f"observed database={database!r}, exit_code={exit_code}"
        )
        return CompatResult(
            state="fail",
            **base_fields,
            missing_capabilities=missing_capabilities,
            detail="; ".join(detail_parts),
        )

    if runtime.override is not None:
        default_path = shutil.which(runtime.binary_name, path=env.get("PATH"))
        if runtime.resolved_path != default_path:
            # Surface override drift, but never downgrade a version-floor / capability
            # failure to warn - an incompatible runtime must still be refused.
            if state != "fail":
                state = "warn"
            detail_parts.append(
                f"override binary {runtime.override} resolves to a different path than default {runtime.binary_name}"
            )

    return CompatResult(
        state=state,
        **base_fields,
        missing_capabilities=missing_capabilities,
        detail="; ".join(detail_parts) if detail_parts else "crawler compatible",
    )
