# First-Class OpenCode Handoff Source Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `.opencode/memory-handoffs/` a first-class built-in handoff source across install, ingest, doctor, fleet sweep, security skip-list, the interactive selector, and the handoff-sources example, and centralize the writer-inbox map so it stops being duplicated.

**Architecture:** Add a single canonical `WRITER_INBOXES` dict (plus `opencode` in `KNOWN_HARNESSES`) to `selection.py`, then point `install.py`, `ingest.py`, `doctor.py`, `repos_cmd.py`, and `handoff_cmd.py` at it. Add an `opencode` harness manifest + handoff template, the security skip entry, the interactive selector entries, and docs.

**Tech Stack:** Python 3.10+, standard library only, pytest. Use `python3` to run pytest.

**Models for subagents:** opus. Never haiku.

---

## File Structure

- Modify: `src/brigade/selection.py` - add `opencode` to `KNOWN_HARNESSES`; add canonical `WRITER_INBOXES` dict.
- Modify: `src/brigade/install.py` - drop local `_WRITER_INBOX`, import canonical map.
- Modify: `src/brigade/ingest.py` - drop local `_WRITER_INBOXES`, import canonical map.
- Modify: `src/brigade/doctor.py` - drop local `_WRITER_INBOXES`, import canonical map.
- Modify: `src/brigade/repos_cmd.py` - derive handoff scan dirs from the canonical map.
- Modify: `src/brigade/handoff_cmd.py` - derive its `WRITER_INBOXES` tuple from the canonical map.
- Modify: `src/brigade/security_cmd.py` - add `(".opencode", "memory-handoffs")` to `SKIP_PREFIXES`.
- Modify: `src/brigade/prompt.py` - add `opencode` to selector order + labels.
- Create: `src/brigade/templates/harnesses/opencode.json` - writer manifest.
- Create: `src/brigade/templates/opencode/memory-handoffs/TEMPLATE.md` - handoff template.
- Modify: `src/brigade/templates/handoff/handoff-sources.example.json` - add opencode inbox.
- Modify: `ROADMAP.md`, `CHANGELOG.md`, `README.md` - docs.
- Tests: `tests/test_selection.py`, `tests/test_install.py` (or equivalent), `tests/test_ingest.py`, `tests/test_doctor.py`, `tests/test_repos*.py`, `tests/test_security*.py`, `tests/test_handoff*.py`.

---

## Task 1: Canonical WRITER_INBOXES + opencode in KNOWN_HARNESSES

**Files:**
- Modify: `src/brigade/selection.py`
- Test: `tests/test_selection.py`

- [ ] **Step 1: Write/extend the failing test**

Add to `tests/test_selection.py` (match existing imports; `from brigade import selection`):

```python
def test_opencode_is_a_known_harness():
    from brigade.selection import KNOWN_HARNESSES, WRITER_INBOXES
    assert "opencode" in KNOWN_HARNESSES
    assert WRITER_INBOXES["opencode"] == ".opencode/memory-handoffs"


def test_writer_inboxes_cover_known_writers():
    from brigade.selection import WRITER_INBOXES
    assert WRITER_INBOXES["claude"] == ".claude/memory-handoffs"
    assert WRITER_INBOXES["codex"] == ".codex/memory-handoffs"
```

Also: if `tests/test_selection.py` has an assertion pinning `KNOWN_HARNESSES` to the exact 4-tuple `("claude","codex","openclaw","hermes")`, update it to include `"opencode"`.

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_selection.py -q`
Expected: FAIL (ImportError on `WRITER_INBOXES` or KeyError / missing opencode).

- [ ] **Step 3: Implement**

In `src/brigade/selection.py`, change `KNOWN_HARNESSES` to include opencode and add the map below it:

```python
KNOWN_HARNESSES = ("claude", "codex", "opencode", "openclaw", "hermes")

