"""Re-check every model answer in Python: first against the schema, then against LiDAR facts.

Neither source is trusted blindly. Each finding gets a `lidar` verdict:
  agree     LiDAR measurements support the model's status
  disagree  LiDAR measurements contradict it (shown prominently)
  partial   consistent, but LiDAR cannot confirm the whole item (e.g. species, materials)
  n/a       LiDAR says nothing about this item
"""
from __future__ import annotations

import json
import re

from . import checklist

STATUSES = {"ok", "concern", "cant_tell"}
SLOPE_DEG = 10.0     # mean slope in 30-100 m treated as significant
DROP_M = 10.0        # and the ground falls at least this far below the home


def parse_and_check(text: str, zone: str, crop: dict | None) -> tuple[list[dict], list[str]]:
    """Return (findings, problems). Findings always cover every item of the zone once."""
    problems: list[str] = []
    items = [i["id"] for i in checklist.zone(zone)["items"]]
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError) as e:
        data = {}
        problems.append(f"not valid JSON: {e}")
    if not isinstance(data, dict):
        problems.append("top level is not an object")
        data = {}
    if data and data.get("zone") != zone:
        problems.append(f"zone field is {data.get('zone')!r}, expected {zone!r}")
    raw = data.get("findings", [])
    if not isinstance(raw, list):
        problems.append("findings is not a list")
        raw = []

    out: dict[str, dict] = {}
    for f in raw:
        if not isinstance(f, dict):
            problems.append("a finding is not an object")
            continue
        iid = f.get("item_id")
        if iid not in items:
            problems.append(f"unknown item_id {iid!r} dropped")
            continue
        if iid in out:
            problems.append(f"duplicate {iid} dropped")
            continue
        status = f.get("status")
        if status not in STATUSES:
            problems.append(f"{iid}: bad status {status!r}, set to cant_tell")
            status = "cant_tell"
        try:
            conf = float(f.get("confidence"))
        except (TypeError, ValueError):
            conf = 0.0
            problems.append(f"{iid}: missing confidence, set to 0")
        if not 0 <= conf <= 1:
            problems.append(f"{iid}: confidence {conf} clipped to 0-1")
            conf = min(1.0, max(0.0, conf))
        reason = str(f.get("reason") or "").strip()[:300]
        loc = f.get("location")
        if loc is not None:
            ok = (isinstance(loc, dict) and isinstance(loc.get("x"), (int, float))
                  and isinstance(loc.get("y"), (int, float)) and crop is not None
                  and 0 <= loc["x"] < crop["width"] and 0 <= loc["y"] < crop["height"])
            if not ok:
                problems.append(f"{iid}: location {loc} outside the crop, dropped")
                loc = None
            else:
                loc = {"x": int(loc["x"]), "y": int(loc["y"])}
        out[iid] = {"item_id": iid, "status": status, "confidence": round(conf, 2),
                    "reason": reason, "location": loc, "schema_ok": True}
    for iid in items:
        if iid not in out:
            problems.append(f"{iid}: missing from answer, recorded as cant_tell")
            out[iid] = {"item_id": iid, "status": "cant_tell", "confidence": 0.0,
                        "reason": "(no answer from model)", "location": None, "schema_ok": False}
    return [out[i] for i in items], problems


_NO_TREES = re.compile(r"\bno (trees?|conifers?|vegetation)\b|\bfree of (trees|conifers)\b|\btreeless\b", re.I)
_NO_BUILDINGS = re.compile(r"\bno (outbuildings?|sheds?|buildings?|structures?)\b", re.I)


def _v(result: str, note: str, lidar_status: str | None = None) -> dict:
    return {"result": result, "note": note, "lidar_status": lidar_status}


