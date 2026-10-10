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
         -> final rasters (COG)           data/04_rasters/
        |
        v
Stage 4  Multi-scale benchmarking
         -> benchmark tables & tract layer   benchmarks/
```

Stages are switched on and off in the `[execution]` block of the config file. Each stage reads only the output of the previous one, so any stage can be re-run alone.

---

## Resolution codes

| Code | Nominal size | Cell size (degrees) | Notes |
|------|--------------|---------------------|-------|
| `030` | 30 m (1") | 1/3600 | Base layer. Shows street-frontage artifacts, because CNEFE points are geocoded to street access points, not footprints. |
| `01C` | 100 m (3") | 1/1200 | Recommended product for most uses. |
| `01K` | 1 km (30") | 1/120 | Coarse product. |

All grids are EPSG:4326 (WGS 84) and share one origin, so every coarser grid is an exact integer aggregation of the 30 m grid.

---

## Inputs

### 1. CNEFE raw address files

One file per state (UF). Raw files are never modified.

| Year | Quality tier | Pattern | Encoding | Delimiter |
|------|--------------|---------|----------|-----------|
| 2022 | Gold standard: 100% georeferenced, full typology | `{uf_id}_{uf_alias}.csv`, e.g. `11_RO.csv` | UTF-8 | `;` |
| 2010 | Silver standard: poorer address base, limited typology | `{uf_id}.txt` | Latin-1 | `;` |
| 2000 / 1991 | Estimates from 2010 addresses plus auxiliary land-use data | planned | n/a | n/a |

### 2. Census geometries (per census year)

Each layer is a GeoPackage with a layer name and a code column, declared in the config.

| Layer | Used for |
|-------|----------|
| Census tracts (*setores censitários*) | Point-in-polygon join, density, tract-level benchmark |
| Districts | Benchmark aggregation |
| Municipalities | Benchmark aggregation |
| States | Benchmark aggregation |

Tract codes encode district, municipality and state, so coarser-level benchmark totals can also be computed by grouping on the tract code. The separate geometry layers are still needed for the zonal statistics of the external products.

### 3. Census demographic table

Tract-level table (GeoPackage or Parquet) with the nominal population column and the nominal household column. These give the ground-truth values `P_T` and `H_T`.

### 4. Raster templates

Blank, pre-initialized, snapped grids at `030`, `01C` and `01K`. They are copied first and then burned into, which guarantees identical grids across states and years.

### 5. External products (benchmark only)

WorldPop, Meta HRSL, GHS-POP and LandScan rasters, clipped or tiled per UF. These are used only by Stage 4, Branch B.

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

The set of layers is **not the same for every census year**. Each year has a catalog in the config (`[rasterization.catalog.<year>]`), made of two parts:

- a **core** set that every vintage must produce, because it needs only the canonical contract, and
- a **year-specific** set that lists only what that vintage's data can truthfully support.

Core layers (all years):

| Layer | SPEC | Filter |
|-------|------|--------|
| `ADDRESS` | `00` | every address point |
| `HOUSEHOLD` | `000` | `is_household = 1`, independent of how each vintage encodes households |
| `POPULATION_RESIDENT` | none | `delta_t` burned for every household point |

2022 adds:

| Layer | SPEC | Filter |
|-------|------|--------|
| `ADDRESS` | `01` to `08` | `cod_especie` = the SPEC number |
| `HOUSEHOLD` | `101` to `104` | `is_household = 1` and `cod_subtipo` = the SPEC number |

For 2010 the year-specific set is still empty. It is defined after profiling the 2010 data, and the 2022 categories are not assumed to exist there.

Rules that keep the years comparable:

- **SPEC meaning is stable across years.** `ADDRESS-08` always means religious establishments. A year without that category has no `ADDRESS-08` file, and a different category never reuses an existing number.
- **A layer a year cannot support is not written.** An all-zero raster would say "none exist", while the truth is "not recorded".
- **Catalogs are written after profiling.** Stage 1 writes a code inventory per UF (field, code, count), and the catalog is authored from it.
- **Startup validation.** Every species and subtype code used by a catalog must exist in the `[codes.<year>]` dictionary, or the run stops.
- **Manifest.** Each year gets `manifest_{year}.csv` in the raster folder with one row per layer, marked produced or skipped, with the reason.

The household definition for 2022 (species 1 and 2) is taken from the legacy rasterize script, which already builds the household total this way. The adapter turns it into `is_household`, so the core layer does not depend on species codes.

After burning, Stage 3 runs consistency checks where the layers exist:

- `HOUSEHOLD-000` equals `ADDRESS-01` plus `ADDRESS-02`, cell by cell.
- The sum of `HOUSEHOLD-101` to `-104` is compared with `HOUSEHOLD-000`. A residual means households with an unknown subtype and is reported.
- The population total is compared with the sum of `P_T` from the microdata (mass conservation).

For 2022, the legacy script `popbr_cnefe2022_rasterize.py` serves as a regression oracle. Its rasters for one UF can be compared cell by cell with the new Stage 3 output before the new path is trusted for the whole country.

### Raster specifications

UInt16 reserves 65535 for NoData, so the largest valid count per cell is 65534. Stage 3 checks the maximum of every count raster after burning and logs a warning if any cell reaches that ceiling (a count that wraps around would be silent corruption). The 1 km grid is the only one where this could plausibly matter.

| | Address and household | Population |
|---|---|---|
| Format | GeoTIFF, internally tiled COG (512 x 512), overviews with `SUM` resampling | same |
| CRS | EPSG:4326 | EPSG:4326 |
| Data type | UInt16 | Float32 |
| Pixel value | Point count (valid range 0 to 65534) | Residents (sum of δ<sub>T</sub> over household points in the cell) |
| NoData | 65535 | -9999 |
| Fill value | 0 | 0.0 |
| Compression | ZSTD, predictor 2 | ZSTD, predictor 3 |

### QML style sidecars

Every published `.tif` ships with a QGIS style sidecar, a `.qml` with the same stem (`output.tif` and `output.qml`). Styles are **not shared across resolutions**. Pixel values scale with cell area, so one color ramp would saturate at 1 km and look empty at 30 m. Each style is therefore defined per variable class and per resolution.

Style templates live in `styles/`, named by class and resolution:

| Template | Applies to | Notes |
|----------|-----------|-------|
| `address_030.qml`, `address_01C.qml`, `address_01K.qml` | `ADDRESS-*` and `HOUSEHOLD-*` count rasters | Ramp breaks tuned to the count range at that cell size |
| `population_030.qml`, `population_01C.qml`, `population_01K.qml` | `POPULATION_RESIDENT` rasters | Ramp breaks tuned to residents per cell at that cell size |

Rules:

- NoData (65535 for counts, -9999 for population) is always transparent.
- Zero is transparent or the lowest class, so empty cells do not dominate the map.
- Species and subtype layers (for example `ADDRESS-04` education) are sparse and have far lower maxima than the totals. They use the same class template for now. If they turn out too faint, they can get a dedicated template later.
- The post-processing mask script picks the template from the raster's variable class and resolution code, parsed from the file name, and copies it as the sidecar. It also sets pixels outside Brazil to NoData.

---

## Intermediate files and sidecars

These are not the published product, but they are first-class outputs. Every one of them can be opened directly in QGIS to check what the pipeline did.

### Format policy: GeoPackage first, Parquet optional

GeoPackage (`.gpkg`) is the default format for every vector intermediate, because it can be inspected visually with no extra steps. Parquet (`.parquet`) is an optional faster copy for heavy national runs. Adapters write the GeoPackage always and the Parquet only if `write_parquet = true` is set in the config. Both carry the same schema.

### Stage 1: standardized addresses

`data/02_standardized/cnefe_{year}_{uf}_standardized.gpkg` (layer `addresses`)

Point layer in EPSG:4326 in the **Minimal Canonical Contract**, which every vintage adapter must meet.

| Field | Type | Nullable | Description |
|-------|------|----------|-------------|
| `id_origem` | string | no | Record ID in the source file |
| geometry | Point | no | WGS 84 location (also kept as `longitude` / `latitude` columns for Parquet) |
| `is_household` | uint8 | no | 1 = residential, 0 = non-residential. This is the core switch for population. |
| `cod_especie` | int | yes | Address species code (1 to 8 in 2022). Drives the `ADDRESS-xx` rasters. |
| `cod_subtipo` | int | yes | Household subtype code (101 to 104 in 2022). Drives the `HOUSEHOLD-1xx` rasters. NULL when the vintage has no typology. |
| `tipo_especie` | string | yes | Label for the species code, from the config dictionary |
| `subtipo_domicilio` | string | yes | Label for the subtype code, from the config dictionary |
| `ano_censo` | uint16 | no | 2010 or 2022 |
| `sigla_uf` | string | no | State code |

Stages 2 to 4 need only the geometry and `is_household`. Everything else is optional.

Next to each standardized file the adapter writes a **code inventory** (`cnefe_{year}_{uf}_code_inventory.csv`) with the distinct codes of every classification field and their counts. It is the profiling step that the per-year layer catalog is written from.

Codes are the source of truth, labels are derived. The adapter looks every code up in the `[codes.<year>]` dictionary of the config. Any code found in the raw data but missing from the dictionary is logged and counted in the run report, and is never silently dropped.

### Stage 2: enriched addresses

`data/03_enriched/cnefe_{year}_{uf}_enriched.gpkg` (layer `addresses`)

The standardized fields plus:

| Field | Description |
|-------|-------------|
| `cd_setor` | Tract code assigned by point-in-polygon, not taken from the raw sector code |
| `h_ti` | Physical household point count in the tract, H<sub>T,i</sub> (sum of `is_household` over points in the tract) |
| `delta_t` | Mean residential occupancy density of the tract, δ<sub>T</sub> = P<sub>T</sub> / H<sub>T,i</sub> (residents per household point). NULL where H<sub>T,i</sub> = 0. |
| `orphan` | 1 if the point falls outside every tract and could not be assigned |

Every point carries its tract's δ<sub>T</sub>. The weight burned for a point is `is_household * delta_t`, computed at burn time in Stage 3, so it is not stored as a separate column. Styling this layer in QGIS by `delta_t` is the quickest sanity check of the allocation.

### Stage 2: orphan balance table

`data/03_enriched/cnefe_{year}_{uf}_balance.gpkg` (layers `tract_balance`, `orphan_points`)

Records where mass conservation needs explicit handling.

| Case | Handling |
|------|----------|
| Tract with `P_T > 0` and no household points | Mass logged in `tract_balance`. With `orphan_policy = centroid_fallback` it is also burned at the tract centroid. |
| Points outside every tract | Listed in `orphan_points` and excluded from δ<sub>T</sub> allocation |

The conservation check is: the sum over household points of δ<sub>T</sub>, plus unallocated mass, equals P for the state.

### Stage 3: sidecars

Per raster: the `.qml` style (see above) and the COG overviews. No separate metadata files.

### Stage 4: benchmark dataset

Written to `benchmarks/`.

`benchmark_{year}_{uf}_tracts.gpkg` is the **master benchmark layer**. It has one polygon per census tract, so the errors can be mapped and inspected in QGIS. It holds:

| Column group | Columns |
|--------------|---------|
| IDs | tract, district, municipality and state codes |
| Ground truth | `P_T`, `H_T` |
| This product | zonal sums from the 30 m, 100 m and 1 km population rasters |
| External products | zonal sums from WorldPop, HRSL, GHS-POP, LandScan |
| Errors | absolute and percentage error per product |

Tables per scale (tract, district, municipality, state) are written as CSV and Parquet:

`benchmark_{year}_{uf}_{scale}.csv`

with the same column groups aggregated to that scale.

`benchmark_{year}_{uf}_metrics.csv` gives bias, MAE, RMSE and MAPE per product and scale.

Two branches use these tables:

- **Branch A, validation against ground truth.** BrazilPop zonal sums against official `P_K` at every scale. This measures boundary discretization error, which should shrink toward zero from tract to state.
- **Branch B, inter-product comparison.** External products against `P_K`, each only at scales where its native resolution gives enough pixels per unit (100 m products from district level up, LandScan from municipality level up).

---

## Configuration

All paths, vintages, UFs and stage switches live in one TOML file, which is what ties the scattered data folders together. A documented template is in `config.example.toml`. Blocks: `[paths]`, `[execution]`, `[resolutions]`, `[ingestion.<year>]`, `[enrichment]`, `[rasterization]`, `[benchmarking]`.

`config.toml` is machine-specific (it holds your drive paths) and is not committed. Only `config.example.toml` is.

Each census vintage has its own `[ingestion.<year>]` block pointing to its adapter script, so adding a vintage means adding one adapter and one config block.

---

## How to build

```bash
python run_pipeline.py --config config.toml                  # all enabled stages
python run_pipeline.py --config config.toml --uf RS          # single state
python run_pipeline.py --config config.toml --stage 3        # one stage only
```

Post-processing (optional): `popbr_cnefe2022_mask.py` applies a Brazil mask, with optional pixel buffer, and copies the QML sidecar.

---

## Repository layout

The repository holds **code, configuration and styles only**. All data lives outside it, typically on external or network drives, because the volumes are large. Nothing in the code hard-codes a data location. Every directory is declared in the `[paths]` block of the config file.

```
build-brazilpop/
  config.toml          Machine-specific paths and parameters (not committed)
  config.example.toml  Documented template (committed)
  run_pipeline.py      Orchestrator
  stages/              One script per stage, plus one adapter per census vintage
  styles/              QML templates, one per variable class and resolution
  README.md
```

Data roots, each independently relocatable (they can sit on different drives):

| Config key | Content | Typical size |
|------------|---------|--------------|
| `paths.raw_cnefe` | Raw CNEFE files per UF (read-only) | large |
| `paths.raw_geometries` | Tract, district, municipality, state layers | medium |
| `paths.raw_microdata` | Tract-level demographic tables | small |
| `paths.raw_external` | WorldPop, HRSL, GHS-POP, LandScan rasters | very large |
| `paths.raster_templates` | Blank 030 / 01C / 01K grids | large |
| `paths.standardized` | Stage 1 output | large |
| `paths.enriched` | Stage 2 output and balance tables | large |
| `paths.rasters` | Stage 3 output (final COGs) | large |
| `paths.benchmarks` | Stage 4 output | medium |

Because of this, a run starts by validating the config: every required path must exist and every output location must be writable, before any stage runs. Paths in the rest of this README such as `data/02_standardized/` stand for the corresponding `paths.*` root.