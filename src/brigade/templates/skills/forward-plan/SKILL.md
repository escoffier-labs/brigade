---
name: forward-plan
description: Use when turning Brigade's native ready set, outcome rank, and ROADMAP.md into a dependency-filed plan artifact under .brigade/work/plans/ - propose GraphTrail-derived edges (blocks from def/use, conflicts-with from write overlap) marked derived and requiring confirmation. Degrade cleanly when GraphTrail is unavailable. Never a subcommand.
allowed-tools: Read, Grep, Write, Bash(brigade work ready:*), Bash(brigade outcome rank:*), Bash(brigade work task:*), Bash(brigade work plans:*), Bash(brigade code:*)
compatibility: Brigade-wired workspaces with a work ledger. GraphTrail optional (degrades to no proposed edges).
---

# forward-plan

Turn the native ready set, outcome ranking, and the repo ROADMAP into one
dependency-filed plan artifact. Brigade stays invoke-only. This skill is the
allowlist-bound agent recipe. It is a registry skill, never a CLI subcommand -
the planning sequence stays inspectable.

## When this applies

- You need a forward plan that joins what is ready now with roadmap direction.
- An operator schedules a headless harness against this skill for planning.
- The workspace has (or can create) `.brigade/work/` and may or may not have
  GraphTrail installed.

## Allowlist

Work only through this allowlist:

- `brigade work ready --json` (add `--explain` when blocked items matter)
- `brigade outcome rank --json` (optional `--by-capability` / `--recency`)
- Read of repo-root `ROADMAP.md` under the parse contract below
- `brigade code …` for GraphTrail footprint / impact / doctor checks
- `brigade work task edge …` / `brigade work task add --graph …` only after an
  operator confirms derived edges (never auto-apply. See Safety gates)
- `brigade work plans` to list existing plan artifacts
- Write only under `.brigade/work/plans/` (the plan JSON + plan.md pair)
- Read/Grep of on-disk task ledgers, existing plans, and code paths named by
  GraphTrail output

Bash is scoped to those `brigade …` commands only. Do not expand into arbitrary
shell, remote APIs, or writes outside `.brigade/work/plans/`. Stay inside the
target workspace. Do not follow `..` or symlinks out of it.

## Safety gates

**Plan draft is not graph-ingest input.** A forward-plan draft under
`.brigade/work/plans/` (the `.json` / `.plan.md` pair) is **not** valid input
to `brigade work task add --graph`. That command expects a graph-ingest file
shaped for task creation with edges. Feeding it a plan draft can create task
nodes while applying **zero** proposed edges from `proposed_edges`.

**Confirmation is procedural, not enforced by the allowlist.** Derived-edge
confirmation is an operator-in-the-loop gate. The broad
`Bash(brigade work task:*)` allowed-tool pattern does not enforce it, so this
recipe must not run any mutating `brigade work task …` or
`brigade work task edge …` command before the operator gives explicit
confirmation.

## Inputs

1. **Native ready set** - `brigade work ready --json` (from the work ledger
   dependency edges / ready resolver). Prefer `--explain` when the ready set is
   empty so blocked paths are visible. Ready items may carry optional
   `seat_class` (`mechanical` | `judgment` | `review`) and `spend_by`
   (ISO-8601 deadline) annotations for quota-driven dispatch routing. Pass those
   through when proposing or filing work; never invent a model id. When creating
   or annotating tasks after confirmation, emit them with
   `brigade work task add … --seat-class … --spend-by …` or
   `brigade work task annotate <id> …`, or as `metadata.seat_class` /
   `metadata.spend_by` on `--graph` nodes. Absent annotations change nothing.
2. **Outcome rank** - `brigade outcome rank --json`. Use ranking only as a
   soft priority signal for ordering proposed work, never as a substitute for
   readiness.
3. **ROADMAP.md** - repo-root roadmap, parsed with the contract below.

## ROADMAP.md parse contract

Read only the repo-root file `ROADMAP.md` (or the path the operator names as
that roadmap). Heading shapes:

| Heading shape | Action |
|---|---|
| `# …` (document title) | Ignore for item extraction. Context only. |
| `## Now…` / `## Current…` | **Read.** Collect `-` / `*` bullets as active candidates. |
| `## Next…` | **Read.** Collect bullets as near-term candidates (below Now). |
| `## Later…` | **Read.** Collect bullets as deferred candidates (lowest priority). |
| `## Vocabulary` / `## How items move` / archive / process sections | **Ignore.** |
| `## Where things stand` / status snapshot sections | **Ignore** for new plan nodes (background only). |
| Any other `##` / `###` | Ignore unless the operator explicitly names it in scope. |

Bullet rules:

- Only lines starting with `- ` or `* ` under an included section become
  candidates. Nested prose paragraphs, tables, and fenced code are ignored.
- Bold lead-ins (`- **Title.** rest`) keep the title as the candidate label.
  The rest is supporting detail.
