"""Run one backend over all zones of a processed property and save the checked findings.

Output: data/processed/<id>/ai/<backend>.json (+ crop_<zone>.png).
"""
from __future__ import annotations

import json
import time


from . import backends, checklist, prompt, validate
from .zones import PropertyData, make_crop, pixel_to_lnglat, zone_facts


def prepare(pid: str) -> tuple[PropertyData, dict]:
    """Crops and facts for every zone (cached on disk, cheap to rebuild)."""
    pd = PropertyData(pid)
    out = pd.dir / "ai"
    out.mkdir(exist_ok=True)
    zones = {}
    for z in checklist.load()["zones"]:
        key = z["zone"]
        crop = make_crop(pd, key, out)
        zones[key] = {"crop": crop, "facts": zone_facts(pd, key, crop)}
    (out / "inputs.json").write_text(json.dumps(zones, indent=1))
    return pd, zones


def _fallback_point(pd: PropertyData, zone: str, idx: int, n: int):
    """Where to pin a finding with no location: spread around the middle of its ring."""
    import math
    props, ring = pd.zones[zone]
    mid = (props["inner_m"] + props["outer_m"]) / 2
    c = pd.home.centroid
    ang = 2 * math.pi * (idx + 0.5) / max(n, 1)
    # distance from centroid ~ half house size + mid-ring distance
    r = (pd.home.area ** 0.5) / 2 + mid
    return pd.to_ll(c.x + r * math.cos(ang), c.y + r * math.sin(ang))


def run(pid: str, backend_name: str, progress=lambda m: None) -> dict:
    be = backends.get(backend_name)
    ok, why = be.available()
    if not ok:
        raise RuntimeError(why)
    progress(f"Preparing zone crops for {backend_name}")
    pd, inputs = prepare(pid)
    result = {"property_id": pid, "backend": be.name, "model": be.model,
              "checklist_version": checklist.load()["version"],
              "property_run_id": pd.summary.get("run_id"),
              "pipeline_version": pd.summary.get("pipeline_version"),
              "started": time.strftime("%Y-%m-%dT%H:%M:%S"), "zones": []}
    for z in checklist.load()["zones"]:
        key = z["zone"]
        crop, facts = inputs[key]["crop"], inputs[key]["facts"]
        img = (pd.dir / "ai" / crop["file"]).read_bytes() if crop else None
        progress(f"{be.name}: asking about the {z['label']}")
        zr = {"zone": key, "label": z["label"], "crop": crop["file"] if crop else None,
              "crop_w": crop["width"] if crop else None, "crop_h": crop["height"] if crop else None,
              "facts": facts}
        try:
            res = be.call(prompt.SYSTEM, prompt.user_text(key, facts, crop), img, prompt.schema(key))
        except backends.SpendCapExceeded as e:
            if isinstance(e, backends.DailyCapReached) and not any(
                    r.get("cost_usd") for r in result["zones"]):
                raise  # nothing paid for yet: refuse the run, keep any earlier results on disk
            zr.update(error=f"skipped: {e}", findings=[], latency_s=0, input_tokens=0,
                      output_tokens=0, cost_usd=0)
            result["zones"].append(zr)
            continue
        backends.log_call(res, property_id=pid, zone=key)
        findings, problems = validate.parse_and_check(res.text, key, crop) if not res.error else ([], [])
        if res.error:
            findings, problems = validate.parse_and_check("{}", key, crop)
            problems.insert(0, res.error)
        n = len(findings)
        # Every LiDAR tree whose trunk or crown edge is in this zone, not only the 40 drawn/listed:
        # a model may correctly name a tree it reads off the crop of a busy zone.
        listed = {t["id"] for t in pd.trees if key in (t["zone"], t["crown_zone"])}
        for i, f in enumerate(findings):
            f["lidar"] = validate.lidar_check(f, facts)
            bad = validate.unknown_tree_refs(f["reason"], listed)
            if bad:
                f["unknown_tree_refs"] = bad
                problems.append(f"{f['item_id']}: cites {', '.join(bad)}, not LiDAR trees in this zone")
            if f["location"] and crop:
                lng, lat = pixel_to_lnglat(pd, crop, f["location"]["x"], f["location"]["y"])
                f["geo"] = {"lng": round(lng, 7), "lat": round(lat, 7), "approx": False}
            else:
                lng, lat = _fallback_point(pd, key, i, n)
                f["geo"] = {"lng": round(lng, 7), "lat": round(lat, 7), "approx": True}
            f["question"] = checklist.items_by_id()[f["item_id"]]["question"]
        zr.update(findings=findings, problems=problems, error=res.error, latency_s=res.latency_s,
                  input_tokens=res.input_tokens, output_tokens=res.output_tokens,
                  cost_usd=res.cost_usd, extra=res.extra)
        result["zones"].append(zr)
    zs = result["zones"]
    result["totals"] = {
        "latency_s": round(sum(z["latency_s"] for z in zs), 1),
        "input_tokens": sum(z["input_tokens"] for z in zs),
        "output_tokens": sum(z["output_tokens"] for z in zs),
        "cost_usd": round(sum(z["cost_usd"] for z in zs), 4),
        "errors": sum(1 for z in zs if z.get("error")),
        "lidar_disagreements": sum(1 for z in zs for f in z["findings"] if f["lidar"]["result"] == "disagree"),
    }
    result["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    (pd.dir / "ai" / f"{be.name}.json").write_text(json.dumps(result, indent=1))
    progress("Done")
    return result
