"""Progressive disclosure for the Overview's top-level sections."""

from html import escape


def heading(identifier: str, title: str, count: int | None = None) -> str:
    identifier = escape(identifier)
    summary = "" if count is None else f'<small class="overview-section-count"> · {count}</small>'
    return (
        f'<h2 id="{identifier}"><button type="button" class="overview-section-toggle" '
        f'aria-expanded="true" aria-controls="{identifier}-content">{escape(title)}{summary}</button></h2>'
    )


def body(identifier: str, content: str) -> str:
    return f'<div class="overview-section-body" id="{escape(identifier)}-content">{content}</div>'


def card(identifier: str, title: str, href: str, content: str, count: int) -> str:
    return (
        f'<section class="admin-card" aria-labelledby="{escape(identifier)}"><header>'
        f'{heading(identifier, title, count)}<a class="card-link" href="{escape(href)}">View all</a></header>'
        f"{body(identifier, content)}</section>"
    )


CSS = """
.overview-section-toggle { color:inherit; font:inherit; background:none; border:0; padding:2px 0;
  text-align:left; cursor:pointer; }
.overview-section-toggle::before { content:""; display:inline-block; width:.45em; height:.45em;
  margin:0 .65em .18em .12em; border:solid currentColor; border-width:0 1px 1px 0; transform:rotate(45deg); }
.overview-section-toggle[aria-expanded="false"]::before { transform:rotate(-45deg); }
.overview-section-count { color:var(--muted); font-weight:400; }
.overview-section-body[hidden] { display:none; }
"""

SCRIPT = """window.fleetOnInit(function(){
document.querySelectorAll('.overview-section-toggle').forEach(function(button){
  var panel=document.getElementById(button.getAttribute('aria-controls'));
  if(!panel)return;
  var key='brigade.deck.section.'+panel.id;
  function show(open){panel.hidden=!open;button.setAttribute('aria-expanded',String(open));}
  var open=true;
  try{open=localStorage.getItem(key)!=='collapsed';}catch(error){}
  show(open);
  button.onclick=function(){
    var next=panel.hidden;
    try{localStorage.setItem(key,next?'expanded':'collapsed');}catch(error){}
    show(next);
  };
});
});"""
