# build-brazilpop

Data pipeline for **BrazilPop**: mass-conserved, multi-resolution gridded address, household and resident-population surfaces for Brazil, built from the IBGE CNEFE address register and census tract microdata.

The method is a dasymetric allocation done in vector space. Each address point is joined to its census tract by point-in-polygon, receives a population weight, and is then burned additively into pre-built grids. No polygon-to-raster step is involved, so there is no boundary leakage at the allocation stage.

---

## Pipeline at a glance

```
Raw CNEFE (2010 / 2022) + IBGE tracts & tract microdata
        |
        v
Stage 1  Ingestion (one adapter per census vintage)
         -> standardized addresses        data/02_standardized/
        |
        v
Stage 2  Vector enrichment & weight allocation (point-in-polygon)
         -> enriched addresses            data/03_enriched/
         -> orphan balance table
        |
        v
Stage 3  Direct additive rasterization (30 m, 100 m, 1 km)
         -> final rasters (GeoTIFF)       data/04_rasters/
        |
        v
Stage 4  Multi-scale benchmarking
         -> benchmark tables, metrics & tract layer   benchmarks/
```

Each stage is a script of its own and reads only the output of the previous one, so any stage can be re-run alone. Stages 1 to 4 are implemented; an orchestrator that chains them is planned.

---

## Resolution codes

| Code | Nominal size | Notes |
|------|--------------|-------|
| `030` | 30 m | Base layer. Shows street-frontage artifacts, because CNEFE points are geocoded to street access points, not footprints. |
| `01C` | 100 m | Recommended product for most uses. |
| `01K` | 1 km | Coarse product. |

The code is only a name. **The grid of each resolution is defined by the raster you provide for it** (extent, pixel size, origin), and must be EPSG:4326 (WGS 84), the CRS of the address points. The three resolutions are independent: each is rasterized on its own from the points, nothing is resampled between them, and they are not compared with each other.

---

## Inputs

### 1. CNEFE raw address files

One file per federal unit ("UF" or "unidade"). Raw files are never modified.

Where the files live and how they are named is up to you. Each census year declares the full path of one UF's file as `input` in `[ingestion.<year>]`, using the optional placeholders `{uf}` (acronym) and `{uf_id}` (IBGE code). The pipeline does not require any folder layout or file naming.

| Year | Quality tier | Example naming (yours may differ) | Format |
|------|--------------|-----------------------------------|--------|
| 2022 | Gold standard: 100% georeferenced, full typology | `{uf_id}_{uf}.csv`, e.g. `11_RO.csv` | Delimited text (declare `delimiter` and `encoding`) |
| 2010 | Silver standard: the address register is face-based, so points are placed by a separate harmonization step | `{uf_id}.parquet`, e.g. `53.parquet` | Parquet (nothing else to declare) |
| 2000 / 1991 | Estimates from 2010 addresses plus auxiliary land-use data | planned | n/a |

The format is chosen by the file extension: `.parquet` is read in record batches, anything else as delimited text.

**2010 input.** IBGE's 2010 CNEFE is not point-located: only about 1% of its rows carry coordinates, the rest are placed by street face. The 2010 input to this pipeline is therefore the harmonized table (one Parquet file per UF) in which every row already has `lon`/`lat`. Only its `especie` column is used: 1 domicílio particular, 2 domicílio coletivo, 3 estabelecimento agropecuário, 4 de ensino, 5 de saúde, 6 de outras finalidades, 7 edificação em construção. **2010 has no religious species and no household subtypes**, so the `ADDRESS-08` and `HOUSEHOLD-101` to `-104` layers are not produced for it. Many addresses of one street face share a point, so cell counts at 30 m reflect that placement.

Column names are also yours: each `col_*` key in `[ingestion.<year>]` maps one of your columns to a canonical field. The canonical names (see the Minimal Canonical Contract below) apply only to what the pipeline writes.

### 2. Census geometries (per census year)

Each layer is a GeoPackage with a layer name and a code column, declared in the config together with its full path. The layers may all sit in one multi-layer database, as IBGE's consolidated 2022 GeoPackage does (layer `setores`, code column `setor_code`). Any OGR-readable polygon format works (GeoPackage, Shapefile, GeoJSON, FlatGeobuf, ...), and GeoParquet too. The geometry file is declared with `tract_path`, plus `tract_layer` when the file has several layers. Files stay where they are and keep their names. A path may use `{year}`, `{uf}` and `{uf_id}`.

