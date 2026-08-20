# README brand header implementation plan

**Goal:** Make the repository README wordmark match the deployed `brigade.tools` Signal treatment and restore the seven shieldcn status badges.

The README continues to load one local SVG asset. That asset will carry outlined Inter glyphs, so GitHub never substitutes Arial or another system font. A narrow regression test will lock the palette, the single amber dot, the absence of live SVG text, and the shield row without adding runtime dependencies.

**File map**

- `tests/test_readme_brand_header.py`: public README brand contract.
- `docs/assets/brigade-wordmark.svg`: self-contained outlined wordmark.
- `README.md`: centered shieldcn badge row below the primary links.
- `docs/readme-coverage.md`: records that the badge row is intentionally present.

### Task 1: Lock the README brand contract

**Files:**
- Create: `tests/test_readme_brand_header.py`

- [x] Add a focused test that parses `docs/assets/brigade-wordmark.svg` and asserts:
  - the panel uses `#0f1318`, `#2a323d`, and an `8` radius;
  - the wordmark uses `#dde3ea`, the maker line uses `#9aa4b2`, and exactly one element uses amber `#e0a45c`;
  - the amber element is the circle identified as `i-dot`;
  - the SVG contains outlined paths and no `<text>` elements, `Arial`, or the retired blue `#5d8dff`;
  - the title remains `Brigade (by Escoffier Labs)`;
  - `README.md` contains the seven expected `shieldcn.dev` URLs exactly once.
- [x] Run RED through Brigade:
  `brigade work verify run --target . --command "pytest -q tests/test_readme_brand_header.py" --capture brigade-work`
  Expect one failure against the current Arial/blue SVG.
- [ ] Commit the failing test with `test: lock README brand header`.

### Task 2: Replace the wordmark with outlined Inter glyphs

**Files:**
- Modify: `docs/assets/brigade-wordmark.svg`

- [ ] Generate the word `brıgade` from Inter ExtraBold 800 and `(by Escoffier Labs)` from Inter Medium 500 as Cairo SVG outlines. Use Inter 4.1 only as a temporary generation input, matching the deployed site's Inter family; do not add the font or a package dependency to the repository.
- [ ] Compose a `920 x 280` SVG with:
  - panel fill `#0f1318`, stroke `#2a323d`, radius `8`;
  - centered off-white outlined wordmark;
  - one amber circle over the dotless `ı`, with no amber terminal period;
  - centered muted maker line below the wordmark;
  - `<title>` and `<desc>` for accessibility.
- [ ] Render the SVG to PNG with ImageMagick and inspect the result for clipping, centering, and the single-dot treatment.
- [ ] Run GREEN through Brigade with the same focused pytest command. Expect `1 passed`.
- [ ] Commit the asset with `docs: match README wordmark to brigade.tools`.

### Task 3: Verify the complete README header

**Files:**
- Verify: `README.md`
- Verify: `docs/readme-coverage.md`
- Verify: `docs/assets/brigade-wordmark.svg`
- Verify: `tests/test_readme_brand_header.py`

- [ ] Request all seven shieldcn URLs and require HTTP 200.
- [ ] Run Vale and content guard on the changed public files through Brigade.
- [ ] Re-run the focused brand test through Brigade after every accepted edit.
- [ ] Inspect the final diff and confirm no product claims or comparison content changed.
- [ ] Write and lint the memory handoff if the brand contract produced durable knowledge.
