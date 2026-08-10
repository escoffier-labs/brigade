# Brigade update channels

`stable` is the immutable release and pinner channel. `beta` is the intentional
development-machine channel for the `0.27` preview line. `brigade update` is the
supported user-global mutation path for a pipx-managed Brigade installation once
you want channel-managed upgrades. Deliberate initial or manual exact pins such
as `pipx install brigade-cli==X.Y.Z` are valid and do not require `brigade update`
until you choose to move. It resolves immutable coordinates before changing pipx
or native components, then publishes its state only after both commands succeed.

| Machine profile | Channel | Use |
| --- | --- | --- |
| Production or operator | `stable` | Default. Pins the latest published non-prerelease Brigade release. |
| Brigade development | `beta` | Intentional development-machine opt-in. Pins the newest non-yanked `brigade-cli==0.27.0.devYYYYMMDD` wheel from PyPI. |

Daily development wheels are built from `main` on a schedule and published as PyPI
prereleases with versions of the form `X.Y.Z.devYYYYMMDD` (for example
`0.27.0.dev20260808`). That delivery channel is separate from stable `vX.Y.Z` releases:
it builds only the wheel, does not create a GitHub release or native assets, and does not require
the development version to be committed into Brigade's stable version declarations. Stable tags
continue to require every declared version to match before the native release matrix can run.
Development wheels are not promoted to stable and PyPI's default resolver does not select them.

Install a chosen daily build with an exact version pin:

```bash
pipx install 'brigade-cli==0.27.0.dev20260808'
```

The same pin works with `pip install 'brigade-cli==0.27.0.dev20260808'`. Bare
`pipx install brigade-cli` or `pip install brigade-cli` resolves the latest stable
release only; prerelease wheels are never selected as `latest`.

Run `brigade update` for the production default. `brigade update --channel beta`
selects the newest non-yanked exact `0.27.0.devYYYYMMDD` wheel for the `0.27`
preview line from PyPI, ignores yanked uploads, unrelated preview lines,
unrelated prereleases, Git refs, and native release assets, and force-reinstalls
that pin into the existing user-global pipx Brigade environment (one active CLI
environment; no second installer). A machine already owned by the other channel
fails until the operator supplies `--switch-channel`; a command never transfers
ownership implicitly. `--dry-run` resolves and prints the exact planned global
pipx and setup commands without changing pipx, managed components, or state.

Rollback from beta to stable is explicit:

```bash
brigade update --channel stable --switch-channel
```

That path uses the existing stable resolution, native component setup, and state
publication semantics unchanged.

## State, lock, and native components

The user-global state is `<Brigade data root>/brigade/update-state.json`, separate from component `installed.json`. Its strict schema records the selected channel, owner, exact CLI version (stable release or beta `0.27.0.devYYYYMMDD`), release id and tag, manifest URL and digest, and timestamp. The shared sibling lock `update.lock` covers both channels. A live owner causes a clear failure. Stale metadata is removed only after its recorded process is confirmed dead.

Prior beta state that recorded a full Git `main` SHA is migrated on the next
successful `brigade update --channel beta` to the selected wheel version and
wheel-backed pipx pin. Dry-run reports the planned migration actions without
writing that state.

Stable resolves `releases/latest` once, verifies the exact `component-manifest-v1.json` release asset by GitHub digest and size, and accepts only exact `escoffier-labs/brigade` release URLs at the resolved tag. Beta pins the CLI to the selected PyPI wheel but uses the same verified stable component manifest, so beta and stable cannot install different native bytes. During the pre-pin window before a new component such as `agent-notify` first ships on stable, beta may run newer Brigade Python from a daily wheel while native setup still follows the last verified stable manifest; components omitted there are skipped until stable publishes their assets.

The daily wheel channel does not change that beta/pre-pin native contract. Its build stamps only the
wheel's Python distribution and runtime version; bundled template and component pins remain the
committed stable values. Operators who use `brigade update --channel beta` receive the
selected `0.27.0.devYYYYMMDD` wheel plus native bytes from the latest verified stable manifest.

The updater runs `pipx install --force` with an exact requirement, then calls the newly installed absolute `brigade` executable with `setup --manifest <verified-cache-path>`. It does not use the prior executable for setup. This sequence is not an atomic installation transaction: pipx replacement happens before component setup. State publication is transactional, so a failed pipx install or setup leaves the prior update state untouched; rerun the same update to repair components.

## Timer migration after release

When the external development timer is migrated, replace its direct pipx script with the thin invocation `brigade update --channel beta`. Disable the old direct-pipx script at the same time. This repository does not modify live timer files.

## Compatibility window

`brigade setup` normally resolves the running CLI's exact `vX.Y.Z` release manifest, never `latest`. Before the first unified release is available, only an absent exact release or manifest can select the bundled standalone manifest. Digest failures, malformed release metadata, manifest parse failures, and component download failures never fall back. `brigade setup --manifest-source standalone` is the explicit one-unified-release compatibility path. Offline setup uses a verified exact-manifest cache when present or that explicit standalone compatibility path.

## Update notifications

After a successful command, brigade may print one stderr line (at most once
per 24 hours) when a newer release exists:

    A new brigade release is available: X.Y.Z (installed A.B.C). Run "brigade update".

How it learns about new releases: at most once per 24 hours, a detached
background process sends one HTTPS GET to
`https://check.brigade.tools/v1/version`. The request has no query
parameters, no body, and no install id. The User-Agent carries the brigade
version and OS name. Raw IPs are never stored server-side (a weekly-salted
hash backs an aggregate weekly-active count). The stderr notice is skipped
when stderr is not a TTY, when `CI` is set, or when the command failed. The
background cache refresh still runs when stderr is piped (agent harnesses) so
`work brief` can surface available updates; it is skipped when `CI` is set or
`BRIGADE_NO_UPDATE_CHECK` is set.

Opt out completely (no notice, no network, ever):

    export BRIGADE_NO_UPDATE_CHECK=1
