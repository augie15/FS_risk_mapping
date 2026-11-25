# Aerial FireSmart checklist

The checklist the vision models answer lives in
[`config/firesmart_checklist.json`](../config/firesmart_checklist.json) (version field inside;
bump it whenever an item changes, because stored marks record the version they were made against).

It is **based on FireSmart BC guidance; it is not an official FireSmart assessment.** Only items
that can plausibly be judged from a 15 cm aerial photo plus LiDAR are included. Items that need a
person on site (vents, window glazing, door seals, siding material, deck underside, spark arrestor,
access routes, address signage) are left out on purpose.

## Sources

1. FireSmart BC, *FireSmart BC Begins At Home Guide*, web edition,
   <https://begins-at-home-guide.firesmartbc.ca/>, read 2026-11-25. Zone definitions and the
   per-zone recommendations below (roof class, 1.5 m non-combustible strip, no conifers within
   10 m, firewood away from the house, conifer crowns 3 m apart and pruned to 2 m in 10-30 m,
   ladder fuels and woody debris removed, outbuildings within 10 m treated like the house).
2. FireSmart BC, *Home Ignition Zone Self-Assessment*, form 2307-02 (PDF last modified
   2025-06-16), <https://firesmartbc.ca/resource/hiz-self-assessment/>. Same three zones
   (0-1.5 m, 1.5-10 m, 10-30 m). The PDF's questions are vector artwork, so wording here follows
   source 1.

FireSmart, Intelli-feu and associated marks are trademarks of the Canadian Interagency Forest
Fire Centre (CIFFC).

## Items

| Zone | Item id | What is checked | Re-checked against |
|---|---|---|---|
| 0-1.5 m | `imm_combustibles` | Combustibles in the 1.5 m strip | – |
| | `imm_roof_material` | Roof covering fire-rated vs wood shake | – |
| | `imm_roof_debris` | Needles / leaves on roof and gutters | – |
| | `imm_overhanging_trees` | Crowns over or touching the roof | LiDAR crowns touching the house |
| | `imm_attachments` | Wooden decks / fences joined to the house | – |
| 1.5-10 m | `int_conifers` | No conifers within 10 m | LiDAR trees ≥ 3 m in zone |
| | `int_woodpile` | No firewood / lumber piles | – |
| | `int_outbuildings` | Outbuildings within 10 m | Building footprints in zone |
| | `int_vehicles_propane` | Vehicles, RVs, propane by the house | – |
| | `int_grass_mulch` | Short green grass / gravel, not dry grass, bark mulch, dense shrubs | – |
| 10-30 m | `ext_crown_spacing` | Crowns at least 3 m apart | LiDAR crown gaps |
| | `ext_canopy_continuity` | Canopy break between forest and house | LiDAR canopy cover |
| | `ext_ladder_fuels` | Small conifers / shrubs under tall trees | – |
| | `ext_surface_debris` | Slash piles, deadfall | – |
| 30-100 m | `out_fuel_type` | Dominant fuel type | LiDAR canopy cover (partial) |
| | `out_continuous_forest` | Continuous forest that could carry crown fire | LiDAR canopy cover |
| | `out_slope` | Slope below the home | LiDAR terrain (drop below home, mean slope) |

Beyond 30 m the FireSmart BC homeowner guide gives no itemised recommendations; the 30-100 m
items are landscape context for the later risk score.
