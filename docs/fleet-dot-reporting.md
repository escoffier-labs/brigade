# Explicit Dot cloud session reports

`brigade fleet dot report` validates a bounded JSON metadata snapshot from stdin
or `--metadata-file`. Dry-run is the default and loads no authentication, holder
file, local checkout, Git state or network client. `--publish` opts into the
existing Fleet Hub `/sessions` and Worklore ownership clients.

```bash
brigade fleet dot report --metadata-file dot-metadata.json
brigade fleet dot report --metadata-file dot-metadata.json --publish
```

A minimal presence report is:

```json
{
  "version": 1,
  "session_id": "session-example",
  "agent_label": "worker-example",
  "source": "cloud_threads",
  "source_scope": "caller-created-tasks",
  "coverage": "explicitly-reported-sessions",
  "observed_at": "2026-01-01T00:00:00Z",
  "sequence": 1
}
```

`cloud_threads` snapshots can describe caller-created tasks or authorized
visible threads. Set `source_scope` to `caller-created-tasks` or
`authorized-visible-threads`, respectively. Explicit manual metadata uses
`source: explicit-metadata` and `source_scope: explicit-session`, which are the
defaults. Coverage is always `explicitly-reported-sessions`. These records
track known sessions and make no account-wide completeness claim. The adapter
accepts sanitized metadata supplied by the caller and does not fetch threads.

The optional fields are `parent_session_id`, `repo_identity`, `work_id`,
`ownership_revision`, `generation`, `task`, `progress`, `blocker`, `result`, and
`evidence_refs`. With `work_id`, both ownership counters are required. Without
it, ownership counters are refused. Session `sequence` and Worklore
`ownership_revision` are independent counters. `observed_at` requires an
explicit timezone. A repository identity must already be canonical
`host/owner/repository`, without scheme, credentials or `.git` suffix.

IDs and agent labels are opaque strings of at most 128 characters. Each summary
is a single line of at most 400 characters. A report permits eight evidence
references, each at most 256 characters, and at most 16 KiB of encoded JSON.
Oversized input is refused rather than truncated. Unknown fields, duplicate
JSON fields, credentials, Bearer/header text, percent-encoded strings, controls,
absolute local paths and userinfo or query/fragment metadata are refused before
publication. Transcript, prompt, instructions, private notes, raw logs and
headers are outside the allowlist.

Evidence references are opaque IDs or HTTPS URLs. URL policy uses syntax only:
port 443 or the default, no userinfo, query or fragment, and a global unicast
literal IP or a dotted hostname with valid labels. Single-label and special
local domains (`localhost`, `local`, `internal`, `test`, `invalid`, `home`, `lan`)
are refused. No DNS lookup, URL fetch, reachability check or evidence verification
occurs. A dotted name can resolve privately, so acceptance is not proof that a
reference is public or reachable. References remain `reported-unverified`.

## Presence identity and compatibility

The enrolled node authenticated by the existing node token owns the report.
`agent_label` attributes it to an agent and conveys no additional authority.
Cloud rows use the existing interactive-session registry. They have no checkout
path, branch or dirty paths, and an absent repository is represented without
inventing a local identity. No harness manifest or additional registry is added.

Provider, agent, parent, optional work linkage, repository and source scope stay
immutable across refreshes from the same reporter. A parent is an opaque
same-reporter reference. It can be unresolved, but inserting a later parent
checks for cycles in the same transaction. Traversal stops after 64 references
and refuses a longer unresolved chain.

Sequence must advance with a nondecreasing observed timestamp. An identical
sequence/context replay does not extend TTL. The Hub expires presence from the
reported observation time, capped by receipt time, and refuses observations
more than 60 seconds in the future. A delayed heartbeat cannot make an old
observation fresh. TTL expiry means stale presence. Provider lifecycle stays
`unobserved`, and the adapter cannot end a provider session or infer completion.

Schema 24 adds a nullable cloud-context column to the existing schema-23 table.
Local snapshots omit the new field and retain their existing wire representation.
Mixed local and cloud clients can use the upgraded Hub. An older Hub rejects the
cloud field, and an older binary refuses a schema-24 database. Do not downgrade
or silently strip cloud provenance to obtain acceptance.

Observed records remain available through supported commands:

```bash
brigade fleet sessions --json
brigade fleet sessions --all --json
brigade fleet status --json
```

`--all` requests bounded known history, not all Dot account sessions. The
existing history limit is 1,000 rows and the active limit is 500.

## Fenced Worklore reporting

For an explicit existing `work_id`, metadata is appended as an immutable
`ownership-reported` event through the existing ownership protocol. The work
must already be owned by the enrolled reporting node. The request needs its
accepted generation, current ownership revision, and existing holder capability
in `X-Worklore-Holder`. The Hub checks these in one transaction with replay and
state validation. It requires no operator privileges for reporting.

`last_report` projects the reported summaries, source/scope, coverage and
unverified evidence, with the authenticated `reporter_node`. Reporting leaves
accepted scope, exclusions, budget, scheduling, claims, task description,
blocker, acceptance, task version and lifecycle unchanged. A reported result
never marks work complete, merged or verified. Ownership event capacity and
terminal-work guards continue to apply.

The adapter derives a deterministic key from work, ownership generation,
session and sequence. The existing ownership fingerprint binds content, CAS
revision and holder. A retry with the original metadata and revision returns
its existing event. A same-sequence changed summary or CAS revision conflicts
rather than creating a second report, including after a presence HTTP 200 replay.
Only the most recent report is projected, while previous events remain immutable.
Within the ownership transaction, reporting checks the latest historical report
for the same authenticated reporter/session, under the existing bounded event
history. Interleaved reports for other sessions cannot hide an older sequence or
changed linkage. This does not add an inventory or a per-session task database.

The Python adapter accepts `holder_nonce` as a secure runtime parameter to
`fleet_dot.report_session(metadata, publish=True, holder_nonce=...)`. It never
accepts a holder capability inside remote JSON. CLI publication can use
`--holder-file` with an already-existing absolute owner-private regular file,
read through the existing no-follow descriptor reader. The reader refuses unsafe
permissions, symlinks and oversized files. It fails closed on platforms without
the required descriptor/owner support. Dry-run never reads this file. The
adapter creates no credentials.

Missing holder authority permits presence publication but refuses the Worklore
component. Missing node authentication refuses publication, without using an
admin fallback. Output gives separate presence/work results and preserves
unknown outcomes on transport or malformed acknowledgments. With no Worklore
item, summaries and evidence are projected only in `nonpersisted_metadata` and
are not persisted as a second task store. Local validation does not demonstrate
successful publication or provider progress.

## Later MCP transport and owner review

This slice provides the reusable Python adapter, not a deployed MCP server or
tunnel. A later write-capable transport needs an approved route to the existing
Hub, its own enrolled nonrevoked node identity/token, and an existing explicitly
accepted Worklore holder capability for report actions. Transport principals
must preserve node identity, generation and ownership CAS, and keep the holder
outside remote metadata and tool results. No admin token or new operator role
is needed for routine reports. Enrollment, deployment and private-network routing
remain separate operator actions. The Grok Bot operator read-only canary does
not establish any of these write permissions.

The adapter performs no claims, capacity reservation, execution, canonical-memory
writes or messages. A separate sanitized handoff draft can follow the existing
Memory Handoff format for OpenClaw owner review. Creating or referencing a draft
does not prove delivery, ingestion or promotion. Those remain unverified until
an actual receipt is observed.
