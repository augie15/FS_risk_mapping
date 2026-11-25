# Data sources

Checked 2026-11-25 for Telkwa / Bulkley Valley (around 54.67 N, 127.05 W). Every source below
was queried live from this machine; the "Telkwa result" column is what came back.

**Rule:** Google Maps imagery is for display only. The Google Maps Platform terms forbid
extracting or deriving data from it, so every analysis step uses the open data below.
Anything we share is "based on FireSmart BC guidance", not an official FireSmart assessment.

## Summary

| Need | Source | Resolution / density | Date | Licence | Telkwa result |
|---|---|---|---|---|---|
| Orthophoto (analysis) | LidarBC object store, NDMP orthophotos | **15 cm** RGB, COG-style tiled GeoTIFF with overviews, UTM 9 (EPSG:26909) | 2019 (some 093L sheets also 2021) | OGL-BC | Covered (sheet 093L065 and neighbours) |
| Orthophoto (older, fallback) | BC provincial orthophoto program, imagex WMS | 0.5 m / 1 m | 2003, 2006 | OGL-BC / access only via WMS | Covered, too old to rely on |
| LiDAR point cloud | LidarBC object store | **8 pts/m²**, LAZ 1.4, classes 1 (unclassified), 2 (ground), 7 (noise) | 2019 ("NDMP Floodplains 2019") | OGL-BC | Covered |
| LiDAR DEM | LidarBC object store | 1 m bare-earth GeoTIFF (whole 1:20k sheet, ~600 MB) | 2019 | OGL-BC | Covered; not needed, we build the DTM from the points |
| Parcels | ParcelMap BC Parcel Fabric (public view, no owner names) | Survey fabric polygons | Updated continuously | OGL-BC | Covered |
| Buildings | Microsoft Global ML Building Footprints | ML-traced polygons, some with height | Release 2026-02-03 | ODbL | 528 footprints in the Telkwa area |
| Buildings (fallback) | OpenStreetMap via Overpass API | Hand-traced | Live | ODbL | Only 12 buildings in the Telkwa area |
| Landscape NDVI / NBR (later) | Sentinel-2 L2A via Element84 Earth Search STAC on AWS | 10 m / 20 m, ~5-day revisit | Live archive | Copernicus open licence | 13 scenes under 10 % cloud, Jun-Sep 2026 |

## Orthophotos

**Use: LidarBC 2019 15 cm orthophotos.** Flown with the 2019 floodplain LiDAR, so imagery and
point cloud line up in time.

- Bucket listing (S3 API, no key): `https://nrs.objectstore.gov.bc.ca/gdwuts?list-type=2&prefix=093/093l/2019/orthophoto/`
- Example tile: `https://nrs.objectstore.gov.bc.ca/gdwuts/093/093l/2019/orthophoto/bc_093l065_4_2_3_xc150mm_utm09_2019.tif`
  (11208 x 9776 px, 0.15 m, 3-band uint8, JPEG-compressed, tiled, overviews 2-32). Rasterio can read
  just the window it needs over HTTP (`/vsicurl/`), so we never download whole tiles.
- Tile names match the LiDAR tiles: `bc_<maptile>_xc150mm_utm09_2019.tif` vs
  `bc_<maptile>_xyes_8_utm09_2019.laz`. There is no separate ortho index layer, so the pipeline
  derives the ortho URL from the LiDAR index result.
- 2019 orthophoto sheets in 093L: 036-038, 046-048, 056-058, 065, 066, 074, 075, 084, 085, 094, 095.
  2021 adds 039, 040, 048-050, 058, 059 (valley floor around Smithers / Telkwa / Quick).
  Coverage follows the floodplain project, so properties well up the valley sides may have none.
