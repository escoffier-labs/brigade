# Keeping the feature map honest

The feature map is only useful while it matches the CLI. Commands get added,
flags get renamed, and a drive that worked last month can stop working without
any test noticing. This page is the loop that catches that drift, plus the
rules for what counts as proof. `control-brigade.py audit` is the read-only
check behind it, and `tests/test_verify_brigade_audit.py` runs that check in CI.

## When to run the loop

Run it when a change touches the CLI surface: a new command or subcommand, a
renamed or removed flag, a changed output field a feature file asserts on, or a
change to a helper subcommand. Also run it before you cite this skill as proof
in a PR body.

## The loop

1. **Audit.** Run the audit against the checkout's own CLI:

   ```bash
   registry/skills/verify-brigade/control-brigade.py audit
   ```

   It prints one JSON object and exits 0 when the map is consistent, 3 when it
   is not, and 1 when it cannot read the CLI at all. Read `failures` first.
   Then read `uncovered`, `uncovered_flags`, `stale`, and `paused`.

2. **Find the drift.** The audit reports four kinds:
   - `broken`: an active feature names a command the CLI no longer has, or one
     whose `--help` fails. Its documented steps cannot work. This fails the
     audit.
   - `uncovered` with `known_gap: false`: a command that exists, that no
     feature covers, and that the known-gap list below does not name. This
     fails the audit. Either write a feature for it or add it to the gap list
     with a reason.
   - `uncovered_flags`: flags in a covered command's `--help` that its feature
     file never mentions. This is a report, not a failure. Read it when you
     touch that feature.
   - `stale`: an active feature with no recorded drive, or whose last drive is
     older than `--max-age-days` (45 by default). This is a report, not a
     failure, because CI should not fail on the calendar.

3. **Re-drive.** For every feature the change touched, and every feature the
   audit flagged as broken or stale, run its `Driving it with control-brigade`
   steps on a fresh target. Compare what you see with what the file says it
   observed. Where they differ, the file is wrong until you have checked
   otherwise.

4. **Update the map.** Fix the observations in the feature file. Set
   `last-drive` to the UTC date of the drive and the commit you drove. Add or
   remove known gaps. Then run the audit again and paste its `summary` into the
   PR body.

## Feature file header

Every feature file starts with a header block. The audit parses it and fails
on a missing or malformed one.

```text
---
status: active
proof: drive
surfaces: work verify run
drive: work-verify
last-drive: 2026-11-03 31d5c549d
---
```

| Key | Required | Meaning |
|---|---|---|
| `status` | always | `active`, `paused`, or `unreachable` |
| `proof` | always | `drive` (the real CLI against a temp target) or `stand-in` (anything else) |
| `surfaces` | always | comma-separated command paths, without the leading `brigade` |
| `drive` | active drives | comma-separated `control-brigade.py` subcommands that drive it |
| `last-drive` | no | `YYYY-MM-DD <commit>`: UTC date and commit of the last drive |
| `reason` | paused, unreachable | why it is not being driven, in one sentence |
| `unpause` | paused | `#<issue or PR>`, a URL, or `pending: <what unblocks it>` |

`surfaces` lists only what the drive actually runs. If a feature file
describes `work verify show` but the helper only runs `work verify run`, then
`work verify show` is not a surface of that feature.

## Statuses

- **active**: driven, and expected to keep working. The audit checks that every
  surface still exists, that its `--help` answers, and that the last drive is
  recent.
- **paused**: under rework. The feature is real, but its observed answers are
  about to change, so a drive now would record something that goes stale at
  once. A pause needs a reason and an `unpause` pointer. The audit skips its
  checks but lists it under `paused` with both, so a pause is always visible.
  `pending:` is for a pause whose unblocking work has no issue yet. Replace it
  with the issue number once one exists.
- **unreachable**: the feature cannot be driven from a temp target at all, for
  example because it needs a service this host does not run. The reason must
  say what is missing. Do not use `unreachable` for "not driven yet"; that is
  a gap.

Paused and unreachable features still claim their surfaces, so those commands
show up under `paused` or `unreachable` instead of `uncovered`.

## Coverage rule

