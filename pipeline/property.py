"""One property, end to end: parcel -> home -> ignition zones -> orthophoto -> CHM -> trees.

Outputs land in data/processed/<property_id>/ (git-ignored):
  parcel.geojson, buildings.geojson, zones.geojson, trees.geojson,
  ortho.tif (UTM, clipped), ortho.png + ortho_bounds in summary.json (EPSG:3857 overlay),
  chm.tif, summary.json
"""
from __future__ import annotations

import hashlib
import json
import time
import warnings
from pathlib import Path

import numpy as np
import pyproj
import rasterio
import rasterio.errors
from rasterio.merge import merge
from rasterio.warp import Resampling, calculate_default_transform, reproject
from shapely.geometry import Point, box, mapping, shape
from shapely.ops import transform as shp_transform

from . import lidar, sources

ROOT = Path(__file__).resolve().parent.parent
PROCESSED = ROOT / "data" / "processed"

# FireSmart BC Home Ignition Zone bands, metres from the building edge.
ZONES = [
    ("immediate", "Immediate (0–1.5 m)", 0.0, 1.5),
    ("intermediate", "Intermediate (1.5–10 m)", 1.5, 10.0),
    ("extended", "Extended (10–30 m)", 10.0, 30.0),
    ("outer", "30–100 m", 30.0, 100.0),
]
ASSUMED_HOUSE_M = 12.0
HOME_SEARCH_M = 60.0
PIPELINE_VERSION = 5


def property_id(lat: float, lng: float) -> str:
    return hashlib.sha1(f"{lat:.5f},{lng:.5f}".encode()).hexdigest()[:12]


def zone_for(dist_m: float) -> str | None:
    for key, _label, lo, hi in ZONES:
        if lo <= dist_m < hi or (lo == 0 and dist_m <= 0):
            return key
    return None


def _utm_epsg(lng: float, tiles: list[dict]) -> int:
    if tiles and str(tiles[0].get("projection", "")).startswith("utm"):
        return 26900 + int(tiles[0]["projection"][3:])
    return 26900 + int((lng + 180) // 6) + 1  # NAD83 / UTM


def _fc(features: list[dict]) -> dict:
    return {"type": "FeatureCollection", "features": features}


def _write(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, separators=(",", ":")))


def _clip_ortho(tiles: list[dict], bounds, out_dir: Path, progress) -> dict | None:
    urls = [u for u in (sources.ortho_url(t) for t in tiles) if sources.url_exists(u)]
    if not urls:
        return None
    progress(f"Reading orthophoto window from {len(urls)} tile(s)")
    srcs = [rasterio.open(f"/vsicurl/{u}") for u in urls]
    try:
        mosaic, tr = merge(srcs, bounds=bounds, nodata=0)
        crs = srcs[0].crs
    finally:
        for s in srcs:
            s.close()
    prof = {"driver": "GTiff", "height": mosaic.shape[1], "width": mosaic.shape[2], "count": 3,
            "dtype": "uint8", "crs": crs, "transform": tr, "compress": "deflate", "tiled": True,
            "nodata": 0}
    with rasterio.open(out_dir / "ortho.tif", "w", **prof) as dst:
        dst.write(mosaic[:3])

    # Web overlay: reproject to Web Mercator so a rectangular image lines up on the map.
    progress("Building orthophoto overlay")
    dst_crs = "EPSG:3857"
    with rasterio.open(out_dir / "ortho.tif") as src:
        t2, w2, h2 = calculate_default_transform(src.crs, dst_crs, src.width, src.height,
                                                 *src.bounds, resolution=0.4)
        rgba = np.zeros((4, h2, w2), dtype="uint8")
        for b in range(3):
            reproject(rasterio.band(src, b + 1), rgba[b], src_transform=src.transform,
                      src_crs=src.crs, dst_transform=t2, dst_crs=dst_crs,
                      resampling=Resampling.bilinear, src_nodata=0, dst_nodata=0)
        rgba[3] = np.where(rgba[:3].max(axis=0) > 0, 255, 0)
    warnings.filterwarnings("ignore", category=rasterio.errors.NotGeoreferencedWarning)
    with rasterio.open(out_dir / "ortho.png", "w", driver="PNG", width=w2, height=h2,
                       count=4, dtype="uint8") as dst:
        dst.write(rgba)
    left, top = t2.c, t2.f
    right, bottom = left + w2 * t2.a, top + h2 * t2.e
    to_ll = pyproj.Transformer.from_crs(3857, 4326, always_xy=True)
    west, south = to_ll.transform(left, bottom)
    east, north = to_ll.transform(right, top)
    return {"west": west, "south": south, "east": east, "north": north,
            "source": urls[0].rsplit("/", 1)[-1], "tiles": len(urls)}


