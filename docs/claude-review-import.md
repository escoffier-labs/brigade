# Bounded managed-review collector

`brigade.claude_review_import.collect_managed_review` is the read-only collector
slice for issue #1601. It uses the existing pure normalizer and an injected
`GetTransport`. It performs no credential access, live network call, provider
mutation, persistence, review purchase, trigger, rerun or merge-policy decision.
The new module has no production callers yet.

## Verified GitHub contract

Official documentation was retrieved on 2026-10-06. These pages supplied no
publication date. The collector was verified with offline fixtures.

- [Check runs and annotations](https://docs.github.com/en/rest/checks/runs)
  document `GET /repos/{owner}/{repo}/commits/{ref}/check-runs`, its
  `total_count`/`check_runs` envelope, `filter=all`, and pagination with `page`
  and `per_page` up to 100. Check objects supply app ID/slug and head SHA.
  `GET /repos/{owner}/{repo}/check-runs/{check_run_id}/annotations` returns an
  array of location/level/message fields without universal annotation IDs.
- [PR reviews](https://docs.github.com/en/rest/pulls/reviews) document
  `GET /repos/{owner}/{repo}/pulls/{pull_number}/reviews`, immutable review IDs,
  commit SHA, submission timestamp and review state.
- [Inline comments](https://docs.github.com/en/rest/pulls/comments) document
  `GET /repos/{owner}/{repo}/pulls/{pull_number}/comments`, comment IDs, review
  IDs, current/original commit and line locations. The documented review and
  inline examples do not establish app identity. The collector therefore
  quarantines these records as `provider_identity=unverified`, even if an
  unsupported app field or convincing bot login is supplied. Their states
  remain observed metadata, never approval decisions.
- [Issue comments](https://docs.github.com/en/rest/issues/comments) support PR
  discussion comments at `GET /repos/{owner}/{repo}/issues/{issue_number}/comments`.
  The official [timeline example](https://docs.github.com/en/rest/issues/timeline)
  documents optional `performed_via_github_app` metadata on issue comments.
  Only when this metadata is present, matches the independently trusted ID/slug,
  and the provider actor type is `Bot`, is a discussion comment app-verified.
  Missing metadata is unverified. A discussion comment has no documented
  reviewed SHA, so its coverage stays unknown. Its body is never retained or
  parsed for findings.

No official Claude app ID is hardcoded. User login, display name, check-name
substrings and GitHub Actions cannot establish managed-review identity. Review
or inline identity integration remains open pending a supported independent
binding. Even genuine observed `APPROVED` metadata would need a separate merge
policy and current-head trust evaluation.

## API and transport boundary

```python
from brigade.claude_review_import import Limits, collect_managed_review

result = collect_managed_review(
    authorized_get_transport,
    owner=canonical_owner,
    repo=canonical_repo,
    pr_number=positive_pr_number,
    current_head=full_pr_head_sha,
    expected_app_id=independently_trusted_app_id,
    expected_app_slug=independently_trusted_app_slug,
    observed_at=timezone_aware_observation_time,
    limits=Limits(max_pages=20, max_requests=100, max_objects=1000,
                  max_bytes=4 * 1024 * 1024, per_page=100),
)
```

`GetTransport.get(path, params=..., max_bytes=...)` returns
`GetResponse(status, body_bytes, truncated=False)`. It must enforce the byte cap
while reading, use a supported GET route, disable redirects and arbitrary URL
following, and retain existing public Octopool/private routing without public
fallback. This slice supplies no production transport. Supplying an injected
transport does not authorize a provider call. Provider `url`, `annotations_url`,
`details_url`, Link headers and blob links never become request destinations.

Page limits apply per fixed endpoint. Request, object and byte budgets apply to
the entire invocation, including rejected objects, duplicates and error bodies.
Limits have hard ceilings and reject booleans, zero, negatives and excessive
values. The collector checks remaining budgets before each request and passes
remaining bytes to the transport. Oversized/truncated responses are discarded,
with accounted bytes capped at the granted budget and explicit truncation. The
transport is responsible for preventing excess wire reads. No retries occur.

All sources retain bounded endpoint, last HTTP status, pages, objects, bytes,
duplicate count, completion flag and independent reasons. HTTP 403, 404 and 429
produce `access-denied`, `unavailable` and `rate-limited`. they do not mean absent
reviews. HTTP denial and response truncation may coexist. Malformed JSON,
duplicate keys, non-JSON constants, unexpected envelopes, changing totals,
short pages before the declared total, repeated or overlapping check pages and exceeded budgets are
incomplete. A full last array page needs another page to prove exhaustion. if
the page cap prevents it, the result is conservatively incomplete.

## Records, redaction and history

Check records retain the normalizer's immutable identity, current/reviewed heads,
status/conclusion, parser disposition and strict documented severity counts.
Links are restricted further to the supplied repository and matching object ID.
A rejected foreign details link can fall back to a valid HTML link. PR links bind
the repository, PR and comment/review fragment. No arbitrary details fetch occurs.
Timestamps normalize to UTC. invalid/future provider times are omitted and marked
independently. Raw summaries, bodies, diff hunks, messages, transcripts, arbitrary
fields, author logins and transport exception text are discarded. Validated
relative file paths, positive line ranges and inline diff sides are retained for
location evidence.
Paths with traversal, controls, encodings or unsupported characters are omitted.

Check/review/comment objects deduplicate by immutable provider ID within their
source kind. Identical repeats do not add rounds. Conflicting observations of one
ID retain the first observation and mark incompleteness rather than silently
replace it. Distinct rerun check IDs remain distinct.

For the same reviewed head, a strictly later valid start timestamp supersedes an
earlier round. The next uniquely timed round supplies `superseded_by`. Missing
or tied timestamps keep ordering unknown. Completion time and event arrival
never establish supersession. Superseded or ambiguously ordered rounds cannot
claim current known/zero findings. Truncated check enumeration cannot establish
that the latest round was seen. A late old-head run stays stale regardless of its
completion time. `current_head` always remains the separately supplied PR head.
Only this invocation's observed rounds are ordered. Persisted cross-observation
history and fetching earlier heads belong to later integration.

Annotations retain parent check ID, observed ordinal, validated location,
annotation level, head coverage and parent action link. Their identity keys are
observation locators with `identity_kind=parent-ordinal-location`, not invented
immutable provider IDs. Malformed items preserve ordinal positions. Every
annotation explicitly reports the lack of a universal immutable ID. Repeated
pages stop with incompleteness. repeated locations remain separate observations.
Matching annotation or inline locations/heads are labeled possible duplicate
representations. Location alone never establishes equivalence, and unverified
inline authors never become Claude findings. `finding_totals` is always `null`.
severity counts come exclusively from each check's documented marker, never
from adding annotations and inline comments.

Neutral indicates execution completion. Current zero requires valid severity
counts, completed execution, an exact current head, valid timing/order and
complete check enumeration. It does not imply approval, annotation completeness,
thread resolution or permission to merge. `complete` describes this bounded
collection's source/metadata completeness only, not issue acceptance. Inspect
`remaining_acceptance` too.

## Remaining issue #1601 acceptance

- Establish supported independent app attribution for PR reviews and inline
  comments. Existing REST candidate records remain explicitly unverified.
- Collect GraphQL thread disposition and retain resolution history after pushes.
  `thread_disposition=not-collected` never claims resolution.
- Preserve cross-observation history and supersession in the existing evidence
  model. correlate representations only with sufficient source identity.
- Wire authorized public/private transports and persistence into existing
  Worklore/Hub, supported JSON and FleetHub coverage/findings readouts.
- Complete integration verification and required CI/review.
  This lane does not complete the issue or supply merge-policy approval.

## Verification

Run `./scripts/verify-focused tests/test_claude_review_import.py
 tests/test_claude_review_evidence.py` on one line. The offline fixtures cover
fixed request paths, budgets, repeated and overlapping pages, identity spoofing,
rerun supersession, uncertain ordering, redaction, access errors, malicious links,
and diff-side location ambiguity. Full repository verification remains required
before publication.
