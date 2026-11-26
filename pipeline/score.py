"""Wildfire risk score per property, based on FireSmart BC guidance (not an official assessment).

Mostly fixed rules on LiDAR measurements per Home Ignition Zone (up to 80 points). AI findings add
up to 20 points, and only for checklist items LiDAR cannot measure, and only when they are
confirmed: marked correct by the operator, or the same concern from both AI models. Everything
else from the AI is listed as "check on site" with no points. Rules: docs/RISK_SCORE.md.
"""
from __future__ import annotations

import json
import time

from .ai import checklist, marks
from .ai.zones import PropertyData, zone_facts
from .property import PROCESSED

RULES_VERSION = "2026-11-26.1"
LIDAR_MAX = 80
AI_MAX = 20
BANDS = [(60, "Extreme"), (40, "High"), (20, "Moderate"), (0, "Low")]
ZONE_ORDER = ["immediate", "intermediate", "extended", "outer"]
ZONE_LABEL = {"immediate": "Immediate 0–1.5 m", "intermediate": "Intermediate 1.5–10 m",
              "extended": "Extended 10–30 m", "outer": "30–100 m"}

# Points for an AI concern on items LiDAR cannot measure, and what to do about it.
AI_ITEMS = {
    "imm_roof_material": (6, "Replace a wood-shake roof with a fire-rated covering (metal, asphalt, tile)."),
    "imm_combustibles": (4, "Clear plants, bark mulch and stored items from the 1.5 m strip; use gravel or rock."),
    "imm_attachments": (4, "Separate or rebuild wooden decks and fences joined to the house with non-combustible parts."),
    "imm_roof_debris": (2, "Clean needles and leaves off the roof and out of the gutters."),
    "int_woodpile": (3, "Move firewood and lumber more than 10 m from the house."),
    "int_vehicles_propane": (3, "Store vehicles, trailers and propane tanks more than 10 m from the house."),
    "int_grass_mulch": (2, "Keep grass mown short and green within 10 m; replace bark mulch with gravel."),
    "ext_ladder_fuels": (3, "Remove small conifers and shrubs under tall trees; prune lower branches to 2 m."),
    "ext_surface_debris": (2, "Clear slash piles, deadfall and woody debris within 30 m."),
}
MIN_AGREE_CONFIDENCE = 0.6


def band(score: float) -> str:
    return next(name for lo, name in BANDS if score >= lo)


def _step(value, steps):
    """Points for the first (threshold, points) whose threshold value reaches."""
    if value is None:
        return 0
    for thr, pts in steps:
        if value >= thr:
            return pts
    return 0


def _ll(pd: PropertyData, x: float, y: float) -> list[float]:
    lng, lat = pd.to_ll(x, y)
    return [round(lng, 7), round(lat, 7)]


def _tree_ids(trees, n=6) -> str:
    ids = [t["id"] for t in trees[:n]]
    return ", ".join(ids) + (f" and {len(trees) - n} more" if len(trees) > n else "")