def lidar_check(finding: dict, facts: dict) -> dict:
    item = checklist.items_by_id()[finding["item_id"]]
    rule = item.get("lidar_check")
    s, reason = finding["status"], finding["reason"]
    if not rule or not facts.get("lidar_available"):
        return _v("n/a", "No LiDAR rule for this item." if not rule else "No LiDAR for this property.")
    hmin = facts.get("tree_min_height_m", 3)

    if rule == "crowns_touching_home":
        n = facts.get("crowns_touching_home", 0)
        if n and s == "ok":
            return _v("disagree", f"LiDAR finds {n} tree crown(s) reaching the house outline.", "concern")
        if not n and s == "concern":
            return _v("disagree", f"LiDAR finds no crowns of trees ≥{hmin} m touching the house; "
                                  "check for lower shrubs.", "ok")
        if s == "cant_tell":
            return _v("partial", f"LiDAR: {n} crown(s) touching the house.", "concern" if n else "ok")
        return _v("agree", f"LiDAR: {n} crown(s) touching the house.")

    if rule == "trees_in_zone":
        n = facts.get("trees_with_trunk_in_zone", 0)
        if n and _NO_TREES.search(reason):
            return _v("disagree", f"Reason says no trees, but LiDAR finds {n} tree(s) ≥{hmin} m in this zone.")
        if not n and s == "concern":
            return _v("disagree", f"LiDAR finds no trees ≥{hmin} m here; a concern needs smaller conifers.", "ok")
        if n and s == "ok":
            return _v("partial", f"LiDAR finds {n} tree(s) ≥{hmin} m; 'ok' holds only if all are deciduous.")
        if n and s == "concern":
            return _v("agree", f"LiDAR finds {n} tree(s) ≥{hmin} m (species not measured).")
        return _v("agree" if s == "ok" else "partial", f"LiDAR finds {n} tree(s) ≥{hmin} m.")

    if rule == "outbuildings_in_zone":
        n = facts.get("outbuildings_in_zone", 0)
        if n and _NO_BUILDINGS.search(reason):
            return _v("disagree", f"Reason says no buildings, but footprints show {n} in this zone.")
        if n and s == "ok":
            return _v("partial", f"Footprints show {n} building(s); 'ok' holds only if non-combustible.")
        if not n and s == "concern":
            return _v("partial", "Footprint data shows none, but it misses small sheds.")
        return _v("agree", f"Footprints show {n} building(s) in this zone.")

    if rule == "crown_gaps":
        n = facts.get("trees_with_trunk_in_zone", 0)
        share = facts.get("share_trees_crown_gap_under_3m")
        if n == 0:
            return (_v("disagree", f"LiDAR finds no trees ≥{hmin} m in this zone.", "ok") if s == "concern"
                    else _v("agree", "No trees in zone."))
        if share is None:
            return _v("n/a", "Too few trees to measure spacing.")
        note = f"{round(share * 100)}% of {n} trees have a crown less than 3 m from another."
        lidar_status = "concern" if share >= 0.5 else ("ok" if share <= 0.2 else None)
        if lidar_status and s != "cant_tell" and s != lidar_status:
            return _v("disagree", note, lidar_status)
        return _v("agree" if lidar_status == s else "partial", note, lidar_status)

    if rule == "canopy_cover":
        cover = facts.get("canopy_cover_pct")
        if cover is None:
            return _v("n/a", "No canopy model.")
        note = f"Canopy (>2 m) covers {cover}% of this zone."
        lidar_status = "concern" if cover >= 50 else ("ok" if cover <= 10 else None)
        if item["id"] == "out_fuel_type":
            # Low canopy can still be grass; high canopy could be deciduous: only flag the strong case.
            if cover <= 10 and s == "concern" and not re.search(r"grass|field|hay", reason, re.I):
                return _v("disagree", note + " Little forest fuel here.", "ok")
            return _v("partial", note + " Species not measured.")
        if lidar_status and s != "cant_tell" and s != lidar_status:
            return _v("disagree", note, lidar_status)
        return _v("agree" if lidar_status == s else "partial", note, lidar_status)

    if rule == "slope":
        slope, drop = facts.get("mean_slope_deg_30_100m"), facts.get("ground_drop_below_home_m")
        if slope is None or drop is None:
            return _v("n/a", "No terrain model.")
        lidar_status = "concern" if slope >= SLOPE_DEG and drop >= DROP_M else "ok"
        note = f"Mean slope {slope}°, ground falls {drop} m below the home within 100 m."
        if s == "cant_tell":
            return _v("partial", note, lidar_status)
        return _v("agree" if s == lidar_status else "disagree", note, lidar_status)

    return _v("n/a", f"Unknown rule {rule}.")


_TREF = re.compile(r"\bT(\d{1,4})\b")


def unknown_tree_refs(reason: str, listed_ids: set[str]) -> list[str]:
    """Tree labels the model cites that were not drawn for this zone (made up or misread)."""
    return sorted({f"T{n}" for n in _TREF.findall(reason)} - listed_ids)
