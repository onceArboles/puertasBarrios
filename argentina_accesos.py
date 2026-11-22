#!/usr/bin/env python3
"""
Extractor escalable de puertas de acceso de barrios privados para toda Argentina.

Lee el extracto diario de OpenStreetMap para Argentina desde Geofabrik
(argentina-latest.osm.pbf), indexa espacialmente los perímetros con Shapely STRtree
y combina dos métodos de detección:
  1. Nodos explícitos de portones/barreras/entradas (barrier=gate|lift_gate|..., entrance=*)
     a <= 10 m del perímetro del barrio.
  2. Intersección geométrica entre el perímetro del barrio y la red vial vehicular
     (highway=*), rescatando accesos aunque no tengan un nodo de portón etiquetado.

Además, realiza un spatial join contra los límites administrativos de OSM
(admin_level 4, 6 y 8) para asignar provincia, partido/departamento y localidad,
y unifica carriles de entrada/salida cercanos (< 45 m) mediante clustering.

Uso:
  python3 argentina_accesos.py
  python3 argentina_accesos.py --provincia "Buenos Aires"
  python3 argentina_accesos.py --pbf data/argentina-latest.osm.pbf --out-dir output
"""

import argparse
import csv
import json
import math
import re
import sys
import time
import urllib.request
from collections import defaultdict
from pathlib import Path

import osmium
import shapely.wkb as wkblib
from shapely import STRtree
from shapely.geometry import LineString, Point, mapping

GEOFABRIK_ARGENTINA_URL = "https://download.geofabrik.de/south-america/argentina-latest.osm.pbf"

GATED_NAME_RE = re.compile(
    r"\b(barrio\s+(privado|cerrado)|country|club\s+de\s+campo|chacras|haras|fincas|estancia)\b",
    re.IGNORECASE,
)

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


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distancia en metros entre dos coordenadas WGS84."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def approx_deg_tolerance(meters: float, lat: float) -> float:
    """Conversión aproximada de metros a grados en la latitud dada."""
    return meters / (111320.0 * max(0.2, math.cos(math.radians(lat))))


def is_gated_community(tags) -> bool:
    """Determina si un área OSM corresponde a un barrio privado."""
    if tags.get("residential") == "gated":
        return True
    if tags.get("landuse") == "residential" and tags.get("access") == "private":
        return True
    if tags.get("landuse") == "residential" and GATED_NAME_RE.search(tags.get("name", "")):
        return True
    return False


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


