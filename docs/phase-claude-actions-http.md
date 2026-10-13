# Passive Actions HTTPS reads, issue #1602

## Scope and design

PRs #1608, #1626 and #1677 supplied offline diagnostics, run import and metadata
collection. This independently useful slice supplies an opt-in live transport
for the existing `GetTransport` protocol. The existing collectors keep their
offline defaults and independent workflow confirmation requirements.

Use only Python's standard library. Bind each transport to one canonical
repository on `https://api.github.com`. Accept an optional caller-supplied
GitHub bearer token in memory. Never discover credentials or read their stores.
Allow only the exact numeric workflow metadata, workflow runs, run-attempt,
attempt jobs and run artifacts endpoints the existing Actions collectors use.
Validate pagination and byte/time budgets before I/O. Force GET, reject
redirects, bound response reads, and preserve HTTP denial/unavailability/rate
limit status. Never download artifacts, logs or transcripts, retry, dispatch,
rerun, install an app, probe a Claude token, or post a comment. Error bodies and
exception text must not expose credentials. Caller owns authorization.

New files: `src/brigade/claude_actions_http.py` owns the transport;
`tests/test_claude_actions_http.py` owns transport contract tests. This document
owns usage and acceptance. No shared Hub, cloud, review or CLI module changes.
Module-size ratchets remain unchanged. Remaining issue scope: independent
workflow/YAML resolution and discovery, persistence, CLI and FleetHub readouts.

## Impact recorded before edits

`brigade code sync .` indexed 1,453 files and 36,042 symbols. Exact-path impact
queries were attempted, but path strings are not symbol names and returned
empty results. The affected query correctly reports the two new paths as
missing, so this is no proof of an empty impact set. Existing collectors and
the protocol module were queried separately. Their graph attribution includes
unrelated dynamically named symbols, so it is only a noisy lower bound.
Manual direct impact: the new transport imports `GetResponse` and satisfies
`GetTransport` from `claude_review_import`; existing `collect_actions` and
`collect_metadata` accept it without modification. Verify all five existing
Actions/review modules plus the new transport tests.

## Implementation recipe

1. Write transport contract tests before production code. Protect fixed GET
   routing, repository binding, redirects, HTTP status preservation, byte
   bounds, timeout/failure redaction, and integration with existing collectors.
   Existing fixture transports cannot detect live HTTP routing failures.
2. Capture the missing-feature failure through Brigade. No test-only production
   API is needed: test the public transport with an offline HTTP boundary.
3. Implement only the contracts proven by those tests, in a small module.
4. Run the focused gate through Brigade for the six related test modules.
5. Root reviews the diff, runs the full gate once with bounded slot waiting,
   obtains independent reviews, then owns PR, current-head CI and merge.

## Sources

[Workflow runs](https://docs.github.com/en/rest/actions/workflow-runs),
[workflow jobs](https://docs.github.com/en/rest/actions/workflow-jobs) and
[artifacts](https://docs.github.com/en/rest/actions/artifacts) were retrieved
2026-10-13 UTC. No publication date was supplied. PageForge was unavailable;
the web tool fetched these official pages. Actions read permission supports
metadata access; its success says nothing about Claude authentication readiness.

## Public usage

`GitHubActionsTransport` is an opt-in implementation of the existing
`GetTransport` protocol. The caller supplies the token and independently
confirmed workflow revision and run attempts. Omitting the transport from either
collector continues to use offline fixtures. No CLI live-read flag is added.

```python
from brigade.claude_actions_evidence import ConfirmedWorkflow
from brigade.claude_actions_http import GitHubActionsTransport
from brigade.claude_actions_import import collect_actions
from brigade.claude_actions_metadata import collect_metadata

repository = "example-org/example-repo"
head = "a" * 40
observed_at = "2026-10-10T12:00:00Z"
workflow = ConfirmedWorkflow(repository, 7, "b" * 40)
transport = GitHubActionsTransport(
    repository=repository,
    token=caller_supplied_token,  # Optional in-memory token. Caller owns access.
    timeout_seconds=10,
)
actions = collect_actions(
    transport,
    repository=repository,
    workflow_id=7,
    current_head_sha=head,
    observed_at=observed_at,
    confirmations={(11, 1): workflow},
)
metadata = collect_metadata(
    transport,
    run=confirmed_run,  # Caller-supplied ActionRunEvidence for this repository.
    current_head_sha=head,
    observed_at=observed_at,
)
```

The example's `caller_supplied_token` and `confirmed_run` come from the caller.
The transport performs no credential lookup. Passing `token=None` sends no
Authorization header. HTTPS uses standard-library certificate verification, enforced
on every request even when the process opted out globally, and
a private opener that disables ambient proxies and redirect following.

Only these routes under `/repos/{canonical_repository}/actions/` are accepted:

| Route | Parameters |
| --- | --- |
| `workflows/{workflow_id}` | None |
| `workflows/{workflow_id}/runs` | `page`, `per_page` |
| `runs/{run_id}/attempts/{attempt}` | None |
| `runs/{run_id}/attempts/{attempt}/jobs` | `page`, `per_page` |
| `runs/{run_id}/artifacts` | `page`, `per_page` |

Repository names are normalized to lowercase using the existing evidence
validator. Paths must use that canonical spelling. IDs are canonical positive
ASCII decimals bounded to signed 64-bit integers. Pagination values are integers
or canonical ASCII decimal strings from 1 through 100. All other routes, URLs,
embedded queries, fragments and parameters are rejected before I/O.

Each request uses explicit GET and a finite socket timeout, defaulting to 10
seconds and bounded to `(0, 300]`. This timeout applies to blocking socket
operations. It does not enforce a hard wall-clock deadline, bound DNS resolution,
or stop a peer that keeps sending bytes before each socket timeout. The existing
collectors reject late responses after the call returns. Callers needing a hard
wall-clock deadline must enforce it outside this transport.

`max_bytes` must be an integer from 1 through 1 MiB. Reads request at most
`max_bytes + 1` body bytes to detect truncation and return at most `max_bytes`
bytes with an explicit `truncated` flag, set only when the cap is exceeded. A
connection that ends before the advertised Content-Length (or mid-chunk) is an
incomplete read and raises the redacted `TransportError`. HTTP 200 metadata remains raw for the
existing collectors to validate and sanitize. Other statuses, including 403,
404, 429 and redirects, retain their status and return empty bodies. Error
cleanup cannot expose network exception text. Network and socket failures raise
`TransportError("metadata transport unavailable")` without the original exception
chain. The transport never follows provider links or download URLs in metadata.

## Acceptance

The new offline tests exercise the real urllib opener and HTTP response parser
through a fake HTTPS connection. They protect method, host and repository
binding, all five routes, invalid input rejection, pagination, socket timeout
propagation, content-length and chunked read caps, truncation, redirect refusal,
HTTP status preservation, error-body removal, exception redaction, cleanup
failures and caller token handling. Both existing collectors are exercised with
this transport, including prior run attempts, job metadata, artifact metadata
and sanitization of private names and download links. No live requests or
credentials are used by these tests.

Focused acceptance uses exactly:

```bash
brigade work verify run --target . --argv-json '["./scripts/verify-focused","tests/test_claude_actions_http.py","tests/test_claude_actions_import.py","tests/test_claude_actions_metadata.py","tests/test_claude_actions_diagnostics.py","tests/test_claude_actions_evidence.py","tests/test_claude_review_import.py"]' --capture brigade-work
```

Full verification, independent review, PR checks and merge belong to root.
