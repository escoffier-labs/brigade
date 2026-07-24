# Phase 4A Compatibility Policy and Phase 4B Archive Checklist

> **Status: The compatibility window is compressed by maintainer decision
> (2026-07-21). Phase 4B archival execution is authorized once the remaining
> checklist items below are complete.**

Phase 4A and Phase 4B are this policy's execution split within RFC Phase 4.
This policy records the compatibility window for the unified Brigade release.
v0.25.0 is live. The window began at T0 on 2026-07-21T00:50:15Z.

## Maintainer decision: compressed window (2026-07-21)

The original policy set a dual gate (v0.27.0 and a 90-day calendar gate at
2026-10-19). That gate protected external consumers of the standalone
mirrors. The maintainer reviewed the exposure and recorded this decision:

- Both mirrors are the maintainer's own MIT-licensed code under the same
  owner (escoffier-labs). There are no external users to protect: zero
  forks, zero known reverse dependencies, and the GitHub clone counts trace
  to the maintainer's own fleet and CI.
- No legal or dependency blocker exists.

The dual gate is therefore waived. Archival may execute as soon as the
remaining Phase 4B checklist items are complete. The following original
requirements are explicitly **waived by the same decision**:

- The 90-day calendar gate and the v0.27.0 version gate.
- The final `graphtrail` 0.5.0 crates.io compatibility release. The
  published crate versions stay unyanked for reproducibility. The crate is
  marked deprecated and maintenance-frozen with no further releases.
- The Brigade-owned session list/search facade as a pre-archival blocker for
  `sessionfind`. The `sessionfind` shim ships in the managed engine set and
  keeps working after the mirrors are archived. The facade remains ordinary
  roadmap work, not an archive gate.

Non-negotiables that survive the compression unchanged:

- Existing databases, data paths, and schemas are non-destructive
  invariants. No action relocates, deletes, or destructively migrates them.
- The `master` branches of the mirrors are never rewritten or force-pushed.
  Migration notices and any security fixes are ordinary commits on top.
- Archiving a mirror freezes it read-only on GitHub. It deletes nothing.

## Scope and references

This policy implements the release and repository-retirement boundaries from
[RFC #352](https://github.com/escoffier-labs/brigade/issues/352), the Phase 4
release work in [#364](https://github.com/escoffier-labs/brigade/issues/364),
and [#365](https://github.com/escoffier-labs/brigade/issues/365). The public
release record is the [Brigade releases page](https://github.com/escoffier-labs/brigade/releases).

The Phase 2 [import source-of-truth policy
context](https://github.com/escoffier-labs/brigade/issues/352#issuecomment-5018303485)
documents the interim import-history, commit-map, and authorship context.
Those records remain anchored to the standalone mirrors. The comment neither
sets nor governs a binding boundary. The governing Phase 3 [no-rewrite
amendment](https://github.com/escoffier-labs/brigade/issues/352#issuecomment-5019220456)
is the authority that prohibits rewriting or force-pushing their `master`
branches. The mirrors are
[escoffier-labs/graphtrail](https://github.com/escoffier-labs/graphtrail) and
[escoffier-labs/miseledger](https://github.com/escoffier-labs/miseledger).

Agent Pantry is out of scope. agent-notify/#366 is out of scope. Neither
repository is part of this compatibility policy, source-history migration, or
future archive checklist.

## Release record

| Record | Value |
| --- | --- |
| First unified compatibility-bearing minor | v0.25.0 |
| Published at | 2026-07-21T00:50:15Z |
| T0 | v0.25.0 published at 2026-07-21T00:50:15Z |
| Original dual gate | v0.27.0 + 2026-10-19 calendar gate (waived 2026-07-21) |
| Current status | Window compressed. Phase 4B authorized pending checklist completion |

## Compatibility contract

The managed engine set installed by `brigade setup` continues to ship these
executables:

- `graphtrail`
- `graphtrail-mcp`
- `miseledger`
- `sessionfind`

Archiving the mirrors does not remove or change any of them. They are the
engines, distributed and pinned by Brigade. The `brigade search sync`,
`brigade search context`, and `brigade search impact` executable aliases for
`brigade code sync`, `brigade code context`, and `brigade code impact` also
remain. Standalone manifest-source and independent-install compatibility
paths retain their separately documented
[one-release fallback](update-channels.md).

Current compatibility-equivalent engine commands for `sessionfind`:

| Compatibility invocation | Current compatibility-equivalent engine command |
| --- | --- |
| `sessionfind list` | `miseledger sessions list` |
| `sessionfind search <query>` | `miseledger sessions search <query>` |
| `sessionfind <query>` | `miseledger sessions search <query>` |

`brigade setup` is the distribution replacement for every standalone install
path. MCP clients retain the `graphtrail-mcp` protocol but configure the
managed absolute path (`brigade setup` records it in `installed.json`, and
Brigade's config generators emit it).

Existing databases, data paths, and schemas are non-destructive invariants.
No compatibility action may relocate, delete, or migrate an existing database
or data path destructively.

## Frozen standalone mirrors

The `master` branches of `escoffier-labs/graphtrail` and
`escoffier-labs/miseledger` must not be rewritten or force-pushed. Their import
commit maps anchor the source-history migration.

Migration notices are ordinary commits on top of `master`. Security fixes may
also be ordinary commits while the mirrors remain unarchived. No feature work
returns to either mirror.

## GraphTrail crates.io policy

Per the 2026-07-21 maintainer decision, no further crates.io releases ship.
Leave every published crate version unyanked for reproducibility. Mark the
crate deprecated and maintenance-frozen, and remove current `cargo install`
guidance from documentation.

## Phase 4B checklist (compressed)

- [x] Maintainer decision recorded waiving the dual gate (this document, 2026-07-21).
- [x] Audit and migrate Brigade-generated MCP configs, including `src/brigade/cursor_user_cmd.py`, from PATH-based `graphtrail-mcp` and `miseledger` commands to managed absolute paths (PR #419).
- [x] Migrate operator MCP client configuration to the Brigade-managed absolute path installed by `brigade setup` (operator machine migrated 2026-07-21: codex, Cursor, OpenClaw, and Claude configs plus the capped MCP wrapper now use the managed set).
- [x] Confirm migration notices as ordinary commits on both mirrors (graphtrail PR #44 and miseledger PR #44, merged 2026-07-21).
- [x] Verify that neither standalone `master` branch was rewritten or force-pushed (ancestry-checked against the pre-notice heads on 2026-07-21).
- [x] Update product and documentation links to the Brigade release path.
- [x] Archive `escoffier-labs/graphtrail` (archived 2026-07-21).
- [x] Archive `escoffier-labs/miseledger` (archived 2026-07-21).

## Stop and rollback conditions

Stop before archival if a data-path or schema invariant is at risk or a
migration notice is missing. If a pre-archive compatibility problem appears,
keep the mirrors active and repair it with an ordinary commit. Do not rewrite
standalone `master`, do not force-push. After archival, a mirror can be
unarchived from GitHub settings at any time if a repair is ever needed.
