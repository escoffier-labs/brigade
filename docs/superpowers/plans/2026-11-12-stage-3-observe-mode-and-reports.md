# Stage 3 Observe Mode and Check Reports Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a three-way work mode (`strict`, `observe`, `off`) in which `observe` records every check an agent runs as its own record type with harness and model identity, and ship two read-only reports over those records, without changing the default or today's strict behavior.

**Architecture:** The mode stays one key, `work.mode` in `.brigade/config.json`, now always written explicitly. Observe-mode capture lives in new modules (`agent_identity.py`, `observed_checks.py`, `claude_hooks/check_events.py`, `observe_harness_hooks.py`, `observe_claims.py`, `observed_report.py`) that the existing hook entry points call with one line each. Observed records are append-only JSONL in the private per-workspace state directory beside the digest key, separate from verify receipts, never signed or digest-chained.

**Tech Stack:** Python 3 standard library only (zero runtime dependencies), pytest, ruff, mypy. Claude Code, Codex and Cursor hook payloads as documented by each vendor on 2026-11-13.

**Spec:** `docs/superpowers/specs/2026-11-12-brigade-stack-trim-design.md`, section "Stage 3: verification becomes observe and score". This plan covers only the capture and reporting half. The default flip is out of scope.

## Decisions made by the planner

1. **Canonical stored names are `strict`, `observe`, `off`.** `engineer` and `enforced` read as `strict`. `yolo` reads as `off`. The writer emits only canonical names. An older Brigade reading `strict`, `observe` or `off` treats the unknown value as engineer (its fail-safe) and its doctor warns, so a shared config set to `off` by this version is enforced by an older binary. That is the safe direction and is stated in the changelog.
2. **Two constants replace `DEFAULT_WORK_MODE`.** `ABSENT_WORK_MODE = "strict"` is what a config without the key, an unreadable config, or an invalid value means, permanently. `NEW_INSTALL_WORK_MODE = "strict"` is what `brigade init` writes into a new config. The later default flip changes only `NEW_INSTALL_WORK_MODE`, so a workspace the upgrade never reached still resolves to `strict`.
3. **"The upgrade writes the current mode" means three paths.** `write_config` always writes `work.mode`, so any `init`, `reconfigure` or `mode set` pins it. `brigade work brief` (which every hooked Claude session runs at start) calls `work_mode.pin_explicit_mode`, which writes the resolved mode only into a keyless, readable config that git does not track. A tracked keyless config is never rewritten silently and doctor reports it as unpinned. No new command is added for this.
4. **`work_mode.is_yolo` is deleted.** It is replaced by `is_strict` and `is_observe`, so any caller left on two-way logic fails to import instead of silently treating `observe` as one of the other two values.
5. **Observe behaves like off for every enforcement surface**: PreToolUse deny, the PostToolUseFailure retry note, the Stop receipt gate and its handoff note, doctor outcome-loop findings, classify dormancy and adoption "bypassed" counting. Observe differs from off only in that capture runs.
6. **Capture runs only for targets whose governing mode is `observe`.** Strict and off write no observed records. Per-target resolution follows the existing rule: the directory each check runs in decides, and an unplaceable check follows the handled root.
7. **Record storage:** `<user-config>/brigade/observed/<repository-key-hash>/checks.jsonl` (checks) and adjacent `claims.jsonl` (claims), owner-only directory and files, one JSON object per line. One bounded OS file lock covers size check, rotation at 4 MiB to a single `.1` generation, and append. Readers decode active then old generation under the same lock, newest first within each, returning at most 10,000 records. Appends never parse either generation. Close the active descriptor before rotation. Torn final lines are skipped by readers and separated by a newline before append. Command text is never stored. `command_digest` uses HMAC-SHA-256 after shell credential masking, `guard.redact_text`, `claude_hooks.envelope.redact_paths` and the 1,000-character cap, truncated to 16 lowercase hex characters. Each canonical workspace path has a directory named by SHA-256 of `work_mode.repository_key(target)` under the per-user rails record parent, in `observed/`. It contains `digest.key`, `checks.jsonl`, `checks.jsonl.1` and `checks.jsonl.lock`. The same directory will hold claims and session state. Nothing is written under the workspace. Ignore any older workspace observed directory, without migration. Refuse state inside any Git work tree. Existing POSIX directories must be owned by the effective user with no group or other bits. Keys must be effective-user-owned, single-link regular files, exactly 32 bytes and mode 0600. Publish a fully written, fsynced temporary key without overwriting a winner. Windows relies on inherited profile ACLs. Key files are exclusive, owner-only and no-follow. State or key failure disables appends and emits `0000000000000000`, which readers accept as unavailable and reports must never group. `tool` is the only human-readable description: a known tool name with an optional known subcommand or target restricted to 32 letters, digits or `._-:/`. Unknown targets become the bare tool name. Environment assignments, option values and arbitrary positional arguments never enter `tool`. Records containing the legacy `command` key are skipped and counted. The schema key set stays closed. No output, output hash or output size is stored.
8. **Status mapping per harness** (anything else is `unknown`): Claude Code `PostToolUse` is `ok`. `PostToolUseFailure` with an `error` starting `Exit code N` is `failed` with `exit_code=N`. An interrupt or any other failure text is `unknown`. Cursor `postToolUse` reads `exitCode` from the JSON string in `tool_output`. `postToolUseFailure` with `failure_type == "error"` is `failed`, `timeout` is `unknown`, `permission_denied` is not recorded. Codex is always `unknown`, because the `tool_response` shape for Bash is not documented.
9. **Verify receipts learn the model through a new variable, `BRIGADE_AGENT_SESSION`.** The Claude SessionStart handler appends `export BRIGADE_AGENT_SESSION=<session fingerprint>` to `CLAUDE_ENV_FILE`. It deliberately does not export `BRIGADE_CLAUDE_SESSION`, because the strict Stop gate matches receipts on `harness_session`, and stamping it on every receipt would change strict behavior.
10. **Outcome records carry identity under `context["agent_identity"]`, not in `context["model"]`.** The capability fingerprint is computed from `context["model_family"]`, so moving the model there would shift `outcome rank` capability cohorts. The new key is outside the capability vector.
11. **`PostModelSwitch` joins the Claude managed events** so a `/model` switch updates the cached model. Existing installs pick it up on the next `brigade work hooks install`. Until then the model comes from SessionStart only.
12. **Codex capture uses a project-scope `.codex/hooks.json`** written when `brigade work mode set observe` runs on a target that selects the `codex` harness. Its command is the existing hidden `brigade work presence-hook` with `--harness codex --context codex-observe`, so no command path is added. Codex loads project hooks only after the user trusts the project `.codex/` layer, so `brigade work mode --json` reports whether the hooks file is installed and the docs name the trust step.
13. **Cursor capture rides the existing presence hook** and adds `postToolUseFailure` to the managed Cursor events.
14. **Grok learns the three values but captures nothing**, because its hook payload fields are not verified.
15. **One new command path: `brigade work checks`**, with `--claims` for the second report. The word "score" appears in neither its name nor its output.
16. **First pass** is the earliest record per group key `(source, session_fingerprint, tool, command_digest)`. The unavailable digest marker (`0000000000000000`) makes each record a separate sample and must never group records together. Receipts without a session fingerprint are grouped per target per UTC day. Receipt status `completed` is `ok`, `failed` is `failed`, `canceled` is `unknown`, `rejected` is excluded (Brigade refused to run it). Reports read live receipts only, not `verify-archive/`.
17. **The claim phrase list** is a built-in default, replaced entirely by `.brigade/claim-phrases.txt` when that file exists, one `<kind>: <phrase>` per line, `#` comments. Kinds are `tests`, `lint`, `types`, `build` and `checks` (`checks` is generic and matches any tool). A phrase match is ignored when the 12 characters before it contain `not ` or `n't `. The message text is never stored, only the matched kinds.
18. **`runtime.py` shrinks instead of growing.** The PostToolUseFailure retry note moves into `claude_hooks/check_events.py`, which pays for the two lines that wire capture in, and the `LEGACY_OVERSIZED` cap for `runtime.py` is lowered to the new count.
19. **Harness detection moves** from `outcome_cmd._detect_harness` into `agent_identity.detect_harness`. `outcome_cmd` imports it. Harness names stay `claude-code`, `codex`, `cursor`, matching existing outcome records.
20. **Caller enumeration was done with grep** over `src/`, `docs/` and templates, because `brigade code callers` had no code-graph index in the planning checkout. Implementers run `brigade code callers` and `impact` on each named symbol before editing when an index exists.

## Global Constraints

- All pull requests target the `overhaul` branch, not `main`. Conventional commits, with the coding agent's co-author trailer as AGENTS.md describes.
- One changelog fragment per user-visible PR, `changelog.d/<slug>.md` with `- ` bullets only. Never edit `CHANGELOG.md`. Run `python3 scripts/changelog_fragments.py check`.
- Checks run through Brigade: `brigade work verify run --target . --argv-json '["./scripts/verify-focused","<selector>",...]' --capture brigade-work`. Each task names its selectors.
- The default does not change: an absent key resolves to `strict`, and new installs are written as `strict`.
- `strict` behaves exactly as `engineer` does today, under every alias.
- Safety rails stay separate and unchanged: no new code reads or writes `work-rails.json`, guard policies, baselines or `templates/hooks/pre-push`.
- Do not change the skill outcome ratchet, `verify_trial` eligibility, `scorecard.py` or `model_scorecard.py`. The word "score" is reserved for the manifest-backed lane.
- An observed check is never a receipt: never written under `verify-runs/`, never given `digests`, `signature` or a chain link.
- Never store command output.
- Module ceiling 2,000 lines. `LEGACY_OVERSIZED` entries never rise. `src/brigade/claude_hooks/runtime.py` must end every PR at or below 3,335 lines (its size at plan time). `src/brigade/doctor.py` is at 1,982 lines and must not grow.
- At most one new command path (`brigade work checks`). No new doc pages. Edit `docs/technical-guide.md`, `docs/agent-assisted-setup.md` and `docs/claude-hook-session-state.md`.
- Public repository: no em dashes, no private host names or home paths, fake placeholders in fixtures with a reviewed `content-guard: allow` tag where a fake secret is needed.

## Review Focus

1. **A check command that carries a secret** (`curl -H "Authorization: Bearer <token>"`, a token in a URL) must be stored redacted, and the stored file must pass the guard scan. Pinned by `test_observed_file_passes_guard_scan` (Task 5) and `test_observe_records_passing_and_failing_checks` (Task 6, token case).
2. **A capture failure** (unwritable `.brigade/work`, full disk, a bug in the parser) must never deny, block or change any hook result. Pinned by `test_capture_failure_never_changes_hook_result` (Task 6) and `test_cursor_capture_failure_keeps_presence_output` (Task 8).
3. **An unreadable, invalid or unsupported-version config** must resolve to `strict`, and the pin must never overwrite it. Pinned by `test_unreadable_config_resolves_to_strict` and `test_pin_writes_current_mode_once_and_skips_tracked_or_unreadable_config` (Task 1).
4. **Mixed modes in one session:** an observe workspace running a check in a strict child repo is still denied and records nothing there. A strict workspace running a check in an observe child is allowed and records into the child only. Pinned by `test_observe_repo_cd_into_strict_repo_is_denied` (Task 2) and `test_check_in_observe_child_records_only_in_child` (Task 6).
5. **A truncated or interleaved JSONL line** (two hook processes appending, a killed writer) must be skipped and counted, never crash a report. Pinned by `test_read_skips_corrupt_lines_and_counts_them` (Task 5) and `test_corrupt_lines_reported_not_fatal` (Task 10).

