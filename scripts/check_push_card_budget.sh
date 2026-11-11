#!/usr/bin/env bash
# Focused hub checks for the bounded push-card negative path.
set -euo pipefail
export CARGO_TARGET_DIR="${TMPDIR:-/tmp}/brigade-push-card-2443-target"
cd "$(dirname "$0")/../engines/hub"
cargo fmt --check
cargo clippy --locked --all-targets --all-features -- -D warnings
cargo test --locked --all-features push_budget_tests -- --nocapture --test-threads=1
cargo test --locked --all-features push_card_scope_discovery_sees_only_eligible_copies -- --test-threads=1
cargo test --locked --all-features push_withheld_tests -- --test-threads=1
cargo check --locked --no-default-features
