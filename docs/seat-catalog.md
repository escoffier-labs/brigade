# Seat Catalog: Wiring Idle Subscription Capacity

Use this guide when a roster uses two or three seats while the operator pays for six or more model lanes. A subscription audit on one operator machine found rosters referencing 2 of roughly 15 usable Cursor model families, an activated-but-unused free reviewer lane, and a coding-plan quota of thousands of requests per week with zero dispatches. The gap is discoverability, not capability: every one of those lanes can hold a Brigade seat today.

This page maps common subscription lanes to seat roles, with copy-paste roster stanzas and the validation pattern to run before trusting a new seat.

## Seat roles

Assign every seat one job. Mixed-purpose seats make outcome capture useless because a failure says nothing about which kind of work the seat is bad at.

| Role | What it does | What to route there |
|---|---|---|
| orchestrator | plans, dispatches, synthesizes | nothing directly. It owns the run |
| worker | implementation, patches, research | the bulk of dispatched tasks |
| scout | inventories, greps, triage, summaries | work that does not deserve a frontier model |
| reviewer | verify claims against the diff | exact-head review passes |

## Lane recipes

Model IDs drift. Every ID below was verified answering on 2026-07-17. Confirm against your harness's live inventory before wiring (see the validation pattern at the end, and issue #299 for making that check automatic).

### Codex / ChatGPT subscription

The usual primary lane. Beyond the default frontier seats, the same OAuth serves cheap tiers that most rosters never touch:

```toml
[agents.scout]
cli = "cursor"
model = "gpt-5.4-mini-low"
role = "Cheap fast scout for file inventories and triage."
```

The mini and nano tiers also answer through harnesses that front the ChatGPT OAuth directly.

### Cursor subscription

Flat-sub plans carry far more than the headline models. Verified answering on one Ultra plan: `composer-2.5`, `grok-4.5` tiers, `kimi-k2.7-code`, `glm-5.2-high`, `gemini-3.5-flash`, `claude-sonnet-5` tiers, and the `gpt-5.4-mini`/`nano` ladder. An open-weight worker at zero marginal cost:

```toml
[agents.kimi27]
cli = "cursor"
model = "kimi-k2.7-code"
role = "Open-weight implementation worker on the flat sub; overflow relief for the primary worker."
```

