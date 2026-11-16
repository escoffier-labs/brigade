- Add bounded Tier 0 hub reflection proposals that anchor clean live claim blocks without rewriting other text, with replica coverage in reflection status and exact rollback through the existing journal.

- Require an exact hub-recorded automatic apply head for automatic anchoring. Treat direct writes, imports and human-reviewed heads as review-only, and identify all-no-delta anchor refusals with a stable hub code.

- Match reflection anchoring to hub-supported blocks, preserve anchor suffixes and line endings, count deferred work accurately, and read progress without the write lock. Automatic anchoring requires a hub-derived structural proof and automated writer provenance.

- Keep human anchoring holds out of automatic retry budgets until the head changes. Preserve real refusal reporting beside no-delta skips, accept whitespace-preserved pins in reflection, and skip storage-dependent tests on Windows.
