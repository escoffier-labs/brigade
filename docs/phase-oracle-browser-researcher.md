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
- **cookies expired.** This must not surface as a generic nonzero exit. Add an
  `oracle` case to `_provider_preflight_detail` (`agents.py:493`) that
  recognises oracle's login prompt and points the operator at
  `brigade pantry expiry-alert`.
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

- [ ] Unit test `_oracle_argv` argv construction, including model pin position.
- [ ] Test that the adapter never emits an API-mode argv.
- [ ] Test `READ_ONLY_ENFORCEMENT` reports `hard`, and that
      `brigade run --read-only` raises no soft-enforcement warning for an
      oracle seat. In scope despite the researcher-only boundary, because
      registering the adapter makes oracle dispatchable by `brigade run`.
- [ ] Test roster validation accepts a `cli = "oracle"` researcher and that
      `resolve_backend` returns a `CliBackend`.
- [ ] Test the `min_timeout` floor raises a short engine timeout, never lowers
      a generous one, and leaves seats without `timeout_seconds` unchanged.
- [ ] Test the expired-cookie preflight detail string.
- [ ] Run focused tests and `./scripts/verify` through
      `brigade work verify run`.
- [ ] Live smoke: one `brigade research run` against real synced cookies,
      recording the result or the environmental blocker.

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
