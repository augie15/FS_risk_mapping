// FS Risk Mapping viewer. Google Maps when a key is set, otherwise a Leaflet fallback,
// both behind one small adapter so the layer code is shared.
// Basemap imagery (Google or Esri) is display only; analysis uses BC open data server-side.
const DEFAULT_CENTER = { lat: 54.6667, lng: -127.05 }; // Telkwa, BC
const statusEl = document.getElementById("status");
const summaryEl = document.getElementById("summary");
const legendEl = document.getElementById("legend");
const treeColourEl = document.getElementById("treeColour");

const ZONE_COLOURS = { immediate: "#d7191c", intermediate: "#fd8d3c", extended: "#fee08b", outer: "#91cf60" };
const ZONE_LABELS = { immediate: "Immediate 0–1.5 m", intermediate: "Intermediate 1.5–10 m", extended: "Extended 10–30 m", outer: "30–100 m" };
const HEIGHT_BINS = [ // [min height m, colour, label]
  [25, "#bd0026", "25 m +"], [15, "#f03b20", "15–25 m"], [8, "#fd8d3c", "8–15 m"], [0, "#fecc5c", "3–8 m"],
];
const heightColour = (h) => HEIGHT_BINS.find(([min]) => h >= min)[1];
const STATUS_COLOURS = { concern: "#d7191c", cant_tell: "#9e9e9e", ok: "#1a9641" };
const STATUS_LABELS = { concern: "Concern", cant_tell: "Can't tell", ok: "OK" };
// Model output is untrusted: escape before it touches innerHTML.
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

let map;                 // adapter
let layers = {};         // name -> handle
let current = null;      // {id, summary}

// ---------------------------------------------------------------- styles

function styleFor(layer, props) {
  switch (layer) {
    case "parcel": return { stroke: "#ffe600", weight: 3, fill: "#ffe600", fillOpacity: 0 };
    case "zones": return { stroke: ZONE_COLOURS[props.zone], weight: 1.5, fill: ZONE_COLOURS[props.zone], fillOpacity: 0.18 };
    case "buildings": return props.home
      ? { stroke: "#00e5ff", weight: 3, fill: "#00e5ff", fillOpacity: 0.25 }
      : { stroke: "#ffffff", weight: 1, fill: "#ffffff", fillOpacity: 0.15 };
    case "findings": return {
      stroke: props.lidar_result === "disagree" ? "#ff9800" : "#ffffff", // orange outline = LiDAR disagrees
      weight: props.lidar_result === "disagree" ? 4 : 2,
      fill: STATUS_COLOURS[props.status], fillOpacity: 1, approx: props.approx,
      pin: { concern: "!", cant_tell: "?", ok: "✓" }[props.status],
    };
    case "trees": {
      const c = treeColourEl.value === "zone" ? ZONE_COLOURS[props.zone] : heightColour(props.height_m);
      return { stroke: "#000000", weight: 1, fill: c, fillOpacity: 0.9, radius: 3 + Math.min(props.height_m, 35) / 7 };
    }
  }
}

function findingPopup(p) {
  const dot = p.x != null && p.w
    ? `<span class="crop-dot" style="left:${(100 * p.x / p.w).toFixed(1)}%;top:${(100 * p.y / p.h).toFixed(1)}%"></span>` : "";
  return `<div class="finding">
    <b style="color:${STATUS_COLOURS[p.status]}">${STATUS_LABELS[p.status]}</b> · ${esc(ZONE_LABELS[p.zone])} · ${esc(p.backend)}<br>
    <i>${esc(p.question)}</i><br>
    ${esc(p.reason)} <span class="hint">(confidence ${esc(p.confidence)})</span><br>
    <span class="lidar-${esc(p.lidar_result)}">LiDAR ${esc(p.lidar_result)}: ${esc(p.lidar_note)}</span>
    ${p.approx ? '<br><span class="hint">Pin position is approximate (no spot given).</span>' : ""}
    ${p.crop ? `<div class="crop-wrap"><img src="${p.crop}" alt="zone crop">${dot}</div>` : ""}
  </div>`;
}

