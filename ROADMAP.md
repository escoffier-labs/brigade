# Brigade Roadmap

Brigade is built as a practical daily workflow first, then a portable setup other people can adapt. The core direction: one command to start, predictable local artifacts, reviewable memory handoffs, and enough inspection to trust the loop during normal work.

This file is direction-only. The full per-feature completion detail through v0.8.x (every implemented, strengthened, and started bullet) lives in the [roadmap archive](docs/roadmap-archive.md). The generated list of every public command is in [docs/command-inventory.md](docs/command-inventory.md).

## Vocabulary

- **harness**: an AI agent program (Claude Code, Codex, OpenClaw, Hermes, Grok, …).
- **operator**: you, the human running the agents.
- **handoff**: a memory note an agent writes to be saved long-term.
- **ingest**: reading those notes and filing them into permanent memory.
- **scanner**: an automation that goes looking for useful work.
- **import / inbox**: a holding queue where found work waits for your review.
- **receipt**: a local file logging that something happened, kept for audit and proof.
- **gate**: a manual approval checkpoint; nothing risky happens without your yes.
- **station**: an optional sidecar tool Brigade installs and health-checks (evidence, pantry, tokens, search, …) without folding its runtime into the Python package.
- **dogfood**: Brigade being used on itself or the maintainer's real setup.

The one rule behind all of it: Brigade writes local files and queues, but it never publishes, edits canonical memory, runs background daemons, or touches remote servers on its own. Everything waits for an explicit command.

## Where things stand

**v0.21.x on main** carries the product core plus the evidence and multi-harness work that closed late spring / early summer 2026:

- Portable setup (`operator quickstart`), handoffs with lint and source coverage, daily operator loop, work inbox, fleet and release receipts, tool catalog, skill registry, research, runbooks, chat sweeps, backup visibility, station manifests.
- **Receipts and learning**: verify/run receipts with digests and optional HMAC signing, code-graph deltas, outcome capture from verify and run receipts, Wilson ranking, reconcile promote/rollback, git provenance, MiseLedger export/import of receipts.
- **Evidence loop**: GraphTrail context packs and deltas on runs, MiseLedger evidence briefs into work context, first-class `evidence` and `pantry` station CLIs, mechanical context evals (`brief_hit_rate`), no-op write-task flags, atomic `run.json` writes.
- **Guard and memory**: content-guard vendored as `brigade guard` / `scrub`; memory-doctor folded into `brigade memory`.
- **Harness fidelity**: Grok and Hermes MCP adapters, Grok headless approval for write tasks, Codex empty-args fingerprints, url-only MCP import as remote, verify `--argv-json`, skill template metadata shipping.
- **Model scorecard**: `brigade model scorecard` aggregates per-(cli, model) outcomes from run artifacts (read-only).
- **Product surfaces**: share / remember / prove / improve framing on the README and brigade.tools hub; station product pages (including content-guard and token-glace); GEO work (compare pages, PyPI URLs, Wikidata, Bing IndexNow + URL submission follow-up).

See the [archive](docs/roadmap-archive.md) for the pre-v0.21 completion record. Command inventory stays the source of truth for the CLI surface.

## Now: respond to real usage and close the loops we opened

The proving-ground milestone still holds: the maintainer workspace runs Brigade end to end. After v0.21 the "now" queue is about feedback and finishing the loops that already have bones.

- **External signals first.** Issues, install patterns, and Discord/shipper friction beat speculative stations. Nothing ships ahead of a reported need unless it unblocks dogfood.
- **Honest first contact.** Every release runs the cold-start gate (`docs/runbooks/cold-start-gate.json`). Significant releases re-run agent cold-start scenarios in `docs/cold-start-testing.md`.
- **Adoption path.** `operator adopt`, `handoff migrate`, and memory-care backfill stay the on-ramp. Friction there outranks new stations.
- **Work loop must stay fed.** Dogfood runs verification through `brigade work verify run` (or run receipts) and captures outcomes. An installed-but-dormant Brigade is a product failure mode, not a win.
- **GEO / discoverability.** Prompt-test baseline shows brand queries work and intent queries still do not. Follow Bing crawl of `/compare/*`, retest ChatGPT after index, keep awesome-list submissions to **one server per PR** with Glama where required, and avoid leading with "MCP server" (Brigade is a CLI control plane).
- **Model scorecard honesty.** Fill the orchestrator-success gap (ok_rate is worker-only today). Headless write adapters for antigravity/kimi (and similar) stay tracked as agent issues, not ignored DNFs.
- **Station productization.** Keep stations process-boundary: plan, doctor, status, never silent start of pantry source/sink or MiseLedger crawl. Fleet README and marks stay aligned with brigade.tools station pages.

## Next: deepen what already sits on the loop

- **Security plugin depth**: richer rule packs for agent workspaces (hooks, MCP configs, prompt-injection patterns), policy packs per audience, optional offline threat-intel enrichment.
- **Memory care depth**: smarter staleness, contradiction, and evidence checks for cards, with safe gated metadata repairs.
- **Scorecard and lane ops**: per-model orchestrator success rate, clearer DNF vs worker-fail, documented probe protocol for new CLI lanes (file write in cwd, never trust reply text alone).
- **MCP and harness fidelity**: continue adapter round-trips (empty args, url-only remotes, headless approvals) as harnesses change; prefer fix-the-adapter over more docs.
- **Evidence quality**: raise brief_hit_rate as a second-class signal only (install/rollback still exit-code only); optional MiseLedger import on capture remain fail-open.
- **Context-aware outcomes**: the content-fingerprint work closed the "score vouches for text that no longer exists" gap for an artifact's own files, but a hash cannot see the runtime harness (executor model, tools, dependencies) a signal was earned under. Phase 1 stamps a coarse `context` manifest and `capability_fingerprint` on new outcome records and surfaces them in `outcome explain`, no scoring change. Phase 2 adds cohort-aware retrieval scoring (exact -> capability -> pooled fallback, recency half-life, shrinkage) with the ratchet still on the pooled cohort. Phase 3 (optional) adds deterministic paired attribution runs for real uplift. Design and the three-model rationale: [docs/design/context-blind-spot.md](docs/design/context-blind-spot.md).

Chat surface scanners and backup visibility already shipped; remaining slices (scheduler spreading, outbound backup notifications) stay in the archive and under Later notifications.

## Later: the workspace on top of the bones

The CLI is the skeleton that carries everything. Every future surface sits on top of an existing command plus its JSON contract, never a parallel implementation.

- A workspace UI that is a view over the CLI: model comparison (scorecard-backed), a document editor, a viewer for research reports and operator-center state.
- Optional local semantic memory retrieval (on-device embeddings over `memory/cards/`), staying file-first and optional.
- Owner-scoped tool gating so a publicly reachable instance refuses high-risk tools by default.
- Multi-channel operator notifications beyond the terminal, still opt-in (`agent-notify` and friends).
- Personal-data surfaces such as calendar and email triage, behind the same privacy and approval gates.
- Deeper fleet GEO (sibling topics, Show HN only after organic traction, more intent SERP ownership vs chezmoi / sync-agents-settings).

## How items move

Roadmap items live here while they are direction. When a slice ships, its detail moves to the [roadmap archive](docs/roadmap-archive.md) with status and closing notes, checked by `brigade roadmap audit` and `brigade roadmap archive`. Command drift between docs and the CLI parser is checked by `brigade roadmap commands --check` in CI.
