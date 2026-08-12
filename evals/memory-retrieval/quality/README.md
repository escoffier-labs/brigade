# Memory quality metrics (#845)

Read-only stale-recall, projection-drift, and scope-leakage metrics over
checked-in cards. Nested under the #722 retrieval eval envelope
(`projection.quality`), not a parallel harness.

Cards use canonical `card-<uuid>` ids; retrieval/projection rows may key by
canonical id or legacy aliases (path, stem) so dual-read is exercised.
