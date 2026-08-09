# GeneratedPatchQuarantine

Status: implemented (#507). Sibling to the verifier-bound scorecard path
(`docs/proposals/skill-scorecards.md`): a fail-closed gate that keeps
model-authored edits from promoting a skill until an independent verifier
receipt records repository-test outcomes and generation metadata.

## The problem

Automated program repair research shows that GPT-family patch generators look
better than they behave when evaluation leans on textual similarity rather than
behavioral proof. Lajko et al. (ACM APR workshop 2024,
doi:10.1145/3643788.3648021) repaired at most 12.89% of 1,559 bugs under their
best configuration, could not rule out training-data contamination, and scored
candidates with lexical similarity. Brigade already treats model edits as
untrusted culturally; this gate makes the outcome ledger enforce that
mechanically.

## The mechanism

A verify manifest may declare `patch_source: "generated"`. That marks the
subject delta as a quarantined proposal. Scorecard eligibility then requires
all of:

1. **Independent verifier.** `verifier_identity.session_id` differs from the
   producing work session (`verifier_not_independent` otherwise). Shipped with
   the #571 subject-binding slice; retained here as the independence half of
   #507.
2. **Repository tests.** The receipt carries at least one
   `check_role: effectiveness` command. Utility-only receipts cannot lift
   quarantine (`generated_patch_missing_repository_tests`).
3. **Generation metadata on the receipt.** `subject_binding.generated_patch_quarantine`
   records `candidate_count` (≥ 1), `model`, and `model_version`
   (`generated_patch_quarantine_incomplete` when absent or malformed).

The quarantine envelope schema is `brigade.generated_patch_quarantine.v1`.
Metadata is resolved at verify stamp time from, in order:

- `.brigade/work/<session-id>/generated-patch.json`
- `session.json` → `generated_patch`
- `BRIGADE_GENERATED_PATCH_CANDIDATE_COUNT`,
  `BRIGADE_GENERATED_PATCH_MODEL` (falls back to `BRIGADE_CONTEXT_MODEL`),
  `BRIGADE_GENERATED_PATCH_MODEL_VERSION`

### Explicitly non-promoting signals

These may be copied onto the quarantine envelope for audit, and they may appear
as `outcome record --source …` values, but they never earn a non-zero
`signal_value` and never substitute for the gates above:

- `model_confidence` / `confidence`
- `lexical_similarity` / `textual_similarity`
- `repeated_sampling` (and related audit keys such as `sampling_rounds`)

`outcome.NON_PROMOTING_SOURCES` and
`generated_patch_quarantine.NON_PROMOTING_AUDIT_KEYS` name the contract.
Repeated sampling of the same patch identity also cannot inflate promotion
evidence: scorecards already dedupe by evidence-unit key and ignore
`reused_from` copies.

## What this does not do

- It does not auto-generate patches or run a repair loop.
- It does not change card promotion (cards stay on the legacy ledger path).
- It does not invent model or candidate metadata; missing fields fail closed.
- It does not treat a high candidate count as stronger evidence. The count is
  transparency, not a score input.

## Surfaces

| Surface | Behavior |
| --- | --- |
| `verify_trial.project_trial` | Generated patches need independence + quarantine + effectiveness checks |
| `work verify` (manifest) | Stamps `generated_patch_quarantine` when metadata resolves |
| `outcome.signal_value` | Non-promoting sources always return `0` |
| Scorecards / reconcile | Inherit eligibility; no separate promote path for model confidence |
