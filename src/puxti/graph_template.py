"""Self-contained HTML for `puxti graph`.

The page (CSS + a dependency-free force-directed layout, all inline) is kept as a
Python string constant so it ships in the wheel and sdist with no packaging change —
same rationale as `agent_skill.py`'s SKILL_MD. `render_graph_html` injects the graph
payload at the `__PUXTI_DATA__` placeholder.

Design ported from the internal `kg-explore.html` mockup: brand palette, type-colored
nodes, legend, entity list, entity-detail card, and definition-history timeline.
"""

from __future__ import annotations

import json
from typing import Any

GRAPH_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>Puxti - Knowledge Graph</title>
<style>
  :root {
    --deep-blue:#1A2B4E; --azure:#39B4E3; --coral:#FF6F61; --green:#22C5A0;
    --amber:#F59E0B; --purple:#7C3AED;
    --white:#fff; --grey-50:#F8FAFC; --grey-100:#F1F5F9; --grey-200:#E2E8F0;
    --grey-400:#94A3B8; --grey-600:#475569; --grey-800:#1E293B;
  }
  * { box-sizing:border-box; margin:0; padding:0; }
  html, body { height:100%; }
  body {
    font-family:system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
    color:var(--grey-800); background:var(--grey-50);
    display:flex; flex-direction:column; height:100vh; overflow:hidden;
  }
  header {
    background:var(--deep-blue); color:#fff; padding:12px 20px;
    display:flex; align-items:center; gap:16px; flex-shrink:0;
  }
  header .brand { font-size:16px; font-weight:800; letter-spacing:-.02em; }
  header .meta { font-size:12px; color:rgba(255,255,255,.6); }
  header .spacer { flex:1; }
  header input {
    font:inherit; font-size:13px; padding:6px 10px; border-radius:7px;
    border:1px solid rgba(255,255,255,.15); background:rgba(255,255,255,.08);
    color:#fff; width:220px;
  }
  header input::placeholder { color:rgba(255,255,255,.45); }
  .legend { display:flex; gap:14px; flex-wrap:wrap; padding:8px 20px;
    background:var(--white); border-bottom:1px solid var(--grey-200); flex-shrink:0; }
  .legend-item { display:flex; align-items:center; gap:5px; font-size:11px; color:var(--grey-600); }
  .legend-dot { width:11px; height:11px; border-radius:3px; border:1.5px solid; }
  main { flex:1; display:grid; grid-template-columns:1fr 340px; overflow:hidden; }
  .graph-wrap { position:relative; overflow:hidden; background:
    radial-gradient(circle at 1px 1px, var(--grey-200) 1px, transparent 0) 0 0 / 22px 22px; }
  svg { width:100%; height:100%; cursor:grab; }
  svg.grabbing { cursor:grabbing; }
  .edge { fill:none; }
  .edge--lineage { stroke:#CBD5E1; stroke-width:1.5; }
  .edge--semantic { stroke:var(--azure); stroke-width:1.5; stroke-dasharray:5 3; opacity:.75; }
  .node { cursor:pointer; }
  .node rect { transition:filter .15s; }
  .node:hover rect { filter:brightness(.96); }
  .node.selected rect { stroke-width:2.5 !important; filter:drop-shadow(0 0 7px rgba(57,180,227,.5)); }
  .node.dim { opacity:.2; }
  .node text { pointer-events:none; user-select:none; }
  .panel { background:var(--white); border-left:1px solid var(--grey-200);
    overflow-y:auto; display:flex; flex-direction:column; }
  .panel-section { padding:16px 18px; border-bottom:1px solid var(--grey-100); }
  .panel-title { font-size:10px; font-weight:700; letter-spacing:.08em; text-transform:uppercase;
    color:var(--grey-400); margin-bottom:10px; }
  .entity-row { display:flex; align-items:center; gap:9px; padding:8px 6px; border-radius:7px;
    cursor:pointer; transition:background .1s;
    width:100%; border:0; background:transparent; font:inherit; color:inherit; text-align:left; }
  .entity-row:focus-visible { outline:2px solid var(--azure); outline-offset:1px; }
  .node:focus { outline:none; }
  .node:focus-visible rect { stroke:var(--azure) !important; stroke-width:2.5 !important; }
  .entity-row:hover { background:var(--grey-50); }
  .entity-row.active { background:rgba(57,180,227,.08); }
  .entity-row__dot { width:9px; height:9px; border-radius:3px; border:1.5px solid; flex-shrink:0; }
  .entity-row__name { font-size:13px; font-weight:600; flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .entity-row__type { font-size:10px; color:var(--grey-400); }
  .badge { display:inline-flex; align-items:center; font-size:10px; font-weight:700;
    padding:2px 8px; border-radius:999px; }
  .badge--proposed { background:rgba(245,158,11,.14); color:var(--amber); }
  .badge--type { background:var(--grey-100); color:var(--grey-600); }
  .detail-name { font-size:18px; font-weight:800; color:var(--deep-blue); letter-spacing:-.01em; }
  .detail-id { font-size:11px; color:var(--grey-400); font-family:ui-monospace, monospace; word-break:break-all; margin-top:2px; }
  .def-text { font-size:13px; line-height:1.65; color:var(--grey-800);
    background:rgba(57,180,227,.05); border-left:3px solid var(--azure);
    padding:12px 14px; border-radius:0 8px 8px 0; }
  .def-meta { font-size:11px; color:var(--grey-400); margin-top:8px; }
  .edge-line { font-size:12px; padding:6px 0; border-bottom:1px solid var(--grey-100); display:flex; gap:6px; align-items:baseline; }
  .edge-line:last-child { border-bottom:none; }
  .edge-line__rel { font-size:9px; font-weight:700; text-transform:uppercase; letter-spacing:.05em;
    color:var(--azure); flex-shrink:0; }
  .edge-line__name { font-weight:600; color:var(--grey-800); }
  .muted { font-size:12px; color:var(--grey-400); }
  .tl-entry { display:flex; gap:10px; }
  .tl-spine { display:flex; flex-direction:column; align-items:center; width:16px; flex-shrink:0; }
  .tl-node { width:11px; height:11px; border-radius:50%; border:2px solid var(--grey-200); background:#fff; margin-top:3px; }
  .tl-node--current { border-color:var(--azure); background:var(--azure); }
  .tl-line { width:2px; flex:1; background:var(--grey-200); margin:3px 0; min-height:12px; }
  .tl-body { flex:1; padding-bottom:16px; }
  .tl-ver { font-size:10px; font-weight:700; text-transform:uppercase; letter-spacing:.06em; color:var(--grey-400); margin-bottom:4px; }
  .tl-def { font-size:12px; line-height:1.55; color:var(--grey-800); }
  .tl-by { font-size:10px; color:var(--grey-400); margin-top:4px; }
  .empty { position:absolute; inset:0; display:flex; flex-direction:column; align-items:center;
    justify-content:center; gap:8px; color:var(--grey-400); font-size:14px; text-align:center; padding:24px; }
</style>
</head>
<body>
<header>
  <span class="brand">Puxti</span>
  <span class="meta" id="hdr-meta"></span>
  <span class="spacer"></span>
  <input id="search" type="search" placeholder="Filter entities..." autocomplete="off" />
</header>
<div class="legend" id="legend"></div>
<main>
  <div class="graph-wrap">
    <svg id="svg"><g id="viewport"><g id="edges"></g><g id="nodes"></g></g></svg>
    <div class="empty" id="empty" style="display:none">
      <div style="font-size:26px">/</div>
      <div>The knowledge graph is empty.</div>
      <div class="muted">Run <code>puxti scan</code> to populate it, then re-run <code>puxti graph</code>.</div>
    </div>
  </div>
  <aside class="panel" id="panel"></aside>
</main>
<script>
const DATA = __PUXTI_DATA__;

const SVGNS = "http://www.w3.org/2000/svg";
const TYPE_STYLE = {
  source:    {fill:"#F1F5F9", stroke:"#94A3B8", label:"Source"},
  table:     {fill:"#F1F5F9", stroke:"#94A3B8", label:"Table"},
  model:     {fill:"rgba(57,180,227,.10)", stroke:"#39B4E3", label:"dbt model"},
  view:      {fill:"rgba(124,58,237,.10)", stroke:"#7C3AED", label:"View"},
  dag:       {fill:"rgba(245,158,11,.12)", stroke:"#F59E0B", label:"DAG"},
  task:      {fill:"rgba(245,158,11,.10)", stroke:"#F59E0B", label:"Task"},
  dashboard: {fill:"rgba(34,197,160,.14)", stroke:"#22C5A0", label:"Dashboard"},
  metric:    {fill:"rgba(26,43,78,.10)", stroke:"#1A2B4E", label:"Metric"},
};
const PROPOSED = {fill:"rgba(245,158,11,.10)", stroke:"#F59E0B", label:"Proposed (unbound)"};
function styleFor(n) {
  if (n.status === "proposed") return PROPOSED;
  return TYPE_STYLE[n.type] || {fill:"#F1F5F9", stroke:"#94A3B8", label:n.type};
}

const nodes = DATA.nodes.map(n => Object.assign({}, n, {x:0, y:0, vx:0, vy:0, fx:null, fy:null}));
const byId = new Map(nodes.map(n => [n.id, n]));
const links = DATA.edges.filter(e => byId.has(e.source) && byId.has(e.target))
  .map(e => Object.assign({}, e, {s: byId.get(e.source), t: byId.get(e.target)}));

document.getElementById("hdr-meta").textContent =
  nodes.length + " entities · " + links.length + " relationships" +
  (DATA.project ? " · " + DATA.project : "");

// Legend: only the types actually present, plus proposed if any.
(function buildLegend() {
  const present = new Set(nodes.map(n => n.status === "proposed" ? "__proposed" : n.type));
  const el = document.getElementById("legend");
  const items = [];
  Object.keys(TYPE_STYLE).forEach(t => { if (present.has(t)) items.push(TYPE_STYLE[t]); });
  if (present.has("__proposed")) items.push(PROPOSED);
  items.push({fill:"none", stroke:"#CBD5E1", label:"Lineage", line:true});
  items.push({fill:"none", stroke:"#39B4E3", label:"Semantic", line:true, dash:true});
  el.innerHTML = "";
  items.forEach(it => {
    const d = document.createElement("div"); d.className = "legend-item";
    const dot = document.createElement("span"); dot.className = "legend-dot";
    dot.style.background = it.fill; dot.style.borderColor = it.stroke;
    if (it.line) { dot.style.height = "0"; dot.style.width = "16px"; dot.style.borderRadius = "0";
      dot.style.borderTop = (it.dash ? "2px dashed " : "2px solid ") + it.stroke; dot.style.borderLeft = dot.style.borderRight = dot.style.borderBottom = "none"; }
    d.appendChild(dot);
    d.appendChild(document.createTextNode(it.label));
    el.appendChild(d);
  });
})();

const svg = document.getElementById("svg");
const gEdges = document.getElementById("edges");
const gNodes = document.getElementById("nodes");

function nodeWidth(n) { return Math.max(84, n.name.length * 7 + 26); }
const NODE_H = 34;

// Build DOM for nodes and edges.
const edgeEls = links.map(l => {
  const p = document.createElementNS(SVGNS, "line");
  p.setAttribute("class", "edge edge--" + (l.kind === "semantic" ? "semantic" : "lineage"));
  p.setAttribute("marker-end", "url(#arr)");
  gEdges.appendChild(p);
  return p;
});
const defs = document.createElementNS(SVGNS, "defs");
defs.innerHTML = '<marker id="arr" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto"><polygon points="0 0,8 3,0 6" fill="#CBD5E1"/></marker>';
svg.insertBefore(defs, svg.firstChild);

const nodeEls = nodes.map(n => {
  const st = styleFor(n);
  const w = nodeWidth(n);
  const g = document.createElementNS(SVGNS, "g");
  g.setAttribute("class", "node");
  g.setAttribute("tabindex", "0");
  g.setAttribute("role", "button");
  g.setAttribute("aria-label", n.name + " (" + (n.status === "proposed" ? "proposed" : n.type) + ")");
  const rect = document.createElementNS(SVGNS, "rect");
  rect.setAttribute("width", w); rect.setAttribute("height", NODE_H);
  rect.setAttribute("x", -w / 2); rect.setAttribute("y", -NODE_H / 2);
  rect.setAttribute("rx", 7);
  rect.setAttribute("fill", st.fill); rect.setAttribute("stroke", st.stroke);
  rect.setAttribute("stroke-width", "1.5");
  if (n.status === "proposed") rect.setAttribute("stroke-dasharray", "4 2");
  const label = document.createElementNS(SVGNS, "text");
  label.setAttribute("text-anchor", "middle"); label.setAttribute("y", 4);
  label.setAttribute("font-size", "12"); label.setAttribute("font-weight", "700");
  label.setAttribute("fill", "#1E293B");
  label.textContent = n.name;
  g.appendChild(rect); g.appendChild(label);
  g.addEventListener("click", (ev) => { ev.stopPropagation(); select(n.id); });
  g.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); select(n.id); }
  });
  attachDrag(g, n);
  gNodes.appendChild(g);
  n._w = w;
  return g;
});

