# Memory Handoff

## Type

security

## Title

Instruction preview needs path and root-decision revalidation

## Summary

Held descriptors preserve safe reads but can still describe a displaced tree.
The Codex instruction preview now revalidates directory path bindings and root
marker observations before reporting completed accounting. Raw filename lists
also have a separate resource bound before normalization or discovery.

## Durable facts

- Document-selection validation alone misses a new deeper root marker and
  renamed directory components, including a retained global scope.
- Directory identity uses device, inode and file type. Directory timestamps
  change with unrelated children and are unsuitable for identity checks.
- Each raw fallback or root-marker list is limited to 64 entries. Repeated and
  invalid entries count before encoding, trimming and filtering.
- Separate final metadata checks do not establish an atomic snapshot. Changes
  after a component's check or displacement followed by restoration can escape.
- The preview hardening slice references #1564. Doctor integration, automatic effective
  configuration observation and actual-session consumption remain outside it.

## Evidence

- files changed: `src/brigade/context_preview.py`,
  `tests/test_context_preview.py`, `docs/codex-context-preview.md`
- graph: `brigade code affected src/brigade/context_preview.py --json` found
  `context_cmd.py` and `cli/context.py`. No attributed tests is a lower bound.
- fail-first: receipt `20261011-024053-work-verify-a1c2f2`,
  `./scripts/verify-focused tests/test_context_preview.py`,
  `22 failed, 120 passed in 1.82s`, with production unchanged.
- focused: receipt `20261011-024243-work-verify-2b1ea3`,
  `./scripts/verify-focused tests/test_context_preview.py tests/test_context_cmd.py`,
  `163 passed in 1.68s`. Both ran through `brigade work verify run` with
  `--capture brigade-work`. Receipts are audit evidence.

## Recommended memory action

no-card

## Target document

.learnings/LEARNINGS.md

## Suggested document content

### Descriptor-safe inspection and current path accounting

Directory descriptors anchor reads, but a renamed ancestor can leave them
pointing at a displaced tree. Before reporting fresh path accounting, validate
each held directory's identity against its name in the retained parent and
recheck root-discovery observations as well as selected files. Compare directory
identity without timestamps to tolerate unrelated child updates. Bound raw
filename-list counts before normalization. These checks remain non-atomic.
