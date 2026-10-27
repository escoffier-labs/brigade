# Brigade Stack

`brigadestack==0.30.0` installs stable `brigade-cli==0.30.0` and adds the
`brigadestack` command. Existing packages and the `brigade` command remain supported.
This bridge contains package metadata and points directly to the dependency's CLI
entry point. Python imports remain under `brigade`.

Requires Python 3.10 or newer. Install in a virtual environment:

```sh
python -m venv .venv
# Linux or macOS
.venv/bin/python -m pip install brigadestack==0.30.0
.venv/bin/brigadestack --version
.venv/bin/brigadestack --help
```

On Windows, use `.venv\Scripts\python.exe` and
`.venv\Scripts\brigadestack.exe` instead. With pipx, use
`pipx install brigadestack==0.30.0`.

The CLI reports Brigade 0.30.0 and keeps the same workspace and storage identities.
The original `brigade` command is supplied by the dependency.

[Homepage](https://brigade.tools) |
[Documentation](https://brigade.tools/docs) |
[Source](https://github.com/escoffier-labs/brigade/tree/main/distribution/brigadestack/pypi) |
[Issues](https://github.com/escoffier-labs/brigade/issues)
