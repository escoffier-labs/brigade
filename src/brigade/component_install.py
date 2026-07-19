"""Download, verify, cache, and install pinned native Brigade components."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import brigade
from brigade import component_manifest, component_paths, component_state, localio

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_READ_CHUNK = 1024 * 1024
_EXECUTABLE_MODE = 0o755
_SMOKE_COMPONENT_IDS = component_manifest.KNOWN_COMPONENT_IDS
_SMOKE_TIMEOUT_SECONDS = 30.0
_DOWNLOAD_TIMEOUT_SECONDS = 30.0
_JSONRPC_INIT_REQUEST = json.dumps(
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "brigade-setup", "version": "1"},
        },
    }
)


class ComponentInstallError(RuntimeError):
    """Raised when a component install step fails verification."""


_SETUP_ACTIONS: tuple[str, ...] = ("verify-cache", "download", "materialize", "smoke")


@dataclass(frozen=True)
class SetupPlanAction:
    component_id: str
    action: str
    cache_path: str
    managed_path: str
    asset_name: str
    byte_size: int
    sha256: str
    download_url: str
    component_revision: str


@dataclass(frozen=True)
class SetupRoots:
    data_root: str
    cache_root: str
    env: Mapping[str, str]


def resolve_roots(
    *,
    env: Mapping[str, str] | None = None,
    system: str | None = None,
) -> SetupRoots:
    """Resolve user-local data and cache roots for component setup."""
    environment = dict(env if env is not None else os.environ)
    data_root_path = component_paths.data_root(env=environment, system=system)
    cache_root_path = component_paths.cache_root(env=environment, system=system)
    return SetupRoots(
        data_root=data_root_path,
        cache_root=cache_root_path,
        env=environment,
    )


def build_setup_plan(
    manifest: component_manifest.ComponentManifest,
    *,
    platform: str,
    roots: SetupRoots,
) -> list[SetupPlanAction]:
    """Build a deterministic dry-run/install plan for every known component."""
    plan: list[SetupPlanAction] = []
    for component_id in component_manifest.KNOWN_COMPONENT_IDS:
        asset = component_manifest.resolve_asset(manifest, component_id, platform)
        component = manifest.components[component_id]
        cache_path = component_paths.cached_asset_path(
            roots.cache_root,
            asset.sha256,
            asset.asset_name,
        )
        managed_path = component_paths.managed_executable_path(
            roots.data_root,
            component.executable,
        )
        for action in _SETUP_ACTIONS:
            plan.append(
                SetupPlanAction(
                    component_id=component_id,
                    action=action,
                    cache_path=cache_path,
                    managed_path=managed_path,
                    asset_name=asset.asset_name,
                    byte_size=asset.byte_size,
                    sha256=asset.sha256,
                    download_url=asset.download_url,
                    component_revision=component.component_revision,
                )
            )
    return plan


def _print_setup_dry_run(
    manifest: component_manifest.ComponentManifest,
    *,
    platform: str,
    plan: Sequence[SetupPlanAction],
) -> None:
    print("component setup dry-run")
    print(f"brigade_version: {brigade.__version__}")
    print(f"manifest_revision: {manifest.manifest_revision}")
    print(f"platform: {platform}")
    current_component: str | None = None
    for entry in plan:
        if entry.component_id != current_component:
            current_component = entry.component_id
            print()
            print(f"component: {entry.component_id}")
            print(f"component_revision: {entry.component_revision}")
            print(f"asset_name: {entry.asset_name}")
            print(f"byte_size: {entry.byte_size}")
            print(f"sha256: {entry.sha256}")
            print(f"download_url: {entry.download_url}")
            print(f"cache_path: {entry.cache_path}")
            print(f"managed_path: {entry.managed_path}")
            print("actions:")
        print(f"  - {entry.action}")


def _setup_dry_run(
    manifest: component_manifest.ComponentManifest,
    *,
    env: Mapping[str, str] | None,
) -> int:
    try:
        roots = resolve_roots(env=env)
        platform = component_manifest.platform_key()
        plan = build_setup_plan(manifest, platform=platform, roots=roots)
    except ValueError as exc:
        print(f"component setup: {exc}", file=sys.stderr)
        return 1
    _print_setup_dry_run(manifest, platform=platform, plan=plan)
    return 0


@dataclass(frozen=True)
class _ManagedExecutableSnapshot:
    path: Path
    existed: bool
    content: bytes | None = None
    mode: int | None = None


def _snapshot_managed_executables(paths: Sequence[Path]) -> tuple[_ManagedExecutableSnapshot, ...]:
    snapshots: list[_ManagedExecutableSnapshot] = []
    for path in paths:
        if path.is_file():
            stat_result = path.stat()
            snapshots.append(
                _ManagedExecutableSnapshot(
                    path=path,
                    existed=True,
                    content=path.read_bytes(),
                    mode=stat_result.st_mode,
                )
            )
        else:
            snapshots.append(_ManagedExecutableSnapshot(path=path, existed=False))
    return tuple(snapshots)


def _restore_managed_executables(snapshots: Sequence[_ManagedExecutableSnapshot]) -> None:
    for snapshot in snapshots:
        if snapshot.existed:
            if snapshot.content is None or snapshot.mode is None:
                raise ComponentInstallError(f"invalid managed executable snapshot for {snapshot.path}")
            _restore_file_atomically(
                snapshot.path,
                content=snapshot.content,
                mode=snapshot.mode,
            )
        elif snapshot.path.exists():
            snapshot.path.unlink()


def _restore_file_atomically(path: Path, *, content: bytes, mode: int) -> None:
    """Restore one snapshotted file through a temp sibling and os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        tmp_path.chmod(stat.S_IMODE(mode))
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def materialize_executable(*, cache_path: Path, managed_path: Path) -> None:
    """Copy a verified cache file into a managed executable path atomically."""
    if not cache_path.is_file():
        raise ComponentInstallError(f"cache asset missing: {cache_path}")
    managed_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=managed_path.parent,
        prefix=f".{managed_path.name}.",
        suffix=".tmp",
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            with cache_path.open("rb") as source:
                while True:
                    chunk = source.read(_READ_CHUNK)
                    if not chunk:
                        break
                    handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        if sys.platform != "win32":
            tmp_path.chmod(_EXECUTABLE_MODE)
        os.replace(tmp_path, managed_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _validate_expected(*, byte_size: int, sha256: str) -> None:
    if not isinstance(byte_size, int) or isinstance(byte_size, bool) or byte_size <= 0:
        raise ComponentInstallError(f"byte_size must be a positive integer, got {byte_size!r}")
    if not _SHA256.fullmatch(sha256):
        raise ComponentInstallError(f"sha256 must be 64 lowercase hex characters, got {sha256!r}")


def _require_https_final_url(response: Any) -> None:
    geturl = getattr(response, "geturl", None)
    if geturl is None:
        return
    final_url = geturl()
    if urlparse(final_url).scheme.lower() != "https":
        raise ComponentInstallError(f"download redirect downgraded to non-https URL: {final_url}")


def _write_bounded_download(handle: Any, response: Any, *, byte_size: int) -> None:
    nbytes = 0
    while True:
        remaining = byte_size - nbytes
        if remaining <= 0:
            if response.read(1):
                raise ComponentInstallError(f"download exceeded byte_size {byte_size}")
            break
        chunk = response.read(min(_READ_CHUNK, remaining + 1))
        if not chunk:
            break
        if len(chunk) > remaining:
            handle.write(chunk[:remaining])
            raise ComponentInstallError(f"download exceeded byte_size {byte_size}")
        handle.write(chunk)
        nbytes += len(chunk)


def _managed_executable_is_ready(path: Path, *, byte_size: int, sha256: str) -> bool:
    try:
        verify_cached_asset(path, byte_size=byte_size, sha256=sha256)
    except ComponentInstallError:
        return False
    if sys.platform == "win32":
        return True
    mode = path.stat().st_mode
    return bool(mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))


