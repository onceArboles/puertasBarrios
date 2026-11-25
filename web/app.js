(() => {
  const $ = (id) => document.getElementById(id);

  const form = $("geocode-form");
  const inputCalle = $("input-calle");
  const inputAltura = $("input-altura");
  const inputLocalidad = $("input-localidad");
  const selectProvincia = $("select-provincia");
  const btnSearch = $("btn-search");
  const btnClear = $("btn-clear");
  const suggestionsList = $("street-suggestions");

  const dbStatusBar = $("db-status-bar");
  const dbStatusText = $("db-status-text");

  const resultsHeader = $("results-header");
  const resultsCount = $("results-count");
  const resultsTiming = $("results-timing");
  const btnExportGeojson = $("btn-export-geojson");
  const resultsContainer = $("results-container");

  const detailCard = $("selected-detail-card");
  const detailBadge = $("detail-badge");
  const detailAddress = $("detail-address");
  const detailExplanation = $("detail-explanation");
  const detailCalle = $("detail-calle");
  const detailAltura = $("detail-altura");
  const detailLocalidad = $("detail-localidad");
  const detailPartido = $("detail-partido");
  const detailProvincia = $("detail-provincia");
  const detailCoords = $("detail-coords");
  const btnCloseDetail = $("btn-close-detail");
  const btnCopyCoords = $("btn-copy-coords");
  const btnCopyJson = $("btn-copy-json");
  const linkOsm = $("link-osm");
  const linkGmaps = $("link-gmaps");

  let currentResults = [];
  let selectedIndex = -1;
  let suggestTimer = null;
  let activeSuggestionIdx = -1;
  let lastSuggestions = [];

  // Inicializar mapa Leaflet centrado en Argentina
  const osmLayer = L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "&copy; OpenStreetMap contributors",
    maxZoom: 19,
  });

  const cartoLayer = L.tileLayer(
    "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
    {
      attribution: "&copy; OpenStreetMap &copy; CARTO",
      maxZoom: 19,
    }
  );

  const esriLayer = L.tileLayer(
    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    {
      attribution: "Esri World Imagery",
      maxZoom: 19,
    }
  );

  const map = L.map("map", {
    center: [-34.615, -58.433],
    zoom: 11,
    layers: [cartoLayer],
  });

  L.control
    .layers(
      {
        "Mapa limpio (CARTO)": cartoLayer,
        "OpenStreetMap": osmLayer,
        "Satélite (Esri)": esriLayer,
      },
      {},
      { position: "topright" }
    )
    .addTo(map);

  const streetOverlay = L.layerGroup().addTo(map);
  const refsOverlay = L.layerGroup().addTo(map);
  const markersOverlay = L.layerGroup().addTo(map);
  let markerRefs = [];

  function formatNumber(val) {
    const num = Number(val);
    return Number.isFinite(num) ? num.toLocaleString("es-AR") : val;
  }

  function escapeHtml(str) {
    return String(str ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  async function loadHealthAndProvinces() {
    try {
      const res = await fetch("/api/health");
      const data = await res.json();
      if (data.ready) {
        dbStatusBar.classList.add("ready");
        dbStatusBar.classList.remove("error");
        dbStatusText.textContent = `Base Geofabrik lista · ${formatNumber(
          data.total_addresses
        )} alturas · ${formatNumber(data.total_streets)} calles (${data.db_size_mb} MB)`;
      } else {
        dbStatusBar.classList.add("error");
        dbStatusBar.classList.remove("ready");
        dbStatusText.textContent =
          "Base aún no indexada. Ejecutá `python3 geocoder_engine.py` para generarla.";
      }
    } catch (err) {
      dbStatusBar.classList.add("error");
      dbStatusText.textContent = "Error al conectar con el servidor local.";
    }

    try {
      const res = await fetch("/api/provinces");
      const data = await res.json();
      if (Array.isArray(data.provinces)) {
        for (const p of data.provinces) {
          const opt = document.createElement("option");
          opt.value = p.provincia;
          opt.textContent = `${p.provincia} (${formatNumber(p.addr_count || 0)} alturas)`;
          selectProvincia.appendChild(opt);
        }
      }
    } catch (_) {
      // Ignorar si la base todavía no está lista
    }
  }

  function hideSuggestions() {
    suggestionsList.classList.add("hidden");
    suggestionsList.innerHTML = "";
    activeSuggestionIdx = -1;
    lastSuggestions = [];
  }

  function renderSuggestions(items) {
    lastSuggestions = items;
    activeSuggestionIdx = -1;
    if (!items.length) {
      hideSuggestions();
      return;
    }

    suggestionsList.innerHTML = items
      .map((item, idx) => {
        const locParts = [item.localidad, item.partido, item.provincia].filter(Boolean);
        const uniqueLoc = [...new Set(locParts)].join(" · ");
        const cntLabel = item.addr_count > 0 ? ` · ${formatNumber(item.addr_count)} alturas` : "";
        return `
          <li role="option" data-idx="${idx}">
            <span class="sug-street">${escapeHtml(item.calle)}</span>
            <span class="sug-meta">${escapeHtml(uniqueLoc)}${escapeHtml(cntLabel)}</span>
          </li>
        `;
      })
      .join("");

    suggestionsList.classList.remove("hidden");
  }

  function applySuggestion(idx) {
    const item = lastSuggestions[idx];
    if (!item) return;
    inputCalle.value = item.calle;
    if (item.localidad && !inputLocalidad.value.trim()) {
      inputLocalidad.value = item.localidad;
    }
    if (item.provincia && !selectProvincia.value) {
      selectProvincia.value = item.provincia;
    }
    hideSuggestions();
    inputAltura.focus();
  }

  inputCalle.addEventListener("input", () => {
    const q = inputCalle.value.trim();
    clearTimeout(suggestTimer);
    if (q.length < 2) {
      hideSuggestions();
      return;
    }
    suggestTimer = setTimeout(async () => {
      try {
        const params = new URLSearchParams({
          q,
          provincia: selectProvincia.value,
          limit: "10",
        });
        const res = await fetch(`/api/suggest?${params.toString()}`);
        const data = await res.json();
        renderSuggestions(data.suggestions || []);
      } catch (_) {
        hideSuggestions();
      }
    }, 150);
  });

  inputCalle.addEventListener("keydown", (ev) => {
    if (suggestionsList.classList.contains("hidden") || !lastSuggestions.length) return;
    const lis = suggestionsList.querySelectorAll("li");
    if (ev.key === "ArrowDown") {
      ev.preventDefault();
      activeSuggestionIdx = (activeSuggestionIdx + 1) % lis.length;
    } else if (ev.key === "ArrowUp") {
      ev.preventDefault();
      activeSuggestionIdx = (activeSuggestionIdx - 1 + lis.length) % lis.length;
    } else if (ev.key === "Enter" && activeSuggestionIdx >= 0) {
      ev.preventDefault();
      applySuggestion(activeSuggestionIdx);
      return;
    } else if (ev.key === "Escape") {
      hideSuggestions();
      return;
    } else {
      return;
    }
    lis.forEach((li, i) => li.classList.toggle("active", i === activeSuggestionIdx));
  });

  suggestionsList.addEventListener("mousedown", (ev) => {
    const li = ev.target.closest("li");
    if (!li) return;
    ev.preventDefault();
    applySuggestion(Number(li.dataset.idx));
  });

  document.addEventListener("click", (ev) => {
    if (!ev.target.closest(".autocomplete-wrap")) {
      hideSuggestions();
    }
  });

  function badgeClassFor(matchType) {
    return `precision-badge precision-${matchType || "aproximada_calle"}`;
  }

  function renderResults(data) {
    currentResults = data.results || [];
    selectedIndex = -1;
    streetOverlay.clearLayers();
    refsOverlay.clearLayers();
    markersOverlay.clearLayers();
    markerRefs = [];

    resultsHeader.classList.remove("hidden");
    resultsTiming.textContent = `${data.elapsed_ms ?? 0} ms`;

    if (!currentResults.length) {
      resultsCount.textContent = "0 resultados";
      btnExportGeojson.classList.add("hidden");
      detailCard.classList.add("hidden");
      resultsContainer.innerHTML = `
        <div class="empty-state">
          <p class="empty-title">No se encontraron coincidencias en la base OSM</p>
          <p class="empty-desc">
            Probá omitir abreviaturas o verificar el nombre de la calle y la localidad/provincia.
            ${data.error ? `<br/><strong>Detalle:</strong> ${escapeHtml(data.error)}` : ""}
          </p>
        </div>
      `;
      return;
    }

    resultsCount.textContent = `${currentResults.length} ${
      currentResults.length === 1 ? "resultado" : "resultados"
    }`;
    btnExportGeojson.classList.remove("hidden");

    resultsContainer.innerHTML = currentResults
      .map((item, idx) => {
        const pct = Math.round((item.confidence || 0) * 100);
        return `
          <article class="result-card" data-idx="${idx}" tabindex="0">
            <div class="result-card-top">
              <span class="${badgeClassFor(item.match_type)}">${escapeHtml(item.match_label)}</span>
              <span class="confidence-txt">${pct}% conf.</span>
            </div>
            <h3 class="result-address">${escapeHtml(item.formatted_address)}</h3>
            <p class="result-detail">${escapeHtml(item.match_detail)}</p>
            <div class="result-coords-row">
              <span class="mono-text">${item.lat.toFixed(6)}, ${item.lon.toFixed(6)}</span>
              <span class="mono-text">${escapeHtml(item.osm_id || "vía")}</span>
            </div>
          </article>
        `;
      })
      .join("");

    // Colocar marcadores en el mapa
    const boundsPts = [];
    currentResults.forEach((item, idx) => {
      const marker = L.circleMarker([item.lat, item.lon], {
        radius: idx === 0 ? 9 : 7,
        color: "#0b463a",
        weight: 2,
        fillColor: idx === 0 ? "#0f5c4d" : "#3b82f6",
        fillOpacity: 0.9,
      }).addTo(markersOverlay);

      marker.bindTooltip(`${escapeHtml(item.formatted_address)}`, { direction: "top" });
      marker.on("click", () => selectResult(idx, false));
      markerRefs.push(marker);
      boundsPts.push([item.lat, item.lon]);
    });

    if (boundsPts.length === 1) {
      map.setView(boundsPts[0], 17);
    } else if (boundsPts.length > 1) {
      map.fitBounds(boundsPts, { padding: [50, 50], maxZoom: 16 });
    }

    selectResult(0, true);
  }

  function selectResult(idx, panTo = true) {
    const item = currentResults[idx];
    if (!item) return;
    selectedIndex = idx;

    // Resaltar tarjeta en la lista
    const cards = resultsContainer.querySelectorAll(".result-card");
    cards.forEach((c, i) => c.classList.toggle("selected", i === idx));
    if (cards[idx]) {
      cards[idx].scrollIntoView({ block: "nearest", behavior: "smooth" });
    }

    // Actualizar estilos de marcadores
    markerRefs.forEach((m, i) => {
      const isSel = i === idx;
      m.setStyle({
        radius: isSel ? 10 : 6.5,
        color: isSel ? "#064e3b" : "#334155",
        weight: isSel ? 2.5 : 1.5,
        fillColor: isSel ? "#10b981" : "#64748b",
        fillOpacity: isSel ? 0.95 : 0.75,
      });
      if (isSel) m.bringToFront();
    });

    // Dibujar traza de calle si está disponible
    streetOverlay.clearLayers();
    if (Array.isArray(item.street_segments) && item.street_segments.length) {
      item.street_segments.forEach((seg) => {
        if (Array.isArray(seg) && seg.length >= 2) {
          L.polyline(seg, {
            color: "#0f5c4d",
            weight: 4,
            opacity: 0.55,
          }).addTo(streetOverlay);
        }
      });
    }

    // Dibujar referencias vecinas si fue interpolada
    refsOverlay.clearLayers();
    if (Array.isArray(item.reference_points) && item.reference_points.length) {
      item.reference_points.forEach((rp) => {
        L.polyline(
          [
            [item.lat, item.lon],
            [rp.lat, rp.lon],
          ],
          {
            color: "#d97706",
            weight: 2,
            dashArray: "5, 5",
            opacity: 0.85,
          }
        ).addTo(refsOverlay);

        L.circleMarker([rp.lat, rp.lon], {
          radius: 5,
          color: "#92400e",
          weight: 1.5,
          fillColor: "#f59e0b",
          fillOpacity: 0.9,
        })
          .bindTooltip(`Referencia: altura ${escapeHtml(rp.altura)}`, { direction: "top" })
          .addTo(refsOverlay);
      });
    }

    if (panTo) {
      const zoomLevel = item.match_type === "aproximada_calle" ? 15 : 17;
      map.flyTo([item.lat, item.lon], zoomLevel, { duration: 0.45 });
    }

    // Actualizar tarjeta flotante de detalle
    detailBadge.className = badgeClassFor(item.match_type);
    detailBadge.textContent = `${item.match_label} · ${Math.round((item.confidence || 0) * 100)}%`;
    detailAddress.textContent = item.formatted_address;
    detailExplanation.textContent = item.match_detail;
    detailCalle.textContent = item.calle || "—";
    detailAltura.textContent = item.altura || "S/N";
    detailLocalidad.textContent = item.localidad || "—";
    detailPartido.textContent = item.partido || "—";
    detailProvincia.textContent = item.provincia || "—";
    detailCoords.textContent = `${item.lat.toFixed(6)}, ${item.lon.toFixed(6)}`;
    linkOsm.href = item.osm_url || "#";
    linkGmaps.href = item.google_maps_url || "#";
    detailCard.classList.remove("hidden");
  }

  resultsContainer.addEventListener("click", (ev) => {
    const card = ev.target.closest(".result-card");
    if (!card) return;
    selectResult(Number(card.dataset.idx), true);
  });

  resultsContainer.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" || ev.key === " ") {
      const card = ev.target.closest(".result-card");
      if (!card) return;
      ev.preventDefault();
      selectResult(Number(card.dataset.idx), true);
    }
  });

  async function runGeocode() {
    hideSuggestions();
    const calle = inputCalle.value.trim();
    const altura = inputAltura.value.trim();
    const localidad = inputLocalidad.value.trim();
    const provincia = selectProvincia.value.trim();

    if (!calle) {
      inputCalle.focus();
      return;
    }

    btnSearch.disabled = true;
    try {
      const params = new URLSearchParams({
        calle,
        altura,
        localidad,
        provincia,
        limit: "12",
      });
      const res = await fetch(`/api/geocode?${params.toString()}`);
      const data = await res.json();
      renderResults(data);
    } catch (err) {
      resultsContainer.innerHTML = `
        <div class="empty-state">
          <p class="empty-title">Error al consultar el geocodificador</p>
          <p class="empty-desc">${escapeHtml(err.message || "Error de red")}</p>
        </div>
      `;
    } finally {
      btnSearch.disabled = false;
    }
  }

  form.addEventListener("submit", (ev) => {
    ev.preventDefault();
    runGeocode();
  });

  btnClear.addEventListener("click", () => {
    inputCalle.value = "";
    inputAltura.value = "";
    inputLocalidad.value = "";
    selectProvincia.value = "";
    hideSuggestions();
    currentResults = [];
    selectedIndex = -1;
    streetOverlay.clearLayers();
    refsOverlay.clearLayers();
    markersOverlay.clearLayers();
    resultsHeader.classList.add("hidden");
    detailCard.classList.add("hidden");
    resultsContainer.innerHTML = `
      <div class="empty-state">
        <p class="empty-title">Ingresá una calle y altura para comenzar</p>
        <p class="empty-desc">
          El motor consulta primero direcciones exactas (<code>addr:housenumber</code>),
          luego rangos de interpolación OSM (<code>addr:interpolation</code>),
          interpolación entre alturas vecinas y finalmente la traza vial.
        </p>
      </div>
    `;
    inputCalle.focus();
  });

  document.querySelectorAll(".chip-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      inputCalle.value = btn.dataset.calle || "";
      inputAltura.value = btn.dataset.altura || "";
      inputLocalidad.value = btn.dataset.localidad || "";
      selectProvincia.value = "";
      runGeocode();
    });
  });

  btnCloseDetail.addEventListener("click", () => {
    detailCard.classList.add("hidden");
  });

  function flashButton(btn, msg) {
    const orig = btn.textContent;
    btn.textContent = msg;
    setTimeout(() => {
      btn.textContent = orig;
    }, 1300);
  }

  btnCopyCoords.addEventListener("click", async () => {
    const item = currentResults[selectedIndex];
    if (!item) return;
    await navigator.clipboard.writeText(`${item.lat.toFixed(6)}, ${item.lon.toFixed(6)}`);
    flashButton(btnCopyCoords, "¡Copiado!");
  });

  btnCopyJson.addEventListener("click", async () => {
    const item = currentResults[selectedIndex];
    if (!item) return;
    await navigator.clipboard.writeText(JSON.stringify(item, null, 2));
    flashButton(btnCopyJson, "¡JSON copiado!");
  });

  btnExportGeojson.addEventListener("click", () => {
    if (!currentResults.length) return;
    const fc = {
      type: "FeatureCollection",
      features: currentResults.map((r) => ({
        type: "Feature",
        geometry: { type: "Point", coordinates: [r.lon, r.lat] },
        properties: {
          formatted_address: r.formatted_address,
          calle: r.calle,
          altura: r.altura,
          localidad: r.localidad,
          partido: r.partido,
          provincia: r.provincia,
          postcode: r.postcode,
          match_type: r.match_type,
          confidence: r.confidence,
          osm_id: r.osm_id,
        },
      })),
    };
    const blob = new Blob([JSON.stringify(fc, null, 2)], { type: "application/geo+json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "geocodificacion_osm.geojson";
    a.click();
    URL.revokeObjectURL(url);
  });

  loadHealthAndProvinces();
})();
