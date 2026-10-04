# Codex instruction preview

`brigade context preview` reports fresh-disk instruction byte accounting for
one explicitly scoped POSIX environment and caller-supplied Codex 0.160.0
settings. Every result has `actual_session_observed: false`. The version is
supplied by the caller. Brigade never invokes Codex to verify it.

```sh
brigade context preview --target /approved/project --cwd /approved/project/sub \
  --codex-version 0.160.0 --trust trusted --read-access full \
  --assume-codex-defaults --project-doc-max-bytes 7 --json
```

Both paths must be absolute. Brigade normalizes separators and dot components
lexically, collapses leading `//` to `/`, and checks containment without resolving
symlinks. `--codex-home /approved/global --global-max-bytes 65536` adds an
explicit global scope. There is no automatic home or configuration scan.

## Supplied configuration

The effective project cap, fallback filenames and root markers each carry
`caller-supplied` provenance. `--fallback-filenames '["TEAM.md"]'` and
`--root-markers '[".git"]'` accept JSON lists. With
`--assume-codex-defaults`, only omitted keys receive `default-assumption`:
cap 32768 bytes, no fallbacks, and `.git` markers. Missing provenance, an
unknown or unsupported version, unknown trust, or unknown read access produces
`not_evaluated` before any document content read. Public errors contain opaque
reason codes. Rejected path-bearing settings are never echoed.

