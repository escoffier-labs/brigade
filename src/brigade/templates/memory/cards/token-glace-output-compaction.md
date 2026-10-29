---
topic: token-glace-output-compaction
type: tool-runbook
tags: [tools, tokenjuice, output-compaction, claude-code, codex]
status: starter
---

# TokenJuice Output Compaction

The tokens station uses upstream [TokenJuice](https://github.com/vincentkoc/tokenjuice).
The legacy card path stays stable for existing references. Brigade owns installation
and health checks at the process boundary. Reducers and artifact storage belong to TokenJuice.

### Install and qualify

`brigade add tokens` selects `tokenjuice@0.8.5`, a released baseline. It does
not claim that the proposed footer repair has shipped. Codex structured JSON
feedback does not use the text-wrap footer, so the released baseline can be
qualified independently for that route. Footer acceptance is not a global
activation gate. An explicitly approved, reviewed development artifact may also
be selected. Qualify the actual host event and tool-name matching before cutover.
Discovery checks the npm package name and upstream repository behind the executable
symlink. A Glace-owned `tokenjuice` alias is not an upstream installation. Unknown
standalone distributions need explicit qualification.

`brigade tokens status`, `doctor`, and `wire plan` do not activate hooks or migrate data.
`wire plan --write` saves a plan only. Adding the station never activates TokenJuice hooks.
A legacy `brigade add token-glace` request resolves to the new owner.

### Host cutover

Review `brigade tokens wire plan`. Back up the selected host's owned configuration,
both executable aliases and raw artifacts. Remove only the old compactor's owned
entries through its installer. Preserve unrelated hooks. Install one selected
compactor route per host and qualify actual host loading with synthetic output.
Codex hooks must use an absolute upstream executable path. Mixed-name double-wrap
detection belongs to the upstream shared pre-tool guard. Brigade does not duplicate it.
OpenClaw remains disabled and its bundled dependency is separately owned.

### Evidence and rollback

```bash
tokenjuice doctor hooks --format json
tokenjuice stats --format json --timezone utc
tokenjuice wrap -- git status --short
tokenjuice wrap --raw -- git status --short
```

Compacted output may omit relevant evidence. Recover captured output with the
artifact instructions or use raw output for exact logs and diffs. Capture limits
can also truncate raw output. Review side effects before rerunning commands.

TokenJuice starts a new statistics epoch. Preserve legacy sidecars for recovery
and rollback. Do not combine totals or infer billing savings. Rollback removes
only the new owned entries and restores the saved owner and configuration, then
repeats the same synthetic host checks. Keep new artifacts separately.
