# Proposal: a risk band in the route brief, independent of size

Status: draft design note for issue #1973. Not implemented. No gate changes
until the calibration step below has run.

## The gap

The route brief answers "which stages does this task need" and labels the
answer with a size (`router.size_label`, a count of routed stages: XS through
XXL). It has no separate answer to "how much damage does this change do if it
is wrong." A one-line change to a token check and a one-line typo fix can both
compose an S route (`implement`, `correctness-review`, `verify`), and both get
the same planning depth, the same reviewer pool, and no stop before dispatch.
`router.py`, `route_catalog.py`, and `docs/route-brief.md` contain no risk
axis today.

Size and risk are different questions. Size grows with the number of surfaces a
task touches. Risk grows with what a single surface can break: an auth bypass,
a leaked credential, a weakened signature check, a timeout that turns a slow
dependency into an outage, a rate limit that stops limiting, a migration that
loses rows, a destructive command. Folding risk into size would make every
small risky change look large and every large safe change look risky.

## Reference

forge 2.3.0 (alp82/forge, CHANGELOG entry dated 2026-07-26) judges the two
separately. forge is the renamed alp-river, the project `src/brigade/router.py`
is derived from. Its 2.3.0 entry says:

- Small, dangerous changes get a concise plan, an independent second look, and
  the user's sign-off.
- Large, safe changes get full planning and structural review with no sign-off
  stop.
- Small, safe changes keep the existing fast path.
- Every change is still reviewed for correctness and for delivering what was
  asked.

forge 2.2.2 (2026-07-21) also made its second opinion always come from a
different model than the one running forge. This note borrows both ideas.
nWave's rigor profiles address the same need and are noted as adjacent prior
art. Nothing here is ported from either project.

## Shape

A new pure function in `route_catalog.py`, `risk_band(task, signals,
changed_paths, overrides) -> RouteRisk`, called from `route_brief` after
`derive_signals`. It never reads the composed route or its size, and
`compute_route` never reads the band. Capture the original size before injecting
risk gate signals. The brief keeps that size label for classification and
calibration, even when a gate changes the runnable stages. Gates are a function
of the pair (original size, band), decided outside the router.

```python
@dataclass(frozen=True)
class RouteRisk:
    band: str                  # "routine" | "raised" | "critical"
    classes: tuple[str, ...]   # sorted risk classes that fired
    reasons: tuple[dict, ...]  # {"class", "source", "match"} per hit
    version: str = "brigade.route-risk.v1"
```

The band is the highest band any fired class maps to. No class fired means
`routine`. Combination rules (for example, two raised classes escalating to
critical) are left out of v1 and become a calibration question.

The word "band" already names skill maturity bands in `route_policy.py`
(`promoted`, `candidate`, `provisional`, `unseen`). The new field is always
spelled `risk` or `risk_band` in code and payloads so the two never collide.

## What sets each band

Two inputs feed the classes: signals `derive_signals` already produces, and
path classes matched on the changed paths the run already collects from git
(tracked modifications plus untracked files, capped at 200). Path classes reuse
the existing whole-segment matcher (`_path_segments`), so `author.py` never
fires auth and `tokenizer.md` never fires on token.

| Class | Band | Existing signal | Task keywords (new) | Path segments |
| --- | --- | --- | --- | --- |
| `destructive` | critical | `destructive-op` | (signal only) | none (destruction is an action) |
| `credentials` | critical | part of `auth-surface` | secret, password, credential, api key, private key, keyring | `secret`, `secrets`, `credentials`, `keyring` |
| `crypto` | critical | none | encrypt, decrypt, cipher, hmac, signature, signing, nonce, tls | `crypto`, `cryptography`, `signing`, `hmac`, `cipher` |
| `migration` | raised, or critical with a data verb | `migration` | backfill, drop column, delete rows, rewrite data | `migration`, `migrations`, `schema`, `schemas` (existing set) |
| `auth` | raised | `auth-surface` minus the credential hits | (existing auth pattern) | the existing `_AUTH_SEGMENTS` minus the credential entries |
| `timeouts` | raised | none | timeout, deadline, ttl, keepalive, retry budget | `timeout`, `timeouts`, `deadline`, `deadlines` |
| `rate-limits` | raised | none | rate limit, throttle, quota, backoff | `ratelimit`, `throttle`, or the adjacent segment pair `rate` + `limit(s)` |

Notes on the table:

- `auth-surface` stays exactly as it is and still pulls `security-review` and
  `needs-tests`. The risk function splits its evidence into `credentials`
  (critical) and `auth` (raised) by which pattern or segment matched. A task
  that only says "login" is raised. One that touches a `secrets/` path is
  critical.