## Callers that branch on the mode today

Every site found by grep for `is_yolo`, `MODE_YOLO`, `MODE_ENGINEER`, `WORK_MODES`, `DEFAULT_WORK_MODE`, `LEGACY_WORK_MODE_ALIASES`, `resolve_mode`, `verification_in_yolo` and the literal `"yolo"`:

| Site | What it decides | Task |
| --- | --- | --- |
| `config.py:32-36,53,56-62,226-229,299-305` | allowed values, aliases, writer omission, loader fallback | 1 |
| `work_mode.py` `MODE_*`, `resolve_mode`, `is_yolo`, `set_mode`, `doctor_checks` | resolution and doctor rows | 1 |
| `claude_hooks/verification_mode.py:181` `verification_in_yolo` | which mode governs a check | 2 |
| `claude_hooks/runtime.py:2373` PreToolUse deny | deny raw checks | 2 |
| `claude_hooks/runtime.py:2490` PostToolUseFailure note | retry note | 2 (moved in 6) |
| `claude_hooks/runtime.py:2526` Stop gate | block on missing receipt | 2 |
| `claude_hooks/classify.py:92,125` | `dormant` and state `yolo` | 2 |
| `claude_hooks/install_cmd.py:493` | `work_mode` field in hook status | 2 |
| `doctor.py:131-134` `_check_outcome_loop` | skip outcome-loop findings | 2 |
| `doctor.py:828` `_check_claude_work_loop` | clean state is OK | 2 |
| `repos_cmd/adoption.py:29,242,279,285` | adoption state and bypass counting | 2 |
| `brief_sections.py:94-96,192-201` | session-close items and brief mode line | 3 |
| `work_cmd/session/briefing.py:345-353,671` | brief payload mode. Pin call site | 1 (pin), 3 (text) |
| `install.py:355-365,527-533` | merged config mode. Agent hint text | 1, 3 |
| `cli/init.py:79-98` | `--yolo`, `--engineer` flags | 3 |
| `cli/work/registration.py:112-122` | `mode set` metavar and help | 3 |
| `work_mode_cmd.py` | `mode set` and hook reconcile | 3 (Codex in 9) |
| `status.py:126` | prints `work_mode.summary` | 3 (value spelling only) |
| `templates/grok/hooks/brigade-work.sh:127-139,221,249` | Grok deny and close-out | 3 |
| `docs/technical-guide.md`, `docs/agent-assisted-setup.md` | mode documentation | 3 |
| `agents.py:299` `mode = ... "yolo"` | worker sandbox flag, not the work mode | none |

---

## Pull request 1: `feat(work): three-way work mode with explicit config writes`

Changelog fragment: `changelog.d/stage3-work-modes.md`.

### Task 1: Mode values, aliases, explicit writes and the pin

**Files:**
- Modify: `src/brigade/config.py:30-62,190-230,295-306`
- Modify: `src/brigade/work_mode.py` (constants, `resolve_mode`, `is_yolo`, `set_mode`, new `is_strict`, `is_observe`, `pin_explicit_mode`, `doctor_checks`)
- Modify: `src/brigade/install.py:355-365` (`_merged_config`)
- Modify: `src/brigade/work_cmd/session/briefing.py:345` (`_brief_payload`)
- Test: `tests/test_work_mode.py`, `tests/test_config.py`

**Interfaces:**
- Produces: `config.WORK_MODES = ("strict", "observe", "off")`, `config.ABSENT_WORK_MODE = "strict"`, `config.NEW_INSTALL_WORK_MODE = "strict"`, `config.LEGACY_WORK_MODE_ALIASES = {"engineer": "strict", "enforced": "strict", "yolo": "off"}`, `config.validate_work_mode(value: Any) -> str` (canonical name or `ValueError`).
- Produces: `work_mode.MODE_STRICT = "strict"`, `MODE_OBSERVE = "observe"`, `MODE_OFF = "off"`. `resolve_mode(target: Path) -> str`. `is_strict(target: Path) -> bool`. `is_observe(target: Path) -> bool`. `pin_explicit_mode(target: Path) -> str | None` (the mode written, or None when nothing was written). `MODE_ENGINEER`, `MODE_YOLO`, `DEFAULT_WORK_MODE` and `is_yolo` no longer exist.

- [ ] **Step 1: Write the failing tests** in `tests/test_work_mode.py`

```python
@pytest.mark.parametrize(
    ("value", "expected"),
    [("strict", "strict"), ("observe", "observe"), ("off", "off"),
     ("engineer", "strict"), ("enforced", "strict"), ("yolo", "off")],
)
def test_mode_aliases_resolve_to_canonical(tmp_path, value, expected):
    target = _wired(tmp_path)
    _plant_mode(target, value)          # edits work.mode in the JSON directly
    assert work_mode.resolve_mode(target) == expected
    assert config.load_config(target).work_mode == expected

def test_writer_always_writes_work_mode(tmp_path):
    target = tmp_path / "t"
    config.write_config(target, config.Config(version=1, selection=_selection()))
    assert json.loads(config.config_path(target).read_text())["work"] == {"mode": "strict"}

def test_absent_key_resolves_to_strict_and_new_install_writes_it(tmp_path):
    target = _wired(tmp_path)
    assert json.loads(config.config_path(target).read_text())["work"] == {"mode": "strict"}
    _drop_mode_key(target)
    assert work_mode.resolve_mode(target) == "strict"

@pytest.mark.parametrize("content", ["{", '{"version": 99, "work": {"mode": "off"}}',
                                     '{"version": 1, "work": {"mode": "chaos"}}'])
def test_unreadable_config_resolves_to_strict(tmp_path, content):
    path = tmp_path / "t" / ".brigade" / "config.json"
    path.parent.mkdir(parents=True)
    path.write_text(content)
    assert work_mode.resolve_mode(tmp_path / "t") == "strict"
    assert work_mode.is_strict(tmp_path / "t") is True

def test_pin_writes_current_mode_once_and_skips_tracked_or_unreadable_config(tmp_path):
    untracked = _wired(tmp_path, name="untracked"); _drop_mode_key(untracked)
    assert work_mode.pin_explicit_mode(untracked) == "strict"
    assert json.loads(config.config_path(untracked).read_text())["work"] == {"mode": "strict"}
    assert work_mode.pin_explicit_mode(untracked) is None
    tracked = _wired(tmp_path, name="tracked"); _drop_mode_key(tracked); _git_track_config(tracked)
    before = config.config_path(tracked).read_bytes()
    assert work_mode.pin_explicit_mode(tracked) is None
    assert config.config_path(tracked).read_bytes() == before
    broken = tmp_path / "broken" / ".brigade" / "config.json"
    broken.parent.mkdir(parents=True); broken.write_text("{")
    assert work_mode.pin_explicit_mode(tmp_path / "broken") is None and broken.read_text() == "{"

def test_brief_pins_an_unpinned_untracked_config(tmp_path, capsys):
    target = _wired(tmp_path); _drop_mode_key(target)
    assert cli.main(["work", "brief", "--target", str(target), "--json"]) == 0
    assert json.loads(config.config_path(target).read_text())["work"] == {"mode": "strict"}

def test_doctor_reports_an_unpinned_tracked_config(tmp_path):
    target = _wired(tmp_path); _drop_mode_key(target); _git_track_config(target)
    rows = work_mode.doctor_checks(target)
    assert ("INFO", "work.mode") in {(status, name) for status, name, _ in rows}
```

Add the small helpers `_plant_mode`, `_drop_mode_key`, `_git_track_config` (runs `git init` and `git add .brigade/config.json` in the target) and `_selection` next to `_wired`.

The existing `test_config_round_trips_mode_and_never_writes_rails` asserts `"work" not in` the default config. The owner decided the writer stops omitting the key, so change that one assertion to `== {"mode": "strict"}` and its `"yolo"` write to `"off"`. Keep the rails assertions as they are. In `test_invalid_settings_fail_safe_in_hook_readers` the expected value becomes `"strict"`. Say so in the PR body: the spelling changed by decision, the behavior asserted did not.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `brigade work verify run --target . --argv-json '["./scripts/verify-focused","tests/test_work_mode.py","tests/test_config.py"]' --capture brigade-work`
Expected: FAIL (`ValueError: work.mode must be one of: engineer, yolo` for `observe`, missing `pin_explicit_mode`).

- [ ] **Step 3: Implement the constants, writer and loader in `config.py`**

`Config.work_mode` defaults to `NEW_INSTALL_WORK_MODE`. `load_config` falls back to `ABSENT_WORK_MODE` for an absent key and for an invalid value. `write_config` always sets `payload["work"] = {"mode": validate_work_mode(cfg.work_mode)}`. Error text lists `strict, observe, off`.

- [ ] **Step 4: Implement `resolve_mode`, `is_strict`, `is_observe`, `pin_explicit_mode` and the doctor rows in `work_mode.py`**

`resolve_mode` returns `ABSENT_WORK_MODE` on any read or validation failure. `pin_explicit_mode` reads the raw JSON first and returns None unless the file is readable, has a supported version and has no `work.mode`. It returns None when `git -C <target> ls-files --error-unmatch .brigade/config.json` exits 0 (tracked) or git cannot be started (`OSError`). Otherwise it writes through the existing `_update(target, work_mode=resolve_mode(target))` path. `doctor_checks` gives one INFO line per mode (strict keeps today's engineer wording with the new name. Observe: "checks are recorded, nothing is blocked". Off keeps today's yolo wording) and adds `("INFO", "work.mode", ...)` when the key is absent, naming `brigade work mode set strict` as the way to pin it. The invalid-value WARN text says "using strict".

- [ ] **Step 5: Wire the pin and the new-install default**

`install._merged_config` keeps a recorded mode and uses `NEW_INSTALL_WORK_MODE` only for a config it creates. `briefing._brief_payload` calls `work_mode.pin_explicit_mode(target)` before `work_mode.summary(target)` and ignores the result.

- [ ] **Step 6: Run the tests to verify they pass**

Run the Step 2 command. Expected: PASS, with `ruff`, `mypy` and both snapshot checks clean.

- [ ] **Step 7: Commit**

```bash
git add src/brigade/config.py src/brigade/work_mode.py src/brigade/install.py src/brigade/work_cmd/session/briefing.py tests/test_work_mode.py tests/test_config.py
git commit -m "feat(work): add strict, observe and off work modes with explicit config writes"
```

### Task 2: Enforcement callers handle the third value

