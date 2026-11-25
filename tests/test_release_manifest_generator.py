"""Contract tests for the deterministic unified-release manifest generator."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "generate_component_manifest.py"
COMPONENTS = (
    "agent-notify",
    "brigade-code-graph",
    "brigade-code-graph-mcp",
    "brigade-evidence",
    "brigade-hub",
    "sessionfind",
)
PLATFORMS = ("linux-amd64", "linux-arm64", "darwin-amd64", "darwin-arm64", "windows-amd64")
# brigade-hub ships for Linux only.
LINUX = ("linux-amd64", "linux-arm64")


def _platforms(component: str) -> tuple[str, ...]:
    return LINUX if component == "brigade-hub" else PLATFORMS


@pytest.fixture(autouse=True)
def _workflow_repository(monkeypatch):
    # The generator records GITHUB_REPOSITORY; pin it so the suite does not depend on the runner.
    monkeypatch.setenv("GITHUB_REPOSITORY", "escoffier-labs/brigade")


@pytest.fixture()
def generator_module():
    spec = importlib.util.spec_from_file_location("generate_component_manifest_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _asset_name(component: str, platform: str) -> str:
    return f"{component}-{platform}" + (".exe" if platform.startswith("windows-") else "")


def _write_inventory(root: Path) -> set[str]:
    names: set[str] = set()
    for component in COMPONENTS:
        for platform in _platforms(component):
            name = _asset_name(component, platform)
            (root / name).write_bytes(name.encode())
            names.add(name)
    return names


def test_generator_writes_stable_unified_release_manifest_and_checksums(generator_module, tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    expected_names = _write_inventory(assets)
    manifest_path = tmp_path / "component-manifest-v1.json"
    checksums_path = tmp_path / "checksums.txt"

    generator_module.generate_manifest(
        tag="v0.33.0",
        commit="a" * 40,
        assets_dir=assets,
        output=manifest_path,
        checksums_output=checksums_path,
    )

    manifest = json.loads(manifest_path.read_text())
    assert manifest["schema_version"] == 1
    assert manifest["brigade_version"] == "0.33.0"
    assert manifest["supported_platforms"] == list(PLATFORMS)
    assert set(manifest["components"]) == set(COMPONENTS)
    for component in COMPONENTS:
        assert manifest["components"][component]["component_revision"] == "a" * 40
        assert manifest["components"][component]["source"] == {
            "repository": "escoffier-labs/brigade",
            "release_tag": "v0.33.0",
        }
        assert set(manifest["components"][component]["assets"]) == set(_platforms(component))
        for platform, asset in manifest["components"][component]["assets"].items():
            assert asset["asset_name"] == _asset_name(component, platform)
            assert asset["download_url"] == (
                "https://github.com/escoffier-labs/brigade/releases/download/v0.33.0/" + asset["asset_name"]
            )
            assert asset["sha256"] == hashlib.sha256(asset["asset_name"].encode()).hexdigest()

    checksum_names = {line.split()[1] for line in checksums_path.read_text().splitlines()}
    assert checksum_names == expected_names | {"component-manifest-v1.json"}


@pytest.mark.parametrize("name", ["brigade-hub-darwin-arm64", "brigade-hub-windows-amd64.exe"])
def test_generator_rejects_a_brigade_hub_asset_outside_linux(generator_module, tmp_path, name):
    assets = tmp_path / "assets"
    assets.mkdir()
    _write_inventory(assets)
    (assets / name).write_bytes(b"not shipped")
    with pytest.raises(ValueError, match="unexpected native assets: " + name.replace(".", r"\.")):
        generator_module.generate_manifest(
            tag="v0.33.0",
            commit="a" * 40,
            assets_dir=assets,
            output=tmp_path / "manifest.json",
            checksums_output=tmp_path / "checksums.txt",
        )


def test_generator_rejects_missing_extra_or_duplicate_native_assets(generator_module, tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    _write_inventory(assets)
    (assets / "brigade-code-graph-linux-amd64").unlink()

    with pytest.raises(ValueError, match="missing native assets"):
        generator_module.generate_manifest(
            tag="v0.33.0",
            commit="a" * 40,
            assets_dir=assets,
            output=tmp_path / "manifest.json",
            checksums_output=tmp_path / "checksums.txt",
        )

    (assets / "brigade-code-graph-linux-amd64").write_bytes(b"restored asset")
    (assets / "unexpected").write_text("no")
    with pytest.raises(ValueError, match="unexpected native assets"):
        generator_module.generate_manifest(
            tag="v0.33.0",
            commit="a" * 40,
            assets_dir=assets,
            output=tmp_path / "manifest.json",
            checksums_output=tmp_path / "checksums.txt",
        )


def test_generator_rejects_retired_asset(generator_module, tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    _write_inventory(assets)
    (assets / "graphtrail-linux-amd64").write_bytes(b"retired asset")
    with pytest.raises(ValueError, match="unexpected native assets"):
        generator_module.generate_manifest(
            tag="v0.33.0",
            commit="a" * 40,
            assets_dir=assets,
            output=tmp_path / "manifest.json",
            checksums_output=tmp_path / "checksums.txt",
        )


@pytest.mark.parametrize("defect", ["partial", "empty", "foreign", "platform", "duplicate"])
def test_generator_rejects_invalid_native_inventory_before_writing(generator_module, tmp_path, defect):
    assets = tmp_path / "assets"
    assets.mkdir()
    _write_inventory(assets)
    if defect == "partial":
        (assets / "brigade-evidence-windows-amd64.exe").unlink()
    elif defect == "empty":
        (assets / "brigade-evidence-linux-amd64").write_bytes(b"")
    else:
        name = {
            "foreign": "foreign-linux-amd64",
            "platform": "brigade-evidence-freebsd-amd64",
            "duplicate": "brigade-evidence-linux-amd64-copy",
        }[defect]
        (assets / name).write_bytes(b"invalid")
    output = tmp_path / "component-manifest-v1.json"
    checksums = tmp_path / "checksums.txt"
    with pytest.raises(ValueError):
        generator_module.generate_manifest(
            tag="v0.33.0", commit="a" * 40, assets_dir=assets, output=output, checksums_output=checksums
        )
    assert not output.exists()
    assert not checksums.exists()


def test_generated_manifest_passes_release_provenance(generator_module, tmp_path):
    from tests.test_component_manifest_provenance import _fetcher, _module, _release

    assets = tmp_path / "assets"
    assets.mkdir()
    _write_inventory(assets)
    output = tmp_path / "component-manifest-v1.json"
    manifest = generator_module.generate_manifest(
        tag="v0.33.0", commit="a" * 40, assets_dir=assets, output=output, checksums_output=tmp_path / "checksums.txt"
    )
    release, checksums, body = _release(manifest, aliases=False)
    assert output.read_bytes() == body.encode()
    assert _module().verify_manifest(output, fetch=_fetcher(release, checksums, body)) == []


def test_generator_preserves_lf_bytes_with_windows_text_defaults(generator_module, tmp_path, monkeypatch):
    original_open = Path.open

    def windows_open(path, mode="r", buffering=-1, encoding=None, errors=None, newline=None):
        if "w" in mode and "b" not in mode and newline is None:
            newline = "\r\n"
        return original_open(path, mode, buffering, encoding, errors, newline)

    monkeypatch.setattr(Path, "open", windows_open)
    assets = tmp_path / "assets"
    assets.mkdir()
    _write_inventory(assets)
    output = tmp_path / "manifest.json"
    checksums = tmp_path / "checksums.txt"
    manifest = generator_module.generate_manifest(
        tag="v0.33.0", commit="a" * 40, assets_dir=assets, output=output, checksums_output=checksums
    )
    assert output.read_bytes() == (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    assert b"\r" not in checksums.read_bytes()


@pytest.mark.parametrize("repository", ["escoffier-labs/brigade", "escoffier-labs/brigadestack"])
def test_generator_records_the_workflow_repository_and_the_result_verifies(
    generator_module, tmp_path, monkeypatch, repository
):
    from tests.test_component_manifest_provenance import _fetcher, _module, _release

    monkeypatch.setenv("GITHUB_REPOSITORY", repository)
    assets = tmp_path / "assets"
    assets.mkdir()
    _write_inventory(assets)
    output = tmp_path / "component-manifest-v1.json"
    manifest = generator_module.generate_manifest(
        tag="v0.33.0", commit="a" * 40, assets_dir=assets, output=output, checksums_output=tmp_path / "checksums.txt"
    )

    for component in manifest["components"].values():
        assert component["source"]["repository"] == repository
        for asset in component["assets"].values():
            assert asset["download_url"].startswith(f"https://github.com/{repository}/releases/download/v0.33.0/")
    release, checksums, body = _release(manifest, repository, aliases=False)
    fetch = _fetcher(release, checksums, body, repository=repository)
    assert _module().verify_manifest(output, fetch=fetch, expected_repository=repository) == []


@pytest.mark.parametrize("repository", ["attacker/brigade", "escoffier-labs/brigade-hub", ""])
def test_generator_refuses_to_record_an_unaccepted_repository_and_writes_nothing(
    generator_module, tmp_path, monkeypatch, repository
):
    monkeypatch.setenv("GITHUB_REPOSITORY", repository)
    assets = tmp_path / "assets"
    assets.mkdir()
    _write_inventory(assets)
    output = tmp_path / "manifest.json"
    checksums = tmp_path / "checksums.txt"

    with pytest.raises(ValueError, match="must be one of"):
        generator_module.generate_manifest(
            tag="v0.33.0", commit="a" * 40, assets_dir=assets, output=output, checksums_output=checksums
        )
    assert not output.exists() and not checksums.exists()


def test_generator_explicit_repository_overrides_the_environment(generator_module, tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    _write_inventory(assets)
    manifest = generator_module.generate_manifest(
        tag="v0.33.0",
        commit="a" * 40,
        assets_dir=assets,
        output=tmp_path / "manifest.json",
        checksums_output=tmp_path / "checksums.txt",
        repository="escoffier-labs/brigadestack",
    )

    assert {c["source"]["repository"] for c in manifest["components"].values()} == {"escoffier-labs/brigadestack"}
