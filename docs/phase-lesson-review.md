# Automatic lesson review (#2601)

Post-tool observations from the existing work hooks count activity per target.
User prompts provide correction evidence but do not increase the tool counter.
A care entry checks the counter after a five-minute idle window. The default
threshold is 20 calls. Configuration can disable review or select a roster seat.

State records observed and committed sequence numbers under
`.brigade/lesson-review/`. A run lock coalesces concurrent care invocations.
Activity recording uses a separate short lock, so new work can interrupt a
review. Committed reviews and quarantined digests advance their own watermark.
Failures retain activity within a three-attempt budget with persisted backoff.
A prepared draft survives process interruption
and is recovered before another worker is dispatched.

The digest contains at most 96 observations of 2 KiB each. Content scanning and
injection checks happen before storage and dispatch. The review uses the existing
read-only single-seat dispatcher with a bounded timeout, without a new provider
adapter. A first response requests up to four cards. Their current bytes are read,
scanned and supplied to the final response in this run. Admission requires that
same-run read and an unchanged hash for updates.

Admission accepts only verified fixes, worked workarounds or quoted user
corrections. Fixes and workarounds require signed failing and passing receipts
from a registered card manifest, bound to the producing work session in the
reviewed observations and the proposed card bytes. Ad hoc code checks do not
qualify. Fixture evidence can establish new card content. Patch evidence supports
existing-card updates, checking the failure historically and the pass against
the live card. Unchanged-tree retries do not qualify. User corrections remain
quoted claims with content and lexical screening, subject to human review.
Empty results commit the review without creating memory.

Receipt authority uses the same fixed-path, private, operator-owned, single-link
workspace key confinement as #2577. It never uses the audit-key environment
override or creates a key. Windows authority stays unavailable pending #2588.
A MAC authenticates tooling against text forgery. An agent with shell access can
read and use the key.
Both failed and successful verification observations take priority over routine
events. The bounded private prompt spool retains multiple prompts and is drained
under the state lock after the worker returns, before comparing sequences.

The coordinator executes the implementation behind `brigade handoff draft` as
the worker's only memory write path. A prepared record binds the draft bytes to
the digest and review ID before publication. Descriptor-bound ingest
delivers only that draft to review. Canonical cards are never written directly.
Machine origin `brigade.lesson-review.v1` identifies review evidence only.
Automatic filing depends on #2577 accepting a receipt-bound machine origin.
That interface is absent, so automatic promotion stays disabled.

Install with `brigade care install --entry lesson-review`. Disable with
`brigade memory lessons disable`, or uninstall the care entry. Repository and hub
targets each retain their own local activity and watermark. No transcript crawl,
cross-machine activity transfer or canonical memory mutation is added.

Validation uses a fake worker for verified capture and ingest, idle retries,
coalescing, empty results, admission failures, same-run card reads, stale cards,
scanner refusal, crash recovery and the off switch. Existing hook, draft, ingest
and scheduler tests cover shared integration paths. Graph lookup was attempted
before changes but this worktree has no graph database. Source-traced impact:
post-tool hooks, memory CLI registration, care catalog and pinned launchers.
