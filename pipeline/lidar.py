"""Canopy height model and individual tree detection from LidarBC point clouds.

LidarBC 2019 tiles in the Bulkley Valley are classified only as ground (2), unclassified (1)
and noise (7). So: DTM from ground points, DSM from the highest non-noise return per cell,
CHM = DSM - DTM, with building footprints masked out so roofs are not called trees.
"""
from __future__ import annotations

from pathlib import Path

import laspy
import numpy as np
from rasterio import features as rfeatures
from rasterio.transform import from_origin
from scipy import ndimage
from scipy.interpolate import griddata
from skimage.feature import peak_local_max
from shapely.geometry import shape
from skimage.segmentation import watershed

CELL = 0.5          # m, CHM resolution
MIN_TREE_H = 3.0    # m, lowest peak counted as a tree (shrubs below this)
MIN_CROWN_SEP = 2.0  # m, minimum spacing between tree tops


def read_points(paths: list[Path], bounds: tuple[float, float, float, float]) -> dict[str, np.ndarray]:
    """x, y, z, classification of all points inside bounds (tile CRS), across tiles."""
    minx, miny, maxx, maxy = bounds
    xs, ys, zs, cs = [], [], [], []
    for p in paths:
        with laspy.open(p) as fh:
            hb = fh.header
            if hb.maxs[0] < minx or hb.mins[0] > maxx or hb.maxs[1] < miny or hb.mins[1] > maxy:
                continue
            for pts in fh.chunk_iterator(2_000_000):
                x, y = np.asarray(pts.x), np.asarray(pts.y)
                m = (x >= minx) & (x <= maxx) & (y >= miny) & (y <= maxy)
                if not m.any():
                    continue
                xs.append(x[m]); ys.append(y[m])
                zs.append(np.asarray(pts.z)[m]); cs.append(np.asarray(pts.classification)[m])
    if not xs:
        return {"x": np.empty(0), "y": np.empty(0), "z": np.empty(0), "c": np.empty(0, np.uint8)}
    return {"x": np.concatenate(xs), "y": np.concatenate(ys),
            "z": np.concatenate(zs), "c": np.concatenate(cs)}


def normalized_dsm(pts: dict[str, np.ndarray], bounds, return_dtm: bool = False):
    """Height above ground of the highest return per cell (unsmoothed), plus transform and stats."""
    minx, miny, maxx, maxy = bounds
    w = int(np.ceil((maxx - minx) / CELL))
    h = int(np.ceil((maxy - miny) / CELL))
    transform = from_origin(minx, maxy, CELL, CELL)

    keep = pts["c"] != 7
    x, y, z, c = pts["x"][keep], pts["y"][keep], pts["z"][keep], pts["c"][keep]
    col = np.clip(((x - minx) / CELL).astype(int), 0, w - 1)
    row = np.clip(((maxy - y) / CELL).astype(int), 0, h - 1)
    idx = row * w + col

    # DSM: highest return per cell.
    dsm = np.full(w * h, -np.inf)
    np.maximum.at(dsm, idx, z)
    dsm = dsm.reshape(h, w)
    dsm[np.isinf(dsm)] = np.nan

    # DTM: lowest ground point per cell, then interpolate gaps (under trees, houses).
    g = c == 2
    if g.sum() < 10:
        raise ValueError("Too few ground points to build a terrain model")
    gmin = np.full(w * h, np.inf)
    np.minimum.at(gmin, idx[g], z[g])
    gmin = gmin.reshape(h, w)
    have = np.isfinite(gmin)
    rr, cc = np.nonzero(have)
    grid_r, grid_c = np.mgrid[0:h, 0:w]
    dtm = griddata((rr, cc), gmin[have], (grid_r, grid_c), method="linear")
    holes = np.isnan(dtm)
    if holes.any():
        dtm[holes] = griddata((rr, cc), gmin[have], (grid_r[holes], grid_c[holes]), method="nearest")

    ndsm = dsm - dtm
    # Fill empty cells (no return) from neighbours.
    ndsm = np.where(np.isnan(ndsm), ndimage.median_filter(np.nan_to_num(ndsm, nan=0), size=3), ndsm)
    ndsm = np.clip(ndsm, 0, 80)
    stats = {"points": int(len(x)), "ground_points": int(g.sum()),
             "density_pts_m2": round(len(x) / ((maxx - minx) * (maxy - miny)), 1)}
    if return_dtm:
        return ndsm.astype("float32"), transform, stats, dtm.astype("float32")
    return ndsm.astype("float32"), transform, stats


