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

## Repeatable populated board check

`render_populated_board_pages()` in `tests/test_ui_theme_integration.py` returns
both actual board documents with a fixed clock, nonce `integration-nonce`, two
synthetic nodes, two claims, two active runs awaiting approval, and two completed
runs. Repository, node, and seat labels are deliberately long and include escaped hostile text.
The Machines fixture includes finished runs. Its two tables have two rows each.
The Repos fixture has one table with two rows and completed outcomes.

From the checkout, run this command to write both HTML fixtures outside the
repository and serve a synthetic local ephemeral preview. It prints the fixture
directory and an automatically allocated loopback port. Stop it with Ctrl-C when
the browser check is finished.

If the collaborative browser runs on another machine, use the environment-port
route and bind the temporary server to the development environment's reachable
interface. Serve only these synthetic pages and stop the server after the check.

```bash
.venv/bin/python - <<'PY'
import runpy
import tempfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlsplit

fixture = runpy.run_path("tests/test_ui_theme_integration.py")
pages = fixture["render_populated_board_pages"]()
root = Path(tempfile.mkdtemp(prefix="fleet-board-preview-"))
for route, source in pages.items():
    (root / (route.rsplit("/", 1)[-1] + ".html")).write_text(source, encoding="utf-8")
nonce = fixture["BOARD_NONCE"]
csp = (
    f"default-src 'none'; script-src 'nonce-{nonce}'; script-src-attr 'none'; "
    f"style-src 'nonce-{nonce}'; img-src 'self'; manifest-src 'self'; "
    "connect-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)

class Preview(BaseHTTPRequestHandler):
    def do_GET(self):
        route = urlsplit(self.path).path
        route = "/view/machines" if route == "/" else route
        if route not in pages:
            self.send_error(404)
            return
        body = pages[route].encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", csp)
        self.end_headers()
        self.wfile.write(body)

with HTTPServer(("127.0.0.1", 0), Preview) as server:
    print(f"HTML fixtures: {root}", flush=True)
    print(f"Preview: http://127.0.0.1:{server.server_port}/view/machines", flush=True)
    server.serve_forever()
PY
```

Use the existing T3 collaborative browser to navigate to `/view/machines` on
the printed preview origin. Set its viewport to 320 by 900 CSS pixels. After
navigation completes, pass the complete contents of
[`tests/ui/fleet_board_layout.js`](../../../../tests/ui/fleet_board_layout.js)
as the JavaScript expression to `preview_evaluate`. The file is a self-contained
IIFE: paste it directly, without loading an external script or calling `eval`.
Repeat after navigating to `/view/repos` on the same origin.

The check throws `fleet board layout FAIL` with measurements if the viewport is
not 320px, populated rows are missing or hidden, the document overflows, a table
lacks its own scroll wrapper, or a long table cannot scroll horizontally within
that wrapper. A clean result returns `status: "PASS"`, viewport and document
widths, and each table's row count, table width, wrapper client and scroll widths,
and positive observed `scrollLeft`. Each wrapper's scroll position is restored.

For a negative control, evaluate the following temporary DOM mutation followed
immediately by the complete JS check in the same `preview_evaluate` expression:

```javascript
document.querySelectorAll(".table-wrap").forEach((wrapper) => {
  wrapper.replaceWith(...wrapper.childNodes);
});
// Paste the complete fleet_board_layout.js IIFE here.
```

Record the resulting FAIL, then reload the fixture and evaluate the unchanged
check to record PASS. Run this sequence on both boards. Keep the mutation and
check in one evaluation because the actual documents refresh every ten seconds.
This control changes only the temporary browser DOM. It never edits source.

The 2026-10-04 T3 browser run recorded these results at 320 by 900 CSS pixels.
The document width was 305px, with space reserved for the vertical scrollbar.

| Board | Table rows | Wrapper width | Table width | Observed scrollLeft | Width without wrappers |
| --- | --- | --- | --- | --- | --- |
| Machines | Two per table | 247px | 327px | 64px | 356px, FAIL |
| Repos | Two | 281px | 405px | 64px | 417px, FAIL |

Both clean pages passed. Removing wrappers failed, and reloading restored PASS.
[The numeric report](populated-layout.json) records both controls and source hashes.

Default pytest CI checks the populated renderer HTML, table ancestry, rows,
escaping, links, and table semantics. It does not execute this browser JS check
or measure layout. The browser run must supply numeric PASS/FAIL evidence
and the negative control. The earlier screenshots are evidence for their captured
revision. They do not replace this executable check. This preview uses no live
fleet data or credentials, and its fixed nonce is only for synthetic local
fixtures. Hub authorization, navigation beyond the two board routes, and form
submission remain outside this static preview.
