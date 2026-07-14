# Station contract verification

Sidecar "stations" declare their install command and machine surfaces in a `station.json` manifest. `brigade stations verify` checks that contract **without running the station's installer**, so you can vet a sidecar before you install it. Pass a local repository or manifest path explicitly:

```bash
brigade stations verify ../agentpantry
brigade stations verify ../agentpantry/station.json --json
brigade stations verify ../agentpantry --check-managed
```

## Isolation

Discovery stays passive. `stations verify` never runs a manifest's install argv and executes only declared read-only commands or safe support probes. On POSIX, each process runs without a shell, from the manifest directory, with temporary `HOME` and XDG directories. Brigade terminates the process group on timeout or when combined stdout and stderr exceed 64 KiB. Windows verification fails closed with `unsupported-platform` before process creation because Brigade does not yet provide Job Object containment and a Windows pipe reader. JSON results contain status, exit code, duration, byte counts, timeout and overflow flags, and a bounded detail. They do not contain raw child output.

## Manifest rules

Active executable manifests must resolve every declared binary and verify every surface. A stateful or templated surface needs a support probe with exact `probe_contains` assertions. Executable probes accept only top-level `--help`, `-h`, `--version`, or `version`, plus ASCII subcommand paths ending in `--help` or `-h`. A `skill-roster` uses a manifest-local `verify-exit` probe instead of executable detection. Embedded, deprecated, and historical manifests skip external execution and name their maintained owner. Older v1 manifests still load and appear in discovery, but strict verification fails when required finite timeouts, presentation caps, or probes are absent. JSON surface output uses strict JSON and rejects `NaN` and infinity values.

## Exit codes

Exit 0 means the station contract passed or a non-active lifecycle was skipped. Exit 1 means an active station is unavailable, failed, unbounded, unverified, unsupported on the current platform, or drifted under `--check-managed`. Missing, unreadable, or malformed manifests and CLI misuse exit 2. Managed-catalog drift is advisory without `--check-managed`, so an independently updated sidecar can still prove its local contract. Human-readable output quotes manifest-controlled fields and removes terminal, control, bidi, and format characters from details.
