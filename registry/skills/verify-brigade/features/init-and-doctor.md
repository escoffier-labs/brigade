# init and doctor

Installing Brigade into a repo or workspace, and checking afterwards that the
install is actually wired. This is the first thing a new user runs and the
first thing that breaks when templates, harness manifests, or doctor checks
change.

## Sub-features

- `brigade init --depth {repo,workspace}` writes bootstrap files
  (`AGENTS.md`, `CLAUDE.md`, `MEMORY.md`, `TOOLS.md`, `USER.md`, `SOUL.md`,
  `IDENTITY.md`, `HEARTBEAT.md`, `SAFETY_RULES.md`), `.brigade/`, `rules/`,
  `hooks/`, and `scripts/`.
- `--harnesses claude,codex,...` decides which harness dirs get files, and
  which built-in skills (`brigade-work`, `ultra-work-scout`) get wired into
  them. `--harnesses none` is the generic install.
- The gitignore block between `>>> brigade gitignore block >>>` markers, or
  `.git/info/exclude` with `--git-exclude`.
- The memory owner (`brigade: memory owner -> claude`), overridable with
  `--owner`.
- `--dry-run`, `--force`, `--no-wire`, `--profile`, `--include`.
- `brigade doctor` returns per-check `OK` / `WARN` / `FAIL` / `MANUAL`,
  plus `ready`, `depth`, `harnesses`, `owner`, and a summary count.
  `--agent` drops passes, `--json` makes it machine-readable, `--full` stops
  condensing, `--operator` adds host-global checks.

## How to get to it (user POV)

A user clones or creates a repo and runs, from `CONTRIBUTING.md`:

```bash
target="$(mktemp -d)"
git init -q -b main "$target"
python -m brigade init --target "$target" --depth workspace --harnesses claude,codex
python -m brigade doctor --target "$target"
```

Then they read the last lines of `init` (which harnesses were wired, who owns
memory) and the doctor summary. The number they care about is `0 failed`.

## Driving it with control-brigade

```bash
C=registry/skills/verify-brigade/control-brigade.py

# creates the dir, git init, brigade init; prints "target"
$C new-target --depth workspace --harnesses claude,codex

TARGET=<the target it printed>

# re-run init against an existing dir you already own
$C init --target "$TARGET" --depth workspace --harnesses claude

# the check
$C doctor-target --target "$TARGET"
```

Proof of a healthy install, all from `doctor-target` output:

- `"ok": true` and helper exit 0
- `"ready": true`
- `"summary": {"failed": 0, ...}` - warns are fine, fails are not
- `"owner"`, `"depth"`, `"harnesses"` match what you asked `init` for

Plus, on disk: `$TARGET/.brigade/config.json` exists, and
`$TARGET/.claude/skills/brigade-work/` exists when claude was in
`--harnesses`. Check the files, not only the summary.

Observed baseline on a fresh `--depth workspace --harnesses claude,codex`
target: `{"failed": 0, "manual": 0, "ok": 54, "total": 61, "warn": 6}`,
`ready: true`, `owner: claude`. A change that moves `failed` off zero, or
drops `ok` sharply, is the regression.

## Gotchas

- `brigade init` has no `--json`. Its output is prose; assert on exit code and
  on the files it wrote. `doctor` does have `--json` - use it.
- Six WARNs on a fresh target are expected, not a bug: memory-care, security,
  backups, notifications, and friends are not initialized by `init`.
- `init` refuses to install into `$HOME` without `--allow-home`. Do not pass
  that flag while verifying - the helper refuses the home directory outright.
- Piping `brigade doctor --json` into `head` returns exit 120 (SIGPIPE), not
  the real exit code. Redirect to a file when the exit code matters.
- `init` is idempotent-ish but not free: without `--force` it leaves existing
  files alone, so a second `init` against a dirty target proves less than a
  first `init` against a fresh one. Use `new-target` per proof.
- Re-running `init` rewrites everything between the gitignore block markers.
  Custom ignores added there do not survive.
