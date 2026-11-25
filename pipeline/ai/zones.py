"""Per-zone inputs for the vision model: an orthophoto crop with the zone drawn on it,
and the LiDAR facts for that zone, both derived from a finished property run."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pyproj
import rasterio
from PIL import Image, ImageDraw, ImageFont
from rasterio import features as rfeatures
from rasterio.windows import from_bounds
from shapely.geometry import Point, shape
from shapely.ops import transform as shp_transform

from ..property import PROCESSED, ZONES

MIN_EDGE = 768    # upscale small crops so the model can see detail
MAX_EDGE = 1024   # and cap big ones (cost and Ollama speed)
MARGIN = {"immediate": 4.0, "intermediate": 4.0, "extended": 5.0, "outer": 5.0}
ZONE_RGB = {"immediate": (215, 25, 28), "intermediate": (253, 141, 60),
            "extended": (254, 224, 139), "outer": (145, 207, 96)}
MAX_LABELLED_TREES = 40


class PropertyData:
    """Everything a finished property run left on disk, in the run's UTM CRS."""

    def __init__(self, pid: str):
        self.pid = pid
        self.dir = PROCESSED / pid
        self.summary = json.loads((self.dir / "summary.json").read_text())
        epsg = int(self.summary["crs"].split(":")[1])
        self.to_utm = pyproj.Transformer.from_crs(4326, epsg, always_xy=True).transform
        self.to_ll = pyproj.Transformer.from_crs(epsg, 4326, always_xy=True).transform
        load = lambda n: json.loads((self.dir / f"{n}.geojson").read_text())["features"]
        self.zones = {f["properties"]["zone"]: (f["properties"], shp_transform(self.to_utm, shape(f["geometry"])))
                      for f in load("zones")}
        blds = load("buildings")
        home = next(f for f in blds if f["properties"].get("home"))
        self.home = shp_transform(self.to_utm, shape(home["geometry"]))
        self.others = [(f["properties"], shp_transform(self.to_utm, shape(f["geometry"])))
                       for f in blds if not f["properties"].get("home")]
        self.trees = []
        for i, f in enumerate(load("trees"), start=1):
            x, y = self.to_utm(*f["geometry"]["coordinates"])
            self.trees.append({"id": f"T{i}", "x": x, "y": y, **f["properties"]})


def _crop_bounds(pd: PropertyData, zone: str):
    ring = pd.zones[zone][1]
    minx, miny, maxx, maxy = ring.bounds
    m = MARGIN[zone]
    return minx - m, miny - m, maxx + m, maxy + m


