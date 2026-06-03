# First-Class OpenCode Handoff Source Design

> **In plain terms:** OpenCode is listed as a supported writer harness, but Brigade never actually wired it in. You can only ingest OpenCode handoffs today by passing a manual `--handoff-inbox` flag. This makes `.opencode/memory-handoffs/` a built-in handoff source everywhere the other writers (Claude, Codex) already are: install scaffolding, ingest, doctor, the fleet sweep, security scan skip-list, the interactive selector, and the handoff-sources example. Along the way it removes the triplicated writer-inbox map that has already caused drift bugs this week.

## Goal

Promote OpenCode to a first-class writer harness for the handoff pipeline so `.opencode/memory-handoffs/` is recognized, scaffolded, ingested, doctored, swept, and skipped-by-security automatically, with no manual flag.

## Background

- `selection.KNOWN_HARNESSES = ("claude", "codex", "openclaw", "hermes")` does **not** include `opencode`, so `brigade init --harnesses opencode` fails validation today.
- The writer-harness-to-inbox map is duplicated byte-for-byte in three places: `install._WRITER_INBOX`, `ingest._WRITER_INBOXES`, `doctor._WRITER_INBOXES`. Two more spots hardcode the same two inbox paths: `repos_cmd` (`_repo_summary` handoff scan) and `security_cmd.SKIP_PREFIXES`. The handoff-sources example (`templates/handoff/handoff-sources.example.json`) lists them too. This duplication is the same drift trap that produced two separate bugs this session.
- Per-harness install scaffolding is driven by harness manifests under `templates/harnesses/<id>.json` plus template files under `templates/<id>/`. `codex.json` is the clean reference: role `writer`, one `TEMPLATE.md` file, one `processed` dir.

## Non-Goals

- OpenCode bootstrap-file support beyond handoffs (e.g. a dedicated bridge file). Codex itself has none ("AGENTS.md is in the depth baseline"); OpenCode follows the same pattern. AGENTS.md already comes from the depth baseline.
- Making OpenCode a memory **owner**. It is a writer; the canonical owner is unchanged.
- Any new dependency. Standard library and existing template machinery only.
- Changing OpenCode's own config or models (handled separately as an environment task).

## Architecture

### 1. Single source of truth for the writer-inbox map

Add to `selection.py` (a leaf module, no import cycles) next to `KNOWN_HARNESSES`:

```python
# Writer harness id -> repo-relative handoff inbox dir. Single source of truth;
# install, ingest, doctor, and the fleet sweep all consume this.
WRITER_INBOXES = {
    "claude": ".claude/memory-handoffs",
    "codex": ".codex/memory-handoffs",
    "opencode": ".opencode/memory-handoffs",
}
```

And add `"opencode"` to `KNOWN_HARNESSES`.

Then replace the three local dicts with imports:
- `install.py`: delete `_WRITER_INBOX`, `from .selection import WRITER_INBOXES`, use it in `build_gitignore_block`.
- `ingest.py`: delete `_WRITER_INBOXES`, import and use `WRITER_INBOXES` in `_resolve_inbox_paths`.
- `doctor.py`: delete `_WRITER_INBOXES`, import and use `WRITER_INBOXES` in `_check_handoff_inboxes`.

Derive the fleet sweep from it:
- `repos_cmd._repo_summary`: iterate `WRITER_INBOXES.values()` instead of the hardcoded 2-tuple.

`security_cmd.SKIP_PREFIXES` is a mixed tuple (handoff dirs plus `.brigade/*` dirs), so it stays a literal but gains `(".opencode", "memory-handoffs")`. A focused test asserts every `WRITER_INBOXES` path is present in `SKIP_PREFIXES` so this can't silently drift.

### 2. OpenCode harness manifest and template

- New `templates/harnesses/opencode.json` mirroring `codex.json`: id `opencode`, role `writer`, files `[{"src": "opencode/memory-handoffs/TEMPLATE.md", "dst": ".opencode/memory-handoffs/TEMPLATE.md"}]`, dirs `[".opencode/memory-handoffs/processed"]`.
- New `templates/opencode/memory-handoffs/TEMPLATE.md`, identical content to `templates/codex/memory-handoffs/TEMPLATE.md` (the handoff template is harness-agnostic).

With these, `brigade init --harnesses opencode` creates `.opencode/memory-handoffs/TEMPLATE.md` + `processed/`, and `build_gitignore_block` emits the matching ignore lines automatically (it reads `WRITER_INBOXES`).