def download_pbf(url: str, dest: Path, force: bool = False) -> None:
    """Descarga el archivo PBF de Geofabrik si no existe localmente."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and not force:
        size_mb = dest.stat().st_size / (1024 * 1024)
        print(f"Usando archivo PBF existente: {dest} ({size_mb:.1f} MB)")
        return

    print(f"Descargando {url} -> {dest} ...")
    tmp = dest.with_suffix(".osm.pbf.part")
    req = urllib.request.Request(url, headers={"User-Agent": "PuertasBarriosArgentina/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp, tmp.open("wb") as out:
        total = int(resp.headers.get("Content-Length", "0") or "0")
        downloaded = 0
        last_pct = -1
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
            downloaded += len(chunk)
            if total > 0:
                pct = int(downloaded * 100 / total)
                if pct >= last_pct + 10:
                    last_pct = pct
                    print(f"  {pct}% ({downloaded / (1024 * 1024):.1f} MB)", flush=True)
    tmp.replace(dest)
    print(f"Descarga completada: {dest}")


class PolygonAndGateHandler(osmium.SimpleHandler):
    """Pasada 1: extrae polígonos de barrios privados, límites administrativos y nodos de portones."""

    def __init__(self) -> None:
        super().__init__()
        self.wkb_factory = osmium.geom.WKBFactory()
        self.barrios: list[dict] = []
        self.admin_areas: dict[str, list[tuple[str, object]]] = {"4": [], "6": [], "8": []}
        self.gate_nodes: list[dict] = []

    def node(self, n) -> None:
        tags = n.tags
        barrier = tags.get("barrier", "")
        has_entrance = "entrance" in tags
        if barrier in GATE_BARRIERS or has_entrance:
            if n.location.valid():
                self.gate_nodes.append(
                    {
                        "id": n.id,
                        "lon": n.location.lon,
                        "lat": n.location.lat,
                        "barrier": barrier,
                        "entrance": tags.get("entrance", ""),
                        "name": tags.get("name", ""),
                    }
                )

    def area(self, a) -> None:
        tags = a.tags
        gated = is_gated_community(tags)
        admin_lvl = tags.get("admin_level", "")
        is_admin = tags.get("boundary") == "administrative" and admin_lvl in self.admin_areas and "name" in tags

        if not gated and not is_admin:
            return

        try:
            wkb = self.wkb_factory.create_multipolygon(a)
            geom = wkblib.loads(wkb, hex=True)
        except Exception:
            return

        if geom.is_empty:
            return
        if not geom.is_valid:
            geom = geom.buffer(0)
            if geom.is_empty:
                return

        if gated:
            orig_id = a.orig_id()
            osm_id = f"way/{orig_id}" if a.from_way() else f"relation/{orig_id}"
            self.barrios.append(
                {
                    "osm_id": osm_id,
                    "name": tags.get("name", f"Sin nombre ({osm_id})"),
                    "geom": geom,
                }
            )

        if is_admin:
            self.admin_areas[admin_lvl].append((tags["name"], geom))


class RoadIntersectionHandler(osmium.SimpleHandler):
    """Pasada 2 (streaming): intersecta vías vehiculares contra el STRtree de perímetros."""

    def __init__(
        self,
        barrio_tree: STRtree,
        barrio_boundaries: list,
        entrance_only_ids: set[int],
    ) -> None:
        super().__init__()
        self.barrio_tree = barrio_tree
        self.barrio_boundaries = barrio_boundaries
        self.entrance_only_ids = entrance_only_ids
        self.road_entrance_ids: set[int] = set()
        self.candidates_by_barrio: dict[int, list[dict]] = defaultdict(list)
        self.roads_processed = 0

    def way(self, w) -> None:
        hw = w.tags.get("highway")
        if hw not in VEHICULAR_HIGHWAYS:
            return
        self.roads_processed += 1

        coords = []
        for n in w.nodes:
            if n.ref in self.entrance_only_ids:
                self.road_entrance_ids.add(n.ref)
            if n.location.valid():
                coords.append((n.location.lon, n.location.lat))

        if len(coords) < 2:
            return

        road_geom = LineString(coords)
        hit_indices = self.barrio_tree.query(road_geom, predicate="intersects")
        for b_idx in hit_indices:
            boundary = self.barrio_boundaries[b_idx]
            inter = boundary.intersection(road_geom)
            for lon, lat in extract_points_from_geom(inter):
                self.candidates_by_barrio[int(b_idx)].append(
                    {
                        "lat": lat,
                        "lon": lon,
                        "source": "interseccion_calle",
                        "name": "",
                        "barrier": "",
                        "highway": hw,
                    }
                )


def cluster_entrances(candidates: list[dict], radius_m: float) -> list[dict]:
    """Agrupa candidatos cercanos (ej. entrada + salida) en una sola coordenada por puerta."""
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

    _HIGHWAY_SCORE = {
        "trunk": 25,
        "trunk_link": 22,
        "primary": 25,
        "primary_link": 22,
        "secondary": 20,
        "secondary_link": 18,
        "tertiary": 15,
        "tertiary_link": 13,
        "unclassified": 10,
        "residential": 10,
        "living_street": 5,
        "service": 5,
        "track": 0,
    }
    _MAIN_NAME_RE = re.compile(r"\b(principal|central|main|acceso\s*1|entrada\s*1)\b", re.IGNORECASE)
    _SEC_NAME_RE = re.compile(
        r"\b(servicio|proveedores|secundari[oa]|obra|emergencia|alternativ[oa]|trasera)\b",
        re.IGNORECASE,
    )

    def _score_entrance(ent: dict) -> tuple:
        score = 0
        name = ent.get("nombre_acceso", "")
        if _MAIN_NAME_RE.search(name):
            score += 100
        if _SEC_NAME_RE.search(name):
            score -= 50
        if "entrance:main" in ent.get("barrier_tag", ""):
            score += 40
        det = ent.get("tipo_deteccion", "")
        if det == "ambos":
            score += 30
        elif det == "tag_explicito":
            score += 20
        hw_scores = [_HIGHWAY_SCORE.get(h, 0) for h in ent.get("calle_tipo", "").split(",") if h]
        if hw_scores:
            score += max(hw_scores)
        return (score, -ent["lat"], ent["lon"])

    if merged:
        best_idx = max(range(len(merged)), key=lambda idx: _score_entrance(merged[idx]))
        for idx, ent in enumerate(merged):
            ent["rol_acceso"] = "principal" if idx == best_idx else "alternativo"

    merged.sort(key=lambda x: (0 if x.get("rol_acceso") == "principal" else 1, -x["lat"], x["lon"]))
    return merged


def build_admin_lookup(admin_areas: dict[str, list[tuple[str, object]]]) -> dict[str, tuple[STRtree, list[str], list]]:
    """Construye índices STRtree para provincias (4), partidos/deptos (6) y localidades (8)."""
    lookup = {}
    for lvl, items in admin_areas.items():
        if not items:
            lookup[lvl] = (None, [], [])
            continue
        names = [name for name, _ in items]
        geoms = [geom for _, geom in items]
        lookup[lvl] = (STRtree(geoms), names, geoms)
    return lookup


def find_admin_name(pt: Point, tree_tuple) -> str:
    """Devuelve el nombre administrativo que contiene al punto dado."""
    tree, names, _ = tree_tuple
    if tree is None:
        return ""
    hits = tree.query(pt, predicate="within")
    if len(hits) == 0:
        return ""
    # Si hay solapamiento menor, devolver el primero ordenado alfabéticamente
    return sorted(names[int(idx)] for idx in hits)[0]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extrae coordenadas de puertas de acceso de barrios privados en toda Argentina."
    )
    parser.add_argument(
        "--pbf",
        type=Path,
        default=Path("data/argentina-latest.osm.pbf"),
        help="Ruta al archivo .osm.pbf (default: data/argentina-latest.osm.pbf)",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("output"),
        help="Directorio de salida (default: output)",
    )
    parser.add_argument(
        "--provincia",
        type=str,
        default="",
        help="Filtrar resultados por provincia (ej. 'Buenos Aires', 'Córdoba')",
    )
    parser.add_argument(
        "--force-download",
        action="store_true",
        help="Forzar descarga del .osm.pbf desde Geofabrik aunque exista localmente",
    )
    parser.add_argument(
        "--gate-tolerance",
        type=float,
        default=10.0,
        help="Tolerancia en metros para asociar portones al perímetro (default: 10.0)",
    )
    parser.add_argument(
        "--cluster-radius",
        type=float,
        default=45.0,
        help="Radio en metros para unificar carriles de una misma puerta (default: 45.0)",
    )
    args = parser.parse_args()

    t0 = time.time()
    download_pbf(GEOFABRIK_ARGENTINA_URL, args.pbf, force=args.force_download)

    print("Pasada 1: extrayendo barrios privados, límites administrativos y portones...")
    p1 = PolygonAndGateHandler()
    p1.apply_file(str(args.pbf), locations=True, idx="flex_mem")
    print(
        f"  Barrios privados: {len(p1.barrios)} | "
        f"Provincias: {len(p1.admin_areas['4'])} | "
        f"Partidos/Deptos: {len(p1.admin_areas['6'])} | "
        f"Localidades: {len(p1.admin_areas['8'])} | "
        f"Nodos portón/entrada: {len(p1.gate_nodes)}"
    )

    if not p1.barrios:
        print("No se encontraron barrios privados.", file=sys.stderr)
        sys.exit(1)

    admin_lookup = build_admin_lookup(p1.admin_areas)

    # Si se filtró por provincia, descartar barrios fuera de esa provincia antes de la Pasada 2
    if args.provincia:
        target_prov = args.provincia.strip().lower()
        filtered_barrios = []
        for b in p1.barrios:
            prov = find_admin_name(b["geom"].representative_point(), admin_lookup["4"])
            if target_prov in prov.lower():
                filtered_barrios.append(b)
        p1.barrios = filtered_barrios
        print(f"  Filtrado por provincia '{args.provincia}': {len(p1.barrios)} barrios.")
        if not p1.barrios:
            sys.exit(0)

    barrio_boundaries = [b["geom"].boundary for b in p1.barrios]
    barrio_tree = STRtree(barrio_boundaries)

    # Pre-asociar portones explícitos al perímetro más cercano usando STRtree
    explicit_candidates_by_barrio: dict[int, list[dict]] = defaultdict(list)
    entrance_only_ids: set[int] = set()
    for gn in p1.gate_nodes:
        tol_deg = approx_deg_tolerance(args.gate_tolerance, gn["lat"])
        pt = Point(gn["lon"], gn["lat"])
        buf = pt.buffer(tol_deg)
        hits = barrio_tree.query(buf, predicate="intersects")
        if len(hits) == 0:
            continue

        is_barrier_gate = gn["barrier"] in GATE_BARRIERS
        if not is_barrier_gate:
            entrance_only_ids.add(gn["id"])

        for b_idx in hits:
            if barrio_boundaries[int(b_idx)].distance(pt) <= tol_deg:
                explicit_candidates_by_barrio[int(b_idx)].append(
                    {
                        "node_id": gn["id"],
                        "is_barrier_gate": is_barrier_gate,
                        "lat": gn["lat"],
                        "lon": gn["lon"],
                        "source": "tag_explicito",
                        "name": gn["name"],
                        "barrier": gn["barrier"] or f"entrance:{gn['entrance']}",
                        "highway": "",
                    }
                )

    print("Pasada 2: intersectando red vial vehicular con perímetros...")
    p2 = RoadIntersectionHandler(barrio_tree, barrio_boundaries, entrance_only_ids)
    p2.apply_file(str(args.pbf), locations=True, idx="flex_mem")
    print(f"  Vías vehiculares procesadas: {p2.roads_processed}")

    # Consolidar por barrio, enriquecer con jurisdicción y deduplicar accesos
    results_by_barrio: dict[str, dict] = {}
    flat_rows: list[dict] = []
    geojson_features: list[dict] = []
    barrio_geojson_features: list[dict] = []

    for b_idx, barrio in enumerate(p1.barrios):
        candidates = list(p2.candidates_by_barrio.get(b_idx, []))
        for ec in explicit_candidates_by_barrio.get(b_idx, []):
            if ec["is_barrier_gate"] or ec["node_id"] in p2.road_entrance_ids:
                candidates.append(ec)

        entrances = cluster_entrances(candidates, radius_m=args.cluster_radius)
        if not entrances:
            continue

        rep_pt = barrio["geom"].representative_point()
        provincia = find_admin_name(rep_pt, admin_lookup["4"])
        partido = find_admin_name(rep_pt, admin_lookup["6"])
        localidad = find_admin_name(rep_pt, admin_lookup["8"])

        # Superficie aproximada en hectáreas usando conversión métrica en la latitud del barrio
        lat_rad = math.radians(rep_pt.y)
        m_per_deg_lat = 111132.92
        m_per_deg_lon = 111320.0 * max(0.2, math.cos(lat_rad))
        superficie_ha = round((barrio["geom"].area * m_per_deg_lat * m_per_deg_lon) / 10000.0, 2)
        osm_url = f"https://www.openstreetmap.org/{barrio['osm_id']}"

        barrio_key = f"{barrio['name']} ({barrio['osm_id']})"
        results_by_barrio[barrio_key] = {
            "barrio": barrio["name"],
            "osm_id": barrio["osm_id"],
            "osm_url": osm_url,
            "provincia": provincia,
            "partido_departamento": partido,
            "localidad": localidad,
            "superficie_ha": superficie_ha,
            "accesos": entrances,
        }

        barrio_geojson_features.append(
            {
                "type": "Feature",
                "geometry": mapping(barrio["geom"]),
                "properties": {
                    "barrio": barrio["name"],
                    "osm_id": barrio["osm_id"],
                    "osm_url": osm_url,
                    "provincia": provincia,
                    "partido_departamento": partido,
                    "localidad": localidad,
                    "cantidad_accesos": len(entrances),
                    "superficie_ha": superficie_ha,
                },
            }
        )

        for idx, ent in enumerate(entrances, start=1):
            rec = {
                "provincia": provincia,
                "partido_departamento": partido,
                "localidad": localidad,
                "barrio": barrio["name"],
                "osm_id": barrio["osm_id"],
                "acceso_num": idx,
                **ent,
            }
            flat_rows.append(rec)
            geojson_features.append(
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [ent["lon"], ent["lat"]]},
                    "properties": rec,
                }
            )

    flat_rows.sort(
        key=lambda r: (
            r["provincia"].lower(),
            r["partido_departamento"].lower(),
            r["barrio"].lower(),
            r["acceso_num"],
        )
    )

    args.out_dir.mkdir(parents=True, exist_ok=True)
    prefix = (
        re.sub(r"[^a-z0-9]+", "_", args.provincia.lower()).strip("_")
        if args.provincia
        else "argentina"
    )
    csv_path = args.out_dir / f"{prefix}_accesos.csv"
    json_path = args.out_dir / f"{prefix}_accesos.json"
    geojson_path = args.out_dir / f"{prefix}_accesos.geojson"
    barrios_geojson_path = args.out_dir / f"{prefix}_barrios.geojson"

    fieldnames = [
        "provincia",
        "partido_departamento",
        "localidad",
        "barrio",
        "osm_id",
        "acceso_num",
        "rol_acceso",
        "lat",
        "lon",
        "nombre_acceso",
        "tipo_deteccion",
        "barrier_tag",
        "calle_tipo",
        "google_maps_url",
    ]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(flat_rows)

    with json_path.open("w", encoding="utf-8") as f:
        json.dump(results_by_barrio, f, ensure_ascii=False, indent=2)

    with geojson_path.open("w", encoding="utf-8") as f:
        json.dump(
            {"type": "FeatureCollection", "features": geojson_features},
            f,
            ensure_ascii=False,
            indent=2,
        )

    with barrios_geojson_path.open("w", encoding="utf-8") as f:
        json.dump(
            {"type": "FeatureCollection", "features": barrio_geojson_features},
            f,
            ensure_ascii=False,
            indent=2,
        )

    elapsed = time.time() - t0
    print(
        f"\nListo en {elapsed:.1f}s! "
        f"Se detectaron {len(flat_rows)} accesos en {len(results_by_barrio)} barrios."
    )
    print(f"  - CSV:             {csv_path.resolve()}")
    print(f"  - JSON:            {json_path.resolve()}")
    print(f"  - GeoJSON accesos: {geojson_path.resolve()}")
    print(f"  - GeoJSON barrios: {barrios_geojson_path.resolve()}")


if __name__ == "__main__":
    main()
