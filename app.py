#!/usr/bin/env python3
"""
Interfaz gráfica interna para explorar, filtrar y exportar barrios privados
y sus puertas de acceso en Argentina.

Ejecución:
  uv run --with streamlit --with folium --with streamlit-folium --with pandas --with openpyxl streamlit run app.py
"""

import io
import json
from pathlib import Path

import folium
import pandas as pd
import streamlit as st
from streamlit_folium import st_folium

OUTPUT_DIR = Path(__file__).parent / "output"
ACCESOS_GEOJSON = OUTPUT_DIR / "argentina_accesos.geojson"
BARRIOS_GEOJSON = OUTPUT_DIR / "argentina_barrios.geojson"

st.set_page_config(
    page_title="Puertas de Barrios Privados - Argentina",
    page_icon="🏘️",
    layout="wide",
)


@st.cache_data(show_spinner="Cargando base geográfica...")
def load_data() -> tuple[pd.DataFrame, dict, dict]:
    if not ACCESOS_GEOJSON.exists():
        return pd.DataFrame(), {"type": "FeatureCollection", "features": []}, {"type": "FeatureCollection", "features": []}

    with ACCESOS_GEOJSON.open("r", encoding="utf-8") as f:
        accesos_gj = json.load(f)

    rows = []
    for feat in accesos_gj.get("features", []):
        props = dict(feat.get("properties", {}))
        coords = feat.get("geometry", {}).get("coordinates", [None, None])
        props.setdefault("lon", coords[0])
        props.setdefault("lat", coords[1])
        props.setdefault("rol_acceso", "principal" if props.get("acceso_num") == 1 else "alternativo")
        rows.append(props)

    df = pd.DataFrame(rows)
    for col in [
        "provincia",
        "partido_departamento",
        "localidad",
        "barrio",
        "osm_id",
        "rol_acceso",
        "nombre_acceso",
        "tipo_deteccion",
        "barrier_tag",
        "calle_tipo",
        "google_maps_url",
    ]:
        if col not in df.columns:
            df[col] = ""
        df[col] = df[col].fillna("").astype(str)

    if BARRIOS_GEOJSON.exists():
        with BARRIOS_GEOJSON.open("r", encoding="utf-8") as f:
            barrios_gj = json.load(f)
    else:
        barrios_gj = {"type": "FeatureCollection", "features": []}

    return df, accesos_gj, barrios_gj


def compute_bounds(df_pts: pd.DataFrame, barrio_features: list[dict]) -> list[list[float]] | None:
    lats: list[float] = []
    lons: list[float] = []

    def _collect_coords(coords):
        if not coords:
            return
        if isinstance(coords[0], (int, float)):
            lons.append(float(coords[0]))
            lats.append(float(coords[1]))
        else:
            for item in coords:
                _collect_coords(item)

    if barrio_features and len(barrio_features) <= 15:
        for feat in barrio_features:
            geom = feat.get("geometry") or {}
            _collect_coords(geom.get("coordinates"))

    if not lats and not df_pts.empty:
        lats = df_pts["lat"].dropna().astype(float).tolist()
        lons = df_pts["lon"].dropna().astype(float).tolist()

    if not lats:
        return None
    return [[min(lats), min(lons)], [max(lats), max(lons)]]


