# Stations reference

Brigade is the hub. Most stations wire an optional standalone tool, installed with `brigade add <station>` and health-checked by `brigade status` and `brigade doctor`. Each external tool is its own repo, independently installable, with no library coupling back into Brigade.

## Selecting stations

Use `brigade profiles list` to see built-in station bundles and `brigade stations list` to see which stations are selected by the default repo profile before installing any sidecar tools. `brigade stations list --json` also shows each managed tool's machine surfaces: doctor JSON, markdown briefs, summary JSON, and verify commands where the tool supports them.

Fresh repo installs use the `repo` profile: core, skills, memory, guard, security, tokens, evidence, and search are selected up front. `brigade init` wires the built-in skills immediately, including `brigade-work` and `ultra-work-scout`, so new Codex users can run the Brigade work loop and broad Scout scoping from the start. External sidecars stay in their own repos and install only when you run `brigade add <station>`.

## The `brigade add` reference

| `brigade add` | Tool | What it does |
|---|---|---|
| `skills` | built-in Scout skills; optional Skillet roster | wires `brigade-work` and `ultra-work-scout` on init; use `skills add escoffier-labs/skillet` after installing the sidecar CLI for the full roster |
| `guard` | embedded content guard, with an optional external checkout override | scans handoffs and content for secrets and PII before anything leaves the machine |
| `tokens` | token-glace | tracks token spend across your harnesses and compacts noisy output |
| `memory` | bootstrap-doctor (optional); memory maintenance is built in | `brigade memory status|lint|compact` plus memory-care for card freshness |
| `pantry` | agentpantry (Go sidecar) | plans and health-checks sealed browser-session sync; never starts source/sink |
| `search` | code-search, graphtrail | local semantic search plus a code-graph CLI for callers, impact, and structural diffs |
| `evidence` | miseledger (Go sidecar) | plans crawl/export and health-checks the local evidence ledger; does not crawl for you |

## Inspecting a station before install

External station repos can publish the same contract in a local `station.json`. Point `brigade add` at that repo to inspect its install command and surfaces without editing Brigade source:

```bash
brigade add ../agentpantry          # inspect station.json
brigade add ../agentpantry --install # run the manifest install command
```

## Verifying a station contract

Before installing a sidecar you can verify its `station.json` contract without running its installer. `brigade stations verify <path>` runs only declared read-only probes, sandboxed and bounded, and returns a machine-readable status. Pass a local repository or manifest path explicitly:

```bash
brigade stations verify ../agentpantry
brigade stations verify ../agentpantry/station.json --json
brigade stations verify ../agentpantry --check-managed
```

### Isolation

Discovery stays passive. `stations verify` never runs a manifest's install argv and executes only declared read-only commands or safe support probes. On POSIX, each process runs without a shell, from the manifest directory, with temporary `HOME` and XDG directories. Brigade terminates the process group on timeout or when combined stdout and stderr exceed 64 KiB. Windows verification fails closed with `unsupported-platform` before process creation because Brigade does not yet provide Job Object containment and a Windows pipe reader. JSON results contain status, exit code, duration, byte counts, timeout and overflow flags, and a bounded detail. They do not contain raw child output.

### Manifest rules

Active executable manifests must resolve every declared binary and verify every surface. A stateful or templated surface needs a support probe with exact `probe_contains` assertions. Executable probes accept only top-level `--help`, `-h`, `--version`, or `version`, plus ASCII subcommand paths ending in `--help` or `-h`. A `skill-roster` uses a manifest-local `verify-exit` probe instead of executable detection. Embedded, deprecated, and historical manifests skip external execution and name their maintained owner. Older v1 manifests still load and appear in discovery, but strict verification fails when required finite timeouts, presentation caps, or probes are absent. JSON surface output uses strict JSON and rejects `NaN` and infinity values.

### Exit codes

Exit 0 means the station contract passed or a non-active lifecycle was skipped. Exit 1 means an active station is unavailable, failed, unbounded, unverified, unsupported on the current platform, or drifted under `--check-managed`. Missing, unreadable, or malformed manifests and CLI misuse exit 2. Managed-catalog drift is advisory without `--check-managed`, so an independently updated sidecar can still prove its local contract. Human-readable output quotes manifest-controlled fields and removes terminal, control, bidi, and format characters from details.
