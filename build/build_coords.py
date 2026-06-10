#!/usr/bin/env python3
"""
Re-geocode every venue in js/data.json against the swisstopo address geocoder
(api3.geo.admin.ch SearchServer) so each pin sits on its real address instead of
the low-precision lat/lon that came out of the source spreadsheet.

Why swisstopo and not a generic geocoder: api3.geo.admin.ch resolves Swiss (and
Liechtenstein) street addresses to the actual building, which is far more
accurate for this dataset than Nominatim/Google's interpolated guesses.

Strategy per venue:
  1. Clean the messy address string (drop venue-name prefixes, "; nur Spiele …"
     notes, etc.) down to "<street nr>, <plz> <city>".
  2. If there is NO house number, keep the existing (hand-placed) coordinate:
     the geocoder would only return some arbitrary low building number on a
     possibly-long street, which is a downgrade, not a fix.
  3. With a house number, query origins=address and keep only results whose
     street name matches the request (suffix/article tolerant, so "Rue de la
     Madeleine" matches the register's "Rue Madeleine"). Use the exact house
     number; if it isn't registered (e.g. MAAG's "Hardstrasse 219"), use the
     numerically NEAREST number on the SAME street.
  4. Never trust results[0] blindly — that's how the first pass put "Hardstrasse
     219" onto "Hardstrasse 181" (on the rail tracks). And never fall back to a
     locality centroid.
  5. If the match is on a different street, or the point would jump an
     implausible distance, KEEP the old coordinate and flag it.

Nothing is silently changed: every move is printed with its distance and the
matched register address, and the flagged set is summarised at the end.
Re-runnable / idempotent.

Usage:
    python build/build_coords.py            # apply corrections, write data.json
    python build/build_coords.py --dry-run  # report only, don't write
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
import unicodedata
import warnings
from pathlib import Path

import requests

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "js" / "data.json"

SEARCH_URL = "https://api3.geo.admin.ch/rest/services/api/SearchServer"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
HEADERS = {"User-Agent": "wm2026-public-viewing-karte/1.0 (coordinate fixup; taddeo.cerletti@20minuten.ch)"}
TIMEOUT = 15

# Name-lookup results are cached on disk so re-runs don't re-hit the public APIs
# (and so we stay well within Nominatim's 1 req/s usage policy).
CACHE_DIR = ROOT / "build" / "geo_cache"
NAME_CACHE = CACHE_DIR / "name_cache.json"
NOMINATIM_PAUSE = 1.1  # seconds between Nominatim calls (usage policy)

# A move larger than this (metres) is treated as a likely mis-geocode: we keep
# the old coordinate and flag it instead of trusting the geocoder blindly.
MAX_MOVE_M = 1500.0

# Name-based verification (third signal, in addition to the address + the old
# coordinate). A name hit is only trusted if it sits within NAME_VALID_KM of the
# venue's municipality. Two independent sources (OSM + swisstopo) agreeing within
# NAME_CORROBORATE_M is "corroborated". When the address match and the name hit
# disagree by more than NAME_FLAG_M, we keep the (authoritative) address point
# but flag the venue so it can be eyeballed.
NAME_VALID_KM = 6.0
NAME_CORROBORATE_M = 250.0
NAME_FLAG_M = 300.0

# Manually verified corrections for venues whose source coordinate was badly
# wrong (>MAX_MOVE_M) but whose exact swisstopo address match was confirmed by
# hand (see build notes). These override the distance guard.
OVERRIDES = {
    # Hegenheimermattweg 130, Allschwil — source point was 2.2 km too far north.
    "sandoase-arena": (47.55779, 7.54848),
    # Sommeraustrasse 32 (Südostschweiz/Radio Grischa), Chur West — was 2.2 km E.
    "radio-grischa": (46.84803, 9.50205),
    # Route des Ecussons 10 (Camping near the Rhône), Sion — was 6.2 km off.
    "camping-sedunum": (46.21139, 7.31201),
    # Caught by the name-vs-address cross-check: the swisstopo "Kanonenstrasse"
    # house numbers sit on a different segment of the hill road, ~700 m from the
    # actual hotel. OSM + the original hand-placed point agree on the knoll above
    # the Gütschbahn — use that.
    "ch-teau-g-tsch": (47.05167, 8.29492),
    # The source address "Merkurstrasse 20" is the organiser's (brewery) address;
    # the actual Strandbad is 1.9 km away at Strandbadweg, confirmed by OSM and
    # swisstopo (Strandbadweg 4).
    "strandbad-sursee": (47.17414, 8.12502),
}


def log(msg: str = "") -> None:
    print(msg, flush=True)


def haversine_m(lat1, lon1, lat2, lon2) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


PLZ_CITY_RE = re.compile(r"(\d{4})\s+([A-Za-zÀ-ÿ.\-/ ]+)")


def clean_address(addr: str):
    """('Street 1, 1234 City' with noise) -> (street_query, 'plz city') or (None, None)."""
    if not addr:
        return None, None
    a = addr.split(";")[0].strip()  # drop "; nur Spiele …" style notes
    m = PLZ_CITY_RE.search(a)
    if not m:
        return None, None
    plz = m.group(1)
    city = m.group(2).strip().strip(",").strip()
    plz_city = f"{plz} {city}"

    before = a[: m.start()].rstrip().rstrip(",").strip()
    parts = [p.strip() for p in before.split(",") if p.strip()]
    street = None
    for p in reversed(parts):  # prefer the last chunk that carries a house number
        if re.search(r"\d", p):
            street = p
            break
    if street is None and parts:
        street = parts[-1]
    if street and ":" in street:  # "der Tennisarena Rümikon: Rümikerstrasse 5B"
        street = street.split(":")[-1].strip()
    return street, plz_city


def _ascii(s: str) -> str:
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()


# Generic street-type words (dropped) and German street-type suffixes (stripped),
# so "Rue de la Madeleine" matches the register's "Rue Madeleine" and
# "Obere Matten" matches "Obere Mattenstrasse".
_ARTICLES = {"de", "la", "le", "les", "du", "des", "l", "der", "die", "das", "den",
             "am", "im", "beim", "bei", "von", "zur", "zum", "d", "of", "the", "und", "et", "a"}
_TYPEWORDS = {"rue", "avenue", "av", "route", "rte", "chemin", "ch", "place", "pl",
              "boulevard", "bd", "via", "viale", "strada", "piazza", "impasse", "quai",
              "allee", "promenade", "ruelle", "passage", "sentier"}
_SUFFIXES = ("strasse", "strase", "gasse", "weg", "platz", "rain", "halde", "matt")


def street_tokens(name: str) -> frozenset:
    """Significant comparison tokens of a street name (articles/type-words removed)."""
    out = set()
    for tok in re.split(r"[^a-z0-9]+", _ascii(name)):
        if not tok or tok in _ARTICLES or tok in _TYPEWORDS:
            continue
        for suf in _SUFFIXES:
            if tok.endswith(suf) and len(tok) > len(suf) + 1:
                tok = tok[: -len(suf)]
                break
        out.add(tok)
    return frozenset(out)


def street_match(req: str, found: str) -> bool:
    """True if two street names refer to the same street (fuzzy, suffix/article tolerant)."""
    a, b = street_tokens(req), street_tokens(found)
    if not a or not b:
        return False
    return a == b or a <= b or b <= a


HOUSE_NUM_RE = re.compile(r"(\d+)\s*([A-Za-z]?)\s*$")
# "Hardstrasse 181 8005 Zürich" -> street / number / letter
LABEL_RE = re.compile(r"^(.+?)\s+(\d+)\s*([A-Za-z]?)\s+\d{4}\b")


def parse_house(street: str):
    """'Hardstrasse 219' -> ('Hardstrasse', 219, ''). No trailing number -> (street, None, '')."""
    m = HOUSE_NUM_RE.search(street)
    if not m:
        return street.strip(), None, ""
    return street[: m.start()].strip(), int(m.group(1)), m.group(2).lower()


def query(search_text: str, limit: int = 30):
    params = {
        "searchText": search_text,
        "type": "locations",
        "origins": "address",
        "sr": "4326",
        "limit": str(limit),
    }
    resp = requests.get(SEARCH_URL, params=params, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json().get("results", [])


def geocode(req_street: str, req_num: int, req_letter: str, plz_city: str):
    """House-number-aware address resolver. Requires a house number.

    Returns (lat, lon, confidence, matched_label) or None, where confidence is:
      'exact'      — the requested street + house number is in the register
      'nearest:N'  — that number is not registered; nearest number on the SAME
                     street was used (N = how far off, in house-number units)
    We only accept results whose street name matches the request (suffix/article
    tolerant), and never fall back to a locality centroid — that would downgrade
    a hand-placed point. Anything we can't pin to the right street returns None
    and keeps its existing coordinate.
    """
    try:
        results = query(f"{req_street} {req_num} {plz_city}")
    except Exception as exc:  # noqa: BLE001
        log(f"    (query failed: {type(exc).__name__})")
        return None

    on_street = []
    for r in results:
        a = r["attrs"]
        label = re.sub(r"<[^>]+>", "", a.get("label", ""))
        lm = LABEL_RE.match(label)
        if not lm or not street_match(req_street, lm.group(1)):
            continue
        on_street.append((int(lm.group(2)), lm.group(3).lower(), a["lat"], a["lon"], label))

    if not on_street:
        return None
    # Exact house-number (and letter, if given) match wins.
    for n, letter, lat, lon, label in on_street:
        if n == req_num and (not req_letter or letter == req_letter):
            return lat, lon, "exact", label
    # Otherwise the numerically nearest house number on the same street.
    n, letter, lat, lon, label = min(on_street, key=lambda t: abs(t[0] - req_num))
    return lat, lon, f"nearest:{abs(n - req_num)}", label


# --- Name-based verification (third signal) ---------------------------------

_name_cache: dict = {}
_nominatim_last = [0.0]


def load_cache() -> None:
    global _name_cache
    if NAME_CACHE.exists():
        _name_cache = json.loads(NAME_CACHE.read_text(encoding="utf-8"))


def save_cache() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    NAME_CACHE.write_text(json.dumps(_name_cache, ensure_ascii=False, indent=0), encoding="utf-8")


def clean_name(name: str) -> str:
    """Strip parenthetical notes for a cleaner POI query: 'Moyo (MAAG …)' -> 'Moyo'."""
    return re.sub(r"\s*\([^)]*\)", "", name or "").strip()


def nominatim(qtext: str):
    """OSM POI/place search, country-restricted. Returns [(lat, lon, label), …]."""
    key = f"nom|{qtext}"
    if key in _name_cache:
        return _name_cache[key]
    # rate-limit
    wait = NOMINATIM_PAUSE - (time.monotonic() - _nominatim_last[0])
    if wait > 0:
        time.sleep(wait)
    out = []
    try:
        resp = requests.get(
            NOMINATIM_URL,
            params={"q": qtext, "format": "json", "limit": "5", "countrycodes": "ch,li"},
            headers=HEADERS, timeout=TIMEOUT,
        )
        _nominatim_last[0] = time.monotonic()
        resp.raise_for_status()
        for r in resp.json():
            out.append((float(r["lat"]), float(r["lon"]), r.get("display_name", "")[:80]))
    except Exception as exc:  # noqa: BLE001
        log(f"    (nominatim failed for {qtext!r}: {type(exc).__name__})")
    _name_cache[key] = out
    return out


def gazetteer(name: str):
    """swisstopo named-feature search. Returns [(lat, lon, label), …]."""
    key = f"gaz|{name}"
    if key in _name_cache:
        return _name_cache[key]
    out = []
    try:
        resp = requests.get(
            SEARCH_URL,
            params={"searchText": name, "type": "locations", "origins": "gazetteer",
                    "sr": "4326", "limit": "6"},
            headers=HEADERS, timeout=TIMEOUT,
        )
        resp.raise_for_status()
        for r in resp.json().get("results", []):
            a = r["attrs"]
            out.append((a["lat"], a["lon"], re.sub(r"<[^>]+>", "", a.get("label", ""))))
    except Exception as exc:  # noqa: BLE001
        log(f"    (gazetteer failed for {name!r}: {type(exc).__name__})")
    _name_cache[key] = out
    return out


_GENERIC = {"public", "viewing", "the", "und", "der", "die", "das", "im", "am", "von"}


def distinctive_tokens(name: str, plz_city: str | None) -> set:
    """Tokens (≥4 chars) of the venue name minus the city name and generic words.

    Used to confirm an OSM hit actually refers to THIS venue and not just some
    other object in the same town."""
    city = {t for t in re.split(r"[^a-z0-9]+", _ascii(plz_city or "")) if t}
    return {t for t in re.split(r"[^a-z0-9]+", _ascii(name))
            if len(t) >= 4 and t not in _GENERIC and t not in city}


def name_geocode(name: str, plz_city: str | None, anchor):
    """Locate a venue by NAME and validate it against `anchor` (lat, lon).

    Returns {'lat','lon','sources','corroborated','label'} or None.

    OSM/Nominatim is the standalone source (it has actual venue POIs). A hit
    must (a) sit within NAME_VALID_KM of the anchor — same municipality — and
    (b) actually mention one of the venue's distinctive name tokens, so we don't
    grab an unrelated object in town. The swisstopo gazetteer is used ONLY to
    corroborate an OSM hit (gazetteer alone tends to return town/locality
    centroids, e.g. "Zermatt", not the venue itself).
    """
    if anchor is None:
        return None

    def near(cands):
        return [c for c in cands
                if haversine_m(anchor[0], anchor[1], c[0], c[1]) <= NAME_VALID_KM * 1000]

    want = distinctive_tokens(name, plz_city)
    if not want:
        return None  # nothing distinctive to verify against -> don't guess

    q_city = f", {plz_city}" if plz_city else ""
    osm_n = []
    for lat, lon, label in near(nominatim(f"{clean_name(name)}{q_city}")):
        have = set(re.split(r"[^a-z0-9]+", _ascii(label)))
        if want & have:
            osm_n.append((lat, lon, label))
    if not osm_n:
        return None

    swt_n = near(gazetteer(name))
    o = osm_n[0]
    # Corroboration: a swisstopo named feature agreeing with the OSM hit.
    for s in swt_n:
        if haversine_m(o[0], o[1], s[0], s[1]) <= NAME_CORROBORATE_M:
            return {"lat": (o[0] + s[0]) / 2, "lon": (o[1] + s[1]) / 2,
                    "sources": ["osm", "swisstopo"], "corroborated": True, "label": o[2]}
    return {"lat": o[0], "lon": o[1], "sources": ["osm"], "corroborated": False, "label": o[2]}


def locality_centroid(plz_city: str | None):
    """Municipality centroid for `plz_city`, used as the name-validation anchor."""
    if not plz_city:
        return None
    key = f"loc|{plz_city}"
    if key in _name_cache:
        v = _name_cache[key]
        return tuple(v) if v else None
    centroid = None
    try:
        resp = requests.get(
            SEARCH_URL,
            params={"searchText": plz_city, "type": "locations",
                    "origins": "zipcode,gg25", "sr": "4326", "limit": "1"},
            headers=HEADERS, timeout=TIMEOUT,
        )
        resp.raise_for_status()
        res = resp.json().get("results", [])
        if res:
            centroid = [res[0]["attrs"]["lat"], res[0]["attrs"]["lon"]]
    except Exception:  # noqa: BLE001
        pass
    _name_cache[key] = centroid
    return tuple(centroid) if centroid else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="report only, don't write data.json")
    args = ap.parse_args()
    load_cache()

    venues = json.loads(DATA.read_text(encoding="utf-8"))
    log(f"Loaded {len(venues)} venues from {DATA.relative_to(ROOT)}\n")

    exact, nearest, by_name, flagged, overridden, disagree = [], [], [], [], [], []

    for v in venues:
        old_lat, old_lon = v["lat"], v["lon"]
        street, plz_city = clean_address(v["address"])

        if v["id"] in OVERRIDES:
            lat, lon = OVERRIDES[v["id"]]
            dist = haversine_m(old_lat, old_lon, lat, lon)
            v["lat"], v["lon"] = round(lat, 6), round(lon, 6)
            overridden.append((v, dist))
            log(f"# {v['name']:<38} moved {dist:6.0f} m  [verified override]")
            continue

        # 1) Address (street + house number) -> swisstopo building register.
        req_street, req_num, req_letter = parse_house(street) if street else (None, None, "")
        addr = None
        if street and plz_city and req_num is not None:
            addr = geocode(req_street, req_num, req_letter, plz_city)
            time.sleep(0.12)

        # 2) Name -> OSM + swisstopo gazetteer, validated against the municipality.
        anchor = locality_centroid(plz_city) or (old_lat, old_lon)
        name_res = name_geocode(v["name"], plz_city, anchor)

        # --- Decide which signal wins ----------------------------------------
        if addr is not None:
            lat, lon, conf, label = addr
            dist = haversine_m(old_lat, old_lon, lat, lon)
            if dist > MAX_MOVE_M:
                flagged.append((v, f"address jump {dist:.0f}m -> {label}", street, plz_city))
                log(f"⚠ {v['name']:<38} KEPT (address would jump {dist:.0f} m)")
                continue
            v["lat"], v["lon"] = round(lat, 6), round(lon, 6)
            # Cross-check the building address against the name location.
            if name_res:
                nd = haversine_m(lat, lon, name_res["lat"], name_res["lon"])
                if nd > NAME_FLAG_M:
                    disagree.append((v, nd, label, name_res))
            (exact if conf == "exact" else nearest).append((v, dist, conf, label))
            mark = "·" if conf == "exact" else "~"
            log(f"{mark} {v['name']:<38} moved {dist:6.0f} m  [{conf}]")
            continue

        # No usable street address -> fall back to the NAME location.
        if name_res is not None:
            nd_old = haversine_m(old_lat, old_lon, name_res["lat"], name_res["lon"])
            # Trust a name hit if two sources corroborate it, or if it merely
            # refines the existing point (≤ MAX_MOVE_M/2). A lone, far hit could
            # be a same-name POI elsewhere -> flag for review instead.
            if name_res["corroborated"] or nd_old <= MAX_MOVE_M / 2:
                v["lat"], v["lon"] = round(name_res["lat"], 6), round(name_res["lon"], 6)
                by_name.append((v, nd_old, name_res))
                mark = "++" if name_res["corroborated"] else "+"
                src = "+".join(name_res["sources"])
                log(f"{mark} {v['name']:<38} moved {nd_old:6.0f} m  [name:{src} -> {name_res['label'][:40]}]")
            else:
                flagged.append((v, f"name hit uncorroborated & {nd_old:.0f}m away", street, plz_city))
                log(f"✗ {v['name']:<38} KEPT (name hit far & single-source)")
            continue

        flagged.append((v, "no address or name match", street, plz_city))
        log(f"✗ {v['name']:<38} KEPT (no address or name match) — addr={v['address']!r}")

    if not args.dry_run:
        save_cache()
    else:
        save_cache()  # persist fetched lookups so re-runs are fast either way

    log("\n" + "=" * 64)
    log(f"  exact street+number match:                {len(exact)}")
    log(f"  nearest-number (exact nr not registered): {len(nearest)}")
    for v, dist, conf, label in nearest:
        log(f"    ~ {v['id']}: {v['address'].split(',')[0]!r} -> [{conf}]")
    log(f"  verified override:                        {len(overridden)}")
    log(f"  located by NAME (no house number):        {len(by_name)}")
    for v, dist, nr in by_name:
        tag = "corroborated" if nr["corroborated"] else f"single-source:{nr['sources'][0]}"
        log(f"    + {v['id']}: moved {dist:.0f}m -> {nr['label'][:46]!r} [{tag}]")
    log(f"  flagged, kept old coords:                 {len(flagged)}")
    for v, why, street, plz_city in flagged:
        log(f"    · {v['id']}: {why}")
    if disagree:
        log(f"  NAME vs ADDRESS disagree >{NAME_FLAG_M:.0f}m (kept address, review): {len(disagree)}")
        for v, nd, label, nr in disagree:
            log(f"    ? {v['id']}: address {label!r} is {nd:.0f}m from name {nr['label'][:36]!r}")
    log("=" * 64)

    if args.dry_run:
        log("\n--dry-run: data.json NOT written.")
        return 0

    DATA.write_text(json.dumps(venues, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"\ndata.json written ({len(venues)} venues).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
