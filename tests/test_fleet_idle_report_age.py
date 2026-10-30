"""Idle report freshness at the Overview HTTP, markup, and refresh boundaries."""

import json
import re
from dataclasses import replace
from datetime import datetime

import pytest

from brigade import fleet_command_deck as deck, fleet_hub_http, fleet_deck_pages_http, fleet_deck_shell
from tests.fleet_script_dom import Tree, run_script
from tests.test_fleet_command_deck import CONFIG, _bearer, _request, _start_hub
from tests.test_fleet_deck_overview import NOW, NODE_A, _machine, _plane, _run, _station, _view
from tests.test_ui_theme_integration import Markup


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


def report_age_view():
    """Only fake machines; also reused for base/head screenshot comparisons."""
    nodes = [f"{i:08d}-1111-4111-8111-111111111111" for i in range(1, 4)]
    stations = (
        _station(nodes[0], "Example idle", 4, heard=4380),
        _station(nodes[1], "Example never", 4, heard=None),
        _station(nodes[2], "Example working", 4, [_run(nodes[2], "example-run", "example/repo")]),
    )
    return _view(stations, plane=_plane([_machine(station.label, station.station.node_id) for station in stations]))


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("history", "1h 13m"),
        ("never", "never"),
        ("malformed", "unknown"),
        ("job", None),
        ("session", None),
        ("stale", None),
        ("offline", None),
        ("limited", None),
        ("unknown", None),
        ("unenrolled", None),
    ],
)
def test_overview_report_age_initial_and_soft_refresh(tmp_path, monkeypatch, case, expected):
    station = _station(NODE_A, "Example machine", 4, heard=None if case == "never" else 4380)
    machine = _machine("Example machine", NODE_A)
    if case == "malformed":
        station = replace(station, last_heard="not-a-timestamp")
    elif case == "job":
        station = _station(NODE_A, "Example machine", 4, [_run(NODE_A, "example-run", "example/repo")])
    elif case == "session":
        station = replace(station, sessions=1)
    elif case == "stale":
        machine = replace(machine, unused_reason="stale-telemetry")
    elif case == "offline":
        machine = replace(machine, state="offline")
    elif case == "limited":
        machine = replace(machine, unused_reason="machine-disabled")
    elif case == "unenrolled":
        station = replace(station, enrolled=False)
    view = _view([station], plane=_plane([] if case == "unknown" else [machine]))
    monkeypatch.setattr(fleet_deck_pages_http, "build_view", lambda *_args: (view, [], []))
    monkeypatch.setattr(fleet_hub_http, "datetime", FrozenDatetime)
    with _start_hub(tmp_path, CONFIG) as (hub, _db):
        for extra in ({}, {"X-Fleet-Soft-Refresh": "1"}):
            status, _headers, page = _request(hub, "GET", "/deck", headers=_bearer(extra))
            assert status == 200
            panel = page.split('<section class="overview-machines"', 1)[1].split("</section>", 1)[0]
            telemetry = "unknown" if case == "unknown" else "1m"
            assert f"<span>Telemetry</span><b>{telemetry}</b>" in panel
            if expected is None:
                assert "Last run report" not in panel
                if case in {"job", "session"}:
                    assert ">working<" in panel
                    assert ("1/4 slots" if case == "job" else "0/4 slots") in panel
                    assert ("<span>Agent sessions</span><b>1</b>" in panel) == (case == "session")
            else:
                # Plain HTML outside agent-only data, tooltips, details, and scripts:
                # the same label is visible to Admin, Agent, and no-script readers.
                markup = Markup(page)
                labels = [
                    i
                    for i, (tag, _attrs) in enumerate(markup.tags)
                    if tag == "span" and markup.text[i].startswith("Last run report:")
                ]
                assert len(labels) == 1
                for ancestor in markup.ancestors[labels[0]]:
                    tag, attrs = markup.tags[ancestor]
                    assert tag not in {"script", "details", "template"}
                    assert "hidden" not in attrs and "data-audience" not in attrs
                assert f"Last run report: <b>{expected}</b>" in panel


