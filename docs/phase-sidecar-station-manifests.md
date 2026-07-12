# Sidecar Station Manifest Plan

Goal: give every first-class Brigade sidecar a self-describing station contract and make each active executable contract match Brigade's managed catalog.

Architecture: each sidecar repository owns its root `station.json`. Brigade remains the offline runtime catalog during this slice. Cross-repo verification uses `brigade stations verify <repo> --check-managed`. Content Guard publishes a lifecycle-only `embedded` record because its maintained implementation now ships inside Brigade.

Key tech: JSON, existing `brigade.station.v1` parsing, repository-local lint gates, and Brigade parity verification. No runtime dependency or executable behavior changes.

## Repository Map

- GraphTrail: replace the drifting root manifest with the exact managed install and surfaces.
- MiseLedger: no file change. Verify its existing manifest as the known-good contract.
- Agent Pantry: add an active executable manifest matching Brigade.
- Token Glace: add an active executable manifest matching Brigade.
- Skillet: add an active `skill-roster` manifest and validate its static fields in the existing linter.
- Content Guard: add an `embedded` lifecycle record owned by `brigade-cli`.
- Brigade: record execution evidence in this plan. Snapshot generation remains the next control-plane slice.

## Task 1: Capture the failing contract state

- [x] Run GraphTrail parity through Brigade and expect exit 1 with drift in `install` and `surfaces`.
- [x] Run Agent Pantry, Token Glace, Skillet, and Content Guard verification through Brigade and expect exit 2 because `station.json` is absent.
- [x] Run MiseLedger verification with `--check-managed` and expect exit 0.

Use the installed Brigade development CLI:

```bash
/home/clawdbot/repos/brigade/.venv/bin/brigade stations verify . --check-managed
```

## Task 2: Repair GraphTrail

**Files:**

- Modify: `/home/clawdbot/.cache/codex/worktrees/upstream-integration/graphtrail/station.json`

- [x] Replace the manifest with:

```json
{
  "schema": "brigade.station.v1",
  "name": "graphtrail",
  "station": "search",
  "summary": "local code-graph CLI: callers, callees, impact, context briefs, and structural diffs",
  "lifecycle": "active",
  "tools": [
    {
      "name": "graphtrail",
      "kind": "executable",
      "command": "graphtrail",
      "summary": "local code-graph CLI: callers, callees, impact, context briefs, and structural diffs",
      "install": ["cargo", "install", "graphtrail"],
      "surfaces": [
        {
          "kind": "brief-markdown",
          "command": ["graphtrail", "context", "<task>", "--markdown"],
          "read_only": true,
          "timeout_seconds": 10,
          "max_chars": 4000,
          "probe": ["graphtrail", "context", "--help"],
          "probe_contains": ["--markdown"]
        },
        {
          "kind": "verify-exit",
          "command": ["graphtrail", "--version"],
          "read_only": true,
          "timeout_seconds": 10
        },
        {
          "kind": "doctor-json",
          "command": ["graphtrail", "doctor", "--json"],
          "read_only": true,
          "timeout_seconds": 30,
          "probe": ["graphtrail", "doctor", "--help"],
          "probe_contains": ["--json"]
        }
      ]
    }
  ]
}
```

- [x] Run `brigade stations verify . --check-managed` through Brigade and expect exit 0.
- [x] Run the GraphTrail completion gate and commit `fix: align Brigade station contract`.

## Task 3: Add Agent Pantry

**Files:**

- Create: `/home/clawdbot/.cache/codex/worktrees/upstream-integration/agentpantry/station.json`

- [x] Add:

```json
{
  "schema": "brigade.station.v1",
  "name": "agentpantry",
  "station": "pantry",
  "summary": "browser session auth sync through a process-boundary Go binary",
  "lifecycle": "active",
  "tools": [
    {
      "name": "agentpantry",
      "kind": "executable",
      "command": "agentpantry",
      "summary": "browser session auth sync from source to sink",
      "install": ["go", "install", "github.com/escoffier-labs/agentpantry/cmd/agentpantry@latest"],
      "surfaces": [
        {
          "kind": "doctor-json",
          "command": ["agentpantry", "doctor", "--json", "--no-net"],
          "read_only": false,
          "timeout_seconds": 10,
          "probe": ["agentpantry", "doctor", "--help"],
          "probe_contains": ["-json", "-no-net"]
        },
        {
          "kind": "summary-json",
          "command": ["agentpantry", "inventory", "--json"],
          "read_only": true,
          "timeout_seconds": 10,
          "max_chars": 4000,
          "probe": ["agentpantry", "inventory", "--help"],
          "probe_contains": ["-json"]
        },
        {
          "kind": "verify-exit",
          "command": ["agentpantry", "version", "--json"],
          "read_only": true,
          "timeout_seconds": 10
        }
      ]
    }
  ]
}
```

- [x] Run parity through Brigade and expect exit 0.
- [x] Run `./scripts/verify` through Brigade and commit `feat: add Brigade station contract`.

## Task 4: Add Token Glace

**Files:**

- Create: `/home/clawdbot/.cache/codex/worktrees/upstream-integration/token-glace/station.json`

