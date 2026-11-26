// Side-by-side comparison of the local and Claude findings for one property.
const BACKENDS = ["local", "claude"];
const NAMES = { local: "Local (Ollama)", claude: "Claude API" };
const STATUS = { ok: "OK", concern: "Concern", cant_tell: "Can't tell" };
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const $ = (id) => document.getElementById(id);

let pid = null;
let results = {};   // backend -> result json | null
let marks = {};     // `${backend}|${zone}|${item}` -> mark
let aiStatus = null;

async function getJson(url) {
  const r = await fetch(url);
  return r.ok ? r.json() : null;
}

async function init() {
  const props = await getJson("/api/properties");
  aiStatus = await getJson("/api/ai/status");
  const want = new URLSearchParams(location.search).get("id");
  $("prop").innerHTML = props.map((p, i) =>
    `<option value="${p.id}">Property ${i + 1} · ${p.id.slice(0, 6)} · ${p.trees} trees · AI: ${p.ai.join(" + ") || "none"}</option>`).join("");
  if (want && props.some((p) => p.id === want)) $("prop").value = want;
  $("prop").addEventListener("change", () => load($("prop").value));
  if (props.length) await load($("prop").value);
  else $("zones").innerHTML = '<p class="none">No properties yet. Click a home on the map first.</p>';
}

async function load(id) {
  pid = id;
  history.replaceState(null, "", `?id=${id}`);
  $("mapLink").href = `index.html?id=${id}`;
  results = {};
  for (const b of BACKENDS) results[b] = await getJson(`/api/ai/${id}/${b}`);
  marks = {};
  for (const m of (await getJson(`/api/marks/${id}`)) || []) marks[`${m.backend}|${m.zone}|${m.item_id}`] = m;
  render();
}

function findingMap(res) {
  const m = {};
  if (!res) return m;
  for (const z of res.zones) for (const f of z.findings) m[`${z.zone}|${f.item_id}`] = { ...f, zone: z.zone };
  return m;
}

function render() {
  const fm = Object.fromEntries(BACKENDS.map((b) => [b, findingMap(results[b])]));
  // Agreement over items both backends answered (cant_tell counts as an answer).
  let both = 0, same = 0;
  for (const k of Object.keys(fm.local)) {
    if (fm.claude[k]) { both++; if (fm.claude[k].status === fm.local[k].status) same++; }
  }
  $("cards").innerHTML = BACKENDS.map((b) => card(b)).join("") +
    `<div class="card"><h2>Agreement</h2>
      <div class="big">${both ? Math.round((100 * same) / both) + "%" : "–"}</div>
      <div class="hint">${both ? `${same} of ${both} checklist items have the same status` : "Needs results from both backends"}</div>
      <div class="hint" style="margin-top:.4rem">${markSummary()}</div></div>`;
  for (const b of BACKENDS) {
    const btn = document.querySelector(`button[data-run="${b}"]`);
    if (btn) btn.addEventListener("click", () => runBackend(b));
  }
  const zonesDef = (results.local || results.claude)?.zones;
  if (!zonesDef) { $("zones").innerHTML = '<p class="none">No findings yet. Run a backend above.</p>'; return; }
  $("zones").innerHTML = zonesDef.map((z) => zoneBlock(z, fm)).join("");
  document.querySelectorAll(".marks button").forEach((btn) => btn.addEventListener("click", onMark));
}

function card(b) {
  const r = results[b];
  const st = aiStatus?.backends?.[b];
  if (!r) {
    return `<div class="card"><h2>${NAMES[b]}</h2><p class="none">Not run for this property.</p>
      <p class="hint">${esc(st?.detail || "")}</p>
      ${st?.available ? `<button data-run="${b}" type="button">Run ${NAMES[b]}</button>` : ""}
      <p id="run-${b}" class="hint"></p></div>`;
  }
  const t = r.totals;
  return `<div class="card"><h2>${NAMES[b]}</h2><table>
    <tr><td>Model</td><td>${esc(r.model)}</td></tr>
    <tr><td>Latency (4 zones)</td><td>${t.latency_s} s</td></tr>
    <tr><td>Tokens in / out</td><td>${t.input_tokens.toLocaleString()} / ${t.output_tokens.toLocaleString()}</td></tr>
    <tr><td>Cost</td><td>$${t.cost_usd.toFixed(4)}</td></tr>
    <tr><td>Errors</td><td>${t.errors}</td></tr>
    <tr><td>LiDAR disagreements</td><td>${t.lidar_disagreements}</td></tr>
    <tr><td>Run</td><td>${esc(r.finished || r.started)}</td></tr></table>
    ${r.stale ? '<div class="note">⚠ Out of date: the property was re-run after these findings. Re-run before marking.</div>' : ""}
    ${st?.available ? `<button data-run="${b}" type="button" style="margin-top:.4rem">Re-run</button>` : ""}
    <p id="run-${b}" class="hint"></p></div>`;
}

function markSummary() {
  const rows = BACKENDS.map((b) => {
    const ms = Object.values(marks).filter((m) => m.backend === b);
    const ok = ms.filter((m) => m.verdict === "correct").length;
    return ms.length ? `${NAMES[b]}: ${ok}/${ms.length} marked correct` : `${NAMES[b]}: no marks yet`;
  });
  return rows.join("<br>");
}

