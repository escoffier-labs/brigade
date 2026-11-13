# Brigade feature map

The maintained list of user-facing Brigade surfaces this skill knows how to
drive. A proof that only exercises one entry point is incomplete when the
change touched another one listed here.

Every file below answers the same four questions: what the feature is
(`Sub-features`), how a user reaches it (`How to get to it (user POV)`), the
exact helper calls (`Driving it with control-brigade`), and what bites
(`Gotchas`).

| Feature | File | Drive with |
|---|---|---|
| Hub connector discovery and preview | [hub-connector.md](hub-connector.md) | `hub-connector` |
| Install and wiring health | [init-and-doctor.md](init-and-doctor.md) | `new-target`, `init`, `doctor-target` |
| Work verification receipts | [work-verify-receipts.md](work-verify-receipts.md) | `work-verify` |
| Handoff inbox lint | [handoff-lint.md](handoff-lint.md) | `handoff-lint` |
| Guard scan and redact parity | [guard-parity.md](guard-parity.md) | `guard-parity` |
| Fleet Hub wire contract | [hub-contract.md](hub-contract.md) | `hub-contract` |
| Fleet Hub storage parity | [storage-parity.md](storage-parity.md) | `storage-parity` |
| Run plan preview | [run-dry-run.md](run-dry-run.md) | none yet (paused) |

Each file opens with a status header: `active`, `paused` (with a reason and
what unpauses it), or `unreachable` (with what is missing). Paused today:
`control-brigade.py audit` to compare the map with the live CLI, and follow
[MAINTENANCE.md](MAINTENANCE.md) when the CLI surface changes. That page also
holds the coverage and honesty rules and the list of known gaps.

Stand-in proofs: checkout-level pytest selectors, not helper drives. A
passing selector here is not a drive of the command (see
[MAINTENANCE.md](MAINTENANCE.md#honesty-rules)). Names
confirmed under `tests/` on this checkout; the skill body table is the
copy agents should cite.

| Surface | Command | Tests |
|---|---|---|
| run | `brigade run` | `tests/test_run_cli.py`, `tests/test_run_lifecycle.py`, `tests/test_run_events.py`, `tests/test_run_control.py`, `tests/test_run_resume.py`, `tests/test_run_receipts.py` |
| work verify | `brigade work verify` | `tests/test_work_cmd_verification.py`, `tests/test_work_cmd_verify_ranking.py`, `tests/test_verify_trial.py`, `tests/test_verification_contract.py`, `tests/test_verify_brigade_skill.py` |
| fleet | `brigade fleet` | `tests/test_fleet_*.py` (58 files; start with `tests/test_fleet_claims.py`, `tests/test_fleet_nodes.py`, `tests/test_fleet_sync.py`) |
| connector | `brigade hub connector` | `tests/test_cloud_agent_profile.py`, `tests/test_dot_transport.py` |
| guard | `brigade guard` / `brigade scrub` | `tests/guard/test_cli.py`, `tests/guard/test_engine.py`, `tests/guard/test_rules.py`, `tests/test_scrub.py`, `tests/test_runguard.py`, `tests/test_verify_brigade_guard_parity.py` |

Helper drives still missing a feature file, and worth adding when a change
lands there: `brigade memory`, `brigade security`, `brigade outcome`,
`brigade evidence`, `brigade code`, `brigade skills`, `brigade daily`.
`brigade guard scan` / `redact` have the parity drive above, and the Fleet Hub
HTTP contract behind `brigade fleet` has the `hub-contract` drive, and the
rows it stores have the `storage-parity` drive; `brigade scrub`, the other `guard` verbs, and the `brigade fleet` CLI itself have
checkout test selectors only. Add a helper feature file with the same four H2s rather than stretching
an existing one.

Setup shared by every file:

```bash
C=registry/skills/verify-brigade/control-brigade.py
TARGET=$($C new-target | python3 -c 'import json,sys; print(json.load(sys.stdin)["target"])')
```

Teardown, always:

```bash
$C evidence --target "$TARGET" --label <what-you-proved>
$C cleanup
```

Connector lifecycle: `brigade hub connector`. Verify with `tests/test_cloud_agent_profile.py` and `tests/test_dot_transport.py`.