**Files:**
- Modify: `src/brigade/claude_hooks/verification_mode.py:181-202`
- Modify: `src/brigade/claude_hooks/runtime.py:2373-2375,2487-2491,2526` (same line count)
- Modify: `src/brigade/claude_hooks/classify.py:92-126`
- Modify: `src/brigade/claude_hooks/install_cmd.py:491-494`
- Modify: `src/brigade/doctor.py:131-134,828` (same line count)
- Modify: `src/brigade/repos_cmd/adoption.py:21-30,240-243,277-287`
- Test: `tests/test_work_mode.py`, `tests/test_claude_hooks_verification_mode.py`, `tests/test_repos_adoption_cmd.py`, `tests/test_claude_hooks_doctor.py`

**Interfaces:**
- Consumes: `work_mode.resolve_mode`, `is_strict` (Task 1).
- Produces: `verification_mode.governing_modes(command: object, cwd: object, root: Path) -> list[tuple[Path, str]]` (one `(target, mode)` per verifier, root standing in for unplaceable or unwired ones, deduplicated by target, empty for a non-string or blank command). `verification_mode.verification_enforced(command: object, cwd: object, root: Path) -> bool` (True when any governing mode is `strict`, or when `governing_modes` finds no verifier and `root` is strict). `verification_in_yolo` is removed.
- Produces: classify state values `"off"` and `"observe"` replace `"yolo"`. `adoption.ADOPTION_STATES` holds `"off"` and `"observe"` instead of `"yolo"`. `adoption._unenforced_use(use: dict[str, Any], status: str) -> dict[str, Any]` replaces `_yolo_use`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_work_mode.py`:

```python
@pytest.mark.parametrize("mode", ["observe", "off", "yolo"])
def test_unenforced_modes_never_deny_or_block(tmp_path, monkeypatch, capsys, mode):
    target = _wired(tmp_path); _set_mode(target, mode, capsys); _quiet_session_start(monkeypatch)
    runtime.handle_payload("SessionStart", _payload(target, "SessionStart"))
    assert runtime.handle_payload("PreToolUse", _bash(target, "PreToolUse", "python -m pytest -q")) is None
    runtime.handle_payload("PostToolUse", _write(target))
    assert runtime.handle_payload("PostToolUseFailure", _bash(target, "PostToolUseFailure", "pytest -q")) is None
    assert runtime.handle_payload("Stop", _payload(target, "Stop", stop_hook_active=False)) is None

@pytest.mark.parametrize("mode", ["strict", "engineer", "enforced"])
def test_strict_behavior_unchanged_under_each_alias(tmp_path, monkeypatch, capsys, mode):
    target = _wired(tmp_path); _set_mode(target, mode, capsys); _quiet_session_start(monkeypatch)
    runtime.handle_payload("SessionStart", _payload(target, "SessionStart"))
    denied = runtime.handle_payload("PreToolUse", _bash(target, "PreToolUse", "python -m pytest -q"))
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    runtime.handle_payload("PostToolUse", _write(target))
    assert runtime.handle_payload("Stop", _payload(target, "Stop", stop_hook_active=False))["decision"] == "block"

@pytest.mark.parametrize("mode", ["strict", "observe", "off"])
def test_every_mode_leaves_rails_record_untouched(tmp_path, capsys, monkeypatch, mode):
    record = _isolated_rails_record(tmp_path, monkeypatch)   # points rails_record_path at tmp
    target = _wired(tmp_path); _set_mode(target, mode, capsys)
    assert not record.exists() and work_mode.rails_enabled(target) is True

@pytest.mark.parametrize("mode", ["observe", "off"])
def test_unenforced_modes_classify_by_name_and_never_dormant(tmp_path, capsys, mode):
    target = _wired(tmp_path); _set_mode(target, mode, capsys); _unreceipted_recent_write(target)
    payload = classify.classify(target)
    assert payload["state"] == mode and "recent work has no verification receipt" not in payload["issues"]

def test_outcome_loop_findings_run_only_in_strict(tmp_path, capsys, monkeypatch):
    calls = []
    monkeypatch.setattr("brigade.outcome_cmd.health", lambda target: calls.append(target) or {"issues": []})
    for mode in ("observe", "off", "strict"):
        target = _wired(tmp_path, name=mode); _set_mode(target, mode, capsys)
        doctor._check_outcome_loop(_doctor_context(target))
    assert [path.name for path in calls] == ["strict"]

def test_hook_status_names_unenforced_mode(tmp_path, capsys):
    target = _wired(tmp_path); _set_mode(target, "observe", capsys)
    assert install_cmd.status_payload(target)["work_mode"] == "observe"
```

Rename `test_adoption_reports_yolo_and_never_bypassed` to `test_adoption_reports_unenforced_modes_and_never_bypassed`, parametrize it over `observe` and `off`, and assert `row["state"] == mode` and every session `status == mode`.

In `tests/test_claude_hooks_verification_mode.py` rename the yolo cases to the new names and add:

```python
def test_observe_repo_cd_into_strict_repo_is_denied(repos):
    # observe workspace, strict child: the check in the child is enforced
    assert verification_mode.verification_enforced("cd child && pytest", repos.workspace, repos.workspace)

def test_strict_workspace_cd_into_observe_repo_is_allowed(repos):
    assert not verification_mode.verification_enforced("cd observed && pytest", repos.strict_root, repos.strict_root)

def test_governing_modes_reports_each_verifier_target(repos):
    modes = verification_mode.governing_modes("pytest && cd ./observed && pytest", repos.strict_root, repos.strict_root)
    assert modes == [(repos.strict_root, "strict")]
    assert verification_mode.governing_modes(
        "cd ./observed && pytest", repos.strict_root, repos.strict_root
    ) == [(repos.observed, "observe")]
    assert verification_mode.governing_modes("echo done", repos.strict_root, repos.strict_root) == []
