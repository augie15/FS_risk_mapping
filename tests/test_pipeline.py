"""Offline tests: no network, synthetic rasters only."""
import numpy as np
from rasterio.transform import from_origin
from shapely.geometry import box

from pipeline import lidar
from pipeline.property import zone_for
from pipeline.sources import _quadkey, ortho_url


def test_zone_bands():
    assert zone_for(0) == "immediate"
    assert zone_for(1.4) == "immediate"
    assert zone_for(1.5) == "intermediate"
    assert zone_for(29.9) == "extended"
    assert zone_for(99) == "outer"
    assert zone_for(150) is None


def test_quadkey_telkwa():
    assert _quadkey(54.6667, -127.05) == "021201031"


def test_ortho_url_from_lidar_tile():
    t = {"s3Url": "https://x/gdwuts/093/093l/2019/pointcloud/bc_093l065_4_2_3_xyes_8_utm09_2019.laz"}
    assert ortho_url(t) == "https://x/gdwuts/093/093l/2019/orthophoto/bc_093l065_4_2_3_xc150mm_utm09_2019.tif"


def _scene():
    """100 x 100 m flat ground, a 10 x 10 m flat 6 m roof and two conical trees."""
    n = int(100 / lidar.CELL)
    yy, xx = np.mgrid[0:n, 0:n] * lidar.CELL
    ndsm = np.zeros((n, n), "float32")
    ndsm[40:60, 40:60] = 6.0  # roof: rows/cols 40-60 -> 20-30 m
    for cx, cy, h in [(75, 75, 18.0), (15, 80, 9.0)]:
        r = np.hypot(xx - cx, yy - cy)
        ndsm = np.maximum(ndsm, np.clip(h - r * 3, 0, None)).astype("float32")
    rng = np.random.default_rng(0)
    ndsm[ndsm > 2] += rng.normal(0, 0.05, (ndsm > 2).sum()).astype("float32")
    return ndsm, from_origin(0, 100, lidar.CELL, lidar.CELL)


def test_trees_found_and_roof_masked():
    ndsm, tr = _scene()
    chm = lidar.canopy_height_model(ndsm, tr)
    trees = lidar.detect_trees(chm, tr)
    hs = sorted(round(t["height_m"]) for t in trees)
    assert len(trees) == 2, trees
    assert hs[1] >= 16 and 7 <= hs[0] <= 9


def test_refine_footprint_snaps_to_roof():
    ndsm, tr = _scene()
    offset_fp = box(22, 68, 32, 78)  # true roof is x 20-30, y 70-80; footprint shifted 2 m
    snapped = lidar.refine_footprint(ndsm, tr, offset_fp)
    assert snapped is not None
    assert snapped.centroid.distance(box(20, 70, 30, 80).centroid) < 0.6
