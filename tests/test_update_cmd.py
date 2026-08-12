"""Tests for immutable Brigade update resolution and ownership."""

from __future__ import annotations

import hashlib
import json
import urllib.error
from pathlib import Path
from unittest.mock import patch

import pytest

from brigade import component_manifest, update_cmd


TAG = "v1.2.3"
VERSION = "1.2.3"
BASE = f"https://github.com/escoffier-labs/brigade/releases/download/{TAG}/"
REF_URL = f"https://api.github.com/repos/escoffier-labs/brigade/git/ref/tags/{TAG}"
TAG_OBJECT_URL = "https://api.github.com/repos/escoffier-labs/brigade/git/tags/"


def _manifest() -> bytes:
    components = {}
    for component in component_manifest.KNOWN_COMPONENT_IDS:
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
        if component == "agent-notify":
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
        "filename": f"brigade_cli-{version}-py3-none-any.whl",
        "packagetype": packagetype,
        "yanked": yanked,
    }


def _sdist_file(version: str, *, yanked: bool = False) -> dict:
    return {
        "filename": f"brigade_cli-{version}.tar.gz",
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
        info_version: str = "0.26.1",
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
            # even when releases contains 0.27.0.devYYYYMMDD wheels.
            return {"info": {"version": self.info_version}, "releases": self.pypi_releases}
        if url.endswith("/releases/latest"):
            return self.release
        if url == REF_URL:
            return self.tag_ref
        if url.startswith(TAG_OBJECT_URL):
            return self.tag_objects[url.removeprefix(TAG_OBJECT_URL)]
        raise AssertionError(url)


def _default_beta_releases() -> dict:
    version = "0.27.0.dev20260808"
    return {version: [_wheel_file(version)]}


BETA_VERSION = "0.27.0.dev20260808"


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
    with patch("urllib.request.urlopen", return_value=_Response(b"manifest", final_url)):
        assert update_cmd._DefaultHttp().bytes(url) == b"manifest"


@pytest.mark.parametrize(
    "final_url",
    (
        "https://example.test/manifest",
        "http://release-assets.githubusercontent.com/manifest",
    ),
)
def test_default_http_rejects_untrusted_or_non_https_asset_redirect(final_url):
    url = BASE + "component-manifest-v1.json"
    with patch("urllib.request.urlopen", return_value=_Response(b"manifest", final_url)):
        with pytest.raises(update_cmd.UpdateError, match="release asset redirected"):
            update_cmd._DefaultHttp().bytes(url)


def test_default_http_rejects_api_redirect_outside_exact_api_path():
    url = "https://api.github.com/repos/escoffier-labs/brigade/releases/latest"
    with patch(
        "urllib.request.urlopen",
        return_value=_Response(b"{}", "https://api.github.com/repos/escoffier-labs/brigade/releases/tags/v1.2.3"),
    ):
        with pytest.raises(update_cmd.UpdateError, match="GitHub API request redirected"):
            update_cmd._DefaultHttp().json(url)


def test_default_http_uses_github_token_for_github_api_requests(monkeypatch):
    url = "https://api.github.com/repos/escoffier-labs/brigade/releases/latest"
    monkeypatch.setenv("GITHUB_TOKEN", "github-token-example")
    monkeypatch.setenv("GH_TOKEN", "gh-token-example")
    requests = []

    def urlopen(request, *, timeout):
        requests.append(request)
        assert timeout == 30
        return _Response(b"{}", url)

    with patch("urllib.request.urlopen", side_effect=urlopen):
        assert update_cmd._DefaultHttp().json(url) == {}

    assert requests[0].get_header("Authorization") == "Bearer github-token-example"


def test_default_http_uses_gh_token_when_github_token_is_absent(monkeypatch):
    url = "https://api.github.com/repos/escoffier-labs/brigade/releases/latest"
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GH_TOKEN", "gh-token-example")

    with patch("urllib.request.urlopen", return_value=_Response(b"{}", url)) as urlopen:
        assert update_cmd._DefaultHttp().json(url) == {}

    request = urlopen.call_args.args[0]
    assert request.get_header("Authorization") == "Bearer gh-token-example"


@pytest.mark.parametrize("status", (401, 403))
def test_default_http_retries_github_api_without_rejected_token(monkeypatch, status):
    url = "https://api.github.com/repos/escoffier-labs/brigade/releases/latest"
    secret = "rejected-token-example"
    monkeypatch.setenv("GITHUB_TOKEN", secret)
    requests = []

    def urlopen(request, *, timeout):
        requests.append(request)
        assert timeout == 30
        if len(requests) == 1:
            raise urllib.error.HTTPError(url, status, "Bad credentials", {}, None)
        return _Response(b"{}", url)

    with patch("urllib.request.urlopen", side_effect=urlopen):
        assert update_cmd._DefaultHttp().json(url) == {}

    assert requests[0].get_header("Authorization") == f"Bearer {secret}"
    assert requests[1].get_header("Authorization") is None


def test_default_http_rejected_token_is_absent_from_final_error(monkeypatch):
    url = "https://api.github.com/repos/escoffier-labs/brigade/releases/latest"
    secret = "rejected-token-example"
    monkeypatch.setenv("GITHUB_TOKEN", secret)

    with patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.HTTPError(url, 401, "Bad credentials", {}, None),
    ):
        with pytest.raises(update_cmd.UpdateError) as caught:
            update_cmd._DefaultHttp().json(url)

    assert secret not in str(caught.value)


