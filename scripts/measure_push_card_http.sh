#!/usr/bin/env bash
# Same benchmark and build settings for a git revision or the working tree.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
export CARGO_TARGET_DIR="${TMPDIR:-/tmp}/brigade-push-card-http-target"
if [[ -n "${PUSH_MEASURE_REV:-}" ]]; then
    checkout="$(mktemp -d)"
    trap 'rm -rf "$checkout"' EXIT
    git -C "$root" archive "$PUSH_MEASURE_REV" | tar -x -C "$checkout"
    cp "$root/engines/hub/src/memory/push_latency_measure.rs" "$checkout/engines/hub/src/memory/"
    cp "$root/engines/hub/src/memory_response.rs" "$checkout/engines/hub/src/"
    printf '#[path = "../memory_response.rs"]\nmod response;\n#[path = "push_latency_measure.rs"]\nmod latency;\n' > "$checkout/engines/hub/src/memory/push_budget_tests.rs"
    printf '\n#[cfg(test)]\nmod push_budget_tests;\n' >> "$checkout/engines/hub/src/memory/mod.rs"
    cd "$checkout/engines/hub"
else
    cd "$root/engines/hub"
fi
cargo test --locked --all-features --lib push_http_latency_measurement -- --ignored --nocapture --test-threads=1
