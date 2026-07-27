# Proposal: multi-dimension skill scorecards from verifier receipts (#502)

Status: draft, planning pass only. Not implemented.

Parent issues: [#502](https://github.com/escoffier-labs/brigade/issues/502) (scorecards),
[#503](https://github.com/escoffier-labs/brigade/issues/503) (dual-criterion promotion).

Schema contracts: verify receipt `brigade.work_verify_receipt` `schema_version: 2` after merged
[#563](https://github.com/escoffier-labs/brigade/pull/563); additive `schema_version` evolution from
pending [#562](https://github.com/escoffier-labs/brigade/pull/562) (`docs/receipt-schemas.md` on
`origin/pr-562`).

## Problem

Scalar Wilson promotion on a single `signal_value` column is not converging. The reported operator
snapshot had 1,601 outcome rows, 18 distinct artifact ids counted as scored, and zero promoted
artifacts. The ranker has little trustworthy per-skill evidence, and the ratchet has promoted
nothing.

[#502](https://github.com/escoffier-labs/brigade/issues/502) asks for verifier-authored,
multi-dimension scorecards with exploration caps. [#503](https://github.com/escoffier-labs/brigade/issues/503)
asks for promotion only when both effectiveness and utility pass. This proposal is executable without
re-derivation.

---

## Core contract (non-negotiable)

1. **Receipt-only scoring inputs.** Every dimension value and eligibility decision is projected
   exclusively from `brigade.work_verify_receipt` `schema_version: 2` fields. `run.json`, work
   closeout, manual `outcome record` rows, `context_eval`, route coverage, `suspected_noop`, and
   any other non-verifier source may be used only to **locate or audit** receipts. They must never
   supply dimension values or eligibility.

2. **Untrusted caller attribution.** `artifact_id` supplied at `brigade outcome capture` or
   `verify run --capture` is caller-controlled and must not be treated as the scored subject. Scoring
   requires an additive **verifier-authored `subject_binding`** on the verify receipt (per #562
   additive evolution).

3. **Verifier-owned declarations.** Subject ids, check roles, and required utility checks come from
   a tracked verifier manifest or fixture manifest selected by Brigade. A CLI caller may choose a
   registered manifest id, but cannot supply or override its scoring fields. Ad hoc `--command`
   verification remains useful evidence for humans but is not scoreable.

4. **Fail closed on missing contracts.** Until `subject_binding`, v2 patch binding, verifier-owned
   `check_role`, and (when applicable) [#474](https://github.com/escoffier-labs/brigade/issues/474)
   failure taxonomy are present on a receipt, that receipt is **unattributed and non-scoring** -
   audit-visible only, never a negative skill result.

---

## Why promotion stays near zero today (file:line evidence)

These are the **actual** mechanisms in current code, not hypothetical gaps.

| # | Mechanism | Evidence |
| --- | --- | --- |
| 1 | **`scored_artifact_count` is not a scorecard** | `outcome_health` sets `scored_artifact_count` to `len(_scores_by_artifact(records))`, which is the number of distinct `artifact_id` values with any parsed outcome rows (`outcome_cmd.py:1590-1622`). It does not measure verifier-attributed trials or Wilson readiness. |
| 2 | **Capture is explicit and often omitted** | Verify does not append ledger rows unless the operator passes `--capture` or runs `brigade outcome capture`; the inline path is opt-in (`verification.py:1175-1213`). `outcome_loop_half_fed` warns when verify runs exist but `record_count == 0` (`outcome_cmd.py:1598-1607`). |
| 3 | **Rank is read-only** | `brigade outcome rank` projects existing ledger rows; it does not advance promotion (`outcome_cmd.py:943-958`). |
| 4 | **Promotion requires explicit reconcile apply** | `reconcile` is dry-run by default; only `--apply` writes decision receipts, advances `status.json`, and runs install/rollback (`cli/outcome.py:38-46`, `cli/outcome.py:142-145`, `outcome_cmd.py:1213-1304`). No internal caller invokes reconcile with `apply=True`. |
| 5 | **`promoted_count == 0` means status never advanced** | `promoted_count` counts artifacts whose persisted status is `promoted` (`outcome_cmd.py:1595`). Zero therefore means `reconcile --apply` has not successfully advanced any artifact to `promoted`, not that scoring is unimplemented. |
| 6 | **Caller `artifact_id` pollutes attribution** | `capture` accepts any caller-supplied `artifact_id` (`outcome_cmd.py:1066-1074`); the documented work loop convention captures `brigade-work` (`docs/agents-guide.md:97`). Per-skill ledgers stay thin even when the loop runs. |
| 7 | **Scalar score hides partial competence** | `rank_score` blends one `outcome` Wilson term (`outcome.py:492-510`). Brief-hit and graph deltas are display-only for reconcile (`outcome_cmd.py:1386-1387`). |

**Net:** the reported 1,601 ledger rows can exist while **zero artifacts** reach
`promoted` because attribution is caller-driven, capture is manual, and reconcile apply has not
been run successfully. Scorecards must not paper over this by joining untrusted `artifact_id` to
receipts.

---

## Verifier-authored `subject_binding` (additive, #562)

Scoring attribution must come from the verify receipt, not capture-time arguments.

### Shape (additive within `brigade.work_verify_receipt` v2+)

```json
{
  "subject_binding": {
    "binding_mode": "patch_backed | fixture_eval",
    "artifact_kind": "skill",
    "artifact_id": "skill-x",
    "content_fingerprint": "sha256:…",
    "producer_binding": {
      "work_session_id": "…",
      "owned_delta_sha256": "…"
    },
    "verifier_identity": {
      "verifier_id": "registered-verifier-id",
      "session_id": "…"
    },
    "patch_binding": {
      "baseline_commit": "…",
      "tree_fingerprint": "…",
      "changes_patch_sha256": "…",
      "subject_path": "skills/skill-x/SKILL.md",
      "subject_hash": "sha256:…"
    },
    "fixture_binding": {
      "manifest_id": "…",
      "case_id": "…",
      "check_id": "…"
    }
  }
}
```

Only one of `patch_binding` or `fixture_binding` is populated, selected by `binding_mode`.

### Patch-backed trials (`binding_mode: patch_backed`)

A receipt is **attributed and scoreable** only when all hold:

- Non-null v2 identity tuple: `baseline_commit`, `tree_fingerprint`, `changes_patch_sha256` (#563).
- `changes_patch_sha256` equals SHA-256 of on-disk `changes.patch` and patch is **non-empty**.
- `subject_binding.patch_binding.subject_path` and `subject_hash` are bound to the patch (verifier
  computes both; caller cannot override).
- `subject_binding.artifact_kind`, `artifact_id`, and `content_fingerprint` match the skill text
  the verifier bound at verify time.
- `producer_binding` proves the subject-path delta belongs to the work session being scored. A
  dirty tree owned by another session, or a session with no owned subject delta, is ineligible.
- For a generated patch governed by #507, `verifier_identity.session_id` differs from the producing
  session and the registered verifier manifest is independent of the patch producer.

### Fixture / evaluation trials (`binding_mode: fixture_eval`)

A receipt is **attributed and scoreable** only when all hold:

- Verifier-owned `manifest_id`, `case_id`, and `check_id` (stable identifiers from the evaluation
  harness, not caller labels).
- `subject_binding.content_fingerprint` for the skill under test.
- `artifact_kind` and `artifact_id` authored by the verifier from the fixture manifest.
- No worktree mutation is required. This is the only scoreable no-patch mode, so a read-only
  session forced to capture an ad hoc git-status check does not qualify.

### Until `subject_binding` ships

Every existing and new verify receipt without `subject_binding` is **unattributed and
non-scoring**. Do not join caller-supplied `artifact_id` from `records.jsonl` to invent attribution.

---

## Verifier-owned check roles (#503, #562)

Utility retention cannot be inferred from duration, graph deltas, or caller metadata. Each scored
command outcome on a verify receipt carries an additive verifier-owned classification copied from
a tracked verifier or fixture manifest:

| Field | Purpose |
| --- | --- |
| `check_role` | `effectiveness` or `utility_guardrail` |
| `check_id` | Stable verifier id for the command/check (e.g. `verify.ruff`, `guardrail.no-regression`) |
| `obligation_id` | Optional stable id when the check maps to a declared skill obligation (#499 advisory corpus) |

Rules:

- **Effectiveness** checks measure whether the skill's exercised work passes independent verification
  (`status`, `exit_code`).
- **Utility guardrail** checks measure retention constraints the skill declares (e.g. tests still pass,
  forbidden paths untouched), defined by verifier manifest rather than heuristics.
- Promotion requires **all required `utility_guardrail` checks passing** on attributed trials **and**
  the effectiveness criterion below. Missing `check_role` on a command makes the receipt **ineligible** (fail
  closed), not a skill failure.
- Do **not** invent utility from `median_duration_s`, `code_graph_delta`, `brief_hit_rate`, or
  `no_op` ratios. Those are not receipt-contracted utility signals.

---

## Score dimensions (receipt-only)

Four dimensions, each populated **only** by projecting `brigade.work_verify_receipt` v2+ fields.
No model self-report, no manual `outcome record` without a physical verify `evidence_ref`, no
LLM grading.

### D1 - `effectiveness` (correctness axis for #503)

**Question:** Did independent verification pass for the verifier-bound subject?

| Input field | Receipt source | Rule |
| --- | --- | --- |
| `status` | verify receipt | Confirms the receipt finalized. It does not override per-command roles; a utility-only failure does not become an effectiveness failure. |
| `commands[].status`, `commands[].exit_code`, `commands[].check_role` | same | All effectiveness commands completed with exit `0` means +1. A completed effectiveness command with non-zero exit means -1. Rejected, timed-out, or interrupted commands require typed classification and otherwise make the trial ineligible. |
| `digests.receipt_sha256` | same | Dedup key with `subject_binding` + normalized planned command list |
| `subject_binding` | verify receipt (additive) | Trial **ineligible** when absent or incomplete |
| `baseline_commit`, `tree_fingerprint`, `changes_patch_sha256` | verify v2 (#563) | Required for `patch_backed`; incomplete tuple → ineligible |
| `reused_from` | verify v2 | Retry-stability cohort key (see D3); does not bypass subject binding |

**Aggregate:** Wilson lower bound over deduped attributed trials (same math as `wilson_lower_bound`,
`outcome.py:107-120`), per `subject_binding.content_fingerprint` cohort.

**Not inferable today:** failure honesty, work quality, obligation coverage beyond explicit
`utility_guardrail` checks.

### D2 - `verifier_cost` (latency axis)

**Question:** How expensive was independent verification for this subject?

| Input field | Receipt source | Rule |
| --- | --- | --- |
| `duration_seconds` / receipt wall time | verify receipt | Per-trial verifier latency |
| `commands[].duration_seconds` | same | Per-command verifier latency |
| `reused_from` | verify v2 | Reuse flags for stability context only |

**Explicitly excluded:** model/task token cost, worker runtime, `run.json` durations, provider
billing. Those are not `brigade.work_verify_receipt` fields and must not appear in scorecards.

**Aggregate:** Median and p95 verifier duration per fingerprint cohort. Display and router budget
hints only. It is **not** a promotion substitute or a #503 utility proxy.

### D3 - `retry_stability`

**Question:** Does the same subject + patch + planned verification behave consistently across retries?

| Input field | Receipt source | Rule |
| --- | --- | --- |
| `subject_binding` + v2 patch tuple | verify receipt | Cohort key |
| Normalized planned commands | verify receipt `commands[].command` (ordered) | Cohort key |
| `reused_from` | verify v2 | Marks cached reuse. Reuse is deduplicated and never counts as an independent pass or retry. |
| `status`, effectiveness command outcomes | same | Pass/fail flip across sequence |

**Aggregate:** First-pass yield, fail-to-pass transition count, and outcome-flip rate for attempts
sharing the same subject fingerprint, patch identity, check id, and normalized planned commands.
Eligibility requires a full binding tuple and attributed subject.

### D4 - `evidence_integrity` (eligibility / audit only)

**Question:** Is this receipt structurally complete enough to enter any scored dimension?

| Input field | Receipt source | Rule |
| --- | --- | --- |
| `subject_binding` present and valid | verify receipt | Required |
| v2 patch binding valid (patch-backed) or fixture binding valid (fixture_eval) | verify receipt | Required |
| `check_role` on every scored command | verify receipt | Required |
| [#474](https://github.com/escoffier-labs/brigade/issues/474) failure taxonomy present when command/receipt failed | verify receipt | Required to classify infrastructure vs skill failure; absent or unknown means ineligible, not -1 |
| `digests.receipt_sha256` | verify receipt | Tamper-evident completeness |

**Aggregate:** `eligible_trials / audit_trials` (simple ratio; no Wilson). D4 gates D1–D3; it is
**not** a promotion lever. Obligation gaps from [#499](https://github.com/escoffier-labs/brigade/issues/499)
may appear in audit reports but do not supply dimension values.

**Audit-only (never scoring inputs):** `run.json`, work closeout, manual outcome rows, `context_eval`,
route coverage, `suspected_noop`. These may locate receipts for human review only.

### Scorecard record shape (new, additive)

Per verifier-bound subject, per policy version, derived on read:

```json
{
  "schema_version": 1,
  "policy_version": "scorecard.v1",
  "subject": {
    "artifact_kind": "skill",
    "artifact_id": "skill-x",
    "content_fingerprint": "abc…"
  },
  "dimensions": {
    "effectiveness": {"helped": 2, "hurt": 0, "wilson": 0.34, "trials": 2},
    "verifier_cost": {"median_s": 12.4, "p95_s": 41.0, "trials": 2},
    "retry_stability": {"consistent": 2, "sequences": 2, "rate": 1.0},
    "evidence_integrity": {"eligible": 2, "audit": 2, "ratio": 1.0}
  },
  "utility_guardrails": {
    "required_check_ids": ["guardrail.tests-green"],
    "passing_trials": 2,
    "required_trials": 2
  },
  "exploration": {"band": "candidate", "route_authority": "read_only"}
}
```

Persist `policy_version` and dimension summaries on **routing receipts** (`route-decision.json`,
`route_receipts.py:16-56`) as optional `score_inputs` (additive within `brigade.route-decision.v1`
per #562 rules).

---

## Infrastructure failure exclusion ([#474](https://github.com/escoffier-labs/brigade/issues/474) dependency)

Infrastructure failures are excluded **only** when the verify receipt carries verifier-authored
typed failure metadata from the #474 command/receipt taxonomy (e.g. `failure_class`,
`failure_kind` on receipt or command). Rules:

- Taxonomy hit on an otherwise failed trial makes it **ineligible** (neutral), not `hurt`.
- Taxonomy absent or unknown on a failed trial also makes it **ineligible** (fail closed), not `hurt`.
- Never map infrastructure failures to negative skill scores.

[#474](https://github.com/escoffier-labs/brigade/issues/474) is a **hard dependency** for slice 1.
Until it lands, failed trials without taxonomy remain ineligible rather than scored.

---

## Exploration caps (route-level, concrete)

Unproven skills must not gain broad write authority. Caps govern **router assignment**, not scoring
trial volume or Wilson denominators.

| Policy constant | Default | Behavior |
| --- | --- | --- |
| `EXPLORATION_ASSIGNMENT_CAP` | `2` per route class per 7-day rolling window | Max exploratory skill **assignments** (not scored trials) |
| `EXPLORATION_ASSIGNMENT_PCT` | `10%` of eligible assignments per route class per window | Enforce the concrete quota below |
| `ONE_EXPLORATORY_SKILL_PER_ROUTE` | `true` | At most one non-promoted skill receives exploratory write authority per composed route |
| `EXPLORATION_DECAY_DAYS` | `7` | Rolling window reset for assignment counts |
| `EXPLORATION_HARD_CEILING` | `5` assignments per skill per route per 30 days | Safety ceiling regardless of band |
| `ROUTER_TOKEN_BUDGET` | optional | When work budget telemetry exists, caps exploratory **routing** only, never a score dimension |

The route class is the existing low-cardinality route fingerprint (path, size, sorted signals). For
each route class and 7-day window:

```
quota = min(2, max(1, floor(0.10 * max(1, eligible_assignment_count))))
```

The bootstrap allowance of one lets an unseen skill receive shadow evaluation when the route class
has no history. The hard ceiling of five assignments per skill per route class in 30 days applies
even after 7-day counters decay.

**Never:** cap scoring trials, freeze Wilson numerators/denominators, or stop logging verify receipts
for attribution/backfill.

### Bands (router authority)

| Band | Effectiveness gate | Route authority | Router behavior |
| --- | --- | --- | --- |
| `unseen` | 0 attributed trials | `shadow` | May accompany one proven route in read-only fixture evaluation; never the sole route provider |
| `candidate` | ≥1 independent fixture/effectiveness pass | `scoped_write` | May act on reversible tasks inside verifier-manifest file globs under the exploration quota |
| `provisional` | ≥2 independent effectiveness passes but utility coverage incomplete | `scoped_write` | Same scope; receives no priority boost |
| `promoted` | Dual criterion (#503) | `full` | Elevated router eligibility/priority (see below) |

Demotion: any attributed D1 `hurt` on a `promoted` subject causes **immediate removal of broad routing
authority**; physical rollback/install side effect retained where applicable (`revert_min_hurt = 1`,
`outcome.py:426-427`).

---

## Promotion / demotion: fold #503

**Decision:** [#503](https://github.com/escoffier-labs/brigade/issues/503) is an **included slice**
of this arc, not a follow-on PR.

### Effectiveness criterion (D1, `check_role: effectiveness` only)

```
effective := effectiveness.wilson >= EFFECTIVE_WILSON_MIN (default 0.15)
          AND effectiveness.helped >= install_min_helped (default 2)
          AND effectiveness.hurt == 0
```

Applied to **attributed, eligible** trials only.

### Utility criterion (verifier `utility_guardrail` checks only)

```
utility := for every required obligation_id / check_id in the skill manifest:
              at least 2 independent current-fingerprint evidence units pass
           AND no current-fingerprint evidence unit has a trusted failure
```

An independent evidence unit is keyed by subject fingerprint, binding mode, patch tree or fixture
case, and check id. Retries and `reused_from` copies of the same unit do not increase the count.
No duration thresholds. No graph-change thresholds. No `no_op` ratios.

### Promotion semantics

Promotion changes **deterministic router eligibility and priority** first. Physical skill
install/uninstall remains a side effect where applicable (`outcome_cmd.py:1269-1273`), but the
authoritative outcome is routing authority:

- **Promote:** subject enters the `promoted` band with full route authority and a priority boost.
- **Demote:** subject loses broad routing authority immediately and returns to `candidate` or `unseen`; rollback
  may run but routing restriction is not gated on install success.

`decide()` accepts a `ScorecardDecision` struct; `reason` strings name the failed criterion
(`"withheld: utility_guardrail guardrail.tests-green"`).

---

## Cold start and backfill

### Cold start (no history)

- Registry skills start `unseen` with empty dimensions.
- First **attributed** verify receipt moves to `candidate`.
- `brigade outcome rank` shows dimensions with `n=0` explicitly.

### Backfill (existing receipts)

Read-only projection:

```
brigade outcome backfill scorecard [--target PATH] [--json]
```

1. Walk `.brigade/work/verify-runs/*/receipt.json`.
2. For each receipt, evaluate `subject_binding`, v2 patch binding, and `check_role` presence.
3. Emit `{eligible, ineligible_by_reason, unattributed}` with **no** join to `records.jsonl`
   `artifact_id`.
4. **Do not** auto-append outcome rows. The reported 1,601 ledger rows remain **audit-only** unless
   re-materialized from newly attributed receipts in a later explicit slice.

Pre-binding receipts: permanently **non-scoring** in backfill reports (`reason: missing_subject_binding`).

---

## Operator surface

| Surface | Change |
| --- | --- |
| `brigade outcome rank` | Default columns: effectiveness Wilson, verifier cost median, integrity ratio; `--json` adds `dimensions` + `utility_guardrails` |
| `brigade outcome explain <id>` | Per-dimension trial trail keyed by `subject_binding`; ineligible reasons |
| `brigade outcome reconcile` | Decision lines cite effectiveness vs utility criterion (#503); JSON `score` carries four dimensions |
| `brigade work brief` | `outcome_loop` adds `dimension_summary` + `exploration_bands` (`briefing.py:491-498`) |
| `brigade operator checkup --surface outcome` | Loop fed, ineligible receipt rate, promoted/provisional/candidate counts, unattributed receipt count |
| `route-decision.json` | Additive `score_inputs` + `policy_version` (`route_receipts.py:59-78`) |

---

## Non-goals

- **LLM-as-judge** for any dimension.
- **Scoring inputs from** `run.json`, closeout, manual outcome rows, `context_eval`, route coverage,
  or `suspected_noop`.
- **Caller `artifact_id` attribution** or ledger joins to invent subjects.
- **Duration / graph-change utility proxies** for #503.
- **Blocking verify** on obligation audit ([#499](https://github.com/escoffier-labs/brigade/issues/499) stays advisory).
- **Capping scoring trials** or Wilson updates for exploration.
- **Fleet-wide cross-repo score fusion**.
- **Replacing `brigade model scorecard`**.

---

## Related issues (context)

| Issue | Relationship |
| --- | --- |
| [#503](https://github.com/escoffier-labs/brigade/issues/503) | Dual-criterion promotion, included slice 4 |
| [#507](https://github.com/escoffier-labs/brigade/issues/507) | Requires producer/verifier independence for generated-patch receipts before scorecard eligibility |
| [#499](https://github.com/escoffier-labs/brigade/issues/499) | Obligation ids for `utility_guardrail` manifest; advisory audit |
| [#546](https://github.com/escoffier-labs/brigade/issues/546) | No-work hook class; parallel hardening, not a score dimension |
| [#557](https://github.com/escoffier-labs/brigade/pull/557) | Adversarial fixtures for eligibility/taxonomy tests |
| [#560](https://github.com/escoffier-labs/brigade/pull/560) | Capture-before-retry; audit ordering, not a scoring source |
| [#562](https://github.com/escoffier-labs/brigade/pull/562) | Additive schema evolution (`subject_binding`, `check_role`) |
| [#563](https://github.com/escoffier-labs/brigade/pull/563) | Verify v2 patch identity tuple |
| [#474](https://github.com/escoffier-labs/brigade/issues/474) | Typed infrastructure failure taxonomy, an eligibility dependency |

---

## Implementation decomposition (S/M slices)

Ordered. Each slice is independently mergeable.

### 1. `subject_binding` + `check_role` on verify receipts (M)

**Scope:** Additive verify receipt fields; pure `project_trial(receipt)`; #474 taxonomy gate.

**Acceptance criteria:**

- [ ] `brigade work verify` writes `subject_binding` for patch-backed runs with non-empty `changes.patch`
  and bound `subject_path`/`subject_hash`.
- [ ] Fixture/eval harness writes `binding_mode: fixture_eval` with verifier-owned `manifest_id`/`case_id`/`check_id`.
- [ ] Every planned command carries `check_role` (`effectiveness` or `utility_guardrail`) and stable `check_id`.
- [ ] Subject, producer ownership, check roles, and required utility checks are copied from a
  tracked verifier manifest. Ad hoc `--command`, `--capture`, or caller labels cannot set them.
- [ ] Patch-backed receipts bind `producer_binding` to the work session's owned subject delta.
  No owned delta, a concurrent session's delta, or an empty patch is ineligible.
- [ ] Generated-patch fixtures covered by #507 require a verifier session distinct from the
  producer session.
- [ ] Receipts missing `subject_binding` or `check_role` make `project_trial` return
  `eligible: false` with a stable reason.
- [ ] #474 taxonomy classifies infrastructure failures as ineligible. Missing taxonomy on failure
  is also ineligible (fail closed).
- [ ] Fixtures from #557 cover patch mismatch, missing binding, and taxonomy gaps.

### 2. Receipt-only dimension projection + `rank` / `explain` (M)

**Scope:** `scorecard.py` pure module; wire `rank` and `explain`; no promotion changes.

**Acceptance criteria:**

- [ ] `brigade outcome rank --json` emits `effectiveness`, `verifier_cost`, `retry_stability`, `evidence_integrity` per attributed subject.
- [ ] No dimension reads `run.json`, closeout, `context_eval`, route coverage, or `suspected_noop`.
- [ ] Subjects with only ineligible receipts show `trials: 0` and `ineligible_summary`.
- [ ] `explain` lists per-receipt eligibility verdict keyed by `subject_binding`, not caller `artifact_id`.

### 3. Route-level exploration caps + `score_inputs` on route receipts (S)

**Scope:** Band classifier; router assignment caps; `write_route_decision` embeds `score_inputs`.

**Acceptance criteria:**

- [ ] `classify_band(scorecard)` returns `unseen|candidate|provisional|promoted`.
- [ ] An unseen skill can receive one read-only shadow evaluation beside a proven route, but can
  never be the sole route provider.
- [ ] Per route class, tests enforce `min(2, max(1, floor(0.10 * max(1, eligible))))` in the
  7-day window, one exploratory skill per route execution, and the five-assignment 30-day ceiling.
- [ ] Scored trial count and Wilson denominators are unchanged by exploration caps (tests assert).
- [ ] Route receipt contains `score_inputs` with band + effectiveness Wilson for each skill in `chosen_route`.

### 4. Dual-criterion `decide` + router promotion, folding in #503 (S)

**Scope:** Extend `decide()` and `reconcile`; router eligibility/priority on promote; demotion drops route authority immediately.

**Acceptance criteria:**

- [ ] Promotion requires `effective AND utility`, where each required utility check has two
  independent current-fingerprint passes and no trusted failure.
- [ ] Retry or `reused_from` copies of the same patch or fixture case do not satisfy the
  two-independent-evidence requirement.
- [ ] Fixture with high Wilson but failing `utility_guardrail` is held with
  `withheld: utility_guardrail <check_id>`.
- [ ] Promote updates router eligibility/priority deterministically before/alongside physical install.
- [ ] Demotion on single attributed `hurt` removes broad routing authority regardless of utility; rollback side effect retained.
- [ ] `brigade outcome fork` previews dual-criterion under `--utility-check-*` overrides.

### 5. Read-only backfill + operator surfaces (S)

**Scope:** `brigade outcome backfill scorecard`; `work brief` and `operator checkup --surface outcome`.

**Acceptance criteria:**

- [ ] Backfill over tmp repo reports `{eligible, ineligible_by_reason, unattributed}` without mutating `records.jsonl`.
- [ ] No join from receipts to ledger `artifact_id`; the reported 1,601 legacy rows are
  documented as audit-only.
- [ ] `brigade work brief` shows `exploration_bands` and unattributed receipt count.
- [ ] `operator checkup --surface outcome` WARN when `outcome_loop_half_fed` or >50% receipts ineligible (missing binding/taxonomy).

---