- `_PATH_SPLIT` breaks `rate_limit.py` into `rate` and `limit`, so the
  rate-limit class needs an adjacent-pair match. The
  timeout and crypto classes must not fire on `hash` (content hashes are
  everywhere in this codebase) or on `keys` (dictionary keys), so neither is in
  the list.
- `.env`-style files split to the segment `env`, which also matches `env.py`
  and every `*_env` helper. v1 leaves it out, and calibration decides whether a
  narrower rule (exact basename `.env` or `.env.*`) earns its place.
- Docs-path tasks get no task-keyword classes, the same rule `derive_signals`
  already applies to everything except the ship and destructive signals. Path
  classes still apply: a "docs" task that dirties `src/.../auth/` is not a docs
  task, and the band should say so.
- `system` path tasks are not raised by path alone. `system-execute` already
  carries the destructive lock, and a band that fired on every machine change
  would train operators to ignore it.
- `ship-requested` does not raise the band. Shipping already has its own hold,
  and the band describes the change itself.

These classes line up with the repository policy that treats security,
authentication, rate-limit, crypto, and timeout literals as non-trivial even in
a leaf file.

### Overrides

`--route-signal` keeps working and moves the band as a side effect:
`~auth-surface` removes the auth and credentials evidence that came from that
signal, `+destructive-op` forces critical. A dedicated `--route-risk
<band>` flag sets the band directly. Raising is always allowed. Lowering a
computed `critical` needs the flag, is recorded with `"source": "override"`,
and appears in the brief text so a reviewer sees it. `--no-route` stays the
full opt-out: no signals, no band, no risk gates, recorded as an unrouted run.

## What each band opens

| | XS / S | M and larger |
| --- | --- | --- |
| routine | short path | today's behavior |
| raised | today's behavior, no short path, independent reviewer seat | same |
| critical | code routes: forced `plan` and plan approval, cross-vendor reviewer | same |

**routine, minimal size: the short path.** When the band is `routine`, the
size is XS or S, and no stage is held, Brigade skips the orchestrator's
planning round and synthesizes the single-worker plan the route already
implies (`implement` plus `verify` for code, `docs-edit` plus `verify` for
docs), with `correctness-review` kept for code. Every change is still reviewed
for correctness. The short path only removes the planning call and the
coverage retry. A routine task larger than S keeps the full plan.

**raised: no short path, independent reviewer.** No new stop. The review
stages in the route must be assigned to a different seat than the
implementer. Same vendor is allowed. The brief names the classes and the
matches that fired, so the reviewer knows where to look.

**critical: code plan approval and a reviewer from another vendor.**

The new plan approval hold applies only to code routes. Critical `docs` and
`system` routes retain their existing holds. This proposal adds no plan or
approval gate to `docs-edit` or `system-execute`. Their risk band is still
reported, and the reviewer requirement applies to review stages they include.
A broader approval gate needs a separate design covering those mutation stages.

1. For code routes, the catalog's `plan` stage subscribes to `risk:critical`
   in addition to `significant-build`, so critical code always plans, at any size.
2. For code routes, `implement` carries a lock `{"while": "risk:critical", "until":
   "plan-approved"}`, reusing the existing while/until machinery. The router
   already re-drops stages whose producer is held, so the first pass yields
   the plan and holds everything downstream.
3. The run writes the plan artifact, records its digest, and stops with a
   held status naming plan approval. `brigade run --approve-plan <digest>`
   resumes with `plan-approved` live. The digest binds the approval to the
   plan that was read: a re-planned run needs a fresh approval.
4. Every review stage in the route needs at least one assignment on a seat
   whose model family differs from the implementer's. Vendor comes from the
   model family (the same classification `outcome_cmd._model_family` already
   applies), not from the CLI adapter, because one adapter can front several
   vendors. A seat whose family resolves to `unknown` cannot satisfy the
   requirement.
5. If the roster has no admissible cross-vendor reviewer, the run fails closed
   before dispatch with a named reason. It never downgrades to a same-vendor
   reviewer silently.

The `risk:critical` and `plan-approved` topics are injected as live router
signals at compose time only. They are not added to `RouteBrief.signals`, for
the fingerprint reason below.

## How bands land in receipts

- `run.json` `route` payload: a new `risk` object
  (`band`, `classes`, `reasons`, `version`). Each reason carries `class`,
  `source` (`signal`, `task`, `path`, `override`), and `match` (the keyword or
  the path segment, never the full path, so receipts do not grow with large
  diffs). A `risk.gates` object records what the band opened and how it
  resolved: `short_path`, `plan_approval` (`held`, `approved`, approval
  digest), `reviewer` (implementer family, reviewer family, satisfied or the
  fail-closed reason).
