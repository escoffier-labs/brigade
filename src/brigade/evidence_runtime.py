"""Crawler runtime selection and compatibility checks for evidence sources.

Brigade does not embed the crawler.  It discovers the crawler on PATH (or via
an explicit override), checks that the resolved binary is compatible, and only
then asks MiseLedger to crawl.  All destructive or archive-mutating crawler
subcommands are driven by MiseLedger; this module only runs the read-only
``version`` and ``doctor --json`` probes.

Memory projection (#844 F1) probes the configured MiseLedger engine itself for
the authoritative ``memory-projection.v1`` capability before any crawl
delegation. Discord/Discrawl keeps the external-crawler contract below.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import component_bins, proc


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


@dataclass(frozen=True)
class MemoryProjectionProbe:
    """Read-only MiseLedger probe for the memory projection capability gate."""

    resolved_path: str | None
    version: str | None
    capability: str | None
    engine_version: str | None
    archive_ok: bool
    probe_exit_code: int | None
    missing_capabilities: list[str]
    detail: str
    error: str | None = None


# Source contracts encoded in code.  Optional .brigade/evidence.toml parsing may
# be added later, but defaults must work with no config.
_CRAWLER_DEFAULTS: dict[str, CrawlerDefaults] = {
    "discord": CrawlerDefaults(
        binary_name="discrawl",
        min_version="0.8.0",
        required_capabilities=["export"],
    ),
}

# Native engine source (not an external crawler). Kept out of _CRAWLER_DEFAULTS
# so Discord PATH/--help probing stays unchanged.
MEMORY_SOURCE = "memory"
MEMORY_PROJECTION_CAPABILITY = "memory-projection.v1"
# Documented diagnostic floor: in-tree engine Version and current managed pin
# are 0.6.0. Capability memory-projection.v1 is authoritative; a version floor
# alone cannot distinguish the published pre-feature 0.6.0 release. Do not
# invent a newer release pin here — that decision is a separate package.
MISELEDGER_VERSION_FLOOR = "0.6.0"

MEMORY_HEALTH_SAFE_KEYS = (
    "capability",
    "engine_version",
    "memory_namespace",
    "last_completed_scan_id",
    "last_completed_at",
    "canonical_count",
    "live_count",
    "hash_divergence",
    "unresolved_relations",
    "malformed_skipped",
    "stale",
    "partial",
    "status",
    "failed",
)

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


def register_memory_facade_extensions(_evidence_sub: Any = None) -> None:
    """Inert F2 registration point for rebuild routing and identity audit.

    F1 leaves this as a no-op so F2 can attach projection-only rebuild and
    read-only identity-audit commands without reshaping the crawl gate.
    """

    return None


def resolve_miseledger(env: dict[str, str] | None = None) -> str | None:
    """Resolve the configured MiseLedger engine binary, or None when missing."""

    return component_bins.resolve("miseledger", env=env)


def _normalize_miseledger_version(raw: str | None) -> str | None:
    if not raw:
        return None
    text = raw.strip()
    if not text:
        return None
    if text.lower().startswith("miseledger "):
        text = text.split(None, 1)[1].strip()
    return text or None


def _probe_miseledger_version(binary_path: str, env: dict[str, str]) -> str | None:
    result = proc.run([binary_path, "version"], env=env, timeout=_READ_ONLY_TIMEOUT)
    if result.code != 0:
        return None
    first = (result.stdout or "").strip().splitlines()
    return _normalize_miseledger_version(first[0] if first else None)


def _extract_memory_capability(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return (capability, engine_version) from doctor/status JSON."""

    capability_block = payload.get("capability")
    if not isinstance(capability_block, dict):
        return None, None
    raw_cap = capability_block.get("memory")
    capability = raw_cap if isinstance(raw_cap, str) else None
    raw_version = capability_block.get("engine_version")
    engine_version = raw_version if isinstance(raw_version, str) else None
    top_version = payload.get("engine_version")
    if engine_version is None and isinstance(top_version, str):
        engine_version = top_version
    return capability, engine_version


