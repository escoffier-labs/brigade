# Brigade Phase 226-250 Plan

This plan keeps the next AFK hardening range bounded and auditable. The local phase execution ledger under `.brigade/work/phases/` is still the source of truth for run evidence, commits, pushes, privacy checks, and closeout.

## Objective

Harden AFK session execution into a more useful daily-driver, release, and recovery surface. The work should make session progress easier to checkpoint, resume, review, and prove in release evidence without adding a daemon, scheduler, remote mutation, or automatic code execution.

## Phase Map

| Phase | Deliverable |
| --- | --- |
| 226 | Add phase session checkpoints. |
| 227 | List and inspect phase session checkpoints. |
| 228 | Compare phase session checkpoints against current session state. |
| 229 | Import checkpoint blockers into the work inbox. Implemented with `brigade work phases session checkpoints import-issues`, dry-run support, and deduped `source: phase-session-checkpoint` imports. |
| 230 | Surface checkpoints in session next and resume. Implemented with latest checkpoint summaries and issue counts in both outputs. |
| 231 | Add session recovery notes. Implemented with local recovery-note records, list/show commands, session references, and activity timeline events. |
| 232 | Add recovery note closeout. Implemented with reviewed, deferred, blocked, and archived note closeout metadata. |
| 233 | Add daily phase checkpoint candidates. Implemented with `phase-session-checkpoint` daily plan candidates for unresolved checkpoint issues. |
| 234 | Allow daily run to write one checkpoint. Implemented with the `write-phase-session-checkpoint` daily adapter. |
| 235 | Add phase session risk summary. Implemented with read-only risk output across next-step, checkpoint, recovery-note, and doctor signals. |
| 236 | Add session verification rollup. Implemented with read-only status counts and missing or failed phase lists. |
| 237 | Add session privacy rollup. Implemented with read-only clean, blocked, and missing privacy-check counts. |
| 238 | Add session handoff rollup. |
| 239 | Add release doctor checkpoint evidence. |
| 240 | Add release candidate checkpoint evidence. |
| 241 | Add center checkpoint review items. |
| 242 | Add work brief checkpoint summary. |
| 243 | Add phase checkpoint action queue. |
| 244 | Add checkpoint archive. |
| 245 | Add session recovery report section. |
| 246 | Add AFK session health schema. |
| 247 | Add wrapper safe resume protocol. |
| 248 | Add phase session release compare checks. |
| 249 | Add AFK session self-audit command. |
| 250 | Close phase 226-250 AFK hardening gate. |

## Stop Condition

Stop only when phases 226-250 have individual local ledger records, each implemented or explicitly deferred phase has evidence, commits, verification, privacy checks, docs where needed, pushed commit evidence, a linted Memory Handoff, and the session gate says the range is safe to claim complete.
