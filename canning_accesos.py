#!/usr/bin/env python3
"""
Extrae las coordenadas de todas las puertas de acceso de los barrios privados
de Canning (Corredor Ruta 58, Ezeiza / Esteban Echeverría, Buenos Aires)
usando OpenStreetMap (Overpass API) + intersección geométrica de calles.

Combina dos métodos:
  1. Nodos explícitos de portones/barreras/entradas (barrier=gate|lift_gate|..., entrance=*)
     sobre o cerca del perímetro del barrio.
  2. Intersección geométrica entre el perímetro del barrio y las calles vehiculares (highway=*),
     lo que detecta accesos aunque nadie haya etiquetado el portón en OSM.

Luego agrupa (clusteriza) detecciones cercanas (< 45 m) dentro de un mismo barrio
para unificar carriles de ingreso/egreso en una sola coordenada por puerta.

Salida:
  - canning_accesos.csv
  - canning_accesos.json (agrupado por nombre de barrio)
  - canning_accesos.geojson (para visualizar en QGIS o geojson.io)

Uso:
  python3 canning_accesos.py
"""

import csv
import json
import math
import re
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

from shapely.geometry import LineString, MultiPolygon, Point, Polygon
from shapely.ops import polygonize

# Bounding box de Canning + Corredor Ruta 58 (sur, oeste, norte, este)
# Cubre ambos lados de Canning (Esteban Echeverría y Ezeiza).
CANNING_BBOX = (-34.935, -58.560, -34.830, -58.465)

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]

# Patrón de respaldo por nombre para polígonos residenciales sin tag residential=gated
GATED_NAME_RE = re.compile(
    r"\b(barrio\s+(privado|cerrado)|country|club\s+de\s+campo|chacras|haras|fincas|estancia)\b",
    re.IGNORECASE,
)

# Tipos de vías vehiculares a considerar para intersección con el perímetro
VEHICULAR_HIGHWAYS = {
    "trunk",
    "trunk_link",
    "primary",
    "primary_link",
    "secondary",
    "secondary_link",
    "tertiary",
    "tertiary_link",
    "unclassified",
    "residential",
    "living_street",
    "service",
    "track",
}

GATE_BARRIERS = {"gate", "lift_gate", "swing_gate", "sally_port", "sliding_gate"}

