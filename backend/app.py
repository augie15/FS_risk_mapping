"""FS Risk Mapping: FastAPI app serving the map frontend and its API."""
import json
import os
import re
import threading
import traceback
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from pipeline.property import PROCESSED, assess, property_id

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

app = FastAPI(title="FS Risk Mapping")

# In-memory job table. One prototype user, one process: no queue needed.
_jobs: dict[str, dict] = {}
_lock = threading.Lock()
LAYERS = {"parcel", "buildings", "zones", "trees"}
ID_RE = re.compile(r"^[0-9a-f]{12}$")


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


@app.get("/api/config")
def config() -> dict:
    """Browser-side config. The Maps JS key is public by design; restrict it by referrer."""
    return {
        "googleMapsApiKey": os.getenv("GOOGLE_MAPS_API_KEY", ""),
        "googleMapsMapId": os.getenv("GOOGLE_MAPS_MAP_ID", "") or None,
    }


class AssessRequest(BaseModel):
    lat: float = Field(ge=48.0, le=60.5)      # British Columbia
    lng: float = Field(ge=-139.5, le=-114.0)
    force: bool = False


def _run(pid: str, req: AssessRequest) -> None:
    def progress(msg: str) -> None:
        with _lock:
            _jobs[pid]["message"] = msg

    try:
        summary = assess(req.lat, req.lng, progress=progress, force=req.force)
        with _lock:
            _jobs[pid].update(status="done", message="Done", summary=summary)
    except Exception as e:  # report to the UI instead of dying silently
        traceback.print_exc()
        with _lock:
            _jobs[pid].update(status="error", message=str(e))


@app.post("/api/assess")
def start_assessment(req: AssessRequest) -> dict:
    """Start (or reuse) the one-property pipeline. Poll GET /api/assess/{id}."""
    pid = property_id(req.lat, req.lng)
    with _lock:
        job = _jobs.get(pid)
        if job and job["status"] == "running":
            return {"id": pid, **_public(job)}
        _jobs[pid] = {"status": "running", "message": "Queued", "summary": None}
    threading.Thread(target=_run, args=(pid, req), daemon=True).start()
    return {"id": pid, "status": "running", "message": "Queued"}


def _public(job: dict) -> dict:
    return {"status": job["status"], "message": job["message"], "summary": job["summary"]}


def _check_id(pid: str) -> Path:
    if not ID_RE.match(pid):
        raise HTTPException(400, "bad id")
    return PROCESSED / pid


@app.get("/api/assess/{pid}")
def assessment_status(pid: str) -> dict:
    out = _check_id(pid)
    with _lock:
        job = _jobs.get(pid)
        if job:
            return {"id": pid, **_public(job)}
    if (out / "summary.json").exists():
        return {"id": pid, "status": "done", "message": "Done",
                "summary": json.loads((out / "summary.json").read_text())}
    raise HTTPException(404, "unknown assessment")


@app.get("/api/property/{pid}/{layer}.geojson")
def property_layer(pid: str, layer: str):
    out = _check_id(pid)
    if layer not in LAYERS or not (out / f"{layer}.geojson").exists():
        raise HTTPException(404, "no such layer")
    return FileResponse(out / f"{layer}.geojson", media_type="application/geo+json")


@app.get("/api/property/{pid}/ortho.png")
def property_ortho(pid: str):
    out = _check_id(pid)
    if not (out / "ortho.png").exists():
        raise HTTPException(404, "no orthophoto for this property")
    return FileResponse(out / "ortho.png", media_type="image/png")


app.mount("/", StaticFiles(directory=ROOT / "frontend", html=True), name="frontend")
