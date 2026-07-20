"""Tests for immutable Brigade update resolution and ownership."""

from __future__ import annotations

import hashlib
import json
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


class _BetaHttp(_Http):
    def __init__(self, release, manifest, *, sha="b" * 40, pages=None):
        super().__init__(release, manifest)
        self.sha = sha
        self.pages = pages or {
            1: {"total_count": 1, "check_runs": [{"id": 1, "status": "completed", "conclusion": "success"}]}
        }

    def json(self, url):
        self.urls.append(url)
        if url.endswith("/releases/latest"):
            return self.release
        if url == REF_URL:
            return self.tag_ref
        if url.startswith(TAG_OBJECT_URL):
            return self.tag_objects[url.removeprefix(TAG_OBJECT_URL)]
        if url.endswith("/commits/main"):
            return {"sha": self.sha}
        prefix = f"https://api.github.com/repos/escoffier-labs/brigade/commits/{self.sha}/check-runs?per_page=100&page="
        if url.startswith(prefix):
            return self.pages[int(url.removeprefix(prefix))]
        raise AssertionError(url)


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


def test_beta_check_runs_paginate_and_require_every_run_to_pass():
    sha = "b" * 40
    pages = {
        1: {
            "total_count": 101,
            "check_runs": [{"id": index, "status": "completed", "conclusion": "success"} for index in range(1, 101)],
        },
        2: {"total_count": 101, "check_runs": [{"id": 101, "status": "completed", "conclusion": "neutral"}]},
    }
    http = _BetaHttp(_release(_manifest()), _manifest(), sha=sha, pages=pages)
    assert update_cmd._check_beta(http) == sha
    assert http.urls[-2:] == [
        f"https://api.github.com/repos/escoffier-labs/brigade/commits/{sha}/check-runs?per_page=100&page=1",
        f"https://api.github.com/repos/escoffier-labs/brigade/commits/{sha}/check-runs?per_page=100&page=2",
    ]


@pytest.mark.parametrize(
    "payload",
    (
        {"total_count": 0, "check_runs": []},
        {"total_count": 1, "check_runs": [{"id": 1, "status": "in_progress", "conclusion": None}]},
        {"total_count": 1, "check_runs": [{"id": 1, "status": "completed", "conclusion": "failure"}]},
        {"total_count": 1, "check_runs": [{"id": 1, "status": "completed", "conclusion": "cancelled"}]},
        {"total_count": 1, "check_runs": [{"id": 1, "status": "completed", "conclusion": "timed_out"}]},
        {"total_count": "1", "check_runs": []},
        {"total_count": 1, "check_runs": [{"status": "completed", "conclusion": "success"}]},
    ),
)
def test_beta_check_runs_fail_closed_for_missing_pending_failed_or_malformed_runs(payload):
    http = _BetaHttp(_release(_manifest()), _manifest(), pages={1: payload})
    with pytest.raises(update_cmd.UpdateError, match="beta fails closed"):
        update_cmd._check_beta(http)


def test_beta_check_runs_reject_truncated_or_inconsistent_pages():
    http = _BetaHttp(
        _release(_manifest()),
        _manifest(),
        pages={
            1: {"total_count": 101, "check_runs": [{"id": 1, "status": "completed", "conclusion": "success"}]},
        },
    )
    with pytest.raises(update_cmd.UpdateError, match="beta fails closed"):
        update_cmd._check_beta(http)


def test_beta_check_runs_reject_inconsistent_or_duplicate_later_pages():
    successful_runs = [{"id": index, "status": "completed", "conclusion": "success"} for index in range(1, 101)]
    for second_page in (
        {"total_count": 100, "check_runs": []},
        {"total_count": 101, "check_runs": [{"id": 1, "status": "completed", "conclusion": "success"}]},
    ):
        http = _BetaHttp(
            _release(_manifest()),
            _manifest(),
            pages={1: {"total_count": 101, "check_runs": successful_runs}, 2: second_page},
        )
        with pytest.raises(update_cmd.UpdateError, match="beta fails closed"):
            update_cmd._check_beta(http)


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
    assert not update_cmd._is_github_api_url(f"https://api.github.com/repos/other/brigade/git/ref/tags/{TAG}")
    assert not update_cmd._is_github_api_url(
        f"https://api.github.com/repos/escoffier-labs/brigade/git/ref/tags/{TAG}?x=1"
    )
    assert not update_cmd._is_github_api_url(f"https://api.github.com/repos/escoffier-labs/brigade/git/tags/{sha}?x=1")


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


def test_beta_update_installs_full_main_sha_and_reuses_verified_stable_manifest(tmp_path):
    manifest = _manifest()
    sha = "b" * 40
    commands = []
    installed_binary = tmp_path / "bin" / "brigade"
    paths = update_cmd.UpdatePaths(tmp_path / "data", tmp_path / "cache", installed_binary)

    assert (
        update_cmd.run_update(
            channel="beta",
            paths=paths,
            http=_BetaHttp(_release(manifest), manifest, sha=sha),
            runner=lambda argv: commands.append(argv) or 0,
            now=lambda: "2026-07-20T12:00:00+00:00",
        )
        == 0
    )
    cache_path = paths.cache_root / "brigade" / "release-manifests" / f"{hashlib.sha256(manifest).hexdigest()}.json"
    assert cache_path.read_bytes() == manifest
    assert commands == [
        ["pipx", "install", "--force", f"git+https://github.com/escoffier-labs/brigade@{sha}"],
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
    assert state.cli_coordinate == sha
    assert (state.component_release_id, state.component_tag) == (42, TAG)


def test_beta_same_coordinates_are_a_noop(tmp_path):
    manifest = _manifest()
    sha = "b" * 40
    paths = update_cmd.UpdatePaths(tmp_path / "data", tmp_path / "cache", tmp_path / "bin" / "brigade")
    update_cmd.write_update_state(
        paths.data_root / "brigade" / "update-state.json",
        update_cmd.UpdateState(
            1,
            "beta",
            "brigade update",
            sha,
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
            http=_BetaHttp(_release(manifest), manifest, sha=sha),
            runner=calls.append,
        )
        == 0
    )
    assert calls == []


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
            "a" * 40,
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
            http=_BetaHttp(_release(manifest), manifest),
            runner=lambda _argv: next(returns),
        )
        == 1
    )
    assert state_path.read_bytes() == before


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
