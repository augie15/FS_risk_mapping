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
    case "trees": {
      const c = treeColourEl.value === "zone" ? ZONE_COLOURS[props.zone] : heightColour(props.height_m);
      return { stroke: "#000000", weight: 1, fill: c, fillOpacity: 0.9, radius: 3 + Math.min(props.height_m, 35) / 7 };
    }
  }
}

function popupFor(layer, p) {
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
  for (;;) {
    const s = await (await fetch(`/api/assess/${id}`)).json();
    statusEl.textContent = s.message;
    if (s.status === "done") return show(id, s.summary);
    if (s.status === "error") { statusEl.textContent = `Error: ${s.message}`; return; }
    await new Promise((res) => setTimeout(res, 1000));
  }
}

async function show(id, summary) {
  for (const h of Object.values(layers)) h.remove();
  layers = {};
  current = { id, summary };
  if (summary.ortho) {
    layers.ortho = map.addImage(`/api/property/${id}/ortho.png`, summary.ortho);
  }
  // Order matters for click targets: zones under parcel under buildings under trees.
  let extent = null;
  for (const name of ["zones", "parcel", "buildings", "trees"]) {
    const fc = await (await fetch(`/api/property/${id}/${name}.geojson`)).json();
    if (name === "zones") extent = bboxOf(fc);
    layers[name] = map.addGeoJSON(fc, (p) => styleFor(name, p), (p) => popupFor(name, p));
  }
  applyToggles();
  if (extent) map.fitBounds(extent);
  renderSummary(summary);
  statusEl.textContent = `Done in ${summary.elapsed_s}s`;
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
    addGeoJSON(fc, style, popup) {
      const d = new Data({ map: gmap });
      d.addGeoJson(fc);
      const apply = () => d.setStyle((f) => {
        const p = {}; f.forEachProperty((v, k) => { p[k] = v; });
        const s = style(p);
        return {
          strokeColor: s.stroke, strokeWeight: s.weight, fillColor: s.fill, fillOpacity: s.fillOpacity,
          icon: s.radius ? { path: google.maps.SymbolPath.CIRCLE, scale: s.radius, fillColor: s.fill,
            fillOpacity: s.fillOpacity, strokeColor: s.stroke, strokeWeight: s.weight } : undefined,
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
    fitBounds(b) { lmap.fitBounds([[b.south, b.west], [b.north, b.east]]); },
    addImage(url, b) {
      const o = L.imageOverlay(url, [[b.south, b.west], [b.north, b.east]], { interactive: false }).addTo(lmap);
      return { show: (v) => (v ? o.addTo(lmap) : o.remove()), remove: () => o.remove(), restyle() {} };
    },
    addGeoJSON(fc, style, popup) {
      const toLeaflet = (s) => ({ color: s.stroke, weight: s.weight, fillColor: s.fill, fillOpacity: s.fillOpacity, radius: s.radius });
      const g = L.geoJSON(fc, {
        style: (f) => toLeaflet(style(f.properties)),
        pointToLayer: (f, ll) => L.circleMarker(ll, toLeaflet(style(f.properties))),
        onEachFeature: (f, l) => l.bindPopup(() => popup(f.properties)),
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
  map.onClick((lat, lng) => assess(lat, lng).catch((e) => { statusEl.textContent = e.message; }));
  renderLegend();
  statusEl.textContent = cfg.googleMapsApiKey
    ? "Ready: click a home"
    : "Ready (no Google Maps key: using Leaflet fallback): click a home";
}

main().catch((e) => { statusEl.textContent = e.message; console.error(e); });