- Links, archive pointers, and "Status: implemented" closure notes do not
  create new plan nodes on their own.
- Do not invent roadmap items that are not on-disk bullets under an included
  heading.

## Output: dependency-filed plan artifact

Write a paired artifact under `.brigade/work/plans/`:

- `.brigade/work/plans/<plan-id>.json`
- `.brigade/work/plans/<plan-id>.plan.md`

Choose `<plan-id>` as `forward-plan-<UTC-YYYYMMDDTHHMMSSZ>` (filesystem-safe).
Use the shipped `plan.template.json` shape in this skill directory as the JSON
contract. Required fields:

- `kind`: `"forward-plan"`
- `status`: `"draft"` until an operator accepts. Never auto-accept
- `inputs`: records of ready-set, outcome-rank, and roadmap sources used
- `nodes`: proposed or existing work items (ready tasks first, then Now/Next
  roadmap candidates not already covered)
- `proposed_edges`: dependency proposals (see below)
- `graphtrail`: availability, snapshot identity when known, and degradation note
- `next_command`: a safe next step (default review the draft. Never a mutating
  apply)

The `.plan.md` mirrors the JSON: summary, nodes, proposed edges (calling out
derived + unconfirmed), GraphTrail status, and next safe command.

## Derived dependency edges (GraphTrail impact intersection)

When GraphTrail is available (`brigade code doctor` / impact / neighbors work):

1. Build a footprint per candidate node (files + symbol ids) from GraphTrail
   context/impact, stamped with the graph snapshot you queried.
2. Pairwise impact intersection over open / proposed nodes:
   - **def/use ordering** → directed `blocks` (definer/source blocks
     consumer/target)
   - **write overlap** (same files written) → unordered `conflicts-with`
3. Every such edge is **derived**: set `derived: true`, record `rule`
   (`def-use` or `write-overlap`), and a `snapshot_hash` (or graph identity).
   Set `confirmed: false`.
4. Derived edges **require confirmation** before they become real ledger edges.
   Do not call `work task edge add` or `--graph` apply for derived edges until
   an operator confirms them.
5. Only derived edges may be auto-retracted by a later reconcile recipe (see
   scheduled-care / outcome reconcile docs). Authored (human-confirmed) edges
   are never auto-deleted by this skill. If GraphTrail later contradicts them,
   flag the contradiction in the plan, do not silently remove them.

`conflicts-with` is proposal-only in the plan artifact today (the native ledger
edge types are `blocks`, `parent-child`, `discovered-from`). On confirmation,
file native `blocks` edges that the operator accepts. Keep confirmed
`conflicts-with` in the plan as parallel-safety advisories unless a native type
exists.

### GraphTrail unavailable (degrade cleanly)

If GraphTrail is missing, the DB is absent, or `brigade code …` fails:

- Propose **no** derived edges (`proposed_edges: []`).
- Set `graphtrail.available` to `false` and state plainly in both JSON and
  plan.md: "GraphTrail unavailable. No derived dependency edges proposed."
- Still write the plan from the ready set, outcome rank, and ROADMAP parse.
- Do not invent edges from prose guesswork.

## Process

1. Run `brigade work ready --json` (and `--explain` if ready is empty).
2. Run `brigade outcome rank --json`.
3. Read `ROADMAP.md` under the parse contract. Collect Now / Next / Later
   bullets.
4. Probe GraphTrail (`brigade code doctor` or a cheap `brigade code stats`).
   If unavailable, skip to step 6 with empty proposed edges and say so.
5. Otherwise compute footprints and propose derived edges from impact
   intersection. Mark each derived + unconfirmed.
6. Soft-order nodes: ready set first, then outcome-rank hints, then Now, Next,
   Later roadmap candidates.
7. Write `.brigade/work/plans/<plan-id>.json` and `.plan.md` from the template
   contract. Leave `status: draft`.
8. Stop. Present the artifact paths and wait for confirmation before filing any
   ledger edges. Capture outcomes against `forward-plan` when verification is
   part of the run.

## Failure paths

- Missing `.brigade/` / work ledger / target: stop. Report the missing path.
  Do not invent a ledger or run against the operator home.
- Empty ready set and no includable ROADMAP bullets: exit cleanly with
  "nothing to plan". Write no artifact (or write a draft that says so and lists
  zero nodes - prefer no artifact when both inputs are empty).
- Missing `ROADMAP.md`: continue with ready set + outcome rank only. Note the
  missing roadmap in `inputs` and do not invent roadmap nodes.
- Malformed ready / rank JSON or unreadable ROADMAP: skip the bad input, say
  so, continue with what remains. Never rewrite ledger or roadmap files to
  "fix" parse errors.
- GraphTrail errors mid-probe: degrade as above (no derived edges, say so).

## Out of scope

- New CLI subcommands or changing the work ledger schema
- Auto-applying derived edges without confirmation
- Scheduler or model-dispatch integration
- Auto-retracting authored edges