def test_overview_machine_panel_uses_shrinkable_cells_and_mobile_grid():
    page = deck.render_deck(report_age_view(), nonce="fixture", now=NOW)
    markup = Markup(page)
    labels = [
        i
        for i, (tag, _attrs) in enumerate(markup.tags)
        if tag == "span" and markup.text[i].startswith("Last run report:")
    ]
    assert len(labels) == 2
    for label in labels:
        cell = markup.ancestors[label][-1]
        row = markup.ancestors[cell][-1]
        assert markup.tags[cell] == ("div", {"class": "overview-machine-age"})
        assert markup.tags[row] == ("div", {"class": "overview-machine-row"})
        cells = [i for i in markup.descendants(row, "div") if markup.ancestors[i][-1] == row]
        assert len(cells) == 4 and cells[-1] == cell
        assert markup.tags[label] == ("span", {})

    stylesheet = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", page, re.S))
    assert ".overview-machines { min-width:0;" in stylesheet
    assert ".overview-machine-rows { min-width:0; }" in stylesheet
    assert (
        ".overview-machine-name, .overview-machine-capacity, .overview-machine-load, "
        ".overview-machine-age { min-width:0; }"
    ) in stylesheet
    assert ".overview-machine-load > span, .overview-machine-age > span { display:block;" in stylesheet
    # The report wraps within the existing fourth cell at desktop widths, including
    # 1100/1280px. It must not gain a fixed width or suppress normal text wrapping.
    age_rules = [
        body
        for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", stylesheet)
        if ".overview-machine-age" in selector
    ]
    assert age_rules
    for rule in age_rules:
        assert not re.search(r"(?:^|;)\s*(?:width|max-width|flex-basis)\s*:", rule)
        assert not re.search(r"min-width\s*:\s*[1-9]", rule)
        assert not re.search(r"white-space\s*:\s*(?:nowrap|pre)\s*(?:;|$)", rule)
    # At 390px the shell drops its sidebar column and machine rows stack.
    narrow_shell = stylesheet.split("@media (max-width: 900px)", 1)[1]
    assert ".shell { grid-template-columns: 1fr; }" in narrow_shell
    mobile = stylesheet.split("@media (max-width:700px)", 1)[1]
    assert ".overview-machine-row { grid-template-columns:minmax(0,1fr); gap:8px; }" in mobile
    assert ".overview-machine-age { display:flex; gap:6px; align-items:baseline; }" in mobile
    assert ".overview-machine-age > span { display:inline; }" in mobile


def test_report_age_survives_the_soft_refresh_swap():
    refreshed = deck.render_deck(report_age_view(), nonce="fixture", now=NOW)
    initial_view = report_age_view()
    initial_view = replace(
        initial_view, stations=tuple(replace(station, last_heard=None) for station in initial_view.stations)
    )
    page = deck.render_deck(initial_view, nonce="fixture", now=NOW)
    # Supply browser APIs at the existing Node DOM boundary. The shipped script
    # owns the fetch, region selection, node replacement, and audience behavior.
    setup = """
Object.defineProperties(Element.prototype,{
 tagName:{get(){return this.tag.toUpperCase();}},parentNode:{get(){return this.parent;}}
});
Element.prototype.appendChild=function(node){node.parent=this;this.children.push(node);return node;};
Element.prototype.contains=function(node){while(node){if(node===this)return true;node=node.parent;}return false;};
const matches=Element.prototype.matches;
Element.prototype.matches=function(selector){return selector==='*'||matches.call(this,selector);};
function prepare(node){node.classList.contains=name=>node.matches('.'+name);
 node.children.filter(child=>typeof child!=='string').forEach(prepare);return node;}
prepare(dom);
document.body=dom.querySelector('body');document.addEventListener=()=>{};
document.importNode=node=>prepare(node.cloneNode());
window.scrollX=0;window.scrollY=0;window.scrollTo=()=>{};
location.href='https://example.invalid/deck';location.pathname='/deck';location.search='';
var tick,requests=0;
global.setTimeout=fn=>{tick=fn;return 1;};global.clearTimeout=()=>{};
"""
    setup += "const refreshed=" + json.dumps(refreshed) + ";\n"
    setup += "const nextTree=" + json.dumps(Tree(refreshed).root) + ";\n"
    setup += """
global.DOMParser=class{parseFromString(text,kind){
 assert.equal(text,refreshed);assert.equal(kind,'text/html');return prepare(new Element(nextTree));}};
global.fetch=(url,options)=>{requests++;assert.equal(url,location.href);
 assert.equal(options.headers['X-Fleet-Soft-Refresh'],'1');
 return Promise.resolve({status:200,headers:{get:()=> 'text/html'},text:()=>Promise.resolve(refreshed)});};
"""
    from brigade import fleet_admin_explorer as explorer

    run_script(
        page,
        setup + explorer.SHELL_SCRIPT + fleet_deck_shell.soft_refresh_script(10),
        """
async function check(){
 const ages=()=>document.querySelectorAll('.overview-machine-age').map(el=>el.textContent);
 const main=document.querySelector('.shell-main');
 const oldRow=document.querySelector('.overview-machine-row');
 assert.ok(ages()[0].includes('Last run report: never'));
 tick();await new Promise(setImmediate);
 assert.equal(requests,1);assert.equal(document.querySelector('.shell-main'),main);
 assert.notEqual(document.querySelector('.overview-machine-row'),oldRow);
 assert.ok(ages()[0].includes('Last run report: 1h 13m'));
 assert.ok(ages()[1].includes('Last run report: never'));
 const working=document.querySelectorAll('.overview-machine-row')[2].textContent;
 assert.ok(!working.includes('Last run report')&&working.includes('1/4 slots'));
 for(const mode of ['admin','agent']){
  window.fleetSetAudience(mode);
  assert.equal(htmlRoot.getAttribute('data-audience-mode'),mode);
  for(const age of document.querySelectorAll('.overview-machine-age')){
   for(let el=age;el;el=el.parentNode){assert.ok(!el.hidden);assert.ok(!el.hasAttribute('data-audience'));}
  }
 }
}
check().catch(error=>{console.error(error);process.exitCode=1;});
""",
    )