def refine_footprint(ndsm: np.ndarray, transform, footprint):
    """Snap an approximate building footprint to the roof seen by LiDAR.

    Roofs are raised (> 2 m) and locally smooth; tree crowns are rough. Keep the smooth raised
    patch that overlaps the footprint (grown by 3 m to allow for offset). Returns a shapely
    polygon in the CHM's CRS, or None when nothing convincing is found.
    """
    roof = _roof_mask(ndsm)
    fp = rfeatures.rasterize([(footprint, 1)], out_shape=ndsm.shape, transform=transform,
                             fill=0, dtype="uint8").astype(bool)
    near = ndimage.binary_dilation(fp, iterations=int(3 / CELL))
    labels, n = ndimage.label(roof)
    if n == 0:
        return None
    # Components that substantially overlap the (grown) footprint.
    overlap = np.bincount(labels[near].ravel(), minlength=n + 1)
    sizes = np.bincount(labels.ravel(), minlength=n + 1)
    pick = [i for i in range(1, n + 1) if overlap[i] >= 0.5 * sizes[i]]
    if not pick:
        return None
    mask = np.isin(labels, pick)
    area = mask.sum() * CELL * CELL
    if not (0.4 * footprint.area <= area <= 2.5 * footprint.area):
        return None
    polys = [shape(geom) for geom, v in rfeatures.shapes(mask.astype("uint8"), mask=mask, transform=transform) if v == 1]
    if not polys:
        return None
    poly = max(polys, key=lambda p: p.area)
    poly = poly.buffer(0.5).buffer(-0.5).simplify(0.4)
    return poly if poly.is_valid and poly.area > 20 else None


def _roof_mask(ndsm: np.ndarray) -> np.ndarray:
    """Raised (> 2 m), locally smooth cells: roofs, plus some very even canopy."""
    mean = ndimage.uniform_filter(ndsm, size=3)
    sq = ndimage.uniform_filter(ndsm * ndsm, size=3)
    rough = np.sqrt(np.clip(sq - mean * mean, 0, None))
    roof = (ndsm > 2.0) & (rough < 0.35)
    roof = ndimage.binary_opening(roof, iterations=1)
    roof = ndimage.binary_closing(roof, iterations=2)
    return ndimage.binary_fill_holes(roof)


def detect_roofs(ndsm: np.ndarray) -> np.ndarray:
    """Building roofs found from LiDAR alone, for buildings missing from footprint datasets.

    Keeps smooth raised patches of building size (15-2500 m2) whose height stays under 15 m,
    which rules out most smooth tall canopy.
    """
    labels, n = ndimage.label(_roof_mask(ndsm))
    if n == 0:
        return np.zeros(ndsm.shape, bool)
    idx = np.arange(1, n + 1)
    area = ndimage.sum_labels(np.ones_like(ndsm), labels, idx) * CELL * CELL
    p95 = np.array([np.percentile(ndsm[labels == i], 95) for i in idx])
    keep = idx[(area >= 15) & (area <= 2500) & (p95 <= 15)]
    return np.isin(labels, keep)


def canopy_height_model(ndsm: np.ndarray, transform, building_geoms=()):
    """Smoothed CHM with buildings masked out, so roofs are not called trees."""
    chm = ndimage.gaussian_filter(ndsm, sigma=0.8)
    bmask = detect_roofs(ndsm)
    if building_geoms:
        bmask |= rfeatures.rasterize([(g, 1) for g in building_geoms], out_shape=chm.shape,
                                     transform=transform, fill=0, dtype="uint8").astype(bool)
    # 1.5 m with a square element so roof corners are covered too (eaves, footprint offset).
    bmask = ndimage.binary_dilation(bmask, structure=np.ones((3, 3), bool), iterations=3)
    chm[bmask] = 0
    return chm.astype("float32")


def detect_trees(chm: np.ndarray, transform) -> list[dict]:
    """Tree tops by local maxima, crowns by watershed. Positions in the CHM's CRS."""
    min_dist = max(1, int(round(MIN_CROWN_SEP / CELL)))
    peaks = peak_local_max(chm, min_distance=min_dist, threshold_abs=MIN_TREE_H, exclude_border=False)
    if len(peaks) == 0:
        return []
    markers = np.zeros(chm.shape, dtype=np.int32)
    for i, (r, c) in enumerate(peaks, start=1):
        markers[r, c] = i
    canopy = chm > 2.0
    labels = watershed(-chm, markers, mask=canopy)
    areas = np.bincount(labels.ravel(), minlength=len(peaks) + 1) * CELL * CELL

    a, _, x0, _, e, y0 = transform.a, transform.b, transform.c, transform.d, transform.e, transform.f
    trees = []
    for i, (r, c) in enumerate(peaks, start=1):
        x = x0 + (c + 0.5) * a
        y = y0 + (r + 0.5) * e
        area = float(areas[i])
        trees.append({"x": x, "y": y, "height_m": round(float(chm[r, c]), 1),
                      "crown_area_m2": round(area, 1),
                      "crown_radius_m": round(float(np.sqrt(area / np.pi)), 1)})
    return trees