def verify_cached_asset(path: Path, *, byte_size: int, sha256: str) -> None:
    """Verify cached asset byte size and SHA-256 digest."""
    _validate_expected(byte_size=byte_size, sha256=sha256)
    if not path.is_file():
        raise ComponentInstallError(f"cached asset missing: {path}")
    actual_size = path.stat().st_size
    if actual_size != byte_size:
        raise ComponentInstallError(f"byte_size mismatch for {path}: expected {byte_size}, got {actual_size}")
    actual_sha256 = localio.file_sha256(path)
    if actual_sha256 != sha256:
        raise ComponentInstallError(f"sha256 mismatch for {path}: expected {sha256}, got {actual_sha256}")


def fetch_asset_to_cache(
    asset: component_manifest.ComponentAsset,
    *,
    cache_path: Path,
    offline: bool,
    opener: Callable[..., Any] | None = None,
) -> Path:
    """Download or reuse a verified cache entry for asset."""
    _validate_expected(byte_size=asset.byte_size, sha256=asset.sha256)
    try:
        verify_cached_asset(cache_path, byte_size=asset.byte_size, sha256=asset.sha256)
    except ComponentInstallError:
        if offline:
            raise ComponentInstallError(f"offline: verified cache required at {cache_path}") from None
    else:
        return cache_path

    def open_url(url: str) -> Any:
        if opener is not None:
            return opener(url)
        return urllib.request.urlopen(url, timeout=_DOWNLOAD_TIMEOUT_SECONDS)

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=cache_path.parent, prefix=f".{cache_path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            with open_url(asset.download_url) as response:
                _require_https_final_url(response)
                _write_bounded_download(handle, response, byte_size=asset.byte_size)
            handle.flush()
            os.fsync(handle.fileno())
        verify_cached_asset(tmp_path, byte_size=asset.byte_size, sha256=asset.sha256)
        os.replace(tmp_path, cache_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return cache_path


def _default_smoke_runner(argv: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    run_kwargs: dict[str, object] = {
        "capture_output": True,
        "text": True,
        "shell": False,
        "timeout": _SMOKE_TIMEOUT_SECONDS,
    }
    run_kwargs.update(kwargs)
    return subprocess.run(list(argv), **run_kwargs)  # type: ignore[arg-type, call-overload]


def _invoke_smoke_runner(
    run: Callable[..., subprocess.CompletedProcess[str]],
    argv: Sequence[str],
    **kwargs: object,
) -> subprocess.CompletedProcess[str]:
    return run(
        list(argv),
        capture_output=True,
        text=True,
        shell=False,
        timeout=_SMOKE_TIMEOUT_SECONDS,
        **kwargs,
    )


def _validate_smoke_managed_paths(managed_paths: Mapping[str, str]) -> dict[str, Path]:
    keys = set(managed_paths)
    expected = set(_SMOKE_COMPONENT_IDS)
    if keys != expected:
        missing = sorted(expected - keys)
        extra = sorted(keys - expected)
        parts: list[str] = []
        if missing:
            parts.append(f"missing: {', '.join(missing)}")
        if extra:
            parts.append(f"unexpected: {', '.join(extra)}")
        raise ComponentInstallError(
            f"post-install smoke requires exactly {len(expected)} managed paths; {'; '.join(parts)}"
        )

    resolved: dict[str, Path] = {}
    for component_id in _SMOKE_COMPONENT_IDS:
        raw = managed_paths[component_id]
        path = Path(raw)
        if not path.is_absolute():
            raise ComponentInstallError(
                f"post-install smoke for {component_id} requires absolute managed path, got {raw!r}"
            )
        if not path.is_file():
            raise ComponentInstallError(f"post-install smoke for {component_id}: managed executable missing: {raw}")
        resolved[component_id] = path
    return resolved


def _smoke_graphtrail(
    path: Path,
    run: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    argv = [str(path), "--version"]
    try:
        completed = _invoke_smoke_runner(run, argv)
    except subprocess.TimeoutExpired as exc:
        raise ComponentInstallError(f"graphtrail smoke timed out after {_SMOKE_TIMEOUT_SECONDS}s") from exc
    except OSError as exc:
        raise ComponentInstallError(f"graphtrail smoke failed to run {path}: {exc}") from exc

    if completed.returncode != 0:
        raise ComponentInstallError(f"graphtrail smoke failed: {path} --version exited {completed.returncode}")
    if not (completed.stdout or "").strip():
        raise ComponentInstallError(f"graphtrail smoke failed: {path} --version produced empty stdout")


def _smoke_graphtrail_mcp(
    path: Path,
    run: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    argv = [str(path)]
    try:
        completed = _invoke_smoke_runner(run, argv, input=_JSONRPC_INIT_REQUEST)
    except subprocess.TimeoutExpired as exc:
        raise ComponentInstallError(f"graphtrail-mcp smoke timed out after {_SMOKE_TIMEOUT_SECONDS}s") from exc
    except OSError as exc:
        raise ComponentInstallError(f"graphtrail-mcp smoke failed to run {path}: {exc}") from exc

    if completed.returncode != 0:
        raise ComponentInstallError(f"graphtrail-mcp smoke failed: {path} exited {completed.returncode}")
    stdout = (completed.stdout or "").strip()
    if not stdout:
        raise ComponentInstallError(f"graphtrail-mcp smoke failed: {path} produced empty stdout")
    try:
        response = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ComponentInstallError(
            f"graphtrail-mcp smoke failed: {path} returned malformed JSON-RPC response"
        ) from exc
    if response.get("jsonrpc") != "2.0":
        raise ComponentInstallError(f"graphtrail-mcp smoke failed: {path} returned invalid JSON-RPC version")
    if response.get("id") != 1:
        raise ComponentInstallError(f"graphtrail-mcp smoke failed: {path} JSON-RPC response id mismatch")
    if "result" not in response:
        raise ComponentInstallError(f"graphtrail-mcp smoke failed: {path} JSON-RPC response missing result")


def _smoke_miseledger(
    path: Path,
    run: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    argv = [str(path), "version"]
    try:
        completed = _invoke_smoke_runner(run, argv)
    except subprocess.TimeoutExpired as exc:
        raise ComponentInstallError(f"miseledger smoke timed out after {_SMOKE_TIMEOUT_SECONDS}s") from exc
    except OSError as exc:
        raise ComponentInstallError(f"miseledger smoke failed to run {path}: {exc}") from exc

    if completed.returncode != 0:
        raise ComponentInstallError(f"miseledger smoke failed: {path} version exited {completed.returncode}")


def _smoke_sessionfind(
    path: Path,
    run: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    argv = [str(path), "--help"]
    try:
        completed = _invoke_smoke_runner(run, argv)
    except subprocess.TimeoutExpired as exc:
        raise ComponentInstallError(f"sessionfind smoke timed out after {_SMOKE_TIMEOUT_SECONDS}s") from exc
    except OSError as exc:
        raise ComponentInstallError(f"sessionfind smoke failed to run {path}: {exc}") from exc

    if completed.returncode != 2:
        raise ComponentInstallError(
            f"sessionfind smoke failed: {path} --help exited {completed.returncode}, expected 2"
        )
    combined = f"{completed.stdout or ''}{completed.stderr or ''}"
    if "usage" not in combined.lower():
        raise ComponentInstallError(f"sessionfind smoke failed: {path} --help produced no usage text")


def run_post_install_smoke(
    managed_paths: Mapping[str, str],
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> None:
    """Run post-install smoke checks using only absolute managed executable paths."""
    paths = _validate_smoke_managed_paths(managed_paths)
    run = runner if runner is not None else _default_smoke_runner

    _smoke_graphtrail(paths["graphtrail"], run)
    _smoke_graphtrail_mcp(paths["graphtrail-mcp"], run)
    _smoke_miseledger(paths["miseledger"], run)
    _smoke_sessionfind(paths["sessionfind"], run)


def _load_rollback_state(
    path: Path,
    *,
    label: str,
    platform: str,
) -> component_state.InstalledState:
    if not path.is_file():
        raise ComponentInstallError(f"{label} installed state missing: {path}")
    state = component_state.load_installed_state(path)
    if state is None:
        raise ComponentInstallError(f"invalid {label} installed state: {path}")
    expected_components = set(component_manifest.KNOWN_COMPONENT_IDS)
    if set(state.components) != expected_components:
        raise ComponentInstallError(
            f"{label} installed state requires exactly {len(expected_components)} components: {path}"
        )
    if state.platform != platform:
        raise ComponentInstallError(
            f"{label} installed state platform {state.platform!r} does not match host {platform!r}"
        )
    return state


def _rollback_cache_paths(
    state: component_state.InstalledState,
    *,
    cache_root: str,
) -> dict[str, Path]:
    cache_paths: dict[str, Path] = {}
    for component_id in component_manifest.KNOWN_COMPONENT_IDS:
        record = state.components[component_id]
        try:
            cache_path = component_paths.cached_asset_path(
                cache_root,
                record.sha256,
                record.asset_name,
            )
        except ValueError as exc:
            raise ComponentInstallError(f"invalid previous installed cache path for {component_id}: {exc}") from exc
        cache_paths[component_id] = Path(cache_path)
    return cache_paths


def _setup_rollback(
    *,
    env: Mapping[str, str] | None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None,
) -> int:
    roots = resolve_roots(env=env)
    platform = component_manifest.platform_key()
    current_state_path = Path(component_paths.installed_state_path(roots.data_root))
    previous_state_path = Path(component_paths.installed_previous_state_path(roots.data_root))
    current_state = _load_rollback_state(
        current_state_path,
        label="current",
        platform=platform,
    )
    previous_state = _load_rollback_state(
        previous_state_path,
        label="previous",
        platform=platform,
    )
    cache_paths = _rollback_cache_paths(previous_state, cache_root=roots.cache_root)

    for component_id in component_manifest.KNOWN_COMPONENT_IDS:
        record = previous_state.components[component_id]
        verify_cached_asset(
            cache_paths[component_id],
            byte_size=record.byte_size,
            sha256=record.sha256,
        )

    managed_paths = {
        component_id: Path(component_paths.managed_executable_path(roots.data_root, component_id))
        for component_id in component_manifest.KNOWN_COMPONENT_IDS
    }
    managed_snapshots = _snapshot_managed_executables(list(managed_paths.values()))
    state_snapshots = _snapshot_managed_executables([current_state_path, previous_state_path])
    try:
        for component_id in component_manifest.KNOWN_COMPONENT_IDS:
            materialize_executable(
                cache_path=cache_paths[component_id],
                managed_path=managed_paths[component_id],
            )
        for component_id in component_manifest.KNOWN_COMPONENT_IDS:
            record = previous_state.components[component_id]
            verify_cached_asset(
                managed_paths[component_id],
                byte_size=record.byte_size,
                sha256=record.sha256,
            )
        run_post_install_smoke(
            {component_id: str(path) for component_id, path in managed_paths.items()},
            runner=runner,
        )
        component_state.write_installed_state(current_state_path, previous_state)
        component_state.write_installed_state(previous_state_path, current_state)
    except (ComponentInstallError, OSError, ValueError) as exc:
        try:
            _restore_managed_executables(managed_snapshots)
            _restore_managed_executables(state_snapshots)
        except (ComponentInstallError, OSError) as restore_exc:
            raise ComponentInstallError(f"{exc}; failed to restore rollback transaction: {restore_exc}") from exc
        raise
    return 0


def setup_native_components(
    *,
    dry_run: bool = False,
    offline: bool = False,
    rollback: bool = False,
    env: Mapping[str, str] | None = None,
    opener: Callable[..., Any] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> int:
    """Install pinned native components; return process exit code."""
    try:
        if dry_run and rollback:
            raise ComponentInstallError("--dry-run and --rollback cannot be used together")
        if rollback:
            return _setup_rollback(env=env, runner=runner)
        manifest = component_manifest.load()
        if manifest.brigade_version != brigade.__version__:
            raise ComponentInstallError(
                "brigade_version mismatch: "
                f"manifest requires {manifest.brigade_version!r}, "
                f"running Brigade {brigade.__version__!r}"
            )
        if dry_run:
            return _setup_dry_run(manifest, env=env)

        roots = resolve_roots(env=env)
        platform = component_manifest.platform_key()
        plan = build_setup_plan(manifest, platform=platform, roots=roots)
        assets = [entry for entry in plan if entry.action == "verify-cache"]
        current_state_path = Path(component_paths.installed_state_path(roots.data_root))
        previous_state_path = Path(component_paths.installed_previous_state_path(roots.data_root))
        current_state = component_state.load_installed_state(current_state_path)
        if current_state_path.exists() and current_state is None:
            raise ComponentInstallError(f"invalid installed state: {current_state_path}")

        for entry in assets:
            asset = manifest.components[entry.component_id].assets[platform]
            fetch_asset_to_cache(
                asset,
                cache_path=Path(entry.cache_path),
                offline=offline,
                opener=opener,
            )

        managed_paths = {entry.component_id: Path(entry.managed_path) for entry in assets}
        snapshots = _snapshot_managed_executables(list(managed_paths.values()))
        state_snapshots = _snapshot_managed_executables([current_state_path, previous_state_path])
        try:
            for entry in assets:
                managed_path = managed_paths[entry.component_id]
                if not _managed_executable_is_ready(
                    managed_path,
                    byte_size=entry.byte_size,
                    sha256=entry.sha256,
                ):
                    verify_cached_asset(
                        Path(entry.cache_path),
                        byte_size=entry.byte_size,
                        sha256=entry.sha256,
                    )
                    materialize_executable(
                        cache_path=Path(entry.cache_path),
                        managed_path=managed_path,
                    )

            for entry in assets:
                verify_cached_asset(
                    managed_paths[entry.component_id],
                    byte_size=entry.byte_size,
                    sha256=entry.sha256,
                )

            run_post_install_smoke(
                {component_id: str(path) for component_id, path in managed_paths.items()},
                runner=runner,
            )
            next_state = component_state.InstalledState(
                schema_version=component_state.SCHEMA_VERSION,
                brigade_version=brigade.__version__,
                manifest_revision=manifest.manifest_revision,
                platform=platform,
                installed_at=localio.utc_now_iso(),
                components={
                    entry.component_id: component_state.InstalledComponentRecord(
                        component_revision=entry.component_revision,
                        asset_name=entry.asset_name,
                        byte_size=entry.byte_size,
                        sha256=entry.sha256,
                        download_url=entry.download_url,
                        executable=str(managed_paths[entry.component_id]),
                    )
                    for entry in assets
                },
            )
            if current_state is not None and component_state.should_rotate_previous(current_state, next_state):
                component_state.write_installed_state(previous_state_path, current_state)
            component_state.write_installed_state(current_state_path, next_state)
        except (ComponentInstallError, OSError, ValueError, urllib.error.URLError) as exc:
            try:
                _restore_managed_executables(snapshots)
                _restore_managed_executables(state_snapshots)
            except (ComponentInstallError, OSError) as restore_exc:
                raise ComponentInstallError(f"{exc}; failed to restore prior managed files: {restore_exc}") from exc
            raise
    except (ComponentInstallError, OSError, ValueError, urllib.error.URLError) as exc:
        print(f"component setup: {exc}", file=sys.stderr)
        return 1
    return 0
