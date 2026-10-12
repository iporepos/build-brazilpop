# Project memory: important decisions

Decisions taken during development. Read with the README. Newest at the bottom of each section.

## Working style
- Advance slowly. Discuss a stage and agree decisions before writing it.
- 2010 is **on hold**. Develop and verify 2022 first; the 2010 blocks in the config are stubs.

## Configuration philosophy
- **The config adapts to the user's data, never the reverse.** No imposed folder layout, file naming or column names. Every raw input is declared with its full path in the block that uses it (`[ingestion.<year>]`, `[enrichment.<year>]`, `[benchmarking.*]`). Columns are mapped with `col_*` / `*_column` keys.
- Placeholders in paths: `{year}`, `{uf}`, `{uf_id}`, all optional. UF acronym <-> IBGE code comes from `[ufs]`.
- The config file is always passed with `--config`; its name is irrelevant. `config.local.toml` and `local.test.toml` are gitignored because they hold local paths.
- **Reference data lives in `data/`, not in the user's config** (user decision): `data/ufs.json` (UF lookup), `data/cnefe_codes.json` (species/subtype dictionaries per year), `data/layer_catalog.json` (raster layers per year), `data/styles/` (QML, by convention `{address|population}_{resolution}.qml`). `load_config` merges them into the cfg under the old keys (`ufs`, `codes`, `rasterization.catalog`, `data.styles`), so stage code did not change. A config that defines `[ufs]`, `[codes]`, `[rasterization.catalog]`, `[rasterization.styles]` or `[paths]` is rejected with a pointer. Tests may pass `load_config(..., data_dir=...)`.
- **Every stage declares its own `output_dir`** in its own block (`[ingestion]`, `[enrichment]`, `[rasterization]`, `[benchmarking]`); the shared `[paths]` block was removed. A stage reads the previous stage's `output_dir` (Stage 2 reads `ingestion.output_dir`, Stage 3 reads `enrichment.output_dir`). Stage 3 layout is fixed: `{output_dir}/{year}/{resolution}/`, manifest and report in `{output_dir}/{year}/`.
- Test layout: T0 raw input, T1 Stage 1, T2 Stage 2, T3 Stage 3 (T4 reserved for Stage 4), all under `data/tests/cnefe/2022/`.
- **Config purge (user request):** every key must be read by code. A test (`test_every_key_of_the_example_is_read_somewhere_in_the_code`) fails on a dead key, and another keeps `config.example.toml` and `local.test.toml` structurally identical. Removed: `project.name`, `resolutions.*.nominal`, `rasterization.crs`, `[scripts]`, `execution.run_stage_*`, `execution.write_parquet` (never implemented), `[rasterization.mask]` (legacy mask script is not integrated) and all of the Stage 4 keys except `[benchmarking] output_dir`. When the orchestrator is written it defines its own stage switches. Stage 4's design sketch is kept below so it can be restored when Stage 4 is built.
- **One source of truth per census year**: the `[codes.<year>]` dictionary owns species/subtype codes, labels and `household = true`. No other key repeats it.
- Canonical field names apply to pipeline outputs only, never to inputs.

## Data facts (verified on CNEFE 2022, DF, `53_DF.csv`)
- Real columns: `COD_UNICO_ENDERECO`, `LONGITUDE`, `LATITUDE`, `COD_ESPECIE`, `COD_TIPO_ESPECI`, plus `NV_GEO_COORD` (1 to 6). Delimiter `;`, UTF-8.
- `COD_UNICO_ENDERECO` is **not unique**: one address can carry several records (household plus establishment, buildings with many households). All rows are kept, as the legacy script does.
- `COD_TIPO_ESPECI` (101 to 104) is filled only for species 1 and 2. Species 2 is always 104. Non-households have no subtype.
- Household = species 1 or 2 (confirmed by legacy `popbr_cnefe2022_rasterize.py`).
- IBGE tract codes are 15-digit strings (read them as strings, leading zeros matter). Only CNEFE's own `COD_SETOR` carries a trailing letter.
- Census microdata table is **national** (no `{uf}` in its path); filter to the UF by tract code prefix.

