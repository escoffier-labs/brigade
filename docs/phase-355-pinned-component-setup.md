# Phase 355: Pinned Component Setup

Parent: [#352](https://github.com/escoffier-labs/brigade/issues/352). Issue: [#355](https://github.com/escoffier-labs/brigade/issues/355).

Goal: add `brigade setup` as a top-level command that installs all four manifest-backed native components (GraphTrail, graphtrail-mcp, MiseLedger, sessionfind) into the user-local Brigade managed bin with SHA-256 and byte-size verification, digest-keyed caching, offline cache hits, dry-run reporting, one-step rollback, and post-install smoke that invokes only absolute managed paths.

Architecture: the bundled `templates/components/manifest-v1.json` shipped inside the installed `brigade-cli` release is the sole manifest source. Setup rejects a `brigade_version` mismatch between the running Brigade and the bundled manifest. A new `component_install` module downloads and verifies every asset before mutating managed binaries, materializes executables via temp-sibling `os.replace`, runs smoke only on absolute managed paths, and commits `installed.json` only after smoke passes. `installed.previous.json` stores one prior state and rotates only when manifest revision or component digests change. Rollback swaps current and prior state using verified cache entries. `brigade add` and its Cargo/Go/npm fallbacks remain unchanged. PATH is never mutated automatically.

Out of scope: [#356](https://github.com/escoffier-labs/brigade/issues/356) doctor/version reporting and [#357](https://github.com/escoffier-labs/brigade/issues/357) Windows porting beyond the existing `component_paths` Windows behavior.

Key tech: Python standard library (`urllib.request`, `hashlib`, `os.replace`, `stat`), existing `component_manifest`, `component_paths`, and `localio` helpers.

## Command Alternatives (Scouting)

### Alternative A — Top-level `brigade setup` (recommended)

Add `brigade setup` as a first-class top-level command with flags `--dry-run`, `--offline`, and `--rollback`. A dedicated `component_install` module owns download, verify, cache, materialize, smoke, state, and rollback. `brigade add` stays station-oriented with unchanged Cargo/Go/npm fallbacks.

Tradeoffs: matches the stated “top-level brigade setup” requirement, keeps install semantics separate from station wiring, and limits blast radius to new files plus manifest publication.

### Alternative B — Native-first inside `brigade add`

`brigade add <station>` checks the bundled manifest for matching tools and downloads pinned natives when assets exist; otherwise runs existing `install_args`.

Tradeoffs: reuses a familiar verb but mixes two install models (toolchain source builds vs pinned binaries), makes dry-run/rollback harder because `add` prints wiring guidance and is station-scoped, and couples manifest publication to station names.

### Alternative C — `brigade components` command group

Expose `brigade components install`, `status`, and `rollback` backed by the same engine as Alternative A.

Tradeoffs: clearer future growth surface, but does not satisfy the top-level `brigade setup` requirement without an alias and adds CLI surface area before Phase 1 needs it.

**Recommendation:** Alternative A. Register `setup` in `COMMAND_GROUPS` under “Stations and tools”. The `component_install` module can later back a `components` group if reporting (#356) needs it.

## Sous-Mode Decision Record

| Decision | Basis | Label |
| --- | --- | --- |
| Top-level `brigade setup` instead of extending `brigade add` | Issue #355 and parent #352 require top-level setup | stated-constraint |
| Bundled installed-release manifest is authoritative; reject `brigade_version` mismatch | Prevents applying pins from a different Brigade build than the one running setup | stated-constraint |
| Publish graphtrail and graphtrail-mcp v0.4.0 assets at commit `64fcd2f9ec37f33e286708845a92e6cfa4abf3bb` with release evidence sizes/digests | Release assets exist; manifest must carry full platform matrix before setup can install GraphTrail tools | evidence |
| Install all four components by default | Phase 1 acceptance requires a clean machine to complete setup without per-component flags | stated-constraint |
| Flags limited to `--dry-run`, `--offline`, `--rollback` | Approved interface; no `--component`, no PATH flags | stated-constraint |
| No automatic PATH mutation | PATH edits are fragile and platform-specific; doctor and docs can reference managed absolute paths | judgment |
| Keep `brigade add` Cargo/Go/npm fallbacks unchanged | Compatibility window and existing `managed.py` install_args remain the alternate install path | evidence |
| Cache at `cached_asset_path(cache_root, sha256, asset_name)` | `component_paths.cached_asset_path` already encodes digest/name layout | evidence |
| Verify byte size and SHA-256 before caching, materializing, or executing | Requirement and existing `localio.file_sha256` pattern | stated-constraint |
| Online: replace bad cache only after a verified temp download; offline: fail on bad cache | Prevents clobbering a recoverable cache with a partial download while keeping offline strict | stated-constraint |
| Download and verify all assets before changing any managed bin | Atomic failure domain: no half-upgraded bin directory | stated-constraint |
| Materialize via temp sibling + `os.replace` with executable bit; restore prior bytes on later failure | Matches `localio.write_text_atomic` swap semantics for binaries | evidence |
| Post-install smoke uses only absolute managed paths | Acceptance criterion: smoke must not accidentally pass via PATH | stated-constraint |
| Commit `installed.json` only after smoke passes | State must reflect a verified, runnable install | stated-constraint |
| `installed.previous.json` holds one prior state; rotate only on manifest/digest change; idempotent repeat does not rotate | Rollback and repeat-install requirements | stated-constraint |
| Tests inject temp roots; never use live network or real `$HOME` | CI determinism and contributor safety | stated-constraint |

## GraphTrail v0.4.0 Release Evidence

Commit: `64fcd2f9ec37f33e286708845a92e6cfa4abf3bb`. Release tag: `v0.4.0`. Repository: `escoffier-labs/graphtrail`.

Download URL pattern: `https://github.com/escoffier-labs/graphtrail/releases/download/v0.4.0/<asset_name>`.

### graphtrail

| Platform | asset_name | byte_size | sha256 |
| --- | --- | ---: | --- |
| darwin-amd64 | graphtrail-darwin-amd64 | 11695172 | eb6768be11d26a9d82c1bcecd503a9fdc7c883bda3bd627dea9ccb04fd31379c |
| darwin-arm64 | graphtrail-darwin-arm64 | 11426112 | 20534dbbb84f134a5a892520fd5a695d2520c2e040733b5535e991c611c99686 |
| linux-amd64 | graphtrail-linux-amd64 | 12802256 | e78c73a80a2eadbe297066739044e2c5fcdd187c8219198f453bd004bf9c9a55 |
| linux-arm64 | graphtrail-linux-arm64 | 12424560 | 7f961a4f018e4b12b9dcf5227a7cdc20fa7cdd9a723d96ec0336e254adf33c07 |
| windows-amd64 | graphtrail-windows-amd64.exe | 10753536 | 1a9adc002c81661d2b0838e642f1c9db2671a2808c93e6b4499cfc2b33d6ea22 |

### graphtrail-mcp

| Platform | asset_name | byte_size | sha256 |
| --- | --- | ---: | --- |
| darwin-amd64 | graphtrail-mcp-darwin-amd64 | 11004028 | d8d605a522d3894c36a2f30e94cdcc99b87c34d05e53b01f4c47018eaebaf93f |
| darwin-arm64 | graphtrail-mcp-darwin-arm64 | 10766928 | 649abe87a3e40415d1933db493c94f1049d84577a996df7ba9c27c4b3c4cf597 |
| linux-amd64 | graphtrail-mcp-linux-amd64 | 11981944 | 66606e4e394973766e1e91d3b0b78b26bd9077076cc85e62b9d0faf9780e7154 |
| linux-arm64 | graphtrail-mcp-linux-arm64 | 11609512 | 0bf17d38d44c5fc4f3132e17078a2db74e92da9bcb27a40c3d8345056523a651 |
| windows-amd64 | graphtrail-mcp-windows-amd64.exe | 10020864 | ec642c7e736fff1673c3d7e06d10eb02c9782008d8f783ab2b40699e69a35b08 |

MiseLedger and sessionfind remain pinned at v0.6.0 per the current bundled manifest; setup installs them through the same engine.

## Installed State Schema v1

Path: `component_paths.installed_state_path(data_root)` → `<data_root>/brigade/installed.json`.

Prior path: `component_paths.installed_previous_state_path(data_root)` → `<data_root>/brigade/installed.previous.json`.

```json
{
  "schema_version": 1,
  "brigade_version": "0.23.0",
  "manifest_revision": "2026-07-18",
  "platform": "linux-amd64",
  "installed_at": "2026-07-19T06:00:00.000000+00:00",
  "components": {
    "graphtrail": {
      "component_revision": "64fcd2f9ec37f33e286708845a92e6cfa4abf3bb",
      "asset_name": "graphtrail-linux-amd64",
      "byte_size": 12802256,
      "sha256": "e78c73a80a2eadbe297066739044e2c5fcdd187c8219198f453bd004bf9c9a55",
      "download_url": "https://github.com/escoffier-labs/graphtrail/releases/download/v0.4.0/graphtrail-linux-amd64",
      "executable": "/tmp/home/.local/share/brigade/bin/graphtrail"
    }
  }
}
```

`installed.previous.json` uses the same schema. Rotation copies the current file to `installed.previous.json` only when `manifest_revision` or any component `sha256` changes after a successful install. Idempotent repeat installs that verify matching cache and managed bytes leave `installed.previous.json` untouched.

## Setup Orchestration

1. Load bundled manifest via `component_manifest.load()`.
2. Compare `manifest.brigade_version` to `brigade.__version__`; exit non-zero on mismatch.
3. Resolve host platform via `component_manifest.platform_key()`.
4. For each component in `KNOWN_COMPONENT_IDS`, resolve asset with `component_manifest.resolve_asset`.
5. **Dry-run (`--dry-run`):** print brigade version, manifest revision, platform, and for each component: component revision, asset name, byte size, sha256, download URL, cache path, managed executable path, and planned actions (`verify-cache`, `download`, `materialize`, `smoke`). Write nothing. Exit 0.
6. **Rollback (`--rollback`):** require `installed.previous.json`. Verify every prior component cache entry (size + sha256). Swap managed executables from verified cache, swap `installed.json` ↔ `installed.previous.json` via atomic writes. Run smoke on restored managed paths. Exit non-zero on verification or smoke failure.
7. **Install (default):** for each asset, ensure cache file at `cached_asset_path` matches `byte_size` and `sha256`. Online: download to temp sibling, verify, then `os.replace` into cache (replacing a bad cache only after verified temp). Offline (`--offline`): require valid cache; fail if cache missing or corrupt. After all caches verified, snapshot current managed executables (if any) for restore-on-failure. Materialize each component to `managed_executable_path` via temp sibling + `os.replace` + executable bit (`0o755` on Unix). Run smoke suite on absolute managed paths. On any materialize or smoke failure, restore snapshotted executables. On success, rotate `installed.previous.json` when manifest revision or digests changed, then write `installed.json`.

### Post-install smoke (absolute paths only)

| Component | Command | Success |
| --- | --- | --- |
| graphtrail | `<managed>/graphtrail --version` | exit 0, non-empty stdout |
| graphtrail-mcp | `<managed>/graphtrail-mcp` JSON-RPC `initialize` on stdin | valid JSON-RPC response on stdout |
| miseledger | `<managed>/miseledger version` | exit 0 |
| sessionfind | `<managed>/sessionfind --help` | exit 2 with usage text in stdout or stderr |

## File Map

| File | Role |
| --- | --- |
| `src/brigade/component_paths.py` | Add `installed_previous_state_path`, `managed_bin_dir` |
| `src/brigade/component_state.py` | Load, validate, render, rotate installed state v1 |
| `src/brigade/component_install.py` | Download, verify, cache, materialize, smoke, orchestration |
| `src/brigade/cli/setup.py` | `brigade setup` argparse registration and dispatch |
| `src/brigade/cli/__init__.py` | Register `_setup_group` after `_add_group` |
| `src/brigade/cli/_common.py` | Add `setup` to `COMMAND_GROUPS` (“Stations and tools”) |
| `src/brigade/component_manifest.py` | Remove graphtrail IDs from `UNPUBLISHED_COMPONENT_IDS` after manifest publication |
| `src/brigade/templates/components/manifest-v1.json` | Publish graphtrail v0.4.0 assets; bump `manifest_revision` |
| `tests/test_component_install.py` | Engine and orchestration tests (no network) |
| `tests/test_cli_setup.py` | CLI wiring and flag tests |
| `tests/test_component_manifest.py` | Extend bundled-manifest assertions for graphtrail v0.4.0 assets |
| `tests/test_component_paths.py` | Assert `installed_previous_state_path` |
| `tests/test_cli_help.py` | Unchanged gate; fails if `setup` missing from `COMMAND_GROUPS` |

## Public Module Signatures

```python
# src/brigade/component_state.py
SCHEMA_VERSION = 1

@dataclass(frozen=True)
class InstalledComponentRecord:
    component_revision: str
    asset_name: str
    byte_size: int
    sha256: str
    download_url: str
    executable: str

@dataclass(frozen=True)
class InstalledState:
    schema_version: int
    brigade_version: str
    manifest_revision: str
    platform: str
    installed_at: str
    components: dict[str, InstalledComponentRecord]

def load_installed_state(path: Path) -> InstalledState | None
def render_installed_state(state: InstalledState) -> dict[str, object]
def state_digest_map(state: InstalledState) -> dict[str, str]
def should_rotate_previous(current: InstalledState | None, next_state: InstalledState) -> bool

# src/brigade/component_install.py
@dataclass(frozen=True)
class SetupPlanAction:
    component_id: str
    action: str  # verify-cache | download | materialize | smoke
    cache_path: str
    managed_path: str
    asset_name: str
    byte_size: int
    sha256: str
    download_url: str
    component_revision: str

@dataclass(frozen=True)
class SetupRoots:
    data_root: str
    cache_root: str
    env: Mapping[str, str]

class ComponentInstallError(RuntimeError)

def resolve_roots(*, env: Mapping[str, str] | None = None, system: str | None = None) -> SetupRoots
def build_setup_plan(manifest: ComponentManifest, *, platform: str, roots: SetupRoots) -> list[SetupPlanAction]
def verify_cached_asset(path: Path, *, byte_size: int, sha256: str) -> None
def fetch_asset_to_cache(asset: ComponentAsset, *, cache_path: Path, offline: bool, opener: Callable[..., object] | None = None) -> Path
def materialize_executable(*, cache_path: Path, managed_path: Path) -> None
def run_post_install_smoke(managed_paths: Mapping[str, str], *, runner: Callable[..., subprocess.CompletedProcess] | None = None) -> None
def setup_native_components(*, dry_run: bool = False, offline: bool = False, rollback: bool = False, env: Mapping[str, str] | None = None, opener: Callable[..., object] | None = None, runner: Callable[..., subprocess.CompletedProcess] | None = None) -> int
```

## Test Helpers

Add `tests/component_install_helpers.py`:

```python
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import brigade
from brigade import component_manifest

GRAPHTRAIL_SHA = "64fcd2f9ec37f33e286708845a92e6cfa4abf3bb"
GRAPHTRAIL_BASE = "https://github.com/escoffier-labs/graphtrail/releases/download/v0.4.0/"


def linux_env(root: Path) -> dict[str, str]:
    home = root / "home"
    home.mkdir(parents=True, exist_ok=True)
    return {
        "HOME": str(home),
        "XDG_DATA_HOME": str(root / "xdg-data"),
        "XDG_CACHE_HOME": str(root / "xdg-cache"),
    }


def fixture_payload(component_id: str, *, platform: str = "linux-amd64") -> tuple[bytes, int, str]:
    """Return (payload_bytes, byte_size, sha256) for deterministic offline fixtures."""
    body = f"fixture:{component_id}:{platform}\n".encode("ascii")
    digest = hashlib.sha256(body).hexdigest()
    return body, len(body), digest


def write_verified_cache(cache_path: Path, *, payload: bytes) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(payload)
    cache_path.chmod(0o755)


def test_manifest_asset(component_id: str, *, platform: str = "linux-amd64") -> component_manifest.ComponentAsset:
    _, byte_size, sha256 = fixture_payload(component_id, platform=platform)
    asset_name = f"{component_id}-{platform}"
    if platform.startswith("windows"):
        asset_name += ".exe"
    return component_manifest.ComponentAsset(
        asset_name=asset_name,
        byte_size=byte_size,
        sha256=sha256,
        download_url=f"https://example.invalid/components/{component_id}/{platform}",
    )


def write_test_manifest(path: Path, *, platform: str = "linux-amd64", brigade_version: str) -> component_manifest.ComponentManifest:
    """Write a manifest whose digests match fixture_payload bytes for offline engine tests."""
    components: dict[str, object] = {}
    for component_id in component_manifest.KNOWN_COMPONENT_IDS:
        _, byte_size, sha256 = fixture_payload(component_id, platform=platform)
        asset = test_manifest_asset(component_id, platform=platform)
        assert asset.byte_size == byte_size
        assert asset.sha256 == sha256
        components[component_id] = {
            "component_revision": f"fixture-{component_id}",
            "source": {"repository": f"https://example.invalid/{component_id}", "release_tag": "fixture"},
            "executable": component_id,
            "assets": {
                platform: {
                    "asset_name": asset.asset_name,
                    "byte_size": byte_size,
                    "sha256": sha256,
                    "download_url": asset.download_url,
                }
            },
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


def smoke_stub_script(name: str) -> str:
    if name == "graphtrail":
        return f'#!/usr/bin/env python3\nimport sys\nif sys.argv[1:] == ["--version"]:\n    print("graphtrail test 0.4.0")\n    raise SystemExit(0)\nraise SystemExit(1)\n'
    if name == "graphtrail-mcp":
        return (
            '#!/usr/bin/env python3\nimport json, sys\n'
            'req = json.load(sys.stdin)\n'
            'assert req.get("method") == "initialize"\n'
            'print(json.dumps({"jsonrpc": "2.0", "id": req.get("id"), "result": {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "graphtrail-mcp", "version": "0.4.0"}}}))\n'
        )
    if name == "miseledger":
        return '#!/usr/bin/env python3\nimport sys\nif sys.argv[1:] == ["version"]:\n    print("miseledger test 0.6.0")\n    raise SystemExit(0)\nraise SystemExit(1)\n'
    if name == "sessionfind":
        return '#!/usr/bin/env python3\nimport sys\nif sys.argv[1:] == ["--help"]:\n    print("usage: sessionfind [options]")\n    raise SystemExit(2)\nraise SystemExit(1)\n'
    raise ValueError(name)


class FakeOpener:
    def __init__(self, payloads: dict[str, bytes]):
        self.payloads = payloads
        self.calls: list[str] = []

    def __call__(self, url: str, *args, **kwargs):
        self.calls.append(url)
        from io import BytesIO
        from urllib.error import HTTPError

        if url not in self.payloads:
            raise HTTPError(url, 404, "not found", hdrs=None, fp=BytesIO(b""))
        return BytesIO(self.payloads[url])
```

Engine tests load `write_test_manifest(...)` from a temp path (or monkeypatch `component_manifest.load`) so every asset `byte_size` and `sha256` is derived from `fixture_payload` at test time. Never synthesize bytes to match an arbitrary digest.

## Implementation Recipe

Each task follows RED → GREEN through Brigade verify, then a small conventional commit. Verification command template:

```bash
brigade work verify run --target . \
  --command ".venv/bin/python -m pytest <test-target> -q" \
  --capture brigade-work
```

### Task 0: Publish graphtrail v0.4.0 assets in bundled manifest

**Files:** `src/brigade/templates/components/manifest-v1.json`, `src/brigade/component_manifest.py`, `tests/test_component_manifest.py`.

- [ ] Update manifest: set `manifest_revision` to `2026-07-19`, add full graphtrail and graphtrail-mcp asset matrices from the release evidence table with `source.release_tag` `v0.4.0` and `component_revision` `64fcd2f9ec37f33e286708845a92e6cfa4abf3bb`.
- [ ] Remove `graphtrail` and `graphtrail-mcp` from `UNPUBLISHED_COMPONENT_IDS`.
- [ ] Add failing bundled-manifest tests:

```python
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
```

- [ ] RED: `brigade work verify run --target . --command ".venv/bin/python -m pytest tests/test_component_manifest.py::test_bundled_manifest_pins_graphtrail_v040_assets tests/test_component_manifest.py::test_bundled_manifest_pins_graphtrail_mcp_v040_assets -q" --capture brigade-work`
- [ ] GREEN after manifest + `UNPUBLISHED_COMPONENT_IDS` update.
- [ ] Commit: `chore(components): publish graphtrail v0.4.0 manifest assets`.

### Task 1: Installed state path and schema

**Files:** `src/brigade/component_paths.py`, `src/brigade/component_state.py`, `tests/test_component_paths.py`, `tests/test_component_state.py`.

- [ ] Add `installed_previous_state_path(data_root_path: str) -> str`.
- [ ] Add failing path test:

```python
def test_installed_previous_state_path_uses_brigade_subdir():
    env = {"HOME": "/home/alice"}
    data = component_paths.data_root(env=env, system="linux")
    path = component_paths.installed_previous_state_path(data)
    assert path.endswith("brigade/installed.previous.json")
```

- [ ] Add failing state tests:

```python
def test_render_installed_state_round_trips_required_fields():
    state = InstalledState(
        schema_version=1,
        brigade_version="0.23.0",
        manifest_revision="2026-07-19",
        platform="linux-amd64",
        installed_at="2026-07-19T06:00:00+00:00",
        components={
            "miseledger": InstalledComponentRecord(
                component_revision="v0.6.0",
                asset_name="miseledger-linux-amd64",
                byte_size=16441315,
                sha256="246893c8c39318f774fc7a06338b5a8e87bf84661b1951251b2c0c971e9a7a6c",
                download_url="https://github.com/escoffier-labs/miseledger/releases/download/v0.6.0/miseledger-linux-amd64",
                executable="/tmp/xdg-data/brigade/bin/miseledger",
            )
        },
    )
    payload = render_installed_state(state)
    assert payload["schema_version"] == 1
    assert payload["installed_at"] == "2026-07-19T06:00:00+00:00"
    assert payload["components"]["miseledger"]["executable"].endswith("/brigade/bin/miseledger")


def test_should_rotate_previous_when_digest_changes():
    current = _sample_state(sha256="a" * 64)
    nxt = _sample_state(sha256="b" * 64)
    assert should_rotate_previous(current, nxt) is True


def test_should_not_rotate_previous_on_identical_digest_map():
    current = _sample_state(sha256="a" * 64)
    nxt = _sample_state(sha256="a" * 64, manifest_revision=current.manifest_revision)
    assert should_rotate_previous(current, nxt) is False
```

- [ ] RED → implement → GREEN on `tests/test_component_paths.py tests/test_component_state.py`.
- [ ] Commit: `feat(components): add installed state schema v1`.

### Task 2: Verify cached assets and brigade_version gate

**Files:** `src/brigade/component_install.py`, `tests/test_component_install.py`.

- [ ] Add failing tests:

```python
def test_verify_cached_asset_rejects_size_mismatch(tmp_path):
    path = tmp_path / "asset"
    path.write_bytes(b"short")
    with pytest.raises(ComponentInstallError, match="byte_size"):
        verify_cached_asset(path, byte_size=10, sha256="a" * 64)


def test_verify_cached_asset_rejects_digest_mismatch(tmp_path):
    path = tmp_path / "asset"
    path.write_bytes(b"0123456789")
    with pytest.raises(ComponentInstallError, match="sha256"):
        verify_cached_asset(path, byte_size=10, sha256="b" * 64)


def test_setup_rejects_brigade_version_mismatch(tmp_path, monkeypatch):
    env = linux_env(tmp_path)
    manifest_path = tmp_path / "manifest-v1.json"
    write_test_manifest(manifest_path, brigade_version="9.9.9")
    monkeypatch.setattr("brigade.__version__", "0.23.0", raising=False)
    monkeypatch.setattr(component_manifest, "manifest_path", lambda: manifest_path)
    rc = setup_native_components(env=env)
    assert rc == 1
```

- [ ] Implement `verify_cached_asset` using `path.stat().st_size` and `localio.file_sha256`.
- [ ] Implement manifest `brigade_version` check at start of `setup_native_components`.
- [ ] RED → GREEN on focused tests.
- [ ] Commit: `feat(components): verify cache bytes and brigade version`.

### Task 3: Download-to-cache with online replace and offline strictness

**Files:** `src/brigade/component_install.py`, `tests/test_component_install.py`, `tests/component_install_helpers.py`.

- [ ] Add failing tests:

```python
def test_fetch_asset_to_cache_writes_verified_bytes(tmp_path):
    asset = test_manifest_asset("miseledger", platform="linux-amd64")
    payload, byte_size, sha256 = fixture_payload("miseledger", platform="linux-amd64")
    assert asset.byte_size == byte_size
    assert asset.sha256 == sha256
    cache_path = tmp_path / "cache" / sha256 / asset.asset_name
    opener = FakeOpener({asset.download_url: payload})
    result = fetch_asset_to_cache(asset, cache_path=cache_path, offline=False, opener=opener)
    assert result == cache_path
    verify_cached_asset(cache_path, byte_size=byte_size, sha256=sha256)


def test_fetch_asset_offline_fails_when_cache_corrupt(tmp_path):
    asset = test_manifest_asset("miseledger", platform="linux-amd64")
    cache_path = tmp_path / "cache" / asset.sha256 / asset.asset_name
    cache_path.parent.mkdir(parents=True)
    cache_path.write_bytes(b"bad")
    with pytest.raises(ComponentInstallError, match="offline"):
        fetch_asset_to_cache(asset, cache_path=cache_path, offline=True)


def test_fetch_asset_online_replaces_bad_cache_only_after_verified_download(tmp_path):
    asset = test_manifest_asset("miseledger", platform="linux-amd64")
    payload, byte_size, sha256 = fixture_payload("miseledger", platform="linux-amd64")
    cache_path = tmp_path / "cache" / sha256 / asset.asset_name
    cache_path.parent.mkdir(parents=True)
    cache_path.write_bytes(b"stale")
    opener = FakeOpener({asset.download_url: payload})
    fetch_asset_to_cache(asset, cache_path=cache_path, offline=False, opener=opener)
    verify_cached_asset(cache_path, byte_size=byte_size, sha256=sha256)
```

- [ ] Implement temp-sibling download, verify, `os.replace` into cache; offline raises when cache invalid; online never replaces until verified temp passes.
- [ ] RED → GREEN.
- [ ] Commit: `feat(components): download assets into digest cache`.

### Task 4: Materialize managed executables with restore-on-failure

**Files:** `src/brigade/component_install.py`, `tests/test_component_install.py`.

- [ ] Add failing tests:

```python
def test_materialize_executable_sets_mode_and_replaces(tmp_path):
    cache_path = tmp_path / "cache.bin"
    managed_path = tmp_path / "bin" / "tool"
    cache_path.write_bytes(b"#!/bin/sh\n")
    materialize_executable(cache_path=cache_path, managed_path=managed_path)
    assert managed_path.read_bytes() == cache_path.read_bytes()
    assert oct(managed_path.stat().st_mode & 0o777) == oct(0o755)


def test_install_restores_prior_executable_when_smoke_fails(tmp_path, monkeypatch):
    env = linux_env(tmp_path)
    roots = resolve_roots(env=env, system="linux")
    prior = Path(roots.data_root) / "brigade" / "bin" / "miseledger"
    prior.parent.mkdir(parents=True)
    prior.write_bytes(b"OLD")
    prior.chmod(0o755)
    monkeypatch.setattr(component_install, "run_post_install_smoke", _raise_smoke_failure)
    with pytest.raises(ComponentInstallError):
        _install_with_stub_assets(env=env, opener=_all_good_opener(env))
    assert prior.read_bytes() == b"OLD"
```

- [ ] Implement snapshot/restore around materialize loop.
- [ ] RED → GREEN.
- [ ] Commit: `feat(components): materialize managed executables safely`.

### Task 5: Post-install smoke on absolute managed paths

**Files:** `src/brigade/component_install.py`, `tests/test_component_install.py`.

- [ ] Add failing test:

```python
def test_run_post_install_smoke_invokes_absolute_paths_only(tmp_path):
    managed = {}
    for name in ("graphtrail", "graphtrail-mcp", "miseledger", "sessionfind"):
        path = tmp_path / name
        path.write_text(smoke_stub_script(name))
        path.chmod(0o755)
        managed[name] = str(path)
    calls: list[list[str]] = []

    def runner(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    run_post_install_smoke(managed, runner=runner)
    assert {cmd[0] for cmd in calls} == set(managed.values())
```

- [ ] Implement smoke runner: `subprocess.run` with absolute argv[0], JSON-RPC stdin for graphtrail-mcp, accept exit 2 for sessionfind --help.
- [ ] RED → GREEN.
- [ ] Commit: `feat(components): add post-install smoke suite`.

### Task 6: Dry-run plan reporting

**Files:** `src/brigade/component_install.py`, `tests/test_component_install.py`.

- [ ] Add failing tests:

```python
def test_build_setup_plan_lists_all_four_components(tmp_path, monkeypatch):
    manifest_path = tmp_path / "manifest-v1.json"
    manifest = write_test_manifest(manifest_path, brigade_version=brigade.__version__)
    monkeypatch.setattr(component_manifest, "manifest_path", lambda: manifest_path)
    roots = resolve_roots(env=linux_env(tmp_path), system="linux")
    plan = build_setup_plan(manifest, platform="linux-amd64", roots=roots)
    assert {action.component_id for action in plan} == set(component_manifest.KNOWN_COMPONENT_IDS)


def test_setup_dry_run_writes_nothing(tmp_path, monkeypatch, capsys):
    env = linux_env(tmp_path)
    manifest_path = tmp_path / "manifest-v1.json"
    write_test_manifest(manifest_path, brigade_version=brigade.__version__)
    monkeypatch.setattr(component_manifest, "manifest_path", lambda: manifest_path)
    rc = setup_native_components(dry_run=True, env=env)
    out = capsys.readouterr().out
    assert rc == 0
    assert "miseledger-linux-amd64" in out
    assert "download" in out
    assert not (Path(env["XDG_DATA_HOME"]) / "brigade" / "installed.json").exists()
```

- [ ] Implement `build_setup_plan` and dry-run reporting in `component_install` only; no CLI files in this task.
- [ ] RED → GREEN on `tests/test_component_install.py` focused tests.
- [ ] Commit: `feat(components): add setup dry-run planning`.

### Task 7: Full install, idempotent repeat, and state commit after smoke

**Files:** `src/brigade/component_install.py`, `tests/test_component_install.py`.

- [ ] Add failing tests:

```python
def test_setup_install_writes_state_after_smoke(tmp_path, monkeypatch):
    env = linux_env(tmp_path)
    manifest_path = tmp_path / "manifest-v1.json"
    write_test_manifest(manifest_path, brigade_version=brigade.__version__)
    monkeypatch.setattr(component_manifest, "manifest_path", lambda: manifest_path)
    payloads = all_fixture_payloads(platform="linux-amd64")
    rc = setup_native_components(env=env, opener=FakeOpener(payloads))
    assert rc == 0
    state_path = Path(component_paths.installed_state_path(resolve_roots(env=env).data_root))
    state = json.loads(state_path.read_text())
    assert state["schema_version"] == 1
    assert set(state["components"]) == set(component_manifest.KNOWN_COMPONENT_IDS)


def test_repeat_setup_is_idempotent_and_skips_previous_rotation(tmp_path, monkeypatch):
    env = linux_env(tmp_path)
    manifest_path = tmp_path / "manifest-v1.json"
    write_test_manifest(manifest_path, brigade_version=brigade.__version__)
    monkeypatch.setattr(component_manifest, "manifest_path", lambda: manifest_path)
    payloads = all_fixture_payloads(platform="linux-amd64")
    opener = FakeOpener(payloads)
    assert setup_native_components(env=env, opener=opener) == 0
    roots = resolve_roots(env=env)
    previous_path = Path(component_paths.installed_previous_state_path(roots.data_root))
    previous_before = previous_path.read_text() if previous_path.is_file() else None
    assert setup_native_components(env=env, opener=opener) == 0
    if previous_before is not None:
        assert previous_path.read_text() == previous_before
```

- [ ] Implement full orchestration: download all → materialize all → smoke → rotate/write state.
- [ ] RED → GREEN.
- [ ] Commit: `feat(components): install pinned native components`.

### Task 8: Rollback via verified prior state

**Files:** `src/brigade/component_install.py`, `tests/test_component_install.py`.

- [ ] Add failing tests:

```python
def test_setup_rollback_restores_previous_manifest(tmp_path):
    env = linux_env(tmp_path)
    _seed_installed_pair(env, revision_a="2026-07-18", revision_b="2026-07-19")
    rc = setup_native_components(rollback=True, env=env)
    assert rc == 0
    state = json.loads(Path(component_paths.installed_state_path(resolve_roots(env=env).data_root)).read_text())
    assert state["manifest_revision"] == "2026-07-18"


def test_setup_rollback_fails_when_prior_cache_missing(tmp_path):
    env = linux_env(tmp_path)
    _seed_installed_pair(env, revision_a="2026-07-18", revision_b="2026-07-19", drop_prior_cache=True)
    assert setup_native_components(rollback=True, env=env) == 1
```

- [ ] Implement rollback swap and smoke on restored paths.
- [ ] RED → GREEN.
- [ ] Commit: `feat(components): add setup rollback`.

### Task 9: CLI registration and help coverage

**Files:** `src/brigade/cli/setup.py`, `src/brigade/cli/__init__.py`, `src/brigade/cli/_common.py`, `tests/test_cli_setup.py`, `tests/test_cli_help.py`.

- [ ] Create `src/brigade/cli/setup.py` and register parser:

```python
def register(sub: argparse._SubParsersAction) -> None:
    p_setup = sub.add_parser("setup", help="Install pinned native Brigade components.")
    p_setup.add_argument("--dry-run", action="store_true", help="Report planned actions without writing.")
    p_setup.add_argument("--offline", action="store_true", help="Use verified cache only; fail if missing.")
    p_setup.add_argument("--rollback", action="store_true", help="Restore the previous installed manifest.")
    p_setup.set_defaults(func=dispatch)
```

- [ ] Add `setup` to `COMMAND_GROUPS` and `_setup_group.register(sub)` after `_add_group.register(sub)`.
- [ ] Add failing CLI dispatch tests:

```python
def test_cli_setup_dry_run_flag(tmp_path, monkeypatch):
    env = linux_env(tmp_path)
    manifest_path = tmp_path / "manifest-v1.json"
    write_test_manifest(manifest_path, brigade_version=brigade.__version__)
    monkeypatch.setattr(component_manifest, "manifest_path", lambda: manifest_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert cli.main(["setup", "--dry-run"]) == 0


def test_cli_setup_offline_flag(tmp_path, monkeypatch):
    env = linux_env(tmp_path)
    manifest_path = tmp_path / "manifest-v1.json"
    write_test_manifest(manifest_path, brigade_version=brigade.__version__)
    monkeypatch.setattr(component_manifest, "manifest_path", lambda: manifest_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert cli.main(["setup", "--offline"]) == 1


def test_cli_setup_rollback_flag(tmp_path, monkeypatch):
    env = linux_env(tmp_path)
    _seed_installed_pair(env, revision_a="2026-07-18", revision_b="2026-07-19")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert cli.main(["setup", "--rollback"]) == 0
```

- [ ] RED: `tests/test_cli_help.py::test_command_groups_cover_every_command_exactly_once` fails until registration complete.
- [ ] GREEN after wiring.
- [ ] Commit: `feat(cli): register brigade setup command`.

### Task 10: Final verification and review gate

- [ ] Run full local gate:

```bash
./scripts/verify
```

- [ ] Run final Brigade verify:

```bash
brigade work verify run --target . \
  --command "./scripts/verify" \
  --capture brigade-work
```

- [ ] Capture outcome:

```bash
brigade outcome capture brigade-work --run-id latest --kind skill
```

- [ ] Content guard: ensure `git push` pre-push hook passes (no leaked hostnames, tokens, or personal paths in new files).
- [ ] Independent Opus review: dispatch `review` skill on the branch diff before opening the PR.
- [ ] Open PR with test plan checklist mirroring Tasks 0–9; confirm CI `content-guard`, `repo-metadata`, `install-from-source`, and `quickstart-smoke` jobs pass on the PR branch.
- [ ] Memory handoff: `.claude/memory-handoffs/<date>-issue-355-pinned-setup.md` recording manifest publication, rollback semantics, and smoke absolute-path requirement.

## Self-Review Checklist

- [x] Three scouting alternatives documented with recommendation.
- [x] Sous-mode decisions carry evidence, stated-constraint, or judgment labels.
- [x] GraphTrail v0.4.0 asset table matches release evidence byte-for-byte.
- [x] Every behavior maps to a file, test name, RED/GREEN verify command, and commit message.
- [x] `#356` reporting and `#357` porting explicitly excluded.
- [x] No automatic PATH mutation; Cargo/Go fallbacks unchanged.
- [x] Installed state uses `installed_at` consistently in schema, signatures, and tests.
- [x] Offline engine tests derive digests from `fixture_payload`; no reverse SHA-256 synthesis.
- [x] Task 6 is engine-only dry-run planning; Task 9 owns all CLI wiring and flag dispatch tests.
- [x] Tests require temp roots and stubbed network; no live GitHub or real home directory.

## Placeholder Scan

Run before committing this document (scan body only; exclude this section):

```bash
python -c "from pathlib import Path; import re, sys; text=Path('docs/phase-355-pinned-component-setup.md').read_text(); body=text.split('## Placeholder Scan', 1)[0]; sys.exit(1 if re.search(r'TODO|TBD|FIXME|XXX', body) else 0)"
```
