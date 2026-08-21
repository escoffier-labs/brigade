# Borderless README wordmark implementation plan

**Goal:** Remove the visible frame from the README wordmark while keeping enough contrast on GitHub light mode.

**Files:**

- `tests/test_readme_brand_header.py`
- `docs/assets/brigade-wordmark.svg`

### Task 1: Lock the SVG contract

- [x] Update the focused test to require a full-bleed `#0d1014` panel with no stroke or radius.
- [x] Lock the existing dot center at `cx="366.2"`.
- [x] Run the focused test through Brigade and record the expected failure.

### Task 2: Apply the approved treatment

- [x] Expand the panel to the full `920 x 280` viewBox.
- [x] Remove the stroke, stroke width, and corner radius.
- [x] Re-run the focused test through Brigade and record the pass.

### Task 3: Verify and publish

- [x] Render the SVG in Chromium and inspect the edge and dot alignment.
- [ ] Run the focused test and changed-file quality checks through Brigade.
- [ ] Review the diff, push the feature branch, open the PR, wait for required checks, and squash merge.
