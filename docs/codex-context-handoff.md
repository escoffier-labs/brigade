# Codex context accounting handoff, version 1

Status: producer schema v1 frozen after the focused gate passed on 2026-10-29.
Receipt `20261029-161331-work-verify-efbb02` covers 593 producer and impacted
regression tests plus lint, format, type, version and snapshot checks. Consumer
implementation and cross-tool validation remain separate work. This is a
fresh disk accounting contract, without session or behavior equivalence claims.

## Scope and entry points

The `brigade.context_handoff.export` Python API accepts explicit
absolute `target` and `cwd`, `codex_version`, `trust`, `read_access`,
`assume_codex_defaults`, `project_doc_max_bytes`, `fallback_filenames`, and
`root_markers`. The command is `brigade context handoff` with the same
project flags as `context preview`, and mandatory `--json`. It writes JSON to
stdout only. Callers choose whether to redirect it. Neither entry point discovers
configuration, runs Codex, writes sources, invokes a model, or proposes edits.
Global inspection is outside this repo-only handoff. Global flags are rejected.
The command inherits the existing extras-surface gate. A caller can opt in for
one invocation with `BRIGADE_EXTRAS=1` without writing persistent configuration.
That framework gate and parser usage errors precede the inspection contract.

`context_preview.preview` keeps its signature and existing JSON. Its internal
inspection optionally collects handoff evidence while file and directory
descriptors remain open. Selection, raw-byte consumption and rendering share
the preview primitives. Calling preview and subsequently
reading files or collecting observations is insufficient.

The consumer protocol is schema version 1, independently of product releases.
Its Codex accounting contract is 0.160.0, source commit
`a956835d020762cb2b570053af06f643a11c0ecc`. This does not claim compatibility
with another Codex version or an observed session.

## Strict envelope

An export has exactly these top-level fields. Nested objects also reject unknown
keys. All fields are mandatory. Failure to establish a complete inspection
returns an error envelope instead of an export.

| Field | Type and meaning |
| --- | --- |
| `schema` | Literal `brigade.context_handoff` |
| `schema_version` | Integer 1 |
| `producer` | Object with `name: "brigade"` and `version` equal to the exporting Brigade version |
| `consumer` | Object with `name: "codex"`, `version: "0.160.0"`, `version_source: "caller-supplied"`, and `source_commit` as pinned above |
| `actual_session_observed` | Literal false |
| `repository` | Object with `location_sha256` and `identity`, without a resolved personal path |
| `cwd` | Repository-relative directory path, `.` for the target itself |
| `settings` | Settings and their provenance, specified below |
| `directories` | Ordered directory identities from target through cwd, inclusive |
| `root_marker_observations` | Ordered nofollow marker observations used in the root search |
| `selection_observations` | Ordered candidate observations used to select each directory's winner |
| `sources` | Root-to-cwd selected regular files, including empty and cap-exhausted files |
| `baseline` | Object containing `selected_bytes`, `consumed_raw_bytes`, `rendered_utf8_bytes` |
| `completeness` | Object with `inspection: "complete"`, `source_hashes: true`, `selection: true`, `directories: true`, `root_markers: true` |
| `limitations` | Exact ordered array below |
| `digest` | Lowercase `sha256:` plus 64 hexadecimal digits |

Integers exclude booleans, negative values and floats, and must fit unsigned
64-bit range. Strings must encode as strict UTF-8 without surrogate code
points. No duplicate JSON keys, nonfinite constants, BOM or trailing data.
No nullable totals, placeholders, partial exports, or unknown schema versions.

`repository.identity` is `{device, inode, kind}` with `kind: "directory"`.
This binds one local worktree, including linked worktrees, without importing Git
configuration or resolving another worktree. `location_sha256` is lowercase
64-digit SHA-256 over the UTF-8 bytes of the normalized absolute lexical POSIX
repository path, prefixed before hashing with the ASCII domain
`brigade.context_handoff.repository.v1` and one NUL byte. The absolute path
itself is never exported. The explicit consumer repository must have the same
location digest and directory identity. A clone,
replacement directory, moved worktree or alternate checkout requires a new
inspection. This identity is local, not portable or authenticated.
Handoffs are local artifacts, not anonymized telemetry. Digests can confirm
guessed paths or short source contents and do not make the data confidential.