## Census inputs (verified: IBGE_CENSO-2022_BD_BRASIL_2022.gpkg, DF)
- One 5 GB multi-layer GeoPackage holds tracts and microdata (path is also noted in `prompts/prompt.md`). Layers used: `setores` (472,780 MultiPolygon, only `setor_code`, EPSG:4674) and `basico` (attribute table, key `setor_code`). Others: `distritos`, `municipios`, `subdistritos`, `bairros`, many thematic tables, and metadata tables `layers`, `fields`, `variables`.
- `setor_code` is a 15-digit string. DF: 5,418 tracts in both `setores` and `basico`, no duplicates, no invalid geometries.
- `basico`: `V0001` = total pessoas (P_T, DF sum 2,817,381), `V0002` = total domicílios (H_T), `V0003` = particulares, `V0004` = coletivos, `V0007` = particulares ocupados. DF `V0002` sum = 1,172,689 = CNEFE species 1+2 count, `V0003` = species 1, `V0004` = species 2.
- CNEFE's own `COD_SETOR` has a trailing letter (16 chars, e.g. `530010805110090P`) and 198 of 5,171 do not even match a tract once stripped. It is **not used**: tracts come only from point-in-polygon.
- Layers are EPSG:4674 (SIRGAS 2000); the pipeline grid is EPSG:4326.

## Stage 2 decisions
- **No redistribution of mass.** A tract with `P_T > 0` and no household points gets zero resident population and is tracked in `tract_balance` (unallocated mass). The centroid-fallback idea was dropped. `orphan_policy` removed from the config.
- Points outside every tract are orphans: no population, logged in `orphan_points`. No nearest-tract snapping.
- **Points are assigned to tracts by spatial join only.** No key-based placement of points; CNEFE `COD_SETOR` is never used.
- **The tract layer is always transformed to EPSG:4326** (not configurable). Address points are never transformed.
- Boundary points go to the lowest tract code, deterministically, and are counted.
- **The address-level column is `p_i`, population at address *i*** (user decision; was `delta_t`, config key `out_delta_t` is now `out_p_i`): the tract density δ_T for a household point, **0 for a non-household point**, NULL only for a household point with an undefined density (orphan, or tract without microdata). All rows are kept, so the enriched layer stays the single input for every raster. **Stage 3 population burn filters `p_i > 0`** (reads fewer points, same raster). The tract-level density keeps the name `delta_t` in `tract_balance`.
- **Integer fields must stay integers.** `.to_numpy()` on a nullable `Int16` gives float64 and was writing `cod_subtipo` as REAL (101.0). Use `.array`. Tests now assert the stored field types in Stage 1 and 2.
- Two passes per UF (assign and count, then write) because `delta_t` needs the full household count of the tract first. Weight stays `is_household * delta_t`, computed at burn time in Stage 3.

## Stage 2 progress
- Step 1 done: `stages/census_inputs.py` (`load_tracts`, `load_microdata`, `match_report`) and `validate_stage2` in `stages/config.py`. UF rows are selected in SQL by tract code prefix, so the 5 GB database is never fully read (DF loads in about 0.2 s).
- **Multiple input formats, nothing guessed.** Tracts: any OGR format or GeoParquet (`tract_path`, `tract_layer`). Microdata: OGR table/GeoPackage layer, Parquet, or delimited text (`microdata_delimiter` required; encoding and decimal declared). Join columns explicit: `tract_code_column` and `microdata_code_column`. Keys renamed from `tract_gpkg` / `microdata_tract_code`; benchmark boundaries use `*_path` too.
- Microdata is related to tracts by code (relational join, confirmed acceptable). Duplicate or null tract codes are a hard error; invalid tract geometries are repaired and counted.
- DF: 5,418 tracts, all matched with microdata, `P_T` total 2,817,381, 76 tracts with zero population, 47 with zero nominal households.
- **Stage 2 implemented** (`stages/stage_02_vector_enrichment.py`): STRtree point-in-polygon in fid-range chunks, tracts sorted by code (boundary points go to the lowest), relational join of `P_T`, `delta_t = P_T / H_T,i`, enriched layer, balance file (`tract_balance`, `orphan_points`, `microdata_unmatched`) and JSON report. Output is `cd_setor`, `h_ti`, `delta_t`, `orphan` on top of the Stage 1 fields. DF runs in 12 s.
- **DF result:** 0 orphans, 0 boundary points, `h_ti` total = `h_t` total = 1,172,689, but `h_ti != h_t` in 4,421 of 5,418 tracts (sum of |diff| = 5.4% of households; about 2.7% of points sit in a different tract than the census count implies). 8 tracts have `P_T > 0` and no household points: 12,279 people unallocated (0.44% of 2,817,381), 83% of it one prison tract (`tipo_code` 6, 10,163 people in 22 collective households). Mass conserved exactly otherwise (per-tract error 1e-13). Max `delta_t` is 1,126 residents per point. 6,399 household points sit in tracts with `P_T = 0` and burn 0.
- **CNEFE sector code vs spatial tract:** only 88% of points carry a `COD_SETOR` equal to their spatial tract; 85,552 points (6.5%) carry a sector that does not exist in the released tract layer. This confirms the decision to use point-in-polygon and never the code.
- To investigate: the 7 ordinary urban tracts (`tipo_code` 0, 226 to 414 households) with no household points inside the polygon.
- `pyarrow` is needed for Parquet microdata.

