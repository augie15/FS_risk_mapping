"""Operator-confirmed home outlines, stored in data/outlines.json (git-ignored).

A confirmed outline always wins over an automatic one. It is linked to the property ids it was
saved from, and also matches any later click that lands inside it.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from shapely.geometry import Point, mapping, shape

PATH = Path(__file__).resolve().parent.parent / "data" / "outlines.json"
_lock = threading.Lock()


def _load() -> list[dict]:
    return json.loads(PATH.read_text()) if PATH.exists() else []


def _save(rows: list[dict]) -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows, indent=1))
    tmp.replace(PATH)


def find(pid: str, lng: float, lat: float) -> dict | None:
    """Confirmed outline for this property id, or one containing the clicked point."""
    rows = _load()
    for r in rows:
        if pid in r["property_ids"]:
            return r
    pt = Point(lng, lat)
    for r in rows:
        if shape(r["geometry"]).buffer(0.00002).contains(pt):  # ~2 m slack
            return r
    return None


def save(pid: str, coords_lnglat: list[list[float]], lng: float, lat: float, note: str | None = None) -> dict:
    """Store an operator-confirmed outline (lon/lat ring), replacing the property's previous one."""
    ring = [list(map(float, c[:2])) for c in coords_lnglat]
    if ring[0] != ring[-1]:
        ring.append(ring[0])
    geom = {"type": "Polygon", "coordinates": [ring]}
    poly = shape(geom)
    if len(ring) < 4 or not poly.is_valid or poly.area == 0:
        raise ValueError("Outline must be a simple polygon with at least 3 corners")
    with _lock:
        rows = _load()
        old = find(pid, lng, lat)
        ids = sorted(set(old["property_ids"] + [pid])) if old else [pid]
        rows = [r for r in rows if r is not old and not (old and r["id"] == old["id"])]
        rec = {"id": old["id"] if old else f"o{int(time.time() * 1000)}", "property_ids": ids,
               "geometry": mapping(poly), "confirmed": True, "source": "operator-confirmed",
               "note": note, "updated": time.strftime("%Y-%m-%dT%H:%M:%S")}
        rows.append(rec)
        _save(rows)
    return rec


def delete(pid: str, lng: float, lat: float) -> bool:
    with _lock:
        rows = _load()
        old = find(pid, lng, lat)
        if not old:
            return False
        _save([r for r in rows if r["id"] != old["id"]])
    return True
