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
corrections. Fixes and workarounds require a current passing verification receipt
and a resolution quote from the digest. Unresolved failures, cleared transient
or infrastructure failures and tool-broken claims are rejected independently of
prompt instructions. Empty results commit the review without creating memory.

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
