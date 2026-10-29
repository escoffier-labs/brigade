# Brigade Stack assisted diagnosis: first proof

Status: fixture-only design approved on 2026-10-28. Product investigation and
native activation remain outside the first proof.

Scope clarification: triage includes unexpected operator memory visibility and
MCP configuration drift, as well as code failures. Distinguish observed mismatch,
unavailable evidence and suspected code defect. A healthy configuration file
does not prove what an operator actually loaded. User-selected redacted evidence
may establish expected versus observed state. No private transcript or live MCP
inspection is authorized by this clarification. Code defects may use the existing
bug-report submission path after exact-preview consent. The first proof mocks
that transport and publishes nothing.

## User workflow

An operator describes a Brigade failure and what they expected. Brigade prepares
its existing local bug report with `brigade bug report --target <repo>
--summary-file <symptom-file> --via file --dry-run --json`. This step writes a
local bundle and receipt. It performs bounded diagnostics, not general Doctor
hooks. It is not a no-write operation.

The operator reviews the redacted bundle before giving it to an assistant.
The assistant asks which Brigade build and command failed, separates observed
checks from hypotheses, explains uncertainty, and proposes the smallest next
check. The first proof uses recorded assistant answers over fake bundles. It
does not launch a model. This is a Brigade diagnosis experience: a missing
profile or failed Brigade check is the case. T3 can be healthy during this flow.

The operator reviews a finding containing the symptom, affected component,
bundle digest, observations, hypothesis, uncertainty and proposed next check.
Every proposed action remains unapproved and verification remains `not-run`.
The reviewed finding can be represented as existing work-import JSONL:

1. `brigade work import validate <finding.jsonl> --json` checks its shape.
2. `brigade work import ingest <finding.jsonl> --target <repo> --dry-run --json`
   previews canonical inbox records without appending them. It may create empty
   inbox locking sidecars. Dry-run is not a guarantee of zero filesystem writes.
3. After explicit review, the same ingest without `--dry-run` appends a pending
   finding. It does not create a task, run a fix, submit a report or promote work.
4. Existing import show, plan, dismiss and promote controls remain separate
   operator choices. Public bug submission retains its existing exact-preview
   consent and receipt behavior. It is outside the first proof.

## Approach and existing seams

Use a fixture-first composition of existing bug and import behavior. A new
public command family and an automatic native launcher are unnecessary for this
proof. Importing only a T3 support summary would miss Brigade-specific diagnosis.
Live assisted execution can follow only after an operation-scoped runtime is
qualified by the native adapter owner and its authority is explicitly aligned.

At checkout `3311daea`, inspected seams are `bug_cmd._prepare/_report`,
`bug_collect.collect/collect_diagnostics`, `bug_redaction.Redactor`,
`work_cmd/imports.import_validate/import_ingest`,
`work_cmd/ledger/import_model._sanitize_untrusted_import_record`, and
`work_cmd/ledger/inbox_provenance._append_import_records`.
Pooled main comparison to `cf677d32` on 2026-10-28 showed these paths unchanged.
This source comparison is not runtime qualification.

The first implementation slice is one temp-repository integration test module
composing those existing entry points with recorded reviewed findings, plus
fake fixtures if keeping them inline impairs readability. No production helper
or schema is necessary until the proof exposes a missing behavior. Do not turn
the proposed input fields below into a new public contract by implication.

Use existing JSONL `kind: finding`, `source: assisted-diagnosis` as a declared
alias, and bounded text. Put the case label, redacted bundle digest, diagnosis
and explicit `verification: not-run` in text. Optional descriptive metadata may
repeat them, but never supplies trust or execution authority. The importer owns
the actual source (`learning-loop`), content identity, provenance and dedup.
Its canonical hash excludes metadata. Changed evidence must change the digest
in text to remain a distinct finding. Reimporting identical content skips only
when the existing row has proof accepted under the workspace authority posture.
Use the existing non-isolated fixture posture for the first dedup case. In an
external-key isolated workspace, unsigned canonical proof must not suppress an
incoming row. Preserve that security behavior instead of promising global dedup.
Neither a supplied scanner identity nor a claimed successful verification is
accepted as proof. Actual verification belongs to a separate Brigade receipt.

## Native boundary and ownership

