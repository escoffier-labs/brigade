# Standalone Compatibility Install Smoke

This smoke covers the standalone public release installer as a one-release compatibility fallback. It is not general installation guidance. Brigade-managed engine smoke starts with `brigade setup`, which verifies the unified release manifest before installing brigade-evidence.

It does not import private session logs.

```bash
tmp_home="$(mktemp -d)"
export HOME="$tmp_home"
export XDG_CONFIG_HOME="$tmp_home/.config"
export XDG_DATA_HOME="$tmp_home/.local/share"
export XDG_CACHE_HOME="$tmp_home/.cache"
export BINDIR="$tmp_home/bin"
export PATH="$BINDIR:$PATH"

MISELEDGER_VERSION=v0.6.0
curl -fsSLO https://raw.githubusercontent.com/escoffier-labs/miseledger/v0.6.0/install.sh
cat install.sh  # review before running
MISELEDGER_VERSION="$MISELEDGER_VERSION" sh install.sh

brigade-evidence version

brigade-evidence init
brigade-evidence doctor --mcp --json

repo=/path/to/brigade/engines/evidence-ledger
brigade-evidence import adapter "$repo/testdata/adapters/discrawl.fixture.jsonl" --source discrawl --json
brigade-evidence import codex "$repo/testdata/harnesses/codex-session.fixture.jsonl" --json
brigade-evidence import openclaw "$repo/testdata/harnesses/openclaw-session.fixture.jsonl" --json
brigade-evidence import claude "$repo/testdata/harnesses/claude-project.fixture.jsonl" --json
brigade-evidence import hermes "$repo/testdata/harnesses/session_hermes-demo.fixture.json" --json
brigade-evidence import hermes "$repo/testdata/harnesses/hermes-trajectory.fixture.jsonl" --json
brigade-evidence import grok "$repo/testdata/harnesses/grok-sessions.fixture" --json

brigade-evidence status --json
brigade-evidence stats --json
brigade-evidence relations backfill --json
brigade-evidence compact --json
brigade-evidence doctor --archive --json
brigade-evidence scans list --json
brigade-evidence search "adapter contract" --json
brigade-evidence evidence "Claude native import" --project brigade-evidence --json
brigade-evidence explain "adapter contract" --json
```

Expected results:

- `install.sh` installs release binaries and verifies sha256 checksums against the release `checksums.txt`
- downloaded release assets can be provenance-checked with `gh attestation verify <asset> --repo escoffier-labs/brigade-evidence`
- `brigade-evidence doctor --mcp --json` returns `ok: true`
- `brigade-evidence status --json` reports FTS as `ok`
- `brigade-evidence stats --json` reports nonzero source and item totals
- `brigade-evidence compact --json` returns `ok: true`
- `brigade-evidence doctor --archive --json` returns `ok: true`
- search returns at least one result
- evidence returns `untrusted_context: true` and a stable bundle ID
- eligible evidence result `artifacts` fields encode as arrays, including empty arrays. Quarantined results contain only an opaque `id`, `eligibility_status`, and `reason_code`

The archive smoke covers Cursor's current SQLite reader in Go tests because the shell smoke does not require the external `sqlite3` program. The fixture database is created in a temporary test directory from `testdata/harnesses/cursor-conversations.fixture.sql`.
