# Skill revision contracts and behavioral evidence

## Outcome

Connect proposed skill revisions to an explicit behavioral contract, a paired
baseline/candidate evaluation, and approval of exact reviewed artifacts. The
implementation extends existing proposal, model-trial, verifier, and fleet-review
paths. No new service or runtime dependency is required.

## Approach

Extend Brigade's existing proposal, trial, verifier and review paths.
Simple prompt/output questionnaires cannot establish observed tool behavior.
A general provider API emulator adds scope without helping the first pilot.
Use a bounded instrumented tool boundary for brigade-work operations instead.

### Revision contract and identity

Proposal metadata records a versioned behavioral contract: reason, concrete
addressed failure, bounded change category, baseline fingerprint, and expected
observable behaviors with assertion IDs. Candidate identity covers the skill
package and relevant metadata. Preserve the contract through acceptance. Legacy
proposals remain visible as incomplete. Acceptance rejects candidate mutation
and a changed baseline. Retain existing filesystem and symlink protections.

### Paired pilot

Compare immutable baseline and candidate packages on passing verification,
failing verification, simulated command timeout, audit-only receipt, and hostile
tool response scenarios. Both arms use the same model/settings and inputs, fresh
sessions and fixture state. Alternate arm order across repetitions. Include
package, scenario, emulator and grader identities in resumable cell identity.

Record tool calls, responses, receipt IDs and state transitions outside agent
write authority. Reject unsupported calls and record them. Grade the final
report against the observed trace, not answers asking the model to assess its
own conduct. A PATH wrapper alone does not establish containment. Distinguish
simulated tool timeout from model/apparatus timeout. Keep incomplete pairs and
regressions visible. Offline apparatus checks and real model runs are separate
deliverables. Record and report both separately.

Evaluation results remain audit-only until the registered fixture verifier
validates exact subject identity, independent evaluator provenance, and check
metadata. Repetitions of the same scenario do not create independent promotion
evidence. Do not weaken scorecard eligibility to make the pilot count.

### Exact approval

Review a stable snapshot with explicit approved node/workspace/harness copies.
Bind source identity, installed and target package identities, contract identity
and evaluation evidence. Validate a displayed review token against the current
view on submission. A changed artifact, target, evidence set or missing binding
requires review again. New fleet copies cannot inherit an old approval.

Plans must withhold executable commands when approval is stale. Mutation-time
preconditions validate the expected identities against the same collected bytes
used for installation, removal or rollback. Bind rollback to the exact snapshot.
Preserve legacy drafts for inspection without treating them as approved.

## Implementation sequence

1. Add proposal contracts and package identity validation with regression tests.
2. Bind fleet review display/save/plan and local mutation preconditions to exact
   identities. Test stale pages, metadata/helper changes, new copies and legacy
   drafts. Capture required real before/after screenshots for visible changes.
3. Add the smallest paired-trial and trace-grading path. Prove one live pair,
   then execute all five cases. Report ties, regressions and apparatus failures.
4. Independently review the integrated change, run focused checks through
   Brigade while iterating, and run the full gate once for this verification
   infrastructure change. Follow required CI and review gates through merge.

## Verification and review focus

Before editing, `brigade code sync .` indexed 1,937 files. `brigade code affected`
covered inbox, skills CLI, fleet skills review/HTTP/report, install, sync/history
and model trials with no missing paths. The actual graph result is retained in
`/tmp/brigade-skill-revision-impact.json`. Static attribution is a lower bound.
Affected consumers include learning proposals, skill adapters, outcome discovery,
verifier manifests, scorecards and the skills facade. Relevant test owners include
`test_skills_cmd`, `test_skills_cmd_facade`, `test_fleet_skills_review`,
`test_context_learning_closeout_cmd`, and model-trial/verifier tests. The focused
selectors must include the precise owners touched by each implementation slice.

Review focuses on contract preservation, changed candidates and baselines,
whole-package identity, stale browser submissions, new fleet copies and exact
rollback identity. Verify mutation after planning, paired resume invalidation,
incomplete pairs, forged success reports, unsupported calls and trace integrity.
Check evaluator provenance, audit-only exclusion and repeated-scenario
deduplication. Tests must exercise the owning public boundary and preserve
existing failing tests.

## Boundaries

No fleet-wide deployment, global installed-skill edits, new dependency, new
service, promotion-policy relaxation, paid API fallback, or unrequested release.
Live execution must use an available authorized model transport. If that
transport cannot enforce the tool boundary, report the specific blocker and
retain an unscored pilot with the blocker attached.