def assess(lat: float, lng: float, progress=lambda m: None, force: bool = False) -> dict:
    pid = property_id(lat, lng)
    out = PROCESSED / pid
    summary_path = out / "summary.json"
    if summary_path.exists() and not force:
        s = json.loads(summary_path.read_text())
        if s.get("pipeline_version") == PIPELINE_VERSION:
            progress("Cached result")
            return s
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    notes: list[str] = []

    # 1. Parcel
    progress("Looking up parcel (ParcelMap BC)")
    parcel = sources.fetch_parcel(lat, lng)
    if parcel is None:
        raise ValueError("No ParcelMap BC parcel at that point (road, water or unsurveyed land?)")
    parcel_ll = shape(parcel["geometry"])

    # Rough lon/lat search box: parcel plus ~130 m.
    pad = 0.0012
    pminx, pminy, pmaxx, pmaxy = parcel_ll.bounds
    near_ll = (min(pminx, lng) - pad * 1.7, min(pminy, lat) - pad,
               max(pmaxx, lng) + pad * 1.7, max(pmaxy, lat) + pad)

    progress("Finding LiDAR coverage (LidarBC index)")
    tiles = sources.lidar_tiles(near_ll)
    epsg = _utm_epsg(lng, tiles)
    to_utm = pyproj.Transformer.from_crs(4326, epsg, always_xy=True).transform
    to_ll = pyproj.Transformer.from_crs(epsg, 4326, always_xy=True).transform
    parcel_u = shp_transform(to_utm, parcel_ll)
    click_u = Point(to_utm(lng, lat))

    # 2. Home building
    progress("Finding building footprints")
    blds = sources.fetch_buildings(near_ll, progress)
    bld_u = [(f, shp_transform(to_utm, shape(f["geometry"]))) for f in blds]
    home = next(((f, g) for f, g in bld_u if g.contains(click_u)), None)
    if home is None:
        cands = [(f, g) for f, g in bld_u
                 if g.intersects(parcel_u) and g.distance(click_u) <= HOME_SEARCH_M and g.area >= 40]
        if cands:
            home = min(cands, key=lambda fg: fg[1].distance(click_u))
            notes.append("Clicked point was not on a building; used the nearest footprint on the parcel.")
    if home is None:
        half = ASSUMED_HOUSE_M / 2
        home_u = box(click_u.x - half, click_u.y - half, click_u.x + half, click_u.y + half)
        home_src = "assumed"
        notes.append(f"No building footprint found; assumed a {ASSUMED_HOUSE_M:.0f} x "
                     f"{ASSUMED_HOUSE_M:.0f} m house at the clicked point.")
    else:
        home_u = home[1]
        home_src = home[0]["properties"]["source"]

    footprint_u = home_u  # as found (Microsoft / OSM / assumed), before LiDAR refinement
    analysis = home_u.buffer(ZONES[-1][3] + 15)  # margin: refinement can move the home a few m
    bounds = tuple(round(v) for v in analysis.bounds)

    # 3. LiDAR: height model, refine the home outline, then trees
    ndsm = tr = None
    lidar_info, use = None, []
    if tiles:
        bounds_ll = shp_transform(to_ll, box(*bounds)).bounds
        use = sources.lidar_tiles(bounds_ll)
        paths = [sources.download_lidar(t, progress) for t in use]
        progress("Reading LiDAR points")
        pts = lidar.read_points(paths, bounds)
        if len(pts["x"]) < 1000:
            notes.append("LiDAR tile found but almost no points inside the 100 m zone.")
        else:
            progress(f"Building height model from {len(pts['x']):,} points")
            ndsm, tr, stats = lidar.normalized_dsm(pts, bounds)
            lidar_info = {**stats, "year": use[0]["year"], "project": use[0].get("oper_name"),
                          "nominal_density": use[0].get("density"), "tiles": [t["filename"] for t in use]}
            refined = lidar.refine_footprint(ndsm, tr, home_u)
            if refined is not None:
                shift = refined.centroid.distance(home_u.centroid)
                home_u = refined
                home_src = f"{home_src} footprint refined with LiDAR"
                if shift > 1.0:
                    notes.append(f"Home outline moved {shift:.1f} m to match the LiDAR roof.")
            elif home_src == "assumed":
                notes.append("LiDAR could not confirm a roof at the clicked point either.")
    else:
        notes.append("No LidarBC point cloud covers this property, so no trees were detected.")

    # 4. Ignition zones around the (refined) home. Not clipped to the parcel: fire ignores lot lines.
    progress("Buffering Home Ignition Zones")
    zone_feats = []
    for key, label, lo, hi in ZONES:
        ring = home_u.buffer(hi, quad_segs=16)
        ring = ring.difference(home_u.buffer(lo, quad_segs=16) if lo > 0 else home_u)
        inside = ring.intersection(parcel_u).area / ring.area if ring.area else 0
        zone_feats.append({"type": "Feature", "geometry": mapping(shp_transform(to_ll, ring)),
                           "properties": {"zone": key, "label": label, "inner_m": lo, "outer_m": hi,
                                          "area_m2": round(ring.area),
                                          "share_on_parcel": round(inside, 2)}})

    # Buildings layer: home + neighbours within 100 m
    reach = home_u.buffer(ZONES[-1][3])
    bfeats = [{"type": "Feature", "geometry": mapping(shp_transform(to_ll, home_u)),
               "properties": {"source": home_src, "home": True, "dist_to_home_m": 0,
                              "area_m2": round(home_u.area)}}]
    for f, g in bld_u:
        if g.intersects(reach) and not g.equals(footprint_u):
            bfeats.append({"type": "Feature", "geometry": f["geometry"],
                           "properties": {**f["properties"], "home": False,
                                          "dist_to_home_m": round(g.distance(home_u), 1)}})

    # 5. Trees from the CHM, roofs masked out
    trees_out = []
    if ndsm is not None:
        progress("Detecting trees")
        # Mask every roof: each footprint plus its LiDAR-snapped outline (ML footprints drift).
        masks = [home_u, footprint_u]
        for _f, g in bld_u:
            if g.intersects(analysis) and not g.equals(footprint_u):
                masks.append(g)
                snapped = lidar.refine_footprint(ndsm, tr, g)
                if snapped is not None:
                    masks.append(snapped)
        chm = lidar.canopy_height_model(ndsm, tr, masks)
        with rasterio.open(out / "chm.tif", "w", driver="GTiff", height=chm.shape[0],
                           width=chm.shape[1], count=1, dtype="float32", crs=f"EPSG:{epsg}",
                           transform=tr, compress="deflate") as dst:
            dst.write(chm, 1)
        for t in lidar.detect_trees(chm, tr):
            p = Point(t["x"], t["y"])
            d = p.distance(home_u)
            if d > ZONES[-1][3]:
                continue
            edge = max(0.0, d - t["crown_radius_m"])
            lon, la = to_ll(t["x"], t["y"])
            trees_out.append({"type": "Feature",
                              "geometry": {"type": "Point", "coordinates": [round(lon, 7), round(la, 7)]},
                              "properties": {"height_m": t["height_m"],
                                             "crown_radius_m": t["crown_radius_m"],
                                             "dist_to_home_m": round(d, 1),
                                             "crown_edge_to_home_m": round(edge, 1),
                                             "zone": zone_for(d),
                                             "crown_zone": zone_for(edge),
                                             "on_parcel": parcel_u.contains(p)}})

    # Orthophoto window: 100 m zone plus a little
    ortho = None
    if tiles:
        try:
            obounds = tuple(round(v) for v in home_u.buffer(ZONES[-1][3] + 5).bounds)
            ortho = _clip_ortho(tiles, obounds, out, progress)
        except Exception as e:  # imagery is helpful, not essential
            notes.append(f"Orthophoto clip failed: {e}")
    if ortho is None:
        notes.append("No LidarBC 2019/2021 orthophoto for this spot.")

    # 6. Write outputs
    _write(out / "parcel.geojson", _fc([parcel]))
    _write(out / "buildings.geojson", _fc(bfeats))
    _write(out / "zones.geojson", _fc(zone_feats))
    _write(out / "trees.geojson", _fc(trees_out))

    by_zone = {k: 0 for k, *_ in ZONES}
    crowns_by_zone = {k: 0 for k, *_ in ZONES}
    for t in trees_out:
        by_zone[t["properties"]["zone"]] += 1
        crowns_by_zone[t["properties"]["crown_zone"]] += 1
    tallest = max((t["properties"]["height_m"] for t in trees_out), default=None)
    summary = {
        "pipeline_version": PIPELINE_VERSION,
        "id": pid,
        "click": {"lat": lat, "lng": lng},
        "crs": f"EPSG:{epsg}",
        "parcel": parcel["properties"],
        "home": {"source": home_src, "area_m2": round(home_u.area)},
        "zones": [{"zone": k, "label": l, "inner_m": lo, "outer_m": hi} for k, l, lo, hi in ZONES],
        "trees": {"count": len(trees_out), "by_trunk_zone": by_zone,
                  "by_crown_edge_zone": crowns_by_zone, "tallest_m": tallest,
                  "min_height_m": lidar.MIN_TREE_H},
        "lidar": lidar_info,
        "ortho": ortho,
        "notes": notes,
        "elapsed_s": round(time.time() - t0, 1),
        "attribution": ("Contains information licensed under the Open Government Licence – "
                        "British Columbia (LidarBC, ParcelMap BC). Building footprints © Microsoft "
                        "(ODbL) / © OpenStreetMap contributors. Based on FireSmart BC guidance; "
                        "not an official FireSmart assessment."),
    }
    _write(summary_path, summary)
    progress("Done")
    return summary
