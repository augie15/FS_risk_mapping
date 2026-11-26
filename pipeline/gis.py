"""GIS downloads: findings as GeoJSON, slope as GeoTIFF (canopy height is chm.tif from the run)."""
from __future__ import annotations

import json

import numpy as np
import rasterio

from .property import PROCESSED

NODATA = -9999.0


def findings_geojson(score: dict) -> dict:
    """One point per finding location (a finding about 3 trees gives 3 points with the same n).

    Findings about a whole zone (canopy cover, slope) get the zone ring polygon instead.
    """
    zones = {f["properties"]["zone"]: f["geometry"] for f in
             json.loads((PROCESSED / score["property_id"] / "zones.geojson").read_text())["features"]}
    feats = []
    for f in score["findings"]:
        props = {k: f.get(k) for k in ("n", "id", "source", "zone", "title", "points", "max_points",
                                       "basis", "evidence", "fix", "approx_location", "whole_zone")}
        props["rules_version"] = score["rules_version"]
        if f.get("whole_zone"):
            feats.append({"type": "Feature", "properties": props, "geometry": zones[f["zone"]]})
            continue
        for lng, lat in f["locations"]:
            feats.append({"type": "Feature", "properties": props,
                          "geometry": {"type": "Point", "coordinates": [lng, lat]}})
    return {"type": "FeatureCollection", "name": f"findings_{score['property_id']}", "features": feats}


def slope_tif(pid: str):
    """Slope in degrees from the run's ground model (dtm.tif), cached next to it as slope.tif."""
    d = PROCESSED / pid
    src_path, out = d / "dtm.tif", d / "slope.tif"
    if out.exists() and out.stat().st_mtime >= src_path.stat().st_mtime:
        return out
    with rasterio.open(src_path) as src:
        dtm = src.read(1).astype("float64")
        prof = src.profile
        valid = np.isfinite(dtm) if src.nodata is None else (dtm != src.nodata) & np.isfinite(dtm)
        gy, gx = np.gradient(np.where(valid, dtm, np.nan), abs(src.transform.e), src.transform.a)
    slope = np.degrees(np.arctan(np.hypot(gx, gy)))
    slope = np.where(np.isfinite(slope), slope, NODATA).astype("float32")
    prof.update(dtype="float32", count=1, nodata=NODATA, compress="deflate", predictor=3)
    tmp = out.with_suffix(".tmp.tif")
    with rasterio.open(tmp, "w", **prof) as dst:
        dst.write(slope, 1)
        dst.update_tags(1, units="degrees", source="LidarBC 2019 ground model (dtm.tif), 0.5 m")
    tmp.replace(out)
    return out
