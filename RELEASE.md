# Release checklist

Pre-flight before tagging a release.

## 1. Tests

```bash
.venv/bin/python -m pytest -q
```

Target: 100% green. No xfail in main suite.

## 2. Content-guard

```bash
brigade security template-audit --target .
brigade runbook run docs/runbooks/cold-start-gate.json --target .
```

Target: no blocker findings.

## 3. Local install smoke

```bash
rm -rf /tmp/brigade-rc dist build
pipx install --force "$PWD"
brigade --version
brigade init --target /tmp/brigade-rc --depth workspace --harnesses claude,codex,openclaw
brigade doctor --target /tmp/brigade-rc
```

Target: zero failed checks. Manual checks for optional external tools are expected when those tools are not installed.

## 4. Version bump

Edit:

- `pyproject.toml` → `[project] version = "X.Y.Z"`
- `src/brigade/__init__.py` → `__version__ = "X.Y.Z"`
- `src/brigade/templates/**/*.json` → every `_brigade_version` field (policies, hermes, memory)

Bump all of them together. Mismatches break `--version` reporting; the CI lint job checks internal sync on every push, and the publish workflow refuses a tag that does not match every declared version.

## 5. Tag

```bash
git commit -m "release: vX.Y.Z"
git tag vX.Y.Z
git push origin main
git push origin vX.Y.Z
```

## 6. Verify pipx install from tag

```bash
pipx uninstall brigade-cli
pipx install git+https://github.com/escoffier-labs/brigade@vX.Y.Z
brigade --version
```

Target: prints `brigade X.Y.Z`.

## 7. Update README install line

If the install command in README points at `main`, leave it. If it points at a tag, bump it.

## 8. Docs and handoff

Update README, QUICKSTART, CHANGELOG, and `docs/command-inventory.md` when the release adds or changes user-visible commands.
Create a Memory Handoff in `.claude/memory-handoffs/` for durable release workflow changes, root causes, or setup gotchas.