## Raster templates and Stage 3 decisions
- **The grid is whatever raster the user provides per resolution** (`[resolutions.<code>].template`, full path). The test templates (`data/tests/raster_templates`, DF) are metre-derived degrees, not 1/3600 etc.: 030 = 0.000269494585, 01C = 0.0009, 01K = 0.0089827935, different origins, not nested. That is accepted: each resolution is rasterized independently, nothing is resampled, no cross-resolution checks. `arcsec`/`res_deg` and `paths.raster_templates` were removed from the config.
- Templates are UInt16 (their file names say UInt32, a misnomer); only the grid is used. **User decision: the template must be UInt16** (Byte, Int16, UInt32, Float32 are rejected at startup, together with a CRS other than EPSG:4326; each template is checked independently). Only the grid is used. `gdal_create -if` would still inherit the template's NoData (a cell holding exactly that count vanished from the statistics) and its band count, so Stage 3 passes `-bands 1` and an explicit unreachable `-a_nodata` (UInt32 max for counts, the population NoData for the Float64 work raster). Tests cover a UInt16 template with NoData 255 and several bands, with a cell holding exactly 255. The Float32 population raster is created on the same grid. A template must be EPSG:4326.
- **GDAL does the burning** (user decision): `gdal_create` (zeroed raster on the template grid, UInt32 for counts, Float64 for population), `gdal_rasterize -add` per UF with `-where` filters, `gdal_translate -of COG` (ZSTD, tiled 512, predictor 2/3). Rasters are built in parallel (`workers`). No GDAL Python bindings are installed; everything is the CLI, and tests read cells through `gdal_translate -of XYZ`.
- Counts clip at 65534 with a warning (`gdal_translate -scale` does NOT clamp, so clipping is done on an ENVI export with numpy). 
- GDAL 3.8.4 has no SUM overview resampling: overviews are optional, off by default.
- `gdalinfo -json` rounds `mean`; exact totals come from the `STATISTICS_MEAN` metadata string.
- Legacy-script regression oracle dropped (user: not needed).
- **DF result (T3):** 45 rasters (15 layers x 3 resolutions) in 10 s; every consistency check passes: ADDRESS-00 = 1,318,887 points, HOUSEHOLD-000 = 1,172,689 = ADDRESS-01 + 02 = sum of 101..104, POPULATION = 2,805,102 (the allocated P_T). The 100 m ADDRESS-00 raster is identical cell by cell to a numpy computation; population matches float64 to 6e-8 relative. Max cell: 599 addresses at 30 m, 920 at 100 m, 12,930 at 1 km; population max 3,586 / 3,586 / 27,456.