| Layer                                 | Used for |
|---------------------------------------|----------|
| Census tracts (*setores censitários*) | Point-in-polygon join, density, tract-level benchmark |
| Districts                             | Benchmark aggregation |
| Municipalities                        | Benchmark aggregation |
| Federal Units                         | Benchmark aggregation |

Tract codes encode district, municipality and state, so coarser-level benchmark totals can also be computed by 
grouping on the tract code. The separate geometry layers are still needed for the zonal statistics of the external products.

### 3. Census demographic table

Tract-level table (GeoPackage or Parquet) with the nominal population column and the nominal household column. 
These give the ground-truth values `P_T` and `H_T`.

This table is **national**: one file for the whole country per census year, so its path in `[enrichment.<year>]` has no `{uf}`. Stage 2 selects the UF being processed by the leading digits of the tract code (the UF code).

It may come from any of these, chosen by file extension:

| Format | Extra keys |
|--------|-----------|
| GeoPackage layer or another OGR table | `microdata_layer` (required when the file has several layers) |
| Parquet | none |
| Delimited text (`.csv`, `.txt`, `.tsv`) | `microdata_delimiter` (required), `microdata_encoding` (default `utf-8`), `microdata_decimal` (default `.`) |

**Nothing is guessed.** The join columns are explicit in the config: `tract_code_column` names the code in the geometry and `microdata_code_column` names the code in the table, together with `population_column` and `household_column`. Codes are read as text so a 15-digit code is never altered, and a floating-point code column is rejected. Every named column is checked before reading, and a missing one is reported with the list of available columns. The tracts and the microdata may be different files in different formats, or two layers of one database. For 2022 in IBGE's consolidated database the table is the layer `basico`, the code column is `setor_code`, the population column is `V0001` (total de pessoas) and the household column is `V0002` (total de domicílios). For 2010 the totals are in the layer `sinopse`, not `basico`: the population column is `V014` (pessoas residentes) and the household column is `V001` (domicílios particulares e coletivos). `basico`'s own `V001` and `V002` only cover permanent private households.

### 4. Raster templates

One raster per resolution (`030`, `01C`, `01K`), declared by full path as `template` in `[resolutions.<code>]`. A template only supplies the grid: its extent, pixel size, origin and CRS. It must be a **UInt16** raster (any other data type is rejected at startup). Its contents and NoData value do not matter, because Stage 3 builds each output on a fresh single-band zeroed raster with that grid (counts as UInt16, population as Float32) and inherits nothing else from the template, so a NoData value of 255 or extra bands cannot affect the counts. Using the same template for every state and year guarantees identical grids across them. A template must be UInt16 and EPSG:4326, and points that fall outside its extent are not burned (the consistency checks report the gap).

### 5. External products (benchmark only)

WorldPop, Meta HRSL, GHS-POP and LandScan rasters. Each is declared by full path as `raster` under `[benchmarking.products.<name>]` (the path may use `{year}`). They are national rasters in EPSG:4326, one band, with NoData declared. They are used only by Stage 4, Branch B. LandScan is an *ambient* (daytime-averaged) population, not residents, so part of its difference from the census is structural.

---

## Outputs

### Naming convention

```
{PROVIDER}_V{VERSION}_{VARIABLE}_{VARIANT}-{SPEC}_{RESOLUTION}_{EXTENSION}_{YEAR}.tif
```

Example for 2022 at 30 m:

### Address layers (counts per cell)

| File | Content |
|------|---------|
| `BRAZILPOP_V6_CNEFE_ADDRESS-00_030_BRAZIL_2022.tif` | All address types |
| `..._ADDRESS-01_...` | Species 1: individual household (*domicílio particular*) |
| `..._ADDRESS-02_...` | Species 2: collective household (*domicílio coletivo*) |
| `..._ADDRESS-03_...` | Species 3: agricultural (*estabelecimento agropecuário*) |
| `..._ADDRESS-04_...` | Species 4: education (*estabelecimento de ensino*) |
| `..._ADDRESS-05_...` | Species 5: health (*estabelecimento de saúde*) |
| `..._ADDRESS-06_...` | Species 6: other purposes (*estabelecimento de outras finalidades*) |
| `..._ADDRESS-07_...` | Species 7: under construction or renovation (*edificação em construção ou reforma*) |
| `..._ADDRESS-08_...` | Species 8: religious (*estabelecimento religioso*) |

