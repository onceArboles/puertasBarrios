#!/usr/bin/env python3
"""Tests unitarios para normalización, parseo de direcciones y motor de geocodificación."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from geocoder_engine import (
    OsmGeocoder,
    _create_schema,
    _interpolate_along_points,
    normalize_jurisdiction,
    normalize_street,
    parse_freeform_address,
    parse_housenumber,
)


class TestNormalizationAndParsing(unittest.TestCase):
    def test_normalize_street_abbreviations_and_accents(self):
        norm, core = normalize_street("Av. Córdoba")
        self.assertEqual(norm, "avenida cordoba")
        self.assertEqual(core, "cordoba")

        norm2, core2 = normalize_street("Gral. José de San Martín")
        self.assertEqual(norm2, "general jose de san martin")
        self.assertEqual(core2, "general jose de san martin")

        norm3, core3 = normalize_street("Bv. Oroño")
        self.assertEqual(norm3, "bulevar orono")
        self.assertEqual(core3, "orono")

        norm4, core4 = normalize_street("Calle 50")
        self.assertEqual(norm4, "calle 50")
        self.assertEqual(core4, "calle 50")

    def test_parse_freeform_address(self):
        p1 = parse_freeform_address("Av. Corrientes 1234, CABA")
        self.assertEqual(p1["calle"], "Av. Corrientes")
        self.assertEqual(p1["altura"], "1234")
        self.assertEqual(p1["localidad"], "CABA")

        p2 = parse_freeform_address("9 de Julio 1500, Córdoba")
        self.assertEqual(p2["calle"], "9 de Julio")
        self.assertEqual(p2["altura"], "1500")
        self.assertEqual(p2["localidad"], "Córdoba")

        p3 = parse_freeform_address("Calle 50 Nº 720, La Plata, Buenos Aires")
        self.assertEqual(p3["calle"], "Calle 50")
        self.assertEqual(p3["altura"], "720")
        self.assertEqual(p3["localidad"], "La Plata")
        self.assertEqual(p3["provincia"], "Buenos Aires")

    def test_parse_housenumber(self):
        self.assertEqual(parse_housenumber("1234"), ("1234", 1234))
        self.assertEqual(parse_housenumber("1234 bis"), ("1234 bis", 1234))
        self.assertEqual(parse_housenumber(""), ("", None))

    def test_jurisdiction_aliases(self):
        self.assertEqual(normalize_jurisdiction("CABA"), "ciudad autonoma de buenos aires")
        self.assertEqual(normalize_jurisdiction("Capital Federal"), "ciudad autonoma de buenos aires")
        self.assertEqual(normalize_jurisdiction("Bs. As."), "buenos aires")


class TestGeocoderQueries(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test.db"
        conn = sqlite3.connect(str(self.db_path))
        _create_schema(conn)

        # Insertar direcciones de prueba
        conn.execute(
            """
            INSERT INTO addresses (
                street, norm_street, core_street, housenumber, house_num,
                lat, lon, provincia, partido, localidad, postcode, name, osm_id, source_type
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "Avenida Corrientes",
                "avenida corrientes",
                "corrientes",
                "1200",
                1200,
                -34.6039,
                -58.3836,
                "Ciudad Autónoma de Buenos Aires",
                "Comuna 1",
                "San Nicolás",
                "C1043",
                "",
                "node/1",
                "node",
            ),
        )
        conn.execute(
            """
            INSERT INTO addresses (
                street, norm_street, core_street, housenumber, house_num,
                lat, lon, provincia, partido, localidad, postcode, name, osm_id, source_type
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "Avenida Corrientes",
                "avenida corrientes",
                "corrientes",
                "1300",
                1300,
                -34.6039,
                -58.3850,
                "Ciudad Autónoma de Buenos Aires",
                "Comuna 1",
                "San Nicolás",
                "C1043",
                "",
                "node/2",
                "node",
            ),
        )
        conn.execute(
            """
            INSERT INTO interpolations (
                street, norm_street, core_street, scheme, num_min, num_max,
                points_json, provincia, partido, localidad, osm_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "Calle 50",
                "calle 50",
                "calle 50",
                "all",
                700,
                800,
                json.dumps([[-34.918, -57.951, 700], [-34.919, -57.952, 800]]),
                "Buenos Aires",
                "La Plata",
                "La Plata",
                "way/10",
            ),
        )
        conn.execute(
            """
            INSERT INTO streets (
                name, norm_name, core_name, highway_type, lat, lon, segments_json,
                provincia, partido, localidad, addr_count, addr_min, addr_max
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "Avenida Corrientes",
                "avenida corrientes",
                "corrientes",
                "primary",
                -34.6039,
                -58.3843,
                json.dumps([[[-34.6039, -58.3836], [-34.6039, -58.3850]]]),
                "Ciudad Autónoma de Buenos Aires",
                "Comuna 1",
                "San Nicolás",
                2,
                1200,
                1300,
            ),
        )
        conn.execute(
            """
            INSERT INTO streets (
                name, norm_name, core_name, highway_type, lat, lon, segments_json,
                provincia, partido, localidad, addr_count, addr_min, addr_max
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "Calle Falsa",
                "calle falsa",
                "falsa",
                "residential",
                -34.6100,
                -58.4000,
                json.dumps([[[-34.6100, -58.3990], [-34.6100, -58.4010]]]),
                "Ciudad Autónoma de Buenos Aires",
                "Comuna 3",
                "Balvanera",
                0,
                None,
                None,
            ),
        )
        conn.executemany(
            """
            INSERT INTO street_names_fts (
                norm_name, core_name, display_name, localidad, partido, provincia,
                addr_count, has_street_geom
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("avenida corrientes", "corrientes", "Avenida Corrientes", "San Nicolás", "Comuna 1", "Ciudad Autónoma de Buenos Aires", 2, 1),
                ("calle 50", "calle 50", "Calle 50", "La Plata", "La Plata", "Buenos Aires", 0, 0),
                ("calle falsa", "falsa", "Calle Falsa", "Balvanera", "Comuna 3", "Ciudad Autónoma de Buenos Aires", 0, 1),
            ],
        )
        conn.execute("INSERT INTO meta (key, value) VALUES ('total_addresses', '2')")
        conn.commit()
        conn.close()
        self.geocoder = OsmGeocoder(self.db_path)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_exact_match(self):
        res = self.geocoder.geocode(calle="Av. Corrientes", altura="1200", localidad="CABA")
        self.assertTrue(res["ok"])
        self.assertEqual(len(res["results"]), 1)
        r0 = res["results"][0]
        self.assertEqual(r0["match_type"], "exacta")
        self.assertAlmostEqual(r0["lat"], -34.6039, places=4)

    def test_neighbor_interpolation(self):
        res = self.geocoder.geocode(calle="Corrientes", altura="1250", provincia="CABA")
        self.assertTrue(res["ok"])
        r0 = res["results"][0]
        self.assertEqual(r0["match_type"], "interpolada_calle")
        self.assertAlmostEqual(r0["lon"], -58.3843, places=4)
        self.assertEqual(len(r0["reference_points"]), 2)

    def test_osm_interpolation_way(self):
        res = self.geocoder.geocode(calle="Calle 50", altura="750", localidad="La Plata")
        self.assertTrue(res["ok"])
        r0 = res["results"][0]
        self.assertEqual(r0["match_type"], "interpolada_osm")
        self.assertAlmostEqual(r0["lat"], -34.9185, places=3)

    def test_street_only_fallback(self):
        res = self.geocoder.geocode(calle="Calle Falsa", altura="123")
        self.assertTrue(res["ok"])
        r0 = res["results"][0]
        self.assertEqual(r0["match_type"], "aproximada_calle")

    def test_suggest_streets_with_abbreviation(self):
        sugs = self.geocoder.suggest_streets("Av. Corr")
        self.assertEqual(len(sugs), 1)
        self.assertEqual(sugs[0]["calle"], "Avenida Corrientes")

    def test_interpolate_along_points_helper(self):
        coord = _interpolate_along_points([[-34.0, -58.0, 100], [-34.0, -58.01, 200]], 150)
        self.assertIsNotNone(coord)
        self.assertAlmostEqual(coord[1], -58.005, places=4)


if __name__ == "__main__":
    unittest.main()
