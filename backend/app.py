"""FS Risk Mapping: FastAPI app serving the map frontend and its API."""
import json
import os
import re
import threading
import traceback
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import pyproj
from shapely.geometry import mapping
from shapely.ops import transform as shp_transform

from pipeline import gis, outlines, report, score
from pipeline import rects as ai_rects
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
    # Rectangles as lon/lat rings: [[[lng, lat] x 4-5] per rectangle]
    rects: list[list[list[float]]] = Field(min_length=1, max_length=6)
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
    epsg = int(s["crs"].split(":")[1])
    to_u = pyproj.Transformer.from_crs(4326, epsg, always_xy=True).transform
    to_ll = pyproj.Transformer.from_crs(epsg, 4326, always_xy=True).transform
    try:
        if any(len(r) not in (4, 5) for r in req.rects):
            raise ValueError("each rectangle needs 4 corners")
        union_u = ai_rects.union_of([[to_u(*c[:2]) for c in r] for r in req.rects])
        outlines.save(pid, mapping(shp_transform(to_ll, union_u)), req.rects, click["lng"], click["lat"], req.note)
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


# ---------------------------------------------------------------- risk score, report, GIS downloads

def _score_or_error(pid: str) -> dict:
    out = _check_id(pid)
    if not (out / "summary.json").exists():
        raise HTTPException(404, "run the property assessment first")
    try:
        return score.compute(pid)
    except ValueError as e:      # outline needed
        raise HTTPException(409, str(e)) from e


@app.get("/api/property/{pid}/score")
def property_score(pid: str) -> dict:
    return _score_or_error(pid)


@app.get("/api/property/{pid}/report.pdf")
def property_report(pid: str):
    sc = _score_or_error(pid)
    pdf = report.build_pdf(pid, sc)
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="wildfire_report_{pid}.pdf"'})


DOWNLOADS = {"outline.geojson", "zones.geojson", "findings.geojson", "chm.tif", "slope.tif"}


@app.get("/api/property/{pid}/download/{name}")
def property_download(pid: str, name: str):
    if name not in DOWNLOADS:
        raise HTTPException(404, "unknown download")
    out = _check_id(pid)
    disp = {"Content-Disposition": f'attachment; filename="{pid}_{name}"'}
    if name == "findings.geojson":
        return JSONResponse(gis.findings_geojson(_score_or_error(pid)), media_type="application/geo+json",
                            headers=disp)
    if name == "slope.tif":
        if not (out / "dtm.tif").exists():
            raise HTTPException(404, "no ground model for this property")
        return FileResponse(gis.slope_tif(pid), media_type="image/tiff", headers=disp)
    path = out / name
    if not path.exists():
        raise HTTPException(404, f"no {name} for this property")
    return FileResponse(path, media_type="image/tiff" if name.endswith(".tif") else "application/geo+json",
                        headers=disp)


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
        state = "ready" if ok else "offline" if getattr(be, "offline", False) else "unavailable"
        out[name] = {"available": ok, "state": state, "detail": why, "model": be.model}
    return {"backends": out, "checklist_version": ai_checklist.load()["version"],
            "claude_spent_today_usd": round(ai_backends.spent_today_usd(), 4),
            "claude_daily_cap_usd": ai_backends.daily_cap_usd()}


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
