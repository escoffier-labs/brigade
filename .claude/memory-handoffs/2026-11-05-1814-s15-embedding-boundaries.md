# Memory Handoff

## Type

project-context

## Title

S15 embedding identity and verification boundaries

## Summary

S15 uses two review slices, with transport in S15a and jobs plus retrieval in S15b. Content-addressed vectors require document normalization to exclude card aliases. A retained private fixture was located through memory and evaluated locally. Its native quality run must be read with S18's import eligibility counts.

## Durable facts

- Model digest, dimensions, normalizer and prefix versions are part of the vector fingerprint. Endpoint responses must report the configured digest.
- Normalizer 1 uses title, summary and body without unrelated frontmatter. An alias must not change the payload for identical content bytes.
- Pull leases require an embed grant on the served scope, a clean or flagged stored version, and a fresh S18 scan. Submissions verify holder, expiry, digest and dimensions.
- A 20,000-row matrix rebuild retaining the old snapshot reached 175,848 KiB at 1,024 dimensions and 655,908 KiB at 4,096 dimensions. These exclude SQLite, lexical postings and a second model.
- Dense compaction captures an immutable matrix and watermark. A background job returns the rebuilt main to the memory writer for key-based tail replay and publication. Derived vector submissions do not advance the memory event sequence, so replay must compare vector keys as well as the captured watermark.

- The native private run imported 1,344 of 1,891 cards. S18 blocked 546 and one id was invalid. All 188 labelled top-five lists matched Python on the imported corpus. Full-corpus reference scores remain higher.

## Evidence

- files changed: `engines/hub/src/memory/embedder.rs`, `embedding.rs`, `dense.rs`, `src/brigade/memory_embed.py`
- commands run: executable scripts through `brigade work verify run --argv-json` with external cargo targets
- measurement receipt: `20261105-233825-work-verify-2e92e5`
- private lexical measurement receipt: `20261105-235600-work-verify-0119a4`
- S15a PR: https://github.com/escoffier-labs/brigade/pull/2314
- S15b PR: https://github.com/escoffier-labs/brigade/pull/2316

## Recommended memory action

no-card

## Target document

.learnings/LEARNINGS.md

## Suggested document content

### Content-addressed embedding payloads

A vector keyed only by content hash and model identity cannot include an alias or card id in document normalization. Keep embedding input independent of scope and alias, and retain explicit tests for shared vectors and cross-scope retirement. Peak matrix rebuild memory must include retained snapshots and must state which other hub components the measurement excludes.