Every relative path uses `/`. It must have no absolute prefix, empty segment,
`.` segment except the entire cwd/directory path `.`, `..`, backslash, colon,
NUL or control character. The export rejects ambiguous/unsafe paths rather
than changing their meaning. Every source and observation must be inside the
declared target. Absolute inputs reject dot and dot-dot components, backslashes,
colons, NUL and Unicode C0/C1 controls before normalization. Repeated slashes
and trailing slashes normalize to the same absolute lexical path. Every lexical
absolute path component is opened without following symlinks, including
ancestors outside the declared target.

## Settings and limitations

Each setting is exactly `{value, source}`. The project cap is a nonnegative
integer at most 1,048,576. Fallbacks and markers are ordered arrays of basenames.
Explicit values have source `caller-supplied`. Omitted values require explicit
`assume_codex_defaults` and have source `default-assumption`: cap 32,768,
fallbacks `[]`, markers `[".git"]`. Otherwise refuse unknown settings. Strict
handoff inputs reject malformed JSON, wrong types, invalid names and fallback
entries that preview would ignore. Do not silently coerce or drop entries.
Names must already equal their Rust-whitespace-trimmed spelling. Duplicates
are retained in order and observed in order.

Also require `trust: {value: "trusted" or "unset", source: "caller-supplied"}`,
`read_access: {value: "full", source: "caller-supplied"}`, and
`environment_order: {value: ["explicit-cwd"], source: "caller-supplied"}`.
No discovered user-base provenance is claimed by this explicit-supplied route.
The source-acquisition ceilings below are protocol constants, not Codex limits.

Zero cap, untrusted loading, restricted access, unsupported platforms and
unevaluated results produce no consumable export. Disabled or uninspected
files must never become trim candidates.

The exact `limitations` array is:

```json
["fresh_disk_only", "no_atomic_snapshot",
 "timestamp_granularity_limits_change_detection", "access_times_may_change",
 "thread_and_session_state_unknown", "global_not_requested",
 "profile_project_system_managed_launch_overrides_unresolved",
 "integrity_digest_not_authorization", "under_cap_not_behavior_guarantee"]
```

## Observation records

`directories` contains `{path, identity}` in target-to-cwd order. `identity`
contains `{device, inode, kind}` with `kind: "directory"`. Directory timestamps
are excluded because unrelated child creation does not change directory identity.
The consumer must bind and revalidate every absolute ancestor too, without
requiring disclosure of all ancestor identities in the export.

An observation is exactly `{directory, name, state, identity}`. `directory`
references one directory path in `directories`. `state` is `absent`, `regular`,
`directory`, `fifo`, `socket`, `character`, or `block`. An absent observation
has `identity: null`. A directory identity has `{device, inode, kind}`. Every
other present identity has `{device, inode, kind, size_bytes, mtime_ns, ctime_ns}`,
with `kind` equal to `state`. A symlink, unsupported type, metadata failure or
changed observation invalidates the inspection. Never open a nonregular source.
Version 1 does not support negative identity integers, including pre-1970
timestamps. These inputs refuse with `identity_not_established`.

Root search walks cwd toward target using supplied markers in order. Record
each absence/type up to and including the first present marker, then stop.
Presence of any supported nonsymlink type establishes that boundary, matching
preview. If the marker array is empty, only cwd is selected and marker
observations are empty. A nonempty array without a found marker inside target
cannot export. No ancestor outside the approved repository becomes a winner.

Starting at the observed root boundary, inspect directories in root-to-cwd
order. Candidate order is `AGENTS.override.md`, `AGENTS.md`, then fallbacks.
Record each absence or nonregular type, stopping after the first regular file
at each directory. A zero-length regular override wins. A missing winner
records all candidates. Files after a winner need no observation because they
cannot affect that selection. Every searched directory must be represented,
including directories with no source. Candidate and marker observations are
revalidated at the end even if the winning file did not change.

