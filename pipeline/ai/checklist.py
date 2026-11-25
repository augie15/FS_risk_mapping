"""Load the versioned checklist in config/firesmart_checklist.json."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

PATH = Path(__file__).resolve().parents[2] / "config" / "firesmart_checklist.json"


@lru_cache(maxsize=1)
def load() -> dict:
    return json.loads(PATH.read_text())


def zone(zone_key: str) -> dict:
    return next(z for z in load()["zones"] if z["zone"] == zone_key)


def items_by_id() -> dict[str, dict]:
    return {i["id"]: {**i, "zone": z["zone"]} for z in load()["zones"] for i in z["items"]}
