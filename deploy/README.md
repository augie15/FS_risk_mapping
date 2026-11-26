# Deploying on the home server

Target: the app at `https://fsmapping.augiesrentals.ca`, behind a password, for one coworker.
It is a prototype, not a public site.

```
browser ──HTTPS──> Caddy (TLS + basic_auth, strips Authorization) ──HTTP──> 127.0.0.1:8800 uvicorn
                                                                            (user fsmap)
                                                                             │
                                                    /srv/fsmapping/data  <───┤ the only writable path
                                     Ollama on the Mac, over WireGuard  <────┘ (optional)
```

- The app listens on **127.0.0.1:8800 only** and has no login of its own. The password check is
  Caddy's `basic_auth`; that Caddy site block is kept in the ops repo, not here. Caddy strips the
  `Authorization` header before proxying, and the app never logs request headers (uvicorn's access
  log records method, path and status only).
- The frontend uses relative URLs only (`api/...`, `style.css`), so it works behind the proxy
  and under basic auth without changes.

### Isolation from the rest of the server

The app is reachable from the internet behind one shared password, and the ops checkouts in
`/home/augie` hold production secrets. So the app runs as its own system user, **`fsmap`**, with no
login shell, and the systemd unit hides `/home` from it entirely (`ProtectHome=true`). Any
file-read or path bug in the app cannot reach another project's `.env`.

| Path | Owner | Service access |
|---|---|---|
| `/srv/fsmapping/app` (code checkout, `.venv`, `.env`) | `fsmap` | read-only |
| `/srv/fsmapping/data` (LiDAR cache, results, labels, AI log) | `fsmap` | read-write, the only writable path |
| `/home`, `/root`, `/run/user` | — | invisible |

The app finds its data through `FS_DATA_DIR=/srv/fsmapping/data`, set in the unit file. Other
hardening in the unit: `ProtectSystem=strict`, `NoNewPrivileges`, `PrivateTmp`, `PrivateDevices`,
`RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX`, no capabilities.

Commands below are for the home server: Ubuntu 26.04 LTS, x86_64, system Python 3.14. Run them as
an admin user with sudo; never run the app as that user.

## 1. System packages

```bash
sudo apt update
sudo apt install -y git python3 python3-venv ca-certificates curl
python3 --version          # Python 3.14.x on 26.04
```

The app runs on the system Python 3.14 (also tested on 3.12). No PPA or extra Python is needed.

No GDAL, PROJ or LAZ system libraries are needed. The pip wheels bring them:

| Need | Comes from |
|---|---|
| GeoTIFF / HTTP orthophoto reads (GDAL 3.12) | `rasterio` wheel |
| Coordinate transforms (PROJ) | `pyproj` wheel |
| Geometry (GEOS) | `shapely` wheel |
| LAZ point clouds | `laspy` + `lazrs` (Rust LAZ decoder) wheels |
| PDF reports | `reportlab` wheel |

Every pinned package and dependency has a prebuilt manylinux x86_64 wheel for CPython 3.14
(checked 2026-11-26 with `pip download --only-binary=:all:` for cp314; 49 wheels, none built
from source). The install line below passes `--only-binary=:all:`, so pip fails loudly instead of
trying to compile something if a wheel is ever missing.

## 2. User, code, virtual environment and settings

```bash
sudo useradd --system --create-home --home-dir /srv/fsmapping --shell /usr/sbin/nologin fsmap
sudo chmod 750 /srv/fsmapping
sudo -u fsmap git clone https://github.com/augie15/FS_risk_mapping.git /srv/fsmapping/app
sudo -u fsmap git -C /srv/fsmapping/app checkout feature/risk-score-report
sudo -u fsmap python3 -m venv /srv/fsmapping/app/.venv
sudo -u fsmap /srv/fsmapping/app/.venv/bin/pip install --upgrade pip
sudo -u fsmap /srv/fsmapping/app/.venv/bin/pip install --only-binary=:all: \
    -r /srv/fsmapping/app/requirements.txt
sudo -u fsmap /srv/fsmapping/app/.venv/bin/python -c "import rasterio, laspy, lazrs, pyproj, shapely, reportlab; print('ok')"
sudo -u fsmap mkdir -p /srv/fsmapping/data       # must exist before the service starts
sudo -u fsmap install -m 600 /srv/fsmapping/app/.env.example /srv/fsmapping/app/.env
```

