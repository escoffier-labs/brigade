"""Tests for immutable Brigade update resolution and ownership."""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
from email.message import Message
from io import BytesIO
from pathlib import Path
from urllib.response import addinfourl
from unittest.mock import patch

import pytest

from brigade import component_install, component_manifest, component_paths, update_cmd


TAG = "v1.2.3"
VERSION = "1.2.3"
BASE = f"https://github.com/escoffier-labs/brigade/releases/download/{TAG}/"
REF_URL = f"https://api.github.com/repos/escoffier-labs/brigade/git/ref/tags/{TAG}"
TAG_OBJECT_URL = "https://api.github.com/repos/escoffier-labs/brigade/git/tags/"


@pytest.fixture(autouse=True)
def _default_install_name(monkeypatch):
    """Keep command assertions independent of how this test environment was installed."""
    monkeypatch.setattr(update_cmd, "installed_pypi_project", lambda: update_cmd.PYPI_PROJECT)


def _manifest() -> bytes:
    components = {}
    for component in component_manifest.KNOWN_COMPONENT_IDS:
        assets = {}
        for platform in component_manifest.component_platforms(component):
            name = f"{component}-{platform}" + (".exe" if platform == "windows-amd64" else "")
            payload = name.encode()
            assets[platform] = {
                "asset_name": name,
                "byte_size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "download_url": BASE + name,
            }
        components[component] = {
            "component_revision": "a" * 40,
            "source": {"repository": "escoffier-labs/brigade", "release_tag": TAG},
            "executable": component,
            "assets": assets,
        }
    return (
        json.dumps(
            {
                "schema_version": 1,
                "brigade_version": VERSION,
                "manifest_revision": "v1.2.3+" + "a" * 40,
                "supported_platforms": list(component_manifest.SUPPORTED_PLATFORMS),
                "components": components,
            },
            sort_keys=True,
        )
        + "\n"
    ).encode()


def _pre_agent_notify_manifest() -> bytes:
    """Last-stable manifest predating the agent-notify KNOWN_COMPONENT_IDS entry.

    The beta handoff downloads the most recent stable release manifest, which
    was published before agent-notify was added to KNOWN_COMPONENT_IDS on main
    and therefore carries the four published native engines and no agent-notify
    entry at all. Strict validation must reject this manifest; only the narrowly
    named compatibility mode used by the beta handoff may accept it.
    """
    components = {}
    for component in component_manifest.KNOWN_COMPONENT_IDS:
        # The last stable release also predates brigade-hub.
        if component in ("agent-notify", "brigade-hub"):
            continue
        assets = {}
        for platform in component_manifest.SUPPORTED_PLATFORMS:
            name = f"{component}-{platform}" + (".exe" if platform == "windows-amd64" else "")
            payload = name.encode()
            assets[platform] = {
                "asset_name": name,
                "byte_size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "download_url": BASE + name,
            }
        components[component] = {
            "component_revision": "a" * 40,
            "source": {"repository": "escoffier-labs/brigade", "release_tag": TAG},
            "executable": component,
            "assets": assets,
        }
    return (
        json.dumps(
            {
                "schema_version": 1,
                "brigade_version": VERSION,
                "manifest_revision": "v1.2.3+" + "a" * 40,
                "supported_platforms": list(component_manifest.SUPPORTED_PLATFORMS),
                "components": components,
            },
            sort_keys=True,
        )
        + "\n"
    ).encode()


def _release(manifest: bytes) -> dict:
    digest = hashlib.sha256(manifest).hexdigest()
    return {
        "id": 42,
        "tag_name": TAG,
        "target_commitish": "main",
        "draft": False,
        "prerelease": False,
        "assets": [
            {
                "name": "component-manifest-v1.json",
                "size": len(manifest),
                "digest": f"sha256:{digest}",
                "browser_download_url": BASE + "component-manifest-v1.json",
            }
        ],
    }


class _Http:
    def __init__(self, release, manifest, *, tag_ref=None, tag_objects=None):
        self.release = release
        self.manifest = manifest
        self.tag_ref = tag_ref or {"ref": f"refs/tags/{TAG}", "object": {"type": "commit", "sha": "a" * 40}}
        self.tag_objects = tag_objects or {}
        self.urls = []

    def json(self, url):
        self.urls.append(url)
        if url == "https://api.github.com/repos/escoffier-labs/brigade":
            return {"id": update_cmd.REPOSITORY_ID, "full_name": "escoffier-labs/brigade"}
        if url.endswith("/releases/latest"):
            return self.release
        if url == REF_URL:
            return self.tag_ref
        if url.startswith(TAG_OBJECT_URL):
            return self.tag_objects[url.removeprefix(TAG_OBJECT_URL)]
        raise AssertionError(url)

    def bytes(self, url):
        self.urls.append(url)
        assert url == BASE + "component-manifest-v1.json"
        return self.manifest


def _wheel_file(version: str, *, yanked: bool = False, packagetype: str = "bdist_wheel") -> dict:
    return {
        "filename": f"brigadestack-{version}-py3-none-any.whl",
        "packagetype": packagetype,
        "yanked": yanked,
    }


def _sdist_file(version: str, *, yanked: bool = False) -> dict:
    return {
        "filename": f"brigadestack-{version}.tar.gz",
        "packagetype": "sdist",
        "yanked": yanked,
    }


class _PypiHttp(_Http):
    def __init__(
        self,
        release,
        manifest,
        *,
        pypi_releases=None,
        info_version: str = "0.32.0",
        tag_ref=None,
        tag_objects=None,
    ):
        super().__init__(release, manifest, tag_ref=tag_ref, tag_objects=tag_objects)
        self.pypi_releases = pypi_releases if pypi_releases is not None else _default_beta_releases()
        self.info_version = info_version

    def json(self, url):
        self.urls.append(url)
        if url == update_cmd.PYPI_PROJECT_JSON_URL:
            # Mirror live PyPI: info.version stays on the latest stable release
            # even when releases contains 0.33.0.devYYYYMMDD wheels.
            return {"info": {"version": self.info_version}, "releases": self.pypi_releases}
        if url == "https://api.github.com/repos/escoffier-labs/brigade":
            return {"id": update_cmd.REPOSITORY_ID, "full_name": "escoffier-labs/brigade"}
        if url.endswith("/releases/latest"):
            return self.release
        if url == REF_URL:
            return self.tag_ref
        if url.startswith(TAG_OBJECT_URL):
            return self.tag_objects[url.removeprefix(TAG_OBJECT_URL)]
        raise AssertionError(url)


def _default_beta_releases() -> dict:
    version = "0.33.0.dev20260808"
    return {version: [_wheel_file(version)]}


BETA_VERSION = "0.33.0.dev20260808"


class _Response:
    def __init__(self, body: bytes, final_url: str):
        self.body = body
        self.final_url = final_url

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body

    def geturl(self):
        return self.final_url


@pytest.mark.parametrize(
    "final_url",
    (
        "https://release-assets.githubusercontent.com/download/manifest",
        "https://objects.githubusercontent.com/github-production-release-asset-2e65be/manifest",
        "https://github-releases.githubusercontent.com/download/manifest",
    ),
)
def test_default_http_accepts_https_github_release_cdn_redirect(final_url):
    url = BASE + "component-manifest-v1.json"
    with patch("urllib.request.OpenerDirector.open", return_value=_Response(b"manifest", final_url)) as opener:
        assert update_cmd._DefaultHttp().bytes(url) == b"manifest"
    assert opener.call_args.args[0].get_header("User-agent") == "brigade-update/1"


