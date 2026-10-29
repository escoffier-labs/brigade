# Brigade Stack Roadmap

Brigade Stack lets agents work autonomously within scopes the operator controls. Receipts and run journals show what happened, while evidence packages and handoffs give the next agent a checkable starting point. The same records connect local work to fleet workers and cloud seats.

Reconciled **2026-10-28** against current source and linked project records. This file describes direction and acceptance boundaries. Issues and PRs own implementation detail. Historical completion records remain in the [roadmap archive](docs/roadmap-archive.md), and the parser-derived CLI surface lives in the [command inventory](docs/command-inventory.md).

## Vocabulary and compatibility

- **harness**: an AI agent program, such as Claude Code, Codex, OpenClaw or Hermes.
- **operator**: the human setting policy and authorizing work.
- **orchestrator**: the agent coordinating admitted work and results.
- **handoff / ingest**: a durable memory note, and the process that reviews and files it.
- **scanner / inbox**: automation that discovers candidate work, and its review queue.
- **receipt / gate**: an audit record, and an operator-defined approval checkpoint.
- **engine**: Brigade Code Graph or Brigade Evidence, shipped inside Brigade under `engines/` and exposed through `brigade code` or `brigade evidence`.
- **station**: an optional sidecar Brigade installs and health-checks across a process boundary. Pantry is a station. Code Graph and Evidence are engines.

The settled public name is **Brigade Stack**, with install slug **`brigadestack`**. Existing `brigade` commands, `.brigade` workspaces, Python imports, stored fields and historical evidence remain compatible. Public wording changes do not migrate data or rewrite receipt identities. The GitHub repository and organization have not been renamed.

Execution remains explicit: the operator sets policy and starts commands or services, agents carry out authorized work, and external schedulers can trigger care runbooks. See the [execution model](docs/execution-model.md).

## Where things stand

