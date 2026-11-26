# FS Risk Mapping

Prototype of a digital home wildfire risk assessment for the Bulkley Valley, based on
FireSmart BC guidance (not an official FireSmart assessment). Click a home on the map to get its
parcel, Home Ignition Zones and LiDAR-detected trees.

## Run locally

Needs Python 3.12 or 3.14 (`brew install python@3.12`); the Makefile uses 3.12 by default
(`make setup PYTHON=python3.14` for 3.14).

```bash
make setup     # creates .venv with python3.12, installs deps, copies .env.example -> .env
# optional: put a key in .env: GOOGLE_MAPS_API_KEY=...  (without one, a Leaflet map is used)
make run       # http://127.0.0.1:8800
```

Command-line run for one property (prints a summary, writes `data/processed/<id>/`):

```bash
make assess LAT=54.69 LNG=-127.05
```

Running it on the home server behind Caddy: [deploy/README.md](deploy/README.md).

Tests (offline, synthetic data): `.venv/bin/pip install pytest && .venv/bin/python -m pytest tests`

## What the pipeline does

1. **Parcel** from ParcelMap BC (WFS), the polygon containing the clicked point.
2. **Home outline**, in this order:
   - an **operator-confirmed** outline, if one was saved for this house. Edit it on the map as
     rectangles: corner and edge squares resize, the diamond moves (and selects) a rectangle, the
     yellow dot rotates the outline; add or remove rectangles for L-shapes and garages. Stored in
     `data/outlines.json` (git-ignored);
   - otherwise a **LiDAR roof trace** at the click (`pipeline/roof.py`): raised points that are
     locally planar and single-return, the roof patch under the click. Reported with point count
     and a confidence. Microsoft footprints are only a cross-check (overlap %), never the outline;
   - every outline is **rectilinear** (`pipeline/rects.py`): one rectangle along the roof's
     dominant direction, or a union of 2-3 same-orientation rectangles when one rectangle would be
     less than ~80% roof (L/T shapes, attached garages). Footprint suggestions are fitted the same way;
   - otherwise **"Outline needed"**: no zones or trees are computed until you draw and save one.
3. **Home Ignition Zones** buffered from the home outline: Immediate 0–1.5 m,
   Intermediate 1.5–10 m, Extended 10–30 m, and 30–100 m. Not clipped to the parcel.
4. **Orthophoto**: the matching LidarBC 2019 15 cm tile(s), read as a window over HTTP and
   clipped to the 100 m zone (`ortho.tif`, plus `ortho.png` for the map).
5. **Trees**: LidarBC point cloud -> ground model -> canopy height model (0.5 m) with roofs
   masked out -> tree tops (local maxima ≥ 3 m) -> crowns (watershed). Each tree gets height,
   crown radius, distance from trunk and crown edge to the home, and its zone.

Data sources, licences and coverage: [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md).
Google Maps imagery is display only; all analysis uses open BC data.

## AI findings per zone (step 2)

Each ignition zone's orthophoto crop, its LiDAR facts and a fixed checklist
([config/firesmart_checklist.json](config/firesmart_checklist.json), sources in
[docs/FIRESMART_CHECKLIST.md](docs/FIRESMART_CHECKLIST.md)) go to a vision model, either local
(Ollama, `qwen3-vl:8b-instruct`) or the Claude API. Every answer is re-checked in Python against
the schema and the LiDAR measurements. Pins show on the map; `/compare.html` puts both models side
by side and lets you mark answers correct or wrong (saved to `data/labels.sqlite`).
Details, spend cap and settings: [docs/AI_FINDINGS.md](docs/AI_FINDINGS.md).

Add these to your `.env` (and blank to `.env.example`) to use Claude:

```
ANTHROPIC_API_KEY=
FS_AI_RUN_CAP_USD=0.50
```

## API

- `POST /api/assess` `{lat, lng}` starts a run; `GET /api/assess/{id}` polls status and summary
- `GET /api/property/{id}/{parcel|buildings|zones|trees}.geojson`, `GET /api/property/{id}/ortho.png`
- `GET /api/ai/status`; `POST /api/ai/run` `{id, backend}`; `GET /api/ai/job/{id}/{backend}`;
  `GET /api/ai/{id}/{backend}`; `GET /api/property/{id}/crop/{zone}.png`
- `GET /api/marks/{id}`, `POST /api/marks` (correct / wrong per finding)

## Known limits

- LiDAR and 15 cm orthophotos cover the 2019/2021 floodplain flights (valley floor). Elsewhere
  you get parcel and zones only, or older LiDAR without an orthophoto.
- LiDAR tiles are not cloud-optimised: the first run in a new area downloads whole tiles
  (200–650 MB each, up to 4 at a tile corner) into `data/raw/lidar/`.
- Roof traces follow the LiDAR roof, which is the true position; the 2019 photos lean, so roofs
  in the image can look 1-3 m off. Check low-confidence traces and save a corrected outline.
- Trees under 3 m (shrubs) are not counted. Points next to a house can still be eaves,
  chimneys or attached decks; check them against the orthophoto.
- Orthophotos are not true-ortho, so tall trees lean in the image relative to LiDAR positions.
- ParcelMap BC lines in rural areas can be several metres off the ground truth.

## Layout

- `backend/` FastAPI app and job endpoints; serves the frontend
- `pipeline/` data fetchers (`sources.py`), LiDAR processing (`lidar.py`), one-property run (`property.py`)
- `pipeline/ai/` zone crops and facts, prompt and schema, backends, validator, marks
- `config/` the versioned FireSmart checklist
- `frontend/` plain HTML/JS, no build step; Google Maps if a key is set, otherwise Leaflet
- `data/raw`, `data/processed` downloads and outputs (git-ignored)

Never commit `.env` or anything under `data/`: this repository is public.

## Risk score, PDF report and GIS downloads (step 3)

Each assessed property gets a score out of 100 (Low / Moderate / High / Extreme): up to 80 points
from fixed rules on LiDAR measurements per zone, and up to 20 from AI findings on things LiDAR
cannot see, counted only when the operator marked them correct or both models agree. The map panel
shows the score and the three things to fix first, a PDF report (map with zone rings and numbered
findings), and GIS downloads: outline, zones and findings as GeoJSON; canopy height and slope as
GeoTIFF. Rules: [docs/RISK_SCORE.md](docs/RISK_SCORE.md).