def test_beta_update_reports_rate_limited_github_request(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    headers = Message()
    headers["X-RateLimit-Remaining"] = "0"
    paths = update_cmd.UpdatePaths(tmp_path / "data", tmp_path / "cache", tmp_path / "brigade")
    url = f"https://api.github.com/repos/{update_cmd.REPOSITORY}/releases/latest"

    def blocked(request, *, timeout):
        assert request.get_header("User-agent") == "brigade-update/1"
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", headers, None)

    with patch("urllib.request.OpenerDirector.open", side_effect=blocked):
        assert update_cmd.run_update(channel="beta", dry_run=True, paths=paths) == 1
    message = capsys.readouterr().err
    assert url in message and "HTTP 403" in message and "rate limit" in message


def test_update_refuses_redirect_with_original_url_and_status(monkeypatch):
    url = f"https://api.github.com/repos/{update_cmd.REPOSITORY}/releases/latest"
    headers = Message()
    headers["Location"] = "https://example.invalid/unexpected"

    def redirect(_handler, request):
        assert request.full_url == url
        assert request.get_header("User-agent") == "brigade-update/1"
        response = addinfourl(BytesIO(), headers, url, 302)
        response.msg = "Found"
        return response

    monkeypatch.setattr("urllib.request.HTTPSHandler.https_open", redirect)
    with pytest.raises(update_cmd.UpdateError, match=f"{url} HTTP 302.*redirected outside"):
        update_cmd._DefaultHttp().json(url)


@pytest.mark.parametrize(
    "final_url",
    (
        "https://example.test/manifest",
        "http://release-assets.githubusercontent.com/manifest",
    ),
)
def test_default_http_rejects_untrusted_or_non_https_asset_redirect(final_url):
    url = BASE + "component-manifest-v1.json"
    with patch("urllib.request.OpenerDirector.open", return_value=_Response(b"manifest", final_url)):
        with pytest.raises(update_cmd.UpdateError, match="release asset redirected"):
            update_cmd._DefaultHttp().bytes(url)


def test_default_http_rejects_api_redirect_outside_exact_api_path():
    url = "https://api.github.com/repos/escoffier-labs/brigade/releases/latest"
    with patch(
        "urllib.request.OpenerDirector.open",
        return_value=_Response(b"{}", "https://api.github.com/repos/escoffier-labs/brigade/releases/tags/v1.2.3"),
    ):
        with pytest.raises(update_cmd.UpdateError, match="GitHub API request redirected"):
            update_cmd._DefaultHttp().json(url)


def test_default_http_uses_github_token_for_github_api_requests(monkeypatch):
    url = "https://api.github.com/repos/escoffier-labs/brigade/releases/latest"
    monkeypatch.setenv("GITHUB_TOKEN", "test-gh")
    monkeypatch.setenv("GH_TOKEN", "test-alt")
    requests = []

    def urlopen(request, *, timeout):
        requests.append(request)
        assert timeout == 30
        return _Response(b"{}", url)

    with patch("urllib.request.OpenerDirector.open", side_effect=urlopen):
        assert update_cmd._DefaultHttp().json(url) == {}

    assert requests[0].get_header("Authorization") == "Bearer test-gh"


def test_default_http_uses_gh_token_when_github_token_is_absent(monkeypatch):
    url = "https://api.github.com/repos/escoffier-labs/brigade/releases/latest"
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GH_TOKEN", "test-alt")

    with patch("urllib.request.OpenerDirector.open", return_value=_Response(b"{}", url)) as urlopen:
        assert update_cmd._DefaultHttp().json(url) == {}

    request = urlopen.call_args.args[0]
    assert request.get_header("Authorization") == "Bearer test-alt"


@pytest.mark.parametrize("status", (401, 403))
def test_default_http_retries_github_api_without_rejected_token(monkeypatch, status):
    url = "https://api.github.com/repos/escoffier-labs/brigade/releases/latest"
    secret = "test-rejected"
    monkeypatch.setenv("GITHUB_TOKEN", secret)
    requests = []

    def urlopen(request, *, timeout):
        requests.append(request)
        assert timeout == 30
        if len(requests) == 1:
            raise urllib.error.HTTPError(url, status, "Bad credentials", {}, None)
        return _Response(b"{}", url)

    with patch("urllib.request.OpenerDirector.open", side_effect=urlopen):
        assert update_cmd._DefaultHttp().json(url) == {}

    assert requests[0].get_header("Authorization") == f"Bearer {secret}"
    assert requests[1].get_header("Authorization") is None


def test_default_http_rejected_token_is_absent_from_final_error(monkeypatch):
    url = "https://api.github.com/repos/escoffier-labs/brigade/releases/latest"
    secret = "test-rejected"
    monkeypatch.setenv("GITHUB_TOKEN", secret)

    with patch(
        "urllib.request.OpenerDirector.open",
        side_effect=urllib.error.HTTPError(url, 401, "Bad credentials", {}, None),
    ):
        with pytest.raises(update_cmd.UpdateError) as caught:
            update_cmd._DefaultHttp().json(url)

    assert secret not in str(caught.value)


@pytest.mark.parametrize("asset", [True, False])
def test_default_http_never_sends_github_token_to_non_api_requests(monkeypatch, asset):
    monkeypatch.setenv("GITHUB_TOKEN", "test-gh")
    url = BASE + "component-manifest-v1.json" if asset else update_cmd.PYPI_PROJECT_JSON_URL

    with patch("urllib.request.OpenerDirector.open", return_value=_Response(b"{}", url)) as urlopen:
        http = update_cmd._DefaultHttp()
        assert (http.bytes(url) if asset else http.json(url)) == (b"{}" if asset else {})

    request = urlopen.call_args.args[0]
    assert request.get_header("Authorization") is None
    assert request.get_header("User-agent") == "brigade-update/1"


def test_cache_manifest_preserves_verified_crlf_bytes_without_text_round_trip(tmp_path, monkeypatch):
    manifest_bytes = b'{"schema_version":1}\r\n'
    release = update_cmd.ResolvedRelease(
        42,
        TAG,
        VERSION,
        "a" * 40,
        BASE + "component-manifest-v1.json",
        len(manifest_bytes),
        hashlib.sha256(manifest_bytes).hexdigest(),
        manifest_bytes,
    )
    paths = update_cmd.UpdatePaths(tmp_path / "data", tmp_path / "cache", tmp_path / "bin" / "brigade")
    monkeypatch.setattr(
        update_cmd.localio,
        "write_text_atomic",
        lambda *_args: pytest.fail("verified manifest cache must not use a text write"),
    )

    cached = update_cmd._cache_manifest(paths, release)

    assert cached.read_bytes() == manifest_bytes


def test_cache_manifest_rejects_existing_bytes_for_the_same_digest_path(tmp_path):
    manifest_bytes = b'{"schema_version":1}\r\n'
    release = update_cmd.ResolvedRelease(
        42,
        TAG,
        VERSION,
        "a" * 40,
        BASE + "component-manifest-v1.json",
        len(manifest_bytes),
        hashlib.sha256(manifest_bytes).hexdigest(),
        manifest_bytes,
    )
    paths = update_cmd.UpdatePaths(tmp_path / "data", tmp_path / "cache", tmp_path / "bin" / "brigade")
    cached = Path(update_cmd.component_paths.verified_manifest_path(str(paths.cache_root), release.manifest_sha256))
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"different manifest")

    with pytest.raises(update_cmd.UpdateError, match="verified manifest cache digest collision"):
        update_cmd._cache_manifest(paths, release)


def test_resolve_beta_cli_version_selects_newest_non_yanked_current_base_dev_wheel():
    releases = {
        "0.33.0.dev20260801": [_wheel_file("0.33.0.dev20260801")],
        "0.33.0.dev20260810": [_wheel_file("0.33.0.dev20260810", yanked=True)],
        "0.33.0.dev20260809": [_wheel_file("0.33.0.dev20260809")],
        "0.33.0.dev20260808": [_sdist_file("0.33.0.dev20260808")],
        "0.32.0.dev20260811": [_wheel_file("0.32.0.dev20260811")],
        "0.34.0.dev20260811": [_wheel_file("0.34.0.dev20260811")],
        "0.33.0.dev2026080": [_wheel_file("0.33.0.dev2026080")],
        "0.33.0.dev202608091": [_wheel_file("0.33.0.dev202608091")],
        "0.33.0a1": [_wheel_file("0.33.0a1")],
        "0.33.0rc1": [_wheel_file("0.33.0rc1")],
        "0.33.0.dev20260809.post1": [_wheel_file("0.33.0.dev20260809.post1")],
        "0.33.1.dev20260811": [_wheel_file("0.33.1.dev20260811")],
        "1.2.3": [_wheel_file("1.2.3")],
        "main": [_wheel_file("main")],
        "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb": [_wheel_file("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")],
    }
    http = _PypiHttp(_release(_manifest()), _manifest(), pypi_releases=releases)

    assert update_cmd.resolve_beta_cli_version(http) == "0.33.0.dev20260809"
    assert update_cmd.PYPI_PROJECT_JSON_URL in http.urls


@pytest.mark.parametrize(
    "releases",
    (
        {},
        {"0.33.0.dev20260808": []},
        {"0.33.0.dev20260808": [_wheel_file("0.33.0.dev20260808", yanked=True)]},
        {"0.33.0.dev20260808": [_sdist_file("0.33.0.dev20260808")]},
        {"0.33.0.dev20260808": "not-a-list"},
        {"0.33.0.dev2026080": [_wheel_file("0.33.0.dev2026080")]},
        {"0.32.0.dev20260808": [_wheel_file("0.32.0.dev20260808")]},
        {"0.33.0a1": [_wheel_file("0.33.0a1")]},
    ),
)
def test_resolve_beta_cli_version_fails_closed_without_installable_preview_wheel(releases):
    http = _PypiHttp(_release(_manifest()), _manifest(), pypi_releases=releases)
    with pytest.raises(update_cmd.UpdateError, match="beta fails closed"):
        update_cmd.resolve_beta_cli_version(http)


def test_resolve_beta_cli_version_scans_releases_not_info_version():
    """Live PyPI shape after workflow 31422458576: info.version stays stable."""
    releases = {
        "0.32.0": [_wheel_file("0.32.0")],
        "0.33.0.dev20260810": [_wheel_file("0.33.0.dev20260810")],
    }
    http = _PypiHttp(
        _release(_manifest()),
        _manifest(),
        pypi_releases=releases,
        info_version="0.32.0",
    )

    assert update_cmd.resolve_beta_cli_version(http) == "0.33.0.dev20260810"
    assert http.info_version == "0.32.0"


def test_resolve_beta_cli_version_ignores_info_version_even_when_it_looks_like_a_dev_wheel():
    releases = {
        "0.32.0": [_wheel_file("0.32.0")],
        "0.33.0.dev20260810": [_wheel_file("0.33.0.dev20260810")],
    }
    http = _PypiHttp(
        _release(_manifest()),
        _manifest(),
        pypi_releases=releases,
        info_version="0.33.0.dev20991231",
    )

    assert update_cmd.resolve_beta_cli_version(http) == "0.33.0.dev20260810"


def test_resolve_beta_cli_version_fails_closed_when_releases_map_missing():
    class _MissingReleases:
        urls: list[str]

        def __init__(self) -> None:
            self.urls = []

        def json(self, url: str):
            self.urls.append(url)
            assert url == update_cmd.PYPI_PROJECT_JSON_URL
            return {"info": {"version": "0.32.0"}}

        def bytes(self, url: str) -> bytes:
            raise AssertionError(url)

    with pytest.raises(update_cmd.UpdateError, match="releases map"):
        update_cmd.resolve_beta_cli_version(_MissingReleases())


def test_resolve_beta_cli_version_ignores_yanked_sibling_and_keeps_older_wheel():
    releases = {
        "0.33.0.dev20260810": [_wheel_file("0.33.0.dev20260810", yanked=True)],
        "0.33.0.dev20260809": [
            _wheel_file("0.33.0.dev20260809", yanked=True),
            _wheel_file("0.33.0.dev20260809"),
        ],
        "0.33.0.dev20260801": [_wheel_file("0.33.0.dev20260801")],
    }
    http = _PypiHttp(_release(_manifest()), _manifest(), pypi_releases=releases)
    assert update_cmd.resolve_beta_cli_version(http) == "0.33.0.dev20260809"


def test_parse_beta_dev_version_accepts_only_exact_current_base_daily_form():
    assert update_cmd._parse_beta_dev_version("0.33.0.dev20260808") == "20260808"
    assert update_cmd._parse_beta_dev_version("0.33.0.dev2026080") is None
    assert update_cmd._parse_beta_dev_version("0.32.0.dev20260808") is None
    assert update_cmd._parse_beta_dev_version("0.34.0.dev20260808") is None
    assert update_cmd._parse_beta_dev_version("0.33.0a1") is None
    assert update_cmd._parse_beta_dev_version("b" * 40) is None
    assert update_cmd.is_legacy_beta_git_sha("b" * 40)
    assert not update_cmd.is_legacy_beta_git_sha(BETA_VERSION)


def test_stable_update_installs_exact_release_then_new_binary_setup(tmp_path):
    manifest = _manifest()
    commands = []
    installed_binary = tmp_path / "bin" / "brigade"

    def runner(argv):
        commands.append(argv)
        return 0

    result = update_cmd.run_update(
        channel="stable",
        paths=update_cmd.UpdatePaths(
            data_root=tmp_path / "data", cache_root=tmp_path / "cache", brigade_executable=installed_binary
        ),
        http=_Http(_release(manifest), manifest),
        runner=runner,
        now=lambda: "2026-07-20T12:00:00+00:00",
    )

    assert result == 0
    assert commands[0] == ["pipx", "install", "--force", "brigadestack==1.2.3"]
    assert commands[1][0] == str(installed_binary)
    assert commands[1][1:3] == ["setup", "--manifest"]
    state = update_cmd.load_update_state(tmp_path / "data" / "brigade" / "update-state.json")
    assert state is not None
    assert state.channel == "stable"
    assert state.cli_coordinate == "1.2.3"
    assert state.component_tag == TAG