function popupFor(layer, p) {
  if (layer === "findings") return findingPopup(p);
  if (layer === "trees") {
    return `<b>Tree ${p.height_m} m</b><br>Trunk ${p.dist_to_home_m} m from home (${ZONE_LABELS[p.zone]})<br>` +
      `Crown edge ${p.crown_edge_to_home_m} m (${ZONE_LABELS[p.crown_zone]})<br>Crown radius ≈ ${p.crown_radius_m} m` +
      (p.on_parcel ? "" : "<br><i>off this parcel</i>");
  }
  if (layer === "zones") return `<b>${p.label}</b><br>${p.area_m2.toLocaleString()} m², ${Math.round(p.share_on_parcel * 100)}% on parcel`;
  if (layer === "buildings") return p.home ? `<b>Home</b> (${p.source} footprint)` : `Building, ${p.dist_to_home_m} m from home (${p.source})`;
  if (layer === "parcel") return `<b>Parcel</b><br>${p.PARCEL_CLASS || ""} ${p.OWNER_TYPE || ""}<br>${Math.round(p.FEATURE_AREA_SQM || 0).toLocaleString()} m²`;
  return "";
}

function renderLegend() {
  const rows = [];
  rows.push(`<div class="legend-row"><span class="swatch sq" style="background:transparent;border:2px solid #ffe600"></span>Parcel</div>`);
  rows.push(`<div class="legend-row"><span class="swatch sq" style="background:#00e5ff66;border:2px solid #00e5ff"></span>Home</div>`);
  for (const [k, c] of Object.entries(ZONE_COLOURS)) {
    rows.push(`<div class="legend-row"><span class="swatch sq" style="background:${c}"></span>${ZONE_LABELS[k]}</div>`);
  }
  rows.push(`<div style="margin-top:.4rem"><b>Trees by ${treeColourEl.value}</b></div>`);
  if (treeColourEl.value === "height") {
    for (const [, c, label] of HEIGHT_BINS) rows.push(`<div class="legend-row"><span class="swatch" style="background:${c}"></span>${label}</div>`);
  } else {
    rows.push(`<div class="legend-row">Same colours as the zones (by trunk position)</div>`);
  }
  legendEl.innerHTML = rows.join("");
}

// ---------------------------------------------------------------- assessment

async function assess(lat, lng) {
  statusEl.textContent = "Starting assessment…";
  const r = await fetch("/api/assess", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ lat, lng }),
  });
  if (!r.ok) { statusEl.textContent = `Error: ${(await r.json()).detail?.[0]?.msg || r.status}`; return; }
  const { id } = await r.json();
  return pollAssessment(id);
}

async function pollAssessment(id) {
  for (;;) {
    const s = await (await fetch(`/api/assess/${id}`)).json();
    statusEl.textContent = s.message;
    if (s.status === "done") return show(id, s.summary);
    if (s.status === "error") { statusEl.textContent = `Error: ${s.message}`; return; }
    await new Promise((res) => setTimeout(res, 1000));
  }
}

async function show(id, summary) {
  for (const h of Object.values(layers)) h?.remove(); // a slot can be empty (no findings yet)
  layers = {};
  current = { id, summary };
  stopEditing();
  if (summary.ortho) {
    layers.ortho = map.addImage(`/api/property/${id}/ortho.png`, summary.ortho);
  }
  const needed = summary.status === "outline_needed";
  // Order matters for click targets: zones under parcel under buildings under trees.
  let extent = null;
  for (const name of needed ? ["parcel", "buildings"] : ["zones", "parcel", "buildings", "trees"]) {
    const fc = await (await fetch(`/api/property/${id}/${name}.geojson`)).json();
    if (name === "zones") extent = bboxOf(fc);
    // Only trees keep pop-ups; zones, parcel and buildings pass clicks through so any house,
    // including one inside the current rings, can be selected for assessment.
    layers[name] = map.addGeoJSON(fc, (p) => styleFor(name, p), (p) => popupFor(name, p),
      { interactive: name === "trees" });
  }
  applyToggles();
  if (extent) map.fitBounds(extent);
  else if (summary.ortho) map.fitBounds(summary.ortho);
  history.replaceState(null, "", `?id=${id}`);
  await renderOutline(id, summary);
  if (needed) {
    summaryEl.hidden = true;
    aiEl.hidden = true;
    statusEl.textContent = "Outline needed: adjust the cyan outline to the roof, then Save";
    startEditing();
    return;
  }
  renderSummary(summary);
  statusEl.textContent = `Done in ${summary.elapsed_s}s`;
  await loadFindings(id);
}

