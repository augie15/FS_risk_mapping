"""Rectilinear outline fitting."""
import math

import pytest
from shapely import affinity
from shapely.geometry import box
from shapely.ops import unary_union

from pipeline import rects


def _right_angles(poly):
    pts = list(poly.exterior.coords)[:-1]
    for i in range(len(pts)):
        a, b, c = pts[i - 1], pts[i], pts[(i + 1) % len(pts)]
        v1, v2 = (a[0] - b[0], a[1] - b[1]), (c[0] - b[0], c[1] - b[1])
        cos = (v1[0] * v2[0] + v1[1] * v2[1]) / (math.hypot(*v1) * math.hypot(*v2))
        if abs(cos) > 1e-6:
            return False
    return True


def test_jagged_rectangle_becomes_one_rectangle():
    jag = affinity.rotate(box(0, 0, 12, 8).union(box(11.8, 3, 12.3, 3.5)), 33, origin=(0, 0))
    r = rects.fit(jag)
    assert r["shape"] == "rectangle"
    assert len(r["polygon"].exterior.coords) == 5 and _right_angles(r["polygon"])


@pytest.mark.parametrize("parts,n", [([box(0, 0, 20, 8), box(0, 0, 8, 16)], 2),
                                     ([box(0, 0, 24, 6), box(9, 0, 15, 16)], 2)])
def test_l_and_t_shapes_use_multiple_rectangles(parts, n):
    shape = affinity.rotate(unary_union(parts), 12, origin=(0, 0))
    r = rects.fit(shape, 12)
    assert r["shape"] == f"{n} rectangles", r
    assert r["iou"] > 0.95 and _right_angles(r["polygon"])
    assert abs(r["angle_deg"] - 12) < 0.01


def test_union_of_closes_hairline_gaps_and_rejects_apart():
    a = box(0, 0, 10, 6)
    b = box(10.005, 0, 14, 4)            # 5 mm gap from a coordinate round-trip
    u = rects.union_of([list(a.exterior.coords), list(b.exterior.coords)])
    assert u.geom_type == "Polygon" and _right_angles(u)
    with pytest.raises(ValueError):
        rects.union_of([list(a.exterior.coords), list(box(20, 0, 24, 4).exterior.coords)])
