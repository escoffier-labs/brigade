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

# Public doctor/status latest-run whitelist: body-free and path-safe (no
# absolute workspace, detail/stderr, or arbitrary text/raw payloads).
MEMORY_LATEST_RUN_PUBLIC_KEYS = (
    "status",
    "exit_code",
    "crawler_version",
    "database",
    "started_at",
    "finished_at",
    "capability",
    "engine_version",
    "required_capability",
    "preflight",
    "scan_id",
    "scan_status",
    "created",
    "updated",
    "unchanged",
    "removed",
    "skipped",
    "failed",
    "stale",
    "partial",
    "canonical_count",
    "live_count",
    "hash_divergence",
    "unresolved_relations",
    "malformed_skipped",
    "memory_namespace",
    "last_completed_scan_id",
    "dry_run",
)

MEMORY_COUNT_KEYS = ("created", "updated", "unchanged", "removed", "skipped", "failed")
MEMORY_F2_REJECTED_FLAGS = frozenset({"--full"})
MEMORY_ALLOWED_FLAGS = frozenset({"--json", "--dry-run", "--rebuild", "--limit"})

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


def register_memory_facade_extensions(evidence_sub: Any = None) -> None:
    """Register the F2 read-only identity audit beneath ``evidence memory``."""

    if evidence_sub is None:
        return
    from pathlib import Path

    memory = evidence_sub.add_parser("memory", help="Audit canonical memory projection identity without editing cards.")
    memory_sub = memory.add_subparsers(dest="evidence_memory_command", metavar="<memory-command>")
    memory_sub.required = True
    audit = memory_sub.add_parser("audit", help="Read identity coverage and proposed aliases for memory cards.")
    audit.add_argument("workspaces", nargs="+", type=Path, help="Workspace roots containing memory/cards/.")
    audit.add_argument("--json", action="store_true", help="Print machine-readable JSON.")


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
    (so a stale projection can be re-crawled). Exit 0/1 alone is not enough:
    absent or malformed ``checks`` and explicit archive failures refuse
    delegation. Positive structured evidence is required (``database``,
    ``schema``, or ``fts`` with ``ok=true``), matching the engine's success
    path which emits schema/fts after a successful open rather than a
    ``database: ok`` row.
    """

    if exit_code not in (0, 1):
        return False, f"doctor exit {exit_code}"

    checks = payload.get("checks")
    if "checks" not in payload:
        return False, "absent checks: doctor JSON missing structured checks"
    if not isinstance(checks, list):
        return False, f"malformed checks: expected list, observed {type(checks).__name__}"
    if not checks:
        return False, "absent checks: doctor checks list is empty"

    well_formed = 0
    positive_archive = False
    for row in checks:
        if not isinstance(row, dict):
            continue
        name = row.get("name")
        if not isinstance(name, str) or not name:
            continue
        ok = row.get("ok")
        if not isinstance(ok, bool):
            continue
        well_formed += 1
        if name in {"database", "paths"} and ok is False:
            return False, str(row.get("detail") or f"{name} check failed")
        if name in {"database", "schema", "fts"} and ok is True:
            positive_archive = True

    if well_formed == 0:
        return False, "malformed checks: no well-formed doctor check rows"
    if not positive_archive:
        return (
            False,
            "absent archive-readability evidence: need database|schema|fts ok=true",
        )
    # exit 1 with structured positive archive evidence is allowed when another
    # check fails (for example memory_projection unhealthy while the archive
    # remains readable).
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
        value = raw.get(key)
        if key == "capability" and isinstance(value, str) and re.fullmatch(r"memory-projection\.v\d+", value):
            out[key] = value
        elif (
            key == "engine_version"
            and isinstance(value, str)
            and re.fullmatch(r"\d+(?:\.\d+){1,3}(?:[-+][A-Za-z0-9.-]+)?", value)
        ):
            out[key] = value
        elif (
            key == "memory_namespace"
            and isinstance(value, str)
            and re.fullmatch(
                r"memory-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}",
                value,
            )
        ):
            out[key] = value
        elif (
            key == "last_completed_scan_id"
            and isinstance(value, str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value)
        ):
            out[key] = value
        elif (
            key
            in {
                "canonical_count",
                "live_count",
                "hash_divergence",
                "unresolved_relations",
                "malformed_skipped",
                "failed",
            }
            and _typed_nonneg_int(value) is not None
        ):
            out[key] = value
        elif key in {"stale", "partial"} and isinstance(value, bool):
            out[key] = value
        elif key == "status" and value in {"absent", "completed", "failed", "interrupted"}:
            out[key] = value
    return out or None


def public_memory_latest_run(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """Whitelist body-free, path-safe fields from a memory last-run record."""

    if not isinstance(raw, dict):
        return None
    out: dict[str, Any] = {}
    for key in MEMORY_LATEST_RUN_PUBLIC_KEYS:
        if key not in raw:
            continue
        value = raw[key]
        if key == "database" and isinstance(value, str) and value not in {"ok", "unreadable"}:
            # Avoid leaking absolute archive paths via database.
            continue
        out[key] = value
    return out or None


def parse_memory_crawl_options(rest: list[str]) -> tuple[str | None, bool, bool, list[str]]:
    """Parse F1 crawl trailing options.

    Returns ``(error, want_json, dry_run, passthrough)``. ``--rebuild`` is
    delegated only after the same preflight as a normal crawl; ``--full`` is
    intentionally not exposed because it is broader than this projection.
    """

    want_json = False
    dry_run = False
    passthrough: list[str] = []
    i = 0
    while i < len(rest):
        arg = rest[i]
        if arg in MEMORY_F2_REJECTED_FLAGS:
            return (
                f"option {arg} is out of scope for evidence crawl memory F1 (projection rebuild belongs to F2)",
                False,
                False,
                [],
            )
        if arg == "--json":
            want_json = True
            i += 1
            continue
        if arg == "--dry-run":
            dry_run = True
            i += 1
            continue
        if arg == "--rebuild":
            passthrough.append(arg)
            i += 1
            continue
        if arg == "--limit":
            if i + 1 >= len(rest):
                return "option --limit requires a positive integer value", False, False, []
            value = rest[i + 1]
            if not value.isdigit() or int(value) < 1:
                return f"option --limit requires a positive integer, observed {value!r}", False, False, []
            passthrough.extend(["--limit", value])
            i += 2
            continue
        if arg.startswith("--limit="):
            value = arg.split("=", 1)[1]
            if not value.isdigit() or int(value) < 1:
                return f"option --limit requires a positive integer, observed {value!r}", False, False, []
            passthrough.append(arg)
            i += 1
            continue
        if arg.startswith("-"):
            return (
                f"unsupported evidence crawl memory option {arg!r}; allowed: {', '.join(sorted(MEMORY_ALLOWED_FLAGS))}",
                False,
                False,
                [],
            )
        return f"unexpected positional argument after workspace: {arg!r}", False, False, []
    return None, want_json, dry_run, passthrough


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


def _typed_nonneg_int(value: object) -> int | None:
    """Accept only real ints (reject bool, str, float, missing)."""

    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0:
        return None
    return value


def parse_memory_crawl_receipt(
    receipt: dict[str, Any] | None,
    *,
    dry_run: bool = False,
) -> tuple[str, dict[str, Any] | None, str | None]:
    """Validate an engine memory crawl receipt fail-closed.

    Returns ``(status, counts_or_none, detail)``. Status is one of
    ``ok | partial | fail | dry_run``. Missing/malformed fields never fabricate
    zero counts; ``counts_or_none`` is None when the receipt is incomplete.
    """

    if not isinstance(receipt, dict):
        return "fail", None, "malformed memory crawl receipt: expected JSON object"

    if dry_run or receipt.get("dry_run") is True:
        # Dry-run is non-mutating and never current/healthy.
        return "dry_run", None, "memory crawl dry-run is non-current; no scan receipt recorded as healthy"

    capability = receipt.get("capability")
    if capability != MEMORY_PROJECTION_CAPABILITY:
        return (
            "fail",
            None,
            f"incomplete memory crawl receipt: capability must be {MEMORY_PROJECTION_CAPABILITY}, "
            f"observed {capability!r}",
        )

    scan_id = receipt.get("scan_id")
    if not isinstance(scan_id, str) or not scan_id.strip():
        return "fail", None, "incomplete memory crawl receipt: missing or empty scan_id"

    status = receipt.get("status")
    if status != "completed":
        return (
            "fail",
            None,
            f"incomplete memory crawl receipt: status must be 'completed', observed {status!r}",
        )

    if not isinstance(receipt.get("stale"), bool):
        return "fail", None, "incomplete memory crawl receipt: stale must be an explicit boolean"
    if not isinstance(receipt.get("partial"), bool):
        return "fail", None, "incomplete memory crawl receipt: partial must be an explicit boolean"

    counts: dict[str, Any] = {"scan_id": scan_id.strip()}
    for key in MEMORY_COUNT_KEYS:
        parsed = _typed_nonneg_int(receipt.get(key))
        if parsed is None:
            return (
                "fail",
                None,
                f"incomplete memory crawl receipt: {key} must be a non-negative int, observed {receipt.get(key)!r}",
            )
        counts[key] = parsed

    stale = receipt["stale"]
    partial = receipt["partial"]
    failed = counts["failed"]
    if memory_projection_is_healthy(
        capability=MEMORY_PROJECTION_CAPABILITY,
        status="completed",
        stale=stale,
        partial=partial,
        failed=failed,
        engine_ok=True,
    ):
        return "ok", counts, None
    if failed > 0 or partial or stale:
        return "partial", counts, "memory crawl completed with partial/stale/failed state"
    return "fail", counts, "memory crawl receipt did not meet healthy contract"


def classify_memory_crawl_status(
    *,
    exit_code: int,
    receipt: dict[str, Any] | None,
    dry_run: bool = False,
) -> str:
    """Map engine crawl outcome to last-run status: ok | partial | fail | dry_run."""

    if exit_code != 0:
        return "fail"
    status, _counts, _detail = parse_memory_crawl_receipt(receipt, dry_run=dry_run)
    return status


def extract_memory_crawl_counts(receipt: dict[str, Any]) -> dict[str, Any] | None:
    """Return scan_id and the six typed counts, or None when incomplete.

    Unlike a soft coerce-to-zero helper, missing or mistyped fields fail closed.
    """

    status, counts, _detail = parse_memory_crawl_receipt(receipt, dry_run=False)
    if status == "dry_run":
        return None
    return counts
