# BrigadeStack distributions

`brigadestack` is the real package on PyPI and on npm. On PyPI the root `pyproject.toml`
builds it, and it contains the whole CLI. On npm [`npm/feasibility`](../../npm/feasibility/README.md)
builds it: a launcher that bundles the same wheel and a managed Python runtime.

`brigade-cli` (PyPI) is the same code under the old name, and `@escoffier-labs/brigade` (npm)
is a compatibility package. Through 0.33 the release build publishes `brigade-cli` as a full
copy of the `brigadestack` build with only the distribution name and console scripts changed,
so `pipx upgrade brigade-cli` stays an ordinary in-place upgrade. The npm package depends on
exactly `brigadestack` at the same version and declares its own `brigade` command, so
`npm update --global @escoffier-labs/brigade` keeps a working `brigade`. Their sources are
[`distribution/brigade-cli/pypi`](../brigade-cli/pypi/README.md) (the long description only) and
[`distribution/escoffier-labs-brigade/npm`](../escoffier-labs-brigade/npm/README.md).

| Package | Contains | Commands |
| --- | --- | --- |
| PyPI `brigadestack` | the CLI | `brigade`, `brigadestack`, `brigade-hook` |
| PyPI `brigade-cli` | the same CLI, built from the same tree | `brigade`, `brigade-cli`, `brigade-hook` |
| npm `brigadestack` | launcher, bundled `brigadestack` wheel, uv companions | `brigade`, `brigadestack` |
| npm `@escoffier-labs/brigade` | launcher only, `brigadestack@<version>` | `brigade` |

Both PyPI distributions use the existing CLI behavior, Python imports, workspace
files, and storage formats. Install only one package per ecosystem globally: the real package
and its old-name or compatibility package both link a `brigade` command. Documentation lives at
[brigade.tools](https://brigade.tools/docs). The source repository is
[escoffier-labs/brigadestack](https://github.com/escoffier-labs/brigadestack/tree/main/distribution/brigadestack).

## Publishing the Python distributions

Both distributions publish from `publish.yml` on a stable `vX.Y.Z` tag, through three jobs,
so that no job holding the OIDC token runs code from a PyPI release:

1. `build-python-dist` (no `id-token`) builds the root project as `brigadestack`, then runs
   `scripts/stamp_brigade_cli.py` to write a temporary copy of the same tree under the
   distribution name `brigade-cli` (same version, dependencies, extras and files; the console
   scripts and long description differ; the checkout is never edited) and builds that too, into
   one `dist/` directory. It checks that exactly those four files carry the tag's version,
   records a SHA-256 manifest of `dist/` as a job output, and uploads `dist/` as the `python-dist`
   artifact, exposing its artifact ID and digest as outputs.
2. `check-cli-upgrade` (no `id-token`, no environment) downloads that artifact by ID and runs
   `scripts/check_cli_upgrade.py`: in a fresh venv it installs the newest older stable
   `brigade-cli` from PyPI, upgrades it to the built wheel and requires `brigade --version` to
   report the new version and `brigade-hook --help` to run.
3. `build-and-publish` (environment `pypi`, `id-token: write`) needs both, downloads the artifact
   by the same ID, verifies that `dist/` holds exactly the files in the build job's manifest with
   matching hashes and nothing extra, and only then uploads all four files in one
   trusted-publishing step. Job outputs cannot be changed by a later job, and a replaced artifact
   has a different ID, so code that runs during the gate cannot change what is published.

The tag check in `validate-release` covers the root version. PyPI matches a trusted publisher
on the workflow file and the environment, not the job name.

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

## Publishing the npm packages

Both npm packages publish from `npm-publish.yml`, dispatched on `main` with the reviewed
`expected_sha` and the stable `release_tag`, through npm trusted publishing (GitHub OIDC).
No npm token exists. The workflow publishes in this order:

1. The five uv companions and `brigadestack` (the `publish` job, through `npm/feasibility/publish.cjs`).
2. Native registry acceptance of `brigadestack@<version>` on five platforms.
3. `@escoffier-labs/brigade@<version>` (the `publish-compat` job), after it checks that
   `brigadestack@<version>` is on npm, that the compatibility version equals the version in
   `npm/feasibility/runtime.json`, and that it is above the current `latest`. It packs the
   tarball from `distribution/escoffier-labs-brigade/npm`, checks that it holds only
   `LICENSE`, `README.md`, `launcher.cjs` and `package.json`, installs it with the real
   dependency, checks `brigade --version`, and publishes it with provenance.

npm accepts one trusted publisher per package, and every package names this workflow and the
`npm` environment:

| npm trust field | Value |
| --- | --- |
| Package | each of `brigadestack`, `@escoffier-labs/brigade` and the five `@escoffier-labs/brigade-uv-*` companions |
| Repository | `escoffier-labs/brigade` |
| Workflow file | `npm-publish.yml` |
| Environment | `npm` |

`npm trust list <package>` shows the entry. The one-time bridge workflow that published the
0.31.0 `brigadestack` alias (`brigadestack-npm-publish.yml`) is retired, so `brigadestack`
needs its trust entry replaced. From a logged-in maintainer session:

```sh
npm trust list brigadestack
npm trust revoke brigadestack --id <id of the brigadestack-npm-publish.yml entry>
npm trust github brigadestack --repo escoffier-labs/brigade \
  --file npm-publish.yml --env npm --allow-publish
```

If only the final `latest` confirmation of `publish-compat` fails, the package may already be
published. Check `npm view @escoffier-labs/brigade dist-tags` before dispatching again; a second
run stops at the "already on npm" check once the version exists.
