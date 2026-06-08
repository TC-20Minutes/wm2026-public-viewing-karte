#!/usr/bin/env python3
"""
Build script for the 20 Minuten Public-Viewing map.

Reads the source Excel sheet, extracts each venue's real URL from the Excel
HYPERLINK() formula, fetches the link's Open Graph preview image (og:image),
downloads + optimises it to a self-hosted WebP, and emits js/data.json that the
static frontend consumes. Everything it produces is committed to the repo so the
GitHub Pages site has no runtime dependency on third-party servers.

Re-runnable / idempotent: existing teaser images are reused unless --refresh is
passed. Prints a clear fetched-vs-failed summary at the end (no silent gaps).

Usage:
    pip install -r build/requirements.txt
    python build/build_data.py            # reuse already-downloaded teasers
    python build/build_data.py --refresh  # re-fetch every og:image
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin, urlparse

import openpyxl
import requests
from bs4 import BeautifulSoup
from PIL import Image

warnings.filterwarnings("ignore")  # silence LibreSSL/urllib3 noise

# --- Paths -------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
XLSX = ROOT / "data" / "Public_Viewing_mit_Kurzbeschrieb_und_klickbaren_Links.xlsx"
SHEET = "Public Viewing"
TEASER_DIR = ROOT / "assets" / "teasers"
DATA_OUT = ROOT / "js" / "data.json"

# --- Network config ----------------------------------------------------------
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "de-CH,de;q=0.9,en;q=0.8",
}
HTML_TIMEOUT = 12
IMG_TIMEOUT = 15
TEASER_WIDTH = 700          # downscale wide images to this max width
TEASER_QUALITY = 80
MIN_IMG_BYTES = 1500        # ignore 1px tracking pixels / empty responses
MAX_WORKERS = 12

HYPERLINK_RE = re.compile(r'HYPERLINK\("([^"]+)"', re.IGNORECASE)


def log(msg: str) -> None:
    print(msg, flush=True)


def extract_url(cell_value) -> str | None:
    """Return the real URL from a cell that may hold an Excel HYPERLINK formula."""
    if cell_value is None:
        return None
    s = str(cell_value).strip()
    m = HYPERLINK_RE.search(s)
    if m:
        return m.group(1).strip()
    if s.lower().startswith(("http://", "https://")):
        return s
    return None


def split_name_address(combined: str) -> tuple[str, str]:
    """'Name: Street 1, 1234 City' -> ('Name', 'Street 1, 1234 City')."""
    if combined and ":" in combined:
        name, addr = combined.split(":", 1)
        return name.strip(), addr.strip()
    return (combined or "").strip(), ""


def slugify(text: str, fallback: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug or fallback


def find_og_image(html: str, base_url: str) -> str | None:
    """Pick the best preview image: og:image -> twitter:image -> largest <img>."""
    soup = BeautifulSoup(html, "html.parser")

    for attr, key in (("property", "og:image:secure_url"),
                      ("property", "og:image:url"),
                      ("property", "og:image"),
                      ("name", "twitter:image"),
                      ("name", "twitter:image:src")):
        tag = soup.find("meta", attrs={attr: key})
        if tag and tag.get("content"):
            return urljoin(base_url, tag["content"].strip())

    link = soup.find("link", attrs={"rel": "image_src"})
    if link and link.get("href"):
        return urljoin(base_url, link["href"].strip())

    # Fallback: the first reasonably-sized <img> on the page.
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src")
        if not src or src.startswith("data:"):
            continue
        return urljoin(base_url, src.strip())
    return None


def fetch_teaser(idx: int, venue: dict, refresh: bool) -> dict:
    """Fetch + store one venue's teaser. Returns a status dict (no exceptions escape)."""
    vid = venue["id"]
    out_path = TEASER_DIR / f"{vid}.webp"
    rel_path = f"assets/teasers/{vid}.webp"

    if out_path.exists() and not refresh:
        venue["image"] = rel_path
        return {"id": vid, "status": "cached"}

    url = venue["url"]
    try:
        resp = requests.get(url, headers=HEADERS, timeout=HTML_TIMEOUT, allow_redirects=True)
        resp.raise_for_status()
        img_url = find_og_image(resp.text, resp.url)
        if not img_url:
            venue["image"] = None
            return {"id": vid, "status": "no-og", "url": url}

        img_resp = requests.get(img_url, headers=HEADERS, timeout=IMG_TIMEOUT, allow_redirects=True)
        img_resp.raise_for_status()
        if len(img_resp.content) < MIN_IMG_BYTES:
            venue["image"] = None
            return {"id": vid, "status": "tiny-img", "url": img_url}

        im = Image.open(io.BytesIO(img_resp.content))
        im = im.convert("RGB")
        if im.width > TEASER_WIDTH:
            ratio = TEASER_WIDTH / im.width
            im = im.resize((TEASER_WIDTH, max(1, round(im.height * ratio))), Image.LANCZOS)
        TEASER_DIR.mkdir(parents=True, exist_ok=True)
        im.save(out_path, "WEBP", quality=TEASER_QUALITY, method=6)
        venue["image"] = rel_path
        return {"id": vid, "status": "ok"}
    except Exception as exc:  # noqa: BLE001 - want to keep going on any single failure
        venue["image"] = None
        return {"id": vid, "status": "error", "url": url, "error": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Build data.json + teaser images.")
    parser.add_argument("--refresh", action="store_true", help="re-fetch every og:image")
    args = parser.parse_args()

    if not XLSX.exists():
        log(f"ERROR: source workbook not found: {XLSX}")
        return 1

    wb = openpyxl.load_workbook(XLSX)
    ws = wb[SHEET]
    rows = list(ws.iter_rows(values_only=True))[1:]  # skip header

    venues = []
    seen_ids: dict[str, int] = {}
    skipped = 0
    for i, row in enumerate(rows):
        combined, lat, lon, canton, name, desc, link = row[:7]
        url = extract_url(link)
        if lat is None or lon is None:
            skipped += 1
            log(f"  ! skipping row {i + 2}: missing coordinates ({name})")
            continue
        name = (name or "").strip()
        _, address = split_name_address(combined or "")

        base_slug = slugify(name, f"venue-{i}")
        seen_ids[base_slug] = seen_ids.get(base_slug, 0) + 1
        vid = base_slug if seen_ids[base_slug] == 1 else f"{base_slug}-{seen_ids[base_slug]}"

        venues.append({
            "id": vid,
            "name": name,
            "address": address,
            "canton": (canton or "").strip(),
            "lat": round(float(lat), 6),
            "lon": round(float(lon), 6),
            "description": (desc or "").strip(),
            "url": url,
            "image": None,
        })

    log(f"Parsed {len(venues)} venues ({skipped} skipped).")
    log(f"Fetching teaser images ({MAX_WORKERS} workers, refresh={args.refresh}) ...")

    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(fetch_teaser, i, v, args.refresh): v for i, v in enumerate(venues)}
        for fut in as_completed(futures):
            res = fut.result()
            results.append(res)
            marker = {"ok": "✓", "cached": "·"}.get(res["status"], "✗")
            log(f"  {marker} {res['id']} [{res['status']}]")

    DATA_OUT.parent.mkdir(parents=True, exist_ok=True)
    DATA_OUT.write_text(json.dumps(venues, ensure_ascii=False, indent=2), encoding="utf-8")

    # --- Summary -------------------------------------------------------------
    by_status: dict[str, int] = {}
    for r in results:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    with_img = sum(1 for v in venues if v["image"])

    log("\n" + "=" * 56)
    log(f"  data.json written: {DATA_OUT.relative_to(ROOT)}  ({len(venues)} venues)")
    log(f"  teasers available: {with_img}/{len(venues)}")
    for status, count in sorted(by_status.items()):
        log(f"    - {status}: {count}")
    failed = [r for r in results if r["status"] in ("error", "no-og", "tiny-img")]
    if failed:
        log("\n  Venues without a teaser (branded placeholder will be used):")
        for r in failed:
            detail = r.get("error") or r.get("url", "")
            log(f"    · {r['id']}: {r['status']} {detail}")
    log("=" * 56)
    return 0


if __name__ == "__main__":
    sys.exit(main())
