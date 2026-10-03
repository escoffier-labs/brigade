# Reviewed memory pair proposals

This slice resolves one saved memory-care finding through an exact preview,
explicit review, and a recoverable transaction. Markdown cards remain canonical.
Proposal creation never edits canonical cards.

## Proposal contract

`brigade memory proposal create` selects a saved care issue by ID, a survivor
path, and `merge` or `supersede`. The issue must name exactly two eligible card
paths. Both must still share an identity or alias collision in the current
owner workspace. A collision is recorded as `identity-collision`. A separate
`opposite-polarity` signal can flag assertions for review. Duplicate IDs alone
never establish a semantic contradiction.

The versioned proposal records:

- The finding ID, fingerprint, owner binding and selected relation.
- Both source paths, IDs, aliases, exact original text and raw-byte SHA-256.
- Source provenance, citation identifiers, scope, and the operator's reason.
- The survivor identity, alias migration and supersession or merge relation.
- Every intended mutation, including exact before and after text, hashes,
  losing-card removal, and affected configured index links.

Each proposal is bounded to two cards and configured index files. Creation
validates paths, scope and trust before composing the candidate. All paths must
be regular files under allowed roots, without symlinked ancestors or multiple
hard links. Reads are descriptor-bound and preserve exact UTF-8 bytes. Exclusions
apply before composition. Namespace, scope, owner, repository, task, operator,
branch and worktree metadata must agree whenever either source declares a
value. Sources must belong to the same configured card root.

Legacy cards already in the selected canonical owner workspace are eligible
after a clean injection scan. Explicit unknown, untrusted or quarantined labels,
and pending, error or flagged injection states, are ineligible. Declared
provenance must pass the existing trust gate as well. Proposal creation never
silently upgrades a source's trust. Citations preserve source identifiers and
hashes. External cited content is not fetched or incorporated.

Merge combines the two eligible bodies and preserves both sets of provenance.
A detected polarity conflict blocks merge and requires explicit supersession.
Supersession keeps only the chosen survivor's assertions. Both retain the
survivor's valid stable ID. A legacy survivor receives a deterministic stable ID
and retains its old keys as aliases. The losing card's ID, path and legacy keys
become explicit aliases of the survivor. Any resulting third-card identity
collision blocks the proposal. Relation metadata distinguishes merged sources
from superseded sources.

The losing canonical file is removed only at accepted apply. Its exact prior
content remains in the private proposal archive outside canonical card roots.
It therefore cannot outrank the replacement in canonical retrieval or be
projected as a current card. Configured Markdown index links are rewritten to
the survivor as previewed, revision-fenced mutations. Wiki-link validation
recognizes explicit migrated aliases. A non-index direct file link that would
break blocks creation rather than expanding this slice to rewrite other cards.
Ambiguous references also block creation. Reference and identity checks repeat
at apply so newly added conflicts invalidate the old proposal.

## Review and apply

`show` displays the proposal digest and exact mutations. Creation also writes
a standard memory-owner handoff containing the proposal reference, citations
and review instructions. The handoff uses a no-card action and does not contain
an ingest-promotable canonical rewrite. Handoff lint status is not approval.

`review` and `reject` require the exact proposal digest and a reason. They reuse
the existing digest-bound provenance event ledger with a dedicated proposal
item reference, operator command and edit-accepted or edit-rejected decision.
Decisions bind the proposal revision, owner and scope as well as its digest.
An accepted content trust label alone is not permission to apply a proposal.
Rejection is terminal for that digest.

`apply` requires the reviewed digest, unchanged source bytes, matching owner
and scope, intact prior-content archive, and unchanged configured index files.
The operation revalidates under an exclusive local lock and uses the existing
projection transaction kernel for card replacement, card removal, index edits
and an application marker. Compare-before-write callbacks raise rollback-safe
errors. A stale source refuses without partial canonical writes. Failed I/O
returns the kernel's restored or recovery-required state and recovery command.

The original source bytes are retained independently of the kernel's temporary
backups, which are deleted after commit. A retry checks the application marker,
committed receipt and expected after-state. It never reruns a committed kernel
operation merely because a later bookkeeping step failed. An unfinished kernel
operation requires recovery before another apply. A restored attempt uses a
fresh operation ID after full validation. Finding, configuration, trust,
identity, reference or source drift invalidates the old acceptance. Reverting
a completed apply requires a separate reviewed compensating change.

Review uses the existing local operator trust boundary. A process able to alter
the operator's files can also write review events. This feature does not add a
separate authenticated approval service.

## Implementation plan

One worker owns the write path. Use existing identity, handoff, trust-event and
projection APIs. Add no runtime dependencies or model-provider integration.

1. Add public workflow tests in `tests/test_memory_proposals.py`. Capture an
   expected failing focused run before implementing creation, preview, review,
   rejection and apply in `src/brigade/memory_proposals.py`. Wire the command
   family in `src/brigade/cli/memory.py`.
2. Extend `card_identity.py` to consume bounded explicit aliases only for valid
   stable IDs. Add behavioral identity and third-card collision tests. Prevent
   normal ingest from importing reserved alias/proposal relation metadata.
   Teach wiki-link lint to resolve migrated aliases without hiding collisions.
3. Implement revision-fenced application using `projection.kernel`. Cover
   source and proposal tampering, owner/scope drift, rejected proposals,
   transaction rollback, preserved prior content, and committed retry behavior.
   Use temporary targets for every test.
4. Extend existing retrieval and projection evaluation tests with accepted,
   rejected, stale, contradictory and cross-scope workflows. Exercise actual
   current-adapter rankings and vault projection, including old-ID alias
   resolution and absence of the removed card from current results.
5. Run the focused gate, resolve independent code and authority reviews, then
   run the coordinated full gate and required CI before merging the exact head.

The focused command is:

```bash
brigade work verify run --target . --argv-json '["./scripts/verify-focused","tests/test_memory_proposals.py","tests/test_card_identity.py","tests/test_ingest.py","tests/test_memory_cmd.py","tests/test_memory_retrieval_eval.py","tests/test_memory_vault_projection.py","tests/memory_doctor"]' --capture brigade-work
```

This slice does not discover pairs across the corpus, rewrite cards in the
background, change an external retrieval engine, or edit operator memory during
development. Growth beyond a two-card finding requires a separate contract.