def test_release_resolution_uses_lightweight_tag_commit_not_release_target_commitish():
    manifest = _manifest()
    release = update_cmd.resolve_release(_Http(_release(manifest), manifest), latest=True)

    assert release.target_commit == "a" * 40


def test_release_resolution_dereferences_annotated_tags_to_their_commit():
    manifest = _manifest()
    first_tag = "b" * 40
    second_tag = "c" * 40
    http = _Http(
        _release(manifest),
        manifest,
        tag_ref={"ref": f"refs/tags/{TAG}", "object": {"type": "tag", "sha": first_tag}},
        tag_objects={
            first_tag: {"tag": TAG, "object": {"type": "tag", "sha": second_tag}},
            second_tag: {"tag": TAG, "object": {"type": "commit", "sha": "a" * 40}},
        },
    )

    release = update_cmd.resolve_release(http, latest=True)

    assert release.target_commit == "a" * 40
    # urls[1] is the repository identity check that follows the release lookup.
    assert http.urls[2:5] == [REF_URL, TAG_OBJECT_URL + first_tag, TAG_OBJECT_URL + second_tag]


@pytest.mark.parametrize(
    ("tag_ref", "tag_objects"),
    (
        ({"ref": "refs/tags/v9.9.9", "object": {"type": "commit", "sha": "a" * 40}}, {}),
        ({"ref": f"refs/tags/{TAG}", "object": {"type": "blob", "sha": "a" * 40}}, {}),
        ({"ref": f"refs/tags/{TAG}", "object": {"type": "commit", "sha": "A" * 40}}, {}),
        (
            {"ref": f"refs/tags/{TAG}", "object": {"type": "tag", "sha": "b" * 40}},
            {"b" * 40: {"tag": "v9.9.9", "object": {"type": "commit", "sha": "a" * 40}}},
        ),
        (
            {"ref": f"refs/tags/{TAG}", "object": {"type": "tag", "sha": "b" * 40}},
            {"b" * 40: {"tag": TAG, "object": {"type": "tag", "sha": "b" * 40}}},
        ),
    ),
)
def test_release_resolution_fails_closed_for_malformed_or_cyclic_tag_objects(tag_ref, tag_objects):
    with pytest.raises(update_cmd.UpdateError):
        update_cmd.resolve_release(
            _Http(_release(_manifest()), _manifest(), tag_ref=tag_ref, tag_objects=tag_objects), latest=True
        )


def test_release_resolution_fails_closed_for_excessively_nested_annotated_tags():
    shas = [f"{index:040x}" for index in range(1, update_cmd.MAX_TAG_DEREFERENCE_DEPTH + 2)]
    tag_objects = {
        sha: {"tag": TAG, "object": {"type": "tag", "sha": next_sha}}
        for sha, next_sha in zip(shas[:-1], shas[1:], strict=True)
    }
    tag_ref = {"ref": f"refs/tags/{TAG}", "object": {"type": "tag", "sha": shas[0]}}

    with pytest.raises(update_cmd.UpdateError, match="depth"):
        update_cmd.resolve_release(
            _Http(_release(_manifest()), _manifest(), tag_ref=tag_ref, tag_objects=tag_objects), latest=True
        )


def test_github_api_url_allowlist_only_adds_exact_tag_ref_and_tag_object_endpoints():
    sha = "a" * 40

    assert update_cmd._is_github_api_url(REF_URL)
    assert update_cmd._is_github_api_url(TAG_OBJECT_URL + sha)
    assert update_cmd._is_github_api_url("https://api.github.com/repos/escoffier-labs/brigade/releases/latest")
    assert not update_cmd._is_github_api_url("https://api.github.com/repos/escoffier-labs/brigade/commits/main")
    assert not update_cmd._is_github_api_url(
        f"https://api.github.com/repos/escoffier-labs/brigade/commits/{sha}/check-runs?per_page=100&page=1"
    )
    assert not update_cmd._is_github_api_url(f"https://api.github.com/repos/other/brigade/git/ref/tags/{TAG}")
    assert not update_cmd._is_github_api_url(
        f"https://api.github.com/repos/escoffier-labs/brigade/git/ref/tags/{TAG}?x=1"
    )
    assert not update_cmd._is_github_api_url(f"https://api.github.com/repos/escoffier-labs/brigade/git/tags/{sha}?x=1")


def test_pypi_project_json_url_allowlist_is_exact():
    assert update_cmd._is_pypi_project_json_url(update_cmd.PYPI_PROJECT_JSON_URL)
    assert not update_cmd._is_pypi_project_json_url("https://pypi.org/pypi/brigade-cli/json?foo=1")
    assert not update_cmd._is_pypi_project_json_url("https://pypi.org/pypi/other/json")
    assert not update_cmd._is_pypi_project_json_url("http://pypi.org/pypi/brigade-cli/json")


def test_default_paths_uses_configured_pipx_bin_dir(monkeypatch, tmp_path):
    pipx_bin = tmp_path / "configured-pipx-bin"
    monkeypatch.setenv("PIPX_BIN_DIR", str(pipx_bin))

    paths = update_cmd.default_paths()

    expected_name = "brigade.exe" if update_cmd.os.name == "nt" else "brigade"
    assert paths.brigade_executable == pipx_bin / expected_name


def test_release_manifest_rejects_component_revision_mismatched_to_target_commit():
    manifest = json.loads(_manifest())
    manifest["components"]["sessionfind"]["component_revision"] = "b" * 40
    raw = (json.dumps(manifest, sort_keys=True) + "\n").encode()
    release = update_cmd.ResolvedRelease(
        42,
        TAG,
        VERSION,
        "a" * 40,
        BASE + "component-manifest-v1.json",
        len(raw),
        hashlib.sha256(raw).hexdigest(),
        raw,
    )

    with pytest.raises(update_cmd.UpdateError, match="component_revision.*target commit"):
        update_cmd.validate_release_manifest_bytes(release)


def test_beta_update_installs_exact_dev_wheel_and_reuses_verified_stable_manifest(tmp_path):
    manifest = _manifest()
    commands = []
    installed_binary = tmp_path / "bin" / "brigade"
    paths = update_cmd.UpdatePaths(tmp_path / "data", tmp_path / "cache", installed_binary)

    assert (
        update_cmd.run_update(
            channel="beta",
            paths=paths,
            http=_PypiHttp(_release(manifest), manifest),
            runner=lambda argv: commands.append(argv) or 0,
            now=lambda: "2026-07-20T12:00:00+00:00",
        )
        == 0
    )
    cache_path = paths.cache_root / "brigade" / "release-manifests" / f"{hashlib.sha256(manifest).hexdigest()}.json"
    assert cache_path.read_bytes() == manifest
    assert commands == [
        ["pipx", "install", "--force", f"brigadestack=={BETA_VERSION}"],
        [
            str(installed_binary),
            "setup",
            "--manifest",
            str(cache_path),
            "--allow-compatible-stable-manifest",
            VERSION,
        ],
    ]
    state = update_cmd.load_update_state(paths.data_root / "brigade" / "update-state.json")
    assert state is not None
    assert state.channel == "beta"
    assert state.cli_coordinate == BETA_VERSION
    assert (state.component_release_id, state.component_tag) == (42, TAG)


def test_beta_update_accepts_pre_agent_notify_stable_manifest(tmp_path):
    # The previous test is a false positive: its fixture gives every current
    # component full assets. A real beta handoff right after agent-notify enters
    # KNOWN_COMPONENT_IDS on main but before the first stable release publishing
    # it downloads the last stable manifest, which has no agent-notify entry.
    manifest = _pre_agent_notify_manifest()
    commands = []
    installed_binary = tmp_path / "bin" / "brigade"
    paths = update_cmd.UpdatePaths(tmp_path / "data", tmp_path / "cache", installed_binary)

    assert (
        update_cmd.run_update(
            channel="beta",
            paths=paths,
            http=_PypiHttp(_release(manifest), manifest),
            runner=lambda argv: commands.append(argv) or 0,
            now=lambda: "2026-07-20T12:00:00+00:00",
        )
        == 0
    )
    cache_path = paths.cache_root / "brigade" / "release-manifests" / f"{hashlib.sha256(manifest).hexdigest()}.json"
    assert cache_path.read_bytes() == manifest
    assert commands == [
        ["pipx", "install", "--force", f"brigadestack=={BETA_VERSION}"],
        [
            str(installed_binary),
            "setup",
            "--manifest",
            str(cache_path),
            "--allow-compatible-stable-manifest",
            VERSION,
        ],
    ]
    state = update_cmd.load_update_state(paths.data_root / "brigade" / "update-state.json")
    assert state is not None
    assert state.channel == "beta"
    assert state.cli_coordinate == BETA_VERSION
    assert (state.component_release_id, state.component_tag) == (42, TAG)


def test_release_manifest_still_rejects_missing_agent_notify_without_beta_handoff():
    manifest = _pre_agent_notify_manifest()
    release = update_cmd.ResolvedRelease(
        42,
        TAG,
        VERSION,
        "a" * 40,
        BASE + "component-manifest-v1.json",
        len(manifest),
        hashlib.sha256(manifest).hexdigest(),
        manifest,
    )

    # Strict validation (no compatibility handoff) rejects the missing
    # agent-notify entry, preserving the stable/release invariant.
    # brigade-hub is optional, so only agent-notify is reported missing.
    with pytest.raises(ValueError, match="missing required components: agent-notify$"):
        update_cmd.validate_release_manifest_bytes(release)

    # The narrowly named compatibility mode is the only path that accepts it,
    # and only because agent-notify and brigade-hub are registered unpublished.
    accepted = update_cmd.validate_release_manifest_bytes(release, allow_compatible_stable_manifest=True)
    assert "agent-notify" not in accepted.components
    assert set(accepted.components) == set(component_manifest.KNOWN_COMPONENT_IDS) - {"agent-notify", "brigade-hub"}


def test_release_manifest_compatibility_mode_rejects_missing_published_component():
    manifest = json.loads(_pre_agent_notify_manifest())
    del manifest["components"]["brigade-evidence"]
    raw = (json.dumps(manifest, sort_keys=True) + "\n").encode()
    release = update_cmd.ResolvedRelease(
        42,
        TAG,
        VERSION,
        "a" * 40,
        BASE + "component-manifest-v1.json",
        len(raw),
        hashlib.sha256(raw).hexdigest(),
        raw,
    )

    with pytest.raises(ValueError, match="missing required components: brigade-evidence"):
        update_cmd.validate_release_manifest_bytes(release, allow_compatible_stable_manifest=True)