### Household layers (counts per cell)

| File | Content |
|------|---------|
| `BRAZILPOP_V6_CNEFE_HOUSEHOLD-000_030_BRAZIL_2022.tif` | All households (`ADDRESS-01` + `ADDRESS-02`) |
| `..._HOUSEHOLD-101_...` | House (*casa*) |
| `..._HOUSEHOLD-102_...` | House in vila or condominium |
| `..._HOUSEHOLD-103_...` | Apartment |
| `..._HOUSEHOLD-104_...` | Other |

The species and subtype layers listed here are the 2022 set. Which of them exist for another census year is declared in the config and recorded in that year's manifest (see Layer catalog and SPEC codes below). Layers a vintage cannot support are skipped without failing the pipeline.

### Population layer

| File | Content |
|------|---------|
| `BRAZILPOP_V6_POPULATION_RESIDENT_030_BRAZIL_2022.tif` | Estimated resident population per cell |

### Layer catalog and SPEC codes

The set of layers is **not the same for every census year**. Each year has a catalog in `data/layer_catalog.json` (fixed reference data, not user configuration), made of two parts:

- a **core** set that every vintage must produce, because it needs only the canonical contract, and
- a **year-specific** set that lists only what that vintage's data can truthfully support.

Core layers (all years):

| Layer | SPEC | Filter |
|-------|------|--------|
| `ADDRESS` | `00` | every address point |
| `HOUSEHOLD` | `000` | `is_household = 1`, independent of how each vintage encodes households |
| `POPULATION_RESIDENT` | none | `p_i` burned for every address point (only points with `p_i > 0` are read) |

2022 adds:

| Layer | SPEC | Filter |
|-------|------|--------|
| `ADDRESS` | `01` to `08` | `cod_especie` = the SPEC number |
| `HOUSEHOLD` | `101` to `104` | `is_household = 1` and `cod_subtipo` = the SPEC number |

2010 adds `ADDRESS-01` to `ADDRESS-07` only. It has no religious species (`ADDRESS-08`) and no household subtypes (`HOUSEHOLD-101` to `-104`), so those files are not written for 2010 and the subtype consistency check is skipped. Its species codes 1 to 7 mean the same as in 2022.

Rules that keep the years comparable:

- **SPEC meaning is stable across years.** `ADDRESS-08` always means religious establishments. A year without that category has no `ADDRESS-08` file, and a different category never reuses an existing number.
- **A layer a year cannot support is not written.** An all-zero raster would say "none exist", while the truth is "not recorded".
- **Catalogs are written after profiling.** Stage 1 writes a code inventory per UF (field, code, count), and the catalog is authored from it.
- **Startup validation.** Every species and subtype code used by a catalog must exist in that year's dictionary in `data/cnefe_codes.json`, or the run stops.
- **Manifest.** Each year gets `manifest_{year}.csv` in the raster folder with one row per layer, marked produced or skipped, with the reason.

The household definition for 2022 (species 1 and 2) is taken from the legacy rasterize script, which already builds the household total this way. It is declared once, as `household = true` on those species in `data/cnefe_codes.json`, and nowhere else. The adapter turns it into `is_household`, so the core layer does not depend on species codes.

After burning, Stage 3 runs consistency checks where the layers exist:

- The `ADDRESS-00` total equals the number of points and the `HOUSEHOLD-000` total equals the number of household points, both from the Stage 2 report. A gap means points outside the template extent.
- The `HOUSEHOLD-000` total equals the `ADDRESS-01` plus `ADDRESS-02` totals.
- The sum of `HOUSEHOLD-101` to `-104` is compared with `HOUSEHOLD-000`. A residual means households with an unknown subtype and is reported.
- The population total equals the allocated `P_T` of the Stage 2 report, that is `P_T` minus the unallocated mass (mass conservation).

The checks compare totals within one resolution. They are written to the Stage 3 report and a failed check is flagged, not fatal. Resolutions are never compared with each other.

### Raster specifications

UInt16 reserves 65535 for NoData, so the largest valid count per cell is 65534. Counts are burned directly into the UInt16 raster, as the legacy script did. GDAL's `-add` stops at the type's maximum (it does not wrap), so a cell holding more than 65,534 points reads 65535 (the NoData value) and the excess is lost. Stage 3 reads every count raster once, with NoData switched off, and logs a warning and sets `saturated` in the manifest if any cell reached 65535. The 1 km grid is the only one where this could plausibly matter.

