# Live roster model inventory implementation plan

### Task 1: Classify live harness inventory

**Files:** `src/brigade/model_inventory.py`, `tests/test_model_inventory.py`

- [x] Add failing tests for exact, narrow fuzzy, missing, and unavailable Cursor inventory results.
- [x] Add failing tests for Grok's model-list shape and the same exact/fuzzy boundary.
- [x] Add failing tests for exact, absent, retired cloud, and unavailable Ollama results.
- [x] Run the focused module through Brigade and confirm RED.
- [x] Implement the smallest typed inspector with per-run caching and no dependencies.
- [x] Run the same Brigade command to GREEN.
- [x] Commit the classifier.

### Task 2: Surface inventory in roster doctor

**Files:** `src/brigade/roster_cmd.py`, `tests/test_roster_cmd.py`

- [x] Add failing doctor tests for exact, fuzzy-resolved, missing, retired, and unavailable seats.
- [x] Run the focused roster-doctor tests through Brigade and confirm RED.
- [x] Add one advisory model-inventory check per supported seat while preserving existing pin and ACP checks.
- [x] Reuse inventory results across repeated seats in the same doctor run.
- [x] Run the same Brigade command to GREEN.
- [x] Commit the doctor integration.

### Task 3: Document the operator contract

**Files:** `docs/technical-guide.md`, `docs/seat-catalog.md`

- [x] Document supported harnesses, the four states, the narrow fuzzy boundary, and warning-only behavior.
- [x] Document the Ollama cloud retirement probe and the unavailable-inventory fallback.
- [x] Commit the documentation.

### Task 4: Verify, review, and publish

**Files:** all changed files

- [x] Run focused inventory and roster-doctor coverage through Brigade.
- [x] Run `./scripts/verify` through Brigade.
- [x] Review the exact diff against every #299 acceptance criterion.
- [x] Request independent review and verify every sendback claim before changing code.
- [x] Repeat focused and full gates after accepted fixes.
- [ ] Push an exact-tree branch, open a ready PR closing #299, monitor CI and review threads, merge only when the head is clean, then run the merged-head gate.
- [ ] Write and lint the #299 memory handoff.