Edit `/srv/fsmapping/app/.env` with `sudo -u fsmap nano /srv/fsmapping/app/.env` (never commit it,
and never copy keys into it from the ops `.env` files; use a separate Anthropic key with its own
spend limit):

| Setting | Server value | Notes |
|---|---|---|
| `GOOGLE_MAPS_API_KEY` | the Maps JS key | Without it a Leaflet map with Esri imagery is used |
| `ANTHROPIC_API_KEY` | Claude API key | Optional; without it only local AI is offered |
| `FS_CLAUDE_DAILY_CAP_USD` | `5` | Hard daily cap on Claude spend (US$); calls are refused before sending |
| `FS_AI_RUN_CAP_USD` | `0.50` | Cap per property run |
| `OLLAMA_HOST` | the Mac's WireGuard address, e.g. `10.10.0.x:11434` | Optional, see below |

### Data

Everything the app writes is under `/srv/fsmapping/data`:

- `raw/lidar/` LidarBC LAZ tiles, about 100–300 MB each, downloaded on first use and reused.
  The Telkwa test area used 1.1 GB. Budget a few GB per area of interest; there is 68 GB free.
- `raw/buildings/` Microsoft footprint tiles (small).
- `processed/<id>/` per-property results (~10–60 MB each).
- `outlines.json` operator-confirmed home outlines.
- `labels.sqlite` correct/wrong marks; `ai_calls.jsonl` every AI call with its cost
  (the daily cap is summed from this file, so do not delete it mid-day).

Back up `outlines.json`, `labels.sqlite` and `ai_calls.jsonl`; the rest can be re-downloaded or
recomputed. To move existing results from the Mac, copy the Mac's `data/` into a staging folder
you own on the server, then `sudo rsync -a --chown=fsmap:fsmap <staging>/ /srv/fsmapping/data/`.

### Local AI (Ollama on the Mac)

The server reaches Ollama on the Mac over WireGuard, so local AI works **only while the Mac is
awake**. When it is not answering, the app shows "Local AI offline" (not an error) and offers Claude
only. On the Mac, Ollama must listen on the WireGuard interface, not just localhost:
`launchctl setenv OLLAMA_HOST 0.0.0.0:11434` then restart Ollama, and allow only the WireGuard
subnet in the Mac firewall. Check from the server: `curl -s http://<mac-wg-ip>:11434/api/tags`.

## 3. Service

```bash
sudo cp /srv/fsmapping/app/deploy/fsmapping.service /etc/systemd/system/fsmapping.service
sudo systemd-analyze verify /etc/systemd/system/fsmapping.service
sudo systemctl daemon-reload
sudo systemctl enable --now fsmapping
systemctl status fsmapping --no-pager
journalctl -u fsmapping -f            # logs
```

The unit runs uvicorn as `fsmap` with one worker, no `--reload`, bound to 127.0.0.1:8800, with
write access only to `/srv/fsmapping/data`. To update:
`sudo -u fsmap git -C /srv/fsmapping/app pull`, then (if `requirements.txt` changed) the pip
install lines above, then `sudo systemctl restart fsmapping`. Restart after any `.env` change too
(the Claude key check is cached per process).

## 4. Final checks (on the server)

```bash
curl -s http://127.0.0.1:8800/api/health            # {"ok":true}
curl -s http://127.0.0.1:8800/api/ai/status | python3 -m json.tool
ss -ltnp | grep 8800                                 # must show 127.0.0.1:8800, never 0.0.0.0
systemctl show fsmapping -p User                     # User=fsmap

# fsmap must not be able to read other projects' secrets. Each of these must FAIL:
sudo -u fsmap cat /home/augie/augies-rentals-ops/.env
sudo -u fsmap cat /home/augie/augies-rentals-dev/.env
# and the running service cannot even see /home (expect "No such file or directory" or empty):
sudo systemd-run --pipe --wait -p User=fsmap -p ProtectHome=true ls /home/augie
systemd-analyze security fsmapping --no-pager | tail -1   # exposure score; lower is better
```

The `sudo -u fsmap cat` check relies on ordinary file permissions (`/home/augie` should be mode
750 or 700, and the `.env` files 600). If it succeeds, fix those permissions first: the unit's
`ProtectHome=true` protects the running service, but a permission hole would still be a hole.

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
