# Workflow Rule Templates

Brigade installs repo-shareable workflow rule templates under `rules/` for issue and TDD work loops.

Installed files:

- `rules/issue-tdd-loop.md`
- `rules/acceptance-driven-work.md`

These templates are meant to be committed with a repo when the workflow should be shared by every operator and harness working there. They are intentionally generic:

- no personal preference policy
- no private repo names
- no raw issue bodies, chat transcripts, logs, paths, secrets, or hostnames
- no remote mutation requirement

Use gitignored local Brigade config for machine-local choices and personal defaults. Use the repo `rules/` files for project workflow rules that should travel with the repository.

`brigade work doctor` reports `workflow_rules` so an operator can see whether the templates are present in the current repo.