- Older province-wide imagery: `https://openmaps.gov.bc.ca/imagex/ecw_wms.dll` (WMS) and tile
  index `WHSE_IMAGERY_AND_BASE_MAPS.AIMG_ORTHOPHOTO_TILES_POLY`. For 093L065 that is 1 m (2003)
  and 0.5 m (2006). Usable as a fallback outline, not for current vegetation.
- Licence: LidarBC content is under the
  [Open Government Licence – British Columbia](https://www2.gov.bc.ca/gov/content/data/policy-standards/open-data/open-government-licence-bc),
  which allows copying, adapting and commercial use with attribution
  ("Contains information licensed under the Open Government Licence – British Columbia").

## LiDAR (LidarBC)

- Portal: <https://governmentofbc.maps.arcgis.com/apps/MapSeries/index.html?appid=d06b37979b0c4709b7fcf2a1ed458e03>
- Tile index (ArcGIS feature service, queryable by point, returns the S3 URL):
  `https://services6.arcgis.com/ubm4tcTYICKBpist/arcgis/rest/services/LiDAR_BC_S3_Public/FeatureServer/4`
  (layer 4 = point cloud; 0 = project extents; 1-3 DSM and 5-6 DEM indexes).
- Telkwa: `bc_093l065_4_2_3_xyes_8_utm09_2019.laz`, 2019, 8 pts/m², CGVD2013 heights, classes
  `[1, 2, 7]`. There is **no vegetation or building class**, so tree heights come from
  "first returns minus ground" and buildings are masked out with the footprint layer.
- Tiles are about 1.7 x 1.5 km and 4-285 MB each (not COPC, so a whole tile must be downloaded
  once; the pipeline caches it under `data/raw/lidar/`).
- The same bucket holds 2016 and 2018 point clouds for other 093L sheets.
- Licence: OGL-BC (catalogue entry "LiDAR").

## Parcels (ParcelMap BC)

- WFS: `https://openmaps.gov.bc.ca/geo/pub/wfs`, layer `pub:WHSE_CADASTRE.PMBC_PARCEL_FABRIC_POLY_SVW`
- Fields include PID, plan number, parcel class, owner *type* (Private / Crown), regional district
  and area. **No owner names.** The "Fully Attributed" version is access-only and not used.
- Licence: OGL-BC (catalogue entry "ParcelMap BC Parcel Fabric").
- Caution: the repository is public. Do not commit parcel PIDs paired with addresses, or any
  cached parcel data; `data/` is git-ignored.

## Building footprints

- **Microsoft Global ML Building Footprints**:
  index `https://minedbuildings.z5.web.core.windows.net/global-buildings/dataset-links.csv`,
  one gzipped GeoJSON-lines file per level-9 quadkey (Telkwa = `021201031`, 476 KB, 6088 buildings).
  ODbL; attribute "Microsoft". Footprints are ML output: expect missed outbuildings and
  slightly offset or merged polygons.
- **OpenStreetMap** (Overpass API) as a fallback: ODbL, "© OpenStreetMap contributors". Sparse here.
- No province-wide open BC building footprint layer was found in the BC Data Catalogue.
- Google Open Buildings does not cover Canada.
- If no footprint is found, the pipeline assumes a 12 x 12 m house at the clicked point and says so.

## Sentinel-2 (for later landscape-scale NDVI / NBR)

- **Element84 Earth Search STAC** (no account): `https://earth-search.aws.element84.com/v1`,
  collection `sentinel-2-l2a`, COG assets on AWS. Telkwa is in MGRS tile 9UXA.
- **Copernicus Data Space STAC** (account needed for downloads): `https://stac.dataspace.copernicus.eu/v1`.
- Licence: Copernicus Sentinel data terms (free, open, attribution "Contains modified Copernicus
  Sentinel data <year>").

## Attribution block for outputs

> Contains information licensed under the Open Government Licence – British Columbia
> (LidarBC, ParcelMap BC). Building footprints © Microsoft, ODbL. Based on FireSmart BC guidance;
> not an official FireSmart assessment.