### 3. Interactive selector

`prompt.py` adds `opencode` to `_HARNESS_ORDER` and `_HARNESS_LABELS` (e.g. `"opencode": "OpenCode"`) so the numbered selector offers it like the others.

### 4. Handoff-sources example and doctor coverage

- `handoff_cmd.py` has its **own** `WRITER_INBOXES` (a tuple of path strings, used in ~6 places for source-coverage checks and draft scanning). Re-derive it from the canonical map to avoid a fourth divergent copy:
  ```python
  from .selection import WRITER_INBOXES as _WRITER_INBOX_MAP
  WRITER_INBOXES = tuple(_WRITER_INBOX_MAP.values())
  ```
  This keeps the existing module-local name and its tuple-of-paths shape (so every current usage is unchanged) while flowing the opencode entry through automatically.
- `templates/handoff/handoff-sources.example.json`: add `.opencode/memory-handoffs` to the default `sources[0].inboxes` list so a fresh config covers OpenCode.
- A test confirms `handoff_cmd.WRITER_INBOXES` contains `.opencode/memory-handoffs`, so an `.opencode/memory-handoffs` directory is recognized as a known writer inbox (not flagged as an unknown/uncovered source).

## Data Flow

```
brigade init --harnesses opencode
   └─ install: opencode.json manifest -> .opencode/memory-handoffs/{TEMPLATE.md,processed/}
                build_gitignore_block (reads WRITER_INBOXES) -> ignore .opencode/memory-handoffs/*

OpenCode writes .opencode/memory-handoffs/<handoff>.md
   └─ brigade ingest: _resolve_inbox_paths (reads WRITER_INBOXES) -> routes/promotes -> archives to processed/
   └─ brigade doctor / handoff doctor: reports the opencode inbox
   └─ brigade repos scan: counts opencode handoff backlog in the fleet
   └─ brigade security scan: skips .opencode/memory-handoffs (untrusted handoff content not flagged as repo injection)
```

## Error Handling

- Unknown harness validation is unchanged; `opencode` simply joins the valid set.
- Installing into a repo that already has `.opencode/memory-handoffs/TEMPLATE.md` hits the existing `--force` conflict guard (no new path).
- A repo with an `.opencode/memory-handoffs` dir but no Brigade config still falls back through `_resolve_inbox_paths`' legacy `.claude` path only when no config exists; with a config listing `opencode`, the opencode inbox resolves normally.

## Testing

- **selection:** `test_selection.py` - `opencode` is accepted by `validate()`; `WRITER_INBOXES` includes it. Update any assertion that pins `KNOWN_HARNESSES` to the old 4-tuple.
- **install:** installing a selection with `harnesses=["opencode"]` creates `.opencode/memory-handoffs/TEMPLATE.md` and `processed/`, and the gitignore block contains `.opencode/memory-handoffs/*` and `!.opencode/memory-handoffs/TEMPLATE.md`.
- **ingest:** a handoff in `.opencode/memory-handoffs/` (with a config selecting opencode) is promoted/routed like a codex one; archived to `.opencode/memory-handoffs/processed/`.
- **doctor:** `_check_handoff_inboxes` reports an `opencode` inbox OK when present, FAIL when missing.
- **repos:** `_repo_summary` counts an `.opencode/memory-handoffs` inbox in `handoff_inboxes`.
- **security:** a parametrized/explicit test asserts every `WRITER_INBOXES` value appears in `SKIP_PREFIXES` (drift guard), and a file under `.opencode/memory-handoffs/` is skipped by the scanner.
- **handoff:** `handoff_cmd.WRITER_INBOXES` includes `.opencode/memory-handoffs`; the handoff-sources example JSON lists it.
- **prompt:** the selector lists `opencode` (covered by existing prompt/work_cmd tests if they assert the harness set; update them).
- Full suite stays green.

## Rollout

- Branch `feat/opencode-handoff-source` off `main`.
- Flip the ROADMAP "Promote OpenCode to a first-class built-in handoff source" item from `Status: proposed (next)` to implemented.
- CHANGELOG: Unreleased / Added. README harness list updated. Regenerate `docs/command-inventory.md` if the harness set is documented there.
- No release.
- Live dogfood: install opencode into a scratch repo, have OpenCode (gpt-oss via ollama-cloud) write a handoff, run `brigade ingest`, confirm it routes and archives.
