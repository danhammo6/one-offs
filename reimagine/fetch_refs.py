#!/usr/bin/env python3
"""One-shot helper: pull free-to-use dynamic-posture reference images from
Wikimedia Commons into input/<category>/, and record attribution in
input/CREDITS.md. Screens for a real bitmap (not SVG) and a free license.

Not part of the render pipeline — just seeds the starter reference set.
"""
import io
import json
import logging
import time
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image

logger = logging.getLogger(__name__)

API = "https://commons.wikimedia.org/w/api.php"
UA = "reimagine-refs/0.1 (dev-day; contact: local)"
INPUT = Path(__file__).parent / "input"

# Wikimedia rate-limits hard and blocks on-the-fly large thumbnails ("robot
# policy"). Be polite: pace requests and request a standard thumb width.
PACE_S = 2.0
THUMB_W = 1024


def _get(url):
    """GET with UA + exponential backoff on HTTP 429."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    delay = PACE_S
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 5:
                time.sleep(delay)
                delay *= 2
                continue
            raise
    raise RuntimeError("unreachable")

# category -> list of (slug, search phrase). One image per phrase; first
# free-licensed bitmap hit wins. Phrases chosen for clear single-subject motion.
WANTED = {
    "sports": [
        ("sprint", "athletics sprint running race track"),
        ("cycling", "road cycling racer bicycle"),
        ("gymnastics", "gymnastics vault artistic"),
        ("martial-arts", "taekwondo kick sparring"),
        ("climbing", "rock climbing sport lead"),
    ],
    "dance": [
        ("ballet-leap", "ballet dancer grand jete leap"),
        ("breakdance", "breakdance freeze b-boy"),
        ("yoga-warrior", "yoga warrior pose asana"),
        ("contemporary", "contemporary dance performance stage"),
        ("flamenco", "flamenco dancer performance"),
    ],
    "everyday": [
        ("construction", "construction worker building site"),
        ("chef", "chef cooking kitchen flame"),
        ("gardener", "gardener digging garden"),
        ("musician", "guitarist playing concert stage"),
        ("painter", "artist painting canvas studio"),
    ],
    "animals": [
        ("horse-gallop", "horse galloping racehorse"),
        ("dog-leap", "dog running jumping dock diving"),
        ("bird-takeoff", "bird taking off wings spread"),
        ("cat-pounce", "cat pouncing jump"),
        ("dolphin-breach", "dolphin jumping ocean"),
    ],
}


def _api(params):
    params = {**params, "format": "json"}
    url = f"{API}?{urllib.parse.urlencode(params)}"
    time.sleep(PACE_S)
    return json.loads(_get(url))


def search_files(phrase, limit=15):
    """Return candidate File: titles for a search phrase, best match first."""
    data = _api({
        "action": "query", "generator": "search",
        "gsrsearch": f"{phrase}", "gsrnamespace": 6, "gsrlimit": limit,
        "prop": "imageinfo",
        "iiprop": "url|extmetadata|mime|size",
        "iiurlwidth": THUMB_W,
    })
    pages = (data.get("query") or {}).get("pages") or {}
    # search generator returns an 'index' for ordering
    return sorted(pages.values(), key=lambda p: p.get("index", 999))


FREE_HINTS = ("cc0", "cc-by", "cc by", "public domain", "pdm", "attribution")


def is_free(ii):
    md = ii.get("extmetadata") or {}
    lic = (md.get("LicenseShortName", {}).get("value", "")
           or md.get("License", {}).get("value", "")).lower()
    return any(h in lic for h in FREE_HINTS) and "nd" not in lic.split()


def pick_and_download(phrase, dest):
    for page in search_files(phrase):
        ii = (page.get("imageinfo") or [{}])[0]
        mime = ii.get("mime", "")
        if not mime.startswith("image/") or "svg" in mime:
            continue
        if not is_free(ii):
            continue
        src = ii.get("thumburl") or ii.get("url")
        if not src:
            continue
        try:
            raw = _get(src)
            img = Image.open(io.BytesIO(raw)).convert("RGB")
        except Exception as error:
            logger.warning("      skip (fetch/decode %s)", error)
            continue
        img.save(dest, "JPEG", quality=90)
        md = ii.get("extmetadata") or {}
        return {
            "title": page.get("title", ""),
            "descurl": ii.get("descriptionurl", ""),
            "license": md.get("LicenseShortName", {}).get("value", "?"),
            "artist": md.get("Artist", {}).get("value", "?"),
            "wh": f"{img.width}x{img.height}",
        }
    return None


def render_credits(store):
    """Rebuild CREDITS.md from the accumulated JSON store."""
    import re
    lines = ["# Reference image credits",
             "",
             "All images from Wikimedia Commons under free licenses "
             "(CC0 / CC BY / public domain). Attribution preserved below.",
             ""]
    for cat, items in WANTED.items():
        lines.append(f"## {cat}\n")
        for slug, _ in items:
            info = store.get(f"{cat}/{slug}")
            if not info:
                lines.append(f"- **{slug}** — no free image found")
                continue
            artist = re.sub(r"<[^>]+>", "", info.get("artist", "?")).strip() or "?"
            lines.append(
                f"- **{slug}** ({info['wh']}, {info['license']}) — "
                f"{artist} — [{info['title']}]({info['descurl']})")
        lines.append("")
    (INPUT / "CREDITS.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    store_path = INPUT / ".credits.json"
    store = json.loads(store_path.read_text()) if store_path.exists() else {}
    for cat, items in WANTED.items():
        catdir = INPUT / cat
        catdir.mkdir(parents=True, exist_ok=True)
        for slug, phrase in items:
            key = f"{cat}/{slug}"
            dest = catdir / f"{slug}.jpg"
            if dest.exists() and key in store:
                logger.info("[%s] already have %s, skipping", key, dest.name)
                continue
            logger.info("[%s] %r", key, phrase)
            info = pick_and_download(phrase, dest)
            if not info:
                logger.warning("      NO FREE HIT")
                continue
            logger.info("      -> %s (%s) %s", dest.name, info["wh"],
                        info["license"])
            store[key] = info
            store_path.write_text(json.dumps(store, indent=2))  # flush per hit
    render_credits(store)
    logger.info("\nWrote %s  (%d/20 images)", INPUT / "CREDITS.md", len(store))


if __name__ == "__main__":
    main()
