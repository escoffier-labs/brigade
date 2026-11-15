# Brigade Stack bridge packages

Brigade Stack 0.31.0 adds the `brigadestack` command to stable Brigade 0.31.0.
Choose either the Python or npm package. Existing `brigade-cli`,
`@escoffier-labs/brigade`, and the `brigade` command remain supported.

| Package | Exact dependency | Command added |
| --- | --- | --- |
| PyPI `brigadestack==0.31.0` | `brigade-cli==0.31.0` | `brigadestack` |
| npm `brigadestack@0.31.0` | `@escoffier-labs/brigade@0.31.0` | `brigadestack` |

The Python distribution contains metadata and a console entry point to the
dependency's `brigade.cli:process_main`. The npm distribution runs the dependency's
launcher with Node. Both use the existing CLI behavior, Python imports, workspace
files, and storage formats.

See the [Python instructions](pypi/README.md) or [npm instructions](npm/README.md).
Documentation lives at [brigade.tools](https://brigade.tools/docs).
The source repository is [escoffier-labs/brigade](https://github.com/escoffier-labs/brigade/tree/main/distribution/brigadestack).

## Publishing the Python bridge

Configure a pending GitHub trusted publisher in the existing PyPI account before
the first upload, using these exact fields:

| PyPI publisher field | Value |
| --- | --- |
| Project name | `brigadestack` |
| Owner | `escoffier-labs` |
| Repository name | `brigade` |
| Workflow name | `brigadestack-publish.yml` |
| Environment name | `pypi` |

A pending publisher configuration alone does not reserve the project name.
The first successful upload creates the project. Complete account configuration
through the authorized browser session. The workflow uses GitHub OIDC trusted
publishing and needs no PyPI API token or local upload configuration.

After the controller reviews and merges the bridge source and workflow, select
**Brigade Stack PyPI publication** in GitHub Actions and run it on `main`.
The workflow accepts no inputs and runs only in `escoffier-labs/brigade` on
`refs/heads/main`. Review the selected source commit before dispatch.

The unprivileged job builds only `distribution/brigadestack/pypi`, checks the
0.31.0 wheel and sdist, and installs the wheel with the actual stable
`brigade-cli==0.31.0` dependency to exercise `brigadestack --version` and `--help`.
It passes those two checked artifacts to a separate publish job in the `pypi`
environment. Only that job can request an OIDC token. The root development
package and native release assets are outside this workflow's build path.

Publication runs are serialized. Before dispatch or retry, inspect PyPI for
`brigadestack==0.31.0` and stop if it already exists. Duplicate uploads fail.
After a successful run, the controller must download and install the published
artifacts in a fresh environment and check identity, hashes and CLI behavior.

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
workflow edit. It stops when the dependency version is not on npm or the bridge
version already exists. The build job runs the launcher tests, checks that the
tarball holds only `LICENSE`, `README.md`, `launcher.cjs` and `package.json`,
installs it with the real dependency and checks `brigadestack --version` and
`--help`. Only the publish job, in the `npm` environment, can request an OIDC
token. It publishes that checked tarball with provenance and waits until
`latest` points at the new version.
