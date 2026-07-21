"""Read-only inspection of pinned native Brigade components."""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import brigade
from brigade import component_install, component_manifest, component_paths, component_state, localio

ComponentStatus = Literal["healthy", "missing", "stale", "corrupt", "unsupported"]
STATE_FILE_STATUS = Literal["missing", "valid", "corrupt"]

# Deterministic precedence: first matching condition wins.
STATUS_PRECEDENCE: tuple[ComponentStatus, ...] = (
    "unsupported",
    "corrupt",
    "stale",
    "missing",
    "healthy",
)


@dataclass(frozen=True)
class ComponentInspection:
    component_id: str
    status: ComponentStatus
    detail: str
    expected_component_revision: str | None
    installed_component_revision: str | None
    expected_asset_name: str | None
    expected_byte_size: int | None
    expected_sha256: str | None
    installed_asset_name: str | None
    installed_byte_size: int | None
    installed_sha256: str | None
    recorded_executable: str | None
    managed_executable_path: str
    actual_byte_size: int | None
    actual_sha256: str | None
    path_binary: str | None


@dataclass(frozen=True)
class ComponentReport:
    brigade_version: str
    manifest_schema_version: int | None
    manifest_revision: str | None
    manifest_brigade_version: str | None
    manifest_path: str
    platform: str | None
    platform_error: str | None
    state_schema_version: int
    installed_state_path: str
    state_file_status: STATE_FILE_STATUS
    installed_manifest_revision: str | None
    installed_brigade_version: str | None
    installed_platform: str | None
    manifest_unknown_diagnostics: tuple[str, ...]
    components: tuple[ComponentInspection, ...]


_UNAVAILABLE_DATA_ROOT = "<unavailable>"


def inspect_components(
    *,
    env: Mapping[str, str] | None = None,
    system: str | None = None,
    manifest_path: Path | None = None,
) -> ComponentReport:
    """Inspect managed native components without mutating user data."""
    environment = dict(env if env is not None else os.environ)
    manifest_unknown: tuple[str, ...] = ()
    manifest_schema_version: int | None = None
    manifest_revision: str | None = None
    manifest_brigade_version: str | None = None
    roots: component_install.SetupRoots | None = None
    environment_error: str | None = None
    try:
        roots = component_install.resolve_roots(env=environment, system=system)
    except ValueError as exc:
        environment_error = str(exc)

    state_path = Path(
        component_paths.installed_state_path(roots.data_root if roots is not None else _UNAVAILABLE_DATA_ROOT)
    )
    installed_state, state_file_status = _read_installed_state(state_path) if roots is not None else (None, "missing")

    manifest_source = manifest_path or component_manifest.manifest_path()
    manifest: component_manifest.ComponentManifest | None = None
    try:
        if manifest_path is not None:
            manifest = component_manifest.load(manifest_path)
        elif roots is not None and component_install.uses_bundled_compatibility_manifest():
            exact_release_manifest = component_install.load_verified_exact_release_manifest(roots)
            if exact_release_manifest is None:
                manifest = component_manifest.load()
            else:
                manifest, manifest_source = exact_release_manifest
                if installed_state is not None and not _installed_state_matches_manifest(installed_state, manifest):
                    manifest = component_manifest.load()
                    manifest_source = component_manifest.manifest_path()
        else:
            manifest = component_manifest.load()
    except component_install.ExactReleaseManifestError as exc:
        return _environment_blocked_report(
            manifest_source=exc.manifest_path,
            platform_error=str(exc),
            manifest=None,
            roots=roots,
            state_path=state_path,
            installed_state=installed_state,
            state_file_status=state_file_status,
        )
    except ValueError as exc:
        return _environment_blocked_report(
            manifest_source=manifest_source,
            platform_error=str(exc),
            manifest=None,
            roots=roots,
            state_path=state_path,
            installed_state=installed_state,
            state_file_status=state_file_status,
        )
    manifest_schema_version = manifest.schema_version
    manifest_revision = manifest.manifest_revision
    manifest_brigade_version = manifest.brigade_version
    manifest_unknown = manifest.unknown_component_diagnostics

    platform: str | None = None
    platform_error: str | None = environment_error
    if environment_error is None:
        try:
            platform = component_manifest.platform_key(system=system)
        except ValueError as exc:
            platform_error = str(exc)

    if roots is None:
        return _environment_blocked_report(
            manifest_source=manifest_source,
            platform_error=platform_error or "component environment is unavailable",
            manifest=manifest,
            manifest_unknown_diagnostics=manifest_unknown,
            roots=roots,
            state_path=state_path,
            installed_state=installed_state,
            state_file_status=state_file_status,
        )

    inspections: list[ComponentInspection] = []
    for component_id in component_manifest.KNOWN_COMPONENT_IDS:
        inspections.append(
            _inspect_component(
                component_id,
                manifest=manifest,
                platform=platform,
                platform_error=platform_error,
                roots=roots,
                installed_state=installed_state,
                state_file_status=state_file_status,
                env=environment,
            )
        )

    return ComponentReport(
        brigade_version=brigade.__version__,
        manifest_schema_version=manifest_schema_version,
        manifest_revision=manifest_revision,
        manifest_brigade_version=manifest_brigade_version,
        manifest_path=str(manifest_source),
        platform=platform,
        platform_error=platform_error,
        state_schema_version=component_state.SCHEMA_VERSION,
        installed_state_path=str(state_path),
        state_file_status=state_file_status,
        installed_manifest_revision=installed_state.manifest_revision if installed_state else None,
        installed_brigade_version=installed_state.brigade_version if installed_state else None,
        installed_platform=installed_state.platform if installed_state else None,
        manifest_unknown_diagnostics=manifest_unknown,
        components=tuple(inspections),
    )


