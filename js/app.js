/* =====================================================================
   20 Minuten – Public Viewing WM 2026 map
   Vanilla JS + Leaflet. Loads js/data.json, renders clustered markers on a
   OpenStreetMap base map, with a canton filter, geolocate, list view and a
   mobile-first detail sheet.
   ===================================================================== */
(function () {
  "use strict";

  var PLACEHOLDER = "assets/teasers/placeholder.svg";
  var CH_BOUNDS = L.latLngBounds([45.7, 5.8], [47.95, 10.6]); // Switzerland + Liechtenstein
  var isMobile = function () { return window.matchMedia("(max-width: 719px)").matches; };

  var els = {
    app: document.getElementById("app"),
    map: document.getElementById("map"),
    list: document.getElementById("list"),
    canton: document.getElementById("canton-filter"),
    viewMap: document.getElementById("view-map"),
    viewList: document.getElementById("view-list"),
    count: document.getElementById("result-count"),
    sheet: document.getElementById("sheet"),
    scrim: document.getElementById("scrim"),
    sheetClose: document.getElementById("sheet-close"),
    sheetImg: document.getElementById("sheet-img"),
    sheetTitle: document.getElementById("sheet-title"),
    sheetCanton: document.getElementById("sheet-canton"),
    sheetAddress: document.getElementById("sheet-address"),
    sheetAddressRow: document.getElementById("sheet-address-row"),
    sheetDesc: document.getElementById("sheet-desc"),
    sheetLink: document.getElementById("sheet-link")
  };

  var state = {
    venues: [],
    markers: {},          // id -> L.marker
    activeId: null,
    lastFocus: null,
    view: "map"
  };

  /* ---- Map setup ---------------------------------------------------- */
  var map = L.map("map", {
    zoomControl: false,
    gestureHandling: true,
    gestureHandlingOptions: {
      text: {
        touch: "Zum Bewegen zwei Finger benutzen",
        scroll: "Mit Strg + Scrollen zoomen",
        scrollMac: "Mit ⌘ + Scrollen zoomen"
      },
      duration: 1800
    },
    maxBounds: CH_BOUNDS.pad(0.35),
    minZoom: 6,
    maxZoom: 18,
    zoomSnap: 0  // allow fractional zoom so the country fits tightly, uncut
  });

  // Minimal OpenStreetMap-based basemap (CARTO Positron, no labels) — clean
  // light grey with no place names so the markers and canton borders lead.
  var basemap = L.tileLayer("https://{s}.basemaps.cartocdn.com/light_nolabels/{z}/{x}/{y}{r}.png", {
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> &copy; <a href="https://carto.com/attributions" target="_blank" rel="noopener">CARTO</a>',
    subdomains: "abcd",
    maxZoom: 19
  }).addTo(map);

  // Fallback: standard OpenStreetMap tiles if CARTO fails repeatedly.
  var tileErrors = 0, fellBack = false;
  basemap.on("tileerror", function () {
    if (fellBack) return;
    if (++tileErrors >= 8) {
      fellBack = true;
      map.removeLayer(basemap);
      L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors',
        maxZoom: 19
      }).addTo(map);
    }
  });

  L.control.zoom({ position: "topright" }).addTo(map);
  map.fitBounds(CH_BOUNDS, { padding: [10, 10] });

  /* ---- Boundaries: mask out neighbours + highlight cantons ---------- */
  // Dedicated panes stacked between the tiles (200) and the markers (600).
  map.createPane("maskPane");        map.getPane("maskPane").style.zIndex = 350;        map.getPane("maskPane").style.pointerEvents = "none";
  map.createPane("cantonBorderPane");map.getPane("cantonBorderPane").style.zIndex = 355; map.getPane("cantonBorderPane").style.pointerEvents = "none";
  map.createPane("cantonPane");      map.getPane("cantonPane").style.zIndex = 360;      map.getPane("cantonPane").style.pointerEvents = "none";
  map.createPane("outlinePane");     map.getPane("outlinePane").style.zIndex = 370;     map.getPane("outlinePane").style.pointerEvents = "none";

  var cantonsByKey = {};
  var outlineBounds = null;
  var programmaticMove = false;  // true while we move the map ourselves
  var userInteracted = false;    // stop auto-refit once the user takes over
  map.on("movestart zoomstart", function () { if (!programmaticMove) userInteracted = true; });
  var highlightLayer = L.geoJSON(null, {
    pane: "cantonPane",
    interactive: false,
    style: { color: "#2659FF", weight: 2.5, fillColor: "#2659FF", fillOpacity: 0.12 }
  }).addTo(map);

  function eachExteriorRing(geom, cb) {
    if (!geom) return;
    if (geom.type === "Polygon") cb(geom.coordinates[0]);
    else if (geom.type === "MultiPolygon") geom.coordinates.forEach(function (poly) { cb(poly[0]); });
  }

  Promise.all([
    fetch("assets/geo/outline.geojson").then(function (r) { return r.json(); }),
    fetch("assets/geo/cantons.geojson").then(function (r) { return r.json(); })
  ]).then(function (res) {
    var outline = res[0], cantons = res[1];

    // Inverse mask: a world rectangle with Switzerland + Liechtenstein punched
    // out as holes, filled opaque so the surrounding countries disappear.
    var outer = [[-85, -180], [-85, 180], [85, 180], [85, -180]];
    var holes = [];
    outline.features.forEach(function (f) {
      eachExteriorRing(f.geometry, function (ring) {
        holes.push(ring.map(function (c) { return [c[1], c[0]]; }));
      });
    });
    L.polygon([outer].concat(holes), {
      pane: "maskPane", stroke: false, fillColor: "#d4dcea", fillOpacity: 0.96, interactive: false
    }).addTo(map);

    var outlineLayer = L.geoJSON(outline, {
      pane: "outlinePane", interactive: false,
      style: { fill: false, color: "#0D2880", weight: 1.8, opacity: 0.7 }
    }).addTo(map);

    // Always-on canton borders (subtle), drawn beneath any highlight.
    L.geoJSON(cantons, {
      pane: "cantonBorderPane", interactive: false,
      style: { fill: false, color: "#9fb0cc", weight: 1, opacity: 0.85 }
    }).addTo(map);

    // Fit the whole country with breathing room so it isn't clipped at the
    // edges, and don't let the user zoom out past that.
    outlineBounds = outlineLayer.getBounds();
    map.setMaxBounds(outlineBounds.pad(0.3));
    fitCountry();
    // Re-fit once layout has fully settled (mobile toolbars / address bars can
    // change the map height after first paint, which would otherwise clip it).
    [120, 400, 900].forEach(function (t) { setTimeout(fitCountry, t); });

    cantons.features.forEach(function (f) {
      (cantonsByKey[f.properties.key] = cantonsByKey[f.properties.key] || []).push(f);
    });
  }).catch(function (err) { console.warn("boundaries failed to load", err); });

  // Fit Switzerland + Liechtenstein to the current map size, padded so it is
  // never cut off. Recomputed on resize until the user interacts with the map.
  function fitCountry() {
    if (!outlineBounds) return;
    programmaticMove = true;
    map.invalidateSize({ animate: false });
    map.setMinZoom(6);
    // zoomSnap:0 means fitBounds fits exactly, with real per-side padding.
    map.fitBounds(outlineBounds, { padding: [22, 22], animate: false });
    map.setMinZoom(map.getZoom());  // can't zoom out past the whole country
    programmaticMove = false;
  }

  function updateCantonHighlight(key, fit) {
    highlightLayer.clearLayers();
    if (key && cantonsByKey[key]) {
      cantonsByKey[key].forEach(function (f) { highlightLayer.addData(f); });
      if (fit) map.fitBounds(highlightLayer.getBounds(), { padding: [28, 28], maxZoom: 13 });
    } else if (fit && outlineBounds) {
      map.fitBounds(outlineBounds, { padding: [8, 8] });
    }
  }

  /* ---- Geolocate control -------------------------------------------- */
  var userMarker = null;
  var LocateControl = L.Control.extend({
    options: { position: "topright" },
    onAdd: function () {
      var c = L.DomUtil.create("div", "leaflet-bar");
      var a = L.DomUtil.create("a", "pv-locate", c);
      a.href = "#"; a.title = "Meinen Standort finden"; a.setAttribute("role", "button");
      a.setAttribute("aria-label", "Meinen Standort finden");
      a.innerHTML = locateSvg();
      L.DomEvent.on(a, "click", function (e) {
        L.DomEvent.stop(e);
        locateUser(a);
      });
      return c;
    }
  });
  map.addControl(new LocateControl());

  function locateSvg() {
    return '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" aria-hidden="true">' +
      '<circle cx="12" cy="12" r="4" stroke="currentColor" stroke-width="2"/>' +
      '<path d="M12 2v3M12 19v3M2 12h3M19 12h3" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>';
  }

  function locateUser(btn) {
    if (!navigator.geolocation) { btn.classList.add("error"); return; }
    btn.classList.add("locating");
    navigator.geolocation.getCurrentPosition(function (pos) {
      btn.classList.remove("locating", "error");
      var ll = L.latLng(pos.coords.latitude, pos.coords.longitude);
      if (userMarker) { userMarker.setLatLng(ll); }
      else {
        userMarker = L.marker(ll, {
          icon: L.divIcon({ className: "", html: '<span class="pv-userdot"></span>', iconSize: [18, 18], iconAnchor: [9, 9] }),
          keyboard: false, interactive: false
        }).addTo(map);
      }
      map.flyTo(ll, Math.max(map.getZoom(), 12), { duration: 0.8 });
    }, function () {
      btn.classList.remove("locating");
      btn.classList.add("error");
    }, { enableHighAccuracy: true, timeout: 8000, maximumAge: 60000 });
  }

  /* ---- Marker cluster group ----------------------------------------- */
  var cluster = L.markerClusterGroup({
    showCoverageOnHover: false,
    maxClusterRadius: 55,
    spiderfyOnMaxZoom: true,
    iconCreateFunction: function (c) {
      var n = c.getChildCount();
      var size = n < 10 ? "small" : n < 30 ? "medium" : "large";
      return L.divIcon({
        html: "<div class='pv-cluster " + size + "'>" + n + "</div>",
        className: "",
        iconSize: null
      });
    }
  });
  map.addLayer(cluster);

  function pinIcon() {
    return L.divIcon({
      className: "",
      iconSize: [38, 38],
      iconAnchor: [19, 37],
      popupAnchor: [0, -34],
      html: "<svg class='pv-pin' viewBox='0 0 32 42' xmlns='http://www.w3.org/2000/svg' aria-hidden='true'>" +
        "<path d='M16 1C8.27 1 2 7.16 2 14.78 2 25 16 41 16 41s14-16 14-26.22C30 7.16 23.73 1 16 1Z' fill='#2659FF' stroke='#fff' stroke-width='2'/>" +
        "<circle cx='16' cy='15' r='5.4' fill='#fff'/></svg>"
    });
  }

  /* ---- Data load ---------------------------------------------------- */
  fetch("js/data.json")
    .then(function (r) { return r.json(); })
    .then(function (data) {
      state.venues = data.filter(function (v) { return typeof v.lat === "number" && typeof v.lon === "number"; });
      buildCantonOptions();
      buildMarkers();
      applyFilters();
    })
    .catch(function (err) {
      els.count.textContent = "Daten konnten nicht geladen werden.";
      console.error("data.json load failed", err);
    });

  function buildCantonOptions() {
    var cantons = {};
    state.venues.forEach(function (v) { if (v.canton) cantons[v.canton] = true; });
    Object.keys(cantons).sort(function (a, b) { return a.localeCompare(b, "de"); }).forEach(function (c) {
      var o = document.createElement("option");
      o.value = c; o.textContent = c;
      els.canton.appendChild(o);
    });
  }

  function buildMarkers() {
    state.venues.forEach(function (v) {
      var m = L.marker([v.lat, v.lon], {
        icon: pinIcon(),
        keyboard: true,
        title: v.name,
        alt: v.name
      });
      m.on("click", function () { openSheet(v, m); });
      m.on("keypress", function (e) { if (e.originalEvent.key === "Enter") openSheet(v, m); });
      state.markers[v.id] = m;
    });
  }

  /* ---- Filtering + rendering ---------------------------------------- */
  function currentMatches() {
    var canton = els.canton.value;
    return state.venues.filter(function (v) {
      return !canton || v.canton === canton;
    });
  }

  function applyFilters() {
    var matches = currentMatches();
    var matchIds = {};
    matches.forEach(function (v) { matchIds[v.id] = true; });

    // Sync cluster layer to the filtered set.
    cluster.clearLayers();
    var layers = matches.map(function (v) { return state.markers[v.id]; });
    cluster.addLayers(layers);

    renderList(matches);
    renderCount(matches.length);

    // If the active venue was filtered out, close the sheet.
    if (state.activeId && !matchIds[state.activeId]) closeSheet();
  }

  function renderCount(n) {
    var total = state.venues.length;
    if (n === total) {
      els.count.innerHTML = "<strong>" + total + "</strong> Public Viewings in der Schweiz";
    } else if (n === 0) {
      els.count.innerHTML = "Keine Treffer – Filter anpassen";
    } else {
      els.count.innerHTML = "<strong>" + n + "</strong> von " + total + " Standorten";
    }
  }

  function renderList(matches) {
    els.list.innerHTML = "";
    if (!matches.length) {
      var empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent = "Keine Standorte gefunden. Bitte einen anderen Kanton wählen.";
      els.list.appendChild(empty);
      return;
    }
    var sorted = matches.slice().sort(function (a, b) {
      return (a.canton || "").localeCompare(b.canton || "", "de") || a.name.localeCompare(b.name, "de");
    });
    var frag = document.createDocumentFragment();
    sorted.forEach(function (v) {
      var card = document.createElement("button");
      card.type = "button";
      card.className = "card";
      card.innerHTML =
        "<img class='thumb' loading='lazy' alt='' src='" + (v.image || PLACEHOLDER) + "'>" +
        "<span class='body'>" +
          "<h3>" + esc(v.name) + "</h3>" +
          "<span class='meta'><span class='chip'>" + esc(v.canton) + "</span>" + esc(shortAddr(v.address)) + "</span>" +
        "</span>";
      card.querySelector(".thumb").addEventListener("error", function () { this.src = PLACEHOLDER; });
      card.addEventListener("click", function () { focusVenueOnMap(v); });
      frag.appendChild(card);
    });
    els.list.appendChild(frag);
  }

  function shortAddr(a) {
    if (!a) return "";
    var parts = a.split(",");
    return parts[parts.length - 1].trim() || a;
  }

  /* ---- View toggle -------------------------------------------------- */
  function setView(view) {
    state.view = view;
    var listMode = view === "list";
    els.app.setAttribute("data-view", view);
    els.viewList.setAttribute("aria-pressed", String(listMode));
    els.viewMap.setAttribute("aria-pressed", String(!listMode));
    if (!listMode) { setTimeout(function () { map.invalidateSize(); }, 50); }
  }
  els.viewMap.addEventListener("click", function () { setView("map"); });
  els.viewList.addEventListener("click", function () { setView("list"); });

  // From a list card: switch to map, decluster, open sheet.
  function focusVenueOnMap(v) {
    var m = state.markers[v.id];
    setView("map");
    setTimeout(function () {
      map.invalidateSize();
      cluster.zoomToShowLayer(m, function () { openSheet(v, m); });
    }, 60);
  }

  /* ---- Detail sheet ------------------------------------------------- */
  function openSheet(v, marker) {
    state.lastFocus = document.activeElement;
    setActiveMarker(v.id);

    els.sheetTitle.textContent = v.name;
    els.sheetCanton.textContent = v.canton || "";
    els.sheetCanton.style.display = v.canton ? "" : "none";
    if (v.address) { els.sheetAddress.textContent = v.address; els.sheetAddressRow.style.display = ""; }
    else { els.sheetAddressRow.style.display = "none"; }
    els.sheetDesc.textContent = v.description || "";

    els.sheetImg.onerror = function () { els.sheetImg.onerror = null; els.sheetImg.src = PLACEHOLDER; };
    els.sheetImg.src = v.image || PLACEHOLDER;
    els.sheetImg.alt = "Vorschaubild: " + v.name;

    if (v.url) { els.sheetLink.href = v.url; els.sheetLink.style.display = ""; }
    else { els.sheetLink.style.display = "none"; }

    els.scrim.hidden = false; els.sheet.hidden = false;
    requestAnimationFrame(function () {
      els.scrim.classList.add("show");
      els.sheet.classList.add("show");
    });
    panToMarker(marker);
    els.sheetClose.focus();
  }

  function panToMarker(marker) {
    if (!marker) return;
    var ll = marker.getLatLng();
    var z = map.getZoom();
    if (isMobile()) {
      // Lift the marker above the bottom sheet (~30% from top of the map).
      var pt = map.project(ll, z).subtract([0, map.getSize().y * 0.22]);
      map.panTo(map.unproject(pt, z), { animate: true });
    } else {
      map.panTo(ll, { animate: true });
    }
  }

  function closeSheet() {
    if (els.sheet.hidden) return;
    els.scrim.classList.remove("show");
    els.sheet.classList.remove("show");
    setActiveMarker(null);
    var done = function () {
      els.sheet.hidden = true; els.scrim.hidden = true;
      els.sheet.removeEventListener("transitionend", done);
    };
    els.sheet.addEventListener("transitionend", done);
    setTimeout(done, 350); // fallback if no transition fires
    if (state.lastFocus && state.lastFocus.focus) state.lastFocus.focus();
  }

  function setActiveMarker(id) {
    if (state.activeId && state.markers[state.activeId]) {
      var prev = state.markers[state.activeId].getElement();
      if (prev) { var p = prev.querySelector(".pv-pin"); if (p) p.classList.remove("active"); }
    }
    state.activeId = id;
    if (id && state.markers[id]) {
      var el = state.markers[id].getElement();
      if (el) { var c = el.querySelector(".pv-pin"); if (c) c.classList.add("active"); }
    }
  }

  /* ---- Events ------------------------------------------------------- */
  els.canton.addEventListener("change", function () {
    applyFilters();
    updateCantonHighlight(els.canton.value, true);
    if (state.view === "list") setView("map"); // show the highlight on the map
  });
  els.sheetClose.addEventListener("click", closeSheet);
  els.scrim.addEventListener("click", closeSheet);
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && !els.sheet.hidden) closeSheet();
  });

  // Swipe-down to dismiss the sheet on touch devices.
  var touchStartY = null;
  els.sheet.addEventListener("touchstart", function (e) {
    if (els.sheet.querySelector(".scroll").scrollTop <= 0) touchStartY = e.touches[0].clientY;
  }, { passive: true });
  els.sheet.addEventListener("touchmove", function (e) {
    if (touchStartY === null) return;
    var dy = e.touches[0].clientY - touchStartY;
    if (dy > 80) { closeSheet(); touchStartY = null; }
  }, { passive: true });
  els.sheet.addEventListener("touchend", function () { touchStartY = null; });

  var onResize = debounce(function () {
    if (!userInteracted && outlineBounds) fitCountry();
    else map.invalidateSize();
  }, 200);
  window.addEventListener("resize", onResize);
  window.addEventListener("orientationchange", function () { setTimeout(onResize, 250); });

  /* ---- Utils -------------------------------------------------------- */
  function debounce(fn, ms) {
    var t; return function () { var a = arguments, c = this; clearTimeout(t); t = setTimeout(function () { fn.apply(c, a); }, ms); };
  }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
    });
  }
})();