def _install_update_test_engines(tmp_path, paths, manifest_bytes, monkeypatch, *, version=VERSION, payload_suffix=b""):
    monkeypatch.setattr("brigade.__version__", version)
    monkeypatch.setattr(component_manifest, "platform_key", lambda: "linux-amd64")
    monkeypatch.setattr(component_install, "run_post_install_smoke", lambda *args, **kwargs: None)
    roots = component_install.SetupRoots(str(paths.data_root), str(paths.cache_root), {})
    monkeypatch.setattr(component_install, "resolve_roots", lambda **kwargs: roots)
    manifest_path = tmp_path / "fixture-manifest.json"
    manifest_path.write_bytes(manifest_bytes)
    manifest = component_manifest.load(manifest_path)
    entries = [
        entry
        for entry in component_install.build_setup_plan(manifest, platform="linux-amd64", roots=roots)
        if entry.action == "verify-cache"
    ]
    for entry in entries:
        cache = Path(entry.cache_path)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(entry.asset_name.encode() + payload_suffix)
    assert component_install.setup_native_components(manifest_path=manifest_path, offline=True) == 0
    return entries


def test_beta_same_coordinates_are_a_noop(tmp_path, monkeypatch):
    manifest = _manifest()
    paths = update_cmd.UpdatePaths(tmp_path / "data", tmp_path / "cache", tmp_path / "bin" / "brigade")
    _install_update_test_engines(tmp_path, paths, manifest, monkeypatch)
    update_cmd.write_update_state(
        paths.data_root / "brigade" / "update-state.json",
        update_cmd.UpdateState(
            1,
            "beta",
            "brigade update",
            BETA_VERSION,
            42,
            TAG,
            "a" * 40,
            BASE + "component-manifest-v1.json",
            hashlib.sha256(manifest).hexdigest(),
            "2026-07-20T12:00:00+00:00",
        ),
    )
    calls = []
    assert (
        update_cmd.run_update(
            channel="beta",
            paths=paths,
            http=_PypiHttp(_release(manifest), manifest),
            runner=calls.append,
        )
        == 0
    )
    assert calls == []


def test_beta_update_migrates_git_sha_state_to_exact_wheel_version(tmp_path):
    manifest = _manifest()
    sha = "b" * 40
    paths = update_cmd.UpdatePaths(tmp_path / "data", tmp_path / "cache", tmp_path / "bin" / "brigade")
    state_path = paths.data_root / "brigade" / "update-state.json"
    update_cmd.write_update_state(
        state_path,
        update_cmd.UpdateState(
            1,
            "beta",
            "brigade update",
            sha,
            41,
            "v1.2.2",
            "a" * 40,
            "https://github.com/escoffier-labs/brigade/releases/download/v1.2.2/component-manifest-v1.json",
            "a" * 64,
            "2026-07-20T12:00:00+00:00",
        ),
    )
    assert update_cmd.is_legacy_beta_git_sha(sha)
    commands = []
    assert (
        update_cmd.run_update(
            channel="beta",
            paths=paths,
            http=_PypiHttp(_release(manifest), manifest),
            runner=lambda argv: commands.append(argv) or 0,
            now=lambda: "2026-07-20T12:05:00+00:00",
        )
        == 0
    )
    assert commands[0] == ["pipx", "install", "--force", f"brigadestack=={BETA_VERSION}"]
    state = update_cmd.load_update_state(state_path)
    assert state is not None
    assert state.channel == "beta"
    assert state.cli_coordinate == BETA_VERSION
    assert not update_cmd.is_legacy_beta_git_sha(state.cli_coordinate)


def test_beta_setup_failure_leaves_prior_update_state_unchanged(tmp_path):
    manifest = _manifest()
    paths = update_cmd.UpdatePaths(tmp_path / "data", tmp_path / "cache", tmp_path / "bin" / "brigade")
    state_path = paths.data_root / "brigade" / "update-state.json"
    update_cmd.write_update_state(
        state_path,
        update_cmd.UpdateState(
            1,
            "beta",
            "brigade update",
            "0.32.0.dev20260701",
            41,
            "v1.2.2",
            "a" * 40,
            "https://github.com/escoffier-labs/brigade/releases/download/v1.2.2/component-manifest-v1.json",
            "a" * 64,
            "2026-07-20T12:00:00+00:00",
        ),
    )
    before = state_path.read_bytes()
    returns = iter((0, 1))
    assert (
        update_cmd.run_update(
            channel="beta",
            paths=paths,
            http=_PypiHttp(_release(manifest), manifest),
            runner=lambda _argv: next(returns),
        )
        == 1
    )
    assert state_path.read_bytes() == before


def test_beta_dry_run_prints_exact_pipx_pin_and_mutates_nothing(tmp_path, capsys):
    manifest = _manifest()
    paths = update_cmd.UpdatePaths(tmp_path / "data", tmp_path / "cache", tmp_path / "bin" / "brigade")
    state_path = paths.data_root / "brigade" / "update-state.json"
    update_cmd.write_update_state(
        state_path,
        update_cmd.UpdateState(
            1,
            "beta",
            "brigade update",
            "b" * 40,
            41,
            "v1.2.2",
            "a" * 40,
            "https://github.com/escoffier-labs/brigade/releases/download/v1.2.2/component-manifest-v1.json",
            "a" * 64,
            "2026-07-20T12:00:00+00:00",
        ),
    )
    before = state_path.read_bytes()
    calls = []

    assert (
        update_cmd.run_update(
            channel="beta",
            dry_run=True,
            paths=paths,
            http=_PypiHttp(_release(manifest), manifest),
            runner=calls.append,
        )
        == 0
    )
    captured = capsys.readouterr().out
    assert f"pipx install --force brigadestack=={BETA_VERSION}" in captured
    assert str(paths.brigade_executable) in captured
    assert calls == []
    assert state_path.read_bytes() == before
    assert not paths.cache_root.exists()


def test_rollback_from_beta_to_stable_uses_stable_path_and_state(tmp_path):
    manifest = _manifest()
    paths = update_cmd.UpdatePaths(tmp_path / "data", tmp_path / "cache", tmp_path / "bin" / "brigade")
    update_cmd.write_update_state(
        paths.data_root / "brigade" / "update-state.json",
        update_cmd.UpdateState(
            1,
            "beta",
            "brigade update",
            BETA_VERSION,
            41,
            "v1.2.2",
            "a" * 40,
            "https://github.com/escoffier-labs/brigade/releases/download/v1.2.2/component-manifest-v1.json",
            "a" * 64,
            "2026-07-20T12:00:00+00:00",
        ),
    )
    commands = []
    assert (
        update_cmd.run_update(
            channel="stable",
            switch_channel=True,
            paths=paths,
            http=_Http(_release(manifest), manifest),
            runner=lambda argv: commands.append(argv) or 0,
            now=lambda: "2026-07-20T12:10:00+00:00",
        )
        == 0
    )
    assert commands[0] == ["pipx", "install", "--force", "brigadestack==1.2.3"]
    assert "--allow-compatible-stable-manifest" not in commands[1]
    state = update_cmd.load_update_state(paths.data_root / "brigade" / "update-state.json")
    assert state is not None
    assert state.channel == "stable"
    assert state.cli_coordinate == "1.2.3"
    assert state.component_tag == TAG


def test_stable_update_regression_still_pins_exact_release_without_pypi_lookup(tmp_path):
    manifest = _manifest()
    commands = []
    http = _Http(_release(manifest), manifest)
    assert (
        update_cmd.run_update(
            channel="stable",
            paths=update_cmd.UpdatePaths(tmp_path / "data", tmp_path / "cache", tmp_path / "bin" / "brigade"),
            http=http,
            runner=lambda argv: commands.append(argv) or 0,
            now=lambda: "2026-07-20T12:00:00+00:00",
        )
        == 0
    )
    assert commands[0] == ["pipx", "install", "--force", "brigadestack==1.2.3"]
    assert update_cmd.PYPI_PROJECT_JSON_URL not in http.urls
    assert all("/commits/main" not in url and "check-runs" not in url for url in http.urls)


def test_update_refuses_channel_takeover_without_switch_flag(tmp_path):
    path = tmp_path / "data" / "brigade" / "update-state.json"
    update_cmd.write_update_state(
        path,
        update_cmd.UpdateState(
            1,
            "stable",
            "brigade update",
            "1.2.3",
            42,
            TAG,
            "a" * 40,
            BASE + "component-manifest-v1.json",
            "a" * 64,
            "2026-07-20T12:00:00+00:00",
        ),
    )
    with pytest.raises(update_cmd.UpdateError, match="--switch-channel"):
        update_cmd.ensure_channel_ownership(update_cmd.load_update_state(path), "beta", switch_channel=False)


