# Brigade Stack trim: design

Date: 2026-11-12. Status: draft for operator approval.

This is the umbrella design for shrinking Brigade Stack to a small core. Each
stage below gets its own short design and plan before any code changes. This
document fixes the direction, the order and the rules every stage follows.

Lines marked **(decided)** came from the operator. Lines marked **(proposed)**
are the author's calls and can be overruled.

## Intent

Brigade Stack should be something an engineer who is not its author can adopt
in a day and keep using. Today the first thing a new engineer meets is its
size. Measured on main at `60c0b3046`:

- about 436,000 lines of Python in 912 files under `src/brigade`
- 894 command paths in 61 command groups, per the generated command inventory
- 478 files under `docs/`

Much of that surface exists for one of two reasons that no longer hold as
strongly: models needed rails to avoid skipping steps, and harnesses could not
dispatch or isolate work themselves. Models are better and harnesses now do
both.

## What the product is

**(decided)** Two pillars:

1. **Shared memory.** Any agent, in any harness or cloud app, starts with what
   the others already learned. This is the main thing.
2. **Validated work that produces measurable data.** Engineers who care can
   have agent work checked, and the resulting data helps them improve their
   agents and subagents. This pillar is optional. Yolo mode (#2234) already
   serves people who do not want it.

**(proposed)** A third, supporting capability: **ownership across machines**
(who has claimed what), because two capable agents can still pick up the same
task.

## The test for keeping something

**(decided in principle, wording proposed)** For every area ask: does a
stronger model or a more capable harness make this unnecessary, or more
valuable?

- Keep what gets more valuable: memory across vendors, measurement, ownership.
- Cut what compensates for weak models or thin harnesses.
- Cut what serves a single agent product when a generic path can serve all of
  them.

Previous decisions do not protect anything. **(decided)**

## Goals

- A new engineer connects one agent to shared memory in one sitting and sees a
  second agent use what the first one learned.
- The default install enforces nothing about how checks are run. It records
  what was run and what happened.
- Engineers who opt in get a scorecard that tells them which agents, models
  and subagents get work right.
- The codebase, command count and doc count go down at every stage and never
  back up.

## Non-goals

- A rewrite or a second product. The existing repository, test suite and
  release pipeline are kept.
- New orchestration features. Dispatch, worktrees and child tasks belong to
  the harness.
- Per-product integrations. One generic connector replaces them.
- Changing the Fleet Hub wire contract or the hub memory design shipped in
  0.31.0.

## Anti-goals

These would make the work a failure even if every stage lands:

- An engineer already using Brigade with a cloud agent loses shared memory
  because a product-specific profile was deleted with no generic replacement.
- The strict rails are removed for people who chose them.
- The leak and secret checks (safety rails) weaken. They are not part of this
  trim.
- The size goes down but the first-day experience does not change.

## Constraints

- 0.31.0 ships as planned from `60c0b3046`. Nothing here lands before it.
  **(proposed)**
- The live fleet keeps working at every stage. Every stage is an ordinary
  release.
- A removed command prints a "this was removed in version X" message for one
  release, then disappears. **(proposed)** This is shorter than the
  two-release window the engine rename used.
- Stored data that users own (memory cards, handoffs, receipts, evidence)
  stays readable. Removing a command never deletes user data.
- CI enforces a 2,000-line module ceiling and a size ratchet
  (`scripts/check_size_ratchet_baseline.py`). The trim works with them.

## Approaches considered

1. **Cut in place, area by area.** Delete whole areas in ordered pull
   requests, with a budget that only goes down. The fleet keeps running and
   the work can stop at any stage with a working product. Cost: several
   releases, and what remains keeps its old shape.
2. **New small core, old code frozen.** A fresh package for memory,
   measurement and ownership on the Rust hub. Cleanest result. Cost: two
   products to run, a fleet migration, and test coverage earned again.
3. **Keep everything, hide most of it.** A small default surface with the
   rest behind optional installs. An `extras` switch already does part of
   this. Cost: all the code stays to maintain, so only the appearance
   improves.

**(decided)** Approach 1. Reason: the test suite, release pipeline and live
fleet are assets, and approach 1 reaches about the same place without giving
them up.

## Rules every stage follows

**(proposed)**

1. **Usage check first.** Before a stage removes anything, it lists each
   command and module in scope with evidence of use or non-use. What counts
   as evidence is settled in stage 0.
2. **Budget only goes down.** Stage 0 records lines, modules, command paths
   and doc pages. CI fails a pull request that raises any of them unless the
   pull request carries a stated exception.
3. **Whole areas, not slivers.** A stage removes an area with its tests, docs,
   schemas, registry entries and references in other modules.
4. **Each stage states what it keeps.** If a stage finds that something in
   scope serves a pillar, it keeps it and says why.
5. **Own design and plan per stage.** This document is not a plan for any of
   them.
6. **Stages are independent unless stated.** If a stage is blocked, the later
   ones go ahead. Stage 2 is the one most likely to wait, and stages 3 to 5 do
   not depend on it.
7. **The removal window is per stage.** It starts at the release that ships
   the stage.
8. **Safety rails are named, not inferred.** They are the content guard,
   secret and credential scanning, and the pre-push publication checks, as
   `work_mode.py` defines them. Stage 0 lists the modules that implement them,
   and no stage may remove a listed module.

## Stages, in order

### Stage 0: baseline, budget and usage evidence

- Record the four size numbers and add the downward-only check. The numbers
  are: Python lines under `src/brigade`; Python files under `src/brigade`;
  command paths in the generated command inventory with extras on; files
  under `docs/`. Tests, `engines/` and generated registries are not counted.
  The existing module-size ratchet stays as it is and is separate.
- Decide what counts as usage evidence and where it comes from. This is open:
  the author has not confirmed what local or hub records of command use
  exist.
- Output: a table of every command group with size and usage, which the later
  stages draw from.

### Stage 1: Grok Bot paths out, generic cloud-agent connector in

**(decided)** The operator no longer uses Grok Bot actively and is willing to
lose its paths outright. Someone must still be able to use Brigade with Grok
Bot for shared memory.

- Remove: the private job queue with its leases and feeds, the scout and
  build worker paths with their reconcile and relay steps, and the three
  steward packs (Obsidian, fleet actions, T3 dispatch). This is about 27,000
  lines in `grokbot*` modules and four packages.
- Add: one generic cloud-agent profile on the existing connector, offering
  memory search, card read and handoff submission to anything that speaks
  MCP. The pack registry is closed today and accepts only named packaged
  profiles, so this profile must exist before the Grok-specific memory
  profile goes.
- About 30 modules outside the Grok Bot packages mention it (fleet hub,
  dashboards, cloud dispatch). Those references go in the same stage.
- Gate for deleting the Grok-specific memory profile: the generic profile
  returns the same results as the Grok profile on the existing connector
  checks (tool list, memory search, private cards withheld).
- The Rust hub under `engines/hub` also carries Grok Bot references in its
  claims, heartbeat and schema code. Those stay in this stage, because
  removing hub tables or routes changes the wire contract. They go in a later
  hub release under the contract v2 work, with its own migration and
  rollback.
- Open for the stage design: whether hub messaging is part of the generic
  profile, and whether the Dot and Duet profiles fold into it too.

### Stage 2: Brigade's own orchestration trimmed or removed

**(decided)** Whether this is a full removal or a trim is open, and is settled
on evidence. Brigade must keep working for someone who does not use T3 Code.

**(proposed)** Trim to a thin cross-vendor worker. A harness can dispatch its
own vendor's subagents, but it cannot ask another vendor's agent for work or a
second opinion. That is the one job nothing else does for a user without a
multi-harness app. So keep a small "run this task on that agent CLI and bring
back the result, recorded" command, and remove the planning, admission,
roster policy, budgets and cloud dispatch layered on top of it. The bullets
below describe the larger scope under review, not a settled removal list.

- In scope: `brigade run` and its run records, rosters, seats, model
  admission and the dispatcher. About 40,000 lines.
- Keep: recording the result of work a harness dispatched itself, because
  that feeds the scorecard.
- Depends on the native child-task work under #2046 reaching the point where
  harness-dispatched work can be recorded. If that is not ready, this stage
  waits and the others proceed.
- Receipts and the scorecard feed belong to the verification area, not to
  `run`. Anything in the run records that the scorecard reads moves there
  before the rest is removed.
- Consequence to accept before this stage starts: the operator's own workflow
  routes substantial work through `brigade run` today. That routing changes
  to harness-native dispatch.

### Stage 3: verification becomes observe and score

- Default mode records checks however the agent runs them. Nothing is blocked
  and no receipt is demanded at the end of a session.
- Today's enforcing behavior stays available as an opt-in strict mode. Yolo
  stays as the off switch.
- Existing installs keep the behavior they have: the upgrade writes their
  current mode into the workspace config explicitly, so only new installs get
  the new default. This is proposed and listed as an open question, because
  it decides whether the operator's own fleet changes behavior.
- Add the scorecard as the reason to opt in. What it shows is open and needs
  the operator: the question "which recorded numbers have actually changed a
  decision" was asked and not yet answered.
- Known gap to design around: a receipt proves that a command exited 0, and
  receipts created from a bare command are not scoreable.
- Measured on the operator's main machine on 2026-11-12: of 21,648 stored
  verification receipts, none is scoreable, and none records which model,
  agent or subagent did the work. The existing model scorecard counts worker
  processes that exited cleanly, not verified results.
- **(proposed)** Observe mode records each check an agent runs as its own
  kind of record, separate from receipts, with the model, harness and
  subagent type taken from the hook event. It never stores command output.
- **(proposed)** First report: first-pass check rate by harness and model,
  with sample size and an "unknown" bucket, labeled as observed and not
  independently verified. Second: checks claimed in the final message versus
  checks actually run.
- The mode default is stored today as an absent key, so changing the default
  would silently change every existing install. The upgrade must write the
  current mode explicitly first.

### Stage 4: fleet down to what the pillars need

- Fleet is the largest area: 153 modules and about 71,000 lines, with 101
  command paths.
- Keep ownership (claims), presence, and the memory hub.
- **(decided)** The hub's web dashboards (deck, boards, capacity, projects,
  admin pages) are not used and not necessary. They go.
