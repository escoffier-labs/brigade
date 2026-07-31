//! Static HTML rendering for bounded file-rooted call maps.

use anyhow::Result;
use rusqlite::Connection;

use super::map::{MapOptions, build_file_map};

const HTML_PREFIX: &str = r###"<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GraphTrail map</title>
<style>
:root { color-scheme: light dark; font-family: system-ui, sans-serif; }
body { margin: 0; background: Canvas; color: CanvasText; }
a:focus-visible, button:focus-visible { outline: 3px solid Highlight; outline-offset: 3px; }
.skip-link { left: 0.75rem; position: absolute; top: -3rem; }
.skip-link:focus { top: 0.75rem; }
.layout { display: grid; gap: 1rem; grid-template-columns: minmax(16rem, 1fr) minmax(18rem, 2fr); padding: 1rem; }
.lane { border-block-start: 1px solid ButtonBorder; padding-block: 0.5rem; }
.node { border: 1px solid ButtonBorder; display: block; margin: 0.5rem 0; padding: 0.5rem; text-align: left; width: 100%; }
.direction-label { font-weight: 700; }
@media (prefers-color-scheme: dark) { .lane, .node { border-color: GrayText; } }
@media (prefers-reduced-motion: reduce) { *, *::before, *::after { scroll-behavior: auto !important; transition-duration: 0.01ms !important; } }
</style>
</head>
<body>
<a class="skip-link" href="#map-main">Skip to map</a>
<header>
<p>GraphTrail</p>
<h1 id="map-title">Code map</h1>
</header>
<main id="map-main" tabindex="-1">
<section aria-labelledby="map-title">
<p id="map-status" role="status" aria-live="polite"></p>
<div class="layout">
<section aria-label="Map nodes">
<p><span class="direction-label">Direction</span>: <span id="map-direction"></span></p>
<div id="map-nodes" role="tree" aria-label="Code map nodes">
<section id="map-focus-lane" class="lane" aria-labelledby="map-focus-heading">
<h2 id="map-focus-heading">Focus file</h2>
<div id="map-focus-list"></div>
</section>
<section id="map-callers-lane" class="lane" aria-labelledby="map-callers-heading">
<h2 id="map-callers-heading">Callers (incoming relationships)</h2>
<div id="map-callers-list"></div>
</section>
<section id="map-callees-lane" class="lane" aria-labelledby="map-callees-heading">
<h2 id="map-callees-heading">Callees (outgoing relationships)</h2>
<div id="map-callees-list"></div>
</section>
<section id="map-additional-neighbors-lane" class="lane" aria-labelledby="map-additional-neighbors-heading">
<h2 id="map-additional-neighbors-heading">Additional neighbors</h2>
<div id="map-additional-neighbors-list"></div>
</section>
</div>
</section>
<aside id="map-details" aria-label="Selected node" tabindex="-1"></aside>
<section id="map-relationships-lane" class="lane" aria-labelledby="map-relationships-heading">
<h2 id="map-relationships-heading">Relationships (source → target)</h2>
<ul id="map-relationship-list"></ul>
</section>
</div>
</section>
</main>
<script id="graphtrail-map-data" type="application/json">"###;

