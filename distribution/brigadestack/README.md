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