def lidar_rules(pd: PropertyData, facts: dict[str, dict]) -> list[dict]:
    """The fixed LiDAR rules. Each returns a finding dict (points may be 0)."""
    out = []

    def add(rid, zone, title, points, max_points, evidence, fix, locations, whole_zone=False):
        """whole_zone: the finding is about the zone as a whole (no point locations)."""
        out.append({"id": rid, "source": "lidar", "zone": zone, "title": title, "points": points,
                    "max_points": max_points, "evidence": evidence, "fix": fix,
                    "locations": [] if whole_zone else locations, "whole_zone": whole_zone,
                    "approx_location": False, "basis": "LiDAR measurement"})

    trees = sorted(pd.trees, key=lambda t: t["crown_edge_to_home_m"])
    im, it, ex, ou = (facts[z] for z in ZONE_ORDER)

    touching = [t for t in trees if t["crown_edge_to_home_m"] <= 0.0]
    add("L1", "immediate", "Tree crowns touching or over the house", 15 if touching else 0, 15,
        f"{len(touching)} crown(s) reach the house" + (f" ({_tree_ids(touching)})" if touching else ""),
        f"Remove or cut back the trees whose crowns touch the house ({_tree_ids(touching)})." if touching else "",
        [_ll(pd, t["x"], t["y"]) for t in touching])

    trunks = [t for t in trees if t["zone"] == "immediate"]
    add("L2", "immediate", "Trees growing within 1.5 m of the house", 5 if trunks else 0, 5,
        f"{len(trunks)} tree trunk(s) within 1.5 m",
        "Remove trees growing right against the house." if trunks else "",
        [_ll(pd, t["x"], t["y"]) for t in trunks])

    cc = im.get("canopy_cover_pct")
    add("L3", "immediate", "Canopy over the 1.5 m strip", 5 if (cc or 0) > 10 else 0, 5,
        f"Canopy cover {cc}% of the 0–1.5 m zone" if cc is not None else "No canopy model",
        "Prune branches back so nothing hangs over the 1.5 m strip." if (cc or 0) > 10 else "",
        None, whole_zone=True)

    near = [t for t in trees if t["crown_edge_to_home_m"] < 10.0]
    pts = _step(len(near), [(6, 12), (3, 8), (1, 4)])
    add("L4", "intermediate", "Trees with crowns within 10 m of the house", pts, 12,
        f"{len(near)} tree crown(s) within 10 m" + (f" ({_tree_ids(near)})" if near else "")
        + "; LiDAR cannot tell conifers from deciduous trees",
        f"Thin trees within 10 m, conifers first; keep deciduous trees well spaced ({_tree_ids(near)})."
        if near else "",
        [_ll(pd, t["x"], t["y"]) for t in near])

    cc = it.get("canopy_cover_pct")
    pts = _step(cc, [(30, 4), (15, 2)])
    add("L5", "intermediate", "Canopy cover 1.5–10 m", pts, 4,
        f"Canopy cover {cc}% of the 1.5–10 m zone" if cc is not None else "No canopy model",
        "Open up the canopy within 10 m so crowns do not join." if pts else "",
        None, whole_zone=True)

    n_out = it.get("outbuildings_in_zone", 0) or 0
    lo, hi = pd.zones["intermediate"][0]["inner_m"], pd.zones["intermediate"][0]["outer_m"]
    obs = [g for _p, g in pd.others if lo <= g.distance(pd.home) < hi]
    add("L6", "intermediate", "Outbuildings within 10 m", 4 if n_out else 0, 4,
        f"{n_out} other building(s) 1.5–10 m from the house",
        "Move sheds more than 10 m away, or make them fire-resistant and keep them clear." if n_out else "",
        [_ll(pd, g.centroid.x, g.centroid.y) for g in obs])

    cc = ex.get("canopy_cover_pct")
    pts = _step(cc, [(60, 10), (35, 6), (15, 3)])
    add("L7", "extended", "Canopy cover 10–30 m", pts, 10,
        f"Canopy cover {cc}% of the 10–30 m zone" if cc is not None else "No canopy model",
        "Thin the stand within 30 m to break up continuous canopy." if pts else "",
        None, whole_zone=True)

    share = ex.get("share_trees_crown_gap_under_3m")
    pts = _step(share, [(0.5, 5), (0.25, 2)])
    add("L8", "extended", "Crowns closer than 3 m apart (10–30 m)", pts, 5,
        f"{round(100 * share)}% of trees have a crown within 3 m of another" if share is not None
        else "Fewer than 2 trees in the zone",
        "Space tree crowns at least 3 m apart within 30 m." if pts else "",
        None, whole_zone=True)

    cc = ou.get("canopy_cover_pct")
    pts = _step(cc, [(60, 10), (35, 6), (15, 3)])
    add("L9", "outer", "Canopy cover 30–100 m", pts, 10,
        f"Canopy cover {cc}% of the 30–100 m zone" if cc is not None else "No canopy model",
        "Work with neighbours on thinning and fuel breaks within 100 m." if pts else "",
        None, whole_zone=True)

    sl = ou.get("mean_slope_deg_30_100m")
    pts = _step(sl, [(20, 5), (10, 3), (5, 1)])
    add("L10", "outer", "Slope around the home (30–100 m)", pts, 5,
        f"Mean slope {sl}°" if sl is not None else "No terrain model",
        "On sloping ground, extend the cleared zones further downhill." if pts else "",
        None, whole_zone=True)

    drop = ou.get("ground_drop_below_home_m")
    pts = _step(drop, [(10, 5), (5, 3), (2, 1)])
    add("L11", "outer", "Ground falling away below the home", pts, 5,
        f"Ground drops {drop} m below the home within 100 m" if drop is not None else "No terrain model",
        "Fire runs uphill: clear fuels on the downhill side first." if pts else "",
        None, whole_zone=True)
    return out


def _load_ai(pd: PropertyData) -> dict[str, dict]:
    """Current (not stale) AI results by backend."""
    out = {}
    for f in sorted((pd.dir / "ai").glob("*.json")) if (pd.dir / "ai").exists() else []:
        if f.stem not in ("local", "claude"):
            continue
        res = json.loads(f.read_text())
        if (res.get("property_run_id") != pd.summary.get("run_id")
                or res.get("pipeline_version") != pd.summary.get("pipeline_version")):
            continue
        out[f.stem] = res
    return out


