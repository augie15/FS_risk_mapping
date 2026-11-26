"""Trace a building roof outline from LiDAR at a clicked point.

Primary method for the home outline. Building footprint datasets are used only as a cross-check.

1. Non-ground points 2.5 m or more above the terrain within `search_m` of the click.
2. Roof vs canopy per point: local planarity from the 3-D covariance of its neighbours (roof
   facets are planar, crowns scatter), a not-too-steep surface normal, and few returns per pulse
   (canopy splits a pulse into several returns).
3. Roof points -> 0.5 m mask -> clean up -> the connected patch under the click (or the nearest
   patch within `snap_m` if the click is just off the edge).
4. Trace the patch, find its dominant orientation, and fit straight-edged rectangles along it:
   one rectangle, or a union of 2-3 for L/T shapes and attached garages (pipeline/rects.py).
"""
from __future__ import annotations

import math

import numpy as np
from rasterio import features as rfeatures
from rasterio.transform import from_origin, rowcol
from scipy import ndimage
from scipy.spatial import cKDTree

from . import rects
from shapely import affinity
from shapely.geometry import Point, Polygon, shape

CELL = 0.5
MIN_HAG = 2.5          # m above ground for a roof point
K = 10                 # neighbours for planarity
PLANAR_MAX = 0.02      # smallest-eigenvalue share below which a neighbourhood is "planar"
MIN_NZ = 0.35          # |normal z|: facets steeper than ~70 deg are walls or crowns
MIN_AREA, MAX_AREA = 20.0, 5000.0


def _hag(pts, dtm, dtm_transform):
    r, c = rowcol(dtm_transform, pts["x"], pts["y"])
    r = np.clip(np.asarray(r), 0, dtm.shape[0] - 1)
    c = np.clip(np.asarray(c), 0, dtm.shape[1] - 1)
    return pts["z"] - dtm[r, c]