if (!nodes.length) document.getElementById("empty").style.display = "flex";

// ---- Force simulation ---------------------------------------------------
const W = () => svg.clientWidth || 800;
const H = () => svg.clientHeight || 600;
// Seed on a circle so the layout unfolds deterministically.
nodes.forEach((n, i) => {
  const a = (i / Math.max(1, nodes.length)) * Math.PI * 2;
  n.x = W() / 2 + Math.cos(a) * 180;
  n.y = H() / 2 + Math.sin(a) * 180;
});
let alpha = 1;
function tick() {
  const cx = W() / 2, cy = H() / 2;
  // Repulsion (pairwise).
  for (let i = 0; i < nodes.length; i++) {
    for (let j = i + 1; j < nodes.length; j++) {
      const a = nodes[i], b = nodes[j];
      let dx = a.x - b.x, dy = a.y - b.y;
      let d2 = dx * dx + dy * dy || 1;
      const f = 5200 / d2;
      const d = Math.sqrt(d2);
      const ux = dx / d, uy = dy / d;
      a.vx += ux * f; a.vy += uy * f;
      b.vx -= ux * f; b.vy -= uy * f;
    }
  }
  // Link springs.
  links.forEach(l => {
    let dx = l.t.x - l.s.x, dy = l.t.y - l.s.y;
    const d = Math.sqrt(dx * dx + dy * dy) || 1;
    const f = (d - 150) * 0.02;
    const ux = dx / d, uy = dy / d;
    l.s.vx += ux * f; l.s.vy += uy * f;
    l.t.vx -= ux * f; l.t.vy -= uy * f;
  });
  // Centering + integrate.
  nodes.forEach(n => {
    n.vx += (cx - n.x) * 0.002;
    n.vy += (cy - n.y) * 0.002;
    if (n.fx !== null) { n.x = n.fx; n.y = n.fy; n.vx = 0; n.vy = 0; return; }
    n.vx *= 0.85; n.vy *= 0.85;
    n.x += n.vx * alpha; n.y += n.vy * alpha;
  });
  alpha *= 0.992;
  render();
  if (alpha > 0.02) requestAnimationFrame(tick);
}
function render() {
  nodes.forEach((n, i) => nodeEls[i].setAttribute("transform", "translate(" + n.x + "," + n.y + ")"));
  links.forEach((l, i) => {
    const e = edgeEls[i];
    e.setAttribute("x1", l.s.x); e.setAttribute("y1", l.s.y);
    e.setAttribute("x2", l.t.x); e.setAttribute("y2", l.t.y);
  });
}
if (nodes.length) requestAnimationFrame(tick);

