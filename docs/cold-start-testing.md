# Cold-Start Testing

Brigade's worst bugs have been first-contact bugs: things that work on the maintainer's machine and break on a stranger's. Two complementary checks guard against them.

## The deterministic gate (every release)

```bash
brigade runbook run docs/runbooks/cold-start-gate.json --target .
```

The runbook executes the documented new-user journey literally in a throwaway sandbox with neutralized global git config: pipx install from the working tree, the README quickstart and handoff-draft blocks verbatim, the first-10-minutes health checks, and regression guards for past first-contact bugs (the gitignore clobber, the inactive-hook blocker). It is part of the pre-tag checklist in RELEASE.md. A neutral sandbox matters: the maintainer's global gitignore and hooksPath masked real clean-machine failures for weeks.

## The agent scenarios (before significant releases)

Deterministic checks cannot judge whether the docs are *understandable*. For that, spawn fresh agent sessions in clean sandboxes with only public docs as input and score where they stumble. The three standard scenarios:

**A - fresh repo, docs only.** Sandbox gets README.md and docs/first-10-minutes.md, nothing else; Brigade preinstalled. Task: set the repo up for two harnesses, write one handoff recording a decision, lint it, run the documented health checks, confirm healthy. Score 1-5 on "could a stranger do this from the docs alone"; every command that fails first try or doc claim that mismatches reality is a finding.

**B - homegrown adoption.** Sandbox is a fake existing operator workspace (instruction files, a handoff inbox with one deliberately malformed note carrying an injection line, cron-ish scripts). Only the README plus `--help` allowed. Task: assess with Brigade without modifying any existing file, find the adoption path, detect the malformed handoff. Verify all mutations stayed under `.brigade/` (checksum the fixtures). Score on "safe to point at a real homegrown setup".

**C - help-only discoverability.** No docs at all. Task: reach a linted handoff using only `--help` screens, counting every screen consulted (budget: six). Scores the CLI's self-explanation.

Findings route into the work inbox (`brigade work import add --source cold-start-test ...`), get fixed, and the scenario re-runs before tagging. History: this loop found the gitignore selection clobber, the broken README example, the dry-run misreporting, the hooksPath false positive, the template shadowing, and the local-operator hook blocker.
