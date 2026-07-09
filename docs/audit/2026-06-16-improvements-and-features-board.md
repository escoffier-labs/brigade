# Brigade: improvements + features board (2026-06-16)

A grounded sweep of the repo across 12 lenses (the `special` feature lenses plus
line-check / bug-hunt / reduce improvement lenses). Every item cites a file:line
signal and was verified that the signal exists, the capability is not already
built, and it does not violate a declared non-goal. 63 candidates found, 59
survived verification.

Status legend: **[shipped]** landed on branch `fix/doctor-first-contact-and-json`;
**[issue #N]** filed; everything else is an open proposal.

## The read

The codebase wants two things: a credibility pass on its diagnostics surface, and
parity-closing on its station verbs. Issue #83's UTC-vs-local clock bug surfaced
independently in four lenses (the single most-corroborated finding), and first
contact leaks scary WARNs for dirs that `init` itself fails to create (#79). Both
erode trust in `doctor`, the surface that sells the tool. The second through-line
is asymmetry: `install` has rollback but `tools apply` does not, `skills` has lint
but no `--fix`, `security` has every `--json` except suppress, `projects` is the
one station missing `doctor`, and `ingest` carries inverted write-defaults between
its top-level and fleet forms. Underneath both, two infra gaps quietly threaten the
"grep-able receipt" promise: `write_json` was non-atomic and several git subprocess
calls still lack the v0.11.0 stdin/timeout fix.

## Top picks

| # | Item | Type | Leverage | Effort | Status |
|---|------|------|----------|--------|--------|
| 1 | doctor memory-care UTC-vs-local future-date warn | fix | high | S | **shipped** (#83) |
| 2 | `.brigade/memory-care/decay` created by workspace init | fix | high | S | **shipped** (#79) |
| 3 | `write_json` atomic (tmp + os.replace) | fix | high | S | **shipped** |
| 4 | `doctor --json` / `status --json` | feature | high | S | **shipped** |
| 5 | `security diff --base --against` (by fingerprint) | feature | high | S | **shipped** (0.12.0) |
| 6 | Parallelize fleet `_repo_summary` (ThreadPoolExecutor) | perf | high | S | **shipped** (0.12.0) |
| 7 | `brigade operator checkup` (all first-run doctors at once) | feature | high | M | **shipped** (0.12.0; loop health in Unreleased) |

## Improvements backlog

### Bugs / fixes
- **[shipped] doctor memory-care UTC vs local today (#83)** - `memory_cmd.py:88` `_today()`=UTC; `doctor.py:507` used local `date.today()`; warns at `:478-480` when age<0. Fixed to `datetime.now(timezone.utc).date()` + regression test.
- **[shipped] workspace init decay dir (#79)** - `templates/depth/workspace.json:29` created legacy `memory/cards/decay`; doctor checks `.brigade/memory-care/decay` (`memory_cmd.py:21`). Template now creates the canonical path.
- **[shipped] `write_json` non-atomic** - `localio.py:43-46` did a direct `write_text`; `read_json_dict` swallows `JSONDecodeError`. Now tmp + `os.replace` with cleanup on failure.
- **v0.11.0 stdin/timeout fix missed several git subprocess sites** - present at `proc.py:45`, missing at `localio.py:89` (`check_git_ignored`), `scrub.py:136/151/250`, `work_cmd/helpers.py:27`. Add `stdin=DEVNULL` + `timeout` to the egress-gate and heavy-caller sites first. S, high.
- **pre-push hook labels any nonzero content-guard exit as "BLOCKED" (#82)** - `templates/hooks/pre-push:30-36` does not discriminate exit codes; content-guard separates 1=findings from 2=infra error. Branch on `rc`. S, med.
- **TOML 3.10 fallback: top-level quoted keys mis-parsed** - `toml_compat.py:47` assigns the raw key without `_parse_key` (inline tables call it at `:150`). Route line 47 through `_parse_key`. S, high. Sibling bare-date/time issue is real but theoretical since brigade always quotes dates.

### Refactors
- **Extract `tools_cmd` projection renderer** - 13 contiguous pure functions `tools_cmd.py:1951-2241` in a 6112-line file; `work_cmd/__init__.py:1-24` documents the facade pattern. M, med.
- **Split `tools_cmd` runtime supervision** (pid/port/process `:992-1593`) into a submodule. L, med.
- **Share `actionqueue.find_action`** instead of `phases_cmd` reimplementing matching at `:3936/:5310` (keep local stamping; fields diverge). S, high.
- **`add_target_arg` helper** - `--target` literal duplicated 161x; `cli/_common.py` is the designated home but has no helper; 5 commands drop the help string. S, high.

### Tests
- UTC-vs-local clock in memory-care freshness (**shipped** with #83).
- Scrub egress gate: no test where content-guard returns nonzero (`scrub.py:253`). S.
- Run-lock `PermissionError` (foreign-user PID) path untested (`runguard.py:98-99`). S.
- Threaded dispatch worker-exception path is `# pragma: no cover` (`aboyeur.py:472`). S, med.

### Perf (maintainer runs ~482 cards / ~35 repos)
- **Parallelize fleet `_repo_summary`** (`repos_cmd.py:844`) - serial today, ~3 git/IO sweeps per repo. S, high.
- doctor re-walks the cards tree 3x per run (`doctor.py:352,386,285`). S, high.
- memory-care `scan` calls `path.stat()` 4x per card (`memory_cmd.py:448,543,551,552`). S, high.
- `_index_links` re-stats per card (`memory_cmd.py:436,571`). S, high.
- mtime/hash sidecar index so `scan` skips unchanged cards (`_scan_payload:431-524`); invalidate on config change + date rollover. M, med.

### UX / docs
- **[shipped] `doctor`/`status --json`.**
- `ingest` inverted write-defaults: top-level writes by default (`ingest.py:13`), `repos ingest` is dry-run by default (`repos.py:54`). Document both + add a parser-walk guard test. S, high.
- Parser-walk test enforcing same-named verbs agree on `--apply`/`--dry-run` and read-commands expose `--json` (`test_cli_help.py:8-20` already has the walker). M, high.
- Document `brigade budgets` (`cli/budgets.py:14`), the `untrusted` station (`cli/untrusted.py:11`), and cover both in the technical-guide tour. S each.
- Soften README "all harnesses get projected tools/skills" claim - `overview.md:147-159` shows several get only `rules`/`instructions`. S, high.
- Add a "backing out" / undo subsection (`reconfigure --prune` removes harness files but not root `MEMORY.md`/`AGENTS.md`). S.
- Mark done-vs-pending state in `work-cmd-split-plan.md` (says "not started" but `work_cmd/` is a 14-module package). S.

## Specials board (leverage-sorted)

- **[shipped] `security diff`** - 0.12.0: fingerprint-keyed new/resolved/persisting findings between two scans.
- **[shipped] `brigade operator checkup`** - 0.12.0: every first-run doctor in one pass; Unreleased adds optional GraphTrail/MiseLedger loop health (graph/ledger/brief hit rate).
- **[shipped] Surface read-only enforcement strength per harness in `brigade run`** - 0.12.0 (issue #87).
- **[shipped] Expose memory cards over the existing MCP stdio server** - 0.12.0 `memory serve-mcp` with `card://` (issue #88).
- **[shipped] Generate shell completions** - 0.12.0 `brigade completions bash|zsh|fish` (issue #89).
- **[med] Deterministic keyword search over `memory/cards`** - reuse `_iter_cards`+`_parse_frontmatter`; stdlib precursor to the roadmapped (Later) semantic retrieval. `memory search <term> [--json]`. M.
- **[med] Route failed runbook steps into work-import** - 30 other sites use `ledger._append_import_records`; runbook closeout (`runbook_cmd.py:370`) does not. M.
- **[med] `skills uninstall`** to mirror `skills install` - literal inverse, reuses `_install_targets`/`_install_dir` + history receipt. S.
- **[med] `archive` for `tools call` / `tools checkpoint` queues** - `calls.jsonl` never self-prunes; copy `daily approvals archive` semantics. S.
- **[med] `projects doctor`** - the one station missing a doctor wrapper; `projects_cmd.health()` already exists. S.
- **[med] `scrub` receipt + `--json`** - the egress gate is the most safety-critical boundary and the only station with no trail; store summary-only to avoid re-leaking gated PII. S.
- **[med] AGENTS.md quality lint station (#84)** - `doctor.py:206-258` checks existence + byte budget only. Owner must pin the required-section vocabulary first (brigade's own template uses neither "Definition of Done" nor a handoff-footer heading). M.
- **[shipped] `doctor` fleet sweep (#78)** - 0.12.0 `brigade repos doctor --deep` runs operator checkup per fleet repo.
- **[med] init/doctor detect third-party clones -> recommend `.git/info/exclude` (#81)** - reuse `build_gitignore_block`, recommend-only; needs an owner-identity heuristic. M, med.
- **[med] Wire `friction scan` staleness into `daily status`** - friction is absent from the daily loop; report latest.json staleness + candidate count, no auto-run. M.
- **[low] Snapshot on `tools apply`** for rollback (mirror skills install) - `tools_cmd.py:5499` writes with no snapshot. M.
- **[low] `--json` on `security suppress/unsuppress`** (`cli/security.py:79-89`). S.
- **[low] `skills lint --fix`** (metadata-only, mirror handoff migrate). M, med.
- **[low] `runs export`** (portable run summary from `runs_cmd.py:194` show data). S.
- **[low] `friction list/show`** read past scans (`friction_cmd.py:370-384` writes but no reader). S.
- **[low] contradictory-card content detection** - today only fires on duplicate ids (`memory_cmd.py:584-600`). M, med.
- **[low] doctor: separate machine-global from per-repo findings (#80)** - presentation grouping on existing tuples. S.

## Considered and cut
- skills/tools parity - different concepts by design; the capability exists via user-extensible `adapters.json`.
- `brigade daily loop` command - `operator guide` already prints the ordered sequence; ~90% duplicative.
- Stamp a handoff/card format version - misreads the format (section-header markdown), brushes the "never edits canonical memory" non-goal; migrate path is deliberately version-free.
- Migrate `phases_cmd` report-resolve to reportstore - reverses a documented carve-out (`reportstore.py:12-20`).

## Suggested sequence
First-contact fixes (#83, #79, atomic receipts) and `doctor/status --json` shipped on
`fix/doctor-first-contact-and-json`. Next, `security diff` (self-contained, immediate
value) and the fleet `_repo_summary` parallelization (cheap, high impact). Then
`operator checkup`, which the `doctor --json` flag now unblocks, followed by the #78
fleet sweep on top of it. Push the parser-walk consistency test to the end of a batch
(or xfail it) since it will go red on the ingest/doctor write-default divergences until
those land.