function reheat() {
  const stopped = alpha <= 0.02;
  alpha = Math.max(alpha, 0.6);
  if (stopped) requestAnimationFrame(tick);  // avoid stacking overlapping tick chains
}

// ---- Drag ---------------------------------------------------------------
function attachDrag(g, n) {
  let dragging = false;
  g.addEventListener("pointerdown", (ev) => {
    dragging = true; g.setPointerCapture(ev.pointerId);
    ev.stopPropagation();
  });
  g.addEventListener("pointermove", (ev) => {
    if (!dragging) return;
    const p = toWorld(ev.clientX, ev.clientY);
    n.fx = p.x; n.fy = p.y; n.x = p.x; n.y = p.y;
    reheat();
  });
  g.addEventListener("pointerup", (ev) => {
    dragging = false; g.releasePointerCapture(ev.pointerId);
    n.fx = null; n.fy = null;
  });
}

// ---- Pan / zoom ---------------------------------------------------------
let view = {x:0, y:0, k:1};
const vp = document.getElementById("viewport");
function applyView() { vp.setAttribute("transform", "translate(" + view.x + "," + view.y + ") scale(" + view.k + ")"); }
function toWorld(clientX, clientY) {
  const r = svg.getBoundingClientRect();
  return {x: (clientX - r.left - view.x) / view.k, y: (clientY - r.top - view.y) / view.k};
}
svg.addEventListener("wheel", (ev) => {
  ev.preventDefault();
  const r = svg.getBoundingClientRect();
  const mx = ev.clientX - r.left, my = ev.clientY - r.top;
  const factor = ev.deltaY < 0 ? 1.1 : 1 / 1.1;
  const nk = Math.min(3, Math.max(0.25, view.k * factor));
  view.x = mx - (mx - view.x) * (nk / view.k);
  view.y = my - (my - view.y) * (nk / view.k);
  view.k = nk; applyView();
}, {passive:false});
let panning = false, panStart = null;
svg.addEventListener("pointerdown", (ev) => { panning = true; panStart = {x:ev.clientX - view.x, y:ev.clientY - view.y}; svg.classList.add("grabbing"); });
svg.addEventListener("pointermove", (ev) => { if (!panning) return; view.x = ev.clientX - panStart.x; view.y = ev.clientY - panStart.y; applyView(); });
window.addEventListener("pointerup", () => { panning = false; svg.classList.remove("grabbing"); });
svg.addEventListener("click", () => select(null));

