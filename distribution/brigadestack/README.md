# Brigade Stack distributions

`brigadestack` is the real PyPI package: the root `pyproject.toml` builds it, and it
contains the whole CLI. `brigade-cli` is the compatibility package on PyPI. Its source
is [`distribution/brigade-cli/pypi`](../brigade-cli/pypi/README.md). It depends on
exactly `brigadestack==<version>` and declares the `brigade`, `brigade-cli` and
`brigade-hook` commands so `pipx upgrade brigade-cli` keeps a working `brigade`.
The npm bridge in this directory still pins stable 0.31.0 until the npm package flips.

| Package | Contains | Commands |
| --- | --- | --- |
| PyPI `brigadestack` | the CLI | `brigade`, `brigadestack`, `brigade-hook` |
| PyPI `brigade-cli` | metadata only, `brigadestack==<version>` | `brigade`, `brigade-cli`, `brigade-hook` |
| npm `brigadestack@0.31.0` | launcher, `@escoffier-labs/brigade@0.31.0` | `brigadestack` |

Both Python distributions use the existing CLI behavior, Python imports, workspace
files, and storage formats. See the [npm instructions](npm/README.md).
Documentation lives at [brigade.tools](https://brigade.tools/docs).
The source repository is [escoffier-labs/brigade](https://github.com/escoffier-labs/brigade/tree/main/distribution/brigadestack).

## Publishing the Python distributions

Both distributions publish from `publish.yml` on a stable `vX.Y.Z` tag. The
`build-and-publish` job builds the root project as `brigadestack` and
`distribution/brigade-cli/pypi` as `brigade-cli` into one `dist/` directory, checks
that exactly those four files carry the tag's version, and uploads them in one
trusted-publishing step in the `pypi` environment. `scripts/version_sync.py` keeps
the compatibility package's version and its `brigadestack==<version>` pin equal to
the root version, and the tag check in `validate-release` covers it too.

Each PyPI project needs a trusted publisher for this workflow. Configure these exact
fields on `brigadestack` and on `brigade-cli`:

| PyPI publisher field | Value |
| --- | --- |
| Owner | `escoffier-labs` |
| Repository name | `brigade` |
| Workflow name | `publish.yml` |
| Environment name | `pypi` |

`brigadestack` also needs the same publisher for `publish-dev.yml`, which uploads the
daily `X.Y.Z.devYYYYMMDD` wheels of the root project. The one-time workflow that
published the 0.31.0 `brigadestack` alias (`brigadestack-publish.yml`) is retired.

## Publishing the npm bridge

The npm bridge publishes through npm trusted publishing (GitHub OIDC) and needs
no npm token. Its npm trust entry uses these exact fields:

| npm trust field | Value |
| --- | --- |
| Package | `brigadestack` |
| Repository | `escoffier-labs/brigade` |
| Workflow file | `brigadestack-npm-publish.yml` |
| Environment | `npm` |

`npm trust list brigadestack` shows the entry. Recreate it from a logged-in
maintainer session with:

```sh
npm trust github brigadestack --repo escoffier-labs/brigade \
  --file brigadestack-npm-publish.yml --env npm --allow-publish
```

After the stable `@escoffier-labs/brigade` release is on npm and the bridge
`package.json` pins that same exact version, select **Brigade Stack npm
publication** in GitHub Actions and run it on `main`. The workflow reads the
version from `distribution/brigadestack/npm/package.json`, so a release needs no
workflow edit. It stops before any registry write when the dependency version is
not on npm, the bridge version already exists, or the bridge version is not above
the current `latest`, since publishing would otherwise move `latest` backwards.
The build job runs the launcher tests, checks that the
tarball holds only `LICENSE`, `README.md`, `launcher.cjs` and `package.json`,
installs it with the real dependency and checks `brigadestack --version` and
`--help`. Only the publish job, in the `npm` environment, can request an OIDC
token. It publishes that checked tarball with provenance and waits up to five
minutes until `latest` points at the new version.

If only that final confirmation fails, the package may already be published.
Check `npm view brigadestack dist-tags` before dispatching again; a second run
stops at the "already on npm" check once the version exists.

This workflow publishes the `main` commit current at
dispatch and takes no reviewed-SHA input, unlike `npm-publish.yml`. The bridge is
four files with an exact dependency, so review the `main` head before dispatch.
