# Tool-surface benchmark

Compares two MCP tool shapes for code navigation on the same corpus and query set:

- **narrow tools**: GraphTrail's 14 typed tools (`search`, `callers`, `callees`,
  `impact`, `context`, `affected`, `explain`, ...).
- **single search**: one general semantic-search tool that takes a natural-language
  query and returns ranked code chunks.

Corpus: the GraphTrail repository itself (84 files, 712 symbols, 1061 call edges).
Query set: 12 real agent questions, split into `locate` (find the file/symbol for a
concept) and `structural` (callers, callees, affected tests, edge resolution).

Success for a task means every expected string appears in the tool's text result.
Response size is approximated as `ceil(chars / 4)` tokens on the JSON body.

## What the run showed

| surface        | tools/list tokens | hit rate | locate | structural |
|----------------|-------------------|----------|--------|------------|
| narrow tools   | 2289              | 11/12    | 6/7    | 5/5        |
| single search  | 826               | 4/12     | 3/7    | 1/5        |

Two facts drove the follow-up change:

1. **Structural questions need typed tools.** The single-search surface answered 1 of 5
   structural tasks; it returns chunks near the query text, not the call graph, so
   "what calls `sync_repo`" or "which tests are affected by this file" have no reliable
   answer. GraphTrail's typed tools answered all 5. No change justified here.

2. **The typed tools had no per-call size lever.** `impact` on a hot symbol
   (`sync_repo`, 79 incoming callers) emitted ~9,150 tokens in one call, and `callers`
   ~7,460, against the single-search surface's bounded ~1,900. The single-search tool
   caps output with a `limit` (default small, agent-raisable); `callers`, `callees`,
   and `impact` had only a fixed 500-edge safety cap and no way for the caller to ask
   for fewer. On a token budget that erased the advantage typed tools are supposed to
   have.

The change that followed: an optional `limit` on `callers`, `callees`, and `impact`
that truncates the edge list and marks the cut, so an agent can bound a blast-radius
query the same way it bounds a search. Omitting `limit` preserves the prior output
byte-for-byte.

`results.json` holds the per-task numbers.
