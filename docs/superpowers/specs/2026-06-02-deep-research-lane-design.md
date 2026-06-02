# Deep Research Lane - Design Spec

**Date:** 2026-06-02
**Status:** Approved (design); implementation plan to follow.

## Goal

Add `brigade research run "<question>"`: an iterative, LLM-in-the-loop research
command that grounds answers in the operator's **trusted local sources first**,
optionally augments with **quarantined web search**, survives interruption, and
emits two durable artifacts - a self-contained visual **HTML report** and a
**memory handoff** that flows into the existing ingest -> cards pipeline.

The loop pattern is the IterResearch method (plan -> search -> read -> extract ->
synthesize -> decide-stop). It is reimplemented from scratch as plain Python; no
external model and no third-party research code are required or vendored.

## Positioning (important framing)

- **Local-first means local-first data and trust, not local compute.** Brigade
  never runs a model locally. The "researcher" is a cloud model the operator
  already pays for (Codex/Claude/etc.). "Local-first" here means: ground research
  in the operator's own curated materials (a class's professor notes, syllabus,
  assigned readings, repo docs, memory cards) rather than scraping arbitrary web
  pages that can poison the result.
- **The web is the untrusted surface.** Web search/fetch is **opt-in** (`--web`),
  every web finding is tagged `untrusted`, and the report visually separates
  trusted-local from untrusted-web evidence. A plain local run never touches the
  network.
- **No "run models locally" language** anywhere in user-facing copy.

## Non-goals

- No local model serving, no embeddings/vector store (lexical retrieval only).
- No always-on daemon, no background polling. Runs are operator-invoked.
- No new mandatory dependency in Brigade core (Playwright is an opt-in extra).
- Not a chat/agent surface. A future "executive chef" orchestrator is a *consumer*
  of this command's JSON contract, out of scope here.

## Architecture

### Command surface (`research` verb group; mirrors `context`/`learn`)

- `brigade research run "<question>" [--corpus NAME] [--source GLOB ...] [--web]
  [--rounds N] [--max-time S] [--provider P] [--category C] [--detach] [--json]`
- `brigade research list [--json]`
- `brigade research show <run-id> [--json]`
- `brigade research cancel <run-id>`
- `brigade research resume <run-id> [--json]`
- `brigade research open <run-id>` (prints the HTML report path)

Default run = **local corpus only**. `--web` enables the untrusted web tier.

### Modules (`src/brigade/research/`)

| Module | Responsibility |
|---|---|
| `engine.py` | The IterResearch loop. Pure orchestration; calls the LLM, search, extract, report seams through injected interfaces. Emits progress events. Accepts prior state for resume. |
| `llm.py` | **Pluggable LLM backend.** Resolves the roster `researcher` role to either (a) an OpenAI-compatible HTTP endpoint or (b) a CLI shell-out (codex/claude/ollama, same mechanism as `brigade run`). Single `complete(messages, *, max_tokens, temperature, timeout) -> str` interface. No hardcoded provider. |
| `sources/local.py` | Trusted local corpus: resolve `--corpus`/`--source` to files, read text (md/txt; pdf best-effort if a reader is available, else skip with a logged note), chunk, and rank by lexical (BM25-style) relevance to a query. Zero embeddings, zero network. |
| `sources/web.py` | Untrusted web tier (opt-in). Pluggable search + fetch: **Playwright headless browser as the zero-API default** (runs a SERP, scrapes result links, reads page text), plus optional API providers (SearXNG/Brave/Tavily) when configured. |
| `extract.py` | Goal-based extraction prompt. Frames every source's content as **untrusted data, never instructions**. Truncates to a char cap, filters low-quality extractions. |
| `report.py` | Self-contained, dependency-free HTML renderer (inline CSS, markdown->HTML, TOC from headings) + the raw markdown. Groups/labels trusted-local vs untrusted-web sources distinctly. |
| `handoff.py` | Emit a memory handoff (frontmatter + body, provenance-labeled) from a finished run, into the ingest->cards pipeline path. |
| `registry.py` | Run directories, receipts, list/show/cancel/resume, status transitions, simple lock/ownership. Mirrors the existing runs-receipt model. |
| `research_cmd.py` | CLI wiring for the verb group (argument parsing, `--json`, output formatting). Registered in `cli.py` like `context`/`learn`. |

