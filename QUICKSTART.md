# Brigade Stack Quickstart

Install stable 0.30.0, then configure one repository and record a check. Brigade Stack is built by Escoffier Labs. The command reference and generated files retain the compatible `brigade` name.

## 1. Install

Choose one route. Git is required for the repository walkthrough. These commands install the published stable bridge, while `main` contains the unreleased 0.31 preview. See [update channels](docs/update-channels.md) before choosing a development build.

### npm

Use Node.js `^22.15.0 || >=23.11.0` and npm on glibc Linux x64/ARM64, macOS x64/ARM64, or Windows x64. Other targets, including musl Linux and Windows ARM64, use Python.

```bash
npm install --global brigadestack@0.30.0
brigadestack --version
brigadestack --help
```

The bridge exposes `brigadestack`. Do not assume its dependency's `brigade` command is on PATH. Expected version output is `brigade 0.30.0`, the underlying CLI's version.

Generated agent instructions and printed next-step suggestions still use `brigade`. On a bridge-only install, run them as `brigadestack` and tell your agents to do the same. With npx, use the full prefix below. Existing `brigade` installs need no change.

The first launch needs network access to download a managed Python. Keep npm optional dependencies enabled so the platform's uv binary is installed. See the [npm package notes](npm/feasibility/README.md) for cache and offline behavior.

For a one-off invocation:

```bash
npx --yes brigadestack@0.30.0 --version
```

On that route, replace `brigadestack` at the start of **every** command below with `npx --yes brigadestack@0.30.0`. It does not create a global command.

### Python with pipx

Requires Python 3.10 or newer. Install pipx using its [platform install guide](https://pipx.pypa.io/latest/installation/), then:

```bash
pipx install brigadestack==0.30.0
brigadestack --version
brigadestack --help
```

If pipx reports that its binary directory is missing from PATH, run `pipx ensurepath` and open a new terminal. The bridge exposes `brigadestack`. The dependency's console scripts are not exposed by this install.

On Linux, prefer your distribution's pipx package. The linked guide covers platform setup, including systems with an externally managed Python.

If you already use uv, the equivalent isolated tool install is:

```bash
uv tool install brigadestack==0.30.0
brigadestack --version
```

Follow uv's PATH guidance if the command is not found. See [uv tool installation](https://docs.astral.sh/uv/guides/tools/#installing-tools) for details.

### Windows PowerShell

The install commands above also use PowerShell syntax. No WSL is required. When supplying a repository path, use Windows path spelling:

```powershell
brigadestack operator quickstart --target .\my-repo --harnesses codex --dry-run
```

The stable install and walkthrough were replayed on Linux x64. macOS and Windows are supported package targets but were not replayed for this documentation refresh.

### Existing Brigade users

Keep your working install. Existing `brigade` commands, `brigade` Python imports, `.brigade/` data, and stored evidence remain compatible. You can follow the walkthrough using `brigade` wherever it says `brigadestack`. No uninstall or service migration is required to keep using it.

If you choose the new bridge, install it with one route above and check `brigadestack --version`. The bridge delegates to stable 0.30.0. Avoid mixing commands from different versions on PATH. Updating a bridge is a package-manager operation with an exact version. The npm-managed CLI refuses `brigade update`. This walkthrough does not change existing services or schedulers.

On pipx or uv bridge installs, do not use `brigadestack update`: the stable updater targets a separate `brigade-cli` pipx app rather than updating the bridge. Manage the bridge through the package manager you used, with an exact version. The [update-channel guide](docs/update-channels.md) describes the legacy CLI's updater.

## 2. Configure a repository

From a Git repository you control:

```bash
brigadestack operator quickstart --target . --harnesses codex --dry-run
brigadestack operator quickstart --target . --harnesses codex
brigadestack operator doctor --target . --profile local-operator
brigadestack work brief --target .
```

Choose the writer harnesses you use, such as `codex,claude`. Preview first and review the generated files after applying. Quickstart installs templates, local operator configuration, MCP catalog configuration, and the work-loop configuration. It does not start services or install schedulers.

Health output depends on the host. Read each warning and any `blocking_issues` before continuing. The [first 10 minutes](docs/first-10-minutes.md) explains the output and records a first check with a receipt.

For template files alone, use `brigadestack init --target . --harnesses codex`. For an existing custom setup, read [agent-assisted setup](docs/agent-assisted-setup.md) before applying changes.

## 3. Optional engines

The first verification receipt needs Git and your check command. Native code-graph and evidence engines are optional for this walkthrough. Setup downloads pinned native binaries into Brigade's data directory. Preview the plan before installing:

```bash
brigadestack setup --dry-run
brigadestack setup
```

On the stable 0.30.0 smoke host, setup returned exit 0 without output. Inspect evidence health with `brigadestack evidence status`, or see the code-graph interface with `brigadestack code --help`. Setup does not guarantee that a dependency command is available on PATH. The [technical guide](docs/technical-guide.md#managed-stations) covers the engine surfaces. No service or session setup is needed here.

## Next steps

- Follow [First 10 Minutes](docs/first-10-minutes.md) for preview, apply, health, brief, check, and receipt inspection.
- Read [the execution model](docs/execution-model.md) before adding managed workers.
- Review [the technical guide](docs/technical-guide.md) for command details. Some reference material follows `main`, so check your installed command's `--help` before using preview features.