const HTML_SUFFIX: &str = r###"</script>
<script>
(() => {
  const source = document.getElementById('graphtrail-map-data');
  const graph = JSON.parse(source.textContent);
  const details = document.getElementById('map-details');
  const status = document.getElementById('map-status');
  const focusList = document.getElementById('map-focus-list');
  const callersList = document.getElementById('map-callers-list');
  const calleesList = document.getElementById('map-callees-list');
  const additionalNeighborsList = document.getElementById('map-additional-neighbors-list');
  const relationshipList = document.getElementById('map-relationship-list');
  const buttons = [];
  document.getElementById('map-direction').textContent = graph.direction;
  if (graph.empty) {
    status.textContent = `No indexed symbols in ${graph.focus_path}.`;
  } else {
    status.textContent = `${graph.status.rendered_nodes} nodes rendered, ${graph.status.omitted_nodes} omitted; ${graph.status.rendered_edges} edges rendered, ${graph.status.omitted_edges} omitted.`;
  }
  const show = (node) => {
    const heading = document.createElement('h2');
    heading.textContent = node.qualified_name;
    const location = document.createElement('p');
    location.textContent = `${node.file_path}:${node.start_line}`;
    details.replaceChildren(heading, location);
  };
  const makeNodeButton = (node) => {
    const button = document.createElement('button');
    button.className = 'node';
    button.type = 'button';
    button.setAttribute('role', 'treeitem');
    button.textContent = `${node.qualified_name} (${node.file_path}:${node.start_line})`;
    button.addEventListener('click', () => show(node));
    button.addEventListener('keydown', (event) => {
      const index = buttons.indexOf(button);
      if (event.key === 'ArrowDown') { event.preventDefault(); buttons[(index + 1) % buttons.length].focus(); }
      if (event.key === 'ArrowUp') { event.preventDefault(); buttons[(index + buttons.length - 1) % buttons.length].focus(); }
      if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); show(node); }
    });
    buttons.push(button);
    return button;
  };
  const renderLane = (list, laneNodes, emptyMessage) => {
    if (laneNodes.length === 0) {
      const empty = document.createElement('p');
      empty.textContent = emptyMessage;
      list.replaceChildren(empty);
      return;
    }
    list.replaceChildren(...laneNodes.map(makeNodeButton));
  };
  const selectedNodeIds = new Set(graph.nodes.map((node) => node.id));
  const edges = graph.edges.filter((edge) => selectedNodeIds.has(edge.source) && selectedNodeIds.has(edge.target));
  const focusNodes = graph.nodes.filter((node) => node.file_path === graph.focus_path);
  const focusIds = new Set(focusNodes.map((node) => node.id));
  const callerIds = new Set(edges.filter((edge) => focusIds.has(edge.target)).map((edge) => edge.source));
  const calleeIds = new Set(edges.filter((edge) => focusIds.has(edge.source)).map((edge) => edge.target));
  const callers = graph.nodes.filter((node) => !focusIds.has(node.id) && callerIds.has(node.id));
  const callees = graph.nodes.filter((node) => !focusIds.has(node.id) && calleeIds.has(node.id));
  const additionalNeighbors = graph.nodes.filter((node) => !focusIds.has(node.id) && !callerIds.has(node.id) && !calleeIds.has(node.id));
  renderLane(focusList, focusNodes, 'No focus-file symbols.');
  renderLane(callersList, callers, 'No direct callers.');
  renderLane(calleesList, callees, 'No direct callees.');
  renderLane(additionalNeighborsList, additionalNeighbors, 'No additional neighbors.');
  const nodesById = new Map(graph.nodes.map((node) => [node.id, node]));
  const relationships = edges.map((edge) => {
    const source = nodesById.get(edge.source);
    const target = nodesById.get(edge.target);
    const item = document.createElement('li');
    item.textContent = `${source.qualified_name} ${edge.kind} (line ${edge.line}) → ${target.qualified_name}`;
    return item;
  });
  if (relationships.length === 0) {
    const empty = document.createElement('li');
    empty.textContent = 'No selected relationships.';
    relationshipList.replaceChildren(empty);
  } else {
    relationshipList.replaceChildren(...relationships);
  }
})();
</script>
</body>
</html>
"###;

/// Builds a self-contained static HTML document for the requested file map.
pub fn export_html_map(
    conn: &Connection,
    focus_path: &str,
    options: MapOptions,
) -> Result<String> {
    let graph = build_file_map(conn, focus_path, options)?;
    let data = serde_json::to_string(&graph)?;
    let safe_data = data
        .replace('&', "\\u0026")
        .replace('<', "\\u003c")
        .replace('>', "\\u003e");

    Ok(format!("{HTML_PREFIX}{safe_data}{HTML_SUFFIX}"))
}
