# build-brazilpop

Data pipeline for the BrazilPop dataset


# Goal

# Outputs

The main output consists of a set of raster files of:

1) Address counts
2) Household count
3) Resident population estimates

For the following years of census in Brazil:

 - 2022 - Gold standard, best address database
 - 2010 - Silver standard, address database is poorer
 - 2000 & 1991 - Estimates using auxiliary land use data and address from 2010.

At the nominal (approximate) resolutions:

030 = 30 m cell size.
01C = 100 m cell size.
01K = 1 km cell size.

Format: GeoTiff
High compression

Address and household:
Format: GeoTIFF (.tif)
CRS: WGS 84 (EPSG:4326) 
Data type: UInt16
Pixel values: address point count
Nodata value: 65535 

Population
Format: GeoTIFF (.tif)
CRS: WGS 84 (EPSG:4326) 
Data type: Float32
Pixel values: address point count
Nodata value: -9999 

Raster naming logic:

{PROVIDER}_V{VERSION}_{VARIABLE}_{VARIANT}-{SPEC}_{RESOLUTION}_{EXTENSION}_{YEAR}.tif

Example for 2022 and 30 meters

Address types layers
BRAZILPOP_V6_CNEFE_ADDRESS-00_030_BRAZIL_2022.tif — All address types.
BRAZILPOP_V6_CNEFE_ADDRESS-01_030_BRAZIL_2022.tif — Species 1: Individual household (Domicílio particular)
BRAZILPOP_V6_CNEFE_ADDRESS-02_030_BRAZIL_2022.tif — Species 2: Collective household (Domicílio coletivo)
BRAZILPOP_V6_CNEFE_ADDRESS-03_030_BRAZIL_2022.tif — Species 3: Agricultural address (Estabelecimento agropecuário)
BRAZILPOP_V6_CNEFE_ADDRESS-04_030_BRAZIL_2022.tif — Species 4: Education address (Estabelecimento de ensino)
BRAZILPOP_V6_CNEFE_ADDRESS-05_030_BRAZIL_2022.tif — Species 5: Health address (Estabelecimento de saúde)
BRAZILPOP_V6_CNEFE_ADDRESS-06_030_BRAZIL_2022.tif — Species 6: Other type address (Estabelecimento de outras finalidades)
BRAZILPOP_V6_CNEFE_ADDRESS-07_030_BRAZIL_2022.tif — Species 7: Under construction / renovation (Edificação em construção ou reforma)
BRAZILPOP_V6_CNEFE_ADDRESS-08_030_BRAZIL_2022.tif — Species 8: Religious address (Estabelecimento religioso)

Household subtypes layers
BRAZILPOP_V6_CNEFE_HOUSEHOLD-000_030_BRAZIL_2022.tif — All household subtypes (the raster sum of the ADDRESS-01 and ADDRESS-02 rasters)
BRAZILPOP_V6_CNEFE_HOUSEHOLD-101_030_BRAZIL_2022.tif — Subtype 101: House (Casa)
BRAZILPOP_V6_CNEFE_HOUSEHOLD-102_030_BRAZIL_2022.tif — Subtype 102: House in vila or condominium (Casa de vila ou em condomínio)
BRAZILPOP_V6_CNEFE_HOUSEHOLD-103_030_BRAZIL_2022.tif — Subtype 103: Apartment (Apartamento)
BRAZILPOP_V6_CNEFE_HOUSEHOLD-104_030_BRAZIL_2022.tif — Subtype 104: Other (Outro)

Population layer
BRAZILPOP_V6_POPULATION_RESIDENT_030_BRAZIL_2022.tif — Resident population.

The SPEC follows 

# Inputs

## CNEFE raw dataset for 2022 and 2010

Default split (2022 and 2010) by State (UF)

Tier 0 (raw files)

2022: 
{uf_id}_{uf_alias}.csv
Example: 11_RO.csv
Where uf = Unidade Federal (state)

2010:
{uf_id}.txt
delimiter:

Expected config param:




## Raster templates

These need to be first copied and then used in the burn process

raster_030 = path to 30 m UInt16 tif
raster_01C = path to 100 m UInt16 tif
raster_01K = path to 1 km UInt16 tif

## CENSO datasets layers for each census year:

useful for computing the population estimate

Geometries:

CensusTracts
gpkg file, layer name and code column

Districts
gpkg file, layer name and code column

Municipalities
gpkg file, layer name and code column

States
gpkg file, layer name and code column

Demographic table
gpkg file, layer name, nominal population column, nominal househould column



# How to build