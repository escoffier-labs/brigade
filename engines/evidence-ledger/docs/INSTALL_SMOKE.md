# Install Smoke

This smoke proves the public release installers can install Logspine, AgentTrail, and SourceHarvest, then import fixture-only data into a temporary local archive.

It does not import private session logs.

```bash
tmp_home="$(mktemp -d)"
export HOME="$tmp_home"
export XDG_CONFIG_HOME="$tmp_home/.config"
export XDG_DATA_HOME="$tmp_home/.local/share"
export XDG_CACHE_HOME="$tmp_home/.cache"
export BINDIR="$tmp_home/bin"
export PATH="$BINDIR:$PATH"

curl -fsSL https://raw.githubusercontent.com/solomonneas/logspine/HEAD/install.sh | sh
curl -fsSL https://raw.githubusercontent.com/solomonneas/agenttrail/HEAD/install.sh | sh
curl -fsSL https://raw.githubusercontent.com/solomonneas/sourceharvest/HEAD/install.sh | sh

spine version
agenttrail version
sourceharvest version

spine init
spine doctor --mcp --json

repo=/path/to/logspine
spine import adapter "$repo/testdata/adapters/discrawl.fixture.jsonl" --source discrawl --json
spine import codex "$repo/testdata/harnesses/codex-session.fixture.jsonl" --json
spine import openclaw "$repo/testdata/harnesses/openclaw-session.fixture.jsonl" --json
spine import claude "$repo/testdata/harnesses/claude-project.fixture.jsonl" --json

spine status --json
spine scans list --json
spine search "adapter contract" --json
spine evidence "Claude native import" --project logspine --json
```

Expected results:

- release binaries install with checksum verification
- `spine doctor --mcp --json` returns `ok: true`
- `spine status --json` reports FTS as `ok`
- search returns at least one result
- evidence returns `untrusted_context: true`
- evidence result `artifacts` fields encode as arrays, including empty arrays

