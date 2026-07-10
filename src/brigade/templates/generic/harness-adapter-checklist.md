# Harness Adapter Checklist

Use this when wiring a new harness into the `brigade` contract.

## 1. Bootstrap loading

- [ ] Identify which file the harness loads first when starting a session.
- [ ] Confirm the harness can be configured to load `AGENTS.md` + (its own bridge file).
- [ ] If the harness has its own preference file (e.g. `CLAUDE.md`, `GEMINI.md`, `AGENTS.md`), keep both: ours and theirs.

## 2. Memory ownership

- [ ] Decide who owns canonical durable memory. Often this is the harness with the longest-lived context.
- [ ] Configure all *other* harnesses to write handoffs, not direct memory edits.
- [ ] If multiple harnesses think they own memory, you have a bug; pick one.

## 3. Handoff inbox

- [ ] Create the harness's own `.<harness>/memory-handoffs/` inbox in every repo it works in.
- [ ] Drop the closeout instruction into the harness's instruction file.
- [ ] Test by running a small task and looking for an emitted handoff.

## 4. Routing

- [ ] Confirm `brigade ingest --target <workspace> --dry-run` finds the handoffs.
- [ ] Confirm the auto-promote rules match the handoffs the harness actually writes (filename regex + frontmatter).
- [ ] Watch `memory/handoff-inbox/` for a week; if it fills up, refine handoff quality, do not loosen the rules.

## 5. Doctor

- [ ] Run `brigade doctor --target <workspace> --harness <harness>`.
- [ ] All checks `OK` or explicitly `MANUAL ACTION NEEDED` with a follow-up step.

## 6. Publish gate

- [ ] `hooks/pre-push` installed.
- [ ] `git config core.hooksPath hooks` run once.
- [ ] `brigade scrub --target <workspace> --policy public-repo --dry-run` resolves the embedded guard and policy.

## 7. Verification

```bash
# Bootstrap files in place
ls AGENTS.md CLAUDE.md

# Handoff infrastructure in place
ls .<harness>/memory-handoffs/TEMPLATE.md
ls memory/cards/

# Ingest loop alive (after writing a sample handoff)
brigade ingest --target . --dry-run

# Publish gate live
hooks/pre-push </dev/null || echo "(hook needs git push to exercise)"
```
