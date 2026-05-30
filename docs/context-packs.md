# Context Packs

`brigade context` builds local context engineering packs for task, repo, release, and tool-use scenarios.

Commands:

```bash
brigade context plan
brigade context build
brigade context list
brigade context show <pack-id>
brigade context archive <pack-id>
brigade context sync plan <pack-id|latest>
brigade context sync record <pack-id|latest>
brigade context doctor
brigade context import-issues
```

Packs are written under `.brigade/context/packs/` and stay gitignored. A pack contains safe summaries and references, not raw private evidence.

Pack contents include:

- selected task id and acceptance criteria when available
- README, ROADMAP, and CHANGELOG presence plus line-count summaries
- guidance-file presence summaries without copying guidance contents
- recent work closeout
- recent review finding summaries
- recent security summary
- selected tool catalog references
- private evidence excluded by default
- source references and freshness status
- context sync plan with no writes

Default exclusions include raw chat exports, secret-looking values, private infrastructure values, full local logs, private absolute paths, and raw scanner output.

`brigade context sync plan` reads configured local harness destinations from `.brigade/context/sync-targets.json` and compares them to a built context pack. It reports missing destinations, current managed destinations, stale managed destinations, unmanaged conflicts, stale pack age, and missing source references. `brigade context sync record` writes the read-only plan receipt under `.brigade/context/sync-plans/`.

Sync planning never writes harness context files. A future explicit apply command would be required before any configured destination is mutated.

`brigade context doctor` reports stale context packs, missing source references, task acceptance criteria that changed after pack build, stale tool references, and sync-plan blockers. `brigade context import-issues` routes those issues into the work inbox as `source: context-pack` tasks with stable fingerprints and dismiss-until-changed behavior.

`brigade context archive` moves a local pack into `.brigade/context/archive/`. It does not delete source files, write harness context files, edit memory, or run tools.

Repo-fleet actions can also build action-scoped context packs:

```bash
brigade repos actions context plan <fleet-action-id>
brigade repos actions context build <fleet-action-id>
```

These packs are written in the target repo under `.brigade/context/packs/`. They include the fleet action id, safe repo label, safe action summary, acceptance criteria, guidance-file presence, local receipt labels, dispatch state, and explicit private-evidence exclusions. They do not copy raw guidance contents, raw logs, raw scanner output, private paths, exact private repo names, owner names, org names, hostnames, or secrets.
