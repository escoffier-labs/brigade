"""Tests for unified Brigade release manifest provenance."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "escoffier-labs/brigade"
RENAMED_REPOSITORY = "escoffier-labs/brigadestack"
TAG = "v0.32.3"
BASE = f"https://github.com/{REPOSITORY}/releases/download/{TAG}/"
API = f"https://api.github.com/repos/{REPOSITORY}/releases/tags/{TAG}"
REF_API = f"https://api.github.com/repos/{REPOSITORY}/git/ref/tags/{TAG}"
TAG_API = f"https://api.github.com/repos/{REPOSITORY}/git/tags/"


def _urls(repository: str) -> tuple[str, str, str, str]:
    return (
        f"https://github.com/{repository}/releases/download/{TAG}/",
        f"https://api.github.com/repos/{repository}/releases/tags/{TAG}",
        f"https://api.github.com/repos/{repository}/git/ref/tags/{TAG}",
        f"https://api.github.com/repos/{repository}/git/tags/",
    )


COMPONENTS = (
    "agent-notify",
    "brigade-code-graph",
    "brigade-code-graph-mcp",
    "brigade-evidence",
    "brigade-hub",
    "sessionfind",
)
PLATFORMS = ("linux-amd64", "linux-arm64", "darwin-amd64", "darwin-arm64", "windows-amd64")


def _platforms(component: str) -> tuple[str, ...]:
    # brigade-hub ships for Linux only.
    return ("linux-amd64", "linux-arm64") if component == "brigade-hub" else PLATFORMS


def _module():
    spec = importlib.util.spec_from_file_location(
        "verify_component_manifest_provenance_test", ROOT / "scripts/verify_component_manifest_provenance.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _asset_name(component: str, platform: str) -> str:
    return f"{component}-{platform}" + (".exe" if platform == "windows-amd64" else "")


def _manifest(repository: str = REPOSITORY) -> dict:
    base = _urls(repository)[0]
    components = {}
    for component in COMPONENTS:
        assets = {}
        for platform in _platforms(component):
            name = _asset_name(component, platform)
            body = name.encode()
            assets[platform] = {
                "asset_name": name,
                "byte_size": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
                "download_url": base + name,
            }
        components[component] = {
            "component_revision": "a" * 40,
            "source": {"repository": repository, "release_tag": TAG},
            "executable": component,
            "assets": assets,
        }
    return {
        "schema_version": 1,
        "brigade_version": "0.32.3",
        "manifest_revision": "v0.32.3+" + "a" * 40,
        "supported_platforms": list(PLATFORMS),
        "components": components,
    }


def _release(manifest: dict, repository: str = REPOSITORY, *, aliases: bool = True) -> tuple[dict, str, str]:
    tag = next(iter(manifest["components"].values()))["source"]["release_tag"]
    base = _urls(repository)[0].replace(TAG, tag)
    native = {
        asset["asset_name"]: asset
        for component in manifest["components"].values()
        for asset in component["assets"].values()
    }
    for component in ("graphtrail", "graphtrail-mcp", "miseledger") if aliases else ():
        for platform in PLATFORMS:
            name = _asset_name(component, platform)
            native[name] = {"byte_size": len(name.encode()), "sha256": hashlib.sha256(name.encode()).hexdigest()}
    manifest_body = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    manifest_digest = hashlib.sha256(manifest_body.encode()).hexdigest()
    checksum_map = {name: asset["sha256"] for name, asset in native.items()}
    checksum_map["component-manifest-v1.json"] = manifest_digest
    checksums = "".join(f"{digest}  {name}\n" for name, digest in sorted(checksum_map.items()))
    assets = [
        {
            "name": name,
            "size": asset["byte_size"],
            "digest": f"sha256:{asset['sha256']}",
            "browser_download_url": base + name,
        }
        for name, asset in sorted(native.items())
    ]
    assets.extend(
        [
            {
                "name": "component-manifest-v1.json",
                "size": len(manifest_body.encode()),
                "digest": f"sha256:{manifest_digest}",
                "browser_download_url": base + "component-manifest-v1.json",
            },
            {
                "name": "checksums.txt",
                "size": len(checksums.encode()),
                "digest": f"sha256:{hashlib.sha256(checksums.encode()).hexdigest()}",
                "browser_download_url": base + "checksums.txt",
            },
        ]
    )
    return {"tag_name": tag, "target_commitish": "main", "assets": assets}, checksums, manifest_body


REPOSITORY_ID = 1238165856


def _fetcher(
    release: dict,
    checksums: str,
    manifest_body: str,
    *,
    tag_ref=None,
    tag_objects=None,
    repository: str = REPOSITORY,
    repository_id: int = REPOSITORY_ID,
):
    tag = release["tag_name"]
    base, api, ref_api, tag_api = (url.replace(TAG, tag) for url in _urls(repository))
    tag_ref = tag_ref or {"ref": f"refs/tags/{tag}", "object": {"type": "commit", "sha": "a" * 40}}
    tag_objects = tag_objects or {}

    def fetch(url: str) -> tuple[int, str]:
        if url == f"https://api.github.com/repos/{repository}":
            return 200, json.dumps({"id": repository_id, "full_name": repository})
        if url == api:
            return 200, json.dumps(release)
        if url == ref_api:
            return 200, json.dumps(tag_ref)
        if url.startswith(tag_api):
            return 200, json.dumps(tag_objects[url.removeprefix(tag_api)])
        if url == base + "checksums.txt":
            return 200, checksums
        if url == base + "component-manifest-v1.json":
            return 200, manifest_body
        raise AssertionError(f"unexpected fetch URL: {url}")

    return fetch


def test_verifier_accepts_exact_one_release_inventory_and_release_page_manifest(tmp_path):
    module = _module()
    manifest = _manifest()
    release, checksums, manifest_body = _release(manifest)
    path = tmp_path / "manifest.json"
    path.write_text(manifest_body)

    assert module.verify_manifest(path, fetch=_fetcher(release, checksums, manifest_body)) == []


def test_verifier_rejects_component_release_coordinate_drift(tmp_path):
    module = _module()
    manifest = _manifest()
    manifest["components"]["brigade-evidence"]["source"]["repository"] = "escoffier-labs/miseledger"
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    release, checksums, manifest_body = _release(_manifest())

    errors = module.verify_manifest(path, fetch=_fetcher(release, checksums, manifest_body))

    assert any("escoffier-labs/brigade" in error for error in errors)


def test_verifier_rejects_incomplete_matrix_extra_release_asset_and_checksum_gap(tmp_path):
    module = _module()
    manifest = _manifest()
    del manifest["components"]["sessionfind"]["assets"]["darwin-arm64"]
    release, checksums, manifest_body = _release(_manifest())
    release["assets"].append(
        {"name": "unexpected", "size": 1, "digest": "sha256:" + "a" * 64, "browser_download_url": BASE + "unexpected"}
    )
    checksums = checksums.replace("component-manifest-v1.json\n", "missing-manifest\n")
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))

    errors = module.verify_manifest(path, fetch=_fetcher(release, checksums, manifest_body))

    assert any("full platform matrix" in error for error in errors)
    assert any("unexpected assets" in error for error in errors)
    assert any("checksums.txt" in error for error in errors)


def test_verifier_requires_release_page_manifest_matching_the_local_manifest(tmp_path):
    module = _module()
    manifest = _manifest()
    release, checksums, _ = _release(manifest)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    errors = module.verify_manifest(path, fetch=_fetcher(release, checksums, "{}\n"))

    assert any("release-page component-manifest-v1.json" in error for error in errors)


def test_verifier_rejects_fetched_checksums_bytes_that_do_not_match_release_asset_digest(tmp_path):
    module = _module()
    manifest = _manifest()
    release, checksums, manifest_body = _release(manifest)
    checksum_asset = next(asset for asset in release["assets"] if asset["name"] == "checksums.txt")
    checksum_asset["digest"] = "sha256:" + "b" * 64
    path = tmp_path / "manifest.json"
    path.write_text(manifest_body)

    errors = module.verify_manifest(path, fetch=_fetcher(release, checksums, manifest_body))

    assert any("checksums.txt" in error and "digest" in error for error in errors)


def test_verifier_rejects_component_revision_mismatched_to_resolved_tag_commit(tmp_path):
    module = _module()
    manifest = _manifest()
    manifest["components"]["brigade-evidence"]["component_revision"] = "b" * 40
    release, checksums, manifest_body = _release(manifest)
    path = tmp_path / "manifest.json"
    path.write_text(manifest_body)

    errors = module.verify_manifest(path, fetch=_fetcher(release, checksums, manifest_body))

    assert any("component_revision" in error and "tag commit" in error for error in errors)


def test_verifier_dereferences_annotated_tag_and_ignores_release_target_commitish(tmp_path):
    module = _module()
    manifest = _manifest()
    release, checksums, manifest_body = _release(manifest)
    path = tmp_path / "manifest.json"
    path.write_text(manifest_body)
    first_tag = "b" * 40
    second_tag = "c" * 40

    assert (
        module.verify_manifest(
            path,
            fetch=_fetcher(
                release,
                checksums,
                manifest_body,
                tag_ref={"ref": f"refs/tags/{TAG}", "object": {"type": "tag", "sha": first_tag}},
                tag_objects={
                    first_tag: {"tag": TAG, "object": {"type": "tag", "sha": second_tag}},
                    second_tag: {"tag": TAG, "object": {"type": "commit", "sha": "a" * 40}},
                },
            ),
        )
        == []
    )


@pytest.mark.parametrize(
    ("tag_ref", "tag_objects"),
    (
        ({"ref": "refs/tags/v9.9.9", "object": {"type": "commit", "sha": "a" * 40}}, {}),
        ({"ref": f"refs/tags/{TAG}", "object": {"type": "blob", "sha": "a" * 40}}, {}),
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
def test_verifier_fails_closed_for_malformed_or_cyclic_tag_objects(tmp_path, tag_ref, tag_objects):
    module = _module()
    manifest = _manifest()
    release, checksums, manifest_body = _release(manifest)
    path = tmp_path / "manifest.json"
    path.write_text(manifest_body)

    errors = module.verify_manifest(
        path, fetch=_fetcher(release, checksums, manifest_body, tag_ref=tag_ref, tag_objects=tag_objects)
    )

    assert any("tag" in error for error in errors)


def test_verifier_fails_closed_for_excessively_nested_annotated_tags(tmp_path):
    module = _module()
    manifest = _manifest()
    release, checksums, manifest_body = _release(manifest)
    path = tmp_path / "manifest.json"
    path.write_text(manifest_body)
    shas = [f"{index:040x}" for index in range(1, module.MAX_TAG_DEREFERENCE_DEPTH + 2)]
    tag_objects = {
        sha: {"tag": TAG, "object": {"type": "tag", "sha": next_sha}}
        for sha, next_sha in zip(shas[:-1], shas[1:], strict=True)
    }

    errors = module.verify_manifest(
        path,
        fetch=_fetcher(
            release,
            checksums,
            manifest_body,
            tag_ref={"ref": f"refs/tags/{TAG}", "object": {"type": "tag", "sha": shas[0]}},
            tag_objects=tag_objects,
        ),
    )

    assert any("depth" in error for error in errors)


@pytest.mark.parametrize(
    "asset_name, digest",
    (
        ("component-manifest-v1.json", None),
        ("checksums.txt", None),
        ("graphtrail-linux-amd64", None),
        ("component-manifest-v1.json", {"sha256": "not-a-string"}),
    ),
)
def test_verifier_collects_errors_for_missing_or_malformed_github_release_digests(tmp_path, asset_name, digest):
    module = _module()
    manifest = _manifest()
    release, checksums, manifest_body = _release(manifest)
    asset = next(item for item in release["assets"] if item["name"] == asset_name)
    if digest is None:
        asset.pop("digest")
    else:
        asset["digest"] = digest
    path = tmp_path / "manifest.json"
    path.write_text(manifest_body)

    errors = module.verify_manifest(path, fetch=_fetcher(release, checksums, manifest_body))

    assert errors
    assert any(asset_name in error and "digest" in error for error in errors)


ALIASES = ("brigade-code-graph", "brigade-code-graph-mcp", "brigade-evidence")


def _additive_release():
    release, checksums, body = _release(_manifest())
    _authenticate_checksums(release, checksums)
    return release, checksums, body


def _authenticate_checksums(release, checksums):
    asset = next(item for item in release["assets"] if item["name"] == "checksums.txt")
    asset["digest"] = "sha256:" + hashlib.sha256(checksums.encode()).hexdigest()
    asset["size"] = len(checksums.encode())


def test_verifier_accepts_additive_release_with_distinct_alias_digests(tmp_path):
    release, checksums, body = _additive_release()
    path = tmp_path / "manifest.json"
    path.write_text(body)
    # 40 full-matrix assets, brigade-hub's 2 Linux assets, the manifest and checksums.txt.
    assert len(release["assets"]) == 44
    assert len(checksums.splitlines()) == 43
    assert _module().verify_manifest(path, fetch=_fetcher(release, checksums, body)) == []


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("size", 0, "size"),
        ("size", -1, "size"),
        ("size", True, "size"),
        ("size", "12", "size"),
        ("size", 1.5, "size"),
        ("size", None, "size"),
        ("digest", None, "digest"),
        ("digest", {"sha256": "a" * 64}, "digest"),
        ("digest", "sha256:bad", "digest"),
        ("digest", "sha256:" + "b" * 64, "digest"),
        ("browser_download_url", BASE.replace(TAG, "latest") + "brigade-evidence-linux-amd64", "URL"),
        ("browser_download_url", BASE + "brigade-evidence-linux-arm64", "URL"),
        ("browser_download_url", "https://example.com/brigade-evidence-linux-amd64", "URL"),
    ],
)
def test_verifier_rejects_malformed_alias_metadata(tmp_path, field, value, message):
    release, checksums, body = _additive_release()
    name = "brigade-evidence-linux-amd64"
    alias = next(asset for asset in release["assets"] if asset["name"] == name)
    alias[field] = value
    path = tmp_path / "manifest.json"
    path.write_text(body)
    errors = _module().verify_manifest(path, fetch=_fetcher(release, checksums, body))
    assert any(name in error and message in error for error in errors), errors


@pytest.mark.parametrize(
    "defect",
    [
        "missing",
        "partial",
        "foreign",
        "platform",
        "duplicate",
        "checksum-gap",
        "checksum-extra",
        "checksum-duplicate",
        "checksum-corrupt",
    ],
)
def test_verifier_rejects_invalid_additive_membership_and_checksums(tmp_path, defect):
    release, checksums, body = _additive_release()
    name = "brigade-evidence-windows-amd64.exe"
    alias = next(asset for asset in release["assets"] if asset["name"] == name)
    if defect == "missing":
        release["assets"].remove(alias)
    elif defect == "partial":
        release["assets"] = [
            asset for asset in release["assets"] if not asset["name"].startswith("brigade-") or asset == alias
        ]
    elif defect in {"foreign", "platform"}:
        alias["name"] = "foreign" if defect == "foreign" else "brigade-evidence-freebsd-amd64"
    elif defect == "duplicate":
        release["assets"].append(dict(alias))
    elif defect == "checksum-gap":
        checksums = "\n".join(line for line in checksums.splitlines() if not line.endswith(name)) + "\n"
    elif defect == "checksum-extra":
        checksums += "a" * 64 + "  unexpected\n"
    elif defect == "checksum-duplicate":
        checksums += next(line for line in checksums.splitlines() if line.endswith(name)) + "\n"
    else:
        checksums = checksums.replace(alias["digest"].removeprefix("sha256:"), "invalid")
    _authenticate_checksums(release, checksums)
    path = tmp_path / "manifest.json"
    path.write_text(body)
    errors = _module().verify_manifest(path, fetch=_fetcher(release, checksums, body))
    expected = (
        "duplicate"
        if defect in {"duplicate", "checksum-duplicate"}
        else "checksums.txt"
        if defect.startswith("checksum")
        else "assets"
    )
    assert any(expected in error for error in errors), errors


# The repository is renamed after 0.32.0: a release verifies under the name its manifest
# records, and only the two known names are ever accepted (#2816).


def _write(tmp_path, manifest: dict, body: str | None = None):
    path = tmp_path / "manifest.json"
    path.write_text(body if body is not None else json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return path


@pytest.mark.parametrize("repository", [REPOSITORY, RENAMED_REPOSITORY])
def test_verifier_accepts_a_release_recorded_under_either_accepted_repository(tmp_path, repository):
    manifest = _manifest(repository)
    release, checksums, body = _release(manifest, repository)
    path = _write(tmp_path, manifest, body)

    fetch = _fetcher(release, checksums, body, repository=repository)

    assert _module().verify_manifest(path, fetch=fetch) == []
    assert _module().verify_manifest(path, fetch=fetch, expected_repository=repository) == []


def test_verifier_rejects_a_third_repository_before_any_network_lookup(tmp_path):
    manifest = _manifest("attacker/brigade")
    path = _write(tmp_path, manifest)

    def no_network(url: str) -> tuple[int, str]:
        raise AssertionError(f"must not look up {url}")

    errors = _module().verify_manifest(path, fetch=no_network)

    assert errors
    assert all("attacker/brigade" not in error or "must be one of" in error for error in errors)
    assert any(f"{REPOSITORY}, {RENAMED_REPOSITORY}" in error for error in errors)


def test_verifier_rejects_a_manifest_that_mixes_the_two_accepted_names(tmp_path):
    manifest = _manifest()
    manifest["components"]["sessionfind"]["source"]["repository"] = RENAMED_REPOSITORY
    path = _write(tmp_path, manifest)

    def no_network(url: str) -> tuple[int, str]:
        raise AssertionError(f"must not look up {url}")

    errors = _module().verify_manifest(path, fetch=no_network)

    assert any("does not match" in error for error in errors)


def test_verifier_rejects_a_manifest_recording_a_different_repository_than_the_one_expected(tmp_path):
    manifest = _manifest(REPOSITORY)
    path = _write(tmp_path, manifest)

    def no_network(url: str) -> tuple[int, str]:
        raise AssertionError(f"must not look up {url}")

    errors = _module().verify_manifest(path, fetch=no_network, expected_repository=RENAMED_REPOSITORY)

    assert errors == [f"component manifest records {REPOSITORY}, expected {RENAMED_REPOSITORY}"]


def test_verifier_rejects_an_expected_repository_outside_the_two_accepted_names(tmp_path):
    manifest = _manifest(REPOSITORY)
    path = _write(tmp_path, manifest)

    errors = _module().verify_manifest(path, fetch=lambda url: (404, ""), expected_repository="attacker/brigade")

    assert any("attacker/brigade" in error and "not one of" in error for error in errors)


def test_verifier_does_not_accept_release_urls_of_the_other_repository(tmp_path):
    # Component sources name the new repository but the asset URLs still point at the old one.
    manifest = _manifest(RENAMED_REPOSITORY)
    for component in manifest["components"].values():
        for asset in component["assets"].values():
            asset["download_url"] = asset["download_url"].replace(RENAMED_REPOSITORY, REPOSITORY)
    release, checksums, body = _release(manifest, RENAMED_REPOSITORY)
    path = _write(tmp_path, manifest, body)

    errors = _module().verify_manifest(path, fetch=_fetcher(release, checksums, body, repository=RENAMED_REPOSITORY))

    assert any("immutable Brigade release URL" in error for error in errors)


@pytest.mark.parametrize("repository", [REPOSITORY, RENAMED_REPOSITORY])
def test_print_repository_reports_the_recorded_repository_without_network(tmp_path, capsys, repository):
    path = _write(tmp_path, _manifest(repository))

    assert _module().main(["--manifest", str(path), "--print-repository"]) == 0
    assert capsys.readouterr().out == repository + "\n"


@pytest.mark.parametrize(
    "defect", ["third", "mixed", "missing", "one-component", "extra-key", "non-dict", "duplicate-key", "empty"]
)
def test_print_repository_fails_closed_for_an_untrusted_manifest(tmp_path, capsys, defect):
    manifest = _manifest()
    body = None
    if defect == "third":
        for component in manifest["components"].values():
            component["source"]["repository"] = "attacker/brigade"
    elif defect == "mixed":
        manifest["components"]["sessionfind"]["source"]["repository"] = RENAMED_REPOSITORY
    elif defect == "missing":
        del manifest["components"]["sessionfind"]["source"]
    elif defect == "one-component":
        manifest["components"] = {"sessionfind": manifest["components"]["sessionfind"]}
    elif defect == "extra-key":
        manifest["components"]["extra-tool"] = manifest["components"]["sessionfind"]
    elif defect == "non-dict":
        manifest["components"]["sessionfind"] = "escoffier-labs/brigade"
    elif defect == "empty":
        manifest["components"] = {}
    elif defect == "duplicate-key":
        # The second "components" key would silently win under plain json.loads.
        good = json.dumps(manifest["components"])
        body = '{"components": ' + good + ', "components": ' + good + "}"
    path = _write(tmp_path, manifest, body)

    assert _module().main(["--manifest", str(path), "--print-repository"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("component manifest provenance: ")
    assert captured.err.strip() != "component manifest provenance:"


def test_verifier_rejects_a_manifest_with_a_duplicate_key(tmp_path):
    manifest = _manifest()
    good = json.dumps(manifest["components"])
    path = _write(tmp_path, manifest, '{"components": ' + good + ', "components": ' + good + "}")

    errors = _module().verify_manifest(path, fetch=lambda url: (_ for _ in ()).throw(AssertionError(url)))

    assert any("duplicate manifest key" in error for error in errors)


# After the rename, GitHub's release API reports a pre-rename release's assets under the
# new name (and the reverse). The manifest's own URLs stay strict to the recorded name.


def _release_with_api_urls(manifest: dict, repository: str, api_repository: str):
    release, checksums, body = _release(manifest, repository)
    for asset in release["assets"]:
        asset["browser_download_url"] = asset["browser_download_url"].replace(repository, api_repository)
    return release, checksums, body


@pytest.mark.parametrize(("recorded", "reported"), [(REPOSITORY, RENAMED_REPOSITORY), (RENAMED_REPOSITORY, REPOSITORY)])
def test_verifier_accepts_release_api_urls_under_the_other_accepted_name(tmp_path, recorded, reported):
    manifest = _manifest(recorded)
    release, checksums, body = _release_with_api_urls(manifest, recorded, reported)
    path = _write(tmp_path, manifest, body)

    assert _module().verify_manifest(path, fetch=_fetcher(release, checksums, body, repository=recorded)) == []


def test_verifier_rejects_release_api_urls_under_a_third_repository(tmp_path):
    manifest = _manifest(REPOSITORY)
    release, checksums, body = _release_with_api_urls(manifest, REPOSITORY, "attacker/brigade")
    path = _write(tmp_path, manifest, body)

    errors = _module().verify_manifest(path, fetch=_fetcher(release, checksums, body, repository=REPOSITORY))

    assert any("immutable Brigade release URL" in error for error in errors)
    assert any("URL does not match manifest" in error for error in errors)


# The names are labels; the immutable repository ID is the identity. A reused old name resolves to
# another repository, so a self-consistent impostor release must never verify (#2816).


def test_verifier_pins_the_repository_id_next_to_the_two_names():
    module = _module()

    assert module.REPOSITORY_ID == REPOSITORY_ID == 1238165856


@pytest.mark.parametrize("repository", [REPOSITORY, RENAMED_REPOSITORY])
def test_verifier_accepts_either_name_when_it_resolves_to_the_real_repository_id(tmp_path, repository):
    manifest = _manifest(repository)
    release, checksums, body = _release(manifest, repository)
    path = _write(tmp_path, manifest, body)

    fetch = _fetcher(release, checksums, body, repository=repository, repository_id=REPOSITORY_ID)

    assert _module().verify_manifest(path, fetch=fetch) == []


@pytest.mark.parametrize("repository", [REPOSITORY, RENAMED_REPOSITORY])
@pytest.mark.parametrize("impostor_id", [1, REPOSITORY_ID + 1, "1238165856", None, True])
def test_verifier_rejects_a_reused_name_before_any_release_tag_or_asset_lookup(tmp_path, repository, impostor_id):
    # The impostor publishes a complete, self-consistent release under the reused name.
    manifest = _manifest(repository)
    release, checksums, body = _release(manifest, repository)
    path = _write(tmp_path, manifest, body)
    inner = _fetcher(release, checksums, body, repository=repository, repository_id=impostor_id)
    seen = []

    def fetch(url):
        seen.append(url)
        return inner(url)

    errors = _module().verify_manifest(path, fetch=fetch)

    assert any("repository ID" in error for error in errors)
    assert seen == [f"https://api.github.com/repos/{repository}"]


@pytest.mark.parametrize("status", [404, 403, 500])
def test_verifier_rejects_a_repository_lookup_that_fails(tmp_path, status):
    manifest = _manifest()
    path = _write(tmp_path, manifest)

    errors = _module().verify_manifest(path, fetch=lambda url: (status, "{}"))

    assert errors == [f"GitHub repository lookup failed for {REPOSITORY}: HTTP {status}"]


def test_verifier_rejects_a_repository_lookup_that_is_not_json(tmp_path):
    path = _write(tmp_path, _manifest())

    errors = _module().verify_manifest(path, fetch=lambda url: (200, "<html>"))

    assert errors == [f"GitHub repository lookup returned invalid JSON for {REPOSITORY}"]


def _attestation(identifier):
    # The shape `gh attestation verify --format json` prints for a v0.31.0 release asset.
    return [{"verificationResult": {"signature": {"certificate": {"sourceRepositoryIdentifier": identifier}}}}]


def test_attestation_check_accepts_the_brigade_repository_id():
    assert _module().attestation_repository_errors(json.dumps(_attestation("1238165856"))) == []


@pytest.mark.parametrize(
    "payload",
    [
        _attestation("999"),
        _attestation(1238165856),  # the certificate field is a decimal string
        _attestation(None),
        [],
        {},
        [{}],
        [{"verificationResult": {"signature": {}}}],
        _attestation("1238165856") + _attestation("999"),
    ],
)
def test_attestation_check_rejects_any_other_or_missing_repository_id(payload):
    assert _module().attestation_repository_errors(json.dumps(payload))


def test_attestation_check_rejects_output_that_is_not_json():
    assert _module().attestation_repository_errors("not json") == ["attestation verification output is not JSON"]


@pytest.mark.parametrize(("identifier", "code"), [("1238165856", 0), ("2", 1)])
def test_check_attestation_json_cli_exit_code_follows_the_repository_id(tmp_path, capsys, identifier, code):
    path = tmp_path / "attestation.json"
    path.write_text(json.dumps(_attestation(identifier)))

    assert _module().main(["--check-attestation-json", str(path)]) == code
    assert ("repository ID" in capsys.readouterr().err) == (code == 1)


def test_check_attestation_json_cli_rejects_an_unreadable_file(tmp_path, capsys):
    assert _module().main(["--check-attestation-json", str(tmp_path / "missing.json")]) == 1
    assert "could not read attestation output" in capsys.readouterr().err