def roof_points(pts: dict, dtm, dtm_transform, bounds) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (xyz of raised points, is_roof mask, planarity) inside bounds."""
    minx, miny, maxx, maxy = bounds
    m = ((pts["x"] >= minx) & (pts["x"] <= maxx) & (pts["y"] >= miny) & (pts["y"] <= maxy)
         & (pts["c"] != 2) & (pts["c"] != 7))
    sub = {k: v[m] for k, v in pts.items()}
    if len(sub["x"]) < K + 1:
        return np.empty((0, 3)), np.empty(0, bool), np.empty(0)
    hag = _hag(sub, dtm, dtm_transform)
    keep = hag >= MIN_HAG
    xyz = np.column_stack([sub["x"][keep], sub["y"][keep], sub["z"][keep]])
    nr = sub["nr"][keep] if "nr" in sub else np.ones(len(xyz), np.uint8)
    if len(xyz) < K + 1:
        return xyz, np.zeros(len(xyz), bool), np.ones(len(xyz))
    _, idx = cKDTree(xyz).query(xyz, k=K)
    nb = xyz[idx] - xyz[idx].mean(axis=1, keepdims=True)
    cov = np.einsum("nki,nkj->nij", nb, nb) / K
    w, v = np.linalg.eigh(cov)                     # ascending eigenvalues
    planarity = w[:, 0] / np.clip(w.sum(axis=1), 1e-9, None)
    nz = np.abs(v[:, 2, 0])                        # z of the smallest-eigenvalue vector = normal
    is_roof = (planarity < PLANAR_MAX) & (nz > MIN_NZ) & (nr <= 2)
    return xyz, is_roof, planarity


def _dominant_angle(mask: np.ndarray) -> float:
    """Dominant edge direction (degrees, 0-90, world frame) from blurred-mask gradients."""
    blur = ndimage.gaussian_filter(mask.astype(float), 1.5)
    gx = ndimage.sobel(blur, axis=1)
    gy = -ndimage.sobel(blur, axis=0)              # image rows grow downward; world y grows up
    mag = np.hypot(gx, gy)
    sel = mag > mag.max() * 0.2
    ang = np.degrees(np.arctan2(gy[sel], gx[sel])) % 90.0
    hist, _ = np.histogram(ang, bins=90, range=(0, 90), weights=mag[sel])
    hist = ndimage.gaussian_filter1d(hist, 2, mode="wrap")
    return float(np.argmax(hist) + 0.5)


def _orthogonalise(poly: Polygon, angle: float, tol: float = 0.6) -> Polygon | None:
    """Snap a traced outline to two orthogonal directions (angle and angle+90)."""
    c = poly.centroid
    rot = affinity.rotate(poly, -angle, origin=c).simplify(tol)
    pts = list(rot.exterior.coords)[:-1]
    if len(pts) < 4:
        return None
    # Classify edges as horizontal / vertical in the rotated frame and merge runs.
    edges = []
    for i in range(len(pts)):
        (x0, y0), (x1, y1) = pts[i], pts[(i + 1) % len(pts)]
        horiz = abs(x1 - x0) >= abs(y1 - y0)
        length = math.hypot(x1 - x0, y1 - y0)
        val = (y0 + y1) / 2 if horiz else (x0 + x1) / 2
        if edges and edges[-1][0] == horiz:
            h, v, L = edges[-1]
            edges[-1] = (h, (v * L + val * length) / (L + length), L + length)
        else:
            edges.append((horiz, val, length))
    if len(edges) > 1 and edges[0][0] == edges[-1][0]:
        h, v, L = edges.pop()
        h0, v0, L0 = edges[0]
        edges[0] = (h0, (v0 * L0 + v * L) / (L0 + L), L0 + L)
    if len(edges) < 4 or len(edges) % 2:
        return None
    verts = []
    for i in range(len(edges)):
        a, b = edges[i], edges[(i + 1) % len(edges)]
        verts.append((b[1], a[1]) if a[0] else (a[1], b[1]))  # horizontal a gives y; vertical b gives x
    out = Polygon(verts)
    if not out.is_valid or out.area <= 0:
        return None
    return affinity.rotate(out, angle, origin=c)


def trace_roof(pts: dict, dtm, dtm_transform, click_xy: tuple[float, float],
               search_m: float = 40.0, snap_m: float = 6.0, hint=None) -> dict | None:
    """Trace the roof at (or just next to) the click. Returns None when no roof is found."""
    cx, cy = click_xy
    bounds = (cx - search_m, cy - search_m, cx + search_m, cy + search_m)
    xyz, is_roof, planarity = roof_points(pts, dtm, dtm_transform, bounds)
    if is_roof.sum() < 30:
        return None
    w = h = int(round(2 * search_m / CELL))
    tr = from_origin(bounds[0], bounds[3], CELL, CELL)
    rp = xyz[is_roof]
    col = np.clip(((rp[:, 0] - bounds[0]) / CELL).astype(int), 0, w - 1)
    row = np.clip(((bounds[3] - rp[:, 1]) / CELL).astype(int), 0, h - 1)
    mask = np.zeros((h, w), bool)
    mask[row, col] = True
    mask = ndimage.binary_closing(mask, structure=np.ones((3, 3)), iterations=2)
    mask = ndimage.binary_opening(mask, iterations=1)
    mask = ndimage.binary_fill_holes(mask)
    labels, n = ndimage.label(mask, structure=np.ones((3, 3)))
    if n == 0:
        return None
    r0 = int((bounds[3] - cy) / CELL)
    c0 = int((cx - bounds[0]) / CELL)
    lab = labels[r0, c0]
    snapped = False
    if lab == 0:
        dist, (ir, ic) = ndimage.distance_transform_edt(labels == 0, return_indices=True)
        if dist[r0, c0] * CELL > snap_m:
            return None
        lab = labels[ir[r0, c0], ic[r0, c0]]
        snapped = True
    comp = labels == lab
    area_cells = comp.sum() * CELL * CELL
    if not MIN_AREA <= area_cells <= MAX_AREA:
        return None

    polys = [shape(g) for g, v in rfeatures.shapes(comp.astype("uint8"), mask=comp, transform=tr) if v == 1]
    raw = max(polys, key=lambda p: p.area).buffer(0)
    raw = raw.buffer(-CELL * 0.4).buffer(0)       # cells overshoot the edge points by ~half a cell
    if raw.is_empty or raw.geom_type != "Polygon":
        return None
    angle = _dominant_angle(comp)
    ortho = _orthogonalise(raw, angle)   # rectilinear sketch: its corners seed the rectangle fit
    fitted = rects.fit(raw, angle, hint_rectilinear=ortho)
    reg = fitted["polygon"]
    reg_iou = reg.intersection(raw).area / reg.union(raw).area
    method = "lidar_roof_trace"

    inside = np.array([reg.contains(Point(x, y)) for x, y in xyz[:, :2]]) if len(xyz) < 60000 else None
    n_roof_in = int(np.sum(is_roof & inside)) if inside is not None else int(is_roof.sum())
    planar_share = float(np.mean(is_roof[inside])) if inside is not None and inside.any() else 0.0
    density = n_roof_in / reg.area if reg.area else 0.0
    conf = 0.4 * reg_iou + 0.3 * min(1.0, planar_share / 0.8) + 0.3 * min(1.0, density / 6.0)
    if snapped:
        conf *= 0.9
    hint_iou = None
    if hint is not None and not hint.is_empty:
        hint_iou = round(reg.intersection(hint).area / reg.union(hint).area, 2)
    return {
        "polygon": reg, "method": method, "points": n_roof_in, "area_m2": round(reg.area, 1),
        "orientation_deg": round(angle, 1), "regularisation_iou": round(reg_iou, 2),
        "shape": fitted["shape"], "rect_fill": fitted["fill"], "rects": fitted["rects"],
        "roof_point_share": round(planar_share, 2), "roof_density_pts_m2": round(density, 1),
        "confidence": round(conf, 2),
        "confidence_label": "high" if conf >= 0.8 else "medium" if conf >= 0.6 else "low",
        "snapped_to_nearest_roof": snapped, "footprint_iou": hint_iou,
    }
