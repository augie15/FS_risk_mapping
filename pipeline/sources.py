"""Fetchers for the open BC data sources listed in docs/DATA_SOURCES.md.

Everything downloaded is cached under data/raw (git-ignored).
"""
from __future__ import annotations

import fcntl
import gzip
import json
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Callable

import requests
from shapely.geometry import Point, box, shape

from .paths import DATA

ROOT = Path(__file__).resolve().parent.parent
RAW = DATA / "raw"
UA = {"User-Agent": "fs-risk-mapping-prototype"}

BC_WFS = "https://openmaps.gov.bc.ca/geo/pub/wfs"
PARCEL_LAYER = "pub:WHSE_CADASTRE.PMBC_PARCEL_FABRIC_POLY_SVW"
LIDAR_INDEX = (
    "https://services6.arcgis.com/ubm4tcTYICKBpist/arcgis/rest/services/"
    "LiDAR_BC_S3_Public/FeatureServer/4/query"
)
MS_BUILDINGS_INDEX = "https://minedbuildings.z5.web.core.windows.net/global-buildings/dataset-links.csv"
OVERPASS = "https://overpass-api.de/api/interpreter"

Progress = Callable[[str], None]


def _noop(_msg: str) -> None:
    pass


def _get(url: str, **kw) -> requests.Response:
    r = requests.get(url, headers=UA, timeout=kw.pop("timeout", 60), **kw)
    r.raise_for_status()
    return r


# ---------------------------------------------------------------- parcels

def fetch_parcel(lat: float, lng: float) -> dict | None:
    """ParcelMap BC parcel containing the point, as a GeoJSON feature in EPSG:4326."""
    d = 0.0005  # ~50 m search box; filter to the containing polygon below
    params = {
        "service": "WFS", "version": "2.0.0", "request": "GetFeature",
        "typeName": PARCEL_LAYER, "outputFormat": "application/json",
        "srsName": "EPSG:4326", "count": 50,
        "bbox": f"{lat - d},{lng - d},{lat + d},{lng + d},urn:ogc:def:crs:EPSG::4326",
    }
    feats = _get(BC_WFS, params=params).json().get("features", [])
    pt = Point(lng, lat)
    hits = [f for f in feats if f.get("geometry") and shape(f["geometry"]).contains(pt)]
    if not hits:
        return None
    f = min(hits, key=lambda f: shape(f["geometry"]).area)  # innermost if nested
    keep = ["PID_FORMATTED", "PLAN_NUMBER", "PARCEL_CLASS", "OWNER_TYPE", "MUNICIPALITY",
            "REGIONAL_DISTRICT", "FEATURE_AREA_SQM", "PARCEL_STATUS"]
    return {"type": "Feature", "geometry": f["geometry"],
            "properties": {k: f["properties"].get(k) for k in keep}}


# ---------------------------------------------------------------- buildings

def _quadkey(lat: float, lng: float, z: int = 9) -> str:
    n = 2 ** z
    x = int((lng + 180) / 360 * n)
    lr = math.radians(lat)
    y = int((1 - math.log(math.tan(lr) + 1 / math.cos(lr)) / math.pi) / 2 * n)
    q = ""
    for i in range(z, 0, -1):
        m = 1 << (i - 1)
        q += str((1 if x & m else 0) + (2 if y & m else 0))
    return q


def _ms_tile(qk: str, progress: Progress) -> Path | None:
    cache = RAW / "buildings" / f"ms_{qk}.geojsonl.gz"
    if cache.exists():
        return cache
    index = RAW / "buildings" / "ms_dataset_links.csv"
    index.parent.mkdir(parents=True, exist_ok=True)
    if not index.exists():
        progress("Downloading Microsoft building footprint index")
        index.write_bytes(_get(MS_BUILDINGS_INDEX, timeout=120).content)
    url = None
    for line in index.read_text().splitlines():
        parts = line.split(",")
        if len(parts) >= 3 and parts[0] == "Canada" and parts[1] == qk:
            url = parts[2]
            break
    if not url:
        return None
    progress(f"Downloading building footprints (quadkey {qk})")
    cache.write_bytes(_get(url, timeout=120).content)
    return cache