def build_excel_bytes(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Accesos")
    return buf.getvalue()


def main() -> None:
    st.title("🏘️ Puertas de Barrios Privados — Argentina")
    st.caption(
        "Visor interno para analistas funcionales y GIS. "
        "Visualizá polígonos, accesos principales vs. alternativos y exportá la selección."
    )

    df, accesos_gj, barrios_gj = load_data()
    if df.empty:
        st.error(
            f"No se encontró `{ACCESOS_GEOJSON}`. "
            "Ejecutá primero `python3 argentina_accesos.py` para generar la base."
        )
        return

    # --- Sidebar: Filtros ---
    with st.sidebar:
        st.header("🔍 Filtros y búsqueda")

        barrios_disponibles = sorted(df["barrio"].unique())
        barrio_sel = st.selectbox(
            "Buscar por nombre de barrio",
            options=["(Todos los barrios)"] + barrios_disponibles,
            index=0,
            help="Seleccioná un barrio para hacer zoom automático sobre su polígono y ver sus accesos.",
        )

        texto_libre = st.text_input(
            "Filtro de texto (barrio / localidad / partido)",
            placeholder="Ej: Nordelta, Canning, Pilar...",
        ).strip().lower()

        provs = sorted([p for p in df["provincia"].unique() if p])
        prov_sel = st.multiselect("Provincia", options=provs, default=[])

        df_scope = df if not prov_sel else df[df["provincia"].isin(prov_sel)]
        partidos = sorted([p for p in df_scope["partido_departamento"].unique() if p])
        partido_sel = st.multiselect("Partido / Departamento", options=partidos, default=[])

        if partido_sel:
            df_scope = df_scope[df_scope["partido_departamento"].isin(partido_sel)]
        localidades = sorted([l for l in df_scope["localidad"].unique() if l])
        loc_sel = st.multiselect("Localidad", options=localidades, default=[])

        st.divider()
        st.subheader("🚪 Tipo de acceso")
        roles_sel = st.multiselect(
            "Rol del acceso",
            options=["principal", "alternativo"],
            default=["principal", "alternativo"],
        )
        tipos_det = sorted([t for t in df["tipo_deteccion"].unique() if t])
        det_sel = st.multiselect(
            "Método de detección",
            options=tipos_det,
            default=tipos_det,
        )

        st.divider()
        st.subheader("🗺️ Capas del mapa")
        show_polygons = st.checkbox("Mostrar polígonos de barrios", value=True)
        show_links = st.checkbox(
            "Trazar conexión centro-puertas (al filtrar ≤ 25 barrios)",
            value=True,
        )

    # --- Aplicar filtros ---
    fdf = df.copy()
    if barrio_sel != "(Todos los barrios)":
        fdf = fdf[fdf["barrio"] == barrio_sel]
    if texto_libre:
        mask = (
            fdf["barrio"].str.lower().str.contains(texto_libre, na=False)
            | fdf["localidad"].str.lower().str.contains(texto_libre, na=False)
            | fdf["partido_departamento"].str.lower().str.contains(texto_libre, na=False)
        )
        fdf = fdf[mask]
    if prov_sel:
        fdf = fdf[fdf["provincia"].isin(prov_sel)]
    if partido_sel:
        fdf = fdf[fdf["partido_departamento"].isin(partido_sel)]
    if loc_sel:
        fdf = fdf[fdf["localidad"].isin(loc_sel)]
    if roles_sel:
        fdf = fdf[fdf["rol_acceso"].isin(roles_sel)]
    if det_sel:
        fdf = fdf[fdf["tipo_deteccion"].isin(det_sel)]

    filtered_osm_ids = set(fdf["osm_id"].unique())
    filtered_barrio_features = [
        feat
        for feat in barrios_gj.get("features", [])
        if feat.get("properties", {}).get("osm_id") in filtered_osm_ids
    ]

    # --- Métricas ---
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Barrios privados", f"{fdf['osm_id'].nunique():,}")
    c2.metric("Accesos totales", f"{len(fdf):,}")
    c3.metric("Accesos principales", f"{int((fdf['rol_acceso'] == 'principal').sum()):,}")
    c4.metric("Accesos alternativos", f"{int((fdf['rol_acceso'] == 'alternativo').sum()):,}")

    # --- Mapa ---
    m = folium.Map(location=[-34.65, -58.65], zoom_start=9, tiles="OpenStreetMap", control_scale=True)
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        attr="Esri World Imagery",
        name="Satélite (Esri)",
        overlay=False,
        control=True,
    ).add_to(m)

    if show_polygons and filtered_barrio_features:
        is_single = len(filtered_barrio_features) == 1
        poly_layer = folium.FeatureGroup(name="Polígonos de barrios", show=True)
        folium.GeoJson(
            {"type": "FeatureCollection", "features": filtered_barrio_features},
            style_function=lambda _: {
                "fillColor": "#2563eb",
                "color": "#1d4ed8",
                "weight": 3 if is_single else 1.5,
                "fillOpacity": 0.22 if is_single else 0.14,
            },
            highlight_function=lambda _: {
                "fillColor": "#3b82f6",
                "color": "#1e3a8a",
                "weight": 3,
                "fillOpacity": 0.35,
            },
            tooltip=folium.GeoJsonTooltip(
                fields=["barrio", "provincia", "partido_departamento", "cantidad_accesos", "superficie_ha"],
                aliases=["Barrio:", "Provincia:", "Partido/Depto:", "Accesos:", "Superficie (ha):"],
                sticky=True,
            ),
            popup=folium.GeoJsonPopup(
                fields=["barrio", "osm_id", "provincia", "partido_departamento", "localidad", "cantidad_accesos", "superficie_ha", "osm_url"],
                aliases=["Barrio", "OSM ID", "Provincia", "Partido/Depto", "Localidad", "Accesos", "Superficie (ha)", "OpenStreetMap"],
            ),
        ).add_to(poly_layer)
        poly_layer.add_to(m)
    elif show_polygons and not barrios_gj.get("features"):
        st.info(
            "Todavía no se generó `output/argentina_barrios.geojson`. "
            "Corré `argentina_accesos.py` para incluir los polígonos de los barrios."
        )

    # Trazar líneas centroide -> accesos cuando hay pocos barrios seleccionados
    n_barrios = fdf["osm_id"].nunique()
    if show_links and 0 < n_barrios <= 25 and not fdf.empty:
        links_layer = folium.FeatureGroup(name="Trazado centro → accesos", show=True)
        for _, grp in fdf.groupby("osm_id"):
            c_lat = grp["lat"].mean()
            c_lon = grp["lon"].mean()
            for _, row in grp.iterrows():
                is_main = row["rol_acceso"] == "principal"
                folium.PolyLine(
                    locations=[[c_lat, c_lon], [row["lat"], row["lon"]]],
                    color="#15803d" if is_main else "#d97706",
                    weight=2.5 if is_main else 1.5,
                    dash_array=None if is_main else "5, 5",
                    opacity=0.8,
                ).add_to(links_layer)
        links_layer.add_to(m)

    # Marcadores de accesos diferenciados por rol
    principal_layer = folium.FeatureGroup(name="Acceso principal", show=True)
    alt_layer = folium.FeatureGroup(name="Accesos alternativos", show=True)
    use_rich_icons = len(fdf) <= 150

    for _, row in fdf.iterrows():
        is_main = row["rol_acceso"] == "principal"
        target_layer = principal_layer if is_main else alt_layer
        badge = "⭐ PRINCIPAL" if is_main else "🔸 Alternativo"
        popup_html = (
            f"<b>{row['barrio']}</b><br>"
            f"<b>Rol:</b> {badge} (Acceso #{row['acceso_num']})<br>"
            f"<b>Detección:</b> {row['tipo_deteccion']}<br>"
            f"<b>Tag barrera:</b> {row['barrier_tag'] or '-'}<br>"
            f"<b>Calle:</b> {row['calle_tipo'] or '-'}<br>"
            f"<a href='{row['google_maps_url']}' target='_blank'>Abrir en Google Maps ↗</a>"
        )
        tooltip_txt = f"{row['barrio']} — {badge}"

        if use_rich_icons:
            folium.Marker(
                location=[row["lat"], row["lon"]],
                popup=folium.Popup(popup_html, max_width=280),
                tooltip=tooltip_txt,
                icon=folium.Icon(
                    color="green" if is_main else "orange",
                    icon="star" if is_main else "sign-in",
                    prefix="fa",
                ),
            ).add_to(target_layer)
        else:
            folium.CircleMarker(
                location=[row["lat"], row["lon"]],
                radius=6.5 if is_main else 4.5,
                color="#14532d" if is_main else "#92400e",
                weight=1.5,
                fill=True,
                fill_color="#22c55e" if is_main else "#f59e0b",
                fill_opacity=0.9,
                popup=folium.Popup(popup_html, max_width=280),
                tooltip=tooltip_txt,
            ).add_to(target_layer)

    principal_layer.add_to(m)
    alt_layer.add_to(m)
    folium.LayerControl(collapsed=False).add_to(m)

    bounds = compute_bounds(fdf, filtered_barrio_features)
    if bounds:
        m.fit_bounds(bounds, padding=(30, 30))

    st_folium(m, width=None, height=540, returned_objects=[])

    st.caption("🟢 **Verde / Estrella**: Acceso principal estimado | 🟠 **Naranja**: Accesos alternativos | 🔵 **Azul**: Perímetro del barrio privado")

    # --- Tabla de datos y exportación ---
    st.subheader("📋 Tabla de accesos filtrados")
    cols_display = [
        "provincia",
        "partido_departamento",
        "localidad",
        "barrio",
        "acceso_num",
        "rol_acceso",
        "nombre_acceso",
        "tipo_deteccion",
        "barrier_tag",
        "calle_tipo",
        "lat",
        "lon",
        "google_maps_url",
        "osm_id",
    ]
    st.dataframe(
        fdf[cols_display],
        use_container_width=True,
        hide_index=True,
        column_config={
            "google_maps_url": st.column_config.LinkColumn("Google Maps", display_text="Ver mapa ↗"),
            "lat": st.column_config.NumberColumn("Latitud", format="%.6f"),
            "lon": st.column_config.NumberColumn("Longitud", format="%.6f"),
        },
    )

    st.subheader("⬇️ Exportar selección actual")
    e1, e2, e3, e4 = st.columns(4)

    csv_bytes = fdf[cols_display].to_csv(index=False).encode("utf-8-sig")
    e1.download_button(
        "Descargar CSV (Excel)",
        data=csv_bytes,
        file_name="barrios_accesos_filtrado.csv",
        mime="text/csv",
        use_container_width=True,
    )

    e2.download_button(
        "Descargar Excel (.xlsx)",
        data=build_excel_bytes(fdf[cols_display]),
        file_name="barrios_accesos_filtrado.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
    )

    filtered_accesos_gj = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
                "properties": r,
            }
            for r in fdf[cols_display].to_dict(orient="records")
        ],
    }
    e3.download_button(
        "Descargar GeoJSON (Puntos)",
        data=json.dumps(filtered_accesos_gj, ensure_ascii=False, indent=2).encode("utf-8"),
        file_name="barrios_accesos_filtrado.geojson",
        mime="application/geo+json",
        use_container_width=True,
    )

    filtered_barrios_gj = {
        "type": "FeatureCollection",
        "features": filtered_barrio_features,
    }
    e4.download_button(
        "Descargar GeoJSON (Polígonos)",
        data=json.dumps(filtered_barrios_gj, ensure_ascii=False, indent=2).encode("utf-8"),
        file_name="barrios_poligonos_filtrado.geojson",
        mime="application/geo+json",
        disabled=len(filtered_barrio_features) == 0,
        use_container_width=True,
    )


if __name__ == "__main__":
    main()