// ---------------------------------------------------------------- home outline editing

const outlineEl = document.getElementById("outline");
const ob = (id) => document.getElementById(id);
let editor = null;       // active polygon editor
let outlineFeature = null;

async function renderOutline(id, summary) {
  outlineEl.hidden = false;
  const r = await fetch(`/api/property/${id}/outline.geojson`);
  outlineFeature = r.ok ? (await r.json()).features[0] : null;
  const o = summary.home?.outline;
  const rows = [];
  if (summary.status === "outline_needed") {
    rows.push(`<div class="note">${esc(summary.notes[0])}</div>`);
    rows.push(`<p class="hint">Starting shape: ${esc(outlineFeature?.properties.source || "none")}. It is not used until you save.</p>`);
  } else if (o) {
    const conf = o.confidence_label === "confirmed" ? "operator-confirmed"
      : `${o.confidence_label} confidence (${Math.round(o.confidence * 100)}%)`;
    rows.push(`<table>
      <tr><td>Source</td><td>${esc(o.source)}</td></tr>
      <tr><td>Confidence</td><td>${esc(conf)}</td></tr>
      ${o.points ? `<tr><td>Roof points</td><td>${o.points.toLocaleString()} (${o.roof_density_pts_m2}/m²)</td></tr>` : ""}
      <tr><td>Area</td><td>${summary.home.area_m2} m²</td></tr>
      ${o.footprint_iou != null ? `<tr><td>Overlap with Microsoft footprint</td><td>${Math.round(o.footprint_iou * 100)}%</td></tr>` : ""}
    </table>`);
  }
  ob("outlineInfo").innerHTML = rows.join("");
  ob("outlineRevert").hidden = o?.source !== "operator-confirmed";
  setEditButtons(false);
}

function setEditButtons(on) {
  ob("outlineEdit").hidden = on;
  ob("outlineSave").hidden = !on;
  ob("outlineCancel").hidden = !on; // always offer a way out, so other houses can be clicked
  ob("outlineHelp").hidden = !on;
}

function startEditing() {
  if (!outlineFeature) return;
  stopEditing();
  editor = map.editPolygon(outlineFeature.geometry.coordinates[0].slice(0, -1));
  layers.buildings?.show(false); // the static home shape would sit under the editable one
  setEditButtons(true);
}

function stopEditing() {
  editor?.remove();
  editor = null;
  setEditButtons(false);
  if (layers.buildings) applyToggles();
}

async function saveOutline() {
  if (!editor || !current) return;
  const coords = editor.getPath();
  if (coords.length < 3) { statusEl.textContent = "An outline needs at least 3 corners"; return; }
  statusEl.textContent = "Saving outline…";
  const r = await fetch(`/api/property/${current.id}/outline`, { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify({ coordinates: coords }) });
  if (!r.ok) { statusEl.textContent = `Error: ${JSON.stringify((await r.json()).detail)}`; return; }
  stopEditing();
  await pollAssessment(current.id);
}

async function revertOutline() {
  if (!current) return;
  const r = await fetch(`/api/property/${current.id}/outline`, { method: "DELETE" });
  if (!r.ok) { statusEl.textContent = `Error: ${(await r.json()).detail}`; return; }
  await pollAssessment(current.id);
}

ob("outlineEdit").addEventListener("click", startEditing);
ob("outlineCancel").addEventListener("click", stopEditing);
ob("outlineSave").addEventListener("click", saveOutline);
ob("outlineRevert").addEventListener("click", revertOutline);

const aiEl = document.getElementById("ai");
const aiBackendEl = document.getElementById("aiBackend");
const aiStatusEl = document.getElementById("aiStatus");
const aiTotalsEl = document.getElementById("aiTotals");