# Radio máximo (en metros) para asociar un nodo de portón al perímetro del barrio
# (10 m evita absorber portones del barrio de enfrente al otro lado de la calle)
GATE_PERIMETER_TOLERANCE_M = 10.0
# Radio de clustering (en metros) para unificar carriles de entrada/salida de una misma puerta
CLUSTER_RADIUS_M = 45.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distancia en metros entre dos coordenadas WGS84."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def query_overpass(query: str, timeout: int = 45) -> dict:
    """Ejecuta una consulta Overpass con fallback entre servidores públicos."""
    data = urllib.parse.urlencode({"data": query}).encode("utf-8")
    last_err = None
    for url in OVERPASS_ENDPOINTS:
        try:
            req = urllib.request.Request(
                url,
                data=data,
                headers={
                    "User-Agent": "CanningBarriosPrivadosExtractor/1.0",
                    "Accept": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            last_err = exc
            print(f"[aviso] Falló {url}: {exc}", file=sys.stderr)
    raise RuntimeError(f"No se pudo consultar Overpass API: {last_err}")


def fetch_osm_data(bbox: tuple[float, float, float, float]) -> dict:
    """Descarga barrios privados, calles vehiculares y nodos de portones en el bbox."""
    s, w, n, e = bbox
    bb = f"{s},{w},{n},{e}"
    # Sin regex en Overpass (solo búsquedas indexadas): responde en pocos segundos
    query = f"""
    [out:json][timeout:45];
    (
      way["residential"="gated"]({bb});
      relation["residential"="gated"]({bb});
      way["landuse"="residential"]({bb});
      relation["landuse"="residential"]({bb});
      way["highway"]({bb});
      node["barrier"]({bb});
      node["entrance"]({bb});
    );
    out body;
    >;
    out skel qt;
    """
    return query_overpass(query)


def is_gated_community(tags: dict) -> bool:
    """Determina si un elemento OSM corresponde a un barrio privado."""
    if not tags:
        return False
    if tags.get("residential") == "gated":
        return True
    if tags.get("landuse") == "residential" and tags.get("access") == "private":
        return True
    if tags.get("landuse") == "residential" and GATED_NAME_RE.search(tags.get("name", "")):
        return True
    return False


def build_way_coords(way: dict, nodes: dict[int, tuple[float, float]]) -> list[tuple[float, float]]:
    """Convierte un way OSM a lista de coordenadas (lon, lat)."""
    coords = []
    for nid in way.get("nodes", []):
        if nid in nodes:
            coords.append(nodes[nid])
    return coords


def build_polygon_from_relation(
    rel: dict, ways: dict[int, dict], nodes: dict[int, tuple[float, float]]
) -> Polygon | MultiPolygon | None:
    """Reconstruye el polígono exterior de una relación multipolygon."""
    outer_lines = []
    for member in rel.get("members", []):
        if member.get("type") == "way" and member.get("role", "outer") in ("outer", ""):
            w = ways.get(member["ref"])
            if not w:
                continue
            coords = build_way_coords(w, nodes)
            if len(coords) >= 2:
                outer_lines.append(LineString(coords))
    if not outer_lines:
        return None
    polys = list(polygonize(outer_lines))
    if not polys:
        return None
    return polys[0] if len(polys) == 1 else MultiPolygon(polys)


def approx_deg_tolerance(meters: float, lat: float) -> float:
    """Conversión aproximada de metros a grados en la latitud dada."""
    return meters / (111320.0 * math.cos(math.radians(lat)))


def extract_points_from_geom(geom) -> list[tuple[float, float]]:
    """Extrae pares (lon, lat) de cualquier geometría resultado de una intersección."""
    pts = []
    if geom.is_empty:
        return pts
    gtype = geom.geom_type
    if gtype == "Point":
        pts.append((geom.x, geom.y))
    elif gtype == "MultiPoint":
        for p in geom.geoms:
            pts.append((p.x, p.y))
    elif gtype in ("LineString", "LinearRing"):
        c = geom.interpolate(0.5, normalized=True)
        pts.append((c.x, c.y))
    elif gtype == "GeometryCollection":
        for sub in geom.geoms:
            pts.extend(extract_points_from_geom(sub))
    return pts


def cluster_entrances(candidates: list[dict], radius_m: float = CLUSTER_RADIUS_M) -> list[dict]:
    """
    Agrupa candidatos cercanos (ej. barrera de entrada + barrera de salida)
    usando componentes conexas por distancia Haversine.
    """
    n = len(candidates)
    if n == 0:
        return []

    visited = [False] * n
    clusters = []

    for i in range(n):
        if visited[i]:
            continue
        stack = [i]
        visited[i] = True
        comp = []
        while stack:
            curr = stack.pop()
            comp.append(candidates[curr])
            for j in range(n):
                if not visited[j]:
                    d = haversine_m(
                        candidates[curr]["lat"],
                        candidates[curr]["lon"],
                        candidates[j]["lat"],
                        candidates[j]["lon"],
                    )
                    if d <= radius_m:
                        visited[j] = True
                        stack.append(j)
        clusters.append(comp)

    merged = []
    for comp in clusters:
        # Priorizar nodos con tag explícito al elegir la coordenada más precisa
        explicit = [c for c in comp if c["source"] == "tag_explicito"]
        inter = [c for c in comp if c["source"] == "interseccion_calle"]

        if explicit and inter:
            source = "ambos"
            ref_pts = explicit
        elif explicit:
            source = "tag_explicito"
            ref_pts = explicit
        else:
            source = "interseccion_calle"
            ref_pts = inter

        lat = sum(p["lat"] for p in ref_pts) / len(ref_pts)
        lon = sum(p["lon"] for p in ref_pts) / len(ref_pts)

        names = sorted({p["name"] for p in comp if p.get("name")})
        barriers = sorted({p["barrier"] for p in comp if p.get("barrier")})
        highways = sorted({p["highway"] for p in comp if p.get("highway")})

        merged.append(
            {
                "lat": round(lat, 6),
                "lon": round(lon, 6),
                "nombre_acceso": " / ".join(names),
                "tipo_deteccion": source,
                "barrier_tag": ",".join(barriers),
                "calle_tipo": ",".join(highways),
                "google_maps_url": f"https://www.google.com/maps?q={lat:.6f},{lon:.6f}",
            }
        )

    # Ordenar de norte a sur para salida determinista
    merged.sort(key=lambda x: (-x["lat"], x["lon"]))
    return merged


def main() -> None:
    print(f"Consultando OpenStreetMap para Canning (bbox={CANNING_BBOX})...")
    osm = fetch_osm_data(CANNING_BBOX)
    elements = osm.get("elements", [])
    print(f"Elementos descargados: {len(elements)}")

    nodes: dict[int, tuple[float, float]] = {}
    gate_nodes: list[dict] = []
    ways: dict[int, dict] = {}
    relations: list[dict] = []

    for el in elements:
        etype = el["type"]
        if etype == "node":
            nodes[el["id"]] = (el["lon"], el["lat"])
            tags = el.get("tags", {})
            if tags.get("barrier") in GATE_BARRIERS or "entrance" in tags:
                gate_nodes.append(el)
        elif etype == "way":
            ways[el["id"]] = el
        elif etype == "relation":
            relations.append(el)

    # 1) Construir polígonos de barrios privados
    barrios: list[dict] = []
    for wid, w in ways.items():
        tags = w.get("tags", {})
        if not is_gated_community(tags):
            continue
        coords = build_way_coords(w, nodes)
        if len(coords) >= 4 and coords[0] == coords[-1]:
            poly = Polygon(coords)
            if not poly.is_valid:
                poly = poly.buffer(0)
            if not poly.is_empty:
                barrios.append(
                    {
                        "osm_id": f"way/{wid}",
                        "name": tags.get("name", f"Sin nombre (way/{wid})"),
                        "geom": poly,
                    }
                )

    for rel in relations:
        tags = rel.get("tags", {})
        if not is_gated_community(tags):
            continue
        poly = build_polygon_from_relation(rel, ways, nodes)
        if poly is not None and not poly.is_empty:
            if not poly.is_valid:
                poly = poly.buffer(0)
            barrios.append(
                {
                    "osm_id": f"relation/{rel['id']}",
                    "name": tags.get("name", f"Sin nombre (relation/{rel['id']})"),
                    "geom": poly,
                }
            )

    # 2) Construir red vial vehicular
    roads: list[tuple[LineString, str]] = []
    road_node_ids: set[int] = set()
    for w in ways.values():
        tags = w.get("tags", {})
        hw = tags.get("highway")
        if hw in VEHICULAR_HIGHWAYS:
            w_nodes = w.get("nodes", [])
            coords = build_way_coords(w, nodes)
            if len(coords) >= 2:
                roads.append((LineString(coords), hw))
                road_node_ids.update(w_nodes)

    # Filtrar nodos de entrance=* que no sean portones ni estén sobre una calle vehicular
    # (evita capturar puertas peatonales de casas o edificios cercanos al perímetro)
    filtered_gate_nodes = [
        gn
        for gn in gate_nodes
        if gn.get("tags", {}).get("barrier") in GATE_BARRIERS or gn["id"] in road_node_ids
    ]

    print(
        f"Barrios privados encontrados: {len(barrios)} | "
        f"Vías vehiculares: {len(roads)} | "
        f"Nodos de portón/acceso vial: {len(filtered_gate_nodes)}"
    )

    # 3) Detectar accesos para cada barrio
    ref_lat = (CANNING_BBOX[0] + CANNING_BBOX[2]) / 2.0
    tol_deg = approx_deg_tolerance(GATE_PERIMETER_TOLERANCE_M, ref_lat)

    results_by_barrio: dict[str, list[dict]] = defaultdict(list)
    flat_rows: list[dict] = []
    geojson_features: list[dict] = []

    for barrio in sorted(barrios, key=lambda b: b["name"].lower()):
        poly = barrio["geom"]
        boundary = poly.boundary
        minx, miny, maxx, maxy = poly.bounds

        candidates: list[dict] = []

        # Método A: Nodos explícitos de portones/entradas cerca del perímetro
        for gn in filtered_gate_nodes:
            lon, lat = gn["lon"], gn["lat"]
            if not (minx - tol_deg <= lon <= maxx + tol_deg and miny - tol_deg <= lat <= maxy + tol_deg):
                continue
            pt = Point(lon, lat)
            if boundary.distance(pt) <= tol_deg:
                tags = gn.get("tags", {})
                candidates.append(
                    {
                        "lat": lat,
                        "lon": lon,
                        "source": "tag_explicito",
                        "name": tags.get("name", ""),
                        "barrier": tags.get("barrier") or f"entrance:{tags.get('entrance', '')}",
                        "highway": "",
                    }
                )

        # Método B: Intersección del perímetro del barrio con calles vehiculares
        for road_geom, hw in roads:
            rminx, rminy, rmaxx, rmaxy = road_geom.bounds
            if rmaxx < minx or rminx > maxx or rmaxy < miny or rminy > maxy:
                continue
            if boundary.intersects(road_geom):
                inter = boundary.intersection(road_geom)
                for lon, lat in extract_points_from_geom(inter):
                    candidates.append(
                        {
                            "lat": lat,
                            "lon": lon,
                            "source": "interseccion_calle",
                            "name": "",
                            "barrier": "",
                            "highway": hw,
                        }
                    )

        entrances = cluster_entrances(candidates, radius_m=CLUSTER_RADIUS_M)

        for idx, ent in enumerate(entrances, start=1):
            ent_record = {
                "barrio": barrio["name"],
                "osm_id": barrio["osm_id"],
                "acceso_num": idx,
                **ent,
            }
            results_by_barrio[barrio["name"]].append(
                {
                    "acceso_num": idx,
                    "lat": ent["lat"],
                    "lon": ent["lon"],
                    "nombre_acceso": ent["nombre_acceso"],
                    "tipo_deteccion": ent["tipo_deteccion"],
                    "google_maps_url": ent["google_maps_url"],
                }
            )
            flat_rows.append(ent_record)
            geojson_features.append(
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [ent["lon"], ent["lat"]]},
                    "properties": ent_record,
                }
            )

    # 4) Exportar resultados
    out_dir = Path(".")
    csv_path = out_dir / "canning_accesos.csv"
    json_path = out_dir / "canning_accesos.json"
    geojson_path = out_dir / "canning_accesos.geojson"

    fieldnames = [
        "barrio",
        "osm_id",
        "acceso_num",
        "lat",
        "lon",
        "nombre_acceso",
        "tipo_deteccion",
        "barrier_tag",
        "calle_tipo",
        "google_maps_url",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(flat_rows)

    with json_path.open("w", encoding="utf-8") as f:
        json.dump(dict(results_by_barrio), f, ensure_ascii=False, indent=2)

    with geojson_path.open("w", encoding="utf-8") as f:
        json.dump(
            {"type": "FeatureCollection", "features": geojson_features},
            f,
            ensure_ascii=False,
            indent=2,
        )

    print(f"\nListo! Se detectaron {len(flat_rows)} accesos en {len(results_by_barrio)} barrios.")
    print(f"  - CSV:     {csv_path.resolve()}")
    print(f"  - JSON:    {json_path.resolve()}")
    print(f"  - GeoJSON: {geojson_path.resolve()}")


if __name__ == "__main__":
    main()