// ---- Selection / detail panel ------------------------------------------
let selectedId = null;
function select(id) {
  selectedId = id;
  nodeEls.forEach((g, i) => {
    g.classList.toggle("selected", nodes[i].id === id);
  });
  renderPanel();
}
function esc(s) { const d = document.createElement("div"); d.textContent = s == null ? "" : String(s); return d.innerHTML; }
function typeBadge(n) { return '<span class="badge badge--type">' + esc(n.type) + '</span>'; }

function renderPanel() {
  const panel = document.getElementById("panel");
  if (!selectedId) { renderList(panel); return; }
  const n = byId.get(selectedId);
  const outs = links.filter(l => l.source === n.id);
  const ins = links.filter(l => l.target === n.id);
  let html = '<div class="panel-section">';
  html += '<div class="detail-name">' + esc(n.name) + '</div>';
  html += '<div style="margin-top:6px;display:flex;gap:6px;flex-wrap:wrap;">' + typeBadge(n);
  if (n.status === "proposed") html += '<span class="badge badge--proposed">proposed</span>';
  html += '</div>';
  html += '<div class="detail-id">' + esc(n.id) + '</div></div>';

  html += '<div class="panel-section"><div class="panel-title">Definition</div>';
  if (n.definition) {
    html += '<div class="def-text">' + esc(n.definition.description) + '</div>';
    html += '<div class="def-meta">v' + esc(n.definition.version) + ' · by ' +
      esc(n.definition.created_by) + (n.definition.created_at ? ' · ' + esc(n.definition.created_at.slice(0,10)) : '') + '</div>';
    if (n.status === "proposed") html += '<div class="def-meta" style="color:var(--amber)">Intent only — not yet implemented by a model.</div>';
  } else {
    html += '<div class="muted">No definition. Run <code>puxti scan</code>.</div>';
  }
  html += '</div>';

  if (outs.length || ins.length) {
    html += '<div class="panel-section"><div class="panel-title">Relationships</div>';
    outs.forEach(l => { html += '<div class="edge-line"><span class="edge-line__rel">' + esc(l.type) + ' →</span><span class="edge-line__name">' + esc(byId.get(l.target).name) + '</span></div>'; });
    ins.forEach(l => { html += '<div class="edge-line"><span class="edge-line__rel">← ' + esc(l.type) + '</span><span class="edge-line__name">' + esc(byId.get(l.source).name) + '</span></div>'; });
    html += '</div>';
  }

  if (n.history && n.history.length > 1) {
    html += '<div class="panel-section"><div class="panel-title">Definition history</div>';
    const hist = n.history.slice().sort((a,b) => b.version - a.version);
    hist.forEach((h, i) => {
      const last = i === hist.length - 1;
      html += '<div class="tl-entry"><div class="tl-spine"><div class="tl-node ' + (i===0?'tl-node--current':'') + '"></div>' + (last?'':'<div class="tl-line"></div>') + '</div>';
      html += '<div class="tl-body"><div class="tl-ver">v' + esc(h.version) + (i===0?' · current':'') + '</div>';
      html += '<div class="tl-def">' + esc(h.description) + '</div>';
      html += '<div class="tl-by">by ' + esc(h.created_by) + (h.created_at ? ' · ' + esc(h.created_at.slice(0,10)) : '') + '</div></div></div>';
    });
    html += '</div>';
  }
  panel.innerHTML = html;
}