def _archive_readable_from_doctor(payload: dict[str, Any], exit_code: int) -> tuple[bool, str | None]:
    """Interpret MiseLedger doctor JSON for archive readability.

    Preflight accepts a readable archive even when memory health is unhealthy
    (so a stale projection can be re-crawled). A failed database check or a
    non-JSON probe is refused before delegation.
    """

    checks = payload.get("checks")
    if isinstance(checks, list):
        for row in checks:
            if not isinstance(row, dict):
                continue
            if row.get("name") == "database" and row.get("ok") is False:
                return False, str(row.get("detail") or "database check failed")
            if row.get("name") == "paths" and row.get("ok") is False:
                return False, str(row.get("detail") or "paths check failed")
    if exit_code not in (0, 1):
        return False, f"doctor exit {exit_code}"
    # exit 1 with structured checks is allowed when the archive opened (memory
    # projection health may fail while capability is still readable).
    return True, None


def probe_memory_projection(
    binary_path: str | None,
    env: dict[str, str] | None = None,
) -> MemoryProjectionProbe:
    """Read-only probe of MiseLedger for ``memory-projection.v1``.

    Invokes only ``version`` and ``doctor --json``. Never runs crawl/import.
    """

    if env is None:
        env = dict(os.environ)

    if not binary_path:
        return MemoryProjectionProbe(
            resolved_path=None,
            version=None,
            capability=None,
            engine_version=None,
            archive_ok=False,
            probe_exit_code=None,
            missing_capabilities=[MEMORY_PROJECTION_CAPABILITY],
            detail="miseledger binary not found; run `brigade setup`",
            error="missing binary",
        )

    version = _probe_miseledger_version(binary_path, env)
    result = proc.run([binary_path, "doctor", "--json"], env=env, timeout=_READ_ONLY_TIMEOUT)
    data = result.json()
    if not isinstance(data, dict):
        return MemoryProjectionProbe(
            resolved_path=binary_path,
            version=version,
            capability=None,
            engine_version=None,
            archive_ok=False,
            probe_exit_code=result.code,
            missing_capabilities=[MEMORY_PROJECTION_CAPABILITY],
            detail=(
                f"malformed miseledger doctor probe: expected JSON object, exit_code={result.code}, version={version!r}"
            ),
            error="malformed probe",
        )

    capability, engine_version = _extract_memory_capability(data)
    if engine_version is None:
        engine_version = version
    archive_ok, archive_detail = _archive_readable_from_doctor(data, result.code)
    missing: list[str] = []
    if capability != MEMORY_PROJECTION_CAPABILITY:
        missing.append(MEMORY_PROJECTION_CAPABILITY)

    detail_parts: list[str] = []
    if not archive_ok:
        detail_parts.append(f"archive unreadable: {archive_detail or 'doctor probe failed'}")
    if capability is None:
        detail_parts.append(
            f"absent capability: expected {MEMORY_PROJECTION_CAPABILITY}, observed capability.memory=None"
        )
    elif capability != MEMORY_PROJECTION_CAPABILITY:
        detail_parts.append(
            f"incompatible capability: expected {MEMORY_PROJECTION_CAPABILITY}, observed {capability!r}"
        )
    if not detail_parts:
        detail_parts.append(f"memory projection probe ok: capability={capability}, engine_version={engine_version}")

    return MemoryProjectionProbe(
        resolved_path=binary_path,
        version=version,
        capability=capability,
        engine_version=engine_version,
        archive_ok=archive_ok,
        probe_exit_code=result.code,
        missing_capabilities=missing,
        detail="; ".join(detail_parts),
        error=None if archive_ok and not missing else "incompatible",
    )


