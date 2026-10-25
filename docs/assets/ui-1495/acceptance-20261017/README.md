# Deck spacing scale evidence

The baseline is `a4ef0b6e666a2253900042ad93cc09a2053fd568`. It already shares
the Deck shell, colors, and component sizes across the hub, boards, and Center.
This follow-up connects their core spacing rules to CSS properties generated
from `ui_theme.SPACING` and adds the explicit Center `deck-nav` class.

Captures use the actual Python renderers with synthetic data. The fleet fixture
comes from `tests/test_ui_theme_integration.py::fleet_pages` and
`render_populated_board_pages`. The populated policy fixture uses the document
and injected inventory from `tests/test_fleet_policy_page.py`. Center wraps the
Outcomes renderer in `render.page` with an example workspace and ranking entry.
Two ephemeral local servers freeze the before and after documents on separate
fresh ports. Meta refresh tags and polling scripts are removed in the capture
fixtures. Nonce style CSP remains enforced. No live fleet data or credentials
appear in the images.

Screenshots use Chromium through the T3 preview at 1280 by 900 CSS pixels.
The policy-form images scroll the first action into view. These are viewport
captures, not full-page images. Policy save times and the Center clock differ
between fixture generations.

| Page | Before | After |
| --- | --- | --- |
| Deck | [Image](before-deck.png) | [Image](after-deck.png) |
| Repository policy | [Image](before-deck-repos.png) | [Image](after-deck-repos.png) |
| Roster | [Image](before-deck-roster.png) | [Image](after-deck-roster.png) |
| Policy | [Image](before-deck-policy.png) | [Image](after-deck-policy.png) |
| Policy form and action caption | [Image](before-policy-form.png) | [Image](after-policy-form.png) |
| Machines board | [Image](before-view-machines.png) | [Image](after-view-machines.png) |
| Repositories board | [Image](before-view-repos.png) | [Image](after-view-repos.png) |
| Center Outcomes | [Image](before-view-outcomes.png) | [Image](after-view-outcomes.png) |

[Numeric browser evidence](layout.json) records both revisions at 1280px and
320px. Panel padding, helper gaps, action heights and containment, nav geometry,
table wrapper widths, and document widths match across all seven pages at both
widths. Each document fits its viewport. Text-field helper gaps remain 8px,
Preview change buttons remain 40px tall, and their captions stay inside panels.
Checkbox helper gaps are 11px in both revisions.

A temporary CSSOM override of `--space-4: 24px` changes panel padding from 16px
to 24px on Deck, Machines, and Center after this change. It leaves all three
baseline panels at 16px. Removing the override restores 16px in each case.
This exercises the shared scale in the browser without changing source.

[Code Graph evidence](graph-impact.json) preserves the pre-edit queries and
their actual impact sets. Markdown, new image assets, and the new evidence
files have no graph coverage. Static test attribution is a lower bound.

The preview covers presentation only. It does not exercise hub authentication,
policy submission, Center data fetching, or navigation outside these fixtures.
The existing focused renderer and HTTP tests cover those boundaries.
