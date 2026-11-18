- Add bounded Tier 0 hub reflection proposals that anchor clean live claim blocks without rewriting other text, with replica coverage in reflection status and exact rollback through the existing journal.

- Require an exact hub-recorded automatic apply head for automatic anchoring. Treat direct writes, imports and human-reviewed heads as review-only, and identify all-no-delta anchor refusals with a stable hub code.

- Match reflection anchoring to hub-supported blocks, preserve anchor suffixes and line endings, count deferred work accurately, and read progress without the write lock. Automatic anchoring requires a hub-derived structural proof and automated writer provenance.

- Keep human anchoring holds out of automatic retry budgets until the head changes. Preserve real refusal reporting beside no-delta skips, accept whitespace-preserved pins in reflection, and skip storage-dependent tests on Windows.

- Derive anchoring eligibility from the exact automatic hub apply event, skip other heads, reserve four pending slots for repairs, and withdraw unapplied anchors with an audit reason.

- Strip claim anchors from history text ending in a newline.

- Read automatic hold diagnostics from preflight checks so stale anchoring revisions are suppressed while fresh eligible heads advance.
- Hold a withdrawn anchoring proposal for seven days, then let reflection propose the same anchor again, so a withdrawal only delays an anchor.
