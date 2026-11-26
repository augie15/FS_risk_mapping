"""Rectilinear home outlines: one rectangle, or a union of 2-3 rectangles sharing one orientation.

Every edge is straight and every corner is 90 degrees. Used for LiDAR traces, footprint suggestions
and operator-saved outlines alike.
"""
from __future__ import annotations

import math

import numpy as np
from shapely import affinity
from shapely.geometry import Polygon, box
from shapely import contains_xy
from shapely.ops import unary_union

FILL_OK = 0.80        # one rectangle is enough when the shape fills this much of it
MIN_SIDE = 2.0        # m, smallest rectangle side
SNAP = 0.8            # m, merge breakpoints closer than this
GRID = 0.25           # m, raster used to score candidate rectangles
COVER_MIN = 0.80      # a candidate must be at least this much inside the shape
EXTRA_GAIN = 0.03     # a third rectangle must improve IoU by this much


def dominant_angle(poly: Polygon) -> float:
    """Orientation (0-90 deg) of the longest edge of the minimum rotated rectangle."""
    mrr = poly.minimum_rotated_rectangle
    xs, ys = mrr.exterior.coords.xy
    edges = [(xs[i + 1] - xs[i], ys[i + 1] - ys[i]) for i in range(4)]
    dx, dy = max(edges, key=lambda e: math.hypot(*e))
    return math.degrees(math.atan2(dy, dx)) % 90.0


def _breaks(vals: list[float], lo: float, hi: float) -> list[float]:
    """Sorted breakpoints with near-duplicates (< SNAP apart) merged; outer bounds kept exact."""
    v = sorted(set([lo, hi] + [x for x in vals if lo <= x <= hi]))
    clusters = [[v[0]]]
    for x in v[1:]:
        if x - clusters[-1][-1] < SNAP:
            clusters[-1].append(x)
        else:
            clusters.append([x])
    return [lo if lo in cl else hi if hi in cl else sum(cl) / len(cl) for cl in clusters]


def _touch(a, b) -> bool:
    """Axis-aligned boxes (x0, y0, x1, y1) overlap or share an edge (a corner alone is not enough)."""
    ox = min(a[2], b[2]) - max(a[0], b[0])
    oy = min(a[3], b[3]) - max(a[1], b[1])
    return ox >= 0 and oy >= 0 and (ox > 0 or oy > 0)


