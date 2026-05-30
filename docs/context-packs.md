# Context Packs

`brigade context` builds local context engineering packs for task, repo, release, and tool-use scenarios.

Commands:

```bash
brigade context plan
brigade context build
brigade context list
brigade context show <pack-id>
brigade context archive <pack-id>
```

Packs are written under `.brigade/context/packs/` and stay gitignored. A pack contains safe summaries and references, not raw private evidence.

Pack contents include:

- selected task id and acceptance criteria when available
- README, ROADMAP, and CHANGELOG summaries
- guidance-file presence and short safe summaries
- recent work closeout
- recent review finding summaries
- recent security summary
- selected tool catalog references
- private evidence excluded by default
- source references and freshness status
- context sync plan with no writes

Default exclusions include raw chat exports, secret-looking values, private infrastructure values, full local logs, private absolute paths, and raw scanner output.

`brigade context archive` moves a local pack into `.brigade/context/archive/`. It does not delete source files, write harness context files, edit memory, or run tools.