@pytest.mark.parametrize("drift", ["healthy", "missing", "older", "digest", "record", "failed"])
def test_same_immutable_coordinates_check_engines(tmp_path, monkeypatch, capsys, drift):
    manifest = _manifest()
    calls = []
    paths = update_cmd.UpdatePaths(tmp_path / "data", tmp_path / "cache", tmp_path / "bin" / "brigade")
    if drift == "older":
        older = json.loads(manifest.replace(b"v1.2.3", b"v1.2.2").replace(b'"1.2.3"', b'"1.2.2"'))
        for component in older["components"].values():
            for asset in component["assets"].values():
                payload = asset["asset_name"].encode() + b"-old"
                asset.update(byte_size=len(payload), sha256=hashlib.sha256(payload).hexdigest())
        entries = _install_update_test_engines(
            tmp_path, paths, json.dumps(older).encode(), monkeypatch, version="1.2.2", payload_suffix=b"-old"
        )
        monkeypatch.setattr("brigade.__version__", VERSION)
    else:
        entries = _install_update_test_engines(tmp_path, paths, manifest, monkeypatch)
    executable = Path(entries[0].managed_path)
    if drift in {"missing", "failed"}:
        executable.unlink()
    elif drift == "digest":
        executable.write_bytes(b"x" * entries[0].byte_size)
    state_path = paths.data_root / "brigade" / "update-state.json"
    update_cmd.write_update_state(
        state_path,
        update_cmd.UpdateState(
            1,
            "stable",
            "brigade update",
            VERSION,
            42,
            TAG,
            "a" * 40,
            BASE + "component-manifest-v1.json",
            hashlib.sha256(manifest).hexdigest(),
            "2026-07-20T12:00:00+00:00",
        ),
    )

    before_update = state_path.read_bytes()
    installed_path = Path(component_paths.installed_state_path(str(paths.data_root)))
    if drift == "record":
        mismatched = json.loads(installed_path.read_bytes())
        mismatched["components"][entries[0].component_id]["sha256"] = "f" * 64
        installed_path.write_text(json.dumps(mismatched), encoding="utf-8")
    before_installed = installed_path.read_bytes()
    if drift == "failed":

        def fail_smoke(*args, **kwargs):
            raise component_install.ComponentInstallError("smoke command exited 7")

        monkeypatch.setattr(component_install, "run_post_install_smoke", fail_smoke)

    def setup_only(argv):
        calls.append(argv)
        assert argv[1:3] == ["setup", "--manifest"]
        return component_install.setup_native_components(
            manifest_path=Path(argv[3]), opener=lambda url: BytesIO(url.rsplit("/", 1)[-1].encode())
        )

    http = _Http(_release(manifest), manifest)
    if drift == "healthy":
        release = update_cmd.resolve_release(http, latest=True)
        update_cmd._cache_manifest(paths, release)
        monkeypatch.setattr(http, "bytes", lambda _: pytest.fail("healthy update downloaded a manifest"))

    result = update_cmd.run_update(
        channel="stable",
        paths=paths,
        http=http,
        runner=setup_only,
        now=lambda: "2026-07-20T12:01:00+00:00",
    )
    assert state_path.read_bytes() == before_update
    if drift == "failed":
        assert result != 0
        assert "smoke command exited 7" in capsys.readouterr().err
        assert installed_path.read_bytes() == before_installed
        assert not executable.exists()
    else:
        assert result == 0
        assert json.loads(installed_path.read_bytes())["manifest_revision"] == json.loads(manifest)["manifest_revision"]
        assert executable.read_bytes() == entries[0].asset_name.encode()
        assert json.loads(installed_path.read_bytes())["components"][entries[0].component_id]["sha256"] == (
            hashlib.sha256(entries[0].asset_name.encode()).hexdigest()
        )
    assert len(calls) == (0 if drift == "healthy" else 1)


def test_dry_run_does_not_write_manifest_cache_or_state(tmp_path):
    manifest = _manifest()
    paths = update_cmd.UpdatePaths(tmp_path / "data", tmp_path / "cache", tmp_path / "bin" / "brigade")

    assert (
        update_cmd.run_update(
            channel="stable",
            dry_run=True,
            paths=paths,
            http=_Http(_release(manifest), manifest),
        )
        == 0
    )
    assert not paths.data_root.exists()
    assert not paths.cache_root.exists()


@pytest.mark.parametrize("channel,dry_run", [("stable", False), ("beta", False), ("stable", True)])
@pytest.mark.parametrize("provided_paths", [False, True])
def test_npm_update_refuses_before_side_effects(tmp_path, monkeypatch, capsys, channel, dry_run, provided_paths):
    from brigade import distribution

    monkeypatch.setattr(distribution, "_name", "npm")

    def forbidden(*args, **kwargs):
        pytest.fail("npm update crossed the side-effect boundary")

    paths = update_cmd.UpdatePaths(tmp_path / "data", tmp_path / "cache", tmp_path / "brigade")
    for name in (
        "default_paths",
        "_DefaultHttp",
        "_UpdateLock",
        "load_update_state",
        "_cache_manifest",
        "write_update_state",
    ):
        monkeypatch.setattr(update_cmd, name, forbidden)
    monkeypatch.setattr(update_cmd.component_paths, "update_state_path", forbidden)
    monkeypatch.setattr(update_cmd.component_paths, "update_lock_path", forbidden)
    http = _Http({}, b"")
    monkeypatch.setattr(http, "json", forbidden)
    monkeypatch.setattr(http, "bytes", forbidden)
    assert (
        update_cmd.run_update(
            channel=channel,
            dry_run=dry_run,
            switch_channel=True,
            paths=paths if provided_paths else None,
            http=http,
            runner=forbidden,
            now=forbidden,
        )
        != 0
    )
    assert not list(tmp_path.iterdir())
    message = capsys.readouterr().err.lower()
    assert "npm update --global brigadestack" in message
    assert "npm update --global @escoffier-labs/brigade" in message
    assert "brigade setup" in message


def test_distribution_identity_is_process_local_and_ignores_environment(monkeypatch):
    import os
    import subprocess
    import sys

    from brigade import distribution

    monkeypatch.setenv("BRIGADE_DISTRIBUTION", "npm")
    assert distribution._name is None
    assert distribution.is_npm() is False
    for name in ("pipx", "NPM", "npm"):
        monkeypatch.setattr(distribution, "_name", name)
        assert distribution.is_npm() is (name == "npm")
    # Exec starts a fresh interpreter with the caller's environment unchanged.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from brigade import distribution as d; assert d._name is None; assert d.is_npm() is False",
        ],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


CURRENT_SLUG = "escoffier-labs/brigade"
RENAMED_SLUG = "escoffier-labs/brigadestack"


class _GithubServing:
    """GitHub serving the repository under one name.

    A request under the name GitHub does not serve is answered with the typed
    rename signal, exactly as a redirect to the immutable repository ID is. The
    repository identity endpoint reports the serving name unless a test overrides
    it to model a different repository answering on that slug.
    """

    def __init__(self, serving: str, manifest_slug: str, *, repository_id=None, full_name=None):
        self.serving = serving
        self.manifest = _manifest().replace(CURRENT_SLUG.encode(), manifest_slug.encode())
        self.base = f"https://github.com/{serving}/releases/download/{TAG}/"
        self.repository_id = update_cmd.REPOSITORY_ID if repository_id is None else repository_id
        self.full_name = serving if full_name is None else full_name
        self.urls: list[str] = []

    def release(self) -> dict:
        release = _release(self.manifest)
        release["assets"][0]["browser_download_url"] = self.base + "component-manifest-v1.json"
        return release

    def json(self, url):
        self.urls.append(url)
        if url == f"https://api.github.com/repos/{self.serving}":
            return {"id": self.repository_id, "full_name": self.full_name}
        if f"/repos/{self.serving}/" not in url:
            raise update_cmd.RepositoryRenamedError(f"GitHub reports the repository was renamed: {url}")
        if url.endswith("/releases/latest"):
            return self.release()
        if url == f"https://api.github.com/repos/{self.serving}/git/ref/tags/{TAG}":
            return {"ref": f"refs/tags/{TAG}", "object": {"type": "commit", "sha": "a" * 40}}
        raise AssertionError(url)

    def bytes(self, url):
        self.urls.append(url)
        assert url == self.base + "component-manifest-v1.json"
        return self.manifest


def _write_previous_state(paths, slug):
    update_cmd.write_update_state(
        paths.data_root / "brigade" / "update-state.json",
        update_cmd.UpdateState(
            1,
            "stable",
            "brigade update",
            "1.2.2",
            41,
            "v1.2.2",
            "b" * 40,
            f"https://github.com/{slug}/releases/download/v1.2.2/component-manifest-v1.json",
            "c" * 64,
            "2026-07-01T00:00:00+00:00",
        ),
    )


@pytest.mark.parametrize("stored", [None, CURRENT_SLUG, RENAMED_SLUG])
@pytest.mark.parametrize(
    "serving,manifest_slug",
    [
        (CURRENT_SLUG, CURRENT_SLUG),  # today: repository named brigade
        (RENAMED_SLUG, CURRENT_SLUG),  # renamed repository, release published before the rename
        (RENAMED_SLUG, RENAMED_SLUG),  # renamed repository, release published after the rename
    ],
)
def test_update_works_under_either_repository_name_and_either_stored_state(tmp_path, serving, manifest_slug, stored):
    paths = update_cmd.UpdatePaths(tmp_path / "data", tmp_path / "cache", tmp_path / "bin" / "brigade")
    if stored is not None:
        _write_previous_state(paths, stored)
    http = _GithubServing(serving, manifest_slug)
    commands = []

    result = update_cmd.run_update(
        channel="stable",
        paths=paths,
        http=http,
        runner=lambda argv: commands.append(argv) or 0,
        now=lambda: "2026-07-20T12:00:00+00:00",
    )

    assert result == 0
    assert commands[0] == ["pipx", "install", "--force", "brigadestack==1.2.3"]
    state = update_cmd.load_update_state(paths.data_root / "brigade" / "update-state.json")
    assert state is not None
    assert state.component_tag == TAG
    assert state.component_manifest_url == http.base + "component-manifest-v1.json"
    # The current name is requested first. Under the renamed repository that first request is
    # reported as a rename and the next slug answers only after its repository identity is checked.
    assert http.urls[0] == f"https://api.github.com/repos/{CURRENT_SLUG}/releases/latest"
    if serving != CURRENT_SLUG:
        assert f"https://api.github.com/repos/{serving}" in http.urls


def test_release_lookup_reports_the_first_error_when_no_slug_answers():
    class _Down:
        def json(self, url):
            raise update_cmd.UpdateError(f"GitHub request failed: {url} HTTP 500")

    with pytest.raises(update_cmd.UpdateError, match=f"repos/{CURRENT_SLUG}/"):
        update_cmd.resolve_release(_Down(), latest=True)


@pytest.mark.parametrize(
    "final_url",
    [
        "https://api.github.com/repos/escoffier-labs/brigade-site/releases/latest",
        "https://api.github.com/repos/someone-else/brigade/releases/latest",
    ],
)
def test_redirect_to_an_unrelated_repository_is_still_refused(final_url):
    # A redirect to the pinned repository ID is covered separately: it is the only
    # redirect treated as a rename signal, and only for the known slugs and suffix.
    url = f"https://api.github.com/repos/{CURRENT_SLUG}/releases/latest"
    with patch("urllib.request.OpenerDirector.open", return_value=_Response(b"{}", final_url)):
        with pytest.raises(update_cmd.UpdateError, match="GitHub API request redirected"):
            update_cmd._DefaultHttp().json(url)


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_default_http_http_status_failure_is_not_a_rename_signal(status):
    url = f"https://api.github.com/repos/{CURRENT_SLUG}/releases/latest"

    def urlopen(request, *, timeout):
        raise urllib.error.HTTPError(url, status, "failure", None, None)

    with patch("urllib.request.OpenerDirector.open", side_effect=urlopen):
        with pytest.raises(update_cmd.UpdateError) as raised:
            update_cmd._DefaultHttp().json(url)

    assert type(raised.value) is update_cmd.UpdateError


def test_default_http_url_error_and_timeout_are_not_rename_signals():
    url = f"https://api.github.com/repos/{CURRENT_SLUG}/releases/latest"
    for error in (urllib.error.URLError("connection refused"), TimeoutError("timed out")):
        with patch("urllib.request.OpenerDirector.open", side_effect=error):
            with pytest.raises((update_cmd.UpdateError, OSError)) as raised:
                update_cmd._DefaultHttp().json(url)
        assert not isinstance(raised.value, update_cmd.RepositoryRenamedError)


