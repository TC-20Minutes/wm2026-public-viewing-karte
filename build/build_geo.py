#!/usr/bin/env python3
"""
Build the boundary GeoJSON the map uses to (a) mask out neighbouring countries
so only Switzerland + Liechtenstein are visible, and (b) highlight the selected
canton.

Source: geoBoundaries (gbOpen, open data, CC-BY 4.0).
  - CHE ADM0  -> national outline of Switzerland
  - LIE ADM0  -> Liechtenstein outline
  - CHE ADM1  -> the 26 cantons

Outputs (committed to the repo, so the page has no runtime dependency):
  assets/geo/outline.geojson   # Switzerland + Liechtenstein (mask + border)
  assets/geo/cantons.geojson   # cantons + Liechtenstein, each tagged with the
                               # `key` used in js/data.json's "canton" field

Coordinates are rounded to 4 decimals (~11 m) to keep the files small.

Usage:  python build/build_geo.py
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path

import requests

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parent.parent
GEO_DIR = ROOT / "assets" / "geo"
CACHE = Path(__file__).resolve().parent / "geo_cache"
REV = "9469f09"
BASE = "https://github.com/wmgeolab/geoBoundaries/raw/" + REV + "/releaseData/gbOpen"

SOURCES = {
    "che_adm0": BASE + "/CHE/ADM0/geoBoundaries-CHE-ADM0_simplified.geojson",
    "lie_adm0": BASE + "/LIE/ADM0/geoBoundaries-LIE-ADM0_simplified.geojson",
    "che_adm1": BASE + "/CHE/ADM1/geoBoundaries-CHE-ADM1_simplified.geojson",
}

# geoBoundaries canton shapeName -> the canton label used in our data.
SHAPENAME_TO_KEY = {
    "Aargau": "Aargau",
    "Appenzell Ausserrhoden": "Appenzell (AI/AR)",
    "Appenzell Innerrhoden": "Appenzell (AI/AR)",
    "Basel-Landschaft": "Basel-Land",
    "Basel-Stadt": "Basel-Stadt",
    "Bern": "Bern",
    "Fribourg": "Freiburg",
    "Genève": "Genf",
    "Glarus": "Glarus",
    "Graubünden": "Graubünden",
    "Jura": "Jura",
    "Luzern": "Luzern",
    "Neuchâtel": "Neuenburg",
    "Nidwalden": "Nidwalden",
    "Obwalden": "Obwalden",
    "Schaffhausen": "Schaffhausen",
    "Schwyz": "Schwyz",
    "Solothurn": "Solothurn",
    "St. Gallen": "St. Gallen",
    "Thurgau": "Thurgau",
    "Ticino": "Tessin",
    "Uri": "Uri",
    "Valais": "Wallis",
    "Vaud": "Waadt",
    "Zug": "Zug",
    "Zürich": "Zürich",
}


def fetch(name: str, url: str) -> dict:
    CACHE.mkdir(parents=True, exist_ok=True)
    cached = CACHE / (name + ".geojson")
    if cached.exists():
        return json.loads(cached.read_text(encoding="utf-8"))
    print("  downloading", name)
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    cached.write_text(r.text, encoding="utf-8")
    return r.json()


def round_coords(obj, nd=4):
    """Recursively round all numbers in a GeoJSON coordinate structure."""
    if isinstance(obj, (int, float)):
        return round(obj, nd)
    if isinstance(obj, list):
        return [round_coords(x, nd) for x in obj]
    return obj


def main() -> int:
    GEO_DIR.mkdir(parents=True, exist_ok=True)
    che0 = fetch("che_adm0", SOURCES["che_adm0"])
    lie0 = fetch("lie_adm0", SOURCES["lie_adm0"])
    che1 = fetch("che_adm1", SOURCES["che_adm1"])

    # ---- outline.geojson: Switzerland + Liechtenstein ----
    outline_feats = []
    for fc, label in ((che0, "Schweiz"), (lie0, "Liechtenstein")):
        for f in fc["features"]:
            outline_feats.append({
                "type": "Feature",
                "properties": {"name": label},
                "geometry": _round_geom(f["geometry"]),
            })
    write_fc(GEO_DIR / "outline.geojson", outline_feats)

    # ---- cantons.geojson: cantons + Liechtenstein, tagged with our key ----
    canton_feats = []
    unmatched = []
    for f in che1["features"]:
        name = f["properties"].get("shapeName")
        key = SHAPENAME_TO_KEY.get(name)
        if not key:
            unmatched.append(name)
            continue
        canton_feats.append({
            "type": "Feature",
            "properties": {"key": key, "name": name},
            "geometry": _round_geom(f["geometry"]),
        })
    # Liechtenstein appears in our canton filter too.
    for f in lie0["features"]:
        canton_feats.append({
            "type": "Feature",
            "properties": {"key": "Liechtenstein", "name": "Liechtenstein"},
            "geometry": _round_geom(f["geometry"]),
        })
    write_fc(GEO_DIR / "cantons.geojson", canton_feats)

    print("\n  outline.geojson:", len(outline_feats), "features",
          kb(GEO_DIR / "outline.geojson"))
    print("  cantons.geojson:", len(canton_feats), "features",
          kb(GEO_DIR / "cantons.geojson"))
    keys = sorted(set(f["properties"]["key"] for f in canton_feats))
    print("  canton keys:", keys)
    if unmatched:
        print("  ! unmatched shapeNames:", unmatched)
    return 0


def _round_geom(geom: dict) -> dict:
    return {"type": geom["type"], "coordinates": round_coords(geom["coordinates"])}


def write_fc(path: Path, features: list) -> None:
    fc = {"type": "FeatureCollection", "features": features}
    path.write_text(json.dumps(fc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def kb(path: Path) -> str:
    return "(" + str(path.stat().st_size // 1024) + " KB)"


if __name__ == "__main__":
    raise SystemExit(main())
