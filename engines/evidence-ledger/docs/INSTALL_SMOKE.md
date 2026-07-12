# Install Smoke

This smoke proves the public release installer can install MiseLedger, then import fixture-only data into a temporary local archive.

It does not import private session logs.

```bash
tmp_home="$(mktemp -d)"
export HOME="$tmp_home"
export XDG_CONFIG_HOME="$tmp_home/.config"
export XDG_DATA_HOME="$tmp_home/.local/share"
export XDG_CACHE_HOME="$tmp_home/.cache"
export BINDIR="$tmp_home/bin"
export PATH="$BINDIR:$PATH"

MISELEDGER_VERSION=v0.5.0
curl -fsSLO https://raw.githubusercontent.com/escoffier-labs/miseledger/v0.5.0/install.sh
cat install.sh  # review before running
MISELEDGER_VERSION="$MISELEDGER_VERSION" sh install.sh

miseledger version

miseledger init
miseledger doctor --mcp --json

repo=/path/to/miseledger
miseledger import adapter "$repo/testdata/adapters/discrawl.fixture.jsonl" --source discrawl --json
miseledger import codex "$repo/testdata/harnesses/codex-session.fixture.jsonl" --json
miseledger import openclaw "$repo/testdata/harnesses/openclaw-session.fixture.jsonl" --json
miseledger import claude "$repo/testdata/harnesses/claude-project.fixture.jsonl" --json
miseledger import hermes "$repo/testdata/harnesses/session_hermes-demo.fixture.json" --json
miseledger import hermes "$repo/testdata/harnesses/hermes-trajectory.fixture.jsonl" --json

miseledger status --json
miseledger stats --json
miseledger relations backfill --json
miseledger compact --json
miseledger doctor --archive --json
miseledger scans list --json
miseledger search "adapter contract" --json
miseledger evidence "Claude native import" --project miseledger --json
miseledger explain "adapter contract" --json
```

Expected results:

- `install.sh` installs release binaries and verifies sha256 checksums against the release `checksums.txt`
- downloaded release assets can be provenance-checked with `gh attestation verify <asset> --repo escoffier-labs/miseledger`
- `miseledger doctor --mcp --json` returns `ok: true`
- `miseledger status --json` reports FTS as `ok`
- `miseledger stats --json` reports nonzero source and item totals
- `miseledger compact --json` returns `ok: true`
- `miseledger doctor --archive --json` returns `ok: true`
- search returns at least one result
- evidence returns `untrusted_context: true` and a stable bundle ID
- evidence result `artifacts` fields encode as arrays, including empty arrays
