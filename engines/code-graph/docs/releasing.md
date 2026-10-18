# Releasing GraphTrail

GraphTrail ships as an engine in Brigade's unified release. The root `.github/workflows/publish.yml` builds `graphtrail` and `graphtrail-mcp` from `engines/code-graph`, includes them in the release assets and verified engine manifest, and publishes the Brigade package. The nested workflow in this engine directory is a historical standalone workflow and does not run as a Brigade workflow.

## One-time setup

Use Brigade's root release configuration. The standalone GraphTrail repository is archived, and crates.io is frozen at 0.4.0 with no further releases, per the [compatibility policy](../../../docs/phase-4a-compatibility-and-archive.md#graphtrail-cratesio-policy).

## Publish a tag

Run the root full verification gate and required CI, keep Brigade version fields synchronized, and tag the approved release commit as `vX.Y.Z`. The root [publish workflow](../../../.github/workflows/publish.yml) starts on exact stable tags and validates the version fields before building the engines. Use the [release-readiness guide](../../../docs/release-readiness.md) for the local evidence gate. Do not dispatch the archived repository's standalone crates.io publisher.

## Recovery after a partial release

Inspect the root publish workflow's failed job and release assets before rerunning it. Do not move or recreate an existing release tag or reuse a version for different source.

The archived standalone workflow used crates.io Trusted Publishing. For an existing standalone release, do not move or recreate the tag. If the exact version is absent from crates.io, that standalone publication did not complete. If the exact version is present, do not publish it again. These historical rules do not authorize a new standalone release.