## 2010 (Stage 1 done; Stage 2 config ready, not yet run)
- **The 2010 T0 input is a harmonized Parquet, not the raw CNEFE text.** Raw IBGE 2010 is face-based (only 1.3% of rows have lon/lat, some far outside DF). The Parquet (`53.parquet`, 956,344 rows, one per UF) has lon/lat on every row (`metodo`: face 899,760, face_2019/2021, setor_cnefe2022, setor, coordenada 12,077 original GPS). The `53.txt` fixed-width file is not used.
- **User decision: use only the `especie` column (1-7)** and ignore the harmonized `especie_2022` / `tipo_domicilio_2022`. 1 particular, 2 coletivo, 3 agropecuário, 4 ensino, 5 saúde, 6 outras finalidades, 7 em construção. **2010 has no religious species (8) and no household subtypes** (col_subtype is empty, `cod_subtipo` all NULL). `data/cnefe_codes.json` 2010 has species 1-7 and an empty subtype dictionary; `data/layer_catalog.json` 2010 has ADDRESS-01..07 only (no 08, no HOUSEHOLD-1xx). Column `especie` counts (DF): 855,506 / 1,129 / 2,965 / 1,472 / 1,112 / 70,023 / 24,137.
- **Census totals for 2010 are in `sinopse`, not `basico`** (user instruction): `V014` = pessoas residentes (P_T), `V001` = domicílios particulares e coletivos (H_T). `basico_V001/V002` only cover permanent private households. DF: P 2,570,160; H 856,635.
- **Reconciliation (DF) with `especie`:** CNEFE households (species 1+2) = 856,635 = `sinopse V001` exactly, and all 4,358 tracts match one by one (using CNEFE's own `cd_setor`). Species 2 (coletivo) is 1,129 vs census collective households 974.
- Many addresses share one point (47,680 distinct points for 899,760 face-placed rows), so 2010 cell counts at 30 m are lumpy.
- 2010 tract layer `setores` (316,574; CRS84, `setor_code` 15 digits). DF: 4,454 tracts in geometry, 4,358 in `sinopse` (96 tracts have no totals), CNEFE has 4,407 tracts.
- **Adapters are thin**: `stages/adapters/tabular.py` holds the shared logic (text or Parquet by extension); `parse_cnefe_2022.py` / `parse_cnefe_2010.py` only set the year.
- **`output_dir` may contain `{year}`** (to run 2010 beside 2022). Stage 3 layout is now `{output_dir}/{resolution}/` (no year subfolder; the year is in file names and in `output_dir` if wanted). Test config uses `data/tests/cnefe/{year}/T1..T4`.

## Stage 1 (done for 2022)
- Output is a minimal GeoPackage: geometry, `is_household`, `cod_especie`, `cod_subtipo`. No id, labels, year or UF (derivable or joined later).
- Rows dropped only for missing or out-of-range coordinates; counted in the report. Unknown codes are logged and flagged in the code inventory (`fail_on_unknown_codes` makes them fatal). A record with an unknown species is never a household.
- GeoPackage has no unsigned 8-bit type, so `is_household` is stored as int16.
- DF result: 1,318,887 rows, 1,172,689 households, 6.3 s.

## Testing
- Test data lives in the gitignored `data/tests/`: `cnefe/2022/T0` raw input, `cnefe/2022/T1` Stage 1 output, `cnefe/2010/T0` raw 2010.
- Unit tests use synthetic inputs and always run. Integration tests use `local.test.toml` (relative paths from the repo root) and skip when it or its input is absent. They assert data-independent invariants only.
- Test data is never committed (200+ MB; Git history would keep it forever).

## Environment
- Python venv in `.venv` (gitignored), dependencies in `requirements.txt`: pandas 3, geopandas, pyogrio, shapely, pytest.
- GDAL command-line tools are installed on the dev machine; no GDAL Python bindings.

## Stage 4 config sketch (removed from the example until Stage 4 exists)
Keep the keys consistent with the other stages (full input paths, `*_path` / `*_layer`, own `output_dir`):

```toml
[benchmarking]
output_dir           = "/mnt/hdd3/brazilpop/benchmarks"      # Stage 4 writes here; it reads Stage 3 from [rasterization] output_dir
scales = ["tract", "district", "municipality", "state"]
master_layer_pattern = "benchmark_{year}_{uf}_tracts.gpkg"
table_pattern        = "benchmark_{year}_{uf}_{scale}.csv"
metrics_pattern      = "benchmark_{year}_{uf}_metrics.csv"
write_parquet_tables = true

# Boundary layers per census year, in YOUR file naming. Same keys for every year.
[benchmarking.boundaries.2010]
tract_path          = "path/to/file.gpkg"
tract_layer        = "layername"
district_path       = "path/to/file.gpkg"
district_layer     = "layername"
municipality_path   = "path/to/file.gpkg"
municipality_layer = "layername"
federalunits_path   = "path/to/file.gpkg"
federalunits_layer = "layername"

[benchmarking.boundaries.2022]
tract_path          = "path/to/file.gpkg"
tract_layer        = "layername"
district_path       = "path/to/file.gpkg"
district_layer     = "layername"
municipality_path   = "path/to/file.gpkg"
municipality_layer = "layername"
federalunits_path   = "path/to/file.gpkg"
federalunits_layer = "layername"

# Resolution used for each product's zonal statistics, and the minimum scale
# at which it is fairly evaluated (native cell size vs. unit size)
[benchmarking.products.brazilpop]
resolutions = ["030", "01C", "01K"]
min_scale   = "tract"

[benchmarking.products.worldpop]
raster    = "/mnt/hdd2/brazilpop/raw/external/worldpop_{year}_{uf}.tif"
min_scale = "district"

[benchmarking.products.hrsl]
raster    = "/mnt/hdd2/brazilpop/raw/external/meta_hrsl_{uf}.tif"
min_scale = "district"

[benchmarking.products.ghsl]
raster    = "/mnt/hdd2/brazilpop/raw/external/ghsl_pop_{year}_{uf}.tif"
min_scale = "district"

[benchmarking.products.landscan]
raster    = "/mnt/hdd2/brazilpop/raw/external/landscan_{year}_{uf}.tif"
min_scale = "municipality"

[benchmarking.metrics]
bias = true
mae  = true
rmse = true
mape = true
state_mass_gap = true
```

## Stage 4 (planning; nothing implemented yet)
- **Inputs (user, prompt.md):** district and municipality geometries are layers `distritos` and `municipios` of the same 2022 census database used for tracts/microdata. The only external product for now is LandScan: `/media/ipo/Feynman/planalto/ZENODO/BRAZILPOP/V500/T1/ASSETS/BCMK/LANDSCAN_BRAZIL_2022.tif`. WorldPop and GHSL files sit beside it in the same folder (deferred).
- **LandScan facts:** national, 49,955 x 47,372, EPSG:4326, Float32, NoData -9999, pixel 1/1200 degree (3 arcsec, about 93 m), unit = persons per cell (count, not density), **ambient (daytime-averaged) population, not residents** (so differences against the census are partly structural), strip layout (one row per block), DEFLATE, 243 MB.
- 2022 database has **no federal-unit layer**; the state polygon is dissolved from municipalities (state = first 2 digits of the code). Hierarchy by code prefix holds (tract[:9] = district, tract[:7] = municipality). DF has 1 district, 1 municipality, 1 state, so only the tract scale has a distribution in the test data.
- **Decisions (user):** zonal rule = **cell centre inside the polygon**. All scales are computed. **The result is Brazil-wide, not per UF**: the UF option is mainly for testing, so Stage 4 outputs and metrics are for the whole run (all selected UFs together) and the per-UF file names in the old sketch are superseded. LandScan enters the metrics from `min_scale` (README: municipality), set lower in the test config.
- **Zonal statistics tooling:** GDAL 3.8.4 has no zonal-stats command (the unified `gdal` CLI is not installed). QGIS and `qgis_process` are installed here (QGIS has a Zonal statistics algorithm). `gdal_rasterize` burns polygons with the cell-centre rule by default, so zonal sums can be composed from GDAL CLI + numpy (as Stage 3 does). Pending user choice: GDAL composition vs `qgis_process` vs adding rasterio.

### Stage 4: QGIS zonal statistics (user decision: use PyQGIS `native:zonalstatisticsfb`), FINDINGS, rule question OPEN
- User asked: use the QGIS python wrappers, example in prompts/prompt.md, columns `pi_sum_<dataset>` (`pi_sum_brazilpop`, `pi_sum_worldpop`), districts and municipalities from the same 2022 census database, extensible to other benchmark rasters.
- **Environment:** PyQGIS 3.34.4 imports from the SYSTEM Python (`/usr/bin/python3`, numpy 1.26) but NOT from `.venv` (isolated, numpy 2.5). So QGIS code lives in `stages/qgis_zonal.py`, run by a configurable interpreter (`qgis_python`); the rest of Stage 4 stays in the venv. QGIS names fields prefix + statistic (`pi_count`, `pi_sum`), so the runner builds `pi_sum_<name>` / `pi_count_<name>` itself. Output CRS stays that of the input layer (EPSG:4674); QGIS transforms internally.
- **QGIS does NOT apply a pure cell-centre rule.** Probed on DF tracts and synthetic polygons: polygons with several cell centres use the centre rule (identical to gdal_rasterize + numpy on all 5,418 tracts at 30 m); polygons with few centres (about 1 or none; threshold between 1 and 4 centres) are summed by exact overlap area, so a cell's mass can be counted by several polygons. DF, sum |diff| against the pure centre rule: 30 m 0 tracts differ; 100 m 348 tracts (2.6% of total, QGIS total 2,823,023 vs raster 2,805,102, +0.64%); LandScan 93 m 308 tracts (0.75%); 1 km 5,005 of 5,418 tracts differ. The user chose "cell centre" earlier, so this conflicts and is awaiting a decision (options: accept QGIS hybrid and document it; pure centre rule via GDAL composition; QGIS raster-zones algorithm over a gdal_rasterize zone raster).
- Test files: `tests/test_qgis_zonal.py` pins the observed QGIS behaviour. Throwaway probes live in the session scratchpad only.
- **Cost of the composed (GDAL + numpy) zonal sum, MEASURED on the real national data** (24 cores, 62 GB RAM, NVMe; tract scale only, 472,780 polygons, centre rule, one raster, 300 to 1000 row windows, each window = gdal_rasterize of zone ids to an Int32 ENVI raster + gdal_translate window + numpy bincount): national LandScan (2.4 billion cells) 110 s on 1 core, 22 s on 6, 15 s on 12, **13 s on 24**; national BrazilPop 30 m (22.6 billion cells, UInt16, the V500 raster) 17 min extrapolated on 1 core, **124 s on 12 workers, 105 s on 24**, total 203,066,273 residents. Per 50M-cell window: rasterize 1.8 s (about 85%), read 0.2 to 0.3 s, numpy 0.2 s. Memory per worker about 1 GB at 50M cells. Not measured: district and municipality scales, writing the master layer.

## Stage 2: duplicated tract codes (found running 2022 nationally, fixed)
- The 2022 `setores` layer has 472,780 features but 468,099 unique codes: **914 codes are stored as 2 to 247 polygons** (5,595 features), in 22 UFs (AM 178, PA 248, RJ 133, SP 81, ...). `basico` has exactly one row per code, and the parts never overlap (islands, river and lake fragments). The old code raised "tract codes must be unique" and stopped RO, AC were fine, the next UFs failed.
- **Fix (user instruction: "force our way", report in a sidecar):** `load_tracts` merges the parts of one code into one multi-part geometry (`dissolve`), so codes stay unique and the relational join is unchanged. Sidecar: layer `duplicate_tracts` (tract_code, parts) in the balance GeoPackage, plus `duplicate_tract_codes_merged` / `duplicate_tract_features_merged` in the Stage 2 report and a WARNING in the log. Polygons with no code are not selected by the UF filter at all.
- Verified on the failing UFs (output to scratch): AM 178 codes/1,204 features, AP 6/13, TO 2/4, RS 6/12, PA 248/2,169; all conserved mass, AM 41 s, RS 126 s, PA 51 s, peak memory under 1 GB.
- User's real config is `config.local.toml` (outputs under `/home/ipo/Data/outputs/BRAZILPOP/V6/STAGE_0N/{year}`, `layer_name = "ibge_cnefe_addresses"`, `out_tract_code = "setor_code"`, names with `{uf_id}_{uf}`). All 27 UFs of Stage 1 2022 exist; Stage 2 2022 only had RO and AC before the fix and needs a re-run for the others.
- Stage 4 issues are written up in `prompts/stage4_issues.md`.
- **Layer names are configurable in both stages** (user request): `[ingestion] layer_name` (Stage 1 output, Stage 2 input) and new `[enrichment] layer_name` (Stage 2 output, Stage 3 `-l`), default `addresses`, both accept `{year}`, `{uf}`, `{uf_id}` through `stages.config.layer_name`. Balance layer names stay fixed (`tract_balance`, `orphan_points`, `microdata_unmatched`, `duplicate_tracts`). The Stage 2 report has `enriched_layer`. The user's `config.local.toml` was not edited (the default applies).