The consumer validates the declared ordered search trace against the settings,
directory chain and declared boundary. It checks every declared observation
without searching for another winner. If the trace is missing, contradictory,
or stale, validation fails. Inserting an override or root marker, replacing a
nonregular candidate with a regular file, or rebinding any directory requires
another producer inspection. A digest alone cannot justify missing assertions.
For nonempty markers, the final marker observation must be present and its
directory defines the root boundary. With no markers, the boundary is cwd.
Each searched directory with a regular winner must correspond to exactly one
source in root-to-cwd order. That source's path is the winner's directory joined
to its name, and its identity must equal the winner observation's identity.
At directory `.`, the source path is just the winner's name. Missing, extra,
reordered or contradictory sources invalidate the handoff.

## Complete source acquisition and accounting

A source is exactly `{path, selection_order, identity, size_bytes, sha256,
cumulative_selected_bytes, consumed_raw_bytes, rendered_utf8_bytes,
contribution}`. `selection_order` is 1-based and contiguous. `identity` is the
same regular-file fingerprint shape as an observation. `sha256` is lowercase
64-digit hex over the entire raw file, without a prefix. `size_bytes` must equal
the full read length and identity's size. Cumulative selected bytes include
all selected files even after the modeled cap is exhausted.

Open a selected file relative to its held directory with nofollow/nonblocking
flags, require a regular file, compare nofollow selection metadata with fstat,
read its complete bytes under independent ceilings, then compare held and
named fingerprints again. Derive accounting and hashing from these same
bytes. Keep the complete hash for files with `contribution: "cap_exhausted"`.
At final validation, re-read each held source descriptor with bounded positional
reads of at most the recorded size plus one overflow byte and compare its hash
again. Recheck metadata and all selection bindings after those reads. This catches
persistent same-size changes even when timestamps do not advance. It does not
provide an atomic snapshot or detect content changed and restored between reads.
Revalidate selections, candidate observations, root markers and all held
directory bindings before returning any export. A final failure discards the
whole export, including previously computed rows.

The version 1 ceilings are 1,048,576 bytes per source, 8,388,608 bytes
across all selected sources, 256 selected sources, 128 target-to-cwd
directories, 64 entries in each supplied filename list before validation,
4,096 UTF-8 bytes per path, and 8,388,608 UTF-8 bytes per canonical JSON
envelope, including its digest but excluding the transport newline. Absolute target/cwd paths also have at most
128 nonempty components each. Canonical JSON has a maximum nesting depth of
64, where the top-level value has depth 0. The latter two limits bound ancestor
descriptors and serialization recursion. They are additions to the draft
ceilings. All other draft ceilings are unchanged. The 256-source ceiling is
currently redundant because each of at most 128 searched directories selects
at most one source. Counts are checked before content acquisition.
These limits are independent of the cap. Bound reads with an overflow probe
of at most one extra byte, and check counts before accumulating data. Refuse
oversize files even when the modeled cap has already been exhausted.

For each selected file with remaining cap greater than zero, take at most the
remaining number of raw bytes. Decode that prefix with UTF-8 replacement.
If the decoded prefix is entirely Rust whitespace, the file contributes zero
raw and rendered bytes and does not consume the cap. Otherwise raw consumption
is prefix length and rendered size is the encoded decoded prefix length.
Do not strip project text. `contribution` is `truncated` if full source size
exceeds remaining cap, otherwise `loaded`. Deduct consumed raw bytes. With
remaining cap zero, contribution is `cap_exhausted` with both counts zero,
including empty files. Entirely blank prefixes have contribution `empty`.

Rust whitespace is U+0009..U+000D, U+0020, U+0085, U+00A0, U+1680,
U+2000..U+200A, U+2028, U+2029, U+202F, U+205F and U+3000. Replacement
characters consume three rendered UTF-8 bytes. Preserve CRLF and invalid source
bytes for hashes and raw accounting. Baseline totals are sums of source rows.
Rendered totals do not include separators or prompt framing.

## Canonical digest and refusal

Remove only the top-level `digest` field. Serialize the remaining strict JSON
value with sorted object keys, no spaces or trailing newline, UTF-8 output,
`ensure_ascii=False`, and no nonfinite numbers. Array order is significant.
Hash those exact bytes with SHA-256 and prefix the lowercase hex with
`sha256:`. JSON whitespace in the transported envelope is irrelevant.
The digest detects accidental changes. Anyone who edits a handoff can recompute
it. It provides no authorization, signature, freshness proof or atomic snapshot.