def test_default_http_never_sends_github_token_to_non_api_requests(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "github-token-example")
    url = BASE + "component-manifest-v1.json"

    with patch("urllib.request.urlopen", return_value=_Response(b"manifest", url)) as urlopen:
        assert update_cmd._DefaultHttp().bytes(url) == b"manifest"

    request = urlopen.call_args.args[0]
    assert request.get_header("Authorization") is None


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


def test_resolve_beta_cli_version_selects_newest_non_yanked_0_27_dev_wheel():
    releases = {
        "0.27.0.dev20260801": [_wheel_file("0.27.0.dev20260801")],
        "0.27.0.dev20260810": [_wheel_file("0.27.0.dev20260810", yanked=True)],
        "0.27.0.dev20260809": [_wheel_file("0.27.0.dev20260809")],
        "0.27.0.dev20260808": [_sdist_file("0.27.0.dev20260808")],
        "0.26.0.dev20260811": [_wheel_file("0.26.0.dev20260811")],
        "0.28.0.dev20260811": [_wheel_file("0.28.0.dev20260811")],
        "0.27.0.dev2026080": [_wheel_file("0.27.0.dev2026080")],
        "0.27.0.dev202608091": [_wheel_file("0.27.0.dev202608091")],
        "0.27.0a1": [_wheel_file("0.27.0a1")],
        "0.27.0rc1": [_wheel_file("0.27.0rc1")],
        "0.27.0.dev20260809.post1": [_wheel_file("0.27.0.dev20260809.post1")],
        "0.27.1.dev20260811": [_wheel_file("0.27.1.dev20260811")],
        "1.2.3": [_wheel_file("1.2.3")],
        "main": [_wheel_file("main")],
        "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb": [_wheel_file("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")],
    }
    http = _PypiHttp(_release(_manifest()), _manifest(), pypi_releases=releases)

    assert update_cmd.resolve_beta_cli_version(http) == "0.27.0.dev20260809"
    assert update_cmd.PYPI_PROJECT_JSON_URL in http.urls


@pytest.mark.parametrize(
    "releases",
    (
        {},
        {"0.27.0.dev20260808": []},
        {"0.27.0.dev20260808": [_wheel_file("0.27.0.dev20260808", yanked=True)]},
        {"0.27.0.dev20260808": [_sdist_file("0.27.0.dev20260808")]},
        {"0.27.0.dev20260808": "not-a-list"},
        {"0.27.0.dev2026080": [_wheel_file("0.27.0.dev2026080")]},
        {"0.28.0.dev20260808": [_wheel_file("0.28.0.dev20260808")]},
        {"0.27.0a1": [_wheel_file("0.27.0a1")]},
    ),
)
def test_resolve_beta_cli_version_fails_closed_without_installable_preview_wheel(releases):
    http = _PypiHttp(_release(_manifest()), _manifest(), pypi_releases=releases)
    with pytest.raises(update_cmd.UpdateError, match="beta fails closed"):
        update_cmd.resolve_beta_cli_version(http)


def test_resolve_beta_cli_version_scans_releases_not_info_version():
    """Live PyPI shape after workflow 31422458576: info.version stays stable."""
    releases = {
        "0.26.1": [_wheel_file("0.26.1")],
        "0.27.0.dev20260810": [_wheel_file("0.27.0.dev20260810")],
    }
    http = _PypiHttp(
        _release(_manifest()),
        _manifest(),
        pypi_releases=releases,
        info_version="0.26.1",
    )

    assert update_cmd.resolve_beta_cli_version(http) == "0.27.0.dev20260810"
    assert http.info_version == "0.26.1"


