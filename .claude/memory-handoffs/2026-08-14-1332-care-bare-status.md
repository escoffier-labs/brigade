# Memory Handoff

## Type

bugfix

## Title

Bare care status discovers target-scoped registrations

## Summary

`brigade care status` previously assessed the atomic five-entry default even when a target had only per-entry registrations. Bare systemd and launchd status now discovers target-namespaced registrations before aggregation. Systemd status reports `enabled: false` until every discovered timer has its `timers.target.wants` link.

## Durable facts

- A missing aggregate status now means the target has no discovered scheduler registrations.
- Explicit `--entry` selection keeps its requested-entry behavior.
- Systemd registration files and systemd enablement are separate states.

## Evidence

- files changed: `src/brigade/care_cmd.py`, `src/brigade/cli/care.py`, `tests/test_care_cmd.py`
- commands run: `brigade work verify run --target . --command "pytest -q tests/test_care_cmd.py::test_care_bare_status_enumerates_per_entry_registrations_and_enablement" --capture brigade-work`
- commands run: `brigade work verify run --target . --command "./scripts/verify" --capture brigade-work`
- error strings: `AssertionError: assert 'missing' == 'current'` before the fix

## Recommended memory action

no-card

## Target document

.learnings/LEARNINGS.md

## Suggested document content

### Care bare-status discovery

For namespaced care registrations, bare status must discover the target's installed entries before aggregating their definition state. On systemd, installed unit files do not prove a timer is enabled; inspect the `timers.target.wants` link separately.