# Writer harness id -> repo-relative handoff inbox dir. Single source of truth;
# install, ingest, doctor, the fleet sweep, and the handoff doctor consume this.
WRITER_INBOXES = {
    "claude": ".claude/memory-handoffs",
    "codex": ".codex/memory-handoffs",
    "opencode": ".opencode/memory-handoffs",
}
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_selection.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd <repo-root>
git add src/brigade/selection.py tests/test_selection.py
git commit -m "feat(selection): canonical WRITER_INBOXES map; register opencode harness"
```

---

## Task 2: Point install/ingest/doctor at the canonical map

**Files:**
- Modify: `src/brigade/install.py`, `src/brigade/ingest.py`, `src/brigade/doctor.py`
- Test: existing `tests/test_install.py`, `tests/test_ingest.py`, `tests/test_doctor.py`

- [ ] **Step 1: Replace install's local dict**

In `src/brigade/install.py`, delete:
```python
# Writer harness -> inbox-dir prefix. Only writer harnesses have an inbox.
_WRITER_INBOX = {
    "claude": ".claude/memory-handoffs",
    "codex": ".codex/memory-handoffs",
}
```
Add `WRITER_INBOXES` to the existing `from .selection import ...` line (or add `from .selection import WRITER_INBOXES`). In `build_gitignore_block`, change `inbox = _WRITER_INBOX.get(h)` to `inbox = WRITER_INBOXES.get(h)`.

- [ ] **Step 2: Replace ingest's local dict**

In `src/brigade/ingest.py`, delete:
```python
# Writer harness id -> inbox dir (mirror of install._WRITER_INBOX).
_WRITER_INBOXES = {
    "claude": ".claude/memory-handoffs",
    "codex": ".codex/memory-handoffs",
}
```
Add `from .selection import WRITER_INBOXES` with the other imports. In `_resolve_inbox_paths`, change `rel = _WRITER_INBOXES.get(h)` to `rel = WRITER_INBOXES.get(h)`.

- [ ] **Step 3: Replace doctor's local dict**

In `src/brigade/doctor.py`, delete:
```python
# Writer harness -> inbox-dir prefix. Only writer harnesses have an inbox.
_WRITER_INBOXES = {
    "claude": ".claude/memory-handoffs",
    "codex": ".codex/memory-handoffs",
}
```
Add `from .selection import WRITER_INBOXES` (or extend the existing selection import). In `_check_handoff_inboxes`, change `rel = _WRITER_INBOXES.get(h)` to `rel = WRITER_INBOXES.get(h)`.

- [ ] **Step 4: Run the affected suites**

Run: `python3 -m pytest tests/ -k "install or ingest or doctor" -q`
Expected: PASS (no regression; the refactor is behavior-preserving for claude/codex).

- [ ] **Step 5: Commit**

```bash
git add src/brigade/install.py src/brigade/ingest.py src/brigade/doctor.py
git commit -m "refactor(handoff): consume canonical WRITER_INBOXES in install/ingest/doctor"
```

---

## Task 3: OpenCode harness manifest + handoff template

**Files:**
- Create: `src/brigade/templates/harnesses/opencode.json`
- Create: `src/brigade/templates/opencode/memory-handoffs/TEMPLATE.md`
- Test: `tests/test_install.py` (or the install test module)

- [ ] **Step 1: Write the failing install test**

First inspect an existing install test to match fixtures/helpers:
Run: `python3 -m pytest tests/ -k install -q` and open `tests/test_install.py`.

Add a test (adapt the install entrypoint/fixture names to the file's conventions; the project installs via `brigade.install.install_selection` with a `Selection`):

```python
def test_opencode_install_creates_inbox_and_gitignore(tmp_path):
    from brigade.install import install_selection, build_gitignore_block
    from brigade.selection import Selection
    sel = Selection(depth="repo", harnesses=["opencode"], owner="opencode", includes=[])
    rc = install_selection(tmp_path, sel)
    assert rc == 0
    assert (tmp_path / ".opencode" / "memory-handoffs" / "TEMPLATE.md").is_file()
    assert (tmp_path / ".opencode" / "memory-handoffs" / "processed").is_dir()
    block = build_gitignore_block(sel)
    assert ".opencode/memory-handoffs/*" in block
    assert "!.opencode/memory-handoffs/TEMPLATE.md" in block
```

(If `Selection`'s field names or `owner` validation differ, mirror an existing passing install test for codex and swap `codex`->`opencode`.)

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/ -k opencode_install -q`
Expected: FAIL (manifest/template missing -> `install_selection` returns nonzero "template missing", or assertion fails).

- [ ] **Step 3: Create the manifest**

Create `src/brigade/templates/harnesses/opencode.json`:

```json
{
  "id": "opencode",
  "role": "writer",
  "description": "OpenCode handoff inbox. (AGENTS.md is in the depth baseline; no separate bridge file today.)",
  "files": [
    {"src": "opencode/memory-handoffs/TEMPLATE.md", "dst": ".opencode/memory-handoffs/TEMPLATE.md"}
  ],
  "dirs": [
    ".opencode/memory-handoffs/processed"
  ]
}
```

- [ ] **Step 4: Create the template**

Copy the codex handoff template verbatim:

```bash
mkdir -p src/brigade/templates/opencode/memory-handoffs
cp src/brigade/templates/codex/memory-handoffs/TEMPLATE.md src/brigade/templates/opencode/memory-handoffs/TEMPLATE.md
```

Confirm the file is non-empty: `head -3 src/brigade/templates/opencode/memory-handoffs/TEMPLATE.md`.

- [ ] **Step 5: Run to verify pass**

Run: `python3 -m pytest tests/ -k "opencode_install or install" -q`
Expected: PASS.

- [ ] **Step 6: Verify packaging includes the new template**

