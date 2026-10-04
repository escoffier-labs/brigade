# Shared Deck presentation evidence

These captures use synthetic nodes, repositories, policy, and roster data through
the actual Python renderers. The before source is commit `99b38154`. The after
source is the accompanying presentation change. An ephemeral local fixture
server applies the hub's nonce-based script and style policy. No operator hub
credentials or live fleet data were used.

Desktop captures are 1280 CSS pixels wide and extend to the page's full height.
The `-320` captures use a 320 by 900 CSS-pixel viewport. They show responsive
layout in a desktop browser, rather than emulating a mobile browser engine.

| Page | Before | After | Before at 320px | After at 320px |
| --- | --- | --- | --- | --- |
| Command Deck | [Image](before-deck.png) | [Image](after-deck.png) | [Image](before-deck-320.png) | [Image](after-deck-320.png) |
| Roster | [Image](before-deck-roster.png) | [Image](after-deck-roster.png) | [Image](before-deck-roster-320.png) | [Image](after-deck-roster-320.png) |
| Policy | [Image](before-deck-policy.png) | [Image](after-deck-policy.png) | [Image](before-deck-policy-320.png) | [Image](after-deck-policy-320.png) |
| Repository policy | [Image](before-deck-repos.png) | [Image](after-deck-repos.png) | [Image](before-deck-repos-320.png) | [Image](after-deck-repos-320.png) |
| Machines board | [Image](before-view-machines.png) | [Image](after-view-machines.png) | [Image](before-view-machines-320.png) | [Image](after-view-machines-320.png) |
| Repositories board | [Image](before-view-repos.png) | [Image](after-view-repos.png) | [Image](before-view-repos-320.png) | [Image](after-view-repos-320.png) |

Browser measurements after the change:

- All six pages fit the 320px viewport without document-level horizontal overflow.
  Before the change, Machines had a 332px document width and Repos had 359px.
- Form buttons are 40px tall. Deck stations have a 16px gap before Control plane.
- Policy helper text has an 8px gap below its control, and action captions remain
  inside their panels.
- All six pages have nonce-bearing script and style elements, with no inline
  style attributes. Typing `public-tool` in the Repos quick filter hides the
  `private-tool` row and keeps the matching row visible.

The static fixture server exercises rendering and page scripts. HTTP route,
authorization, and form submission behavior are covered by the existing server
tests. Static preview links are not evidence of live hub navigation. The preview
reported that the document lacked focus during keyboard checks, so it did not
provide evidence for the `:focus-visible` state.