def test_default_http_invalid_json_is_not_a_rename_signal():
    url = f"https://api.github.com/repos/{CURRENT_SLUG}/releases/latest"
    with patch("urllib.request.OpenerDirector.open", return_value=_Response(b"not json", url)):
        with pytest.raises(update_cmd.UpdateError, match="invalid JSON") as raised:
            update_cmd._DefaultHttp().json(url)
    assert type(raised.value) is update_cmd.UpdateError


@pytest.mark.parametrize("slug", [CURRENT_SLUG, RENAMED_SLUG])
def test_default_http_reports_a_verified_rename_redirect_as_the_typed_signal(slug):
    url = f"https://api.github.com/repos/{slug}/releases/latest"
    final_url = "https://api.github.com/repositories/1238165856/releases/latest"
    with patch("urllib.request.OpenerDirector.open", return_value=_Response(b"{}", final_url)):
        with pytest.raises(update_cmd.RepositoryRenamedError, match="repository was renamed"):
            update_cmd._DefaultHttp().json(url)


@pytest.mark.parametrize(
    "final_url",
    [
        "https://api.github.com/repos/escoffier-labs/brigade-site/releases/latest",
        "https://api.github.com/repos/someone-else/brigade/releases/latest",
        "https://api.github.com/repositories/1238165855/releases/latest",
        "https://api.github.com/repositories/1238165856/releases/tags/v9.9.9",
        "https://api.github.com/repositories/1238165856/releases/latest?page=2",
        "https://api.github.com/repositories/1238165856/releases/latest#fragment",
    ],
)
def test_default_http_keeps_every_other_redirect_as_a_plain_refusal(final_url):
    url = f"https://api.github.com/repos/{CURRENT_SLUG}/releases/latest"
    with patch("urllib.request.OpenerDirector.open", return_value=_Response(b"{}", final_url)):
        with pytest.raises(update_cmd.UpdateError, match="redirected outside its exact expected URL") as raised:
            update_cmd._DefaultHttp().json(url)
    assert type(raised.value) is update_cmd.UpdateError


def test_redirect_between_the_two_slugs_is_refused_by_the_exact_url_rule():
    url = f"https://api.github.com/repos/{CURRENT_SLUG}/releases/latest"
    final_url = f"https://api.github.com/repos/{RENAMED_SLUG}/releases/latest"
    with patch("urllib.request.OpenerDirector.open", return_value=_Response(b"{}", final_url)):
        with pytest.raises(update_cmd.UpdateError, match="GitHub API request redirected"):
            update_cmd._DefaultHttp().json(url)


def test_api_and_release_url_checks_accept_both_slugs_and_no_others():
    for slug in (CURRENT_SLUG, RENAMED_SLUG):
        assert update_cmd._is_github_api_url(f"https://api.github.com/repos/{slug}")
        assert not update_cmd._is_github_api_url(f"https://api.github.com/repos/{slug}?x=1")
        assert update_cmd._is_github_api_url(f"https://api.github.com/repos/{slug}/releases/latest")
        assert update_cmd._is_github_api_url(f"https://api.github.com/repos/{slug}/git/ref/tags/{TAG}")
        assert not update_cmd._is_github_api_url(f"https://api.github.com/repos/{slug}/commits/main")
        assert not update_cmd._is_github_api_url(f"https://api.github.com/repos/{slug}/releases/latest?x=1")
        assert update_cmd._is_release_url(
            f"https://github.com/{slug}/releases/download/{TAG}/component-manifest-v1.json",
            TAG,
            "component-manifest-v1.json",
        )
    for slug in ("escoffier-labs/brigade-site", "someone-else/brigade"):
        assert not update_cmd._is_github_api_url(f"https://api.github.com/repos/{slug}")
        assert not update_cmd._is_github_api_url(f"https://api.github.com/repos/{slug}/releases/latest")
        assert not update_cmd._is_release_url(
            f"https://github.com/{slug}/releases/download/{TAG}/component-manifest-v1.json",
            TAG,
            "component-manifest-v1.json",
        )


def test_stored_state_naming_an_unrelated_repository_is_rejected(tmp_path):
    paths = update_cmd.UpdatePaths(tmp_path / "data", tmp_path / "cache", tmp_path / "bin" / "brigade")
    _write_previous_state(paths, "escoffier-labs/brigade-site")

    assert update_cmd.load_update_state(paths.data_root / "brigade" / "update-state.json") is None


def test_release_manifest_naming_an_unrelated_repository_is_rejected():
    manifest = json.loads(_manifest())
    manifest["components"]["sessionfind"]["source"]["repository"] = "escoffier-labs/brigade-site"
    raw = (json.dumps(manifest, sort_keys=True) + "\n").encode()
    release = update_cmd.ResolvedRelease(
        42, TAG, VERSION, "a" * 40, BASE + "component-manifest-v1.json", len(raw), hashlib.sha256(raw).hexdigest(), raw
    )

    with pytest.raises(update_cmd.UpdateError, match="outside the resolved Brigade release"):
        update_cmd.validate_release_manifest_bytes(release)


def test_stored_state_under_either_slug_for_the_same_release_compares_equal():
    def state(slug):
        return update_cmd.UpdateState(
            1,
            "stable",
            "brigade update",
            VERSION,
            42,
            TAG,
            "a" * 40,
            f"https://github.com/{slug}/releases/download/{TAG}/component-manifest-v1.json",
            "a" * 64,
            "2026-07-20T12:00:00+00:00",
        )

    assert update_cmd._same_coordinates(state(CURRENT_SLUG), state(RENAMED_SLUG))


class _FailingHttp:
    def __init__(self, error):
        self.error = error
        self.urls: list[str] = []

    def json(self, url):
        self.urls.append(url)
        raise self.error

    def bytes(self, url):
        raise AssertionError(url)


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(update_cmd.UpdateError("GitHub request failed: HTTP 401"), id="http-401"),
        pytest.param(update_cmd.UpdateError("GitHub request failed: HTTP 403"), id="http-403"),
        pytest.param(update_cmd.UpdateError("GitHub request failed: HTTP 429"), id="http-429"),
        pytest.param(update_cmd.UpdateError("GitHub request failed: HTTP 500"), id="http-500"),
        pytest.param(urllib.error.URLError("connection refused"), id="url-error"),
        pytest.param(TimeoutError("timed out"), id="timeout"),
        pytest.param(update_cmd.UpdateError("GitHub request returned invalid JSON"), id="invalid-json"),
        pytest.param(
            update_cmd.UpdateError(
                "GitHub API request redirected outside its exact expected URL: "
                "https://api.github.com/repos/escoffier-labs/brigade-site/releases/latest"
            ),
            id="redirect-other-repository",
        ),
        pytest.param(
            update_cmd.UpdateError(
                "GitHub API request redirected outside its exact expected URL: "
                "https://api.github.com/repositories/1238165855/releases/latest"
            ),
            id="redirect-different-id",
        ),
        pytest.param(
            update_cmd.UpdateError(
                "GitHub API request redirected outside its exact expected URL: "
                "https://api.github.com/repositories/1238165856/releases/tags/v9.9.9"
            ),
            id="redirect-different-suffix",
        ),
        pytest.param(
            update_cmd.UpdateError(
                "GitHub API request redirected outside its exact expected URL: "
                "https://api.github.com/repositories/1238165856/releases/latest?page=2"
            ),
            id="redirect-with-query",
        ),
    ],
)
def test_release_lookup_raises_the_first_slug_error_without_trying_the_second(error):
    http = _FailingHttp(error)

    with pytest.raises(type(error)) as raised:
        update_cmd._lookup_release(http, "latest")

    assert raised.value is error
    assert http.urls == [f"https://api.github.com/repos/{CURRENT_SLUG}/releases/latest"]


class _RenamedHttp:
    """First slug reports a rename; the second slug reports the supplied identity."""

    def __init__(self, identity):
        self.identity = identity
        self.urls: list[str] = []

    def json(self, url):
        self.urls.append(url)
        if url == f"https://api.github.com/repos/{CURRENT_SLUG}/releases/latest":
            raise update_cmd.RepositoryRenamedError("GitHub reports the repository was renamed")
        if url == f"https://api.github.com/repos/{RENAMED_SLUG}":
            return self.identity
        if url == f"https://api.github.com/repos/{RENAMED_SLUG}/releases/latest":
            return {"tag_name": TAG}
        raise AssertionError(url)

    def bytes(self, url):
        raise AssertionError(url)


def test_release_lookup_verifies_the_second_slug_identity_before_using_its_release():
    http = _RenamedHttp({"id": 1238165856, "full_name": RENAMED_SLUG})

    raw, repository = update_cmd._lookup_release(http, "latest")

    assert (raw, repository) == ({"tag_name": TAG}, RENAMED_SLUG)
    assert http.urls == [
        f"https://api.github.com/repos/{CURRENT_SLUG}/releases/latest",
        f"https://api.github.com/repos/{RENAMED_SLUG}",
        f"https://api.github.com/repos/{RENAMED_SLUG}/releases/latest",
    ]


@pytest.mark.parametrize(
    "identity",
    [
        pytest.param({"id": 1238165855, "full_name": RENAMED_SLUG}, id="different-id"),
        pytest.param({"id": 1238165856, "full_name": CURRENT_SLUG}, id="different-full-name"),
        pytest.param({"id": 1238165856, "full_name": "Escoffier-Labs/brigadestack"}, id="full-name-case"),
        pytest.param({"id": 1238165856}, id="missing-full-name"),
        pytest.param("not-an-object", id="not-an-object"),
    ],
)
def test_release_lookup_refuses_the_second_slug_when_its_identity_differs(identity):
    http = _RenamedHttp(identity)

    with pytest.raises(update_cmd.UpdateError, match="GitHub repository"):
        update_cmd._lookup_release(http, "latest")

    assert f"https://api.github.com/repos/{RENAMED_SLUG}/releases/latest" not in http.urls


