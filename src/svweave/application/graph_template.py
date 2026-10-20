"""Self-contained HTML viewer for ``svweave graph export`` (ADR-014).

One file, no external resource: the CSP below forbids every network fetch, the data is an inline
JSON block and the script writes source-derived text with ``textContent`` only (never innerHTML).
"""

from __future__ import annotations

import json
from typing import Any

_PLACEHOLDER = "@@SVWEAVE_GRAPH_DATA@@"


def dumps_safe(obj: Any) -> str:
    """JSON for a <script type=application/json> element: ASCII only (so U+2028/2029 are escaped)
    and ``<``, ``>``, ``&`` escaped, so source text can never close the element or open a comment."""
    return (
        json.dumps(obj, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def render_html(model: dict[str, Any]) -> str:
    return _PAGE.replace(_PLACEHOLDER, dumps_safe(model))


_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SVWeave graph</title>
<style>
:root{--bg:#fff;--fg:#1d2330;--mut:#667085;--line:#c5ccd8;--card:#f5f7fa;--acc:#2457d6;--con:#d9480f;--ok:#2b8a3e;--warn:#b7791f;--bad:#c92a2a;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#14171f;--fg:#e6e9f0;--mut:#98a2b3;--line:#3a4252;--card:#1d222d;--acc:#7aa2ff;--con:#ff8a5c;--ok:#69db7c;--warn:#fcc419;--bad:#ff8787;color-scheme:dark}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.4 system-ui,sans-serif;display:flex;flex-direction:column;height:100vh}
header{padding:8px 16px;border-bottom:1px solid var(--line);display:flex;gap:12px;align-items:center;flex-wrap:wrap}
h1{font-size:16px;margin:0}
.badge{padding:1px 8px;border-radius:10px;border:1px solid var(--line);font-size:12px}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
#banner{padding:6px 16px;background:var(--card);border-bottom:1px solid var(--line);color:var(--warn);font-size:13px}
#banner:empty{display:none}
#bar{padding:6px 16px;display:flex;gap:10px;align-items:center;flex-wrap:wrap;border-bottom:1px solid var(--line)}
input[type=search]{padding:4px 8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg);min-width:200px}
button{padding:3px 10px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg);cursor:pointer}
main{flex:1;display:flex;min-height:0}
#view{flex:1;min-width:0;overflow:hidden;cursor:grab;touch-action:none}
#view.drag{cursor:grabbing}
svg{width:100%;height:100%;display:block}
aside{width:340px;max-width:45vw;border-left:1px solid var(--line);overflow:auto;padding:10px 14px;background:var(--card)}
aside h2{font-size:14px;margin:12px 0 4px}
aside dl{margin:0;display:grid;grid-template-columns:auto 1fr;gap:2px 10px}
aside dt{color:var(--mut)}aside dd{margin:0;word-break:break-all}
table{border-collapse:collapse;width:100%;font-size:12px}
td,th{border-bottom:1px solid var(--line);padding:2px 4px;text-align:left;vertical-align:top;word-break:break-all}
.node rect{fill:var(--card);stroke:var(--line);stroke-width:1.2}
.node.top rect{stroke:var(--acc);stroke-width:2}
.node.stub rect,.node.blackbox rect{stroke-dasharray:4 3;stroke:var(--warn)}
.node.sel rect{stroke:var(--acc);stroke-width:3}
.node.hit rect{fill:color-mix(in srgb,var(--acc) 22%,var(--card))}
.node.dim{opacity:.35}
.node text{fill:var(--fg);font-size:12px;pointer-events:none}
.node text.sub{fill:var(--mut);font-size:10.5px}
.node{cursor:pointer}
.hier{fill:none;stroke:var(--line);stroke-width:1.2}
.conn{fill:none;stroke:var(--con);stroke-width:1.4;stroke-dasharray:5 3;opacity:.8;cursor:pointer}
.conn.sel{opacity:1;stroke:var(--acc)}
.cnt{fill:var(--con);font-size:11px;pointer-events:none}
.conn.und{stroke-dasharray:2 3}
small{color:var(--mut)}
</style>
</head>
<body>
<header><h1>SVWeave graph</h1><span id="meta"></span></header>
<div id="banner"></div>
<div id="bar">
<input id="q" type="search" placeholder="search path or definition" aria-label="search">
<label><input id="showConn" type="checkbox" checked> connections</label>
<button id="expand">expand all</button><button id="collapse">collapse all</button>
<button id="fit">fit</button><span id="count"></span>
</div>
<main>
<div id="view"><svg id="svg" role="img" aria-label="instance hierarchy graph"><defs>
<marker id="arr" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto"><path d="M0,0 L8,4 L0,8 z" fill="#d9480f"/></marker></defs><g id="root"></g></svg></div>
<aside id="side"><p><small>Click a node or a connection (a line stands for all signals between two nodes). Double-click a node to collapse or expand it. Drag to pan, wheel to zoom.</small></p></aside>
</main>
<script id="data" type="application/json">@@SVWEAVE_GRAPH_DATA@@</script>
<script>
"use strict";
(function () {
var D = JSON.parse(document.getElementById("data").textContent);
var NS = "http://www.w3.org/2000/svg";
var byId = {}, kids = {}, roots = [];
D.nodes.forEach(function (n) { byId[n.id] = n; kids[n.id] = []; n.collapsed = n.depth >= 2; });
D.nodes.forEach(function (n) { if (n.parent && byId[n.parent]) kids[n.parent].push(n); else roots.push(n); });
function el(tag, attrs, text) {
  var e = document.createElementNS(NS, tag);
  for (var k in attrs) e.setAttribute(k, attrs[k]);
  if (text !== undefined) e.textContent = text;
  return e;
}
function h(tag, text, cls) { var e = document.createElement(tag); if (text !== undefined) e.textContent = text; if (cls) e.className = cls; return e; }

// ---- header and banner
var m = D.meta, meta = document.getElementById("meta");
function badge(t, c) { var b = h("span", t, "badge " + (c || "")); meta.appendChild(b); meta.appendChild(document.createTextNode(" ")); }
badge("build " + m.build_id.slice(0, 12));
badge(m.completeness, m.completeness === "COMPLETE" ? "ok" : "warn");
badge(m.freshness, m.freshness === "CURRENT" ? "ok" : "warn");
badge("profile " + m.profile);
badge(m.evidence_kind);
var msgs = [];
if (m.completeness !== "COMPLETE") msgs.push("Analysis is " + m.completeness + ": an absent instance or connection is not proof of absence.");
if (m.freshness !== "CURRENT") msgs.push("Freshness is " + m.freshness + ": the workspace changed since this build.");
if (D.truncated) msgs.push("Truncated: " + D.stats.instances_shown + " of " + D.stats.instances_total + " instances shown (shallowest first).");
if (D.hubs.length) msgs.push(D.hubs.length + " high-fanout signal(s) are listed in the side panel, not drawn.");
msgs.push(D.note);
document.getElementById("banner").textContent = msgs.join("  ");

// ---- layout (left to right tidy tree over the visible nodes)
var NW = 190, NH = 38, GX = 70, GY = 14, pos = {};
function visibleKids(n) { return n.collapsed ? [] : kids[n.id]; }
function layout() {
  pos = {};
  var y = 0;
  function walk(n, d) {
    var ks = visibleKids(n), cy;
    if (!ks.length) { cy = y; y += NH + GY; }
    else { var first = null, last = null; ks.forEach(function (k) { walk(k, d + 1); }); first = pos[ks[0].id].y; last = pos[ks[ks.length - 1].id].y; cy = (first + last) / 2; }
    pos[n.id] = { x: d * (NW + GX), y: cy };
  }
  roots.forEach(function (r) { walk(r, 0); });
}
function visAnc(id) { // nearest visible ancestor-or-self
  var n = byId[id];
  while (n && !pos[n.id]) n = n.parent ? byId[n.parent] : null;
  return n ? n.id : null;
}

// ---- drawing
var root = document.getElementById("root"), sel = null, selEdge = null, hits = null;
function draw() {
  layout();
  while (root.firstChild) root.removeChild(root.firstChild);
  var gh = el("g", {}), gc = el("g", {}), gn = el("g", {});
  root.appendChild(gh); root.appendChild(gc); root.appendChild(gn);
  D.nodes.forEach(function (n) {
    var p = pos[n.id]; if (!p || !n.parent || !pos[n.parent]) return;
    var q = pos[n.parent], x1 = q.x + NW, y1 = q.y + NH / 2, x2 = p.x, y2 = p.y + NH / 2, mx = (x1 + x2) / 2;
    gh.appendChild(el("path", { "class": "hier", d: "M" + x1 + "," + y1 + " C" + mx + "," + y1 + " " + mx + "," + y2 + " " + x2 + "," + y2 }));
  });
  if (document.getElementById("showConn").checked) {
    var bundles = {}, order = [];
    D.edges.forEach(function (e) {
      var a = visAnc(e.from), b = visAnc(e.to);
      if (!a || !b || a === b) return;
      var key = a + ">" + b;
      if (!bundles[key]) { bundles[key] = { a: a, b: b, items: [], directed: true }; order.push(key); }
      bundles[key].items.push(e);
      if (!e.directed) bundles[key].directed = false;
    });
    order.forEach(function (key, i) {
      var bd = bundles[key], pa = pos[bd.a], pb = pos[bd.b], x1 = pa.x + NW, y1 = pa.y + NH / 2, x2 = pb.x, y2 = pb.y + NH / 2;
      if (pb.x <= pa.x) { x1 = pa.x + NW / 2; x2 = pb.x + NW / 2; y1 = pa.y + (pb.y >= pa.y ? NH : 0); y2 = pb.y + (pb.y >= pa.y ? 0 : NH); }
      var dx = Math.max(40, Math.abs(x2 - x1) / 2), off = 14 + (i % 4) * 8;
      var d = "M" + x1 + "," + y1 + " C" + (x1 + dx) + "," + (y1 - off) + " " + (x2 - dx) + "," + (y2 - off) + " " + x2 + "," + y2;
      var n = bd.items.length;
      var attrs = { "class": "conn" + (bd.directed ? "" : " und") + (selEdge === key ? " sel" : ""), d: d, "stroke-width": String(1.4 + Math.min(3, Math.log(n) / Math.LN2 * 0.6)) };
      if (bd.directed) attrs["marker-end"] = "url(#arr)";
      var pth = el("path", attrs);
      pth.addEventListener("click", function (ev) { ev.stopPropagation(); selEdge = key; sel = null; showBundle(bd); draw(); });
      pth.appendChild(el("title", {}, n + " signal(s): " + bd.items.slice(0, 8).map(function (e) { return e.signal; }).join(", ") + (n > 8 ? ", ..." : "")));
      gc.appendChild(pth);
      if (n > 1) {
        var mx = (x1 + x2) / 2, my = (y1 + y2) / 2 - off * 0.75;
        gc.appendChild(el("text", { x: mx, y: my, "class": "cnt", "text-anchor": "middle" }, "\u00d7" + n));
      }
    });
  }
  D.nodes.forEach(function (n) {
    var p = pos[n.id]; if (!p) return;
    var cls = "node" + (n.top ? " top" : "") + (n.stub ? " stub" : "") + (n.blackbox ? " blackbox" : "") + (sel === n.id ? " sel" : "");
    if (hits) cls += hits[n.id] ? " hit" : " dim";
    var g = el("g", { "class": cls, transform: "translate(" + p.x + "," + p.y + ")", tabindex: "0" });
    g.appendChild(el("rect", { width: NW, height: NH, rx: 6 }));
    g.appendChild(el("text", { x: 8, y: 16 }, trunc(relName(n), 26)));
    var sub = n.definition || "", ck = kids[n.id].length;
    if (ck && n.collapsed) sub += "  [+" + ck + "]";
    g.appendChild(el("text", { x: 8, y: 30, "class": "sub" }, trunc(sub, 32)));
    g.appendChild(el("title", {}, n.path));
    g.addEventListener("click", function (ev) { ev.stopPropagation(); sel = n.id; selEdge = null; showNode(n); draw(); });
    g.addEventListener("dblclick", function (ev) { ev.stopPropagation(); if (kids[n.id].length) { n.collapsed = !n.collapsed; draw(); } });
    g.addEventListener("keydown", function (ev) { if (ev.key === "Enter") { sel = n.id; selEdge = null; showNode(n); draw(); } });
    gn.appendChild(g);
  });
  document.getElementById("count").textContent = Object.keys(pos).length + " of " + D.nodes.length + " nodes visible, " + D.edges.length + " connections";
}
function relName(n) {
  var p = n.parent && byId[n.parent];
  return p && n.path.indexOf(p.path + ".") === 0 ? n.path.slice(p.path.length + 1) : n.name;
}
function trunc(s, n) { s = String(s == null ? "" : s); return s.length > n ? s.slice(0, n - 1) + "\\u2026" : s; }

// ---- side panel
var side = document.getElementById("side");
function clearSide() { while (side.firstChild) side.removeChild(side.firstChild); }
function dl(rows) { var d = h("dl"); rows.forEach(function (r) { if (r[1] === null || r[1] === undefined || r[1] === "") return; d.appendChild(h("dt", r[0])); d.appendChild(h("dd", String(r[1]))); }); return d; }
function table(head, rows) {
  var t = h("table"), tr = h("tr"); head.forEach(function (x) { tr.appendChild(h("th", x)); }); t.appendChild(tr);
  rows.forEach(function (r) { var row = h("tr"); r.forEach(function (c) { row.appendChild(h("td", c)); }); t.appendChild(row); });
  return t;
}
function showNode(n) {
  clearSide();
  side.appendChild(h("h2", n.name));
  side.appendChild(dl([["path", n.path], ["definition", n.definition], ["top", n.top ? "yes" : ""], ["interface", n.interface ? "yes" : ""],
    ["stub", n.stub ? "yes" : ""], ["blackbox candidate", n.blackbox ? "yes" : ""], ["generate", (n.generate || []).join(" / ")],
    ["source", n.file ? n.file + (n.line ? ":" + n.line : "") : ""], ["children", kids[n.id].length]]));
  if (n.params.length) { side.appendChild(h("h2", "Parameters")); side.appendChild(table(["name", "value", ""], n.params.map(function (p) { return [p.name, p.value, p.overridden ? "overridden" : ""]; }))); }
  if (n.ports.length) { side.appendChild(h("h2", "Port associations")); side.appendChild(table(["port", "dir", "actual"], n.ports.map(function (p) { return [p.port, p.dir, p.actual]; }))); }
  var rel = D.edges.filter(function (e) { return e.from === n.id || e.to === n.id; });
  if (rel.length) { side.appendChild(h("h2", "Connections (" + rel.length + ")")); side.appendChild(table(["signal", "to / from"], rel.slice(0, 200).map(function (e) { var o = e.from === n.id ? byId[e.to] : byId[e.from]; return [e.signal, (e.from === n.id ? "-> " : "<- ") + (o ? o.path : "?")]; }))); }
  hubs();
}
function showBundle(bd) {
  clearSide();
  var a = byId[bd.a], b = byId[bd.b];
  side.appendChild(h("h2", "Connections (" + bd.items.length + ")"));
  side.appendChild(dl([["from", a && a.path], ["to", b && b.path], ["direction", bd.directed ? "declared port direction" : "mixed / undirected"]]));
  side.appendChild(table(["signal", "port"], bd.items.slice(0, 300).map(function (e) { return [e.signal, e.port]; })));
  side.appendChild(h("p", D.note, "mut"));
  hubs();
}
function hubs() {
  if (!D.hubs.length) return;
  side.appendChild(h("h2", "High-fanout signals (not drawn)"));
  side.appendChild(table(["signal", "instances"], D.hubs.slice(0, 50).map(function (x) { return [x.signal, x.instances]; })));
}

// ---- toolbar
document.getElementById("showConn").addEventListener("change", draw);
document.getElementById("expand").addEventListener("click", function () { D.nodes.forEach(function (n) { n.collapsed = false; }); draw(); fit(); });
document.getElementById("collapse").addEventListener("click", function () { D.nodes.forEach(function (n) { n.collapsed = n.depth >= 1; }); draw(); fit(); });
document.getElementById("fit").addEventListener("click", fit);
document.getElementById("q").addEventListener("input", function () {
  var q = this.value.trim().toLowerCase();
  if (!q) { hits = null; draw(); return; }
  hits = {};
  D.nodes.forEach(function (n) {
    if ((n.path + " " + (n.definition || "")).toLowerCase().indexOf(q) >= 0) {
      hits[n.id] = 1;
      var a = n.parent ? byId[n.parent] : null;
      while (a) { a.collapsed = false; a = a.parent ? byId[a.parent] : null; }
    }
  });
  draw();
});

// ---- pan / zoom
var view = document.getElementById("view"), svg = document.getElementById("svg");
var tx = 20, ty = 20, k = 1;
function apply() { root.setAttribute("transform", "translate(" + tx + "," + ty + ") scale(" + k + ")"); }
function fit() {
  var r = view.getBoundingClientRect(), maxx = 0, maxy = 0;
  Object.keys(pos).forEach(function (id) { maxx = Math.max(maxx, pos[id].x + NW); maxy = Math.max(maxy, pos[id].y + NH); });
  k = Math.min(1, (r.width - 40) / Math.max(maxx, 1), (r.height - 40) / Math.max(maxy, 1)); tx = 20; ty = 20; apply();
}
var drag = null;
view.addEventListener("pointerdown", function (e) { if (e.target.closest && e.target.closest(".node,.conn")) return; drag = { x: e.clientX, y: e.clientY, tx: tx, ty: ty }; view.classList.add("drag"); view.setPointerCapture(e.pointerId); });
view.addEventListener("pointermove", function (e) { if (!drag) return; tx = drag.tx + e.clientX - drag.x; ty = drag.ty + e.clientY - drag.y; apply(); });
view.addEventListener("pointerup", function () { drag = null; view.classList.remove("drag"); });
view.addEventListener("wheel", function (e) {
  e.preventDefault();
  var r = svg.getBoundingClientRect(), px = e.clientX - r.left, py = e.clientY - r.top, nk = Math.min(4, Math.max(0.1, k * (e.deltaY < 0 ? 1.12 : 1 / 1.12)));
  tx = px - (px - tx) * (nk / k); ty = py - (py - ty) * (nk / k); k = nk; apply();
}, { passive: false });
draw(); fit();
})();
</script>
</body>
</html>
"""