| | Address and household | Population |
|---|---|---|
| Format | Cloud Optimized GeoTIFF, tiled 512 x 512 | same |
| CRS | EPSG:4326 | EPSG:4326 |
| Data type | UInt16 | Float32 |
| Pixel value | Point count (valid range 0 to 65534) | Residents (sum of `p_i` over the points in the cell) |
| NoData | 65535 | -9999 |
| Fill value | 0 | 0.0 |
| Compression | ZSTD, predictor 2 | ZSTD, predictor 3 |

### QML style sidecars

Every published `.tif` ships with a QGIS style sidecar, a `.qml` with the same stem (`output.tif` and `output.qml`). 
Styles are **not shared across resolutions**. Pixel values scale with cell area, so one color ramp would saturate at 1 km and look empty at 30 m. 
Each style is therefore defined per variable class and per resolution.

Style templates live in `/data/styles/`, named by class and resolution:

| Template | Applies to | Notes |
|----------|-----------|-------|
| `address_030.qml`, `address_01C.qml`, `address_01K.qml` | `ADDRESS-*` and `HOUSEHOLD-*` count rasters | Ramp breaks tuned to the count range at that cell size |
| `population_030.qml`, `population_01C.qml`, `population_01K.qml` | `POPULATION_RESIDENT` rasters | Ramp breaks tuned to residents per cell at that cell size |

Rules:

- NoData (65535 for counts, -9999 for population) is always transparent.
- Zero is transparent or the lowest class, so empty cells do not dominate the map.
- Species and subtype layers (for example `ADDRESS-04` education) are sparse and have far lower maxima than the totals. They use the same class template for now. If they turn out too faint, they can get a dedicated template later.
- Stage 3 picks the template from the raster's variable class and resolution code and copies it as the sidecar. Setting pixels outside Brazil to NoData is a separate post-processing step (the legacy `popbr_cnefe2022_mask.py`), not integrated yet.

---

## Intermediate files and sidecars

These are not the published product, but they are first-class outputs. Every one of them can be opened directly in QGIS to check what the pipeline did.

### Format policy: GeoPackage first, Parquet optional

GeoPackage (`.gpkg`) is the default format for every vector intermediate, because it can be inspected visually with no extra steps. Only GeoPackage is written today. A Parquet copy for heavy national runs may be added later, with the same schema, if it turns out to be needed.

### Stage 1: standardized addresses

`data/02_standardized/cnefe_{year}_{uf}_standardized.gpkg` (layer `addresses`)

Point layer in EPSG:4326 in the **Minimal Canonical Contract**, which every vintage adapter must meet. It holds only what later stages consume. Everything else is added by joins when needed (tract fields in Stage 2, labels from the code dictionary, year and UF from the file name).

| Field | Type | Nullable | Description |
|-------|------|----------|-------------|
| geometry | Point | no | WGS 84 location |
| `is_household` | small integer (0/1) | no | 1 = residential, 0 = non-residential. Derived from `household = true` in `data/cnefe_codes.json`. This is the core switch for population. |
| `cod_especie` | integer | yes | Address species code (1 to 8 in 2022). Drives the `ADDRESS-xx` rasters. NULL when the source value is missing. |
| `cod_subtipo` | integer | yes | Household subtype code (101 to 104 in 2022). Drives the `HOUSEHOLD-1xx` rasters. NULL when the record has no subtype or the vintage has no typology. |

Stages 2 to 4 need only the geometry and `is_household`. The two code fields are needed only for the species and subtype layers.

**Every source row is kept.** One address can carry several records (for example a household and an establishment), so the source address ID is not unique and is not carried. Rows with missing or out-of-range coordinates are the only rows dropped, and they are counted in the run report.

Next to each standardized file the adapter writes a **code inventory** (`cnefe_{year}_{uf}_code_inventory.csv`) with the distinct codes of every classification field, their counts, their label and whether they are declared in the dictionary. It is the profiling step that the per-year layer catalog is written from.

Codes are the source of truth, labels are derived. The adapter checks every code against that year's dictionary in `data/cnefe_codes.json`. Any code found in the raw data but missing from the dictionary is logged and flagged in the inventory, and the run stops instead if `fail_on_unknown_codes = true`. A record with a code that is not in the dictionary is never treated as a household.

