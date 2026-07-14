# The runtime-context blind spot

## The problem

The outcome ledger fingerprints a skill or card by hashing its content: SKILL.md, the whole skill bundle, and a card's transitively-linked cards. A signal (a real verify exit code) stops vouching for that artifact once the content changes. That closes the "score keeps crediting text that no longer exists" gap for the artifact's own files.

A content hash sees the files an artifact is made of. It does not see the runtime harness the artifact executes inside. Two verify runs with a byte-identical `content_fingerprint` can have different true outcomes because the context changed:

- a different model executing the skill (opus-4.8 vs a downgrade),
- different tools or MCP servers available,
- other skills loaded alongside (interaction effects),
- external dependencies the skill calls (a script, an API version, a service, env vars),
- the task itself differing run to run.

So the ledger can keep crediting or blaming a skill's text for outcomes that were really driven by the environment. This is the same caveat CocoIndex documents for undecorated helpers: the fingerprint sees the decorated logic, not what the logic reaches into.

## What three models converged on

The design below is the consensus of independent proposals from Claude Opus, xAI Grok, and OpenAI GPT-5.2, each given the same brief. Where they agreed:

1. **Context is a separate cohort axis, never folded into `content_fingerprint`.** Folding model or tool identity into the content hash would zero every artifact's score on a single model upgrade. Context gets its own fingerprint and its own cohort split, reusing the pure-fold pattern already in `split_by_fingerprint`.
2. **Capture it from what Brigade can observe, not from the model.** Same integrity posture as exit codes. On the orchestrated run path the `(cli, model)` are known from the roster. On the verify-capture path Brigade runs only the test command, so the executor model is not directly observable; capture what Brigade computes itself (its own version, interpreter, platform) plus a documented env allowlist, and mark everything else absent.
3. **Capture coarse, not exact.** Model *family*, a sorted tool/MCP digest, interpreter major.minor, platform. Not the task text (unbounded cardinality, guarantees n=1 cohorts), not env-var values (churn and secret-leak risk into an append-only ledger), not git or working-tree state (splits cohorts to death), not deep runtime tracing.
4. **Degrade gracefully.** Unknown context must never zero a score. Records with no manifest stay a grandfathered cohort, exactly like pre-fingerprint records.
5. **Recency and shrinkage handle what context cannot be observed.** A global recency half-life lets credit earned under a drifted-away environment fade without rewriting history. Thin cohorts shrink toward the pooled rate.

### The failure mode they all named, and its fix

Exact context cohorts fragment to n=1: every run is unique, so an exact `(content, context)` cohort never accumulates enough samples to score. GPT-5.2's answer, adopted here: cohort on a **low-cardinality capability vector** derived from the manifest (harness, model family, interpreter major.minor, platform), and fall back exact capability cohort -> pooled current-fingerprint cohort when a capability cohort is thin. Coarse buckets accumulate; exact contexts do not.

### The one real disagreement

Grok listed counterfactual or uplift scoring under "do not build" because it requires a judge. GPT-5.2 rebutted with **paired attribution runs**: on a small deterministically-sampled fraction of captures, run the verify twice, once with the artifact and once with a baseline, and record `delta = +1 if the artifact-arm passes and the baseline-arm fails`. That is a real counterfactual with no judge, only two exit codes. It is the only proposed mechanism that attacks attribution rather than stratifying it. The cost is real (it doubles verification on sampled runs and needs a disposable workspace), so it is deferred to Phase 3 and gated to safe commands.

## Phased plan

### Phase 1 (this milestone): capture and surface, no scoring change

Stamp a coarse `context` manifest and a `capability_fingerprint` onto new outcome records. Surface the per-capability breakdown in `outcome explain`. The default score, the rank order, and the promotion ratchet are unchanged. This is the data foundation: cohort-aware scoring cannot mean anything until records actually carry capability fingerprints, exactly as content fingerprints were captured and surfaced (brigade #218) before they drove the ratchet (#219). Records without a manifest are grandfathered.

Captured, and honest about provenance:

- Brigade-computed (trustworthy): `brigade_version`, `python` (major.minor), `platform`.
- Best-effort, marked with a `_source`: `harness` (auto-detected from env signals like `CLAUDECODE`, or a `BRIGADE_CONTEXT_HARNESS` override) and `model` (from the run receipt's agent on the run path, or a `BRIGADE_CONTEXT_MODEL` override; `unknown` otherwise).

`capability_fingerprint` is the sha256 of the coarse vector `{harness, model_family, python, platform}`.

### Phase 2 (shipped): cohort-aware scoring (retrieval only)

`outcome rank` and `outcome explain` resolve the current runtime capability once and score each artifact's content-current records earned under it. A thin capability cohort is pulled toward the pooled rate by deterministic shrinkage, `(helped + kappa*pooled_rate) / (total + kappa)` with a single documented `kappa` (4.0), so one run under a novel harness cannot swing the estimate and an unresolvable capability falls back to the pooled score. Records with no capability fingerprint are grandfathered into the current-capability cohort, so a pre-context ledger's rank output stays byte-identical until signals under a different capability actually accumulate. `outcome rank --by-capability` sorts by "what worked under my current context" (capability-shrunk estimate, then on-capability sample size, then pooled); the default sort and the promotion ratchet are unchanged, the ratchet still scores the pooled current-fingerprint cohort only.

Recency (shipped): `outcome rank --recency` (default half-life 45 days, `--recency-half-life DAYS` to override) weights the score it sorts by over recency-weighted counts, so a signal's weight halves every half-life and credit earned under a drifted-away environment fades without rewriting the append-only log. It applies to the pooled score by default and the capability-shrunk score with `--by-capability`, and the two flags compose. Off by default, so rank output stays byte-identical. Wilson and the shrinkage prior both accept fractional (weighted) counts; the ratchet never uses recency.

Still deferred, tracked as a follow-up: Opus's regression-attribution guard (never *globally* demote for a regression isolated to one novel capability cohort; quarantine it there, because a false rollback destroys a verified signal). It touches the ratchet, not retrieval display, and is sequenced after the capability cohorts prove out.

### Phase 3 (optional): paired attribution runs

Deterministically sampled, safe-command-only, disposable-workspace paired verifies that record a real uplift `delta`. This is the only path to causal attribution without a judge. Built only if the observational cohorts prove insufficient.

## The honest boundary

Even with all three phases, the fingerprint sees the files an artifact is made of, the cards a card links, and the coarse harness Brigade could observe or was told about. It does not see a skill reaching into a live API, an untracked script, or a silently-rerouted model. A passing signal vouches for this artifact under contexts we have observed. Unobserved executors, tool sets, and dependencies stay outside the receipt's guarantee. That sentence is the deliverable, not a `context_complete: true` flag that would only fake precision.
