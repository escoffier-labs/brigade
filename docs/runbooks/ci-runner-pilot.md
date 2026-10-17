# CI runner pilot

The pilot adds merge-group classification and one optional Linux canary. All
existing hosted jobs, display names, and 20 required statuses stay in place.
Keep the three Python versions with four shards each, eight installer variants,
all platform checks, and the 78% coverage floor. Required-name and matrix
reductions are deferred.

## Land the compatible workflow

Merge the workflow with `merge_group: checks_requested` support before enabling
merge queue. Merge groups bypass the path filter and classify all four component
flags as the string `true`: code graph, evidence ledger, notify, and Fleet Hub
container. Pushes and pull requests retain path filtering. Public pull requests
and merge-group jobs continue to use hosted runners.

Preserve current branch protections, review requirements, and conversation
resolution requirements throughout the pilot. Queue preparation does not require
an aggregate check or replacement of the existing 20 required statuses.

## Track the separate shadow aggregate

[PR #1721](https://github.com/escoffier-labs/brigade/pull/1721) owns the separate
shadow aggregate rollout. This pilot does not add another aggregate job or
helper. That PR merges current main and includes conditional Fleet Hub container
acceptance: a container may skip only after successful classification explicitly
marks it unchanged. Merge groups require container success. Check its current
diff and acceptance evidence before integration. The shadow ci-gate remains
nonrequired and excludes the optional trusted-runner-canary.

Keep any aggregate nonrequired until deliberate failure cases have been proved
on GitHub. Record run URLs and head commits showing rejection of failures,
cancellations, missing or malformed policy, and unexpected skips, including a
matrix member failure and merge-group full classification. A successful local
contract test is not live Actions proof. Retain all 20 existing required statuses
when introducing an aggregate.

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

Retain all current 20 required statuses and all existing review and conversation
requirements. Confirm a real merge-group run creates the existing required
checks and runs all four component classes on hosted runners. Record its run URL,
head SHA, classifications, check conclusions, and duration before treating queue
preparation as operational proof.

Merge queue does not need access to the trusted runner group. Keep queue jobs
hosted and preserve the group's exact-main workflow restriction. Any later
required-status change, matrix reduction, or expansion beyond one pilot runner
needs a separate rollout decision.