async function setupAi() {
  const st = await (await fetch("/api/ai/status")).json();
  aiBackendEl.innerHTML = Object.entries(st.backends).map(([k, b]) =>
    `<option value="${k}" ${b.available ? "" : "disabled"}>${k === "local" ? "Local" : "Claude"} (${esc(b.model)})${b.available ? "" : " – unavailable"}</option>`).join("");
  const firstOk = Object.entries(st.backends).find(([, b]) => b.available);
  if (firstOk) aiBackendEl.value = firstOk[0];
  aiBackendEl.title = Object.values(st.backends).map((b) => b.detail).join("\n");
  aiBackendEl.addEventListener("change", () => current && loadFindings(current.id));
  document.getElementById("aiRun").addEventListener("click", runAi);
}

async function runAi() {
  if (!current) return;
  const backend = aiBackendEl.value;
  const r = await fetch("/api/ai/run", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id: current.id, backend }) });
  if (!r.ok) { aiStatusEl.textContent = `Error: ${(await r.json()).detail}`; return; }
  for (;;) {
    const j = await (await fetch(`/api/ai/job/${current.id}/${backend}`)).json();
    aiStatusEl.textContent = j.message;
    if (j.status === "done") return loadFindings(current.id);
    if (j.status === "error") { aiStatusEl.textContent = `Error: ${j.message}`; return; }
    await new Promise((res) => setTimeout(res, 1500));
  }
}

async function loadFindings(id) {
  layers.findings?.remove();
  Reflect.deleteProperty(layers, "findings");
  aiEl.hidden = false;
  document.getElementById("compareLink").href = `compare.html?id=${id}`;
  const backend = aiBackendEl.value || "local";
  const r = await fetch(`/api/ai/${id}/${backend}`);
  if (!r.ok) { aiStatusEl.textContent = "No findings yet for this backend. Press Run."; aiTotalsEl.innerHTML = ""; return; }
  const res = await r.json();
  const feats = [];
  for (const z of res.zones) {
    const crop = z.crop ? `/api/property/${id}/crop/${z.zone}.png` : null;
    for (const f of z.findings) {
      if (f.status === "ok" && f.lidar.result !== "disagree") continue; // pin concerns, can't-tells and conflicts
      feats.push({ type: "Feature", geometry: { type: "Point", coordinates: [f.geo.lng, f.geo.lat] },
        properties: { zone: z.zone, backend: res.model, status: f.status, confidence: f.confidence, reason: f.reason,
          question: f.question, lidar_result: f.lidar.result, lidar_note: f.lidar.note, approx: f.geo.approx, crop,
          x: f.location?.x ?? null, y: f.location?.y ?? null, w: z.crop_w ?? null, h: z.crop_h ?? null } });
    }
  }
  layers.findings = map.addGeoJSON({ type: "FeatureCollection", features: feats },
    (p) => styleFor("findings", p), (p) => popupFor("findings", p));
  applyToggles();
  const t = res.totals;
  aiStatusEl.textContent = res.stale
    ? `⚠ Out of date: the property was re-run after these ${res.model} findings. Press Run again.`
    : `${res.model}, checklist ${res.checklist_version}`;
  aiStatusEl.classList.toggle("note", !!res.stale);
  const count = (st) => res.zones.reduce((n, z) => n + z.findings.filter((f) => f.status === st).length, 0);
  const sw = (bg, extra = "") => `<span class="swatch" style="display:inline-block;vertical-align:middle;background:${bg};${extra}"></span>`;
  aiTotalsEl.innerHTML = `<table>
    <tr><td>${sw(STATUS_COLOURS.concern)} Concerns</td><td>${count("concern")}</td></tr>
    <tr><td>${sw(STATUS_COLOURS.cant_tell)} Can't tell</td><td>${count("cant_tell")}</td></tr>
    <tr><td>${sw(STATUS_COLOURS.ok)} OK (not pinned)</td><td>${count("ok")}</td></tr>
    <tr><td>${sw("#fff", "border:3px solid #ff9800")} LiDAR disagrees</td><td>${t.lidar_disagreements}</td></tr>
    <tr><td>Time · cost</td><td>${t.latency_s}s · $${t.cost_usd.toFixed(3)}</td></tr></table>`;
}