def _environment_blocked_report(
    *,
    manifest_source: Path,
    platform_error: str,
    manifest: component_manifest.ComponentManifest | None,
    manifest_unknown_diagnostics: tuple[str, ...] = (),
    roots: component_install.SetupRoots | None = None,
    state_path: Path | None = None,
    installed_state: component_state.InstalledState | None = None,
    state_file_status: STATE_FILE_STATUS = "missing",
) -> ComponentReport:
    """Return a read-only unsupported report when roots or manifest cannot be resolved."""
    report_state_path = state_path or Path(component_paths.installed_state_path(_UNAVAILABLE_DATA_ROOT))
    data_root = roots.data_root if roots is not None else _UNAVAILABLE_DATA_ROOT
    components = tuple(
        ComponentInspection(
            component_id=component_id,
            status="unsupported",
            detail=platform_error,
            expected_component_revision=(
                manifest.components[component_id].component_revision if manifest is not None else None
            ),
            installed_component_revision=(
                installed_state.components[component_id].component_revision
                if installed_state is not None and component_id in installed_state.components
                else None
            ),
            expected_asset_name=None,
            expected_byte_size=None,
            expected_sha256=None,
            installed_asset_name=(
                installed_state.components[component_id].asset_name
                if installed_state is not None and component_id in installed_state.components
                else None
            ),
            installed_byte_size=(
                installed_state.components[component_id].byte_size
                if installed_state is not None and component_id in installed_state.components
                else None
            ),
            installed_sha256=(
                installed_state.components[component_id].sha256
                if installed_state is not None and component_id in installed_state.components
                else None
            ),
            recorded_executable=(
                installed_state.components[component_id].executable
                if installed_state is not None and component_id in installed_state.components
                else None
            ),
            managed_executable_path=component_paths.managed_executable_path(
                data_root,
                component_id,
            ),
            actual_byte_size=None,
            actual_sha256=None,
            path_binary=None,
        )
        for component_id in component_manifest.KNOWN_COMPONENT_IDS
    )
    return ComponentReport(
        brigade_version=brigade.__version__,
        manifest_schema_version=manifest.schema_version if manifest is not None else None,
        manifest_revision=manifest.manifest_revision if manifest is not None else None,
        manifest_brigade_version=manifest.brigade_version if manifest is not None else None,
        manifest_path=str(manifest_source),
        platform=None,
        platform_error=platform_error,
        state_schema_version=component_state.SCHEMA_VERSION,
        installed_state_path=str(report_state_path),
        state_file_status=state_file_status,
        installed_manifest_revision=installed_state.manifest_revision if installed_state else None,
        installed_brigade_version=installed_state.brigade_version if installed_state else None,
        installed_platform=installed_state.platform if installed_state else None,
        manifest_unknown_diagnostics=manifest_unknown_diagnostics,
        components=components,
    )