The caller must supply effective values after configuration resolution. Codex
0.160.0 applies enabled layers by source rank: packaged defaults, MDM, system,
enterprise managed, user base, active user profile, project, session flags,
legacy managed file, and legacy managed MDM. Thread layers are inserted by
source rank. The cap and fallback list use enabled layers. Root markers exclude
Project layers. Origins are per key and requirements have separate provenance.
Brigade does not read that tree. See the pinned
[config layer ranks](https://github.com/openai/codex/blob/a956835d020762cb2b570053af06f643a11c0ecc/codex-rs/config/src/config_layer_source.rs),
[loader](https://github.com/openai/codex/blob/a956835d020762cb2b570053af06f643a11c0ecc/codex-rs/config/src/loader/mod.rs),
[enabled layer state](https://github.com/openai/codex/blob/a956835d020762cb2b570053af06f643a11c0ecc/codex-rs/config/src/state.rs),
and [effective defaults](https://github.com/openai/codex/blob/a956835d020762cb2b570053af06f643a11c0ecc/codex-rs/core/src/config/mod.rs).

`--trust` accepts `trusted`, `untrusted`, `unset`, or `unknown`. Supplied
`untrusted` suppresses project loading. `unset` allows project instructions.
Project configuration trust is evaluated per directory containing `.codex`,
with project/repository lookup keys. It differs from active-project instruction
trust. Linked worktrees can derive trust from the main checkout in Codex config
resolution. Brigade uses supplied effective trust and never traverses the Git
common directory. See pinned
[trust definitions](https://github.com/openai/codex/blob/a956835d020762cb2b570053af06f643a11c0ecc/codex-rs/config/src/config_toml.rs),
[worktree trust tests](https://github.com/openai/codex/blob/a956835d020762cb2b570053af06f643a11c0ecc/codex-rs/core/src/worktree_trust_tests.rs),
and [instruction loader](https://github.com/openai/codex/blob/a956835d020762cb2b570053af06f643a11c0ecc/codex-rs/core/src/agents_md.rs).

`--read-access full|restricted|unknown` gates inspection and records the supplied
filesystem profile. An unknown value prevents document content reads. For known
values it reports the caller's mode, without changing Brigade's error outcome.
Codex discards an environment load on non-NotFound errors. Callers with full disk
read access log and continue, while restricted callers error. Brigade reports
unknown accounting for the affected scope in both modes. It does not infer
access from the sandbox's name.

## Accounting and selection

Marker discovery stops at the approved scope boundary. If configured markers
are nonempty and no marker is found inside, the root is unknown and Brigade
reads no project documents. This is a bounded divergence from Codex's search
up to filesystem root, where no marker yields cwd only. An explicit empty
marker list means cwd only. Brigade accepts each marker as one nonempty POSIX
basename excluding slash, NUL, `.` and `..`. Rejected marker entries make the
preview unevaluated. Codex supports broader marker path syntax.

At each directory from the selected root to cwd, selection chooses the first
regular file among `AGENTS.override.md`, `AGENTS.md`, then normalized valid
fallback basenames. An empty selected override blocks the normal file.
Directories and other nonregular candidates are skipped without reading them.
Discovery completes before project reads, including candidates whose eventual
contribution is `cap_exhausted`. POSIX colon and backslash names and valid
surrogateescaped bytes are supported. Unencodable names are unevaluated.
Fallback trimming uses Rust's explicit Unicode White_Space set. U+001C through
U+001F remain content. Invalid fallback entries are discarded and labeled
`invalid_fallback_entries_ignored`, without disclosing their values. See pinned
[discovery and loading](https://github.com/openai/codex/blob/a956835d020762cb2b570053af06f643a11c0ecc/codex-rs/core/src/agents_md.rs)
and [loader tests](https://github.com/openai/codex/blob/a956835d020762cb2b570053af06f643a11c0ecc/codex-rs/core/src/agents_md_tests.rs).

Each file row reports its named scope and relative path, selection order,
held-file raw size, cumulative selected bytes, consumed raw prefix bytes,
rendered UTF-8 bytes, contribution and Brigade advisory bootstrap budget.
For root `root` (4 bytes), child `abcdef` (6 bytes), and cap 7, selected
cumulative sizes are 4 and 10. Consumed prefixes are 4 and 3. A whitespace-only
prefix contributes zero and debits no project budget. A 3-byte prefix of `éé`
consumes 3 raw bytes and renders 5 bytes after replacement decoding. Separators
and prompt wrappers are excluded from these counts. A zero cap skips project
loading. Brigade advisory budgets are independent of Codex's consumer cap.

Global loading tries the override and then normal file until it finds nonempty
trimmed text. It has separate totals and never debits the project cap. Brigade
requires an explicit global safety limit. An oversized selected global file is
unevaluated, is not read, and does not fall through. Project and global limits
must each be at most 1 MiB. A larger supplied limit refuses the entire preview
before any document content read. These are Brigade resource limits. Brigade
never silently clamps a consumer cap. See the pinned
[global instruction provider](https://github.com/openai/codex/blob/a956835d020762cb2b570053af06f643a11c0ecc/codex-rs/codex-home/src/instructions/mod.rs).

## Scope and uncertainty

Brigade opens absolute path components and descendants with retained directory
descriptors and `O_NOFOLLOW`, then opens only regular documents with
`O_NONBLOCK`. Reads are bounded to the remaining limit plus one byte. It compares
held-file device, inode, ctime, mtime and size before and after reading, and
checks that the name still identifies that file. Before completion it rechecks
every discovered project winner or absence, including cap-exhausted rows, and
every examined global candidate. These final checks use nofollow metadata only.
Changed sizes, replacements, missing winners, new higher-priority overrides and
new candidates in previously empty directories make the affected accounting
unknown. Final checks never read cap-exhausted content or select a replacement
winner for accounting. There is no atomic multi-file snapshot. Same-size
rewrites can escape detection when the filesystem preserves both timestamps
within its timestamp granularity. Changes after a final check can also escape.

Scope and cwd directory chains are validated even when untrusted settings or
a zero cap suppress project content reads. Native POSIX support must include
directory-relative open and stat, nofollow stat, `O_DIRECTORY`, `O_NONBLOCK`
and `O_NOFOLLOW`. Missing capabilities or unsupported runtime calls produce
`posix_nofollow_required` without filesystem error details.

All symlinks are refused, including in-scope links, dangling override links,
markers, cwd, and scope prefixes. `symlink_not_evaluated` sets
`matches_codex: false`, because Codex can follow those links or fall through a
dangling candidate. Brigade never follows or reads a refused link. A `.git`
file is marker metadata only: Brigade reads neither its body nor a common
directory. Bodies and absolute input paths are excluded from text and JSON. Filename controls are JSON-escaped in both formats.

`complete` and `matches_codex: true` apply only to fresh-disk accounting under
the supplied settings and requested scopes. `partial` retains a separately
completed requested scope when another scope fails. A valid empty global scope
counts as completed. A global scope that was not requested cannot justify
`partial`. Retained scopes receive final metadata validation even after another
scope fails. A scope counts as completed only after that validation. Status is
calculated after failed accounting becomes null.
`not_evaluated` means the inspection could not establish those results. Every
result discloses fresh-disk and active-session uncertainty. Inspection performs
no intentional writes. Reads can update access times.

## Evidence boundaries

The isolated synthetic Codex 0.160.0 oracle matrix passed 30 cases. It used
`codex debug prompt-input` with disposable synthetic inputs, read-only fixture
documents, masked operator directories, and Bubblewrap namespace isolation.
The native binary hash was taken on the host bind source at run start. Version
was queried inside the isolated namespace. The oracle creates ephemeral state
in its synthetic home and scratch directories. Product preview runs no oracle,
Codex, Git or model subprocess.

Offline tests reproduce the matrix's byte counts and selection expectations:
ASCII caps, split and invalid UTF-8, whitespace, override priority, empty
overrides, fallback normalization, absence, global separation, zero cap,
supplied trust and config outcomes, nested/empty marker settings, and linked
worktree marker files. File/cwd/dangling symlinks assert conservative divergence.
The no-marker case asserts unknown root at Brigade's scope boundary. Synthetic
config tests supply the effective cap and trust. They do not execute config
resolution. Linked-worktree tests use a synthetic marker file and assert no
common-directory reads. They do not invoke Git.

Permission-denied behavior, dangling markers, multiple environments, project
cache invalidation, global retained snapshots, and thread state remain
source-only findings. They are not claimed as executed fixtures. Codex's
outer environment loop debits rendered bytes, and cache selection/trust state
can preserve earlier text despite disk changes. Global refresh can retain the
last good snapshot on warning. Thread instructions occupy a separate render
slot and have their own approximate token bound. See pinned
[project manager](https://github.com/openai/codex/blob/a956835d020762cb2b570053af06f643a11c0ecc/codex-rs/core/src/agents_md_manager.rs),
[global provider tests](https://github.com/openai/codex/blob/a956835d020762cb2b570053af06f643a11c0ecc/codex-rs/codex-home/src/instructions/tests.rs),
and [marker search](https://github.com/openai/codex/blob/a956835d020762cb2b570053af06f643a11c0ecc/codex-rs/file-system/src/find_up.rs).

This bounded preview addresses part of issue #1564. Existing-session consumption
and automatic effective-config observation remain outside this slice.
