"""Offline tests for the risk score rules, AI confirmation and GIS exports (synthetic data)."""
import json
from types import SimpleNamespace

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from pipeline import gis, score


def test_bands_and_steps():
    assert [score.band(v) for v in (0, 19, 20, 39, 40, 59, 60, 100)] == \
        ["Low", "Low", "Moderate", "Moderate", "High", "High", "Extreme", "Extreme"]
    steps = [(60, 10), (35, 6), (15, 3)]
    assert [score._step(v, steps) for v in (None, 0, 15, 34.9, 35, 99)] == [0, 0, 3, 3, 6, 10]


def test_rule_maxima_add_up_to_lidar_cap():
    # Zone maxima in docs/RISK_SCORE.md: 25 + 20 + 15 + 20 = 80
    assert score.LIDAR_MAX + score.AI_MAX == 100
    assert sum(v[0] for v in score.AI_ITEMS.values()) >= score.AI_MAX


def _finding(item, status, conf=0.8, approx=False):
    return {"item_id": item, "status": status, "confidence": conf, "reason": f"{status} reason",
            "geo": {"lng": -127.0, "lat": 54.7, "approx": approx}}


def _write_ai(d, backend, findings, run_id="r1", problems=()):
    (d / "ai").mkdir(exist_ok=True)
    zones = {}
    for f in findings:
        zone = {"imm": "immediate", "int": "intermediate", "ext": "extended", "out": "outer"}[f["item_id"][:3]]
        zones.setdefault(zone, []).append(f)
    res = {"property_run_id": run_id, "pipeline_version": 8,
           "zones": [{"zone": z, "findings": fs, "problems": list(problems)} for z, fs in zones.items()]}
    (d / "ai" / f"{backend}.json").write_text(json.dumps(res))


@pytest.fixture
def pd(tmp_path, monkeypatch):
    monkeypatch.setattr(score.marks, "for_property", lambda pid: [])
    return SimpleNamespace(pid="abc", dir=tmp_path, summary={"run_id": "r1", "pipeline_version": 8})


def test_ai_both_agree_scores(pd):
    _write_ai(pd.dir, "local", [_finding("int_woodpile", "concern")])
    _write_ai(pd.dir, "claude", [_finding("int_woodpile", "concern", 0.7)])
    out, meta = score.ai_rules(pd)
    assert meta["backends_used"] == ["claude", "local"]
    (f,) = out
    assert f["points"] == 3 and f["basis"] == "both AI models agree" and f["confirmed"]


def test_ai_one_model_or_low_confidence_is_check_on_site(pd):
    _write_ai(pd.dir, "local", [_finding("int_woodpile", "concern"), _finding("imm_combustibles", "concern", 0.4)])
    _write_ai(pd.dir, "claude", [_finding("int_woodpile", "ok"), _finding("imm_combustibles", "concern")])
    out, _ = score.ai_rules(pd)
    assert {f["id"]: f["points"] for f in out} == {"int_woodpile": 0, "imm_combustibles": 0}
    assert all("check on site" in f["basis"] for f in out)


def test_ai_validation_problem_blocks_agreement(pd):
    _write_ai(pd.dir, "local", [_finding("int_woodpile", "concern")], problems=["int_woodpile: confidence 2 clipped"])
    _write_ai(pd.dir, "claude", [_finding("int_woodpile", "concern")])
    (f,) = score.ai_rules(pd)[0]
    assert f["points"] == 0


def test_ai_lidar_items_never_score(pd):
    _write_ai(pd.dir, "local", [_finding("imm_overhanging_trees", "concern")])
    _write_ai(pd.dir, "claude", [_finding("imm_overhanging_trees", "concern")])
    assert score.ai_rules(pd)[0] == []


def test_ai_stale_results_ignored(pd):
    _write_ai(pd.dir, "local", [_finding("int_woodpile", "concern")])
    _write_ai(pd.dir, "claude", [_finding("int_woodpile", "concern")], run_id="old")
    out, meta = score.ai_rules(pd)
    assert meta["backends_used"] == ["local"] and out[0]["points"] == 0


def test_operator_marks_win(pd, monkeypatch):
    _write_ai(pd.dir, "local", [_finding("int_woodpile", "concern"), _finding("imm_roof_material", "concern"),
                                _finding("ext_ladder_fuels", "ok")])
    _write_ai(pd.dir, "claude", [_finding("int_woodpile", "concern"), _finding("imm_roof_material", "ok"),
                                 _finding("ext_ladder_fuels", "ok")])
    monkeypatch.setattr(score.marks, "for_property", lambda pid: [
        # both models said woodpile, but the operator says the local one is wrong and claude too
        {"backend": "local", "zone": "intermediate", "item_id": "int_woodpile", "verdict": "wrong", "true_status": None},
        {"backend": "claude", "zone": "intermediate", "item_id": "int_woodpile", "verdict": "wrong", "true_status": None},
        # only local saw the shake roof; operator confirms
        {"backend": "local", "zone": "immediate", "item_id": "imm_roof_material", "verdict": "correct", "true_status": None},
        # both said ok on ladder fuels; operator states the true status is concern
        {"backend": "claude", "zone": "extended", "item_id": "ext_ladder_fuels", "verdict": "wrong", "true_status": "concern"},
    ])
    out = {f["id"]: f for f in score.ai_rules(pd)[0]}
    assert "int_woodpile" not in out
    assert out["imm_roof_material"]["points"] == 6 and out["imm_roof_material"]["basis"] == "confirmed by operator"
    assert out["ext_ladder_fuels"]["points"] == 3


def test_slope_tif_of_a_plane(tmp_path, monkeypatch):
    monkeypatch.setattr(gis, "PROCESSED", tmp_path)
    d = tmp_path / "abc"
    d.mkdir()
    _, xx = np.mgrid[0:40, 0:40]
    dtm = (xx * 0.5 * 1.0).astype("float32")           # rises 1 m per metre eastward: 45 degrees
    prof = {"driver": "GTiff", "width": 40, "height": 40, "count": 1, "dtype": "float32",
            "crs": "EPSG:26909", "transform": from_origin(600000, 6060000, 0.5, 0.5)}
    with rasterio.open(d / "dtm.tif", "w", **prof) as dst:
        dst.write(dtm, 1)
    out = gis.slope_tif("abc")
    with rasterio.open(out) as src:
        a = src.read(1)
        assert src.crs.to_epsg() == 26909 and src.nodata == gis.NODATA
    assert np.allclose(a[5:-5, 5:-5], 45.0, atol=0.01)


def test_findings_geojson_points_and_zone_polygons(tmp_path, monkeypatch):
    monkeypatch.setattr(gis, "PROCESSED", tmp_path)
    (tmp_path / "abc").mkdir()
    ring = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}
    (tmp_path / "abc" / "zones.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"zone": "extended"}, "geometry": ring}]}))
    base = {"source": "lidar", "title": "t", "points": 3, "max_points": 5, "basis": "b", "evidence": "e",
            "fix": "f", "approx_location": False}
    sc = {"property_id": "abc", "rules_version": "x", "findings": [
        {**base, "n": 1, "id": "L4", "zone": "intermediate", "locations": [[1, 2], [3, 4]], "whole_zone": False},
        {**base, "n": 2, "id": "L7", "zone": "extended", "locations": [], "whole_zone": True}]}
    fc = gis.findings_geojson(sc)
    kinds = [(f["properties"]["n"], f["geometry"]["type"]) for f in fc["features"]]
    assert kinds == [(1, "Point"), (1, "Point"), (2, "Polygon")]
