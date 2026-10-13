# Task and worktree recall comparison (#1563)

Seven paired scenarios use the existing retrieval harness at K=5 over five
synthetic cards. `contexts.json` records generated checkout names, explicit
caller context and expected candidate queries. `queries.json` records the
basename and candidate query for each case with identical gold labels.
The aggregate is case-weighted. `stale-scope` repeats the `explicit-task`
query and gold label to retain a second stale/scope observation. It is not an
independent task. Across the four distinct candidate queries, two improve
(`orbit kit` and `token refresh`), one ties (`comet notes`), and one misses
(`fix thing`).

```bash
.venv/bin/python -m brigade.memory_retrieval_eval \
  --fixture-root evals/memory-retrieval/task-recall --adapters current,grep --json
.venv/bin/pytest -q -s \
  tests/test_memory_task_recall.py::test_task_worktree_fixture_matches_real_recall_and_existing_eval
```

The regression test creates isolated temporary Git worktrees and exercises the
production CLI. It checks each candidate's query and ranked card IDs against the
harness, then feeds both modes' actual ranked results into the existing quality
evaluator at the fixed date 2026-10-13. The JSON printed by the test includes
aggregate relevance metrics and stale/scope violation rows. The commands above
are standalone reproductions. Development verification uses the repository's
Brigade focused gate.

| Scenario | Basename query | Candidate query | Candidate ranking |
| --- | --- | --- | --- |
| linked-one | lane 7ac9 | orbit kit | orbit-deploy, active-token, retired-token, foreign-token |
| linked-two | lane 4bd8 | orbit kit | orbit-deploy, active-token, retired-token, foreign-token |
| explicit-task | lane a129 | token refresh | active-token, foreign-token, retired-token |
| plain-fallback | comet notes | comet notes | comet, active-token, foreign-token, orbit-deploy, retired-token |
| blank-task | lane 0ed3 | orbit kit | orbit-deploy, active-token, retired-token, foreign-token |
| vague-task | lane 2af0 | fix thing | empty |
| stale-scope | lane 9ac1 | token refresh | active-token, foreign-token, retired-token |

Basename retrieval is empty except for plain-fallback, whose ranking equals the
candidate. Linked-worktree basenames miss all relevant cards. Repository hints
recover their gold card. An explicit lexical task query also recovers its gold,
while the vague task remains a miss. The plain-directory fallback ties.

| Metric, current keyword adapter | Basename | Candidate |
| --- | --- | --- |
| Recall@5 / hit rate | 1/7 (0.142857) | 6/7 (0.857143) |
| Precision@5 | 0.028571 | 0.171429 |
| First gold rank, mean / median among hits | 1 / 1 | 1 / 1 |
| Misses | 6 | 1 |
| Returned result occurrences | 5 | 23 |
| Stale occurrences | 1/5 (0.2) | 6/23 (0.260870) |
| Scope violations | 4/5 (0.8) | 15/23 (0.652174) |

The grep adapter ties current on recall/hit rate for both modes. This comparison
measures query construction, with deliberately lexical gold examples. It does
not establish a scorer improvement or production-corpus performance.

Relevance improves on five case rows, including the repeated task observation,
ties on the plain fallback, and remains zero
for the vague explicit task. Stale retrieval gets worse in count and rate.
Candidate scope violations also increase in count. The lower scope fraction
reflects a different returned population, without any scope-filter guarantee.
Search includes frontmatter text: `orbit kit` matches repository metadata in
token cards, and `notes` matches every fixture body. Those extra results are
retained in the quality measurements. The quality evaluator reports one scope
violation per result occurrence, even if several dimensions disagree. Missing
projection input is reported unavailable.

Requested scope is fixture annotation: repository is `orbit-kit` for the linked
worktrees and `comet-notes` for the plain fallback. The explicit, vague, and
stale/scope cases request task `token-refresh`. Other cases request task null.
The existing quality contract flags any task-tagged card for a null requested
task, even if its repository matches. These annotations describe the known
fixture intent. The CLI receives only the task query and checkout path, and
does not enforce those scope annotations.

All searches stay inside the supplied fixture memory target. Stable repository
hints select query terms and do not select another corpus. No retrieval filter,
embedding, transcript capture or network service is added. Relevance and usage
diagnostics never become manifest-backed verification scores.
