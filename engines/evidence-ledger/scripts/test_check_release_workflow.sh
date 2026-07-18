#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

POSITIVE_CASES=(
  ".github/workflows/release.yml"
  "testdata/workflows/release/valid-minimal.yml"
)

NEGATIVE_CASES=(
  "commented-contract.yml|moved-to-comment contract"
  "token-extraction.yml|token extraction"
  "weakened-equality.yml|weakened equality"
  "trailing-output.yml|trailing-output acceptance"
  "missing-binary.yml|missing binary loop member"
  "wrong-step.yml|verification script outside named step"
  "noop-bypass.yml|colon no-op bypass"
  "quoted-literal.yml|quoted literal bypass"
  "heredoc-bypass.yml|here-doc bypass"
  "heredoc-quoted-delimiter.yml|quoted here-doc delimiter"
  "heredoc-hyphen-delimiter.yml|hyphenated here-doc delimiter"
  "partial-substring.yml|extra executable line in post step"
  "publish-altered-run.yml|altered publish run script"
  "extra-line-pre.yml|extra executable line in pre step"
  "exit-0-before-contract.yml|exit 0 before contract"
  "false-wrapper.yml|uncalled function wrapper"
  "missing-validator-step.yml|missing validator step"
  "wrong-order.yml|verify steps after publish"
  "missing-pre-step.yml|missing pre-publish step"
  "pre-if-false.yml|if:false on pre step"
  "validator-if-false.yml|if:false on validator step"
  "post-if-false.yml|if:false on post step"
  "publish-if-false.yml|if:false on publish step"
  "pre-continue-on-error.yml|continue-on-error on pre step"
  "validator-continue-on-error.yml|continue-on-error on validator step"
  "post-continue-on-error.yml|continue-on-error on post step"
  "publish-continue-on-error.yml|continue-on-error on publish step"
  "pre-custom-shell.yml|custom shell on pre step"
  "validator-custom-shell.yml|custom shell on validator step"
  "post-custom-shell.yml|custom shell on post step"
  "pre-extra-env.yml|extra env on pre step"
  "validator-extra-env.yml|extra env on validator step"
  "post-extra-env.yml|extra env on post step"
  "publish-extra-env.yml|extra env on publish step"
  "post-missing-env.yml|missing env on post step"
  "pre-added-comment.yml|added comment in pre step"
  "pre-added-noop.yml|added no-op in pre step"
  "pre-blank-line.yml|blank line in pre step"
  "job-if-false.yml|if:false on release job"
  "job-continue-on-error.yml|continue-on-error on release job"
  "job-defaults.yml|defaults on release job"
  "job-strategy.yml|strategy on release job"
  "duplicate-runs-on.yml|duplicate runs-on"
  "duplicate-pre-run.yml|duplicate pre-step run"
  "duplicate-post-gh-token.yml|duplicate post GH_TOKEN"
  "duplicate-step-name.yml|duplicate step name key"
  "anchored-pre-step.yml|anchored pre step"
  "anchored-run.yml|anchored run metadata"
  "job-merge-alias.yml|merge key or alias on release job"
  "duplicate-publish-after-post.yml|second publish after post verification"
  "unnamed-step-after-post.yml|unnamed step after post verification"
  "duplicate-pre-step-name.yml|duplicate pre step name"
  "duplicate-validator-step-name.yml|duplicate validator step name"
  "duplicate-post-step-name.yml|duplicate post step name"
)

run_positive() {
  local target="$1"
  if ! python3 scripts/validate_release_workflow.py "$target"; then
    echo "FAIL: expected acceptance for $target" >&2
    exit 1
  fi
  echo "ok: accepts $target"
}

run_negative() {
  local target="$1"
  local reason="$2"
  if python3 scripts/validate_release_workflow.py "$target" >/dev/null 2>&1; then
    echo "FAIL: expected rejection for $reason ($target)" >&2
    exit 1
  fi
  echo "ok: rejects $reason"
}

for target in "${POSITIVE_CASES[@]}"; do
  run_positive "$target"
done

for entry in "${NEGATIVE_CASES[@]}"; do
  fixture="${entry%%|*}"
  reason="${entry#*|}"
  run_negative "testdata/workflows/release/${fixture}" "$reason"
done

CASE_COUNT=$((${#POSITIVE_CASES[@]} + ${#NEGATIVE_CASES[@]}))
echo "check_release_workflow tests: ok (${CASE_COUNT} cases)"
