# OpenCode Go model fitness, 2026-07-29

## Decision

Promote MiniMax M3 to the primary cheap coding seat after a 20-run shadow
period. Use GLM-5.2 for the security-capable reviewer and Kimi K3 as the
expensive cross-file canary. Keep all OpenCode seats read-only and isolated
until Brigade can enforce an OpenCode sandbox.

## Catalog boundary

`opencode models opencode-go --verbose --pure` returned these 16 models from
the OpenCode Go subscription provider:

`deepseek-v4-flash`, `deepseek-v4-pro`, `glm-5.1`, `glm-5.2`, `grok-4.5`,
`hy3`, `kimi-k2.6`, `kimi-k2.7-code`, `kimi-k3`, `mimo-v2.5`,
`mimo-v2.5-pro`, `minimax-m2.7`, `minimax-m3`, `qwen3.6-plus`,
`qwen3.7-max`, and `qwen3.7-plus`.

The CLI exposed a separate `opencode` provider with 7 limited-time free Zen
models. It also identified separate API-key environment providers, whose
catalogs were not queried or used. No account, token, or credential value was
recorded.

## Evidence split

The new matrix ran 5 previously untested Go models through 3 fixed tasks.
The earlier screen covered 4 models with only the routine and contract-drift
tasks. The tables stay separate because the earlier models have no security
cell.

### New three-task matrix

| Rank | Model | Variant | Findings | Output contracts | Mean latency | Refused |
|---:|---|---|---:|---:|---:|---:|
| 1 | `opencode-go/glm-5.2` | high | 5/5 | 3/3 | 13.586s | 0 |
| 2 | `opencode-go/kimi-k3` | max | 5/5 | 3/3 | 15.200s | 0 |
| 3 | `opencode-go/kimi-k2.7-code` | default | 5/5 | 1/3 | 7.060s | 0 |
| 4 | `opencode-go/deepseek-v4-pro` | high | 5/5 | 0/3 | 9.700s | 0 |
| 5 | `opencode-go/qwen3.7-max` | high | 5/5 | 0/3 | 9.226s | 0 |

GLM-5.2 and Kimi K3 were the only new candidates that obeyed every strict
output contract. All 5 models found all planted issues. DeepSeek Pro and Qwen
Max fenced every JSON response. Kimi K2.7 Code returned one valid structured
response out of 3.

### Earlier two-task screen

| Rank | Model | Variant | Findings | Output contracts | Mean latency |
|---:|---|---|---:|---:|---:|
| 1 | `opencode-go/minimax-m3` | thinking | 3/3 | 2/2 | 6.419s |
| 2 | `opencode-go/qwen3.7-plus` | high | 3/3 | 2/2 | 14.091s |
| 3 | `opencode-go/deepseek-v4-flash` | high | 3/3 | 1/2 | 5.516s |
| 4 | `opencode-go/mimo-v2.5` | default | 3/3 | 1/2 | 16.711s |

MiniMax M3 is the cheap primary candidate because it passed both available
output contracts, averaged 6.419 seconds, and its catalog price was $0.30 input
and $1.20 output per million tokens. Promote it after a 20-run shadow period.
It still needs a security cell before an unsupervised security role.

## Role policy

- `go_minimax_m3_primary`: post-shadow target for short read-only coding
  analysis and structured first-pass review. Timeout 120 seconds. Fall back to
  GLM-5.2.
- `go_glm_52_security`: security review and final structured review for cheap
  lanes. Timeout 180 seconds. Its security response found both planted issues
  without refusing.
- `go_kimi_k3_canary`: difficult cross-file canary when the higher Go usage
  cost is justified. Timeout 240 seconds. Claude or GPT-5.6 should still own
  final high-risk review.
- The proposal allows 2 worker processes per Brigade run. This worktree ran 1
  at a time because `.brigade/run.lock` serializes runs per worktree. A
  host-wide limit of 2 remains an external dispatcher policy and is not
  enforced by this roster.
- Retry one transient provider or rate-limit failure when the requested delay
  is at most 30 seconds. Do not retry auth, entitlement, timeout, or invalid
  final output on the same seat.
- Run with `--read-only --worktree` and require `changed_files = []`. Brigade
  0.25.1 records `sandbox = "read-only"` but warns that it is not applied to
  OpenCode.

### Router preference

- Prefer MiniMax M3 over Kimi K2.7 for short, low-risk analysis after a
  20-run shadow period confirms its two-task result. Keep Kimi K2.7 as the
  fallback during that period.
- Prefer GLM-5.2 over `glm_cursor` when Cursor capacity is constrained or the
  output must be strict JSON. Prefer `glm_cursor` for edits because OpenCode
  lacks enforced write isolation.
- Prefer Kimi K3 over GPT-5.6 Terra only for a read-only cross-file canary where
  a later frontier reviewer can catch a miss.
- Keep Claude or GPT-5.6 as the final reviewer for high-risk contract drift,
  security decisions, or repository changes.

### Deferred candidates

- Qwen3.7 Plus needs a security cell before promotion.
- Kimi K2.7 Code is fast, but passed only 1/3 output contracts.
- DeepSeek V4 Flash passed only 1/2 earlier output contracts.
- DeepSeek V4 Pro and Qwen3.7 Max passed 0/3 output contracts.
- MiMo V2.5 passed 1/2 contracts and had a 25.986-second review.

The exact post-shadow proposal is in
[`proposed-opencode-go-roster.toml`](proposed-opencode-go-roster.toml).

## Multi-seat wiring

One normal Brigade run used GLM-5.2 as orchestrator and MiniMax M3 as worker.
GLM produced a valid one-assignment plan, MiniMax found the defect, and GLM's
final synthesis returned `WIRING_OK`. The run completed in 14.620 seconds with
0 changed files. Both requested models resolved through OpenCode CLI with no
fallback. The raw ignored artifacts were checked against the shipped dataset
by receipt `20260729-201417-work-verify-f959da`, MiseLedger
`fbb5c10d67e793cfb8050057`.

## Receipts and limits

- Earlier validator: `20260729-183625-work-verify-69cacc`, MiseLedger
  `ec4e976c7c7689a39f91ae97`.
- New fresh validator: `20260729-194155-work-verify-f0f8e3`, MiseLedger
  `dc8c9568260c63269750f666`.
- Schema, semantic, roster, and raw multi-seat artifact validator:
  `20260729-201417-work-verify-f959da`, MiseLedger
  `fbb5c10d67e793cfb8050057`.
- Per-call token and usage values were not exposed by Brigade worker receipts
  or OpenCode logs, so the dataset records them as `null`.
- All 15 new probe runs and the multi-seat trial recorded 0 auth, entitlement,
  rate-limit, or provider failures.
- One run per cell is a screen. The prompts named their expected findings, so
  this does not measure blind vulnerability discovery or adversarial review.
- Pre-change GraphTrail symbol tracing was skipped because this directory holds
  benchmark JSON, TOML, and Markdown only. Brigade verification's automatic
  graph delta found 0 changed symbols, imports, control-flow nodes, or APIs.

Machine-readable results live in
[`opencode-go-results.json`](opencode-go-results.json), with the local schema in
[`opencode-go-schema.json`](opencode-go-schema.json). Run
`python3 benchmarks/model-ratings-2026-07/validate.py` to check cross-field
arithmetic and, when `jsonschema` is installed, the Draft 2020-12 schema. Add
`--artifacts <multi-seat-run-dir>` to compare a local raw multi-seat run with
the recorded trial.