def _installed_state_matches_manifest(
    installed_state: component_state.InstalledState,
    manifest: component_manifest.ComponentManifest,
) -> bool:
    """Return whether installed components use the manifest's exact recorded coordinates."""
    if (
        installed_state.manifest_revision != manifest.manifest_revision
        or installed_state.brigade_version != manifest.brigade_version
    ):
        return False
    for component_id in component_manifest.KNOWN_COMPONENT_IDS:
        installed = installed_state.components.get(component_id)
        if installed is None:
            return False
        component = manifest.components[component_id]
        try:
            asset = component_manifest.resolve_asset(manifest, component_id, installed_state.platform)
        except ValueError:
            return False
        if (
            installed.component_revision != component.component_revision
            or installed.asset_name != asset.asset_name
            or installed.byte_size != asset.byte_size
            or installed.sha256 != asset.sha256
            or installed.download_url != asset.download_url
        ):
            return False
    return True


def doctor_checks(
    *,
    env: Mapping[str, str] | None = None,
    system: str | None = None,
    manifest_path: Path | None = None,
) -> list[tuple[str, str, str]]:
    """Return doctor check tuples for managed component inspection."""
    from .doctor import FAIL, INFO

    report = inspect_components(env=env, system=system, manifest_path=manifest_path)
    checks: list[tuple[str, str, str]] = []
    if report.manifest_schema_version is None:
        checks.append((FAIL, "components: manifest", report.platform_error or "manifest could not be loaded"))
        return checks
    for diagnostic in report.manifest_unknown_diagnostics:
        checks.append((INFO, "components: manifest", diagnostic))
    if report.platform is None and report.platform_error:
        checks.append((INFO, "components: platform", report.platform_error))
    for component in report.components:
        checks.append(
            (_doctor_severity(component, report), f"components: {component.component_id}", _doctor_detail(component))
        )
    return checks


