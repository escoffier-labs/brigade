# Cross-tool context and impact corpus

This public synthetic corpus compares GraphTrail with optional external implementations on identical task text, repository state, and a 4,000-character response budget. It covers Python, TypeScript, Rust, and Go plus symbol navigation, high-degree hubs, explicitly named disconnected files, unresolved references, affected-test attribution, and branch-switch staleness.

The corpus is versioned in `corpus.v1.json`; output is versioned by `result-schema.v1.json`. Fixtures contain invented names and code only. The runner uses Python's standard library, works in temporary repositories, makes no network calls, and adds nothing to GraphTrail's runtime dependencies.

Build GraphTrail, validate the corpus, then run it:

```bash
cargo build --release
python3 benchmarks/cross-tool/run.py --validate-only
python3 benchmarks/cross-tool/run.py \
  --graphtrail target/release/graphtrail \
  --output cross-tool-results.json
```

## Optional comparators

Repeat `--comparator NAME=COMMAND` to add an external adapter. The runner starts the command in a fresh copy of the same fixture repository, writes one JSON request to stdin, and expects one JSON object on stdout:

```json
{
  "response": "budgeted human-readable or JSON result",
  "files": ["typescript/router.ts"],
  "setup_steps": 1,
  "tool_calls": 2
}
```

The request contains `protocol_version`, `case_id`, the exact `task`, `behavior`, `fixture_variant`, `response_budget_chars`, and (for branch drift) `branch_transition`. Correctness labels are deliberately withheld. Comparator adapters own their internal setup and must report its step and tool-call counts honestly. A branch-transition adapter must perform the supplied switch as part of its measured command and include it in `setup_steps`. GraphTrail's equivalent switch is also timed and counted. Commands are parsed into argument lists with platform-appropriate `shlex` rules, including native Windows paths; they are not run through a shell.

Example with an adapter already on `PATH`:

```bash
python3 benchmarks/cross-tool/run.py \
  --graphtrail target/release/graphtrail \
  --comparator 'candidate=context-bench-adapter --json' \
  --output cross-tool-results.json
```

The runner records label correctness, setup steps, tool calls, budgeted response characters, elapsed milliseconds, and peak resident memory in KiB. Comparator paths are normalized to forward-slash repository paths; `files` and `response` are combined in that order before the same character budget is applied, so the structured file list cannot bypass the response limit. GraphTrail runs only the command selected by each case (`context`, `affected`, or `doctor`); its context task field is removed before scoring so task echo cannot satisfy labels. Corpus validation rejects any correctness label repeated in task text or disclosed setup metadata. The branch case scores the derived, tool-neutral observation `index mismatch`, not the branch names required to perform the transition. Fixture creation has its own 30-second safety bound and is excluded from setup counts and timing for every implementation. Every measured subprocess has a configurable timeout (`--timeout-seconds`, 120 by default); timeout cleanup terminates the measured process tree so descendants cannot hold result pipes open. Peak memory is `null` where the host cannot expose process RSS. Comparator adapters should replace themselves with the measured tool process when possible so RSS covers that tool rather than only a wrapper. Correctness is computed by the runner from withheld expected paths and tool-neutral tokens; comparator self-scores are ignored. The result file is written even on a measured-command failure, and the runner exits nonzero if any implementation fails a case.

These measurements are evidence, not a default-policy switch. Do not change default ranking, MCP behavior, or thresholds until a reviewed result set establishes an explicit bar.
