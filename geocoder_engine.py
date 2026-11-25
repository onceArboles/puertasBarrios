#!/usr/bin/env python3
"""
Motor de indexación y geocodificación local sobre extractos OpenStreetMap de Geofabrik
(ej. data/argentina-latest.osm.pbf).

Construye una base SQLite + FTS5 con:
  1. Direcciones explícitas (nodos y polígonos/edificios con addr:street + addr:housenumber).
  2. Vías de interpolación OSM (ways con addr:interpolation=*).
  3. Red vial con nombre (highway=* con name=*) enriquecida con límites administrativos
     (provincia, partido/departamento/comuna, localidad/barrio).

Soporta consultas por calle y altura con resolución jerárquica:
  - exacta: coincidencia exacta de calle y altura en OSM
  - interpolada_osm: interpolación sobre un way addr:interpolation de OSM
  - interpolada_calle: interpolación lineal entre alturas vecinas conocidas en la misma calle
  - cercana_calle: altura próxima conocida sobre la misma calle
  - aproximada_calle: coincidencia de traza de calle sin altura cargada en OSM

Uso CLI (construir índice):
  uv run --with osmium --with shapely python3 geocoder_engine.py
  uv run --with osmium --with shapely python3 geocoder_engine.py --provincia "Buenos Aires"
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import sys
import time
import unicodedata
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

GEOFABRIK_ARGENTINA_URL = "https://download.geofabrik.de/south-america/argentina-latest.osm.pbf"
DEFAULT_PBF_PATH = Path(__file__).parent / "data" / "argentina-latest.osm.pbf"
DEFAULT_DB_PATH = Path(__file__).parent / "data" / "argentina_geocoder.db"

HIGHWAY_TYPES = {
    "motorway",
    "trunk",
    "primary",
    "secondary",
    "tertiary",
    "unclassified",
    "residential",
    "living_street",
    "pedestrian",
    "service",
    "road",
    "track",
}

STREET_TYPE_PREFIXES = {
    "avenida",
    "calle",
    "pasaje",
    "bulevar",
    "boulevard",
    "diagonal",
    "autopista",
    "camino",
    "ruta",
    "colectora",
    "peatonal",
    "cortada",
}

ABBREVIATION_MAP = {
    "av": "avenida",
    "avda": "avenida",
    "ave": "avenida",
    "bv": "bulevar",
    "bvd": "bulevar",
    "blvd": "bulevar",
    "pje": "pasaje",
    "psje": "pasaje",
    "diag": "diagonal",
    "aut": "autopista",
    "au": "autopista",
    "cno": "camino",
    "gral": "general",
    "dr": "doctor",
    "dra": "doctora",
    "ing": "ingeniero",
    "cnel": "coronel",
    "tte": "teniente",
    "sarg": "sargento",
    "sgto": "sargento",
    "cmte": "comandante",
    "cte": "comandante",
    "brig": "brigadier",
    "alm": "almirante",
    "alte": "almirante",
    "pres": "presidente",
    "pte": "presidente",
    "gdor": "gobernador",
    "prof": "profesor",
    "sta": "santa",
    "sto": "santo",
    "ntra": "nuestra",
    "sra": "senora",
    "sr": "senor",
    "hno": "hermano",
    "pbro": "presbitero",
    "mons": "monsenor",
    "cap": "capitan",
    "my": "mayor",
    "vte": "vicente",
    "fco": "francisco",
    "j": "j",
    "rn": "ruta nacional",
    "rp": "ruta provincial",
}

JURISDICTION_ALIASES = {
    "caba": "ciudad autonoma de buenos aires",
    "capital federal": "ciudad autonoma de buenos aires",
    "cap fed": "ciudad autonoma de buenos aires",
    "capital": "ciudad autonoma de buenos aires",
    "bs as": "buenos aires",
    "bsas": "buenos aires",
    "pba": "buenos aires",
    "prov de buenos aires": "buenos aires",
    "provincia de buenos aires": "buenos aires",
}

HOUSE_NUM_RE = re.compile(r"(\d+)")
TRAILING_NUM_RE = re.compile(
    r"^(?P<street>.+?)\s+(?:n[°ºo.]*\s*)?(?P<num>\d+(?:\s*(?:bis|ter|pb|[a-z]))?)\s*$",
    re.IGNORECASE,
)


def strip_accents(text: str) -> str:
    """Elimina tildes y diacríticos manteniendo caracteres ASCII base."""
    nfd = unicodedata.normalize("NFD", text)
    return "".join(ch for ch in nfd if unicodedata.category(ch) != "Mn")


def normalize_text(text: str) -> str:
    """Normaliza texto en minúsculas sin tildes ni puntuación superflua."""
    if not text:
        return ""
    s = strip_accents(text).lower()
    s = s.replace("°", " ").replace("º", " ").replace("'", " ").replace("’", " ")
    s = re.sub(r"[.,;/\-_()\[\]]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def normalize_street(street: str) -> tuple[str, str]:
    """
    Devuelve (norm_street, core_street).
    - norm_street: abreviaturas expandidas (ej. 'av corrientes' -> 'avenida corrientes')
    - core_street: sin el tipo de vía inicial (ej. 'corrientes')
    """
    raw_norm = normalize_text(street)
    if not raw_norm:
        return "", ""

    tokens = raw_norm.split()
    expanded: list[str] = []
    for tok in tokens:
        rep = ABBREVIATION_MAP.get(tok, tok)
        expanded.extend(rep.split())

    norm = " ".join(expanded)
    core_tokens = list(expanded)
    while len(core_tokens) > 1 and core_tokens[0] in STREET_TYPE_PREFIXES:
        # No quitar 'calle' si el resto es solo un número, ej. "calle 12" -> mantener "calle 12" como norm
        # pero core puede ser "12" o "calle 12"; mejor conservar 'calle 12' si es numérico puro.
        if core_tokens[0] == "calle" and len(core_tokens) == 2 and core_tokens[1].isdigit():
            break
        core_tokens.pop(0)
    core = " ".join(core_tokens)
    return norm, core


def normalize_jurisdiction(text: str) -> str:
    norm = normalize_text(text)
    return JURISDICTION_ALIASES.get(norm, norm)


def parse_housenumber(raw: str | int | None) -> tuple[str, int | None]:
    """Extrae el texto normalizado y el primer entero de una altura."""
    if raw is None:
        return "", None
    s = str(raw).strip()
    if not s:
        return "", None
    m = HOUSE_NUM_RE.search(s)
    num = int(m.group(1)) if m else None
    return s, num


def parse_freeform_address(query: str) -> dict[str, str]:
    """
    Intenta separar una dirección en formato libre como:
      'Av. Corrientes 1234, CABA'
      'Calle 50 720, La Plata, Buenos Aires'
      '9 de Julio 1500'
    """
    q = (query or "").strip()
    if not q:
        return {"calle": "", "altura": "", "localidad": "", "provincia": ""}

    parts = [p.strip() for p in q.split(",") if p.strip()]
    street_part = parts[0] if parts else q
    localidad = parts[1] if len(parts) >= 2 else ""
    provincia = parts[2] if len(parts) >= 3 else ""

    m = TRAILING_NUM_RE.match(street_part)
    if m:
        calle = m.group("street").strip()
        altura = m.group("num").strip()
    else:
        calle = street_part
        altura = ""

    return {
        "calle": calle,
        "altura": altura,
        "localidad": localidad,
        "provincia": provincia,
    }


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def download_pbf(url: str, dest: Path, force: bool = False) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and not force:
        size_mb = dest.stat().st_size / (1024 * 1024)
        print(f"Usando archivo PBF existente: {dest} ({size_mb:.1f} MB)")
        return

    print(f"Descargando {url} -> {dest} ...")
    tmp = dest.with_suffix(".osm.pbf.part")
    req = urllib.request.Request(url, headers={"User-Agent": "PuertasBarriosGeocoder/1.0"})
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


def _simplify_coords(coords: list[tuple[float, float]], max_pts: int = 12) -> list[list[float]]:
    """Reduce una polilínea de (lon, lat) a [lat, lon] compactos para visualización."""
    if len(coords) <= max_pts:
        return [[round(lat, 6), round(lon, 6)] for lon, lat in coords]
    step = (len(coords) - 1) / (max_pts - 1)
    sampled = [coords[round(i * step)] for i in range(max_pts)]
    return [[round(lat, 6), round(lon, 6)] for lon, lat in sampled]


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        PRAGMA journal_mode = WAL;
        PRAGMA synchronous = NORMAL;
        PRAGMA temp_store = MEMORY;

        DROP TABLE IF EXISTS meta;
        DROP TABLE IF EXISTS addresses;
        DROP TABLE IF EXISTS interpolations;
        DROP TABLE IF EXISTS streets;
        DROP TABLE IF EXISTS streets_fts;
        DROP TABLE IF EXISTS street_names_fts;

        CREATE TABLE meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE addresses (
            id INTEGER PRIMARY KEY,
            street TEXT NOT NULL,
            norm_street TEXT NOT NULL,
            core_street TEXT NOT NULL,
            housenumber TEXT NOT NULL,
            house_num INTEGER,
            lat REAL NOT NULL,
            lon REAL NOT NULL,
            provincia TEXT NOT NULL DEFAULT '',
            partido TEXT NOT NULL DEFAULT '',
            localidad TEXT NOT NULL DEFAULT '',
            postcode TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL DEFAULT '',
            osm_id TEXT NOT NULL,
            source_type TEXT NOT NULL
        );

        CREATE TABLE interpolations (
            id INTEGER PRIMARY KEY,
            street TEXT NOT NULL,
            norm_street TEXT NOT NULL,
            core_street TEXT NOT NULL,
            scheme TEXT NOT NULL,
            num_min INTEGER NOT NULL,
            num_max INTEGER NOT NULL,
            points_json TEXT NOT NULL,
            provincia TEXT NOT NULL DEFAULT '',
            partido TEXT NOT NULL DEFAULT '',
            localidad TEXT NOT NULL DEFAULT '',
            osm_id TEXT NOT NULL
        );

        CREATE TABLE streets (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            norm_name TEXT NOT NULL,
            core_name TEXT NOT NULL,
            highway_type TEXT NOT NULL,
            lat REAL NOT NULL,
            lon REAL NOT NULL,
            segments_json TEXT NOT NULL,
            provincia TEXT NOT NULL DEFAULT '',
            partido TEXT NOT NULL DEFAULT '',
            localidad TEXT NOT NULL DEFAULT '',
            addr_count INTEGER NOT NULL DEFAULT 0,
            addr_min INTEGER,
            addr_max INTEGER
        );

        CREATE VIRTUAL TABLE street_names_fts USING fts5(
            norm_name,
            core_name,
            display_name,
            localidad,
            partido,
            provincia,
            addr_count UNINDEXED,
            has_street_geom UNINDEXED,
            tokenize='unicode61 remove_diacritics 2'
        );
        """
    )