- `route-decision.json`: a top-level `risk_band` next to `chosen_route` and
  `confidence`, under the existing `brigade.route-decision.v1` schema as an
  additive optional field.
- Outcome route manifest (`outcome_cmd.route_manifest`): gains `risk_band`.
  It is **not** added to `route_fingerprint`. Every record written before the
  band exists has none, so fingerprinting it would split every route class in
  two the day it ships, resetting the exploration counts `route_policy` keys
  on `route_class`. This follows the precedent `coverage` already set: in the
  manifest, out of the fingerprint. The risk classes stay out of `signals` for
  the same reason.
- The brief text gains one line after the size line, for example
  `Risk: raised (timeouts via path segment "timeout")`, and `brigade route`
  prints the same line, with the full object under `--json`.

## Calibrating before any gate changes

Bands ship in shadow first: computed, recorded, printed, gating nothing.

1. **Record what replay needs.** Today `run.json` stores derived signals but
   not the changed paths, so path classes cannot be replayed from old runs.
   Shadow mode records the path-class hits (class plus matched segment) at
   run time. Task-text classes can be replayed over historical runs from the
   stored task, signals, and overrides.
2. **Replay the existing corpus.** Run `risk_band` over every historical
   `run.json` route payload and every outcome record with a route manifest.
   Report the band distribution by path and size, and the class hit counts.
3. **Hand-label a stratified sample.** Every critical run, plus a fixed random
   sample of raised and routine runs. The label question: "if this change were
   wrong, would the damage justify a stop before dispatch?"
4. **Join with outcomes that already exist.** Runs whose receipts carry
   `findings:security` or `findings:migration`, runs followed by a revert or a
   fix of the same paths, and runs with a failed verify. A routine band on any
   of those is a miss to explain.
5. **Acceptance before a gate turns on** (proposed, to be confirmed against
   the real numbers): no hand-labeled critical change sits in the routine
   sample before the short path ships, and critical stays a small enough share
   of code runs that plan approval does not become a reflex click. The replay
   report states the observed share, and the threshold is set from
   that number.

The calibration report lives with the PR that turns on the first gate, so the
evidence and the behavior change are reviewed together.

## Rollout order

Ordered so that each step's failure mode is cheaper than the next one's.
A tightening gate that misfires costs friction. A loosening gate that misfires
costs a missed review, so
the short path goes last.

1. This note and the forge citation update (no behavior change).
2. Shadow band: `risk_band`, the `risk` payload, the brief line, the
   `route-decision.json` field, the manifest field, and the recorded
   path-class hits. Tests: table-driven class and band cases, a property test
   that the band never changes the composed route or size, and a fixture
   proving the route fingerprint is unchanged. These invariants apply to shadow
   mode. Once gates are enabled, tests distinguish the original brief size from
   the gated route's runnable-stage count.
3. Calibration report over the corpus and pattern fixes from it.
4. Critical cross-vendor reviewer requirement (adds review diversity, no stop).
5. Raised independent-seat requirement.
6. Critical code route plan approval hold and `--approve-plan`.
7. Routine short path, after the routine recall from step 3 is re-checked on
   the shadow data gathered since.

Steps 4 through 7 each ship behind a config switch that defaults off for one
release, then on.

## What this deliberately does not do

- No model call. The band is keyword and path heuristics, deterministic like
  the rest of the route brief.
- No change to the baseline size calculation: `size_label` and `compute_route`
  are untouched. The brief retains the original label captured before risk
  gates. Forcing `plan` and holding `implement` on critical code routes can
  change the gated runnable-stage count. That count does not replace the
  original size in the brief or calibration records.
- No replacement for the ship and destructive holds. Those stay as they are,
  and the band sits beside them.
- No re-banding after implementation. The band is computed once at plan time
  from task text and dirty paths. Re-deriving it from the real diff belongs to
  `route-recomposition.md`, and would let a diff that quietly touched a
  credential path raise the band before review.
- No numeric risk score and no per-file weighting. Three bands are easy to
  read in a receipt and easy to calibrate. A score would invite threshold
  tuning without labels to tune against.
- No security scanning. The band decides how much process a change gets. It
  does not look for vulnerabilities. `security-review` and the security seat
  still do that.
- No change to fleet admission, hub routing, or seat scoring.
- No change to the route fingerprint or to existing route classes.
