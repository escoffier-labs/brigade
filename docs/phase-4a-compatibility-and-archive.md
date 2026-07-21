# Phase 4A Compatibility Policy and Phase 4B Archive Checklist

> **Status: Phase 4A policy is active. Phase 4B archival execution is not authorized.**

Phase 4A and Phase 4B are this policy's execution split within RFC Phase 4.
This policy records the active compatibility window for the unified Brigade
release. v0.25.0 is live, so the window began at T0 on
2026-07-21T00:50:15Z. It does not authorize, execute, or claim completion of
any archival step. Phase 4B requires the gates and checklist below.

## Scope and references

This policy implements the release and repository-retirement boundaries from
[RFC #352](https://github.com/escoffier-labs/brigade/issues/352), the Phase 4
release work in [#364](https://github.com/escoffier-labs/brigade/issues/364),
and [#365](https://github.com/escoffier-labs/brigade/issues/365). The public
release record is the [Brigade releases page](https://github.com/escoffier-labs/brigade/releases).

The Phase 2 [import source-of-truth policy
context](https://github.com/escoffier-labs/brigade/issues/352#issuecomment-5018303485)
documents the interim import-history, commit-map, and authorship context.
Those records remain anchored to the standalone mirrors; the comment neither
sets nor governs a binding boundary. The governing Phase 3 [no-rewrite
amendment](https://github.com/escoffier-labs/brigade/issues/352#issuecomment-5019220456)
is the authority that prohibits rewriting or force-pushing their `master`
branches. The mirrors are
[escoffier-labs/graphtrail](https://github.com/escoffier-labs/graphtrail) and
[escoffier-labs/miseledger](https://github.com/escoffier-labs/miseledger).

Agent Pantry is out of scope. agent-notify/#366 is out of scope. Neither
repository is part of this compatibility policy, source-history migration, or
future archive checklist.

## Release record and dual gate

| Record | Value |
| --- | --- |
| First unified compatibility-bearing minor | v0.25.0 |
| Published at | 2026-07-21T00:50:15Z |
| UTC calendar gate | 2026-10-19 |
| Exact 90-day timestamp | 2026-10-19T00:50:15Z |
| Second compatibility-bearing minor | v0.26.0 |
| Earliest removal or archival release | v0.27.0, after the calendar gate |
| T0 | v0.25.0 published at 2026-07-21T00:50:15Z |
| Current status | The compatibility window is active. Phase 4B archival execution remains unauthorized |

v0.25.0 is the first compatibility-bearing unified minor, and v0.26.0 is the
second. T0 is the live v0.25.0 publication timestamp, 2026-07-21T00:50:15Z.
The UTC calendar gate date is 2026-10-19. The gate does not open until
2026-10-19T00:50:15Z.

Removal or archival may first ship in v0.27.0 after the calendar gate. Both the
version gate and the calendar gate must be satisfied.

If v0.27.0 ships before the calendar gate, wait for the calendar gate. If the
calendar gate arrives before v0.27.0, wait for the version gate.
A release date alone does not authorize removal, and a version number alone
does not authorize archival.

## Compatibility contract

The compatibility window covers these executable shims:

- `graphtrail`
- `graphtrail-mcp`
- `miseledger`
- `sessionfind`

It also covers the `brigade search sync`, `brigade search context`, and
`brigade search impact` executable aliases for `brigade code sync`,
`brigade code context`, and `brigade code impact`. Standalone manifest-source
and independent-install compatibility paths retain their separately documented
[one-release fallback](update-channels.md).

At T0, the governed operation inventory for each v0.25.0 shim is its public
subcommands, help behavior, and JSON contracts. For every shipped non-meta
operation, Phase 4B requires either a behavior-equivalent Brigade-owned path
or an explicit maintainer decision to retain the shim. An operation without a
disposition blocks removal.

`--help`, `--version`, and `version` are compatibility probes, not migration
workflows that require replacement commands. They must remain functional for
the compatibility window. This includes `sessionfind version`; its probe does
not imply that it needs a user-workflow replacement command.

| Compatibility invocation | Current compatibility-equivalent engine command |
| --- | --- |
| `sessionfind list` | `miseledger sessions list` |
| `sessionfind search <query>` | `miseledger sessions search <query>` |
| `sessionfind <query>` | `miseledger sessions search <query>` |

The `miseledger sessions list` and `miseledger sessions search` entries are
current compatibility-equivalent engine commands, not final Brigade-owned
replacements. Because `miseledger` is in the same shim cohort as `sessionfind`,
`sessionfind` is not removal-ready until a Brigade-owned session list/search
facade exists, tests prove equivalent filters and JSON behavior, and its
deprecation message names that Brigade command. A missing Brigade-owned session
facade blocks Phase 4B.

`brigade setup` is the distribution replacement command for `graphtrail-mcp`.
It installs the Brigade-managed `graphtrail-mcp` binary. MCP clients retain the
`graphtrail-mcp` protocol but must move their configuration to the managed
absolute path. The `graphtrail-mcp` deprecation message must name `brigade
setup`, the managed-path configuration change, and the earliest removal
condition: v0.27.0 after the actual T0 + 90-day calendar gate, with both the
version gate and the calendar gate satisfied.

For each shipped non-meta operation, Phase 4B requires either a
behavior-equivalent Brigade-owned path or an explicit maintainer decision to
retain the shim. Any operation without that disposition blocks removal.

Existing databases, data paths, and schemas are non-destructive invariants.
No compatibility action may relocate, delete, or migrate an existing database
or data path destructively.

Each deprecation message must name the replacement command and state the
earliest removal condition: v0.27.0 after the actual T0 + 90-day calendar gate,
with both the version gate and the calendar gate satisfied. For code-graph
invocations, name the applicable `brigade code sync`, `brigade code context`,
or `brigade code impact` command. For MiseLedger-backed evidence invocations,
name the applicable `brigade evidence crawl`, `brigade evidence search`, or
`brigade evidence doctor` command. For sessionfind invocations, use the mappings
above. There are no silent removals.

## Frozen standalone mirrors

The `master` branches of `escoffier-labs/graphtrail` and
`escoffier-labs/miseledger` must not be rewritten or force-pushed. Their import
commit maps anchor the source-history migration.

Migration notices are ordinary commits on top of `master`. Security fixes may
also be ordinary commits during the compatibility window. No feature work
returns to either mirror.

## GraphTrail crates.io policy

Publish graphtrail 0.5.0 from the Brigade monorepo as the final compatibility
minor. It must retain working legacy binaries and features and include migration
warnings. Patch releases during the compatibility window are limited to security
and release-integrity fixes.

After both gates are satisfied, leave every published crate version unyanked for
reproducibility. Mark the crate deprecated and maintenance-frozen, remove current
`cargo install` guidance, and publish no feature releases.

## Future Phase 4B checklist

All unchecked items below describe future work. They are not authorization to
perform it while Phase 4A is active.

- [ ] Confirm both the version gate and the calendar gate.
- [ ] Verify every shim and Brigade search alias, including its replacement command and earliest removal message.
- [ ] Capture the T0 shim operation inventory and disposition every shipped non-meta operation with a behavior-equivalent Brigade-owned path or an explicit maintainer decision to retain the shim.
- [ ] Build and verify the Brigade-owned session list/search facade, including equivalent filters and JSON behavior, then name it in the `sessionfind` deprecation message.
- [ ] Migrate `graphtrail-mcp` MCP client configuration to the Brigade-managed absolute path installed by `brigade setup`, and verify its deprecation message.
- [ ] Audit and migrate Brigade-generated MCP configs, including `src/brigade/cursor_user_cmd.py`, from PATH-based `graphtrail-mcp` and `miseledger` commands to managed absolute paths.
- [ ] Audit, transfer, or close remaining issues with links to [#364](https://github.com/escoffier-labs/brigade/issues/364) and [#365](https://github.com/escoffier-labs/brigade/issues/365).
- [ ] Publish and verify the final `graphtrail` 0.5.0 compatibility release.
- [ ] Confirm migration notices as ordinary commits on both mirrors.
- [ ] Verify that neither standalone `master` branch was rewritten or force-pushed.
- [ ] Update product and documentation links to the Brigade release path.
- [ ] Capture final release and acceptance evidence.
- [ ] Obtain maintainer approval for Phase 4B execution.
- [ ] Archive `escoffier-labs/graphtrail` (prohibited during Phase 4A).
- [ ] Archive `escoffier-labs/miseledger` (prohibited during Phase 4A).

## Stop and rollback conditions

Stop Phase 4B before archival if either dual gate is unmet, a shim or alias
lacks its required message, a data-path or schema invariant is at risk, the
final crate release is not verified, a migration notice is missing, or
maintainer approval is absent.

If a pre-archive compatibility problem appears, keep the mirrors active and
repair it with an ordinary commit or a security/release-integrity patch as
applicable. Do not rewrite standalone `master`, do not force-push, and do not
archive either repository until every Phase 4B checklist item is complete.