### Stage 2: enriched addresses

`data/03_enriched/cnefe_{year}_{uf}_enriched.gpkg` (layer `addresses` by default; set `[enrichment] layer_name`, which may use `{year}`, `{uf}` and `{uf_id}`. Stage 3 reads the same key. The Stage 1 layer name is `[ingestion] layer_name`, same rules.)

The standardized fields plus:

| Field | Description |
|-------|-------------|
| `cd_setor` | Tract code assigned by point-in-polygon, not taken from the raw sector code |
| `h_ti` | Physical household point count in the tract, H<sub>T,i</sub> (sum of `is_household` over points in the tract) |
| `p_i` | Population of the address point *i* (residents). For a household point, the mean residential occupancy density of its tract, δ<sub>T</sub> = P<sub>T</sub> / H<sub>T,i</sub> (residents per household point). For a non-household point, 0. NULL for a household point whose density is undefined (outside every tract, or its tract has no microdata). |
| `orphan` | 1 if the point falls outside every tract and could not be assigned |

**Joins.** Address points are assigned to tracts only by a spatial join (point-in-polygon). No key is ever used to place a point: CNEFE's own sector code is ignored. The tract layer is always transformed to EPSG:4326 before the join, whatever CRS it declares (EPSG:4674 in IBGE's 2022 database), so points and tracts share the pipeline CRS and no transform is applied to the address points.

`p_i` is what Stage 3 burns, so the population raster is a plain sum of `p_i` over the points in each cell. Non-household points stay in the layer (they are still addresses, and the address-count rasters burn from it) and have `p_i = 0`, so the population burn reads only points with `p_i > 0`. The tract density δ<sub>T</sub> itself is available per tract in the balance file (`tract_balance.delta_t`). Styling this layer in QGIS by `p_i` is the quickest sanity check of the allocation.

### Stage 2: orphan balance table

`data/03_enriched/cnefe_{year}_{uf}_balance.gpkg` (layers `tract_balance`, `orphan_points`, and `microdata_unmatched` and `duplicate_tracts` when needed)

Records every case where census population cannot be placed on an address point. **Nothing is redistributed, snapped to the nearest tract or burned at a centroid.** Unplaceable mass is left out and tracked, so the final surfaces never contain population that the address data cannot support.

| Case | Handling |
|------|----------|
| Tract with `P_T > 0` and no household points | Resident population is zero. The tract is listed in `tract_balance` with its unallocated `P_T`. |
| Tract in the geometry layer with no microdata row | `p_i` is NULL for its household points, population is zero. Listed in `tract_balance` with status `no_microdata`. |
| Tract in the microdata with no geometry | Cannot be placed. Its `P_T` is added to the unallocated total of the run report. |
| Point outside every tract | `orphan = 1`, no tract and `p_i` NULL, so no population. Listed in `orphan_points`. |
| Point on a boundary shared by two tracts | Assigned to the tract with the lowest code, deterministically, and counted in the run report. |
| Tract code stored as several polygons (islands, river or lake fragments) | The parts are **merged under their code** into one multi-part geometry, so the tract stays one tract with one microdata row. The codes and their number of parts are listed in the `duplicate_tracts` layer and counted in the run report. |

The conservation identity is: the sum over household points of δ<sub>T</sub>, plus the unallocated mass, equals `P` for the UF. The run report states the unallocated mass and its share of `P`, so the gap is always visible.

**Layers**

| Layer | Content |
|-------|---------|
| `tract_balance` | One polygon per tract (EPSG:4326): `tract_code`, `p_t`, `h_t` (census households), `h_ti` (household points inside the polygon), `h_diff` (`h_ti - h_t`), `delta_t` (the tract density δ<sub>T</sub>), `unallocated_p`, `status` |
| `orphan_points` | Points outside every tract |
| `microdata_unmatched` | Microdata rows whose code has no tract polygon (table, no geometry) |
| `duplicate_tracts` | Tract codes that were stored as several polygons and merged: `tract_code`, `parts` (table, no geometry) |

`tract_balance.status`:

| Status | Meaning |
|--------|---------|
| `ok` | `P_T` known and `H_T,i > 0`: `delta_t` defined |
| `no_household_points` | `P_T > 0` and `H_T,i = 0`: `unallocated_p = P_T` |
| `empty` | `P_T = 0` and `H_T,i = 0`: nothing to allocate |
| `no_microdata` | no microdata row for the tract: `delta_t` is NULL |

