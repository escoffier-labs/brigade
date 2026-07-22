#!/usr/bin/env python3
"""Accept a published Brigade artifact on Unix GitHub-hosted runners."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


COMPONENT_IDS = ("agent-notify", "graphtrail", "graphtrail-mcp", "miseledger", "sessionfind")
SUPPORTED_PLATFORMS = ("linux-amd64", "linux-arm64", "darwin-amd64", "darwin-arm64", "windows-amd64")
REPOSITORY = "escoffier-labs/brigade"
PYPI_PROJECT_URL = "https://pypi.org/pypi/brigade-cli/json"
PYPI_AVAILABILITY_TIMEOUT_SECONDS = 6 * 60
PYPI_POLL_INTERVAL_SECONDS = 5
# agent-notify ldflags inject main.version, main.commit, and main.buildDate.
# A bare `go build` leaves "dev" / "unknown" / "unknown"; published release
# assets must report the exact Brigade release version, a hex git SHA (the
# release build injects the full github.sha, but a short SHA is also valid),
# and a UTC build timestamp shaped like YYYY-MM-DDTHH:MM:SSZ.
_PLACEHOLDER_METADATA = {"dev", "unknown"}
_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
_BUILD_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
Runner = Callable[..., subprocess.CompletedProcess[str]]
JsonFetcher = Callable[[str], Any]
BytesFetcher = Callable[[str], bytes]


class AcceptanceError(RuntimeError):
    """A published-artifact acceptance requirement was not met."""


def managed_bin_path(data_home: Path, profile: Path, *, platform: str | None = None) -> Path:
    if (platform or sys.platform) == "darwin":
        return profile / "Library" / "Application Support" / "brigade" / "bin"
    return data_home / "brigade" / "bin"


def host_platform_key() -> str:
    machine = __import__("platform").machine().lower()
    os_name = "darwin" if sys.platform == "darwin" else "linux" if sys.platform.startswith("linux") else "windows"
    arch = "arm64" if machine in {"arm64", "aarch64"} else "amd64" if machine in {"x86_64", "amd64"} else machine
    platform = f"{os_name}-{arch}"
    if platform not in SUPPORTED_PLATFORMS:
        raise AcceptanceError(f"unsupported acceptance host platform {platform!r}")
    return platform


def fetch_pypi_json(url: str) -> Any:
    with urllib.request.urlopen(url, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_bytes(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read()


def _sha256_bytes(value: bytes) -> str:
    import hashlib

    return hashlib.sha256(value).hexdigest()


def _sha256_path(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _release_url(version: str, filename: str) -> str:
    return f"https://github.com/{REPOSITORY}/releases/download/v{version}/{filename}"


def _parse_checksums(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        parts = raw.split()
        if len(parts) != 2 or len(parts[0]) != 64 or any(char not in "0123456789abcdef" for char in parts[0]):
            raise AcceptanceError(f"checksums.txt line {number} is malformed")
        digest, name = parts
        if name in result:
            raise AcceptanceError(f"checksums.txt repeats {name!r}")
        result[name] = digest
    return result


def verify_release_assets(
    version: str,
    destination: Path,
    *,
    fetch_bytes: BytesFetcher = fetch_bytes,
) -> dict[str, Any]:
    """Fetch the immutable release manifest and verify every native release asset."""
    tag = f"v{version}"
    release_dir = destination / "release-assets"
    release_dir.mkdir(parents=True, exist_ok=True)
    manifest_url = _release_url(version, "component-manifest-v1.json")
    try:
        manifest_bytes = fetch_bytes(manifest_url)
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AcceptanceError(f"could not fetch release component manifest: {exc}") from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get("components"), dict):
        raise AcceptanceError("release component manifest has no components object")
    components = manifest["components"]
    if set(components) != set(COMPONENT_IDS):
        raise AcceptanceError("release component manifest must contain exactly five components")

    expected: dict[str, tuple[str, str]] = {}
    native_paths: dict[str, dict[str, Path]] = {component: {} for component in COMPONENT_IDS}
    for component in COMPONENT_IDS:
        record = components.get(component)
        if not isinstance(record, dict):
            raise AcceptanceError(f"release component manifest entry {component!r} is invalid")
        source = record.get("source")
        if not isinstance(source, dict) or source.get("repository") != REPOSITORY or source.get("release_tag") != tag:
            raise AcceptanceError(f"release component {component!r} does not point to {REPOSITORY}@{tag}")
        component_assets = record.get("assets")
        if not isinstance(component_assets, dict) or set(component_assets) != set(SUPPORTED_PLATFORMS):
            raise AcceptanceError(f"release component {component!r} does not publish the full platform matrix")
        for platform in SUPPORTED_PLATFORMS:
            asset = component_assets[platform]
            if not isinstance(asset, dict):
                raise AcceptanceError(f"release component {component!r} asset {platform!r} is invalid")
            name = asset.get("asset_name")
            digest = asset.get("sha256")
            url = asset.get("download_url")
            expected_name = f"{component}-{platform}" + (".exe" if platform == "windows-amd64" else "")
            if name != expected_name or not isinstance(digest, str) or len(digest) != 64:
                raise AcceptanceError(f"release component {component!r} asset {platform!r} has invalid metadata")
            if url != _release_url(version, name):
                raise AcceptanceError(
                    f"release component {component!r} asset {name!r} does not use its immutable release URL"
                )
            if name in expected:
                raise AcceptanceError(f"release component manifest repeats native asset {name!r}")
            expected[name] = (digest, url)

    checksums_url = _release_url(version, "checksums.txt")
    try:
        checksums = _parse_checksums(fetch_bytes(checksums_url).decode("utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise AcceptanceError(f"could not fetch release checksums.txt: {exc}") from exc
    expected_checksum_names = set(expected) | {"component-manifest-v1.json"}
    if set(checksums) != expected_checksum_names:
        raise AcceptanceError("checksums.txt must cover exactly all 25 native assets and component-manifest-v1.json")
    if checksums.get("component-manifest-v1.json") != _sha256_bytes(manifest_bytes):
        raise AcceptanceError("release manifest digest does not match checksums.txt")
    (release_dir / "component-manifest-v1.json").write_bytes(manifest_bytes)
    for name, (digest, url) in sorted(expected.items()):
        if checksums.get(name) != digest:
            raise AcceptanceError(f"checksums.txt digest for {name!r} does not match the release manifest")
        try:
            body = fetch_bytes(url)
        except OSError as exc:
            raise AcceptanceError(f"could not download release asset {name!r}: {exc}") from exc
        if _sha256_bytes(body) != digest:
            raise AcceptanceError(f"release asset {name!r} digest does not match the release manifest")
        path = release_dir / name
        path.write_bytes(body)
        component, platform = next(
            (component, platform)
            for component in COMPONENT_IDS
            for platform in SUPPORTED_PLATFORMS
            if name == f"{component}-{platform}" + (".exe" if platform == "windows-amd64" else "")
        )
        if platform != "windows-amd64":
            path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        native_paths[component][platform] = path
    return {"manifest": manifest, "native_paths": native_paths}


def verify_managed_component_digests(manifest: Any, managed_paths: Mapping[str, Path], platform: str) -> None:
    components = manifest.get("components") if isinstance(manifest, dict) else None
    if not isinstance(components, dict):
        raise AcceptanceError("release manifest has no component digest map")
    for component in COMPONENT_IDS:
        asset = components.get(component, {}).get("assets", {}).get(platform)
        if not isinstance(asset, dict) or not isinstance(asset.get("sha256"), str):
            raise AcceptanceError(f"release manifest is missing {component} {platform} digest")
        actual = _sha256_path(managed_paths[component])
        if actual != asset["sha256"]:
            raise AcceptanceError(f"managed {component} digest does not match the Brigade release manifest")


def wait_for_pypi_version(
    version: str,
    *,
    fetch_json: JsonFetcher = fetch_pypi_json,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    timeout_seconds: float = PYPI_AVAILABILITY_TIMEOUT_SECONDS,
    poll_interval_seconds: float = PYPI_POLL_INTERVAL_SECONDS,
) -> None:
    deadline = monotonic() + timeout_seconds
    detail = "PyPI response did not list the version"
    while True:
        try:
            payload = fetch_json(PYPI_PROJECT_URL)
        except (OSError, ValueError) as exc:
            detail = f"PyPI was unavailable: {exc}"
        else:
            releases = payload.get("releases") if isinstance(payload, dict) else None
            if isinstance(releases, dict) and version in releases:
                return
            if releases is None:
                detail = "PyPI response did not contain a releases object"
            elif not isinstance(releases, dict):
                detail = "PyPI response contained an invalid releases object"
            else:
                detail = "PyPI response did not list the version"

        remaining = deadline - monotonic()
        if remaining <= 0:
            raise AcceptanceError(
                f"PyPI version {version} was not available within {timeout_seconds:g} seconds: {detail}"
            )
        sleep(min(poll_interval_seconds, remaining))


def run_checked(
    argv: Sequence[str | Path],
    *,
    runner: Runner = subprocess.run,
    env: Mapping[str, str] | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [str(value) for value in argv]
    try:
        completed = runner(command, text=True, input=input_text, capture_output=True, env=env, check=False)
    except OSError as exc:
        raise AcceptanceError(f"could not run {' '.join(command)}: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "no command output").strip()
        raise AcceptanceError(f"{' '.join(command)} failed with exit {completed.returncode}: {detail}")
    return completed


def validate_component_report(report: Any, managed_bin: Path) -> dict[str, Path]:
    if not isinstance(report, dict) or not isinstance(report.get("components"), list):
        raise AcceptanceError("component report did not contain a components list")
    components = report["components"]
    if len(components) != len(COMPONENT_IDS):
        raise AcceptanceError(f"expected exactly 5 components, got {len(components)}")

    root = managed_bin.resolve()
    managed_paths: dict[str, Path] = {}
    for component in components:
        if not isinstance(component, dict):
            raise AcceptanceError("component report entry was not an object")
        component_id = component.get("component_id")
        if component_id not in COMPONENT_IDS:
            raise AcceptanceError(f"unexpected component {component_id!r}")
        if component_id in managed_paths:
            raise AcceptanceError(f"component report repeated {component_id}")
        if component.get("status") != "healthy":
            raise AcceptanceError(
                f"component {component_id} is {component.get('status')}: {component.get('detail', '')}"
            )
        executable = component.get("recorded_executable") or component.get("managed_executable_path")
        if not isinstance(executable, str) or not executable:
            raise AcceptanceError(f"component {component_id} has no managed executable path")
        raw_path = Path(executable)
        if not raw_path.is_absolute():
            raise AcceptanceError(f"component {component_id} executable must be absolute: {executable!r}")
        resolved = raw_path.resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise AcceptanceError(f"component {component_id} executable is outside managed root: {resolved}") from exc
        if not resolved.is_file():
            raise AcceptanceError(f"component {component_id} managed executable missing: {resolved}")
        managed_paths[component_id] = resolved

    if set(managed_paths) != set(COMPONENT_IDS):
        missing = ", ".join(sorted(set(COMPONENT_IDS) - set(managed_paths)))
        raise AcceptanceError(f"component report missing required components: {missing}")
    return managed_paths


def validate_agent_notify_version_payload(payload: Any, version: str) -> None:
    """Require agent-notify version JSON to carry the exact release metadata.

    A bare `go build` leaves main.version/main.commit/main.buildDate at their
    `dev`/`unknown`/`unknown` defaults. Published release assets must report the
    requested Brigade release version, a hex git SHA (the release build injects
    the full github.sha, but a short SHA is also accepted), and a UTC build
    timestamp. `dev`/`unknown` placeholders are rejected for every field.
    """
    if not isinstance(payload, dict):
        raise AcceptanceError("agent-notify smoke returned a non-object version payload")
    actual_version = payload.get("version")
    if not isinstance(actual_version, str) or not actual_version:
        raise AcceptanceError("agent-notify smoke JSON missing version field")
    if actual_version in _PLACEHOLDER_METADATA:
        raise AcceptanceError(f"agent-notify version must not report dev/unknown metadata: {actual_version!r}")
    if actual_version != version:
        raise AcceptanceError(f"agent-notify version mismatch: expected {version!r}, got {actual_version!r}")
    commit = payload.get("commit")
    if not isinstance(commit, str) or commit in _PLACEHOLDER_METADATA or not _COMMIT_SHA_RE.match(commit):
        raise AcceptanceError("agent-notify commit must be a hex git SHA (short or full SHA), not 'unknown'")
    build_date = payload.get("build_date")
    if not isinstance(build_date, str) or build_date in _PLACEHOLDER_METADATA or not _BUILD_DATE_RE.match(build_date):
        raise AcceptanceError("agent-notify build_date must be a UTC timestamp (YYYY-MM-DDTHH:MM:SSZ), not 'unknown'")


def smoke_managed_components(
    managed_paths: Mapping[str, Path],
    *,
    version: str,
    runner: Runner = subprocess.run,
    env: Mapping[str, str] | None = None,
) -> None:
    graphtrail = run_checked([managed_paths["graphtrail"], "--version"], runner=runner, env=env)
    if not graphtrail.stdout.strip():
        raise AcceptanceError("graphtrail smoke produced no version output")

    mcp = run_checked(
        [managed_paths["graphtrail-mcp"]],
        runner=runner,
        env=env,
        input_text='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n',
    )
    try:
        response = json.loads(mcp.stdout)
    except json.JSONDecodeError as exc:
        raise AcceptanceError("graphtrail-mcp smoke returned malformed JSON-RPC") from exc
    if response.get("jsonrpc") != "2.0" or response.get("id") != 1 or "result" not in response:
        raise AcceptanceError("graphtrail-mcp smoke returned an invalid JSON-RPC response")

    run_checked([managed_paths["miseledger"], "version"], runner=runner, env=env)
    sessionfind = run_checked([managed_paths["sessionfind"], "--help"], runner=runner, env=env)
    if "usage" not in f"{sessionfind.stdout}{sessionfind.stderr}".lower() and not any(
        line.strip().startswith("sessionfind ") for line in sessionfind.stdout.splitlines()
    ):
        raise AcceptanceError("sessionfind smoke produced no help text")
    agent_notify = run_checked([managed_paths["agent-notify"], "version", "--json"], runner=runner, env=env)
    try:
        agent_notify_payload = json.loads(agent_notify.stdout)
    except json.JSONDecodeError as exc:
        raise AcceptanceError("agent-notify smoke returned malformed JSON") from exc
    validate_agent_notify_version_payload(agent_notify_payload, version)


def smoke_rosetta_darwin_amd64(native_paths: Mapping[str, Mapping[str, Path]], *, runner: Runner) -> None:
    if sys.platform != "darwin":
        raise AcceptanceError("--rosetta-darwin-amd64 requires macOS")
    paths = {component: native_paths[component]["darwin-amd64"] for component in COMPONENT_IDS}
    graphtrail = run_checked(["arch", "-x86_64", paths["graphtrail"], "--version"], runner=runner)
    if not graphtrail.stdout.strip():
        raise AcceptanceError("Rosetta graphtrail smoke produced no version output")
    mcp = run_checked(
        ["arch", "-x86_64", paths["graphtrail-mcp"]],
        runner=runner,
        input_text='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n',
    )
    try:
        response = json.loads(mcp.stdout)
    except json.JSONDecodeError as exc:
        raise AcceptanceError("Rosetta graphtrail-mcp smoke returned malformed JSON-RPC") from exc
    if response.get("jsonrpc") != "2.0" or response.get("id") != 1 or "result" not in response:
        raise AcceptanceError("Rosetta graphtrail-mcp smoke returned invalid JSON-RPC")
    run_checked(["arch", "-x86_64", paths["miseledger"], "version"], runner=runner)
    run_checked(["arch", "-x86_64", paths["sessionfind"], "--help"], runner=runner)


def assert_no_poison_invocation(marker: Path) -> None:
    if marker.exists() and marker.read_text().strip():
        raise AcceptanceError(f"poison component binary was invoked: {marker.read_text().strip()}")


def _write_poison_binaries(poison_dir: Path, marker: Path) -> None:
    poison_dir.mkdir(parents=True)
    marker_literal = shlex.quote(str(marker))
    for component_id in COMPONENT_IDS:
        poison = poison_dir / component_id
        poison.write_text(f"#!/bin/sh\nprintf '%s\\n' {shlex.quote(component_id)} >> {marker_literal}\nexit 97\n")
        poison.chmod(poison.stat().st_mode | stat.S_IXUSR)


def run_acceptance(version: str, *, runner: Runner = subprocess.run, rosetta_darwin_amd64: bool = False) -> None:
    if not version or version.startswith("v"):
        raise AcceptanceError("--brigade-version must be an exact PyPI version without a v prefix")

    runner_temp = os.environ.get("RUNNER_TEMP")
    with tempfile.TemporaryDirectory(prefix="brigade-published-acceptance-", dir=runner_temp) as temporary:
        root = Path(temporary)
        profile = root / "profile"
        data_home = root / "xdg-data"
        pipx_home = root / "pipx-home"
        pipx_bin = root / "pipx-bin"
        poison_dir = root / "poison-bin"
        marker = root / "poison-invoked"
        for directory in (profile, data_home, pipx_home, pipx_bin, root / "xdg-config", root / "xdg-cache"):
            directory.mkdir(parents=True)
        _write_poison_binaries(poison_dir, marker)
        release = verify_release_assets(version, root)

        env = os.environ.copy()
        env.update(
            {
                "HOME": str(profile),
                "XDG_CONFIG_HOME": str(root / "xdg-config"),
                "XDG_DATA_HOME": str(data_home),
                "XDG_CACHE_HOME": str(root / "xdg-cache"),
                "PIPX_HOME": str(pipx_home),
                "PIPX_BIN_DIR": str(pipx_bin),
                "PATH": os.pathsep.join((str(poison_dir), env.get("PATH", ""))),
            }
        )
        managed_brigade = pipx_bin / "brigade"
        try:
            run_checked([sys.executable, "-m", "pip", "install", "--upgrade", "pip", "pipx"], runner=runner, env=env)
            wait_for_pypi_version(version)
            run_checked([sys.executable, "-m", "pipx", "install", f"brigade-cli=={version}"], runner=runner, env=env)
            if not managed_brigade.is_file():
                raise AcceptanceError(f"pipx did not install brigade at {managed_brigade}")

            version_output = run_checked([managed_brigade, "--version"], runner=runner, env=env).stdout.strip()
            if version_output != f"brigade {version}":
                raise AcceptanceError(f"installed brigade version mismatch: expected {version}, got {version_output}")
            run_checked([managed_brigade, "setup"], runner=runner, env=env)
            run_checked([managed_brigade, "setup", "--offline"], runner=runner, env=env)
            report_output = run_checked([managed_brigade, "version", "--components", "--json"], runner=runner, env=env)
            try:
                report = json.loads(report_output.stdout)
            except json.JSONDecodeError as exc:
                raise AcceptanceError("brigade version --components --json returned malformed JSON") from exc
            managed_paths = validate_component_report(report, managed_bin_path(data_home, profile))
            verify_managed_component_digests(release["manifest"], managed_paths, host_platform_key())
            smoke_managed_components(managed_paths, version=version, runner=runner, env=env)
            if rosetta_darwin_amd64:
                smoke_rosetta_darwin_amd64(release["native_paths"], runner=runner)
        finally:
            assert_no_poison_invocation(marker)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--brigade-version", required=True)
    parser.add_argument("--rosetta-darwin-amd64", action="store_true")
    args = parser.parse_args(argv)
    try:
        run_acceptance(args.brigade_version, rosetta_darwin_amd64=args.rosetta_darwin_amd64)
    except AcceptanceError as exc:
        print(f"published artifact acceptance failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