def make_crop(pd: PropertyData, zone: str, out_dir: Path) -> dict | None:
    """Write crop_<zone>.png; return its geometry so pixel answers can be mapped back."""
    ortho = pd.dir / "ortho.tif"
    if not ortho.exists():
        return None
    left, bottom, right, top = _crop_bounds(pd, zone)
    with rasterio.open(ortho) as src:
        win = from_bounds(left, bottom, right, top, src.transform)
        rgb = src.read([1, 2, 3], window=win, boundless=True, fill_value=0)
    img = Image.fromarray(np.moveaxis(rgb, 0, -1), "RGB")
    long_edge = max(img.size)
    scale = min(MAX_EDGE / long_edge, max(1.0, MIN_EDGE / long_edge))
    if scale != 1.0:
        img = img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)
    w, h = img.size
    sx, sy = w / (right - left), h / (top - bottom)

    def px(x, y):
        return ((x - left) * sx, (top - y) * sy)

    d = ImageDraw.Draw(img)
    lw = max(2, round(w / 300))
    ring = pd.zones[zone][1]
    for geom in getattr(ring, "geoms", [ring]):
        for line in [geom.exterior, *geom.interiors]:
            d.line([px(*c) for c in line.coords], fill=ZONE_RGB[zone], width=lw)
    d.line([px(*c) for c in pd.home.exterior.coords], fill=(0, 229, 255), width=lw)
    font = ImageFont.load_default(size=max(14, round(w / 48)))
    labelled = 0
    for t in sorted(pd.trees, key=lambda t: t["dist_to_home_m"]):
        if t["zone"] != zone or labelled >= MAX_LABELLED_TREES:
            continue
        cx, cy = px(t["x"], t["y"])
        r = max(4, lw * 2)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(255, 0, 255), width=max(1, lw // 2 + 1))
        d.text((cx + r + 2, cy - r - 2), t["id"], fill=(255, 0, 255), font=font,
               stroke_width=2, stroke_fill=(0, 0, 0))
        labelled += 1
    path = out_dir / f"crop_{zone}.png"
    img.save(path, optimize=True)
    return {"file": path.name, "width": w, "height": h,
            "bounds_utm": [left, bottom, right, top], "m_per_px": round(1 / sx, 3)}


def pixel_to_lnglat(pd: PropertyData, crop: dict, x: float, y: float) -> tuple[float, float]:
    left, bottom, right, top = crop["bounds_utm"]
    ux = left + x / crop["width"] * (right - left)
    uy = top - y / crop["height"] * (top - bottom)
    return pd.to_ll(ux, uy)


def _raster_in(pd: PropertyData, name: str, geom):
    path = pd.dir / name
    if not path.exists():
        return None
    with rasterio.open(path) as src:
        arr = src.read(1)
        mask = rfeatures.rasterize([(geom, 1)], out_shape=arr.shape, transform=src.transform,
                                   fill=0, dtype="uint8").astype(bool)
        return arr, mask, src.transform


def zone_facts(pd: PropertyData, zone: str, crop: dict | None) -> dict:
    """LiDAR and footprint measurements for one zone. These are what the validator checks against."""
    props, ring = pd.zones[zone]
    lo, hi = props["inner_m"], props["outer_m"]
    in_zone = [t for t in pd.trees if t["zone"] == zone]
    facts: dict = {
        "zone": zone, "inner_m": lo, "outer_m": hi,
        "lidar_available": pd.summary.get("lidar") is not None,
        "tree_min_height_m": pd.summary["trees"]["min_height_m"],
        "trees_with_trunk_in_zone": len(in_zone),
        "crowns_reaching_zone": sum(1 for t in pd.trees if t["crown_zone"] == zone),
        "tallest_in_zone_m": max((t["height_m"] for t in in_zone), default=None),
        "home_footprint_m2": round(pd.home.area),
        "home_outline_source": pd.summary["home"]["source"],
    }
    if zone == "immediate":
        facts["crowns_touching_home"] = sum(1 for t in pd.trees if t["crown_edge_to_home_m"] <= 0.0)
    if zone in ("immediate", "intermediate"):
        facts["outbuildings_in_zone"] = sum(1 for p, g in pd.others
                                            if lo <= g.distance(pd.home) < hi)
    # Tree list (nearest first) with crop pixel positions so the model can refer to them.
    listed = []
    for t in sorted(in_zone, key=lambda t: t["dist_to_home_m"])[:MAX_LABELLED_TREES]:
        e = {"id": t["id"], "height_m": t["height_m"], "trunk_to_home_m": t["dist_to_home_m"],
             "crown_edge_to_home_m": t["crown_edge_to_home_m"], "crown_radius_m": t["crown_radius_m"]}
        if crop:
            left, bottom, right, top = crop["bounds_utm"]
            e["px"] = [round((t["x"] - left) / (right - left) * crop["width"]),
                       round((top - t["y"]) / (top - bottom) * crop["height"])]
        listed.append(e)
    facts["trees"] = listed

    chm = _raster_in(pd, "chm.tif", ring)
    if chm is not None:
        arr, mask, _ = chm
        facts["canopy_cover_pct"] = round(100 * float((arr[mask] > 2.0).mean()), 1) if mask.any() else None
    if zone in ("extended", "outer") and len(in_zone) >= 2:
        pts = np.array([[t["x"], t["y"]] for t in pd.trees])
        rad = np.array([t["crown_radius_m"] for t in pd.trees])
        close = 0
        for t in in_zone:
            d = np.hypot(pts[:, 0] - t["x"], pts[:, 1] - t["y"])
            gap = d - rad - t["crown_radius_m"]
            gap[d == 0] = np.inf
            if gap.min() < 3.0:
                close += 1
        facts["share_trees_crown_gap_under_3m"] = round(close / len(in_zone), 2)
    if zone == "outer":
        dtm = _raster_in(pd, "dtm.tif", ring)
        if dtm is not None:
            arr, mask, tr = dtm
            gy, gx = np.gradient(arr, abs(tr.e), tr.a)
            slope = np.degrees(np.arctan(np.hypot(gx, gy)))
            hc = pd.home.centroid
            r, c = rasterio.transform.rowcol(tr, hc.x, hc.y)
            home_z = float(arr[r, c])
            ring_z = arr[mask]
            facts["mean_slope_deg_30_100m"] = round(float(slope[mask].mean()), 1)
            facts["ground_drop_below_home_m"] = round(home_z - float(np.percentile(ring_z, 10)), 1)
            facts["ground_rise_above_home_m"] = round(float(np.percentile(ring_z, 90)) - home_z, 1)
    return facts
