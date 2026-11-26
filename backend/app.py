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

from pipeline import outlines
from pipeline.property import PROCESSED, assess, property_id

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=True)  # .env wins over keys inherited from the shell

from pipeline.ai import backends as ai_backends  # noqa: E402  (reads model settings from .env)
from pipeline.ai import checklist as ai_checklist  # noqa: E402
from pipeline.ai import marks as ai_marks  # noqa: E402
from pipeline.ai import runner as ai_runner  # noqa: E402

app = FastAPI(title="FS Risk Mapping")

# In-memory job table. One prototype user, one process: no queue needed.
_jobs: dict[str, dict] = {}
_lock = threading.Lock()
LAYERS = {"parcel", "buildings", "zones", "trees", "outline"}
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


def _run(pid: str, req: "AssessRequest") -> None:
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
    return _start(property_id(req.lat, req.lng), req)


def _start(pid: str, req: "AssessRequest") -> dict:
    with _lock:
        job = _jobs.get(pid)
        if job and job["status"] == "running":
            return {"id": pid, **_public(job)}
        _jobs[pid] = {"status": "running", "message": "Queued", "summary": None}
    threading.Thread(target=_run, args=(pid, req), daemon=True).start()
    return {"id": pid, "status": "running", "message": "Queued"}


class OutlineRequest(BaseModel):
    coordinates: list[list[float]] = Field(min_length=3, max_length=500)  # [[lng, lat], ...]
    note: str | None = Field(default=None, max_length=300)


def _summary(pid: str) -> dict:
    path = _check_id(pid) / "summary.json"
    if not path.exists():
        raise HTTPException(404, "unknown property")
    return json.loads(path.read_text())


@app.get("/api/property/{pid}/outline.geojson")
def property_outline(pid: str):
    out = _check_id(pid)
    if not (out / "outline.geojson").exists():
        raise HTTPException(404, "no outline")
    return FileResponse(out / "outline.geojson", media_type="application/geo+json")


@app.post("/api/property/{pid}/outline")
def save_outline(pid: str, req: OutlineRequest) -> dict:
    """Save an operator-confirmed home outline and recompute the property from it."""
    s = _summary(pid)
    click = s["click"]
    try:
        outlines.save(pid, req.coordinates, click["lng"], click["lat"], req.note)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _start(pid, AssessRequest(lat=click["lat"], lng=click["lng"], force=True))


@app.delete("/api/property/{pid}/outline")
def clear_outline(pid: str) -> dict:
    """Drop the confirmed outline and go back to the automatic LiDAR trace."""
    s = _summary(pid)
    click = s["click"]
    if not outlines.delete(pid, click["lng"], click["lat"]):
        raise HTTPException(404, "no confirmed outline for this property")
    return _start(pid, AssessRequest(lat=click["lat"], lng=click["lng"], force=True))


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


# ---------------------------------------------------------------- AI findings

BACKENDS = ("local", "claude")
_ai_jobs: dict[str, dict] = {}


@app.get("/api/ai/status")
def ai_status() -> dict:
    """Which backends can run. Never returns the key itself."""
    out = {}
    for name in BACKENDS:
        be = ai_backends.get(name)
        ok, why = be.available()
        out[name] = {"available": ok, "detail": why, "model": be.model}
    return {"backends": out, "checklist_version": ai_checklist.load()["version"]}


@app.get("/api/ai/checklist")
def ai_checklist_json() -> dict:
    return ai_checklist.load()


class AIRunRequest(BaseModel):
    id: str
    backend: str


def _ai_run(key: str, pid: str, backend: str) -> None:
    def progress(msg: str) -> None:
        with _lock:
            _ai_jobs[key]["message"] = msg
    try:
        ai_runner.run(pid, backend, progress=progress)
        with _lock:
            _ai_jobs[key].update(status="done", message="Done")
    except Exception as e:
        traceback.print_exc()
        with _lock:
            _ai_jobs[key].update(status="error", message=str(e))


