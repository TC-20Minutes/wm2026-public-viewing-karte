# Public Viewing WM 2026 – Schweizer Karte

Interactive, mobile-first map of all **112 public-viewing venues** for the 2026 Football World Cup across
Switzerland, built for **20 Minuten** and designed to be **embedded via `<iframe>`** in articles and hosted on
**GitHub Pages**.

- **Zoomable Leaflet map** on a monochrome **OpenStreetMap** basemap (CARTO Positron; standard OSM fallback).
- **Switzerland + Liechtenstein only**: neighbouring countries are masked out, and the selected canton is highlighted on filter.
- **Marker clustering** so dense areas (e.g. Zürich's 19 venues) stay readable; clusters split on zoom.
- **Tap a pin** → a large, finger-friendly detail sheet with the venue **name**, **canton**, **address**,
  **short description**, a **teaser image** (the link's Open Graph preview), and a **"Zur Website"** button.
- **Canton filter**, **geolocate-me**, and a **map / list toggle**.
- **Gesture handling**: one finger scrolls the article, two fingers pan/zoom the map (no scroll trap inside the iframe).
- **20 Minuten brand**: Matter typeface, blue-first palette.

Everything (libraries, fonts, teaser images, data) is **self-hosted in this repo** — the published page makes no
runtime call to third-party servers except the swisstopo map tiles.

## Project layout

```
index.html                 # the map page (iframe target)
css/style.css              # brand styling + Matter @font-face
js/app.js                  # map, clustering, canton filter, geolocate, list, detail sheet
js/data.json               # GENERATED – 112 venues + teaser image paths
assets/teasers/*.webp      # GENERATED – self-hosted og:images
assets/teasers/placeholder.svg
assets/geo/outline.geojson # GENERATED – Switzerland + Liechtenstein (mask + border)
assets/geo/cantons.geojson # GENERATED – cantons + Liechtenstein (filter highlight)
assets/fonts/Matter-*.woff2
vendor/                    # pinned Leaflet, markercluster, gesture-handling
build/build_data.py        # regenerates data.json + teaser images from the Excel sheet
build/build_geo.py         # regenerates the boundary GeoJSON from geoBoundaries
data/…xlsx                 # source spreadsheet
```

## Rebuilding the data

Run this whenever the source spreadsheet (`data/…xlsx`) changes, or to re-fetch preview images:

```bash
pip install -r build/requirements.txt
python build/build_data.py            # reuses already-downloaded teasers
python build/build_data.py --refresh  # re-fetches every og:image
```

The script parses the Excel `HYPERLINK()` formulas, fetches each venue link's `og:image`, downscales it to a
self-hosted WebP, and writes `js/data.json`. It prints a fetched-vs-failed summary at the end. Venues whose link has
no usable preview image (e.g. Instagram pages that require login) fall back to a branded placeholder — that is
expected and the map handles it gracefully.

The boundary files (country mask + canton highlight) are generated separately and rarely need rebuilding:

```bash
python build/build_geo.py   # downloads from geoBoundaries (cached in build/geo_cache/), writes assets/geo/*.geojson
```

## Local preview

```bash
python3 -m http.server 8000
# open http://localhost:8000/
```

## Publishing on GitHub Pages

1. Push this folder to a GitHub repository.
2. **Settings → Pages → Build and deployment → Source: Deploy from a branch**, branch `main`, folder `/ (root)`.
3. The site is served at `https://<org>.github.io/<repo>/`. (`.nojekyll` ensures `vendor/` and dot-paths serve as-is.)

## Embedding in a 20 Minuten article

Paste this responsive snippet into the article HTML. It keeps a fixed, mobile-friendly height and lazy-loads:

```html
<iframe
  src="https://tc-20minutes.github.io/wm2026-public-viewing-karte/"
  title="Public Viewing in der Schweiz – WM 2026"
  loading="lazy"
  style="width:100%; height:640px; max-height:85vh; border:0; border-radius:12px; overflow:hidden;"
  allow="geolocation">
</iframe>
```

Notes:
- `allow="geolocation"` enables the “find my location” button (the browser still asks the user for permission).
- Adjust `height` to taste; `640px` works well on mobile and desktop. The map itself fills the iframe.
- Because gesture handling is on, readers can scroll past the map with one finger; two fingers pan/zoom.
- **Dark mode:** the widget uses the 20 Minuten dark background `#1D242A`. There are three ways to set the theme,
  in order of precedence:
  1. **Follow the article live (recommended):** the host page tells the widget its theme via `postMessage`. This
     also updates instantly if the article has a light/dark toggle. Add this next to the iframe:
     ```html
     <iframe id="pv-map" src="https://tc-20minutes.github.io/wm2026-public-viewing-karte/"
             title="Public Viewing in der Schweiz – WM 2026" loading="lazy"
             style="width:100%;height:640px;max-height:85vh;border:0;border-radius:12px" allow="geolocation"></iframe>
     <script>
       (function () {
         var iframe = document.getElementById("pv-map");
         // Adapt this to how the page marks dark mode (class, attribute, etc.):
         function pageTheme() {
           return document.documentElement.classList.contains("dark") ? "dark" : "light";
         }
         function sendTheme() {
           iframe.contentWindow.postMessage({ type: "set-theme", theme: pageTheme() },
             "https://tc-20minutes.github.io");
         }
         iframe.addEventListener("load", sendTheme);   // initial sync
         // If the article has a live theme toggle, also call sendTheme() when it changes.
       })();
     </script>
     ```
  2. **Fixed per embed:** append `?theme=dark` or `?theme=light` to the `src` (no script needed; no live toggle).
  3. **Automatic:** with neither of the above, it follows the visitor's `prefers-color-scheme`.
- The widget has a subtle 1px border + 12px rounded corners so it reads as an embedded widget in light and dark pages.

## Attribution & licences

- Base map: © OpenStreetMap contributors, © [CARTO](https://carto.com/attributions) (Positron); fallback © OpenStreetMap contributors.
- Boundaries (country mask + cantons): [geoBoundaries](https://www.geoboundaries.org/) (gbOpen, CC-BY 4.0).
- Map library: [Leaflet](https://leafletjs.com/) + [Leaflet.markercluster](https://github.com/Leaflet/Leaflet.markercluster) + [leaflet-gesture-handling](https://github.com/elmarquis/Leaflet.GestureHandling).
- Typeface: **Matter** — commercial font; embedding assumes 20 Minuten holds the appropriate web licence.
- Teaser images are the respective venues' own Open Graph preview images, fetched from their public websites.
