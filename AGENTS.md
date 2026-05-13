# AGENTS.md - Workspace Rules

## Memory Owner

The configured memory owner is **OpenClaw**. Side harnesses may keep local session context, but durable knowledge must be written as a Memory Handoff in `.claude/memory-handoffs/`. The memory owner ingests those handoffs into canonical durable memory.

Do not create a second canonical memory system. If a session produced durable knowledge, write the handoff and let the owner route it.

## Every Session

- Read the repo-local instructions before editing.
- Prefer root-cause fixes over surface patches.
- Run the smallest meaningful verification before claiming success.
- Ask before destructive, production-impacting, or dependency-adding work.

## Memory Handoff

If a session discovers durable knowledge - architecture decisions, workflow changes, non-obvious fixes, setup gotchas, security findings, reusable commands, durable research, or user preferences - create a handoff at the end of the task.

Write the handoff to `.claude/memory-handoffs/<YYYY-MM-DD-HHMM>-<slug>.md` using the format in `.claude/memory-handoffs/TEMPLATE.md`.

Do not wait to be reminded. Do not edit canonical memory directly unless this is the memory owner.

## Safety

- Never expose secrets, private hostnames, account IDs, or internal endpoints in public output.
- Use deterministic scrubbers before publishing generated content.
- Do not bypass security checks unless the user explicitly accepts the risk.
- Read `SAFETY_RULES.md` for hard boundaries.

## Multi-Agent Workflow

- Delegate bounded tasks with clear ownership.
- Keep write scopes separate when multiple agents work in parallel.
- Integrate results before reporting completion.

## Solo-mise repo-specific rules

This is the source repo for `solo-mise` itself. The files here are templates that get installed into *other* directories, so the bar for safety is higher than usual.

### Before committing

- `python -m pytest -q` must pass (currently 40 tests).
- `PYTHONPATH=$HOME/repos/content-guard/src python -m content_guard scan . --policy $HOME/repos/content-guard/policies/public-repo.json` must report `Clean.` or warn-only.
- New profile JSON entries must use relative paths only - no absolute paths, no `..` segments. The path validator in `src/solo_mise/init.py:_ensure_safe_rel` enforces this at runtime; tests cover it in `tests/test_init.py::test_init_rejects_unsafe_profile_paths`.

### Invariants worth not breaking

- **Ingester `promote_cards` and `route_documents` default to `False`.** They are opt-in. Codex flagged the original "default on" as a BLOCKER for a public-safety installer. If you find yourself flipping these, write a memory handoff explaining why first.
- **`init` refuses `$HOME` as target unless `--allow-home`.** Don't relax this without an alternative guard.
- **`init --dry-run` does not mkdir.** Verified by `test_dry_run_creates_no_files_or_dirs`.
- **Inboxed handoffs are copied verbatim, not reconstructed.** Reviewers need to see what the harness actually wrote.

### Template hygiene

- Every text template under `src/solo_mise/templates/` gets `{{placeholder}}` substitution. Keep placeholders bounded to: `memory_owner`, `memory_owner_name`, `profile`, `harness`. New placeholders require a corresponding entry in `init.py::context`.
- Templates must pass content-guard's `public-repo` policy. The repo's own pre-push hook scans them. Inline allow tags (`<!-- content-guard: allow <rule-id> -->`) are okay for documented examples; bulk-disabling rules is not.

### Releases

See `RELEASE.md` for the checklist. Tag, push, verify pipx install from tag.

### OpenClaw integration

This repo is dogfooded with `--profile openclaw`. The fragments under `.solo-mise/openclaw/` are placeholders - if you actually wire this repo into a live OpenClaw workspace, edit them with real provider/model ids before merging into `~/.openclaw/openclaw.json`. The fragments use `<provider/main-model-id>` style sentinels that will fail the gateway if merged unedited.
