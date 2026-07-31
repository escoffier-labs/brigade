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
.node { border: 1px solid ButtonBorder; display: block; margin: 0.5rem 0; padding: 0.5rem; text-align: left; width: 100%; }
.direction-label { font-weight: 700; }
@media (prefers-color-scheme: dark) { .node { border-color: GrayText; } }
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
<div id="map-nodes" role="tree" aria-label="Code map nodes"></div>
</section>
<aside id="map-details" aria-label="Selected node" tabindex="-1"></aside>
</div>
</section>
</main>
<script id="graphtrail-map-data" type="application/json">"###;

const HTML_SUFFIX: &str = r###"</script>
<script>
(() => {
  const source = document.getElementById('graphtrail-map-data');
  const graph = JSON.parse(source.textContent);
  const nodes = document.getElementById('map-nodes');
  const details = document.getElementById('map-details');
  const status = document.getElementById('map-status');
  document.getElementById('map-direction').textContent = graph.direction;
  if (graph.empty) {
    status.textContent = `No indexed symbols in ${graph.focus_path}.`;
  } else {
    status.textContent = `${graph.status.rendered_nodes} nodes rendered, ${graph.status.omitted_nodes} omitted; ${graph.status.rendered_edges} edges rendered, ${graph.status.omitted_edges} omitted.`;
  }
  const show = (node) => {
    details.replaceChildren();
    const heading = document.createElement('h2');
    heading.textContent = node.qualified_name;
    const location = document.createElement('p');
    location.textContent = `${node.file_path}:${node.start_line}`;
    details.append(heading, location);
    details.focus();
  };
  const buttons = graph.nodes.map((node) => {
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
    nodes.append(button);
    return button;
  });
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
