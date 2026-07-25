# Oracle Browser Researcher Adapter

## Goal

Give the roster's `researcher` role a working model again on a subscription
cookie lane, with no API key and no new Brigade browser code, by adding
`oracle` as a one-shot CLI adapter.

## Context

Three facts set the shape of this phase:

- `research/llm.py:58` `resolve_backend` already returns a `CliBackend` for a
  `researcher` agent that declares `cli`, and `research/engine.py:64` only ever
  calls `.complete()`. Backend selection needs no change.
- `research/sources/web.py` already ships `PlaywrightProvider`, a headless
  Chromium tier that searches DuckDuckGo and carries `trust = "browser"`,
  behind `pip install 'brigade[research]'`. Research already has web grounding.
  The gap is a planning and synthesis model, not search.
- Agent Pantry already syncs the encrypted `gemini.google.com` cookie jar
  between machines, and `brigade pantry expiry-alert` already warns before it
  lapses. Oracle's browser engine reads exactly those cookies from the default
  Chrome profile.

So the whole change is one adapter. Pantry supplies the cookies, oracle drives
the session, Brigade shells out and reads text back.

## Boundary

- Require a user-installed `oracle` (MIT, npm `@steipete/oracle`). Brigade does
  not install it, does not bundle Node, and does not add it to the component
  manifest in this phase.
- Browser engine only (`--engine browser`). The adapter never emits an
  API-key path, so the lane cannot silently fall off the subscription rail.
- Read-only is hard by construction: oracle has no filesystem write path, so
  no flag and no prompt instruction are needed to enforce it.
- Scope is the `researcher` role. No run transport, no ChatGPT Pro seat, no
  multi-model panel, no oracle sessions or `--followup`, no MCP bridge, no cost
  reporting. Those are later phases.
- Known side effect: registering the adapter makes `cli = "oracle"` assignable
  to any seat, not only the researcher, because `_ADAPTERS` is a global table.
  That is accepted rather than gated, but it means the read-only enforcement
  contract has to hold for `brigade run` from day one.
- Keep the existing DuckDuckGo web tier as the search provider. Oracle does
  planning, query generation, and synthesis only.

## Design

Four layers, three of them already built:

| Layer | Component | Change |
|---|---|---|
| Auth | Agent Pantry cookie sync | none |
| Driver | `oracle --engine browser` | none, external binary |
| Adapter | `src/brigade/agents.py` | new, small |
| Caller | roster `researcher` | config only |
| Timeout | `src/brigade/research/llm.py` | one floor, see below |

The one non-adapter change: `research/engine.py:138` asks for `timeout=30` on
its planning call and `research/types.py:50` defaults to `timeout=60`, both
hardcoded at the call site. A browser round trip will not meet 30 seconds, so
the lane fails on its first request without a floor. The roster already carries
per-agent `timeout_seconds` and `resolve_backend` currently discards it, so
`CliBackend` takes it as a `min_timeout` that raises short engine timings and
never lowers generous ones. No new config concept, no edits to the engine's
literals, and seats that declare no `timeout_seconds` keep today's behavior.

Adapter surface in `src/brigade/agents.py`:

```python
def _oracle_argv(prompt: str, read_only: bool, sandbox: str | None, cwd: Path | None) -> List[str]:
    # Oracle has no filesystem write path, so read-only needs no flag and no
    # prompt instruction. --engine browser pins the run to the cookie lane so
    # the adapter can never fall back to an API key.
    return ["oracle", "--engine", "browser", "-p", prompt]
```

Registrations:

- `_ADAPTERS["oracle"] = _oracle_argv`
- `READ_ONLY_ENFORCEMENT["oracle"] = "hard"`
- `_MODEL_PIN["oracle"] = ("--model", _pin_after_cmd)`, producing
  `oracle --model gemini-3.1-pro --engine browser -p <prompt>`

`command_for` needs no entry: it falls through to the ref name, and the binary
is already called `oracle`.

Roster:

```toml
[agents.researcher]
cli = "oracle"
model = "gemini-3.1-pro"
role = "researcher"
timeout_seconds = 300
```

Data flow: `brigade research run "<question>"` -> `DeepResearcher` ->
`llm.complete()` -> `CliBackend("oracle", "gemini-3.1-pro")` -> `run_agent` ->
`_ADAPTERS` -> subprocess -> stdout -> `validate_final_output` -> engine.

Models the browser engine accepts: `gemini-3.5-flash`, `gemini-3.1-pro`, and
`gemini-3-deep-think` (browser-only; oracle rejects it in API mode).

## Failure handling

- **oracle absent.** `resolve_agent_executable` already returns
  `failure_kind="command-not-found"` at `failure_phase="dispatch"`. No work.
- **cookies expired.** This must not surface as a generic nonzero exit.
  `_oracle_auth_detail` recognises oracle's login and expired-session messages
  and points the operator at `brigade pantry expiry-alert`. It is a sibling of
  `_provider_preflight_detail`, not a branch inside it: that function is about
  workspace trust, a concept oracle does not have. Both failure paths in
  `run_agent` try the auth detail first, since it is the more specific
  diagnosis, and an auth hit reports `failure_kind="browser-auth"` rather than
  `"workspace-trust"`. Keeping those kinds distinct matters downstream: outcome
  capture and the model scorecard read `failure_kind`, and a stale cookie jar
  is an operator action while a trust refusal is a workspace problem. The
  first implementation shared the preflight branch and mislabelled every
  oracle auth failure as `workspace-trust`; the regression tests that caught
  it exercise both call sites through `run_agent`, not the detail function
  alone.
