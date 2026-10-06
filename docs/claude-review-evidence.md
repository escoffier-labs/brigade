# Managed Claude review evidence

`brigade.claude_review_evidence` normalizes one already-retrieved GitHub
check run into a frozen, sanitized observation. It performs no network calls,
provider actions, credential access or persistence. This is the first bounded
slice of issue #1601. It is not wired into the CLI, Worklore or FleetHub yet.

## Provider contract

The official [Code Review check output documentation](https://code.claude.com/docs/en/code-review#check-run-output)
describes the `Claude Code Review` check, neutral completion even with findings,
and a final HTML comment containing severity JSON:

```html
<!-- bughunter-severity: {"normal": 2, "nit": 1, "pre_existing": 0} -->
```

`normal` counts Important findings. Annotations can retain findings when an
inline comment is rejected. Neutral completion alone does not establish zero
findings or approval.

Source retrieved on 2026-10-06. This is a retrieval date. The page did not
supply a publication date for this contract.

## Pure API

```python
from brigade.claude_review_evidence import normalize_check_run, parse_severity_marker

severity = parse_severity_marker(check_run["output"]["text"])
evidence = normalize_check_run(
    check_run,
    expected_app_id=trusted_app["id"],
    expected_app_slug=trusted_app["slug"],
    current_head=pr_head_sha,
    observed_at=observation_time,
)
```

The caller must establish the expected app identity independently of this
check run. There is no hardcoded official app ID. Both the positive integer app
ID and exact slug must match, and the name must be exactly `Claude Code Review`.
Missing or invalid identity, a mismatched app, GitHub Actions, or an invalid
check ID returns `None`. An ordinary installation test containing Claude in its
name cannot become managed review evidence. The Actions slug is rejected even
if supplied as the expected identity.

The caller supplies a full 40-digit hexadecimal PR head and a timezone-aware
ISO timestamp. Invalid caller head or observation time raises `ValueError`.
Malformed JSON-compatible provider fields are rejected, omitted or retained as
unknown without raising. The function does not mutate the input.

The observation retains provider/source, app ID/slug, immutable check ID,
reviewed and current head, observation time, provider start/completion times,
validated source URL, lifecycle, conclusion, coverage and severity evidence.
It omits raw output, summaries, titles and arbitrary provider fields. Timestamps
are normalized to UTC. Invalid provider timestamps and SHAs become `None`.

| Dimension | Values and interpretation |
| --- | --- |
| Lifecycle | `queued`, `in_progress`, `completed`, or `unknown`, from status only |
| Conclusion | Recognized GitHub execution conclusion or `None`, never approval |
| Parser disposition | `known` with counts, or `unknown` with a bounded reason and `None` counts |
| Coverage | `current` for an exact head match, `stale` for another valid head, `unknown` for an invalid/missing reviewed head |

`current_findings_known` requires completed execution, current coverage and
valid counts. `current_zero_findings` also requires all three counts to
be zero. Parsed counts may remain available for pending or stale observations,
so callers must use the current predicates before claiming current coverage.
Neutral does not change lifecycle, and a `COMMENTED` review state is not a
check conclusion. The observation contains no approval decision.

`identity_key` is `github:check-run:<app-id>:<check-id>`. Repeated reads of the
same provider object retain this key, while reruns with different check IDs
remain distinct. A delayed old-head completion stays stale even if it finishes
later than a current-head run. Callers own history and supersession. This module
does not create an evidence store or choose a winning review round.

## Parser and URL limits

The parser accepts exactly one marker on the final non-empty line. It rejects
duplicate or conflicting markers, duplicate JSON keys, malformed JSON,
non-JSON constants, missing or additional keys, and any value other than a
non-negative integer. Boolean, string, float and null counts remain unknown.
The only supported keys are `normal`, `nit`, and `pre_existing`.

Output is limited to 65,536 UTF-8 bytes, the final marker line to 512 bytes,
and each count to 2,147,483,647. Oversized or future schemas remain unknown
instead of being truncated into valid evidence. Invalid UTF-8 surrogate content
also remains unknown. IDs are positive integers at most `2**63 - 1`.

Source links accept only HTTPS `github.com` paths of the form
`/<owner>/<repo>/runs/<check-id>` or `/<owner>/<repo>/check-runs/<check-id>`,
with simple bounded repository segments and the same immutable check ID.
Credentials, explicit ports, controls, encoded path segments, query parameters,
fragments and other hosts or paths are omitted. Links are limited to 2,048
characters and are never fetched. A rejected details URL falls back to a
validated `html_url`. A valid link survives unknown severity output. This
conservative URL allowlist may omit other legitimate provider links.

## Remaining issue coverage

Issue #1601 remains open. Later integration must bind canonical repository and
PR identity, import annotations/reviews/comments, correlate duplicate finding
representations without adding their counts, and retain resolved-thread and
supersession history. It must also bound and paginate authorized reads, retain
truncation and 403/404/429/access-denied reasons, and expose evidence through
existing Worklore, Hub JSON and FleetHub readouts. No shared schema, registry,
daemon, CLI, branch protection or review-trigger behavior changes in this slice.

## Regression evidence

`tests/test_claude_review_evidence.py` uses synthetic offline fixtures for
neutral findings, valid zero counts, spoofed app identity, ordinary Actions
tests, malformed/future/oversized markers, rerun IDs, delayed stale completion,
unsafe URLs and sanitized malformed metadata. The owning boundary needs no
provider subprocesses or credentials. Run the focused project gate through
Brigade with `./scripts/verify-focused tests/test_claude_review_evidence.py` and
capture against `brigade-work`.