- **(proposed)** One small page replaces them, served by the hub daemon: what
  is connected and healthy, a memory browser (search, read, archived cards,
  restore), the list of what memory care changed on its own, and later the
  observe-mode reports. It is a viewer over data the core already keeps, with
  no state of its own.
- Everything else in fleet is judged by the stage 0 table. Hub messaging is
  the main open item, because cloud agents use it today.

### Stage 5: first-day experience

- Rewrite the README, quickstart and first-run flow around the first goal:
  one agent connected to shared memory, then a second one benefiting.
- Remove doc pages for removed areas. The doc count is part of the budget.

## Automation: nothing waits on a person forever

**(decided)** Brigade should work in the background without the user having
to act, understand how it works, or intervene. The model is the way a coding
agent remembers from its own chats. An earlier rule that forbade unattended
self-editing of skills, instructions or canonical memory is dropped.

**(proposed)** Every place Brigade stops for a person gets one of four
treatments:

- **Apply automatically** when the change is easy to reverse and low risk.
- **Apply, then check** when it is reversible but worth a second look, with
  an automatic rollback if the check fails.
- **Expire** when nobody acting means it was not important.
- **Stay human** only when the action is irreversible or outward-facing:
  publishing, deleting, credentials, spending.

Unattended jobs follow the same rule. A scheduled job that keeps failing is
fixed, reported once, or switched off. It does not fail quietly for weeks.