function zoneBlock(z, fm) {
  const anyRes = results.local || results.claude;
  const zr = anyRes.zones.find((x) => x.zone === z.zone);
  const facts = zr.facts || {};
  const factBits = [
    `${facts.trees_with_trunk_in_zone ?? "?"} LiDAR trees in zone`,
    facts.tallest_in_zone_m ? `tallest ${facts.tallest_in_zone_m} m` : null,
    facts.canopy_cover_pct != null ? `canopy ${facts.canopy_cover_pct}%` : null,
    facts.crowns_touching_home != null ? `${facts.crowns_touching_home} crowns touching house` : null,
    facts.outbuildings_in_zone != null ? `${facts.outbuildings_in_zone} outbuildings` : null,
    facts.share_trees_crown_gap_under_3m != null ? `${Math.round(facts.share_trees_crown_gap_under_3m * 100)}% crowns < 3 m apart` : null,
    facts.mean_slope_deg_30_100m != null ? `slope ${facts.mean_slope_deg_30_100m}°, drop below home ${facts.ground_drop_below_home_m} m` : null,
  ].filter(Boolean).join(" · ");
  const items = (results.local || results.claude).zones.find((x) => x.zone === z.zone).findings.map((f) => f.item_id);
  const rows = items.map((iid) => {
    const k = `${z.zone}|${iid}`;
    const l = fm.local[k], c = fm.claude[k];
    const q = (l || c).question;
    const agree = l && c ? (l.status === c.status ? '<span title="Same status">✓</span>' : '<span title="Different status" style="color:#d7191c">✗</span>') : "–";
    return `<tr><td class="q">${esc(q)}</td><td class="ans">${answer("local", z.zone, iid, l)}</td>
      <td class="ans">${answer("claude", z.zone, iid, c)}</td><td class="agree">${agree}</td></tr>`;
  }).join("");
  const problems = BACKENDS.flatMap((b) => (results[b]?.zones.find((x) => x.zone === z.zone)?.problems || [])
    .map((p) => `${NAMES[b]}: ${esc(p)}`));
  return `<section class="zone"><h2>${esc(z.label)}</h2>
    <div class="zone-grid">
      <div>${z.crop ? `<img src="/api/property/${pid}/crop/${z.zone}.png" alt="${esc(z.label)} crop">` : '<p class="none">No orthophoto</p>'}
        <div class="facts">${esc(factBits)}</div></div>
      <div><table><thead><tr><th>Checklist item</th><th>${NAMES.local}</th><th>${NAMES.claude}</th><th>Agree</th></tr></thead>
        <tbody>${rows}</tbody></table>
        ${problems.length ? `<p class="problems">Validator: ${problems.join("<br>")}</p>` : ""}</div>
    </div></section>`;
}

function answer(b, zone, iid, f) {
  if (!f) return '<span class="none">not run</span>';
  const m = marks[`${b}|${zone}|${iid}`];
  const on = (v) => (m?.verdict === v ? "on" : "");
  return `<span class="chip ${f.status}">${STATUS[f.status]}</span><span class="conf">${Math.round(f.confidence * 100)}%</span>
    <div class="reason">${esc(f.reason)}</div>
    <div class="lidar ${f.lidar.result === "disagree" ? "disagree" : ""}">LiDAR ${esc(f.lidar.result)}: ${esc(f.lidar.note)}</div>
    <div class="marks" data-b="${b}" data-zone="${zone}" data-item="${iid}" data-status="${f.status}">
      <button type="button" class="correct ${on("correct")}" data-v="correct">✓ correct</button>
      <button type="button" class="wrong ${on("wrong")}" data-v="wrong">✗ wrong</button></div>`;
}

async function onMark(e) {
  const box = e.currentTarget.parentElement;
  const { b, zone, item, status } = box.dataset;
  const key = `${b}|${zone}|${item}`;
  const v = e.currentTarget.dataset.v;
  const verdict = marks[key]?.verdict === v ? null : v; // click again to clear
  const body = { property_id: pid, backend: b, zone, item_id: item, verdict, model: results[b].model,
    model_status: status, checklist_version: results[b].checklist_version };
  const r = await fetch("/api/marks", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!r.ok) { $("status").textContent = "Could not save mark"; return; }
  if (verdict) marks[key] = body; else delete marks[key];
  box.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x.dataset.v === verdict));
  $("status").textContent = "Mark saved";
  render();
}

async function runBackend(b) {
  const msg = $(`run-${b}`);
  const r = await fetch("/api/ai/run", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: pid, backend: b }) });
  if (!r.ok) { msg.textContent = `Error: ${(await r.json()).detail}`; return; }
  for (;;) {
    const j = await getJson(`/api/ai/job/${pid}/${b}`);
    const el = $(`run-${b}`);
    if (el) el.textContent = j.message;
    if (j.status === "done") return load(pid);
    if (j.status === "error") { if (el) el.textContent = `Error: ${j.message}`; return; }
    await new Promise((res) => setTimeout(res, 1500));
  }
}

init().catch((e) => { $("status").textContent = e.message; console.error(e); });