def test_rename_retry_checks_the_identity_then_uses_the_second_slug(tmp_path):
    paths = update_cmd.UpdatePaths(tmp_path / "data", tmp_path / "cache", tmp_path / "bin" / "brigade")
    http = _GithubServing(RENAMED_SLUG, RENAMED_SLUG)

    result = update_cmd.run_update(
        channel="stable",
        paths=paths,
        http=http,
        runner=lambda argv: 0,
        now=lambda: "2026-07-20T12:00:00+00:00",
    )

    assert result == 0
    assert http.urls == [
        f"https://api.github.com/repos/{CURRENT_SLUG}/releases/latest",
        f"https://api.github.com/repos/{RENAMED_SLUG}",
        f"https://api.github.com/repos/{RENAMED_SLUG}/releases/latest",
        f"https://api.github.com/repos/{RENAMED_SLUG}/git/ref/tags/{TAG}",
        http.base + "component-manifest-v1.json",
    ]
    state = update_cmd.load_update_state(paths.data_root / "brigade" / "update-state.json")
    assert state is not None
    assert state.component_release_id == 42
    assert state.component_manifest_url == http.base + "component-manifest-v1.json"


@pytest.mark.parametrize(
    "identity",
    [
        pytest.param({"repository_id": 1238165855}, id="different-id"),
        pytest.param({"full_name": "someone-else/brigadestack"}, id="different-full-name"),
        pytest.param({"full_name": "Escoffier-Labs/brigadestack"}, id="full-name-case"),
    ],
)
def test_rename_retry_refuses_a_second_slug_with_a_different_identity(tmp_path, identity):
    paths = update_cmd.UpdatePaths(tmp_path / "data", tmp_path / "cache", tmp_path / "bin" / "brigade")
    http = _GithubServing(RENAMED_SLUG, RENAMED_SLUG, **identity)
    commands = []

    result = update_cmd.run_update(
        channel="stable",
        paths=paths,
        http=http,
        runner=lambda argv: commands.append(argv) or 0,
        now=lambda: "2026-07-20T12:00:00+00:00",
    )

    assert result == 1
    assert commands == []
    assert not (paths.data_root / "brigade" / "update-state.json").exists()
    assert f"https://api.github.com/repos/{RENAMED_SLUG}/releases/latest" not in http.urls


def test_current_name_makes_the_same_requests_as_before_plus_one_repository_identity_check(tmp_path):
    paths = update_cmd.UpdatePaths(tmp_path / "data", tmp_path / "cache", tmp_path / "bin" / "brigade")
    http = _GithubServing(CURRENT_SLUG, CURRENT_SLUG)

    result = update_cmd.run_update(
        channel="stable",
        paths=paths,
        http=http,
        runner=lambda argv: 0,
        now=lambda: "2026-07-20T12:00:00+00:00",
    )

    assert result == 0
    assert http.urls == [
        f"https://api.github.com/repos/{CURRENT_SLUG}/releases/latest",
        # New: a name that answers directly is trusted only while it still resolves to the repository ID.
        f"https://api.github.com/repos/{CURRENT_SLUG}",
        f"https://api.github.com/repos/{CURRENT_SLUG}/git/ref/tags/{TAG}",
        BASE + "component-manifest-v1.json",
    ]


def _run_stable_update(tmp_path, channel="stable"):
    manifest = _manifest()
    commands = []

    def runner(argv):
        commands.append(argv)
        return 0

    result = update_cmd.run_update(
        channel=channel,
        paths=update_cmd.UpdatePaths(
            data_root=tmp_path / "data", cache_root=tmp_path / "cache", brigade_executable=tmp_path / "bin" / "brigade"
        ),
        http=_Http(_release(manifest), manifest),
        runner=runner,
        now=lambda: "2026-07-20T12:00:00+00:00",
    )
    return result, commands


def _make_env(venvs, venv_entries=(), metadata="brigadestack"):
    """A fake brigadestack pipx environment under ``venvs``; ``metadata`` is the package pipx_metadata.json names."""
    env = venvs / "brigadestack"
    venv_bin = update_cmd._venv_bin_dir(venvs)  # bin on POSIX, Scripts on Windows
    venv_bin.mkdir(parents=True)
    for name in venv_entries:
        (venv_bin / name).write_text("#!/bin/sh\n")
    if metadata == "garbage":
        (env / "pipx_metadata.json").write_text("not json")
    elif metadata is not None:
        (env / "pipx_metadata.json").write_text(json.dumps({"main_package": {"package": metadata}}))
    return env


def _migrate(
    tmp_path,
    monkeypatch,
    channel,
    runner,
    *,
    windows=False,
    venv_entries=(),
    metadata="brigadestack",
    reported=None,
    capture=None,
    venvs=None,
    make_env=True,
):
    """Run an update of a brigade-cli pipx app and return (result, paths) with a fake pipx layout.

    By default the venvs directory is configured explicitly. Pass ``capture`` (what `pipx environment` prints)
    to make the update ask pipx instead, with ``venvs`` as the directory the environment is built under.
    """
    monkeypatch.setattr(update_cmd, "installed_pypi_project", lambda: "brigade-cli")
    monkeypatch.setattr(update_cmd, "_pipx_copies_apps", lambda: windows)
    (tmp_path / "bin").mkdir()
    venvs = venvs or tmp_path / "venvs"
    paths = update_cmd.UpdatePaths(
        data_root=tmp_path / "data",
        cache_root=tmp_path / "cache",
        brigade_executable=tmp_path / "bin" / "brigade",
        pipx_venvs=None if capture is not None else venvs,
    )
    if make_env:
        _make_env(venvs, venv_entries, metadata)
    manifest = _manifest()
    http = _Http(_release(manifest), manifest) if channel == "stable" else _PypiHttp(_release(manifest), manifest)
    result = update_cmd.run_update(
        channel=channel,
        paths=paths,
        http=http,
        runner=runner,
        now=lambda: "2026-07-20T12:00:00+00:00",
        capture=capture,
    )
    return result, paths


def _version(channel):
    return "1.2.3" if channel == "stable" else BETA_VERSION


def _no_state(tmp_path):
    return not list((tmp_path / "data").rglob("*state*.json"))


@pytest.mark.parametrize("channel", ["stable", "beta"])
def test_update_migrates_a_brigade_cli_install_and_runs_setup_from_the_new_environment(tmp_path, monkeypatch, channel):
    # brigade-cli's last release becomes a stub, so an update must never install it over a working CLI.
    commands = []
    result, paths = _migrate(tmp_path, monkeypatch, channel, lambda argv: commands.append(argv) or 0)
    assert result == 0
    install = ["pipx", "install", "--force", f"brigadestack=={_version(channel)}"]
    assert commands[:3] == [install, ["pipx", "uninstall", "brigade-cli"], install]
    # Setup runs from the brigadestack environment, not the bin-directory link the removal may have taken.
    expected = update_cmd._venv_bin_dir(tmp_path / "venvs") / ("brigade.exe" if os.name == "nt" else "brigade")
    assert commands[3][:2] == [str(expected), "setup"]
    assert str(paths.brigade_executable) not in commands[3]
    assert len(commands) == 4
    assert list((tmp_path / "data").rglob("*state*.json"))


@pytest.mark.parametrize("channel", ["stable", "beta"])
def test_update_records_nothing_when_the_old_app_cannot_be_removed(tmp_path, monkeypatch, capsys, channel):
    commands = []

    def runner(argv):
        commands.append(argv)
        return 1 if argv[1] == "uninstall" else 0

    result, _paths = _migrate(tmp_path, monkeypatch, channel, runner)
    assert result == 1
    # The second install still runs to restore launchers a partial removal took; setup and state do not.
    install = ["pipx", "install", "--force", f"brigadestack=={_version(channel)}"]
    assert commands == [install, ["pipx", "uninstall", "brigade-cli"], install]
    assert _no_state(tmp_path)
    err = capsys.readouterr().err
    assert f"Finish with: pipx uninstall brigade-cli ; {' '.join(install)}" in err
    assert "then run brigade update again" in err


@pytest.mark.skipif(os.name == "nt", reason="pipx copies launchers on Windows, so nothing is relinked there")
@pytest.mark.parametrize("channel", ["stable", "beta"])
def test_failed_second_install_relinks_the_launchers_locally_and_records_nothing(
    tmp_path, monkeypatch, capsys, channel
):
    commands = []

    def runner(argv):
        commands.append(argv)
        return 1 if len(commands) == 3 else 0  # the install after the uninstall fails

    entries = ("brigade", "brigadestack", "brigade-hook")
    result, paths = _migrate(tmp_path, monkeypatch, channel, runner, venv_entries=entries)
    assert result == 1
    assert len(commands) == 3  # no setup
    assert _no_state(tmp_path)
    for name in entries:
        link = paths.brigade_executable.parent / name
        assert link.is_symlink()
        assert link.resolve() == (update_cmd._venv_bin_dir(tmp_path / "venvs") / name).resolve()
    assert not list((tmp_path / "bin").glob(".*update-link"))
    err = capsys.readouterr().err
    assert "relinked to the existing brigadestack environment" in err
    assert f"Restore with: pipx install --force brigadestack=={_version(channel)}" in err


def test_failed_second_install_replaces_a_stale_launcher_and_reports_when_nothing_can_be_relinked(
    tmp_path, monkeypatch, capsys
):
    commands = []

    def runner(argv):
        commands.append(argv)
        return 1 if len(commands) == 3 else 0

    result, paths = _migrate(tmp_path, monkeypatch, "stable", runner)  # no brigadestack environment entries
    assert result == 1
    assert not (paths.brigade_executable.parent / "brigade").exists()
    err = capsys.readouterr().err
    assert "no brigadestack environment could be relinked" in err
    assert "Restore with: pipx install --force brigadestack==1.2.3" in err


@pytest.mark.parametrize("channel", ["stable", "beta"])
def test_windows_migration_leaves_the_old_app_and_records_that_retirement_is_pending(
    tmp_path, monkeypatch, capsys, channel
):
    # Copy-mode pipx (Windows) uninstalls `brigade.exe` by name and cannot delete the running
    # interpreter, so the update must not remove the app it is running from. State is not written, or the
    # next update would stop at "already owns" and never retire the old app.
    commands = []
    result, _paths = _migrate(tmp_path, monkeypatch, channel, lambda argv: commands.append(argv) or 0, windows=True)
    assert result == 1
    assert commands[0] == ["pipx", "install", "--force", f"brigadestack=={_version(channel)}"]
    assert commands[1][1] == "setup"
    assert all(command[1] != "uninstall" for command in commands)
    assert _no_state(tmp_path)
    err = capsys.readouterr().err
    assert "retiring the old brigade-cli app is pending" in err
    assert "pipx uninstall brigade-cli ; pipx install --force brigadestack==" in err


def test_stable_update_upgrades_a_brigadestack_install_under_its_own_name(tmp_path, monkeypatch):
    monkeypatch.setattr(update_cmd, "installed_pypi_project", lambda: "brigadestack")
    result, commands = _run_stable_update(tmp_path)
    assert result == 0
    assert commands[0] == ["pipx", "install", "--force", "brigadestack==1.2.3"]


