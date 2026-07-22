# Brigade update channels

`brigade update` is the only supported user-global mutation path for a pipx-managed Brigade installation. It resolves immutable coordinates before changing pipx or native components, then publishes its state only after both commands succeed.

| Machine profile | Channel | Use |
| --- | --- | --- |
| Production or operator | `stable` | Default. Pins the latest published non-prerelease Brigade release. |
| Brigade development | `beta` | Intentional development-machine opt-in. Pins a full `main` SHA only when every GitHub check run is terminal and successful, neutral, or skipped. |

Run `brigade update` for the production default. `brigade update --channel beta` is not a general preview channel. A machine already owned by the other channel fails until the operator supplies `--switch-channel`; a command never transfers ownership implicitly. `--dry-run` resolves and prints the exact commands without changing pipx, managed components, or state.

## State, lock, and native components

The user-global state is `<Brigade data root>/brigade/update-state.json`, separate from component `installed.json`. Its strict schema records the selected channel, owner, exact CLI version or beta SHA, release id and tag, manifest URL and digest, and timestamp. The shared sibling lock `update.lock` covers both channels. A live owner causes a clear failure. Stale metadata is removed only after its recorded process is confirmed dead.

Stable resolves `releases/latest` once, verifies the exact `component-manifest-v1.json` release asset by GitHub digest and size, and accepts only exact `escoffier-labs/brigade` release URLs at the resolved tag. Beta pins the CLI to a checked full `main` SHA but uses the same verified stable component manifest, so beta and stable cannot install different native bytes.

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
hash backs an aggregate weekly-active count). The notice is skipped entirely
when stderr is not a TTY, when `CI` is set, or when the command failed.

Opt out completely (no notice, no network, ever):

    export BRIGADE_NO_UPDATE_CHECK=1