**(decided, 2026-11-13)** Brigade Stack is used by agents, not by people, and
setup follows from that:

- Setup is one non-interactive command that an agent can run to the end. It
  asks no questions and leaves no step for a person.
- Setup schedules its own background work. Nobody writes a cron line, a
  systemd unit or a scheduled task by hand, and no documented path depends on
  one. Today `brigade care install` is a separate opt-in step. It becomes part
  of setup, and an upgrade adds any schedule entry a new release needs.
- Once the local daemon from Stage B exists, it owns the schedule and setup
  starts it. Until then setup registers entries with the platform scheduler
  itself.
- Output and errors are written for an agent to act on: a stable machine
  format, and an error that names the next command to run.
- A feature that only works after a person performs a manual step is not
  finished.

### Stage A: memory tends itself

This runs alongside stages 1 to 3 and does not wait for the scorecard.

**(decided)** Memory care is fully automatic. Cards that are uncertain do not
sit waiting for the user. Cards that no longer matter, such as notes for a
course the user has finished, are archived without being asked.

**(proposed)**

- Archive, never delete. An archived card leaves search and recall but can be
  restored, so every automatic decision is reversible.
- An uncertain card resolves itself: it is checked against available evidence
  and either confirmed, corrected or archived within a fixed time.
- Staleness is judged from use and subject, not age alone: cards nobody has
  read, and cards about work that has ended.
- The user gets a short periodic summary of what was archived, merged or
  corrected. Reading it is optional.
- Self-editing of instructions and skills follows later, after stage 3,
  because it needs trustworthy measurement.

Measured on the operator's main machine on 2026-11-12:

- 363 handoffs sit in the review inbox, the oldest about 201 days old, and
  300 more drafts are unprocessed.
- The scheduled memory refresh job only writes a plan, the closeout job
  records a deferral every day, and the outcome job is disabled and would
  only preview.
- Retiring a stale card has no apply path at all. It is a read-only list.
- Card verification is off by default and can never correct a card itself.

So the jobs exist and report success while the loop stays open. The stage
closes these specific loops before adding anything new. It also updates the
written rules that require review queues: `GOVERNANCE.md`,
`docs/memory-care.md` and `docs/phase-memory-verify.md`.

**(decided)** The background work may spend the user's subscription quota
without asking each time, under a cap the user sets once.

