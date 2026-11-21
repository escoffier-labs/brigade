# brigade-cli

`brigade-cli` is the compatibility name for [`brigadestack`](https://pypi.org/project/brigadestack/),
the Brigade CLI. `brigade-cli==0.32.0` depends on exactly `brigadestack==0.32.0` and installs the
same `brigade` command. Existing installs keep working, and `pipx upgrade brigade-cli` and
`brigade update` keep upgrading under this name.

Install `brigadestack` for new setups:

```sh
pipx install brigadestack
brigadestack --version
```

This package contains metadata and console scripts only. It declares `brigade`, `brigade-cli`
and `brigade-hook` because pipx exposes only the installed package's own scripts. Python imports
remain under `brigade`, and the workspace files and storage formats do not change.

Requires Python 3.11 or newer.

[Homepage](https://brigade.tools) |
[Documentation](https://brigade.tools/docs) |
[Source](https://github.com/escoffier-labs/brigade/tree/main/distribution/brigade-cli/pypi) |
[Issues](https://github.com/escoffier-labs/brigade/issues)
