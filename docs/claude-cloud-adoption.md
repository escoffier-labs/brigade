# Local Claude subscription cloud adoption

This local slice of #1600 adds explicit observation to the existing cloud
registry. It depends on the reviewed Claude identity foundation. The supported
provider surfaces and their retrieval date are recorded in
[Claude cloud capabilities](claude-cloud-capabilities.md).

## Adopt an explicit session

Supply a subscription cloud session identity and its repository:

```bash
brigade run cloud adopt --provider claude-cloud \
  --session-id cse_fixture --repo fixture-owner/fixture-repo \
  --branch claude/fixture --commit aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
  --pr 12 --target ./workspace --json
```

`register` supports the same fields and requires a label. Both commands accept
`--task-id` as the explicit identity, or `--session-id` with `--repo`. If both IDs
are supplied, they must normalize to the same identity. Supported identities are
`session_...`, `cse_...`, `https://claude.ai/code/<id>`, and the exact scheme-less
`claude.ai/code/<id>` form. Query and fragment are discarded. The normalizer's
ASCII, length, exact authority and path restrictions still apply. Validation
checks syntax only, without verifying account ownership or session existence.
The default adoption label uses the normalized session ID, so discarded URL
tracking data is not retained in the label. An explicit label is preserved.

`--repo` accepts GitHub `owner/repo` or an exact HTTPS GitHub repository URL,
with an optional trailing slash. It stores the lowercase owner/repo. Owners are
1 to 39 ASCII alphanumeric or hyphen characters with alphanumeric ends and no
consecutive hyphens. Repository names are 1 to 100 ASCII alphanumeric, dot,
`_` or hyphen characters. Dot paths and `.git` suffixes are refused.
URLs with credentials, ports, query strings or another host are refused.

Optional references are caller-supplied associations. `--branch` accepts a
bounded printable ASCII Git branch name and rejects unsafe ref syntax.
`--commit` requires a full 40 or 64 hexadecimal character immutable Git object
ID and stores lowercase. `--pr` accepts a positive bounded integer or exact
HTTPS GitHub PR URL for the bound repository. Brigade stores its canonical URL.
A commit reference does not prove that a PR currently covers that commit.

## Replay and conflicts

Identity is the case-sensitive provider/session pair. Its registry ID is
`cloud-` plus the first 24 hexadecimal characters of SHA-256 over
`claude-cloud`, a NUL separator, and the normalized session ID. Repository
binding is an invariant of that identity, rather than another identity key.

Register and adopt share the existing registry and Brigade's bounded file-lock
primitive at `.brigade/cloud/registry.lock`. The lock spans read, conflict
checks and atomic save. Concurrent local callers using that registry return one
record for the same identity and repository. Labels, original observation times
and existing evidence survive replay. Missing branch, commit or PR references
can be added. A different nonempty reference is refused as a reference conflict.
A different repository is refused as a repository binding conflict, without
writing a second worker. An existing legacy identity without a repository also
conflicts with an attempted explicit binding. There is no automatic migration.
Both legacy identity fields participate in conflict checks. A legacy write
cannot duplicate a bound session, and conflicting legacy identities or duplicate
historical bindings are refused when an explicit binding would be ambiguous.

Registry compaction and threshold updates share the writer lock. Low-level
`save_registry` remains a snapshot writer whose caller must own the lock when
performing read/modify/write. Local idempotency covers one registry on one
filesystem. Separate client registries do not establish shared authority.

Legacy CLI register/adopt forms without the new binding fields remain accepted
as unbound records. A `claude/` branch, local background session, Remote Control
session or bot author does not establish subscription cloud identity. Inferred
branch rows remain artifact provenance, without a session or repository binding.

## Unknown lifecycle and separate evidence

No supported read-only provider status source was found in the reviewed
capability matrix. Claude rows therefore expose `provider_state: null` and
`provider_lifecycle.state: unknown`, with the bounded source
`unsupported-provider-status` and reason `status-unavailable`. `observed_at` is
the current observation time. `last_confirmed_at` is null unless an existing
provider fact carries a valid confirmation time. Polling never updates that
confirmation or the registry's original observation time.

Without a confirmation, freshness is `unobserved`. An old confirmation yields
`stale` after the configured stale-hours threshold. Its age remains based on
that confirmation. Even a recent stored fact cannot establish a current
provider lifecycle through this unsupported observation path.

Claude registered rows now classify as `needs-investigation` instead of
`pending` or artifact-derived `landed`. This intentional compatibility change
prevents consumers from mapping unavailable lifecycle to running or succeeded.
The local Center activity projection always reports Claude as `unknown`,
including inferred branch rows. Other providers keep their existing projection.

A matching merged PR sets the separate `artifact_state: landed`, while provider
lifecycle stays unknown. Explicit PR references match the canonical URL.
Branch-associated PRs must also carry a URL for the bound repository. Missing
or deleted branches never prove provider completion. Status retains any existing
`local_continuation` and `lease_evidence` as separate facts. Adoption cannot
supply a lease holder or mint holder authority.

The bounded GitHub observer retains explicit references even when their branch
names lack a cloud prefix. Branch evidence requires confirmed observation of
the bound repository; unknown or different repository scope reports
`branch_exists: null`. Observation remains limited to the existing first 100
branch and PR results, so missing evidence outside that window stays unobserved.
An omitted branch reports `false` only when the repository branch snapshot is
complete; a failed, truncated or unproven snapshot reports `null` instead.

Status exposes `lifecycle_counts.claude-cloud` with `active: null`, unknown row
count, and `coverage: unavailable`. Classification counts count local rows, not
active provider sessions. An empty local registry cannot prove zero active
Claude sessions. The provider source remains unwired and disabled by policy for
launch and live inventory. Doctor reports that limitation without invoking
Claude or reading its credentials.

## Remaining issue scope

Adoption is observational. It never admits, binds, renews or releases a Hub
holder. Automatic launch remains disabled. Status and doctor never send cloud
follow-ups, teleport, authenticate a browser, follow arbitrary detail links,
contact provider/private endpoints or inspect provider credential stores. Local
`claude agents --json --all` inventory remains separate from cloud discovery.

Cross-client Hub/Worklore linkage, capacity-holder admission and renew/release
contracts, lease-expiry enforcement, and FleetHub operator readouts remain open
for separate reviewed slices. This local integration does not complete #1600
or establish cross-client capacity authority. It adds no daemon, alternate
registry or runtime dependency.
