"""Download, verify, cache, and install pinned native Brigade components."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import ssl
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
from brigade import component_manifest, component_paths, component_state, localio, templates

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_READ_CHUNK = 1024 * 1024
_EXECUTABLE_MODE = 0o755
_SMOKE_COMPONENT_IDS = component_manifest.KNOWN_COMPONENT_IDS
_SMOKE_TIMEOUT_SECONDS = 30.0
_DOWNLOAD_TIMEOUT_SECONDS = 30.0
_WINDOWS_HTTPS_PRIME_TIMEOUT_SECONDS = 30.0
_WINDOWS_HTTPS_PRIME_URL_ENV = "BRIGADE_HTTPS_PRIME_URL"
_WINDOWS_HTTPS_PRIME_COMMAND = (
    "Invoke-WebRequest -Uri $env:BRIGADE_HTTPS_PRIME_URL -Method Head -UseBasicParsing | Out-Null"
)
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


class ExactReleaseManifestError(RuntimeError):
    """Raised when matching update state cannot provide a verified manifest."""

    def __init__(self, message: str, manifest_path: Path) -> None:
        super().__init__(message)
        self.manifest_path = manifest_path


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


def uses_bundled_compatibility_manifest() -> bool:
    """Return whether automatic manifest selection starts at the bundled fallback."""
    bundled_path = templates.template_root() / "components" / "manifest-v1.json"
    return component_manifest.manifest_path() == bundled_path


def load_verified_exact_release_manifest(
    roots: SetupRoots,
    *,
    online_stable_cache_repair: bool = False,
) -> tuple[component_manifest.ComponentManifest, Path] | None:
    """Read the current release manifest recorded in update state without mutating it.

    For ``stable`` state the cached manifest must pin the exact running
    Brigade version (``state.component_tag == v{brigade.__version__}``); any
    other stable state is treated as stale and returns ``None`` so the caller
    re-resolves the release (online) or fails (offline).

    For ``beta`` state the update transaction persisted the verified
    last-stable release manifest coordinates. A later ``brigade setup``
    reuses that stable cache instead of resolving an unreleased
    ``v{brigade.__version__}`` tag (which fails online) or rejecting the
    state as stale (which fails offline). The beta path validates the cached
    manifest against its recorded stable ``component_tag``/version with
    compatible missing-unpublished handling enabled, so a pre-agent-notify
    stable manifest is accepted while every published component still pins.

    Arbitrary state cannot opt in: :func:`update_cmd.load_update_state`
    already validates the channel/state coordinates (channel in
    {stable, beta}, semver ``component_tag``, 40-hex ``component_target_commit``,
    exact release ``component_manifest_url`` matching ``component_tag``,
    64-hex ``component_manifest_sha256``, ISO ``updated_at``); the digest
    check below validates provenance, and ``validate_release_manifest_bytes``
    validates the stable tag/version against the cached manifest.

    When ``online_stable_cache_repair`` is set and the persisted state is a
    matching stable state (``state.component_tag == v{brigade.__version__}``),
    a missing or corrupt verified cache returns ``None`` instead of raising
    so an online ``brigade setup`` falls through to exact-release resolution
    and repairs the cache. A corrupt cache entry (unreadable, digest
    mismatch, or invalid manifest bytes) is removed in this mode so the
    caller's re-cache write is not blocked by the stale corrupt file; this
    only touches a file whose content already fails the recorded-digest
    provenance check. This only relaxes the matching-stable path: offline
    stable setup and every beta path stay fail-closed, because neither can
    repair the cache and a beta handoff cannot re-resolve an unreleased tag.
    """
    from brigade import update_cmd

    state_path = Path(component_paths.update_state_path(roots.data_root))
    state = update_cmd.load_update_state(state_path)
    if state is None:
        return None

    if state.channel == "stable":
        if state.component_tag != f"v{brigade.__version__}":
            return None
        compatible = False
        release_version = brigade.__version__
    elif state.channel == "beta":
        stable_version = update_cmd._parse_tag(state.component_tag)
        if stable_version is None:
            return None
        compatible = True
        release_version = stable_version
    else:
        return None

    cached = Path(component_paths.verified_manifest_path(roots.cache_root, state.component_manifest_sha256))
    # Online stable setup tolerates a missing/corrupt verified cache so the
    # caller can re-resolve the exact release and repair it. Offline stable
    # and all beta paths remain fail-closed: neither can repair the cache,
    # and a beta handoff cannot re-resolve an unreleased v{cli} tag. In repair
    # mode a corrupt entry is removed so the caller's re-cache write is not
    # blocked by a stale file that already fails the recorded-digest check.
    tolerate_cache_repair = online_stable_cache_repair and state.channel == "stable"

    def _drop_corrupt_cache() -> None:
        try:
            cached.unlink(missing_ok=True)
        except OSError:
            pass

    if not cached.is_file():
        if tolerate_cache_repair:
            return None
        raise ExactReleaseManifestError("cached exact-release manifest is missing", cached)
    try:
        cached_bytes = cached.read_bytes()
    except OSError as exc:
        if tolerate_cache_repair:
            _drop_corrupt_cache()
            return None
        raise ExactReleaseManifestError(f"cached exact-release manifest cannot be read: {exc}", cached) from exc
    if hashlib.sha256(cached_bytes).hexdigest() != state.component_manifest_sha256:
        if tolerate_cache_repair:
            _drop_corrupt_cache()
            return None
        raise ExactReleaseManifestError("cached exact-release manifest digest does not match update state", cached)

    release = update_cmd.ResolvedRelease(
        state.component_release_id,
        state.component_tag,
        release_version,
        state.component_target_commit,
        state.component_manifest_url,
        len(cached_bytes),
        state.component_manifest_sha256,
        cached_bytes,
    )
    try:
        if compatible:
            manifest = update_cmd.validate_release_manifest_bytes(release, allow_compatible_stable_manifest=True)
        else:
            manifest = update_cmd.validate_release_manifest_bytes(release)
    except update_cmd.UpdateError as exc:
        if tolerate_cache_repair:
            _drop_corrupt_cache()
            return None
        raise ExactReleaseManifestError(str(exc), cached) from exc
    except ValueError as exc:
        if tolerate_cache_repair:
            _drop_corrupt_cache()
            return None
        raise ExactReleaseManifestError(f"cached exact-release manifest is invalid: {exc}", cached) from exc
    return manifest, cached


def build_setup_plan(
    manifest: component_manifest.ComponentManifest,
    *,
    platform: str,
    roots: SetupRoots,
) -> list[SetupPlanAction]:
    """Build a deterministic dry-run/install plan for every published component."""
    plan: list[SetupPlanAction] = []
    for component_id in component_manifest.published_component_ids(manifest):
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


def _is_certificate_verify_failure(exc: BaseException) -> bool:
    reason: BaseException | object = exc
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
    if isinstance(reason, ssl.CertificateError):
        return True
    message = str(exc)
    if isinstance(reason, BaseException):
        message = f"{message} {reason}"
    return "CERTIFICATE_VERIFY_FAILED" in message.upper()


def _windows_powershell_exe() -> str:
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    candidate = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if candidate.is_file():
        return str(candidate)
    found = shutil.which("powershell.exe") or shutil.which("powershell")
    return found if found is not None else "powershell.exe"


def _prime_windows_https_roots(url: str, *, timeout: float) -> bool:
    """Run a bounded native Windows HTTPS HEAD request to refresh trusted roots."""
    environment = os.environ.copy()
    environment[_WINDOWS_HTTPS_PRIME_URL_ENV] = url
    try:
        completed = subprocess.run(
            [
                _windows_powershell_exe(),
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                _WINDOWS_HTTPS_PRIME_COMMAND,
            ],
            shell=False,
            capture_output=True,
            timeout=timeout,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _default_urlopen(url: str) -> Any:
    return urllib.request.urlopen(url, timeout=_DOWNLOAD_TIMEOUT_SECONDS)


def _urlopen_default_with_windows_cert_retry(url: str) -> Any:
    try:
        return _default_urlopen(url)
    except urllib.error.URLError as exc:
        if not _is_certificate_verify_failure(exc):
            raise
        if not _prime_windows_https_roots(url, timeout=_WINDOWS_HTTPS_PRIME_TIMEOUT_SECONDS):
            raise
        return _default_urlopen(url)


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
        if sys.platform == "win32":
            return _urlopen_default_with_windows_cert_retry(url)
        return _default_urlopen(url)

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


def _validate_smoke_managed_paths(
    managed_paths: Mapping[str, str],
    *,
    expected_components: Sequence[str] = _SMOKE_COMPONENT_IDS,
) -> dict[str, Path]:
    keys = set(managed_paths)
    expected = set(expected_components)
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
    for component_id in expected_components:
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


def _sessionfind_help_is_valid(stdout: str, stderr: str) -> bool:
    combined = f"{stdout}{stderr}"
    if "usage" in combined.lower():
        return True
    return any(line.strip().startswith("sessionfind ") for line in stdout.splitlines())


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

    if completed.returncode != 0:
        raise ComponentInstallError(
            f"sessionfind smoke failed: {path} --help exited {completed.returncode}, expected 0"
        )
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    if not _sessionfind_help_is_valid(stdout, stderr):
        raise ComponentInstallError(f"sessionfind smoke failed: {path} --help produced no help text")


def _smoke_agent_notify(
    path: Path,
    run: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    argv = [str(path), "version", "--json"]
    try:
        completed = _invoke_smoke_runner(run, argv)
    except subprocess.TimeoutExpired as exc:
        raise ComponentInstallError(f"agent-notify smoke timed out after {_SMOKE_TIMEOUT_SECONDS}s") from exc
    except OSError as exc:
        raise ComponentInstallError(f"agent-notify smoke failed to run {path}: {exc}") from exc

    if completed.returncode != 0:
        raise ComponentInstallError(f"agent-notify smoke failed: {path} version --json exited {completed.returncode}")
    stdout = (completed.stdout or "").strip()
    if not stdout:
        raise ComponentInstallError(f"agent-notify smoke failed: {path} version --json produced empty stdout")
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ComponentInstallError(f"agent-notify smoke failed: {path} returned malformed JSON") from exc
    if not isinstance(payload, dict) or not payload.get("version"):
        raise ComponentInstallError(f"agent-notify smoke failed: {path} JSON missing version field")


_SMOKE_DISPATCH: dict[str, Callable[[Path, Callable[..., subprocess.CompletedProcess[str]]], None]] = {
    "agent-notify": _smoke_agent_notify,
    "graphtrail": _smoke_graphtrail,
    "graphtrail-mcp": _smoke_graphtrail_mcp,
    "miseledger": _smoke_miseledger,
    "sessionfind": _smoke_sessionfind,
}


def run_post_install_smoke(
    managed_paths: Mapping[str, str],
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    expected_components: Sequence[str] = _SMOKE_COMPONENT_IDS,
) -> None:
    """Run post-install smoke checks using only absolute managed executable paths."""
    paths = _validate_smoke_managed_paths(managed_paths, expected_components=expected_components)
    run = runner if runner is not None else _default_smoke_runner

    for component_id in expected_components:
        _SMOKE_DISPATCH[component_id](paths[component_id], run)


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
    if state.platform != platform:
        raise ComponentInstallError(
            f"{label} installed state platform {state.platform!r} does not match host {platform!r}"
        )
    return state


def _ordered_component_ids(state: component_state.InstalledState) -> tuple[str, ...]:
    """Return the state's component ids in :data:`KNOWN_COMPONENT_IDS` order."""
    present = set(state.components)
    return tuple(cid for cid in component_manifest.KNOWN_COMPONENT_IDS if cid in present)