def test_resolve_beta_cli_version_ignores_info_version_even_when_it_looks_like_a_dev_wheel():
    releases = {
        "0.26.1": [_wheel_file("0.26.1")],
        "0.27.0.dev20260810": [_wheel_file("0.27.0.dev20260810")],
    }
    http = _PypiHttp(
        _release(_manifest()),
        _manifest(),
        pypi_releases=releases,
        info_version="0.27.0.dev20991231",
    )

    assert update_cmd.resolve_beta_cli_version(http) == "0.27.0.dev20260810"


def test_resolve_beta_cli_version_fails_closed_when_releases_map_missing():
    class _MissingReleases:
        urls: list[str]

        def __init__(self) -> None:
            self.urls = []

        def json(self, url: str):
            self.urls.append(url)
            assert url == update_cmd.PYPI_PROJECT_JSON_URL
            return {"info": {"version": "0.26.1"}}

        def bytes(self, url: str) -> bytes:
            raise AssertionError(url)

    with pytest.raises(update_cmd.UpdateError, match="releases map"):
        update_cmd.resolve_beta_cli_version(_MissingReleases())


def test_resolve_beta_cli_version_ignores_yanked_sibling_and_keeps_older_wheel():
    releases = {
        "0.27.0.dev20260810": [_wheel_file("0.27.0.dev20260810", yanked=True)],
        "0.27.0.dev20260809": [
            _wheel_file("0.27.0.dev20260809", yanked=True),
            _wheel_file("0.27.0.dev20260809"),
        ],
        "0.27.0.dev20260801": [_wheel_file("0.27.0.dev20260801")],
    }
    http = _PypiHttp(_release(_manifest()), _manifest(), pypi_releases=releases)
    assert update_cmd.resolve_beta_cli_version(http) == "0.27.0.dev20260809"


def test_parse_beta_dev_version_accepts_only_exact_0_27_daily_form():
    assert update_cmd._parse_beta_dev_version("0.27.0.dev20260808") == "20260808"
    assert update_cmd._parse_beta_dev_version("0.27.0.dev2026080") is None
    assert update_cmd._parse_beta_dev_version("0.28.0.dev20260808") is None
    assert update_cmd._parse_beta_dev_version("0.27.0a1") is None
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
    assert commands[0] == ["pipx", "install", "--force", "brigade-cli==1.2.3"]
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
    assert http.urls[1:4] == [REF_URL, TAG_OBJECT_URL + first_tag, TAG_OBJECT_URL + second_tag]


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
        ["pipx", "install", "--force", f"brigade-cli=={BETA_VERSION}"],
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
        ["pipx", "install", "--force", f"brigade-cli=={BETA_VERSION}"],
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
    with pytest.raises(ValueError, match="missing required components: agent-notify"):
        update_cmd.validate_release_manifest_bytes(release)

    # The narrowly named compatibility mode is the only path that accepts it,
    # and only because agent-notify is currently unpublished.
    accepted = update_cmd.validate_release_manifest_bytes(release, allow_compatible_stable_manifest=True)
    assert "agent-notify" not in accepted.components
    assert set(accepted.components) == set(component_manifest.KNOWN_COMPONENT_IDS) - {"agent-notify"}


def test_release_manifest_compatibility_mode_rejects_missing_published_component():
    manifest = json.loads(_pre_agent_notify_manifest())
    del manifest["components"]["miseledger"]
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

    with pytest.raises(ValueError, match="missing required components: miseledger"):
        update_cmd.validate_release_manifest_bytes(release, allow_compatible_stable_manifest=True)


def test_beta_same_coordinates_are_a_noop(tmp_path):
    manifest = _manifest()
    paths = update_cmd.UpdatePaths(tmp_path / "data", tmp_path / "cache", tmp_path / "bin" / "brigade")
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
    assert commands[0] == ["pipx", "install", "--force", f"brigade-cli=={BETA_VERSION}"]
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
            "0.27.0.dev20260701",
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
    assert f"pipx install --force brigade-cli=={BETA_VERSION}" in captured
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
    assert commands[0] == ["pipx", "install", "--force", "brigade-cli==1.2.3"]
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
    assert commands[0] == ["pipx", "install", "--force", "brigade-cli==1.2.3"]
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


def test_same_immutable_coordinates_are_a_noop(tmp_path):
    manifest = _manifest()
    calls = []
    paths = update_cmd.UpdatePaths(tmp_path / "data", tmp_path / "cache", tmp_path / "bin" / "brigade")
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

    assert (
        update_cmd.run_update(
            channel="stable",
            paths=paths,
            http=_Http(_release(manifest), manifest),
            runner=calls.append,
            now=lambda: "2026-07-20T12:01:00+00:00",
        )
        == 0
    )
    assert calls == []


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
