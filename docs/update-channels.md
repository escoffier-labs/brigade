# Brigade update channels

`stable` is the immutable release and pinner channel. `beta` is the intentional
development-machine channel for the next release's preview line. `brigade update` is the
supported user-global mutation path for a pipx-managed Brigade installation once
you want channel-managed upgrades. Deliberate initial or manual exact pins such
as `pipx install brigadestack==X.Y.Z` are valid and do not require `brigade update`
until you choose to move. It resolves immutable coordinates before changing pipx
or native components, then publishes its state only after both commands succeed.

| Machine profile | Channel | Use |
| --- | --- | --- |
| Production or operator | `stable` | Default. Pins the latest published non-prerelease Brigade release. |
| Brigade development | `beta` | Intentional development-machine opt-in. Pins the newest non-yanked `brigadestack==0.33.0.devYYYYMMDD` wheel from PyPI. |

Daily development wheels are built from `main` on a schedule and published as PyPI
prereleases with versions of the form `X.Y.Z.devYYYYMMDD` (for example
`0.28.0.dev20260907`). That delivery channel is separate from stable `vX.Y.Z` releases:
it builds only the wheel, does not create a GitHub release or native assets, and does not require
the development version to be committed into Brigade's stable version declarations. Stable tags
continue to require every declared version to match before the native release matrix can run.
Development wheels are not promoted to stable and PyPI's default resolver does not select them.

Install a chosen daily build with an exact version pin. This example uses a published build from the previous preview line.

```bash
pipx install 'brigadestack==0.32.0.dev20261120'
```

The same pin works with `pip install 'brigadestack==0.32.0.dev20261120'`. Bare
`pipx install brigadestack` or `pip install brigadestack` resolves the latest stable
release only; prerelease wheels are never selected as `latest`.

`brigadestack` is the real PyPI package. Through 0.33, `brigade-cli` is the same code under
the old name: the release build publishes both from one tree with the same version,
dependencies, extras and `brigade` package files, and only the distribution name and the
console scripts differ (`brigade`, `brigade-cli` and `brigade-hook` for `brigade-cli`).
It does not depend on `brigadestack`, so `pipx upgrade brigade-cli` is an ordinary in-place
upgrade, and the publish job refuses to upload until an installed previous `brigade-cli`
release upgrades to the built wheel and still runs. After 0.33 the last `brigade-cli`
release becomes a stub that prints the migration command.
`brigade update` installs `brigadestack` on both channels, so it moves an install off
`brigade-cli`: the last `brigade-cli` release becomes a stub, and an update must never install
it over a working CLI. Development wheels are published only as `brigadestack`, which is also
why the beta channel never installs `brigade-cli`. On a machine whose pipx app is named
`brigade-cli`, the update installs `brigadestack`, removes the `brigade-cli` app, installs
`brigadestack` again (older pipx removes launchers by name), and only then runs setup, from the
`brigadestack` pipx environment, and records the update, and it records only when the old
app is gone. The `brigadestack` environment is found by asking the configured pipx
(`pipx environment --value PIPX_LOCAL_VENVS`, which must be an absolute path), falling back to
`PIPX_HOME/venvs` and the default locations; the `PIPX_LOCAL_VENVS` environment variable is not
read, because pipx ignores it. Setup is run from, and launchers are relinked to, that environment
only when its `pipx_metadata.json` names `brigadestack` as the main package and the directory is not
a symbolic link; otherwise the update stops without running or linking anything and records nothing.

- If removing the old app fails, the update does not run setup and records nothing. It prints the
  commands to finish (`pipx uninstall brigade-cli ; pipx install --force brigadestack==<version>`)
  and exits non-zero. Run `brigade update` again afterwards.
- If the second install fails after the old app was removed, the update relinks `brigade`,
  `brigadestack` and `brigade-hook` in the pipx bin directory to the existing `brigadestack`
  environment without the network, so `brigade` keeps running, then records nothing and exits
  non-zero with the install command to retry. If that environment is gone, it says so.
- On Windows, where pipx copies launchers and cannot remove the running app, the update installs
  `brigadestack` and runs setup, but does not record the update because retiring the old app is
  pending. It exits non-zero and prints the two commands to run after it exits:
  `pipx uninstall brigade-cli`, then the same `pipx install --force brigadestack==<version>` to
  restore the `brigade` launcher. Run `brigade update` again to record it.

Run `brigade update` for the production default. `brigade update --channel beta`
selects the newest non-yanked exact `0.33.0.devYYYYMMDD` wheel for the `0.33`
preview line from PyPI release records (never from `info.version`, which stays
on the latest stable release such as `0.32.0`), ignores yanked uploads, unrelated
preview lines, unrelated prereleases, Git refs, and native release assets, and
force-reinstalls that pin into the existing user-global pipx Brigade environment
(one active CLI environment; no second installer). A machine already owned by the
other channel fails until the operator supplies `--switch-channel`; a command never
transfers ownership implicitly. Right after a version bump, beta fails closed until the first dev wheel of the new base publishes at 12:00 UTC. `--dry-run` resolves and prints the exact planned
global pipx and setup commands without changing pipx, managed components, or state.

Rollback from beta to stable is explicit:

```bash
brigade update --channel stable --switch-channel
```

That path uses the existing stable resolution, native component setup, and state
publication semantics unchanged.

## State, lock, and native components