def test_beta_update_resolves_brigadestack_wheels_even_on_a_brigade_cli_install():
    # Development wheels are published only for the real package.
    assert update_cmd.PYPI_PROJECT_JSON_URL == "https://pypi.org/pypi/brigadestack/json"


def test_beta_update_of_a_brigadestack_install_removes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(update_cmd, "installed_pypi_project", lambda: "brigadestack")
    manifest = _manifest()
    commands = []
    result = update_cmd.run_update(
        channel="beta",
        paths=update_cmd.UpdatePaths(
            data_root=tmp_path / "data", cache_root=tmp_path / "cache", brigade_executable=tmp_path / "bin" / "brigade"
        ),
        http=_PypiHttp(_release(manifest), manifest),
        runner=lambda argv: commands.append(argv) or 0,
        now=lambda: "2026-07-20T12:00:00+00:00",
    )
    assert result == 0
    assert all(command[1] != "uninstall" for command in commands)


PIPX_QUERY = ["pipx", "environment", "--value", "PIPX_LOCAL_VENVS"]


def _setup_executable(venvs):
    return update_cmd._venv_bin_dir(venvs) / ("brigade.exe" if os.name == "nt" else "brigade")


def _ok(commands):
    return lambda argv: commands.append(argv) or 0


def test_the_pipx_reported_venvs_directory_is_used_and_the_environment_variable_is_ignored(tmp_path, monkeypatch):
    # pipx derives its venvs directory from PIPX_HOME only; PIPX_LOCAL_VENVS is not an input to it.
    reported, stale = tmp_path / "reported" / "venvs", tmp_path / "stale" / "venvs"
    monkeypatch.setenv("PIPX_LOCAL_VENVS", str(stale))
    _make_env(stale)  # a perfectly valid environment, in the wrong place
    queries, commands = [], []

    def capture(argv):
        queries.append(argv)
        return f"{reported}\n"

    result, _ = _migrate(tmp_path, monkeypatch, "stable", _ok(commands), capture=capture, venvs=reported)
    assert result == 0
    assert queries == [PIPX_QUERY]  # an argument list, asked of the configured pipx executable
    assert commands[3][0] == str(_setup_executable(reported))
    assert str(stale) not in " ".join(commands[3])


def test_the_environment_variable_never_chooses_the_venvs_directory_when_pipx_cannot_be_asked(tmp_path, monkeypatch):
    home, stale = tmp_path / "home", tmp_path / "stale" / "venvs"
    monkeypatch.setenv("PIPX_HOME", str(home))
    monkeypatch.setenv("PIPX_LOCAL_VENVS", str(stale))
    _make_env(stale)
    commands = []
    result, _ = _migrate(
        tmp_path, monkeypatch, "stable", _ok(commands), capture=lambda argv: None, venvs=home / "venvs"
    )
    assert result == 0
    assert commands[3][0] == str(_setup_executable(home / "venvs"))


@pytest.mark.parametrize("reported", ["relative/venvs", "venvs", "./venvs", "", "/a/venvs\n/b/venvs"])
def test_a_relative_or_malformed_pipx_report_is_rejected(tmp_path, monkeypatch, reported):
    home = tmp_path / "home"
    monkeypatch.setenv("PIPX_HOME", str(home))
    commands = []
    result, _ = _migrate(
        tmp_path, monkeypatch, "stable", _ok(commands), capture=lambda argv: reported, venvs=home / "venvs"
    )
    assert result == 0
    # The report is ignored, so the fallback directory is the one setup runs from.
    assert commands[3][0] == str(_setup_executable(home / "venvs"))


def test_the_default_capture_returns_none_when_the_command_cannot_run(tmp_path):
    assert update_cmd._default_capture([str(tmp_path / "no-such-pipx"), "environment"]) is None


@pytest.mark.parametrize("metadata", [None, "garbage", "something-else", "brigade-cli"])
@pytest.mark.parametrize("channel", ["stable", "beta"])
def test_an_unverified_environment_is_never_executed_and_nothing_is_recorded(
    tmp_path, monkeypatch, capsys, channel, metadata
):
    commands = []
    result, paths = _migrate(
        tmp_path, monkeypatch, channel, _ok(commands), metadata=metadata, venv_entries=("brigade", "brigadestack")
    )
    assert result == 1
    assert all(command[1] != "setup" for command in commands)  # setup was not run from the venv
    assert str(_setup_executable(tmp_path / "venvs")) not in [command[0] for command in commands]
    assert _no_state(tmp_path)
    err = capsys.readouterr().err
    assert "not running setup from" in err and "pipx_metadata.json" in err
    assert "no update was recorded" in err
    assert not (paths.brigade_executable.parent / "brigade").exists()


@pytest.mark.parametrize("metadata", [None, "garbage", "something-else"])
def test_an_unverified_environment_is_not_relinked_after_a_failed_second_install(
    tmp_path, monkeypatch, capsys, metadata
):
    commands = []

    def runner(argv):
        commands.append(argv)
        return 1 if len(commands) == 3 else 0

    entries = ("brigade", "brigadestack", "brigade-hook")
    result, paths = _migrate(tmp_path, monkeypatch, "stable", runner, venv_entries=entries, metadata=metadata)
    assert result == 1
    assert len(commands) == 3 and _no_state(tmp_path)
    for name in entries:
        assert not (paths.brigade_executable.parent / name).exists()
        assert not (paths.brigade_executable.parent / name).is_symlink()
    assert "no brigadestack environment could be relinked" in capsys.readouterr().err


@pytest.mark.skipif(os.name == "nt", reason="symbolic links need privileges on Windows")
def test_a_symlinked_environment_directory_is_not_trusted(tmp_path, monkeypatch, capsys):
    (tmp_path / "venvs").mkdir()
    # A complete, valid-looking environment that is reached through a link.
    real = _make_env(tmp_path / "real")
    (tmp_path / "venvs" / "brigadestack").symlink_to(real, target_is_directory=True)
    commands = []
    result, _ = _migrate(tmp_path, monkeypatch, "stable", _ok(commands), make_env=False)
    assert result == 1
    assert all(command[1] != "setup" for command in commands) and _no_state(tmp_path)
    assert "is a symbolic link" in capsys.readouterr().err


def test_env_verification_accepts_only_a_brigadestack_environment(tmp_path):
    assert update_cmd._brigadestack_env_problem(tmp_path / "venvs") is not None  # nothing there
    _make_env(tmp_path / "venvs")
    assert update_cmd._brigadestack_env_problem(tmp_path / "venvs") is None


@pytest.mark.parametrize("package", ["brigade-cli", "brigadestack"])
def test_installed_pypi_project_reads_the_pipx_record(tmp_path, monkeypatch, package):
    monkeypatch.undo()  # drop the autouse stub so the real function runs
    (tmp_path / "pipx_metadata.json").write_text(json.dumps({"main_package": {"package": package}}))
    monkeypatch.setattr(update_cmd.sys, "prefix", str(tmp_path))
    assert update_cmd.installed_pypi_project() == package


def test_installed_pypi_project_falls_back_to_distribution_metadata(tmp_path, monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(update_cmd.sys, "prefix", str(tmp_path))  # no pipx record
    seen = []

    def fake_distribution(name):
        seen.append(name)
        if name == "brigade-cli":
            return object()
        raise update_cmd.importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(update_cmd.importlib.metadata, "distribution", fake_distribution)
    assert update_cmd.installed_pypi_project() == "brigade-cli"

    def only_real_package(name):
        raise update_cmd.importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(update_cmd.importlib.metadata, "distribution", only_real_package)
    assert update_cmd.installed_pypi_project() == "brigadestack"


def test_installed_pypi_project_prefers_brigadestack_when_both_dist_infos_exist(tmp_path, monkeypatch):
    # A venv upgraded from the 0.31.0 brigadestack bridge keeps an orphaned brigade-cli dist-info.
    monkeypatch.undo()
    monkeypatch.setattr(update_cmd.sys, "prefix", str(tmp_path))  # no pipx record
    present = {"brigadestack", "brigade-cli"}

    def both(name):
        if name in present:
            return object()
        raise update_cmd.importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(update_cmd.importlib.metadata, "distribution", both)
    assert update_cmd.installed_pypi_project() == "brigadestack"
    present.discard("brigadestack")
    assert update_cmd.installed_pypi_project() == "brigade-cli"


def test_installed_pypi_project_ignores_an_unrelated_pipx_record(tmp_path, monkeypatch):
    monkeypatch.undo()
    (tmp_path / "pipx_metadata.json").write_text(json.dumps({"main_package": {"package": "something-else"}}))
    monkeypatch.setattr(update_cmd.sys, "prefix", str(tmp_path))

    def missing(name):
        raise update_cmd.importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(update_cmd.importlib.metadata, "distribution", missing)
    assert update_cmd.installed_pypi_project() == "brigadestack"


# The names are labels; the immutable repository ID is the identity (#2816). After the rename an
# organization member can create a new escoffier-labs/brigade, which then answers directly instead of
# redirecting, and its self-consistent release must never be installed.


def test_update_pins_the_repository_id():
    assert update_cmd.REPOSITORY_ID == 1238165856


@pytest.mark.parametrize("impostor_id", [1, update_cmd.REPOSITORY_ID + 1, "1238165856"])
def test_a_reused_current_name_answering_directly_is_refused_before_anything_is_installed(
    tmp_path, impostor_id, capsys
):
    class _Impostor(_GithubServing):
        """The old name now belongs to another repository, which serves a complete release itself."""

    paths = update_cmd.UpdatePaths(tmp_path / "data", tmp_path / "cache", tmp_path / "bin" / "brigade")
    http = _Impostor(CURRENT_SLUG, CURRENT_SLUG, repository_id=impostor_id)
    commands = []

    result = update_cmd.run_update(
        channel="stable",
        paths=paths,
        http=http,
        runner=lambda argv: commands.append(argv) or 0,
        now=lambda: "2026-07-20T12:00:00+00:00",
    )

    assert result == 1
    assert "is not repository ID" in capsys.readouterr().err
    assert commands == []
    assert not any(url.endswith("component-manifest-v1.json") for url in http.urls)


def test_the_real_repository_id_under_the_current_name_still_updates(tmp_path):
    paths = update_cmd.UpdatePaths(tmp_path / "data", tmp_path / "cache", tmp_path / "bin" / "brigade")
    http = _GithubServing(CURRENT_SLUG, CURRENT_SLUG)

    result = update_cmd.run_update(
        channel="stable",
        paths=paths,
        http=http,
        runner=lambda argv: 0,
        now=lambda: "2026-07-20T12:00:00+00:00",
    )

    assert result == 0
    assert f"https://api.github.com/repos/{CURRENT_SLUG}" in http.urls