- **browser too slow for the engine's timings.** Not a hang, the common case.
  Fixed by the `min_timeout` floor above, driven by the seat's
  `timeout_seconds`. `.brigade/research.toml` is the wrong home for this:
  `Caps` has no timeout field, and `Caps.build` silently drops unknown keys, so
  a config-only attempt would look applied and do nothing.
- **genuine browser hang.** Covered by `run_agent(timeout=...)` once the floor
  raises it to the seat's declared ceiling.
- **partial or scraped garbage.** `validate_final_output` already runs at
  `agents.py:1182`. If oracle's stdout wraps the answer in progress chrome, add
  an extraction function following the `_parse_grok_final_output` precedent
  (`agents.py:360`) rather than loosening validation.
- **no silent fallback.** If the researcher role fails, `research` raises
  rather than quietly degrading to another seat, matching the existing
  `NoResearcherError` semantics.

## Verification

- [x] Unit test `_oracle_argv` argv construction, including model pin position.
- [x] Test that the adapter never emits an API-mode argv.
- [x] Test `READ_ONLY_ENFORCEMENT` reports `hard`, and that
      `brigade run --read-only` raises no soft-enforcement warning for an
      oracle seat. In scope despite the researcher-only boundary, because
      registering the adapter makes oracle dispatchable by `brigade run`.
- [x] Test roster validation accepts a `cli = "oracle"` researcher and that
      `resolve_backend` returns a `CliBackend`.
- [x] Test the `min_timeout` floor raises a short engine timeout, never lowers
      a generous one, and leaves seats without `timeout_seconds` unchanged.
- [x] Test the expired-cookie preflight detail string.
- [x] Run focused tests and `./scripts/verify` through
      `brigade work verify run`.
- [x] Live smoke: one `brigade research run` against real synced cookies,
      recording the result or the environmental blocker.

## Proof coverage

What is proven, and by what:

| Claim | Evidence |
|---|---|
| argv shape, model pin position, no `--heartbeat`, no API path | unit, `tests/test_agents_oracle.py` |
| argv survives a real `exec` | stub binary on a narrowed PATH records its own argv |
| stdout is the answer channel at the Brigade layer | stub returns markdown, `run_agent` returns it verbatim |
| auth failure on the nonzero-exit path | `run_agent` returns `failure_kind="browser-auth"` |
| auth failure on the empty-output path | separate branch, same assertion |
| auth beats workspace-trust when both patterns appear | stub emits both, auth wins |
| non-oracle seats keep `workspace-trust` | codex regression guard |
| timeout floor survives the real engine | `DeepResearcher` run asserts 300, not the engine's 30 |
| roster accepts `cli = "oracle"` | clears `is_known` and `limits.allow_models` |

Still unproven, and only oracle itself can settle it: whether real oracle stdout
carries progress chrome around the answer, and whether the browser session
actually completes against synced cookies.

## Live result

The adapter dispatches correctly and fails cleanly when the binary is absent.
`agents.run_agent("oracle", "hello")` returned `ok=False`,
`failure_phase="dispatch"`, `failure_kind="command-not-found"`, detail
`oracle not installed`, and `build_argv` produced exactly
`['oracle', '--model', 'gemini-3.1-pro', '--engine', 'browser', '-p', 'hello']`.

A full browser round trip could not run, blocked twice on this machine:

- `oracle` is not installed (`command not found`, and no global npm package).
  Brigade does not install it by design.
- `brigade pantry status` reports the agentpantry build **rejected by version
  policy** (unreleased or non-semver build; expected released >= 0.5.0), so the
  cookie substrate is not healthy here either.

Consequently the settled stdout decision is **not yet empirically confirmed**.
It rests on reading oracle's `src/cli/renderOutput.ts` (`if (!richTty) return
markdown;`, `richTty` defaulting to `process.stdout.isTTY`) plus never passing
`--heartbeat`. The first real run should check whether stdout carried only the
answer; if it did not, add an extraction function following the
`_parse_grok_final_output` precedent rather than loosening
`validate_final_output`.

## Resolved during planning

- **Is stdout clean enough to skip an extraction function? Yes.** Oracle's
  `src/cli/renderOutput.ts` returns `markdown` unrendered when `richTty` is
  false, and `richTty` defaults to `process.stdout.isTTY`. Brigade captures
  subprocess pipes, so that is false and no ANSI reaches stdout. Heartbeat
  progress is opt-in behind `--heartbeat`, which the adapter never passes. No
  extraction function, and a test asserts the flag is never emitted.

## Open questions

- Whether to map Brigade's `reasoning` pin onto
  `--browser-thinking-time <light|standard|extended|heavy>`. Cheap and a
  natural fit, but not needed for research synthesis. Deferred to the ChatGPT
  Pro phase, where thinking depth is the whole point.

## Risks

- Oracle's browser mode is labelled experimental by its author. It is a
  third-party Node tool driving a web UI that can change without notice. The
  `cli = "oracle"` seam keeps it replaceable: a future Brigade-owned driver
  swaps the adapter without touching `research/`.
- Cookie-driven automation of a consumer web session is a grey area against
  provider terms. This is a single-operator machine lane. It should not become
  a fleet default or a documented supported install path.

## Later phases

1. ChatGPT Pro reviewer seat via a `transport = "browser"` roster entry, with
   receipts and outcome capture. acpx is the sizing precedent.
2. `--browser-thinking-time` reasoning pin.
3. Component manifest entry and a station-style doctor, if the lane proves
   durable enough to be worth pinning a version against.
