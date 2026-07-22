# Component manifest v1 policy

Brigade ships a standalone component manifest contract for Phase 1 native tools. It is separate
from `brigade.station.v1` and `station_manifest.load`.

## Unified-release components

The five-component / 25-asset contract below is the **future first stable manifest
contract**, landing after `agent-notify` publication. No stable release yet contains
this five-component / 25-asset `agent-notify` contract: the bundled compatibility
manifest on `main` lists `agent-notify` as a known component with **empty/unpublished**
assets, and the other four components have not yet shipped a stable manifest under
this contract. Stable validation
stays strict and will require every published component to carry the full
five-platform matrix with matching digests and provenance.

| Component id | Executable | Native release status |
| --- | --- | --- |
| `graphtrail` | `graphtrail` | built from the tagged Brigade commit |
| `graphtrail-mcp` | `graphtrail-mcp` | built from the tagged Brigade commit |
| `miseledger` | `miseledger` | built from the tagged Brigade commit |
| `sessionfind` | `sessionfind` | built from the tagged Brigade commit |
| `agent-notify` | `agent-notify` | bundled empty/unpublished on main; first stable manifest contract after `agent-notify` publication |

Every component records the immutable 40-character tagged Brigade commit in `component_revision`.
Every `source.repository` is `escoffier-labs/brigade`, and every `source.release_tag` is the same
immutable Brigade tag. The package template is replaced with the generated manifest before its wheel
and sdist are built. Schema version remains v1.

## Platform matrix

All Phase 1 components share one fixed support matrix:

- `linux-amd64`
- `linux-arm64`
- `darwin-amd64`
- `darwin-arm64`
- `windows-amd64`

Unsupported host platforms fail with the resolved key and the supported keys. Brigade never invents
a filename and never falls back to Cargo builds.

## Asset filenames

Go-style platform keys drive asset names:

- Linux and macOS: `<executable>-<platform-key>` with no suffix (`miseledger-linux-amd64`)
- Windows: same pattern with a `.exe` suffix (`miseledger-windows-amd64.exe`)

Each asset records `asset_name`, `byte_size`, lowercase 64-hex `sha256`, and an immutable
`download_url` whose final path segment equals `asset_name` over HTTPS with no query or fragment.
The JSON Schema constrains URL shape and Go-style asset names; Brigade runtime validation also
requires the download URL final segment to match `asset_name` exactly.

## Schema and runtime validation

`docs/component-manifest-v1.schema.json` is the structural contract for manifest authorship and
review. Unified release and update validation enforce the same platform matrix, asset naming rules,
download URL invariants, and a 40-character lowercase `component_revision` for every component.
The revision must equal the immutable target commit of the resolved Brigade release. Unknown
component ids remain a soft diagnostic; malformed known components are a hard failure.

`scripts/generate_component_manifest.py` derives the future first stable
manifest in deterministic order from the tag, commit, exact 25 filenames, byte
sizes, and SHA-256 values. That 25-asset set is the contract the first stable
manifest will publish after `agent-notify` publication; until then the bundled
compatibility manifest on `main` carries empty/unpublished `agent-notify`
assets and no stable release is claimed. The script also writes
`checksums.txt`, which contains exactly those 25 assets and
`component-manifest-v1.json`.

The release gate runs `scripts/verify_component_manifest_provenance.py` after creating the release.
It requires exactly one `escoffier-labs/brigade` tag for all five components, the complete five-platform
matrix, exactly 25 native assets plus the manifest and checksum file, matching release API digests,
the complete checksum map, matching fetched `checksums.txt` bytes, and a release-page manifest
byte-for-byte equal to the packaged manifest. This is the future first stable manifest contract;
it does not apply to the current bundled compatibility manifest, whose `agent-notify` assets remain
empty/unpublished. It uses injected fetchers in unit tests. Attestation verification is deliberately performed by `gh
attestation verify` in the release workflow rather than represented by an API boolean.

## Pre-pin beta window

Stable validation remains strict: every published component on a stable manifest must carry the full
five-platform matrix with matching digests and provenance.

Beta is the development channel for CI-green `main`. It installs the checked `main` Brigade wheel
but reuses the last verified stable component manifest for native bytes, so beta and stable cannot
install different native assets from two manifests at once.

During the pre-pin window before `agent-notify` first appears on a stable manifest, main may list
`agent-notify` as a known component with empty assets in the bundled compatibility manifest.
`brigade setup` on beta skips a component explicitly unpublished on main until a stable manifest
publishes real assets for it. Stable releases never ship with empty asset sets for a listed
component.

## User-local path invariants

Components install under the user data root, never under a repo `.brigade` directory:

- Data root defaults: Linux `XDG_DATA_HOME` or `~/.local/share`, macOS
  `~/Library/Application Support`, Windows `%LOCALAPPDATA%`
- Cache root defaults: Linux `XDG_CACHE_HOME` or `~/.cache`, macOS `~/Library/Caches`,
  Windows `%LOCALAPPDATA%`

Layout:

- `<data-root>/brigade/components/` - component metadata directory
- `<data-root>/brigade/bin/<executable>` - managed executables
- `<data-root>/brigade/installed.json` - install state (future phases)
- `<cache-root>/brigade/components/<sha256>/<asset_name>` - verified download cache

Brigade does not relocate `.graphtrail/graphtrail.db` or MiseLedger archive paths.

## Forward compatibility

- Unknown `schema_version` values are a hard failure naming received and supported versions.
- Unknown component ids listed in a v1 manifest are ignored for known-component operations and
  emit a deterministic diagnostic. The manifest is not rejected.
- Malformed known components or assets are a hard failure naming component, platform, and field.

## Current boundaries

This policy owns one release and update contract. `brigade update --channel stable` resolves the
latest immutable Brigade release, installs that exact CLI version, and runs setup against its
verified manifest. Beta uses the validated main commit while retaining the verified stable release
manifest. `brigade setup` resolves the running CLI's exact release manifest; offline automatic
setup requires a verified exact-release cache. The bundled legacy manifest is available only with
`--manifest-source standalone`.