The user-global state is `<Brigade data root>/brigade/update-state.json`, separate from component `installed.json`. Its strict schema records the selected channel, owner, exact CLI version (stable release or beta `0.33.0.devYYYYMMDD`), release id and tag, manifest URL and digest, and timestamp. The shared sibling lock `update.lock` covers both channels. A live owner causes a clear failure. Stale metadata is removed only after its recorded process is confirmed dead.

Prior beta state that recorded a full Git `main` SHA is migrated on the next
successful `brigade update --channel beta` to the selected wheel version and
wheel-backed pipx pin. Dry-run reports the planned migration actions without
writing that state.

Stable resolves `releases/latest` once, verifies the exact `component-manifest-v1.json` release asset by GitHub digest and size, and accepts only exact release URLs at the resolved tag under `escoffier-labs/brigade` or `escoffier-labs/brigadestack`, so an install keeps updating if GitHub renames the repository. It requests `escoffier-labs/brigade` first and tries the other name only when GitHub reports the repository was renamed and that name resolves to the same repository by immutable ID; every other redirected API response is still refused. Beta pins the CLI to the selected PyPI wheel but uses the same verified stable component manifest, so beta and stable cannot install different native bytes. During the pre-pin window before a new component such as `agent-notify` first ships on stable, beta may run newer Brigade Python from a daily wheel while native setup still follows the last verified stable manifest; components omitted there are skipped until stable publishes their assets.

The daily wheel channel does not change that beta/pre-pin native contract. Its build stamps only the
wheel's Python distribution and runtime version; bundled template and component pins remain the
committed stable values. Operators who use `brigade update --channel beta` receive the
selected `0.33.0.devYYYYMMDD` wheel plus native bytes from the latest verified stable manifest.

The updater runs `pipx install --force` with an exact requirement, then calls the newly installed absolute `brigade` executable with `setup --manifest <verified-cache-path>`. It does not use the prior executable for setup. This sequence is not an atomic installation transaction: pipx replacement happens before component setup. State publication is transactional, so a failed pipx install or setup leaves the prior update state untouched; rerun the same update to repair components.

## Timer migration after release

When the external development timer is migrated, replace its direct pipx script with the thin invocation `brigade update --channel beta`. Disable the old direct-pipx script at the same time. This repository does not modify live timer files.

## Switching npm packages

`brigadestack` and `@escoffier-labs/brigade` both link a global `brigade` command, so install only one of them. npm stops with `EEXIST` on the second install, which is what `npm install --global brigadestack` does while `@escoffier-labs/brigade` owns the command. `npm update --global @escoffier-labs/brigade` keeps working: the package is a compatibility package that depends on exactly `brigadestack` at the same version. To move to the new name, uninstall the old package first:

```sh
npm uninstall --global @escoffier-labs/brigade
npm install --global brigadestack
```

To go back, run `npm uninstall --global brigadestack`, then `npm install --global @escoffier-labs/brigade`.

## Compatibility window

`brigade setup` normally resolves the running CLI's exact `vX.Y.Z` release manifest, never `latest`. Before the first unified release is available, only an absent exact release or manifest can select the bundled standalone manifest. Digest failures, malformed release metadata, manifest parse failures, and component download failures never fall back. `brigade setup --manifest-source standalone` is the explicit one-unified-release compatibility path. Offline setup uses a verified exact-manifest cache when present or that explicit standalone compatibility path.

## Update notifications

After a successful interactive command, brigade may print one stderr line when
a newer release exists. The normal cache interval is 24 hours, but it is a
best-effort local schedule, not a guarantee that a request or notice occurs at
that cadence:

    A new brigade release is available: X.Y.Z (installed A.B.C). Run "brigade update".

How it learns about new releases: a detached background process may send one
HTTPS GET to `https://check.brigade.tools/v1/version`. The request has no query
parameters or body. Its User-Agent carries the Brigade version and OS name,
with the same bytes as earlier clients. A separate `X-Brigade-Channel` header carries a closed label:
`pipx`, `pip`, `uvx`, `npm`, `source`, or `plugin`. The current Worker ignores
that header. Channel recording requires the separate Worker update to ship.
See [Python CLI telemetry](telemetry.md) for channel detection and
the complete field list.
When the User-Agent exactly matches the Brigade CLI format, the Worker records a
daily version observation: ISO week and UTC day, version, first and last UTC
timestamps, request count, and an `internal` classification for configured
operator egress. It derives the source value from a salted SHA-256 hash of the
connecting address and ISO week. The hash changes weekly, and reports do not
join sources across weeks.

Analytics does not store raw IP addresses or a persistent installation ID. It
also does not store hostnames, usernames, repository or project names, command
arguments, or account identifiers. These observations are not a count of
people or installations. A weekly scheduled purge deletes rows older than 90
days.

The stderr notice is skipped when stderr is not a TTY, when `CI` is set, or
when the command failed. The background cache refresh can still run with piped
stderr so `work brief` can surface available updates. It is skipped when `CI`
or `BRIGADE_NO_UPDATE_CHECK` is set. npm installs now refresh the version cache
but suppress both the stderr notice and cached work-brief hint.

Set `DO_NOT_TRACK=1` or `BRIGADE_TELEMETRY=0` to keep version retrieval without
a counted observation. The request then uses a generic `Python-urllib`
User-Agent and omits the channel header. The Worker records nothing for this
User-Agent. `BRIGADE_TELEMETRY=off` and `false` also disable counting.

To disable this automatic update check and its cache refresh:

    export BRIGADE_NO_UPDATE_CHECK=1

This setting controls the automatic version check only. It does not disable an
explicit `brigade update` command.