Open for the stage design: which model does the background work and what the
default cap is.

### Stage B: the hub is core and easy to run

**(decided)** The hub owns memory and talks to every agent, so it is a core
part of the product, not an optional add-on. Nobody should need an LXC
container to run it. A small daemon or a first-class local Docker setup is
wanted, and it must not be complicated.

**(decided)** The three items marked decided below were approved by the
operator on 2026-11-12.

- **Default: a small local daemon (decided).** One command starts the hub as a
  background service for the current user, with a local database, bound to
  this machine only, with no token or config for the user to prepare. Agents
  on the same machine find it without setup.
- **Second supported path: one published container image** with a short run
  command. The current Compose recipe (holder namespace, HTTPS front, setup
  script, secrets files, locally built image) stays as an advanced hardening
  guide, not the way in.
- **Adding a machine is one step:** a pairing code or a single address and
  token. Brigade does not try to solve private networking. The user brings
  whatever they already use to reach their own machines.
- **One memory path (decided).** Memory always goes through the hub,
  including on a single machine. This retires the separate hubless memory
  path and the sync, mirror, push and pull commands between the two.
- **Native hub on macOS and Windows (decided, in scope).** The native hub
  binary installs on Linux only today and still runs the Python hub as a
  child process. Closing that gap is part of this stage. Until it lands,
  those users get the Python hub.
- Hub messaging between agents stays in the core, because the hub is how
  agents reach each other.

## How success is measured

- **Size:** the four budget numbers at each stage. No overall target is set
  until stage 0 produces the usage table. **(proposed)**
- **Memory reaching agents:** the existing connector test, where cloud agents
  gave a card-backed answer on 3 of 10 prompts. Target 8 of 10, the figure
  already used for hub memory.
- **First day:** a person who has never used Brigade follows the quickstart
  and gets a second agent to use a memory from the first, with no help.

## Left to the builder

- The mechanics of the downward-only budget check and how exceptions are
  stated.
- How removed commands print their one-release message.
- The order of pull requests inside each stage.
- Naming of the generic connector profile and the verification modes.

## Open questions for the operator

1. Is the proposed first scorecard right: first-pass check rate by harness
   and model, then checks claimed versus checks run?
2. Is the one-release removal window acceptable, given existing installs on
   0.30.0 and 0.31.0? Proposed: yes.
3. When observe-and-score becomes the default, do existing installs stay
   strict? Proposed: yes, by writing their current mode explicitly at
   upgrade.

## Decisions and findings recorded on 2026-11-13

- **(decided)** Message encryption is cut in two steps. Step 1, the client
  side, merged in #2536. Step 2 removes the hub storage, routes, schema and
  the Rust hub code with the next hub contract version. Until then the hub
  restore text and the hub architecture doc still describe statement signing.
- **(decided)** `0.32.0` is the removal version for every removed command.
- **(found)** Archiving against a live hub works with a 0.31.0 sync running
  beside it. Cards whose hub id collides with another card are archived
  locally and skipped on the hub, so the hub can keep a live card under the
  shared id. The archive step needs a rule for that case.
- **(found)** The archive folder sits inside the tree that another memory
  search on the same machine may index. Archived cards leave Brigade search
  and the hub but can still surface there. The archive location or an ignore
  marker needs a decision.
- **(found)** The built-in handoff ingester reads local folders only. A
  machine that collects handoffs from other hosts cannot switch to it until
  handoffs travel through the hub. Held review drafts written by another
  ingester are not readable by the inbox drain.
- **(found)** A one-time model triage cleared a 359-draft review inbox with
  one item left for the owner. The same triage, run on a schedule under the
  quota cap, is the design for the background model step in Stage A.
- **(decided)** Message encryption stays removed. A hub that a person runs on
  a rented server is self-hosted: that person is the hub operator, so hiding
  messages from the operator protects nothing, and the memory cards sit on
  the same disk in plain text. Two smaller protections replace it and belong
  to the hub stage:
  - The hub refuses to listen on a non-loopback address unless the
    connection is encrypted (TLS) or the operator declares a private network.
    Setup picks the safe choice without asking a person.
  - Hub backups are encrypted with a key the user holds, which covers memory
    as well as messages.
  Neither defends against a hub run by a third party for many people. That
  would need memory encryption too and is outside this design.
- **(done)** The archive rule for shared and old card ids merged in #2537. The
  hub copy follows the cards still in use: the last holder archived retires
  it, one remaining holder takes it over, undo restores it, and the background
  hub step repairs archives that were skipped earlier. The skips seen in the
  field came from published records written by older releases with no source
  path, not from two files sharing an id. One case is left open in #2539: a
  takeover whose reply is lost, followed by the holder going private.
