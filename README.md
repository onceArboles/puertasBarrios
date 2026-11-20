# Puertas de Barrios Privados

Extractor automatizado de coordenadas de puertas de acceso de barrios privados en Argentina utilizando OpenStreetMap (tags explícitos de portones/entradas + intersección geométrica entre perímetros y red vial vehicular).

## Contenido actual (Prueba piloto: Canning)

- `canning_accesos.py`: Script en Python que consulta Overpass API para el corredor de Canning (Ruta 58, Ezeiza / Esteban Echeverría), detecta accesos por ambos métodos y los unifica mediante clustering (45 m).
- `canning_accesos.csv`: 83 accesos detectados en 46 barrios privados (con link directo a Google Maps).
- `canning_accesos.json`: Mismos resultados agrupados por nombre de barrio.
- `canning_accesos.geojson`: Capa geográfica lista para abrir en QGIS o [geojson.io](https://geojson.io).

## Uso

Requiere Python 3.10+ y `shapely`:

```bash
pip install shapely
python3 canning_accesos.py
```

O sin instalar dependencias globalmente con `uv`:

```bash
uv run --with shapely python3 canning_accesos.py
```