def render_text(report: ComponentReport) -> str:
    lines = [f"brigade {report.brigade_version}"]
    if report.manifest_schema_version is not None:
        lines.append(f"manifest schema {report.manifest_schema_version} (revision {report.manifest_revision})")
    else:
        lines.append(f"manifest: {report.platform_error}")
    if report.platform is not None:
        lines.append(f"platform: {report.platform}")
    elif report.platform_error:
        lines.append(f"platform: unsupported ({report.platform_error})")
    lines.append("")
    for component in report.components:
        lines.append(f"{component.component_id}: {component.status}")
        if component.expected_component_revision is not None:
            lines.append(f"  expected revision: {component.expected_component_revision}")
        if component.installed_component_revision is not None:
            lines.append(f"  installed revision: {component.installed_component_revision}")
        if component.expected_asset_name is not None:
            lines.append(f"  expected asset: {component.expected_asset_name}")
        if component.expected_byte_size is not None and component.expected_sha256 is not None:
            lines.append(f"  expected size: {component.expected_byte_size} sha256: {component.expected_sha256}")
        if component.installed_asset_name is not None:
            lines.append(f"  installed asset: {component.installed_asset_name}")
        if component.installed_byte_size is not None and component.installed_sha256 is not None:
            lines.append(f"  installed size: {component.installed_byte_size} sha256: {component.installed_sha256}")
        if component.recorded_executable is not None:
            lines.append(f"  recorded executable: {component.recorded_executable}")
        lines.append(f"  managed executable: {component.managed_executable_path}")
        if component.actual_byte_size is not None:
            lines.append(f"  actual size: {component.actual_byte_size}")
        if component.actual_sha256 is not None:
            lines.append(f"  actual sha256: {component.actual_sha256}")
        if component.path_binary is not None:
            lines.append(f"  path binary: {component.path_binary}")
        if component.detail:
            lines.append(f"  detail: {component.detail}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_json(report: ComponentReport) -> dict[str, Any]:
    return {
        "brigade_version": report.brigade_version,
        "manifest": {
            "schema_version": report.manifest_schema_version,
            "revision": report.manifest_revision,
            "brigade_version": report.manifest_brigade_version,
            "path": report.manifest_path,
            "unknown_component_diagnostics": list(report.manifest_unknown_diagnostics),
        },
        "platform": report.platform,
        "platform_error": report.platform_error,
        "installed_state": {
            "schema_version": report.state_schema_version,
            "path": report.installed_state_path,
            "state_file_status": report.state_file_status,
            "manifest_revision": report.installed_manifest_revision,
            "brigade_version": report.installed_brigade_version,
            "platform": report.installed_platform,
        },
        "status_precedence": list(STATUS_PRECEDENCE),
        "components": [
            {
                "component_id": component.component_id,
                "status": component.status,
                "detail": component.detail,
                "expected_component_revision": component.expected_component_revision,
                "installed_component_revision": component.installed_component_revision,
                "expected_asset_name": component.expected_asset_name,
                "expected_byte_size": component.expected_byte_size,
                "expected_sha256": component.expected_sha256,
                "installed_asset_name": component.installed_asset_name,
                "installed_byte_size": component.installed_byte_size,
                "installed_sha256": component.installed_sha256,
                "recorded_executable": component.recorded_executable,
                "managed_executable_path": component.managed_executable_path,
                "actual_byte_size": component.actual_byte_size,
                "actual_sha256": component.actual_sha256,
                "path_binary": component.path_binary,
            }
            for component in report.components
        ],
    }


def _read_installed_state(path: Path) -> tuple[component_state.InstalledState | None, STATE_FILE_STATUS]:
    if not path.exists():
        return None, "missing"
    state = component_state.load_installed_state(path)
    if state is None:
        return None, "corrupt"
    return state, "valid"


def _path_binary(executable: str, managed_path: str, env: Mapping[str, str]) -> str | None:
    try:
        found = shutil.which(executable, path=env.get("PATH"))
        if found is None:
            return None
        resolved_managed = str(Path(managed_path).resolve())
        resolved_found = str(Path(found).resolve())
    except OSError:
        return None
    if resolved_found == resolved_managed:
        return None
    return resolved_found


def _inspect_component(
    component_id: str,
    *,
    manifest: component_manifest.ComponentManifest,
    platform: str | None,
    platform_error: str | None,
    roots: component_install.SetupRoots,
    installed_state: component_state.InstalledState | None,
    state_file_status: STATE_FILE_STATUS,
    env: Mapping[str, str],
) -> ComponentInspection:
    component = manifest.components[component_id]
    managed_path = component_paths.managed_executable_path(roots.data_root, component_id)
    path_binary = _path_binary(component.executable, managed_path, env)
    asset: component_manifest.ComponentAsset | None = None
    asset_error: str | None = None
    if platform is not None:
        try:
            asset = component_manifest.resolve_asset(manifest, component_id, platform)
        except ValueError as exc:
            asset_error = str(exc)

    managed_file = Path(managed_path)
    managed_exists = False
    actual_byte_size: int | None = None
    actual_sha256: str | None = None
    file_inspection_error: str | None = None
    try:
        managed_exists = managed_file.is_file()
        if managed_exists:
            actual_byte_size = managed_file.stat().st_size
            actual_sha256 = localio.file_sha256(managed_file)
    except OSError as exc:
        file_inspection_error = str(exc)
        managed_exists = True

    installed_record = installed_state.components.get(component_id) if installed_state is not None else None
    status, detail = _diagnose_component(
        platform=platform,
        platform_error=platform_error,
        asset=asset,
        asset_error=asset_error,
        state_file_status=state_file_status,
        installed_state=installed_state,
        installed_record=installed_record,
        expected_revision=component.component_revision,
        expected_executable=managed_path,
        manifest_revision=manifest.manifest_revision,
        managed_exists=managed_exists,
        actual_byte_size=actual_byte_size,
        actual_sha256=actual_sha256,
        managed_path=managed_path,
        file_inspection_error=file_inspection_error,
    )
    return ComponentInspection(
        component_id=component_id,
        status=status,
        detail=detail,
        expected_component_revision=component.component_revision,
        installed_component_revision=installed_record.component_revision if installed_record else None,
        expected_asset_name=asset.asset_name if asset is not None else None,
        expected_byte_size=asset.byte_size if asset is not None else None,
        expected_sha256=asset.sha256 if asset is not None else None,
        installed_asset_name=installed_record.asset_name if installed_record else None,
        installed_byte_size=installed_record.byte_size if installed_record else None,
        installed_sha256=installed_record.sha256 if installed_record else None,
        recorded_executable=installed_record.executable if installed_record else None,
        managed_executable_path=managed_path,
        actual_byte_size=actual_byte_size,
        actual_sha256=actual_sha256,
        path_binary=path_binary,
    )


def _diagnose_component(
    *,
    platform: str | None,
    platform_error: str | None,
    asset: component_manifest.ComponentAsset | None,
    asset_error: str | None,
    state_file_status: STATE_FILE_STATUS,
    installed_state: component_state.InstalledState | None,
    installed_record: component_state.InstalledComponentRecord | None,
    expected_revision: str,
    expected_executable: str,
    manifest_revision: str,
    managed_exists: bool,
    actual_byte_size: int | None,
    actual_sha256: str | None,
    managed_path: str,
    file_inspection_error: str | None = None,
) -> tuple[ComponentStatus, str]:
    findings: dict[ComponentStatus, str] = {}

    if file_inspection_error is not None:
        findings["corrupt"] = f"managed executable is unreadable: {file_inspection_error}"

    if platform is None:
        findings["unsupported"] = platform_error or "host platform is unsupported"
    elif asset is None:
        findings["unsupported"] = asset_error or f"no asset for platform {platform!r}"

    if state_file_status == "corrupt":
        findings["corrupt"] = "installed.json exists but is invalid"

    if state_file_status == "missing" and managed_exists:
        findings["corrupt"] = "managed executable exists without installed.json"
    if state_file_status == "valid" and installed_record is None and managed_exists:
        findings["corrupt"] = "managed executable exists but component is absent from installed state"

    if managed_exists and asset is not None and file_inspection_error is None:
        if actual_byte_size != asset.byte_size:
            findings["corrupt"] = (
                f"byte_size mismatch for {managed_path}: expected {asset.byte_size}, got {actual_byte_size}"
            )
        elif actual_sha256 != asset.sha256:
            findings["corrupt"] = f"sha256 mismatch for {managed_path}: expected {asset.sha256}, got {actual_sha256}"

    if installed_state is not None and platform is not None and installed_state.platform != platform:
        findings["unsupported"] = (
            f"installed state platform {installed_state.platform!r} does not match host {platform!r}"
        )

    if installed_state is not None and installed_state.manifest_revision != manifest_revision:
        findings["stale"] = (
            f"installed manifest revision {installed_state.manifest_revision!r} != expected {manifest_revision!r}"
        )
    if installed_record is not None and installed_record.component_revision != expected_revision:
        findings["stale"] = (
            f"installed component revision {installed_record.component_revision!r} != expected {expected_revision!r}"
        )
    if installed_record is not None and asset is not None:
        stale_metadata: list[str] = []
        if installed_record.asset_name != asset.asset_name:
            stale_metadata.append(
                f"installed asset name {installed_record.asset_name!r} != expected {asset.asset_name!r}"
            )
        if installed_record.byte_size != asset.byte_size:
            stale_metadata.append(f"installed byte size {installed_record.byte_size} != expected {asset.byte_size}")
        if installed_record.sha256 != asset.sha256:
            stale_metadata.append("installed sha256 does not match manifest asset")
        if installed_record.executable != expected_executable:
            stale_metadata.append(
                f"recorded executable {installed_record.executable!r} != expected {expected_executable!r}"
            )
        if stale_metadata:
            findings["stale"] = "; ".join(stale_metadata)

    if state_file_status == "missing" and installed_record is None and not managed_exists:
        findings["missing"] = "not installed"
    elif state_file_status == "valid" and installed_record is None and not managed_exists:
        findings["missing"] = "component not present in installed state"
    elif not managed_exists and "corrupt" not in findings and "stale" not in findings:
        findings["missing"] = f"managed executable missing: {managed_path}"

    if not findings:
        findings["healthy"] = "installed and verified"

    for status in STATUS_PRECEDENCE:
        if status in findings:
            return status, findings[status]
    return "healthy", findings["healthy"]


def _doctor_severity(component: ComponentInspection, report: ComponentReport) -> str:
    from .doctor import FAIL, INFO, MANUAL, OK, WARN

    if component.status == "healthy":
        return OK
    if component.status == "missing":
        return MANUAL
    if component.status == "stale":
        return WARN
    if component.status == "corrupt":
        return FAIL
    if component.status == "unsupported":
        if report.state_file_status == "corrupt":
            return FAIL
        if report.installed_platform is not None and report.platform is not None:
            if report.installed_platform != report.platform:
                return FAIL
        return INFO
    return WARN


def _doctor_detail(component: ComponentInspection) -> str:
    expected = component.expected_component_revision or "unknown"
    installed = component.installed_component_revision or "none"
    return f"expected revision {expected}, installed revision {installed}; {component.detail}"