- [x] Add:

```json
{
  "schema": "brigade.station.v1",
  "name": "token-glace",
  "station": "tokens",
  "summary": "output compaction and token-use summaries",
  "lifecycle": "active",
  "tools": [
    {
      "name": "token-glace",
      "kind": "executable",
      "command": "token-glace",
      "summary": "output compaction through host hooks",
      "install": [
        "npm",
        "install",
        "-g",
        "https://github.com/escoffier-labs/token-glace/releases/download/v0.8.3/token-glace-v0.8.3.tar.gz"
      ],
      "surfaces": [
        {
          "kind": "doctor-json",
          "command": ["token-glace", "doctor", "hooks", "--format", "json"],
          "read_only": true,
          "timeout_seconds": 30
        },
        {
          "kind": "summary-json",
          "command": ["token-glace", "stats", "--format", "json", "--timezone", "utc"],
          "read_only": true,
          "timeout_seconds": 30,
          "max_chars": 4000,
          "probe": ["token-glace", "--help"],
          "probe_contains": ["--format", "--timezone"]
        },
        {
          "kind": "verify-exit",
          "command": ["token-glace", "verify"],
          "read_only": true,
          "timeout_seconds": 60
        }
      ]
    }
  ]
}
```

- [x] Run parity through Brigade and expect exit 0.
- [x] Run `pnpm verify` through Brigade and commit `feat: add Brigade station contract`.

## Task 5: Add Skillet

**Files:**

- Create: `/home/clawdbot/.cache/codex/worktrees/upstream-integration/skillet/station.json`
- Modify: `/home/clawdbot/.cache/codex/worktrees/upstream-integration/skillet/tests/lint-skills.sh`

- [x] Add a failing static linter check that requires:
  - schema `brigade.station.v1`
  - name `skillet`
  - station `skills`
  - lifecycle `active`
  - one tool with kind `skill-roster`
  - install `npx skills add escoffier-labs/skillet`
  - one `verify-exit` probe targeting `tests/lint-skills.sh`

- [x] Run `./tests/lint-skills.sh` through Brigade and watch it fail because the manifest is absent.

- [x] Add this manifest:

```json
{
  "schema": "brigade.station.v1",
  "name": "skillet",
  "station": "skills",
  "summary": "reviewed portable agent skill roster",
  "lifecycle": "active",
  "tools": [
    {
      "name": "skillet",
      "kind": "skill-roster",
      "summary": "portable skill packages for supported coding harnesses",
      "install": ["npx", "skills", "add", "escoffier-labs/skillet"],
      "surfaces": [
        {
          "kind": "verify-exit",
          "probe": ["bash", "tests/lint-skills.sh"],
          "probe_contains": ["[ok] catalog (36 skills)"],
          "timeout_seconds": 60
        }
      ]
    }
  ]
}
```

The linter validates static JSON only. It must not invoke `brigade stations verify`, because that verifier executes the linter as the skill-roster probe.

- [x] Run the linter and Brigade parity to green.
- [x] Commit `feat: add Brigade station contract`.

## Task 6: Mark Content Guard embedded

**Files:**

- Create: `/home/clawdbot/.cache/codex/worktrees/upstream-integration/content-guard/station.json`

- [x] Add:

```json
{
  "schema": "brigade.station.v1",
  "name": "content-guard",
  "station": "guard",
  "summary": "historical standalone package now maintained inside brigade-cli",
  "lifecycle": "embedded",
  "owner": "brigade-cli",
  "tools": []
}
```

- [x] Run Brigade verification and expect `embedded-skip` with exit 0.
- [x] Run `./scripts/verify` through Brigade and commit `docs: record embedded Brigade lifecycle`.

## Task 7: Cross-repo closeout

- [x] Re-run `brigade stations verify . --check-managed` for GraphTrail, MiseLedger, Agent Pantry, Token Glace, and Skillet. All 5 active contracts must exit 0.
- [x] Re-run Content Guard verification. It must exit 0 with `embedded-skip`.
- [x] Record exact repository commits and verification receipts in the ecosystem closeout.

## Execution Evidence

| Repository | Commit | Contract receipt | Repository gate receipt |
| --- | --- | --- | --- |
| GraphTrail | `e51ed5d` | `20260712-210024-work-verify-6afabd` | `20260712-210040-work-verify-589fea`, `20260712-210058-work-verify-a35c15`, `20260712-210109-work-verify-696bb4`, `20260712-210118-work-verify-45e304` |
| MiseLedger | unchanged | `20260712-205916-work-verify-b208bd` | unchanged |
| Agent Pantry | `d9470b1` | `20260712-210024-work-verify-efbc13` | `20260712-210039-work-verify-361c94` |
| Token Glace | `44db23d` | `20260712-210024-work-verify-ae323b` | `20260712-210040-work-verify-1ecf42` |
| Skillet | `74520a2` | `20260712-210024-work-verify-d1f2db` | `20260712-210040-work-verify-0e53d6` |
| Content Guard | `87e095e` | `20260712-210024-work-verify-9b81e9` | `20260712-210039-work-verify-888db3` |
