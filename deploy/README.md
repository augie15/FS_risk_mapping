# Deploying on the home server

Target: the app at `https://fsmapping.augiesrentals.ca`, behind a password, for one coworker.
It is a prototype, not a public site.

```
browser ──HTTPS──> Caddy (TLS + basic_auth) ──HTTP──> 127.0.0.1:8800 uvicorn (this app)
                                                       │
                                                       ├─ data/ (LiDAR cache, results, labels)
                                                       └─ Ollama on the Mac, over WireGuard (optional)
```

- The app listens on **127.0.0.1:8800 only** and has no login of its own. The password check is
  Caddy's `basic_auth`; that Caddy site block is kept in the ops repo, not here.
- The frontend uses relative URLs only (`api/...`, `style.css`), so it works behind the proxy
  and under basic auth without changes.

Commands below assume Ubuntu 24.04, user `augie`, checkout at `/home/augie/FS_risk_mapping`.
If you use another path or user, change `deploy/fsmapping.service` to match.

## 1. System packages

```bash
sudo apt update
sudo apt install -y git python3.12 python3.12-venv python3-pip ca-certificates curl
```

On Ubuntu 22.04 Python 3.12 is not in the main archive:
`sudo add-apt-repository ppa:deadsnakes/ppa && sudo apt install -y python3.12 python3.12-venv`.

No GDAL, PROJ or LAZ system libraries are needed. The pip wheels bring them:

| Need | Comes from |
|---|---|
| GeoTIFF / HTTP orthophoto reads (GDAL 3.12) | `rasterio` wheel |
| Coordinate transforms (PROJ) | `pyproj` wheel |
| Geometry (GEOS) | `shapely` wheel |
| LAZ point clouds | `laspy` + `lazrs` (Rust LAZ decoder) wheels |
| PDF reports | `reportlab` wheel |

These wheels exist for x86_64 and aarch64 Linux on Python 3.12. If `pip` ever starts building
one of them from source, stop: that means the Python version is wrong.

## 2. Code, virtual environment and settings

```bash
cd /home/augie
git clone https://github.com/augie15/FS_risk_mapping.git
cd FS_risk_mapping
git checkout deploy/home-server        # or the branch you are deploying
python3.12 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
mkdir -p data                           # must exist before the service starts (ReadWritePaths)
cp .env.example .env && chmod 600 .env
```

Edit `.env` (never commit it):

| Setting | Server value | Notes |
|---|---|---|
| `GOOGLE_MAPS_API_KEY` | the Maps JS key | Without it a Leaflet map with Esri imagery is used |
| `ANTHROPIC_API_KEY` | Claude API key | Optional; without it only local AI is offered |
| `FS_CLAUDE_DAILY_CAP_USD` | `5` | Hard daily cap on Claude spend (US$); calls are refused before sending |
| `FS_AI_RUN_CAP_USD` | `0.50` | Cap per property run |
| `OLLAMA_HOST` | the Mac's WireGuard address, e.g. `10.10.0.x:11434` | Optional, see below |

### Data

Everything the app writes is under `data/` in the checkout (git-ignored):

- `data/raw/lidar/` LidarBC LAZ tiles, about 100–300 MB each, downloaded on first use and reused.
  The Telkwa test area used 1.1 GB. Budget a few GB per area of interest; there is 68 GB free.
- `data/raw/buildings/` Microsoft footprint tiles (small).
- `data/processed/<id>/` per-property results (~10–60 MB each).
- `data/outlines.json` operator-confirmed home outlines.
- `data/labels.sqlite` correct/wrong marks; `data/ai_calls.jsonl` every AI call with its cost
  (the daily cap is summed from this file, so do not delete it mid-day).

Back up `data/outlines.json`, `data/labels.sqlite` and `data/ai_calls.jsonl`; the rest can be
re-downloaded or recomputed. To move existing results from the Mac, copy `data/processed/` and
those three files across (`rsync -a data/ augie@10.10.0.1:FS_risk_mapping/data/`).

### Local AI (Ollama on the Mac)

The server reaches Ollama on the Mac over WireGuard, so local AI works **only while the Mac is
awake**. When it is not answering, the app shows "Local AI offline" (not an error) and offers Claude
only. On the Mac, Ollama must listen on the WireGuard interface, not just localhost:
`launchctl setenv OLLAMA_HOST 0.0.0.0:11434` then restart Ollama, and allow only the WireGuard
subnet in the Mac firewall. Check from the server: `curl -s http://<mac-wg-ip>:11434/api/tags`.

## 3. Service

```bash
sudo cp deploy/fsmapping.service /etc/systemd/system/fsmapping.service
sudo systemctl daemon-reload
sudo systemctl enable --now fsmapping
systemctl status fsmapping --no-pager
journalctl -u fsmapping -f            # logs
```

The unit runs uvicorn as `augie` with one worker, no `--reload`, bound to 127.0.0.1:8800, with
write access only to `data/`. After a `git pull` or a `.env` change:
`sudo systemctl restart fsmapping` (the Claude key check is cached per process).

## 4. Final check (on the server)

```bash
curl -s http://127.0.0.1:8800/api/health            # {"ok":true}
curl -s http://127.0.0.1:8800/api/ai/status | python3 -m json.tool
ss -ltnp | grep 8800                                 # must show 127.0.0.1:8800, never 0.0.0.0
```

Then, once the Caddy site is live, from outside: `https://fsmapping.augiesrentals.ca` asks for the
password, and `curl -I https://fsmapping.augiesrentals.ca/api/health` without credentials gives 401.

## Google Maps key

The Maps JavaScript key is sent to every browser by design, so it must be restricted in Google
Cloud Console → APIs & Services → Credentials → the key:

- Application restriction: **Websites**, with referrers
  `https://fsmapping.augiesrentals.ca/*`, `http://127.0.0.1:8800/*` and `http://localhost:8800/*`
  (development).
- API restriction: Maps JavaScript API only.

Google Maps imagery is display only. All analysis uses open BC government data.