**v0.31.x on main** is unreleased development after stable **v0.30.0**. The [latest stable release](https://github.com/escoffier-labs/brigade/releases/tag/v0.30.0) was published on 2026-10-20. “Brigade Stack v2” names the coordinated refresh milestone. It does not promise a v2.0 release or date.

| State | Verified scope | Boundary |
| --- | --- | --- |
| Published stable | Real `brigadestack` 0.30.0 bridges on [PyPI](https://pypi.org/project/brigadestack/0.30.0/) and [npm](https://www.npmjs.com/package/brigadestack/v/0.30.0), following [#2089](https://github.com/escoffier-labs/brigade/pull/2089). They pin `brigade-cli==0.30.0` and `@escoffier-labs/brigade@0.30.0`. | These are working install aliases for stable behavior. They do not include all changes on main. |
| Merged, unreleased | Preferred `fleet preference set --orchestrator` alias in [#2096](https://github.com/escoffier-labs/brigade/pull/2096). Public CLI/docs terminology, neutral new starter seats and conditional installed-workflow guidance in [#2107](https://github.com/escoffier-labs/brigade/pull/2107). | Legacy `--chef`, stored `chef` fields and existing rosters remain supported. New starter defaults do not migrate installed fleets. |
| Draft, held | Engine contributor-guide and Rust contract work in [#2111](https://github.com/escoffier-labs/brigade/pull/2111). | Held from ready/merge pending the expanded internal rename. No native binary aliases or data migration are implied. |

The established core remains: portable setup, handoffs and source coverage, memory care, work inboxes, fleet ownership, verification receipts, outcome capture, tool/skill registries, research and runbooks. Code Graph context and Evidence briefs support those loops. Provenance envelopes, trust checks, stable card IDs, scanner authority, chat scans and backup visibility are historical foundations. The read-only `brigade center serve` dashboard and per-model scorecard remain views over existing contracts. See the [archive](docs/roadmap-archive.md) for earlier closeouts.

## Now: complete the coordinated refresh

### Native T3 alignment comes first

Before implementing overlapping execution, coordination or notification features, check current native T3 capabilities. Reuse its child execution, threads, isolated worktrees, completion delivery and PR notifications where qualified. Brigade retains ownership, admission, claims, budgets, verification and provenance. Proposed upstream work, merged source, released versions, tools exposed by the selected runtime, and qualified behavior are separate states.

- **In progress, owned by [#2046](https://github.com/escoffier-labs/brigade/issues/2046):** connect app-owned child tasks and explicit task batches to Brigade governance, readiness and retained results. Task-targeted controls [#2100](https://github.com/escoffier-labs/brigade/pull/2100) and dormant launch-permission claims [#2105](https://github.com/escoffier-labs/brigade/pull/2105) are merged foundations. Dormant target/lifecycle contracts are open in [#2113](https://github.com/escoffier-labs/brigade/pull/2113), with execution disabled. The adapter, activation, readiness producers and end-to-end qualification remain with that owner. A smoke batch is not performance qualification.
- **Research complete, implementation proposed:** a daily source-backed native-alignment watch should compare upstream changes with current owned work and flag duplication or breaking contracts. Prefer a bounded extension of existing daily maintenance, with a manual proof first. No watch was installed or scheduled. Reuse the existing ledger and handoff path, with source failures recorded as unknown. No second scheduler or tracking database.

### Finish engine identity before moving existing data

Use **Brigade Code Graph** and **Brigade Evidence** in current presentation. The next implementation is a coordinated rename of internal code, compiled binaries and build/distribution artifacts, with compatibility readers and installer/rollback acceptance. Canonical source and target spellings remain proposals until that plan is coordinated with [#2111](https://github.com/escoffier-labs/brigade/pull/2111).

Real existing data-directory migration comes later, with consumer and recovery proof. Preserve legacy reads, published artifact coordinates, schemas and MAC verification. Never rewrite historical receipts or memory, or re-sign old evidence merely to change branding. Source-only rename research does not establish an implemented native alias.

### Qualify the owned integration and Hub slices

- **E2EE in progress:** [#2005](https://github.com/escoffier-labs/brigade/issues/2005) owns encrypted message acceptance. Crypto, Hub key-directory/storage and client trust foundations are merged, including offline peer-key acceptance in [#2106](https://github.com/escoffier-labs/brigade/pull/2106). These do not activate encrypted ordinary CLI/MCP messaging. Finish identity/key-management integration, inbox opening, send/reply sealing and sealed operator questions under that owner. Require a verified two-principal round trip, no plaintext subject/body in Hub storage, tamper/wrong-signer rejection, revocation and unchanged operation without the optional extra. The cryptography extra remains optional and the core retains zero mandatory runtime dependencies. Native Windows private-key custody remains unsupported.
- **Fleet Hub in progress:** [#2058](https://github.com/escoffier-labs/brigade/issues/2058) owns the human admin view. Existing theme and dashboard work is on main. Admin simplification/Capacity [#2108](https://github.com/escoffier-labs/brigade/pull/2108) is open, while searchable pages/Projects/refresh [#2109](https://github.com/escoffier-labs/brigade/pull/2109) and MCP inventory [#2110](https://github.com/escoffier-labs/brigade/pull/2110) are drafts. Agree a bounded launch-readiness subset with the owner, including readable states, dark/light and narrow-screen checks, and safe read contracts. The whole continuing design epic need not end for the refresh to launch.

### Complete workflow guidance adoption

Prefer pinned upstream Superpowers for generic workflow procedures, with thin Brigade verification/ownership guidance. Discover the active harness's installed skills and use Superpowers when installed, as [#2107](https://github.com/escoffier-labs/brigade/pull/2107) specifies. Retain specialist and T3 skills whose responsibilities remain necessary.

Harness adoption follows actual usage with fresh catalog/load/pin acceptance and rollback evidence. Avoid routine maintenance of generic forks.

### Prepare the public site and release acceptance

The functional Brigade Stack site/docs refresh is a held draft. Its draft status does not establish merge, deployment or domain readiness. Keep a functional site live while reviewing the concise homepage, docs, benefits and official marks.

The approved direction is `brigadestack.com` as primary, with `brigadestack.dev` and `brigade.tools` redirecting to matching pages. Domain purchase does not establish DNS or redirect configuration. Release acceptance must verify actual package versions/pins and fresh installs, canonical URLs, path/query redirects and analytics continuity before announcement. Further registry publication, site merge/deployment, DNS changes and announcement remain held pending their explicit release decisions. Repository/organization renaming remains future coordination. The separate organization-site hub design is adjacent work outside refresh launch acceptance.

## Next: close demonstrated product gaps

- **External feedback and adoption:** prioritize real install and workflow friction. Keep `operator adopt`, `handoff migrate` and memory-care backfill usable. Run the [cold-start gate](docs/runbooks/cold-start-gate.json) for releases and [agent cold-start scenarios](docs/cold-start-testing.md) for significant changes.
- **Verification and scorecard honesty:** keep work flowing through `brigade work verify run` and outcome capture. Audit-only receipts must not become scored artifact evidence. The scorecard's `ok_rate` is still worker-only. Separate orchestrator success, DNF, worker failure and suspected no-op writes, and qualify new lanes with real file-write probes.
- **Retrieval and memory care:** remain keyword-first until the fixture corpus and grep baseline in `evals/memory-retrieval/` show measured improvement. The retrieval evaluator is `python -m brigade.memory_retrieval_eval`. Preserve recall/miss signals, staleness and contradiction checks, and gated metadata repairs. Build on existing reinforcement dedup and standalone-chunk lint.
- **Code/Evidence provenance:** improve useful context and `brief_hit_rate` without treating retrieval quality as execution success. Preserve trust-policy and provenance-hash checks. Optional Evidence import on capture remains fail-open. Keep station plan/doctor/status boundaries explicit, with no silent pantry start or evidence crawl.
- **Context-aware outcomes:** context manifests, capability fingerprints, cohort ranking and recency weighting already exist. Continue evaluating context-isolated regression handling and paired attribution against the [context design](docs/design/context-blind-spot.md), with explicit acceptance before changing promotion policy.
- **Security and adapter fidelity:** deepen workspace/MCP/hook and prompt-injection rule packs, per-audience policy packs and optional offline threat-intel enrichment where reported needs justify them. Continue empty-argument, remote-URL and headless-approval round trips as harnesses evolve. Executor V2 MCP connector research recommends optional integration first. Deployed compatibility and approval behavior remain unverified.
- **Discoverability:** update public copy and metadata with the release, then remeasure brand and intent-query discovery. Prior prompt tests and crawl submissions are historical observations, not current ranking evidence or trademark clearance. Keep external submissions bounded and separately authorized.

## Later and non-goals

Future views reuse the CLI and versioned JSON contracts: scorecard-backed comparison, document editing and opt-in operator notifications. Optional local semantic retrieval still needs an eval advantage over grep. Owner-scoped tool gating should refuse high-risk tools by default on publicly reachable instances. Calendar/email triage needs explicit privacy and approval gates. Scheduler spreading, outbound backup notifications and broader discovery remain follow-ups to usage.

The refresh does not authorize a competing T3 manager, a second task database, silent service activation, blanket deletion, historical evidence re-baking, or org-wide product renaming. Native feature availability alone cannot remove Brigade governance. Personal data, credentials, node identities, keys, backups and retained evidence need their own scoped migration or retirement proof.

## How items move

Keep direction here and accountable acceptance in linked issues/PRs. Consolidate shipped detail behind the [archive](docs/roadmap-archive.md). Keep the active queue limited to remaining work. `brigade roadmap audit` checks roadmap state, `brigade roadmap archive` reads historical records, and `brigade roadmap commands --check` checks parser-derived command documentation. A merged PR is source evidence. Release, installation, deployment and end-to-end qualification require their own evidence.
