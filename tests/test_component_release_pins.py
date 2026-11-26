"""Coverage for the historical release trust anchors shipped in the CLI."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from brigade import component_install, component_manifest, component_release_pins, update_cmd

_V0321_COMMIT = "088da09cb0561c62e5121156b07dfdffe6807f9c"
_V0321_MANIFEST = Path(__file__).parent / "fixtures" / "component-manifest-v0.32.1.json"


def test_every_pinned_release_has_complete_fingerprints() -> None:
    asset_pins = component_release_pins._ASSET_PINS
    digests = component_release_pins.MANIFEST_DIGESTS
    assert asset_pins.keys() == digests.keys()
    assert {"v0.30.0", "v0.32.0", "v0.32.1"} <= {revision.partition("+")[0] for revision in digests}
    for revision, pins in asset_pins.items():
        components = (
            component_manifest.DEFAULT_COMPONENT_IDS
            if revision.startswith("v0.30.0+")
            else component_manifest.KNOWN_COMPONENT_IDS
        )
        assert pins.keys() == {
            (component, platform)
            for component in components
            for platform in component_manifest.COMPONENT_PLATFORMS.get(
                component, component_manifest.SUPPORTED_PLATFORMS
            )
        }, revision
        assert re.fullmatch(r"[0-9a-f]{64}", digests[revision]), revision
        for asset, (size, digest) in pins.items():
            assert type(size) is int and size > 0, (revision, asset)
            assert re.fullmatch(r"[0-9a-f]{64}", digest), (revision, asset)


def test_v0321_published_manifest_authenticates_offline() -> None:
    # Restore the published LF framing on checkouts that convert text to CRLF.
    content = _V0321_MANIFEST.read_bytes().replace(b"\r\n", b"\n")
    release = component_install._authenticate_release_manifest(content, "v0.32.1", offline=True)

    assert release.target_commit == _V0321_COMMIT
    manifest = update_cmd.validate_release_manifest_bytes(release, allow_compatible_stable_manifest=True)
    for component_id, component in manifest.components.items():
        for platform, asset in component.assets.items():
            assert component_release_pins.release_asset_pin(manifest.manifest_revision, component_id, platform) == (
                asset.byte_size,
                asset.sha256,
            )

    # Changing one whitespace byte leaves the JSON valid and only changes its digest.
    tampered = content.replace(b"\n", b" ", 1)
    assert tampered != content
    with pytest.raises(component_install.ComponentInstallError, match="package-pinned SHA-256 digest"):
        component_install._authenticate_release_manifest(tampered, "v0.32.1", offline=True)
