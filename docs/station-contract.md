<a id="stations-reference"></a>

# Components reference

`brigade components list|discover|verify` is unreleased, targeted for the next
0.31.x release (preferably 0.31.0 if still unreleased at integration). Stable
0.30 users should use `brigade stations list|discover|verify` instead. The
`brigadestack` bridge accepts the same commands when backed by the new CLI.

`stations` remains a supported quiet alias with identical arguments and exit
codes, no warning, and no removal date. Root help advertises only `components`.
Explicit `stations --help` shows canonical component help. Argparse invalid-choice
errors and generated bash, zsh, and fish completions still include the supported
alias because they enumerate parser choices.

The on-disk filename `station.json`, schemas `brigade.station.v1` and
`brigade.stations.verify.v1`, and JSON keys `station`, `stations`,
`selected_stations`, `optional_stations`, and `missing_stations` remain unchanged.
Discovery JSON retains `docs.list: "brigade stations list"`. Human guidance uses
`brigade components list`. Stored identifiers and Python imports are unchanged.
Profile summary prose uses components.

Brigade is the hub. Most components wire an optional standalone tool, installed with `brigade add <component>` and health-checked by `brigade status` and `brigade doctor`. External component tools are independently installable, with no library coupling back into Brigade. The code-graph and evidence engines live in this repository under `engines/` and are installed by `brigade setup` from the release manifest.

<a id="selecting-stations"></a>

## Selecting components

Use `brigade profiles list` to see built-in component bundles and `brigade components list` to see which components are selected by the default repo profile before installing any component tools. `brigade components list --json` also shows each managed tool's machine surfaces: doctor JSON, markdown briefs, summary JSON, and verify commands where the tool supports them.

Fresh repo installs use the `repo` profile: core, skills, memory, guard, security, tokens, evidence, and search are selected up front. `brigade init` wires the built-in skills immediately, including `brigade-work` and `ultra-work-scout`, so new Codex users can run the Brigade work loop and broad Scout scoping from the start. External components stay in their own repos and install only when you run `brigade add <component>`.

## The `brigade add` reference

| `brigade add` | Tool | What it does |
|---|---|---|
| `skills` | built-in Scout skills; optional Skillet roster | wires `brigade-work` and `ultra-work-scout` on init; use `skills add escoffier-labs/skillet` after installing the sidecar CLI for the full roster |
| `guard` | embedded content guard, with an optional external checkout override | scans handoffs and content for secrets and PII before anything leaves the machine |
| `tokens` | token-glace | tracks token spend across your harnesses and compacts noisy output |
| `memory` | bootstrap-doctor (optional); memory maintenance is built in | `brigade memory status|lint|compact` plus memory-care for card freshness |
| `pantry` | agentpantry (Go sidecar) | plans and health-checks sealed browser-session sync; never starts source/sink |
| `search` | code-search; the code map engine is installed by `brigade setup` (historically GraphTrail) | local semantic search plus the code map for callers, impact, and structural diffs (`brigade code …`) |
| `evidence` | evidence log engine installed by `brigade setup` (historically MiseLedger) | runs the local evidence engine for `brigade evidence crawl|search`; crawl/export plans are review-only, and Brigade neither starts daemons nor uploads data |

<a id="inspecting-a-station-before-install"></a>

## Inspecting a component before install

External component repos can publish the same contract in a local `station.json`. Point `brigade add` at that repo to inspect its install command and surfaces without editing Brigade source:

```bash
brigade add ../agentpantry          # inspect station.json
brigade add ../agentpantry --install # run the manifest install command
```

<a id="verifying-a-station-contract"></a>

## Verifying a component contract

Before installing a sidecar you can verify its `station.json` contract without running its installer. `brigade components verify <path>` runs only declared read-only probes, sandboxed and bounded, and returns a machine-readable status. Pass a local repository or manifest path explicitly:

```bash
brigade components verify ../agentpantry
brigade components verify ../agentpantry/station.json --json
brigade components verify ../agentpantry --check-managed
```

### Isolation

Discovery stays passive. `components verify` never runs a manifest's install argv and executes only declared read-only commands or safe support probes. On POSIX, each process runs without a shell, from the manifest directory, with temporary `HOME` and XDG directories. Brigade terminates the process group on timeout or when combined stdout and stderr exceed 64 KiB. Windows verification fails closed with `unsupported-platform` before process creation because Brigade does not yet provide Job Object containment and a Windows pipe reader. JSON results contain status, exit code, duration, byte counts, timeout and overflow flags, and a bounded detail. They do not contain raw child output.

### Manifest rules

Active executable manifests must resolve every declared binary and verify every surface. A stateful or templated surface needs a support probe with exact `probe_contains` assertions. Executable probes accept only top-level `--help`, `-h`, `--version`, or `version`, plus ASCII subcommand paths ending in `--help` or `-h`. A `skill-roster` uses a manifest-local `verify-exit` probe instead of executable detection. Embedded, deprecated, and historical manifests skip external execution and name their maintained owner. Older v1 manifests still load and appear in discovery, but strict verification fails when required finite timeouts, presentation caps, or probes are absent. JSON surface output uses strict JSON and rejects `NaN` and infinity values.

### Exit codes

Exit 0 means the component contract passed or a non-active lifecycle was skipped. Exit 1 means an active component is unavailable, failed, unbounded, unverified, unsupported on the current platform, or drifted under `--check-managed`. Missing, unreadable, or malformed manifests and CLI misuse exit 2. Managed-catalog drift is advisory without `--check-managed`, so an independently updated sidecar can still prove its local contract. Human-readable output quotes manifest-controlled fields and removes terminal, control, bidi, and format characters from details.
