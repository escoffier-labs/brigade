## Session close

- [ ] Verify captured through Brigade? (`brigade work verify run --target . --command "..." --capture brigade-work`)
- [ ] Outcome recorded? (`brigade outcome capture <skill-or-card-id> --run-id latest`)
- [ ] Memory Handoff written when durable knowledge was produced?

## Brigade

Brigade is the local work loop for this workspace: briefs, verification receipts, outcomes, and Memory Handoffs.

### Start here

1. Run `brigade work brief --target .` and follow it before editing.
2. Run counting checks through Brigade, not raw.
3. After substantial work, write a Memory Handoff; never edit canonical memory directly.

### Hard rules

- Verify through Brigade when the result should count.
- Capture outcomes (failures included) so the ledger stays honest.
- Handoff at the end of substantial work.

### Work loop protocol

Invoke the `using-skillet` skill and use each applicable reviewed skill before substantive work in a Brigade-wired repository.

Run `brigade work brief --target .` when a `.brigade/` directory exists or `brigade status --target .` succeeds, and follow the brief before editing.

Route worker-sized or parallelizable implementation through `brigade run` and keep the frontier session on planning, dispatch, review, and synthesis.

Run counting checks through `brigade work verify run --target . --command "<command>" --capture brigade-work`, capturing failures as evidence so the outcome ledger stays honest.

After substantial work, create a Memory Handoff through the standard canonical-owner flow and never edit canonical memory directly.