function renderSummary(s) {
  const t = s.trees;
  const zoneRows = Object.keys(ZONE_LABELS).map((k) =>
    `<tr><td><span class="swatch sq" style="display:inline-block;vertical-align:middle;background:${ZONE_COLOURS[k]}"></span> ${ZONE_LABELS[k]}</td>` +
    `<td>${t.by_trunk_zone[k]}</td><td>${t.by_crown_edge_zone[k]}</td></tr>`).join("");
  const lid = s.lidar
    ? `LiDAR ${s.lidar.year} (${s.lidar.project}), ${s.lidar.density_pts_m2} pts/m² here`
    : "No LiDAR coverage";
  const ortho = s.ortho ? `Orthophoto ${s.ortho.source.match(/(\d{4})\.tif$/)?.[1] || ""}, 15 cm` : "No orthophoto";
  summaryEl.innerHTML = `
    <h2>This property</h2>
    <table>
      <tr><td>Parcel</td><td>${s.parcel.PARCEL_CLASS || ""}, ${Math.round(s.parcel.FEATURE_AREA_SQM || 0).toLocaleString()} m²</td></tr>
      <tr><td>Home footprint</td><td>${s.home.area_m2} m² (${s.home.source})</td></tr>
      <tr><td>Trees ≥ ${t.min_height_m} m within 100 m</td><td>${t.count}</td></tr>
      <tr><td>Tallest</td><td>${t.tallest_m ?? "–"} m</td></tr>
    </table>
    <h2>Trees by zone</h2>
    <table>
      <tr><td></td><td>trunk</td><td>crown edge</td></tr>
      ${zoneRows}
    </table>
    <p class="hint" style="margin-top:.5rem">${lid}. ${ortho}.</p>
    ${s.notes.map((n) => `<div class="note">${n}</div>`).join("")}`;
  summaryEl.hidden = false;
}

function bboxOf(fc) {
  const b = { west: 180, south: 90, east: -180, north: -90 };
  const walk = (c) => {
    if (typeof c[0] === "number") {
      b.west = Math.min(b.west, c[0]); b.east = Math.max(b.east, c[0]);
      b.south = Math.min(b.south, c[1]); b.north = Math.max(b.north, c[1]);
    } else c.forEach(walk);
  };
  fc.features.forEach((f) => walk(f.geometry.coordinates));
  return fc.features.length ? b : null;
}

function applyToggles() {
  for (const cb of document.querySelectorAll("#layers input[type=checkbox]")) {
    layers[cb.dataset.layer]?.show(cb.checked);
  }
}

document.querySelectorAll("#layers input[type=checkbox]").forEach((cb) => cb.addEventListener("change", applyToggles));
treeColourEl.addEventListener("change", () => {
  renderLegend();
  layers.trees?.restyle();
});

// ---------------------------------------------------------------- map adapters

