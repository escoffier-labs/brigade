"""Immutable, user-global update transactions for the Brigade CLI."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from brigade import component_manifest, component_paths, localio

REPOSITORY = "escoffier-labs/brigade"
STATE_SCHEMA_VERSION = 1
MANIFEST_ASSET = "component-manifest-v1.json"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_DIGEST = re.compile(r"^sha256:([0-9a-f]{64})$")
_CHECK_RUN_PAGE_SIZE = 100
_MAX_CHECK_RUN_PAGES = 100
MAX_TAG_DEREFERENCE_DEPTH = 5
_GITHUB_RELEASE_CDN_HOSTS = frozenset({"release-assets.githubusercontent.com", "objects.githubusercontent.com"})
_STATE_KEYS = frozenset(
    {
        "schema_version",
        "channel",
        "owner",
        "cli_coordinate",
        "component_release_id",
        "component_tag",
        "component_target_commit",
        "component_manifest_url",
        "component_manifest_sha256",
        "updated_at",
    }
)


class UpdateError(RuntimeError):
    """Raised when an update cannot safely complete."""


class UpdateHttp(Protocol):
    def json(self, url: str) -> Any: ...

    def bytes(self, url: str) -> bytes: ...


@dataclass(frozen=True)
class UpdatePaths:
    data_root: Path
    cache_root: Path
    brigade_executable: Path
    pipx_executable: str = "pipx"


@dataclass(frozen=True)
class UpdateState:
    schema_version: int
    channel: str
    owner: str
    cli_coordinate: str
    component_release_id: int
    component_tag: str
    component_target_commit: str
    component_manifest_url: str
    component_manifest_sha256: str
    updated_at: str


@dataclass(frozen=True)
class ResolvedRelease:
    release_id: int
    tag: str
    version: str
    target_commit: str
    manifest_url: str
    manifest_size: int
    manifest_sha256: str
    manifest_bytes: bytes


class _DefaultHttp:
    def _read(
        self,
        url: str,
        *,
        final_url_is_allowed: Callable[[str], bool],
        redirect_error: str,
    ) -> bytes:
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "brigade-update/1"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                final = response.geturl()
                if not final_url_is_allowed(final):
                    raise UpdateError(f"{redirect_error}: {final}")
                return response.read()
        except urllib.error.HTTPError as exc:
            raise UpdateError(f"GitHub request failed: {url} HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise UpdateError(f"GitHub request failed: {url}") from exc

    def json(self, url: str) -> Any:
        if not _is_github_api_url(url):
            raise UpdateError(f"GitHub API request is not an expected API URL: {url}")
        try:
            return json.loads(
                self._read(
                    url,
                    final_url_is_allowed=lambda final: final == url and _is_github_api_url(final),
                    redirect_error="GitHub API request redirected outside its exact expected URL",
                ).decode("utf-8")
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UpdateError(f"GitHub request returned invalid JSON: {url}") from exc

    def bytes(self, url: str) -> bytes:
        if not _is_manifest_release_url(url):
            raise UpdateError(f"GitHub release asset is not an exact manifest URL: {url}")
        return self._read(
            url,
            final_url_is_allowed=lambda final: _is_release_asset_redirect(final, original=url),
            redirect_error="GitHub release asset redirected outside trusted HTTPS CDN hosts",
        )


def default_paths() -> UpdatePaths:
    roots = component_paths.data_root()
    cache = component_paths.cache_root()
    bin_dir = Path(os.environ.get("PIPX_BIN_DIR", str(Path.home() / ".local" / "bin")))
    executable = bin_dir / ("brigade.exe" if os.name == "nt" else "brigade")
    return UpdatePaths(data_root=Path(roots), cache_root=Path(cache), brigade_executable=executable)


def load_update_state(path: Path) -> UpdateState | None:
    payload = localio.read_json_dict(path)
    if payload is None or set(payload) != _STATE_KEYS:
        return None
    if payload["schema_version"] != STATE_SCHEMA_VERSION or payload["channel"] not in {"stable", "beta"}:
        return None
    fields = (
        "owner",
        "cli_coordinate",
        "component_tag",
        "component_target_commit",
        "component_manifest_url",
        "component_manifest_sha256",
        "updated_at",
    )
    if any(not isinstance(payload[field], str) or not payload[field].strip() for field in fields):
        return None
    if not isinstance(payload["component_release_id"], int) or isinstance(payload["component_release_id"], bool):
        return None
    if not _SHA256.fullmatch(payload["component_manifest_sha256"]):
        return None
    if not _SHA.fullmatch(payload["component_target_commit"]):
        return None
    if _parse_tag(payload["component_tag"]) is None or not _is_release_url(
        payload["component_manifest_url"], payload["component_tag"], MANIFEST_ASSET
    ):
        return None
    if localio.parse_iso_datetime(payload["updated_at"]) is None:
        return None
    return UpdateState(**payload)


def write_update_state(path: Path, state: UpdateState) -> None:
    if state.schema_version != STATE_SCHEMA_VERSION:
        raise UpdateError("unsupported update state schema")
    localio.write_json(
        path,
        {
            "schema_version": state.schema_version,
            "channel": state.channel,
            "owner": state.owner,
            "cli_coordinate": state.cli_coordinate,
            "component_release_id": state.component_release_id,
            "component_tag": state.component_tag,
            "component_target_commit": state.component_target_commit,
            "component_manifest_url": state.component_manifest_url,
            "component_manifest_sha256": state.component_manifest_sha256,
            "updated_at": state.updated_at,
        },
    )


def ensure_channel_ownership(state: UpdateState | None, channel: str, *, switch_channel: bool) -> None:
    if state is not None and state.channel != channel and not switch_channel:
        raise UpdateError(
            f"update state is owned by {state.channel!r}; pass --switch-channel to transfer ownership to {channel!r}"
        )


def _parse_tag(tag: Any) -> str | None:
    if not isinstance(tag, str) or not tag.startswith("v"):
        return None
    version = tag[1:]
    return version if _SEMVER.fullmatch(version) else None


def _is_release_url(url: Any, tag: str, name: str) -> bool:
    if not isinstance(url, str):
        return False
    parsed = urlparse(url)
    return (
        parsed.scheme == "https"
        and parsed.netloc == "github.com"
        and not parsed.query
        and not parsed.fragment
        and parsed.path == f"/{REPOSITORY}/releases/download/{tag}/{name}"
    )


def _is_manifest_release_url(url: Any) -> bool:
    if not isinstance(url, str):
        return False
    parsed = urlparse(url)
    parts = parsed.path.split("/")
    return len(parts) == 7 and _is_release_url(url, parts[-2], MANIFEST_ASSET)


def _is_github_api_url(url: Any) -> bool:
    if not isinstance(url, str):
        return False
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "api.github.com"
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        return False
    prefix = f"/repos/{REPOSITORY}/"
    if parsed.path in {f"{prefix}releases/latest", f"{prefix}commits/main"}:
        return not parsed.query
    tag = parsed.path.removeprefix(f"{prefix}releases/tags/")
    if parsed.path == f"{prefix}releases/tags/{tag}" and _parse_tag(tag) is not None:
        return not parsed.query
    tag_ref = parsed.path.removeprefix(f"{prefix}git/ref/tags/")
    if parsed.path == f"{prefix}git/ref/tags/{tag_ref}" and _parse_tag(tag_ref) is not None:
        return not parsed.query
    tag_object = parsed.path.removeprefix(f"{prefix}git/tags/")
    if parsed.path == f"{prefix}git/tags/{tag_object}" and _SHA.fullmatch(tag_object) is not None:
        return not parsed.query
    check_prefix = f"{prefix}commits/"
    if not parsed.path.startswith(check_prefix) or not parsed.path.endswith("/check-runs"):
        return False
    sha = parsed.path[len(check_prefix) : -len("/check-runs")]
    if not _SHA.fullmatch(sha):
        return False
    return (
        parsed.query.startswith(f"per_page={_CHECK_RUN_PAGE_SIZE}&page=")
        and parsed.query.removeprefix(f"per_page={_CHECK_RUN_PAGE_SIZE}&page=").isdigit()
        and int(parsed.query.removeprefix(f"per_page={_CHECK_RUN_PAGE_SIZE}&page=")) > 0
    )


def _is_release_asset_redirect(final_url: str, *, original: str) -> bool:
    if final_url == original:
        return True
    parsed = urlparse(final_url)
    hostname = parsed.hostname
    return (
        parsed.scheme == "https"
        and parsed.username is None
        and parsed.password is None
        and hostname is not None
        and (hostname in _GITHUB_RELEASE_CDN_HOSTS or hostname.endswith(".githubusercontent.com"))
    )


def _asset(release: dict[str, Any]) -> dict[str, Any]:
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise UpdateError("GitHub release is missing its assets array")
    found = [asset for asset in assets if isinstance(asset, dict) and asset.get("name") == MANIFEST_ASSET]
    if len(found) != 1:
        raise UpdateError(f"GitHub release must contain exactly one {MANIFEST_ASSET}")
    return found[0]


def _tag_ref_url(tag: str) -> str:
    return f"https://api.github.com/repos/{REPOSITORY}/git/ref/tags/{tag}"


def _tag_object_url(sha: str) -> str:
    return f"https://api.github.com/repos/{REPOSITORY}/git/tags/{sha}"


def _tag_target(value: Any, *, source: str) -> tuple[str, str]:
    if not isinstance(value, dict):
        raise UpdateError(f"GitHub {source} must be an object")
    object_value = value.get("object")
    if not isinstance(object_value, dict):
        raise UpdateError(f"GitHub {source} is missing its object")
    object_type = object_value.get("type")
    sha = object_value.get("sha")
    if object_type not in {"commit", "tag"} or not isinstance(sha, str) or _SHA.fullmatch(sha) is None:
        raise UpdateError(f"GitHub {source} has an invalid object type or SHA")
    return object_type, sha


def resolve_tag_commit(http: UpdateHttp, tag: str) -> str:
    """Resolve an exact release tag to its immutable commit through Git refs."""
    if _parse_tag(tag) is None:
        raise UpdateError("GitHub release tag must be vX.Y.Z")
    ref = http.json(_tag_ref_url(tag))
    if not isinstance(ref, dict) or ref.get("ref") != f"refs/tags/{tag}":
        raise UpdateError(f"GitHub tag ref did not report exact tag refs/tags/{tag}")
    object_type, sha = _tag_target(ref, source="tag ref")
    seen: set[str] = set()
    depth = 0
    while object_type == "tag":
        if sha in seen:
            raise UpdateError("GitHub tag dereference detected a cycle")
        if depth >= MAX_TAG_DEREFERENCE_DEPTH:
            raise UpdateError("GitHub tag dereference exceeded the maximum depth")
        seen.add(sha)
        annotated = http.json(_tag_object_url(sha))
        if not isinstance(annotated, dict) or annotated.get("tag") != tag:
            raise UpdateError(f"GitHub tag object did not report exact tag {tag}")
        object_type, sha = _tag_target(annotated, source="tag object")
        depth += 1
    return sha


def resolve_release(http: UpdateHttp, *, latest: bool, tag: str | None = None) -> ResolvedRelease:
    if latest == (tag is not None):
        raise ValueError("choose exactly one release lookup mode")
    suffix = "latest" if latest else f"tags/{tag}"
    raw = http.json(f"https://api.github.com/repos/{REPOSITORY}/releases/{suffix}")
    if not isinstance(raw, dict) or raw.get("draft") is not False or raw.get("prerelease") is not False:
        raise UpdateError("GitHub release must be a published non-prerelease")
    version = _parse_tag(raw.get("tag_name"))
    if version is None:
        raise UpdateError("GitHub release tag must be vX.Y.Z")
    release_tag = raw["tag_name"]
    if tag is not None and release_tag != tag:
        raise UpdateError(f"GitHub release lookup did not return exact tag {tag}")
    release_id = raw.get("id")
    if not isinstance(release_id, int) or isinstance(release_id, bool):
        raise UpdateError("GitHub release is missing an immutable release ID")
    target_commit = resolve_tag_commit(http, release_tag)
    asset = _asset(raw)
    url = asset.get("browser_download_url")
    size = asset.get("size")
    digest = asset.get("digest")
    match = _DIGEST.fullmatch(digest) if isinstance(digest, str) else None
    if not isinstance(url, str) or not _is_release_url(url, release_tag, MANIFEST_ASSET):
        raise UpdateError("component manifest asset URL is not the exact Brigade release URL")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0 or match is None:
        raise UpdateError("component manifest asset is missing a valid size or SHA-256 release digest")
    content = http.bytes(url)
    if len(content) != size or hashlib.sha256(content).hexdigest() != match.group(1):
        raise UpdateError("component manifest asset size or SHA-256 verification failed")
    return ResolvedRelease(release_id, release_tag, version, target_commit, url, size, match.group(1), content)


def _validate_manifest_coordinates(
    release: ResolvedRelease, manifest: component_manifest.ComponentManifest
) -> component_manifest.ComponentManifest:
    if manifest.brigade_version != release.version:
        raise UpdateError("component manifest brigade_version does not match the resolved Brigade release")
    for component in manifest.components.values():
        if component.component_revision != release.target_commit:
            raise UpdateError("component manifest component_revision does not match the resolved release target commit")
        if component.source.repository != REPOSITORY or component.source.release_tag != release.tag:
            raise UpdateError("component manifest contains components outside the resolved Brigade release")
        for asset in component.assets.values():
            if not _is_release_url(asset.download_url, release.tag, asset.asset_name):
                raise UpdateError("component manifest contains a non-exact component release URL")
    return manifest


def validate_release_manifest(
    release: ResolvedRelease,
    path: Path,
    *,
    allow_compatible_stable_manifest: bool = False,
) -> component_manifest.ComponentManifest:
    return _validate_manifest_coordinates(
        release,
        component_manifest.load(path, allow_compatible_stable_manifest=allow_compatible_stable_manifest),
    )


def validate_release_manifest_bytes(
    release: ResolvedRelease,
    *,
    allow_compatible_stable_manifest: bool = False,
) -> component_manifest.ComponentManifest:
    return _validate_manifest_coordinates(
        release,
        component_manifest.load_bytes(
            release.manifest_bytes,
            source=Path(release.manifest_url),
            allow_compatible_stable_manifest=allow_compatible_stable_manifest,
        ),
    )


def _cache_manifest(paths: UpdatePaths, release: ResolvedRelease) -> Path:
    target = Path(component_paths.verified_manifest_path(str(paths.cache_root), release.manifest_sha256))
    if target.is_file():
        if target.read_bytes() == release.manifest_bytes:
            return target
        raise UpdateError(f"verified manifest cache digest collision: {target}")
    localio.write_bytes_atomic(target, release.manifest_bytes)
    return target


def _check_beta(http: UpdateHttp) -> str:
    commit = http.json(f"https://api.github.com/repos/{REPOSITORY}/commits/main")
    sha = commit.get("sha") if isinstance(commit, dict) else None
    if not isinstance(sha, str) or not _SHA.fullmatch(sha):
        raise UpdateError("GitHub main resolution did not return a full commit SHA")
    all_runs: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    total_count: int | None = None
    for page in range(1, _MAX_CHECK_RUN_PAGES + 1):
        checks = http.json(
            f"https://api.github.com/repos/{REPOSITORY}/commits/{sha}/check-runs?per_page={_CHECK_RUN_PAGE_SIZE}&page={page}"
        )
        page_total = checks.get("total_count") if isinstance(checks, dict) else None
        runs = checks.get("check_runs") if isinstance(checks, dict) else None
        if (
            not isinstance(page_total, int)
            or isinstance(page_total, bool)
            or page_total <= 0
            or not isinstance(runs, list)
        ):
            raise UpdateError("GitHub main check-runs payload is incomplete or malformed; beta fails closed")
        if total_count is None:
            total_count = page_total
            if total_count > _MAX_CHECK_RUN_PAGES * _CHECK_RUN_PAGE_SIZE:
                raise UpdateError("GitHub main has too many check runs to validate; beta fails closed")
        elif page_total != total_count:
            raise UpdateError("GitHub main check-runs pages disagree on total_count; beta fails closed")
        expected_count = min(_CHECK_RUN_PAGE_SIZE, total_count - len(all_runs))
        if len(runs) != expected_count:
            raise UpdateError("GitHub main check-runs page is incomplete; beta fails closed")
        for run in runs:
            run_id = run.get("id") if isinstance(run, dict) else None
            if (
                not isinstance(run_id, int)
                or isinstance(run_id, bool)
                or run_id <= 0
                or run_id in seen_ids
                or run.get("status") != "completed"
                or run.get("conclusion") not in {"success", "neutral", "skipped"}
            ):
                raise UpdateError("GitHub main has a non-successful or malformed check run; beta fails closed")
            seen_ids.add(run_id)
            all_runs.append(run)
        if len(all_runs) == total_count:
            return sha
    raise UpdateError("GitHub main check-runs pagination exceeded validation limit; beta fails closed")


class _UpdateLock:
    def __init__(self, path: Path, *, pid_alive: Callable[[int], bool] | None = None):
        self.path = path
        self.pid_alive = pid_alive or _pid_alive

    def __enter__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            stale_pid = _lock_pid(self.path)
            if stale_pid is not None and not self.pid_alive(stale_pid):
                try:
                    self.path.unlink()
                except OSError as exc:
                    raise UpdateError(f"another brigade update owns lock {self.path}") from exc
                return self.__enter__()
            raise UpdateError(f"another brigade update owns lock {self.path}") from None
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"pid": os.getpid(), "created_at": localio.utc_now_iso()}, handle)

    def __exit__(self, *_args: object) -> None:
        self.path.unlink(missing_ok=True)


def _lock_pid(path: Path) -> int | None:
    payload = localio.read_json_dict(path)
    value = payload.get("pid") if payload else None
    return value if isinstance(value, int) and value > 0 else None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _returncode(result: Any) -> int:
    return result if isinstance(result, int) else int(getattr(result, "returncode", 1))


def _same_coordinates(left: UpdateState, right: UpdateState) -> bool:
    """Compare durable coordinates only; timestamps intentionally do not update no-ops."""
    return (
        left.channel,
        left.owner,
        left.cli_coordinate,
        left.component_release_id,
        left.component_tag,
        left.component_target_commit,
        left.component_manifest_url,
        left.component_manifest_sha256,
    ) == (
        right.channel,
        right.owner,
        right.cli_coordinate,
        right.component_release_id,
        right.component_tag,
        right.component_target_commit,
        right.component_manifest_url,
        right.component_manifest_sha256,
    )


def _default_runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, check=False, text=True)


def run_update(
    *,
    channel: str = "stable",
    dry_run: bool = False,
    switch_channel: bool = False,
    paths: UpdatePaths | None = None,
    http: UpdateHttp | None = None,
    runner: Callable[[list[str]], Any] | None = None,
    now: Callable[[], str] = localio.utc_now_iso,
    pid_alive: Callable[[int], bool] | None = None,
) -> int:
    """Update CLI, then publish state after native component setup succeeds."""
    if channel not in {"stable", "beta"}:
        raise UpdateError("channel must be stable or beta")
    selected_paths = paths or default_paths()
    selected_http = http or _DefaultHttp()
    selected_runner = runner or _default_runner
    state_path = Path(component_paths.update_state_path(str(selected_paths.data_root)))
    lock_path = Path(component_paths.update_lock_path(str(selected_paths.data_root)))
    try:
        lock = nullcontext() if dry_run else _UpdateLock(lock_path, pid_alive=pid_alive)
        with lock:
            current = load_update_state(state_path)
            if state_path.exists() and current is None:
                raise UpdateError(f"invalid update state: {state_path}")
            ensure_channel_ownership(current, channel, switch_channel=switch_channel)
            release = resolve_release(selected_http, latest=True)
            manifest = validate_release_manifest_bytes(
                release,
                allow_compatible_stable_manifest=channel == "beta",
            )
            coordinate = release.version if channel == "stable" else _check_beta(selected_http)
            compatible_manifest_version = manifest.brigade_version if channel == "beta" else None
            if compatible_manifest_version is not None and compatible_manifest_version != release.version:
                raise UpdateError("beta stable manifest compatibility handoff is internally inconsistent")
            next_state = UpdateState(
                STATE_SCHEMA_VERSION,
                channel,
                "brigade update",
                coordinate,
                release.release_id,
                release.tag,
                release.target_commit,
                release.manifest_url,
                release.manifest_sha256,
                now(),
            )
            if current is not None and _same_coordinates(current, next_state):
                print(f"brigade update: {channel} already owns {coordinate}; no changes")
                return 0
            pipx_spec = (
                f"brigade-cli=={coordinate}"
                if channel == "stable"
                else f"git+https://github.com/{REPOSITORY}@{coordinate}"
            )
            cached_manifest_path = Path(
                component_paths.verified_manifest_path(str(selected_paths.cache_root), release.manifest_sha256)
            )
            setup_command = [str(selected_paths.brigade_executable), "setup", "--manifest", str(cached_manifest_path)]
            if compatible_manifest_version is not None:
                setup_command.extend(["--allow-compatible-stable-manifest", compatible_manifest_version])
            commands = [
                [selected_paths.pipx_executable, "install", "--force", pipx_spec],
                setup_command,
            ]
            if dry_run:
                for command in commands:
                    print(" ".join(command))
                return 0
            manifest_path = _cache_manifest(selected_paths, release)
            if manifest_path != cached_manifest_path:
                raise UpdateError("verified manifest cache path changed unexpectedly")
            for command in commands:
                if _returncode(selected_runner(command)) != 0:
                    raise UpdateError(f"update command failed: {' '.join(command)}")
            write_update_state(state_path, next_state)
    except (UpdateError, OSError, UnicodeDecodeError, ValueError) as exc:
        print(f"brigade update: {exc}", file=sys.stderr)
        return 1
    return 0
