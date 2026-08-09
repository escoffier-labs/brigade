# Changelog

## 0.1.0

- Initial bundled forward-plan skill: ready set + outcome rank + ROADMAP.md to a
  dependency-filed plan artifact under `.brigade/work/plans/`.
- Propose GraphTrail-derived edges (`blocks` from def/use, `conflicts-with` from
  write overlap), marked derived and requiring confirmation; degrade to zero
  proposed edges when GraphTrail is unavailable.
- Ship `plan.template.json` as the mechanical plan-artifact contract.
- Scope Bash to named Brigade ready/rank/task/plans/code commands; Write only
  under `.brigade/work/plans/`.