Check `MANIFEST.in` / `pyproject.toml` package-data globs already cover `templates/**`. If templates are included by a recursive glob (e.g. `recursive-include src/brigade/templates *`), nothing to do. If each harness dir is listed explicitly, add the `opencode` template path. Run:
`grep -n "templates" MANIFEST.in pyproject.toml`
and only edit if opencode would otherwise be excluded.

- [ ] **Step 7: Commit**

```bash
git add src/brigade/templates/harnesses/opencode.json src/brigade/templates/opencode/ tests/
[ -n "$(git diff --cached --name-only MANIFEST.in pyproject.toml)" ] && true
git commit -m "feat(handoff): opencode harness manifest and handoff template"
```

---

## Task 4: Fleet sweep + security skip-list coverage

**Files:**
- Modify: `src/brigade/repos_cmd.py`, `src/brigade/security_cmd.py`
- Test: `tests/test_repos*.py`, `tests/test_security*.py`

- [ ] **Step 1: Write failing tests**

Security drift guard + skip behavior (add to the security test module; match its imports):

```python
def test_skip_prefixes_cover_all_writer_inboxes():
    from brigade.security_cmd import SKIP_PREFIXES
    from brigade.selection import WRITER_INBOXES
    for rel in WRITER_INBOXES.values():
        parts = tuple(rel.split("/"))
        assert parts in SKIP_PREFIXES, f"{rel} not skipped by security scan"
```

Repos handoff inbox detection (add to the repos test module; mirror an existing `_repo_summary` test if present):

```python
def test_repo_summary_counts_opencode_inbox(tmp_path):
    from brigade import repos_cmd
    (tmp_path / ".opencode" / "memory-handoffs").mkdir(parents=True)
    # Build a RepoEntry the same way the existing repos tests do; then:
    summary = repos_cmd._repo_summary(_make_entry(tmp_path))
    assert ".opencode/memory-handoffs" in summary["handoff_inboxes"]
```

(If `_repo_summary`/`RepoEntry` construction differs, copy the pattern from an existing repos test; the assertion on `handoff_inboxes` is the point.)

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/ -k "skip_prefixes_cover or opencode_inbox" -q`
Expected: FAIL.

- [ ] **Step 3: Implement**

In `src/brigade/repos_cmd.py` `_repo_summary`, replace the hardcoded tuple:
```python
    handoff_inboxes = [
        inbox
        for inbox in (".claude/memory-handoffs", ".codex/memory-handoffs")
        if (repo / inbox).is_dir()
    ]
```
with a derivation from the canonical map (add `from .selection import WRITER_INBOXES` if not already imported):
```python
    handoff_inboxes = [
        inbox
        for inbox in WRITER_INBOXES.values()
        if (repo / inbox).is_dir()
    ]
```

In `src/brigade/security_cmd.py`, add the opencode entry to `SKIP_PREFIXES`:
```python
SKIP_PREFIXES = (
    (".brigade", "runs"),
    (".brigade", "security"),
    (".brigade", "work"),
    (".claude", "memory-handoffs"),
    (".codex", "memory-handoffs"),
    (".opencode", "memory-handoffs"),
)
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/ -k "repos or security" -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/brigade/repos_cmd.py src/brigade/security_cmd.py tests/
git commit -m "feat(handoff): cover opencode inbox in fleet sweep and security skip-list"
```

---

## Task 5: Handoff doctor coverage + selector + handoff-sources example

**Files:**
- Modify: `src/brigade/handoff_cmd.py`, `src/brigade/prompt.py`, `src/brigade/templates/handoff/handoff-sources.example.json`
- Test: `tests/test_handoff*.py`

- [ ] **Step 1: Write failing tests**

Add to the handoff test module:

```python
def test_handoff_writer_inboxes_include_opencode():
    from brigade import handoff_cmd
    assert ".opencode/memory-handoffs" in handoff_cmd.WRITER_INBOXES


def test_handoff_sources_example_lists_opencode():
    import json
    from brigade.templates import template_root
    data = json.loads((template_root() / "handoff" / "handoff-sources.example.json").read_text())
    assert ".opencode/memory-handoffs" in data["sources"][0]["inboxes"]
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/ -k "writer_inboxes_include_opencode or sources_example_lists_opencode" -q`
Expected: FAIL.

- [ ] **Step 3: Implement**

In `src/brigade/handoff_cmd.py`, replace:
```python
WRITER_INBOXES = (".claude/memory-handoffs", ".codex/memory-handoffs")
```
with a derivation from the canonical map (add the import near the top):
```python
from .selection import WRITER_INBOXES as _WRITER_INBOX_MAP

