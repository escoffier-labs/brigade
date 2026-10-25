# Memory Handoff

## Type
security

## Title
Operator questions require a separate message principal

## Summary
The ask_user review found that selecting a node as operator let its agent sessions answer with the same credential. Question creation now requires a dedicated agent principal, and automatic live delivery excludes question messages. Operator authentication remains an assertion from the trusted hub.

## Durable facts

- Schema 43 follows main's schema 41 and 42 migrations. Question audit repair adds late-answer columns and replaces the legacy reply trigger without deleting audits.
- A direct database insertion from a peer must leave the question pending. HTTP-only tests did not catch removal of the trigger's operator-sender guard.
- The first late operator reply records its message ID and receipt timestamp, while the question remains timed out with no answer.
- Desktop results carry the desktop-local label and no operator authentication. The dedicated operator token belongs only in its human inbox client.
- Hub database write access can fabricate operator authority. Signed, sealed answers remain the agent mesh slice 7b design in PR #2013.

## Evidence

- Files: `src/brigade/fleet_hub_questions.py`, `src/brigade/fleet_ask_user.py`, `src/brigade/fleet_inbox_delivery.py`, `tests/test_fleet_questions.py`.
- Mutation receipt: `20261022-233308-work-verify-97a275`. Removing `operator=NEW.sender` produced `('answered', 'Peer answer', 2)` instead of `('pending', None, None)`.
- Green trigger, identity, and migration receipt: `20261022-233647-work-verify-ad4472`.
- Command inventory receipt: `20261022-233126-work-verify-4534cd`.
- Code Graph impact: four initial source paths, 19 statically attributed test files, 61 impacted files, no missing indexed paths. `untrusted.py` and the transport credential module also received impact queries before their label and error-code changes. Dynamic question consumers were tested explicitly.

## Recommended memory action
no-card

## Target document
.learnings/LEARNINGS.md

## Suggested document content

### Question authority needs a dedicated principal and a storage-boundary regression

For operator questions, keep the operator token separate from default node and agent session credentials. Exclude question messages from automatic agent-session delivery. Test reply authority directly at the database trigger as well as through HTTP: route authorization can mask a missing trigger predicate. Plaintext audit storage and hub-returned authority fields require trusting the hub until signed, sealed answers land.
