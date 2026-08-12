# Issue #846 R3 - bounded SQLite release characterization repair

Refs #846. R3 repairs only the research SQLite adapter and its characterization artifacts. It does not select or adopt a backend, modify the production store, add a dependency, start a listener, or migrate the source of truth.

## Provenance and scope

- Baseline: `3532f07cb125bfb6ad5c06a3fab0eb97ee555aeb` on the current `main` lineage.
- Inputs: the accepted R1 and R2 harness artifacts plus the bounded R3 correction request.
- Harness: `scripts/measure_work_store.py`, standard-library SQLite, 10 warmups and 100 measured trials per dataset.
- Output: `docs/measurements/issue-846-work-store-r3.json`.
- GraphTrail was invoked through `brigade code doctor --target .` and `brigade code impact _sqlite_release --target .`; both reported `error: graphtrail is not installed; run \`brigade setup\``. No graph result is claimed.

The accepted R2 JSON and report remain byte-for-byte unchanged. R3 uses only synthetic, machine-neutral data; the measurement output records platform class and contains no private workspace path.

## Corrected behavior

The SQLite adapter now performs release under `BEGIN IMMEDIATE` and delegates task mutation semantics to the canonical claim helpers:

1. Releasing a pending, unclaimed task returns exit `1` with reason `not_claimed`; the transaction rolls back and the exported ledger is byte-for-byte unchanged.
2. A successful claim creates the canonical nested claim record and increments top-level `item_revision`.
3. A successful release increments `item_revision` again, returns the released claim record, and removes `assignee`, `claim`, `claim_id`, and `claimed_at` while returning status to `pending`.
4. `if_actor` mismatch, `if_status` mismatch, matching guards, and an empty actor filter execute against the SQLite transaction path. Mismatches and empty filters roll back.

The R3 measurement reports the SQLite guard/release cell as measured rather than unavailable. All JSON-ledger behavior and the no-listener/Dolt-blocked boundaries remain characterization-only.

## Decision record

The decision remains to keep the machine-local JSON ledger. SQLite remains a standard-library, local characterization adapter only. Dolt remains neither selected nor banned, and all listener-dependent cells remain blocked.

## Verification protocol

Focused checks are run through Brigade work verification with the measurement regression and the #737/#738 anchors. The repository completion gate is `./scripts/verify`. Measurement values are observations from this checkout and are not service-level commitments.
