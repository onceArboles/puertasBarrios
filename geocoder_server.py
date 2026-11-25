#!/usr/bin/env python3
"""
Servidor web FastAPI + API REST para el geocodificador local de OpenStreetMap (Geofabrik).

Ejecución:
  uvicorn geocoder_server:app --host 0.0.0.0 --port 8000
o:
  python3 geocoder_server.py --port 8000
"""

from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from geocoder_engine import DEFAULT_DB_PATH, OsmGeocoder

WEB_DIR = Path(__file__).parent / "web"

app = FastAPI(
    title="Geocodificador OSM Argentina (Geofabrik)",
    description="Consulta direcciones por calle y altura sobre el extracto local de OpenStreetMap (Geofabrik).",
    version="1.0.0",
)

geocoder = OsmGeocoder(DEFAULT_DB_PATH)


class GeocodeRequest(BaseModel):
    calle: str = ""
    altura: str = ""
    localidad: str = ""
    provincia: str = ""
    q: str = ""
    limit: int = 10


@app.get("/api/health")
def api_health() -> dict:
    return geocoder.stats()


@app.get("/api/provinces")
def api_provinces() -> dict:
    return {"provinces": geocoder.list_provinces()}


@app.get("/api/suggest")
def api_suggest(
    q: str = Query("", min_length=1),
    provincia: str = Query(""),
    limit: int = Query(10, ge=1, le=30),
) -> dict:
    return {"suggestions": geocoder.suggest_streets(q, provincia=provincia, limit=limit)}


@app.get("/api/geocode")
def api_geocode_get(
    calle: str = Query(""),
    altura: str = Query(""),
    localidad: str = Query(""),
    provincia: str = Query(""),
    q: str = Query(""),
    limit: int = Query(10, ge=1, le=30),
) -> JSONResponse:
    res = geocoder.geocode(
        calle=calle,
        altura=altura,
        localidad=localidad,
        provincia=provincia,
        raw_query=q,
        limit=limit,
    )
    status = 200 if res.get("ok") else 400
    return JSONResponse(content=res, status_code=status)


@app.post("/api/geocode")
def api_geocode_post(payload: GeocodeRequest) -> JSONResponse:
    res = geocoder.geocode(
        calle=payload.calle,
        altura=payload.altura,
        localidad=payload.localidad,
        provincia=payload.provincia,
        raw_query=payload.q,
        limit=max(1, min(payload.limit, 30)),
    )
    status = 200 if res.get("ok") else 400
    return JSONResponse(content=res, status_code=status)


if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(str(WEB_DIR / "index.html"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Servidor web geocodificador OSM Geofabrik")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    args = parser.parse_args()

    global geocoder
    geocoder = OsmGeocoder(args.db)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
