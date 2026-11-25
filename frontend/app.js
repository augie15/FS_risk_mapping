// Loads the Google Maps JS API with the key from /api/config and opens a satellite view.
const DEFAULT_CENTER = { lat: 54.6667, lng: -127.05 }; // Telkwa, BC
const statusEl = document.getElementById("status");

async function main() {
  const cfg = await (await fetch("/api/config")).json();
  if (!cfg.googleMapsApiKey) {
    statusEl.textContent = "No Google Maps key";
    document.getElementById("map").innerHTML =
      '<div class="notice"><h2>Google Maps key missing</h2><p>Set <code>GOOGLE_MAPS_API_KEY</code> in <code>.env</code> and restart the server.</p></div>';
    return;
  }
  await loadMapsApi(cfg.googleMapsApiKey);
  const { Map } = await google.maps.importLibrary("maps");
  const map = new Map(document.getElementById("map"), {
    center: DEFAULT_CENTER,
    zoom: 13,
    mapTypeId: "hybrid",
    mapId: cfg.googleMapsMapId || undefined,
    tilt: 0,
  });
  window.fsMap = map;
  statusEl.textContent = "Ready";
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

main().catch((e) => { statusEl.textContent = e.message; console.error(e); });
