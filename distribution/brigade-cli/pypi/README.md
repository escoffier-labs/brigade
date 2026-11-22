# brigade-cli

`brigade-cli` is the old PyPI name of [`brigadestack`](https://pypi.org/project/brigadestack/),
the Brigade CLI. Through 0.33 it is a full build of the same source under the old name: the same
`brigade` command, Python imports, dependencies, extras, workspace files and storage formats as
`brigadestack` at the same version. `pipx upgrade brigade-cli` stays an ordinary in-place upgrade,
and `brigade update` moves an install to `brigadestack`.

Install `brigadestack` for new setups:

```sh
pipx install brigadestack
brigadestack --version
```

The release workflow builds both distributions from the same tree, so the `brigade` package files
are identical. This name declares the `brigade`, `brigade-cli` and `brigade-hook` commands, and
`brigadestack` declares `brigade`, `brigadestack` and `brigade-hook`. Install only one of the two
globally, because both link a `brigade` command.

Requires Python 3.11 or newer.

[Homepage](https://brigadestack.com) |
[Documentation](https://brigade.tools/docs) |
[Source](https://github.com/escoffier-labs/brigade) |
[Issues](https://github.com/escoffier-labs/brigade/issues)