def check_memory_projection_preflight(probe: MemoryProjectionProbe) -> CompatResult:
    """Gate crawl/import mutation on the memory projection probe.

    Failures (missing binary, failed/malformed probe, absent/old/future
    capability, below-floor version, unreadable archive) must refuse
    delegation. Version floor is the documented ``MISELEDGER_VERSION_FLOOR``;
    capability match remains authoritative.
    """

    base = CompatResult(
        state="fail",
        resolved_path=probe.resolved_path,
        version=probe.engine_version or probe.version,
        database="ok" if probe.archive_ok else "unreadable",
        config_path=None,
        missing_capabilities=list(probe.missing_capabilities),
        detail=probe.detail,
    )
    if probe.resolved_path is None:
        return CompatResult(
            state="fail",
            resolved_path=None,
            version=None,
            database=None,
            config_path=None,
            missing_capabilities=[MEMORY_PROJECTION_CAPABILITY],
            detail=probe.detail or "miseledger binary not found",
        )
    if probe.error == "malformed probe" or (probe.probe_exit_code not in (0, 1) and not probe.archive_ok):
        return base
    if not probe.archive_ok:
        return base
    if probe.missing_capabilities or probe.capability != MEMORY_PROJECTION_CAPABILITY:
        return base

    observed_raw = probe.engine_version or probe.version
    observed = _parse_version(observed_raw)
    required = _parse_version(MISELEDGER_VERSION_FLOOR)
    detail_parts: list[str] = []
    state = "ok"
    if observed_raw is None or observed is None:
        return CompatResult(
            state="fail",
            resolved_path=probe.resolved_path,
            version=observed_raw,
            database="ok",
            config_path=None,
            missing_capabilities=[],
            detail=(f"version not parseable: expected >= {MISELEDGER_VERSION_FLOOR}, observed {observed_raw!r}"),
        )
    if required is not None and observed < required:
        return CompatResult(
            state="fail",
            resolved_path=probe.resolved_path,
            version=observed_raw,
            database="ok",
            config_path=None,
            missing_capabilities=[],
            detail=(f"version below floor: expected >= {MISELEDGER_VERSION_FLOOR}, observed {observed_raw}"),
        )
    if observed_raw != MISELEDGER_VERSION_FLOOR:
        state = "warn"
        detail_parts.append(
            f"version drift: expected {MISELEDGER_VERSION_FLOOR}, observed {observed_raw}; "
            f"capability={MEMORY_PROJECTION_CAPABILITY}"
        )
    else:
        detail_parts.append(
            f"memory projection compatible: capability={MEMORY_PROJECTION_CAPABILITY}, engine_version={observed_raw}"
        )
    return CompatResult(
        state=state,
        resolved_path=probe.resolved_path,
        version=observed_raw,
        database="ok",
        config_path=None,
        missing_capabilities=[],
        detail="; ".join(detail_parts),
    )


def body_free_memory_health(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return doctor/status memory health fields without card bodies/raw records."""

    if not isinstance(raw, dict):
        return None
    out: dict[str, Any] = {}
    for key in MEMORY_HEALTH_SAFE_KEYS:
        if key in raw:
            out[key] = raw[key]
    return out or None


def memory_projection_is_healthy(
    *,
    capability: str | None,
    status: str | None,
    stale: bool | None,
    partial: bool | None,
    failed: int | None,
    engine_ok: bool,
) -> bool:
    """Healthy requires completed + not stale/partial + failed=0 + capability + engine ok."""

    if not engine_ok:
        return False
    if capability != MEMORY_PROJECTION_CAPABILITY:
        return False
    if status != "completed":
        return False
    if stale is not False:
        return False
    if partial is not False:
        return False
    if failed != 0:
        return False
    return True


def classify_memory_crawl_status(
    *,
    exit_code: int,
    receipt: dict[str, Any] | None,
) -> str:
    """Map engine crawl outcome to last-run status: ok | partial | fail."""

    if exit_code != 0:
        return "fail"
    if not isinstance(receipt, dict):
        return "fail"
    capability = receipt.get("capability")
    status = receipt.get("status")
    stale = bool(receipt.get("stale"))
    partial = bool(receipt.get("partial"))
    failed = int(receipt.get("failed") or 0)
    if memory_projection_is_healthy(
        capability=capability if isinstance(capability, str) else None,
        status=status if isinstance(status, str) else None,
        stale=stale,
        partial=partial,
        failed=failed,
        engine_ok=True,
    ):
        return "ok"
    if status == "completed" and (failed > 0 or partial or stale):
        return "partial"
    return "fail"


def extract_memory_crawl_counts(receipt: dict[str, Any]) -> dict[str, Any]:
    """Pass through scan_id and the six result counts from an engine receipt."""

    def _int(key: str) -> int:
        value = receipt.get(key)
        return int(value) if isinstance(value, int) else 0

    return {
        "scan_id": receipt.get("scan_id") if isinstance(receipt.get("scan_id"), str) else None,
        "created": _int("created"),
        "updated": _int("updated"),
        "unchanged": _int("unchanged"),
        "removed": _int("removed"),
        "skipped": _int("skipped"),
        "failed": _int("failed"),
    }
