"""Shared helpers for component install engine tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from brigade import component_manifest

GRAPHTRAIL_SHA = "64fcd2f9ec37f33e286708845a92e6cfa4abf3bb"
GRAPHTRAIL_BASE = "https://github.com/escoffier-labs/graphtrail/releases/download/v0.4.0/"
FIXTURE_REPOSITORY = "example/components"
RELEASE_REPOSITORY = "escoffier-labs/brigade"
MANIFEST_ASSET_NAME = "component-manifest-v1.json"


def linux_env(root: Path) -> dict[str, str]:
    home = root / "home"
    home.mkdir(parents=True, exist_ok=True)
    return {
        "HOME": str(home),
        "XDG_DATA_HOME": str(root / "xdg-data"),
        "XDG_CACHE_HOME": str(root / "xdg-cache"),
    }


def smoke_stub_script(name: str) -> str:
    if name == "graphtrail":
        return (
            '#!/usr/bin/env python3\nimport sys\nif sys.argv[1:] == ["--version"]:\n'
            '    print("graphtrail test 0.4.0")\n    raise SystemExit(0)\nraise SystemExit(1)\n'
        )
    if name == "graphtrail-mcp":
        return (
            "#!/usr/bin/env python3\nimport json, sys\n"
            "req = json.load(sys.stdin)\n"
            'assert req.get("method") == "initialize"\n'
            'print(json.dumps({"jsonrpc": "2.0", "id": req.get("id"), "result": '
            '{"protocolVersion": "2024-11-05", "capabilities": {}, '
            '"serverInfo": {"name": "graphtrail-mcp", "version": "0.4.0"}}}))\n'
        )
    if name == "miseledger":
        return (
            '#!/usr/bin/env python3\nimport sys\nif sys.argv[1:] == ["version"]:\n'
            '    print("miseledger test 0.6.0")\n    raise SystemExit(0)\nraise SystemExit(1)\n'
        )
    if name == "sessionfind":
        return (
            '#!/usr/bin/env python3\nimport sys\nif sys.argv[1:] == ["--help"]:\n'
            '    print("sessionfind list [--source KIND] ...")\n'
            '    print("sessionfind search <query> ...")\n'
            '    print("sessionfind <query> ...")\n'
            "    raise SystemExit(0)\nraise SystemExit(1)\n"
        )
    if name == "agent-notify":
        return (
            '#!/usr/bin/env python3\nimport json, sys\nif sys.argv[1:] == ["version", "--json"]:\n'
            '    print(json.dumps({"version": "test 0.1.0"}))\n    raise SystemExit(0)\nraise SystemExit(1)\n'
        )
    raise ValueError(name)


def fixture_payload(component_id: str, *, platform: str = "linux-amd64") -> tuple[bytes, int, str]:
    """Return (payload_bytes, byte_size, sha256) for runnable smoke-stub fixture bytes."""
    lines = smoke_stub_script(component_id).splitlines(keepends=True)
    if lines and lines[0].startswith("#!"):
        lines.insert(1, f"# platform: {platform}\n")
    else:
        lines.insert(0, f"# platform: {platform}\n")
    body = "".join(lines).encode("utf-8")
    digest = hashlib.sha256(body).hexdigest()
    return body, len(body), digest


def fixture_asset_name(component_id: str, *, platform: str) -> str:
    base = f"{component_id}-{platform}"
    if platform == "windows-amd64":
        return f"{base}.exe"
    return base


def fixture_component_revision(component_id: str) -> str:
    return GRAPHTRAIL_SHA


def write_verified_cache(cache_path: Path, *, payload: bytes) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(payload)
    cache_path.chmod(0o755)


def test_manifest_asset(component_id: str, *, platform: str = "linux-amd64") -> component_manifest.ComponentAsset:
    _, byte_size, sha256 = fixture_payload(component_id, platform=platform)
    asset_name = fixture_asset_name(component_id, platform=platform)
    return component_manifest.ComponentAsset(
        asset_name=asset_name,
        byte_size=byte_size,
        sha256=sha256,
        download_url=f"https://example.invalid/components/{asset_name}",
    )


def write_test_manifest(path: Path, *, brigade_version: str) -> component_manifest.ComponentManifest:
    """Write a manifest whose digests match fixture_payload bytes for offline engine tests."""
    components: dict[str, object] = {}
    for component_id in component_manifest.KNOWN_COMPONENT_IDS:
        assets: dict[str, object] = {}
        for platform in component_manifest.SUPPORTED_PLATFORMS:
            _, byte_size, sha256 = fixture_payload(component_id, platform=platform)
            asset = test_manifest_asset(component_id, platform=platform)
            assert asset.byte_size == byte_size
            assert asset.sha256 == sha256
            assets[platform] = {
                "asset_name": asset.asset_name,
                "byte_size": byte_size,
                "sha256": sha256,
                "download_url": asset.download_url,
            }
        components[component_id] = {
            "component_revision": fixture_component_revision(component_id),
            "source": {"repository": FIXTURE_REPOSITORY, "release_tag": "fixture"},
            "executable": component_id,
            "assets": assets,
        }
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "brigade_version": brigade_version,
                "manifest_revision": "fixture",
                "supported_platforms": list(component_manifest.SUPPORTED_PLATFORMS),
                "components": components,
            }
        )
    )
    return component_manifest.load(path)


def all_fixture_payloads(*, platform: str = "linux-amd64") -> dict[str, bytes]:
    payloads: dict[str, bytes] = {}
    for component_id in component_manifest.KNOWN_COMPONENT_IDS:
        payload, _, _ = fixture_payload(component_id, platform=platform)
        asset = test_manifest_asset(component_id, platform=platform)
        payloads[asset.download_url] = payload
    return payloads


class _RedirectFakeResponse:
    def __init__(self, payload: bytes, *, final_url: str):
        from io import BytesIO

        self._stream = BytesIO(payload)
        self._final_url = final_url
        self.read_sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            return self._stream.read()
        self.read_sizes.append(size)
        return self._stream.read(size)

    def geturl(self) -> str:
        return self._final_url

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class _TrackingFakeResponse:
    def __init__(self, payload: bytes):
        from io import BytesIO

        self._stream = BytesIO(payload)
        self.read_sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            return self._stream.read()
        self.read_sizes.append(size)
        return self._stream.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class FakeOpener:
    def __init__(
        self,
        payloads: dict[str, bytes],
        *,
        final_urls: dict[str, str] | None = None,
    ):
        self.payloads = payloads
        self.final_urls = final_urls or {}
        self.calls: list[str] = []
        self.responses: list[_TrackingFakeResponse | _RedirectFakeResponse] = []

    def __call__(self, url: str, *args, **kwargs):
        self.calls.append(url)
        from io import BytesIO
        from urllib.error import HTTPError

        if url not in self.payloads:
            raise HTTPError(url, 404, "not found", hdrs=None, fp=BytesIO(b""))
        payload = self.payloads[url]
        final_url = self.final_urls.get(url)
        if final_url is not None:
            response: _TrackingFakeResponse | _RedirectFakeResponse = _RedirectFakeResponse(
                payload, final_url=final_url
            )
        else:
            response = _TrackingFakeResponse(payload)
        self.responses.append(response)
        return response


def release_manifest_url(tag: str) -> str:
    """The exact Brigade release manifest asset URL for ``tag``."""
    return f"https://github.com/{RELEASE_REPOSITORY}/releases/download/{tag}/{MANIFEST_ASSET_NAME}"


def release_component_asset_url(component_id: str, *, tag: str, platform: str) -> str:
    """The exact Brigade release asset URL for one component on one platform."""
    asset_name = fixture_asset_name(component_id, platform=platform)
    return f"https://github.com/{RELEASE_REPOSITORY}/releases/download/{tag}/{asset_name}"


def write_release_manifest(
    path: Path, *, brigade_version: str, target_commit: str, drop_component: str | None = None
) -> bytes:
    """Write a manifest with real Brigade release coordinates.

    Every component pins to ``target_commit`` and the real Brigade release
    repository/tag/asset URLs so the real
    :func:`brigade.update_cmd.validate_release_manifest_bytes` accepts it
    against a release resolved for ``v{brigade_version}``.

    ``drop_component`` omits one component id entirely so the manifest fails
    ``load_bytes`` validation (``missing required components``) while remaining
    otherwise well-formed; the returned bytes hash to their own digest so a
    caller can persist a state recording that digest and exercise the
    digest-matching-but-invalid path without violating content addressing.
    """
    tag = f"v{brigade_version}"
    components: dict[str, object] = {}
    for component_id in component_manifest.KNOWN_COMPONENT_IDS:
        if component_id == drop_component:
            continue
        assets: dict[str, object] = {}
        for platform in component_manifest.SUPPORTED_PLATFORMS:
            _, byte_size, sha256 = fixture_payload(component_id, platform=platform)
            asset_name = fixture_asset_name(component_id, platform=platform)
            assets[platform] = {
                "asset_name": asset_name,
                "byte_size": byte_size,
                "sha256": sha256,
                "download_url": release_component_asset_url(component_id, tag=tag, platform=platform),
            }
        components[component_id] = {
            "component_revision": target_commit,
            "source": {"repository": RELEASE_REPOSITORY, "release_tag": tag},
            "executable": component_id,
            "assets": assets,
        }
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "brigade_version": brigade_version,
                "manifest_revision": f"{tag}+{target_commit}",
                "supported_platforms": list(component_manifest.SUPPORTED_PLATFORMS),
                "components": components,
            }
        )
    )
    return path.read_bytes()


def all_release_payloads(*, tag: str, platform: str = "linux-amd64") -> dict[str, bytes]:
    """``FakeOpener`` payloads keyed by the real Brigade release asset URLs."""
    payloads: dict[str, bytes] = {}
    for component_id in component_manifest.KNOWN_COMPONENT_IDS:
        payload, _, _ = fixture_payload(component_id, platform=platform)
        payloads[release_component_asset_url(component_id, tag=tag, platform=platform)] = payload
    return payloads


class FakeReleaseHttp:
    """Fake GitHub HTTP transport for the real exact-tag release resolver.

    Serves the release JSON, tag ref, and manifest bytes so
    :func:`brigade.update_cmd.resolve_release` runs for real: it dereferences
    the tag to ``target_commit``, verifies the manifest asset size and SHA-256
    release digest, and downloads the manifest bytes. The component assets
    themselves are still downloaded through the ``FakeOpener`` passed to
    ``setup_native_components``.
    """

    def __init__(self, *, tag: str, manifest_bytes: bytes, target_commit: str, release_id: int = 42):
        self.tag = tag
        self.manifest_bytes = manifest_bytes
        self.target_commit = target_commit
        self.release_id = release_id
        digest = hashlib.sha256(manifest_bytes).hexdigest()
        self._release = {
            "id": release_id,
            "tag_name": tag,
            "target_commitish": "main",
            "draft": False,
            "prerelease": False,
            "assets": [
                {
                    "name": MANIFEST_ASSET_NAME,
                    "size": len(manifest_bytes),
                    "digest": f"sha256:{digest}",
                    "browser_download_url": release_manifest_url(tag),
                }
            ],
        }
        self._ref = {"ref": f"refs/tags/{tag}", "object": {"type": "commit", "sha": target_commit}}
        self.urls: list[str] = []

    def json(self, url: str) -> Any:
        self.urls.append(url)
        if url == f"https://api.github.com/repos/{RELEASE_REPOSITORY}/releases/tags/{self.tag}":
            return self._release
        if url == f"https://api.github.com/repos/{RELEASE_REPOSITORY}/git/ref/tags/{self.tag}":
            return self._ref
        raise AssertionError(url)

    def bytes(self, url: str) -> bytes:
        self.urls.append(url)
        if url != release_manifest_url(self.tag):
            raise AssertionError(url)
        return self.manifest_bytes
