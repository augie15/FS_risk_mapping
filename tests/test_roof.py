"""Roof tracing on a synthetic point cloud: L-shaped gabled house, trees, flat ground."""
import math

import numpy as np
from rasterio.transform import from_origin
from shapely import affinity
from shapely.geometry import Point, Polygon, box
from shapely.ops import unary_union

from pipeline import roof

RNG = np.random.default_rng(1)
ANGLE = 25.0
HOUSE = affinity.rotate(unary_union([box(0, 0, 20, 8), box(0, 0, 8, 16)]), ANGLE, origin=(0, 0))


def _scene(with_house=True):
    xs, ys, zs, cs, ns = [], [], [], [], []
    # ground, 6 pts/m2 over 100 x 100 m
    n = 60000
    gx, gy = RNG.uniform(-50, 50, n), RNG.uniform(-50, 50, n)
    xs.append(gx); ys.append(gy); zs.append(RNG.normal(0, 0.03, n)); cs.append(np.full(n, 2)); ns.append(np.ones(n))
    if with_house:
        # roof points at ~14 pts/m2 inside the L, two gable facets per wing (planar)
        m = int(HOUSE.area * 14)
        px, py = RNG.uniform(-25, 25, m * 3), RNG.uniform(-25, 25, m * 3)
        inside = np.array([HOUSE.contains(Point(a, b)) for a, b in zip(px, py)])
        px, py = px[inside][:m], py[inside][:m]
        # local frame
        c, s = math.cos(math.radians(-ANGLE)), math.sin(math.radians(-ANGLE))
        lx, ly = px * c - py * s, px * s + py * c
        in_a = ly <= 8                       # east-west wing, ridge along y=4
        z = np.where(in_a, 6.5 - 0.6 * np.abs(ly - 4), 6.5 - 0.6 * np.abs(lx - 4))
        xs.append(px); ys.append(py); zs.append(z + RNG.normal(0, 0.03, len(px)))
        cs.append(np.ones(len(px))); ns.append(np.ones(len(px)))
    # trees: scattered crowns, multiple returns, 6-20 m tall, outside the house
    for tx, ty in [(-12, 15), (-15, -10), (25, 20), (18, -14), (-20, 2)]:
        k = 400
        r = RNG.uniform(0, 2.5, k); a = RNG.uniform(0, 2 * math.pi, k)
        xs.append(tx + r * np.cos(a)); ys.append(ty + r * np.sin(a))
        zs.append(RNG.uniform(4, 16, k)); cs.append(np.ones(k)); ns.append(RNG.integers(1, 5, k))
    return {"x": np.concatenate(xs), "y": np.concatenate(ys), "z": np.concatenate(zs),
            "c": np.concatenate(cs).astype(np.uint8), "nr": np.concatenate(ns).astype(np.uint8)}


DTM = np.zeros((200, 200), "float32")
DTR = from_origin(-50, 50, 0.5, 0.5)


def _iou(a, b):
    return a.intersection(b).area / a.union(b).area


def test_traces_l_shape_with_orthogonal_edges():
    r = roof.trace_roof(_scene(), DTM, DTR, (3.0, 3.0))
    assert r is not None
    assert _iou(r["polygon"], HOUSE) > 0.85, _iou(r["polygon"], HOUSE)
    assert abs(r["orientation_deg"] - ANGLE) < 3
    assert len(r["polygon"].exterior.coords) - 1 == 6       # an L has six corners
    assert r["confidence_label"] == "high"


def test_click_just_off_the_roof_snaps():
    edge_out = affinity.rotate(Point(10, -2), ANGLE, origin=(0, 0))   # 2 m outside the south wall
    r = roof.trace_roof(_scene(), DTM, DTR, (edge_out.x, edge_out.y))
    assert r is not None and r["snapped_to_nearest_roof"]
    assert _iou(r["polygon"], HOUSE) > 0.85


def test_trees_only_gives_no_roof():
    assert roof.trace_roof(_scene(with_house=False), DTM, DTR, (-12.0, 15.0)) is None


def test_click_on_a_tree_far_from_house_gives_no_roof():
    assert roof.trace_roof(_scene(), DTM, DTR, (25.0, 20.0)) is None


def test_footprint_iou_reported_as_cross_check():
    hint = affinity.translate(HOUSE, 2, 1)
    r = roof.trace_roof(_scene(), DTM, DTR, (3.0, 3.0), hint=hint)
    assert 0.5 < r["footprint_iou"] < 0.9
