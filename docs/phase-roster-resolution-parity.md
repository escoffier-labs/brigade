# Roster Resolution Parity Plan

Goal: make `brigade run` and `brigade roster doctor` resolve explicit, workspace, and user roster paths through one tested helper.

Architecture: `src/brigade/roster.py` owns path selection and missing-path diagnostics. The run CLI and roster doctor call that helper before loading the TOML. Explicit `--roster` paths never fall back, workspace rosters win over user rosters, and a missing implicit roster names both checked paths.

Key tech: Python `pathlib`, pytest, existing Brigade doctor reporting. Execute the task in order, keep the checkbox state current, run the failing test before production edits, and commit only after the focused and full gates pass.

## File Map

- `src/brigade/roster.py`: resolve one roster path and raise a stable missing-path error.
- `src/brigade/cli/run.py`: replace the inline fallback block with the shared resolver.
- `src/brigade/roster_cmd.py`: use the same resolver before loading and reporting the selected path.
- `tests/test_roster.py`: unit coverage for precedence, explicit-path behavior, and missing diagnostics.
- `tests/test_roster_cmd.py`: doctor regression for user fallback and an isolated missing-roster test.
- `tests/test_run_cli.py`: existing CLI regressions remain the compatibility gate.

## Task 1: Centralize roster path selection

**Files:**

- Modify: `src/brigade/roster.py`
- Modify: `src/brigade/cli/run.py`
- Modify: `src/brigade/roster_cmd.py`
- Modify: `tests/test_roster.py`
- Modify: `tests/test_roster_cmd.py`
- Test: `tests/test_run_cli.py`

- [x] Add the failing doctor regression to `tests/test_roster_cmd.py`:

```python
def test_roster_doctor_falls_back_to_home_roster(monkeypatch, tmp_target, tmp_path, capsys):
    home = tmp_path / "home"
    path = home / ".brigade" / "roster.toml"
    path.parent.mkdir(parents=True)
    path.write_text(
        'orchestrator = "chef"\n'
        '[agents.chef]\n'
        'endpoint = "https://example.test/v1/chat"\n'
        'model = "some-hosted-model"\n'
        'role = "plan"\n'
    )
    monkeypatch.setattr(Path, "home", lambda: home)

    assert roster_cmd.doctor(tmp_target) == 0
    assert str(path) in capsys.readouterr().out
```

- [x] Isolate `test_roster_doctor_missing_file_fails` from the real user roster by adding `monkeypatch` and `tmp_path`, setting `Path.home()` to `tmp_path / "empty-home"`, and retaining its existing failure assertions.

- [x] Run the new regression and watch it fail:

```bash
.venv/bin/pytest tests/test_roster_cmd.py::test_roster_doctor_falls_back_to_home_roster -q
```

Expected: FAIL because `roster_cmd.doctor()` checks only `tmp_target/.brigade/roster.toml`.

- [x] Add resolver unit tests to `tests/test_roster.py`:

```python
def test_resolve_roster_path_prefers_workspace(monkeypatch, tmp_path):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    local = workspace / ".brigade" / "roster.toml"
    user = home / ".brigade" / "roster.toml"
    local.parent.mkdir(parents=True)
    user.parent.mkdir(parents=True)
    local.write_text(VALID)
    user.write_text(VALID)
    monkeypatch.setattr(Path, "home", lambda: home)
    assert roster_mod.resolve_roster_path(workspace) == local


def test_resolve_roster_path_uses_user_fallback(monkeypatch, tmp_path):
    home = tmp_path / "home"
    user = home / ".brigade" / "roster.toml"
    user.parent.mkdir(parents=True)
    user.write_text(VALID)
    monkeypatch.setattr(Path, "home", lambda: home)
    assert roster_mod.resolve_roster_path(tmp_path / "workspace") == user


def test_resolve_roster_path_explicit_never_falls_back(monkeypatch, tmp_path):
    home = tmp_path / "home"
    user = home / ".brigade" / "roster.toml"
    user.parent.mkdir(parents=True)
    user.write_text(VALID)
    missing = tmp_path / "missing.toml"
    monkeypatch.setattr(Path, "home", lambda: home)
    with pytest.raises(FileNotFoundError, match=str(missing)):
        roster_mod.resolve_roster_path(tmp_path / "workspace", missing)


def test_resolve_roster_path_missing_names_both_candidates(monkeypatch, tmp_path):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    monkeypatch.setattr(Path, "home", lambda: home)
    with pytest.raises(FileNotFoundError) as exc:
        roster_mod.resolve_roster_path(workspace)
    message = str(exc.value)
    assert str(workspace / ".brigade" / "roster.toml") in message
    assert str(home / ".brigade" / "roster.toml") in message
```

- [x] Add the minimal resolver to `src/brigade/roster.py` above `load_roster()`:

```python
def resolve_roster_path(target: Path, explicit: Path | None = None) -> Path:
    if explicit is not None:
        path = explicit.expanduser()
        if path.exists():
            return path
        raise FileNotFoundError(f"roster not found: {path}")

    workspace_path = target.expanduser() / ".brigade" / "roster.toml"
    user_path = Path.home() / ".brigade" / "roster.toml"
    if workspace_path.exists():
        return workspace_path
    if user_path.exists():
        return user_path
    raise FileNotFoundError(f"roster not found: checked {workspace_path} and {user_path}")
```

- [x] Replace the inline roster-selection block in `src/brigade/cli/run.py` with:

```python
try:
    roster_path = roster_mod.resolve_roster_path(run_cwd, args.roster)
except FileNotFoundError as exc:
    print(f"error: {exc}. Create .brigade/roster.toml or pass --roster.", file=sys.stderr)
    return 2
```

Keep the existing `load_roster()` error handling directly after this block.

- [x] In `src/brigade/roster_cmd.py::doctor`, resolve the path before `load_roster()`:

```python
try:
    path = roster_mod.resolve_roster_path(target, roster_path)
    loaded = roster_mod.load_roster(path)
except FileNotFoundError as exc:
    checks.append((doctor_mod.FAIL, "roster: file", f"{exc}; run `brigade roster init`"))
    return doctor_mod._report(checks)
```

Retain the existing invalid-roster branch and all agent diagnostics.

- [x] Run the focused suite through Brigade:

```bash
.venv/bin/brigade work verify run --target . --command ".venv/bin/pytest tests/test_roster.py tests/test_roster_cmd.py tests/test_run_cli.py -q" --capture brigade-work
```

Expected: all selected tests pass.

- [x] Run the full gate through Brigade:

```bash
.venv/bin/brigade work verify run --target . --command "env PY=.venv/bin ./scripts/verify" --capture brigade-work
```

Expected: exit 0 with the coverage floor met.

- [x] Commit:

```bash
git add src/brigade/roster.py src/brigade/cli/run.py src/brigade/roster_cmd.py tests/test_roster.py tests/test_roster_cmd.py docs/phase-roster-resolution-parity.md
git commit -m "fix(roster): align user fallback resolution"
```
