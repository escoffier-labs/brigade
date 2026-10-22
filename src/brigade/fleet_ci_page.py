"""The themed read-only CI board. Provider URLs are constructed, never trusted."""

from __future__ import annotations

import html
from datetime import datetime
from typing import Any

from . import fleet_command_deck

CSS = """
.ci-repos { display: grid; gap: var(--space-5); }
.ci-repo { border: 1px solid var(--line); padding: var(--space-5); background: var(--surface); }
.ci-repo h2 { margin: 0; font-size: 20px; overflow-wrap: anywhere; }
.ci-top { display: flex; flex-wrap: wrap; justify-content: space-between; gap: var(--space-3); }
.ci-facts { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: var(--space-4); margin: var(--space-5) 0; }
.ci-fact { border-top: 1px solid var(--line); padding-top: var(--space-3); min-width: 0; }
.ci-label { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .06em; }
.ci-value { display: block; margin: var(--space-2) 0; font-size: 16px; overflow-wrap: anywhere; }
.ci-meta { font-size: 12px; color: var(--muted); overflow-wrap: anywhere; margin: var(--space-2) 0; }
.ci-chip { display: inline-block; padding: 4px 8px; border: 1px solid var(--line); font-size: 12px; }
.ci-sha { font-family: var(--mono); overflow-wrap: anywhere; }
.ci-prs { display: grid; gap: var(--space-3); list-style: none; padding: 0; }
.ci-prs li { border-top: 1px solid var(--line-quiet); padding-top: var(--space-3); }
@media (max-width: 800px) { .ci-facts { grid-template-columns: 1fr; } .ci-repo { padding: var(--space-4); } }
"""


def esc(value: object) -> str:
    return html.escape(str(value if value is not None else "unknown"), quote=True)


def provenance(fact: dict[str, Any]) -> str:
    return (
        f'<p class="ci-meta">{esc(fact.get("source", "unknown"))} · '
        f"<time>{esc(fact.get('observed_at'))}</time> · {esc(fact.get('freshness', 'unknown'))}</p>"
    )


def render(view: dict[str, Any], *, nonce: str) -> str:
    now = datetime.fromisoformat(view["as_of"].replace("Z", "+00:00"))
    body = [
        '<main class="deck-shell"><header class="masthead"><div><p class="eyebrow">Fleet operations</p>'
        "<h1>CI board</h1></div>"
        f'<p class="header-meta">{esc(view["as_of"])}</p></header>'
        '<p class="station-meta">Exact-head checks and workflow observations. '
        "GitHub remains authoritative for merge readiness.</p>",
        '<div class="ci-repos">',
    ]
    for repo in view["repositories"]:
        name = esc(repo["repo"])
        head, required, run = repo["head"], repo["required"], repo["latest_run"]
        body.append(
            f'<article class="ci-repo"><div class="ci-top"><h2>{name}</h2>'
            f'<span class="ci-chip">{esc(repo["visibility"])}</span></div><div class="ci-facts">'
            '<section class="ci-fact"><h3 class="ci-label">Default branch head</h3>'
            f'<span class="ci-value">{esc(head["branch"])}</span><code class="ci-sha">{esc(head["sha"])}</code>'
            + provenance(head)
            + '</section><section class="ci-fact"><h3 class="ci-label">Required checks</h3>'
            f'<strong class="ci-value">{esc(required["state"])}</strong>'
            '<span class="ci-meta">For the exact default-branch SHA. Neutral and skipped do not pass.</span>'
            + provenance(required)
            + '</section><section class="ci-fact"><h3 class="ci-label">Latest workflow run</h3>'
        )
        if run:
            url = f"https://github.com/{name}/actions/runs/{run['object_id']}/attempts/{run['attempt']}"
            body.append(
                f'<a class="ci-value" href="{url}" rel="noreferrer">Run {run["object_id"]} · attempt {run["attempt"]}</a>'
                f"<span>{esc(run['status'])} / {esc(run['conclusion'])}</span>"
                f'<p class="ci-meta">{esc(run["head_relation"])}</p><code class="ci-sha">{esc(run["head_sha"])}</code>'
                + provenance(run)
            )
        else:
            body.append('<span class="ci-value">unknown</span><p class="ci-meta">Latest workflow identity unknown.</p>')
        body.append("</section></div>")
        history = [
            r
            for r in repo["runs"]
            if run is None or (r["object_id"], r["attempt"]) != (run["object_id"], run["attempt"])
        ]
        if history:
            body.append('<details><summary>Recent workflow attempts</summary><ul class="ci-prs">')
            for previous in sorted(history, key=lambda r: (r["object_id"], r["attempt"]), reverse=True)[:10]:
                url = f"https://github.com/{name}/actions/runs/{previous['object_id']}/attempts/{previous['attempt']}"
                body.append(
                    f'<li><a href="{url}" rel="noreferrer">Run {previous["object_id"]} · attempt {previous["attempt"]}</a> '
                    f"{esc(previous['status'])} / {esc(previous['conclusion'])} · {esc(previous['head_relation'])}"
                    + provenance(previous)
                    + "</li>"
                )
            body.append("</ul></details>")
        body.append('<h3 class="ci-label">Open pull requests</h3><ul class="ci-prs">')
        for pr in repo["pulls"]:
            body.append(
                f'<li><a href="https://github.com/{name}/pull/{pr["number"]}" rel="noreferrer">PR #{pr["number"]}</a> '
                f'<span class="ci-chip">{esc(pr["rollup"]["state"])}</span> '
                f'<code class="ci-sha">{esc(pr["head_sha"])}</code>'
                + provenance(pr)
                + provenance(pr["rollup"])
                + "</li>"
            )
        if not repo["pulls"]:
            body.append(
                "<li>No open PRs observed.</li>" if repo["pulls_complete"] else "<li>Open PR inventory unknown.</li>"
            )
        body.append("</ul>")
        if not repo["complete"]:
            body.append(
                f'<p class="ci-meta">Incomplete scope: {esc(repo["reason"] or "unknown")}. Unobserved state is unknown.</p>'
            )
        body.append(
            f'<footer class="ci-meta">Last event received: <time>{esc(repo["last_event_received"])}</time><br>'
            f"Last reconciled: <time>{esc(repo['last_reconciled'])}</time></footer></article>"
        )
    if not view["repositories"]:
        body.append(
            '<section class="panel"><h2>No CI observations yet</h2><p>Run node reconciliation to populate repository heads, workflows and checks.</p></section>'
        )
    body.append("</div></main>")
    return fleet_command_deck._document(
        "".join(body), nonce=nonce, now=now, title="CI board", active="ci", refresh=False, extra_css=CSS
    )