def fit(poly: Polygon, angle: float | None = None, hint_rectilinear: Polygon | None = None) -> dict:
    """Fit 1-3 same-orientation rectangles to `poly` (any CRS in metres).

    `hint_rectilinear` (optional) is an orthogonalised version of the shape whose corners give
    good candidate breakpoints for L/T shapes.
    """
    angle = dominant_angle(poly) if angle is None else angle % 90.0
    c = poly.centroid
    rot = affinity.rotate(poly, -angle, origin=c)
    bminx, bminy, bmaxx, bmaxy = rot.bounds
    # Raster of the shape in the rotated frame: scoring and robust bounds (stray edge cells ignored).
    nx = max(1, int(math.ceil((bmaxx - bminx) / GRID)))
    ny = max(1, int(math.ceil((bmaxy - bminy) / GRID)))
    gx = bminx + (np.arange(nx) + 0.5) * GRID
    gy = bminy + (np.arange(ny) + 0.5) * GRID
    M = contains_xy(rot, *np.meshgrid(gx, gy))
    total = int(M.sum())
    if hint_rectilinear is not None:
        src = affinity.rotate(hint_rectilinear, -angle, origin=c)
        minx, miny, maxx, maxy = src.bounds
        hx, hy = (list(v) for v in src.exterior.coords.xy)
    else:
        cols, rows = M.any(axis=0), M.any(axis=1)
        occ_x = np.repeat(gx[None, :], ny, 0)[M]
        occ_y = np.repeat(gy[:, None], nx, 1)[M]
        minx, maxx = np.percentile(occ_x, [1, 99]) if total else (bminx, bmaxx)
        miny, maxy = np.percentile(occ_y, [1, 99]) if total else (bminy, bmaxy)
        s2 = rot.simplify(0.5)
        hx, hy = (list(v) for v in s2.exterior.coords.xy)

    ix = lambda v: min(nx, max(0, int(round((v - bminx) / GRID))))  # noqa: E731
    iy = lambda v: min(ny, max(0, int(round((v - bminy) / GRID))))  # noqa: E731

    def sl(r):
        return (slice(iy(r[1]), iy(r[3])), slice(ix(r[0]), ix(r[2])))

    def score(chosen):
        U = np.zeros_like(M)
        for r in chosen:
            U[sl(r)] = True
        inter = int((U & M).sum())
        return inter / max(1, total + int(U.sum()) - inter)

    one = (minx, miny, maxx, maxy)
    cells = M[sl(one)]
    fill = float(cells.mean()) if cells.size else 0.0
    chosen, iou, shape_name = [one], score([one]), "rectangle"
    diag = {"one_rect_iou": round(iou, 3)}

    if fill < FILL_OK:
        xb = _breaks(hx, minx, maxx)
        yb = _breaks(hy, miny, maxy)
        cands = []
        for i in range(len(xb)):
            for j in range(i + 1, len(xb)):
                if xb[j] - xb[i] < MIN_SIDE:
                    continue
                for k in range(len(yb)):
                    for m in range(k + 1, len(yb)):
                        if yb[m] - yb[k] < MIN_SIDE:
                            continue
                        r = (xb[i], yb[k], xb[j], yb[m])
                        cc = M[sl(r)]
                        if cc.size and cc.mean() >= COVER_MIN:
                            cands.append(r)
        results, best, best_iou = {}, [], 0.0
        for n in (1, 2, 3):
            pick, pick_iou = None, best_iou
            for cand in cands:
                if best and not any(_touch(cand, r) for r in best):
                    continue  # the outline must stay one piece
                sc = score(best + [cand])
                if sc > pick_iou:
                    pick, pick_iou = cand, sc
            if pick is None:
                break
            best, best_iou = best + [pick], pick_iou
            results[n] = (list(best), best_iou)
        diag.update(candidates=len(cands), xb=[round(v, 1) for v in xb], yb=[round(v, 1) for v in yb],
                    multi_iou={n: round(v[1], 3) for n, v in results.items()})
        if 2 in results:
            n = 3 if 3 in results and results[3][1] - results[2][1] > EXTRA_GAIN else 2
            if results[n][1] > iou + 0.04:
                chosen, iou = results[n]
                shape_name = f"{len(chosen)} rectangles"

    # Union in the rotated frame, where shared edges are exactly equal, then rotate back.
    union_r = unary_union([box(*r) for r in chosen]).simplify(0.01)  # drop collinear meeting points
    if union_r.geom_type != "Polygon":   # should not happen (candidates must share an edge)
        chosen, shape_name, iou = [one], "rectangle", score([one])
        union_r = box(*one)
    rects = [affinity.rotate(box(*r), angle, origin=c) for r in chosen]
    union = affinity.rotate(union_r, angle, origin=c)
    return {"polygon": union, "rects": rects, "shape": shape_name, "angle_deg": round(angle, 1),
            "fill": round(fill, 2), "iou": round(float(iou), 2), "diag": diag}


def union_of(rect_rings: list) -> Polygon:
    """Union of operator-edited rectangles (rings of 4-5 points, metres). Must form one polygon.

    A 2 cm mitred buffer closes hairline gaps from coordinate round-trips without rounding corners.
    """
    polys = []
    for ring in rect_rings:
        p = Polygon(ring)
        if len(ring) < 4 or not p.is_valid or p.area <= 0:
            raise ValueError("each rectangle needs 4 corners and a non-zero area")
        polys.append(p.buffer(0.02, join_style="mitre"))
    u = unary_union(polys).buffer(-0.02, join_style="mitre").simplify(0.01)
    if u.geom_type != "Polygon":
        raise ValueError("rectangles must touch or overlap to form one outline")
    return u
