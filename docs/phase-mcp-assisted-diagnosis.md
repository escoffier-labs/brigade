# MCP-first assisted diagnosis

Status: approved operator workflow, with implementation choices delegated to the
feature owner on 2026-10-29. This extends the fixture proof merged in #2120.

## Operator experience

Use `brigade bug diagnose --target <repo> --harness codex --summary <symptom>`.
An optional `--name <server>` narrows the comparison. `--json` returns structured
output. The default operation reads only the selected project's canonical MCP
catalog, ownership sidecar and selected harness configuration. It writes nothing.

The result states the symptom, expected configuration, observed difference,
uncertainty and next check. A missing native entry explains that the selected
server exists in the catalog but is absent from the selected project config.
Matching configuration always says runtime availability is unknown. Missing,
stale and externally edited configurations remain distinct. Disabled or excluded
servers are identified as excluded. Malformed input is an evidence
error, never silently treated as an empty native config.

The operator can explicitly rerun with one of two mutually exclusive actions:

- `--save-finding <path>` exports the current redacted finding as existing work
  import JSONL to a new file. It does not ingest or promote it. The exported
  content is still reviewed through existing import validate/dry-run controls.
- `--prepare-report` prepares and previews a local report using the existing bug
  report v1/redaction/security checks and receipt path. It never submits. Exact
  preview consent and later public submission stay in `brigade bug report`.
  Preparation uses the collected diagnosis directly. General diagnostics,
  subprocesses, recent receipts, global last-error and installation metadata
  are not collected. Uncollected report metadata remains unknown.

These actions operate on the current comparison and display its output. A new
comparison may observe changed configuration. No saved-preview approval is
reused for changed evidence or for public submission.

## Implementation and limits

Reuse the per-harness projection/fingerprint engine in `mcp_cmd`, not a second
comparison implementation. Factor its comparison core to accept in-memory
canonical servers, ownership state and native text. Existing plan/sync/doctor
behavior stays intact. Do not call broad `mcp doctor` from diagnosis: it chooses
all configured targets and its readers have no diagnosis byte limits.

The diagnosis reader accepts project adapters only. Reject user-scope adapters
and paths escaping the project. Read at most 64 KiB each from three fixed files,
at most 192 KiB total. Missing native config means no projection. Missing or
malformed catalog, malformed sidecar, non-regular files, hardlinks, symlinks,
reparse points and oversized inputs fail closed. Missing sidecar means no prior
ownership. Preserve native validation before adapter parsing. Bound findings to
100 servers and fail closed if that limit would be exceeded.

Use existing checked descriptor primitives for ancestor/leaf safety and bounded
reads. `bug_io.read_bytes` alone resolves ancestors, so it is insufficient to
enforce the selected-project boundary against redirected parent components.
If a platform cannot enforce the boundary, return an unavailable diagnosis with
a safe explanation. Do not silently weaken safety to obtain a result.

Allowlist output fields. Do not emit raw config, commands, arguments, environment
maps, auth headers, URLs, exception messages or absolute source paths. Server
labels and operator text pass through the existing Redactor and terminal checks.
Evidence fingerprints derive only from the selected harness and redacted
findings exposed in the result. Never hash raw credential-bearing input files
for export. Formatting or secret-only changes that preserve the public findings
preserve this digest. Place it in finding text so the existing
metadata-insensitive importer can dedup.
Never emit these as scanner or verified-source identities. Findings always say
verification `not-run` and runtime availability `unknown`.

Next checks use static existing MCP preview commands with safely quoted selected
arguments. Do not execute those commands or present `--write` as already approved.
For malformed data, recommend review of the selected configuration. A reported
configuration difference is not sufficient evidence of a Brigade code defect.

No model/provider or native T3 launch, server connection, network lookup, runtime
probe, transcript/log access, config rewrite, service restart or issue posting.
This is deterministic assistance over local selected configuration. Memory-access
diagnosis and actual loaded-state evidence follow in a later bounded slice.

## Acceptance

- Missing selected entry: expected/observed explanation and an MCP sync preview.
- Matching config: configuration match, runtime unknown and no passing verification.
- Managed edits and catalog drift: distinct existing conflict/stale semantics.
- Excluded server, unsupported harness and absent catalog: accurate bounded result.
- Malformed, oversized, linked or escaping input: blocked before content escapes.
- Secrets and terminal controls in all three inputs or symptom: absent from output
  and exported finding/report. Another harness's config is never read.
- Default preview leaves repository and report/inbox/task/outcome state unchanged.
- Explicit export validates and ingests through the existing import boundary as a
  pending finding. Explicit report preparation remains local and unsubmitted.
- Existing MCP plan/sync/fidelity and bug-report consent regressions still pass.

## Grounding

Source baseline `083d4783`, refreshed from main in the existing worktree. No shell
worktree was created. The native T3 worker exception applies to implementation.
The graph sync indexed 1,869 files. The initial affected query for MCP comparison,
bug registration/reporting and redaction/read primitives returned 262 test files
with no missing source paths. Static call attribution supplies a lower bound.
Native #2046 adapter, parent rebrand/roadmap, E2EE and Hub paths remain separately
owned and unchanged by this slice.
