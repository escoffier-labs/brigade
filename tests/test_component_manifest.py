"""Tests for brigade component manifest v1."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brigade import component_manifest

MISELEDGER_BASE = "https://github.com/escoffier-labs/miseledger/releases/download/v0.6.0/"
GRAPHTRAIL_BASE = "https://github.com/escoffier-labs/graphtrail/releases/download/v0.4.0/"
GRAPHTRAIL_SHA = "64fcd2f9ec37f33e286708845a92e6cfa4abf3bb"

GRAPHTRAIL_V040_ASSETS = [
    (
        "graphtrail",
        "darwin-amd64",
        "graphtrail-darwin-amd64",
        11695172,
        "eb6768be11d26a9d82c1bcecd503a9fdc7c883bda3bd627dea9ccb04fd31379c",
    ),
    (
        "graphtrail",
        "darwin-arm64",
        "graphtrail-darwin-arm64",
        11426112,
        "20534dbbb84f134a5a892520fd5a695d2520c2e040733b5535e991c611c99686",
    ),
    (
        "graphtrail",
        "linux-amd64",
        "graphtrail-linux-amd64",
        12802256,
        "e78c73a80a2eadbe297066739044e2c5fcdd187c8219198f453bd004bf9c9a55",
    ),
    (
        "graphtrail",
        "linux-arm64",
        "graphtrail-linux-arm64",
        12424560,
        "7f961a4f018e4b12b9dcf5227a7cdc20fa7cdd9a723d96ec0336e254adf33c07",
    ),
    (
        "graphtrail",
        "windows-amd64",
        "graphtrail-windows-amd64.exe",
        10753536,
        "1a9adc002c81661d2b0838e642f1c9db2671a2808c93e6b4499cfc2b33d6ea22",
    ),
    (
        "graphtrail-mcp",
        "darwin-amd64",
        "graphtrail-mcp-darwin-amd64",
        11004028,
        "d8d605a522d3894c36a2f30e94cdcc99b87c34d05e53b01f4c47018eaebaf93f",
    ),
    (
        "graphtrail-mcp",
        "darwin-arm64",
        "graphtrail-mcp-darwin-arm64",
        10766928,
        "649abe87a3e40415d1933db493c94f1049d84577a996df7ba9c27c4b3c4cf597",
    ),
    (
        "graphtrail-mcp",
        "linux-amd64",
        "graphtrail-mcp-linux-amd64",
        11981944,
        "66606e4e394973766e1e91d3b0b78b26bd9077076cc85e62b9d0faf9780e7154",
    ),
    (
        "graphtrail-mcp",
        "linux-arm64",
        "graphtrail-mcp-linux-arm64",
        11609512,
        "0bf17d38d44c5fc4f3132e17078a2db74e92da9bcb27a40c3d8345056523a651",
    ),
    (
        "graphtrail-mcp",
        "windows-amd64",
        "graphtrail-mcp-windows-amd64.exe",
        10020864,
        "ec642c7e736fff1673c3d7e06d10eb02c9782008d8f783ab2b40699e69a35b08",
    ),
]

MISELEDGER_V060_ASSETS = [
    (
        "miseledger",
        "darwin-amd64",
        "miseledger-darwin-amd64",
        16659744,
        "fb952aefd763a624e2c0346e7cc22dde8af812649ece97bd6cb62f16bc9881df",
    ),
    (
        "miseledger",
        "darwin-arm64",
        "miseledger-darwin-arm64",
        15784018,
        "ae13b508d84d651e388218275511ca13176a4747c6b93dfc78c7ab6bc9cdca1f",
    ),
    (
        "miseledger",
        "linux-amd64",
        "miseledger-linux-amd64",
        16441315,
        "246893c8c39318f774fc7a06338b5a8e87bf84661b1951251b2c0c971e9a7a6c",
    ),
    (
        "miseledger",
        "linux-arm64",
        "miseledger-linux-arm64",
        15314138,
        "425ea81daaf9fc793862e2d66f49ef948dc1ea186d3db56df49544dca5e26b32",
    ),
    (
        "miseledger",
        "windows-amd64",
        "miseledger-windows-amd64.exe",
        16616960,
        "033f9a492435068cd7e2bd1882422c1419189f60898071411656595a93640e39",
    ),
    (
        "sessionfind",
        "darwin-amd64",
        "sessionfind-darwin-amd64",
        16659776,
        "33359c447f58954463a1a7a02253f7090db52f6e8189c406c7707bc20844689d",
    ),
    (
        "sessionfind",
        "darwin-arm64",
        "sessionfind-darwin-arm64",
        15784066,
        "0219288e4bd1df8cf49989ab83753f6b7f564eee072f63af1db8a7460a902771",
    ),
    (
        "sessionfind",
        "linux-amd64",
        "sessionfind-linux-amd64",
        16445238,
        "5b42f573d44f4301b0e1cfc2008842849c7704c0f96e390c2f5461f1f8059ac0",
    ),
    (
        "sessionfind",
        "linux-arm64",
        "sessionfind-linux-arm64",
        15315773,
        "80695c1d92a0a01cffe8248c655bba3fe2579bb931c35ca311e7067eef69ab37",
    ),
    (
        "sessionfind",
        "windows-amd64",
        "sessionfind-windows-amd64.exe",
        16620032,
        "b8bb05fe9b310012893cd49d11f4acf27171194b9a7ab492fa5d2dc1614b0842",
    ),
]


def _minimal_known_component(
    *,
    component_revision: str = "v0.6.0",
    repository: str = "escoffier-labs/miseledger",
    release_tag: str | None = "v0.6.0",
    executable: str = "miseledger",
    assets: dict | None = None,
) -> dict:
    source: dict[str, str] = {"repository": repository}
    asset_map = assets if assets is not None else {}
    if asset_map:
        if release_tag is None:
            raise ValueError("release_tag is required when assets are published")
        source["release_tag"] = release_tag
    elif release_tag is not None:
        raise ValueError("release_tag must be omitted when assets are unpublished")
    return {
        "component_revision": component_revision,
        "source": source,
        "executable": executable,
        "assets": asset_map,
    }


def _miseledger_asset(asset_name: str, byte_size: int, sha256: str) -> dict:
    return {
        "asset_name": asset_name,
        "byte_size": byte_size,
        "sha256": sha256,
        "download_url": MISELEDGER_BASE + asset_name,
    }


def _full_miseledger_assets(component_id: str) -> dict:
    assets: dict = {}
    for comp, platform, asset_name, byte_size, sha256 in MISELEDGER_V060_ASSETS:
        if comp != component_id:
            continue
        assets[platform] = _miseledger_asset(asset_name, byte_size, sha256)
    return assets


def _graphtrail_asset(asset_name: str, byte_size: int, sha256: str) -> dict:
    return {
        "asset_name": asset_name,
        "byte_size": byte_size,
        "sha256": sha256,
        "download_url": GRAPHTRAIL_BASE + asset_name,
    }


def _full_graphtrail_assets(component_id: str) -> dict:
    assets: dict = {}
    for comp, platform, asset_name, byte_size, sha256 in GRAPHTRAIL_V040_ASSETS:
        if comp != component_id:
            continue
        assets[platform] = _graphtrail_asset(asset_name, byte_size, sha256)
    return assets


def _write_manifest(tmp_path: Path, **overrides) -> Path:
    payload = {
        "schema_version": 1,
        "brigade_version": "test",
        "manifest_revision": "2026-07-18",
        "supported_platforms": list(component_manifest.SUPPORTED_PLATFORMS),
        "components": {
            "graphtrail": _minimal_known_component(
                component_revision=GRAPHTRAIL_SHA,
                repository="escoffier-labs/graphtrail",
                release_tag="v0.4.0",
                executable="graphtrail",
                assets=_full_graphtrail_assets("graphtrail"),
            ),
            "graphtrail-mcp": _minimal_known_component(
                component_revision=GRAPHTRAIL_SHA,
                repository="escoffier-labs/graphtrail",
                release_tag="v0.4.0",
                executable="graphtrail-mcp",
                assets=_full_graphtrail_assets("graphtrail-mcp"),
            ),
            "miseledger": _minimal_known_component(assets=_full_miseledger_assets("miseledger")),
            "sessionfind": _minimal_known_component(
                executable="sessionfind",
                assets=_full_miseledger_assets("sessionfind"),
            ),
        },
    }
    payload.update(overrides)
    path = tmp_path / "manifest-v1.json"
    path.write_text(json.dumps(payload))
    return path


def test_bundled_manifest_pins_miseledger_and_graphtrail_assets():
    manifest = component_manifest.load()

    assert manifest.schema_version == 1
    assert manifest.brigade_version == "0.24.0"
    assert manifest.manifest_revision == "2026-07-19"
    assert manifest.supported_platforms == component_manifest.SUPPORTED_PLATFORMS
    assert set(manifest.components) == set(component_manifest.KNOWN_COMPONENT_IDS)
    assert manifest.components["miseledger"].component_revision == "v0.6.0"
    assert manifest.components["miseledger"].source.release_tag == "v0.6.0"
    asset = manifest.components["miseledger"].assets["linux-amd64"]
    assert asset.asset_name == "miseledger-linux-amd64"
    assert asset.byte_size == 16441315
    assert asset.sha256 == "246893c8c39318f774fc7a06338b5a8e87bf84661b1951251b2c0c971e9a7a6c"
    assert asset.download_url.endswith("/miseledger-linux-amd64")
    assert set(manifest.components["graphtrail"].assets) == set(component_manifest.SUPPORTED_PLATFORMS)
    assert manifest.components["graphtrail"].source.release_tag == "v0.4.0"
    assert set(manifest.components["graphtrail-mcp"].assets) == set(component_manifest.SUPPORTED_PLATFORMS)
    assert manifest.components["graphtrail-mcp"].source.release_tag == "v0.4.0"
    assert manifest.unknown_component_diagnostics == ()


@pytest.mark.parametrize(
    ("component_id", "platform", "asset_name", "byte_size", "sha256"),
    MISELEDGER_V060_ASSETS,
    ids=[f"{comp}-{plat}" for comp, plat, *_ in MISELEDGER_V060_ASSETS],
)
def test_bundled_manifest_pins_miseledger_v060_assets(component_id, platform, asset_name, byte_size, sha256):
    manifest = component_manifest.load()
    asset = manifest.components[component_id].assets[platform]
    assert asset.asset_name == asset_name
    assert asset.byte_size == byte_size
    assert asset.sha256 == sha256
    assert asset.download_url == MISELEDGER_BASE + asset_name
    if platform == "windows-amd64":
        assert asset_name.endswith(".exe")
    else:
        assert not asset_name.endswith(".exe")


def test_bundled_manifest_pins_graphtrail_v040_assets():
    manifest = component_manifest.load()
    asset = manifest.components["graphtrail"].assets["linux-amd64"]
    assert asset.byte_size == 12802256
    assert asset.sha256 == "e78c73a80a2eadbe297066739044e2c5fcdd187c8219198f453bd004bf9c9a55"
    assert manifest.components["graphtrail"].source.release_tag == "v0.4.0"


def test_bundled_manifest_pins_graphtrail_mcp_v040_assets():
    manifest = component_manifest.load()
    asset = manifest.components["graphtrail-mcp"].assets["linux-amd64"]
    assert asset.byte_size == 11981944
    assert asset.sha256 == "66606e4e394973766e1e91d3b0b78b26bd9077076cc85e62b9d0faf9780e7154"


def test_bundled_manifest_pins_graphtrail_to_git_sha():
    manifest = component_manifest.load()
    for component_id in ("graphtrail", "graphtrail-mcp"):
        revision = manifest.components[component_id].component_revision
        assert revision == GRAPHTRAIL_SHA
        assert len(revision) == 40
        assert all(ch in "0123456789abcdef" for ch in revision)


def test_schema_contract_requires_exact_platform_order_and_known_components():
    schema = json.loads((Path(__file__).resolve().parents[1] / "docs/component-manifest-v1.schema.json").read_text())
    supported = schema["properties"]["supported_platforms"]
    assert supported["const"] == list(component_manifest.SUPPORTED_PLATFORMS)

    components = schema["properties"]["components"]
    assert components["required"] == list(component_manifest.KNOWN_COMPONENT_IDS)
    for component_id in ("miseledger", "sessionfind"):
        component_schema = components["properties"][component_id]
        assert component_schema["allOf"][0]["$ref"] == "#/$defs/known_component"
        assert component_schema["allOf"][1]["properties"]["executable"]["const"] == component_id
    graphtrail_paired_branches = [
        {
            "properties": {
                "source": {"$ref": "#/$defs/unpublished_source"},
                "assets": {"$ref": "#/$defs/empty_assets"},
            },
            "required": ["source", "assets"],
        },
        {
            "properties": {
                "source": {"$ref": "#/$defs/published_source"},
                "assets": {"$ref": "#/$defs/published_assets"},
            },
            "required": ["source", "assets"],
        },
    ]
    for component_id in ("graphtrail", "graphtrail-mcp"):
        component_schema = components["properties"][component_id]
        base = component_schema["allOf"][0]
        assert base["type"] == "object"
        assert base["additionalProperties"] is False
        assert base["properties"]["executable"]["const"] == component_id
        assert component_schema["allOf"][1]["oneOf"] == graphtrail_paired_branches
    assert components["additionalProperties"] is True

    known_component = schema["$defs"]["known_component"]
    assert known_component["additionalProperties"] is False
    assert set(known_component["required"]) == {
        "component_revision",
        "source",
        "executable",
        "assets",
    }

    graphtrail_revision = schema["$defs"]["graphtrail_component_revision"]
    assert graphtrail_revision["pattern"] == "^[0-9a-f]{40}$"
    for component_id in ("graphtrail", "graphtrail-mcp"):
        revision = components["properties"][component_id]["allOf"][0]["properties"]["component_revision"]
        assert revision == {"$ref": "#/$defs/graphtrail_component_revision"}

    platform_keys = list(component_manifest.SUPPORTED_PLATFORMS)
    published_assets = schema["$defs"]["published_assets"]
    assert published_assets["required"] == platform_keys
    assert set(published_assets["properties"]) == set(platform_keys)
    assert published_assets["additionalProperties"] is False
    for component_id in ("miseledger", "sessionfind"):
        assets = components["properties"][component_id]["allOf"][1]["properties"]["assets"]
        assert assets == {"$ref": "#/$defs/published_assets"}
        source = components["properties"][component_id]["allOf"][1]["properties"]["source"]
        assert source == {"$ref": "#/$defs/published_source"}

    published_source = schema["$defs"]["published_source"]
    assert published_source["required"] == ["repository", "release_tag"]
    assert set(published_source["properties"]) == {"repository", "release_tag"}

    asset = schema["$defs"]["asset"]
    assert asset["additionalProperties"] is False
    assert set(asset["properties"]) == {"asset_name", "byte_size", "sha256", "download_url"}
    assert asset["properties"]["download_url"]["pattern"] == "^https://[^?#]+/[^/?#]+$"

    empty_assets = schema["$defs"]["empty_assets"]
    assert empty_assets["type"] == "object"
    assert empty_assets["additionalProperties"] is False
    assert empty_assets["maxProperties"] == 0
    assert "unpublished_assets" not in schema["$defs"]

    unpublished_source = schema["$defs"]["unpublished_source"]
    assert unpublished_source["required"] == ["repository"]
    assert set(unpublished_source["properties"]) == {"repository"}

    unix_asset = schema["$defs"]["unix_asset"]
    assert "linux|darwin" in unix_asset["allOf"][1]["properties"]["asset_name"]["pattern"]
    windows_asset = schema["$defs"]["windows_asset"]
    assert windows_asset["allOf"][1]["properties"]["asset_name"]["pattern"].endswith("\\.exe$")


def test_manifest_rejects_unknown_schema_versions(tmp_path):
    path = _write_manifest(tmp_path, schema_version=2)
    with pytest.raises(ValueError, match="unsupported component manifest schema version 2"):
        component_manifest.load(path)

    path = _write_manifest(tmp_path, schema_version=True)
    with pytest.raises(ValueError, match="unsupported component manifest schema version"):
        component_manifest.load(path)


def test_manifest_rejects_unsupported_host_platform_matrix(tmp_path):
    path = _write_manifest(tmp_path, supported_platforms=["linux-amd64"])
    with pytest.raises(ValueError, match="supported_platforms"):
        component_manifest.load(path)


def test_manifest_rejects_reordered_supported_platforms(tmp_path):
    reordered = [
        "linux-arm64",
        "linux-amd64",
        "darwin-amd64",
        "darwin-arm64",
        "windows-amd64",
    ]
    path = _write_manifest(tmp_path, supported_platforms=reordered)
    with pytest.raises(ValueError, match="supported_platforms"):
        component_manifest.load(path)


def test_manifest_rejects_partial_graphtrail_assets(tmp_path):
    base = json.loads(_write_manifest(tmp_path).read_text())
    base["components"]["graphtrail"]["assets"] = {
        "linux-amd64": _miseledger_asset(
            "graphtrail-linux-amd64",
            100,
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )
    }
    path = tmp_path / "partial-graphtrail.json"
    path.write_text(json.dumps(base))
    with pytest.raises(ValueError, match="must publish the full supported platform matrix"):
        component_manifest.load(path)


def test_manifest_accepts_full_graphtrail_matrix(tmp_path):
    base = json.loads(_write_manifest(tmp_path).read_text())
    release_tag = "v0.1.0"
    assets = {}
    for platform, asset_name, byte_size, sha256 in [
        ("linux-amd64", "graphtrail-linux-amd64", 100, "a" * 64),
        ("linux-arm64", "graphtrail-linux-arm64", 100, "b" * 64),
        ("darwin-amd64", "graphtrail-darwin-amd64", 100, "c" * 64),
        ("darwin-arm64", "graphtrail-darwin-arm64", 100, "d" * 64),
        (
            "windows-amd64",
            "graphtrail-windows-amd64.exe",
            100,
            "e" * 64,
        ),
    ]:
        assets[platform] = {
            "asset_name": asset_name,
            "byte_size": byte_size,
            "sha256": sha256,
            "download_url": (
                f"https://github.com/escoffier-labs/graphtrail/releases/download/{release_tag}/{asset_name}"
            ),
        }
    base["components"]["graphtrail"]["assets"] = assets
    base["components"]["graphtrail"]["source"]["release_tag"] = release_tag
    path = tmp_path / "full-graphtrail.json"
    path.write_text(json.dumps(base))
    manifest = component_manifest.load(path)
    assert set(manifest.components["graphtrail"].assets) == set(component_manifest.SUPPORTED_PLATFORMS)
    assert manifest.components["graphtrail"].source.release_tag == release_tag


def test_manifest_rejects_invalid_asset_platform_key(tmp_path):
    base = json.loads(_write_manifest(tmp_path).read_text())
    asset = base["components"]["miseledger"]["assets"]["linux-amd64"]
    base["components"]["miseledger"]["assets"] = {"freebsd-amd64": asset}
    path = tmp_path / "bad-platform-key.json"
    path.write_text(json.dumps(base))
    with pytest.raises(ValueError, match="invalid platform key 'freebsd-amd64'"):
        component_manifest.load(path)


def test_manifest_rejects_out_of_matrix_asset_platform_key(tmp_path):
    base = json.loads(_write_manifest(tmp_path).read_text())
    asset = base["components"]["miseledger"]["assets"]["linux-amd64"]
    base["components"]["miseledger"]["assets"] = {"windows-arm64": asset}
    path = tmp_path / "out-of-matrix-platform-key.json"
    path.write_text(json.dumps(base))
    with pytest.raises(ValueError, match="outside the Phase 1 support matrix"):
        component_manifest.load(path)


def test_manifest_ignores_unknown_components_with_deterministic_diagnostic(tmp_path):
    path = _write_manifest(tmp_path)
    base = json.loads(path.read_text())
    base["components"]["future-tool"] = {
        "component_revision": "v9",
        "source": {"repository": "example/future"},
        "executable": "future-tool",
        "assets": {},
    }
    path.write_text(json.dumps(base))

    manifest = component_manifest.load(path)

    assert "future-tool" not in manifest.components
    assert manifest.unknown_component_diagnostics == (
        "component manifest lists unknown component 'future-tool'; known components: "
        "graphtrail, graphtrail-mcp, miseledger, sessionfind",
    )


@pytest.mark.parametrize("value", ["future-tool", ["future-tool"], None])
def test_manifest_ignores_unknown_components_regardless_of_value_shape(tmp_path, value):
    path = _write_manifest(tmp_path)
    base = json.loads(path.read_text())
    base["components"]["future-tool"] = value
    path.write_text(json.dumps(base))

    manifest = component_manifest.load(path)

    assert "future-tool" not in manifest.components
    assert manifest.unknown_component_diagnostics == (
        "component manifest lists unknown component 'future-tool'; known components: "
        "graphtrail, graphtrail-mcp, miseledger, sessionfind",
    )


def test_manifest_rejects_unexpected_top_level_key(tmp_path):
    path = _write_manifest(tmp_path, extra_field="nope")
    with pytest.raises(ValueError, match="manifest field 'extra_field' is not supported"):
        component_manifest.load(path)


def test_manifest_rejects_unexpected_known_component_key(tmp_path):
    base = json.loads(_write_manifest(tmp_path).read_text())
    base["components"]["miseledger"]["extra_field"] = True
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(base))
    with pytest.raises(ValueError, match="component 'miseledger' field 'extra_field' is not supported"):
        component_manifest.load(path)


def test_manifest_rejects_unexpected_source_key(tmp_path):
    base = json.loads(_write_manifest(tmp_path).read_text())
    base["components"]["miseledger"]["source"]["extra_field"] = True
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(base))
    with pytest.raises(ValueError, match="component 'miseledger' source field 'extra_field' is not supported"):
        component_manifest.load(path)


def test_manifest_rejects_unexpected_asset_key(tmp_path):
    base = json.loads(_write_manifest(tmp_path).read_text())
    base["components"]["miseledger"]["assets"]["linux-amd64"]["extra_field"] = True
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(base))
    with pytest.raises(
        ValueError,
        match="component 'miseledger' platform 'linux-amd64' field 'extra_field' is not supported",
    ):
        component_manifest.load(path)


def test_manifest_accepts_alternate_valid_graphtrail_git_sha(tmp_path):
    alternate_sha = "a" * 40
    base = json.loads(_write_manifest(tmp_path).read_text())
    base["components"]["graphtrail"]["component_revision"] = alternate_sha
    path = tmp_path / "alt-sha.json"
    path.write_text(json.dumps(base))
    manifest = component_manifest.load(path)
    assert manifest.components["graphtrail"].component_revision == alternate_sha


def test_resolve_unknown_component_lists_known_components(tmp_path):
    manifest = component_manifest.load(_write_manifest(tmp_path))
    with pytest.raises(ValueError, match="unknown component 'missing'"):
        component_manifest.resolve_asset(manifest, "missing", "linux-amd64")


def test_resolve_graphtrail_returns_pinned_asset(tmp_path):
    manifest = component_manifest.load(_write_manifest(tmp_path))
    asset = component_manifest.resolve_asset(manifest, "graphtrail", "linux-amd64")
    assert asset.asset_name == "graphtrail-linux-amd64"


def test_resolve_miseledger_returns_pinned_asset(tmp_path):
    manifest = component_manifest.load(_write_manifest(tmp_path))
    asset = component_manifest.resolve_asset(manifest, "miseledger", "linux-amd64")
    assert asset.asset_name == "miseledger-linux-amd64"


def test_manifest_rejects_malformed_known_asset_fields(tmp_path):
    base = json.loads(_write_manifest(tmp_path).read_text())
    base["components"]["miseledger"]["assets"]["linux-amd64"]["sha256"] = "not-hex"
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(base))
    with pytest.raises(ValueError, match="component 'miseledger' platform 'linux-amd64' field 'sha256'"):
        component_manifest.load(path)


def test_manifest_rejects_asset_names_that_escape_cache(tmp_path):
    base = json.loads(_write_manifest(tmp_path).read_text())
    base["components"]["miseledger"]["assets"]["linux-amd64"]["asset_name"] = "../miseledger"
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(base))
    with pytest.raises(ValueError, match="must be 'miseledger-linux-amd64'"):
        component_manifest.load(path)


def test_manifest_rejects_executable_mismatch(tmp_path):
    base = json.loads(_write_manifest(tmp_path).read_text())
    base["components"]["miseledger"]["executable"] = "wrong-name"
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(base))
    with pytest.raises(ValueError, match="field 'executable' must equal the component id"):
        component_manifest.load(path)


def test_manifest_rejects_partial_miseledger_assets(tmp_path):
    base = json.loads(_write_manifest(tmp_path).read_text())
    del base["components"]["miseledger"]["assets"]["linux-arm64"]
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(base))
    with pytest.raises(ValueError, match="must publish the full supported platform matrix"):
        component_manifest.load(path)


def test_manifest_rejects_empty_sessionfind_assets(tmp_path):
    base = json.loads(_write_manifest(tmp_path).read_text())
    base["components"]["sessionfind"]["assets"] = {}
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(base))
    with pytest.raises(ValueError, match="must publish assets for every supported platform"):
        component_manifest.load(path)


def test_manifest_rejects_graphtrail_revision_that_is_not_git_sha(tmp_path, monkeypatch):
    monkeypatch.setattr(component_manifest, "UNPUBLISHED_COMPONENT_IDS", frozenset({"graphtrail"}))
    base = json.loads(_write_manifest(tmp_path).read_text())
    base["components"]["graphtrail"]["assets"] = {}
    del base["components"]["graphtrail"]["source"]["release_tag"]
    base["components"]["graphtrail"]["component_revision"] = "unreleased"
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(base))
    with pytest.raises(ValueError, match="must be a 40-character lowercase git SHA"):
        component_manifest.load(path)


def test_manifest_rejects_published_component_without_release_tag(tmp_path):
    base = json.loads(_write_manifest(tmp_path).read_text())
    del base["components"]["miseledger"]["source"]["release_tag"]
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(base))
    with pytest.raises(ValueError, match="source.release_tag' must be set when assets are published"):
        component_manifest.load(path)


def test_manifest_rejects_unpublished_component_with_release_tag(tmp_path, monkeypatch):
    monkeypatch.setattr(component_manifest, "UNPUBLISHED_COMPONENT_IDS", frozenset({"graphtrail"}))
    base = json.loads(_write_manifest(tmp_path).read_text())
    base["components"]["graphtrail"]["source"]["release_tag"] = "v0.1.0"
    base["components"]["graphtrail"]["assets"] = {}
    base["components"]["graphtrail-mcp"]["assets"] = {}
    del base["components"]["graphtrail-mcp"]["source"]["release_tag"]
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(base))
    with pytest.raises(ValueError, match="source.release_tag' must be omitted when assets are unpublished"):
        component_manifest.load(path)


def test_manifest_accepts_sha_revision_with_semantic_release_tag(tmp_path):
    base = json.loads(_write_manifest(tmp_path).read_text())
    base["components"]["graphtrail"]["component_revision"] = GRAPHTRAIL_SHA
    release_tag = "v0.1.0"
    assets = {}
    for platform, asset_name, byte_size, sha256 in [
        ("linux-amd64", "graphtrail-linux-amd64", 100, "a" * 64),
        ("linux-arm64", "graphtrail-linux-arm64", 100, "b" * 64),
        ("darwin-amd64", "graphtrail-darwin-amd64", 100, "c" * 64),
        ("darwin-arm64", "graphtrail-darwin-arm64", 100, "d" * 64),
        ("windows-amd64", "graphtrail-windows-amd64.exe", 100, "e" * 64),
    ]:
        assets[platform] = {
            "asset_name": asset_name,
            "byte_size": byte_size,
            "sha256": sha256,
            "download_url": (
                f"https://github.com/escoffier-labs/graphtrail/releases/download/{release_tag}/{asset_name}"
            ),
        }
    base["components"]["graphtrail"]["assets"] = assets
    base["components"]["graphtrail"]["source"]["release_tag"] = release_tag
    path = tmp_path / "sha-with-release-tag.json"
    path.write_text(json.dumps(base))
    manifest = component_manifest.load(path)
    assert manifest.components["graphtrail"].component_revision == GRAPHTRAIL_SHA
    assert manifest.components["graphtrail"].source.release_tag == release_tag


@pytest.mark.parametrize(
    "download_url",
    [
        "http://github.com/escoffier-labs/miseledger/releases/download/v0.6.0/miseledger-linux-amd64",
        "https:///miseledger-linux-amd64",
        MISELEDGER_BASE + "miseledger-linux-amd64?cache=1",
        MISELEDGER_BASE + "miseledger-linux-amd64#frag",
        MISELEDGER_BASE + "evil-miseledger-linux-amd64",
    ],
)
def test_manifest_rejects_bad_download_urls(tmp_path, download_url):
    base = json.loads(_write_manifest(tmp_path).read_text())
    base["components"]["miseledger"]["assets"]["linux-amd64"]["download_url"] = download_url
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(base))
    with pytest.raises(ValueError, match="download_url"):
        component_manifest.load(path)


def test_manifest_rejects_wrong_windows_asset_suffix(tmp_path):
    base = json.loads(_write_manifest(tmp_path).read_text())
    base["components"]["miseledger"]["assets"]["windows-amd64"]["asset_name"] = "miseledger-windows-amd64"
    base["components"]["miseledger"]["assets"]["windows-amd64"]["download_url"] = (
        MISELEDGER_BASE + "miseledger-windows-amd64"
    )
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(base))
    with pytest.raises(ValueError, match="must be 'miseledger-windows-amd64.exe'"):
        component_manifest.load(path)


def test_platform_key_uses_go_names_and_rejects_unsupported_values():
    assert component_manifest.platform_key(system="Linux", machine="x86_64") == "linux-amd64"
    assert component_manifest.platform_key(system="Darwin", machine="arm64") == "darwin-arm64"
    assert component_manifest.platform_key(system="Windows", machine="AMD64") == "windows-amd64"
    with pytest.raises(ValueError, match="unsupported platform"):
        component_manifest.platform_key(system="linux", machine="plan9")


def test_platform_key_hard_failure_lists_supported_keys():
    with pytest.raises(ValueError, match="supported platform keys: linux-amd64"):
        component_manifest.platform_key(system="Plan9", machine="mips")
