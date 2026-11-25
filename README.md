# Puertas de Barrios Privados

Extractor automatizado de coordenadas de puertas de acceso de barrios privados en Argentina utilizando OpenStreetMap (tags explícitos de portones/entradas + intersección geométrica entre perímetros y red vial vehicular).

## Solución escalable (Toda Argentina): `argentina_accesos.py`

Descarga el extracto diario de Geofabrik (`argentina-latest.osm.pbf`), indexa espacialmente los perímetros con `Shapely STRtree` en dos pasadas de bajo consumo de RAM, detecta accesos por tags explícitos + intersección de calles, unifica carriles de entrada/salida (clustering 45 m) y enriquece cada barrio con su **provincia**, **partido/departamento** y **localidad**.

Última corrida sobre Argentina completa: **2,234 accesos en 929 barrios privados**.

### Archivos generados (`output/`)
- `output/argentina_accesos.csv` — tabla plana con jurisdicción, barrio, rol (`principal` / `alternativo`), coordenadas y link a Google Maps (UTF-8 con BOM para Excel).
- `output/argentina_accesos.json` — agrupado por barrio.
- `output/argentina_accesos.geojson` — capa geográfica de **puntos** (puertas de acceso) lista para QGIS o [geojson.io](https://geojson.io).
- `output/argentina_barrios.geojson` — capa geográfica de **polígonos** (perímetros de barrios privados con superficie y cantidad de accesos).

### Interfaz gráfica interna (`app.py`)
Visor web en Streamlit para buscar por barrio privado, visualizar polígonos y accesos (`principal` vs. `alternativo`) en el mapa, y exportar la selección filtrada a CSV, Excel (`.xlsx`) o GeoJSON:

```bash
uv run --with streamlit --with folium --with streamlit-folium --with pandas --with openpyxl streamlit run app.py
```

### Uso del extractor
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

## Geocodificador por Calle y Altura (`geocoder_engine.py` + `geocoder_server.py`)

Aplicación web y API local para ingresar una dirección (**calle**, **altura** y opcionalmente **localidad/provincia**) y geocodificarla directamente contra el extracto de OpenStreetMap de Geofabrik (`data/argentina-latest.osm.pbf`).

### 1. Construir el índice local SQLite + FTS5
Indexa direcciones exactas (`addr:housenumber`), vías de interpolación (`addr:interpolation`) y red vial con nombre (`highway=*` + `name=*`):

```bash
uv run --with osmium --with shapely python3 geocoder_engine.py
```

Genera `data/argentina_geocoder.db` (si no existe el `.osm.pbf`, lo descarga automáticamente desde Geofabrik).

### 2. Iniciar la aplicación web
```bash
uv run --with fastapi --with uvicorn python3 geocoder_server.py --port 8000
```
Luego abrí el navegador en `http://localhost:8000` (o consultá el endpoint REST `GET /api/geocode?calle=Av.+Corrientes&altura=1234&localidad=CABA`).

