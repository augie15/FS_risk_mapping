# Risk score rules

Version `2026-11-26.1` (`RULES_VERSION` in `pipeline/score.py`). Based on FireSmart BC guidance
(Home Ignition Zones and the FireSmart home assessment checklist). **Not an official FireSmart
assessment** and not calibrated against one: the points are a transparent screening scale for
comparing homes and choosing what to fix first.

## Scale

| Part | Max | From |
|---|---|---|
| LiDAR rules | 80 | Fixed thresholds on measurements from LidarBC 2019 per zone |
| Confirmed AI findings | 20 | Checklist items LiDAR cannot measure, only when confirmed |
| **Score** | **100** | Higher means more hazard |

Bands: **Low** 0–19 · **Moderate** 20–39 · **High** 40–59 · **Extreme** 60–100.

No score is given while a property's home outline is still "Outline needed": every distance
depends on the outline.

## LiDAR rules (up to 80)

Trees are LiDAR tree tops 3 m or taller with watershed crowns. "Crown edge to home" is the gap
between the crown and the home outline (0 or less means touching or overhanging).

| Id | Zone | Rule | Points |
|---|---|---|---|
| L1 | Immediate 0–1.5 m | Any crown touching or over the house | 15 |
| L2 | Immediate | Any tree trunk within 1.5 m | 5 |
| L3 | Immediate | Canopy over more than 10% of the strip | 5 |
| L4 | Intermediate 1.5–10 m | Trees with crown edge within 10 m: 1–2 / 3–5 / 6 or more | 4 / 8 / 12 |
| L5 | Intermediate | Canopy cover of the zone: 15% / 30% or more | 2 / 4 |
| L6 | Intermediate | Any other building 1.5–10 m from the house | 4 |
| L7 | Extended 10–30 m | Canopy cover: 15% / 35% / 60% or more | 3 / 6 / 10 |
| L8 | Extended | Share of trees with another crown within 3 m: 25% / 50% or more | 2 / 5 |
| L9 | 30–100 m | Canopy cover: 15% / 35% / 60% or more | 3 / 6 / 10 |
| L10 | 30–100 m | Mean slope: 5° / 10° / 20° or more | 1 / 3 / 5 |
| L11 | 30–100 m | Ground drop below the home (home minus 10th percentile of the ring): 2 / 5 / 10 m or more | 1 / 3 / 5 |

Zone maxima: Immediate 25, Intermediate 20, Extended 15, 30–100 m 20 (the sum is capped at 80).
Canopy cover is the share of 0.5 m canopy-height cells above 2 m inside the zone ring.

What LiDAR cannot do, and the score does not pretend to: tell conifers from deciduous trees (the
biggest FireSmart factor in the 10 m zone), see anything that changed since 2019, or see the
ground under the canopy.

## AI findings (up to 20)

Only these checklist items can add points, because LiDAR cannot measure them:

| Item | Points |
|---|---|
| `imm_roof_material` (wood shake roof) | 6 |
| `imm_combustibles` (plants, mulch, stored items in the 1.5 m strip) | 4 |
| `imm_attachments` (wooden deck or fence joined to the house) | 4 |
| `imm_roof_debris` | 2 |
| `int_woodpile` | 3 |
| `int_vehicles_propane` | 3 |
| `int_grass_mulch` | 2 |
| `ext_ladder_fuels` | 3 |
| `ext_surface_debris` | 2 |

An AI "concern" counts only when it is confirmed, in this order:

1. **Operator mark.** On the compare page: a stated true status wins; "correct" on a model's
   answer confirms that answer; "wrong" discards that model's answer.
2. **Both models agree.** With no mark, both the local and the Claude results (current, not out of
   date) say "concern" with confidence 0.6 or more, and neither answer had a validation problem.

Anything else that a model called a concern is listed as **"check on site"** with 0 points.
AI answers on the LiDAR-checkable items (overhanging trees, conifers in 10 m, outbuildings, crown
spacing, canopy, slope) never add points: the LiDAR rules above cover them.

## Findings, numbers and "fix first"

Scored findings are numbered by points (ties: nearer zone first), then the "check on site" items.
"What to fix first" is the top three scored findings. On the map and in the PDF, a finding about
specific trees or buildings is pinned on them; a finding about a whole zone (canopy cover, crown
spacing, slope) is shown as a tag on that zone's ring and exported as the ring polygon.

## Outputs

- `GET api/property/<id>/score`: the score as JSON (all rules, including 0-point ones).
- `GET api/property/<id>/report.pdf`: report with the map, rings, numbered findings and fixes.
  The map uses the LidarBC orthophoto, never Google imagery.
- `GET api/property/<id>/download/<name>`: `outline.geojson`, `zones.geojson`,
  `findings.geojson` (WGS84), `chm.tif` (canopy height, m) and `slope.tif` (degrees), both
  GeoTIFF in the run's UTM zone at 0.5 m.

## Changing the rules

Edit `pipeline/score.py`, bump `RULES_VERSION`, and update this file and `tests/test_score.py`.