Known gap: Composer models return empty text in read-only plan mode (issue #206). Pin a non-composer model for read-only seats.

### Claude subscription

Two seats, two costs. The heavier model reviews. A lighter sibling takes routine passes at a fraction of the quota burn:

```toml
[agents.claude]
cli = "claude"
model = "claude-opus-4-8"
role = "Cross-model reviewer: verify claims against the diff."

[agents.sonnet5]
cli = "claude"
model = "claude-sonnet-5"
role = "Light reviewer and worker for routine passes."
```

Keep the flagship interactive model out of headless seats entirely: one operator machine logged 502 API calls in an evening after review seats defaulted to the top tier with internal subagent fan-out. One dispatch should be one flat pass. Parallelism belongs in `brigade run` workers, where each lane is metered and receipted.

### Antigravity (free Google lane)

Often the most idle capacity an operator owns: two accounts can sit at 0-1% of weekly quota while paid lanes run hot. Route scans, summaries, research reads, and small changes here first.

```toml
[agents.flash]
cli = "antigravity"
model = "Gemini 3.5 Flash (Low)"
role = "Fast worker for research, summaries, scans, and small code changes."

[agents.reviewer2]
cli = "antigravity"
model = "Claude Sonnet 4.6 (Thinking)"
role = "Cross-model reviewer on the Google lane; verify claims without spending the Claude subscription."
```

Remember: every seat's `cli` value must appear in `limits.allow_models`, in the same edit. Brigade validates at roster load, not at edit time, and a missing entry fails every run against that roster.

### Direct Grok invalid-final recovery

A direct Grok read-only worker can continue its exact session once when the CLI exits 0 without the required structured final. To allow the one terminal fallback after another invalid final, name a reviewed Cursor-Grok ACP seat on the direct seat:

```toml
[agents.grok-review]
cli = "grok"
model = "grok-4.5"
reasoning = "high"
role = "Review the requested change and report actionable findings."
invalid_final_fallback = "cursor-grok"

[agents.cursor-grok]
cli = "cursor"
model = "grok-4.5"
transport = "acpx"
transport_version = "0.12.0"
role = "Fallback review through the reviewed ACP transport."
```

The reference is fail-closed: it must resolve to a non-orchestrator Cursor seat with a `grok-*` model and the reviewed ACPX version. Brigade does not search the roster for a similar model. The policy applies only to a typed malformed-final result from a direct Grok `--worker` read-only run; startup, authentication, network, timeout, permission, and nonzero failures are terminal.

### Anthropic-compatible API lanes (Kimi, GLM, and friends)

Several open-weight providers expose Anthropic-compatible endpoints, which means the `claude` CLI can drive them and Brigade can seat them directly. Declare the lane's environment on the seat: plain values inline, secrets by reference with a `_REF` suffix naming the environment variable that holds the value (the roster never stores the secret itself):

```toml
[agents.k3]
cli = "claude"
model = "kimi-k3"
role = "Open-weight worker on a coding-plan quota; relief for orchestrator-tier work."
env = { ANTHROPIC_BASE_URL = "https://api.moonshot.ai/anthropic", ANTHROPIC_AUTH_TOKEN_REF = "KIMI_API_KEY", CLAUDE_CONFIG_DIR = "/home/operator/.claude-lanes" }
```

Overrides apply to the spawned CLI process only, `run.json` records the override names and endpoint host (never values), and a missing referenced variable fails the worker before dispatch. If the CLI echoes any resolved override value, Brigade replaces the exact value with its target name in brackets before worker text, detail, stdout, or stderr can be stored. Direct CLI seats only: acpx and codex-cloud seats manage their own environment.

The isolated `CLAUDE_CONFIG_DIR` is load-bearing: with the default config directory, the `claude` CLI prefers its subscription OAuth over env auth, the upstream returns 401, and the CLI retries silently, which presents as an indefinite hang.

### Ollama (local and hosted)

Brigade seats ollama models through a prefix reference: the model rides in the `cli` value, and the full reference must appear verbatim in `limits.allow_models`. Hosted free-tier models work too (`kimi-k2.7-code:cloud` verified as a seat):

```toml
[agents.k27cloud]
cli = "ollama:kimi-k2.7-code:cloud"
role = "Hosted open-weight worker on the ollama cloud free tier."
```

Brigade never auto-pulls ollama models: dispatch fails unless the model is already present, which protects the disk from multi-GB surprise pulls. Hosted models can be retired upstream without notice, so validate before each wiring.

## Validate before trusting

A seat that answers a smoke prompt is wired, not proven. The pattern:

1. Smoke the seat: `brigade run --roster <roster> --read-only --worker <seat> --output-dir <dir> "Reply with exactly: SEAT-OK"` and confirm `run.json` records `status=ok` with the expected text in `final.txt`.
2. Bench it against an incumbent seat on a task with an objectively gradable answer (a planted bug with a known line and fix works well), and keep the receipts.
3. Capture the outcome against the seat so `brigade outcome rank` learns something: a seat with no outcome history is a guess wearing a roster stanza.

Retire stale IDs on sight. Harness model inventories drift: one roster carried a model ID its harness no longer listed (it worked only through fuzzy mapping), and a hosted model was retired upstream two days before a run tried to use it. Run `brigade roster doctor` after changing direct Cursor, Grok, or Ollama seats. It reports `exact`, `fuzzy-resolved`, `missing`, or `unavailable`; the last three warn without rejecting the roster. A transient inventory failure is not evidence that a model disappeared.
