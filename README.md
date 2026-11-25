# FS Risk Mapping

LiDAR and satellite imagery processing platform with a Google Maps front end.

## Run locally

```bash
make setup     # creates .venv, installs deps, copies .env.example -> .env
# put your key in .env: GOOGLE_MAPS_API_KEY=...
make run       # http://127.0.0.1:8800
```

## Layout

- `backend/` FastAPI app (`/api/health`, `/api/config`, serves the frontend)
- `frontend/` plain HTML/JS, no build step; Google Maps JS API loaded at runtime
- `data/raw`, `data/processed` local LiDAR/imagery (git-ignored)

Never commit `.env`: this repository is public.
