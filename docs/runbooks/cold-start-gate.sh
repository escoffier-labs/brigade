#!/usr/bin/env bash
# Cold-start gate: the documented stranger's journey, executed literally.
# Fails loudly if any step a new user copy-pastes from the README or
# docs/first-10-minutes.md stops working. Complements (does not replace)
# the agent-driven cold-start scenarios in docs/cold-start-testing.md.
set -euo pipefail

REPO_DIR="${1:-$PWD}"
SANDBOX="$(mktemp -d -t brigade-cold-start-XXXXXX)"
trap 'rm -rf "$SANDBOX"' EXIT
echo "sandbox: $SANDBOX"

# A stranger's machine has no surprising global git config; ours might (a
# global `.claude/` gitignore correctly trips the template-shadow check).
touch "$SANDBOX/gitconfig"
export GIT_CONFIG_GLOBAL="$SANDBOX/gitconfig"
export GIT_CONFIG_SYSTEM=/dev/null
git config --file "$GIT_CONFIG_GLOBAL" user.email "cold-start@example.invalid"
git config --file "$GIT_CONFIG_GLOBAL" user.name "cold-start-gate"

pipx install --force "$REPO_DIR" >/dev/null
echo "installed: $(brigade --version)"

cd "$SANDBOX"
git init -q -b main repo
cd repo
echo 'print("hello")' > app.py

# expect <pattern> <cmd...>: run, capture, assert pattern present (SIGPIPE-safe)
expect() {
  local pattern="$1"; shift
  local out
  out="$("$@")"
  if ! grep -q "$pattern" <<< "$out"; then
    echo "FAIL: expected '$pattern' from: $*" >&2
    echo "$out" | tail -20 >&2
    exit 1
  fi
}
# refuse <pattern> <cmd...>: run, capture, assert pattern absent
refuse() {
  local pattern="$1"; shift
  local out
  out="$("$@")"
  if grep -q "$pattern" <<< "$out"; then
    echo "FAIL: forbidden '$pattern' from: $*" >&2
    echo "$out" | grep "$pattern" | head -5 >&2
    exit 1
  fi
}

# README install block, verbatim shape
expect "status: ok" brigade operator quickstart --target . --harnesses codex,claude
expect "ready: yes" brigade operator doctor --target . --profile local-operator

# README handoff example, verbatim shape
expect "lint: ok" brigade handoff draft --target . --inbox codex \
  --title "What changed" \
  --summary "Short note future agents should know." \
  --content "The durable note itself goes here."
expect "\[ok\]" brigade handoff lint --target .
refuse "\[fail\]" brigade handoff doctor --target .

# first-10-minutes health checks
expect "findings: 0" brigade security scan --target . --output-dir .brigade/security/latest
expect "ready: yes" brigade operator verify-harness --target . --harness codex
expect "ready: yes" brigade operator verify-harness --target . --harness claude

# gitignore regression guard (the 0.9.0 clobber class)
grep -q ".codex/memory-handoffs/\*" .gitignore
grep -q ".claude/memory-handoffs/\*" .gitignore

echo "cold-start gate: PASS"