def fetch_buildings(bounds_ll: tuple[float, float, float, float], progress: Progress = _noop) -> list[dict]:
    """Building footprints intersecting a lon/lat bbox. Microsoft first, OSM fallback."""
    minx, miny, maxx, maxy = bounds_ll
    area = box(*bounds_ll)
    out: list[dict] = []
    qks = {_quadkey(y, x) for x in (minx, maxx) for y in (miny, maxy)}
    for qk in sorted(qks):
        path = _ms_tile(qk, progress)
        if not path:
            continue
        with gzip.open(path, "rt") as fh:
            for line in fh:
                f = json.loads(line)
                g = shape(f["geometry"])
                if g.intersects(area):
                    h = f.get("properties", {}).get("height", -1)
                    out.append({"type": "Feature", "geometry": f["geometry"],
                                "properties": {"source": "Microsoft",
                                               "height_m": h if h and h > 0 else None}})
    if out:
        return out
    progress("No Microsoft footprints here; trying OpenStreetMap")
    q = f'[out:json][timeout:50];way["building"]({miny},{minx},{maxy},{maxx});out geom;'
    try:
        els = requests.post(OVERPASS, data={"data": q}, headers=UA, timeout=60).json()["elements"]
    except Exception:
        return []
    for e in els:
        coords = [[p["lon"], p["lat"]] for p in e.get("geometry", [])]
        if len(coords) >= 4:
            out.append({"type": "Feature",
                        "geometry": {"type": "Polygon", "coordinates": [coords]},
                        "properties": {"source": "OpenStreetMap", "height_m": None}})
    return out


# ---------------------------------------------------------------- LiDAR / ortho

def lidar_tiles(bounds_ll: tuple[float, float, float, float]) -> list[dict]:
    """LidarBC point-cloud tiles intersecting a lon/lat bbox, newest year first."""
    minx, miny, maxx, maxy = bounds_ll
    params = {
        "geometry": f"{minx},{miny},{maxx},{maxy}", "geometryType": "esriGeometryEnvelope",
        "inSR": 4326, "spatialRel": "esriSpatialRelIntersects",
        "outFields": "filename,maptile,year,density,projection,classes,s3Url,oper_name",
        "returnGeometry": "false", "f": "json",
    }
    feats = _get(LIDAR_INDEX, params=params).json().get("features", [])
    tiles = [f["attributes"] for f in feats]
    if not tiles:
        return []
    newest = max(t["year"] for t in tiles)
    # One acquisition only, so tiles mosaic cleanly.
    return [t for t in tiles if t["year"] == newest]


def ortho_url(tile: dict) -> str:
    """Matching LidarBC orthophoto for a point-cloud tile (same maptile, same year)."""
    return re.sub(r"_xyes_\d+_", "_xc150mm_",
                  tile["s3Url"].replace("/pointcloud/", "/orthophoto/")).replace(".laz", ".tif")


def url_exists(url: str) -> bool:
    try:
        return requests.head(url, headers=UA, timeout=30).status_code == 200
    except requests.RequestException:
        return False


def download_lidar(tile: dict, progress: Progress = _noop) -> Path:
    dest = RAW / "lidar" / tile["filename"]
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    # One downloader per tile across threads *and* processes (server + CLI): an exclusive lock
    # file. Whoever waits finds the finished tile on the re-check below.
    with open(dest.with_name(dest.name + ".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            if dest.exists():
                return dest
            fd, tmp_name = tempfile.mkstemp(prefix=dest.name + ".", suffix=".part", dir=dest.parent)
            tmp = Path(tmp_name)
            try:
                with os.fdopen(fd, "wb") as fh, \
                        requests.get(tile["s3Url"], headers=UA, stream=True, timeout=120) as r:
                    r.raise_for_status()
                    total = int(r.headers.get("Content-Length", 0))
                    done, last = 0, -1
                    for chunk in r.iter_content(1 << 20):
                        fh.write(chunk)
                        done += len(chunk)
                        if done // (10 << 20) != last:
                            last = done // (10 << 20)
                            progress(f"Downloading LiDAR {tile['filename']}: "
                                     f"{done / 1e6:.0f} / {total / 1e6:.0f} MB")
                if total and done != total:
                    raise IOError(f"LiDAR download truncated: {done} of {total} bytes")
                os.replace(tmp, dest)  # atomic: readers never see a half-written tile
            finally:
                if tmp.exists():
                    tmp.unlink()
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    return dest
