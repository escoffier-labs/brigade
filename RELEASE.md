# Release checklist

Pre-flight before tagging a release.

## 1. Tests

```bash
.venv/bin/python -m pytest -q
```

Target: 100% green. No xfail in main suite.

## 2. Content-guard

```bash
PYTHONPATH=$HOME/repos/content-guard/src python3 -m content_guard scan . \
  --policy $HOME/repos/content-guard/policies/public-repo.json
```

Target: `Clean.` or warn-only output. No `BLOCK` findings.

## 3. Local install smoke

```bash
rm -rf /tmp/solo-mise-rc
pipx install --force "$PWD"
solo-mise --version
solo-mise init --target /tmp/solo-mise-rc --profile workspace
solo-mise doctor --target /tmp/solo-mise-rc
```

Target: zero failed checks. Manual checks for content-guard / OpenClaw are expected when those tools are not installed.

## 4. Version bump

Edit:

- `pyproject.toml` → `[project] version = "X.Y.Z"`
- `src/solo_mise/__init__.py` → `__version__ = "X.Y.Z"`
- `src/solo_mise/templates/policies/*.json` → `_solo_mise_version` fields
- `src/solo_mise/templates/hermes/*.json` → `_solo_mise_version` fields

Bump both numbers together. Mismatches break `--version` reporting.

## 5. Tag

```bash
git commit -m "release: vX.Y.Z"
git tag vX.Y.Z
git push origin main
git push origin vX.Y.Z
```

## 6. Verify pipx install from tag

```bash
pipx uninstall solo-mise
pipx install git+https://github.com/solomonneas/solo-mise@vX.Y.Z
solo-mise --version
```

Target: prints `solo-mise X.Y.Z`.

## 7. Update README install line

If the install command in README points at `main`, leave it. If it points at a tag, bump it.

## 8. Cookbook handoff

Update the cookbook pointer in `~/repos/solos-cookbook/README.md` if anything user-visible changed (new profile, new command, new flag).