def ai_rules(pd: PropertyData) -> tuple[list[dict], dict]:
    """AI findings on non-LiDAR items: scored when confirmed, otherwise 'check on site'."""
    results = _load_ai(pd)
    mk = {(m["backend"], m["zone"], m["item_id"]): m for m in marks.for_property(pd.pid)}
    items = checklist.items_by_id()
    answers: dict[str, dict] = {}   # item_id -> backend -> finding
    for be, res in results.items():
        for z in res["zones"]:
            bad = {p.split(":", 1)[0] for p in z.get("problems", [])}
            for f in z["findings"]:
                if f["item_id"] in AI_ITEMS:
                    answers.setdefault(f["item_id"], {})[be] = {**f, "zone": z["zone"],
                                                               "has_problem": f["item_id"] in bad}
    out = []
    for item_id, by_be in answers.items():
        pts_max, fix = AI_ITEMS[item_id]
        zone = items[item_id]["zone"]
        # Operator marks win: a stated true status, or a backend marked correct; "wrong" discards it.
        truth, basis = None, None
        usable = {}
        for be, f in by_be.items():
            m = mk.get((be, f["zone"], item_id))
            if m and m.get("true_status"):
                truth, basis = m["true_status"], "confirmed by operator"
            elif m and m["verdict"] == "correct":
                truth, basis = truth or f["status"], basis or "confirmed by operator"
            elif m and m["verdict"] == "wrong":
                continue
            else:
                usable[be] = f
        if truth is None:
            concerns = [f for f in usable.values() if f["status"] == "concern"]
            if (len(results) >= 2 and len(usable) >= 2 and len(concerns) == len(usable)
                    and all(f["confidence"] >= MIN_AGREE_CONFIDENCE and not f["has_problem"] for f in concerns)):
                truth, basis = "concern", "both AI models agree"
            elif concerns:
                truth, basis = "unconfirmed", "one AI model only: check on site"
        if truth not in ("concern", "unconfirmed"):
            continue
        src = next((f for f in by_be.values() if f["status"] == "concern" and not f["geo"]["approx"]),
                   next((f for f in by_be.values() if f["status"] == "concern"), next(iter(by_be.values()))))
        out.append({"id": item_id, "source": "ai", "zone": zone,
                    "title": items[item_id]["question"],
                    "points": pts_max if truth == "concern" else 0, "max_points": pts_max,
                    "evidence": "; ".join(f"{be}: {f['reason']}" for be, f in by_be.items()
                                          if f["status"] == "concern"),
                    "fix": fix, "locations": [[src["geo"]["lng"], src["geo"]["lat"]]],
                    "approx_location": bool(src["geo"]["approx"]), "whole_zone": False, "basis": basis,
                    "confirmed": truth == "concern"})
    meta = {"backends_used": sorted(results), "marks": len(mk)}
    return out, meta


def compute(pid: str) -> dict:
    summ = json.loads((PROCESSED / pid / "summary.json").read_text())
    if summ.get("status") == "outline_needed":   # no zones or trees on disk yet
        raise ValueError("outline needed: draw and save the home outline first")
    pd = PropertyData(pid)
    facts = {z: zone_facts(pd, z, None) for z in ZONE_ORDER}
    lid = lidar_rules(pd, facts)
    ai, ai_meta = ai_rules(pd)
    lidar_pts = min(LIDAR_MAX, sum(f["points"] for f in lid))
    ai_pts = min(AI_MAX, sum(f["points"] for f in ai))
    score = lidar_pts + ai_pts

    scored = [f for f in lid + ai if f["points"] > 0]
    checks = [f for f in ai if f["points"] == 0]
    scored.sort(key=lambda f: (-f["points"], ZONE_ORDER.index(f["zone"])))
    numbered = scored + sorted(checks, key=lambda f: ZONE_ORDER.index(f["zone"]))
    for i, f in enumerate(numbered, 1):
        f["n"] = i
    zones = {z: {"label": ZONE_LABEL[z],
                 "lidar_points": sum(f["points"] for f in lid if f["zone"] == z),
                 "ai_points": sum(f["points"] for f in ai if f["zone"] == z)} for z in ZONE_ORDER}
    return {
        "property_id": pid, "rules_version": RULES_VERSION,
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "property_run_id": pd.summary.get("run_id"),
        "score": score, "max": LIDAR_MAX + AI_MAX, "band": band(score),
        "lidar_points": lidar_pts, "lidar_max": LIDAR_MAX, "ai_points": ai_pts, "ai_max": AI_MAX,
        "zones": zones, "findings": numbered,
        "fix_first": [f["n"] for f in scored[:3]],
        "rules": lid, "ai": ai_meta,
        "home": {"area_m2": round(pd.home.area), "outline": pd.summary["home"].get("source")},
        "lidar_available": pd.summary.get("lidar") is not None,
        "label": "Based on FireSmart BC guidance. Not an official FireSmart assessment.",
    }
