# Component manifest v1 policy

Brigade ships a standalone component manifest contract for Phase 1 native tools. It is separate
from `brigade.station.v1` and `station_manifest.load`.

## Phase 1 components

| Component id | Executable | Native release status |
| --- | --- | --- |
| `graphtrail` | `graphtrail` | pinned to GraphTrail commit `64fcd2f9`, assets unpublished |
| `graphtrail-mcp` | `graphtrail-mcp` | pinned to GraphTrail commit `64fcd2f9`, assets unpublished |
| `miseledger` | `miseledger` | pinned to MiseLedger v0.6.0 |
| `sessionfind` | `sessionfind` | pinned to MiseLedger v0.6.0 |

GraphTrail components record an immutable 40-character lowercase git SHA in `component_revision`.
Published components also record the GitHub release tag in `source.release_tag`. The engine revision
and release tag are independent: GraphTrail may pin a commit SHA while release assets use a semantic
tag such as `v0.1.0`. Issue #354 will update the GraphTrail commit SHA and add its release tag and
assets.

MiseLedger components record the release tag in both `component_revision` and `source.release_tag`
(`v0.6.0` today).

## Platform matrix

All Phase 1 components share one fixed support matrix:

- `linux-amd64`
- `linux-arm64`
- `darwin-amd64`
- `darwin-arm64`
- `windows-amd64`

Unsupported host platforms fail with the resolved key and the supported keys. Requesting an
unpublished component/platform pair (for example GraphTrail on any platform today) raises an
`unsupported-component-platform` diagnostic. Brigade never invents a filename and never falls
back to Cargo builds.

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
review. `brigade.component_manifest.load` enforces the same platform matrix, asset naming rules,
and download URL invariants at runtime, including exact `supported_platforms` order, full-matrix
published assets, and empty or complete unpublished assets. Unknown component ids remain a soft
diagnostic; malformed known components are a hard failure.

CI runs `scripts/verify_component_manifest_provenance.py` in a separate job from `./scripts/verify`.
That script reads the bundled manifest, calls the GitHub Releases tag API with `urllib`, compares
each published asset's name, `byte_size`, `browser_download_url`, and API `sha256:<hex>` digest,
cross-checks every manifest `sha256`/name pair against the release `checksums.txt`, and verifies
that each published component's `source.release_tag` matches the GitHub release tag embedded in
asset `download_url` values. It never compares `component_revision` to the release tag and never
downloads native binaries. Local runs work without `GITHUB_TOKEN`; CI passes `github.token` for
rate-limit headroom.

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

## Phase 1 boundaries

Issue #353 defines schema, pins, path invariants, and validation only. Downloading, unpacking,
rollback, and managed-catalog rewrites are reserved for later issues.