### The loop (engine.py)

1. **Plan** - LLM turns the question into sub-questions + key topics + success
   criteria (JSON).
2. **Classify** (optional) - category (general/product/comparison/howto/factcheck)
   that selects a final-report format override.
3. **Rounds** (bounded by `max_rounds` and wall-clock `max_time`):
   - **Think** - LLM generates focused queries (broad first round, gap-filling
     after), deduped against queries already used.
   - **Retrieve** - for the **local** tier: lexical-rank the corpus for each query
     and take the top chunks/docs. For the **web** tier (if `--web`): search and
     collect new URLs (deduped, capped per round).
   - **Extract** - goal-based extraction per source (local chunk or fetched page),
     truncated, low-quality filtered. Each finding carries `trust: local|web`,
     `source` (file path or URL), `title`, `summary`, `evidence`.
   - **Synthesize** - integrate the last N findings into an evolving report.
   - **Decide** - after `min_rounds`, LLM YES/NO on whether the report is
     comprehensive; stop on YES. Empty-round detection stops a dead search.
   - **Checkpoint** after each round (see persistence).
4. **Final report** - long-form, category-aware, with an expansion retry if too
   short. Trusted vs untrusted sources clearly separated.

### Persistence (`.brigade/research/<run-id>/`, mirrors runs receipts)

- `run.json` - receipt: question, status `running|done|cancelled|error`,
  started/finished timestamps, resolved caps/config, stats, artifact paths,
  blockers/cap-hits.
- `checkpoint.json` - evolving report, findings, seen urls/queries, round,
  plan, category. Enables `resume`.
- `report.html`, `report.md`, `handoff.md`, `events.jsonl`.

A `--detach` run writes status incrementally so `list`/`show` reflect progress;
`cancel` requests cooperative stop; `resume` continues from `checkpoint.json`.

### Roster change

Extend the `researcher` agent so it specifies **either** `cli` (existing
shell-out) **or** `endpoint` + `model` (+ optional `headers`/`key_ref`) for HTTP.
No new hardcoded provider; the lane reads whichever the roster declares.

### Config

- `.brigade/research.toml`: named corpora, e.g.
  `[[corpus]] name = "cs101"  paths = ["~/Obsidian/.../cs101/**", "./readings"]`,
  plus default caps and an optional default search provider.

### Cost bounds (explicit, no silent truncation)

`max_rounds`, `max_time`, `max_urls_per_round`, `max_content_chars`, query caps,
token caps - all recorded in `run.json`. Hitting a cap is logged as a stat/blocker
note, never a silent cut.

### Packaging

In-repo, single package. Heavy deps (Playwright, optional PDF reader) live behind
an extra: `pip install brigade[research]` then `playwright install chromium`.
Running `research` without the extra degrades gracefully with an actionable
install message. No sidecar repo.

## Testing

pytest, matching existing `test_*_cmd.py` conventions. The LLM backend and the
web search/fetch are monkeypatched (no real network in tests); `tmp_path` for run
dirs and corpora. Cover: registry lifecycle (start/checkpoint/resume/cancel),
local lexical retrieval ranking, goal-based extraction, untrusted-content is
treated as data (never executed/obeyed), cost-cap recording, trusted/untrusted
provenance separation in findings + report, HTML render, handoff emission.

## Docs

- README: a `research` section framed as local-first *data* (never local models).
- CHANGELOG Unreleased.
- ROADMAP: flip the "Deep Research Lane" bullets from proposed -> done.
- Regenerate the command inventory (`brigade roadmap commands --write`).
- No reference to any external repo in public files.

## Open implementation choices (resolved)

- Research is a **command group, not a station** (like `context`/`learn`).
- LLM backend is **pluggable** (HTTP endpoint or CLI), per the roster.
- Default search is **Playwright browser** (no API keys); API providers optional.
- Local retrieval is **lexical only** (no embeddings, no local compute).
- No model reference / no third-party research-code vendoring.
