# GraphTrail Audit Expedite Plan

Goal: clear audit findings 1, 2, 6, 7, 8, 9, and 10 on an isolated branch while preserving the CLI, MCP, schema, and default no-network contracts.

Architecture: repository-safety and HTTP-response bounds stay local to existing build and Code Search boundaries. MCP metadata moves to one registry without changing JSON shapes. The sync façade remains stable while walking, repository policy, persistence, and resolution move behind focused store modules. Dead-code output gains evidence-aware confidence without claiming dynamic-language certainty.

Execute task by task. Keep every behavior change test-first and commit each task separately.

## File map

- `Dockerfile`, `.dockerignore`, `.gitignore`: build-context and local-state boundaries.
- `tests/repository_contract.rs`: repository-level safety contracts.
- `src/adapters/codesearch.rs`: bounded Code Search response decoding.
- `src/mcp.rs`, `tests/mcp.rs`: single MCP tool registry and complete tool-call contracts.
- `.github/workflows/ci.yml`: Rust 1.85 MSRV gate.
- `README.md`, `AGENTS.md`, `CHANGELOG.md`, `.github/ISSUE_TEMPLATE/*.yml`: current contracts and release notes.
- `src/store/{sync,walk,persist,repo_policy,resolve}.rs`: focused sync internals behind the existing public façade.
- `src/query/health.rs`, `src/model.rs`, `tests/analysis.rs`: ranked dead-code candidates with explicit confidence/reason fields.

### Task 1: Secure the Docker context

Files: `Dockerfile`, `.dockerignore`, `.gitignore`, `tests/repository_contract.rs`.

- [x] Add a failing test that reads `Dockerfile` and `.dockerignore`, asserts `COPY . .` is absent, and asserts exclusions for `.brigade`, `.codex`, `memory`, `.mcp.json`, `.env`, PEM/key patterns.
- [x] Run `cargo test --test repository_contract docker_context_excludes_private_state`; expect failure on `COPY . .` and missing exclusions.
- [x] Replace `COPY . .` with selective manifest/source copies and add the exclusions. Ignore `.codex/` in git.
- [x] Re-run the focused test; expect pass.
- [x] Commit `fix(docker): exclude private local state from build context`.

Review fix: `.dockerignore` now denies all context inputs and re-includes only the Dockerfile, Cargo manifests, and `src/`; the Docker context fell from 973.3 kB to 322 kB.

### Task 2: Bound Code Search responses

Files: `src/adapters/codesearch.rs`.

- [x] Add `oversized_response_is_rejected_before_json_decode`, backed by the existing mock TCP server, that serves more than `MAX_CODE_SEARCH_RESPONSE_BYTES` and asserts the size-limit error.
- [x] Run `cargo test --all-features adapters::codesearch::tests::oversized_response_is_rejected_before_json_decode`; expect the current unbounded decoder not to return the size-limit error.
- [x] Read at most `MAX_CODE_SEARCH_RESPONSE_BYTES + 1` through `Read::take`, reject overflow, then deserialize with `serde_json::from_slice`.
- [x] Re-run the focused test and existing Code Search tests; expect pass.
- [x] Commit `fix(codesearch): cap response bodies before decoding`.

### Task 3: Centralize and contract-test MCP tools

Files: `src/mcp.rs`, `tests/mcp.rs`.

- [x] Add a unit test for a new `tool_specs()` registry that asserts unique names, full default membership, and refresh policy, plus integration tests for successful `dead_code`, `cycles`, and `affected` calls and invalid `affected.files`, `limit`, and `depth` cases.
- [x] Run the focused registry unit test; expect a compile failure because `tool_specs()` does not exist.
- [x] Introduce one `ToolSpec` registry that owns tool names, schemas, refresh capability, and validation/dispatch identity. Generate `tools/list` and policy checks from it without changing response schemas.
- [x] Run `cargo test --all-features --test mcp`; expect all MCP tests pass.
- [x] Commit `refactor(mcp): define tool contracts from one registry`.

### Task 4: Enforce supported toolchain and update project guidance

Files: `.github/workflows/ci.yml`, `README.md`, `AGENTS.md`, `CHANGELOG.md`, `.github/ISSUE_TEMPLATE/bug.yml`, `.github/ISSUE_TEMPLATE/feature.yml`.

- [x] Add a failing repository-contract test asserting an MSRV job pins `1.85`, README describes `refresh: true` as an opt-in index write, and AGENTS documents Brigade verification/handoffs.
- [x] Run the focused contract test; expect failure.
- [x] Add the MSRV `cargo check --locked --all-features` job; update README trust boundary, Rust prerequisite, current stats example; update AGENTS module map and Brigade loop; record schema v5/v6 and analysis commands in `Unreleased`; modernize issue forms.
- [x] Re-run the contract test and `cargo fmt --check`; expect pass.
- [x] Commit `docs: align agent and release contracts with current graphtrail`.

Review fix: `Cargo.lock` pins `idna_adapter` to 1.2.1, `icu_collections`, `icu_locale_core`, `icu_normalizer`, `icu_normalizer_data`, and `icu_provider` to 2.1.1, plus `icu_properties` and `icu_properties_data` to 2.1.2. Source let chains were rewritten to Rust 1.85-compatible control flow.

### Task 5: Split sync internals without changing behavior

Files: `src/store/sync.rs`; create `src/store/walk.rs`, `src/store/persist.rs`, `src/store/repo_policy.rs`, `src/store/resolve.rs`; modify `src/store/mod.rs`.

- [x] Record the existing public exports and run `cargo test --all-features incremental resolution`; expect pass as baseline.
- [x] Move repository walking/ignore policy first and run `cargo check`; expect an initial unresolved-module compile failure before wiring `mod` imports.
- [x] Move persistence and call resolution in separate mechanical steps, keeping `sync_repo`, `sync_repo_force`, and output types stable.
- [x] Run `cargo test --all-features`; expect 143 or more tests pass.
- [x] Commit `refactor(sync): separate walking persistence and resolution`.

Review fix: extracted sync internals use store-only visibility; only `guard_unsafe_root` and `current_git_branch` remain crate-visible for existing consumers.

### Task 6: Improve dead-code candidate precision

Files: `src/query/health.rs`, `src/model.rs`, `tests/analysis.rs`.

- [x] Add fixtures showing a trait method, exported/public entry point, and callback-style symbol must not be presented as high-confidence dead code. Assert each result includes `confidence` and `reason`.
- [x] Run `cargo test --test analysis dead_code`; expect failure because the fields and classification are absent.
- [x] Add conservative classification from symbol kind/signature/container and graph evidence. Keep uncertain candidates, mark them low confidence, and sort high-confidence candidates first. Preserve the existing caveat and total count.
- [x] Re-run analysis tests and the full suite; expect pass.
- [x] Commit `feat(analysis): rank dead-code candidates by confidence`.

Review fix: top-level JavaScript/TypeScript functions remain low-confidence because
export-list visibility is not persisted, and plain-text CLI rows show each
candidate's confidence and reason.

### Task 7: Final verification

- [x] Run through Brigade: `cargo fmt --check`, `cargo clippy --all-targets --all-features -- -D warnings`, `cargo test --all-features`, `cargo build --release`.
- [x] Run a Docker build if the daemon is available; otherwise verify the context contract test and report the blocker.
- [x] Re-run `line-check` against the branch and confirm the addressed findings are gone.

Final verification receipt: `.brigade/work/verify-runs/20260709-205755-work-verify-687edf/receipt.json`. The exact Rust 1.85 locked check, 158 tests, release build, and Docker build passed; the independent review's four Important findings were fixed and re-reviewed.