async function googleAdapter(cfg) {
  await loadMapsApi(cfg.googleMapsApiKey);
  const { Map, InfoWindow, GroundOverlay, Data } = await google.maps.importLibrary("maps");
  const gmap = new Map(document.getElementById("map"), {
    center: DEFAULT_CENTER, zoom: 13, mapTypeId: "hybrid", mapId: cfg.googleMapsMapId || undefined, tilt: 0,
  });
  window.fsMap = gmap;
  const info = new InfoWindow();
  return {
    kind: "google",
    onClick(cb) { gmap.addListener("click", (e) => cb(e.latLng.lat(), e.latLng.lng())); },
    fitBounds(b) { gmap.fitBounds({ west: b.west, south: b.south, east: b.east, north: b.north }); },
    addImage(url, b) {
      const o = new GroundOverlay(url, { west: b.west, south: b.south, east: b.east, north: b.north }, { clickable: false });
      o.setMap(gmap);
      return { show: (v) => o.setMap(v ? gmap : null), remove: () => o.setMap(null), restyle() {} };
    },
    editPolygon(lnglats) {
      const poly = new google.maps.Polygon({ map: gmap, editable: true, zIndex: 2000,
        paths: lnglats.map(([lng, lat]) => ({ lat, lng })),
        strokeColor: "#00e5ff", strokeWeight: 3, fillColor: "#00e5ff", fillOpacity: 0.15 });
      poly.addListener("rightclick", (e) => {
        const path = poly.getPath();
        if (e.vertex != null && path.getLength() > 3) path.removeAt(e.vertex);
      });
      return { getPath: () => poly.getPath().getArray().map((ll) => [ll.lng(), ll.lat()]),
        remove: () => poly.setMap(null) };
    },
    addGeoJSON(fc, style, popup, { interactive = true } = {}) {
      const d = new Data({ map: gmap });
      d.addGeoJson(fc);
      const apply = () => d.setStyle((f) => {
        const p = {}; f.forEachProperty((v, k) => { p[k] = v; });
        const s = style(p);
        return {
          // Non-interactive layers let clicks fall through to the map (assess the house under it).
          clickable: interactive,
          strokeColor: s.stroke, strokeWeight: s.weight, fillColor: s.fill, fillOpacity: s.fillOpacity,
          icon: s.pin
            ? { path: "M0 0C-3-9-11-12-11-20A11 11 0 1 1 11-20C11-12 3-9 0 0Z", scale: 1.1, fillColor: s.fill,
              fillOpacity: 1, strokeColor: s.stroke, strokeWeight: s.weight, labelOrigin: new google.maps.Point(0, -20) }
            : s.radius ? { path: google.maps.SymbolPath.CIRCLE, scale: s.radius, fillColor: s.fill,
              fillOpacity: s.fillOpacity, strokeColor: s.stroke, strokeWeight: s.weight } : undefined,
          label: s.pin ? { text: s.pin, color: "#fff", fontWeight: "700" } : undefined,
          zIndex: s.pin ? 1000 : undefined,
        };
      });
      apply();
      d.addListener("click", (e) => {
        const p = {}; e.feature.forEachProperty((v, k) => { p[k] = v; });
        info.setContent(popup(p)); info.setPosition(e.latLng); info.open(gmap);
      });
      return { show: (v) => d.setMap(v ? gmap : null), remove: () => d.setMap(null), restyle: apply };
    },
  };
}

async function leafletAdapter() {
  await loadCss("https://unpkg.com/leaflet@1.9.4/dist/leaflet.css");
  await loadScript("https://unpkg.com/leaflet@1.9.4/dist/leaflet.js");
  const lmap = L.map("map", { maxZoom: 21 }).setView([DEFAULT_CENTER.lat, DEFAULT_CENTER.lng], 14);
  const esri = L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", {
    maxZoom: 21, maxNativeZoom: 18, attribution: "Basemap: Esri World Imagery (display only)",
  }).addTo(lmap);
  const osm = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 21, maxNativeZoom: 19, attribution: "© OpenStreetMap contributors",
  });
  L.control.layers({ "Esri imagery": esri, "OpenStreetMap": osm }).addTo(lmap);
  window.fsMap = lmap;
  return {
    kind: "leaflet",
    onClick(cb) { lmap.on("click", (e) => cb(e.latlng.lat, e.latlng.lng)); },
    fitBounds(b) { lmap.invalidateSize(); lmap.fitBounds([[b.south, b.west], [b.north, b.east]]); },
    addImage(url, b) {
      const o = L.imageOverlay(url, [[b.south, b.west], [b.north, b.east]], { interactive: false }).addTo(lmap);
      return { show: (v) => (v ? o.addTo(lmap) : o.remove()), remove: () => o.remove(), restyle() {} };
    },
    editPolygon(lnglats) {
      // Small vertex editor: drag corners, drag a midpoint to add a corner, right-click to remove.
      let pts = lnglats.map(([lng, lat]) => L.latLng(lat, lng));
      const poly = L.polygon(pts, { color: "#00e5ff", weight: 3, fillOpacity: 0.15, interactive: false }).addTo(lmap);
      const handles = L.layerGroup().addTo(lmap);
      const icon = (cls) => L.divIcon({ className: cls, iconSize: [12, 12] });
      const redraw = () => {
        poly.setLatLngs(pts);
        handles.clearLayers();
        pts.forEach((p, i) => {
          const m = L.marker(p, { draggable: true, icon: icon("vtx"), bubblingMouseEvents: false }).addTo(handles);
          m.on("drag", (e) => { pts[i] = e.target.getLatLng(); poly.setLatLngs(pts); });
          m.on("dragend", redraw);
          m.on("contextmenu", () => { if (pts.length > 3) { pts.splice(i, 1); redraw(); } });
          const q = pts[(i + 1) % pts.length];
          const mid = L.latLng((p.lat + q.lat) / 2, (p.lng + q.lng) / 2);
          const mm = L.marker(mid, { draggable: true, icon: icon("vtx mid"), bubblingMouseEvents: false }).addTo(handles);
          mm.on("dragstart", () => pts.splice(i + 1, 0, mid));
          mm.on("drag", (e) => { pts[i + 1] = e.target.getLatLng(); poly.setLatLngs(pts); });
          mm.on("dragend", redraw);
        });
      };
      redraw();
      return { getPath: () => pts.map((p) => [p.lng, p.lat]),
        remove: () => { poly.remove(); handles.remove(); } };
    },
    addGeoJSON(fc, style, popup, { interactive = true } = {}) {
      const toLeaflet = (s) => ({ color: s.stroke, weight: s.weight, fillColor: s.fill, fillOpacity: s.fillOpacity,
        radius: s.radius, interactive,
        // Interactive features (trees, pins) open their popup only; the map click must not also fire.
        bubblingMouseEvents: !interactive });
      const g = L.geoJSON(fc, {
        interactive,
        style: (f) => toLeaflet(style(f.properties)),
        pointToLayer: (f, ll) => {
          const s = style(f.properties);
          if (!s.pin) return L.circleMarker(ll, toLeaflet(s));
          return L.marker(ll, { riseOnHover: true, zIndexOffset: 1000, icon: L.divIcon({
            className: "fpin-wrap", iconSize: [26, 34], iconAnchor: [13, 34], popupAnchor: [0, -30],
            html: `<span class="fpin" style="background:${s.fill};border-color:${s.stroke};opacity:${s.approx ? 0.75 : 1}"><b>${s.pin}</b></span>` }) });
        },
        onEachFeature: (f, l) => { if (interactive) l.bindPopup(() => popup(f.properties)); },
      }).addTo(lmap);
      return {
        show: (v) => (v ? g.addTo(lmap) : g.remove()),
        remove: () => g.remove(),
        restyle: () => g.setStyle((f) => toLeaflet(style(f.properties))),
      };
    },
  };
}