def _validate_rollback_component_sets(
    current: component_state.InstalledState,
    previous: component_state.InstalledState,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return ``(restore_ids, remove_ids)`` for a rollback transaction.

    Component sets may differ: a four-component prior install upgraded with
    agent-notify rolls back by restoring the previous set and removing only
    managed binaries introduced by the current transaction.
    """
    current_ids = set(current.components)
    previous_ids = set(previous.components)
    known = set(component_manifest.KNOWN_COMPONENT_IDS)
    if not previous_ids or not previous_ids <= known:
        raise ComponentInstallError("previous installed state lists unknown or no components; cannot roll back")
    if not current_ids or not current_ids <= known:
        raise ComponentInstallError("current installed state lists unknown or no components; cannot roll back")
    restore_ids = _ordered_component_ids(previous)
    remove_ids = tuple(
        component_id
        for component_id in component_manifest.KNOWN_COMPONENT_IDS
        if component_id in current_ids and component_id not in previous_ids
    )
    return restore_ids, remove_ids


def _managed_binary_belongs_to_transaction(
    path: Path,
    record: component_state.InstalledComponentRecord,
) -> bool:
    """Return True when ``path`` is the managed binary from ``record``."""
    if Path(record.executable) != path:
        return False
    if not path.is_file():
        return False
    try:
        verify_cached_asset(path, byte_size=record.byte_size, sha256=record.sha256)
    except ComponentInstallError:
        return False
    return True


def _remove_transaction_managed_binaries(
    *,
    data_root: str,
    current: component_state.InstalledState,
    remove_ids: Sequence[str],
) -> None:
    """Remove managed binaries introduced by the current install transaction.

    Only deletes the managed path under the user data root when it matches the
    current installed record. External PATH or env-override binaries are never
    touched.
    """
    for component_id in remove_ids:
        record = current.components[component_id]
        managed_path = Path(component_paths.managed_executable_path(data_root, component_id))
        if not _managed_binary_belongs_to_transaction(managed_path, record):
            continue
        managed_path.unlink()


def _rollback_cache_paths(
    state: component_state.InstalledState,
    *,
    cache_root: str,
    component_ids: Sequence[str],
) -> dict[str, Path]:
    cache_paths: dict[str, Path] = {}
    for component_id in component_ids:
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
    restore_ids, remove_ids = _validate_rollback_component_sets(current_state, previous_state)
    cache_paths = _rollback_cache_paths(previous_state, cache_root=roots.cache_root, component_ids=restore_ids)

    for component_id in restore_ids:
        record = previous_state.components[component_id]
        verify_cached_asset(
            cache_paths[component_id],
            byte_size=record.byte_size,
            sha256=record.sha256,
        )

    managed_paths = {
        component_id: Path(component_paths.managed_executable_path(roots.data_root, component_id))
        for component_id in restore_ids
    }
    remove_paths = [
        Path(component_paths.managed_executable_path(roots.data_root, component_id)) for component_id in remove_ids
    ]
    managed_snapshots = _snapshot_managed_executables([*managed_paths.values(), *remove_paths])
    state_snapshots = _snapshot_managed_executables([current_state_path, previous_state_path])
    try:
        for component_id in restore_ids:
            materialize_executable(
                cache_path=cache_paths[component_id],
                managed_path=managed_paths[component_id],
            )
        for component_id in restore_ids:
            record = previous_state.components[component_id]
            verify_cached_asset(
                managed_paths[component_id],
                byte_size=record.byte_size,
                sha256=record.sha256,
            )
        run_post_install_smoke(
            {component_id: str(path) for component_id, path in managed_paths.items()},
            runner=runner,
            expected_components=restore_ids,
        )
        _remove_transaction_managed_binaries(
            data_root=roots.data_root,
            current=current_state,
            remove_ids=remove_ids,
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


def _load_setup_manifest(
    *,
    manifest_path: Path | None,
    manifest_source: str,
    offline: bool,
    opener: Callable[..., Any] | None,
    env: Mapping[str, str] | None,
    allow_compatible_stable_manifest: bool = False,
) -> tuple[component_manifest.ComponentManifest, Any | None, str | None]:
    """Load an exact release manifest, with one standalone compatibility fallback.

    Returns ``(manifest, auto_release, auto_compatible_stable_version)``.
    ``auto_release`` is the resolved release when the auto branch re-resolved
    it online (used to publish state); ``None`` when a persisted verified
    cache was reused or an explicit/standalone manifest was loaded.
    ``auto_compatible_stable_version`` is the manifest's ``brigade_version``
    when the auto branch reused a persisted beta-handoff cache whose stable
    version predates the running Brigade version, so the caller can treat that
    reuse as the narrowly named compatible-stable-manifest path; ``None``
    otherwise (exact-release stable path stays strict).

    The narrowly named ``allow_compatible_stable_manifest`` mode only applies
    to the ``--manifest`` branch used by the beta handoff, which reuses the
    verified last-stable release manifest. The auto branch resolves the exact
    release for the installed Brigade version and stays strict, except that a
    persisted beta-handoff state is the explicit compatible-stable-manifest
    path and is reused rather than re-resolved.
    """
    if manifest_path is not None:
        return (
            component_manifest.load(
                manifest_path,
                allow_compatible_stable_manifest=allow_compatible_stable_manifest,
            ),
            None,
            None,
        )
    if manifest_source == "standalone":
        return component_manifest.load(allow_standalone_legacy_revisions=True), None, None
    if manifest_source != "auto":
        raise ComponentInstallError("manifest source must be auto or standalone")

    if not uses_bundled_compatibility_manifest():
        return component_manifest.load(), None, None

    from brigade import update_cmd

    roots = resolve_roots(env=env)
    try:
        # Persisted update state supplies a verified manifest cache that
        # ``brigade setup`` reuses instead of re-resolving the release. Online
        # stable setup tolerates a missing/corrupt cache so it can fall through
        # to exact-release resolution and repair it; offline stable and all
        # beta paths stay fail-closed.
        cached_manifest = load_verified_exact_release_manifest(roots, online_stable_cache_repair=not offline)
        if offline:
            # Offline setup must reuse any verified cache: the exact-release
            # stable cache (state.component_tag == v{brigade.__version__}) or
            # the beta handoff cache, which persists the verified last-stable
            # release manifest coordinates and would otherwise fail offline.
            if cached_manifest is not None:
                cached_manifest_obj, _cached_path = cached_manifest
                auto_compatible = (
                    cached_manifest_obj.brigade_version
                    if cached_manifest_obj.brigade_version != brigade.__version__
                    else None
                )
                return cached_manifest_obj, None, auto_compatible
            raise ComponentInstallError("offline setup requires a verified exact-release manifest cache")

        # Online setup reuses the cache only for a persisted beta handoff,
        # whose stable manifest tag predates the running Brigade version and
        # would otherwise re-resolve an unreleased v{brigade.__version__} tag.
        # Stable state retains exact current-version behavior: it re-resolves
        # the exact current release as before.
        if cached_manifest is not None:
            state = update_cmd.load_update_state(Path(component_paths.update_state_path(roots.data_root)))
            if state is not None and state.channel == "beta":
                cached_manifest_obj, _cached_path = cached_manifest
                return cached_manifest_obj, None, cached_manifest_obj.brigade_version

        release = update_cmd.resolve_release(update_cmd._DefaultHttp(), latest=False, tag=f"v{brigade.__version__}")
        update_cmd._cache_manifest(
            update_cmd.UpdatePaths(Path(roots.data_root), Path(roots.cache_root), Path("unused")), release
        )
        return update_cmd.validate_release_manifest_bytes(release), release, None
    except (update_cmd.UpdateError, ExactReleaseManifestError, ValueError) as exc:
        raise ComponentInstallError(f"exact release manifest setup failed: {exc}") from exc


def _publish_auto_setup_release_state(*, roots: SetupRoots, release: Any) -> None:
    """Publish only auto-resolved component coordinates after setup commits."""
    from brigade import update_cmd

    state_path = Path(component_paths.update_state_path(roots.data_root))
    current = update_cmd.load_update_state(state_path)
    if current is None:
        channel = "stable"
        owner = "brigade setup"
        cli_coordinate = release.version
    else:
        channel = current.channel
        owner = current.owner
        cli_coordinate = current.cli_coordinate
    update_cmd.write_update_state(
        state_path,
        update_cmd.UpdateState(
            update_cmd.STATE_SCHEMA_VERSION,
            channel,
            owner,
            cli_coordinate,
            release.release_id,
            release.tag,
            release.target_commit,
            release.manifest_url,
            release.manifest_sha256,
            localio.utc_now_iso(),
        ),
    )


def setup_native_components(
    *,
    dry_run: bool = False,
    offline: bool = False,
    rollback: bool = False,
    env: Mapping[str, str] | None = None,
    opener: Callable[..., Any] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    manifest_path: Path | None = None,
    manifest_source: str = "auto",
    allow_compatible_stable_manifest: str | None = None,
) -> int:
    """Install pinned native components; return process exit code."""
    try:
        if dry_run and rollback:
            raise ComponentInstallError("--dry-run and --rollback cannot be used together")
        if allow_compatible_stable_manifest is not None and manifest_path is None:
            raise ComponentInstallError("--allow-compatible-stable-manifest requires --manifest")
        if rollback:
            return _setup_rollback(env=env, runner=runner)
        compatible_stable_manifest_requested = allow_compatible_stable_manifest is not None
        manifest, auto_release, auto_compatible_stable_version = _load_setup_manifest(
            manifest_path=manifest_path,
            manifest_source=manifest_source,
            offline=offline,
            opener=opener,
            env=env,
            allow_compatible_stable_manifest=compatible_stable_manifest_requested,
        )
        # The explicit --allow-compatible-stable-manifest flag (beta handoff
        # via --manifest) and the auto branch's reuse of a persisted beta
        # cache both count as the narrowly named compatible-stable-manifest
        # path; either way the manifest's brigade_version must match the
        # recorded stable version, not the running Brigade version.
        compatible_stable_version = allow_compatible_stable_manifest
        if auto_compatible_stable_version is not None:
            compatible_stable_version = auto_compatible_stable_version
        compatible_stable_manifest = (
            compatible_stable_version is not None and compatible_stable_version == manifest.brigade_version
        )
        if manifest.brigade_version != brigade.__version__ and not compatible_stable_manifest:
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
        state_paths = [current_state_path, previous_state_path]
        if auto_release is not None:
            state_paths.append(Path(component_paths.update_state_path(roots.data_root)))
        state_snapshots = _snapshot_managed_executables(state_paths)
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
                expected_components=component_manifest.published_component_ids(manifest),
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
            if auto_release is not None:
                _publish_auto_setup_release_state(roots=roots, release=auto_release)
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