`h_diff` is expected to be non-zero in many tracts. Points are geocoded to street access, and census households are enumerated by operational sector, so the number of household points inside a polygon is not the census household count. Only the allocation uses the point count (`H_T,i`); `H_T` is kept for diagnosis. Because `delta_t = P_T / H_T,i`, the population of every tract with household points is conserved regardless of `h_diff`.

### Stage 2: run report

`data/03_enriched/cnefe_{year}_{uf}_stage2_report.json` holds point, household, orphan and boundary-point counts, tracts by status, `h_ti` and `h_t` totals, `P` total, allocated and unallocated mass (and its share of `P`), the conservation gap and a `conserved` flag, plus the tract-reading report (source CRS, geometries repaired).

### Stage 3: sidecars

Per raster: the `.qml` style (see above). Overviews are optional and off by default (`build_overviews`): they are display-only, and the `SUM` resampling that counts would need is not supported by GDAL's COG driver (3.8), so only methods such as `AVERAGE` are available. No separate metadata files.

### Stage 3: report and manifest

`{output_dir}/manifest_{year}.csv` has one row per raster (layer, status, total, maximum, whether it saturated, file). `{output_dir}/stage3_report_{year}.json` adds the consistency checks with expected and actual values.

Rasterization is a close port of the legacy script and uses GDAL only. For every resolution and layer: the blank UInt16 template is **copied** to the output name (population: a compressed Float32 raster is created on the same grid), then `gdal_rasterize -add` burns the points of **one UF at a time** straight into it, in a loop (counts burn 1, population burns `p_i`). The log shows every UF (`[ 3/27] PA burning` / `done in 108s`) and a running estimate of the time left. There are no work copies, so disk use is the template size plus a little (a 30 m national count raster stays in the tens of MB; measured: +16 MB per large UF). The raster is written under `.part.tif` and renamed when complete. Stage 3 requires each template to be blank, one band, with NoData 65535 or none. Optional `[rasterization.geotiff_profile] write_cog = true` rewrites each finished raster as a COG. All UFs of a run go into the same rasters, so run every UF you want in one invocation.

### Stage 4: benchmark dataset

Written to `[benchmarking] output_dir`. **The result is for the whole run (all selected UFs together), not one file per UF**; the UF option is mostly for testing.

**Method.** For each scale and each product, the cells of the product's raster are added up per polygon and compared with the census total of the same unit.

- Tracts, districts and municipalities are each summed over their **own polygons** (tracts from `[enrichment.<year>]`, districts and municipalities from `[benchmarking.<year>]`). A **state is the sum of its municipalities**, because the 2022 database has no state layer.
- **A cell belongs to the polygon that contains its centre.** This is applied with GDAL (`gdal_rasterize` burns polygon ids on a grid that is the raster window cell for cell, numpy adds the values per id), so a cell is never split and never counted twice, and the sum over the polygons cannot exceed the raster total. NoData cells are skipped. Windows with no polygon are skipped, and windows run in parallel.
- The census total `P_K` (and `H_K`) of a unit is the sum of the tract microdata over its code prefix: district = first 9 digits, municipality = first 7, state = first 2.
- BrazilPop is read from the Stage 3 population rasters. `stage3_resolution` is used at every scale except those in `resolution_by_scale`; the default configuration samples **tracts at 30 m (a quality check) and the larger units at 100 m**. The resolution used at each scale is written in the metrics.
- A polygon smaller than a cell can contain no cell centre and then gets 0 even though it has population. That is the reason for `min_scale` per product, and the number of such units is reported (`units_without_cells`).

**Columns**, per product `<key>` (the key of `[benchmarking.products.<key>]`): `pi_sum_<key>` (sum of the cell values), `pi_count_<key>` (cells counted), `err_<key>` (`pi_sum - P_K`) and `pct_err_<key>`.

| File | Content |
|------|---------|
| `benchmark_{year}_tracts.gpkg` (layer `tracts`) | **Master layer**: one polygon per tract with `tract_code`, `district_code`, `municipality_code`, `uf_code`, `p_k`, `h_k`, `n_tracts` and every product column, so the errors can be mapped in QGIS |
| `benchmark_{year}_{scale}.csv` | One table per scale (`tract`, `district`, `municipality`, `state`): `code`, `name`, `p_k`, `h_k`, `n_tracts` and every product column |
| `benchmark_{year}_metrics.csv` | One row per scale and product (see below) |
| `benchmark_{year}_report.json` | Inputs, polygon counts, the census total, per raster the total inside the polygons, windows read and skipped, time, and the census units that have no polygon |