// ---------------------------------------------------------------- loaders

function loadScript(src) {
  return new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = src; s.onload = resolve; s.onerror = () => reject(new Error(`Failed to load ${src}`));
    document.head.appendChild(s);
  });
}

function loadCss(href) {
  return new Promise((resolve) => {
    const l = document.createElement("link");
    l.rel = "stylesheet"; l.href = href; l.onload = resolve; l.onerror = resolve;
    document.head.appendChild(l);
  });
}

// Google's documented dynamic bootstrap loader, condensed.
function loadMapsApi(key) {
  return new Promise((resolve, reject) => {
    const cb = "__gmapsReady";
    window[cb] = () => { delete window[cb]; resolve(); };
    const s = document.createElement("script");
    s.src = `https://maps.googleapis.com/maps/api/js?key=${encodeURIComponent(key)}&v=weekly&loading=async&callback=${cb}`;
    s.async = true;
    s.onerror = () => reject(new Error("Google Maps failed to load"));
    document.head.appendChild(s);
  });
}

async function main() {
  const cfg = await (await fetch("/api/config")).json();
  map = cfg.googleMapsApiKey ? await googleAdapter(cfg) : await leafletAdapter();
  map.onClick((lat, lng) => {
    if (editor) return; // clicks belong to the outline editor while it is open
    assess(lat, lng).catch((e) => { statusEl.textContent = e.message; });
  });
  renderLegend();
  await setupAi();
  const qid = new URLSearchParams(location.search).get("id");
  if (qid && /^[0-9a-f]{12}$/.test(qid)) {
    const s = await (await fetch(`/api/assess/${qid}`)).json();
    if (s.summary) return show(qid, s.summary);
  }
  statusEl.textContent = cfg.googleMapsApiKey
    ? "Ready: click a home"
    : "Ready (no Google Maps key: using Leaflet fallback): click a home";
}

main().catch((e) => { statusEl.textContent = e.message; console.error(e); });
