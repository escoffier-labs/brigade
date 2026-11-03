# Changelog fragments

Pull requests do not edit `CHANGELOG.md`. A PR with a user-visible effect adds one
fragment file here instead. The release cut folds the fragments into
`## [Unreleased]` and deletes them, so sibling PRs never share a changelog hunk.

## Writing a fragment

Create `changelog.d/<pr-number-or-slug>.md`, for example `2231.md` or
`fix-hub-lease.md`. Use only lowercase letters, digits, `.`, `_` and `-`, and end
the name in `.md`.

The file holds one or more bullets. Each starts with `- ` at the left margin, and an
indented line continues the bullet above it. Blank lines between bullets are fine.
Nothing else is allowed: no headings, no prose paragraphs, no category markers.

```markdown
- Describe the effect a user sees, not the commit subject.
- A second change in the same PR gets its own bullet.
```

Write effects, not commit subjects. A PR with no user-visible effect needs no
fragment. Do not repeat an entry that another fragment already holds.

## Checking

```bash
python3 scripts/changelog_fragments.py check
```

CI runs the same check, plus a guard that fails a PR that edits `CHANGELOG.md`
outside a release commit. Corrections to an entry that already reached
`CHANGELOG.md` are made during the release cut.

## Release cut

On the release branch, before rolling the changelog:

```bash
python3 scripts/changelog_fragments.py assemble
```

This appends every fragment to `## [Unreleased]` in numeric filename order and
deletes the fragment files. Then roll `Unreleased` under the new `## [X.Y.Z] - date`
heading and bump the version as usual. The release commit is the one that adds that
version heading, and the guard requires it to leave no fragment behind. If a
fragment lands on `main` after the release branch was cut, run `assemble` again on
the updated branch.
