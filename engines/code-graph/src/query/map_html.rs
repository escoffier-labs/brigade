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
a:focus-visible, button:focus-visible, [tabindex]:focus-visible { outline: 3px solid Highlight; outline-offset: 3px; }
.skip-link { left: 0.75rem; position: absolute; top: -3rem; }
.skip-link:focus { top: 0.75rem; }
.layout { display: grid; gap: 1rem; grid-template-columns: minmax(16rem, 1fr) minmax(18rem, 2fr); padding: 1rem; }
.lane { border-block-start: 1px solid ButtonBorder; padding-block: 0.5rem; }
.node { border: 1px solid ButtonBorder; display: block; margin: 0.5rem 0; padding: 0.5rem; text-align: left; width: 100%; }
.direction-label { font-weight: 700; }
.map-view-toggle, .graph-controls, #map-graph-legend { display: flex; flex-wrap: wrap; gap: 0.5rem; margin-block: 0.5rem; }
.graph-controls button, .map-view-toggle button { border: 1px solid ButtonBorder; border-radius: 0.3rem; padding: 0.4rem 0.65rem; }
#map-graph-view { grid-column: 1 / -1; min-inline-size: 0; }
#map-graph { background: color-mix(in srgb, Canvas 92%, CanvasText); block-size: min(74vh, 52rem); border: 1px solid ButtonBorder; border-radius: 0.4rem; inline-size: 100%; touch-action: none; }
.graph-edge { stroke: color-mix(in srgb, CanvasText 55%, Canvas); stroke-width: 1.35; vector-effect: non-scaling-stroke; }
.graph-node circle { stroke-width: 2; vector-effect: non-scaling-stroke; }
.graph-node text { fill: CanvasText; font-size: 12px; paint-order: stroke; stroke: Canvas; stroke-width: 3px; stroke-linejoin: round; }
.graph-node--focus circle, .graph-legend-swatch--focus { fill: #005a9c; stroke: #003f6b; }
.graph-node--caller circle, .graph-legend-swatch--caller { fill: #a64000; stroke: #702b00; }
.graph-node--callee circle, .graph-legend-swatch--callee { fill: #007a65; stroke: #005443; }
.graph-node--additional circle, .graph-legend-swatch--additional { fill: #654090; stroke: #472868; }
.graph-node--focus circle { stroke-width: 4; }
.graph-legend-swatch { border: 1px solid ButtonBorder; display: inline-block; height: 0.8rem; margin-inline-end: 0.25rem; vertical-align: middle; width: 0.8rem; }
.graph-legend-swatch--focus { background-color: #005a9c; border-color: #003f6b; }
.graph-legend-swatch--caller { background-color: #a64000; border-color: #702b00; }
.graph-legend-swatch--callee { background-color: #007a65; border-color: #005443; }
.graph-legend-swatch--additional { background-color: #654090; border-color: #472868; }
#graphtrail-arrowhead path { fill: color-mix(in srgb, CanvasText 55%, Canvas); }
.graph-label { display: none; }
.graph-label--focus, .graph-node:hover .graph-label, .graph-node:focus-within .graph-label, #map-graph.graph-labels-expanded .graph-label { display: block; }
.graph-edge.is-neighbor, .graph-node.is-neighbor { opacity: 1; }
.graph-edge.is-muted, .graph-node.is-muted { opacity: 0.25; }
@media (prefers-color-scheme: dark) { .lane, .node { border-color: GrayText; } }
@media (prefers-reduced-motion: reduce) { *, *::before, *::after { scroll-behavior: auto !important; transition-duration: 0.01ms !important; } }
@media (prefers-reduced-motion: reduce) { #map-graph { transition: none; } }
@media (max-width: 48rem) { .layout { grid-template-columns: 1fr; } #map-graph { block-size: 64vh; } }
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
<div class="map-view-toggle" role="group" aria-label="Map view">
<button id="map-list-view" type="button" aria-pressed="true">List</button>
<button id="map-graph-view-toggle" type="button" aria-pressed="false">Graph</button>
</div>
<div class="layout">
<section id="map-list-panel" aria-label="Map nodes">
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
<section id="map-graph-view" hidden aria-labelledby="map-graph-heading">
<h2 id="map-graph-heading">Graph view</h2>
<p id="map-graph-status" role="status" aria-live="polite"></p>
<div id="map-graph-legend" aria-label="Graph legend">
<span><span class="graph-legend-swatch graph-legend-swatch--focus" aria-hidden="true"></span>Focus file</span>
<span><span class="graph-legend-swatch graph-legend-swatch--caller" aria-hidden="true"></span>Caller</span>
<span><span class="graph-legend-swatch graph-legend-swatch--callee" aria-hidden="true"></span>Callee</span>
<span><span class="graph-legend-swatch graph-legend-swatch--additional" aria-hidden="true"></span>Additional neighbor</span>
</div>
<div id="map-graph-controls" class="graph-controls" role="group" aria-label="Graph controls" tabindex="0">
<button id="map-graph-zoom-in" type="button">Zoom in</button>
<button id="map-graph-zoom-out" type="button">Zoom out</button>
<button id="map-graph-reset" type="button">Reset view</button>
</div>
<svg id="map-graph" role="img" aria-labelledby="map-graph-heading map-graph-description" viewBox="0 0 960 640" tabindex="0">
<desc id="map-graph-description">Interactive call graph. Use the controls or keyboard to pan, zoom, and reset.</desc>
<defs><marker id="graphtrail-arrowhead" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M 0 0 L 8 4 L 0 8 z"></path></marker></defs>
<g id="map-graph-pan-layer"><g id="map-graph-edge-layer"></g><g id="map-graph-node-layer"></g></g>
</svg>
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
  const listView = document.getElementById('map-list-panel');
  const listToggle = document.getElementById('map-list-view');
  const graphToggle = document.getElementById('map-graph-view-toggle');
  const graphView = document.getElementById('map-graph-view');
  const graphStatus = document.getElementById('map-graph-status');
  const graphControls = document.getElementById('map-graph-controls');
  const svg = document.getElementById('map-graph');
  const panLayer = document.getElementById('map-graph-pan-layer');
  const edgeLayer = document.getElementById('map-graph-edge-layer');
  const nodeLayer = document.getElementById('map-graph-node-layer');
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
    const command = document.createElement('code');
    command.textContent = `graphtrail map ${node.file_path} --out graphtrail-map.html`;
    details.replaceChildren(heading, location, command);
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
  const viewBox = { width: 960, height: 640 };
  const layoutSeed = (id) => {
    let hash = 0;
    for (let index = 0; index < id.length; index += 1) {
      hash = ((hash << 5) - hash + id.charCodeAt(index)) | 0;
    }
    return hash >>> 0;
  };
  const seededPosition = (id) => {
    const angle = (layoutSeed(id) / 0x100000000) * Math.PI * 2;
    const radius = 120 + (layoutSeed(`${id}:radius`) % 180);
    return { x: viewBox.width / 2 + Math.cos(angle) * radius, y: viewBox.height / 2 + Math.sin(angle) * radius };
  };
  const compareStableText = (left, right) => left < right ? -1 : left > right ? 1 : 0;
  const layoutNodes = [...graph.nodes].sort((left, right) => compareStableText(left.id, right.id));
  const edgeKey = (edge) => `${edge.source}:${edge.target}:${edge.kind}:${edge.line}`;
  const layoutEdges = [...edges].sort((left, right) => compareStableText(edgeKey(left), edgeKey(right)));
  const positions = new Map(layoutNodes.map((node) => [node.id, seededPosition(node.id)]));
  const forceIterations = 160;
  const settlePositions = () => {
    for (let leftIndex = 0; leftIndex < layoutNodes.length; leftIndex += 1) {
      const leftPosition = positions.get(layoutNodes[leftIndex].id);
      for (let rightIndex = leftIndex + 1; rightIndex < layoutNodes.length; rightIndex += 1) {
        const rightPosition = positions.get(layoutNodes[rightIndex].id);
        const dx = rightPosition.x - leftPosition.x;
        const dy = rightPosition.y - leftPosition.y;
        const distanceSquared = Math.max(dx * dx + dy * dy, 1);
        const repulsion = 1800 / distanceSquared;
        leftPosition.x -= dx * repulsion;
        leftPosition.y -= dy * repulsion;
        rightPosition.x += dx * repulsion;
        rightPosition.y += dy * repulsion;
      }
    }
    for (const edge of layoutEdges) {
      const sourcePosition = positions.get(edge.source);
      const targetPosition = positions.get(edge.target);
      const dx = targetPosition.x - sourcePosition.x;
      const dy = targetPosition.y - sourcePosition.y;
      const distance = Math.max(Math.hypot(dx, dy), 1);
      const desiredLength = 140;
      const spring = (distance - desiredLength) * 0.012;
      sourcePosition.x += (dx / distance) * spring;
      sourcePosition.y += (dy / distance) * spring;
      targetPosition.x -= (dx / distance) * spring;
      targetPosition.y -= (dy / distance) * spring;
    }
  };
  for (let iteration = 0; iteration < forceIterations; iteration += 1) {
    settlePositions();
  }
  const focusPosition = { x: viewBox.width / 2, y: viewBox.height / 2 };
  const primaryFocusNode = focusNodes[0];
  if (primaryFocusNode) {
    const primaryFocusPosition = positions.get(primaryFocusNode.id);
    const focusOffset = { x: focusPosition.x - primaryFocusPosition.x, y: focusPosition.y - primaryFocusPosition.y };
    for (const position of positions.values()) {
      position.x += focusOffset.x;
      position.y += focusOffset.y;
    }
    if (primaryFocusNode) positions.set(primaryFocusNode.id, focusPosition);
  }
  const layoutMargin = 48;
  const maxDistanceX = Math.max(...[...positions.values()].map((position) => Math.abs(position.x - focusPosition.x)), 1);
  const maxDistanceY = Math.max(...[...positions.values()].map((position) => Math.abs(position.y - focusPosition.y)), 1);
  const layoutScale = Math.min(1, (viewBox.width / 2 - layoutMargin) / maxDistanceX, (viewBox.height / 2 - layoutMargin) / maxDistanceY);
  for (const position of positions.values()) {
    position.x = focusPosition.x + (position.x - focusPosition.x) * layoutScale;
    position.y = focusPosition.y + (position.y - focusPosition.y) * layoutScale;
  }
  const positionRepresentation = graph.nodes.map((node) => ({ id: node.id, x: positions.get(node.id).x, y: positions.get(node.id).y }));
  const degreeById = new Map(graph.nodes.map((node) => [node.id, 0]));
  for (const edge of edges) {
    degreeById.set(edge.source, (degreeById.get(edge.source) || 0) + 1);
    degreeById.set(edge.target, (degreeById.get(edge.target) || 0) + 1);
  }
  const nodeClass = (node) => {
    if (node.file_path === graph.focus_path) return 'graph-node graph-node--focus';
    if (callerIds.has(node.id)) return 'graph-node graph-node--caller';
    if (calleeIds.has(node.id)) return 'graph-node graph-node--callee';
    return 'graph-node graph-node--additional';
  };
  let transform = { x: 0, y: 0, scale: 1 };
  let panStart = null;
  const labelZoomThreshold = 1.5;
  const updateTransform = () => {
    panLayer.setAttribute('transform', `translate(${transform.x} ${transform.y}) scale(${transform.scale})`);
    svg.classList.toggle('graph-labels-expanded', transform.scale >= labelZoomThreshold);
  };
  const clearNeighborHighlight = () => {
    for (const element of svg.querySelectorAll('.is-neighbor, .is-muted')) {
      element.classList.remove('is-neighbor', 'is-muted');
    }
  };
  const highlightNeighbors = (nodeId) => {
    const neighborIds = new Set([nodeId]);
    for (const edge of edges) {
      if (edge.source === nodeId) neighborIds.add(edge.target);
      if (edge.target === nodeId) neighborIds.add(edge.source);
    }
    for (const nodeElement of svg.querySelectorAll('.graph-node')) {
      const isNeighbor = neighborIds.has(nodeElement.dataset.nodeId);
      nodeElement.classList.toggle('is-neighbor', isNeighbor);
      nodeElement.classList.toggle('is-muted', !isNeighbor);
    }
    for (const edgeElement of svg.querySelectorAll('.graph-edge')) {
      const isNeighbor = edgeElement.dataset.source === nodeId || edgeElement.dataset.target === nodeId;
      edgeElement.classList.toggle('is-neighbor', isNeighbor);
      edgeElement.classList.toggle('is-muted', !isNeighbor);
    }
  };
  const renderGraph = () => {
    edgeLayer.replaceChildren();
    nodeLayer.replaceChildren();
    for (const edge of edges) {
      const sourcePosition = positions.get(edge.source);
      const targetPosition = positions.get(edge.target);
      const line = document.createElementNS(svg.namespaceURI, 'line');
      line.classList.add('graph-edge');
      line.dataset.source = edge.source;
      line.dataset.target = edge.target;
      line.setAttribute('marker-end', 'url(#graphtrail-arrowhead)');
      line.setAttribute('x1', String(sourcePosition.x));
      line.setAttribute('y1', String(sourcePosition.y));
      line.setAttribute('x2', String(targetPosition.x));
      line.setAttribute('y2', String(targetPosition.y));
      const title = document.createElementNS(svg.namespaceURI, 'title');
      title.textContent = `${edge.kind} (line ${edge.line})`;
      line.append(title);
      edgeLayer.append(line);
    }
    for (const node of graph.nodes) {
      const position = positions.get(node.id);
      const nodeGroup = document.createElementNS(svg.namespaceURI, 'g');
      nodeGroup.setAttribute('class', nodeClass(node));
      nodeGroup.dataset.nodeId = node.id;
      nodeGroup.setAttribute('tabindex', '0');
      nodeGroup.setAttribute('role', 'button');
      const label = node.qualified_name || node.name;
      nodeGroup.setAttribute('aria-label', `${label}, ${node.file_path}:${node.start_line}`);
      nodeGroup.setAttribute('transform', `translate(${position.x} ${position.y})`);
      const circle = document.createElementNS(svg.namespaceURI, 'circle');
      const degree = degreeById.get(node.id) || 0;
      const degreeRadius = 6 + Math.min(degree, 8);
      const radius = node.id === primaryFocusNode?.id ? Math.max(16, degreeRadius) : degreeRadius;
      circle.setAttribute('r', String(radius));
      const text = document.createElementNS(svg.namespaceURI, 'text');
      const maxLabelLength = 36;
      const displayLabel = label.length > maxLabelLength ? label.slice(0, maxLabelLength - 1) + '…' : label;
      text.setAttribute('x', String(radius + 5));
      text.setAttribute('y', '4');
      text.textContent = displayLabel;
      text.classList.add('graph-label');
      if (node.file_path === graph.focus_path) text.classList.add('graph-label--focus');
      nodeGroup.append(circle, text);
      nodeGroup.addEventListener('click', () => show(node));
      nodeGroup.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); show(node); }
      });
      nodeGroup.addEventListener('pointerenter', () => highlightNeighbors(node.id));
      nodeGroup.addEventListener('focusin', () => highlightNeighbors(node.id));
      nodeGroup.addEventListener('pointerleave', clearNeighborHighlight);
      nodeGroup.addEventListener('focusout', clearNeighborHighlight);
      nodeLayer.append(nodeGroup);
    }
    updateTransform();
  };
  const resetGraphView = () => { transform = { x: 0, y: 0, scale: 1 }; updateTransform(); };
  const beginPan = (event) => { panStart = { x: event.clientX - transform.x, y: event.clientY - transform.y }; svg.setPointerCapture(event.pointerId); };
  const zoomGraph = (event) => { event.preventDefault(); transform.scale = Math.max(0.5, Math.min(3, transform.scale + (event.deltaY < 0 ? 0.1 : -0.1))); updateTransform(); };
  const zoomIn = () => { transform.scale = Math.min(3, transform.scale + 0.1); updateTransform(); };
  const zoomOut = () => { transform.scale = Math.max(0.5, transform.scale - 0.1); updateTransform(); };
  svg.addEventListener('pointerdown', beginPan);
  svg.addEventListener('wheel', zoomGraph, { passive: false });
  svg.addEventListener('pointermove', (event) => { if (panStart) { transform.x = event.clientX - panStart.x; transform.y = event.clientY - panStart.y; updateTransform(); } });
  svg.addEventListener('pointerup', () => { panStart = null; });
  svg.addEventListener('pointercancel', () => { panStart = null; });
  graphControls.addEventListener('keydown', (event) => {
    if (event.key === '+' || event.key === '=') { event.preventDefault(); transform.scale = Math.min(3, transform.scale + 0.1); updateTransform(); }
    if (event.key === '-') { event.preventDefault(); transform.scale = Math.max(0.5, transform.scale - 0.1); updateTransform(); }
    if (event.key === '0') { event.preventDefault(); resetGraphView(); }
    if (event.key === 'ArrowLeft') { event.preventDefault(); transform.x -= 20; updateTransform(); }
    if (event.key === 'ArrowRight') { event.preventDefault(); transform.x += 20; updateTransform(); }
    if (event.key === 'ArrowUp') { event.preventDefault(); transform.y -= 20; updateTransform(); }
    if (event.key === 'ArrowDown') { event.preventDefault(); transform.y += 20; updateTransform(); }
  });
  document.getElementById('map-graph-zoom-in').addEventListener('click', zoomIn);
  document.getElementById('map-graph-zoom-out').addEventListener('click', zoomOut);
  document.getElementById('map-graph-reset').addEventListener('click', resetGraphView);
  const setGraphView = (showGraph) => {
    graphView.hidden = !showGraph;
    listView.hidden = showGraph;
    listToggle.setAttribute('aria-pressed', String(!showGraph));
    graphToggle.setAttribute('aria-pressed', String(showGraph));
  };
  listToggle.addEventListener('click', () => setGraphView(false));
  graphToggle.addEventListener('click', () => setGraphView(true));
  graphStatus.textContent = `Graph view: ${graph.status.rendered_nodes} nodes rendered, ${graph.status.omitted_nodes} omitted; ${graph.status.rendered_edges} edges rendered, ${graph.status.omitted_edges} omitted.`;
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) { renderGraph(); return; }
  renderGraph();
})();
</script>
</body>
</html>
"###;

/// Builds a self-contained static HTML document for the requested file map.
pub fn export_html_map(conn: &Connection, focus_path: &str, options: MapOptions) -> Result<String> {
    let graph = build_file_map(conn, focus_path, options)?;
    let data = serde_json::to_string(&graph)?;
    let safe_data = data
        .replace('&', "\\u0026")
        .replace('<', "\\u003c")
        .replace('>', "\\u003e");

    Ok(format!("{HTML_PREFIX}{safe_data}{HTML_SUFFIX}"))
}
