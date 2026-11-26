"""Human marks (correct / wrong) on findings, stored in data/labels.sqlite (git-ignored).

These become a small labelled test set for comparing backends later.
"""
from __future__ import annotations

import sqlite3
import time

from ..paths import DATA

DB = DATA / "labels.sqlite"


def _conn() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS marks (
        property_id TEXT NOT NULL, backend TEXT NOT NULL, model TEXT, zone TEXT NOT NULL,
        item_id TEXT NOT NULL, model_status TEXT, verdict TEXT NOT NULL CHECK (verdict IN ('correct','wrong')),
        true_status TEXT, note TEXT, checklist_version TEXT, updated TEXT NOT NULL,
        PRIMARY KEY (property_id, backend, zone, item_id))""")
    return c


def put(m: dict) -> None:
    with _conn() as c:
        c.execute("""INSERT INTO marks VALUES (:property_id,:backend,:model,:zone,:item_id,:model_status,
                     :verdict,:true_status,:note,:checklist_version,:updated)
                     ON CONFLICT(property_id,backend,zone,item_id) DO UPDATE SET
                     model=excluded.model, model_status=excluded.model_status, verdict=excluded.verdict,
                     true_status=excluded.true_status, note=excluded.note,
                     checklist_version=excluded.checklist_version, updated=excluded.updated""",
                  {"model": None, "model_status": None, "true_status": None, "note": None,
                   "checklist_version": None, **m, "updated": time.strftime("%Y-%m-%dT%H:%M:%S")})


def delete(property_id: str, backend: str, zone: str, item_id: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM marks WHERE property_id=? AND backend=? AND zone=? AND item_id=?",
                  (property_id, backend, zone, item_id))


def for_property(property_id: str) -> list[dict]:
    with _conn() as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute("SELECT * FROM marks WHERE property_id=?", (property_id,))]