On inspection refusal, the CLI exits 2 and returns exactly
`{schema: "brigade.context_handoff.error", schema_version: 1,
actual_session_observed: false, reason: <opaque-code>}`. It emits no partial
source list, bodies, raw exceptions or unsafe input values. The complete reason
vocabulary is:

| Reason | Condition |
| --- | --- |
| `invalid_settings` | Unknown/malformed settings, absent provenance, forbidden global arguments |
| `unsupported_version` | Caller version differs from the pinned contract |
| `inspection_disabled` | Zero cap, untrusted loading or restricted reads |
| `unsupported_platform` | POSIX nofollow/dirfd/nonblocking primitives unavailable |
| `resource_limit` | Any declared resource ceiling exceeded |
| `unsafe_path` | Unsafe lexical path, symlink or unsupported file kind |
| `identity_not_established` | Missing root boundary, unreadable input, failed metadata or other uninspectable state |
| `inspection_changed` | Changed held/named fingerprint, selection, observation or directory binding |
| `json_required` | CLI omitted mandatory `--json` |

Every inspection refusal returns the same four keys. CLI output is canonical
UTF-8 JSON with one trailing newline on stdout and no stderr output. The API
returns the corresponding dictionary and never a partial export. Its keyword
signature accepts supplied Python values without JSON coercion. As with normal
Python calls, missing or unknown keyword arguments raise `TypeError` before
inspection. CLI argument syntax and `--help` remain standard argparse behavior.
They are outside the inspection envelope contract. Known global flags are
accepted syntactically only to return `invalid_settings`, never to read them.

CLI cap and filename-list values use strict JSON parsing. JSON null is invalid
for an explicitly supplied setting. Duplicate object keys, nonfinite constants,
BOM, trailing data and surrogate strings are rejected. Wrong-shaped values are
rejected even when ordinary preview would ignore or normalize them. Omitted
settings have the documented explicit-default semantics.

`canonical_bytes(value)` validates supported JSON values and returns their
canonical UTF-8 bytes. It rejects floats, integers outside unsigned 64-bit range,
nonstring keys, surrogate strings, unsupported Python containers, excessive
nesting and excessive serialized size with `ValueError`. Boolean values and
null remain valid JSON values where the schema permits them. It does not remove
a digest field. `integrity_digest(object)` removes only the top-level `digest`
before canonicalization. Nested digest fields remain bound. Schema field/type
validation is the consumer's responsibility, in addition to digest checking.
Consumer validation failures never trigger rediscovery, configuration resolution
or a replacement chain.

## Shared fixtures and verification

[Accounting vectors](fixtures/codex-context-handoff-v1.json) contain synthetic
`files` as path-to-hex maps, directory declarations, explicit cap/settings,
expected selected rows and expected totals. Source hex belongs only to this
test corpus. Actual exports contain no source bodies or body-derived excerpts.
`canonical_digest_vectors` separately pin canonical serialization. Filesystem
identities must be generated per local fixture, never copied from a real host.
These are accounting vectors, not claimed producer export fixtures.

[Export fixture](fixtures/codex-context-handoff-export-v1.json) is a complete
producer export template generated from synthetic files. Its wrapper is
`brigade.context_handoff.export_fixture.v1` and explicitly marks it
`portable-test-template-not-a-consumable-handoff`. The producer version, identity fields,
repository location digest and integrity digest are placeholders. The tests
materialize these expectations from a newly generated local filesystem, then
compare the entire export, including its digest. Source hex is fixture setup,
never part of the actual export. Copying the template into a consumer is invalid.
A real export binds its local target and must be regenerated there.

`tests/test_context_handoff.py` verifies all 15 accounting and 3 canonical
vectors against the producer, privacy, same-size changes beyond the cap,
changed-during-read and final validation races, new overrides and markers,
directory rebinding, empty-directory selection traces, symlinks, nonregular
observations, explicit provenance, resource limits and opaque refusals. Existing
preview tests keep the public API and output under regression coverage. These
producer tests do not establish consumer conformance or behavior preservation.