The supplied research report inspected upstream `9826dbb` and the official
`npx t3 triage` command. It found installation investigation, session/context
writes and possible source preparation. `server.prepareTriage` returns a prompt,
not an inspected structured read-only diagnostic result. Prompt instructions
do not enforce permissions, spend limits or durable action idempotence.

The #2046 owner confirmed that V0 only qualifies supplied runtime
data. Matching declarations confer no admission, capacity, ownership or launch
authority, and execution remains disabled. Its owned paths are
`native_t3_contract.py`, `native_t3_observations.py`, their
`tests/test_native_t3_*.py` tests, and the phase-2046 design/plan documents.
No adapter/backend changes are proposed here. Source availability
and an installed version do not qualify a runtime. Missing native references
remain absent. A prompt path, child exit code or claimed diagnosis never becomes
successful verification. Record actual native version and environment/thread/
turn references only when supplied by a later qualified route.

Parent retains CLI/core-docs rebrand, engine rename and roadmap PR #2114.
E2EE #2005 and Hub #2058 retain their owners. This slice owns only the diagnosis
design and fixture composition. New scanners, database, thread managers,
provider launchers, ranking, schedules and MCP servers are outside scope.

## Smallest fixture proof

All execution uses temp Git repos, temp data roots, fake report state, denied
network transports and no inherited credentials. The existing bug collector
also consults global last-error/install state, so isolating only `--target`
is insufficient. Follow the test report environment and isolate those readers.

| Case | Observable assertion |
| --- | --- |
| Brigade symptom | Fake failed profile check produces report v1 and a local unsubmitted receipt. Recorded assistance cites the check and uncertainty. |
| Redaction | Fake token, user path and terminal control input cannot survive in the reviewed bundle or rendered finding. Security concerns stay blocked from public reporting. |
| Preview | Bug preparation writes only isolated report data. Import validate and dry-run leave the temp repo inbox and task state unchanged. Dry-run may create empty lock sidecars. No native preparation runs. |
| Identity | Supplied source, scanner provenance and identity cannot become trusted producer authority. Same content imports once under the non-isolated fixture posture. Unsigned isolated proof cannot suppress. Changed digest in text creates a distinct pending finding. |
| Verification | Recorded assistance and import metadata cannot create verification receipts or a passing outcome. The finding visibly says `not-run`. |
| Invalid input | Missing text, unsupported kind and invalid JSON fail validation without inbox writes. A prompt-only native preparation record is not a finding. |
| Review boundary | Ingest creates pending findings only. No task promotion, fix command, public submission, native session, installation or service restart occurs. |

Before test edits, refresh the code graph and record exact affected source/test
paths. Existing behavioral references include `tests/test_bug_report.py`,
`tests/test_bug_redaction.py`, `tests/test_bug_review_regressions.py`,
`tests/test_work_cmd_imports.py` and `tests/test_work_import_provenance_stamp.py`.
The initial graph sync indexed 1,855 files. An affected query for the six seams
above returned 365 test files at depth 3 with no missing source files. This is
a static lower bound, not a reason to expand the fixture into a source refactor.
Use the focused gate through Brigade for the new test module. Use the full
gate once for the later PR, then current-head CI, configured code/security
reviews and the protected merge route. Do not count this design as a proof run.

## Observed proof and held work

`tests/test_assisted_triage_workflow.py` supplies seven fixture tests for memory
visibility, MCP drift, a suspected code defect, reviewed imports and invalid
inputs. The code-defect case checks exact saved-preview consent with a mocked
issue transport. It does not publish an issue. The focused gate passed with
seven tests in receipt `20261028-213438-work-verify-702651` before review changes.
After review, direct decoded escape checks, actual outcome paths and an exact
dry-run lock-file allowance passed the focused gate with seven tests in receipt
`20261028-223647-work-verify-b2ce47`.

Design alignment authorizes the fixture composition above, not live model
execution. If a production extension is needed, return the observed gap and
smallest concrete diff for alignment first. Native operation qualification,
live logs/DB access, private transcript access, update installation, external
publication, site deployment and package publication remain held. Recorded
diagnoses are test inputs. Automatic investigation and actual loaded-memory/MCP
inspection remain unimplemented. The first proof establishes the existing
report/import composition, not the accuracy of an assistant's diagnosis.
