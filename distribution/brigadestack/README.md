# Brigade Stack bridge packages

Brigade Stack 0.30.0 adds the `brigadestack` command to stable Brigade 0.30.0.
Choose either the Python or npm package. Existing `brigade-cli`,
`@escoffier-labs/brigade`, and the `brigade` command remain supported.

| Package | Exact dependency | Command added |
| --- | --- | --- |
| PyPI `brigadestack==0.30.0` | `brigade-cli==0.30.0` | `brigadestack` |
| npm `brigadestack@0.30.0` | `@escoffier-labs/brigade@0.30.0` | `brigadestack` |

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
0.30.0 wheel and sdist, and installs the wheel with the actual stable
`brigade-cli==0.30.0` dependency to exercise `brigadestack --version` and `--help`.
It passes those two checked artifacts to a separate publish job in the `pypi`
environment. Only that job can request an OIDC token. The root development
package and native release assets are outside this workflow's build path.

Publication runs are serialized. Before dispatch or retry, inspect PyPI for
`brigadestack==0.30.0` and stop if it already exists. Duplicate uploads fail.
After a successful run, the controller must download and install the published
artifacts in a fresh environment and check identity, hashes and CLI behavior.
The npm bridge has a separate manual publication step owned by the controller.