def build_index_from_pbf(
    pbf_path: Path = DEFAULT_PBF_PATH,
    db_path: Path = DEFAULT_DB_PATH,
    provincia_filter: str = "",
    force_download: bool = False,
) -> dict[str, Any]:
    """
    Lee el archivo .osm.pbf de Geofabrik en dos pasadas de bajo consumo de RAM
    y construye el índice SQLite + FTS5 para geocodificación por calle y altura.
    """
    import osmium
    import shapely.wkb as wkblib
    from shapely import STRtree
    from shapely.geometry import Point

    download_pbf(GEOFABRIK_ARGENTINA_URL, pbf_path, force=force_download)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_db = db_path.with_suffix(".db.tmp")
    if tmp_db.exists():
        tmp_db.unlink()

    t0 = time.time()
    print(f"Construyendo índice geocodificador desde {pbf_path} ...")

    class AdminAndAddressHandler(osmium.SimpleHandler):
        """Pasada 1: límites administrativos, nodos con dirección y áreas/edificios con dirección."""

        def __init__(self) -> None:
            super().__init__()
            self.wkb_factory = osmium.geom.WKBFactory()
            self.admin_areas: dict[str, list[tuple[str, Any]]] = {
                "4": [],
                "6": [],
                "8": [],
                "9": [],
                "10": [],
            }
            self.place_areas: list[tuple[str, Any]] = []
            self.raw_addresses: list[tuple] = []
            self.node_addr_map: dict[int, tuple[str, int, str]] = {}

        def node(self, n) -> None:
            tags = n.tags
            hn = tags.get("addr:housenumber")
            st = tags.get("addr:street")
            if not hn or not n.location.valid():
                return

            hn_str, hn_num = parse_housenumber(hn)
            if hn_num is not None:
                self.node_addr_map[n.id] = (hn_str, hn_num, st or "")

            if not st:
                return

            self.raw_addresses.append(
                (
                    st,
                    hn_str,
                    hn_num,
                    round(n.location.lat, 6),
                    round(n.location.lon, 6),
                    tags.get("addr:state", ""),
                    tags.get("addr:district", "") or tags.get("addr:county", ""),
                    tags.get("addr:city", "") or tags.get("addr:suburb", ""),
                    tags.get("addr:postcode", ""),
                    tags.get("name", ""),
                    f"node/{n.id}",
                    "node",
                )
            )

        def area(self, a) -> None:
            tags = a.tags
            admin_lvl = tags.get("admin_level", "")
            is_admin = tags.get("boundary") == "administrative" and admin_lvl in self.admin_areas and "name" in tags
            is_place = tags.get("place") in {"city", "town", "village", "suburb", "neighbourhood"} and "name" in tags
            hn = tags.get("addr:housenumber")
            st = tags.get("addr:street")
            is_addr = bool(hn and st)

            if not is_admin and not is_place and not is_addr:
                return

            if is_addr and not is_admin and not is_place:
                lats: list[float] = []
                lons: list[float] = []
                try:
                    for ring in a.outer_rings():
                        for nr in ring:
                            if nr.location.valid():
                                lats.append(nr.location.lat)
                                lons.append(nr.location.lon)
                        break
                except Exception:
                    return
                if not lats:
                    return
                hn_str, hn_num = parse_housenumber(hn)
                orig_id = a.orig_id()
                osm_id = f"way/{orig_id}" if a.from_way() else f"relation/{orig_id}"
                self.raw_addresses.append(
                    (
                        st,
                        hn_str,
                        hn_num,
                        round(sum(lats) / len(lats), 6),
                        round(sum(lons) / len(lons), 6),
                        tags.get("addr:state", ""),
                        tags.get("addr:district", "") or tags.get("addr:county", ""),
                        tags.get("addr:city", "") or tags.get("addr:suburb", ""),
                        tags.get("addr:postcode", ""),
                        tags.get("name", ""),
                        osm_id,
                        "area",
                    )
                )
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

            if is_admin:
                self.admin_areas[admin_lvl].append((tags["name"], geom))
            if is_place:
                self.place_areas.append((tags["name"], geom))
            if is_addr:
                rep = geom.representative_point()
                hn_str, hn_num = parse_housenumber(hn)
                orig_id = a.orig_id()
                osm_id = f"way/{orig_id}" if a.from_way() else f"relation/{orig_id}"
                self.raw_addresses.append(
                    (
                        st,
                        hn_str,
                        hn_num,
                        round(rep.y, 6),
                        round(rep.x, 6),
                        tags.get("addr:state", ""),
                        tags.get("addr:district", "") or tags.get("addr:county", ""),
                        tags.get("addr:city", "") or tags.get("addr:suburb", ""),
                        tags.get("addr:postcode", ""),
                        tags.get("name", ""),
                        osm_id,
                        "area",
                    )
                )

    print("Pasada 1/2: extrayendo límites administrativos y direcciones explícitas...")
    p1 = AdminAndAddressHandler()
    p1.apply_file(str(pbf_path), locations=True, idx="flex_mem")
    print(
        f"  Admin lvl4={len(p1.admin_areas['4'])}, lvl6={len(p1.admin_areas['6'])}, "
        f"lvl8={len(p1.admin_areas['8'])}, lvl9/10={len(p1.admin_areas['9']) + len(p1.admin_areas['10'])} | "
        f"Direcciones={len(p1.raw_addresses)}"
    )

    # Construir árboles espaciales de jurisdicciones (simplificando levemente lvl4/lvl6 para acelerar spatial join)
    def _make_tree(items: list[tuple[str, Any]], simplify_tol: float = 0.0):
        if not items:
            return None, [], []
        names = [n for n, _ in items]
        geoms = []
        for _, g in items:
            if simplify_tol > 0:
                sg = g.simplify(simplify_tol, preserve_topology=True)
                geoms.append(sg if not sg.is_empty else g)
            else:
                geoms.append(g)
        return STRtree(geoms), names, geoms

    tree_prov = _make_tree(p1.admin_areas["4"], simplify_tol=0.0003)
    tree_partido = _make_tree(p1.admin_areas["6"], simplify_tol=0.0002)
    # Para localidad combinamos lvl8 + lvl10 + lvl9 + places, priorizando el polígono más específico (menor área)
    loc_items = p1.admin_areas["8"] + p1.admin_areas["10"] + p1.admin_areas["9"] + p1.place_areas
    tree_loc = _make_tree(loc_items, simplify_tol=0.0001)

    def lookup_smallest(pt: Point, tree_tuple) -> str:
        tree, names, geoms = tree_tuple
        if tree is None:
            return ""
        hits = tree.query(pt, predicate="within")
        if len(hits) == 0:
            return ""
        if len(hits) == 1:
            return names[int(hits[0])]
        best_idx = min(hits, key=lambda idx: geoms[int(idx)].area)
        return names[int(best_idx)]

    jur_cache: dict[tuple[float, float], tuple[str, str, str]] = {}

    def resolve_jurisdiction(
        lat: float,
        lon: float,
        tag_prov: str,
        tag_part: str,
        tag_loc: str,
        cache_precision: int = 4,
    ) -> tuple[str, str, str]:
        ckey = (round(lat, cache_precision), round(lon, cache_precision))
        cached = jur_cache.get(ckey)
        if cached is None:
            pt = Point(lon, lat)
            c_prov = lookup_smallest(pt, tree_prov)
            c_part = lookup_smallest(pt, tree_partido)
            c_loc = lookup_smallest(pt, tree_loc)
            cached = (c_prov, c_part, c_loc)
            jur_cache[ckey] = cached

        prov = cached[0] or tag_prov
        part = cached[1] or tag_part
        loc = cached[2] or tag_loc
        # En CABA muchas veces no hay lvl8; si loc está vacío y part existe, usar part o CABA
        if not loc and prov == "Ciudad Autónoma de Buenos Aires":
            loc = part or "Ciudad Autónoma de Buenos Aires"
        return prov, part, loc

    class WayHandler(osmium.SimpleHandler):
        """Pasada 2: vías de interpolación addr:interpolation y calles con nombre (highway=*)."""

        def __init__(self, node_addr_map: dict[int, tuple[str, int, str]]) -> None:
            super().__init__()
            self.node_addr_map = node_addr_map
            self.interpolations: list[tuple] = []
            # Pre-agrupamos segmentos de calles por (norm_name, bucket_lat, bucket_lon)
            # para reducir memoria y acelerar el spatial join posterior.
            self.street_cells: dict[tuple[str, float, float], dict[str, Any]] = {}

        def way(self, w) -> None:
            tags = w.tags
            interp = tags.get("addr:interpolation")
            if interp in {"even", "odd", "all", "alphabetic"}:
                pts = []
                street_from_nodes = ""
                nums = []
                for n in w.nodes:
                    if not n.location.valid():
                        continue
                    info = self.node_addr_map.get(n.ref)
                    num_val = info[1] if info else None
                    if info and info[2] and not street_from_nodes:
                        street_from_nodes = info[2]
                    if num_val is not None:
                        nums.append(num_val)
                    pts.append([round(n.location.lat, 6), round(n.location.lon, 6), num_val])

                street = tags.get("addr:street") or street_from_nodes
                if street and len(pts) >= 2 and len(nums) >= 2:
                    self.interpolations.append(
                        (
                            street,
                            interp,
                            min(nums),
                            max(nums),
                            pts,
                            tags.get("addr:state", ""),
                            tags.get("addr:district", "") or tags.get("addr:county", ""),
                            tags.get("addr:city", "") or tags.get("addr:suburb", ""),
                            f"way/{w.id}",
                        )
                    )

            hw = tags.get("highway")
            name = tags.get("name")
            if hw in HIGHWAY_TYPES and name:
                coords = [(n.location.lon, n.location.lat) for n in w.nodes if n.location.valid()]
                if len(coords) >= 2:
                    mid = coords[len(coords) // 2]
                    lat = round(mid[1], 6)
                    lon = round(mid[0], 6)
                    norm_st, core_st = normalize_street(name)
                    if not norm_st:
                        return
                    cell_key = (norm_st, round(lat, 2), round(lon, 2))
                    cell = self.street_cells.get(cell_key)
                    seg_pts = _simplify_coords(coords, max_pts=8)
                    if cell is None:
                        self.street_cells[cell_key] = {
                            "name": name,
                            "norm_name": norm_st,
                            "core_name": core_st,
                            "hw": hw,
                            "lats": [lat],
                            "lons": [lon],
                            "segments": [seg_pts],
                        }
                    else:
                        cell["lats"].append(lat)
                        cell["lons"].append(lon)
                        if len(cell["segments"]) < 20:
                            cell["segments"].append(seg_pts)

    print("Pasada 2/2: extrayendo vías de interpolación y red vial con nombre...")
    p2 = WayHandler(p1.node_addr_map)
    p2.apply_file(str(pbf_path), locations=True, idx="flex_mem")
    p1.node_addr_map.clear()
    print(
        f"  Interpolaciones={len(p2.interpolations)} | Celdas viales con nombre={len(p2.street_cells)}"
    )

    target_prov = normalize_jurisdiction(provincia_filter) if provincia_filter else ""

    conn = sqlite3.connect(str(tmp_db))
    _create_schema(conn)

    # 1. Insertar direcciones explícitas con enriquecimiento espacial
    print("Resolviendo jurisdicciones e indexando direcciones...")
    addr_rows = []
    addr_stats: dict[tuple[str, str, str, str], list[int]] = defaultdict(list)
    street_display_by_key: dict[tuple[str, str, str, str], str] = {}
    street_core_by_norm: dict[str, str] = {}

    for rec in p1.raw_addresses:
        st, hn_str, hn_num, lat, lon, t_prov, t_part, t_loc, pc, name, osm_id, stype = rec
        prov, part, loc = resolve_jurisdiction(lat, lon, t_prov, t_part, t_loc)
        if target_prov and target_prov not in normalize_jurisdiction(prov):
            continue
        norm_st, core_st = normalize_street(st)
        if not norm_st:
            continue
        addr_rows.append(
            (
                st,
                norm_st,
                core_st,
                hn_str,
                hn_num,
                lat,
                lon,
                prov,
                part,
                loc,
                pc,
                name,
                osm_id,
                stype,
            )
        )
        key = (norm_st, prov, part, loc)
        street_display_by_key.setdefault(key, st)
        street_core_by_norm.setdefault(norm_st, core_st)
        if hn_num is not None:
            addr_stats[key].append(hn_num)

    p1.raw_addresses.clear()
    conn.executemany(
        """
        INSERT INTO addresses (
            street, norm_street, core_street, housenumber, house_num,
            lat, lon, provincia, partido, localidad, postcode, name, osm_id, source_type
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        addr_rows,
    )

    # 2. Insertar interpolaciones OSM
    interp_rows = []
    for rec in p2.interpolations:
        st, scheme, nmin, nmax, pts, t_prov, t_part, t_loc, osm_id = rec
        mid_pt = pts[len(pts) // 2]
        prov, part, loc = resolve_jurisdiction(mid_pt[0], mid_pt[1], t_prov, t_part, t_loc)
        if target_prov and target_prov not in normalize_jurisdiction(prov):
            continue
        norm_st, core_st = normalize_street(st)
        if not norm_st:
            continue
        interp_rows.append(
            (
                st,
                norm_st,
                core_st,
                scheme,
                nmin,
                nmax,
                json.dumps(pts, separators=(",", ":")),
                prov,
                part,
                loc,
                osm_id,
            )
        )
        key = (norm_st, prov, part, loc)
        street_display_by_key.setdefault(key, st)
        street_core_by_norm.setdefault(norm_st, core_st)

    p2.interpolations.clear()
    conn.executemany(
        """
        INSERT INTO interpolations (
            street, norm_street, core_street, scheme, num_min, num_max,
            points_json, provincia, partido, localidad, osm_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        interp_rows,
    )

    # 3. Agrupar celdas viales por (norm_name, provincia, partido, localidad)
    print("Agrupando red vial por calle y localidad...")
    grouped_streets: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for cell in p2.street_cells.values():
        c_lat = sum(cell["lats"]) / len(cell["lats"])
        c_lon = sum(cell["lons"]) / len(cell["lons"])
        prov, part, loc = resolve_jurisdiction(c_lat, c_lon, "", "", "", cache_precision=3)
        if target_prov and target_prov not in normalize_jurisdiction(prov):
            continue
        norm_st = cell["norm_name"]
        core_st = cell["core_name"]
        key = (norm_st, prov, part, loc)
        street_display_by_key.setdefault(key, cell["name"])
        street_core_by_norm.setdefault(norm_st, core_st)
        grp = grouped_streets.get(key)
        if grp is None:
            grouped_streets[key] = {
                "name": cell["name"],
                "norm_name": norm_st,
                "core_name": core_st,
                "hw": cell["hw"],
                "lats": cell["lats"],
                "lons": cell["lons"],
                "segments": cell["segments"],
            }
        else:
            grp["lats"].extend(cell["lats"])
            grp["lons"].extend(cell["lons"])
            if len(grp["segments"]) < 25:
                grp["segments"].extend(cell["segments"][: 25 - len(grp["segments"])])

    p2.street_cells.clear()
    jur_cache.clear()

    street_rows = []
    for key, grp in grouped_streets.items():
        norm_st, prov, part, loc = key
        nums = addr_stats.get(key, [])
        c_lat = round(sum(grp["lats"]) / len(grp["lats"]), 6)
        c_lon = round(sum(grp["lons"]) / len(grp["lons"]), 6)
        street_rows.append(
            (
                grp["name"],
                norm_st,
                grp["core_name"],
                grp["hw"],
                c_lat,
                c_lon,
                json.dumps(grp["segments"], separators=(",", ":")),
                prov,
                part,
                loc,
                len(nums),
                min(nums) if nums else None,
                max(nums) if nums else None,
            )
        )

    conn.executemany(
        """
        INSERT INTO streets (
            name, norm_name, core_name, highway_type, lat, lon, segments_json,
            provincia, partido, localidad, addr_count, addr_min, addr_max
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        street_rows,
    )

    # 4. Poblar índice FTS5 unificado de calles + localidades
    print("Creando índices de búsqueda FTS5...")
    fts_rows = []
    for key, disp in street_display_by_key.items():
        norm_st, prov, part, loc = key
        core_st = street_core_by_norm.get(norm_st, norm_st)
        cnt = len(addr_stats.get(key, []))
        has_geom = 1 if key in grouped_streets else 0
        fts_rows.append((norm_st, core_st, disp, loc, part, prov, cnt, has_geom))

    conn.executemany(
        """
        INSERT INTO street_names_fts (
            norm_name, core_name, display_name, localidad, partido, provincia,
            addr_count, has_street_geom
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        fts_rows,
    )

    conn.executescript(
        """
        CREATE INDEX idx_addr_norm_num ON addresses(norm_street, house_num);
        CREATE INDEX idx_addr_core_num ON addresses(core_street, house_num);
        CREATE INDEX idx_addr_prov ON addresses(provincia);

        CREATE INDEX idx_interp_norm ON interpolations(norm_street, num_min, num_max);
        CREATE INDEX idx_interp_core ON interpolations(core_street, num_min, num_max);

        CREATE INDEX idx_streets_norm ON streets(norm_name);
        CREATE INDEX idx_streets_core ON streets(core_name);
        CREATE INDEX idx_streets_prov ON streets(provincia);
        """
    )

    elapsed = round(time.time() - t0, 1)
    provs_count = conn.execute(
        "SELECT COUNT(DISTINCT provincia) FROM streets WHERE provincia != ''"
    ).fetchone()[0]

    meta_items = {
        "pbf_path": str(pbf_path),
        "pbf_size_mb": f"{pbf_path.stat().st_size / (1024 * 1024):.1f}",
        "built_at": datetime.now(timezone.utc).isoformat(),
        "build_seconds": str(elapsed),
        "provincia_filter": provincia_filter or "Argentina completa",
        "total_addresses": str(len(addr_rows)),
        "total_interpolations": str(len(interp_rows)),
        "total_streets": str(len(street_rows)),
        "total_fts_entries": str(len(fts_rows)),
        "provinces_count": str(provs_count),
    }
    conn.executemany("INSERT INTO meta (key, value) VALUES (?, ?)", list(meta_items.items()))
    conn.commit()
    conn.close()

    tmp_db.replace(db_path)
    print(
        f"Índice listo en {elapsed}s -> {db_path} "
        f"({len(addr_rows):,} direcciones, {len(interp_rows):,} interpolaciones, {len(street_rows):,} calles)"
    )
    return meta_items


@dataclass
class GeocodeMatch:
    calle: str
    altura: str
    localidad: str
    partido: str
    provincia: str
    postcode: str
    formatted_address: str
    lat: float
    lon: float
    match_type: str
    match_label: str
    match_detail: str
    confidence: float
    osm_id: str
    osm_url: str
    google_maps_url: str
    street_segments: list[list[list[float]]]
    reference_points: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "calle": self.calle,
            "altura": self.altura,
            "localidad": self.localidad,
            "partido": self.partido,
            "provincia": self.provincia,
            "postcode": self.postcode,
            "formatted_address": self.formatted_address,
            "lat": round(self.lat, 6),
            "lon": round(self.lon, 6),
            "match_type": self.match_type,
            "match_label": self.match_label,
            "match_detail": self.match_detail,
            "confidence": round(self.confidence, 3),
            "osm_id": self.osm_id,
            "osm_url": self.osm_url,
            "google_maps_url": self.google_maps_url,
            "street_segments": self.street_segments,
            "reference_points": self.reference_points,
        }


def _format_address(
    street: str,
    altura: str,
    localidad: str,
    partido: str,
    provincia: str,
    postcode: str = "",
) -> str:
    head = f"{street} {altura}".strip() if altura else street
    parts = [head]
    seen = {head.lower()}
    for p in (localidad, partido, provincia):
        if p and p.lower() not in seen:
            if p == partido and p.lower() in (localidad or "").lower():
                continue
            parts.append(p)
            seen.add(p.lower())
    if postcode:
        parts.append(f"CP {postcode}")
    return ", ".join(parts)


def _interpolate_along_points(pts: list[list[Any]], target_num: int) -> tuple[float, float] | None:
    """Interpola una coordenada (lat, lon) sobre una lista de vértices [lat, lon, num|None] de addr:interpolation."""
    numbered = [(i, p[2]) for i, p in enumerate(pts) if p[2] is not None]
    if len(numbered) < 2:
        return None

    # Buscar el tramo consecutivo de nodos numerados que contenga a target_num
    best_seg = None
    for k in range(len(numbered) - 1):
        i1, n1 = numbered[k]
        i2, n2 = numbered[k + 1]
        lo, hi = min(n1, n2), max(n1, n2)
        if lo <= target_num <= hi:
            best_seg = (i1, n1, i2, n2)
            break

    if best_seg is None:
        return None

    i1, n1, i2, n2 = best_seg
    if n1 == n2:
        return float(pts[i1][0]), float(pts[i1][1])

    sub_pts = pts[i1 : i2 + 1]
    t = (target_num - n1) / (n2 - n1)
    # Calcular longitud acumulada sobre los vértices intermedios del way
    cum_dist = [0.0]
    for idx in range(len(sub_pts) - 1):
        d = haversine_m(sub_pts[idx][0], sub_pts[idx][1], sub_pts[idx + 1][0], sub_pts[idx + 1][1])
        cum_dist.append(cum_dist[-1] + d)

    total_len = cum_dist[-1]
    if total_len <= 0:
        return float(sub_pts[0][0]), float(sub_pts[0][1])

    target_d = t * total_len
    for idx in range(len(cum_dist) - 1):
        if cum_dist[idx] <= target_d <= cum_dist[idx + 1]:
            seg_len = cum_dist[idx + 1] - cum_dist[idx]
            frac = (target_d - cum_dist[idx]) / seg_len if seg_len > 0 else 0.0
            lat = sub_pts[idx][0] + frac * (sub_pts[idx + 1][0] - sub_pts[idx][0])
            lon = sub_pts[idx][1] + frac * (sub_pts[idx + 1][1] - sub_pts[idx][1])
            return float(lat), float(lon)

    return float(sub_pts[-1][0]), float(sub_pts[-1][1])


class OsmGeocoder:
    """Motor de consultas rápido sobre SQLite + FTS5."""

    def __init__(self, db_path: Path = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)

    def is_ready(self) -> bool:
        return self.db_path.exists() and self.db_path.stat().st_size > 0

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def stats(self) -> dict[str, Any]:
        if not self.is_ready():
            return {"ready": False, "db_path": str(self.db_path)}
        with self._connect() as conn:
            rows = conn.execute("SELECT key, value FROM meta").fetchall()
            meta = {r["key"]: r["value"] for r in rows}
            meta["ready"] = True
            meta["db_path"] = str(self.db_path)
            meta["db_size_mb"] = f"{self.db_path.stat().st_size / (1024 * 1024):.1f}"
            return meta

    def list_provinces(self) -> list[dict[str, Any]]:
        if not self.is_ready():
            return []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT provincia, COUNT(*) AS street_count, SUM(addr_count) AS addr_count
                FROM streets
                WHERE provincia != ''
                GROUP BY provincia
                ORDER BY addr_count DESC, provincia ASC
                """
            ).fetchall()
            return [dict(r) for r in rows]

    def _fts_match_tokens(self, query: str) -> str:
        norm, _ = normalize_street(query)
        tokens = [t for t in norm.split() if len(t) >= 1]
        if not tokens:
            return ""
        # Prefijo en el último token para autocomplete fluido
        parts = [f'"{t}"' for t in tokens[:-1]] + [f'"{tokens[-1]}"*']
        return " AND ".join(parts)

    def suggest_streets(
        self,
        query: str,
        provincia: str = "",
        limit: int = 12,
    ) -> list[dict[str, Any]]:
        if not self.is_ready():
            return []
        fts_q = self._fts_match_tokens(query)
        if not fts_q:
            return []

        norm_prov = normalize_jurisdiction(provincia)
        with self._connect() as conn:
            sql = """
                SELECT display_name, norm_name, localidad, partido, provincia, addr_count
                FROM street_names_fts
                WHERE norm_name MATCH ?
            """
            params: list[Any] = [fts_q]
            if norm_prov:
                sql += " AND provincia LIKE ?"
                params.append(f"%{provincia.strip()}%")
            sql += " ORDER BY addr_count DESC, has_street_geom DESC LIMIT ?"
            params.append(limit * 3)
            rows = conn.execute(sql, params).fetchall()

        seen = set()
        results = []
        for r in rows:
            if norm_prov and norm_prov not in normalize_jurisdiction(r["provincia"]):
                continue
            key = (r["display_name"], r["localidad"], r["provincia"])
            if key in seen:
                continue
            seen.add(key)
            results.append(
                {
                    "calle": r["display_name"],
                    "localidad": r["localidad"],
                    "partido": r["partido"],
                    "provincia": r["provincia"],
                    "addr_count": r["addr_count"],
                }
            )
            if len(results) >= limit:
                break
        return results

    def _find_candidate_streets(
        self,
        conn: sqlite3.Connection,
        norm_street: str,
        core_street: str,
        norm_loc: str,
        norm_prov: str,
    ) -> list[tuple[str, str, str, float]]:
        """
        Devuelve lista de (norm_name, display_name, source_quality, street_sim)
        ordenadas de mejor a peor coincidencia de nombre de calle.
        """
        candidates: dict[str, tuple[str, float]] = {}

        # 1. Match exacto en norm_name / core_name
        exact_rows = conn.execute(
            """
            SELECT DISTINCT norm_name, display_name
            FROM street_names_fts
            WHERE norm_name = ? OR core_name = ?
            LIMIT 40
            """,
            (norm_street, core_street),
        ).fetchall()
        for r in exact_rows:
            sim = 1.0 if r["norm_name"] == norm_street else 0.96
            prev = candidates.get(r["norm_name"])
            if prev is None or sim > prev[1]:
                candidates[r["norm_name"]] = (r["display_name"], sim)

        # 2. Match FTS5 (maneja nombres parciales, ej. "Peron" -> "Tte Gral Juan Domingo Peron")
        tokens = [t for t in core_street.split() if t]
        if tokens and len(candidates) < 10:
            fts_q = " AND ".join(f'"{t}"*' for t in tokens)
            fts_rows = conn.execute(
                """
                SELECT norm_name, core_name, display_name, localidad, partido, provincia, addr_count
                FROM street_names_fts
                WHERE core_name MATCH ?
                ORDER BY addr_count DESC
                LIMIT 80
                """,
                (fts_q,),
            ).fetchall()
            for r in fts_rows:
                if norm_prov and norm_prov not in normalize_jurisdiction(r["provincia"]):
                    continue
                if norm_loc and (
                    norm_loc not in normalize_jurisdiction(r["localidad"])
                    and norm_loc not in normalize_jurisdiction(r["partido"])
                ):
                    continue
                rn = r["norm_name"]
                rc = r["core_name"]
                if rn == norm_street:
                    sim = 1.0
                elif rc == core_street:
                    sim = 0.96
                elif rc.startswith(core_street):
                    sim = 0.88
                else:
                    # Penalizar si el nombre encontrado tiene muchas más palabras que lo buscado
                    ratio = len(core_street) / max(len(rc), 1)
                    sim = round(0.62 + 0.25 * min(ratio, 1.0), 3)
                prev = candidates.get(rn)
                if prev is None or sim > prev[1]:
                    candidates[rn] = (r["display_name"], sim)

        # Fallback si con filtro estricto de localidad no encontró en FTS
        if not candidates and tokens:
            fts_q = " AND ".join(f'"{t}"*' for t in tokens)
            fts_rows = conn.execute(
                """
                SELECT norm_name, core_name, display_name, addr_count
                FROM street_names_fts
                WHERE core_name MATCH ?
                ORDER BY addr_count DESC
                LIMIT 40
                """,
                (fts_q,),
            ).fetchall()
            for r in fts_rows:
                rn = r["norm_name"]
                rc = r["core_name"]
                ratio = len(core_street) / max(len(rc), 1)
                sim = round(0.60 + 0.25 * min(ratio, 1.0), 3)
                prev = candidates.get(rn)
                if prev is None or sim > prev[1]:
                    candidates[rn] = (r["display_name"], sim)

        ordered = sorted(
            [(k, v[0], v[1]) for k, v in candidates.items()],
            key=lambda x: (-x[2], len(x[0])),
        )
        return [(n, d, "exact" if s >= 0.95 else "partial", s) for n, d, s in ordered[:12]]

    def _jurisdiction_score(
        self,
        prov: str,
        part: str,
        loc: str,
        norm_prov: str,
        norm_loc: str,
    ) -> tuple[bool, float]:
        """Devuelve (pasa_filtro, bonus_score)."""
        np = normalize_jurisdiction(prov)
        npart = normalize_jurisdiction(part)
        nloc = normalize_jurisdiction(loc)

        if norm_prov and norm_prov not in np:
            return False, 0.0

        bonus = 0.0
        if norm_loc:
            if norm_loc == nloc or norm_loc == npart:
                bonus += 0.25
            elif norm_loc in nloc or norm_loc in npart or norm_loc in np:
                bonus += 0.15
            else:
                return False, 0.0
        return True, bonus

    def _fetch_street_segments(
        self,
        conn: sqlite3.Connection,
        norm_street: str,
        prov: str,
        loc: str,
    ) -> list[list[list[float]]]:
        row = conn.execute(
            """
            SELECT segments_json FROM streets
            WHERE norm_name = ? AND provincia = ? AND localidad = ?
            LIMIT 1
            """,
            (norm_street, prov, loc),
        ).fetchone()
        if row is None:
            row = conn.execute(
                """
                SELECT segments_json FROM streets
                WHERE norm_name = ? AND provincia = ?
                LIMIT 1
                """,
                (norm_street, prov),
            ).fetchone()
        if row is None:
            return []
        try:
            return json.loads(row["segments_json"])
        except Exception:
            return []

    def geocode(
        self,
        calle: str = "",
        altura: str | int | None = None,
        localidad: str = "",
        provincia: str = "",
        raw_query: str = "",
        limit: int = 10,
    ) -> dict[str, Any]:
        t0 = time.perf_counter()
        if not self.is_ready():
            return {
                "ok": False,
                "error": "Base geocodificadora no construida todavía.",
                "results": [],
                "elapsed_ms": 0.0,
            }

        # Si viene raw_query sin calle explícita (o calle contiene número al final sin altura separada)
        if raw_query and not calle:
            parsed = parse_freeform_address(raw_query)
            calle = parsed["calle"]
            altura = altura or parsed["altura"]
            localidad = localidad or parsed["localidad"]
            provincia = provincia or parsed["provincia"]
        elif calle and (altura is None or str(altura).strip() == ""):
            parsed = parse_freeform_address(calle)
            if parsed["altura"]:
                calle = parsed["calle"]
                altura = parsed["altura"]
            if parsed["localidad"] and not localidad:
                localidad = parsed["localidad"]
            if parsed["provincia"] and not provincia:
                provincia = parsed["provincia"]

        norm_street, core_street = normalize_street(calle)
        if not norm_street:
            return {
                "ok": False,
                "error": "Ingresá un nombre de calle para buscar.",
                "results": [],
                "elapsed_ms": 0.0,
            }

        altura_str, target_num = parse_housenumber(altura)
        norm_loc = normalize_jurisdiction(localidad)
        norm_prov = normalize_jurisdiction(provincia)

        with self._connect() as conn:
            matched_streets = self._find_candidate_streets(
                conn, norm_street, core_street, norm_loc, norm_prov
            )
            if not matched_streets:
                elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
                return {
                    "ok": True,
                    "query": {
                        "calle": calle,
                        "altura": altura_str,
                        "localidad": localidad,
                        "provincia": provincia,
                        "norm_street": norm_street,
                        "core_street": core_street,
                    },
                    "results": [],
                    "elapsed_ms": elapsed_ms,
                }

            norm_names = [m[0] for m in matched_streets]
            sim_by_norm = {m[0]: m[3] for m in matched_streets}
            placeholders = ",".join("?" for _ in norm_names)

            results: list[GeocodeMatch] = []
            seen_keys: set[tuple[str, str, str, str]] = set()

            # --- NIVEL 1: Coincidencia exacta de calle + altura ---
            if target_num is not None:
                rows = conn.execute(
                    f"""
                    SELECT * FROM addresses
                    WHERE norm_street IN ({placeholders}) AND house_num = ?
                    LIMIT 120
                    """,
                    (*norm_names, target_num),
                ).fetchall()

                for r in rows:
                    ok, j_bonus = self._jurisdiction_score(
                        r["provincia"], r["partido"], r["localidad"], norm_prov, norm_loc
                    )
                    if not ok:
                        continue
                    sim = sim_by_norm.get(r["norm_street"], 0.8)
                    exact_hn_bonus = 0.05 if normalize_text(r["housenumber"]) == normalize_text(altura_str) else 0.0
                    conf = min(1.0, 0.85 * sim + j_bonus + exact_hn_bonus + 0.1)
                    dedup_key = (r["norm_street"], str(r["house_num"]), r["localidad"], r["provincia"])
                    if dedup_key in seen_keys:
                        continue
                    seen_keys.add(dedup_key)

                    segs = self._fetch_street_segments(conn, r["norm_street"], r["provincia"], r["localidad"])
                    fmt = _format_address(
                        r["street"], r["housenumber"], r["localidad"], r["partido"], r["provincia"], r["postcode"]
                    )
                    detail = f"Coincidencia exacta en OSM ({r['source_type']})"
                    if r["name"]:
                        detail += f" — {r['name']}"

                    results.append(
                        GeocodeMatch(
                            calle=r["street"],
                            altura=r["housenumber"],
                            localidad=r["localidad"],
                            partido=r["partido"],
                            provincia=r["provincia"],
                            postcode=r["postcode"],
                            formatted_address=fmt,
                            lat=r["lat"],
                            lon=r["lon"],
                            match_type="exacta",
                            match_label="Exacta (OSM)",
                            match_detail=detail,
                            confidence=conf,
                            osm_id=r["osm_id"],
                            osm_url=f"https://www.openstreetmap.org/{r['osm_id']}",
                            google_maps_url=f"https://www.google.com/maps?q={r['lat']:.6f},{r['lon']:.6f}",
                            street_segments=segs,
                            reference_points=[],
                        )
                    )

            # --- NIVEL 2: Interpolación oficial OSM (addr:interpolation) ---
            if target_num is not None:
                interp_rows = conn.execute(
                    f"""
                    SELECT * FROM interpolations
                    WHERE norm_street IN ({placeholders}) AND num_min <= ? AND num_max >= ?
                    LIMIT 60
                    """,
                    (*norm_names, target_num, target_num),
                ).fetchall()

                for r in interp_rows:
                    ok, j_bonus = self._jurisdiction_score(
                        r["provincia"], r["partido"], r["localidad"], norm_prov, norm_loc
                    )
                    if not ok:
                        continue
                    scheme = r["scheme"]
                    if scheme == "even" and target_num % 2 != 0:
                        continue
                    if scheme == "odd" and target_num % 2 == 0:
                        continue

                    dedup_key = (r["norm_street"], str(target_num), r["localidad"], r["provincia"])
                    if dedup_key in seen_keys:
                        continue

                    pts = json.loads(r["points_json"])
                    coord = _interpolate_along_points(pts, target_num)
                    if coord is None:
                        continue

                    seen_keys.add(dedup_key)
                    sim = sim_by_norm.get(r["norm_street"], 0.8)
                    conf = min(0.95, 0.78 * sim + j_bonus + 0.08)
                    segs = self._fetch_street_segments(conn, r["norm_street"], r["provincia"], r["localidad"])
                    fmt = _format_address(
                        r["street"], str(target_num), r["localidad"], r["partido"], r["provincia"]
                    )
                    results.append(
                        GeocodeMatch(
                            calle=r["street"],
                            altura=str(target_num),
                            localidad=r["localidad"],
                            partido=r["partido"],
                            provincia=r["provincia"],
                            postcode="",
                            formatted_address=fmt,
                            lat=coord[0],
                            lon=coord[1],
                            match_type="interpolada_osm",
                            match_label="Interpolada (rango OSM)",
                            match_detail=f"Interpolación OSM ({scheme}) entre alturas {r['num_min']} y {r['num_max']}",
                            confidence=conf,
                            osm_id=r["osm_id"],
                            osm_url=f"https://www.openstreetmap.org/{r['osm_id']}",
                            google_maps_url=f"https://www.google.com/maps?q={coord[0]:.6f},{coord[1]:.6f}",
                            street_segments=segs,
                            reference_points=[],
                        )
                    )

            # --- NIVEL 3: Interpolación / cercanía entre alturas conocidas en la misma calle ---
            if target_num is not None and len(results) < limit:
                addr_pool = conn.execute(
                    f"""
                    SELECT * FROM addresses
                    WHERE norm_street IN ({placeholders}) AND house_num IS NOT NULL
                    ORDER BY ABS(house_num - ?) ASC
                    LIMIT 400
                    """,
                    (*norm_names, target_num),
                ).fetchall()

                # Agrupar por (norm_street, provincia, localidad)
                by_area: dict[tuple[str, str, str], list[sqlite3.Row]] = defaultdict(list)
                for r in addr_pool:
                    ok, _ = self._jurisdiction_score(
                        r["provincia"], r["partido"], r["localidad"], norm_prov, norm_loc
                    )
                    if not ok:
                        continue
                    by_area[(r["norm_street"], r["provincia"], r["localidad"])].append(r)

                for (n_st, prov, loc), group in by_area.items():
                    dedup_key = (n_st, str(target_num), loc, prov)
                    if dedup_key in seen_keys:
                        continue

                    below = [g for g in group if g["house_num"] < target_num]
                    above = [g for g in group if g["house_num"] > target_num]

                    low = max(below, key=lambda x: x["house_num"]) if below else None
                    high = min(above, key=lambda x: x["house_num"]) if above else None

                    # Preferir misma paridad si está dentro de un rango razonable
                    if below and above:
                        same_par_low = [g for g in below if g["house_num"] % 2 == target_num % 2]
                        same_par_high = [g for g in above if g["house_num"] % 2 == target_num % 2]
                        if same_par_low and same_par_high:
                            cand_l = max(same_par_low, key=lambda x: x["house_num"])
                            cand_h = min(same_par_high, key=lambda x: x["house_num"])
                            if cand_h["house_num"] - cand_l["house_num"] <= 600:
                                low, high = cand_l, cand_h

                    sim = sim_by_norm.get(n_st, 0.8)
                    _, j_bonus = self._jurisdiction_score(prov, group[0]["partido"], loc, norm_prov, norm_loc)
                    segs = self._fetch_street_segments(conn, n_st, prov, loc)

                    if low is not None and high is not None:
                        dist_m = haversine_m(low["lat"], low["lon"], high["lat"], high["lon"])
                        span = high["house_num"] - low["house_num"]
                        if dist_m <= 3000 and span <= 1500:
                            t = (target_num - low["house_num"]) / span
                            lat = low["lat"] + t * (high["lat"] - low["lat"])
                            lon = low["lon"] + t * (high["lon"] - low["lon"])
                            seen_keys.add(dedup_key)
                            conf = min(0.90, 0.72 * sim + j_bonus + (0.06 if span <= 200 else 0.02))
                            fmt = _format_address(low["street"], str(target_num), loc, low["partido"], prov)
                            results.append(
                                GeocodeMatch(
                                    calle=low["street"],
                                    altura=str(target_num),
                                    localidad=loc,
                                    partido=low["partido"],
                                    provincia=prov,
                                    postcode=low["postcode"] or high["postcode"],
                                    formatted_address=fmt,
                                    lat=lat,
                                    lon=lon,
                                    match_type="interpolada_calle",
                                    match_label="Interpolada por alturas vecinas",
                                    match_detail=(
                                        f"Interpolada entre {low['housenumber']} y {high['housenumber']} "
                                        f"(distancia entre referencias: {dist_m:.0f} m)"
                                    ),
                                    confidence=conf,
                                    osm_id=low["osm_id"],
                                    osm_url=f"https://www.openstreetmap.org/{low['osm_id']}",
                                    google_maps_url=f"https://www.google.com/maps?q={lat:.6f},{lon:.6f}",
                                    street_segments=segs,
                                    reference_points=[
                                        {
                                            "altura": low["housenumber"],
                                            "lat": low["lat"],
                                            "lon": low["lon"],
                                        },
                                        {
                                            "altura": high["housenumber"],
                                            "lat": high["lat"],
                                            "lon": high["lon"],
                                        },
                                    ],
                                )
                            )
                            continue

                    # Si no hubo par bracketing válido, usar la altura más cercana si está cerca
                    nearest = min(group, key=lambda g: abs(g["house_num"] - target_num))
                    diff = abs(nearest["house_num"] - target_num)
                    if diff <= 300:
                        seen_keys.add(dedup_key)
                        conf = min(0.80, 0.60 * sim + j_bonus + (0.08 if diff <= 50 else 0.0))
                        fmt = _format_address(
                            nearest["street"], str(target_num), loc, nearest["partido"], prov, nearest["postcode"]
                        )
                        results.append(
                            GeocodeMatch(
                                calle=nearest["street"],
                                altura=str(target_num),
                                localidad=loc,
                                partido=nearest["partido"],
                                provincia=prov,
                                postcode=nearest["postcode"],
                                formatted_address=fmt,
                                lat=nearest["lat"],
                                lon=nearest["lon"],
                                match_type="cercana_calle",
                                match_label="Altura cercana en la calle",
                                match_detail=(
                                    f"La altura {target_num} no está cargada; "
                                    f"referencia más próxima: {nearest['housenumber']} (Δ {diff})"
                                ),
                                confidence=conf,
                                osm_id=nearest["osm_id"],
                                osm_url=f"https://www.openstreetmap.org/{nearest['osm_id']}",
                                google_maps_url=f"https://www.google.com/maps?q={nearest['lat']:.6f},{nearest['lon']:.6f}",
                                street_segments=segs,
                                reference_points=[
                                    {
                                        "altura": nearest["housenumber"],
                                        "lat": nearest["lat"],
                                        "lon": nearest["lon"],
                                    }
                                ],
                            )
                        )

            # --- NIVEL 4: Coincidencia a nivel de calle (sin altura cargada en OSM o sin altura pedida) ---
            if len(results) < limit:
                st_rows = conn.execute(
                    f"""
                    SELECT * FROM streets
                    WHERE norm_name IN ({placeholders})
                    ORDER BY addr_count DESC
                    LIMIT 60
                    """,
                    norm_names,
                ).fetchall()

                for r in st_rows:
                    ok, j_bonus = self._jurisdiction_score(
                        r["provincia"], r["partido"], r["localidad"], norm_prov, norm_loc
                    )
                    if not ok:
                        continue
                    # Si ya devolvimos una dirección exacta o interpolada en esta misma calle/localidad, no duplicar
                    if any(
                        res.localidad == r["localidad"]
                        and res.provincia == r["provincia"]
                        and normalize_street(res.calle)[0] == r["norm_name"]
                        for res in results
                    ):
                        continue

                    sim = sim_by_norm.get(r["norm_name"], 0.75)
                    base = 0.70 if target_num is None else 0.45
                    conf = min(0.85, base * sim + j_bonus)
                    segs = json.loads(r["segments_json"])
                    fmt = _format_address(r["name"], altura_str, r["localidad"], r["partido"], r["provincia"])
                    detail = (
                        f"Traza de calle en OSM (tipo: {r['highway_type']})"
                        if target_num is None
                        else f"Calle encontrada en OSM sin altura {target_num} cargada (tipo: {r['highway_type']})"
                    )
                    if r["addr_count"]:
                        detail += f" · rango cargado: {r['addr_min']}–{r['addr_max']}"

                    results.append(
                        GeocodeMatch(
                            calle=r["name"],
                            altura=altura_str,
                            localidad=r["localidad"],
                            partido=r["partido"],
                            provincia=r["provincia"],
                            postcode="",
                            formatted_address=fmt,
                            lat=r["lat"],
                            lon=r["lon"],
                            match_type="aproximada_calle",
                            match_label="Centroide de calle (sin altura)" if target_num is not None else "Calle encontrada",
                            match_detail=detail,
                            confidence=conf,
                            osm_id="",
                            osm_url=f"https://www.openstreetmap.org/#map=16/{r['lat']:.5f}/{r['lon']:.5f}",
                            google_maps_url=f"https://www.google.com/maps?q={r['lat']:.6f},{r['lon']:.6f}",
                            street_segments=segs,
                            reference_points=[],
                        )
                    )

            # Ordenar por confianza descendente, priorizando precisión (exacta > interpolada_osm > interpolada_calle > cercana > calle)
            type_rank = {
                "exacta": 5,
                "interpolada_osm": 4,
                "interpolada_calle": 3,
                "cercana_calle": 2,
                "aproximada_calle": 1,
            }
            results.sort(key=lambda x: (-round(x.confidence, 2), -type_rank.get(x.match_type, 0)))
            results = results[:limit]

        elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
        return {
            "ok": True,
            "query": {
                "calle": calle,
                "altura": altura_str,
                "localidad": localidad,
                "provincia": provincia,
                "norm_street": norm_street,
                "core_street": core_street,
            },
            "results": [r.to_dict() for r in results],
            "elapsed_ms": elapsed_ms,
        }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Indexa el extracto Geofabrik OSM (.osm.pbf) en SQLite+FTS5 para geocodificación."
    )
    parser.add_argument("--pbf", type=Path, default=DEFAULT_PBF_PATH, help="Ruta al .osm.pbf de Geofabrik")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="Ruta de salida de la base SQLite")
    parser.add_argument("--provincia", type=str, default="", help="Filtrar por provincia (opcional)")
    parser.add_argument("--force-download", action="store_true", help="Re-descargar .osm.pbf desde Geofabrik")
    args = parser.parse_args()

    build_index_from_pbf(
        pbf_path=args.pbf,
        db_path=args.db,
        provincia_filter=args.provincia,
        force_download=args.force_download,
    )


if __name__ == "__main__":
    main()
