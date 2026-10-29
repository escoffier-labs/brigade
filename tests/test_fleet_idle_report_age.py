"""Idle report freshness at the full Overview HTTP and browser boundaries."""

import json
import re
import shutil
import subprocess
from dataclasses import replace
from datetime import datetime
from html import escape, unescape

import pytest

from brigade import fleet_command_deck as deck, fleet_hub_http, fleet_deck_pages_http
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
                    assert ("1/4 used" if case == "job" else "0/4 used") in panel
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


@pytest.mark.parametrize("width", [390, 1100, 1280])
def test_overview_machine_panel_fits_viewport(tmp_path, width):
    chromium = shutil.which("chromium") or shutil.which("chromium-browser")
    if chromium is None:
        pytest.skip("Chromium is required for the Overview layout check")
    page = deck.render_deck(report_age_view(), nonce="fixture", now=NOW)
    # Remove scripts so the geometry also proves the no-script presentation.
    page = re.sub(r"<script\b[^>]*>.*?</script>", "", page, flags=re.S)
    page = (
        f'<iframe style="width:{width}px;height:1200px;border:0" srcdoc="{escape(page, quote=True)}"></iframe>'
        + """<script>
setTimeout(function(){
 const frame=document.querySelector('iframe').contentWindow;
 const panel=frame.document.querySelector('.overview-machines');
 const result={viewport:frame.innerWidth,page:frame.document.documentElement.scrollWidth,
               panel:panel.clientWidth,content:panel.scrollWidth};
 document.body.setAttribute('data-layout-result',JSON.stringify(result));
},500);
</script>"""
    )
    source = tmp_path / "overview.html"
    source.write_text(page, encoding="utf-8")
    result = subprocess.run(
        [
            chromium,
            "--headless=new",
            "--ozone-platform=headless",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--hide-scrollbars",
            f"--user-data-dir={tmp_path / 'browser'}",
            f"--window-size={width},1200",
            "--virtual-time-budget=1000",
            "--dump-dom",
            source.as_uri(),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    match = re.search(r'data-layout-result="([^"]+)"', result.stdout)
    assert match, result.stderr
    sizes = json.loads(unescape(match[1]))
    assert sizes["viewport"] == width, sizes
    assert sizes["page"] <= sizes["viewport"], sizes
    assert sizes["content"] <= sizes["panel"], sizes


def test_report_age_survives_the_browser_soft_refresh_swap(tmp_path):
    chromium = shutil.which("chromium") or shutil.which("chromium-browser")
    if chromium is None:
        pytest.skip("Chromium is required for the Overview soft refresh check")
    refreshed = deck.render_deck(report_age_view(), nonce="fixture", now=NOW)
    initial_view = report_age_view()
    initial_view = replace(
        initial_view, stations=tuple(replace(station, last_heard=None) for station in initial_view.stations)
    )
    page = deck.render_deck(initial_view, nonce="fixture", now=NOW)
    page = re.sub(r"var SECONDS=\d+,", "var SECONDS=1,", page)
    response_body = json.dumps(refreshed).replace("<", "\\u003c")
    intercept = "<script>window.fetch=function(url,options){" + (
        "document.body.dataset.refreshRequested=options.headers['X-Fleet-Soft-Refresh'];"
        "return Promise.resolve({status:200,headers:{get:function(){return 'text/html';}},"
        f"text:function(){{return Promise.resolve({response_body});}}"
        "});};</script>"
    )
    page = page.replace("<head>", "<head>" + intercept, 1)
    page += """<script>
setTimeout(function(){
 const ages=()=>Array.from(document.querySelectorAll('.overview-machine-age'),el=>el.textContent);
 const initial=ages();
 setTimeout(function(){
  window.fleetSetAudience('agent');
  document.body.setAttribute('data-swap-result',JSON.stringify({initial:initial,final:ages(),
    requested:document.body.dataset.refreshRequested,
    working:document.querySelectorAll('.overview-machine-row')[2].textContent,
    visible:Array.from(document.querySelectorAll('.overview-machine-age')).every(el=>el.offsetHeight>0)}));
 },2000);
},100);
</script>"""
    source = tmp_path / "refresh.html"
    source.write_text(page, encoding="utf-8")
    result = subprocess.run(
        [
            chromium,
            "--headless=new",
            "--ozone-platform=headless",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            f"--user-data-dir={tmp_path / 'browser'}",
            "--window-size=1100,1200",
            "--virtual-time-budget=3000",
            "--dump-dom",
            source.as_uri(),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    match = re.search(r'data-swap-result="([^"]+)"', result.stdout)
    assert match, result.stderr
    swap = json.loads(unescape(match[1]))
    assert swap["requested"] == "1" and swap["visible"]
    assert "Last run report: never" in swap["initial"][0]
    assert "Last run report: 1h 13m" in swap["final"][0]
    assert "Last run report: never" in swap["final"][1]
    assert "Last run report" not in swap["working"] and "1/4 used" in swap["working"]