```

Adjust the `repos` fixture to build one workspace and children in each mode. Keep every existing case's expectation, with `yolo` read as `off`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `brigade work verify run --target . --argv-json '["./scripts/verify-focused","tests/test_work_mode.py","tests/test_claude_hooks_verification_mode.py","tests/test_repos_adoption_cmd.py","tests/test_claude_hooks_doctor.py","tests/test_claude_hooks_runtime.py"]' --capture brigade-work`
Expected: FAIL (`AttributeError: module 'brigade.work_mode' has no attribute 'is_yolo'` and the new assertions).

- [ ] **Step 3: Implement `governing_modes` and `verification_enforced`**

Build both on the existing `verifier_cwds` walk and `resolve_wired_target`. Delete `verification_in_yolo`.

- [ ] **Step 4: Update every caller in the table with the same line count where noted**

`runtime.py` 2373: `or not verification_mode.verification_enforced(`. 2490: `and verification_mode.verification_enforced(command, payload.get("cwd"), target)`. 2526: `or not work_mode.is_strict(stop_target)`. `classify.py`: `mode = work_mode.resolve_mode(target)`, `dormant = recent_unreceipted and mode == work_mode.MODE_STRICT`, the `elif yolo` branch becomes `elif mode != work_mode.MODE_STRICT: state = mode`. `install_cmd.py`: set `result["work_mode"]` to the resolved mode when the project-scope mode is not strict. `doctor.py`: import `is_strict`, return `[]` when `not is_strict(ctx.target)`. `clean_yolo` becomes `clean_unenforced = state in {"off", "observe"} and not payload.get("issues")`. `adoption.py`: as in Interfaces, branch on `not work_mode.is_strict(target)` and `classified_state in {"off", "observe"}`.

- [ ] **Step 5: Run the tests to verify they pass, and check the line counts**

Run the Step 2 command. Expected: PASS. Then `wc -l src/brigade/claude_hooks/runtime.py src/brigade/doctor.py` prints 3335 and 1982 or less.

- [ ] **Step 6: Commit**

```bash
git add src/brigade/claude_hooks src/brigade/doctor.py src/brigade/repos_cmd/adoption.py tests
git commit -m "feat(work): handle observe in hooks, doctor and adoption"
```

### Task 3: CLI, brief, install hint, Grok hook and docs

**Files:**
- Modify: `src/brigade/cli/init.py:77-98`, `src/brigade/cli/work/registration.py:112-122`, `src/brigade/work_mode_cmd.py`
- Modify: `src/brigade/brief_sections.py:49-58,94-96,192-201`, `src/brigade/install.py:527-545`
- Modify: `src/brigade/templates/grok/hooks/brigade-work.sh:127-139,221,249`
- Modify: `docs/technical-guide.md`, `docs/agent-assisted-setup.md`
- Create: `changelog.d/stage3-work-modes.md`
- Test: `tests/test_work_mode.py`, `tests/test_grok_hook_runtime.py`, `tests/test_install.py`, `tests/test_completions.py`

**Interfaces:**
- Consumes: Task 1 constants.
- Produces: `brief_sections.session_close_items(*, mode: str = "strict") -> tuple[str, ...]` and `render_brief_session_close_lines(*, mode: str = "strict") -> list[str]`. `_YOLO_SESSION_CLOSE_ITEMS` renamed `_UNENFORCED_SESSION_CLOSE_ITEMS`, used for `off` and `observe`. `init` flags `--strict` (alias `--engineer`), `--observe`, `--off` (alias `--yolo`) in the existing mutually exclusive group.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize(("given", "stored"), [("strict", "strict"), ("observe", "observe"), ("off", "off"),
                                               ("engineer", "strict"), ("yolo", "off")])
def test_mode_set_accepts_canonical_and_alias_names(tmp_path, capsys, given, stored):
    target = _wired(tmp_path)
    assert cli.main(["work", "mode", "set", given, "--target", str(target), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == stored

def test_mode_set_rejects_unknown_mode_listing_canonical_names(tmp_path, capsys):
    target = _wired(tmp_path)
    assert cli.main(["work", "mode", "set", "chaos", "--target", str(target)]) == 2
    err = capsys.readouterr().err
    assert "strict, observe, off" in err and "engineer" not in err and "yolo" not in err

@pytest.mark.parametrize(("flag", "stored"), [("--observe", "observe"), ("--off", "off"), ("--yolo", "off"),
                                              ("--strict", "strict"), ("--engineer", "strict")])
def test_init_mode_flags_write_canonical_mode(tmp_path, capsys, flag, stored): ...
    # asserts config work.mode == stored after `brigade init ... <flag>`

def test_observe_brief_states_checks_are_recorded(tmp_path, capsys):
    target = _wired(tmp_path); _set_mode(target, "observe", capsys)
    assert cli.main(["work", "brief", "--target", str(target)]) == 0
    assert "work_mode: observe (checks are recorded, not enforced" in capsys.readouterr().out

def test_init_observe_prints_observe_agent_workflow(tmp_path, capsys): ...
    # init --observe output contains "Agent: observe work mode" and "brigade work mode set strict"
```

In `tests/test_grok_hook_runtime.py` replace `test_yolo_mode_allows_raw_verification_and_engineer_still_denies` with `test_unenforced_modes_allow_raw_verification_and_strict_denies`, parametrized over config values `off`, `yolo`, `observe` (allowed) and `strict`, `engineer`, absent key (denied), using the file's existing hook runner. Keep `test_yolo_ignored_for_unsupported_config_version` and assert the same for `off`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `brigade work verify run --target . --argv-json '["./scripts/verify-focused","tests/test_work_mode.py","tests/test_grok_hook_runtime.py","tests/test_install.py","tests/test_completions.py","tests/test_cli_inventory_contract.py"]' --capture brigade-work`
Expected: FAIL.

- [ ] **Step 3: Implement the CLI, brief and hint text**

`mode set` metavar `{strict,observe,off}`. Help names the aliases. The brief line for `off` keeps today's yolo sentence with the new name. For `observe` it reads `work_mode: observe (checks are recorded, not enforced; `brigade work verify run` still records a receipt)`. The `install.py` hint prints an `Agent: observe work mode` block for observe (brief, run checks directly and they are recorded, optional handoff) and keeps today's two blocks for off and strict with the new names. `work_mode_cmd` messages use the canonical names.

- [ ] **Step 4: Update the Grok hook**

The Python snippet prints `off` for `off` or `yolo`, `observe` for `observe`, `strict` otherwise (unsupported version still `strict`). Line 221 becomes `elif [[ "$work_mode" != "strict" ]]`. Line 249 becomes `[[ "$work_mode" == "strict" ]]`.

- [ ] **Step 5: Update the two docs and write the fragment**

Document the three modes, the aliases, the explicit `work.mode` key, the brief-time pin and the older-version fail-safe. Fragment bullets: the three modes and aliases. `work.mode` is now always written. The default is unchanged.

- [ ] **Step 6: Run the tests to verify they pass**

Run the Step 2 command plus `python3 scripts/changelog_fragments.py check`. Expected: PASS. If the command inventory or completion snapshot drifts, regenerate with `BRIGADE_EXTRAS=1 brigade roadmap commands --write` and rerun.

- [ ] **Step 7: Commit**

```bash
git add src/brigade docs changelog.d tests
git commit -m "feat(work): expose strict, observe and off in init, mode set, brief and Grok hook"
```

---

## Pull request 2: `feat(work): observed-check record and writer`

No user-visible effect on its own (library only), so no changelog fragment.

### Task 4: Agent identity type and harness detection

**Files:**
- Create: `src/brigade/agent_identity.py`
- Modify: `src/brigade/outcome_cmd.py:400-450` (`_HARNESS_SIGNALS`, `_detect_harness` move out. Import from `agent_identity`)
- Test: `tests/test_agent_identity.py`

**Interfaces:**
- Produces: `UNKNOWN = "unknown"`. `MODEL_SOURCES = ("hook-session-start", "hook-model-switch", "hook-event", "env", "unknown")`. `@dataclass(frozen=True) class AgentIdentity: harness: str; harness_source: str; model: str; model_source: str; agent_type: str = UNKNOWN` with `as_dict(self) -> dict[str, str]`. `detect_harness(environ: Mapping[str, str] | None = None) -> tuple[str, str]` (moved body, same results). `env_model(environ: Mapping[str, str] | None = None) -> tuple[str, str]` (`BRIGADE_CONTEXT_MODEL` as `("<model>", "env")`, else `(UNKNOWN, UNKNOWN)`). `session_fingerprint(harness: str, session_id: str) -> str` remains the compatibility helper for existing receipt identity. Observed `build_check` HMACs its session identifier argument under the workspace key with the distinct `observed-session` domain. The fixed unavailable marker is used on key failure. Do not store raw session identifiers.

- [ ] **Step 1: Write the failing tests**

```python
def test_detect_harness_matches_previous_outcome_detection(monkeypatch):
    for var in ("BRIGADE_CONTEXT_HARNESS", "CLAUDECODE", "CURSOR_TRACE_ID", "CODEX_SANDBOX", "AI_AGENT"):
        monkeypatch.delenv(var, raising=False)
    assert agent_identity.detect_harness() == ("unknown", "unknown")
    monkeypatch.setenv("CLAUDECODE", "1")
    assert agent_identity.detect_harness() == ("claude-code", "auto")
    monkeypatch.setenv("BRIGADE_CONTEXT_HARNESS", "My Harness")
    assert agent_identity.detect_harness() == ("my-harness", "env")
    assert outcome_cmd.context_manifest()["harness"] == "my-harness"

def test_session_fingerprint_matches_claude_runtime():
    assert agent_identity.session_fingerprint("claude-code", "abc") == runtime._session_fingerprint("abc")
    assert agent_identity.session_fingerprint("codex", "abc") != runtime._session_fingerprint("abc")

def test_identity_as_dict_keys():
    identity = agent_identity.AgentIdentity("codex", "hook-event", "model-x", "hook-event")
    assert identity.as_dict() == {"harness": "codex", "harness_source": "hook-event", "model": "model-x",
                                  "model_source": "hook-event", "agent_type": "unknown"}
```

- [ ] **Step 2: Run to verify failure**

Run: `brigade work verify run --target . --argv-json '["./scripts/verify-focused","tests/test_agent_identity.py","tests/test_outcome_cmd.py::test_context_manifest_computes_trustworthy_fields","tests/test_outcome_cmd.py::test_context_manifest_detects_harness_from_env_signal"]' --capture brigade-work`
Expected: FAIL (`ModuleNotFoundError: brigade.agent_identity`).

- [ ] **Step 3: Implement `agent_identity.py` and the move**

- [ ] **Step 4: Run to verify pass.** Expected: PASS. `outcome_cmd.py` is shorter than before.

- [ ] **Step 5: Commit**

```bash
git add src/brigade/agent_identity.py src/brigade/outcome_cmd.py tests/test_agent_identity.py
git commit -m "refactor(outcome): move harness detection into agent_identity"
```

### Task 5: Observed-check record, writer and reader

**Files:**
- Create: `src/brigade/observed_checks.py`
- Test: `tests/test_observed_checks.py`

**Interfaces:**
- Consumes: `AgentIdentity`, `session_fingerprint` (Task 4). `claude_hooks.runtime._first_verifier_command`, `_shell_tokens`, `_strip_env`, `_python_module_invocation`, `_package_runner_script`, `_strip_runner_global_options`, `_strip_runner_run_options`, `_strip_npx_options` through a lazy `_rt()` accessor like `claude_hooks/write_attribution.py` uses. `guard.redact_text`. `claude_hooks.envelope.redact_paths`. `localio.tree_fingerprint`, `localio.utc_now_iso`.
- Produces:
  - `digest_key_path(target)` resolves the private per-user workspace directory. `CHECKS_FILE = "checks.jsonl"`, `CHECK_SCHEMA = "brigade.observed_check.v1"`, `STATUSES = ("ok", "failed", "unknown")`, `ROTATE_BYTES = 4 * 1024 * 1024`, `COMMAND_MAX_CHARS = 1000`, `CHECK_KINDS = ("tests", "lint", "types", "build", "checks")`.
  - `@dataclass(frozen=True) class ObservedCheck: schema: str; ts: str; harness: str; model: str; model_source: str; agent_type: str; session_fingerprint: str; tree_fingerprint: str | None; tool: str; command_digest: str; status: str; exit_code: int | None; duration_ms: int | None` with `to_dict()` and `from_dict(cls, data: Mapping[str, Any]) -> ObservedCheck | None` (None for any missing, extra or mistyped field, including a legacy `command` key).
  - `normalize_tool(command: str) -> str`, `check_kind(tool: str) -> str`, `command_digest(command: str, target: Path) -> str`. Redaction is an internal step before hashing.
  - `build_check(*, target: Path, command: str, status: str, identity: AgentIdentity, session_fingerprint: str, exit_code: int | None = None, duration_ms: int | None = None) -> ObservedCheck`.
  - `append_jsonl(path: Path, payload: Mapping[str, Any]) -> bool` (rotation, 0600, one `os.write` per line with `O_APPEND`, False on `OSError`, never raises). `read_jsonl(path: Path) -> tuple[list[dict[str, Any]], int]` (reads `<path>.1` then `<path>`, returns records and skipped-line count).
  - `append_check(target: Path, check: ObservedCheck) -> bool`. `read_checks(target: Path) -> tuple[list[ObservedCheck], int]`.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize(("command", "tool"), [
    ("python -m pytest -q tests/x.py", "pytest"), ("uv run pytest", "pytest"), ("npx vitest run", "vitest"),
    ("npm test", "npm:test"), ("pnpm run check:types", "pnpm:check:types"), ("go test ./...", "go test"),
    ("cargo clippy", "cargo clippy"), ("make check", "make check"), ("ruff check .", "ruff check"),
    ("ruff format --check .", "ruff format"), ("mypy", "mypy"),
    ("./scripts/verify-focused tests/x.py", "scripts/verify-focused"), ("cd sub && FOO=1 pytest", "pytest"),
    ("echo hi", "other"),
])
def test_normalize_tool(command, tool):
    assert observed_checks.normalize_tool(command) == tool

@pytest.mark.parametrize(("tool", "kind"), [("pytest", "tests"), ("npm:test", "tests"), ("ruff check", "lint"),
    ("eslint", "lint"), ("mypy", "types"), ("tsc", "types"), ("make check", "checks"),
    ("scripts/verify", "checks"), ("other", "checks")])
def test_check_kind(tool, kind):
    assert observed_checks.check_kind(tool) == kind

def test_command_is_redacted_and_bounded(tmp_path, monkeypatch):
    set_home(monkeypatch, tmp_path / "home")
    # content-guard: allow bare-token-prefix
    command = f'curl -H "Authorization: Bearer ghp_{"A" * 36}" {tmp_path / "home"}/x && pytest ' + "x" * 2000
    check = observed_checks.build_check(target=tmp_path, command=command, status="ok",
        identity=identity, session_fingerprint="a" * 16)
    assert "command" not in check.to_dict()
    assert len(check.command_digest) == 16

def test_append_and_read_round_trip(tmp_path): ...       # build_check, append_check, read_checks == ([check], 0)
def test_rotation_keeps_one_generation(tmp_path, monkeypatch): ...
    # ROTATE_BYTES = 200; append 10 checks; files checks.jsonl and checks.jsonl.1 exist, no .2; read_checks returns the newest
def test_read_skips_corrupt_lines_and_counts_them(tmp_path): ...
    # write one valid line, '{"half": ', and a valid line; read_checks returns 2 records and skipped == 1
def test_append_failure_returns_false_without_raising(tmp_path): ...
    # make .brigade/work a file; append_check returns False
def test_observed_file_is_not_a_receipt(tmp_path): ...
    # after append: no .brigade/work/verify-runs dir; record has no "digests", "signature" or "previous" keys
def test_observed_file_passes_guard_scan(tmp_path):
    # append a check whose command held the fake token above
    text = observed_checks.digest_key_path(target).with_name("checks.jsonl").read_text()
    assert guard.scan_text(text).findings == []
    assert not (target / ".brigade").exists()
```

- [ ] **Step 2: Run to verify failure**

Run: `brigade work verify run --target . --argv-json '["./scripts/verify-focused","tests/test_observed_checks.py"]' --capture brigade-work`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement `observed_checks.py`**

`normalize_tool` takes the first verifier segment using the existing classifier and strips environment and shell control prefixes. Wrappers recurse on their tail. Every result is checked against a closed vocabulary of known tools and known targets. Targets must also match `[A-Za-z0-9._:/-]{1,32}`. Never retain an arbitrary basename, target, script name, subcommand, option or positional argument. Unknown targets fall back to the bare known tool, other commands to `other`. `check_kind` maps known tools to tests, lint, types, build or checks. Build the digest only after the internal redaction pass. Validate its 16 lowercase hex characters on read and append. Reject extra keys, including legacy `command`. Map harness, model and agent type through the shared closed-value mapping at build, append and read. Unknown identity values become `unknown`. HMAC the session identifier argument under the workspace key with the distinct `observed-session` domain, using the fixed marker when unavailable. Non-null tree fingerprints must be 40 lowercase hex characters. Serialize size check, rotation and append under the shared OS file lock in the private per-user workspace directory. Appending never parses either generation. Close the active descriptor before replacing the previous generation. Readers decode active first, then old, newest first within each, skipping bad lines and returning at most 10,000 records. Keep the short-write newline guard. Bound each file read to 8 MiB, lines to 16 KiB and string fields to 4,096 characters. Windows uses the shared handle-relative backend and inherited ACLs. See decision 7 for state-directory and key trust checks. Nothing is created inside the workspace. Ignore older workspace records without migration.

- [ ] **Step 4: Run to verify pass.** Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/brigade/observed_checks.py tests/test_observed_checks.py
git commit -m "feat(work): add the observed-check record, writer and reader"
```

---

## Pull request 3: `feat(hooks): record observed checks and model identity from Claude Code`

Changelog fragment: `changelog.d/stage3-claude-observe.md`.

### Task 6: Claude Code capture and identity cache

**Files:**
- Create: `src/brigade/claude_hooks/check_events.py`
- Modify: `src/brigade/claude_hooks/runtime.py` (import, one `on_event` call before the `SessionEnd` branch, two `restore_identity` calls in `_normalize_state`, PostToolUseFailure branch reduced to a `failure_followup` call)
- Modify: `src/brigade/claude_hooks/package.py:14-23` (`MANAGED_EVENTS` gains `PostModelSwitch`. Bump `PACKAGE_VERSION` to `1.4.0`)
- Modify: `tests/test_module_size_ratchet.py` (lower the `runtime.py` cap to its new line count)
- Modify: `docs/claude-hook-session-state.md` (fields `model`, `model_source`)
- Test: `tests/test_claude_hooks_check_events.py`, existing `tests/test_claude_hooks_runtime.py`, `tests/test_work_mode.py`, `tests/test_claude_hooks_settings_merge.py`

**Interfaces:**
- Consumes: `verification_mode.governing_modes` (Task 2). `AgentIdentity`, `env_model`, `session_fingerprint` (Task 4). `build_check`, `append_check` (Task 5). `runtime.is_raw_verification`, `runtime._additional_context`, `runtime._parse_capture_flag`, `runtime._is_routed_verify`, `session_state._write_session_state_preserving_latch` through the lazy accessor.
- Produces: `check_events.IDENTITY_FIELDS = ("model", "model_source")`. `check_events.AGENT_SESSION_ENV = "BRIGADE_AGENT_SESSION"`. `on_event(event: str, payload: dict[str, Any], target: Path, session_id: str, state: dict[str, Any]) -> None` (never raises. Logs failures through `envelope` to the hook log). `restore_identity(normalized: dict[str, Any], persisted: Mapping[str, Any]) -> None`. `claude_identity(payload: Mapping[str, Any], state: Mapping[str, Any]) -> AgentIdentity`. `claude_status(event: str, payload: Mapping[str, Any]) -> tuple[str, int | None]`. `failure_followup(payload: dict[str, Any], target: Path, session_id: str, state: dict[str, Any]) -> dict[str, Any] | None` (today's PostToolUseFailure branch body, moved without behavior change).

- [ ] **Step 1: Write the failing tests** in `tests/test_claude_hooks_check_events.py` (reuse the `_wired`, `_payload`, `_bash`, `_set_mode` helpers by importing them from `tests/test_work_mode.py` or a shared helper module)

```python
def test_session_start_caches_model_in_session_state(tmp_path, monkeypatch, capsys):
    target = _observe_target(tmp_path, monkeypatch, capsys)
    runtime.handle_payload("SessionStart", _payload(target, "SessionStart", source="startup", model="test-model-a"))
    state = runtime.read_session_state(target, "s1")
    assert (state["model"], state["model_source"]) == ("test-model-a", "hook-session-start")
    runtime.handle_payload("PostToolUse", _write(target))          # a later full-state write keeps it
    assert runtime.read_session_state(target, "s1")["model"] == "test-model-a"

def test_model_switch_updates_cached_model(tmp_path, monkeypatch, capsys): ...
    # PostModelSwitch with to_model="test-model-b" -> state model "test-model-b", source "hook-model-switch"

def test_observe_records_passing_and_failing_checks(tmp_path, monkeypatch, capsys):
    target = _observe_target(tmp_path, monkeypatch, capsys)
    runtime.handle_payload("SessionStart", _payload(target, "SessionStart", model="test-model-a"))
    runtime.handle_payload("PostToolUse", _bash(target, "PostToolUse", "python -m pytest -q", duration_ms=1200))
    runtime.handle_payload("PostToolUseFailure", _bash(target, "PostToolUseFailure", "ruff check .",
                           error="Exit code 1\nfound 2 errors", agent_id="a1", agent_type="Explore"))
    # content-guard: allow bare-token-prefix
    runtime.handle_payload("PostToolUse", _bash(target, "PostToolUse", f'GH_TOKEN=ghp_{"A" * 36} pytest'))
    checks, skipped = observed_checks.read_checks(target)
    assert skipped == 0 and [c.tool for c in checks] == ["pytest", "ruff check", "pytest"]
    assert (checks[0].status, checks[0].duration_ms, checks[0].agent_type) == ("ok", 1200, "main")
    assert (checks[1].status, checks[1].exit_code, checks[1].agent_type) == ("failed", 1, "Explore")
    assert {c.model for c in checks} == {"test-model-a"} and {c.harness for c in checks} == {"claude-code"}
    assert "command" not in checks[2].to_dict()
    assert len(checks[2].command_digest) == 16

@pytest.mark.parametrize("mode", ["strict", "off"])
def test_strict_and_off_record_nothing(tmp_path, monkeypatch, capsys, mode): ...
@pytest.mark.parametrize("command", ["ls -la", "brigade work verify run --target . --command pytest"])
def test_non_check_commands_are_not_recorded(tmp_path, monkeypatch, capsys, command): ...
def test_interrupted_or_timed_out_check_records_unknown(tmp_path, monkeypatch, capsys): ...
    # is_interrupt=True -> unknown; error="Command timed out" -> unknown, exit_code None
def test_capture_failure_never_changes_hook_result(tmp_path, monkeypatch, capsys):
    target = _observe_target(tmp_path, monkeypatch, capsys)
    monkeypatch.setattr(observed_checks, "append_check", _raise(OSError("disk full")))
    assert runtime.handle_payload("PostToolUse", _bash(target, "PostToolUse", "pytest")) is None
    assert runtime.handle_payload("PreToolUse", _bash(target, "PreToolUse", "pytest")) is None
def test_check_in_observe_child_records_only_in_child(tmp_path, monkeypatch, capsys): ...
    # strict workspace, observe child: PreToolUse "cd child && pytest" not denied; PostToolUse records in child only
def test_session_start_exports_agent_session_to_env_file(tmp_path, monkeypatch, capsys):
    env_file = tmp_path / "claude-env.sh"; monkeypatch.setenv("CLAUDE_ENV_FILE", str(env_file))
    target = _observe_target(tmp_path, monkeypatch, capsys)
    runtime.handle_payload("SessionStart", _payload(target, "SessionStart"))
    text = env_file.read_text()
    assert f"export BRIGADE_AGENT_SESSION={runtime._session_fingerprint('s1')}" in text
    assert "BRIGADE_CLAUDE_SESSION" not in text
def test_install_writes_post_model_switch_handler(tmp_path): ...
    # after hooks_install, settings.json hooks contain "PostModelSwitch" with the managed command
```

The strict retry-note behavior is already pinned by the existing PostToolUseFailure tests in `tests/test_claude_hooks_runtime.py` and `tests/test_work_mode.py`. They must pass unchanged after the move.

- [ ] **Step 2: Run to verify failure**

Run: `brigade work verify run --target . --argv-json '["./scripts/verify-focused","tests/test_claude_hooks_check_events.py","tests/test_claude_hooks_runtime.py","tests/test_work_mode.py","tests/test_claude_hooks_settings_merge.py","tests/test_module_size_ratchet.py"]' --capture brigade-work`
Expected: FAIL (`ModuleNotFoundError: brigade.claude_hooks.check_events`).

- [ ] **Step 3: Implement `check_events.py`**

`on_event` dispatches on `event`. `SessionStart`: cache a string `model` with source `hook-session-start`, persist with `patch_fields=IDENTITY_FIELDS`, then append the export line to `CLAUDE_ENV_FILE` when that variable names an absolute path (quote with `shlex.quote`. Ignore `OSError`). `PostModelSwitch`: cache `to_model` with `hook-model-switch`. `PostToolUse` and `PostToolUseFailure` with `tool_name == "Bash"` and `is_raw_verification(command)`: for each `(target, mode)` from `governing_modes(command, payload.get("cwd"), handled_target)` with mode `observe`, append `build_check(...)`. `claude_identity`: harness `claude-code` with source `hook-event`. Model from the state cache, else `env_model()`. `agent_type` from a non-empty payload `agent_type`, else `"subagent"` when `agent_id` is present, else `"main"`. `claude_status` follows decision 8. `duration_ms` is taken only when it is a non-negative int. Every branch is wrapped so an exception is logged and swallowed.

- [ ] **Step 4: Wire `runtime.py` and lower its cap**

Add `check_events` to the `from . import (...)` block. Call `check_events.on_event(event, payload, target, session_id, state)` once, after `_touch_session_targets` and before `if event == "SessionEnd":`. In `_normalize_state`, call `check_events.restore_identity(normalized, payload)` in both the epoch-reset return path and the main path, so a cached model survives full-state writes and `/clear`. Replace the PostToolUseFailure branch body with `return check_events.failure_followup(payload, target, session_id, state)`. Add `PostModelSwitch` to `MANAGED_EVENTS` and bump `PACKAGE_VERSION`. Then set `LEGACY_OVERSIZED["src/brigade/claude_hooks/runtime.py"]` to the new `wc -l` count, which must be below 3,335.

- [ ] **Step 5: Run to verify pass.** Run the Step 2 command. Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/brigade/claude_hooks tests docs/claude-hook-session-state.md
git commit -m "feat(hooks): record observed checks and cache the model from Claude Code"
```

### Task 7: Verify receipts and outcome records carry agent identity

**Files:**
- Modify: `src/brigade/agent_identity.py` (add `for_verify_run`, `claude_session_model`)
- Modify: `src/brigade/work_cmd/verification.py:1500-1504,1579,1735` (`_stamp_harness_session(receipt, target)`)
- Modify: `src/brigade/outcome_cmd.py` verify branch of capture (around line 2045-2059)
- Modify: `docs/technical-guide.md` (receipt field `agent_identity`, outcome `context.agent_identity`)
- Create: `changelog.d/stage3-claude-observe.md`
- Test: `tests/test_verify_agent_identity.py`

**Interfaces:**
- Consumes: Task 4 and Task 6 (`AGENT_SESSION_ENV`, the cached session fields).
- Produces: `agent_identity.claude_session_model(target: Path, fingerprint: str) -> tuple[str, str] | None` (scans `claude_hooks.runtime.iter_session_states(target)` for a matching `session_fingerprint` and returns its cached `(model, model_source)`). `agent_identity.for_verify_run(target: Path, environ: Mapping[str, str] | None = None) -> AgentIdentity` (harness from `detect_harness`. Model from `env_model`, else from `claude_session_model` using a 16-hex `BRIGADE_AGENT_SESSION` or `BRIGADE_CLAUDE_SESSION`, else unknown. `agent_type` always `unknown`). Receipt key `agent_identity` (the `as_dict()` shape), stamped before the digest is computed. Outcome record `context["agent_identity"]`, copied from the receipt.

- [ ] **Step 1: Write the failing tests**

```python
def test_verify_receipt_carries_agent_identity_from_env(tmp_path, monkeypatch, capsys):
    target = _wired(tmp_path); _clear_identity_env(monkeypatch)
    monkeypatch.setenv("CLAUDECODE", "1"); monkeypatch.setenv("BRIGADE_CONTEXT_MODEL", "test-model-a")
    receipt = _verify(target, "true")              # cli.main(["work","verify","run",...]) then read latest receipt
    assert receipt["agent_identity"] == {"harness": "claude-code", "harness_source": "auto",
        "model": "test-model-a", "model_source": "env", "agent_type": "unknown"}

def test_verify_receipt_model_from_claude_session_state(tmp_path, monkeypatch, capsys): ...
    # SessionStart with model "test-model-b"; env BRIGADE_AGENT_SESSION=<fp>; receipt model "test-model-b",
    # model_source "hook-session-start"
def test_verify_receipt_identity_unknown_without_signals(tmp_path, monkeypatch, capsys): ...
def test_harness_session_stamp_unchanged_by_agent_session_env(tmp_path, monkeypatch, capsys):
    # BRIGADE_AGENT_SESSION set, BRIGADE_CLAUDE_SESSION unset -> "harness_session" not in receipt
def test_receipt_digest_covers_agent_identity(tmp_path, monkeypatch, capsys):
    receipt = _verify(target, "true")
    assert receipt["digests"]["receipt_sha256"] == localio.canonical_json_digest(receipt, exclude_keys={"digests"})
def test_outcome_capture_copies_identity_without_changing_capability(tmp_path, monkeypatch, capsys):
    # verify run --capture brigade-work with BRIGADE_CONTEXT_MODEL unset but a session model available
    record = _latest_outcome_record(target)
    assert record.context["agent_identity"]["model"] == "test-model-b"
    plain = {k: v for k, v in record.context.items() if k != "agent_identity"}
    assert record.capability_fingerprint == outcome_cmd.capability_fingerprint(plain)
    assert record.context["model"] == "unknown"
```

- [ ] **Step 2: Run to verify failure**

Run: `brigade work verify run --target . --argv-json '["./scripts/verify-focused","tests/test_verify_agent_identity.py","tests/test_agent_identity.py","tests/test_module_size_ratchet.py"]' --capture brigade-work`
Expected: FAIL (`KeyError: 'agent_identity'`).

- [ ] **Step 3: Implement**

`_stamp_harness_session(receipt, target)` keeps its current body and adds `receipt["agent_identity"] = agent_identity.for_verify_run(target).as_dict()`. Both call sites pass `target`. In `outcome_cmd` capture's verify branch, keep `receipt.get("agent_identity")` when it is a dict and set `manifest["agent_identity"]` to it after `context_manifest(run_agent)` and before building the record. `verification.py` must stay at or below its 2,380 cap.

- [ ] **Step 4: Run to verify pass**, plus the existing receipt and outcome suites: add `tests/test_receipts_cmd.py` and `tests/test_outcome_cmd.py` to the Step 2 selectors. Expected: PASS. Then `python3 scripts/changelog_fragments.py check`.

- [ ] **Step 5: Commit**

```bash
git add src/brigade/agent_identity.py src/brigade/work_cmd/verification.py src/brigade/outcome_cmd.py docs changelog.d tests
git commit -m "feat(work): stamp harness and model on verify receipts and outcome records"
```

---

## Pull request 4: `feat(hooks): observe checks from Codex and Cursor hooks`

Changelog fragment: `changelog.d/stage3-codex-cursor-observe.md`.

### Task 8: Cursor capture

**Files:**
- Create: `src/brigade/observe_harness_hooks.py`
- Modify: `src/brigade/fleet_session_presence.py:60-64,608-627` (call capture for `postToolUse` and `postToolUseFailure`)
- Modify: `src/brigade/cursor_user_cmd.py:19`, `src/brigade/cursor_profile_hooks.py:12` (add `postToolUseFailure`)
- Test: `tests/test_observe_harness_hooks.py`, `tests/test_cursor_user_cmd.py`, `tests/test_fleet_session_presence.py`

**Interfaces:**
- Consumes: Tasks 2, 4, 5.
- Produces: `observe_harness_hooks.cursor_identity(payload: Mapping[str, Any]) -> AgentIdentity` (harness `cursor`, source `hook-event`. Model `model_id` when a non-empty string, else `model`. `agent_type` `unknown`). `cursor_status(event: str, payload: Mapping[str, Any]) -> tuple[str, int | None] | None` (None means do not record). `capture_cursor_event(event: str, payload: Mapping[str, Any], target: Path) -> None` (never raises. Records only `tool_name == "Shell"`, `is_raw_verification(tool_input.command)`, governing mode `observe`. Session fingerprint from `conversation_id`. `duration` taken as milliseconds).

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize(("output", "status", "code"), [('{"exitCode":0,"stdout":"ok"}', "ok", 0),
    ('{"exitCode":2}', "failed", 2), ("not json", "unknown", None), ('{"stdout":"x"}', "unknown", None)])
def test_cursor_shell_post_tool_use_records_exit_code(tmp_path, monkeypatch, capsys, output, status, code):
    target = _observe_target(tmp_path, capsys, harnesses=("cursor",))
    _run_cursor_hook(monkeypatch, {"hook_event_name": "postToolUse", "conversation_id": "c1",
        "workspace_roots": [str(target)], "tool_name": "Shell", "tool_input": {"command": "npm test"},
        "tool_output": output, "duration": 5432, "model": "legacy-slug", "model_id": "test-model-c"})
    check = observed_checks.read_checks(target)[0][0]
    assert (check.status, check.exit_code, check.duration_ms) == (status, code, 5432)
    assert (check.harness, check.model, check.agent_type) == ("cursor", "test-model-c", "unknown")

@pytest.mark.parametrize(("failure_type", "recorded"), [("error", "failed"), ("timeout", "unknown"),
                                                        ("permission_denied", None)])
def test_cursor_failure_types(tmp_path, monkeypatch, capsys, failure_type, recorded): ...
def test_cursor_non_shell_tool_and_strict_target_record_nothing(tmp_path, monkeypatch, capsys): ...
def test_cursor_capture_failure_keeps_presence_output(tmp_path, monkeypatch, capsys):
    # append_check raises; stdout is still "{}" and run_presence_hook returns 0
def test_cursor_hooks_manage_post_tool_use_failure(tmp_path):
    assert "postToolUseFailure" in cursor_profile_hooks.HOOK_EVENTS
    assert cursor_user_cmd.install_project_presence_hooks(tmp_path) is None
    hooks = json.loads((tmp_path / ".cursor" / "hooks.json").read_text())["hooks"]
    assert "postToolUseFailure" in hooks
```

- [ ] **Step 2: Run to verify failure**

Run: `brigade work verify run --target . --argv-json '["./scripts/verify-focused","tests/test_observe_harness_hooks.py","tests/test_cursor_user_cmd.py","tests/test_fleet_session_presence.py"]' --capture brigade-work`
Expected: FAIL.

- [ ] **Step 3: Implement** `cursor_identity`, `cursor_status` (decision 8), `capture_cursor_event`. Call it from `_run_cursor_presence_hook` for the two events once `target` is resolved, before the output is printed, inside its own `try`.

- [ ] **Step 4: Run to verify pass.** Expected: PASS, including the existing Cursor profile and presence suites.

- [ ] **Step 5: Commit**

```bash
git add src/brigade/observe_harness_hooks.py src/brigade/fleet_session_presence.py src/brigade/cursor_user_cmd.py src/brigade/cursor_profile_hooks.py tests
git commit -m "feat(hooks): record observed checks from Cursor shell tool events"
```

### Task 9: Codex hook install and capture

**Files:**
- Modify: `src/brigade/observe_harness_hooks.py` (Codex functions)
- Modify: `src/brigade/fleet_session_presence.py:554-581` (route `harness == "codex"` with context `codex-observe`)
- Modify: `src/brigade/work_mode_cmd.py` (`mode set observe` installs Codex hooks for codex-selected targets. JSON output gains `observe_hooks`)
- Modify: `docs/agent-assisted-setup.md` (Codex trust step)
- Create: `changelog.d/stage3-codex-cursor-observe.md`
- Test: `tests/test_observe_harness_hooks.py`, `tests/test_work_mode.py`

**Interfaces:**
- Produces: `CODEX_HOOK_CONTEXT = "codex-observe"`. `CODEX_HOOK_COMMAND = "brigade work presence-hook --harness codex --stdin --context codex-observe"`. `CODEX_POST_TOOL_MATCHER = "^(Bash|apply_patch)$"`. `codex_identity(payload: Mapping[str, Any]) -> AgentIdentity` (harness `codex`, model from `model` with source `hook-event`, `agent_type` `unknown`). `run_codex_hook() -> None` (reads bounded stdin JSON, resolves the target with `resolve_wired_target(cwd, harness="codex")`, records a Bash check with status `unknown`, records `last_write_at` for `apply_patch` in `<private-workspace-state>/sessions/<session fingerprint>.json`, prints `{}`). `install_codex_observe_hooks(target: Path) -> str | None` (error text or None. Adds one async `PostToolUse` handler with the matcher above and one `Stop` handler to `<target>/.codex/hooks.json`, keeps foreign entries, is idempotent, refuses an unreadable or non-object file without writing). `codex_observe_hooks_installed(target: Path) -> bool`. `observe_session_path(target: Path, session_fingerprint: str) -> Path`.

- [ ] **Step 1: Write the failing tests**

```python
def test_codex_hook_records_bash_check_with_model(tmp_path, monkeypatch, capsys):
    target = _observe_target(tmp_path, capsys, harnesses=("codex",))
    _run_codex_hook(monkeypatch, {"hook_event_name": "PostToolUse", "session_id": "thr_1", "cwd": str(target),
        "model": "test-model-d", "tool_name": "Bash", "tool_input": {"command": "pytest -q"}, "tool_response": {}})
    check = observed_checks.read_checks(target)[0][0]
    assert (check.harness, check.model, check.model_source, check.status, check.agent_type) == \
           ("codex", "test-model-d", "hook-event", "unknown", "unknown")
    assert capsys.readouterr().out.strip() == "{}"

def test_codex_apply_patch_updates_last_write(tmp_path, monkeypatch, capsys): ...
def test_codex_install_writes_project_hooks_json(tmp_path):
    assert observe_harness_hooks.install_codex_observe_hooks(tmp_path) is None
    hooks = json.loads((tmp_path / ".codex" / "hooks.json").read_text())["hooks"]
    post = hooks["PostToolUse"][0]
    assert post["matcher"] == "^(Bash|apply_patch)$"
    assert post["hooks"][0] == {"type": "command", "command": observe_harness_hooks.CODEX_HOOK_COMMAND, "async": True}
    assert observe_harness_hooks.install_codex_observe_hooks(tmp_path) is None          # idempotent
    assert len(json.loads((tmp_path / ".codex" / "hooks.json").read_text())["hooks"]["PostToolUse"]) == 1
def test_codex_install_keeps_foreign_entries_and_refuses_invalid_json(tmp_path): ...
def test_mode_set_observe_installs_codex_hooks_only_for_codex_targets(tmp_path, capsys): ...
```

- [ ] **Step 2: Run to verify failure**

Run: `brigade work verify run --target . --argv-json '["./scripts/verify-focused","tests/test_observe_harness_hooks.py","tests/test_work_mode.py","tests/test_fleet_session_presence.py"]' --capture brigade-work`
Expected: FAIL.

- [ ] **Step 3: Implement** the Codex functions and the presence-hook route (the route always exits 0 and always prints `{}`, because Codex `Stop` requires JSON on stdout). `mode_command` calls `install_codex_observe_hooks` after a successful `set observe` when the config selects `codex`, prints a warning on error, and adds `"observe_hooks": {"codex": <installed bool>}` to the payload.

- [ ] **Step 4: Run to verify pass**, then `python3 scripts/changelog_fragments.py check`. Fragment bullets: Codex and Cursor checks are recorded in observe mode. Codex needs the project `.codex/` layer trusted. Codex status is always unknown and agent type is unknown for both.

- [ ] **Step 5: Commit**

```bash
git add src/brigade docs changelog.d tests
git commit -m "feat(hooks): record observed checks from Codex project hooks"
```

---

## Pull request 5: `feat(work): first-pass check report`

Changelog fragment: `changelog.d/stage3-checks-report.md`.

### Task 10: `brigade work checks`

**Files:**
- Create: `src/brigade/observed_report.py`, `src/brigade/work_checks_cmd.py`
- Modify: `src/brigade/cli/work/registration.py` (parser next to `mode`), `src/brigade/cli/work/dispatching.py:98` area (route), `src/brigade/brief_sections.py` (observe brief line names `brigade work checks`)
- Modify: `docs/command-inventory.md` (regenerated), `docs/technical-guide.md`
- Test: `tests/test_work_checks_report.py`, `tests/test_cli_inventory_contract.py`, `tests/test_completions.py`

**Interfaces:**
- Consumes: `read_checks` (Task 5). Receipt `agent_identity` and `harness_session` (Task 7).
- Produces: `observed_report.REPORT_LABEL = "observed, not independently verified"`. `RECEIPT_LABEL = "receipt: exit status recorded by brigade work verify run"`. `@dataclass(frozen=True) class FirstPassRow: source: str; harness: str; model: str; samples: int; passed: int; failed: int; unknown: int` with `rate(self) -> float | None` (passed over passed plus failed, None when both are zero). `@dataclass(frozen=True) class ReportNotes: observed_read: int; receipts_read: int; skipped_lines: int`. `first_pass_rows(target: Path, *, since: datetime | None = None) -> tuple[list[FirstPassRow], ReportNotes]`. `render_first_pass(rows: list[FirstPassRow], notes: ReportNotes) -> str`. `first_pass_json(rows, notes) -> dict[str, Any]`. `work_checks_cmd.checks_command(*, target: Path, since_days: int | None, claims: bool, json_output: bool) -> int`. CLI: `brigade work checks [--target PATH] [--since-days N] [--claims] [--json]`.

- [ ] **Step 1: Write the failing tests**

```python
def test_first_pass_counts_only_first_attempt_per_session_digest(tmp_path):
    _checks(tmp_path, [("A", "pytest", "failed"), ("A", "pytest", "ok"), ("B", "pytest", "ok")], model="m1")
    rows, notes = observed_report.first_pass_rows(tmp_path)
    assert [(r.source, r.model, r.samples, r.passed, r.failed) for r in rows] == [("observed", "m1", 2, 1, 1)]
    assert rows[0].rate() == 0.5 and notes.observed_read == 3

def test_unknown_status_excluded_from_rate_and_counted(tmp_path): ...
    # first attempts: ok, unknown -> samples 2, passed 1, unknown 1, rate 1.0
def test_unknown_identity_bucket_sorted_last(tmp_path): ...
    # rows for (claude-code, m1), (unknown, unknown): unknown row is last
def test_receipts_labeled_and_grouped(tmp_path): ...
    # receipt fixtures: completed with harness_session fp X and agent_identity model m2 -> source "receipt";
    # two receipts without a session on the same UTC day and redacted planned-command digest -> one sample;
    # rejected -> excluded; canceled -> unknown
def test_report_text_carries_labels_and_never_says_score(tmp_path, capsys):
    assert cli.main(["work", "checks", "--target", str(target)]) == 0
    out = capsys.readouterr().out
    assert observed_report.REPORT_LABEL in out and observed_report.RECEIPT_LABEL in out
    assert "score" not in out.lower()
def test_corrupt_lines_reported_not_fatal(tmp_path, capsys): ...
    # a half line in checks.jsonl -> exit 0, output contains "skipped 1 unreadable line"
def test_since_filter(tmp_path): ...
def test_json_output_shape(tmp_path, capsys): ...
    # keys: "label", "receipt_label", "rows", "observed_read", "receipts_read", "skipped_lines"
```

- [ ] **Step 2: Run to verify failure**

Run: `brigade work verify run --target . --argv-json '["./scripts/verify-focused","tests/test_work_checks_report.py","tests/test_cli_inventory_contract.py","tests/test_completions.py"]' --capture brigade-work`
Expected: FAIL.

- [ ] **Step 3: Implement** the grouping in decision 16. Observed samples use the record's harness and model. Receipt samples use `agent_identity` when present, else `harness_session.harness` mapped `claude` to `claude-code` with model `unknown`, else both `unknown`. For receipts, derive `command_digest` from the first entry of `planned_commands` using Task 5's workspace-keyed helper, and derive the tool using `normalize_tool`. Observed samples use their stored `command_digest` directly. An unavailable digest (`0000000000000000`) is a separate sample per record, never a shared grouping key. Never read or reconstruct command text from an observed record. Rows are sorted by source, then known identities by samples descending, unknown last. Text output prints the two label lines first, then a fixed-width table `source harness model n passed failed unknown rate`, then the skipped-line note when non-zero. `--claims` prints "claims report lands in a later release" and exits 2 until Task 12.

- [ ] **Step 4: Register the command, regenerate the inventory, update the observe brief line and docs**

`BRIGADE_EXTRAS=1 brigade roadmap commands --write`. The observe brief line becomes `work_mode: observe (checks are recorded, not enforced; `brigade work checks` shows them)`.

- [ ] **Step 5: Run to verify pass**, plus `tests/test_work_mode.py` for the brief line and `python3 scripts/changelog_fragments.py check`. Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/brigade docs changelog.d tests
git commit -m "feat(work): add the first-pass check report"
```

---

## Pull request 6: `feat(work): claimed versus run checks report`

Changelog fragment: `changelog.d/stage3-claims-report.md`.

### Task 11: Claim capture at Stop

**Files:**
- Create: `src/brigade/observe_claims.py`
- Modify: `src/brigade/claude_hooks/check_events.py` (`Stop` branch of `on_event`), `src/brigade/observe_harness_hooks.py` (Codex `Stop`)
- Test: `tests/test_observe_claims.py`

**Interfaces:**
- Consumes: Tasks 4, 5, 6, 9.
- Produces: `CLAIM_PHRASES_REL_PATH = Path(".brigade/claim-phrases.txt")`. `CLAIMS_FILE = "claims.jsonl"`. `CLAIM_SCHEMA = "brigade.observed_claim.v1"`. `DEFAULT_CLAIM_PHRASES: tuple[tuple[str, str], ...]` with exactly `("tests", "tests pass")`, `("tests", "tests passed")`, `("tests", "all tests pass")`, `("tests", "test suite passes")`, `("lint", "lint passes")`, `("lint", "lint is clean")`, `("types", "type check passes")`, `("types", "mypy passes")`, `("build", "build succeeds")`, `("checks", "all checks pass")`, `("checks", "verified")`. `load_claim_phrases(target: Path) -> tuple[tuple[str, str], ...]`. `claimed_kinds(message: str, phrases: Sequence[tuple[str, str]]) -> tuple[str, ...]` (sorted, unique. Lowercased, whitespace collapsed. Negation rule from decision 17). `@dataclass(frozen=True) class ObservedClaim: schema: str; ts: str; harness: str; model: str; model_source: str; agent_type: str; session_fingerprint: str; tree_fingerprint: str | None; last_write_at: str | None; claimed_kinds: tuple[str, ...]` with `to_dict` and `from_dict`. `append_claim(target: Path, claim: ObservedClaim) -> bool`. `read_claims(target: Path) -> tuple[list[ObservedClaim], int]`.

- [ ] **Step 1: Write the failing tests**

```python
def test_claimed_kinds_matches_configured_phrases():
    assert observe_claims.claimed_kinds("Done. All tests pass and   LINT is clean.",
                                        observe_claims.DEFAULT_CLAIM_PHRASES) == ("lint", "tests")

def test_negated_phrase_is_not_a_claim():
    assert observe_claims.claimed_kinds("Not all tests pass yet.", observe_claims.DEFAULT_CLAIM_PHRASES) == ()
    assert observe_claims.claimed_kinds("The build didn't succeed; build succeeds only locally",
                                        observe_claims.DEFAULT_CLAIM_PHRASES) == ("build",)

def test_phrase_file_overrides_defaults_and_ignores_bad_lines(tmp_path):
    (tmp_path / ".brigade").mkdir()
    (tmp_path / ".brigade" / "claim-phrases.txt").write_text("# team list\ntests: green\nnonsense\nbogus: x\n")
    assert observe_claims.load_claim_phrases(tmp_path) == (("tests", "green"),)

def test_claude_stop_appends_claim_without_message_text(tmp_path, monkeypatch, capsys):
    target = _observe_target(tmp_path, monkeypatch, capsys)
    runtime.handle_payload("PostToolUse", _write(target))
    runtime.handle_payload("Stop", _payload(target, "Stop", stop_hook_active=False,
                                            last_assistant_message="Refactor done, all tests pass."))
    claims, _ = observe_claims.read_claims(target)
    assert claims[0].claimed_kinds == ("tests",) and claims[0].last_write_at is not None
    assert "Refactor" not in observed_checks.digest_key_path(target).with_name("claims.jsonl").read_text()

def test_codex_stop_appends_claim(tmp_path, monkeypatch, capsys): ...
def test_message_without_claim_writes_nothing(tmp_path, monkeypatch, capsys): ...
def test_strict_stop_records_no_claim_and_still_blocks(tmp_path, monkeypatch, capsys): ...
```

- [ ] **Step 2: Run to verify failure**

Run: `brigade work verify run --target . --argv-json '["./scripts/verify-focused","tests/test_observe_claims.py","tests/test_claude_hooks_check_events.py","tests/test_observe_harness_hooks.py"]' --capture brigade-work`
Expected: FAIL.

- [ ] **Step 3: Implement** `observe_claims.py` (records go through `observed_checks.append_jsonl` and `read_jsonl`). Claude `Stop` in `on_event`: when the handled target is in observe mode, `stop_hook_active` is not true and `last_assistant_message` yields at least one kind, append a claim whose `last_write_at` is the state's `last_verification_write_at`, else `last_write_at`, else None. Codex `Stop`: same, with `last_write_at` from the observe session file.

- [ ] **Step 4: Run to verify pass.** Expected: PASS. `runtime.py` is not touched by this task.

- [ ] **Step 5: Commit**

```bash
git add src/brigade/observe_claims.py src/brigade/claude_hooks/check_events.py src/brigade/observe_harness_hooks.py tests
git commit -m "feat(hooks): record claimed check kinds from the final message"
```

### Task 12: `brigade work checks --claims`

**Files:**
- Modify: `src/brigade/observed_report.py`, `src/brigade/work_checks_cmd.py`
- Modify: `docs/technical-guide.md`
- Create: `changelog.d/stage3-claims-report.md`
- Test: `tests/test_work_checks_report.py`

**Interfaces:**
- Consumes: `read_claims` (Task 11), `read_checks`, `check_kind` (Task 5), receipts as in Task 10.
- Produces: `@dataclass(frozen=True) class ClaimRow: harness: str; model: str; claims: int; backed: int; unbacked: int; contradicted: int; unknown: int`. `claim_rows(target: Path, *, since: datetime | None = None) -> tuple[list[ClaimRow], ReportNotes]`. `render_claims(rows: list[ClaimRow], notes: ReportNotes) -> str`. `claims_json(rows, notes) -> dict[str, Any]`. Counting is per claimed kind, so one claim of `tests` and `lint` adds two to `claims`.

- [ ] **Step 1: Write the failing tests**

```python
def test_claim_backed_by_check_after_last_write(tmp_path):
    _claim(tmp_path, session="A", kinds=("tests",), last_write_at=T1, ts=T3)
    _checks(tmp_path, [("A", "pytest", "ok")], ts=T2)
    rows, _ = observed_report.claim_rows(tmp_path)
    assert (rows[0].claims, rows[0].backed) == (1, 1)

def test_check_before_last_write_does_not_back_claim(tmp_path): ...     # check at T0 < T1 -> unbacked 1
def test_failed_only_is_contradicted_and_unknown_only_is_unknown(tmp_path): ...
def test_generic_checks_kind_matches_any_tool(tmp_path): ...           # claim "checks", check "mypy" ok -> backed
def test_other_session_check_does_not_back_claim(tmp_path): ...
def test_receipt_backs_claim_by_session(tmp_path): ...                 # completed receipt with harness_session fp A
def test_claims_output_labels(tmp_path, capsys):
    assert cli.main(["work", "checks", "--claims", "--target", str(target)]) == 0
    out = capsys.readouterr().out
    assert observed_report.REPORT_LABEL in out and "claimed vs run" in out and "score" not in out.lower()
```

- [ ] **Step 2: Run to verify failure**

Run: `brigade work verify run --target . --argv-json '["./scripts/verify-focused","tests/test_work_checks_report.py"]' --capture brigade-work`
Expected: FAIL.

- [ ] **Step 3: Implement** matching: for each claim and each claimed kind, candidate evidence is observed checks with the same `session_fingerprint` and receipts whose `harness_session.fingerprint` equals it, with a timestamp after `last_write_at` (any time in the session when None) and at or before the claim. The kind matches when equal or when the claimed kind is `checks`. Outcome: `backed` if any candidate is `ok`. Else `contradicted` if any is `failed`. Else `unknown` if any is `unknown`. Else `unbacked`. `--claims` now prints this report under the heading `claimed vs run (observed, not independently verified)`.

- [ ] **Step 4: Run to verify pass**, then `python3 scripts/changelog_fragments.py check`. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/brigade/observed_report.py src/brigade/work_checks_cmd.py docs changelog.d tests
git commit -m "feat(work): add the claimed versus run checks report"
```

---

## Identity coverage after this plan

| Harness | Model | Agent or subagent type | Check status | Claim capture |
| --- | --- | --- | --- | --- |
| Claude Code | SessionStart `model`, updated by `PostModelSwitch` `to_model` | `agent_type` per tool event, `main` when absent | `ok` / `failed` with exit code / `unknown` | Stop `last_assistant_message` |
| Codex | `model` on every event | unknown (tool events carry the parent session id and no agent field) | unknown | Stop `last_assistant_message` |
| Cursor | `model_id`, else `model`, on every event | unknown (tool events carry no subagent field) | from `exitCode` in `tool_output`, else unknown | none (`stop` carries no message) |
| Grok, OpenCode, every other harness | unknown | unknown | not captured | none |
| `brigade work verify run` receipts | `BRIGADE_CONTEXT_MODEL`, else the Claude session cache through `BRIGADE_AGENT_SESSION` or `BRIGADE_CLAUDE_SESSION`, else unknown | unknown | receipt status | not applicable |

## Self-review

1. **Spec coverage.** Three-way mode with aliases: Tasks 1 to 3. Default unchanged, explicit writes, brief-time pin, unreadable resolves to strict: Task 1. Rails separate: `test_every_mode_leaves_rails_record_untouched` (Task 2). Observe never denies or blocks: Task 2. Observed checks as their own record type, never a receipt, command text never stored, no output: Task 5. Classifier reuse: Task 5 (`normalize_tool`) and Task 6 (`is_raw_verification`). Claude identity: Task 6. Codex and Cursor where verified, unknowns stated: Tasks 8 and 9 and the coverage table. Model and harness on receipts and outcome records: Task 7. First report with sample size, unknown bucket and label: Task 10. Second report with a configured phrase list: Tasks 11 and 12. "Score" reserved, ratchet and eligibility untouched: Global Constraints and the label tests. Every yolo caller assigned: caller table. Private per-user record directory and pre-push scan: Task 5. Gap found and fixed during review: `_normalize_state` drops unknown keys, so Task 6 adds `restore_identity` in both of its return paths.
2. **Step scan.** Each code step names the function, file and pinned values. Test steps carry names and assertions. Steps marked `...` name the exact assertions in a comment. No step says "handle edge cases".
3. **Type consistency.** `AgentIdentity` fields, `session_fingerprint(harness, session_id)`, `governing_modes -> list[tuple[Path, str]]`, `append_jsonl`/`read_jsonl`, `ObservedCheck`, `ObservedClaim`, `FirstPassRow`, `ClaimRow` and `ReportNotes` are used with the same names and types in every task that consumes them. Harness names are `claude-code`, `codex`, `cursor` throughout.
4. **Review Focus.** Each of the five lines has a named test in its owning task.
5. **Proportion.** The plan carries signatures, test names and assertions. The only algorithm bodies spelled out are the tool normalization order, the rotation lock and the claim matching rule, which the tests alone do not fix.

Round 4 identity contract: observed harness names come from `selection.KNOWN_HARNESSES`, with the existing `claude-code` hook spelling. Agent types use closed hook defaults and built-in names. Models are lowercase, 3 to 48 letters, digits, dots or hyphens with a known family prefix. One mapping applies on build, append and read. Unknown free text becomes `unknown`.

## Follow-ups from the pull request 2 reviews

Pull request 2 merged with the record stored in the per-user state directory, outside every repository. These items were deferred to the pull requests named.

- **Pull request 3 (hook capture):** add a `brigade doctor` line that says when recording is unavailable and why, without printing the path. Known causes: the state directory sits inside the project's nearest work tree, a parent directory is writable by other users, or the config directory is reached through a symbolic link.
- **Pull request 3:** the model field keeps a shaped model id with a known family prefix. Add new families to the table in `agent_identity.py` when a harness reports one, and consider stripping provider prefixes and date suffixes so those ids keep their family.
- **Pull request 5 (first-pass report):** when rotation keeps failing because another process holds the old generation open, the active file can pass the reader's size limit and the reader then returns nothing from it. Read the newest part of an oversized file instead of rejecting it.
- **Pull request 5:** each record is validated twice on read. Fold the two passes into one before reports read the file often.
- Windows relies on the user profile's inherited access rules for the state directory. Revisit only if a shared state location becomes a supported setup.

## Follow-up from the first-pass report

Pull request 5 reports observed checks only and omits `--claims`. Verify
receipts are a second source with a real pass or fail result, and the only
such source for a harness whose hooks cannot report status. Folding receipts
into this report first needs a read-only way to compute the planned-command
digest. The existing helper can create state and cannot be used by this report.

## Follow-ups from pull requests 3 and 4

Claude Code and Codex capture are merged. Cursor capture was removed from pull request 4 and needs its own design.

- **Cursor capture, new pull request.** Do not probe a binary for the version. Use the version Cursor reports in each hook payload, recorded by the hooks already installed, and install the failure event only when every reader seen is verified to accept it. Never write that event to the project-level hooks document, which other people's installs read. Without the failure event, record no Cursor checks at all, so the sample is never passes only.
- **Codex reports no pass or fail.** Every Codex record has unknown status. A pass rate for Codex has to come from verify receipts or another source.
- **Identity on a receipt is a label.** The cached model lives in workspace session state, so a receipt's identity is declared attribution, not proof of which model ran.
- **Model names are shaped, not allowlisted.** They reach the tracked outcome ledger. Revisit if a repository with a tracked ledger is public.
- **Timing-sensitive tests on Windows.** A hook-isolation test that starts a child brief process sits close to its timeout on the Windows runner, and an HTTP test file drops connections there intermittently. Give both a bound that scales, or fix the cause.
- The per-check capture budget is 250 ms. A dropped record under load is possible and silent.