One convenient entry point driven once is not proof that a feature works.
A feature covers the commands its drive runs, and nothing else. The audit
applies that rule to every branch a feature walks down: if a feature drives
`guard scan`, then every other `guard` verb is listed as uncovered until some
feature drives it or the gap list names it. A green drive of one verb says
nothing about its siblings.

When a change touches a command, drive that command. Do not substitute a
neighbor that happens to have a helper subcommand.

## Honesty rules

- **Stand-ins are labeled.** A pytest selector, a mocked call, an imported
  function, or a fake `brigade` wrapper is a stand-in proof. It is useful, and
  it is not a drive. Label it `stand-in` wherever it appears, and never
  describe it as "driven". A feature with `proof: stand-in` covers nothing in
  the audit; it is listed under `stand_ins`.
- **Unreachable means unreachable.** Mark a feature or a sub-feature
  unreachable only when you tried and something outside Brigade blocked it.
  Say what blocked it. "No time" or "no helper yet" is a gap, not
  unreachability.
- **Absence is data.** When a drive reaches a boundary that is not there (no
  listener, no hub), record the honest answer the CLI gave. Do not fake the
  missing side to get a green result.
- **Observations carry a date and a commit.** Any "observed" line in a feature
  file is a claim about one build. `last-drive` says which one.
- **Host-dependent answers are named.** If a drive's result depends on host
  config (a hub URL, an installed optional package), say which part depends on
  it. Pause the feature when its main answer comes from outside the target.

## Known gaps

The audit reads the fenced block below. Each line is a command path, two or
more spaces, then a note. A path ending in ` *` covers every uncovered command
below it. The most specific match wins. The audit fails on an entry that names
a command that no longer exists, or one a feature now covers, so this list
cannot rot quietly either.

```audit-gaps
add                  no feature file yet
budgets              no feature file yet
bug                  no feature file yet
care                 no feature file yet
center               no feature file yet; extras surface
chat                 no feature file yet; extras surface
code                 no feature file yet; code graph evidence moves with hub memory, add a feature after that lands
completions          no feature file yet; the audit reads `completions bash` for the command tree, which is not a drive of it
components           no feature file yet
context              no feature file yet; extras surface
daily                no feature file yet
dogfood              no feature file yet; extras surface
evidence             no feature file yet; evidence moves with hub memory, add a feature after that lands
extras               no feature file yet
fleet *              stand-in only: tests/test_fleet_*.py; only `fleet serve` is driven (hub-contract.md, storage-parity.md)
friction             no feature file yet; extras surface
governance           no feature file yet
hub connector *      only list and setup preview are driven (hub-connector.md)
handoff-template     no feature file yet
harness              no feature file yet
ingest               no feature file yet
learn                no feature file yet; extras surface
mcp                  no feature file yet
memory               no feature file yet
model                no feature file yet
node                 no feature file yet
notifications        no feature file yet; extras surface
operator             no feature file yet
outcome              no feature file yet
pantry               no feature file yet; extras surface
profiles             no feature file yet
projection           no feature file yet
projects             no feature file yet; extras surface
receipts             no feature file yet
reconfigure          no feature file yet
release              no feature file yet; extras surface
repos                no feature file yet; extras surface
research             no feature file yet; extras surface
roadmap              no feature file yet; extras surface
roster               no feature file yet
route                no feature file yet
runbook              no feature file yet; extras surface
runs                 no feature file yet
scrub                stand-in only: tests/test_scrub.py; no drive
search               no feature file yet
security             no feature file yet
setup                no feature file yet
skills               no feature file yet
stations             no feature file yet
status               run by hand on the checkout in SKILL.md, not recorded as a drive
tokens               no feature file yet
tools                no feature file yet
untrusted            no feature file yet; extras surface
update               no feature file yet
version              doctor reads --version; the version subcommand is not driven
workflow             no feature file yet; extras surface
guard *              only guard scan and guard redact are driven (guard-parity)
handoff *            only handoff lint has a feature, and it is paused
run approve          no feature file yet
run cloud            provider adapters and removal tombstones have no dedicated drive feature
work *               only work verify run is driven
work verify *        plan, runs, and show are not driven; work-verify runs only work verify run
```

Connector lifecycle: `brigade hub connector`. Verify with `tests/test_cloud_agent_profile.py` and `tests/test_dot_transport.py`.
