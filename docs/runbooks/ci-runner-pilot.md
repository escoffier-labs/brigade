# CI runner pilot

The pilot adds merge-group classification and one optional Linux canary. All
existing hosted jobs and display names stay in place. `ci-gate` is the only
required status on `main`. Merge groups and pushes to `main` run the three
Python versions with four shards each. Pull requests run the four Python 3.12
shards, which carry the 78% coverage floor. All runs keep the eight installer
variants and all platform checks.

## Land the compatible workflow

Merge the workflow with `merge_group: checks_requested` support before enabling
merge queue. Merge groups bypass the path filter and classify all four component
flags as the string `true`: code graph, evidence ledger, notify, and Fleet Hub
container. Pushes and pull requests retain path filtering. Public pull requests
and merge-group jobs continue to use hosted runners.

Preserve current branch protections, review requirements, and conversation
resolution requirements throughout the pilot. The queue was prepared while the
20 per-job statuses were still required. `ci-gate` replaced them afterward.

## Required aggregate

[PR #1721](https://github.com/escoffier-labs/brigade/pull/1721) added `ci-gate`.
It always runs and requires success from the 9 jobs behind the former 20
required statuses plus their 4 dependencies. A missing or extra dependency
fails closed. Fleet Hub container may skip only after successful classification
marks it unchanged, and never in a merge group. The gate excludes the optional
trusted-runner-canary.

On 2026-10-21 `ci-gate` replaced the 20 per-job required statuses. These
GitHub runs proved each acceptance case first:

| Case | Event | Run | Head | ci-gate |
| --- | --- | --- | --- | --- |
| Clean head | pull_request | [39062560230](https://github.com/escoffier-labs/brigade/actions/runs/39062560230) | `13ad96985` | success |
| Unchanged-container skip | pull_request | [39062629527](https://github.com/escoffier-labs/brigade/actions/runs/39062629527) | `fd42bb080` | success |
| Python shard failure (matrix member) | pull_request | [39065646753](https://github.com/escoffier-labs/brigade/actions/runs/39065646753) | `3e571f7c6` | failure |
| Lint failure | pull_request | [39062595036](https://github.com/escoffier-labs/brigade/actions/runs/39062595036) | `7ed4eab3d` | failure |
| Cancellation | pull_request | [39062567035](https://github.com/escoffier-labs/brigade/actions/runs/39062567035) | `4e3e06693` | failure |
| Unexpected skip (`repo-metadata`) | pull_request | [39062613193](https://github.com/escoffier-labs/brigade/actions/runs/39062613193) | `25c91dcad` | failure |
| Missing policy dependency | pull_request | [39683853181](https://github.com/escoffier-labs/brigade/actions/runs/39683853181) | `de8753038` | failure |
| Full classification | merge_group | [39651345036](https://github.com/escoffier-labs/brigade/actions/runs/39651345036) | `65bb00fb6` | success |
| Shard failure | merge_group | [39539973591](https://github.com/escoffier-labs/brigade/actions/runs/39539973591) | `e6b6ae3ed` | failure |

Before the switch, `ci-gate` agreed with the 20 statuses on all 54 open and
recently merged pull requests that had both results. To step back, restore the
20 contexts: the expanded check names of the nine protected jobs in the
`ci-gate` policy set, required from the GitHub Actions app.

A skipped matrix job reports one check with the unexpanded name, such as
`Feature configuration (${{ matrix.name }})`. Never skip a matrix job with a
job-level `if:` when its expanded names are required. Shrink the matrix with an
expression instead, as the `test-shards` Python axis does.

## Activate one canary

Before activation, verify the server-side `brigade-trusted` runner group accepts
only `.github/workflows/ci.yml@refs/heads/main` in this repository. Keep that exact
workflow/ref restriction. Do not add pull-request refs or merge-queue refs.
Prepare one ephemeral Ubuntu runner with system Python 3.10 or newer and Git
available. During provisioning, verify the interpreter before taking the clean
snapshot or enabling the canary:

```sh
python3 -c 'import sys; print(sys.version); sys.exit(0 if sys.version_info >= (3, 10) else "Python 3.10+ required")'
```

Source execution bypasses the package installer, so the job cannot rely on
`requires-python` to reject an unsupported interpreter. Keep the canary disabled
if this check fails.

With the compatible workflow on `main`, set repository variable
`BRIGADE_SELF_HOSTED` to exactly `on` to enable the canary for subsequent pushes
to `refs/heads/main`. Unset or off leaves it disabled. The job also requires a
`push` event, so same-repository pull requests and merge groups cannot qualify.
It uses static group `brigade-trusted` and labels `self-hosted`, `linux`, `x64`,
has a ten-minute timeout, and receives only `contents: read`. Checkout does not
persist credentials.

The canary runs the source CLI with system Python and an absolute checkout `src`
path. It checks the CLI version, initializes a temporary Git workspace with the
Claude and Codex harnesses, and runs doctor there. It installs no runtime or
helper and does not target the operator workspace. Keep the canary nonrequired
and outside aggregate dependencies. There is no automatic hosted fallback.

Record one completed canary job: workflow run URL, head SHA, job URL, assigned
runner, duration, version output, and doctor result. Record controller evidence
that the one-job runner was removed and its clean replacement was registered.
Also record a controller rollback exercise and the resulting replacement state.
Keep infrastructure identifiers and controller evidence in the private operator
record, outside this repository.

## Disable and roll back

Set `BRIGADE_SELF_HOSTED` off or unset it to prevent future canary allocations.
Changing the variable does not cancel jobs already running or queued. Explicitly
cancel those jobs and inspect their state before any rerun. Recheck eligibility
and routing for the chosen rerun. A job already queued for the trusted group
does not move to a hosted runner automatically.

Use the controller's approved rollback procedure to replace or remove the pilot
runner, then record the observed runner and replacement state privately. If the
workflow needs a hosted replacement, make that routing change explicitly and
review it. Leave required hosted checks and branch protections intact.

## Configure merge queue later

Only after the compatible `merge_group` workflow exists on `main`, configure the
queue for the exact `main` branch with these settings:

- Build concurrency: 1.
- Minimum merge group size: 1.
- Maximum merge group size: 3.
- Merge only non-failing pull requests.
- Status-check timeout: 90 minutes.

Retain the required `ci-gate` status (see [Required aggregate](#required-aggregate))
and all existing review and conversation requirements. Confirm a real merge-group run creates the existing required
checks and runs all four component classes on hosted runners. Record its run URL,
head SHA, classifications, check conclusions, and duration before treating queue
preparation as operational proof.

Merge queue does not need access to the trusted runner group. Keep queue jobs
hosted and preserve the group's exact-main workflow restriction. Any later
required-status change, matrix reduction, or expansion beyond one pilot runner
needs a separate rollout decision.
