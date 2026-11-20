# Puertas de Barrios Privados

Extractor automatizado de coordenadas de puertas de acceso de barrios privados en Argentina utilizando OpenStreetMap (tags explícitos de portones/entradas + intersección geométrica entre perímetros y red vial vehicular).

## Solución escalable (Toda Argentina): `argentina_accesos.py`

Descarga el extracto diario de Geofabrik (`argentina-latest.osm.pbf`), indexa espacialmente los perímetros con `Shapely STRtree` en dos pasadas de bajo consumo de RAM, detecta accesos por tags explícitos + intersección de calles, unifica carriles de entrada/salida (clustering 45 m) y enriquece cada barrio con su **provincia**, **partido/departamento** y **localidad**.

Última corrida sobre Argentina completa: **2,234 accesos en 929 barrios privados**.

### Archivos generados (`output/`)
- `output/argentina_accesos.csv` (374 KB) — tabla plana con jurisdicción, barrio, coordenadas y link a Google Maps.
- `output/argentina_accesos.json` (852 KB) — agrupado por barrio.
- `output/argentina_accesos.geojson` (1.5 MB) — capa geográfica lista para QGIS o [geojson.io](https://geojson.io).

### Uso
Requiere Python 3.10+, `osmium` y `shapely`:

```bash
pip install osmium shapely
python3 argentina_accesos.py
```

O sin instalar dependencias globalmente con `uv`:

```bash
uv run --with osmium --with shapely python3 argentina_accesos.py
```

Opciones útiles:
```bash
# Filtrar por una provincia específica
uv run --with osmium --with shapely python3 argentina_accesos.py --provincia "Buenos Aires"

# Forzar re-descarga del extracto diario de Geofabrik
uv run --with osmium --with shapely python3 argentina_accesos.py --force-download
```

---

## Prototipo inicial (Canning): `canning_accesos.py`

Script liviano que consulta Overpass API para el corredor de Canning (Ruta 58, Ezeiza / Esteban Echeverría) y genera `canning_accesos.csv`, `canning_accesos.json` y `canning_accesos.geojson` (83 accesos en 46 barrios).

```bash
uv run --with shapely python3 canning_accesos.py
```