function renderList(panel) {
  const q = (document.getElementById("search").value || "").toLowerCase();
  const shown = nodes.filter(n => !q || n.name.toLowerCase().includes(q));
  // Built with DOM APIs (not innerHTML) so entity ids cannot break out of any
  // attribute, and so each row is a focusable, Enter/Space-operable button.
  panel.innerHTML = "";
  const section = document.createElement("div"); section.className = "panel-section";
  const title = document.createElement("div"); title.className = "panel-title";
  title.textContent = "Entities (" + shown.length + ")";
  section.appendChild(title);
  shown.slice().sort((a, b) => a.name.localeCompare(b.name)).forEach(n => {
    const st = styleFor(n);
    const row = document.createElement("button");
    row.type = "button"; row.className = "entity-row";
    const dot = document.createElement("span"); dot.className = "entity-row__dot";
    dot.style.background = st.fill; dot.style.borderColor = st.stroke;
    const name = document.createElement("span"); name.className = "entity-row__name";
    name.textContent = n.name;
    const type = document.createElement("span"); type.className = "entity-row__type";
    type.textContent = n.status === "proposed" ? "proposed" : n.type;
    row.append(dot, name, type);
    row.addEventListener("click", () => select(n.id));
    section.appendChild(row);
  });
  panel.appendChild(section);
}

document.getElementById("search").addEventListener("input", () => {
  const q = (document.getElementById("search").value || "").toLowerCase();
  nodeEls.forEach((g, i) => g.classList.toggle("dim", !!q && !nodes[i].name.toLowerCase().includes(q)));
  if (!selectedId) renderList(document.getElementById("panel"));
});

applyView();
renderPanel();
</script>
</body>
</html>
"""


def render_graph_html(payload: dict[str, Any]) -> str:
    """Return the standalone HTML page with the graph payload embedded.

    `</` is escaped so a definition containing `</script>` cannot break out of the
    inline script; the result is still valid JSON and JavaScript.
    """
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    return GRAPH_HTML.replace("__PUXTI_DATA__", data)
