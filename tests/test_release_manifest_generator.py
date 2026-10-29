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
COMPONENTS = ("agent-notify", "graphtrail", "graphtrail-mcp", "miseledger", "sessionfind")
PLATFORMS = ("linux-amd64", "linux-arm64", "darwin-amd64", "darwin-arm64", "windows-amd64")


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
        for platform in PLATFORMS:
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
        tag="v1.2.3",
        commit="a" * 40,
        assets_dir=assets,
        output=manifest_path,
        checksums_output=checksums_path,
    )

    manifest = json.loads(manifest_path.read_text())
    assert manifest["schema_version"] == 1
    assert manifest["brigade_version"] == "1.2.3"
    assert manifest["supported_platforms"] == list(PLATFORMS)
    assert set(manifest["components"]) == set(COMPONENTS)
    for component in COMPONENTS:
        assert manifest["components"][component]["component_revision"] == "a" * 40
        assert manifest["components"][component]["source"] == {
            "repository": "escoffier-labs/brigade",
            "release_tag": "v1.2.3",
        }
        assert set(manifest["components"][component]["assets"]) == set(PLATFORMS)
        for platform, asset in manifest["components"][component]["assets"].items():
            assert asset["asset_name"] == _asset_name(component, platform)
            assert asset["download_url"] == (
                "https://github.com/escoffier-labs/brigade/releases/download/v1.2.3/" + asset["asset_name"]
            )
            assert asset["sha256"] == hashlib.sha256(asset["asset_name"].encode()).hexdigest()

    checksum_names = {line.split()[1] for line in checksums_path.read_text().splitlines()}
    assert checksum_names == expected_names | {"component-manifest-v1.json"}


def test_generator_rejects_missing_extra_or_duplicate_native_assets(generator_module, tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    _write_inventory(assets)
    (assets / "graphtrail-linux-amd64").unlink()

    with pytest.raises(ValueError, match="missing native assets"):
        generator_module.generate_manifest(
            tag="v1.2.3",
            commit="a" * 40,
            assets_dir=assets,
            output=tmp_path / "manifest.json",
            checksums_output=tmp_path / "checksums.txt",
        )

    (assets / "graphtrail-linux-amd64").write_bytes(b"graphtrail-linux-amd64")
    (assets / "unexpected").write_text("no")
    with pytest.raises(ValueError, match="unexpected native assets"):
        generator_module.generate_manifest(
            tag="v1.2.3",
            commit="a" * 40,
            assets_dir=assets,
            output=tmp_path / "manifest.json",
            checksums_output=tmp_path / "checksums.txt",
        )


ALIASES = ("brigade-code-graph", "brigade-code-graph-mcp", "brigade-evidence")


def _write_aliases(root: Path) -> set[str]:
    names = {_asset_name(component, platform) for component in ALIASES for platform in PLATFORMS}
    for name in names:
        (root / name).write_bytes(name.encode())
    return names


def test_generator_additive_inventory_preserves_primary_manifest_and_hashes_every_file(generator_module, tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    legacy = _write_inventory(assets)
    output = tmp_path / "component-manifest-v1.json"
    checksums = tmp_path / "checksums.txt"
    kwargs = dict(tag="v1.2.3", commit="a" * 40, assets_dir=assets, output=output, checksums_output=checksums)
    generator_module.generate_manifest(**kwargs)
    legacy_manifest = output.read_bytes()
    aliases = _write_aliases(assets)

    generator_module.generate_manifest(**kwargs)

    assert output.read_bytes() == legacy_manifest
    assert generator_module.expected_asset_names() == legacy
    entries = dict(line.split()[::-1] for line in checksums.read_text().splitlines())
    assert len(entries) == 41
    assert set(entries) == legacy | aliases | {output.name}
    for name in legacy | aliases:
        assert entries[name] == hashlib.sha256((assets / name).read_bytes()).hexdigest()
    assert entries[output.name] == hashlib.sha256(output.read_bytes()).hexdigest()


@pytest.mark.parametrize("defect", ["partial", "empty", "foreign", "platform", "duplicate"])
def test_generator_rejects_invalid_additive_inventory_before_writing(generator_module, tmp_path, defect):
    assets = tmp_path / "assets"
    assets.mkdir()
    _write_inventory(assets)
    _write_aliases(assets)
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
            tag="v1.2.3", commit="a" * 40, assets_dir=assets, output=output, checksums_output=checksums
        )
    assert not output.exists()
    assert not checksums.exists()
