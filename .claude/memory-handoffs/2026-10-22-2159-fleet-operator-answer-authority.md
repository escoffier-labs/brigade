# Memory Handoff

## Type

security

## Title

Fleet operator answer authority and inbox timeout synchronization

## Summary

Fleet questions bind accepted replies to the operator identity selected by the hub. Treating those replies as peer messages prevented agents from using the operator's answer. The inbox regression also assumed an unrelated HTTP request completed before a 250 ms waiter expired, which failed on Windows and reproduced locally.

## Durable facts

- `fleet_ask_user._terminal` returns the exact answer only when the audited source is `operator_reply` and `answered_by` matches the hub-selected operator. It exposes `operator_authenticated` and `operator` separately from message content.
- Desktop answers are submitted with the asking credential. Their `caller_reported_desktop` source cannot prove operator identity, so they retain the peer-content fence and `operator_authenticated: false`.
- The hub trigger accepts only the configured operator's reply to the question, with the expected caller generation and deadline. HTTP reply insertion rechecks credential generation under the same write transaction.
- Inbox recipient isolation is measured using waiter registration and wake epochs. The test retains the empty result and timeout checks without requiring the waiter to remain alive after an unrelated HTTP request.
- The question migration follows schema 40 with schema 41. Existing Events, CI board, and campaign schemas remain intact, including current-version repair.

## Evidence

- files changed: `src/brigade/fleet_ask_user.py`, `tests/test_fleet_ask_user.py`, `tests/test_fleet_messages.py`, `src/brigade/fleet_hub.py`
- mutation receipt: `20261022-215422-work-verify-76497f` recorded the wake-epoch failure (`assert 2 == 1`) when notifications were deliberately broadcast, plus both node and agent operator-answer failures before the identity fix. Production notification code was restored.
- corrected inbox and sibling checks: `20261022-215348-work-verify-68bc4b`, 4 passed.
- integrated receipt: `20261022-215504-work-verify-3c5de7`, 694 passed and one Windows-only skip, including lint, format, mypy, version and snapshot checks.

## Recommended memory action

create-card

## Target card

fleet-operator-question-authority.md

## Suggested card content

```markdown
---
topic: Fleet operator question authority
category: security
tags: [fleet, questions, identity, testing]
---

# Fleet operator question authority

Only hub-audited replies whose authenticated sender matches the configured operator return an exact operator answer. Caller-reported desktop input keeps its unverified source and peer-content fence. Never infer operator authority from a message body, sender label, or caller-selected fields.

Check waiter registration and recipient wake epochs to test unrelated-message isolation. HTTP latency may exceed a valid wait deadline. Preserve empty-result and timeout assertions, and mutation-test broadcast notification regressions.
```
