# Releasing GraphTrail

GraphTrail publishes an existing Git tag through `.github/workflows/publish.yml`. The workflow is manual, runs in the `release` environment, packages the exact tagged source before requesting credentials, and uses crates.io Trusted Publishing for a short-lived token. Ordinary pull-request CI never receives publication credentials.

## One-time setup

1. Create a protected GitHub environment named `release` and limit deployments to the default branch.
2. In the `graphtrail` crate settings on crates.io, add a GitHub Trusted Publishing configuration for owner `escoffier-labs`, repository `graphtrail`, workflow `publish.yml`, and environment `release`.
3. Keep the workflow filename and environment aligned with that crates.io configuration. No long-lived registry token is needed.

## Publish a tag

Before creating a tag, set the package version in `Cargo.toml`, add the matching `CHANGELOG.md` section, run the full repository gate, and merge the release commit. Create the version tag on that exact commit, then dispatch the workflow from the default branch:

```bash
gh workflow run publish.yml --ref master -f tag=vX.Y.Z
```

The workflow refuses a tag that disagrees with the manifest or changelog, refuses a checkout not pointed to by that tag, runs `cargo package --locked`, publishes, and polls crates.io until the exact version is visible. For the existing v0.3.0 GitHub release, dispatch `tag=v0.3.0`; the workflow packages the tagged commit rather than current `master`.

## Recovery after a partial release

If package publication fails after the tag or GitHub release exists, do not move or recreate the tag. Released source identity is immutable.

- If the exact version is absent from crates.io, correct the environment or Trusted Publishing configuration and rerun the workflow with the same tag.
- If the exact version is present on crates.io, do not publish again. Confirm the registry response and treat the failed verification step as recovered.

Never reuse a version for different source. If tagged source itself is wrong, leave the tag and release as historical records, fix forward with a new patch version, and document the superseding release.