WRITER_INBOXES = tuple(_WRITER_INBOX_MAP.values())
```
Leave every existing use of `WRITER_INBOXES` in the file unchanged (it stays a tuple of path strings).

In `src/brigade/templates/handoff/handoff-sources.example.json`, add `".opencode/memory-handoffs"` to `sources[0].inboxes` (after the codex entry).

In `src/brigade/prompt.py`, add `opencode` to `_HARNESS_ORDER` (after `codex`) and add `"opencode": "OpenCode"` to `_HARNESS_LABELS`.

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/ -k "handoff or prompt or work_cmd" -q`
Expected: PASS. If a prompt/work_cmd test pins the exact harness list, update it to include opencode.

- [ ] **Step 5: Commit**

```bash
git add src/brigade/handoff_cmd.py src/brigade/prompt.py src/brigade/templates/handoff/handoff-sources.example.json tests/
git commit -m "feat(handoff): opencode in handoff doctor coverage, selector, and sources example"
```

---

## Task 6: End-to-end ingest test + docs

**Files:**
- Test: `tests/test_ingest.py`
- Modify: `ROADMAP.md`, `CHANGELOG.md`, `README.md`, `docs/command-inventory.md` (if it lists harnesses)

- [ ] **Step 1: Write the end-to-end ingest test**

Add to `tests/test_ingest.py`, mirroring an existing codex/claude ingest test but installing opencode. The point: a handoff in `.opencode/memory-handoffs/` is promoted and archived to `processed/`.

```python
def test_opencode_handoff_is_ingested(tmp_target: Path):
    from brigade.install import install_selection
    from brigade.selection import Selection
    install_selection(tmp_target, Selection(depth="workspace", harnesses=["opencode"], owner="opencode", includes=[]))
    inbox = tmp_target / ".opencode" / "memory-handoffs"
    _write_handoff(
        inbox,
        "2026-06-02-1200-opencode.md",
        """\
        # Memory Handoff

        ## Recommended memory action
        create-card

        ## Target card
        opencode-test.md

        ## Suggested card content
        ---
        topic: opencode-test
        ---
        body line
        """,
    )
    rc = ingest_mod.run(target=tmp_target, dry_run=False, promote_cards=True, route_documents=True)
    assert rc == 0
    assert (tmp_target / "memory" / "cards" / "opencode-test.md").is_file()
    assert (inbox / "processed" / "2026-06-02-1200-opencode.md").is_file()
```

(Reuse the module's existing `_write_handoff` helper and `tmp_target` fixture. If `install_selection` with `owner="opencode"` is rejected by owner validation, use a valid owner the other ingest tests use and keep `harnesses=["opencode"]`.)

- [ ] **Step 2: Run to verify pass**

Run: `python3 -m pytest tests/test_ingest.py -k opencode -q`
Expected: PASS (the prior tasks already wired the path).

- [ ] **Step 3: Update docs**

- `ROADMAP.md`: change the "Promote OpenCode to a first-class built-in handoff source" bullet's `Status: proposed (next).` to `Status: implemented with .opencode/memory-handoffs/ wired into install scaffolding, ingest, doctor, handoff doctor source coverage, the fleet sweep, the security skip-list, and the interactive selector, plus a template scaffold.`
- `CHANGELOG.md`: under `## [Unreleased]` -> `### Added`:
  ```
  - First-class OpenCode handoff support: `.opencode/memory-handoffs/` is now a built-in writer inbox (install scaffolding, ingest, doctor, handoff doctor source coverage, fleet sweep, security skip-list, and the interactive selector), so OpenCode handoffs ingest without a manual `--handoff-inbox` flag. The writer-inbox map is now centralized in `brigade.selection.WRITER_INBOXES`.
  ```
- `README.md`: if it enumerates supported writer harnesses (search for "Codex" / "OpenClaw" near "handoff" or "harness"), add OpenCode to that list.

- [ ] **Step 4: Regenerate command inventory if applicable**

Run: `grep -rn "opencode\|claude\|codex" docs/command-inventory.md | head`
If the inventory documents the harness set and a generator exists, run `python3 -m brigade roadmap commands --write` (the project's documented regenerator) and include the diff. If the inventory does not list harnesses, skip.

- [ ] **Step 5: Full suite + commit**

Run: `python3 -m pytest -q`
Expected: PASS (all).

```bash
git add tests/test_ingest.py ROADMAP.md CHANGELOG.md README.md docs/
git commit -m "feat(handoff): end-to-end opencode ingest test and docs"
```

---

## Verification (no commit)

- [ ] `python3 -m pytest -q` fully green.
- [ ] `grep -rn "memory-handoffs\"" src/brigade/*.py` shows the only literal claude/codex/opencode inbox dicts live in `selection.py` (other modules import or derive from it). The security `SKIP_PREFIXES` literal is the one allowed exception (guarded by a test).
- [ ] `python3 -m brigade init` (or the install path) with opencode selected creates `.opencode/memory-handoffs/{TEMPLATE.md,processed/}` in a scratch dir.