**Metrics**, over the units that have a census total, with `e = pi_sum - P_K`:

| Column | Definition |
|--------|------------|
| `units`, `units_with_population` | units compared, and those with `P_K > 0` |
| `bias` | mean of `e` |
| `mae`, `rmse` | mean of `|e|`, square root of the mean of `e²` |
| `mape` | mean of `|e| / P_K` in percent, over units with `P_K > 0` |
| `total_pred`, `total_truth`, `mass_gap`, `ratio` | sums, their difference and quotient |
| `units_without_cells` | units with `P_K > 0` and no cell centre |

A product enters the metrics of a scale from its `min_scale` up (its columns are still written at every scale).

Two branches use these tables:

- **Branch A, validation against ground truth.** BrazilPop against official `P_K` at every scale. This measures boundary discretization error, which shrinks from tract to state.
- **Branch B, inter-product comparison.** External products against `P_K`, each only at scales where its native resolution gives enough pixels per unit (LandScan and WorldPop from municipality level up).

**Adding a benchmark** is one block under `[benchmarking.products.<key>]` (`raster`, `min_scale`, optional `label` and `years`); nothing else changes. Its columns appear in every table, the master layer and the metrics, and in the charts.

**Quick look.** `tools/plot_benchmark.py --config <config> --year <year>` reads the Stage 4 tables and writes two PNGs next to them (`plots/`): a scatter of census against estimate per tract, one panel per product, and bars of MAPE, MAE, RMSE and bias grouped by scale (value axes are logarithmic with ticks in scientific notation, every bar carries its value in plain figures). BrazilPop is drawn in one accent blue and every benchmark in one dark gray (`--accent` changes which product is the accent), so there is no rainbow and any number of benchmarks fits. Because colour does not tell the benchmarks apart, each bar is named under it, with the scale as the group label, and each scatter panel is titled with its product. It needs matplotlib (`requirements.txt`).

**Cost (timed on the whole of Brazil with national rasters of the previous product version, 24 cores, NVMe scratch, 16 workers).** About 6.5 minutes for four products at three polygon scales: 258 s for the 22.6-billion-cell 30 m raster, 24 s for BrazilPop 100 m, 28 s for LandScan, 33 s for WorldPop. Peak memory of the parent process 5.7 GB, about 1 GB per worker.

---

## Configuration

There are two kinds of information, kept apart:

| Where | What | Who edits it |
|-------|------|--------------|
| **Your config file** (a TOML passed with `--config`) | Where your inputs are, which columns they use, where each stage writes, run switches | You, per machine |
| **`data/` in the repository** | Reference data that does not depend on your inputs: `ufs.json` (UF acronym <-> IBGE code), `cnefe_codes.json` (species and subtype dictionaries per census year), `layer_catalog.json` (the raster layers each year produces), `styles/` (QML templates) | The project, under version control |

A documented template of the config is `config.example.toml`. Its blocks are `[project]`, `[execution]`, `[resolutions.<code>]`, `[ingestion]` and `[ingestion.<year>]`, `[enrichment]` and `[enrichment.<year>]`, `[rasterization]`, `[benchmarking]`. The reference data is merged into the configuration when it is loaded; a config that still defines `[ufs]`, `[codes]`, `[rasterization.catalog]`, `[rasterization.styles]` or `[paths]` is rejected with a message that says where each one went.

The config file is always passed explicitly with `--config <path>`, so its name and location are up to you. Your real config holds your drive paths and must not be committed: name it `config.local.toml` (gitignored) or keep it outside the repository. Only `config.example.toml` is committed.

**Every stage has its own explicit `output_dir`**, declared in its own block (`[ingestion]`, `[enrichment]`, `[rasterization]`, `[benchmarking]`). A stage reads the previous stage's `output_dir`, so a location is written once. The output directories can sit on different drives. An `output_dir` may contain `{year}` (for example `/data/{year}/02_standardized`) to keep census years in separate folders; without it, years share the folder and are told apart by the year in every file name.

**The config adapts to your data, not the other way around.** The pipeline never asks you to move, rename or restructure input files or columns:

- Every raw input (CNEFE, tracts, microdata, boundaries, external products, raster templates) is declared by its full path, in the block of the stage that uses it.
- Paths may use `{year}`, `{uf}` (UF acronym) and `{uf_id}` (IBGE UF code), both resolved from `data/ufs.json`. A path with no `{uf}` is a national file.
- Column names are mapped with `col_*` and `*_column` keys. Canonical names exist only on the pipeline's own outputs.
- CNEFE address files are always per UF. The census microdata table is national and is filtered to the UF by tract code prefix.

The code dictionaries in `data/cnefe_codes.json` are the single source of truth for species and subtype codes, labels and household status. Nothing else repeats them.

Each census vintage has its own `[ingestion.<year>]` block pointing to its adapter script, so adding a vintage means adding one adapter, one dictionary in `data/cnefe_codes.json`, one `[enrichment.<year>]` block and, when it has extra layers, one entry in `data/layer_catalog.json`.

---

## How to build

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest tests                              # unit + integration tests

# Stages 1 to 4 are implemented; each runs alone and needs the previous stage's output.
# Stages 3 and 4 need the GDAL command-line tools; run all the UFs you want in one call.
.venv/bin/python stages/stage_01_ingest_manager.py --config config.local.toml --uf RS
.venv/bin/python stages/stage_02_vector_enrichment.py --config config.local.toml --uf RS
.venv/bin/python stages/stage_03_additive_rasterize.py --config config.local.toml --uf RS
.venv/bin/python stages/stage_04_benchmark.py --config config.local.toml --year 2022   # whole run, not per UF

# Planned (not written yet): an orchestrator that runs the stages in order
# python run_pipeline.py --config config.local.toml --uf RS
```

### Tests

`tests/` has two kinds of tests:

- **Unit tests** run on tiny synthetic inputs generated inside the test, so they need no data and always run.
- **Integration tests** run the stages on the real test files declared in `local.test.toml` (relative paths into the gitignored `data/tests/`). They are skipped automatically when that file or its input is absent, for example on a fresh clone. They assert data-independent invariants only (no row lost, inventory accounts for every row, output matches the report), so swapping the test UF does not break them.

Post-processing (not integrated): the legacy `popbr_cnefe2022_mask.py` applies a Brazil mask, with optional pixel buffer.

---

## Repository layout

The repository holds **code, configuration and styles only**. All data lives outside it, typically on external or network drives, because the volumes are large. Nothing in the code hard-codes a data location. Each stage's output directory is its own `output_dir` in the config. Raw inputs are declared by full path where they are used (see Configuration).

```
build-brazilpop/
  config.local.toml    Your machine-specific config, any name works via --config (gitignored)
  local.test.toml      Config pointing at data/tests/ for the test suite (gitignored)
  config.example.toml  Documented template (committed)
  run_pipeline.py      Orchestrator
  stages/              One script per stage; adapters/ holds one thin adapter per census vintage over a shared tabular reader (text or Parquet)
  tests/               Unit tests on small synthetic inputs
  requirements.txt     Python dependencies
  data/ufs.json            UF lookup (reference data)
  data/cnefe_codes.json    Code dictionaries per census year (reference data)
  data/layer_catalog.json  Raster layers per census year (reference data)
  data/styles/             QML templates, {address|population}_{resolution}.qml
  data/tests/              Test data (gitignored)
  README.md
```

Output directories, one per stage and each independently relocatable (they can sit on different drives):

| Config key | Content | Typical size |
|------------|---------|--------------|
| `[ingestion] output_dir` | Stage 1 output | large |
| `[enrichment] output_dir` | Stage 2 output and balance tables | large |
| `[rasterization] output_dir` | Stage 3 output (final COGs, one subfolder per resolution) | large |
| `[benchmarking] output_dir` | Stage 4 output | medium |

Raw inputs (CNEFE, tracts, microdata, raster templates, boundaries, external products) are full paths in `[ingestion.<year>]`, `[enrichment.<year>]` or `[resolutions.<code>]` (and in the benchmarking block once Stage 4 exists), in whatever layout you already have.

Because of this, a run starts by validating the config: every declared input path (expanded for the selected year and UFs) must exist and every output location must be writable, before any stage runs. Paths in the rest of this README such as `data/02_standardized/` stand for the corresponding stage's `output_dir`.