@app.post("/api/ai/run")
def ai_run(req: AIRunRequest) -> dict:
    out = _check_id(req.id)
    if req.backend not in BACKENDS:
        raise HTTPException(400, "unknown backend")
    if not (out / "summary.json").exists():
        raise HTTPException(404, "run the property assessment first")
    if json.loads((out / "summary.json").read_text()).get("status") == "outline_needed":
        raise HTTPException(409, "outline needed: draw and save the home outline first")
    ok, why = ai_backends.get(req.backend).available()
    if not ok:
        raise HTTPException(409, why)
    key = f"{req.id}:{req.backend}"
    with _lock:
        if _ai_jobs.get(key, {}).get("status") == "running":
            return {"key": key, **_ai_jobs[key]}
        _ai_jobs[key] = {"status": "running", "message": "Queued"}
    threading.Thread(target=_ai_run, args=(key, req.id, req.backend), daemon=True).start()
    return {"key": key, "status": "running", "message": "Queued"}


@app.get("/api/ai/job/{pid}/{backend}")
def ai_job(pid: str, backend: str) -> dict:
    _check_id(pid)
    with _lock:
        job = _ai_jobs.get(f"{pid}:{backend}")
    return job or {"status": "idle", "message": ""}


@app.get("/api/ai/{pid}/{backend}")
def ai_result(pid: str, backend: str):
    out = _check_id(pid)
    if backend not in BACKENDS or not (out / "ai" / f"{backend}.json").exists():
        raise HTTPException(404, "no findings yet")
    res = json.loads((out / "ai" / f"{backend}.json").read_text())
    summ = json.loads((out / "summary.json").read_text())
    # Findings must come from the property run that is on disk now (same trees, zones, crops).
    res["stale"] = (res.get("property_run_id") != summ.get("run_id")
                    or res.get("pipeline_version") != summ.get("pipeline_version"))
    return res


@app.get("/api/property/{pid}/crop/{zone}.png")
def property_crop(pid: str, zone: str):
    out = _check_id(pid)
    if zone not in {z["zone"] for z in ai_checklist.load()["zones"]}:
        raise HTTPException(404, "no such zone")
    path = out / "ai" / f"crop_{zone}.png"
    if not path.exists():
        raise HTTPException(404, "no crop")
    return FileResponse(path, media_type="image/png")


@app.get("/api/properties")
def properties() -> list[dict]:
    """Processed properties (for the comparison page). Ids only, plus what has been run."""
    rows = []
    for d in sorted(PROCESSED.glob("*/summary.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        s = json.loads(d.read_text())
        if s.get("status") == "outline_needed":
            continue  # nothing to compare until an outline is confirmed
        rows.append({"id": s["id"], "trees": s["trees"]["count"], "home": s["home"]["source"],
                     "ai": [b for b in BACKENDS if (d.parent / "ai" / f"{b}.json").exists()]})
    return rows


class Mark(BaseModel):
    property_id: str
    backend: str
    zone: str
    item_id: str
    verdict: str | None = Field(default=None, pattern="^(correct|wrong)$")  # None clears the mark
    model: str | None = None
    model_status: str | None = None
    true_status: str | None = Field(default=None, pattern="^(ok|concern|cant_tell)$")
    note: str | None = Field(default=None, max_length=500)
    checklist_version: str | None = None


@app.get("/api/marks/{pid}")
def get_marks(pid: str) -> list[dict]:
    _check_id(pid)
    return ai_marks.for_property(pid)


@app.post("/api/marks")
def put_mark(m: Mark) -> dict:
    _check_id(m.property_id)
    if m.backend not in BACKENDS or m.item_id not in ai_checklist.items_by_id():
        raise HTTPException(400, "unknown backend or item")
    if m.verdict is None:
        ai_marks.delete(m.property_id, m.backend, m.zone, m.item_id)
    else:
        ai_marks.put(m.model_dump())
    return {"ok": True}


app.mount("/", StaticFiles(directory=ROOT / "frontend", html=True), name="frontend")
